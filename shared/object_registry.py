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
)


# ============================================================
# ОПИСАНИЕ СВОЙСТВА (для generic-санитайзера non-Part типов)
# ============================================================

@dataclass(frozen=True)
class PropertySpec:
    kind: str  # "vector3" | "size3" | "color" | "bool" | "float" | "float01" | "string"
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
        elif spec.kind == "string":
            if isinstance(value, str):
                clean[key] = value[: spec.max_len]

    return clean


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
        default_properties=dict(_PART_LIKE_DEFAULTS),
        property_schema=dict(_PART_LIKE_SCHEMA),
        has_3d_entity=True,
        inspector_sections=("transform", "appearance", "behavior"),
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

    register_object_type(ObjectTypeDefinition(
        type_id="PointLight",
        display_name="PointLight",
        category="World",
        description="Editor-only placeholder — real-time lighting is not implemented yet.",
        icon="light",
        default_parent="Workspace",
        allowed_parent_types=_CONTAINER_PARENTS,
        keywords=("light", "lamp", "glow"),
        default_properties={"Color": [255, 255, 255], "Brightness": 1.0, "Range": 8.0},
        property_schema={
            "Color": PropertySpec("color"),
            "Brightness": PropertySpec("float"),
            "Range": PropertySpec("float"),
        },
        editor_only=True,
        inspector_sections=("placeholder",),
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


_register_defaults()
