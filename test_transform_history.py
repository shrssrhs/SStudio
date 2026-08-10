"""Behavioral regression tests for gizmo-drag transform Undo/Redo (Move,
Rotate, Scale) on a single Part -- see the Stage 3.9 report's "narrower
Redo bug" follow-up.

These are deliberately NOT source-inspection tests. Each test drives the
REAL, unmodified production pipeline end-to-end and asserts on both the
canonical instance state (record.properties) and the actual rendered
Entity transform:

  gizmo drag-release (simulated: optimistic local apply + real network
  send, exactly like _apply_gizmo_result()/apply_property_edit() do)
    -> editor_history.push_optimistic() (exactly like
       _push_part_transform_history() does)
    -> CommandManager.undo()/redo() (the real class)
    -> MultiplayerGame.apply_property_edit() (the real, unmodified
       bound method) -- a real network send over a REAL asyncio
       websocket connection to a REAL server.py instance (its own
       thread/event loop, an isolated port -- not the shared dev port)
    -> the real server's handle_update_property()/broadcast_to_all()
    -> the real client_studio.NetworkClient's background thread +
       incoming queue
    -> MultiplayerGame.process_network_messages() ->
       MultiplayerGame.update_instance() ->
       MultiplayerGame._apply_instance_properties() (the real,
       unmodified bound method) -- which updates BOTH record.properties
       and a REAL headless ursina.Entity's .position/.rotation/.scale.

Only the "game" object driving all of this is a lightweight duck-typed
stand-in (no Qt Inspector/Explorer, no Panda3D window) -- everything
that actually touches history/network/property state is the real
production code.

Follows this project's existing test convention: plain top-level-
assertion script, run directly, offscreen Qt platform, headless Ursina
window (Ursina(window_type="none")), real background threads for the
server and NetworkClient (same pattern as test_launcher_handoff.py's
QTimer-driven async flows and test_stage39_api.py's real Bullet world --
this stage's own bug was specifically about async/threaded behavior a
synchronous mock could not have caught).
"""
import asyncio
import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, '.')

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from ursina import Entity, Ursina, Vec3 as UrsinaVec3

ursina_app = Ursina(window_type="none")

import server
import client_studio as cs
import editor_history
from shared import protocol

TEST_PORT = 18790

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"ok: {message}")


# ---------------- real, isolated local server ----------------

def _run_server() -> None:
    async def _serve() -> None:
        async with server.serve(
            server.client_handler, "127.0.0.1", TEST_PORT,
            ping_interval=20, ping_timeout=20, max_size=64 * 1024,
        ):
            broadcast_task = asyncio.create_task(server.broadcast_world_state())
            try:
                await stop_future
            finally:
                broadcast_task.cancel()
                try:
                    await broadcast_task
                except asyncio.CancelledError:
                    pass

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    global stop_future
    stop_future = loop.create_future()
    loop.run_until_complete(_serve())


threading.Thread(target=_run_server, daemon=True).start()
time.sleep(0.5)  # let the listener actually bind before any client connects


# ---------------- minimal duck-typed host reusing REAL bound methods ----------------

class _FakeStatusText:
    def __init__(self) -> None:
        self.text = ""


class _FakeGame:
    """Not a MultiplayerGame -- a lightweight stand-in wide enough for
    process_network_messages()'s dispatch to run without a real Qt/Ursina
    editor window. apply_property_edit/update_instance/
    _apply_instance_properties/process_network_messages are the REAL,
    unmodified MultiplayerGame methods, bound here."""

    apply_property_edit = cs.MultiplayerGame.apply_property_edit
    update_instance = cs.MultiplayerGame.update_instance
    _apply_instance_properties = cs.MultiplayerGame._apply_instance_properties
    process_network_messages = cs.MultiplayerGame.process_network_messages

    def __init__(self, network: "cs.NetworkClient") -> None:
        self.network = network
        self.instances: dict[str, "cs.InstanceRecord"] = {}
        self.parts: dict[str, Entity] = {}
        self.selected_part_id: str | None = None
        self.studio_adapter = None
        self._gizmo_dragging_instance_id: str | None = None
        self.status_text = _FakeStatusText()
        self.history = editor_history.CommandManager(self)

    def update_selection_highlight(self, entity: Entity) -> None:
        pass

    def handle_connected_message(self, message: dict) -> None:
        pass

    def update_remote_players(self, players: dict) -> None:
        pass

    def load_world_snapshot(self, parts: list, services) -> None:
        pass


def _make_connected_game(player_name: str) -> tuple["_FakeGame", "cs.NetworkClient"]:
    network = cs.NetworkClient(f"ws://127.0.0.1:{TEST_PORT}", player_name)
    network.start()
    check(
        network.connected_event.wait(timeout=5.0),
        f"{player_name}: real NetworkClient connected to the real local server",
    )
    game = _FakeGame(network)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        game.process_network_messages()
        time.sleep(0.02)
    return game, network


def _create_real_part(game: "_FakeGame", network: "cs.NetworkClient") -> str:
    network.send({
        "type": protocol.CREATE_PART,
        "class_name": "Part",
        "properties": {"Position": [0.0, 0.0, 0.0], "Rotation": [0.0, 0.0, 0.0], "Size": [1.0, 1.0, 1.0]},
    })
    created_id: str | None = None
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and created_id is None:
        for message in network.receive_all():
            if message.get("type") == protocol.PART_CREATED:
                part = message.get("part", {})
                created_id = str(part.get("id"))
                props = dict(part.get("properties", {}))
                game.instances[created_id] = cs.InstanceRecord(created_id, "Part", "Part", None, props, True)
                entity = Entity(model="cube", position=UrsinaVec3(0, 0, 0))
                entity.instance_properties = props
                game.parts[created_id] = entity
        if created_id is None:
            time.sleep(0.02)
    return created_id


def _pump(game: "_FakeGame", seconds: float = 0.8) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        game.process_network_messages()
        ursina_app.step()
        time.sleep(0.01)


def _read(entity: Entity, key: str) -> tuple[float, float, float]:
    v = {"Position": entity.position, "Rotation": entity.rotation, "Size": entity.scale}[key]
    return (round(v.x, 4), round(v.y, 4), round(v.z, 4))


def _do_transform(
    game: "_FakeGame", entity: Entity, record: "cs.InstanceRecord", instance_id: str,
    key: str, before_vals: list, after_vals: list, label: str, command_cls,
) -> "editor_history.PartTransformCommand | editor_history.ResizeCommand":
    """Mirrors try_begin_gizmo_drag() -> live drag -> end_gizmo_drag() ->
    _apply_gizmo_result()/_push_part_transform_history() exactly: an
    optimistic LOCAL apply (record + Entity) first, then the real
    network send (force_network=True on release), THEN the history
    command is built and pushed via push_optimistic() -- never via
    perform() (see push_optimistic()'s own docstring: the forward action
    was already sent by other code)."""
    record.properties[key] = list(after_vals)
    if key == "Position":
        entity.position = UrsinaVec3(*after_vals)
    elif key == "Rotation":
        entity.rotation = UrsinaVec3(*after_vals)
    elif key == "Size":
        entity.scale = UrsinaVec3(*after_vals)
    game.apply_property_edit(instance_id, properties={key: list(after_vals)})
    _pump(game)
    command = command_cls(
        instance_id, label,
        before_properties={key: list(before_vals)},
        after_properties={key: list(after_vals)},
    )
    game.history.push_optimistic(command)
    return command


# ============================================================
# Move
# ============================================================

def test_move_undo_redo_seven_step() -> None:
    """1) establish A, 2) commit one Move history command exactly like a
    gizmo drag-release does, 3) assert B on both record + real Entity,
    4) Undo, 5) assert A on both, 6) Redo, 7) assert B returns exactly --
    on both record.properties AND the actual rendered Entity.position."""
    game, network = _make_connected_game("MoveProbe")
    instance_id = _create_real_part(game, network)
    check(instance_id is not None, "Move test: real server created a real Part")
    entity = game.parts[instance_id]
    record = game.instances[instance_id]
    game.selected_part_id = instance_id

    a, b = [0.0, 0.0, 0.0], [5.0, 2.0, -3.0]
    _do_transform(game, entity, record, instance_id, "Position", a, b, "Move Part", editor_history.PartTransformCommand)

    check(record.properties["Position"] == b, f"Move: after drag-release, record Position == B, got {record.properties['Position']}")
    check(_read(entity, "Position") == tuple(b), f"Move: after drag-release, real entity.position == B, got {_read(entity, 'Position')}")

    game.history.undo()
    _pump(game)
    check(record.properties["Position"] == a, f"Move: after Undo, record Position == A, got {record.properties['Position']}")
    check(_read(entity, "Position") == tuple(a), f"Move: after Undo, real entity.position == A, got {_read(entity, 'Position')}")

    game.history.redo()
    _pump(game)
    check(record.properties["Position"] == b, f"Move: after Redo, record Position == B, got {record.properties['Position']}")
    check(_read(entity, "Position") == tuple(b), f"Move: after Redo, real entity.position == B, got {_read(entity, 'Position')}")

    network.stop()


# ============================================================
# Rotate
# ============================================================

def test_rotate_undo_redo_seven_step() -> None:
    game, network = _make_connected_game("RotateProbe")
    instance_id = _create_real_part(game, network)
    check(instance_id is not None, "Rotate test: real server created a real Part")
    entity = game.parts[instance_id]
    record = game.instances[instance_id]
    game.selected_part_id = instance_id

    a, b = [0.0, 0.0, 0.0], [0.0, 90.0, 0.0]
    _do_transform(game, entity, record, instance_id, "Rotation", a, b, "Rotate Part", editor_history.PartTransformCommand)

    check(record.properties["Rotation"] == b, f"Rotate: after drag-release, record Rotation == B, got {record.properties['Rotation']}")
    check(_read(entity, "Rotation") == tuple(b), f"Rotate: after drag-release, real entity.rotation == B, got {_read(entity, 'Rotation')}")

    game.history.undo()
    _pump(game)
    check(record.properties["Rotation"] == a, f"Rotate: after Undo, record Rotation == A, got {record.properties['Rotation']}")
    check(_read(entity, "Rotation") == tuple(a), f"Rotate: after Undo, real entity.rotation == A, got {_read(entity, 'Rotation')}")

    game.history.redo()
    _pump(game)
    check(record.properties["Rotation"] == b, f"Rotate: after Redo, record Rotation == B, got {record.properties['Rotation']}")
    check(_read(entity, "Rotation") == tuple(b), f"Rotate: after Redo, real entity.rotation == B, got {_read(entity, 'Rotation')}")

    network.stop()


# ============================================================
# Scale
# ============================================================

def test_scale_undo_redo_seven_step() -> None:
    game, network = _make_connected_game("ScaleProbe")
    instance_id = _create_real_part(game, network)
    check(instance_id is not None, "Scale test: real server created a real Part")
    entity = game.parts[instance_id]
    record = game.instances[instance_id]
    game.selected_part_id = instance_id

    a, b = [1.0, 1.0, 1.0], [3.0, 1.0, 2.0]
    _do_transform(game, entity, record, instance_id, "Size", a, b, "Scale Part", editor_history.ResizeCommand)

    check(record.properties["Size"] == b, f"Scale: after drag-release, record Size == B, got {record.properties['Size']}")
    check(_read(entity, "Size") == tuple(b), f"Scale: after drag-release, real entity.scale == B, got {_read(entity, 'Size')}")

    game.history.undo()
    _pump(game)
    check(record.properties["Size"] == a, f"Scale: after Undo, record Size == A, got {record.properties['Size']}")
    check(_read(entity, "Size") == tuple(a), f"Scale: after Undo, real entity.scale == A, got {_read(entity, 'Size')}")

    game.history.redo()
    _pump(game)
    check(record.properties["Size"] == b, f"Scale: after Redo, record Size == B, got {record.properties['Size']}")
    check(_read(entity, "Size") == tuple(b), f"Scale: after Redo, real entity.scale == B, got {_read(entity, 'Size')}")

    network.stop()


# ============================================================
# Chained A -> B -> C -> D, interleaving all three operations --
# catches stale snapshots or reference aliasing that a single-command
# test could miss.
# ============================================================

def test_chained_move_rotate_scale_catches_stale_snapshots() -> None:
    game, network = _make_connected_game("ChainProbe")
    instance_id = _create_real_part(game, network)
    check(instance_id is not None, "Chain test: real server created a real Part")
    entity = game.parts[instance_id]
    record = game.instances[instance_id]
    game.selected_part_id = instance_id

    PTC = editor_history.PartTransformCommand
    RC = editor_history.ResizeCommand

    # Two commands per property key (A->B->C->D), interleaved by kind
    # exactly like a user alternately dragging move/rotate/scale handles.
    sequence = [
        ("Position", [0, 0, 0], [5, 0, 0], "Move Part", PTC),
        ("Position", [5, 0, 0], [10, 0, 0], "Move Part", PTC),
        ("Rotation", [0, 0, 0], [0, 90, 0], "Rotate Part", PTC),
        ("Rotation", [0, 90, 0], [0, 180, 0], "Rotate Part", PTC),
        ("Size", [1, 1, 1], [2, 1, 1], "Scale Part", RC),
        ("Size", [2, 1, 1], [3, 1, 1], "Scale Part", RC),
    ]
    for key, before, after, label, cmd_cls in sequence:
        _do_transform(game, entity, record, instance_id, key, before, after, label, cmd_cls)

    check(record.properties["Position"] == [10.0, 0.0, 0.0], "Chain: Position == D after full chain")
    check(_read(entity, "Position") == (10.0, 0.0, 0.0), "Chain: real entity.position == D after full chain")
    check(record.properties["Rotation"] == [0.0, 180.0, 0.0], "Chain: Rotation == D after full chain")
    check(_read(entity, "Rotation") == (0.0, 180.0, 0.0), "Chain: real entity.rotation == D after full chain")
    check(record.properties["Size"] == [3.0, 1.0, 1.0], "Chain: Size == D after full chain")
    check(_read(entity, "Size") == (3.0, 1.0, 1.0), "Chain: real entity.scale == D after full chain")

    # Undo -> C, Undo -> B (Size only, mirrors "A -> B, B -> C, C -> D"
    # then "Undo -> C, Undo -> B" from the report's required protocol).
    game.history.undo()
    _pump(game)
    game.history.undo()
    _pump(game)
    check(record.properties["Size"] == [1.0, 1.0, 1.0], "Chain: Size == B(original) after 2x Undo")
    check(_read(entity, "Size") == (1.0, 1.0, 1.0), "Chain: real entity.scale == B(original) after 2x Undo")

    # Redo -> C, Redo -> D.
    game.history.redo()
    _pump(game)
    check(record.properties["Size"] == [2.0, 1.0, 1.0], "Chain: Size == C after 1x Redo")
    check(_read(entity, "Size") == (2.0, 1.0, 1.0), "Chain: real entity.scale == C after 1x Redo")

    game.history.redo()
    _pump(game)
    check(record.properties["Size"] == [3.0, 1.0, 1.0], "Chain: Size == D after 2x Redo")
    check(_read(entity, "Size") == (3.0, 1.0, 1.0), "Chain: real entity.scale == D after 2x Redo")

    # Unwind everything (6 commands total: 2 Move + 2 Rotate + 2 Size --
    # the undo stack again holds all 6 after the 2x Undo/2x Redo above).
    for _ in range(6):
        game.history.undo()
        _pump(game)
    check(record.properties["Position"] == [0.0, 0.0, 0.0], "Chain: Position == A after undoing everything")
    check(_read(entity, "Position") == (0.0, 0.0, 0.0), "Chain: real entity.position == A after undoing everything")
    check(record.properties["Rotation"] == [0.0, 0.0, 0.0], "Chain: Rotation == A after undoing everything")
    check(record.properties["Size"] == [1.0, 1.0, 1.0], "Chain: Size == A after undoing everything")

    # And redo everything back to D.
    for _ in range(6):
        game.history.redo()
        _pump(game)
    check(record.properties["Position"] == [10.0, 0.0, 0.0], "Chain: Position == D after redoing everything")
    check(_read(entity, "Position") == (10.0, 0.0, 0.0), "Chain: real entity.position == D after redoing everything")
    check(record.properties["Rotation"] == [0.0, 180.0, 0.0], "Chain: Rotation == D after redoing everything")
    check(record.properties["Size"] == [3.0, 1.0, 1.0], "Chain: Size == D after redoing everything")

    network.stop()


test_move_undo_redo_seven_step()
test_rotate_undo_redo_seven_step()
test_scale_undo_redo_seven_step()
test_chained_move_rotate_scale_catches_stale_snapshots()

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for message in FAILURES:
        print(f"  - {message}")
    sys.exit(1)
print("All transform-history Move/Rotate/Scale Undo/Redo behavioral tests passed.")
