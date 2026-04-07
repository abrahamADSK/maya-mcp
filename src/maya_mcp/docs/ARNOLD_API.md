# Arnold (mtoa) API Reference

Arnold renderer integration for Maya via the mtoa plugin.
Covers shader creation, render settings, AOVs, and light setup.

## Plugin Loading

```python
import maya.cmds as cmds
cmds.loadPlugin('mtoa', quiet=True)
# Verify: cmds.pluginInfo('mtoa', q=True, loaded=True)
```

The mtoa plugin must be loaded before creating Arnold nodes.

## Shaders

- `cmds.shadingNode('aiStandardSurface', asShader=True)` — PBR uber-shader (default in Maya 2020+)
- `cmds.shadingNode('aiStandardHair', asShader=True)` — Hair/fur shader
- `cmds.shadingNode('aiStandardVolume', asShader=True)` — Volume shader
- `cmds.shadingNode('aiToon', asShader=True)` — Toon/cel shader
- `cmds.shadingNode('aiFlat', asShader=True)` — Flat/unlit shader
- `cmds.shadingNode('aiMixShader', asShader=True)` — Mix two shaders
- `cmds.shadingNode('aiCarPaint', asShader=True)` — Car paint shader

### aiStandardSurface Attributes

Base layer:
- `baseColor` (double3) — Base color, default (0.8, 0.8, 0.8)
- `base` (float) — Base weight, default 1.0
- `diffuseRoughness` (float) — Oren-Nayar roughness, default 0.0
- `metalness` (float) — Metallic, default 0.0

Specular:
- `specularColor` (double3) — Specular tint, default (1, 1, 1)
- `specular` (float) — Specular weight, default 1.0
- `specularRoughness` (float) — Roughness 0-1, default 0.1
- `specularIOR` (float) — Index of refraction, default 1.5
- `specularAnisotropy` (float) — 0-1, default 0.0

Transmission:
- `transmission` (float) — Transparency weight, default 0.0
- `transmissionColor` (double3) — Transmission tint
- `transmissionDepth` (float) — Depth for Beer's law absorption

Subsurface:
- `subsurface` (float) — SSS weight, default 0.0
- `subsurfaceColor` (double3) — SSS color
- `subsurfaceRadius` (double3) — Per-channel SSS radius
- `subsurfaceType` (enum) — 0=diffusion, 1=randomwalk, 2=randomwalk_v2

Coat:
- `coat` (float) — Clearcoat weight, default 0.0
- `coatColor` (double3) — Coat tint
- `coatRoughness` (float) — Coat roughness

Emission:
- `emission` (float) — Emission weight, default 0.0
- `emissionColor` (double3) — Emission color
- `opacity` (double3) — Per-channel opacity, default (1, 1, 1)

## Lights

- `cmds.shadingNode('aiSkyDomeLight', asLight=True)` — Environment/HDRI light
- `cmds.shadingNode('aiAreaLight', asLight=True)` — Area light (quad, disk, cylinder)
- `cmds.shadingNode('aiPhotometricLight', asLight=True)` — IES light
- `cmds.shadingNode('aiMeshLight', asLight=True)` — Mesh as emitter
- `cmds.shadingNode('aiLightPortal', asLight=True)` — Portal for interior scenes

### SkyDome HDRI Setup
```python
import maya.cmds as cmds
dome = cmds.shadingNode('aiSkyDomeLight', asLight=True)
dome_shape = cmds.listRelatives(dome, shapes=True)[0]
file_node = cmds.shadingNode('file', asTexture=True)
cmds.setAttr(f'{file_node}.fileTextureName', '/path/env.hdr', type='string')
cmds.connectAttr(f'{file_node}.outColor', f'{dome_shape}.color')
cmds.setAttr(f'{dome_shape}.intensity', 1.0)
cmds.setAttr(f'{dome_shape}.camera', 0)  # 0=invisible to camera
```

## Render Settings

```python
import maya.cmds as cmds

# Set Arnold as renderer
cmds.setAttr('defaultRenderGlobals.currentRenderer', 'arnold', type='string')

# Sampling
cmds.setAttr('defaultArnoldRenderOptions.AASamples', 5)        # Camera (AA)
cmds.setAttr('defaultArnoldRenderOptions.GIDiffuseSamples', 3)  # Diffuse
cmds.setAttr('defaultArnoldRenderOptions.GISpecularSamples', 3) # Specular
cmds.setAttr('defaultArnoldRenderOptions.GITransmissionSamples', 2)
cmds.setAttr('defaultArnoldRenderOptions.GISSSSamples', 3)      # SSS
cmds.setAttr('defaultArnoldRenderOptions.GIVolumeSamples', 2)   # Volume

# Ray depth
cmds.setAttr('defaultArnoldRenderOptions.GIDiffuseDepth', 2)
cmds.setAttr('defaultArnoldRenderOptions.GISpecularDepth', 4)
cmds.setAttr('defaultArnoldRenderOptions.GITransmissionDepth', 4)

# Resolution (uses Maya's global resolution)
cmds.setAttr('defaultResolution.width', 1920)
cmds.setAttr('defaultResolution.height', 1080)

# Output
cmds.setAttr('defaultArnoldDriver.aiTranslator', 'exr', type='string')
cmds.setAttr('defaultArnoldDriver.mergeAOVs', 1)  # Multi-layer EXR
```

## AOVs (Arbitrary Output Variables)

```python
import maya.cmds as cmds
import mtoa.aovs as aovs

# Create AOV
aovs.AOVInterface().addAOV('diffuse')
aovs.AOVInterface().addAOV('specular')
aovs.AOVInterface().addAOV('N')       # Normal
aovs.AOVInterface().addAOV('Z')       # Depth
aovs.AOVInterface().addAOV('crypto_object')  # Cryptomatte

# Enable AOVs globally
cmds.setAttr('defaultArnoldRenderOptions.aovMode', 1)  # 1=enabled

# Custom AOV via shader
aovs.AOVInterface().addAOV('custom_mask', aovType='rgba')
```

Common AOVs: `diffuse`, `specular`, `coat`, `transmission`, `sss`, `emission`,
`background`, `volume`, `N` (normal), `Z` (depth), `P` (position),
`motionvector`, `crypto_object`, `crypto_material`, `crypto_asset`.

## Arnold Render Commands

- `cmds.arnoldRender(cam='persp')` — Render current frame interactively
- `cmds.arnoldRender(cam='persp', batch=True)` — Batch render
- `cmds.arnoldExportAss(f='/path/scene.ass', cam='persp')` — Export .ass file
- `cmds.arnoldIpr(cam='persp', mode='start')` — Start IPR
- `cmds.arnoldIpr(mode='stop')` — Stop IPR

## Textures and Utilities

- `cmds.shadingNode('aiImage', asTexture=True)` — Arnold native image node
- `cmds.shadingNode('aiNoise', asTexture=True)` — Procedural noise
- `cmds.shadingNode('aiColorCorrect', asUtility=True)` — Color correction
- `cmds.shadingNode('aiRange', asUtility=True)` — Remap range
- `cmds.shadingNode('aiNormalMap', asUtility=True)` — Normal map node
- `cmds.shadingNode('aiBump2d', asUtility=True)` — Bump map node
- `cmds.shadingNode('aiTriplanar', asUtility=True)` — Triplanar projection

### PBR Material Setup Pattern
```python
import maya.cmds as cmds

shader = cmds.shadingNode('aiStandardSurface', asShader=True, name='myPBR')
sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name='myPBR_SG')
cmds.connectAttr(f'{shader}.outColor', f'{sg}.surfaceShader')

# Base color texture
base_file = cmds.shadingNode('file', asTexture=True, name='base_tex')
place = cmds.shadingNode('place2dTexture', asUtility=True)
cmds.connectAttr(f'{place}.outUV', f'{base_file}.uv')
cmds.connectAttr(f'{place}.outUvFilterSize', f'{base_file}.uvFilterSize')
cmds.setAttr(f'{base_file}.fileTextureName', '/path/base_color.exr', type='string')
cmds.connectAttr(f'{base_file}.outColor', f'{shader}.baseColor')

# Roughness
rough_file = cmds.shadingNode('file', asTexture=True, name='rough_tex')
cmds.connectAttr(f'{place}.outUV', f'{rough_file}.uv')
cmds.setAttr(f'{rough_file}.fileTextureName', '/path/roughness.exr', type='string')
cmds.setAttr(f'{rough_file}.colorSpace', 'Raw', type='string')
cmds.setAttr(f'{rough_file}.alphaIsLuminance', 1)
cmds.connectAttr(f'{rough_file}.outAlpha', f'{shader}.specularRoughness')

# Normal map
normal_file = cmds.shadingNode('file', asTexture=True, name='normal_tex')
cmds.connectAttr(f'{place}.outUV', f'{normal_file}.uv')
cmds.setAttr(f'{normal_file}.fileTextureName', '/path/normal.exr', type='string')
cmds.setAttr(f'{normal_file}.colorSpace', 'Raw', type='string')
bump = cmds.shadingNode('aiBump2d', asUtility=True)
cmds.connectAttr(f'{normal_file}.outColor', f'{bump}.bumpMap')
cmds.setAttr(f'{bump}.bumpHeight', 1.0)
cmds.connectAttr(f'{bump}.outValue', f'{shader}.normalCamera')
```
