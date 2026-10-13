from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QFontDatabase, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel,
    QMainWindow, QPlainTextEdit, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from ui.conversion_worker import CANCELLED, CONVERTED, FAILED, MISMATCH, ConversionOptions, ConversionWorker
from ui.source_check_worker import SourceCheckWorker
from ui.theme import apply_theme
from ui.widgets import CollapsibleSection, DropZone, PathField, PulsingProgressBar, SkeletonList
from utils.conversion_common import APP_NAME
from utils.conversion_errors import ConversionError
from utils.converter import PROJECT_DIR, config_path, open_design
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
# How spare height is shared out: a third to the upload section and two thirds to the run log. The
# filler under them takes next to nothing beside those, and all of it once both are collapsed.
UPLOAD_STRETCH, LOGS_STRETCH, FILLER_STRETCH = 100, 200, 1
# How each file's outcome reads in the run log, and the status colour it gives the run.
OUTCOME_STATES = {CONVERTED: "ok", MISMATCH: "warning", FAILED: "error", CANCELLED: "warning"}


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
        # Checks of dropped items still running; each adds its sources when done.
        self.checks: list[SourceCheckWorker] = []
        self.close_pending = False
        # True from a cancel until the next run; the worker's own flag is gone once its thread ends.
        self.cancel_requested = False
        # The file being converted and how many the run has, for the progress label.
        self.file_number = self.file_total = 0
        # True from the end of a run until its log is cleared; the button then reads Clear.
        self.run_done = False
        # The worst outcome of the run so far, for the status line's colour.
        self.run_state = "ok"
        # True from the start of a run until its log, progress bar and footer are shown, which waits
        # for the sections above them to close.
        self.logs_pending = False
        # The size the window opened at: the height it keeps to, growing past it only while what is showing needs more.
        self.start_size: QSize | None = None
        # The height the showing widgets needed at the last fit; the window is fitted again when it changes.
        self.fitted_height = 0
        # One fit for however many layout changes arrive together, after the layout has caught up with them.
        self.fit_timer = QTimer(self)
        self.fit_timer.setSingleShot(True)
        self.fit_timer.setInterval(0)
        self.fit_timer.timeout.connect(self.fit_window)

        self.setWindowTitle(WINDOW_TITLE)
        self.setMinimumWidth(620)
        self.build_ui()
        self.load_initial_values()
        self.refresh_state()

    def build_ui(self) -> None:
        """
        Lay out the window: the upload section, options, then the run area.
        """
        central = QWidget()
        central.setObjectName("CentralWidget")
        self.setCentralWidget(central)
        self.root = QVBoxLayout(central)
        self.root.setContentsMargins(18, 18, 18, 18)
        self.root.setSpacing(12)
        self.build_header()
        self.build_upload()
        self.build_options()
        self.build_run_area()
        self.build_footer()
        # What a run adds is shown once these have closed; shown with them it would grow the window first and shrink it after.
        for section in (self.upload, self.output):
            section.settled.connect(self.show_pending_logs)
        # The advanced options take the upload section's place while open, so the window stays near its size.
        self.advanced.toggle.toggled.connect(self.on_advanced_toggled)
        # Takes the height left over when every section is collapsed, so the rest stays at the top.
        self.root.addStretch(FILLER_STRETCH)
        # Whatever is shown, hidden, collapsed or resized asks the central widget for a new layout.
        central.installEventFilter(self)

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

    def build_upload(self) -> None:
        """
        Add the Upload section: the drop zone and the source list, collapsed while a run is on.
        """
        layout = QVBoxLayout()
        layout.setSpacing(12)
        self.build_drop_zone(layout)
        self.build_source_list(layout)
        self.upload = CollapsibleSection("Upload")
        self.upload.set_content_layout(layout, indent=0)
        self.upload.set_expanded(True)
        self.root.addWidget(self.upload, UPLOAD_STRETCH)

    def build_drop_zone(self, parent: QVBoxLayout) -> None:
        """
        Add the area sources are dropped on.

        Args:
        - parent: The layout it goes in.
        """
        self.drop_zone = DropZone("Drop Fusion files or folders here", "")
        self.drop_zone.paths_dropped.connect(self.add_paths)
        self.drop_zone.clicked.connect(self.choose_files)
        parent.addWidget(self.drop_zone, 1)

    def build_source_list(self, parent: QVBoxLayout) -> None:
        """
        Add the list of what was dropped, with its buttons.

        Args:
        - parent: The layout it goes in.
        """
        self.source_panel = QWidget()
        layout = QVBoxLayout(self.source_panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # Items being checked show as pulsing rows below the listed ones.
        self.source_list = SkeletonList()
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
        parent.addWidget(self.source_panel)

    def build_options(self) -> None:
        """
        Add the export options in an Output section, shown once there is something to convert
        and collapsed while a run is on.
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
        section = QVBoxLayout()
        section.addWidget(self.options_card)
        self.output = CollapsibleSection("Output")
        self.output.set_content_layout(section, indent=0)
        self.output.set_expanded(True)
        self.root.addWidget(self.output)

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
        Add the result banner, the Close and Convert buttons, the progress bar, the status line,
        and the run log in a Logs section.
        """
        self.build_result_banner()
        row = QHBoxLayout()
        row.setSpacing(12)
        self.status_label = QLabel()
        self.status_label.setObjectName("StatusLabel")
        self.status_label.setWordWrap(True)
        self.convert_btn = QPushButton("Convert")
        self.convert_btn.setObjectName("ConvertBtn")
        self.convert_btn.clicked.connect(self.on_convert_clicked)
        # Beside the button while it reads Clear: a finished run is either cleared for another or the last one.
        self.close_btn = QPushButton("Close")
        self.close_btn.setObjectName("CloseBtn")
        self.close_btn.setToolTip("Close the converter")
        self.close_btn.clicked.connect(self.close)
        self.close_btn.setVisible(False)
        row.addWidget(self.status_label, 1)
        row.addWidget(self.close_btn)
        row.addWidget(self.convert_btn)
        self.root.addLayout(row)

        # Pulses the step under way, so a step taking minutes still shows the run is alive.
        self.progress_bar = PulsingProgressBar()
        self.progress_label = QLabel()
        self.progress_label.setObjectName("ProgressLabel")
        self.progress_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.progress_row = QWidget()
        progress = QHBoxLayout(self.progress_row)
        progress.setContentsMargins(0, 0, 0, 0)
        progress.setSpacing(10)
        progress.addWidget(self.progress_bar, 1)
        progress.addWidget(self.progress_label)
        self.progress_row.setVisible(False)
        self.root.addWidget(self.progress_row)

        self.run_log = QPlainTextEdit()
        self.run_log.setObjectName("RunLog")
        self.run_log.setReadOnly(True)
        self.run_log.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.run_log.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.run_log.setMinimumHeight(170)
        layout = QVBoxLayout()
        layout.addWidget(self.run_log)
        # Shown from the start of a run until it is cleared; open unless collapsed.
        self.logs = CollapsibleSection("Logs")
        self.logs.set_content_layout(layout, indent=0)
        self.logs.set_expanded(True)
        self.logs.setVisible(False)
        self.root.addWidget(self.logs, LOGS_STRETCH)

    def build_result_banner(self) -> None:
        """
        Add the banner that says how a finished run went, with buttons to open the log file
        and what was saved; hidden until a run finishes.
        """
        self.result_banner = QFrame()
        self.result_banner.setObjectName("ResultBanner")
        layout = QHBoxLayout(self.result_banner)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(8)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.result_title = QLabel()
        self.result_title.setObjectName("ResultTitle")
        self.result_detail = QLabel()
        self.result_detail.setObjectName("ResultDetail")
        self.result_detail.setWordWrap(True)
        text.addWidget(self.result_title)
        text.addWidget(self.result_detail)
        layout.addLayout(text, 1)
        self.open_log_btn = self.result_action("Open log", self.open_log)
        self.open_log_btn.setToolTip("Open the log file, which has every step of the run and the report of each design")
        # Reads Open design or Open folder, by how many designs the run saved.
        self.open_saved_btn = self.result_action("Open design", self.open_saved)
        layout.addWidget(self.open_log_btn, 0, Qt.AlignVCenter)
        layout.addWidget(self.open_saved_btn, 0, Qt.AlignVCenter)
        self.result_banner.setVisible(False)
        self.root.addWidget(self.result_banner)

    def result_action(self, text: str, action) -> QPushButton:
        """
        Make a button of the result banner.

        Args:
        - text: What the button reads.
        - action: What a click on it calls.

        Returns:
        - The button, named so the theme styles it.
        """
        button = QPushButton(text)
        button.setObjectName("ResultAction")
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(action)
        return button

    def saved_designs(self) -> list[Path]:
        """
        List the designs the last run saved.

        Returns:
        - The designs, in the order they were saved; empty before the first run.
        """
        return list(self.worker.saved) if self.worker is not None else []

    def open_log(self) -> None:
        """
        Open the log file in the program the system uses for it.
        """
        self.open_path(self.log_path)

    def open_saved(self) -> None:
        """
        Open what the last run saved: its one design in SolidCraft, or the folder of its first when it saved several.
        """
        designs = self.saved_designs()
        if not designs:
            return
        if len(designs) > 1:
            self.open_path(designs[0].parent)
            return
        try:
            open_design(self.worker.config, designs[0])
        except ConversionError as error:
            logger.error("The saved design could not be opened: %s", error)
            self.set_status(str(error), "error")

    def open_path(self, path: Path) -> None:
        """
        Open a file or a folder with the system's own program for it, saying so when that fails.

        Args:
        - path: The file or folder.
        """
        if QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            logger.info("Opened %s", path)
            return
        logger.error("The system could not open %s", path)
        self.set_status(f"Could not open {path}", "error")

    def show_result(self, state: str, title: str, detail: str) -> None:
        """
        Show how the run went in the banner, coloured by its outcome.

        Args:
        - state: ``ok``, ``warning`` or ``error``.
        - title: A few words: the outcome.
        - detail: A sentence about it.
        """
        self.result_title.setText(title)
        self.result_detail.setText(detail)
        saved = len(self.saved_designs())
        self.open_saved_btn.setVisible(saved > 0)
        self.open_saved_btn.setText("Open design" if saved == 1 else "Open folder")
        self.open_saved_btn.setToolTip("Open the saved design in SolidCraft" if saved == 1 else "Open the folder the first design was saved in")
        self.result_banner.setProperty("state", state)
        # A property change alone does not re-evaluate the style sheet.
        for widget in (self.result_banner, self.result_title):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        self.result_banner.setVisible(True)

    def build_footer(self) -> None:
        """
        Add the line under the run log asking for patience, shown only while a run is on.
        """
        self.footer = QLabel("This can take a while, please be patient.")
        self.footer.setObjectName("Footer")
        self.footer.setAlignment(Qt.AlignCenter)
        self.footer.setVisible(False)
        self.root.addWidget(self.footer)

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
        Check dropped or chosen items on a worker thread; what can be converted is added when it is done.

        Args:
        - paths: Files and folders.
        """
        check = SourceCheckWorker(paths, self)
        check.checked.connect(self.on_sources_checked)
        check.finished.connect(self.on_check_finished)
        self.checks.append(check)
        self.set_status(f"Checking {len(paths)} item(s)")
        check.start()
        self.refresh_state()

    def on_sources_checked(self, usable: list[tuple[Path, bool]], ignored: int) -> None:
        """
        Add checked items to the sources.

        Args:
        - usable: The items that can be converted, each with whether it is a folder.
        - ignored: How many items were left out.
        """
        if usable and self.run_done:
            # Something new starts over: the last run's log makes way for the next.
            self.reset_run()
        for path, is_folder in usable:
            if path not in self.sources:
                self.sources.append(path)
                self.source_list.addItem(f"{path}\\" if is_folder else str(path))
        logger.info("Sources now %d; ignored %d of %d added", len(self.sources), ignored, len(usable) + ignored)
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

    def on_check_finished(self) -> None:
        """
        Forget a finished check, and close the window if that was asked for while it ran.
        """
        check = self.sender()
        if check in self.checks:
            self.checks.remove(check)
            check.deleteLater()
        self.refresh_state()
        if self.close_pending and not self.checks:
            self.close()

    def refresh_state(self) -> None:
        """
        Show, hide and enable the widgets to suit what there is to convert and whether a run is on.
        """
        running = self.is_running()
        self.refresh_sources()
        self.refresh_run_controls(running)
        # The run has read its options already, so they are locked; the Advanced
        # section still opens and closes to show what the run is using.
        for widget in (self.drop_zone, self.source_panel, self.output_field, self.open_check, self.advanced.content):
            widget.setEnabled(not running)

    def refresh_sources(self) -> None:
        """
        Fit the drop zone, the source list and the options to what is listed and what is being checked.
        """
        has_sources, checking = bool(self.sources), bool(self.checks)
        self.source_list.set_skeleton_rows(self.pending_rows())
        self.drop_zone.set_compact(has_sources or checking)
        if has_sources:
            self.drop_zone.title.setText(f"{len(self.sources)} item(s) to convert")
            self.drop_zone.hint.setText("Drop more, or click to choose files")
        else:
            self.drop_zone.title.setText("Drop Fusion files or folders here")
            self.drop_zone.hint.setText(".f3z and .f3d, or click to choose files")
        self.source_panel.setVisible(has_sources or checking)
        self.fit_source_list()
        self.output.setVisible(has_sources)
        if not has_sources:
            # The advanced options go with the Output section; closing them brings the drop zone back.
            self.advanced.set_expanded(False)

    def pending_rows(self) -> int:
        """
        Count the placeholder rows to show for items still being checked.

        Returns:
        - One per item being checked, no more than the list shows at once.
        """
        return min(sum(len(check.paths) for check in self.checks), VISIBLE_SOURCES)

    def fit_source_list(self) -> None:
        """
        Size the source list to its rows, up to VISIBLE_SOURCES; past that it scrolls.
        """
        rows = min(self.source_list.count(), VISIBLE_SOURCES)
        row_height = max(self.source_list.sizeHintForRow(0), 0)
        self.source_list.setFixedHeight(rows * row_height + 2 * self.source_list.frameWidth() + 4)

    def refresh_run_controls(self, running: bool) -> None:
        """
        Set the Convert and Close buttons and the footer for whether a run is on.

        Args:
        - running: Whether a conversion is under way.
        """
        if running:
            # During a run the button cancels it, once.
            cancelling = self.cancel_requested
            self.convert_btn.setEnabled(not cancelling)
            self.convert_btn.setText("Cancelling" if cancelling else "Cancel")
        else:
            # After a run the button clears its log; otherwise it converts what is listed.
            # Converting waits for dropped items to be checked, so none is missed.
            self.convert_btn.setEnabled(self.run_done or (bool(self.sources) and not self.checks))
            self.convert_btn.setText("Clear" if self.run_done else "Convert")
        self.close_btn.setVisible(self.run_done and not running)
        self.convert_btn.setProperty("mode", "cancel" if running else "")
        # A property change alone does not re-evaluate the style sheet.
        self.convert_btn.style().unpolish(self.convert_btn)
        self.convert_btn.style().polish(self.convert_btn)
        self.footer.setVisible(running and not self.logs_pending)

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
        Do what the button reads: cancel the run, clear the finished one, or convert what is listed.
        """
        if self.is_running():
            self.cancel_conversion()
        elif self.run_done:
            self.reset_run()
        else:
            self.start_conversion()

    def cancel_conversion(self) -> None:
        """
        Ask the run to stop at the end of the step under way; nothing of the file being converted is saved.
        """
        if not self.is_running() or self.cancel_requested:
            return
        logger.info("Cancel asked for")
        self.cancel_requested = True
        self.worker.requestInterruption()
        self.set_status("Cancelling after the current step", "warning")
        self.refresh_state()

    def reset_run(self) -> None:
        """
        Put the window back as it opened: no run log, no status, the button reading Convert.
        """
        logger.info("Run log cleared")
        self.run_done = False
        self.logs_pending = False
        self.run_log.clear()
        self.logs.setVisible(False)
        self.result_banner.setVisible(False)
        self.set_status("")
        # Closed since the run began; the advanced options keep its place if they are open.
        self.upload.set_expanded(not self.advanced.toggle.isChecked())
        self.refresh_state()

    def on_advanced_toggled(self, expanded: bool) -> None:
        """
        Collapse the upload section while the advanced options are open, and open it again after.

        During a run, and after it until its log is cleared, the upload section stays as the run set it.

        Args:
        - expanded: Whether the advanced options are now open.
        """
        if not self.is_running() and not self.run_done:
            self.upload.set_expanded(not expanded)

    def eventFilter(self, watched, event) -> bool:
        """
        Ask for the window to be fitted whenever what the central widget shows changes.

        Args:
        - watched: The widget the event is for.
        - event: The event.

        Returns:
        - Whether the event stops here; a layout request never does.
        """
        if watched is self.centralWidget() and event.type() == QEvent.LayoutRequest:
            self.fit_timer.start()
        return super().eventFilter(watched, event)

    def fit_window(self) -> None:
        """
        Bring the window's height back to the one it opened at, or to what is showing needs when
        that is more. Its width is kept, and so is a height set by hand until what is showing changes.
        """
        if self.start_size is None or self.isMaximized() or self.isFullScreen():
            return
        # The window's minimum size still counts hidden widgets until its layouts are redone.
        self.centralWidget().layout().activate()
        self.layout().activate()
        needed = self.minimumSizeHint().height()
        if needed == self.fitted_height:
            return
        self.fitted_height = needed
        height = max(self.start_size.height(), needed)
        if height != self.height():
            logger.debug("Window height %d to %d; what is showing needs %d", self.height(), height, needed)
            self.resize(self.width(), height)

    def showEvent(self, event) -> None:
        """
        Note the size the window first opens at, and what its widgets needed then.

        Args:
        - event: The show event.
        """
        super().showEvent(event)
        if self.start_size is None:
            self.start_size = self.size()
            self.fitted_height = self.minimumSizeHint().height()

    def start_conversion(self) -> None:
        """
        Start converting the sources on a worker thread.
        """
        if self.is_running() or self.checks or not self.sources:
            return
        self.run_state = "ok"
        self.cancel_requested = False
        # What was dropped and the options are not needed during the run; collapsed, they leave the room to the log.
        self.advanced.set_expanded(False)
        self.upload.set_expanded(False)
        self.output.set_expanded(False)
        self.run_log.clear()
        self.result_banner.setVisible(False)
        self.logs_pending = True
        self.file_number, self.file_total = 0, 0
        self.on_progressed(0, 0)
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
        # At once when both sections were closed already.
        self.show_pending_logs()

    def show_pending_logs(self) -> None:
        """
        Show the progress bar and the footer and slide the run log in, once a run has started
        and the sections above them have closed.
        """
        if not self.logs_pending or self.upload.is_sliding() or self.output.is_sliding():
            return
        self.logs_pending = False
        running = self.is_running()
        self.progress_row.setVisible(running)
        self.footer.setVisible(running)
        self.logs.slide_in()

    def on_file_started(self, number: int, total: int, source: str) -> None:
        """
        Head the run log with the file now being converted.

        Args:
        - number: Its place in the run, from 1.
        - total: How many files the run has.
        - source: The file.
        """
        self.run_log.appendPlainText(f"[{number}/{total}] {source}")
        self.file_number, self.file_total = number, total
        self.on_progressed(0, 0)

    def on_progressed(self, done: int, total: int) -> None:
        """
        Move the progress bar and its percentage, led by the file's place when the run has several.

        The total grows as the file's documents are read, so early on the
        percentage can drop back.

        Args:
        - done: Steps finished in the current file.
        - total: Steps the current file is known to have; 0 shows the bar as busy.
        """
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(done)
        percent = f"{100 * done // total}%" if total else "…"
        place = f"{self.file_number}/{self.file_total} · " if self.file_total > 1 else ""
        self.progress_label.setText(place + percent)

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
        Sum the run up in the result banner and empty the source list, leaving the run log to read.

        The worker sends this once, after the last file of the last folder, so
        the entries stay listed for the whole run. A run that could not start
        does not come here and keeps its entries for another try.

        Args:
        - saved: How many designs were saved.
        - total: How many files the run had.
        """
        self.run_done = True
        # The banner says it now; the status line would only repeat it.
        self.set_status("")
        designs = "design" if total == 1 else "designs"
        if self.cancel_requested:
            # The sources stay listed, so the run can be started again.
            self.show_result("warning", "Conversion cancelled", f"{saved} of {total} {designs} saved before the cancel.")
            self.run_log.appendPlainText(f"Cancelled: {saved} of {total} saved. Details in {self.log_path}")
            return
        state = self.run_state if saved == total else "error"
        if state == "ok":
            self.show_result("ok", "Conversion complete", f"{saved} of {total} {designs} saved, matching the original.")
        elif state == "warning":
            self.show_result("warning", "Saved with differences", f"{saved} of {total} {designs} saved; the log says what differs.")
        else:
            self.show_result("error", "Not everything was converted", f"{saved} of {total} {designs} saved; the log says why.")
        self.run_log.appendPlainText(f"Done: {saved} of {total} saved. Details in {self.log_path}")
        self.sources.clear()
        self.source_list.clear()

    def on_thread_finished(self) -> None:
        """
        Put the window back in its waiting state, or close it if that was asked for during the run.
        """
        self.progress_row.setVisible(False)
        # A finished run keeps the upload section closed until its log is cleared. A run that could not
        # start has nothing to clear, so the section is back at once, unless the advanced options
        # opened in its place to explain why.
        if not self.run_done:
            self.upload.set_expanded(not self.advanced.toggle.isChecked())
        self.output.set_expanded(True)
        self.refresh_state()
        if self.close_pending:
            self.close()

    def closeEvent(self, event) -> None:
        """
        Cancel a run and hold the window open until it stops, so SolidCraft is not left running,
        and until dropped items are checked, as a thread cannot be destroyed while it runs.

        Args:
        - event: The close event.
        """
        if not self.is_running() and not self.checks:
            event.accept()
            return
        if not self.is_running():
            logger.info("Close asked for while dropped items are checked; closing when that is done")
            self.close_pending = True
            event.ignore()
            return
        logger.info("Close asked for during a run; cancelling it first")
        self.close_pending = True
        self.cancel_conversion()
        self.setWindowTitle(f"{WINDOW_TITLE} (closing after the current step)")
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
