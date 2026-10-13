.PHONY: help run save open gui setup create-venv recreate-venv activate-venv cadmpeg check lint log clean clean-logs

MAIN := main.py

# This project runs on Windows only: it drives solidcraft-cli.exe and cadmpeg.exe.
# Recipes are written for cmd.exe, so make must not pick up an sh.exe on PATH.
# The setup targets run tools\setup.ps1 on Windows and tools/setup.sh elsewhere:
#   $(call SETUP,step)     run one step: all, venv, cadmpeg or check
#   $(call SETUP,step,1)   the same, deleting the Python environment first
ifeq ($(OS),Windows_NT)
SHELL := cmd.exe
.SHELLFLAGS := /c
SETUP = powershell -NoProfile -File tools\setup.ps1 -Step $(1) $(if $(2),-Recreate)
else
SETUP = bash tools/setup.sh --step=$(1) $(if $(2),--recreate)
endif

# Everything installed lives outside the project folder, under the user's
# application data. The project sits on a network share, and a Python
# environment there cannot load numpy's DLLs.
APP_DIR := $(LOCALAPPDATA)\f3z-to-solidcraft
VENV_DIR := $(APP_DIR)\venv
VENV_PYTHON := $(VENV_DIR)\Scripts\python.exe
PYTHON := "$(VENV_PYTHON)"
LOG_FILE := $(APP_DIR)\logs\f3z2solvecraft.log

# The Fusion file to convert, or a folder of them, and any extra options for main.py:
#   make run FILE="C:\path\to\design.f3z"
#   make run FILE="C:\path\to\design.f3z" ARGS="--exact-on-mismatch"
#   make save FOLDER="C:\Archive"
#   make save FOLDER="C:\Archive" RECURSIVE=1 ARGS="--output-dir D:\Converted"
FILE ?=
FOLDER ?=
RECURSIVE ?=
ARGS ?=

# What main.py is given: the file if there is one, else the folder.
SOURCE := $(if $(strip $(FILE)),$(FILE),$(FOLDER))
ifneq ($(strip $(RECURSIVE)),)
ARGS += --recursive
endif

# The report and the progress lines use characters cmd's code page lacks.
export PYTHONUTF8 := 1

help:
	@echo Available targets:
	@echo   make run FILE=...    - Convert a .f3z/.f3d, then ask whether to save or open it
	@echo   make save FILE=...   - Convert and save next to the source without asking
	@echo   make open FILE=...   - Convert and open the result in the SolidCraft UI
	@echo                          Use FOLDER=... instead of FILE=... to convert every .f3z/.f3d in a folder;
	@echo                          add RECURSIVE=1 to include its subfolders
	@echo                          Add ARGS="--exact-on-mismatch" or other main.py options
	@echo   make gui             - Open the window to drop files or folders on; FILE or FOLDER starts it with one
	@echo   make setup           - Install everything: the Python environment, cadmpeg, then check
	@echo   make create-venv     - Create the Python environment if missing and install dependencies
	@echo   make recreate-venv   - Delete and rebuild the Python environment from scratch
	@echo   make activate-venv   - Print the command to activate the Python environment
	@echo   make cadmpeg         - Build and install cadmpeg if missing (about 10 minutes)
	@echo   make check           - Check that Python, ezf3d, cadmpeg and SolidCraft are all usable
	@echo   make lint            - Run flake8 on the project's own code
	@echo   make log             - Show the end of the log file
	@echo   make clean           - Remove __pycache__ and leftover temporary run folders
	@echo   make clean-logs      - Remove the log files

# FILE or FOLDER is required by the three targets that convert.
run save open: require-source

.PHONY: require-source
require-source:
ifeq ($(strip $(SOURCE)),)
	@echo Usage: make $(firstword $(MAKECMDGOALS)) FILE="C:\path\to\design.f3z"
	@echo    or: make $(firstword $(MAKECMDGOALS)) FOLDER="C:\path\to\folder" [RECURSIVE=1]
	@exit 1
endif

run:
	@$(PYTHON) $(MAIN) "$(SOURCE)" $(ARGS)

save:
	@$(PYTHON) $(MAIN) "$(SOURCE)" --save $(ARGS)

open:
	@$(PYTHON) $(MAIN) "$(SOURCE)" --open $(ARGS)

# The window needs no source; one given is listed in it when it opens.
gui:
	@$(PYTHON) $(MAIN) --gui $(if $(strip $(SOURCE)),"$(SOURCE)") $(ARGS)

# The setup script holds the versions (Python, cadmpeg, the Rust toolchain) and
# logs each step to $(APP_DIR)\logs\setup.log.
setup:
	@$(call SETUP,all)

create-venv:
	@$(call SETUP,venv)

recreate-venv:
	@$(call SETUP,venv,1)

activate-venv:
	@echo make cannot activate an environment in your current shell; run one of the following yourself:
	@echo   PowerShell: ^& "$(VENV_DIR)\Scripts\Activate.ps1"
	@echo   cmd.exe:    "$(VENV_DIR)\Scripts\activate.bat"

# cadmpeg decodes the Fusion timeline and writes the original solid as STEP.
# It has no Windows package here, so it is built from crates.io.
cadmpeg:
	@$(call SETUP,cadmpeg)

# config.yaml names the SolidCraft folder and cadmpeg; load_config fails with a
# message saying which is missing.
check:
	@$(call SETUP,check)

# Two passes, as QSnippet's lint does: blocking on real errors, advisory on style.
lint:
	$(PYTHON) -m flake8 $(MAIN) utils ui --select=E9,F63,F7,F82 --show-source
	$(PYTHON) -m flake8 $(MAIN) utils ui --exit-zero --max-line-length 170

log:
	@if exist "$(LOG_FILE)" (powershell -NoProfile -Command "Get-Content -LiteralPath '$(LOG_FILE)' -Tail 40") else (echo No log yet at $(LOG_FILE))

# Each run leaves a temporary folder (the unpacked documents, STEP and mesh
# files); they are safe to remove once the run has finished.
clean:
	@powershell -NoProfile -Command "& { \
		Get-ChildItem -LiteralPath . -Directory -Recurse -Force -Filter '__pycache__' | Where-Object { $$_.FullName -notlike '*\vendor\*' } | ForEach-Object { Remove-Item -LiteralPath $$_.FullName -Recurse -Force }; \
		$$runs = @(Get-ChildItem -LiteralPath $$env:TEMP -Directory -Filter 'f3z2solvecraft-*'); \
		foreach ($$run in $$runs) { Remove-Item -LiteralPath $$run.FullName -Recurse -Force }; \
		Write-Host ('Removed __pycache__ and ' + $$runs.Count + ' temporary run folder(s)') \
	}"

clean-logs:
	@if exist "$(APP_DIR)\logs" (rmdir /s /q "$(APP_DIR)\logs" & echo Removed $(APP_DIR)\logs) else (echo No logs to remove)
