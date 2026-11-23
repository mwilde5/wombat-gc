"""
gcxgc_view.py - the GCxGC image panel: smooth colour map with level controls, colour schemes
(including the colour-stop editor), selection markers and integrated-blob overlays.
"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets
from scipy import ndimage

import colormaps as cmaps
from gcxgc import transform
from view_widgets import Image2DViewBox

SCALES = {"Linear": "linear", "Square root": "sqrt", "Log": "log"}


def display_baseline(y, window_pts):
    """
    Rolling-minimum + rolling-mean baseline, subtracted and clipped at zero - the same
    approach as the single-trace -> 3D page.  Display / 3D model only, never integration.
    """
    if window_pts < 3:
        return y
    b = ndimage.minimum_filter1d(np.asarray(y, float), int(window_pts), mode="nearest")
    b = ndimage.uniform_filter1d(b, int(window_pts), mode="nearest")
    return np.maximum(0.0, y - b)


def smooth_upsample(T, max_cols=3000, max_rows=900):
    """Cubic-spline interpolation between modulations (and points), so peaks render as smooth blobs."""
    n1, n2 = T.shape
    f1 = int(max(1, min(8, max_cols // max(n1, 1))))
    f2 = int(max(1, min(4, max_rows // max(n2, 1))))
    if f1 == 1 and f2 == 1:
        return T
    S = ndimage.zoom(T.astype(np.float32), (f1, f2), order=3, mode="nearest", grid_mode=True)
    return np.clip(S, float(T.min()), float(T.max()))


# =============================================================================
class ColourDialog(QtWidgets.QDialog):
    """Preset + editable colour stops, smoothing, display baseline and 3D colours."""

    def __init__(self, panel: "GCxGCPanel"):
        super().__init__(panel)
        self.p = panel
        self.setWindowTitle("Colours & display")
        self.resize(420, 560)
        v = QtWidgets.QVBoxLayout(self)
        f = QtWidgets.QFormLayout()
        self.preset = QtWidgets.QComboBox()
        self.preset.addItems(list(cmaps.PRESETS) + [cmaps.CUSTOM])
        self.preset.setCurrentText(panel.palette_name)
        self.preset.currentTextChanged.connect(self._preset)
        f.addRow("Preset", self.preset)
        v.addLayout(f)
        self.grad = QtWidgets.QLabel()
        self.grad.setFixedHeight(18)
        v.addWidget(self.grad)
        lab = QtWidgets.QHBoxLayout()
        lab.addWidget(QtWidgets.QLabel("low"))
        lab.addStretch(1)
        lab.addWidget(QtWidgets.QLabel("high"))
        v.addLayout(lab)
        self.rows = QtWidgets.QVBoxLayout()
        box = QtWidgets.QWidget()
        box.setLayout(self.rows)
        sc = QtWidgets.QScrollArea()
        sc.setWidgetResizable(True)
        sc.setWidget(box)
        v.addWidget(sc, 1)
        h = QtWidgets.QHBoxLayout()
        b_add = QtWidgets.QPushButton("+ Add colour")
        b_add.clicked.connect(self._add)
        b_rev = QtWidgets.QPushButton("⇄ Reverse")
        b_rev.clicked.connect(self._reverse)
        h.addWidget(b_add)
        h.addWidget(b_rev)
        v.addLayout(h)

        g = QtWidgets.QGroupBox("Display")
        f2 = QtWidgets.QFormLayout(g)
        self.smooth = QtWidgets.QCheckBox("Smooth surface (interpolate between modulations)")
        self.smooth.setChecked(panel.smooth)
        self.smooth.toggled.connect(self._display)
        self.bw = QtWidgets.QDoubleSpinBox()
        self.bw.setRange(0, 600)
        self.bw.setDecimals(0)
        self.bw.setSuffix(" s")
        self.bw.setSpecialValueText("off")
        self.bw.setValue(panel.baseline_s)
        self.bw.setToolTip("Rolling-minimum baseline removed from the plot, 3D view and 3D models.\n"
                           "Integration always uses the raw data.")
        self.bw.valueChanged.connect(self._display)
        f2.addRow(self.smooth)
        f2.addRow("Baseline window (display)", self.bw)
        self.base_col = self._colour_button(panel.base_colour, self._base_col)
        self.bg_col = self._colour_button(panel.bg_colour, self._bg_col)
        f2.addRow("3D base / sides colour", self.base_col)
        f2.addRow("3D background", self.bg_col)
        v.addWidget(g)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        v.addWidget(bb)
        self._render()

    def _colour_button(self, hexcol, cb):
        b = QtWidgets.QPushButton()
        b.setFixedWidth(70)
        b.setStyleSheet(f"background:{hexcol}; border:1px solid #888; min-height:18px;")
        b.clicked.connect(lambda: cb(b))
        return b

    def _pick(self, b, hexcol):
        c = QtWidgets.QColorDialog.getColor(QtGui.QColor(hexcol), self)
        if c.isValid():
            b.setStyleSheet(f"background:{c.name()}; border:1px solid #888; min-height:18px;")
            return c.name()
        return None

    def _base_col(self, b):
        c = self._pick(b, self.p.base_colour)
        if c:
            self.p.set_3d_colours(base=c)

    def _bg_col(self, b):
        c = self._pick(b, self.p.bg_colour)
        if c:
            self.p.set_3d_colours(bg=c)

    def _render(self):
        while self.rows.count():
            it = self.rows.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        stops = self.p.stops
        css = ", ".join(f"stop:{p:.3f} {c}" for p, c in stops)
        self.grad.setStyleSheet(f"border:1px solid #888; background: qlineargradient(x1:0, y1:0, x2:1, y2:0, {css});")
        for k, (pos, col) in enumerate(stops):
            w = QtWidgets.QWidget()
            h = QtWidgets.QHBoxLayout(w)
            h.setContentsMargins(0, 0, 0, 0)
            sw = QtWidgets.QPushButton()
            sw.setFixedSize(40, 24)
            sw.setStyleSheet(f"background:{col}; border:1px solid #888;")
            sw.clicked.connect(lambda _=False, k=k: self._recolour(k))
            sp = QtWidgets.QSpinBox()
            sp.setRange(0, 100)
            sp.setSuffix(" %")
            sp.setValue(int(round(pos * 100)))
            sp.editingFinished.connect(lambda k=k, sp=sp: self._move(k, sp.value()))
            tag = QtWidgets.QLabel("lowest" if k == 0 else "highest" if k == len(stops) - 1 else "")
            tag.setStyleSheet("color:#777;")
            x = QtWidgets.QPushButton("×")
            x.setFixedWidth(28)
            x.setEnabled(len(stops) > 2)
            x.clicked.connect(lambda _=False, k=k: self._remove(k))
            for wd in (sw, sp, tag):
                h.addWidget(wd)
            h.addStretch(1)
            h.addWidget(x)
            self.rows.addWidget(w)
        self.rows.addStretch(1)

    def _custom(self, stops):
        self.p.set_stops(sorted(stops), custom=True)
        self.preset.blockSignals(True)
        self.preset.setCurrentText(cmaps.CUSTOM)
        self.preset.blockSignals(False)
        self._render()

    def _preset(self, name):
        if name != cmaps.CUSTOM:
            self.p.set_palette(name)
            self._render()

    def _recolour(self, k):
        c = QtWidgets.QColorDialog.getColor(QtGui.QColor(self.p.stops[k][1]), self)
        if c.isValid():
            st = list(self.p.stops)
            st[k] = (st[k][0], c.name())
            self._custom(st)

    def _move(self, k, v):
        st = list(self.p.stops)
        if abs(st[k][0] * 100 - v) > 0.4:
            st[k] = (v / 100.0, st[k][1])
            self._custom(st)

    def _remove(self, k):
        st = list(self.p.stops)
        del st[k]
        self._custom(st)

    def _add(self):
        st = list(self.p.stops)
        gaps = [st[i + 1][0] - st[i][0] for i in range(len(st) - 1)]
        g = int(np.argmax(gaps))
        p = (st[g][0] + st[g + 1][0]) / 2
        st.append((p, cmaps.rgb_hex(cmaps.colours_at(st, p))))
        self._custom(st)

    def _reverse(self):
        self._custom([(1 - p, c) for p, c in self.p.stops])

    def _display(self, *_):
        self.p.set_display(self.smooth.isChecked(), self.bw.value())


# =============================================================================
class GCxGCPanel(QtWidgets.QWidget):
    sigClick = QtCore.Signal(float, float)
    sigRect = QtCore.Signal(float, float, float, float, bool)
    sigHover = QtCore.Signal(float, float)
    sigDisplayChanged = QtCore.Signal()       # data, colours, scale, levels or 3D colours changed
    sigViewChanged = QtCore.Signal()          # zoom / pan
    sigMarker = QtCore.Signal(float, float)   # selected point / box centre (for the 3D view)

    def __init__(self, settings: QtCore.QSettings, parent=None):
        super().__init__(parent)
        self.s = settings
        self.M = None              # raw folded matrix (integration)
        self.Mdisp = None          # folded matrix after optional display baseline removal
        self.T = None              # Mdisp after scale transform (not smoothed)
        self.fold = None
        self.y = None
        # "Mike's preference" is the default; switch everyone to it once with this version
        if settings.value("gx/default_v14") != "done":
            settings.setValue("gx/palette2", cmaps.DEFAULT)
            settings.setValue("gx/default_v14", "done")
        name = settings.value("gx/palette2", cmaps.DEFAULT)
        self.palette_name = cmaps.RENAMED.get(name, name)
        if self.palette_name not in cmaps.PRESETS and self.palette_name != cmaps.CUSTOM:
            self.palette_name = cmaps.DEFAULT
        custom = cmaps.loads(settings.value("gx/custom_stops", "") or "")
        self.custom_stops = custom or cmaps.PRESETS["Blue → Red"]
        self.stops = self.custom_stops if self.palette_name == cmaps.CUSTOM else \
            cmaps.PRESETS[self.palette_name]
        self.smooth = settings.value("gx/smooth", "true") == "true"
        self.baseline_s = float(settings.value("gx/baseline_s", 0))
        self.base_colour = settings.value("gx/base_colour", "#2e3138")
        self.bg_colour = settings.value("gx/bg_colour", "#3a3c42")    # dark grey: white floors stand out

        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(6, 4, 6, 2)
        h = QtWidgets.QHBoxLayout()
        self.title = QtWidgets.QLabel("")
        self.title.setStyleSheet("font-weight:600;")
        self.title.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        self.title.setMinimumWidth(60)
        h.addWidget(self.title, 1)
        h.addWidget(QtWidgets.QLabel("Colours"))
        self.pal = QtWidgets.QComboBox()
        self.pal.addItems(list(cmaps.PRESETS) + [cmaps.CUSTOM])
        self.pal.setCurrentText(self.palette_name)
        self.pal.currentTextChanged.connect(self.set_palette)
        h.addWidget(self.pal)
        self.btn_display = QtWidgets.QPushButton("Edit…")
        self.btn_display.setToolTip("Edit colour stops, smoothing, display baseline and 3D colours")
        self.btn_display.clicked.connect(lambda: ColourDialog(self).exec())
        h.addWidget(self.btn_display)
        h.addWidget(QtWidgets.QLabel("Scale"))
        self.scale = QtWidgets.QComboBox()
        self.scale.addItems(SCALES.keys())
        self.scale.setCurrentText(settings.value("gx/scale", "Square root"))
        self.scale.currentTextChanged.connect(self._scale_changed)
        h.addWidget(self.scale)
        self.btn_auto = QtWidgets.QPushButton("Auto levels")
        self.btn_auto.setToolTip("Reset the colour range to the data in view.\n"
                                 "Drag the handles on the colour bar to set the noise floor and saturation.")
        self.btn_auto.clicked.connect(lambda: self.auto_levels(view_only=True))
        h.addWidget(self.btn_auto)
        self.btn_csv = QtWidgets.QPushButton("CSV…")
        self.btn_csv.setToolTip("Export the 2D matrix (modulations × 2tR) as CSV")
        self.btn_img = QtWidgets.QPushButton("Image…")
        self.btn_img.setToolTip("Export the 2D chromatogram as a high-resolution image")
        self.btn_model = QtWidgets.QPushButton("3D model…")
        self.btn_model.setToolTip("Export a solid 3D model (GLB, FBX or STL) of the chromatogram")
        for b in (self.btn_csv, self.btn_img, self.btn_model):
            h.addWidget(b)
        v.addLayout(h)

        body = QtWidgets.QHBoxLayout()
        self.vb = Image2DViewBox()
        self.pw = pg.PlotWidget(viewBox=self.vb)
        self.pw.setLabel("bottom", "¹tR (min)")
        self.pw.setLabel("left", "²tR (s)")
        self.pw.getPlotItem().hideButtons()
        self.pw.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.pw.setMinimumSize(250, 110)
        self.img = pg.ImageItem(axisOrder="col-major")
        self.pw.addItem(self.img)
        self.hist = pg.HistogramLUTWidget(image=self.img)
        self.hist.setMinimumWidth(95)
        self.hist.setMaximumWidth(120)
        body.addWidget(self.pw, 1)
        body.addWidget(self.hist)
        v.addLayout(body, 1)

        # overlays
        self.sel_point = pg.ScatterPlotItem(size=16, symbol="+", pen=pg.mkPen("w", width=2.5), brush=None)
        self.sel_point2 = pg.ScatterPlotItem(size=14, symbol="+", pen=pg.mkPen((214, 39, 40), width=1.5),
                                             brush=None)
        self.sel_rect = self._rect_item((214, 39, 40), QtCore.Qt.SolidLine)
        self.bkg_rect = self._rect_item((255, 255, 255), QtCore.Qt.DashLine)
        self.drag_rect = self._rect_item((255, 210, 0), QtCore.Qt.DashLine)
        for it in (self.sel_point, self.sel_point2):
            it.setZValue(25)
            self.pw.addItem(it, ignoreBounds=True)
        self.blob_items = []

        self.vb.sigClick.connect(self.sigClick)
        self.vb.sigRectDrag.connect(self.sigRect)
        self.vb.sigRangeChanged.connect(lambda *_: self.sigViewChanged.emit())
        self.hist.item.sigLevelsChanged.connect(lambda *_: self.sigDisplayChanged.emit())
        self.pw.scene().sigMouseMoved.connect(self._moved)
        self._apply_cmap()

    def _rect_item(self, color, style):
        r = QtWidgets.QGraphicsRectItem()
        r.setPen(pg.mkPen(color, width=1.6, style=style, cosmetic=True))
        r.setBrush(QtGui.QBrush(QtCore.Qt.NoBrush))
        r.hide()
        r.setZValue(20)
        self.pw.addItem(r, ignoreBounds=True)
        return r

    # ---- appearance ---------------------------------------------------------------
    @property
    def scale_name(self):
        return SCALES[self.scale.currentText()]

    def mpl_cmap(self):
        return cmaps.to_matplotlib(self.stops, self.palette_name)

    def _apply_cmap(self):
        self.hist.gradient.setColorMap(cmaps.to_pyqtgraph(self.stops))
        self.sigDisplayChanged.emit()

    def set_palette(self, name):
        self.palette_name = name
        self.s.setValue("gx/palette2", name)
        self.stops = self.custom_stops if name == cmaps.CUSTOM else cmaps.PRESETS[name]
        if self.pal.currentText() != name:
            self.pal.blockSignals(True)
            self.pal.setCurrentText(name)
            self.pal.blockSignals(False)
        self._apply_cmap()

    def set_stops(self, stops, custom=True):
        self.stops = list(stops)
        if custom:
            self.custom_stops = list(stops)
            self.s.setValue("gx/custom_stops", cmaps.dumps(stops))
            self.palette_name = cmaps.CUSTOM
            self.s.setValue("gx/palette2", cmaps.CUSTOM)
            self.pal.blockSignals(True)
            self.pal.setCurrentText(cmaps.CUSTOM)
            self.pal.blockSignals(False)
        self._apply_cmap()

    def set_display(self, smooth, baseline_s):
        changed_bl = abs(baseline_s - self.baseline_s) > 1e-9
        self.smooth, self.baseline_s = bool(smooth), float(baseline_s)
        self.s.setValue("gx/smooth", str(self.smooth).lower())
        self.s.setValue("gx/baseline_s", self.baseline_s)
        if self.y is not None:
            self._compute(keep_levels=not changed_bl)

    def set_3d_colours(self, base=None, bg=None):
        if base:
            self.base_colour = base
            self.s.setValue("gx/base_colour", base)
        if bg:
            self.bg_colour = bg
            self.s.setValue("gx/bg_colour", bg)
        self.sigDisplayChanged.emit()

    def _scale_changed(self, name):
        self.s.setValue("gx/scale", name)
        if self.y is not None:
            self._compute(keep_levels=False)

    # ---- data ------------------------------------------------------------------------
    def set_data(self, y, M, fold, reset_view=False, keep_levels=False):
        """y: per-scan trace (TIC/EIC/FID), M: its raw folded matrix (used for integration)."""
        self.y, self.M, self.fold = y, M, fold
        x0, x1, y0, y1 = fold.extent
        self.vb.set_full(x0, x1, y0, y1)
        self._compute(keep_levels=keep_levels)
        if reset_view:
            self.vb.reset_view()

    def _compute(self, keep_levels=False):
        f = self.fold
        lv = self.hist.getLevels() if keep_levels else None
        if self.baseline_s > 0:
            self.Mdisp = f.matrix(display_baseline(self.y, round(self.baseline_s / f.dt)))
        else:
            self.Mdisp = self.M
        self.T = transform(self.Mdisp, self.scale_name)
        shown = smooth_upsample(self.T) if self.smooth else self.T
        self.img.setImage(shown, autoLevels=False)
        x0, x1, y0, y1 = f.extent
        self.img.setRect(QtCore.QRectF(x0, y0, x1 - x0, y1 - y0))
        if lv is not None:
            self.hist.setLevels(*lv)
        else:
            self.auto_levels(view_only=False)
        self.sigDisplayChanged.emit()

    def auto_levels(self, view_only=True):
        if self.T is None:
            return
        T = self.T
        if view_only and self.fold is not None:
            (xa, xb), (ya, yb) = self.vb.viewRange()
            j0, j1, r0, r1 = self.fold.box_pixels(xa, xb, ya, yb)
            T = T[j0:j1 + 1, r0:r1 + 1]
        if not T.size:
            return
        # noise floor = median; saturate at the 99.8th percentile so moderate peaks get real
        # colour instead of being washed out by the single biggest peak (as GCxGC software does)
        lo = float(np.nanpercentile(T, 50))
        hi = float(min(np.nanmax(T), np.nanpercentile(T, 99.8)))
        if hi <= lo:
            hi = float(np.nanmax(T))
        self.hist.setLevels(lo, hi if hi > lo else lo + 1)

    def levels(self):
        return self.hist.getLevels()

    def view_box_pixels(self):
        (xa, xb), (ya, yb) = self.vb.viewRange()
        return self.fold.box_pixels(xa, xb, ya, yb)

    def _moved(self, pos):
        if self.pw.sceneBoundingRect().contains(pos):
            p = self.vb.mapSceneToView(pos)
            self.sigHover.emit(p.x(), p.y())

    # ---- markers ----------------------------------------------------------------------
    def show_point(self, x, y):
        self.sel_rect.hide()
        self.sel_point.setData([x], [y])
        self.sel_point2.setData([x], [y])
        self.sel_xy = (x, y)
        self.sigMarker.emit(x, y)

    def show_sel_rect(self, x0, x1, y0, y1):
        self.sel_point.setData([], [])
        self.sel_point2.setData([], [])
        self.sel_rect.setRect(QtCore.QRectF(QtCore.QPointF(x0, y0), QtCore.QPointF(x1, y1)).normalized())
        self.sel_rect.show()
        self.sel_xy = ((x0 + x1) / 2, (y0 + y1) / 2)
        self.sigMarker.emit(*self.sel_xy)

    def show_bkg_rect(self, x0, x1, y0, y1):
        self.bkg_rect.setRect(QtCore.QRectF(QtCore.QPointF(x0, y0), QtCore.QPointF(x1, y1)).normalized())
        self.bkg_rect.show()

    def show_drag(self, x0, y0, x1, y1):
        self.drag_rect.setRect(QtCore.QRectF(QtCore.QPointF(x0, y0), QtCore.QPointF(x1, y1)).normalized())
        self.drag_rect.show()

    def hide_drag(self):
        self.drag_rect.hide()

    def blob_rect(self, b):
        f = self.fold
        x0 = float(f.col_start_min(b.j0))
        x1 = float(f.col_start_min(b.j1 + 1))
        return x0, x1, b.r0 * f.dt, (b.r1 + 1) * f.dt

    def draw_blobs(self, blobs, trace_key, selected=None):
        for it in self.blob_items:
            self.pw.removeItem(it)
        self.blob_items = []
        if self.fold is None:
            return
        for k, b in enumerate(blobs):
            if b.trace_key != trace_key:
                continue
            x0, x1, y0, y1 = self.blob_rect(b)
            col = (255, 165, 0) if k == selected else (255, 255, 255)
            r = QtWidgets.QGraphicsRectItem(QtCore.QRectF(x0, y0, x1 - x0, y1 - y0))
            r.setPen(pg.mkPen(col, width=2.2 if k == selected else 1.2, cosmetic=True))
            r.setZValue(15)
            self.pw.addItem(r, ignoreBounds=True)
            lab = pg.TextItem(str(k + 1), color=col, anchor=(0, 1))
            lab.setPos(x0, y1)
            lab.setZValue(16)
            self.pw.addItem(lab, ignoreBounds=True)
            self.blob_items += [r, lab]
