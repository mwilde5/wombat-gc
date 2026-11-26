"""
Spectrum windows: FT-IR (IRWindow) and any other x,y data from CSV/JCAMP (XYWindow).
Peak labelling, data tables, overlays, integration of regions and figure export.
"""
from __future__ import annotations

import csv
import os
import traceback

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

import plots_export as px
import spectra as S
import tables
from common import APP_NAME, ToolWindow, hint_label, mode_buttons, set_row, table_widget
from view_widgets import make_plot

OVERLAY_COLOURS = ["#c0392b", "#2a7a2a", "#8e44ad", "#d35400", "#16a085"]
MODES = [
    ("zoom", "1 Zoom", "Drag across the spectrum to zoom in", "1"),
    ("peaks", "2 Peaks", "Click a band/peak to add or remove its label; drag to label all peaks in a range", "2"),
    ("integrate", "3 Integrate", "Drag across a peak to measure its area (straight baseline between the ends)", "3"),
]
HINTS = {
    "zoom": "Drag to zoom · Wheel: zoom · Right-click: zoom out · Home: full spectrum",
    "peaks": "Click a peak to add/remove its label · drag to label every peak in that range",
    "integrate": "Drag from one side of a peak to the other to measure its area",
}


class SpectrumWindow(ToolWindow):
    TECHNIQUE = "Spectrum"
    KIND = "generic"

    def __init__(self):
        super().__init__()
        self.main: S.Spectrum | None = None
        self.overlays: list[S.Spectrum] = []
        self.bands: list[S.Band] = []
        self.regions: list[list] = []          # [lo, hi, area, apex_x, height, note]
        self.mode = "peaks"
        self._items = []
        self._updating = False
        self._build_ui()
        self.place(1400, 860)
        self._set_mode("peaks")

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        mb = self.menuBar()
        fm = mb.addMenu("&File")
        fm.addAction("Open…", self.open_dialog).setShortcut("Ctrl+O")
        fm.addAction("Add spectrum to overlay…", self.add_overlay_dialog).setShortcut("Ctrl+Shift+O")
        fm.addAction("Remove overlays", self.clear_overlays)
        fm.addSeparator()
        fm.addAction("Export image…", self.export_image).setShortcut("Ctrl+E")
        fm.addAction("Export spectrum (CSV)…", self.export_csv)
        fm.addAction("Export peak table (Excel/CSV)…", self.export_peaks).setShortcut("Ctrl+T")
        self.add_common_menus(fm)
        pm = mb.addMenu("&Peaks")
        pm.addAction("Find peaks (whole spectrum)", self.auto_peaks).setShortcut("Ctrl+R")
        pm.addAction("Find peaks in view", self.peaks_in_view)
        pm.addAction("Clear peak labels", self.clear_peaks)
        pm.addAction("Baseline correction", self.baseline_correct)
        hm = mb.addMenu("&Help")
        self.add_report_menu(before=hm)
        hm.addAction("How to use", self.show_help).setShortcut("F1")

        tb = self.addToolBar("Main")
        tb.setMovable(False)
        tb.addAction("Open…", self.open_dialog)
        tb.addAction("Overlay…", self.add_overlay_dialog)
        tb.addSeparator()
        self.mode_actions = mode_buttons(tb, MODES, self._set_mode, self)
        tb.addSeparator()
        tb.addAction("Find peaks", self.auto_peaks)
        tb.addWidget(QtWidgets.QLabel(" Sensitivity "))
        self.sens = QtWidgets.QSpinBox()
        self.sens.setRange(1, 100)
        self.sens.setValue(int(self.settings.value(f"spec/{self.KIND}/sens", 70)))
        self.sens.setToolTip("Higher finds smaller peaks")
        self.sens.valueChanged.connect(lambda v: self.settings.setValue(f"spec/{self.KIND}/sens", v))
        tb.addWidget(self.sens)
        self._extra_toolbar(tb)
        tb.addSeparator()
        a = tb.addAction("Full view", self.reset_zoom)
        a.setShortcut("Home")
        tb.addAction("Export image…", self.export_image)

        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.pw, self.vb = make_plot(self._yrange, "x", "y")
        self.pw.getPlotItem().setClipToView(False)
        self.pw.setDownsampling(auto=True, mode="peak")
        self.vb.sigDrag.connect(self._drag)
        self.vb.sigClick.connect(self._click)
        self.pw.scene().sigMouseMoved.connect(self._hover)
        self.curve = self.pw.plot(pen=pg.mkPen("#111111", width=1.1))
        self.ov_curves = []
        self.drag_item = pg.LinearRegionItem(orientation="vertical", movable=False,
                                             brush=pg.mkBrush(100, 100, 255, 40))
        self.drag_item.hide()
        self.vb.addItem(self.drag_item, ignoreBounds=True)
        self.legend = self.pw.getPlotItem().addLegend(offset=(-10, 10))
        split.addWidget(self.pw)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._build_peak_tab(), "Peaks")
        self.tabs.addTab(self._build_int_tab(), "Integrals")
        rep = self._build_report_tab()
        if rep is not None:
            self.tabs.addTab(rep, "Report")
        self.info = QtWidgets.QTableWidget(0, 2)
        self.info.setHorizontalHeaderLabels(["Parameter", "Value"])
        self.info.verticalHeader().hide()
        self.info.horizontalHeader().setStretchLastSection(True)
        self.info.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.tabs.addTab(self.info, "Info")
        split.addWidget(self.tabs)
        split.setSizes([920, 480])
        split.setStretchFactor(0, 1)
        self.setCentralWidget(split)
        self.readout = QtWidgets.QLabel("")
        self.status.addPermanentWidget(self.readout)
        self.hint = QtWidgets.QLabel("")
        self.hint.setStyleSheet("color:#444;")
        self.status.addWidget(self.hint, 1)
        t = pg.TextItem(html=f"<div style='font-size:13pt;color:#666'>{self._welcome_text()}</div>", anchor=(0.5, 0.5))
        self.vb.addItem(t)
        t.setPos(0.5, 0.5)
        self.vb.setRange(xRange=(0, 1), yRange=(0, 1))
        self._welcome = t

    def _welcome_text(self):
        return "Open a file (File ▸ Open, or drag it here)."

    def _extra_toolbar(self, tb):
        pass

    def _peak_headers(self):
        return ["x", "y", "Rel. %", "Assignment / note"]

    def _build_peak_tab(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        self.ptable = table_widget(self._peak_headers(), editable_cols=(len(self._peak_headers()) - 1,))
        self.ptable.itemChanged.connect(self._ptable_edited)
        self.ptable.itemSelectionChanged.connect(self._ptable_selected)
        v.addWidget(self.ptable, 1)
        h = QtWidgets.QHBoxLayout()
        for txt, fn in (("Find peaks", self.auto_peaks), ("Find in view", self.peaks_in_view),
                        ("Delete selected", self.delete_peaks), ("Clear all", self.clear_peaks)):
            b = QtWidgets.QPushButton(txt)
            b.clicked.connect(fn)
            h.addWidget(b)
        v.addLayout(h)
        v.addWidget(tables.export_buttons(w, self.export_peaks, self.copy_peaks, copy_text_="Copy for report"))
        v.addWidget(hint_label(self._peak_hint()))
        return w

    def _peak_hint(self):
        return "Double-click the last column to type an assignment. Mode 2: click a peak to add or remove it."

    def _build_int_tab(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        self.itable = table_widget(["From", "To", "Apex", "Height", "Area", "Area %", "Note"], editable_cols=(6,))
        self.itable.itemChanged.connect(self._itable_edited)
        v.addWidget(self.itable, 1)
        h = QtWidgets.QHBoxLayout()
        for txt, fn in (("Delete selected", self.delete_regions), ("Clear all", self.clear_regions)):
            b = QtWidgets.QPushButton(txt)
            b.clicked.connect(fn)
            h.addWidget(b)
        h.addStretch(1)
        v.addLayout(h)
        v.addWidget(tables.export_buttons(w, self.export_regions, self.copy_regions, copy_text_="Copy for report"))
        v.addWidget(hint_label("Mode 3: drag across a peak. The area is above a straight line joining the two ends, "
                               "in (x units × y units)."))
        return w

    def _build_report_tab(self):
        return None

    # ------------------------------------------------------------------ data
    def display(self, sp: S.Spectrum):
        """(x, y) as shown on screen."""
        return sp.x, sp.y

    def analysis_y(self, sp):
        """Signal in which peaks point UP (used for picking)."""
        return self.display(sp)[1]

    def is_sticks(self, sp):
        if sp.kind != "ms":
            return False
        d = np.diff(sp.x)
        return sp.x.size < 5000 and (d.size == 0 or np.median(d) >= 0.3)

    def open_dialog(self):
        fn = self.ask_open("Open", self._open_filter(), f"{self.KIND}_dir")
        if fn:
            self.open_path(fn)

    def _open_filter(self):
        return "Spectra (*.jdx *.dx *.csv *.txt *.tsv *.dat *.xy *.asc *.prn);;All files (*)"

    def read_file(self, path, for_overlay=False):
        """Return a list of Spectrum objects (None = cancelled)."""
        low = path.lower()
        if low.endswith((".jdx", ".dx", ".jcamp")):
            return [S.read_jcamp(path)]
        if S.is_opus(path):
            return [S.read_opus(path)]
        import csv_import as ci
        dlg = ci.ImportDialog(self, path, self.settings, forced_kind=self.KIND if self.KIND != "generic" else None)
        if dlg.exec() != QtWidgets.QDialog.Accepted or dlg.result is None:
            return None
        res = dlg.result
        if res.kind in ("chrom", "gcxgc_trace", "gcxgc_xyz", "nmr") or \
                (res.kind == "ir") != (self.KIND == "ir"):
            # belongs in another window
            self.sigImport.emit(res)
            return None
        return ci.to_spectra(res)

    sigImport = QtCore.Signal(object)

    def open_path(self, path):
        try:
            sps = self.read_file(path)
        except Exception as e:
            self.error(f"Could not read {path}\n\n{e}\n\n{traceback.format_exc(limit=2)}")
            return
        if not sps:
            return
        self.set_spectrum(sps[0])
        for sp in sps[1:]:
            self.add_overlay(sp)

    def set_spectrum(self, sp: S.Spectrum):
        if self._welcome is not None:
            self.vb.removeItem(self._welcome)
            self._welcome = None
        self.main = sp
        self.bands, self.regions = [], []
        self.overlays = []
        self._setup_axes()
        self._fill_info()
        self.setWindowTitle(f"{sp.name} — {APP_NAME} {self.TECHNIQUE}")
        self._draw()
        self.reset_zoom()
        self.auto_peaks()
        self._fill_regions()

    def _setup_axes(self):
        sp = self.main
        reverse = S.KINDS.get(sp.kind, S.KINDS["generic"])[3]
        self.vb.invertX(reverse)
        self.pw.setLabel("bottom", sp.x_label)
        self.pw.setLabel("left", self.y_label())

    def y_label(self):
        return self.main.y_label if self.main else "y"

    def add_overlay_dialog(self):
        if self.main is None:
            self.open_dialog()
            return
        if len(self.overlays) >= 4:
            self.error("Up to 4 overlays (5 spectra in total).")
            return
        fn = self.ask_open("Add spectrum to overlay", self._open_filter(), f"{self.KIND}_dir")
        if not fn:
            return
        try:
            sps = self.read_file(fn, for_overlay=True)
        except Exception as e:
            self.error(f"Could not read {fn}\n\n{e}")
            return
        for sp in sps or []:
            self.add_overlay(sp)

    def add_overlay(self, sp):
        if len(self.overlays) >= 4:
            return
        self.overlays.append(sp)
        self._draw()
        self.vb.fit_y()

    def clear_overlays(self):
        self.overlays = []
        self._draw()

    def _fill_info(self):
        sp = self.main
        rows = [("Name", sp.name), ("Type", S.KINDS.get(sp.kind, S.KINDS["generic"])[0]), ("Source", sp.source),
                ("File", sp.path), ("Points", f"{sp.x.size:,}"),
                ("Range", f"{sp.x.min():g} – {sp.x.max():g}")]
        rows += list(sp.meta.items())
        self.info.setRowCount(len(rows))
        for r, (k, v) in enumerate(rows):
            self.info.setItem(r, 0, QtWidgets.QTableWidgetItem(str(k)))
            self.info.setItem(r, 1, QtWidgets.QTableWidgetItem(str(v)))
        self.info.resizeColumnToContents(0)

    # ------------------------------------------------------------------ drawing
    def _yrange(self, x0, x1):
        if self.main is None:
            return None
        lo, hi = None, None
        for k, sp in enumerate([self.main] + self.overlays):
            x, y = self.display(sp)
            y = y + self._offset(k)
            m = (x >= min(x0, x1)) & (x <= max(x0, x1))
            if m.any():
                a, b = float(np.nanmin(y[m])), float(np.nanmax(y[m]))
                lo = a if lo is None else min(lo, a)
                hi = b if hi is None else max(hi, b)
        if hi is None:
            return None
        span = (hi - lo) or 1.0
        lab = 0.14 * span if self.bands else 0.02 * span
        if self.labels_down():
            return lo - lab, hi + 0.03 * span
        return (0.0 if self.is_sticks(self.main) else lo - 0.03 * span), hi + lab

    def labels_down(self):
        return False

    def _offset(self, k):
        return 0.0

    def _draw(self):
        sp = self.main
        if sp is None:
            return
        self.legend.clear()
        x, y = self.display(sp)
        if self.is_sticks(sp):
            xx = np.repeat(x, 2)
            yy = np.column_stack([np.zeros_like(y), y]).ravel()
            self.curve.setData(xx, yy, connect="pairs")
        else:
            self.curve.setData(x, y, connect="all")
        for c in self.ov_curves:
            self.pw.removeItem(c)
        self.ov_curves = []
        if self.overlays:
            self.legend.addItem(self.curve, sp.name)
        for k, ov in enumerate(self.overlays):
            ox, oy = self.display(ov)
            c = self.pw.plot(ox, oy + self._offset(k + 1), pen=pg.mkPen(OVERLAY_COLOURS[k], width=1.1))
            self.ov_curves.append(c)
            self.legend.addItem(c, ov.name)
        self._draw_labels()

    def _draw_labels(self):
        for it in self._items:
            self.vb.removeItem(it)
        self._items = []
        if self.main is None:
            return
        x, y = self.display(self.main)
        (vx0, vx1), (vy0, vy1) = self.vb.viewRange()
        span = float(np.nanmax(y) - np.nanmin(y)) or 1.0
        down = self.labels_down()
        for b in self.bands:
            yv = float(np.interp(b.x, x, y))
            t = pg.TextItem(self.band_text(b), color=(150, 30, 30), anchor=(1, 0.5) if down else (0, 0.5), angle=90)
            t.setPos(b.x, yv - 0.01 * span if down else yv + 0.01 * span)
            self.vb.addItem(t, ignoreBounds=True)
            self._items.append(t)
        if self.bands:
            sc = pg.ScatterPlotItem([b.x for b in self.bands], [float(np.interp(b.x, x, y)) for b in self.bands],
                                    symbol="t1" if down else "t", size=6, pen=None, brush=pg.mkBrush(200, 40, 40))
            self.vb.addItem(sc, ignoreBounds=True)
            self._items.append(sc)
        for k, (lo, hi, area, ax, h, note) in enumerate(self.regions):
            m = (x >= lo) & (x <= hi)
            if m.sum() < 2:
                continue
            xx, yy = x[m], y[m]
            base = yy[0] + (yy[-1] - yy[0]) * (xx - xx[0]) / (xx[-1] - xx[0])
            c1, c2 = pg.PlotDataItem(xx, yy), pg.PlotDataItem(xx, base)
            f = pg.FillBetweenItem(c1, c2, brush=pg.mkBrush(76, 120, 168, 80))
            self.vb.addItem(f, ignoreBounds=True)
            t = pg.TextItem(f"∫{k + 1}", color=(40, 80, 130), anchor=(0.5, 1))
            i = int(np.argmax(yy - base))
            t.setPos(float(xx[i]), float(yy[i]))
            self.vb.addItem(t, ignoreBounds=True)
            self._items += [f, t]

    def band_text(self, b):
        return f"{b.x:.1f}" if abs(b.x) < 100 else f"{b.x:.0f}"

    def reset_zoom(self):
        if self.main is None:
            return
        xs = [self.main.x] + [o.x for o in self.overlays]
        self.vb.set_full_x(float(min(a.min() for a in xs)), float(max(a.max() for a in xs)))
        self.vb.reset_view()

    # ------------------------------------------------------------------ mouse
    def _set_mode(self, k):
        self.mode = k
        self.mode_actions[k].setChecked(True)
        self.hint.setText(HINTS[k])

    def _hover(self, pos):
        if self.main is None or not self.pw.sceneBoundingRect().contains(pos):
            return
        p = self.vb.mapSceneToView(pos)
        x, y = self.display(self.main)
        yv = float(np.interp(p.x(), x, y))
        self.readout.setText(f"x {p.x():.2f}   y {yv:.4g}")

    def _drag(self, x0, x1, finished):
        if self.main is None:
            return
        lo, hi = sorted((x0, x1))
        if not finished:
            self.drag_item.setRegion((lo, hi))
            self.drag_item.show()
            return
        self.drag_item.hide()
        if hi <= lo:
            return
        if self.mode == "zoom":
            self.vb._push()
            self.vb.setXRange(lo, hi, padding=0)
        elif self.mode == "peaks":
            new = self.find_bands(lo, hi)
            self.bands = [b for b in self.bands if not (lo <= b.x <= hi)] + new
            self._after_bands()
        else:
            x, y = self.display(self.main)
            a = self.analysis_y(self.main)
            area, apex, h = S.integrate_xy(x, a, lo, hi)
            self.regions.append([lo, hi, area, apex, h, ""])
            self.regions.sort(key=lambda r: r[0])
            self._fill_regions()
            self._draw_labels()
            self.tabs.setCurrentIndex(1)

    def _click(self, x, y):
        if self.main is None or self.mode != "peaks":
            return
        (vx0, vx1), _ = self.vb.viewRange()
        tol = abs(vx1 - vx0) * 0.008
        near = [b for b in self.bands if abs(b.x - x) < tol]
        if near:
            self.bands.remove(min(near, key=lambda b: abs(b.x - x)))
        else:
            xs = self.main.x
            a = self.analysis_y(self.main)
            m = np.where((xs >= x - tol * 1.5) & (xs <= x + tol * 1.5))[0]
            if m.size == 0:
                return
            i = int(m[np.argmax(a[m])])
            if not 0 < i < xs.size - 1:
                return
            y0, y1, y2 = a[i - 1], a[i], a[i + 1]
            den = y0 - 2 * y1 + y2
            dx = 0.5 * (y0 - y2) / den if den else 0.0
            bx = float(xs[i] + max(-0.5, min(0.5, dx)) * (xs[i + 1] - xs[i]))
            from scipy.signal import peak_widths
            wi = float(peak_widths(a, [i], rel_height=0.5)[0][0] * np.median(np.abs(np.diff(xs))))
            self.bands.append(S.Band(bx, float(a[i]), float(a[i]), wi))
        self._after_bands()

    # ------------------------------------------------------------------ peaks
    def find_bands(self, lo=None, hi=None):
        a = self.analysis_y(self.main)
        if self.is_sticks(self.main):
            x = self.main.x
            m = np.ones(x.size, bool) if lo is None else (x >= lo) & (x <= hi)
            idx = np.nonzero(m)[0]
            n = max(3, int(round(self._max_bands() * self.sens.value() / 100)))
            keep = idx[np.argsort(a[idx])[::-1][:n]]
            top = float(a.max()) or 1.0
            return [S.Band(float(x[i]), float(a[i]), float(a[i])) for i in sorted(keep) if a[i] > 0.01 * top]
        rel = 0.3 * (1 - self.sens.value() / 100) ** 2 + 0.003
        step = float(np.median(np.abs(np.diff(self.main.x))))
        bands = S.pick_bands(self.main.x, a, lo, hi, min_rel=rel, min_sep=max(4 * step, self._min_sep()),
                             max_n=self._max_bands())
        return bands

    def _min_sep(self):
        return 0.0

    def _max_bands(self):
        return 40

    def auto_peaks(self):
        if self.main is None:
            return
        self.bands = self.find_bands()
        self._after_bands()

    def peaks_in_view(self):
        if self.main is None:
            return
        (x0, x1), _ = self.vb.viewRange()
        lo, hi = sorted((x0, x1))
        self.bands = [b for b in self.bands if not (lo <= b.x <= hi)] + self.find_bands(lo, hi)
        self._after_bands()

    def _after_bands(self):
        self.bands.sort(key=lambda b: b.x)
        self._classify()
        self._fill_peaks()
        self._draw_labels()
        self.vb.fit_y()
        self.update_report()

    def _classify(self):
        pass

    def delete_peaks(self):
        rows = sorted({i.row() for i in self.ptable.selectedIndexes()})
        order = self._table_order()
        drop = {id(order[r]) for r in rows if r < len(order)}
        self.bands = [b for b in self.bands if id(b) not in drop]
        self._after_bands()

    def clear_peaks(self):
        self.bands = []
        self._after_bands()

    def _table_order(self):
        return sorted(self.bands, key=lambda b: -b.x) if S.KINDS.get(self.main.kind, S.KINDS["generic"])[3] \
            else list(self.bands)

    def _peak_row(self, b, top):
        x, y = self.display(self.main)
        yv = float(np.interp(b.x, x, y))
        return [self.band_text(b), f"{yv:.4g}", f"{100 * b.height / top:.1f}" if top else "", b.note]

    def _fill_peaks(self):
        self._updating = True
        order = self._table_order()
        t = self.ptable
        t.setRowCount(len(order))
        top = max((b.height for b in order), default=1) or 1
        last = t.columnCount() - 1
        for r, b in enumerate(order):
            set_row(t, r, self._peak_row(b, top), editable=(last,))
        self._updating = False

    def _ptable_edited(self, item):
        if self._updating:
            return
        order = self._table_order()
        if item.column() == self.ptable.columnCount() - 1 and item.row() < len(order):
            order[item.row()].note = item.text().strip()
            self.update_report()

    def _ptable_selected(self):
        rows = sorted({i.row() for i in self.ptable.selectedIndexes()})
        order = self._table_order()
        if len(rows) == 1 and rows[0] < len(order):
            b = order[rows[0]]
            (x0, x1), _ = self.vb.viewRange()
            if not (min(x0, x1) <= b.x <= max(x0, x1)):
                w = abs(x1 - x0)
                self.vb.setXRange(b.x - w / 2, b.x + w / 2, padding=0)

    # ------------------------------------------------------------------ regions
    def _fill_regions(self):
        self._updating = True
        t = self.itable
        t.setRowCount(len(self.regions))
        tot = sum(r[2] for r in self.regions) or 1
        for k, (lo, hi, area, ax, h, note) in enumerate(self.regions):
            set_row(t, k, [f"{lo:.4g}", f"{hi:.4g}", f"{ax:.4g}", f"{h:.4g}", f"{area:.5g}",
                           f"{100 * area / tot:.2f}", note], editable=(6,))
        self._updating = False

    def _itable_edited(self, item):
        if self._updating:
            return
        if item.column() == 6 and item.row() < len(self.regions):
            self.regions[item.row()][5] = item.text()

    def delete_regions(self):
        rows = sorted({i.row() for i in self.itable.selectedIndexes()})
        self.regions = [r for k, r in enumerate(self.regions) if k not in rows]
        self._fill_regions()
        self._draw_labels()

    def clear_regions(self):
        self.regions = []
        self._fill_regions()
        self._draw_labels()

    def baseline_correct(self):
        if self.main is None:
            return
        self._baseline(self.main)
        for o in self.overlays:
            self._baseline(o)
        self._draw()
        self.auto_peaks()
        self.status.showMessage("Baseline corrected (rubber band)", 4000)

    def _baseline(self, sp):
        sp.y = sp.y - S.rubberband_baseline(sp.x, sp.y)

    # ------------------------------------------------------------------ report (overridden for IR)
    def update_report(self):
        pass

    # ------------------------------------------------------------------ exports
    def _meta(self):
        sp = self.main
        return [("Spectrum", sp.name), ("Type", S.KINDS.get(sp.kind, S.KINDS["generic"])[0]), ("File", sp.path)] + \
            [(k, v) for k, v in sp.meta.items()]

    def peak_rows(self, short=False):
        order = self._table_order()
        top = max((b.height for b in order), default=1) or 1
        h = [self.main.x_label, self.y_label(), "Relative intensity (%)", "Assignment / note"]
        rows = [[self.band_text(b), *self._peak_row(b, top)[1:3], b.note] for b in order]
        if short:
            h = [h[0], h[3]]
            rows = [[r[0], r[3]] for r in rows]
        return h, rows

    def export_peaks(self):
        if self.main is None or not self.bands:
            self.status.showMessage("No peaks labelled yet", 3000)
            return
        h, rows = self.peak_rows()
        fn = tables.export_table(self, self.settings, h, rows, self._meta(), f"{self.safe(self.main.name)}_peaks",
                                 sheet="Peaks")
        if fn:
            self.done(fn)

    def copy_peaks(self):
        if self.main is None or not self.bands:
            return
        h, rows = self.peak_rows(short=True)
        tables.copy_table(h, rows, f"{self.main.name} – peaks")
        self.status.showMessage("Table copied – paste into Word/PowerPoint (formatted) or Excel", 4000)

    def _region_rows(self):
        tot = sum(r[2] for r in self.regions) or 1
        h = ["Integral", f"From ({self.main.x_label})", "To", "Apex", "Height", "Area", "Area %", "Note"]
        return h, [[k + 1, f"{lo:.5g}", f"{hi:.5g}", f"{ax:.5g}", f"{hh:.5g}", f"{area:.6g}",
                    f"{100 * area / tot:.2f}", note] for k, (lo, hi, area, ax, hh, note) in enumerate(self.regions)]

    def export_regions(self):
        if self.main is None or not self.regions:
            self.status.showMessage("No integrals yet – use mode 3", 3000)
            return
        h, rows = self._region_rows()
        fn = tables.export_table(self, self.settings, h, rows, self._meta(),
                                 f"{self.safe(self.main.name)}_integrals", sheet="Integrals")
        if fn:
            self.done(fn)

    def copy_regions(self):
        if self.main is None or not self.regions:
            return
        h, rows = self._region_rows()
        tables.copy_table(h, rows, f"{self.main.name} – integrals")
        self.status.showMessage("Table copied", 3000)

    def export_csv(self):
        if self.main is None:
            return
        fn = self.save_name("Export spectrum", f"{self.safe(self.main.name)}.csv", "CSV (*.csv)")
        if not fn:
            return
        x, y = self.display(self.main)
        with open(fn, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow([self.main.x_label, self.y_label()])
            for a, b in zip(x, y):
                w.writerow([f"{a:.6g}", f"{b:.6g}"])
        self.done(fn)

    def report_sources(self):
        if self.main is None:
            return []
        out = [("the spectrum", "image", self.export_image)]
        if self.bands:
            out.append(("the peak table", "table", self.copy_peaks))
        if getattr(self, "regions", None):
            out.append(("the integral table", "table", self.copy_regions))
        rep = getattr(self, "report", None)
        if rep is not None and hasattr(self, "copy_report") and rep.toPlainText().strip():
            out.append(("the report text (experimental data)", "text", self.copy_report))
        return out

    def export_image(self):
        if self.main is None:
            return
        extras = [("labels", "Label peaks", True), ("overlays", "Include overlaid spectra", True),
                  ("integrals", "Shade integrals", bool(self.regions))]
        fn, opts, use_view = self.image_target(f"spec_{self.KIND}", self.main.name, self.safe(self.main.name), extras)
        if not fn:
            return
        xlim = tuple(self.vb.viewRange()[0]) if use_view else None
        traces = [(self.main.name, *self.display(self.main), opts.get("color"))]
        if opts.pop("overlays", True):
            traces += [(o.name, self.display(o)[0], self.display(o)[1] + self._offset(k + 1), OVERLAY_COLOURS[k])
                       for k, o in enumerate(self.overlays)]
        labels = [(b.x, self.band_text(b)) for b in self.bands] if opts.pop("labels", True) else []
        regs = [(lo, hi, f"∫{k + 1}") for k, (lo, hi, *_r) in enumerate(self.regions)] \
            if opts.pop("integrals", False) else []
        try:
            px.xy_figure(fn, traces, xlim=xlim, xlabel=self.main.x_label, ylabel=self.y_label(),
                         reverse_x=S.KINDS.get(self.main.kind, S.KINDS["generic"])[3], labels=labels,
                         labels_down=self.labels_down(), sticks=self.is_sticks(self.main), regions=regs, **opts)
        except Exception as e:
            self.error(f"Export failed:\n{e}\n{traceback.format_exc(limit=3)}")
            return
        self.done(fn)

    def show_help(self):
        QtWidgets.QMessageBox.information(self, "Spectra", """
<b>Opening</b>: JCAMP-DX files open directly; CSV/text files ask which columns to use and what the data are.
<i>Overlay…</i> adds up to four more spectra on the same axes.<br><br>
<b>Peaks</b>: <i>Find peaks</i> labels the main peaks (raise <i>Sensitivity</i> for smaller ones). In mode 2,
click a peak to add or remove a label, or drag across a range to label everything in it.
Type assignments in the last column of the table.<br><br>
<b>Integrals</b>: mode 3, drag across a peak (straight baseline between the ends).<br><br>
<b>Tables</b>: <i>Export table…</i> saves Excel/CSV; <i>Copy for report</i> pastes as a formatted table in Word.<br><br>
<b>Mouse</b>: wheel zooms, right-drag draws a zoom box, right-click zooms out, Home shows everything.
""")


# =============================================================================
class IRWindow(SpectrumWindow):
    TECHNIQUE = "FT-IR"
    KIND = "ir"

    def _welcome_text(self):
        return ("Open an FT-IR spectrum: Bruker OPUS (.0, .1 …), JCAMP-DX or CSV<br>"
                "(File ▸ Open, or drag the file here).")

    def _open_filter(self):
        return "IR spectra (*.0 *.1 *.2 *.3 *.4 *.5 *.jdx *.dx *.csv *.txt *.spa);;All files (*)"

    def _extra_toolbar(self, tb):
        tb.addSeparator()
        tb.addWidget(QtWidgets.QLabel(" Show "))
        self.ymode = QtWidgets.QComboBox()
        self.ymode.addItems(["Transmittance (%)", "Absorbance"])
        self.ymode.setCurrentIndex(int(self.settings.value("ir/ymode", 0)))
        self.ymode.currentIndexChanged.connect(self._ymode_changed)
        tb.addWidget(self.ymode)
        self.offset = QtWidgets.QDoubleSpinBox()
        self.offset.setRange(0, 200)
        self.offset.setDecimals(2)
        self.offset.setValue(0)
        self.offset.setToolTip("Shift each overlaid spectrum by this much so they don't overlap")
        self.offset.valueChanged.connect(lambda *_: (self._draw(), self.vb.fit_y()))
        tb.addWidget(QtWidgets.QLabel(" Overlay offset "))
        tb.addWidget(self.offset)

    def _ymode_changed(self, i):
        self.settings.setValue("ir/ymode", i)
        if self.main is None:
            return
        self.pw.setLabel("left", self.y_label())
        self._draw()
        self._fill_peaks()
        self.vb.fit_y()

    def transmittance(self):
        return self.ymode.currentIndex() == 0

    def display(self, sp):
        if sp.kind != "ir":
            return sp.x, sp.y
        return sp.x, (sp.transmittance_pct() if self.transmittance() else sp.absorbance())

    def analysis_y(self, sp):
        return sp.absorbance() if sp.kind == "ir" else sp.y

    def labels_down(self):
        return self.transmittance()

    def _offset(self, k):
        return -k * self.offset.value() if self.transmittance() else k * self.offset.value()

    def y_label(self):
        return "Transmittance (%)" if self.transmittance() else "Absorbance"

    def _min_sep(self):
        return 8.0

    def _peak_headers(self):
        return ["ν (cm⁻¹)", "%T / A", "Intensity", "Assignment / note"]

    def _fill_peaks(self):
        super()._fill_peaks()
        self._updating = True
        for r in range(self.ptable.rowCount()):
            it = self.ptable.item(r, 2)
            it.setFlags(it.flags() | QtCore.Qt.ItemIsEditable)
            it.setToolTip("Double-click to change (s, m, w, br, sh …)")
        self._updating = False

    def _ptable_edited(self, item):
        if self._updating:
            return
        order = self._table_order()
        if item.column() == 2 and item.row() < len(order):
            order[item.row()].label = item.text().strip()
            self._manual_labels = True
            self.update_report()
            return
        super()._ptable_edited(item)

    def _peak_hint(self):
        return ("Intensity: s = strong, m = medium, w = weak (relative to the strongest band), br = broad. "
                "Double-click <b>Assignment</b> to type e.g. <i>O–H stretch</i>.")

    def _classify(self):
        if self.main is not None:
            S.classify_ir(self.bands, self.main.absorbance())

    def _peak_row(self, b, top):
        x, y = self.display(self.main)
        yv = float(np.interp(b.x, x, y))
        return [f"{b.x:.0f}", f"{yv:.1f}" if self.transmittance() else f"{yv:.3f}", b.label, b.note]

    def band_text(self, b):
        return f"{b.x:.0f}"

    def peak_rows(self, short=False):
        order = self._table_order()
        x, y = self.display(self.main)
        h = ["Wavenumber (cm⁻¹)", self.y_label(), "Intensity", "Assignment"]
        rows = [[f"{b.x:.0f}", f"{float(np.interp(b.x, x, y)):.{1 if self.transmittance() else 3}f}", b.label, b.note]
                for b in order]
        if short:
            h = ["ν (cm⁻¹)", "Intensity", "Assignment"]
            rows = [[r[0], r[2], r[3]] for r in rows]
        return h, rows

    def _baseline(self, sp):
        a = sp.absorbance()
        a = a - S.rubberband_baseline(sp.x, a)
        sp.y, sp.y_mode = a, "A"

    def _build_report_tab(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        f = QtWidgets.QHBoxLayout()
        f.addWidget(QtWidgets.QLabel("Sampling"))
        self.technique = QtWidgets.QComboBox()
        self.technique.setEditable(True)
        self.technique.addItems(["ATR", "KBr disc", "thin film", "neat", "Nujol mull", "CHCl₃ solution", ""])
        self.technique.currentTextChanged.connect(lambda *_: self.update_report())
        f.addWidget(self.technique)
        self.with_lab = QtWidgets.QCheckBox("Include s/m/w/br")
        self.with_lab.setChecked(True)
        self.with_lab.toggled.connect(lambda *_: self.update_report())
        f.addWidget(self.with_lab)
        f.addStretch(1)
        v.addLayout(f)
        self.report = QtWidgets.QTextEdit()
        v.addWidget(self.report, 1)
        h = QtWidgets.QHBoxLayout()
        b = QtWidgets.QPushButton("Regenerate")
        b.clicked.connect(self.update_report)
        h.addWidget(b)
        b = QtWidgets.QPushButton("Copy (formatted)")
        b.clicked.connect(self.copy_report)
        h.addWidget(b)
        h.addStretch(1)
        v.addLayout(h)
        v.addWidget(hint_label("Standard experimental-section format. Assignments you type in the Peaks table are "
                               "added in square brackets. You can edit the text before copying."))
        return w

    def set_spectrum(self, sp):
        super().set_spectrum(sp)
        acc = str(sp.meta.get("Accessory", "")).upper()
        if "ATR" in acc:
            self.technique.setCurrentText("ATR")
        self.update_report()

    def update_report(self):
        if not hasattr(self, "report") or self.main is None:
            return
        txt = S.ir_report(self.bands, self.technique.currentText().strip(), with_labels=self.with_lab.isChecked()) \
            if self.bands else ""
        self.report.setHtml(self._report_html(txt))

    @staticmethod
    def _report_html(txt):
        import html
        h = html.escape(txt).replace("νmax/cm⁻¹", "<i>ν</i><sub>max</sub>/cm<sup>−1</sup>")
        return f"<span style='font-family:Calibri,Arial;font-size:11pt'>{h}</span>"

    def copy_report(self):
        plain = self.report.toPlainText()
        tables.copy_text(plain, self.report.toHtml())
        self.status.showMessage("Report copied – paste into Word to keep the formatting", 4000)

    def show_help(self):
        QtWidgets.QMessageBox.information(self, "FT-IR", """
<b>Opening</b>: Bruker OPUS files (.0, .1 …), JCAMP-DX, or CSV (you choose the columns). Overlay up to four more
spectra with <i>Overlay…</i>; <i>Overlay offset</i> separates them vertically.<br><br>
<b>Display</b>: switch between %Transmittance and Absorbance; the band positions don't change.
<i>Peaks ▸ Baseline correction</i> removes a sloping baseline (rubber band, shown as absorbance).<br><br>
<b>Bands</b>: <i>Find peaks</i> labels the main bands; raise <i>Sensitivity</i> for weaker ones. In mode 2 click a
band to add/remove it, or drag across a region. Each band gets s/m/w (strength relative to the strongest band)
and br when it is broad. Type assignments in the table.<br><br>
<b>Report</b>: the experimental-section line, e.g. <i>IR (ATR) ν<sub>max</sub>/cm<sup>−1</sup> 3344 (br), 2929 (s) …</i><br><br>
<b>Tables</b>: <i>Export table…</i> saves Excel/CSV; <i>Copy for report</i> pastes into Word as a formatted table.
""")


# =============================================================================
class XYWindow(SpectrumWindow):
    TECHNIQUE = "x,y data"
    KIND = "generic"

    def _welcome_text(self):
        return ("Open a CSV or text file: you'll choose the columns and say what the data are.<br>"
                "Chromatograms, GCxGC and NMR data open in their own windows.")

    def _max_bands(self):
        return 30 if (self.main is not None and self.main.kind != "ms") else 25

    def set_spectrum(self, sp):
        self.TECHNIQUE = S.KINDS.get(sp.kind, S.KINDS["generic"])[0]
        super().set_spectrum(sp)

    def _peak_row(self, b, top):
        x, y = self.display(self.main)
        yv = float(np.interp(b.x, x, y))
        return [self.band_text(b), f"{yv:.4g}", f"{100 * b.height / top:.1f}", b.note]

    def _peak_headers(self):
        return ["x", "y", "Rel. %", "Assignment / note"]
