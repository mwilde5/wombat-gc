"""
NMR window: 1H / 13C / DEPT spectra, integration, multiplet & J analysis, peak lists,
journal-style reports and data tables.
"""
from __future__ import annotations

import csv
import html
import os
import re
import traceback

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

import nmr
import plots_export as px
import tables
from common import APP_NAME, ToolWindow, hint_label, mode_buttons, set_row, table_widget
from view_widgets import make_plot

COL_SPEC = "#111111"
COL_INT = (42, 122, 42)
COL_REG = (76, 160, 76, 40)
COL_REG_SEL = (255, 165, 0, 70)
COL_REG_OFF = (150, 150, 150, 35)
COL_PEAK = (200, 40, 40)

MODES = [
    ("zoom", "1 Zoom", "Drag across the spectrum to zoom in (right-click zooms out)", "1"),
    ("integrate", "2 Integrate", "Drag across a multiplet to integrate it and measure J. Drag the green "
                                 "edges to adjust; click a region to select it", "2"),
    ("peaks", "3 Peaks", "Click a peak to add/remove its label; drag to pick all peaks in a range", "3"),
    ("reference", "4 Reference", "Click a peak (e.g. the solvent or TMS) to set its chemical shift", "4"),
]
HINTS = {
    "zoom": "Drag to zoom · Wheel: zoom · Right-click: zoom out · Home: full spectrum",
    "integrate": "Drag across a multiplet to add an integral · drag green edges to adjust · "
                 "type a number in the nH column to set the scale",
    "peaks": "Click a peak to add/remove a label · drag to label every peak in a range",
    "reference": "Click the peak whose shift you know (solvent residual, TMS…)",
}


class RegionItem(pg.LinearRegionItem):
    sigClicked = QtCore.Signal(object)

    def __init__(self, lo, hi, brush):
        super().__init__(values=(lo, hi), orientation="vertical", brush=pg.mkBrush(brush),
                         pen=pg.mkPen(COL_INT[:3] + (160,), width=1), movable=True)
        self.setZValue(-5)
        # edges movable, body not (so dragging inside makes a new selection instead)
        self.movable = False
        for ln in self.lines:
            ln.setMovable(True)
            ln.setHoverPen(pg.mkPen((255, 140, 0), width=3))

    def mouseClickEvent(self, ev):
        if ev.button() == QtCore.Qt.LeftButton:
            self.sigClicked.emit(self)
            ev.accept()
        else:
            ev.ignore()

    def mouseDragEvent(self, ev):
        ev.ignore()


class NMRWindow(ToolWindow):
    TECHNIQUE = "NMR"

    def __init__(self):
        super().__init__()
        self.sp: nmr.NMRSpectrum | None = None
        self.dept135 = None
        self.dept90 = None
        self.regions: list[nmr.Multiplet] = []
        self.peaks: list[nmr.Peak] = []
        self.ref_index = None
        self.ref_value = None
        self.sel_region = None
        self.mode = "integrate"
        self._region_items = []
        self._int_curves = []
        self._labels = []
        self._peak_items = []
        self._phase_base = None
        self._updating = False
        self._build_ui()
        self.place()
        self._set_mode("integrate")

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        mb = self.menuBar()
        fm = mb.addMenu("&File")
        fm.addAction("Open NMR file / zip…", self.open_dialog).setShortcut("Ctrl+O")
        fm.addAction("Open Bruker experiment folder…", self.open_folder_dialog).setShortcut("Ctrl+Shift+O")
        fm.addSeparator()
        fm.addAction("Add DEPT-135 (or DEPT-90) spectrum…", self.add_dept_dialog)
        fm.addAction("Remove DEPT", self.remove_dept)
        fm.addSeparator()
        fm.addAction("Export image…", self.export_image).setShortcut("Ctrl+E")
        fm.addAction("Export spectrum (CSV)…", self.export_spectrum_csv)
        fm.addAction("Export multiplet table (Excel/CSV)…", self.export_multiplets)
        fm.addAction("Export peak table (Excel/CSV)…", self.export_peaks)
        self.add_common_menus(fm)
        am = mb.addMenu("&Analyse")
        am.addAction("Auto analyse (integrals, multiplets / peaks)", self.auto_analyse).setShortcut("Ctrl+R")
        am.addAction("Pick peaks in view", self.pick_in_view)
        am.addAction("Reference to solvent", self.reference_to_solvent)
        am.addAction("Baseline correction", self.baseline_correct)
        hm = mb.addMenu("&Help")
        hm.addAction("How to analyse an NMR spectrum", self.show_help).setShortcut("F1")

        tb = self.addToolBar("Main")
        tb.setMovable(False)
        tb.addAction("Open…", self.open_dialog)
        tb.addAction("Add DEPT…", self.add_dept_dialog)
        tb.addSeparator()
        self.mode_actions = mode_buttons(tb, MODES, self._set_mode, self)
        tb.addSeparator()
        tb.addAction("Auto analyse", self.auto_analyse)
        a = tb.addAction("Full view", self.reset_zoom)
        a.setShortcut("Home")
        tb.addSeparator()
        tb.addAction("Export image…", self.export_image)

        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        left = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.pw, self.vb = make_plot(self._yrange, "δ (ppm)", "")
        self.vb.invertX(True)
        self.pw.getPlotItem().hideAxis("left")
        self.pw.setDownsampling(auto=True, mode="peak")
        self.pw.getPlotItem().setClipToView(False)      # clip-to-view misbehaves with an inverted x axis
        self.curve = self.pw.plot(pen=pg.mkPen(COL_SPEC, width=1))
        self.vb.sigDrag.connect(self._drag)
        self.vb.sigClick.connect(self._click)
        self.pw.scene().sigMouseMoved.connect(self._hover)
        self.drag_item = pg.LinearRegionItem(orientation="vertical", movable=False,
                                             brush=pg.mkBrush(100, 100, 255, 40))
        self.drag_item.hide()
        self.vb.addItem(self.drag_item, ignoreBounds=True)
        left.addWidget(self.pw)

        self.dpw, self.dvb = make_plot(self._dept_yrange, "δ (ppm)", "")
        self.dvb.invertX(True)
        self.dpw.getPlotItem().hideAxis("left")
        self.dpw.setDownsampling(auto=True, mode="peak")
        self.dpw.getPlotItem().setClipToView(False)
        self.dpw.setXLink(self.pw)
        self.dcurve = self.dpw.plot(pen=pg.mkPen("#1f3b73", width=1))
        self.dcurve90 = self.dpw.plot(pen=pg.mkPen("#c0392b", width=1))
        self.dzero = pg.InfiniteLine(0, angle=0, pen=pg.mkPen((150, 150, 150), width=1))
        self.dpw.addItem(self.dzero)
        self.dlabel = pg.TextItem("", color="#1f3b73", anchor=(0, 0))
        self.dpw.addItem(self.dlabel, ignoreBounds=True)
        self.dpw.hide()
        left.addWidget(self.dpw)
        left.setSizes([600, 250])
        split.addWidget(left)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._build_mult_tab(), "Multiplets / integrals")
        self.tabs.addTab(self._build_peak_tab(), "Peaks")
        self.tabs.addTab(self._build_report_tab(), "Report")
        self.tabs.addTab(self._build_proc_tab(), "Processing")
        self.info = QtWidgets.QTableWidget(0, 2)
        self.info.setHorizontalHeaderLabels(["Parameter", "Value"])
        self.info.verticalHeader().hide()
        self.info.horizontalHeader().setStretchLastSection(True)
        self.info.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.tabs.addTab(self.info, "Info")
        split.addWidget(self.tabs)
        split.setSizes([950, 520])
        split.setStretchFactor(0, 1)
        self.setCentralWidget(split)
        self.readout = QtWidgets.QLabel("")
        self.status.addPermanentWidget(self.readout)
        self.hint = QtWidgets.QLabel("")
        self.hint.setStyleSheet("color:#444;")
        self.status.addWidget(self.hint, 1)
        self._welcome()

    def _welcome(self):
        t = pg.TextItem(html="<div style='font-size:13pt;color:#666'>Open a Bruker experiment folder or zip "
                             "(File ▸ Open, or drag it here).<br>TopSpin ASCII exports and JCAMP-DX also work.</div>",
                        anchor=(0.5, 0.5))
        self.vb.addItem(t)
        t.setPos(0.5, 0.5)
        self.vb.setRange(xRange=(0, 1), yRange=(0, 1))
        self._welcome_item = t

    def _build_mult_tab(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        self.mtable = table_widget(["Report", "δ (ppm)", "Range (ppm)", "Mult.", "J (Hz)", "nH", "Integral", "Note"],
                                   editable_cols=(3, 4, 5, 7))
        self.mtable.itemChanged.connect(self._mtable_edited)
        self.mtable.itemSelectionChanged.connect(self._mtable_selected)
        v.addWidget(self.mtable, 1)
        g = QtWidgets.QGridLayout()
        for k, (txt, fn, tip) in enumerate([
                ("Auto detect", self.auto_regions, "Find every multiplet automatically (replaces the list)"),
                ("Re-analyse selected", self.reanalyse_selected, "Measure multiplicity and J again (undoes your edits)"),
                ("Merge selected", self.merge_selected, "Join the selected rows into one multiplet"),
                ("Delete selected", self.delete_selected, ""),
                ("Clear all", self.clear_regions, ""),
        ]):
            b = QtWidgets.QPushButton(txt)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            g.addWidget(b, k // 3, k % 3)
        v.addLayout(g)
        v.addWidget(tables.export_buttons(w, self.export_multiplets, self.copy_multiplets,
                                          copy_text_="Copy for report"))
        v.addWidget(hint_label("Type a number in the <b>nH</b> column (e.g. 3 for a CH₃) to scale every integral. "
                               "Mult. and J can be edited (e.g. <i>dd</i> and <i>8.1, 2.0</i>). Untick "
                               "<b>Report</b> for solvent/water/impurity signals."))
        return w

    def _build_peak_tab(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        f = QtWidgets.QHBoxLayout()
        f.addWidget(QtWidgets.QLabel("Threshold"))
        self.snr = QtWidgets.QDoubleSpinBox()
        self.snr.setRange(2, 1000)
        self.snr.setValue(float(self.settings.value("nmr/snr", 8)))
        self.snr.setSuffix(" × noise")
        self.snr.valueChanged.connect(lambda x: self.settings.setValue("nmr/snr", x))
        f.addWidget(self.snr)
        b = QtWidgets.QPushButton("Pick peaks in view")
        b.clicked.connect(self.pick_in_view)
        f.addWidget(b)
        f.addStretch(1)
        v.addLayout(f)
        self.ptable = table_widget(["δ (ppm)", "ν (Hz)", "Intensity", "Rel. %", "Carbon type", "Note"],
                                   editable_cols=(4, 5))
        self.ptable.itemChanged.connect(self._ptable_edited)
        self.ptable.itemSelectionChanged.connect(self._ptable_selected)
        v.addWidget(self.ptable, 1)
        h = QtWidgets.QHBoxLayout()
        for txt, fn in (("Delete selected", self.delete_peaks), ("Clear all", self.clear_peaks)):
            bb = QtWidgets.QPushButton(txt)
            bb.clicked.connect(fn)
            h.addWidget(bb)
        h.addStretch(1)
        v.addLayout(h)
        v.addWidget(tables.export_buttons(w, self.export_peaks, self.copy_peaks, copy_text_="Copy for report"))
        v.addWidget(hint_label("¹³C: carbon types come from DEPT (File ▸ Add DEPT): positive = CH or CH₃, "
                               "negative = CH₂, missing = quaternary C. Add a DEPT-90 as well to tell CH from CH₃."))
        return w

    def _build_report_tab(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        f = QtWidgets.QHBoxLayout()
        f.addWidget(QtWidgets.QLabel("δ decimals"))
        self.dec = QtWidgets.QSpinBox()
        self.dec.setRange(1, 4)
        self.dec.setValue(int(self.settings.value("nmr/dec", 2)))
        self.dec.valueChanged.connect(lambda x: (self.settings.setValue("nmr/dec", x), self.update_report()))
        f.addWidget(self.dec)
        self.types_chk = QtWidgets.QCheckBox("¹³C: include carbon types")
        self.types_chk.setChecked(self.settings.value("nmr/types", "false") == "true")
        self.types_chk.toggled.connect(lambda b: (self.settings.setValue("nmr/types", str(b).lower()),
                                                  self.update_report()))
        f.addWidget(self.types_chk)
        f.addStretch(1)
        v.addLayout(f)
        self.report = QtWidgets.QTextEdit()
        self.report.setAcceptRichText(True)
        self.report.setMinimumHeight(120)
        v.addWidget(self.report, 1)
        h = QtWidgets.QHBoxLayout()
        b = QtWidgets.QPushButton("Regenerate")
        b.clicked.connect(self.update_report)
        h.addWidget(b)
        b = QtWidgets.QPushButton("Copy (formatted)")
        b.setToolTip("Superscripts, subscripts and italic J are kept when you paste into Word")
        b.clicked.connect(self.copy_report)
        h.addWidget(b)
        h.addStretch(1)
        v.addLayout(h)
        v.addWidget(hint_label("This is the standard journal / lab-report format. Check every multiplicity and J "
                               "against the spectrum – overlapping or second-order multiplets need your judgement. "
                               "You can edit the text here before copying."))
        return w

    def _build_proc_tab(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        gb = QtWidgets.QGroupBox("Phase correction")
        g = QtWidgets.QGridLayout(gb)
        self.ph0 = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.ph0.setRange(-1800, 1800)
        self.ph1 = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.ph1.setRange(-3600, 3600)
        self.ph0_lab = QtWidgets.QLabel("0.0°")
        self.ph1_lab = QtWidgets.QLabel("0.0°")
        g.addWidget(QtWidgets.QLabel("Zero order (PH0)"), 0, 0)
        g.addWidget(self.ph0, 0, 1)
        g.addWidget(self.ph0_lab, 0, 2)
        g.addWidget(QtWidgets.QLabel("First order (PH1)"), 1, 0)
        g.addWidget(self.ph1, 1, 1)
        g.addWidget(self.ph1_lab, 1, 2)
        self.ph0.valueChanged.connect(self._phase_changed)
        self.ph1.valueChanged.connect(self._phase_changed)
        self.ph0.sliderReleased.connect(self._phase_released)
        self.ph1.sliderReleased.connect(self._phase_released)
        hb = QtWidgets.QHBoxLayout()
        b = QtWidgets.QPushButton("Auto phase")
        b.clicked.connect(self.auto_phase)
        hb.addWidget(b)
        b = QtWidgets.QPushButton("Reset sliders")
        b.clicked.connect(self._reset_phase_sliders)
        hb.addWidget(b)
        g.addLayout(hb, 2, 0, 1, 3)
        v.addWidget(gb)

        gb = QtWidgets.QGroupBox("Processing from the FID")
        f = QtWidgets.QFormLayout(gb)
        self.lb = QtWidgets.QDoubleSpinBox()
        self.lb.setRange(-5, 50)
        self.lb.setDecimals(2)
        self.lb.setSuffix(" Hz")
        self.lb.setToolTip("Exponential line broadening: larger = smoother but broader lines")
        self.zf = QtWidgets.QComboBox()
        for n in (16384, 32768, 65536, 131072, 262144):
            self.zf.addItem(f"{n // 1024}k points", n)
        self.btn_proc = QtWidgets.QPushButton("Re-process FID")
        self.btn_proc.clicked.connect(self.reprocess)
        f.addRow("Line broadening", self.lb)
        f.addRow("Size (zero filling)", self.zf)
        f.addRow(self.btn_proc)
        v.addWidget(gb)

        gb = QtWidgets.QGroupBox("Baseline and referencing")
        f = QtWidgets.QFormLayout(gb)
        b = QtWidgets.QPushButton("Baseline correction (polynomial)")
        b.clicked.connect(self.baseline_correct)
        f.addRow(b)
        self.solv = QtWidgets.QComboBox()
        self.solv.addItem("(unknown)", None)
        for k, d in nmr.SOLVENTS.items():
            if k == "MeOD":
                continue
            self.solv.addItem(d["label"], k)
        self.solv.currentIndexChanged.connect(self._solvent_changed)
        f.addRow("Solvent", self.solv)
        b = QtWidgets.QPushButton("Reference to solvent residual peak")
        b.clicked.connect(self.reference_to_solvent)
        f.addRow(b)
        f.addRow(hint_label("Or use mode 4 (Reference) and click any peak whose shift you know."))
        v.addWidget(gb)

        gb = QtWidgets.QGroupBox("Automatic analysis")
        f = QtWidgets.QFormLayout(gb)
        self.split_hz = QtWidgets.QDoubleSpinBox()
        self.split_hz.setRange(3, 60)
        self.split_hz.setValue(float(self.settings.value("nmr/split", 19)))
        self.split_hz.setSuffix(" Hz")
        self.split_hz.setToolTip("Lines further apart than this are treated as separate multiplets. "
                                 "Keep above your largest J (trans-alkene ≈ 16 Hz, geminal up to ≈ 18 Hz).")
        self.split_hz.valueChanged.connect(lambda x: self.settings.setValue("nmr/split", x))
        f.addRow("New multiplet when lines are >", self.split_hz)
        self.region_snr = QtWidgets.QDoubleSpinBox()
        self.region_snr.setRange(3, 10000)
        self.region_snr.setValue(float(self.settings.value("nmr/rsnr", 20)))
        self.region_snr.setSuffix(" × noise")
        self.region_snr.valueChanged.connect(lambda x: self.settings.setValue("nmr/rsnr", x))
        f.addRow("Ignore signals smaller than", self.region_snr)
        v.addWidget(gb)
        v.addStretch(1)
        return w

    # ------------------------------------------------------------------ loading
    def open_dialog(self):
        fn = self.ask_open("Open NMR data",
                           "NMR data (*.zip *.jdx *.dx *.txt *.csv fid acqus 1r procs);;All files (*)", "nmr_dir")
        if not fn:
            return
        if os.path.basename(fn) in ("fid", "acqus", "acqu", "ser", "pulseprogram"):
            fn = os.path.dirname(fn)
        elif os.path.basename(fn) in ("1r", "1i", "procs", "proc", "title"):
            fn = os.path.dirname(fn)
        self.open_path(fn)

    def open_folder_dialog(self):
        d = self.ask_open_dir("Choose a Bruker experiment folder (the numbered folder with fid/acqus)", "nmr_dir")
        if d:
            self.open_path(d)

    def _choose_exp(self, exps):
        items = []
        for e in exps:
            pp, nuc, title = nmr.describe_experiment(e)
            items.append(f"{os.path.basename(os.path.dirname(e))}/{os.path.basename(e)}  –  {nuc}, {pp}  {title}")
        it, ok = QtWidgets.QInputDialog.getItem(self, "Choose experiment",
                                                "This folder contains several experiments:", items, 0, False)
        return items.index(it) if ok else None

    def _read(self, path):
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            return nmr.read_any(path, choose=self._choose_exp)
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

    def open_path(self, path):
        try:
            sp = self._read(path)
        except Exception as e:
            self.error(f"Could not read {path}\n\n{e}\n\n{traceback.format_exc(limit=2)}")
            return
        if sp is None:
            return
        if sp.is_dept and self.sp is not None and self.sp.nucleus == "13C" and not self.sp.is_dept:
            if QtWidgets.QMessageBox.question(
                    self, APP_NAME, f"This is a {sp.kind} spectrum. Add it under the current ¹³C spectrum "
                                    "to assign carbon types?") == QtWidgets.QMessageBox.Yes:
                self._set_dept(sp)
                return
        self.set_spectrum(sp)

    def set_spectrum(self, sp: nmr.NMRSpectrum):
        if getattr(self, "_welcome_item", None) is not None:
            self.vb.removeItem(self._welcome_item)
            self._welcome_item = None
        self.sp = sp
        self.regions, self.peaks = [], []
        self.ref_index = self.ref_value = None
        self.sel_region = None
        self.dept135 = self.dept90 = None
        self.dpw.hide()
        self._phase_base = None
        self._reset_phase_sliders(apply=False)
        self._updating = True
        k = nmr.solvent_key(sp.solvent)
        idx = self.solv.findData(k) if k else 0
        self.solv.setCurrentIndex(max(0, idx))
        self._updating = False
        self.lb.setValue(sp.params.get("lb", 0.3))
        si = int(sp.params.get("si", 65536))
        j = self.zf.findData(si)
        self.zf.setCurrentIndex(j if j >= 0 else 2)
        self.btn_proc.setEnabled(sp.fid is not None)
        self.lb.setEnabled(sp.fid is not None)
        self.zf.setEnabled(sp.fid is not None)
        self.vb.set_full_x(float(sp.ppm.min()), float(sp.ppm.max()))
        self._draw_spectrum()
        self._fill_info()
        title = sp.title.splitlines()[0] if sp.title else ""
        self.setWindowTitle(f"{sp.name} ({sp.kind}) {('– ' + title) if title else ''} — {APP_NAME}")
        self.auto_analyse(quiet=True)
        self.reset_zoom()
        self.tabs.setCurrentIndex(0 if self._is_1h() else 1)

    def _is_1h(self):
        return self.sp is not None and self.sp.nucleus.upper().startswith("1H") and not self.sp.is_dept

    def _fill_info(self):
        sp = self.sp
        rows = [("Name", sp.name), ("Title", sp.title.replace("\n", " · ")), ("Type", sp.kind),
                ("Source", sp.source), ("File", sp.path)]
        rows += [(k, v) for k, v in sp.meta.items() if v not in ("", None)]
        rows += [("Points", f"{sp.ppm.size:,}"), ("Digital resolution", f"{sp.hz_per_point:.3f} Hz/point")
                 if sp.sf else ("Digital resolution", "")]
        rows += [("Phase (PH0, PH1)", f"{sp.params.get('phc0', 0):.1f}°, {sp.params.get('phc1', 0):.1f}°"),
                 ("Line broadening", f"{sp.params.get('lb', 0):g} Hz")]
        self.info.setRowCount(len(rows))
        for r, (k, v) in enumerate(rows):
            self.info.setItem(r, 0, QtWidgets.QTableWidgetItem(str(k)))
            self.info.setItem(r, 1, QtWidgets.QTableWidgetItem(str(v)))
        self.info.resizeColumnToContents(0)

    # ------------------------------------------------------------------ DEPT
    def add_dept_dialog(self):
        if self.sp is None:
            self.error("Open the ¹³C spectrum first, then add the DEPT.")
            return
        fn = self.ask_open("Add DEPT spectrum", "NMR data (*.zip *.jdx *.dx *.txt fid acqus 1r);;All files (*)",
                           "nmr_dir")
        if not fn:
            return
        if os.path.basename(fn) in ("fid", "acqus", "1r", "procs", "title"):
            fn = os.path.dirname(fn)
        try:
            sp = self._read(fn)
        except Exception as e:
            self.error(f"Could not read {fn}\n\n{e}")
            return
        if sp is None:
            return
        if not sp.is_dept:
            it, ok = QtWidgets.QInputDialog.getItem(self, "DEPT type", "Which DEPT experiment is this?",
                                                    ["DEPT-135", "DEPT-90"], 0, False)
            if not ok:
                return
            sp.pulprog = sp.pulprog + (" dept135" if it.endswith("135") else " dept90")
        self._set_dept(sp)

    def _set_dept(self, sp):
        if sp.dept_angle == 90:
            self.dept90 = sp
        else:
            self.dept135 = sp
        self.dpw.show()
        self._draw_dept()
        if self.peaks:
            self._assign_types()
        else:
            self.auto_analyse(quiet=True)
        self.status.showMessage(f"Added {sp.kind}: carbon types assigned", 5000)

    def remove_dept(self):
        self.dept135 = self.dept90 = None
        self.dpw.hide()
        for p in self.peaks:
            if p.ctype != "solvent":
                p.ctype = ""
        self._refresh_peaks()

    def _draw_dept(self):
        if self.dept135 is not None:
            self.dcurve.setData(self.dept135.ppm, self.dept135.real)
        else:
            self.dcurve.setData([], [])
        if self.dept90 is not None:
            self.dcurve90.setData(self.dept90.ppm, self.dept90.real)
        else:
            self.dcurve90.setData([], [])
        names = [s.kind for s in (self.dept135, self.dept90) if s is not None]
        self.dlabel.setText("  ".join(names) + "   (up: CH, CH₃ · down: CH₂)" if self.dept135 else "  ".join(names))
        self.dvb.fit_y()
        self._place_dept_label()

    def _place_dept_label(self):
        (x0, x1), (y0, y1) = self.dvb.viewRange()
        self.dlabel.setPos(max(x0, x1), y1)

    def _dept_yrange(self, x0, x1):
        vals = []
        for s in (self.dept135, self.dept90):
            if s is None:
                continue
            lo, hi = min(x0, x1), max(x0, x1)
            m = (s.ppm >= lo) & (s.ppm <= hi)
            if m.any():
                vals.append(np.abs(s.real[m]).max())
        if not vals:
            return None
        a = max(vals)
        QtCore.QTimer.singleShot(0, self._place_dept_label)
        return -a * 1.1, a * 1.1

    def _assign_types(self):
        for p in self.peaks:
            if p.ctype != "solvent":
                p.ctype = ""
        nmr.label_solvent_carbons(self.sp, self.peaks)
        nmr.assign_dept(self.peaks, self.dept135, self.dept90)
        self._refresh_peaks()

    # ------------------------------------------------------------------ drawing
    def _yrange(self, x0, x1):
        if self.sp is None:
            return None
        lo, hi = min(x0, x1), max(x0, x1)
        m = (self.sp.ppm >= lo) & (self.sp.ppm <= hi)
        if not m.any():
            return None
        y = self.sp.real[m]
        ymax = float(y.max())
        ymin = float(y.min())
        if ymax <= 0:
            ymax = abs(ymin) or 1.0
        extra = 0.12 if (self._labels or self.peaks) else 0.0
        return min(ymin, -0.07 * ymax), ymax * (1.0 + extra)

    def _draw_spectrum(self):
        self.curve.setData(self.sp.ppm, self.sp.real)
        self._draw_overlays()

    def reset_zoom(self):
        if self.sp is None:
            return
        if self._is_1h():
            # most 1H spectra are recorded 16 → -4 ppm; show the usual -0.5 … 12 if nothing is outside
            hi = 12.5 if not np.any(self.sp.real[self.sp.ppm > 12.5] > 30 * nmr.noise_level(self.sp.real)) \
                else self.sp.ppm.max()
            self.vb.full_x = (max(float(self.sp.ppm.min()), -0.8), min(float(self.sp.ppm.max()), hi))
        elif self.sp.nucleus.upper().startswith("13C"):
            self.vb.full_x = (max(float(self.sp.ppm.min()), -5), min(float(self.sp.ppm.max()), 225))
        self.vb.reset_view()

    def _clear_items(self):
        for it in self._region_items + self._int_curves + self._labels + self._peak_items:
            self.vb.removeItem(it)
        self._region_items, self._int_curves, self._labels, self._peak_items = [], [], [], []

    def _draw_overlays(self):
        if self.sp is None:
            return
        self._clear_items()
        sp = self.sp
        ymax = float(sp.real.max()) or 1.0
        # integral curves scaled to a common factor
        ints = []
        for m in self.regions:
            r = (sp.ppm >= m.lo) & (sp.ppm <= m.hi)
            if r.sum() > 1:
                xi, yi = sp.ppm[r], sp.real[r]          # high → low ppm: rises left to right
                ints.append((xi, np.concatenate([[0], np.cumsum((yi[1:] + yi[:-1]) / 2 * -np.diff(xi))])))
            else:
                ints.append((np.array([]), np.array([])))
        cmax = max((c[-1] for _, c in ints if c.size), default=0) or 1
        for k, m in enumerate(self.regions):
            brush = COL_REG_SEL if k == self.sel_region else (COL_REG if not m.solvent else COL_REG_OFF)
            it = RegionItem(m.lo, m.hi, brush)
            it._k = k
            it.sigRegionChangeFinished.connect(self._region_moved)
            it.sigClicked.connect(lambda item: self._select_region(item._k))
            self.vb.addItem(it, ignoreBounds=True)
            self._region_items.append(it)
            xi, c = ints[k]
            if c.size:
                cur = pg.PlotDataItem(xi, ymax * 0.05 + c / cmax * ymax * 0.35,
                                      pen=pg.mkPen(COL_INT if not m.solvent else (150, 150, 150), width=1.2))
                self.vb.addItem(cur, ignoreBounds=True)
                self._int_curves.append(cur)
            lab = f"{m.nh:.2f}" if m.nh else ""
            if lab:
                t = pg.TextItem(lab, color=COL_INT if not m.solvent else (140, 140, 140), anchor=(0.5, 0))
                t.setPos((m.lo + m.hi) / 2, -0.012 * ymax)
                self.vb.addItem(t, ignoreBounds=True)
                self._labels.append(t)
            if self._is_1h() and m.mult and not m.solvent:
                txt = m.mult if not m.js else f"{m.mult} {', '.join(f'{j:.1f}' for j in m.js)}"
                r = (sp.ppm >= m.lo) & (sp.ppm <= m.hi)
                ytop = float(sp.real[r].max()) if r.any() else ymax
                t = pg.TextItem(txt, color=(60, 60, 140), anchor=(0.5, 1))
                t.setPos(m.centre if m.mult != "m" else (m.lo + m.hi) / 2, ytop + 0.01 * ymax)
                self.vb.addItem(t, ignoreBounds=True)
                self._labels.append(t)
        # peak labels
        if self.peaks:
            xs = [p.ppm for p in self.peaks]
            ys = [p.height for p in self.peaks]
            sc = pg.ScatterPlotItem(xs, ys, symbol="t", size=6, pen=None, brush=pg.mkBrush(COL_PEAK))
            self.vb.addItem(sc, ignoreBounds=True)
            self._peak_items.append(sc)
            dec = 2 if self._is_1h() else 1
            for p in self.peaks:
                lab = f"{p.ppm:.{dec + (1 if self._is_1h() else 0)}f}"
                if p.ctype and p.ctype != "solvent":
                    lab += f" {p.ctype.replace('CH or CH3', 'CH/CH₃').replace('CH3', 'CH₃').replace('CH2', 'CH₂')}"
                t = pg.TextItem(lab, color=(150, 30, 30) if p.ctype != "solvent" else (140, 140, 140),
                                anchor=(0, 0.5), angle=90)
                t.setPos(p.ppm, p.height + 0.01 * ymax * np.sign(p.height or 1))
                self.vb.addItem(t, ignoreBounds=True)
                self._peak_items.append(t)
        self.vb.fit_y() if self.vb.auto_y else None

    # ------------------------------------------------------------------ modes & mouse
    def _set_mode(self, k):
        self.mode = k
        self.mode_actions[k].setChecked(True)
        self.hint.setText(HINTS[k])

    def _hover(self, pos):
        if self.sp is None or not self.pw.sceneBoundingRect().contains(pos):
            return
        p = self.vb.mapSceneToView(pos)
        sf = self.sp.sf
        self.readout.setText(f"δ {p.x():.4f} ppm" + (f"   ({p.x() * sf:.1f} Hz)" if sf else ""))

    def _drag(self, x0, x1, finished):
        if self.sp is None:
            return
        lo, hi = sorted((x0, x1))
        if not finished:
            self.drag_item.setRegion((lo, hi))
            self.drag_item.show()
            return
        self.drag_item.hide()
        if hi - lo <= 0:
            return
        if self.mode == "zoom":
            self.vb._push()
            self.vb.setXRange(lo, hi, padding=0)
        elif self.mode == "integrate":
            self.add_region(lo, hi)
        elif self.mode == "peaks":
            new = nmr.pick_peaks(self.sp, lo, hi, min_snr=self.snr.value(), negative=self.sp.is_dept)
            known = {p.index for p in self.peaks}
            self.peaks += [p for p in new if p.index not in known]
            self.peaks.sort(key=lambda p: -p.ppm)
            self._after_peaks_changed()
        elif self.mode == "reference":
            self._click((lo + hi) / 2, 0)

    def _nearest_max(self, x, window_ppm=None):
        sp = self.sp
        (vx0, vx1), _ = self.vb.viewRange()
        w = window_ppm or abs(vx1 - vx0) * 0.01
        m = np.where((sp.ppm >= x - w) & (sp.ppm <= x + w))[0]
        if m.size == 0:
            return None
        y = sp.real[m]
        i = m[int(np.argmax(np.abs(y) if sp.is_dept else y))]
        if 0 < i < sp.ppm.size - 1:
            return i
        return None

    def _click(self, x, y):
        if self.sp is None:
            return
        if self.mode == "integrate":
            for k, m in enumerate(self.regions):
                if m.lo <= x <= m.hi:
                    self._select_region(k)
                    return
        elif self.mode == "peaks":
            (vx0, vx1), _ = self.vb.viewRange()
            tol = abs(vx1 - vx0) * 0.006
            near = [p for p in self.peaks if abs(p.ppm - x) < tol]
            if near:
                self.peaks.remove(min(near, key=lambda p: abs(p.ppm - x)))
            else:
                i = self._nearest_max(x)
                if i is None:
                    return
                self.peaks.append(nmr.Peak(nmr._refine(self.sp.ppm, self.sp.real, i), float(self.sp.real[i]), i))
                self.peaks.sort(key=lambda p: -p.ppm)
            self._after_peaks_changed()
        elif self.mode == "reference":
            i = self._nearest_max(x)
            if i is None:
                return
            cur = nmr._refine(self.sp.ppm, self.sp.real, i)
            k = nmr.solvent_key(self.solv.currentData())
            guess = cur
            if k:
                cands = nmr.SOLVENTS[k]["1H" if self._is_1h() else "13C"]
                if cands:
                    guess = min(cands, key=lambda s: abs(s - cur))
                    if abs(guess - cur) > 1.0:
                        guess = cur
            val, ok = QtWidgets.QInputDialog.getDouble(
                self, "Reference", f"The peak at {cur:.4f} ppm should be at (ppm):", guess, -50, 300, 4)
            if ok:
                self.shift_axis(val - cur)

    def shift_axis(self, d):
        if self.sp is None or d == 0:
            return
        for s in (self.sp, self.dept135, self.dept90):
            if s is not None and (s is self.sp or self.sp.nucleus == s.nucleus):
                s.ppm = s.ppm + d
                if "offset" in s.params:
                    s.params["offset"] += d
        for m in self.regions:
            m.lo += d
            m.hi += d
            m.centre += d
            m.lines = [v + d for v in m.lines]
            if m.ab:
                m.ab = (m.ab[0] + d, m.ab[1] + d, m.ab[2])
        for p in self.peaks:
            p.ppm += d
        (x0, x1), _ = self.vb.viewRange()
        self.curve.setData(self.sp.ppm, self.sp.real)
        self._draw_dept()
        self.vb.set_full_x(float(self.sp.ppm.min()), float(self.sp.ppm.max()))
        self.vb.setXRange(x0 + d, x1 + d, padding=0)
        self._refresh_regions()
        self._refresh_peaks()
        self.status.showMessage(f"Shifted the ppm scale by {d:+.4f} ppm", 5000)

    def reference_to_solvent(self):
        if self.sp is None:
            return
        k = self.solv.currentData()
        if not k:
            self.error("Choose the solvent first (Processing tab).")
            return
        shifts = nmr.SOLVENTS[k]["1H" if self._is_1h() else "13C"]
        if not shifts:
            self.error("No residual peak listed for this solvent / nucleus.")
            return
        target = shifts[0]
        m = np.where(np.abs(self.sp.ppm - target) < (0.15 if self._is_1h() else 1.5))[0]
        if not m.size:
            return
        i = m[int(np.argmax(self.sp.real[m]))]
        if self.sp.nucleus.startswith("13C") and k == "CDCl3":
            # the CDCl3 triplet: use its centre line
            pk = nmr.pick_peaks(self.sp, target - 1, target + 1, min_snr=5)
            if len(pk) >= 3:
                pk.sort(key=lambda p: -p.height)
                c = sorted(pk[:3], key=lambda p: p.ppm)[1]
                self.shift_axis(target - c.ppm)
                return
        self.shift_axis(target - nmr._refine(self.sp.ppm, self.sp.real, i))

    def _solvent_changed(self):
        if self._updating or self.sp is None:
            return
        self.sp.solvent = self.solv.currentData()
        self.update_report()

    # ------------------------------------------------------------------ regions / multiplets
    def auto_analyse(self, quiet=False):
        if self.sp is None:
            return
        if self.sp.is_dept or not self._is_1h():
            self.auto_peaks()
            if self._is_1h():
                self.auto_regions()
        else:
            self.auto_regions()
            self.peaks = []
            self._refresh_peaks()
        if not quiet:
            self.status.showMessage("Analysed – check the multiplets and integrals", 5000)

    def auto_regions(self):
        if self.sp is None:
            return
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            spans = nmr.auto_regions(self.sp, min_snr=self.region_snr.value(), split_hz=self.split_hz.value())
            self.regions = [nmr.Multiplet(lo, hi) for lo, hi in spans]
            for m in self.regions:
                nmr.analyse_region(self.sp, m)
            nmr.mark_solvent(self.sp, self.regions)
            self.ref_index = self.ref_value = None
            self._auto_reference()
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        self.sel_region = None
        self._refresh_regions()

    def _auto_reference(self):
        """Choose the integral scale that makes the signals closest to whole numbers of protons
        (preferring the smallest total), then use the cleanest signal as the reference."""
        nmr.normalise(self.regions)
        use = [k for k, m in enumerate(self.regions) if not m.solvent and m.integral > 0]
        if not use:
            return
        f, k0, n0 = nmr.best_scale([self.regions[k].integral for k in use])
        self.ref_index, self.ref_value = use[k0], float(n0)
        nmr.normalise(self.regions, self.ref_index, self.ref_value)

    def add_region(self, lo, hi):
        # remove regions mostly inside the new one; trim overlapping ones
        keep = []
        for m in self.regions:
            c = (m.lo + m.hi) / 2
            if lo <= c <= hi:
                continue
            if m.hi > lo and m.lo < hi:
                if c > hi:
                    m.lo = hi
                else:
                    m.hi = lo
                m.manual = False
                nmr.analyse_region(self.sp, m)
            keep.append(m)
        new = nmr.Multiplet(lo, hi)
        nmr.analyse_region(self.sp, new)
        keep.append(new)
        keep.sort(key=lambda m: -m.hi)
        self.regions = keep
        nmr.mark_solvent(self.sp, [new])
        self._renormalise()
        self.sel_region = self.regions.index(new)
        self._refresh_regions()

    def _renormalise(self):
        if self.ref_index is not None and (self.ref_index >= len(self.regions)):
            self.ref_index = None
        if self.ref_index is None:
            self._auto_reference()
        else:
            nmr.normalise(self.regions, self.ref_index, self.ref_value)

    def _region_moved(self, item):
        k = item._k
        if k >= len(self.regions):
            return
        lo, hi = sorted(item.getRegion())
        m = self.regions[k]
        m.lo, m.hi = float(lo), float(hi)
        m.manual = False
        nmr.analyse_region(self.sp, m)
        self._renormalise()
        self.sel_region = k
        QtCore.QTimer.singleShot(0, self._refresh_regions)

    def _select_region(self, k):
        self.sel_region = k
        self._updating = True
        self.mtable.selectRow(k)
        self._updating = False
        for j, it in enumerate(self._region_items):
            m = self.regions[j]
            it.setBrush(pg.mkBrush(COL_REG_SEL if j == k else (COL_REG if not m.solvent else COL_REG_OFF)))
            it.update()
        self.tabs.setCurrentIndex(0)

    def reanalyse_selected(self):
        for k in self._selected_rows(self.mtable):
            m = self.regions[k]
            m.manual = False
            nmr.analyse_region(self.sp, m)
        self._renormalise()
        self._refresh_regions()

    def merge_selected(self):
        rows = self._selected_rows(self.mtable)
        if len(rows) < 2:
            self.status.showMessage("Select two or more rows to merge (Ctrl/Shift-click)", 4000)
            return
        lo = min(self.regions[k].lo for k in rows)
        hi = max(self.regions[k].hi for k in rows)
        self.regions = [m for k, m in enumerate(self.regions) if k not in rows]
        self.ref_index = None
        self.add_region(lo, hi)

    def delete_selected(self):
        rows = self._selected_rows(self.mtable)
        if not rows:
            return
        if self.ref_index in rows:
            self.ref_index = None
        elif self.ref_index is not None:
            self.ref_index -= sum(1 for r in rows if r < self.ref_index)
        self.regions = [m for k, m in enumerate(self.regions) if k not in rows]
        self.sel_region = None
        self._renormalise()
        self._refresh_regions()

    def clear_regions(self):
        self.regions = []
        self.ref_index = None
        self.sel_region = None
        self._refresh_regions()

    @staticmethod
    def _selected_rows(table):
        return sorted({i.row() for i in table.selectedIndexes()})

    def _refresh_regions(self):
        self._updating = True
        t = self.mtable
        t.setRowCount(len(self.regions))
        for r, m in enumerate(self.regions):
            js = ", ".join(f"{j:.1f}" for j in m.js)
            if m.mult == "AB" and m.ab:
                js = f"{m.ab[2]:.1f}"
            shift = f"{m.centre:.3f}" if m.mult != "m" else f"{max(m.lines or [m.hi]):.2f}–{min(m.lines or [m.lo]):.2f}"
            if m.mult == "AB" and m.ab:
                shift = f"{m.ab[0]:.3f}, {m.ab[1]:.3f}"
            set_row(t, r, ["", shift, f"{m.hi:.3f}–{m.lo:.3f}", m.mult, js, f"{m.nh:.2f}",
                           f"{m.integral:.4g}", m.note], editable=(3, 4, 5, 7))
            chk = QtWidgets.QTableWidgetItem()
            chk.setFlags(QtCore.Qt.ItemIsUserCheckable | QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable)
            chk.setCheckState(QtCore.Qt.Unchecked if m.solvent else QtCore.Qt.Checked)
            t.setItem(r, 0, chk)
            if r == self.ref_index:
                f = t.item(r, 5).font()
                f.setBold(True)
                t.item(r, 5).setFont(f)
                t.item(r, 5).setToolTip("Integral reference")
            if m.manual:
                for c in (3, 4):
                    t.item(r, c).setForeground(QtGui.QColor("#1f3b73"))
                    t.item(r, c).setToolTip("Edited by you")
        if self.sel_region is not None and self.sel_region < len(self.regions):
            t.selectRow(self.sel_region)
        self._updating = False
        self._draw_overlays()
        self.update_report()

    def _mtable_selected(self):
        if self._updating:
            return
        rows = self._selected_rows(self.mtable)
        if len(rows) == 1:
            self.sel_region = rows[0]
            for j, it in enumerate(self._region_items):
                m = self.regions[j]
                it.setBrush(pg.mkBrush(COL_REG_SEL if j == rows[0] else (COL_REG if not m.solvent else COL_REG_OFF)))
                it.update()
            m = self.regions[rows[0]]
            (x0, x1), _ = self.vb.viewRange()
            if not (min(x0, x1) <= m.lo and m.hi <= max(x0, x1)):
                w = max(0.3, (m.hi - m.lo) * 3)
                c = (m.lo + m.hi) / 2
                self.vb.setXRange(c - w / 2, c + w / 2, padding=0)

    def _mtable_edited(self, item):
        if self._updating:
            return
        r, c = item.row(), item.column()
        if r >= len(self.regions):
            return
        m = self.regions[r]
        txt = item.text().strip()
        if c == 0:
            m.solvent = item.checkState() != QtCore.Qt.Checked
            if m.solvent and r == self.ref_index:
                self.ref_index = None
            self._renormalise()
        elif c == 3:
            m.mult = txt or "m"
            m.manual = True
            if m.mult not in ("AB",):
                m.ab = None
            if m.mult in ("s", "m", "br s", "brs"):
                m.js = []
            if m.mult != "m" and m.lines and m.centre == 0:
                m.centre = float(np.mean(m.lines))
        elif c == 4:
            try:
                m.js = [float(v) for v in re.split(r"[,;/\s]+", txt) if v]
                m.js.sort(reverse=True)
                m.manual = True
            except ValueError:
                self.status.showMessage("J values must be numbers, e.g. 8.1, 2.0", 4000)
        elif c == 5:
            try:
                val = float(txt)
            except ValueError:
                self.status.showMessage("nH must be a number", 3000)
                val = None
            if val and m.integral > 0:
                self.ref_index, self.ref_value = r, val
                nmr.normalise(self.regions, r, val)
        elif c == 7:
            m.note = txt
        QtCore.QTimer.singleShot(0, self._refresh_regions)

    # ------------------------------------------------------------------ peaks
    def auto_peaks(self):
        if self.sp is None:
            return
        snr = self.snr.value()
        self.peaks = nmr.pick_peaks(self.sp, min_snr=snr, negative=self.sp.is_dept)
        if self.sp.nucleus.startswith("13C") and not self.sp.is_dept:
            # 13C: drop tiny shoulders from noise spikes (keep >2 % of the median peak)
            if self.peaks:
                med = np.median([p.height for p in self.peaks])
                self.peaks = [p for p in self.peaks if p.height > 0.02 * med]
        if self.sp.is_dept:
            for p in self.peaks:
                p.ctype = "CH2" if p.height < 0 else ("CH" if self.sp.dept_angle == 90 else "CH or CH3")
        self._after_peaks_changed()

    def pick_in_view(self):
        if self.sp is None:
            return
        (x0, x1), _ = self.vb.viewRange()
        lo, hi = sorted((x0, x1))
        new = nmr.pick_peaks(self.sp, lo, hi, min_snr=self.snr.value(), negative=self.sp.is_dept)
        self.peaks = [p for p in self.peaks if not (lo <= p.ppm <= hi)] + new
        self.peaks.sort(key=lambda p: -p.ppm)
        self._after_peaks_changed()
        self.tabs.setCurrentIndex(1)

    def _after_peaks_changed(self):
        if self.sp.nucleus.startswith("13C") and not self.sp.is_dept:
            self._assign_types()
        else:
            self._refresh_peaks()

    def delete_peaks(self):
        rows = self._selected_rows(self.ptable)
        self.peaks = [p for k, p in enumerate(self.peaks) if k not in rows]
        self._refresh_peaks()

    def clear_peaks(self):
        self.peaks = []
        self._refresh_peaks()

    def _refresh_peaks(self):
        self._updating = True
        t = self.ptable
        t.setRowCount(len(self.peaks))
        top = max((abs(p.height) for p in self.peaks if p.ctype != "solvent"), default=1) or 1
        sf = self.sp.sf if self.sp else 0
        dec = 3 if self._is_1h() else 2
        for r, p in enumerate(self.peaks):
            set_row(t, r, [f"{p.ppm:.{dec}f}", f"{p.ppm * sf:.1f}" if sf else "", f"{p.height:.4g}",
                           f"{100 * p.height / top:.1f}", p.ctype, p.note], editable=(4, 5))
        self._updating = False
        self._draw_overlays()
        self.update_report()

    def _ptable_selected(self):
        rows = self._selected_rows(self.ptable)
        if len(rows) == 1 and self.sp is not None:
            p = self.peaks[rows[0]]
            (x0, x1), _ = self.vb.viewRange()
            if not (min(x0, x1) <= p.ppm <= max(x0, x1)):
                w = abs(x1 - x0)
                self.vb.setXRange(p.ppm - w / 2, p.ppm + w / 2, padding=0)

    def _ptable_edited(self, item):
        if self._updating:
            return
        r, c = item.row(), item.column()
        if r >= len(self.peaks):
            return
        if c == 4:
            self.peaks[r].ctype = item.text().strip()
        elif c == 5:
            self.peaks[r].note = item.text().strip()
        QtCore.QTimer.singleShot(0, self._refresh_peaks)

    # ------------------------------------------------------------------ report
    def report_text(self):
        if self.sp is None:
            return ""
        dec = self.dec.value()
        if self._is_1h():
            if not self.regions:
                return ""
            return nmr.report_1h(self.sp, self.regions, dec)
        if self.peaks:
            if self.sp.is_dept:
                return f"{self.sp.kind} ({self.sp.mhz_label()}) δ " + ", ".join(
                    f"{p.ppm:.1f} ({'CH₂' if p.height < 0 else 'CH/CH₃'})" for p in self.peaks) + "."
            return nmr.report_13c(self.sp, self.peaks, decimals=1 if dec <= 2 else dec - 1,
                                  with_types=self.types_chk.isChecked())
        return ""

    @staticmethod
    def report_html(text):
        h = html.escape(text)
        h = re.sub(r"¹H", "<sup>1</sup>H", h)
        h = re.sub(r"¹³C", "<sup>13</sup>C", h)
        h = re.sub(r"\bJ = ", "<i>J</i> = ", h)
        for a, b in (("₃", "<sub>3</sub>"), ("₂", "<sub>2</sub>"), ("₆", "<sub>6</sub>"), ("₈", "<sub>8</sub>")):
            h = h.replace(a, b)
        h = h.replace("δ", "<i>δ</i>")
        return f"<span style='font-family:Calibri,Arial;font-size:11pt'>{h}</span>"

    def update_report(self):
        txt = self.report_text()
        self.report.setHtml(self.report_html(txt))

    def copy_report(self):
        plain = self.report.toPlainText()
        tables.copy_text(plain, self.report_html(plain))
        self.status.showMessage("Report copied – paste into Word to keep the formatting", 4000)

    # ------------------------------------------------------------------ processing
    def _phase_changed(self):
        if self.sp is None or self._updating:
            return
        if self.sp.imag is None:
            self.status.showMessage("Phasing needs the imaginary data (1i) or the FID", 4000)
            return
        if self._phase_base is None:
            self._phase_base = (self.sp.real.copy(), self.sp.imag.copy(), dict(self.sp.params))
        d0, d1 = self.ph0.value() / 10, self.ph1.value() / 10
        self.ph0_lab.setText(f"{d0:+.1f}°")
        self.ph1_lab.setText(f"{d1:+.1f}°")
        r, i, params = self._phase_base
        z = (r + 1j * i) * np.exp(1j * np.deg2rad(d0 + d1 * np.arange(r.size) / r.size))
        self.sp.real, self.sp.imag = z.real, z.imag
        self.sp.params["phc0"] = params.get("phc0", 0) + d0
        self.sp.params["phc1"] = params.get("phc1", 0) + d1
        self.curve.setData(self.sp.ppm, self.sp.real)

    def _phase_released(self):
        if self.sp is None:
            return
        for m in self.regions:
            nmr.analyse_region(self.sp, m)
        self._renormalise()
        self._refresh_regions()
        self._fill_info()

    def _reset_phase_sliders(self, apply=True):
        self._updating = True
        self.ph0.setValue(0)
        self.ph1.setValue(0)
        self.ph0_lab.setText("0.0°")
        self.ph1_lab.setText("0.0°")
        self._updating = False
        self._phase_base = None

    def auto_phase(self):
        if self.sp is None:
            return
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            if self.sp.fid is not None:
                nmr.process(self.sp, lb=self.lb.value(), si=self.zf.currentData(), autophase=True)
            elif self.sp.imag is not None:
                z = self.sp.real + 1j * self.sp.imag
                p0, p1 = nmr._autophase(z[::-1])
                # _autophase works on FT order; apply the same correction to our (reversed) data
                y = (z[::-1] * np.exp(-1j * np.deg2rad(p0 + p1 * np.arange(z.size) / z.size)))[::-1]
                self.sp.real, self.sp.imag = y.real, y.imag
            else:
                self.status.showMessage("No imaginary data – cannot phase", 4000)
                return
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        self._reset_phase_sliders()
        self._after_processing()

    def reprocess(self):
        if self.sp is None or self.sp.fid is None:
            return
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            nmr.process(self.sp, lb=self.lb.value(), si=self.zf.currentData())
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        self.sp.source = "Bruker raw FID (processed here)"
        self._reset_phase_sliders()
        self._after_processing()

    def baseline_correct(self):
        if self.sp is None:
            return
        self.sp.real = self.sp.real - nmr.estimate_baseline(self.sp.real)
        self._phase_base = None
        self._after_processing()

    def _after_processing(self):
        self.curve.setData(self.sp.ppm, self.sp.real)
        for m in self.regions:
            nmr.analyse_region(self.sp, m)
        self._renormalise()
        self._refresh_regions()
        if self.peaks:
            self.peaks = [p for p in self.peaks]
            self._refresh_peaks()
        self._fill_info()
        self.vb.fit_y()

    # ------------------------------------------------------------------ tables & exports
    def _mult_rows(self):
        h = ["δ (ppm)", "Multiplicity", "J (Hz)", "Integral (nH)", "Assignment / note"]
        rows = []
        dec = self.dec.value()
        for m in sorted(self.regions, key=lambda m: -m.centre):
            if m.solvent:
                continue
            if m.mult == "AB" and m.ab:
                for v in m.ab[:2]:
                    rows.append([f"{v:.{dec}f}", "d (AB)", f"{m.ab[2]:.1f}", f"{m.nh / 2:.0f}", m.note])
                continue
            shift = f"{m.centre:.{dec}f}" if m.mult != "m" else f"{m.hi_shift(dec)}–{m.lo_shift(dec)}"
            rows.append([shift, m.mult, ", ".join(f"{j:.1f}" for j in m.js), f"{m.nh:.0f}", m.note])
        return h, rows

    def _mult_rows_full(self):
        h = ["δ (ppm)", "Range from (ppm)", "Range to (ppm)", "Multiplicity", "J (Hz)", "nH (rounded)",
             "nH", "Raw integral", "In report", "Note"]
        rows = []
        for m in self.regions:
            js = ", ".join(f"{j:.1f}" for j in m.js)
            shift = f"{m.centre:.4f}" if m.mult != "m" else ""
            if m.mult == "AB" and m.ab:
                shift = f"{m.ab[0]:.4f}, {m.ab[1]:.4f}"
            rows.append([shift, f"{m.hi:.4f}", f"{m.lo:.4f}", m.mult, js, f"{m.nh:.0f}", f"{m.nh:.3f}",
                         f"{m.integral:.6g}", "no" if m.solvent else "yes", m.note])
        return h, rows

    def _meta(self):
        sp = self.sp
        return [("Spectrum", sp.name), ("Title", sp.title.replace("\n", " · ")), ("Experiment", sp.kind),
                ("Frequency", sp.mhz_label()), ("Solvent", sp.solvent or ""), ("File", sp.path)]

    def export_multiplets(self):
        if self.sp is None or not self.regions:
            self.status.showMessage("No integrals yet", 3000)
            return
        h, rows = self._mult_rows_full()
        meta = self._meta()
        if self.ref_index is not None:
            meta.append(("Integral reference", f"row {self.ref_index + 1} = {self.ref_value:g} H"))
        meta.append(("Report", self.report.toPlainText()))
        fn = tables.export_table(self, self.settings, h, rows, meta, f"{self.safe(self.sp.name)}_{self.sp.kind}_multiplets",
                                 sheet="Multiplets")
        if fn:
            self.done(fn)

    def copy_multiplets(self):
        if self.sp is None or not self.regions:
            return
        h, rows = self._mult_rows()
        tables.copy_table(h, rows, f"{nmr.nucleus_label(self.sp.nucleus)} NMR ({self.sp.mhz_label()}, "
                                   f"{self.sp.solvent or ''}) – {self.sp.name}")
        self.status.showMessage("Table copied – paste into Word/PowerPoint (formatted) or Excel", 4000)

    def _peak_rows(self, full=True):
        is13 = not self._is_1h()
        sf = self.sp.sf
        if full:
            h = ["δ (ppm)", "ν (Hz)", "Intensity", "Relative %"] + (["Carbon type"] if is13 else []) + ["Note"]
        else:
            h = ["δ (ppm)"] + (["Carbon type"] if is13 else []) + ["Assignment / note"]
        top = max((abs(p.height) for p in self.peaks if p.ctype != "solvent"), default=1) or 1
        rows = []
        for p in self.peaks:
            if not full and p.ctype == "solvent":
                continue
            if full:
                rows.append([f"{p.ppm:.4f}", f"{p.ppm * sf:.2f}" if sf else "", f"{p.height:.6g}",
                             f"{100 * p.height / top:.1f}"] + ([p.ctype] if is13 else []) + [p.note])
            else:
                rows.append([f"{p.ppm:.{1 if is13 else 3}f}"] + ([p.ctype.replace("CH or CH3", "CH or CH₃")
                                                                   .replace("CH3", "CH₃").replace("CH2", "CH₂")]
                                                                  if is13 else []) + [p.note])
        return h, rows

    def export_peaks(self):
        if self.sp is None or not self.peaks:
            self.status.showMessage("No peaks picked yet", 3000)
            return
        h, rows = self._peak_rows(True)
        fn = tables.export_table(self, self.settings, h, rows, self._meta(),
                                 f"{self.safe(self.sp.name)}_{self.sp.kind}_peaks", sheet="Peaks")
        if fn:
            self.done(fn)

    def copy_peaks(self):
        if self.sp is None or not self.peaks:
            return
        h, rows = self._peak_rows(False)
        tables.copy_table(h, rows, f"{nmr.nucleus_label(self.sp.nucleus)} NMR peaks – {self.sp.name}")
        self.status.showMessage("Table copied – paste into Word/PowerPoint (formatted) or Excel", 4000)

    def export_spectrum_csv(self):
        if self.sp is None:
            return
        fn = self.save_name("Export spectrum", f"{self.safe(self.sp.name)}_{self.sp.kind}.csv", "CSV (*.csv)")
        if not fn:
            return
        with open(fn, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ppm", "intensity"] + (["Hz"] if self.sp.sf else []))
            for x, y in zip(self.sp.ppm, self.sp.real):
                w.writerow([f"{x:.6f}", f"{y:.6g}"] + ([f"{x * self.sp.sf:.3f}"] if self.sp.sf else []))
        self.done(fn)

    def export_image(self):
        if self.sp is None:
            return
        is1h = self._is_1h()
        extras = [("integrals", "Show integrals under the axis", is1h),
                  ("curves", "Integral curves", is1h),
                  ("peaks", "Label picked peaks (ppm)", not is1h),
                  ("mults", "Label multiplets (s, d, J …)", False)]
        if self.dept135 is not None or self.dept90 is not None:
            extras.append(("dept", "DEPT panel underneath", True))
        k = nmr.solvent_key(self.sp.solvent)
        solv = nmr.SOLVENTS[k]["label"] if k else (self.sp.solvent or "")
        title = f"{nmr.nucleus_label(self.sp.nucleus) if not self.sp.is_dept else self.sp.kind} NMR " \
                f"({self.sp.mhz_label()}{', ' + solv if solv else ''}) – " \
                f"{(self.sp.title.splitlines()[0] if self.sp.title else self.sp.name)}"
        fn, opts, use_view = self.image_target("nmr1h" if is1h else "nmrx", title, f"{self.safe(self.sp.name)}_{self.sp.kind}", extras)
        if not fn:
            return
        xlim = tuple(self.vb.viewRange()[0]) if use_view else self.vb.full_x
        regions = [(m.lo, m.hi, f"{m.nh:.2f}") for m in self.regions if not m.solvent] \
            if (opts.get("integrals") or opts.get("curves")) else []
        want_curves, want_ints = opts.get("curves", False), opts.get("integrals", False)
        peak_labels = [(p.ppm, f"{p.ppm:.{3 if is1h else 1}f}") for p in self.peaks if p.ctype != "solvent"] \
            if opts.get("peaks") else []
        mult_labels = []
        if opts.get("mults"):
            for m in self.regions:
                if m.solvent:
                    continue
                txt = m.mult + (f", J = {', '.join(f'{j:.1f}' for j in m.js)} Hz" if m.js else "")
                mult_labels.append((m.centre if m.mult != "m" else (m.lo + m.hi) / 2, txt))
        dept = None
        if opts.get("dept"):
            d = self.dept135 or self.dept90
            dept = (d.ppm, d.real, d.kind + ("   (up: CH, CH₃ · down: CH₂)" if d is self.dept135 else ""))
        for k in ("integrals", "curves", "peaks", "mults", "dept"):
            opts.pop(k, None)
        try:
            px.nmr_figure(fn, self.sp.ppm, self.sp.real, xlim=xlim, regions=regions,
                          integral_curves=want_curves, show_integrals=want_ints,
                          peak_labels=peak_labels, multiplet_labels=mult_labels, dept=dept, **opts)
        except Exception as e:
            self.error(f"Export failed:\n{e}")
            return
        self.done(fn)

    def show_help(self):
        QtWidgets.QMessageBox.information(self, "NMR – how to", """
<b>Opening data</b><br>
Bruker: open the experiment's .zip, or the numbered experiment folder (the one containing <i>fid</i> and
<i>acqus</i>). TopSpin's processed spectrum (pdata/1) is used when present, otherwise the FID is
processed here. TopSpin ASCII exports and JCAMP-DX files also open.<br><br>
<b>¹H spectra</b><br>
<i>Auto analyse</i> finds each multiplet, integrates it and measures the coupling constants.
Check the list: drag the green edges to adjust a region, drag across the spectrum (mode 2) to add one,
merge rows that belong together, untick <b>Report</b> for solvent/water/impurities.
Type the number of protons for one signal in the <b>nH</b> column to scale all integrals.<br>
Multiplicity and J can be typed over when a multiplet is overlapped or second order.<br><br>
<b>¹³C and DEPT</b><br>
Peaks are picked automatically. Add a DEPT-135 (File ▸ Add DEPT): positive = CH or CH₃, negative = CH₂,
absent = quaternary. Add a DEPT-90 too to separate CH from CH₃.<br><br>
<b>Report</b><br>
The Report tab gives the journal-style line (e.g. δ 7.54 (d, <i>J</i> = 7.5 Hz, 2H)); <i>Copy (formatted)</i>
keeps superscripts and italics in Word. Tables export to Excel/CSV or copy straight into Word.<br><br>
<b>Mouse</b><br>
Wheel: zoom · Right-drag: zoom box · Right-click: zoom out · Home: full spectrum · 1–4: modes
""")
