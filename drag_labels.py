"""
drag_labels.py - plot labels the user can drag out of the way.

A DragLabel is a pyqtgraph TextItem attached to a point on the data (a peak apex or an
m/z stick).  Dragging it moves the text and draws a thin leader line back to the point;
the new position (in data coordinates) is reported through on_moved so the window can
store it and reuse it in image exports.
"""
from __future__ import annotations

import pyqtgraph as pg
from PySide6 import QtCore, QtGui

LEADER_PEN = pg.mkPen("#7a7a7a", width=0.8)


class DragLabel(pg.TextItem):
    def __init__(self, text, point, pos=None, *, color="#333333", anchor=(0.5, 1.1),
                 on_moved=None, on_click=None, tooltip=""):
        super().__init__(text, color=color, anchor=anchor)
        self.point = (float(point[0]), float(point[1]))
        self.on_moved = on_moved
        self.on_click = on_click
        self.leader = pg.PlotDataItem([], [], pen=LEADER_PEN)
        self._grab = None
        self.setCursor(QtGui.QCursor(QtCore.Qt.OpenHandCursor))
        self.setToolTip(tooltip or "Drag to move this label")
        self.moved = pos is not None
        self.setPos(*(pos if pos is not None else self.point))
        self._update_leader()

    def add_to(self, plot):
        plot.addItem(self.leader, ignoreBounds=True)
        plot.addItem(self, ignoreBounds=True)
        return [self, self.leader]

    def _update_leader(self):
        if not self.moved:
            self.leader.setData([], [])
            return
        p = self.pos()
        self.leader.setData([self.point[0], p.x()], [self.point[1], p.y()])

    def place(self, x, y, moved=True):
        self.moved = moved
        self.setPos(x, y)
        self._update_leader()

    # pyqtgraph scene events --------------------------------------------------
    def mouseDragEvent(self, ev):
        if ev.button() != QtCore.Qt.LeftButton:
            ev.ignore()
            return
        vb = self.getViewBox()
        if vb is None:
            ev.ignore()
            return
        ev.accept()
        cur = vb.mapSceneToView(ev.scenePos())
        if ev.isStart():
            p = self.pos()
            self._grab = (p.x() - cur.x(), p.y() - cur.y())
            self.setCursor(QtGui.QCursor(QtCore.Qt.ClosedHandCursor))
        if self._grab is None:
            return
        self.place(cur.x() + self._grab[0], cur.y() + self._grab[1])
        if ev.isFinish():
            self._grab = None
            self.setCursor(QtGui.QCursor(QtCore.Qt.OpenHandCursor))
            if self.on_moved is not None:
                p = self.pos()
                self.on_moved((p.x(), p.y()))

    def mouseClickEvent(self, ev):
        if self.on_click is not None and ev.button() == QtCore.Qt.LeftButton:
            ev.accept()
            self.on_click()
        else:
            ev.ignore()


def arrange(vb, items, trace_x, trace_y, fixed=()):
    """Choose label positions for the view box's current zoom (see plots_export.arrange_labels)."""
    import plots_export as px
    (x0, x1), (y0, y1) = vb.viewRange()
    pw, ph = vb.viewPixelSize()
    return px.arrange_labels(items, trace_x, trace_y, (x0, x1), (y0, y1), pw, ph, fixed=fixed)
