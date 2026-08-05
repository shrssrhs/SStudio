"""Regression tests for Stage 3.4's default SStudio character visual rig
(character_rig.py) and its integration points in client_studio.py.

Three tiers, mirroring test_character_controller.py's own structure:

1. Pure math (select_animation_state, yaw_from_horizontal_velocity,
   step_facing_yaw, idle_pose/walk_pose/jump_pose/fall_pose, blend_poses) --
   no engine import at all beyond what character_rig.py itself needs for
   this tier (nothing -- these functions have zero Ursina dependency).

2. Real headless Ursina rig construction/behavior. Unlike
   test_character_controller.py's Bullet tier (which needs no window at
   all), CharacterVisualRig genuinely needs live Panda3D Entities, which
   in turn need an active Ursina app -- confirmed empirically that
   `Ursina(window_type='none')` builds a fully working headless app (no
   real window, no splash, no visible surface) in which Entity()
   construction, parenting, world_position, color, and enabled/stash-based
   visibility all behave identically to a normal windowed run. This tier
   is what proves the actual rig behaves correctly: hierarchy, appearance,
   accessories, controller-following, and animation-state selection.

3. Structural checks on client_studio.py's integration (via
   inspect.getsource(), same technique test_character_controller.py and
   test_legacy_content_removal.py already use) for the invariants that
   genuinely require a live MultiplayerGame/Ursina app to observe
   behaviorally: Play/Stop lifecycle wiring (exactly one rig created per
   Play, destroyed on every Stop), and that rig creation/update never
   dirties the Place, never touches Undo/Redo history, and never invokes
   the legacy PlayerVisual/player.glb path. The corresponding BEHAVIORAL
   claims (one rig spawns on Play, no duplicate/leaked rig across repeated
   Play/Stop, no rig survives Save Place, first/third-person visibly
   correct) are verified separately via the real Windows GUI test pass
   documented in the Stage 3.4 report -- keep that report's list in sync
   with what is/isn't covered here.

Follows this project's existing test convention: plain top-level-assertion
script, run directly, offscreen Qt platform, headless Ursina window.
"""
import inspect
import os
import re
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, '.')

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from ursina import Ursina

ursina_app = Ursina(window_type="none")

import character_rig as cr
import client_studio as cs

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"ok: {message}")


def _colors_close(a, b, tolerance: float = 1e-3) -> bool:
    """Panda3D stores color components as float32 internally, so a Python
    float assigned via Entity.color round-trips through a lower-precision
    representation (e.g. 0.1 -> 0.10000000149011612) -- exact tuple
    equality would spuriously fail even when nothing is actually wrong."""
    a, b = tuple(a), tuple(b)
    return len(a) == len(b) and all(abs(x - y) < tolerance for x, y in zip(a, b))


# ============================================================
# TIER 1: pure math -- no Ursina involved in the assertions themselves
# ============================================================

def test_select_animation_state_idle() -> None:
    state = cr.select_animation_state(horizontal_speed=0.0, grounded=True, vertical_velocity=0.0)
    check(state == cr.ANIM_IDLE, "select_animation_state: stationary + grounded -> Idle")


def test_select_animation_state_walk() -> None:
    state = cr.select_animation_state(horizontal_speed=2.0, grounded=True, vertical_velocity=0.0)
    check(state == cr.ANIM_WALK, "select_animation_state: moving + grounded -> Walk")


def test_select_animation_state_walk_threshold() -> None:
    below = cr.select_animation_state(cr.WALK_SPEED_THRESHOLD - 0.05, True, 0.0)
    above = cr.select_animation_state(cr.WALK_SPEED_THRESHOLD + 0.05, True, 0.0)
    check(below == cr.ANIM_IDLE and above == cr.ANIM_WALK, "select_animation_state: WALK_SPEED_THRESHOLD is the exact Idle/Walk boundary")


def test_select_animation_state_jump() -> None:
    state = cr.select_animation_state(horizontal_speed=0.0, grounded=False, vertical_velocity=5.0)
    check(state == cr.ANIM_JUMP, "select_animation_state: airborne + rising velocity -> Jump")


def test_select_animation_state_fall() -> None:
    state = cr.select_animation_state(horizontal_speed=0.0, grounded=False, vertical_velocity=cr.FALL_VELOCITY_THRESHOLD - 1.0)
    check(state == cr.ANIM_FALL, "select_animation_state: airborne + falling velocity below threshold -> Fall")


def test_select_animation_state_diagonal_speed_not_distorted() -> None:
    """A diagonal horizontal_speed of magnitude X must select the same
    state as a straight horizontal_speed of the same magnitude X --
    select_animation_state() only ever sees the already-normalized scalar
    speed, never raw per-axis components, so there is nothing for
    "diagonal" to distort in the first place."""
    straight = cr.select_animation_state(3.0, True, 0.0)
    diagonal_same_magnitude = cr.select_animation_state((3.0**2 + 0.0**2) ** 0.5, True, 0.0)
    check(straight == diagonal_same_magnitude == cr.ANIM_WALK, "select_animation_state: state selection depends only on speed magnitude")


def test_yaw_from_horizontal_velocity_matches_controller_convention() -> None:
    # character_controller.yaw_only_forward(0) == (0,0,1) i.e. +Z; yaw 90 == (1,0,0) i.e. +X.
    yaw_forward = cr.yaw_from_horizontal_velocity(0.0, 1.0)
    yaw_right = cr.yaw_from_horizontal_velocity(1.0, 0.0)
    check(abs(yaw_forward - 0.0) < 1e-6, "yaw_from_horizontal_velocity: +Z velocity -> yaw 0 (matches character_controller's forward convention)")
    check(abs(yaw_right - 90.0) < 1e-6, "yaw_from_horizontal_velocity: +X velocity -> yaw 90 (matches character_controller's right convention)")


def test_yaw_from_horizontal_velocity_below_min_speed_is_none() -> None:
    result = cr.yaw_from_horizontal_velocity(0.01, 0.01, min_speed=cr.MIN_FACING_SPEED)
    check(result is None, "yaw_from_horizontal_velocity: near-zero velocity returns None (caller keeps last facing)")


def test_step_facing_yaw_reaches_target_without_overshoot() -> None:
    yaw = 0.0
    for _ in range(200):
        yaw = cr.step_facing_yaw(yaw, 90.0, dt=1 / 60)
    check(abs(yaw - 90.0) < 1e-6, "step_facing_yaw: converges exactly to target over many steps, no overshoot")


def test_step_facing_yaw_shortest_path() -> None:
    # From 170 to -170 (i.e. 350), the shortest path is +20 degrees, not -340.
    result = cr.step_facing_yaw(170.0, -170.0, dt=1.0, turn_speed_degrees=1000.0)
    check(abs(result - (-170.0)) < 1e-6, "step_facing_yaw: takes the shortest angular path across the +-180 wrap")


def test_idle_pose_low_amplitude() -> None:
    pose = cr.idle_pose(cr.math.pi / 2)
    max_angle = max(abs(a) for angles in pose.values() for a in angles)
    check(max_angle < 10.0, "idle_pose: stays low-amplitude ('no excessive motion')")


def test_walk_pose_opposite_arm_leg_swing() -> None:
    pose = cr.walk_pose(cr.math.pi / 2)
    left_arm = pose[cr.PART_LEFT_UPPER_ARM][0]
    right_arm = pose[cr.PART_RIGHT_UPPER_ARM][0]
    left_leg = pose[cr.PART_LEFT_UPPER_LEG][0]
    right_leg = pose[cr.PART_RIGHT_UPPER_LEG][0]
    check(left_arm == -right_arm and left_arm != 0.0, "walk_pose: left/right arm swing are mirrored opposites")
    check(left_leg == -right_leg and left_leg != 0.0, "walk_pose: left/right leg swing are mirrored opposites")
    check((left_arm > 0) != (left_leg > 0), "walk_pose: same-side arm and leg swing in opposite directions (natural walk gait)")


def test_jump_pose_distinct_from_fall_pose() -> None:
    jump = cr.jump_pose()
    fall = cr.fall_pose()
    check(jump != fall, "jump_pose() and fall_pose() are distinct poses")
    check(jump[cr.PART_LEFT_UPPER_LEG] != (0.0, 0.0, 0.0), "jump_pose: legs move into a readable jump pose")


def test_blend_poses_interpolates() -> None:
    a = {name: (0.0, 0.0, 0.0) for name in cr.POSABLE_PARTS}
    b = {name: (10.0, 0.0, 0.0) for name in cr.POSABLE_PARTS}
    mid = cr.blend_poses(a, b, 0.5)
    check(all(abs(mid[name][0] - 5.0) < 1e-9 for name in cr.POSABLE_PARTS), "blend_poses: t=0.5 is the exact midpoint")
    check(cr.blend_poses(a, b, 0.0) == a, "blend_poses: t=0.0 returns pose_a")
    check(cr.blend_poses(a, b, 1.0) == b, "blend_poses: t=1.0 returns pose_b")


def test_ease_smoothstep_endpoints_and_clamping() -> None:
    check(cr.ease_smoothstep(0.0) == 0.0 and cr.ease_smoothstep(1.0) == 1.0, "ease_smoothstep: exact endpoints")
    check(cr.ease_smoothstep(-1.0) == 0.0 and cr.ease_smoothstep(2.0) == 1.0, "ease_smoothstep: clamps outside [0,1]")


def test_default_appearance_matches_spec_direction() -> None:
    appearance = cr.default_appearance()
    check(isinstance(appearance, cr.CharacterAppearance), "default_appearance() returns a CharacterAppearance")
    check(appearance.head_accessory is None and appearance.neck_accessory is None and appearance.back_accessory is None, "default_appearance(): no accessories attached by default")


# ============================================================
# TIER 2: real headless Ursina rig construction/behavior
# ============================================================

def test_rig_construction_has_all_required_parts() -> None:
    rig = cr.CharacterVisualRig()
    try:
        missing = [name for name in cr.REQUIRED_PART_NAMES if name not in rig.parts]
        check(not missing, f"CharacterVisualRig: all REQUIRED_PART_NAMES present, missing={missing}")
        check(len(rig.parts) == len(cr.REQUIRED_PART_NAMES), "CharacterVisualRig: parts dict has exactly the required part count")
    finally:
        rig.destroy()


def test_rig_hierarchy_nesting() -> None:
    rig = cr.CharacterVisualRig()
    try:
        check(rig.parts[cr.PART_PELVIS].parent == rig.parts[cr.PART_ROOT], "hierarchy: pelvis parented to root")
        check(rig.parts[cr.PART_TORSO].parent == rig.parts[cr.PART_PELVIS], "hierarchy: torso parented to pelvis")
        check(rig.parts[cr.PART_HEAD].parent == rig.parts[cr.PART_TORSO], "hierarchy: head parented to torso")
        check(rig.parts[cr.PART_SCREEN].parent == rig.parts[cr.PART_HEAD], "hierarchy: screen parented to head")
        check(rig.parts[cr.PART_LEFT_LOWER_ARM].parent == rig.parts[cr.PART_LEFT_UPPER_ARM], "hierarchy: lower arm parented to upper arm (elbow joint)")
        check(rig.parts[cr.PART_LEFT_LOWER_LEG].parent == rig.parts[cr.PART_LEFT_UPPER_LEG], "hierarchy: lower leg parented to upper leg (knee joint)")
        check(rig.parts[cr.PART_LEFT_UPPER_LEG].parent == rig.parts[cr.PART_PELVIS], "hierarchy: upper leg parented to pelvis (hip joint)")
    finally:
        rig.destroy()


def test_default_appearance_application() -> None:
    rig = cr.CharacterVisualRig()
    try:
        appearance = rig.appearance
        check(_colors_close(rig._torso_mesh.color, appearance.vest_color), "appearance: torso mesh colored with vest_color")
        check(_colors_close(rig._screen_mesh.color, appearance.screen_color), "appearance: screen mesh colored with screen_color")
        check(_colors_close(rig._screen_accent.color, appearance.accent_color), "appearance: screen accent colored with accent_color")
    finally:
        rig.destroy()


def test_accent_color_propagation() -> None:
    rig = cr.CharacterVisualRig()
    try:
        custom = cr.default_appearance()
        custom.accent_color = (0.1, 0.2, 0.3, 1.0)
        rig.set_appearance(custom)
        check(_colors_close(rig._screen_accent.color, (0.1, 0.2, 0.3, 1.0)), "set_appearance: accent_color propagates to the screen accent entity")
    finally:
        rig.destroy()


def test_screen_color_propagation() -> None:
    rig = cr.CharacterVisualRig()
    try:
        custom = cr.default_appearance()
        custom.screen_color = (0.4, 0.5, 0.6, 1.0)
        rig.set_appearance(custom)
        check(_colors_close(rig._screen_mesh.color, (0.4, 0.5, 0.6, 1.0)), "set_appearance: screen_color propagates to the screen mesh entity")
    finally:
        rig.destroy()


def test_clothing_colors_propagation() -> None:
    rig = cr.CharacterVisualRig()
    try:
        custom = cr.default_appearance()
        custom.shirt_color = (0.9, 0.9, 0.9, 1.0)
        custom.vest_color = (0.05, 0.05, 0.05, 1.0)
        custom.trouser_color = (0.2, 0.2, 0.2, 1.0)
        custom.shoe_color = (0.0, 0.0, 0.0, 1.0)
        rig.set_appearance(custom)
        check(_colors_close(rig._shirt.color, (0.9, 0.9, 0.9, 1.0)), "set_appearance: shirt_color propagates")
        check(_colors_close(rig._torso_mesh.color, (0.05, 0.05, 0.05, 1.0)), "set_appearance: vest_color propagates to torso")
        check(_colors_close(rig._limb_meshes["left_upper_leg"].color, (0.2, 0.2, 0.2, 1.0)), "set_appearance: trouser_color propagates to legs")
        check(_colors_close(rig._left_shoe.color, (0.0, 0.0, 0.0, 1.0)), "set_appearance: shoe_color propagates")
    finally:
        rig.destroy()


def test_accessory_slots_exist() -> None:
    rig = cr.CharacterVisualRig()
    try:
        for slot in cr.ACCESSORY_SLOT_NAMES:
            check(slot in rig.parts, f"accessory slot '{slot}' exists as an addressable part")
            check(rig.accessory_at(slot) is None, f"accessory slot '{slot}' starts empty")
    finally:
        rig.destroy()


def test_fedora_attach_and_remove() -> None:
    rig = cr.CharacterVisualRig()
    try:
        before = rig.entity_count()
        fedora = rig.attach_accessory(cr.PART_HEAD_ACCESSORY_SLOT, "fedora")
        check(fedora.parent == rig.parts[cr.PART_HEAD_ACCESSORY_SLOT], "attach_accessory: fedora parented to the head accessory slot")
        check(rig.entity_count() > before, "attach_accessory: entity_count increases after attaching")
        check(rig.accessory_at(cr.PART_HEAD_ACCESSORY_SLOT) is fedora, "accessory_at: returns the attached fedora entity")
        rig.remove_accessory(cr.PART_HEAD_ACCESSORY_SLOT)
        check(rig.entity_count() == before, "remove_accessory: entity_count returns to its pre-attach value")
        check(rig.accessory_at(cr.PART_HEAD_ACCESSORY_SLOT) is None, "remove_accessory: slot is empty again")
    finally:
        rig.destroy()


def test_attach_accessory_rejects_unknown_slot_and_kind() -> None:
    rig = cr.CharacterVisualRig()
    try:
        raised_for_slot = False
        try:
            rig.attach_accessory(cr.PART_TORSO, "fedora")
        except ValueError:
            raised_for_slot = True
        check(raised_for_slot, "attach_accessory: raises ValueError for a non-accessory-slot part name")

        raised_for_kind = False
        try:
            rig.attach_accessory(cr.PART_HEAD_ACCESSORY_SLOT, "not-a-real-accessory")
        except ValueError:
            raised_for_kind = True
        check(raised_for_kind, "attach_accessory: raises ValueError for an unknown accessory kind")
    finally:
        rig.destroy()


def test_root_follows_controller_position() -> None:
    rig = cr.CharacterVisualRig()
    try:
        rig.update(1 / 60, (5.0, 1.5, -3.0), (0.0, 0.0), True, 0.0)
        pos = rig.parts[cr.PART_ROOT].position
        check((round(pos.x, 6), round(pos.y, 6), round(pos.z, 6)) == (5.0, 1.5, -3.0), "update(): root position exactly matches the fed feet_position, no lag")
    finally:
        rig.destroy()


def test_body_yaw_follows_movement_direction() -> None:
    rig = cr.CharacterVisualRig(initial_yaw_degrees=0.0)
    try:
        for _ in range(120):
            rig.update(1 / 60, (0.0, 0.0, 0.0), (1.0, 0.0), True, 0.0)
        yaw = rig.parts[cr.PART_ROOT].rotation.y
        check(abs(yaw - 90.0) < 1.0, f"update(): body yaw converges toward movement direction (+X -> ~90deg), got {yaw}")
    finally:
        rig.destroy()


def test_stationary_facing_preserved() -> None:
    rig = cr.CharacterVisualRig(initial_yaw_degrees=0.0)
    try:
        for _ in range(120):
            rig.update(1 / 60, (0.0, 0.0, 0.0), (1.0, 0.0), True, 0.0)
        moving_yaw = rig.parts[cr.PART_ROOT].rotation.y
        for _ in range(30):
            rig.update(1 / 60, (0.0, 0.0, 0.0), (0.0, 0.0), True, 0.0)
        stationary_yaw = rig.parts[cr.PART_ROOT].rotation.y
        check(abs(stationary_yaw - moving_yaw) < 1e-6, "update(): facing yaw unchanged while stationary (last meaningful facing preserved)")
    finally:
        rig.destroy()


def test_camera_pitch_never_applied_to_root() -> None:
    """The rig has no pitch input at all -- update()'s signature doesn't
    even accept one -- so camera pitch structurally cannot tilt the body,
    not just "isn't currently wired to.\""""
    params = list(inspect.signature(cr.CharacterVisualRig.update).parameters)
    check("pitch" not in " ".join(params).lower(), "CharacterVisualRig.update(): signature has no pitch parameter at all")


def test_idle_state_selected_when_stationary() -> None:
    rig = cr.CharacterVisualRig()
    try:
        rig.update(1 / 60, (0, 0, 0), (0.0, 0.0), True, 0.0)
        check(rig._state == cr.ANIM_IDLE, "update(): Idle selected while stationary and grounded")
    finally:
        rig.destroy()


def test_walk_state_selected_when_moving() -> None:
    rig = cr.CharacterVisualRig()
    try:
        rig.update(1 / 60, (0, 0, 0), (2.0, 0.0), True, 0.0)
        check(rig._state == cr.ANIM_WALK, "update(): Walk selected while moving and grounded")
    finally:
        rig.destroy()


def test_jump_state_selected_when_rising() -> None:
    rig = cr.CharacterVisualRig()
    try:
        rig.update(1 / 60, (0, 0, 0), (0.0, 0.0), False, 5.0)
        check(rig._state == cr.ANIM_JUMP, "update(): Jump selected while airborne and rising")
    finally:
        rig.destroy()


def test_fall_state_selected_when_falling() -> None:
    rig = cr.CharacterVisualRig()
    try:
        rig.update(1 / 60, (0, 0, 0), (0.0, 0.0), False, cr.FALL_VELOCITY_THRESHOLD - 2.0)
        check(rig._state == cr.ANIM_FALL, "update(): Fall selected while airborne and falling fast")
    finally:
        rig.destroy()


def test_transition_is_smoothed_not_instant() -> None:
    rig = cr.CharacterVisualRig()
    try:
        rig.update(1 / 60, (0, 0, 0), (0.0, 0.0), True, 0.0)  # settle into Idle
        for _ in range(30):
            rig.update(1 / 60, (0, 0, 0), (0.0, 0.0), True, 0.0)
        rig.update(1 / 60, (0, 0, 0), (3.0, 0.0), True, 0.0)  # first Walk frame -- transition just started
        check(rig._transition_t < 1.0, "update(): a state change starts a blend (transition_t < 1.0) rather than snapping instantly")
        arm_first_frame = rig.parts[cr.PART_LEFT_UPPER_ARM].rotation.x
        for _ in range(30):
            rig.update(1 / 60, (0, 0, 0), (3.0, 0.0), True, 0.0)
        check(rig._transition_t >= 1.0, "update(): transition completes (transition_t reaches 1.0) after TRANSITION_DURATION")
    finally:
        rig.destroy()


def test_diagonal_movement_does_not_speed_up_walk_phase() -> None:
    """A diagonal (vx, vz) with the same MAGNITUDE as a straight-axis
    velocity must advance the walk-cycle phase at the same rate --
    verifies the actual per-frame update() path, not just the pure
    select_animation_state() function above."""
    straight_rig = cr.CharacterVisualRig()
    diagonal_rig = cr.CharacterVisualRig()
    try:
        speed = 3.0
        diagonal_component = speed / (2.0 ** 0.5)
        for _ in range(10):
            straight_rig.update(1 / 60, (0, 0, 0), (speed, 0.0), True, 0.0)
            diagonal_rig.update(1 / 60, (0, 0, 0), (diagonal_component, diagonal_component), True, 0.0)
        check(abs(straight_rig._phase - diagonal_rig._phase) < 1e-6, "update(): diagonal movement of equal magnitude advances the walk phase identically to straight movement")
    finally:
        straight_rig.destroy()
        diagonal_rig.destroy()


def test_first_person_hides_head() -> None:
    rig = cr.CharacterVisualRig()
    try:
        rig.set_first_person(True)
        check(rig.parts[cr.PART_TORSO].enabled is False, "set_first_person(True): torso (and its head/screen descendants) disabled")
    finally:
        rig.destroy()


def test_third_person_shows_full_rig() -> None:
    rig = cr.CharacterVisualRig()
    try:
        rig.set_first_person(True)
        rig.set_first_person(False)
        check(rig.parts[cr.PART_TORSO].enabled is True, "set_first_person(False): torso (and full rig) visible again")
    finally:
        rig.destroy()


def test_destroy_is_idempotent() -> None:
    rig = cr.CharacterVisualRig()
    rig.destroy()
    try:
        rig.destroy()
        check(True, "destroy(): calling twice does not raise")
    except Exception as exc:
        check(False, f"destroy(): calling twice raised {exc!r}")


def test_repeated_construct_destroy_does_not_leak_reachable_entities() -> None:
    for _ in range(5):
        rig = cr.CharacterVisualRig()
        rig.update(1 / 60, (0, 0, 0), (1.0, 0.0), True, 0.0)
        rig.attach_accessory(cr.PART_HEAD_ACCESSORY_SLOT, "cap")
        root_node = rig.parts[cr.PART_ROOT]
        rig.destroy()
        # destroy() calls Panda3D's NodePath.removeNode() immediately (see
        # ursina.destroy._destroy) -- isEmpty() is the reliable immediate
        # post-condition; entity.parent is NOT reset by destroy() (only
        # its scene-graph node is removed), so checking .parent here would
        # spuriously pass/fail on stale Python-side bookkeeping instead of
        # actual Panda3D node state.
        check(root_node.isEmpty(), "repeated construct/destroy: destroyed root's NodePath is empty (actually removed)")
    check(True, "repeated construct/destroy x5 completed without exception")


# ============================================================
# TIER 3: structural checks on client_studio.py's integration
# ============================================================

def _source(func) -> str:
    return inspect.getsource(func)


def test_start_character_constructs_exactly_one_rig() -> None:
    src = _source(cs.MultiplayerGame._start_character)
    check(src.count("character_rig.CharacterVisualRig(") == 1, "_start_character(): constructs exactly one CharacterVisualRig")
    check("self._character_visual = visual" in src, "_start_character(): stores the rig on self._character_visual")


def test_stop_character_destroys_visual_and_runtime() -> None:
    src = _source(cs.MultiplayerGame._stop_character)
    check("self._character_visual.destroy()" in src, "_stop_character(): destroys the visual rig")
    check("self._character_visual = None" in src, "_stop_character(): clears self._character_visual")
    check("self._character_runtime.destroy()" in src, "_stop_character(): still destroys the controller runtime (Stage 3.3 path unchanged)")


def test_start_character_failure_path_also_cleans_up_visual() -> None:
    src = _source(cs.MultiplayerGame._start_character)
    check("visual.destroy()" in src, "_start_character(): exception handler also destroys any partially-created visual rig")


def test_sync_character_camera_feeds_feet_position() -> None:
    src = _source(cs.MultiplayerGame.sync_character_camera)
    check("visual.update(" in src, "sync_character_camera(): calls visual.update() each frame")
    check("half_height" in src, "sync_character_camera(): computes a feet-adjusted position using controller.half_height")


def test_export_world_never_reads_character_visual() -> None:
    src = _source(cs.MultiplayerStudioAdapter.export_world)
    check("_character_visual" not in src, "export_world() (used by Save Place) never reads _character_visual")
    check("_character_runtime" not in src, "export_world() (used by Save Place) never reads _character_runtime")


def test_start_stop_character_never_dirty_place_or_touch_history() -> None:
    for fn in (cs.MultiplayerGame._start_character, cs.MultiplayerGame._stop_character, cs.MultiplayerGame.sync_character_camera):
        src = _source(fn)
        check("mark_place_dirty" not in src, f"{fn.__name__}(): never calls mark_place_dirty()")
        check(".history." not in src, f"{fn.__name__}(): never touches editor Undo/Redo history")


def test_character_rig_module_never_touches_legacy_playervisual() -> None:
    import character_rig
    module_src = inspect.getsource(character_rig)
    # Every docstring (module- AND class-level) is allowed to MENTION
    # PlayerVisual/player.glb by name (explaining what this module
    # deliberately does NOT reuse) -- so strip every triple-quoted string
    # before checking the actual executable body for any reference.
    body_src = re.sub(r'""".*?"""', "", module_src, flags=re.DOTALL)
    check("PlayerVisual(" not in body_src, "character_rig.py: never constructs PlayerVisual")
    check("player.glb" not in body_src, "character_rig.py: never references player.glb")
    check("RemotePlayer(" not in body_src, "character_rig.py: never constructs RemotePlayer")


def test_start_character_never_touches_legacy_playervisual() -> None:
    src = _source(cs.MultiplayerGame._start_character)
    check("PlayerVisual(" not in src, "_start_character(): never constructs the legacy PlayerVisual")
    check("player.glb" not in src, "_start_character(): never references player.glb")
    check("load_fresh_model_node" not in src, "_start_character(): never invokes the player.glb loader")


def test_toggle_third_person_prefers_new_rig() -> None:
    src = _source(cs.MultiplayerGame.toggle_third_person)
    check("self._character_visual" in src, "toggle_third_person(): checks the new rig first")
    check("set_first_person" in src, "toggle_third_person(): drives visibility via set_first_person(), not the legacy local_visual.enabled toggle for the new rig path")


def test_v_key_wired_in_play_mode_input() -> None:
    src = _source(cs.MultiplayerGame.input)
    check('key == "v"' in src and "toggle_third_person" in src, "input(): 'v' key routes to toggle_third_person() in Play mode")


test_select_animation_state_idle()
test_select_animation_state_walk()
test_select_animation_state_walk_threshold()
test_select_animation_state_jump()
test_select_animation_state_fall()
test_select_animation_state_diagonal_speed_not_distorted()
test_yaw_from_horizontal_velocity_matches_controller_convention()
test_yaw_from_horizontal_velocity_below_min_speed_is_none()
test_step_facing_yaw_reaches_target_without_overshoot()
test_step_facing_yaw_shortest_path()
test_idle_pose_low_amplitude()
test_walk_pose_opposite_arm_leg_swing()
test_jump_pose_distinct_from_fall_pose()
test_blend_poses_interpolates()
test_ease_smoothstep_endpoints_and_clamping()
test_default_appearance_matches_spec_direction()

test_rig_construction_has_all_required_parts()
test_rig_hierarchy_nesting()
test_default_appearance_application()
test_accent_color_propagation()
test_screen_color_propagation()
test_clothing_colors_propagation()
test_accessory_slots_exist()
test_fedora_attach_and_remove()
test_attach_accessory_rejects_unknown_slot_and_kind()
test_root_follows_controller_position()
test_body_yaw_follows_movement_direction()
test_stationary_facing_preserved()
test_camera_pitch_never_applied_to_root()
test_idle_state_selected_when_stationary()
test_walk_state_selected_when_moving()
test_jump_state_selected_when_rising()
test_fall_state_selected_when_falling()
test_transition_is_smoothed_not_instant()
test_diagonal_movement_does_not_speed_up_walk_phase()
test_first_person_hides_head()
test_third_person_shows_full_rig()
test_destroy_is_idempotent()
test_repeated_construct_destroy_does_not_leak_reachable_entities()

test_start_character_constructs_exactly_one_rig()
test_stop_character_destroys_visual_and_runtime()
test_start_character_failure_path_also_cleans_up_visual()
test_sync_character_camera_feeds_feet_position()
test_export_world_never_reads_character_visual()
test_start_stop_character_never_dirty_place_or_touch_history()
test_character_rig_module_never_touches_legacy_playervisual()
test_start_character_never_touches_legacy_playervisual()
test_toggle_third_person_prefers_new_rig()
test_v_key_wired_in_play_mode_input()

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for message in FAILURES:
        print(f"  - {message}")
    sys.exit(1)
print("All character rig tests passed.")
