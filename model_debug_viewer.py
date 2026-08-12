from __future__ import annotations

import builtins
import platform
from pathlib import Path

from panda3d.core import Filename, TransparencyAttrib, loadPrcFileData
from ursina import (
    AmbientLight,
    Button,
    DirectionalLight,
    Entity,
    Text,
    Ursina,
    Vec3,
    application,
    color,
    window,
)
from ursina.prefabs.editor_camera import EditorCamera


# ============================================================
# ПУТИ
# ============================================================

PROJECT_DIR = Path(__file__).resolve().parent
ASSETS_DIR = PROJECT_DIR / "assets"
PLAYER_MODEL_PATH = ASSETS_DIR / "player.glb"

ASSETS_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# НАСТРОЙКИ
# ============================================================

MODEL_TARGET_HEIGHT = 2.0

# Начальный поворот модели по Y. -90 = "влево" (как просили проверить).
# Если стрелка модели (перёд) смотрит не туда — крути кнопками/Q,E
# и сообщи итоговое значение, впишем его в MODEL_ROTATION_Y основного
# клиента.
INITIAL_MODEL_ROTATION_Y = -90.0
ROTATION_STEP = 15.0

ARROW_LENGTH = 1.5
ARROW_COLOR = color.rgba32(20, 255, 120, 220)
ARROW_TIP_COLOR = color.rgba32(20, 255, 120, 255)


# ============================================================
# ЗАГРУЗКА GLB
# ============================================================

def model_file_status() -> tuple[bool, str]:
    if not PLAYER_MODEL_PATH.exists():
        return False, f"Файл не найден: {PLAYER_MODEL_PATH}"

    if PLAYER_MODEL_PATH.stat().st_size <= 0:
        return False, f"Файл пустой: {PLAYER_MODEL_PATH}"

    return True, f"Файл найден: {PLAYER_MODEL_PATH.name}"


def load_model_node():
    file_ok, message = model_file_status()

    if not file_ok:
        return None, message

    panda_loader = getattr(builtins, "loader", None)

    if panda_loader is None:
        return None, "Panda3D loader ещё не создан"

    try:
        # Filename.from_os_specific конвертирует Windows-путь ("C:/...")
        # в формат Panda VFS ("/c/..."), иначе loader не находит файл.
        panda_filename = Filename.from_os_specific(str(PLAYER_MODEL_PATH))
        model_node = panda_loader.loadModel(panda_filename)
    except Exception as error:
        return None, f"Ошибка загрузки GLB: {type(error).__name__}: {error}"

    if model_node is None or model_node.isEmpty():
        return None, "Panda3D не смог загрузить модель"

    try:
        model_node.setTransparency(TransparencyAttrib.MNone)
        model_node.setTwoSided(True)
    except Exception:
        pass

    return model_node, message


def fit_model_to_height(model_node, target_height: float) -> None:
    minimum, maximum = model_node.getTightBounds()

    if minimum is None or maximum is None:
        return

    size = max(
        maximum.x - minimum.x,
        maximum.y - minimum.y,
        maximum.z - minimum.z,
    )

    if size <= 0.00001:
        return

    scale = target_height / size
    model_node.setScale(scale)

    minimum, maximum = model_node.getTightBounds()
    center_x = (minimum.x + maximum.x) / 2
    center_z = (minimum.z + maximum.z) / 2
    bottom_y = minimum.y

    model_node.setPos(-center_x, -bottom_y, -center_z)


# ============================================================
# ВЬЮЕР
# ============================================================

class ModelViewer(Entity):
    def __init__(self) -> None:
        super().__init__()

        self.body_visible = True
        self.arrow_visible = True
        self.model_rotation_y = INITIAL_MODEL_ROTATION_Y

        self.create_world()
        self.create_body()
        self.create_arrow()
        self.create_gui()

    # --------------------------------------------------------
    # МИР
    # --------------------------------------------------------

    def create_world(self) -> None:
        Entity(
            model="plane",
            texture="white_cube",
            texture_scale=(20, 20),
            color=color.rgb32(80, 90, 80),
            scale=30,
        )

        ambient = AmbientLight()
        ambient.color = color.rgba32(170, 170, 180, 255)

        sun = DirectionalLight()
        sun.color = color.rgba32(235, 225, 205, 255)
        sun.look_at(Vec3(1, -1, -1))

    # --------------------------------------------------------
    # ТЕЛО
    # --------------------------------------------------------

    def create_body(self) -> None:
        self.body_root = Entity(rotation=(0, self.model_rotation_y, 0))

        model_node, message = load_model_node()
        self.model_status = message
        self.has_model = model_node is not None

        if model_node is not None:
            model_node.reparentTo(self.body_root)
            fit_model_to_height(model_node, MODEL_TARGET_HEIGHT)
            self.model_node = model_node
        else:
            # Плейсхолдер на случай, если GLB не найден/не грузится —
            # чтобы можно было хотя бы проверить GUI и повороты.
            Entity(
                parent=self.body_root,
                model="cube",
                color=color.azure,
                scale=(0.9, 1.6, 0.55),
                y=0.8,
            )
            Entity(
                parent=self.body_root,
                model="sphere",
                color=color.light_gray,
                scale=0.65,
                y=1.9,
            )

        print(f"[MODEL] {message}")

    # --------------------------------------------------------
    # ОТЛАДОЧНАЯ СТРЕЛКА
    # --------------------------------------------------------

    def create_arrow(self) -> None:
        # Стрелка — неподвижный ориентир "мировой перёд" (+Z).
        # Сверяем с ней, куда смотрит перёд модели после поворота.
        self.arrow_root = Entity(position=(0, 1.0, 0))

        Entity(
            parent=self.arrow_root,
            model="cube",
            color=ARROW_COLOR,
            scale=(0.1, 0.1, ARROW_LENGTH),
            z=ARROW_LENGTH / 2,
        )

        Entity(
            parent=self.arrow_root,
            model="cone",
            color=ARROW_TIP_COLOR,
            scale=(0.3, 0.3, 0.45),
            position=(0, 0, ARROW_LENGTH),
            rotation_x=90,
        )

    # --------------------------------------------------------
    # GUI
    # --------------------------------------------------------

    def create_gui(self) -> None:
        self.status_text = Text(
            text=self.model_status,
            color=color.lime if self.has_model else color.red,
            position=(-0.87, 0.46),
            origin=(-0.5, 0.5),
            background=True,
        )

        self.rotation_text = Text(
            text=f"Поворот модели (Y): {self.model_rotation_y:.0f}°",
            position=(-0.87, 0.41),
            origin=(-0.5, 0.5),
            background=True,
        )

        Text(
            text=(
                "Камера: ПКМ — вращать, колесо — зум, "
                "средняя кнопка — пан\n"
                "1 — только стрелка   2 — только тело   "
                "3 — оба   Q/E — повернуть модель"
            ),
            position=(-0.87, 0.36),
            origin=(-0.5, 0.5),
            background=True,
        )

        button_scale = (0.22, 0.06)

        self.body_button = Button(
            text="Тело: ВКЛ",
            color=color.azure,
            scale=button_scale,
            position=(-0.55, -0.42),
        )
        self.body_button.on_click = self.toggle_body

        self.arrow_button = Button(
            text="Стрелка: ВКЛ",
            color=color.lime,
            scale=button_scale,
            position=(-0.3, -0.42),
        )
        self.arrow_button.on_click = self.toggle_arrow

        self.only_arrow_button = Button(
            text="Только стрелка",
            color=color.orange,
            scale=button_scale,
            position=(-0.05, -0.42),
        )
        self.only_arrow_button.on_click = self.show_only_arrow

        self.only_body_button = Button(
            text="Только тело",
            color=color.violet,
            scale=button_scale,
            position=(0.2, -0.42),
        )
        self.only_body_button.on_click = self.show_only_body

        self.rotate_left_button = Button(
            text="⟲ -15°",
            color=color.gray,
            scale=(0.14, 0.06),
            position=(-0.55, -0.49),
        )
        self.rotate_left_button.on_click = lambda: self.rotate_model(-ROTATION_STEP)

        self.rotate_right_button = Button(
            text="+15° ⟳",
            color=color.gray,
            scale=(0.14, 0.06),
            position=(-0.38, -0.49),
        )
        self.rotate_right_button.on_click = lambda: self.rotate_model(ROTATION_STEP)

    # --------------------------------------------------------
    # ДЕЙСТВИЯ
    # --------------------------------------------------------

    def toggle_body(self) -> None:
        self.body_visible = not self.body_visible
        self.body_root.enabled = self.body_visible
        self.body_button.text = f"Тело: {'ВКЛ' if self.body_visible else 'ВЫКЛ'}"

    def toggle_arrow(self) -> None:
        self.arrow_visible = not self.arrow_visible
        self.arrow_root.enabled = self.arrow_visible
        self.arrow_button.text = f"Стрелка: {'ВКЛ' if self.arrow_visible else 'ВЫКЛ'}"

    def show_only_arrow(self) -> None:
        self._set_visibility(body=False, arrow=True)

    def show_only_body(self) -> None:
        self._set_visibility(body=True, arrow=False)

    def show_both(self) -> None:
        self._set_visibility(body=True, arrow=True)

    def _set_visibility(self, body: bool, arrow: bool) -> None:
        self.body_visible = body
        self.arrow_visible = arrow
        self.body_root.enabled = body
        self.arrow_root.enabled = arrow
        self.body_button.text = f"Тело: {'ВКЛ' if body else 'ВЫКЛ'}"
        self.arrow_button.text = f"Стрелка: {'ВКЛ' if arrow else 'ВЫКЛ'}"

    def rotate_model(self, delta: float) -> None:
        self.model_rotation_y += delta
        self.body_root.rotation_y = self.model_rotation_y
        self.rotation_text.text = f"Поворот модели (Y): {self.model_rotation_y:.0f}°"

    # --------------------------------------------------------
    # ВВОД С КЛАВИАТУРЫ (дублирует кнопки)
    # --------------------------------------------------------

    def input(self, key: str) -> None:
        if key == "1":
            self.show_only_arrow()
        elif key == "2":
            self.show_only_body()
        elif key == "3":
            self.show_both()
        elif key == "q":
            self.rotate_model(-ROTATION_STEP)
        elif key == "e":
            self.rotate_model(ROTATION_STEP)


# ============================================================
# ЗАПУСК
# ============================================================

def main() -> None:
    application.asset_folder = ASSETS_DIR

    if platform.system() == "Darwin":
        # См. комментарий в client.py — на macOS без этого шейдеры
        # (GLSL 130/140+) не компилируются в легаси OpenGL 2.1-контексте.
        loadPrcFileData("", "gl-version 3 2")

    app = Ursina()

    window.title = "Model Debug Viewer"
    window.color = color.rgb32(35, 35, 40)
    window.fps_counter.enabled = True

    EditorCamera()

    ModelViewer()

    app.run()


if __name__ == "__main__":
    main()