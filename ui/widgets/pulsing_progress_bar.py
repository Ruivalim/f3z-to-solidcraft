from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QProgressBar

from ui.theme import detect_theme, resolve_colours
from ui.widgets.skeleton_list import PulseClock

# The step under way glows between these opacities of the accent, so a long step still shows life.
STEP_ALPHA_MIN, STEP_ALPHA_RANGE = 0.18, 0.5
# While the total is not known, a segment this share of the bar sweeps across it.
SWEEP_WIDTH = 0.3


class PulsingProgressBar(QProgressBar):
    """
    A progress bar that keeps moving while work goes on.

    Steps done fill in solid; the step under way pulses ahead of them. With no
    total yet (a range of 0 to 0), a segment sweeps across instead. Paints
    itself, as the style sheet's busy bar does not animate.
    """

    def __init__(self, parent=None) -> None:
        """
        Set the bar up, still until it is shown.

        Args:
        - parent: The parent widget.
        """
        super().__init__(parent)
        self.setTextVisible(False)
        self.colours = resolve_colours(detect_theme())

    def showEvent(self, event) -> None:
        """
        Pick up the theme's colours and start pulsing.

        Args:
        - event: The show event.
        """
        super().showEvent(event)
        self.colours = resolve_colours(detect_theme())
        PulseClock.instance().subscribe(self, self.update)

    def hideEvent(self, event) -> None:
        """
        Stop pulsing; a hidden bar needs no frames.

        Args:
        - event: The hide event.
        """
        PulseClock.instance().unsubscribe(self)
        super().hideEvent(event)

    def paintEvent(self, _event) -> None:
        """
        Paint the track, the steps done, and the pulse or sweep.

        Args:
        - _event: The paint event.
        """
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = track.height() / 2
        painter.setPen(QPen(QColor(self.colours["border"]), 1))
        painter.setBrush(QColor(self.colours["input"]))
        painter.drawRoundedRect(track, radius, radius)

        inner = track.adjusted(1, 1, -1, -1)
        phase = PulseClock.instance().phase
        accent = QColor(self.colours["accent"])
        painter.setPen(Qt.PenStyle.NoPen)
        span = self.maximum() - self.minimum()
        if span <= 0:
            # No total yet: sweep a segment from off the left edge to off the right.
            width = inner.width() * SWEEP_WIDTH
            x = inner.x() - width + phase * (inner.width() + width)
            painter.setClipRect(inner)
            self.draw_segment(painter, QRectF(x, inner.y(), width, inner.height()), accent)
        else:
            done = (self.value() - self.minimum()) / span
            step = 1 / span
            self.draw_segment(painter, QRectF(inner.x(), inner.y(), inner.width() * done, inner.height()), accent)
            if done < 1:
                glow = QColor(accent)
                glow.setAlphaF(STEP_ALPHA_MIN + STEP_ALPHA_RANGE * (1 - abs(2 * phase - 1)))
                start = inner.x() + inner.width() * done
                self.draw_segment(painter, QRectF(start, inner.y(), inner.width() * min(step, 1 - done), inner.height()), glow)
        painter.end()

    @staticmethod
    def draw_segment(painter: QPainter, rect: QRectF, colour: QColor) -> None:
        """
        Fill a rounded piece of the bar.

        Args:
        - painter: The painter.
        - rect: The piece.
        - colour: Its colour.
        """
        if rect.width() <= 0:
            return
        painter.setBrush(colour)
        radius = min(rect.height(), rect.width()) / 2
        painter.drawRoundedRect(rect, radius, radius)
