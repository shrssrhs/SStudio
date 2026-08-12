from __future__ import annotations

import argparse
import asyncio
import builtins
import json
import math
import queue
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

from build_ui import ExplorerPanel, InspectorPanel
from shared import protocol
from shared.instance import DEFAULT_PART_PROPERTIES

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
# ОСНОВНОЙ КОНТРОЛЛЕР ИГРЫ
# ============================================================

class MultiplayerGame(Entity):
    def __init__(self, server_url: str, player_name: str) -> None:
        super().__init__()

        self.server_url = server_url
        self.player_name = player_name

        self.network = NetworkClient(
            server_url=server_url,
            player_name=player_name,
        )

        self.local_player_id: str | None = None
        self.remote_players: dict[str, RemotePlayer] = {}

        # Часть мира (Instance.id -> Entity). Сервер — источник правды:
        # клиент никогда не создаёт Entity сразу по нажатию E, только
        # по подтверждению part_created, пришедшему обратно с сервера.
        self.parts: dict[str, Entity] = {}

        # Build-режим (Explorer/Inspector), как переключение между
        # игрой и редактором в Roblox Studio (Play/Edit).
        self.edit_mode = False
        self.selected_part_id: str | None = None
        self.selection_highlight: Entity | None = None

        self.player_yaw = 0.0
        self.player_pitch = 0.0
        self.last_send_time = 0.0

        # ------------------------------------------------------------
        # ИСПРАВЛЕНИЕ: раньше у локального игрока не было ни одной
        # PlayerVisual — модель показывалась ТОЛЬКО у RemotePlayer,
        # то есть у чужих подключений. При тесте одним клиентом модель
        # физически некому было рендерить. Теперь у своего игрока тоже
        # есть визуал, но по умолчанию скрыт (первое лицо), и его можно
        # включить клавишей V для проверки, что GLB реально грузится.
        # ------------------------------------------------------------
        self.third_person_enabled = False

        self.create_world()
        self.create_first_person_player()
        self.create_local_visual()
        self.create_interface()
        self.create_build_ui()

        mouse.locked = True
        mouse.visible = False

        self.network.start()

    # --------------------------------------------------------
    # МИР
    # --------------------------------------------------------

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

        for x, y, z, scale_x, scale_y, scale_z in blocks:
            Entity(
                model="cube",
                color=BLOCK_COLOR,
                position=(x, y, z),
                scale=(scale_x, scale_y, scale_z),
                collider="box",
            )

        ambient_light = AmbientLight()
        ambient_light.color = AMBIENT_LIGHT_COLOR

        sun = DirectionalLight()
        sun.color = SUN_LIGHT_COLOR
        sun.look_at(Vec3(1, -1, -1))

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
        self.status_text = Text(
            text="Запуск сети...",
            position=(-0.87, 0.46),
            origin=(-0.5, 0.5),
            background=True,
        )

        self.players_text = Text(
            text="Игроков: 1",
            position=(-0.87, 0.405),
            origin=(-0.5, 0.5),
            background=True,
        )

        Text(
            text=(
                "WASD — полёт\n"
                "Space / Ctrl — вверх и вниз\n"
                "Shift — ускорение\n"
                "Мышь — обзор\n"
                "V — от третьего лица (проверка модели)\n"
                "B — сменить фон (небо/сетка)\n"
                "E — заспавнить кубик перед собой\n"
                "Tab — режим построения (Explorer/Inspector)\n"
                "Esc — освободить мышь"
            ),
            position=(-0.87, 0.31),
            origin=(-0.5, 0.5),
            background=True,
        )

        self.edit_mode_text = Text(
            text="Режим: ПОЛЁТ (Tab)",
            position=(-0.87, 0.17),
            origin=(-0.5, 0.5),
            background=True,
        )

        self.background_status_text = Text(
            text="Фон: небо (B)",
            position=(-0.87, 0.245),
            origin=(-0.5, 0.5),
            background=True,
        )

        file_ok, file_message = model_file_status()
        self.model_status_text = Text(
            text=file_message,
            color=color.lime if file_ok else color.red,
            position=(-0.87, -0.35),
            origin=(-0.5, 0.5),
            background=True,
        )

        if not SIMPLEPBR_AVAILABLE:
            Text(
                text="simplepbr не установлен: pip install panda3d-simplepbr (модель будет чёрной)",
                color=color.orange,
                position=(-0.87, -0.41),
                origin=(-0.5, 0.5),
                background=True,
            )

        Entity(
            parent=camera.ui,
            model="quad",
            color=color.rgba32(255, 255, 255, 200),
            scale=0.006,
        )

    # --------------------------------------------------------
    # BUILD-РЕЖИМ (Explorer / Inspector)
    # --------------------------------------------------------

    def create_build_ui(self) -> None:
        self.explorer = ExplorerPanel(on_select=self.select_part)

        self.inspector = InspectorPanel(
            on_apply=self.apply_property_edit,
            on_delete=self.delete_selected_part,
        )

    def toggle_edit_mode(self) -> None:
        self.edit_mode = not self.edit_mode

        # В build-режиме мышь свободна для клика по частям/UI, полёт
        # клавишами (WASD) продолжает работать — так же, как камера
        # в Roblox Studio не следует за мышью, пока не зажат ПКМ.
        mouse.locked = not self.edit_mode
        mouse.visible = self.edit_mode

        self.explorer.enabled = self.edit_mode
        self.inspector.enabled = self.edit_mode

        self.edit_mode_text.text = (
            "Режим: ПОСТРОЙКА (Tab)" if self.edit_mode else "Режим: ПОЛЁТ (Tab)"
        )

        if not self.edit_mode:
            self.deselect_part()

    def refresh_explorer(self) -> None:
        items = [
            (part_id, f"{entity.instance_name}  ({part_id[:6]})")
            for part_id, entity in self.parts.items()
        ]
        self.explorer.refresh(items)

    def select_part(self, part_id: str) -> None:
        entity = self.parts.get(part_id)

        if entity is None:
            return

        self.selected_part_id = part_id
        self.inspector.show(
            part_id,
            entity.instance_name,
            entity.instance_properties,
        )
        self.update_selection_highlight(entity)

    def deselect_part(self) -> None:
        self.selected_part_id = None
        self.inspector.clear()

        if self.selection_highlight is not None:
            destroy(self.selection_highlight)
            self.selection_highlight = None

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
        )

    def handle_edit_mode_click(self) -> None:
        hovered = mouse.hovered_entity

        if hovered is None:
            return

        part_id = getattr(hovered, "part_id", None)

        if part_id is not None:
            self.select_part(part_id)

    def apply_property_edit(self, part_id: str, properties: dict[str, Any]) -> None:
        # Клиент не меняет Entity локально сам — ждёт подтверждения
        # part_updated от сервера, чтобы все игроки видели одно и то же.
        self.network.send(
            {
                "type": protocol.UPDATE_PROPERTY,
                "id": part_id,
                "properties": properties,
            }
        )

    def delete_selected_part(self, part_id: str) -> None:
        self.network.send(
            {
                "type": protocol.DELETE_PART,
                "id": part_id,
            }
        )
        self.deselect_part()

    # --------------------------------------------------------
    # ВВОД
    # --------------------------------------------------------

    def input(self, key: str) -> None:
        if key == "tab":
            self.toggle_edit_mode()
            return

        if self.edit_mode:
            if key == "left mouse down":
                self.handle_edit_mode_click()
            return

        if key == "escape":
            mouse.locked = not mouse.locked
            mouse.visible = not mouse.locked

        elif key == "left mouse down" and not mouse.locked:
            mouse.locked = True
            mouse.visible = False

        elif key == "v":
            self.toggle_third_person()

        elif key == "b":
            self.toggle_background()

        elif key == "e":
            self.spawn_part_in_front_of_camera()

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
                self.status_text.text = str(message.get("message", ""))

            elif message_type == "network_error":
                self.status_text.text = (
                    "Ошибка сети: " + str(message.get("message", "неизвестно"))
                )

            elif message_type == protocol.WORLD_SNAPSHOT:
                parts = message.get("parts", [])
                if isinstance(parts, list):
                    self.load_world_snapshot(parts)

            elif message_type == protocol.PART_CREATED:
                part_data = message.get("part", {})
                if isinstance(part_data, dict):
                    self.spawn_part_entity(part_data)

            elif message_type == protocol.PART_UPDATED:
                part_id = str(message.get("id", ""))
                properties = message.get("properties", {})
                if isinstance(properties, dict):
                    self.update_part_entity(part_id, properties)

            elif message_type == protocol.PART_DELETED:
                part_id = str(message.get("id", ""))
                self.remove_part_entity(part_id)

    def load_world_snapshot(self, parts: list[dict[str, Any]]) -> None:
        for part_data in parts:
            if isinstance(part_data, dict):
                self.spawn_part_entity(part_data)

    def spawn_part_entity(self, part_data: dict[str, Any]) -> None:
        part_id = str(part_data.get("id", ""))

        if not part_id or part_id in self.parts:
            return

        raw_properties = part_data.get("properties", {})
        if not isinstance(raw_properties, dict):
            raw_properties = {}

        # Мержим с дефолтами — сервер обычно шлёт полный набор свойств,
        # но подстрахуемся на случай частичных данных.
        properties = {**DEFAULT_PART_PROPERTIES, **raw_properties}

        position = properties["Position"]
        size = properties["Size"]
        rotation = properties["Rotation"]
        rgb = properties["Color"]
        transparency = properties["Transparency"]

        try:
            entity = Entity(
                model="cube",
                position=Vec3(float(position[0]), float(position[1]), float(position[2])),
                scale=Vec3(float(size[0]), float(size[1]), float(size[2])),
                rotation=Vec3(float(rotation[0]), float(rotation[1]), float(rotation[2])),
                color=color.rgba32(
                    int(rgb[0]),
                    int(rgb[1]),
                    int(rgb[2]),
                    int(255 * (1.0 - float(transparency))),
                ),
                collider="box",
            )
        except (TypeError, ValueError, IndexError):
            print(f"[WORLD] Пропущена часть {part_id}: битые properties")
            return

        entity.part_id = part_id
        entity.instance_name = str(part_data.get("name", "Part"))
        entity.instance_properties = properties

        self.parts[part_id] = entity
        self.refresh_explorer()

    def update_part_entity(
        self,
        part_id: str,
        properties: dict[str, Any],
    ) -> None:
        entity = self.parts.get(part_id)

        if entity is None:
            return

        if "Position" in properties:
            position = properties["Position"]
            entity.position = Vec3(
                float(position[0]),
                float(position[1]),
                float(position[2]),
            )

        if "Size" in properties:
            size = properties["Size"]
            entity.scale = Vec3(
                float(size[0]),
                float(size[1]),
                float(size[2]),
            )

        if "Rotation" in properties:
            rotation = properties["Rotation"]
            entity.rotation = Vec3(
                float(rotation[0]),
                float(rotation[1]),
                float(rotation[2]),
            )

        if "Color" in properties or "Transparency" in properties:
            rgb = properties.get("Color", [255, 255, 255])
            transparency = properties.get("Transparency", 0.0)
            entity.color = color.rgba32(
                int(rgb[0]),
                int(rgb[1]),
                int(rgb[2]),
                int(255 * (1.0 - float(transparency))),
            )

        entity.instance_properties.update(properties)

        if part_id == self.selected_part_id:
            self.inspector.show(
                part_id,
                entity.instance_name,
                entity.instance_properties,
            )
            self.update_selection_highlight(entity)

    def remove_part_entity(self, part_id: str) -> None:
        entity = self.parts.pop(part_id, None)

        if entity is not None:
            destroy(entity)

        if part_id == self.selected_part_id:
            self.deselect_part()

        self.refresh_explorer()

    def spawn_part_in_front_of_camera(self) -> None:
        if not self.network.connected_event.is_set():
            return

        forward_direction = forward_from_angles(
            self.player_yaw,
            self.player_pitch,
        )

        spawn_position = (
            self.local_player.position
            + Vec3(0, EYE_HEIGHT, 0)
            + forward_direction * PART_SPAWN_DISTANCE
        )

        self.network.send(
            {
                "type": protocol.CREATE_PART,
                "properties": {
                    "Position": [
                        round(float(spawn_position.x), 3),
                        round(float(spawn_position.y), 3),
                        round(float(spawn_position.z), 3),
                    ],
                    "Size": PART_DEFAULT_SIZE,
                    "Color": PART_DEFAULT_COLOR,
                },
            }
        )

    def handle_connected_message(self, message: dict[str, Any]) -> None:
        self.local_player_id = str(message.get("player_id", ""))
        player_data = message.get("player", {})

        if isinstance(player_data, dict):
            position_data = player_data.get("position", [0, 3, 0])

            try:
                self.local_player.position = Vec3(
                    float(position_data[0]),
                    float(position_data[1]),
                    float(position_data[2]),
                )
            except (TypeError, ValueError, IndexError):
                pass

        self.status_text.text = f"Подключено: {self.local_player_id}"

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
        self.update_mouse_look()
        self.update_flight()
        self.send_transform()
        self.update_remote_smoothing()
        self.update_sky()

    def shutdown(self) -> None:
        self.network.stop()


# ============================================================
# ЗАПУСК
# ============================================================

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Клиент мультиплеерного 3D-прототипа"
    )

    parser.add_argument(
        "--server",
        default=DEFAULT_SERVER_URL,
        help="Адрес игрового сервера (по умолчанию зашит в коде — "
        "друзьям не нужно ничего вводить)",
    )

    parser.add_argument(
        "--name",
        default="Player",
        help="Имя игрока",
    )

    return parser.parse_args()


def main() -> None:
    arguments = parse_arguments()

    application.asset_folder = ASSETS_DIR

    app = Ursina()

    if SIMPLEPBR_AVAILABLE:
        # Должен вызываться после создания Ursina()/ShowBase, но до
        # загрузки моделей — ставит корректный PBR-шейдер на всю сцену.
        simplepbr.init()
        print("[MODEL] simplepbr инициализирован — PBR-материалы будут рендериться корректно")
    else:
        print(
            "[MODEL] simplepbr не установлен — PBR-модели (в т.ч. player.glb) "
            "будут рендериться чёрными. Установи: pip install panda3d-simplepbr"
        )

    window.title = "Pick A Door Multiplayer Prototype"
    window.borderless = False
    window.exit_button.visible = False
    window.fps_counter.enabled = True
    window.color = SKY_COLOR

    game = MultiplayerGame(
        server_url=arguments.server,
        player_name=arguments.name,
    )

    try:
        app.run()
    finally:
        game.shutdown()


if __name__ == "__main__":
    main()