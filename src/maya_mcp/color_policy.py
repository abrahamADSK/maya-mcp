"""
color_policy.py
===============
Single source of truth for maya-mcp's **review/preview colour management**.

Background (Chat 79): the Maya projects in this pipeline inherit Maya's
**built-in default OCIO config** — nothing sets an ``OCIO`` env var,
``colorManagementPrefs``, or an ACES config anywhere in maya-mcp, the fpt-mcp
launcher, or the Toolkit config. Its default view transform is
``"Un-tone-mapped (sRGB)"`` over a scene-linear Rec.709/sRGB working space.

Two DISTINCT knobs decide whether a preview matches the viewport — do not
conflate them:

* **VP2.0 playblasts** (``review_turntable``, ``maya_viewport_capture``) bake
  whatever **view transform** Maya's colour management is displaying. If colour
  management is off, or set to a different view, the capture comes out dark or
  mismatched. → :func:`view_transform_apply_code` pins it (best-effort) and
  :func:`view_transform_restore_code` puts it back. The Arnold driver setting
  does NOT affect these — they never pass through an Arnold driver.

* **Arnold file renders** bake the **output transform** the driver is told to
  apply. A *preview* (8-bit, display-referred PNG/JPG) MUST apply the output
  transform; a *production EXR* (scene-linear, scene-referred) MUST NOT — baking
  a display transform into an EXR destroys the linear data the comp/Flame stage
  needs. → :func:`arnold_output_transform_code` enables it for ``preview`` and
  force-DISABLES it for ``exr`` (the guardrail). The single shared
  ``defaultArnoldDriver`` is exactly why the guardrail matters: one driver feeds
  both preview and EXR output.

Every block this module emits is **best-effort and guarded**: on a Maya version
or OCIO config where a flag is absent it degrades to a no-op (the pre-Chat-79
behaviour), never raising. The Arnold driver's output-transform attribute name
is DISCOVERED at runtime via ``listAttr`` rather than hardcoded, so nothing here
depends on an unverified attribute name (the exact name was UNCONFIRMED in the
repo as of Chat 79 — it must be confirmed in a live mtoa Maya with
``cmds.listAttr('defaultArnoldDriver', string='*ransform*')`` to ever replace
discovery with a direct set).
"""

from __future__ import annotations

# Maya built-in default-config review view (Chat 79). Override per project via
# config.json -> "review_view_transform" if the project ever moves to ACES
# (then this would be e.g. "sRGB (ACES)" / "ACES 1.0 SDR-video"). This constant
# is the canonical default; review_build.py hardcodes the same literal because
# it ships standalone over the Command Port and cannot import this module inside
# Maya — tests/test_color_policy.py pins the two together so they cannot drift.
DEFAULT_REVIEW_VIEW = "Un-tone-mapped (sRGB)"


def view_transform_apply_code(
    view_transform: str = DEFAULT_REVIEW_VIEW,
    prev_var: str = "_mcp_cp_prev_view",
) -> str:
    """Code string: enable colour management and pin ``view_transform`` (best-effort).

    Captures the previous view transform into ``prev_var`` so it can be restored
    with :func:`view_transform_restore_code`. ``prev_var`` stays ``None`` (a
    clean no-op for the restore) when colour management is unavailable or the
    view name is not offered by the active OCIO config.
    """
    return f"""
{prev_var} = None
try:
    import maya.cmds as _mcp_cp_cmds
    _mcp_cp_cmds.colorManagementPrefs(edit=True, cmEnabled=True)
    _mcp_cp_views = _mcp_cp_cmds.colorManagementPrefs(query=True, viewTransformNames=True) or []
    if {view_transform!r} in _mcp_cp_views:
        {prev_var} = _mcp_cp_cmds.colorManagementPrefs(query=True, viewTransformName=True)
        if {prev_var} != {view_transform!r}:
            _mcp_cp_cmds.colorManagementPrefs(edit=True, viewTransformName={view_transform!r})
except Exception:
    {prev_var} = None
"""


def view_transform_restore_code(prev_var: str = "_mcp_cp_prev_view") -> str:
    """Code string: restore the view transform captured by :func:`view_transform_apply_code`.

    No-op when ``prev_var`` is falsy (nothing was changed). Place inside the
    caller's ``finally`` so the user's colour-management state is left as found.
    """
    return f"""
try:
    import maya.cmds as _mcp_cp_cmds
    if {prev_var}:
        _mcp_cp_cmds.colorManagementPrefs(edit=True, viewTransformName={prev_var})
except Exception:
    pass
"""


def arnold_output_transform_code(
    mode: str = "preview",
    view_transform: str = DEFAULT_REVIEW_VIEW,
    driver: str = "defaultArnoldDriver",
) -> str:
    """Code string: gate the Arnold **output transform** by output kind.

    ``mode="preview"`` (display-referred 8-bit PNG/JPG): enable Maya's output
    transform set to ``view_transform`` and, if the driver exposes an
    output-transform toggle, switch it on so the file matches the viewport.

    ``mode="exr"`` / ``"linear"`` (scene-linear EXR): force the output transform
    OFF — the guardrail that stops a display transform being baked into the EXR
    the comp/Flame stage consumes. EVERY non-preview mode disables it.

    The driver's output-transform attribute name is DISCOVERED at runtime
    (``listAttr`` for ``*ransform*``, then the boolean one is the toggle) instead
    of hardcoded, so this never depends on an unverified attribute name.
    Best-effort; never raises. Assigns a small report dict to ``result``.
    """
    enable = "True" if mode == "preview" else "False"
    return f"""
_mcp_cp_report = {{"mode": {mode!r}, "global_output_transform": None, "driver_attr": None}}
try:
    import maya.cmds as _mcp_cp_cmds
    # 1) Maya GLOBAL output transform (the "Apply Output Transform" knob).
    try:
        _mcp_cp_cmds.colorManagementPrefs(edit=True, outputTransformEnabled={enable})
        if {enable} and {view_transform!r} in (
                _mcp_cp_cmds.colorManagementPrefs(query=True, outputTransformNames=True) or []):
            _mcp_cp_cmds.colorManagementPrefs(edit=True, outputTransformName={view_transform!r})
        _mcp_cp_report["global_output_transform"] = {enable}
    except Exception:
        pass
    # 2) Point the Arnold driver at the output transform. DISCOVER the attribute
    #    name (never hardcode it): a boolean *transform* attr on the driver is the
    #    "use output transform" toggle. Setting it False on EXR is the guardrail.
    if _mcp_cp_cmds.objExists({driver!r}):
        for _mcp_cp_a in (_mcp_cp_cmds.listAttr({driver!r}, string='*ransform*') or []):
            try:
                _mcp_cp_full = {driver!r} + '.' + _mcp_cp_a
                if _mcp_cp_cmds.getAttr(_mcp_cp_full, type=True) == 'bool':
                    _mcp_cp_cmds.setAttr(_mcp_cp_full, {enable})
                    _mcp_cp_report["driver_attr"] = _mcp_cp_a
                    break
            except Exception:
                pass
except Exception:
    pass
result = _mcp_cp_report
"""
