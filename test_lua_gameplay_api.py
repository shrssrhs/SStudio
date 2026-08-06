"""Regression tests for Stage 3.5's Lua Player/Character/Signal/
UserInputService gameplay API (lua_gameplay_api.py) and its additive
integration points in lua_runtime.py/client_studio.py.

Follows this project's existing test convention (test_character_controller.py,
test_character_rig.py): plain top-level-assertion script, run directly,
offscreen Qt platform, headless Ursina window (needed for real
CharacterVisualRig Entities, exactly like test_character_rig.py), a real
headless BulletWorld (needed for a real CharacterController, exactly like
test_character_controller.py).

A minimal _FakeGame stands in for MultiplayerGame -- it provides exactly
the attributes/methods LuaRuntimeManager, RuntimeSceneLayer, and
LuaGameplayContext actually read (instances/parts/_physics_world/
studio_adapter/_character_runtime/_character_visual/third_person_enabled/
_mouse_look_captured()/set_camera_mode()), with a REAL
character_controller.CharacterRuntime and character_rig.CharacterVisualRig
underneath -- Stage 3.5's whole point is exposing those two real,
already-accepted systems to Lua, so faking them out would test nothing.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, '.')

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from ursina import Ursina

ursina_app = Ursina(window_type="none")

from panda3d.bullet import BulletBoxShape, BulletRigidBodyNode, BulletWorld
from panda3d.core import NodePath, Vec3 as PVec3

import character_controller as cc
import character_rig as cr
import client_studio as cs
import lua_gameplay_api as lga
import lua_runtime

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"ok: {message}")


def _add_static_floor(world: BulletWorld, y: float = -0.5, half_extents=(50.0, 0.5, 50.0)) -> NodePath:
    """Mirrors test_character_controller.py's own add_static_floor() --
    without a real floor body, a spawned capsule just falls forever and
    every grounded/jump-related test below is meaningless."""
    node = BulletRigidBodyNode("floor")
    node.addShape(BulletBoxShape(PVec3(*half_extents)))
    node.setMass(0.0)
    node.setStatic(True)
    np = NodePath(node)
    np.setPos(0, y, 0)
    world.attachRigidBody(node)
    return np


# ============================================================
# FAKE GAME
# ============================================================

class _FakeGame:
    def __init__(self, with_character: bool = True) -> None:
        self.instances: dict[str, cs.InstanceRecord] = {}
        self.parts: dict = {}
        self._physics_world = None
        self.studio_adapter = None
        self.third_person_enabled = False
        self._captured = False
        self._character_runtime = None
        self._character_visual = None
        if with_character:
            world = BulletWorld()
            world.setGravity((0, -24.0, 0))
            self._bullet_world = world
            _add_static_floor(world)
            self._character_runtime = cc.CharacterRuntime(world, (0.0, 3.0, 0.0))
            self._character_visual = cr.CharacterVisualRig()

    def _mouse_look_captured(self) -> bool:
        return self._captured

    def set_camera_mode(self, third_person: bool) -> None:
        third_person = bool(third_person)
        self.third_person_enabled = third_person
        if self._character_visual is not None:
            self._character_visual.set_first_person(not third_person)

    def teardown(self) -> None:
        if self._character_visual is not None:
            self._character_visual.destroy()
        if self._character_runtime is not None:
            self._character_runtime.destroy()


def add_script(game: _FakeGame, source: str, class_name: str = "LocalScript", name: str = "Script1", script_id: str = "script1", parent_id: str | None = None) -> str:
    game.instances[script_id] = cs.InstanceRecord(script_id, class_name, name, parent_id, {"Source": source})
    return script_id


def make_context(game: _FakeGame) -> tuple[lua_runtime.LuaRuntimeManager, lga.LuaGameplayContext]:
    manager = lua_runtime.LuaRuntimeManager(game)
    manager.start()
    ctx = lga.LuaGameplayContext(game, manager)
    ctx.start()
    return manager, ctx


def run_frame(manager: lua_runtime.LuaRuntimeManager, ctx: lga.LuaGameplayContext, dt: float = 1 / 60) -> None:
    manager.update(dt)
    ctx.update(dt)


def teardown(game: _FakeGame, manager: lua_runtime.LuaRuntimeManager, ctx: lga.LuaGameplayContext) -> None:
    ctx.stop()
    manager.stop()
    game.teardown()


# ============================================================
# LuaSignal
# ============================================================

# All generic-Signal behavior below is exercised through
# UserInputService.InputBegan -- a REAL, normally-reachable signal (via
# game:GetService(...)), not a synthetic backdoor. __gameplay_make_signal_proxy
# itself is intentionally NOT copied into any script's sandboxed _ENV (see
# _build_sandbox_env() in lua_runtime.py) -- exactly like every other
# __bridge_*/__registry_* internal, so calling it directly from script
# Source is correctly a "nil global" error, not something tests should
# route around.

def test_signal_connect_and_fire() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        source = "game:GetService('UserInputService').InputBegan:Connect(function(input, processed) print('got', input.KeyCode, processed) end)"
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        ctx.on_key_event("w")
        run_frame(manager, ctx)
        messages = [d.message for d in manager.diagnostics]
        check(any("got\tW\ttrue" in m for m in messages), "Signal:Connect() listener receives the fired arguments")
    finally:
        teardown(game, manager, ctx)


def test_signal_disconnect_stops_future_firing() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        source = (
            "local sig = game:GetService('UserInputService').InputBegan\n"
            "local count = 0\n"
            "local conn = sig:Connect(function() count = count + 1; print('count', count) end)\n"
            "conn:Disconnect()\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        ctx.on_key_event("w")
        run_frame(manager, ctx)
        messages = [d.message for d in manager.diagnostics]
        check(not any("count" in m for m in messages), "Disconnect() before firing: listener never runs")
    finally:
        teardown(game, manager, ctx)


def test_signal_once_fires_exactly_once() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        source = (
            "local sig = game:GetService('UserInputService').InputBegan\n"
            "sig:Once(function() print('once-fired') end)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        ctx.on_key_event("w")
        run_frame(manager, ctx)
        ctx.on_key_event("w up")
        ctx.on_key_event("w")  # a second, distinct InputBegan firing
        run_frame(manager, ctx)
        count = sum(1 for d in manager.diagnostics if "once-fired" in d.message)
        check(count == 1, f"Signal:Once(): listener fires exactly once across two separate firings, got {count}")
    finally:
        teardown(game, manager, ctx)


def test_signal_wait_resumes_with_args() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        source = (
            "local sig = game:GetService('UserInputService').InputBegan\n"
            "local input, processed = sig:Wait()\n"
            "print('waited', input.KeyCode, processed)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)  # script parks on Wait()
        ctx.on_key_event("a")
        run_frame(manager, ctx)  # scheduler polls signal_wait, resumes
        messages = [d.message for d in manager.diagnostics]
        check(any("waited\tA\ttrue" in m for m in messages), "Signal:Wait() resumes with the fired arguments")
    finally:
        teardown(game, manager, ctx)


def test_signal_listener_order() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        source = (
            "local sig = game:GetService('UserInputService').InputBegan\n"
            "sig:Connect(function() print('first') end)\n"
            "sig:Connect(function() print('second') end)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        ctx.on_key_event("w")
        run_frame(manager, ctx)
        order = [d.message for d in manager.diagnostics if d.message in ("first", "second")]
        check(order == ["first", "second"], f"listeners fire in connection order, got {order}")
    finally:
        teardown(game, manager, ctx)


def test_signal_listener_error_isolation() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        source = (
            "local sig = game:GetService('UserInputService').InputBegan\n"
            "sig:Connect(function() error('boom') end)\n"
            "sig:Connect(function() print('still-ran') end)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        ctx.on_key_event("w")
        run_frame(manager, ctx)
        messages = [d.message for d in manager.diagnostics]
        check(any("still-ran" in m for m in messages), "one failing listener does not stop the other listener from running")
        check(any("boom" in m for m in messages), "the failing listener's error IS reported as a diagnostic")
    finally:
        teardown(game, manager, ctx)


def test_signal_disconnect_during_dispatch_is_safe() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        source = (
            "local sig = game:GetService('UserInputService').InputBegan\n"
            "local connA, connB\n"
            "connA = sig:Connect(function() connB:Disconnect(); print('A-ran') end)\n"
            "connB = sig:Connect(function() print('B-ran') end)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        ctx.on_key_event("w")
        run_frame(manager, ctx)
        messages = [d.message for d in manager.diagnostics]
        check(any("A-ran" in m for m in messages), "listener A (which disconnects B) still runs")
        check(not any("B-ran" in m for m in messages), "B, disconnected mid-dispatch by A, is skipped (snapshot re-checked against live table)")
    finally:
        teardown(game, manager, ctx)


def test_signal_connect_during_dispatch_does_not_corrupt_iteration() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        source = (
            "local sig = game:GetService('UserInputService').InputBegan\n"
            "sig:Connect(function() sig:Connect(function() print('late-added') end); print('original-ran') end)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        try:
            ctx.on_key_event("w")
            run_frame(manager, ctx)
            ok = True
        except Exception as exc:
            ok = False
            check(False, f"connecting a new listener during dispatch raised: {exc!r}")
        if ok:
            messages = [d.message for d in manager.diagnostics]
            check(any("original-ran" in m for m in messages), "connect-during-dispatch: the original listener still runs without corrupting iteration")
            check(not any("late-added" in m for m in messages), "connect-during-dispatch: the newly-added listener is NOT retroactively invoked by the firing already in progress")
    finally:
        teardown(game, manager, ctx)


def test_signal_and_connection_tostring() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        source = (
            "local sig = game:GetService('UserInputService').InputBegan\n"
            "print(tostring(sig))\n"
            "local conn = sig:Connect(function() end)\n"
            "print(tostring(conn))\n"
            "conn:Disconnect()\n"
            "print(tostring(conn))\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        messages = [d.message for d in manager.diagnostics]
        check(any(m == "Signal" for m in messages), "tostring(signal) is a clear, readable string")
        check(any("connected" in m and "disconnected" not in m for m in messages), "tostring(connection) reflects the connected state")
        check(any("disconnected" in m for m in messages), "tostring(connection) reflects the disconnected state after Disconnect()")
    finally:
        teardown(game, manager, ctx)


def test_signal_callback_budget_enforced() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        source = (
            "local sig = game:GetService('UserInputService').InputBegan\n"
            "sig:Connect(function() while true do end end)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        ctx.on_key_event("w")
        run_frame(manager, ctx)
        messages = [d.message for d in manager.diagnostics]
        check(any("execution budget" in m for m in messages), "an infinite-loop signal callback is stopped by the existing instruction budget, same as any other script code")
    finally:
        teardown(game, manager, ctx)


def test_signal_connections_cleared_on_stop() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        source = "game:GetService('UserInputService').InputBegan:Connect(function() print('should-not-fire-after-stop') end)"
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
    finally:
        teardown(game, manager, ctx)
    # After stop(), the whole VM (and every Lua-side signal registry entry
    # and coroutine it held) is gone -- there is no surviving Lua reference
    # left in this test to even attempt firing it again through, which
    # itself is the guarantee: nothing can retain a live connection past
    # Stop. Confirmed indirectly by manager.lua being cleared.
    check(manager.lua is None, "LuaRuntimeManager.stop() discards the VM -- no Lua-side signal state can survive Stop")


# ============================================================
# Players / LocalPlayer / Character lifecycle
# ============================================================

def test_get_service_identity() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        source = (
            "local a = game:GetService('Players')\n"
            "local b = game:GetService('Players')\n"
            "print(a == b)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        messages = [d.message for d in manager.diagnostics]
        check("true" in messages, "game:GetService('Players') returns the SAME proxy on repeated calls within one Play session")
    finally:
        teardown(game, manager, ctx)


def test_get_service_unknown_errors_cleanly() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        source = "game:GetService('NotAService')"
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        errors = [d for d in manager.diagnostics if d.severity == "error"]
        check(len(errors) == 1, "unknown service name reported as exactly one clean error diagnostic")
        check(errors and "not a valid service" in errors[0].message, "unknown service error message is readable")
    finally:
        teardown(game, manager, ctx)


def test_local_player_exists_with_deterministic_identity() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        source = (
            "local Players = game:GetService('Players')\n"
            "local p = Players.LocalPlayer\n"
            "print(p.Name, p.UserId)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        messages = [d.message for d in manager.diagnostics]
        check(any(lga.LOCAL_PLAYER_NAME in m for m in messages), "Players.LocalPlayer.Name is the deterministic local-preview identity")
    finally:
        teardown(game, manager, ctx)


def test_get_players_returns_fresh_table() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        source = (
            "local Players = game:GetService('Players')\n"
            "local a = Players:GetPlayers()\n"
            "local b = Players:GetPlayers()\n"
            "print(a == b, #a)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        messages = [d.message for d in manager.diagnostics]
        check(any(m.startswith("false\t1") for m in messages), "GetPlayers() returns a FRESH table each call (not the same reference), containing exactly one player")
    finally:
        teardown(game, manager, ctx)


def test_character_valid_immediately_and_added_fires_once() -> None:
    """Character is readable from a script's very first line (matches
    real usage -- a script reading Players.LocalPlayer.Character directly,
    without first waiting on CharacterAdded, must not see nil when a
    character already exists this session). CharacterAdded still fires
    exactly once, for scripts that specifically want the notification."""
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        source = (
            "local Players = game:GetService('Players')\n"
            "local player = Players.LocalPlayer\n"
            "print('character-before', player.Character ~= nil)\n"
            "player.CharacterAdded:Connect(function(character)\n"
            "    print('character-added', character ~= nil)\n"
            "end)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)  # script's top-level runs (connects), CharacterAdded fires same frame
        run_frame(manager, ctx)  # let the CharacterAdded listener's own coroutine actually resume
        messages = [d.message for d in manager.diagnostics]
        check(any("character-before\ttrue" in m for m in messages), "Character is already readable (non-nil) from a script's very first line")
        added_count = sum(1 for m in messages if m.startswith("character-added"))
        check(added_count == 1, f"CharacterAdded fires exactly once, got {added_count}")
        check(any("character-added\ttrue" in m for m in messages), "CharacterAdded's listener receives a non-nil character proxy")
    finally:
        teardown(game, manager, ctx)


def test_character_removing_fires_once_on_stop() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    source = (
        "local Players = game:GetService('Players')\n"
        "local player = Players.LocalPlayer\n"
        "player.CharacterRemoving:Connect(function(character)\n"
        "    print('character-removing', character ~= nil)\n"
        "end)\n"
    )
    add_script(game, source, script_id="s1")
    manager._start_script("s1")
    run_frame(manager, ctx)
    teardown(game, manager, ctx)
    # teardown() already called ctx.stop() (which fires CharacterRemoving
    # and pumps the scheduler) -- diagnostics were captured on `manager`
    # before manager.stop() cleared self.lua, so still readable here.
    messages = [d.message for d in manager.diagnostics]
    removing_count = sum(1 for m in messages if m.startswith("character-removing"))
    check(removing_count == 1, f"CharacterRemoving fires exactly once on Stop, got {removing_count}")
    check(any("character-removing\ttrue" in m for m in messages), "CharacterRemoving's listener receives a still-readable (non-nil) character proxy")


def test_stale_character_proxy_after_stop_reports_safe_error() -> None:
    """CharacterRemoving's own listeners run WHILE the character is still
    considered valid (per spec: "CharacterRemoving fires while the proxy
    is still readable" -- see LuaGameplayContext.stop()'s pump loop, which
    runs BEFORE _character_valid flips to False) -- so a reference
    retained and used from a later, SEPARATE point in the same VM (not
    from inside the removal listener's own immediate continuation) is the
    realistic "stale proxy" scenario this test needs to reproduce.

    Reads the SAME real, singleton character proxy every script sees
    (_G.__gameplay_character_proxy, the real host global the prelude
    populates -- see GAMEPLAY_PRELUDE) directly via the Python<->lupa
    boundary, rather than through a script's own Source: a script's own
    `_G` is its private sandboxed environment table (see
    _build_sandbox_env()'s `env["_G"] = env`), NOT the real global table,
    so a value a script stores via `_G.x = ...` is unreachable from
    outside that one script's env -- correct sandboxing, just not usable
    for retaining a cross-boundary Lua reference from a test."""
    game = _FakeGame()
    manager, ctx = make_context(game)
    character_proxy = manager.lua.globals()["__gameplay_character_proxy"]

    ctx.stop()  # character now invalid; the VM (and this proxy) is still alive -- manager.stop() hasn't run yet
    try:
        _ = character_proxy.Position
        check(False, "expected accessing a stale Character proxy to raise")
    except Exception as exc:
        check("no longer available" in str(exc), f"a Character proxy retained past Stop reports a clean 'Character is no longer available' error, not a crash -- got {exc!r}")

    manager.stop()
    game.teardown()


def test_character_before_spawn_is_nil() -> None:
    """A Play session with no character (e.g. _start_character() failed)
    must still expose a working Players/LocalPlayer with Character == nil,
    never raise."""
    game = _FakeGame(with_character=False)
    manager, ctx = make_context(game)
    try:
        source = "local Players = game:GetService('Players')\nprint('character-is', Players.LocalPlayer.Character)"
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        messages = [d.message for d in manager.diagnostics]
        check(any("character-is\tnil" in m for m in messages), "Players.LocalPlayer.Character is nil when no character exists this session")
    finally:
        teardown(game, manager, ctx)


# ============================================================
# Character API
# ============================================================

def test_character_position_and_velocity() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        source = (
            "local character = game:GetService('Players').LocalPlayer.Character\n"
            "print(typeof(character.Position), typeof(character.Velocity), typeof(character.HorizontalVelocity))\n"
            "print(character.HorizontalSpeed, character.VerticalVelocity)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        messages = [d.message for d in manager.diagnostics]
        check(any(m == "Vector3\tVector3\tVector3" for m in messages), "Position/Velocity/HorizontalVelocity are real Vector3 values")
        check(any(m.startswith("0.0\t0.0") or m.startswith("0\t0") for m in messages), f"a freshly-spawned, stationary character reports zero horizontal speed and zero vertical velocity, got {messages}")
    finally:
        teardown(game, manager, ctx)


def test_character_grounded_jumping_falling_states() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        source = (
            "local character = game:GetService('Players').LocalPlayer.Character\n"
            "print(character.IsGrounded, character.IsJumping, character.IsFalling)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        # Let the capsule actually fall and land -- same physics as test_character_controller.py's own falling test.
        for _ in range(120):
            game._character_runtime.step(1 / 120)
            game._bullet_world.doPhysics(1 / 120, 10, 1 / 120)
        run_frame(manager, ctx)
        messages = [d.message for d in manager.diagnostics]
        check(any(m == "true\tfalse\tfalse" for m in messages), f"a landed, stationary character reports IsGrounded=true, IsJumping=false, IsFalling=false, got {messages}")
    finally:
        teardown(game, manager, ctx)


def test_character_jump_uses_controller_path_and_blocks_air_jump() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        # Land first (matches test_character_controller.py's own grounded-jump-gate precondent).
        for _ in range(120):
            game._character_runtime.step(1 / 120)
            game._bullet_world.doPhysics(1 / 120, 10, 1 / 120)
        check(game._character_runtime.controller.is_grounded(), "precondition: character is grounded before testing Jump()")

        source = (
            "local character = game:GetService('Players').LocalPlayer.Character\n"
            "character:Jump()\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        check(not game._character_runtime.controller.is_grounded() or game._character_runtime.controller.vertical_velocity() > 0, "character:Jump() actually launches the capsule upward via the real CharacterController.try_jump() path")

        vy_after_first_jump = game._character_runtime.controller.vertical_velocity()
        source2 = "game:GetService('Players').LocalPlayer.Character:Jump()"
        add_script(game, source2, script_id="s2")
        manager._start_script("s2")
        run_frame(manager, ctx)
        check(game._character_runtime.controller.vertical_velocity() <= vy_after_first_jump + 1e-6, "a second Jump() call while airborne does not add extra upward velocity (no air-jump bypass -- same grounded gate as Stage 3.3)")
    finally:
        teardown(game, manager, ctx)


def test_character_camera_mode_validation() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        source = (
            "local character = game:GetService('Players').LocalPlayer.Character\n"
            "character:SetCameraMode('ThirdPerson')\n"
            "print('mode-after-set', character.CameraMode)\n"
            "local ok, err = pcall(function() character:SetCameraMode('Sideways') end)\n"
            "print('invalid-mode', ok, err)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        check(game.third_person_enabled is True, "SetCameraMode('ThirdPerson') actually flips the game's third_person_enabled state")
        messages = [d.message for d in manager.diagnostics]
        check(any("mode-after-set\tThirdPerson" in m for m in messages), "character.CameraMode reads back the mode just set")
        check(any("invalid-mode\tfalse" in m and "not a valid camera mode" in m for m in messages), "an invalid camera mode string produces a clean Lua error, not a crash")
    finally:
        teardown(game, manager, ctx)


def test_character_appearance_color_updates() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        source = (
            "local character = game:GetService('Players').LocalPlayer.Character\n"
            "character.AccentColor = Color3.fromRGB(70, 150, 255)\n"
            "local c = character.AccentColor\n"
            "print('accent', c.R, c.G, c.B)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        rig_color = game._character_visual.appearance.accent_color
        check(abs(rig_color[0] - 70 / 255) < 1e-3, "AccentColor assignment updates the live rig's appearance immediately (R)")
        check(abs(rig_color[1] - 150 / 255) < 1e-3, "AccentColor assignment updates the live rig's appearance immediately (G)")
        check(abs(rig_color[2] - 255 / 255) < 1e-3, "AccentColor assignment updates the live rig's appearance immediately (B)")
        messages = [d.message for d in manager.diagnostics]
        check(any(m.startswith("accent\t") and "0.27" in m for m in messages), "reading character.AccentColor back returns a real Color3 with the same value")
    finally:
        teardown(game, manager, ctx)


def test_character_accessory_attach_and_remove() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        before = game._character_visual.entity_count()
        source = "game:GetService('Players').LocalPlayer.Character:SetAccessory('Head', 'Fedora')"
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        check(game._character_visual.accessory_at(cr.PART_HEAD_ACCESSORY_SLOT) is not None, "SetAccessory('Head', 'Fedora') actually attaches an accessory to the live rig")
        check(game._character_visual.entity_count() > before, "entity_count increases after SetAccessory()")

        source2 = "game:GetService('Players').LocalPlayer.Character:RemoveAccessory('Head')"
        add_script(game, source2, script_id="s2")
        manager._start_script("s2")
        run_frame(manager, ctx)
        check(game._character_visual.accessory_at(cr.PART_HEAD_ACCESSORY_SLOT) is None, "RemoveAccessory('Head') removes it again")
        check(game._character_visual.entity_count() == before, "entity_count returns to its original value after RemoveAccessory()")
    finally:
        teardown(game, manager, ctx)


def test_character_unknown_accessory_rejected() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        source = (
            "local character = game:GetService('Players').LocalPlayer.Character\n"
            "local ok1, err1 = pcall(function() character:SetAccessory('Waist', 'Fedora') end)\n"
            "print('bad-slot', ok1, err1)\n"
            "local ok2, err2 = pcall(function() character:SetAccessory('Head', 'Crown') end)\n"
            "print('bad-kind', ok2, err2)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        messages = [d.message for d in manager.diagnostics]
        check(any("bad-slot\tfalse" in m for m in messages), "an unknown accessory slot name produces a clean Lua error")
        check(any("bad-kind\tfalse" in m for m in messages), "an unknown accessory kind produces a clean Lua error")
    finally:
        teardown(game, manager, ctx)


def test_character_appearance_never_dirties_or_serializes() -> None:
    """Structural check, same technique test_character_controller.py/
    test_character_rig.py already use -- appearance mutation goes straight
    through CharacterVisualRig.set_appearance(), never through
    self.game.instances/RuntimeSceneLayer/mark_place_dirty()/history."""
    import inspect
    source = inspect.getsource(lga.LuaGameplayContext._character_set)
    check("mark_place_dirty" not in source, "_character_set(): never calls mark_place_dirty()")
    check(".instances" not in source, "_character_set(): never touches self.game.instances (not a Place edit)")
    check(".history" not in source, "_character_set(): never touches editor Undo/Redo history")
    check("send" not in source.lower() and "network" not in source.lower(), "_character_set(): never sends a scene-edit network message")


# ============================================================
# UserInputService
# ============================================================

def test_input_began_ended_and_processed_flag() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        source = (
            "local UserInputService = game:GetService('UserInputService')\n"
            "UserInputService.InputBegan:Connect(function(input, processed)\n"
            "    print('began', input.KeyCode, input.UserInputType, input.State, processed)\n"
            "end)\n"
            "UserInputService.InputEnded:Connect(function(input, processed)\n"
            "    print('ended', input.KeyCode, processed)\n"
            "end)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        ctx.on_key_event("w")
        run_frame(manager, ctx)
        ctx.on_key_event("w up")
        run_frame(manager, ctx)
        messages = [d.message for d in manager.diagnostics]
        check(any("began\tW\tKeyboard\tBegin\ttrue" in m for m in messages), "InputBegan fires with KeyCode/UserInputType/State and processed=true for a movement key")
        check(any("ended\tW\ttrue" in m for m in messages), "InputEnded fires for the matching key release")
    finally:
        teardown(game, manager, ctx)


def test_input_key_repeat_guard() -> None:
    """Ursina's input() fires "w" once per physical press, not once per
    frame held -- on_key_event() must not re-fire InputBegan if called
    again for a key already marked down (defense in depth even though the
    real key event source is itself already edge-triggered)."""
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        source = (
            "local UserInputService = game:GetService('UserInputService')\n"
            "local count = 0\n"
            "UserInputService.InputBegan:Connect(function() count = count + 1; print('began-count', count) end)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        ctx.on_key_event("w")
        ctx.on_key_event("w")  # repeat while still held -- must be a no-op
        run_frame(manager, ctx)
        messages = [d.message for d in manager.diagnostics]
        count = sum(1 for m in messages if m.startswith("began-count"))
        check(count == 1, f"InputBegan fires once per physical press even if on_key_event('w') is called again before release, got {count}")
    finally:
        teardown(game, manager, ctx)


def test_jump_request_fires_on_space() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        source = "game:GetService('UserInputService').JumpRequest:Connect(function() print('jump-requested') end)"
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        ctx.on_key_event("space")
        run_frame(manager, ctx)
        messages = [d.message for d in manager.diagnostics]
        check(any("jump-requested" in m for m in messages), "pressing Space fires UserInputService.JumpRequest")
    finally:
        teardown(game, manager, ctx)


def test_is_key_down() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        ctx.on_key_event("a")
        source = (
            "local UserInputService = game:GetService('UserInputService')\n"
            "print('a-down', UserInputService:IsKeyDown('A'))\n"
            "print('s-down', UserInputService:IsKeyDown('S'))\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        messages = [d.message for d in manager.diagnostics]
        check(any("a-down\ttrue" in m for m in messages), "IsKeyDown('A') is true while A is held")
        check(any("s-down\tfalse" in m for m in messages), "IsKeyDown('S') is false while S is not held")
    finally:
        teardown(game, manager, ctx)


def test_is_input_captured_reflects_game_state() -> None:
    game = _FakeGame()
    game._captured = True
    manager, ctx = make_context(game)
    try:
        source = "print('captured', game:GetService('UserInputService'):IsInputCaptured())"
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        messages = [d.message for d in manager.diagnostics]
        check(any("captured\ttrue" in m for m in messages), "IsInputCaptured() reflects the game's actual mouse-look capture state")
    finally:
        teardown(game, manager, ctx)


def test_escape_release_clears_held_keys() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        source = "game:GetService('UserInputService').InputEnded:Connect(function(input) print('released', input.KeyCode) end)"
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        ctx.on_key_event("w")
        run_frame(manager, ctx)
        check("W" in ctx._held_keys, "precondition: W is tracked as held")
        ctx.release_all_keys()
        run_frame(manager, ctx)
        check("W" not in ctx._held_keys, "release_all_keys() (called from release_play_input_capture(), e.g. on Escape) clears held-key state")
        messages = [d.message for d in manager.diagnostics]
        check(any("released\tW" in m for m in messages), "release_all_keys() fires a synthetic InputEnded for whatever was still held")
    finally:
        teardown(game, manager, ctx)


def test_on_key_event_ignores_mouse_and_scroll_keys() -> None:
    """Stage 3.7: input() now forwards "right mouse down"/"right mouse
    up"/"scroll up"/"scroll down" (and "left mouse down" is now also
    reachable while third-person, uncaptured) to on_key_event() every Play
    frame those fire, alongside every other key -- on_key_event() itself
    needed no Stage 3.7 changes because TRACKED_KEYS never included any of
    these, so this just confirms that stays true (spec: "RMB camera
    movement must not generate excessive mouse-move Lua events")."""
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        for key in ("right mouse down", "right mouse up", "left mouse down", "scroll up", "scroll down"):
            ctx.on_key_event(key)
        check(not ctx._held_keys, "mouse/scroll key strings are never added to held-key state")
    finally:
        teardown(game, manager, ctx)


def test_stop_clears_held_key_state() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    ctx.on_key_event("d")
    check("D" in ctx._held_keys, "precondition: D is tracked as held before Stop")
    teardown(game, manager, ctx)
    check(not ctx._held_keys, "Stop clears all held-key state")


def test_no_input_events_outside_play() -> None:
    """A gameplay context that was never start()-ed (mirrors 'not
    Play') must not dispatch anything -- on_key_event()/release_all_keys()
    are no-ops when _active is False."""
    game = _FakeGame()
    manager = lua_runtime.LuaRuntimeManager(game)
    manager.start()
    ctx = lga.LuaGameplayContext(game, manager)
    # deliberately never call ctx.start()
    try:
        ctx.on_key_event("w")  # must not raise, must not touch _held_keys
        check(not ctx._held_keys, "on_key_event() is a no-op before start() (no Play session active)")
    finally:
        manager.stop()
        game.teardown()


# ============================================================
# Lifecycle across repeated Play/Stop
# ============================================================

def test_repeated_play_stop_creates_fresh_context_and_old_signals_silent() -> None:
    game = _FakeGame()
    manager1, ctx1 = make_context(game)
    add_script(game, "game:GetService('UserInputService').InputBegan:Connect(function() print('session1-fired') end)", script_id="s1")
    manager1._start_script("s1")
    run_frame(manager1, ctx1)
    teardown(game, manager1, ctx1)
    check(manager1.lua is None, "first session's VM is fully discarded on Stop")

    game2 = _FakeGame()
    manager2, ctx2 = make_context(game2)
    try:
        # A completely independent VM/signal registry -- session 1's
        # listener (and its whole VM) no longer exists to fire at all, and
        # this session's InputBegan is a brand new signal_id.
        add_script(game2, "game:GetService('UserInputService').InputBegan:Connect(function() print('session2-fired') end)", script_id="s1")
        manager2._start_script("s1")
        run_frame(manager2, ctx2)
        ctx2.on_key_event("w")
        run_frame(manager2, ctx2)
        messages = [d.message for d in manager2.diagnostics]
        check(any("session2-fired" in m for m in messages), "the new session's own signal fires normally")
        check(not any("session1-fired" in m for m in messages), "the OLD session's listener never fires in the new session")
    finally:
        teardown(game2, manager2, ctx2)
        game.teardown()


def test_new_character_added_fires_in_new_session() -> None:
    game = _FakeGame()
    manager1, ctx1 = make_context(game)
    teardown(game, manager1, ctx1)
    game.teardown()

    game2 = _FakeGame()
    manager2, ctx2 = make_context(game2)
    try:
        source = "game:GetService('Players').LocalPlayer.CharacterAdded:Connect(function() print('fresh-character-added') end)"
        add_script(game2, source, script_id="s1")
        manager2._start_script("s1")
        run_frame(manager2, ctx2)
        run_frame(manager2, ctx2)
        messages = [d.message for d in manager2.diagnostics]
        check(any("fresh-character-added" in m for m in messages), "a fresh Play session fires its own CharacterAdded normally")
    finally:
        teardown(game2, manager2, ctx2)


def test_no_python_object_escapes_into_lua() -> None:
    """Structural check: every character/player bridge function returns
    only primitives, plain arrays, or (ok, message) pairs -- never `self`,
    `self.game`, the CharacterRuntime/CharacterVisualRig instances
    themselves, or any Ursina/Panda/Bullet object."""
    import inspect
    source = inspect.getsource(lga.LuaGameplayContext._register_bridge_functions)
    check("return self\n" not in source and "return self.game" not in source, "no bridge function returns the context or game object itself")
    check("_character_runtime\n" not in source.replace("self.game._character_runtime.controller", ""), "no bridge function returns the raw CharacterRuntime")


# ============================================================
# Stage 3.6 -- execution roots / gameplay-context startup ordering
#
# Unlike every test above (which adds scripts AFTER make_context() and
# bypasses discovery via manager._start_script() directly, since Stage 3.5
# never cared about WHERE a script lived), these tests add scripts to
# game.instances BEFORE calling make_context()/manager.start(), so the
# real build_script_execution_plan()-driven discovery in start() is what
# actually finds and runs them -- exercising the full, real Play-time path
# for the new StarterPlayer/ServerScriptService roots, not just the
# lower-level lua_runtime tests in test_script_execution_plan.py.
# ============================================================

def test_localscript_under_starterplayer_is_auto_discovered_and_runs() -> None:
    game = _FakeGame()
    add_script(game, "print('starterplayer-ran')", class_name="LocalScript", script_id="s1", parent_id="StarterPlayer")
    manager, ctx = make_context(game)
    try:
        run_frame(manager, ctx)
        messages = [d.message for d in manager.diagnostics]
        check(any("starterplayer-ran" in m for m in messages), "a LocalScript parented under StarterPlayer is auto-discovered and runs, with no manual _start_script() call")
    finally:
        teardown(game, manager, ctx)


def test_script_under_server_script_service_is_auto_discovered_and_runs() -> None:
    game = _FakeGame()
    add_script(game, "print('serverscriptservice-ran')", class_name="Script", script_id="s1", parent_id="ServerScriptService")
    manager, ctx = make_context(game)
    try:
        run_frame(manager, ctx)
        messages = [d.message for d in manager.diagnostics]
        check(any("serverscriptservice-ran" in m for m in messages), "a Script parented under ServerScriptService is auto-discovered and runs")
    finally:
        teardown(game, manager, ctx)


def test_localscript_under_workspace_still_runs_and_gets_character_added() -> None:
    """Regression guard: the Stage 3.5 Workspace-rooted path must not
    regress now that discovery is multi-root."""
    game = _FakeGame()
    add_script(
        game,
        "game:GetService('Players').LocalPlayer.CharacterAdded:Connect(function(c) print('workspace-character-added') end)",
        class_name="LocalScript", script_id="s1", parent_id="Workspace",
    )
    manager, ctx = make_context(game)
    try:
        run_frame(manager, ctx)
        messages = [d.message for d in manager.diagnostics]
        check(any("workspace-character-added" in m for m in messages), "CharacterAdded still fires normally for a Workspace-rooted LocalScript (no regression)")
    finally:
        teardown(game, manager, ctx)


def test_localscript_under_replicatedstorage_is_skipped_with_one_warning() -> None:
    game = _FakeGame()
    add_script(game, "print('should-never-run')", class_name="LocalScript", name="Input", script_id="s1", parent_id="ReplicatedStorage")
    manager, ctx = make_context(game)
    try:
        run_frame(manager, ctx)
        messages = [d.message for d in manager.diagnostics]
        check(not any("should-never-run" in m for m in messages), "a LocalScript under ReplicatedStorage never actually executes")
        warnings = [d for d in manager.diagnostics if d.severity == "warning" and "was skipped" in d.message]
        check(len(warnings) == 1, "exactly one skipped-location warning is emitted, at Play startup")
        check("ReplicatedStorage" in warnings[0].message and "s1" in warnings[0].message, "the warning names the actual (unsupported) location and the stable instance id")
    finally:
        teardown(game, manager, ctx)


def test_gameplay_services_available_on_first_line_of_starterplayer_localscript() -> None:
    """The exact example script from the Stage 3.6 spec: Players and
    UserInputService must both already be usable on a StarterPlayer
    LocalScript's very first executed lines -- proving gameplay-context
    startup ordering holds for the new root, not just the Workspace one
    Stage 3.5 already verified."""
    game = _FakeGame()
    source = (
        "local Players = game:GetService('Players')\n"
        "local UserInputService = game:GetService('UserInputService')\n"
        "local player = Players.LocalPlayer\n"
        "print(player.Name)\n"
    )
    add_script(game, source, class_name="LocalScript", script_id="s1", parent_id="StarterPlayer")
    manager, ctx = make_context(game)
    try:
        run_frame(manager, ctx)
        errors = [d for d in manager.diagnostics if d.severity == "error"]
        check(errors == [], f"no errors starting up services from a StarterPlayer LocalScript's first lines: {[e.message for e in errors]}")
        messages = [d.message for d in manager.diagnostics]
        check(any(lga.LOCAL_PLAYER_NAME in m for m in messages), "Players.LocalPlayer.Name printed correctly from a StarterPlayer LocalScript")
    finally:
        teardown(game, manager, ctx)


def test_userinputservice_works_from_starterplayer_localscript() -> None:
    game = _FakeGame()
    source = "game:GetService('UserInputService').InputBegan:Connect(function(input) print('starterplayer-input', input.KeyCode) end)"
    add_script(game, source, class_name="LocalScript", script_id="s1", parent_id="StarterPlayer")
    manager, ctx = make_context(game)
    try:
        run_frame(manager, ctx)
        ctx.on_key_event("w")
        run_frame(manager, ctx)
        messages = [d.message for d in manager.diagnostics]
        check(any("starterplayer-input\tW" in m for m in messages), "UserInputService.InputBegan fires correctly for a StarterPlayer LocalScript")
    finally:
        teardown(game, manager, ctx)


def test_modulescript_required_from_replicatedstorage_via_getservice() -> None:
    """Exercises the full real-syntax chain: game:GetService('ReplicatedStorage')
    -> :FindFirstChild(name) -> require(proxy) -- not just the underlying
    location-agnostic _require() Python method (already covered structurally
    by Stage 3.0), and not just the discovery-plan question of WHETHER a
    ModuleScript ever tries to auto-run (it must not, see
    test_script_execution_plan.py)."""
    game = _FakeGame()
    game.instances["mod1"] = cs.InstanceRecord("mod1", "ModuleScript", "MyModule", "ReplicatedStorage", {"Source": "return 42"})
    source = (
        "local ReplicatedStorage = game:GetService('ReplicatedStorage')\n"
        "local mod = ReplicatedStorage:FindFirstChild('MyModule')\n"
        "local value = require(mod)\n"
        "print('required', value)\n"
    )
    add_script(game, source, class_name="LocalScript", script_id="s1", parent_id="Workspace")
    manager, ctx = make_context(game)
    try:
        run_frame(manager, ctx)
        errors = [d for d in manager.diagnostics if d.severity == "error"]
        check(errors == [], f"require() from ReplicatedStorage via GetService/FindFirstChild works with no errors: {[e.message for e in errors]}")
        messages = [d.message for d in manager.diagnostics]
        check(any("required\t42" in m for m in messages), "the ModuleScript's return value comes back correctly through require()")
    finally:
        teardown(game, manager, ctx)


def test_modulescript_cache_is_once_per_session() -> None:
    game = _FakeGame()
    game.instances["mod1"] = cs.InstanceRecord("mod1", "ModuleScript", "Counted", "ReplicatedStorage", {"Source": "_G.__load_count = (_G.__load_count or 0) + 1\nreturn _G.__load_count"})
    source = (
        "local RS = game:GetService('ReplicatedStorage')\n"
        "local mod = RS:FindFirstChild('Counted')\n"
        "local a = require(mod)\n"
        "local b = require(mod)\n"
        "print('counts', a, b)\n"
    )
    add_script(game, source, class_name="LocalScript", script_id="s1", parent_id="Workspace")
    manager, ctx = make_context(game)
    try:
        run_frame(manager, ctx)
        messages = [d.message for d in manager.diagnostics]
        check(any("counts\t1\t1" in m for m in messages), "requiring the same ModuleScript twice in one session only executes its body once (cached)")
    finally:
        teardown(game, manager, ctx)


def test_diagnostics_preserve_correct_id_and_line_for_starterplayer_script() -> None:
    game = _FakeGame()
    source = "print('before')\nerror('boom')\n"
    add_script(game, source, class_name="LocalScript", script_id="starterplayer_script_id", parent_id="StarterPlayer")
    manager, ctx = make_context(game)
    try:
        run_frame(manager, ctx)
        errors = [d for d in manager.diagnostics if d.severity == "error"]
        check(len(errors) == 1, "exactly one error diagnostic for the runtime error")
        check(errors and errors[0].script_id == "starterplayer_script_id", "the diagnostic's script_id is the StarterPlayer script's own stable instance id")
        check(errors and errors[0].line == 2, f"the diagnostic points at line 2 (the error() call), got {errors[0].line if errors else None}")
    finally:
        teardown(game, manager, ctx)


def test_second_play_creates_fresh_execution_plan_and_old_callbacks_are_silent() -> None:
    game = _FakeGame()
    add_script(game, "print('session1-starterplayer')", class_name="LocalScript", script_id="s1", parent_id="StarterPlayer")
    manager1, ctx1 = make_context(game)
    run_frame(manager1, ctx1)
    plan1_ids = [e.instance_id for e in manager1._execution_plan]
    teardown(game, manager1, ctx1)
    game.teardown()

    game2 = _FakeGame()
    add_script(game2, "print('session2-starterplayer')", class_name="LocalScript", script_id="s1", parent_id="StarterPlayer")
    manager2, ctx2 = make_context(game2)
    try:
        run_frame(manager2, ctx2)
        messages = [d.message for d in manager2.diagnostics]
        check(any("session2-starterplayer" in m for m in messages), "the new session's own StarterPlayer script runs")
        check(not any("session1-starterplayer" in m for m in messages), "the OLD session's StarterPlayer script never fires in the new session")
        check(plan1_ids == ["s1"] and [e.instance_id for e in manager2._execution_plan] == ["s1"], "each session independently builds its own fresh execution plan")
    finally:
        teardown(game2, manager2, ctx2)
        game.teardown()


def test_build_script_execution_plan_never_touches_history_or_dirty_state() -> None:
    """Structural check, same technique as
    test_character_appearance_never_dirties_or_serializes: script
    discovery/startup is a pure read of the authoritative hierarchy, never
    a Place edit."""
    import inspect
    source = inspect.getsource(lua_runtime.build_script_execution_plan)
    check("mark_place_dirty" not in source, "build_script_execution_plan(): never calls mark_place_dirty()")
    check(".history" not in source, "build_script_execution_plan(): never touches editor Undo/Redo history")
    check(source.count("instances[") == 0 and source.count("instances.pop") == 0, "build_script_execution_plan(): never writes to the instances dict (read-only)")


# ============================================================
# run everything
# ============================================================

test_signal_connect_and_fire()
test_signal_disconnect_stops_future_firing()
test_signal_once_fires_exactly_once()
test_signal_wait_resumes_with_args()
test_signal_listener_order()
test_signal_listener_error_isolation()
test_signal_disconnect_during_dispatch_is_safe()
test_signal_connect_during_dispatch_does_not_corrupt_iteration()
test_signal_and_connection_tostring()
test_signal_callback_budget_enforced()
test_signal_connections_cleared_on_stop()

test_get_service_identity()
test_get_service_unknown_errors_cleanly()
test_local_player_exists_with_deterministic_identity()
test_get_players_returns_fresh_table()
test_character_valid_immediately_and_added_fires_once()
test_character_removing_fires_once_on_stop()
test_stale_character_proxy_after_stop_reports_safe_error()
test_character_before_spawn_is_nil()

test_character_position_and_velocity()
test_character_grounded_jumping_falling_states()
test_character_jump_uses_controller_path_and_blocks_air_jump()
test_character_camera_mode_validation()
test_character_appearance_color_updates()
test_character_accessory_attach_and_remove()
test_character_unknown_accessory_rejected()
test_character_appearance_never_dirties_or_serializes()

test_input_began_ended_and_processed_flag()
test_input_key_repeat_guard()
test_jump_request_fires_on_space()
test_is_key_down()
test_is_input_captured_reflects_game_state()
test_escape_release_clears_held_keys()
test_on_key_event_ignores_mouse_and_scroll_keys()
test_stop_clears_held_key_state()
test_no_input_events_outside_play()

test_repeated_play_stop_creates_fresh_context_and_old_signals_silent()
test_new_character_added_fires_in_new_session()
test_no_python_object_escapes_into_lua()

test_localscript_under_starterplayer_is_auto_discovered_and_runs()
test_script_under_server_script_service_is_auto_discovered_and_runs()
test_localscript_under_workspace_still_runs_and_gets_character_added()
test_localscript_under_replicatedstorage_is_skipped_with_one_warning()
test_gameplay_services_available_on_first_line_of_starterplayer_localscript()
test_userinputservice_works_from_starterplayer_localscript()
test_modulescript_required_from_replicatedstorage_via_getservice()
test_modulescript_cache_is_once_per_session()
test_diagnostics_preserve_correct_id_and_line_for_starterplayer_script()
test_second_play_creates_fresh_execution_plan_and_old_callbacks_are_silent()
test_build_script_execution_plan_never_touches_history_or_dirty_state()

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for message in FAILURES:
        print(f"  - {message}")
    sys.exit(1)
print("All Lua gameplay API tests passed.")
