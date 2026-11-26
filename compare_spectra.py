"""
Spectrum panels for the comparison window: one small mass-spectrum plot per sample,
match scores against a reference sample, mirror plots and library hits.
"""
from __future__ import annotations

import html

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from view_widgets import make_plot


def match_arrays(a, b, decimals=0):
    """Cosine match of two spectra ((mz, I) tuples) as 0-999. decimals=0 -> nominal mass,
    otherwise binned to that many decimals (high-resolution data)."""
    if a is None or b is None:
        return None
    (ma, ia), (mb, ib) = a, b
    if len(ma) == 0 or len(mb) == 0:
        return None
    scale = 10 ** decimals
    ka = np.round(np.asarray(ma) * scale).astype(np.int64)
    kb = np.round(np.asarray(mb) * scale).astype(np.int64)
    keys = np.union1d(ka, kb)
    va = np.zeros(keys.size)
    vb = np.zeros(keys.size)
    np.add.at(va, np.searchsorted(keys, ka), np.asarray(ia, float))
    np.add.at(vb, np.searchsorted(keys, kb), np.asarray(ib, float))
    w = (keys / scale) ** 0.5
    va = np.sqrt(np.maximum(va, 0)) * w
    vb = np.sqrt(np.maximum(vb, 0)) * w
    na, nb = np.linalg.norm(va), np.linalg.norm(vb)
    if not na or not nb:
        return None
    c = float(va @ vb / (na * nb))
    return int(round(999 * c * c))


class SpecPanel(QtWidgets.QWidget):
    """Header line + stick spectrum (+ optional mirrored reference)."""
    sigClicked = QtCore.Signal(int)

    def __init__(self, k, colour, parent=None):
        super().__init__(parent)
        self.k = k
        self.colour = colour
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(2, 1, 2, 1)
        v.setSpacing(0)
        self.head = QtWidgets.QLabel("")
        self.head.setTextFormat(QtCore.Qt.RichText)
        self.head.setStyleSheet("font-size:9pt;")
        self.head.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        v.addWidget(self.head)
        self.pw, self.vb = make_plot(self._yrange, "m/z", "")
        self.pw.getPlotItem().setClipToView(False)
        self.pw.getPlotItem().hideAxis("left")
        self.pw.setMinimumHeight(70)
        v.addWidget(self.pw, 1)
        self.msg = QtWidgets.QLabel("")
        self.msg.setAlignment(QtCore.Qt.AlignCenter)
        self.msg.setStyleSheet("color:#999; font-size:9pt;")
        self.msg.hide()
        v.addWidget(self.msg, 1)
        self.curve = self.pw.plot(pen=pg.mkPen(colour, width=1.3))
        self.mirror = self.pw.plot(pen=pg.mkPen((140, 140, 140), width=1.1))
        self.zero = pg.InfiniteLine(0, angle=0, pen=pg.mkPen((170, 170, 170), width=1))
        self.pw.addItem(self.zero)
        self.labels = []
        self.spec = None
        self.ref = None
        self.decimals = 0
        self.empty = pg.TextItem("", color=(150, 150, 150), anchor=(0.5, 0.5))
        self.vb.addItem(self.empty, ignoreBounds=True)

    def _yrange(self, x0, x1):
        if self.spec is None:
            return None
        mz, ab = self.spec
        m = (mz >= x0) & (mz <= x1)
        top = float(ab[m].max()) if m.any() else 1.0
        lo = 0.0
        if self.ref is not None:
            rm, ra = self.ref
            mm = (rm >= x0) & (rm <= x1)
            lo = -float(ra[mm].max()) * 1.05 if mm.any() else 0.0
        return lo, top * 1.18

    @staticmethod
    def _sticks(mz, ab, sign=1.0):
        x = np.repeat(np.asarray(mz, float), 2)
        y = np.column_stack([np.zeros(len(ab)), sign * np.asarray(ab, float)]).ravel()
        return x, y

    def set_spectrum(self, spec, header_html, ref=None, decimals=0, n_labels=5, xrange=None, message=""):
        """spec = (mz, ab) normalised to 100, or None. ref = reference spectrum to mirror, or None."""
        for t in self.labels:
            self.vb.removeItem(t)
        self.labels = []
        self.head.setText(header_html)
        self.head.setToolTip(QtGui.QTextDocumentFragment.fromHtml(header_html).toPlainText())
        self.decimals = decimals
        self.spec = spec
        self.ref = ref
        if spec is None or len(spec[0]) == 0:
            self.curve.setData([], [])
            self.mirror.setData([], [])
            self.pw.hide()
            self.msg.setText(message or "no spectrum")
            self.msg.show()
            self.spec = None
            return
        self.msg.hide()
        self.pw.show()
        mz, ab = spec
        self.curve.setData(*self._sticks(mz, ab), connect="pairs")
        if ref is not None:
            self.mirror.setData(*self._sticks(ref[0], ref[1], -1.0), connect="pairs")
        else:
            self.mirror.setData([], [])
        # label the largest peaks
        order = np.argsort(ab)[::-1]
        used = []
        span = (xrange[1] - xrange[0]) if xrange else (float(mz.max()) - float(mz.min()) or 1.0)
        for i in order:
            if len(used) >= n_labels or ab[i] < 3:
                break
            if any(abs(mz[i] - u) < span * 0.04 for u in used):
                continue
            used.append(mz[i])
            t = pg.TextItem(f"{mz[i]:.{decimals}f}", color=self.colour, anchor=(0.5, 1))
            t.setPos(float(mz[i]), float(ab[i]))
            self.vb.addItem(t, ignoreBounds=True)
            self.labels.append(t)
        if xrange:
            self.vb.set_full_x(*xrange)
            if not getattr(self, "_zoomed", False):
                self.vb.setXRange(*xrange, padding=0.07)
        self.vb.fit_y()


def header(name, colour, where, match=None, hit=None, note=""):
    bits = [f"<b style='color:{colour}'>{html.escape(name[:34])}</b>", html.escape(where)]
    if match is not None:
        col = "#2a7a2a" if match >= 800 else ("#b7791f" if match >= 600 else "#c0392b")
        bits.append(f"<span style='color:{col}'>match {match}</span>")
    if hit is not None:
        bits.append(f"<span style='color:#1f3b73'>{html.escape(hit.name[:40])} ({hit.match})</span>")
    if note:
        bits.append(f"<span style='color:#888'>{html.escape(note)}</span>")
    return " · ".join(bits)
