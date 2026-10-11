import os
import sys
import time
import json
import socket
import shutil
import requests
import subprocess
import threading
import re
import ctypes
from ctypes import wintypes
import urllib3.util.connection

# Must match PROTOCOL in manager/app.py
PROTOCOL = 3

# The script hython runs for jobs submitted from Houdini. It sits next to this
# file, or inside the bundle once this is built into an exe.
RUNNER = os.path.join(getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__))), 'hython_runner.py')

# The manager listens on IPv4 only. A hostname in MANAGER_URL usually resolves
# to IPv6 addresses as well, and those are tried first: every request then
# waits out a full timeout before falling back to the address that works.
urllib3.util.connection.HAS_IPV6 = False

POLL_INTERVAL = 3    # seconds between polls while idle
REPORT_INTERVAL = 2  # seconds between progress reports while rendering
MAX_PENDING_LOG = 1024 * 1024  # characters of log kept while the manager is unreachable

ALF_PROGRESS = re.compile(r'ALF_PROGRESS\s+([0-9]+)%')

def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}")

def load_config():
    # Determine the directory of the executable or script
    if getattr(sys, 'frozen', False):
        application_path = os.path.dirname(sys.executable)
    else:
        # If running as script, put config next to script or in current dir
        application_path = os.getcwd()

    config_path = os.path.join(application_path, 'config.json')

    # Default config
    config = {
        "MANAGER_URL": "http://127.0.0.1:5000"
    }

    try:
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                config.update(json.load(f))
        else:
            with open(config_path, 'w') as f:
                json.dump(config, f, indent=4)
    except Exception as e:
        print(f"Warning: Could not create/read config at {config_path}: {e}")
        # Fallback to current working directory
        config_path = os.path.join(os.getcwd(), 'config.json')
        try:
            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    config.update(json.load(f))
            else:
                with open(config_path, 'w') as f:
                    json.dump(config, f, indent=4)
        except Exception:
            pass # give up and use defaults

    return config, config_path

# --- Windows process control ---

kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]

ntdll = ctypes.WinDLL('ntdll')
ntdll.NtResumeProcess.restype = ctypes.c_long
ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_SUSPEND_RESUME = 0x0800
CREATE_SUSPENDED = 0x00000004
ERROR_ALREADY_EXISTS = 183

class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
        'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('PerProcessUserTimeLimit', ctypes.c_int64),
        ('PerJobUserTimeLimit', ctypes.c_int64),
        ('LimitFlags', wintypes.DWORD),
        ('MinimumWorkingSetSize', ctypes.c_size_t),
        ('MaximumWorkingSetSize', ctypes.c_size_t),
        ('ActiveProcessLimit', wintypes.DWORD),
        ('Affinity', ctypes.c_size_t),
        ('PriorityClass', wintypes.DWORD),
        ('SchedulingClass', wintypes.DWORD),
    ]

class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('BasicLimitInformation', JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ('IoInfo', IO_COUNTERS),
        ('ProcessMemoryLimit', ctypes.c_size_t),
        ('JobMemoryLimit', ctypes.c_size_t),
        ('PeakProcessMemoryUsed', ctypes.c_size_t),
        ('PeakJobMemoryUsed', ctypes.c_size_t),
    ]

class JobObject:
    """Windows Job Object: a container whose processes can all be killed at once.

    A render is a process tree (hython starts husk, a .bat starts cmd.exe), so
    killing only the process we launched leaves the real render running. While
    the job is armed, Windows also kills the tree if the worker itself is closed
    or crashes, so a render never outlives the worker that is watching it.
    """

    def __init__(self):
        self.handle = kernel32.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._set_limits(JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)

    def _set_limits(self, flags):
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = flags
        if not kernel32.SetInformationJobObject(self.handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())

    def add(self, pid):
        """Put a process that was created suspended into the job, then let it run."""
        process = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_SUSPEND_RESUME, False, pid)
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel32.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
            status = ntdll.NtResumeProcess(process)
            if status != 0:
                raise OSError(f"Could not resume process {pid} (NTSTATUS 0x{status & 0xFFFFFFFF:08X})")
        finally:
            kernel32.CloseHandle(process)

    def kill(self):
        kernel32.TerminateJobObject(self.handle, 1)

    def release(self):
        """Let go of the job without killing what is left in it (e.g. a license helper the render started)."""
        if self.handle:
            try:
                self._set_limits(0)
            except OSError:
                pass
            kernel32.CloseHandle(self.handle)
            self.handle = None

def start_in_job(job, command, cwd=None, env=None):
    """Start a process as a member of a job, with its output on a pipe.

    The process is created suspended and only resumed once it is in the job.
    Otherwise it could start a child before we get to it, and that child would
    be outside the job and survive a cancel.
    """
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding='utf-8',
        errors='replace',
        bufsize=1,
        creationflags=CREATE_SUSPENDED
    )
    try:
        job.add(process.pid)
    except OSError:
        process.kill()
        raise
    return process

def resolve_command(command, cwd):
    """Replace the program at the start of a command line with its full path.

    The .bat files rely on cmd.exe finding hython/husk in the folder they cd
    into. We start the program directly, with no cmd.exe in between, so that
    lookup happens here.
    """
    if cwd and not os.path.isdir(cwd):
        raise FileNotFoundError(f"Folder not found on {socket.gethostname()}: {cwd}")

    match = re.match(r'\s*(?:"([^"]+)"|(\S+))(.*)', command, re.S)
    if not match:
        raise ValueError("Empty command")
    program = match.group(1) or match.group(2)
    arguments = match.group(3)

    search_path = os.pathsep.join(p for p in [cwd, os.environ.get('PATH', '')] if p)
    found = shutil.which(program, path=search_path)
    if not found:
        raise FileNotFoundError(f"'{program}' not found on {socket.gethostname()} (looked in {cwd or 'PATH'})")
    return f'"{os.path.normpath(found)}"{arguments}'

# --- Jobs submitted from Houdini ---

def program_in(folder, name):
    for extension in ('.exe', '.cmd', '.bat'):
        path = os.path.join(folder, name + extension)
        if os.path.isfile(path):
            return path
    return None

def find_houdini(extra_dirs=()):
    """Houdini installs on this machine, as {version: bin folder}.

    Looks in the standard install folder plus any folders listed under
    HOUDINI_DIRS in config.json.
    """
    roots = [os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'), 'Side Effects Software')]
    roots.extend(extra_dirs)

    installs = {}
    for root in roots:
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            match = re.fullmatch(r'Houdini (\d+\.\d+\.\d+)', name)
            bin_dir = os.path.join(root, name, 'bin')
            if match and program_in(bin_dir, 'husk'):
                installs[match.group(1)] = bin_dir
    return installs

def husk_frame_args(frames):
    steps = {b - a for a, b in zip(frames, frames[1:])}
    if len(steps) <= 1:
        step = steps.pop() if steps else 1
        return ['-f', str(frames[0]), '-n', str(len(frames)), '-i', str(step)]
    # Resuming a task with gaps in it, e.g. frames 4 and 9 are all that is left.
    # husk wants each frame as its own argument, not one quoted list.
    return ['--frame-list'] + [str(f) for f in frames]

def houdini_command(task, installs):
    """Command line (as a list) and working folder for a task described by the Houdini submit tool."""
    host = socket.gethostname()
    bin_dir = installs.get(task["houdini"])
    if not bin_dir:
        found = ', '.join(sorted(installs)) or 'none'
        raise FileNotFoundError(f"Houdini {task['houdini']} is not installed on {host} (found: {found})")
    if not os.path.isfile(task["scene"]):
        raise FileNotFoundError(f"Scene file cannot be reached from {host}: {task['scene']}")

    if task["type"] == "husk":
        # The args are the options the artist's render node asked for. Add the
        # two the queue relies on unless they are already there: husk refuses
        # an option given twice.
        command = [program_in(bin_dir, 'husk')] + task["args"]
        if not any(arg.startswith('-V') or arg == '--verbose' for arg in task["args"]):
            command.append('-Va')
        if '--make-output-path' not in task["args"]:
            command.append('--make-output-path')
        # The scene is named with --usd-input instead of being left as the last
        # argument, because --frame-list swallows every argument after it
        command += ['--usd-input', task["scene"]]
        command += husk_frame_args(task["frames"])
    else:
        command = [program_in(bin_dir, 'hython'), RUNNER, '--hip', task["scene"], '--node', task["node"]]
        if task.get("hip_name"):
            command += ['--name', task["hip_name"]]
        command += ['--frames'] + [str(f) for f in task["frames"]]
    return command, bin_dir

class OutputWatch:
    """Tells which frames of a task are finished by watching the files they write.

    This is what lets a cancelled or failed task pick up where it stopped, and
    it catches a render that exits normally without writing anything.
    """

    def __init__(self, outputs):
        # outputs comes from JSON, so its frame numbers are strings
        self.paths = {(int(f) if f.lstrip('-').isdigit() else float(f)): path for f, path in outputs.items()}
        self.before = {frame: self.signature(path) for frame, path in self.paths.items()}
        self.last_seen = dict(self.before)
        self.done = []

    @staticmethod
    def signature(path):
        try:
            info = os.stat(path)
            return info.st_mtime_ns, info.st_size
        except OSError:
            return None

    def check(self, render_ended=False):
        """Frames finished so far.

        A frame counts once its file is new and looked the same on two checks
        in a row, so a file the renderer is still writing is not mistaken for
        a finished one. After a clean exit every new file counts.
        """
        for frame, path in self.paths.items():
            if frame in self.done:
                continue
            now = self.signature(path)
            is_new = now is not None and now != self.before[frame]
            if is_new and (render_ended or now == self.last_seen[frame]):
                self.done.append(frame)
            self.last_seen[frame] = now
        return self.done

    def missing(self):
        return [frame for frame in self.paths if frame not in self.done]

class TaskOutput:
    """What the render has printed so far and not yet sent to the manager."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pending = ""
        self.progress = 0

    def add(self, line):
        match = ALF_PROGRESS.search(line)
        with self.lock:
            if match:
                self.progress = int(match.group(1))
            self.pending += line
            if len(self.pending) > MAX_PENDING_LOG:
                self.pending = "[... older output dropped ...]\n" + self.pending[-MAX_PENDING_LOG // 2:]

    def take(self):
        with self.lock:
            text, self.pending = self.pending, ""
            return text, self.progress

    def put_back(self, text):
        with self.lock:
            self.pending = text + self.pending

class Worker:
    def __init__(self, manager_url, worker_id, houdini_dirs=()):
        self.manager_url = manager_url.rstrip('/')
        self.worker_id = worker_id
        self.houdini_dirs = houdini_dirs
        self.installs = {}

    def post(self, path, payload):
        """Returns the manager's reply, or None if the message did not get through."""
        try:
            response = requests.post(f"{self.manager_url}{path}", json=payload, timeout=5)
            response.raise_for_status()
            return response.json()
        except (requests.exceptions.RequestException, ValueError) as e:
            log(f"Connection to manager failed: {e}")
            return None

    def poll_for_task(self):
        # Looked up on every poll, so a Houdini installed while the worker runs is picked up
        self.installs = find_houdini(self.houdini_dirs)
        reply = self.post("/api/worker/poll", {
            "worker_id": self.worker_id,
            "protocol": PROTOCOL,
            "houdini": sorted(self.installs)
        })
        if reply is None:
            return None
        if reply.get("protocol") != PROTOCOL:
            log("Manager and worker are different versions. Update both to the same build.")
            return None
        if reply.get("status") == "task":
            return reply.get("task")
        return None

    def report(self, task, status, logs="", progress=None, frames_done=(), warning=None):
        payload = {
            "worker_id": self.worker_id,
            "task_id": task["id"],
            "attempt_id": task["attempt_id"],
            "status": status,
            "logs": logs,
            "frames_done": list(frames_done),
            "warning": warning
        }
        if progress is not None:
            payload["progress"] = progress
        return self.post("/api/worker/update", payload)

    def report_final(self, task, status, logs="", progress=None, frames_done=(), warning=None):
        # The manager only learns a task ended from this message, so keep trying until it gets through
        delay = 1
        while self.report(task, status, logs, progress, frames_done, warning) is None:
            log(f"Task result not delivered, retrying in {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, 30)

    def execute_task(self, task):
        log(f"Executing task: {task['command']}")

        output = TaskOutput()
        job = JobObject()
        try:
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"

            if task.get("type"):
                command, cwd = houdini_command(task, self.installs)
                # The exact command goes in the task's log, so a problem can be reproduced by hand
                output.add(f"[worker] {subprocess.list2cmdline(command)}\n")
            else:
                command, cwd = resolve_command(task["command"], task["cwd"]), task["cwd"]
            process = start_in_job(job, command, cwd, env)
        except Exception as e:
            job.release()
            log(f"Could not start task: {e}")
            self.report_final(task, "failed", logs=f"[worker] {e}\n")
            return

        watch = OutputWatch(task.get("outputs") or {})
        finished = threading.Event()
        stopped = threading.Event()

        def reporter():
            # Runs on a timer rather than per log line, so the manager hears
            # from us (and can tell us to stop) even while the render is silent
            while not finished.wait(REPORT_INTERVAL):
                text, progress = output.take()
                reply = self.report(task, "running", text, progress, watch.check())
                if reply is None:
                    output.put_back(text)
                elif reply.get("status") == "stop":
                    stopped.set()
                    job.kill()
                    return

        reporter_thread = threading.Thread(target=reporter, daemon=True)
        reporter_thread.start()

        try:
            for line in process.stdout:
                print(line, end="")
                output.add(line)
            returncode = process.wait()
        except BaseException:
            # Never leave a render running that nobody is watching
            job.kill()
            raise
        finally:
            finished.set()
            reporter_thread.join()
            job.release()

        text, progress = output.take()
        if stopped.is_set():
            log("Task cancelled.")
            self.report_final(task, "cancelled", text, progress)
        elif returncode != 0:
            log(f"Task failed with code {returncode}.")
            self.report_final(task, "failed", text + f"\n[worker] exited with code {returncode}\n", progress, watch.check())
        else:
            watch.check(render_ended=True)
            missing = watch.missing()
            warning = None
            if missing:
                # Renderers do not always exit with an error when they produce nothing.
                # Only a warning, not a failure: the file names come from the scene
                # and a renderer may name its files differently than expected.
                warning = f"{len(missing)} of {len(watch.paths)} frames not found on disk"
                text += f"\n[worker] warning: {warning}, e.g. {watch.paths[missing[0]]}\n"
            log("Task completed successfully." if not warning else f"Task completed, but {warning}.")
            self.report_final(task, "completed", text, 100, watch.done, warning)

    def run(self):
        while True:
            task = self.poll_for_task()
            if task:
                try:
                    self.execute_task(task)
                except Exception as e:
                    log(f"Exception during execution: {e}")
                    self.report_final(task, "failed", logs=f"[worker] {e}\n")
            else:
                # Sleep a bit before polling again
                time.sleep(POLL_INTERVAL)

def already_running():
    """Two workers on one machine would share a name and keep stealing each other's task."""
    ctypes.set_last_error(0)
    kernel32.CreateMutexW(None, False, "Global\\DeadliteWorker")
    return ctypes.get_last_error() == ERROR_ALREADY_EXISTS

def main():
    # Render output can contain any character, printing it must never crash the worker
    sys.stdout.reconfigure(errors='replace')

    if already_running():
        print("A Deadlite Worker is already running on this machine.")
        time.sleep(5)
        return

    config, config_path = load_config()
    worker = Worker(config.get("MANAGER_URL", "http://127.0.0.1:5000"), socket.gethostname(), config.get("HOUDINI_DIRS", []))

    print(f"Starting Deadlite Worker [{worker.worker_id}]")
    print(f"Using config file at: {config_path}")
    print(f"Manager URL: {worker.manager_url}")
    print(f"Houdini found: {', '.join(sorted(find_houdini(worker.houdini_dirs))) or 'none'}")
    print("Waiting for tasks...")

    worker.run()

if __name__ == "__main__":
    main()
