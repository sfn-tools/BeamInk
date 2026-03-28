# BeamInk

BeamInk is a Python application that turns a `Wacom Intuos S` drawing tablet into a live drawing controller for an `xTool F1` laser engraver.

![BeamInk](img/BeamInk.gif)

BeamInk runs on Linux, captures pen input through `evdev`, maps tablet position and pressure into stage movement and laser power, live streams G-code to the F1 over TCP, and shows the live session in a desktop GUI. The same package also includes a simulator transport so you can work on the UI and mapping logic without touching the real machine.

If you just cloned the repo and want to use it, this file is the fastest guide to what the code does, how to start it, and where to look when something goes wrong.

## What BeamInk Does

At a high level, BeamInk converts stylus input into laser commands:

- pen position becomes X/Y motion on the F1 stage
- pen pressure becomes laser power within configurable limits
- pen up can either mean zero power or low-power preview mode
- pen motion and transport state are mirrored in a Tkinter GUI
- the live transport sends G-code to the F1 on TCP `8780`
- session stop and machine-info operations use the xTool HTTP endpoints

The project currently targets one real-world setup:

- Linux host
- Wacom Intuos S / Intuos BT S class tablet
- xTool F1 connected with USB that shows up as USB Ethernet adapter

## Safety First

BeamInk controls real laser hardware.

- Keep the hardware emergency stop within reach.
- Start with `Pen-up mode` set to `off`.
- Use scrap material for first tests.
- Assume even low power can mark or ignite material.
- Use the simulator before touching the real F1 if you are only checking UI.

BeamInk does include safety helpers:

- live output starts disarmed
- `Stop Session` triggers xTool processing stop
- closing the app also triggers stop processing
- 30 seconds without pen movement triggers automatic stop
- startup and shutdown park the head at the configured focus point


## Naming Convention

The project name is `BeamInk`, but the Python package name stays lowercase as `beamink`.

- docs, UI labels, and project metadata: `BeamInk`
- import paths, module names, and `python3 -m ...`: `beamink`


## Quick Start

Create a virtual environment and install the package:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -e .[dev]
```

If `tkinter` is missing, install your distro package first. On Ubuntu that is usually:

```bash
sudo apt install python3-tk
```


### 1. Discover the tablet

```bash
python3 -m beamink --list-devices
```

### 2. Start in simulator mode

```bash
python3 -m beamink --transport sim
```

This is the safest first run. It lets you confirm:

- the Wacom device is being discovered
- exclusive grab works
- strokes appear in the stage preview
- pressure and power values change as expected

### 3. Check the real F1 over HTTP

```bash
python3 -m beamink --transport xtool --machine-info
```

### 4. Start a real live session

```bash
sudo python3 -m beamink --transport xtool --source BLUE
```

Recommended first-run sequence:

1. Verify the GUI reports exclusive tablet capture.
2. Leave live output disarmed while checking motion in the preview.
3. Set `Pen-up mode` to `off`.
4. Set conservative `Contact floor %` and `Contact ceiling %`.
5. Arm output only when ready to draw on material.
6. Keep `Stop Session` and the hardware emergency stop immediately available.

## Common Runtime Options

- `--transport sim`: use the simulator backend
- `--transport xtool`: use the real xTool F1 backend
- `--source BLUE`: use the visible blue laser
- `--source RED`: use the IR laser source
- `--fullscreen`: start the GUI fullscreen
- `--windowed`: force the GUI to start windowed
- `--no-grab`: do not exclusively grab the tablet device
- `--no-gui`: run the controller without the desktop UI
- `--machine-info`: query the xTool machine-info endpoint and exit
- `--dump-default-config`: print the default JSON config

## How The Package Is Organized

The code under `beamink/` is split by responsibility:

- `beamink/__main__.py`
  Runs the package as `python3 -m beamink`.

- `beamink/app.py`
  Main application bootstrap. Parses CLI flags, loads config, selects the transport, initializes the controller, starts the input reader, and launches the GUI when needed.

- `beamink/config.py`
  Dataclass-based configuration model for tablet bounds, stage size, pressure mapping, transport settings, controller safety values, and UI settings.

- `beamink/core/controller.py`
  The safety-critical runtime owner. It receives pen samples, maintains the latest state, applies mapping and filtering rules, batches commands, and decides what gets sent to the transport.

- `beamink/core/mapping.py`
  Pure mapping helpers for coordinate normalization and pressure-to-power conversion.

- `beamink/core/types.py`
  Shared runtime dataclasses such as pen samples, path points, transport snapshots, and controller snapshots.

- `beamink/input/wacom.py`
  Wacom discovery, related-device grouping, pen state derivation, and exclusive `evdev` capture.

- `beamink/transport/base.py`
  Shared G-code rendering helpers and generic transport abstractions.

- `beamink/transport/sim.py`
  Simulator backend used for offline work, tests, and UI iteration.

- `beamink/transport/xtool.py`
  Real xTool F1 transport. Handles TCP `8780` streaming plus HTTP operations like machine info and processing stop.

- `beamink/gui/main_window.py`
  Tkinter operator UI with preview canvas, status fields, settings, debug log, and stop controls.

## End-To-End Data Flow

The runtime pipeline is intentionally simple:

1. `input/wacom.py` reads raw stylus events from Linux input devices.
2. `core/controller.py` converts those into bounded pen samples and intended machine state.
3. `core/mapping.py` maps normalized tablet input into stage coordinates and power values.
4. `transport/*` converts the resulting motion/power commands into simulator events or xTool G-code.
5. `gui/main_window.py` shows the current state and lets the operator arm output, adjust settings, and stop the session.

Keep the controller in charge of safety decisions, and keep the GUI as a client of the controller rather than a second control path.

## Configuration

The main config areas live in `beamink/config.py`:

- `TabletConfig`: active area, device path, exclusive grab
- `StageConfig`: stage size, focus center, safety margin, fit mode
- `PressureConfig`: hover mode, pressure floor/ceiling, contact power range
- `TransportConfig`: host, ports, laser source, power mode, feed rate, timeouts
- `ControllerConfig`: stale-input limits, idle stop timeout, motion filtering, debug log sizes
- `UIConfig`: poll rate, canvas size, fullscreen, always-on-top, cursor hiding

Dump the current defaults with:

```bash
python3 -m beamink --dump-default-config
```

Then save a JSON file and load it with:

```bash
python3 -m beamink --config path/to/config.json
```

## Troubleshooting

If the tablet does not capture correctly:

- try running with `sudo` or add yourself to the correct group for the tablet
- confirm the device appears in `--list-devices`
- check that the GUI reports exclusive capture
- avoid `--no-grab` unless you intentionally want the tablet shared with the desktop

If the app starts but there is no GUI:

- make sure `tkinter` is installed
- use `python3 -m beamink --help` to confirm the package imports correctly

If the F1 does not respond:

- verify the host IP in the config, default `201.234.3.1`
- try `--machine-info` first before a live session
- Hit the "Stop session" button, this usually returns the F1 to a sane state
- confirm the USB Ethernet link is up on the host

If you are changing beam behavior:

- test in `sim` first
- then use low-risk real-machine checks before trying material marking
- save fresh evidence under `artifacts/`


## Safety-Critical Files

These files deserve extra care whenever they change:

- `beamink/core/controller.py`
- `beamink/input/wacom.py`
- `beamink/transport/base.py`
- `beamink/transport/xtool.py`

