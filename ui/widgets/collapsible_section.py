from __future__ import annotations

from PySide6.QtCore import QAbstractAnimation, QEasingCurve, QPropertyAnimation, Qt, Signal
from PySide6.QtWidgets import QLayout, QSizePolicy, QToolButton, QVBoxLayout, QWidget

# How long the content takes to slide open or shut, in milliseconds.
SLIDE_MS = 160
# Qt's own limit on a widget's height; setting it lifts the limit the slide put on the content.
UNLIMITED_HEIGHT = 16777215


class CollapsibleSection(QWidget):
    """
    A titled section that starts collapsed and slides open when its title is clicked.
    """

    # The section has come to rest, fully open or collapsed.
    settled = Signal()

    def __init__(self, title: str, parent=None) -> None:
        """
        Build the section, collapsed and empty.

        Args:
        - title: The text of the title.
        - parent: The parent widget.
        """
        super().__init__(parent)
        # Whether the section is open, or on its way there; None until it is first set.
        self.expanded: bool | None = None
        self.toggle = QToolButton()
        self.toggle.setObjectName("SectionToggle")
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.toggle.setArrowType(Qt.RightArrow)
        self.toggle.setCursor(Qt.PointingHandCursor)
        self.toggle.toggled.connect(self.set_expanded)

        self.content = QWidget()
        self.content.setVisible(False)
        # The slide: the most height the content may take, between none and what it asks for.
        self.slide = QPropertyAnimation(self.content, b"maximumHeight", self)
        self.slide.setDuration(SLIDE_MS)
        self.slide.setEasingCurve(QEasingCurve.OutCubic)
        self.slide.finished.connect(self.settle)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.toggle)
        layout.addWidget(self.content)
        self.set_expanded(False)

    def set_content_layout(self, layout: QLayout, indent: int = 18) -> None:
        """
        Give the section what it shows when open.

        Args:
        - layout: The layout holding the section's widgets.
        - indent: How far the content sits in from the title, in pixels.
        """
        layout.setContentsMargins(indent, 0, 0, 0)
        self.content.setLayout(layout)

    def set_expanded(self, expanded: bool) -> None:
        """
        Open or collapse the section, sliding when it is on screen.

        Args:
        - expanded: Whether the content is shown.
        """
        if self.toggle.isChecked() != expanded:
            # Checking the title comes back here through its signal.
            self.toggle.setChecked(expanded)
            return
        if expanded == self.expanded:
            return
        self.expanded = expanded
        self.toggle.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.slide.stop()
        if not self.isVisible():
            # Nothing on screen to slide.
            self.settle()
            return
        # A slide cut short carries on from where the content had got to.
        height = self.content.height() if self.content.isVisible() else 0
        if expanded:
            self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
            self.content.setMaximumHeight(height)
            self.content.setVisible(True)
        self.slide.setStartValue(height)
        self.slide.setEndValue(self.content.sizeHint().height() if expanded else 0)
        self.slide.start()

    def is_sliding(self) -> bool:
        """
        Tell whether the section is on its way open or shut.

        Returns:
        - True while the slide runs.
        """
        return self.slide.state() == QAbstractAnimation.Running

    def slide_in(self) -> None:
        """
        Show a hidden section, its content sliding open from nothing when the section is open.
        """
        if self.isVisible():
            return
        self.slide.stop()
        if self.expanded:
            self.content.setMaximumHeight(0)
        self.setVisible(True)
        if self.expanded and self.isVisible():
            self.slide.setStartValue(0)
            self.slide.setEndValue(self.content.sizeHint().height())
            self.slide.start()
        else:
            self.settle()

    def settle(self) -> None:
        """
        Leave the content fully open or hidden, as the section now stands, with its height unlimited again.
        """
        self.content.setVisible(bool(self.expanded))
        self.content.setMaximumHeight(UNLIMITED_HEIGHT)
        # Collapsed, the section keeps to its title and leaves spare height to the rest.
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred if self.expanded else QSizePolicy.Maximum)
        self.settled.emit()
