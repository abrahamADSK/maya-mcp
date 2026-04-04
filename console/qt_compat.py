"""Qt compatibility shim — PySide6 (Maya 2025+, standalone) / PySide2 (Maya 2023-2024).

Import everything from this module instead of importing PySide directly::

    from .qt_compat import QtWidgets, QtCore, QtGui, wrapInstance

Inside Maya's Python interpreter, PySide2 is available in 2023-2024 and
PySide6 in 2025+.  Outside Maya (standalone), we always use PySide6.
"""

from __future__ import annotations

import sys

_is_maya = "maya" in sys.modules or "maya.cmds" in sys.modules

try:
    from PySide6 import QtWidgets, QtCore, QtGui  # noqa: F401
    from shiboken6 import wrapInstance  # noqa: F401

    PYSIDE_VERSION = 6
except ImportError:
    from PySide2 import QtWidgets, QtCore, QtGui  # type: ignore[no-redef]  # noqa: F401
    from shiboken2 import wrapInstance  # type: ignore[no-redef]  # noqa: F401

    PYSIDE_VERSION = 2

__all__ = ["QtWidgets", "QtCore", "QtGui", "wrapInstance", "PYSIDE_VERSION", "_is_maya"]
