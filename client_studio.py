from __future__ import annotations

import argparse
import asyncio
import builtins
import ctypes
import json
import math
import queue
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from panda3d.core import Filename, Quat, TransparencyAttrib
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
import editor_history
import lua_runtime
import physics
from shared import object_registry, protocol
from shared.instance import DEFAULT_PART_PROPERTIES, MIN_PART_SIZE
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
# WIN32-ИНТРОСПЕКЦИЯ (только для диагностики embedding-геометрии)
#
# Проект работает только на Windows (см. требования задачи) — прямой
# ctypes-вызов user32 здесь оправдан и не требует кроссплатформенных
# обходов. Используется исключительно для верификации, что нативное
# Panda3D-окно ДЕЙСТВИТЕЛЬНО является WS_CHILD дочерним окном Qt-контейнера
# (а не осталось top-level/WS_POPUP поверх интерфейса) — см. разбор бага
# в MultiplayerGame._sync_panda_window_to_container.
# ============================================================

GWL_STYLE = -16
WS_CHILD = 0x40000000
WS_POPUP = 0x80000000

SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def _win32_window_info(hwnd: int) -> dict[str, Any]:
    """Возвращает GetParent/GWL_STYLE/GetWindowRect/GetClientRect для hwnd.
    Не бросает исключений наружу — это диагностика, а не критический путь."""
    if not hwnd:
        return {"error": "no hwnd"}

    try:
        user32 = ctypes.windll.user32
        parent = user32.GetParent(ctypes.c_void_p(hwnd))

        get_style = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
        style = get_style(ctypes.c_void_p(hwnd), GWL_STYLE)
        # GetWindowLongW может вернуть отрицательное значение как signed —
        # приводим к unsigned 32 бита для корректной проверки битовых флагов.
        style &= 0xFFFFFFFF

        window_rect = _RECT()
        user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(window_rect))
        client_rect = _RECT()
        user32.GetClientRect(ctypes.c_void_p(hwnd), ctypes.byref(client_rect))

        return {
            "hwnd": hwnd,
            "parent_hwnd": int(parent) if parent else None,
            "is_child": bool(style & WS_CHILD),
            "is_popup": bool(style & WS_POPUP),
            "window_rect": (window_rect.left, window_rect.top, window_rect.right, window_rect.bottom),
            "client_rect": (client_rect.left, client_rect.top, client_rect.right, client_rect.bottom),
        }
    except Exception as error:
        return {"error": str(error)}


def _force_native_child_parenting(native_hwnd: int, parent_hwnd: int, width: int, height: int) -> dict[str, Any]:
    """
    QWindow.fromWinId(handle) + QWidget.createWindowContainer(...) is
    documented Qt API for embedding a foreign native window, but measured
    with _win32_window_info() this project's Panda3D window stayed a
    top-level WS_POPUP with parent_hwnd=None the whole time — Qt's own
    reparenting silently didn't take effect for this window. That is the
    actual cause of the viewport rendering at the wrong screen position:
    Panda3D kept moving a top-level popup to chase the container's SCREEN
    coordinates each time WindowProperties got reapplied (see
    MultiplayerGame._sync_panda_window_to_container), instead of simply
    living at local (0, 0) inside a real child window.

    This does the reparenting ourselves at the Win32 level: clear WS_POPUP,
    set WS_CHILD, SetParent() to the container's HWND, then position at
    local (0, 0) with SWP_FRAMECHANGED so Windows re-evaluates the changed
    style. Called once, right after embedding — not on every frame.
    """
    if not native_hwnd or not parent_hwnd:
        return {"error": "missing hwnd"}

    try:
        user32 = ctypes.windll.user32
        get_style = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
        set_style = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW

        style = get_style(ctypes.c_void_p(native_hwnd), GWL_STYLE) & 0xFFFFFFFF
        style = (style & ~WS_POPUP) | WS_CHILD
        set_style(ctypes.c_void_p(native_hwnd), GWL_STYLE, ctypes.c_long(style))

        user32.SetParent(ctypes.c_void_p(native_hwnd), ctypes.c_void_p(parent_hwnd))

        user32.SetWindowPos(
            ctypes.c_void_p(native_hwnd),
            None,
            0, 0, int(width), int(height),
            SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW,
        )

        return _win32_window_info(native_hwnd)
    except Exception as error:
        return {"error": str(error)}


def _reposition_native_child(native_hwnd: int, width: int, height: int) -> None:
    """Lightweight resize/reposition for a window ALREADY confirmed to be a
    real WS_CHILD of the right parent — plain SetWindowPos, no SetParent, no
    GWL_STYLE change, no Panda3D requestProperties() call. Deliberately not
    routed through Panda3D's Python API: that's what was causing Panda3D to
    silently reset the window back to a parentless top-level popup on every
    call (see _sync_panda_window_to_container). A same-parent SetWindowPos
    doesn't touch the window's parent/style, so it doesn't trigger that."""
    if not native_hwnd:
        return
    try:
        ctypes.windll.user32.SetWindowPos(
            ctypes.c_void_p(native_hwnd),
            None,
            0, 0, int(width), int(height),
            SWP_NOZORDER | SWP_NOACTIVATE,
        )
    except Exception:
        pass


# ------------------------------------------------------------
# ПОЛНАЯ ИНВЕНТАРИЗАЦИЯ (диагностика дублирующегося render-surface)
#
# Не предполагаем, что один HWND = одна видимая картинка. Здесь мы
# перечисляем buквально ВСЁ: все GraphicsOutput у Panda3D (не только
# base.win) и все окна процесса на уровне Win32 — чтобы доказать, что
# именно дублируется, а не гадать.
# ------------------------------------------------------------

GWL_EXSTYLE = -20
DWMWA_CLOAKED = 14


def _dump_panda_graphics_outputs() -> list[dict[str, Any]]:
    """Каждый GraphicsOutput, который знает graphicsEngine — не только
    application.base.win. Включает offscreen-буферы (у них getWindowHandle()
    вернёт None/ошибку — это ожидаемо, не баг)."""
    engine = getattr(application.base, "graphicsEngine", None)
    if engine is None:
        return [{"error": "no graphicsEngine"}]

    results: list[dict[str, Any]] = []
    try:
        count = engine.getNumWindows()
    except Exception as error:
        return [{"error": f"getNumWindows failed: {error}"}]

    for index in range(count):
        try:
            output = engine.getWindow(index)
        except Exception as error:
            results.append({"index": index, "error": str(error)})
            continue

        entry: dict[str, Any] = {
            "index": index,
            "type": type(output).__name__,
            "is_base_win": output == getattr(application.base, "win", None),
            "size": None,
            "hwnd": None,
            "active": None,
            "display_regions": [],
        }
        try:
            entry["size"] = (output.getXSize(), output.getYSize())
        except Exception:
            pass
        try:
            entry["active"] = bool(output.isActive())
        except Exception:
            pass
        try:
            handle = output.getWindowHandle()
            if handle is not None:
                entry["hwnd"] = int(handle.getIntHandle())
        except Exception:
            pass
        try:
            for region_index in range(output.getNumDisplayRegions()):
                region = output.getDisplayRegion(region_index)
                camera_node = None
                try:
                    camera_path = region.getCamera()
                    if camera_path and not camera_path.isEmpty():
                        camera_node = str(camera_path)
                except Exception:
                    pass
                entry["display_regions"].append({
                    "index": region_index,
                    "active": bool(region.isActive()) if hasattr(region, "isActive") else None,
                    "camera": camera_node,
                })
        except Exception:
            pass

        results.append(entry)

    return results


def _enumerate_process_windows() -> list[dict[str, Any]]:
    """Каждое окно (top-level + дочерние, рекурсивно), принадлежащее
    ТЕКУЩЕМУ процессу — класс, заголовок, parent, стили, rect, видимость,
    DWM-cloaked. Используется, чтобы найти "лишние" HWND-ы, которые не
    всплывают через application.base.win/graphicsEngine напрямую."""
    try:
        user32 = ctypes.windll.user32
        current_pid = ctypes.windll.kernel32.GetCurrentProcessId()

        get_style = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
        collected: dict[int, dict[str, Any]] = {}

        def describe(hwnd: int) -> dict[str, Any]:
            class_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(ctypes.c_void_p(hwnd), class_buf, 256)
            title_buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(ctypes.c_void_p(hwnd), title_buf, 256)

            style = get_style(ctypes.c_void_p(hwnd), GWL_STYLE) & 0xFFFFFFFF
            exstyle = get_style(ctypes.c_void_p(hwnd), GWL_EXSTYLE) & 0xFFFFFFFF
            parent = user32.GetParent(ctypes.c_void_p(hwnd))

            rect = _RECT()
            user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))

            cloaked = ctypes.c_int(0)
            try:
                ctypes.windll.dwmapi.DwmGetWindowAttribute(
                    ctypes.c_void_p(hwnd), DWMWA_CLOAKED,
                    ctypes.byref(cloaked), ctypes.sizeof(cloaked),
                )
            except Exception:
                pass

            return {
                "hwnd": hwnd,
                "class_name": class_buf.value,
                "title": title_buf.value,
                "parent_hwnd": int(parent) if parent else None,
                "is_child": bool(style & WS_CHILD),
                "is_popup": bool(style & WS_POPUP),
                "ex_topmost": bool(exstyle & 0x00000008),  # WS_EX_TOPMOST
                "window_rect": (rect.left, rect.top, rect.right, rect.bottom),
                "visible": bool(user32.IsWindowVisible(ctypes.c_void_p(hwnd))),
                "dwm_cloaked": bool(cloaked.value),
            }

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def enum_child_proc(hwnd, _lparam):
            hwnd_int = int(hwnd) if hwnd else 0
            if hwnd_int and hwnd_int not in collected:
                collected[hwnd_int] = describe(hwnd_int)
            return True

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def enum_top_proc(hwnd, _lparam):
            hwnd_int = int(hwnd) if hwnd else 0
            if not hwnd_int:
                return True
            pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
            if pid.value != current_pid:
                return True
            if hwnd_int not in collected:
                collected[hwnd_int] = describe(hwnd_int)
            user32.EnumChildWindows(ctypes.c_void_p(hwnd), enum_child_proc, 0)
            return True

        user32.EnumWindows(enum_top_proc, 0)
        return list(collected.values())
    except Exception as error:
        return [{"error": str(error)}]


def _log_full_embedding_inventory(label: str) -> None:
    print(f"[VIEWPORT_INVENTORY] ===== {label} =====")

    outputs = _dump_panda_graphics_outputs()
    print(f"[VIEWPORT_INVENTORY] Panda3D GraphicsOutputs ({len(outputs)}):")
    for entry in outputs:
        print(f"[VIEWPORT_INVENTORY]   {entry}")

    windows = _enumerate_process_windows()
    print(f"[VIEWPORT_INVENTORY] Process HWNDs ({len(windows)}):")
    for entry in windows:
        print(f"[VIEWPORT_INVENTORY]   {entry}")

    print(f"[VIEWPORT_INVENTORY] ===== end {label} =====")


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

# Логирует container/native-window геометрию вокруг событий, которые, как
# выяснилось, портят размер встроенного Panda3D-окна (см. коммент у
# MultiplayerGame._sync_panda_window_to_container). Временный диагностический
# флаг — включать вручную для отладки, в проде должен быть False.
DEBUG_VIEWPORT_GEOMETRY = False

# Считает частоту (Hz) каждого этапа конвейера трансформации гизмо за одну
# сессию перетаскивания (mouse down -> mouse up) и печатает сводку по
# отпусканию кнопки. Не печатает ничего, пока флаг выключен — включать
# вручную для отладки, в проде должен быть False.
DEBUG_GIZMO_TIMING = False


class _GizmoTimingProbe:
    """Собирает частоты событий и длительности за одну gizmo-drag сессию.
    Каждый tick()/record_duration_ms() — это одна операция dict/list, дешёвая
    даже если бы вызывалась без гейта; begin()/end_and_report() дополнительно
    вызываются только когда DEBUG_GIZMO_TIMING включён."""

    def __init__(self) -> None:
        self.active = False
        self._start = 0.0
        self._counts: dict[str, int] = {}
        self._durations_ms: list[float] = []

    def begin(self) -> None:
        self.active = True
        self._start = time.perf_counter()
        self._counts = {}
        self._durations_ms = []

    def tick(self, name: str) -> None:
        if not self.active:
            return
        self._counts[name] = self._counts.get(name, 0) + 1

    def record_duration_ms(self, milliseconds: float) -> None:
        if not self.active:
            return
        self._durations_ms.append(milliseconds)

    def end_and_report(self) -> None:
        if not self.active:
            return
        self.active = False
        elapsed = max(1e-6, time.perf_counter() - self._start)
        print(f"[GIZMO_TIMING] drag session: {elapsed * 1000:.0f} ms")
        for name in sorted(self._counts):
            count = self._counts[name]
            print(f"[GIZMO_TIMING]   {name}: {count} events, {count / elapsed:.1f} Hz")
        if self._durations_ms:
            print(
                f"[GIZMO_TIMING]   _apply_gizmo_result duration (ms): "
                f"min={min(self._durations_ms):.3f} "
                f"avg={sum(self._durations_ms) / len(self._durations_ms):.3f} "
                f"max={max(self._durations_ms):.3f}"
            )


# Логирует запросы/подтверждения/отклонения реродителения (Stage 2.1
# Explorer drag-and-drop). Временный диагностический флаг — включать
# вручную для отладки, в проде должен быть False.
DEBUG_REPARENTING = False

# Stage 2.2: диагностика Model pivot + каскадного transform'а потомков.
# Логирует частоту локального обновления гизмо, число трансформируемых
# потомков, время на квaternion-математику/обновление Entity/bridge-sync,
# частоту сетевой отправки/эха. Временный флаг — по умолчанию False.
DEBUG_MODEL_TRANSFORMS = False

# Stage 2.3: диагностика Scale-гизмо. Одиночный Part resize уже покрыт
# существующим DEBUG_GIZMO_TIMING (те же tick()-точки: entity_transform/
# bridge_sync/network_send/apply_gizmo_result — см. _apply_scale_gizmo_result).
# Этот флаг — отдельная печать для Model uniform-scale каскада (число
# трансформируемых потомков + время на factor-математику/Entity-обновление/
# bridge-sync/сетевую отправку за кадр), по образцу DEBUG_MODEL_TRANSFORMS,
# но для _apply_model_scale_result. По умолчанию False, не печатает ничего
# в проде.
DEBUG_SCALE_GIZMO = False

# Stage 2.4 cleanup: prints git HEAD / running-source-path proof at
# startup (see _print_startup_diagnostics) — was unconditional during the
# uniform-scale bug investigation, now gated off by default like every
# other DEBUG_* flag here.
DEBUG_STARTUP = False

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
        self.qt_viewport_foreign_window: QWindow | None = None
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
        # Instance id, активно перетаскиваемый гизмо ПРЯМО СЕЙЧАС (или None).
        # См. update_instance() — пока не None, входящие PART_UPDATED для
        # этого id не позволяют устаревшему серверному эхо откатить
        # Position/Rotation назад поверх уже более новой локальной позиции.
        self._gizmo_dragging_instance_id: str | None = None
        self._gizmo_timing = _GizmoTimingProbe()

        # Stage 2.2: Model pivot + каскадный transform потомков.
        #
        # model_gizmo_proxy — невидимая Entity (нет model=), к которой
        # гизмо цепляется, когда выбран Model: TransformGizmo умеет тащить
        # только Entity.position/.rotation, ничего не зная про Model —
        # никаких изменений в transform_gizmo.py не потребовалось.
        #
        # transform_scratch — вторая невидимая Entity, используемая ТОЛЬКО
        # как конвертер Euler XYZ <-> Panda Quat через реальный
        # NodePath.set_quat()/.rotation (Ursina), а не через самостоятельно
        # выведенную формулу — так гарантированно совпадает с тем, как
        # Ursina реально держит ориентацию (см. ROTATION_SIGN в
        # transform_gizmo.py: Ursina уже имеет неочевидные знаковые правила
        # на этих осях, повторять их вручную рискованно).
        self.model_gizmo_proxy = Entity(eternal=True)
        self._transform_scratch = Entity(eternal=True)

        # Stage 2.4: Play Mode runtime physics (see physics.py). Exists
        # only between set_studio_playing(True) and set_studio_playing
        # (False) — recreated fresh every Play, fully torn down on every
        # Stop, never persisted. _physics_snapshot holds each simulated
        # Part's exact pre-Play Position/Rotation/Size (properties dict
        # is never mutated by physics itself, but the snapshot is kept
        # explicit rather than relying on that as an invariant — see
        # Stage 2.4 report, "Play/Stop snapshot restoration").
        self._physics_world: physics.PhysicsWorld | None = None
        self._physics_snapshot: dict[str, dict[str, list[float]]] = {}

        # Stage 3.0: sandboxed Lua runtime (see lua_runtime.py). Exists
        # only between set_studio_playing(True) and set_studio_playing
        # (False), created AFTER physics (it binds to self._physics_world)
        # and stopped BEFORE physics on the way out -- Lua must finish
        # cancelling every task and restoring its own overlay before
        # anything else starts tearing the Play session down, so no
        # coroutine can mutate the scene mid-cleanup (see Stage 3.0
        # report).
        self._lua_runtime: lua_runtime.LuaRuntimeManager | None = None

        # Stage 2.5: authoritative Undo/Redo command history (see
        # editor_history.py for the full design). Sequence counter here is
        # deliberately SEPARATE from _model_drag_sequence_counter below —
        # that one guards live-drag echo suppression frame-by-frame; this
        # one is only ever used when history itself re-sends a Model
        # transform (Undo/Redo), a completely independent event.
        self.history = editor_history.CommandManager(self)
        self._history_transform_sequence_counter = 0
        # Set at the start of a gizmo drag (Part or Model), consumed at
        # end_gizmo_drag() to build the before/after command — never
        # holds Qt widgets or Entities, only plain ids/lists (see Stage
        # 2.5 report, "Commands should reference stable instance IDs and
        # serializable scene data").
        self._history_drag_before: dict[str, Any] | None = None

        # Состояние активного каскадного Model-drag (None вне драга).
        # "descendant_relative" — id -> (relative_pos: Vec3, relative_quat:
        # Quat), захваченные в момент begin_drag: смещение/ориентация
        # потомка В ЛОКАЛЬНОЙ РАМКЕ пивота на старте драга. Это НЕ
        # персистентное поле — чисто временный снэпшот на время одного
        # драга (см. отчёт Stage 2.2, раздел про local/world дизайн).
        self._model_drag_state: dict[str, Any] | None = None
        self._model_drag_last_network_send = 0.0
        # id потомков (и самой Model), чьё серверное эхо MODEL_TRANSFORMED
        # подавляется, пока локальный драг активен — тот же приём, что и
        # _gizmo_dragging_instance_id для одиночного Part, но на весь
        # набор объектов. Одного набора id недостаточно для releases:
        # см. _model_drag_sequence/_model_drag_awaiting_final ниже — эти
        # id остаются защищены ПОСЛЕ отпускания мыши, пока не придёт именно
        # финальный подтверждённый ответ (иначе устаревшее эхо более
        # раннего throttled-кадра, ещё в полёте, может откатить превью
        # назад на видимый кадр — ровно то, что требовалось исправить).
        self._model_drag_suppressed_ids: set[str] = set()
        # Монотонный счётчик на один драг (сбрасывается в
        # _begin_model_drag_capture) — каждый отправленный TRANSFORM_MODEL
        # несёт следующее значение, сервер отражает его в ответе не глядя.
        self._model_drag_sequence_counter = 0
        # Заполняется в конце end_gizmo_drag/request_transform_model_pivot
        # финальным (force_network=True) отправленным sequence — только
        # получив MODEL_TRANSFORMED/TRANSFORM_MODEL_REJECTED с ЭТИМ же
        # sequence для этого model_id разрешаем снять защиту.
        self._model_drag_awaiting_final: dict[str, Any] | None = None
        self._model_selection_highlights: list[Entity] = []

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
            self._start_physics()
            self._start_lua()
            self.history.refresh_ui()
        else:
            if preserve_position and self.studio_playing:
                self.saved_play_position = Vec3(self.local_player.position)
                self.saved_play_yaw = float(self.player_yaw)
                self.saved_play_pitch = float(self.player_pitch)
            was_playing = self.studio_playing
            self.studio_playing = False
            self.editor_look_active = False
            self.hud_root.enabled = False
            self._stop_mouse_look()
            if self.selection_highlight is not None:
                self.selection_highlight.enabled = True
            if was_playing:
                # Lua first: every task/coroutine must be cancelled and
                # its scene overlay restored before physics tears down
                # and the ordinary Position/Rotation/Size restore runs --
                # otherwise a script could still be mid-mutation while
                # the rest of Stop is unwinding the world under it (see
                # Stage 3.0 report, "execution authority model").
                self._stop_lua()
                self._stop_physics()
            self.history.refresh_ui()

    def _start_physics(self) -> None:
        """Builds a fresh PhysicsWorld from the CURRENT editor properties
        of every Part-like instance — always reflects whatever Move/
        Rotate/Scale/Anchored/CanCollide edits happened since the last
        Play, satisfying "the next Play uses the updated collision
        shape" without any separate rebuild-tracking (see Stage 2.4
        report, "collision-shape update strategy"). Disabled instances
        are skipped entirely (nothing to simulate for something not
        rendered).

        Wrapped in try/except that ALWAYS prints a full traceback
        (bug-report follow-up: "Part remains perfectly suspended" in the
        real GUI) — set_studio_playing() is invoked through
        EngineBridge._adapter_call(), which catches any exception here,
        logs one line to the Output panel, and otherwise proceeds as if
        Play succeeded (self.studio_playing is already True by the time
        this runs). Without this, a physics-init exception would be
        completely invisible on the console — Play LOOKS like it
        started (gizmo hidden, HUD up) while zero bodies were ever
        created."""
        if physics.DEBUG_PHYSICS:
            repo_dir = Path(__file__).resolve().parent
            try:
                commit = subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True, stderr=subprocess.STDOUT,
                ).strip()
            except Exception as error:
                commit = f"<git rev-parse failed: {error}>"
            print(f"[PHYSICS] Play pressed. git HEAD={commit}")
            print(f"[PHYSICS] physics module: {Path(physics.__file__).resolve()}")
            print(f"[PHYSICS] studio_playing before={self.studio_playing}")

        self._physics_snapshot = {}
        try:
            self._physics_world = physics.PhysicsWorld()
            body_count = 0
            for instance_id, entity in self.parts.items():
                record = self.instances.get(instance_id)
                if record is None or not record.enabled:
                    if physics.DEBUG_PHYSICS and record is not None:
                        print(f"[PHYSICS] skip id={instance_id} name={record.name!r} (disabled)")
                    continue
                properties = record.properties
                position = properties.get("Position", [0.0, 0.0, 0.0])
                rotation = properties.get("Rotation", [0.0, 0.0, 0.0])
                size = properties.get("Size", [1.0, 1.0, 1.0])
                self._physics_snapshot[instance_id] = {
                    "Position": [float(v) for v in position],
                    "Rotation": [float(v) for v in rotation],
                    "Size": [float(v) for v in size],
                }
                anchored = bool(properties.get("Anchored", True))
                can_collide = bool(properties.get("CanCollide", True))
                if physics.DEBUG_PHYSICS:
                    print(
                        f"[PHYSICS] considering id={instance_id} name={record.name!r} "
                        f"Anchored={anchored} CanCollide={can_collide} "
                        f"Position={position} Rotation={rotation} Size={size}"
                    )
                self._physics_world.add_part(
                    instance_id, entity, position, rotation, size, anchored, can_collide,
                )
                body_count += 1
        except Exception:
            print("[PHYSICS] EXCEPTION in _start_physics() -- Play mode continues but physics did NOT initialize:")
            traceback.print_exc()
            return
        if physics.DEBUG_PHYSICS:
            print(
                f"[PHYSICS] Play start: {body_count} instances handed to physics "
                f"({self._physics_world.body_count()} bodies), "
                f"studio_playing after={self.studio_playing}"
            )

    def _stop_physics(self) -> None:
        """Tears down the physics world completely and restores every
        simulated instance's Entity to its exact pre-Play Position/
        Rotation/Size from the explicit snapshot taken in
        _start_physics() — record.properties (the networked,
        authoritative state) was never touched during Play, so this is
        purely a LOCAL visual restore, not a network round-trip."""
        if self._physics_world is not None:
            self._physics_world.destroy()
            self._physics_world = None

        restored = 0
        for instance_id, snapshot in self._physics_snapshot.items():
            entity = self.parts.get(instance_id)
            record = self.instances.get(instance_id)
            if entity is None or record is None:
                continue
            position = snapshot["Position"]
            rotation = snapshot["Rotation"]
            size = snapshot["Size"]
            entity.position = Vec3(position[0], position[1], position[2])
            entity.rotation = Vec3(rotation[0], rotation[1], rotation[2])
            entity.scale = Vec3(size[0], size[1], size[2])
            restored += 1
        if physics.DEBUG_PHYSICS:
            print(f"[PHYSICS] Play stop: restored {restored} instances to pre-Play transforms")
        self._physics_snapshot = {}

    def update_physics(self) -> None:
        if self._physics_world is None:
            return
        self._physics_world.step(ursina_time.dt)

    def _start_lua(self) -> None:
        """Wrapped in try/except that ALWAYS prints a full traceback, for
        the same reason _start_physics() is: this runs through
        EngineBridge._adapter_call(), which would otherwise swallow the
        exception into a one-line Output log while Play looks like it
        succeeded (see Stage 2.4 report's identical concern for physics)."""
        try:
            self._lua_runtime = lua_runtime.LuaRuntimeManager(self)
            self._lua_runtime.start()
        except Exception:
            print("[LUA_RUNTIME] EXCEPTION in _start_lua() -- Play mode continues but scripts did NOT start:")
            traceback.print_exc()
            self._lua_runtime = None

    def _stop_lua(self) -> None:
        if self._lua_runtime is None:
            return
        try:
            self._lua_runtime.stop()
        except Exception:
            print("[LUA_RUNTIME] EXCEPTION in _stop_lua():")
            traceback.print_exc()
        self._lua_runtime = None

    def update_lua(self) -> None:
        if self._lua_runtime is None:
            return
        try:
            self._lua_runtime.update(ursina_time.dt)
        except Exception:
            print("[LUA_RUNTIME] EXCEPTION in update_lua() -- stopping the Lua runtime for the rest of this Play session:")
            traceback.print_exc()
            self._stop_lua()

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

    def set_qt_viewport_container(self, container: QWidget, foreign_window: QWindow | None = None) -> None:
        self.qt_viewport_container = container
        self.qt_viewport_foreign_window = foreign_window

    def _qt_look_available(self) -> bool:
        return self.qt_viewport_container is not None

    def _mouse_look_captured(self) -> bool:
        if self._qt_look_available():
            return self._qt_look_last_pos is not None
        return mouse.locked

    # --------------------------------------------------------
    # ГЕОМЕТРИЯ ВСТРОЕННОГО ОКНА (см. подробный разбор бага в отчёте задачи)
    #
    # Три слоя бага, найденные по очереди:
    #
    # 1) Стухший SIZE: ursina.window САМ является объектом WindowProperties
    #    (Window наследуется от WindowProperties), общим на весь процесс.
    #    mouse.visible/mouse.locked (ursina/mouse.py) на каждое переключение
    #    делают application.base.win.requestProperties(window) — шлют
    #    Panda3D ВЕСЬ накопленный на этом объекте набор свойств, включая
    #    size от исходного Ursina(size=(1100, 700)).
    #
    # 2) Стухший ORIGIN: ursina сама вызывает window.position = Vec2(x, y)
    #    при старте — это экранные координаты, актуальные только для
    #    top-level окна (до встраивания). После embedding Windows
    #    интерпретирует тот же origin для ДОЧЕРНЕГО окна как координаты
    #    ОТНОСИТЕЛЬНО РОДИТЕЛЯ, а не экрана — те же requestProperties(window)
    #    протаскивают его как локальные координаты, сдвигая вьюпорт.
    #
    # 3) Настоящий корень обоих: полная инвентаризация (_dump_panda_graphics_
    #    outputs + EnumWindows по всему процессу — см. _log_full_embedding_
    #    inventory) показала, что Panda3D GraphicsOutput всегда РОВНО ОДИН —
    #    то есть дублирования рендер-поверхности НЕТ. Но createWindowContainer()
    #    не реально не репарентит нативное окно на этой связке Qt/Windows
    #    (остаётся top-level WS_POPUP, parent_hwnd=None), И КРОМЕ ТОГО —
    #    request_properties() САМ ПО СЕБЕ откатывает уже принудительно
    #    припарентованное окно обратно к тому же состоянию на каждый вызов.
    #    Раньше мы боролись с этим повторным принудительным репарентингом на
    #    каждый requestProperties() — а именно частые SetParent/GWL_STYLE-
    #    вызовы это то, что выглядит на реальном экране как "второй,
    #    сдвинутый рендер" (DWM-артефакт compositing/ghosting при смене
    #    родителя окна, который PrintWindow — наш способ скриншотить —
    #    в принципе не воспроизводит, поэтому мы не видели его на своих
    #    скриншотах).
    #
    # Итоговое решение — не бороться с requestProperties(), а не давать
    # повода её вызывать:
    #  1) mouse.visible/mouse.locked для ВСТРОЕННОГО вьюпорта больше не
    #     трогаем вообще — курсор прячем через Qt (_set_embedded_cursor_hidden),
    #     это единственный runtime-вызывающий requestProperties() код,
    #     которым мы управляем;
    #  2) сами мы тоже не вызываем panda_window.requestProperties() — только
    #     сырой Win32 SetWindowPos (_reposition_native_child), который не
    #     проходит через Panda3D API и потому не запускает сброс;
    #  3) полный (тяжёлый) репарентинг — SetParent + смена стиля — теперь
    #     нужен только один раз, при embed; на resize просто проверяем, что
    #     is_child/parent_hwnd всё ещё верны, и делаем только лёгкий
    #     reposition, если да.
    # --------------------------------------------------------

    def _log_viewport_geometry(self, label: str) -> None:
        if not DEBUG_VIEWPORT_GEOMETRY:
            return

        container = self.qt_viewport_container
        container_geom = container_rect = container_contents_rect = None
        container_global_top_left = None
        parent_geom = None
        device_pixel_ratio = None
        container_hwnd = None
        if container is not None:
            geom = container.geometry()
            container_geom = (geom.x(), geom.y(), geom.width(), geom.height())
            rect = container.rect()
            container_rect = (rect.x(), rect.y(), rect.width(), rect.height())
            contents = container.contentsRect()
            container_contents_rect = (contents.x(), contents.y(), contents.width(), contents.height())
            top_left = container.mapToGlobal(QPoint(0, 0))
            container_global_top_left = (top_left.x(), top_left.y())
            device_pixel_ratio = container.devicePixelRatioF()
            try:
                container_hwnd = int(container.winId())
            except Exception:
                container_hwnd = None
            parent = container.parentWidget()
            if parent is not None:
                pg = parent.geometry()
                parent_geom = (pg.x(), pg.y(), pg.width(), pg.height())

        foreign_geom = None
        if self.qt_viewport_foreign_window is not None:
            fg = self.qt_viewport_foreign_window.geometry()
            foreign_geom = (fg.x(), fg.y(), fg.width(), fg.height())

        native_size = native_origin = None
        native_hwnd = None
        native_parent_info: dict[str, Any] = {}
        panda_window = getattr(application.base, "win", None)
        if panda_window is not None:
            try:
                props = panda_window.getProperties()
                native_size = (props.getXSize(), props.getYSize())
                native_origin = (props.getXOrigin(), props.getYOrigin())
                native_hwnd = int(panda_window.getWindowHandle().getIntHandle())
                native_parent_info = _win32_window_info(native_hwnd)
            except Exception as error:
                native_size = f"<error: {error}>"

        focus_widget = QApplication.focusWidget()
        focus_name = type(focus_widget).__name__ if focus_widget is not None else None
        main_window_state = None
        if container is not None:
            top_level = container.window()
            if top_level is not None:
                main_window_state = str(top_level.windowState())

        print(
            f"[VIEWPORT_GEOMETRY] {label}: playing={self.studio_playing} "
            f"container_geometry={container_geom} container_rect={container_rect} "
            f"container_contentsRect={container_contents_rect} "
            f"container_global_top_left={container_global_top_left} "
            f"parent_geometry={parent_geom} foreign_qwindow_geometry={foreign_geom} "
            f"native_size={native_size} native_origin={native_origin} "
            f"native_hwnd={native_hwnd} container_hwnd={container_hwnd} "
            f"native_parent_info={native_parent_info} "
            f"devicePixelRatio={device_pixel_ratio} main_window_state={main_window_state} "
            f"focus={focus_name}"
        )

    def _sync_panda_window_to_container(self, reason: str) -> None:
        container = self.qt_viewport_container
        if container is None:
            return

        panda_window = getattr(application.base, "win", None)
        if panda_window is None:
            return

        target_width = max(1, container.width())
        target_height = max(1, container.height())

        self._log_viewport_geometry(f"before sync [{reason}]")
        if DEBUG_VIEWPORT_GEOMETRY:
            _log_full_embedding_inventory(f"before sync [{reason}]")

        # Обновляем сам синглтон ursina.window — просто чтобы кэш был
        # согласован для любого стороннего кода, который его читает. НЕ
        # вызываем requestProperties() отсюда: полная инвентаризация
        # (_dump_panda_graphics_outputs + EnumWindows по всему процессу)
        # показала, что GraphicsOutput у Panda3D всегда ровно один — то
        # есть видимое на реальном экране "дублирование" не второй
        # рендер-поверхностью, а DWM-артефактом compositing/ghosting от
        # ЧАСТЫХ SetParent-вызовов, которые раньше делались на каждый
        # requestProperties(). requestProperties() на Windows сама по себе
        # откатывает нативное окно к top-level WS_POPUP без родителя
        # (см. _force_native_child_parenting) — и раньше мы реагировали на
        # это повторным принудительным репарентингом на каждый вызов, что
        # и порождало видимые артефакты. Теперь просто не даём поводу
        # возникнуть: единственный чужой вызов requestProperties() в рантайме
        # — из ursina/mouse.py при mouse.visible — устранён ниже (Qt-курсор
        # вместо mouse.visible для встроенного вьюпорта), а сами мы
        # запрашиваем позицию/размер только через сырой Win32 SetWindowPos
        # (_reposition_native_child), который НЕ проходит через Panda3D API
        # и потому не запускает этот сброс.
        window.setSize(target_width, target_height)
        window.setOrigin(0, 0)

        try:
            native_hwnd = int(panda_window.getWindowHandle().getIntHandle())
            parent_hwnd = int(container.winId())
            info = _win32_window_info(native_hwnd)
            if info.get("is_child") and info.get("parent_hwnd") == parent_hwnd:
                # Уже корректно припарентовано — только переставляем
                # позицию/размер, БЕЗ SetParent/смены стиля (это и есть
                # дешёвая, не вызывающая DWM-артефактов операция).
                _reposition_native_child(native_hwnd, target_width, target_height)
            else:
                # Родитель/стиль реально не те, что нужно — только тогда
                # делаем полный (более тяжёлый) репарентинг.
                result = _force_native_child_parenting(native_hwnd, parent_hwnd, target_width, target_height)
                if DEBUG_VIEWPORT_GEOMETRY and not result.get("is_child"):
                    print(f"[VIEWPORT_GEOMETRY] re-parenting still failed after sync [{reason}]: {result}")
        except Exception as error:
            if DEBUG_VIEWPORT_GEOMETRY:
                print(f"[VIEWPORT_GEOMETRY] re-parenting raised after sync [{reason}]: {error}")

        if DEBUG_VIEWPORT_GEOMETRY:
            _log_full_embedding_inventory(f"after sync [{reason}]")

        self._log_viewport_geometry(f"after sync [{reason}]")

    def _set_embedded_cursor_hidden(self, hidden: bool) -> None:
        """Прячет/показывает курсор через Qt (QWidget.setCursor), а не
        через ursina mouse.visible. ursina/mouse.py's visible.setter делает
        application.base.win.requestProperties(window) на КАЖДОЕ
        переключение — а это именно то, что заставляет Panda3D откатывать
        нативное окно к top-level WS_POPUP без родителя (см. разбор в
        _sync_panda_window_to_container). Скрытие курсора чисто на стороне
        Qt никогда не трогает Panda3D window properties вообще."""
        container = self.qt_viewport_container
        if container is None:
            return
        if hidden:
            container.setCursor(Qt.CursorShape.BlankCursor)
        else:
            container.unsetCursor()

    def _start_mouse_look(self) -> None:
        if self._qt_look_available():
            # Всё ещё встроенный вьюпорт — курсор прячем через Qt, НЕ через
            # mouse.visible (см. _set_embedded_cursor_hidden).
            self._set_embedded_cursor_hidden(True)

            container = self.qt_viewport_container
            assert container is not None
            center_global = container.mapToGlobal(container.rect().center())
            QCursor.setPos(center_global)
            self._qt_look_last_pos = center_global
        else:
            # Отдельное top-level окно Panda3D (--no-embed) — там весь этот
            # баг не воспроизводится, штатный ursina-путь безопасен.
            mouse.visible = False
            mouse.locked = True

    def _stop_mouse_look(self) -> None:
        if self._qt_look_available():
            self._set_embedded_cursor_hidden(False)
            self._qt_look_last_pos = None
        else:
            mouse.visible = True
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
        record = self.instances.get(part_id)
        is_model = entity is None and record is not None and record.class_name == "Model"

        if entity is None and not is_model:
            # Ни 3D Entity (Part/SpawnPoint), ни Model — например Folder/
            # Script выбраны из Explorer. Гизмо нечего показывать, но
            # выбор всё равно сохраняем, чтобы Inspector продолжал видеть
            # актуальный selected_part_id (это не регрессия: раньше такой
            # выбор тоже не давал гизмо, просто полностью сбрасывался).
            if record is None:
                self.deselect_part(notify_studio=notify_studio)
                return
            self.selected_part_id = part_id
            self._clear_selection_highlights()
            if notify_studio and self.studio_adapter is not None:
                self.studio_adapter.on_game_selection_changed(part_id)
            return

        self.selected_part_id = part_id
        if is_model:
            self._update_model_selection_highlight(part_id)
        else:
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
        self._clear_selection_highlights()
        if notify_studio and self.studio_adapter is not None:
            self.studio_adapter.on_game_selection_changed(None)

    def update_selection_highlight(self, entity: Entity) -> None:
        self._clear_selection_highlights()
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

    def _clear_selection_highlights(self) -> None:
        for highlight in self._model_selection_highlights:
            destroy(highlight)
        self._model_selection_highlights = []

    def _update_model_selection_highlight(self, model_id: str) -> None:
        """Stage 2.2: подсветка выбранного Model — по wireframe-кубу на
        каждый трансформируемый потомок (а не один куб вокруг несуществующей
        3D-геометрии самого Model), плюс сам гизмо на пивоте показывает,
        куда фактически цепляется drag."""
        if self.selection_highlight is not None:
            destroy(self.selection_highlight)
            self.selection_highlight = None
        self._clear_selection_highlights()

        for descendant_id in self._collect_transformable_descendants(model_id):
            entity = self.parts.get(descendant_id)
            if entity is None:
                continue
            self._model_selection_highlights.append(
                Entity(
                    model="wireframe_cube",
                    color=color.azure,
                    position=entity.position,
                    scale=entity.scale * 1.02,
                    rotation=entity.rotation,
                    unlit=True,
                    enabled=not self.studio_playing,
                )
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

    # --------------------------------------------------------
    # Stage 2.2: MODEL PIVOT + CASCADING DESCENDANT TRANSFORMS
    #
    # These helpers use real Panda3D quaternions (panda3d.core.Quat) for
    # all composition, never manual Euler addition — see the Stage 2.2
    # report for why (Ursina's per-axis rotation has non-obvious sign
    # flips, see ROTATION_SIGN in transform_gizmo.py). Euler<->Quat
    # conversion always round-trips through a real Entity/NodePath
    # (self._transform_scratch) rather than a hand-derived formula, so it
    # is guaranteed to match how Ursina actually renders rotation.
    # --------------------------------------------------------

    def _euler_xyz_to_quat(self, rotation_xyz) -> Quat:
        self._transform_scratch.rotation = Vec3(
            float(rotation_xyz[0]), float(rotation_xyz[1]), float(rotation_xyz[2]),
        )
        return Quat(self._transform_scratch.get_quat())

    def _quat_to_euler_xyz(self, quat: Quat) -> list[float]:
        self._transform_scratch.set_quat(quat)
        r = self._transform_scratch.rotation
        return [float(r.x), float(r.y), float(r.z)]

    @staticmethod
    def _compose_quat(outer: Quat, inner: Quat) -> Quat:
        """Returns the quaternion for 'apply inner, then outer' -- i.e.
        compose(outer, inner).xform(v) == outer.xform(inner.xform(v)).
        Panda3D's `*` composes as (A*B).xform(v) == B.xform(A.xform(v))
        (verified empirically, not assumed), hence the swapped order here."""
        return inner * outer

    def _collect_transformable_descendants(self, root_id: str) -> list[str]:
        """All ids transitively parented under root_id that themselves own
        a transform (Part/SpawnPoint Position+Rotation, or a nested Model's
        pivot) — recurses THROUGH non-spatial containers (Folder/Script)
        without including them. Mirrors shared/instance.py's
        get_descendant_ids() graph walk, but filtered to transformable
        types and working off the client's own InstanceRecord map (no
        round-trip to the server needed for local gizmo math)."""
        children_by_parent: dict[str, list[str]] = {}
        for record in self.instances.values():
            children_by_parent.setdefault(record.parent_id or "Workspace", []).append(record.id)
        result: list[str] = []
        frontier = [root_id]
        while frontier:
            current = frontier.pop()
            for child_id in children_by_parent.get(current, []):
                child_record = self.instances.get(child_id)
                if child_record is not None and child_record.class_name in ("Part", "SpawnPoint", "Model"):
                    result.append(child_id)
                frontier.append(child_id)
        return result

    def _world_bounds_points(self, descendant_id: str) -> list[Vec3]:
        """World-space points a descendant contributes to an ancestor's
        auto-pivot AABB. Part/SpawnPoint contribute their full oriented
        bounding box (all 8 local corners, scaled by Size, rotated by
        world Rotation, translated by world Position) — NOT just their
        center — so the auto-pivot reflects real combined world bounds,
        not merely where descendant origins happen to sit. A nested
        Model, or a Part-like with a missing/invalid Size, has no usable
        visual bounds of its own and falls back to a single point (its
        world position) so it can't corrupt the AABB with a phantom box."""
        descendant_record = self.instances.get(descendant_id)
        if descendant_record is None:
            return []

        if descendant_record.class_name == "Model":
            # Recurse through _model_pivot_world rather than reading
            # PivotPosition directly: a nested Model that has never been
            # explicitly transformed does NOT sit at the registry default
            # [0,0,0] — its true effective pivot is ITS OWN lazy auto-pivot
            # (the bounds-center of its own descendants). Reading the raw
            # property here would treat every untouched nested Model as if
            # it were at world origin, causing a visible jump the first
            # time an ancestor is dragged (see Stage 2.2 nested-Model fix).
            nested_pos, _nested_quat = self._model_pivot_world(descendant_id)
            return [nested_pos]

        position = descendant_record.properties.get("Position", [0.0, 0.0, 0.0])
        pos_vec = Vec3(float(position[0]), float(position[1]), float(position[2]))
        size = descendant_record.properties.get("Size")
        if not (isinstance(size, (list, tuple)) and len(size) == 3):
            return [pos_vec]
        try:
            half = (float(size[0]) / 2.0, float(size[1]) / 2.0, float(size[2]) / 2.0)
        except (TypeError, ValueError):
            return [pos_vec]
        if not all(h >= 0 and math.isfinite(h) for h in half):
            return [pos_vec]

        rotation = descendant_record.properties.get("Rotation", [0.0, 0.0, 0.0])
        quat = self._euler_xyz_to_quat(rotation)
        corners: list[Vec3] = []
        for sx in (-1.0, 1.0):
            for sy in (-1.0, 1.0):
                for sz in (-1.0, 1.0):
                    local = Vec3(sx * half[0], sy * half[1], sz * half[2])
                    corners.append(pos_vec + Vec3(quat.xform(local)))
        return corners

    def _model_pivot_world(self, model_id: str) -> tuple[Vec3, Quat]:
        """World pivot position/orientation for a Model. If
        PivotIsExplicit, returns the persisted PivotPosition/PivotRotation
        verbatim. Otherwise this is a LAZY, NEVER-PERSISTED estimate — the
        center of the combined world-space oriented bounding box of every
        transformable descendant (see _world_bounds_points), identity
        rotation — purely for gizmo placement/display (see Stage 2.2
        report, 'lazy auto pivot'). Nothing here is written back to the
        server merely by computing/displaying it. An empty/never-touched
        Model's default pivot is the world origin, documented there too."""
        record = self.instances.get(model_id)
        if record is None:
            return Vec3(0, 0, 0), Quat()

        if bool(record.properties.get("PivotIsExplicit", False)):
            position = record.properties.get("PivotPosition", [0.0, 0.0, 0.0])
            rotation = record.properties.get("PivotRotation", [0.0, 0.0, 0.0])
            pos_vec = Vec3(float(position[0]), float(position[1]), float(position[2]))
            return pos_vec, self._euler_xyz_to_quat(rotation)

        points: list[Vec3] = []
        for descendant_id in self._collect_transformable_descendants(model_id):
            points.extend(self._world_bounds_points(descendant_id))
        if not points:
            return Vec3(0, 0, 0), Quat()
        xs = [p.x for p in points]
        ys = [p.y for p in points]
        zs = [p.z for p in points]
        center = Vec3(
            (min(xs) + max(xs)) / 2.0,
            (min(ys) + max(ys)) / 2.0,
            (min(zs) + max(zs)) / 2.0,
        )
        return center, Quat()

    def _refresh_model_gizmo_proxy(self, model_id: str) -> None:
        position, quat = self._model_pivot_world(model_id)
        self.model_gizmo_proxy.position = position
        self.model_gizmo_proxy.set_quat(quat)

    def _begin_model_drag_capture(self, model_id: str) -> None:
        """Snapshot every transformable descendant's offset/orientation in
        the pivot's OWN rotated frame at drag-start (a transient local
        transform, never persisted — see Stage 2.2 report). Reconstructed
        every frame in _apply_model_gizmo_result() as the pivot moves, which
        is what keeps the group rigid."""
        pivot_pos, pivot_quat = self._model_pivot_world(model_id)
        pivot_quat_inv = pivot_quat.conjugate()

        descendant_relative: dict[str, tuple[Vec3, Quat]] = {}
        # Stage 2.3: Size at drag-start, Part-like descendants only (a
        # nested Model has no Size of its own, only a pivot) — needed for
        # Model uniform scale (new_size = old_size * factor), computed
        # once here rather than every frame for the same reason relative
        # offsets are: always derive from drag-start state, never from a
        # previous frame, so repeated small drags can't accumulate drift.
        descendant_start_size: dict[str, Vec3] = {}
        # Stage 2.5: complete before-snapshot for ModelTransformCommand,
        # built from the SAME authoritative Position/Rotation/Size (or
        # recursive pivot) values this loop already computes — not a
        # second pass, not a Qt/Entity read.
        history_before_descendants: dict[str, dict[str, list[float]]] = {}
        for descendant_id in self._collect_transformable_descendants(model_id):
            descendant_record = self.instances.get(descendant_id)
            if descendant_record is None:
                continue
            if descendant_record.class_name == "Model":
                # Same recursion as _world_bounds_points: an untouched
                # nested Model's true position is its own lazy auto-pivot,
                # not the raw (registry-default) PivotPosition property.
                child_pos, child_quat = self._model_pivot_world(descendant_id)
            else:
                child_pos_raw = descendant_record.properties.get("Position", [0.0, 0.0, 0.0])
                child_rot_raw = descendant_record.properties.get("Rotation", [0.0, 0.0, 0.0])
                child_pos = Vec3(float(child_pos_raw[0]), float(child_pos_raw[1]), float(child_pos_raw[2]))
                child_quat = self._euler_xyz_to_quat(child_rot_raw)
                child_size_raw = descendant_record.properties.get("Size")
                if isinstance(child_size_raw, (list, tuple)) and len(child_size_raw) == 3:
                    descendant_start_size[descendant_id] = Vec3(
                        float(child_size_raw[0]), float(child_size_raw[1]), float(child_size_raw[2]),
                    )

            relative_pos = Vec3(pivot_quat_inv.xform(child_pos - pivot_pos))
            relative_quat = self._compose_quat(pivot_quat_inv, child_quat)
            descendant_relative[descendant_id] = (relative_pos, relative_quat)

            entry = {
                "Position": [float(child_pos.x), float(child_pos.y), float(child_pos.z)],
                "Rotation": self._quat_to_euler_xyz(child_quat),
            }
            if descendant_id in descendant_start_size:
                size_vec = descendant_start_size[descendant_id]
                entry["Size"] = [float(size_vec.x), float(size_vec.y), float(size_vec.z)]
            history_before_descendants[descendant_id] = entry

        self._model_drag_state = {
            "model_id": model_id,
            "descendant_relative": descendant_relative,
            "descendant_start_size": descendant_start_size,
        }
        self._history_drag_before = {
            "kind": "model",
            "model_id": model_id,
            "pivot": {
                "Position": [float(pivot_pos.x), float(pivot_pos.y), float(pivot_pos.z)],
                "Rotation": self._quat_to_euler_xyz(pivot_quat),
            },
            "descendants": history_before_descendants,
        }
        self._model_drag_suppressed_ids = {model_id, *descendant_relative.keys()}
        self._model_drag_sequence_counter = 0
        self._model_drag_awaiting_final = None
        if DEBUG_MODEL_TRANSFORMS:
            print(
                f"[MODEL_TRANSFORMS] begin_drag model={model_id} "
                f"descendants={len(descendant_relative)} pivot_pos={pivot_pos} explicit={bool(self.instances[model_id].properties.get('PivotIsExplicit', False))}"
            )
            self._model_drag_frame_count = 0
            self._model_drag_network_sends = 0
            self._model_drag_timing_start = time.perf_counter()

    def _apply_model_gizmo_result(self, force_network: bool) -> None:
        if self._model_drag_state is None:
            return
        frame_start = time.perf_counter() if DEBUG_MODEL_TRANSFORMS else 0.0

        model_id = self._model_drag_state["model_id"]
        descendant_relative: dict[str, tuple[Vec3, Quat]] = self._model_drag_state["descendant_relative"]

        pivot_pos_new = Vec3(self.model_gizmo_proxy.position)
        pivot_quat_new = Quat(self.model_gizmo_proxy.get_quat())

        math_start = time.perf_counter() if DEBUG_MODEL_TRANSFORMS else 0.0
        computed: dict[str, tuple[Vec3, list[float]]] = {}
        for descendant_id, (relative_pos, relative_quat) in descendant_relative.items():
            child_pos_new = pivot_pos_new + Vec3(pivot_quat_new.xform(relative_pos))
            child_quat_new = self._compose_quat(pivot_quat_new, relative_quat)
            child_rotation_euler = self._quat_to_euler_xyz(child_quat_new)
            computed[descendant_id] = (child_pos_new, child_rotation_euler)
        math_ms = (time.perf_counter() - math_start) * 1000.0 if DEBUG_MODEL_TRANSFORMS else 0.0

        entity_start = time.perf_counter() if DEBUG_MODEL_TRANSFORMS else 0.0
        for descendant_id, (child_pos_new, child_rotation_euler) in computed.items():
            entity = self.parts.get(descendant_id)
            if entity is not None:
                entity.position = child_pos_new
                entity.rotation = Vec3(*child_rotation_euler)
        entity_ms = (time.perf_counter() - entity_start) * 1000.0 if DEBUG_MODEL_TRANSFORMS else 0.0

        bridge_start = time.perf_counter() if DEBUG_MODEL_TRANSFORMS else 0.0
        pivot_position_list = [float(pivot_pos_new.x), float(pivot_pos_new.y), float(pivot_pos_new.z)]
        pivot_rotation_list = self._quat_to_euler_xyz(pivot_quat_new)
        model_record = self.instances.get(model_id)
        if model_record is not None:
            model_record.properties["PivotPosition"] = pivot_position_list
            model_record.properties["PivotRotation"] = pivot_rotation_list
            model_record.properties["PivotIsExplicit"] = True
            if self.studio_adapter is not None:
                self.studio_adapter.on_instance_transform_live(model_record, "Position", pivot_position_list)
                self.studio_adapter.on_instance_transform_live(model_record, "Rotation", pivot_rotation_list)

        for descendant_id, (child_pos_new, child_rotation_euler) in computed.items():
            descendant_record = self.instances.get(descendant_id)
            if descendant_record is None:
                continue
            position_list = [float(child_pos_new.x), float(child_pos_new.y), float(child_pos_new.z)]
            if descendant_record.class_name == "Model":
                descendant_record.properties["PivotPosition"] = position_list
                descendant_record.properties["PivotRotation"] = child_rotation_euler
                descendant_record.properties["PivotIsExplicit"] = True
            else:
                descendant_record.properties["Position"] = position_list
                descendant_record.properties["Rotation"] = child_rotation_euler
            entity = self.parts.get(descendant_id)
            if entity is not None:
                entity.instance_properties["Position"] = position_list
                entity.instance_properties["Rotation"] = child_rotation_euler
            if self.studio_adapter is not None:
                self.studio_adapter.on_instance_transform_live(descendant_record, "Position", position_list)
                self.studio_adapter.on_instance_transform_live(descendant_record, "Rotation", child_rotation_euler)
        bridge_ms = (time.perf_counter() - bridge_start) * 1000.0 if DEBUG_MODEL_TRANSFORMS else 0.0

        self._update_model_selection_highlight(model_id)

        now = time.monotonic()
        interval = 1.0 / GIZMO_NETWORK_SEND_RATE
        sent = False
        if force_network or (now - self._model_drag_last_network_send) >= interval:
            self._model_drag_last_network_send = now
            self._model_drag_sequence_counter += 1
            sequence = self._model_drag_sequence_counter
            self.request_transform_model(
                model_id,
                pivot_position_list,
                pivot_rotation_list,
                {
                    descendant_id: {
                        "Position": [float(pos.x), float(pos.y), float(pos.z)],
                        "Rotation": rot,
                    }
                    for descendant_id, (pos, rot) in computed.items()
                },
                sequence,
            )
            sent = True
            if force_network:
                # Финальный (release) кадр — держим защиту от эха активной
                # до тех пор, пока не придёт ИМЕННО этот sequence обратно
                # (см. apply_model_transformed/apply_model_transform_rejected).
                # Снимать защиту сразу здесь нельзя: более ранний
                # throttled-кадр может ещё быть "в полёте" и откатить
                # превью назад одним видимым кадром раньше, чем придёт
                # финальное подтверждение.
                self._model_drag_awaiting_final = {
                    "model_id": model_id,
                    "sequence": sequence,
                    "since": now,
                }
            if DEBUG_MODEL_TRANSFORMS:
                self._model_drag_network_sends = getattr(self, "_model_drag_network_sends", 0) + 1

        if DEBUG_MODEL_TRANSFORMS:
            self._model_drag_frame_count = getattr(self, "_model_drag_frame_count", 0) + 1
            total_ms = (time.perf_counter() - frame_start) * 1000.0
            print(
                f"[MODEL_TRANSFORMS] frame={self._model_drag_frame_count} "
                f"descendants={len(computed)} math={math_ms:.3f}ms entity={entity_ms:.3f}ms "
                f"bridge={bridge_ms:.3f}ms total={total_ms:.3f}ms sent={sent}"
            )

    def _apply_model_scale_result(self, result: dict[str, Vec3], force_network: bool) -> None:
        """Model uniform-scale counterpart to _apply_model_gizmo_result().
        Only the uniform handle is ever interactable for a Model target
        (see allow_axis_scale=False in update_gizmo() — axis scale is not
        exactly representable for arbitrarily-rotated descendants, see
        Stage 2.3 report), so `result` here is always a pure factor:
        result["Size"] == (factor, factor, factor) because
        model_gizmo_proxy's own scale is always Vec3(1,1,1) (never touched
        anywhere else — see its definition), so the gizmo's drag-start Size
        is exactly 1 on every axis and the returned Size vector IS the
        scale factor.

        Pivot stays FIXED for a uniform scale (Rotation and Position both
        unchanged) — new_position = pivot + (old_position - pivot) *
        factor, new_size = old_size * factor for Part-like descendants;
        nested Models get only their PivotPosition scaled the same way (no
        Size of their own, PivotRotation unchanged)."""
        if self._model_drag_state is None:
            return
        size_factor_vec = result.get("Size")
        if size_factor_vec is None:
            return
        factor = float(size_factor_vec.x)
        if not math.isfinite(factor) or factor <= 0:
            return
        frame_start = time.perf_counter() if DEBUG_SCALE_GIZMO else 0.0

        model_id = self._model_drag_state["model_id"]
        descendant_relative: dict[str, tuple[Vec3, Quat]] = self._model_drag_state["descendant_relative"]
        descendant_start_size: dict[str, Vec3] = self._model_drag_state.get("descendant_start_size", {})

        # Uniform scale commutes with rotation, so the pivot-rotated
        # relative offset can simply be scaled by `factor` before being
        # rotated back into world space — see Stage 2.3 report for the
        # derivation showing this is exactly equivalent to
        # pivot + (old_position - pivot) * factor.
        pivot_pos = Vec3(self.model_gizmo_proxy.position)
        pivot_quat = Quat(self.model_gizmo_proxy.get_quat())

        math_start = time.perf_counter() if DEBUG_SCALE_GIZMO else 0.0
        computed: dict[str, tuple[Vec3, list[float] | None]] = {}
        for descendant_id, (relative_pos, _relative_quat) in descendant_relative.items():
            child_pos_new = pivot_pos + Vec3(pivot_quat.xform(relative_pos * factor))
            start_size = descendant_start_size.get(descendant_id)
            new_size: list[float] | None = None
            if start_size is not None:
                new_size = [
                    max(MIN_PART_SIZE, start_size.x * factor),
                    max(MIN_PART_SIZE, start_size.y * factor),
                    max(MIN_PART_SIZE, start_size.z * factor),
                ]
            computed[descendant_id] = (child_pos_new, new_size)
        math_ms = (time.perf_counter() - math_start) * 1000.0 if DEBUG_SCALE_GIZMO else 0.0

        entity_start = time.perf_counter() if DEBUG_SCALE_GIZMO else 0.0
        for descendant_id, (child_pos_new, new_size) in computed.items():
            descendant_record = self.instances.get(descendant_id)
            if descendant_record is None:
                continue
            position_list = [float(child_pos_new.x), float(child_pos_new.y), float(child_pos_new.z)]
            entity = self.parts.get(descendant_id)
            if descendant_record.class_name == "Model":
                descendant_record.properties["PivotPosition"] = position_list
                descendant_record.properties["PivotIsExplicit"] = True
            else:
                descendant_record.properties["Position"] = position_list
                if entity is not None:
                    entity.position = child_pos_new
                    entity.instance_properties["Position"] = position_list
                if new_size is not None:
                    descendant_record.properties["Size"] = new_size
                    if entity is not None:
                        entity.scale = Vec3(*new_size)
                        entity.instance_properties["Size"] = new_size
            if self.studio_adapter is not None:
                self.studio_adapter.on_instance_transform_live(descendant_record, "Position", position_list)
                if new_size is not None:
                    self.studio_adapter.on_instance_transform_live(descendant_record, "Size", new_size)
        entity_ms = (time.perf_counter() - entity_start) * 1000.0 if DEBUG_SCALE_GIZMO else 0.0

        self._update_model_selection_highlight(model_id)

        now = time.monotonic()
        interval = 1.0 / GIZMO_NETWORK_SEND_RATE
        sent = False
        if force_network or (now - self._model_drag_last_network_send) >= interval:
            self._model_drag_last_network_send = now
            self._model_drag_sequence_counter += 1
            sequence = self._model_drag_sequence_counter
            pivot_position_list = [float(pivot_pos.x), float(pivot_pos.y), float(pivot_pos.z)]
            pivot_rotation_list = self._quat_to_euler_xyz(pivot_quat)
            descendants_payload: dict[str, dict[str, list[float]]] = {}
            for descendant_id, (child_pos_new, new_size) in computed.items():
                entry: dict[str, list[float]] = {
                    "Position": [float(child_pos_new.x), float(child_pos_new.y), float(child_pos_new.z)],
                }
                if new_size is not None:
                    entry["Size"] = new_size
                descendants_payload[descendant_id] = entry
            self.request_transform_model(
                model_id, pivot_position_list, pivot_rotation_list, descendants_payload, sequence,
            )
            sent = True
            if force_network:
                self._model_drag_awaiting_final = {
                    "model_id": model_id,
                    "sequence": sequence,
                    "since": now,
                }

        if DEBUG_SCALE_GIZMO:
            total_ms = (time.perf_counter() - frame_start) * 1000.0
            print(
                f"[SCALE_GIZMO] model={model_id} factor={factor:.4f} "
                f"descendants={len(computed)} math={math_ms:.3f}ms entity={entity_ms:.3f}ms "
                f"total={total_ms:.3f}ms sent={sent}"
            )

    def next_history_transform_sequence(self) -> int:
        """Independent sequence counter used ONLY when editor_history.py's
        ModelTransformCommand re-sends a Model transform for Undo/Redo --
        see CommandManager/_ModelTransformTracker. Never shared with
        _model_drag_sequence_counter (that one guards live-drag echo
        suppression for an in-progress gizmo drag, a completely different
        event)."""
        self._history_transform_sequence_counter += 1
        return self._history_transform_sequence_counter

    def request_transform_model(
        self,
        model_id: str,
        pivot_position: list[float],
        pivot_rotation: list[float],
        descendants: dict[str, dict[str, list[float]]],
        sequence: int,
    ) -> bool:
        if not self.network.connected_event.is_set():
            return False
        self.network.send({
            "type": protocol.TRANSFORM_MODEL,
            "id": model_id,
            "pivot": {"Position": pivot_position, "Rotation": pivot_rotation},
            "descendants": descendants,
            "sequence": sequence,
        })
        return True

    def request_transform_model_pivot(
        self,
        model_id: str,
        pivot_position: list[float] | None,
        pivot_rotation: list[float] | None,
    ) -> bool:
        """One-shot (non-drag) pivot edit — used by Inspector's Pivot
        Position/Rotation fields. Reuses the exact same cascading-transform
        machinery as the gizmo (_begin_model_drag_capture +
        _apply_model_gizmo_result) for a single synthetic 'frame' instead
        of a live per-frame drag loop, so the two entry points can never
        drift apart mathematically."""
        record = self.instances.get(model_id)
        if record is None or record.class_name != "Model":
            return False
        self._begin_model_drag_capture(model_id)
        if pivot_position is not None:
            self.model_gizmo_proxy.position = Vec3(
                float(pivot_position[0]), float(pivot_position[1]), float(pivot_position[2]),
            )
        if pivot_rotation is not None:
            self.model_gizmo_proxy.set_quat(self._euler_xyz_to_quat(pivot_rotation))
        self._apply_model_gizmo_result(force_network=True)
        self._push_model_transform_history(model_id, label="Edit Model Pivot")
        # _model_drag_state (relative-offset math snapshot) is only needed
        # while actively computing frames, so it's safe to clear now — but
        # _model_drag_suppressed_ids / _model_drag_awaiting_final stay set
        # (populated by the force_network send above) until the matching
        # authoritative response arrives, same reasoning as end_gizmo_drag.
        self._model_drag_state = None
        return True

    def update_gizmo(self) -> None:
        self._check_model_drag_ack_timeout()
        selected_record = self.instances.get(self.selected_part_id) if self.selected_part_id else None
        is_model_target = selected_record is not None and selected_record.class_name == "Model"

        if is_model_target:
            target = self.model_gizmo_proxy
            if not self.gizmo.dragging:
                self._refresh_model_gizmo_proxy(self.selected_part_id)
        else:
            target = self.parts.get(self.selected_part_id) if self.selected_part_id else None

        if target is not self.gizmo.current_target:
            # Model axis-scaling is not exactly representable for
            # arbitrarily-rotated descendants (shear) — see Stage 2.3
            # report. Axis handles stay hidden entirely for a Model
            # target; only the uniform handle is ever interactable there.
            self.gizmo.set_target(target, allow_axis_scale=not is_model_target)

        if target is None:
            return

        self.gizmo.refresh_transform(camera.world_position)

        if self.gizmo.dragging:
            if DEBUG_GIZMO_TIMING:
                # В этой архитектуре "кадр рендера" и "чтение мыши" — одно и
                # то же событие: gizmo-drag читает mouse.x/mouse.y напрямую
                # каждый вызов update_gizmo(), а не через отдельные Qt
                # mouse-move события (те используются только для RMB-обзора
                # камеры через QCursor, см. _poll_qt_look_delta). Оба
                # тикаются здесь для честности замера, а не для различения.
                self._gizmo_timing.tick("rendered_frame")
                self._gizmo_timing.tick("mouse_read")
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
                if DEBUG_GIZMO_TIMING:
                    self._gizmo_timing.tick("entity_transform")
                if is_model_target:
                    if self.gizmo_mode == "scale":
                        self._apply_model_scale_result(result, force_network=False)
                    else:
                        self._apply_model_gizmo_result(force_network=False)
                elif self.gizmo_mode == "scale":
                    self._apply_scale_gizmo_result(target, result, force_network=False)
                else:
                    self._apply_gizmo_result(target, result, force_network=False)
        else:
            ray_origin, ray_direction = mouse_world_ray()
            if ray_origin is not None:
                self.gizmo.update_hover(ray_origin, ray_direction)

    def try_begin_gizmo_drag(self) -> bool:
        if self.gizmo.current_target is None or self.gizmo_mode not in ("move", "rotate", "scale"):
            return False
        ray_origin, ray_direction = mouse_world_ray()
        if ray_origin is None:
            return False
        started = self.gizmo.begin_drag(ray_origin, ray_direction)
        if started:
            # См. update_instance(): пока перетаскивание активно, устаревшие
            # PART_UPDATED-эхо сервера для ЭТОГО объекта не должны откатывать
            # Position/Rotation назад поверх уже более новой локальной
            # позиции — целиком клиентская защита, без изменений протокола.
            self._gizmo_dragging_instance_id = self.selected_part_id
            selected_record = self.instances.get(self.selected_part_id) if self.selected_part_id else None
            if selected_record is not None and selected_record.class_name == "Model":
                self._begin_model_drag_capture(self.selected_part_id)
                # Stage 2.5: _begin_model_drag_capture() already stashes a
                # full before-snapshot into self._history_drag_before (see
                # that method) -- nothing more to do here.
            else:
                self._model_drag_state = None
                self._model_drag_suppressed_ids = set()
                if selected_record is not None:
                    properties = selected_record.properties
                    self._history_drag_before = {
                        "kind": "part",
                        "instance_id": self.selected_part_id,
                        "properties": {
                            "Position": [float(v) for v in properties.get("Position", [0.0, 0.0, 0.0])],
                            "Rotation": [float(v) for v in properties.get("Rotation", [0.0, 0.0, 0.0])],
                            "Size": [float(v) for v in properties.get("Size", [1.0, 1.0, 1.0])],
                        },
                    }
                else:
                    self._history_drag_before = None
            if DEBUG_GIZMO_TIMING:
                self._gizmo_timing.begin()
        return started

    def end_gizmo_drag(self) -> None:
        result = self.gizmo.end_drag()
        if self._model_drag_state is not None:
            model_id_for_history = self._model_drag_state.get("model_id")
            if result is not None:
                if self.gizmo_mode == "scale":
                    self._apply_model_scale_result(result, force_network=True)
                else:
                    self._apply_model_gizmo_result(force_network=True)
                self._push_model_transform_history(model_id_for_history)
            if DEBUG_MODEL_TRANSFORMS:
                elapsed = time.perf_counter() - getattr(self, "_model_drag_timing_start", time.perf_counter())
                frames = getattr(self, "_model_drag_frame_count", 0)
                sends = getattr(self, "_model_drag_network_sends", 0)
                hz = frames / elapsed if elapsed > 0 else 0.0
                print(
                    f"[MODEL_TRANSFORMS] end_drag frames={frames} ({hz:.1f}Hz) "
                    f"network_sends={sends} elapsed={elapsed:.2f}s"
                )
            # _model_drag_suppressed_ids / _model_drag_awaiting_final are
            # deliberately NOT cleared here — the force_network send above
            # populated _model_drag_awaiting_final, and protection must
            # stay up until that exact sequence's MODEL_TRANSFORMED/
            # TRANSFORM_MODEL_REJECTED arrives (see apply_model_transformed
            # and apply_model_transform_rejected). Clearing immediately
            # would let an older still-in-flight throttled echo roll the
            # preview back one visible frame before the final one lands.
            self._model_drag_state = None
            self._gizmo_dragging_instance_id = None
            if DEBUG_GIZMO_TIMING:
                self._gizmo_timing.end_and_report()
            return

        if result is None:
            self._gizmo_dragging_instance_id = None
            return
        target = self.parts.get(self.selected_part_id) if self.selected_part_id else None
        if target is not None:
            if self.gizmo_mode == "scale":
                self._apply_scale_gizmo_result(target, result, force_network=True)
            else:
                self._apply_gizmo_result(target, result, force_network=True)
            self._push_part_transform_history()
        self._gizmo_dragging_instance_id = None
        if DEBUG_GIZMO_TIMING:
            self._gizmo_timing.end_and_report()

    def _push_part_transform_history(self) -> None:
        """Stage 2.5: builds the after-snapshot from CURRENT (just-applied)
        record.properties and pushes a PartTransformCommand (Move/Rotate)
        or ResizeCommand (Scale) — see try_begin_gizmo_drag() for the
        before-snapshot. Never sends anything itself: _apply_gizmo_result/
        _apply_scale_gizmo_result already sent the real request."""
        before = self._history_drag_before
        self._history_drag_before = None
        if before is None or before.get("kind") != "part":
            return
        instance_id = before["instance_id"]
        record = self.instances.get(instance_id)
        if record is None:
            return
        before_properties = before["properties"]
        keys = {"move": ("Position",), "rotate": ("Rotation",), "scale": ("Position", "Size")}.get(self.gizmo_mode)
        if not keys:
            return
        after_properties = {
            key: [float(v) for v in record.properties.get(key, before_properties[key])]
            for key in keys
        }
        before_subset = {key: before_properties[key] for key in keys}
        label = {"move": "Move Part", "rotate": "Rotate Part", "scale": "Scale Part"}[self.gizmo_mode]
        command_cls = editor_history.ResizeCommand if self.gizmo_mode == "scale" else editor_history.PartTransformCommand
        command = command_cls(instance_id, label, before_properties=before_subset, after_properties=after_properties)
        self.history.push_optimistic(command)

    def _push_model_transform_history(self, model_id: str | None, label: str | None = None) -> None:
        """Model counterpart of _push_part_transform_history() — see that
        method and try_begin_gizmo_drag()/_begin_model_drag_capture() for
        the before-snapshot."""
        before = self._history_drag_before
        self._history_drag_before = None
        if before is None or before.get("kind") != "model" or model_id is None or before.get("model_id") != model_id:
            return
        model_record = self.instances.get(model_id)
        if model_record is None:
            return
        after_pivot = {
            "Position": [float(v) for v in model_record.properties.get("PivotPosition", before["pivot"]["Position"])],
            "Rotation": [float(v) for v in model_record.properties.get("PivotRotation", before["pivot"]["Rotation"])],
        }
        after_descendants: dict[str, dict[str, list[float]]] = {}
        for descendant_id, before_entry in before["descendants"].items():
            descendant_record = self.instances.get(descendant_id)
            if descendant_record is None:
                continue
            if descendant_record.class_name == "Model":
                after_descendants[descendant_id] = {
                    "Position": [float(v) for v in descendant_record.properties.get("PivotPosition", before_entry["Position"])],
                    "Rotation": [float(v) for v in descendant_record.properties.get("PivotRotation", before_entry["Rotation"])],
                }
            else:
                entry = {
                    "Position": [float(v) for v in descendant_record.properties.get("Position", before_entry["Position"])],
                    "Rotation": [float(v) for v in descendant_record.properties.get("Rotation", before_entry["Rotation"])],
                }
                if "Size" in before_entry:
                    entry["Size"] = [float(v) for v in descendant_record.properties.get("Size", before_entry["Size"])]
                after_descendants[descendant_id] = entry
        if label is None:
            label = {"move": "Move Model", "rotate": "Rotate Model", "scale": "Scale Model"}.get(self.gizmo_mode, "Transform Model")
        command = editor_history.ModelTransformCommand(
            model_id, label, before["pivot"], after_pivot, before["descendants"], after_descendants,
        )
        self.history.push_optimistic(command)

    def _apply_gizmo_result(
        self,
        entity: Entity,
        result: tuple[str, Vec3],
        force_network: bool,
    ) -> None:
        duration_start = time.perf_counter() if DEBUG_GIZMO_TIMING else 0.0

        kind, vector = result
        part_id = self.selected_part_id
        if part_id is None:
            return

        # Entity уже обновлена внутри gizmo.update_drag()/end_drag() —
        # каждый вызов идёт из update_gizmo(), т.е. каждый рендер-кадр (см.
        # DEBUG_GIZMO_TIMING). Здесь синхронизируем instance_properties
        # (иначе Inspector увидит старое значение — instance_to_scene_object()
        # читает именно record.properties, а не entity.position напрямую) и
        # SceneObject в EngineBridge.
        #
        # ВАЖНО: используем on_instance_transform_live(), а НЕ
        # on_instance_updated() — последний пересобирает целый SceneObject и
        # эмитит scene_changed/selection_changed, что заставляет Explorer и
        # Inspector полностью пересобирать себя (все QTreeWidgetItem'ы,
        # все спинбоксы) НА КАЖДЫЙ РЕНДЕР-КАДР перетаскивания. Это и было
        # настоящей причиной "дёрганого" движения — не throttling сети и не
        # grid snap (см. отчёт задачи). on_instance_transform_live() обновляет
        # SceneObject на месте и обновляет только существующие спинбоксы
        # Inspector, без пересборки.
        new_value = [float(vector.x), float(vector.y), float(vector.z)]
        entity.instance_properties[kind] = new_value
        record = self.instances.get(part_id)
        if record is not None:
            record.properties[kind] = new_value
        self.update_selection_highlight(entity)
        if self.studio_adapter is not None and record is not None:
            self.studio_adapter.on_instance_transform_live(record, kind, new_value)
            if DEBUG_GIZMO_TIMING:
                self._gizmo_timing.tick("bridge_sync")

        now = time.monotonic()
        interval = 1.0 / GIZMO_NETWORK_SEND_RATE
        if force_network or (now - self._gizmo_last_network_send) >= interval:
            self._gizmo_last_network_send = now
            self.apply_property_edit(part_id, {kind: entity.instance_properties[kind]})
            if DEBUG_GIZMO_TIMING:
                self._gizmo_timing.tick("network_send")

        if DEBUG_GIZMO_TIMING:
            self._gizmo_timing.tick("apply_gizmo_result")
            self._gizmo_timing.record_duration_ms((time.perf_counter() - duration_start) * 1000.0)

    def _apply_scale_gizmo_result(
        self,
        entity: Entity,
        result: dict[str, Vec3],
        force_network: bool,
    ) -> None:
        """Scale-mode counterpart to _apply_gizmo_result(). The gizmo's
        result is a dict with "Size" always present and "Position" present
        only for an axis-handle drag (the uniform handle scales around the
        Part's own center and never touches Position — see Stage 2.3
        report). Both keys are synced and sent together in ONE
        apply_property_edit() call so no intermediate network frame ever
        shows Size updated without the matching Position (or vice versa)."""
        duration_start = time.perf_counter() if DEBUG_GIZMO_TIMING else 0.0

        part_id = self.selected_part_id
        if part_id is None:
            return
        record = self.instances.get(part_id)

        # Entity.scale/.position were already updated inside
        # gizmo.update_drag()/end_drag() (see _update_axis_scale_drag/
        # _update_uniform_scale_drag in transform_gizmo.py) — same
        # "render frame == drag frame" architecture as Move/Rotate, so
        # only instance_properties/record/bridge/network need syncing here.
        updated: dict[str, list[float]] = {}
        for key in ("Position", "Size"):
            vector = result.get(key)
            if vector is None:
                continue
            new_value = [float(vector.x), float(vector.y), float(vector.z)]
            entity.instance_properties[key] = new_value
            if record is not None:
                record.properties[key] = new_value
            updated[key] = new_value

        if not updated:
            return

        self.update_selection_highlight(entity)
        if self.studio_adapter is not None and record is not None:
            for key, new_value in updated.items():
                self.studio_adapter.on_instance_transform_live(record, key, new_value)
            if DEBUG_GIZMO_TIMING:
                self._gizmo_timing.tick("bridge_sync")

        now = time.monotonic()
        interval = 1.0 / GIZMO_NETWORK_SEND_RATE
        if force_network or (now - self._gizmo_last_network_send) >= interval:
            self._gizmo_last_network_send = now
            if DEBUG_SCALE_GIZMO:
                print(f"[SCALE_GIZMO] client_studio OUTGOING part_id={part_id} force_network={force_network} payload={updated}")
            self.apply_property_edit(part_id, dict(updated))
            if DEBUG_GIZMO_TIMING:
                self._gizmo_timing.tick("network_send")

        if DEBUG_GIZMO_TIMING:
            self._gizmo_timing.tick("apply_gizmo_result")
            self._gizmo_timing.record_duration_ms((time.perf_counter() - duration_start) * 1000.0)

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

    def request_set_parent(self, instance_id: str, parent_id: str) -> bool:
        if not self.network.connected_event.is_set():
            if self.studio_adapter is not None:
                self.studio_adapter.log("warning", "Нет подключения к серверу: перенос отменён.")
            return False
        if DEBUG_REPARENTING:
            print(f"[REPARENTING] request: {instance_id} -> {parent_id}")
        self.network.send({"type": protocol.SET_PARENT, "id": instance_id, "parent_id": parent_id})
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

    def compute_duplicate_properties(self, instance_id: str) -> tuple["InstanceRecord", dict[str, Any]] | None:
        """Shared by duplicate_instance() (direct, non-history path) and
        MultiplayerStudioAdapter.duplicate_object() (Stage 2.5 history
        path, which needs the concrete offset properties up front to
        build a CreateObjectCommand rather than relying on
        request_create_instance's own spawn-position auto-fill)."""
        record = self.instances.get(instance_id)
        if record is None:
            return None
        properties = dict(record.properties)
        if "Position" in properties:
            position = list(properties["Position"])
            while len(position) < 3:
                position.append(0)
            position[0] = float(position[0]) + 2.0
            position[2] = float(position[2]) + 2.0
            properties["Position"] = position
        return record, properties

    def build_delete_snapshot(self, instance_id: str) -> list[dict[str, Any]] | None:
        """Parent-first snapshot of instance_id and every descendant, for
        DeleteObjectCommand (Stage 2.5) -- must be captured BEFORE the
        delete is sent, since remove_instance() erases these records as
        PART_DELETED broadcasts arrive. Mirrors shared/instance.py's
        server-side get_descendant_ids()/destroy_cascade() traversal, but
        walks the client's own self.instances mirror instead of the
        server's world dict."""
        root_record = self.instances.get(instance_id)
        if root_record is None:
            return None
        ordered_ids = [instance_id]
        frontier = [instance_id]
        while frontier:
            current = frontier.pop(0)
            children = [record.id for record in self.instances.values() if record.parent_id == current]
            ordered_ids.extend(children)
            frontier.extend(children)
        snapshot: list[dict[str, Any]] = []
        for oid in ordered_ids:
            record = self.instances[oid]
            snapshot.append(
                {
                    "old_id": record.id,
                    "class_name": record.class_name,
                    "name": record.name,
                    "parent_id": record.parent_id,
                    "properties": dict(record.properties),
                    "enabled": record.enabled,
                }
            )
        return snapshot

    def duplicate_instance(self, instance_id: str) -> bool:
        result = self.compute_duplicate_properties(instance_id)
        if result is None:
            return False
        record, properties = result
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

            if isinstance(message_type, str):
                self.history.on_network_message(message_type, message)

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
                raw_parent_id = message.get("parent_id")
                parent_id = raw_parent_id if isinstance(raw_parent_id, str) else None
                if instance_id:
                    self.update_instance(instance_id, properties, name, enabled, parent_id)
            elif message_type == protocol.PART_DELETED:
                self.remove_instance(str(message.get("id", "")))
            elif message_type == protocol.SET_PARENT_REJECTED:
                reason = str(message.get("reason", "Reparent rejected by server."))
                if DEBUG_REPARENTING:
                    print(f"[REPARENTING] rejected: {message.get('id')}: {reason}")
                if self.studio_adapter is not None:
                    self.studio_adapter.log("warning", reason)
            elif message_type == protocol.MODEL_TRANSFORMED:
                self.apply_model_transformed(message)
            elif message_type == protocol.TRANSFORM_MODEL_REJECTED:
                self.apply_model_transform_rejected(message)

    # Как долго держать группу под защитой в ожидании финального echo,
    # прежде чем считать соединение зависшим и снять защиту принудительно
    # (иначе Model осталась бы навсегда невосприимчива к чужим апдейтам).
    _MODEL_DRAG_FINAL_ACK_TIMEOUT = 5.0

    def _is_awaited_final_ack(self, model_id: str, sequence: Any) -> bool:
        awaiting = self._model_drag_awaiting_final
        return (
            awaiting is not None
            and awaiting["model_id"] == model_id
            and awaiting["sequence"] == sequence
        )

    def _check_model_drag_ack_timeout(self) -> None:
        """Called every frame from update_gizmo(). If we've been waiting
        too long for the final TRANSFORM_MODEL's authoritative response
        (dropped packet, disconnect), force-clear protection and resync
        from the server rather than leaving the group permanently immune
        to other clients' updates."""
        awaiting = self._model_drag_awaiting_final
        if awaiting is None:
            return
        if time.monotonic() - awaiting["since"] < self._MODEL_DRAG_FINAL_ACK_TIMEOUT:
            return
        if DEBUG_MODEL_TRANSFORMS:
            print(f"[MODEL_TRANSFORMS] final ack timeout for {awaiting['model_id']}, forcing resync")
        self._model_drag_awaiting_final = None
        self._model_drag_suppressed_ids = set()
        if self.studio_adapter is not None:
            self.studio_adapter.log("warning", "Model transform confirmation timed out; resyncing from server.")
            self.studio_adapter.sync_full_scene()

    def _apply_model_transform_payload(
        self,
        model_id: str,
        pivot: Any,
        descendants: Any,
    ) -> None:
        """Shared by the MODEL_TRANSFORMED happy path and the corrective
        'current' snapshot attached to TRANSFORM_MODEL_REJECTED — same
        application logic either way, so a rejection can self-heal the
        local preview through the exact same code as a normal echo."""
        if isinstance(pivot, dict):
            model_record = self.instances.get(model_id)
            if model_record is not None:
                if isinstance(pivot.get("Position"), list):
                    model_record.properties["PivotPosition"] = pivot["Position"]
                if isinstance(pivot.get("Rotation"), list):
                    model_record.properties["PivotRotation"] = pivot["Rotation"]
                model_record.properties["PivotIsExplicit"] = True

        if isinstance(descendants, dict):
            for descendant_id, transform in descendants.items():
                if not isinstance(transform, dict):
                    continue
                record = self.instances.get(str(descendant_id))
                if record is None:
                    continue
                if record.class_name == "Model":
                    if isinstance(transform.get("Position"), list):
                        record.properties["PivotPosition"] = transform["Position"]
                    if isinstance(transform.get("Rotation"), list):
                        record.properties["PivotRotation"] = transform["Rotation"]
                    record.properties["PivotIsExplicit"] = True
                else:
                    self._apply_instance_properties(record, transform, replace=False)

        if self.selected_part_id == model_id:
            self._refresh_model_gizmo_proxy(model_id)
            self._update_model_selection_highlight(model_id)
        if self.studio_adapter is not None:
            self.studio_adapter.sync_full_scene()

    def apply_model_transformed(self, message: dict[str, Any]) -> None:
        """Receives an atomic MODEL_TRANSFORMED batch (our own confirmed
        drag echoing back, or another client's Model transform). Applies
        every descendant + the pivot, then does exactly ONE full-scene
        sync at the end — never one rebuild per descendant, which would
        turn a 100-Part batch into 100 Explorer/Inspector rebuilds."""
        model_id = str(message.get("id", ""))
        if not model_id:
            return
        sequence = message.get("sequence")

        if model_id in self._model_drag_suppressed_ids:
            if not self._is_awaited_final_ack(model_id, sequence):
                # Либо драг ещё активен (никакой final ещё не отправлен),
                # либо это эхо более раннего throttled-кадра, всё ещё в
                # полёте, — не наш ожидаемый financial ack. В обоих
                # случаях локальное превью новее: подавляем.
                if DEBUG_MODEL_TRANSFORMS:
                    print(f"[MODEL_TRANSFORMS] suppressed self-echo for {model_id} seq={sequence}")
                return
            # Это ИМЕННО финальный подтверждённый ответ на наш последний
            # (force_network) запрос — применяем (идемпотентно совпадает
            # с локальным состоянием) и только теперь снимаем защиту.
            self._model_drag_awaiting_final = None
            self._model_drag_suppressed_ids = set()

        self._apply_model_transform_payload(model_id, message.get("pivot"), message.get("descendants"))

    def apply_model_transform_rejected(self, message: dict[str, Any]) -> None:
        """A rejected TRANSFORM_MODEL — always applies the server's
        corrective 'current' snapshot (if present) so a hierarchy change
        mid-drag can never leave the Model visually split. If we're still
        actively dragging this exact Model, re-baseline
        (_begin_model_drag_capture) from the corrected state so the drag
        continues cleanly instead of repeatedly resending a now-invalid
        descendant set; otherwise (rejection arrived after mouse-release)
        the drag session is over, so protection is cleared outright."""
        model_id = str(message.get("id", ""))
        reason = str(message.get("reason", "Model transform rejected by server."))
        sequence = message.get("sequence")
        if DEBUG_MODEL_TRANSFORMS:
            print(f"[MODEL_TRANSFORMS] rejected: {model_id} seq={sequence}: {reason}")
        if self.studio_adapter is not None:
            self.studio_adapter.log("warning", reason)
        if not model_id:
            return

        current = message.get("current")
        if isinstance(current, dict):
            self._apply_model_transform_payload(model_id, current.get("pivot"), current.get("descendants"))

        still_dragging = (
            self.gizmo.dragging
            and self._model_drag_state is not None
            and self._model_drag_state.get("model_id") == model_id
        )
        if model_id in self._model_drag_suppressed_ids:
            if still_dragging:
                # Живой драг продолжается — пересчитываем relative offsets
                # от только что скорректированного авторитетного состояния,
                # а не бросаем драг на середине из-за одного отклонения.
                self._begin_model_drag_capture(model_id)
            else:
                # Мышь уже отпущена (это отклонение финального или более
                # раннего запроса, пришедшее после release) — активного
                # драга защищать больше нечего.
                self._model_drag_awaiting_final = None
                self._model_drag_suppressed_ids = set()

    def load_world_snapshot(self, parts: list[dict[str, Any]]) -> None:
        incoming_ids = {str(item.get("id", "")) for item in parts if isinstance(item, dict)}
        for stale_id in list(self.instances):
            if stale_id not in incoming_ids:
                self.remove_instance(stale_id)
        for part_data in parts:
            if isinstance(part_data, dict):
                self.spawn_instance(part_data)
        # A WORLD_SNAPSHOT is a brand-new authoritative world (initial
        # connect, or a reconnect) -- every stored instance id/parent/
        # property reference an Undo/Redo command might hold could now be
        # stale or mean something different. See Stage 2.5 spec §15.
        self.history.clear()
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
            return Entity(
                model="cube",
                position=Vec3(float(position[0]), float(position[1]), float(position[2])),
                scale=Vec3(float(size[0]), float(size[1]), float(size[2])),
                rotation=Vec3(float(rotation[0]), float(rotation[1]), float(rotation[2])),
                color=color.rgba32(
                    int(rgb[0]), int(rgb[1]), int(rgb[2]),
                    int(255 * (1.0 - float(transparency))),
                ),
                # Stage 2.4: ALWAYS a picking collider, independent of
                # CanCollide — this is Ursina's mouse.hovered_entity
                # raycast target (editor selection), not gameplay
                # collision. CanCollide now only controls the SEPARATE
                # Bullet physics body built in physics.py during Play.
                # Conflating the two here used to make CanCollide=False
                # objects unselectable, which Stage 2.4 explicitly
                # requires NOT to happen.
                collider="box",
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
            # Stage 2.4: CanCollide no longer touches entity.collider (see
            # _build_part_entity) — it only affects the Play Mode physics
            # body, applied fresh from current properties at the start of
            # each Play (see MultiplayerGame.set_studio_playing).
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
        parent_id: str | None = None,
    ) -> None:
        record = self.instances.get(instance_id)
        if record is None:
            return

        if parent_id is not None and parent_id != record.parent_id:
            if DEBUG_REPARENTING:
                print(f"[REPARENTING] confirmed: {instance_id} {record.parent_id!r} -> {parent_id!r}")
            record.parent_id = parent_id

        if properties and instance_id == self._gizmo_dragging_instance_id:
            if DEBUG_GIZMO_TIMING:
                self._gizmo_timing.tick("server_echo")
            if DEBUG_SCALE_GIZMO:
                print(f"[SCALE_GIZMO] client_studio INCOMING echo (dragging, filtered) id={instance_id} raw_properties={properties}")
            # Сервер — источник истины ВНЕ активного локального
            # перетаскивания (см. отчёт задачи), но while a drag on THIS
            # instance is in progress, network throttling (см.
            # GIZMO_NETWORK_SEND_RATE) means an echo we receive now can
            # reflect an OLDER intermediate value than what the local drag
            # has already advanced to this frame — applying it would visibly
            # snap the object backward. Не трогаем Position/Rotation здесь;
            # остальные свойства (Color, Name, ...) по-прежнему применяются
            # нормально. Гвард снимается сразу на отпускании кнопки — тогда
            # финальный echo (в т.ч. этого же перетаскивания) снова
            # применяется как обычно.
            properties = {k: v for k, v in properties.items() if k not in ("Position", "Rotation", "Size")}

        if properties:
            if DEBUG_SCALE_GIZMO and "Size" in properties:
                print(f"[SCALE_GIZMO] client_studio INCOMING echo (applied) id={instance_id} properties={properties}")
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
        # Bug-report follow-up: this is the ONE authoritative local-removal
        # path (driven by the server's confirmed PART_DELETED broadcast,
        # see process_network_messages -- fires for every recursively
        # deleted descendant too, one message each) that Explorer delete,
        # cascade-delete, and any other client's delete all funnel
        # through. Runtime physics used to have no idea an instance
        # existed here at all: its Bullet body (or ghost/ "none"
        # bookkeeping) would keep existing forever after the visible
        # Entity was destroyed -- a deleted static floor kept invisibly
        # supporting whatever rested on it. Removing the body BEFORE
        # destroying the Entity (order doesn't actually matter for
        # correctness here, since PhysicsWorld never touches Ursina's
        # destroy() and step() only ever iterates self._bodies, but doing
        # it first keeps this block reading top-to-bottom as "physics,
        # then the rest").
        if self._physics_world is not None:
            removed_kind = self._physics_world.remove_part(instance_id)
            if removed_kind == "static":
                # A static support may have just vanished out from under
                # sleeping dynamic bodies -- Bullet does not re-evaluate
                # a deactivated body's contacts on its own, so nothing
                # would ever notice the support is gone otherwise. See
                # Stage 2.4 follow-up report for why "wake everything"
                # was chosen over targeted contact-based waking.
                self._physics_world.wake_all_dynamic()
        self._physics_snapshot.pop(instance_id, None)

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
            self.update_physics()
            self.update_lua()
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
        # CommandManager listeners are plain Python callbacks (see
        # editor_history.py) -- safe to emit a Qt signal directly from one
        # since the Ursina game loop and the Qt event loop share the same
        # thread here (panda_timer.timeout.connect(ursina_app.step)).
        self.game.history.add_state_listener(lambda: bridge.history_state_changed.emit())

    def undo(self) -> bool:
        if not self.game.history.can_undo:
            return False
        label = self.game.history.undo_text
        self.game.history.undo()
        self.log("info", f"Undo: {label}")
        return True

    def redo(self) -> bool:
        if not self.game.history.can_redo:
            return False
        label = self.game.history.redo_text
        self.game.history.redo()
        self.log("info", f"Redo: {label}")
        return True

    def history_state(self) -> dict[str, Any]:
        history = self.game.history
        return {
            "can_undo": history.can_undo,
            "can_redo": history.can_redo,
            "undo_text": history.undo_text,
            "redo_text": history.redo_text,
        }

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

        if record.class_name == "Model":
            # Stage 2.2: show the SAME lazily-computed auto-pivot the gizmo
            # would attach to (see MultiplayerGame._model_pivot_world) —
            # showing the raw stored default [0,0,0] here instead would be
            # misleading for a non-explicit pivot the gizmo actually places
            # at the descendant-bounds midpoint. Nothing is written back;
            # this is display-only, exactly like the gizmo's own read path.
            pivot_pos_vec, pivot_quat = self.game._model_pivot_world(record.id)
            pivot_position_tuple = (pivot_pos_vec.x, pivot_pos_vec.y, pivot_pos_vec.z)
            pivot_rotation_tuple = tuple(self.game._quat_to_euler_xyz(pivot_quat))
        else:
            pivot_position_tuple = (0.0, 0.0, 0.0)
            pivot_rotation_tuple = (0.0, 0.0, 0.0)

        return SceneObject(
            id=record.id,
            name=record.name,
            object_type=record.class_name,
            parent=parent,
            enabled=record.enabled,
            anchored=True,
            can_collide=False,
            cast_shadow=False,
            pivot_position=self._as_editor_vec3(list(pivot_position_tuple), (0, 0, 0)),
            pivot_rotation=self._as_editor_vec3(list(pivot_rotation_tuple), (0, 0, 0)),
            pivot_is_explicit=bool(properties.get("PivotIsExplicit", False)),
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

    _LIVE_TRANSFORM_FIELDS = {"Position": "position", "Rotation": "rotation", "Size": "size"}
    # Stage 2.2: a Model's live-dragged transform is its PIVOT, not a
    # Position/Rotation property (Model has none) — same SceneObject
    # live-sync mechanism, different target fields (see SceneObject.
    # pivot_position docstring in studio_editor_live.py).
    _LIVE_PIVOT_TRANSFORM_FIELDS = {"Position": "pivot_position", "Rotation": "pivot_rotation"}

    def on_instance_transform_live(self, record: "InstanceRecord", property_key: str, value: list[float]) -> None:
        """High-frequency counterpart to on_instance_updated(), used while a
        gizmo drag is in progress (see MultiplayerGame._apply_gizmo_result
        for a single Part, _apply_model_gizmo_result for a Model's pivot +
        its cascaded descendants). Routes through EngineBridge.
        sync_transform_live() instead of sync_upsert()/
        instance_to_scene_object() — the latter rebuilds a fresh SceneObject
        and triggers a full Explorer+Inspector widget rebuild on every call,
        which is what caused the jerky drag."""
        if self.bridge is None:
            return
        field_map = self._LIVE_PIVOT_TRANSFORM_FIELDS if record.class_name == "Model" else self._LIVE_TRANSFORM_FIELDS
        field_name = field_map.get(property_key)
        if field_name is None:
            return
        self.bridge.sync_transform_live(record.id, field_name, self._as_editor_vec3(value, (0, 0, 0)))

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

        # `properties=None` here (NOT {}) so CreateObjectCommand.send_forward
        # preserves request_create_instance's own spawn-position auto-fill
        # for the very first creation -- the command's `properties` is
        # filled in with the server-confirmed canonical values by
        # _CreateTracker once PART_CREATED arrives, so a later Redo
        # recreates at the ORIGINAL spawn position rather than wherever the
        # player happens to be facing when Redo is pressed.
        command = editor_history.CreateObjectCommand(
            f"Create {definition.display_name}", object_type, None, resolved_parent, unique_name,
        )
        accepted = self.game.history.perform(command, optimistic=False)
        if accepted:
            self.pending_create_count += 1
        return accepted

    def duplicate_object(self, object_id: str) -> bool:
        result = self.game.compute_duplicate_properties(object_id)
        if result is None:
            return False
        record, properties = result
        command = editor_history.CreateObjectCommand(
            f"Duplicate {record.class_name}", record.class_name, properties, record.parent_id, record.name,
        )
        accepted = self.game.history.perform(command, optimistic=False)
        if accepted:
            self.pending_create_count += 1
        return accepted

    def delete_object(self, object_id: str) -> bool:
        if object_id.startswith("system:"):
            return False
        snapshot = self.game.build_delete_snapshot(object_id)
        if snapshot is None:
            return self.game.delete_instance(object_id)
        label = f"Delete {snapshot[0]['class_name']}"
        command = editor_history.DeleteObjectCommand(label, snapshot)
        return self.game.history.perform(command, optimistic=False)

    def set_parent(self, object_id: str, parent_id: str) -> bool:
        # Финальная валидация всё равно на сервере (handle_set_parent в
        # server.py) — здесь только фильтруем то, что клиент даже не должен
        # пытаться отправлять (system:* объекты не существуют в world сервера
        # и не имеют смысла как перетаскиваемый объект).
        if object_id.startswith("system:"):
            return False
        record = self.game.instances.get(object_id)
        if record is None:
            return self.game.request_set_parent(object_id, parent_id)
        command = editor_history.ReparentCommand(
            object_id, "Reparent", before_parent_id=record.parent_id, after_parent_id=parent_id,
        )
        return self.game.history.perform(command, optimistic=False)

    def transform_model(
        self,
        model_id: str,
        pivot_position: list[float] | None,
        pivot_rotation: list[float] | None,
    ) -> bool:
        """Inspector-driven Pivot Position/Rotation edit — same logical
        group transform as dragging the gizmo (see Stage 2.2 report)."""
        if model_id.startswith("system:"):
            return False
        return self.game.request_transform_model_pivot(model_id, pivot_position, pivot_rotation)

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
        """Every Inspector edit, rename, and Anchored/CanCollide toggle
        goes through here -- the single hook point for
        PropertyEditCommand (Stage 2.5). `obj` already carries the NEW
        (post-edit) value by the time we get here (EngineBridge writes it
        before calling the adapter); the OLD value for history comes from
        `self.game.instances[object_id]`, which is only updated once the
        server confirms via PART_UPDATED -- i.e. it is still the pre-edit
        value at this point."""
        if self.bridge is None or object_id.startswith("system:"):
            return False
        obj = self.bridge.get_object(object_id)
        if obj is None:
            return False

        name: str | None = None
        enabled: bool | None = None
        properties: dict[str, Any] | None = None
        label = "Edit Property"

        if property_path == "name":
            name = str(value)
            label = "Rename"
        elif property_path == "enabled":
            enabled = bool(value)
            label = "Edit Enabled"
        elif property_path.startswith("properties."):
            key = property_path.split(".", 1)[1]
            properties = {key: value}
            label = f"Edit {key}"
        else:
            root = property_path.split(".", 1)[0]
            if root == "position":
                properties = {"Position": [obj.position.x, obj.position.y, obj.position.z]}
                label = "Edit Position"
            elif root == "rotation":
                properties = {"Rotation": [obj.rotation.x, obj.rotation.y, obj.rotation.z]}
                label = "Edit Rotation"
            elif root == "size":
                properties = {"Size": [obj.size.x, obj.size.y, obj.size.z]}
                label = "Edit Size"
            elif property_path == "color":
                properties = {"Color": self._hex_to_rgb(obj.color)}
                label = "Edit Color"
            elif property_path == "transparency":
                properties = {"Transparency": float(obj.transparency)}
                label = "Edit Transparency"
            elif property_path == "material" and "Material" in DEFAULT_PART_PROPERTIES:
                properties = {"Material": str(obj.material)}
                label = "Edit Material"
            elif property_path == "reflectance" and "Reflectance" in DEFAULT_PART_PROPERTIES:
                properties = {"Reflectance": float(obj.reflectance)}
                label = "Edit Reflectance"
            elif property_path == "anchored" and "Anchored" in DEFAULT_PART_PROPERTIES:
                properties = {"Anchored": bool(obj.anchored)}
                label = "Edit Anchored"
            elif property_path == "can_collide" and "CanCollide" in DEFAULT_PART_PROPERTIES:
                properties = {"CanCollide": bool(obj.can_collide)}
                label = "Edit CanCollide"
            elif property_path == "cast_shadow" and "CastShadow" in DEFAULT_PART_PROPERTIES:
                properties = {"CastShadow": bool(obj.cast_shadow)}
                label = "Edit CastShadow"
            elif property_path == "locked" and "Locked" in DEFAULT_PART_PROPERTIES:
                properties = {"Locked": bool(obj.locked)}
                label = "Edit Locked"
            else:
                return False

        record = self.game.instances.get(object_id)
        if record is None:
            # No local record to diff against (should not normally happen
            # for a live-synced object) -- fall back to a direct,
            # non-undoable send rather than losing the edit entirely.
            return self.game.apply_property_edit(object_id, properties=properties, name=name, enabled=enabled)

        before_properties = (
            {key: record.properties.get(key) for key in properties} if properties else None
        )
        command = editor_history.PropertyEditCommand(
            object_id,
            label,
            before_properties=before_properties,
            after_properties=properties,
            before_name=record.name if name is not None else None,
            after_name=name,
            before_enabled=record.enabled if enabled is not None else None,
            after_enabled=enabled,
        )
        return self.game.history.perform(command, optimistic=True)

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
    def __init__(self, container: QWidget, foreign_window: QWindow, game: "MultiplayerGame") -> None:
        super().__init__(container)
        self.container = container
        self.foreign_window = foreign_window
        self.game = game

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() in {
            QEvent.Type.MouseButtonPress,
            QEvent.Type.FocusIn,
            QEvent.Type.Enter,
        }:
            self.container.setFocus(Qt.FocusReason.MouseFocusReason)
            self.foreign_window.requestActivate()
        elif event.type() == QEvent.Type.Resize:
            # Держит ursina.window.size синхронным с реальным размером
            # контейнера при КАЖДОМ ресайзе (окно студии, доки, maximize) —
            # см. подробности в MultiplayerGame._sync_panda_window_to_container.
            self.game._sync_panda_window_to_container("container resize")
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

    if DEBUG_VIEWPORT_GEOMETRY:
        _log_full_embedding_inventory("before createWindowContainer")

    container = QWidget.createWindowContainer(foreign_window)
    container.setMinimumSize(640, 360)
    container.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    container.setMouseTracking(True)

    if DEBUG_VIEWPORT_GEOMETRY:
        _log_full_embedding_inventory("after createWindowContainer, before force-reparent")

    focus_filter = PandaWindowFocusFilter(container, foreign_window, game)
    container.installEventFilter(focus_filter)

    studio._panda_foreign_window = foreign_window
    studio._panda_window_container = container
    studio._panda_focus_filter = focus_filter
    studio.install_engine_viewport(container)

    # createWindowContainer() is documented to reparent the foreign native
    # window under the container, but measured with _win32_window_info() it
    # doesn't reliably happen for this Panda3D window on this Qt/Windows
    # combination — it can stay a top-level WS_POPUP with parent_hwnd=None,
    # which is the actual cause of the viewport rendering at the wrong
    # screen position (see _force_native_child_parenting docstring). Force
    # it explicitly and verify.
    container_hwnd = int(container.winId())
    parenting_before = _win32_window_info(handle)
    parenting_after = _force_native_child_parenting(
        handle, container_hwnd, container.width(), container.height()
    )
    if not parenting_after.get("is_child"):
        bridge.log(
            "error",
            f"Failed to make the Panda3D window a real Win32 child "
            f"(parenting_before={parenting_before}, parenting_after={parenting_after}).",
        )

    game.set_qt_viewport_container(container, foreign_window)
    # Контейнер мог уже получить свой первый Resize до того, как мы успели
    # навесить event filter (порядок layout-прохода Qt не гарантирован) —
    # досинхронизируем сразу, чтобы ursina.window.size/origin не остались
    # протухшими с самого старта.
    game._sync_panda_window_to_container("initial embed")

    if DEBUG_VIEWPORT_GEOMETRY:
        print(f"[VIEWPORT_GEOMETRY] native HWND parenting before force: {parenting_before}")
        print(f"[VIEWPORT_GEOMETRY] native HWND parenting after force: {parenting_after}")
        _log_full_embedding_inventory("after force-reparent + initial sync")

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


def _print_startup_diagnostics() -> None:
    """Bug-report follow-up (uniform-scale mouse-down jump, reported as
    'still present after 4bf1331'): prints exactly what source this
    running process is executing, so a stale packaged .exe (see
    PickADoor.spec / dist/) or an already-running pre-fix process can be
    told apart from an actual current-checkout run instead of guessed at.
    getattr(sys, 'frozen', False) is True specifically inside a
    PyInstaller-built .exe — a frozen build has no .git directory
    alongside it, so `git rev-parse HEAD` deliberately isn't attempted
    there (it would just print a confusing 'not a git repository' error);
    the frozen line itself already answers the question.

    Gated behind DEBUG_STARTUP (default False, Stage 2.4 cleanup) since
    normal runs don't need this on every launch — kept rather than
    deleted because "which checkout/commit is actually running" has
    already been the deciding fact in two separate bug investigations
    this project."""
    if not DEBUG_STARTUP:
        return
    if getattr(sys, "frozen", False):
        print(
            f"[STARTUP] FROZEN BUILD (PyInstaller) — running from "
            f"{sys.executable}, NOT the live source checkout. Source-tree "
            f"fixes (including transform_gizmo.py's Scale drag fixes) do "
            f"NOT apply here; rebuild the .exe or run "
            f"'python client_studio.py' from source instead."
        )
        return
    repo_dir = Path(__file__).resolve().parent
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True, stderr=subprocess.STDOUT,
        ).strip()
    except Exception as error:
        commit = f"<git rev-parse failed: {error}>"
    try:
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=repo_dir, text=True, stderr=subprocess.STDOUT,
        ).strip()
    except Exception:
        dirty = "<unknown>"
    print(f"[STARTUP] running from source: {repo_dir}")
    print(f"[STARTUP] git HEAD: {commit}")
    print(f"[STARTUP] git working tree dirty: {bool(dirty)!r}")
    print(f"[STARTUP] sys.executable: {sys.executable}")
    print(f"[STARTUP] client_studio.py: {Path(__file__).resolve()}")
    import transform_gizmo as _tg
    print(f"[STARTUP] transform_gizmo module: {Path(_tg.__file__).resolve()}")
    print(f"[STARTUP] transform_gizmo.DEBUG_SCALE_GIZMO = {_tg.DEBUG_SCALE_GIZMO}")


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

    _print_startup_diagnostics()

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