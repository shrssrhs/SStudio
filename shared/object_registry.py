"""
Централизованный реестр типов объектов — аналог того, как в Roblox Studio
устроен список ClassName-ов, доступных в Insert Object.

Модуль не знает ни про Qt, ни про Ursina/Panda3D, ни про сеть — это чистые
метаданные + генерация default-свойств + generic-валидатор properties для
типов, которые не Part-образны. Именно поэтому он лежит в shared/: сервер
использует его для валидации create_part/update_property, Studio-клиент —
для окна Insert Object, Explorer и Inspector.

Зависимость только в одну сторону: object_registry -> instance
(используются is_valid_vector3/is_valid_color). instance.py про реестр
ничего не знает.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from shared import instance as instance_module


# ============================================================
# КОРНЕВЫЕ СЕРВИСЫ
# ============================================================

# Псевдо-контейнеры верхнего уровня Explorer — не настоящие Instance (нет
# id, не живут в мире сервера), но допустимые значения parent_id/parent у
# объектов, которые лежат прямо в них. Совпадает с DataModel-сервисами
# Roblox (Workspace, Players, ServerScriptService, ...).
ROOT_SERVICES: tuple[str, ...] = (
    "Workspace",
    "Players",
    "StarterPlayer",
    "StarterGui",
    "ReplicatedStorage",
    "ServerScriptService",
    "ServerStorage",
    # Stage 4.1 (atmosphere slice): deliberately separate from Workspace --
    # Workspace owns world-membership/physics (Gravity, CurrentCamera);
    # Environment owns visual/rendering configuration (fog, exposure,
    # shadows) that has nothing to do with simulation. Kept as its own
    # small root service rather than growing Workspace into two unrelated
    # responsibilities, and rather than cloning Roblox's own Lighting
    # service wholesale -- see datamodel_schema.py's registration for the
    # actual (small, SStudio-specific) property set.
    "Environment",
)


# ============================================================
# ОПИСАНИЕ СВОЙСТВА (для generic-санитайзера non-Part типов)
# ============================================================

@dataclass(frozen=True)
class PropertySpec:
    kind: str  # "vector3" | "size3" | "color" | "bool" | "float" | "float01" | "int" | "string"
    max_len: int = 64  # используется только для kind == "string"


# ============================================================
# ОПИСАНИЕ ТИПА ОБЪЕКТА
# ============================================================

@dataclass(frozen=True)
class ObjectTypeDefinition:
    type_id: str
    display_name: str
    category: str
    description: str = ""
    icon: str = "cube"
    default_parent: str = "Workspace"
    allowed_parent_types: tuple[str, ...] = ()  # пусто = разрешено везде
    creatable: bool = True
    keywords: tuple[str, ...] = ()
    default_properties: dict[str, Any] = field(default_factory=dict)
    property_schema: dict[str, PropertySpec] = field(default_factory=dict)
    has_3d_entity: bool = False
    editor_only: bool = False
    is_container: bool = False
    # Секции, которые Inspector должен построить для этого типа —
    # см. InspectorPanel в studio_editor_live.py. Порядок значим.
    inspector_sections: tuple[str, ...] = ()


# ============================================================
# РЕЕСТР
# ============================================================

_REGISTRY: dict[str, ObjectTypeDefinition] = {}


def register_object_type(definition: ObjectTypeDefinition, *, replace: bool = False) -> None:
    if not replace and definition.type_id in _REGISTRY:
        raise ValueError(f"Тип объекта '{definition.type_id}' уже зарегистрирован")
    _REGISTRY[definition.type_id] = definition


def unregister_object_type(type_id: str) -> None:
    _REGISTRY.pop(type_id, None)


def get_object_type(type_id: str) -> ObjectTypeDefinition | None:
    return _REGISTRY.get(type_id)


def get_all_object_types() -> list[ObjectTypeDefinition]:
    return list(_REGISTRY.values())


def get_types_by_category() -> dict[str, list[ObjectTypeDefinition]]:
    buckets: dict[str, list[ObjectTypeDefinition]] = {}
    for definition in _REGISTRY.values():
        buckets.setdefault(definition.category, []).append(definition)
    for items in buckets.values():
        items.sort(key=lambda d: d.display_name)
    return buckets


def search_object_types(query: str) -> list[ObjectTypeDefinition]:
    normalized = query.strip().lower()
    if not normalized:
        return sorted(_REGISTRY.values(), key=lambda d: d.display_name)

    results = []
    for definition in _REGISTRY.values():
        haystack = " ".join(
            [definition.type_id, definition.display_name, definition.description, *definition.keywords]
        ).lower()
        if normalized in haystack:
            results.append(definition)
    results.sort(key=lambda d: d.display_name)
    return results


def is_parent_allowed(type_id: str, parent_type_id: str | None) -> bool:
    """parent_type_id — либо имя корневого сервиса, либо class_name родителя."""
    definition = get_object_type(type_id)
    if definition is None:
        return False
    if not definition.allowed_parent_types:
        return True
    return parent_type_id in definition.allowed_parent_types


# ============================================================
# GENERIC-САНИТАЙЗЕР (для типов, не имеющих Part-формы)
# ============================================================

def sanitize_properties_for_type(type_id: str, raw_properties: Any) -> dict[str, Any]:
    """
    Аналог instance.sanitize_part_properties(), но управляемый декларативной
    схемой из реестра — используется сервером для всех типов, КРОМЕ
    Part/SpawnPoint (у них своя, уже проверенная в бою, санитация).
    """
    definition = get_object_type(type_id)
    if definition is None or not isinstance(raw_properties, dict):
        return {}

    clean: dict[str, Any] = {}
    for key, spec in definition.property_schema.items():
        if key not in raw_properties:
            continue
        value = raw_properties[key]

        if spec.kind == "vector3":
            if instance_module.is_valid_vector3(value):
                clean[key] = [float(v) for v in value]
        elif spec.kind == "size3":
            if instance_module.is_valid_vector3(value):
                clean[key] = [max(instance_module.MIN_PART_SIZE, float(v)) for v in value]
        elif spec.kind == "color":
            if instance_module.is_valid_color(value):
                clean[key] = [int(v) for v in value]
        elif spec.kind == "bool":
            if isinstance(value, bool):
                clean[key] = value
        elif spec.kind == "float":
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                clean[key] = float(value)
        elif spec.kind == "float01":
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                clean[key] = max(0.0, min(1.0, float(value)))
        elif spec.kind == "int":
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                clean[key] = int(value)
        elif spec.kind == "string":
            if isinstance(value, str):
                clean[key] = value[: spec.max_len]

    return clean


# Stage 4.1 (local lighting foundation): has_3d_entity classes that are
# real spatial Instances (Position, world-membership, Clone/Destroy, ...)
# but are NOT Part-shaped -- no picking-relevant Size/Anchored/CanCollide
# collider, and MUST NEVER get a Bullet physics body. Every
# self._physics(.world).add_part(...) call site in lua_runtime.py/
# client_studio.py checks this before adding a body, rather than each
# guessing "is this a light" from class_name directly -- one definition.
LIGHT_CLASS_NAMES: tuple[str, ...] = ("PointLight", "SpotLight")


# ============================================================
# ПЕРВАЯ ВЕРСИЯ НАБОРА ТИПОВ
# ============================================================

_PART_LIKE_SCHEMA: dict[str, PropertySpec] = {
    "Position": PropertySpec("vector3"),
    "Size": PropertySpec("size3"),
    "Rotation": PropertySpec("vector3"),
    "Color": PropertySpec("color"),
    "Material": PropertySpec("string", 32),
    "Transparency": PropertySpec("float01"),
    "Anchored": PropertySpec("bool"),
    "CanCollide": PropertySpec("bool"),
}

_PART_LIKE_DEFAULTS: dict[str, Any] = dict(instance_module.DEFAULT_PART_PROPERTIES)

_CONTAINER_PARENTS = ("Workspace", "Model", "Folder")

# Stage 4.1 (materials & textures foundation): Part-ONLY surface
# properties -- deliberately NOT folded into _PART_LIKE_SCHEMA/
# _PART_LIKE_DEFAULTS above, which SpawnPoint and MeshPart also inherit.
# SpawnPoint is a functional gameplay marker, not a decorative surface,
# and MeshPart's whole point is to preserve its OWN imported GLB material
# untouched (see MeshPart's registration comment) -- so texturing/PBR
# controls are scoped to plain Part only, merged into ITS OWN
# default_properties/property_schema below via **_PART_LIKE_SCHEMA
# (unchanged) plus these six keys, the same pattern MeshPart already uses
# for its one extra field (MeshId).
#
# TilesPerUnit (not a raw TextureScale Vector2): PropertySpec has no
# vector2 kind, and a single "how many times should this repeat per world
# unit" number is more predictable for level-building than asking a
# creator to hand-compute a UV scale from Size. client_studio.py computes
# the actual (u, v) texture_scale from Size's two LARGEST dimensions --
# correct for the common "thin slab" wall/floor/ceiling Part shape, an
# honest approximation for anything else (see its own comment for why a
# single global UV scale can't be exactly right on all 6 faces of an
# arbitrary box without real per-face UV authoring, which is out of scope
# this pass).
_PART_SURFACE_SCHEMA: dict[str, PropertySpec] = {
    "TextureId": PropertySpec("string", 256),
    "TilesPerUnit": PropertySpec("float"),
    "Roughness": PropertySpec("float01"),
    "Metallic": PropertySpec("float01"),
    "EmissionColor": PropertySpec("color"),
    "EmissionStrength": PropertySpec("float"),
}

_PART_SURFACE_DEFAULTS: dict[str, Any] = {
    "TextureId": "",
    "TilesPerUnit": 1.0,
    "Roughness": 1.0,
    "Metallic": 0.0,
    "EmissionColor": [0, 0, 0],
    "EmissionStrength": 0.0,
}

# Stage 4.1: makes the pre-existing "Material" string property (already
# serialized, already in the Inspector's dropdown, previously purely
# cosmetic -- see Part 4 of the materials-foundation task) genuinely
# useful WITHOUT changing its type or breaking existing Places/API: each
# preset supplies Roughness/Metallic (and, for "Neon", a small default
# EmissionStrength) defaults applied when a creator explicitly PICKS that
# preset in the Inspector -- never re-enforced afterward, so adjusting
# Roughness/Metallic by hand right after always wins. Deliberately the
# same six names the Inspector's Material dropdown already offered
# (nothing new invented) and deliberately small -- not a material catalog.
MATERIAL_PRESETS: dict[str, dict[str, float]] = {
    "Plastic": {"Roughness": 0.5, "Metallic": 0.0},
    "Metal": {"Roughness": 0.35, "Metallic": 1.0},
    "Wood": {"Roughness": 0.8, "Metallic": 0.0},
    # Stage 4.3D: Transparency=0.6 gives Glass a useful starting look
    # instead of silently staying fully opaque -- still a ONE-SHOT
    # initializer like every other preset field (see
    # _on_material_preset_changed()'s own docstring): a creator can
    # freely readjust Transparency afterward and nothing re-applies
    # this value later.
    "Glass": {"Roughness": 0.05, "Metallic": 0.0, "Transparency": 0.6},
    "Concrete": {"Roughness": 0.9, "Metallic": 0.0},
    "Neon": {"Roughness": 0.3, "Metallic": 0.0, "EmissionStrength": 2.0},
}


def _register_defaults() -> None:
    register_object_type(ObjectTypeDefinition(
        type_id="Part",
        display_name="Part",
        category="Basic",
        description="A basic 3D block that can be moved, rotated and collided with.",
        icon="cube",
        default_parent="Workspace",
        allowed_parent_types=_CONTAINER_PARENTS,
        keywords=("block", "cube", "brick"),
        default_properties={**_PART_LIKE_DEFAULTS, **_PART_SURFACE_DEFAULTS},
        property_schema={**_PART_LIKE_SCHEMA, **_PART_SURFACE_SCHEMA},
        has_3d_entity=True,
        # "surface" (Stage 4.1 materials & textures foundation): texture/
        # PBR authoring -- see studio_editor_live.py's
        # InspectorPanel._build_surface_section(). Part-only, same reason
        # the new properties above are Part-only.
        inspector_sections=("transform", "surface", "appearance", "behavior"),
    ))

    # Stage 4.1 (showcase sprint): a Part-like object whose visual geometry
    # is a loaded mesh (.glb/.gltf via panda3d-gltf, see
    # client_studio.py's load_mesh_node()) instead of the primitive cube
    # every other Part-form uses. Deliberately reuses _PART_LIKE_SCHEMA/
    # _PART_LIKE_DEFAULTS wholesale (same Position/Size/Rotation/Color-as-
    # tint/Transparency/Anchored/CanCollide contract as Part) plus exactly
    # one new field -- MeshId, a path relative to client_studio.py's
    # MESH_ASSETS_DIR -- rather than inventing a parallel property system.
    # Size still drives the (box-only) physics/picking collider, same as
    # Part/SpawnPoint: this is a deliberate scope decision (see Stage 4.1
    # report), not an oversight -- true per-triangle mesh collision is not
    # implemented and is out of scope for the showcase sprint.
    #
    # DESIGN NOTE, NOT YET RESOLVED (recorded during the Stage 4.1
    # mesh-lighting investigation, see test_mesh_part.py's
    # test_size_is_a_raw_multiplier_on_native_mesh_bounds_not_normalized):
    # Size currently SCALES whatever native bounding-box dimensions the
    # loaded GLB happens to have -- it is NOT normalized against those
    # native bounds first. Two GLBs with different native sizes and the
    # SAME MeshPart.Size therefore end up with different world-space
    # dimensions, which is surprising for a creator coming from an engine
    # where Size means "final size." The likely better long-term contract
    # is Size = final world-space bounding-box dimensions (normalize the
    # imported mesh against its own native tight-bounds, then scale to
    # Size) -- but do NOT implement that yet: first-load timing (bounds
    # aren't known until the mesh is actually loaded), MeshId-replacement
    # behavior (what happens to an already-placed MeshPart's world size
    # when its MeshId changes to an asset with different native
    # proportions), and non-uniform aspect-ratio handling all need to be
    # designed before changing this contract. Do not add MeshScale or any
    # other speculative property in the meantime.
    register_object_type(ObjectTypeDefinition(
        type_id="MeshPart",
        display_name="MeshPart",
        category="Basic",
        description="A Part-like object that displays an imported 3D mesh (glTF/GLB) instead of a primitive cube.",
        icon="cube",
        default_parent="Workspace",
        allowed_parent_types=_CONTAINER_PARENTS,
        keywords=("mesh", "model", "import", "gltf", "glb", "prop"),
        default_properties={**_PART_LIKE_DEFAULTS, "MeshId": ""},
        property_schema={**_PART_LIKE_SCHEMA, "MeshId": PropertySpec("string", 256)},
        has_3d_entity=True,
        # "mesh" (Stage 4.1 follow-up): minimum MeshId authoring -- see
        # studio_editor_live.py's InspectorPanel._build_mesh_section().
        inspector_sections=("transform", "mesh", "appearance", "behavior"),
    ))

    register_object_type(ObjectTypeDefinition(
        type_id="Model",
        display_name="Model",
        category="Basic",
        description="A hierarchical container for grouping parts and other objects.",
        icon="cube",
        default_parent="Workspace",
        allowed_parent_types=_CONTAINER_PARENTS,
        keywords=("group", "container"),
        # Stage 2.2: Model gains a persistent world-space pivot transform so
        # it can behave as a real transformable group (see
        # SCRIPTING_ARCHITECTURE.md / Stage 2.2 report for the full design).
        # PivotIsExplicit starts False: an unmoved Model has no meaningful
        # stored pivot yet, the client derives one lazily from descendant
        # bounds for gizmo display only, and nothing is written here until
        # the user actually performs a real move/rotate/pivot edit. This is
        # why old scenes need no migration — {**defaults, **raw} already
        # gives every legacy Model a harmless default pivot for free.
        default_properties={
            "PivotPosition": [0.0, 0.0, 0.0],
            "PivotRotation": [0.0, 0.0, 0.0],
            "PivotIsExplicit": False,
        },
        property_schema={
            "PivotPosition": PropertySpec("vector3"),
            "PivotRotation": PropertySpec("vector3"),
            "PivotIsExplicit": PropertySpec("bool"),
        },
        is_container=True,
        inspector_sections=("container", "pivot"),
    ))

    # Stage 3.8: singleton container -- allowed_parent_types restricts it
    # to StarterPlayer alone; the "at most one" rule itself is enforced
    # server-side (handle_create_part/handle_replace_world), same
    # authority boundary every other creation rule already uses. LocalScript/
    # ModuleScript children already execute/require correctly once parented
    # here, since build_script_execution_plan() (lua_runtime.py) walks
    # StarterPlayer's ENTIRE subtree, not just its direct children.
    register_object_type(ObjectTypeDefinition(
        type_id="StarterPlayerScripts",
        display_name="StarterPlayerScripts",
        category="Basic",
        description="Container for LocalScripts/ModuleScripts that run for every player. At most one per Place.",
        icon="folder",
        default_parent="StarterPlayer",
        allowed_parent_types=("StarterPlayer",),
        keywords=("scripts", "client", "container"),
        default_properties={},
        is_container=True,
        inspector_sections=("container",),
    ))

    register_object_type(ObjectTypeDefinition(
        type_id="Folder",
        display_name="Folder",
        category="Basic",
        description="An organizational container with no behaviour of its own.",
        icon="folder",
        default_parent="Workspace",
        allowed_parent_types=(*_CONTAINER_PARENTS, *ROOT_SERVICES),
        keywords=("organize", "group"),
        default_properties={},
        is_container=True,
        inspector_sections=("container",),
    ))

    register_object_type(ObjectTypeDefinition(
        type_id="Script",
        display_name="Script",
        category="Scripting",
        description="Runs on the server. Lua execution is not implemented yet.",
        icon="script",
        default_parent="ServerScriptService",
        allowed_parent_types=(*_CONTAINER_PARENTS, "ServerScriptService", "ServerStorage"),
        keywords=("lua", "server", "code"),
        default_properties={"Source": "-- Script\n", "RunContext": "Server"},
        property_schema={
            "Source": PropertySpec("string", 20000),
            "RunContext": PropertySpec("string", 16),
        },
        inspector_sections=("script",),
    ))

    register_object_type(ObjectTypeDefinition(
        type_id="LocalScript",
        display_name="LocalScript",
        category="Scripting",
        description="Runs on the client. Lua execution is not implemented yet.",
        icon="script",
        default_parent="StarterPlayer",
        allowed_parent_types=(*_CONTAINER_PARENTS, "StarterPlayer", "StarterPlayerScripts", "StarterGui", "ReplicatedStorage"),
        keywords=("lua", "client", "code"),
        default_properties={"Source": "-- LocalScript\n", "RunContext": "Client"},
        property_schema={
            "Source": PropertySpec("string", 20000),
            "RunContext": PropertySpec("string", 16),
        },
        inspector_sections=("script",),
    ))

    register_object_type(ObjectTypeDefinition(
        type_id="ModuleScript",
        display_name="ModuleScript",
        category="Scripting",
        description="A reusable Lua module, required by other scripts.",
        icon="script",
        default_parent="ReplicatedStorage",
        allowed_parent_types=(*_CONTAINER_PARENTS, "ServerScriptService", "ServerStorage", "ReplicatedStorage", "StarterPlayer", "StarterPlayerScripts"),
        keywords=("lua", "module", "require", "code"),
        default_properties={"Source": "-- ModuleScript\nlocal module = {}\n\nreturn module\n"},
        property_schema={"Source": PropertySpec("string", 20000)},
        inspector_sections=("script",),
    ))

    register_object_type(ObjectTypeDefinition(
        type_id="SpawnPoint",
        display_name="SpawnPoint",
        category="World",
        description="Marks where players appear. Renders as a flat coloured pad for now.",
        icon="spawn",
        default_parent="Workspace",
        allowed_parent_types=_CONTAINER_PARENTS,
        keywords=("spawn", "respawn", "start"),
        default_properties={
            **_PART_LIKE_DEFAULTS,
            "Size": [6.0, 0.4, 6.0],
            "Color": [0, 200, 90],
            "Transparency": 0.25,
            "CanCollide": False,
        },
        property_schema=dict(_PART_LIKE_SCHEMA),
        has_3d_entity=True,
        inspector_sections=("transform", "appearance", "behavior"),
    ))

    # Stage 4.1 (local lighting foundation): a real spatial light Instance,
    # not the old "not implemented yet" placeholder. Deliberately NOT
    # has_3d_entity in the Part sense (no Size/Anchored/CanCollide/Material/
    # MeshId) -- see LIGHT_CLASS_NAMES's own comment for why every physics-
    # body-adding call site excludes it. Position is still declared here
    # (client_studio.py's has_3d_entity property-read/write branches handle
    # Position/Color generically for every has_3d_entity class already;
    # Intensity/Range are new, light-specific fields).
    register_object_type(ObjectTypeDefinition(
        type_id="PointLight",
        display_name="PointLight",
        category="World",
        description="A local light source that illuminates nearby geometry with distance falloff.",
        icon="light",
        default_parent="Workspace",
        allowed_parent_types=_CONTAINER_PARENTS,
        keywords=("light", "lamp", "glow", "point"),
        default_properties={
            "Position": [0.0, 0.0, 0.0],
            "Color": [255, 255, 255],
            "Intensity": 1.0,
            "Range": 8.0,
        },
        property_schema={
            "Position": PropertySpec("vector3"),
            "Color": PropertySpec("color"),
            "Intensity": PropertySpec("float"),
            "Range": PropertySpec("float"),
        },
        has_3d_entity=True,
        inspector_sections=("light",),
    ))

    # SpotLight reuses the exact same foundation as PointLight (position,
    # color, intensity, range, world-membership, no physics) plus ordinary
    # Instance Rotation for orientation (no separate look-vector API) and
    # one extra field, Angle (cone half-angle in degrees).
    register_object_type(ObjectTypeDefinition(
        type_id="SpotLight",
        display_name="SpotLight",
        category="World",
        description="A directional cone light source -- for fixtures, spots, and focused pools of light.",
        icon="light",
        default_parent="Workspace",
        allowed_parent_types=_CONTAINER_PARENTS,
        keywords=("light", "lamp", "spot", "cone", "fixture"),
        default_properties={
            "Position": [0.0, 0.0, 0.0],
            "Rotation": [0.0, 0.0, 0.0],
            "Color": [255, 255, 255],
            "Intensity": 1.0,
            "Range": 10.0,
            "Angle": 45.0,
        },
        property_schema={
            "Position": PropertySpec("vector3"),
            "Rotation": PropertySpec("vector3"),
            "Color": PropertySpec("color"),
            "Intensity": PropertySpec("float"),
            "Range": PropertySpec("float"),
            "Angle": PropertySpec("float"),
        },
        has_3d_entity=True,
        inspector_sections=("light",),
    ))

    register_object_type(ObjectTypeDefinition(
        type_id="Sound",
        display_name="Sound",
        category="World",
        description="Editor-only placeholder — audio playback is not implemented yet.",
        icon="sound",
        default_parent="Workspace",
        allowed_parent_types=_CONTAINER_PARENTS,
        keywords=("audio", "music", "sfx"),
        default_properties={"SoundId": "", "Volume": 0.5, "Looped": False, "Playing": False},
        property_schema={
            "SoundId": PropertySpec("string", 256),
            "Volume": PropertySpec("float01"),
            "Looped": PropertySpec("bool"),
            "Playing": PropertySpec("bool"),
        },
        editor_only=True,
        inspector_sections=("placeholder",),
    ))

    register_object_type(ObjectTypeDefinition(
        type_id="ParticleEmitter",
        display_name="ParticleEmitter",
        category="World",
        description="Editor-only placeholder — particle rendering is not implemented yet.",
        icon="particle",
        default_parent="Workspace",
        allowed_parent_types=_CONTAINER_PARENTS,
        keywords=("particles", "effects", "vfx"),
        default_properties={"Rate": 20.0},
        property_schema={"Rate": PropertySpec("float")},
        editor_only=True,
        inspector_sections=("placeholder",),
    ))

    register_object_type(ObjectTypeDefinition(
        type_id="ScreenGui",
        display_name="ScreenGui",
        category="GUI",
        description="Editor-only placeholder — UI rendering is not implemented yet.",
        icon="ui",
        default_parent="StarterGui",
        allowed_parent_types=("StarterGui", "ReplicatedStorage"),
        keywords=("gui", "ui", "hud", "screen"),
        default_properties={},
        is_container=True,
        editor_only=True,
        inspector_sections=("placeholder",),
    ))

    register_object_type(ObjectTypeDefinition(
        type_id="Frame",
        display_name="Frame",
        category="GUI",
        description="Editor-only placeholder — UI rendering is not implemented yet.",
        icon="ui",
        default_parent="StarterGui",
        allowed_parent_types=("StarterGui", "ScreenGui", "Frame"),
        keywords=("gui", "ui", "panel"),
        default_properties={"BackgroundColor": [255, 255, 255], "Visible": True},
        property_schema={
            "BackgroundColor": PropertySpec("color"),
            "Visible": PropertySpec("bool"),
        },
        is_container=True,
        editor_only=True,
        inspector_sections=("placeholder",),
    ))

    register_object_type(ObjectTypeDefinition(
        type_id="TextLabel",
        display_name="TextLabel",
        category="GUI",
        description="Editor-only placeholder — UI rendering is not implemented yet.",
        icon="ui",
        default_parent="StarterGui",
        allowed_parent_types=("StarterGui", "ScreenGui", "Frame"),
        keywords=("gui", "ui", "text", "label"),
        default_properties={"Text": "Label", "Visible": True},
        property_schema={
            "Text": PropertySpec("string", 256),
            "Visible": PropertySpec("bool"),
        },
        editor_only=True,
        inspector_sections=("placeholder",),
    ))

    register_object_type(ObjectTypeDefinition(
        type_id="TextButton",
        display_name="TextButton",
        category="GUI",
        description="Editor-only placeholder — UI rendering is not implemented yet.",
        icon="ui",
        default_parent="StarterGui",
        allowed_parent_types=("StarterGui", "ScreenGui", "Frame"),
        keywords=("gui", "ui", "button", "click"),
        default_properties={"Text": "Button", "Visible": True},
        property_schema={
            "Text": PropertySpec("string", 256),
            "Visible": PropertySpec("bool"),
        },
        editor_only=True,
        inspector_sections=("placeholder",),
    ))

    register_object_type(ObjectTypeDefinition(
        type_id="ImageLabel",
        display_name="ImageLabel",
        category="GUI",
        description="Editor-only placeholder — UI rendering is not implemented yet.",
        icon="ui",
        default_parent="StarterGui",
        allowed_parent_types=("StarterGui", "ScreenGui", "Frame"),
        keywords=("gui", "ui", "image", "picture"),
        default_properties={"Image": "", "Visible": True},
        property_schema={
            "Image": PropertySpec("string", 256),
            "Visible": PropertySpec("bool"),
        },
        editor_only=True,
        inspector_sections=("placeholder",),
    ))

    # Stage 3.9: minimal gameplay-state primitives (score counters, flags,
    # ...). Deliberately generic -- a single "Value" property per class,
    # driven entirely through the same PropertySpec machinery every other
    # non-Part type already uses (see RuntimeSceneLayer's generic
    # get_property/set_property fallback in lua_runtime.py), so there is no
    # parallel property system. allowed_parent_types is left empty (allowed
    # anywhere a Parent can point) rather than restricted to containers:
    # unlike Folder/Model, a Value instance is commonly parented directly to
    # a leaf object (e.g. a Part acting as a pickup, or a player-tracking
    # container) and gains nothing from being container-only. NOTE: Players.
    # LocalPlayer is a Lua-side proxy object owned by LuaGameplayContext, not
    # a RuntimeSceneLayer hierarchy instance -- `.Parent = player` from the
    # spec's example is therefore not literally supported (there is no
    # Instance for it to parent under); use a Folder under the player's
    # Character or Workspace instead. This is a deliberate, documented scope
    # decision (see Stage 3.9 report), not an oversight.
    for _value_type in ("BoolValue", "IntValue", "NumberValue", "StringValue"):
        _value_kind, _value_default = {
            "BoolValue": ("bool", False),
            "IntValue": ("int", 0),
            "NumberValue": ("float", 0.0),
            "StringValue": ("string", ""),
        }[_value_type]
        register_object_type(ObjectTypeDefinition(
            type_id=_value_type,
            display_name=_value_type,
            category="Values",
            description="Holds a single gameplay value, readable and writable from Lua.",
            icon="value",
            default_parent="Workspace",
            allowed_parent_types=(),
            keywords=("value", "state", "score", "variable"),
            default_properties={"Value": _value_default},
            property_schema={"Value": PropertySpec(_value_kind)},
            inspector_sections=("value",),
        ))


_register_defaults()
