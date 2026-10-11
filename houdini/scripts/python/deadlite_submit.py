"""
Submit to DeadLite - sends the selected render nodes straight to the DeadLite manager.
Houdini 21 / 22 | PySide6

Install: copy to <a folder on HOUDINI_PATH>/scripts/python/deadlite_submit.py
         (it is run by the "Submit to DeadLite" shelf tool in toolbar/deadlite.shelf).
After editing this file in a running session: the shelf tool reloads it on every click.

Two ways a node can be rendered:

  husk    USD Render ROPs only. The stage is written to a job folder once, here,
          and the workers render that file with husk. Starts fast, needs no
          scene load, and later edits to the scene cannot change the job.
  hython  Any ROP. Workers open a snapshot of the .hip and render the node.
          Used for caches, simulations and non-USD renderers.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import hou
from PySide6 import QtWidgets

USD_RENDER_TYPES = ("usdrender", "usdrender_rop")

# Output parameters of ROPs whose files are named exactly as the parameter says.
# Used to tell which frames are finished, so anything less certain is left out.
EXACT_OUTPUT_PARMS = ("sopoutput", "vm_picture", "picture", "copoutput")
# Good enough to find the output folder, not to predict file names.
FOLDER_OUTPUT_PARMS = ("RS_outputFileNamePrefix", "ar_picture", "dopoutput", "lopoutput", "filename")

DEFAULTS = {
    "manager_url": "http://127.0.0.1:5000",
    "chunk": 10,
    "route": "husk",
    "paused": False,
    "caches_in_one_task": True,
}


class SubmitError(Exception):
    """A problem to show the artist as a message, not a traceback."""


# --- Preferences -------------------------------------------------------------

def prefs_path() -> str:
    return os.path.join(hou.getenv("HOUDINI_USER_PREF_DIR"), "deadlite_submit.json")


def load_prefs() -> dict:
    prefs = dict(DEFAULTS)
    try:
        with open(prefs_path(), encoding="utf-8") as f:
            prefs.update(json.load(f))
    except (OSError, ValueError):
        pass
    return prefs


def save_prefs(prefs: dict) -> None:
    try:
        with open(prefs_path(), "w", encoding="utf-8") as f:
            json.dump(prefs, f, indent=4)
    except OSError:
        pass  # remembering settings is a convenience, never a reason to fail


# --- Reading the node ----------------------------------------------------------

def is_usd_render(node: hou.Node) -> bool:
    return node.type().name().split("::")[0] in USD_RENDER_TYPES


def is_image_render(node: hou.Node) -> bool:
    """Renders can be split across machines frame by frame. Simulations cannot, and caches often should not."""
    if is_usd_render(node):
        return True
    return any(node.parm(name) for name in ("vm_picture", "picture", "RS_outputFileNamePrefix", "ar_picture"))


def frame_range(node: hou.Node) -> tuple[float, float, float]:
    """The range in the node's Start/End/Inc fields.

    Read even when the node is set to "Render Current Frame": that is the
    usual setting while working, and a farm job nearly always wants the range.
    """
    if node.parmTuple("f") is not None:
        start, end, step = node.parmTuple("f").eval()
        return start, end, step or 1
    start, end = hou.playbar.playbackRange()
    return start, end, 1


def count(number: int, word: str) -> str:
    return f"{number} {word}" if number == 1 else f"{number} {word}s"


def frames_in(start: float, end: float, step: float) -> list:
    total = int((end - start) / step + 1e-9) + 1
    frames = [start + i * step for i in range(total)]
    return [int(f) if float(f).is_integer() else f for f in frames]


# $F, $F4, ${F4} and $FF, with or without the backslash Houdini's warning on the
# ROP asks for. Not $FPS, $FSTART or $FEND.
FRAME_VARIABLE = re.compile(r"\\?\$(?:\{(FF|F\d*)\}|(FF|F\d*)(?![A-Za-z0-9_]))")
NODE_NAME_VARIABLE = re.compile(r"\$(?:\{OS\}|OS(?![A-Za-z0-9_]))")


def husk_frame_tokens(text: str) -> str:
    """Houdini's frame variables rewritten as husk's own: $F4 -> <F4>.

    Houdini leaves <F4> alone when it expands the rest of the path, and husk
    fills it in for each frame it renders.
    """
    return FRAME_VARIABLE.sub(lambda m: f"<{m.group(1) or m.group(2)}>", text)


def expand_frame_tokens(pattern: str, frame) -> str | None:
    """The file husk writes for one frame of an <F4> style path. None if we cannot tell."""
    if "<FF>" in pattern or not float(frame).is_integer():
        return None
    return re.sub(r"<F(\d*)>", lambda m: f"{int(frame):0{int(m.group(1) or 1)}d}", pattern)


def outputs_from_override(override: str, frames: list) -> dict:
    """Image file of each frame when the ROP overrides the output image."""
    first_product = override.split(",")[0]
    return literal_per_frame({frame: expand_frame_tokens(first_product, frame) for frame in frames})


def literal_per_frame(names: dict) -> dict:
    """Keep a frame -> file mapping only if it really names one finished file per frame."""
    values = list(names.values())
    if not values or not all(isinstance(v, str) and v for v in values):
        return {}
    if any(token in v for v in values for token in ("$", "<", "%")):
        return {}  # the renderer fills these in itself, we would be guessing
    if len(values) > 1 and len(set(values)) != len(values):
        return {}  # every frame goes to the same file
    return {str(frame): path.replace("\\", "/") for frame, path in names.items()}


def outputs_from_usd(usd_file: str, settings_path: str, frames: list) -> dict:
    """Image file of each frame, read from the first render product of an exported USD."""
    from pxr import Usd, UsdRender

    stage = Usd.Stage.Open(usd_file, Usd.Stage.LoadNone)
    if not stage:
        return {}
    path = settings_path or stage.GetMetadata("renderSettingsPrimPath")
    if not path:
        return {}
    if not path.startswith("/"):
        path = "/Render/" + path
    settings = UsdRender.Settings(stage.GetPrimAtPath(path))
    targets = settings.GetProductsRel().GetTargets() if settings else []
    if not targets:
        return {}
    name = UsdRender.Product(stage.GetPrimAtPath(targets[0])).GetProductNameAttr()
    return literal_per_frame({frame: name.Get(Usd.TimeCode(frame)) for frame in frames})


def outputs_from_parms(node: hou.Node, frames: list) -> tuple[dict, str | None]:
    """(file of each frame, output folder) for a classic ROP, as far as its parameters tell."""
    for name in EXACT_OUTPUT_PARMS:
        parm = node.parm(name)
        if parm is not None:
            names = {frame: parm.evalAtFrame(frame) for frame in frames}
            folder = os.path.dirname(names[frames[0]]) or None
            return literal_per_frame(names), folder
    for name in FOLDER_OUTPUT_PARMS:
        parm = node.parm(name)
        if parm is not None and parm.evalAtFrame(frames[0]):
            return {}, os.path.dirname(parm.evalAtFrame(frames[0])) or None
    return {}, None


def basic_husk_args(node: hou.Node) -> list[str]:
    """The main husk options of a USD Render ROP, read parameter by parameter.

    Only a fallback for when the node's own command line could not be recorded
    (see export_usd): this knows a handful of options, the node has dozens.
    """
    def value(name):
        parm = node.parm(name)
        return parm.eval() if parm is not None else None

    args = []
    # "Default Delegate" (Houdini 22's default) means: let husk and the render
    # settings decide. The ROP passes no -R for it, and neither do we.
    if value("renderer") and value("renderer") != "default_delegate":
        args += ["-R", value("renderer")]
    if value("rendersettings"):
        args += ["-s", value("rendersettings")]
    if value("husk_purpose"):
        args += ["--purpose", value("husk_purpose")]
    if value("husk_complexity"):
        args += ["--complexity", value("husk_complexity")]
    if value("husk_restartdelegate"):
        args += ["--restart-delegate", str(value("husk_restartdelegateframes"))]
    if value("husk_gpu"):
        args += ["--gpu"]
    if value("husk_instantshutter"):
        args += ["--disable-motionblur"]
    if value("husk_dotimelimit"):
        args += ["--timelimit", str(value("husk_timelimit"))]
        if value("husk_timelimitperimage"):
            args += ["--timelimit-image"]
    if value("husk_legacyexr"):
        args += ["--exrmode", "0"]
    if value("outputimage"):
        args += ["-o", value("outputimage")]
    return args


# husk options the worker supplies itself: the frames of each task, and the
# progress output and folder creation the queue relies on. husk refuses an
# option that is given twice, so they must not also come from here.
WORKER_OPTIONS_WITH_VALUE = ("-f", "--frame", "-n", "--frame-count", "-i", "--frame-inc", "-V", "--verbose")
WORKER_FLAGS = ("--make-output-path",)


def options_for_every_task(recorded: list, usd_file: str) -> list[str]:
    """A recorded husk command line without what the worker adds itself: frames, scene file, progress output."""
    def is_scene(arg):
        return os.path.normcase(os.path.normpath(arg)) == os.path.normcase(os.path.normpath(usd_file))

    options, skip_value = [], False
    for arg in recorded:
        if skip_value:
            skip_value = False
        elif arg in WORKER_OPTIONS_WITH_VALUE:
            skip_value = True
        elif arg not in WORKER_FLAGS and not is_scene(arg):
            options.append(arg)
    return options


def option_value(args: list, *names: str) -> str:
    """Value of the first of these options present on a command line, "" if none is."""
    for name in names:
        if name in args[:-1]:
            return args[args.index(name) + 1]
    return ""


def carry_over_node_name(copy: hou.Node, node: hou.Node) -> None:
    """Make $OS on the temporary copy mean the original node's name.

    A copy cannot have the same name as its original, and output paths very often contain $OS.
    """
    for parm in copy.parms():
        if parm.parmTemplate().type() != hou.parmTemplateType.String or parm.keyframes():
            continue
        try:
            text = parm.unexpandedString()
            renamed = NODE_NAME_VARIABLE.sub(node.name(), text)
            if renamed != text:
                parm.set(renamed)
        except hou.Error:
            pass  # locked, or otherwise not ours to change


def keep_frame_number_open(copy: hou.Node, node: hou.Node) -> None:
    """Rewrite the copy's Override Output Image so that husk fills in the frame number, not Houdini.

    The ROP hands husk the override as a fixed string. With one husk process
    for many frames, "$F4" would arrive as the current frame and every frame
    would be written to that one file (the warning under the parameter is
    about this). husk has its own frame tokens, so the path is passed with
    those instead.
    """
    parm = copy.parm("outputimage")
    if parm is None or not parm.eval():
        return

    text = None
    if not parm.keyframes():
        try:
            text = parm.unexpandedString()
        except hou.OperationFailed:
            pass
    if text is None or "`" in text:
        # Set by an expression. Fine if it names one file, impossible to hand over if it changes per frame.
        if parm.evalAtFrame(1) != parm.evalAtFrame(2):
            raise SubmitError(
                f"{node.path()}: Override Output Image is set by an expression that changes every frame.\n"
                "Write the path with $F4 instead, or submit this node with the hython option.")
        return
    parm.set(husk_frame_tokens(text))


def plain_python() -> str | None:
    """The ordinary Python that ships inside Houdini. Starts instantly and takes no license, unlike hython."""
    path = os.path.join(hou.getenv("HFS"), f"python{sys.version_info.major}{sys.version_info.minor}", "python.exe")
    return path.replace("\\", "/") if os.path.isfile(path) else None


# --- Preparing what the workers will load -------------------------------------

def force_parm(node: hou.Node, name: str, value) -> None:
    parm = node.parm(name)
    if parm is not None:
        parm.deleteAllKeyframes()
        parm.set(value)


def new_job_folder(node: hou.Node) -> str:
    """$HIP/deadlite/<scene>_<node>_<time>: everything the workers load for one job."""
    hip_name = os.path.splitext(os.path.basename(hou.hipFile.path()))[0]
    stamp = time.strftime("%Y%m%d_%H%M%S")
    base = os.path.join(hou.text.expandString("$HIP"), "deadlite", f"{hip_name}_{node.name()}_{stamp}")
    folder, n = base, 1
    while os.path.exists(folder):
        n += 1
        folder = f"{base}_{n}"
    os.makedirs(folder)
    return folder.replace("\\", "/")


# Started by the ROP in place of husk: writes down the arguments it was given.
RECORDER = "import json, sys\njson.dump(sys.argv[2:], open(sys.argv[1], 'w'))\n"


def export_usd(node: hou.Node, folder: str, frames: tuple[float, float, float]) -> tuple[str, list[str]]:
    """Write the stage a USD Render ROP would render into `folder`, and find out how it wants husk run.

    Uses the ROP's own machinery, on a temporary copy of the node so the
    artist's node is not touched:

    - "Save All Files to a Specific Directory" puts every layer inside the job
      folder. The artist's own layer files are left alone and the job is
      unaffected by later edits.
    - The copy's Render Command is a recorder instead of husk. The ROP writes
      its USD and then starts the recorder with exactly the arguments it would
      have given husk. Every option on the node (render settings, render pass,
      camera and resolution overrides, scripts, ...) is carried over that way,
      for any Houdini version, without this tool knowing each of them.

    Returns the USD file and the husk options that apply to every task.
    """
    usd_file = f"{folder}/{node.name()}.usd"
    recorder = f"{folder}/record_husk_args.py"
    recorded = f"{folder}/husk_args.json"
    python = plain_python()

    with hou.undos.disabler():
        copy = hou.copyNodesTo([node], node.parent())[0]
        try:
            carry_over_node_name(copy, node)
            keep_frame_number_open(copy, node)
            if not copy.evalParm("loppath"):
                # A USD Render ROP inside a LOP network renders its input. The copy has none.
                source = node.inputs()[0] if node.inputs() else None
                if source is None:
                    raise SubmitError(f"{node.path()} has nothing to render: connect it or set its LOP Path.")
                force_parm(copy, "loppath", source.path())
            force_parm(copy, "trange", 1)
            force_parm(copy, "allframesatonce", 1)
            force_parm(copy, "alfprogress", 1)  # the dashboard's progress bar reads it
            force_parm(copy, "husk_mplay", 0)
            force_parm(copy, "husk_debug", 0)
            force_parm(copy, "enableoutputprocessor_savetodirectory", 1)
            force_parm(copy, "savetodirectory_directory", folder)
            force_parm(copy, "lopoutput", os.path.basename(usd_file))
            if python:
                with open(recorder, "w", encoding="utf-8") as f:
                    f.write(RECORDER)
                force_parm(copy, "runcommand", 1)
                force_parm(copy, "soho_foreground", 1)  # wait until the recorder has written its file
                force_parm(copy, "rendercommand", f'"{python}" "{recorder}" "{recorded}"')
            else:
                force_parm(copy, "runcommand", 0)
            try:
                copy.render(frame_range=frames)
            except hou.OperationFailed as e:
                raise SubmitError(f"Could not write the USD for {node.path()}:\n{e}\n" + "\n".join(copy.errors()))
            fallback = basic_husk_args(copy)
        finally:
            copy.destroy()

    if not os.path.isfile(usd_file):
        raise SubmitError(f"{node.path()} did not write {usd_file}")
    try:
        with open(recorded, encoding="utf-8") as f:
            options = options_for_every_task(json.load(f), usd_file)
    except (OSError, ValueError):
        options = fallback
    if os.path.isfile(recorder):
        os.remove(recorder)
    return usd_file, options


def snapshot_hip(folder: str) -> str:
    """Copy the saved .hip into the job folder, keeping its file name.

    Workers load the copy and then rename the session back to the original
    path, so $HIP and $HIPNAME resolve exactly as they do here.
    """
    hip = hou.hipFile.path()
    copy = f"{folder}/{os.path.basename(hip)}"
    shutil.copy2(hip, copy)
    return copy


def build_job(node: hou.Node, route: str, frames: tuple[float, float, float], chunk: int, paused: bool) -> dict:
    """Prepare the files for one node and return the job description for the manager."""
    hip = hou.hipFile.path().replace("\\", "/")
    hip_name = os.path.splitext(os.path.basename(hip))[0]
    all_frames = frames_in(*frames)
    job = {
        "name": f"{hip_name} / {node.name()}",
        "houdini": hou.applicationVersionString(),
        "frames": list(frames),
        "chunk": chunk,
        "paused": paused,
    }

    folder = new_job_folder(node)
    if route == "husk" and is_usd_render(node):
        usd_file, args = export_usd(node, folder, frames)
        override = option_value(args, "-o", "--output")
        if override:
            outputs = outputs_from_override(override, all_frames)
            job["output_dir"] = os.path.dirname(override.split(",")[0])
        elif option_value(args, "--pass"):
            outputs = {}  # a render pass can bring its own render settings, so the file names are not ours to predict
        else:
            outputs = outputs_from_usd(usd_file, option_value(args, "-s", "--settings"), all_frames)
        job.update(type="husk", scene=usd_file, args=args, outputs=outputs)
    else:
        outputs, output_dir = outputs_from_parms(node, all_frames)
        job.update(type="hython", scene=snapshot_hip(folder), hip_name=hip, node=node.path(),
                   outputs=outputs, output_dir=output_dir)
    return job


# --- Talking to the manager -----------------------------------------------------

def ipv4_url(url: str) -> str:
    """The same URL with its host replaced by an IPv4 address.

    The manager listens on IPv4 only. A host name usually also has IPv6
    addresses, and those are tried first: each one waits out a full timeout.
    """
    parts = urllib.parse.urlsplit(url)
    try:
        address = socket.getaddrinfo(parts.hostname, parts.port or 80, socket.AF_INET)[0][4][0]
    except (socket.gaierror, TypeError):
        return url
    return urllib.parse.urlunsplit(parts._replace(netloc=f"{address}:{parts.port or 80}"))


def post_job(manager_url: str, job: dict) -> dict:
    url = ipv4_url(manager_url.rstrip("/")) + "/api/jobs"
    request = urllib.request.Request(url, data=json.dumps(job).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        try:
            reason = json.load(e).get("error", "")
        except ValueError:
            reason = ""
        raise SubmitError(f"The DeadLite manager refused the job: {reason or e}")
    except (urllib.error.URLError, OSError) as e:
        raise SubmitError(f"Could not reach the DeadLite manager at {manager_url}.\n"
                          f"Is deadlite_manager running there?\n\n{getattr(e, 'reason', e)}")


def submit(nodes: list, settings: dict) -> list[str]:
    """Submit each node as its own job. Returns one line per job for the artist."""
    lines = []
    for node in nodes:
        route = settings["route"] if is_usd_render(node) else "hython"
        chunk = settings["chunk"]
        if settings["caches_in_one_task"] and not is_image_render(node):
            chunk = 0
        frames = settings.get("frames") or frame_range(node)
        job = build_job(node, route, frames, chunk, settings["paused"])
        reply = post_job(settings["manager_url"], job)
        lines.append(f"{job['name']}: frames {frames[0]:g}-{frames[1]:g} in {count(reply['tasks'], 'task')} ({job['type']})")
    return lines


# --- Dialog --------------------------------------------------------------------

class SubmitDialog(QtWidgets.QDialog):
    def __init__(self, nodes: list, parent=None):
        super().__init__(parent or hou.qt.mainWindow())
        self.nodes = nodes
        self.prefs = load_prefs()
        self.setWindowTitle("Submit to DeadLite")
        self.setStyleSheet(hou.qt.styleSheet())
        self.setMinimumWidth(460)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel("\n".join(self.describe(node) for node in nodes)))
        form = QtWidgets.QFormLayout()
        layout.addLayout(form)

        # Frame range: one node shows its range for editing, several keep their own unless overridden
        start, end, step = frame_range(nodes[0])
        self.own_ranges = QtWidgets.QCheckBox("Use each node's own frame range")
        self.own_ranges.setChecked(len(nodes) > 1)
        self.own_ranges.setVisible(len(nodes) > 1)
        self.start = self.frame_box(start)
        self.end = self.frame_box(end)
        self.step = self.frame_box(step, minimum=1)
        range_row = QtWidgets.QHBoxLayout()
        for label, box in (("Start", self.start), ("End", self.end), ("Step", self.step)):
            range_row.addWidget(QtWidgets.QLabel(label))
            range_row.addWidget(box)
        form.addRow(self.own_ranges)
        form.addRow("Frames", range_row)

        self.chunk = QtWidgets.QSpinBox()
        self.chunk.setRange(0, 100000)
        self.chunk.setSpecialValueText("All in one task")
        self.chunk.setValue(int(self.prefs["chunk"]))
        self.chunk.setToolTip("Each task is one render process on one machine.\n"
                              "Aim for roughly 10-20 minutes per task: fewer frames for heavy scenes.")
        self.task_count = QtWidgets.QLabel()
        chunk_row = QtWidgets.QHBoxLayout()
        chunk_row.addWidget(self.chunk)
        chunk_row.addWidget(self.task_count, 1)
        form.addRow("Frames per task", chunk_row)

        self.route = QtWidgets.QComboBox()
        self.route.addItem("Bake USD, render with husk", "husk")
        self.route.addItem("Open the scene with hython", "hython")
        self.route.setCurrentIndex(max(0, self.route.findData(self.prefs["route"])))
        if any(is_usd_render(node) for node in nodes):
            form.addRow("USD renders", self.route)

        self.caches = QtWidgets.QCheckBox("Keep caches and simulations in one task")
        self.caches.setChecked(bool(self.prefs["caches_in_one_task"]))
        self.caches.setToolTip("A simulation needs its previous frame, so it cannot be split across machines.")
        if any(not is_image_render(node) for node in nodes):
            form.addRow(self.caches)

        self.paused = QtWidgets.QCheckBox("Submit paused (start it from the dashboard)")
        self.paused.setChecked(bool(self.prefs["paused"]))
        form.addRow(self.paused)

        self.manager_url = QtWidgets.QLineEdit(self.prefs["manager_url"])
        form.addRow("Manager", self.manager_url)

        buttons = QtWidgets.QDialogButtonBox()
        buttons.addButton("Submit", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        for widget in (self.start, self.end, self.step, self.chunk):
            widget.valueChanged.connect(self.refresh)
        self.own_ranges.toggled.connect(self.refresh)
        self.refresh()

    @staticmethod
    def frame_box(value: float, minimum: float = -1000000) -> QtWidgets.QDoubleSpinBox:
        box = QtWidgets.QDoubleSpinBox()
        box.setDecimals(0 if float(value).is_integer() else 2)
        box.setRange(minimum, 1000000)
        box.setValue(value)
        return box

    @staticmethod
    def describe(node: hou.Node) -> str:
        kind = "USD render" if is_usd_render(node) else "render" if is_image_render(node) else "cache"
        return f"{node.path()}   ({kind})"

    def refresh(self) -> None:
        own = self.own_ranges.isChecked()
        for box in (self.start, self.end, self.step):
            box.setEnabled(not own)
        if own:
            self.task_count.setText("")
            return
        frames = len(frames_in(self.start.value(), self.end.value(), self.step.value())) if self.end.value() >= self.start.value() else 0
        chunk = self.chunk.value()
        tasks = 1 if chunk == 0 else -(-frames // chunk)
        self.task_count.setText(f"{count(frames, 'frame')} in {count(tasks, 'task')}")

    def settings(self) -> dict:
        frames = None
        if not self.own_ranges.isChecked():
            frames = (self.start.value(), self.end.value(), self.step.value())
        return {
            "manager_url": self.manager_url.text().strip(),
            "chunk": self.chunk.value(),
            "route": self.route.currentData(),
            "paused": self.paused.isChecked(),
            "caches_in_one_task": self.caches.isChecked(),
            "frames": frames,
        }

    def accept(self) -> None:
        settings = self.settings()
        if settings["frames"] and settings["frames"][1] < settings["frames"][0]:
            hou.ui.displayMessage("The end frame is before the start frame.", severity=hou.severityType.Warning)
            return
        save_prefs({key: settings[key] for key in DEFAULTS})
        super().accept()


# --- Entry point -----------------------------------------------------------------

def selected_render_nodes() -> list:
    return [node for node in hou.selectedNodes() if isinstance(node, hou.RopNode)]


def scene_is_ready(nodes: list, settings: dict) -> bool:
    """Job files live next to the .hip and hython jobs load it, so it has to be on disk and current."""
    if not os.path.isfile(hou.hipFile.path()):
        hou.ui.displayMessage("Save the scene first: the job's files are stored next to it.",
                              severity=hou.severityType.Warning)
        return False

    needs_saved_scene = any(settings["route"] == "hython" or not is_usd_render(node) for node in nodes)
    if needs_saved_scene and hou.hipFile.hasUnsavedChanges():
        choice = hou.ui.displayMessage(
            "The scene has unsaved changes.\nWorkers render the saved file, so it needs saving first.",
            buttons=("Save and Submit", "Cancel"), default_choice=0, close_choice=1)
        if choice != 0:
            return False
        hou.hipFile.save()
    return True


def main(kwargs: dict | None = None) -> None:
    nodes = selected_render_nodes()
    if not nodes:
        hou.ui.displayMessage(
            "Select the node to render first: a USD Render ROP, or any other ROP\n"
            "(Redshift, geometry cache, ...).", severity=hou.severityType.Warning, title="Submit to DeadLite")
        return

    dialog = SubmitDialog(nodes)
    if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
        return
    settings = dialog.settings()
    if not scene_is_ready(nodes, settings):
        return

    try:
        with hou.InterruptableOperation("Submitting to DeadLite", open_interrupt_dialog=True):
            lines = submit(nodes, settings)
    except hou.OperationInterrupted:
        hou.ui.displayMessage("Cancelled. Jobs already sent stay in the queue.", title="Submit to DeadLite")
        return
    except SubmitError as e:
        hou.ui.displayMessage(str(e), severity=hou.severityType.Error, title="Submit to DeadLite")
        return

    hou.ui.displayMessage("Submitted:\n\n" + "\n".join(lines), title="Submit to DeadLite")
