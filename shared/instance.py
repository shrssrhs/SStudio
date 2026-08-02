"""
Объектная модель мира — аналог Instance/Part из Roblox Studio.

Общий код для сервера и клиента: оба должны одинаково понимать, что такое
"часть мира" и как упаковать/распаковать её в JSON для сети.

Сервер держит словарь Instance-ов как источник правды (world state),
клиент получает снапшоты/диффы и создаёт по ним визуальные Entity.
"""

from __future__ import annotations

import math
import secrets
from dataclasses import dataclass, field
from typing import Any


# ============================================================
# ДЕФОЛТНЫЕ СВОЙСТВА
# ============================================================

# Свойства, которые есть у любого Part. Как в Roblox: Position/Size/
# Color и т.д. Клиент читает эти ключи, чтобы построить Entity;
# сервер их не интерпретирует, просто хранит и рассылает.
DEFAULT_PART_PROPERTIES: dict[str, Any] = {
    "Position": [0.0, 0.0, 0.0],
    "Size": [1.0, 1.0, 1.0],
    "Rotation": [0.0, 0.0, 0.0],
    "Color": [255, 255, 255],
    "Material": "Plastic",
    "Transparency": 0.0,
    "Anchored": True,
    "CanCollide": True,
}


def new_instance_id() -> str:
    return secrets.token_hex(8)


# ============================================================
# INSTANCE
# ============================================================

@dataclass
class Instance:
    """
    Базовый объект мира. class_name решает, как клиент его рендерит
    ("Part", в будущем "SpawnLocation", "Model" и т.д. — расширяется
    так же, как ClassName в Roblox).
    """

    id: str = field(default_factory=new_instance_id)
    class_name: str = "Part"
    name: str = "Part"
    parent_id: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "class_name": self.class_name,
            "name": self.name,
            "parent_id": self.parent_id,
            "properties": self.properties,
            "tags": self.tags,
            "attributes": self.attributes,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Instance":
        return cls(
            id=str(data["id"]),
            class_name=str(data.get("class_name", "Part")),
            name=str(data.get("name", "Part")),
            parent_id=data.get("parent_id"),
            properties=dict(data.get("properties", {})),
            tags=list(data.get("tags", [])),
            attributes=dict(data.get("attributes", {})),
            enabled=bool(data.get("enabled", True)),
        )

    def get_property(self, key: str, default: Any = None) -> Any:
        return self.properties.get(key, default)

    def set_property(self, key: str, value: Any) -> None:
        self.properties[key] = value

    def rename(self, new_name: str) -> None:
        cleaned = new_name.strip()
        if cleaned:
            self.name = cleaned

    def set_parent(self, parent_id: str | None) -> None:
        self.parent_id = parent_id


# ============================================================
# ОПЕРАЦИИ НАД ГРАФОМ МИРА
# ============================================================

# Instance не хранит список children — иерархия целиком выводится из
# parent_id при обходе world-словаря. Это исключает рассинхронизацию
# (например, если бы child-список и parent_id разошлись после сетевого
# апдейта); цена — O(n) обход при удалении/сборе потомков, что для
# масштабов этого проекта незаметно.

def get_children(world: dict[str, "Instance"], parent_id: str | None) -> list["Instance"]:
    return [item for item in world.values() if item.parent_id == parent_id]


def get_descendant_ids(world: dict[str, "Instance"], root_id: str) -> list[str]:
    result: list[str] = []
    frontier = [root_id]
    while frontier:
        current = frontier.pop()
        children = get_children(world, current)
        for child in children:
            result.append(child.id)
            frontier.append(child.id)
    return result


def destroy_cascade(world: dict[str, "Instance"], instance_id: str) -> list[str]:
    """Удаляет instance_id и всех его потомков из world. Возвращает id всех
    удалённых объектов (сам instance_id включён), в порядке "листья раньше
    предков", удобном для рассылки part_deleted по одному."""
    if instance_id not in world:
        return []

    descendant_ids = get_descendant_ids(world, instance_id)
    removal_order = list(reversed(descendant_ids)) + [instance_id]

    for removed_id in removal_order:
        world.pop(removed_id, None)

    return removal_order


def serialize_world(world: dict[str, "Instance"]) -> list[dict[str, Any]]:
    return [item.to_dict() for item in world.values()]


def create_part(
    position: list[float] | None = None,
    size: list[float] | None = None,
    color: list[int] | None = None,
    name: str = "Part",
) -> Instance:
    """Фабрика для самого частого случая — обычный блок."""

    properties = dict(DEFAULT_PART_PROPERTIES)

    if position is not None:
        properties["Position"] = list(position)

    if size is not None:
        properties["Size"] = list(size)

    if color is not None:
        properties["Color"] = list(color)

    return Instance(
        class_name="Part",
        name=name,
        properties=properties,
    )


# ============================================================
# ВАЛИДАЦИЯ (сервер не должен доверять клиенту)
# ============================================================

def is_valid_vector3(value: Any) -> bool:
    if not isinstance(value, list) or len(value) != 3:
        return False

    # math.isfinite() rejects NaN/Infinity, which otherwise pass the plain
    # isinstance(x, float) check silently (Stage 2.2: batch model-transform
    # payloads make it much easier for a malformed/malicious client to slip
    # a non-finite value into a Position/Rotation than the old single-field
    # update_property path did, so this gap is closed here for everyone).
    return all(
        isinstance(component, (int, float))
        and not isinstance(component, bool)
        and math.isfinite(component)
        for component in value
    )


def is_valid_color(value: Any) -> bool:
    if not isinstance(value, list) or len(value) != 3:
        return False

    return all(
        isinstance(component, (int, float))
        and not isinstance(component, bool)
        and 0 <= component <= 255
        for component in value
    )


def sanitize_part_properties(raw_properties: dict[str, Any]) -> dict[str, Any]:
    """
    Сервер вызывает это на любых properties, пришедших от клиента,
    прежде чем положить их в мир. Всё непрошедшее проверку — отбрасывается,
    а не падает с ошибкой (клиент мог прислать мусор случайно или специально).
    """

    clean: dict[str, Any] = {}

    position = raw_properties.get("Position")
    if is_valid_vector3(position):
        clean["Position"] = [float(v) for v in position]

    size = raw_properties.get("Size")
    if is_valid_vector3(size):
        clean["Size"] = [max(0.05, float(v)) for v in size]

    rotation = raw_properties.get("Rotation")
    if is_valid_vector3(rotation):
        clean["Rotation"] = [float(v) for v in rotation]

    color = raw_properties.get("Color")
    if is_valid_color(color):
        clean["Color"] = [int(v) for v in color]

    material = raw_properties.get("Material")
    if isinstance(material, str) and len(material) <= 32:
        clean["Material"] = material

    transparency = raw_properties.get("Transparency")
    if isinstance(transparency, (int, float)) and not isinstance(transparency, bool):
        clean["Transparency"] = max(0.0, min(1.0, float(transparency)))

    anchored = raw_properties.get("Anchored")
    if isinstance(anchored, bool):
        clean["Anchored"] = anchored

    can_collide = raw_properties.get("CanCollide")
    if isinstance(can_collide, bool):
        clean["CanCollide"] = can_collide

    return clean