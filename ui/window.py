from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel,
    QListWidget, QMainWindow, QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from ui.conversion_worker import CONVERTED, FAILED, MISMATCH, ConversionOptions, ConversionWorker
from ui.theme import apply_theme
from ui.widgets import CollapsibleSection, DropZone, PathField
from utils.conversion_common import APP_NAME
from utils.conversion_errors import ConversionError
from utils.converter import PROJECT_DIR, SOURCE_SUFFIXES, config_path
from utils.solvecraft_session import read_settings

logger = logging.getLogger(__name__)

WINDOW_TITLE = "f3z to SolidCraft"
# The line under the title in the window's header.
APP_TAGLINE = "Convert Fusion .f3z and .f3d designs to SolidCraft, keeping the feature history"
# The SolidCraft logo: the application icon, and the picture in the header.
ICON_PATH = PROJECT_DIR / "assets" / "icon.png"
# Side of the logo in the header, in pixels.
LOGO_SIZE = 52
# Groups the window under its own taskbar button, with its own icon, instead of Python's.
APP_USER_MODEL_ID = "f3z-to-solidcraft.converter"
# The file dialog's filter for sources.
SOURCE_FILTER = "Fusion designs (*.f3z *.f3d)"
# The source list grows with its rows up to this many, then scrolls.
VISIBLE_SOURCES = 4
# How each file's outcome reads in the run log, and the status colour it gives the run.
OUTCOME_STATES = {CONVERTED: "ok", MISMATCH: "warning", FAILED: "error"}


class ConverterWindow(QMainWindow):
    """
    The converter's window: drop sources, choose how to export, convert.
    """

    def __init__(self, args: argparse.Namespace, log_path: Path) -> None:
        """
        Build the window and fill it from the command line and the configuration.

        Args:
        - args: The command line; its options set what the window starts with.
        - log_path: The log file, named when a run ends.
        """
        super().__init__()
        self.args = args
        self.log_path = log_path
        self.sources: list[Path] = []
        self.worker: ConversionWorker | None = None
        self.close_pending = False
        # True from the end of a run until its log is cleared; the button then reads Clear.
        self.run_done = False
        # The worst outcome of the run so far, for the status line's colour.
        self.run_state = "ok"

        self.setWindowTitle(WINDOW_TITLE)
        self.setMinimumWidth(620)
        self.build_ui()
        self.load_initial_values()
        self.refresh_state()

    def build_ui(self) -> None:
        """
        Lay out the window: drop zone, sources, options, then the run area.
        """
        central = QWidget()
        central.setObjectName("CentralWidget")
        self.setCentralWidget(central)
        self.root = QVBoxLayout(central)
        self.root.setContentsMargins(18, 18, 18, 18)
        self.root.setSpacing(12)
        self.build_header()
        self.build_drop_zone()
        self.build_source_list()
        self.build_options()
        self.build_run_area()

    def build_header(self) -> None:
        """
        Add the logo, the application's name and a line saying what it does.
        """
        header = QHBoxLayout()
        header.setSpacing(12)
        self.logo_label = QLabel()
        self.logo_label.setFixedSize(LOGO_SIZE, LOGO_SIZE)
        self.logo_label.setPixmap(load_logo(LOGO_SIZE, self.devicePixelRatioF()))
        header.addWidget(self.logo_label)

        text = QVBoxLayout()
        text.setSpacing(2)
        self.title_label = QLabel(WINDOW_TITLE)
        self.title_label.setObjectName("AppTitle")
        self.subtitle_label = QLabel(APP_TAGLINE)
        self.subtitle_label.setObjectName("AppSubtitle")
        text.addStretch(1)
        text.addWidget(self.title_label)
        text.addWidget(self.subtitle_label)
        text.addStretch(1)
        header.addLayout(text, 1)
        self.root.addLayout(header)

    def build_drop_zone(self) -> None:
        """
        Add the area sources are dropped on.
        """
        self.drop_zone = DropZone("Drop Fusion files or folders here", "")
        self.drop_zone.paths_dropped.connect(self.add_paths)
        self.drop_zone.clicked.connect(self.choose_files)
        self.root.addWidget(self.drop_zone, 1)

    def build_source_list(self) -> None:
        """
        Add the list of what was dropped, with its buttons.
        """
        self.source_panel = QWidget()
        layout = QVBoxLayout(self.source_panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.source_list = QListWidget()
        self.source_list.setObjectName("SourceList")
        # Long paths are shortened in the middle, keeping the drive and the file name in view.
        self.source_list.setTextElideMode(Qt.ElideMiddle)
        self.source_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        layout.addWidget(self.source_list)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.add_folder_btn = QPushButton("Add folder")
        self.add_folder_btn.clicked.connect(self.choose_folder)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self.clear_sources)
        buttons.addWidget(self.add_folder_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.clear_btn)
        layout.addLayout(buttons)
        self.root.addWidget(self.source_panel)

    def build_options(self) -> None:
        """
        Add the export options, shown once there is something to convert.
        """
        self.options_card = QFrame()
        self.options_card.setObjectName("OptionsCard")
        # Spare height goes to the drop zone and the run log, not to the card.
        self.options_card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        layout = QVBoxLayout(self.options_card)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        self.output_field = PathField("Choose where designs are saved", "Next to each source file")
        self.output_field.setToolTip("Each source saves NAME.solidcraft, NAME.script.json and NAME.report.txt here")
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        form.addRow(self.field_label("Save to"), self.output_field)
        layout.addLayout(form)

        self.open_check = QCheckBox("Open each design in SolidCraft when it is saved")
        layout.addWidget(self.open_check)
        layout.addWidget(self.build_advanced())
        self.root.addWidget(self.options_card)

    def build_advanced(self) -> CollapsibleSection:
        """
        Build the collapsed section of options most runs leave alone.

        Returns:
        - The section.
        """
        self.recursive_check = QCheckBox("Include subfolders of dropped folders")
        self.force_check = QCheckBox("Overwrite existing designs")
        self.exact_check = QCheckBox("Insert the original solid when the rebuilt history does not match")
        self.exact_check.setToolTip("A part this happens to has no feature history")
        self.solidcraft_field = PathField("Choose the SolidCraft folder", "Folder holding solidcraft-cli.exe")
        self.cadmpeg_field = PathField("Choose cadmpeg.exe", "Path of cadmpeg.exe", pick_folder=False, file_filter="cadmpeg (cadmpeg.exe);;All files (*)")

        form = QFormLayout()
        form.setSpacing(8)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        form.addRow(self.recursive_check)
        form.addRow(self.force_check)
        form.addRow(self.exact_check)
        form.addRow(self.field_label("SolidCraft folder"), self.solidcraft_field)
        form.addRow(self.field_label("cadmpeg"), self.cadmpeg_field)

        self.advanced = CollapsibleSection("Advanced")
        self.advanced.set_content_layout(form)
        return self.advanced

    def build_run_area(self) -> None:
        """
        Add the Convert button, the progress bar, the status line and the run log.
        """
        row = QHBoxLayout()
        row.setSpacing(12)
        self.status_label = QLabel()
        self.status_label.setObjectName("StatusLabel")
        self.status_label.setWordWrap(True)
        self.convert_btn = QPushButton("Convert")
        self.convert_btn.setObjectName("ConvertBtn")
        self.convert_btn.clicked.connect(self.on_convert_clicked)
        row.addWidget(self.status_label, 1)
        row.addWidget(self.convert_btn)
        self.root.addLayout(row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setVisible(False)
        self.root.addWidget(self.progress_bar)

        self.run_log = QPlainTextEdit()
        self.run_log.setObjectName("RunLog")
        self.run_log.setReadOnly(True)
        self.run_log.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.run_log.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.run_log.setMinimumHeight(170)
        self.run_log.setVisible(False)
        self.root.addWidget(self.run_log, 2)

    def field_label(self, text: str) -> QLabel:
        """
        Make the label of a form row.

        Args:
        - text: The label's text.

        Returns:
        - The label, named so the theme styles it.
        """
        label = QLabel(text)
        label.setObjectName("FieldLabel")
        return label

    def read_file_settings(self) -> dict:
        """
        Read the configuration file the window's fields start from.

        Returns:
        - The file's settings; empty when there is no file or it cannot be
          read, which the status line then says.
        """
        path = config_path(self.args)
        if path is None:
            return {}
        try:
            return read_settings(path)
        except ConversionError as error:
            logger.error("The window starts without the configuration: %s", error)
            self.set_status(str(error), "error")
            return {}

    def load_initial_values(self) -> None:
        """
        Fill the options from the command line, then from the configuration file.
        """
        args, settings = self.args, self.read_file_settings()
        self.output_field.set_text(args.output_dir or settings.get("output_dir") or "")
        self.solidcraft_field.set_text(args.solidcraft_dir or settings.get("solidcraft_dir") or settings.get("solvecraft_dir") or "")
        self.cadmpeg_field.set_text(args.cadmpeg or settings.get("cadmpeg") or "")
        self.open_check.setChecked(args.open)
        self.recursive_check.setChecked(args.recursive)
        self.force_check.setChecked(args.force)
        self.exact_check.setChecked(args.exact_on_mismatch)
        if args.source is not None:
            self.add_paths([args.source])

    def add_paths(self, paths: list[Path]) -> None:
        """
        Add dropped or chosen items to the sources, leaving out what cannot be converted.

        Args:
        - paths: Files and folders.
        """
        usable = [path for path in paths if path.is_dir() or (path.is_file() and path.suffix.lower() in SOURCE_SUFFIXES)]
        ignored = len(paths) - len(usable)
        if usable and self.run_done:
            # Something new starts over: the last run's log makes way for the next.
            self.reset_run()
        for path in usable:
            if path not in self.sources:
                self.sources.append(path)
                self.source_list.addItem(f"{path}\\" if path.is_dir() else str(path))
        logger.info("Sources now %d; ignored %d of %d added", len(self.sources), ignored, len(paths))
        if ignored:
            self.set_status(f"Ignored {ignored} item(s) that are not .f3z or .f3d files or folders", "warning")
        else:
            self.set_status("")
        self.refresh_state()

    def choose_files(self) -> None:
        """
        Ask for Fusion files and add them.
        """
        chosen, _ = QFileDialog.getOpenFileNames(self, "Choose Fusion files", "", SOURCE_FILTER)
        if chosen:
            self.add_paths([Path(name) for name in chosen])

    def choose_folder(self) -> None:
        """
        Ask for a folder of Fusion files and add it.
        """
        chosen = QFileDialog.getExistingDirectory(self, "Choose a folder of Fusion files")
        if chosen:
            self.add_paths([Path(chosen)])

    def clear_sources(self) -> None:
        """
        Forget every source.
        """
        self.sources.clear()
        self.source_list.clear()
        self.set_status("")
        self.refresh_state()

    def is_running(self) -> bool:
        """
        Tell whether a conversion is under way.

        Returns:
        - True while the worker thread runs.
        """
        return self.worker is not None and self.worker.isRunning()

    def refresh_state(self) -> None:
        """
        Show, hide and enable the widgets to suit what there is to convert and whether a run is on.
        """
        has_sources, running = bool(self.sources), self.is_running()
        count = len(self.sources)
        self.drop_zone.set_compact(has_sources)
        self.drop_zone.title.setText(f"{count} item(s) to convert" if has_sources else "Drop Fusion files or folders here")
        self.drop_zone.hint.setText("Drop more, or click to choose files" if has_sources else ".f3z and .f3d, or click to choose files")
        self.source_panel.setVisible(has_sources)
        rows = min(count, VISIBLE_SOURCES)
        self.source_list.setFixedHeight(rows * max(self.source_list.sizeHintForRow(0), 0) + 2 * self.source_list.frameWidth() + 4)
        self.options_card.setVisible(has_sources)
        # After a run the button clears its log; otherwise it converts what is listed.
        self.convert_btn.setEnabled(not running and (has_sources or self.run_done))
        self.convert_btn.setText("Converting" if running else "Clear" if self.run_done else "Convert")
        for widget in (self.drop_zone, self.source_panel, self.options_card):
            widget.setEnabled(not running)

    def set_status(self, text: str, state: str = "") -> None:
        """
        Show a line beside the Convert button.

        Args:
        - text: The line.
        - state: ``ok``, ``warning`` or ``error`` to colour it; empty for plain text.
        """
        self.status_label.setText(text)
        self.status_label.setProperty("state", state)
        # A property change alone does not re-evaluate the style sheet.
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def collect_options(self) -> ConversionOptions:
        """
        Read what the window is set to.

        Returns:
        - The sources and the export options.
        """
        return ConversionOptions(
            items=list(self.sources),
            recursive=self.recursive_check.isChecked(),
            force=self.force_check.isChecked(),
            exact_on_mismatch=self.exact_check.isChecked(),
            also_open=self.open_check.isChecked(),
            config_path=config_path(self.args),
            overrides={
                "solidcraft_dir": self.solidcraft_field.text(),
                "cadmpeg": self.cadmpeg_field.text(),
                "output_dir": self.output_field.text(),
            },
        )

    def on_convert_clicked(self) -> None:
        """
        Do what the button reads: clear the finished run, or convert what is listed.
        """
        if self.run_done:
            self.reset_run()
        else:
            self.start_conversion()

    def reset_run(self) -> None:
        """
        Put the window back as it opened: no run log, no status, the button reading Convert.
        """
        logger.info("Run log cleared")
        self.run_done = False
        self.run_log.clear()
        self.run_log.setVisible(False)
        self.set_status("")
        self.refresh_state()

    def start_conversion(self) -> None:
        """
        Start converting the sources on a worker thread.
        """
        if self.is_running() or not self.sources:
            return
        self.run_state = "ok"
        self.run_log.clear()
        self.run_log.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(True)
        self.set_status("Starting")

        self.worker = ConversionWorker(self.collect_options(), self)
        self.worker.file_started.connect(self.on_file_started)
        self.worker.file_finished.connect(self.on_file_finished)
        self.worker.noted.connect(self.run_log.appendPlainText)
        self.worker.stopped.connect(self.on_stopped)
        self.worker.all_finished.connect(self.on_all_finished)
        self.worker.finished.connect(self.on_thread_finished)
        self.worker.shown.progressed.connect(self.on_progressed)
        self.worker.shown.step_started.connect(self.set_status)
        self.worker.shown.step_finished.connect(self.on_step_finished)
        self.worker.start()
        self.refresh_state()

    def on_file_started(self, number: int, total: int, source: str) -> None:
        """
        Head the run log with the file now being converted.

        Args:
        - number: Its place in the run, from 1.
        - total: How many files the run has.
        - source: The file.
        """
        self.run_log.appendPlainText(f"[{number}/{total}] {source}")
        self.progress_bar.setRange(0, 0)

    def on_progressed(self, done: int, total: int) -> None:
        """
        Move the progress bar.

        Args:
        - done: Steps finished in the current file.
        - total: Steps the current file is known to have; 0 shows the bar as busy.
        """
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(done)

    def on_step_finished(self, what: str, status: str) -> None:
        """
        Add a finished step to the run log.

        Args:
        - what: A few words naming the step.
        - status: The step's outcome status; empty for none.
        """
        self.run_log.appendPlainText(f"  {status:<8} {what}")

    def on_file_finished(self, source: str, outcome: str, detail: str) -> None:
        """
        Add a file's outcome to the run log.

        Args:
        - source: The file.
        - outcome: One of the worker's outcomes.
        - detail: A sentence about it.
        """
        state = OUTCOME_STATES.get(outcome, "warning")
        if state == "error" or (state == "warning" and self.run_state == "ok"):
            self.run_state = state
        self.run_log.appendPlainText(f"  {outcome.upper()}: {detail}\n")

    def on_stopped(self, reason: str) -> None:
        """
        Say why nothing could be converted, and open the settings that usually explain it.

        Args:
        - reason: What stopped the run.
        """
        self.run_log.appendPlainText(f"Stopped: {reason}")
        self.set_status(f"Could not start: {reason}", "error")
        self.advanced.set_expanded(True)

    def on_all_finished(self, saved: int, total: int) -> None:
        """
        Sum the run up in the status line and empty the source list, leaving the run log to read.

        The worker sends this once, after the last file of the last folder, so
        the entries stay listed for the whole run. A run that could not start
        does not come here and keeps its entries for another try.

        Args:
        - saved: How many designs were saved.
        - total: How many files the run had.
        """
        state = self.run_state if saved == total else "error"
        self.set_status(f"{saved} of {total} saved", state)
        self.run_log.appendPlainText(f"Done: {saved} of {total} saved. Details in {self.log_path}")
        self.sources.clear()
        self.source_list.clear()
        self.run_done = True

    def on_thread_finished(self) -> None:
        """
        Put the window back in its waiting state, or close it if that was asked for during the run.
        """
        self.progress_bar.setVisible(False)
        self.refresh_state()
        if self.close_pending:
            self.close()

    def closeEvent(self, event) -> None:
        """
        Hold the window open until the file being converted is done, so SolidCraft is not left running.

        Args:
        - event: The close event.
        """
        if not self.is_running():
            event.accept()
            return
        logger.info("Close asked for during a run; finishing the current file first")
        self.close_pending = True
        self.worker.requestInterruption()
        self.setWindowTitle(f"{WINDOW_TITLE} (closing after the current file)")
        event.ignore()


def load_logo(size: int, pixel_ratio: float = 1.0) -> QPixmap:
    """
    Load the logo, scaled to a square.

    Args:
    - size: The side of the square, in pixels as layouts count them.
    - pixel_ratio: The screen's scale factor, so the logo stays sharp on a scaled display.

    Returns:
    - The logo, or an empty picture when the file cannot be read, which is logged.
    """
    logo = QPixmap(str(ICON_PATH))
    if logo.isNull():
        logger.error("The logo could not be read from %s", ICON_PATH)
        return logo
    side = round(size * pixel_ratio)
    logo = logo.scaled(side, side, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    logo.setDevicePixelRatio(pixel_ratio)
    return logo


def set_taskbar_identity() -> None:
    """
    Tell Windows this is its own application, so the taskbar shows the window's icon and not Python's.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except (AttributeError, OSError) as error:
        logger.error("The taskbar identity could not be set: %r", error)


def launch(args: argparse.Namespace, log_path: Path) -> int:
    """
    Show the converter's window and run until it is closed.

    Args:
    - args: The command line; its options set what the window starts with.
    - log_path: The log file, named when a run ends.

    Returns:
    - The application's exit code.
    """
    set_taskbar_identity()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(QIcon(str(ICON_PATH)))
    apply_theme(app)
    window = ConverterWindow(args, log_path)
    window.show()
    logger.info("Window opened")
    return app.exec()
