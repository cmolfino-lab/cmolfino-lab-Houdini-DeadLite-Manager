"""Renders one DeadLite task. The worker runs this with hython:

    hython hython_runner.py --hip <file> --node <rop path> --frames 1 2 3 [--name <original hip path>]

Used instead of Houdini's hrender.py because it reports progress, renders an
arbitrary list of frames, and exits with an error code when the render fails.
Kept free of recent Python syntax: it runs under whatever Python the job's
Houdini version ships with.
"""
import argparse
import re
import sys

# $F, $F4, ${F4} and $FF, with or without a leading backslash. Not $FPS, $FSTART or $FEND.
FRAME_VARIABLE = re.compile(r"\\?\$(?:\{(FF|F\d*)\}|(FF|F\d*)(?![A-Za-z0-9_]))")


def fail(message):
    print("[deadlite] ERROR: " + message, flush=True)
    sys.exit(1)


def frame_runs(frames):
    """Group frames into (start, end, step) runs: [1, 2, 3, 7, 8] -> [(1, 3, 1), (7, 8, 1)]."""
    runs = []
    i = 0
    while i < len(frames):
        step = frames[i + 1] - frames[i] if i + 1 < len(frames) else 1
        j = i
        while j + 1 < len(frames) and frames[j + 1] - frames[j] == step:
            j += 1
        runs.append((frames[i], frames[j], step))
        i = j + 1
    return runs


def force_parm(node, name, value):
    parm = node.parm(name)
    if parm is None:
        return
    import hou
    try:
        parm.deleteAllKeyframes()
        parm.set(value)
    except hou.Error as e:
        print("[deadlite] warning: could not set %s on %s: %s" % (name, node.path(), e), flush=True)


def output_override_fits_one_process(node):
    """Make a USD Render ROP's Override Output Image usable by a single husk process.

    The ROP hands husk the override as a fixed string, so with one process for
    many frames "$F4" would name the same file every time. The frame variables
    are rewritten as husk's own tokens (<F4>), which husk fills in per frame.
    Returns False when that is not possible, and the node then has to start
    husk once per frame instead.
    """
    import hou

    parm = node.parm("outputimage")
    if parm is None or not parm.eval() or parm.evalAtFrame(1) == parm.evalAtFrame(2):
        return True  # no override, or one that does not change with the frame
    if parm.keyframes():
        return False
    try:
        text = parm.unexpandedString()
    except hou.OperationFailed:
        return False
    if "`" in text:
        return False
    parm.set(FRAME_VARIABLE.sub(lambda m: "<%s>" % (m.group(1) or m.group(2)), text))
    return parm.evalAtFrame(1) == parm.evalAtFrame(2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hip", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--frames", required=True, nargs="+", type=float)
    parser.add_argument("--name", help="path the scene should think it was loaded from")
    args = parser.parse_args()

    import hou

    print("[deadlite] Houdini %s loading %s" % (hou.applicationVersionString(), args.hip), flush=True)
    try:
        hou.hipFile.load(args.hip, suppress_save_prompt=True, ignore_load_warnings=True)
    except hou.LoadWarning as e:
        print(e, flush=True)
    except hou.Error as e:
        fail("could not load %s: %s" % (args.hip, e))

    if args.name:
        # The file is a snapshot taken when the job was submitted. Give the
        # session the artist's file name back, so $HIP and $HIPNAME (and every
        # output path built from them) are what they were at submit time.
        hou.hipFile.setName(args.name)

    node = hou.node(args.node)
    if node is None:
        fail("node %s does not exist in %s" % (args.node, args.hip))
    if not isinstance(node, hou.RopNode):
        fail("node %s is a %s, not something that can be rendered" % (args.node, node.type().name()))

    usd_render = node.type().name().split("::")[0] in ("usdrender", "usdrender_rop")
    if usd_render:
        # One husk process for the whole task instead of one per frame, wait
        # for it to finish, and have husk print the progress.
        one_process = output_override_fits_one_process(node)
        if not one_process:
            print("[deadlite] the output image override changes per frame in a way husk cannot follow: "
                  "starting husk once per frame", flush=True)
        force_parm(node, "allframesatonce", 1 if one_process else 0)
        force_parm(node, "soho_foreground", 1)
        force_parm(node, "alfprogress", 1)
        force_parm(node, "husk_mplay", 0)

    for start, end, step in frame_runs(args.frames):
        print("[deadlite] rendering %s frames %g-%g step %g" % (node.path(), start, end, step), flush=True)
        try:
            # A USD Render ROP "renders" each frame instantly (it only writes
            # USD) and husk does the real work afterwards, so its own per-frame
            # progress would hit 100% before the render starts.
            node.render(frame_range=(start, end, step), verbose=True, output_progress=not usd_render)
        except hou.OperationFailed as e:
            fail("%s failed: %s" % (node.path(), e))
        errors = node.errors()
        if errors:
            fail("%s reported errors: %s" % (node.path(), "; ".join(errors)))

    print("[deadlite] finished", flush=True)


if __name__ == "__main__":
    main()
