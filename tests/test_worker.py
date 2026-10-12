"""Tests for the worker, including real process trees run against a live manager.

Run from the repository root:
    venv\\Scripts\\python -m unittest discover tests -b
"""
import ctypes
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import requests
from werkzeug.serving import make_server

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, os.path.join(ROOT, 'manager'))
sys.path.insert(0, os.path.join(ROOT, 'worker'))
import app as manager
import client

# Stands in for a render: a process that starts another process, like hython starting husk.
# Both print their pid so the tests can check what happened to each of them.
RENDER_TREE = '''
import os, subprocess, sys, time
print("PARENT", os.getpid(), flush=True)
subprocess.Popen([sys.executable, "-c",
    "import os, time; print('CHILD', os.getpid(), flush=True); time.sleep(120)"])
time.sleep(120)
'''

# usage: quick.py [exit code] [seconds between progress lines]
QUICK_RENDER = '''
import sys, time
for percent in (0, 50, 100):
    print(f"ALF_PROGRESS {percent}%", flush=True)
    time.sleep(float(sys.argv[2]) if len(sys.argv) > 2 else 0)
sys.stdout.buffer.write(bytes([0x41, 0xC3, 0x81, 0x81, 0xFF, 0x0A]))  # not valid UTF-8, not valid cp1252
sys.stdout.flush()
print("render done", flush=True)
sys.exit(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
'''

# Stands in for the worker itself: holds a job with a render tree in it, then waits to be killed.
# usage: holder.py <worker dir> <render script>
JOB_HOLDER = '''
import os, sys, time
sys.path.insert(0, sys.argv[1])
import client
job = client.JobObject()
render = client.start_in_job(job, [sys.executable, sys.argv[2]])
print("WORKER", os.getpid(), flush=True)
print(render.stdout.readline(), end="", flush=True)
print(render.stdout.readline(), end="", flush=True)
time.sleep(120)
'''

# Stands in for husk.exe inside a fake Houdini install. Understands the frame
# arguments the worker passes and writes one file per frame.
#   --fake-out <prefix>        write <prefix>.0001.exr, <prefix>.0002.exr, ...
#   --fake-write-nothing       exit normally without writing anything
#   --fake-fail-once <marker>  the first run dies after three frames; later runs are fine
FAKE_HUSK = '''
import os, sys, time
args = sys.argv[1:]
def value(flag):
    return args[args.index(flag) + 1] if flag in args else None
if "--frame-list" in args:
    # like the real husk: every argument after the option is a frame, a quoted list is an error
    frames = [int(f) for f in args[args.index("--frame-list") + 1:]]
else:
    frames = [int(value("-f")) + i * int(value("-i")) for i in range(int(value("-n")))]
marker = value("--fake-fail-once")
dying = bool(marker) and not os.path.exists(marker)
if dying:
    open(marker, "w").close()
print("FAKE HUSK frames", frames, "scene", value("--usd-input"), flush=True)
for n, frame in enumerate(frames):
    if dying and n == 3:
        sys.exit(7)
    if value("--fake-out") and "--fake-write-nothing" not in args:
        with open("%s.%04d.exr" % (value("--fake-out"), frame), "w") as f:
            f.write("pixels")
    print("ALF_PROGRESS %d%%" % (100 * (n + 1) // len(frames)), flush=True)
    time.sleep(0.5)
'''

FAKE_HYTHON = '''
import sys
print("FAKE HYTHON", sys.argv[1:], flush=True)
'''

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")  # as the worker's main() does


def pid_alive(pid):
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True).stdout
    return f'"{pid}"' in out


def throttling_setting(pid):
    """(ControlMask, StateMask) of a process's power throttling: (1, 0) is "never throttle", (0, 0) leaves it to Windows."""
    handle = client.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    state = client.PROCESS_POWER_THROTTLING_STATE(1, 0, 0)
    client.kernel32.GetProcessInformation(ctypes.c_void_p(handle), client.PROCESS_POWER_THROTTLING,
                                          ctypes.byref(state), ctypes.sizeof(state))
    client.kernel32.CloseHandle(handle)
    return state.ControlMask, state.StateMask


def wait_for(condition, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = condition()
        if result:
            return result
        time.sleep(0.1)
    return condition()


def pids_from(log):
    pids = {}
    for line in log.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in ("PARENT", "CHILD"):
            pids[parts[0]] = int(parts[1])
    return pids if len(pids) == 2 else None


class ScriptsMixin:
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.scripts = {}
        for name, source in (("tree", RENDER_TREE), ("quick", QUICK_RENDER), ("holder", JOB_HOLDER),
                             ("husk", FAKE_HUSK), ("hython", FAKE_HYTHON)):
            path = os.path.join(cls.tmp.name, f"{name}.py")
            with open(path, "w") as f:
                f.write(source)
            cls.scripts[name] = path

        # A Houdini install as the worker expects to find it, with stand-in programs
        cls.houdini_root = os.path.join(cls.tmp.name, "installs")
        cls.houdini_bin = os.path.join(cls.houdini_root, "Houdini 99.0.1", "bin")
        os.makedirs(cls.houdini_bin)
        os.makedirs(os.path.join(cls.houdini_root, "Houdini Server"))
        os.makedirs(os.path.join(cls.houdini_root, "Houdini 98.5.2", "not_bin"))
        for program in ("husk", "hython"):
            with open(os.path.join(cls.houdini_bin, f"{program}.cmd"), "w") as f:
                f.write(f'@"{sys.executable}" "{cls.scripts[program]}" %*\n')

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def command(self, script, *args):
        return " ".join([f'"{sys.executable}"', f'"{self.scripts[script]}"', *args])


class ResolveCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = os.path.join(self.tmp.name, "Houdini 21.0.700", "bin")
        os.makedirs(self.bin)
        self.husk = os.path.join(self.bin, "husk.cmd")
        with open(self.husk, "w") as f:
            f.write("@echo off\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_program_is_found_in_the_folder_the_bat_file_cd_into(self):
        resolved = client.resolve_command('husk -Va -f 1 -n 10 "R:/shot/my shot.usd"', self.bin.replace("\\", "/"))

        program, arguments = resolved[1:].split('"', 1)
        self.assertEqual(os.path.normcase(program), os.path.normcase(self.husk))
        self.assertEqual(arguments, ' -Va -f 1 -n 10 "R:/shot/my shot.usd"')

    def test_quoted_program_path_with_spaces(self):
        resolved = client.resolve_command(f'"{self.husk}" -f 1', None)
        self.assertEqual(resolved, f'"{self.husk}" -f 1')

    def test_missing_program_names_the_machine(self):
        with self.assertRaises(FileNotFoundError) as raised:
            client.resolve_command("hython hrender.py", self.bin)
        self.assertIn("hython", str(raised.exception))
        self.assertIn(client.socket.gethostname(), str(raised.exception))

    def test_missing_folder_is_reported_as_such(self):
        with self.assertRaises(FileNotFoundError) as raised:
            client.resolve_command("husk -f 1", "C:/PROGRA~1/SIDEEF~1/HOUDIN~1.999/bin")
        self.assertIn("HOUDIN~1.999", str(raised.exception))


class JobObjectTests(ScriptsMixin, unittest.TestCase):
    def start_tree(self):
        job = client.JobObject()
        process = client.start_in_job(job, [sys.executable, self.scripts["tree"]])
        self.addCleanup(process.stdout.close)
        log = process.stdout.readline() + process.stdout.readline()
        pids = pids_from(log)
        self.assertIsNotNone(pids, f"unexpected output: {log!r}")
        self.addCleanup(self.kill_pids, pids.values())
        return job, pids

    def kill_pids(self, pids):
        for pid in pids:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)

    def assert_tree_dead(self, pids):
        self.assertTrue(wait_for(lambda: not pid_alive(pids["PARENT"]) and not pid_alive(pids["CHILD"])),
                        "render processes survived")

    def test_kill_takes_down_the_whole_tree(self):
        job, pids = self.start_tree()
        self.assertTrue(pid_alive(pids["PARENT"]) and pid_alive(pids["CHILD"]))

        job.kill()
        job.release()

        self.assert_tree_dead(pids)

    def test_every_process_in_the_tree_is_opted_out_of_background_throttling(self):
        # Windows runs windowless processes on the slow cores of a hybrid CPU
        # unless told otherwise, and the setting is not passed on to children.
        job, pids = self.start_tree()
        self.assertEqual(throttling_setting(pids["CHILD"]), (0, 0), "children do not inherit it")

        job.keep_at_full_speed()  # the worker repeats this while a render runs

        self.assertEqual(throttling_setting(pids["PARENT"]), (1, 0))
        self.assertEqual(throttling_setting(pids["CHILD"]), (1, 0))
        job.kill()
        job.release()

    def test_release_leaves_processes_running(self):
        job, pids = self.start_tree()

        job.release()
        time.sleep(1)

        self.assertTrue(pid_alive(pids["PARENT"]))
        self.assertTrue(pid_alive(pids["CHILD"]))

    def test_tree_dies_when_the_worker_holding_the_job_dies(self):
        holder = subprocess.Popen(
            [sys.executable, self.scripts["holder"], os.path.join(ROOT, "worker"), self.scripts["tree"]],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        lines = [holder.stdout.readline() for _ in range(3)]
        self.assertTrue(lines[0].startswith("WORKER"), f"unexpected output: {lines!r}")
        worker_pid = int(lines[0].split()[1])
        pids = pids_from("".join(lines[1:]))
        self.addCleanup(self.kill_pids, [worker_pid, *pids.values()])
        self.assertTrue(pid_alive(pids["PARENT"]) and pid_alive(pids["CHILD"]))

        # Same as closing the worker's console window or ending it in Task Manager
        subprocess.run(["taskkill", "/F", "/PID", str(worker_pid)], capture_output=True)

        self.assert_tree_dead(pids)


class LiveManagerTestCase(ScriptsMixin, unittest.TestCase):
    """A real manager on a local port and a worker pointed at it."""

    def setUp(self):
        # An empty queue in its own folder for every test
        manager.use_data_dir(tempfile.mkdtemp(dir=self.tmp.name))

        self.server = make_server("127.0.0.1", 0, manager.app, threaded=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

        self.worker = client.Worker(self.url, "TEST-PC", houdini_dirs=[self.houdini_root])
        patcher = mock.patch.object(client, "REPORT_INTERVAL", 0.2)
        patcher.start()
        self.addCleanup(patcher.stop)

    def submit(self, command, cwd=None):
        reply = requests.post(f"{self.url}/api/jobs", json={
            "name": "test", "tasks": [{"command": command, "cwd": cwd or self.tmp.name}]}).json()
        self.job_id = reply["job_id"]
        return self.first_task_id()

    def first_task_id(self):
        with manager.LOCK:
            return next(t["id"] for t in manager.STATE["tasks"].values() if t["job_id"] == self.job_id)

    def task(self, task_id):
        """The task as the dashboard sees it, with its whole log added as task["logs"]."""
        task = requests.get(f"{self.url}/api/state").json()["tasks"].get(task_id)
        if task is not None:
            task["logs"] = requests.get(f"{self.url}/api/tasks/{task_id}/log", params={"offset": 0}).json()["text"]
        return task

    def run_worker_once(self):
        """Poll for one task and execute it, on a thread like the real worker's main loop."""
        def work():
            task = wait_for(self.worker.poll_for_task)
            self.worker.execute_task(task)
        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        return thread


class HoudiniCommandTests(ScriptsMixin, unittest.TestCase):
    def setUp(self):
        self.installs = client.find_houdini([self.houdini_root])
        self.scene = os.path.join(self.tmp.name, "job.usd")
        open(self.scene, "w").close()
        self.task = {"type": "husk", "houdini": "99.0.1", "scene": self.scene,
                     "args": ["-R", "BRAY_HdKarmaXPU"], "frames": [11, 12, 13, 14]}

    def test_installs_are_found_by_folder_name_and_bin_folder(self):
        self.assertEqual(self.installs["99.0.1"], self.houdini_bin)
        self.assertNotIn("98.5.2", self.installs)  # no bin folder: half-removed install
        self.assertTrue(all(v[0].isdigit() for v in self.installs))

    def test_husk_command_for_a_run_of_frames(self):
        command, cwd = client.houdini_command(self.task, self.installs)

        self.assertEqual(command[0], os.path.join(self.houdini_bin, "husk.cmd"))
        self.assertEqual(command[1:], ["-R", "BRAY_HdKarmaXPU", "-Va", "--make-output-path",
                                       "--usd-input", self.scene, "-f", "11", "-n", "4", "-i", "1"])
        self.assertEqual(cwd, self.houdini_bin)

    def test_husk_options_already_set_by_the_render_node_are_not_given_twice(self):
        # husk refuses to start when an option appears twice. The node's own
        # verbosity (and its Alfred progress switch) is kept as it came.
        self.task["args"] = ["-s", "/Render/alt", "--make-output-path", "-R", "BRAY_HdKarma", "-V", "3a"]
        command, _ = client.houdini_command(self.task, self.installs)

        self.assertEqual(command[1:], ["-s", "/Render/alt", "--make-output-path", "-R", "BRAY_HdKarma", "-V", "3a",
                                       "--usd-input", self.scene, "-f", "11", "-n", "4", "-i", "1"])

    def test_husk_command_keeps_the_frame_step(self):
        self.task["frames"] = [1, 3, 5]
        command, _ = client.houdini_command(self.task, self.installs)
        self.assertEqual(command[-6:], ["-f", "1", "-n", "3", "-i", "2"])

    def test_husk_command_for_frames_with_gaps_lists_each_frame_as_its_own_argument(self):
        # Checked against husk 21.0.700: a quoted "4 9 10" is rejected, and
        # everything after --frame-list is read as a frame, so nothing may follow it
        self.task["frames"] = [4, 9, 10]
        command, _ = client.houdini_command(self.task, self.installs)
        self.assertEqual(command[-6:], ["--usd-input", self.scene, "--frame-list", "4", "9", "10"])

    def test_hython_command_runs_the_runner_on_the_snapshot(self):
        self.task.update(type="hython", node="/obj/box/cache", hip_name="R:/shot/shot_v01.hiplc")
        command, _ = client.houdini_command(self.task, self.installs)

        self.assertEqual(command, [os.path.join(self.houdini_bin, "hython.cmd"), client.RUNNER,
                                   "--hip", self.scene, "--node", "/obj/box/cache",
                                   "--name", "R:/shot/shot_v01.hiplc", "--frames", "11", "12", "13", "14"])
        self.assertTrue(os.path.isfile(client.RUNNER))

    def test_missing_houdini_version_says_what_this_machine_has(self):
        self.task["houdini"] = "20.5.684"
        with self.assertRaises(FileNotFoundError) as raised:
            client.houdini_command(self.task, self.installs)
        self.assertIn("Houdini 20.5.684 is not installed on", str(raised.exception))
        self.assertIn("99.0.1", str(raised.exception))

    def test_unreachable_scene_is_reported_before_anything_is_started(self):
        self.task["scene"] = "Q:/not/mapped/here/job.usd"
        with self.assertRaises(FileNotFoundError) as raised:
            client.houdini_command(self.task, self.installs)
        self.assertIn("cannot be reached from", str(raised.exception))


class OutputWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = {str(f): os.path.join(self.tmp.name, f"shot.{f:04d}.exr") for f in (1, 2, 3)}

    def write(self, frame, text):
        with open(self.paths[str(frame)], "w") as f:
            f.write(text)

    def test_frame_counts_once_its_file_has_stopped_changing(self):
        watch = client.OutputWatch(self.paths)
        self.assertEqual(watch.check(), [])

        self.write(1, "half")
        self.assertEqual(watch.check(), [], "first sighting: the renderer may still be writing")
        self.write(1, "half and the rest")
        self.assertEqual(watch.check(), [], "it changed again")
        self.assertEqual(watch.check(), [1])
        self.assertEqual(watch.missing(), [2, 3])

    def test_file_left_over_from_an_earlier_render_does_not_count(self):
        self.write(2, "last week's render")
        watch = client.OutputWatch(self.paths)

        watch.check()
        self.assertEqual(watch.check(render_ended=True), [])

    def test_everything_new_counts_once_the_render_has_ended(self):
        watch = client.OutputWatch(self.paths)
        self.write(3, "written just before exit")
        self.assertEqual(watch.check(render_ended=True), [3])


class HoudiniJobEndToEndTests(LiveManagerTestCase):
    def setUp(self):
        super().setUp()
        self.out = tempfile.mkdtemp(dir=self.tmp.name)
        self.scene = os.path.join(self.out, "job.usd")
        open(self.scene, "w").close()

    def output(self, frame):
        return os.path.join(self.out, f"shot.{frame:04d}.exr")

    def submit_job(self, frames, *extra_args, **changes):
        job = {
            "name": "shot / render", "type": "husk", "houdini": "99.0.1", "scene": self.scene,
            "args": ["--fake-out", os.path.join(self.out, "shot"), *extra_args],
            "frames": [frames[0], frames[-1], 1], "chunk": 0,
            "outputs": {str(f): self.output(f) for f in frames},
        }
        job.update(changes)
        self.job_id = requests.post(f"{self.url}/api/jobs", json=job).json()["job_id"]
        return self.first_task_id()

    def test_job_from_houdini_renders_with_the_matching_install(self):
        task_id = self.submit_job([1, 2, 3, 4])
        self.run_worker_once().join(30)

        task = self.task(task_id)
        self.assertEqual(task["status"], "completed", task["logs"])
        self.assertEqual(sorted(task["done_frames"]), [1, 2, 3, 4])
        self.assertTrue(all(os.path.isfile(self.output(f)) for f in (1, 2, 3, 4)))
        self.assertIn("FAKE HUSK frames [1, 2, 3, 4]", task["logs"])
        self.assertIn("[worker] ", task["logs"])
        self.assertIn("husk.cmd", task["logs"])

    def test_render_that_exits_normally_but_writes_nothing_completes_with_a_warning(self):
        task_id = self.submit_job([1, 2], "--fake-write-nothing")
        self.run_worker_once().join(30)

        task = self.task(task_id)
        # A warning rather than a failure: expected file names come from the
        # scene, and failing a render that did its job would be worse
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["warning"], "2 of 2 frames not found on disk")
        self.assertEqual(task["done_frames"], [])
        self.assertIn("shot.0001.exr", task["logs"])

    def test_warning_is_cleared_when_the_task_runs_again(self):
        task_id = self.submit_job([1, 2], "--fake-write-nothing")
        self.run_worker_once().join(30)
        self.assertIsNotNone(self.task(task_id)["warning"])

        requests.post(f"{self.url}/api/tasks/{task_id}/start")
        wait_for(self.worker.poll_for_task)

        self.assertIsNone(self.task(task_id)["warning"])

    def test_restarted_task_renders_only_the_frames_that_were_not_written(self):
        marker = os.path.join(self.out, "died_once")
        task_id = self.submit_job([1, 2, 3, 4, 5, 6], "--fake-fail-once", marker)
        self.run_worker_once().join(30)

        task = self.task(task_id)
        self.assertEqual(task["status"], "failed")
        self.assertIn("exited with code 7", task["logs"])
        self.assertEqual(sorted(task["done_frames"]), [1, 2, 3])
        written_first_time = {f: os.stat(self.output(f)).st_mtime_ns for f in (1, 2, 3)}

        requests.post(f"{self.url}/api/tasks/{task_id}/start")
        self.run_worker_once().join(30)

        task = self.task(task_id)
        self.assertEqual(task["status"], "completed", task["logs"])
        self.assertIn("FAKE HUSK frames [4, 5, 6]", task["logs"])
        self.assertEqual(sorted(task["done_frames"]), [1, 2, 3, 4, 5, 6])
        self.assertEqual({f: os.stat(self.output(f)).st_mtime_ns for f in (1, 2, 3)}, written_first_time)

    def test_hython_job_runs_the_runner_with_the_node_and_original_scene_name(self):
        task_id = self.submit_job([1, 2], type="hython", node="/obj/box/cache",
                                  hip_name="R:/shot/shot_v01.hiplc", outputs={})
        self.run_worker_once().join(30)

        task = self.task(task_id)
        self.assertEqual(task["status"], "completed", task["logs"])
        self.assertIn("FAKE HYTHON", task["logs"])
        self.assertIn("hython_runner.py", task["logs"])
        for expected in ("'--node', '/obj/box/cache'", "'--name', 'R:/shot/shot_v01.hiplc'", "'--frames', '1', '2'"):
            self.assertIn(expected, task["logs"])

    def test_scene_that_cannot_be_reached_fails_with_a_readable_reason(self):
        task_id = self.submit_job([1, 2], scene="Q:/not/mapped/here/job.usd")
        self.run_worker_once().join(30)

        task = self.task(task_id)
        self.assertEqual(task["status"], "failed")
        self.assertIn("Scene file cannot be reached from", task["logs"])

    def test_worker_without_that_houdini_version_is_not_given_the_job(self):
        self.submit_job([1, 2], houdini="20.5.684")

        self.assertIsNone(self.worker.poll_for_task())
        job = requests.get(f"{self.url}/api/state").json()["jobs"][self.job_id]
        self.assertEqual(job["waiting_for"], "a worker with Houdini 20.5.684")


class WorkerEndToEndTests(LiveManagerTestCase):

    def start_tree_task(self):
        task_id = self.submit(self.command("tree"))
        thread = self.run_worker_once()
        pids = wait_for(lambda: pids_from(self.task(task_id)["logs"]))
        self.assertIsNotNone(pids, "render tree did not start")
        return task_id, thread, pids

    def assert_tree_dead(self, pids):
        self.assertTrue(wait_for(lambda: not pid_alive(pids["PARENT"]) and not pid_alive(pids["CHILD"])),
                        "render processes survived")

    # --- tests ---

    def test_completed_render_reports_progress_and_output(self):
        task_id = self.submit(self.command("quick"))
        self.run_worker_once().join(20)

        task = self.task(task_id)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["progress"], 100)
        self.assertIn("ALF_PROGRESS 50%", task["logs"])
        self.assertIn("render done", task["logs"])
        self.assertEqual(requests.get(f"{self.url}/api/state").json()["workers"]["TEST-PC"]["status"], "idle")

    def test_non_zero_exit_code_fails_the_task(self):
        task_id = self.submit(self.command("quick", "3"))
        self.run_worker_once().join(20)

        task = self.task(task_id)
        self.assertEqual(task["status"], "failed")
        self.assertIn("exited with code 3", task["logs"])

    def test_program_that_does_not_exist_fails_with_a_readable_reason(self):
        task_id = self.submit('husk -f 1 -n 10 "shot.usd"')
        self.run_worker_once().join(20)

        task = self.task(task_id)
        self.assertEqual(task["status"], "failed")
        self.assertIn("'husk' not found on", task["logs"])

    def test_cancel_kills_the_whole_render_tree(self):
        task_id, thread, pids = self.start_tree_task()

        requests.post(f"{self.url}/api/tasks/{task_id}/cancel")

        thread.join(15)
        self.assertFalse(thread.is_alive(), "worker is still stuck on the cancelled task")
        self.assert_tree_dead(pids)
        self.assertEqual(self.task(task_id)["status"], "cancelled")
        # and the worker is free for the next task straight away
        self.assertIsNone(self.worker.poll_for_task())
        self.assertEqual(requests.get(f"{self.url}/api/state").json()["workers"]["TEST-PC"]["status"], "idle")

    def test_render_started_by_the_worker_runs_at_full_speed_including_what_it_starts(self):
        task_id, thread, pids = self.start_tree_task()

        # the child (think: husk started by hython) is picked up on the worker's next report
        self.assertTrue(wait_for(lambda: throttling_setting(pids["CHILD"]) == (1, 0), timeout=5))
        self.assertEqual(throttling_setting(pids["PARENT"]), (1, 0))

        requests.post(f"{self.url}/api/tasks/{task_id}/cancel")
        thread.join(15)

    def test_deleting_the_job_kills_the_render_tree(self):
        task_id, thread, pids = self.start_tree_task()

        requests.delete(f"{self.url}/api/jobs/{self.job_id}/delete")

        thread.join(15)
        self.assertFalse(thread.is_alive(), "worker is still rendering a deleted job")
        self.assert_tree_dead(pids)

    def test_task_given_to_another_worker_is_killed_here(self):
        task_id, thread, pids = self.start_tree_task()

        # The manager gives up on this worker and hands the task to someone else
        with manager.LOCK:
            manager.release_task(manager.STATE["tasks"][task_id], "test")
        other = requests.post(f"{self.url}/api/worker/poll",
                              json={"worker_id": "OTHER-PC", "protocol": client.PROTOCOL}).json()
        self.assertEqual(other["task"]["id"], task_id)

        thread.join(15)
        self.assertFalse(thread.is_alive())
        self.assert_tree_dead(pids)
        task = self.task(task_id)
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["worker_id"], "OTHER-PC")

    def test_manager_hostname_is_only_looked_up_as_ipv4(self):
        # MANAGER_URL is often a hostname, which usually resolves to IPv6 addresses too.
        # The manager listens on IPv4 only, and each IPv6 attempt costs a full timeout
        # before the connection falls back (measured: 5s on every request).
        families = []
        real_getaddrinfo = socket.getaddrinfo

        def spy(host, port, family=0, *args, **kwargs):
            families.append(family)
            return real_getaddrinfo(host, port, family, *args, **kwargs)

        worker = client.Worker(f"http://localhost:{self.server.server_port}", "TEST-PC")
        with mock.patch.object(socket, "getaddrinfo", spy):
            reply = worker.post("/api/worker/poll", {"worker_id": "TEST-PC", "protocol": client.PROTOCOL})

        self.assertEqual(reply["status"], "wait")
        self.assertEqual(families, [socket.AF_INET])

    def test_final_report_is_retried_until_the_manager_hears_it(self):
        task_id = self.submit(self.command("quick"))
        real_post = self.worker.post
        dropped = []

        def flaky_post(path, payload):
            if payload.get("status") == "completed" and len(dropped) < 3:
                dropped.append(payload)
                return None
            return real_post(path, payload)

        with mock.patch.object(self.worker, "post", flaky_post), mock.patch.object(client.time, "sleep"):
            self.run_worker_once().join(20)

        self.assertEqual(len(dropped), 3)
        self.assertEqual(self.task(task_id)["status"], "completed")

    def test_render_keeps_going_while_the_manager_is_unreachable(self):
        task_id = self.submit(self.command("quick", "0", "0.4"))
        real_post = self.worker.post
        lost = []

        def post_with_outage(path, payload):
            # Every report made while rendering is lost; only the final one gets through
            if payload.get("status") == "running":
                lost.append(payload)
                return None
            return real_post(path, payload)

        with mock.patch.object(self.worker, "post", post_with_outage):
            self.run_worker_once().join(20)

        self.assertGreater(len(lost), 1)
        task = self.task(task_id)
        self.assertEqual(task["status"], "completed")
        # Nothing was dropped or reordered along the way
        logs = task["logs"]
        positions = [logs.index(text) for text in ("ALF_PROGRESS 0%", "ALF_PROGRESS 50%", "ALF_PROGRESS 100%", "render done")]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(logs.count("ALF_PROGRESS 50%"), 1)


if __name__ == "__main__":
    unittest.main()
