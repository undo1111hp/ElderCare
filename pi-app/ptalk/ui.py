"""Elder Care UI (PyQt6) — multi-screen, responsive, elderly-first.

Screens: Home (voice) · Medicine info · Reminders · Add reminder · Settings,
plus a fullscreen medication Alarm. Shared warm gradient + starfield background,
state-driven character mascot, big high-contrast controls. Portrait & landscape.
"""
import math
import os
import random
import subprocess
import threading
import time

from PyQt6 import QtCore, QtGui, QtWidgets

from . import medicine
from . import reminders as rem
from . import tts
from .voice_client import VoiceEngine

# ---------------- assets & palette ----------------
_PKG_DIR = os.path.dirname(__file__)
ASSET_DIR = (os.environ.get("PTALK_ASSETS")
             or (os.path.join(os.path.dirname(_PKG_DIR), "assets")
                 if os.path.isdir(os.path.join(os.path.dirname(_PKG_DIR), "assets"))
                 else os.path.join(_PKG_DIR, "..", "assets_src")))

ELDER_GRAD = ["#F0D8C8", "#F5E8E0", "#FFF3E8"]
ACCENT = "#E67E22"
ACCENT_DARK = "#D35400"
EMERGENCY = "#D32F2F"
GREEN_OK = "#2E7D32"
GREETING = "#9E3F00"
SUBGREET = "#A84A00"
INK = "#2A2A2A"
MUTED = "#707072"
STAR_COLORS = ["#5EC99A", "#7DD9B0", "#A8E8CC", "#4DC990", "#6DCFAA", "#8FE0BF", "#3DAB7A"]
WAVE_LAYERS = [("#4DC990", 2.0, 1.0, 1.00, 0.0, 70),
               ("#7DD9B0", 3.0, 1.6, 0.65, 1.2, 55),
               ("#A8E8CC", 1.4, 0.7, 0.80, 2.4, 40)]

STATE_ASSET = {"idle": "char_idle.png", "recording": "char_listening.png",
               "uploading": "char_thinking.png", "playing": "char_talking.png",
               "error": "char_error.png"}
STATUS_TEXT = {"idle": "Giữ nút để nói chuyện", "recording": "Đang nghe bạn nói...",
               "uploading": "Đang xử lý...", "playing": "Đang trả lời...",
               "error": "Có lỗi, thử lại nhé"}

_FS = 1.15  # global font scale (set from config at startup)


def H(pt, weight=QtGui.QFont.Weight.Bold):
    f = QtGui.QFont("Noto Sans")
    f.setPointSizeF(pt * _FS)
    f.setWeight(weight)
    return f


def pill_button(text, bg, fg="white", pt=17, radius=26, min_h=64):
    b = QtWidgets.QPushButton(text)
    b.setFont(H(pt, QtGui.QFont.Weight.DemiBold))
    b.setMinimumHeight(int(min_h))
    b.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
    b.setStyleSheet(
        f"QPushButton{{background:{bg};color:{fg};border:none;border-radius:{radius}px;"
        f"padding:8px 22px;}} QPushButton:pressed{{background:{ACCENT_DARK};}}"
        f" QPushButton:disabled{{background:#BBBBBB;}}")
    return b


SLIDER_QSS = (
    "QSlider::groove:horizontal{height:12px;border-radius:6px;background:#EAD3C2;}"
    "QSlider::sub-page:horizontal{background:%s;border-radius:6px;}"
    "QSlider::add-page:horizontal{background:#EAD3C2;border-radius:6px;}"
    "QSlider::handle:horizontal{width:34px;height:34px;margin:-12px 0;border-radius:17px;"
    "background:white;border:3px solid %s;}" % (ACCENT, ACCENT)
)

SCROLLBAR_QSS = (
    "QScrollBar:vertical{background:transparent;width:8px;margin:2px;}"
    "QScrollBar::handle:vertical{background:#D9B79F;border-radius:4px;min-height:44px;}"
    "QScrollBar::handle:vertical:pressed{background:#C79B7F;}"
    "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
    "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent;}"
)


# ======================================================================
#  Starfield helpers
# ======================================================================
def _make_stars(n=45):
    rng = random.Random(42)
    return [{"x": rng.random(), "y": rng.random(), "size": 6 + rng.random() * 12,
             "alpha": 80 + rng.random() * 140, "sx": (rng.random() - 0.5) * 0.0006,
             "sy": (0.2 + rng.random() * 0.5) * 0.0006,
             "tw": 0.025 + rng.random() * 0.05, "phase": rng.random() * math.pi * 2}
            for _ in range(n)]


def _star_path(cx, cy, outer):
    inner = outer * 0.4
    path = QtGui.QPainterPath()
    for k in range(8):
        r = outer if k % 2 == 0 else inner
        a = math.pi / 2 * (k / 2)
        p = QtCore.QPointF(cx + r * math.cos(a), cy + r * math.sin(a))
        path.moveTo(p) if k == 0 else path.lineTo(p)
    path.closeSubpath()
    return path


# ======================================================================
#  Frosted glass panel
# ======================================================================
class Glass(QtWidgets.QFrame):
    def __init__(self, radius=20, alpha=150, parent=None):
        super().__init__(parent)
        self._r, self._a = radius, alpha
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground)

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        r = QtCore.QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        p.setBrush(QtGui.QColor(255, 255, 255, self._a))
        p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 160), 1))
        p.drawRoundedRect(r, self._r, self._r)
        p.end()


# ======================================================================
#  Modern components: glyphs, icon badges, cards, segmented control
# ======================================================================
def draw_glyph(p, kind, cx, cy, s, color):
    col = QtGui.QColor(color)
    pen = QtGui.QPen(col, max(2.4, s * 0.16))
    pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
    NB = QtCore.Qt.BrushStyle.NoBrush
    NP = QtCore.Qt.PenStyle.NoPen
    if kind == "font":
        f = QtGui.QFont("Noto Sans"); f.setPixelSize(int(s * 1.7)); f.setWeight(QtGui.QFont.Weight.Black)
        p.setFont(f); p.setPen(QtGui.QPen(col))
        p.drawText(QtCore.QRectF(cx - s, cy - s, 2 * s, 2 * s), QtCore.Qt.AlignmentFlag.AlignCenter, "A")
    elif kind == "speaker":
        p.setPen(NP); p.setBrush(col)
        path = QtGui.QPainterPath()
        path.moveTo(cx - s * 0.6, cy - s * 0.22); path.lineTo(cx - s * 0.22, cy - s * 0.22)
        path.lineTo(cx + s * 0.1, cy - s * 0.52); path.lineTo(cx + s * 0.1, cy + s * 0.52)
        path.lineTo(cx - s * 0.22, cy + s * 0.22); path.lineTo(cx - s * 0.6, cy + s * 0.22)
        path.closeSubpath(); p.drawPath(path)
        p.setPen(pen); p.setBrush(NB)
        p.drawArc(QtCore.QRectF(cx + s * 0.08, cy - s * 0.34, s * 0.5, s * 0.68), -60 * 16, 120 * 16)
        p.drawArc(QtCore.QRectF(cx + s * 0.05, cy - s * 0.6, s * 0.95, s * 1.2), -55 * 16, 110 * 16)
    elif kind == "screen":
        p.setPen(pen); p.setBrush(NB)
        p.drawRoundedRect(QtCore.QRectF(cx - s * 0.52, cy - s * 0.44, s * 1.04, s * 0.82), s * 0.14, s * 0.14)
        p.drawLine(QtCore.QPointF(cx - s * 0.22, cy + s * 0.56), QtCore.QPointF(cx + s * 0.22, cy + s * 0.56))
    elif kind == "wifi":
        p.setPen(pen); p.setBrush(NB)
        for rr in (0.92, 0.62, 0.33):
            p.drawArc(QtCore.QRectF(cx - s * rr, cy - s * rr + s * 0.18, 2 * s * rr, 2 * s * rr), 35 * 16, 110 * 16)
        p.setBrush(col); p.setPen(NP)
        p.drawEllipse(QtCore.QPointF(cx, cy + s * 0.44), s * 0.1, s * 0.1)
    elif kind == "phone":
        white = col
        path = QtGui.QPainterPath()
        path.moveTo(cx - s * 0.5, cy - s * 0.4); path.quadTo(cx - s * 0.56, cy - s * 0.56, cx - s * 0.36, cy - s * 0.52)
        path.lineTo(cx - s * 0.16, cy - s * 0.32); path.quadTo(cx - s * 0.09, cy - s * 0.25, cx - s * 0.18, cy - s * 0.14)
        path.quadTo(cx - s * 0.02, cy + s * 0.2, cx + s * 0.29, cy + s * 0.27)
        path.quadTo(cx + s * 0.18, cy + s * 0.09, cx + s * 0.27, cy + s * 0.02)
        path.quadTo(cx + s * 0.38, cy - s * 0.07, cx + s * 0.52, cy + s * 0.02)
        path.quadTo(cx + s * 0.61, cy + s * 0.18, cx + s * 0.45, cy + s * 0.38)
        path.quadTo(cx + s * 0.27, cy + s * 0.56, cx - s * 0.05, cy + s * 0.45)
        path.quadTo(cx - s * 0.5, cy + s * 0.27, cx - s * 0.56, cy - s * 0.18)
        path.quadTo(cx - s * 0.6, cy - s * 0.31, cx - s * 0.5, cy - s * 0.4)
        p.setBrush(white); p.setPen(NP); p.drawPath(path)
    elif kind == "chevron":
        p.setPen(pen)
        p.drawLine(QtCore.QPointF(cx - s * 0.12, cy - s * 0.4), QtCore.QPointF(cx + s * 0.28, cy))
        p.drawLine(QtCore.QPointF(cx + s * 0.28, cy), QtCore.QPointF(cx - s * 0.12, cy + s * 0.4))
    elif kind == "info":
        p.setPen(pen); p.setBrush(NB)
        p.drawEllipse(QtCore.QPointF(cx, cy), s * 0.52, s * 0.52)
        p.setBrush(col); p.setPen(NP)
        p.drawEllipse(QtCore.QPointF(cx, cy - s * 0.24), s * 0.09, s * 0.09)
        p.setPen(pen); p.drawLine(QtCore.QPointF(cx, cy - s * 0.02), QtCore.QPointF(cx, cy + s * 0.34))


class IconBadge(QtWidgets.QWidget):
    """A soft-tinted rounded square with a colored glyph — modern settings icon."""
    def __init__(self, kind, color, size=46, parent=None):
        super().__init__(parent)
        self.kind = kind; self.color = QtGui.QColor(color)
        self.setFixedSize(size, size)

    def paintEvent(self, _):
        p = QtGui.QPainter(self); p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        w = self.width()
        tint = QtGui.QColor(self.color); tint.setAlpha(40)
        p.setPen(QtCore.Qt.PenStyle.NoPen); p.setBrush(tint)
        p.drawRoundedRect(QtCore.QRectF(0, 0, w, w), w * 0.30, w * 0.30)
        draw_glyph(p, self.kind, w / 2, w / 2, w * 0.30, self.color)
        p.end()


class Card(Glass):
    """Rounded white panel with a soft shadow; header(icon+title) + body via .v."""
    def __init__(self, radius=22, alpha=238, parent=None):
        super().__init__(radius=radius, alpha=alpha, parent=parent)
        try:
            eff = QtWidgets.QGraphicsDropShadowEffect(self)
            eff.setBlurRadius(26); eff.setOffset(0, 6)
            eff.setColor(QtGui.QColor(150, 80, 30, 55))
            self.setGraphicsEffect(eff)
        except Exception:
            pass
        self.v = QtWidgets.QVBoxLayout(self)
        self.v.setContentsMargins(18, 15, 18, 16); self.v.setSpacing(12)

    def header(self, kind, color, title, subtitle=None):
        h = QtWidgets.QHBoxLayout(); h.setSpacing(12)
        h.addWidget(IconBadge(kind, color, 46), 0, QtCore.Qt.AlignmentFlag.AlignVCenter)
        tv = QtWidgets.QVBoxLayout(); tv.setSpacing(0)
        t = QtWidgets.QLabel(title); t.setFont(H(16.5, QtGui.QFont.Weight.ExtraBold)); t.setStyleSheet(f"color:{INK};")
        tv.addWidget(t)
        self.sub = None
        if subtitle is not None:
            self.sub = QtWidgets.QLabel(subtitle); self.sub.setFont(H(13)); self.sub.setWordWrap(True)
            self.sub.setStyleSheet(f"color:{MUTED};"); tv.addWidget(self.sub)
        h.addLayout(tv, 1)
        self.v.addLayout(h)
        return h


class Segmented(QtWidgets.QWidget):
    """Modern segmented selector (row of connected pills; one highlighted)."""
    changed = QtCore.pyqtSignal(int)

    def __init__(self, options, current=0, parent=None):
        super().__init__(parent)
        self.cur = current; self._btns = []
        self.setStyleSheet("background:transparent;")
        row = QtWidgets.QHBoxLayout(self); row.setContentsMargins(0, 0, 0, 0); row.setSpacing(6)
        for i, o in enumerate(options):
            b = QtWidgets.QPushButton(o); b.setMinimumHeight(54)
            b.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            b.setFont(H(15, QtGui.QFont.Weight.DemiBold))
            b.clicked.connect(lambda _=None, idx=i: self.select(idx))
            row.addWidget(b, 1); self._btns.append(b)
        self._restyle()

    def select(self, idx):
        if idx == self.cur:
            return
        self.cur = idx; self._restyle(); self.changed.emit(idx)

    def set_current(self, idx):
        self.cur = idx; self._restyle()

    def _restyle(self):
        for i, b in enumerate(self._btns):
            if i == self.cur:
                b.setStyleSheet("QPushButton{background:%s;color:white;border:none;border-radius:15px;}" % ACCENT)
            else:
                b.setStyleSheet("QPushButton{background:#FBEFE7;color:%s;border:1px solid #EAD3C2;border-radius:15px;}"
                                "QPushButton:pressed{background:#F3E0D2;}" % ACCENT_DARK)


# ======================================================================
#  Round icon button
# ======================================================================
class CircleButton(QtWidgets.QWidget):
    pressedDown = QtCore.pyqtSignal()
    releasedUp = QtCore.pyqtSignal()
    clicked = QtCore.pyqtSignal()

    def __init__(self, kind, color, diameter=84, hold=False, parent=None):
        super().__init__(parent)
        self.kind, self.color, self.d, self.hold = kind, QtGui.QColor(color), diameter, hold
        self.pulsing = False
        self._m = max(9, int(diameter * 0.20))
        self.setFixedSize(diameter + 2 * self._m, diameter + 2 * self._m)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)

    def set_kind(self, kind, color):
        self.kind, self.color = kind, QtGui.QColor(color); self.update()

    def set_pulsing(self, on):
        if on != self.pulsing:
            self.pulsing = on; self.update()

    def mousePressEvent(self, e):
        if self.hold: self.pressedDown.emit()
        self.update()

    def mouseReleaseEvent(self, e):
        if self.hold: self.releasedUp.emit()
        else: self.clicked.emit()
        self.update()

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        cx, cy = self.width() / 2, self.height() / 2
        d = self.d + (6 if self.pulsing else 0)
        glow_r = d / 2 + (18 if self.pulsing else 10)
        grad = QtGui.QRadialGradient(cx, cy, glow_r)
        c1 = QtGui.QColor(self.color); c1.setAlpha(120 if self.pulsing else 85)
        c0 = QtGui.QColor(self.color); c0.setAlpha(0)
        grad.setColorAt(0.55, c1); grad.setColorAt(1.0, c0)
        p.setPen(QtCore.Qt.PenStyle.NoPen); p.setBrush(grad)
        p.drawEllipse(QtCore.QPointF(cx, cy), glow_r, glow_r)
        p.setBrush(self.color)
        p.drawEllipse(QtCore.QPointF(cx, cy), d / 2, d / 2)
        self._icon(p, cx, cy, d * 0.44)
        p.end()

    def _icon(self, p, cx, cy, s):
        white = QtGui.QColor("white")
        pen = QtGui.QPen(white, max(2.5, s * 0.12))
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
        k = self.kind
        if k == "mic":
            p.setPen(QtCore.Qt.PenStyle.NoPen); p.setBrush(white)
            bw = s * 0.42
            p.drawRoundedRect(QtCore.QRectF(cx - bw / 2, cy - s * 0.6, bw, s * 0.72), bw / 2, bw / 2)
            p.setPen(pen); p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            rr = s * 0.5
            p.drawArc(QtCore.QRectF(cx - rr, cy - rr * 0.7, 2 * rr, 2 * rr), 200 * 16, 140 * 16)
            p.drawLine(QtCore.QPointF(cx, cy + s * 0.32), QtCore.QPointF(cx, cy + s * 0.62))
            p.drawLine(QtCore.QPointF(cx - s * 0.28, cy + s * 0.62), QtCore.QPointF(cx + s * 0.28, cy + s * 0.62))
        elif k == "pill":
            p.save(); p.translate(cx, cy); p.rotate(-45)
            pw, ph = s * 1.5, s * 0.72
            p.setPen(pen); p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QtCore.QRectF(-pw / 2, -ph / 2, pw, ph), ph / 2, ph / 2)
            p.drawLine(QtCore.QPointF(0, -ph / 2), QtCore.QPointF(0, ph / 2))
            p.restore()
        elif k == "phone":
            path = QtGui.QPainterPath()
            path.moveTo(cx - s * 0.55, cy - s * 0.45)
            path.quadTo(cx - s * 0.62, cy - s * 0.62, cx - s * 0.40, cy - s * 0.58)
            path.lineTo(cx - s * 0.18, cy - s * 0.36)
            path.quadTo(cx - s * 0.10, cy - s * 0.28, cx - s * 0.20, cy - s * 0.16)
            path.quadTo(cx - s * 0.02, cy + s * 0.22, cx + s * 0.32, cy + s * 0.30)
            path.quadTo(cx + s * 0.20, cy + s * 0.10, cx + s * 0.30, cy + s * 0.02)
            path.quadTo(cx + s * 0.42, cy - s * 0.08, cx + s * 0.58, cy + s * 0.02)
            path.quadTo(cx + s * 0.68, cy + s * 0.20, cx + s * 0.50, cy + s * 0.42)
            path.quadTo(cx + s * 0.30, cy + s * 0.62, cx - s * 0.05, cy + s * 0.50)
            path.quadTo(cx - s * 0.55, cy + s * 0.30, cx - s * 0.62, cy - s * 0.20)
            path.quadTo(cx - s * 0.66, cy - s * 0.34, cx - s * 0.55, cy - s * 0.45)
            p.setBrush(white); p.setPen(QtCore.Qt.PenStyle.NoPen); p.drawPath(path)
        elif k == "close":
            p.setPen(pen); o = s * 0.5
            p.drawLine(QtCore.QPointF(cx - o, cy - o), QtCore.QPointF(cx + o, cy + o))
            p.drawLine(QtCore.QPointF(cx - o, cy + o), QtCore.QPointF(cx + o, cy - o))
        elif k == "bell":
            p.setPen(pen); p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            path = QtGui.QPainterPath()
            path.moveTo(cx - s * 0.45, cy + s * 0.28)
            path.quadTo(cx - s * 0.45, cy - s * 0.15, cx - s * 0.28, cy - s * 0.32)
            path.quadTo(cx - s * 0.28, cy - s * 0.55, cx, cy - s * 0.55)
            path.quadTo(cx + s * 0.28, cy - s * 0.55, cx + s * 0.28, cy - s * 0.32)
            path.quadTo(cx + s * 0.45, cy - s * 0.15, cx + s * 0.45, cy + s * 0.28)
            path.closeSubpath()
            p.drawPath(path)
            p.drawArc(QtCore.QRectF(cx - s * 0.14, cy + s * 0.28, s * 0.28, s * 0.28), 180 * 16, 180 * 16)
        elif k == "rotate":
            p.setPen(pen); p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            p.drawArc(QtCore.QRectF(cx - s * 0.5, cy - s * 0.5, s, s), 40 * 16, 280 * 16)
            ah = QtGui.QPainterPath()
            tip = QtCore.QPointF(cx + s * 0.5 * math.cos(math.radians(-40)),
                                 cy - s * 0.5 * math.sin(math.radians(-40)))
            ah.moveTo(tip)
            ah.lineTo(tip.x() - s * 0.24, tip.y() - s * 0.02)
            ah.moveTo(tip)
            ah.lineTo(tip.x() - s * 0.02, tip.y() + s * 0.26)
            p.drawPath(ah)
        elif k == "gear":
            p.setPen(pen); p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            p.drawEllipse(QtCore.QPointF(cx, cy), s * 0.34, s * 0.34)
            for i in range(8):
                a = math.pi / 4 * i
                p.drawLine(QtCore.QPointF(cx + s * 0.42 * math.cos(a), cy + s * 0.42 * math.sin(a)),
                           QtCore.QPointF(cx + s * 0.56 * math.cos(a), cy + s * 0.56 * math.sin(a)))
        elif k == "back":
            p.setPen(pen)
            p.drawLine(QtCore.QPointF(cx + s * 0.25, cy - s * 0.45), QtCore.QPointF(cx - s * 0.3, cy))
            p.drawLine(QtCore.QPointF(cx - s * 0.3, cy), QtCore.QPointF(cx + s * 0.25, cy + s * 0.45))


class ActionButton(QtWidgets.QWidget):
    """Round button + caption underneath (elderly-friendly)."""
    def __init__(self, kind, color, caption, diameter=88, hold=False, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(2)
        lay.setAlignment(QtCore.Qt.AlignmentFlag.AlignHCenter)
        self.btn = CircleButton(kind, color, diameter, hold)
        self.cap = QtWidgets.QLabel(caption)
        self.cap.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.cap.setFont(H(12.5, QtGui.QFont.Weight.DemiBold))
        self.cap.setStyleSheet(f"color:{ACCENT_DARK};")
        lay.addWidget(self.btn, 0, QtCore.Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(self.cap)

    def set_caption(self, t): self.cap.setText(t)


# ======================================================================
#  Character + waveform stage
# ======================================================================
class Stage(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground)
        self.state = "idle"
        self._cache = {}
        self._amp = 0.18

    def _pix(self, st):
        if st not in self._cache:
            self._cache[st] = QtGui.QPixmap(os.path.join(ASSET_DIR, STATE_ASSET[st]))
        return self._cache[st]

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        t = float(QtWidgets.QApplication.instance().property("anim_t") or 0.0)
        band = min(240, h * 0.55); cy = h - band / 2
        targ = {"idle": 0.16, "recording": 0.34, "uploading": 0.24, "playing": 0.46, "error": 0.08}
        spd = {"idle": 0.5, "recording": 1.4, "uploading": 0.9, "playing": 2.0, "error": 0.3}
        self._amp += (targ[self.state] - self._amp) * 0.06
        speed = spd[self.state]
        for color, freq, smul, amul, pshift, alpha in WAVE_LAYERS:
            maxA = self._amp * band * amul; phase = t * 2 * math.pi * speed
            path = QtGui.QPainterPath(); first = True; x = 0.0
            while x <= w:
                n = x / w if w else 0; env = math.sin(math.pi * n)
                wv = maxA * env * (0.6 * math.sin(n * 2 * math.pi * freq + phase * smul + pshift)
                                   + 0.4 * math.sin(n * 2 * math.pi * freq * 1.7 - phase * smul))
                y = cy - wv
                path.moveTo(x, y) if first else path.lineTo(x, y); first = False; x += 3
            x = float(w)
            while x >= 0:
                n = x / w if w else 0; env = math.sin(math.pi * n)
                wv = maxA * env * (0.6 * math.sin(n * 2 * math.pi * freq + phase * smul + pshift)
                                   + 0.4 * math.sin(n * 2 * math.pi * freq * 1.7 - phase * smul))
                path.lineTo(x, cy + wv); x -= 3
            path.closeSubpath()
            col = QtGui.QColor(color); col.setAlpha(alpha)
            p.setPen(QtCore.Qt.PenStyle.NoPen); p.setBrush(col); p.drawPath(path)
        pix = self._pix(self.state)
        if not pix.isNull():
            size = max(min(w * 0.8, h * 0.7, 340), 170)
            v = abs(math.sin(t * math.pi / 0.9)); scale = dy = rot = 0.0; scale = 1.0
            if self.state == "idle": scale, dy = 1 + 0.04 * v, -6 * v
            elif self.state == "recording": scale, rot = 1 + 0.12 * v, (v - 0.5) * 0.10
            elif self.state == "uploading": scale, dy = 1 + 0.06 * v, -8 * v
            elif self.state == "playing": dy, scale = -14 * v, 1 + 0.05 * v
            elif self.state == "error": rot = math.sin(v * math.pi * 4) * 0.08
            draw = int(size * scale)
            sc = pix.scaled(draw, draw, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                            QtCore.Qt.TransformationMode.SmoothTransformation)
            p.save(); p.translate(w / 2, h * 0.44 + dy); p.rotate(math.degrees(rot))
            p.drawPixmap(int(-sc.width() / 2), int(-sc.height() / 2), sc); p.restore()
        p.end()


WEEKDAYS = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]


class InfoPanel(Glass):
    """Khung mờ hội thoại. Rảnh: hiển thị ngày·giờ·lịch sắp tới (trả lời 'hôm
    nay ngày nào / mấy giờ / lịch gì'). Tương tác: đổi sang trạng thái hội
    thoại rồi tự quay lại bình thường."""

    def __init__(self, app):
        super().__init__(radius=22, alpha=170)
        self.app = app
        self.mode = "idle"
        self._last_min = -1
        self.setMinimumHeight(int(128 + 66 * _FS))
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred,
                           QtWidgets.QSizePolicy.Policy.Minimum)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(24, 16, 24, 16); v.setSpacing(6)
        v.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.l1 = QtWidgets.QLabel("")
        self.l1.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.l1.setWordWrap(True)
        self.l1.setFont(H(19, QtGui.QFont.Weight.ExtraBold)); self.l1.setStyleSheet(f"color:{ACCENT_DARK};")
        self.l2 = QtWidgets.QLabel("")
        self.l2.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.l2.setWordWrap(True)
        self.l2.setFont(H(16, QtGui.QFont.Weight.DemiBold)); self.l2.setStyleSheet(f"color:{GREETING};")
        self.l3 = QtWidgets.QLabel("")
        self.l3.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.l3.setWordWrap(True)
        self.l3.setFont(H(15)); self.l3.setStyleSheet(f"color:{INK};")
        v.addWidget(self.l1); v.addWidget(self.l2); v.addWidget(self.l3)
        self.refresh()

    def _next_reminder(self):
        items = [r for r in self.app.store.items if r.enabled]
        if not items:
            return None
        now = time.strftime("%H:%M")
        after = sorted((r for r in items if r.time >= now), key=lambda r: r.time)
        return after[0] if after else sorted(items, key=lambda r: r.time)[0]

    def set_state(self, st):
        self.mode = st
        self._last_min = -1
        self.refresh()

    def show_message(self, text):
        self.mode = "msg"
        self.l1.setText(text); self.l2.setText(""); self.l3.setText("")

    def show_chat(self, title, text):
        """Hiển thị hội thoại: dòng tiêu đề (Bà vừa nói / Ngân) + nội dung."""
        self.mode = "chat"
        self.l1.setText(title)
        self.l2.setText(text or "")
        self.l3.setText("")

    def tick(self):
        if self.mode == "idle" and time.localtime().tm_min != self._last_min:
            self.refresh()

    def refresh(self):
        msgs = {"recording": "Bác cứ nói, tôi đang nghe...",
                "uploading": "Tôi đang suy nghĩ...",
                "playing": "Tôi đang trả lời bác...",
                "error": "Có lỗi nhỏ, bác thử lại nhé"}
        if self.mode in msgs:
            self.l1.setText(msgs[self.mode]); self.l2.setText(""); self.l3.setText("")
            return
        lt = time.localtime(); self._last_min = lt.tm_min
        self.l1.setText("Hôm nay: %s" % WEEKDAYS[lt.tm_wday])
        self.l2.setText("Ngày %02d/%02d/%d" % (lt.tm_mday, lt.tm_mon, lt.tm_year))
        nr = self._next_reminder()
        self.l3.setText(("Sắp tới: %s · %s" % (nr.time, nr.label)) if nr
                        else "Hôm nay chưa có lịch nhắc")


# ======================================================================
#  Home screen (voice)
# ======================================================================
class HomeScreen(QtWidgets.QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.state = "idle"
        self._chat_a = ""      # câu trả lời gần nhất của Ngân (giữ hiển thị khi đang đọc)
        self._build()

    def _build(self):
        # top bar
        self.topbar = Glass(radius=18, alpha=150); self.topbar.setFixedHeight(60)
        tl = QtWidgets.QHBoxLayout(self.topbar); tl.setContentsMargins(16, 4, 10, 4)
        self.clock = QtWidgets.QLabel("--:--")
        self.clock.setFont(H(20, QtGui.QFont.Weight.ExtraBold))
        self.clock.setStyleSheet(f"color:{ACCENT_DARK};")
        tl.addWidget(self.clock)
        self.brand = QtWidgets.QLabel("Elder Care")
        self.brand.setFont(H(16, QtGui.QFont.Weight.ExtraBold))
        self.brand.setStyleSheet(f"color:{ACCENT_DARK};")
        self.brand.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        tl.addWidget(self.brand, 1)
        for kind, cb in (("bell", lambda: self.app.navigate("reminders")),
                         ("rotate", self.app.rotate_90),
                         ("gear", lambda: self.app.navigate("settings"))):
            b = CircleButton(kind, ACCENT, 40); b.clicked.connect(cb)
            tl.addWidget(b, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)

        # stage (character + waveform)
        self.stage = Stage(self)

        # conversation / info panel (thay greeting + status chip)
        self.info = InfoPanel(self.app)

        # action buttons
        self.a_med = ActionButton("pill", ACCENT, "Quét thuốc", 78)
        self.a_med.btn.clicked.connect(self.app.scan_medicine)
        self.a_mic = ActionButton("mic", ACCENT, "Giữ để nói", 96, hold=True)
        self.a_mic.btn.pressedDown.connect(self._mic_down)
        self.a_mic.btn.releasedUp.connect(self._mic_up)
        # While Ngân is speaking the button turns into "Dừng" and hold is set
        # False — CircleButton then emits clicked, NOT pressedDown. Without this
        # connection the stop button did nothing at all.
        self.a_mic.btn.clicked.connect(self._mic_click)
        self.a_phone = ActionButton("phone", EMERGENCY, "Gọi khẩn cấp", 78)
        self.a_phone.btn.clicked.connect(self.app.emergency_call)

        self._outer = QtWidgets.QVBoxLayout(self)
        self._outer.setContentsMargins(16, 14, 16, 18)
        self.apply_orientation(self.width() > self.height())

    def _clear_layout(self, lay):
        while lay.count():
            it = lay.takeAt(0)
            if it.widget():
                it.widget().setParent(None)
            elif it.layout():
                self._clear_layout(it.layout())

    def _btn_row(self):
        row = QtWidgets.QHBoxLayout(); row.setSpacing(0)
        row.addStretch(2)
        row.addWidget(self.a_med, 0, QtCore.Qt.AlignmentFlag.AlignBottom)
        row.addStretch(3)
        row.addWidget(self.a_mic, 0, QtCore.Qt.AlignmentFlag.AlignBottom)
        row.addStretch(3)
        row.addWidget(self.a_phone, 0, QtCore.Qt.AlignmentFlag.AlignBottom)
        row.addStretch(2)
        return row

    def apply_orientation(self, landscape):
        self._clear_layout(self._outer)
        for w in (self.topbar, self.stage, self.info,
                  self.a_med, self.a_mic, self.a_phone):
            w.setParent(self); w.show()
        self._outer.addWidget(self.topbar)
        if landscape:
            self._outer.addSpacing(6)
            mid = QtWidgets.QHBoxLayout()
            mid.addWidget(self.stage, 3)
            right = QtWidgets.QVBoxLayout()
            right.addStretch(); right.addWidget(self.info)
            right.addSpacing(18); right.addLayout(self._btn_row()); right.addStretch()
            mid.addLayout(right, 2)
            self._outer.addLayout(mid, 1)
        else:
            self._outer.addSpacing(6)
            self._outer.addWidget(self.stage, 1)
            self._outer.addWidget(self.info)
            self._outer.addSpacing(16)
            self._outer.addLayout(self._btn_row())
            self._outer.addSpacing(8)

    def resizeEvent(self, e):
        land = self.width() > self.height()
        if getattr(self, "_land", None) != land:
            self._land = land
            self.apply_orientation(land)
        super().resizeEvent(e)

    def tick_clock(self):
        self.clock.setText(time.strftime("%H:%M"))
        self.info.tick()

    def _set_state(self, st):
        self.state = st; self.stage.state = st
        self.info.set_state(st)
        self.a_mic.btn.set_pulsing(st == "recording")
        if st == "playing":
            self.a_mic.btn.set_kind("close", EMERGENCY); self.a_mic.btn.hold = False
            self.a_mic.set_caption("Dừng")
        else:
            self.a_mic.btn.set_kind("mic", ACCENT_DARK if st == "recording" else ACCENT)
            self.a_mic.btn.hold = True; self.a_mic.set_caption("Giữ để nói")
        if st == "error":
            QtCore.QTimer.singleShot(
                3000, lambda: self._set_state("idle") if self.state == "error" else None)

    def apply_voice_event(self, ev):
        U = ev.upper()
        if U in ("PROCESSING", "THINKING", "00"): self._set_state("uploading")
        elif U == "SPEAKING":
            self._set_state("playing")
            if self._chat_a:                     # giữ lời của Ngân trên khung, đừng đè bằng câu chung
                self.info.show_chat("Ngân", self._chat_a)
        elif U in ("IDLE", "PLAYBACK_DONE", "CANCELLED", "CONNECTED"):
            self._chat_a = ""; self._set_state("idle")
        elif U == "DISCONNECTED": self.info.show_message("Mất kết nối — đang kết nối lại...")
        elif U.startswith("ERR"): self._set_state("error")

    def on_transcript(self, text):
        """Server nghe được bà nói gì (khung chat)."""
        self._chat_a = ""
        self.info.show_chat("Bà vừa nói", text)

    def on_answer(self, text):
        """Câu trả lời (chữ) của Ngân — hiển thị song song với giọng đọc."""
        self._chat_a = text
        self.info.show_chat("Ngân", text)

    def set_status(self, text):
        self.info.show_message(text)

    def _mic_down(self):
        if self.state == "playing":
            self.app.engine.cancel(); self._set_state("idle"); return
        self._set_state("recording"); self.app.engine.talk_start()

    def _mic_up(self):
        if self.state == "recording":
            self.app.engine.talk_stop()

    def _mic_click(self):
        """A tap (not a hold) — the red 'Dừng' button while Ngân is speaking."""
        if self.state in ("playing", "uploading"):
            self.app.engine.cancel()
            self._set_state("idle")
            self.set_status("Cháu dừng rồi ạ")


# ======================================================================
#  Simple screen scaffold (header with back + title)
# ======================================================================
class SubScreen(QtWidgets.QWidget):
    """Base sub-screen: a full-width header + a horizontally-centered, max-width
    content column (self.root). The max-width keeps it from stretching ugly on
    wide/landscape screens; child screens just add to self.root as before."""
    def __init__(self, app, title, max_width=680):
        super().__init__()
        self.app = app
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0); outer.setSpacing(0)
        head = QtWidgets.QHBoxLayout(); head.setContentsMargins(14, 12, 16, 4); head.setSpacing(10)
        back = CircleButton("back", ACCENT, 46); back.clicked.connect(lambda: app.navigate("home"))
        head.addWidget(back)
        lbl = QtWidgets.QLabel(title); lbl.setFont(H(22, QtGui.QFont.Weight.ExtraBold))
        lbl.setStyleSheet(f"color:{ACCENT_DARK};")
        head.addWidget(lbl); head.addStretch()
        outer.addLayout(head)
        center = QtWidgets.QHBoxLayout(); center.setContentsMargins(0, 0, 0, 0)
        center.addStretch()
        self._col = QtWidgets.QWidget(); self._col.setStyleSheet("background:transparent;")
        self._col.setMaximumWidth(max_width)
        self.root = QtWidgets.QVBoxLayout(self._col)
        self.root.setContentsMargins(16, 2, 16, 16); self.root.setSpacing(14)
        center.addWidget(self._col, 1); center.addStretch()
        outer.addLayout(center, 1)


# ======================================================================
#  Medicine result screen
# ======================================================================
class MedicineScreen(SubScreen):
    def __init__(self, app):
        super().__init__(app, "Thông tin thuốc")
        self.card = Glass(radius=22, alpha=225)
        cl = QtWidgets.QVBoxLayout(self.card); cl.setContentsMargins(6, 6, 6, 6)
        self.scroll = QtWidgets.QScrollArea(); self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.scroll.setStyleSheet("background:transparent;")
        self.text = QtWidgets.QLabel("—")
        self.text.setWordWrap(True); self.text.setFont(H(17))
        self.text.setStyleSheet(f"color:{INK}; padding:14px;")
        self.text.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop | QtCore.Qt.AlignmentFlag.AlignLeft)
        self.text.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.scroll.setWidget(self.text)
        cl.addWidget(self.scroll)
        self.root.addWidget(self.card, 1)
        close = pill_button("✕  Đóng", ACCENT, min_h=68, pt=18)
        close.clicked.connect(lambda: self.app.navigate("home"))
        self.root.addWidget(close)

    def set_result(self, txt):
        self.text.setText(txt)
        self.scroll.verticalScrollBar().setValue(0)


# ======================================================================
#  Reminders list screen
# ======================================================================
class RemindersScreen(SubScreen):
    def __init__(self, app):
        super().__init__(app, "Nhắc lịch · uống thuốc")
        self.scroll = QtWidgets.QScrollArea(); self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.scroll.setStyleSheet("background:transparent;")
        self.holder = QtWidgets.QWidget(); self.holder.setStyleSheet("background:transparent;")
        self.vlist = QtWidgets.QVBoxLayout(self.holder); self.vlist.setSpacing(10)
        self.vlist.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(self.holder)
        self.root.addWidget(self.scroll, 1)
        add = pill_button("➕  Thêm nhắc mới", GREEN_OK, min_h=72, pt=19)
        add.clicked.connect(lambda: self.app.navigate("add"))
        self.root.addWidget(add)

    def refresh(self):
        while self.vlist.count():
            it = self.vlist.takeAt(0)
            if it.widget(): it.widget().setParent(None)
        items = self.app.store.items
        if not items:
            empty = QtWidgets.QLabel("Chưa có lời nhắc nào.\nBấm “Thêm nhắc mới” để tạo.")
            empty.setFont(H(16)); empty.setStyleSheet(f"color:{MUTED};")
            empty.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self.vlist.addWidget(empty); return
        for r in items:
            self.vlist.addWidget(self._row(r))

    def _row(self, r):
        card = Glass(radius=18, alpha=230); card.setMinimumHeight(84)
        h = QtWidgets.QHBoxLayout(card); h.setContentsMargins(18, 8, 12, 8); h.setSpacing(12)
        tlbl = QtWidgets.QLabel(r.time); tlbl.setFont(H(26, QtGui.QFont.Weight.ExtraBold))
        tlbl.setStyleSheet(f"color:{ACCENT_DARK if r.enabled else MUTED};")
        h.addWidget(tlbl)
        col = QtWidgets.QVBoxLayout(); col.setSpacing(0)
        name = QtWidgets.QLabel(r.label)
        name.setFont(H(16, QtGui.QFont.Weight.DemiBold))
        name.setStyleSheet(f"color:{INK if r.enabled else MUTED};")
        kind_txt = "Uống thuốc" if r.kind == "med" else "Lịch hẹn"
        sub = QtWidgets.QLabel(kind_txt + (" · Hằng ngày" if not r.days else " · Theo ngày"))
        sub.setFont(H(12)); sub.setStyleSheet(f"color:{MUTED};")
        col.addWidget(name); col.addWidget(sub)
        h.addLayout(col, 1)
        onoff = pill_button("Bật" if r.enabled else "Tắt",
                            GREEN_OK if r.enabled else "#9E9E9E", min_h=48, pt=14, radius=22)
        onoff.setFixedWidth(78)
        onoff.clicked.connect(lambda _, rid=r.id: (self.app.store.toggle(rid), self.refresh()))
        h.addWidget(onoff)
        dele = CircleButton("close", EMERGENCY, 40)
        dele.clicked.connect(lambda rid=r.id: (self.app.store.remove(rid), self.refresh()))
        h.addWidget(dele)
        return card


# ======================================================================
#  Add reminder screen
# ======================================================================
class AddReminderScreen(SubScreen):
    PRESETS = ["Thuốc huyết áp", "Thuốc tiểu đường", "Thuốc tim", "Vitamin", "Thuốc dạ dày"]

    def __init__(self, app):
        super().__init__(app, "Thêm nhắc")
        self.hh, self.mm, self.kind = 7, 0, "med"

        # time steppers
        trow = QtWidgets.QHBoxLayout(); trow.setSpacing(18)
        trow.addStretch()
        self.h_lbl = self._stepper(trow, "Giờ", lambda d: self._adj("h", d))
        colon = QtWidgets.QLabel(":"); colon.setFont(H(40, QtGui.QFont.Weight.ExtraBold))
        colon.setStyleSheet(f"color:{ACCENT_DARK};"); trow.addWidget(colon)
        self.m_lbl = self._stepper(trow, "Phút", lambda d: self._adj("m", d))
        trow.addStretch()
        self.root.addLayout(trow)

        # kind toggle
        krow = QtWidgets.QHBoxLayout(); krow.setSpacing(12)
        self.b_med = pill_button("Uống thuốc", ACCENT, min_h=64, pt=17)
        self.b_appt = pill_button("Lịch hẹn", "#9E9E9E", min_h=64, pt=17)
        self.b_med.clicked.connect(lambda: self._set_kind("med"))
        self.b_appt.clicked.connect(lambda: self._set_kind("appt"))
        krow.addWidget(self.b_med); krow.addWidget(self.b_appt)
        self.root.addLayout(krow)

        # label
        lab = QtWidgets.QLabel("Tên (bấm gợi ý hoặc gõ):")
        lab.setFont(H(15, QtGui.QFont.Weight.DemiBold)); lab.setStyleSheet(f"color:{INK};")
        self.root.addWidget(lab)
        self.name = QtWidgets.QLineEdit("Thuốc huyết áp")
        self.name.setFont(H(18)); self.name.setMinimumHeight(58)
        self.name.setStyleSheet("QLineEdit{background:white;border:2px solid #E0C0A8;"
                                "border-radius:14px;padding:6px 14px;color:#2A2A2A;}")
        self.root.addWidget(self.name)
        chips = QtWidgets.QHBoxLayout(); chips.setSpacing(8)
        for psname in self.PRESETS:
            c = pill_button(psname, "#F0D8C8", fg=ACCENT_DARK, min_h=44, pt=13, radius=20)
            c.clicked.connect(lambda _, n=psname: self.name.setText(n))
            chips.addWidget(c)
        chips.addStretch()
        self.root.addLayout(chips)
        self.root.addStretch()

        save = pill_button("✓  Lưu lời nhắc", GREEN_OK, min_h=74, pt=20)
        save.clicked.connect(self._save)
        self.root.addWidget(save)
        self._update()

    def _stepper(self, parent, cap, cb):
        box = QtWidgets.QVBoxLayout(); box.setSpacing(4); box.setAlignment(QtCore.Qt.AlignmentFlag.AlignHCenter)
        up = pill_button("▲", ACCENT, min_h=54, pt=18, radius=18); up.setFixedWidth(96)
        up.clicked.connect(lambda: cb(+1))
        val = QtWidgets.QLabel("00"); val.setFont(H(46, QtGui.QFont.Weight.ExtraBold))
        val.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter); val.setStyleSheet(f"color:{INK};")
        val.setFixedWidth(96)
        dn = pill_button("▼", ACCENT, min_h=54, pt=18, radius=18); dn.setFixedWidth(96)
        dn.clicked.connect(lambda: cb(-1))
        cl = QtWidgets.QLabel(cap); cl.setFont(H(12)); cl.setStyleSheet(f"color:{MUTED};")
        cl.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        box.addWidget(up); box.addWidget(val); box.addWidget(dn); box.addWidget(cl)
        parent.addLayout(box)
        return val

    def _adj(self, which, d):
        if which == "h": self.hh = (self.hh + d) % 24
        else: self.mm = (self.mm + d * 5) % 60
        self._update()

    def _set_kind(self, k):
        self.kind = k
        self.b_med.setStyleSheet(self.b_med.styleSheet())
        self.b_med.setStyleSheet(
            f"QPushButton{{background:{ACCENT if k=='med' else '#BFBFBF'};color:white;"
            "border:none;border-radius:26px;padding:8px 22px;}")
        self.b_appt.setStyleSheet(
            f"QPushButton{{background:{ACCENT if k=='appt' else '#BFBFBF'};color:white;"
            "border:none;border-radius:26px;padding:8px 22px;}")

    def _update(self):
        self.h_lbl.setText("%02d" % self.hh); self.m_lbl.setText("%02d" % self.mm)

    def reset(self):
        self.hh, self.mm = 7, 0; self._set_kind("med")
        self.name.setText("Thuốc huyết áp"); self._update()

    def _save(self):
        label = self.name.text().strip() or "Uống thuốc"
        self.app.store.add("%02d:%02d" % (self.hh, self.mm), label, self.kind)
        self.app.navigate("reminders")


# ======================================================================
#  Settings screen
# ======================================================================
class SettingsScreen(SubScreen):
    FS_LEVELS = [1.0, 1.15, 1.35, 1.55]

    def __init__(self, app):
        super().__init__(app, "Cài đặt")
        scroll = QtWidgets.QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}" + SCROLLBAR_QSS)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QtWidgets.QWidget(); body.setStyleSheet("background:transparent;")
        col = QtWidgets.QVBoxLayout(body); col.setContentsMargins(4, 4, 12, 10); col.setSpacing(16)
        col.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)

        # Cỡ chữ
        c = Card(); c.header("font", ACCENT, "Cỡ chữ", "Chọn cỡ chữ dễ đọc nhất")
        self.seg_font = Segmented(["Nhỏ", "Vừa", "Lớn", "Rất lớn"], 1)
        self.seg_font.changed.connect(lambda i: app.set_font_scale(self.FS_LEVELS[i]))
        c.v.addWidget(self.seg_font); col.addWidget(c)

        # Âm lượng loa
        c = Card(); c.header("speaker", "#2E7D32", "Âm lượng loa", "Kéo để tăng hoặc giảm tiếng")
        self.vol = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.vol.setMinimum(20); self.vol.setMaximum(100); self.vol.setPageStep(5)
        self.vol.setStyleSheet(SLIDER_QSS); self.vol.setMinimumHeight(40)
        self.vol.sliderReleased.connect(lambda: app.set_output_gain(self.vol.value() / 100.0))
        vrow = QtWidgets.QHBoxLayout(); vrow.setSpacing(12)
        s1 = QtWidgets.QLabel("Nhỏ"); s1.setFont(H(13)); s1.setStyleSheet(f"color:{MUTED};")
        s2 = QtWidgets.QLabel("To"); s2.setFont(H(13)); s2.setStyleSheet(f"color:{MUTED};")
        vrow.addWidget(s1); vrow.addWidget(self.vol, 1); vrow.addWidget(s2)
        c.v.addLayout(vrow); col.addWidget(c)

        # Màn hình
        c = Card(); c.header("screen", "#1F9E8A", "Màn hình", "Hướng hiển thị")
        self.seg_rot = Segmented(["Dọc", "Ngang"], 0)
        self.seg_rot.changed.connect(lambda i: app.set_rotation(0 if i == 0 else 90))
        c.v.addWidget(self.seg_rot)
        r90 = pill_button("Xoay 90°", GREEN_OK, min_h=54, pt=15, radius=15)
        r90.clicked.connect(app.rotate_90); c.v.addWidget(r90); col.addWidget(c)

        # WiFi & Mạng
        c = Card(); c.header("wifi", "#2F6FE6", "WiFi & Mạng", "Kết nối mạng và xem địa chỉ IP")
        self.net_ssid = QtWidgets.QLabel("—"); self.net_ssid.setFont(H(15, QtGui.QFont.Weight.DemiBold))
        self.net_ssid.setStyleSheet(f"color:{INK};"); self.net_ssid.setWordWrap(True)
        self.net_ip = QtWidgets.QLabel("—"); self.net_ip.setFont(H(14)); self.net_ip.setStyleSheet(f"color:{MUTED};")
        c.v.addWidget(self.net_ssid); c.v.addWidget(self.net_ip)
        wbtn = pill_button("Kết nối / đổi WiFi", "#2F6FE6", min_h=56, pt=16, radius=16)
        wbtn.clicked.connect(lambda: app.navigate("wifi")); c.v.addWidget(wbtn); col.addWidget(c)

        # Gọi khẩn cấp
        c = Card(); c.header("phone", EMERGENCY, "Gọi khẩn cấp", "Số gọi khi cần trợ giúp")
        self.em_lbl = QtWidgets.QLabel("115"); self.em_lbl.setFont(H(30, QtGui.QFont.Weight.ExtraBold))
        self.em_lbl.setStyleSheet(f"color:{EMERGENCY};")
        c.v.addWidget(self.em_lbl); col.addWidget(c)

        # Thông tin
        c = Card(); c.header("info", MUTED, "Thông tin thiết bị")
        try:
            import socket; host = socket.gethostname()
        except Exception:
            host = "-"
        self.info_lbl = QtWidgets.QLabel("Elder Care · Ngân\nThiết bị: %s" % host)
        self.info_lbl.setFont(H(13)); self.info_lbl.setWordWrap(True); self.info_lbl.setStyleSheet(f"color:{MUTED};")
        c.v.addWidget(self.info_lbl); col.addWidget(c)

        scroll.setWidget(body)
        self.root.addWidget(scroll, 1)

    def refresh(self):
        cur = min(range(len(self.FS_LEVELS)), key=lambda i: abs(self.FS_LEVELS[i] - _FS))
        self.seg_font.set_current(cur)
        g = float(self.app.cfg.get("audio", {}).get("output_gain", 0.6))
        self.vol.blockSignals(True); self.vol.setValue(int(round(g * 100))); self.vol.blockSignals(False)
        rot = int(self.app.cfg.get("ui", {}).get("rotation", 0))
        self.seg_rot.set_current(1 if rot in (90, 270) else 0)
        self.em_lbl.setText(self.app.cfg.get("emergency", {}).get("number", "115"))
        try:
            from . import net
            st = net.current()
            self.net_ssid.setText("Đang dùng:  %s" % (st.get("ssid") or "(chưa kết nối)"))
            self.net_ip.setText("Địa chỉ IP:  %s" % st["ip"])
        except Exception:
            self.net_ssid.setText("Đang dùng:  (không rõ)"); self.net_ip.setText("")


# ======================================================================
#  On-screen keyboard (touch) — for WiFi password without a hardware keyboard
# ======================================================================
class OnScreenKeyboard(QtWidgets.QWidget):
    ROWS_ABC = ["1234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm"]
    ROWS_SYM = ["1234567890", "@#$%&*-_=+", "()[]{}/\\|~", "!?.,:;'\"`"]

    def __init__(self, target):
        super().__init__()
        self.target = target
        self._shift = False
        self._sym = False
        self.setStyleSheet("background:transparent;")
        self.box = QtWidgets.QVBoxLayout(self)
        self.box.setSpacing(6); self.box.setContentsMargins(0, 8, 0, 0)
        self._render()

    def _mk(self, label, cb, color="#FFFFFF"):
        b = QtWidgets.QPushButton(label)
        b.setMinimumHeight(50)
        b.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        b.setFont(H(16, QtGui.QFont.Weight.DemiBold))
        b.setStyleSheet(f"QPushButton{{background:{color};border:1px solid #E6CBB9;border-radius:9px;"
                        f"color:{INK};}}QPushButton:pressed{{background:#F0D8C8;}}")
        b.clicked.connect(cb)
        return b

    def _clear(self):
        while self.box.count():
            it = self.box.takeAt(0)
            lay = it.layout()
            if lay:
                while lay.count():
                    w = lay.takeAt(0).widget()
                    if w:
                        w.setParent(None)
            elif it.widget():
                it.widget().setParent(None)

    def _render(self):
        self._clear()
        rows = self.ROWS_SYM if self._sym else self.ROWS_ABC
        for r in rows:
            h = QtWidgets.QHBoxLayout(); h.setSpacing(5)
            for ch in r:
                lab = ch.upper() if (self._shift and not self._sym and ch.isalpha()) else ch
                h.addWidget(self._mk(lab, (lambda _=None, c=ch: self._type(c))))
            self.box.addLayout(h)
        h = QtWidgets.QHBoxLayout(); h.setSpacing(5)
        h.addWidget(self._mk("HOA" if not self._shift else "hoa", self._toggle_shift,
                             color=("#F0D8C8" if self._shift else "#FFF3E8")))
        h.addWidget(self._mk("abc" if self._sym else "#@1", self._toggle_sym, color="#FFF3E8"))
        sp = self._mk("Dấu cách", lambda: self._insert(" "), color="#FFFFFF")
        sp.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        h.addWidget(sp, 3)
        h.addWidget(self._mk("Xoá", self._bksp, color="#F5E0D4"))
        self.box.addLayout(h)

    def _insert(self, s):
        if self.target is not None:
            self.target.insert(s)

    def _type(self, ch):
        c = ch.upper() if (self._shift and not self._sym and ch.isalpha()) else ch
        self._insert(c)
        if self._shift and not self._sym and ch.isalpha():
            self._shift = False
            self._render()

    def _bksp(self):
        if self.target is not None:
            self.target.backspace()

    def _toggle_shift(self):
        self._shift = not self._shift; self._render()

    def _toggle_sym(self):
        self._sym = not self._sym; self._render()


# ======================================================================
#  WiFi screen — connect to Wi-Fi + show IP (usable inside the kiosk)
# ======================================================================
class _WifiSignals(QtCore.QObject):
    scanned = QtCore.pyqtSignal(object)
    connected = QtCore.pyqtSignal(bool, str)


class WiFiScreen(SubScreen):
    def __init__(self, app):
        super().__init__(app, "Mạng WiFi")
        from . import net
        self._net = net
        self._busy = False
        self.sig = _WifiSignals()
        self.sig.scanned.connect(self._on_scanned)
        self.sig.connected.connect(self._on_connected)

        stcard = Card(); stcard.header("wifi", "#2F6FE6", "Trạng thái mạng")
        self.status = QtWidgets.QLabel("")
        self.status.setFont(H(15, QtGui.QFont.Weight.DemiBold)); self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color:{INK};")
        stcard.v.addWidget(self.status)
        self.root.addWidget(stcard)

        self.btn_scan = pill_button("Quét lại mạng WiFi", ACCENT, min_h=58, pt=17, radius=18)
        self.btn_scan.clicked.connect(self.do_scan)
        self.root.addWidget(self.btn_scan)

        # ── list of networks ──
        self.listwrap = QtWidgets.QWidget(); self.listwrap.setStyleSheet("background:transparent;")
        lw = QtWidgets.QVBoxLayout(self.listwrap); lw.setContentsMargins(0, 0, 0, 0); lw.setSpacing(8)
        self.scroll = QtWidgets.QScrollArea(); self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.scroll.setStyleSheet("background:transparent;")
        self.holder = QtWidgets.QWidget(); self.holder.setStyleSheet("background:transparent;")
        self.vlist = QtWidgets.QVBoxLayout(self.holder); self.vlist.setSpacing(8)
        self.vlist.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(self.holder)
        lw.addWidget(self.scroll, 1)
        self.root.addWidget(self.listwrap, 1)

        # ── connect panel (hidden until a network is picked) ──
        self.panel = Glass(radius=20, alpha=225)
        pv = QtWidgets.QVBoxLayout(self.panel); pv.setContentsMargins(14, 12, 14, 12); pv.setSpacing(8)
        self.p_title = QtWidgets.QLabel(""); self.p_title.setFont(H(18, QtGui.QFont.Weight.ExtraBold))
        self.p_title.setStyleSheet(f"color:{ACCENT_DARK};"); self.p_title.setWordWrap(True)
        pv.addWidget(self.p_title)
        self.pw = QtWidgets.QLineEdit(); self.pw.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.pw.setPlaceholderText("Nhập mật khẩu WiFi"); self.pw.setFont(H(18))
        self.pw.setMinimumHeight(54)
        self.pw.setStyleSheet("QLineEdit{background:#FFFFFF;border:2px solid #E6CBB9;border-radius:10px;"
                              f"padding:6px 10px;color:{INK};}}")
        pv.addWidget(self.pw)
        showrow = QtWidgets.QHBoxLayout()
        self.show_pw = QtWidgets.QCheckBox("Hiện mật khẩu"); self.show_pw.setFont(H(14))
        self.show_pw.setStyleSheet(f"color:{MUTED};")
        self.show_pw.toggled.connect(lambda on: self.pw.setEchoMode(
            QtWidgets.QLineEdit.EchoMode.Normal if on else QtWidgets.QLineEdit.EchoMode.Password))
        showrow.addWidget(self.show_pw); showrow.addStretch()
        pv.addLayout(showrow)
        self.kbd = OnScreenKeyboard(self.pw)
        pv.addWidget(self.kbd)
        self.p_msg = QtWidgets.QLabel(""); self.p_msg.setFont(H(15, QtGui.QFont.Weight.DemiBold))
        self.p_msg.setWordWrap(True); self.p_msg.setStyleSheet(f"color:{INK};")
        pv.addWidget(self.p_msg)
        brow = QtWidgets.QHBoxLayout(); brow.setSpacing(10)
        back = pill_button("Quay lại", MUTED, min_h=60, pt=17); back.clicked.connect(self._show_list)
        self.btn_conn = pill_button("Kết nối", GREEN_OK, min_h=60, pt=18); self.btn_conn.clicked.connect(self.do_connect)
        brow.addWidget(back); brow.addWidget(self.btn_conn, 1)
        pv.addLayout(brow)
        self.root.addWidget(self.panel, 2)
        self.panel.setVisible(False)

    # ---------- lifecycle ----------
    def refresh(self):
        self._update_status()
        self._show_list()
        self.do_scan()

    def _update_status(self):
        try:
            st = self._net.current()
        except Exception:
            st = {"ip": "(không rõ)", "ssid": ""}
        ss = st.get("ssid") or "(chưa kết nối)"
        self.status.setText("Địa chỉ IP:  %s\nĐang kết nối:  %s" % (st.get("ip", "-"), ss))

    def _show_list(self):
        self.panel.setVisible(False)
        self.listwrap.setVisible(True)
        self.btn_scan.setVisible(True)

    def _clear_list(self):
        while self.vlist.count():
            it = self.vlist.takeAt(0)
            if it.widget():
                it.widget().setParent(None)

    # ---------- scan ----------
    def do_scan(self):
        if self._busy:
            return
        self._busy = True
        self.btn_scan.setText("Đang quét...")
        self._clear_list()
        info = QtWidgets.QLabel("Đang tìm mạng WiFi xung quanh..."); info.setFont(H(15))
        info.setStyleSheet(f"color:{MUTED};"); info.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.vlist.addWidget(info)
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        try:
            nets = self._net.scan()
        except Exception:
            nets = []
        self.sig.scanned.emit(nets)

    def _on_scanned(self, nets):
        self._busy = False
        self.btn_scan.setText("Quét lại mạng WiFi")
        self._update_status()
        self._clear_list()
        if not nets:
            empty = QtWidgets.QLabel("Không tìm thấy mạng nào.\nThử bấm “Quét lại”.")
            empty.setFont(H(16)); empty.setStyleSheet(f"color:{MUTED};")
            empty.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self.vlist.addWidget(empty); return
        for n in nets[:20]:
            self.vlist.addWidget(self._net_btn(n))

    def _net_btn(self, n):
        sig = n.get("signal", 0)
        level = "sóng mạnh" if sig >= 67 else ("sóng trung bình" if sig >= 40 else "sóng yếu")
        secured = self._net.is_secured(n.get("security"))
        lock = "khoá" if secured else "mở"
        b = QtWidgets.QPushButton("%s\n%s · %s" % (n["ssid"], level, lock))
        b.setMinimumHeight(74); b.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        b.setFont(H(16, QtGui.QFont.Weight.DemiBold))
        b.setStyleSheet(
            "QPushButton{background:white;color:%s;border:1px solid #EAD3C2;border-radius:16px;"
            "padding:8px 18px;text-align:left;}QPushButton:pressed{background:#FBEFE7;}" % INK)
        b.clicked.connect(lambda _=None, net=n: self._pick(net))
        return b

    # ---------- connect ----------
    def _pick(self, n):
        self._sel = n
        secured = self._net.is_secured(n.get("security"))
        self.p_title.setText("Kết nối tới:  %s" % n["ssid"])
        self.p_msg.setText("")
        self.pw.setText("")
        self.pw.setVisible(secured)
        self.show_pw.setVisible(secured)
        self.kbd.setVisible(secured)
        self.listwrap.setVisible(False)
        self.btn_scan.setVisible(False)
        self.panel.setVisible(True)

    def do_connect(self):
        if self._busy or not getattr(self, "_sel", None):
            return
        secured = self._net.is_secured(self._sel.get("security"))
        pwd = self.pw.text()
        if secured and not pwd:
            self.p_msg.setText("Bà nhập mật khẩu WiFi rồi bấm Kết nối nhé.")
            return
        self._busy = True
        self.btn_conn.setText("Đang kết nối...")
        self.p_msg.setText("Đang kết nối, bà chờ một chút...")
        ssid = self._sel["ssid"]
        threading.Thread(target=self._connect_worker, args=(ssid, pwd), daemon=True).start()

    def _connect_worker(self, ssid, pwd):
        try:
            ok, msg = self._net.connect(ssid, pwd)
        except Exception as e:
            ok, msg = False, str(e)
        self.sig.connected.emit(ok, msg)

    def _on_connected(self, ok, msg):
        self._busy = False
        self.btn_conn.setText("Kết nối")
        self._update_status()
        if ok:
            self.p_msg.setText("✔  " + msg + ".  Đã lưu mạng này.")
            QtCore.QTimer.singleShot(1400, self._show_list)
        else:
            self.p_msg.setText("Chưa kết nối được: " + msg)


# ======================================================================
#  Fullscreen medication alarm
# ======================================================================
class AlarmOverlay(QtWidgets.QWidget):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.setStyleSheet("background:#FFF3E0;")
        self.setVisible(False)
        v = QtWidgets.QVBoxLayout(self); v.setContentsMargins(30, 30, 30, 30); v.setSpacing(16)
        v.addStretch()
        self.icon = CircleButton("bell", ACCENT, 120); self.icon.setEnabled(False)
        v.addWidget(self.icon, 0, QtCore.Qt.AlignmentFlag.AlignHCenter)
        self.title = QtWidgets.QLabel("ĐẾN GIỜ UỐNG THUỐC")
        self.title.setFont(H(28, QtGui.QFont.Weight.ExtraBold)); self.title.setStyleSheet(f"color:{ACCENT_DARK};")
        self.title.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter); self.title.setWordWrap(True)
        v.addWidget(self.title)
        self.name = QtWidgets.QLabel("—")
        self.name.setFont(H(34, QtGui.QFont.Weight.ExtraBold)); self.name.setStyleSheet(f"color:{INK};")
        self.name.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter); self.name.setWordWrap(True)
        v.addWidget(self.name)
        self.tm = QtWidgets.QLabel("")
        self.tm.setFont(H(20)); self.tm.setStyleSheet(f"color:{MUTED};")
        self.tm.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.tm)
        v.addStretch()
        done = pill_button("✓  Đã uống", GREEN_OK, min_h=90, pt=24); done.clicked.connect(self._done)
        snooze = pill_button("Nhắc lại sau 10 phút", ACCENT, min_h=78, pt=20); snooze.clicked.connect(self._snooze)
        v.addWidget(done); v.addWidget(snooze)
        self._current = None

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        grad = QtGui.QLinearGradient(0, 0, 0, self.height())
        grad.setColorAt(0.0, QtGui.QColor("#FFE0B2"))
        grad.setColorAt(1.0, QtGui.QColor("#FFF3E0"))
        p.fillRect(self.rect(), grad)
        p.end()

    def show_for(self, r):
        self._current = r
        self.title.setText("ĐẾN GIỜ UỐNG THUỐC" if r.kind == "med" else "ĐẾN GIỜ HẸN")
        self.name.setText(r.label); self.tm.setText("Lúc " + r.time)
        self.setGeometry(self.app.rect()); self.setVisible(True); self.raise_()

    def _done(self):
        self.setVisible(False)

    def _snooze(self):
        if self._current:
            self.app.snooze(self._current, 600)
        self.setVisible(False)


# ======================================================================
#  Root app window
# ======================================================================
class MainWindow(QtWidgets.QWidget):
    def __init__(self, cfg, start_engine=True, preview_state=None, preview_screen=None):
        super().__init__()
        global _FS
        _FS = float(cfg["ui"].get("font_scale", 1.15))
        self.cfg = cfg
        self.setWindowTitle("PTalk Signature — Elder Care")
        self._stars = _make_stars()
        self._elapsed = QtCore.QElapsedTimer(); self._elapsed.start()
        self.store = rem.ReminderStore()
        self._fired = set(); self._snoozes = []

        self._bridge = QtCore.QObject()
        self._sig = _EventSignal(); self._sig.event.connect(self._on_event)
        self.engine = VoiceEngine(cfg, on_event=lambda e: self._sig.event.emit(e))
        if start_engine:
            self.engine.start()

        self.stack = QtWidgets.QStackedWidget(self)
        self.stack.setStyleSheet("background:transparent;")
        self.home = HomeScreen(self)
        self.med = MedicineScreen(self)
        self.reminders = RemindersScreen(self)
        self.addrem = AddReminderScreen(self)
        self.settings = SettingsScreen(self)
        self.wifi = WiFiScreen(self)
        for s in (self.home, self.med, self.reminders, self.addrem, self.settings, self.wifi):
            s.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
            self.stack.addWidget(s)
        self.alarm = AlarmOverlay(self)

        lay = QtWidgets.QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.stack)

        self._timer = QtCore.QTimer(self); self._timer.timeout.connect(self._tick); self._timer.start(33)
        self._sched = QtCore.QTimer(self); self._sched.timeout.connect(self._check_reminders); self._sched.start(15000)

        if start_engine:
            _apply_rotation(self._out(), int(self.cfg["ui"].get("rotation", 0)))

        if preview_state:
            self.home._set_state(preview_state)
        if preview_screen:
            self.navigate(preview_screen)

    # ---------- navigation ----------
    def navigate(self, name):
        target = {"home": self.home, "medicine": self.med, "reminders": self.reminders,
                  "add": self.addrem, "settings": self.settings, "wifi": self.wifi}.get(name, self.home)
        if name == "reminders": self.reminders.refresh()
        if name == "add": self.addrem.reset()
        if name == "settings": self.settings.refresh()
        if name == "wifi": self.wifi.refresh()
        if name == "home": self.home.info.set_state(self.home.state)
        self.stack.setCurrentWidget(target)

    # ---------- background ----------
    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        grad = QtGui.QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0.0, QtGui.QColor(ELDER_GRAD[0]))
        grad.setColorAt(0.5, QtGui.QColor(ELDER_GRAD[1]))
        grad.setColorAt(1.0, QtGui.QColor(ELDER_GRAD[2]))
        p.fillRect(self.rect(), grad)
        frame = self._elapsed.elapsed() / 1000.0 * 60.0
        for i, s in enumerate(self._stars):
            px = (s["x"] + s["sx"] * frame) % 1.0; py = (s["y"] + s["sy"] * frame) % 1.0
            tw = (math.sin(s["phase"] + frame * s["tw"]) + 1) / 2
            a = int(max(0, min(255, s["alpha"] * (0.4 + 0.6 * tw))))
            col = QtGui.QColor(STAR_COLORS[i % len(STAR_COLORS)]); col.setAlpha(a)
            p.setPen(QtCore.Qt.PenStyle.NoPen); p.setBrush(col)
            p.drawPath(_star_path(px * w, py * h, s["size"]))
        p.end()

    def _tick(self):
        QtWidgets.QApplication.instance().setProperty("anim_t", self._elapsed.elapsed() / 1000.0)
        self.home.tick_clock()
        self.update()
        if self.stack.currentWidget() is self.home:
            self.home.stage.update()

    def resizeEvent(self, e):
        self.alarm.setGeometry(self.rect())
        super().resizeEvent(e)

    # ---------- voice / medicine events ----------
    def _on_event(self, ev):
        if ev.startswith("MED_STATUS:"):
            self.home.set_status(ev[len("MED_STATUS:"):]); return
        if ev.startswith("MED_RESULT:"):
            self.med.set_result(ev[len("MED_RESULT:"):]); self.navigate("medicine")
            self.home.a_med.btn.setEnabled(True); self.home._set_state("idle"); return
        if ev.startswith("CHAT_T:"):
            self.home.on_transcript(ev[len("CHAT_T:"):]); return
        if ev.startswith("CHAT_A:"):
            self.home.on_answer(ev[len("CHAT_A:"):]); return
        self.home.apply_voice_event(ev)

    def scan_medicine(self):
        self.home.a_med.btn.setEnabled(False)
        self.home.set_status("Đang chụp ảnh thuốc...")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        try:
            path = "/tmp/ptalk_medicine.jpg"
            medicine.capture_jpeg(path, self.cfg["camera"]["rotation"])
            self._sig.event.emit("MED_STATUS:Đang phân tích thuốc...")
            txt = medicine.analyze_medicine(path, self.cfg["server"]["aitools_url"])
            self._sig.event.emit("MED_RESULT:" + (txt or "Không nhận diện được thuốc."))
        except Exception as e:
            self._sig.event.emit("MED_RESULT:Lỗi khi quét thuốc: %s" % e)

    def emergency_call(self):
        num = self.cfg.get("emergency", {}).get("number", "115")
        tts.speak("Đang gọi khẩn cấp")
        box = QtWidgets.QMessageBox(self)
        box.setStyleSheet("QLabel{font-size:22px;} QPushButton{font-size:18px;padding:10px 24px;}")
        box.setWindowTitle("Gọi khẩn cấp")
        box.setText(f"Đang gọi số khẩn cấp:\n\n{num}")
        box.exec()

    # ---------- reminders ----------
    def _check_reminders(self):
        now = time.time()
        for r in self.store.due_now(now):
            key = r.id + time.strftime("@%Y%m%d%H%M", time.localtime(now))
            if key in self._fired:
                continue
            self._fired.add(key); self._fire(r)
        for item in list(self._snoozes):
            if now >= item[0]:
                self._snoozes.remove(item); self._fire(item[1])

    def _fire(self, r):
        tts.chime()
        msg = ("Đã đến giờ uống thuốc. " + r.label) if r.kind == "med" else ("Đã đến giờ hẹn. " + r.label)
        tts.speak(msg)
        self.alarm.show_for(r)

    def snooze(self, r, seconds):
        self._snoozes.append((time.time() + seconds, r))

    # ---------- settings actions ----------
    def change_font(self, delta):
        global _FS
        _FS = max(0.9, min(1.8, round(_FS + delta, 2)))
        self.cfg.save_user("ui", {"font_scale": _FS})
        self._rebuild_screens()
        self.navigate("settings")

    def set_font_scale(self, v):
        global _FS
        _FS = max(0.9, min(1.8, round(float(v), 2)))
        self.cfg.save_user("ui", {"font_scale": _FS})
        self._rebuild_screens()
        self.navigate("settings")

    def set_output_gain(self, g):
        g = max(0.2, min(1.0, round(float(g), 2)))
        self.cfg.save_user("audio", {"output_gain": g})
        try:
            self.engine.set_output_gain(g)
        except Exception:
            pass

    def _out(self):
        return self.cfg.get("display", {}).get("output", "DSI-2")

    def rotate_90(self):
        """Xoay màn hình thêm 90° mỗi lần bấm: 0 → 90 → 180 → 270 → 0."""
        deg = (int(self.cfg["ui"].get("rotation", 0)) + 90) % 360
        self.cfg.save_user("ui", {"rotation": deg})
        _apply_rotation(self._out(), deg)

    def set_rotation(self, deg):
        deg = int(deg) % 360
        self.cfg.save_user("ui", {"rotation": deg})
        _apply_rotation(self._out(), deg)

    def _rebuild_screens(self):
        cur = self.stack.currentIndex()
        old = [self.home, self.med, self.reminders, self.addrem, self.settings, self.wifi]
        self.home = HomeScreen(self); self.med = MedicineScreen(self)
        self.reminders = RemindersScreen(self); self.addrem = AddReminderScreen(self)
        self.settings = SettingsScreen(self); self.wifi = WiFiScreen(self)
        for s in (self.home, self.med, self.reminders, self.addrem, self.settings, self.wifi):
            s.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
            self.stack.addWidget(s)
        for s in old:
            self.stack.removeWidget(s); s.setParent(None)
        self.stack.setCurrentIndex(min(cur, self.stack.count() - 1))

    def closeEvent(self, e):
        try: self.engine.shutdown()
        except Exception: pass
        super().closeEvent(e)


class _EventSignal(QtCore.QObject):
    event = QtCore.pyqtSignal(str)


_TRANSFORMS = {0: "normal", 90: "90", 180: "180", 270: "270"}


def _apply_rotation(output, deg):
    """Rotate the physical DSI output via wlr-randr (labwc/cage/wlroots)."""
    transform = _TRANSFORMS.get(int(deg) % 360, "normal")
    try:
        subprocess.run(["wlr-randr", "--output", output, "--transform", transform],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    except Exception:
        pass
