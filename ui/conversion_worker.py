from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from utils.conversion_errors import ConversionError
from utils.converter import check, convert, find_sources, make_work_folder, report, save_result
from utils.solvecraft_session import Config, load_config

logger = logging.getLogger(__name__)

# What became of one source file.
CONVERTED, MISMATCH, NOT_SAVED, FAILED = "converted", "mismatch", "not saved", "failed"


@dataclass
class ConversionOptions:
    """
    What the window asks for: the sources and how to export them.
    """

    # Files and folders, as dropped.
    items: list[Path]
    recursive: bool = False
    force: bool = False
    exact_on_mismatch: bool = False
    also_open: bool = False
    config_path: Path | None = None
    # Settings that replace the configuration file's when not empty.
    overrides: dict[str, str | None] = field(default_factory=dict)


class SignalProgress(QObject):
    """
    The converter's progress display, sending each change to the window as a signal.

    It has the methods of the terminal's Progress, so the converter drives
    either without knowing which.
    """

    progressed = Signal(int, int)
    step_started = Signal(str)
    step_finished = Signal(str, str)

    def __init__(self, parent=None) -> None:
        """
        Set up the counters.

        Args:
        - parent: The parent object.
        """
        super().__init__(parent)
        self.done = 0
        self.total = 0

    def __enter__(self) -> SignalProgress:
        """
        Start counting a new file from nothing.

        Returns:
        - This progress display.
        """
        self.done = self.total = 0
        self.progressed.emit(0, 0)
        return self

    def __exit__(self, *exc_info: object) -> None:
        """
        Leave the last state showing.

        Args:
        - *exc_info: The exception being raised through the context, if any.
        """

    def expect(self, steps: int) -> None:
        """
        Add steps to the total; it grows as the documents are read.

        Args:
        - steps: How many more steps there will be.
        """
        self.total += steps
        self.progressed.emit(self.done, self.total)

    def start(self, what: str) -> None:
        """
        Show the step now in progress.

        Args:
        - what: A few words naming it.
        """
        self.step_started.emit(what)

    def finish(self, what: str, status: str = "") -> None:
        """
        Report a finished step and move the bar on.

        Args:
        - what: A few words naming the step.
        - status: One of the outcome statuses; empty for none.
        """
        self.done += 1
        self.step_finished.emit(what, status)
        self.progressed.emit(self.done, self.total)


class ConversionWorker(QThread):
    """
    Converts the sources off the window's thread, one file after another.
    """

    # number, total, path
    file_started = Signal(int, int, str)
    # path, one of the outcomes above, detail
    file_finished = Signal(str, str, str)
    # A line worth showing that is not about one file.
    noted = Signal(str)
    # Why nothing could be converted at all.
    stopped = Signal(str)
    # saved, total
    all_finished = Signal(int, int)

    def __init__(self, options: ConversionOptions, parent=None) -> None:
        """
        Prepare the run; nothing happens until the thread is started.

        Args:
        - options: The sources and how to export them.
        - parent: The parent object.
        """
        super().__init__(parent)
        self.options = options
        self.shown = SignalProgress(self)

    def gather_sources(self) -> list[Path]:
        """
        List the files to convert: the dropped files, and the Fusion files of the dropped folders.

        Returns:
        - The files, each once, in the order they were found.

        Raises:
        - ConversionError: If there is no file to convert.
        """
        sources: list[Path] = []
        for item in self.options.items:
            if not item.is_dir():
                sources.append(item)
                continue
            try:
                sources += find_sources(item, self.options.recursive)
            except ConversionError as error:
                # One folder without Fusion files does not stop the others.
                self.noted.emit(f"Skipped: {error}")
        sources = list(dict.fromkeys(sources))
        if not sources:
            raise ConversionError("there is no .f3z or .f3d file to convert")
        return sources

    def convert_source(self, config: Config, source: Path) -> tuple[str, str]:
        """
        Convert one file and save the design, its script and the report.

        Args:
        - config: Where the tools are and where output goes.
        - source: The .f3z or .f3d file.

        Returns:
        - The outcome, and a sentence about it.

        Raises:
        - ConversionError: If the file cannot be converted or saved.
        """
        design = (config.output_dir or source.parent) / f"{source.stem}.solvecraft"
        # Asked before converting: a conversion takes minutes.
        if design.exists() and not self.options.force:
            logger.info("%s exists and was not overwritten", design)
            return NOT_SAVED, f"{design} exists (tick Overwrite existing designs)"
        logger.info("Converting %s", source)
        parts, commands = convert(config, source, make_work_folder(), self.options.exact_on_mismatch, self.shown)
        text = report(parts)
        logger.info("Report for %s:\n%s", source.name, text)
        save_result(config, source, commands, text, design, self.options.also_open)
        if any(check(part) for part in parts):
            return MISMATCH, f"saved {design}; it does NOT match its original (see {design.with_suffix('.report.txt').name})"
        return CONVERTED, f"saved {design}; it matches its original"

    def convert_all(self, config: Config, sources: list[Path]) -> int:
        """
        Convert every file, carrying on past one that fails.

        Args:
        - config: Where the tools are and where output goes.
        - sources: The files, in order.

        Returns:
        - How many designs were saved.
        """
        saved = 0
        for number, source in enumerate(sources, start=1):
            if self.isInterruptionRequested():
                logger.info("Stopped before %s: the window is closing", source)
                break
            self.file_started.emit(number, len(sources), str(source))
            try:
                outcome, detail = self.convert_source(config, source)
            except ConversionError as error:
                logger.error("Could not convert %s: %s", source, error, exc_info=True)
                outcome, detail = FAILED, str(error)
            except Exception as error:
                # Nothing is expected here. Left to propagate it would end the
                # thread and leave the window waiting, so it is recorded in
                # full and shown as this file's failure.
                logger.exception("Unexpected error converting %s", source)
                outcome, detail = FAILED, f"unexpected error: {error!r}"
            saved += outcome in (CONVERTED, MISMATCH)
            self.file_finished.emit(str(source), outcome, detail)
        return saved

    def run(self) -> None:
        """
        Read the configuration, list the files and convert them.
        """
        logger.info("Window run started: %s", self.options)
        try:
            config = load_config(self.options.config_path, self.options.overrides)
            sources = self.gather_sources()
        except ConversionError as error:
            logger.error("Window run stopped: %s", error, exc_info=True)
            self.stopped.emit(str(error))
            return
        saved = self.convert_all(config, sources)
        logger.info("Window run finished: %d of %d saved", saved, len(sources))
        self.all_finished.emit(saved, len(sources))
