from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from utils.converter import SOURCE_SUFFIXES

logger = logging.getLogger(__name__)


class SourceCheckWorker(QThread):
    """
    Sorts dropped or chosen items into what can be converted, off the window's thread.

    Looking at a path on a network share can take seconds, and longer when
    the share is offline; the window stays responsive meanwhile.
    """

    # [(path, is_folder), ...] of the usable items, and how many were ignored
    checked = Signal(list, int)

    def __init__(self, paths: list[Path], parent=None) -> None:
        """
        Prepare the check; nothing happens until the thread is started.

        Args:
        - paths: Files and folders, as dropped or chosen.
        - parent: The parent object.
        """
        super().__init__(parent)
        self.paths = paths

    def run(self) -> None:
        """
        Keep the folders and the Fusion files, and count the rest.
        """
        usable: list[tuple[Path, bool]] = []
        for path in self.paths:
            try:
                if path.is_dir():
                    usable.append((path, True))
                elif path.is_file() and path.suffix.lower() in SOURCE_SUFFIXES:
                    usable.append((path, False))
            except OSError as error:
                logger.error("Could not look at %s: %s", path, error)
        self.checked.emit(usable, len(self.paths) - len(usable))
