# Workflow Guide: Reference Image → Hunyuan3D → Maya

> Complete step-by-step guide for running the 3D texturing pipeline with a reference image.

---

## Pipeline Overview

```
Reference image (.jpg/.png)
        │
        ▼
[ Remote GPU server ]
  Hunyuan3D-2 Paint (texture generation)
  Input:  mesh.glb + reference.jpg
  Output: mesh_uv.obj + texture_baked.png
        │
        ▼
[ Local Mac / Maya ]
  Import mesh_uv.obj (scaled to match scene)
  Apply texture_baked.png directly (UVs already correct)
  Fix ground position + smooth subdivision + soft normals
```

---

## What the GPU server generates

**Note:** Hunyuan3D-Paint performs **texturing**, not modeling. The input mesh geometry (`mesh.glb`) is not modified — only UVs and texture are generated.

| File | Source | Description |
|------|--------|-------------|
| `mesh.glb` | Pre-existing | Base geometry — no UVs, ~1 unit scale |
| `reference.jpg` | Your input | Photo or render used to guide texturing |
| `mesh_uv.obj` | **Generated** | Same geometry as mesh.glb + UV seams. More verts than mesh.glb due to UV splits |
| `texture_baked.png` | **Generated** | Baked texture derived from reference image |
| `textured.glb` | **Generated** | mesh_uv + texture_baked combined (preview) |

**Key insight:** If you re-run with a different reference image but the same `mesh.glb`, only `mesh_uv.obj` and `texture_baked.png` change.

---

## Step 1: Prepare files and run the pipeline

### Expected folder structure

```
maya-mcp/
├── reference/
│   ├── reference.jpg              ← your reference image
│   └── 3d_output/
│       └── 0/                     ← OUTPUT_SUBDIR (default: 0)
│           ├── input.png          ← copy of reference used as Hunyuan3D input
│           ├── mesh.glb           ← base geometry (pre-existing)
│           ├── mesh_uv.obj        ← OUTPUT: geometry + UVs
│           ├── texture_baked.png  ← OUTPUT: baked texture
│           └── textured.glb       ← OUTPUT: preview
```

### Run from Maya (exec mode — paint-only)

```python
# In Maya Script Editor → Python tab
import os
os.environ['PROJECT_DIR'] = '/path/to/maya-mcp'
os.environ['GPU_SSH_HOST'] = 'user@your-gpu-server'
exec(open(os.path.join(os.environ['PROJECT_DIR'], 'vision/pipeline_runner.py')).read())
```

### Run from terminal

```bash
source .env  # load environment variables

# Paint-only (existing mesh.glb → texture → outputs)
python vision/pipeline_runner.py --mode paint-only \
  --mesh reference/3d_output/0/mesh.glb \
  --image reference/reference.jpg

# Full (image → shape + texture → outputs)
python vision/pipeline_runner.py --mode full \
  --image reference/reference.jpg
```

### Verify outputs

Confirm the following files exist and are non-empty before proceeding to Maya:
- `reference/3d_output/0/mesh_uv.obj` — should have 40,000+ vertices
- `reference/3d_output/0/texture_baked.png` — should be > 500 KB
- `reference/3d_output/0/textured.glb` — preview of the result

---

## Step 2: Import into Maya

### Script: `vision/maya_import_hires.py`

This is the definitive import script. It handles scaling, positioning, material creation, and texture assignment automatically.

### Run in Maya Script Editor

1. Open Maya with your scene
2. Open **Windows → General Editors → Script Editor**
3. Select the **Python** tab
4. Click in the input area (bottom half)
5. Type:

```python
exec(open('/path/to/maya-mcp/vision/maya_import_hires.py').read())
```

6. Press **Ctrl+Enter** to execute

You can also use environment variables to avoid hardcoding paths:

```python
import os
exec(open(os.path.join(os.environ['PROJECT_DIR'], 'vision/maya_import_hires.py')).read())
```

### What the script does

1. Finds the original mesh (highest vertex count, excluding studio_floor / bg_wall / studioSky)
2. **Hides** it (does not delete — restore with `cmds.showHidden(orig_xf)`)
3. Imports `mesh_uv.obj` into the `hires:` namespace
4. Calculates scale factor: `orig_span / h_span` — matches imported mesh to original scene scale
5. Centers the imported mesh on the original position
6. Creates a `lambert` material with `texture_baked.png`
7. Applies `polySoftEdge(a=180)` for smooth normals
8. Enables textured display and fits the camera

### Expected output in Script Editor History

```
============================================================
IMPORT HIRES — mesh_uv.obj directo
============================================================
Mesh original: 'your_mesh'  v=XXXX  f=XXXX
  center: (X.XXX, Y.XXX, Z.XXX)
  size:   (X.XXX, Y.XXX, Z.XXX)
'your_mesh' ocultado

── Importando mesh_uv.obj ──
  Importado: 'hires:Mesh'  v=45974  f=64548

── Escalando y alineando ──
  Scale factor: X.XXXXx
  Alineado en: (X.XXX, Y.XXX, Z.XXX)

── Aplicando texture_baked.png (UVs ya correctos) ──
  Material aplicado a '|hires:Mesh'

============================================================
LISTO
============================================================
```

---

## Step 3: Post-import corrections

### 3a. Fix ground position + smooth subdivision

Run in Script Editor Python tab:

```python
exec(open('/path/to/maya-mcp/vision/maya_fix_position_smooth.py').read())
```

Or from MEL tab (if the Python tab has paste issues):

```mel
python("exec(open('/path/to/maya-mcp/vision/maya_fix_position_smooth.py').read())");
```

### What `maya_fix_position_smooth.py` does

1. **Finds** the hires mesh (highest vertex count, skipping studio objects)
2. **Deletes construction history** (bakes any existing polySmoothFace nodes)
3. **Fixes Y position**: translates mesh so bounding box min Y = 0 (exactly on ground)
4. **Applies smooth**: `polySmooth(divisions=1, method=0, continuity=1.0, smoothUVs=True, keepBorder=True, ch=False)`
   - `ch=False`: immediate bake, no history node left behind
5. **Soft normals**: `polySoftEdge(a=180, ch=False)`
6. Enables textured display and fits camera

### Expected output

```
Mesh encontrado: 'hires:Mesh'  v=45974
  BBox Y: min=-0.XXXX  max=17.XXXX
  Corregido: Y ahora min=0.0000  max=17.XXXX
Aplicando polySmooth (1 div)...
  Smooth OK: v=183XXX  f=258XXX
✓ Listo
```

### 3b. If smooth was already applied from the Maya menu

If you already applied `Mesh > Smooth` (which creates a `polySmoothFace1` history node), bake it first:

1. Select the hires mesh in the Outliner
2. Go to **Edit → Delete by Type → History**
3. Then run only the position correction:

```python
import maya.cmds as cmds
hm_xf = '|hires:Mesh'  # adjust to your mesh name
bb = cmds.exactWorldBoundingBox(hm_xf)
if bb[1] < -0.001:
    cmds.xform(hm_xf, translation=[0, -bb[1], 0], worldSpace=True, relative=True)
    print(f"Position corrected: min Y = {cmds.exactWorldBoundingBox(hm_xf)[1]:.4f}")
cmds.select(hm_xf)
cmds.polySoftEdge(a=180, ch=False)
cmds.select(clear=True)
for p in cmds.getPanel(type='modelPanel'):
    cmds.modelEditor(p, edit=True, displayTextures=True)
cmds.viewFit(all=True)
print("✓ Done")
```

---

## Step 4: Final verification

### Visual checklist in the viewport

- [ ] Texture is visible (textured mode active — press **6**)
- [ ] Character's feet are exactly on the ground plane (Y=0)
- [ ] Mesh looks smooth (no hard faceting)
- [ ] Normals are soft (no hard edges visible)
- [ ] Scale is consistent with scene environment objects

---

## Repeating with a different reference image

1. Replace the reference image in `reference/reference.jpg` (or add a new one)
2. On the GPU server: run the texturing pipeline with the new image
3. New `mesh_uv.obj` and `texture_baked.png` are saved to `reference/3d_output/0/` (or a new subdirectory set via `OUTPUT_SUBDIR`)
4. Override `OBJ_PATH` and `TEX_PATH` env vars if using a different output path
5. In Maya: execute `maya_import_hires.py` → `maya_fix_position_smooth.py`

> **Note:** If the base mesh (`mesh.glb`) is different, the scale factor will be recalculated automatically — the script computes it fresh on each run.

---

## Troubleshooting

### Python tab in Script Editor doesn't accept paste (Ctrl+V)

**Cause:** The Python input area may not have keyboard focus.
**Solution:** Use the MEL tab and wrap Python code:

```mel
python("exec(open('/path/to/project/vision/SCRIPT_NAME.py').read())");
```

### Character appears without texture (gray or black)

1. Press **6** in viewport to enable textured mode
2. Check `baked_tex_file` node's `fileTextureName` attribute for correct path
3. Confirm the mesh has `baked_tex_mat` assigned (check Attribute Editor)

### Character sinks below the ground plane

Run `maya_fix_position_smooth.py` — it will detect and correct the Y offset.

### Texture looks stretched or UV-misaligned

This should not occur because `mesh_uv.obj` and `texture_baked.png` are generated together with consistent UVs. If it does, check whether the mesh was non-uniformly scaled or rotated after import. Use **Modify → Freeze Transformations** on the original mesh and re-run.

### `mesh_uv.obj` import fails ("Import failed")

1. Confirm the file exists and is non-empty
2. Ensure the OBJ plugin is loaded: **Windows → Settings/Preferences → Plug-in Manager → objExport.bundle → Loaded**

### Scale factor wrong (mesh is giant or tiny)

Before running the pipeline, select the original mesh in Maya and run **Modify → Freeze Transformations** to bake scale to identity. Then re-import.

---

## Technical Reference

### Maya material nodes created by `maya_import_hires.py`

```
baked_tex_mat     → lambert shader
baked_tex_mat_SG  → shading group
baked_tex_file    → file node → texture_baked.png
baked_tex_p2d     → place2dTexture → UV placement
```

### Scale factor formula

```
scale_factor = max(orig_sx, orig_sy, orig_sz) / max(h_sx, h_sy, h_sz)
```

Where `orig_s*` is the bounding box size of the original scene mesh, and `h_s*` is the bounding box size of the imported `mesh_uv.obj`.

### Typical mesh statistics

| Stage | Vertices | Faces |
|-------|----------|-------|
| `mesh.glb` (input geometry) | ~32,000 | ~64,000 |
| `mesh_uv.obj` (after UV unwrap) | ~46,000 | ~64,000 |
| After `polySmooth` (1 division) | ~184,000 | ~258,000 |
