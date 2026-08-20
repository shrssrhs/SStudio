"""Regression tests for Stage 4.1's atmospheric rendering foundation: the
new `Environment` root service (fog/exposure/directional shadows) and its
application through client_studio.MultiplayerGame.apply_environment_settings().

Scope, matching the sprint spec exactly: these tests cover what is
objectively testable -- schema/registration, type validation, save/open
persistence, runtime property application (the right values reach the right
objects), default values, repeated Play/Stop not re-touching shadow state,
fog enable/disable, exposure change, and shadow configuration state. They
deliberately do NOT claim "the shadow looks correct" or "the fog looks
atmospheric" -- that is manual visual acceptance, not something an
automated assertion can honestly claim.

Architecture under test:
  - shared/object_registry.py: "Environment" added to ROOT_SERVICES.
  - datamodel_schema.py: Environment ClassDescriptor with 6 properties
    (FogEnabled/FogColor/FogDensity/Exposure/ShadowsEnabled/ShadowDistance).
  - client_studio.py: MultiplayerGame.pbr_pipeline (the ONE simplepbr.
    Pipeline object, captured once in main(), never recreated) and
    apply_environment_settings() (the ONE place Environment properties are
    ever applied -- world-snapshot load, Play start, Stop-restore, and
    runtime Lua writes all funnel through it).

Follows this project's existing test convention: plain top-level-assertion
script, run directly, offscreen Qt platform, headless Ursina window, real
Ursina/Panda3D objects (a real DirectionalLight, a real Fog node) -- state
is checked against the real Panda3D objects, not inferred from source. The
simplepbr Pipeline itself is stubbed (a plain object with the same
enable_fog/exposure attributes) rather than constructing a real one, since
a real Pipeline needs shader compilation against a live graphics context --
what's under test here is that SStudio writes the correct values to
whatever Pipeline it owns, not simplepbr's own correctness.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, '.')

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from ursina import AmbientLight, DirectionalLight, Ursina

ursina_app = Ursina(window_type="none")

from panda3d.core import Fog
from ursina import application

import client_studio as cs
import datamodel_schema
import lua_gameplay_api as lga
import lua_runtime
import physics

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"ok: {message}")


class _FakePipeline:
    """Stands in for simplepbr.Pipeline -- only the two attributes
    apply_environment_settings() actually writes to."""

    def __init__(self) -> None:
        self.enable_fog = False
        self.exposure = 0.0


class _FakeGame:
    """Extends the project's established minimal-harness pattern (see
    test_world_membership.py/test_mesh_part.py) with what THIS slice needs:
    a real DirectionalLight/Fog and a stub Pipeline, plus the real Lua
    machinery for the runtime-write session-only contract test."""

    apply_environment_settings = cs.MultiplayerGame.apply_environment_settings
    apply_runtime_service_write = cs.MultiplayerGame.apply_runtime_service_write
    _build_part_entity = cs.MultiplayerGame._build_part_entity

    def __init__(self, with_pipeline: bool = True) -> None:
        self.instances: dict[str, cs.InstanceRecord] = {}
        self.parts: dict = {}
        self.studio_adapter = None
        self.third_person_enabled = False
        self._captured = False
        self._character_runtime = None
        self._character_visual = None
        self._physics_world = physics.PhysicsWorld(gravity=-24.0)
        self.services: dict[str, dict] = datamodel_schema.sanitize_services_snapshot(None)
        self._runtime_min_zoom = 2.0
        self._runtime_max_zoom = 10.0
        self.applied_service_writes: list[tuple[str, dict]] = []

        # Real Ursina/Panda3D lighting objects -- shadows=False here for
        # the same reason create_world() passes it: suppress Ursina's own
        # deferred (invoke(), ~1 frame later) default shadow setup so it
        # can't race with a test's own synchronous assertions.
        self.sun = DirectionalLight(shadows=False)
        self.ambient_light = AmbientLight()
        self._environment_fog = Fog("TestEnvironmentFog")
        self.pbr_pipeline = _FakePipeline() if with_pipeline else None
        self._environment_state: dict | None = None

    def apply_runtime_service_write_stub(self, service_name: str, properties: dict) -> None:
        self.applied_service_writes.append((service_name, dict(properties)))

    def _mouse_look_captured(self) -> bool:
        return self._captured

    def set_camera_mode(self, third_person: bool) -> None:
        pass

    def teardown(self) -> None:
        self._physics_world.destroy()
        application.base.render.clearFog()


def make_context(game: _FakeGame) -> tuple[lua_runtime.LuaRuntimeManager, lga.LuaGameplayContext]:
    manager = lua_runtime.LuaRuntimeManager(game)
    manager.start()
    ctx = lga.LuaGameplayContext(game, manager)
    ctx.start()
    return manager, ctx


def teardown(game: _FakeGame, manager: lua_runtime.LuaRuntimeManager, ctx: lga.LuaGameplayContext) -> None:
    ctx.stop()
    manager.stop()
    game.teardown()


# ============================================================
# Registry / schema
# ============================================================

def test_environment_is_registered_as_a_root_service() -> None:
    import shared.object_registry as reg
    check("Environment" in reg.ROOT_SERVICES, "Environment is a root service")
    descriptor = datamodel_schema.get_class("Environment")
    check(descriptor is not None, "Environment is registered in datamodel_schema")
    check(descriptor.service, "Environment.service is True")
    check(descriptor.singleton, "Environment.singleton is True")
    check(not descriptor.creatable, "Environment is not creatable (a root service, like Workspace)")
    names = {p.name for p in datamodel_schema.get_all_properties("Environment")}
    for expected in ("FogEnabled", "FogColor", "FogDensity", "Exposure", "ShadowsEnabled", "ShadowDistance"):
        check(expected in names, f"Environment declares {expected}")


def test_default_values_match_current_appearance() -> None:
    defaults = datamodel_schema.default_properties("Environment")
    check(defaults["FogEnabled"] is False, "FogEnabled defaults to False (no fog change from current appearance)")
    check(defaults["Exposure"] == 0.0, "Exposure defaults to 0.0 (neutral, matches current appearance)")
    check(defaults["ShadowsEnabled"] is True, "ShadowsEnabled defaults to True (existing DirectionalLight already casts light)")
    check(defaults["ShadowDistance"] == 40.0, "ShadowDistance has a sane default")
    check(defaults["AmbientColor"] == [68.0, 68.0, 82.0], "AmbientColor defaults to the exact value the old hardcoded constant held")
    check(defaults["AmbientIntensity"] == 1.0, "AmbientIntensity defaults to 1.0 (neutral multiplier -- reproduces AmbientColor exactly)")


def test_environment_reachable_via_sanitize_services_snapshot() -> None:
    snapshot = datamodel_schema.sanitize_services_snapshot(None)
    check("Environment" in snapshot, "a full services snapshot always includes Environment")
    check(snapshot["Environment"]["ShadowsEnabled"] is True, "...with correct defaults filled in")


# ============================================================
# Type validation
# ============================================================

def test_property_validation_rejects_bad_values() -> None:
    result = datamodel_schema.validate_property_value("Environment", "FogDensity", 999.0)
    check(not result.ok, "FogDensity above its maximum is rejected")
    result = datamodel_schema.validate_property_value("Environment", "FogDensity", -1.0)
    check(not result.ok, "negative FogDensity is rejected")
    result = datamodel_schema.validate_property_value("Environment", "Exposure", 100.0)
    check(not result.ok, "Exposure far outside its bounds is rejected")
    result = datamodel_schema.validate_property_value("Environment", "Exposure", 2.0)
    check(result.ok, "Exposure within bounds is accepted")
    result = datamodel_schema.validate_property_value("Environment", "FogEnabled", "yes")
    check(not result.ok, "a non-bool FogEnabled is rejected")
    result = datamodel_schema.validate_property_value("Environment", "FogColor", [10, 20, 30])
    check(result.ok, "a valid color3 FogColor is accepted")
    result = datamodel_schema.validate_property_value("Environment", "ShadowDistance", 0.0)
    check(not result.ok, "ShadowDistance below its minimum is rejected")


# ============================================================
# Persistence: save -> close -> open -> same configuration
# ============================================================

def test_environment_persists_through_save_open_round_trip() -> None:
    import tempfile
    import place_manager as pm

    with tempfile.TemporaryDirectory() as tmp_dir:
        path = os.path.join(tmp_dir, "environment_roundtrip.nebula.json")
        custom_environment = {
            "FogEnabled": True,
            "FogColor": [90.0, 95.0, 110.0],
            "FogDensity": 0.03,
            "Exposure": -1.5,
            "ShadowsEnabled": True,
            "ShadowDistance": 25.0,
        }
        services = datamodel_schema.sanitize_services_snapshot({"Environment": custom_environment})
        check(services["Environment"]["FogDensity"] == 0.03, "sanitize_services_snapshot keeps the custom FogDensity")

        manager = pm.PlaceManager()
        save_result = manager.save_as(path, [], services)
        check(save_result.success, f"saving a Place with custom Environment succeeds: {save_result.message}")

        reopened = pm.PlaceManager()
        open_result = reopened.open(path)
        check(open_result.success, f"reopening succeeds: {open_result.message}")
        reopened_environment = (open_result.services or {}).get("Environment", {})
        check(reopened_environment.get("FogEnabled") is True, "FogEnabled survives save/open")
        check(reopened_environment.get("FogDensity") == 0.03, "FogDensity survives save/open exactly")
        check(reopened_environment.get("Exposure") == -1.5, "Exposure survives save/open exactly")
        check(reopened_environment.get("ShadowDistance") == 25.0, "ShadowDistance survives save/open exactly")


# ============================================================
# Runtime application -- the right values reach the right objects
# ============================================================

def test_apply_environment_settings_writes_pipeline_fields() -> None:
    game = _FakeGame()
    try:
        game.apply_environment_settings({
            "FogEnabled": True, "FogColor": [100.0, 110.0, 120.0], "FogDensity": 0.05,
            "Exposure": 1.25, "ShadowsEnabled": True, "ShadowDistance": 30.0,
        })
        check(game.pbr_pipeline.enable_fog is True, "enable_fog reaches the Pipeline")
        check(game.pbr_pipeline.exposure == 1.25, "exposure reaches the Pipeline")
    finally:
        game.teardown()


def test_apply_environment_settings_is_a_no_op_without_a_pipeline() -> None:
    """SIMPLEPBR_AVAILABLE False / a harness that never called main() ->
    pbr_pipeline is None -- must not crash, shadows/fog-node handling
    (independent of the Pipeline) still applies."""
    game = _FakeGame(with_pipeline=False)
    try:
        game.apply_environment_settings({"FogEnabled": True, "ShadowsEnabled": True, "ShadowDistance": 20.0})
        check(True, "apply_environment_settings does not crash with pbr_pipeline=None")
        check(game.sun._shadows is True, "shadow state still applies independent of the Pipeline")
    finally:
        game.teardown()


def test_fog_enable_disable_toggles_the_render_fog_node() -> None:
    game = _FakeGame()
    try:
        check(not application.base.render.hasFog(), "no fog attached before enabling")
        game.apply_environment_settings({"FogEnabled": True, "FogColor": [50.0, 60.0, 70.0], "FogDensity": 0.02})
        check(application.base.render.hasFog(), "fog is attached to render once FogEnabled=True")
        # getFog() returns a fresh Python wrapper each call (Panda3D C++
        # binding semantics) -- `is` identity is the wrong check; compare
        # by name/value instead, both of which only match if the SAME
        # owned Fog object (game._environment_fog, mutated in place) is
        # what's actually attached, not a freshly constructed one.
        attached_fog = application.base.render.getFog()
        check(attached_fog == game._environment_fog, "the SAME owned Fog object is reused, not a new one each time")
        check(abs(attached_fog.get_exp_density() - 0.02) < 1e-9, "...with the density we just set")

        game.apply_environment_settings({"FogEnabled": False})
        check(not application.base.render.hasFog(), "fog is cleared from render once FogEnabled=False")
    finally:
        game.teardown()


def test_exposure_change_applies() -> None:
    game = _FakeGame()
    try:
        game.apply_environment_settings({"Exposure": -2.0})
        check(game.pbr_pipeline.exposure == -2.0, "a darker exposure value is applied")
        game.apply_environment_settings({"Exposure": 3.0})
        check(game.pbr_pipeline.exposure == 3.0, "a brighter exposure value is applied")
    finally:
        game.teardown()


def test_ambient_color_and_intensity_apply_to_the_real_ambient_light() -> None:
    game = _FakeGame()
    try:
        game.apply_environment_settings({"AmbientColor": [100.0, 50.0, 200.0], "AmbientIntensity": 1.0})
        applied = tuple(game.ambient_light.color)
        expected = (100.0 / 255.0, 50.0 / 255.0, 200.0 / 255.0)
        check(all(abs(a - e) < 0.01 for a, e in zip(applied[:3], expected)), f"AmbientColor at Intensity=1.0 reaches the real AmbientLight: got {applied[:3]}, expected {expected}")

        game.apply_environment_settings({"AmbientColor": [100.0, 50.0, 200.0], "AmbientIntensity": 0.0})
        applied_dark = tuple(game.ambient_light.color)
        check(all(abs(c) < 0.01 for c in applied_dark[:3]), f"AmbientIntensity=0.0 gives a genuinely dark (black) ambient contribution: got {applied_dark[:3]}")
    finally:
        game.teardown()


def test_ambient_persists_through_save_open_and_restores_after_lua_session() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        game.services = datamodel_schema.sanitize_services_snapshot({"Environment": {"AmbientColor": [10.0, 10.0, 10.0], "AmbientIntensity": 0.2}})
        game.apply_environment_settings(game.services["Environment"])
        dark_applied = tuple(game.ambient_light.color)

        ok, err = manager.scene.set_property("Environment", "AmbientIntensity", 2.0)
        check(ok, f"Environment.AmbientIntensity = 2.0 from Lua succeeds: {err}")
        bright_applied = tuple(game.ambient_light.color)
        check(bright_applied[0] > dark_applied[0], "the runtime Lua write actually brightened the live ambient light")
        check(game.services["Environment"]["AmbientIntensity"] == 0.2, "...but the persisted services dict is untouched (session-only, same contract as Fog/Exposure)")

        # Simulate Stop's restore step.
        game.apply_environment_settings(game.services.get("Environment", {}))
        restored = tuple(game.ambient_light.color)
        check(all(abs(a - b) < 0.001 for a, b in zip(restored, dark_applied)), "re-applying the persisted Environment (Stop's restore step) brings ambient back to the authored dark value")
    finally:
        teardown(game, manager, ctx)


def test_shadow_configuration_state() -> None:
    game = _FakeGame()
    try:
        game.apply_environment_settings({"ShadowsEnabled": True, "ShadowDistance": 15.0})
        check(game.sun._shadows is True, "ShadowsEnabled=True actually enables the light's shadow caster")
        lens = game.sun._light.get_lens()
        check(lens.get_near() == -15.0, "shadow lens near matches -ShadowDistance")
        check(lens.get_far() == 15.0, "shadow lens far matches ShadowDistance")
        film_size = lens.get_film_size()
        check(film_size.x == 30.0 and film_size.y == 30.0, "shadow lens film size matches 2x ShadowDistance")

        game.apply_environment_settings({"ShadowsEnabled": False, "ShadowDistance": 15.0})
        check(game.sun._shadows is False, "ShadowsEnabled=False disables the light's shadow caster")
    finally:
        game.teardown()


def test_repeated_identical_apply_does_not_retouch_shadow_caster() -> None:
    """Play -> Stop -> Play again with an unchanged Place must not
    repeatedly rebuild the DirectionalLight's shadow buffer. The
    underlying Panda3D set_shadow_caster() is a read-only bound C++
    method (can't be monkeypatched directly) -- instead this patches
    Ursina's own `DirectionalLight.shadows` PYTHON property (a plain
    property defined in ursina/lights.py), which is what
    apply_environment_settings() actually calls, and is what's guarded
    by its `if getattr(self.sun, "_shadows", None) != shadows_enabled`
    check."""
    game = _FakeGame()
    light_cls = type(game.sun)
    original_property = light_cls.shadows
    call_count = {"n": 0}

    def counting_setter(self, value):
        call_count["n"] += 1
        return original_property.fset(self, value)

    light_cls.shadows = property(original_property.fget, counting_setter)
    try:
        props = {"ShadowsEnabled": True, "ShadowDistance": 40.0, "FogEnabled": False, "Exposure": 0.0}
        game.apply_environment_settings(props)
        first_count = call_count["n"]
        check(first_count == 1, "the first apply touches the shadow caster exactly once")

        # Simulate Play -> Stop -> Play again with the SAME persisted
        # Environment properties (the realistic case: nothing changed).
        game.apply_environment_settings(dict(props))
        game.apply_environment_settings(dict(props))
        game.apply_environment_settings(dict(props))
        check(call_count["n"] == first_count, "repeated identical applies (Play/Stop/Play again) do not re-touch the shadow caster")
    finally:
        light_cls.shadows = original_property
        game.teardown()


def test_changed_shadow_distance_does_retouch_lens_but_not_necessarily_caster() -> None:
    """A genuine property change must still take effect -- the guard is
    against REDUNDANT applies, not against real ones."""
    game = _FakeGame()
    try:
        game.apply_environment_settings({"ShadowsEnabled": True, "ShadowDistance": 10.0})
        lens = game.sun._light.get_lens()
        check(lens.get_far() == 10.0, "initial ShadowDistance applied")

        game.apply_environment_settings({"ShadowsEnabled": True, "ShadowDistance": 60.0})
        lens = game.sun._light.get_lens()
        check(lens.get_far() == 60.0, "a genuinely changed ShadowDistance is re-applied")
    finally:
        game.teardown()


# ============================================================
# Runtime Lua writes: session-only, same contract as Workspace.Gravity
# ============================================================

def test_environment_runtime_write_applies_live_but_does_not_persist() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        game.apply_environment_settings(game.services.get("Environment", {}))
        check(game.pbr_pipeline.enable_fog is False, "starts with no fog, matching persisted defaults")

        ok, err = manager.scene.set_property("Environment", "FogEnabled", True)
        check(ok, f"Environment.FogEnabled = true from Lua succeeds: {err}")
        ok, err = manager.scene.set_property("Environment", "FogDensity", 0.08)
        check(ok, f"Environment.FogDensity = 0.08 from Lua succeeds: {err}")

        check(game.pbr_pipeline.enable_fog is True, "the runtime write applied live to the Pipeline")
        check(game.services.get("Environment", {}).get("FogEnabled") is False, "...but the PERSISTED services dict was never touched (session-only, like Workspace.Gravity)")

        kind, value = manager.scene.get_property("Environment", "FogEnabled")
        check(kind == "bool" and value is True, "reading Environment.FogEnabled from Lua reflects the live runtime overlay")
    finally:
        teardown(game, manager, ctx)


def test_environment_lua_write_rejects_bad_types() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        ok, err = manager.scene.set_property("Environment", "FogEnabled", "not a bool")
        check(not ok, "a non-bool FogEnabled write from Lua is rejected")
        ok, err = manager.scene.set_property("Environment", "Exposure", 999.0)
        check(not ok, "an out-of-range Exposure write from Lua is rejected")
    finally:
        teardown(game, manager, ctx)


# ============================================================
# Pipeline ownership does not multiply
# ============================================================

def test_simplepbr_init_is_called_exactly_once_in_source() -> None:
    """Static guard against a future accidental second simplepbr.init()
    call anywhere in client_studio.py -- the whole point of capturing and
    reusing self.pbr_pipeline is that init() runs exactly once per
    process; a second call would build a second, competing pipeline
    rather than reconfigure the first."""
    import inspect
    source = inspect.getsource(cs)
    # Excludes comment-only lines (this module's own docstrings/comments
    # legitimately mention "simplepbr.init()" by name while explaining
    # the single-call contract) -- counts only lines where the call
    # actually appears as code.
    real_call_lines = [
        line for line in source.splitlines()
        if "simplepbr.init(" in line and not line.strip().startswith("#")
    ]
    check(len(real_call_lines) == 1, f"simplepbr.init() is called exactly once in client_studio.py (found: {real_call_lines})")


test_environment_is_registered_as_a_root_service()
test_default_values_match_current_appearance()
test_environment_reachable_via_sanitize_services_snapshot()
test_property_validation_rejects_bad_values()
test_environment_persists_through_save_open_round_trip()
test_apply_environment_settings_writes_pipeline_fields()
test_apply_environment_settings_is_a_no_op_without_a_pipeline()
test_fog_enable_disable_toggles_the_render_fog_node()
test_exposure_change_applies()
test_ambient_color_and_intensity_apply_to_the_real_ambient_light()
test_ambient_persists_through_save_open_and_restores_after_lua_session()
test_shadow_configuration_state()
test_repeated_identical_apply_does_not_retouch_shadow_caster()
test_changed_shadow_distance_does_retouch_lens_but_not_necessarily_caster()
test_environment_runtime_write_applies_live_but_does_not_persist()
test_environment_lua_write_rejects_bad_types()
test_simplepbr_init_is_called_exactly_once_in_source()

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for message in FAILURES:
        print(f"  - {message}")
    sys.exit(1)
print("All Environment/atmosphere tests passed.")
