# Scripting Architecture (plan, not implemented)

This document describes where scripting is *going*, so that the object
model, network protocol and editor built in this stage (`object_registry.py`,
`Instance`, `Script`/`LocalScript`/`ModuleScript`) don't have to be redesigned
when it lands. **Nothing described below runs yet.** `Script.Source` is an
inert string; there is no interpreter, no `eval`/`exec`, no sandbox. Treat
this as an architecture sketch to build towards, not a status report.

## Language split

| Layer                         | Language | Why                                                                 |
|--------------------------------|----------|----------------------------------------------------------------------|
| Game scripting (`Script`, `LocalScript`, `ModuleScript`) | Lua | Sandboxable, fast to embed, the de-facto standard for this kind of in-editor scripting (Roblox, Garry's Mod, World of Warcraft), and safe to expose to untrusted map/game authors. |
| Editor plugins & tools          | Python   | The Studio shell (`studio_editor_live.py`, `client_studio.py`) is already Python/PySide6 — plugins extending the *editor* should share that runtime, not add a second one. |
| Native/performance extensions   | C++ (DLL)| For work that's too slow in Lua or Python: physics, mesh processing, codecs. Loaded as an optional native module, never required to run the editor or a game. |

There is deliberately no fourth, project-specific scripting language — Lua
covers gameplay, Python covers tooling, C++ covers performance-critical
native code, matching Roblox's own split (Luau / native plugins).

## Object model hooks already in place

- `object_registry.py` marks `Script`/`LocalScript`/`ModuleScript` with
  `default_properties={"Source": ..., "RunContext": ...}` — Source is stored
  as a plain string on the `Instance`/`SceneObject`, exactly like `Position`
  is a plain list. The future Lua runtime reads this field; it does not
  change shape when the runtime is added.
- `RunContext` (`"Server"` / `"Client"`) already distinguishes `Script` from
  `LocalScript` the same way Roblox's `RunContext` does, so the future
  scheduler can filter "what runs where" without a schema migration.
- Explorer's double-click / Inspector's "Open Script" button are wired to a
  placeholder dialog today; they are the intended entry point for the real
  code editor later — no new UI plumbing should be needed, just swapping the
  placeholder for a real editor widget.

## Target runtime architecture

```
                 ┌───────────────────────────┐
                 │   Engine Object API        │  Python-side facade over
                 │  (Instance tree, services)  │  Instance/world — the thing
                 └──────────────┬─────────────┘  Lua calls into.
                                │
                     Lua <-> Python binding
                     (e.g. lupa / cffi + LuaJIT,
                      chosen when this is built)
                                │
                 ┌──────────────┴─────────────┐
                 │        Script VM            │
                 │  one Lua state per Script    │
                 │  instance (isolated globals) │
                 └──────────────┬─────────────┘
                                │
                     start() / update(dt) / events
```

### Engine Object API

A thin, explicit Python API surface (not "give Lua the whole Instance
object") that Lua scripts call into — e.g. `GetChildren()`, `FindFirstChild()`,
property get/set going through the *same* validated path server-side updates
already use (`object_registry.sanitize_properties_for_type`), so a script
can't write a property the network protocol wouldn't otherwise accept.

### Script lifecycle

- `start()` — called once when the script's Instance becomes part of a
  running world (Play mode entered, or object created while already
  playing).
- `update(dt)` — called on a fixed/variable tick while the world is running,
  mirroring Ursina's own per-frame `update()` so both stay on one clock.
- Events — a small pub/sub surface (property changed, child added/removed,
  player joined/left) scripts can connect to, instead of polling.
- Teardown — scripts are stopped and their Lua state destroyed when Play
  mode ends or the Instance is destroyed; no state survives Stop.

### Sandbox & resource limits

- Each `Script`/`LocalScript` gets its own Lua state — no shared globals
  between scripts, matching Roblox's per-script environment model.
- No filesystem, socket, or process access from Lua — anything scripts need
  from the outside world goes through the Engine Object API, which can
  enforce permissions per `RunContext` (a `LocalScript` should never be able
  to do something only the server is trusted to do).
- Execution budget (instruction count or wall-clock per tick) so a script
  with an infinite loop can be killed instead of freezing the editor/server.

### Play Mode uses a runtime copy of the scene

Scripts must not be able to corrupt the *editor's* scene state. When Play
starts, the world is snapshotted into a separate runtime copy (server already
has a natural point for this: `world` dict at the moment `Play` is pressed);
scripts run against that copy. Stopping Play discards the runtime copy and
restores the editor's own state — the same "editor edits are never mutated
by gameplay" guarantee `EngineBridge` already gives non-live/demo mode.

### Python plugins

Editor plugins are ordinary Python, loaded into the same process as
`studio_editor_live.py`/`client_studio.py`. The extension points this stage
already prepared:
- `object_registry.register_object_type(...)` — a plugin can add a new
  insertable type without touching `InsertObjectDialog` or Explorer/Inspector
  code, both of which are already driven entirely by the registry.
- The `EngineBridge` signals (`scene_changed`, `object_added`, `selection_changed`,
  `property_changed`, `log_message`, ...) are the intended plugin event feed.
- Plugins get no special network privileges — they call the same
  `EngineBridge`/adapter methods the built-in UI calls, so a plugin can't do
  anything a hand-written script action couldn't already do through the UI.

### C++ extensions

Loaded as native DLLs when a specific hot path needs them (not by default).
The intended boundary is the same Engine Object API used by Lua — a C++
extension should look like "a faster implementation of some Engine Object API
calls", not a second, parallel way to mutate the world.

## What this stage explicitly does NOT build

- No Lua interpreter is embedded.
- No `eval`/`exec` of any kind, anywhere — `Script.Source` is inert data.
- No plugin loader.
- No C++ extension loader.

These are left for later stages; this document exists so their eventual
design has an object model, protocol and UI to land on without a rewrite.
