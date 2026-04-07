# maya.cmds API Reference

Authoritative reference for the maya.cmds Python module in Autodesk Maya.
Used by the RAG system to prevent hallucinations about command names, flags, and syntax.

## Scene Management

- `cmds.file(new=True, force=True)` — Create a new scene (force discards unsaved changes)
- `cmds.file(save=True)` — Save current scene
- `cmds.file(rename='path.ma')` — Rename current scene (call before first save)
- `cmds.file(q=True, sceneName=True)` — Query current scene path
- `cmds.file('path.ma', open=True, force=True)` — Open a scene file
- `cmds.file('path.ma', i=True, namespace='ns')` — Import a file with namespace
- `cmds.file('path.ma', reference=True, namespace='ns')` — Reference a file
- `cmds.file('path.ma', exportSelected=True, type='mayaAscii')` — Export selected
- `cmds.file(q=True, modified=True)` — Check if scene has unsaved changes

File types: `'mayaAscii'` (.ma), `'mayaBinary'` (.mb), `'OBJ'`, `'FBX export'`, `'FBX'`.

## Polygon Primitives

- `cmds.polyCube(w=1, h=1, d=1, sx=1, sy=1, sz=1, name='myCube')` — Create cube
- `cmds.polySphere(r=1, sx=20, sy=20, name='mySphere')` — Create sphere
- `cmds.polyCylinder(r=1, h=2, sx=20, sy=1, sz=1)` — Create cylinder
- `cmds.polyCone(r=1, h=2, sx=20)` — Create cone
- `cmds.polyPlane(w=1, h=1, sx=10, sy=10)` — Create plane
- `cmds.polyTorus(r=1, sr=0.5, tw=0, sx=20, sy=20)` — Create torus
- `cmds.polyPrism(l=1, ns=3)` — Create prism (ns=number of sides)
- `cmds.polyPyramid(ns=4, w=1)` — Create pyramid
- `cmds.polyPipe(r=1, h=2, t=0.1)` — Create pipe

Return value: `[transform_name, shape_name]` — always returns a list of two strings.

## NURBS Primitives

- `cmds.nurbsPlane(w=1, lr=1, d=3, u=1, v=1)` — NURBS plane
- `cmds.sphere(r=1, s=8, nsp=4)` — NURBS sphere (note: not polySphere)
- `cmds.circle(r=1, s=8, d=3)` — NURBS circle
- `cmds.curve(d=3, p=[(0,0,0),(1,1,0),(2,0,0)])` — Create curve from points

## Transform Operations (xform)

- `cmds.xform('obj', t=[x,y,z], ws=True)` — Set world-space translation
- `cmds.xform('obj', t=[x,y,z], r=True)` — Relative translation
- `cmds.xform('obj', ro=[rx,ry,rz], ws=True)` — Set world-space rotation
- `cmds.xform('obj', s=[sx,sy,sz])` — Set scale
- `cmds.xform('obj', piv=[px,py,pz], ws=True)` — Set pivot point
- `cmds.xform('obj', q=True, t=True, ws=True)` — Query world-space translation
- `cmds.xform('obj', q=True, ro=True, ws=True)` — Query rotation
- `cmds.xform('obj', q=True, s=True)` — Query scale
- `cmds.xform('obj', q=True, bb=True)` — Query bounding box [xmin,ymin,zmin,xmax,ymax,zmax]
- `cmds.xform('obj', q=True, m=True, ws=True)` — Query world matrix (16 floats)
- `cmds.makeIdentity('obj', apply=True, t=1, r=1, s=1)` — Freeze transforms

Important: `ws=True` means world space, `os=True` means object space.
`r=True` means relative (additive), without it means absolute.

## Selection

- `cmds.select('obj1', 'obj2')` — Select objects
- `cmds.select('obj', add=True)` — Add to selection
- `cmds.select('obj', deselect=True)` — Remove from selection
- `cmds.select(all=True)` — Select all
- `cmds.select(clear=True)` — Clear selection
- `cmds.ls(selection=True)` — Get current selection
- `cmds.ls(type='mesh')` — List all meshes
- `cmds.ls(type='transform')` — List all transforms
- `cmds.ls('prefix*')` — List by name pattern (wildcards)
- `cmds.ls(dag=True, long=True)` — List DAG with full paths
- `cmds.filterExpand(sm=12)` — Expand selection to faces (sm=12), vertices (sm=31), edges (sm=32)

Selection masks: 12=faces, 31=vertices, 32=edges, 34=UVs, 35=CVs.

## Hierarchy and DAG

- `cmds.parent('child', 'parent')` — Parent child under parent
- `cmds.parent('child', world=True)` — Unparent to world
- `cmds.group('obj1', 'obj2', name='grp')` — Group objects
- `cmds.listRelatives('obj', children=True)` — List children
- `cmds.listRelatives('obj', parent=True)` — Get parent
- `cmds.listRelatives('obj', shapes=True)` — Get shape nodes
- `cmds.listRelatives('obj', allDescendents=True)` — Get all descendants
- `cmds.duplicate('obj', name='obj_copy')` — Duplicate object
- `cmds.instance('obj', name='obj_inst')` — Create instance

## Attributes

- `cmds.getAttr('obj.translateX')` — Get attribute value
- `cmds.setAttr('obj.translateX', 5.0)` — Set float attribute
- `cmds.setAttr('obj.translate', 1, 2, 3, type='double3')` — Set compound attribute
- `cmds.setAttr('obj.customStr', 'hello', type='string')` — Set string attribute
- `cmds.addAttr('obj', ln='myAttr', at='float', dv=0.0)` — Add custom float attr
- `cmds.addAttr('obj', ln='myEnum', at='enum', en='Red:Green:Blue:', dv=0)` — Enum attr
- `cmds.addAttr('obj', ln='myStr', dt='string')` — Add string attr
- `cmds.attributeQuery('translateX', node='obj', exists=True)` — Check attr exists
- `cmds.listAttr('obj', userDefined=True)` — List custom attributes
- `cmds.connectAttr('src.outColor', 'dst.inColor')` — Connect attributes
- `cmds.disconnectAttr('src.outColor', 'dst.inColor')` — Disconnect
- `cmds.listConnections('obj.attr')` — List connections

Important: `type=` is required for compound types (double3, string, matrix).
Common mistake: `cmds.setAttr('obj.translate', [1,2,3])` — WRONG. Must unpack.

## Polygon Modeling Operations

- `cmds.polyExtrudeFacet('obj.f[0:3]', ltz=0.5)` — Extrude faces (ltz=local translate Z)
- `cmds.polyBevel3('obj.e[0:3]', offset=0.1, segments=2)` — Bevel edges
- `cmds.polyBoolOp('obj1', 'obj2', op=1)` — Boolean: 1=union, 2=difference, 3=intersection
- `cmds.polyUnite('obj1', 'obj2')` — Combine meshes
- `cmds.polySeparate('obj')` — Separate mesh shells
- `cmds.polySmooth('obj', divisions=2)` — Smooth mesh
- `cmds.polyReduce('obj', percentage=50)` — Reduce polygon count
- `cmds.polyMergeVertex('obj', d=0.001)` — Merge close vertices
- `cmds.polyNormal('obj', normalMode=0)` — Conform normals
- `cmds.polyMirrorFace('obj', direction=0, mergeMode=1)` — Mirror mesh
- `cmds.polyRetopo('obj', targetFaceCount=1000)` — Retopologize (Maya 2022+)
- `cmds.polySoftEdge('obj', a=180)` — Soften edges (a=angle threshold)
- `cmds.polyTriangulate('obj')` — Triangulate mesh
- `cmds.polyQuad('obj')` — Convert triangles to quads
- `cmds.polyCloseBorder('obj')` — Close holes
- `cmds.polyProjection('obj.f[*]', type='Planar')` — UV projection types: Planar, Cylindrical, Spherical

## UV Operations

- `cmds.polyAutoProjection('obj', lm=0, pb=0, ibd=1)` — Auto UV projection
- `cmds.polyPlanarProjection('obj.f[0:5]')` — Planar UV projection
- `cmds.polyMapCut('obj.e[0:3]')` — Cut UV edges
- `cmds.polyMapSew('obj.e[0:3]')` — Sew UV edges
- `cmds.unfold('obj')` — Unfold UVs (Legacy unfold)
- `cmds.u3dUnfold('obj')` — Unfold3D algorithm (Maya 2020+)
- `cmds.u3dLayout('obj')` — Layout UVs in 0-1 space
- `cmds.polyLayoutUV('obj', sc=1, se=2, rbf=0, fr=True)` — Layout UVs

## Materials and Shading

- `cmds.shadingNode('lambert', asShader=True, name='mat')` — Create shader node
- `cmds.shadingNode('file', asTexture=True)` — Create file texture node
- `cmds.shadingNode('place2dTexture', asUtility=True)` — Create UV placement node
- `cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name='matSG')` — Create shading group
- `cmds.connectAttr('mat.outColor', 'matSG.surfaceShader')` — Connect shader to SG
- `cmds.sets('obj', forceElement='matSG')` — Assign material to object
- `cmds.hyperShade(assign='mat')` — Assign material to selection

Shader types: `lambert`, `blinn`, `phong`, `aiStandardSurface` (Arnold),
`standardSurface` (Maya 2020+ built-in), `surfaceShader`, `useBackground`.

## Lights

- `cmds.directionalLight(name='dirLight')` — Create directional light
- `cmds.pointLight(name='ptLight')` — Create point light
- `cmds.spotLight(name='spotLight', coneAngle=40)` — Create spot light
- `cmds.ambientLight(name='ambLight')` — Create ambient light
- `cmds.shadingNode('areaLight', asLight=True)` — Create area light (transform only)
- `cmds.shadingNode('aiSkyDomeLight', asLight=True)` — Arnold sky dome
- `cmds.shadingNode('aiAreaLight', asLight=True)` — Arnold area light

Light attributes: intensity, color, decayRate, shadowColor, useDepthMapShadows.

## Cameras

- `cmds.camera(name='cam')` — Create camera (returns [transform, shape])
- `cmds.lookThru('cam')` — Look through camera in active viewport
- `cmds.viewFit('cam', all=True)` — Frame all objects
- `cmds.setAttr('camShape.focalLength', 50)` — Set focal length
- `cmds.setAttr('camShape.nearClipPlane', 0.1)` — Set near clip
- `cmds.setAttr('camShape.farClipPlane', 10000)` — Set far clip

Default cameras: `persp`, `top`, `front`, `side` (shapes: `perspShape`, etc.)

## Animation

- `cmds.setKeyframe('obj', at='translateX', t=1, v=0)` — Set keyframe
- `cmds.setKeyframe('obj', at='translateX', t=24, v=10)` — Set at frame 24
- `cmds.keyTangent('obj', at='translateX', itt='linear', ott='linear')` — Set tangents
- `cmds.playbackOptions(min=1, max=120, ast=1, aet=120)` — Set playback range
- `cmds.currentTime(24)` — Set current frame
- `cmds.currentTime(q=True)` — Query current frame
- `cmds.cutKey('obj', at='translateX')` — Delete keyframes
- `cmds.keyframe('obj', q=True, tc=True)` — Query keyframe times
- `cmds.bakeResults('obj', t=(1,120), sm=True)` — Bake animation

Tangent types: `auto`, `linear`, `flat`, `spline`, `step`, `clamped`, `plateau`.

## Rendering

- `cmds.render(cam='persp')` — Render current frame
- `cmds.playblast(f='path', fmt='image', fr=[1,120], wh=[1920,1080], p=100, v=False)` — Playblast
- `cmds.setAttr('defaultRenderGlobals.currentRenderer', 'arnold', type='string')` — Set renderer
- `cmds.getAttr('defaultRenderGlobals.currentRenderer')` — Get current renderer
- `cmds.setAttr('defaultResolution.width', 1920)` — Set render resolution
- `cmds.setAttr('defaultResolution.height', 1080)` — Set render resolution
- `cmds.arnoldRender(cam='persp', batch=True)` — Arnold batch render

Renderers: `arnold`, `mayaHardware2`, `mayaSoftware`, `vray`, `redshift`.

## Plugins

- `cmds.loadPlugin('mtoa')` — Load Arnold plugin
- `cmds.pluginInfo('mtoa', q=True, loaded=True)` — Check if loaded
- `cmds.unloadPlugin('mtoa')` — Unload plugin (dangerous on active scenes)
- `cmds.pluginInfo(q=True, listPlugins=True)` — List all loaded plugins

Common plugins: `mtoa` (Arnold), `fbxmaya` (FBX), `AbcImport`/`AbcExport` (Alembic),
`mayaUsdPlugin` (USD), `gpuCache`, `OneClick` (Mudbox), `Type`.

## Deformers

- `cmds.lattice('obj', dv=[2,5,2], oc=True)` — Create lattice
- `cmds.blendShape('target', 'base', name='bs')` — Create blend shape
- `cmds.skinCluster('joint1', 'obj', tsb=True)` — Bind skin
- `cmds.cluster('obj')` — Create cluster deformer
- `cmds.nonLinear('obj', type='bend')` — Nonlinear deformer (bend, flare, sine, squash, twist, wave)
- `cmds.wrap('driver', 'driven')` — Create wrap deformer
- `cmds.wire('obj', wire='curve')` — Wire deformer

## Constraints

- `cmds.parentConstraint('parent', 'child', mo=True)` — Parent constraint (mo=maintain offset)
- `cmds.pointConstraint('target', 'obj')` — Point constraint
- `cmds.orientConstraint('target', 'obj')` — Orient constraint
- `cmds.scaleConstraint('target', 'obj')` — Scale constraint
- `cmds.aimConstraint('target', 'obj', aim=[0,1,0], u=[0,0,1])` — Aim constraint
- `cmds.poleVectorConstraint('pv', 'ikHandle')` — Pole vector constraint

## Joints and IK

- `cmds.joint(p=[0,0,0], name='joint1')` — Create joint at position
- `cmds.joint('parentJoint', p=[0,5,0], name='childJoint')` — Create child joint
- `cmds.ikHandle(sj='startJoint', ee='endJoint', sol='ikRPsolver')` — IK handle
- `cmds.mirrorJoint('joint', mirrorYZ=True, mb='_L', rb='_R')` — Mirror joints

IK solvers: `ikRPsolver` (Rotate Plane), `ikSCsolver` (Single Chain), `ikSplineSolver`.

## Namespaces and References

- `cmds.namespace(add='myNS')` — Create namespace
- `cmds.namespace(set='myNS')` — Set current namespace
- `cmds.namespace(set=':')` — Reset to root namespace
- `cmds.namespaceInfo(lon=True)` — List all namespaces
- `cmds.referenceQuery('refNode', filename=True)` — Get reference file path
- `cmds.file(loadReference='refNode')` — Load a reference
- `cmds.file(unloadReference='refNode')` — Unload a reference

## Undo System

- `cmds.undoInfo(openChunk=True, chunkName='myOp')` — Open undo chunk
- `cmds.undoInfo(closeChunk=True)` — Close undo chunk
- `cmds.undo()` — Undo last operation
- `cmds.redo()` — Redo
- `cmds.undoInfo(q=True, undoName=True)` — Query what will be undone
- `cmds.undoInfo(stateWithoutFlush=False)` — Disable undo (DANGEROUS)
- `cmds.undoInfo(stateWithoutFlush=True)` — Re-enable undo

## Viewport

- `cmds.modelEditor('modelPanel4', e=True, displayTextures=True)` — Show textures
- `cmds.modelEditor('modelPanel4', e=True, wireframeOnShaded=True)` — Wireframe on shaded
- `cmds.modelEditor('modelPanel4', e=True, displayLights='all')` — Show all lights
- `cmds.refresh(force=True)` — Force viewport refresh
- `cmds.viewFit(all=True)` — Frame all objects in viewport

## Common Patterns

### Safe file save with error handling
```python
import maya.cmds as cmds
scene = cmds.file(q=True, sceneName=True)
if scene:
    cmds.file(save=True, type='mayaAscii')
else:
    cmds.file(rename='/path/untitled.ma')
    cmds.file(save=True, type='mayaAscii')
```

### Create and assign material
```python
import maya.cmds as cmds
mat = cmds.shadingNode('aiStandardSurface', asShader=True, name='myMat')
sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name='myMat_SG')
cmds.connectAttr(f'{mat}.outColor', f'{sg}.surfaceShader')
cmds.setAttr(f'{mat}.baseColor', 0.8, 0.2, 0.1, type='double3')
cmds.select('pCube1')
cmds.sets(forceElement=sg)
```

### Batch rename with padding
```python
import maya.cmds as cmds
sel = cmds.ls(selection=True)
for i, obj in enumerate(sel):
    new_name = f'asset_{i+1:03d}'
    cmds.rename(obj, new_name)
```

### Export selected as FBX
```python
import maya.cmds as cmds
cmds.loadPlugin('fbxmaya', quiet=True)
cmds.select('myGroup')
cmds.file('/path/export.fbx', force=True, options='v=0', type='FBX export',
          preserveReferences=True, exportSelected=True)
```

### Query scene statistics
```python
import maya.cmds as cmds
meshes = cmds.ls(type='mesh')
total_verts = sum(cmds.polyEvaluate(m, vertex=True) for m in meshes)
total_faces = sum(cmds.polyEvaluate(m, face=True) for m in meshes)
transforms = cmds.ls(type='transform')
cameras = cmds.ls(type='camera')
lights = cmds.ls(lights=True)
```
