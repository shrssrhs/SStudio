"""
Stage 3.3: Play Mode character controller foundation.

Scope (see Stage 3.3 spec): this module implements only the PHYSICAL/INPUT
controller layer -- movement, capsule collider, gravity, grounding, jumping,
spawn placement. It deliberately does NOT implement a visible character
model, animations, cosmetics, or third-person camera; those are future work
(Stage 3.4+) that will attach to this same controller without needing to
touch it (see CharacterRuntime's optional debug-visual hook).

Architecture:

    CharacterRuntime            <- one per Play session, owns the below
    +-- CharacterController     <- physical capsule (Bullet), no camera/Qt
    +-- CharacterInputState     <- pure held-key/captured data, no engine dep
    +-- CharacterCamera         <- pure yaw/pitch math, no engine dep

CharacterController/CharacterInputState/CharacterCamera have NO dependency
on each other's engine bindings -- CharacterInputState and CharacterCamera
are plain Python (no Panda3D/Ursina/Qt imports at all) and are fully
testable in isolation. CharacterController needs a live Bullet world (the
SAME one physics.PhysicsWorld already owns for this Play session -- this
module never creates its own BulletWorld, per the Stage 3.3 spec's "Do not
introduce a second Bullet world").

Nothing here owns Qt widgets, PlaceManager, editor history/Undo state, Lua
VM state, or serialized Instance records. client_studio.py's MultiplayerGame
is the only thing that decides IF/WHEN a CharacterRuntime exists for the
current Play session, and is responsible for copying the controller's
position onto the existing local_player/camera_pitch_pivot Entities each
frame (camera-follow) -- this module never touches Ursina Entities itself.

NETWORK BOUNDARY (local-only limitation, see Stage 3.3 spec section
"NETWORK BOUNDARY"): everything in this module is local-player runtime
state only. The capsule's transform is never sent through UPDATE_PROPERTY
or any other scene-edit message, never becomes a persistent Instance in the
authoritative Place world, and is never broadcast to other clients. This
stage does not implement server-authoritative movement, remote character
snapshots, respawn, or visual-rig replication -- CharacterRuntime is built
so those can be layered on top later (a future networked-movement system
would read CharacterController.get_position()/is_grounded() the same way
the local camera-follow code does, without needing to change this module).
"""

from __future__ import annotations

import math
from typing import Any, Optional

from panda3d.bullet import BulletCapsuleShape, BulletCharacterControllerNode, BulletWorld
from panda3d.core import NodePath
from panda3d.core import Vec3 as PVec3

# ============================================================
# TUNABLES (all overridable via CharacterController's constructor kwargs)
# ============================================================

DEFAULT_RADIUS = 0.4
DEFAULT_HEIGHT = 1.8  # standing height, feet to head
DEFAULT_STEP_HEIGHT = 0.4
DEFAULT_WALK_SPEED = 6.0
DEFAULT_GROUND_ACCEL = 40.0  # units/s^2 toward the input-driven target velocity while grounded
DEFAULT_AIR_ACCEL = 8.0  # limited air control -- much slower to redirect mid-air than on the ground
DEFAULT_JUMP_SPEED = 8.0
DEFAULT_MAX_SLOPE_DEGREES = 45.0
# Magnitude only, matching physics.GRAVITY_Y's magnitude (-24.0) -- kept as
# a local constant rather than importing physics.py, so this module has no
# dependency on it (see module docstring: CharacterController only needs
# an existing BulletWorld handed to it, not the PhysicsWorld wrapper).
DEFAULT_GRAVITY = 24.0

# Documented safe fallback spawn position when no SpawnPoint Instance
# exists in the current Place -- matches MultiplayerGame's pre-existing
# default local_player start position (see create_first_person_player()),
# well above the default baseplate/ground height so a Blank Place (which
# has no ground at all) still gives the capsule room to fall onto whatever
# collidable geometry (if any) exists below.
FALLBACK_SPAWN_POSITION = (0.0, 3.0, 0.0)

# Extra clearance added above a SpawnPoint pad's top surface (or the
# fallback position) so the capsule never starts embedded in a collider.
SPAWN_CLEARANCE = 0.05


# ============================================================
# PURE MATH (no Panda3D/Ursina/Qt import -- testable in isolation)
# ============================================================

def yaw_only_forward(yaw_degrees: float) -> tuple[float, float, float]:
    """Ground-plane forward direction from yaw alone. Pitch is deliberately
    ignored here -- looking up/down must not make a walking character move
    up/down, unlike the editor's free-fly camera (client_studio.py's
    forward_from_angles(), which intentionally DOES include pitch)."""
    yaw = math.radians(yaw_degrees)
    return (math.sin(yaw), 0.0, math.cos(yaw))


def yaw_only_right(yaw_degrees: float) -> tuple[float, float, float]:
    """Same sign convention as client_studio.py's right_from_yaw(), so a
    yaw value shared between the editor camera and this controller always
    means the same facing direction."""
    yaw = math.radians(yaw_degrees)
    return (math.cos(yaw), 0.0, -math.sin(yaw))


def _add3(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _scale3(a: tuple[float, float, float], s: float) -> tuple[float, float, float]:
    return (a[0] * s, a[1] * s, a[2] * s)


def _length3(a: tuple[float, float, float]) -> float:
    return math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])


def _normalized3(a: tuple[float, float, float]) -> tuple[float, float, float]:
    length = _length3(a)
    if length <= 1e-9:
        return (0.0, 0.0, 0.0)
    return (a[0] / length, a[1] / length, a[2] / length)


class CharacterInputState:
    """Pure data: which movement keys are currently held, an edge-triggered
    jump flag, and whether Play input is currently captured. No Qt, no
    Panda3D, no Ursina -- fully unit-testable in isolation. client_studio.py
    owns setting these flags from actual key events; this class never reads
    input itself."""

    __slots__ = ("forward", "backward", "left", "right", "jump_held", "captured")

    def __init__(self) -> None:
        self.forward = False
        self.backward = False
        self.left = False
        self.right = False
        self.jump_held = False
        self.captured = False

    def movement_axes(self) -> tuple[float, float]:
        """Returns (strafe, forward), each independently in {-1, 0, 1}.
        Deliberately NOT normalized here -- normalization happens once in
        compute_target_velocity(), after combining with world-space
        direction vectors, so W+D (diagonal) is never faster than W alone
        (Stage 3.3 spec: "diagonal movement is not faster than straight
        movement")."""
        forward = (1.0 if self.forward else 0.0) - (1.0 if self.backward else 0.0)
        strafe = (1.0 if self.right else 0.0) - (1.0 if self.left else 0.0)
        return strafe, forward


def compute_target_velocity(
    input_state: CharacterInputState,
    yaw_degrees: float,
    walk_speed: float,
) -> tuple[float, float, float]:
    """World-space XZ target velocity for the current input + facing."""
    strafe, forward = input_state.movement_axes()
    if strafe == 0.0 and forward == 0.0:
        return (0.0, 0.0, 0.0)
    forward_dir = yaw_only_forward(yaw_degrees)
    right_dir = yaw_only_right(yaw_degrees)
    combined = _add3(_scale3(forward_dir, forward), _scale3(right_dir, strafe))
    direction = _normalized3(combined)
    return _scale3(direction, walk_speed)


def accelerate_toward(
    current: tuple[float, float, float],
    target: tuple[float, float, float],
    accel: float,
    dt: float,
) -> tuple[float, float, float]:
    """Blends `current` toward `target` at rate `accel` (units/s^2),
    clamped so a single step never overshoots `target`. Used for both
    ground and air movement, just with different accel rates (see
    CharacterController.apply_movement)."""
    if dt <= 0.0:
        return current
    delta = (target[0] - current[0], target[1] - current[1], target[2] - current[2])
    delta_length = _length3(delta)
    max_step = accel * dt
    if delta_length <= max_step or delta_length <= 1e-9:
        return target
    scale = max_step / delta_length
    return (
        current[0] + delta[0] * scale,
        current[1] + delta[1] * scale,
        current[2] + delta[2] * scale,
    )


def select_spawn_point(spawn_candidates: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Deterministic selection when multiple SpawnPoint Instances exist:
    lowest instance id (plain string sort -- stable across calls, doesn't
    depend on dict/network ordering). Each candidate needs at least
    {"id": str, "position": (x, y, z)}. Returns None if the list is empty
    -- caller must fall back to FALLBACK_SPAWN_POSITION."""
    if not spawn_candidates:
        return None
    return min(spawn_candidates, key=lambda candidate: str(candidate["id"]))


def spawn_position_from_point(
    spawn_point: dict[str, Any], capsule_half_height: float,
) -> tuple[float, float, float]:
    """Places the capsule's ORIGIN (its center, matching
    BulletCharacterControllerNode's own convention -- NodePath.setPos() on
    a character node positions its center, not its feet) safely above the
    SpawnPoint pad's top surface: pad center Y + half the pad's own height
    + capsule half-height + SPAWN_CLEARANCE, so the capsule never starts
    embedded in the pad (which would immediately push it out unpredictably,
    or in the worst case get stuck)."""
    position = spawn_point["position"]
    size = spawn_point.get("size", (1.0, 1.0, 1.0))
    pad_half_height = float(size[1]) / 2.0
    spawn_y = float(position[1]) + pad_half_height + capsule_half_height + SPAWN_CLEARANCE
    return (float(position[0]), spawn_y, float(position[2]))


def fallback_spawn_position(capsule_half_height: float) -> tuple[float, float, float]:
    """Documented safe fallback used when no SpawnPoint Instance exists in
    the current Place (Stage 3.3 spec item 4: "if no SpawnPoint exists, use
    a documented safe fallback position")."""
    base = FALLBACK_SPAWN_POSITION
    return (base[0], base[1] + capsule_half_height, base[2])


class CharacterCamera:
    """Pure yaw/pitch state + math for the Stage 3.3 minimal first-person
    Play camera. No Panda3D/Ursina/Qt dependency -- client_studio.py reads
    .yaw_degrees/.pitch_degrees each frame and applies them to the existing
    local_player/camera_pitch_pivot Entities (the SAME rig the editor's
    free-look camera uses -- Stage 3.3 does not create a second camera rig
    or repeat native viewport embedding). Kept as its OWN yaw/pitch state,
    separate from MultiplayerGame.player_yaw/player_pitch (which the editor
    free-look path keeps using unchanged), so a Play session's camera moves
    independently of whatever the editor camera was doing before Play
    started; the existing saved_play_position/yaw/pitch snapshot mechanism
    in set_studio_playing() is what restores the editor's own camera state
    after Stop."""

    __slots__ = ("yaw_degrees", "pitch_degrees", "min_pitch", "max_pitch", "sensitivity_x", "sensitivity_y")

    def __init__(
        self,
        yaw_degrees: float = 0.0,
        pitch_degrees: float = 0.0,
        min_pitch: float = -80.0,
        max_pitch: float = 80.0,
        sensitivity_x: float = 0.08,
        sensitivity_y: float = 0.08,
    ) -> None:
        self.yaw_degrees = float(yaw_degrees)
        self.pitch_degrees = float(pitch_degrees)
        self.min_pitch = float(min_pitch)
        self.max_pitch = float(max_pitch)
        self.sensitivity_x = float(sensitivity_x)
        self.sensitivity_y = float(sensitivity_y)

    def apply_delta(self, delta_x: float, delta_y: float) -> None:
        self.yaw_degrees = (self.yaw_degrees + delta_x * self.sensitivity_x + 180.0) % 360.0 - 180.0
        pitch = self.pitch_degrees + (-delta_y) * self.sensitivity_y
        self.pitch_degrees = max(self.min_pitch, min(self.max_pitch, pitch))


# ============================================================
# PHYSICAL CONTROLLER (needs a real Bullet world)
# ============================================================

class CharacterController:
    """Owns exactly one BulletCharacterControllerNode + BulletCapsuleShape,
    attached to a caller-supplied EXISTING BulletWorld -- never creates its
    own (Stage 3.3 spec: "Do not introduce a second Bullet world"; the
    caller is expected to pass physics.PhysicsWorld's own bullet_world, the
    same one static/dynamic/ghost Part bodies for this Play session already
    live in, so the capsule collides with them for free). Purely physical:
    no camera, no Qt, no PlaceManager, no Instance records, no network.

    Uses Panda3D's own kinematic ghost-sweep character controller for
    MOVEMENT/COLLISION (NOT a free dynamic rigid body, which would tip over
    / not stay upright) -- but NOT for gravity, jumping, or grounded
    detection. Verified empirically (see Stage 3.3 investigation):
    BulletCharacterControllerNode.setGravity()/isOnGround()/doJump() are
    hardcoded to Panda3D's native Z-up convention regardless of the
    capsule's `up_axis` argument, and are simply wrong in this project's
    Y-up world (Ursina/physics.py, see physics.py's gravity=(0,-G,0)) --
    a controller with setGravity(24) drifted along Z while its Y coordinate
    never moved, and isOnGround() reported True while floating in mid-air
    nowhere near any surface. So this class disables the node's own gravity
    (setGravity(0.0)) and instead:
      - integrates its own vertical velocity each frame (same gravity
        constant/approach as physics.py's "ghost" bodies);
      - detects grounded state with its own short downward ray test via
        bullet_world.rayTestClosest() (confirmed reliable against static
        floor bodies in the same world);
      - jumps by setting vertical velocity directly, gated on that ray-test
        grounded check (this alone is what makes air-jumping impossible).
    The combined horizontal+vertical velocity is still handed to Bullet's
    own setLinearMovement() every frame, so wall/step/slope collision
    resolution (confirmed working correctly for horizontal movement) stays
    entirely Bullet's job -- only the vertical axis needed a manual
    workaround.

    destroy() is idempotent and MUST be called on Stop so repeated Play/
    Stop cannot leak Bullet nodes (Stage 3.3 spec's explicit leak-check
    requirement)."""

    # Downward ray-test window below the capsule's feet used for grounded
    # detection -- starts slightly ABOVE the theoretical foot line (so a
    # capsule resting with a hair of overlap still hits) and extends a
    # short distance below it (so a small step-down gap still counts as
    # grounded instead of flickering to airborne every frame).
    _GROUND_RAY_ABOVE_FEET = 0.05
    _GROUND_RAY_BELOW_FEET = 0.15
    # Small resting downward speed re-applied every grounded frame instead
    # of letting velocity fall all the way to 0 -- keeps the ray test (and
    # Bullet's own sweep) consistently detecting continued ground contact
    # instead of the capsule "letting go" of the surface for a frame.
    _GROUND_STICK_SPEED = -1.0

    def __init__(
        self,
        bullet_world: BulletWorld,
        radius: float = DEFAULT_RADIUS,
        height: float = DEFAULT_HEIGHT,
        step_height: float = DEFAULT_STEP_HEIGHT,
        walk_speed: float = DEFAULT_WALK_SPEED,
        ground_accel: float = DEFAULT_GROUND_ACCEL,
        air_accel: float = DEFAULT_AIR_ACCEL,
        jump_speed: float = DEFAULT_JUMP_SPEED,
        gravity: float = DEFAULT_GRAVITY,
        max_slope_degrees: float = DEFAULT_MAX_SLOPE_DEGREES,
        name: str = "PlayCharacter",
    ) -> None:
        self.bullet_world = bullet_world
        self.radius = float(radius)
        self.height = float(height)
        self.step_height = float(step_height)
        self.walk_speed = float(walk_speed)
        self.ground_accel = float(ground_accel)
        self.air_accel = float(air_accel)
        self.jump_speed = float(jump_speed)
        self.gravity = float(gravity)
        self.max_slope_degrees = float(max_slope_degrees)

        # BulletCapsuleShape(radius, height, up_axis)'s `height` argument is
        # the CYLINDER length between the two hemisphere caps, not the total
        # standing height -- total standing height = cylinder_height +
        # 2*radius, so the caps are subtracted back out here. up_axis=1
        # only affects the SHAPE's own geometric orientation (confirmed
        # correct for collision purposes); it does NOT affect the
        # controller's internal gravity axis (see class docstring).
        cylinder_height = max(self.height - 2.0 * self.radius, 0.05)
        shape = BulletCapsuleShape(self.radius, cylinder_height, 1)
        self.node = BulletCharacterControllerNode(shape, self.step_height, name)
        self.node.setGravity(0.0)  # disabled -- see class docstring
        self.node.setMaxSlope(math.radians(self.max_slope_degrees))
        self.np = NodePath(self.node)
        bullet_world.attachCharacter(self.node)
        self._current_velocity_xz = (0.0, 0.0)
        self._velocity_y = 0.0
        self._grounded = False
        self._destroyed = False

    @property
    def half_height(self) -> float:
        return self.height / 2.0

    def set_position(self, position: tuple[float, float, float]) -> None:
        self.np.setPos(PVec3(position[0], position[1], position[2]))

    def get_position(self) -> tuple[float, float, float]:
        pos = self.np.getPos()
        return (pos.x, pos.y, pos.z)

    def is_grounded(self) -> bool:
        """Cached result of the last apply_movement()'s ray test -- see
        class docstring for why this doesn't use the node's own (broken)
        isOnGround()."""
        return self._grounded

    def horizontal_velocity(self) -> tuple[float, float]:
        """Current (vx, vz) horizontal velocity -- public read accessor
        (Stage 3.4) for a visual rig's animation-state/facing-direction
        selection. Read-only view of the same state apply_movement()
        already maintains; does not affect movement integration."""
        return self._current_velocity_xz

    def horizontal_speed(self) -> float:
        """Magnitude of horizontal_velocity() -- public read accessor
        (Stage 3.4) for animation-state selection (Idle vs Walk)."""
        vx, vz = self._current_velocity_xz
        return _length3((vx, 0.0, vz))

    def vertical_velocity(self) -> float:
        """Current vertical (Y) velocity -- public read accessor (Stage
        3.4) for animation-state selection (Jump vs Fall)."""
        return self._velocity_y

    def _ray_test_grounded(self) -> bool:
        pos = self.np.getPos()
        foot_y = pos.y - self.half_height
        from_point = PVec3(pos.x, foot_y + self._GROUND_RAY_ABOVE_FEET, pos.z)
        to_point = PVec3(pos.x, foot_y - self._GROUND_RAY_BELOW_FEET, pos.z)
        result = self.bullet_world.rayTestClosest(from_point, to_point)
        return bool(result.hasHit())

    def try_jump(self) -> bool:
        """Only succeeds while grounded -- this IS the "no unlimited air
        jumping" guarantee (Stage 3.3 spec). Safe to call every frame Space
        is held: once airborne, is_grounded() is False and this is a no-op
        until landing again (holding Space through a landing correctly
        re-triggers a jump, same as most games -- there is no separate
        "already jumped" latch needed because the grounded gate alone
        prevents mid-air jumps)."""
        if not self.is_grounded():
            return False
        self._velocity_y = self.jump_speed
        return True

    def apply_movement(self, input_state: CharacterInputState, yaw_degrees: float, dt: float) -> None:
        """Refreshes grounded state, integrates vertical velocity (gravity
        + optional jump), blends horizontal velocity toward the input-
        driven target at ground_accel while grounded / air_accel while
        airborne, then hands the COMBINED velocity to Bullet's own
        kinematic sweep step via setLinearMovement(). The actual position
        integration/collision resolution happens inside
        bullet_world.doPhysics(), called once per frame by
        physics.PhysicsWorld.step() -- the SAME Bullet world/step this
        controller is attached to (see class docstring); this method must
        be called BEFORE that step runs each frame, and get_position()
        read AFTER it."""
        self._grounded = self._ray_test_grounded()

        if self._grounded and self._velocity_y <= 0.0:
            self._velocity_y = self._GROUND_STICK_SPEED
        if input_state.jump_held:
            self.try_jump()
        self._velocity_y += -self.gravity * dt

        target_xz = compute_target_velocity(input_state, yaw_degrees, self.walk_speed)
        accel = self.ground_accel if self._grounded else self.air_accel
        current_3d = (self._current_velocity_xz[0], 0.0, self._current_velocity_xz[1])
        target_3d = (target_xz[0], 0.0, target_xz[2])
        blended = accelerate_toward(current_3d, target_3d, accel, dt)
        self._current_velocity_xz = (blended[0], blended[2])

        self.node.setLinearMovement(PVec3(blended[0], self._velocity_y, blended[2]), False)

    def destroy(self) -> None:
        if self._destroyed:
            return
        self.bullet_world.removeCharacter(self.node)
        self._destroyed = True


# ============================================================
# PER-PLAY-SESSION COMPOSITION ROOT
# ============================================================

class CharacterRuntime:
    """Composes CharacterController + CharacterInputState + CharacterCamera
    for exactly one Play session. Created fresh after Play validates and
    the runtime Bullet scene is ready, destroyed on Stop -- see module
    docstring for the local-only networking boundary this stays behind.
    Does not own Qt widgets, PlaceManager, editor history, Lua VM state, or
    serialized Instance records (Stage 3.3 spec)."""

    def __init__(
        self,
        bullet_world: BulletWorld,
        spawn_position: tuple[float, float, float],
        initial_yaw_degrees: float = 0.0,
        **controller_kwargs: Any,
    ) -> None:
        self.controller = CharacterController(bullet_world, **controller_kwargs)
        self.controller.set_position(spawn_position)
        self.input = CharacterInputState()
        self.camera = CharacterCamera(yaw_degrees=initial_yaw_degrees)
        # Optional developer debug visual (Stage 3.3 spec "PLACEHOLDER
        # VISUAL"): not created by default, never serialized, no
        # player.glb/legacy PlayerVisual involved. client_studio.py may set
        # this to an Ursina Entity for an explicit toggle; this module
        # never creates or renders one itself.
        self.debug_visual: Optional[Any] = None
        self._destroyed = False

    def step(self, dt: float) -> None:
        """Call BEFORE physics.PhysicsWorld.step() runs this frame."""
        self.controller.apply_movement(self.input, self.camera.yaw_degrees, dt)

    def synced_position(self) -> tuple[float, float, float]:
        """Call AFTER physics.PhysicsWorld.step() has run this frame --
        returns the controller's post-step position, for the caller to
        copy onto local_player.position (camera-follow)."""
        return self.controller.get_position()

    def destroy(self) -> None:
        if self._destroyed:
            return
        self.controller.destroy()
        self._destroyed = True
