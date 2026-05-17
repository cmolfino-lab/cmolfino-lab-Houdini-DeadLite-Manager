import os
import uuid
import time
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)

# In-memory database
STATE = {
    "jobs": {},     # job_id -> {id, name, status, created_at}
    "tasks": {},    # task_id -> {id, job_id, command, cwd, status, worker_id, logs, start_time, end_time}
    "workers": {},  # worker_id -> {id, status, last_seen, current_task_id}
    "job_order": [] # List of job_ids in queue order
}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/state')
def get_state():
    # Clean up dead workers (not seen in 30 seconds)
    now = time.time()
    for w_id in list(STATE["workers"].keys()):
        if now - STATE["workers"][w_id]["last_seen"] > 30:
            STATE["workers"][w_id]["status"] = "offline"

    # Update job statuses based on task statuses
    for job_id, job in STATE["jobs"].items():
        # Do not override if user explicitly paused or cancelled the job manually
        if job["status"] in ["paused", "cancelled"]:
            continue
            
        job_tasks = [t for t in STATE["tasks"].values() if t["job_id"] == job_id]
        if not job_tasks:
            continue
        
        if all(t["status"] == "completed" for t in job_tasks):
            job["status"] = "completed"
        elif any(t["status"] == "failed" for t in job_tasks):
            job["status"] = "failed"
        elif any(t["status"] == "running" for t in job_tasks):
            job["status"] = "running"
        else:
            job["status"] = "queued"
            
    # Ensure job_order is in sync with existing jobs
    if "job_order" not in STATE:
        STATE["job_order"] = []
    for job_id in STATE["jobs"]:
        if job_id not in STATE["job_order"]:
            STATE["job_order"].append(job_id)
    STATE["job_order"] = [jid for jid in STATE["job_order"] if jid in STATE["jobs"]]
            
    return jsonify(STATE)

@app.route('/api/jobs', methods=['POST'])
def submit_job():
    data = request.json
    job_name = data.get('name', 'Untitled Job')
    tasks_data = data.get('tasks', [])
    
    job_id = str(uuid.uuid4())
    STATE["jobs"][job_id] = {
        "id": job_id,
        "name": job_name,
        "status": "queued",
        "created_at": time.time()
    }
    
    if "job_order" not in STATE:
        STATE["job_order"] = []
    STATE["job_order"].append(job_id)
    
    for t_data in tasks_data:
        task_id = str(uuid.uuid4())
        STATE["tasks"][task_id] = {
            "id": task_id,
            "job_id": job_id,
            "command": t_data.get("command"),
            "cwd": t_data.get("cwd"),
            "status": "queued", # queued, running, completed, failed
            "progress": 0,
            "worker_id": None,
            "logs": "",
            "start_time": None,
            "end_time": None
        }
        
    return jsonify({"status": "ok", "job_id": job_id})

@app.route('/api/jobs/<job_id>/cancel', methods=['POST'])
def cancel_job(job_id):
    if job_id in STATE["jobs"]:
        STATE["jobs"][job_id]["status"] = "cancelled"
        for t in STATE["tasks"].values():
            if t["job_id"] == job_id:
                if t["status"] in ["queued", "running", "paused"]:
                    t["status"] = "cancelled"
        return jsonify({"status": "ok"})
    return jsonify({"error": "Job not found"}), 404

@app.route('/api/jobs/<job_id>/start', methods=['POST'])
def start_job(job_id):
    if job_id in STATE["jobs"]:
        STATE["jobs"][job_id]["status"] = "queued"
        for t in STATE["tasks"].values():
            if t["job_id"] == job_id and t["status"] in ["paused", "failed", "cancelled"]:
                t["status"] = "queued"
        return jsonify({"status": "ok"})
    return jsonify({"error": "Job not found"}), 404

@app.route('/api/jobs/<job_id>/pause', methods=['POST'])
def pause_job(job_id):
    if job_id in STATE["jobs"]:
        STATE["jobs"][job_id]["status"] = "paused"
        for t in STATE["tasks"].values():
            if t["job_id"] == job_id and t["status"] == "queued":
                t["status"] = "paused"
        return jsonify({"status": "ok"})
    return jsonify({"error": "Job not found"}), 404

@app.route('/api/tasks/<task_id>/start', methods=['POST'])
def start_task(task_id):
    if task_id in STATE["tasks"]:
        STATE["tasks"][task_id]["status"] = "queued"
        return jsonify({"status": "ok"})
    return jsonify({"error": "Task not found"}), 404

@app.route('/api/tasks/<task_id>/pause', methods=['POST'])
def pause_task(task_id):
    if task_id in STATE["tasks"]:
        if STATE["tasks"][task_id]["status"] == "queued":
            STATE["tasks"][task_id]["status"] = "paused"
        return jsonify({"status": "ok"})
    return jsonify({"error": "Task not found"}), 404

@app.route('/api/tasks/<task_id>/cancel', methods=['POST'])
def cancel_task(task_id):
    if task_id in STATE["tasks"]:
        t = STATE["tasks"][task_id]
        if t["status"] in ["queued", "running", "paused"]:
            t["status"] = "cancelled"
        return jsonify({"status": "ok"})
    return jsonify({"error": "Task not found"}), 404

@app.route('/api/jobs/<job_id>/delete', methods=['DELETE'])
def delete_job(job_id):
    if job_id in STATE["jobs"]:
        del STATE["jobs"][job_id]
        
        # Delete from job_order
        if "job_order" in STATE and job_id in STATE["job_order"]:
            STATE["job_order"].remove(job_id)
            
        # Delete associated tasks
        tasks_to_delete = [t_id for t_id, t in STATE["tasks"].items() if t["job_id"] == job_id]
        for t_id in tasks_to_delete:
            del STATE["tasks"][t_id]
            
        return jsonify({"status": "ok"})
    return jsonify({"error": "Job not found"}), 404

@app.route('/api/tasks/<task_id>/status', methods=['GET'])
def check_task_status(task_id):
    if task_id in STATE["tasks"]:
        return jsonify({"status": STATE["tasks"][task_id]["status"]})
    return jsonify({"error": "Task not found"}), 404

@app.route('/api/jobs/upload', methods=['POST'])
def upload_job():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
        
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400
        
    if file and file.filename.endswith('.bat'):
        content = file.read().decode('utf-8')
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
            
        # Create Job
        job_id = str(uuid.uuid4())
        STATE["jobs"][job_id] = {
            "id": job_id,
            "name": job_name,
            "status": "paused", # changed from queued
            "created_at": time.time()
        }
        
        if "job_order" not in STATE:
            STATE["job_order"] = []
        STATE["job_order"].append(job_id)
        
        for t_data in tasks_data:
            task_id = str(uuid.uuid4())
            STATE["tasks"][task_id] = {
                "id": task_id,
                "job_id": job_id,
                "command": t_data["command"],
                "cwd": t_data["cwd"],
                "status": "paused", # changed from queued
                "progress": 0,
                "worker_id": None,
                "logs": "",
                "start_time": None,
                "end_time": None
            }
            
        return jsonify({"status": "ok", "job_id": job_id})
        
    return jsonify({"error": "Invalid file type"}), 400

@app.route('/api/jobs/<job_id>/move', methods=['POST'])
def move_job(job_id):
    if job_id not in STATE["jobs"]:
        return jsonify({"error": "Job not found"}), 404
        
    data = request.json or {}
    direction = data.get("direction")
    if direction not in ["up", "down"]:
        return jsonify({"error": "Invalid direction"}), 400
        
    # Ensure job_order is in sync
    if "job_order" not in STATE:
        STATE["job_order"] = []
    for jid in STATE["jobs"]:
        if jid not in STATE["job_order"]:
            STATE["job_order"].append(jid)
    STATE["job_order"] = [jid for jid in STATE["job_order"] if jid in STATE["jobs"]]
    
    try:
        idx = STATE["job_order"].index(job_id)
    except ValueError:
        return jsonify({"error": "Job not in order list"}), 400
        
    if direction == "up" and idx > 0:
        # Swap with previous
        STATE["job_order"][idx], STATE["job_order"][idx-1] = STATE["job_order"][idx-1], STATE["job_order"][idx]
    elif direction == "down" and idx < len(STATE["job_order"]) - 1:
        # Swap with next
        STATE["job_order"][idx], STATE["job_order"][idx+1] = STATE["job_order"][idx+1], STATE["job_order"][idx]
        
    return jsonify({"status": "ok", "job_order": STATE["job_order"]})

@app.route('/api/worker/poll', methods=['POST'])
def worker_poll():
    data = request.json
    worker_id = data.get('worker_id')
    
    if not worker_id:
        return jsonify({"error": "worker_id required"}), 400
        
    # Register/Update worker
    if worker_id not in STATE["workers"]:
        STATE["workers"][worker_id] = {"id": worker_id, "status": "idle", "last_seen": time.time(), "current_task_id": None}
    
    worker = STATE["workers"][worker_id]
    worker["last_seen"] = time.time()
    
    # If worker is already running a task, don't assign a new one
    if worker["status"] == "running" and worker["current_task_id"]:
        return jsonify({"status": "wait", "message": "already running a task"})
        
    # Find next queued task in order of job priority (job_order)
    # job_order[0] is the highest priority job
    if "job_order" not in STATE:
        STATE["job_order"] = []
    for job_id in STATE["job_order"]:
        job = STATE["jobs"].get(job_id)
        if job and job["status"] not in ["paused", "cancelled"]:
            # Find queued tasks for this job
            job_tasks = [t for t in STATE["tasks"].values() if t["job_id"] == job_id and t["status"] == "queued"]
            if job_tasks:
                # Sort tasks by frame number
                def get_frame(task):
                    import re
                    match = re.search(r'-f\s+([0-9.]+)', task.get("command", ""))
                    if match:
                        try:
                            return float(match.group(1))
                        except ValueError:
                            pass
                    return 0.0
                job_tasks.sort(key=get_frame)
                
                task = job_tasks[0]
                task_id = task["id"]
                
                # Assign task
                task["status"] = "running"
                task["worker_id"] = worker_id
                task["start_time"] = time.time()
                
                worker["status"] = "running"
                worker["current_task_id"] = task_id
                
                return jsonify({"status": "task", "task": task})
            
    worker["status"] = "idle"
    worker["current_task_id"] = None
    return jsonify({"status": "wait"})

@app.route('/api/worker/update', methods=['POST'])
def worker_update():
    data = request.json
    worker_id = data.get('worker_id')
    task_id = data.get('task_id')
    task_status = data.get('status')
    logs = data.get('logs', '')
    progress = data.get('progress')
    
    if worker_id in STATE["workers"]:
        STATE["workers"][worker_id]["last_seen"] = time.time()
        # Free the worker if task is finished or cancelled
        if task_status in ["completed", "failed", "cancelled"]:
            STATE["workers"][worker_id]["status"] = "idle"
            STATE["workers"][worker_id]["current_task_id"] = None
        
    if task_id in STATE["tasks"]:
        task = STATE["tasks"][task_id]
        if logs:
            task["logs"] += logs
        if progress is not None:
            task["progress"] = progress
        if task_status in ["completed", "failed", "cancelled"]:
            task["status"] = task_status
            task["end_time"] = time.time()
            if task_status == "completed":
                task["progress"] = 100
                
    return jsonify({"status": "ok"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
