"""
maya_fix_position_smooth.py — Corrige posición Y y aplica smooth subdivision.

Ejecutar en Maya Script Editor (Python tab):
  exec(open('/path/to/project/vision/maya_fix_position_smooth.py').read())

Seguro re-ejecutar: borra historia existente (polySmoothFace1, etc.) antes de aplicar smooth.
"""
import maya.cmds as cmds

SKIP = {'studio_floor', 'bg_wall', 'studioSky'}

# Encontrar el mesh hires (el que tiene más verts, que es el importado)
hires_xf = None
best_vc = 0
for m in cmds.ls(type='mesh', long=True):
    xf_list = cmds.listRelatives(m, parent=True, fullPath=True) or []
    if not xf_list: continue
    name = xf_list[0].split('|')[-1]
    if name in SKIP: continue
    vc = cmds.polyEvaluate(m, vertex=True)
    if vc > best_vc:
        best_vc = vc
        hires_xf = xf_list[0]

if not hires_xf:
    raise RuntimeError("No se encontró mesh hires")

print(f"Mesh encontrado: '{hires_xf.split('|')[-1]}'  v={best_vc}")

# ── 0. BORRAR HISTORIA EXISTENTE (bake polySmoothFace1 si existe) ──
try:
    cmds.delete(hires_xf, constructionHistory=True)
    print("  Historia borrada (polySmoothFace1 bakeado si existía)")
except Exception as e:
    print(f"  Advertencia al borrar historia: {e}")

# Recalcular vert count después de borrar historia (puede haber cambiado si había smooth)
hires_shape = cmds.listRelatives(hires_xf, shapes=True, type='mesh') or []
if hires_shape:
    best_vc = cmds.polyEvaluate(hires_shape[0], vertex=True)
    print(f"  Verts tras bake: {best_vc}")

# ── 1. CORREGIR POSICIÓN: base exactamente en Y=0 (suelo) ──
bb = cmds.exactWorldBoundingBox(hires_xf)
min_y = bb[1]
print(f"  BBox Y: min={min_y:.4f}  max={bb[4]:.4f}")

if abs(min_y) > 0.001:
    cmds.xform(hires_xf, translation=[0, -min_y, 0], worldSpace=True, relative=True)
    bb2 = cmds.exactWorldBoundingBox(hires_xf)
    print(f"  Corregido: Y ahora min={bb2[1]:.4f}  max={bb2[4]:.4f}")
else:
    print(f"  Posición Y ya correcta")

# ── 2. SMOOTH SUBDIVISION (1 nivel) — ch=False = bake inmediato ──
print("Aplicando polySmooth (1 div)...")
cmds.select(hires_xf)
cmds.polySmooth(
    hires_xf, method=0, divisions=1, continuity=1.0,
    smoothUVs=True, keepBorder=True, keepHardEdge=False,
    propagateEdgeHardness=False, keepMapBorders=1, ch=False
)
cmds.select(clear=True)
vc2 = cmds.polyEvaluate(hires_xf, vertex=True)
fc2 = cmds.polyEvaluate(hires_xf, face=True)
print(f"  Smooth OK: v={vc2}  f={fc2}")

cmds.select(hires_xf)
cmds.polySoftEdge(a=180, ch=False)
cmds.select(clear=True)

# ── 3. ACTIVAR TEXTURED + FIT ──
for panel in cmds.getPanel(type='modelPanel'):
    cmds.modelEditor(panel, edit=True, displayTextures=True)
cmds.viewFit(all=True)

print("✓ Listo")
