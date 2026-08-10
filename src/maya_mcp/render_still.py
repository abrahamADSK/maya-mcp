"""Deterministic single-frame **Arnold** still → ``out_path`` (PNG/JPG).

Why this exists (Chat 82): ``maya_viewport_capture`` is a Viewport-2.0 *grab*
(hardware playblast), NOT a ray-traced render — its own docstring says so. When
the user asks for "a still" meaning an Arnold-rendered image, the only still-ish
tool was the playblast, which (a) is not an Arnold render and (b) hangs Maya if
an Arnold IPR / render-override is live on the focused panel
(``feedback_maya_gs_arnold_ipr_hang``). This module is the missing piece: a
proper Arnold render of ONE frame to an exact file path.

WHO WRITES THE FILE IS THE WHOLE DESIGN (Chat 94)
-------------------------------------------------
The first implementation rendered with ``cmds.arnoldRender`` and then dumped the
Render View via ``renderWindowEditor(writeImage=…)``. That produces a
**scene-linear** 8-bit file: measured in-vivo with a flat ``surfaceShader``
patch of exactly 0.5, the PNG came back at **127** (0.5 written raw) no matter
what colour setting was used — output-transform name, output-transform on/off,
scene view transform, ``renderWindowEditor``'s own ``viewTransformName`` /
``outputColorManage``, the Arnold driver enum, or ``outputTarget="renderer"``.
A geometry change *did* alter the output, so the renders were live, not cached.

The same scene rendered by **Arnold writing the file itself** (``kick``) came
back at **188** — 0.5 sRGB-encoded, i.e. the output transform applied. Maya's
output transform and the driver's colour management only take effect on
Arnold's own write path; dumping the Render View bypasses both, and so does
``cmds.render()`` inside interactive Maya (also measured at 127 — the
``catcher-passes`` skill independently documents that ``cmds.render`` fails
here).

So this module no longer renders in Maya at all. It **exports a ``.ass``** with
the colour policy and the output path baked in, and ``server._do_render_still``
runs ``kick`` on it — the same pattern as the ``catcher-passes`` skill, whose
hard-won invocation (``-dw -dp``, ``imageFilePrefix`` baked in, never ``-o``)
is reused here. Side benefits: no Render View is ever opened (one less Qt
window on a macOS Tahoe box that crashes on ``QWidget::setVisible(false)``),
Maya's main thread only does a fast scene export, and the render itself happens
out-of-process where it cannot hang the UI.

Recipe:

1. Resolve the camera (explicit arg → focused modelPanel's camera → ``persp``).
2. Snapshot every attribute touched and restore ALL of it in ``finally`` — the
   user's scene is left exactly as found.
3. Bake the colour policy the way ``color_policy`` does it (validated in-vivo
   Chat 79/94): colour management on, global output transform on, the view set
   by name *only when Maya lists it*, and the Arnold driver's ``colorManagement``
   enum mapped BY NAME (indices vary by mtoa version) to ``Use Output
   Transform``. Whether the view took is reported in ``view_transform_applied``;
   it is never silently assumed.
   NB: ``colorManagementPrefs`` has NO "renderView" target — ``outputTarget``
   accepts only "renderer"/"playblast", and passing anything else RAISES. The
   first implementation passed ``outputTarget="renderView"`` behind a bare
   ``except``, which is how the whole transform became a silent no-op.
4. Point ``imageFilePrefix`` at ``out_path`` minus its extension with animation
   OFF, so Arnold writes exactly ``out_path`` and appends no frame padding, and
   set the driver translator from that extension.
5. Export the ``.ass`` for the requested frame and hand it back; the caller
   renders it with ``kick -i <ass> -as <AA> -r <W> <H> -dw -dp``.

All blocks are best-effort/guarded: a missing attribute or flag degrades to a
no-op rather than raising, and the ``finally`` restore always runs.
"""

import os

MTOA_PLUGIN = "mtoa"


def _engine_context():
    """Best-effort tk-maya context → (asset, task, version_code) or (None, …).

    ``version_code`` = ``{Asset}_{Task}`` so the review Version created from this
    still is named after the task it was generated in — same contract as
    review_build._engine_context (kept in sync). Silent/None when Maya is not
    engine'd (a plain, non-Toolkit Maya).
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


def _resolve_camera(cmds, camera):
    """Explicit camera → focused modelPanel's camera → 'persp'."""
    if camera and cmds.objExists(camera):
        return camera
    fp = cmds.getPanel(withFocus=True)
    if fp and cmds.getPanel(typeOf=fp) == "modelPanel":
        cam = cmds.modelPanel(fp, query=True, camera=True)
        if cam and cmds.objExists(cam):
            return cam
    return "persp"


def _kick_path(cmds):
    """Locate ``kick`` from the LOADED mtoa, never a hardcoded version.

    ``pluginInfo(path=True)`` gives e.g.
    ``/Applications/Autodesk/Arnold/mtoa/2027/plug-ins/mtoa.bundle`` → kick sits
    at ``../bin/kick``. Returns None when it cannot be resolved, so the caller
    reports a useful error instead of failing on a bogus argv[0].
    """
    try:
        plug = cmds.pluginInfo(MTOA_PLUGIN, query=True, path=True)
    except Exception:
        return None
    if not plug:
        return None
    root = os.path.dirname(os.path.dirname(plug))  # …/mtoa/<ver>
    cand = os.path.join(root, "bin", "kick")
    return cand if os.path.exists(cand) else None


def export_still_ass(out_path, ass_path, camera=None, frame=None,
                     view_transform="Un-tone-mapped (sRGB)"):
    """Bake the colour policy + output path into a ``.ass`` for ``kick``.

    Renders nothing itself — see the module docstring for why the render has to
    be Arnold's own write path. Returns a report dict; the caller runs kick.

    :param out_path: absolute output image path (``.png``/``.jpg``).
    :param ass_path: absolute path for the temporary ``.ass`` to export.
    :param camera: camera transform/shape; falls back to the focused panel / persp.
    :param frame: frame to export; defaults to the current time.
    :param view_transform: colour-management view baked into the 8-bit still.
    """
    import maya.cmds as cmds  # type: ignore[import-not-found]

    # tk-maya context so the caller can name the review Version after the task
    # ({Asset}_{Task}); None when Maya is not engine'd.
    _asset, _task, _version_code = _engine_context()
    report = {"ass": None, "error": None, "camera": None, "frame": None,
              "out_path": out_path, "view_transform": view_transform,
              "view_transform_applied": False, "kick": None,
              "asset": _asset, "task": _task, "version_code": _version_code}

    cam = _resolve_camera(cmds, camera)
    report["camera"] = cam

    # --- snapshot the state we mutate (restored in finally) ---
    def _get(attr, typ=None):
        try:
            return cmds.getAttr(attr, **({"type": typ} if typ else {}))
        except Exception:
            return None

    def _cmq(**kw):
        try:
            return cmds.colorManagementPrefs(query=True, **kw)
        except Exception:
            return None

    prev_renderer = _get("defaultRenderGlobals.currentRenderer")
    prev_prefix = _get("defaultRenderGlobals.imageFilePrefix")
    prev_anim = _get("defaultRenderGlobals.animation")
    prev_frame = cmds.currentTime(query=True)
    prev_ot_enabled = _cmq(outputTransformEnabled=True)
    prev_ot_name = _cmq(outputTransformName=True)

    # Arnold nodes do not exist until the plugin loads; initialised here so the
    # finally restore can always read them, even on an early failure.
    prev_translator = None
    prev_driver_cm = None

    try:
        if not cmds.pluginInfo(MTOA_PLUGIN, query=True, loaded=True):
            cmds.loadPlugin(MTOA_PLUGIN)

        # Arnold's default nodes (defaultArnoldDriver / RenderOptions / Filter)
        # are created LAZILY — loading the plugin is not enough (Chat 94: in a
        # Maya where nobody had opened the Arnold render settings yet, every
        # defaultArnoldDriver attribute below raised "No object matches name").
        # createOptions() is mtoa's own idempotent bootstrap for them.
        try:
            from mtoa.core import createOptions

            createOptions()
        except Exception:
            pass

        report["kick"] = _kick_path(cmds)
        prev_translator = _get("defaultArnoldDriver.aiTranslator")
        prev_driver_cm = _get("defaultArnoldDriver.colorManagement")

        if frame is not None:
            cmds.currentTime(float(frame))
        report["frame"] = cmds.currentTime(query=True)

        cmds.setAttr("defaultRenderGlobals.currentRenderer", "arnold", type="string")

        # 8-bit display-referred still → driver translator from the extension.
        fmt = "jpeg" if out_path.lower().endswith((".jpg", ".jpeg")) else "png"
        try:
            cmds.setAttr("defaultArnoldDriver.aiTranslator", fmt, type="string")
        except Exception:
            pass

        # Colour policy — the form validated in-vivo. See the module docstring
        # for why outputTarget must NOT be passed a "renderView" value.
        try:
            cmds.colorManagementPrefs(edit=True, cmEnabled=True)
            cmds.colorManagementPrefs(edit=True, outputTransformEnabled=True)
            if view_transform in (cmds.colorManagementPrefs(
                    query=True, outputTransformNames=True) or []):
                cmds.colorManagementPrefs(edit=True, outputTransformName=view_transform)
                report["view_transform_applied"] = True
            # The driver must be told to USE that output transform; the enum is
            # mapped BY NAME because its indices vary by mtoa version.
            enum = (cmds.attributeQuery("colorManagement", node="defaultArnoldDriver",
                                        listEnum=True) or [""])[0]
            opts = enum.split(":")
            if "Use Output Transform" in opts:
                cmds.setAttr("defaultArnoldDriver.colorManagement",
                             opts.index("Use Output Transform"))
        except Exception as exc:  # noqa: BLE001 — never raise out of the recipe
            report["view_transform_error"] = "%s: %s" % (type(exc).__name__, exc)

        # Arnold builds the output name from imageFilePrefix + the translator's
        # extension. Animation OFF → no frame padding, so the file lands on the
        # EXACT out_path the caller asked for.
        base, _ext = os.path.splitext(out_path)
        try:
            cmds.setAttr("defaultRenderGlobals.animation", 0)
        except Exception:
            pass
        cmds.setAttr("defaultRenderGlobals.imageFilePrefix", base, type="string")

        for folder in (os.path.dirname(out_path), os.path.dirname(ass_path)):
            if folder and not os.path.isdir(folder):
                os.makedirs(folder)

        # No mask / no sf-ef: those make mtoa write a frame-numbered .ass and
        # drop nodes the scene still references (the denoiser, Chat 94).
        cmds.arnoldExportAss(f=ass_path, cam=cam, lightLinks=1, shadowLinks=1)

        if os.path.exists(ass_path):
            report["ass"] = ass_path
        else:
            report["error"] = ("arnoldExportAss returned but no .ass was written "
                               "at ass_path.")
    except Exception as exc:  # noqa: BLE001 — never raise out of the recipe
        report["error"] = "%s: %s" % (type(exc).__name__, exc)
    finally:
        try:
            if prev_renderer:
                cmds.setAttr("defaultRenderGlobals.currentRenderer",
                             prev_renderer, type="string")
            if prev_prefix is not None:
                cmds.setAttr("defaultRenderGlobals.imageFilePrefix",
                             prev_prefix, type="string")
            if prev_anim is not None:
                cmds.setAttr("defaultRenderGlobals.animation", int(prev_anim))
            if prev_translator is not None:
                cmds.setAttr("defaultArnoldDriver.aiTranslator",
                             prev_translator, type="string")
            if prev_driver_cm is not None:
                cmds.setAttr("defaultArnoldDriver.colorManagement", int(prev_driver_cm))
            if prev_ot_name:
                cmds.colorManagementPrefs(edit=True, outputTransformName=prev_ot_name)
            if prev_ot_enabled is not None:
                cmds.colorManagementPrefs(edit=True,
                                          outputTransformEnabled=bool(prev_ot_enabled))
            cmds.currentTime(prev_frame)
        except Exception:
            pass

    return report
