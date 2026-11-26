"""
figure_maker.py - lay out plots, structures, labels, arrows and captions on a page and export it as
a high-resolution image (PNG/TIFF), SVG or PDF, or add it to a report.

Plots arrive as vector graphics from the data windows (Report ▸ Send … to the figure maker), so they
stay sharp at any size.  Structures and other pictures come from files or the clipboard.
Scene units are quarter points (U = 4 per pt), so fonts and line widths match the exported figure.
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import re

from PySide6 import QtCore, QtGui, QtSvg, QtWidgets

from common import APP_NAME, __version__, settings

U = 4.0                       # scene units per point
PT_MM = 25.4 / 72
MM = 72 / 25.4 * U            # scene units per mm
HANDLE = 7.0                  # handle size in screen pixels
GI = QtWidgets.QGraphicsItem.GraphicsItemFlag
BLUE = QtGui.QColor("#2a6fdb")

PAGE_PRESETS = [("Report width (170 × 110 mm)", 170, 110), ("Journal, single column (85 × 70 mm)", 85, 70),
                ("Journal, double column (175 × 100 mm)", 175, 100), ("Square (120 × 120 mm)", 120, 120),
                ("A4 portrait (210 × 297 mm)", 210, 297), ("A4 landscape (297 × 210 mm)", 297, 210),
                ("Slide 16:9 (254 × 143 mm)", 254, 143)]


def _handle_size(item):
    """Handle size in item units so handles look the same at every zoom."""
    sc = item.scene()
    if sc is None or not sc.views():
        return HANDLE * 2
    v = sc.views()[0]
    return HANDLE / max(v.transform().m11(), 1e-6)


def _svg_size_pt(data: bytes):
    """Natural size of an SVG in points from its width/height attributes (px assumed at 96 dpi)."""
    head = data[:4000].decode("utf-8", "ignore")
    m = re.search(r"<svg\b[^>]*>", head, re.S)
    root = m.group(0) if m else ""
    k = {"pt": 1.0, "px": 0.75, "mm": 72 / 25.4, "cm": 72 / 2.54, "in": 72.0, "pc": 12.0, "": 0.75}

    def attr(name):
        a = re.search(rf'\b{name}\s*=\s*"([\d.]+)\s*([a-z%]*)"', root)
        if a and a.group(2) != "%":
            return float(a.group(1)) * k.get(a.group(2), 0.75)
        return None
    w, h = attr("width"), attr("height")
    if w and h:
        return w, h
    r = QtSvg.QSvgRenderer(QtCore.QByteArray(data))
    vb = r.viewBoxF()
    if vb.width() > 0:
        return vb.width() * 0.75, vb.height() * 0.75
    s = r.defaultSize()
    return s.width() * 0.75, s.height() * 0.75


# =============================================================================
# items
class PictureItem(QtWidgets.QGraphicsObject):
    """A plot (SVG, vector) or picture (PNG/JPEG) drawn into a resizable box."""

    def __init__(self, data: bytes, fmt: str, w=None, h=None, meta=None):
        super().__init__()
        self.data, self.fmt = data, fmt
        self.meta = dict(meta or {})          # e.g. {"ket": ...} for structures drawn in the editor
        self._load(data, fmt, w, h)
        self.setFlags(GI.ItemIsMovable | GI.ItemIsSelectable | GI.ItemSendsGeometryChanges)
        self.setAcceptHoverEvents(True)
        self._resizing = False

    def _load(self, data, fmt, w, h):
        self.svg = None
        self.img = None
        if fmt == "svg":
            self.svg = QtSvg.QSvgRenderer(QtCore.QByteArray(data))
            wpt, hpt = _svg_size_pt(data)
        else:
            self.img = QtGui.QImage.fromData(data)
            dpm = self.img.dotsPerMeterX()
            dpi = dpm * 0.0254 if dpm > 0 else 96
            if dpi < 72:
                dpi = 96
            wpt, hpt = self.img.width() / dpi * 72, self.img.height() / dpi * 72
        self.aspect = (wpt / hpt) if hpt else 1.0
        self.nat_w = wpt * U
        self.w = w if w is not None else wpt * U
        self.h = h if h is not None else hpt * U

    def replace_svg(self, data, ket):
        """New drawing from the structure editor, kept at the same scale."""
        k = self.w / self.nat_w if self.nat_w else 1.0
        self.prepareGeometryChange()
        self.data, self.fmt = data, "svg"
        self.meta["ket"] = ket
        self._load(data, "svg", None, None)
        self.w, self.h = self.nat_w * k, self.nat_w * k / self.aspect
        self.update()

    def mouseDoubleClickEvent(self, ev):
        if self.meta.get("ket"):
            import structure_editor
            structure_editor.open_editor(self)
            ev.accept()
            return
        super().mouseDoubleClickEvent(ev)

    def boundingRect(self):
        hs = _handle_size(self)
        return QtCore.QRectF(-hs, -hs, self.w + 2 * hs, self.h + 2 * hs)

    def _handle_rect(self):
        hs = _handle_size(self)
        return QtCore.QRectF(self.w - hs / 2, self.h - hs / 2, hs, hs)

    def paint(self, p, opt, widget=None):
        r = QtCore.QRectF(0, 0, self.w, self.h)
        if self.svg is not None:
            self.svg.render(p, r)
        elif self.img is not None:
            p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
            p.drawImage(r, self.img)
        if self.isSelected() and not getattr(self.scene(), "exporting", False):
            pen = QtGui.QPen(BLUE, 0, QtCore.Qt.DashLine)
            pen.setCosmetic(True)
            p.setPen(pen)
            p.setBrush(QtCore.Qt.NoBrush)
            p.drawRect(r)
            p.setBrush(QtGui.QColor("white"))
            pen.setStyle(QtCore.Qt.SolidLine)
            p.setPen(pen)
            p.drawRect(self._handle_rect())

    def hoverMoveEvent(self, ev):
        on = self.isSelected() and self._handle_rect().contains(ev.pos())
        self.setCursor(QtCore.Qt.SizeFDiagCursor if on else QtCore.Qt.SizeAllCursor)

    def mousePressEvent(self, ev):
        if ev.button() == QtCore.Qt.LeftButton and self.isSelected() and self._handle_rect().contains(ev.pos()):
            self._resizing = True
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._resizing:
            w = max(10.0, ev.pos().x())
            h = max(10.0, ev.pos().y()) if ev.modifiers() & QtCore.Qt.ShiftModifier else w / self.aspect
            self.set_size(w, h)
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self._resizing:
            self._resizing = False
            if self.scene() is not None:
                self.scene().edited()
            return
        super().mouseReleaseEvent(ev)

    def set_size(self, w, h=None):
        self.prepareGeometryChange()
        self.w = w
        self.h = h if h is not None else w / self.aspect
        self.update()

    def to_dict(self):
        return dict(type="picture", fmt=self.fmt, data=self.data, x=self.x(), y=self.y(), w=self.w, h=self.h,
                    z=self.zValue(), meta=dict(self.meta))


class ArrowItem(QtWidgets.QGraphicsObject):
    STYLES = [("single", "→  Arrow"), ("double", "↔  Double-headed (resonance)"),
              ("equilibrium", "⇌  Equilibrium (e.g. keto–enol)"), ("retro", "⇒  Retrosynthesis"),
              ("line", "—  Line")]

    def __init__(self, a, b, style="single", width_pt=1.0, color="#000000"):
        super().__init__()
        self.a, self.b = QtCore.QPointF(a), QtCore.QPointF(b)
        self.style = style
        self.width_pt = width_pt
        self.color = QtGui.QColor(color)
        self.setFlags(GI.ItemIsMovable | GI.ItemIsSelectable)
        self.setAcceptHoverEvents(True)
        self._drag = None

    # geometry ------------------------------------------------------------
    def _head(self):
        return max(6.0, self.width_pt * 6.0) * U

    def _gap(self):
        return max(3.0, self.width_pt * 3.2) * U

    def _path(self):
        a, b = self.a, self.b
        dx, dy = b.x() - a.x(), b.y() - a.y()
        L = math.hypot(dx, dy) or 1.0
        ux, uy = dx / L, dy / L
        nx, ny = -uy, ux
        hl = self._head()
        hw = hl * 0.42
        path = QtGui.QPainterPath()
        fill = QtGui.QPainterPath()

        def head(tip, sx, sy, half=None):
            # sx, sy: direction the arrow points; half = +1/-1 for a one-sided barb
            base = QtCore.QPointF(tip.x() - sx * hl, tip.y() - sy * hl)
            px_, py_ = -sy, sx
            p1 = QtCore.QPointF(base.x() + px_ * hw, base.y() + py_ * hw)
            p2 = QtCore.QPointF(base.x() - px_ * hw, base.y() - py_ * hw)
            if half is not None:
                barb = p1 if half > 0 else p2
                path.moveTo(tip)
                path.lineTo(barb)
                return tip
            poly = QtGui.QPolygonF([tip, p1, QtCore.QPointF(tip.x() - sx * hl * 0.75, tip.y() - sy * hl * 0.75), p2])
            fill.addPolygon(poly)
            fill.closeSubpath()
            return QtCore.QPointF(tip.x() - sx * hl * 0.6, tip.y() - sy * hl * 0.6)

        if self.style == "equilibrium":
            g = self._gap() / 2
            a1, b1 = QtCore.QPointF(a.x() + nx * -g, a.y() + ny * -g), QtCore.QPointF(b.x() + nx * -g, b.y() + ny * -g)
            a2, b2 = QtCore.QPointF(a.x() + nx * g, a.y() + ny * g), QtCore.QPointF(b.x() + nx * g, b.y() + ny * g)
            path.moveTo(a1)
            path.lineTo(b1)
            head(b1, ux, uy, half=-1)
            path.moveTo(b2)
            path.lineTo(a2)
            head(a2, -ux, -uy, half=-1)
        elif self.style == "retro":
            g = self._gap() / 2
            end = QtCore.QPointF(b.x() - ux * g * 1.2, b.y() - uy * g * 1.2)
            for s in (-1, 1):
                path.moveTo(QtCore.QPointF(a.x() + nx * g * s, a.y() + ny * g * s))
                path.lineTo(QtCore.QPointF(end.x() + nx * g * s, end.y() + ny * g * s))
            w2 = g * 2.6
            path.moveTo(QtCore.QPointF(b.x() - ux * w2 + nx * w2, b.y() - uy * w2 + ny * w2))
            path.lineTo(b)
            path.lineTo(QtCore.QPointF(b.x() - ux * w2 - nx * w2, b.y() - uy * w2 - ny * w2))
        else:
            s, e = QtCore.QPointF(a), QtCore.QPointF(b)
            if self.style in ("single", "double"):
                e = head(b, ux, uy)
            if self.style == "double":
                s = head(a, -ux, -uy)
            path.moveTo(s)
            path.lineTo(e)
        return path, fill

    def boundingRect(self):
        m = self._head() + self._gap() + _handle_size(self)
        return QtCore.QRectF(self.a, self.b).normalized().adjusted(-m, -m, m, m)

    def shape(self):
        path, fill = self._path()
        st = QtGui.QPainterPathStroker()
        st.setWidth(max(self.width_pt * U, 4 * U) + 4 * U)
        s = st.createStroke(path)
        s.addPath(fill)
        if self.isSelected():
            hs = _handle_size(self)
            for q in (self.a, self.b):
                s.addRect(QtCore.QRectF(q.x() - hs, q.y() - hs, 2 * hs, 2 * hs))
        return s

    def paint(self, p, opt, widget=None):
        path, fill = self._path()
        pen = QtGui.QPen(self.color, self.width_pt * U, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap,
                         QtCore.Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(QtCore.Qt.NoBrush)
        p.drawPath(path)
        if not fill.isEmpty():
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(self.color)
            p.drawPath(fill)
        if self.isSelected() and not getattr(self.scene(), "exporting", False):
            hs = _handle_size(self)
            hp = QtGui.QPen(BLUE, 0)
            hp.setCosmetic(True)
            p.setPen(hp)
            p.setBrush(QtGui.QColor("white"))
            for q in (self.a, self.b):
                p.drawRect(QtCore.QRectF(q.x() - hs / 2, q.y() - hs / 2, hs, hs))

    # interaction ---------------------------------------------------------
    def _which(self, pos):
        if not self.isSelected():
            return None
        hs = _handle_size(self)
        for name, q in (("a", self.a), ("b", self.b)):
            if abs(pos.x() - q.x()) <= hs and abs(pos.y() - q.y()) <= hs:
                return name
        return None

    def hoverMoveEvent(self, ev):
        self.setCursor(QtCore.Qt.CrossCursor if self._which(ev.pos()) else QtCore.Qt.SizeAllCursor)

    def mousePressEvent(self, ev):
        w = self._which(ev.pos()) if ev.button() == QtCore.Qt.LeftButton else None
        if w:
            self._drag = w
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._drag:
            other = self.b if self._drag == "a" else self.a
            q = snap_angle(other, ev.pos(), ev.modifiers() & QtCore.Qt.ShiftModifier)
            self.prepareGeometryChange()
            setattr(self, self._drag, q)
            self.update()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self._drag:
            self._drag = None
            if self.scene() is not None:
                self.scene().edited()
            return
        super().mouseReleaseEvent(ev)

    def to_dict(self):
        return dict(type="arrow", x=self.x(), y=self.y(), ax=self.a.x(), ay=self.a.y(), bx=self.b.x(),
                    by=self.b.y(), style=self.style, width=self.width_pt, color=self.color.name(), z=self.zValue())


def snap_angle(origin, p, free):
    """Snap the line origin->p to 15° steps unless Shift (free) is held."""
    if free:
        return QtCore.QPointF(p)
    dx, dy = p.x() - origin.x(), p.y() - origin.y()
    L = math.hypot(dx, dy)
    if L == 0:
        return QtCore.QPointF(p)
    ang = round(math.atan2(dy, dx) / (math.pi / 12)) * (math.pi / 12)
    return QtCore.QPointF(origin.x() + L * math.cos(ang), origin.y() + L * math.sin(ang))


class TextItem(QtWidgets.QGraphicsTextItem):
    """Label or caption. Double-click to edit; Ctrl+B/I, Ctrl+= subscript, Ctrl+Shift+= superscript."""

    def __init__(self, html_="", size_pt=10, width=-1):
        super().__init__()
        f = QtGui.QFont("Arial")
        f.setPixelSize(int(round(size_pt * U)))
        f.setHintingPreference(QtGui.QFont.PreferNoHinting)
        self.setFont(f)
        self.document().setDocumentMargin(U)
        if html_:
            self.setHtml(html_)
        self.setTextWidth(width)
        self.setFlags(GI.ItemIsMovable | GI.ItemIsSelectable | GI.ItemIsFocusable)
        self.setTextInteractionFlags(QtCore.Qt.NoTextInteraction)
        self._before = None

    def size_pt(self):
        return self.font().pixelSize() / U

    def set_size_pt(self, pt):
        f = self.font()
        f.setPixelSize(int(round(pt * U)))
        self.setFont(f)

    def mouseDoubleClickEvent(self, ev):
        if self.textInteractionFlags() == QtCore.Qt.NoTextInteraction:
            self._before = self.toHtml()
            self.setTextInteractionFlags(QtCore.Qt.TextEditorInteraction)
            self.setFocus(QtCore.Qt.MouseFocusReason)
            c = self.textCursor()
            c.select(QtGui.QTextCursor.Document)
            self.setTextCursor(c)
        super().mouseDoubleClickEvent(ev)

    def editing(self):
        return self.textInteractionFlags() != QtCore.Qt.NoTextInteraction

    def focusOutEvent(self, ev):
        super().focusOutEvent(ev)
        self.finish_editing()

    def finish_editing(self):
        if not self.editing():
            return
        self.setTextInteractionFlags(QtCore.Qt.NoTextInteraction)
        c = self.textCursor()
        c.clearSelection()
        self.setTextCursor(c)
        sc = self.scene()
        if sc is not None:
            if not self.toPlainText().strip():
                sc.removeItem(self)
                sc.edited()
            elif self.toHtml() != self._before:
                sc.edited()

    def keyPressEvent(self, ev):
        mods = ev.modifiers()
        ctrl = mods & QtCore.Qt.ControlModifier
        if ev.key() == QtCore.Qt.Key_Escape:
            self.clearFocus()
            return
        if ctrl and ev.key() in (QtCore.Qt.Key_Equal, QtCore.Qt.Key_Plus):
            self.vertical("super" if (mods & QtCore.Qt.ShiftModifier or ev.key() == QtCore.Qt.Key_Plus) else "sub")
            return
        if ctrl and ev.key() == QtCore.Qt.Key_B:
            self.toggle("bold")
            return
        if ctrl and ev.key() == QtCore.Qt.Key_I:
            self.toggle("italic")
            return
        super().keyPressEvent(ev)

    def _cursor(self):
        c = self.textCursor()
        if not self.editing() or not c.hasSelection():
            c = QtGui.QTextCursor(self.document())
            c.select(QtGui.QTextCursor.Document)
        return c

    def toggle(self, what):
        c = self._cursor()
        cf = c.charFormat()
        f = QtGui.QTextCharFormat()
        if what == "bold":
            f.setFontWeight(QtGui.QFont.Normal if cf.fontWeight() >= 600 else QtGui.QFont.Bold)
        else:
            f.setFontItalic(not cf.fontItalic())
        c.mergeCharFormat(f)
        if not self.editing() and self.scene():
            self.scene().edited()

    def vertical(self, kind):
        VA = QtGui.QTextCharFormat.VerticalAlignment
        c = self._cursor()
        want = VA.AlignSuperScript if kind == "super" else VA.AlignSubScript
        f = QtGui.QTextCharFormat()
        f.setVerticalAlignment(VA.AlignNormal if c.charFormat().verticalAlignment() == want else want)
        c.mergeCharFormat(f)

    def set_color(self, col):
        self.setDefaultTextColor(col)
        c = QtGui.QTextCursor(self.document())
        c.select(QtGui.QTextCursor.Document)
        f = QtGui.QTextCharFormat()
        f.setForeground(col)
        c.mergeCharFormat(f)

    def paint(self, p, opt, widget=None):
        if getattr(self.scene(), "exporting", False):
            opt = QtWidgets.QStyleOptionGraphicsItem(opt)
            opt.state &= ~QtWidgets.QStyle.State_Selected
            opt.state &= ~QtWidgets.QStyle.State_HasFocus
        super().paint(p, opt, widget)

    def to_dict(self):
        return dict(type="text", html=self.toHtml(), size=self.size_pt(), x=self.x(), y=self.y(),
                    width=self.textWidth(), z=self.zValue(), color=self.defaultTextColor().name())


# =============================================================================
class FigureScene(QtWidgets.QGraphicsScene):
    sigEdited = QtCore.Signal()

    def __init__(self):
        super().__init__()
        self.exporting = False
        self.page = QtWidgets.QGraphicsRectItem()
        self.page.setBrush(QtGui.QColor("white"))
        self.page.setPen(QtGui.QPen(QtGui.QColor("#9a9a9a"), 0))
        self.page.setZValue(-1e9)
        shadow = QtWidgets.QGraphicsDropShadowEffect()
        shadow.setBlurRadius(12)
        shadow.setOffset(2, 2)
        shadow.setColor(QtGui.QColor(0, 0, 0, 70))
        self.page.setGraphicsEffect(shadow)
        self.addItem(self.page)
        self.set_page(170, 110)
        self.setBackgroundBrush(QtGui.QColor("#e4e6ea"))
        self._press_pos = {}
        self.arrow_mode = None
        self._new_arrow = None

    def set_page(self, w_mm, h_mm):
        self.w_mm, self.h_mm = float(w_mm), float(h_mm)
        r = QtCore.QRectF(0, 0, w_mm * MM, h_mm * MM)
        self.page.setRect(r)
        m = 30 * MM
        self.setSceneRect(r.adjusted(-m, -m, m, m))

    def page_rect(self):
        return self.page.rect()

    def content_items(self):
        return [it for it in self.items() if isinstance(it, (PictureItem, ArrowItem, TextItem))]

    def edited(self):
        self.sigEdited.emit()

    # moves -> one undo step
    def mousePressEvent(self, ev):
        if self.arrow_mode and ev.button() == QtCore.Qt.LeftButton:
            p = ev.scenePos()
            self._new_arrow = ArrowItem(QtCore.QPointF(0, 0), QtCore.QPointF(0, 0), self.arrow_mode)
            self._new_arrow.setPos(p)
            self._new_arrow.setZValue(self.top_z() + 1)
            self.addItem(self._new_arrow)
            ev.accept()
            return
        super().mousePressEvent(ev)
        self._press_pos = {it: it.pos() for it in self.selectedItems()}

    def mouseMoveEvent(self, ev):
        if self._new_arrow is not None:
            a = self._new_arrow
            q = snap_angle(QtCore.QPointF(0, 0), ev.scenePos() - a.pos(), ev.modifiers() & QtCore.Qt.ShiftModifier)
            a.prepareGeometryChange()
            a.b = q
            a.update()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self._new_arrow is not None:
            a = self._new_arrow
            self._new_arrow = None
            if math.hypot(a.b.x(), a.b.y()) < 6 * U:
                a.b = QtCore.QPointF(30 * MM / 2.0, 0)          # a click makes a 15 mm arrow
            self.clearSelection()
            a.setSelected(True)
            self.arrow_mode = None
            for v in self.views():
                v.viewport().setCursor(QtCore.Qt.ArrowCursor)
                if hasattr(v, "sigArrowDone"):
                    v.sigArrowDone.emit()
            self.edited()
            return
        super().mouseReleaseEvent(ev)
        moved = any(it.scene() is self and it.pos() != p for it, p in self._press_pos.items())
        self._press_pos = {}
        if moved:
            self.edited()

    def top_z(self):
        zs = [it.zValue() for it in self.content_items()]
        return max(zs) if zs else 0.0


class FigureView(QtWidgets.QGraphicsView):
    sigArrowDone = QtCore.Signal()

    def __init__(self, scene):
        super().__init__(scene)
        self.setRenderHints(QtGui.QPainter.Antialiasing | QtGui.QPainter.TextAntialiasing |
                            QtGui.QPainter.SmoothPixmapTransform)
        self.setDragMode(QtWidgets.QGraphicsView.RubberBandDrag)
        self.setViewportUpdateMode(QtWidgets.QGraphicsView.FullViewportUpdate)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.AnchorUnderMouse)
        self.setAcceptDrops(True)

    def wheelEvent(self, ev):
        if ev.modifiers() & QtCore.Qt.ControlModifier:
            f = 1.15 ** (ev.angleDelta().y() / 120)
            self.scale(f, f)
            ev.accept()
            return
        super().wheelEvent(ev)

    def fit(self):
        r = self.scene().page_rect()
        m = 6 * MM
        self.fitInView(r.adjusted(-m, -m, m, m), QtCore.Qt.KeepAspectRatio)

    def actual_size(self):
        # 1 mm on paper = 1 mm on screen (approximately)
        dpi = self.logicalDpiX() or 96
        k = dpi / 72 / U
        self.setTransform(QtGui.QTransform.fromScale(k, k))

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() or e.mimeData().hasImage():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        e.acceptProposedAction()

    def dropEvent(self, e):
        w = self.window()
        pos = self.mapToScene(e.position().toPoint())
        for u in e.mimeData().urls():
            if u.toLocalFile():
                w.insert_file(u.toLocalFile(), pos)
        if not e.mimeData().urls() and e.mimeData().hasImage():
            w.insert_qimage(QtGui.QImage(e.mimeData().imageData()), pos)


# =============================================================================
def item_from_dict(d):
    t = d.get("type")
    if t == "picture":
        it = PictureItem(d["data"], d["fmt"], d["w"], d["h"], d.get("meta"))
    elif t == "arrow":
        it = ArrowItem(QtCore.QPointF(d["ax"], d["ay"]), QtCore.QPointF(d["bx"], d["by"]), d.get("style", "single"),
                       d.get("width", 1.0), d.get("color", "#000000"))
    elif t == "text":
        it = TextItem("", d.get("size", 10), d.get("width", -1))
        it.setHtml(d.get("html", ""))
        it.setDefaultTextColor(QtGui.QColor(d.get("color", "#000000")))
    else:
        return None
    it.setPos(d.get("x", 0), d.get("y", 0))
    it.setZValue(d.get("z", 0))
    return it


class ExportDialog(QtWidgets.QDialog):
    def __init__(self, parent, s):
        super().__init__(parent)
        self.setWindowTitle("Export figure")
        self.s = s
        f = QtWidgets.QFormLayout(self)
        self.fmt = QtWidgets.QComboBox()
        self.fmt.addItems(["PNG", "TIFF", "SVG (vector)", "PDF (vector)"])
        self.fmt.setCurrentIndex(int(s.value("fig/fmt", 0)))
        self.dpi = QtWidgets.QSpinBox()
        self.dpi.setRange(72, 2400)
        self.dpi.setSingleStep(100)
        self.dpi.setValue(int(s.value("fig/dpi", 600)))
        self.dpi.setSuffix(" dpi")
        self.px = QtWidgets.QLabel()
        f.addRow("Format", self.fmt)
        f.addRow("Resolution", self.dpi)
        f.addRow("Size", self.px)
        self.fmt.currentIndexChanged.connect(self._upd)
        self.dpi.valueChanged.connect(self._upd)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)
        self._upd()

    def _upd(self):
        sc = self.parent().scene
        vec = self.fmt.currentIndex() >= 2
        self.dpi.setEnabled(not vec)
        self.px.setText(f"{sc.w_mm:g} × {sc.h_mm:g} mm" + ("" if vec else
                        f"  =  {round(sc.w_mm / 25.4 * self.dpi.value())} × "
                        f"{round(sc.h_mm / 25.4 * self.dpi.value())} px"))

    def result_opts(self):
        self.s.setValue("fig/fmt", self.fmt.currentIndex())
        self.s.setValue("fig/dpi", self.dpi.value())
        return [".png", ".tif", ".svg", ".pdf"][self.fmt.currentIndex()], self.dpi.value()


# =============================================================================
class FigureWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = settings()
        self.path = None
        self.dirty = False
        self.undo_stack, self.redo_stack = [], []
        self._restoring = False
        self.scene = FigureScene()
        self.view = FigureView(self.scene)
        self.setCentralWidget(self.view)
        self.scene.sigEdited.connect(self._edited)
        self.scene.selectionChanged.connect(self._selection_changed)
        self.view.sigArrowDone.connect(lambda: [b.setChecked(False) for b in self.arrow_buttons])
        self._build()
        self._snapshot_now = self.snapshot()
        self.resize(1300, 860)
        self._update_title()
        QtCore.QTimer.singleShot(0, self.view.fit)

    # ---------------------------------------------------------------- UI
    def _build(self):
        mb = self.menuBar()
        fm = mb.addMenu("&File")
        fm.addAction("New figure", self.new_figure).setShortcut("Ctrl+N")
        fm.addAction("Open figure…", self.open_figure).setShortcut("Ctrl+O")
        fm.addAction("Save figure", self.save_figure).setShortcut("Ctrl+S")
        fm.addAction("Save figure as…", lambda: self.save_figure(True)).setShortcut("Ctrl+Shift+S")
        fm.addSeparator()
        fm.addAction("Export image…", self.export_dialog).setShortcut("Ctrl+E")
        fm.addAction("Copy figure as image", self.copy_image).setShortcut("Ctrl+Shift+C")
        fm.addAction("Add figure to report", self.add_to_report).setShortcut("Ctrl+R")
        fm.addSeparator()
        fm.addAction("Close", self.close)
        em = mb.addMenu("&Edit")
        em.addAction("Undo", self.undo).setShortcut(QtGui.QKeySequence.Undo)
        em.addAction("Redo", self.redo).setShortcut(QtGui.QKeySequence.Redo)
        em.addSeparator()
        em.addAction("Paste picture / text", self.paste).setShortcut(QtGui.QKeySequence.Paste)
        em.addAction("Duplicate", self.duplicate).setShortcut("Ctrl+D")
        a = em.addAction("Delete", self.delete_selected)
        a.setShortcuts([QtGui.QKeySequence.Delete, QtGui.QKeySequence("Backspace")])
        em.addAction("Select all", lambda: [it.setSelected(True) for it in self.scene.content_items()]) \
            .setShortcut(QtGui.QKeySequence.SelectAll)
        em.addSeparator()
        em.addAction("Bring to front", lambda: self.restack(1)).setShortcut("Ctrl+]")
        em.addAction("Send to back", lambda: self.restack(-1)).setShortcut("Ctrl+[")
        im = mb.addMenu("&Insert")
        im.addAction("Draw structure…", self.open_structure_editor).setShortcut("Ctrl+K")
        im.addAction("Picture / structure from file…", self.insert_file_dialog).setShortcut("Ctrl+I")
        im.addAction("Label (text)", lambda: self.insert_text()).setShortcut("Ctrl+T")
        im.addAction("Caption", self.insert_caption)
        for key, name in ArrowItem.STYLES:
            im.addAction(f"Arrow: {name}", lambda k=key: self.start_arrow(k))
        vm = mb.addMenu("&View")
        vm.addAction("Fit page", self.view.fit).setShortcut("Ctrl+0")
        vm.addAction("Actual size", self.view.actual_size).setShortcut("Ctrl+1")
        vm.addAction("Zoom in", lambda: self.view.scale(1.25, 1.25)).setShortcut(QtGui.QKeySequence.ZoomIn)
        vm.addAction("Zoom out", lambda: self.view.scale(0.8, 0.8)).setShortcut(QtGui.QKeySequence.ZoomOut)
        hm = mb.addMenu("&Help")
        hm.addAction("How to make a figure", self.show_help).setShortcut("F1")

        tb = self.addToolBar("Insert")
        tb.setMovable(False)
        tb.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
        tb.addAction("Picture…", self.insert_file_dialog).setToolTip(
            "Insert a structure or picture (PNG, JPEG, SVG). Plots come from the data windows: "
            "Report ▸ Send … to the figure maker.")
        tb.addAction("Draw structure…", self.open_structure_editor).setToolTip(
            "Draw a structure or reaction scheme (e.g. keto ⇌ enol) in the structure editor")
        tb.addAction("Paste", self.paste).setToolTip("Paste a picture (e.g. a structure copied from a drawing "
                                                     "program) or text")
        tb.addAction("Label", lambda: self.insert_text()).setToolTip("Add a text label; double-click to edit it")
        tb.addAction("Caption", self.insert_caption).setToolTip("Add a caption across the bottom of the page")
        tb.addSeparator()
        self.arrow_buttons = []
        for key, name in ArrowItem.STYLES:
            sym, desc = name.split("  ", 1)
            b = QtWidgets.QToolButton()
            b.setText(sym)
            b.setCheckable(True)
            b.setToolTip(f"{desc}: drag on the page to draw it (Shift = any angle)")
            f = b.font()
            f.setPointSize(f.pointSize() + 3)
            b.setFont(f)
            b.toggled.connect(lambda on, k=key, b=b: self.start_arrow(k) if on else None)
            tb.addWidget(b)
            self.arrow_buttons.append(b)
        tb.addSeparator()
        tb.addAction("Export…", self.export_dialog)
        tb.addAction("Add to report", self.add_to_report)
        tb.addAction("Copy", self.copy_image)

        # properties dock
        dock = QtWidgets.QDockWidget("Properties", self)
        dock.setFeatures(QtWidgets.QDockWidget.NoDockWidgetFeatures)
        pw = QtWidgets.QWidget()
        pv = QtWidgets.QVBoxLayout(pw)
        page = QtWidgets.QGroupBox("Page")
        pf = QtWidgets.QFormLayout(page)
        self.preset = QtWidgets.QComboBox()
        self.preset.addItem("Custom")
        for name, w, h in PAGE_PRESETS:
            self.preset.addItem(name, (w, h))
        self.preset.activated.connect(self._preset_chosen)
        self.page_w = QtWidgets.QDoubleSpinBox()
        self.page_h = QtWidgets.QDoubleSpinBox()
        for sp in (self.page_w, self.page_h):
            sp.setRange(20, 600)
            sp.setDecimals(0)
            sp.setSuffix(" mm")
            sp.editingFinished.connect(self._page_size_edited)
        self.page_w.setValue(170)
        self.page_h.setValue(110)
        pf.addRow(self.preset)
        hb = QtWidgets.QHBoxLayout()
        hb.addWidget(self.page_w)
        hb.addWidget(QtWidgets.QLabel("×"))
        hb.addWidget(self.page_h)
        pf.addRow("Size", hb)
        b = QtWidgets.QPushButton("Fit page to contents")
        b.setToolTip("Shrink or grow the page to the items on it, with a small margin")
        b.clicked.connect(self.fit_page_to_contents)
        pf.addRow(b)
        pv.addWidget(page)

        self.props = QtWidgets.QStackedWidget()
        none = QtWidgets.QLabel("Select an item to change it.\n\nDrag items to move them; drag the small square "
                                "of a picture to resize it, or the ends of an arrow to point it.")
        none.setWordWrap(True)
        none.setStyleSheet("color:#666;")
        none.setAlignment(QtCore.Qt.AlignTop)
        self.props.addWidget(none)
        # picture
        w = QtWidgets.QGroupBox("Picture")
        f = QtWidgets.QFormLayout(w)
        self.pic_w = QtWidgets.QDoubleSpinBox()
        self.pic_w.setRange(5, 600)
        self.pic_w.setSuffix(" mm")
        self.pic_w.setDecimals(1)
        self.pic_w.editingFinished.connect(self._pic_width_edited)
        f.addRow("Width", self.pic_w)
        b = QtWidgets.QPushButton("Fit to page width")
        b.clicked.connect(self._pic_fit_width)
        f.addRow(b)
        b = QtWidgets.QPushButton("Restore proportions")
        b.clicked.connect(self._pic_restore_aspect)
        f.addRow(b)
        self.props.addWidget(w)
        # text
        w = QtWidgets.QGroupBox("Text")
        f = QtWidgets.QFormLayout(w)
        self.txt_size = QtWidgets.QSpinBox()
        self.txt_size.setRange(4, 72)
        self.txt_size.setSuffix(" pt")
        self.txt_size.valueChanged.connect(self._txt_size_edited)
        f.addRow("Size", self.txt_size)
        hb = QtWidgets.QHBoxLayout()
        for txt, fn in (("B", lambda: self._txt("bold")), ("I", lambda: self._txt("italic")),
                        ("x²", lambda: self._txt("super")), ("x₂", lambda: self._txt("sub")),
                        ("Colour…", self._txt_colour)):
            b = QtWidgets.QPushButton(txt)
            b.setMaximumWidth(60)
            b.clicked.connect(fn)
            hb.addWidget(b)
        f.addRow(hb)
        self.txt_wrap = QtWidgets.QCheckBox("Wrap to a fixed width")
        self.txt_wrap.toggled.connect(self._txt_wrap)
        f.addRow(self.txt_wrap)
        hint = QtWidgets.QLabel("Double-click the text to edit it. Select characters first to format part of it "
                                "(Ctrl+= subscript, Ctrl+Shift+= superscript).")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#666;")
        f.addRow(hint)
        self.props.addWidget(w)
        # arrow
        w = QtWidgets.QGroupBox("Arrow")
        f = QtWidgets.QFormLayout(w)
        self.arr_style = QtWidgets.QComboBox()
        for key, name in ArrowItem.STYLES:
            self.arr_style.addItem(name, key)
        self.arr_style.activated.connect(self._arrow_edited)
        self.arr_width = QtWidgets.QDoubleSpinBox()
        self.arr_width.setRange(0.25, 6)
        self.arr_width.setSingleStep(0.25)
        self.arr_width.setSuffix(" pt")
        self.arr_width.valueChanged.connect(self._arrow_edited)
        b = QtWidgets.QPushButton("Colour…")
        b.clicked.connect(self._arrow_colour)
        f.addRow("Style", self.arr_style)
        f.addRow("Line width", self.arr_width)
        f.addRow(b)
        self.props.addWidget(w)
        # several
        w = QtWidgets.QGroupBox("Align selected items")
        g = QtWidgets.QGridLayout(w)
        for i, (txt, key) in enumerate((("Left", "l"), ("Centre", "hc"), ("Right", "r"),
                                        ("Top", "t"), ("Middle", "vc"), ("Bottom", "b"))):
            b = QtWidgets.QPushButton(txt)
            b.clicked.connect(lambda _=False, k=key: self.align(k))
            g.addWidget(b, i // 3, i % 3)
        b = QtWidgets.QPushButton("Centre on page")
        b.clicked.connect(lambda: self.align("page"))
        g.addWidget(b, 2, 0, 1, 3)
        self.props.addWidget(w)
        pv.addWidget(self.props)
        arr = QtWidgets.QGroupBox("Arrange")
        g = QtWidgets.QGridLayout(arr)
        for i, (txt, fn) in enumerate((("To front", lambda: self.restack(1)), ("To back", lambda: self.restack(-1)),
                                       ("Duplicate", self.duplicate), ("Delete", self.delete_selected))):
            b = QtWidgets.QPushButton(txt)
            b.clicked.connect(fn)
            g.addWidget(b, i // 2, i % 2)
        pv.addWidget(arr)
        pv.addStretch(1)
        dock.setWidget(pw)
        dock.setMinimumWidth(270)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, dock)
        self.status = self.statusBar()

    # ---------------------------------------------------------------- undo
    def snapshot(self):
        return dict(page=(self.scene.w_mm, self.scene.h_mm),
                    items=[it.to_dict() for it in sorted(self.scene.content_items(), key=lambda i: i.zValue())])

    def restore(self, snap):
        self._restoring = True
        try:
            for it in self.scene.content_items():
                self.scene.removeItem(it)
            self.scene.set_page(*snap["page"])
            self.page_w.setValue(snap["page"][0])
            self.page_h.setValue(snap["page"][1])
            for d in snap["items"]:
                it = item_from_dict(d)
                if it is not None:
                    self.scene.addItem(it)
        finally:
            self._restoring = False
        self._selection_changed()

    def _edited(self):
        if self._restoring:
            return
        snap = self.snapshot()
        if snap == self._snapshot_now:
            return
        self.undo_stack.append(self._snapshot_now)
        del self.undo_stack[:-60]
        self.redo_stack.clear()
        self._snapshot_now = snap
        self.dirty = True
        self._update_title()
        self._selection_changed()

    def undo(self):
        if not self.undo_stack:
            return
        self.redo_stack.append(self._snapshot_now)
        self._snapshot_now = self.undo_stack.pop()
        self.restore(self._snapshot_now)
        self.dirty = True
        self._update_title()

    def redo(self):
        if not self.redo_stack:
            return
        self.undo_stack.append(self._snapshot_now)
        self._snapshot_now = self.redo_stack.pop()
        self.restore(self._snapshot_now)
        self.dirty = True
        self._update_title()

    def _update_title(self):
        name = os.path.basename(self.path) if self.path else "untitled"
        self.setWindowTitle(f"{APP_NAME} – Figure maker – {name}{' *' if self.dirty else ''}")

    # ---------------------------------------------------------------- inserting
    def _free_spot(self, w, h):
        """Top-left position for a new item: below the lowest item, or the page centre."""
        pr = self.scene.page_rect()
        items = self.scene.content_items()
        if not items:
            return QtCore.QPointF(max(0, (pr.width() - w) / 2), max(0, (pr.height() - h) / 2))
        bottom = max(it.sceneBoundingRect().bottom() for it in items)
        y = bottom + 2 * MM if bottom + h < pr.height() else max(0, (pr.height() - h) / 2)
        return QtCore.QPointF(max(0, (pr.width() - w) / 2), y)

    def _place(self, it, pos=None):
        it.setZValue(self.scene.top_z() + 1)
        self.scene.addItem(it)
        br = it.boundingRect()
        if pos is None:
            pos = self._free_spot(br.width(), br.height())
        else:
            pos = QtCore.QPointF(pos.x() - br.width() / 2, pos.y() - br.height() / 2)
        it.setPos(pos)
        self.scene.clearSelection()
        it.setSelected(True)
        self.scene.edited()

    def add_structure(self, svg, ket):
        """A drawing from the structure editor; double-click it later to edit it again."""
        it = self.add_picture(svg, "svg", max_w_mm=self.scene.w_mm * 0.6, meta={"ket": ket})
        self.status.showMessage("Structure added – drag to place it; double-click it to edit the structure", 6000)
        return it

    def add_picture(self, data, fmt, pos=None, max_w_mm=None, meta=None):
        it = PictureItem(data, fmt, meta=meta)
        pw = self.scene.page_rect().width()
        limit = (max_w_mm * MM) if max_w_mm else pw
        if it.w > limit:
            it.set_size(limit)
        self._place(it, pos)
        return it

    def open_structure_editor(self):
        import structure_editor
        structure_editor.open_editor()

    def insert_file(self, path, pos=None):
        ext = os.path.splitext(path)[1].lower()
        if ext == ".wgfig":
            self.open_figure(path)
            return
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as e:
            QtWidgets.QMessageBox.critical(self, APP_NAME, str(e))
            return
        if ext == ".svg":
            if not QtSvg.QSvgRenderer(QtCore.QByteArray(data)).isValid():
                QtWidgets.QMessageBox.warning(self, APP_NAME, "That SVG file couldn't be read.")
                return
            self.add_picture(data, "svg", pos)
            return
        img = QtGui.QImage.fromData(data)
        if img.isNull():
            QtWidgets.QMessageBox.warning(self, APP_NAME, "That file isn't a picture this program can read "
                                                         "(PNG, JPEG, TIFF, BMP or SVG).")
            return
        if ext not in (".png", ".jpg", ".jpeg"):
            data = _png(img)
        self.add_picture(data, "png" if ext != ".jpg" and ext != ".jpeg" else "jpg", pos)

    def insert_qimage(self, img, pos=None):
        if img is None or img.isNull():
            return
        self.add_picture(_png(img), "png", pos)

    def insert_file_dialog(self):
        fn, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Insert picture or structure", self.settings.value("fig/dir", os.path.expanduser("~")),
            "Pictures (*.png *.jpg *.jpeg *.svg *.tif *.tiff *.bmp)")
        if fn:
            self.settings.setValue("fig/dir", os.path.dirname(fn))
            self.insert_file(fn)

    def paste(self):
        fi = self.scene.focusItem()
        if isinstance(fi, TextItem) and fi.editing():
            fi.textCursor().insertText(QtWidgets.QApplication.clipboard().text())
            return
        md = QtWidgets.QApplication.clipboard().mimeData()
        if md.hasFormat("image/svg+xml"):
            data = bytes(md.data("image/svg+xml"))
            if QtSvg.QSvgRenderer(QtCore.QByteArray(data)).isValid():
                self.add_picture(data, "svg")
                return
        if md.hasImage():
            self.insert_qimage(QtGui.QImage(md.imageData()))
            return
        if md.hasUrls():
            for u in md.urls():
                if u.toLocalFile():
                    self.insert_file(u.toLocalFile())
            return
        if md.hasText() and md.text().strip():
            self.insert_text(md.text())
            return
        self.status.showMessage("Nothing on the clipboard to paste", 4000)

    def insert_text(self, text="Label", size=None, edit=True):
        it = TextItem("", size or float(self.settings.value("fig/textsize", 10)))
        it.setPlainText(text)
        self._place(it, self.view.mapToScene(self.view.viewport().rect().center())
                    if not self.scene.content_items() else None)
        if edit:
            it._before = it.toHtml()
            it.setTextInteractionFlags(QtCore.Qt.TextEditorInteraction)
            it.setFocus()
            c = it.textCursor()
            c.select(QtGui.QTextCursor.Document)
            it.setTextCursor(c)
        return it

    def insert_caption(self):
        pr = self.scene.page_rect()
        it = TextItem("", 9, pr.width() - 8 * MM)
        it.setHtml("<b>Figure 1.</b> Caption text.")
        it.setZValue(self.scene.top_z() + 1)
        self.scene.addItem(it)
        items = [x for x in self.scene.content_items() if x is not it]
        bottom = max([x.sceneBoundingRect().bottom() for x in items], default=pr.height() - 20 * MM)
        if bottom + it.boundingRect().height() > pr.height():
            self.scene.set_page(self.scene.w_mm, (bottom + it.boundingRect().height() + 3 * MM) / MM)
            self.page_h.setValue(round(self.scene.h_mm))
        it.setPos(4 * MM, min(bottom + 1 * MM, pr.height()))
        self.scene.clearSelection()
        it.setSelected(True)
        self.scene.edited()

    def start_arrow(self, style):
        for b, (k, _) in zip(self.arrow_buttons, ArrowItem.STYLES):
            b.blockSignals(True)
            b.setChecked(k == style)
            b.blockSignals(False)
        self.scene.arrow_mode = style
        self.view.viewport().setCursor(QtCore.Qt.CrossCursor)
        self.status.showMessage("Drag on the page to draw the arrow (hold Shift for any angle)", 6000)

    def add_item(self, item):
        """From a data window (Report ▸ Send … to the figure maker) or the report builder."""
        path = item.get("path", "")
        if path:
            self.insert_file(path)
            src = item.get("source", "")
            self.status.showMessage(f"Added {'plot' if path.endswith('.svg') else 'picture'}"
                                    f"{' from ' + src if src else ''} – drag to place it", 6000)

    # ---------------------------------------------------------------- selection / properties
    def _sel(self):
        return [it for it in self.scene.selectedItems() if isinstance(it, (PictureItem, ArrowItem, TextItem))]

    def _selection_changed(self):
        try:
            sel = self._sel()
        except RuntimeError:          # window is closing
            return
        self._updating = True
        try:
            if len(sel) > 1:
                self.props.setCurrentIndex(4)
            elif not sel:
                self.props.setCurrentIndex(0)
            else:
                it = sel[0]
                if isinstance(it, PictureItem):
                    self.props.setCurrentIndex(1)
                    self.pic_w.setValue(it.w / MM)
                elif isinstance(it, TextItem):
                    self.props.setCurrentIndex(2)
                    self.txt_size.setValue(int(round(it.size_pt())))
                    self.txt_wrap.setChecked(it.textWidth() > 0)
                else:
                    self.props.setCurrentIndex(3)
                    self.arr_style.setCurrentIndex([k for k, _ in ArrowItem.STYLES].index(it.style))
                    self.arr_width.setValue(it.width_pt)
        finally:
            self._updating = False

    def _pic_width_edited(self):
        for it in self._sel():
            if isinstance(it, PictureItem):
                it.set_size(self.pic_w.value() * MM)
        self.scene.edited()

    def _pic_fit_width(self):
        pr = self.scene.page_rect()
        for it in self._sel():
            if isinstance(it, PictureItem):
                it.set_size(pr.width())
                it.setX(0)
        self.scene.edited()

    def _pic_restore_aspect(self):
        for it in self._sel():
            if isinstance(it, PictureItem):
                it.set_size(it.w)
        self.scene.edited()

    def _txt_size_edited(self, v):
        if getattr(self, "_updating", False):
            return
        for it in self._sel():
            if isinstance(it, TextItem):
                it.set_size_pt(v)
        self.settings.setValue("fig/textsize", v)
        self.scene.edited()

    def _txt(self, what):
        for it in self._sel():
            if isinstance(it, TextItem):
                if what in ("bold", "italic"):
                    it.toggle(what)
                else:
                    it.vertical(what)
        self.scene.edited()

    def _txt_colour(self):
        sel = [it for it in self._sel() if isinstance(it, TextItem)]
        if not sel:
            return
        c = QtWidgets.QColorDialog.getColor(sel[0].defaultTextColor(), self, "Text colour")
        if c.isValid():
            for it in sel:
                it.set_color(c)
            self.scene.edited()

    def _txt_wrap(self, on):
        if getattr(self, "_updating", False):
            return
        for it in self._sel():
            if isinstance(it, TextItem):
                it.setTextWidth(max(it.boundingRect().width(), 40 * MM) if on else -1)
        self.scene.edited()

    def _arrow_edited(self, *_):
        if getattr(self, "_updating", False):
            return
        for it in self._sel():
            if isinstance(it, ArrowItem):
                it.prepareGeometryChange()
                it.style = self.arr_style.currentData()
                it.width_pt = self.arr_width.value()
                it.update()
        self.scene.edited()

    def _arrow_colour(self):
        sel = [it for it in self._sel() if isinstance(it, ArrowItem)]
        if not sel:
            return
        c = QtWidgets.QColorDialog.getColor(sel[0].color, self, "Arrow colour")
        if c.isValid():
            for it in sel:
                it.color = c
                it.update()
            self.scene.edited()

    def align(self, how):
        sel = self._sel()
        if not sel:
            return
        rects = {it: it.sceneBoundingRect() for it in sel}
        if how == "page":
            u = QtCore.QRectF()
            for r in rects.values():
                u = u.united(r)
            pr = self.scene.page_rect()
            d = pr.center() - u.center()
            for it in sel:
                it.moveBy(d.x(), d.y())
            self.scene.edited()
            return
        if len(sel) < 2:
            return
        L = min(r.left() for r in rects.values())
        R = max(r.right() for r in rects.values())
        T = min(r.top() for r in rects.values())
        B = max(r.bottom() for r in rects.values())
        for it, r in rects.items():
            dx = dy = 0.0
            if how == "l":
                dx = L - r.left()
            elif how == "r":
                dx = R - r.right()
            elif how == "hc":
                dx = (L + R) / 2 - r.center().x()
            elif how == "t":
                dy = T - r.top()
            elif how == "b":
                dy = B - r.bottom()
            elif how == "vc":
                dy = (T + B) / 2 - r.center().y()
            it.moveBy(dx, dy)
        self.scene.edited()

    def restack(self, d):
        sel = self._sel()
        if not sel:
            return
        zs = [it.zValue() for it in self.scene.content_items()]
        for k, it in enumerate(sel):
            it.setZValue((max(zs) + 1 + k) if d > 0 else (min(zs) - 1 - k))
        self.scene.edited()

    def duplicate(self):
        sel = self._sel()
        if not sel:
            return
        self.scene.clearSelection()
        z = self.scene.top_z()
        for k, it in enumerate(sel):
            n = item_from_dict(it.to_dict())
            n.setPos(it.pos() + QtCore.QPointF(4 * MM, 4 * MM))
            n.setZValue(z + 1 + k)
            self.scene.addItem(n)
            n.setSelected(True)
        self.scene.edited()

    def delete_selected(self):
        fi = self.scene.focusItem()
        if isinstance(fi, TextItem) and fi.editing():
            return
        sel = self._sel()
        for it in sel:
            self.scene.removeItem(it)
        if sel:
            self.scene.edited()

    def keyPressEvent(self, ev):
        fi = self.scene.focusItem()
        if not (isinstance(fi, TextItem) and fi.editing()) and ev.key() in (
                QtCore.Qt.Key_Left, QtCore.Qt.Key_Right, QtCore.Qt.Key_Up, QtCore.Qt.Key_Down):
            step = (5 if ev.modifiers() & QtCore.Qt.ShiftModifier else 0.5) * MM
            dx = {QtCore.Qt.Key_Left: -step, QtCore.Qt.Key_Right: step}.get(ev.key(), 0)
            dy = {QtCore.Qt.Key_Up: -step, QtCore.Qt.Key_Down: step}.get(ev.key(), 0)
            for it in self._sel():
                it.moveBy(dx, dy)
            if self._sel():
                self.scene.edited()
            return
        if ev.key() == QtCore.Qt.Key_Escape:
            self.scene.arrow_mode = None
            for b in self.arrow_buttons:
                b.setChecked(False)
            self.view.viewport().setCursor(QtCore.Qt.ArrowCursor)
        super().keyPressEvent(ev)

    # ---------------------------------------------------------------- page
    def _preset_chosen(self, i):
        d = self.preset.itemData(i)
        if d:
            self.page_w.setValue(d[0])
            self.page_h.setValue(d[1])
            self._page_size_edited()

    def _page_size_edited(self):
        w, h = self.page_w.value(), self.page_h.value()
        if (w, h) != (self.scene.w_mm, self.scene.h_mm):
            self.scene.set_page(w, h)
            self.view.fit()
            self.scene.edited()

    def fit_page_to_contents(self):
        items = self.scene.content_items()
        if not items:
            return
        u = QtCore.QRectF()
        for it in items:
            u = u.united(it.sceneBoundingRect())
        hs = _handle_size(items[0])
        u = u.adjusted(hs, hs, -hs, -hs)       # bounding rects include the selection handles
        m = 2 * MM
        for it in items:
            it.moveBy(-u.left() + m, -u.top() + m)
        w, h = (u.width() + 2 * m) / MM, (u.height() + 2 * m) / MM
        self.scene.set_page(round(w, 1), round(h, 1))
        self.page_w.setValue(round(w))
        self.page_h.setValue(round(h))
        self.view.fit()
        self.scene.edited()

    # ---------------------------------------------------------------- output
    def _render(self, painter, target):
        fi = self.scene.focusItem()
        if isinstance(fi, TextItem):
            fi.finish_editing()
            fi.clearFocus()
        sel = self.scene.selectedItems()
        self.scene.exporting = True
        self.scene.clearSelection()
        self.scene.page.setPen(QtCore.Qt.NoPen)
        eff = self.scene.page.graphicsEffect()
        eff.setEnabled(False)
        try:
            self.scene.render(painter, target, self.scene.page_rect())
        finally:
            eff.setEnabled(True)
            self.scene.page.setPen(QtGui.QPen(QtGui.QColor("#9a9a9a"), 0))
            self.scene.exporting = False
            for it in sel:
                it.setSelected(True)

    def render_image(self, dpi):
        w = int(round(self.scene.w_mm / 25.4 * dpi))
        h = int(round(self.scene.h_mm / 25.4 * dpi))
        img = QtGui.QImage(w, h, QtGui.QImage.Format_ARGB32)
        img.fill(QtGui.QColor("white"))
        dpm = int(round(dpi / 0.0254))
        img.setDotsPerMeterX(dpm)
        img.setDotsPerMeterY(dpm)
        p = QtGui.QPainter(img)
        p.setRenderHints(QtGui.QPainter.Antialiasing | QtGui.QPainter.TextAntialiasing |
                         QtGui.QPainter.SmoothPixmapTransform)
        self._render(p, QtCore.QRectF(0, 0, w, h))
        p.end()
        return img

    def export_to(self, fn, ext, dpi=600):
        if ext == ".svg":
            from PySide6 import QtSvg as S
            gen = S.QSvgGenerator()
            gen.setFileName(fn)
            wpt, hpt = self.scene.w_mm / PT_MM, self.scene.h_mm / PT_MM
            gen.setSize(QtCore.QSize(round(wpt), round(hpt)))
            gen.setViewBox(QtCore.QRectF(0, 0, wpt, hpt))
            gen.setResolution(72)
            gen.setTitle("Figure")
            gen.setDescription(f"Made with {APP_NAME} {__version__}")
            p = QtGui.QPainter(gen)
            p.setRenderHints(QtGui.QPainter.Antialiasing | QtGui.QPainter.TextAntialiasing)
            self._render(p, QtCore.QRectF(0, 0, wpt, hpt))
            p.end()
        elif ext == ".pdf":
            w = QtGui.QPdfWriter(fn)
            w.setPageSize(QtGui.QPageSize(QtCore.QSizeF(self.scene.w_mm, self.scene.h_mm),
                                          QtGui.QPageSize.Millimeter))
            w.setPageMargins(QtCore.QMarginsF(0, 0, 0, 0))
            w.setResolution(1200)
            w.setCreator(f"{APP_NAME} {__version__}")
            p = QtGui.QPainter(w)
            p.setRenderHints(QtGui.QPainter.Antialiasing | QtGui.QPainter.TextAntialiasing |
                             QtGui.QPainter.LosslessImageRendering)
            r = QtCore.QRectF(0, 0, self.scene.w_mm / 25.4 * 1200, self.scene.h_mm / 25.4 * 1200)
            self._render(p, r)
            p.end()
        else:
            img = self.render_image(dpi)
            if ext == ".tif":
                try:
                    from PIL import Image
                    im = Image.open(io.BytesIO(_png(img)))
                    im.convert("RGB").save(fn, format="TIFF", dpi=(dpi, dpi), compression="tiff_lzw")
                except ImportError:
                    img.save(fn, "TIFF")
            else:
                if not img.save(fn, "PNG"):
                    raise OSError(f"Couldn't write {fn}")

    def export_dialog(self):
        if not self.scene.content_items():
            self.status.showMessage("The figure is empty", 4000)
            return
        dlg = ExportDialog(self, self.settings)
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return
        ext, dpi = dlg.result_opts()
        filt = {".png": "PNG (*.png)", ".tif": "TIFF (*.tif *.tiff)", ".svg": "SVG (*.svg)", ".pdf": "PDF (*.pdf)"}[ext]
        stem = os.path.splitext(os.path.basename(self.path))[0] if self.path else "figure"
        fn, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export figure", os.path.join(self.settings.value("export_dir", os.path.expanduser("~")),
                                                stem + ext), filt)
        if not fn:
            return
        if not os.path.splitext(fn)[1]:
            fn += ext
        self.settings.setValue("export_dir", os.path.dirname(fn))
        try:
            self.export_to(fn, ext, dpi)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, APP_NAME, f"Export failed:\n{e}")
            return
        self.status.showMessage(f"Saved {fn}", 6000)

    def copy_image(self):
        if not self.scene.content_items():
            return
        QtWidgets.QApplication.clipboard().setImage(self.render_image(300))
        self.status.showMessage("Figure copied (300 dpi) – paste into Word or PowerPoint", 5000)

    def add_to_report(self):
        if not self.scene.content_items():
            self.status.showMessage("The figure is empty", 4000)
            return
        import tempfile
        import report_builder
        dpi = int(self.settings.value("img/dpi", 600))
        fd, fn = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        self.render_image(dpi).save(fn, "PNG")
        report_builder.deliver("report", dict(kind="image", path=fn, caption="", source="the figure maker"), self)

    # ---------------------------------------------------------------- files
    def _confirm_discard(self):
        if not self.dirty or not self.scene.content_items():
            return True
        r = QtWidgets.QMessageBox.question(
            self, APP_NAME, "Save the current figure first?",
            QtWidgets.QMessageBox.Save | QtWidgets.QMessageBox.Discard | QtWidgets.QMessageBox.Cancel)
        if r == QtWidgets.QMessageBox.Save:
            return self.save_figure()
        return r == QtWidgets.QMessageBox.Discard

    def new_figure(self):
        if not self._confirm_discard():
            return
        self.restore(dict(page=(self.scene.w_mm, self.scene.h_mm), items=[]))
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._snapshot_now = self.snapshot()
        self.path = None
        self.dirty = False
        self._update_title()

    def save_figure(self, as_new=False):
        fn = self.path
        if as_new or not fn:
            fn, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "Save figure", os.path.join(self.settings.value("export_dir", os.path.expanduser("~")),
                                                  "figure.wgfig"), f"{APP_NAME} figure (*.wgfig)")
            if not fn:
                return False
            if not fn.lower().endswith(".wgfig"):
                fn += ".wgfig"
        snap = self.snapshot()
        for d in snap["items"]:
            if "data" in d:
                d["data"] = base64.b64encode(d["data"]).decode("ascii")
        try:
            with open(fn, "w", encoding="utf-8") as fh:
                json.dump({"app": f"{APP_NAME} figure", "version": 1, **snap}, fh)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, APP_NAME, f"Couldn't save:\n{e}")
            return False
        self.path = fn
        self.dirty = False
        self._update_title()
        self.status.showMessage(f"Saved {fn}", 5000)
        return True

    def open_figure(self, fn=None):
        if not self._confirm_discard():
            return
        if not fn:
            fn, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "Open figure", self.settings.value("export_dir", os.path.expanduser("~")),
                f"{APP_NAME} figure (*.wgfig)")
        if not fn:
            return
        try:
            with open(fn, encoding="utf-8") as fh:
                d = json.load(fh)
            for it in d.get("items", []):
                if "data" in it:
                    it["data"] = base64.b64decode(it["data"])
            snap = dict(page=tuple(d.get("page", (170, 110))), items=d.get("items", []))
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, APP_NAME, f"Couldn't open the figure:\n{e}")
            return
        self.restore(snap)
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._snapshot_now = self.snapshot()
        self.path = fn
        self.dirty = False
        self._update_title()
        self.view.fit()

    def closeEvent(self, e):
        if self._confirm_discard():
            e.accept()
        else:
            e.ignore()

    def show_help(self):
        QtWidgets.QMessageBox.information(self, "Figure maker", """
<b>Plots</b>: in a data window choose <i>Report ▸ Send … to the figure maker</i>. Chromatograms and spectra
arrive exactly as you set them up (zoom, labels you moved or added) as vector graphics.<br><br>
<b>Structures</b>: <i>Draw structure…</i> opens the structure editor (structures and reaction schemes);
its <i>Insert into figure maker</i> places the drawing here. Double-click a drawn structure to edit it again.<br><br>
<b>Pictures</b>: <i>Picture…</i> inserts PNG, JPEG or SVG files; <i>Paste</i> (Ctrl+V)
inserts a structure copied from a drawing program. You can also drag files onto the page.<br><br>
<b>Labels and captions</b>: double-click text to edit it. Select characters and use x² / x₂ (or
Ctrl+Shift+= / Ctrl+=) for super- and subscripts, e.g. CDCl₃ or cm⁻¹.<br><br>
<b>Arrows</b>: pick a style on the toolbar and drag on the page: → reaction, ↔ resonance,
⇌ equilibrium (e.g. keto–enol tautomerism), ⇒ retrosynthesis. Drag the ends to point them;
hold Shift for any angle (otherwise 15° steps).<br><br>
<b>Moving and sizing</b>: drag items; drag a picture's corner square to resize it (Shift = free proportions);
arrow keys nudge (Shift = 5 mm). Select several items to align them.<br><br>
<b>Export</b> as PNG/TIFF (600 dpi default), SVG or PDF at the page size, copy it, or add it to the report.
<i>Save figure</i> keeps everything editable (.wgfig).
""")


def _png(img: QtGui.QImage):
    ba = QtCore.QByteArray()
    buf = QtCore.QBuffer(ba)
    buf.open(QtCore.QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(ba)
