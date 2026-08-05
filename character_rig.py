"""
Stage 3.4: default SStudio character visual rig.

Scope (see Stage 3.4 spec): this module implements the VISUAL layer that
sits on top of the Stage 3.3 invisible capsule controller
(character_controller.CharacterController) -- a procedural, primitive-built
humanoid rig, a small procedural animation state machine, and a
configurable-but-not-yet-persisted appearance. It never drives physics and
never becomes an authoritative Place Instance.

Architecture (mirrors character_controller.py's split between pure math and
engine-bound state):

    CharacterVisualRig              <- one per Play session, Ursina-bound
    +-- CharacterAppearance          <- plain data (colors + accessory slots)
    +-- select_animation_state()     <- pure function, no Ursina import
    +-- yaw_from_horizontal_velocity()/step_facing_yaw()  <- pure math
    +-- idle_pose()/walk_pose()/jump_pose()/fall_pose()/blend_poses()
                                      <- pure functions, no Ursina import

Everything above the "PURE" section header has no Panda3D/Ursina/Qt import
at all and is fully testable in isolation, exactly like
character_controller.py's CharacterInputState/CharacterCamera/movement math.
Only CharacterVisualRig itself (and the small accessory-builder helpers it
uses) touches Ursina Entities.

Nothing here owns Qt widgets, PlaceManager, editor history/Undo state, Lua
VM state, or serialized Instance records, and nothing here is added to
MultiplayerGame.instances -- client_studio.py is solely responsible for
deciding IF/WHEN a CharacterVisualRig exists for the current Play session
(see MultiplayerGame._start_character()/_stop_character()) and for feeding
it position/velocity/grounded state read from the existing
character_controller.CharacterController each frame. This module never
reads CharacterController itself, and never touches the Bullet world --
the rig ONLY ever follows; it never influences movement, collision, or
Play/Stop lifecycle sequencing.

NOT a rewrite of player.glb or the legacy PickADoor PlayerVisual: this is a
fully original, primitive-built design (see CharacterVisualRig's
docstring), with no shared code path with PlayerVisual (client_studio.py's
--legacy-demo third-person avatar) or RemotePlayer.

NETWORK BOUNDARY (local-only limitation, same as character_controller.py):
everything in this module is local-player visual state only. No limb
transform, appearance, or accessory is ever sent through UPDATE_PROPERTY or
any other scene-edit message, never becomes a persistent Instance in the
authoritative Place world, and is never broadcast to other clients. This
stage does not implement remote-player visual snapshots, server-assigned
accent colors, or appearance persistence -- CharacterVisualRig is built so
those can be layered on top later (a future networked-appearance system
would read/write a CharacterAppearance the same way set_appearance() does,
without needing to change this module's rig-construction code).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ursina import Cylinder, Entity, Vec3, color, destroy

# ============================================================
# STABLE PART NAMES
# ============================================================
# Every visual part is addressable by one of these stable internal names --
# NEVER an editor Instance ID (this rig is never a Place Instance at all).

PART_ROOT = "root"
PART_PELVIS = "pelvis"
PART_TORSO = "torso"
PART_HEAD = "head"
PART_SCREEN = "screen"
PART_LEFT_UPPER_ARM = "left_upper_arm"
PART_LEFT_LOWER_ARM = "left_lower_arm"
PART_RIGHT_UPPER_ARM = "right_upper_arm"
PART_RIGHT_LOWER_ARM = "right_lower_arm"
PART_LEFT_UPPER_LEG = "left_upper_leg"
PART_LEFT_LOWER_LEG = "left_lower_leg"
PART_RIGHT_UPPER_LEG = "right_upper_leg"
PART_RIGHT_LOWER_LEG = "right_lower_leg"
PART_HEAD_ACCESSORY_SLOT = "head_accessory_slot"
PART_NECK_ACCESSORY_SLOT = "neck_accessory_slot"
PART_BACK_ACCESSORY_SLOT = "back_accessory_slot"

REQUIRED_PART_NAMES = (
    PART_ROOT,
    PART_PELVIS,
    PART_TORSO,
    PART_HEAD,
    PART_SCREEN,
    PART_LEFT_UPPER_ARM,
    PART_LEFT_LOWER_ARM,
    PART_RIGHT_UPPER_ARM,
    PART_RIGHT_LOWER_ARM,
    PART_LEFT_UPPER_LEG,
    PART_LEFT_LOWER_LEG,
    PART_RIGHT_UPPER_LEG,
    PART_RIGHT_LOWER_LEG,
    PART_HEAD_ACCESSORY_SLOT,
    PART_NECK_ACCESSORY_SLOT,
    PART_BACK_ACCESSORY_SLOT,
)

ACCESSORY_SLOT_NAMES = (PART_HEAD_ACCESSORY_SLOT, PART_NECK_ACCESSORY_SLOT, PART_BACK_ACCESSORY_SLOT)

# Parts a pose dict may set a rotation for -- root/pelvis/head/screen/slots
# are deliberately excluded (they never animate procedurally in this
# stage; root's rotation is driven by facing yaw only, see
# CharacterVisualRig.update()).
POSABLE_PARTS = (
    PART_TORSO,
    PART_LEFT_UPPER_ARM,
    PART_LEFT_LOWER_ARM,
    PART_RIGHT_UPPER_ARM,
    PART_RIGHT_LOWER_ARM,
    PART_LEFT_UPPER_LEG,
    PART_LEFT_LOWER_LEG,
    PART_RIGHT_UPPER_LEG,
    PART_RIGHT_LOWER_LEG,
)


# ============================================================
# PURE: ANIMATION STATE SELECTION (no Ursina import)
# ============================================================

ANIM_IDLE = "idle"
ANIM_WALK = "walk"
ANIM_JUMP = "jump"
ANIM_FALL = "fall"

# Horizontal speed above which Walk is selected over Idle while grounded.
WALK_SPEED_THRESHOLD = 0.5
# Vertical velocity below which an airborne character counts as Fall
# rather than the rising/apex portion of a Jump.
FALL_VELOCITY_THRESHOLD = -1.0


def select_animation_state(horizontal_speed: float, grounded: bool, vertical_velocity: float) -> str:
    """Pure state-selection rule, deliberately mirroring
    CharacterController's own grounded/velocity semantics (Stage 3.3) so
    the visual rig's state always agrees with the physical controller's
    actual motion -- no separate "is the character moving" heuristic."""
    if not grounded:
        return ANIM_FALL if vertical_velocity < FALL_VELOCITY_THRESHOLD else ANIM_JUMP
    return ANIM_WALK if horizontal_speed > WALK_SPEED_THRESHOLD else ANIM_IDLE


# ============================================================
# PURE: FACING DIRECTION (no Ursina import)
# ============================================================

# Below this horizontal speed, the current velocity direction is treated
# as noise rather than deliberate movement -- the rig keeps its last
# meaningful facing instead of snapping toward a near-zero vector.
MIN_FACING_SPEED = 0.15
# How fast the body yaw turns to catch up to the movement-direction target,
# in degrees/second -- fast enough to feel responsive, slow enough that a
# quick direction reversal doesn't look like an instant snap.
DEFAULT_TURN_SPEED_DEGREES = 720.0


def yaw_from_horizontal_velocity(vx: float, vz: float, min_speed: float = MIN_FACING_SPEED) -> Optional[float]:
    """Facing yaw (degrees) matching character_controller.yaw_only_forward()'s
    own convention (forward = (sin(yaw), 0, cos(yaw))), so a yaw value
    computed here always means the same facing direction as the capsule's
    own movement math. Returns None when horizontal speed is below
    min_speed -- the caller must then keep the previous facing yaw
    unchanged (Stage 3.4 spec: "when stationary, preserve the last
    meaningful facing direction")."""
    if math.hypot(vx, vz) < min_speed:
        return None
    return math.degrees(math.atan2(vx, vz))


def step_facing_yaw(current_yaw: float, target_yaw: float, dt: float, turn_speed_degrees: float = DEFAULT_TURN_SPEED_DEGREES) -> float:
    """Turns current_yaw toward target_yaw by at most turn_speed_degrees*dt,
    always via the SHORTEST angular path (never the long way around)."""
    if dt <= 0.0:
        return current_yaw
    diff = ((target_yaw - current_yaw + 180.0) % 360.0) - 180.0
    max_step = turn_speed_degrees * dt
    if abs(diff) <= max_step:
        return target_yaw
    return current_yaw + (max_step if diff > 0.0 else -max_step)


# ============================================================
# PURE: PROCEDURAL POSES (no Ursina import)
# ============================================================

IDLE_BREATH_PERIOD = 3.2  # seconds per full idle breathing cycle
IDLE_TORSO_BREATH_DEGREES = 1.6
IDLE_ARM_SWAY_DEGREES = 3.0

# Radians of walk-cycle phase advanced per second, per unit (m/s) of
# horizontal speed -- keying animation speed to actual movement speed
# (a scalar) rather than to raw per-axis input means diagonal movement
# (already speed-capped/normalized by character_controller's own
# compute_target_velocity) never animates faster than straight movement
# of the same speed (Stage 3.4 spec: "diagonal movement does not distort
# animation speed").
WALK_PHASE_SPEED = 2.4
WALK_ARM_SWING_DEGREES = 32.0
WALK_LEG_SWING_DEGREES = 28.0
WALK_ELBOW_BEND_DEGREES = 18.0

JUMP_ARM_DEGREES = -50.0
JUMP_LEG_TUCK_DEGREES = 24.0
JUMP_KNEE_BEND_DEGREES = 40.0

FALL_ARM_DEGREES = -15.0
FALL_LEG_DEGREES = 8.0

# How long a blended transition between two animation states takes.
TRANSITION_DURATION = 0.15


def _zero_pose() -> dict[str, tuple[float, float, float]]:
    return {name: (0.0, 0.0, 0.0) for name in POSABLE_PARTS}


def idle_pose(phase: float) -> dict[str, tuple[float, float, float]]:
    """Subtle breathing torso motion + small arm sway -- deliberately low
    amplitude (Stage 3.4 spec: "no excessive motion")."""
    pose = _zero_pose()
    breathe = math.sin(phase) * IDLE_TORSO_BREATH_DEGREES
    sway = math.sin(phase) * IDLE_ARM_SWAY_DEGREES
    pose[PART_TORSO] = (breathe, 0.0, 0.0)
    pose[PART_LEFT_UPPER_ARM] = (sway, 0.0, 0.0)
    pose[PART_RIGHT_UPPER_ARM] = (-sway, 0.0, 0.0)
    return pose


def walk_pose(phase: float) -> dict[str, tuple[float, float, float]]:
    """Opposite arm/leg swing -- amplitude is fixed (only phase ADVANCE
    speed is tied to horizontal speed, in CharacterVisualRig.update()), so
    the swing itself always reads as a normal walk, never a speed-scaled
    stretch."""
    pose = _zero_pose()
    swing = math.sin(phase)
    arm_swing = swing * WALK_ARM_SWING_DEGREES
    leg_swing = swing * WALK_LEG_SWING_DEGREES
    elbow = abs(swing) * WALK_ELBOW_BEND_DEGREES
    pose[PART_LEFT_UPPER_ARM] = (arm_swing, 0.0, 0.0)
    pose[PART_RIGHT_UPPER_ARM] = (-arm_swing, 0.0, 0.0)
    pose[PART_LEFT_LOWER_ARM] = (elbow, 0.0, 0.0)
    pose[PART_RIGHT_LOWER_ARM] = (elbow, 0.0, 0.0)
    pose[PART_LEFT_UPPER_LEG] = (-leg_swing, 0.0, 0.0)
    pose[PART_RIGHT_UPPER_LEG] = (leg_swing, 0.0, 0.0)
    return pose


def jump_pose() -> dict[str, tuple[float, float, float]]:
    """Fixed readable jump pose -- legs/arms tucked, distinct from
    fall_pose()."""
    pose = _zero_pose()
    pose[PART_LEFT_UPPER_ARM] = (JUMP_ARM_DEGREES, 0.0, 0.0)
    pose[PART_RIGHT_UPPER_ARM] = (JUMP_ARM_DEGREES, 0.0, 0.0)
    pose[PART_LEFT_UPPER_LEG] = (JUMP_LEG_TUCK_DEGREES, 0.0, 0.0)
    pose[PART_RIGHT_UPPER_LEG] = (JUMP_LEG_TUCK_DEGREES, 0.0, 0.0)
    pose[PART_LEFT_LOWER_LEG] = (JUMP_KNEE_BEND_DEGREES, 0.0, 0.0)
    pose[PART_RIGHT_LOWER_LEG] = (JUMP_KNEE_BEND_DEGREES, 0.0, 0.0)
    return pose


def fall_pose() -> dict[str, tuple[float, float, float]]:
    """Fixed pose, deliberately different from jump_pose() (Stage 3.4
    spec: "Fall: slightly different pose from Jump where practical")."""
    pose = _zero_pose()
    pose[PART_LEFT_UPPER_ARM] = (FALL_ARM_DEGREES, 0.0, 0.0)
    pose[PART_RIGHT_UPPER_ARM] = (FALL_ARM_DEGREES, 0.0, 0.0)
    pose[PART_LEFT_UPPER_LEG] = (FALL_LEG_DEGREES, 0.0, 0.0)
    pose[PART_RIGHT_UPPER_LEG] = (FALL_LEG_DEGREES, 0.0, 0.0)
    return pose


def pose_for_state(state: str, phase: float) -> dict[str, tuple[float, float, float]]:
    if state == ANIM_WALK:
        return walk_pose(phase)
    if state == ANIM_JUMP:
        return jump_pose()
    if state == ANIM_FALL:
        return fall_pose()
    return idle_pose(phase)


def ease_smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def blend_poses(
    pose_a: dict[str, tuple[float, float, float]],
    pose_b: dict[str, tuple[float, float, float]],
    t: float,
) -> dict[str, tuple[float, float, float]]:
    """Linear-interpolates two pose dicts part-by-part. Used for smoothing
    a transition between animation states (Stage 3.4 spec: "Animation
    transitions should be smoothed rather than snapping abruptly")."""
    t = max(0.0, min(1.0, t))
    result: dict[str, tuple[float, float, float]] = {}
    for name in POSABLE_PARTS:
        a = pose_a.get(name, (0.0, 0.0, 0.0))
        b = pose_b.get(name, (0.0, 0.0, 0.0))
        result[name] = (
            a[0] + (b[0] - a[0]) * t,
            a[1] + (b[1] - a[1]) * t,
            a[2] + (b[2] - a[2]) * t,
        )
    return result


# ============================================================
# APPEARANCE (plain data; Ursina only for the default color values)
# ============================================================

@dataclass
class CharacterAppearance:
    body_color: Any
    shirt_color: Any
    vest_color: Any
    trouser_color: Any
    shoe_color: Any
    screen_color: Any
    accent_color: Any
    head_accessory: Optional[str] = None
    neck_accessory: Optional[str] = None
    back_accessory: Optional[str] = None


# Fixed (non-customizable) tie color -- Stage 3.4 spec lists the tie as
# part of the fixed default visual design, not one of CharacterAppearance's
# configurable fields.
TIE_COLOR = color.rgb32(18, 18, 20)


def default_appearance() -> CharacterAppearance:
    """One default appearance preset (Stage 3.4 spec requirement): white
    shirt, dark vest, dark gray trousers, dark shoes, a neutral body/frame
    tone, a dark screen, and a cyan accent -- matches the spec's "DEFAULT
    VISUAL DESIGN" section."""
    return CharacterAppearance(
        body_color=color.rgb32(214, 190, 168),
        shirt_color=color.rgb32(245, 245, 245),
        vest_color=color.rgb32(32, 32, 38),
        trouser_color=color.rgb32(52, 52, 58),
        shoe_color=color.rgb32(18, 18, 18),
        screen_color=color.rgb32(14, 17, 22),
        accent_color=color.rgb32(0, 200, 255),
    )


# ============================================================
# RIG DIMENSIONS (world units, roughly matching
# character_controller.DEFAULT_HEIGHT=1.8 / DEFAULT_RADIUS=0.4)
# ============================================================

_LEG_UPPER_LEN = 0.40
_LEG_LOWER_LEN = 0.40
_LEG_THICK = 0.14
_PELVIS_Y = _LEG_UPPER_LEN + _LEG_LOWER_LEN
_HIP_OFFSET_X = 0.13

_TORSO_LEN = 0.55
_TORSO_WIDTH = 0.50
_TORSO_DEPTH = 0.26

_HEAD_SIZE = 0.30
_HEAD_DEPTH = 0.26

_ARM_UPPER_LEN = 0.32
_ARM_LOWER_LEN = 0.30
_ARM_THICK = 0.11
_SHOULDER_OFFSET_X = _TORSO_WIDTH / 2.0 + _ARM_THICK / 2.0 + 0.02
_SHOULDER_Y = _TORSO_LEN * 0.86


# ============================================================
# ACCESSORY BUILDERS (a few lightweight primitives)
# ============================================================

def _build_fedora(accent: Any) -> Entity:
    brim = Entity(model=Cylinder(radius=1.0, height=1.0, resolution=10), scale=(0.30, 0.035, 0.30), color=color.rgb32(40, 32, 28))
    Entity(parent=brim, model=Cylinder(radius=1.0, height=1.0, resolution=10), scale=(0.55, 3.6, 0.55), y=0.55, color=color.rgb32(40, 32, 28))
    Entity(parent=brim, model=Cylinder(radius=1.0, height=1.0, resolution=10), scale=(0.62, 0.25, 0.62), y=0.16, color=accent)
    return brim


def _build_cap(accent: Any) -> Entity:
    return Entity(model=Cylinder(radius=1.0, height=1.0, resolution=8), scale=(0.19, 0.11, 0.19), y=0.03, color=accent)


_ACCESSORY_BUILDERS: dict[str, Callable[[Any], Entity]] = {
    "fedora": _build_fedora,
    "cap": _build_cap,
}


# ============================================================
# THE RIG ITSELF (Ursina-bound)
# ============================================================

class CharacterVisualRig:
    """Original, primitive-built SStudio character visual (Stage 3.4) --
    humanoid but stylized, screen/display head, no player.glb, no shared
    code with the legacy PickADoor PlayerVisual/RemotePlayer classes. Built
    entirely from Ursina's own built-in primitives (cube meshes +
    procedural Cylinder), no external model file and no Blender dependency.

    Owns exactly one Entity tree, rooted at self.parts[PART_ROOT] (a plain
    Entity with no model of its own -- world position/facing yaw only).
    self.parts maps every REQUIRED_PART_NAMES entry to its Entity; nothing
    outside this class ever needs to know the tree's internal nesting.

    Rig root is intentionally NOT parented to client_studio.py's
    local_player Entity (which also carries the CAMERA) -- if it were, the
    rig would inherit local_player's rotation, which during Play IS the
    camera's own yaw, coupling body facing to camera look direction. This
    class computes its own facing yaw purely from horizontal movement
    direction (see yaw_from_horizontal_velocity()/step_facing_yaw()), so
    update() must be fed the capsule's FEET position each frame (caller's
    responsibility -- see character_controller.CharacterController.
    half_height) rather than being parented under any camera-bearing
    Entity.

    destroy() is idempotent and MUST be called on Stop so repeated Play/
    Stop cannot leak Ursina Entities (same requirement as
    CharacterController.destroy())."""

    def __init__(
        self,
        appearance: Optional[CharacterAppearance] = None,
        initial_yaw_degrees: float = 0.0,
        debug: bool = False,
    ) -> None:
        self.parts: dict[str, Entity] = {}
        self._accessories: dict[str, Optional[Entity]] = {name: None for name in ACCESSORY_SLOT_NAMES}
        self._decorative_entities: list[Entity] = []

        self._build_hierarchy()

        self.appearance = appearance or default_appearance()
        self._appearance_preset_name = "default"
        self._apply_appearance(self.appearance)
        if self.appearance.head_accessory:
            self.attach_accessory(PART_HEAD_ACCESSORY_SLOT, self.appearance.head_accessory)
        if self.appearance.neck_accessory:
            self.attach_accessory(PART_NECK_ACCESSORY_SLOT, self.appearance.neck_accessory)
        if self.appearance.back_accessory:
            self.attach_accessory(PART_BACK_ACCESSORY_SLOT, self.appearance.back_accessory)

        self._facing_yaw = float(initial_yaw_degrees)
        self._state = ANIM_IDLE
        self._phase = 0.0
        self._last_rendered_pose = idle_pose(0.0)
        self._transition_from_pose = self._last_rendered_pose
        self._transition_t = 1.0
        self._first_person = False

        self.debug_enabled = bool(debug)
        self._debug_frame_counter = 0

        self._destroyed = False

    # ------------------------------------------------------------
    # construction
    # ------------------------------------------------------------

    def _build_hierarchy(self) -> None:
        # Every entry in self.parts (a "joint") is an UNSCALED pivot Entity
        # -- position + rotation only, scale left at its Ursina default of
        # 1. Actual visible geometry is always a separate scaled LEAF
        # "mesh" child, offset from its pivot by a plain absolute-unit Y
        # (or Z) position (e.g. "hangs down from the pivot by half its own
        # length"). This sidesteps Panda3D/Ursina's parent-scale-multiplies
        # -child-position behavior entirely: since no pivot in this tree
        # ever carries a non-1 scale, every position offset below means
        # exactly what it says, at every nesting depth -- the same
        # unscaled-pivot + scaled-leaf pattern client_studio.py's
        # PlayerVisual already uses (aim_pivot/model_anchor, both scale=1,
        # with scaled placeholder meshes as their children).
        root = Entity(position=(0, 0, 0), rotation=(0, 0, 0))
        self.parts[PART_ROOT] = root

        pelvis = Entity(parent=root, position=(0, _PELVIS_Y, 0))
        self.parts[PART_PELVIS] = pelvis
        self._pelvis_mesh = Entity(parent=pelvis, model="cube", scale=(0.30, 0.16, 0.20))

        torso = Entity(parent=pelvis, position=(0, 0.06, 0))
        self.parts[PART_TORSO] = torso
        self._torso_mesh = Entity(parent=torso, model="cube", scale=(_TORSO_WIDTH, _TORSO_LEN, _TORSO_DEPTH), y=_TORSO_LEN / 2.0)

        self._shirt = Entity(parent=torso, model="cube", scale=(_TORSO_WIDTH * 0.55, _TORSO_LEN * 0.4, 0.02), position=(0, _TORSO_LEN * 0.72, _TORSO_DEPTH / 2.0 + 0.001))
        self._tie = Entity(parent=torso, model="cube", scale=(0.055, _TORSO_LEN * 0.4, 0.01), position=(0, _TORSO_LEN * 0.5, _TORSO_DEPTH / 2.0 + 0.002), color=TIE_COLOR)
        self._decorative_entities.extend((self._shirt, self._tie))

        head = Entity(parent=torso, position=(0, _TORSO_LEN + 0.045, 0))
        self.parts[PART_HEAD] = head
        self._head_mesh = Entity(parent=head, model="cube", scale=(_HEAD_SIZE, _HEAD_SIZE, _HEAD_DEPTH))

        screen = Entity(parent=head, position=(0, 0.02, _HEAD_DEPTH / 2.0 + 0.02))
        self.parts[PART_SCREEN] = screen
        self._screen_mesh = Entity(parent=screen, model="cube", scale=(_HEAD_SIZE * 0.72, _HEAD_SIZE * 0.55, 0.03))

        self._screen_accent = Entity(parent=screen, model="cube", scale=(0.05, 0.05, 0.02), position=(_HEAD_SIZE * 0.20, _HEAD_SIZE * 0.16, 0.03))
        self._decorative_entities.append(self._screen_accent)

        head_slot = Entity(parent=head, position=(0, _HEAD_SIZE / 2.0 + 0.03, 0))
        self.parts[PART_HEAD_ACCESSORY_SLOT] = head_slot

        neck_slot = Entity(parent=torso, position=(0, _TORSO_LEN * 0.97, _TORSO_DEPTH * 0.35))
        self.parts[PART_NECK_ACCESSORY_SLOT] = neck_slot

        back_slot = Entity(parent=torso, position=(0, _TORSO_LEN * 0.55, -_TORSO_DEPTH / 2.0 - 0.02))
        self.parts[PART_BACK_ACCESSORY_SLOT] = back_slot

        self._limb_meshes: dict[str, Entity] = {}
        for side, sign in (("left", -1.0), ("right", 1.0)):
            upper_arm = Entity(parent=torso, position=(sign * _SHOULDER_OFFSET_X, _SHOULDER_Y, 0))
            self._limb_meshes[f"{side}_upper_arm"] = Entity(parent=upper_arm, model="cube", scale=(_ARM_THICK, _ARM_UPPER_LEN, _ARM_THICK), y=-_ARM_UPPER_LEN / 2.0)

            lower_arm = Entity(parent=upper_arm, position=(0, -_ARM_UPPER_LEN, 0))
            self._limb_meshes[f"{side}_lower_arm"] = Entity(parent=lower_arm, model="cube", scale=(_ARM_THICK * 0.9, _ARM_LOWER_LEN, _ARM_THICK * 0.9), y=-_ARM_LOWER_LEN / 2.0)

            self.parts[f"{side}_upper_arm"] = upper_arm
            self.parts[f"{side}_lower_arm"] = lower_arm

            upper_leg = Entity(parent=pelvis, position=(sign * _HIP_OFFSET_X, 0, 0))
            self._limb_meshes[f"{side}_upper_leg"] = Entity(parent=upper_leg, model="cube", scale=(_LEG_THICK, _LEG_UPPER_LEN, _LEG_THICK), y=-_LEG_UPPER_LEN / 2.0)

            lower_leg = Entity(parent=upper_leg, position=(0, -_LEG_UPPER_LEN, 0))
            self._limb_meshes[f"{side}_lower_leg"] = Entity(parent=lower_leg, model="cube", scale=(_LEG_THICK * 0.9, _LEG_LOWER_LEN, _LEG_THICK * 0.9), y=-_LEG_LOWER_LEN / 2.0)

            shoe = Entity(parent=lower_leg, model="cube", scale=(_LEG_THICK * 1.15, 0.10, _LEG_THICK * 1.7), position=(0, -_LEG_LOWER_LEN, 0.02))
            self._decorative_entities.append(shoe)
            setattr(self, f"_{side}_shoe", shoe)

            self.parts[f"{side}_upper_leg"] = upper_leg
            self.parts[f"{side}_lower_leg"] = lower_leg

        missing = [name for name in REQUIRED_PART_NAMES if name not in self.parts]
        assert not missing, f"CharacterVisualRig built without required part(s): {missing}"

    # ------------------------------------------------------------
    # appearance
    # ------------------------------------------------------------

    def _apply_appearance(self, appearance: CharacterAppearance) -> None:
        self._torso_mesh.color = appearance.vest_color
        self._pelvis_mesh.color = appearance.trouser_color
        self._head_mesh.color = appearance.body_color
        self._screen_mesh.color = appearance.screen_color
        self._shirt.color = appearance.shirt_color
        self._screen_accent.color = appearance.accent_color
        for side in ("left", "right"):
            self._limb_meshes[f"{side}_upper_arm"].color = appearance.body_color
            self._limb_meshes[f"{side}_lower_arm"].color = appearance.body_color
            self._limb_meshes[f"{side}_upper_leg"].color = appearance.trouser_color
            self._limb_meshes[f"{side}_lower_leg"].color = appearance.trouser_color
            getattr(self, f"_{side}_shoe").color = appearance.shoe_color

    def set_appearance(self, appearance: CharacterAppearance, preset_name: str = "custom") -> None:
        """Applies a new appearance to every relevant Entity. Purely a
        local-runtime visual change (Stage 3.4 spec's network boundary --
        never sent over the wire, never written to a Place)."""
        self.appearance = appearance
        self._appearance_preset_name = preset_name
        self._apply_appearance(appearance)

    # ------------------------------------------------------------
    # accessories
    # ------------------------------------------------------------

    def attach_accessory(self, slot_name: str, kind: str) -> Entity:
        if slot_name not in ACCESSORY_SLOT_NAMES:
            raise ValueError(f"'{slot_name}' is not an accessory slot (expected one of {ACCESSORY_SLOT_NAMES})")
        builder = _ACCESSORY_BUILDERS.get(kind)
        if builder is None:
            raise ValueError(f"unknown accessory kind: '{kind}' (expected one of {sorted(_ACCESSORY_BUILDERS)})")
        self.remove_accessory(slot_name)
        entity = builder(self.appearance.accent_color)
        entity.parent = self.parts[slot_name]
        entity.position = Vec3(0, 0, 0)
        self._accessories[slot_name] = entity
        return entity

    def remove_accessory(self, slot_name: str) -> None:
        if slot_name not in ACCESSORY_SLOT_NAMES:
            raise ValueError(f"'{slot_name}' is not an accessory slot (expected one of {ACCESSORY_SLOT_NAMES})")
        existing = self._accessories.get(slot_name)
        if existing is not None:
            destroy(existing)
            self._accessories[slot_name] = None

    def accessory_at(self, slot_name: str) -> Optional[Entity]:
        return self._accessories.get(slot_name)

    # ------------------------------------------------------------
    # camera-mode visibility
    # ------------------------------------------------------------

    def set_first_person(self, enabled: bool) -> None:
        """Hides everything that would clip the camera in first-person --
        torso.enabled=False cascades to head/screen/arms/accessory slots
        (all descendants of torso), which is exactly the "hide the head,
        hide anything that clips the camera" requirement in one step.
        Pelvis/legs stay visible in both modes (a look-down view of your
        own legs is harmless and common in first-person games)."""
        self._first_person = bool(enabled)
        self.parts[PART_TORSO].enabled = not self._first_person

    # ------------------------------------------------------------
    # per-frame update
    # ------------------------------------------------------------

    def update(
        self,
        dt: float,
        feet_position: tuple[float, float, float],
        horizontal_velocity: tuple[float, float],
        grounded: bool,
        vertical_velocity: float,
    ) -> None:
        """Call AFTER the capsule's own physics step this frame (see
        character_controller.CharacterController.apply_movement's
        docstring for the equivalent ordering requirement) -- feet_position
        must already be the FEET position (capsule center minus
        half_height), not the capsule's own center, since this rig's own
        Y=0 is its feet, not its pelvis. horizontal_velocity is (vx, vz),
        e.g. from CharacterController.horizontal_velocity()."""
        root = self.parts[PART_ROOT]
        root.position = Vec3(feet_position[0], feet_position[1], feet_position[2])

        horizontal_speed = math.hypot(horizontal_velocity[0], horizontal_velocity[1])
        target_yaw = yaw_from_horizontal_velocity(horizontal_velocity[0], horizontal_velocity[1])
        if target_yaw is not None:
            self._facing_yaw = step_facing_yaw(self._facing_yaw, target_yaw, dt)
        root.rotation = Vec3(0, self._facing_yaw, 0)

        new_state = select_animation_state(horizontal_speed, grounded, vertical_velocity)
        if new_state != self._state:
            self._transition_from_pose = self._last_rendered_pose
            self._transition_t = 0.0
            self._state = new_state
            if new_state == ANIM_WALK:
                self._phase = 0.0

        if self._state == ANIM_WALK:
            self._phase += dt * horizontal_speed * WALK_PHASE_SPEED
        elif self._state == ANIM_IDLE:
            self._phase += dt * (2.0 * math.pi / IDLE_BREATH_PERIOD)
        # ANIM_JUMP/ANIM_FALL: fixed poses, no phase advance.

        target_pose = pose_for_state(self._state, self._phase)
        if self._transition_t < 1.0:
            self._transition_t = min(1.0, self._transition_t + dt / TRANSITION_DURATION)
            rendered_pose = blend_poses(self._transition_from_pose, target_pose, ease_smoothstep(self._transition_t))
        else:
            rendered_pose = target_pose
        self._last_rendered_pose = rendered_pose

        for name, angles in rendered_pose.items():
            self.parts[name].rotation = Vec3(angles[0], angles[1], angles[2])

        if self.debug_enabled:
            self._debug_frame_counter += 1
            if self._debug_frame_counter % 60 == 1:
                info = self.debug_snapshot()
                print(f"[RIG DEBUG] state={info['animation_state']} entities={info['entity_count']} appearance={info['appearance_preset']} first_person={info['first_person']}")

    # ------------------------------------------------------------
    # diagnostics
    # ------------------------------------------------------------

    def entity_count(self) -> int:
        accessory_count = sum(1 for entity in self._accessories.values() if entity is not None)
        return len(self.parts) + len(self._decorative_entities) + accessory_count

    def debug_snapshot(self) -> dict[str, Any]:
        return {
            "animation_state": self._state,
            "entity_count": self.entity_count(),
            "appearance_preset": self._appearance_preset_name,
            "first_person": self._first_person,
        }

    # ------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------

    def destroy(self) -> None:
        if self._destroyed:
            return
        destroy(self.parts[PART_ROOT])
        self._destroyed = True
