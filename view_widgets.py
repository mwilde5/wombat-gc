"""
view_widgets.py - plot widgets with chromatography-style mouse handling, shared by the
1D chromatogram, the mass spectrum and the GCxGC image.
"""
from __future__ import annotations

import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets


class InteractiveViewBox(pg.ViewBox):
    sigClick = QtCore.Signal(float, float)
    sigDoubleClick = QtCore.Signal(float, float)
    sigDrag = QtCore.Signal(float, float, bool)   # x0, x1, finished
    sigRectDrag = QtCore.Signal(float, float, float, float, bool)   # x0, y0, x1, y1, finished

    def __init__(self, y_provider=None):
        super().__init__(enableMenu=False)
        self.setMouseEnabled(x=False, y=False)
        self.y_provider = y_provider       # f(x0, x1) -> (ymin, ymax) of data in view
        self.full_x = (0.0, 1.0)
        self.auto_y = True
        self.history = []
        self._rb = QtWidgets.QGraphicsRectItem()
        self._rb.setPen(pg.mkPen((60, 60, 60), width=1, style=QtCore.Qt.DashLine, cosmetic=True))
        self._rb.setBrush(pg.mkBrush(100, 100, 255, 30))
        self._rb.hide()
        self.addItem(self._rb, ignoreBounds=True)
        self.sigXRangeChanged.connect(self._on_x_changed)

    # ----- ranges -----
    def set_full_x(self, x0, x1):
        self.full_x = (float(x0), float(x1))

    def reset_view(self):
        self.history.clear()
        self.auto_y = True
        self.setXRange(*self.full_x, padding=0)
        self.fit_y()

    def zoom_out(self):
        if self.history:
            (xr, yr, auto) = self.history.pop()
            self.auto_y = auto
            self.setXRange(*xr, padding=0)
            if auto:
                self.fit_y()
            else:
                self.setYRange(*yr, padding=0)
        else:
            self.reset_view()

    def _push(self):
        xr, yr = self.viewRange()
        self.history.append((tuple(xr), tuple(yr), self.auto_y))
        del self.history[:-30]

    def fit_y(self):
        if not self.y_provider:
            return
        x0, x1 = self.viewRange()[0]
        r = self.y_provider(x0, x1)
        if r is None:
            return
        ymin, ymax = r
        if ymax <= ymin:
            ymax = ymin + 1
        self.setYRange(min(0.0, ymin), ymax * 1.08, padding=0)

    def _on_x_changed(self, *_):
        if self.auto_y:
            self.fit_y()

    # ----- mouse -----
    def mouseClickEvent(self, ev):
        if ev.button() == QtCore.Qt.LeftButton:
            p = self.mapSceneToView(ev.scenePos())
            if ev.double():
                self.sigDoubleClick.emit(p.x(), p.y())
            else:
                self.sigClick.emit(p.x(), p.y())
            ev.accept()
        elif ev.button() == QtCore.Qt.RightButton:
            self.zoom_out()
            ev.accept()
        else:
            ev.ignore()

    def mouseDragEvent(self, ev, axis=None):
        btn = ev.button()
        p0 = self.mapSceneToView(ev.buttonDownScenePos())
        p1 = self.mapSceneToView(ev.scenePos())
        if btn == QtCore.Qt.LeftButton:
            ev.accept()
            self.sigDrag.emit(p0.x(), p1.x(), ev.isFinish())
            self.sigRectDrag.emit(p0.x(), p0.y(), p1.x(), p1.y(), ev.isFinish())
        elif btn == QtCore.Qt.RightButton:
            ev.accept()
            r = QtCore.QRectF(p0, p1).normalized()
            if ev.isFinish():
                self._rb.hide()
                if r.width() > 0 and r.height() > 0:
                    self._push()
                    self.auto_y = False
                    self.setXRange(r.left(), r.right(), padding=0)
                    self.setYRange(max(r.top(), min(0, r.top())), r.bottom(), padding=0)
            else:
                self._rb.setRect(r)
                self._rb.show()
        elif btn == QtCore.Qt.MiddleButton:
            ev.accept()
            last = self.mapSceneToView(ev.lastScenePos())
            dx = p1.x() - last.x()
            x0, x1 = self.viewRange()[0]
            self.setXRange(x0 - dx, x1 - dx, padding=0)
        else:
            ev.ignore()

    def wheelEvent(self, ev, axis=None):
        delta = ev.delta() if hasattr(ev, "delta") else ev.angleDelta().y()
        if delta == 0:
            ev.ignore()
            return
        f = 0.85 ** (delta / 120.0)
        mods = ev.modifiers()
        if mods & QtCore.Qt.ControlModifier:
            y0, y1 = self.viewRange()[1]
            self.auto_y = False
            self.setYRange(y0, y0 + (y1 - y0) * f, padding=0)
        else:
            c = self.mapSceneToView(ev.scenePos()).x()
            x0, x1 = self.viewRange()[0]
            nx0, nx1 = c - (c - x0) * f, c + (x1 - c) * f
            fx0, fx1 = self.full_x
            span = fx1 - fx0
            nx0, nx1 = max(nx0, fx0 - 0.02 * span), min(nx1, fx1 + 0.02 * span)
            self.setXRange(nx0, nx1, padding=0)
        ev.accept()


def make_plot(y_provider, xlabel, ylabel):
    vb = InteractiveViewBox(y_provider)
    pw = pg.PlotWidget(viewBox=vb)
    pw.setLabel("bottom", xlabel)
    pw.setLabel("left", ylabel)
    pw.showGrid(x=False, y=False)
    pw.getPlotItem().hideButtons()
    pw.setFocusPolicy(QtCore.Qt.StrongFocus)
    pw.getPlotItem().setClipToView(True)
    for ax in ("left", "bottom"):
        pw.getAxis(ax).setTextPen("#222222")
    return pw, vb



class Image2DViewBox(InteractiveViewBox):
    """Zooms both axes (GCxGC image): wheel = both, Shift+wheel = 1tR only, Ctrl+wheel = 2tR only."""

    def __init__(self):
        super().__init__(None)
        self.auto_y = False
        self.full_y = (0.0, 1.0)

    def set_full(self, x0, x1, y0, y1):
        self.full_x = (float(x0), float(x1))
        self.full_y = (float(y0), float(y1))

    def reset_view(self):
        self.history.clear()
        self.setRange(xRange=self.full_x, yRange=self.full_y, padding=0)

    def zoom_out(self):
        if self.history:
            xr, yr, _ = self.history.pop()
            self.setRange(xRange=xr, yRange=yr, padding=0)
        else:
            self.reset_view()

    def fit_y(self):
        pass

    def wheelEvent(self, ev, axis=None):
        delta = ev.delta() if hasattr(ev, "delta") else ev.angleDelta().y()
        if delta == 0:
            ev.ignore()
            return
        f = 0.85 ** (delta / 120.0)
        c = self.mapSceneToView(ev.scenePos())
        (x0, x1), (y0, y1) = self.viewRange()
        mods = ev.modifiers()
        zx = not (mods & QtCore.Qt.ControlModifier)
        zy = not (mods & QtCore.Qt.ShiftModifier)
        if zx:
            x0, x1 = c.x() - (c.x() - x0) * f, c.x() + (x1 - c.x()) * f
        if zy:
            y0, y1 = c.y() - (c.y() - y0) * f, c.y() + (y1 - c.y()) * f
        self.setRange(xRange=(x0, x1), yRange=(y0, y1), padding=0)
        ev.accept()
