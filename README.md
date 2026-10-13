# f3z-to-solidcraft

Convert Autodesk Fusion `.f3z` and `.f3d` designs into [SolidCraft](https://github.com/bherbruck/solidcraft) designs, keeping the feature history.

The converter replays the Fusion timeline as SolidCraft features. You get the same sketches, extrudes, holes and fillets, in the same order and with the same names. Each part is then measured against its original solid, and a report tells you what matched and what did not.

## Disclaimer

This is a personal project. It has no affiliation with Autodesk, Inc. or Fusion 360, and Autodesk does not endorse it. It is also not affiliated with [SolidCraft](https://github.com/bherbruck/solidcraft), and the SolidCraft developers do not currently endorse it.

## Status

This is an early tool. It has been tested on one `.f3z` file with two parts. Expect gaps on other designs.

What is rebuilt today:

* Parameters
* Sketches (lines, arcs and circles, on their real planes)
* Extrudes (new body, join and cut)
* Fillets
* Holes
* Work planes (as offset planes)
* Linked components

What is not rebuilt yet:

* Sketch constraints and dimensions
* Any other feature type (revolve, sweep, loft, chamfer, shell, patterns and so on). These are listed as skipped in the report.
* The position of a linked component. Linked components are placed at the origin.

Known limits in the test file:

* SolidCraft refuses to fillet some edge combinations that Fusion accepts. The converter rounds the edges SolidCraft allows and reports the rest.
* SolidCraft's "hole at sketch points" mode removes the wrong material. The converter makes one hole feature per position instead.

## How it works

The Fusion file does not store everything needed to replay a feature. The converter uses three sources:

1. [cadmpeg](https://github.com/cadmpeg/cadmpeg) decodes the timeline, the sketches and some full feature definitions.
2. [ezf3d](https://github.com/AlexSabaka/ezf3d) reads the parameters and which parameter drives which feature.
3. The finished solid fills the gaps. When the file does not say which sketch region an extrude used, how deep a cut went, or which edges a fillet rounded, the converter works it out from the original solid.

Commands are sent to SolidCraft one at a time through `solidcraft-cli`. A command that fails is reported and the rest of the timeline carries on. The commands that worked are saved as a script.

Each timeline step ends up with one of four results:

| Result | Meaning |
|---|---|
| `exact` | Rebuilt from what the file records |
| `inferred` | Rebuilt, with some details worked out from the finished solid |
| `FAILED` | Could not be rebuilt |
| `skipped` | Feature type not supported yet |

## Requirements

* Windows
* [SolidCraft](https://github.com/bherbruck/solidcraft) portable build, which provides `solidcraft-cli.exe` and `solidcraft.exe`. Builds older than 0.5 name them `solvecraft-cli.exe` and `solvecraft.exe`, and those work too.
* [uv](https://docs.astral.sh/uv/), which creates the Python 3.13 environment
* GNU Make
* Rust with the `stable-x86_64-pc-windows-gnu` toolchain and a MinGW `gcc`, used to build cadmpeg
* A copy of ezf3d in `vendor/ezf3d`

## Setup

1. Put ezf3d in the `vendor` folder:

   ```
   git clone https://github.com/AlexSabaka/ezf3d vendor/ezf3d
   ```

2. Edit `config.yaml` and set `solidcraft_dir` to the folder that holds `solidcraft-cli.exe`.

3. Install everything else:

   ```
   make setup
   ```

   This creates the Python environment, builds cadmpeg (about 10 minutes the first time) and checks that every tool is found.

   Without make, run `.\tools\setup.ps1` in PowerShell or `tools/setup.sh` in Git Bash instead; both do the same three steps.

The Python environment and cadmpeg are installed under `%LOCALAPPDATA%\f3z-to-solidcraft`, not in the project folder. This keeps the project usable from a network drive.

## Usage

Convert one file. The converter asks at the end whether to save or open the result:

```
make run FILE="C:\path\to\design.f3z"
```

Convert and save without being asked:

```
make save FILE="C:\path\to\design.f3z"
```

Convert and open the result in the SolidCraft UI:

```
make open FILE="C:\path\to\design.f3z"
```

Convert every `.f3z` and `.f3d` file in a folder:

```
make save FOLDER="C:\path\to\archive"
make save FOLDER="C:\path\to\archive" RECURSIVE=1
```

For a folder, the converter asks once at the start what to do with the results. A file that fails is reported and the rest carry on. A summary is printed at the end.

Pass extra options with `ARGS`:

```
make save FILE="C:\path\to\design.f3z" ARGS="--output-dir D:\Converted --force"
```

One file takes about two minutes in the test case.

### Window

Open a window instead of converting at the terminal:

```
make gui
```

Drop `.f3z` or `.f3d` files, or folders of them, on the window. The export options then appear: where to save, and whether to open each design in SolidCraft. The Advanced section holds the rest: subfolders, overwriting, inserting the original solid on a mismatch, and the SolidCraft and cadmpeg paths. Press Convert. The window always saves; a design that already exists is left alone unless Overwrite is ticked.

The fields start from `config.yaml` and from any options given with `--gui`. What you change in the window lasts until it is closed.

### Without make

`main.py` is the only entry point. Run it with the project's Python environment:

```
"%LOCALAPPDATA%\f3z-to-solidcraft\venv\Scripts\python.exe" main.py design.f3z --save
```

Run `main.py --help` for the full list of options.

### Options

| Option | What it does |
|---|---|
| `--save [PATH]` | Save without asking. `PATH` is the `.solidcraft` file and is for a single file only. Give it a `.json` name to save plain JSON instead (SolidCraft 0.6 and newer). |
| `--open` | Open the result in the SolidCraft UI |
| `--force` | Overwrite an existing design |
| `--recursive` | With a folder, include its subfolders |
| `--gui` | Open the window. `SOURCE` is optional and the other options set what the window starts with. |
| `--exact-on-mismatch` | If a part's rebuilt history does not match the original solid, insert the original solid instead. That part then has no feature history. |
| `--config FILE` | Use another configuration file |
| `--solidcraft-dir DIR` | Override `solidcraft_dir` from the configuration |
| `--cadmpeg FILE` | Override `cadmpeg` from the configuration |
| `--output-dir DIR` | Override `output_dir` from the configuration |

## Configuration

`config.yaml` sits beside `main.py`:

```yaml
# Folder holding solidcraft-cli.exe and solidcraft.exe.
solidcraft_dir: 'C:\Tools\solidcraft-0.6.0-windows-x64-portable'

# Path of cadmpeg.exe. "make cadmpeg" installs it here.
cadmpeg: '%LOCALAPPDATA%\f3z-to-solidcraft\cadmpeg\bin\cadmpeg.exe'

# Where saved designs go. Empty means next to the source file.
output_dir: ''
```

Environment variables such as `%LOCALAPPDATA%` are expanded. A command line option always wins over the same setting in the file.

## Output

Saving writes three files side by side:

| File | Contents |
|---|---|
| `NAME.solidcraft` | The SolidCraft design |
| `NAME.script.json` | The commands that build the design. You can replay it with `solidcraft-cli run`. |
| `NAME.report.txt` | What became of every timeline step, and the check against the original |

The report ends with one of these lines:

```
RESULT: every part matches its original
RESULT: NOT every part matches its original
```

A part matches when its volume and bounding box equal those of the original solid.

SolidCraft 0.6 and newer save the design as a zip container. SolidCraft 0.5 and older cannot open it, as a container or as JSON. Older builds still work with the converter: they write plain JSON under the same `.solidcraft` name. Designs saved as `.solvecraft` by earlier versions of the converter still open in every build.

## Logs

Each run is logged to:

```
%LOCALAPPDATA%\f3z-to-solidcraft\logs\f3z2solvecraft.log
```

Show the last lines with `make log`. Remove the log files with `make clean-logs`.

## Make targets

| Target | What it does |
|---|---|
| `make help` | List the targets |
| `make run`, `make save`, `make open` | Convert a `FILE` or a `FOLDER` |
| `make gui` | Open the window |
| `make setup` | Run the setup script (`tools\setup.ps1` on Windows, `tools/setup.sh` elsewhere): the Python environment, cadmpeg, then the check |
| `make create-venv` | Create the Python environment and install dependencies |
| `make recreate-venv` | Delete the Python environment and build it again |
| `make cadmpeg` | Build and install cadmpeg if it is missing |
| `make check` | Check that Python, ezf3d, cadmpeg and SolidCraft are usable |
| `make lint` | Run flake8 on the project code |
| `make log` | Show the end of the log file |
| `make clean` | Remove `__pycache__` folders and leftover temporary run folders |
| `make clean-logs` | Remove the log files |

## Project layout

```
main.py              Entry point
config.yaml          Settings
Makefile             Setup and run targets
requirements.txt     Python dependencies
utils/
  converter.py           Reads the file, rebuilds it, reports, saves or opens
  timeline_builder.py    Turns each Fusion timeline step into SolidCraft commands
  fusion_design.py       Reads a Fusion document through cadmpeg and ezf3d
  solvecraft_session.py  Configuration and the session with solidcraft-cli
  solid_oracle.py        Questions asked of the finished solid
  conversion_common.py   Shared settings, measurements, results and progress display
  conversion_errors.py   The error raised when a conversion cannot go on
  logging_utils.py       Rotating log file handler
ui/
  window.py              The window opened by --gui
  conversion_worker.py   Runs conversions off the window's thread
  theme.py               Dark and light style sheets
  widgets/               Drop zone, collapsible section, path field
vendor/ezf3d/        The ezf3d library
```

## Credits

* [ezf3d](https://github.com/AlexSabaka/ezf3d), MIT license
* [cadmpeg](https://github.com/cadmpeg/cadmpeg), Apache 2.0 license
* [SolidCraft](https://github.com/bherbruck/solidcraft), MIT or Apache 2.0 license
