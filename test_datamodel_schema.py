"""Stage 3.8: focused tests for datamodel_schema.py's class/property
metadata registry -- the reusable foundation the rest of Stage 3.8 (root
service Inspector rendering, Place serialization, Lua service property
access) is built on top of.

Pure Python, no Qt/Ursina/network dependency -- the registry module itself
has none, and these tests exercise it directly, plus a locally-registered
throwaway "future class" (never touching the production registry) proving
a brand-new class can gain Inspector fields, defaults, validation,
serialization, Parent rules, and Lua get/set without any class-specific
Inspector/serialization code -- see the module's own docstring.

Follows this project's existing test convention: plain top-level-assertion
script, run directly.
"""
import sys

sys.path.insert(0, '.')

import datamodel_schema as ds

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"ok: {message}")


# ============================================================
# REGISTRY BASICS
# ============================================================

def test_instance_is_registered_and_creatable_false() -> None:
    descriptor = ds.get_class("Instance")
    check(descriptor is not None, "Instance is registered")
    check(descriptor.creatable is False, "Instance is not directly creatable")


def test_duplicate_class_rejected() -> None:
    try:
        ds.register_class(ds.ClassDescriptor(class_name="Instance", base_class=None))
        check(False, "registering a duplicate class_name should raise")
    except ds.DuplicateClassError:
        check(True, "registering a duplicate class_name raises DuplicateClassError")


def test_unknown_base_class_rejected() -> None:
    try:
        ds.register_class(ds.ClassDescriptor(class_name="_NoSuchBaseTest", base_class="_DoesNotExist"))
        check(False, "registering a class with an unregistered base_class should raise")
    except ds.UnknownClassError:
        check(True, "registering a class with an unregistered base_class raises UnknownClassError")


def test_get_class_unknown_returns_none() -> None:
    check(ds.get_class("_TotallyUnknownClass") is None, "get_class() returns None for an unregistered class")


# ============================================================
# INHERITANCE / PROPERTY ORDERING
# ============================================================

def test_class_chain_base_first() -> None:
    chain = [d.class_name for d in ds.class_chain("Workspace")]
    check(chain == ["Instance", "Workspace"], f"class_chain('Workspace') is base-first: {chain}")


def test_is_a_reflects_inheritance() -> None:
    check(ds.is_a("Workspace", "Instance"), "Workspace IsA Instance")
    check(ds.is_a("Workspace", "Workspace"), "Workspace IsA Workspace (itself)")
    check(not ds.is_a("Workspace", "StarterPlayer"), "Workspace is NOT a StarterPlayer")


def test_workspace_inherits_common_instance_properties() -> None:
    names = [p.name for p in ds.get_all_properties("Workspace")]
    check("Name" in names, "Workspace inherits Name from Instance")
    check("ClassName" in names, "Workspace inherits ClassName from Instance")
    check("Parent" in names, "Workspace inherits Parent from Instance")
    check("Gravity" in names, "Workspace declares its own Gravity")
    check(names.index("Name") < names.index("Gravity"), "inherited (Instance) properties precede the subclass's own -- stable base-first ordering")


def test_property_ordering_is_stable_across_calls() -> None:
    first = [p.name for p in ds.get_all_properties("StarterPlayer")]
    second = [p.name for p in ds.get_all_properties("StarterPlayer")]
    check(first == second, "get_all_properties() returns the same order on repeated calls")


def test_properties_by_category_stable_order() -> None:
    categories = [category for category, _ in ds.properties_by_category("StarterPlayer")]
    check(categories == sorted(set(categories), key=categories.index), "properties_by_category() preserves first-seen category order with no duplicates")
    check("Camera" in categories and "Character" in categories, "StarterPlayer has both Camera and Character categories")


def test_subclass_can_override_base_property_in_place() -> None:
    """A subclass re-declaring a base property's name replaces it without
    disturbing ordering -- proven with a throwaway subclass so this
    doesn't depend on any production class actually doing this."""
    ds.register_class(ds.ClassDescriptor(
        class_name="_OverrideBaseTest",
        base_class="Instance",
        properties=(ds.PropertyDescriptor("Name", "string", default="Overridden"),),
    ))
    try:
        props = ds.get_all_properties("_OverrideBaseTest")
        names = [p.name for p in props]
        check(names.count("Name") == 1, "an overridden property appears exactly once, not duplicated")
        name_prop = next(p for p in props if p.name == "Name")
        check(name_prop.default == "Overridden", "the subclass's override wins over the base's original default")
    finally:
        ds.unregister_class("_OverrideBaseTest")


# ============================================================
# DEFAULTS
# ============================================================

def test_default_properties_workspace() -> None:
    defaults = ds.default_properties("Workspace")
    check(defaults.get("Gravity") == 24.0, f"Workspace.Gravity default is 24.0 (accepted current SStudio gravity magnitude), got {defaults.get('Gravity')}")
    check("CurrentCamera" not in defaults, "CurrentCamera is NOT serialized (runtime-only) -- absent from default_properties()")


def test_default_properties_starter_player() -> None:
    defaults = ds.default_properties("StarterPlayer")
    check(defaults.get("CameraMode") == "Classic", "StarterPlayer.CameraMode defaults to Classic")
    check(defaults.get("CharacterWalkSpeed") == 6.0, f"StarterPlayer.CharacterWalkSpeed default is 6.0 (accepted current controller walk speed), got {defaults.get('CharacterWalkSpeed')}")
    check(defaults.get("CameraMinZoomDistance") == 2.0 and defaults.get("CameraMaxZoomDistance") == 10.0, "StarterPlayer zoom defaults match the accepted 2-10 range")


# ============================================================
# VALIDATION
# ============================================================

def test_numeric_min_validation() -> None:
    result = ds.validate_property_value("Workspace", "Gravity", -1.0)
    check(not result.ok, "negative Gravity is rejected")
    result = ds.validate_property_value("Workspace", "Gravity", 0.0)
    check(result.ok and result.value == 0.0, "zero Gravity is accepted (zero gravity is a valid state)")


def test_numeric_max_validation() -> None:
    result = ds.validate_property_value("StarterPlayer", "CharacterMaxSlopeAngle", 200.0)
    check(not result.ok, "CharacterMaxSlopeAngle above its maximum (89) is rejected")


def test_enum_validation() -> None:
    result = ds.validate_property_value("StarterPlayer", "CameraMode", "LockFirstPerson")
    check(result.ok and result.value == "LockFirstPerson", "a valid enum value is accepted")
    result = ds.validate_property_value("StarterPlayer", "CameraMode", "NotARealMode")
    check(not result.ok, "an invalid enum value is rejected")
    result = ds.validate_property_value("StarterPlayer", "CameraMode", 5)
    check(not result.ok, "a non-string value for an enum property is rejected")


def test_bool_type_validation() -> None:
    result = ds.validate_property_value("StarterPlayer", "CharacterUseJumpPower", True)
    check(result.ok, "a real bool is accepted for a bool property")
    result = ds.validate_property_value("StarterPlayer", "CharacterUseJumpPower", 1)
    check(not result.ok, "an int is rejected for a bool property (1 is not True)")


def test_read_only_rejection() -> None:
    result = ds.validate_property_value("Workspace", "CurrentCamera", "Camera")
    check(not result.ok, "writing a read-only property (CurrentCamera) is rejected")
    result = ds.validate_property_value("Workspace", "ClassName", "SomethingElse")
    check(not result.ok, "writing ClassName (inherited read-only) is rejected")


def test_unknown_property_rejection() -> None:
    result = ds.validate_property_value("Workspace", "TotallyMadeUpProperty", 1.0)
    check(not result.ok, "writing an unregistered property name is rejected")


def test_finite_number_required() -> None:
    result = ds.validate_property_value("Workspace", "Gravity", float("nan"))
    check(not result.ok, "NaN is rejected for a numeric property")
    result = ds.validate_property_value("Workspace", "Gravity", float("inf"))
    check(not result.ok, "infinity is rejected for a numeric property")


# ============================================================
# PARENT / SINGLETON RULES
# ============================================================

def test_starter_player_scripts_parent_rule() -> None:
    check(ds.is_parent_allowed("StarterPlayerScripts", "StarterPlayer"), "StarterPlayerScripts is allowed under StarterPlayer")
    check(not ds.is_parent_allowed("StarterPlayerScripts", "Workspace"), "StarterPlayerScripts is NOT allowed under Workspace")


def test_is_starter_character_predicate() -> None:
    check(ds.is_starter_character("Model", "StarterCharacter", "StarterPlayer"), "a Model named StarterCharacter under StarterPlayer IS the special role")
    check(not ds.is_starter_character("Model", "StarterCharacter", "Workspace"), "a Model named StarterCharacter under Workspace is NOT the special role (wrong parent)")
    check(not ds.is_starter_character("Model", "MyModel", "StarterPlayer"), "a differently-named Model under StarterPlayer is an ordinary Model")
    check(not ds.is_starter_character("Folder", "StarterCharacter", "StarterPlayer"), "a Folder (not a Model) named StarterCharacter is not the special role")


def test_service_classes_are_singleton_and_undeletable() -> None:
    for name in ("Workspace", "StarterPlayer", "ReplicatedStorage", "ServerScriptService", "ServerStorage", "StarterGui", "Players"):
        descriptor = ds.get_class(name)
        check(descriptor is not None and descriptor.service, f"{name} is registered as a service class")
        check(descriptor.singleton, f"{name} is a singleton")
        check(not descriptor.deletable, f"{name} is not deletable")
        check(not descriptor.renameable, f"{name} is not renameable")


# ============================================================
# SERIALIZATION HELPERS
# ============================================================

def test_sanitize_persistent_properties_drops_unknown_and_invalid() -> None:
    clean = ds.sanitize_persistent_properties("Workspace", {"Gravity": 10.0, "Bogus": 1, "CurrentCamera": "x"})
    check(clean == {"Gravity": 10.0}, f"sanitize_persistent_properties() keeps only known, valid, serialized properties: {clean}")


def test_sanitize_persistent_properties_non_dict_input() -> None:
    check(ds.sanitize_persistent_properties("Workspace", None) == {}, "sanitize_persistent_properties(None) returns {} rather than raising")
    check(ds.sanitize_persistent_properties("Workspace", "not a dict") == {}, "sanitize_persistent_properties() on a non-dict returns {} rather than raising")


def test_sanitize_services_snapshot_complete_and_defaulted() -> None:
    snapshot = ds.sanitize_services_snapshot(None)
    check("Workspace" in snapshot and "StarterPlayer" in snapshot, "sanitize_services_snapshot(None) still produces every registered service")
    check(snapshot["Workspace"]["Gravity"] == 24.0, "missing service values fall back to descriptor defaults")


def test_sanitize_services_snapshot_merges_partial_input() -> None:
    snapshot = ds.sanitize_services_snapshot({"Workspace": {"Gravity": 5.0}})
    check(snapshot["Workspace"]["Gravity"] == 5.0, "a provided valid value overrides the default")
    check("StarterPlayer" in snapshot, "a service omitted from the input still gets a complete defaulted entry")


def test_sanitize_services_snapshot_drops_invalid_values() -> None:
    snapshot = ds.sanitize_services_snapshot({"Workspace": {"Gravity": -5.0}})
    check(snapshot["Workspace"]["Gravity"] == 24.0, "an invalid stored value (negative Gravity) falls back to the default instead of being accepted")


# ============================================================
# GENERIC "FUTURE CLASS" DESCRIPTOR (proves the registry is reusable
# without any Inspector/serialization/Lua code specific to this class)
# ============================================================

def test_future_class_gains_full_behavior_with_zero_special_case_code() -> None:
    enum = ds.EnumDescriptor("KeyCode", ("E", "F", "Space"))
    ds.register_class(ds.ClassDescriptor(
        class_name="_TestProximityPromptLike",
        base_class="Instance",
        display_name="ProximityPrompt (test)",
        category="Interaction",
        allowed_parent_classes=("Part",),
        properties=(
            ds.PropertyDescriptor("ActionText", "string", category="Data", default="Interact"),
            ds.PropertyDescriptor("ObjectText", "string", category="Data", default=""),
            ds.PropertyDescriptor("Enabled", "bool", category="Behavior", default=True),
            ds.PropertyDescriptor("KeyboardKeyCode", "enum", category="Behavior", default="E", enum=enum),
            ds.PropertyDescriptor("HoldDuration", "float", category="Behavior", default=1.0, minimum=0.0),
            ds.PropertyDescriptor("MaxActivationDistance", "float", category="Behavior", default=10.0, minimum=0.0),
            ds.PropertyDescriptor("RequiresLineOfSight", "bool", category="Behavior", default=True),
        ),
    ))
    try:
        # Inspector fields / stable ordering / categories -- exactly what
        # InspectorPanel._build_schema_sections() (studio_editor_live.py)
        # iterates for ANY registered class, service or not.
        props = ds.get_all_properties("_TestProximityPromptLike")
        check({p.name for p in props} >= {"ActionText", "Enabled", "KeyboardKeyCode", "HoldDuration"}, "future class exposes its declared Inspector fields via the same generic accessor every class uses")

        # Defaults
        check(ds.default_properties("_TestProximityPromptLike")["HoldDuration"] == 1.0, "future class has working defaults")

        # Validation (numeric + enum)
        check(not ds.validate_property_value("_TestProximityPromptLike", "HoldDuration", -1.0).ok, "future class numeric validation works")
        check(ds.validate_property_value("_TestProximityPromptLike", "KeyboardKeyCode", "F").ok, "future class enum validation accepts a valid value")
        check(not ds.validate_property_value("_TestProximityPromptLike", "KeyboardKeyCode", "Q").ok, "future class enum validation rejects an invalid value")

        # Parent rules
        check(ds.is_parent_allowed("_TestProximityPromptLike", "Part"), "future class Parent rule allows its declared parent")
        check(not ds.is_parent_allowed("_TestProximityPromptLike", "Workspace"), "future class Parent rule rejects an undeclared parent")

        # Serialization
        clean = ds.sanitize_persistent_properties("_TestProximityPromptLike", {"ActionText": "Open", "HoldDuration": 2.5, "Bogus": 1})
        check(clean == {"ActionText": "Open", "HoldDuration": 2.5}, "future class serializes/sanitizes through the same generic path")

        # "Lua get/set exposure" -- the SAME PropertyDescriptor.lua_readable/
        # lua_writable flags lua_runtime.py's service-property bridge reads
        # for Workspace/StarterPlayer; proven generically here rather than
        # requiring a live Lua VM for this specific test class.
        action_text_prop = ds.get_property_descriptor("_TestProximityPromptLike", "ActionText")
        check(action_text_prop.lua_readable and action_text_prop.lua_writable, "future class properties default to Lua-readable and Lua-writable")
    finally:
        ds.unregister_class("_TestProximityPromptLike")


if __name__ == "__main__":
    test_instance_is_registered_and_creatable_false()
    test_duplicate_class_rejected()
    test_unknown_base_class_rejected()
    test_get_class_unknown_returns_none()

    test_class_chain_base_first()
    test_is_a_reflects_inheritance()
    test_workspace_inherits_common_instance_properties()
    test_property_ordering_is_stable_across_calls()
    test_properties_by_category_stable_order()
    test_subclass_can_override_base_property_in_place()

    test_default_properties_workspace()
    test_default_properties_starter_player()

    test_numeric_min_validation()
    test_numeric_max_validation()
    test_enum_validation()
    test_bool_type_validation()
    test_read_only_rejection()
    test_unknown_property_rejection()
    test_finite_number_required()

    test_starter_player_scripts_parent_rule()
    test_is_starter_character_predicate()
    test_service_classes_are_singleton_and_undeletable()

    test_sanitize_persistent_properties_drops_unknown_and_invalid()
    test_sanitize_persistent_properties_non_dict_input()
    test_sanitize_services_snapshot_complete_and_defaulted()
    test_sanitize_services_snapshot_merges_partial_input()
    test_sanitize_services_snapshot_drops_invalid_values()

    test_future_class_gains_full_behavior_with_zero_special_case_code()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for message in FAILURES:
            print(f"  - {message}")
        sys.exit(1)
    print("All datamodel_schema tests passed.")
