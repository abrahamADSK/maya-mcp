"""
test_bvh_import.py
==================
Offline tests for the pure BVH parser in ``src/maya_mcp/bvh_import.py``.

No Maya, MCP SDK, or network required. These exercise the parser
(:func:`parse_bvh`) — joint tree, channel orders, offsets, frame count/time,
frame values, robustness to CRLF/whitespace — and the rotation-order mapping
(:func:`maya_rotate_order`), which is the correctness-critical piece of the
import.

The Maya builder (:func:`build_in_maya`) is NOT exercised here (no Maya in CI);
one placeholder test is marked ``skipif`` when ``maya.cmds`` is unavailable so it
lights up automatically when run from inside Maya.
"""

import textwrap

import pytest

from maya_mcp.bvh_import import (
    BvhParseError,
    build_in_maya,
    maya_available,
    maya_rotate_order,
    maya_rotate_order_enum,
    parse_bvh,
)


# ── fixtures ──────────────────────────────────────────────────────────────────

# A small 3-joint chain (Hips -> Chest -> Head) + an End Site leaf, 3 frames.
# Root has 6 channels (3 position + 3 rotation, ZXY rotation order); the two
# child joints have 3 rotation channels each (ZXY). Deliberately uses mixed
# spacing/tabs to check whitespace robustness. 6 + 3 + 3 = 12 channel columns.
BVH_TEXT = textwrap.dedent(
    """\
    HIERARCHY
    ROOT Hips
    {
        OFFSET 0.00 0.00 0.00
        CHANNELS 6 Xposition Yposition Zposition Zrotation Xrotation Yrotation
        JOINT Chest
        {
            OFFSET 0.0 5.0 0.0
            CHANNELS 3 Zrotation Xrotation Yrotation
            JOINT Head
            {
            \tOFFSET 0.0 3.0 0.0
            \tCHANNELS 3 Zrotation Xrotation Yrotation
                End Site
                {
                    OFFSET 0.0 2.0 0.0
                }
            }
        }
    }
    MOTION
    Frames: 3
    Frame Time: 0.033333
    0 0 0   0 0 0   0 0 0   0 0 0
    1.0 2.0 3.0   10 20 30   1 2 3   4 5 6
    -1 -2 -3   -10 -20 -30   -1 -2 -3   -4 -5 -6
    """
)


@pytest.fixture
def parsed():
    return parse_bvh(BVH_TEXT)


# ── 1. joint tree ─────────────────────────────────────────────────────────────


def test_root_identity_and_offset(parsed):
    skeleton, _ = parsed
    assert skeleton.root.name == "Hips"
    assert skeleton.root.parent is None
    assert skeleton.root.offset == (0.0, 0.0, 0.0)


def test_flat_joint_map_keys_and_order(parsed):
    skeleton, _ = parsed
    # DFS declaration order; End Site synthesised as "<parent>_End".
    assert list(skeleton.joints.keys()) == ["Hips", "Chest", "Head", "Head_End"]


def test_parent_child_links(parsed):
    skeleton, _ = parsed
    joints = skeleton.joints
    assert joints["Chest"].parent is joints["Hips"]
    assert joints["Head"].parent is joints["Chest"]
    assert joints["Head_End"].parent is joints["Head"]
    assert [c.name for c in joints["Hips"].children] == ["Chest"]
    assert [c.name for c in joints["Head"].children] == ["Head_End"]


def test_end_site_is_channelless_leaf(parsed):
    skeleton, _ = parsed
    end = skeleton.joints["Head_End"]
    assert end.is_end_site is True
    assert end.channels == []
    assert end.children == []
    assert end.offset == (0.0, 2.0, 0.0)


def test_child_offsets(parsed):
    skeleton, _ = parsed
    assert skeleton.joints["Chest"].offset == (0.0, 5.0, 0.0)
    assert skeleton.joints["Head"].offset == (0.0, 3.0, 0.0)


# ── 2. channels ───────────────────────────────────────────────────────────────


def test_root_channel_list_exact(parsed):
    skeleton, _ = parsed
    assert skeleton.joints["Hips"].channels == [
        "Xposition", "Yposition", "Zposition",
        "Zrotation", "Xrotation", "Yrotation",
    ]


def test_child_channels_are_three_rotations(parsed):
    skeleton, _ = parsed
    assert skeleton.joints["Chest"].channels == [
        "Zrotation", "Xrotation", "Yrotation"]
    assert skeleton.joints["Head"].rotation_channels == [
        "Zrotation", "Xrotation", "Yrotation"]
    assert skeleton.joints["Hips"].position_channels == [
        "Xposition", "Yposition", "Zposition"]
    assert skeleton.joints["Chest"].has_position is False


def test_flat_channel_order_layout(parsed):
    skeleton, _ = parsed
    order = skeleton.channel_order
    assert skeleton.num_channels == 12
    assert len(order) == 12
    # First 6 columns belong to the root, in declared order.
    assert order[:6] == [
        ("Hips", "Xposition"), ("Hips", "Yposition"), ("Hips", "Zposition"),
        ("Hips", "Zrotation"), ("Hips", "Xrotation"), ("Hips", "Yrotation"),
    ]
    # Next 3 -> Chest, last 3 -> Head.
    assert order[6:9] == [
        ("Chest", "Zrotation"), ("Chest", "Xrotation"), ("Chest", "Yrotation")]
    assert order[9:12] == [
        ("Head", "Zrotation"), ("Head", "Xrotation"), ("Head", "Yrotation")]


def test_channel_columns_map(parsed):
    skeleton, _ = parsed
    cols = skeleton.channel_columns()
    assert cols["Hips"]["Xposition"] == 0
    assert cols["Hips"]["Yrotation"] == 5
    assert cols["Chest"]["Zrotation"] == 6
    assert cols["Head"]["Yrotation"] == 11
    assert "Head_End" not in cols  # End Site has no channels


# ── 3. motion ─────────────────────────────────────────────────────────────────


def test_frame_count_and_time(parsed):
    _, motion = parsed
    assert motion.num_frames == 3
    assert motion.frame_time == pytest.approx(0.033333)
    assert motion.fps == pytest.approx(1.0 / 0.033333)


def test_frame_values(parsed):
    _, motion = parsed
    assert motion.frames[0] == [0.0] * 12
    assert motion.frames[1] == [
        1.0, 2.0, 3.0, 10.0, 20.0, 30.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    assert motion.frames[2] == [
        -1.0, -2.0, -3.0, -10.0, -20.0, -30.0, -1.0, -2.0, -3.0, -4.0, -5.0, -6.0]


# ── 4. rotation-order mapping (the subtle part) ───────────────────────────────


def test_rotate_order_is_reverse_of_channel_order():
    # BVH ZXY rotation channels -> Maya rotateOrder is the REVERSE -> "yxz".
    assert maya_rotate_order(
        ["Zrotation", "Xrotation", "Yrotation"]) == "yxz"
    assert maya_rotate_order_enum(
        ["Zrotation", "Xrotation", "Yrotation"]) == 4


@pytest.mark.parametrize(
    "channels, expected_order, expected_enum",
    [
        (["Xrotation", "Yrotation", "Zrotation"], "zyx", 5),
        (["Zrotation", "Yrotation", "Xrotation"], "xyz", 0),
        (["Yrotation", "Xrotation", "Zrotation"], "zxy", 2),
        (["Zrotation", "Xrotation", "Yrotation"], "yxz", 4),
    ],
)
def test_rotate_order_mapping_table(channels, expected_order, expected_enum):
    assert maya_rotate_order(channels) == expected_order
    assert maya_rotate_order_enum(channels) == expected_enum


def test_rotate_order_ignores_interleaved_position_channels():
    # A root with interleaved position + rotation channels: only the 3 rotation
    # channels drive the order; their reverse is used.
    channels = ["Xposition", "Yposition", "Zposition",
                "Zrotation", "Xrotation", "Yrotation"]
    joint_rot = [c for c in channels if c.endswith("rotation")]
    assert maya_rotate_order(joint_rot) == "yxz"


def test_rotate_order_rejects_incomplete_channels():
    with pytest.raises(BvhParseError):
        maya_rotate_order(["Zrotation", "Xrotation"])


# ── 5. robustness ─────────────────────────────────────────────────────────────


def test_crlf_and_extra_whitespace():
    # Same content with CRLF line endings and padded whitespace must parse
    # identically.
    crlf = BVH_TEXT.replace("\n", "\r\n")
    skeleton, motion = parse_bvh(crlf)
    assert list(skeleton.joints.keys()) == ["Hips", "Chest", "Head", "Head_End"]
    assert motion.num_frames == 3
    assert motion.frames[1][0] == 1.0


def test_parse_from_file_path(tmp_path):
    path = tmp_path / "clip.bvh"
    path.write_text(BVH_TEXT, encoding="utf-8")
    # Accept both a str path and an os.PathLike.
    skeleton_str, motion_str = parse_bvh(str(path))
    skeleton_p, motion_p = parse_bvh(path)
    assert skeleton_str.root.name == "Hips"
    assert skeleton_p.root.name == "Hips"
    assert motion_str.num_frames == motion_p.num_frames == 3


def test_missing_motion_section_raises():
    with pytest.raises(BvhParseError):
        parse_bvh("HIERARCHY\nROOT Hips\n{\nOFFSET 0 0 0\n}\n")


def test_frame_count_mismatch_raises():
    bad = BVH_TEXT.replace("Frames: 3", "Frames: 5")
    with pytest.raises(BvhParseError):
        parse_bvh(bad)


def test_frame_width_mismatch_raises():
    # Drop a value from a data row so it no longer matches the channel count.
    bad = BVH_TEXT.replace(
        "1.0 2.0 3.0   10 20 30   1 2 3   4 5 6",
        "1.0 2.0 3.0   10 20 30   1 2 3   4 5",  # 11 values, expected 12
    )
    with pytest.raises(BvhParseError):
        parse_bvh(bad)


def test_unknown_channel_raises():
    bad = BVH_TEXT.replace("Xposition Yposition Zposition", "Wposition Yposition Zposition")
    with pytest.raises(BvhParseError):
        parse_bvh(bad)


def test_missing_frame_time_raises():
    bad = BVH_TEXT.replace("Frame Time: 0.033333\n", "")
    with pytest.raises(BvhParseError):
        parse_bvh(bad)


# ── 6. Maya builder (only runs inside Maya) ───────────────────────────────────


@pytest.mark.skipif(
    not maya_available(), reason="maya.cmds unavailable (not running inside Maya)")
def test_build_in_maya_smoke():
    skeleton, motion = parse_bvh(BVH_TEXT)
    result = build_in_maya(skeleton, motion, namespace="bvhtest")
    assert result["root"].endswith("Hips")
    assert result["num_joints"] == 4
    assert result["num_frames"] == 3
    assert result["start_frame"] == 1
    assert result["end_frame"] == 3


def test_build_in_maya_raises_off_dcc():
    # Off-DCC, the guarded builder must fail fast with a clear message rather
    # than raising an ImportError deep in the call.
    if maya_available():
        pytest.skip("running inside Maya; off-DCC guard not applicable")
    skeleton, motion = parse_bvh(BVH_TEXT)
    with pytest.raises(RuntimeError):
        build_in_maya(skeleton, motion)
