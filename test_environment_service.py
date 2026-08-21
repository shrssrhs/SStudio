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
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, '.')

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from ursina import AmbientLight, DirectionalLight, Ursina, Vec3

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


class _FakePostProcessQuad:
    """Stands in for simplepbr's real post-process NodePath -- records
    every set_shader_input() call by name, exactly like the existing
    _FakePipeline stubs the two Pipeline attributes it needs. No real
    shader compilation/GPU involved, matching this test file's own
    documented convention (a real Pipeline needs a live graphics
    context, which the objective behavior under test here does not)."""

    def __init__(self) -> None:
        self.inputs: dict[str, Any] = {}

    def set_shader_input(self, name: str, value: Any) -> None:
        self.inputs[name] = value


class _FakeSky:
    """Stands in for self.sky (a real Entity with the sky_gradient_shader
    in production) -- only set_shader_input() is exercised by
    apply_environment_settings(), so that's all this records, same
    pattern as _FakePostProcessQuad."""

    def __init__(self) -> None:
        self.inputs: dict[str, Any] = {}

    def set_shader_input(self, name: str, value: Any) -> None:
        self.inputs[name] = value


class _FakePipeline:
    """Stands in for simplepbr.Pipeline -- only the attributes
    apply_environment_settings() actually writes to."""

    def __init__(self) -> None:
        self.enable_fog = False
        self.exposure = 0.0
        self._post_process_quad = _FakePostProcessQuad()


class _FakeGame:
    """Extends the project's established minimal-harness pattern (see
    test_world_membership.py/test_mesh_part.py) with what THIS slice needs:
    a real DirectionalLight/Fog and a stub Pipeline, plus the real Lua
    machinery for the runtime-write session-only contract test."""

    apply_environment_settings = cs.MultiplayerGame.apply_environment_settings
    apply_runtime_service_write = cs.MultiplayerGame.apply_runtime_service_write
    _apply_postprocess_uniforms = cs.MultiplayerGame._apply_postprocess_uniforms
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
        self.sky = _FakeSky()
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

# ============================================================
# Stage 4.3B: post-processing (Contrast/Saturation/ColorTint/
# VignetteIntensity/GrainIntensity)
# ============================================================
# The real GLSL shader (compiled against a live GPU) is deliberately NOT
# exercised here -- same "a real Pipeline needs a live graphics context"
# reasoning as the rest of this file. What IS objectively testable
# headlessly, and is tested below: schema/defaults, validation/ranges,
# save/open, that _apply_postprocess_uniforms() pushes the RIGHT values
# to whatever quad it's given (via _FakePostProcessQuad), Lua read/write,
# and the session-only runtime-overlay/Stop-restore contract every other
# Environment property already has. The real shader's actual visual
# behavior (tex binding survives set_shader(), neutral vs authored is an
# obvious difference, grain is genuinely time-varying, FPS impact,
# resize survival) was verified with a real window+GPU diagnostic; see
# the Stage 4.3B completion report for that evidence -- it is
# intentionally not a permanent automated test, matching this project's
# established convention that pixel-perfect rendering facts live in a
# one-time audit, not a headless test file.

_POSTPROCESS_DEFAULTS = {
    "Contrast": 1.0,
    "Saturation": 1.0,
    "ColorTint": [255.0, 255.0, 255.0],
    "VignetteIntensity": 0.0,
    "GrainIntensity": 0.0,
}


def test_postprocess_schema_and_defaults() -> None:
    descriptor = datamodel_schema.get_class("Environment")
    by_name = {p.name: p for p in descriptor.properties}
    for name, expected_default in _POSTPROCESS_DEFAULTS.items():
        check(name in by_name, f"Environment declares {name}")
        check(by_name[name].category == "Post Processing", f"{name} is grouped under the 'Post Processing' Inspector category")
        check(by_name[name].default == expected_default, f"{name} default is {expected_default} (a true no-op)")
    defaults = datamodel_schema.default_properties("Environment")
    for name, expected_default in _POSTPROCESS_DEFAULTS.items():
        check(defaults.get(name) == expected_default, f"default_properties('Environment') includes {name}={expected_default}")


def test_postprocess_validation_rejects_out_of_range_values() -> None:
    cases = [
        ("Contrast", -0.5), ("Contrast", 3.5),
        ("Saturation", -1.0), ("Saturation", 10.0),
        ("VignetteIntensity", -0.1), ("VignetteIntensity", 1.5),
        ("GrainIntensity", -0.1), ("GrainIntensity", 2.0),
    ]
    for name, bad_value in cases:
        result = datamodel_schema.validate_property_value("Environment", name, bad_value)
        check(not result.ok, f"{name}={bad_value} is rejected as out of range")
    ok_contrast = datamodel_schema.validate_property_value("Environment", "Contrast", 2.0)
    check(ok_contrast.ok and ok_contrast.value == 2.0, "an in-range Contrast value is accepted")
    ok_tint = datamodel_schema.validate_property_value("Environment", "ColorTint", [200.0, 210.0, 255.0])
    check(ok_tint.ok, "a 3-component ColorTint is accepted")


def test_postprocess_persists_through_save_open() -> None:
    import tempfile
    import place_manager as pm

    with tempfile.TemporaryDirectory() as tmp_dir:
        path = tmp_dir + "\\postprocess_roundtrip.nebula.json"
        services = datamodel_schema.sanitize_services_snapshot({
            "Environment": {
                "Contrast": 1.3, "Saturation": 0.6, "ColorTint": [210.0, 220.0, 255.0],
                "VignetteIntensity": 0.4, "GrainIntensity": 0.2,
            }
        })
        manager = pm.PlaceManager()
        save_result = manager.save_as(path, [], services)
        check(save_result.success, f"saving custom Post Processing settings succeeds: {save_result.message}")

        reopened = pm.PlaceManager().open(path)
        check(reopened.success, f"reopening succeeds: {reopened.message}")
        env = (reopened.services or {}).get("Environment", {})
        check(env.get("Contrast") == 1.3, "Contrast survives save/open")
        check(env.get("Saturation") == 0.6, "Saturation survives save/open")
        check(env.get("ColorTint") == [210.0, 220.0, 255.0], "ColorTint survives save/open")
        check(env.get("VignetteIntensity") == 0.4, "VignetteIntensity survives save/open")
        check(env.get("GrainIntensity") == 0.2, "GrainIntensity survives save/open")


def test_apply_postprocess_uniforms_writes_correct_values_to_the_quad() -> None:
    game = _FakeGame()
    try:
        game.apply_environment_settings({
            "Contrast": 1.4, "Saturation": 0.5, "ColorTint": [200.0, 210.0, 255.0],
            "VignetteIntensity": 0.6, "GrainIntensity": 0.3,
        })
        inputs = game.pbr_pipeline._post_process_quad.inputs
        check(inputs.get("sstudio_contrast") == 1.4, "Contrast reaches the real quad's shader input")
        check(inputs.get("sstudio_saturation") == 0.5, "Saturation reaches the real quad's shader input")
        tint = inputs.get("sstudio_color_tint")
        check(tint is not None and abs(tint.x - 200.0/255.0) < 0.001 and abs(tint.y - 210.0/255.0) < 0.001 and abs(tint.z - 1.0) < 0.001, f"ColorTint is converted from 0-255 to a 0-1 Vec3, got {tint}")
        check(abs(inputs.get("sstudio_vignette_intensity", -1) - 0.6) < 0.001, "VignetteIntensity reaches the quad")
        check(abs(inputs.get("sstudio_grain_intensity", -1) - 0.3) < 0.001, "GrainIntensity reaches the quad")
    finally:
        game.teardown()


def test_apply_postprocess_uniforms_neutral_defaults_are_true_noop() -> None:
    game = _FakeGame()
    try:
        game.apply_environment_settings(dict(_POSTPROCESS_DEFAULTS, FogEnabled=False))
        inputs = game.pbr_pipeline._post_process_quad.inputs
        check(inputs.get("sstudio_contrast") == 1.0, "neutral Contrast is exactly 1.0")
        check(inputs.get("sstudio_saturation") == 1.0, "neutral Saturation is exactly 1.0")
        tint = inputs.get("sstudio_color_tint")
        check(tint is not None and tint.x == 1.0 and tint.y == 1.0 and tint.z == 1.0, "neutral ColorTint is exactly white (1,1,1), a true shader no-op")
        check(inputs.get("sstudio_vignette_intensity") == 0.0, "neutral VignetteIntensity is exactly 0.0 (disable path)")
        check(inputs.get("sstudio_grain_intensity") == 0.0, "neutral GrainIntensity is exactly 0.0 (disable path)")
    finally:
        game.teardown()


def test_apply_postprocess_uniforms_clamps_defensively() -> None:
    """Belt-and-suspenders clamp inside apply_environment_settings() itself
    (same precedent as AmbientIntensity's max(0.0, ...)) -- schema
    validation already rejects out-of-range values on every real entry
    path (Inspector/Lua/save-open), but this is a second, independent
    guard against a bad value ever reaching the live shader uniform."""
    game = _FakeGame()
    try:
        game.apply_environment_settings({"VignetteIntensity": 5.0, "GrainIntensity": -3.0, "Contrast": -1.0, "Saturation": -1.0})
        inputs = game.pbr_pipeline._post_process_quad.inputs
        check(inputs.get("sstudio_vignette_intensity") == 1.0, "an out-of-range VignetteIntensity is clamped to 1.0, not passed through raw")
        check(inputs.get("sstudio_grain_intensity") == 0.0, "an out-of-range negative GrainIntensity is clamped to 0.0")
        check(inputs.get("sstudio_contrast") == 0.0, "a negative Contrast is clamped to 0.0 (no negative-contrast inversion)")
        check(inputs.get("sstudio_saturation") == 0.0, "a negative Saturation is clamped to 0.0")
    finally:
        game.teardown()


def test_postprocess_runtime_lua_write_applies_live_but_does_not_persist() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        game.services = datamodel_schema.sanitize_services_snapshot({"Environment": {"Contrast": 1.0, "VignetteIntensity": 0.0}})
        game.apply_environment_settings(game.services["Environment"])
        neutral_vignette = game.pbr_pipeline._post_process_quad.inputs.get("sstudio_vignette_intensity")

        ok, err = manager.scene.set_property("Environment", "VignetteIntensity", 0.8)
        check(ok, f"Environment.VignetteIntensity = 0.8 from Lua succeeds: {err}")
        live_vignette = game.pbr_pipeline._post_process_quad.inputs.get("sstudio_vignette_intensity")
        check(live_vignette == 0.8, "the runtime Lua write reaches the live shader uniform immediately")
        check(game.services["Environment"]["VignetteIntensity"] == 0.0, "...but the persisted services dict is untouched (session-only, same contract as Fog/Exposure/Ambient)")

        # Simulate Stop's restore step.
        game.apply_environment_settings(game.services.get("Environment", {}))
        restored_vignette = game.pbr_pipeline._post_process_quad.inputs.get("sstudio_vignette_intensity")
        check(restored_vignette == neutral_vignette == 0.0, "re-applying the persisted Environment (Stop's restore step) brings VignetteIntensity back to the authored neutral value")
    finally:
        teardown(game, manager, ctx)


def test_postprocess_lua_write_rejects_bad_values() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        ok, err = manager.scene.set_property("Environment", "Contrast", 10.0)
        check(not ok, "an out-of-range Contrast write from Lua is rejected")
        ok, err = manager.scene.set_property("Environment", "GrainIntensity", "heavy")
        check(not ok, "a non-numeric GrainIntensity write from Lua is rejected")
    finally:
        teardown(game, manager, ctx)


def test_install_postprocess_shader_is_the_only_shader_install_call_site() -> None:
    """Stage 4.3B resource-lifecycle requirement: exactly one owned
    post-process pipeline for the whole process. _install_postprocess_shader()
    is called exactly once (from __init__, right after self.pbr_pipeline
    is set) -- Play/Stop (set_studio_playing()) only ever call
    apply_environment_settings() to update uniforms on the SAME quad,
    never rebuilding/reinstalling the shader. A source-level guard, not a
    real-GPU test, matching this file's existing
    test_simplepbr_init_is_called_exactly_once_in_source() precedent."""
    import inspect
    source = inspect.getsource(cs)
    call_lines = [
        line for line in source.splitlines()
        if line.strip() == "self._install_postprocess_shader()"
    ]
    check(len(call_lines) == 1, f"_install_postprocess_shader() is called exactly once in client_studio.py (found: {call_lines})")

    playing_source = inspect.getsource(cs.MultiplayerGame.set_studio_playing)
    check("_install_postprocess_shader" not in playing_source, "set_studio_playing() (Play/Stop) never re-installs the post-process shader -- only apply_environment_settings() (uniform updates) runs there")


# ============================================================
# Stage 4.3C: Sky & Outdoor (SkyTopColor/SkyHorizonColor/SkyBottomColor,
# SunColor/SunIntensity/SunRotation)
# ============================================================
# Same "no real GPU shader compilation here" scope as Post Processing
# above -- the sky's actual gradient rendering was verified with a real
# window+GPU diagnostic (see the Stage 4.3C completion report), not a
# permanent headless test. self.sun IS a real DirectionalLight in this
# harness (unlike the sky, which is stubbed), so its .color/.rotation are
# checked against the real Panda3D/Ursina object, matching this file's
# existing Ambient/Shadow test convention.

_SKY_SUN_DEFAULTS = {
    "SkyTopColor": [70.0, 145.0, 225.0],
    "SkyHorizonColor": [70.0, 145.0, 225.0],
    "SkyBottomColor": [70.0, 145.0, 225.0],
    "SunColor": [235.0, 225.0, 205.0],
    "SunIntensity": 1.0,
    "SunRotation": [35.264392, 135.0, -120.00001],
}


def test_sky_sun_schema_and_defaults() -> None:
    descriptor = datamodel_schema.get_class("Environment")
    by_name = {p.name: p for p in descriptor.properties}
    for name, expected_default in _SKY_SUN_DEFAULTS.items():
        check(name in by_name, f"Environment declares {name}")
        check(by_name[name].category == "Sky & Outdoor", f"{name} is grouped under the 'Sky & Outdoor' Inspector category")
        check(by_name[name].default == expected_default, f"{name} default is {expected_default}")
    defaults = datamodel_schema.default_properties("Environment")
    for name, expected_default in _SKY_SUN_DEFAULTS.items():
        check(defaults.get(name) == expected_default, f"default_properties('Environment') includes {name}={expected_default}")


def test_sky_sun_defaults_reproduce_the_legacy_hardcoded_appearance() -> None:
    """Backward compatibility, proven not assumed: an old Place with no
    authored Sky/Sun properties must look like the pre-4.3C hardcoded
    scene, not silently convert into a different sky/lighting look.
    SkyTopColor==SkyHorizonColor==SkyBottomColor at the old flat
    SKY_COLOR collapses the gradient shader to that single solid color
    (proven separately with a real-GPU capture, see the completion
    report); SunColor/SunIntensity=1.0 reproduces the exact old
    SUN_LIGHT_COLOR; SunRotation's default was verified (real Ursina
    DirectionalLight, both this session and here) to produce the
    bit-identical forward direction the old hardcoded
    look_at(Vec3(1,-1,-1)) call used to."""
    check(
        _SKY_SUN_DEFAULTS["SkyTopColor"] == _SKY_SUN_DEFAULTS["SkyHorizonColor"] == _SKY_SUN_DEFAULTS["SkyBottomColor"] == [70.0, 145.0, 225.0],
        "all 3 sky stops default to the exact old SKY_COLOR constant -- the gradient collapses to the old flat color",
    )
    check(_SKY_SUN_DEFAULTS["SunColor"] == [235.0, 225.0, 205.0] and _SKY_SUN_DEFAULTS["SunIntensity"] == 1.0, "SunColor*SunIntensity reproduces the exact old SUN_LIGHT_COLOR")

    sun = DirectionalLight(shadows=False)
    sun.look_at(Vec3(1, -1, -1))
    legacy_forward = tuple(round(v, 4) for v in sun.forward)

    sun2 = DirectionalLight(shadows=False)
    sun2.rotation = Vec3(*_SKY_SUN_DEFAULTS["SunRotation"])
    default_forward = tuple(round(v, 4) for v in sun2.forward)
    check(legacy_forward == default_forward, f"SunRotation's default reproduces the legacy look_at(Vec3(1,-1,-1)) direction exactly: {legacy_forward} == {default_forward}")


def test_sky_sun_validation_rejects_bad_values() -> None:
    cases = [
        ("SunIntensity", -1.0), ("SunIntensity", 10.0),
    ]
    for name, bad_value in cases:
        result = datamodel_schema.validate_property_value("Environment", name, bad_value)
        check(not result.ok, f"{name}={bad_value} is rejected as out of range")
    bad_rotation = datamodel_schema.validate_property_value("Environment", "SunRotation", [1.0, 2.0])
    check(not bad_rotation.ok, "a 2-component SunRotation is rejected (must be a 3-component vector)")
    ok_rotation = datamodel_schema.validate_property_value("Environment", "SunRotation", [10.0, 200.0, -45.0])
    check(ok_rotation.ok, "a valid 3-component SunRotation is accepted")


def test_sky_sun_persists_through_save_open() -> None:
    import tempfile
    import place_manager as pm

    with tempfile.TemporaryDirectory() as tmp_dir:
        path = tmp_dir + "\\sky_sun_roundtrip.nebula.json"
        services = datamodel_schema.sanitize_services_snapshot({
            "Environment": {
                "SkyTopColor": [10.0, 12.0, 30.0], "SkyHorizonColor": [40.0, 35.0, 55.0], "SkyBottomColor": [5.0, 5.0, 8.0],
                "SunColor": [140.0, 160.0, 220.0], "SunIntensity": 0.15, "SunRotation": [60.0, 90.0, 0.0],
            }
        })
        manager = pm.PlaceManager()
        save_result = manager.save_as(path, [], services)
        check(save_result.success, f"saving a night-scene Environment succeeds: {save_result.message}")

        reopened = pm.PlaceManager().open(path)
        check(reopened.success, f"reopening succeeds: {reopened.message}")
        env = (reopened.services or {}).get("Environment", {})
        check(env.get("SkyTopColor") == [10.0, 12.0, 30.0], "SkyTopColor survives save/open")
        check(env.get("SkyHorizonColor") == [40.0, 35.0, 55.0], "SkyHorizonColor survives save/open")
        check(env.get("SkyBottomColor") == [5.0, 5.0, 8.0], "SkyBottomColor survives save/open")
        check(env.get("SunColor") == [140.0, 160.0, 220.0], "SunColor survives save/open")
        check(env.get("SunIntensity") == 0.15, "SunIntensity survives save/open")
        check(env.get("SunRotation") == [60.0, 90.0, 0.0], "SunRotation survives save/open")


def test_apply_environment_settings_writes_sky_and_sun_state() -> None:
    game = _FakeGame()
    try:
        game.apply_environment_settings({
            "SkyTopColor": [10.0, 12.0, 30.0], "SkyHorizonColor": [40.0, 35.0, 55.0], "SkyBottomColor": [5.0, 5.0, 8.0],
            "SunColor": [140.0, 160.0, 220.0], "SunIntensity": 0.5, "SunRotation": [60.0, 90.0, 0.0],
        })
        sky_inputs = game.sky.inputs
        top = sky_inputs.get("sky_top_color")
        check(top is not None and abs(top.x - 10.0/255.0) < 0.001 and abs(top.y - 12.0/255.0) < 0.001 and abs(top.z - 30.0/255.0) < 0.001, f"SkyTopColor reaches the real sky shader input as a 0-1 Vec3, got {top}")
        horizon = sky_inputs.get("sky_horizon_color")
        check(horizon is not None and abs(horizon.x - 40.0/255.0) < 0.001, f"SkyHorizonColor reaches the shader input, got {horizon}")
        bottom = sky_inputs.get("sky_bottom_color")
        check(bottom is not None and abs(bottom.x - 5.0/255.0) < 0.001, f"SkyBottomColor reaches the shader input, got {bottom}")

        sun_color = tuple(game.sun.color)
        check(abs(sun_color[0] - 140.0/510.0) < 0.02, f"SunColor*SunIntensity(0.5) actually darkens the real DirectionalLight's color, got {sun_color}")
        check(tuple(round(v, 3) for v in game.sun.rotation) == (60.0, 90.0, 0.0), f"SunRotation reaches the real DirectionalLight's rotation, got {game.sun.rotation}")
    finally:
        game.teardown()


def test_sun_intensity_zero_genuinely_removes_light_contribution() -> None:
    """The night-scene requirement from the spec: SunIntensity=0.0 must be
    a REAL zero, not just "very dim" -- proven against the actual
    DirectionalLight color, not inferred."""
    game = _FakeGame()
    try:
        game.apply_environment_settings({"SunColor": [255.0, 255.0, 255.0], "SunIntensity": 0.0})
        sun_color = tuple(game.sun.color)
        check(sun_color[0] < 0.01 and sun_color[1] < 0.01 and sun_color[2] < 0.01, f"SunIntensity=0.0 genuinely zeroes the sun's real color, got {sun_color}")
    finally:
        game.teardown()


def test_sky_sun_runtime_lua_write_applies_live_but_does_not_persist() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        game.services = datamodel_schema.sanitize_services_snapshot({"Environment": {"SunIntensity": 1.0}})
        game.apply_environment_settings(game.services["Environment"])
        day_color = tuple(game.sun.color)

        ok, err = manager.scene.set_property("Environment", "SunIntensity", 0.0)
        check(ok, f"Environment.SunIntensity = 0.0 from Lua succeeds: {err}")
        night_color = tuple(game.sun.color)
        check(night_color[0] < day_color[0], "the runtime Lua write actually darkened the live sun")
        check(game.services["Environment"]["SunIntensity"] == 1.0, "...but the persisted services dict is untouched (session-only, same contract as everything else in Environment)")

        game.apply_environment_settings(game.services.get("Environment", {}))
        restored_color = tuple(game.sun.color)
        check(all(abs(a - b) < 0.01 for a, b in zip(restored_color, day_color)), "re-applying the persisted Environment (Stop's restore step) brings the sun back to the authored value")
    finally:
        teardown(game, manager, ctx)


def test_sky_sun_lua_write_rejects_bad_values() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        ok, err = manager.scene.set_property("Environment", "SunIntensity", -5.0)
        check(not ok, "an out-of-range SunIntensity write from Lua is rejected")
        ok, err = manager.scene.set_property("Environment", "SkyTopColor", "blue")
        check(not ok, "a non-list SkyTopColor write from Lua is rejected")
    finally:
        teardown(game, manager, ctx)


def test_create_world_is_called_exactly_once_in_source() -> None:
    """Resource-lifecycle requirement: self.sky/self.sun must not be
    recreated by Play/Stop or Place open/close -- create_world() (their
    only construction site) is called exactly once, from __init__.
    set_studio_playing() and load_world_snapshot() only ever reach
    apply_environment_settings() (mutating the SAME sky/sun objects),
    never create_world() again."""
    import inspect
    source = inspect.getsource(cs)
    call_lines = [line for line in source.splitlines() if line.strip() == "self.create_world()"]
    check(len(call_lines) == 1, f"create_world() is called exactly once in client_studio.py (found: {call_lines})")


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
test_postprocess_schema_and_defaults()
test_postprocess_validation_rejects_out_of_range_values()
test_postprocess_persists_through_save_open()
test_apply_postprocess_uniforms_writes_correct_values_to_the_quad()
test_apply_postprocess_uniforms_neutral_defaults_are_true_noop()
test_apply_postprocess_uniforms_clamps_defensively()
test_postprocess_runtime_lua_write_applies_live_but_does_not_persist()
test_postprocess_lua_write_rejects_bad_values()
test_install_postprocess_shader_is_the_only_shader_install_call_site()
test_sky_sun_schema_and_defaults()
test_sky_sun_defaults_reproduce_the_legacy_hardcoded_appearance()
test_sky_sun_validation_rejects_bad_values()
test_sky_sun_persists_through_save_open()
test_apply_environment_settings_writes_sky_and_sun_state()
test_sun_intensity_zero_genuinely_removes_light_contribution()
test_sky_sun_runtime_lua_write_applies_live_but_does_not_persist()
test_sky_sun_lua_write_rejects_bad_values()
test_create_world_is_called_exactly_once_in_source()
test_simplepbr_init_is_called_exactly_once_in_source()

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for message in FAILURES:
        print(f"  - {message}")
    sys.exit(1)
print("All Environment/atmosphere tests passed.")
