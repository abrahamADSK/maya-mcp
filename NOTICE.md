# Third-Party Notices

## maya-mcp

Copyright (c) 2026 Abraham Borbujo
Licensed under the MIT License — see [LICENSE](LICENSE).

---

## Autodesk Maya

maya-mcp communicates with **Autodesk Maya** via its Command Port TCP interface.
Maya and its Python API (`maya.cmds`, `maya.mel`, `pymel`) are proprietary software
developed and owned by **Autodesk, Inc.**

This project does not include, redistribute, or modify any Autodesk software.
It interacts with Maya solely through its documented, user-accessible scripting interfaces.

- Autodesk Maya: <https://www.autodesk.com/products/maya>
- Maya Python API docs: <https://help.autodesk.com/view/MAYAUL/2026/ENU/>

---

## Autodesk Arnold (mtoa)

Arnold rendering features are accessed through Maya's `mtoa` plugin API.
Arnold is proprietary software developed by **Autodesk, Inc.** (originally Solid Angle).

- Arnold: <https://www.autodesk.com/products/arnold>

---

## Vision3D / Hunyuan3D-2 (remote dependency)

maya-mcp optionally communicates with a **Vision3D** server via HTTP REST API.
Vision3D uses **Hunyuan3D-2** by **Tencent** and **SDXL Turbo** by **Stability AI**.
These models run on a separate GPU server and are NOT included in this repository.

- Hunyuan3D-2: <https://github.com/Tencent/Hunyuan3D-2> (Tencent Mixed Source License)
- SDXL Turbo: <https://huggingface.co/stabilityai/sdxl-turbo> (Stability AI Community License)

---

## Python Dependencies

| Package | License |
|---|---|
| `mcp` | MIT |
| `httpx` | BSD-3-Clause |
| `pydantic` | MIT |
| `chromadb` | Apache-2.0 |
| `sentence-transformers` | Apache-2.0 |
| `rank-bm25` | Apache-2.0 |
| `PySide6` | LGPL-3.0 / Commercial (Qt) |
