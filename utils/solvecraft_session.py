from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from .conversion_errors import ConversionError
from .solid_oracle import Mesh

logger = logging.getLogger(__name__)

#: Seconds SolidCraft is given to exit once its input is closed.
CLOSE_TIMEOUT = 30
#: Names of the headless command line, newest first. SolidCraft was called
#: SolveCraft before 0.5; from 0.5 the old name is a shim that prints a
#: deprecation warning, so the new name is used wherever it exists.
CLI_NAMES = ("solidcraft-cli.exe", "solvecraft-cli.exe")
#: Names of the desktop application, newest first.
APP_NAMES = ("solidcraft.exe", "solvecraft.exe")


def first_existing(folder: Path, names: tuple[str, ...]) -> Path:
    """
    Pick the program a folder holds under the newest of its names.

    Args:
    - folder: The folder to look in.
    - names: The program's names, newest first.

    Returns:
    - The path of the first name that exists, or of the newest name when none does.
    """
    return next((folder / name for name in names if (folder / name).is_file()), folder / names[0])


@dataclass
class Config:
    """
    Where the tools the converter drives are, and where its output goes.
    """

    solidcraft_dir: Path
    output_dir: Path | None
    cadmpeg: Path

    @property
    def cli(self) -> Path:
        """
        Locate SolidCraft's headless command line.

        Returns:
        - The path of ``solidcraft-cli.exe``, or of ``solvecraft-cli.exe`` in a build older than 0.5.
        """
        return first_existing(self.solidcraft_dir, CLI_NAMES)

    @property
    def app(self) -> Path:
        """
        Locate SolidCraft's desktop application.

        Returns:
        - The path of ``solidcraft.exe``, or of ``solvecraft.exe`` in a build older than 0.5.
        """
        return first_existing(self.solidcraft_dir, APP_NAMES)


def path_setting(raw: dict, key: str) -> Path | None:
    """
    Read one path from the configuration.

    Args:
    - raw: The parsed configuration file.
    - key: The setting's name.

    Returns:
    - The path with environment variables and ``~`` expanded, or None when the setting is empty.
    """
    return Path(os.path.expandvars(raw[key])).expanduser() if raw.get(key) else None


def read_settings(path: Path) -> dict:
    """
    Read the settings in a configuration file.

    Args:
    - path: The YAML configuration file.

    Returns:
    - The settings, by name.

    Raises:
    - ConversionError: If the file cannot be read or parsed, or is not a mapping of settings.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        logger.error("Could not read the configuration %s: %s", path, error)
        raise ConversionError(f"could not read the configuration {path}: {error}") from error
    if not isinstance(raw, dict):
        logger.error("The configuration %s is not a mapping of settings", path)
        raise ConversionError(f"the configuration {path} is not a mapping of settings")
    return raw


def load_config(path: Path | None, overrides: dict[str, str | None] | None = None) -> Config:
    """
    Put the configuration together and check the tools it names exist.

    Args:
    - path: The YAML configuration file, or None to use the overrides alone.
    - overrides: Settings given on the command line; one that is not empty
      replaces the file's.

    Returns:
    - The configuration.

    Raises:
    - ConversionError: If the file cannot be read, or a tool it or the overrides name is not there.
    """
    raw = read_settings(path) if path is not None else {}
    given = {key: value for key, value in (overrides or {}).items() if value}
    raw = raw | given
    # solvecraft_dir is the setting's name from before SolidCraft was renamed; a configuration that still uses it keeps working.
    folder = path_setting(raw, "solidcraft_dir") or path_setting(raw, "solvecraft_dir") or Path()
    config = Config(folder, path_setting(raw, "output_dir"), path_setting(raw, "cadmpeg") or Path())
    if not config.cli.is_file():
        logger.error("None of %s found in %s", CLI_NAMES, config.solidcraft_dir)
        raise ConversionError(f"{CLI_NAMES[0]} not found in {config.solidcraft_dir} (set solidcraft_dir in the configuration, or pass --solidcraft-dir)")
    if not config.cadmpeg.is_file():
        logger.error("cadmpeg not found at %s", config.cadmpeg)
        raise ConversionError(f"cadmpeg not found at {config.cadmpeg} (set cadmpeg in the configuration, or pass --cadmpeg); it reads the Fusion timeline")
    logger.info(
        "Configuration from %s%s: SolidCraft in %s, cadmpeg at %s, output to %s",
        path or "the command line",
        f" with {sorted(given)} from the command line" if given and path else "",
        config.cli,
        config.cadmpeg,
        config.output_dir or "the source's folder",
    )
    return config


class Session:
    """
    A headless design session: `solidcraft-cli mcp`, one JSON-RPC message per line.
    """

    def __init__(self, cli: Path, folders: list[Path]) -> None:
        """
        Start SolidCraft and open the session.

        Args:
        - cli: The ``solidcraft-cli`` executable.
        - folders: The folders the session reads and writes files in.

        Raises:
        - ConversionError: If SolidCraft cannot be started or does not answer.
        """
        # SolidCraft after 0.9.0 keeps a session's files to the folders it is told of; earlier ones ignore the option.
        allowed = [word for folder in dict.fromkeys(folders) for word in ("--allow-path", str(folder))]
        try:
            self.process = subprocess.Popen(
                [str(cli), "mcp", *allowed], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8"
            )
        except OSError as error:
            logger.error("Could not start %s: %s", cli, error)
            raise ConversionError(f"could not start {cli}: {error}") from error
        self.id = 0
        #: The commands that worked, in order: the script.
        self.script: list[dict] = []
        self.request(
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "f3z2solvecraft", "version": "1"}},
        )
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        logger.info("SolidCraft session started (process %s)", self.process.pid)

    def send(self, message: dict) -> None:
        """
        Write one message to SolidCraft.

        Args:
        - message: The JSON-RPC message.

        Raises:
        - ConversionError: If SolidCraft has stopped reading.
        """
        try:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()
        except OSError as error:
            logger.error("SolidCraft stopped reading: %s", error)
            raise ConversionError(f"SolidCraft stopped reading: {error}") from error

    def request(self, method: str, params: dict) -> dict:
        """
        Send a request and wait for its reply.

        Args:
        - method: The JSON-RPC method.
        - params: Its parameters.

        Returns:
        - The reply message.

        Raises:
        - ConversionError: If SolidCraft closes the connection or sends something that is not JSON.
        """
        self.id += 1
        self.send({"jsonrpc": "2.0", "id": self.id, "method": method, "params": params})
        while True:
            line = self.process.stdout.readline()
            if not line:
                logger.error("SolidCraft closed the connection during %s", method)
                raise ConversionError(f"SolidCraft closed the connection during {method}")
            try:
                reply = json.loads(line)
            except json.JSONDecodeError as error:
                logger.error("SolidCraft sent a line that is not JSON: %r", line[:200])
                raise ConversionError(f"SolidCraft sent a line that is not JSON: {error}") from error
            if reply.get("id") == self.id:
                return reply

    def call(self, tool: str, arguments: dict) -> tuple[bool, object]:
        """
        Run one of SolidCraft's tools.

        Args:
        - tool: The tool's name, such as ``execute`` or ``measure``.
        - arguments: Its arguments.

        Returns:
        - Whether it worked, and its decoded result or its error text.

        Raises:
        - ConversionError: If the connection to SolidCraft fails.
        """
        reply = self.request("tools/call", {"name": tool, "arguments": arguments})
        if "error" in reply:
            return False, reply["error"].get("message", "")
        result = reply["result"]
        text = next((block["text"] for block in result.get("content", []) if block.get("type") == "text"), "")
        if result.get("isError"):
            return False, text.strip()
        try:
            return True, json.loads(text)
        except json.JSONDecodeError:
            # Some tools answer in plain words; that is their result.
            return True, text

    def execute(self, command: str, params: dict, record: bool = True) -> tuple[bool, object]:
        """
        Run a SolidCraft command.

        Args:
        - command: The command id, such as ``solid.extrude``.
        - params: Its parameters.
        - record: Whether a command that works goes into the script; False for trials.

        Returns:
        - Whether it worked, and its result or its error text.

        Raises:
        - ConversionError: If the connection to SolidCraft fails.
        """
        ok, result = self.call("execute", {"command": command, "params": params})
        if ok and record:
            self.script.append({"command": command, "params": params})
        if not ok:
            logger.debug("%s%s was refused: %s", command, "" if record else " (trial)", result)
        elif record:
            logger.debug("%s %s", command, params.get("name", ""))
            # From 0.8 a feature that was built can still come with a warning.
            if isinstance(result, dict) and result.get("warning"):
                logger.warning("%s %s was built with a warning: %s", command, params.get("name", ""), result["warning"])
        return ok, result

    def undo(self) -> None:
        """
        Undo the last change to the design.

        Raises:
        - ConversionError: If the connection to SolidCraft fails.
        """
        self.call("undo", {})

    def bodies(self) -> list[dict]:
        """
        Measure every body in the design.

        Returns:
        - One record per body: name, volume, bounding box, face count.

        Raises:
        - ConversionError: If the connection to SolidCraft fails.
        """
        ok, result = self.call("measure", {})
        return result.get("bodies", []) if ok and isinstance(result, dict) else []

    def mesh(self, names: list[str], path: Path) -> Mesh | None:
        """
        Export bodies as triangles.

        Args:
        - names: The bodies to export.
        - path: Where the STL file is written.

        Returns:
        - The mesh, or None when no bodies are named or SolidCraft cannot export them.

        Raises:
        - ConversionError: If the connection fails or the exported file cannot be read.
        """
        if not names:
            return None
        ok, error = self.call("export", {"path": str(path), "bodies": names, "format": "stl"})
        if not ok or not path.is_file():
            logger.error("SolidCraft could not export %s: %s", names, error)
            return None
        return Mesh.load(path)

    def close(self) -> None:
        """
        End the session and wait for SolidCraft to exit.
        """
        try:
            self.process.stdin.close()
            self.process.wait(timeout=CLOSE_TIMEOUT)
        except subprocess.TimeoutExpired:
            logger.error("SolidCraft did not exit within %s s; stopping it", CLOSE_TIMEOUT)
            self.process.kill()
        except OSError as error:
            logger.error("Closing the SolidCraft session failed: %s", error)
        logger.info("SolidCraft session closed after %d recorded commands", len(self.script))
