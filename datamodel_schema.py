"""
Stage 3.8: authoritative DataModel class/property metadata registry.

This is the ONE place that knows what properties a class has, what type
each property is, what its default/limits/read-only-ness are, and what
class a class inherits from -- the same responsibility Roblox's own
class/property reflection database has. Everything else (the Properties
Inspector, Place serialization, the Lua service-property bridge, server-
side validation) reads THIS registry instead of hardcoding per-class
branches, so a brand-new class (ProximityPrompt, eventually) only ever
needs one register_class() call, never new Inspector/serialization code.

Deliberately engine-agnostic: no Qt, no Ursina/Panda3D, no network I/O.
Pure metadata + validation, exactly like shared/object_registry.py (which
this module complements, not replaces -- see its module docstring for why
Part/Model/Script/etc keep using that registry for now; ClassDescriptor
entries for those classes here are thin compatibility stubs, not a full
re-migration, per the Stage 3.8 spec's explicit "incremental migration"
requirement).

Two kinds of class this registry actually drives end-to-end in this stage:
  - service classes (Workspace, StarterPlayer, ...) -- root services that
    were previously invisible to the Inspector entirely;
  - StarterPlayerScripts / the StarterCharacter special-Model role, whose
    Parent/singleton rules are validated through here.
Everything else registered here (Part, Model, Script, LocalScript,
ModuleScript, Instance) exists so inheritance/common-property lookups and
future Inspector/Lua code have ONE place to ask "what does this class
look like", without yet being the authoritative source of truth for those
classes' own specific fields (object_registry.py still is, for now).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# ============================================================
# VALUE TYPES
# ============================================================

# Deliberately a small closed set, not an open string -- every Inspector
# editor widget and every Lua wrap/unwrap path switches on one of these
# exact values (see studio_editor_live.py's _build_service_sections() and
# lua_runtime.py's service property bridge).
VALUE_TYPES = frozenset({
    "string", "bool", "int", "float", "enum", "vector3", "color3", "instance_ref",
})


@dataclass(frozen=True)
class EnumDescriptor:
    """A closed set of stable string values for an "enum" PropertyDescriptor
    -- e.g. StarterPlayer.CameraMode's ("Classic", "LockFirstPerson").
    Deliberately NOT the real Roblox Enum API (spec: "do not claim full
    Roblox Enum API compatibility") -- just a validated string set."""
    name: str
    values: tuple[str, ...]

    def is_valid(self, value: Any) -> bool:
        return isinstance(value, str) and value in self.values


@dataclass(frozen=True)
class PropertyDescriptor:
    """One property on one class. `category` groups properties in the
    Inspector (e.g. "Camera", "Character", "Behavior") -- see
    ClassDescriptor.properties_by_category()."""

    name: str
    value_type: str
    category: str = "Data"
    display_name: str = ""
    default: Any = None
    editable: bool = True
    serialized: bool = True
    runtime_visible: bool = True
    lua_readable: bool = True
    lua_writable: bool = True
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    enum: Optional[EnumDescriptor] = None
    validator: Optional[Callable[[Any], "ValidationResult"]] = None
    # Runtime-only properties (e.g. Workspace.CurrentCamera) resolve their
    # CURRENT value through this hook instead of the persistent/runtime
    # overlay dict -- given (game, service_name) -> "kind", value (same
    # (kind, value) tuple shape lua_runtime.py's bridge_get already uses).
    runtime_getter: Optional[Callable[[Any, str], tuple[str, Any]]] = None

    def __post_init__(self) -> None:
        if self.value_type not in VALUE_TYPES:
            raise ValueError(f"PropertyDescriptor({self.name!r}): unknown value_type {self.value_type!r}")
        if self.value_type == "enum" and self.enum is None:
            raise ValueError(f"PropertyDescriptor({self.name!r}): value_type='enum' requires enum=")
        if not self.display_name:
            object.__setattr__(self, "display_name", self.name)

    def label(self) -> str:
        return self.display_name


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    value: Any = None
    error: str = ""

    @staticmethod
    def accept(value: Any) -> "ValidationResult":
        return ValidationResult(True, value=value)

    @staticmethod
    def reject(error: str) -> "ValidationResult":
        return ValidationResult(False, error=error)


@dataclass(frozen=True)
class ClassDescriptor:
    """One ClassName's metadata. `properties` lists ONLY properties
    introduced by this class -- inherited ones come from base_class and
    are resolved by get_all_properties()/get_class(), never duplicated
    here (a subclass CAN override a base property by re-declaring the
    same name; the subclass's copy wins, see get_all_properties())."""

    class_name: str
    base_class: Optional[str] = None
    display_name: str = ""
    category: str = "Data"
    creatable: bool = True
    service: bool = False
    singleton: bool = False
    deletable: bool = True
    renameable: bool = True
    allowed_parent_classes: tuple[str, ...] = ()  # empty = no Parent restriction from THIS descriptor
    properties: tuple[PropertyDescriptor, ...] = ()
    # Legacy classes (Part/Model/Script/...) whose Inspector rendering
    # still goes through shared/object_registry.py's inspector_sections
    # path for now -- see module docstring. Never true for a class this
    # stage actually drives end-to-end (Workspace, StarterPlayer, ...).
    legacy_managed: bool = False

    def __post_init__(self) -> None:
        if not self.display_name:
            object.__setattr__(self, "display_name", self.class_name)
        seen: set[str] = set()
        for prop in self.properties:
            if prop.name in seen:
                raise ValueError(f"ClassDescriptor({self.class_name!r}): duplicate property {prop.name!r}")
            seen.add(prop.name)


# ============================================================
# REGISTRY
# ============================================================

class DuplicateClassError(ValueError):
    pass


class UnknownClassError(ValueError):
    pass


_REGISTRY: dict[str, ClassDescriptor] = {}


def register_class(descriptor: ClassDescriptor, *, replace: bool = False) -> None:
    if descriptor.base_class is not None and descriptor.base_class not in _REGISTRY and not replace:
        raise UnknownClassError(
            f"ClassDescriptor({descriptor.class_name!r}): base_class {descriptor.base_class!r} is not registered"
        )
    if not replace and descriptor.class_name in _REGISTRY:
        raise DuplicateClassError(f"Class '{descriptor.class_name}' is already registered")
    _REGISTRY[descriptor.class_name] = descriptor


def unregister_class(class_name: str) -> None:
    _REGISTRY.pop(class_name, None)


def get_class(class_name: str) -> Optional[ClassDescriptor]:
    return _REGISTRY.get(class_name)


def get_all_classes() -> list[ClassDescriptor]:
    return list(_REGISTRY.values())


def class_chain(class_name: str) -> list[ClassDescriptor]:
    """Base-first inheritance chain (Instance, ..., class_name itself).
    Empty list if class_name is unknown."""
    chain: list[ClassDescriptor] = []
    current: Optional[str] = class_name
    seen: set[str] = set()
    while current is not None and current not in seen:
        seen.add(current)
        descriptor = _REGISTRY.get(current)
        if descriptor is None:
            return []
        chain.append(descriptor)
        current = descriptor.base_class
    chain.reverse()
    return chain


def is_a(class_name: str, ancestor_class_name: str) -> bool:
    return any(d.class_name == ancestor_class_name for d in class_chain(class_name))


def get_all_properties(class_name: str) -> list[PropertyDescriptor]:
    """Stable, deterministic order: base class properties first (in their
    own declaration order), then each subclass's own new/overriding
    properties, in declaration order. A subclass re-declaring a base
    property's name REPLACES it in place (keeps the base's position in
    the ordering) rather than appending a duplicate -- this is what lets
    a future subclass narrow a base property's limits without disturbing
    Inspector category ordering."""
    ordered: list[PropertyDescriptor] = []
    index_by_name: dict[str, int] = {}
    for descriptor in class_chain(class_name):
        for prop in descriptor.properties:
            existing_index = index_by_name.get(prop.name)
            if existing_index is None:
                index_by_name[prop.name] = len(ordered)
                ordered.append(prop)
            else:
                ordered[existing_index] = prop
    return ordered


def get_property_descriptor(class_name: str, property_name: str) -> Optional[PropertyDescriptor]:
    for prop in get_all_properties(class_name):
        if prop.name == property_name:
            return prop
    return None


def properties_by_category(class_name: str) -> list[tuple[str, list[PropertyDescriptor]]]:
    """Stable category ordering: first-seen-wins, in the same base-first/
    declaration order get_all_properties() already guarantees."""
    order: list[str] = []
    buckets: dict[str, list[PropertyDescriptor]] = {}
    for prop in get_all_properties(class_name):
        if prop.category not in buckets:
            buckets[prop.category] = []
            order.append(prop.category)
        buckets[prop.category].append(prop)
    return [(category, buckets[category]) for category in order]


def default_properties(class_name: str) -> dict[str, Any]:
    return {prop.name: prop.default for prop in get_all_properties(class_name) if prop.serialized}


def is_parent_allowed(class_name: str, parent_class_name: Optional[str]) -> bool:
    """Walks the SUBCLASS's own inheritance chain looking for the nearest
    declared allowed_parent_classes (empty tuple = unrestricted at that
    level) -- a subclass may narrow but a base declaring no restriction
    imposes none unless a more specific descriptor in the chain does."""
    for descriptor in reversed(class_chain(class_name)):
        if descriptor.allowed_parent_classes:
            return parent_class_name in descriptor.allowed_parent_classes
    return True


def validate_property_value(class_name: str, property_name: str, raw_value: Any) -> ValidationResult:
    """Type/range/enum validation shared by the Inspector, Lua writes, and
    server-side sanitization -- ONE definition of "is this a legal value
    for this property" instead of three drifting copies."""
    prop = get_property_descriptor(class_name, property_name)
    if prop is None:
        return ValidationResult.reject(f"'{property_name}' is not a valid member of {class_name}")
    if not prop.editable:
        return ValidationResult.reject(f"'{property_name}' is read-only")

    if prop.value_type == "bool":
        if not isinstance(raw_value, bool):
            return ValidationResult.reject(f"{property_name} must be a boolean")
        value: Any = raw_value
    elif prop.value_type in ("int", "float"):
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            return ValidationResult.reject(f"{property_name} must be a number")
        numeric = float(raw_value)
        if not math.isfinite(numeric):
            return ValidationResult.reject(f"{property_name} must be a finite number")
        if prop.minimum is not None and numeric < prop.minimum:
            return ValidationResult.reject(f"{property_name} must be >= {prop.minimum}")
        if prop.maximum is not None and numeric > prop.maximum:
            return ValidationResult.reject(f"{property_name} must be <= {prop.maximum}")
        value = int(numeric) if prop.value_type == "int" else numeric
    elif prop.value_type == "string":
        if not isinstance(raw_value, str):
            return ValidationResult.reject(f"{property_name} must be a string")
        value = raw_value
    elif prop.value_type == "enum":
        assert prop.enum is not None
        if not prop.enum.is_valid(raw_value):
            return ValidationResult.reject(
                f"'{raw_value}' is not a valid value for {property_name} (expected one of {list(prop.enum.values)})"
            )
        value = raw_value
    elif prop.value_type == "vector3":
        if not (isinstance(raw_value, (list, tuple)) and len(raw_value) == 3):
            return ValidationResult.reject(f"{property_name} must be a 3-component vector")
        try:
            components = [float(v) for v in raw_value]
        except (TypeError, ValueError):
            return ValidationResult.reject(f"{property_name} components must be numbers")
        if not all(math.isfinite(v) for v in components):
            return ValidationResult.reject(f"{property_name} components must be finite")
        value = components
    elif prop.value_type == "color3":
        if not (isinstance(raw_value, (list, tuple)) and len(raw_value) == 3):
            return ValidationResult.reject(f"{property_name} must be a 3-component color")
        try:
            components = [float(v) for v in raw_value]
        except (TypeError, ValueError):
            return ValidationResult.reject(f"{property_name} components must be numbers")
        value = components
    elif prop.value_type == "instance_ref":
        value = raw_value
    else:  # pragma: no cover -- VALUE_TYPES already closes this off
        return ValidationResult.reject(f"unsupported value_type for {property_name}")

    if prop.validator is not None:
        return prop.validator(value)
    return ValidationResult.accept(value)


def sanitize_services_snapshot(raw: Any) -> dict[str, dict[str, Any]]:
    """A complete, valid `{service_name: {property: value}}` snapshot for
    EVERY registered service class -- missing services/properties fall
    back to descriptor defaults, malformed per-property input is dropped
    (never fatal), exactly like sanitize_persistent_properties() below.
    The one shared definition of "a valid services snapshot" used by both
    server.py (REPLACE_WORLD / UPDATE_SERVICE_PROPERTY) and
    place_manager.py (Place-file load, including transparently upgrading
    a version<3 Place that has no "services" key at all)."""
    source = raw if isinstance(raw, dict) else {}
    result: dict[str, dict[str, Any]] = {}
    for descriptor in get_all_classes():
        if not descriptor.service:
            continue
        merged = default_properties(descriptor.class_name)
        raw_service = source.get(descriptor.class_name)
        if isinstance(raw_service, dict):
            merged.update(sanitize_persistent_properties(descriptor.class_name, raw_service))
        result[descriptor.class_name] = merged
    return result


def sanitize_persistent_properties(class_name: str, raw: Any) -> dict[str, Any]:
    """Place-file / REPLACE_WORLD-shaped sanitizer: only keys that exist
    as serialized properties on class_name survive, each individually
    validated -- an invalid or unknown key is DROPPED (never raises),
    matching shared/object_registry.sanitize_properties_for_type()'s
    existing "malformed input is untrusted, not fatal" contract. Missing
    keys are simply absent here; callers apply default_properties() first
    and overlay this result on top (see place_manager.py / server.py)."""
    if not isinstance(raw, dict):
        return {}
    clean: dict[str, Any] = {}
    for prop in get_all_properties(class_name):
        if not prop.serialized or prop.name not in raw:
            continue
        result = validate_property_value(class_name, prop.name, raw[prop.name])
        if result.ok:
            clean[prop.name] = result.value
    return clean


# ============================================================
# BASE CLASS
# ============================================================

_ID_PROPERTY = PropertyDescriptor(
    "SStudioInstanceId", "string", category="Debug", display_name="Instance Id (SStudio)",
    default="", editable=False, serialized=False, lua_readable=False, lua_writable=False,
)

register_class(ClassDescriptor(
    class_name="Instance",
    base_class=None,
    display_name="Instance",
    category="Data",
    creatable=False,
    properties=(
        PropertyDescriptor("Name", "string", category="Data", default="Instance", serialized=False, lua_writable=True),
        PropertyDescriptor("ClassName", "string", category="Data", default="Instance", editable=False, serialized=False, lua_writable=False),
        PropertyDescriptor("Parent", "instance_ref", category="Data", default=None, editable=False, serialized=False, lua_writable=False),
        _ID_PROPERTY,
    ),
))


# ============================================================
# SERVICE / ROOT-SERVICE CLASSES
# ============================================================

def _register_service(class_name: str, *, extra_properties: tuple[PropertyDescriptor, ...] = ()) -> None:
    """Every root service (Workspace, ReplicatedStorage, ...) at minimum
    gets the common Instance properties -- this alone is what
    fixes "selecting a root service shows a blank Inspector" for services
    that have no functional custom properties yet (spec: "must not show a
    blank Inspector... do not add fake editable fields merely to imitate
    Roblox Studio")."""
    register_class(ClassDescriptor(
        class_name=class_name,
        base_class="Instance",
        display_name=class_name,
        category="Service",
        creatable=False,
        service=True,
        singleton=True,
        deletable=False,
        renameable=False,
        properties=extra_properties,
    ))


def _current_camera_getter(game: Any, _service_name: str) -> tuple[str, Any]:
    """Workspace.CurrentCamera runtime_getter -- resolves to the existing
    safe Camera Lua proxy id when a Play session's character/camera is
    active, nil otherwise. Never touches the raw Ursina camera/Panda
    NodePath/Qt object (spec: "never expose a raw engine handle")."""
    if getattr(game, "studio_playing", False):
        return "instance", "Camera"
    return "nil", None


register_class(ClassDescriptor(
    class_name="Workspace",
    base_class="Instance",
    display_name="Workspace",
    category="Service",
    creatable=False,
    service=True,
    singleton=True,
    deletable=False,
    renameable=False,
    properties=(
        PropertyDescriptor(
            "Gravity", "float", category="Behavior", default=24.0,
            minimum=0.0, maximum=None,
        ),
        PropertyDescriptor(
            "CurrentCamera", "instance_ref", category="Runtime", default=None,
            editable=False, serialized=False, lua_writable=False,
            runtime_getter=_current_camera_getter,
        ),
    ),
))

_CAMERA_MODE_ENUM = EnumDescriptor("CameraMode", ("Classic", "LockFirstPerson"))

register_class(ClassDescriptor(
    class_name="StarterPlayer",
    base_class="Instance",
    display_name="StarterPlayer",
    category="Service",
    creatable=False,
    service=True,
    singleton=True,
    deletable=False,
    renameable=False,
    properties=(
        PropertyDescriptor(
            "CameraMode", "enum", category="Camera", default="Classic", enum=_CAMERA_MODE_ENUM,
        ),
        PropertyDescriptor(
            "CameraMinZoomDistance", "float", category="Camera", default=2.0, minimum=0.01,
        ),
        PropertyDescriptor(
            "CameraMaxZoomDistance", "float", category="Camera", default=10.0, minimum=0.01,
        ),
        PropertyDescriptor(
            "CharacterWalkSpeed", "float", category="Character", default=6.0, minimum=0.0,
        ),
        PropertyDescriptor(
            "CharacterUseJumpPower", "bool", category="Character", default=True,
        ),
        PropertyDescriptor(
            "CharacterJumpPower", "float", category="Character", default=8.0, minimum=0.0,
        ),
        PropertyDescriptor(
            "CharacterJumpHeight", "float", category="Character", default=2.0, minimum=0.0,
        ),
        PropertyDescriptor(
            "CharacterMaxSlopeAngle", "float", category="Character", default=45.0, minimum=0.0, maximum=89.0,
        ),
    ),
))

# Stage 3.8: every OTHER shared.object_registry.ROOT_SERVICES entry gets
# the common-properties-only treatment (spec: "services without functional
# custom properties may show only the common read-only/service properties
# for now... must not show a blank Inspector"). Deliberately NOT
# "Lighting"/"Camera" -- confirmed via baseline inspection that neither is
# actually a member of ROOT_SERVICES in this codebase; both are ordinary
# offline-demo SceneObjects (object_type="Lighting"/"Camera", random
# per-instance ids, plain object_registry-managed properties), not root
# services at all, so registering them here would claim a root service
# that Explorer can never actually select.
for _name in ("Players", "StarterGui", "ReplicatedStorage", "ServerScriptService", "ServerStorage"):
    _register_service(_name)


def validate_starter_player_zoom(min_value: float, max_value: float) -> ValidationResult:
    """Cross-field rule Inspector edits and Lua writes both need: the pair
    must stay ordered. Called explicitly by callers editing either bound
    (see studio_editor_live.py/lua_runtime.py) -- not wired through
    PropertyDescriptor.validator since it needs the OTHER property's
    current value, not just the one being written."""
    if min_value > max_value:
        return ValidationResult.reject("CameraMinZoomDistance cannot exceed CameraMaxZoomDistance")
    return ValidationResult.accept((min_value, max_value))


# ============================================================
# STARTERPLAYERSCRIPTS / STARTERCHARACTER FOUNDATION
# ============================================================

# StarterPlayerScripts is a normal, serialized, deletable, renameable
# Instance -- NOT a root service (it lives INSIDE StarterPlayer, not
# beside it) -- see shared/object_registry.py for its actual
# ObjectTypeDefinition (Insert Object / CREATE / DELETE / REPARENT still
# go through that existing registry, per the spec's "reuse existing
# CREATE/DELETE/REPARENT machinery" guidance). Registered here too only
# so class_chain()/is_a() can answer questions about it uniformly with
# every other class this module knows about.
register_class(ClassDescriptor(
    class_name="StarterPlayerScripts",
    base_class="Instance",
    display_name="StarterPlayerScripts",
    category="Container",
    creatable=True,
    service=False,
    singleton=True,
    deletable=True,
    renameable=False,
    allowed_parent_classes=("StarterPlayer",),
    legacy_managed=True,
))


STARTER_CHARACTER_NAME = "StarterCharacter"


def is_starter_character(class_name: str, name: str, parent_key: Optional[str]) -> bool:
    """StarterCharacter is deliberately NOT its own ClassName (spec:
    "StarterCharacter is not a root service and should not be introduced
    as a fake service class... represent it as a special Model role") --
    this is the one predicate every authoritative check (server creation/
    rename/reparent validation, Explorer/Inspector display) uses to
    decide whether a given Model is playing that role. An ordinary Model
    with any other name, or a Model named "StarterCharacter" anywhere
    other than directly under StarterPlayer, is just an ordinary Model."""
    return class_name == "Model" and name == STARTER_CHARACTER_NAME and parent_key == "StarterPlayer"


# ============================================================
# LEGACY-COMPATIBLE CLASSES (thin stubs -- see module docstring)
# ============================================================

for _legacy_name, _legacy_base in (
    ("Model", "Instance"),
    ("Folder", "Instance"),
    ("Part", "Instance"),
    ("Script", "Instance"),
    ("LocalScript", "Instance"),
    ("ModuleScript", "Instance"),
    # Stage 3.9: SpawnPoint is genuinely Part-like (same has_3d_entity
    # transform/physics presence, see shared/object_registry.py) -- basing
    # it on "Part" instead of "Instance" here lets datamodel_schema.is_a()
    # answer IsA("Part")/IsA("SpawnPoint") correctly via real inheritance,
    # replacing the single hand-rolled special case that used to live in
    # lua_runtime.py's RuntimeSceneLayer.is_a() (see its Stage 3.9 update).
    ("SpawnPoint", "Part"),
    # Stage 4.1 (showcase sprint): MeshPart is genuinely Part-like (same
    # has_3d_entity transform/physics presence, see shared/object_registry
    # .py), differing only in visual representation (a loaded mesh instead
    # of a primitive cube) and one extra property (MeshId) -- basing it on
    # "Part" here for the same IsA("Part") reasoning as SpawnPoint above.
    ("MeshPart", "Part"),
    # Stage 3.9: gameplay-state primitives (see shared/object_registry.py
    # registration) -- registered here too, purely so IsA("Instance") and
    # class_chain() resolve correctly for them; their actual properties are
    # driven entirely by RuntimeSceneLayer's generic property_schema
    # fallback, not by anything in this module.
    ("BoolValue", "Instance"),
    ("IntValue", "Instance"),
    ("NumberValue", "Instance"),
    ("StringValue", "Instance"),
):
    register_class(ClassDescriptor(
        class_name=_legacy_name,
        base_class=_legacy_base,
        display_name=_legacy_name,
        category="Legacy",
        creatable=True,
        legacy_managed=True,
    ))
