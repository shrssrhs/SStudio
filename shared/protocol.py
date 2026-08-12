"""
Типы сетевых сообщений. Держим в одном месте, чтобы клиент и сервер не
разъезжались в названиях (раньше "transform"/"world_state" были просто
строками в двух разных файлах — с ростом протокола это станет источником
опечаток).
"""

# Существующие (transform-протокол игроков)
TRANSFORM = "transform"
SET_NAME = "set_name"
CONNECTED = "connected"
WORLD_STATE = "world_state"          # позиции игроков, как сейчас
CONNECTION_STATUS = "connection_status"
NETWORK_ERROR = "network_error"

# Новые (world-building протокол)
CREATE_PART = "create_part"           # клиент -> сервер: запрос на создание
DELETE_PART = "delete_part"           # клиент -> сервер: запрос на удаление
UPDATE_PROPERTY = "update_property"   # клиент -> сервер: изменить свойство
WORLD_SNAPSHOT = "world_snapshot"     # сервер -> клиент: весь мир целиком
                                       # (при подключении)
PART_CREATED = "part_created"         # сервер -> все клиенты: часть появилась
PART_DELETED = "part_deleted"         # сервер -> все клиенты: часть удалена
PART_UPDATED = "part_updated"         # сервер -> все клиенты: свойство поменялось
                                       # (включая необязательное поле parent_id
                                       # при успешном реродителении)

# Stage 2.1: явный reparenting-запрос — намеренно НЕ часть update_property:
# требует валидации циклов/allowed_parent_types на сервере, а не просто
# санитайза произвольных properties (клиент не может обойти правила, послав
# parent_id через generic update_property — сервер его там не читает).
SET_PARENT = "set_parent"                     # клиент -> сервер: запрос на перенос
SET_PARENT_REJECTED = "set_parent_rejected"   # сервер -> только запросившему клиенту

# Stage 2.2: Model pivot + cascading descendant transforms. Один Model-drag
# двигает/вращает Model pivot И произвольное число потомков одновременно —
# генерик UPDATE_PROPERTY (один id за раз) не может передать это как один
# атомарный кадр, а рассылка потомков по отдельности рискует показать
# другим клиентам "разваленную" на середине кадра композицию. Поэтому
# отдельная batch-операция вместо перегрузки update_property.
TRANSFORM_MODEL = "transform_model"           # клиент -> сервер: пивот + дельты потомков
MODEL_TRANSFORMED = "model_transformed"       # сервер -> все клиенты: атомарный батч
TRANSFORM_MODEL_REJECTED = "transform_model_rejected"  # сервер -> только запросившему клиенту

# Stage 3.2: замена всего мира целиком (Create-from-template / Open Place).
# Один explicit atomic-replace вместо сотен create_part/update_property —
# см. Stage 3.2 spec §8. Клиент уже полностью санитизирует и ремаппит id
# локально (place_manager.py) до отправки; сервер повторно санитизирует
# каждый Instance той же схемой, что использует CREATE_PART/UPDATE_PROPERTY
# (доверяй, но проверяй), и либо целиком принимает батч, либо целиком
# отклоняет — никогда частичной заменой. Успех рассылается как обычный
# WORLD_SNAPSHOT всем клиентам (существующий обработчик уже корректно
# чистит историю и перестраивает сцену — см. Stage 3.2 отчёт, вопрос 6);
# REPLACE_WORLD_RESULT идёт только запросившему клиенту, чтобы он знал,
# применять ли путь/заголовок Place или показать ошибку, не трогая
# текущее состояние.
REPLACE_WORLD = "replace_world"                       # клиент -> сервер
REPLACE_WORLD_RESULT = "replace_world_result"         # сервер -> только запросившему клиенту

# Stage 3.8: root-service persistent property edits (Workspace.Gravity,
# StarterPlayer.CharacterWalkSpeed, ...). Root services are not Instances
# (see shared/object_registry.ROOT_SERVICES) so they cannot go through
# UPDATE_PROPERTY/PART_UPDATED, which are keyed by instance id and looked
# up in the server's `world` dict -- this is the same request/broadcast
# shape as that pair, just keyed by service name against the server's
# separate `services` dict. No reject path (same reasoning as
# UPDATE_PROPERTY -- see editor_history.py's module docstring): the
# server sanitizes via datamodel_schema and always echoes back whatever
# it actually stored.
UPDATE_SERVICE_PROPERTY = "update_service_property"           # клиент -> сервер
SERVICE_PROPERTY_UPDATED = "service_property_updated"         # сервер -> все клиенты