"""Tests for the parts of the Houdini submit tool that do not need Houdini.

The module imports hou and PySide6, which only exist inside Houdini, so both
are replaced by empty stand-ins here. Everything that touches a real scene
(export, snapshot, the dialog) is checked by hand in Houdini instead.

Run from the repository root:
    venv\\Scripts\\python -m unittest discover tests -b
"""
import os
import sys
import types
import unittest

hou = types.ModuleType("hou")
qt_widgets = types.ModuleType("PySide6.QtWidgets")
qt_widgets.QDialog = type("QDialog", (), {})
qt_widgets.QDoubleSpinBox = type("QDoubleSpinBox", (), {})
pyside = types.ModuleType("PySide6")
pyside.QtWidgets = qt_widgets
sys.modules.setdefault("hou", hou)
sys.modules.setdefault("PySide6", pyside)
sys.modules.setdefault("PySide6.QtWidgets", qt_widgets)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'houdini', 'scripts', 'python'))
import deadlite_submit as submit


class FakeParm:
    def __init__(self, value):
        self.value = value

    def eval(self):
        return self.value


class FakeNode:
    """Just enough of hou.Node for the functions that only read parameters."""

    def __init__(self, **parms):
        self.parms = parms

    def parm(self, name):
        return FakeParm(self.parms[name]) if name in self.parms else None


class RecordedCommandTests(unittest.TestCase):
    """The husk options come from the command line the USD Render ROP itself puts together."""

    # As recorded from a USD Render ROP in Houdini 21.0.700 and 22.0.429
    USD = "R:/shot/deadlite/shot_v01_render_20261011_101500/render.usd"
    RECORDED = ["-s", "/Render/alt", "--make-output-path", "-o", "R:/shot/render/ovr/shot.<F4>.exr",
                "-f", "3", "-n", "6", "-i", "1", "-R", "BRAY_HdKarma",
                "--purpose", "geometry,render", "--complexity", "veryhigh", "--snapshot", "300",
                "--restart-delegate", "5", "--mplay-session", "shot_alt", "-V", "a", USD]

    def test_what_the_worker_adds_itself_is_left_out(self):
        # Frames and scene file differ per task. Progress output and folder
        # creation are added by every worker, and husk refuses an option twice.
        self.assertEqual(submit.options_for_every_task(self.RECORDED, self.USD), [
            "-s", "/Render/alt", "-o", "R:/shot/render/ovr/shot.<F4>.exr",
            "-R", "BRAY_HdKarma", "--purpose", "geometry,render", "--complexity", "veryhigh",
            "--snapshot", "300", "--restart-delegate", "5", "--mplay-session", "shot_alt"])

    def test_options_of_a_node_with_a_render_pass_and_output_override(self):
        # Recorded from a real scene: render settings, render pass, output override, motion blur off
        recorded = ["-s", "/Render/karmarendersettings", "--pass", "/Render/Passes/renderpass1",
                    "--make-output-path", "-o", "R:/shot/render/beauty/beauty.<F4>.exr",
                    "-f", "1", "-n", "100", "-i", "1", "--disable-motionblur",
                    "--purpose", "geometry,render", "--complexity", "veryhigh", "--snapshot", "300",
                    "--mplay-session", "shot_v01", "-V", "a", self.USD]
        self.assertEqual(submit.options_for_every_task(recorded, self.USD), [
            "-s", "/Render/karmarendersettings", "--pass", "/Render/Passes/renderpass1",
            "-o", "R:/shot/render/beauty/beauty.<F4>.exr", "--disable-motionblur",
            "--purpose", "geometry,render", "--complexity", "veryhigh", "--snapshot", "300",
            "--mplay-session", "shot_v01"])

    def test_scene_file_is_recognised_however_its_path_is_written(self):
        recorded = ["-R", "BRAY_HdKarma", "r:\\shot\\deadlite\\SHOT_v01_render_20261011_101500\\render.usd"]
        self.assertEqual(submit.options_for_every_task(recorded, self.USD), ["-R", "BRAY_HdKarma"])

    def test_options_this_tool_has_never_heard_of_are_kept(self):
        recorded = ["--pass", "/Render/Passes/renderpass1", "-c", "/cameras/closeup", "--res", "1920", "1080",
                    "--frame", "1", "--frame-count", "100", "--frame-inc", "1", self.USD]
        self.assertEqual(submit.options_for_every_task(recorded, self.USD),
                         ["--pass", "/Render/Passes/renderpass1", "-c", "/cameras/closeup", "--res", "1920", "1080"])

    def test_reading_one_option_back(self):
        options = submit.options_for_every_task(self.RECORDED, self.USD)
        self.assertEqual(submit.option_value(options, "-o", "--output"), "R:/shot/render/ovr/shot.<F4>.exr")
        self.assertEqual(submit.option_value(options, "--settings", "-s"), "/Render/alt")
        self.assertEqual(submit.option_value(options, "--pass"), "")
        self.assertEqual(submit.option_value(["--gpu", "-o"], "-o"), "")


class BasicHuskArgsTests(unittest.TestCase):
    """The fallback for when the ROP's own command line could not be recorded."""

    def test_settings_on_the_node_become_husk_options(self):
        node = FakeNode(renderer="BRAY_HdKarmaXPU", rendersettings="/Render/beauty",
                        husk_purpose="geometry,render", husk_complexity="veryhigh",
                        husk_restartdelegate=1, husk_restartdelegateframes=5,
                        husk_gpu=0, husk_instantshutter=0, husk_dotimelimit=0, husk_legacyexr=0, outputimage="")

        self.assertEqual(submit.basic_husk_args(node), [
            "-R", "BRAY_HdKarmaXPU", "-s", "/Render/beauty",
            "--purpose", "geometry,render", "--complexity", "veryhigh",
            "--restart-delegate", "5"])

    def test_default_delegate_is_left_for_husk_to_choose(self):
        # Houdini 22's default. Its ROP passes no -R for it; "-R default_delegate" is not a renderer.
        node = FakeNode(renderer="default_delegate", husk_purpose="geometry,render")
        self.assertNotIn("-R", submit.basic_husk_args(node))

    def test_time_limit_and_output_override(self):
        node = FakeNode(renderer="HdRedshiftRendererPlugin", husk_dotimelimit=1, husk_timelimit=300,
                        husk_timelimitperimage=1, outputimage="R:/shot/render/ovr/shot.<F4>.exr")
        self.assertEqual(submit.basic_husk_args(node), [
            "-R", "HdRedshiftRendererPlugin", "--timelimit", "300", "--timelimit-image",
            "-o", "R:/shot/render/ovr/shot.<F4>.exr"])

    def test_node_without_those_parameters_gives_no_options(self):
        self.assertEqual(submit.basic_husk_args(FakeNode()), [])


class FrameTests(unittest.TestCase):
    def test_frames_in_a_range(self):
        self.assertEqual(submit.frames_in(1.0, 5.0, 1.0), [1, 2, 3, 4, 5])
        self.assertEqual(submit.frames_in(10, 20, 5), [10, 15, 20])
        self.assertEqual(submit.frames_in(7, 7, 1), [7])
        self.assertEqual(submit.frames_in(1, 2, 0.5), [1, 1.5, 2])

    def test_counting_words(self):
        self.assertEqual(submit.count(1, "frame"), "1 frame")
        self.assertEqual(submit.count(12, "task"), "12 tasks")


class OutputOverrideTests(unittest.TestCase):
    """The ROP's Override Output Image has to reach husk with the frame number still open."""

    def test_houdini_frame_variables_become_husk_tokens(self):
        for typed, for_husk in (("$HIP/render/shot.$F4.exr", "$HIP/render/shot.<F4>.exr"),
                                ("$HIP/render/shot.${F4}_beauty.exr", "$HIP/render/shot.<F4>_beauty.exr"),
                                ("$HIP/render/shot.$F.exr", "$HIP/render/shot.<F>.exr"),
                                ("$HIP/render/shot.$FF.exr", "$HIP/render/shot.<FF>.exr"),
                                # written the way the ROP's own warning asks for
                                ("$HIP/render/shot.\\$F4.exr", "$HIP/render/shot.<F4>.exr")):
            with self.subTest(typed=typed):
                self.assertEqual(submit.husk_frame_tokens(typed), for_husk)

    def test_other_variables_starting_with_f_are_left_alone(self):
        for typed in ("$HIP/render/${FPS}fps/still.exr", "$HIP/$FSTART-$FEND/still.exr", "$JOB/$F4_beauty.exr"):
            with self.subTest(typed=typed):
                self.assertEqual(submit.husk_frame_tokens(typed), typed)

    def test_node_name_variable_is_found(self):
        pattern = submit.NODE_NAME_VARIABLE
        self.assertEqual(pattern.sub("beauty", "$HIP/render/$OS/${OS}.$F4.exr"), "$HIP/render/beauty/beauty.$F4.exr")
        self.assertEqual(pattern.sub("beauty", "$HIP/$OSX/still.exr"), "$HIP/$OSX/still.exr")

    def test_files_of_an_override_are_worked_out_per_frame(self):
        outputs = submit.outputs_from_override("R:/shot/render/ovr/shot.<F4>.exr", [9, 10, 11])
        self.assertEqual(outputs, {"9": "R:/shot/render/ovr/shot.0009.exr",
                                   "10": "R:/shot/render/ovr/shot.0010.exr",
                                   "11": "R:/shot/render/ovr/shot.0011.exr"})
        self.assertEqual(submit.outputs_from_override("R:/render/shot.<F>.exr", [9, 10]),
                         {"9": "R:/render/shot.9.exr", "10": "R:/render/shot.10.exr"})

    def test_first_file_is_followed_when_several_products_are_overridden(self):
        outputs = submit.outputs_from_override("R:/render/beauty.<F4>.exr,R:/render/depth.<F4>.exr", [1, 2])
        self.assertEqual(outputs, {"1": "R:/render/beauty.0001.exr", "2": "R:/render/beauty.0002.exr"})

    def test_override_we_cannot_predict_is_not_tracked(self):
        self.assertEqual(submit.outputs_from_override("R:/render/shot.<FF>.exr", [1, 2]), {})
        self.assertEqual(submit.outputs_from_override("R:/render/one_file.exr", [1, 2]), {})
        self.assertEqual(submit.outputs_from_override("R:/render/shot.<F4>.exr", [1.5, 2]), {})


class OutputNameTests(unittest.TestCase):
    """Which frame -> file lists are trustworthy enough to decide a frame is finished."""

    def test_one_distinct_file_per_frame_is_kept(self):
        names = {1: "R:\\shot\\render\\shot.0001.exr", 2: "R:\\shot\\render\\shot.0002.exr"}
        self.assertEqual(submit.literal_per_frame(names),
                         {"1": "R:/shot/render/shot.0001.exr", "2": "R:/shot/render/shot.0002.exr"})

    def test_names_the_renderer_still_has_to_fill_in_are_dropped(self):
        for name in ("R:/render/shot.$F4.exr", "R:/render/shot.<F4>.exr", "R:/render/shot.%04d.exr"):
            with self.subTest(name=name):
                self.assertEqual(submit.literal_per_frame({1: name, 2: name}), {})

    def test_every_frame_writing_the_same_file_is_dropped(self):
        self.assertEqual(submit.literal_per_frame({1: "R:/cache/sim.abc", 2: "R:/cache/sim.abc"}), {})

    def test_missing_or_empty_names_are_dropped(self):
        self.assertEqual(submit.literal_per_frame({1: "R:/render/a.exr", 2: None}), {})
        self.assertEqual(submit.literal_per_frame({1: ""}), {})
        self.assertEqual(submit.literal_per_frame({}), {})

    def test_single_frame_job_keeps_its_one_file(self):
        self.assertEqual(submit.literal_per_frame({5: "R:/render/still.exr"}), {"5": "R:/render/still.exr"})


class ManagerAddressTests(unittest.TestCase):
    def test_host_name_is_replaced_by_its_ipv4_address(self):
        # localhost also resolves to ::1, which nothing listens on
        self.assertEqual(submit.ipv4_url("http://localhost:5000"), "http://127.0.0.1:5000")

    def test_address_and_path_are_kept(self):
        self.assertEqual(submit.ipv4_url("http://192.168.1.20:5000/api/jobs"), "http://192.168.1.20:5000/api/jobs")

    def test_name_that_does_not_resolve_is_left_for_the_request_to_report(self):
        self.assertEqual(submit.ipv4_url("http://no-such-host.invalid:5000"), "http://no-such-host.invalid:5000")


if __name__ == "__main__":
    unittest.main()
