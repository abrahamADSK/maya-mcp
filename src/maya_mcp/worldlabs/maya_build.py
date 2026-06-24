"""
maya_build.py
=============
In-Maya scene-build recipe for a World Labs Gaussian-splat environment, codified
from the Chat 71 in-vivo validation against ``street_environment.ply`` (Maya 2027
+ MtoA 5.6.2 / Arnold 7.5.2).

These functions run **inside Maya** (they use ``maya.cmds`` / the OpenMaya API).
maya-mcp's server runs *outside* Maya, so the dispatcher ships this module's
source to Maya over the Command Port and then calls the orchestrator. All Maya
imports are therefore done **inside** the functions, so this file imports cleanly
in the server process (e.g. for source extraction or syntax checks).

Validated recipe (the WorldLabs "load environment" invariant):

1. ``import_gaussian_splat`` — an ``aiGaussianSplat`` node pointing at the PLY
   (``filename`` + ``useFile`` + ``drawMode``). Renders in Arnold; in plain
   VP2.0 it draws only a bounding box (the splat/point draw modes need the
   Arnold viewport renderer).
2. ``build_point_proxy`` — a native Maya ``particle`` point cloud coloured from
   the splats' SH-DC term (``rgb = 0.28209479*f_dc + 0.5``), render type Points,
   **render-excluded** (``primaryVisibility`` + Arnold visibilities off) so it is
   a VP2.0-only navigation aid that never appears in the render.
3. ``assign_splat_shader`` — an ``aiGaussianSplatShader`` via a shading group.
   ``emissionWeight`` = the captured (e.g. night) look; ``diffuseWeight`` = the
   relightable component (kept 0 by default to preserve the captured look).
4. ``place_eye_camera`` — camera at the splats' horizontal centroid, at
   ``ground + eye_height`` where ``ground`` is a low Y-percentile (robust to
   stray splats below the floor), aimed along the dominant horizontal axis.
   NB: World Labs worlds are not guaranteed metric; ``eye_height`` is in world
   units (≈ metres for the validated street).
5. ``setup_dome_from_pano`` — an ``aiSkyDomeLight`` driven by the LDR panorama
   with a "fake-HDR" highlight squeeze, for image-based lighting of CG/characters
   added to the world. Light-linked to EXCLUDE the splat so it never washes the
   emission look (the night-scene lesson).

GOTCHA baked in (memory ``feedback_maya_gs_arnold_ipr_hang``): never leave the
Arnold viewport IPR running on the full splat — it saturates Maya's main thread.
Interactive work uses VP2.0 + the proxy; Arnold is for one-shot renders only.
"""

from __future__ import annotations

# SH degree-0 -> RGB constant (INRIA 3DGS convention).
SH_C0 = 0.28209479177387814


def _read_ply(ply_path):
    """Parse a binary-little-endian 3DGS PLY → (nverts, props, numpy array).

    Returns the full per-vertex float32 array shaped (nverts, nprop). Assumes all
    properties are float32 (the World Labs / INRIA splat layout); validated:
    17 props × 4 bytes = 68 B/vertex, SH degree 0 (x,y,z,nx,ny,nz,f_dc_0..2,
    opacity,scale_0..2,rot_0..3).
    """
    import numpy as np

    with open(ply_path, "rb") as fh:
        nverts = 0
        props = []
        while True:
            line = fh.readline().decode("ascii", "ignore").strip()
            if line.startswith("element vertex"):
                nverts = int(line.split()[-1])
            elif line.startswith("property"):
                props.append(line.split()[-1])
            elif line == "end_header":
                break
        nprop = len(props)
        blob = fh.read(nverts * nprop * 4)
    arr = np.frombuffer(blob, dtype=np.float32).reshape(nverts, nprop)
    return nverts, props, arr


def import_gaussian_splat(ply_path, name="worldSplat", draw_mode=0):
    """Create an ``aiGaussianSplat`` reading ``ply_path``. Renders in Arnold.

    ``draw_mode``: 0 Bounding Box (VP2.0-light default), 1 Point Cloud,
    2 Gaussian Splat (1 & 2 only draw under the Arnold viewport renderer).
    """
    import maya.cmds as cmds

    shape = cmds.createNode("aiGaussianSplat")
    par = cmds.listRelatives(shape, parent=True)
    trans = cmds.rename(par[0], name) if par else shape
    shape = cmds.listRelatives(trans, shapes=True)[0]
    cmds.setAttr(shape + ".filename", str(ply_path), type="string")
    cmds.setAttr(shape + ".useFile", 1)
    cmds.setAttr(shape + ".drawMode", int(draw_mode))
    bb = cmds.exactWorldBoundingBox(trans)
    return {"transform": trans, "shape": shape, "bbox": bb}


def build_point_proxy(ply_path, name="worldSplatPoints", step=1, point_size=2):
    """Native Maya coloured point cloud (VP2.0 nav proxy, render-excluded).

    ``step`` decimates (1 = every point). Colours come from the SH-DC term.
    Uses ``MFnParticleSystem.emit`` (bulk, fast) — NOT ``cmds.particle`` with a
    huge ``p=`` list, which is pathologically slow at ~500k points.
    """
    import maya.cmds as cmds
    import maya.OpenMaya as om
    import maya.OpenMayaFX as omfx
    import numpy as np

    _, props, arr = _read_ply(ply_path)
    idc = props.index("f_dc_0")
    xyz = arr[::step, 0:3]
    rgb = np.clip(SH_C0 * arr[::step, idc:idc + 3] + 0.5, 0.0, 1.0)
    n = int(xyz.shape[0])

    pts = om.MPointArray()
    cols = om.MVectorArray()
    pts.setLength(n)
    cols.setLength(n)
    xl = xyz.tolist()
    cl = rgb.tolist()
    for i in range(n):
        a = xl[i]
        c = cl[i]
        pts.set(om.MPoint(a[0], a[1], a[2]), i)
        cols.set(om.MVector(c[0], c[1], c[2]), i)

    fn = omfx.MFnParticleSystem()
    # create() follows MFnDagNode convention: returns the TRANSFORM MObject.
    # Resolve the shape child explicitly so fn is bound to the right object.
    tr_obj = fn.create()
    dagFn = om.MFnDagNode(tr_obj)
    shape_obj = dagFn.child(0) if dagFn.childCount() else tr_obj
    fn.setObject(shape_obj)
    shp = om.MFnDagNode(shape_obj).partialPathName()
    if not cmds.attributeQuery("rgbPP", node=shp, exists=True):
        cmds.addAttr(shp, ln="rgbPP", dt="vectorArray")
        cmds.addAttr(shp, ln="rgbPP0", dt="vectorArray")
    fn.emit(pts)
    fn.setPerParticleAttribute("rgbPP", cols)
    fn.saveInitialState()

    trans = cmds.rename(cmds.listRelatives(shp, parent=True)[0], name)
    shp = cmds.listRelatives(trans, shapes=True)[0]
    cmds.setAttr(shp + ".particleRenderType", 3)  # Points
    if not cmds.attributeQuery("pointSize", node=shp, exists=True):
        cmds.addAttr(shp, ln="pointSize", at="long", min=1, max=60, dv=point_size)
    cmds.setAttr(shp + ".pointSize", int(point_size))

    # Viewport-only: never render the proxy (camera + all Arnold ray visibility).
    for a in ("primaryVisibility", "castsShadows", "receiveShadows",
              "visibleInReflections", "visibleInRefractions", "motionBlur",
              "aiVisibleInDiffuseReflection", "aiVisibleInSpecularReflection",
              "aiVisibleInDiffuseTransmission", "aiVisibleInSpecularTransmission",
              "aiVisibleInVolume", "aiSelfShadows"):
        if cmds.attributeQuery(a, node=shp, exists=True):
            cmds.setAttr(shp + "." + a, 0)
    return {"transform": trans, "shape": shp, "count": n}


def assign_splat_shader(splat_shape, shader_name="worldSplatShader",
                        emission=1.0, diffuse=0.0):
    """Assign an ``aiGaussianSplatShader`` to the splat via a shading group.

    Default emission=1 / diffuse=0 preserves the captured look (for a night
    scene the look lives in emission). Raise ``diffuse`` (and add lights) to make
    the splat react to scene lighting.
    """
    import maya.cmds as cmds

    sh = cmds.shadingNode("aiGaussianSplatShader", asShader=True, name=shader_name)
    sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=sh + "SG")
    cmds.connectAttr(sh + ".outColor", sg + ".surfaceShader", force=True)
    cmds.sets(splat_shape, e=True, forceElement=sg)
    cmds.setAttr(sh + ".emissionWeight", float(emission))
    cmds.setAttr(sh + ".diffuseWeight", float(diffuse))
    return {"shader": sh, "sg": sg}


def place_eye_camera(ply_path, name="worldCamEye", eye_height=1.5,
                     focal=28.0, ground_pct=2.0):
    """Camera at the splats' horizontal centroid, ``ground + eye_height`` high,
    aimed horizontally along the dominant axis (the street).

    ``ground`` = the ``ground_pct`` percentile of Y (robust to stray low splats).
    ``eye_height`` is in WORLD UNITS (≈ metres for a metric-scaled world).
    """
    import math
    import maya.cmds as cmds
    import numpy as np

    _, _, arr = _read_ply(ply_path)
    X, Y, Z = arr[:, 0], arr[:, 1], arr[:, 2]
    ground = float(np.percentile(Y, ground_pct))
    cx, cz = float(np.median(X)), float(np.median(Z))
    eye = ground + float(eye_height)
    x2, x98 = float(np.percentile(X, 2)), float(np.percentile(X, 98))
    z2, z98 = float(np.percentile(Z, 2)), float(np.percentile(Z, 98))
    xr, zr = x98 - x2, z98 - z2
    if zr >= xr:
        far = z98 if (z98 - cz) > (cz - z2) else z2
        tx, tz = cx, far
    else:
        far = x98 if (x98 - cx) > (cx - x2) else x2
        tx, tz = far, cz

    if cmds.objExists(name):
        cmds.delete(name)
    cam, _shape = cmds.camera(name=name)
    cam = cmds.rename(cam, name)
    cam_shape = cmds.listRelatives(cam, shapes=True)[0]
    cmds.setAttr(cam + ".translate", cx, eye, cz, type="double3")
    ry = math.degrees(math.atan2(-(tx - cx), -(tz - cz)))
    cmds.setAttr(cam + ".rotate", 0.0, ry, 0.0, type="double3")
    cmds.setAttr(cam_shape + ".focalLength", float(focal))
    cmds.setAttr(cam_shape + ".farClipPlane", 100000.0)
    cmds.setAttr(cam_shape + ".nearClipPlane", 0.1)
    return {
        "camera": cam, "shape": cam_shape,
        "ground": round(ground, 3), "eye": round(eye, 3),
        "centroid_xz": [round(cx, 3), round(cz, 3)],
        "street_axis": "Z" if zr >= xr else "X", "rotateY": round(ry, 2),
    }


def setup_dome_from_pano(pano_path, name="envDome", gain=1.0,
                         fake_hdr_gamma=0.45, exclude_shapes=None):
    """Skydome IBL from the LDR panorama, with a fake-HDR highlight squeeze.

    The Marble panorama is LDR PNG; ``aiColorCorrect`` with ``gamma < 1`` expands
    the bright pixels into a pseudo-HDR so the dome gives directional-ish IBL for
    CG/characters. Light-linked to EXCLUDE ``exclude_shapes`` (the splat) so it
    never washes the emission look.
    """
    import maya.cmds as cmds

    # shadingNode(asLight=True) returns the TRANSFORM, not the shape.
    # Resolve the shape explicitly so color/intensity attrs are reachable.
    dome_trans = cmds.shadingNode("aiSkyDomeLight", asLight=True, name=name + "Shape")
    dome = cmds.listRelatives(dome_trans, shapes=True)[0]
    file_node = cmds.shadingNode("file", asTexture=True, name=name + "Pano")
    cmds.setAttr(file_node + ".fileTextureName", str(pano_path), type="string")
    cc = cmds.shadingNode("aiColorCorrect", asUtility=True, name=name + "FakeHDR")
    cmds.setAttr(cc + ".gamma", float(fake_hdr_gamma))  # < 1 expands highlights
    cmds.connectAttr(file_node + ".outColor", cc + ".input", force=True)
    cmds.connectAttr(cc + ".outColor", dome + ".color", force=True)
    cmds.setAttr(dome + ".intensity", float(gain))
    # dome_trans already in hand — no listRelatives(parent=True) needed.
    cmds.setAttr(dome + ".camera", 0)  # not directly visible to render camera
    if exclude_shapes:
        for s in exclude_shapes:
            try:
                cmds.lightlink(b=True, light=dome_trans, object=s)
            except Exception:
                pass
    return {"dome": dome, "dome_transform": dome_trans, "file": file_node, "color_correct": cc}


def build_environment(ply_path, pano_path=None, eye_height=1.5, proxy_step=1,
                      relight=False):
    """Orchestrate the full WorldLabs environment load (the invariant recipe).

    splat (Arnold) + coloured point proxy (VP2.0) + emission shader + eye camera,
    and — if ``pano_path`` is given — a fake-HDR dome light-linked to exclude the
    splat. ``relight=True`` raises the splat diffuse so it also reacts to lights.
    Returns a summary dict of everything created.
    """
    out = {}
    splat = import_gaussian_splat(ply_path)
    out["splat"] = splat
    out["proxy"] = build_point_proxy(ply_path, step=proxy_step)
    out["shader"] = assign_splat_shader(
        splat["shape"], emission=1.0, diffuse=(1.0 if relight else 0.0)
    )
    out["camera"] = place_eye_camera(ply_path, eye_height=eye_height)
    if pano_path:
        out["dome"] = setup_dome_from_pano(
            pano_path, exclude_shapes=[splat["shape"]]
        )
    return out
