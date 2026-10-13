from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLayout, QSizePolicy, QToolButton, QVBoxLayout, QWidget


class CollapsibleSection(QWidget):
    """
    A titled section that starts collapsed and opens when its title is clicked.
    """

    def __init__(self, title: str, parent=None) -> None:
        """
        Build the section, collapsed and empty.

        Args:
        - title: The text of the title.
        - parent: The parent widget.
        """
        super().__init__(parent)
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
        Open or collapse the section.

        Args:
        - expanded: Whether the content is shown.
        """
        self.toggle.setChecked(expanded)
        self.toggle.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.content.setVisible(expanded)
        # Collapsed, the section keeps to its title and leaves spare height to the rest.
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred if expanded else QSizePolicy.Maximum)
