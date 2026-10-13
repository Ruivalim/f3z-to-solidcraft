from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import QApplication

logger = logging.getLogger(__name__)

# The Windows 11 palettes of QSnippet's ThemeManager, cut down to what this window uses.
THEMES: dict[str, dict[str, str]] = {
    "dark": {
        "window": "#202020",
        "panel": "#2b2b2b",
        "card": "rgba(255, 255, 255, 0.05)",
        "input": "rgba(255, 255, 255, 0.06)",
        "hover": "#393939",
        "selected": "#585858",
        "text": "#ffffff",
        "text_muted": "rgba(255, 255, 255, 0.6)",
        "border": "rgba(255, 255, 255, 0.08)",
        "border_strong": "rgba(255, 255, 255, 0.28)",
        "scrollbar": "rgba(255, 255, 255, 0.18)",
        "scrollbar_hover": "rgba(255, 255, 255, 0.38)",
        "danger": "#fc4f4f",
        "warning": "#ffd75e",
        "success": "#6cc46c",
    },
    "light": {
        "window": "#f3f3f3",
        "panel": "#ffffff",
        "card": "rgba(0, 0, 0, 0.04)",
        "input": "#ffffff",
        "hover": "#ececec",
        "selected": "#dcdcdc",
        "text": "#000000",
        "text_muted": "rgba(0, 0, 0, 0.55)",
        "border": "rgba(0, 0, 0, 0.08)",
        "border_strong": "rgba(0, 0, 0, 0.3)",
        "scrollbar": "rgba(0, 0, 0, 0.2)",
        "scrollbar_hover": "rgba(0, 0, 0, 0.38)",
        "danger": "#c42b1c",
        "warning": "#8a6100",
        "success": "#157347",
    },
}
# The accent used when the system's cannot be read, as in QSnippet.
FALLBACK_ACCENT = "#9c0000"
# A system highlight colour less saturated than this is a grey, not an accent.
MINIMUM_SATURATION = 20
# Above this brightness an accent takes black text, below it white.
LIGHT_ACCENT = 150


def detect_theme() -> str:
    """
    Tell whether the system is in dark or light mode.

    Returns:
    - ``dark`` or ``light``; ``dark`` when the system does not say.
    """
    scheme = QGuiApplication.styleHints().colorScheme()
    return "light" if scheme == Qt.ColorScheme.Light else "dark"


def system_accent() -> str:
    """
    Read the system accent colour.

    Returns:
    - The colour as ``#rrggbb``, or the fallback when the system's is a grey.
    """
    colour = QGuiApplication.palette().highlight().color()
    return colour.name() if colour.saturation() > MINIMUM_SATURATION else FALLBACK_ACCENT


def contrast_text(colour: str) -> str:
    """
    Pick the text colour that stays readable on a background.

    Args:
    - colour: The background, as ``#rrggbb``.

    Returns:
    - Black for a light background, white for a dark one.
    """
    background = QColor(colour)
    luminance = 0.299 * background.red() + 0.587 * background.green() + 0.114 * background.blue()
    return "#000000" if luminance > LIGHT_ACCENT else "#ffffff"


def translucent(colour: str, alpha: float) -> str:
    """
    Make a colour see-through.

    Args:
    - colour: The colour, as ``#rrggbb``.
    - alpha: How opaque it is, from 0 to 1.

    Returns:
    - The colour as a style sheet ``rgba(...)`` value.
    """
    solid = QColor(colour)
    return f"rgba({solid.red()}, {solid.green()}, {solid.blue()}, {alpha})"


def resolve_colours(theme: str) -> dict[str, str]:
    """
    Put a theme's palette together with the system accent.

    Args:
    - theme: ``dark`` or ``light``.

    Returns:
    - The palette, with ``accent``, ``on_accent`` and ``highlight`` added.
    """
    colours = dict(THEMES[theme])
    accent = system_accent()
    colours["accent"] = accent
    colours["on_accent"] = contrast_text(accent)
    colours["highlight"] = translucent(accent, 0.18)
    return colours


def build_qss(c: dict[str, str]) -> str:
    """
    Write the application style sheet.

    Args:
    - c: The palette, by role.

    Returns:
    - The style sheet. Rules are keyed on object names, as in QSnippet.
    """
    return f"""
QWidget {{
    color: {c['text']};
}}
QMainWindow, QWidget#CentralWidget {{
    background-color: {c['window']};
}}

/* Header */
QLabel#AppTitle {{
    font-size: 18px;
    font-weight: 700;
}}
QLabel#AppSubtitle {{
    color: {c['text_muted']};
}}
QLabel#Footer {{
    color: {c['text_muted']};
}}
QFrame#ResultBanner {{
    border-radius: 8px;
}}
QFrame#ResultBanner[state="ok"] {{
    background-color: {translucent(c['success'], 0.12)};
    border: 1px solid {c['success']};
}}
QFrame#ResultBanner[state="warning"] {{
    background-color: {translucent(c['warning'], 0.12)};
    border: 1px solid {c['warning']};
}}
QFrame#ResultBanner[state="error"] {{
    background-color: {translucent(c['danger'], 0.12)};
    border: 1px solid {c['danger']};
}}
QLabel#ResultTitle {{
    font-weight: 600;
}}
QFrame#ResultBanner[state="ok"] QLabel#ResultTitle {{
    color: {c['success']};
}}
QFrame#ResultBanner[state="warning"] QLabel#ResultTitle {{
    color: {c['warning']};
}}
QFrame#ResultBanner[state="error"] QLabel#ResultTitle {{
    color: {c['danger']};
}}
QLabel#ProgressLabel {{
    color: {c['text_muted']};
    min-width: 36px;
}}

/* Drop zone */
QFrame#DropZone {{
    background-color: {c['card']};
    border: 2px dashed {c['border_strong']};
    border-radius: 10px;
}}
QFrame#DropZone[dragging="true"] {{
    background-color: {c['highlight']};
    border-color: {c['accent']};
}}
QLabel#DropTitle {{
    font-size: 15px;
    font-weight: 600;
}}
QLabel#DropHint, QLabel#FieldLabel {{
    color: {c['text_muted']};
}}

/* Options */
QFrame#OptionsCard {{
    background-color: {c['card']};
    border: 1px solid {c['border']};
    border-radius: 8px;
}}
QToolButton#SectionToggle {{
    background: transparent;
    border: none;
    font-weight: 600;
    padding: 4px 0px;
}}

/* Inputs */
QLineEdit {{
    background-color: {c['input']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    padding: 6px 8px;
    selection-background-color: {c['selected']};
}}
QLineEdit:focus {{
    border-color: {c['accent']};
}}
QListWidget#SourceList, QPlainTextEdit#RunLog {{
    background-color: {c['input']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    outline: none;
}}
QListWidget#SourceList::item {{
    padding: 4px 6px;
    border-radius: 4px;
}}
QListWidget#SourceList::item:selected {{
    background-color: {c['selected']};
    color: {c['text']};
}}

/* Buttons */
QPushButton {{
    background-color: {c['input']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    padding: 6px 12px;
}}
QPushButton:hover {{
    background-color: {c['hover']};
}}
QPushButton:pressed {{
    background-color: {c['selected']};
}}
QPushButton:disabled {{
    color: {c['text_muted']};
}}
QPushButton#ConvertBtn {{
    background-color: {c['accent']};
    color: {c['on_accent']};
    border: 1px solid {c['accent']};
    font-weight: 600;
    padding: 8px 28px;
}}
QPushButton#ConvertBtn:hover {{
    border-color: {c['text']};
}}
QPushButton#ConvertBtn[mode="cancel"] {{
    background-color: {c['input']};
    color: {c['danger']};
    border-color: {c['danger']};
}}
QPushButton#ConvertBtn:disabled {{
    background-color: {c['border']};
    color: {c['text_muted']};
    border-color: {c['border']};
}}

/* Progress and status */
QProgressBar {{
    background-color: {c['input']};
    border: 1px solid {c['border']};
    border-radius: 4px;
    max-height: 8px;
}}
QProgressBar::chunk {{
    background-color: {c['accent']};
    border-radius: 3px;
}}
QLabel#StatusLabel[state="error"] {{
    color: {c['danger']};
}}
QLabel#StatusLabel[state="warning"] {{
    color: {c['warning']};
}}
QLabel#StatusLabel[state="ok"] {{
    color: {c['success']};
}}

/* Scroll bars */
QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {c['scrollbar']};
    border-radius: 4px;
    min-height: 20px;
}}
QScrollBar::handle:vertical:hover {{
    background: {c['scrollbar_hover']};
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 8px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {c['scrollbar']};
    border-radius: 4px;
    min-width: 20px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {c['scrollbar_hover']};
}}
QScrollBar::add-page, QScrollBar::sub-page, QScrollBar::add-line, QScrollBar::sub-line {{
    background: transparent;
    border: none;
    width: 0;
    height: 0;
}}

QToolTip {{
    background-color: {c['panel']};
    color: {c['text']};
    border: 1px solid {c['border']};
    border-radius: 4px;
    padding: 4px 6px;
}}
"""


def apply_theme(app: QApplication) -> None:
    """
    Style the application to match the system's dark or light mode and accent.

    Args:
    - app: The application.
    """
    theme = detect_theme()
    app.setStyleSheet(build_qss(resolve_colours(theme)))
    logger.info("Theme applied: %s", theme)
