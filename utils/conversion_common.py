from __future__ import annotations

import os
import platform
import re
from dataclasses import dataclass, field
from pathlib import Path

from rich import progress
from rich.console import Console
from rich.markup import escape

from .fusion_design import Design
from .solid_oracle import Mesh

# Folder name under the user's application data, as QSnippet uses its own.
APP_NAME = "f3z-to-solidcraft"
# Name of the log file, without extension.
SCRIPT_NAME = "f3z2solvecraft"

# Bounding boxes closer than this (mm) count as the same size.
BOUNDS_TOLERANCE = 0.05
# Volumes closer than this fraction count as the same.
VOLUME_TOLERANCE = 1e-4
# Two points closer than this (mm) are the same point.
POINT_TOLERANCE = 1e-3
# How far off a sketch plane (mm) the finished solid is asked what is there.
PROBE = 0.2
# An arc end this close (mm) to a line end is meant to be on it.
SNAP = 0.01
# A sketch region is part of an extrude when at least this share of the points in it say so.
MAJORITY = 0.5
# What became of a timeline entry.
EXACT, INFERRED, FAILED, SKIPPED = "exact", "inferred", "FAILED", "skipped"


def default_paths() -> dict[str, Path]:
    """
    Resolve where the converter keeps its data and its logs.

    The same layout QSnippet uses: application data under the user's profile,
    logs in a ``logs`` folder inside it (``/var/log`` on Linux).

    Returns:
    - ``app_data`` and ``log_dir``.

    Raises:
    - KeyError: On Windows, if ``LOCALAPPDATA`` is not set.
    """
    system = platform.system()
    home = Path.home()
    if system == "Windows":
        app_data = Path(os.environ["LOCALAPPDATA"]) / APP_NAME
        log_dir = app_data / "logs"
    elif system == "Darwin":
        app_data = home / "Library" / "Application Support" / APP_NAME
        log_dir = app_data / "logs"
    else:
        app_data = Path(os.getenv("XDG_DATA_HOME", home / ".local" / "share")) / APP_NAME
        log_dir = Path("/var/log") / APP_NAME
    return {"app_data": app_data, "log_dir": log_dir}


def numbers(values, places: int = 6) -> list[float]:
    """
    Round numbers into plain floats fit for JSON.

    Args:
    - values: The numbers; numpy values are accepted.
    - places: Decimal places kept.

    Returns:
    - The rounded values, with ``-0.0`` turned into ``0.0``.
    """
    return [round(float(value), places) + 0.0 for value in values]


def slug(text: str) -> str:
    """
    Turn a name into something usable in a file or parameter name.

    Args:
    - text: Any name.

    Returns:
    - The name with each run of other characters replaced by an underscore.
    """
    return re.sub(r"\W+", "_", text).strip("_") or "doc"


@dataclass
class Solid:
    """
    What a set of bodies measures: the thing a rebuild and its original are compared by.
    """

    volume: float
    low: list[float]
    high: list[float]
    faces: int

    @property
    def size(self) -> list[float]:
        """
        Give the bounding box's size.

        Returns:
        - Its extent along x, y and z in millimetres.
        """
        return [round(self.high[i] - self.low[i], 4) for i in range(3)]

    def inside_bounds_of(self, other: Solid) -> bool:
        """
        Tell whether this solid fits in another's bounding box.

        Args:
        - other: The solid whose box is the limit.

        Returns:
        - True when no side of this one reaches past the other's.
        """
        return all(a >= b - BOUNDS_TOLERANCE for a, b in zip(self.low, other.low, strict=True)) and all(
            a <= b + BOUNDS_TOLERANCE for a, b in zip(self.high, other.high, strict=True)
        )

    def differs_from(self, other: Solid) -> str:
        """
        Compare this solid with another.

        Args:
        - other: The solid it should equal.

        Returns:
        - Why the two are not the same solid, or an empty string when they are.
        """
        if abs(self.volume - other.volume) > VOLUME_TOLERANCE * max(abs(other.volume), 1.0):
            return f"volume {self.volume:.1f} against {other.volume:.1f} mm3"
        if any(abs(a - b) > BOUNDS_TOLERANCE for a, b in zip(self.low + self.high, other.low + other.high, strict=True)):
            return f"bounds {self.low}..{self.high} against {other.low}..{other.high}"
        return ""


def measured(bodies: list[dict]) -> Solid | None:
    """
    Add up what SolidCraft measured of some bodies.

    Args:
    - bodies: The ``bodies`` of a `measure` or `eval` reply.

    Returns:
    - The bodies taken together, or None when there are none.
    """
    boxes = [body["bbox"] for body in bodies if body.get("bbox")]
    if not boxes:
        return None
    return Solid(
        volume=sum(body.get("volume_mm3") or 0.0 for body in bodies),
        low=[round(min(box["min"][i] for box in boxes), 4) for i in range(3)],
        high=[round(max(box["max"][i] for box in boxes), 4) for i in range(3)],
        faces=sum(body.get("faces") or 0 for body in bodies),
    )


@dataclass
class Outcome:
    """
    What became of one Fusion timeline entry.
    """

    feature: str
    status: str
    detail: str = ""


@dataclass
class Part:
    """
    One Fusion document being rebuilt, and what it has to match.
    """

    design: Design
    # The original solid: cadmpeg's STEP of it, what SolidCraft measures that as, and its triangles.
    step: Path | None = None
    reference: Solid | None = None
    final: Mesh | None = None
    # ``history`` (replay the timeline) or ``exact`` (insert the original solid).
    route: str = "history"
    outcomes: list[Outcome] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    result: Solid | None = None


class Progress:
    """
    A progress bar on stderr, with a line for each step as it finishes.
    """

    COLOURS = {EXACT: "green", INFERRED: "yellow", FAILED: "red", SKIPPED: "dim"}

    def __init__(self) -> None:
        """
        Set up the bar; nothing is drawn until the context is entered.
        """
        self.console = Console(stderr=True)
        self.bar = progress.Progress(
            progress.SpinnerColumn(),
            progress.TextColumn("{task.description}"),
            progress.BarColumn(),
            progress.MofNCompleteColumn(),
            progress.TimeElapsedColumn(),
            console=self.console,
        )
        self.task = self.bar.add_task("Starting", total=0)

    def __enter__(self) -> Progress:
        """
        Start drawing the bar.

        Returns:
        - This progress display.
        """
        self.bar.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        """
        Stop drawing the bar, leaving its last state on screen.

        Args:
        - *exc_info: The exception being raised through the context, if any.
        """
        self.bar.stop()

    def expect(self, steps: int) -> None:
        """
        Add steps to the total; it grows as the documents are read.

        Args:
        - steps: How many more steps there will be.
        """
        self.bar.update(self.task, total=(self.bar.tasks[0].total or 0) + steps)

    def start(self, what: str) -> None:
        """
        Show the step now in progress.

        Args:
        - what: A few words naming it.
        """
        self.bar.update(self.task, description=what)

    def finish(self, what: str, status: str = "") -> None:
        """
        Print a line for a finished step and move the bar on.

        Args:
        - what: A few words naming the step.
        - status: One of the outcome statuses, shown in colour; empty for none.
        """
        colour = self.COLOURS.get(status)
        tag = f"[{colour}]{status:<8}[/{colour}] " if colour else ""
        self.console.print(f"  {tag}{escape(what)}", highlight=False)
        self.bar.advance(self.task)
