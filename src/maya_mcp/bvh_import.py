"""Self-contained BVH (BioVision Hierarchy) importer for maya-mcp.

Maya has no native BVH import. This module unlocks free mocap (CMU, Bandai-Namco,
etc.) by parsing a ``.bvh`` file and building the joint hierarchy + animation
inside Maya. It is split into two cleanly separated layers:

1. **Pure parser** (no Maya dependency, fully unit-testable offline):
   :func:`parse_bvh` reads the ``HIERARCHY`` and ``MOTION`` sections into plain
   dataclasses — :class:`BvhJoint`, :class:`BvhSkeleton`, :class:`BvhMotion`.
   It is robust to CRLF/LF, arbitrary whitespace/tabs, ``End Site`` blocks,
   nested braces, joints with 3 or 6 (or any N) channels, and any channel order.

2. **Maya builder** (:func:`build_in_maya`) which uses ``maya.cmds``. The import
   is LAZY (inside the function) so the parser above stays importable — and
   therefore testable — on a machine with no Maya (e.g. CI). See
   :func:`maya_available`.

The subtle part of a correct import is the **rotation order** (see
:func:`maya_rotate_order`). BVH lists rotation channels in the order the
rotation matrices are composed under the standard column-vector / pre-multiply
convention (first-listed channel = outermost/leftmost matrix). Maya's
``rotateOrder`` attribute uses a row-vector / post-multiply convention. Bridging
the two, the Maya ``rotateOrder`` must be the **reverse** of the BVH rotation
channel order, with the per-axis rotate values mapped straight through (no sign
flip). Full derivation lives in the :func:`maya_rotate_order` docstring.

Because the physical rotation convention cannot be verified without a running
Maya, the mapping is isolated in one small pure function so it is unit-tested in
isolation and trivial to audit / flip; the end-to-end build is expected to be
validated in-vivo before being relied upon (in-vivo gate).
"""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Dict, Iterator, List, Optional, Tuple

__all__ = [
    "BvhJoint",
    "BvhSkeleton",
    "BvhMotion",
    "BvhParseError",
    "parse_bvh",
    "maya_rotate_order",
    "maya_rotate_order_enum",
    "maya_available",
    "build_in_maya",
    "MAYA_ROTATE_ORDER_ENUM",
    "POSITION_CHANNELS",
    "ROTATION_CHANNELS",
]

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

#: BVH position channel keywords, mapped to their Maya translate axis suffix.
POSITION_CHANNELS: Dict[str, str] = {
    "Xposition": "X",
    "Yposition": "Y",
    "Zposition": "Z",
}

#: BVH rotation channel keywords, mapped to their Maya rotate axis suffix.
ROTATION_CHANNELS: Dict[str, str] = {
    "Xrotation": "X",
    "Yrotation": "Y",
    "Zrotation": "Z",
}

#: Maya ``rotateOrder`` attribute enum values (the integer stored on the node)
#: keyed by the lowercase axis-order string.
MAYA_ROTATE_ORDER_ENUM: Dict[str, int] = {
    "xyz": 0,
    "yzx": 1,
    "zxy": 2,
    "xzy": 3,
    "yxz": 4,
    "zyx": 5,
}


class BvhParseError(ValueError):
    """Raised when the BVH text is malformed or internally inconsistent."""


# ─────────────────────────────────────────────────────────────────────────────
# Data structures (pure, no Maya)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class BvhJoint:
    """A single node in the BVH skeleton tree.

    Attributes:
        name: Unique joint name. ``End Site`` leaves are synthesised as
            ``"<parent>_End"`` (disambiguated with a numeric suffix on collision).
        offset: Local translation ``(x, y, z)`` relative to the parent, in the
            file's native units (unscaled).
        channels: The joint's channel list exactly as declared, e.g.
            ``["Xposition", "Yposition", "Zposition", "Zrotation", "Xrotation",
            "Yrotation"]``. ``End Site`` leaves have an empty list.
        parent: The parent :class:`BvhJoint`, or ``None`` for the root. Excluded
            from ``repr``/``eq`` to avoid infinite recursion on the cyclic tree.
        children: Child joints in declaration order.
        is_end_site: ``True`` for a synthetic ``End Site`` leaf.
    """

    name: str
    offset: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    channels: List[str] = field(default_factory=list)
    parent: Optional["BvhJoint"] = field(default=None, repr=False, compare=False)
    children: List["BvhJoint"] = field(default_factory=list)
    is_end_site: bool = False

    @property
    def rotation_channels(self) -> List[str]:
        """The joint's rotation channels, in declared order (e.g. ``[Z, X, Y]``)."""
        return [c for c in self.channels if c in ROTATION_CHANNELS]

    @property
    def position_channels(self) -> List[str]:
        """The joint's position channels, in declared order."""
        return [c for c in self.channels if c in POSITION_CHANNELS]

    @property
    def has_position(self) -> bool:
        """Whether the joint carries animated position (translation) channels."""
        return bool(self.position_channels)


@dataclass
class BvhSkeleton:
    """The parsed ``HIERARCHY`` section.

    Attributes:
        root: The root :class:`BvhJoint`.
        joints: Flat ``name -> BvhJoint`` map in DFS declaration order (a plain
            ``dict`` preserves insertion order, so a parent always precedes its
            children — safe to iterate for creation).
        channel_order: The flat, ordered list of ``(joint_name, channel_name)``
            pairs, in the exact order the channels are declared across the whole
            hierarchy. This IS the column layout of every ``MOTION`` frame row.
    """

    root: BvhJoint
    joints: Dict[str, BvhJoint]
    channel_order: List[Tuple[str, str]]

    @property
    def num_channels(self) -> int:
        """Total animated channels == the expected width of each motion frame."""
        return len(self.channel_order)

    def iter_joints(self) -> Iterator[BvhJoint]:
        """Yield joints in DFS declaration order (root first, parents before
        children)."""
        yield from self.joints.values()

    def channel_columns(self) -> Dict[str, Dict[str, int]]:
        """Map ``joint_name -> {channel_name: column_index}`` into a motion row.

        Precomputed once so the builder can slice per-joint values out of each
        frame in O(1) without re-scanning ``channel_order``.
        """
        columns: Dict[str, Dict[str, int]] = {}
        for index, (joint_name, channel) in enumerate(self.channel_order):
            columns.setdefault(joint_name, {})[channel] = index
        return columns


@dataclass
class BvhMotion:
    """The parsed ``MOTION`` section.

    Attributes:
        frame_time: Seconds per frame (``Frame Time:``). ``1 / frame_time`` is
            the source FPS.
        frames: One list of floats per frame; each row's length equals
            :attr:`BvhSkeleton.num_channels` and follows
            :attr:`BvhSkeleton.channel_order`.
    """

    frame_time: float
    frames: List[List[float]]

    @property
    def num_frames(self) -> int:
        """Number of motion frames actually parsed."""
        return len(self.frames)

    @property
    def fps(self) -> float:
        """Source frames-per-second derived from ``frame_time``."""
        return 1.0 / self.frame_time if self.frame_time > 0 else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Pure parser
# ─────────────────────────────────────────────────────────────────────────────


def parse_bvh(source: "str | os.PathLike[str]") -> Tuple[BvhSkeleton, BvhMotion]:
    """Parse a BVH document into a :class:`BvhSkeleton` and :class:`BvhMotion`.

    Args:
        source: Either the raw BVH text, or a path to a ``.bvh`` file. A ``str``
            is treated as a filesystem path only when it contains no newline and
            points to an existing file; otherwise it is treated as BVH text. An
            ``os.PathLike`` is always read as a file.

    Returns:
        A ``(skeleton, motion)`` tuple.

    Raises:
        BvhParseError: On malformed structure or a size mismatch between the
            declared frame count / channel count and the actual motion data.
    """
    text = _read_source(source)
    hierarchy_lines, motion_lines = _split_sections(text)
    skeleton = _parse_hierarchy(hierarchy_lines)
    motion = _parse_motion(motion_lines, skeleton.num_channels)
    return skeleton, motion


def _read_source(source: "str | os.PathLike[str]") -> str:
    """Return the BVH text, reading from disk when ``source`` is a path."""
    if isinstance(source, os.PathLike):
        return Path(source).read_text(encoding="utf-8", errors="replace")
    if isinstance(source, str):
        # A one-line string that resolves to an existing file is a path;
        # anything multi-line (i.e. real BVH content) is treated as text.
        if "\n" not in source and "\r" not in source:
            try:
                candidate = Path(source)
                if candidate.is_file():
                    return candidate.read_text(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
        return source
    raise TypeError(f"source must be str or os.PathLike, got {type(source)!r}")


def _split_sections(text: str) -> Tuple[List[str], List[str]]:
    """Split normalised text into ``HIERARCHY`` and ``MOTION`` line lists."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    hierarchy_start = None
    motion_start = None
    for index, line in enumerate(lines):
        stripped = line.strip().upper()
        if stripped == "HIERARCHY" and hierarchy_start is None:
            hierarchy_start = index
        elif stripped == "MOTION" and motion_start is None:
            motion_start = index
            break

    if motion_start is None:
        raise BvhParseError("No MOTION section found (missing 'MOTION' keyword).")

    start = 0 if hierarchy_start is None else hierarchy_start + 1
    hierarchy_lines = lines[start:motion_start]
    motion_lines = lines[motion_start + 1:]
    return hierarchy_lines, motion_lines


def _parse_hierarchy(lines: List[str]) -> BvhSkeleton:
    """Build the joint tree from the ``HIERARCHY`` lines via a token stream."""
    tokens: Deque[str] = deque()
    for line in lines:
        tokens.extend(line.split())

    if not tokens:
        raise BvhParseError("Empty HIERARCHY section.")

    keyword = tokens.popleft()
    if keyword.upper() != "ROOT":
        raise BvhParseError(f"Expected ROOT at start of hierarchy, got {keyword!r}.")

    joints: Dict[str, BvhJoint] = {}
    channel_order: List[Tuple[str, str]] = []
    root = _parse_joint(tokens, parent=None, joints=joints,
                        channel_order=channel_order)
    return BvhSkeleton(root=root, joints=joints, channel_order=channel_order)


def _unique_name(base: str, taken: Dict[str, BvhJoint]) -> str:
    """Return ``base`` or ``base_1``/``base_2``/… so joint names stay unique."""
    if base not in taken:
        return base
    suffix = 1
    while f"{base}_{suffix}" in taken:
        suffix += 1
    return f"{base}_{suffix}"


def _parse_joint(
    tokens: Deque[str],
    parent: Optional[BvhJoint],
    joints: Dict[str, BvhJoint],
    channel_order: List[Tuple[str, str]],
) -> BvhJoint:
    """Recursively parse one ``ROOT``/``JOINT`` block (name already consumed)."""
    if not tokens:
        raise BvhParseError("Unexpected end of hierarchy (expected joint name).")

    raw_name = tokens.popleft()
    name = _unique_name(raw_name, joints)
    joint = BvhJoint(name=name, parent=parent)
    joints[name] = joint

    _expect(tokens, "{")

    while True:
        if not tokens:
            raise BvhParseError(f"Unterminated block for joint {name!r} (missing '}}').")
        token = tokens.popleft()
        upper = token.upper()

        if token == "}":
            break
        if upper == "OFFSET":
            joint.offset = _read_offset(tokens, name)
        elif upper == "CHANNELS":
            joint.channels = _read_channels(tokens, name)
            for channel in joint.channels:
                channel_order.append((name, channel))
        elif upper == "JOINT":
            child = _parse_joint(tokens, joint, joints, channel_order)
            joint.children.append(child)
        elif upper == "END":
            # "End Site": a leaf with only an OFFSET and no channels.
            site = tokens.popleft() if tokens else ""
            if site.upper() != "SITE":
                raise BvhParseError(
                    f"Expected 'End Site' in joint {name!r}, got 'End {site}'.")
            child = _parse_end_site(tokens, joint, joints)
            joint.children.append(child)
        else:
            raise BvhParseError(
                f"Unexpected token {token!r} inside joint {name!r}.")

    return joint


def _parse_end_site(
    tokens: Deque[str],
    parent: BvhJoint,
    joints: Dict[str, BvhJoint],
) -> BvhJoint:
    """Parse an ``End Site`` block into a synthetic, channel-less leaf joint."""
    name = _unique_name(f"{parent.name}_End", joints)
    joint = BvhJoint(name=name, parent=parent, is_end_site=True)
    joints[name] = joint

    _expect(tokens, "{")
    while True:
        if not tokens:
            raise BvhParseError(f"Unterminated End Site for {parent.name!r}.")
        token = tokens.popleft()
        if token == "}":
            break
        if token.upper() == "OFFSET":
            joint.offset = _read_offset(tokens, name)
        else:
            raise BvhParseError(
                f"Unexpected token {token!r} inside End Site of {parent.name!r}.")
    return joint


def _expect(tokens: Deque[str], expected: str) -> None:
    """Pop the next token and assert it equals ``expected``."""
    if not tokens:
        raise BvhParseError(f"Expected {expected!r} but hierarchy ended.")
    token = tokens.popleft()
    if token != expected:
        raise BvhParseError(f"Expected {expected!r}, got {token!r}.")


def _read_offset(tokens: Deque[str], joint_name: str) -> Tuple[float, float, float]:
    """Read three floats following an ``OFFSET`` keyword."""
    try:
        x = float(tokens.popleft())
        y = float(tokens.popleft())
        z = float(tokens.popleft())
    except (IndexError, ValueError) as exc:
        raise BvhParseError(f"Malformed OFFSET for joint {joint_name!r}.") from exc
    return (x, y, z)


def _read_channels(tokens: Deque[str], joint_name: str) -> List[str]:
    """Read ``CHANNELS N c1 c2 … cN`` following the ``CHANNELS`` keyword."""
    if not tokens:
        raise BvhParseError(f"Missing channel count for joint {joint_name!r}.")
    try:
        count = int(tokens.popleft())
    except ValueError as exc:
        raise BvhParseError(
            f"Non-integer channel count for joint {joint_name!r}.") from exc
    if count < 0:
        raise BvhParseError(f"Negative channel count for joint {joint_name!r}.")
    if len(tokens) < count:
        raise BvhParseError(
            f"Declared {count} channels for {joint_name!r} but only "
            f"{len(tokens)} tokens remain.")
    channels = [tokens.popleft() for _ in range(count)]
    unknown = [c for c in channels
               if c not in POSITION_CHANNELS and c not in ROTATION_CHANNELS]
    if unknown:
        raise BvhParseError(
            f"Unknown channel(s) {unknown} for joint {joint_name!r}.")
    return channels


def _parse_motion(lines: List[str], expected_channels: int) -> BvhMotion:
    """Parse the ``MOTION`` section: frame count, frame time, and frame rows."""
    declared_frames: Optional[int] = None
    frame_time: Optional[float] = None
    frames: List[List[float]] = []

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        lowered = line.lower()
        if lowered.startswith("frames:"):
            declared_frames = _parse_labelled_int(line, "Frames")
        elif lowered.startswith("frame time"):
            frame_time = _parse_labelled_float(line, "Frame Time")
        else:
            frames.append(_parse_frame_row(line, expected_channels, len(frames)))

    if frame_time is None:
        raise BvhParseError("Missing 'Frame Time:' in MOTION section.")
    if frame_time <= 0:
        raise BvhParseError(f"Non-positive Frame Time: {frame_time}.")
    if declared_frames is not None and declared_frames != len(frames):
        raise BvhParseError(
            f"Frames: declared {declared_frames} but parsed {len(frames)} rows.")

    return BvhMotion(frame_time=frame_time, frames=frames)


def _parse_labelled_int(line: str, label: str) -> int:
    """Parse the integer after ``Label:`` on a line."""
    try:
        return int(line.split(":", 1)[1].strip())
    except (IndexError, ValueError) as exc:
        raise BvhParseError(f"Malformed '{label}:' line: {line!r}.") from exc


def _parse_labelled_float(line: str, label: str) -> float:
    """Parse the float after ``Label:`` on a line."""
    try:
        return float(line.split(":", 1)[1].strip())
    except (IndexError, ValueError) as exc:
        raise BvhParseError(f"Malformed '{label}:' line: {line!r}.") from exc


def _parse_frame_row(line: str, expected: int, index: int) -> List[float]:
    """Parse one whitespace-separated float row, validating its width."""
    try:
        values = [float(token) for token in line.split()]
    except ValueError as exc:
        raise BvhParseError(f"Non-numeric value in frame {index}: {line!r}.") from exc
    if len(values) != expected:
        raise BvhParseError(
            f"Frame {index} has {len(values)} values, expected {expected} "
            f"(channel count).")
    return values


# ─────────────────────────────────────────────────────────────────────────────
# Rotation-order mapping (the subtle part — kept pure and isolated)
# ─────────────────────────────────────────────────────────────────────────────


def maya_rotate_order(rotation_channels: List[str]) -> str:
    """Map a joint's BVH rotation channel order to a Maya ``rotateOrder`` string.

    THE CORRECTNESS-CRITICAL MAPPING. BVH declares its rotation channels in the
    order the rotation matrices compose under the standard column-vector /
    pre-multiply convention: for channels ``[Zrotation, Xrotation, Yrotation]``
    the local rotation is ``R = Rz · Rx · Ry`` applied to a column vector
    ``p' = R · p`` — i.e. the FIRST-listed channel is the outermost (leftmost)
    matrix.

    Maya's ``rotateOrder`` uses a row-vector / post-multiply convention: for
    ``rotateOrder = "abc"`` a point transforms as ``p' = p · Ra · Rb · Rc`` with
    Maya's row matrices (the transpose of the column ones). Requiring Maya to
    realise the same physical rotation as BVH and matching the two expressions
    term-by-term yields Maya-order == the **reverse** of the BVH channel order,
    with each axis' rotate value passed straight through (no sign change):

        ``[Zrotation, Xrotation, Yrotation]``  ->  ``"yxz"``  (Maya enum 4)

    Because this depends on a physical convention that can only be confirmed with
    a running Maya, it is isolated here so it is unit-tested in isolation and
    trivial to audit or flip should in-vivo validation contradict it.

    Args:
        rotation_channels: The joint's rotation channels in declared order (as
            returned by :attr:`BvhJoint.rotation_channels`). Must contain exactly
            the three distinct rotation channels.

    Returns:
        A lowercase Maya ``rotateOrder`` axis string (one of the keys of
        :data:`MAYA_ROTATE_ORDER_ENUM`).

    Raises:
        BvhParseError: If the channels are not exactly ``X``/``Y``/``Z`` rotation.
    """
    axes = [ROTATION_CHANNELS[c] for c in rotation_channels
            if c in ROTATION_CHANNELS]
    if len(axes) != 3 or set(axes) != {"X", "Y", "Z"}:
        raise BvhParseError(
            f"Expected exactly the 3 rotation channels, got {rotation_channels!r}.")
    order = "".join(reversed(axes)).lower()
    return order


def maya_rotate_order_enum(rotation_channels: List[str]) -> int:
    """Return the integer Maya ``rotateOrder`` enum for a joint's rotation
    channels."""
    return MAYA_ROTATE_ORDER_ENUM[maya_rotate_order(rotation_channels)]


# ─────────────────────────────────────────────────────────────────────────────
# Maya builder (guarded import — never imported at module load)
# ─────────────────────────────────────────────────────────────────────────────


def maya_available() -> bool:
    """Return ``True`` when ``maya.cmds`` can be imported (i.e. running in Maya).

    Used by tests to skip the in-Maya build and by callers to fail fast with a
    clear message off-DCC.
    """
    try:
        import maya.cmds  # noqa: F401
    except Exception:
        return False
    return True


def _sanitize_node_name(name: str) -> str:
    """Coerce a BVH joint name into a valid Maya node name.

    Maya node names must match ``[A-Za-z_][A-Za-z0-9_]*``. Invalid characters
    become ``_``; a leading digit is prefixed with ``_``.
    """
    cleaned = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in name)
    if not cleaned:
        cleaned = "joint"
    if cleaned[0].isdigit():
        cleaned = "_" + cleaned
    return cleaned


def build_in_maya(
    skeleton: BvhSkeleton,
    motion: BvhMotion,
    namespace: str = "bvh",
    scale: float = 1.0,
    up_axis: str = "y",
    start_frame: int = 1,
) -> Dict[str, object]:
    """Build the BVH skeleton + animation inside Maya (requires ``maya.cmds``).

    Creates a joint hierarchy (offsets applied as local translations, each joint
    given the correct ``rotateOrder`` per its BVH channel order — see
    :func:`maya_rotate_order`), then keyframes every animated channel per frame:
    the root (and any joint with position channels) has its translation keyed
    from the position channels (``offset + value``), and every joint has its
    rotation keyed from its rotation channels. Everything is grouped under one
    container node and wrapped in a single undo chunk, matching the repo
    convention.

    Args:
        skeleton: Parsed skeleton from :func:`parse_bvh`.
        motion: Parsed motion from :func:`parse_bvh`.
        namespace: Maya namespace to isolate the created nodes (created if
            missing); node names are ``"<namespace>:<joint>"``.
        scale: Uniform scale applied to every offset and translation value
            (e.g. mocap in centimetres → ``0.01`` for a metres scene).
        up_axis: ``"y"`` (BVH default, no correction) or ``"z"`` (Z-up source:
            a ``-90`` X rotation is applied to the container group so it reads
            correctly in Maya's Y-up world).
        start_frame: Maya frame the first BVH frame is keyed on.

    Returns:
        A dict describing what was created::

            {
              "root": "<ns>:Hips",          # root joint node name
              "group": "<ns>:bvh_grp",      # container transform
              "namespace": "<ns>",
              "joints": ["<ns>:Hips", ...], # all created joint nodes, DFS order
              "num_joints": int,
              "num_frames": int,
              "start_frame": int,
              "end_frame": int,
              "frame_time": float,
              "fps": float,
            }

    Raises:
        RuntimeError: If ``maya.cmds`` is unavailable (not running inside Maya).
    """
    if not maya_available():
        raise RuntimeError(
            "build_in_maya requires a running Maya (maya.cmds unavailable).")

    import maya.cmds as cmds  # lazy: keeps the parser importable off-DCC

    if not cmds.namespace(exists=namespace):
        cmds.namespace(add=namespace)

    columns = skeleton.channel_columns()
    created: List[str] = []
    name_map: Dict[str, str] = {}  # bvh joint.name -> actual Maya node name

    cmds.undoInfo(openChunk=True, chunkName="bvh_import")
    try:
        cmds.select(clear=True)
        group = cmds.group(empty=True, name=f"{namespace}:bvh_grp")
        if up_axis.lower() == "z":
            # Rotate a Z-up source into Maya's Y-up world.
            cmds.setAttr(f"{group}.rotateX", -90.0)

        # 1. Build the joint hierarchy (parents precede children by dict order).
        for joint in skeleton.iter_joints():
            parent_node = group if joint.parent is None else name_map[joint.parent.name]
            cmds.select(parent_node)
            node_name = f"{namespace}:{_sanitize_node_name(joint.name)}"
            actual = cmds.joint(name=node_name)
            name_map[joint.name] = actual
            created.append(actual)

            ox, oy, oz = joint.offset
            cmds.setAttr(f"{actual}.translate",
                        ox * scale, oy * scale, oz * scale, type="double3")

            if not joint.is_end_site and joint.rotation_channels:
                cmds.setAttr(f"{actual}.rotateOrder",
                            maya_rotate_order_enum(joint.rotation_channels))

        # 2. Keyframe every animated channel, frame by frame.
        for frame_index, row in enumerate(motion.frames):
            time = start_frame + frame_index
            for joint in skeleton.iter_joints():
                if joint.is_end_site or not joint.channels:
                    continue
                node = name_map[joint.name]
                joint_columns = columns.get(joint.name, {})
                ox, oy, oz = joint.offset
                offset_by_axis = {"X": ox, "Y": oy, "Z": oz}

                for channel, axis in POSITION_CHANNELS.items():
                    col = joint_columns.get(channel)
                    if col is None:
                        continue
                    value = (offset_by_axis[axis] + row[col]) * scale
                    cmds.setKeyframe(node, attribute=f"translate{axis}",
                                    value=value, time=time)

                for channel, axis in ROTATION_CHANNELS.items():
                    col = joint_columns.get(channel)
                    if col is None:
                        continue
                    cmds.setKeyframe(node, attribute=f"rotate{axis}",
                                    value=row[col], time=time)
    finally:
        cmds.undoInfo(closeChunk=True)

    end_frame = start_frame + max(motion.num_frames - 1, 0)

    # Set the scene FPS + playback range so the clip plays at its native rate.
    fps = motion.fps
    try:
        cmds.currentUnit(time=f"{int(round(fps))}fps")
    except Exception:
        pass  # non-standard rate: leave the scene unit untouched
    cmds.playbackOptions(minTime=start_frame, maxTime=end_frame,
                        animationStartTime=start_frame, animationEndTime=end_frame)

    return {
        "root": name_map[skeleton.root.name],
        "group": group,
        "namespace": namespace,
        "joints": created,
        "num_joints": len(created),
        "num_frames": motion.num_frames,
        "start_frame": start_frame,
        "end_frame": end_frame,
        "frame_time": motion.frame_time,
        "fps": fps,
    }
