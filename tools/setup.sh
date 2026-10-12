#!/usr/bin/env bash
# Installs everything the converter needs, without make.
#
# Does what `make setup` does: creates the Python environment and installs the
# dependencies, builds cadmpeg if it is missing, then checks that every tool is
# usable. This project runs on Windows only, so this is for Git Bash or MSYS2.
# Everything is installed under %LOCALAPPDATA%\f3z-to-solidcraft, because the
# project sits on a network share and a Python environment there cannot load
# numpy's DLLs.
#
# Usage:
#   tools/setup.sh              install whatever is missing, then check
#   tools/setup.sh --recreate   delete the Python environment and build it again
#   tools/setup.sh --step=NAME  run one step only: venv, cadmpeg or check

set -Eeuo pipefail

if [ -z "${LOCALAPPDATA:-}" ]; then
    echo "LOCALAPPDATA is not set; run this from Git Bash on Windows." >&2
    exit 1
fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$(cygpath -u "$LOCALAPPDATA")/f3z-to-solidcraft"
VENV_DIR="$APP_DIR/venv"
VENV_PYTHON="$VENV_DIR/Scripts/python.exe"
CADMPEG_DIR="$APP_DIR/cadmpeg"
CADMPEG="$CADMPEG_DIR/bin/cadmpeg.exe"
CADMPEG_VERSION="0.5.5"
LOG_FILE="$APP_DIR/logs/setup.log"

# ezf3d needs Python 3.13; uv fetches it if it is not installed.
PYTHON_VERSION="3.13"
# The default Rust toolchain here is MSVC, which has no linker installed; the
# GNU toolchain builds cadmpeg with the mingw gcc that is.
RUST_TOOLCHAIN="stable-x86_64-pc-windows-gnu"

RECREATE=0
STEP="all"

# Writes one line to the log file and to the console.
# Args:
# - $1: the level, INFO, DEBUG or ERROR
# - $2: the text of the line
log() {
    local line
    line="[$1] $(date '+%Y-%m-%d %H:%M:%S') [setup]: $2"
    echo "$line" >> "$LOG_FILE"
    echo "$line"
}

# Logs the line a command failed on; installed as the ERR trap.
# Args:
# - $1: the exit code of the failed command
# - $2: the line number it failed on
on_error() {
    log ERROR "Setup failed: exit code $1 at line $2"
}

# Reads the command line options into RECREATE and STEP.
# Args:
# - $@: the options given to the script
parse_args() {
    local option
    local usage="Usage: tools/setup.sh [--recreate] [--step=all|venv|cadmpeg|check]"
    for option in "$@"; do
        case "$option" in
            --recreate) RECREATE=1 ;;
            --step=*) STEP="${option#--step=}" ;;
            *)
                echo "Unknown option: $option" >&2
                echo "$usage" >&2
                exit 2
                ;;
        esac
    done
    case "$STEP" in
        all|venv|cadmpeg|check) ;;
        *)
            echo "Unknown step: $STEP" >&2
            echo "$usage" >&2
            exit 2
            ;;
    esac
    if [ "$RECREATE" -eq 1 ] && [ "$STEP" != all ] && [ "$STEP" != venv ]; then
        echo "--recreate only applies to the venv step, not $STEP" >&2
        exit 2
    fi
}

# Creates the folder the log file is written to.
init_log() {
    mkdir -p "$(dirname "$LOG_FILE")"
}

# Stops the script if a program is not on PATH.
# Args:
# - $1: the program to look for
# - $2: where to get the program, shown when it is missing
require_command() {
    if ! command -v "$1" > /dev/null 2>&1; then
        log ERROR "$1 is not on PATH. $2"
        exit 1
    fi
}

# Deletes the Python environment if there is one.
remove_venv() {
    if [ -d "$VENV_DIR" ]; then
        log INFO "Removing the Python environment at $VENV_DIR"
        rm -rf "$VENV_DIR"
    fi
}

# Creates the Python environment if it is missing and installs the dependencies.
install_venv() {
    require_command uv "Install it from https://docs.astral.sh/uv/"
    if [ -f "$VENV_PYTHON" ]; then
        log INFO "Python environment already exists at $VENV_DIR"
    else
        log INFO "Creating the Python environment at $VENV_DIR"
        uv venv --python "$PYTHON_VERSION" "$(cygpath -w "$VENV_DIR")"
    fi
    log INFO "Installing the Python dependencies"
    uv pip install --python "$(cygpath -w "$VENV_DIR")" -r "$(cygpath -w "$PROJECT_DIR/requirements.txt")"
}

# Builds and installs cadmpeg from crates.io if it is missing (about 10 minutes).
install_cadmpeg() {
    if [ -f "$CADMPEG" ]; then
        log INFO "cadmpeg already installed at $CADMPEG"
        return
    fi
    require_command cargo "Install Rust from https://rustup.rs/"
    log INFO "Building cadmpeg $CADMPEG_VERSION (about 10 minutes)"
    cargo "+$RUST_TOOLCHAIN" install cadmpeg --version "$CADMPEG_VERSION" --locked --root "$(cygpath -w "$CADMPEG_DIR")"
}

# Checks that Python, ezf3d, cadmpeg and SolidCraft are all usable.
check_setup() {
    log INFO "Checking the installation"
    # load_config reads config.yaml and imports utils, both relative to the project folder.
    cd "$PROJECT_DIR"
    "$VENV_PYTHON" --version
    "$VENV_PYTHON" -c "import ezf3d, numpy, rich, yaml; print('ezf3d, numpy, rich, yaml: ok')"
    "$VENV_PYTHON" -c "from pathlib import Path; from utils.solvecraft_session import load_config; c = load_config(Path('config.yaml')); print('SolidCraft:', c.cli); print('cadmpeg:   ', c.cadmpeg)"
    "$CADMPEG" --version
}

# Runs the chosen setup steps in order.
# Args:
# - $@: the options given to the script
main() {
    parse_args "$@"
    init_log
    trap 'on_error $? $LINENO' ERR
    log INFO "Setup started for $PROJECT_DIR (step: $STEP)"
    if [ "$RECREATE" -eq 1 ]; then
        remove_venv
    fi
    case "$STEP" in all|venv) install_venv ;; esac
    case "$STEP" in all|cadmpeg) install_cadmpeg ;; esac
    case "$STEP" in all|check) check_setup ;; esac
    log INFO "Setup finished"
}

export PYTHONUTF8=1
main "$@"
