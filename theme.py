"""
Site Deploy — visual theme.

Fluent-flavoured look for Windows 11: system accent colour, light/dark
following the OS, rounded controls, generous spacing, flat line icons
drawn in code (nothing to ship, nothing to lose), and a dark title bar.

Kept separate from main.py so the deploy engine stays readable.
"""

import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt, QRectF, QPointF, QSize
from PySide6.QtGui import (
    QColor, QFont, QFontDatabase, QIcon, QPainter, QPainterPath, QPen, QPixmap,
)

# ── palette ──────────────────────────────────────────────────────────────

FALLBACK_ACCENT = "#3E6FD1"

# Muted status colours (feedback: avoid highly saturated)
OK_LIGHT, OK_DARK = "#1F7A45", "#4CC38A"
WARN_LIGHT, WARN_DARK = "#9A6700", "#D9A62E"
ERR_LIGHT, ERR_DARK = "#B3261E", "#E8736A"

TRAY_IDLE = "#3E6FD1"
TRAY_BUSY = "#C08A2E"
TRAY_ERROR = "#C0504D"


def is_dark() -> bool:
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        with key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except Exception:
        return False


def system_accent() -> str:
    """Windows stores the accent as ABGR under HKCU\\...\\DWM\\AccentColor."""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM")
        with key:
            abgr = winreg.QueryValueEx(key, "AccentColor")[0]
        r, g, b = abgr & 0xFF, (abgr >> 8) & 0xFF, (abgr >> 16) & 0xFF
        c = QColor(r, g, b)
        # Keep it readable as a button fill: clamp very light/dark accents.
        h, s, v, _ = c.getHsv()
        v = max(120, min(v, 215))
        s = min(s, 200)
        c.setHsv(h, s, v)
        return c.name()
    except Exception:
        return FALLBACK_ACCENT


class Palette:
    def __init__(self, mode: str = "light"):
        # "light" / "dark" / "auto" (auto = follow Windows). Default light:
        # this app is a background utility, and a bright window reads as
        # calmer and easier to follow than a dark one.
        self.mode = mode
        self.dark = is_dark() if mode == "auto" else (mode == "dark")
        self.accent = system_accent()
        a = QColor(self.accent)
        self.accent_hover = a.lighter(115).name() if self.dark else a.darker(108).name()
        self.accent_press = a.darker(115).name()
        if self.dark:
            self.window = "#1F1F1F"
            self.card = "#282828"
            self.card_alt = "#303030"
            self.border = "#3A3A3A"
            self.text = "#F2F2F2"
            self.text_dim = "#A8A8A8"
            self.hover = "#333333"
            self.selected = "#3A3A3A"
            self.field = "#2E2E2E"
            self.ok, self.warn, self.err = OK_DARK, WARN_DARK, ERR_DARK
            self.on_accent = "#FFFFFF"
        else:
            self.window = "#F7F9FC"
            self.card = "#FFFFFF"
            self.card_alt = "#F4F7FB"
            self.border = "#E4E9F0"
            self.text = "#16202B"
            self.text_dim = "#5C6976"
            self.hover = "#EEF3F9"
            self.selected = "#E4ECF6"
            self.field = "#FFFFFF"
            self.ok, self.warn, self.err = OK_LIGHT, WARN_LIGHT, ERR_LIGHT
            self.on_accent = "#FFFFFF"


PALETTE = Palette("light")


def set_mode(mode: str):
    """Re-theme in place so every module holding PALETTE sees the change."""
    PALETTE.__init__(mode)


def ui_font() -> QFont:
    families = QFontDatabase.families()
    for name in ("Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI"):
        if name in families:
            f = QFont(name, 10)
            f.setHintingPreference(QFont.PreferNoHinting)
            return f
    return QFont("", 10)


# ── generated assets (QSS can't draw, so a couple of glyphs go to disk) ──

def _asset(name: str, painter_fn, size: int = 32) -> str:
    """Render a tiny PNG once and return a QSS-safe url() path."""
    folder = Path(tempfile.gettempdir()) / "SiteDeployAssets"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    painter_fn(p)
    p.end()
    pm.save(str(path), "PNG")
    return str(path).replace("\\", "/")


def _checkmark(color: str) -> str:
    def draw(p):
        _pen(p, color, 4.0)
        p.drawLine(7, 17, 13, 23)
        p.drawLine(13, 23, 25, 9)
    tag = color.lstrip("#")
    return _asset(f"check_{tag}.png", draw)


# ── stylesheet ───────────────────────────────────────────────────────────

def stylesheet(p: Palette = PALETTE) -> str:
    return f"""
    QWidget {{
        color: {p.text};
        background: transparent;
    }}
    QMainWindow, QDialog {{
        background: {p.window};
    }}

    /* Cards replace group boxes: whitespace and a soft edge, no heavy border */
    QFrame#Card {{
        background: {p.card};
        border: 1px solid {p.border};
        border-radius: 10px;
    }}
    QFrame#Tile {{
        background: {p.card_alt};
        border: 1px solid {p.border};
        border-radius: 8px;
    }}
    QFrame#Sidebar {{
        background: {p.card};
        border: 1px solid {p.border};
        border-radius: 10px;
    }}

    QLabel#H1 {{ font-size: 17px; font-weight: 600; }}
    QLabel#H2 {{ font-size: 13px; font-weight: 600; }}
    QLabel#Dim {{ color: {p.text_dim}; }}
    QLabel#TileValue {{ font-size: 15px; font-weight: 600; }}
    QLabel#TileLabel {{ color: {p.text_dim}; font-size: 11px; }}

    /* Buttons — one filled primary, everything else quiet */
    QPushButton {{
        background: transparent;
        border: 1px solid {p.border};
        border-radius: 6px;
        padding: 7px 14px;
        color: {p.text};
    }}
    QPushButton:hover  {{ background: {p.hover}; }}
    QPushButton:pressed {{ background: {p.selected}; }}
    QPushButton:disabled {{ color: {p.text_dim}; border-color: {p.border}; }}

    QPushButton#Primary {{
        background: {p.accent};
        border: 1px solid {p.accent};
        color: {p.on_accent};
        font-weight: 600;
        padding: 8px 18px;
    }}
    QPushButton#Primary:hover   {{ background: {p.accent_hover}; border-color: {p.accent_hover}; }}
    QPushButton#Primary:pressed {{ background: {p.accent_press}; border-color: {p.accent_press}; }}
    QPushButton#Primary:disabled {{
        background: {p.card_alt}; border-color: {p.border}; color: {p.text_dim};
    }}

    QPushButton#Quiet {{
        border: none;
        padding: 7px 10px;
        text-align: left;
        color: {p.text};
    }}
    QPushButton#Quiet:hover {{ background: {p.hover}; border-radius: 6px; }}

    QPushButton#Link {{
        border: none; padding: 4px 6px; color: {p.accent}; text-align: left;
    }}
    QPushButton#Link:hover {{ text-decoration: underline; }}

    /* Sidebar site list — rounded selection, accent when active */
    QListWidget {{
        background: transparent;
        border: none;
        outline: 0;
    }}
    QListWidget::item {{
        padding: 9px 10px;
        border-radius: 6px;
        margin: 2px 0px;
    }}
    QListWidget::item:hover {{ background: {p.hover}; }}
    QListWidget::item:selected {{
        background: {p.accent};
        color: {p.on_accent};
    }}

    QListWidget#Timeline::item {{ padding: 2px; margin: 0px; border-radius: 6px; }}
    QListWidget#Timeline::item:selected {{ background: {p.hover}; color: {p.text}; }}

    /* Fields */
    QLineEdit, QSpinBox, QComboBox, QPlainTextEdit {{
        background: {p.field};
        border: 1px solid {p.border};
        border-radius: 6px;
        padding: 7px 9px;
        selection-background-color: {p.accent};
        selection-color: {p.on_accent};
    }}
    QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {{
        border: 1px solid {p.accent};
    }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox QAbstractItemView {{
        background: {p.card};
        border: 1px solid {p.border};
        border-radius: 8px;
        padding: 4px;
        selection-background-color: {p.accent};
        selection-color: {p.on_accent};
    }}

    QCheckBox {{ spacing: 8px; padding: 3px 0px; }}
    QCheckBox::indicator {{
        width: 17px; height: 17px;
        border: 1px solid {p.text_dim};
        border-radius: 4px;
        background: {p.field};
    }}
    QCheckBox::indicator:hover {{ border-color: {p.accent}; }}
    QCheckBox::indicator:checked {{
        background: {p.accent};
        border-color: {p.accent};
        image: url({_checkmark(p.on_accent)});
    }}

    /* Smooth, unsegmented progress (Fluent-style) */
    QProgressBar {{
        background: {p.card_alt};
        border: none;
        border-radius: 3px;
        height: 6px;
        text-align: center;
        color: transparent;
    }}
    QProgressBar::chunk {{
        background: {p.accent};
        border-radius: 3px;
    }}

    QMenu {{
        background: {p.card};
        border: 1px solid {p.border};
        border-radius: 8px;
        padding: 6px;
    }}
    QMenu::item {{ padding: 7px 22px 7px 12px; border-radius: 5px; }}
    QMenu::item:selected {{ background: {p.hover}; }}
    QMenu::separator {{ height: 1px; background: {p.border}; margin: 5px 8px; }}

    QScrollBar:vertical {{
        background: transparent; width: 10px; margin: 2px;
    }}
    QScrollBar::handle:vertical {{
        background: {p.border}; border-radius: 5px; min-height: 24px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {p.text_dim}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0px; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    QToolTip {{
        background: {p.card};
        color: {p.text};
        border: 1px solid {p.border};
        border-radius: 6px;
        padding: 6px 8px;
    }}
    """


# ── Fluent-ish line icons, drawn in code ─────────────────────────────────

def _pen(painter, color, width=6.0):
    pen = QPen(QColor(color), width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)


def line_icon(name: str, color: str = None, size: int = 64) -> QIcon:
    """Flat single-stroke icons on a 64×64 grid, scaled to `size`."""
    color = color or PALETTE.text
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    _pen(p, color)

    if name == "folder":
        path = QPainterPath()
        path.moveTo(8, 46)
        path.lineTo(8, 18)
        path.lineTo(26, 18)
        path.lineTo(31, 25)
        path.lineTo(56, 25)
        path.lineTo(56, 46)
        path.closeSubpath()
        p.drawPath(path)
    elif name == "globe":
        p.drawEllipse(QRectF(9, 9, 46, 46))
        p.drawLine(9, 32, 55, 32)
        p.drawArc(QRectF(20, 9, 24, 46), 0, 360 * 16)
    elif name == "settings":
        import math
        _pen(p, color, 5.0)
        p.drawEllipse(QRectF(18, 18, 28, 28))     # gear body
        p.drawEllipse(QRectF(27, 27, 10, 10))     # hub
        for i in range(8):
            a = math.radians(i * 45)
            p.drawLine(QPointF(32 + 13 * math.cos(a), 32 + 13 * math.sin(a)),
                       QPointF(32 + 22 * math.cos(a), 32 + 22 * math.sin(a)))
    elif name == "history":
        p.drawArc(QRectF(11, 11, 42, 42), 60 * 16, 300 * 16)
        p.drawLine(32, 20, 32, 33)
        p.drawLine(32, 33, 42, 39)
        p.drawLine(11, 22, 11, 12)
        p.drawLine(11, 22, 21, 22)
    elif name == "check":
        p.drawLine(14, 33, 27, 45)
        p.drawLine(27, 45, 50, 19)
    elif name == "warning":
        path = QPainterPath()
        path.moveTo(32, 12)
        path.lineTo(56, 52)
        path.lineTo(8, 52)
        path.closeSubpath()
        p.drawPath(path)
        p.drawLine(32, 27, 32, 38)
        p.drawPoint(32, 45)
        _pen(p, color, 6.5)
        p.drawLine(32, 45, 32, 45)
    elif name == "upload":
        p.drawLine(32, 46, 32, 17)
        p.drawLine(32, 16, 20, 28)
        p.drawLine(32, 16, 44, 28)
        p.drawLine(12, 52, 52, 52)
    elif name == "plus":
        p.drawLine(32, 15, 32, 49)
        p.drawLine(15, 32, 49, 32)
    elif name == "pencil":
        p.drawLine(16, 48, 44, 20)
        p.drawLine(44, 20, 50, 26)
        p.drawLine(50, 26, 22, 54)
        p.drawLine(13, 51, 20, 55)
    elif name == "trash":
        p.drawLine(13, 20, 51, 20)
        p.drawLine(26, 20, 26, 13)
        p.drawLine(26, 13, 38, 13)
        p.drawLine(38, 13, 38, 20)
        p.drawLine(18, 20, 21, 53)
        p.drawLine(46, 20, 43, 53)
        p.drawLine(21, 53, 43, 53)
    elif name == "pause":
        p.drawLine(25, 16, 25, 48)
        p.drawLine(39, 16, 39, 48)
    elif name == "play":
        path = QPainterPath()
        path.moveTo(22, 14)
        path.lineTo(48, 32)
        path.lineTo(22, 50)
        path.closeSubpath()
        p.drawPath(path)
    elif name == "power":
        p.drawArc(QRectF(13, 15, 38, 38), 300 * 16, -240 * 16)
        p.drawLine(32, 10, 32, 30)
    elif name == "plug":
        p.drawLine(24, 12, 24, 28)
        p.drawLine(40, 12, 40, 28)
        p.drawArc(QRectF(18, 22, 28, 28), 180 * 16, 180 * 16)
        p.drawLine(32, 44, 32, 54)
    elif name == "chevron-down":
        p.drawLine(18, 26, 32, 40)
        p.drawLine(32, 40, 46, 26)
    elif name == "chevron-right":
        p.drawLine(26, 18, 40, 32)
        p.drawLine(40, 32, 26, 46)
    elif name == "dot":
        p.setBrush(QColor(color))
        p.setPen(Qt.NoPen)
        p.drawEllipse(QRectF(22, 22, 20, 20))
    p.end()

    if size != 64:
        pm = pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return QIcon(pm)


def tray_icon(state: str = "idle") -> QIcon:
    """Filled round badge — has to read clearly at 16px in the tray."""
    bg = {"idle": TRAY_IDLE, "busy": TRAY_BUSY, "error": TRAY_ERROR}.get(state, TRAY_IDLE)
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(bg))
    p.setPen(Qt.NoPen)
    p.drawEllipse(QRectF(2, 2, 60, 60))
    _pen(p, "#FFFFFF", 7)
    if state == "error":
        p.drawLine(32, 17, 32, 37)
        p.setBrush(QColor("#FFFFFF"))
        p.setPen(Qt.NoPen)
        p.drawEllipse(QRectF(27.5, 43, 9, 9))
    else:
        p.drawLine(32, 47, 32, 21)
        p.drawLine(32, 19, 21, 30)
        p.drawLine(32, 19, 43, 30)
    p.end()
    return QIcon(pm)


# ── window chrome ────────────────────────────────────────────────────────

def apply_window_effects(widget):
    """Dark title bar + rounded corners on Windows 11. No-op elsewhere."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = int(widget.winId())
        dwm = ctypes.windll.dwmapi
        use_dark = ctypes.c_int(1 if PALETTE.dark else 0)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(use_dark), ctypes.sizeof(use_dark))
        round_pref = ctypes.c_int(2)   # DWMWCP_ROUND
        dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(round_pref), ctypes.sizeof(round_pref))
    except Exception:
        pass
