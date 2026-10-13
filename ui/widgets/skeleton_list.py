from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QStyledItemDelegate

from ui.theme import THEMES, detect_theme

# The skeleton pulse of PyQue's skeleton_pulse, so placeholders look the same in every app.
# Milliseconds between frames, and for one pulse.
PULSE_MS, CYCLE_MS = 40, 1400
# How far each bar's pulse trails the one above, so rows read as a travelling shimmer.
STEP_OFFSET = 0.08
# The bars are the theme's text colour at an opacity pulsing between these.
ALPHA_MIN, ALPHA_RANGE = 0.06, 0.10
# Bar widths, as fractions of the row, so the rows do not look like a grid.
SKELETON_WIDTHS = (0.72, 0.55, 0.82, 0.64, 0.48, 0.77, 0.6, 0.69)
# Marks a row as a placeholder.
SKELETON_ROLE = Qt.ItemDataRole.UserRole + 100
# Height of a bar, in pixels; less on a row too short for it.
BAR_HEIGHT = 10


class PulseClock(QObject):
    """
    The one clock every skeleton pulses to; it only runs while a skeleton shows.
    """

    _instance: PulseClock | None = None

    def __init__(self) -> None:
        """
        Set up the timer, stopped.
        """
        super().__init__()
        # Where the pulse is, from 0 to 1.
        self.phase = 0.0
        self.subscribers: dict[int, Callable[[], None]] = {}
        self.timer = QTimer(self)
        self.timer.setInterval(PULSE_MS)
        self.timer.timeout.connect(self.tick)

    @classmethod
    def instance(cls) -> PulseClock:
        """
        Get the clock, made on first use.

        Returns:
        - The clock.
        """
        if cls._instance is None:
            cls._instance = PulseClock()
        return cls._instance

    def subscribe(self, owner: object, repaint: Callable[[], None]) -> None:
        """
        Call a repaint on every frame until unsubscribed.

        Args:
        - owner: What the repaint belongs to; the key to unsubscribe it with.
        - repaint: Called on every frame.
        """
        self.subscribers[id(owner)] = repaint
        if not self.timer.isActive():
            self.timer.start()

    def unsubscribe(self, owner: object) -> None:
        """
        Stop calling an owner's repaint, and the timer once nothing is left.

        Args:
        - owner: What was subscribed.
        """
        self.subscribers.pop(id(owner), None)
        if not self.subscribers:
            self.timer.stop()

    def tick(self) -> None:
        """
        Move the pulse on and repaint every skeleton.
        """
        self.phase = (self.phase + PULSE_MS / CYCLE_MS) % 1.0
        for key, repaint in list(self.subscribers.items()):
            try:
                repaint()
            except RuntimeError:
                # The widget was deleted without unsubscribing.
                self.subscribers.pop(key, None)
        if not self.subscribers:
            self.timer.stop()


def pulse_colour(step: int) -> QColor:
    """
    Get a bar's colour at this point of the pulse.

    Args:
    - step: The bar's place, which sets how far its pulse trails.

    Returns:
    - The theme's text colour, at the bar's opacity.
    """
    t = (PulseClock.instance().phase + step * STEP_OFFSET) % 1.0
    colour = QColor(THEMES[detect_theme()]["text"])
    colour.setAlphaF(ALPHA_MIN + ALPHA_RANGE * (1 - abs(2 * t - 1)))
    return colour


class SkeletonDelegate(QStyledItemDelegate):
    """
    Paints placeholder rows as pulsing bars, and every other row as usual.
    """

    def paint(self, painter: QPainter, option, index) -> None:
        """
        Paint one row.

        Args:
        - painter: The painter.
        - option: Where and how to paint.
        - index: The row.
        """
        if not index.data(SKELETON_ROLE):
            super().paint(painter, option, index)
            return
        rect = QRectF(option.rect).adjusted(6, 0, -6, 0)
        height = min(BAR_HEIGHT, rect.height() - 8)
        width = rect.width() * SKELETON_WIDTHS[index.row() % len(SKELETON_WIDTHS)]
        if height <= 0 or width <= 0:
            return
        bar = QRectF(rect.x(), rect.center().y() - height / 2, width, height)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(pulse_colour(index.row()))
        painter.drawRoundedRect(bar, height / 2, height / 2)
        painter.restore()

    def sizeHint(self, option, index) -> QSize:
        """
        Size a placeholder row like a row of text, so the list keeps its height when it fills in.

        Args:
        - option: How the row is shown.
        - index: The row.

        Returns:
        - The row's size.
        """
        hint = super().sizeHint(option, index)
        if index.data(SKELETON_ROLE):
            hint.setHeight(max(hint.height(), option.fontMetrics.height()))
        return hint


class SkeletonList(QListWidget):
    """
    A list that shows pulsing placeholder rows below its real rows while more are on their way.
    """

    def __init__(self, parent=None) -> None:
        """
        Set the list up with no placeholders.

        Args:
        - parent: The parent widget.
        """
        super().__init__(parent)
        self.setItemDelegate(SkeletonDelegate(self))

    def real_count(self) -> int:
        """
        Count the real rows; they always come before the placeholders.

        Returns:
        - How many rows are not placeholders.
        """
        rows = self.count()
        while rows and self.item(rows - 1).data(SKELETON_ROLE):
            rows -= 1
        return rows

    def set_skeleton_rows(self, rows: int) -> None:
        """
        Show this many placeholder rows below the real ones, pulsing while there are any.

        Args:
        - rows: How many; 0 removes them.
        """
        if self.count() - self.real_count() == rows:
            return
        while self.count() > self.real_count():
            self.takeItem(self.count() - 1)
        for _ in range(rows):
            item = QListWidgetItem(" ")
            item.setData(SKELETON_ROLE, True)
            # Placeholders cannot be selected or clicked.
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.addItem(item)
        if rows:
            PulseClock.instance().subscribe(self, self.viewport().update)
        else:
            PulseClock.instance().unsubscribe(self)

    def addItem(self, item) -> None:
        """
        Add a row; a real one goes above the placeholders.

        Args:
        - item: A QListWidgetItem, or the text of one.
        """
        if isinstance(item, QListWidgetItem) and item.data(SKELETON_ROLE):
            super().addItem(item)
        else:
            self.insertItem(self.real_count(), item)

    def clear(self) -> None:
        """
        Remove every row, placeholders too, and stop pulsing.
        """
        PulseClock.instance().unsubscribe(self)
        super().clear()
