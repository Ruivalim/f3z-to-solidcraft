from __future__ import annotations

from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLineEdit, QPushButton, QWidget


class PathField(QWidget):
    """
    A path that can be typed or browsed for.
    """

    def __init__(self, caption: str, placeholder: str = "", pick_folder: bool = True, file_filter: str = "", parent=None) -> None:
        """
        Build the field and its Browse button.

        Args:
        - caption: The title of the browse dialog.
        - placeholder: What the empty field says.
        - pick_folder: Whether Browse picks a folder; False to pick a file.
        - file_filter: The file dialog's filter, when picking a file.
        - parent: The parent widget.
        """
        super().__init__(parent)
        self.caption = caption
        self.pick_folder = pick_folder
        self.file_filter = file_filter

        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        self.edit.setClearButtonEnabled(True)
        self.button = QPushButton("Browse")
        self.button.clicked.connect(self.browse)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.button)

    def text(self) -> str:
        """
        Give the path as typed.

        Returns:
        - The field's text without surrounding spaces or quotes; empty when nothing is set.
        """
        return self.edit.text().strip().strip('"')

    def set_text(self, text: str) -> None:
        """
        Fill the field.

        Args:
        - text: The path to show.
        """
        self.edit.setText(text)
        # A long path shows from its start, not its end.
        self.edit.setCursorPosition(0)

    def browse(self) -> None:
        """
        Ask for the folder or file and put the answer in the field.
        """
        if self.pick_folder:
            chosen = QFileDialog.getExistingDirectory(self, self.caption, self.text())
        else:
            chosen, _ = QFileDialog.getOpenFileName(self, self.caption, self.text(), self.file_filter)
        if chosen:
            self.set_text(chosen)
