<div align="center">

# SStudio

### An open-source game editor, runtime, and experimentation platform.

SStudio is an independent game-development environment built around a visual 3D editor, a structured DataModel, sandboxed Lua scripting, and a server-authoritative runtime.

Inspired by the workflow of editors such as Roblox Studio, but built independently with its own architecture, features, and direction.

![Status](https://img.shields.io/badge/status-early%20development-orange)
![Platform](https://img.shields.io/badge/platform-Windows-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB)

</div>

---

## About

SStudio started as an experiment in building a Roblox Studio-like editor from scratch.

It has since grown into a larger project exploring how a complete game-creation environment can be built with an independent editor, runtime, object model, scripting API, networking layer, physics system, and file format.

The goal is not to reproduce Roblox Studio feature-for-feature.

SStudio is intended to become its own open-source environment where the editor and runtime are transparent, modifiable, and controlled by the community building them.

> [!WARNING]
> SStudio is in active early development. APIs, project files, scripting behavior, UI, and internal architecture may change between commits. It is not yet intended for production game development.

---

## Current Features

### Editor

* Integrated 3D viewport powered by Ursina / Panda3D
* PySide6 desktop interface
* Explorer-style object hierarchy
* Inspector / Properties panel
* Move, Rotate, and Scale transform gizmos
* Grid snapping
* Object duplication and deletion
* Hierarchical Models and Folders
* Drag-and-drop hierarchy editing
* Undo / redo infrastructure
* Play and Stop workflow
* Output and diagnostic interfaces
* Integrated Lua source editor
* Place creation, opening, and saving
* Built-in project templates

### DataModel

SStudio uses a structured object model rather than treating the scene as an untyped collection of entities.

The current DataModel includes:

* `Instance`
* `Workspace`
* `StarterPlayer`
* `Players`
* `StarterGui`
* `ReplicatedStorage`
* `ServerScriptService`
* `ServerStorage`
* `Part`
* `Model`
* `Folder`
* `Script`
* `LocalScript`
* `ModuleScript`
* and other editor/runtime object types

The metadata registry supports:

* class inheritance
* typed properties
* property categories
* defaults
* validation
* read-only properties
* service classes
* singleton rules
* parent restrictions
* serialization rules

This allows editor UI, persistence, networking, and scripting to share the same underlying model instead of implementing object behavior separately in each subsystem.

### Lua Scripting

SStudio includes an embedded Lua 5.4 runtime through [Lupa](https://github.com/scoder/lupa).

Scripts run in isolated environments with a controlled API instead of receiving unrestricted access to Python internals.

Implemented scripting foundations include:

* `Script`, `LocalScript`, and `ModuleScript`
* `Instance` proxies
* object property access
* hierarchy access
* `Vector3`
* `Color3`
* signals and connections
* `Connect`
* `Once`
* `Wait`
* task scheduling
* `Players`
* local `Player`
* character API
* `UserInputService`
* runtime diagnostics
* script error locations
* protected Lua metatables

The runtime also includes resource protection such as:

* instruction limits
* per-session memory limits
* script environment isolation
* controlled Python ↔ Lua bridges
* limits on runtime-created instances and scheduled tasks

Gameplay changes made during Play mode operate on runtime state and are discarded when the session stops instead of permanently modifying the editor scene.

### Physics & Character Runtime

SStudio currently contains foundations for:

* physics simulation
* collisions
* anchored objects
* player character movement
* jumping
* first-person and third-person camera behavior
* runtime character representation
* configurable Workspace gravity
* configurable StarterPlayer character properties

### Networking

The editor/runtime uses a WebSocket-based client/server architecture.

The server acts as the authoritative source of truth for shared world state. Scene changes are validated before being applied and synchronized to connected clients.

Current networking foundations include:

* shared world snapshots
* object creation and deletion
* property synchronization
* hierarchy synchronization
* transform synchronization
* service-property synchronization
* player state
* validation of client mutations

---

## Architecture

SStudio is currently primarily written in Python, with Lua used for game scripting.

| Component                 | Purpose                                                      |
| ------------------------- | ------------------------------------------------------------ |
| `studio_editor_live.py`   | Main Qt editor UI                                            |
| `client_studio.py`        | Editor/runtime integration, rendering, networking, Play mode |
| `server.py`               | Authoritative WebSocket server                               |
| `datamodel_schema.py`     | Class and property metadata registry                         |
| `lua_runtime.py`          | Sandboxed Lua runtime and core scripting API                 |
| `lua_gameplay_api.py`     | Player, Character, Signal, and input APIs                    |
| `transform_gizmo.py`      | Move / Rotate / Scale viewport tools                         |
| `editor_history.py`       | Undo / redo command history                                  |
| `physics.py`              | Physics integration                                          |
| `character_controller.py` | Runtime character movement                                   |
| `character_rig.py`        | Character visual representation                              |
| `place_manager.py`        | Place serialization and loading                              |
| `sstudio_templates.py`    | Template browser and project templates                       |
| `shared/`                 | Shared protocol, Instance model, and object registry         |

The project deliberately separates the editor, runtime, server-authoritative state, and scripting environment so they can evolve without turning the editor UI into the engine itself.

---

## Getting Started

### Requirements

SStudio currently targets **Windows**.

You will need:

* Python 3.10 or newer
* Git
* a GPU/driver capable of running Panda3D

### Clone

```bash
git clone https://github.com/shrssrhs/SStudio.git
cd SStudio
```

### Create a virtual environment

```powershell
python -m venv .venv
.venv\Scripts\activate
```

### Install dependencies

```powershell
python -m pip install --upgrade pip
pip install -r requirements_studio.txt
```

---

## Running SStudio

SStudio currently uses a separate authoritative server.

Start the server first:

```powershell
python server.py
```

By default it listens on:

```text
ws://127.0.0.1:8765
```

Then open another terminal and start the Studio client:

```powershell
python client_studio.py
```

The client connects to the local server automatically.

### Client options

A different server can be specified manually:

```powershell
python client_studio.py --server ws://127.0.0.1:8765
```

Set the local player name:

```powershell
python client_studio.py --name Developer
```

Open a Place immediately:

```powershell
python client_studio.py --place path\to\place.nebula.json
```

Several developer/debug options are also available:

```powershell
python client_studio.py --help
```

---

## Testing

The repository contains regression tests for the editor, runtime, DataModel, Lua APIs, physics, transforms, places, character systems, and other core components.

Most tests are standalone Python test scripts and can be run directly:

```powershell
python test_datamodel_schema.py
python test_lua_gameplay_api.py
python test_transform_history.py
```

To run every `test_*.py` script from PowerShell:

```powershell
Get-ChildItem test_*.py | ForEach-Object {
    python $_.FullName
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
```

---

## Project Direction

SStudio is still building its foundations.

Current development is focused on turning the existing editor/runtime prototype into a coherent game-development platform rather than rapidly adding isolated features.

Areas expected to grow over time include:

* broader DataModel coverage
* richer Lua gameplay APIs
* more engine services
* improved physics and character systems
* better asset and model workflows
* editor plugins and extensibility
* multiplayer development workflows
* scripting tools and debugging
* project packaging and distribution
* performance and stability
* cross-platform support where the architecture allows it

This is a direction, not a compatibility promise or fixed release schedule.

---

## Contributing

Contributions, bug reports, architecture discussions, and experiments are welcome.

For larger changes, opening an issue first is recommended so the implementation can fit the existing editor/runtime architecture instead of creating a second parallel system.

When changing existing behavior, add or update regression tests where practical.

The project is young enough that substantial architectural improvements are still possible — but changes should preserve clear boundaries between the editor, authoritative state, runtime, and scripting environment.

---

## Independence

SStudio is an independent open-source project.

It is not affiliated with, sponsored by, or endorsed by Roblox Corporation.

References to Roblox or Roblox Studio describe inspiration, familiar concepts, or workflow comparisons only. SStudio does not aim to be an official Roblox client, an official Roblox development tool, or a drop-in compatible implementation.

---

## License

SStudio is released under the [MIT License](LICENSE).

You are free to use, modify, distribute, and build on the project under the terms of that license.

---

<div align="center">

Built in the open.

**SStudio**

</div>
