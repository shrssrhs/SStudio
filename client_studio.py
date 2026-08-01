from __future__ import annotations

import argparse
import asyncio
import builtins
import json
import math
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any

from panda3d.core import Filename, TransparencyAttrib
from ursina import (
    AmbientLight,
    Cone,
    DirectionalLight,
    Entity,
    Grid,
    Text,
    Ursina,
    Vec3,
    application,
    camera,
    color,
    destroy,
    held_keys,
    lerp,
    mouse,
    time as ursina_time,
    window,
)
from ursina.shaders import unlit_shader
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from PySide6.QtCore import QEvent, QObject, QPoint, QTimer, Qt
from PySide6.QtGui import QCursor, QWindow
from PySide6.QtWidgets import QApplication, QWidget

from studio_editor_live import (
    DARK_STYLE,
    EngineBridge,
    SceneObject,
    StudioMainWindow,
    Vec3 as EditorVec3,
)
from shared import object_registry, protocol
from shared.instance import DEFAULT_PART_PROPERTIES
from shared.object_registry import ROOT_SERVICES
from transform_gizmo import (
    DEFAULT_MOVE_SNAP,
    TransformGizmo,
    mouse_world_ray,
)

try:
    # simplepbr подставляет корректный PBR-шейдер (metallic/roughness,
    # normal maps) на всю сцену. Без него glTF-материалы рендерятся
    # Panda3D через старый Blinn-Phong auto-shader, который не понимает
    # PBR-параметры — из-за этого модель выходит сплошным чёрным силуэтом.
    import simplepbr
    SIMPLEPBR_AVAILABLE = True
except ImportError:
    simplepbr = None
    SIMPLEPBR_AVAILABLE = False


# ============================================================
# ПУТИ
# ============================================================

# Внутри собранного PyInstaller-экзешника __file__ не указывает на реальную
# папку проекта — данные (assets/, бандл ursina/panda3d) распакованы в
# sys._MEIPASS (onefile) или лежат рядом с exe (onedir, тоже sys._MEIPASS).
# В обычном запуске `python client_studio.py` sys.frozen не установлен,
# так что этот код не меняет поведение при разработке.
if getattr(sys, "frozen", False):
    PROJECT_DIR = Path(sys._MEIPASS)  # type: ignore[attr-defined]
else:
    PROJECT_DIR = Path(__file__).resolve().parent
ASSETS_DIR = PROJECT_DIR / "assets"
PLAYER_MODEL_PATH = ASSETS_DIR / "player.glb"

ASSETS_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# НАСТРОЙКИ СЕТИ
# ============================================================

# ВАЖНО ДЛЯ ТЕСТА С ДРУЗЬЯМИ:
# Это дефолтный адрес, на который клиент подключится, если не передан
# флаг --server. Перед сборкой .exe пропиши сюда публичный адрес
# (например ngrok tcp-туннель вида "ws://0.tcp.ngrok.io:12345") —
# тогда друзьям не нужно ничего вводить, просто запустить exe.
DEFAULT_SERVER_URL = "ws://127.0.0.1:8765"

SEND_RATE = 20
RECONNECT_DELAY = 2.0

# Частота отправки UPDATE_PROPERTY во время drag transform gizmo.
# Локально Entity/Inspector обновляются каждый кадр — в сеть уходит
# throttled, чтобы не слать сообщение на каждый микроскопический пиксель.
GIZMO_NETWORK_SEND_RATE = 25.0


# ============================================================
# УПРАВЛЕНИЕ И ПОЛЁТ
# ============================================================

FLIGHT_SPEED = 8.0
BOOST_MULTIPLIER = 2.5

MOUSE_SENSITIVITY_X = 55.0
MOUSE_SENSITIVITY_Y = 55.0

# Вертикальный обзор ограничен. Камера не сможет перевернуться.
MIN_PITCH = -80.0
MAX_PITCH = 80.0

# ------------------------------------------------------------
# ВСТРОЕННЫЙ VIEWPORT: свой relative mouse look через Qt.
#
# Ursina реализует mouse.locked через связку window.M_confined +
# Mouse.position-setter, который каждый кадр телепортирует курсор в
# центр НАТИВНОГО окна Panda3D (base.win.move_pointer), а на следующем
# кадре читает смещение курсора как mouse.velocity. Этот трюк верен
# только когда Panda3D сам знает экранные координаты своего окна.
#
# После QWindow.fromWinId(...) + QWidget.createWindowContainer(...)
# окно Panda3D — это дочернее окно внутри Qt, чью позицию/размер
# целиком считает Qt layout. Внутренняя бухгалтерия Panda3D о своём
# положении на экране после встраивания не совпадает с реальностью
# (особенно на macOS, где обёрнутый чужой handle обычно вообще не
# полноценный NSWindow) — move_pointer телепортирует курсор не туда,
# get_pointer() на следующем кадре читает уже испорченное смещение, и
# получается постоянная ненулевая "скорость" каждый кадр даже без
# движения мыши. Это и вызывает мгновенный разворот камеры вверх и
# непрерывное вращение вправо, пока зажата ПКМ.
#
# Поэтому пока viewport встроен в Qt, mouse.locked/mouse.velocity для
# взгляда камеры вообще не используются. Дельта считается вручную:
# QCursor.pos() опрашивается каждый кадр (глобальные экранные
# координаты, которые Qt всегда знает верно), курсор принудительно
# возвращается в центр контейнера через QCursor.setPos(...) с позицией
# самого контейнера — Qt её тоже знает верно, в отличие от Panda3D.
# В режиме --no-embed Panda3D владеет настоящим top-level окном и
# старый mouse.locked-трюк там работает штатно, поэтому используется
# он же (см. MultiplayerGame._qt_look_available).
# ------------------------------------------------------------

DEBUG_MOUSE_LOOK = False

QT_LOOK_SENSITIVITY_X = 0.08
QT_LOOK_SENSITIVITY_Y = 0.08

# Высота камеры относительно сетевой позиции игрока.
EYE_HEIGHT = 0.65

# Смещение камеры назад в режиме от третьего лица (для отладки модели).
THIRD_PERSON_DISTANCE = 4.0
THIRD_PERSON_HEIGHT = 0.4


# ============================================================
# МОДЕЛЬ ДРУГИХ ИГРОКОВ
# ============================================================

# Поворот применяется только к настоящей GLB-модели.
# Начни с нулей: корректно экспортированный GLB обычно уже имеет нужные оси.
# Если модель лежит боком, меняй только эти три значения.
MODEL_ROTATION_X = 0.0
MODEL_ROTATION_Y = 90.0
MODEL_ROTATION_Z = 0.0

# При AUTO_FIT_MODEL = True модель автоматически масштабируется по высоте.
AUTO_FIT_MODEL = True
MODEL_TARGET_HEIGHT = 2.0

# Используется только при AUTO_FIT_MODEL = False.
MODEL_SCALE = 1.0

# Дополнительное смещение после автоматического центрирования.
MODEL_OFFSET = Vec3(0, 0, 0)

PLAYER_NAME_HEIGHT = 2.35

AIM_ARROW_HEIGHT = 0.65
AIM_ARROW_START = 1.15
AIM_ARROW_LENGTH = 1.5


# ============================================================
# СГЛАЖИВАНИЕ УДАЛЁННЫХ ИГРОКОВ
# ============================================================

REMOTE_POSITION_SMOOTHNESS = 12.0
REMOTE_ROTATION_SMOOTHNESS = 12.0


# ============================================================
# СТРОИТЕЛЬСТВО (Part spawning)
# ============================================================

# Дистанция перед камерой, на которой появляется новая часть по нажатию E.
PART_SPAWN_DISTANCE = 4.0
PART_DEFAULT_SIZE = [2.0, 2.0, 2.0]
PART_DEFAULT_COLOR = [200, 90, 90]


# ============================================================
# МИР И ЦВЕТА
# ============================================================

SKY_COLOR = color.rgb32(70, 145, 225)
GROUND_COLOR = color.rgb32(92, 112, 88)
BLOCK_COLOR = color.rgb32(92, 101, 111)

# Второй режим фона: тёмно-серый "как в Blender" вместо неба, плюс
# яркая серая сетка на полу вместо травяного грунта.
GRID_BACKGROUND_COLOR = color.rgb32(45, 45, 48)
GRID_GROUND_COLOR = color.rgb32(58, 58, 61)
GRID_LINE_COLOR = color.rgba32(150, 150, 158, 255)
GRID_LINE_COUNT = 60
GRID_LINE_SPACING = 2.0

PLACEHOLDER_BODY_COLOR = color.rgb32(30, 130, 240)
PLACEHOLDER_HEAD_COLOR = color.rgb32(195, 195, 195)
PLACEHOLDER_FRONT_COLOR = color.rgb32(25, 25, 25)

ARROW_COLOR = color.rgba32(20, 255, 120, 190)
ARROW_TIP_COLOR = color.rgba32(20, 255, 120, 235)

AMBIENT_LIGHT_COLOR = color.rgba32(125, 125, 145, 255)
SUN_LIGHT_COLOR = color.rgba32(235, 225, 205, 255)


# ============================================================
# СЕТЕВОЙ КЛИЕНТ
# ============================================================

class NetworkClient:
    def __init__(self, server_url: str, player_name: str) -> None:
        self.server_url = server_url
        self.player_name = player_name

        self.incoming_messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.outgoing_messages: queue.Queue[dict[str, Any]] = queue.Queue()

        self.connected_event = threading.Event()
        self.stop_event = threading.Event()

        self.thread = threading.Thread(
            target=self._thread_main,
            daemon=True,
            name="network-thread",
        )

    def start(self) -> None:
        if not self.thread.is_alive():
            self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def send(self, message: dict[str, Any]) -> None:
        if not self.stop_event.is_set():
            self.outgoing_messages.put(message)

    def receive_all(self) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []

        while True:
            try:
                messages.append(self.incoming_messages.get_nowait())
            except queue.Empty:
                return messages

    def _push_status(self, text: str) -> None:
        self.incoming_messages.put(
            {
                "type": "connection_status",
                "message": text,
            }
        )

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._network_loop())
        except Exception as error:
            self.incoming_messages.put(
                {
                    "type": "network_error",
                    "message": f"{type(error).__name__}: {error}",
                }
            )

    async def _network_loop(self) -> None:
        while not self.stop_event.is_set():
            self._push_status(f"Подключение к {self.server_url}")

            try:
                async with connect(
                    self.server_url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=3,
                    max_size=256 * 1024,
                ) as websocket:
                    self.connected_event.set()
                    self._push_status("Подключено")

                    await websocket.send(
                        json.dumps(
                            {
                                "type": "set_name",
                                "name": self.player_name,
                            }
                        )
                    )

                    sender_task = asyncio.create_task(
                        self._sender_loop(websocket)
                    )
                    receiver_task = asyncio.create_task(
                        self._receiver_loop(websocket)
                    )

                    done, pending = await asyncio.wait(
                        {sender_task, receiver_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    for task in pending:
                        task.cancel()

                    await asyncio.gather(*pending, return_exceptions=True)

                    for task in done:
                        try:
                            task.result()
                        except ConnectionClosed:
                            pass

            except ConnectionClosed:
                self._push_status("Соединение закрыто")

            except OSError as error:
                self._push_status(f"Сервер недоступен: {error}")

            except Exception as error:
                self.incoming_messages.put(
                    {
                        "type": "network_error",
                        "message": f"{type(error).__name__}: {error}",
                    }
                )

            finally:
                self.connected_event.clear()

            if not self.stop_event.is_set():
                await asyncio.sleep(RECONNECT_DELAY)

    async def _sender_loop(self, websocket: Any) -> None:
        while not self.stop_event.is_set():
            try:
                message = self.outgoing_messages.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.005)
                continue

            await websocket.send(json.dumps(message))

    async def _receiver_loop(self, websocket: Any) -> None:
        async for raw_message in websocket:
            try:
                message = json.loads(raw_message)
            except json.JSONDecodeError:
                continue

            if isinstance(message, dict):
                self.incoming_messages.put(message)


# ============================================================
# МАТЕМАТИКА
# ============================================================

def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def wrap_angle(angle: float) -> float:
    return (angle + 180.0) % 360.0 - 180.0


def shortest_angle_difference(
    current_angle: float,
    target_angle: float,
) -> float:
    return (target_angle - current_angle + 180.0) % 360.0 - 180.0


def forward_from_angles(yaw_degrees: float, pitch_degrees: float) -> Vec3:
    yaw = math.radians(yaw_degrees)
    pitch = math.radians(pitch_degrees)

    direction = Vec3(
        math.sin(yaw) * math.cos(pitch),
        math.sin(pitch),
        math.cos(yaw) * math.cos(pitch),
    )

    if direction.length() <= 0:
        return Vec3(0, 0, 1)

    return direction.normalized()


def right_from_yaw(yaw_degrees: float) -> Vec3:
    yaw = math.radians(yaw_degrees)

    direction = Vec3(
        math.cos(yaw),
        0,
        -math.sin(yaw),
    )

    if direction.length() <= 0:
        return Vec3(1, 0, 0)

    return direction.normalized()


# ============================================================
# ЗАГРУЗКА И ПОДГОТОВКА GLB-МОДЕЛИ
# ============================================================

def model_file_status() -> tuple[bool, str]:
    if not PLAYER_MODEL_PATH.exists():
        return False, f"Файл не найден: {PLAYER_MODEL_PATH}"

    if not PLAYER_MODEL_PATH.is_file():
        return False, f"Путь не является файлом: {PLAYER_MODEL_PATH}"

    if PLAYER_MODEL_PATH.stat().st_size <= 0:
        return False, f"Файл пустой: {PLAYER_MODEL_PATH}"

    return True, f"Файл найден: {PLAYER_MODEL_PATH.name}"


def load_fresh_model_node() -> tuple[Any | None, str]:
    file_ok, file_message = model_file_status()

    if not file_ok:
        return None, file_message

    panda_loader = getattr(builtins, "loader", None)

    if panda_loader is None:
        return None, "Panda3D loader ещё не создан"

    try:
        # ИСПРАВЛЕНИЕ: Panda3D работает через свою виртуальную файловую
        # систему (VFS), где диски Windows маппятся как /c/... вместо
        # C:/... . Простой .as_posix() меняет только слеши, но не решает
        # эту проблему — путь вида "C:/Users/..." Panda не распознаёт
        # как абсолютный и начинает искать файл по model-path, где его
        # нет ("not found on model path"), хотя на диске файл есть.
        # Filename.from_os_specific() делает правильную конвертацию.
        panda_filename = Filename.from_os_specific(str(PLAYER_MODEL_PATH))

        # Прямой Panda3D loader не использует кэш копий Ursina,
        # который может сбрасывать текстуры у повторно загруженной модели.
        model_node = panda_loader.loadModel(panda_filename)
    except Exception as error:
        return None, f"Ошибка загрузки GLB: {type(error).__name__}: {error}"

    if model_node is None:
        return None, "Panda3D loader вернул None"

    try:
        if model_node.isEmpty():
            return None, "Panda3D загрузил пустой NodePath"
    except AttributeError:
        pass

    # ------------------------------------------------------------
    # Правильный PBR-шейдер для glTF ставится один раз глобально через
    # simplepbr.init() в main() — НЕ здесь per-node через setShaderAuto().
    # setShaderAuto() — это старый Blinn-Phong auto-shader, он не умеет
    # metallic/roughness-материалы glTF и рисует их чёрными.
    # Тут только отключаем случайную прозрачность из материала (частый
    # косяк экспорта из Blender с alpha=0 или BLEND-режимом) и бэкфейсы.
    # ------------------------------------------------------------
    try:
        model_node.setTransparency(TransparencyAttrib.MNone)
        model_node.setTwoSided(True)
    except Exception:
        pass

    return model_node, file_message


def fit_model_to_height(model_node: Any, target_height: float) -> str:
    if not AUTO_FIT_MODEL:
        model_node.setScale(MODEL_SCALE)
        model_node.setPos(MODEL_OFFSET.x, MODEL_OFFSET.y, MODEL_OFFSET.z)
        return f"ручной scale={MODEL_SCALE}"

    try:
        minimum, maximum = model_node.getTightBounds()
    except Exception as error:
        model_node.setScale(MODEL_SCALE)
        model_node.setPos(MODEL_OFFSET.x, MODEL_OFFSET.y, MODEL_OFFSET.z)
        return f"bounds недоступны ({error}); scale={MODEL_SCALE}"

    if minimum is None or maximum is None:
        model_node.setScale(MODEL_SCALE)
        model_node.setPos(MODEL_OFFSET.x, MODEL_OFFSET.y, MODEL_OFFSET.z)
        return f"пустые bounds; scale={MODEL_SCALE}"

    size_x = float(maximum.x - minimum.x)
    size_y = float(maximum.y - minimum.y)
    size_z = float(maximum.z - minimum.z)

    model_height = max(size_z, size_y, size_x)

    if model_height <= 0.00001:
        model_node.setScale(MODEL_SCALE)
        model_node.setPos(MODEL_OFFSET.x, MODEL_OFFSET.y, MODEL_OFFSET.z)
        return f"нулевой размер; scale={MODEL_SCALE}"

    scale_factor = target_height / model_height
    model_node.setScale(scale_factor)

    # После масштаба получаем bounds ещё раз и ставим нижнюю точку на y=0.
    minimum_scaled, maximum_scaled = model_node.getTightBounds()

    center_x = (minimum_scaled.x + maximum_scaled.x) / 2
    center_z = (minimum_scaled.z + maximum_scaled.z) / 2
    bottom_y = minimum_scaled.y

    model_node.setPos(
        -center_x + MODEL_OFFSET.x,
        -bottom_y + MODEL_OFFSET.y,
        -center_z + MODEL_OFFSET.z,
    )

    return (
        f"auto-fit scale={scale_factor:.4f}; "
        f"bounds=({size_x:.3f}, {size_y:.3f}, {size_z:.3f})"
    )


# ============================================================
# ВИЗУАЛЬНАЯ МОДЕЛЬ ИГРОКА (используется и для удалённых, и для себя)
# ============================================================

class PlayerVisual(Entity):
    def __init__(self, player_name: str, show_name_label: bool = True) -> None:
        super().__init__()

        self.player_name = player_name
        self.using_custom_model = False
        self.model_status = ""

        # ------------------------------------------------------------
        # ВАЖНО: aim_pivot создаётся ПЕРВЫМ, а model_anchor становится
        # его дочерним объектом (не соседом, как раньше). Это и есть
        # "сварка" тела со стрелкой прицела — как WeldConstraint в
        # Roblox: единая иерархия, единый поворот по pitch.
        # model_anchor смещён на -AIM_ARROW_HEIGHT, чтобы скомпенсировать
        # позицию aim_pivot и модель осталась на том же месте при pitch=0.
        # ------------------------------------------------------------
        self.aim_pivot = Entity(
            parent=self,
            position=(0, AIM_ARROW_HEIGHT, 0),
        )

        self.model_anchor = Entity(
            parent=self.aim_pivot,
            position=(0, -AIM_ARROW_HEIGHT, 0),
            rotation=(0, 0, 0),
        )

        model_node, load_message = load_fresh_model_node()

        if model_node is not None:
            self.model_anchor.rotation = Vec3(
                MODEL_ROTATION_X,
                MODEL_ROTATION_Y,
                MODEL_ROTATION_Z,
            )
            model_node.reparentTo(self.model_anchor)
            fit_message = fit_model_to_height(model_node, MODEL_TARGET_HEIGHT)

            self.model_node = model_node
            self.using_custom_model = True
            self.model_status = f"{load_message}; {fit_message}"

            print(f"[MODEL] {self.model_status}")
        else:
            self.model_status = load_message
            print(f"[MODEL] {load_message}")
            print("[MODEL] Используется плейсхолдер")

            self.placeholder_body = Entity(
                parent=self.model_anchor,
                model="cube",
                color=PLACEHOLDER_BODY_COLOR,
                scale=(0.9, 1.6, 0.55),
                y=0.8,
            )

            Entity(
                parent=self.model_anchor,
                model="sphere",
                color=PLACEHOLDER_HEAD_COLOR,
                scale=(0.65, 0.65, 0.65),
                y=1.9,
            )

            Entity(
                parent=self.model_anchor,
                model="cube",
                color=PLACEHOLDER_FRONT_COLOR,
                scale=(0.22, 0.22, 0.08),
                position=(0, 1.05, 0.32),
            )

        self.arrow_root = Entity(
            parent=self.aim_pivot,
            position=(0, 0, AIM_ARROW_START),
        )

        Entity(
            parent=self.arrow_root,
            model="cube",
            color=ARROW_COLOR,
            scale=(0.10, 0.10, AIM_ARROW_LENGTH),
        )

        # ИСПРАВЛЕНИЕ: model="cone" — это НЕ встроенный пресет Ursina
        # (в отличие от "cube"/"sphere"/"plane"), поэтому загрузчик не
        # находил файл и писал "warning: missing model: 'cone'", а
        # наконечник стрелки просто не рендерился. Cone(resolution=8) —
        # процедурная примитива Ursina, которая всегда доступна и не
        # зависит от asset_folder/файлов на диске.
        Entity(
            parent=self.arrow_root,
            model=Cone(resolution=8),
            color=ARROW_TIP_COLOR,
            scale=(0.36, 0.36, 0.52),
            position=(0, 0, AIM_ARROW_LENGTH / 2),
            rotation_x=90,
        )

        self.name_label = None
        if show_name_label:
            self.name_label = Text(
                parent=self,
                text=player_name,
                origin=(0, 0),
                position=(0, PLAYER_NAME_HEIGHT, 0),
                scale=6,
                billboard=True,
                background=True,
            )

    def set_name(self, player_name: str) -> None:
        self.player_name = player_name
        if self.name_label is not None:
            self.name_label.text = player_name

    def set_pitch(self, pitch_degrees: float) -> None:
        # model_anchor — дочерний объект aim_pivot, поэтому поворот
        # aim_pivot автоматически "тащит" за собой всю модель вместе со
        # стрелкой — они единая сварная конструкция (WeldConstraint-стиль).
        # model_anchor.rotation НЕ трогаем здесь: это статичная поправка
        # осей модели (MODEL_ROTATION_X/Y/Z), выставленная один раз при
        # загрузке, а не компенсация pitch — трогать её каждый кадр было
        # бы как раз тем, что "отваривает" модель от стрелки.
        self.aim_pivot.rotation = Vec3(-pitch_degrees, 0, 0)


# ============================================================
# УДАЛЁННЫЙ ИГРОК
# ============================================================

class RemotePlayer(Entity):
    def __init__(
        self,
        player_id: str,
        player_name: str,
        position: Vec3,
        yaw: float,
        pitch: float,
    ) -> None:
        super().__init__(
            position=position,
            rotation=(0, yaw, 0),
        )

        self.player_id = player_id
        self.player_name = player_name

        self.target_position = Vec3(position)
        self.target_yaw = wrap_angle(yaw)
        self.target_pitch = clamp(pitch, MIN_PITCH, MAX_PITCH)
        self.current_pitch = self.target_pitch

        self.visual = PlayerVisual(player_name=player_name)
        self.visual.parent = self
        self.visual.set_pitch(self.current_pitch)

    def set_target(
        self,
        position: Vec3,
        yaw: float,
        pitch: float,
        player_name: str,
    ) -> None:
        self.target_position = Vec3(position)
        self.target_yaw = wrap_angle(yaw)
        self.target_pitch = clamp(pitch, MIN_PITCH, MAX_PITCH)

        if player_name != self.player_name:
            self.player_name = player_name
            self.visual.set_name(player_name)

    def smooth_update(self) -> None:
        position_factor = min(
            1.0,
            ursina_time.dt * REMOTE_POSITION_SMOOTHNESS,
        )
        rotation_factor = min(
            1.0,
            ursina_time.dt * REMOTE_ROTATION_SMOOTHNESS,
        )

        self.position = lerp(
            self.position,
            self.target_position,
            position_factor,
        )

        yaw_difference = shortest_angle_difference(
            self.rotation_y,
            self.target_yaw,
        )
        self.rotation_y += yaw_difference * rotation_factor

        self.current_pitch += (
            self.target_pitch - self.current_pitch
        ) * rotation_factor

        # Удалённая модель не может перевернуться или завалиться набок.
        self.rotation_x = 0
        self.rotation_z = 0
        self.visual.rotation_x = 0
        self.visual.rotation_z = 0
        self.visual.set_pitch(self.current_pitch)


# ============================================================
# ЗАПИСЬ О СИНХРОНИЗИРОВАННОМ ОБЪЕКТЕ
# ============================================================

class InstanceRecord:
    """Клиентское зеркало серверного Instance. Существует для КАЖДОГО
    синхронизированного объекта (Part, Folder, Script, ...), а не только
    для тех, у кого есть 3D-Entity — Explorer/Inspector должны знать про
    все объекты одинаково. `entity` заполняется только когда у типа
    has_3d_entity=True (см. object_registry) — тогда же id появляется и в
    MultiplayerGame.parts, которым пользуются gizmo/выбор/наведение мыши.
    """

    __slots__ = ("id", "class_name", "name", "parent_id", "properties", "enabled", "entity")

    def __init__(
        self,
        instance_id: str,
        class_name: str,
        name: str,
        parent_id: str | None,
        properties: dict[str, Any],
        enabled: bool = True,
    ) -> None:
        self.id = instance_id
        self.class_name = class_name
        self.name = name
        self.parent_id = parent_id
        self.properties = properties
        self.enabled = enabled
        self.entity: Entity | None = None


# ============================================================
# ОСНОВНОЙ КОНТРОЛЛЕР ИГРЫ
# ============================================================

class MultiplayerGame(Entity):
    def __init__(self, server_url: str, player_name: str) -> None:
        super().__init__()

        self.server_url = server_url
        self.player_name = player_name
        self.network = NetworkClient(server_url=server_url, player_name=player_name)

        self.local_player_id: str | None = None
        self.remote_players: dict[str, RemotePlayer] = {}
        # self.parts — только объекты с реальной 3D-Entity (Part/SpawnPoint):
        # это то, с чем работают gizmo, hover, click-select. self.instances —
        # ВСЕ синхронизированные объекты (включая Folder/Model/Script/...),
        # это то, что видит Explorer/Inspector через адаптер.
        self.parts: dict[str, Entity] = {}
        self.instances: dict[str, InstanceRecord] = {}
        self.selected_part_id: str | None = None
        self.selection_highlight: Entity | None = None
        self.studio_adapter: MultiplayerStudioAdapter | None = None

        self.studio_playing = False
        self.editor_look_active = False
        self.player_yaw = 0.0
        self.player_pitch = 0.0
        self.last_send_time = 0.0
        self.third_person_enabled = False

        # Заполняется embed_panda_window() при успешном встраивании
        # viewport'а в Qt. Пока None — используется штатный путь Ursina
        # (mouse.locked/mouse.velocity), это верно для --no-embed и для
        # случая, когда встраивание не удалось и Panda3D осталась
        # владеть отдельным top-level окном.
        self.qt_viewport_container: QWidget | None = None
        self._qt_look_last_pos: QPoint | None = None

        self.saved_play_position = Vec3(0, 3, 0)
        self.saved_play_yaw = 0.0
        self.saved_play_pitch = 0.0

        # Transform gizmo (Move/Rotate) для Studio-редактора. Создаётся
        # один раз и переиспользуется — привязка к объекту и режим
        # переключаются через set_gizmo_mode()/update_gizmo().
        self.gizmo = TransformGizmo()
        self.gizmo_mode = "select"
        self.gizmo_snap_size = DEFAULT_MOVE_SNAP
        self._gizmo_last_network_send = 0.0

        self.create_world()
        self.create_first_person_player()
        self.create_local_visual()
        self.create_interface()
        self.set_studio_playing(False, preserve_position=False)
        self.network.start()

    def set_studio_adapter(self, adapter: "MultiplayerStudioAdapter") -> None:
        self.studio_adapter = adapter

    def set_studio_playing(self, playing: bool, preserve_position: bool = True) -> None:
        playing = bool(playing)

        if playing:
            self.editor_look_active = False
            self.local_player.position = Vec3(self.saved_play_position)
            self.player_yaw = self.saved_play_yaw
            self.player_pitch = self.saved_play_pitch
            self.local_player.rotation = Vec3(0, self.player_yaw, 0)
            self.camera_pitch_pivot.rotation = Vec3(-self.player_pitch, 0, 0)
            self.studio_playing = True
            self.hud_root.enabled = True
            self._start_mouse_look()
            self.gizmo.set_target(None)
            if self.selection_highlight is not None:
                self.selection_highlight.enabled = False
        else:
            if preserve_position and self.studio_playing:
                self.saved_play_position = Vec3(self.local_player.position)
                self.saved_play_yaw = float(self.player_yaw)
                self.saved_play_pitch = float(self.player_pitch)
            self.studio_playing = False
            self.editor_look_active = False
            self.hud_root.enabled = False
            self._stop_mouse_look()
            if self.selection_highlight is not None:
                self.selection_highlight.enabled = True

    def begin_editor_look(self) -> None:
        if self.studio_playing:
            return
        self.editor_look_active = True
        self._start_mouse_look()

    def end_editor_look(self) -> None:
        if self.studio_playing:
            return
        self.editor_look_active = False
        self._stop_mouse_look()

    # --------------------------------------------------------
    # RELATIVE MOUSE LOOK (см. комментарий у QT_LOOK_SENSITIVITY_*)
    # --------------------------------------------------------

    def set_qt_viewport_container(self, container: QWidget) -> None:
        self.qt_viewport_container = container

    def _qt_look_available(self) -> bool:
        return self.qt_viewport_container is not None

    def _mouse_look_captured(self) -> bool:
        if self._qt_look_available():
            return self._qt_look_last_pos is not None
        return mouse.locked

    def _start_mouse_look(self) -> None:
        # Скрытие курсора трогает только cursor_hidden окна Panda3D,
        # не mouse_mode — это безопасно и для встроенного, и для
        # отдельного окна (в отличие от mouse.locked).
        mouse.visible = False

        if self._qt_look_available():
            container = self.qt_viewport_container
            assert container is not None
            center_global = container.mapToGlobal(container.rect().center())
            QCursor.setPos(center_global)
            self._qt_look_last_pos = center_global
        else:
            mouse.locked = True

    def _stop_mouse_look(self) -> None:
        mouse.visible = True

        if self._qt_look_available():
            self._qt_look_last_pos = None
        else:
            mouse.locked = False

    def _poll_qt_look_delta(self) -> tuple[float, float]:
        container = self.qt_viewport_container
        if container is None or self._qt_look_last_pos is None:
            return 0.0, 0.0

        current_global = QCursor.pos()
        delta_x = float(current_global.x() - self._qt_look_last_pos.x())
        delta_y = float(current_global.y() - self._qt_look_last_pos.y())

        # Каждый кадр возвращаем курсор в центр КОНТЕЙНЕРА (позицию
        # которого верно знает Qt), а не окна Panda3D (которое после
        # встраивания знает её неверно) — это и есть суть исправления.
        center_global = container.mapToGlobal(container.rect().center())
        QCursor.setPos(center_global)
        self._qt_look_last_pos = center_global

        if DEBUG_MOUSE_LOOK and (delta_x != 0.0 or delta_y != 0.0):
            print(f"[MOUSE_LOOK] delta_x={delta_x:.2f} delta_y={delta_y:.2f}")

        return delta_x, delta_y

    def create_world(self) -> None:
        self.background_mode = "sky"

        self.sky = Entity(
            model="sphere",
            texture="white_cube",
            color=SKY_COLOR,
            scale=1000,
            double_sided=True,
            shader=unlit_shader,
        )

        self.ground = Entity(
            model="plane",
            texture="white_cube",
            texture_scale=(64, 64),
            color=GROUND_COLOR,
            scale=150,
            y=-8,
            collider="box",
        )

        # Сетка на полу для Blender-style режима. Настоящий wireframe-меш
        # (Grid), а не текстура — линии остаются чёткими на любом зуме.
        # Чуть приподнята над ground (y=-7.99), чтобы не мерцать (z-fighting).
        self.floor_grid = Entity(
            model=Grid(GRID_LINE_COUNT, GRID_LINE_COUNT),
            color=GRID_LINE_COLOR,
            scale=GRID_LINE_COUNT * GRID_LINE_SPACING,
            rotation_x=90,
            y=-7.99,
            unlit=True,
            enabled=False,
        )

        blocks = (
            (-12, -5, 8, 4, 6, 4),
            (15, -4, 12, 6, 8, 6),
            (-20, -2, -15, 5, 12, 5),
            (20, 1, -20, 4, 18, 4),
            (0, -5, 25, 10, 6, 10),
        )

        self.static_blocks: list[Entity] = []

        for x, y, z, scale_x, scale_y, scale_z in blocks:
            block = Entity(
                model="cube",
                color=BLOCK_COLOR,
                position=(x, y, z),
                scale=(scale_x, scale_y, scale_z),
                collider="box",
            )
            self.static_blocks.append(block)

        self.ambient_light = AmbientLight()
        self.ambient_light.color = AMBIENT_LIGHT_COLOR

        self.sun = DirectionalLight()
        self.sun.color = SUN_LIGHT_COLOR
        self.sun.look_at(Vec3(1, -1, -1))

    def toggle_background(self) -> None:
        if self.background_mode == "sky":
            self.background_mode = "grid"
            self.sky.enabled = False
            self.floor_grid.enabled = True
            self.ground.color = GRID_GROUND_COLOR
            window.color = GRID_BACKGROUND_COLOR
        else:
            self.background_mode = "sky"
            self.sky.enabled = True
            self.floor_grid.enabled = False
            self.ground.color = GROUND_COLOR
            window.color = SKY_COLOR

        self.background_status_text.text = (
            f"Фон: {'сетка' if self.background_mode == 'grid' else 'небо'} (B)"
        )

    # --------------------------------------------------------
    # ПЕРВОЕ ЛИЦО
    # --------------------------------------------------------

    def create_first_person_player(self) -> None:
        # Корень игрока получает только yaw и позицию.
        self.local_player = Entity(
            position=(0, 3, 0),
            rotation=(0, 0, 0),
        )

        # Отдельный pivot получает только pitch.
        self.camera_pitch_pivot = Entity(
            parent=self.local_player,
            position=(0, EYE_HEIGHT, 0),
            rotation=(0, 0, 0),
        )

        camera.parent = self.camera_pitch_pivot
        camera.position = Vec3(0, 0, 0)
        camera.rotation = Vec3(0, 0, 0)
        camera.fov = 90
        camera.clip_plane_far = 1500

    def create_local_visual(self) -> None:
        # Модель самого себя. По умолчанию скрыта, т.к. игрок в первом
        # лице обычно не должен видеть своё тело перед камерой.
        # Включается клавишей V (третье лицо) — удобно, чтобы проверить,
        # что player.glb вообще грузится и рендерится, не поднимая
        # второй клиент.
        self.local_visual = PlayerVisual(
            player_name=self.player_name,
            show_name_label=False,
        )
        self.local_visual.parent = self.local_player
        self.local_visual.enabled = False

    # --------------------------------------------------------
    # ИНТЕРФЕЙС
    # --------------------------------------------------------

    def create_interface(self) -> None:
        self.hud_root = Entity(parent=camera.ui)

        self.status_text = Text(
            parent=self.hud_root,
            text="Запуск сети...",
            position=(-0.87, 0.46),
            origin=(-0.5, 0.5),
            background=True,
        )
        self.players_text = Text(
            parent=self.hud_root,
            text="Игроков: 1",
            position=(-0.87, 0.405),
            origin=(-0.5, 0.5),
            background=True,
        )
        self.help_text = Text(
            parent=self.hud_root,
            text=(
                "WASD — полёт\n"
                "Space / Ctrl — вверх и вниз\n"
                "Shift — ускорение\n"
                "Мышь — обзор\n"
                "V — первое/третье лицо\n"
                "B — небо/сетка\n"
                "E — создать Part\n"
                "Esc — освободить мышь\n"
                "Shift+F5 — вернуться в Studio"
            ),
            position=(-0.87, 0.31),
            origin=(-0.5, 0.5),
            background=True,
        )
        self.background_status_text = Text(
            parent=self.hud_root,
            text="Фон: небо (B)",
            position=(-0.87, 0.245),
            origin=(-0.5, 0.5),
            background=True,
        )

        file_ok, file_message = model_file_status()
        self.model_status_text = Text(
            parent=self.hud_root,
            text=file_message,
            color=color.lime if file_ok else color.red,
            position=(-0.87, -0.35),
            origin=(-0.5, 0.5),
            background=True,
        )

        if not SIMPLEPBR_AVAILABLE:
            Text(
                parent=self.hud_root,
                text="simplepbr не установлен: pip install panda3d-simplepbr",
                color=color.orange,
                position=(-0.87, -0.41),
                origin=(-0.5, 0.5),
                background=True,
            )

        self.crosshair = Entity(
            parent=self.hud_root,
            model="quad",
            color=color.rgba32(255, 255, 255, 200),
            scale=0.006,
        )

    def select_part(self, part_id: str, notify_studio: bool = True) -> None:
        entity = self.parts.get(part_id)
        if entity is None:
            self.deselect_part(notify_studio=notify_studio)
            return

        self.selected_part_id = part_id
        self.update_selection_highlight(entity)
        if notify_studio and self.studio_adapter is not None:
            self.studio_adapter.on_game_selection_changed(part_id)

    def select_part_from_studio(self, part_id: str | None) -> None:
        if part_id is None:
            self.deselect_part(notify_studio=False)
            return
        self.select_part(part_id, notify_studio=False)

    def deselect_part(self, notify_studio: bool = True) -> None:
        self.selected_part_id = None
        if self.selection_highlight is not None:
            destroy(self.selection_highlight)
            self.selection_highlight = None
        if notify_studio and self.studio_adapter is not None:
            self.studio_adapter.on_game_selection_changed(None)

    def update_selection_highlight(self, entity: Entity) -> None:
        if self.selection_highlight is not None:
            destroy(self.selection_highlight)

        self.selection_highlight = Entity(
            model="wireframe_cube",
            color=color.yellow,
            position=entity.position,
            scale=entity.scale * 1.02,
            rotation=entity.rotation,
            unlit=True,
            enabled=not self.studio_playing,
        )

    def handle_editor_click(self) -> None:
        hovered = mouse.hovered_entity
        if hovered is None:
            self.deselect_part()
            return
        part_id = getattr(hovered, "part_id", None)
        if part_id is None:
            self.deselect_part()
            return
        self.select_part(str(part_id))

    def focus_selected_part(self) -> None:
        entity = self.parts.get(self.selected_part_id or "")
        if entity is None:
            return
        offset = forward_from_angles(self.player_yaw, self.player_pitch) * -8.0
        self.local_player.position = entity.world_position + offset + Vec3(0, 2.0, 0)

    # --------------------------------------------------------
    # TRANSFORM GIZMO (Move/Rotate)
    # --------------------------------------------------------

    def set_gizmo_mode(self, mode: str) -> None:
        self.gizmo_mode = mode
        self.gizmo.set_mode(mode)

    def set_gizmo_snap(self, value: float) -> None:
        if value > 0:
            self.gizmo_snap_size = value

    def update_gizmo(self) -> None:
        target = self.parts.get(self.selected_part_id) if self.selected_part_id else None
        if target is not self.gizmo.current_target:
            self.gizmo.set_target(target)

        if target is None:
            return

        self.gizmo.refresh_transform(camera.world_position)

        if self.gizmo.dragging:
            ray_origin, ray_direction = mouse_world_ray()
            if ray_origin is None:
                return
            shift_held = bool(held_keys["shift"] or held_keys["left shift"])
            result = self.gizmo.update_drag(
                ray_origin,
                ray_direction,
                self.gizmo_snap_size,
                not shift_held,
            )
            if result is not None:
                self._apply_gizmo_result(target, result, force_network=False)
        else:
            ray_origin, ray_direction = mouse_world_ray()
            if ray_origin is not None:
                self.gizmo.update_hover(ray_origin, ray_direction)

    def try_begin_gizmo_drag(self) -> bool:
        if self.gizmo.current_target is None or self.gizmo_mode not in ("move", "rotate"):
            return False
        ray_origin, ray_direction = mouse_world_ray()
        if ray_origin is None:
            return False
        return self.gizmo.begin_drag(ray_origin, ray_direction)

    def end_gizmo_drag(self) -> None:
        result = self.gizmo.end_drag()
        if result is None:
            return
        target = self.parts.get(self.selected_part_id) if self.selected_part_id else None
        if target is None:
            return
        self._apply_gizmo_result(target, result, force_network=True)

    def _apply_gizmo_result(
        self,
        entity: Entity,
        result: tuple[str, Vec3],
        force_network: bool,
    ) -> None:
        kind, vector = result
        part_id = self.selected_part_id
        if part_id is None:
            return

        # Entity уже обновлена внутри gizmo.update_drag()/end_drag(). Тут
        # синхронизируем instance_properties (иначе Inspector увидит
        # старое значение — instance_to_scene_object() читает именно
        # record.properties, а не entity.position напрямую) и SceneObject в
        # EngineBridge — через on_instance_updated(), который НЕ дёргает
        # адаптер повторно (в отличие от bridge.set_property()).
        new_value = [float(vector.x), float(vector.y), float(vector.z)]
        entity.instance_properties[kind] = new_value
        record = self.instances.get(part_id)
        if record is not None:
            record.properties[kind] = new_value
        self.update_selection_highlight(entity)
        if self.studio_adapter is not None and record is not None:
            self.studio_adapter.on_instance_updated(record)

        now = time.monotonic()
        interval = 1.0 / GIZMO_NETWORK_SEND_RATE
        if force_network or (now - self._gizmo_last_network_send) >= interval:
            self._gizmo_last_network_send = now
            self.apply_property_edit(part_id, {kind: entity.instance_properties[kind]})

    def apply_property_edit(
        self,
        part_id: str,
        properties: dict[str, Any] | None = None,
        name: str | None = None,
        enabled: bool | None = None,
    ) -> bool:
        if not self.network.connected_event.is_set():
            if self.studio_adapter is not None:
                self.studio_adapter.log("warning", "Нет подключения к серверу: изменение не отправлено.")
            return False

        message: dict[str, Any] = {"type": protocol.UPDATE_PROPERTY, "id": part_id}
        if properties:
            message["properties"] = properties
        if name is not None:
            message["name"] = name
        if enabled is not None:
            message["enabled"] = enabled

        self.network.send(message)
        return True

    def delete_instance(self, instance_id: str) -> bool:
        if not self.network.connected_event.is_set():
            return False
        self.network.send({"type": protocol.DELETE_PART, "id": instance_id})
        return True

    def request_create_instance(
        self,
        class_name: str,
        properties: dict[str, Any] | None = None,
        parent_id: str | None = None,
        name: str | None = None,
    ) -> bool:
        if not self.network.connected_event.is_set():
            if self.studio_adapter is not None:
                self.studio_adapter.log("warning", f"Нет подключения к серверу: {class_name} не создан.")
            return False

        definition = object_registry.get_object_type(class_name)

        if properties is None:
            if definition is not None and definition.has_3d_entity:
                forward_direction = forward_from_angles(self.player_yaw, self.player_pitch)
                spawn_position = (
                    self.local_player.position
                    + Vec3(0, EYE_HEIGHT, 0)
                    + forward_direction * PART_SPAWN_DISTANCE
                )
                properties = {
                    "Position": [
                        round(float(spawn_position.x), 3),
                        round(float(spawn_position.y), 3),
                        round(float(spawn_position.z), 3),
                    ],
                }
                if class_name == "Part":
                    properties["Size"] = list(PART_DEFAULT_SIZE)
                    properties["Color"] = list(PART_DEFAULT_COLOR)
            else:
                properties = {}

        message: dict[str, Any] = {
            "type": protocol.CREATE_PART,
            "class_name": class_name,
            "properties": properties,
        }
        if parent_id is not None:
            message["parent_id"] = parent_id
        if name is not None:
            message["name"] = name

        self.network.send(message)
        return True

    def duplicate_instance(self, instance_id: str) -> bool:
        record = self.instances.get(instance_id)
        if record is None:
            return False
        properties = dict(record.properties)
        if "Position" in properties:
            position = list(properties["Position"])
            while len(position) < 3:
                position.append(0)
            position[0] = float(position[0]) + 2.0
            position[2] = float(position[2]) + 2.0
            properties["Position"] = position
        return self.request_create_instance(
            record.class_name, properties, parent_id=record.parent_id, name=record.name,
        )

    def input(self, key: str) -> None:
        if not self.studio_playing:
            if key == "left mouse down":
                if not self.try_begin_gizmo_drag():
                    self.handle_editor_click()
            elif key == "left mouse up":
                self.end_gizmo_drag()
            elif key == "right mouse down":
                if not self.gizmo.dragging:
                    self.begin_editor_look()
            elif key == "right mouse up":
                self.end_editor_look()
            elif key == "escape":
                self.end_editor_look()
            elif key == "b":
                self.toggle_background()
            elif key == "e":
                self.request_create_instance("Part")
            elif key == "f":
                self.focus_selected_part()
            return

        if key == "escape":
            if self._mouse_look_captured():
                self._stop_mouse_look()
            else:
                self._start_mouse_look()
        elif key == "left mouse down" and not self._mouse_look_captured():
            self._start_mouse_look()
        elif key == "v":
            self.toggle_third_person()
        elif key == "b":
            self.toggle_background()
        elif key == "e":
            self.request_create_instance("Part")

    def toggle_third_person(self) -> None:
        self.third_person_enabled = not self.third_person_enabled
        self.local_visual.enabled = self.third_person_enabled

        if self.third_person_enabled:
            camera.parent = self.camera_pitch_pivot
            camera.position = Vec3(0, 0, -THIRD_PERSON_DISTANCE)
            camera.y = THIRD_PERSON_HEIGHT
        else:
            camera.parent = self.camera_pitch_pivot
            camera.position = Vec3(0, 0, 0)

    def update_mouse_look(self) -> None:
        if self._qt_look_available():
            delta_x, delta_y = self._poll_qt_look_delta()
            if delta_x == 0.0 and delta_y == 0.0:
                return
            self.player_yaw += delta_x * QT_LOOK_SENSITIVITY_X
            # Экранная ось Y растёт вниз, а движение мыши вверх должно
            # увеличивать pitch — отсюда минус (см. mouse.velocity[1]
            # ниже, где знак уже заложен в саму Ursina-координату y).
            self.player_pitch += -delta_y * QT_LOOK_SENSITIVITY_Y
        else:
            if not mouse.locked:
                return

            self.player_yaw += mouse.velocity[0] * MOUSE_SENSITIVITY_X
            self.player_pitch += mouse.velocity[1] * MOUSE_SENSITIVITY_Y

        self.player_yaw = wrap_angle(self.player_yaw)
        self.player_pitch = clamp(
            self.player_pitch,
            MIN_PITCH,
            MAX_PITCH,
        )

        # Тело не наклоняется по X/Z.
        self.local_player.rotation = Vec3(0, self.player_yaw, 0)

        # Отрицательный знак нужен для направления оси X камеры Ursina.
        self.camera_pitch_pivot.rotation = Vec3(
            -self.player_pitch,
            0,
            0,
        )

        camera.rotation_z = 0

        if self.third_person_enabled:
            self.local_visual.set_pitch(self.player_pitch)

    def update_flight(self) -> None:
        forward_input = held_keys["w"] - held_keys["s"]
        right_input = held_keys["d"] - held_keys["a"]

        control_pressed = max(
            held_keys["control"],
            held_keys["left control"],
        )
        vertical_input = held_keys["space"] - control_pressed

        forward_direction = forward_from_angles(
            self.player_yaw,
            self.player_pitch,
        )
        right_direction = right_from_yaw(self.player_yaw)
        up_direction = Vec3(0, 1, 0)

        movement = (
            forward_direction * forward_input
            + right_direction * right_input
            + up_direction * vertical_input
        )

        if movement.length() <= 0:
            return

        shift_pressed = max(
            held_keys["shift"],
            held_keys["left shift"],
        )

        speed = FLIGHT_SPEED
        if shift_pressed:
            speed *= BOOST_MULTIPLIER

        self.local_player.position += (
            movement.normalized() * speed * ursina_time.dt
        )

        self.local_player.rotation_x = 0
        self.local_player.rotation_z = 0

    # --------------------------------------------------------
    # СЕТЕВЫЕ СООБЩЕНИЯ
    # --------------------------------------------------------

    def process_network_messages(self) -> None:
        for message in self.network.receive_all():
            message_type = message.get("type")

            if message_type == "connected":
                self.handle_connected_message(message)
            elif message_type == "world_state":
                players = message.get("players", {})
                if isinstance(players, dict):
                    self.update_remote_players(players)
            elif message_type == "connection_status":
                status = str(message.get("message", ""))
                self.status_text.text = status
                if self.studio_adapter is not None:
                    self.studio_adapter.on_network_status(status)
            elif message_type == "network_error":
                error = "Ошибка сети: " + str(message.get("message", "неизвестно"))
                self.status_text.text = error
                if self.studio_adapter is not None:
                    self.studio_adapter.log("error", error)
            elif message_type == protocol.WORLD_SNAPSHOT:
                parts = message.get("parts", [])
                if isinstance(parts, list):
                    self.load_world_snapshot(parts)
            elif message_type == protocol.PART_CREATED:
                part_data = message.get("part", {})
                if isinstance(part_data, dict):
                    self.spawn_instance(part_data)
            elif message_type == protocol.PART_UPDATED:
                instance_id = str(message.get("id", ""))
                raw_properties = message.get("properties")
                properties = raw_properties if isinstance(raw_properties, dict) else None
                raw_name = message.get("name")
                name = raw_name if isinstance(raw_name, str) else None
                raw_enabled = message.get("enabled")
                enabled = raw_enabled if isinstance(raw_enabled, bool) else None
                if instance_id:
                    self.update_instance(instance_id, properties, name, enabled)
            elif message_type == protocol.PART_DELETED:
                self.remove_instance(str(message.get("id", "")))

    def load_world_snapshot(self, parts: list[dict[str, Any]]) -> None:
        incoming_ids = {str(item.get("id", "")) for item in parts if isinstance(item, dict)}
        for stale_id in list(self.instances):
            if stale_id not in incoming_ids:
                self.remove_instance(stale_id)
        for part_data in parts:
            if isinstance(part_data, dict):
                self.spawn_instance(part_data)
        if self.studio_adapter is not None:
            self.studio_adapter.sync_full_scene()

    def _build_part_entity(self, properties: dict[str, Any]) -> Entity | None:
        """Строит Ursina-куб для типов с has_3d_entity=True (Part/SpawnPoint —
        обе Part-формы, различаются только дефолтными properties)."""
        try:
            position = properties["Position"]
            size = properties["Size"]
            rotation = properties["Rotation"]
            rgb = properties["Color"]
            transparency = properties["Transparency"]
            can_collide = bool(properties.get("CanCollide", True))
            return Entity(
                model="cube",
                position=Vec3(float(position[0]), float(position[1]), float(position[2])),
                scale=Vec3(float(size[0]), float(size[1]), float(size[2])),
                rotation=Vec3(float(rotation[0]), float(rotation[1]), float(rotation[2])),
                color=color.rgba32(
                    int(rgb[0]), int(rgb[1]), int(rgb[2]),
                    int(255 * (1.0 - float(transparency))),
                ),
                collider="box" if can_collide else None,
            )
        except (TypeError, ValueError, IndexError, KeyError) as error:
            print(f"[WORLD] Не удалось построить Entity: битые properties: {error}")
            return None

    def _apply_instance_properties(
        self, record: "InstanceRecord", properties: dict[str, Any], replace: bool,
    ) -> None:
        if replace:
            record.properties = dict(properties)
        else:
            record.properties.update(properties)

        entity = self.parts.get(record.id)
        if entity is None:
            return

        entity.instance_properties = record.properties
        merged = record.properties

        try:
            if replace or "Position" in properties:
                position = merged["Position"]
                entity.position = Vec3(float(position[0]), float(position[1]), float(position[2]))
            if replace or "Size" in properties:
                size = merged["Size"]
                entity.scale = Vec3(float(size[0]), float(size[1]), float(size[2]))
            if replace or "Rotation" in properties:
                rotation = merged["Rotation"]
                entity.rotation = Vec3(float(rotation[0]), float(rotation[1]), float(rotation[2]))
            if replace or "Color" in properties or "Transparency" in properties:
                rgb = merged.get("Color", [255, 255, 255])
                transparency = merged.get("Transparency", 0.0)
                entity.color = color.rgba32(
                    int(rgb[0]), int(rgb[1]), int(rgb[2]),
                    int(255 * (1.0 - float(transparency))),
                )
            if replace or "CanCollide" in properties:
                entity.collider = "box" if bool(merged.get("CanCollide", True)) else None
        except (TypeError, ValueError, IndexError, KeyError) as error:
            print(f"[WORLD] Не удалось обновить {record.id}: {error}")

    def spawn_instance(self, data: dict[str, Any]) -> None:
        instance_id = str(data.get("id", ""))
        if not instance_id:
            return

        class_name = str(data.get("class_name", "Part"))
        definition = object_registry.get_object_type(class_name)

        raw_properties = data.get("properties", {})
        if not isinstance(raw_properties, dict):
            raw_properties = {}
        defaults = definition.default_properties if definition is not None else DEFAULT_PART_PROPERTIES
        properties = {**defaults, **raw_properties}

        name = str(data.get("name") or (definition.display_name if definition is not None else class_name))
        raw_parent_id = data.get("parent_id")
        parent_id = raw_parent_id if isinstance(raw_parent_id, str) and raw_parent_id else None
        enabled = bool(data.get("enabled", True))

        record = self.instances.get(instance_id)
        if record is not None:
            record.class_name = class_name
            record.name = name
            record.parent_id = parent_id
            record.enabled = enabled
            self._apply_instance_properties(record, properties, replace=True)
            entity = self.parts.get(instance_id)
            if entity is not None:
                entity.instance_name = name
            if self.studio_adapter is not None:
                self.studio_adapter.on_instance_updated(record)
            return

        record = InstanceRecord(instance_id, class_name, name, parent_id, properties, enabled)
        self.instances[instance_id] = record

        if definition is not None and definition.has_3d_entity:
            entity = self._build_part_entity(properties)
            if entity is not None:
                entity.part_id = instance_id
                entity.instance_name = name
                entity.instance_properties = properties
                self.parts[instance_id] = entity
                record.entity = entity

        if self.studio_adapter is not None:
            self.studio_adapter.on_instance_created(record)

    def update_instance(
        self,
        instance_id: str,
        properties: dict[str, Any] | None,
        name: str | None,
        enabled: bool | None = None,
    ) -> None:
        record = self.instances.get(instance_id)
        if record is None:
            return

        if properties:
            self._apply_instance_properties(record, properties, replace=False)
        if name is not None:
            record.name = name
            entity = self.parts.get(instance_id)
            if entity is not None:
                entity.instance_name = name
        if enabled is not None:
            record.enabled = enabled

        if instance_id == self.selected_part_id:
            entity = self.parts.get(instance_id)
            if entity is not None:
                self.update_selection_highlight(entity)
        if self.studio_adapter is not None:
            self.studio_adapter.on_instance_updated(record)

    def remove_instance(self, instance_id: str) -> None:
        self.instances.pop(instance_id, None)
        entity = self.parts.pop(instance_id, None)
        if entity is not None:
            destroy(entity)
        if instance_id == self.selected_part_id:
            self.deselect_part(notify_studio=False)
        if self.studio_adapter is not None:
            self.studio_adapter.on_instance_deleted(instance_id)

    def spawn_part_in_front_of_camera(self) -> None:
        self.request_create_instance("Part")

    def handle_connected_message(self, message: dict[str, Any]) -> None:
        self.local_player_id = str(message.get("player_id", ""))
        player_data = message.get("player", {})

        if isinstance(player_data, dict):
            position_data = player_data.get("position", [0, 3, 0])
            try:
                server_position = Vec3(
                    float(position_data[0]),
                    float(position_data[1]),
                    float(position_data[2]),
                )
                self.saved_play_position = Vec3(server_position)
                if self.studio_playing:
                    self.local_player.position = Vec3(server_position)
            except (TypeError, ValueError, IndexError):
                pass

        status = f"Подключено: {self.local_player_id}"
        self.status_text.text = status
        if self.studio_adapter is not None:
            self.studio_adapter.on_network_status(status)

    def update_remote_players(self, players: dict[str, Any]) -> None:
        active_player_ids: set[str] = set()

        for player_id, player_data in players.items():
            if player_id == self.local_player_id:
                continue

            if not isinstance(player_data, dict):
                continue

            position_data = player_data.get("position", [0, 0, 0])

            try:
                position = Vec3(
                    float(position_data[0]),
                    float(position_data[1]),
                    float(position_data[2]),
                )
                yaw = float(player_data.get("rotation_y", 0.0))
                pitch = float(player_data.get("rotation_x", 0.0))
            except (TypeError, ValueError, IndexError):
                continue

            yaw = wrap_angle(yaw)
            pitch = clamp(pitch, MIN_PITCH, MAX_PITCH)
            player_name = str(
                player_data.get("name", f"Player-{player_id[:4]}")
            )

            active_player_ids.add(player_id)

            if player_id not in self.remote_players:
                remote_player = RemotePlayer(
                    player_id=player_id,
                    player_name=player_name,
                    position=position,
                    yaw=yaw,
                    pitch=pitch,
                )
                self.remote_players[player_id] = remote_player

                if remote_player.visual.using_custom_model:
                    self.model_status_text.text = remote_player.visual.model_status
                    self.model_status_text.color = color.lime
                else:
                    self.model_status_text.text = remote_player.visual.model_status
                    self.model_status_text.color = color.red

            else:
                self.remote_players[player_id].set_target(
                    position=position,
                    yaw=yaw,
                    pitch=pitch,
                    player_name=player_name,
                )

        removed_player_ids = set(self.remote_players) - active_player_ids

        for player_id in removed_player_ids:
            remote_player = self.remote_players.pop(player_id)
            destroy(remote_player)

        self.players_text.text = f"Игроков: {1 + len(self.remote_players)}"

    def send_transform(self) -> None:
        if not self.network.connected_event.is_set():
            return

        current_time = time.monotonic()

        if current_time - self.last_send_time < 1.0 / SEND_RATE:
            return

        self.last_send_time = current_time
        position = self.local_player.position

        self.network.send(
            {
                "type": "transform",
                "position": [
                    round(float(position.x), 4),
                    round(float(position.y), 4),
                    round(float(position.z), 4),
                ],
                "rotation_y": round(float(self.player_yaw), 3),
                "rotation_x": round(float(self.player_pitch), 3),
            }
        )

    # --------------------------------------------------------
    # КАДР
    # --------------------------------------------------------

    def update_remote_smoothing(self) -> None:
        for remote_player in self.remote_players.values():
            remote_player.smooth_update()

    def update_sky(self) -> None:
        self.sky.position = camera.world_position

    def update(self) -> None:
        self.process_network_messages()
        if not self.studio_playing:
            self.update_gizmo()
        if (self.studio_playing or self.editor_look_active) and not self.gizmo.dragging:
            self.update_mouse_look()
            self.update_flight()
        if self.studio_playing:
            self.send_transform()
        self.update_remote_smoothing()
        self.update_sky()

    def shutdown(self) -> None:
        self.network.stop()


class MultiplayerStudioAdapter:
    def __init__(self, game: MultiplayerGame) -> None:
        self.game = game
        self.bridge: EngineBridge | None = None
        self.pending_create_count = 0

    def attach_bridge(self, bridge: EngineBridge) -> None:
        self.bridge = bridge
        self.game.set_studio_adapter(self)
        self.sync_full_scene()
        self.game.set_studio_playing(False, preserve_position=False)

    def log(self, level: str, message: str) -> None:
        if self.bridge is not None:
            self.bridge.log(level, message)
        else:
            print(f"[{level.upper()}] {message}")

    @staticmethod
    def _as_editor_vec3(value: Any, default: tuple[float, float, float]) -> EditorVec3:
        try:
            return EditorVec3(float(value[0]), float(value[1]), float(value[2]))
        except (TypeError, ValueError, IndexError):
            return EditorVec3(*default)

    @staticmethod
    def _rgb_to_hex(value: Any) -> str:
        try:
            r = max(0, min(255, int(value[0])))
            g = max(0, min(255, int(value[1])))
            b = max(0, min(255, int(value[2])))
        except (TypeError, ValueError, IndexError):
            r, g, b = 255, 255, 255
        return f"#{r:02x}{g:02x}{b:02x}"

    @staticmethod
    def _hex_to_rgb(value: str) -> list[int]:
        clean = str(value).strip().lstrip("#")
        if len(clean) == 3:
            clean = "".join(char * 2 for char in clean)
        if len(clean) != 6:
            return [255, 255, 255]
        try:
            return [int(clean[0:2], 16), int(clean[2:4], 16), int(clean[4:6], 16)]
        except ValueError:
            return [255, 255, 255]

    def _system_objects(self) -> list[SceneObject]:
        objects = [
            SceneObject(
                id="system:camera",
                name="Camera",
                object_type="Camera",
                parent="Workspace",
                position=EditorVec3(
                    float(self.game.local_player.x),
                    float(self.game.local_player.y),
                    float(self.game.local_player.z),
                ),
                can_collide=False,
                cast_shadow=False,
            ),
            SceneObject(
                id="system:terrain",
                name="Terrain",
                object_type="Terrain",
                parent="Workspace",
                position=EditorVec3(0, -8, 0),
                size=EditorVec3(150, 1, 150),
                color="#5c7058",
                anchored=True,
            ),
            SceneObject(
                id="system:lighting",
                name="Lighting",
                object_type="Lighting",
                parent="Workspace",
                can_collide=False,
                cast_shadow=False,
            ),
            SceneObject(
                id="system:baseplate",
                name="Baseplate",
                object_type="Baseplate",
                parent="Workspace",
                position=EditorVec3(0, -8, 0),
                size=EditorVec3(150, 1, 150),
                color="#5c7058",
                anchored=True,
            ),
            # Синтетическая (не сетевая) папка — просто чтобы StaticBlock_N
            # ниже собрались в Explorer в одну группу, как раньше.
            SceneObject(
                id="system:static_geometry",
                name="Static Geometry",
                object_type="Folder",
                parent="Workspace",
            ),
        ]
        for index, block in enumerate(self.game.static_blocks, start=1):
            objects.append(
                SceneObject(
                    id=f"system:block:{index}",
                    name=f"StaticBlock_{index}",
                    object_type="Part",
                    parent="system:static_geometry",
                    position=EditorVec3(float(block.x), float(block.y), float(block.z)),
                    rotation=EditorVec3(
                        float(block.rotation_x),
                        float(block.rotation_y),
                        float(block.rotation_z),
                    ),
                    size=EditorVec3(float(block.scale_x), float(block.scale_y), float(block.scale_z)),
                    color="#5c656f",
                    anchored=True,
                )
            )
        return objects

    def instance_to_scene_object(self, record: "InstanceRecord") -> SceneObject:
        definition = object_registry.get_object_type(record.class_name)
        properties = record.properties
        parent = record.parent_id or "Workspace"

        if definition is not None and definition.has_3d_entity:
            return SceneObject(
                id=record.id,
                name=record.name,
                object_type=record.class_name,
                parent=parent,
                enabled=record.enabled,
                position=self._as_editor_vec3(properties.get("Position"), (0, 0, 0)),
                rotation=self._as_editor_vec3(properties.get("Rotation"), (0, 0, 0)),
                scale=EditorVec3(1, 1, 1),
                size=self._as_editor_vec3(properties.get("Size"), (2, 2, 2)),
                color=self._rgb_to_hex(properties.get("Color", [255, 255, 255])),
                material=str(properties.get("Material", "Plastic")),
                transparency=float(properties.get("Transparency", 0.0)),
                reflectance=float(properties.get("Reflectance", 0.0)),
                anchored=bool(properties.get("Anchored", False)),
                can_collide=bool(properties.get("CanCollide", True)),
                cast_shadow=bool(properties.get("CastShadow", True)),
                locked=bool(properties.get("Locked", False)),
                attributes={"server_id": record.id},
                properties=dict(properties),
            )

        return SceneObject(
            id=record.id,
            name=record.name,
            object_type=record.class_name,
            parent=parent,
            enabled=record.enabled,
            anchored=True,
            can_collide=False,
            cast_shadow=False,
            attributes={"server_id": record.id},
            properties=dict(properties),
        )

    def sync_full_scene(self) -> None:
        if self.bridge is None:
            return
        objects = self._system_objects()
        objects.extend(self.instance_to_scene_object(record) for record in self.game.instances.values())
        self.bridge.sync_scene(objects)

    def on_instance_created(self, record: "InstanceRecord") -> None:
        if self.bridge is None:
            return
        self.bridge.sync_upsert(self.instance_to_scene_object(record))
        self.log("info", f'Created {record.class_name} "{record.name}"')
        if self.pending_create_count > 0:
            self.pending_create_count -= 1
            self.bridge.sync_select(record.id)
            self.game.select_part_from_studio(record.id)

    def on_instance_updated(self, record: "InstanceRecord") -> None:
        if self.bridge is not None:
            self.bridge.sync_upsert(self.instance_to_scene_object(record))

    def on_instance_deleted(self, instance_id: str) -> None:
        if self.bridge is not None:
            self.bridge.sync_delete(instance_id)

    def on_game_selection_changed(self, part_id: str | None) -> None:
        if self.bridge is not None:
            self.bridge.sync_select(part_id)

    def on_network_status(self, status: str) -> None:
        lowered = status.lower()
        level = "info"
        if "ошиб" in lowered or "недоступ" in lowered:
            level = "error"
        elif "подключение" in lowered or "закрыто" in lowered:
            level = "warning"
        self.log(level, status)

    def _unique_sibling_name(self, base: str, parent_key: str) -> str:
        if self.bridge is None:
            return base
        siblings = {
            obj.name for obj in self.bridge.objects
            if (obj.parent or "Workspace") == parent_key
        }
        if base not in siblings:
            return base
        index = 2
        while f"{base}{index}" in siblings:
            index += 1
        return f"{base}{index}"

    def create_part(self, object_type: str, parent_id: str | None = None) -> bool:
        definition = object_registry.get_object_type(object_type)
        if definition is None or not definition.creatable:
            self.log("warning", f"Unknown or non-creatable object type '{object_type}'.")
            return False

        resolved_parent = parent_id or definition.default_parent
        unique_name = self._unique_sibling_name(definition.display_name, resolved_parent)

        accepted = self.game.request_create_instance(
            object_type, parent_id=resolved_parent, name=unique_name,
        )
        if accepted:
            self.pending_create_count += 1
        return accepted

    def duplicate_object(self, object_id: str) -> bool:
        accepted = self.game.duplicate_instance(object_id)
        if accepted:
            self.pending_create_count += 1
        return accepted

    def delete_object(self, object_id: str) -> bool:
        if object_id.startswith("system:"):
            return False
        return self.game.delete_instance(object_id)

    def select_object(self, object_id: str | None) -> bool:
        if object_id is None or object_id.startswith("system:"):
            self.game.select_part_from_studio(None)
            return True
        self.game.select_part_from_studio(object_id)
        return True

    def set_property(
        self,
        object_id: str,
        property_path: str,
        value: Any,
    ) -> bool:
        if self.bridge is None or object_id.startswith("system:"):
            return False
        obj = self.bridge.get_object(object_id)
        if obj is None:
            return False

        if property_path == "name":
            return self.game.apply_property_edit(object_id, name=str(value))
        if property_path == "enabled":
            return self.game.apply_property_edit(object_id, enabled=bool(value))
        if property_path.startswith("properties."):
            key = property_path.split(".", 1)[1]
            return self.game.apply_property_edit(object_id, properties={key: value})

        properties: dict[str, Any]
        root = property_path.split(".", 1)[0]
        if root == "position":
            properties = {"Position": [obj.position.x, obj.position.y, obj.position.z]}
        elif root == "rotation":
            properties = {"Rotation": [obj.rotation.x, obj.rotation.y, obj.rotation.z]}
        elif root == "size":
            properties = {"Size": [obj.size.x, obj.size.y, obj.size.z]}
        elif property_path == "color":
            properties = {"Color": self._hex_to_rgb(obj.color)}
        elif property_path == "transparency":
            properties = {"Transparency": float(obj.transparency)}
        elif property_path == "material" and "Material" in DEFAULT_PART_PROPERTIES:
            properties = {"Material": str(obj.material)}
        elif property_path == "reflectance" and "Reflectance" in DEFAULT_PART_PROPERTIES:
            properties = {"Reflectance": float(obj.reflectance)}
        elif property_path == "anchored" and "Anchored" in DEFAULT_PART_PROPERTIES:
            properties = {"Anchored": bool(obj.anchored)}
        elif property_path == "can_collide" and "CanCollide" in DEFAULT_PART_PROPERTIES:
            properties = {"CanCollide": bool(obj.can_collide)}
        elif property_path == "cast_shadow" and "CastShadow" in DEFAULT_PART_PROPERTIES:
            properties = {"CastShadow": bool(obj.cast_shadow)}
        elif property_path == "locked" and "Locked" in DEFAULT_PART_PROPERTIES:
            properties = {"Locked": bool(obj.locked)}
        else:
            return False

        return self.game.apply_property_edit(object_id, properties)

    def play(self) -> bool:
        self.game.set_studio_playing(True)
        return True

    def stop(self) -> bool:
        self.game.set_studio_playing(False)
        return True

    def set_transform_mode(self, mode: str) -> bool:
        self.game.set_gizmo_mode(mode)
        return True

    def set_grid_snap(self, value: float) -> bool:
        self.game.set_gizmo_snap(float(value))
        return True

    def new_scene(self) -> bool:
        return False

    def import_scene(self, objects: list[SceneObject]) -> bool:
        return False


class PandaWindowFocusFilter(QObject):
    def __init__(self, container: QWidget, foreign_window: QWindow) -> None:
        super().__init__(container)
        self.container = container
        self.foreign_window = foreign_window

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() in {
            QEvent.Type.MouseButtonPress,
            QEvent.Type.FocusIn,
            QEvent.Type.Enter,
        }:
            self.container.setFocus(Qt.FocusReason.MouseFocusReason)
            self.foreign_window.requestActivate()
        return False


def embed_panda_window(
    studio: StudioMainWindow,
    bridge: EngineBridge,
    game: "MultiplayerGame",
) -> bool:
    panda_window = getattr(application.base, "win", None)
    if panda_window is None:
        bridge.log("error", "Panda3D did not create a graphics window.")
        return False

    try:
        handle = int(panda_window.getWindowHandle().getIntHandle())
    except Exception as error:
        bridge.log("error", f"Cannot read Panda3D native window handle: {error}")
        return False

    if handle == 0:
        bridge.log("error", "Panda3D returned native window handle 0; embedding is unavailable.")
        return False

    foreign_window = QWindow.fromWinId(handle)
    if foreign_window is None:
        bridge.log("error", "Qt could not wrap the Panda3D native window.")
        return False

    container = QWidget.createWindowContainer(foreign_window)
    container.setMinimumSize(640, 360)
    container.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    container.setMouseTracking(True)

    focus_filter = PandaWindowFocusFilter(container, foreign_window)
    container.installEventFilter(focus_filter)

    studio._panda_foreign_window = foreign_window
    studio._panda_window_container = container
    studio._panda_focus_filter = focus_filter
    studio.install_engine_viewport(container)
    game.set_qt_viewport_container(container)
    bridge.log("info", f"Ursina viewport embedded. Native handle: {handle}")
    return True


# ============================================================
# ЗАПУСК STUDIO + URSINA
# ============================================================

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pick A Door Studio: Qt editor + Ursina multiplayer client"
    )
    parser.add_argument(
        "--server",
        default=DEFAULT_SERVER_URL,
        help="Адрес игрового WebSocket-сервера",
    )
    parser.add_argument("--name", default="Player", help="Имя игрока")
    parser.add_argument(
        "--no-embed",
        action="store_true",
        help="Не встраивать окно Ursina внутрь Studio (режим диагностики)",
    )
    return parser.parse_args()


def main() -> int:
    # На большинстве Windows-машин консоль по умолчанию НЕ в UTF-8 (обычно
    # cp1251/cp1252), а в коде много print() с кириллицей — без этого первый
    # же такой print роняет процесс с UnicodeEncodeError ещё до открытия
    # окна. sys.stdout/stderr может быть None в --windowed сборке PyInstaller
    # (нет консоли вообще) — тогда reconfigure просто пропускаем.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    arguments = parse_arguments()

    qt_app = QApplication.instance() or QApplication(sys.argv)
    qt_app.setApplicationName("Pick A Door Studio")
    qt_app.setOrganizationName("LegitsEngine")
    qt_app.setStyleSheet(DARK_STYLE)

    application.asset_folder = ASSETS_DIR
    ursina_app = Ursina(
        title="Pick A Door Viewport",
        borderless=True,
        fullscreen=False,
        size=(1100, 700),
        development_mode=False,
        editor_ui_enabled=False,
        vsync=True,
    )

    if SIMPLEPBR_AVAILABLE:
        simplepbr.init()
        print("[MODEL] simplepbr инициализирован")
    else:
        print(
            "[MODEL] simplepbr не установлен. Установи: "
            "python -m pip install panda3d-simplepbr"
        )

    window.title = "Pick A Door Viewport"
    window.borderless = True
    window.fullscreen = False
    if hasattr(window, "exit_button"):
        window.exit_button.visible = False
    if hasattr(window, "fps_counter"):
        window.fps_counter.enabled = True
    window.color = SKY_COLOR

    game = MultiplayerGame(
        server_url=arguments.server,
        player_name=arguments.name,
    )

    bridge = EngineBridge(objects=[], live_mode=True)
    adapter = MultiplayerStudioAdapter(game)
    bridge.set_adapter(adapter)

    studio = StudioMainWindow(bridge=bridge)
    studio.setWindowTitle("Live Server — Pick A Door Studio")
    studio.viewport_frame.scene_title.setText("Live Server")

    embedded = False
    if not arguments.no_embed:
        embedded = embed_panda_window(studio, bridge, game)
    if not embedded:
        bridge.log(
            "warning",
            "Viewport is running as a separate Panda3D window. "
            "The editor remains connected to the same live client.",
        )
        window.borderless = False
        try:
            application.base.win.requestProperties(window)
        except Exception:
            pass

    panda_timer = QTimer()
    panda_timer.setTimerType(Qt.TimerType.PreciseTimer)
    panda_timer.setInterval(8)
    panda_timer.timeout.connect(ursina_app.step)
    panda_timer.start()

    shutdown_state = {"done": False}

    def shutdown() -> None:
        if shutdown_state["done"]:
            return
        shutdown_state["done"] = True
        panda_timer.stop()
        game.shutdown()
        try:
            application.base.destroy()
        except Exception as error:
            print(f"[SHUTDOWN] Panda3D cleanup warning: {error}")

    studio.set_shutdown_callback(shutdown)
    qt_app.aboutToQuit.connect(shutdown)
    studio.show()
    bridge.stop()

    return qt_app.exec()


if __name__ == "__main__":
    raise SystemExit(main())