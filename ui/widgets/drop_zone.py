from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout

logger = logging.getLogger(__name__)

# Height of the zone while it is the only thing in the window, and once sources are listed under it.
EMPTY_HEIGHT = 220
COMPACT_HEIGHT = 96


class DropZone(QFrame):
    """
    The area files and folders are dropped on; a click on it asks to browse instead.
    """

    paths_dropped = Signal(list)
    clicked = Signal()

    def __init__(self, title: str, hint: str, parent=None) -> None:
        """
        Build the zone.

        Args:
        - title: The line that says what to drop.
        - hint: The smaller line under it.
        - parent: The parent widget.
        """
        super().__init__(parent)
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(EMPTY_HEIGHT)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(6)
        self.title = QLabel(title)
        self.title.setObjectName("DropTitle")
        self.title.setAlignment(Qt.AlignCenter)
        self.hint = QLabel(hint)
        self.hint.setObjectName("DropHint")
        self.hint.setAlignment(Qt.AlignCenter)
        layout.addStretch(1)
        layout.addWidget(self.title)
        layout.addWidget(self.hint)
        layout.addStretch(1)

    def set_compact(self, compact: bool) -> None:
        """
        Shrink the zone once there is something listed under it, or grow it back.

        Args:
        - compact: Whether the zone gives up its height.
        """
        self.setMinimumHeight(COMPACT_HEIGHT if compact else EMPTY_HEIGHT)

    def set_dragging(self, dragging: bool) -> None:
        """
        Highlight the zone while something is held over it.

        Args:
        - dragging: Whether a drag is over the zone.
        """
        self.setProperty("dragging", dragging)
        # A property change alone does not re-evaluate the style sheet.
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event) -> None:
        """
        Accept a drag that carries files or folders.

        Args:
        - event: The drag event.
        """
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.set_dragging(True)

    def dragLeaveEvent(self, event) -> None:
        """
        Drop the highlight when the drag leaves.

        Args:
        - event: The drag event.
        """
        self.set_dragging(False)

    def dropEvent(self, event) -> None:
        """
        Pass on the files and folders that were dropped.

        Args:
        - event: The drop event.
        """
        self.set_dragging(False)
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        logger.debug("Dropped %d item(s)", len(paths))
        if paths:
            event.acceptProposedAction()
            self.paths_dropped.emit(paths)

    def mouseReleaseEvent(self, event) -> None:
        """
        Treat a left click as a request to browse.

        Args:
        - event: The mouse event.
        """
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
