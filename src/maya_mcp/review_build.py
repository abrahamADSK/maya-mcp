"""
review_build.py
===============
Deterministic in-Maya **review turntable** recipe — codified so the playblast
step never depends on the LLM improvising (which hung the console: a Viewport-2.0
playblast that actually captured an Arnold/IPR panel saturated Maya's main thread
and timed out). Chat 71.

Runs INSIDE Maya (uses ``maya.cmds``); maya-mcp's server ships this module's
source over the Command Port and calls :func:`review_turntable`. All Maya imports
are inside the functions so the module imports cleanly in the server process.

What :func:`review_turntable` does, deterministically:
1. Sets fps (25 → Maya 'pal') WITHOUT rescaling existing keys, and the frame
   range ``[start, end]``.
2. Frames the model and builds a camera that orbits it 360° over the range.
3. Camera Film Aspect = 1.778, lens squeeze (pixel aspect) = 1.0, Film Fit =
   Overscan → 16:9 square-pixel framing (the anti-anamorphic guarantee comes
   from the camera plus an explicit 16:9 ``widthHeight``, not render globals).
4. Resolves the panel the playblast WILL capture (the focused model panel, else
   the last one), forces **Viewport 2.0** (NEVER Arnold) + the turntable camera
   ON THAT panel, and playblasts **offScreen** with ``editorPanelName`` pinned —
   so the captured panel is exactly the one set to VP2.0 (this is what prevents
   the Chat 71 Arnold-on-main-thread hang). QuickTime/H.264 with an
   avfoundation→PNG-sequence fallback if the encoder is unavailable.
5. In a ``finally`` block, restores the panel camera/renderer, deletes the
   throwaway turntable nodes, and restores the time unit — the deliverable scene
   is left exactly as found.
6. Returns the .mov path plus, if Maya was launched via the ``tk-maya`` engine,
   the asset/task context + a suggested Version code ``{Asset}_{Task}`` so the
   ShotGrid Version is named after the task it was generated in.

It does NOT touch ShotGrid. The caller resolves ``out_path`` via fpt
``tk_resolve_path(movie_asset_publish)`` and does the Version create + set
``sg_path_to_movie`` + ``sg_upload`` to ``sg_uploaded_movie`` with ``version_code``.

GOTCHA baked in (memory ``feedback_maya_gs_arnold_ipr_hang`` /
``feedback_maya_heavy_ops_crash``): VP2.0 + offScreen, pinned to the captured
panel; one attempt — the caller must not retry on error.
"""

from __future__ import annotations

# 25 fps → Maya's 'pal' time unit. Integer broadcast rates only; fractional
# rates (23.976 / 29.97) are not remapped — the scene keeps its current unit.
_FPS_TIME_UNIT = {24: "film", 25: "pal", 30: "ntsc", 48: "show", 60: "ntscf"}


def _engine_context():
    """Best-effort tk-maya context → (asset, task, version_code) or (None, …).

    ``version_code`` = ``{Asset}_{Task}`` so the Version is named after the task
    it was generated in. Silent/None when Maya is not engine'd.
    """
    try:
        import sgtk

        eng = sgtk.platform.current_engine()
        ctx = eng.context if eng else None
        if not ctx:
            return None, None, None
        asset = (ctx.entity or {}).get("name")
        task = (ctx.task or {}).get("name")
        if not task and ctx.step:
            task = (ctx.step or {}).get("name")
        code = f"{asset}_{task}" if (asset and task) else None
        return asset, task, code
    except Exception:
        return None, None, None


def review_turntable(
    out_path,
    start=1,
    end=100,
    fps=25,
    width=1920,
    height=1080,
    objects=None,
    focal=50.0,
):
    """Deterministic turntable playblast → ``out_path`` (.mov). See module docstring.

    ``out_path`` is REQUIRED (resolve it via fpt ``tk_resolve_path`` upstream).
    ``objects`` frames a specific selection; otherwise the current selection, else
    every top-level assembly (excluding the default cameras).
    """
    import os

    import maya.cmds as cmds

    out_path = str(out_path)
    parent = os.path.dirname(out_path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)

    objs = (objects or cmds.ls(sl=True, long=True)
            or [a for a in (cmds.ls(assemblies=True) or [])
                if a not in ("persp", "top", "front", "side")])
    if not objs:
        return {"error": "nothing to frame (empty scene / no selection)"}
    bb = cmds.exactWorldBoundingBox(objs)
    cx, cy, cz = (bb[0] + bb[3]) / 2.0, (bb[1] + bb[4]) / 2.0, (bb[2] + bb[5]) / 2.0
    size = max(bb[3] - bb[0], bb[4] - bb[1], bb[5] - bb[2]) or 1.0

    prev_unit = cmds.currentUnit(query=True, time=True)
    piv = None
    panel = prev_cam = prev_rnm = None
    used = {}
    mov = None
    err = None
    try:
        unit = _FPS_TIME_UNIT.get(int(fps))
        if unit and unit != prev_unit:
            cmds.currentUnit(time=unit, updateAnimation=False)  # do NOT rescale keys
        cmds.playbackOptions(minTime=start, maxTime=end,
                             animationStartTime=start, animationEndTime=end)

        # turntable camera orbiting the bbox centre
        cam_t, cam_s = cmds.camera(name="reviewTurntableCam")
        piv = cmds.group(empty=True, name="reviewTurntablePivot")
        cmds.xform(piv, worldSpace=True, translation=(cx, cy, cz))
        cmds.parent(cam_t, piv)
        cmds.setAttr(cam_t + ".translate", 0.0, size * 0.15, size * 2.4, type="double3")
        cmds.setAttr(cam_t + ".rotateX", -6.0)
        cmds.setAttr(cam_s + ".focalLength", float(focal))
        hfa = cmds.getAttr(cam_s + ".horizontalFilmAperture")
        cmds.setAttr(cam_s + ".verticalFilmAperture", hfa / 1.778)  # 16:9
        cmds.setAttr(cam_s + ".lensSqueezeRatio", 1.0)              # square pixels
        cmds.setAttr(cam_s + ".filmFit", 3)                         # 3 = Overscan
        cmds.cutKey(piv + ".rotateY")
        cmds.setKeyframe(piv + ".rotateY", time=start, value=0.0)
        cmds.setKeyframe(piv + ".rotateY", time=end, value=360.0)
        cmds.keyTangent(piv + ".rotateY", inTangentType="linear", outTangentType="linear")

        # Pin the playblast to the panel we configure: playblast captures the
        # FOCUSED panel, so set VP2.0 + the turntable cam on THAT one and pass
        # editorPanelName. Without this it grabs the focused panel's renderer —
        # if that's Arnold/IPR it hangs Maya's main thread (the bug to prevent).
        mps = cmds.getPanel(type="modelPanel") or []
        panel = cmds.getPanel(withFocus=True)
        if panel not in mps:
            panel = mps[-1] if mps else None
        if panel:
            prev_cam = cmds.modelEditor(panel, query=True, camera=True)
            prev_rnm = cmds.modelEditor(panel, query=True, rendererName=True)
            cmds.modelPanel(panel, edit=True, camera=cam_s)
            cmds.modelEditor(panel, edit=True, rendererName="vp2Renderer",
                             displayAppearance="smoothShaded", displayTextures=True,
                             headsUpDisplay=False, grid=False)

        # choose an available movie encoder, else fall back (never raise out)
        fmts = cmds.playblast(query=True, format=True) or []
        pb = dict(filename=out_path, widthHeight=(int(width), int(height)),
                  percent=100, quality=95, forceOverwrite=True,
                  viewer=False, offScreen=True, showOrnaments=False, framePadding=4,
                  startTime=start, endTime=end)
        if panel:
            pb["editorPanelName"] = panel
        if "qt" in fmts:
            pb["format"] = "qt"
            if "H.264" in (cmds.playblast(query=True, compression=True) or []):
                pb["compression"] = "H.264"
            used = {"format": "qt", "compression": pb.get("compression", "default")}
        elif "avfoundation" in fmts:
            pb["format"] = "avfoundation"
            used = {"format": "avfoundation"}
        else:
            pb["format"] = "image"
            pb["compression"] = "png"
            pb["filename"] = os.path.splitext(out_path)[0]
            used = {"format": "image", "compression": "png",
                    "note": "qt/avfoundation unavailable — wrote a PNG sequence, not a .mov"}
        try:
            mov = cmds.playblast(**pb)
        except RuntimeError as exc:
            err = f"playblast failed ({used.get('format')}): {exc}"
    finally:
        # leave the deliverable scene exactly as found
        if panel and prev_cam:
            try:
                cmds.modelPanel(panel, edit=True, camera=prev_cam)
            except Exception:
                pass
        if panel and prev_rnm:
            try:
                cmds.modelEditor(panel, edit=True, rendererName=prev_rnm)
            except Exception:
                pass
        if piv and cmds.objExists(piv):
            try:
                cmds.delete(piv)  # deletes the parented turntable camera too
            except Exception:
                pass
        try:
            if cmds.currentUnit(query=True, time=True) != prev_unit:
                cmds.currentUnit(time=prev_unit, updateAnimation=False)
        except Exception:
            pass

    if err:
        return {"error": err, "format_attempted": used}

    asset, task, version_code = _engine_context()
    return {
        "mov": mov or out_path,
        "frames": [start, end],
        "fps": int(fps),
        "resolution": [int(width), int(height)],
        "format": used,
        "asset": asset,
        "task": task,
        "version_code": version_code,  # {Asset}_{Task} — name the Version after the task
        "note": "Set the Version code to version_code (+ _v###), sg_path_to_movie "
                "to mov, and sg_upload mov to sg_uploaded_movie.",
    }
