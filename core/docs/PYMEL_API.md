# PyMEL API Reference

PyMEL (pymel.core) provides an object-oriented Python API for Maya.
Wraps maya.cmds with Pythonic node objects, attribute access, and type safety.

## Core Imports

```python
import pymel.core as pm
import pymel.core.datatypes as dt
import pymel.core.nodetypes as nt
```

## Node Creation and Selection

- `pm.polyCube(name='myCube')` — Returns (PyNode transform, PyNode shape)
- `pm.polySphere(r=2, name='mySphere')` — Create sphere
- `pm.selected()` — Returns list of selected PyNodes
- `pm.select('obj1', 'obj2')` — Select objects
- `pm.select(clear=True)` — Clear selection
- `pm.ls(type='mesh')` — List all mesh nodes as PyNodes
- `pm.ls(type='transform')` — List all transforms
- `pm.PyNode('pCube1')` — Wrap existing node as PyNode

Return types: PyMEL returns PyNode objects, not strings like cmds.

## PyNode Attribute Access

- `node.translateX.get()` — Get attribute value
- `node.translateX.set(5.0)` — Set attribute value
- `node.translate.get()` — Returns dt.Vector(x, y, z)
- `node.translate.set(1, 2, 3)` — Set compound attribute
- `node.attr('customAttr').get()` — Dynamic attribute access
- `node.getAttr('translateX')` — Alternative getter
- `node.setAttr('translateX', 5.0)` — Alternative setter

## Attribute Connections

- `src.outColor >> dst.inColor` — Connect attributes (Python operator overload)
- `src.outColor // dst.inColor` — Disconnect attributes
- `node.translateX.connections()` — List connections
- `node.translateX.inputs()` — List input connections
- `node.translateX.outputs()` — List output connections
- `node.translateX.isConnected()` — Check if connected

## Hierarchy

- `pm.parent(child, parent_node)` — Parent nodes
- `node.getParent()` — Get parent PyNode
- `node.getChildren()` — Get child PyNodes
- `node.getShape()` — Get shape node
- `node.getShapes()` — Get all shape nodes
- `pm.group(obj1, obj2, name='grp')` — Group objects
- `node.listRelatives(allDescendents=True)` — All descendants

## Transform Operations

- `node.setTranslation([x,y,z], space='world')` — Set world position
- `node.getTranslation(space='world')` — Get world position
- `node.setRotation([rx,ry,rz])` — Set rotation (degrees)
- `node.getRotation()` — Get rotation as EulerRotation
- `node.setScale([sx,sy,sz])` — Set scale
- `node.getScale()` — Get scale
- `node.getBoundingBox()` — Returns BoundingBox object
- `pm.makeIdentity(node, apply=True, t=1, r=1, s=1)` — Freeze transforms

## Data Types

- `dt.Vector(1, 2, 3)` — 3D vector with math operations
- `dt.Point(1, 2, 3)` — 3D point
- `dt.Matrix()` — 4x4 matrix
- `dt.EulerRotation(rx, ry, rz)` — Euler rotation
- `dt.Quaternion(x, y, z, w)` — Quaternion

Vector operations: `v1 + v2`, `v1 * scalar`, `v1.cross(v2)`, `v1.dot(v2)`,
`v1.normal()`, `v1.length()`, `v1.angle(v2)`.

## Mesh Components

- `mesh.vtx[0]` — Access vertex by index
- `mesh.f[0:5]` — Access face range
- `mesh.e[0]` — Access edge
- `mesh.vtx[0].getPosition(space='world')` — Get vertex position
- `mesh.vtx[0].setPosition(dt.Point(1,2,3))` — Set vertex position
- `mesh.numVertices()` — Vertex count
- `mesh.numFaces()` — Face count
- `mesh.numEdges()` — Edge count

## Common Patterns

### Iterate over mesh vertices
```python
import pymel.core as pm
mesh = pm.PyNode('pCube1').getShape()
for vtx in mesh.vtx:
    pos = vtx.getPosition(space='world')
    print(f'{vtx}: {pos}')
```

### Create and assign material
```python
import pymel.core as pm
shader = pm.shadingNode('aiStandardSurface', asShader=True, name='myShader')
sg = pm.sets(renderable=True, noSurfaceShader=True, empty=True, name='myShader_SG')
shader.outColor >> sg.surfaceShader
shader.baseColor.set(0.8, 0.2, 0.1)
pm.select('pCube1')
pm.sets(sg, forceElement=True)
```

### Query connections network
```python
import pymel.core as pm
node = pm.PyNode('aiStandardSurface1')
for conn in node.listConnections(source=True, destination=False):
    print(f'{conn} -> {node}')
```

## Key Differences from maya.cmds

1. PyMEL returns PyNode objects, cmds returns strings
2. PyMEL uses `node.attr.get()` / `.set()`, cmds uses `getAttr('node.attr')`
3. PyMEL supports `>>` operator for connections, cmds uses `connectAttr()`
4. PyMEL methods are camelCase on nodes, cmds functions are standalone
5. PyMEL handles type conversion automatically, cmds needs explicit `type=`
6. PyMEL is slower than cmds for bulk operations (wrapping overhead)
