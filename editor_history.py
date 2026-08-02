"""
Stage 2.5: authoritative Undo/Redo command history.

Architecture (see Stage 2.5 report for full rationale):

- ONE CommandManager, owned by MultiplayerGame (client_studio.py), driven by
  two things: (a) UI hook points that construct a Command AFTER capturing
  before/after state and call CommandManager.perform(command) for a brand
  new local action, and (b) MultiplayerGame.process_network_messages()
  forwarding every incoming message to CommandManager.on_network_message()
  so pending confirmations can resolve.
- Commands never touch Qt widgets or Ursina Entities directly. Every
  command's send_forward()/send_inverse() calls the SAME authoritative
  request methods on `host` (MultiplayerGame) that ordinary user actions
  already use (apply_property_edit / request_create_instance /
  delete_instance / request_set_parent / request_transform_model) -- undo
  and redo are indistinguishable from a normal edit as far as the server
  and the rest of the client are concerned.
- Confirmation policy (deliberately NOT uniform, see report):
    * Property-shaped commands (PropertyEditCommand / PartTransformCommand
      / ResizeCommand) are pushed to the undo stack OPTIMISTICALLY the
      first time they happen -- UPDATE_PROPERTY has no server-side reject
      path (server.py's handle_update_property sanitizes, never rejects
      the whole batch), so gating the FIRST occurrence behind a
      confirmation round-trip would only add latency with no correctness
      benefit.
    * Create/Delete/Reparent/ModelTransform commands DO wait for
      confirmation before entering the undo stack -- these have real
      reject paths (SET_PARENT_REJECTED, TRANSFORM_MODEL_REJECTED) or
      need server-assigned data (the new instance id from PART_CREATED)
      before the command is even well-formed.
    * Undo/Redo of ANY command, regardless of the above, ALWAYS goes
      through CommandManager's single pending-confirmation slot -- one
      in-flight undo/redo at a time, matching the explicit Stage 2.5
      requirement that repeated Ctrl+Z cannot launch overlapping inverse
      requests.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from shared import protocol

DEBUG_UNDO_REDO = False
MAX_HISTORY_DEPTH = 100
PENDING_TIMEOUT = 5.0


def _debug(message: str) -> None:
    if DEBUG_UNDO_REDO:
        print(f"[UNDO_REDO] {message}")


# ============================================================
# TRACKERS -- resolve a single in-flight request against incoming
# network messages. Each tracker is stateful and may itself send
# follow-up requests (see _RecreateSubtreeTracker).
# ============================================================

class _SingleIdTracker:
    """Confirms on the first message of `confirm_type` for this exact
    instance id; for property-shaped edits there is no reject path, so
    only a confirm type is tracked."""

    def __init__(self, instance_id: str, confirm_type: str) -> None:
        self.instance_id = instance_id
        self.confirm_type = confirm_type

    def on_message(self, host: Any, message_type: str, message: dict[str, Any]) -> Optional[str]:
        if message_type != self.confirm_type:
            return None
        if str(message.get("id", "")) != self.instance_id:
            return None
        return "confirm"


class _ReparentTracker:
    """SET_PARENT confirms via PART_UPDATED carrying the matching
    parent_id (see shared/protocol.py's PART_UPDATED comment), or is
    explicitly rejected via SET_PARENT_REJECTED."""

    def __init__(self, instance_id: str, target_parent_id: str) -> None:
        self.instance_id = instance_id
        self.target_parent_id = target_parent_id

    def on_message(self, host: Any, message_type: str, message: dict[str, Any]) -> Optional[str]:
        if str(message.get("id", "")) != self.instance_id:
            return None
        if message_type == protocol.SET_PARENT_REJECTED:
            return "reject"
        if message_type == protocol.PART_UPDATED and "parent_id" in message:
            return "confirm" if message.get("parent_id") == self.target_parent_id else None
        return None


class _CreateTracker:
    """Confirms on the next PART_CREATED matching the requested
    class_name (single-flight assumption -- see module docstring; the
    same heuristic client_studio.py's pre-existing pending_create_count
    already relied on for a near-identical problem). Captures the
    server-assigned id AND the server's canonical properties/parent_id/
    name into the command on confirm -- this is what lets a later Redo
    recreate the object exactly as it was originally confirmed (e.g. the
    actual spawn position picked by request_create_instance's auto-fill)
    rather than re-deriving a new one (e.g. the player's current facing)."""

    def __init__(self, command: "CreateObjectCommand") -> None:
        self.command = command

    def on_message(self, host: Any, message_type: str, message: dict[str, Any]) -> Optional[str]:
        if message_type != protocol.PART_CREATED:
            return None
        part = message.get("part")
        if not isinstance(part, dict) or part.get("class_name") != self.command.class_name:
            return None
        self.command.live_instance_id = str(part.get("id", ""))
        self.command.properties = dict(part.get("properties") or {})
        self.command.parent_id = part.get("parent_id")
        self.command.name = str(part.get("name", self.command.name))
        return "confirm"


class _DeleteTracker:
    """Confirms once every expected id has been seen in a PART_DELETED
    message (recursive delete sends one per removed descendant, leaves
    first -- see server.py's handle_delete_part)."""

    def __init__(self, expected_ids: set[str]) -> None:
        self.remaining = set(expected_ids)

    def on_message(self, host: Any, message_type: str, message: dict[str, Any]) -> Optional[str]:
        if message_type != protocol.PART_DELETED:
            return None
        removed_id = str(message.get("id", ""))
        self.remaining.discard(removed_id)
        return "confirm" if not self.remaining else None


class _RecreateSubtreeTracker:
    """Drives DeleteObjectCommand.send_inverse(): (re)creates the
    snapshotted subtree ONE object at a time, parent before child,
    remapping old_id -> newly-assigned live id as each PART_CREATED
    confirms, so the next child's parent_id request is always a real,
    already-live parent. Only reports "confirm" to CommandManager once
    every item has been recreated."""

    def __init__(self, command: "DeleteObjectCommand", items: list[dict[str, Any]]) -> None:
        self.command = command
        self.items = items
        self.index = 0
        self.awaiting_class: str | None = None

    def start(self, host: Any) -> None:
        self.command.id_map = {}
        self._send_next(host)

    def _send_next(self, host: Any) -> None:
        if self.index >= len(self.items):
            return
        item = self.items[self.index]
        old_parent = item["parent_id"]
        live_parent = self.command.id_map.get(old_parent, old_parent) if old_parent else None
        self.awaiting_class = item["class_name"]
        host.request_create_instance(
            item["class_name"],
            properties=dict(item["properties"]),
            parent_id=live_parent,
            name=item["name"],
        )

    def on_message(self, host: Any, message_type: str, message: dict[str, Any]) -> Optional[str]:
        if message_type != protocol.PART_CREATED:
            return None
        part = message.get("part")
        if not isinstance(part, dict) or part.get("class_name") != self.awaiting_class:
            return None
        item = self.items[self.index]
        live_id = str(part.get("id", ""))
        self.command.id_map[item["old_id"]] = live_id
        _debug(f"recreate subtree: id remap {item['old_id']} -> {live_id} ({item['class_name']})")
        self.index += 1
        if self.index >= len(self.items):
            return "confirm"
        self._send_next(host)
        return None


class _ModelTransformTracker:
    """Reuses TRANSFORM_MODEL's existing sequence-gated confirmation
    (see Stage 2.2 report) -- confirms on MODEL_TRANSFORMED with the
    matching sequence, rejects on TRANSFORM_MODEL_REJECTED with the
    matching sequence."""

    def __init__(self, model_id: str, sequence: int) -> None:
        self.model_id = model_id
        self.sequence = sequence

    def on_message(self, host: Any, message_type: str, message: dict[str, Any]) -> Optional[str]:
        if str(message.get("id", "")) != self.model_id or message.get("sequence") != self.sequence:
            return None
        if message_type == protocol.MODEL_TRANSFORMED:
            return "confirm"
        if message_type == protocol.TRANSFORM_MODEL_REJECTED:
            return "reject"
        return None


class _NullTracker:
    """For an inverse/forward direction that has nothing to send (e.g.
    undoing a Create whose live id was somehow never captured) -- resolves
    immediately so the pending slot never gets stuck."""

    def on_message(self, host: Any, message_type: str, message: dict[str, Any]) -> Optional[str]:
        return None

    resolved = True


# ============================================================
# COMMANDS
# ============================================================

class _PropertyDeltaCommand:
    """Shared shape for any command fully expressed as an UPDATE_PROPERTY
    before/after delta on ONE instance -- see module docstring for why
    PropertyEditCommand / PartTransformCommand / ResizeCommand are kept as
    distinct (but structurally identical) classes rather than one type."""

    def __init__(
        self,
        instance_id: str,
        label: str,
        before_properties: dict[str, Any] | None = None,
        after_properties: dict[str, Any] | None = None,
        before_name: str | None = None,
        after_name: str | None = None,
        before_enabled: bool | None = None,
        after_enabled: bool | None = None,
    ) -> None:
        self.instance_id = instance_id
        self.label = label
        self.before_properties = before_properties
        self.after_properties = after_properties
        self.before_name = before_name
        self.after_name = after_name
        self.before_enabled = before_enabled
        self.after_enabled = after_enabled

    def describe(self) -> str:
        return self.label

    def is_noop(self, tolerance: float = 1e-6) -> bool:
        """True when after == before for every captured field, within a
        documented float tolerance -- see Stage 2.5 spec §6: 'Do not push
        a command when the final state equals the initial state.'"""
        if self.before_name != self.after_name:
            return False
        if self.before_enabled != self.after_enabled:
            return False
        before = self.before_properties or {}
        after = self.after_properties or {}
        if set(before.keys()) != set(after.keys()):
            return False
        for key, before_value in before.items():
            after_value = after.get(key)
            if isinstance(before_value, list) and isinstance(after_value, list):
                if len(before_value) != len(after_value):
                    return False
                for b, a in zip(before_value, after_value):
                    if isinstance(b, (int, float)) and isinstance(a, (int, float)):
                        if abs(float(b) - float(a)) > tolerance:
                            return False
                    elif b != a:
                        return False
            elif before_value != after_value:
                return False
        return True

    def send_forward(self, host: Any) -> Any:
        host.apply_property_edit(
            self.instance_id, properties=self.after_properties,
            name=self.after_name, enabled=self.after_enabled,
        )
        return _SingleIdTracker(self.instance_id, protocol.PART_UPDATED)

    def send_inverse(self, host: Any) -> Any:
        host.apply_property_edit(
            self.instance_id, properties=self.before_properties,
            name=self.before_name, enabled=self.before_enabled,
        )
        return _SingleIdTracker(self.instance_id, protocol.PART_UPDATED)


class PropertyEditCommand(_PropertyDeltaCommand):
    """Inspector property edits, rename, Anchored/CanCollide and similar
    boolean toggles -- anything that goes through
    MultiplayerStudioAdapter.set_property()."""


class PartTransformCommand(_PropertyDeltaCommand):
    """One complete gizmo Move or Rotate drag on a single Part."""


class ResizeCommand(_PropertyDeltaCommand):
    """One complete gizmo Scale (axis or uniform) drag on a single Part."""


class ReparentCommand:
    """One accepted Explorer drag-and-drop reparent."""

    def __init__(self, instance_id: str, label: str, before_parent_id: str | None, after_parent_id: str) -> None:
        self.instance_id = instance_id
        self.label = label
        self.before_parent_id = before_parent_id
        self.after_parent_id = after_parent_id

    def describe(self) -> str:
        return self.label

    def send_forward(self, host: Any) -> Any:
        host.request_set_parent(self.instance_id, self.after_parent_id)
        return _ReparentTracker(self.instance_id, self.after_parent_id)

    def send_inverse(self, host: Any) -> Any:
        target = self.before_parent_id or "Workspace"
        host.request_set_parent(self.instance_id, target)
        return _ReparentTracker(self.instance_id, target)


class CreateObjectCommand:
    """One confirmed Insert Object / Duplicate creation. Also used, via
    DeleteObjectCommand's inverse, as the building block for subtree
    restore -- see _RecreateSubtreeTracker."""

    def __init__(self, label: str, class_name: str, properties: dict[str, Any] | None, parent_id: str | None, name: str) -> None:
        self.label = label
        self.class_name = class_name
        # None (as opposed to {}) means "let request_create_instance apply
        # its own default/spawn-position auto-fill" -- only true for the
        # very first forward send of a fresh create_part(); _CreateTracker
        # overwrites this with the server's canonical properties on
        # confirm, so every subsequent send_forward (Redo) is concrete.
        self.properties = properties
        self.parent_id = parent_id
        self.name = name
        self.live_instance_id: str | None = None

    def describe(self) -> str:
        return self.label

    def send_forward(self, host: Any) -> Any:
        host.request_create_instance(
            self.class_name,
            properties=dict(self.properties) if self.properties is not None else None,
            parent_id=self.parent_id,
            name=self.name,
        )
        return _CreateTracker(self)

    def send_inverse(self, host: Any) -> Any:
        if self.live_instance_id is None:
            return _NullTracker()
        host.delete_instance(self.live_instance_id)
        return _SingleIdTracker(self.live_instance_id, protocol.PART_DELETED)


class DeleteObjectCommand:
    """One confirmed delete of a single object OR a recursive Model/
    Folder subtree. `snapshot` is parent-first: snapshot[0] is the
    directly-deleted root; every item's parent_id is either another
    item's old_id (a descendant) or the root's ORIGINAL external parent
    (for the root itself)."""

    def __init__(self, label: str, snapshot: list[dict[str, Any]]) -> None:
        self.label = label
        self.snapshot = snapshot
        self.id_map: dict[str, str] = {}

    def describe(self) -> str:
        return self.label

    def root_id(self) -> str:
        root_old = self.snapshot[0]["old_id"]
        return self.id_map.get(root_old, root_old)

    def send_forward(self, host: Any) -> Any:
        host.delete_instance(self.root_id())
        expected = {self.id_map.get(item["old_id"], item["old_id"]) for item in self.snapshot}
        return _DeleteTracker(expected)

    def send_inverse(self, host: Any) -> Any:
        tracker = _RecreateSubtreeTracker(self, list(self.snapshot))
        tracker.start(host)
        return tracker


class ModelTransformCommand:
    """One complete Model Move, Rotate, or uniform-Scale gizmo drag (or
    the equivalent one-shot Inspector pivot edit) -- atomic pivot +
    descendant snapshot, sent through the existing TRANSFORM_MODEL
    protocol exactly like a normal drag."""

    def __init__(
        self,
        model_id: str,
        label: str,
        before_pivot: dict[str, list[float]],
        after_pivot: dict[str, list[float]],
        before_descendants: dict[str, dict[str, list[float]]],
        after_descendants: dict[str, dict[str, list[float]]],
    ) -> None:
        self.model_id = model_id
        self.label = label
        self.before_pivot = before_pivot
        self.after_pivot = after_pivot
        self.before_descendants = before_descendants
        self.after_descendants = after_descendants
        self._sequence = 0

    def describe(self) -> str:
        return self.label

    def is_noop(self, tolerance: float = 1e-6) -> bool:
        def vec_close(a, b):
            return all(abs(float(x) - float(y)) <= tolerance for x, y in zip(a, b))

        if set(self.before_pivot.keys()) != set(self.after_pivot.keys()):
            return False
        for key, before_value in self.before_pivot.items():
            after_value = self.after_pivot.get(key)
            if isinstance(before_value, list):
                if not vec_close(before_value, after_value):
                    return False
            elif before_value != after_value:
                return False
        if set(self.before_descendants.keys()) != set(self.after_descendants.keys()):
            return False
        for instance_id, before_entry in self.before_descendants.items():
            after_entry = self.after_descendants.get(instance_id, {})
            for key, before_value in before_entry.items():
                after_value = after_entry.get(key)
                if after_value is None or not vec_close(before_value, after_value):
                    return False
        return True

    def send_forward(self, host: Any) -> Any:
        self._sequence = host.next_history_transform_sequence()
        host.request_transform_model(
            self.model_id, self.after_pivot["Position"], self.after_pivot["Rotation"],
            self.after_descendants, self._sequence,
        )
        return _ModelTransformTracker(self.model_id, self._sequence)

    def send_inverse(self, host: Any) -> Any:
        self._sequence = host.next_history_transform_sequence()
        host.request_transform_model(
            self.model_id, self.before_pivot["Position"], self.before_pivot["Rotation"],
            self.before_descendants, self._sequence,
        )
        return _ModelTransformTracker(self.model_id, self._sequence)


# ============================================================
# COMMAND MANAGER
# ============================================================

class _PendingEntry:
    __slots__ = ("command", "kind", "tracker", "since")

    def __init__(self, command: Any, kind: str, tracker: Any) -> None:
        self.command = command
        self.kind = kind  # "new" | "undo" | "redo"
        self.tracker = tracker
        self.since = time.monotonic()


class CommandManager:
    """See module docstring for the full design. `host` is the
    MultiplayerGame instance -- CommandManager only ever calls its
    existing public request methods, never touches Qt or Ursina
    directly."""

    def __init__(self, host: Any) -> None:
        self._host = host
        self._undo_stack: list[Any] = []
        self._redo_stack: list[Any] = []
        self._new_pending: list[_PendingEntry] = []
        self._undo_redo_pending: _PendingEntry | None = None
        self._state_listeners: list[Callable[[], None]] = []

    # ---------------- state / UI sync ----------------

    def add_state_listener(self, callback: Callable[[], None]) -> None:
        self._state_listeners.append(callback)

    def _notify(self) -> None:
        for callback in self._state_listeners:
            callback()

    def refresh_ui(self) -> None:
        """Public trigger for listeners to re-read can_undo/can_redo/
        undo_text/redo_text without any stack change -- used by
        set_studio_playing() so Edit-menu enabled state updates the
        instant Play starts/stops, not just on the next history event."""
        self._notify()

    @property
    def can_undo(self) -> bool:
        if getattr(self._host, "studio_playing", False):
            return False
        return bool(self._undo_stack) and self._undo_redo_pending is None

    @property
    def can_redo(self) -> bool:
        if getattr(self._host, "studio_playing", False):
            return False
        return bool(self._redo_stack) and self._undo_redo_pending is None

    @property
    def undo_text(self) -> str:
        return self._undo_stack[-1].describe() if self._undo_stack else ""

    @property
    def redo_text(self) -> str:
        return self._redo_stack[-1].describe() if self._redo_stack else ""

    def clear(self) -> None:
        """Scene load / New Scene -- see Stage 2.5 spec §15: a different
        authoritative world snapshot invalidates every stored id/parent/
        property reference a command might hold."""
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._new_pending.clear()
        self._undo_redo_pending = None
        _debug("history cleared (scene load / new scene)")
        self._notify()

    # ---------------- performing a brand-new local action ----------------

    def perform(self, command: Any, *, optimistic: bool) -> bool:
        """`optimistic=True` (property-shaped commands): push immediately,
        no reject path exists server-side. `optimistic=False` (create/
        delete/reparent/model-transform): hold in `_new_pending` until a
        matching confirmation/rejection arrives (see on_network_message).
        Returns False without sending anything if not connected (mirrors
        the existing accepted-bool contract every request_* method on
        MultiplayerGame already has) or if the command is a no-op."""
        network = getattr(self._host, "network", None)
        if network is not None and not network.connected_event.is_set():
            return False
        if isinstance(command, _PropertyDeltaCommand) and command.is_noop():
            _debug(f"skipped no-op command: {command.describe()}")
            return True
        if isinstance(command, ModelTransformCommand) and command.is_noop():
            _debug(f"skipped no-op command: {command.describe()}")
            return True

        tracker = command.send_forward(self._host)
        _debug(f"perform (optimistic={optimistic}): {command.describe()}")
        if optimistic:
            self._push_undo(command)
            return True
        self._new_pending.append(_PendingEntry(command, "new", tracker))
        return True

    def push_optimistic(self, command: Any) -> None:
        """For commands whose forward action was ALREADY sent by other,
        pre-existing code this stage deliberately does not restructure --
        gizmo Move/Rotate/Scale go through _apply_gizmo_result /
        _apply_model_gizmo_result / _apply_model_scale_result (Stage 2.2/
        2.3), which already do their own send + echo-suppression + (for
        Models) reject-triggered resync. This only records history; it
        never sends anything itself, so there is no risk of a duplicate
        network request."""
        if isinstance(command, _PropertyDeltaCommand) and command.is_noop():
            _debug(f"skipped no-op command: {command.describe()}")
            return
        if isinstance(command, ModelTransformCommand) and command.is_noop():
            _debug(f"skipped no-op command: {command.describe()}")
            return
        _debug(f"push_optimistic: {command.describe()}")
        self._push_undo(command)

    def _cap_undo_stack(self) -> None:
        if len(self._undo_stack) > MAX_HISTORY_DEPTH:
            self._undo_stack.pop(0)

    def _push_undo(self, command: Any) -> None:
        self._undo_stack.append(command)
        self._cap_undo_stack()
        if self._redo_stack:
            _debug(f"redo stack cleared ({len(self._redo_stack)} entries) by new action: {command.describe()}")
        self._redo_stack.clear()
        _debug(f"push to undo stack: {command.describe()} (undo={len(self._undo_stack)}, redo={len(self._redo_stack)})")
        self._notify()

    # ---------------- undo / redo ----------------

    def undo(self) -> None:
        if not self.can_undo:
            return
        command = self._undo_stack.pop()
        if isinstance(command, _PropertyDeltaCommand):
            # UPDATE_PROPERTY's PART_UPDATED echo carries no per-request
            # correlation id, so a pending-confirmation slot here could
            # easily resolve against an unrelated in-flight PART_UPDATED
            # for the same instance (e.g. the original edit's own
            # not-yet-arrived echo) rather than this specific inverse --
            # discovered via a headless test that fired an Undo
            # immediately after an unconfirmed optimistic edit. Since
            # there is no reject path for these anyway (see perform()'s
            # optimistic push for the same reasoning), transfer
            # immediately instead of waiting.
            command.send_inverse(self._host)
            self._redo_stack.append(command)
            _debug(f"undo (optimistic): {command.describe()} (undo={len(self._undo_stack)}, redo={len(self._redo_stack)})")
            self._notify()
            return
        tracker = command.send_inverse(self._host)
        _debug(f"undo requested: {command.describe()}")
        self._undo_redo_pending = _PendingEntry(command, "undo", tracker)
        self._notify()

    def redo(self) -> None:
        if not self.can_redo:
            return
        command = self._redo_stack.pop()
        if isinstance(command, _PropertyDeltaCommand):
            command.send_forward(self._host)
            self._undo_stack.append(command)
            self._cap_undo_stack()
            _debug(f"redo (optimistic): {command.describe()} (undo={len(self._undo_stack)}, redo={len(self._redo_stack)})")
            self._notify()
            return
        tracker = command.send_forward(self._host)
        _debug(f"redo requested: {command.describe()}")
        self._undo_redo_pending = _PendingEntry(command, "redo", tracker)
        self._notify()

    # ---------------- network message dispatch ----------------

    def on_network_message(self, message_type: str, message: dict[str, Any]) -> None:
        self._check_timeout()

        if self._undo_redo_pending is not None:
            result = self._undo_redo_pending.tracker.on_message(self._host, message_type, message)
            if result == "confirm":
                self._resolve_undo_redo(success=True)
            elif result == "reject":
                self._resolve_undo_redo(success=False, reason=message.get("reason"))

        remaining: list[_PendingEntry] = []
        for entry in self._new_pending:
            result = entry.tracker.on_message(self._host, message_type, message)
            if result == "confirm":
                _debug(f"new action confirmed: {entry.command.describe()}")
                self._push_undo(entry.command)
            elif result == "reject":
                _debug(f"new action rejected: {entry.command.describe()} ({message.get('reason')})")
                if self._host.studio_adapter is not None:
                    self._host.studio_adapter.log(
                        "warning", f"Action rejected: {entry.command.describe()} ({message.get('reason', 'unknown reason')})",
                    )
            else:
                remaining.append(entry)
        self._new_pending = remaining

    def _resolve_undo_redo(self, success: bool, reason: Any = None) -> None:
        entry = self._undo_redo_pending
        if entry is None:
            return
        self._undo_redo_pending = None
        if success:
            if entry.kind == "undo":
                self._redo_stack.append(entry.command)
                _debug(f"undo confirmed: {entry.command.describe()}")
            else:
                self._undo_stack.append(entry.command)
                _debug(f"redo confirmed: {entry.command.describe()}")
        else:
            # Rejected or timed out -- put it back where it came from so
            # history stays logically consistent (see Stage 2.5 spec §3:
            # "do not silently discard the command").
            if entry.kind == "undo":
                self._undo_stack.append(entry.command)
            else:
                self._redo_stack.append(entry.command)
            if self._host.studio_adapter is not None:
                verb = "Undo" if entry.kind == "undo" else "Redo"
                self._host.studio_adapter.log(
                    "warning", f"{verb} rejected: {entry.command.describe()} ({reason or 'server rejected the request'})",
                )
        self._notify()

    def _check_timeout(self) -> None:
        if self._undo_redo_pending is None:
            return
        if time.monotonic() - self._undo_redo_pending.since < PENDING_TIMEOUT:
            return
        _debug("undo/redo confirmation timed out; resyncing")
        self._resolve_undo_redo(success=False, reason="confirmation timed out")
        if self._host.studio_adapter is not None:
            self._host.studio_adapter.sync_full_scene()
