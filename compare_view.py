"""
Compare up to 5 chromatograms (GC-MS, GC-FID, netCDF): stacked or overlaid, linked zoom,
peaks matched across samples, click a peak to highlight it in every sample, and a
peak × sample table.
"""
from __future__ import annotations

import os
import traceback
from dataclasses import dataclass, field

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

import hashlib

import gcms_reader as gr
import gcxgc as gx
import integration as integ
import plots_export as px
import tables
from common import APP_NAME, ToolWindow, hint_label, mode_buttons, set_row, table_widget
from view_widgets import make_plot

MAX_SAMPLES = 5
COLOURS = ["#1f3b73", "#c0392b", "#2a7a2a", "#8e44ad", "#d35400"]
SEL_BRUSH = (255, 165, 0, 110)
PEAK_BRUSH = (76, 120, 168, 45)

MODES = [
    ("inspect", "1 Inspect", "Click to put a marker at that retention time in every sample; drag to zoom", "1"),
    ("integrate", "2 Integrate", "Drag across a peak: it is integrated in every sample at once", "2"),
    ("select", "3 Select peak", "Click a peak to highlight it in all samples (Ctrl-click for several)", "3"),
]
HINTS = {
    "inspect": "Click: marker at that time in every sample · Drag: zoom · Right-click: zoom out · Home: full view",
    "integrate": "Drag from the start to the end of a peak – it is integrated in every sample, "
                 "and marked 'not detected' where it isn't there",
    "select": "Click a peak to highlight it everywhere · Ctrl-click to add more · click empty space to clear",
}


@dataclass
class Sample:
    path: str
    run: gr.GCMSRun
    name: str
    colour: str
    shift: float = 0.0          # minutes added to this sample's times for display/matching
    visible: bool = True
    y: np.ndarray | None = None
    noise: float = 1.0
    fold: object = None          # gcxgc.Fold for modulated (GCxGC) runs
    M: np.ndarray | None = None  # folded matrix of y

    @property
    def t(self):
        return self.run.times + self.shift


@dataclass
class Group2D:
    box: tuple                                   # x0, x1 (1tR min), y0, y1 (2tR s)
    blobs: dict = field(default_factory=dict)    # sample index -> gx.Blob or None
    note: str = ""
    ms_match: dict = field(default_factory=dict)

    def found(self):
        return [k for k, b in self.blobs.items() if b is not None]

    @property
    def rt1(self):
        f = [b.rt1 for b in self.blobs.values() if b is not None]
        return float(np.mean(f)) if f else (self.box[0] + self.box[1]) / 2

    @property
    def rt2(self):
        f = [b.rt2 for b in self.blobs.values() if b is not None]
        return float(np.mean(f)) if f else (self.box[2] + self.box[3]) / 2


@dataclass
class Group:
    rt: float                                   # aligned retention time (min)
    peaks: dict = field(default_factory=dict)   # sample index -> integ.Peak or None (not detected)
    note: str = ""
    ms_match: dict = field(default_factory=dict)  # sample index -> match (0-999) vs reference spectrum

    def found(self):
        return [k for k, p in self.peaks.items() if p is not None]


def estimate_noise(y, seg=60):
    """Baseline noise: standard deviation about a straight line in short stretches of the trace,
    taking the quiet ones (20th percentile). Robust to drift, solvent peaks and smooth,
    oversampled detector signals."""
    y = np.asarray(y, float)
    n = y.size // seg
    if n < 5:
        return float(np.std(y)) or 1.0
    Y = y[:n * seg].reshape(n, seg)
    x = np.arange(seg) - (seg - 1) / 2
    slope = (Y * x).sum(axis=1) / (x * x).sum()
    resid = Y - Y.mean(axis=1, keepdims=True) - slope[:, None] * x
    sd = np.sort(resid.std(axis=1))
    sd = sd[sd > 0]
    return float(sd[int(0.2 * sd.size)]) if sd.size else 1.0


def find_peaks_auto(t, y, noise, min_snr=20.0, min_rel=0.005, max_peaks=80):
    """Simple chromatographic peak finder -> list of (t_start, t_end) windows."""
    from scipy.signal import find_peaks, savgol_filter
    y = np.asarray(y, float)
    ys = savgol_filter(y, 7, 2) if y.size > 15 else y
    base = float(np.median(ys))
    # reference height for "% of largest": ignore a big early solvent peak
    i_max = int(np.argmax(ys))
    top = float(ys[i_max] - base) or 1.0
    if i_max < 0.3 * ys.size:
        j0, j1 = i_max, i_max
        while j0 > 0 and ys[j0 - 1] > base + 0.01 * top:
            j0 -= 1
        while j1 < ys.size - 1 and ys[j1 + 1] > base + 0.01 * top:
            j1 += 1
        rest = np.concatenate([ys[:j0], ys[j1 + 1:]])
        if rest.size:
            top = float(rest.max() - base) or top
    idx, props = find_peaks(ys, prominence=max(min_snr * noise, min_rel * top), width=2)
    if idx.size == 0:
        return []
    order = np.argsort(props["prominences"])[::-1][:max_peaks]
    out = []
    n = y.size
    for k in sorted(idx[order]):
        apex = ys[k]
        lo = k
        floor = np.median(ys) + 3 * noise
        while lo > 0 and ys[lo - 1] <= ys[lo] and ys[lo] > floor + 0.002 * apex:
            lo -= 1
        hi = k
        while hi < n - 1 and ys[hi + 1] <= ys[hi] and ys[hi] > floor + 0.002 * apex:
            hi += 1
        if hi - lo >= 3:
            out.append((float(t[lo]), float(t[hi])))
    return out


def spectrum_match(run_a, i_a, run_b, i_b):
    """Cosine similarity of two apex spectra on nominal mass, as 0-999 (like a library match)."""
    def vec(run, i):
        mz, ab = run.spectrum(i)
        if len(mz) == 0:
            return None
        m = np.round(np.asarray(mz)).astype(int)
        v = np.bincount(m, weights=np.asarray(ab, float), minlength=1001)[:1001]
        return np.sqrt(np.maximum(v, 0)) * np.arange(v.size) ** 0.5
    a, b = vec(run_a, i_a), vec(run_b, i_b)
    if a is None or b is None or not a.any() or not b.any():
        return None
    c = float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))
    return int(round(999 * c * c))


class ComparePlot(QtCore.QObject):
    """One PlotWidget (a stacked panel, or the single overlay plot)."""

    def __init__(self, win, sample_ids):
        super().__init__()
        self.win = win
        self.ids = sample_ids
        self.pw, self.vb = make_plot(self._yrange, "Retention time (min)", "")
        self.pw.getPlotItem().setClipToView(False)
        self.pw.setDownsampling(auto=True, mode="peak")
        self.pw.setMinimumHeight(90)
        self.items = []
        self.curves = {}
        self.label = pg.TextItem("", anchor=(1, 0))
        self.vb.addItem(self.label, ignoreBounds=True)
        self.marker = pg.InfiniteLine(angle=90, pen=pg.mkPen((90, 90, 90), width=1, style=QtCore.Qt.DashLine))
        self.marker.hide()
        self.vb.addItem(self.marker, ignoreBounds=True)
        self.drag = pg.LinearRegionItem(orientation="vertical", movable=False, brush=pg.mkBrush(100, 100, 255, 40))
        self.drag.hide()
        self.vb.addItem(self.drag, ignoreBounds=True)
        self.vb.sigXRangeChanged.connect(lambda *_: QtCore.QTimer.singleShot(0, self._place_label))

    def _yrange(self, x0, x1):
        lo, hi = None, None
        for k in self.ids:
            s = self.win.samples[k]
            if not s.visible or s.y is None:
                continue
            y = self.win.display_y(k)
            m = (s.t >= x0) & (s.t <= x1)
            if m.any():
                a, b = float(y[m].min()), float(y[m].max())
                lo = a if lo is None else min(lo, a)
                hi = b if hi is None else max(hi, b)
        if hi is None:
            return None
        return lo, hi * 1.08 if hi > 0 else hi + 1

    def _place_label(self):
        (x0, x1), (y0, y1) = self.vb.viewRange()
        self.label.setPos(x1, y1)

    def clear_items(self):
        for it in self.items:
            self.vb.removeItem(it)
        self.items = []


class CompareWindow(ToolWindow):
    TECHNIQUE = "Compare samples"

    def __init__(self):
        super().__init__()
        self.samples: list[Sample] = []
        self.groups: list[Group] = []
        self.groups2d: list[Group2D] = []
        self.panels2d = []
        self.selected: set[int] = set()
        self.mode = "integrate"
        self.layout_mode = self.settings.value("cmp/layout", "stacked")
        self.trace_mode = "TIC"
        self.marker_t = None
        self.plots: list[ComparePlot] = []
        self._updating = False
        self._build_ui()
        self.place(1500, 950)
        self._set_mode("integrate")
        self._rebuild_plots()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        mb = self.menuBar()
        fm = mb.addMenu("&File")
        fm.addAction("Add sample (.D folder)…", self.add_folder_dialog).setShortcut("Ctrl+O")
        fm.addAction("Add sample file (.cdf / DATA.MS / .ch)…", self.add_file_dialog).setShortcut("Ctrl+Shift+O")
        fm.addSeparator()
        fm.addAction("Export comparison table (Excel/CSV)…", self.export_table).setShortcut("Ctrl+T")
        fm.addAction("Export image…", self.export_image).setShortcut("Ctrl+E")
        self.add_common_menus(fm)
        am = mb.addMenu("&Peaks")
        am.addAction("Find peaks automatically", self.auto_peaks).setShortcut("Ctrl+R")
        am.addAction("Align samples on the selected peak", self.align_on_selected)
        am.addAction("Auto-align (cross-correlation)", self.auto_align)
        am.addAction("Reset alignment", self.reset_alignment)
        am.addSeparator()
        am.addAction("Delete selected peaks", self.delete_selected).setShortcut(QtGui.QKeySequence.Delete)
        am.addAction("Clear all peaks", self.clear_groups)
        hm = mb.addMenu("&Help")
        hm.addAction("How to compare samples", self.show_help).setShortcut("F1")

        tb = self.addToolBar("Main")
        tb.setMovable(False)
        tb.addAction("Add sample…", self.add_folder_dialog)
        tb.addSeparator()
        self.mode_actions = mode_buttons(tb, MODES, self._set_mode, self)
        tb.addSeparator()
        tb.addAction("Find peaks", self.auto_peaks)
        tb.addAction("Align on selected", self.align_on_selected)
        tb.addSeparator()
        self.layout_combo = QtWidgets.QComboBox()
        self.layout_combo.addItems(["Stacked", "Overlaid", "2D (GCxGC)"])
        self.layout_combo.setCurrentIndex(0 if self.layout_mode == "stacked" else 1)
        self.layout_combo.setItemData(2, "Side-by-side 2D plots – available when every sample is GCxGC (netCDF)",
                                      QtCore.Qt.ToolTipRole)
        self.layout_combo.currentIndexChanged.connect(self._layout_changed)
        tb.addWidget(QtWidgets.QLabel(" View "))
        tb.addWidget(self.layout_combo)
        self.norm_chk = QtWidgets.QCheckBox("Normalise heights")
        self.norm_chk.setToolTip("Scale every sample so its largest peak is 100 % (display only – areas are unchanged)")
        self.norm_chk.toggled.connect(lambda *_: self._redraw())
        tb.addWidget(self.norm_chk)
        a = tb.addAction("Full view", self.reset_zoom)
        a.setShortcut("Home")
        tb.addAction("Export table…", self.export_table)
        tb.addAction("Export image…", self.export_image)

        tb2 = QtWidgets.QToolBar("Trace")
        tb2.setMovable(False)
        self.addToolBarBreak()
        self.addToolBar(tb2)
        tb2.addWidget(QtWidgets.QLabel(" Trace "))
        self.trace_combo = QtWidgets.QComboBox()
        self.trace_combo.addItems(["TIC / detector signal", "EIC (m/z below)"])
        self.trace_combo.currentIndexChanged.connect(self._trace_changed)
        tb2.addWidget(self.trace_combo)
        self.eic_edit = QtWidgets.QLineEdit()
        self.eic_edit.setPlaceholderText("m/z, e.g. 91 or 91, 105")
        self.eic_edit.setMaximumWidth(160)
        self.eic_edit.returnPressed.connect(lambda: (self.trace_combo.setCurrentIndex(1), self._trace_changed()))
        tb2.addWidget(self.eic_edit)
        tb2.addSeparator()
        tb2.addWidget(QtWidgets.QLabel(" Match peaks within "))
        self.tol = QtWidgets.QDoubleSpinBox()
        self.tol.setRange(0.002, 2.0)
        self.tol.setDecimals(3)
        self.tol.setSingleStep(0.01)
        self.tol.setValue(float(self.settings.value("cmp/tol", 0.05)))
        self.tol.setSuffix(" min")
        self.tol.setToolTip("Peaks in different samples closer than this (after alignment) are treated as the same compound")
        self.tol.valueChanged.connect(lambda x: self.settings.setValue("cmp/tol", x))
        tb2.addWidget(self.tol)
        tb2.addWidget(QtWidgets.QLabel("  Auto-find: S/N ≥ "))
        self.snr = QtWidgets.QDoubleSpinBox()
        self.snr.setRange(3, 1000)
        self.snr.setValue(float(self.settings.value("cmp/snr", 30)))
        self.snr.valueChanged.connect(lambda x: self.settings.setValue("cmp/snr", x))
        tb2.addWidget(self.snr)
        tb2.addWidget(QtWidgets.QLabel("  and ≥ "))
        self.minrel = QtWidgets.QDoubleSpinBox()
        self.minrel.setRange(0.0, 50)
        self.minrel.setDecimals(2)
        self.minrel.setValue(float(self.settings.value("cmp/minrel", 0.5)))
        self.minrel.setSuffix(" % of largest")
        self.minrel.valueChanged.connect(lambda x: self.settings.setValue("cmp/minrel", x))
        tb2.addWidget(self.minrel)
        tb2.addSeparator()
        tb2.addWidget(QtWidgets.QLabel(" Baseline "))
        self.base_combo = QtWidgets.QComboBox()
        for k, txt in integ.BASELINES.items():
            self.base_combo.addItem(txt, k)
        tb2.addWidget(self.base_combo)

        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        # left: sample list
        left = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(left)
        lv.setContentsMargins(4, 4, 4, 4)
        lv.addWidget(QtWidgets.QLabel("<b>Samples</b> (up to 5)"))
        self.stable = table_widget(["", "Sample", "Shift (min)", "Detector"], editable_cols=(1, 2))
        self.stable.itemChanged.connect(self._sample_edited)
        lv.addWidget(self.stable, 1)
        h = QtWidgets.QHBoxLayout()
        for txt, fn in (("Add .D…", self.add_folder_dialog), ("Add file…", self.add_file_dialog),
                        ("Remove", self.remove_selected_samples)):
            b = QtWidgets.QPushButton(txt)
            b.clicked.connect(fn)
            h.addWidget(b)
        lv.addLayout(h)
        lv.addWidget(hint_label("Tick/untick to show or hide a sample. Edit <b>Shift</b> to line up "
                                "retention times, or select a peak and use <i>Align on selected</i>. "
                                "Double-click a name to rename it."))
        split.addWidget(left)

        right = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.plot_host = QtWidgets.QWidget()
        self.plot_layout = QtWidgets.QVBoxLayout(self.plot_host)
        self.plot_layout.setContentsMargins(0, 0, 0, 0)
        self.plot_layout.setSpacing(0)
        right.addWidget(self.plot_host)

        bottom = QtWidgets.QWidget()
        bv = QtWidgets.QVBoxLayout(bottom)
        bv.setContentsMargins(4, 2, 4, 2)
        hb = QtWidgets.QHBoxLayout()
        hb.addWidget(QtWidgets.QLabel("<b>Peaks in each sample</b>"))
        hb.addStretch(1)
        hb.addWidget(QtWidgets.QLabel("Show"))
        self.value_combo = QtWidgets.QComboBox()
        self.value_combo.addItems(["Area", "Area % (of the peaks in the table)", "Height", "Ratio to reference peak"])
        self.value_combo.setToolTip("Ratio: select one row and choose Peaks ▸ … or right-click ▸ Use as reference "
                                    "(e.g. an internal standard)")
        self.value_combo.currentIndexChanged.connect(lambda *_: self._fill_table())
        hb.addWidget(self.value_combo)
        bv.addLayout(hb)
        self.ptable = QtWidgets.QTableWidget(0, 3)
        self.ptable.verticalHeader().hide()
        self.ptable.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.ptable.setEditTriggers(QtWidgets.QAbstractItemView.DoubleClicked | QtWidgets.QAbstractItemView.EditKeyPressed)
        self.ptable.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        self.ptable.itemSelectionChanged.connect(self._table_selected)
        self.ptable.itemChanged.connect(self._table_edited)
        self.ptable.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.ptable.customContextMenuRequested.connect(self._table_menu)
        bv.addWidget(self.ptable, 1)
        hb2 = QtWidgets.QHBoxLayout()
        for txt, fn in (("Delete selected", self.delete_selected), ("Clear all", self.clear_groups)):
            b = QtWidgets.QPushButton(txt)
            b.clicked.connect(fn)
            hb2.addWidget(b)
        hb2.addWidget(tables.export_buttons(bottom, self.export_table, self.copy_table, copy_text_="Copy for report"))
        hb2.addStretch(1)
        bv.addLayout(hb2)
        right.addWidget(bottom)
        right.setSizes([620, 260])
        split.addWidget(right)
        split.setSizes([330, 1170])
        split.setStretchFactor(1, 1)
        self.setCentralWidget(split)
        self.ref_group = None
        self.readout = QtWidgets.QLabel("")
        self.status.addPermanentWidget(self.readout)
        self.hint = QtWidgets.QLabel("")
        self.hint.setStyleSheet("color:#444;")
        self.status.addWidget(self.hint, 1)

    # ------------------------------------------------------------------ samples
    def add_folder_dialog(self):
        if len(self.samples) >= MAX_SAMPLES:
            self.error(f"Up to {MAX_SAMPLES} samples can be compared at once.")
            return
        d = self.ask_open_dir("Choose an Agilent .D folder", "cmp_dir")
        if d:
            self.add_path(d)

    def add_file_dialog(self):
        if len(self.samples) >= MAX_SAMPLES:
            self.error(f"Up to {MAX_SAMPLES} samples can be compared at once.")
            return
        start = self.settings.value("cmp_dir", os.path.expanduser("~"))
        fns, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Add samples", start, "Chromatograms (*.cdf *.nc *.ms *.MS *.ch *.CH);;All files (*)")
        for fn in fns[:MAX_SAMPLES - len(self.samples)]:
            self.settings.setValue("cmp_dir", os.path.dirname(fn))
            self.add_path(fn)

    def open_path(self, path):
        self.add_path(path)

    def add_path(self, path):
        if not path:
            return
        if len(self.samples) >= MAX_SAMPLES:
            self.error(f"Up to {MAX_SAMPLES} samples can be compared at once – remove one first.")
            return
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            p = path.rstrip("/\\")
            if os.path.isdir(p) and not p.lower().endswith(".d"):
                ds = sorted(os.path.join(p, n) for n in os.listdir(p) if n.lower().endswith(".d"))
                if ds:
                    QtWidgets.QApplication.restoreOverrideCursor()
                    for d in ds[:MAX_SAMPLES - len(self.samples)]:
                        self.add_path(d)
                    return
            if p.lower().endswith(".ch") and os.path.isfile(p):
                run = gr.read_signal(p)
            else:
                run = gr.read_d(p)
        except Exception as e:
            QtWidgets.QApplication.restoreOverrideCursor()
            self.error(f"Could not read {path}\n\n{e}\n\n{traceback.format_exc(limit=2)}")
            return
        QtWidgets.QApplication.restoreOverrideCursor()
        used = {s.colour for s in self.samples}
        colour = next(c for c in COLOURS if c not in used)
        s = Sample(path=p, run=run, name=run.name, colour=colour)
        self.samples.append(s)
        self._set_fold(s)
        self._compute_trace(len(self.samples) - 1)
        # integrate existing peak windows in the new sample too
        k = len(self.samples) - 1
        for g in self.groups:
            ref = next((g.peaks[j] for j in g.found()), None)
            if ref is not None:
                ref_s = self.samples[g.found()[0]]
                g.peaks[k] = self._integrate_in(k, ref.t_start + ref_s.shift, ref.t_end + ref_s.shift)
        self._compute_matches()
        self._fill_samples()
        self._rebuild_plots()
        self._fill_table()
        if len(self.samples) == 1:
            self.reset_zoom()
        self.status.showMessage(f"Added {run.name} ({run.source})", 5000)

    def remove_selected_samples(self):
        rows = sorted({i.row() for i in self.stable.selectedIndexes()}, reverse=True)
        if not rows:
            self.status.showMessage("Select a sample in the list first", 3000)
            return
        for r in rows:
            del self.samples[r]
            for g in self.groups:
                g.peaks = {(j if j < r else j - 1): p for j, p in g.peaks.items() if j != r}
                g.ms_match = {(j if j < r else j - 1): m for j, m in g.ms_match.items() if j != r}
        self.groups = [g for g in self.groups if g.found()]
        self.selected = set()
        self._fill_samples()
        self._rebuild_plots()
        self._fill_table()

    def _fill_samples(self):
        self._updating = True
        t = self.stable
        t.setRowCount(len(self.samples))
        for r, s in enumerate(self.samples):
            set_row(t, r, ["", s.name, f"{s.shift:+.3f}", s.run.trace_name if not s.run.has_spectra else "MS"],
                    editable=(1, 2))
            chk = QtWidgets.QTableWidgetItem()
            chk.setFlags(QtCore.Qt.ItemIsUserCheckable | QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable)
            chk.setCheckState(QtCore.Qt.Checked if s.visible else QtCore.Qt.Unchecked)
            pm = QtGui.QPixmap(14, 14)
            pm.fill(QtGui.QColor(s.colour))
            chk.setIcon(QtGui.QIcon(pm))
            t.setItem(r, 0, chk)
            t.item(r, 1).setToolTip(s.path)
        self._updating = False

    def _sample_edited(self, item):
        if self._updating:
            return
        r, c = item.row(), item.column()
        if r >= len(self.samples):
            return
        s = self.samples[r]
        if c == 0:
            s.visible = item.checkState() == QtCore.Qt.Checked
            self._redraw()
        elif c == 1:
            s.name = item.text().strip() or s.run.name
            self._rebuild_plots()
            self._fill_table()
        elif c == 2:
            try:
                s.shift = float(item.text())
            except ValueError:
                pass
            self._regroup()
            QtCore.QTimer.singleShot(0, self._fill_samples)

    def _set_fold(self, s):
        """GCxGC: use the modulation the user set for this file in the main window, else detect it."""
        key = "fold/" + hashlib.md5(os.path.abspath(s.path).encode()).hexdigest()[:12]
        saved = self.settings.value(key)
        period, phase = 0.0, 0.0
        if saved:
            period, phase = (float(x) for x in str(saved).split("|"))
        elif s.run.source.startswith("ANDI") or s.path.lower().endswith((".cdf", ".nc")):
            p, _ = gr.guess_modulation_period(s.run, os.path.basename(s.path), signal_only_ok=True)
            period = p or 0.0
        s.fold = gx.Fold(s.run.times, period, phase) if period > 0 else None

    # ------------------------------------------------------------------ traces
    def _eic_masses(self):
        vals = []
        for tok in self.eic_edit.text().replace(";", ",").split(","):
            try:
                vals.append(float(tok))
            except ValueError:
                pass
        return vals

    def trace_label(self):
        if self.trace_combo.currentIndex() == 1 and self._eic_masses():
            return "EIC m/z " + ", ".join(f"{m:g}" for m in self._eic_masses())
        return "TIC / signal"

    def _compute_trace(self, k):
        s = self.samples[k]
        if self.trace_combo.currentIndex() == 1 and s.run.has_spectra and self._eic_masses():
            s.y = np.sum([s.run.eic(m, 0.5) for m in self._eic_masses()], axis=0).astype(float)
        else:
            s.y = np.asarray(s.run.tic, float)
        s.noise = estimate_noise(s.y)
        if s.fold is not None:
            s.M = s.fold.matrix(s.y)

    def _trace_changed(self, *_):
        if self.trace_combo.currentIndex() == 1 and not self._eic_masses():
            self.eic_edit.setFocus()
            return
        for k in range(len(self.samples)):
            self._compute_trace(k)
        # re-integrate every peak on the new trace (same windows)
        for g in self.groups:
            for k, p in list(g.peaks.items()):
                ref = next((g.peaks[j] for j in g.found()), None)
                if ref is None:
                    continue
                rs = self.samples[g.found()[0]]
                g.peaks[k] = self._integrate_in(k, ref.t_start + rs.shift, ref.t_end + rs.shift)
        self.groups = [g for g in self.groups if g.found()]
        for g in self.groups2d:
            for k in range(len(self.samples)):
                g.blobs[k] = self._integrate_box(k, *g.box)
        self.groups2d = [g for g in self.groups2d if g.found()]
        if self.is2d():
            for k, pnl in enumerate(self.panels2d):
                s = self.samples[k]
                pnl.set_data(s.y, s.M, s.fold, keep_levels=False)
        self._redraw()
        self._fill_table()

    def display_y(self, k):
        s = self.samples[k]
        if not self.norm_chk.isChecked():
            return s.y
        top = float(s.y.max() - np.median(s.y)) or 1.0
        return (s.y - np.median(s.y)) / top * 100

    # ------------------------------------------------------------------ plots
    def is2d(self):
        return self.layout_mode == "2d"

    def _layout_changed(self, i):
        if i == 2 and not (self.samples and all(s.fold is not None for s in self.samples)):
            self.status.showMessage("2D view needs every sample to be GCxGC data with a modulation period", 6000)
            self.layout_combo.blockSignals(True)
            self.layout_combo.setCurrentIndex(0 if self.layout_mode != "overlaid" else 1)
            self.layout_combo.blockSignals(False)
            return
        self.layout_mode = ["stacked", "overlaid", "2d"][i]
        if i < 2:
            self.settings.setValue("cmp/layout", self.layout_mode)
        self.selected = set()
        xr = self.plots[0].vb.viewRange()[0] if self.plots else None
        self.ref_group = None
        self._rebuild_plots()
        if xr and self.plots:
            self.plots[0].vb.setXRange(*xr, padding=0)

    def _rebuild_plots(self):
        for p in self.plots:
            self.plot_layout.removeWidget(p.pw)
            p.pw.deleteLater()
        self.plots = []
        for pnl in self.panels2d:
            pnl.setParent(None)
            pnl.deleteLater()
        self.panels2d = []
        if getattr(self, "grid2d_host", None) is not None:
            self.plot_layout.removeWidget(self.grid2d_host)
            self.grid2d_host.deleteLater()
            self.grid2d_host = None
        if self.is2d():
            self._build_2d()
            self._fill_table()
            return
        if self.layout_mode == "stacked" and self.samples:
            sets = [[k] for k in range(len(self.samples))]
        else:
            sets = [list(range(len(self.samples)))]
        for n, ids in enumerate(sets):
            cp = ComparePlot(self, ids)
            if n > 0:
                cp.pw.setXLink(self.plots[0].pw)
            if n < len(sets) - 1:
                cp.pw.getPlotItem().getAxis("bottom").setLabel("")
            cp.vb.sigDrag.connect(lambda a, b, f, cp=cp: self._drag(cp, a, b, f))
            cp.vb.sigClick.connect(lambda x, y, cp=cp: self._click(cp, x, y))
            cp.pw.scene().sigMouseMoved.connect(lambda pos, cp=cp: self._hover(cp, pos))
            for k in ids:
                s = self.samples[k]
                cp.curves[k] = cp.pw.plot(pen=pg.mkPen(s.colour, width=1.2), name=s.name)
            self.plots.append(cp)
            self.plot_layout.addWidget(cp.pw, 1)
        if self.layout_mode != "stacked" and self.samples:
            leg = self.plots[0].pw.getPlotItem().addLegend(offset=(-10, 10))
            for k in self.plots[0].ids:
                leg.addItem(self.plots[0].curves[k], self.samples[k].name)
        if not self.samples:
            cp = self.plots[0]
            t = pg.TextItem(html="<div style='font-size:13pt;color:#666'>Add up to 5 samples: "
                                 "File ▸ Add sample, or drag .D folders / .cdf files here.<br>"
                                 "Then drag across a peak to compare it in every sample.</div>", anchor=(0.5, 0.5))
            cp.vb.addItem(t)
            t.setPos(0.5, 0.5)
            cp.vb.setRange(xRange=(0, 1), yRange=(0, 1))
        self._redraw()

    def _full_range(self):
        if not self.samples:
            return 0.0, 1.0
        return min(float(s.t.min()) for s in self.samples), max(float(s.t.max()) for s in self.samples)

    def reset_zoom(self):
        x0, x1 = self._full_range()
        for p in self.plots:
            p.vb.set_full_x(x0, x1)
        if self.plots:
            self.plots[0].vb.reset_view()
            for p in self.plots[1:]:
                p.vb.auto_y = True
                p.vb.fit_y()

    def _redraw(self):
        if not self.samples:
            return
        if self.is2d():
            self._draw_2d()
            return
        x0, x1 = self._full_range()
        for cp in self.plots:
            cp.vb.set_full_x(x0, x1)
            cp.clear_items()
            for k in cp.ids:
                s = self.samples[k]
                c = cp.curves[k]
                c.setData(s.t, self.display_y(k))
                c.setVisible(s.visible)
            if len(cp.ids) == 1:
                k = cp.ids[0]
                s = self.samples[k]
                cp.label.setHtml(f"<span style='color:{s.colour};font-weight:600'>&nbsp;{s.name}</span>"
                                 f"<span style='color:#777'>&nbsp;&nbsp;{s.run.trace_name if not s.run.has_spectra else self.trace_label()}"
                                 f"{'  shift %+.3f min' % s.shift if s.shift else ''}</span>")
            else:
                cp.label.setHtml(f"<span style='color:#555'>&nbsp;{self.trace_label()}</span>")
            self._draw_groups(cp)
            cp.marker.setVisible(self.marker_t is not None)
            if self.marker_t is not None:
                cp.marker.setValue(self.marker_t)
            cp.vb.fit_y()
            cp._place_label()

    def _draw_groups(self, cp: ComparePlot):
        overlay = len(cp.ids) > 1
        for gi, g in enumerate(self.groups):
            sel = gi in self.selected
            for k in cp.ids:
                s = self.samples[k]
                if not s.visible:
                    continue
                p = g.peaks.get(k)
                yk = self.display_y(k)
                if p is None:
                    if sel:
                        # not detected: dashed red line + label
                        ln = pg.InfiniteLine(g.rt, angle=90, pen=pg.mkPen((200, 40, 40), width=1.5,
                                                                          style=QtCore.Qt.DashLine))
                        cp.vb.addItem(ln, ignoreBounds=True)
                        cp.items.append(ln)
                        if not overlay:
                            i = int(np.argmin(np.abs(s.t - g.rt)))
                            tx = pg.TextItem(f"#{gi + 1} not detected", color=(200, 40, 40), anchor=(0.5, 1))
                            tx.setPos(g.rt, float(yk[max(0, i - 3):i + 4].max()))
                            cp.vb.addItem(tx, ignoreBounds=True)
                            cp.items.append(tx)
                    continue
                t = s.t[p.i0:p.i1 + 1]
                yy = yk[p.i0:p.i1 + 1]
                if self.norm_chk.isChecked():
                    top = float(s.y.max() - np.median(s.y)) or 1.0
                    base = (p.baseline_at(s.run.times[p.i0:p.i1 + 1]) - np.median(s.y)) / top * 100
                else:
                    base = p.baseline_at(s.run.times[p.i0:p.i1 + 1])
                if overlay and not sel:
                    continue
                c1 = pg.PlotDataItem(t, yy)
                c2 = pg.PlotDataItem(t, base)
                brush = SEL_BRUSH if sel else PEAK_BRUSH
                if overlay:
                    col = QtGui.QColor(s.colour)
                    brush = (col.red(), col.green(), col.blue(), 70)
                fill = pg.FillBetweenItem(c1, c2, brush=pg.mkBrush(brush))
                fill.setZValue(-2)
                cp.vb.addItem(fill, ignoreBounds=True)
                cp.items.append(fill)
                if not overlay or sel:
                    ia = int(np.argmax(yy - base)) if yy.size else 0
                    tx = pg.TextItem(f"#{gi + 1}", color=(200, 90, 0) if sel else (60, 90, 140), anchor=(0.5, 1))
                    tx.setPos(float(t[ia]), float(yy[ia]))
                    cp.vb.addItem(tx, ignoreBounds=True)
                    cp.items.append(tx)

    # ------------------------------------------------------------------ 2D (GCxGC)
    def _build_2d(self):
        import gcxgc_view as gxv
        host = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(2)
        n = len(self.samples)
        cols = 1 if n == 1 else 2 if n <= 4 else 3
        for k, s in enumerate(self.samples):
            pnl = gxv.GCxGCPanel(self.settings)
            for b in (pnl.btn_csv, pnl.btn_img, pnl.btn_model):
                b.hide()
            pnl.title.setText(f"<span style='color:{s.colour}'>{s.name}</span>")
            pnl.set_data(s.y, s.M, s.fold, reset_view=True)
            if k > 0:
                pnl.vb.setXLink(self.panels2d[0].vb)
                pnl.vb.setYLink(self.panels2d[0].vb)
                # one set of colour / scale controls (on the first panel) drives every panel
                for wdg in pnl.findChildren(QtWidgets.QWidget):
                    if wdg in (pnl.pal, pnl.btn_display, pnl.scale) or (
                            isinstance(wdg, QtWidgets.QLabel) and wdg.text() in ("Colours", "Scale")):
                        wdg.hide()
            pnl.sigClick.connect(lambda x, y, kk=k: self._click2d(kk, x, y))
            pnl.sigRect.connect(lambda x0, y0, x1, y1, f, kk=k: self._rect2d(kk, x0, y0, x1, y1, f))
            pnl.sigHover.connect(lambda x, y, kk=k: self._hover2d(kk, x, y))
            grid.addWidget(pnl, k // cols, k % cols)
            self.panels2d.append(pnl)
        self.grid2d_host = host
        self.plot_layout.addWidget(host, 1)
        p0 = self.panels2d[0]
        p0.pal.currentTextChanged.connect(lambda *_: self._sync_2d_display())
        p0.scale.currentTextChanged.connect(lambda *_: self._sync_2d_display())
        p0.sigDisplayChanged.connect(self._sync_2d_display)
        self._draw_2d()

    def _sync_2d_display(self):
        if len(self.panels2d) < 2:
            return
        p0 = self.panels2d[0]
        for p in self.panels2d[1:]:
            if p.scale.currentText() != p0.scale.currentText():
                p.scale.setCurrentText(p0.scale.currentText())
            if list(p.stops) != list(p0.stops):
                p.stops = list(p0.stops)
                p._apply_cmap()
            if (p.smooth, p.baseline_s) != (p0.smooth, p0.baseline_s):
                p.set_display(p0.smooth, p0.baseline_s)

    def _integrate_box(self, k, x0, x1, y0, y1):
        s = self.samples[k]
        if s.fold is None or s.M is None:
            return None
        j0, j1, r0, r1 = s.fold.box_pixels(x0, x1, y0, y1)
        b = gx.integrate_box(s.fold, s.M, j0, j1, r0, r1, "trace", "line")
        if b is None or b.height < 10 * s.noise or b.volume <= 0:
            return None
        # a real maximum must sit inside the box, not on its edge
        if b.r0 + 0 == int(round(b.rt2 / s.fold.dt)) or b.r1 == int(round(b.rt2 / s.fold.dt)):
            if b.height < 30 * s.noise:
                return None
        return b

    def _rect2d(self, k, x0, y0, x1, y1, finished):
        if not finished:
            for p in self.panels2d:
                p.show_drag(x0, y0, x1, y1)
            return
        for p in self.panels2d:
            p.hide_drag()
        xa, xb = sorted((x0, x1))
        ya, yb = sorted((y0, y1))
        if self.mode == "integrate":
            g = Group2D(box=(xa, xb, ya, yb))
            for j in range(len(self.samples)):
                g.blobs[j] = self._integrate_box(j, xa, xb, ya, yb)
            if not g.found():
                self.status.showMessage("No peak inside that box in any sample", 4000)
                return
            g.ms_match = {}
            fk = [j for j in g.found() if self.samples[j].run.has_spectra]
            for j in fk[1:]:
                g.ms_match[j] = spectrum_match(self.samples[fk[0]].run, g.blobs[fk[0]].apex_scan,
                                               self.samples[j].run, g.blobs[j].apex_scan)
            self.groups2d.append(g)
            self.groups2d.sort(key=lambda g: (g.rt1, g.rt2))
            self.selected = {self.groups2d.index(g)}
            self._fill_table()
            self._draw_2d()
            miss = [self.samples[j].name for j in range(len(self.samples)) if g.blobs.get(j) is None]
            self.status.showMessage(f"2D peak at {g.rt1:.2f} min / {g.rt2:.2f} s: found in {len(g.found())} of "
                                    f"{len(self.samples)}" + (f" (not in {', '.join(miss)})" if miss else ""), 6000)
        else:
            vb = self.panels2d[k].vb
            vb._push()
            vb.setRange(xRange=(xa, xb), yRange=(ya, yb), padding=0)

    def _click2d(self, k, x, y):
        if self.mode == "inspect" or not self.groups2d:
            for p in self.panels2d:
                p.show_point(x, y)
            msg = []
            for s in self.samples:
                i = s.fold.scan_at(x, y)
                msg.append(f"{s.name}: {s.y[i]:.4g}")
            self.status.showMessage(f"{x:.2f} min, {y:.2f} s — " + " · ".join(msg), 8000)
            return
        hit = None
        for gi, g in enumerate(self.groups2d):
            x0, x1, y0, y1 = g.box
            if x0 <= x <= x1 and y0 <= y <= y1:
                hit = gi
                break
        ctrl = QtWidgets.QApplication.keyboardModifiers() & (QtCore.Qt.ControlModifier | QtCore.Qt.MetaModifier)
        if hit is None:
            if not ctrl:
                self.selected = set()
            for p in self.panels2d:
                p.show_point(x, y)
        elif ctrl:
            self.selected ^= {hit}
        else:
            self.selected = {hit}
        self._sync_table_selection()
        self._draw_2d()

    def _hover2d(self, k, x, y):
        bits = []
        for s in self.samples:
            if s.fold is not None:
                bits.append(f"{s.name[:18]}: {s.y[s.fold.scan_at(x, y)]:.4g}")
        self.readout.setText(f"¹tR {x:.2f} min  ²tR {y:.2f} s   " + "   ".join(bits))

    def _draw_2d(self):
        for k, pnl in enumerate(self.panels2d):
            for it in getattr(pnl, "_cmp_items", []):
                pnl.pw.removeItem(it)
            pnl._cmp_items = []
            for gi, g in enumerate(self.groups2d):
                sel = gi in self.selected
                b = g.blobs.get(k)
                if b is not None:
                    x0, x1, y0, y1 = pnl.blob_rect(b)
                    col = (255, 165, 0) if sel else (255, 255, 255)
                    style = QtCore.Qt.SolidLine
                    lab = f"{gi + 1}"
                else:
                    x0, x1, y0, y1 = g.box
                    col = (230, 60, 60)
                    style = QtCore.Qt.DashLine
                    lab = f"{gi + 1} n.d." if sel else f"{gi + 1}"
                    if not sel:
                        col = (230, 120, 120)
                r = QtWidgets.QGraphicsRectItem(QtCore.QRectF(x0, y0, x1 - x0, y1 - y0))
                r.setPen(pg.mkPen(col, width=2.4 if sel else 1.2, style=style, cosmetic=True))
                r.setZValue(15)
                pnl.pw.addItem(r, ignoreBounds=True)
                t = pg.TextItem(lab, color=col, anchor=(0, 1))
                t.setPos(x0, y1)
                t.setZValue(16)
                pnl.pw.addItem(t, ignoreBounds=True)
                pnl._cmp_items += [r, t]

    # ------------------------------------------------------------------ mouse
    def _set_mode(self, k):
        self.mode = k
        self.mode_actions[k].setChecked(True)
        self.hint.setText(HINTS[k])

    def _hover(self, cp, pos):
        if not self.samples or not cp.pw.sceneBoundingRect().contains(pos):
            return
        x = cp.vb.mapSceneToView(pos).x()
        bits = []
        for k in cp.ids:
            s = self.samples[k]
            i = int(np.argmin(np.abs(s.t - x)))
            bits.append(f"{s.name[:18]}: {s.y[i]:.4g}")
        self.readout.setText(f"{x:.3f} min   " + "   ".join(bits))

    def _drag(self, cp, x0, x1, finished):
        if not self.samples:
            return
        lo, hi = sorted((x0, x1))
        if not finished:
            for p in self.plots:
                p.drag.setRegion((lo, hi))
                p.drag.show()
            return
        for p in self.plots:
            p.drag.hide()
        if hi - lo <= 0:
            return
        if self.mode == "integrate":
            self.add_group(lo, hi)
        else:
            cp.vb._push()
            cp.vb.setXRange(lo, hi, padding=0)

    def _click(self, cp, x, y):
        if not self.samples:
            return
        if self.mode == "inspect":
            self.marker_t = x
            self._redraw()
            msgs = []
            for k, s in enumerate(self.samples):
                i = int(np.argmin(np.abs(s.t - x)))
                msgs.append(f"{s.name}: {s.y[i]:.4g}")
            self.status.showMessage(f"{x:.3f} min — " + " · ".join(msgs), 8000)
            return
        # select / integrate mode: click on a peak selects its group
        hit = None
        for gi, g in enumerate(self.groups):
            for k in cp.ids:
                p = g.peaks.get(k)
                if p is not None:
                    s = self.samples[k]
                    if p.t_start + s.shift <= x <= p.t_end + s.shift:
                        hit = gi
                        break
            if hit is not None:
                break
        ctrl = QtWidgets.QApplication.keyboardModifiers() & (QtCore.Qt.ControlModifier | QtCore.Qt.MetaModifier)
        if hit is None:
            if not ctrl:
                self.selected = set()
        elif ctrl:
            self.selected ^= {hit}
        else:
            self.selected = {hit}
        self._sync_table_selection()
        self._redraw()

    # ------------------------------------------------------------------ peaks
    def _integrate_in(self, k, lo, hi):
        """Integrate [lo, hi] (aligned minutes) in sample k. None = not detected."""
        s = self.samples[k]
        p = integ.integrate(s.run.times, s.y, lo - s.shift, hi - s.shift, "trace",
                            self.base_combo.currentData(), snap_window=3)
        if p is None:
            return None
        # detected only if there is a real maximum inside the window that stands above the noise
        inner = s.y[p.i0:p.i1 + 1]
        ia = int(np.argmax(inner))
        edge = ia in (0, inner.size - 1)
        if p.height < max(10 * s.noise, 1e-12) or (edge and p.height < 30 * s.noise) or p.area <= 0:
            return None
        return p

    def add_group(self, lo, hi):
        g = Group(rt=(lo + hi) / 2)
        for k in range(len(self.samples)):
            g.peaks[k] = self._integrate_in(k, lo, hi)
        found = g.found()
        if not found:
            self.status.showMessage("No peak found there in any sample", 4000)
            return
        g.rt = float(np.mean([g.peaks[k].rt_apex + self.samples[k].shift for k in found]))
        # replace any existing group at the same place
        self.groups = [x for x in self.groups if abs(x.rt - g.rt) > self.tol.value()]
        self.groups.append(g)
        self.groups.sort(key=lambda x: x.rt)
        self._compute_matches()
        self.selected = {self.groups.index(g)}
        self._fill_table()
        self._sync_table_selection()
        self._redraw()
        miss = [self.samples[k].name for k in range(len(self.samples)) if g.peaks.get(k) is None]
        self.status.showMessage(f"Peak at {g.rt:.3f} min: found in {len(found)} of {len(self.samples)} samples"
                                + (f" (not in {', '.join(miss)})" if miss else ""), 6000)

    def auto_peaks(self):
        if not self.samples:
            return
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            cand = []
            for k, s in enumerate(self.samples):
                for a, b in find_peaks_auto(s.run.times, s.y, s.noise, self.snr.value(), self.minrel.value() / 100):
                    cand.append((a + s.shift, b + s.shift))
            cand.sort()
            # merge windows from different samples that describe the same peak
            merged = []
            for a, b in cand:
                c = (a + b) / 2
                if merged and abs(c - (merged[-1][0] + merged[-1][1]) / 2) < self.tol.value() * 1.5:
                    continue                      # same peak already found in another sample
                merged.append((a, b))
            self.groups = []
            for a, b in merged:
                g = Group(rt=(a + b) / 2)
                for k in range(len(self.samples)):
                    g.peaks[k] = self._integrate_in(k, a, b)
                if g.found():
                    g.rt = float(np.mean([g.peaks[k].rt_apex + self.samples[k].shift for k in g.found()]))
                    if not self.groups or abs(self.groups[-1].rt - g.rt) > self.tol.value() * 0.5:
                        self.groups.append(g)
            self._compute_matches()
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        self.selected = set()
        self._fill_table()
        self._redraw()
        self.status.showMessage(f"Found {len(self.groups)} peaks. Click one to see it in every sample; "
                                "delete rows you don't need.", 6000)

    def _regroup(self):
        """After a shift change: re-integrate every group around its reference window."""
        for g in self.groups:
            ref_k = next(iter(g.found()), None)
            if ref_k is None:
                continue
            ref = g.peaks[ref_k]
            # keep each sample's own window but in the new aligned frame
            lo = ref.t_start + self.samples[ref_k].shift
            hi = ref.t_end + self.samples[ref_k].shift
            for k in range(len(self.samples)):
                g.peaks[k] = self._integrate_in(k, lo, hi)
            if g.found():
                g.rt = float(np.mean([g.peaks[k].rt_apex + self.samples[k].shift for k in g.found()]))
        self.groups = [g for g in self.groups if g.found()]
        self._compute_matches()
        self._fill_table()
        self._redraw()

    def _compute_matches(self):
        for g in self.groups:
            g.ms_match = {}
            found = [k for k in g.found() if self.samples[k].run.has_spectra]
            if len(found) < 2:
                continue
            ref = found[0]
            rs = self.samples[ref]
            ia = rs.run.scan_at(g.peaks[ref].rt_apex)
            for k in found[1:]:
                s = self.samples[k]
                g.ms_match[k] = spectrum_match(rs.run, ia, s.run, s.run.scan_at(g.peaks[k].rt_apex))

    def align_on_selected(self):
        if len(self.selected) != 1:
            self.status.showMessage("Select one peak that is in every sample (click it, or its row), then align", 5000)
            return
        g = self.groups[next(iter(self.selected))]
        found = g.found()
        if not found:
            return
        target = g.peaks[found[0]].rt_apex + self.samples[found[0]].shift
        for k in found:
            s = self.samples[k]
            s.shift = target - g.peaks[k].rt_apex
        self._fill_samples()
        self._regroup()
        self.status.showMessage(f"Aligned {len(found)} samples on the peak at {target:.3f} min", 5000)

    def auto_align(self):
        if len(self.samples) < 2:
            return
        ref = self.samples[0]
        grid = ref.run.times
        dt = float(np.median(np.diff(grid)))
        maxlag = int(round(0.5 / dt))
        a = ref.y - np.median(ref.y)
        a = a / (np.linalg.norm(a) or 1)
        for s in self.samples[1:]:
            b = np.interp(grid, s.run.times, s.y)
            b = b - np.median(b)
            b = b / (np.linalg.norm(b) or 1)
            best = (0, -1)
            for lag in range(-maxlag, maxlag + 1):
                if lag >= 0:
                    c = float(a[lag:] @ b[:b.size - lag])
                else:
                    c = float(a[:lag] @ b[-lag:])
                if c > best[1]:
                    best = (lag, c)
            s.shift = best[0] * dt + ref.shift
        self._fill_samples()
        self._regroup()
        self.status.showMessage("Aligned by cross-correlation with the first sample", 5000)

    def reset_alignment(self):
        for s in self.samples:
            s.shift = 0.0
        self._fill_samples()
        self._regroup()

    def cur(self):
        return self.groups2d if self.is2d() else self.groups

    @staticmethod
    def _item(g, k):
        return g.blobs.get(k) if isinstance(g, Group2D) else g.peaks.get(k)

    @staticmethod
    def _amount(x):
        return x.volume if isinstance(x, gx.Blob) else x.area

    def delete_selected(self):
        rows = self._selected_rows()
        if not rows:
            return
        if self.is2d():
            self.groups2d = [g for i, g in enumerate(self.groups2d) if i not in rows]
        else:
            self.groups = [g for i, g in enumerate(self.groups) if i not in rows]
        self.selected = set()
        self.ref_group = None
        self._fill_table()
        self._redraw()

    def clear_groups(self):
        if self.is2d():
            self.groups2d = []
        else:
            self.groups = []
        self.selected = set()
        self.ref_group = None
        self._fill_table()
        self._redraw()

    # ------------------------------------------------------------------ table
    def _value(self, gi, k):
        groups = self.cur()
        g = groups[gi]
        p = self._item(g, k)
        if p is None:
            return None
        mode = self.value_combo.currentIndex()
        if mode == 0:
            return self._amount(p)
        if mode == 1:
            tot = sum(self._amount(self._item(x, k)) for x in groups if self._item(x, k) is not None)
            return 100 * self._amount(p) / tot if tot else None
        if mode == 2:
            return p.height
        if self.ref_group is None or self.ref_group >= len(groups):
            return None
        r = self._item(groups[self.ref_group], k)
        return self._amount(p) / self._amount(r) if r is not None and self._amount(r) else None

    def _fmt(self, v):
        if v is None:
            return "n.d."
        mode = self.value_combo.currentIndex()
        if mode == 1:
            return f"{v:.2f}"
        if mode == 3:
            return f"{v:.4f}"
        return f"{v:.4g}" if abs(v) < 1e5 else f"{v:.0f}"

    def _has_ms(self):
        return any(s.run.has_spectra for s in self.samples)

    def _headers(self):
        h = ["#", "¹tR (min)", "²tR (s)"] if self.is2d() else ["#", "RT (min)"]
        h += ["Compound / note"] + [s.name for s in self.samples] + ["Found in"]
        if self._has_ms():
            h += ["MS match (min)"]
        return h

    def _fill_table(self):
        self._updating = True
        h = self._headers()
        t = self.ptable
        t.clear()
        t.setColumnCount(len(h))
        t.setHorizontalHeaderLabels(h)
        groups = self.cur()
        t.setRowCount(len(groups))
        n = len(self.samples)
        lead = 3 if self.is2d() else 2
        self.value_combo.setItemText(0, "Volume" if self.is2d() else "Area")
        for gi, g in enumerate(groups):
            row = [gi + 1] + ([f"{g.rt1:.3f}", f"{g.rt2:.3f}"] if self.is2d() else [f"{g.rt:.3f}"]) + [g.note]
            row += [self._fmt(self._value(gi, k)) for k in range(n)]
            row += [f"{len(g.found())}/{n}"]
            if "MS match (min)" in h:
                vals = [v for v in g.ms_match.values() if v is not None]
                row += [str(min(vals)) if vals else ""]
            set_row(t, gi, row, editable=(lead,))
            for k in range(n):
                it = t.item(gi, lead + 1 + k)
                it.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                if self._item(g, k) is None:
                    it.setForeground(QtGui.QColor("#c0392b"))
                    it.setToolTip("Not detected in this sample")
                elif k in g.ms_match and g.ms_match[k] is not None and g.ms_match[k] < 700:
                    it.setBackground(QtGui.QColor("#fff1d6"))
                    it.setToolTip(f"Spectrum match to {self.samples[g.found()[0]].name}: {g.ms_match[k]} – "
                                  "possibly a different compound at this time")
            if gi == self.ref_group:
                for c in range(t.columnCount()):
                    f = t.item(gi, c).font()
                    f.setBold(True)
                    t.item(gi, c).setFont(f)
                t.item(gi, 0).setToolTip("Reference peak for ratios")
        self._updating = False
        self._sync_table_selection()

    def _selected_rows(self):
        return sorted({i.row() for i in self.ptable.selectedIndexes()})

    def _sync_table_selection(self):
        self._updating = True
        self.ptable.clearSelection()
        mode = QtWidgets.QAbstractItemView.MultiSelection
        old = self.ptable.selectionMode()
        self.ptable.setSelectionMode(mode)
        for gi in sorted(self.selected):
            if gi < self.ptable.rowCount():
                self.ptable.selectRow(gi)
        self.ptable.setSelectionMode(old)
        if self.selected:
            self.ptable.scrollToItem(self.ptable.item(min(self.selected), 0))
        self._updating = False

    def _table_selected(self):
        if self._updating:
            return
        self.selected = set(self._selected_rows())
        if self.is2d():
            if len(self.selected) == 1 and self.panels2d:
                g = self.groups2d[next(iter(self.selected))]
                (x0, x1), (y0, y1) = self.panels2d[0].vb.viewRange()
                if not (x0 <= g.rt1 <= x1 and y0 <= g.rt2 <= y1):
                    w, hh = x1 - x0, y1 - y0
                    self.panels2d[0].vb.setRange(xRange=(g.rt1 - w / 2, g.rt1 + w / 2),
                                                 yRange=(g.rt2 - hh / 2, g.rt2 + hh / 2), padding=0)
            self._draw_2d()
            return
        if len(self.selected) == 1:
            g = self.groups[next(iter(self.selected))]
            (x0, x1), _ = self.plots[0].vb.viewRange()
            if not (x0 <= g.rt <= x1):
                w = max(0.5, (x1 - x0))
                self.plots[0].vb.setXRange(g.rt - w / 2, g.rt + w / 2, padding=0)
        self._redraw()

    def _table_edited(self, item):
        if self._updating:
            return
        lead = 3 if self.is2d() else 2
        if item.column() == lead and item.row() < len(self.cur()):
            self.cur()[item.row()].note = item.text()

    def _table_menu(self, pos):
        row = self.ptable.rowAt(pos.y())
        if row < 0:
            return
        m = QtWidgets.QMenu(self)
        m.addAction("Use as reference peak (internal standard) for ratios", lambda: self._set_ref(row))
        if not self.is2d():
            m.addAction("Align samples on this peak",
                        lambda: (setattr(self, "selected", {row}), self.align_on_selected()))
        m.addAction("Delete", lambda: (setattr(self, "selected", {row}), self.ptable.selectRow(row),
                                       self.delete_selected()))
        m.exec(self.ptable.viewport().mapToGlobal(pos))

    def _set_ref(self, row):
        self.ref_group = row
        self.value_combo.setCurrentIndex(3)
        self._fill_table()

    def table_rows(self, short=False):
        n = len(self.samples)
        label = [("Volume" if self.is2d() else "Area"), "%", "Height", "Ratio to reference"][self.value_combo.currentIndex()]
        h = ["Peak"] + (["1tR (min)", "2tR (s)"] if self.is2d() else ["RT (min)"]) + ["Compound / note"]
        h += [f"{s.name} ({label})" for s in self.samples] + ["Found in"]
        has_ms = self._has_ms()
        if has_ms and not short:
            h += ["Lowest MS match"]
        rows = []
        for gi, g in enumerate(self.cur()):
            r = [gi + 1] + ([f"{g.rt1:.3f}", f"{g.rt2:.3f}"] if self.is2d() else [f"{g.rt:.3f}"]) + [g.note]
            r += [self._fmt(self._value(gi, k)) for k in range(n)] + [f"{len(g.found())}/{n}"]
            if has_ms and not short:
                vals = [v for v in g.ms_match.values() if v is not None]
                r += [min(vals) if vals else ""]
            rows.append(r)
        return h, rows

    def export_table(self):
        if not self.cur():
            self.status.showMessage("No peaks to export – drag across peaks or use Find peaks", 4000)
            return
        h, rows = self.table_rows()
        meta = [("Trace", self.trace_label()), ("Values", self.value_combo.currentText()),
                ("n.d.", "not detected (no peak above 10 x baseline noise in that window)")]
        if not self.is2d():
            meta.insert(2, ("Matching tolerance", f"{self.tol.value():g} min"))
        else:
            meta.insert(2, ("GCxGC", "volumes summed above a straight baseline across each modulation"))
        if self.value_combo.currentIndex() == 3 and self.ref_group is not None:
            meta.append(("Reference peak", f"#{self.ref_group + 1}"))
        for k, s in enumerate(self.samples):
            extra = f"  |  modulation {s.fold.P:g} s" if (self.is2d() and s.fold is not None) else \
                f"  |  shift {s.shift:+.3f} min"
            meta.append((f"Sample {k + 1}", f"{s.name}{extra}  |  {s.path}"))
        fn = tables.export_table(self, self.settings, h, rows, meta,
                                 "sample_comparison_2D" if self.is2d() else "sample_comparison", sheet="Comparison")
        if fn:
            self.done(fn)

    def copy_table(self):
        if not self.cur():
            return
        h, rows = self.table_rows(short=True)
        tables.copy_table(h, rows, f"Comparison of {len(self.samples)} samples – {self.trace_label()}")
        self.status.showMessage("Table copied – paste into Word/PowerPoint (formatted) or Excel", 4000)

    def export_image(self):
        if not self.samples:
            return
        if self.is2d():
            self._export_image_2d()
            return
        extras = [("selected_only", "Shade only the selected peaks", True), ("numbers", "Number the peaks", True)]
        fn, opts, use_view = self.image_target("compare", "", "sample_comparison", extras)
        if not fn:
            return
        xlim = tuple(self.plots[0].vb.viewRange()[0]) if use_view else self._full_range()
        sel_only = opts.pop("selected_only", True)
        numbers = opts.pop("numbers", True)
        groups = []
        for gi, g in enumerate(self.groups):
            if sel_only and self.selected and gi not in self.selected:
                continue
            groups.append((gi + 1, g.rt, {k: p for k, p in g.peaks.items()}))
        traces = [(s.name, s.t, self.display_y(k), s.colour, s.shift) for k, s in enumerate(self.samples) if s.visible]
        vis_ids = [k for k, s in enumerate(self.samples) if s.visible]
        try:
            px.compare_figure(fn, traces, vis_ids, groups, [s.run.times for s in self.samples],
                              stacked=self.layout_mode == "stacked", xlim=xlim, numbers=numbers,
                              ylabel="Normalised (%)" if self.norm_chk.isChecked() else self.samples[0].run.signal_label,
                              normalise=self.norm_chk.isChecked(), ys_raw=[s.y for s in self.samples], **opts)
        except Exception as e:
            self.error(f"Export failed:\n{e}\n{traceback.format_exc(limit=3)}")
            return
        self.done(fn)

    def _export_image_2d(self):
        fn, opts, use_view = self.image_target("2d", "", "sample_comparison_2D")
        if not fn:
            return
        p0 = self.panels2d[0]
        (xa, xb), (ya, yb) = p0.vb.viewRange()
        try:
            px.gcxgc_grid_figure(fn, [(s.name, pnl.T if pnl.T is not None else pnl.M, s.fold.extent, pnl.levels())
                                      for s, pnl in zip(self.samples, self.panels2d)],
                                 cmap=p0.mpl_cmap(), xlim=(xa, xb) if use_view else None,
                                 ylim=(ya, yb) if use_view else None,
                                 boxes=[(gi + 1, g.box, [g.blobs.get(k) is not None for k in range(len(self.samples))],
                                         gi in self.selected) for gi, g in enumerate(self.groups2d)],
                                 bg=p0.bg_colour, **opts)
        except Exception as e:
            self.error(f"Export failed:\n{e}\n{traceback.format_exc(limit=3)}")
            return
        self.done(fn)

    def show_help(self):
        QtWidgets.QMessageBox.information(self, "Comparing samples", """
<b>Adding samples</b><br>
Add up to 5 runs (.D folders, netCDF .cdf, or FID .ch files). Choosing a folder that contains several .D folders
adds them all. GC-MS and GC-FID runs can be mixed.<br><br>
<b>Comparing peaks</b><br>
<b>2 Integrate</b>: drag across a peak in any sample – it is integrated in every sample at once.
Samples where nothing stands above the noise get <b>n.d.</b> (not detected) and a red dashed line when the peak is selected.<br>
<i>Find peaks</i> does this automatically for every peak above the thresholds in the second toolbar.<br>
<b>3 Select</b>: click a peak (Ctrl-click for several) to highlight it in all samples; selecting rows in the table does the same.<br><br>
<b>Retention-time shifts</b><br>
Select a peak present in every sample and press <i>Align on selected</i>, or type a shift for each sample.
Peaks are matched within the tolerance in the second toolbar.<br><br>
<b>GCxGC</b><br>
When every sample is GCxGC (netCDF with a modulation period), choose View ▸ <i>2D (GCxGC)</i> for side-by-side
2D plots with linked zoom. Drag a box around a blob to integrate it in every sample; click to put a cross at the
same place in every plot.<br><br>
<b>The table</b><br>
Shows area, area %, height, or the ratio to a reference peak (right-click a row ▸ Use as reference – e.g. an internal standard).
For GC-MS, <i>MS match</i> compares the apex spectra (999 = identical); low values are shaded: the peaks
may be different compounds that happen to elute together.<br>
<i>Export table</i> saves Excel/CSV; <i>Copy for report</i> pastes as a formatted table in Word.
""")
