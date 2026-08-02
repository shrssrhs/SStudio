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