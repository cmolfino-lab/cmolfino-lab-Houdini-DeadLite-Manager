import locale
import os
import time
import uuid
import threading
from functools import wraps
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)

# Bumped whenever the messages between worker and manager change.
# A worker on another version is shown as outdated and never given work.
PROTOCOL = 3

WORKER_TIMEOUT = 30  # seconds of silence before a worker counts as offline
MAX_ATTEMPTS = 3     # times a task is handed out before it is failed for good

# In-memory database
#
# Jobs come in two kinds. A .bat upload gives tasks that are a command line and
# a folder. A job submitted from Houdini is described instead (type, scene,
# Houdini version, frames) and each worker builds the command line itself,
# because only the worker knows where its Houdini is installed.
STATE = {
    "jobs": {},     # job_id -> {id, name, status, created_at} plus, from Houdini: {type, houdini, scene, args, node, hip_name, output_dir}
    "tasks": {},    # task_id -> {id, job_id, index, command, cwd, frames, done_frames, failed_on, warning, status, progress, worker_id, attempt, attempt_id, logs, start_time, end_time}
    "workers": {},  # worker_id -> {id, status, last_seen, current_task_id, outdated, houdini}
    "job_order": [] # List of job_ids in queue order
}

# job_id -> {frame: file that frame writes}. Kept outside STATE because the
# dashboard downloads STATE every two seconds and has no use for it.
OUTPUTS = {}

# Flask serves each request on its own thread
LOCK = threading.Lock()

def locked(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        with LOCK:
            return view(*args, **kwargs)
    return wrapper

# --- Task state machine ---

# status -> statuses a task may move to from there
TRANSITIONS = {
    "queued":    {"running", "paused", "cancelled"},
    "paused":    {"queued", "cancelled"},
    "running":   {"completed", "failed", "cancelled", "queued"},
    "completed": {"queued"},
    "failed":    {"queued"},
    "cancelled": {"queued"},
}

def set_status(task, new_status, worker_id=None):
    """The only place a task's status changes. Returns False if the move is not allowed."""
    old_status = task["status"]
    if new_status not in TRANSITIONS[old_status]:
        return False

    if new_status == "running":
        # Each hand-out is a new attempt. Only the worker holding attempt_id may report on the task.
        task["attempt"] += 1
        task["attempt_id"] = str(uuid.uuid4())
        task["worker_id"] = worker_id
        task["warning"] = None
        task["progress"] = 0
        task["start_time"] = time.time()
        task["end_time"] = None
        if task["logs"]:
            task["logs"] += f"\n===== attempt {task['attempt']} on {worker_id} =====\n"
    elif new_status == "queued":
        task["attempt_id"] = None
        task["worker_id"] = None
        task["progress"] = 0
        task["start_time"] = None
        task["end_time"] = None
    elif old_status == "running":
        # completed, failed or cancelled: the attempt is over, late reports from its worker are ignored
        task["attempt_id"] = None
        task["end_time"] = time.time()
        if new_status == "completed":
            task["progress"] = 100

    task["status"] = new_status
    return True

def requeue(task):
    """Manual restart: back in the queue with a clean slate."""
    was_completed = task["status"] == "completed"
    if set_status(task, "queued"):
        task["attempt"] = 0
        task["logs"] = ""
        task["failed_on"] = []
        if was_completed:
            # Asked to render a finished task again: do all of it. A task that
            # was cancelled or failed keeps its finished frames and resumes.
            task["done_frames"] = []

def release_task(task, reason):
    """A running task lost its worker: put it back in the queue, or fail it after MAX_ATTEMPTS."""
    task["logs"] += f"\n[manager] {reason}\n"
    if task["attempt"] >= MAX_ATTEMPTS:
        task["logs"] += f"[manager] giving up after {task['attempt']} attempts\n"
        set_status(task, "failed")
    else:
        set_status(task, "queued")

def remaining_frames(task):
    return [f for f in task["frames"] if f not in task["done_frames"]]

def job_tasks(job_id):
    tasks = [t for t in STATE["tasks"].values() if t["job_id"] == job_id]
    tasks.sort(key=lambda t: t["index"])
    return tasks

def job_status(job_id):
    """A job has no status of its own, it is a summary of its tasks."""
    statuses = {t["status"] for t in job_tasks(job_id)}
    for status in ["running", "queued"]:
        if status in statuses:
            return status
    for status in ["failed", "paused", "cancelled"]:
        if status in statuses:
            return status
    return "completed"

def create_job(name, tasks_data, status, **details):
    job_id = str(uuid.uuid4())
    STATE["jobs"][job_id] = {
        "id": job_id,
        "name": name,
        "status": status,
        "created_at": time.time()
    }
    STATE["jobs"][job_id].update(details)
    STATE["job_order"].append(job_id)

    for index, t_data in enumerate(tasks_data):
        task_id = str(uuid.uuid4())
        STATE["tasks"][task_id] = {
            "id": task_id,
            "job_id": job_id,
            "index": index, # position in the submitted file
            "command": t_data.get("command"),
            "cwd": t_data.get("cwd"),
            "frames": t_data.get("frames", []), # jobs from Houdini only
            "done_frames": [], # frames a worker has seen written to disk
            "failed_on": [],   # workers that reported this task as failed
            "warning": None,   # e.g. finished, but some expected files were not found
            "status": status, # queued, paused, running, completed, failed, cancelled
            "progress": 0,
            "worker_id": None,
            "attempt": 0,
            "attempt_id": None,
            "logs": "",
            "start_time": None,
            "end_time": None
        }
    return job_id

def whole_number(value):
    """Frames arrive as JSON numbers: 12.0 and 12 are the same frame."""
    return int(value) if float(value).is_integer() else float(value)

def chunk_frames(start, end, step, size):
    """Frames start..end cut into lists of `size` frames. A size of 0 keeps them together."""
    count = int((end - start) / step + 1e-9) + 1
    frames = [whole_number(start + i * step) for i in range(count)]
    if size <= 0:
        return [frames]
    return [frames[i:i + size] for i in range(0, len(frames), size)]

def job_from_houdini(data):
    """Check a job description sent by the Houdini submit tool and turn it into create_job() arguments.

    Raises ValueError with a message meant for the artist.
    """
    job_type = data.get('type')
    if job_type not in ("husk", "hython"):
        raise ValueError(f"Unknown job type: {job_type!r}")
    for field in ("houdini", "scene"):
        if not data.get(field) or not isinstance(data[field], str):
            raise ValueError(f"Missing '{field}'")
    if job_type == "hython" and not data.get('node'):
        raise ValueError("A hython job needs the path of the node to render")

    try:
        start, end, step = (float(v) for v in data.get('frames'))
        chunk = int(data.get('chunk', 0))
    except (TypeError, ValueError):
        raise ValueError("'frames' must be [start, end, step] and 'chunk' a number of frames")
    if step <= 0 or end < start:
        raise ValueError(f"Bad frame range: {start:g} to {end:g} step {step:g}")

    scene_name = os.path.basename(data['scene'])
    tasks_data = []
    for frames in chunk_frames(start, end, step, chunk):
        tasks_data.append({
            "command": f"{job_type} {scene_name} frames {frames[0]}-{frames[-1]}",
            "frames": frames
        })

    outputs = data.get('outputs') or {}
    details = {
        "type": job_type,
        "houdini": data['houdini'],
        "scene": data['scene'],
        "args": [str(a) for a in data.get('args') or []],
        "node": data.get('node'),
        "hip_name": data.get('hip_name'),
        "output_dir": os.path.dirname(next(iter(outputs.values()))) if outputs else data.get('output_dir')
    }
    return tasks_data, details, {str(frame): path for frame, path in outputs.items()}

# --- Workers ---

def touch_worker(worker_id):
    if worker_id not in STATE["workers"]:
        STATE["workers"][worker_id] = {"id": worker_id, "status": "idle", "last_seen": time.time(), "current_task_id": None, "outdated": False, "houdini": []}
    worker = STATE["workers"][worker_id]
    worker["last_seen"] = time.time()
    return worker

def can_run(worker, job):
    """A job from Houdini needs that Houdini version on the worker. A .bat job brings its own path."""
    return not job.get("houdini") or job["houdini"] in worker["houdini"]

def another_worker_could_try(task, job, worker_id):
    """Is some other machine up that can run this task and has not already failed it?"""
    for other in STATE["workers"].values():
        if other["id"] == worker_id or other["id"] in task["failed_on"]:
            continue
        if other["status"] != "offline" and not other["outdated"] and can_run(other, job):
            return True
    return False

def release_tasks_of(worker_id, reason):
    for task in STATE["tasks"].values():
        if task["status"] == "running" and task["worker_id"] == worker_id:
            release_task(task, reason)

def sweep_workers():
    """Mark silent workers offline and take back whatever they were rendering."""
    now = time.time()
    for worker in STATE["workers"].values():
        if worker["status"] != "offline" and now - worker["last_seen"] > WORKER_TIMEOUT:
            worker["status"] = "offline"
            worker["current_task_id"] = None
            release_tasks_of(worker["id"], f"worker {worker['id']} stopped responding")

def next_queued_task(worker):
    # job_order[0] is the highest priority job
    for job_id in STATE["job_order"]:
        job = STATE["jobs"][job_id]
        if not can_run(worker, job):
            continue
        for task in job_tasks(job_id):
            if task["status"] != "queued":
                continue
            # A task that failed here goes to the other machine first: the two
            # are not identical (VRAM, installed plugins), so it may well work there
            if worker["id"] in task["failed_on"] and another_worker_could_try(task, job, worker["id"]):
                continue
            return task
    return None

def task_for_worker(task):
    """What a worker needs to know to run a task."""
    job = STATE["jobs"][task["job_id"]]
    message = {
        "id": task["id"],
        "attempt_id": task["attempt_id"],
        "command": task["command"],
        "cwd": task["cwd"]
    }
    if job.get("type"):
        frames = remaining_frames(task)
        outputs = OUTPUTS.get(job["id"], {})
        message.update({
            "type": job["type"],
            "houdini": job["houdini"],
            "scene": job["scene"],
            "args": job["args"],
            "node": job["node"],
            "hip_name": job["hip_name"],
            "frames": frames,
            "outputs": {str(f): outputs[str(f)] for f in frames if str(f) in outputs}
        })
    return message

# --- Dashboard ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/state')
@locked
def get_state():
    sweep_workers()
    workers_up = [w for w in STATE["workers"].values() if w["status"] != "offline" and not w["outdated"]]
    for job_id, job in STATE["jobs"].items():
        job["status"] = job_status(job_id)
        # Say why a queued job is not moving when no machine could ever take it
        job["waiting_for"] = None
        if job["status"] == "queued" and not any(can_run(w, job) for w in workers_up):
            job["waiting_for"] = f"a worker with Houdini {job['houdini']}" if job.get("houdini") else "a worker"
    return jsonify(STATE)

@app.route('/api/jobs', methods=['POST'])
@locked
def submit_job():
    data = request.json or {}
    job_name = data.get('name', 'Untitled Job')
    status = "paused" if data.get('paused') else "queued"

    if data.get('type'):
        # Submitted from Houdini
        try:
            tasks_data, details, outputs = job_from_houdini(data)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        job_id = create_job(job_name, tasks_data, status, **details)
        OUTPUTS[job_id] = outputs
        return jsonify({"status": "ok", "job_id": job_id, "tasks": len(tasks_data)})

    tasks_data = data.get('tasks', [])
    if not tasks_data:
        return jsonify({"error": "A job needs at least one task"}), 400

    job_id = create_job(job_name, tasks_data, status)
    return jsonify({"status": "ok", "job_id": job_id})

@app.route('/api/jobs/<job_id>/open-output', methods=['POST'])
@locked
def open_output(job_id):
    """Open the job's output folder in Explorer, on the machine the manager runs on."""
    job = STATE["jobs"].get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    folder = job.get("output_dir")
    # Only ever a folder: startfile on a file would run it
    if not folder or not os.path.isdir(folder):
        return jsonify({"error": f"Output folder does not exist yet: {folder}"}), 404
    os.startfile(folder)
    return jsonify({"status": "ok"})

@app.route('/api/jobs/<job_id>/cancel', methods=['POST'])
@locked
def cancel_job(job_id):
    if job_id in STATE["jobs"]:
        for t in job_tasks(job_id):
            set_status(t, "cancelled")
        return jsonify({"status": "ok"})
    return jsonify({"error": "Job not found"}), 404

@app.route('/api/jobs/<job_id>/start', methods=['POST'])
@locked
def start_job(job_id):
    if job_id in STATE["jobs"]:
        for t in job_tasks(job_id):
            if t["status"] in ["paused", "failed", "cancelled"]:
                requeue(t)
        return jsonify({"status": "ok"})
    return jsonify({"error": "Job not found"}), 404

@app.route('/api/jobs/<job_id>/pause', methods=['POST'])
@locked
def pause_job(job_id):
    # Running tasks finish their chunk, everything still waiting is held back
    if job_id in STATE["jobs"]:
        for t in job_tasks(job_id):
            set_status(t, "paused")
        return jsonify({"status": "ok"})
    return jsonify({"error": "Job not found"}), 404

@app.route('/api/tasks/<task_id>/start', methods=['POST'])
@locked
def start_task(task_id):
    if task_id in STATE["tasks"]:
        t = STATE["tasks"][task_id]
        if t["status"] in ["paused", "failed", "cancelled", "completed"]:
            requeue(t)
        return jsonify({"status": "ok"})
    return jsonify({"error": "Task not found"}), 404

@app.route('/api/tasks/<task_id>/pause', methods=['POST'])
@locked
def pause_task(task_id):
    if task_id in STATE["tasks"]:
        set_status(STATE["tasks"][task_id], "paused")
        return jsonify({"status": "ok"})
    return jsonify({"error": "Task not found"}), 404

@app.route('/api/tasks/<task_id>/cancel', methods=['POST'])
@locked
def cancel_task(task_id):
    if task_id in STATE["tasks"]:
        set_status(STATE["tasks"][task_id], "cancelled")
        return jsonify({"status": "ok"})
    return jsonify({"error": "Task not found"}), 404

@app.route('/api/jobs/<job_id>/delete', methods=['DELETE'])
@locked
def delete_job(job_id):
    if job_id in STATE["jobs"]:
        del STATE["jobs"][job_id]
        STATE["job_order"].remove(job_id)
        OUTPUTS.pop(job_id, None)

        # Delete associated tasks. A worker still rendering one is told to stop on its next report.
        tasks_to_delete = [t_id for t_id, t in STATE["tasks"].items() if t["job_id"] == job_id]
        for t_id in tasks_to_delete:
            del STATE["tasks"][t_id]

        return jsonify({"status": "ok"})
    return jsonify({"error": "Job not found"}), 404

@app.route('/api/jobs/upload', methods=['POST'])
@locked
def upload_job():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    if file and file.filename.endswith('.bat'):
        raw = file.read()
        try:
            content = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            # Houdini writes the .bat in the Windows code page, which is not UTF-8 once a path has an accent
            content = raw.decode(locale.getpreferredencoding(False), errors='replace')
        lines = content.split('\n')

        job_name = file.filename
        tasks_data = []
        cwd = None

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Keep track of directory changes
            if line.startswith('cd /d') or line.startswith('cd '):
                # Extract path
                cwd = line.split(' ', 2)[-1].strip('"')
                continue

            # If it's a render command
            if line.startswith('hython') or line.startswith('hrender') or line.startswith('husk'):
                tasks_data.append({
                    "command": line,
                    "cwd": cwd
                })

        if not tasks_data:
            return jsonify({"error": "No valid render commands found in .bat file"}), 400

        # Uploaded jobs wait for Start, so a single task can be test rendered first
        job_id = create_job(job_name, tasks_data, "paused")
        return jsonify({"status": "ok", "job_id": job_id})

    return jsonify({"error": "Invalid file type"}), 400

@app.route('/api/jobs/<job_id>/move', methods=['POST'])
@locked
def move_job(job_id):
    if job_id not in STATE["jobs"]:
        return jsonify({"error": "Job not found"}), 404

    data = request.json or {}
    direction = data.get("direction")
    if direction not in ["up", "down"]:
        return jsonify({"error": "Invalid direction"}), 400

    idx = STATE["job_order"].index(job_id)

    if direction == "up" and idx > 0:
        # Swap with previous
        STATE["job_order"][idx], STATE["job_order"][idx-1] = STATE["job_order"][idx-1], STATE["job_order"][idx]
    elif direction == "down" and idx < len(STATE["job_order"]) - 1:
        # Swap with next
        STATE["job_order"][idx], STATE["job_order"][idx+1] = STATE["job_order"][idx+1], STATE["job_order"][idx]

    return jsonify({"status": "ok", "job_order": STATE["job_order"]})

# --- Worker API ---

@app.route('/api/worker/poll', methods=['POST'])
@locked
def worker_poll():
    """An idle worker asking for work."""
    data = request.json or {}
    worker_id = data.get('worker_id')

    if not worker_id:
        return jsonify({"error": "worker_id required"}), 400

    worker = touch_worker(worker_id)
    worker["outdated"] = data.get('protocol') != PROTOCOL
    worker["houdini"] = data.get('houdini') or []
    worker["status"] = "idle"
    worker["current_task_id"] = None
    sweep_workers()

    if worker["outdated"]:
        return jsonify({"status": "wait", "protocol": PROTOCOL, "message": "worker and manager are different versions"})

    # A worker only polls when it has nothing to do. If we still think it is
    # rendering, it was closed or crashed mid-task and that task needs a new home.
    release_tasks_of(worker_id, f"worker {worker_id} restarted mid-task")

    while True:
        task = next_queued_task(worker)
        if not task:
            return jsonify({"status": "wait", "protocol": PROTOCOL})

        set_status(task, "running", worker_id)
        if task["frames"] and not remaining_frames(task):
            # Restarted after its last frame was already written: nothing to render
            task["logs"] += "\n[manager] every frame of this task is already on disk\n"
            set_status(task, "completed")
            continue

        worker["status"] = "running"
        worker["current_task_id"] = task["id"]
        return jsonify({"status": "task", "protocol": PROTOCOL, "task": task_for_worker(task)})

@app.route('/api/worker/update', methods=['POST'])
@locked
def worker_update():
    """Progress report from a rendering worker. Doubles as its heartbeat and as the way it hears about a cancel."""
    data = request.json or {}
    worker_id = data.get('worker_id')
    task_id = data.get('task_id')
    attempt_id = data.get('attempt_id')
    task_status = data.get('status')
    logs = data.get('logs', '')
    progress = data.get('progress')

    if not worker_id:
        return jsonify({"error": "worker_id required"}), 400
    worker = touch_worker(worker_id)

    # Only the current attempt of a running task is allowed to report. Anything
    # else is a render we no longer want (cancelled, deleted, handed to someone
    # else, or from before a manager restart), so the worker is told to kill it.
    task = STATE["tasks"].get(task_id)
    if not task or task["status"] != "running" or not attempt_id or task["attempt_id"] != attempt_id:
        worker["status"] = "idle"
        worker["current_task_id"] = None
        return jsonify({"status": "stop"})

    worker["status"] = "running"
    worker["current_task_id"] = task_id

    if logs:
        task["logs"] += logs
    if progress is not None:
        task["progress"] = progress
    for frame in data.get('frames_done') or []:
        if frame in task["frames"] and frame not in task["done_frames"]:
            task["done_frames"].append(frame)
    task["warning"] = data.get('warning')

    if task_status in ["completed", "failed", "cancelled"]:
        job = STATE["jobs"][task["job_id"]]
        if task_status == "failed":
            task["failed_on"].append(worker_id)
        if task_status == "failed" and task["attempt"] < MAX_ATTEMPTS and another_worker_could_try(task, job, worker_id):
            task["logs"] += f"\n[manager] failed on {worker_id}, queued again for another machine\n"
            set_status(task, "queued")
        else:
            set_status(task, task_status)
        worker["status"] = "idle"
        worker["current_task_id"] = None

    return jsonify({"status": "ok"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, threaded=True)
