import os
import sys
import time
import json
import socket
import requests
import subprocess
import threading
import re

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

MANAGER_URL = config.get("MANAGER_URL", "http://127.0.0.1:5000")
WORKER_ID = socket.gethostname()

def poll_for_task():
    try:
        response = requests.post(f"{MANAGER_URL}/api/worker/poll", json={"worker_id": WORKER_ID}, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        if data.get("status") == "task":
            return data.get("task")
    except requests.exceptions.RequestException as e:
        print(f"[{time.strftime('%H:%M:%S')}] Connection to manager failed: {e}")
    return None

def update_task_status(task_id, status, logs="", progress=None):
    payload = {
        "worker_id": WORKER_ID,
        "task_id": task_id,
        "status": status,
        "logs": logs
    }
    if progress is not None:
        payload["progress"] = progress
        
    try:
        requests.post(f"{MANAGER_URL}/api/worker/update", json=payload, timeout=5)
    except requests.exceptions.RequestException as e:
        print(f"Failed to update task status: {e}")

def execute_task(task):
    task_id = task["id"]
    command = task["command"]
    cwd = task["cwd"]
    
    print(f"[{time.strftime('%H:%M:%S')}] Executing task: {command}")
    
    # Update manager that we are running
    update_task_status(task_id, "running")
    
    try:
        # We need to run the command in a subprocess. 
        # Since the .bat file can contain Windows-specific commands, we use shell=True
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        
        cancel_event = threading.Event()
        
        def check_cancel():
            while process.poll() is None:
                try:
                    res = requests.get(f"{MANAGER_URL}/api/tasks/{task_id}/status", timeout=2)
                    if res.status_code == 200 and res.json().get("status") == "cancelled":
                        process.kill()
                        cancel_event.set()
                        break
                except:
                    pass
                time.sleep(3)
                
        t = threading.Thread(target=check_cancel, daemon=True)
        t.start()
        
        full_logs = ""
        chunk = ""
        last_send_time = time.time()
        current_progress = 0
        alf_regex = re.compile(r'ALF_PROGRESS\s+([0-9]+)%')
        
        # Stream logs (we could stream to manager, but for simplicity we collect and send at end)
        # To make UI responsive, we could send logs every N seconds, but let's just send at the end for now
        # or periodically send small chunks.
        for line in process.stdout:
            print(line, end="")
            full_logs += line
            chunk += line
            
            # Parse ALF_PROGRESS
            match = alf_regex.search(line)
            if match:
                current_progress = int(match.group(1))
            
            if time.time() - last_send_time > 2.0 and chunk:
                update_task_status(task_id, "running", logs=chunk, progress=current_progress)
                chunk = ""
                last_send_time = time.time()
                
        process.wait()
        
        if cancel_event.is_set():
            print(f"[{time.strftime('%H:%M:%S')}] Task cancelled.")
            update_task_status(task_id, "cancelled", logs=chunk, progress=current_progress)
        elif process.returncode == 0:
            print(f"[{time.strftime('%H:%M:%S')}] Task completed successfully.")
            update_task_status(task_id, "completed", logs=chunk, progress=100)
        else:
            print(f"[{time.strftime('%H:%M:%S')}] Task failed with code {process.returncode}.")
            update_task_status(task_id, "failed", logs=chunk, progress=current_progress)
            
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] Exception during execution: {e}")
        update_task_status(task_id, "failed", logs=str(e))

def main():
    print(f"Starting Deadlite Worker [{WORKER_ID}]")
    print(f"Using config file at: {config_path}")
    print(f"Manager URL: {MANAGER_URL}")
    print("Waiting for tasks...")
    
    while True:
        task = poll_for_task()
        if task:
            execute_task(task)
        else:
            # Sleep a bit before polling again
            time.sleep(3)

if __name__ == "__main__":
    main()
