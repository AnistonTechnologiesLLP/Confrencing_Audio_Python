"""Interactive 2D / 3D layout canvas (QPainter, no extra deps)."""
from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

import conf_pipeline as cp
from conf_pipeline import matrix as mx
from conf_pipeline.angles import Point3D, steering_angles
from conf_pipeline.model import Point2D, RectShape, default_elevation

from .state import AppState
from .theme import palette

DEVICE_STYLE = {
    "processor": ("#9a6dff", "square"),
    "microphoneArray": ("#6d8bff", "circle"),
    "wirelessMic": ("#3ddc97", "circle"),
    "wiredMic": ("#2dd4bf", "circle"),
    "loudspeaker": ("#f7c948", "triangle"),
    "codec": ("#94a3b8", "diamond"),
}
TALKER_COLOR = "#ff7ab6"
DEFAULT_TALKER_ELEV = 1.2


def _qc(hex_str: str, alpha: int = 255) -> QColor:
    c = QColor(hex_str)
    c.setAlpha(alpha)
    return c


def _zone_style(ztype: str):
    if ztype == "exclusion":
        return (_qc("#ff6b81", 40), _qc("#ff6b81"), [2, 3], "⦸")
    if ztype == "dedicated":
        return (_qc("#9a6dff", 51), _qc("#9a6dff"), [], "◆")
    return (_qc("#6d8bff", 38), _qc("#6d8bff"), [5, 4], "▢")


REC_COLOR = "#3ddc97"  # placement-recommendation accent (green)

# What each workflow mode may edit / emphasises on the canvas. Selection works
# everywhere (cross-mode synergy); geometry editing is a DESIGN job, talkers
# stay draggable in SIMULATE for what-if exploration.
MODE_PROFILE = {
    "design":   dict(edit=True,  drag_devices=True,  drag_talkers=True,  dim_zones=False, bold_routes=False),
    "simulate": dict(edit=False, drag_devices=False, drag_talkers=True,  dim_zones=False, bold_routes=False),
    "route":    dict(edit=False, drag_devices=False, drag_talkers=False, dim_zones=True,  bold_routes=True),
    "deploy":   dict(edit=False, drag_devices=False, drag_talkers=False, dim_zones=True,  bold_routes=False),
    "live":     dict(edit=False, drag_devices=False, drag_talkers=False, dim_zones=False, bold_routes=False),
}


def _lerp(a, b, t):
    return a + (b - a) * t


def _heat_color(t: float, alpha: int = 110) -> QColor:
    """Score ``t`` in ``[0,1]`` -> blue (low) → teal → amber (high)."""
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    if t < 0.5:
        u = t / 0.5
        r, g, b = _lerp(40, 40, u), _lerp(70, 200, u), _lerp(160, 180, u)
    else:
        u = (t - 0.5) / 0.5
        r, g, b = _lerp(40, 245, u), _lerp(200, 200, u), _lerp(180, 70, u)
    return QColor(int(r), int(g), int(b), alpha)


class Canvas(QWidget):
    def __init__(self, state: AppState):
        super().__init__()
        self.state = state
        self.setMouseTracking(True)
        self.setMinimumSize(360, 280)
        self.setFocusPolicy(Qt.StrongFocus)
        self.drag = None          # active drag preview
        self.orbit = None         # {"x","y"} during 3D orbit
        self.move3 = None         # {"id","type","h"} during 3D entity move
        self.draw_pts = []        # in-progress room polygon
        self.hover = None         # world point (room tool) / screen point (connect)
        self.connect_from = None
        self.coord_cb = None      # optional callback(str) for coordinate readout
        self._bg_pixmap = None    # cached floor-plan QPixmap
        self._bg_pixmap_key = None
        self._diff_cache = None       # cached deployment diff for DEPLOY badges
        self._diff_cache_key = None
        state.changed.connect(self.update)
        state.liveOverlayChanged.connect(self._on_live_overlay)

    def _profile(self) -> dict:
        return MODE_PROFILE.get(getattr(self.state, "mode", "design"), MODE_PROFILE["design"])

    def _pal(self) -> dict:
        return palette(getattr(self.state, "theme", "dark"))

    def _on_live_overlay(self):
        if getattr(self.state, "mode", "design") == "live":
            self.update()

    # ----------------------------------------------------------------- helpers
    @property
    def cfg(self):
        return self.state.config

    def snap(self, v: float) -> float:
        s = self.state.snap
        return round(v / s) * s if s else round(v * 100) / 100

    def device_pos(self, d):
        if self.drag and self.drag.get("kind") == "device" and self.drag["id"] == d.id:
            return self.drag["pos"]
        return d.position

    def talker_pos(self, t):
        if self.drag and self.drag.get("kind") == "talker" and self.drag["id"] == t.id:
            return self.drag["pos"]
        return t.position

    def talker_elev(self, t):
        return t.elevation if t.elevation is not None else DEFAULT_TALKER_ELEV

    def elev3d(self, d):
        return d.elevation if d.elevation is not None else default_elevation(d, self._room_h())

    def _room_h(self):
        return self.cfg.room.height if self.cfg.room else 3.0

    def shape_corners(self, shape):
        if isinstance(shape, RectShape):
            o, w, h = shape.origin, shape.width, shape.height
            return [o, Point2D(o.x + w, o.y), Point2D(o.x + w, o.y + h), Point2D(o.x, o.y + h)]
        return shape.points

    def bounds(self):
        pts = []
        if self.cfg.room:
            pts += self.cfg.room.vertices
        for d in self.cfg.devices:
            if d.position:
                pts.append(d.position)
            if d.type == "microphoneArray":
                for z in d.zones:
                    pts += self.shape_corners(z.shape)
        for t in self.cfg.talkers:
            pts.append(t.position)
        if not pts:
            return (0.0, 0.0, 12.0, 9.0)
        xs = [p.x for p in pts]
        ys = [p.y for p in pts]
        minx, miny, maxx, maxy = min(xs), min(ys), max(xs), max(ys)
        pad = 1.5
        minx, miny, maxx, maxy = minx - pad, miny - pad, maxx + pad, maxy + pad
        if maxx - minx < 6:
            c = (minx + maxx) / 2
            minx, maxx = c - 3, c + 3
        if maxy - miny < 5:
            c = (miny + maxy) / 2
            miny, maxy = c - 2.5, c + 2.5
        return (minx, miny, maxx, maxy)

    # ------------------------------------------------------------- 2D transform
    def view2d(self):
        w, h, m = self.width(), self.height(), 34
        minx, miny, maxx, maxy = self.bounds()
        sx, sy = max(maxx - minx, 1), max(maxy - miny, 1)
        scale = min((w - 2 * m) / sx, (h - 2 * m) / sy)
        offx = m + (w - 2 * m - sx * scale) / 2 - minx * scale
        offy = m + (h - 2 * m - sy * scale) / 2 - miny * scale
        return (scale, offx, offy, minx, miny, maxx, maxy)

    def w2s(self, p, v):
        scale, offx, offy = v[0], v[1], v[2]
        return QPointF(offx + p.x * scale, offy + p.y * scale)

    def s2w(self, x, y, v):
        scale, offx, offy = v[0], v[1], v[2]
        return Point2D((x - offx) / scale, (y - offy) / scale)

    # ------------------------------------------------------------- 3D transform
    FOV = 0.92

    def camera(self):
        minx, miny, maxx, maxy = self.bounds()
        tx, tz = (minx + maxx) / 2, (miny + maxy) / 2
        ty = self._room_h() / 2
        cam = self.state.cam
        d = cam["dist"]
        cp_ = Point3D(
            tx + d * math.cos(cam["pitch"]) * math.sin(cam["yaw"]),
            ty + d * math.sin(cam["pitch"]),
            tz + d * math.cos(cam["pitch"]) * math.cos(cam["yaw"]),
        )

        def sub(a, b):
            return Point3D(a.x - b.x, a.y - b.y, a.z - b.z)

        def norm(a):
            ln = math.sqrt(a.x * a.x + a.y * a.y + a.z * a.z) or 1
            return Point3D(a.x / ln, a.y / ln, a.z / ln)

        def cross(a, b):
            return Point3D(a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x)

        fwd = norm(sub(Point3D(tx, ty, tz), cp_))
        right = norm(cross(fwd, Point3D(0, 1, 0)))
        up = cross(right, fwd)
        return cp_, fwd, right, up

    def project(self, P, cam):
        cp_, fwd, right, up = cam
        r = Point3D(P.x - cp_.x, P.y - cp_.y, P.z - cp_.z)
        cz = r.x * fwd.x + r.y * fwd.y + r.z * fwd.z
        if cz <= 0.05:
            return None
        focal = (self.height() / 2) / math.tan(self.FOV / 2)
        cx = r.x * right.x + r.y * right.y + r.z * right.z
        cy = r.x * up.x + r.y * up.y + r.z * up.z
        return (self.width() / 2 + focal * cx / cz, self.height() / 2 - focal * cy / cz, cz, focal / cz)

    def ray_floor(self, sx, sy, cam, h):
        cp_, fwd, right, up = cam
        focal = (self.height() / 2) / math.tan(self.FOV / 2)
        dx = (sx - self.width() / 2) / focal
        dy = -(sy - self.height() / 2) / focal
        dir_ = Point3D(right.x * dx + up.x * dy + fwd.x, right.y * dx + up.y * dy + fwd.y, right.z * dx + up.z * dy + fwd.z)
        ln = math.sqrt(dir_.x ** 2 + dir_.y ** 2 + dir_.z ** 2) or 1
        dir_ = Point3D(dir_.x / ln, dir_.y / ln, dir_.z / ln)
        if abs(dir_.y) < 1e-4:
            return None
        t = (h - cp_.y) / dir_.y
        if t <= 0:
            return None
        return Point2D(cp_.x + dir_.x * t, cp_.z + dir_.z * t)

    def dev3(self, d):
        p = self.device_pos(d)
        if not p:
            return None
        return Point3D(p.x, self.elev3d(d), p.y)

    def talker3(self, t):
        p = self.talker_pos(t)
        return Point3D(p.x, self.talker_elev(t), p.y)

    def footprint(self):
        if self.cfg.room:
            return [(p.x, p.y) for p in self.cfg.room.vertices]
        minx, miny, maxx, maxy = self.bounds()
        return [(minx + 1.5, miny + 1.5), (maxx - 1.5, miny + 1.5), (maxx - 1.5, maxy - 1.5), (minx + 1.5, maxy - 1.5)]

    # --------------------------------------------------------------- validation
    def _error_refs(self):
        res = cp.validate(self.cfg)
        err = set()
        warn = set()
        for e in res.errors:
            err.update(e.refs)
        for w in res.warnings:
            warn.update(w.refs)
        return err, warn

    # ------------------------------------------------------------------- paint
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor(self._pal()["canvas_bg"]))
        if self.state.view == "3d":
            self._paint3d(p)
        else:
            self._paint2d(p)
        self._paint_empty_hint(p)

    def _paint_empty_hint(self, p):
        """Centered, mode-aware hint when there is nothing on the canvas yet."""
        cfg = self.cfg
        has_room = cfg.room is not None and len(cfg.room.vertices) >= 3
        if has_room or cfg.devices or cfg.talkers:
            return
        cx, cy = self.width() / 2, self.height() / 2
        pal = self._pal()
        head, dim = QColor(pal["canvas_text"]), QColor(pal["canvas_text_dim"])
        mode = getattr(self.state, "mode", "design")
        if mode == "design":
            lines = [
                ("Start your room design", head, 15, QFont.DemiBold),
                ("", None, 6, QFont.Normal),
                ("• Press the Room tool (R) and click to draw an outline", dim, 11, QFont.Normal),
                ("• Or load a sample from the ☰ menu", dim, 11, QFont.Normal),
            ]
        else:
            verb = {"simulate": "simulate", "route": "route", "deploy": "deploy", "live": "drive"}.get(mode, "show")
            lines = [
                (f"Nothing to {verb} yet", head, 15, QFont.DemiBold),
                ("", None, 6, QFont.Normal),
                ("• Switch to DESIGN (Ctrl+1) and build the room first", dim, 11, QFont.Normal),
            ]
        y = cy - 36
        for text, color, size, weight in lines:
            if not text:
                y += size
                continue
            p.setFont(QFont("Segoe UI", size, weight))
            fm = p.fontMetrics()
            p.setPen(color)
            p.drawText(QPointF(cx - fm.horizontalAdvance(text) / 2, y), text)
            y += fm.height() + 2

    def _grid_pen(self, axis):
        pal = self._pal()
        return QPen(QColor(pal["canvas_axis"] if axis else pal["canvas_grid"]), 1)

    def _label(self, p, x, y, text, color="#e6e9f2", bg=True):
        p.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(text)
        if bg:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(8, 12, 24, 184))
            p.drawRoundedRect(QRectF(x - 1, y - 11, tw + 10, 16), 4, 4)
        p.setPen(QColor(color))
        p.drawText(QPointF(x + 4, y + 2), text)

    def _marker(self, p, shape, x, y, r, fill, outline):
        p.setBrush(QBrush(fill))
        p.setPen(QPen(outline, 2))
        if shape == "circle":
            p.drawEllipse(QPointF(x, y), r, r)
        elif shape == "square":
            p.drawRect(QRectF(x - r, y - r, 2 * r, 2 * r))
        elif shape == "triangle":
            poly = QPolygonF([QPointF(x, y - r * 1.15), QPointF(x + r, y + r * 0.85), QPointF(x - r, y + r * 0.85)])
            p.drawPolygon(poly)
        else:
            poly = QPolygonF([QPointF(x, y - r * 1.15), QPointF(x + r, y), QPointF(x, y + r * 1.15), QPointF(x - r, y)])
            p.drawPolygon(poly)

    def _person(self, p, x, y, r, sel):
        p.setBrush(QBrush(_qc(TALKER_COLOR)))
        p.setPen(QPen(QColor("#070a12"), 2))
        path = QPainterPath()
        path.moveTo(x - r * 1.05, y + r * 0.95)
        path.arcTo(QRectF(x - r * 1.05, y - r * 0.1, r * 2.1, r * 2.1), 180, 180)
        path.closeSubpath()
        p.drawPath(path)
        p.drawEllipse(QPointF(x, y - r * 0.55), r * 0.6, r * 0.6)
        if sel:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor("#ffffff"), 2))
            p.drawEllipse(QPointF(x, y), r * 1.7, r * 1.7)

    def _angle_rays(self, p, project=None, cam=None, v=None):
        sel = self.state.selection
        if not sel or sel["kind"] != "talker":
            return
        t = next((x for x in self.cfg.talkers if x.id == sel["id"]), None)
        if not t:
            return
        if project:
            tp = project(self.talker3(t), cam)
            if not tp:
                return
            tb = QPointF(tp[0], tp[1])
        else:
            tb = self.w2s(self.talker_pos(t), v)
        for d in self.cfg.devices:
            if d.type != "microphoneArray" or not d.position:
                continue
            ang = cp.array_to_talker_angles(self.cfg, d.id, t.id)
            if not ang:
                continue
            if project:
                ap = project(self.dev3(d), cam)
                if not ap:
                    continue
                a = QPointF(ap[0], ap[1])
            else:
                a = self.w2s(d.position, v)
            pen = QPen(QColor("#7fe3ff"), 1.4)
            pen.setDashPattern([4, 4])
            p.setPen(pen)
            p.drawLine(a, tb)
            mx_, my_ = (a.x() + tb.x()) / 2, (a.y() + tb.y()) / 2
            self._label(p, mx_ - 26, my_, f"{round(ang.off_nadir_deg)}° · {ang.distance:.1f}m", "#7fe3ff")

    # ---- placement-simulation overlays ----
    def _paint_heatmap(self, p, v):
        if not self.state.sim_show_heatmap:
            return
        hm = self.state.sim_heatmap
        if not hm or hm.nx == 0 or hm.ny == 0:
            return
        rng = max(hm.vmax - hm.vmin, 1e-6)
        half = hm.step_m / 2.0
        for iy in range(hm.ny):
            for ix in range(hm.nx):
                val = hm.at(ix, iy)
                if val is None:
                    continue
                wx = hm.origin.x + ix * hm.step_m
                wy = hm.origin.y + iy * hm.step_m
                a = self.w2s(Point2D(wx - half, wy - half), v)
                b = self.w2s(Point2D(wx + half, wy + half), v)
                p.fillRect(QRectF(a.x(), a.y(), b.x() - a.x(), b.y() - a.y()), _heat_color((val - hm.vmin) / rng))

    def _paint_recommendation(self, p, v=None, project=None, cam=None):
        rec = self.state.sim_recommendation
        if not rec:
            return

        def to_screen(pt2, elev):
            if pt2 is None:
                return None
            if project is not None:
                s = project(Point3D(pt2.x, elev, pt2.y), cam)
                return QPointF(s[0], s[1]) if s else None
            return self.w2s(pt2, v)

        a = to_screen(rec.array_pos, rec.array_elev)
        seat = to_screen(rec.talker_pos, DEFAULT_TALKER_ELEV) if rec.talker_pos else None
        col = QColor(REC_COLOR)
        # steer ray array -> seat
        if a and seat:
            pen = QPen(col, 1.6)
            pen.setDashPattern([5, 4])
            p.setPen(pen)
            p.drawLine(a, seat)
        # array reticle (double ring + crosshair)
        if a:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(col, 2))
            p.drawEllipse(a, 12, 12)
            p.drawEllipse(a, 5, 5)
            p.drawLine(QPointF(a.x() - 16, a.y()), QPointF(a.x() + 16, a.y()))
            p.drawLine(QPointF(a.x(), a.y() - 16), QPointF(a.x(), a.y() + 16))
            tilt = "" if rec.talker_pos is None else f"  {round(rec.steer_off_nadir_deg)}°"
            self._label(p, a.x() + 15, a.y() - 11, f"★ array{tilt}", REC_COLOR)
        # seat marker
        if seat:
            p.setBrush(_qc(REC_COLOR, 60))
            p.setPen(QPen(col, 2))
            p.drawEllipse(seat, 9, 9)
            lbl = "★ seat"
            if rec.validated is not None:
                lbl += f"  {rec.validated.predicted_snr_db:.1f} dB"
            self._label(p, seat.x() + 12, seat.y() + 4, lbl, REC_COLOR)

    def _paint_room_dims(self, p, v):
        """Label each room wall with its length, plus the ceiling height."""
        verts = self._room_verts_live()
        if not verts or len(verts) < 2:
            return
        cx = sum(pt.x for pt in verts) / len(verts)
        cy = sum(pt.y for pt in verts) / len(verts)
        n = len(verts)
        for i in range(n):
            a, b = verts[i], verts[(i + 1) % n]
            length = math.hypot(b.x - a.x, b.y - a.y)
            if length < 0.05:
                continue
            midx, midy = (a.x + b.x) / 2, (a.y + b.y) / 2
            ox, oy = midx - cx, midy - cy
            olen = math.hypot(ox, oy) or 1.0
            s = self.w2s(Point2D(midx + ox / olen * 0.35, midy + oy / olen * 0.35), v)
            self._label(p, s.x() - 16, s.y() + 2, f"{length:.2f} m", "#a8b6e6")
        s0 = self.w2s(verts[0], v)
        self._label(p, s0.x() + 6, s0.y() - 7, f"H {self._room_h():.2f} m", "#8b95bd")

    def _bg_pixmap_for(self, bg):
        from PySide6.QtGui import QPixmap
        if bg is None:
            self._bg_pixmap = None
            self._bg_pixmap_key = None
            return None
        if self._bg_pixmap_key != bg.path:
            pm = QPixmap(bg.path)
            self._bg_pixmap = None if pm.isNull() else pm
            self._bg_pixmap_key = bg.path
        return self._bg_pixmap

    def _paint_background(self, p, v):
        """Floor-plan image under the grid (2D), scaled into its world rect."""
        bg = self.cfg.room.background if self.cfg.room else None
        pm = self._bg_pixmap_for(bg)
        if pm is None or not bg or not bg.scale_m_per_px:
            return
        tl = self.w2s(bg.origin, v)
        br = self.w2s(Point2D(bg.origin.x + bg.image_width_px * bg.scale_m_per_px,
                              bg.origin.y + bg.image_height_px * bg.scale_m_per_px), v)
        p.setOpacity(bg.opacity)
        p.drawPixmap(QRectF(tl, br), pm, QRectF(pm.rect()))
        p.setOpacity(1.0)

    def _paint_coverage(self, p, v):
        """Dashed coverage-area circles for each placed array (2D)."""
        if not self.state.show_coverage:
            return
        color = DEVICE_STYLE["microphoneArray"][0]
        for d in self.cfg.devices:
            if d.type != "microphoneArray":
                continue
            circ = cp.array_coverage_circle(self.cfg, d.id)
            if not circ:
                continue
            center, radius = circ
            c_s = self.w2s(center, v)
            r_s = radius * v[0]
            p.setBrush(_qc(color, 16))
            pen = QPen(_qc(color, 150), 1.3)
            pen.setDashPattern([5, 4])
            p.setPen(pen)
            p.drawEllipse(c_s, r_s, r_s)

    # ---- 2D ----
    def _paint2d(self, p):
        v = self.view2d()
        prof = self._profile()
        minx, miny, maxx, maxy = v[3], v[4], v[5], v[6]
        self._paint_background(p, v)
        for x in range(math.ceil(minx), math.floor(maxx) + 1):
            p.setPen(self._grid_pen(x == 0))
            p.drawLine(self.w2s(Point2D(x, miny), v), self.w2s(Point2D(x, maxy), v))
        for y in range(math.ceil(miny), math.floor(maxy) + 1):
            p.setPen(self._grid_pen(y == 0))
            p.drawLine(self.w2s(Point2D(minx, y), v), self.w2s(Point2D(maxx, y), v))
        self._paint_heatmap(p, v)
        # room
        verts = self._room_verts_live()
        if verts and len(verts) >= 2:
            poly = QPolygonF([self.w2s(p2, v) for p2 in verts])
            p.setBrush(_qc("#6d8bff", 13))
            p.setPen(QPen(QColor("#46568f"), 2))
            p.drawPolygon(poly)
            if self.state.tool == "select" and prof["edit"]:
                p.setBrush(QColor("#9db4ff"))
                p.setPen(Qt.NoPen)
                for p2 in verts:
                    s = self.w2s(p2, v)
                    p.drawEllipse(s, 4, 4)
        self._paint_room_dims(p, v)
        # zones (dimmed in modes that emphasise something else)
        dim = prof["dim_zones"]
        for d in self.cfg.devices:
            if d.type != "microphoneArray":
                continue
            for z in d.zones:
                fill, stroke, dash, glyph = _zone_style(z.type)
                if dim:
                    fill = QColor(fill)
                    fill.setAlpha(12)
                    stroke = QColor(stroke)
                    stroke.setAlpha(80)
                corners = [self.w2s(c, v) for c in self.shape_corners(self._zone_shape_live(d.id, z))]
                p.setBrush(fill)
                pen = QPen(stroke, 1.5)
                if dash:
                    pen.setDashPattern(dash)
                p.setPen(pen)
                p.drawPolygon(QPolygonF(corners))
                c0 = corners[0]
                p.setPen(_qc("#cdd6f4", 110 if dim else 255))
                p.setFont(QFont("Segoe UI", 8))
                p.drawText(QPointF(c0.x() + 4, c0.y() + 13), f"{glyph} {z.label}")
                if self.state.tool == "select" and prof["edit"]:
                    br = corners[2]
                    p.setBrush(QColor("#ffffff"))
                    p.setPen(QPen(stroke, 1))
                    p.drawRect(QRectF(br.x() - 4, br.y() - 4, 8, 8))
        self._paint_coverage(p, v)
        self._draw_routes_2d(p, v)
        self._angle_rays(p, v=v)
        # in-progress zone
        if self.drag and self.drag.get("kind") == "zone-new":
            a = self.w2s(self.drag["start"], v)
            b = self.w2s(self.drag["cur"], v)
            pen = QPen(QColor("#6d8bff"), 1.5)
            pen.setDashPattern([5, 4])
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF(min(a.x(), b.x()), min(a.y(), b.y()), abs(b.x() - a.x()), abs(b.y() - a.y())))
        # in-progress room
        if self.state.tool == "room" and self.draw_pts:
            pen = QPen(QColor("#9db4ff"), 1.5)
            p.setPen(pen)
            path = QPainterPath()
            path.moveTo(self.w2s(self.draw_pts[0], v))
            for pt in self.draw_pts[1:]:
                path.lineTo(self.w2s(pt, v))
            if isinstance(self.hover, Point2D):
                path.lineTo(self.w2s(self.hover, v))
            p.drawPath(path)
        err, warn = self._error_refs()
        # devices
        for d in self.cfg.devices:
            pos = self.device_pos(d)
            if not pos:
                continue
            color, shape = DEVICE_STYLE[d.type]
            s = self.w2s(pos, v)
            self._highlight(p, s, d.id, err, warn)
            outline = QColor("#ff6b81") if d.id in err else QColor("#070a12")
            self._marker(p, shape, s.x(), s.y(), 9, _qc(color), outline)
            self._label(p, s.x() + 11, s.y() + 4, d.label)
        # talkers
        for t in self.cfg.talkers:
            s = self.w2s(self.talker_pos(t), v)
            sel = self.state.selection and self.state.selection["kind"] == "talker" and self.state.selection["id"] == t.id
            self._person(p, s.x(), s.y(), 8, bool(sel))
            cov = cp.talker_coverage(self.cfg, t.id)
            dot = "#3ddc97" if cov.captured else ("#ff6b81" if cov.excluded_by else "#69739a")
            p.setBrush(QColor(dot))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(s.x() + 8, s.y() - 9), 3.2, 3.2)
            self._label(p, s.x() + 12, s.y() + 4, t.label, "#ffd6ea")
        self._paint_recommendation(p, v=v)
        if getattr(self.state, "mode", "design") == "deploy":
            self._paint_deploy_badges(p, v)
        if getattr(self.state, "mode", "design") == "live":
            self._paint_live_overlay(p, v)
        # connect pending
        if self.state.tool == "connect" and self.connect_from and isinstance(self.hover, QPointF):
            d = next((x for x in self.cfg.devices if x.id == self.connect_from), None)
            if d and d.position:
                a = self.w2s(d.position, v)
                pen = QPen(QColor("#6d8bff"), 2)
                pen.setDashPattern([6, 5])
                p.setPen(pen)
                p.drawLine(a, self.hover)
        # floor-plan calibration rubber-band
        if self.drag and self.drag.get("kind") == "calibrate":
            ca, cb = self.drag["a"], self.drag["b"]
            pen = QPen(QColor("#ffd24a"), 2)
            pen.setDashPattern([6, 4])
            p.setPen(pen)
            p.drawLine(ca, cb)
            aw = self.s2w(ca.x(), ca.y(), v)
            bw = self.s2w(cb.x(), cb.y(), v)
            self._label(p, cb.x() + 8, cb.y(), f"{math.hypot(bw.x - aw.x, bw.y - aw.y):.2f} m", "#ffd24a")

    def _highlight(self, p, s, did, err, warn):
        if did in err or did in warn:
            p.setBrush(QColor(255, 107, 129, 51) if did in err else QColor(247, 201, 72, 51))
            p.setPen(Qt.NoPen)
            p.drawEllipse(s, 16, 16)
        sel = self.state.selection
        if sel and sel.get("kind") == "device" and sel.get("id") == did:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor("#ffffff"), 2))
            p.drawEllipse(s, 14, 14)
        if self.connect_from == did:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor("#6d8bff"), 2))
            p.drawEllipse(s, 14, 14)

    def _draw_routes_2d(self, p, v):
        err, _ = self._error_refs()
        bold = self._profile()["bold_routes"]
        for r in self.cfg.routes:
            fp = cp.find_port(self.cfg, r.from_port_id)
            tp = cp.find_port(self.cfg, r.to_port_id)
            if not fp or not tp:
                continue
            a = next((x for x in self.cfg.devices if x.id == fp.device_id), None)
            b = next((x for x in self.cfg.devices if x.id == tp.device_id), None)
            if not a or not b or not self.device_pos(a) or not self.device_pos(b):
                continue
            A = self.w2s(self.device_pos(a), v)
            B = self.w2s(self.device_pos(b), v)
            color = QColor("#ff6b81") if r.id in err else (QColor("#5fb6ff") if fp.transport == "dante" else QColor("#ffc06a"))
            p.setPen(QPen(color, 3.2 if bold else 2))
            p.drawLine(A, B)
            self._arrowhead(p, A, B, color)
            if bold:
                mid = QPointF((A.x() + B.x()) / 2, (A.y() + B.y()) / 2)
                self._label(p, mid.x() + 5, mid.y() - 4, fp.transport)

    def _paint_deploy_badges(self, p, v):
        """DEPLOY mode: mark devices added (+) or changed (~) since the last deploy."""
        room = self.state.rooms[self.state.active_room]
        base = room.get("last_deployed")
        if base is None:
            return
        key = (id(base), id(self.cfg))
        if self._diff_cache_key != key:
            try:
                self._diff_cache = cp.deployment_diff(base, self.cfg)
            except Exception:
                self._diff_cache = None
            self._diff_cache_key = key
        diff = self._diff_cache
        if diff is None or diff.identical:
            return
        marks = {**{d: ("+", "#3ddc97") for d in diff.devices_added},
                 **{d: ("~", "#f7c948") for d in diff.devices_changed}}
        for d in self.cfg.devices:
            if d.id in marks and d.position:
                glyph, color = marks[d.id]
                s = self.w2s(d.position, v)
                p.setBrush(_qc(color, 230))
                p.setPen(QPen(QColor("#070a12"), 1.4))
                p.drawEllipse(QPointF(s.x() - 13, s.y() - 13), 7.5, 7.5)
                p.setPen(QColor("#070a12"))
                p.setFont(QFont("Segoe UI", 9, QFont.Bold))
                p.drawText(QRectF(s.x() - 20.5, s.y() - 21, 15, 16), Qt.AlignCenter, glyph)

    # ---- LIVE operations overlay (sector wedge + DOA rays + level halo) ----
    def _live_array_pos(self, ov):
        aid = ov.get("array_id")
        d = next((x for x in self.cfg.devices if x.id == aid and x.position), None)
        if d is None:
            d = next((x for x in self.cfg.devices if x.type == "microphoneArray" and x.position), None)
        return d.position if d else None

    @staticmethod
    def _bearing_dir(bearing_deg):
        """Compass bearing (0° = +Y, clockwise) → world-space unit vector."""
        rad = math.radians(bearing_deg)
        return math.sin(rad), math.cos(rad)

    def _paint_live_overlay(self, p, v):
        ov = getattr(self.state, "live_overlay", None)
        if not ov or not ov.get("connected"):
            return
        pos = self._live_array_pos(ov)
        if pos is None:
            return
        c = self.w2s(pos, v)
        # level halo: breathes with the output meter
        lvl = max(0.0, min(1.0, float(ov.get("level") or 0.0)))
        p.setPen(Qt.NoPen)
        p.setBrush(_qc("#ff6b81", 26 + int(lvl * 60)))
        p.drawEllipse(c, 16 + lvl * 26, 16 + lvl * 26)
        # steering sector (auto-steer): drawn relative to the room's +Y "front" —
        # detections are shown front-relative (bearing − front_offset), matching
        # the sector-gate convention in conf_pipeline_control.doa
        sector = ov.get("sector")
        front = sector[2] if sector else 0.0
        if sector:
            center_deg, half_deg, _off = sector
            radius_m = 2.5
            path = QPainterPath(c)
            steps = 24
            for i in range(steps + 1):
                b = center_deg - half_deg + (2 * half_deg) * i / steps
                dx, dy = self._bearing_dir(b)
                path.lineTo(self.w2s(Point2D(pos.x + dx * radius_m, pos.y + dy * radius_m), v))
            path.closeSubpath()
            p.setBrush(_qc("#6d8bff", 34))
            pen = QPen(_qc("#85a0ff", 170), 1.4)
            pen.setDashPattern([4, 3])
            p.setPen(pen)
            p.drawPath(path)
        # DOA rays: green = in-sector (followed), red = outside (nulled)
        for az, sal, in_sector in ov.get("detections") or []:
            dx, dy = self._bearing_dir(az - front)
            ray_m = 3.5
            tip = self.w2s(Point2D(pos.x + dx * ray_m, pos.y + dy * ray_m), v)
            color = "#3ddc97" if in_sector else "#ff6b81"
            alpha = int(max(90, min(255, 130 + sal * 8)))
            p.setPen(QPen(_qc(color, alpha), 2.4))
            p.drawLine(c, tip)
            self._label(p, tip.x() + 4, tip.y(), f"{az:.0f}° · {sal:.0f} dB", color)

    def _arrowhead(self, p, A, B, color):
        ang = math.atan2(B.y() - A.y(), B.x() - A.x())
        # place near B but before the marker
        bx, by = B.x() - math.cos(ang) * 11, B.y() - math.sin(ang) * 11
        p.setBrush(QBrush(color))
        p.setPen(Qt.NoPen)
        poly = QPolygonF([
            QPointF(bx, by),
            QPointF(bx - math.cos(ang - 0.4) * 9, by - math.sin(ang - 0.4) * 9),
            QPointF(bx - math.cos(ang + 0.4) * 9, by - math.sin(ang + 0.4) * 9),
        ])
        p.drawPolygon(poly)

    # ---- 3D ----
    def _paint3d(self, p):
        cam = self.camera()
        minx, miny, maxx, maxy = self.bounds()
        h = self._room_h()
        for x in range(math.ceil(minx), math.floor(maxx) + 1):
            a = self.project(Point3D(x, 0, miny), cam)
            b = self.project(Point3D(x, 0, maxy), cam)
            if a and b:
                p.setPen(QPen(QColor(self._pal()["canvas_axis"] if x == 0 else self._pal()["canvas_grid"]), 1))
                p.drawLine(QPointF(a[0], a[1]), QPointF(b[0], b[1]))
        for z in range(math.ceil(miny), math.floor(maxy) + 1):
            a = self.project(Point3D(minx, 0, z), cam)
            b = self.project(Point3D(maxx, 0, z), cam)
            if a and b:
                p.setPen(QPen(QColor(self._pal()["canvas_axis"] if z == 0 else self._pal()["canvas_grid"]), 1))
                p.drawLine(QPointF(a[0], a[1]), QPointF(b[0], b[1]))
        fp = self.footprint()
        floor = [self.project(Point3D(x, 0, zz), cam) for (x, zz) in fp]
        top = [self.project(Point3D(x, h, zz), cam) for (x, zz) in fp]
        if all(floor):
            p.setBrush(_qc("#6d8bff", 13))
            p.setPen(QPen(QColor("#46568f"), 2))
            p.drawPolygon(QPolygonF([QPointF(s[0], s[1]) for s in floor]))
        p.setPen(QPen(QColor("#2f3b66"), 1.4))
        for i in range(len(fp)):
            if floor[i] and top[i]:
                p.drawLine(QPointF(floor[i][0], floor[i][1]), QPointF(top[i][0], top[i][1]))
        if all(top):
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor("#28335a"), 1.4))
            p.drawPolygon(QPolygonF([QPointF(s[0], s[1]) for s in top]))
        # zones on floor
        for d in self.cfg.devices:
            if d.type != "microphoneArray":
                continue
            for z in d.zones:
                corners = [self.project(Point3D(c.x, 0.02, c.y), cam) for c in self.shape_corners(z.shape)]
                if not all(corners):
                    continue
                fill, stroke, dash, _ = _zone_style(z.type)
                p.setBrush(fill)
                pen = QPen(stroke, 1.3)
                if dash:
                    pen.setDashPattern(dash)
                p.setPen(pen)
                p.drawPolygon(QPolygonF([QPointF(s[0], s[1]) for s in corners]))
        self._draw_routes_3d(p, cam)
        self._angle_rays(p, project=self.project, cam=cam)
        err, warn = self._error_refs()
        # devices + talkers depth-sorted together
        items = []
        for d in self.cfg.devices:
            P = self.dev3(d)
            if not P:
                continue
            s = self.project(P, cam)
            if s:
                items.append((s[2], "device", d, P, s))
        for t in self.cfg.talkers:
            P = self.talker3(t)
            s = self.project(P, cam)
            if s:
                items.append((s[2], "talker", t, P, s))
        items.sort(key=lambda it: -it[0])
        for _, kind, obj, P, s in items:
            foot = self.project(Point3D(P.x, 0, P.z), cam)
            if foot:
                pen = QPen(QColor("#2b3760"), 1)
                pen.setDashPattern([3, 3])
                p.setPen(pen)
                p.drawLine(QPointF(foot[0], foot[1]), QPointF(s[0], s[1]))
            r = max(5, min(20, 0.24 * s[3]))
            if kind == "device":
                color, shape = DEVICE_STYLE[obj.type]
                self._highlight(p, QPointF(s[0], s[1]), obj.id, err, warn)
                outline = QColor("#ff6b81") if obj.id in err else QColor("#070a12")
                self._marker(p, shape, s[0], s[1], r, _qc(color), outline)
                self._label(p, s[0] + r + 3, s[1] + 4, obj.label)
            else:
                sel = self.state.selection and self.state.selection["kind"] == "talker" and self.state.selection["id"] == obj.id
                self._person(p, s[0], s[1], r, bool(sel))
                cov = cp.talker_coverage(self.cfg, obj.id)
                dot = "#3ddc97" if cov.captured else ("#ff6b81" if cov.excluded_by else "#69739a")
                p.setBrush(QColor(dot))
                p.setPen(Qt.NoPen)
                p.drawEllipse(QPointF(s[0] + r * 0.9, s[1] - r), 3, 3)
                self._label(p, s[0] + r + 3, s[1] + 4, obj.label, "#ffd6ea")
        self._paint_recommendation(p, project=self.project, cam=cam)

    def _draw_routes_3d(self, p, cam):
        err, _ = self._error_refs()
        for r in self.cfg.routes:
            fp = cp.find_port(self.cfg, r.from_port_id)
            tp = cp.find_port(self.cfg, r.to_port_id)
            if not fp or not tp:
                continue
            a = next((x for x in self.cfg.devices if x.id == fp.device_id), None)
            b = next((x for x in self.cfg.devices if x.id == tp.device_id), None)
            pa = self.dev3(a) if a else None
            pb = self.dev3(b) if b else None
            if not pa or not pb:
                continue
            sa = self.project(pa, cam)
            sb = self.project(pb, cam)
            if not sa or not sb:
                continue
            color = QColor("#ff6b81") if r.id in err else (QColor("#5fb6ff") if fp.transport == "dante" else QColor("#ffc06a"))
            p.setPen(QPen(color, 1.8))
            p.drawLine(QPointF(sa[0], sa[1]), QPointF(sb[0], sb[1]))

    # --------------------------------------------------------------- live edits
    def _room_verts_live(self):
        if not self.cfg.room:
            return None
        if self.drag and self.drag.get("kind") == "vertex":
            return [self.drag["pos"] if i == self.drag["index"] else p for i, p in enumerate(self.cfg.room.vertices)]
        return self.cfg.room.vertices

    def _zone_shape_live(self, aid, z):
        if self.drag and self.drag.get("kind") in ("zone-move", "zone-resize") and self.drag["array_id"] == aid and self.drag["zone_id"] == z.id:
            return self.drag["shape"]
        return z.shape

    # ------------------------------------------------------------------- input
    def _coord(self, w):
        if self.coord_cb:
            self.coord_cb(f"x {w.x:.2f} , y {w.y:.2f}")

    def mousePressEvent(self, e):
        pos = e.position()
        if self.state.view == "3d":
            return self._down3d(pos)
        self._down2d(pos)

    def mouseMoveEvent(self, e):
        pos = e.position()
        if self.state.view == "3d":
            return self._move3d(pos)
        self._move2d(pos)

    def mouseReleaseEvent(self, e):
        if self.state.view == "3d":
            return self._up3d()
        self._up2d()

    def mouseDoubleClickEvent(self, e):
        if self.state.view == "2d" and self.state.tool == "room":
            pts = []
            for pt in self.draw_pts:
                if not pts or pts[-1].x != pt.x or pts[-1].y != pt.y:
                    pts.append(pt)
            self.draw_pts = []
            self.hover = None
            if len(pts) >= 3:
                from conf_pipeline.model import RoomLayout
                self.state.set_config(cp.set_room(self.cfg, RoomLayout(vertices=pts, height=self._room_h(), units="meters", objects=[])))
            else:
                self.update()

    def wheelEvent(self, e):
        if self.state.view != "3d":
            return
        self.state.cam["dist"] = max(3.0, min(70.0, self.state.cam["dist"] * math.exp(e.angleDelta().y() * 0.0012 * -1)))
        self.update()

    # ---- right-click context menu (2D; geometry editing is a DESIGN job) ----
    def contextMenuEvent(self, e):
        if self.state.view != "2d" or getattr(self.state, "mode", "design") != "design":
            return
        from PySide6.QtWidgets import QMenu
        v = "2d"
        world = self.s2w(e.pos().x(), e.pos().y(), v)
        hit = self._hit_test(world, v)
        menu = QMenu(self)

        if hit and hit["kind"] == "device":
            dev = next((d for d in self.cfg.devices if d.id == hit["id"]), None)
            menu.addAction(f"Edit {dev.label if dev else hit['id']}", lambda: self._ctx_select(hit))
            if dev is not None and dev.type == "microphoneArray":
                menu.addAction("Add coverage zone here", lambda: self._ctx_add_zone(hit["id"], world))
            menu.addSeparator()
            menu.addAction("Delete device", lambda: self._ctx_delete({"kind": "device", "id": hit["id"]}))
        elif hit and hit["kind"] == "talker":
            menu.addAction("Edit talker", lambda: self._ctx_select(hit))
            menu.addAction("Delete talker", lambda: self._ctx_delete({"kind": "talker", "id": hit["id"]}))
        elif hit and hit["kind"] in ("zone-move", "zone-resize"):
            menu.addAction("Edit zone", lambda: self._ctx_select({"kind": "zone", "array_id": hit["array_id"], "zone_id": hit["zone_id"]}))
            menu.addAction("Delete zone", lambda: self._ctx_delete({"kind": "zone", "array_id": hit["array_id"], "zone_id": hit["zone_id"]}))
        else:
            # empty floor — quick-add affordances
            menu.addAction("Place talker here", lambda: self._ctx_add_talker(world))
            if any(d.type == "microphoneArray" for d in self.cfg.devices):
                menu.addAction("Add array here", lambda: self._ctx_add_array(world))
            else:
                menu.addAction("Add mic array here", lambda: self._ctx_add_array(world))
            if self.cfg.room is None:
                menu.addAction("Add rectangular room", lambda: self.state.set_config(cp.set_room(self.cfg, cp.rectangular_room(9, 7, 3))))

        if not menu.isEmpty():
            menu.exec(e.globalPos())

    def _ctx_select(self, sel):
        self.state.select(sel)

    def _ctx_delete(self, sel):
        try:
            if sel["kind"] == "device":
                self.state.set_config(cp.remove_device(self.cfg, sel["id"]))
            elif sel["kind"] == "talker":
                self.state.set_config(cp.remove_talker(self.cfg, sel["id"]))
            elif sel["kind"] == "zone":
                self.state.set_config(cp.remove_coverage_zone(self.cfg, sel["array_id"], sel["zone_id"]))
            self.state.select(None)
        except Exception:
            pass

    def _ctx_add_talker(self, world):
        tid = self.state.next_talker_id()
        cfg = cp.add_talker(self.cfg, cp.create_talker(tid, tid, Point2D(self.snap(world.x), self.snap(world.y))))
        self.state.select({"kind": "talker", "id": tid})
        self.state.set_config(cfg)

    def _ctx_add_array(self, world):
        did = self.state.next_device_id("microphoneArray")
        cfg = cp.add_device(self.cfg, cp.create_microphone_array(did, f"Ceiling Array {did}", "automatic"))
        cfg = cp.set_device_position(cfg, did, Point2D(self.snap(world.x), self.snap(world.y)))
        self.state.select({"kind": "device", "id": did})
        self.state.set_config(cfg)

    def _ctx_add_zone(self, array_id, world):
        zid = self.state.next_zone_id(array_id)
        shape = RectShape(origin=Point2D(self.snap(world.x - 1), self.snap(world.y - 1)), width=2, height=2)
        cfg = cp.add_coverage_zone(self.cfg, array_id, cp.dynamic_zone(zid, f"Records {zid}", shape))
        self.state.select({"kind": "zone", "array_id": array_id, "zone_id": zid})
        self.state.set_config(cfg)

    # ---- 2D input ----
    def _hit_test(self, world, v):
        sp = self.w2s(world, v)

        def near(a, b, px):
            sa, sb = self.w2s(a, v), self.w2s(b, v)
            return math.hypot(sa.x() - sb.x(), sa.y() - sb.y()) < px

        for t in self.cfg.talkers:
            if math.hypot(self.w2s(t.position, v).x() - sp.x(), self.w2s(t.position, v).y() - sp.y()) < 14:
                return {"kind": "talker", "id": t.id, "pos": t.position}
        for d in self.cfg.devices:
            if d.position and math.hypot(self.w2s(d.position, v).x() - sp.x(), self.w2s(d.position, v).y() - sp.y()) < 14:
                return {"kind": "device", "id": d.id, "pos": d.position}
        for d in self.cfg.devices:
            if d.type != "microphoneArray":
                continue
            for z in d.zones:
                if not isinstance(z.shape, RectShape):
                    continue
                c = self.shape_corners(z.shape)
                if near(c[2], world, 10):
                    return {"kind": "zone-resize", "array_id": d.id, "zone_id": z.id, "shape": z.shape}
        for d in self.cfg.devices:
            if d.type != "microphoneArray":
                continue
            for z in d.zones:
                if not isinstance(z.shape, RectShape):
                    continue
                o, w_, h_ = z.shape.origin, z.shape.width, z.shape.height
                if o.x <= world.x <= o.x + w_ and o.y <= world.y <= o.y + h_:
                    return {"kind": "zone-move", "array_id": d.id, "zone_id": z.id, "shape": z.shape, "grab": (world.x - o.x, world.y - o.y)}
        if self.cfg.room:
            for i, vert in enumerate(self.cfg.room.vertices):
                if near(vert, world, 9):
                    return {"kind": "vertex", "index": i, "pos": vert}
        return None

    def _down2d(self, pos):
        if self.state.calibrating:
            self.drag = {"kind": "calibrate", "a": pos, "b": pos}
            return self.update()
        v = self.view2d()
        w = self.s2w(pos.x(), pos.y(), v)
        psnap = Point2D(self.snap(w.x), self.snap(w.y))
        tool = self.state.tool
        try:
            if tool == "select":
                hit = self._hit_test(w, v)
                prof = self._profile()
                drag_ok = hit is not None and (
                    (hit["kind"] == "device" and prof["drag_devices"])
                    or (hit["kind"] == "talker" and prof["drag_talkers"])
                    or (hit["kind"] in ("zone-move", "zone-resize", "vertex") and prof["edit"])
                )
                self.drag = hit if drag_ok and hit["kind"] != "route" else None
                if hit and hit["kind"] == "device":
                    self.state.select({"kind": "device", "id": hit["id"]})
                elif hit and hit["kind"] == "talker":
                    self.state.select({"kind": "talker", "id": hit["id"]})
                elif hit and hit["kind"] in ("zone-move", "zone-resize"):
                    self.state.select({"kind": "zone", "array_id": hit["array_id"], "zone_id": hit["zone_id"]})
                elif not hit:
                    self.state.select(None)
                self.update()
            elif tool == "connect":
                hit = self._hit_test(w, v)
                did = hit["id"] if hit and hit["kind"] == "device" else None
                if not did:
                    return
                self._do_connect(did)
            elif tool == "room":
                self.draw_pts.append(psnap)
                self.update()
            elif tool == "talker":
                tid = self.state.next_talker_id()
                self.state.set_config(cp.add_talker(self.cfg, cp.create_talker(tid, f"Talker {tid}", psnap)))
                self.state.select({"kind": "talker", "id": tid})
            elif tool == "zone":
                aid = self._current_array()
                if not aid:
                    return
                kind = self.state.zone_kind
                if kind == "dedicated":
                    zid = self.state.next_zone_id(aid)
                    self.state.set_config(cp.add_coverage_zone(self.cfg, aid, cp.dedicated_zone(zid, f"Always-on {zid}", psnap)))
                else:
                    self.drag = {"kind": "zone-new", "array_id": aid, "start": psnap, "cur": psnap, "ztype": kind}
                    self.update()
        except Exception as exc:  # surface engine errors without crashing
            self._toast(str(exc))

    def _update_hover_cursor(self, world, v):
        """Cursor feedback so interactive items feel grabbable in the Select tool."""
        if self.state.tool != "select":
            # tool-specific cursors: crosshair for drawing/placing
            self.setCursor(Qt.CrossCursor if self.state.tool in ("room", "zone", "talker") else Qt.ArrowCursor)
            return
        hit = self._hit_test(world, v)
        if hit is None:
            self.setCursor(Qt.ArrowCursor)
        elif hit["kind"] == "zone-resize":
            self.setCursor(Qt.SizeFDiagCursor)
        elif hit["kind"] in ("device", "talker", "vertex", "zone-move"):
            self.setCursor(Qt.OpenHandCursor)
        else:
            self.setCursor(Qt.ArrowCursor)

    def _move2d(self, pos):
        if self.drag and self.drag.get("kind") == "calibrate":
            self.drag["b"] = pos
            return self.update()
        v = self.view2d()
        w = self.s2w(pos.x(), pos.y(), v)
        self._coord(w)
        if self.state.tool == "room":
            self.hover = Point2D(self.snap(w.x), self.snap(w.y))
            return self.update()
        if self.state.tool == "connect" and self.connect_from:
            self.hover = pos
            return self.update()
        if not self.drag:
            self._update_hover_cursor(w, v)
            return
        psnap = Point2D(self.snap(w.x), self.snap(w.y))
        k = self.drag["kind"]
        if k in ("device", "talker", "vertex"):
            self.drag["pos"] = psnap
        elif k == "zone-move":
            o = self.drag["shape"]
            gx, gy = self.drag["grab"]
            self.drag["shape"] = RectShape(origin=Point2D(self.snap(psnap.x - gx), self.snap(psnap.y - gy)), width=o.width, height=o.height)
        elif k == "zone-resize":
            o = self.drag["shape"]
            self.drag["shape"] = RectShape(origin=o.origin, width=max(0.3, self.snap(psnap.x - o.origin.x)), height=max(0.3, self.snap(psnap.y - o.origin.y)))
        elif k == "zone-new":
            self.drag["cur"] = psnap
        self.update()

    def _up2d(self):
        d = self.drag
        self.drag = None
        if not d:
            return
        if d["kind"] == "calibrate":
            return self._finish_calibrate(d)
        try:
            k = d["kind"]
            if k == "device":
                self.state.set_config(cp.set_device_position(self.cfg, d["id"], d["pos"]))
            elif k == "talker":
                self.state.set_config(cp.set_talker_position(self.cfg, d["id"], d["pos"]))
            elif k == "vertex":
                from conf_pipeline.model import RoomLayout
                verts = [d["pos"] if i == d["index"] else p for i, p in enumerate(self.cfg.room.vertices)]
                self.state.set_config(cp.set_room(self.cfg, RoomLayout(vertices=verts, height=self.cfg.room.height, units="meters", objects=list(self.cfg.room.objects))))
            elif k in ("zone-move", "zone-resize"):
                self.state.set_config(cp.set_zone_shape(self.cfg, d["array_id"], d["zone_id"], d["shape"]))
            elif k == "zone-new":
                o = Point2D(min(d["start"].x, d["cur"].x), min(d["start"].y, d["cur"].y))
                w_, h_ = abs(d["cur"].x - d["start"].x), abs(d["cur"].y - d["start"].y)
                if w_ >= 0.3 and h_ >= 0.3:
                    aid = d["array_id"]
                    zid = self.state.next_zone_id(aid)
                    shape = RectShape(origin=o, width=self.snap(w_), height=self.snap(h_))
                    if d["ztype"] == "exclusion":
                        z = cp.exclusion_zone(zid, f"No-pickup {zid}", shape)
                    else:
                        z = cp.dynamic_zone(zid, f"Records {zid}", shape)
                    self.state.set_config(cp.add_coverage_zone(self.cfg, aid, z))
                else:
                    self.update()
        except Exception as exc:
            self._toast(str(exc))
            self.update()

    def _finish_calibrate(self, d):
        from PySide6.QtWidgets import QInputDialog
        self.state.calibrating = False
        v = self.view2d()
        aw = self.s2w(d["a"].x(), d["a"].y(), v)
        bw = self.s2w(d["b"].x(), d["b"].y(), v)
        world_dist = math.hypot(bw.x - aw.x, bw.y - aw.y)
        bg = self.cfg.room.background if self.cfg.room else None
        if bg is None or not bg.scale_m_per_px or world_dist < 1e-6:
            self._toast("Calibration needs a placed floor plan and a longer line.")
            return self.update()
        val, ok = QInputDialog.getDouble(self, "Calibrate scale", "Real length of the drawn line (m):", 1.0, 0.01, 10000.0, 2)
        if not ok:
            return self.update()
        try:
            new_scale = cp.calibrated_scale(bg.scale_m_per_px, world_dist, val)
            self.state.set_config(cp.set_room_background_scale(self.cfg, new_scale))
            self._toast(f"Scale set: 1 px = {new_scale:.4f} m")
        except Exception as exc:
            self._toast(str(exc))
        self.update()

    # ---- 3D input ----
    def _pick3d(self, pos, cam):
        mx_, my_ = pos.x(), pos.y()
        best, bd = None, 18
        for t in self.cfg.talkers:
            s = self.project(self.talker3(t), cam)
            if s and math.hypot(s[0] - mx_, s[1] - my_) < bd:
                bd = math.hypot(s[0] - mx_, s[1] - my_)
                best = ("talker", t.id)
        for d in self.cfg.devices:
            P = self.dev3(d)
            if not P:
                continue
            s = self.project(P, cam)
            if s and math.hypot(s[0] - mx_, s[1] - my_) < bd:
                bd = math.hypot(s[0] - mx_, s[1] - my_)
                best = ("device", d.id)
        return best

    def _down3d(self, pos):
        cam = self.camera()
        if self.state.tool == "connect":
            hit = self._pick3d(pos, cam)
            did = hit[1] if hit and hit[0] == "device" else None
            if not did:
                self.orbit = {"x": pos.x(), "y": pos.y()}
                return
            self._do_connect(did)
            return
        hit = self._pick3d(pos, cam)
        if hit:
            if hit[0] == "talker":
                t = next(x for x in self.cfg.talkers if x.id == hit[1])
                self.state.select({"kind": "talker", "id": hit[1]})
                self.move3 = {"id": hit[1], "type": "talker", "h": self.talker_elev(t)}
            else:
                d = next(x for x in self.cfg.devices if x.id == hit[1])
                self.state.select({"kind": "device", "id": hit[1]})
                self.move3 = {"id": hit[1], "type": "device", "h": self.elev3d(d)}
            return
        self.state.select(None)
        self.orbit = {"x": pos.x(), "y": pos.y()}

    def _move3d(self, pos):
        cam = self.camera()
        if self.move3:
            p = self.ray_floor(pos.x(), pos.y(), cam, self.move3["h"])
            if p:
                self.drag = {"kind": self.move3["type"], "id": self.move3["id"], "pos": Point2D(self.snap(p.x), self.snap(p.y))}
                self._coord(p)
                self.update()
            return
        if self.orbit:
            dx = pos.x() - self.orbit["x"]
            dy = pos.y() - self.orbit["y"]
            self.orbit = {"x": pos.x(), "y": pos.y()}
            self.state.cam["yaw"] -= dx * 0.008
            self.state.cam["pitch"] = max(0.06, min(1.45, self.state.cam["pitch"] + dy * 0.008))
            self.update()

    def _up3d(self):
        if self.move3:
            d = self.drag
            self.move3 = None
            self.drag = None
            if d and d["kind"] == "device":
                self.state.set_config(cp.set_device_position(self.cfg, d["id"], d["pos"]))
            elif d and d["kind"] == "talker":
                self.state.set_config(cp.set_talker_position(self.cfg, d["id"], d["pos"]))
            else:
                self.update()
            return
        self.orbit = None

    # ---- shared ----
    def _do_connect(self, did):
        if not self.connect_from:
            self.connect_from = did
            return self.update()
        if self.connect_from == did:
            self.connect_from = None
            return self.update()
        conn = self._resolve_connection(self.connect_from, did)
        self.connect_from = None
        if not conn:
            return self._toast("No compatible free ports between those devices")
        self.state.set_config(cp.route(self.cfg, conn[0], conn[1]))

    def _resolve_connection(self, from_id, to_id):
        a = next((d for d in self.cfg.devices if d.id == from_id), None)
        b = next((d for d in self.cfg.devices if d.id == to_id), None)
        if not a or not b or a is b:
            return None
        outs = [p for p in a.ports if p.kind == "output"]
        ins = [p for p in b.ports if p.kind == "input"]
        used = {r.to_port_id for r in self.cfg.routes}
        for t in ("dante", "analog"):
            o = next((p for p in outs if p.transport == t), None)
            i = next((p for p in ins if p.transport == t and p.id not in used), None) or next((p for p in ins if p.transport == t), None)
            if o and i:
                return (o.id, i.id)
        return None

    def _current_array(self):
        arrays = [d for d in self.cfg.devices if d.type == "microphoneArray"]
        if not arrays:
            self._toast("Add a microphone array first")
            return None
        sel = self.state.selection
        if sel and sel.get("kind") == "device":
            if any(a.id == sel["id"] for a in arrays):
                return sel["id"]
        return arrays[0].id

    def _toast(self, msg):
        w = self.window()
        if hasattr(w, "toast"):
            w.toast(msg)
