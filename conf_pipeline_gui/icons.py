"""Programmatic line icons: stroke-drawn QIcons on a 20px grid, theme-tinted.

No SVG assets to ship; crisp on high-DPI (rendered at 2x with a device pixel
ratio); recolorable per palette role. Each draw function paints with a 1.6px
round-capped pen inside a 20x20 logical box.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

_CACHE: dict = {}


def _pen(color: str, width: float = 1.6) -> QPen:
    pen = QPen(QColor(color), width)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    return pen


# ---- draw functions (20x20 logical grid) ----
def _select(p, c):
    path = QPainterPath(QPointF(5, 3))
    for pt in [(5, 15), (8.4, 12.2), (10.6, 17), (12.6, 16.1), (10.4, 11.4), (14.6, 11)]:
        path.lineTo(QPointF(*pt))
    path.closeSubpath()
    p.setPen(_pen(c))
    p.drawPath(path)


def _connect(p, c):
    p.setPen(_pen(c))
    p.drawEllipse(QPointF(5, 15), 2.4, 2.4)
    p.drawEllipse(QPointF(15, 5), 2.4, 2.4)
    p.drawLine(QPointF(6.8, 13.2), QPointF(13.2, 6.8))


def _room(p, c):
    p.setPen(_pen(c))
    p.drawRect(QRectF(3.5, 4.5, 13, 11))
    p.drawLine(QPointF(3.5, 9), QPointF(7, 9))   # door notch
    p.drawLine(QPointF(7, 9), QPointF(7, 4.5))


def _zone(p, c):
    pen = _pen(c)
    pen.setDashPattern([3, 2.2])
    p.setPen(pen)
    p.drawRoundedRect(QRectF(3.5, 5, 13, 10), 2, 2)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(c))
    p.drawEllipse(QPointF(10, 10), 1.6, 1.6)


def _talker(p, c):
    p.setPen(_pen(c))
    p.drawEllipse(QPointF(10, 6.5), 3, 3)
    path = QPainterPath(QPointF(4.5, 17))
    path.cubicTo(QPointF(5.5, 11.5), QPointF(14.5, 11.5), QPointF(15.5, 17))
    p.drawPath(path)


def _view2d(p, c):
    p.setPen(_pen(c, 1.4))
    p.drawRect(QRectF(3.5, 3.5, 13, 13))
    p.drawLine(QPointF(8, 3.5), QPointF(8, 16.5))
    p.drawLine(QPointF(12.5, 3.5), QPointF(12.5, 16.5))
    p.drawLine(QPointF(3.5, 8), QPointF(16.5, 8))
    p.drawLine(QPointF(3.5, 12.5), QPointF(16.5, 12.5))


def _view3d(p, c):
    p.setPen(_pen(c, 1.4))
    top = [QPointF(10, 3), QPointF(16.5, 6.5), QPointF(10, 10), QPointF(3.5, 6.5)]
    p.drawPolygon(top)
    p.drawLine(QPointF(3.5, 6.5), QPointF(3.5, 13.5))
    p.drawLine(QPointF(16.5, 6.5), QPointF(16.5, 13.5))
    p.drawLine(QPointF(10, 10), QPointF(10, 17))
    p.drawLine(QPointF(3.5, 13.5), QPointF(10, 17))
    p.drawLine(QPointF(16.5, 13.5), QPointF(10, 17))


def _coverage(p, c):
    p.setPen(_pen(c, 1.4))
    p.drawEllipse(QPointF(10, 10), 2.2, 2.2)
    p.drawEllipse(QPointF(10, 10), 5.2, 5.2)
    pen = _pen(c, 1.2)
    pen.setDashPattern([2.5, 2.5])
    p.setPen(pen)
    p.drawEllipse(QPointF(10, 10), 8, 8)


def _heatmap(p, c):
    p.setPen(Qt.NoPen)
    col = QColor(c)
    for i, (x, y, a) in enumerate([(5.5, 5.5, 90), (10, 5.5, 160), (14.5, 5.5, 255),
                                   (5.5, 10, 160), (10, 10, 255), (14.5, 10, 160),
                                   (5.5, 14.5, 255), (10, 14.5, 160), (14.5, 14.5, 90)]):
        col.setAlpha(a)
        p.setBrush(col)
        p.drawEllipse(QPointF(x, y), 1.7, 1.7)


def _undo(p, c):
    p.setPen(_pen(c))
    path = QPainterPath(QPointF(6, 5))
    path.lineTo(QPointF(3.5, 8))
    path.lineTo(QPointF(6.5, 10.5))
    p.drawPath(path)
    arc = QPainterPath(QPointF(3.8, 8))
    arc.cubicTo(QPointF(9, 7), QPointF(16, 7.5), QPointF(15.5, 12.5))
    arc.cubicTo(QPointF(15, 16.5), QPointF(9, 16.5), QPointF(6.5, 14.5))
    p.drawPath(arc)


def _redo(p, c):
    p.save()
    p.translate(20, 0)
    p.scale(-1, 1)
    _undo(p, c)
    p.restore()


def _optimize(p, c):
    p.setPen(_pen(c))
    path = QPainterPath(QPointF(10, 3))
    path.cubicTo(QPointF(10.8, 8.2), QPointF(11.8, 9.2), QPointF(17, 10))
    path.cubicTo(QPointF(11.8, 10.8), QPointF(10.8, 11.8), QPointF(10, 17))
    path.cubicTo(QPointF(9.2, 11.8), QPointF(8.2, 10.8), QPointF(3, 10))
    path.cubicTo(QPointF(8.2, 9.2), QPointF(9.2, 8.2), QPointF(10, 3))
    p.drawPath(path)
    p.setBrush(QColor(c))
    p.setPen(Qt.NoPen)
    p.drawEllipse(QPointF(15.5, 4.5), 1.3, 1.3)


def _route(p, c):
    p.setPen(_pen(c))
    p.drawEllipse(QPointF(5, 5), 2, 2)
    p.drawEllipse(QPointF(5, 15), 2, 2)
    p.drawEllipse(QPointF(15, 10), 2, 2)
    p.drawLine(QPointF(7, 5.6), QPointF(13.2, 9.2))
    p.drawLine(QPointF(7, 14.4), QPointF(13.2, 10.8))


def _deploy(p, c):
    p.setPen(_pen(c))
    p.drawLine(QPointF(10, 13), QPointF(10, 3.5))
    p.drawLine(QPointF(10, 3.5), QPointF(6.4, 7.1))
    p.drawLine(QPointF(10, 3.5), QPointF(13.6, 7.1))
    path = QPainterPath(QPointF(3.5, 12.5))
    path.lineTo(QPointF(3.5, 16.5))
    path.lineTo(QPointF(16.5, 16.5))
    path.lineTo(QPointF(16.5, 12.5))
    p.drawPath(path)


def _import_(p, c):
    p.setPen(_pen(c))
    p.drawLine(QPointF(10, 3.5), QPointF(10, 13))
    p.drawLine(QPointF(10, 13), QPointF(6.4, 9.4))
    p.drawLine(QPointF(10, 13), QPointF(13.6, 9.4))
    path = QPainterPath(QPointF(3.5, 12.5))
    path.lineTo(QPointF(3.5, 16.5))
    path.lineTo(QPointF(16.5, 16.5))
    path.lineTo(QPointF(16.5, 12.5))
    p.drawPath(path)


def _report(p, c):
    p.setPen(_pen(c, 1.4))
    path = QPainterPath(QPointF(5, 3.5))
    path.lineTo(QPointF(12, 3.5))
    path.lineTo(QPointF(15, 6.5))
    path.lineTo(QPointF(15, 16.5))
    path.lineTo(QPointF(5, 16.5))
    path.closeSubpath()
    p.drawPath(path)
    p.drawLine(QPointF(7.2, 9), QPointF(12.8, 9))
    p.drawLine(QPointF(7.2, 11.6), QPointF(12.8, 11.6))
    p.drawLine(QPointF(7.2, 14.2), QPointF(11, 14.2))


def _theme(p, c):
    p.setPen(_pen(c, 1.4))
    p.drawEllipse(QPointF(10, 10), 6.5, 6.5)
    path = QPainterPath(QPointF(10, 3.5))
    path.arcTo(QRectF(3.5, 3.5, 13, 13), 90, -180)
    path.closeSubpath()
    p.fillPath(path, QColor(c))


def _menu(p, c):
    p.setPen(_pen(c, 1.8))
    for y in (5.5, 10, 14.5):
        p.drawLine(QPointF(4, y), QPointF(16, y))


def _rooms(p, c):
    p.setPen(_pen(c, 1.4))
    p.drawRect(QRectF(3.5, 6.5, 10, 10))
    p.drawLine(QPointF(6.5, 6.5), QPointF(6.5, 3.5))
    p.drawLine(QPointF(6.5, 3.5), QPointF(16.5, 3.5))
    p.drawLine(QPointF(16.5, 3.5), QPointF(16.5, 13.5))
    p.drawLine(QPointF(16.5, 13.5), QPointF(13.5, 13.5))


def _gear(p, c):
    p.setPen(_pen(c, 1.4))
    p.drawEllipse(QPointF(10, 10), 3, 3)
    import math
    for i in range(8):
        a = math.pi / 4 * i
        x1, y1 = 10 + 5 * math.cos(a), 10 + 5 * math.sin(a)
        x2, y2 = 10 + 7 * math.cos(a), 10 + 7 * math.sin(a)
        p.drawLine(QPointF(x1, y1), QPointF(x2, y2))


def _mic(p, c):
    p.setPen(_pen(c, 1.5))
    p.drawRoundedRect(QRectF(7.5, 3.5, 5, 8.5), 2.5, 2.5)
    path = QPainterPath(QPointF(5, 9.5))
    path.cubicTo(QPointF(5, 14), QPointF(15, 14), QPointF(15, 9.5))
    p.drawPath(path)
    p.drawLine(QPointF(10, 13.8), QPointF(10, 16.5))


def _help(p, c):
    p.setPen(_pen(c, 1.5))
    path = QPainterPath(QPointF(7, 7.2))
    path.cubicTo(QPointF(7, 4.2), QPointF(13, 4.2), QPointF(13, 7.2))
    path.cubicTo(QPointF(13, 9.6), QPointF(10, 9.4), QPointF(10, 12.2))
    p.drawPath(path)
    p.setBrush(QColor(c))
    p.setPen(Qt.NoPen)
    p.drawEllipse(QPointF(10, 16), 1.3, 1.3)


_DRAW = {
    "select": _select, "connect": _connect, "room": _room, "zone": _zone, "talker": _talker,
    "view2d": _view2d, "view3d": _view3d, "coverage": _coverage, "heatmap": _heatmap,
    "undo": _undo, "redo": _redo, "optimize": _optimize, "route": _route,
    "deploy": _deploy, "import": _import_, "export": _deploy, "report": _report,
    "theme": _theme, "menu": _menu, "rooms": _rooms, "gear": _gear, "mic": _mic, "help": _help,
}


def pixmap(name: str, color: str, size: int = 20) -> QPixmap:
    key = (name, color, size)
    if key in _CACHE:
        return _CACHE[key]
    pm = QPixmap(size * 2, size * 2)
    pm.setDevicePixelRatio(2.0)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.scale(size / 20.0, size / 20.0)
    _DRAW[name](p, color)
    p.end()
    _CACHE[key] = pm
    return pm


def icon(name: str, color: str, size: int = 20, active_color: str | None = None) -> QIcon:
    ic = QIcon()
    ic.addPixmap(pixmap(name, color, size), QIcon.Normal, QIcon.Off)
    if active_color:
        for mode in (QIcon.Active, QIcon.Selected):
            ic.addPixmap(pixmap(name, active_color, size), mode, QIcon.Off)
        ic.addPixmap(pixmap(name, active_color, size), QIcon.Normal, QIcon.On)
    return ic


def clear_cache() -> None:
    """Drop cached pixmaps (call on theme switch)."""
    _CACHE.clear()
