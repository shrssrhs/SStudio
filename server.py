import asyncio
import json
import logging
import secrets
from typing import Any

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

import datamodel_schema
from shared import object_registry, protocol
from shared.instance import (
    DEFAULT_PART_PROPERTIES,
    MIN_PART_SIZE,
    Instance,
    destroy_cascade,
    get_descendant_ids,
    is_valid_vector3,
    sanitize_part_properties,
    serialize_world,
)

PART_LIKE_TYPES = ("Part", "SpawnPoint")

# Stage 2.2: типы, чей id разрешено появляться в "descendants" батча
# transform_model. Part-подобные хранят Position/Rotation, Model — свой
# PivotPosition/PivotRotation (см. object_registry.py). Folder/Script и
# прочие непространственные типы транзитом проходят через дерево потомков
# при обходе get_descendant_ids, но сами transform не имеют — их id здесь
# отклоняются.
_MODEL_TRANSFORM_DESCENDANT_TYPES = PART_LIKE_TYPES + ("Model", "MeshPart")


HOST = "0.0.0.0"
PORT = 8765
TICK_RATE = 20

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)

clients: dict[str, ServerConnection] = {}
players: dict[str, dict[str, Any]] = {}

# Источник правды о построенном мире. Ключ — Instance.id.
# Клиенты никогда не хранят "оригинал" — только то, что им прислал сервер.
world: dict[str, Instance] = {}

# Stage 3.8: persistent root-service properties (Workspace.Gravity,
# StarterPlayer.CharacterWalkSpeed, ...). Root services are never Instances
# (see object_registry.ROOT_SERVICES) so they live in their own dict, keyed
# by service name, validated through datamodel_schema instead of the
# Instance-shaped sanitizers above. Seeded with every registered service
# class's own defaults so a brand-new server (or a Place with no "services"
# key at all -- format version < 3) always has a complete, valid set.
services: dict[str, dict[str, Any]] = datamodel_schema.sanitize_services_snapshot(None)

state_lock = asyncio.Lock()


def create_player(player_id: str) -> dict[str, Any]:
    spawn_index = len(players)

    return {
        "id": player_id,
        "name": f"Player-{player_id[:4]}",
        "position": [
            float((spawn_index % 4) * 3),
            3.0,
            float((spawn_index // 4) * 3),
        ],
        "rotation_y": 0.0,
        "rotation_x": 0.0,
    }


def is_valid_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and -100_000 <= float(value) <= 100_000
    )


def validate_transform_message(
    message: dict[str, Any],
) -> bool:
    position = message.get("position")
    rotation_y = message.get("rotation_y")
    rotation_x = message.get("rotation_x", 0.0)

    if not isinstance(position, list):
        return False

    if len(position) != 3:
        return False

    if not all(
        is_valid_number(value)
        for value in position
    ):
        return False

    if not is_valid_number(rotation_y):
        return False

    if not is_valid_number(rotation_x):
        return False

    return True


async def send_json(
    websocket: ServerConnection,
    payload: dict[str, Any],
) -> bool:
    try:
        await websocket.send(
            json.dumps(payload)
        )
        return True

    except ConnectionClosed:
        return False

    except Exception:
        logging.exception(
            "Ошибка отправки сообщения"
        )
        return False


async def broadcast_to_all(payload: dict[str, Any]) -> None:
    """
    Рассылает payload всем подключённым клиентам. В отличие от
    broadcast_world_state (который шлёт позиции игроков по тик-рейту),
    это разовая рассылка события — используется для part_created/
    part_updated/part_deleted, которые должны прийти сразу, а не ждать
    следующего тика.
    """

    async with state_lock:
        connections = list(clients.items())

    for player_id, websocket in connections:
        await send_json(websocket, payload)


async def broadcast_world_state() -> None:
    while True:
        await asyncio.sleep(1 / TICK_RATE)

        async with state_lock:
            if not clients:
                continue

            payload = {
                "type": "world_state",
                "players": players,
            }

            connections = list(
                clients.items()
            )

        encoded_payload = json.dumps(payload)

        disconnected_ids: list[str] = []

        for player_id, websocket in connections:
            try:
                await websocket.send(
                    encoded_payload
                )

            except ConnectionClosed:
                disconnected_ids.append(
                    player_id
                )

            except Exception:
                logging.exception(
                    "Не удалось отправить состояние игроку %s",
                    player_id,
                )

                disconnected_ids.append(
                    player_id
                )

        if disconnected_ids:
            async with state_lock:
                for player_id in disconnected_ids:
                    clients.pop(
                        player_id,
                        None,
                    )

                    players.pop(
                        player_id,
                        None,
                    )


async def handle_message(
    player_id: str,
    message: dict[str, Any],
) -> None:
    message_type = message.get("type")

    if message_type == "transform":
        if not validate_transform_message(message):
            logging.warning(
                "Игрок %s прислал неправильный transform",
                player_id,
            )
            return

        async with state_lock:
            player = players.get(player_id)

            if player is None:
                return

            player["position"] = [
                float(message["position"][0]),
                float(message["position"][1]),
                float(message["position"][2]),
            ]

            player["rotation_y"] = float(
                message["rotation_y"]
            )

            player["rotation_x"] = float(
                message.get(
                    "rotation_x",
                    0.0,
                )
            )

    elif message_type == "set_name":
        requested_name = str(
            message.get(
                "name",
                "",
            )
        ).strip()

        if not requested_name:
            return

        safe_name = requested_name[:20]

        async with state_lock:
            player = players.get(player_id)

            if player is not None:
                player["name"] = safe_name

    elif message_type == protocol.CREATE_PART:
        await handle_create_part(player_id, message)

    elif message_type == protocol.UPDATE_PROPERTY:
        await handle_update_property(player_id, message)

    elif message_type == protocol.DELETE_PART:
        await handle_delete_part(player_id, message)

    elif message_type == protocol.SET_PARENT:
        await handle_set_parent(player_id, message)

    elif message_type == protocol.TRANSFORM_MODEL:
        await handle_transform_model(player_id, message)

    elif message_type == protocol.REPLACE_WORLD:
        await handle_replace_world(player_id, message)

    elif message_type == protocol.UPDATE_SERVICE_PROPERTY:
        await handle_update_service_property(player_id, message)


async def handle_create_part(
    player_id: str,
    message: dict[str, Any],
) -> None:
    raw_properties = message.get("properties", {})

    if not isinstance(raw_properties, dict):
        logging.warning(
            "Игрок %s прислал неправильный create_part",
            player_id,
        )
        return

    class_name = str(message.get("class_name") or "Part")
    definition = object_registry.get_object_type(class_name)

    if definition is None or not definition.creatable:
        logging.warning(
            "Игрок %s запросил неизвестный/некреатируемый тип '%s'; создаю Part",
            player_id,
            class_name,
        )
        class_name = "Part"
        definition = object_registry.get_object_type("Part")

    if class_name in PART_LIKE_TYPES:
        clean_properties = sanitize_part_properties(raw_properties)
        base_defaults = DEFAULT_PART_PROPERTIES if class_name == "Part" else definition.default_properties
    else:
        clean_properties = object_registry.sanitize_properties_for_type(class_name, raw_properties)
        base_defaults = definition.default_properties if definition is not None else {}

    properties = dict(base_defaults)
    properties.update(clean_properties)

    parent_id = message.get("parent_id")
    if not isinstance(parent_id, str) or not parent_id:
        parent_id = None

    requested_name = str(message.get("name", "")).strip()
    default_name = definition.display_name if definition is not None else class_name
    part = Instance(
        class_name=class_name,
        name=(requested_name[:32] if requested_name else default_name),
        parent_id=parent_id,
        properties=properties,
    )

    async with state_lock:
        conflict = _singleton_conflict(world, part.class_name, part.name, part.parent_id)
        if conflict is not None:
            logging.warning(
                "Игрок %s: create_part отклонён (%s)", player_id, conflict,
            )
            return
        world[part.id] = part

    logging.info(
        "Игрок %s создал %s %s (%s)",
        player_id,
        class_name,
        part.id,
        part.name,
    )

    await broadcast_to_all(
        {
            "type": protocol.PART_CREATED,
            "part": part.to_dict(),
        }
    )


async def handle_update_property(
    player_id: str,
    message: dict[str, Any],
) -> None:
    part_id = str(message.get("id", ""))

    if not part_id:
        return

    async with state_lock:
        existing = world.get(part_id)
        class_name = existing.class_name if existing is not None else None

    if class_name is None:
        return

    raw_properties = message.get("properties", {})
    clean_properties: dict[str, Any] = {}
    if isinstance(raw_properties, dict) and raw_properties:
        if class_name in PART_LIKE_TYPES:
            clean_properties = sanitize_part_properties(raw_properties)
        else:
            clean_properties = object_registry.sanitize_properties_for_type(class_name, raw_properties)

    clean_name: str | None = None
    raw_name = message.get("name")
    if isinstance(raw_name, str):
        candidate = raw_name.strip()[:32]
        if candidate:
            clean_name = candidate

    clean_enabled: bool | None = None
    raw_enabled = message.get("enabled")
    if isinstance(raw_enabled, bool):
        clean_enabled = raw_enabled

    if not clean_properties and clean_name is None and clean_enabled is None:
        return

    async with state_lock:
        part = world.get(part_id)

        if part is None:
            return

        if clean_name is not None:
            conflict = _singleton_conflict(
                world, part.class_name, clean_name, part.parent_id, exclude_id=part.id,
            )
            if conflict is not None:
                logging.warning(
                    "Игрок %s: rename отклонён для %s (%s)", player_id, part_id, conflict,
                )
                clean_name = None

        if not clean_properties and clean_name is None and clean_enabled is None:
            return

        if clean_properties:
            part.properties.update(clean_properties)
        if clean_name is not None:
            part.rename(clean_name)
        if clean_enabled is not None:
            part.enabled = clean_enabled

    payload: dict[str, Any] = {
        "type": protocol.PART_UPDATED,
        "id": part_id,
    }
    if clean_properties:
        payload["properties"] = clean_properties
    if clean_name is not None:
        payload["name"] = clean_name
    if clean_enabled is not None:
        payload["enabled"] = clean_enabled

    await broadcast_to_all(payload)


async def handle_update_service_property(
    player_id: str,
    message: dict[str, Any],
) -> None:
    """Root-service counterpart to handle_update_property() -- see
    protocol.UPDATE_SERVICE_PROPERTY's docstring for why this is a
    separate message pair. Same optimistic-broadcast shape: sanitize via
    datamodel_schema (never trust the client), merge onto the persistent
    `services` dict, echo back to everyone (including the requester) as
    SERVICE_PROPERTY_UPDATED. Unknown service names and properties are
    silently dropped, matching handle_update_property's own "malformed
    input is untrusted, not fatal" contract."""
    service_name = str(message.get("id", ""))
    descriptor = datamodel_schema.get_class(service_name)
    if descriptor is None or not descriptor.service:
        return

    raw_properties = message.get("properties", {})
    if not isinstance(raw_properties, dict) or not raw_properties:
        return

    clean_properties = datamodel_schema.sanitize_persistent_properties(service_name, raw_properties)
    if not clean_properties:
        return

    async with state_lock:
        current = services.setdefault(service_name, datamodel_schema.default_properties(service_name))
        clean_properties = _apply_zoom_ordering_guard(current, clean_properties)
        if not clean_properties:
            return
        current.update(clean_properties)

    await broadcast_to_all({
        "type": protocol.SERVICE_PROPERTY_UPDATED,
        "id": service_name,
        "properties": clean_properties,
    })


def _apply_zoom_ordering_guard(current: dict[str, Any], clean_properties: dict[str, Any]) -> dict[str, Any]:
    """Stage 3.8 fix: CameraMinZoomDistance/CameraMaxZoomDistance are each
    individually valid floats by the time this runs (sanitize_persistent_
    properties already checked that), but the pair must also stay ORDERED
    -- datamodel_schema.validate_starter_player_zoom() is the one shared
    cross-field rule for that; it was defined but never actually called
    from any write path. Resolves the effective pair (this update's new
    value if present, else whatever is currently persisted in `current`)
    and drops BOTH zoom keys from the update if the resulting pair would
    be invalid -- same "malformed input is dropped, never fatal" contract
    as every other property here. Pure/sync so it's directly unit-testable
    like every other server-side validation helper (_singleton_conflict,
    _hierarchy_is_valid, ...)."""
    if "CameraMinZoomDistance" not in clean_properties and "CameraMaxZoomDistance" not in clean_properties:
        return clean_properties
    effective_min = clean_properties.get("CameraMinZoomDistance", current.get("CameraMinZoomDistance", 2.0))
    effective_max = clean_properties.get("CameraMaxZoomDistance", current.get("CameraMaxZoomDistance", 10.0))
    zoom_result = datamodel_schema.validate_starter_player_zoom(effective_min, effective_max)
    if zoom_result.ok:
        return clean_properties
    clean_properties = dict(clean_properties)
    clean_properties.pop("CameraMinZoomDistance", None)
    clean_properties.pop("CameraMaxZoomDistance", None)
    return clean_properties


async def handle_delete_part(
    player_id: str,
    message: dict[str, Any],
) -> None:
    part_id = str(message.get("id", ""))

    if not part_id:
        return

    async with state_lock:
        if part_id not in world:
            return
        removed_ids = destroy_cascade(world, part_id)

    logging.info(
        "Игрок %s удалил %s (и %d потомков)",
        player_id,
        part_id,
        len(removed_ids) - 1,
    )

    for removed_id in removed_ids:
        await broadcast_to_all(
            {
                "type": protocol.PART_DELETED,
                "id": removed_id,
            }
        )


async def _reject_set_parent(player_id: str, instance_id: str, reason: str) -> None:
    logging.warning("Отклонён set_parent от %s (%s): %s", player_id, instance_id, reason)
    async with state_lock:
        websocket = clients.get(player_id)
    if websocket is not None:
        await send_json(
            websocket,
            {
                "type": protocol.SET_PARENT_REJECTED,
                "id": instance_id,
                "reason": reason,
            },
        )


async def handle_set_parent(
    player_id: str,
    message: dict[str, Any],
) -> None:
    """
    Реродителение сетевых объектов — единственный путь, которым можно
    поменять parent_id (generic update_property его сознательно не
    принимает, см. handle_update_property). Валидация — здесь, а не только
    в Qt UI клиента: клиент не может обойти правила, послав вручную
    собранное сообщение с произвольным parent_id.
    """
    instance_id = str(message.get("id", ""))
    if not instance_id:
        return

    raw_parent_id = message.get("parent_id")
    parent_id = str(raw_parent_id) if isinstance(raw_parent_id, str) and raw_parent_id else "Workspace"

    async with state_lock:
        instance = world.get(instance_id)

    if instance is None:
        await _reject_set_parent(player_id, instance_id, "Object does not exist.")
        return

    if parent_id == instance_id:
        await _reject_set_parent(player_id, instance_id, "An object cannot be its own parent.")
        return

    if parent_id in object_registry.ROOT_SERVICES:
        target_type_id = parent_id
    else:
        async with state_lock:
            target_instance = world.get(parent_id)
        if target_instance is None:
            await _reject_set_parent(player_id, instance_id, "Target parent does not exist.")
            return
        target_type_id = target_instance.class_name

        async with state_lock:
            descendant_ids = set(get_descendant_ids(world, instance_id))
        if parent_id in descendant_ids:
            await _reject_set_parent(
                player_id,
                instance_id,
                f"Cannot parent '{instance.name}' to its own descendant.",
            )
            return

    if not object_registry.is_parent_allowed(instance.class_name, target_type_id):
        await _reject_set_parent(
            player_id,
            instance_id,
            f"'{instance.class_name}' cannot be parented to '{target_type_id}'.",
        )
        return

    if parent_id not in object_registry.ROOT_SERVICES:
        target_definition = object_registry.get_object_type(target_type_id)
        if target_definition is None or not target_definition.is_container:
            await _reject_set_parent(
                player_id,
                instance_id,
                f"'{target_type_id}' cannot contain children.",
            )
            return

    async with state_lock:
        # Перепроверяем внутри лока — instance мог быть удалён между первой
        # проверкой и этой точкой (сообщения от других клиентов
        # обрабатываются конкурентно).
        instance = world.get(instance_id)
        if instance is None:
            return
        conflict = _singleton_conflict(
            world, instance.class_name, instance.name, parent_id, exclude_id=instance.id,
        )
        if conflict is not None:
            reject = True
        else:
            instance.set_parent(parent_id)
            instance_name = instance.name
            reject = False

    if reject:
        await _reject_set_parent(player_id, instance_id, conflict)
        return

    logging.info(
        "Игрок %s: реродитель %s (%s) -> %s",
        player_id,
        instance_id,
        instance_name,
        parent_id,
    )

    await broadcast_to_all(
        {
            "type": protocol.PART_UPDATED,
            "id": instance_id,
            "parent_id": parent_id,
        }
    )


def _expected_transform_descendant_ids(model_id: str) -> set[str]:
    """Must be called with state_lock held. The exact set of ids the
    client's descendants payload is required to match — recomputed fresh
    on every transform_model, never cached across the await boundary, so a
    reparent/create/delete that happens mid-drag is caught by a plain set
    mismatch (see handle_transform_model's single-lock-block rationale)."""
    return {
        d_id for d_id in get_descendant_ids(world, model_id)
        if (d_inst := world.get(d_id)) is not None and d_inst.class_name in _MODEL_TRANSFORM_DESCENDANT_TYPES
    }


def _snapshot_model_transform_state(model_id: str) -> dict[str, Any] | None:
    """Must be called with state_lock held. Authoritative pivot + every
    current transformable descendant's transform — attached to rejections
    so a client whose local preview drifted from a stale hierarchy can
    self-correct in one pass instead of leaving the Model visually split
    (see Stage 2.2 report, 'hierarchy consistency')."""
    model_instance = world.get(model_id)
    if model_instance is None or model_instance.class_name != "Model":
        return None
    descendants_payload: dict[str, dict[str, list[float]]] = {}
    for descendant_id in _expected_transform_descendant_ids(model_id):
        descendant_instance = world.get(descendant_id)
        if descendant_instance is None:
            continue
        if descendant_instance.class_name == "Model":
            descendants_payload[descendant_id] = {
                "Position": descendant_instance.get_property("PivotPosition", [0.0, 0.0, 0.0]),
                "Rotation": descendant_instance.get_property("PivotRotation", [0.0, 0.0, 0.0]),
            }
        else:
            descendants_payload[descendant_id] = {
                "Position": descendant_instance.get_property("Position", [0.0, 0.0, 0.0]),
                "Rotation": descendant_instance.get_property("Rotation", [0.0, 0.0, 0.0]),
                "Size": descendant_instance.get_property("Size", [1.0, 1.0, 1.0]),
            }
    return {
        "pivot": {
            "Position": model_instance.get_property("PivotPosition", [0.0, 0.0, 0.0]),
            "Rotation": model_instance.get_property("PivotRotation", [0.0, 0.0, 0.0]),
        },
        "descendants": descendants_payload,
    }


async def _reject_transform_model(
    player_id: str,
    model_id: str,
    reason: str,
    sequence: Any,
    current: dict[str, Any] | None,
) -> None:
    logging.warning("Отклонён transform_model от %s (%s): %s", player_id, model_id, reason)
    async with state_lock:
        websocket = clients.get(player_id)
    if websocket is not None:
        await send_json(
            websocket,
            {
                "type": protocol.TRANSFORM_MODEL_REJECTED,
                "id": model_id,
                "reason": reason,
                "sequence": sequence,
                "current": current,
            },
        )


def _extract_transform_vectors(raw: Any) -> dict[str, list[float]] | None:
    """Валидирует {"Position": [...], "Rotation": [...], "Size": [...]} —
    все три опциональны (Move-only/Rotate-only/Scale-only кадры не обязаны
    слать остальные), но хотя бы одна должна быть валидным vector3, иначе
    запись мусорная. Size (Stage 2.3, Model uniform scale cascade и
    одиночный Part resize) клэмпится к MIN_PART_SIZE тем же правилом, что
    и sanitize_part_properties — применяется здесь тоже, а не только там,
    потому что этот payload's Size может попасть в model_transform-ветку
    (нет sanitize_part_properties) для не-Model потомков через
    handle_transform_model, который передаёт clean_transform дальше в
    sanitize_part_properties и клэмпит повторно — здесь клэмп нужен только
    чтобы отбросить NaN/Infinity до того, как что-либо более осмысленное
    (типа сравнения factor) увидит мусор."""
    if not isinstance(raw, dict):
        return None
    clean: dict[str, list[float]] = {}
    position = raw.get("Position")
    if position is not None:
        if not is_valid_vector3(position):
            return None
        clean["Position"] = [float(v) for v in position]
    rotation = raw.get("Rotation")
    if rotation is not None:
        if not is_valid_vector3(rotation):
            return None
        clean["Rotation"] = [float(v) for v in rotation]
    size = raw.get("Size")
    if size is not None:
        if not is_valid_vector3(size):
            return None
        clean["Size"] = [max(MIN_PART_SIZE, float(v)) for v in size]
    if not clean:
        return None
    return clean


async def handle_transform_model(player_id: str, message: dict[str, Any]) -> None:
    """
    Атомарный каскадный transform: один Model pivot + произвольное число
    потомков за один кадр сети, вместо N отдельных update_property (это бы
    позволило другим клиентам увидеть "развалившуюся" на середине кадра
    композицию). Сервер не делает никакой кватернионной математики сам —
    как и с одиночным Part-gizmo drag, финальные Position/Rotation уже
    посчитаны клиентом; сервер здесь только проверяет принадлежность/
    форму/конечность значений, то есть остаётся источником правды за счёт
    валидации, а не пересчёта.

    Всё или ничего, по-настоящему: вся валидация И применение происходят
    внутри ОДНОГО удержания state_lock, без await между ними — это не
    просто "перепроверить перед записью" (как в handle_set_parent), а
    полное устранение TOCTOU-окна. С отдельными приобретениями лока (как
    было раньше) сообщение от другого клиента могло обработаться МЕЖДУ
    валидацией и применением и незаметно исказить результат. Здесь это
    структурно невозможно: пока лок удержан, ни один другой awaitable
    обработчик не может вклиниться.

    Ожидаемый набор потомков (_expected_transform_descendant_ids) должен
    ТОЧНО совпадать с присланным — не подмножество. Если между началом
    drag на клиенте и этим сообщением иерархия изменилась (reparent,
    create, delete), набор разойдётся и весь батч отклоняется — сервер не
    молча отфильтровывает лишнее/недостающее и не применяет частичный
    результат.
    """
    model_id = str(message.get("id", ""))
    sequence = message.get("sequence")
    if not model_id:
        return

    raw_pivot = message.get("pivot")
    clean_pivot = _extract_transform_vectors(raw_pivot)
    raw_descendants = message.get("descendants", {})

    async with state_lock:
        model_instance = world.get(model_id)

        if model_instance is None:
            current = None
            reason = "Model does not exist."
        elif model_instance.class_name != "Model":
            current = None
            reason = "Target is not a Model."
        elif clean_pivot is None:
            current = _snapshot_model_transform_state(model_id)
            reason = "Malformed or missing pivot transform."
        elif not isinstance(raw_descendants, dict):
            current = _snapshot_model_transform_state(model_id)
            reason = "Malformed descendants payload."
        else:
            expected_ids = _expected_transform_descendant_ids(model_id)
            submitted_ids = {str(k) for k in raw_descendants.keys()}
            reason = None
            current = None

            if submitted_ids != expected_ids:
                missing = expected_ids - submitted_ids
                extra = submitted_ids - expected_ids
                reason = (
                    "Hierarchy changed during drag (descendant set mismatch): "
                    f"missing={sorted(missing)} extra={sorted(extra)}"
                )
                current = _snapshot_model_transform_state(model_id)
            else:
                clean_descendants: dict[str, dict[str, list[float]]] = {}
                for descendant_id in submitted_ids:
                    descendant_instance = world.get(descendant_id)
                    if descendant_instance is None or descendant_instance.class_name not in _MODEL_TRANSFORM_DESCENDANT_TYPES:
                        reason = f"'{descendant_id}' is not a transformable object."
                        current = _snapshot_model_transform_state(model_id)
                        break
                    clean_transform = _extract_transform_vectors(raw_descendants[descendant_id])
                    if clean_transform is None:
                        reason = f"Malformed transform for descendant '{descendant_id}'."
                        current = _snapshot_model_transform_state(model_id)
                        break
                    clean_descendants[descendant_id] = clean_transform

                if reason is None:
                    pivot_properties = dict(clean_pivot)
                    pivot_properties["PivotPosition"] = pivot_properties.pop(
                        "Position", model_instance.get_property("PivotPosition"),
                    )
                    pivot_properties["PivotRotation"] = pivot_properties.pop(
                        "Rotation", model_instance.get_property("PivotRotation"),
                    )
                    pivot_properties["PivotIsExplicit"] = True
                    sanitized_pivot = object_registry.sanitize_properties_for_type("Model", pivot_properties)
                    model_instance.properties.update(sanitized_pivot)

                    applied_descendants: dict[str, dict[str, list[float]]] = {}
                    for descendant_id, clean_transform in clean_descendants.items():
                        descendant_instance = world.get(descendant_id)
                        if descendant_instance is None:
                            continue
                        if descendant_instance.class_name == "Model":
                            model_transform: dict[str, Any] = {}
                            if "Position" in clean_transform:
                                model_transform["PivotPosition"] = clean_transform["Position"]
                            if "Rotation" in clean_transform:
                                model_transform["PivotRotation"] = clean_transform["Rotation"]
                            model_transform["PivotIsExplicit"] = True
                            sanitized = object_registry.sanitize_properties_for_type("Model", model_transform)
                        else:
                            sanitized = sanitize_part_properties(clean_transform)
                        descendant_instance.properties.update(sanitized)
                        applied_descendants[descendant_id] = clean_transform

    if reason is not None:
        await _reject_transform_model(player_id, model_id, reason, sequence, current)
        return

    logging.info(
        "Игрок %s: transform_model %s (%s потомков)",
        player_id,
        model_id,
        len(applied_descendants),
    )

    await broadcast_to_all(
        {
            "type": protocol.MODEL_TRANSFORMED,
            "id": model_id,
            "pivot": clean_pivot,
            "descendants": applied_descendants,
            "sequence": sequence,
        }
    )


# ============================================================
# Stage 3.2: REPLACE_WORLD (Create Place from template / Open Place)
# ============================================================

# Security policy (see Stage 3.2 spec §9): the server has no filesystem
# access of its own to guard -- all Place file I/O happens client-side in
# place_manager.py, on the local user's own machine. The one thing that
# DOES need a guard is REPLACE_WORLD itself, since it is a generic
# client->server message like any other and this protocol has no per-
# connection auth/role concept at all today (any socket can already send
# CREATE_PART unauthenticated). For this stage, REPLACE_WORLD is honored
# only from a loopback connection -- "the current client is explicitly the
# local host", one of the three explicitly acceptable policies in the spec.
# A non-local connection gets a clean rejection, never a partial/silent one.
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}

MAX_REPLACE_WORLD_OBJECTS = 20000


def _is_local_connection(player_id: str) -> bool:
    websocket = clients.get(player_id)
    if websocket is None:
        return False
    try:
        host = websocket.remote_address[0]
    except (TypeError, IndexError, AttributeError):
        return False
    return host in _LOCAL_HOSTS


async def _send_replace_world_result(
    player_id: str, request_id: str, success: bool, message: str,
) -> None:
    async with state_lock:
        websocket = clients.get(player_id)
    if websocket is not None:
        await send_json(
            websocket,
            {
                "type": protocol.REPLACE_WORLD_RESULT,
                "request_id": request_id,
                "success": success,
                "message": message,
            },
        )


def _sanitize_replace_world_object(raw: Any) -> Instance | None:
    """Mirrors handle_create_part's own sanitization exactly -- a Place
    file is untrusted input the same way a network message is, whether it
    came from a template or a hand-edited/corrupted file on disk."""
    if not isinstance(raw, dict):
        return None

    class_name = str(raw.get("class_name") or "")
    definition = object_registry.get_object_type(class_name)
    if definition is None:
        return None

    raw_properties = raw.get("properties", {})
    if not isinstance(raw_properties, dict):
        raw_properties = {}

    if class_name in PART_LIKE_TYPES:
        clean_properties = sanitize_part_properties(raw_properties)
        base_defaults = DEFAULT_PART_PROPERTIES if class_name == "Part" else definition.default_properties
    else:
        clean_properties = object_registry.sanitize_properties_for_type(class_name, raw_properties)
        base_defaults = definition.default_properties

    properties = dict(base_defaults)
    properties.update(clean_properties)

    instance_id = str(raw.get("id") or "").strip()
    if not instance_id:
        return None

    name = str(raw.get("name", "")).strip()[:32] or definition.display_name

    raw_parent_id = raw.get("parent_id")
    parent_id = str(raw_parent_id) if isinstance(raw_parent_id, str) and raw_parent_id else None

    raw_tags = raw.get("tags", [])
    tags = [str(t) for t in raw_tags if isinstance(t, str)][:32] if isinstance(raw_tags, list) else []
    raw_attributes = raw.get("attributes", {})
    attributes = dict(raw_attributes) if isinstance(raw_attributes, dict) else {}

    return Instance(
        id=instance_id,
        class_name=class_name,
        name=name,
        parent_id=parent_id,
        properties=properties,
        tags=tags,
        attributes=attributes,
        enabled=bool(raw.get("enabled", True)),
    )


def _singleton_conflict(
    world_snapshot: dict[str, Instance],
    class_name: str,
    name: str,
    parent_id: Any,
    exclude_id: str | None = None,
) -> str | None:
    """Stage 3.8: authoritative singleton enforcement for
    StarterPlayerScripts (at most one anywhere) and the StarterCharacter
    special-Model role (at most one exact match under StarterPlayer) --
    called from every path that could introduce a duplicate: create,
    rename, reparent, and REPLACE_WORLD. Returns a rejection reason, or
    None if (class_name, name, parent_id) is fine. `exclude_id` lets a
    rename/reparent of an EXISTING singleton check against every OTHER
    instance without tripping over itself."""
    parent_key = parent_id or "Workspace"
    if class_name == "StarterPlayerScripts":
        for other in world_snapshot.values():
            if other.id == exclude_id:
                continue
            if other.class_name == "StarterPlayerScripts":
                return "Only one StarterPlayerScripts is allowed."
    if datamodel_schema.is_starter_character(class_name, name, parent_key):
        for other in world_snapshot.values():
            if other.id == exclude_id:
                continue
            other_parent = other.parent_id or "Workspace"
            if datamodel_schema.is_starter_character(other.class_name, other.name, other_parent):
                return "Only one StarterCharacter is allowed under StarterPlayer."
    return None


def _hierarchy_is_valid(instances: dict[str, Instance]) -> bool:
    for instance in instances.values():
        parent_key = instance.parent_id or "Workspace"
        if parent_key not in object_registry.ROOT_SERVICES and parent_key not in instances:
            return False
        visited = {instance.id}
        walker = parent_key
        while walker in instances:
            if walker in visited:
                return False
            visited.add(walker)
            walker = instances[walker].parent_id or "Workspace"
    return True


def _replace_world_singleton_conflict(new_world: dict[str, Instance]) -> str | None:
    starter_scripts_count = sum(1 for i in new_world.values() if i.class_name == "StarterPlayerScripts")
    if starter_scripts_count > 1:
        return "Rejected: more than one StarterPlayerScripts in Place data."
    starter_character_count = sum(
        1 for i in new_world.values()
        if datamodel_schema.is_starter_character(i.class_name, i.name, i.parent_id or "Workspace")
    )
    if starter_character_count > 1:
        return "Rejected: more than one StarterCharacter under StarterPlayer in Place data."
    return None


async def handle_replace_world(player_id: str, message: dict[str, Any]) -> None:
    """Create Place from template / Open Place: one atomic all-or-nothing
    swap of the entire authoritative world. Never partially applied -- any
    rejection leaves `world` completely untouched (see Stage 3.2 spec §8,
    §17)."""
    request_id = str(message.get("request_id") or "")

    if not _is_local_connection(player_id):
        logging.warning(
            "Игрок %s (не localhost) запросил replace_world — отклонено", player_id,
        )
        await _send_replace_world_result(
            player_id, request_id, False,
            "Place file operations are only available from a local connection.",
        )
        return

    raw_objects = message.get("objects")
    if not request_id or not isinstance(raw_objects, list):
        await _send_replace_world_result(player_id, request_id, False, "Malformed replace_world request.")
        return

    if len(raw_objects) > MAX_REPLACE_WORLD_OBJECTS:
        await _send_replace_world_result(player_id, request_id, False, "Place is too large.")
        return

    new_world: dict[str, Instance] = {}
    for raw in raw_objects:
        instance = _sanitize_replace_world_object(raw)
        if instance is None:
            await _send_replace_world_result(
                player_id, request_id, False, "Rejected: invalid or unrecognized object in Place data.",
            )
            return
        if instance.id in new_world:
            await _send_replace_world_result(
                player_id, request_id, False, "Rejected: duplicate instance id in Place data.",
            )
            return
        new_world[instance.id] = instance

    if not _hierarchy_is_valid(new_world):
        await _send_replace_world_result(
            player_id, request_id, False, "Rejected: Place data has a missing or cyclic parent chain.",
        )
        return

    singleton_conflict = _replace_world_singleton_conflict(new_world)
    if singleton_conflict is not None:
        await _send_replace_world_result(player_id, request_id, False, singleton_conflict)
        return

    # Stage 3.8: "services" is optional (an older/offline client, or a
    # version<3 Place with no services key at all -- see place_manager.py)
    # -- when omitted, the persistent service state (Gravity,
    # StarterPlayer.*, ...) is left exactly as it already was rather than
    # silently reset to defaults, matching every other REPLACE_WORLD field
    # this handler treats as "absent means unchanged" nowhere else applies,
    # but this one setting genuinely differs per Place, so ANY REPLACE_WORLD
    # that DOES include "services" (every current client always does, see
    # client_studio.py's request_replace_world) fully replaces it.
    raw_services = message.get("services")
    new_services = datamodel_schema.sanitize_services_snapshot(raw_services) if isinstance(raw_services, dict) else None

    async with state_lock:
        world.clear()
        world.update(new_world)
        if new_services is not None:
            services.clear()
            services.update(new_services)
        snapshot = serialize_world(world)
        services_snapshot = {name: dict(props) for name, props in services.items()}

    logging.info(
        "Игрок %s заменил мир целиком (%d объектов)", player_id, len(new_world),
    )

    # Broadcast first (including to the requester) so the ordinary
    # WORLD_SNAPSHOT handling -- which already clears history and rebuilds
    # the scene, see Stage 3.2 baseline report question 6 -- has run by the
    # time the requester's UI acts on the result below.
    await broadcast_to_all({
        "type": protocol.WORLD_SNAPSHOT, "parts": snapshot, "services": services_snapshot,
    })
    await _send_replace_world_result(player_id, request_id, True, "World replaced.")


async def client_handler(
    websocket: ServerConnection,
) -> None:
    player_id = secrets.token_hex(4)

    async with state_lock:
        clients[player_id] = websocket

        players[player_id] = create_player(
            player_id
        )

        initial_player = dict(
            players[player_id]
        )

    logging.info(
        "Игрок %s подключился: %s",
        player_id,
        websocket.remote_address,
    )

    connected = await send_json(
        websocket,
        {
            "type": "connected",
            "player_id": player_id,
            "player": initial_player,
        },
    )

    if not connected:
        async with state_lock:
            clients.pop(
                player_id,
                None,
            )

            players.pop(
                player_id,
                None,
            )

        return

    async with state_lock:
        world_snapshot = serialize_world(world)
        services_snapshot = {name: dict(props) for name, props in services.items()}

    await send_json(
        websocket,
        {
            "type": protocol.WORLD_SNAPSHOT,
            "parts": world_snapshot,
            "services": services_snapshot,
        },
    )

    try:
        async for raw_message in websocket:
            try:
                message = json.loads(
                    raw_message
                )

            except json.JSONDecodeError:
                logging.warning(
                    "Игрок %s прислал неправильный JSON",
                    player_id,
                )
                continue

            if not isinstance(
                message,
                dict,
            ):
                continue

            await handle_message(
                player_id,
                message,
            )

    except ConnectionClosed:
        pass

    except Exception:
        logging.exception(
            "Ошибка соединения с игроком %s",
            player_id,
        )

    finally:
        async with state_lock:
            clients.pop(
                player_id,
                None,
            )

            players.pop(
                player_id,
                None,
            )

        logging.info(
            "Игрок %s отключился",
            player_id,
        )


async def main() -> None:
    logging.info(
        "Запуск сервера на ws://%s:%s",
        HOST,
        PORT,
    )

    logging.info(
        "Для локального теста используй "
        "ws://127.0.0.1:%s",
        PORT,
    )

    async with serve(
        client_handler,
        HOST,
        PORT,
        ping_interval=20,
        ping_timeout=20,
        max_size=64 * 1024,
    ):
        broadcast_task = asyncio.create_task(
            broadcast_world_state()
        )

        try:
            await asyncio.Future()

        finally:
            broadcast_task.cancel()

            try:
                await broadcast_task

            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        logging.info(
            "Сервер остановлен"
        )   