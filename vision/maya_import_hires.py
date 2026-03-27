"""
maya_import_hires.py — Importa mesh_uv.obj escalado al tamaño del mesh original.
Usa la textura baked generada por texture_remote.py (no necesita UV transfer).

Ejecutar en Maya Script Editor (Python tab) via:
  exec(open('/path/to/project/vision/maya_import_hires.py').read())

O configurar PROJECT_DIR como variable de entorno antes de abrir Maya.

Este script:
  1. Oculta (no borra) el mesh original en escena
  2. Importa mesh_uv.obj escalado para igualar el tamaño del original
  3. Lo posiciona en el mismo lugar
  4. Aplica texture_baked.png directamente (los UVs ya son correctos)
  5. Suaviza normales
"""

import maya.cmds as cmds
import os

# Resolver rutas: PROJECT_DIR env var, o autodetección relativa a este script
_project_dir = os.environ.get('PROJECT_DIR', '')
if not _project_dir:
    # Cuando se ejecuta via exec() en Maya, __file__ no está disponible.
    # Intentar buscar el proyecto por nombre de directorio conocido.
    _candidates = [
        os.path.expanduser('~/Documents/maya-mcp-server'),
        os.path.expanduser('~/maya-mcp-server'),
        os.path.expanduser('~/projects/maya-mcp-server'),
    ]
    for _c in _candidates:
        if os.path.isdir(_c):
            _project_dir = _c
            break

_output_subdir = os.environ.get('OUTPUT_SUBDIR', '0')
_output_dir    = os.path.join(_project_dir, 'reference', '3d_output', _output_subdir)

OBJ_PATH = os.environ.get('OBJ_PATH', os.path.join(_output_dir, 'mesh_uv.obj'))
TEX_PATH = os.environ.get('TEX_PATH', os.path.join(_output_dir, 'texture_baked.png'))
SKIP = {'studio_floor', 'bg_wall', 'studioSky'}

def bbox(xf):
    bb = cmds.exactWorldBoundingBox(xf)
    cx = (bb[0]+bb[3])/2; cy = (bb[1]+bb[4])/2; cz = (bb[2]+bb[5])/2
    sx = bb[3]-bb[0];     sy = bb[4]-bb[1];     sz = bb[5]-bb[2]
    return cx, cy, cz, sx, sy, sz

def get_char_meshes():
    result = []
    for m in cmds.ls(type='mesh', long=True):
        xf_list = cmds.listRelatives(m, parent=True, fullPath=True) or []
        if not xf_list:
            continue
        name = xf_list[0].split('|')[-1]
        if name in SKIP:
            continue
        vc = cmds.polyEvaluate(m, vertex=True)
        fc = cmds.polyEvaluate(m, face=True)
        result.append((vc, fc, name, xf_list[0]))
    return sorted(result, reverse=True)

print("=" * 60)
print("IMPORT HIRES — mesh_uv.obj directo")
print("=" * 60)

for p in [OBJ_PATH, TEX_PATH]:
    if not os.path.exists(p):
        raise FileNotFoundError(p)

# Encontrar el mesh original
meshes = get_char_meshes()
if not meshes:
    raise RuntimeError("No character meshes found")
orig_vc, orig_fc, orig_name, orig_xf = meshes[0]
orig_cx, orig_cy, orig_cz, orig_sx, orig_sy, orig_sz = bbox(orig_xf)
print(f"Mesh original: '{orig_name}'  v={orig_vc}  f={orig_fc}")
print(f"  center: ({orig_cx:.3f}, {orig_cy:.3f}, {orig_cz:.3f})")
print(f"  size:   ({orig_sx:.3f}, {orig_sy:.3f}, {orig_sz:.3f})")

# Ocultar (no borrar) el mesh original
cmds.hide(orig_xf)
print(f"\n'{orig_name}' ocultado (no borrado — usa cmds.showHidden(orig_xf) para recuperarlo)")

# ── IMPORTAR mesh_uv.obj ──────────────────────────────────────
print("\n── Importando mesh_uv.obj ──")
before = set(cmds.ls(long=True))
cmds.file(OBJ_PATH, i=True, type='OBJ', ignoreVersion=True,
          mergeNamespacesOnClash=False, namespace='hires',
          options='mo=1;lo=0', pr=True)
after = set(cmds.ls(long=True))

hires_xf = None
hires_mesh = None
for n in (after - before):
    if cmds.objectType(n) == 'mesh':
        xf_list = cmds.listRelatives(n, parent=True, fullPath=True)
        if xf_list:
            hires_xf = xf_list[0]
            hires_mesh = n
            h_vc = cmds.polyEvaluate(n, vertex=True)
            h_fc = cmds.polyEvaluate(n, face=True)
            break

if not hires_xf:
    raise RuntimeError("Import failed")

h_cx, h_cy, h_cz, h_sx, h_sy, h_sz = bbox(hires_xf)
print(f"  Importado: '{hires_xf.split('|')[-1]}'  v={h_vc}  f={h_fc}")
print(f"  center: ({h_cx:.3f}, {h_cy:.3f}, {h_cz:.3f})")
print(f"  size:   ({h_sx:.3f}, {h_sy:.3f}, {h_sz:.3f})")

# ── ESCALAR Y ALINEAR ─────────────────────────────────────────
print("\n── Escalando y alineando ──")
orig_span = max(orig_sx, orig_sy, orig_sz)
h_span    = max(h_sx, h_sy, h_sz)
scale_factor = orig_span / h_span
print(f"  Scale factor: {scale_factor:.4f}x")

cmds.xform(hires_xf, scale=[scale_factor, scale_factor, scale_factor])
cmds.makeIdentity(hires_xf, apply=True, scale=True)

# Recentrar después de escalar
h_cx2, h_cy2, h_cz2 = bbox(hires_xf)[:3]
dx = orig_cx - h_cx2
dy = orig_cy - h_cy2
dz = orig_cz - h_cz2
cmds.xform(hires_xf, translation=[dx, dy, dz], worldSpace=True, relative=True)

h_cx3, h_cy3, h_cz3, h_sx3, h_sy3, h_sz3 = bbox(hires_xf)
print(f"  Alineado en: ({h_cx3:.3f}, {h_cy3:.3f}, {h_cz3:.3f})")
print(f"  Tamaño final: ({h_sx3:.3f}, {h_sy3:.3f}, {h_sz3:.3f})")

# ── APLICAR TEXTURA DIRECTAMENTE ─────────────────────────────
print("\n── Aplicando texture_baked.png (UVs ya correctos) ──")

for node in ['baked_tex_mat', 'baked_tex_mat_SG', 'baked_tex_file', 'baked_tex_p2d']:
    if cmds.objExists(node):
        cmds.delete(node)

mat = cmds.shadingNode('lambert', asShader=True, name='baked_tex_mat')
sg  = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name='baked_tex_mat_SG')
cmds.connectAttr(f'{mat}.outColor', f'{sg}.surfaceShader', force=True)

fn  = cmds.shadingNode('file', asTexture=True, name='baked_tex_file')
cmds.setAttr(f'{fn}.fileTextureName', TEX_PATH, type='string')
cmds.connectAttr(f'{fn}.outColor', f'{mat}.color', force=True)

p2d = cmds.shadingNode('place2dTexture', asUtility=True, name='baked_tex_p2d')
cmds.connectAttr(f'{p2d}.outUV', f'{fn}.uvCoord', force=True)
cmds.connectAttr(f'{p2d}.outUvFilterSize', f'{fn}.uvFilterSize', force=True)

cmds.select(hires_xf)
cmds.sets(edit=True, forceElement=sg)
cmds.select(clear=True)
print(f"  Material aplicado a '{hires_xf.split('|')[-1]}'")

# Normales suaves
cmds.select(hires_xf)
cmds.polySoftEdge(a=180, ch=False)
cmds.select(clear=True)
print("  polySoftEdge(180)")

# ── BORRAR MATERIALES HUÉRFANOS DEL OBJ ──────────────────────
for n in (after - before):
    if cmds.objExists(n) and cmds.objectType(n) in ['lambert','shadingEngine','blinn','phong','materialInfo']:
        if n not in ['baked_tex_mat', 'baked_tex_mat_SG']:
            try:
                cmds.delete(n)
            except:
                pass

# ── VISTA TEXTURADA ───────────────────────────────────────────
for panel in cmds.getPanel(type='modelPanel'):
    cmds.modelEditor(panel, edit=True, displayTextures=True)
cmds.viewFit(all=True)

# ── RESULTADO ─────────────────────────────────────────────────
print("\n" + "=" * 60)
print("LISTO")
print("=" * 60)
print(f"  Mesh hires importado y texturizado: '{hires_xf.split('|')[-1]}'")
print(f"  Mesh original '{orig_name}' está oculto (Layer > Show para recuperarlo)")
print(f"  Textura: {TEX_PATH}")
print("\n  Para restaurar el original:")
print(f"    import maya.cmds as cmds; cmds.showHidden('{orig_xf}')")
