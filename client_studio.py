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
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from panda3d.core import Filename, Fog, Quat, TransparencyAttrib
from ursina import (
    AmbientLight,
    Cone,
    DirectionalLight,
    Entity,
    Grid,
    PointLight as UrsinaPointLight,
    SpotLight as UrsinaSpotLight,
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
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox, QWidget

from studio_editor_live import (
    DARK_STYLE,
    EngineBridge,
    SCENE_FILE_FILTER,
    SceneObject,
    StudioMainWindow,
    Vec3 as EditorVec3,
)
import character_controller
import character_rig
import datamodel_schema
import editor_history
import lua_gameplay_api
import lua_runtime
import physics
import place_manager
import sstudio_templates
from shared import object_registry, protocol
from shared.instance import DEFAULT_PART_PROPERTIES, MIN_PART_SIZE
from shared.object_registry import LIGHT_CLASS_NAMES, ROOT_SERVICES
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
# Stage 4.1 (showcase sprint): MeshPart.MeshId is a path relative to this
# folder (e.g. "crate.glb" or "props/crate.glb") -- kept separate from
# ASSETS_DIR's root (which already holds engine assets like player.glb) so
# user/project mesh props have one dedicated, unambiguous home.
MESH_ASSETS_DIR = ASSETS_DIR / "meshes"

ASSETS_DIR.mkdir(parents=True, exist_ok=True)
MESH_ASSETS_DIR.mkdir(parents=True, exist_ok=True)


def resolve_mesh_asset_path(mesh_id: str) -> Optional[Path]:
    """Resolves a MeshPart.MeshId string to a real file under
    MESH_ASSETS_DIR, or None if mesh_id is empty/malformed/escapes that
    folder. MeshId is untrusted data (Lua-writable, Inspector-writable,
    round-trips through Place JSON) -- rejecting absolute paths and ".."
    segments before ever touching the filesystem is a deliberate guard
    against it being used to read files outside the project's mesh folder,
    not just a correctness nicety."""
    if not mesh_id or not isinstance(mesh_id, str):
        return None
    normalized = mesh_id.strip().replace("\\", "/")
    if not normalized or normalized.startswith("/") or ":" in normalized:
        return None
    parts = [segment for segment in normalized.split("/") if segment not in ("", ".")]
    if not parts or any(segment == ".." for segment in parts):
        return None
    candidate = MESH_ASSETS_DIR.joinpath(*parts)
    try:
        resolved = candidate.resolve()
        resolved.relative_to(MESH_ASSETS_DIR.resolve())
    except (OSError, ValueError):
        return None
    return resolved


def load_mesh_node(mesh_id: str):
    """Loads a MeshPart's geometry via Panda3D's own model loader (the same
    call model_debug_viewer.py already proved works for a .glb, backed by
    the installed panda3d-gltf loader) -- returns (node_path, error_message);
    node_path is None on any failure, with error_message describing why
    (missing file, empty file, malformed asset, loader not ready yet)."""
    path = resolve_mesh_asset_path(mesh_id)
    if path is None:
        return None, f"invalid or missing MeshId: {mesh_id!r}"
    if not path.exists() or not path.is_file():
        return None, f"mesh asset not found: {mesh_id}"
    if path.stat().st_size <= 0:
        return None, f"mesh asset is empty: {mesh_id}"

    panda_loader = getattr(builtins, "loader", None)
    if panda_loader is None:
        return None, "engine loader is not ready yet"

    try:
        node = panda_loader.loadModel(Filename.from_os_specific(str(path)))
    except Exception as error:  # noqa: BLE001 -- any loader failure is a "missing/bad asset", not a crash
        return None, f"failed to load mesh {mesh_id!r}: {type(error).__name__}: {error}"

    if node is None or node.isEmpty():
        return None, f"failed to load mesh {mesh_id!r}: loader returned no geometry"

    try:
        node.setTransparency(TransparencyAttrib.MNone)
    except Exception:
        pass

    return node, ""


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

# Viewport-interaction rewrite: editor-camera mouse-wheel dolly step
# (world units moved along the current view direction per wheel tick) --
# editor mode previously had no scroll-wheel handling at all (scroll was
# only ever wired for Play-mode third-person zoom, THIRD_PERSON_ZOOM_STEP
# below). Deliberately its own constant rather than reusing that one --
# "zoom" there orbits a fixed third-person offset; this dollies the whole
# free camera through world space, a different feel at a different scale.
EDITOR_ZOOM_STEP = 1.5

# focus_selected_part() ("F"): camera-back-off distance = bounding-box
# diagonal * FACTOR, clamped to at least MIN -- a small Part isn't
# approached until the camera is uncomfortably close, a huge one isn't
# framed from so far away it reads as a speck.
FOCUS_DISTANCE_FACTOR = 1.5
FOCUS_MIN_DISTANCE = 4.0

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

# Bug-report follow-up: logs the full editor-viewport lifecycle state
# (viewport_focused, editor_look_active, capture state, current Qt
# focus widget, cursor positions, computed delta, gizmo.dragging, camera
# yaw/pitch) at every RMB-look begin/end, every focus-loss detection,
# and every applicationStateChanged transition -- see
# MultiplayerGame._log_viewport_lifecycle(). Off by default; flip to
# True to diagnose a future "RMB look / Alt+Tab recovery" regression
# without needing to re-derive this instrumentation from scratch.
DEBUG_VIEWPORT_LIFECYCLE = False

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

# Stage 3.7: Roblox-Studio-style third-person camera zoom (mouse wheel,
# RMB-hold-to-look only -- see MultiplayerGame._adjust_third_person_distance()).
THIRD_PERSON_MIN_DISTANCE = 2.0
THIRD_PERSON_MAX_DISTANCE = 10.0
THIRD_PERSON_ZOOM_STEP = 0.5


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

# Stage 4.1 rendering diagnosis (superseded): AmbientLight used to be a
# hardcoded constant here -- at its ORIGINAL (125,125,145), normalized to
# ~0.49 gray, applied UNIFORMLY to every surface by simplepbr's shader (the
# ambient/IBL terms are NOT attenuated by the shadow factor, only the
# direct per-light term is), a fully-shadowed surface still kept ~half of a
# fully-lit surface's brightness -- the actual root cause of the scene
# reading "flat gray" regardless of correctly-configured shadows/fog, since
# there was never enough light/dark RANGE for contrast to read as anything
# but subtle. Now authored via Environment.AmbientColor/AmbientIntensity
# (see apply_environment_settings()) -- default [68,68,82]*1.0 preserves
# the corrected (~0.27, roughly halved) value exactly.
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
    # Stage 3.7: named Play input-ownership states -- see play_input_state()
    # below. Class attributes (not an Enum) purely so they stay easy to
    # reference as self.PLAY_INPUT_STATE_* / MultiplayerGame.PLAY_INPUT_STATE_*
    # from both this class and tests without an extra import.
    PLAY_INPUT_STATE_EDITOR_UI = "EDITOR_UI"
    PLAY_INPUT_STATE_THIRD_PERSON_FREE_CURSOR = "PLAY_THIRD_PERSON_FREE_CURSOR"
    PLAY_INPUT_STATE_THIRD_PERSON_RMB_LOOK = "PLAY_THIRD_PERSON_RMB_LOOK"
    PLAY_INPUT_STATE_FIRST_PERSON_CAPTURED = "PLAY_FIRST_PERSON_CAPTURED"
    PLAY_INPUT_STATE_FIRST_PERSON_RELEASED = "PLAY_FIRST_PERSON_RELEASED"

    def __init__(
        self, server_url: str, player_name: str, legacy_demo: bool = False, pbr_pipeline: Any = None,
    ) -> None:
        super().__init__()

        self.server_url = server_url
        self.player_name = player_name
        # Stage 4.1 (atmosphere slice): the ONE simplepbr.Pipeline object
        # for this process's whole lifetime -- captured once from
        # simplepbr.init()'s return value in main() (previously discarded
        # entirely) and never recreated (Play/Stop must not call
        # simplepbr.init() again; see apply_environment_settings()). None
        # when simplepbr isn't installed (SIMPLEPBR_AVAILABLE False) or in
        # a test harness that never called main() -- every environment
        # write below is a no-op in that case, not a crash.
        self.pbr_pipeline = pbr_pipeline
        # Last-applied Environment properties, purely to avoid redundantly
        # re-touching the DirectionalLight's shadow buffer (see
        # apply_environment_settings()'s docstring for why that specific
        # call is the one worth guarding against repetition).
        self._environment_state: dict[str, Any] | None = None
        # Gates the original PickADoor demo content (hardcoded ground plane,
        # 5 hardcoded test blocks, eagerly-loaded player.glb avatar) that
        # used to be created unconditionally on every launch, independently
        # of WORLD_SNAPSHOT/the selected template -- see create_world() and
        # create_local_visual(). Off by default: normal SStudio mode shows
        # only the actual authoritative world (server Instances) plus the
        # reusable Camera/Lighting service nodes. --legacy-demo restores it
        # for developers who still want the original standalone demo scene.
        self.legacy_demo = legacy_demo
        self.network = NetworkClient(server_url=server_url, player_name=player_name)

        self.local_player_id: str | None = None
        self.remote_players: dict[str, RemotePlayer] = {}
        # self.parts — только объекты с реальной 3D-Entity (Part/SpawnPoint):
        # это то, с чем работают gizmo, hover, click-select. self.instances —
        # ВСЕ синхронизированные объекты (включая Folder/Model/Script/...),
        # это то, что видит Explorer/Inspector через адаптер.
        self.parts: dict[str, Entity] = {}
        self.instances: dict[str, InstanceRecord] = {}
        # Stage 3.8: persistent root-service properties (Workspace.Gravity,
        # StarterPlayer.CharacterWalkSpeed, ...) -- keyed by service name,
        # NOT part of self.instances (root services are not Instances, see
        # shared/object_registry.ROOT_SERVICES). Seeded with schema
        # defaults so Inspector/Lua/Play never see a missing key even
        # before the first WORLD_SNAPSHOT arrives. Overwritten wholesale by
        # load_world_snapshot() and merged into by SERVICE_PROPERTY_UPDATED
        # -- see process_network_messages().
        self.services: dict[str, dict[str, Any]] = datamodel_schema.sanitize_services_snapshot(None)
        self.selected_part_id: str | None = None
        self.selection_highlight: Entity | None = None
        self.studio_adapter: MultiplayerStudioAdapter | None = None

        # Stage 3.2: REPLACE_WORLD is fire-and-forget over the websocket;
        # the eventual REPLACE_WORLD_RESULT is correlated back to its
        # caller purely by request_id, since nothing else about this
        # exchange is otherwise distinguishable from any other confirmed/
        # rejected message pair already flowing through process_network_
        # messages().
        self._pending_replace_world: dict[str, Callable[[bool, str], None]] = {}

        self.studio_playing = False
        self.editor_look_active = False
        # Viewport-interaction rewrite: THE single source of truth for
        # "the editor viewport currently owns keyboard input" -- separate
        # from editor_look_active (RMB-look/rotation specifically) on
        # purpose. True the moment any click lands in the viewport (see
        # PandaWindowFocusFilter), False the moment focus moves to any
        # other Qt widget or the app loses OS focus (see
        # release_play_input_capture(), called from both). update()'s
        # editor-mode branch gates WASD/Space/Q/E flight on THIS flag,
        # not on editor_look_active -- holding RMB is no longer a
        # prerequisite for ordinary camera movement.
        self.viewport_focused = False
        self.player_yaw = 0.0
        self.player_pitch = 0.0
        self.last_send_time = 0.0
        self.third_person_enabled = False
        # Stage 3.7: mutable per-session third-person zoom distance (mouse
        # wheel while RMB-look is available) -- see _adjust_third_person_distance().
        # THIRD_PERSON_DISTANCE remains the fixed reset-to default.
        self._third_person_distance = THIRD_PERSON_DISTANCE
        # Stage 3.8: session-local zoom limits, seeded from the persistent
        # StarterPlayer.CameraMin/MaxZoomDistance at Play start (see
        # set_studio_playing()) -- _adjust_third_person_distance() clamps
        # against THESE, not the fixed THIRD_PERSON_MIN/MAX_DISTANCE
        # constants, which now exist only as a pre-connection/editor-time
        # fallback (used here before the first Play ever runs).
        self._runtime_min_zoom = THIRD_PERSON_MIN_DISTANCE
        self._runtime_max_zoom = THIRD_PERSON_MAX_DISTANCE
        # Stage 3.8: True while StarterPlayer.CameraMode == "LockFirstPerson"
        # for the CURRENT Play session -- blocks V/SetCameraMode("ThirdPerson")
        # for this session only (spec: does not persist, is not itself a
        # persistent/serialized property). Always False outside Play.
        self._camera_mode_locked_first_person = False

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

        # Stage 3.3: Play Mode character controller. Exists only between
        # _start_character() and _stop_character() (mirrors physics/Lua's
        # own Play-scoped lifecycle) -- recreated fresh every Play, fully
        # destroyed on every Stop, never persisted, never serialized into
        # the Place (see character_controller.py's module docstring for
        # the full local-only networking boundary).
        self._character_runtime: character_controller.CharacterRuntime | None = None

        # Stage 3.4: default SStudio character visual rig. Exists only
        # between _start_character() and _stop_character(), same lifespan
        # as self._character_runtime above -- deliberately a SEPARATE
        # field (not stored inside CharacterRuntime itself) so
        # character_controller.py stays untouched by the visual layer, per
        # Stage 3.4 spec ("Do not rewrite the capsule controller"). Never
        # parented to local_player (see character_rig.CharacterVisualRig's
        # class docstring for why), never serialized, never added to
        # self.instances.
        self._character_visual: character_rig.CharacterVisualRig | None = None

        # Stage 3.0: sandboxed Lua runtime (see lua_runtime.py). Exists
        # only between set_studio_playing(True) and set_studio_playing
        # (False), created AFTER physics (it binds to self._physics_world)
        # and stopped BEFORE physics on the way out -- Lua must finish
        # cancelling every task and restoring its own overlay before
        # anything else starts tearing the Play session down, so no
        # coroutine can mutate the scene mid-cleanup (see Stage 3.0
        # report).
        self._lua_runtime: lua_runtime.LuaRuntimeManager | None = None

        # Stage 3.5: Lua Player/Character/Signal/UserInputService gameplay
        # API (see lua_gameplay_api.py). Exists only between _start_lua()
        # and _stop_lua() -- created AFTER LuaRuntimeManager.start()
        # succeeds (needs a live VM to install its own prelude fragment
        # into) and torn down BEFORE LuaRuntimeManager.stop() discards
        # that VM, so CharacterRemoving listeners still have a live VM to
        # run in when they fire (see LuaGameplayContext.stop()'s
        # docstring).
        self._lua_gameplay: lua_gameplay_api.LuaGameplayContext | None = None

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
            self.viewport_focused = False
            self.local_player.position = Vec3(self.saved_play_position)
            self.player_yaw = self.saved_play_yaw
            self.player_pitch = self.saved_play_pitch
            self.local_player.rotation = Vec3(0, self.player_yaw, 0)
            self.camera_pitch_pivot.rotation = Vec3(-self.player_pitch, 0, 0)
            self.studio_playing = True
            self.hud_root.enabled = True
            # Stage 3.7: third-person with a free, uncaptured cursor is the
            # default Play camera mode (Roblox Studio-style), not permanent
            # first-person mouse-look -- capture is now only ever entered
            # explicitly (RMB-hold in third-person, or switching to
            # first-person; see input()/set_camera_mode()). Reset any zoom
            # left over from a previous session to the fixed default.
            #
            # Stage 3.8: StarterPlayer.CameraMode selects the STARTING mode
            # for this session -- "Classic" keeps the Stage 3.7 default
            # above; "LockFirstPerson" starts captured in first-person
            # instead and blocks toggling back out for the rest of this
            # session (see toggle_third_person()/set_camera_mode() and
            # lua_gameplay_api.character_set_camera_mode()). Zoom limits
            # are re-seeded from the CURRENT persistent values every Play
            # start, same reasoning as _starter_player_controller_kwargs().
            starter_player = self.services.get("StarterPlayer", {})
            self._runtime_min_zoom = float(starter_player.get("CameraMinZoomDistance", THIRD_PERSON_MIN_DISTANCE))
            self._runtime_max_zoom = float(starter_player.get("CameraMaxZoomDistance", THIRD_PERSON_MAX_DISTANCE))
            self._camera_mode_locked_first_person = starter_player.get("CameraMode") == "LockFirstPerson"
            self.third_person_enabled = not self._camera_mode_locked_first_person
            self._third_person_distance = max(self._runtime_min_zoom, min(self._runtime_max_zoom, THIRD_PERSON_DISTANCE))
            # Stage 4.1: defensive re-apply, same reasoning as the zoom
            # limits just above -- guarantees Play always starts from the
            # authored Environment state even if something else drifted
            # it between world-snapshot load and now (it also self-guards
            # against redundant shadow-buffer churn when nothing changed;
            # see apply_environment_settings()).
            self.apply_environment_settings(self.services.get("Environment", {}))
            self.gizmo.set_target(None)
            if self.selection_highlight is not None:
                self.selection_highlight.enabled = False
            self._set_light_markers_visible(False)
            self._start_physics()
            self._start_character()
            if self._camera_mode_locked_first_person:
                # LockFirstPerson starts captured immediately -- the same
                # primitive RMB-hold/set_camera_mode() already use, just
                # invoked once here at Play start instead of on a click.
                self._start_mouse_look()
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
            # Stage 3.9 viewport rewrite: deliberately NOT restored to True
            # here -- "click gives focus" stays the one consistent rule
            # (see viewport_focused's own docstring at __init__) rather
            # than a Stop-specific exception; a single click back into the
            # viewport (the natural next action after Stop) restores WASD
            # immediately, same as any other focus transfer.
            self.viewport_focused = False
            self.hud_root.enabled = False
            # Stage 3.7: Stop must work on the first click from EVERY Play
            # input state, including mid-RMB-drag or first-person capture --
            # release_play_input_capture() (not a bare _stop_mouse_look())
            # is what also clears any held W/A/S/D/Space state and emits
            # their InputEnded events, and it's idempotent/safe even when
            # nothing was captured. Must run before _stop_lua() below so
            # LuaGameplayContext is still active to receive release_all_keys().
            self.release_play_input_capture()
            if self.selection_highlight is not None:
                self.selection_highlight.enabled = True
            self._set_light_markers_visible(True)
            if was_playing:
                # Lua first: every task/coroutine must be cancelled and
                # its scene overlay restored before physics tears down
                # and the ordinary Position/Rotation/Size restore runs --
                # otherwise a script could still be mid-mutation while
                # the rest of Stop is unwinding the world under it (see
                # Stage 3.0 report, "execution authority model").
                self._stop_lua()
                self._stop_character()
                self._stop_physics()
                # Stage 4.1: a Lua session may have written Environment.*
                # at runtime (apply_runtime_service_write() applies those
                # live but never touches self.services) -- restoring the
                # authored values here is what makes that change session-
                # only, the exact same contract every other runtime
                # overlay in this class already has (Position/Rotation/
                # Destroyed/Signals/...).
                self.apply_environment_settings(self.services.get("Environment", {}))
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
            # Stage 3.8: Workspace.Gravity is a positive MAGNITUDE (see
            # datamodel_schema.py); PhysicsWorld/physics.GRAVITY_Y are
            # signed (negative = downward, matching the existing Y-up
            # world) -- negate here, the one place editor-magnitude and
            # engine-signed-value meet. self.services is already a
            # complete, schema-defaulted snapshot by the time Play can
            # start (see load_world_snapshot()/__init__), so no extra
            # default-handling is needed here.
            gravity_magnitude = float(self.services.get("Workspace", {}).get("Gravity", 24.0))
            self._physics_world = physics.PhysicsWorld(gravity=-gravity_magnitude)
            body_count = 0
            for instance_id, entity in self.parts.items():
                record = self.instances.get(instance_id)
                if record is None or not record.enabled:
                    if physics.DEBUG_PHYSICS and record is not None:
                        print(f"[PHYSICS] skip id={instance_id} name={record.name!r} (disabled)")
                    continue
                if record.class_name in LIGHT_CLASS_NAMES:
                    # Stage 4.1 (local lighting foundation): lights are
                    # real has_3d_entity Instances (world-membership,
                    # picking, Clone/Destroy) but never get a Bullet body.
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

    # --------------------------------------------------------
    # STAGE 3.3: PLAY MODE CHARACTER CONTROLLER
    # --------------------------------------------------------

    def _collect_spawn_candidates(self) -> list[dict[str, Any]]:
        """Every enabled SpawnPoint Instance, identified by ClassName (not
        display name -- the schema already supports this, see shared/
        object_registry.py's SpawnPoint registration)."""
        candidates: list[dict[str, Any]] = []
        for instance_id, record in self.instances.items():
            if record.class_name != "SpawnPoint" or not record.enabled:
                continue
            properties = record.properties
            position = properties.get("Position", [0.0, 0.0, 0.0])
            size = properties.get("Size", [1.0, 1.0, 1.0])
            candidates.append({
                "id": instance_id,
                "position": (float(position[0]), float(position[1]), float(position[2])),
                "size": (float(size[0]), float(size[1]), float(size[2])),
            })
        return candidates

    def apply_runtime_service_write(self, service_name: str, properties: dict[str, Any]) -> None:
        """Stage 3.8: called from lua_runtime.RuntimeSceneLayer's service
        property overlay for every runtime Lua write to a root service
        (workspace.Gravity = X, StarterPlayer.CharacterWalkSpeed = X, ...)
        -- `properties` is the FULL current overlay for service_name
        (post-write), not just the single changed key, so derived values
        (jump_speed from JumpHeight+Gravity) can always be recomputed
        correctly regardless of write order. Applies REAL effects to the
        CURRENT Play session only -- never touches self.services (the
        persistent/serialized dict) or the network; discarded on Stop the
        same way every other Lua runtime overlay already is."""
        if service_name == "Workspace":
            gravity_magnitude = float(properties.get("Gravity", 24.0))
            if self._physics_world is not None:
                self._physics_world.set_gravity(-gravity_magnitude)
            if self._character_runtime is not None:
                self._character_runtime.controller.gravity = gravity_magnitude
            return

        if service_name == "Environment":
            # Stage 4.1: same "session-only, discarded on Stop" contract
            # every other runtime service overlay already has -- this
            # never touches self.services (the persisted dict), so
            # set_studio_playing()'s Stop path re-applying
            # self.services["Environment"] is what restores the authored
            # values, exactly like every other Lua runtime mutation in
            # this class.
            self.apply_environment_settings(properties)
            return

        if service_name != "StarterPlayer":
            return

        gravity_magnitude = -self._physics_world.gravity if self._physics_world is not None else 24.0
        if self._character_runtime is not None:
            controller = self._character_runtime.controller
            controller.walk_speed = float(properties.get("CharacterWalkSpeed", controller.walk_speed))
            if bool(properties.get("CharacterUseJumpPower", True)):
                controller.jump_speed = float(properties.get("CharacterJumpPower", controller.jump_speed))
            else:
                jump_height = float(properties.get("CharacterJumpHeight", 2.0))
                controller.jump_speed = math.sqrt(2.0 * gravity_magnitude * jump_height) if gravity_magnitude > 0.0 else 0.0
            # CharacterMaxSlopeAngle deliberately NOT applied here -- baked
            # into the Bullet node at construction (see character_controller.
            # CharacterController.__init__), takes effect on the NEXT
            # character spawn only (documented limitation, same as the
            # Inspector-edit path -- see _starter_player_controller_kwargs()).

        self._runtime_min_zoom = float(properties.get("CameraMinZoomDistance", self._runtime_min_zoom))
        self._runtime_max_zoom = float(properties.get("CameraMaxZoomDistance", self._runtime_max_zoom))
        self._clamp_third_person_distance_to_runtime_limits()

    def _starter_player_controller_kwargs(self) -> dict[str, float]:
        """Stage 3.8: translates the persistent StarterPlayer/Workspace
        service properties into character_controller.CharacterController's
        constructor kwargs -- called once per _start_character(), so a
        fresh character always starts from whatever is currently
        persistent (Inspector-edited or otherwise), never a stale value
        from a previous Play session. CharacterMaxSlopeAngle is baked into
        the Bullet node at construction (see CharacterController.__init__)
        and genuinely cannot be updated for an already-spawned character --
        this method (and therefore a fresh Play) is the only place a
        changed value ever takes effect, matching the spec's documented
        "apply on next character creation" limitation."""
        starter_player = self.services.get("StarterPlayer", {})
        gravity_magnitude = float(self.services.get("Workspace", {}).get("Gravity", 24.0))
        walk_speed = float(starter_player.get("CharacterWalkSpeed", character_controller.DEFAULT_WALK_SPEED))
        use_jump_power = bool(starter_player.get("CharacterUseJumpPower", True))
        if use_jump_power:
            jump_speed = float(starter_player.get("CharacterJumpPower", character_controller.DEFAULT_JUMP_SPEED))
        else:
            # v = sqrt(2 * g * h) -- standard projectile-launch-velocity
            # formula for reaching height h under gravity g. g=0 correctly
            # gives v=0 (floats away at whatever moves it, never NaN/inf).
            jump_height = float(starter_player.get("CharacterJumpHeight", 2.0))
            jump_speed = math.sqrt(2.0 * gravity_magnitude * jump_height) if gravity_magnitude > 0.0 else 0.0
        max_slope = float(starter_player.get("CharacterMaxSlopeAngle", character_controller.DEFAULT_MAX_SLOPE_DEGREES))
        return {
            "walk_speed": walk_speed,
            "jump_speed": jump_speed,
            "gravity": gravity_magnitude,
            "max_slope_degrees": max_slope,
        }

    def _start_character(self) -> None:
        """Creates the Play-session CharacterRuntime AND its visual rig --
        called AFTER _start_physics() (needs self._physics_world.
        bullet_world to already exist; see character_controller.py's "do
        not introduce a second Bullet world" constraint) and BEFORE
        _start_lua(). Wrapped in try/except that ALWAYS prints a full
        traceback and guarantees no half-created Bullet node OR visual rig
        survives a failed spawn, for the same reason _start_physics()/
        _start_lua() are (see their docstrings) -- a failed spawn must not
        leave Play looking broken with zero console explanation, must
        still allow Stop, and must not corrupt the editor scene (nothing
        here ever touches record.properties or self.instances)."""
        self._character_runtime = None
        self._character_visual = None
        if self._physics_world is None:
            print("[CHARACTER] _start_character(): no physics world available -- character NOT spawned.")
            return

        runtime: character_controller.CharacterRuntime | None = None
        visual: character_rig.CharacterVisualRig | None = None
        try:
            capsule_half_height = character_controller.DEFAULT_HEIGHT / 2.0
            chosen = character_controller.select_spawn_point(self._collect_spawn_candidates())
            if chosen is not None:
                spawn_position = character_controller.spawn_position_from_point(chosen, capsule_half_height)
                print(f"[CHARACTER] SpawnPoint selected: {chosen['id']}")
            else:
                spawn_position = character_controller.fallback_spawn_position(capsule_half_height)
                print("[CHARACTER] no SpawnPoint found -- using fallback spawn position")

            runtime = character_controller.CharacterRuntime(
                self._physics_world.bullet_world,
                spawn_position,
                initial_yaw_degrees=self.player_yaw,
                **self._starter_player_controller_kwargs(),
            )
            self._character_runtime = runtime
            print(f"[CHARACTER] spawned at {spawn_position}")
            # Stage 3.9: lets Touched fire when a Part overlaps/contacts the
            # player (see physics.py's poll_new_contacts() docstring) --
            # "__character__" is a sentinel id, not a real RuntimeSceneLayer
            # instance; LuaRuntimeManager._fire_touched() special-cases it.
            if self._physics_world is not None:
                self._physics_world.register_external_node("__character__", runtime.controller.node)

            visual = character_rig.CharacterVisualRig(initial_yaw_degrees=self.player_yaw)
            visual.set_first_person(not self.third_person_enabled)
            self._character_visual = visual
            self._apply_third_person_camera()
            print(f"[CHARACTER] visual rig created ({visual.entity_count()} entities)")
        except Exception:
            print("[CHARACTER] EXCEPTION in _start_character() -- Play mode continues but no character was spawned:")
            traceback.print_exc()
            if visual is not None:
                try:
                    visual.destroy()
                except Exception:
                    pass
            if runtime is not None:
                try:
                    runtime.destroy()
                except Exception:
                    pass
            self._character_runtime = None
            self._character_visual = None

    def _stop_character(self) -> None:
        if self._character_visual is not None:
            self._character_visual.destroy()
            self._character_visual = None
        if self._character_runtime is None:
            return
        if self._physics_world is not None:
            self._physics_world.unregister_external_node(self._character_runtime.controller.node)
        self._character_runtime.destroy()
        self._character_runtime = None
        # Always leave the editor camera in its normal first-person
        # transform on Stop, regardless of whichever view mode Play was
        # last in -- toggle_third_person() is the only other place camera.
        # position/parent change, and if the user stopped while still in
        # third-person, nothing else would otherwise reset it before the
        # editor's own camera state (restored separately by
        # set_studio_playing()) takes over.
        self.third_person_enabled = False
        # Stage 3.8: LockFirstPerson is session-local (StarterPlayer's
        # persistent value is untouched) -- always clear on Stop so a
        # leftover lock never bleeds into the editor or into the next
        # Play session before set_studio_playing() re-derives it fresh.
        self._camera_mode_locked_first_person = False
        camera.parent = self.camera_pitch_pivot
        camera.position = Vec3(0, 0, 0)
        print("[CHARACTER] removed")

    def update_character(self) -> None:
        """Reads held movement keys + mouse-look delta into the Play
        CharacterRuntime and applies movement to its Bullet capsule --
        call BEFORE update_physics() runs this frame's doPhysics() step
        (see character_controller.CharacterController.apply_movement's
        docstring). Camera/position sync happens separately, AFTER the
        physics step, in sync_character_camera()."""
        runtime = self._character_runtime
        if runtime is None:
            return

        runtime.input.forward = bool(held_keys["w"])
        runtime.input.backward = bool(held_keys["s"])
        runtime.input.left = bool(held_keys["a"])
        runtime.input.right = bool(held_keys["d"])
        runtime.input.jump_held = bool(held_keys["space"])
        runtime.input.captured = self._mouse_look_captured()

        if runtime.input.captured:
            if self._qt_look_available():
                delta_x, delta_y = self._poll_qt_look_delta()
                if delta_x != 0.0 or delta_y != 0.0:
                    runtime.camera.apply_delta(delta_x, delta_y)
            elif mouse.locked:
                # Non-embedded (--no-embed diagnostic) fallback path --
                # mirrors update_mouse_look()'s own scaling exactly rather
                # than going through CharacterCamera.apply_delta() (whose
                # default sensitivity matches the embedded/QT_LOOK_
                # SENSITIVITY_* path, a different unit convention than
                # mouse.velocity here).
                runtime.camera.yaw_degrees = wrap_angle(
                    runtime.camera.yaw_degrees + mouse.velocity[0] * MOUSE_SENSITIVITY_X
                )
                runtime.camera.pitch_degrees = clamp(
                    runtime.camera.pitch_degrees + mouse.velocity[1] * MOUSE_SENSITIVITY_Y,
                    runtime.camera.min_pitch,
                    runtime.camera.max_pitch,
                )

        runtime.step(ursina_time.dt)

    def sync_character_camera(self) -> None:
        """Call AFTER update_physics() has stepped the shared Bullet world
        this frame -- copies the capsule's resulting position onto
        local_player (camera-follow, since camera_pitch_pivot/camera stay
        parented to local_player exactly as in editor free-look) and
        applies the Play camera's yaw/pitch. Also mirrors those into
        self.player_yaw/player_pitch so the pre-existing send_transform()
        network message and the Play/Stop position-snapshot restore (see
        set_studio_playing) keep working unchanged -- CharacterCamera's own
        yaw/pitch remain the source of truth during Play; player_yaw/pitch
        is just kept in sync as a read-only mirror for that existing
        machinery, never the other way around."""
        runtime = self._character_runtime
        if runtime is None:
            return
        position = runtime.synced_position()
        self.local_player.position = Vec3(position[0], position[1], position[2])
        self.player_yaw = runtime.camera.yaw_degrees
        self.player_pitch = runtime.camera.pitch_degrees
        self.local_player.rotation = Vec3(0, self.player_yaw, 0)
        self.camera_pitch_pivot.rotation = Vec3(-self.player_pitch, 0, 0)
        camera.rotation_z = 0

        # Stage 3.4: the visual rig follows using this SAME post-step
        # position -- feet_position, not the capsule's own center (the
        # rig's own root is its feet, not its pelvis; see
        # character_rig.CharacterVisualRig.update()'s docstring).
        # Deliberately NOT parented to local_player (see that same
        # docstring for why), so this call is the rig's only per-frame
        # link to the controller.
        visual = self._character_visual
        if visual is not None:
            feet_position = (position[0], position[1] - runtime.controller.half_height, position[2])
            visual.update(
                ursina_time.dt,
                feet_position,
                runtime.controller.horizontal_velocity(),
                runtime.controller.is_grounded(),
                runtime.controller.vertical_velocity(),
            )

    def release_play_input_capture(self) -> None:
        """Explicit, idempotent release of Play's mouse-look capture --
        deliberately callable from OUTSIDE Ursina's own input() event loop
        (see main()'s QApplication.focusChanged/applicationStateChanged
        wiring, and _poll_qt_look_delta()'s own focus check below). This is
        the fix for the known bug where clicking the embedded viewport
        could leave Qt controls unclickable until process restart: capture
        used to only ever release via Ursina's "escape"/"left mouse down"
        key handling in input(), which requires the native Panda window to
        still have OS keyboard focus -- but the continuous QCursor.setPos()
        re-centering in _poll_qt_look_delta() ran every frame regardless,
        so once the user tried to click a DIFFERENT Qt widget (Explorer,
        Output, a menu...), the cursor snapping back to the viewport center
        60 times a second made it physically impossible for that click to
        ever land, which meant focus could never actually move away from
        the viewport to let Escape (or anything else) reach it either --
        a genuine deadlock. Safe to call any time, including when nothing
        is captured or Play isn't running (both branches below are no-ops
        in that case).

        Viewport-interaction rewrite: also unconditionally resets
        editor_look_active and viewport_focused -- every caller of this
        method (Escape, a click landing outside the viewport, the app
        losing OS foreground focus) is exactly a "the viewport just lost
        input ownership" event in EDITOR mode too, not just Play. Fixes a
        real, confirmed-by-code-reading gap: this method used to stop the
        mouse-look/cursor capture but leave editor_look_active itself
        True, so update()'s `elif self.editor_look_active and not
        self.gizmo.dragging:` branch kept calling update_mouse_look()/
        update_flight() every frame after a focus-loss mid-RMB-drag, even
        though capture had already been released -- "losing focus must
        always release captured mouse state safely" (spec) means the
        FLAG has to go too, not just the cursor's visible state."""
        self.editor_look_active = False
        self.viewport_focused = False
        if self._mouse_look_captured():
            self._stop_mouse_look()
            if self.studio_playing:
                print("[CHARACTER] Play input released")
            # Stage 3.5: whatever keys UserInputService still thought were
            # held gets a synthetic InputEnded here -- covers every path
            # that reaches this method (Escape, Qt focus loss,
            # applicationStateChanged), not just Escape specifically, so
            # "stuck key" can never survive losing capture for any reason.
            if self._lua_gameplay is not None:
                self._lua_gameplay.release_all_keys()
        self._log_viewport_lifecycle("release_play_input_capture")

    def play_input_state(self) -> str:
        """Stage 3.7: pure derivation of the current Play input-ownership
        state from the handful of independent flags that already exist
        (studio_playing, third_person_enabled, _mouse_look_captured()) --
        deliberately NOT a separately-tracked field of its own, so it can
        never drift out of sync with the flags that actually drive
        behavior elsewhere in this class. See the PLAY_INPUT_STATE_*
        constants on this class for the five possible results."""
        if not self.studio_playing:
            return self.PLAY_INPUT_STATE_EDITOR_UI
        if self.third_person_enabled:
            if self._mouse_look_captured():
                return self.PLAY_INPUT_STATE_THIRD_PERSON_RMB_LOOK
            return self.PLAY_INPUT_STATE_THIRD_PERSON_FREE_CURSOR
        if self._mouse_look_captured():
            return self.PLAY_INPUT_STATE_FIRST_PERSON_CAPTURED
        return self.PLAY_INPUT_STATE_FIRST_PERSON_RELEASED

    def _start_lua(self) -> None:
        """Wrapped in try/except that ALWAYS prints a full traceback, for
        the same reason _start_physics() is: this runs through
        EngineBridge._adapter_call(), which would otherwise swallow the
        exception into a one-line Output log while Play looks like it
        succeeded (see Stage 2.4 report's identical concern for physics)."""
        try:
            self._lua_runtime = lua_runtime.LuaRuntimeManager(self)
            self._lua_runtime.add_diagnostic_listener(self._forward_lua_diagnostic)
            self._lua_runtime.start()
            self._forward_lua_session_started(self._lua_runtime.session_id)
        except Exception:
            print("[LUA_RUNTIME] EXCEPTION in _start_lua() -- Play mode continues but scripts did NOT start:")
            traceback.print_exc()
            self._lua_runtime = None
            return

        try:
            self._lua_gameplay = lua_gameplay_api.LuaGameplayContext(self, self._lua_runtime)
            self._lua_gameplay.start()
        except Exception:
            print("[LUA_GAMEPLAY] EXCEPTION in _start_lua() -- Play mode continues but Players/Character/UserInputService are NOT available to scripts this session:")
            traceback.print_exc()
            self._lua_gameplay = None

    def _forward_lua_diagnostic(self, diag: Any) -> None:
        """Stage 3.1: the code editor's gutter markers/Output-click
        navigation need the SAME diagnostic Stage 3.0 already logs as a
        formatted string, just structured. Forwarded through the adapter
        (which owns the Qt-side EngineBridge) rather than emitting a Qt
        signal directly from here -- this class has no Qt dependency."""
        if self.studio_adapter is not None:
            self.studio_adapter.on_lua_diagnostic(diag.script_id, diag.severity, diag.message, diag.line, diag.session_id)

    def _forward_lua_session_started(self, session_id: int) -> None:
        """Tells the editor's diagnostic gutter a fresh Play session just
        began, independent of whether that session ever reports a single
        diagnostic -- without this, a clean Play after an errored one would
        leave the previous session's stale gutter markers on screen
        forever (nothing would ever arrive to clear them)."""
        if self.studio_adapter is not None:
            self.studio_adapter.on_lua_session_started(session_id)

    def _stop_lua(self) -> None:
        if self._lua_gameplay is not None:
            # MUST run before LuaRuntimeManager.stop() below -- see
            # LuaGameplayContext.stop()'s docstring: CharacterRemoving
            # needs a still-live VM (and a still-live character/rig, which
            # _stop_character() hasn't torn down yet at this point in the
            # existing Stop order) to fire correctly.
            try:
                self._lua_gameplay.stop()
            except Exception:
                print("[LUA_GAMEPLAY] EXCEPTION in _stop_lua():")
                traceback.print_exc()
            self._lua_gameplay = None
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
            if self._lua_gameplay is not None:
                self._lua_gameplay.update(ursina_time.dt)
        except Exception:
            print("[LUA_RUNTIME] EXCEPTION in update_lua() -- stopping the Lua runtime for the rest of this Play session:")
            traceback.print_exc()
            self._stop_lua()

    def begin_editor_look(self) -> None:
        if self.studio_playing:
            return
        self.editor_look_active = True
        self._start_mouse_look()
        self._log_viewport_lifecycle("begin_editor_look")

    def end_editor_look(self) -> None:
        if self.studio_playing:
            return
        self.editor_look_active = False
        self._stop_mouse_look()
        self._log_viewport_lifecycle("end_editor_look")

    def _log_viewport_lifecycle(self, label: str) -> None:
        """Bug-report follow-up instrumentation ("RMB look works
        initially, then stops rotating the camera" / Alt+Tab recovery) --
        dumps every piece of state the report asked to have instrumented,
        in one place, so a future regression here doesn't need this
        re-derived from scratch. Gated behind DEBUG_VIEWPORT_LIFECYCLE
        (default False); a no-op call otherwise, safe to sprinkle at
        every relevant transition point."""
        if not DEBUG_VIEWPORT_LIFECYCLE:
            return
        container = self.qt_viewport_container
        focus_widget = QApplication.focusWidget()
        cursor_pos = QCursor.pos()
        print(
            f"[VIEWPORT_LIFECYCLE] {label}: "
            f"viewport_focused={self.viewport_focused} "
            f"editor_look_active={self.editor_look_active} "
            f"mouse_look_captured={self._mouse_look_captured()} "
            f"qt_look_last_pos={self._qt_look_last_pos} "
            f"cursor_pos=({cursor_pos.x()},{cursor_pos.y()}) "
            f"focus_widget={focus_widget!r} "
            f"is_container={focus_widget is container} "
            f"active_window={QApplication.activeWindow()!r} "
            f"gizmo_dragging={self.gizmo.dragging} "
            f"studio_playing={self.studio_playing} "
            f"yaw={self.player_yaw:.2f} pitch={self.player_pitch:.2f}"
        )

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
            # Stage 3.3: explicitly grant the container Qt focus here.
            # Capture can start two ways: the user clicking the viewport
            # (PandaWindowFocusFilter already moves Qt focus to the
            # container as part of that click, before Ursina even sees the
            # "left mouse down" that calls this) or Play auto-capturing on
            # start (set_studio_playing(True) calls this directly, e.g.
            # right after the user clicked the Qt Play button in the
            # ribbon -- focus is still on that button, NOT the viewport, at
            # this exact moment). Without this call, _poll_qt_look_delta()'s
            # own-focus-lost safety check (see its docstring) would find
            # focus is not on the container on the very next frame and
            # immediately release the capture that was just granted --
            # correct in spirit (capture should track real Qt focus) but
            # wrong here, since Play hasn't actually lost the viewport as
            # the active surface, it just never explicitly claimed it.
            container.setFocus(Qt.FocusReason.OtherFocusReason)
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

        # Stage 3.3 pointer-capture fix: once Qt focus has moved away from
        # the viewport for ANY reason (clicked Explorer/Output/Inspector, a
        # menu opened and grabbed focus, a dialog appeared...), stop
        # re-centering the cursor immediately instead of continuing to warp
        # it back to the viewport every frame -- that unconditional warp is
        # exactly what previously made it impossible to ever complete a
        # click on anything else (see release_play_input_capture()'s
        # docstring for the full failure chain). This check runs every
        # frame regardless of whether Ursina's own input() ever sees an
        # "escape" key, which it structurally cannot once the native Panda
        # window itself has lost keyboard focus.
        #
        # Bug-report follow-up ("RMB look works initially, then stops
        # rotating the camera"): the original check bailed on ANY
        # `focusWidget() is not container`, INCLUDING `focusWidget() is
        # None`. None is a legitimate, expected state once the embedded
        # viewport (a createWindowContainer()-wrapped FOREIGN native
        # window, see embed_panda_window()) genuinely holds real OS
        # keyboard focus via foreign_window.requestActivate() -- Qt's own
        # widget-focus bookkeeping does not reliably keep reporting
        # `container` once a foreign HWND owns real keyboard input.
        # Confirmed by code-path analysis: RMB release/cursor-restore
        # (which does NOT depend on this check at all -- see
        # _stop_mouse_look(), driven purely by native RMB-up reaching
        # Ursina's input()) kept working exactly when rotation silently
        # stopped, meaning capture correctly engaged/disengaged the whole
        # time and ONLY this focus-widget comparison was ever wrong.
        # Distinguishing None (focus genuinely belongs to the native
        # window -- keep rotating) from "some OTHER real Qt widget"
        # (Explorer, Inspector, a dialog -- focus genuinely left the
        # viewport -- stop) is what actually matters here;
        # PlayInputReleaseFilter (an app-wide, cursor-position-based
        # click detector, NOT a focusWidget() query) remains the primary/
        # reliable "the user clicked something else" signal this check
        # was only ever meant to complement, see its own docstring.
        current_focus = QApplication.focusWidget()
        if current_focus is not None and current_focus is not container:
            self._stop_mouse_look()
            self.editor_look_active = False
            self.viewport_focused = False
            if self.studio_playing:
                print("[CHARACTER] Play input released (Qt focus left the viewport)")
            self._log_viewport_lifecycle("_poll_qt_look_delta: focus lost")
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

        # Сетка на полу для Blender-style режима. Настоящий wireframe-меш
        # (Grid), а не текстура — линии остаются чёткими на любом зуме.
        # Чуть приподнята над ground (y=-7.99), чтобы не мерцать (z-fighting).
        # Purely a reference overlay (no collider, disabled by default) --
        # kept unconditional regardless of legacy_demo, unlike the actual
        # ground plane below.
        self.floor_grid = Entity(
            model=Grid(GRID_LINE_COUNT, GRID_LINE_COUNT),
            color=GRID_LINE_COLOR,
            scale=GRID_LINE_COUNT * GRID_LINE_SPACING,
            rotation_x=90,
            y=-7.99,
            unlit=True,
            enabled=False,
        )

        # The original PickADoor demo's hardcoded outdoor test level: a
        # walkable/collidable ground plane and 5 scattered test blocks,
        # created unconditionally regardless of which Place/template (if
        # any) is actually loaded -- i.e. visible independently of
        # WORLD_SNAPSHOT, which is exactly what normal SStudio mode must
        # not do (a Blank Place must render as empty, a Baseplate Place
        # must show only its own real Part). Gated behind --legacy-demo;
        # self.ground/self.static_blocks are None/[] otherwise, and
        # _system_objects() (client_studio.py) skips the matching
        # Terrain/Baseplate/Static Geometry Explorer entries in that case.
        self.ground: Entity | None = None
        self.static_blocks: list[Entity] = []

        if self.legacy_demo:
            self.ground = Entity(
                model="plane",
                texture="white_cube",
                texture_scale=(64, 64),
                color=GROUND_COLOR,
                scale=150,
                y=-8,
                collider="box",
            )

            blocks = (
                (-12, -5, 8, 4, 6, 4),
                (15, -4, 12, 6, 8, 6),
                (-20, -2, -15, 5, 12, 5),
                (20, 1, -20, 4, 18, 4),
                (0, -5, 25, 10, 6, 10),
            )

            for x, y, z, scale_x, scale_y, scale_z in blocks:
                block = Entity(
                    model="cube",
                    color=BLOCK_COLOR,
                    position=(x, y, z),
                    scale=(scale_x, scale_y, scale_z),
                    collider="box",
                )
                self.static_blocks.append(block)

        # Stage 4.1: color is NOT set here -- apply_environment_settings()
        # below (Environment.AmbientColor/AmbientIntensity) is the sole
        # owner, same "one clearly owned lifecycle" reasoning as
        # self._environment_fog/self.pbr_pipeline.
        self.ambient_light = AmbientLight()

        # shadows=False: suppresses Ursina's own DEFERRED (invoke(),
        # ~1 frame later) default shadow setup, which would otherwise call
        # its update_bounds() -- sizing the shadow frustum to the tight
        # bounds of the WHOLE scene, including the 1000-unit sky sphere
        # (SStudio's sky is a plain Entity, not a tracked ursina.prefabs.
        # sky.Sky instance, so update_bounds()'s sky-exclusion never
        # catches it) -- unusably low shadow resolution for a room-sized
        # scene. apply_environment_settings() below (and every later call
        # to it) is the sole, deliberate owner of self.sun.shadows instead.
        self.sun = DirectionalLight(shadows=False)
        self.sun.color = SUN_LIGHT_COLOR
        self.sun.look_at(Vec3(1, -1, -1))

        # Stage 4.1 (atmosphere slice): one Fog object, owned and mutated
        # in place for the whole process lifetime -- never recreated, same
        # "one clearly owned lifecycle" reasoning as self.pbr_pipeline.
        self._environment_fog = Fog("EnvironmentFog")
        self.apply_environment_settings(self.services.get("Environment", {}))

    def apply_environment_settings(self, properties: dict[str, Any]) -> None:
        """Applies Environment.* (Fog/Exposure/Shadows) to the live scene.
        The ONE place this happens -- called from create_world() (initial
        default), load_world_snapshot() (Place open/REPLACE_WORLD), Play
        start and Stop (see set_studio_playing()), and runtime Lua writes
        (see apply_runtime_service_write()) -- so there is exactly one
        code path to reason about for "where do these settings apply".

        Guarded by self._environment_state: repeatedly applying identical
        properties (e.g. every Play/Stop cycle when nothing changed) must
        not repeatedly rebuild the DirectionalLight's shadow buffer --
        Panda3D's set_shadow_caster() reallocates its shadow buffer on
        every call, so calling it every Play/Stop even with an unchanged
        value would be a real (if slow) leak-like buffer churn, not just
        pointless work."""
        if properties == self._environment_state:
            return
        self._environment_state = dict(properties)

        # Stage 4.1 (local lighting foundation): AmbientColor*AmbientIntensity
        # replaces the old hardcoded AMBIENT_LIGHT_COLOR constant -- see its
        # own comment above for why this specific value matters (it's what
        # makes shadow/local-light contrast visible at all). Independent of
        # self.pbr_pipeline (a plain Ursina/Panda3D AmbientLight, not a
        # simplepbr-owned setting), so it applies even if simplepbr isn't
        # installed.
        ambient_rgb = properties.get("AmbientColor", [68.0, 68.0, 82.0])
        ambient_intensity = max(0.0, float(properties.get("AmbientIntensity", 1.0)))
        self.ambient_light.color = color.rgba32(
            min(255, max(0, int(float(ambient_rgb[0]) * ambient_intensity))),
            min(255, max(0, int(float(ambient_rgb[1]) * ambient_intensity))),
            min(255, max(0, int(float(ambient_rgb[2]) * ambient_intensity))),
            255,
        )

        if self.pbr_pipeline is not None:
            fog_enabled = bool(properties.get("FogEnabled", False))
            self.pbr_pipeline.enable_fog = fog_enabled
            if fog_enabled:
                fog_color = properties.get("FogColor", [160.0, 165.0, 175.0])
                self._environment_fog.setColor(
                    float(fog_color[0]) / 255.0, float(fog_color[1]) / 255.0, float(fog_color[2]) / 255.0,
                )
                self._environment_fog.setExpDensity(float(properties.get("FogDensity", 0.01)))
                application.base.render.setFog(self._environment_fog)
            else:
                application.base.render.clearFog()

            self.pbr_pipeline.exposure = float(properties.get("Exposure", 0.0))

        shadows_enabled = bool(properties.get("ShadowsEnabled", True))
        # getattr(..., None), not self.sun.shadows: DirectionalLight's own
        # `shadows` default is applied via a ONE-FRAME-DEFERRED invoke()
        # (see ursina/lights.py), so immediately after construction
        # self.sun._shadows may not exist yet -- reading the property
        # directly here (this method runs synchronously right after
        # DirectionalLight() in create_world()) would raise AttributeError.
        if getattr(self.sun, "_shadows", None) != shadows_enabled:
            self.sun.shadows = shadows_enabled
        if shadows_enabled:
            # Deliberately NOT self.sun.update_bounds(): that method sizes
            # the shadow frustum to the TIGHT BOUNDS OF THE WHOLE SCENE,
            # including the unlit sky sphere (scale=1000, see
            # create_world()) -- SStudio's sky is a plain Entity, not an
            # instance of ursina.prefabs.sky.Sky, so update_bounds()'s own
            # sky-exclusion logic never catches it, producing a shadow map
            # spread across ~2000 world units (unusably low resolution for
            # a room-sized scene). ShadowDistance gives an explicit,
            # predictable, author-controlled frustum instead.
            distance = float(properties.get("ShadowDistance", 40.0))
            lens = self.sun._light.get_lens()
            lens.set_near_far(-distance, distance)
            lens.set_film_size(distance * 2.0, distance * 2.0)

    def toggle_background(self) -> None:
        if self.background_mode == "sky":
            self.background_mode = "grid"
            self.sky.enabled = False
            self.floor_grid.enabled = True
            if self.ground is not None:
                self.ground.color = GRID_GROUND_COLOR
            window.color = GRID_BACKGROUND_COLOR
        else:
            self.background_mode = "sky"
            self.sky.enabled = True
            self.floor_grid.enabled = False
            if self.ground is not None:
                self.ground.color = GROUND_COLOR
            window.color = SKY_COLOR

    def _trigger_editor_undo(self) -> None:
        """Bug-report follow-up: "Undo/Redo eventually stops responding".
        Root cause confirmed by architecture, not guessed: Ctrl+Z/Ctrl+Y
        are wired ONLY as QAction/QShortcut objects on StudioMainWindow
        (see _build_menu()'s self.undo_action/self.redo_action), which
        only ever fire for a keystroke that passes through QT'S OWN event
        loop. The embedded viewport is a createWindowContainer()-wrapped
        FOREIGN native window (see embed_panda_window()) -- once it holds
        real OS keyboard focus (which PandaWindowFocusFilter's
        foreign_window.requestActivate() deliberately gives it, so WASD/
        camera keys work at all), a keystroke typed while it has focus is
        delivered directly to Panda3D's own native window procedure and
        NEVER reaches Qt's event loop/shortcut map -- Qt's QAction system
        structurally cannot see it. "worked initially" was simply
        whatever moment Qt (not the viewport) still happened to hold real
        keyboard focus, before the user's first click into the viewport.

        Fixed by giving the viewport its OWN entry point into the exact
        same undo path Qt's QAction already uses (self.studio_adapter.
        undo(), unchanged -- see MultiplayerStudioAdapter.undo()) rather
        than a second, divergent implementation: same can_undo guard,
        same log message, same self.game.history.add_state_listener()
        notification that keeps the Edit-menu's enabled state/text in
        sync no matter which entry point triggered it."""
        if self.studio_adapter is not None:
            self.studio_adapter.undo()

    def _trigger_editor_redo(self) -> None:
        """See _trigger_editor_undo()'s docstring -- same fix, same
        reasoning, for Ctrl+Y and the Ctrl+Shift+Z alt-chord (mirroring
        _build_menu()'s self.redo_alt_shortcut)."""
        if self.studio_adapter is not None:
            self.studio_adapter.redo()

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
        # Модель самого себя (third-person avatar, toggled with V). PlayerVisual
        # eagerly loads player.glb in its constructor regardless of visibility --
        # that eager load is exactly what used to make normal SStudio startup
        # print "[MODEL] Файл найден: player.glb" on every launch, independently
        # of any Place/template. Gated behind --legacy-demo; self.local_visual is
        # None otherwise (toggle_third_person() below no-ops in that case). Not
        # needed for the Studio editing workflow -- RemotePlayer (other users'
        # avatars) is unaffected and still only loads on-demand when someone
        # actually joins.
        self.local_visual: PlayerVisual | None = None
        if self.legacy_demo:
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
                "Space / Q — вверх и вниз\n"
                "E — ускорение\n"
                "Мышь — обзор\n"
                "V — первое/третье лицо\n"
                "B — небо/сетка\n"
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

        # Stage 3.3: this label reports player.glb's on-disk status -- only
        # meaningful when --legacy-demo's PlayerVisual path can actually
        # use it (see create_local_visual()). In normal SStudio mode the
        # Play HUD no longer references player.glb at all, so showing this
        # by default would be confusing leftover legacy UI, not an actual
        # error -- hidden unless --legacy-demo is active.
        self.model_status_text: Text | None = None
        if self.legacy_demo:
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
        """Roblox-Studio-style "F to focus" -- bug-report follow-up: the
        old version moved the camera to `entity.world_position + (some
        fixed backward offset) + Vec3(0, 2.0, 0)` WITHOUT ever re-aiming
        the camera's yaw/pitch at the target, so the fixed "+2 up" put
        the camera's OWN eye-line above the object with nothing pointing
        it back down -- the crosshair ended up aiming at the horizon at
        the camera's new (raised) height, i.e. visibly above the object's
        real center, exactly the reported symptom.

        Fixed properly: computes the object's actual world-space bounds
        CENTER (not just its .position, which can differ once rotation
        is involved) by reusing _world_bounds_points() -- the exact same
        oriented-corner AABB math _model_pivot_world()'s own auto-pivot
        already relies on, not a new parallel implementation -- and backs
        the camera straight up along its CURRENT view direction (yaw/
        pitch left untouched) by a distance scaled to the object's own
        size. Camera position = center - forward*distance means the
        camera is now looking EXACTLY at center by construction (center
        sits distance units along the view ray from the new position) --
        no vertical fudge factor needed at all. Works for a plain Part/
        SpawnPoint (its own oriented box) and for a Model (combined
        bounds of every transformable descendant, same as the gizmo's
        own auto-pivot)."""
        part_id = self.selected_part_id
        if not part_id:
            return
        record = self.instances.get(part_id)
        if record is None:
            return

        if record.class_name == "Model":
            points: list[Vec3] = []
            for descendant_id in self._collect_transformable_descendants(part_id):
                points.extend(self._world_bounds_points(descendant_id))
        else:
            points = self._world_bounds_points(part_id)
        if not points:
            return

        xs = [p.x for p in points]
        ys = [p.y for p in points]
        zs = [p.z for p in points]
        center = Vec3(
            (min(xs) + max(xs)) / 2.0,
            (min(ys) + max(ys)) / 2.0,
            (min(zs) + max(zs)) / 2.0,
        )
        extent = Vec3(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))
        distance = max(extent.length() * FOCUS_DISTANCE_FACTOR, FOCUS_MIN_DISTANCE)

        forward_direction = forward_from_angles(self.player_yaw, self.player_pitch)
        self.local_player.position = center - forward_direction * distance

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
                if child_record is not None and child_record.class_name in ("Part", "SpawnPoint", "Model", "MeshPart"):
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

    def apply_service_property_edit(self, service_name: str, properties: dict[str, Any]) -> bool:
        """Root-service counterpart to apply_property_edit() -- see
        protocol.UPDATE_SERVICE_PROPERTY's docstring. Called only from
        ServicePropertyEditCommand.send_forward()/send_inverse() (editor-
        time, persistent, undoable edits) -- Lua runtime writes to
        workspace.Gravity/StarterPlayer.* are a SEPARATE, purely local
        overlay (see lua_runtime.RuntimeSceneLayer) that never calls this
        and never reaches the network at all."""
        if not self.network.connected_event.is_set():
            if self.studio_adapter is not None:
                self.studio_adapter.log("warning", "Нет подключения к серверу: изменение не отправлено.")
            return False
        self.network.send({
            "type": protocol.UPDATE_SERVICE_PROPERTY, "id": service_name, "properties": properties,
        })
        return True

    def export_services(self) -> dict[str, dict[str, Any]]:
        """Save Place counterpart to export_world() -- current persistent
        (editor-time) service properties, ready to hand straight to
        place_manager.save()/save_as()."""
        return {name: dict(props) for name, props in self.services.items()}

    def delete_instance(self, instance_id: str) -> bool:
        if not self.network.connected_event.is_set():
            return False
        self.network.send({"type": protocol.DELETE_PART, "id": instance_id})
        return True

    def request_replace_world(
        self,
        objects: list[dict[str, Any]],
        on_result: Callable[[bool, str], None],
        services: dict[str, dict[str, Any]] | None = None,
    ) -> str:
        """Stage 3.2: Create Place from template / Open Place. One atomic
        REPLACE_WORLD request; on_result(success, message) fires once the
        matching REPLACE_WORLD_RESULT arrives (see process_network_
        messages) -- never called synchronously, since the request has not
        even been sent yet when this method returns.

        Stage 3.8: `services` rides along in the same request when given
        (every current caller always provides it -- see place_manager.
        PlaceOperationResult.services); omitted entirely (not an empty
        dict) leaves the server's persistent service state untouched, see
        server.handle_replace_world's own docstring for why that
        distinction matters."""
        request_id = uuid.uuid4().hex
        if not self.network.connected_event.is_set():
            on_result(False, "Not connected to a server.")
            return request_id
        self._pending_replace_world[request_id] = on_result
        message: dict[str, Any] = {
            "type": protocol.REPLACE_WORLD,
            "request_id": request_id,
            "objects": objects,
        }
        if services is not None:
            message["services"] = services
        self.network.send(message)
        return request_id

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
            if key in ("left mouse down", "right mouse down"):
                # Viewport-interaction rewrite: Ursina only ever calls
                # input() for a key/button event once the native Panda
                # window genuinely has OS focus (Panda3D does not deliver
                # input otherwise) -- so reaching this branch at all is
                # itself proof the viewport has focus, independent of and
                # redundant with PandaWindowFocusFilter's own Qt-level
                # MouseButtonPress/FocusIn handling (see its docstring).
                # Deliberately reconfirmed here rather than assumed, since
                # the two event systems (Qt's filter vs. Panda's native
                # input) are not guaranteed to fire in a fixed order.
                self.viewport_focused = True
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
            elif key == "f":
                self.focus_selected_part()
            elif key == "scroll up":
                self.dolly_camera(EDITOR_ZOOM_STEP)
            elif key == "scroll down":
                self.dolly_camera(-EDITOR_ZOOM_STEP)
            elif key == "z" and held_keys["control"] and held_keys["shift"]:
                self._trigger_editor_redo()
            elif key == "z" and held_keys["control"]:
                self._trigger_editor_undo()
            elif key == "y" and held_keys["control"]:
                self._trigger_editor_redo()
            return

        # Stage 3.5: purely additive observer -- forwards every Play-mode
        # key event to UserInputService's InputBegan/InputEnded (only for
        # the small TRACKED_KEYS set; anything else is ignored internally,
        # see LuaGameplayContext.on_key_event()) without replacing or
        # reordering any of the existing handling below. Ursina's input()
        # is only ever invoked for a genuine key event on the embedded
        # viewport's own native window -- exactly why Script Editor/Qt
        # text-field typing never reaches here in the first place (same
        # native-focus boundary Stage 3.3's WASD movement already relies
        # on), so no separate "suppress gameplay input while a Qt text
        # field has focus" check is needed here.
        if self._lua_gameplay is not None:
            self._lua_gameplay.on_key_event(key)

        if key in ("escape", "escape up"):
            # Stage 3.3 spec: Escape always RELEASES capture (it no longer
            # toggles back on) -- re-capturing is exclusively via clicking
            # the viewport again, the next branch below. Both edges are
            # handled: confirmed via live testing that Panda3D's watcher
            # does not always deliver the "escape" keydown edge to Ursina's
            # input() while the embedded window is mid-recapture (only the
            # "escape up" edge reliably arrives in that case) -- handling
            # both is a safe, idempotent no-op when capture is already
            # released (see release_play_input_capture()'s own guard).
            self.release_play_input_capture()
        elif key == "right mouse down":
            # Stage 3.7: in third-person, RMB-hold is the ONLY way to enter
            # mouse-look -- a temporary capture that ends the instant RMB
            # is released (see "right mouse up" below), never on Escape
            # alone (nothing to release if RMB isn't held). In first-person,
            # capture is already permanent (entered via "left mouse down"
            # below or set_camera_mode()'s transition), so RMB here is a
            # deliberate no-op -- it must never SHORTEN first-person's
            # capture lifetime, only Escape does that.
            if self.third_person_enabled and not self._mouse_look_captured():
                self._start_mouse_look()
        elif key == "right mouse up":
            # Only ever releases the temporary third-person RMB-look --
            # first-person's permanent capture is untouched by RMB release
            # (see the "right mouse down" comment above).
            if self.third_person_enabled and self._mouse_look_captured():
                self._stop_mouse_look()
        elif key == "left mouse down":
            # Stage 3.7: a plain left click in third-person must NEVER
            # start mouse-look/pointer capture -- the free cursor stays
            # free (spec: "the viewport must not consume left-click events
            # merely to enter movement mode"). Left-clicking the viewport
            # already transfers gameplay keyboard focus on its own, via
            # PandaWindowFocusFilter, independent of anything in this
            # method. First-person keeps its original click-to-(re)capture
            # behavior unchanged.
            if not self.third_person_enabled and not self._mouse_look_captured():
                self._start_mouse_look()
                print("[CHARACTER] Play input captured")
        elif key == "scroll up":
            if self.third_person_enabled:
                self._adjust_third_person_distance(-THIRD_PERSON_ZOOM_STEP)
        elif key == "scroll down":
            if self.third_person_enabled:
                self._adjust_third_person_distance(THIRD_PERSON_ZOOM_STEP)
        elif key == "v":
            self.toggle_third_person()
        elif key == "b":
            self.toggle_background()

    def _apply_third_person_camera(self) -> None:
        """Applies self.third_person_enabled's camera transform --
        factored out so both toggle_third_person() (the V-key path) and
        _start_character() (which must apply whatever third_person_enabled
        was already set to, e.g. from a previous Play session, to a
        freshly created rig) stay in sync without duplicating the camera
        math. Switching view never touches native viewport embedding or
        pointer capture (Stage 3.4 spec) -- this only ever reparents/moves
        the existing `camera` Entity, the same one Stage 3.3's first-person
        view already uses."""
        camera.parent = self.camera_pitch_pivot
        if self.third_person_enabled:
            camera.position = Vec3(0, 0, -self._third_person_distance)
            camera.y = THIRD_PERSON_HEIGHT
        else:
            camera.position = Vec3(0, 0, 0)

    def _adjust_third_person_distance(self, delta: float) -> None:
        """Stage 3.7/3.8: mouse-wheel zoom while in third-person Play.
        Clamped to [self._runtime_min_zoom, self._runtime_max_zoom] --
        session-local values seeded from the persistent StarterPlayer.
        CameraMin/MaxZoomDistance at Play start (see set_studio_playing()),
        NOT the fixed THIRD_PERSON_MIN/MAX_DISTANCE module constants
        (Stage 3.7's original hardcoded 2-10 range, now only a fallback
        default). Only ever re-applies the camera transform (never touches
        capture state, never modifies/serializes the editor Camera
        Instance -- this only moves the runtime `camera` Entity, same as
        _apply_third_person_camera() elsewhere)."""
        self._third_person_distance = max(
            self._runtime_min_zoom,
            min(self._runtime_max_zoom, self._third_person_distance + delta),
        )
        if self.studio_playing and self.third_person_enabled:
            self._apply_third_person_camera()

    def _clamp_third_person_distance_to_runtime_limits(self) -> None:
        """Stage 3.8: called whenever self._runtime_min_zoom/max_zoom
        change mid-session (a runtime Lua write to StarterPlayer.CameraMin/
        MaxZoomDistance) -- spec: "current camera distance is clamped
        immediately if a runtime change makes it invalid"."""
        clamped = max(self._runtime_min_zoom, min(self._runtime_max_zoom, self._third_person_distance))
        if clamped != self._third_person_distance:
            self._third_person_distance = clamped
            if self.studio_playing and self.third_person_enabled:
                self._apply_third_person_camera()

    def _set_play_capture(self, captured: bool) -> None:
        """Stage 3.7: shared capture-transition helper for set_camera_mode()
        below -- only takes effect while actually in Play (mode switches in
        the editor, before/after Play, must never touch native pointer
        capture). `captured=True` is used when switching into first-person
        (acquire immediately); `captured=False` when switching into
        third-person (always release -- RMB-hold is the only way back in)."""
        if not self.studio_playing:
            return
        if captured:
            if not self._mouse_look_captured():
                self._start_mouse_look()
        else:
            self.release_play_input_capture()

    def set_camera_mode(self, third_person: bool) -> None:
        """Stage 3.5: explicit-set variant of the V-key toggle below, used
        by the Lua Character API's SetCameraMode("FirstPerson"/
        "ThirdPerson") -- see lua_gameplay_api.py. Shares the exact same
        rig-vs-legacy-avatar branching and _apply_third_person_camera()
        call toggle_third_person() already used; that method is now just
        `self.set_camera_mode(not self.third_person_enabled)`, so both
        callers can never drift out of sync with each other.

        Stage 3.7: also owns the capture transition between the two Play
        camera modes -- switching to third-person ALWAYS releases capture
        (free cursor is mandatory there; RMB-hold is the only way back in),
        switching to first-person immediately ACQUIRES it (permanent
        capture is mandatory there). This runs for both the rig branch and
        the legacy-avatar branch below, and is a no-op outside of Play (see
        _set_play_capture()).

        Stage 3.8: while StarterPlayer.CameraMode == "LockFirstPerson" for
        this Play session, switching TO third-person is silently refused
        (V has no error-reporting channel) -- switching to first-person is
        always allowed regardless (it's a no-op if already there). The Lua
        Character:SetCameraMode("ThirdPerson") API call checks this same
        flag itself, BEFORE calling here, so it can raise a proper Lua
        error instead of silently doing nothing (see
        lua_gameplay_api.character_set_camera_mode())."""
        third_person = bool(third_person)
        if third_person and self._camera_mode_locked_first_person:
            return
        if self._character_visual is not None:
            self.third_person_enabled = third_person
            self._character_visual.set_first_person(not third_person)
            self._apply_third_person_camera()
            self._set_play_capture(not third_person)
            return

        if self.local_visual is None:
            # No avatar to show in third person without --legacy-demo --
            # no-op rather than raising, so the V shortcut (and the Lua
            # API) stay harmless.
            return
        self.third_person_enabled = third_person
        self.local_visual.enabled = third_person
        self._apply_third_person_camera()
        self._set_play_capture(not third_person)

    def toggle_third_person(self) -> None:
        """Stage 3.4: prefers the new SStudio character rig (normal Play
        mode) over the legacy --legacy-demo PlayerVisual avatar, which this
        no longer requires to function -- V now works in ordinary Play
        sessions, not just --legacy-demo ones. Falls back to the old
        local_visual toggle when no rig exists (e.g. --legacy-demo without
        the character controller having spawned one, or before Play has
        started), preserving that path's previous behavior unchanged."""
        self.set_camera_mode(not self.third_person_enabled)

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
        """Editor-only noclip fly camera (see the call site's `not
        self.studio_playing` guard) -- Q/E replaced the bare Ctrl/Shift
        bindings for downward movement / speed boost so those keys are
        free for gameplay use (Q and E are both forwarded to
        UserInputService's InputBegan/InputEnded during Play -- this
        method never runs then, so there is no conflict either way).
        Direction/meaning unchanged: Q still moves down (paired with
        Space moving up), E still boosts flight speed."""
        forward_input = held_keys["w"] - held_keys["s"]
        right_input = held_keys["d"] - held_keys["a"]

        down_pressed = held_keys["q"]
        vertical_input = held_keys["space"] - down_pressed

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

        boost_pressed = held_keys["e"]

        speed = FLIGHT_SPEED
        if boost_pressed:
            speed *= BOOST_MULTIPLIER

        self.local_player.position += (
            movement.normalized() * speed * ursina_time.dt
        )

        self.local_player.rotation_x = 0
        self.local_player.rotation_z = 0

    def dolly_camera(self, step: float) -> None:
        """Viewport-interaction rewrite: editor-mode mouse-wheel camera
        dolly -- moves the free camera forward/backward along its current
        view direction by a fixed step per wheel tick (a discrete event,
        not a per-frame rate, so unlike update_flight() this is NOT
        scaled by ursina_time.dt). Editor-only, mirrors update_flight()'s
        own `not self.studio_playing` guard at its call site in input()."""
        forward_direction = forward_from_angles(self.player_yaw, self.player_pitch)
        self.local_player.position += forward_direction * step

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
                raw_services = message.get("services")
                if isinstance(parts, list):
                    self.load_world_snapshot(parts, raw_services if isinstance(raw_services, dict) else None)
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
            elif message_type == protocol.SERVICE_PROPERTY_UPDATED:
                service_name = str(message.get("id", ""))
                raw_properties = message.get("properties")
                if service_name and isinstance(raw_properties, dict):
                    self.services.setdefault(service_name, datamodel_schema.default_properties(service_name))
                    self.services[service_name].update(raw_properties)
                    if self.studio_adapter is not None:
                        self.studio_adapter.on_service_property_updated(service_name, raw_properties)
            elif message_type == protocol.REPLACE_WORLD_RESULT:
                request_id = str(message.get("request_id", ""))
                callback = self._pending_replace_world.pop(request_id, None)
                if callback is not None:
                    success = bool(message.get("success", False))
                    result_message = str(message.get("message", ""))
                    callback(success, result_message)

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
        *,
        confirmed: bool = True,
    ) -> None:
        """Shared by the MODEL_TRANSFORMED happy path and the corrective
        'current' snapshot attached to TRANSFORM_MODEL_REJECTED — same
        application logic either way, so a rejection can self-heal the
        local preview through the exact same code as a normal echo.
        confirmed=False (the rejection path) must NOT dirty the Place --
        it is re-syncing to the ALREADY-authoritative state, not applying
        a new one."""
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
            if confirmed:
                self.studio_adapter.mark_place_dirty()

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
            self._apply_model_transform_payload(
                model_id, current.get("pivot"), current.get("descendants"), confirmed=False,
            )

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

    def load_world_snapshot(self, parts: list[dict[str, Any]], raw_services: dict[str, Any] | None = None) -> None:
        # Stage 3.2: spawn_instance()/remove_instance() below call
        # on_instance_created/updated/deleted per object, which would
        # otherwise mark a just-created/just-opened Place dirty from its
        # own load -- see MultiplayerStudioAdapter.begin_snapshot_reload.
        if self.studio_adapter is not None:
            self.studio_adapter.begin_snapshot_reload()
        try:
            incoming_ids = {str(item.get("id", "")) for item in parts if isinstance(item, dict)}
            for stale_id in list(self.instances):
                if stale_id not in incoming_ids:
                    self.remove_instance(stale_id)
            for part_data in parts:
                if isinstance(part_data, dict):
                    self.spawn_instance(part_data)
            # Stage 3.8: a full snapshot's "services" (present on every
            # WORLD_SNAPSHOT the current server sends -- initial connect
            # AND REPLACE_WORLD's broadcast) always REPLACES the whole
            # dict wholesale, same as parts above -- this is a brand-new
            # authoritative world, not a merge.
            self.services = datamodel_schema.sanitize_services_snapshot(raw_services)
            # Stage 4.1: a brand-new authoritative world means a brand-new
            # authoritative Environment too -- reapply fog/exposure/
            # shadows to match whatever this Place actually has saved
            # (or the schema defaults, for a Place with none yet).
            self.apply_environment_settings(self.services.get("Environment", {}))
        finally:
            if self.studio_adapter is not None:
                self.studio_adapter.end_snapshot_reload()
        # A WORLD_SNAPSHOT is a brand-new authoritative world (initial
        # connect, or a reconnect) -- every stored instance id/parent/
        # property reference an Undo/Redo command might hold could now be
        # stale or mean something different. See Stage 2.5 spec §15.
        self.history.clear()
        if self.studio_adapter is not None:
            self.studio_adapter.sync_full_scene()

    def _build_part_entity(self, properties: dict[str, Any], class_name: str = "Part") -> Entity | None:
        """Строит Ursina-Entity для типов с has_3d_entity=True. Part/SpawnPoint
        остаются кубом (различаются только дефолтными properties); MeshPart
        (Stage 4.1) вместо примитива грузит реальную геометрию через
        load_mesh_node() и реэродителит её на тот же transform/collider-root,
        так что Position/Size/Rotation/выделение/picking-коллайдер и Bullet-
        физика (см. physics.py — AABB по-прежнему из Size, не по треугольникам)
        работают identично обычному Part-у. PointLight/SpotLight (Stage 4.1)
        dispatch entirely separately, BEFORE the Part-cube properties are
        even indexed -- a light's own properties dict has no Size/Rotation/
        Transparency at all (see shared/object_registry.py's registration),
        so falling through to the code below would KeyError."""
        if class_name in LIGHT_CLASS_NAMES:
            return self._build_light_entity(class_name, properties)
        try:
            position = properties["Position"]
            size = properties["Size"]
            rotation = properties["Rotation"]
            rgb = properties["Color"]
            transparency = properties["Transparency"]
            root = Entity(
                model="cube" if class_name != "MeshPart" else None,
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

        if class_name == "MeshPart":
            self._apply_mesh_geometry(root, str(properties.get("MeshId", "")))
        return root

    def _build_light_entity(self, class_name: str, properties: dict[str, Any]) -> Entity | None:
        """PointLight/SpotLight (Stage 4.1 local lighting foundation).
        UrsinaPointLight/UrsinaSpotLight ARE Entities (see ursina/lights.py)
        that already self-register with Panda3D's render.setLight() on
        construction -- no separate "attach the light" step needed, same
        as self.ambient_light/self.sun in create_world(). A small always-
        visible-in-editor marker sphere is added as a child purely so the
        (otherwise geometry-less) light can be seen/selected/moved --
        its own enabled state is toggled off during Play by
        set_studio_playing() (see _light_markers), matching the spec's
        "must NOT render as game geometry during Play" requirement. The
        light entity itself keeps a small picking collider, same
        "always a picking collider, independent of gameplay semantics"
        reasoning _build_part_entity's own docstring gives for Parts."""
        try:
            position = properties.get("Position", [0.0, 0.0, 0.0])
            light_cls = UrsinaPointLight if class_name == "PointLight" else UrsinaSpotLight
            light_entity = light_cls(
                position=Vec3(float(position[0]), float(position[1]), float(position[2])),
                collider="box",
            )
        except (TypeError, ValueError, IndexError, KeyError) as error:
            print(f"[WORLD] Не удалось построить Entity света: битые properties: {error}")
            return None

        if class_name == "SpotLight":
            rotation = properties.get("Rotation", [0.0, 0.0, 0.0])
            try:
                light_entity.rotation = Vec3(float(rotation[0]), float(rotation[1]), float(rotation[2]))
            except (TypeError, ValueError, IndexError):
                pass

        marker = Entity(
            parent=light_entity,
            model="sphere",
            scale=0.3,
            color=color.rgba32(255, 255, 200, 255),
            unlit=True,
            collider=None,
        )
        light_entity._editor_marker = marker
        marker.enabled = not self.studio_playing

        self._apply_light_properties(light_entity, class_name, properties)
        return light_entity

    def _apply_light_properties(self, entity: Entity, class_name: str, properties: dict[str, Any]) -> None:
        """The ONE place PointLight/SpotLight's Color/Intensity/Range/Angle
        actually reach the real Panda3D light node -- called from
        _build_light_entity (initial build), _apply_instance_properties
        (editor/Inspector edits), and RuntimeSceneLayer._apply_visual (Lua
        writes -- see its own docstring for why this Panda3D-specific work
        lives here rather than in lua_runtime.py). Range/Intensity are
        deliberately a small, creator-friendly abstraction over Panda3D's
        raw quadratic-attenuation coefficients (spec: "do not expose low-
        level attenuation coefficients directly unless unavoidable"):
        constant=1, linear=0, quadratic=1/Range^2 -- a standard, simple,
        documented convention (brightness is roughly halved at
        distance=Range), not a claim of physical correctness."""
        light = getattr(entity, "_light", None)
        if light is None:
            return
        try:
            rgb = properties.get("Color", [255, 255, 255])
            intensity = max(0.0, float(properties.get("Intensity", 1.0)))
            light.setColor((
                float(rgb[0]) / 255.0 * intensity,
                float(rgb[1]) / 255.0 * intensity,
                float(rgb[2]) / 255.0 * intensity,
                1.0,
            ))
            light_range = max(0.01, float(properties.get("Range", 8.0)))
            quadratic = 1.0 / (light_range * light_range)
            light.setAttenuation(Vec3(1.0, 0.0, quadratic))
            if class_name == "SpotLight":
                angle = max(1.0, min(179.0, float(properties.get("Angle", 45.0))))
                light.getLens().setFov(angle, angle)
                # Stage 4.1 (local lighting foundation): SpotLight shadows
                # ARE enabled -- a Spotlight uses one PerspectiveLens, the
                # same "one shadow camera" shape DirectionalLight already
                # uses successfully, unlike PointLight (which would need a
                # 6-face cube shadow map -- genuinely more complex/costly,
                # deliberately deferred, not implemented this pass). Small
                # fixed 512x512 map (half DirectionalLight's 1024, since
                # these are local/smaller-scale fixtures) -- not exposed as
                # an authorable property, always on for SpotLight, always
                # off for PointLight. Guarded the same way DirectionalLight
                # is (only call setShadowCaster once) since it reallocates
                # a real buffer every call.
                if not getattr(entity, "_shadow_caster_enabled", False):
                    light.setShadowCaster(True, 512, 512)
                    entity._shadow_caster_enabled = True
        except (TypeError, ValueError, IndexError, AttributeError):
            pass

    def _set_light_markers_visible(self, visible: bool) -> None:
        """Toggles every PointLight/SpotLight's editor-only marker sphere
        (see _build_light_entity) -- called from set_studio_playing() at
        Play start (False) and Stop (True), same "editor-only visual must
        not render as game geometry during Play" pattern already used for
        self.selection_highlight. The light itself (and its illumination)
        is never touched here -- only the small visible marker child."""
        for entity in self.parts.values():
            marker = getattr(entity, "_editor_marker", None)
            if marker is not None:
                marker.enabled = visible

    def _apply_mesh_geometry(self, entity: Entity, mesh_id: str) -> None:
        """(Re)loads a MeshPart's visual geometry onto `entity`, replacing
        whatever mesh geometry it currently carries. `entity` itself stays
        the unscaled-by-mesh transform/collider root built by
        _build_part_entity (its own `scale` already carries Size, exactly
        like a Part's cube) -- the loaded model node is reparented as a
        plain Panda child, matching the already-proven
        model_debug_viewer.py pattern. The previous geometry is tracked via
        entity._mesh_node/_mesh_placeholder and torn down explicitly rather
        than by scanning entity.children, since a raw reparentTo()'d Panda
        NodePath is not one of Ursina's own tracked child Entities. On any
        failure (empty MeshId, missing file, bad asset) falls back to a
        visibly-distinct magenta placeholder cube instead of leaving the
        Part invisible, so a broken reference is obvious in the viewport,
        not silently blank. Destroying `entity` itself (Destroy()/Stop)
        still cleans up whichever of the two is active for free -- both are
        genuine Panda scene-graph descendants of entity's own NodePath."""
        previous_node = getattr(entity, "_mesh_node", None)
        if previous_node is not None:
            previous_node.removeNode()
        previous_placeholder = getattr(entity, "_mesh_placeholder", None)
        if previous_placeholder is not None:
            destroy(previous_placeholder)
        entity._mesh_node = None
        entity._mesh_placeholder = None

        node, error = load_mesh_node(mesh_id) if mesh_id else (None, "MeshId is empty")
        if node is not None:
            node.reparentTo(entity)
            node.setPos(0, 0, 0)
            node.setHpr(0, 0, 0)
            entity._mesh_node = node
            return

        if mesh_id:
            print(f"[MESH] {error}")
        entity._mesh_placeholder = Entity(
            parent=entity,
            model="cube",
            color=color.rgba32(255, 0, 220, 255),
        )

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

        is_light = record.class_name in LIGHT_CLASS_NAMES
        try:
            # .get(...) here, not direct indexing -- PointLight/SpotLight
            # (Stage 4.1) don't carry Size at all, and PointLight has no
            # Rotation either (see shared/object_registry.py), so a
            # replace=True full-record update must not assume every
            # Part-shaped key exists.
            if (replace or "Position" in properties) and "Position" in merged:
                position = merged["Position"]
                entity.position = Vec3(float(position[0]), float(position[1]), float(position[2]))
            if (replace or "Size" in properties) and "Size" in merged:
                size = merged["Size"]
                entity.scale = Vec3(float(size[0]), float(size[1]), float(size[2]))
            if (replace or "Rotation" in properties) and "Rotation" in merged:
                rotation = merged["Rotation"]
                entity.rotation = Vec3(float(rotation[0]), float(rotation[1]), float(rotation[2]))
            if not is_light and (replace or "Color" in properties or "Transparency" in properties):
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
            if record.class_name == "MeshPart" and (replace or "MeshId" in properties):
                # Stage 4.1 follow-up: an Inspector edit to an EXISTING
                # MeshPart's MeshId must hot-swap its geometry immediately
                # (magenta placeholder -> real mesh), not just persist the
                # new value for the next Place open. Gated to MeshPart
                # only -- calling this unconditionally would attach a
                # placeholder mesh child to every ordinary Part on its
                # very first property edit.
                self._apply_mesh_geometry(entity, str(merged.get("MeshId", "")))
            if is_light and (
                replace or "Color" in properties or "Intensity" in properties
                or "Range" in properties or "Angle" in properties
            ):
                self._apply_light_properties(entity, record.class_name, merged)
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
            entity = self._build_part_entity(properties, class_name)
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

                if self.model_status_text is not None:
                    self.model_status_text.text = remote_player.visual.model_status
                    self.model_status_text.color = color.lime if remote_player.visual.using_custom_model else color.red

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
        if self.studio_playing:
            # Stage 3.3: Play input/movement now goes through the character
            # controller instead of the editor's noclip update_flight() --
            # update_mouse_look()'s free-look is reserved for editor RMB
            # free-look (editor_look_active) below. update_character() must
            # run BEFORE update_physics() (sets this frame's capsule
            # movement); sync_character_camera() must run AFTER it (reads
            # back the post-step position) -- see both methods' docstrings.
            self.update_character()
        elif not self.gizmo.dragging:
            # Viewport-interaction rewrite: WASD/Space/Q/E flight and RMB
            # look-rotation are now two INDEPENDENT gates, not one bundled
            # condition -- this is the actual fix for "must hold RMB for
            # WASD to work" (root-caused to this exact line: the old code
            # only ever called update_flight() when editor_look_active was
            # True, and editor_look_active is ONLY ever set True by RMB-
            # down, see begin_editor_look()). viewport_focused (true from
            # any click into the viewport, false the moment focus moves
            # away -- see PandaWindowFocusFilter/release_play_input_
            # capture()) is what now gates ordinary camera movement;
            # editor_look_active (RMB-down/up only) still independently
            # gates rotation. Both are skipped entirely while a gizmo
            # handle is being dragged, same as before.
            if self.viewport_focused:
                self.update_flight()
            if self.editor_look_active:
                self.update_mouse_look()
        if self.studio_playing:
            self.send_transform()
            self.update_physics()
            self.sync_character_camera()
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
        # Stage 3.2: set once by main()/StudioMainWindow's Place workflow.
        # Optional by design -- an adapter with no PlaceManager attached
        # (e.g. any headless test that builds one directly) simply never
        # marks anything dirty, exactly like before this stage existed.
        self.place_manager: Any = None
        # spawn_instance()/remove_instance() call on_instance_created/
        # updated/deleted for EVERY object during a mass WORLD_SNAPSHOT
        # reload (initial connect, or a Stage 3.2 REPLACE_WORLD success) --
        # without this guard a just-created/just-opened Place would look
        # dirty from its own load. See MultiplayerGame.load_world_snapshot.
        self._suppressing_dirty = False

    def set_place_manager(self, place_manager: Any) -> None:
        self.place_manager = place_manager

    def begin_snapshot_reload(self) -> None:
        self._suppressing_dirty = True

    def end_snapshot_reload(self) -> None:
        self._suppressing_dirty = False

    def mark_place_dirty(self) -> None:
        if self._suppressing_dirty:
            return
        if self.place_manager is not None:
            self.place_manager.mark_authoritative_edit()

    def replace_world(
        self,
        objects: list[dict[str, Any]],
        on_result: Callable[[bool, str], None],
        services: dict[str, dict[str, Any]] | None = None,
    ) -> bool:
        self.game.request_replace_world(objects, on_result, services)
        return True

    def export_world(self) -> list[dict[str, Any]]:
        """Stage 3.2 Save Place: the client's `game.instances` mirror is
        already kept in lockstep with the server through every existing
        confirmed round-trip, so Save needs no network request of its own
        -- just read out what is already authoritative. InstanceRecord
        does not mirror tags/attributes (nothing in the live UI ever reads
        them), so Places saved from here always carry empty tags/
        attributes; this matches how the client already treats them
        everywhere else, not a new limitation."""
        return [
            {
                "id": record.id,
                "class_name": record.class_name,
                "name": record.name,
                "parent_id": record.parent_id,
                "properties": dict(record.properties),
                "tags": [],
                "attributes": {},
                "enabled": record.enabled,
            }
            for record in self.game.instances.values()
        ]

    def export_services(self) -> dict[str, dict[str, Any]]:
        return self.game.export_services()

    def on_service_property_updated(self, service_name: str, properties: dict[str, Any]) -> None:
        """Called from MultiplayerGame.process_network_messages() on every
        SERVICE_PROPERTY_UPDATED broadcast (including echoes of this
        client's own edits) -- forwards to the bridge so Inspector/
        Explorer stay in sync, mirroring update_instance()'s equivalent
        role for ordinary Instances."""
        if self.bridge is not None:
            self.bridge.sync_service_property(service_name, properties)

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

    def on_lua_diagnostic(self, script_id: str, severity: str, message: str, line: Any, session_id: int) -> None:
        if self.bridge is not None:
            self.bridge.lua_diagnostic.emit(script_id, severity, message, line, session_id)

    def on_lua_session_started(self, session_id: int) -> None:
        if self.bridge is not None:
            self.bridge.lua_session_started.emit(session_id)

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
        # Camera/Lighting are reusable editor/service nodes (Roblox-Studio-
        # style Workspace services) -- always present, regardless of which
        # Place/template is loaded. Terrain/Baseplate/Static Geometry below
        # are NOT: they mirror the original PickADoor demo's hardcoded
        # ground plane and 5 test blocks (self.game.ground/static_blocks),
        # which only exist at all when launched with --legacy-demo -- see
        # MultiplayerGame.create_world(). Without that flag they must not
        # appear in Explorer, since they'd otherwise show up independently
        # of WORLD_SNAPSHOT for every Place including a genuinely empty
        # Blank one.
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
                id="system:lighting",
                name="Lighting",
                object_type="Lighting",
                parent="Workspace",
                can_collide=False,
                cast_shadow=False,
            ),
        ]

        if not self.game.legacy_demo:
            return objects

        objects.append(
            SceneObject(
                id="system:terrain",
                name="Terrain",
                object_type="Terrain",
                parent="Workspace",
                position=EditorVec3(0, -8, 0),
                size=EditorVec3(150, 1, 150),
                color="#5c7058",
                anchored=True,
            )
        )
        objects.append(
            SceneObject(
                id="system:baseplate",
                name="Baseplate",
                object_type="Baseplate",
                parent="Workspace",
                position=EditorVec3(0, -8, 0),
                size=EditorVec3(150, 1, 150),
                color="#5c7058",
                anchored=True,
            )
        )
        # Синтетическая (не сетевая) папка — просто чтобы StaticBlock_N
        # ниже собрались в Explorer в одну группу, как раньше.
        objects.append(
            SceneObject(
                id="system:static_geometry",
                name="Static Geometry",
                object_type="Folder",
                parent="Workspace",
            )
        )
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
        self.bridge.sync_services(self.game.services)

    def on_instance_created(self, record: "InstanceRecord") -> None:
        if self.bridge is None:
            return
        # mark_place_dirty() must run BEFORE sync_upsert(): sync_upsert()
        # synchronously emits scene_changed, which StudioMainWindow._update_
        # title() reads place_manager.is_dirty from -- calling it after
        # would leave the title one edit stale until some unrelated later
        # event happened to refresh it.
        self.mark_place_dirty()
        self.bridge.sync_upsert(self.instance_to_scene_object(record))
        self.log("info", f'Created {record.class_name} "{record.name}"')
        if self.pending_create_count > 0:
            self.pending_create_count -= 1
            self.bridge.sync_select(record.id)
            self.game.select_part_from_studio(record.id)

    def on_instance_updated(self, record: "InstanceRecord") -> None:
        if self.bridge is not None:
            self.mark_place_dirty()
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
            self.mark_place_dirty()
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

        # Stage 3.8 fix: StarterPlayerScripts is inserted through this
        # generic Insert Object path (unlike StarterCharacter, which has
        # its own dedicated create_starter_character() above) -- mirror
        # server._singleton_conflict()'s "at most one anywhere" rule here
        # too, purely as an immediate, no-round-trip warning. The server
        # remains the actual authority and still rejects a duplicate even
        # if this check somehow passed a stale local scene.
        if object_type == "StarterPlayerScripts":
            for record in self.game.instances.values():
                if record.class_name == "StarterPlayerScripts":
                    self.log("warning", "Only one StarterPlayerScripts is allowed.")
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

    def create_starter_character(self) -> bool:
        """Stage 3.8: creates a Model named EXACTLY "StarterCharacter"
        under StarterPlayer -- the special-role predicate every
        authoritative check uses is datamodel_schema.is_starter_character(),
        never a dedicated ClassName (spec: "StarterCharacter is not a root
        service and should not be introduced as a fake service class").
        This client-side pre-check is UX only (an immediate, no-round-trip
        warning for the common case) -- server.py's _singleton_conflict()
        remains the actual authority and would silently reject a duplicate
        even if this check somehow passed a stale local scene."""
        for record in self.game.instances.values():
            if datamodel_schema.is_starter_character(record.class_name, record.name, record.parent_id or "Workspace"):
                self.log("warning", "A StarterCharacter already exists under StarterPlayer.")
                return False
        command = editor_history.CreateObjectCommand(
            "Insert StarterCharacter", "Model", None, "StarterPlayer", datamodel_schema.STARTER_CHARACTER_NAME,
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

    def set_service_property(self, service_name: str, property_path: str, value: Any) -> bool:
        """Root-service counterpart to set_property() above -- see
        editor_history.ServicePropertyEditCommand's docstring. Only ever
        called for "properties.X" paths (EngineBridge.set_property()
        already validated X through datamodel_schema before reaching
        here); `record` here is self.game.services[service_name] (the
        server-confirmed persistent value), not an InstanceRecord."""
        if not property_path.startswith("properties."):
            return False
        key = property_path.split(".", 1)[1]
        before = self.game.services.get(service_name, {})
        command = editor_history.ServicePropertyEditCommand(
            service_name,
            f"Edit {service_name}.{key}",
            before_properties={key: before.get(key)},
            after_properties={key: value},
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


class PlayInputReleaseFilter(QObject):
    """Application-wide safety net for the Stage 3.3 pointer-capture fix.

    _poll_qt_look_delta()'s own per-frame QApplication.focusWidget() check
    (see its docstring) does not catch every case: confirmed empirically
    that clicking an item in the Explorer panel updates Explorer's own
    selection/highlight (and the 3D scene selection) WITHOUT ever changing
    QApplication.focusWidget() at all -- the viewport container silently
    remained "the focus widget" the entire time, even with a different
    panel now visibly selected and receiving the user's clicks. Relying on
    focusWidget() alone would leave capture (and the cursor-recentering
    warp loop) running underneath an Explorer click, exactly the class of
    stuck-pointer bug this stage exists to fix.

    This filter is installed on the whole QApplication (see main()) and
    catches the click itself: any MouseButtonPress whose target widget is
    not the viewport container (or a descendant of it) releases capture
    immediately, regardless of that widget's own FocusPolicy. Complements
    (does not replace) the per-frame focus check, which still independently
    covers keyboard-driven focus changes (e.g. Tab) that never generate a
    mouse press at all."""

    def __init__(self, container: QWidget, game: "MultiplayerGame") -> None:
        super().__init__(container)
        self.container = container
        self.game = game

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.MouseButtonPress:
            clicked = QApplication.widgetAt(QCursor.pos())
            if clicked is not None and clicked is not self.container and not self.container.isAncestorOf(clicked):
                self.game.release_play_input_capture()
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
            # Viewport-interaction rewrite: an actual click (or a genuine
            # Qt FocusIn) gives the editor viewport WASD/camera control --
            # deliberately excludes plain Enter (hover) so merely moving
            # the mouse over the viewport never grants control on its own,
            # matching "clicking gives focus", not "hovering gives focus".
            if event.type() != QEvent.Type.Enter and not self.game.studio_playing:
                self.game.viewport_focused = True
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
    parser.add_argument(
        "--place",
        default=None,
        help="Открыть указанный файл Place сразу при запуске, минуя стартовую страницу шаблонов",
    )
    parser.add_argument(
        "--legacy-demo",
        action="store_true",
        help=(
            "Developer-only: restore the original PickADoor demo content "
            "(player.glb avatar, hardcoded ground plane, 5 hardcoded test "
            "blocks) that normal SStudio mode no longer creates automatically. "
            "Not needed for template/Place workflows -- default is off."
        ),
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


def _on_template_activated(studio: StudioMainWindow, spec: sstudio_templates.TemplateSpec) -> None:
    """install_template_browser()'s on_template callback (Stage 3.2). Called
    with the TemplateSpec itself -- see _invoke_template_callback's
    flexible-arity dispatch in sstudio_templates.py, which passes the spec
    object here because this callback's sole parameter is named 'spec'."""
    if not studio.bridge.live_mode:
        studio.bridge.log("warning", "Templates require a live server connection.")
        return
    dialog = place_manager.PlaceCreateDialog(spec.name, studio.place_manager.projects_root, parent=studio)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return
    result = studio.place_manager.create_from_template(
        spec.template_id, dialog.result_name(), dialog.result_directory()
    )
    if not result.success:
        QMessageBox.critical(studio, "Create Place Failed", result.message)
        return
    # Stage 3.7: no longer switches to Scene eagerly here -- that's now
    # activate_scene_for_loaded_place()'s job, called from
    # _replace_world_and_report()'s success branch below (the same
    # chokepoint File > Open Place / Recent Places / --place go through),
    # so the switch only ever happens once the server has actually
    # confirmed the REPLACE_WORLD, not speculatively before it.
    studio._replace_world_and_report(result)


def _show_recent_places_dialog(studio: StudioMainWindow) -> None:
    dialog = place_manager.RecentPlacesDialog(studio.place_manager.recents, parent=studio)
    if dialog.exec() == QDialog.DialogCode.Accepted:
        path = dialog.chosen_path()
        if path:
            studio.open_recent_place(path)


def _on_navigation_requested(studio: StudioMainWindow, section: str) -> None:
    """install_template_browser()'s on_navigation callback (Stage 3.2). The
    attached Sidebar has no built-in destination for Recent/Projects/
    Archive -- wire each to the closest real behavior rather than leaving
    a click that silently does nothing (spec section 19)."""
    binding = studio.templates_binding
    if section == "recent":
        _show_recent_places_dialog(studio)
    elif section == "projects":
        studio._place_open()
    elif section == "archive":
        studio.bridge.log("info", "Archive is not implemented in this stage.")
    elif section == "home" and binding is not None:
        binding.show()


def _run_launcher_and_resolve_place_path(qt_app: QApplication) -> Optional[Path]:
    """Stage 3.9 UI-architecture fix: the Home/Projects/Templates page used
    to be a SECOND page inside StudioMainWindow.central_stack, embedded
    between Explorer and Inspector in the same top-level window as the
    actual 3D editor -- this shows it as a genuinely separate, standalone
    top-level window instead (sstudio_templates.SStudioTemplatesWindow,
    already built for exactly this purpose but previously only exercised
    by that module's own standalone demo main()), shown and fully resolved
    BEFORE any Ursina/Panda3D/network/StudioMainWindow state is created.

    This ordering is not optional: Panda3D's ShowBase (what Ursina(...)
    constructs) is a process-wide singleton (see character_controller.py's
    "do not introduce a second Bullet world" precedent for the same kind
    of constraint one layer down) -- the launcher must fully resolve its
    choice and close BEFORE Ursina(...) ever runs, not coexist alongside a
    live editor window.

    Blocks the calling thread via a nested QEventLoop (the same technique
    QDialog.exec() itself uses internally) until the user either:
      - picks or creates a Place -> returns its resolved file path so the
        caller can treat it exactly like an already-resolved --place
        argument (see main()'s own use of this), or
      - closes the launcher window -> returns None, so the caller can exit
        cleanly instead of falling through to build a full editor session
        for a project the user never actually chose.

    Uses its own throwaway place_manager.PlaceManager() -- deliberately
    NOT the instance StudioMainWindow constructs later (this window closes
    before that one exists) -- safe because PlaceManager is plain data
    (no engine/Qt-widget references, see its own docstring) and
    RecentPlacesStore persists through QSettings, so both instances
    observe the exact same on-disk Recents list; a Place created here via
    "New from Template" is fully written to disk before this function
    returns (place_manager.create_from_template() always does, success or
    not), so the caller re-opening it by path afterward sees the real,
    complete file, not something only this throwaway instance knows about.

    Bug-report follow-up: the very first version of this function ended
    the nested loop via `qt_app.lastWindowClosed.connect(loop.quit)` --
    confirmed EMPIRICALLY (isolated PySide6 6.11.1 repro, not guessed)
    that QApplication.lastWindowClosed simply never fires for a window
    closed while a NESTED QEventLoop (as opposed to the top-level
    QCoreApplication::exec()) is the one currently running -- regardless
    of quitOnLastWindowClosed's value either way. That signal-based
    design meant loop.exec() below never returned once the user picked a
    project (window.close() ran, but nothing ever unblocked the loop),
    so main()'s rest never executed and the editor window never
    appeared. Fixed by never depending on that signal at all: `_finish()`
    calls loop.quit() directly (the normal "developer picked something"
    path), and the window's own closeEvent is intercepted directly (the
    "user clicked the window's X button without picking anything" path)
    -- both call loop.quit() unconditionally, so there is no code path
    left that can leave this function's nested loop running forever, and
    no reliance on quitOnLastWindowClosed at all (it is never touched
    here, so the app's normal shutdown-on-last-window-closed behavior
    for the REAL editor window, later, is completely untouched)."""
    from PySide6.QtCore import QEventLoop

    launcher_place_manager = place_manager.PlaceManager()
    window = sstudio_templates.SStudioTemplatesWindow()
    # Real (C++-level) destruction on close, not just hide -- a launcher
    # is a one-shot window (this function runs exactly once per process,
    # see main()'s own comment), so there is no reason for it to keep
    # existing as a hidden top-level widget for the rest of the app's
    # lifetime once resolved. Also closes a real gap this had without it:
    # QApplication.topLevelWidgets() keeps listing a merely-hidden
    # QMainWindow indefinitely, which is exactly the kind of stale
    # reference future code (or a test) could accidentally pick up.
    window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    loop = QEventLoop()
    resolved: dict[str, Optional[Path]] = {"path": None}

    def _finish(path: Path) -> None:
        resolved["path"] = path
        window.close()
        loop.quit()

    def _on_template(spec: sstudio_templates.TemplateSpec) -> None:
        dialog = place_manager.PlaceCreateDialog(spec.name, launcher_place_manager.projects_root, parent=window)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        result = launcher_place_manager.create_from_template(
            spec.template_id, dialog.result_name(), dialog.result_directory()
        )
        if not result.success:
            QMessageBox.critical(window, "Create Place Failed", result.message)
            return
        _finish(result.path)

    def _on_navigation(section: str) -> None:
        # Mirrors _on_navigation_requested()'s in-editor mapping above, but
        # against launcher_place_manager/window instead of a live studio --
        # "home" is deliberately absent: this window already IS Home, a
        # click on it is a harmless no-op here.
        if section == "recent":
            dialog = place_manager.RecentPlacesDialog(launcher_place_manager.recents, parent=window)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                chosen = dialog.chosen_path()
                if chosen:
                    _finish(Path(chosen))
        elif section == "projects":
            start_dir = str(launcher_place_manager.projects_root)
            path, _ = QFileDialog.getOpenFileName(window, "Open Place", start_dir, SCENE_FILE_FILTER)
            if path:
                _finish(Path(path))
        elif section == "archive":
            QMessageBox.information(window, "Archive", "Archive is not implemented in this stage.")

    window.page.template_activated_spec.connect(_on_template)
    window.page.navigation_requested.connect(_on_navigation)

    # The "user closed the launcher without choosing anything" path --
    # intercepting closeEvent directly (confirmed reliable, unlike
    # lastWindowClosed -- see docstring above) rather than a signal.
    # Calling loop.quit() twice (once here, once from _finish() above,
    # since _finish() also calls window.close() which re-enters this
    # override) is harmless -- QEventLoop.quit() on an already-stopped
    # loop is a documented no-op.
    original_close_event = window.closeEvent

    def _on_close_event(event: Any) -> None:
        original_close_event(event)
        loop.quit()

    window.closeEvent = _on_close_event

    window.show()
    loop.exec()

    return resolved["path"]


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

    # Stage 3.9 UI-architecture fix: the standalone Home/Projects/Templates
    # launcher window is resolved to a concrete Place path (or "the user
    # closed it without choosing anything") BEFORE any Ursina/Panda3D/
    # network/StudioMainWindow state exists -- see
    # _run_launcher_and_resolve_place_path()'s own docstring for why this
    # ordering is required, not just stylistic. --place PATH bypasses the
    # interactive launcher entirely (unchanged from before this fix --
    # existing tooling/tests that rely on skipping the picker keep working
    # exactly as before); once resolved either way, `arguments.place` is
    # always set, so every later `if arguments.place:` branch below (the
    # deferred-open-on-connect flow, install_template_browser()'s
    # show_immediately=) needs no further special-casing for "launched via
    # the new launcher" vs. "launched via --place".
    if not arguments.place:
        launcher_place_path = _run_launcher_and_resolve_place_path(qt_app)
        if launcher_place_path is None:
            return 0
        arguments.place = str(launcher_place_path)
        # Bug-report follow-up: on Windows, hide()/WA_DeleteOnClose alone
        # were not enough to make the launcher visually disappear before
        # the editor appeared -- Ursina(...) below blocks the thread for
        # a real, human-noticeable amount of time (window creation,
        # simplepbr.init(), asset loading) WITHOUT pumping Qt's event
        # loop at all, and DWM appears to need that pump to actually
        # finish compositing the hide/close (and, per Alt+Tab, fully drop
        # the window) rather than leaving its last frame on screen.
        # Forcing a few explicit processEvents() passes right here, while
        # nothing else competes for the event loop, closes that gap.
        for _ in range(5):
            qt_app.processEvents()

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

    # Stage 4.1 (atmosphere slice): capture the Pipeline object -- it used
    # to be discarded entirely (simplepbr.init() called for its side
    # effect only), which meant nothing in SStudio could ever reconfigure
    # fog/exposure/shadows after startup. This is the ONE simplepbr.init()
    # call for the whole process; it must never be called again (a second
    # call would build a second, competing post-process pipeline, not
    # reconfigure the first one) -- see MultiplayerGame.pbr_pipeline /
    # apply_environment_settings() for the single owned-lifecycle contract
    # this feeds into.
    pbr_pipeline = None
    if SIMPLEPBR_AVAILABLE:
        pbr_pipeline = simplepbr.init()
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
        legacy_demo=arguments.legacy_demo,
        pbr_pipeline=pbr_pipeline,
    )

    # Stage 3.3 pointer-capture fix, supplementary safety net: Qt's own
    # focusWidget() tracking (already checked every frame in
    # MultiplayerGame._poll_qt_look_delta()) does not reliably change when
    # the whole APPLICATION loses OS foreground focus (Alt+Tab to another
    # app) -- it only fires on click and Tab, so an app-level release is
    # wired separately here to cover that case explicitly.
    def _on_app_state_changed(state: Qt.ApplicationState) -> None:
        if DEBUG_VIEWPORT_LIFECYCLE:
            print(f"[VIEWPORT_LIFECYCLE] applicationStateChanged -> {state!r}")
        if state != Qt.ApplicationState.ApplicationActive:
            game.release_play_input_capture()

    qt_app.applicationStateChanged.connect(_on_app_state_changed)

    bridge = EngineBridge(objects=[], live_mode=True)
    adapter = MultiplayerStudioAdapter(game)
    bridge.set_adapter(adapter)

    studio = StudioMainWindow(bridge=bridge)
    studio.setWindowTitle("Live Server — Pick A Door Studio")
    studio.viewport_frame.scene_title.setText("Live Server")
    adapter.set_place_manager(studio.place_manager)

    embedded = False
    if not arguments.no_embed:
        embedded = embed_panda_window(studio, bridge, game)
    play_input_release_filter: PlayInputReleaseFilter | None = None
    if embedded and game.qt_viewport_container is not None:
        # Stage 3.3 pointer-capture fix, primary mechanism: see
        # PlayInputReleaseFilter's docstring for why the per-frame
        # focusWidget() check in _poll_qt_look_delta() alone is not
        # sufficient (confirmed empirically that clicking Explorer items
        # never changes QApplication.focusWidget()). The Python wrapper
        # is kept alive via play_input_release_filter for main()'s whole
        # lifetime (it never returns before the app quits) -- QObject
        # parenting alone only keeps the underlying C++ object alive and
        # is not sufficient to guarantee the Python-side eventFilter()
        # override keeps getting dispatched.
        play_input_release_filter = PlayInputReleaseFilter(game.qt_viewport_container, game)
        qt_app.installEventFilter(play_input_release_filter)
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

    # Stage 3.2 crash fix: pass host=studio.central_stack explicitly -- this
    # is the permanent outer QStackedWidget StudioMainWindow._build_central()
    # installs as the central widget during normal construction, BEFORE
    # embed_panda_window() ever runs. Passing it directly makes
    # install_template_browser() take its isinstance(target, QStackedWidget)
    # branch, which only calls stack.addWidget(page) for the brand-new
    # template page -- it never calls takeCentralWidget()/setCentralWidget()
    # or reparents the existing editor widget. Without host=, a QMainWindow
    # with an already-set plain central widget falls through to a DIFFERENT
    # branch that DOES take/reparent/reset the central widget -- safe before
    # embedding, but fatal after it (see _build_central's docstring: doing
    # this after embed_panda_window() had already realized a native HWND for
    # the embedded container's ancestor chain and raw-Win32 SetParent()'d the
    # foreign Panda3D window under it caused an access violation on
    # studio.show()).
    #
    # With no --place given it shows on top immediately (show_immediately
    # defaults True); with --place it stays behind the already-visible
    # editor (QStackedWidget defaults to showing the first-added widget,
    # i.e. the original editor, when show() is never called).
    # Connected directly to the page's own signals below rather than via
    # install_template_browser()'s on_template=/on_navigation= (which route
    # through a short-lived _EditorAdapter instance that install_template_
    # browser() does not return or keep any reference to -- once this call
    # returns, nothing keeps that adapter alive, and its bound-method slots
    # stop firing). Connecting straight to `page`'s signals ties the
    # lifetime of these callbacks to `page` itself, which the returned
    # TemplateBrowserBinding (and the QStackedWidget it lives in) keeps
    # alive for as long as the window exists.
    templates_binding = sstudio_templates.install_template_browser(
        studio,
        host=studio.central_stack,
        show_immediately=not arguments.place,
    )
    templates_binding.page.template_activated_spec.connect(
        lambda spec: _on_template_activated(studio, spec)
    )
    templates_binding.page.navigation_requested.connect(
        lambda section: _on_navigation_requested(studio, section)
    )
    studio.set_templates_binding(templates_binding)

    if arguments.place:
        # request_replace_world() requires an established websocket
        # connection, which is still in progress at this point in main()
        # (MultiplayerGame connects on its background asyncio thread) --
        # poll until connected_event is set, then run the normal Open
        # Place path exactly once (adds to Recents on success, same as
        # File > Open Place).
        deferred_place_path = Path(arguments.place)
        deferred_state = {"done": False}
        deferred_timer = QTimer()
        deferred_timer.setInterval(100)

        def _try_open_deferred_place() -> None:
            if deferred_state["done"]:
                return
            if not game.network.connected_event.is_set():
                return
            deferred_state["done"] = True
            deferred_timer.stop()
            studio._open_place_path(deferred_place_path)

        deferred_timer.timeout.connect(_try_open_deferred_place)
        deferred_timer.start()

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