"""Regression tests: normal SStudio startup must not create any legacy
PickADoor demo content (player.glb avatar, hardcoded ground/test blocks)
independently of the actual authoritative world/selected template.

Constructing a real MultiplayerGame requires a live Ursina/Panda3D graphics
context (it's an Entity subclass built inside main()'s Ursina() app), which
this project's existing test suite never spins up for client_studio.py.
Rather than open a real window from an automated test, these tests exercise
the same code paths at the level that's actually decidable without one:

- parse_arguments()/MultiplayerGame.__init__'s --legacy-demo default,
  checked via real argument parsing / real signature introspection (not
  guessed from reading the source).
- MultiplayerStudioAdapter._system_objects() called directly against a
  lightweight duck-typed fake `game` object (the method only reads
  game.legacy_demo/game.static_blocks/game.local_player.x/y/z -- see its
  __init__, which stores nothing but `self.game = game`), which exercises
  the REAL method body, not a description of it.
- place_manager.TemplateRepository against the real templates/*.nebula.json
  files on disk.

The Real Windows manual test pass (see the accompanying report) is what
confirms the full behavioral claim end-to-end: no player.glb log line, no
legacy geometry visible, Blank/Baseplate template content correct.

Follows this project's existing test convention: plain top-level-assertion
script, run directly, offscreen Qt platform.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, '.')

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

import client_studio as cs
import place_manager as pm

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"ok: {message}")


class _FakeVec3:
    def __init__(self, x: float, y: float, z: float) -> None:
        self.x, self.y, self.z = x, y, z


class _FakeGame:
    """Duck-typed stand-in for MultiplayerGame -- only the attributes
    MultiplayerStudioAdapter._system_objects() actually reads."""

    def __init__(self, legacy_demo: bool, static_blocks=None, instances=None) -> None:
        self.legacy_demo = legacy_demo
        self.static_blocks = static_blocks or []
        self.local_player = _FakeVec3(1.0, 2.0, 3.0)
        self.instances = instances or {}


# ============================================================
# --legacy-demo CLI flag
# ============================================================

def test_legacy_demo_flag_defaults_off() -> None:
    argv_backup = sys.argv
    try:
        sys.argv = ["client_studio.py"]
        arguments = cs.parse_arguments()
    finally:
        sys.argv = argv_backup
    check(arguments.legacy_demo is False, "--legacy-demo defaults to False (normal SStudio mode is the default)")


def test_legacy_demo_flag_can_be_enabled_explicitly() -> None:
    argv_backup = sys.argv
    try:
        sys.argv = ["client_studio.py", "--legacy-demo"]
        arguments = cs.parse_arguments()
    finally:
        sys.argv = argv_backup
    check(arguments.legacy_demo is True, "--legacy-demo can still be explicitly enabled for developers")


def test_multiplayer_game_legacy_demo_parameter_defaults_false() -> None:
    import inspect
    signature = inspect.signature(cs.MultiplayerGame.__init__)
    check("legacy_demo" in signature.parameters, "MultiplayerGame.__init__ accepts a legacy_demo parameter")
    check(signature.parameters["legacy_demo"].default is False, "MultiplayerGame.__init__'s legacy_demo defaults to False")


# ============================================================
# _system_objects(): no legacy geometry independent of WORLD_SNAPSHOT
# ============================================================

def test_system_objects_without_legacy_demo_has_no_legacy_geometry() -> None:
    """The actual regression: Camera/Lighting (reusable service nodes) stay,
    but Terrain/Baseplate/Static Geometry/StaticBlock_N (the old PickADoor
    demo's hardcoded ground + 5 test blocks) must not appear at all."""
    game = _FakeGame(legacy_demo=False, static_blocks=["would-be-ignored"])
    adapter = cs.MultiplayerStudioAdapter(game)
    objects = adapter._system_objects()
    names = {obj.name for obj in objects}

    check(names == {"Camera", "Lighting"}, f"normal mode: _system_objects() is exactly {{Camera, Lighting}}, got {names}")
    check(not any(n.startswith("StaticBlock_") for n in names), "no StaticBlock_N entries in normal mode")
    check("Terrain" not in names, "no legacy Terrain entry in normal mode")
    check("Baseplate" not in names, "no legacy (system) Baseplate entry in normal mode")
    check("Static Geometry" not in names, "no legacy Static Geometry folder in normal mode")


def test_system_objects_with_legacy_demo_still_available() -> None:
    """--legacy-demo is a real opt-in, not a removed feature -- the original
    content must still be reachable for developers who ask for it."""
    fake_block = _FakeVec3(1.0, 2.0, 3.0)
    fake_block.rotation_x = fake_block.rotation_y = fake_block.rotation_z = 0.0
    fake_block.scale_x = fake_block.scale_y = fake_block.scale_z = 1.0
    game = _FakeGame(legacy_demo=True, static_blocks=[fake_block])
    adapter = cs.MultiplayerStudioAdapter(game)
    objects = adapter._system_objects()
    names = {obj.name for obj in objects}

    check("Terrain" in names, "--legacy-demo: Terrain entry still available")
    check("Baseplate" in names, "--legacy-demo: Baseplate entry still available")
    check("Static Geometry" in names, "--legacy-demo: Static Geometry folder still available")
    check("StaticBlock_1" in names, "--legacy-demo: StaticBlock_N entries still available")


def test_empty_world_stays_visually_empty_in_normal_mode() -> None:
    """sync_full_scene()'s object list is _system_objects() + game.instances
    -- with an empty authoritative world (a real empty WORLD_SNAPSHOT) and no
    legacy geometry, nothing renderable (Part-shaped) should be left over."""
    game = _FakeGame(legacy_demo=False, instances={})
    adapter = cs.MultiplayerStudioAdapter(game)
    objects = adapter._system_objects()
    renderable = [obj for obj in objects if obj.object_type not in ("Camera", "Lighting")]
    check(renderable == [], "an empty world + normal mode produces zero renderable system objects")
    check(len(game.instances) == 0, "sanity: fake empty WORLD_SNAPSHOT has no instances")


# ============================================================
# player.glb / PlayerVisual construction gating (structural)
# ============================================================

def test_create_local_visual_gates_playervisual_behind_legacy_demo() -> None:
    """Structural safety-net check: create_local_visual() (the method that
    eagerly loads player.glb via PlayerVisual()) must construct it only
    inside an `if self.legacy_demo:` guard. This is a source-level check
    because constructing a real PlayerVisual requires a live Panda3D loader
    (builtins.loader) -- see the Real Windows test pass for the runtime
    proof (no player.glb log line on normal startup)."""
    import inspect
    source = inspect.getsource(cs.MultiplayerGame.create_local_visual)
    guard_index = source.find("if self.legacy_demo:")
    call_index = source.find("PlayerVisual(")
    check(guard_index != -1, "create_local_visual() contains an `if self.legacy_demo:` guard")
    check(call_index != -1, "create_local_visual() still constructs PlayerVisual somewhere")
    check(guard_index != -1 and call_index != -1 and guard_index < call_index, "the legacy_demo guard appears BEFORE the PlayerVisual(...) construction")


def test_create_world_gates_ground_and_blocks_behind_legacy_demo() -> None:
    import inspect
    source = inspect.getsource(cs.MultiplayerGame.create_world)
    guard_index = source.find("if self.legacy_demo:")
    ground_index = source.find('model="plane"')
    check(guard_index != -1, "create_world() contains an `if self.legacy_demo:` guard")
    check(ground_index != -1, "create_world() still constructs the ground plane somewhere (for --legacy-demo)")
    check(guard_index != -1 and ground_index != -1 and guard_index < ground_index, "the legacy_demo guard appears BEFORE the ground-plane construction")


# ============================================================
# Bundled templates: resolved from the app directory, never Documents,
# never falling back to legacy content on a missing resource
# ============================================================

def test_templates_dir_is_application_resource_directory() -> None:
    from pathlib import Path
    check(pm.TEMPLATES_DIR.parent == Path(pm.__file__).resolve().parent, "TEMPLATES_DIR is <app dir>/templates, resolved from place_manager.py's own location")
    projects_root = pm.default_projects_root().resolve()
    templates_dir = pm.TEMPLATES_DIR.resolve()
    check(
        projects_root != templates_dir and projects_root not in templates_dir.parents,
        "TEMPLATES_DIR is not the user's projects root (Documents/SStudio Projects) or a subdirectory of it",
    )
    check(pm.TEMPLATES_DIR.is_dir(), "TEMPLATES_DIR actually exists on disk")


def test_missing_template_raises_and_never_falls_back() -> None:
    repo = pm.TemplateRepository()
    try:
        repo.load_raw_objects("definitely_not_a_real_template_id")
        check(False, "load_raw_objects() on a missing template id raises instead of returning content")
    except FileNotFoundError:
        check(True, "load_raw_objects() on a missing template id raises FileNotFoundError")
    try:
        repo.instantiate("definitely_not_a_real_template_id")
        check(False, "instantiate() on a missing template id raises instead of silently falling back")
    except FileNotFoundError:
        check(True, "instantiate() on a missing template id raises FileNotFoundError (no legacy-map fallback)")


def test_blank_template_produces_no_visible_user_objects() -> None:
    repo = pm.TemplateRepository()
    objects = repo.instantiate("blank")
    check(objects == [], "Blank template instantiates to exactly zero objects")


def test_baseplate_template_produces_only_its_own_declared_content() -> None:
    repo = pm.TemplateRepository()
    objects = repo.instantiate("baseplate")
    check(len(objects) == 1, f"Baseplate template instantiates to exactly one object, got {len(objects)}")
    if objects:
        part = objects[0]
        check(part["class_name"] == "Part", "Baseplate's one object is a Part")
        check(part["properties"].get("Anchored") is True, "Baseplate Part is Anchored")
        check(part["properties"].get("CanCollide") is True, "Baseplate Part is CanCollide")
        check(list(part["properties"].get("Size", [])) == [128.0, 1.0, 128.0], f"Baseplate Part size is exactly 128x1x128, got {part['properties'].get('Size')}")


if __name__ == "__main__":
    test_legacy_demo_flag_defaults_off()
    test_legacy_demo_flag_can_be_enabled_explicitly()
    test_multiplayer_game_legacy_demo_parameter_defaults_false()
    test_system_objects_without_legacy_demo_has_no_legacy_geometry()
    test_system_objects_with_legacy_demo_still_available()
    test_empty_world_stays_visually_empty_in_normal_mode()
    test_create_local_visual_gates_playervisual_behind_legacy_demo()
    test_create_world_gates_ground_and_blocks_behind_legacy_demo()
    test_templates_dir_is_application_resource_directory()
    test_missing_template_raises_and_never_falls_back()
    test_blank_template_produces_no_visible_user_objects()
    test_baseplate_template_produces_only_its_own_declared_content()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for message in FAILURES:
            print(f"  - {message}")
        sys.exit(1)
    print("All legacy-content-removal regression tests passed.")
