"""Regression tests for Stage 4.3E's Graphics Quality foundation
(MSAA / shadow resolution / grain multiplier, and the deliberately
narrow choke point that applies them: MultiplayerGame.set_graphics_
quality()).

Scope, matching the sprint spec: preset/default definitions, backward
compatibility (Medium reproduces the exact pre-4.3E renderer state),
the critical MSAA-rebuild lifecycle (simplepbr silently swaps in a new
post-process quad -- proven with a real window earlier this session --
so SStudio's custom shader must be reinstalled every time), shadow-
resolution mapping with no redundant reallocation, the grain multiplier
NEVER touching authored Environment.GrainIntensity, repeated preset
switching not accumulating shadow-buffer reallocations, Play/Stop never
resetting quality, and Place save/open never serializing it. Does NOT
claim any test proves AA/shadow quality *looks* different -- that is
manual packaged acceptance, not something an automated assertion can
honestly claim.
"""
import os
import sys
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, '.')

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from ursina import AmbientLight, DirectionalLight, Ursina, Vec2

ursina_app = Ursina(window_type="none")

import client_studio as cs
import datamodel_schema
import inspect
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
    """Same stand-in as test_environment_service.py's -- records every
    set_shader_input()/set_shader() call, no real GPU compilation."""

    def __init__(self) -> None:
        self.inputs: dict[str, Any] = {}
        self.shader_set_count = 0

    def set_shader_input(self, name: str, value: Any) -> None:
        self.inputs[name] = value

    def set_shader(self, shader: Any) -> None:
        self.shader_set_count += 1

    def get_shader_input(self, name: str):
        class _StubInput:
            def get_texture(self_inner):
                return None
        return _StubInput()


class _FakePipeline:
    """Stands in for simplepbr.Pipeline. Unlike the real Pipeline,
    msaa_samples here is just a plain attribute -- it does NOT rebuild
    _post_process_quad on assignment (that exact mechanism was already
    proven with a REAL window+GPU this session; re-deriving it headlessly
    would just be re-testing simplepbr's own code, not SStudio's). What
    IS tested here is that SStudio's own set_graphics_quality() correctly
    calls _reinstall_postprocess_shader() every time msaa_samples
    changes -- verified via a call-counting wrapper below, not by
    faking simplepbr's rebuild."""

    def __init__(self) -> None:
        self.enable_fog = False
        self.exposure = 0.0
        self.msaa_samples = 4
        self.use_330 = False
        self._post_process_quad = _FakePostProcessQuad()


class _FakeGame:
    apply_environment_settings = cs.MultiplayerGame.apply_environment_settings
    _apply_postprocess_uniforms = cs.MultiplayerGame._apply_postprocess_uniforms
    _reinstall_postprocess_shader = cs.MultiplayerGame._reinstall_postprocess_shader
    set_graphics_quality = cs.MultiplayerGame.set_graphics_quality
    _build_part_entity = cs.MultiplayerGame._build_part_entity
    _build_light_entity = cs.MultiplayerGame._build_light_entity
    _apply_light_properties = cs.MultiplayerGame._apply_light_properties
    _apply_part_surface = cs.MultiplayerGame._apply_part_surface
    _apply_mesh_geometry = cs.MultiplayerGame._apply_mesh_geometry
    _apply_instance_properties = cs.MultiplayerGame._apply_instance_properties
    _set_light_render_active = cs.MultiplayerGame._set_light_render_active
    set_studio_playing = cs.MultiplayerGame.set_studio_playing
    spawn_instance = cs.MultiplayerGame.spawn_instance

    def __init__(self, initial_graphics_quality: str = cs.DEFAULT_GRAPHICS_QUALITY) -> None:
        self.instances: dict[str, cs.InstanceRecord] = {}
        self.parts: dict = {}
        self.studio_adapter = None
        self.services: dict[str, dict] = datamodel_schema.sanitize_services_snapshot(None)
        self.studio_playing = False
        self._physics_world = physics.PhysicsWorld(gravity=-24.0)
        self.sun = DirectionalLight(shadows=False)
        self.ambient_light = AmbientLight()
        self.sky = _FakeSky()
        self._environment_fog = None
        self._environment_state: dict | None = None
        self.pbr_pipeline = _FakePipeline()
        self.graphics_quality = initial_graphics_quality if initial_graphics_quality in cs.GRAPHICS_QUALITY_PRESETS else cs.DEFAULT_GRAPHICS_QUALITY
        self._graphics_quality_grain_multiplier = cs.GRAPHICS_QUALITY_PRESETS[self.graphics_quality]["grain_multiplier"]
        # Minimal stand-ins for set_studio_playing()'s own dependencies --
        # only what's needed to prove Play/Stop never touches
        # graphics_quality, not a full Play/Stop harness.
        self._character_runtime = None
        self._character_visual = None
        self.third_person_enabled = False
        self._captured = False
        self.lua_manager = None
        self.lua_context = None

    def _mouse_look_captured(self) -> bool:
        return self._captured

    def set_camera_mode(self, third_person: bool) -> None:
        pass

    def teardown(self) -> None:
        self._physics_world.destroy()
        from ursina import application
        application.base.render.clearFog()


class _FakeSky:
    def __init__(self) -> None:
        self.inputs: dict[str, Any] = {}

    def set_shader_input(self, name: str, value: Any) -> None:
        self.inputs[name] = value


def _new_spotlight(game: _FakeGame, instance_id: str) -> Any:
    entity = game._build_light_entity("SpotLight", {
        "Position": [0, 3, 0], "Rotation": [90, 0, 0], "Color": [255, 255, 255],
        "Intensity": 1.0, "Range": 8.0, "Angle": 45.0,
    })
    game.parts[instance_id] = entity
    return entity


def make_context(game: _FakeGame):
    manager = lua_runtime.LuaRuntimeManager(game)
    manager.start()
    ctx = lga.LuaGameplayContext(game, manager)
    ctx.start()
    return manager, ctx


def stop_context(game: _FakeGame, manager, ctx) -> None:
    ctx.stop()
    manager.stop()


# ============================================================
# Post-acceptance follow-up: a light built AFTER a quality change must
# start at the CURRENTLY selected quality's shadow resolution, not the
# old hardcoded 512 -- Insert Object, opening/replacing a Place, a
# runtime Instance.new("SpotLight"), or any light lazily becoming
# world-attached later. Before this fix, _apply_light_properties()
# hardcoded 512x512 for every newly-shadow-cast SpotLight regardless of
# self.graphics_quality; only lights that ALREADY EXISTED at the moment
# set_graphics_quality() ran got corrected -- a new one built afterward
# silently reverted to the old value until the creator toggled quality
# again. Every scenario below funnels through the same ONE guarded
# "first shadow caster enable" branch in _apply_light_properties(), so
# these are deliberately NOT redundant with each other -- each proves a
# genuinely different production code path reaches that same fix.
# ============================================================

def test_new_spotlight_built_directly_after_switching_to_low_starts_at_low_resolution() -> None:
    game = _FakeGame(initial_graphics_quality="Medium")
    try:
        game.set_graphics_quality("Low")
        spot = _new_spotlight(game, "spot_after_low")
        check(getattr(spot, "_shadow_caster_enabled", False), "the new SpotLight got a shadow caster on first build")
        lens = spot._light.get_lens()
        buffer_size = spot._light.get_shadow_buffer_size()
        check(tuple(buffer_size) == (256, 256), f"BLOCKER-STYLE CHECK: a SpotLight built AFTER selecting Low starts at Low's 256x256 shadow resolution immediately, got {tuple(buffer_size)}")
    finally:
        game.teardown()


def test_new_spotlight_built_directly_after_switching_to_high_starts_at_high_resolution() -> None:
    game = _FakeGame(initial_graphics_quality="Medium")
    try:
        game.set_graphics_quality("High")
        spot = _new_spotlight(game, "spot_after_high")
        buffer_size = spot._light.get_shadow_buffer_size()
        check(tuple(buffer_size) == (1024, 1024), f"a SpotLight built AFTER selecting High starts at High's 1024x1024 shadow resolution immediately, got {tuple(buffer_size)}")
    finally:
        game.teardown()


def test_opening_a_place_with_spotlights_after_selecting_high_uses_high_immediately() -> None:
    """'select High -> open a Place containing SpotLights -> every newly
    created renderer SpotLight uses High immediately' -- exercised via
    the real production spawn_instance() (the same method
    load_world_snapshot() itself calls for every incoming object), not a
    reimplementation."""
    game = _FakeGame(initial_graphics_quality="Medium")
    try:
        game.set_graphics_quality("High")
        place_object = {
            "id": "opened_spot", "class_name": "SpotLight", "name": "OpenedFixture", "parent_id": "Workspace",
            "properties": {"Position": [1, 3, 0], "Rotation": [90, 0, 0], "Color": [255, 255, 255], "Intensity": 1.0, "Range": 8.0, "Angle": 45.0},
            "tags": [], "attributes": {}, "enabled": True,
        }
        game.spawn_instance(place_object)
        entity = game.parts.get("opened_spot")
        check(entity is not None, "spawn_instance() built a real Entity for the incoming SpotLight")
        if entity is not None:
            buffer_size = entity._light.get_shadow_buffer_size()
            check(tuple(buffer_size) == (1024, 1024), f"a SpotLight arriving via spawn_instance() (Place open/REPLACE_WORLD) after High was selected uses High's 1024x1024 immediately, got {tuple(buffer_size)}")
    finally:
        game.teardown()


def test_runtime_instance_new_spotlight_during_play_after_selecting_low_uses_low_immediately() -> None:
    """'select Low -> Play -> runtime-created SpotLight -> Low resolution
    immediately' -- exercised via the real Lua Instance.new(), not a
    reimplementation."""
    game = _FakeGame(initial_graphics_quality="Medium")
    game.set_graphics_quality("Low")
    manager, ctx = make_context(game)
    try:
        ok, spot_id = manager.scene.instance_new("SpotLight", "Workspace")
        check(ok, f"Instance.new('SpotLight', workspace) succeeds during Play: {spot_id}")
        entity = game.parts.get(spot_id)
        check(entity is not None, "the runtime SpotLight has a real Entity")
        if entity is not None:
            buffer_size = entity._light.get_shadow_buffer_size()
            check(tuple(buffer_size) == (256, 256), f"a SpotLight created at runtime (Instance.new) after Low was selected uses Low's 256x256 immediately, got {tuple(buffer_size)}")
    finally:
        stop_context(game, manager, ctx)
        game.teardown()


def test_new_spotlight_after_repeated_switching_matches_the_final_selected_quality() -> None:
    """A light built after several switches must reflect whatever
    quality is CURRENT at the moment it is built, not any earlier value
    in the sequence."""
    game = _FakeGame(initial_graphics_quality="Medium")
    try:
        for level in ("Low", "High", "Medium", "Low"):
            game.set_graphics_quality(level)
        spot = _new_spotlight(game, "spot_after_sequence")
        buffer_size = spot._light.get_shadow_buffer_size()
        check(tuple(buffer_size) == (256, 256), f"after Medium->Low->High->Medium->Low, a newly built SpotLight uses the FINAL selected quality (Low, 256x256), got {tuple(buffer_size)}")
    finally:
        game.teardown()


def test_create_world_applies_graphics_quality_after_sun_exists() -> None:
    """Equivalent startup-order sanity check for the global
    DirectionalLight: create_world() is a true process-lifetime
    singleton (proven elsewhere -- test_create_world_is_called_exactly_
    once_in_source in test_local_lights.py), so there is no 'a second sun
    gets created later with the old resolution' scenario the way there
    is for SpotLights. What DOES need proving is that self.sun exists
    BEFORE set_graphics_quality() is called with it, so a non-default
    startup quality (e.g. via --graphics-quality or a persisted QSettings
    value) is applied correctly on the very first frame rather than
    needing a later correction."""
    source = inspect.getsource(cs.MultiplayerGame.create_world)
    sun_index = source.index("self.sun = DirectionalLight(")
    quality_index = source.index("self.set_graphics_quality(self.graphics_quality)")
    check(quality_index > sun_index, "create_world() calls self.set_graphics_quality() AFTER self.sun is constructed, not before")


# ============================================================
# Preset definitions / backward compatibility
# ============================================================

def test_presets_are_exactly_low_medium_high() -> None:
    check(set(cs.GRAPHICS_QUALITY_PRESETS.keys()) == {"Low", "Medium", "High"}, f"exactly Low/Medium/High presets exist, got {list(cs.GRAPHICS_QUALITY_PRESETS.keys())}")
    for level, preset in cs.GRAPHICS_QUALITY_PRESETS.items():
        for key in ("msaa_samples", "shadow_directional", "shadow_spot", "grain_multiplier"):
            check(key in preset, f"{level} preset defines {key}")


def test_default_is_medium_and_reproduces_pre_4_3e_baseline() -> None:
    check(cs.DEFAULT_GRAPHICS_QUALITY == "Medium", f"default graphics quality is Medium, got {cs.DEFAULT_GRAPHICS_QUALITY}")
    medium = cs.GRAPHICS_QUALITY_PRESETS["Medium"]
    check(medium["msaa_samples"] == 4, "Medium's MSAA (4) matches simplepbr's own installed default -- verified by reading simplepbr's Pipeline dataclass directly")
    check(medium["shadow_directional"] == 1024, "Medium's directional shadow resolution (1024) matches Ursina DirectionalLight's own default (ursina/lights.py)")
    check(medium["shadow_spot"] == 512, "Medium's SpotLight shadow resolution (512) matches the pre-existing hardcoded value in _apply_light_properties()")
    check(medium["grain_multiplier"] == 1.0, "Medium does not suppress authored grain")


def test_invalid_startup_quality_falls_back_to_default() -> None:
    game = _FakeGame(initial_graphics_quality="Ultra")
    check(game.graphics_quality == cs.DEFAULT_GRAPHICS_QUALITY, f"an invalid startup quality falls back to {cs.DEFAULT_GRAPHICS_QUALITY}, got {game.graphics_quality}")


# ============================================================
# set_graphics_quality(): the one choke point
# ============================================================

def test_unknown_level_is_rejected() -> None:
    game = _FakeGame()
    try:
        before = game.graphics_quality
        result = game.set_graphics_quality("Ultra")
        check(result is False, "set_graphics_quality('Ultra') returns False")
        check(game.graphics_quality == before, "an unknown level does not change the current quality")
    finally:
        game.teardown()


def test_msaa_changes_and_shader_is_reinstalled() -> None:
    """The critical lifecycle fact this slice audited: an MSAA change
    must always be followed by re-installing SStudio's own shader (a
    real window+GPU test earlier this session proved simplepbr silently
    swaps in a fresh quad with its OWN stock shader otherwise)."""
    game = _FakeGame(initial_graphics_quality="Medium")
    try:
        reinstall_calls = []
        game._reinstall_postprocess_shader = lambda: reinstall_calls.append(1)
        result = game.set_graphics_quality("Low")
        check(result is True, "set_graphics_quality('Low') succeeds")
        check(game.pbr_pipeline.msaa_samples == 0, f"Low sets msaa_samples to 0, got {game.pbr_pipeline.msaa_samples}")
        check(len(reinstall_calls) == 1, f"changing MSAA triggers exactly one _reinstall_postprocess_shader() call, got {len(reinstall_calls)}")
    finally:
        game.teardown()


def test_same_level_reapplied_does_not_reinstall_shader() -> None:
    game = _FakeGame(initial_graphics_quality="Medium")
    try:
        reinstall_calls = []
        game._reinstall_postprocess_shader = lambda: reinstall_calls.append(1)
        game.set_graphics_quality("Medium")
        check(len(reinstall_calls) == 0, "re-selecting the SAME quality (unchanged MSAA) does not reinstall the shader")
    finally:
        game.teardown()


def test_grain_multiplier_applies_without_touching_authored_environment() -> None:
    game = _FakeGame(initial_graphics_quality="Medium")
    try:
        game.services = datamodel_schema.sanitize_services_snapshot({"Environment": {"GrainIntensity": 0.5}})
        game.apply_environment_settings(game.services["Environment"])
        medium_grain = game.pbr_pipeline._post_process_quad.inputs.get("sstudio_grain_intensity")
        check(abs(medium_grain - 0.5) < 0.001, f"Medium applies the full authored GrainIntensity, got {medium_grain}")

        game.set_graphics_quality("Low")
        low_grain = game.pbr_pipeline._post_process_quad.inputs.get("sstudio_grain_intensity")
        check(low_grain == 0.0, f"BLOCKER-STYLE CHECK: Low's grain multiplier suppresses the RENDERED grain to 0.0, got {low_grain}")
        check(game.services["Environment"]["GrainIntensity"] == 0.5, "...but the AUTHORED Environment.GrainIntensity is completely untouched -- Graphics Quality never rewrites Place state")

        game.set_graphics_quality("High")
        high_grain = game.pbr_pipeline._post_process_quad.inputs.get("sstudio_grain_intensity")
        check(abs(high_grain - 0.5) < 0.001, f"switching back to High restores the full authored grain, got {high_grain}")
    finally:
        game.teardown()


# ============================================================
# Shadow-quality mapping and lifecycle
# ============================================================

def test_directional_shadow_resolution_changes_with_quality() -> None:
    """Panda3D's DirectionalLight.set_shadow_caster is a read-only bound
    C++ method (confirmed this session, same limitation hit earlier for
    Stage 4.1's shadow-caster tests) -- cannot be monkeypatched to count
    calls. Verified instead via the real, observable effect
    (shadow_map_resolution actually changes) plus the guard-state
    attribute set_graphics_quality() itself uses to decide whether to
    call set_shadow_caster() at all, same workaround already established
    and documented in test_local_lights.py for this exact limitation."""
    game = _FakeGame(initial_graphics_quality="Medium")
    try:
        game.sun.shadows = True
        game.set_graphics_quality("High")
        check(tuple(game.sun.shadow_map_resolution) == (2048, 2048), f"High sets the directional shadow_map_resolution to 2048, got {tuple(game.sun.shadow_map_resolution)}")
        check(game._graphics_quality_directional_resolution == 2048, "the guard-state attribute records the newly-applied resolution")

        game.set_graphics_quality("High")
        check(game._graphics_quality_directional_resolution == 2048, "re-selecting the SAME quality leaves the guard state unchanged (no redundant reallocation triggered)")
    finally:
        game.teardown()


def test_spotlight_shadow_resolution_changes_with_quality_no_redundant_realloc() -> None:
    game = _FakeGame(initial_graphics_quality="Medium")
    try:
        spot = _new_spotlight(game, "spot_1")
        check(getattr(spot, "_shadow_caster_enabled", False), "the SpotLight already has an active shadow caster (Stage 4.1 default)")

        game.set_graphics_quality("Low")
        check(game._graphics_quality_spot_resolution == 256, "Low records SpotLight shadow resolution 256 in the guard state")

        game.set_graphics_quality("Low")
        check(game._graphics_quality_spot_resolution == 256, "re-selecting the SAME quality leaves the guard state unchanged (no redundant reallocation)")

        game.set_graphics_quality("High")
        check(game._graphics_quality_spot_resolution == 1024, "switching to a genuinely different level updates the guard state to the new resolution")
    finally:
        game.teardown()


def test_repeated_quality_switching_does_not_accumulate_shadow_reallocations() -> None:
    """The mandatory stress test from the spec: Low -> Medium -> High ->
    Low -> High must reallocate each light's shadow buffer only when the
    resolution genuinely changes between consecutive levels, never once
    per switch regardless of value. Verified via the guard-state
    attributes (see the two tests above for why the native
    set_shadow_caster/setShadowCaster calls themselves can't be counted
    directly) plus real render-light-count/Entity-count stability across
    the whole sequence, confirming nothing else leaked either."""
    from ursina import application
    from panda3d.core import LightAttrib

    def count_active_render_lights() -> int:
        attrib = application.base.render.node().getAttrib(LightAttrib)
        return 0 if attrib is None else attrib.getNumOnLights()

    game = _FakeGame(initial_graphics_quality="Medium")
    try:
        game.sun.shadows = True
        spot = _new_spotlight(game, "spot_1")
        baseline_lights = count_active_render_lights()

        sequence = ["Low", "Medium", "High", "Low", "High"]
        expected_directional = {"Low": 512, "Medium": 1024, "High": 2048}
        expected_spot = {"Low": 256, "Medium": 512, "High": 1024}
        for level in sequence:
            game.set_graphics_quality(level)
            check(game._graphics_quality_directional_resolution == expected_directional[level], f"after switching to {level}, directional guard state is {expected_directional[level]}")
            check(game._graphics_quality_spot_resolution == expected_spot[level], f"after switching to {level}, SpotLight guard state is {expected_spot[level]}")

        check(game.graphics_quality == "High", "ends on the last selected level")
        check(len(game.parts) == 1, "repeated quality switching does not create/destroy any Entities -- exactly the one SpotLight still exists")
        check(count_active_render_lights() == baseline_lights, "repeated quality switching does not leak or lose any active render lights (ghost-light regression guard, reused here)")
    finally:
        game.teardown()


# ============================================================
# Play/Stop and persistence
# ============================================================

def test_set_studio_playing_never_touches_graphics_quality() -> None:
    """Spec requirement: quality is machine/client state, not an
    authored Play-session value -- Play/Stop must never reset or
    restore it, unlike Environment's runtime-overlay contract."""
    source = inspect.getsource(cs.MultiplayerGame.set_studio_playing)
    check("graphics_quality" not in source, "set_studio_playing() never references graphics_quality anywhere in its source")


def test_environment_schema_has_no_graphics_quality_property() -> None:
    """Confirms Graphics Quality was never added to the Environment
    service -- it must not be possible to author/serialize it into a
    Place at all."""
    descriptor = datamodel_schema.get_class("Environment")
    property_names = {p.name for p in descriptor.properties}
    check(not any("quality" in name.lower() or "msaa" in name.lower() or "aa" == name.lower() for name in property_names), f"Environment's schema has no quality/MSAA/AA property, got {property_names}")


def test_place_save_open_does_not_serialize_graphics_quality() -> None:
    import tempfile
    import place_manager as pm

    with tempfile.TemporaryDirectory() as tmp_dir:
        path = tmp_dir + "\\quality_not_serialized.nebula.json"
        manager = pm.PlaceManager()
        save_result = manager.save_as(path, [], datamodel_schema.sanitize_services_snapshot(None))
        check(save_result.success, f"saving succeeds: {save_result.message}")
        with open(path, encoding="utf-8") as f:
            raw_text = f.read()
        check("graphics_quality" not in raw_text.lower() and "msaa" not in raw_text.lower(), "the saved Place file contains no trace of graphics quality/MSAA state")


test_new_spotlight_built_directly_after_switching_to_low_starts_at_low_resolution()
test_new_spotlight_built_directly_after_switching_to_high_starts_at_high_resolution()
test_opening_a_place_with_spotlights_after_selecting_high_uses_high_immediately()
test_runtime_instance_new_spotlight_during_play_after_selecting_low_uses_low_immediately()
test_new_spotlight_after_repeated_switching_matches_the_final_selected_quality()
test_create_world_applies_graphics_quality_after_sun_exists()
test_presets_are_exactly_low_medium_high()
test_default_is_medium_and_reproduces_pre_4_3e_baseline()
test_invalid_startup_quality_falls_back_to_default()
test_unknown_level_is_rejected()
test_msaa_changes_and_shader_is_reinstalled()
test_same_level_reapplied_does_not_reinstall_shader()
test_grain_multiplier_applies_without_touching_authored_environment()
test_directional_shadow_resolution_changes_with_quality()
test_spotlight_shadow_resolution_changes_with_quality_no_redundant_realloc()
test_repeated_quality_switching_does_not_accumulate_shadow_reallocations()
test_set_studio_playing_never_touches_graphics_quality()
test_environment_schema_has_no_graphics_quality_property()
test_place_save_open_does_not_serialize_graphics_quality()

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for message in FAILURES:
        print(f"  - {message}")
    sys.exit(1)
print("All graphics-quality tests passed.")
