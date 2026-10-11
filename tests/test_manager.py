"""Tests for the manager's task state machine.

Run from the repository root:
    venv\\Scripts\\python -m unittest discover tests -b
"""
import io
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'manager'))
import app as manager

BAT = (
    'cd /d "C:/PROGRA~1/SIDEEF~1/HOUDIN~1.700/bin"\n\n'
    'husk -Va --make-output-path -f 1 -n 10 "R:/shot/shot.usd"\n'
    'husk -Va --make-output-path -f 11 -n 10 "R:/shot/shot.usd"\n'
    'husk -Va --make-output-path -f 21 -n 10 "R:/shot/shot.usd"\n'
    '\n\npause\n'
)


def reset_state():
    for key in ("jobs", "tasks", "workers"):
        manager.STATE[key].clear()
    manager.STATE["job_order"].clear()
    manager.OUTPUTS.clear()


class ManagerTestCase(unittest.TestCase):
    def setUp(self):
        reset_state()
        self.client = manager.app.test_client()

    # --- helpers ---

    def upload(self, data=None, name="shot.bat"):
        data = BAT.encode("utf-8") if data is None else data
        response = self.client.post("/api/jobs/upload", data={"file": (io.BytesIO(data), name)},
                                    content_type="multipart/form-data")
        return response

    def new_job(self, start=True):
        job_id = self.upload().get_json()["job_id"]
        if start:
            self.client.post(f"/api/jobs/{job_id}/start")
        return job_id

    def poll(self, worker, protocol=manager.PROTOCOL, houdini=("21.0.700",)):
        return self.client.post("/api/worker/poll", json={
            "worker_id": worker, "protocol": protocol, "houdini": list(houdini)}).get_json()

    def take_task(self, worker, **kwargs):
        reply = self.poll(worker, **kwargs)
        self.assertEqual(reply["status"], "task", f"{worker} was not given a task")
        return reply["task"]

    def report(self, worker, task, status="running", **fields):
        payload = {"worker_id": worker, "task_id": task["id"], "attempt_id": task["attempt_id"], "status": status}
        payload.update(fields)
        return self.client.post("/api/worker/update", json=payload).get_json()

    def state(self):
        return self.client.get("/api/state").get_json()

    def task(self, task_id):
        return self.state()["tasks"][task_id]

    def tasks(self, job_id):
        tasks = [t for t in self.state()["tasks"].values() if t["job_id"] == job_id]
        return sorted(tasks, key=lambda t: t["index"])

    def job_status(self, job_id):
        return self.state()["jobs"][job_id]["status"]


class UploadTests(ManagerTestCase):
    def test_upload_waits_for_start_and_keeps_file_order(self):
        job_id = self.new_job(start=False)
        tasks = self.tasks(job_id)

        self.assertEqual([t["status"] for t in tasks], ["paused"] * 3)
        self.assertEqual(self.job_status(job_id), "paused")
        self.assertIn("-f 1 -n 10", tasks[0]["command"])
        self.assertIn("-f 21 -n 10", tasks[2]["command"])
        self.assertEqual(tasks[0]["cwd"], "C:/PROGRA~1/SIDEEF~1/HOUDIN~1.700/bin")
        self.assertEqual(self.poll("PC-A")["status"], "wait")

    def test_upload_accepts_bat_written_in_windows_code_page(self):
        bat = BAT.replace("R:/shot/shot.usd", "D:/Projects/Jos\u00e9/shot.usd").encode("cp1252")
        response = self.upload(data=bat)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Jos\u00e9", self.tasks(response.get_json()["job_id"])[0]["command"])

    def test_upload_without_render_commands_is_rejected(self):
        self.assertEqual(self.upload(data=b"echo hello\npause\n").status_code, 400)

    def test_empty_json_job_is_rejected(self):
        response = self.client.post("/api/jobs", json={"name": "empty", "tasks": []})
        self.assertEqual(response.status_code, 400)


class DispatchTests(ManagerTestCase):
    def test_tasks_are_handed_out_in_file_order(self):
        job_id = self.new_job()
        first = self.take_task("PC-A")
        second = self.take_task("PC-B")

        tasks = self.tasks(job_id)
        self.assertEqual(first["id"], tasks[0]["id"])
        self.assertEqual(second["id"], tasks[1]["id"])

    def test_higher_job_in_queue_is_served_first(self):
        job_a = self.new_job()
        job_b = self.new_job()
        self.client.post(f"/api/jobs/{job_b}/move", json={"direction": "up"})

        task = self.take_task("PC-A")
        self.assertEqual(self.task(task["id"])["job_id"], job_b)
        self.assertEqual(self.job_status(job_a), "queued")

    def test_start_on_one_task_of_a_paused_job_renders_just_that_task(self):
        job_id = self.new_job(start=False)
        test_task = self.tasks(job_id)[1]
        self.client.post(f"/api/tasks/{test_task['id']}/start")

        given = self.take_task("PC-A")
        self.assertEqual(given["id"], test_task["id"])
        self.assertEqual(self.poll("PC-B")["status"], "wait")
        self.assertEqual(self.job_status(job_id), "running")

        self.report("PC-A", given, "completed")
        self.assertEqual(self.job_status(job_id), "paused")

    def test_no_task_is_handed_out_twice_under_concurrent_polls(self):
        tasks = [{"command": f"husk -f {i} -n 1 shot.usd", "cwd": None} for i in range(60)]
        self.client.post("/api/jobs", json={"name": "many", "tasks": tasks})

        given = []

        def poll_until_empty(thread_no):
            client = manager.app.test_client()
            for n in range(60):
                reply = client.post("/api/worker/poll",
                                    json={"worker_id": f"PC-{thread_no}-{n}", "protocol": manager.PROTOCOL}).get_json()
                if reply["status"] != "task":
                    return
                given.append(reply["task"]["id"])

        threads = [threading.Thread(target=poll_until_empty, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(given), 60)
        self.assertEqual(len(set(given)), 60)

    def test_worker_on_another_version_gets_no_work(self):
        self.new_job()
        reply = self.poll("PC-OLD", protocol=None)

        self.assertEqual(reply["status"], "wait")
        self.assertTrue(self.state()["workers"]["PC-OLD"]["outdated"])


class CancelTests(ManagerTestCase):
    def test_cancelled_task_tells_its_worker_to_stop(self):
        self.new_job()
        task = self.take_task("PC-A")
        self.assertEqual(self.report("PC-A", task)["status"], "ok")

        self.client.post(f"/api/tasks/{task['id']}/cancel")

        self.assertEqual(self.report("PC-A", task)["status"], "stop")
        self.assertEqual(self.task(task["id"])["status"], "cancelled")
        self.assertIsNotNone(self.task(task["id"])["end_time"])

    def test_cancelled_job_stops_running_and_waiting_tasks_but_keeps_completed(self):
        job_id = self.new_job()
        done = self.take_task("PC-A")
        self.report("PC-A", done, "completed")
        running = self.take_task("PC-A")

        self.client.post(f"/api/jobs/{job_id}/cancel")

        self.assertEqual([t["status"] for t in self.tasks(job_id)], ["completed", "cancelled", "cancelled"])
        self.assertEqual(self.job_status(job_id), "cancelled")
        self.assertEqual(self.report("PC-A", running)["status"], "stop")
        self.assertEqual(self.poll("PC-B")["status"], "wait")

    def test_deleted_job_tells_its_worker_to_stop(self):
        job_id = self.new_job()
        task = self.take_task("PC-A")

        self.client.delete(f"/api/jobs/{job_id}/delete")

        self.assertEqual(self.report("PC-A", task)["status"], "stop")
        self.assertNotIn(job_id, self.state()["jobs"])

    def test_late_report_from_cancelled_attempt_cannot_touch_the_restarted_task(self):
        self.new_job()
        first = self.take_task("PC-A")
        self.report("PC-A", first, logs="old attempt\n", progress=60)

        self.client.post(f"/api/tasks/{first['id']}/cancel")
        self.client.post(f"/api/tasks/{first['id']}/start")
        second = self.take_task("PC-B")
        self.assertEqual(second["id"], first["id"])
        self.assertNotEqual(second["attempt_id"], first["attempt_id"])

        # PC-A finally notices and reports in
        self.assertEqual(self.report("PC-A", first, "cancelled", logs="late\n")["status"], "stop")

        task = self.task(first["id"])
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["worker_id"], "PC-B")
        self.assertNotIn("late", task["logs"])
        self.assertEqual(self.report("PC-B", second)["status"], "ok")

    def test_restarted_task_starts_from_a_clean_slate(self):
        self.new_job()
        task = self.take_task("PC-A")
        self.report("PC-A", task, "failed", logs="boom\n", progress=60)

        self.client.post(f"/api/tasks/{task['id']}/start")

        restarted = self.task(task["id"])
        self.assertEqual(restarted["status"], "queued")
        self.assertEqual(restarted["progress"], 0)
        self.assertEqual(restarted["logs"], "")
        self.assertIsNone(restarted["worker_id"])
        self.assertIsNone(restarted["start_time"])
        self.assertIsNone(restarted["end_time"])

    def test_pause_holds_waiting_tasks_and_lets_the_running_one_finish(self):
        job_id = self.new_job()
        running = self.take_task("PC-A")

        self.client.post(f"/api/jobs/{job_id}/pause")

        self.assertEqual([t["status"] for t in self.tasks(job_id)], ["running", "paused", "paused"])
        self.assertEqual(self.report("PC-A", running)["status"], "ok")
        self.assertEqual(self.poll("PC-B")["status"], "wait")

        self.report("PC-A", running, "completed")
        self.assertEqual(self.job_status(job_id), "paused")

    def test_start_cannot_requeue_a_task_that_is_rendering(self):
        self.new_job()
        task = self.take_task("PC-A")

        self.client.post(f"/api/tasks/{task['id']}/start")

        self.assertEqual(self.task(task["id"])["status"], "running")
        self.assertEqual(self.report("PC-A", task)["status"], "ok")


class LostWorkerTests(ManagerTestCase):
    def test_worker_that_restarts_mid_task_is_given_work_again(self):
        self.new_job()
        first = self.take_task("PC-A")

        # PC-A was closed and reopened: it polls although we think it is rendering
        again = self.take_task("PC-A")

        self.assertEqual(again["id"], first["id"])
        self.assertNotEqual(again["attempt_id"], first["attempt_id"])
        task = self.task(first["id"])
        self.assertEqual(task["attempt"], 2)
        self.assertIn("restarted mid-task", task["logs"])

    def test_silent_worker_goes_offline_and_its_task_moves_to_another_worker(self):
        self.new_job()
        task = self.take_task("PC-A")
        manager.STATE["workers"]["PC-A"]["last_seen"] = time.time() - manager.WORKER_TIMEOUT - 1

        state = self.state()
        self.assertEqual(state["workers"]["PC-A"]["status"], "offline")
        self.assertEqual(state["tasks"][task["id"]]["status"], "queued")

        self.assertEqual(self.take_task("PC-B")["id"], task["id"])
        # PC-A comes back from its network blip still rendering the old attempt
        self.assertEqual(self.report("PC-A", task)["status"], "stop")

    def test_report_without_new_output_still_counts_as_a_heartbeat(self):
        self.new_job()
        task = self.take_task("PC-A")
        manager.STATE["workers"]["PC-A"]["last_seen"] = time.time() - manager.WORKER_TIMEOUT - 1

        self.report("PC-A", task)  # a long silent frame: nothing to send but "still here"

        self.assertEqual(self.state()["workers"]["PC-A"]["status"], "running")
        self.assertEqual(self.task(task["id"])["status"], "running")

    def test_task_fails_when_it_keeps_losing_its_worker(self):
        self.new_job()
        task = self.take_task("PC-A")
        for _ in range(manager.MAX_ATTEMPTS - 1):
            self.assertEqual(self.take_task("PC-A")["id"], task["id"])

        # Lost once more: this time it is not handed out again
        given = self.take_task("PC-A")

        self.assertNotEqual(given["id"], task["id"])
        failed = self.task(task["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("giving up", failed["logs"])

    def test_report_for_unknown_task_tells_worker_to_stop(self):
        # e.g. the manager was restarted while a worker was rendering
        reply = self.client.post("/api/worker/update", json={
            "worker_id": "PC-A", "task_id": "gone", "attempt_id": "gone", "status": "running"}).get_json()

        self.assertEqual(reply["status"], "stop")
        self.assertIn("PC-A", self.state()["workers"])


class HoudiniJobTests(ManagerTestCase):
    """Jobs described by the Houdini submit tool instead of a .bat file."""

    def submit(self, expect=200, **changes):
        job = {
            "name": "shot_v01 / render",
            "type": "husk",
            "houdini": "21.0.700",
            "scene": "R:/shot/deadlite/shot_v01_render/job.usd",
            "args": ["-R", "BRAY_HdKarmaXPU"],
            "frames": [1, 25, 1],
            "chunk": 10,
            "outputs": {str(f): f"R:/shot/render/shot_v01.{f:04d}.exr" for f in range(1, 26)},
        }
        job.update(changes)
        response = self.client.post("/api/jobs", json=job)
        self.assertEqual(response.status_code, expect, response.get_json())
        return response.get_json()

    def test_frame_range_is_cut_into_tasks_of_chunk_frames(self):
        reply = self.submit()
        tasks = self.tasks(reply["job_id"])

        self.assertEqual(reply["tasks"], 3)
        self.assertEqual([(t["frames"][0], t["frames"][-1]) for t in tasks], [(1, 10), (11, 20), (21, 25)])
        self.assertEqual({t["status"] for t in tasks}, {"queued"})
        job = self.state()["jobs"][reply["job_id"]]
        self.assertEqual((job["type"], job["houdini"]), ("husk", "21.0.700"))
        self.assertEqual(job["output_dir"], "R:/shot/render")

    def test_chunk_of_zero_keeps_a_simulation_in_one_task(self):
        reply = self.submit(type="hython", node="/obj/sim/cache", frames=[1, 240, 1], chunk=0)
        tasks = self.tasks(reply["job_id"])

        self.assertEqual(len(tasks), 1)
        self.assertEqual(len(tasks[0]["frames"]), 240)

    def test_frame_step_is_respected(self):
        tasks = self.tasks(self.submit(frames=[1, 10, 3], chunk=2)["job_id"])
        self.assertEqual([t["frames"] for t in tasks], [[1, 4], [7, 10]])

    def test_job_can_be_submitted_paused(self):
        job_id = self.submit(paused=True)["job_id"]
        self.assertEqual(self.job_status(job_id), "paused")
        self.assertEqual(self.poll("PC-A")["status"], "wait")

    def test_bad_descriptions_are_rejected_with_a_reason(self):
        for changes, word in (({"type": "blender"}, "type"),
                              ({"type": "hython"}, "node"),
                              ({"frames": [10, 1, 1]}, "frame range"),
                              ({"frames": [1, 10, 0]}, "frame range"),
                              ({"frames": "1-10"}, "frames"),
                              ({"scene": ""}, "scene")):
            with self.subTest(changes=changes):
                self.assertIn(word, self.submit(expect=400, **changes)["error"])
        self.assertEqual(self.state()["jobs"], {})

    def test_worker_is_sent_a_description_with_its_own_frames_and_outputs(self):
        self.submit()
        task = self.take_task("PC-A")

        self.assertEqual(task["type"], "husk")
        self.assertEqual(task["houdini"], "21.0.700")
        self.assertEqual(task["scene"], "R:/shot/deadlite/shot_v01_render/job.usd")
        self.assertEqual(task["args"], ["-R", "BRAY_HdKarmaXPU"])
        self.assertEqual(task["frames"], list(range(1, 11)))
        self.assertEqual(sorted(task["outputs"], key=int), [str(f) for f in range(1, 11)])
        self.assertEqual(task["outputs"]["3"], "R:/shot/render/shot_v01.0003.exr")

    def test_dashboard_state_does_not_carry_the_output_file_list(self):
        self.submit()
        self.assertNotIn("shot_v01.0003.exr", self.client.get("/api/state").get_data(as_text=True))

    def test_job_only_goes_to_a_worker_with_that_houdini_version(self):
        job_id = self.submit()["job_id"]

        self.assertEqual(self.poll("PC-NEW", houdini=["22.0.429"])["status"], "wait")
        self.assertEqual(self.state()["jobs"][job_id]["waiting_for"], "a worker with Houdini 21.0.700")

        self.take_task("PC-OLD", houdini=["21.0.700", "22.0.429"])
        self.assertIsNone(self.state()["jobs"][job_id]["waiting_for"])

    def test_bat_job_is_not_tied_to_a_houdini_version(self):
        self.new_job()
        self.take_task("PC-A", houdini=[])

    def test_cancelled_task_resumes_with_only_the_frames_not_yet_on_disk(self):
        self.submit()
        task = self.take_task("PC-A")
        self.report("PC-A", task, frames_done=[1, 2, 3])
        self.report("PC-A", task, frames_done=[1, 2, 3, 4])

        self.client.post(f"/api/tasks/{task['id']}/cancel")
        self.client.post(f"/api/tasks/{task['id']}/start")
        again = self.take_task("PC-A")

        self.assertEqual(again["id"], task["id"])
        self.assertEqual(again["frames"], [5, 6, 7, 8, 9, 10])
        self.assertEqual(sorted(again["outputs"], key=int), ["5", "6", "7", "8", "9", "10"])

    def test_frames_reported_by_a_cancelled_attempt_do_not_count(self):
        self.submit()
        task = self.take_task("PC-A")
        self.client.post(f"/api/tasks/{task['id']}/cancel")

        self.report("PC-A", task, "cancelled", frames_done=[1, 2, 3])

        self.assertEqual(self.task(task["id"])["done_frames"], [])

    def test_rendering_a_completed_task_again_renders_every_frame(self):
        self.submit()
        task = self.take_task("PC-A")
        self.report("PC-A", task, "completed", frames_done=list(range(1, 11)))

        self.client.post(f"/api/tasks/{task['id']}/start")

        self.assertEqual(self.task(task["id"])["done_frames"], [])

    def test_task_whose_frames_are_all_on_disk_completes_without_rendering(self):
        job_id = self.submit(frames=[1, 10, 1])["job_id"]
        task = self.take_task("PC-A")
        self.report("PC-A", task, frames_done=list(range(1, 11)))
        self.client.post(f"/api/tasks/{task['id']}/cancel")
        self.client.post(f"/api/tasks/{task['id']}/start")

        self.assertEqual(self.poll("PC-A")["status"], "wait")
        self.assertEqual(self.task(task["id"])["status"], "completed")
        self.assertEqual(self.job_status(job_id), "completed")

    def test_task_that_fails_on_one_machine_is_tried_on_the_other_first(self):
        self.submit()
        on_a = self.take_task("PC-A")
        on_b = self.take_task("PC-B")

        self.report("PC-A", on_a, "failed", logs="out of VRAM\n")

        failed = self.task(on_a["id"])
        self.assertEqual(failed["status"], "queued")
        self.assertIn("failed on PC-A", failed["logs"])
        # PC-A moves on to the next chunk rather than trying the same one again
        self.assertNotEqual(self.take_task("PC-A")["id"], on_a["id"])

        self.report("PC-B", on_b, "completed")
        retry = self.take_task("PC-B")
        self.assertEqual(retry["id"], on_a["id"])

        # Failing there too is final
        self.report("PC-B", retry, "failed")
        self.assertEqual(self.task(on_a["id"])["status"], "failed")

    def test_failed_task_stays_failed_when_there_is_no_other_machine(self):
        self.submit()
        task = self.take_task("PC-A")

        self.report("PC-A", task, "failed")

        self.assertEqual(self.task(task["id"])["status"], "failed")

    def test_machine_without_the_houdini_version_does_not_count_as_another_machine(self):
        self.submit()
        self.poll("PC-NEW", houdini=["22.0.429"])
        task = self.take_task("PC-A")

        self.report("PC-A", task, "failed")

        self.assertEqual(self.task(task["id"])["status"], "failed")

    def test_open_output_only_ever_opens_a_folder(self):
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as folder, mock.patch.object(manager.os, "startfile", create=True) as startfile:
            a_file = os.path.join(folder, "run_me.bat")
            open(a_file, "w").close()

            opened = self.submit(outputs={"1": os.path.join(folder, "shot.0001.exr")}, frames=[1, 1, 1])["job_id"]
            self.assertEqual(self.client.post(f"/api/jobs/{opened}/open-output").status_code, 200)
            startfile.assert_called_once_with(folder)

            startfile.reset_mock()
            for output_dir in (a_file, os.path.join(folder, "not_there"), None):
                job_id = self.submit(outputs={}, output_dir=output_dir)["job_id"]
                self.assertEqual(self.client.post(f"/api/jobs/{job_id}/open-output").status_code, 404)
            startfile.assert_not_called()


class JobStatusTests(ManagerTestCase):
    def set_statuses(self, job_id, statuses):
        for task, status in zip(manager.job_tasks(job_id), statuses):
            task["status"] = status

    def test_job_status_is_a_summary_of_its_tasks(self):
        job_id = self.new_job(start=False)
        cases = [
            (["completed", "completed", "completed"], "completed"),
            (["completed", "running", "failed"], "running"),
            (["completed", "queued", "failed"], "queued"),
            (["completed", "failed", "paused"], "failed"),
            (["completed", "paused", "cancelled"], "paused"),
            (["completed", "completed", "cancelled"], "cancelled"),
        ]
        for statuses, expected in cases:
            with self.subTest(statuses=statuses):
                self.set_statuses(job_id, statuses)
                self.assertEqual(self.job_status(job_id), expected)


if __name__ == "__main__":
    unittest.main()
