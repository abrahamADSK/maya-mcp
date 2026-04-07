# Maya-USD API Reference

Integration between Autodesk Maya and Universal Scene Description (USD).
Covers import/export, the mayaUsd plugin, and pxr Python API within Maya.

## Plugin Loading

```python
import maya.cmds as cmds
cmds.loadPlugin('mayaUsdPlugin', quiet=True)
# Verify: cmds.pluginInfo('mayaUsdPlugin', q=True, loaded=True)
```

The mayaUsdPlugin provides Maya-native USD support (proxy shapes, import/export).

## Import and Export Commands

### Export
- `cmds.mayaUSDExport(file='/path/scene.usd')` — Export entire scene
- `cmds.mayaUSDExport(file='/out.usda', selection=True)` — Export selected only
- `cmds.mayaUSDExport(file='/out.usd', exportSkels='auto', exportSkin='auto')` — With skinning
- `cmds.mayaUSDExport(file='/out.usd', shadingMode='useRegistry', convertMaterialsTo=['UsdPreviewSurface'])` — Convert materials
- `cmds.mayaUSDExport(file='/out.usd', mergeTransformAndShape=True)` — Merge xform+shape
- `cmds.mayaUSDExport(file='/out.usd', frameRange=[1,120], frameStride=1)` — Animation range
- `cmds.mayaUSDExport(file='/out.usd', kind='component')` — Set USD kind

Export formats: `.usd` (binary crate), `.usda` (ASCII), `.usdc` (binary), `.usdz` (packaged).

### Import
- `cmds.mayaUSDImport(file='/path/scene.usd')` — Import USD to Maya geometry
- `cmds.mayaUSDImport(file='/in.usd', primPath='/World/Geo')` — Import specific prim
- `cmds.mayaUSDImport(file='/in.usd', readAnimData=True)` — Include animation
- `cmds.mayaUSDImport(file='/in.usd', shadingMode=[['useRegistry','UsdPreviewSurface']])` — Import materials

## Proxy Shape (Non-destructive USD in Maya)

```python
import maya.cmds as cmds
# Create a USD proxy shape — loads USD without converting to Maya
proxy = cmds.createNode('mayaUsdProxyShape', name='usdProxyShape')
cmds.setAttr(f'{proxy}.filePath', '/path/scene.usd', type='string')
cmds.setAttr(f'{proxy}.primPath', '/', type='string')
```

Proxy shapes display USD data in Maya viewport without converting to native geometry.
Faster for large scenes. Edit via UFE (Unified Front End) or pull to Maya as needed.

## pxr Python API (OpenUSD in Maya)

Maya ships with the full pxr (Pixar USD) Python bindings.

### Stage Operations
```python
from pxr import Usd, UsdGeom, Sdf, Gf

# Open existing stage
stage = Usd.Stage.Open('/path/scene.usda')

# Create new stage
stage = Usd.Stage.CreateNew('/path/new.usda')

# Get root layer
root_layer = stage.GetRootLayer()

# Define a prim
xform = UsdGeom.Xform.Define(stage, '/World')
mesh = UsdGeom.Mesh.Define(stage, '/World/MyMesh')

# Set attributes
xform.AddTranslateOp().Set(Gf.Vec3d(1.0, 2.0, 3.0))
mesh.GetPointsAttr().Set([(0,0,0), (1,0,0), (1,1,0), (0,1,0)])
mesh.GetFaceVertexCountsAttr().Set([4])
mesh.GetFaceVertexIndicesAttr().Set([0, 1, 2, 3])

# Save
stage.GetRootLayer().Save()
```

### Common pxr Types
- `Gf.Vec3d(x, y, z)` — Double-precision 3D vector
- `Gf.Vec3f(x, y, z)` — Float-precision 3D vector
- `Gf.Matrix4d()` — 4x4 matrix
- `Gf.Quatf(w, x, y, z)` — Quaternion
- `Sdf.Path('/World/Geo')` — USD path
- `Sdf.ValueTypeNames.Float3` — Type token for attributes

### Traversal
```python
from pxr import Usd, UsdGeom

stage = Usd.Stage.Open('/path/scene.usda')
for prim in stage.Traverse():
    if prim.IsA(UsdGeom.Mesh):
        mesh = UsdGeom.Mesh(prim)
        points = mesh.GetPointsAttr().Get()
        print(f'{prim.GetPath()}: {len(points)} vertices')
```

### Layers and Composition
```python
from pxr import Usd, Sdf

stage = Usd.Stage.Open('/path/scene.usda')

# Add sublayer
root = stage.GetRootLayer()
root.subLayerPaths.append('/path/overlay.usda')

# Add reference
prim = stage.DefinePrim('/World/Asset')
prim.GetReferences().AddReference('/path/asset.usd')

# Variants
vset = prim.GetVariantSets().AddVariantSet('modelComplexity')
vset.AddVariant('low')
vset.AddVariant('high')
vset.SetVariantSelection('high')
```

## Maya-USD Workflow Patterns

### Round-trip: Maya -> USD -> Maya
```python
import maya.cmds as cmds

# Export scene to USD
cmds.mayaUSDExport(file='/tmp/roundtrip.usda', selection=True)

# ... edit externally ...

# Re-import
cmds.mayaUSDImport(file='/tmp/roundtrip.usda')
```

### USD Reference in Maya Scene
```python
import maya.cmds as cmds
# Create proxy for viewport display
proxy_transform = cmds.createNode('transform', name='usd_asset')
proxy_shape = cmds.createNode('mayaUsdProxyShape', parent=proxy_transform)
cmds.setAttr(f'{proxy_shape}.filePath', '/path/asset.usd', type='string')
```

### Export with Material Conversion
```python
import maya.cmds as cmds
# Arnold materials -> UsdPreviewSurface
cmds.mayaUSDExport(
    file='/out.usd',
    shadingMode='useRegistry',
    convertMaterialsTo=['UsdPreviewSurface'],
    exportDisplayColor=True,
)
```

## Important Notes

- USD file paths in Maya must be absolute or relative to the workspace
- USDZ is read-only in Maya (can import but not directly reference)
- Animation export requires explicit `frameRange` parameter
- Material conversion is lossy — complex Arnold networks may simplify
- Proxy shapes use UFE for selection, not standard Maya selection
- `cmds.mayaUSDExport` and `cmds.mayaUSDImport` are the correct command names
  (NOT `cmds.usdExport` or `cmds.usdImport` — those are WRONG)
