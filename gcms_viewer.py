#!/usr/bin/env python3
"""
GC-MS Viewer - view Agilent .D GC-MS data (ChemStation and MassHunter), export the
chromatogram and mass spectra as CSV / high-resolution images, and integrate peaks
manually.

Run:  python gcms_viewer.py  [optional path to a .D folder]
"""
from __future__ import annotations

import csv
import hashlib
import os
import sys
import traceback

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

import gcms_reader as gr
import gcxgc as gx
import gcxgc_view as gxv
import integration as integ
import library_panel as lp
import mesh_export as me
import view3d
import plots_export as px
import tables
from view_widgets import make_plot
from export_dialogs import ImageExportDialog

from common import APP_NAME, __version__

pg.setConfigOptions(antialias=True, background="w", foreground="#222222")

COL_TRACE = "#1f3b73"
COL_SPEC = "#111111"
COL_SEL = (214, 39, 40)
COL_BKG = (120, 120, 120, 60)
COL_PEAK = (76, 120, 168, 90)
COL_PEAK_SEL = (255, 165, 0, 130)
COL_MIRROR = (200, 40, 40)

MODES = {
    "spectrum": ("Spectrum",
                 "Click the chromatogram to show that scan's mass spectrum. "
                 "Drag across a region to average its spectra."),
    "integrate": ("Integrate",
                  "Drag from the start to the end of a peak to integrate it. "
                  "The baseline is drawn between the two ends (see the Peaks panel for options)."),
    "background": ("Background",
                   "Click or drag a quiet region to use as background. "
                   "'Subtract bkg' above the spectrum is ticked automatically."),
}
MODES_2D = {
    "spectrum": "Click the 2D plot for that scan's spectrum; drag a box to average the spectra inside it.",
    "integrate": "Drag a box around a peak (blob) to integrate its volume. Click a box to select it.",
    "background": "Click or drag a box over a quiet area to use as background; 'Subtract bkg' is ticked for you.",
}
NAV_HINT_2D = "   |   Wheel: zoom (Shift: ¹tR only, Ctrl: ²tR only) · Right-drag: zoom box · Right-click: zoom out"
NAV_HINT = "   |   Wheel: zoom · Right-drag: zoom box · Right-click: zoom out (Help ▸ Controls)"


# =============================================================================
# Image export dialog
# =============================================================================
class Model3DDialog(QtWidgets.QDialog):
    """Options for exporting the GCxGC chromatogram as a solid 3D model."""

    def __init__(self, parent, settings):
        super().__init__(parent)
        self.s = settings
        g = lambda k, d: settings.value(f"model/{k}", d)  # noqa: E731
        self.setWindowTitle("Export 3D model")
        f = QtWidgets.QFormLayout(self)
        self.fmt = QtWidgets.QComboBox()
        self.fmt.addItem("GLB (glTF binary: PowerPoint, Windows 3D Viewer, web, Blender, Unity)", "glb")
        self.fmt.addItem("FBX (Blender, Unity, Unreal, 3ds Max)", "fbx")
        self.fmt.addItem("STL (3D printing - no colours, millimetres)", "stl")
        self.fmt.setCurrentIndex(int(g("fmt", 0)))
        self.region = QtWidgets.QComboBox()
        self.region.addItems(["Current 2D view", "Whole run"])
        self.region.setCurrentIndex(int(g("region", 0)))
        self.width = QtWidgets.QDoubleSpinBox()
        self.width.setDecimals(3)
        self.width.setRange(0.001, 5000)
        self.stretched = QtWidgets.QCheckBox("Stretched aspect (wide model, 2.4 : 1)")
        self.stretched.setChecked(g("stretched", "true") == "true")
        self.height = QtWidgets.QDoubleSpinBox()
        self.height.setRange(0.1, 5)
        self.height.setSingleStep(0.1)
        self.height.setValue(float(g("height", 1.0)))
        self.height.setSuffix(" ×")
        self.max1 = QtWidgets.QSpinBox()
        self.max1.setRange(50, 4000)
        self.max1.setSingleStep(100)
        self.max1.setValue(int(g("max1", 800)))
        self.max1.setToolTip("Points along ¹tR. Fewer modulations than this are smoothly interpolated up;\n"
                             "more are max-pooled down so peak tops survive.")
        self.max2 = QtWidgets.QSpinBox()
        self.max2.setRange(20, 1000)
        self.max2.setSingleStep(25)
        self.max2.setValue(int(g("max2", 200)))
        self.max2.setToolTip("Points along ²tR (max-pooled)")
        self.match = QtWidgets.QCheckBox("Colours use the on-screen colour range (untick: colour by height)")
        self.match.setChecked(g("match", "true") == "true")
        self.est = QtWidgets.QLabel()
        self.est.setStyleSheet("color:#555;")
        f.addRow("Format", self.fmt)
        f.addRow("Region", self.region)
        f.addRow("Model width", self.width)
        f.addRow("", self.stretched)
        f.addRow("Height exaggeration", self.height)
        f.addRow("¹tR resolution", self.max1)
        f.addRow("²tR resolution", self.max2)
        f.addRow("", self.match)
        note = QtWidgets.QLabel("Scaling (linear / √ / log), colour scheme, smoothing and display baseline "
                                "follow the 2D plot. The base and sides use the 3D base colour (Colours ▸ Edit…).")
        note.setWordWrap(True)
        note.setStyleSheet("color:#555;")
        f.addRow(note)
        f.addRow("Estimate", self.est)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)
        self._fmt_changed()
        self.fmt.currentIndexChanged.connect(self._fmt_changed)
        for w in (self.max1, self.max2):
            w.valueChanged.connect(self._estimate)

    def _fmt_changed(self, *_):
        stl = self.fmt.currentData() == "stl"
        self.width.blockSignals(True)
        if stl:
            self.width.setSuffix(" mm")
            self.width.setValue(float(self.s.value("model/width_mm", 150)))
        else:
            self.width.setSuffix(" m")
            self.width.setValue(float(self.s.value("model/width_m", 1.0)))
        self.width.blockSignals(False)
        self.match.setEnabled(not stl)
        self._estimate()

    def _estimate(self, *_):
        n = self.max1.value() * self.max2.value() * 2
        per = {"glb": 22, "fbx": 70, "stl": 25}[self.fmt.currentData()]
        self.est.setText(f"≈ {n:,} triangles, ≈ {n * per / 1e6:.0f} MB (fewer for small regions)")

    def options(self):
        s = self.s
        fmt = self.fmt.currentData()
        s.setValue("model/fmt", self.fmt.currentIndex())
        s.setValue("model/region", self.region.currentIndex())
        s.setValue("model/width_mm" if fmt == "stl" else "model/width_m", self.width.value())
        s.setValue("model/stretched", str(self.stretched.isChecked()).lower())
        s.setValue("model/height", self.height.value())
        s.setValue("model/max1", self.max1.value())
        s.setValue("model/max2", self.max2.value())
        s.setValue("model/match", str(self.match.isChecked()).lower())
        return {"fmt": fmt, "full": self.region.currentIndex() == 1, "width": self.width.value(),
                "stretched": self.stretched.isChecked(), "height": self.height.value(),
                "max1": self.max1.value(), "max2": self.max2.value(),
                "match": self.match.isChecked() and fmt != "stl"}


# =============================================================================
# Main window
# =============================================================================
class MainWindow(QtWidgets.QMainWindow):
    sigHome = QtCore.Signal()
    sigCompare = QtCore.Signal(list)      # paths of the current run(s) to start a comparison with
    TECHNIQUE = "GC-MS / GC-FID / GCxGC"

    def __init__(self):
        super().__init__()
        self.settings = QtCore.QSettings("UoP", "GCMSViewer")
        self.run: gr.GCMSRun | None = None
        self.d_path = None
        self.mode = "spectrum"
        self.trace_key = "TIC"
        self.trace_y = None
        self.sel = None          # (i0, i1) selected scans for spectrum
        self.bkg = None          # (i0, i1) background scans
        self.peaks: list[integ.Peak] = []
        self.peak_items = []
        self.sel_peak = None
        self.cur_spec = (np.zeros(0), np.zeros(0))
        self.cur_spec_desc = ""
        self.spec_y = np.zeros(0)
        self.mirror = None       # (hit, mz, intensity 0-999) of the selected library hit
        self.sel_idx = np.zeros(0, dtype=np.int64)   # scans making up the current spectrum
        self.sel_desc = ""
        self.sel_tag = ""
        self.bkg_idx = None
        self.bkg_desc = ""
        self.fold: gx.Fold | None = None              # GCxGC folding (None = 1D)
        self.M = None                                  # folded matrix of the current trace
        self.blobs: list[gx.Blob] = []
        self.sel_blob = None

        self.setWindowTitle(APP_NAME)
        self.setAcceptDrops(True)
        self._build_ui()
        self._place_window()
        self._set_mode("spectrum")
        self._update_enabled()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        # ---- toolbar
        tb = self.addToolBar("Main")
        tb.setMovable(False)
        tb.setIconSize(QtCore.QSize(18, 18))
        st = self.style()
        self.act_open = QtGui.QAction(st.standardIcon(QtWidgets.QStyle.SP_DirOpenIcon), "Open .D…", self)
        self.act_open.setToolTip("Open an Agilent .D folder (Ctrl+O)")
        self.act_open.setShortcut(QtGui.QKeySequence.Open)
        self.act_open.triggered.connect(self.open_dialog)
        tb.addAction(self.act_open)
        self.act_open_cdf = QtGui.QAction(st.standardIcon(QtWidgets.QStyle.SP_FileIcon), "Open .cdf…", self)
        self.act_open_cdf.setToolTip("Open an ANDI netCDF file - e.g. a ChromSpace / ChromaTOF GCxGC export (Ctrl+Shift+O)")
        self.act_open_cdf.setShortcut("Ctrl+Shift+O")
        self.act_open_cdf.triggered.connect(self.open_cdf_dialog)
        tb.addAction(self.act_open_cdf)
        tb.addWidget(QtWidgets.QLabel("  Data: "))
        self.src_combo = QtWidgets.QComboBox()
        self.src_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)
        self.src_combo.setMinimumContentsLength(20)
        self.src_combo.setToolTip("Which data in the .D to show: MassHunter (AcqData), ChemStation (data.ms), "
                                  "or a detector signal such as FID1A")
        self.src_combo.currentIndexChanged.connect(self._source_changed)
        tb.addWidget(self.src_combo)
        tb.addSeparator()

        tb.addWidget(QtWidgets.QLabel(" Mouse mode: "))
        self.mode_group = QtWidgets.QButtonGroup(self)
        self.mode_btns = {}
        for i, (k, (label, tip)) in enumerate(MODES.items()):
            b = QtWidgets.QToolButton()
            b.setText(f"{label}  ({i + 1})")
            b.setCheckable(True)
            b.setToolTip(tip)
            b.setShortcut(str(i + 1))
            b.clicked.connect(lambda _=False, k=k: self._set_mode(k))
            self.mode_group.addButton(b)
            self.mode_btns[k] = b
            tb.addWidget(b)
        tb.addSeparator()

        tb.addWidget(QtWidgets.QLabel(" Chromatogram: "))
        self.trace_combo = QtWidgets.QComboBox()
        self.trace_combo.addItems(["TIC", "EIC"])
        self.trace_combo.currentIndexChanged.connect(self._trace_changed)
        tb.addWidget(self.trace_combo)
        self.eic_edit = QtWidgets.QLineEdit()
        self.eic_edit.setPlaceholderText("m/z, e.g. 176 or 145, 176")
        self.eic_edit.setFixedWidth(170)
        self.eic_edit.setToolTip("One or more m/z values (comma separated, summed). "
                                 "Tip: double-click a peak in the spectrum to plot its EIC.")
        self.eic_edit.returnPressed.connect(self._eic_entered)
        tb.addWidget(self.eic_edit)
        tb.addWidget(QtWidgets.QLabel(" ±"))
        self.eic_tol = QtWidgets.QDoubleSpinBox()
        self.eic_tol.setRange(0.001, 5); self.eic_tol.setDecimals(3); self.eic_tol.setSingleStep(0.1)
        self.eic_tol.setValue(0.5); self.eic_tol.setSuffix(" Da")
        self.eic_tol.editingFinished.connect(self._eic_entered)
        tb.addWidget(self.eic_tol)
        tb.addSeparator()
        self.act_reset = QtGui.QAction("Reset zoom", self)
        self.act_reset.setShortcut(QtCore.Qt.Key_Home)
        self.act_reset.triggered.connect(self.reset_zoom)
        tb.addAction(self.act_reset)

        # ---- GCxGC toolbar (second row)
        self.addToolBarBreak()
        tb2 = self.addToolBar("GCxGC")
        tb2.setMovable(False)
        self.tb2 = tb2
        tb2.addWidget(QtWidgets.QLabel(" GCxGC  modulation period: "))
        self.mod_spin = QtWidgets.QDoubleSpinBox()
        self.mod_spin.setRange(0, 30)
        self.mod_spin.setDecimals(3)
        self.mod_spin.setSingleStep(0.5)
        self.mod_spin.setSuffix(" s")
        self.mod_spin.setSpecialValueText("off (1D)")
        self.mod_spin.setToolTip("Modulation period. 0 = ordinary 1D GC-MS. Press Enter to apply.")
        self.mod_spin.editingFinished.connect(self._fold_settings_changed)
        tb2.addWidget(self.mod_spin)
        tb2.addWidget(QtWidgets.QLabel("  phase shift: "))
        self.phase_spin = QtWidgets.QDoubleSpinBox()
        self.phase_spin.setRange(-30, 30)
        self.phase_spin.setDecimals(2)
        self.phase_spin.setSingleStep(0.05)
        self.phase_spin.setSuffix(" s")
        self.phase_spin.setToolTip("Shifts where each modulation starts - use it to move wrapped-around peaks "
                                   "back into place. Press Enter to apply.")
        self.phase_spin.editingFinished.connect(self._fold_settings_changed)
        tb2.addWidget(self.phase_spin)
        tb2.addSeparator()
        tb2.addWidget(QtWidgets.QLabel(" View: "))
        self.view_group = QtWidgets.QButtonGroup(self)
        self.btn_view2d = QtWidgets.QToolButton()
        self.btn_view2d.setText("2D")
        self.btn_view1d = QtWidgets.QToolButton()
        self.btn_view1d.setText("1D raw")
        for i, b in enumerate((self.btn_view2d, self.btn_view1d)):
            b.setCheckable(True)
            self.view_group.addButton(b, i)
            tb2.addWidget(b)
        self.btn_view2d.setToolTip("Two-dimensional colour plot (Ctrl+2)")
        self.btn_view1d.setToolTip("The raw, modulated 1D chromatogram (Ctrl+1)")
        self.btn_view2d.setShortcut("Ctrl+2")
        self.btn_view1d.setShortcut("Ctrl+1")
        self.view_group.idClicked.connect(lambda i: self._show_view(two_d=(i == 0)))
        self.fold_lbl = QtWidgets.QLabel("")
        self.fold_lbl.setStyleSheet("color:#555;")
        tb2.addWidget(self.fold_lbl)

        # ---- chromatogram panel
        self.chrom_pw, self.chrom_vb = make_plot(self._chrom_yrange, "Retention time (min)", "Abundance")
        self.chrom_curve = self.chrom_pw.plot([], [], pen=pg.mkPen(COL_TRACE, width=1.2))
        self.sel_line = pg.InfiniteLine(angle=90, pen=pg.mkPen(COL_SEL, width=1.5))
        self.sel_region = pg.LinearRegionItem(brush=pg.mkBrush(*COL_SEL, 50), movable=False,
                                              pen=pg.mkPen(COL_SEL, width=1))
        self.bkg_region = pg.LinearRegionItem(brush=pg.mkBrush(*COL_BKG), movable=False,
                                              pen=pg.mkPen((90, 90, 90), width=1, style=QtCore.Qt.DashLine))
        self.drag_region = pg.LinearRegionItem(brush=pg.mkBrush(255, 200, 0, 60), movable=False)
        self.drag_base = pg.PlotDataItem([], [], pen=pg.mkPen((192, 57, 43), width=1.5, style=QtCore.Qt.DashLine))
        for it in (self.sel_line, self.sel_region, self.bkg_region, self.drag_region):
            it.hide()
            self.chrom_pw.addItem(it, ignoreBounds=True)
        self.chrom_pw.addItem(self.drag_base, ignoreBounds=True)
        self.chrom_vb.sigClick.connect(self._chrom_click)
        self.chrom_vb.sigDrag.connect(self._chrom_drag)
        self.chrom_pw.scene().sigMouseMoved.connect(self._chrom_hover)

        self.chrom_title = QtWidgets.QLabel("Open an Agilent .D folder or a netCDF file (File ▸ Open, or drag it onto this window)")
        self.chrom_title.setStyleSheet("font-weight:600;")
        self.btn_chrom_csv = QtWidgets.QPushButton("CSV…")
        self.btn_chrom_csv.setToolTip("Export the chromatogram (TIC/EIC) as CSV")
        self.btn_chrom_csv.clicked.connect(self.export_chrom_csv)
        self.btn_chrom_img = QtWidgets.QPushButton("Image…")
        self.btn_chrom_img.setToolTip("Export the chromatogram as a high-resolution image")
        self.btn_chrom_img.clicked.connect(self.export_chrom_image)
        chrom_box = self._panel(self.chrom_title, [self.btn_chrom_csv, self.btn_chrom_img], self.chrom_pw)

        # ---- GCxGC panel
        self.gxp = gxv.GCxGCPanel(self.settings)
        self.gxp.sigClick.connect(self._gx_click)
        self.gxp.sigRect.connect(self._gx_rect)
        self.gxp.sigHover.connect(self._gx_hover)
        self.gxp.btn_csv.clicked.connect(self.export_2d_csv)
        self.gxp.btn_img.clicked.connect(self.export_2d_image)
        self.gxp.btn_model.clicked.connect(self.export_3d_model)
        # 3D view beside the 2D plot (can pop out into its own window)
        self.view3d = view3d.View3DPanel(self.gxp, self.settings)
        self.view3d.sigPop.connect(self.toggle_3d_popout)
        self.holder3d = QtWidgets.QWidget()
        hl = QtWidgets.QVBoxLayout(self.holder3d)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(self.view3d)
        self.win3d = None
        self.gx_split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.gx_split.addWidget(self.gxp)
        self.gx_split.addWidget(self.holder3d)
        self.gx_split.setChildrenCollapsible(False)
        self.gx_split.setStretchFactor(0, 3)
        self.gx_split.setStretchFactor(1, 2)
        self.gx_split.setSizes([800, 520])
        self.holder3d.setVisible(self.settings.value("3d/show", "true") == "true")
        self.chrom_stack = QtWidgets.QStackedWidget()
        self.chrom_stack.addWidget(chrom_box)
        self.chrom_stack.addWidget(self.gx_split)

        # ---- spectrum panel
        self.spec_pw, self.spec_vb = make_plot(self._spec_yrange, "m/z", "Abundance")
        self.spec_curve = self.spec_pw.plot([], [], pen=pg.mkPen(COL_SPEC, width=1.2), connect="pairs")
        self.mirror_curve = self.spec_pw.plot([], [], pen=pg.mkPen(COL_MIRROR, width=1.2), connect="pairs")
        # Sticks are drawn as (baseline, top) point pairs. Clip-to-view can drop an odd number of
        # points at the left edge and pair one peak's top with the next one's baseline (slanted
        # "rotated" peaks), so never clip spectra - they are small anyway.
        self.spec_pw.getPlotItem().setClipToView(False)
        for c in (self.spec_curve, self.mirror_curve):
            c.setClipToView(False)
        self.zero_line = pg.InfiniteLine(pos=0, angle=0, pen=pg.mkPen("#888888", width=0.8))
        self.zero_line.hide()
        self.spec_pw.addItem(self.zero_line, ignoreBounds=True)
        self.spec_labels = []
        self.spec_vb.sigRangeChanged.connect(lambda *_: self._label_spectrum())
        self.spec_vb.sigDoubleClick.connect(self._spec_dblclick)
        self.spec_title = QtWidgets.QLabel("Mass spectrum")
        self.spec_title.setStyleSheet("font-weight:600;")
        self.spec_title.setWordWrap(True)
        self.spec_title.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        self.chk_rel = QtWidgets.QCheckBox("Relative %")
        self.chk_rel.setChecked(True)
        self.chk_rel.toggled.connect(lambda *_: self._refresh_spectrum(changed=False))
        self.chk_bkg = QtWidgets.QCheckBox("Subtract bkg")
        self.chk_bkg.setToolTip("Subtract the background region chosen in Background mode")
        self.chk_bkg.toggled.connect(self._refresh_spectrum)
        self.btn_spec_csv = QtWidgets.QPushButton("CSV…")
        self.btn_spec_csv.setToolTip("Export spectrum as CSV")
        self.btn_spec_csv.clicked.connect(self.export_spec_csv)
        self.btn_spec_img = QtWidgets.QPushButton("Image…")
        self.btn_spec_img.setToolTip("Export spectrum as a high-resolution image")
        self.btn_spec_img.clicked.connect(self.export_spec_image)
        self.btn_spec_copy = QtWidgets.QPushButton("Copy")
        self.btn_spec_copy.setToolTip("Copy the spectrum as tab-separated m/z / abundance (paste into Excel, NIST MS Search, …)")
        self.btn_spec_copy.clicked.connect(self.copy_spectrum)
        spec_box = self.spec_box = self._panel(self.spec_title,
                               [self.chk_rel, self.chk_bkg, self.btn_spec_copy, self.btn_spec_csv, self.btn_spec_img],
                               self.spec_pw)

        # ---- side tabs: peaks + info
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._build_peaks_tab(), "Peaks")
        self.lib_panel = lp.LibraryPanel(self.settings, self._library_query, self.set_mirror,
                                         lambda msg, t=0: self.statusBar().showMessage(msg, t), self)
        self.tabs.addTab(self.lib_panel, "Library search")
        self.info = QtWidgets.QPlainTextEdit(readOnly=True)
        self.tabs.addTab(self.info, "File info")

        bottom = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        bottom.addWidget(spec_box)
        bottom.addWidget(self.tabs)
        self.tabs.setMinimumWidth(240)
        self.tabs.setMinimumHeight(140)   # contents scroll/squash rather than forcing a tall window
        bottom.setChildrenCollapsible(False)
        bottom.setStretchFactor(0, 3)
        bottom.setStretchFactor(1, 2)
        bottom.setSizes([820, 560])
        main = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        main.addWidget(self.chrom_stack)
        main.addWidget(bottom)
        main.setChildrenCollapsible(False)
        main.setSizes([460, 420])
        self.setCentralWidget(main)

        # ---- menus
        mb = self.menuBar()
        fm = mb.addMenu("&File")
        fm.addAction(self.act_open)
        fm.addAction(self.act_open_cdf)
        self.recent_menu = fm.addMenu("Open recent")
        self._rebuild_recent()
        fm.addSeparator()
        for text, fn in [("Export chromatogram CSV…", self.export_chrom_csv),
                         ("Export chromatogram image…", self.export_chrom_image),
                         ("Export spectrum CSV…", self.export_spec_csv),
                         ("Export spectrum image…", self.export_spec_image),
                         ("Export peak table (Excel/CSV)…", self.export_peaks_csv),
                         ("Export GCxGC 2D matrix CSV…", self.export_2d_csv),
                         ("Export GCxGC 2D image…", self.export_2d_image),
                         ("Export GCxGC 3D model (GLB / FBX / STL)…", self.export_3d_model)]:
            fm.addAction(text, fn)
        fm.addSeparator()
        fm.addAction("Compare samples (up to 5)…",
                     lambda: self.sigCompare.emit([self.d_path] if self.d_path else [])).setShortcut("Ctrl+M")
        fm.addAction("Home – choose another technique…", self.sigHome.emit).setShortcut("Ctrl+H")
        fm.addAction("Close window", self.close, QtGui.QKeySequence.Close)
        lm = mb.addMenu("&Library")
        a = lm.addAction("Search open libraries", lambda: self.lib_panel.search_open())
        a.setShortcut("F5")
        a = lm.addAction("Search NIST MS Search", lambda: self.lib_panel.search_nist())
        a.setShortcut("F6")
        a.setEnabled(lp.IS_WINDOWS)
        lm.addSeparator()
        lm.addAction("Libraries && NIST settings…", lambda: self.lib_panel.open_settings())
        lm.addAction("Copy hit list", lambda: self.lib_panel.copy_hits())
        vm = mb.addMenu("&View")
        vm.addAction(self.act_reset)
        vm.addSeparator()
        self.act_3d = vm.addAction("3D view (GCxGC)")
        self.act_3d.setCheckable(True)
        self.act_3d.setChecked(self.settings.value("3d/show", "true") == "true")
        self.act_3d.setShortcut("Ctrl+3")
        self.act_3d.toggled.connect(self._show_3d)
        vm.addAction("Pop out / dock 3D view", self.toggle_3d_popout)
        hm = mb.addMenu("&Help")
        hm.addAction("Mouse && keyboard controls", self.show_help)
        hm.addAction("About", lambda: QtWidgets.QMessageBox.about(
            self, APP_NAME, f"<b>{APP_NAME}</b> {__version__}<br>GC-MS, GC-FID and GCxGC viewer.<br>"
                            "File reading for MassHunter uses the open-source <i>rainbow-api</i> library.<br>"
                            "Open spectral libraries: MassBank (massbank.eu), MoNA (mona.fiehnlab.ucdavis.edu)."))

        undo = QtGui.QShortcut(QtGui.QKeySequence.Undo, self)
        undo.activated.connect(self.undo_peak)
        dele = QtGui.QShortcut(QtGui.QKeySequence.Delete, self.peak_table)
        dele.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        dele.activated.connect(self.delete_selected_peaks)
        dele2 = QtGui.QShortcut(QtGui.QKeySequence.Delete, self.blob_table)
        dele2.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        dele2.activated.connect(self.delete_selected_peaks)
        for key, step in ((QtCore.Qt.Key_Left, -1), (QtCore.Qt.Key_Right, 1)):
            sc = QtGui.QShortcut(QtGui.QKeySequence(key), self.chrom_pw)
            sc.setContext(QtCore.Qt.WidgetShortcut)
            sc.activated.connect(lambda s=step: self.step_scan(s))
        # GCxGC: left/right = previous/next modulation, up/down = previous/next scan
        for key, step in ((QtCore.Qt.Key_Left, "-mod"), (QtCore.Qt.Key_Right, "+mod"),
                          (QtCore.Qt.Key_Down, -1), (QtCore.Qt.Key_Up, 1)):
            sc = QtGui.QShortcut(QtGui.QKeySequence(key), self.gxp.pw)
            sc.setContext(QtCore.Qt.WidgetShortcut)
            sc.activated.connect(lambda s=step: self.step_scan(s))

        self.status = self.statusBar()
        self.cursor_lbl = QtWidgets.QLabel()
        self.status.addPermanentWidget(self.cursor_lbl)

    def _place_window(self):
        """Fit the window to the screen it opens on, so the title bar is always reachable."""
        scr = (QtGui.QGuiApplication.screenAt(QtGui.QCursor.pos())
               or QtGui.QGuiApplication.primaryScreen())
        avail = scr.availableGeometry()
        geo = self.settings.value("geometry")
        if geo is not None and self.restoreGeometry(geo):
            fg = self.frameGeometry()
            # accept the saved position only if it fits on a screen with the title bar visible
            on_screen = any(sc.availableGeometry().contains(fg.topLeft() + QtCore.QPoint(40, 10))
                            and fg.width() <= sc.availableGeometry().width() + 16
                            and fg.height() <= sc.availableGeometry().height() + 16
                            for sc in QtGui.QGuiApplication.screens())
            if on_screen:
                if self.settings.value("maximized", "false") == "true":
                    QtCore.QTimer.singleShot(0, self.showMaximized)
                return
        w = min(1400, int(avail.width() * 0.92))
        h = min(900, int(avail.height() * 0.90))
        self.resize(w, h)
        fg = self.frameGeometry()
        fg.moveCenter(avail.center())
        self.move(max(avail.left(), fg.left()), max(avail.top(), fg.top()))
        if avail.width() < 1100 or avail.height() < 700:
            QtCore.QTimer.singleShot(0, self.showMaximized)

    def closeEvent(self, e):
        self.settings.setValue("maximized", "true" if self.isMaximized() else "false")
        if not self.isMaximized():
            self.settings.setValue("geometry", self.saveGeometry())
        super().closeEvent(e)

    def _panel(self, title, widgets, plot):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(6, 4, 6, 2)
        title.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        title.setMinimumWidth(60)
        h = QtWidgets.QHBoxLayout()
        h.addWidget(title, 1)
        for x in widgets:
            h.addWidget(x)
        v.addLayout(h)
        v.addWidget(plot, 1)
        plot.setMinimumSize(250, 110)
        return w

    HINT_1D = ("Switch to <b>Integrate</b> mode (key 2) and drag across a peak. Ctrl+Z undoes the last peak. "
               "Peaks are kept per trace (TIC or each EIC); Area % is within the current trace. "
               "Double-click the Name column to annotate.")
    HINT_2D = ("In <b>Integrate</b> mode (key 2) drag a box around a peak (blob). The baseline is set "
               "separately in each modulation inside the box; volume = signal above it × scan time. "
               "<b>Identify</b> fills in names from the library. Double-click Name to edit.")

    def _build_peaks_tab(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        self.peak_stack = QtWidgets.QStackedWidget()
        p1 = QtWidgets.QWidget()
        v1 = QtWidgets.QVBoxLayout(p1)
        v1.setContentsMargins(0, 0, 0, 0)
        form = QtWidgets.QFormLayout()
        self.base_combo = QtWidgets.QComboBox()
        self.base_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.base_combo.setMinimumContentsLength(14)
        for k, txt in integ.BASELINES.items():
            self.base_combo.addItem(txt, k)
        self.base_combo.setCurrentIndex(int(self.settings.value("integ/base", 0)))
        self.base_combo.currentIndexChanged.connect(lambda i: self.settings.setValue("integ/base", i))
        self.snap = QtWidgets.QSpinBox()
        self.snap.setRange(0, 50)
        self.snap.setValue(int(self.settings.value("integ/snap", 3)))
        self.snap.setSuffix(" scans")
        self.snap.setToolTip("Move each end of a new integration to the lowest point within this many scans (0 = off)")
        self.snap.valueChanged.connect(lambda x: self.settings.setValue("integ/snap", x))
        form.addRow("Baseline", self.base_combo)
        form.addRow("Snap to valley", self.snap)
        v1.addLayout(form)

        self.peak_table = QtWidgets.QTableWidget(0, 9)
        self.peak_table.setHorizontalHeaderLabels(
            ["#", "Trace", "RT (min)", "Start", "End", "Height", "Area (counts·s)", "Area %", "Name / note"])
        self.peak_table.verticalHeader().hide()
        self.peak_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.peak_table.setEditTriggers(QtWidgets.QAbstractItemView.DoubleClicked
                                        | QtWidgets.QAbstractItemView.EditKeyPressed)
        hh = self.peak_table.horizontalHeader()
        hh.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        hh.setStretchLastSection(True)
        self.peak_table.itemSelectionChanged.connect(self._peak_selected)
        self.peak_table.itemChanged.connect(self._peak_note_edited)
        v1.addWidget(self.peak_table, 1)
        self.peak_stack.addWidget(p1)

        # GCxGC blobs
        p2 = QtWidgets.QWidget()
        v2 = QtWidgets.QVBoxLayout(p2)
        v2.setContentsMargins(0, 0, 0, 0)
        f2 = QtWidgets.QFormLayout()
        self.base2d = QtWidgets.QComboBox()
        self.base2d.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.base2d.setMinimumContentsLength(14)
        for k, txt in gx.BASELINES_2D.items():
            self.base2d.addItem(txt, k)
        self.base2d.setCurrentIndex(int(self.settings.value("integ/base2d", 0)))
        self.base2d.currentIndexChanged.connect(lambda i: self.settings.setValue("integ/base2d", i))
        h2 = QtWidgets.QHBoxLayout()
        h2.addWidget(self.base2d, 1)
        self.btn_identify = QtWidgets.QPushButton("Identify")
        self.btn_identify.setToolTip("Library-search the selected peaks (all peaks if none selected) and fill in "
                                     "the Name and Match columns from the top hit")
        self.btn_identify.clicked.connect(self.identify_blobs)
        h2.addWidget(self.btn_identify)
        f2.addRow("Baseline", h2)
        v2.addLayout(f2)
        self.blob_table = QtWidgets.QTableWidget(0, 10)
        self.blob_table.setHorizontalHeaderLabels(
            ["#", "Trace", "¹tR (min)", "²tR (s)", "Height", "Volume (counts·s)", "Vol %", "Mods",
             "Name / ID", "Match"])
        self.blob_table.verticalHeader().hide()
        self.blob_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.blob_table.setEditTriggers(QtWidgets.QAbstractItemView.DoubleClicked
                                        | QtWidgets.QAbstractItemView.EditKeyPressed)
        hh2 = self.blob_table.horizontalHeader()
        hh2.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        self.blob_table.setMinimumHeight(40)
        self.blob_table.itemSelectionChanged.connect(self._blob_selected)
        self.blob_table.itemChanged.connect(self._blob_note_edited)
        v2.addWidget(self.blob_table, 1)
        self.peak_stack.addWidget(p2)
        v.addWidget(self.peak_stack, 1)

        h = QtWidgets.QGridLayout()
        b_del = QtWidgets.QPushButton("Delete selected")
        b_del.clicked.connect(self.delete_selected_peaks)
        b_clr = QtWidgets.QPushButton("Clear all")
        b_clr.clicked.connect(self.clear_peaks)
        b_exp = QtWidgets.QPushButton("Export table…")
        b_exp.setToolTip("Save the full peak table as Excel (.xlsx), CSV or text")
        b_exp.clicked.connect(self.export_peaks_csv)
        b_cp = QtWidgets.QPushButton("Copy for report")
        b_cp.setToolTip("Copies a short table (peak, RT, area, area %, name). Paste into Word/PowerPoint "
                        "for a formatted table, or into Excel")
        b_cp.clicked.connect(self.copy_peaks)
        for k, b in enumerate((b_del, b_clr, b_cp, b_exp)):
            h.addWidget(b, k // 2, k % 2)
        v.addLayout(h)
        self.peak_hint = QtWidgets.QLabel(self.HINT_1D)
        self.peak_hint.setWordWrap(True)
        self.peak_hint.setStyleSheet("color:#555;")
        self.peak_hint.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Ignored)
        self.peak_table.setMinimumHeight(40)
        v.addWidget(self.peak_hint)
        return w

    def _update_enabled(self):
        on = self.run is not None
        for w in (self.btn_chrom_csv, self.btn_chrom_img, self.btn_spec_csv, self.btn_spec_img,
                  self.btn_spec_copy, self.trace_combo, self.eic_edit, self.eic_tol,
                  self.mod_spin, self.phase_spin):
            w.setEnabled(on)
        for b in (self.btn_view2d, self.btn_view1d):
            b.setEnabled(on and self.fold is not None)
        if on:
            self._apply_detector_mode()

    def is2d(self):
        return self.fold is not None and self.chrom_stack.currentIndex() == 1

    # ------------------------------------------------------------ open
    def open_dialog(self):
        start = self.settings.value("last_dir", os.path.expanduser("~"))
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Select an Agilent .D folder", start)
        if d:
            self.open_path(d)

    def open_cdf_dialog(self):
        start = self.settings.value("last_dir", os.path.expanduser("~"))
        fn, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Open ANDI netCDF file", start,
                                                      "netCDF (*.cdf *.CDF *.nc);;All files (*)")
        if fn:
            self.open_path(fn)

    def open_path(self, path, prefer=None):
        path = os.path.abspath(path)
        if os.path.isfile(path) and path.lower().endswith((".cdf", ".nc")):
            return self._open_netcdf(path)
        if os.path.isfile(path):
            if os.path.basename(path).lower() == "data.ms":
                path = os.path.dirname(path)
            else:
                path = os.path.dirname(path)
        if not path.lower().endswith(".d"):
            # user may have picked AcqData or a file inside the .D
            parent = os.path.dirname(path)
            if parent.lower().endswith(".d"):
                path = parent
        srcs = gr.available_sources(path)
        if not srcs:
            QtWidgets.QMessageBox.warning(self, APP_NAME,
                                          f"No GC data found in:\n{path}\n\n"
                                          "Expected DATA.MS (ChemStation), AcqData/MSPeak.bin (MassHunter) "
                                          "or a detector signal file such as FID1A.ch.")
            return
        prefer = prefer or self.settings.value("prefer", "masshunter")
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            run = gr.read_d(path, prefer=prefer)
        except Exception as e:
            QtWidgets.QApplication.restoreOverrideCursor()
            QtWidgets.QMessageBox.critical(self, APP_NAME, f"Could not read {path}\n\n{e}\n\n{traceback.format_exc(limit=3)}")
            return
        QtWidgets.QApplication.restoreOverrideCursor()

        same_file = self.d_path == path
        self.run, self.d_path = run, path
        self.settings.setValue("last_dir", os.path.dirname(path))
        self._add_recent(path)

        self.src_combo.blockSignals(True)
        self.src_combo.clear()
        labels = {"masshunter": "MassHunter (AcqData)", "chemstation": "ChemStation (data.ms)"}
        for s in srcs:
            self.src_combo.addItem(labels.get(s, s[3:-3] + " signal" if s.startswith("ch:") else s), s)
        cur = ("masshunter" if "MassHunter" in run.source else "chemstation") if run.has_spectra \
            else "ch:" + run.metadata.get("signal", "") + ".ch"
        self.src_combo.setCurrentIndex(srcs.index(cur) if cur in srcs else 0)
        self.src_combo.setEnabled(len(srcs) > 1)
        self.src_combo.blockSignals(False)

        self._after_load(run, same_file)

    def open_run(self, run, period_s=None):
        """Show a run built elsewhere (e.g. imported from CSV). period_s: GCxGC modulation, if known."""
        self.run, self.d_path = run, run.path
        if period_s:
            self.settings.setValue(self._fold_key(), f"{float(period_s)}|0.0")
        self.src_combo.blockSignals(True)
        self.src_combo.clear()
        self.src_combo.addItem(run.source, "import")
        self.src_combo.setEnabled(False)
        self.src_combo.blockSignals(False)
        self._after_load(run, False)

    def _open_netcdf(self, path):
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            run = gr.read_netcdf(path)
        except Exception as e:
            QtWidgets.QApplication.restoreOverrideCursor()
            QtWidgets.QMessageBox.critical(self, APP_NAME, f"Could not read {path}\n\n{e}\n\n{traceback.format_exc(limit=3)}")
            return
        QtWidgets.QApplication.restoreOverrideCursor()
        same_file = self.d_path == path
        self.run, self.d_path = run, path
        self.settings.setValue("last_dir", os.path.dirname(path))
        self._add_recent(path)
        self.src_combo.blockSignals(True)
        self.src_combo.clear()
        self.src_combo.addItem("ANDI netCDF", "netcdf")
        self.src_combo.setEnabled(False)
        self.src_combo.blockSignals(False)
        self._after_load(run, same_file)

    def _fold_key(self):
        return "fold/" + hashlib.md5(os.path.abspath(self.d_path or "").encode()).hexdigest()[:12]

    def _after_load(self, run, same_file):
        if not same_file:
            self.peaks.clear()
            self.blobs.clear()
            self.bkg = None
            self.bkg_idx = None
            self.chk_bkg.setChecked(False)
            self.bkg_region.hide()
            self.gxp.bkg_rect.hide()
            self.trace_combo.blockSignals(True)
            self.trace_combo.setCurrentIndex(0)
            self.trace_combo.blockSignals(False)
        # GCxGC: remembered settings for this file, else detect the modulation period
        saved = self.settings.value(self._fold_key())
        msg = ""
        if saved:
            period, phase = (float(x) for x in str(saved).split("|"))
            msg = f"GCxGC: {period:g} s modulation (your setting for this file)"
        else:
            period, why = gr.guess_modulation_period(run, os.path.basename(self.d_path or ""),
                                                     signal_only_ok=run.source.startswith("ANDI"))
            phase = 0.0
            period = period or 0.0
            if period:
                msg = f"GCxGC detected: modulation period {why}"
        self.mod_spin.blockSignals(True)
        self.phase_spin.blockSignals(True)
        self.mod_spin.setValue(period)
        self.phase_spin.setValue(phase)
        self.mod_spin.blockSignals(False)
        self.phase_spin.blockSignals(False)
        self._make_fold()
        self._apply_detector_mode()
        self.setWindowTitle(f"{run.name} — {APP_NAME}")
        self._fill_info()
        self._show_view(two_d=self.fold is not None, refresh=False)
        self._update_trace(reset_view=True)
        self.select_scans(int(np.argmax(self.run.tic)))
        self._update_enabled()
        self.status.showMessage(f"Loaded {run.name}  ·  {run.source}  ·  {run.n_scans} scans  ·  "
                                f"{run.times[0]:.2f}–{run.times[-1]:.2f} min" + (f"  ·  {msg}" if msg else ""),
                                10000)

    def _area_unit(self):
        if self.run is None or self.run.has_spectra:
            return "counts"
        return self.run.metadata.get("signal_unit") or "signal"

    def _apply_detector_mode(self):
        """MS data: everything on.  Single-channel detectors (FID…): no spectra, EICs or library search."""
        ms = self.run is not None and self.run.has_spectra
        self.trace_combo.blockSignals(True)
        self.trace_combo.setItemText(0, self.run.trace_name if self.run else "TIC")
        if not ms:
            self.trace_combo.setCurrentIndex(0)
        self.trace_combo.blockSignals(False)
        for w in (self.chk_rel, self.chk_bkg, self.btn_spec_copy, self.btn_spec_csv, self.btn_spec_img,
                  self.lib_panel, self.mode_btns["background"]):
            w.setEnabled(ms)
        self.trace_combo.setEnabled(ms)
        self.eic_edit.setEnabled(ms)
        self.eic_tol.setEnabled(ms)
        if not ms and self.mode == "background":
            self._set_mode("spectrum")
        lab = self.run.signal_label if self.run else "Abundance"
        self.chrom_pw.setLabel("left", lab)
        self.spec_box.setVisible(ms)          # FID: give the space to the peak table
        u = self._area_unit()
        self.peak_table.setHorizontalHeaderItem(6, QtWidgets.QTableWidgetItem(f"Area ({u}·s)"))
        self.blob_table.setHorizontalHeaderItem(5, QtWidgets.QTableWidgetItem(f"Volume ({u}·s)"))
        if not ms:
            self.spec_curve.setData([], [])
            self.set_mirror(None, None)
            self.spec_title.setText(f"No mass spectra - {self.run.trace_name} detector data")

    # ------------------------------------------------------------ GCxGC fold / view
    def _make_fold(self):
        P = self.mod_spin.value()
        if self.run is None or P <= 0:
            self.fold = None
            self.fold_lbl.setText("")
            return
        self.fold = gx.Fold(self.run.times, P, self.phase_spin.value())
        f = self.fold
        self.fold_lbl.setText(f"   {f.n1} modulations × {f.n2} points ({f.dt * 1000:.0f} ms)")

    def _fold_settings_changed(self):
        if self.run is None:
            return
        P, ph = self.mod_spin.value(), self.phase_spin.value()
        old = (self.fold.P, self.fold.phase) if self.fold else (0.0, 0.0)
        if (P, ph) == old:
            return
        if self.blobs:
            if QtWidgets.QMessageBox.question(
                    self, APP_NAME, "Changing the modulation settings removes the integrated 2D peaks. Continue?") \
                    != QtWidgets.QMessageBox.Yes:
                self.mod_spin.setValue(old[0])
                self.phase_spin.setValue(old[1])
                return
            self.blobs.clear()
        self.settings.setValue(self._fold_key(), f"{P}|{ph}")
        self._make_fold()
        self._fill_info()
        self._show_view(two_d=self.fold is not None, refresh=False)
        self._update_trace(reset_view=self.fold is not None and old[0] != P)
        if self.sel_idx.size:
            self.select_set(self.sel_idx, self.sel_desc, self.sel_tag) if self.sel_idx.size > 1 \
                else self.select_scans(int(self.sel_idx[0]))
        self._update_enabled()

    def _show_view(self, two_d, refresh=True):
        two_d = bool(two_d and self.fold is not None)
        self.chrom_stack.setCurrentIndex(1 if two_d else 0)
        if self.win3d is not None:
            self.win3d.setVisible(two_d and self.act_3d.isChecked())
        self.peak_stack.setCurrentIndex(1 if two_d else 0)
        self.peak_hint.setText(self.HINT_2D if two_d else self.HINT_1D)
        (self.btn_view2d if two_d else self.btn_view1d).setChecked(True)
        if self.fold is not None:
            self.chrom_title.setText(self.chrom_title.text().split("   [raw")[0] + "   [raw, modulated]")
        self._set_mode(self.mode)

    def _source_changed(self, i):
        if self.d_path and i >= 0:
            pref = self.src_combo.itemData(i)
            self.settings.setValue("prefer", pref)
            self.open_path(self.d_path, prefer=pref)

    def _fill_info(self):
        r = self.run
        lines = [f"Run:        {r.name}", f"Folder:     {r.path}", f"Read from:  {r.source}",
                 f"Scans:      {r.n_scans}", f"RT range:   {r.times[0]:.3f} – {r.times[-1]:.3f} min",
                 f"Scan rate:  {1 / np.median(np.diff(r.times * 60)):.2f} scans/s" if r.n_scans > 1 else "",
                 f"m/z range:  {r.mz.min():.2f} – {r.mz.max():.2f}" if r.mz.size else "", ""]
        if self.fold is not None:
            f = self.fold
            lines += [f"GCxGC:      modulation {f.P:g} s, phase shift {f.phase:g} s",
                      f"            {f.n1} modulations × {f.n2} points per modulation", ""]
        for k, v in r.metadata.items():
            if not k.startswith("mz_") and v:
                lines.append(f"{k}: {v}")
        self.info.setPlainText("\n".join(x for x in lines if x is not None))

    # recent files
    def _recent_list(self):
        rec = self.settings.value("recent", []) or []
        return [rec] if isinstance(rec, str) else list(rec)

    def _add_recent(self, p):
        rec = [x for x in self._recent_list() if x != p]
        rec.insert(0, p)
        self.settings.setValue("recent", rec[:10])
        self._rebuild_recent()

    def _rebuild_recent(self):
        self.recent_menu.clear()
        rec = self._recent_list()
        for p in rec:
            self.recent_menu.addAction(p, lambda p=p: self.open_path(p))
        self.recent_menu.setEnabled(bool(rec))

    # drag & drop
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        urls = e.mimeData().urls()
        if urls:
            self.open_path(urls[0].toLocalFile())

    # ------------------------------------------------------------ trace
    def _eic_masses(self):
        txt = self.eic_edit.text().replace(";", ",")
        out = []
        for part in txt.split(","):
            part = part.strip()
            if part:
                try:
                    out.append(float(part))
                except ValueError:
                    pass
        return out

    def _eic_entered(self):
        if self.run is None:
            return
        if self._eic_masses():
            if self.trace_combo.currentIndex() != 1:
                self.trace_combo.setCurrentIndex(1)   # triggers update
            else:
                self._update_trace()

    def _trace_changed(self, _):
        if self.trace_combo.currentIndex() == 1 and not self._eic_masses():
            self.eic_edit.setFocus()
            self.status.showMessage("Type an m/z value and press Enter (or double-click a peak in the spectrum).", 6000)
        self._update_trace()

    def _current_trace(self):
        r = self.run
        if self.trace_combo.currentIndex() == 1:
            masses = self._eic_masses()
            if masses:
                tol = self.eic_tol.value()
                y = np.zeros(r.n_scans)
                for m in masses:
                    y += r.eic(m, tol)
                key = "EIC " + "+".join(f"{m:g}" for m in masses) + f" ±{tol:g}"
                return key, y
        return r.trace_name, r.tic

    def _update_trace(self, reset_view=False):
        if self.run is None:
            return
        self.trace_key, self.trace_y = self._current_trace()
        t = self.run.times
        self.chrom_curve.setData(t, self.trace_y)
        self.chrom_vb.set_full_x(t[0], t[-1])
        lab = self.trace_key if not self.trace_key.startswith("EIC ") else self.trace_key.replace("EIC ", "EIC m/z ")
        self.chrom_title.setText(f"{self.run.name}   ·   {lab}")
        if self.fold is not None:
            self.chrom_title.setText(self.chrom_title.text() + "   [raw, modulated]")
        if reset_view:
            self.chrom_vb.reset_view()
        else:
            self.chrom_vb.fit_y() if self.chrom_vb.auto_y else None
        self._redraw_peaks()
        if self.fold is not None:
            self.M = self.fold.matrix(self.trace_y)
            self.gxp.set_data(self.trace_y, self.M, self.fold, reset_view=reset_view)
            self.gxp.title.setText(f"{self.run.name}   ·   {lab}")
            self._redraw_blobs()
        else:
            self.M = None

    def _chrom_yrange(self, x0, x1):
        if self.trace_y is None:
            return None
        t = self.run.times
        m = (t >= x0) & (t <= x1)
        if not m.any():
            return None
        return float(self.trace_y[m].min()), float(self.trace_y[m].max())

    def reset_zoom(self):
        self.chrom_vb.reset_view()
        self.spec_vb.reset_view()
        if self.fold is not None:
            self.gxp.vb.reset_view()

    # ------------------------------------------------------------ mouse on chromatogram
    def _set_mode(self, k):
        self.mode = k
        self.mode_btns[k].setChecked(True)
        self.status.showMessage(MODES[k][1] + NAV_HINT)
        cur = QtCore.Qt.CrossCursor if k != "spectrum" else QtCore.Qt.ArrowCursor
        self.chrom_pw.setCursor(cur)
        self.gxp.pw.setCursor(QtCore.Qt.CrossCursor)
        if self.is2d():
            self.status.showMessage(MODES_2D[k] + NAV_HINT_2D)
        if k == "integrate":
            self.tabs.setCurrentIndex(0)

    def _chrom_hover(self, pos):
        if self.run is None:
            return
        if self.chrom_pw.sceneBoundingRect().contains(pos):
            p = self.chrom_vb.mapSceneToView(pos)
            i = self.run.scan_at(p.x())
            self.cursor_lbl.setText(f"RT {self.run.times[i]:.3f} min   scan {i + 1}   "
                                    f"{self.trace_key}: {self.trace_y[i]:,.0f}")

    def _chrom_click(self, x, _y):
        if self.run is None:
            return
        i = self.run.scan_at(x)
        if self.mode == "spectrum":
            self.select_scans(i)
        elif self.mode == "background":
            self.set_background(i, i)
        elif self.mode == "integrate":
            # a click on an integrated peak selects it
            for k, p in enumerate(self.peaks):
                if p.trace_key == self.trace_key and p.t_start <= x <= p.t_end:
                    self._select_peak_row(k)
                    return
            self.status.showMessage("Drag across a peak (from start to end) to integrate it.", 4000)

    def _chrom_drag(self, x0, x1, finished):
        if self.run is None:
            return
        a, b = sorted((x0, x1))
        if not finished:
            self.drag_region.setRegion((a, b))
            self.drag_region.show()
            if self.mode == "integrate":
                p = integ.integrate(self.run.times, self.trace_y, a, b, self.trace_key,
                                    self.base_combo.currentData(), self.snap.value())
                if p:
                    self.drag_base.setData([p.t_start, p.t_end], [p.b0, p.b1])
            return
        self.drag_region.hide()
        self.drag_base.setData([], [])
        i0, i1 = self.run.scan_at(a), self.run.scan_at(b)
        if self.mode == "spectrum":
            self.select_scans(i0, i1)
        elif self.mode == "background":
            self.set_background(i0, i1)
        elif self.mode == "integrate":
            p = integ.integrate(self.run.times, self.trace_y, a, b, self.trace_key,
                                self.base_combo.currentData(), self.snap.value())
            if p is None:
                self.status.showMessage("Integration range too short - drag across at least 3 scans.", 4000)
                return
            self.peaks.append(p)
            self._redraw_peaks()
            self._select_peak_row(len(self.peaks) - 1)

    # ------------------------------------------------------------ spectrum
    def select_scans(self, i0, i1=None):
        """Select one scan, or a contiguous range of scans (1D drag)."""
        i1 = i0 if i1 is None else i1
        i0, i1 = sorted((int(i0), int(i1)))
        t = self.run.times
        self.sel = (i0, i1)
        self.sel_idx = np.arange(i0, i1 + 1)
        self.sel_desc = self._sel_description((i0, i1))
        self.sel_tag = f"{t[i0]:.3f}min" if i0 == i1 else f"{t[i0]:.3f}-{t[i1]:.3f}min"
        if i0 == i1:
            self.sel_region.hide()
            self.sel_line.setValue(t[i0])
            self.sel_line.show()
            if self.fold is not None:
                self.gxp.show_point(*self.fold.coords_of_scan(i0))
                self.sel_tag = "%.3fmin_%.3fs" % self.fold.coords_of_scan(i0)
        else:
            self.sel_line.hide()
            self.sel_region.setRegion((t[i0], t[i1]))
            self.sel_region.show()
            if self.fold is not None:
                self.gxp.sel_point.setData([], [])
                self.gxp.sel_point2.setData([], [])
                self.gxp.sel_rect.hide()
        self._refresh_spectrum()

    def select_set(self, idx, desc, tag, rect=None):
        """Select an arbitrary set of scans (a GCxGC box or blob)."""
        idx = np.asarray(idx, dtype=np.int64)
        if idx.size == 0:
            self.status.showMessage("No scans in that area.", 3000)
            return
        self.sel = (int(idx[0]), int(idx[-1]))
        self.sel_idx, self.sel_desc, self.sel_tag = idx, desc, tag
        self.sel_region.hide()
        self.sel_line.setValue(self.run.times[idx[0]])
        self.sel_line.show()
        if rect is not None and self.fold is not None:
            self.gxp.show_sel_rect(*rect)
        self._refresh_spectrum()

    def step_scan(self, step):
        if self.run is None or not self.sel_idx.size:
            return
        if step in ("-mod", "+mod"):
            if self.fold is None:
                return
            step = self.fold.n2 * (1 if step == "+mod" else -1)
        i = int(np.clip(self.sel_idx[0] + step, 0, self.run.n_scans - 1))
        self.select_scans(i)

    def set_background(self, i0, i1):
        i0, i1 = sorted((i0, i1))
        self.bkg = (i0, i1)
        self.bkg_idx = np.arange(i0, i1 + 1)
        self.bkg_desc = self._sel_description((i0, i1))
        t = self.run.times
        w = max(t[i1] - t[i0], (t[-1] - t[0]) / 2000)
        self.bkg_region.setRegion((t[i0], t[i0] + w if i0 == i1 else t[i1]))
        self.bkg_region.show()
        if self.fold is not None:
            x, y = self.fold.coords_of_scan(i0)
            self.gxp.show_bkg_rect(x, x + self.fold.P / 60, y, y + self.fold.dt)
        self._bkg_changed()

    def set_background_set(self, idx, desc, rect):
        self.bkg_idx = np.asarray(idx, dtype=np.int64)
        self.bkg = (int(idx[0]), int(idx[-1]))
        self.bkg_desc = desc
        self.bkg_region.hide()
        self.gxp.show_bkg_rect(*rect)
        self._bkg_changed()

    def _bkg_changed(self):
        if not self.chk_bkg.isChecked():
            self.chk_bkg.setChecked(True)   # triggers refresh
        else:
            self._refresh_spectrum()

    def _sel_description(self, rng):
        i0, i1 = rng
        t = self.run.times
        if i0 == i1:
            if self.fold is not None:
                x, y = self.fold.coords_of_scan(i0)
                return f"scan {i0 + 1} @ ¹tR {x:.3f} min, ²tR {y:.2f} s"
            return f"scan {i0 + 1} @ {t[i0]:.3f} min"
        return f"average {t[i0]:.3f}–{t[i1]:.3f} min ({i1 - i0 + 1} scans)"

    def _compute_spectrum(self):
        idx = self.sel_idx
        desc = self.sel_desc
        grid = self.run.metadata.get("mz_grid", 0.01)
        if self.chk_bkg.isChecked() and self.bkg_idx is not None and self.bkg_idx.size:
            bkg = self.run.spectrum_scans(self.bkg_idx, bin_width=grid)
            mz, ab = self.run.spectrum_scans(idx, bin_width=grid)
            mz, ab = self.run.subtract((mz, ab), bkg)
            desc += f"  −  background {self.bkg_desc}"
        else:
            mz, ab = self.run.spectrum_scans(idx)
        return mz, ab, desc

    def _refresh_spectrum(self, *_, changed=True):
        if self.run is None or not self.sel_idx.size:
            return
        if not self.run.has_spectra:
            self.cur_spec = (np.zeros(0), np.zeros(0))
            i = int(self.sel_idx[0])
            where = self._sel_description((i, i)).replace(f"scan {i + 1} @ ", "")
            self.spec_title.setText(f"No mass spectra - {self.run.trace_name} detector data   ·   "
                                    f"{where}: {self.run.tic[i]:,.1f} {self.run.metadata.get('signal_unit', '')}")
            return
        mz, ab, desc = self._compute_spectrum()
        self.cur_spec = (mz, ab)
        self.cur_spec_desc = desc
        rel = self.chk_rel.isChecked()
        y = ab / ab.max() * 100 if (rel and ab.size and ab.max() > 0) else ab
        self.spec_y = y
        xs = np.repeat(mz, 2)
        ys = np.zeros(xs.size)
        ys[1::2] = y
        self.spec_curve.setData(xs, ys)
        self.spec_pw.setLabel("left", "Relative abundance (%)" if rel else "Abundance")
        base = f"   ·   base {mz[np.argmax(ab)]:.{self.run.metadata.get('mz_label_decimals', 1)}f}" if ab.size else ""
        self.spec_title.setText(f"{desc}{base}")
        self.spec_title.setToolTip(f"{desc}{base}")
        r = self.run
        lo = np.floor(r.mz.min() / 10) * 10 - 5 if r.mz.size else 0
        hi = np.ceil(r.mz.max() / 10) * 10 + 5 if r.mz.size else 100
        if self.spec_vb.full_x != (lo, hi):
            self.spec_vb.set_full_x(lo, hi)
            self.spec_vb.reset_view()
        elif self.spec_vb.auto_y:
            self.spec_vb.fit_y()
        if changed:
            self.lib_panel.on_spectrum_changed()   # re-search (Auto) or clear the mirror
        self._draw_mirror()
        self._label_spectrum()

    def _spec_yrange(self, x0, x1):
        mz = self.cur_spec[0]
        if mz.size == 0:
            return 0.0, 1.0
        m = (mz >= x0) & (mz <= x1)
        top = float(self.spec_y[m].max()) * 1.06 if m.any() else 1.0   # headroom for labels
        if self.mirror is not None:
            return -top * 1.08, top
        return 0.0, top

    # ---- library mirror (head-to-tail) plot
    def _library_query(self):
        if self.run is None or not self.sel_idx.size or self.cur_spec[0].size == 0:
            return None
        mz, ab = self.cur_spec
        return mz, ab, self.cur_spec_desc, float(np.floor(self.run.mz.min())) if self.run.mz.size else None

    def set_mirror(self, hit, spec):
        self.mirror = None if (hit is None or spec is None) else (hit, spec[0], spec[1])
        self._draw_mirror()
        if self.spec_vb.auto_y:
            self.spec_vb.fit_y()
        self._label_spectrum()

    def _mirror_scale(self):
        """Library intensities (0-999) -> plot units, matching the query's base peak height."""
        ref = float(self.spec_y.max()) if self.spec_y.size else 1.0
        return ref / 999.0

    def _draw_mirror(self):
        desc = self.cur_spec_desc
        base = self.spec_title.text().split("   ▼")[0] if self.spec_title.text() else desc
        if self.mirror is None or self.cur_spec[0].size == 0:
            self.mirror_curve.setData([], [])
            self.zero_line.hide()
            self.spec_title.setText(base)
            return
        hit, mz, ab = self.mirror
        y = -ab * self._mirror_scale()
        xs = np.repeat(mz, 2)
        ys = np.zeros(xs.size)
        ys[1::2] = y
        self.mirror_curve.setData(xs, ys)
        self.zero_line.show()
        self.spec_title.setText(f"{base}   ▼ <span style='color:#c82828'>{hit.name}  (match {hit.match})</span>")

    def _label_spectrum(self):
        for t in self.spec_labels:
            self.spec_pw.removeItem(t)
        self.spec_labels = []
        mz = self.cur_spec[0]
        if self.run is None or mz.size == 0:
            return
        x0, x1 = self.spec_vb.viewRange()[0]
        m = np.nonzero((mz >= x0) & (mz <= x1))[0]
        if m.size == 0:
            return
        dec = self.run.metadata.get("mz_label_decimals", 1)
        y = self.spec_y
        ymax = y[m].max()
        for k in px._pick_labels(mz[m], y[m], 14, (x1 - x0) * 0.03):
            j = m[k]
            if y[j] < 0.02 * ymax:
                continue
            ti = pg.TextItem(f"{mz[j]:.{dec}f}", color="#333333", anchor=(0.5, 1.0))
            ti.setPos(mz[j], y[j])
            self.spec_pw.addItem(ti, ignoreBounds=True)
            self.spec_labels.append(ti)
        if self.mirror is not None:
            _, lmz, lab = self.mirror
            lm = np.nonzero((lmz >= x0) & (lmz <= x1))[0]
            sc = self._mirror_scale()
            for k in px._pick_labels(lmz[lm], lab[lm], 8, (x1 - x0) * 0.04):
                j = lm[k]
                if lab[j] < 0.03 * 999:
                    continue
                ti = pg.TextItem(f"{lmz[j]:.0f}", color="#c82828", anchor=(0.5, 0.0))
                ti.setPos(lmz[j], -lab[j] * sc)
                self.spec_pw.addItem(ti, ignoreBounds=True)
                self.spec_labels.append(ti)

    def _spec_dblclick(self, x, _y):
        mz = self.cur_spec[0]
        if mz.size == 0:
            return
        x0, x1 = self.spec_vb.viewRange()[0]
        j = int(np.argmin(np.abs(mz - x)))
        if abs(mz[j] - x) > (x1 - x0) * 0.02:
            return
        val = round(mz[j], self.run.metadata.get("mz_label_decimals", 1))
        self.eic_edit.setText(f"{val:g}")
        self._eic_entered()
        self.status.showMessage(f"Showing EIC of m/z {val:g}", 4000)

    # ------------------------------------------------------------ peaks
    def _redraw_peaks(self):
        for it in self.peak_items:
            self.chrom_pw.removeItem(it)
        self.peak_items = []
        if self.run is None:
            self._fill_peak_table()
            return
        t, y = self.run.times, self.trace_y
        for k, p in enumerate(self.peaks):
            if p.trace_key != self.trace_key:
                continue
            seg_t = t[p.i0:p.i1 + 1]
            seg_y = y[p.i0:p.i1 + 1]
            base = p.baseline_at(seg_t)
            poly = QtGui.QPolygonF([QtCore.QPointF(a, b) for a, b in zip(seg_t, seg_y)]
                                   + [QtCore.QPointF(a, b) for a, b in zip(seg_t[::-1], base[::-1])])
            item = QtWidgets.QGraphicsPolygonItem(poly)
            item.setPen(pg.mkPen(None))
            item.setBrush(pg.mkBrush(*(COL_PEAK_SEL if k == self.sel_peak else COL_PEAK)))
            self.chrom_pw.addItem(item, ignoreBounds=True)
            bl = pg.PlotDataItem([p.t_start, p.t_end], [p.b0, p.b1], pen=pg.mkPen((192, 57, 43), width=1.3))
            self.chrom_pw.addItem(bl, ignoreBounds=True)
            ya = float(np.interp(p.rt_apex, t, y))
            lab = pg.TextItem(f"{k + 1}", color="#1f3b73", anchor=(0.5, 1.2))
            lab.setPos(p.rt_apex, ya)
            self.chrom_pw.addItem(lab, ignoreBounds=True)
            self.peak_items += [item, bl, lab]
        self._fill_peak_table()

    def _fill_peak_table(self):
        tbl = self.peak_table
        tbl.blockSignals(True)
        tbl.setRowCount(len(self.peaks))
        totals = {}
        for p in self.peaks:
            totals[p.trace_key] = totals.get(p.trace_key, 0.0) + p.area
        for r, p in enumerate(self.peaks):
            pct = 100 * p.area / totals[p.trace_key] if totals[p.trace_key] else 0
            vals = [f"{r + 1}", p.trace_key, f"{p.rt_apex:.3f}", f"{p.t_start:.3f}", f"{p.t_end:.3f}",
                    f"{p.height:,.0f}", f"{p.area:,.0f}", f"{pct:.2f}", p.note]
            for c, v in enumerate(vals):
                it = QtWidgets.QTableWidgetItem(v)
                if c != 8:
                    it.setFlags(it.flags() & ~QtCore.Qt.ItemIsEditable)
                if c in (0, 2, 3, 4, 5, 6, 7):
                    it.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                if p.trace_key != self.trace_key:
                    it.setForeground(QtGui.QColor("#999999"))
                tbl.setItem(r, c, it)
        tbl.blockSignals(False)

    def _select_peak_row(self, k):
        self.peak_table.selectRow(k)

    def _peak_selected(self):
        rows = sorted({i.row() for i in self.peak_table.selectedIndexes()})
        new = rows[0] if rows else None
        if new == self.sel_peak:
            return
        self.sel_peak = new
        if new is not None and self.run is not None:
            p = self.peaks[new]
            self._switch_to_trace(p.trace_key)
            self.select_scans(p.i0 + int(np.argmin(np.abs(self.run.times[p.i0:p.i1 + 1] - p.rt_apex))))
        self._redraw_peaks()
        if new is not None:
            self.peak_table.blockSignals(True)
            self.peak_table.selectRow(new)
            self.peak_table.blockSignals(False)

    def _switch_to_trace(self, key):
        """Show the TIC or EIC that a peak was integrated on."""
        if key == self.trace_key:
            return
        if not key.startswith("EIC "):
            self.trace_combo.setCurrentIndex(0)
            return
        masses, tol = key[4:].split(" ±")
        self.eic_edit.setText(", ".join(masses.split("+")))
        self.eic_tol.setValue(float(tol))
        if self.trace_combo.currentIndex() != 1:
            self.trace_combo.setCurrentIndex(1)
        else:
            self._update_trace()

    def _trace_signal(self, key):
        if key == self.trace_key:
            return self.trace_y
        if not key.startswith("EIC "):
            return self.run.tic
        masses, tol = key[4:].split(" ±")
        y = np.zeros(self.run.n_scans)
        for m in masses.split("+"):
            y += self.run.eic(float(m), float(tol))
        return y

    # ------------------------------------------------------------ GCxGC mouse
    def _gx_hover(self, x, y):
        if self.run is None or self.fold is None or self.M is None:
            return
        j, r = self.fold.pixel(x, y)
        i = self.fold.scan_map[j, r]
        self.cursor_lbl.setText(f"¹tR {float(self.fold.col_start_min(j)):.3f} min   ²tR {r * self.fold.dt:.2f} s   "
                                f"{'scan %d   ' % (i + 1) if i >= 0 else ''}{self.trace_key}: {self.M[j, r]:,.0f}")

    def _gx_click(self, x, y):
        if self.run is None or self.fold is None:
            return
        i = self.fold.scan_at(x, y)
        if self.mode == "spectrum":
            self.select_scans(i)
        elif self.mode == "background":
            self.set_background(i, i)
        elif self.mode == "integrate":
            for k, b in enumerate(self.blobs):
                x0, x1, y0, y1 = self.gxp.blob_rect(b)
                if b.trace_key == self.trace_key and x0 <= x <= x1 and y0 <= y <= y1:
                    self.blob_table.selectRow(k)
                    return
            self.status.showMessage("Drag a box around a peak to integrate it.", 4000)

    def _box_desc(self, j0, j1, r0, r1, n):
        f = self.fold
        return (f"box ¹tR {float(f.col_start_min(j0)):.2f}–{float(f.col_start_min(j1 + 1)):.2f} min, "
                f"²tR {r0 * f.dt:.2f}–{(r1 + 1) * f.dt:.2f} s ({n} scans)")

    def _gx_rect(self, x0, y0, x1, y1, finished):
        if self.run is None or self.fold is None:
            return
        if not finished:
            self.gxp.show_drag(x0, y0, x1, y1)
            return
        self.gxp.hide_drag()
        f = self.fold
        j0, j1, r0, r1 = f.box_pixels(x0, x1, y0, y1)
        rect = (float(f.col_start_min(j0)), float(f.col_start_min(j1 + 1)), r0 * f.dt, (r1 + 1) * f.dt)
        if self.mode == "integrate":
            b = gx.integrate_box(f, self.M, j0, j1, r0, r1, self.trace_key, self.base2d.currentData())
            if b is None:
                self.status.showMessage("Box too small - drag across at least 3 points in ²tR.", 4000)
                return
            self.blobs.append(b)
            self._redraw_blobs()
            self.blob_table.selectRow(len(self.blobs) - 1)
            return
        idx = f.box_scans(j0, j1, r0, r1)
        if idx.size == 0:
            return
        if idx.size == 1:
            return self._gx_click(x1, y1)
        desc = self._box_desc(j0, j1, r0, r1, idx.size)
        if self.mode == "spectrum":
            tag = f"box_{rect[0]:.2f}-{rect[1]:.2f}min_{rect[2]:.2f}-{rect[3]:.2f}s"
            self.select_set(idx, "average of " + desc, tag, rect)
        elif self.mode == "background":
            self.set_background_set(idx, desc, rect)

    # ------------------------------------------------------------ GCxGC blobs
    def _redraw_blobs(self):
        if self.fold is not None:
            self.gxp.draw_blobs(self.blobs, self.trace_key, self.sel_blob)
        self._fill_blob_table()

    def _fill_blob_table(self):
        t = self.blob_table
        t.blockSignals(True)
        t.setRowCount(len(self.blobs))
        totals = {}
        for b in self.blobs:
            totals[b.trace_key] = totals.get(b.trace_key, 0.0) + b.volume
        for r, b in enumerate(self.blobs):
            pct = 100 * b.volume / totals[b.trace_key] if totals[b.trace_key] else 0
            vals = [f"{r + 1}", b.trace_key, f"{b.rt1:.3f}", f"{b.rt2:.2f}", f"{b.height:,.0f}",
                    f"{b.volume:,.0f}", f"{pct:.2f}", f"{b.n_mod}", b.note, str(b.match) if b.match else ""]
            for c, v in enumerate(vals):
                it = QtWidgets.QTableWidgetItem(v)
                if c != 8:
                    it.setFlags(it.flags() & ~QtCore.Qt.ItemIsEditable)
                if c in (0, 2, 3, 4, 5, 6, 7, 9):
                    it.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                if b.trace_key != self.trace_key:
                    it.setForeground(QtGui.QColor("#999999"))
                if c == 8 and b.library:
                    it.setToolTip(f"Top hit from {b.library}")
                t.setItem(r, c, it)
        t.blockSignals(False)

    def _blob_selected(self):
        rows = sorted({i.row() for i in self.blob_table.selectedIndexes()})
        new = rows[0] if rows else None
        if new == self.sel_blob or self.run is None or self.fold is None:
            return
        self.sel_blob = new
        if new is not None:
            b = self.blobs[new]
            self._switch_to_trace(b.trace_key)
            idx = gx.blob_scans(self.fold, self.M, b)
            rect = self.gxp.blob_rect(b)
            self.select_set(idx, f"peak {new + 1}: {idx.size} scans ≥10% of apex "
                                 f"(¹tR {b.rt1:.2f} min, ²tR {b.rt2:.2f} s)", f"peak{new + 1}_{b.rt1:.2f}min", rect)
        self._redraw_blobs()
        if new is not None:
            self.blob_table.blockSignals(True)
            self.blob_table.selectRow(new)
            self.blob_table.blockSignals(False)

    def _blob_note_edited(self, item):
        if item.column() == 8 and item.row() < len(self.blobs):
            b = self.blobs[item.row()]
            b.note = item.text()
            b.library, b.match = "", 0
            self._fill_blob_table()

    def identify_blobs(self):
        if not self.blobs or self.run is None or self.fold is None:
            return
        rows = sorted({i.row() for i in self.blob_table.selectedIndexes()}) or list(range(len(self.blobs)))
        dlg = QtWidgets.QProgressDialog("Searching libraries…", "Cancel", 0, len(rows), self)
        dlg.setMinimumDuration(400)
        dlg.setWindowModality(QtCore.Qt.ApplicationModal)
        found = 0
        for n, r in enumerate(rows):
            if dlg.wasCanceled():
                break
            b = self.blobs[r]
            M = self.fold.matrix(self._trace_signal(b.trace_key)) if b.trace_key != self.trace_key else self.M
            idx = gx.blob_scans(self.fold, M, b)
            mz, ab = self.run.spectrum_scans(idx, bin_width=self.run.metadata.get("mz_grid", 0.01))
            hit = self.lib_panel.best_hit(mz, ab, f"peak {r + 1}")
            if hit is None and n == 0 and not self.lib_panel.has_libraries():
                break
            if hit is not None:
                if not b.note or b.library:      # don't overwrite names you typed yourself
                    b.note, b.library, b.match = hit.name, hit.library, hit.match
                found += 1
            dlg.setValue(n + 1)
            QtWidgets.QApplication.processEvents()
        dlg.close()
        self._fill_blob_table()
        self.status.showMessage(f"Identified {found} of {len(rows)} peaks from the open libraries", 6000)

    def _blob_rows(self):
        totals = {}
        for b in self.blobs:
            totals[b.trace_key] = totals.get(b.trace_key, 0.0) + b.volume
        f = self.fold
        header = ["Peak", "Trace", "1tR apex (min)", "2tR apex (s)", "1tR start (min)", "1tR end (min)",
                  "2tR start (s)", "2tR end (s)", "Baseline", f"Height ({self._area_unit()})", f"Volume ({self._area_unit()}·s)",
                  "Volume % (of trace)", "Modulations", "Name / ID", "Match", "Library"]
        rows = []
        for k, b in enumerate(self.blobs):
            rows.append([k + 1, b.trace_key, f"{b.rt1:.4f}", f"{b.rt2:.3f}",
                         f"{float(f.col_start_min(b.j0)):.4f}" if f else "", f"{float(f.col_start_min(b.j1 + 1)):.4f}" if f else "",
                         f"{b.r0 * f.dt:.3f}" if f else "", f"{(b.r1 + 1) * f.dt:.3f}" if f else "",
                         gx.BASELINES_2D[b.baseline], f"{b.height:.1f}", f"{b.volume:.1f}",
                         f"{100 * b.volume / totals[b.trace_key]:.3f}" if totals[b.trace_key] else "", b.n_mod,
                         b.note, b.match or "", b.library])
        return header, rows

    def _peak_note_edited(self, item):
        if item.column() == 8 and item.row() < len(self.peaks):
            self.peaks[item.row()].note = item.text()

    def delete_selected_peaks(self):
        if self.is2d():
            rows = sorted({i.row() for i in self.blob_table.selectedIndexes()}, reverse=True)
            for r in rows:
                del self.blobs[r]
            self.sel_blob = None
            self._redraw_blobs()
            return
        rows = sorted({i.row() for i in self.peak_table.selectedIndexes()}, reverse=True)
        for r in rows:
            del self.peaks[r]
        self.sel_peak = None
        self._redraw_peaks()

    def undo_peak(self):
        if self.is2d():
            if self.blobs:
                self.blobs.pop()
                self.sel_blob = None
                self._redraw_blobs()
                self.status.showMessage("Removed last 2D peak", 3000)
            return
        if self.peaks:
            self.peaks.pop()
            self.sel_peak = None
            self._redraw_peaks()
            self.status.showMessage("Removed last peak", 3000)

    def clear_peaks(self):
        if self.is2d():
            if self.blobs and QtWidgets.QMessageBox.question(self, APP_NAME, "Remove all integrated 2D peaks?") \
                    == QtWidgets.QMessageBox.Yes:
                self.blobs.clear()
                self.sel_blob = None
                self._redraw_blobs()
            return
        if self.peaks and QtWidgets.QMessageBox.question(self, APP_NAME, "Remove all integrated peaks?") \
                == QtWidgets.QMessageBox.Yes:
            self.peaks.clear()
            self.sel_peak = None
            self._redraw_peaks()

    def _peak_rows(self):
        totals = {}
        for p in self.peaks:
            totals[p.trace_key] = totals.get(p.trace_key, 0.0) + p.area
        header = ["Peak", "Trace", "RT apex (min)", "Start (min)", "End (min)", "Start scan", "End scan",
                  "Baseline", f"Height ({self._area_unit()})", f"Area ({self._area_unit()}·s)", "Area % (of trace)", "Name / note"]
        rows = []
        for k, p in enumerate(self.peaks):
            rows.append([k + 1, p.trace_key, f"{p.rt_apex:.4f}", f"{p.t_start:.4f}", f"{p.t_end:.4f}",
                         p.i0 + 1, p.i1 + 1, integ.BASELINES[p.baseline], f"{p.height:.1f}", f"{p.area:.1f}",
                         f"{100 * p.area / totals[p.trace_key]:.3f}" if totals[p.trace_key] else "", p.note])
        return header, rows

    def _report_rows(self):
        """Short table for reports."""
        u = self._area_unit()
        if self.is2d():
            h, rows = self._blob_rows()
            keep = [0, 2, 3, 10, 11, 13, 14]
            hh = ["Peak", "¹tR (min)", "²tR (s)", f"Volume ({u}·s)", "Volume %", "Compound / note", "Match"]
            return hh, [[r[i] for i in keep] for r in rows]
        totals = {}
        for p in self.peaks:
            totals[p.trace_key] = totals.get(p.trace_key, 0.0) + p.area
        multi = len(totals) > 1
        h = ["Peak"] + (["Trace"] if multi else []) + ["Retention time (min)", f"Area ({u}·s)", "Area %", "Compound / note"]
        rows = []
        for k, p in enumerate(self.peaks):
            rows.append([k + 1] + ([p.trace_key] if multi else []) +
                        [f"{p.rt_apex:.3f}", f"{p.area:.4g}" if p.area < 1e4 else f"{p.area:.0f}",
                         f"{100 * p.area / totals[p.trace_key]:.2f}" if totals[p.trace_key] else "", p.note])
        return h, rows

    def copy_peaks(self):
        if not (self.blobs if self.is2d() else self.peaks):
            self.status.showMessage("No peaks integrated yet", 3000)
            return
        h, rows = self._report_rows()
        title = f"{self.run.name} – {'GCxGC ' if self.is2d() else ''}integrated peaks"
        tables.copy_table(h, rows, title)
        self.status.showMessage("Table copied – paste into Word/PowerPoint (formatted) or Excel", 4000)

    # ------------------------------------------------------------ exports
    def _save_name(self, title, default, filt):
        start = os.path.join(self.settings.value("export_dir", os.path.dirname(self.d_path or "~")), default)
        fn, _ = QtWidgets.QFileDialog.getSaveFileName(self, title, start, filt)
        if fn:
            self.settings.setValue("export_dir", os.path.dirname(fn))
        return fn

    def _safe(self, s):
        return "".join(c if c.isalnum() or c in "._-+" else "_" for c in s).strip("_")

    def _trace_tag(self):
        return self._safe(self.trace_key.replace(" ±", "_tol")) if self.trace_key.startswith("EIC ") else self.trace_key

    def _done(self, fn):
        self.status.showMessage(f"Saved {fn}", 6000)

    def export_chrom_csv(self):
        if self.run is None:
            return
        fn = self._save_name("Export chromatogram", f"{self.run.name}_{self._trace_tag()}.csv", "CSV (*.csv)")
        if not fn:
            return
        with open(fn, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Scan", "Retention time (min)", self.trace_key])
            for i, (t, y) in enumerate(zip(self.run.times, self.trace_y)):
                w.writerow([i + 1, f"{t:.5f}", f"{y:.0f}" if float(y).is_integer() else f"{y:.3f}"])
        self._done(fn)

    def export_spec_csv(self):
        if self.run is None or not self.sel_idx.size:
            return
        mz, ab = self.cur_spec
        tag = self.sel_tag
        if self.chk_bkg.isChecked() and self.bkg_idx is not None:
            tag += "_bkgsub"
        fn = self._save_name("Export spectrum", f"{self.run.name}_MS_{tag}.csv", "CSV (*.csv)")
        if not fn:
            return
        dec = self.run.metadata.get("mz_decimals", 2)
        mx = ab.max() if ab.size else 1
        with open(fn, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["m/z", "Abundance", "Relative abundance (%)"])
            for m, a in zip(mz, ab):
                w.writerow([f"{m:.{dec}f}", f"{a:.0f}" if float(a).is_integer() else f"{a:.2f}", f"{100 * a / mx:.3f}"])
        self._done(fn)

    def export_2d_csv(self):
        if self.run is None or self.fold is None:
            return
        fn = self._save_name("Export 2D matrix", f"{self.run.name}_2D_{self._trace_tag()}.csv", "CSV (*.csv)")
        if not fn:
            return
        f, M = self.fold, self.M
        with open(fn, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["1tR (min) \\ 2tR (s)"] + [f"{r * f.dt:.3f}" for r in range(f.n2)])
            for j in range(f.n1):
                w.writerow([f"{float(f.col_start_min(j)):.4f}"] +
                           [f"{v:.0f}" if float(v).is_integer() else f"{v:.2f}" for v in M[j]])
        self._done(fn)

    def export_2d_image(self):
        if self.run is None or self.fold is None:
            return
        lab = self.trace_key if not self.trace_key.startswith("EIC ") else self.trace_key.replace("EIC ", "EIC m/z ")
        fn, opts, use_view = self._image_target("2d", f"{self.run.name} – {lab}",
                                                f"{self.run.name}_2D_{self._trace_tag()}")
        if not fn:
            return
        (xa, xb), (ya, yb) = self.gxp.vb.viewRange()
        blobs = []
        if opts.pop("show_blobs", False):
            for k, b in enumerate(self.blobs):
                if b.trace_key == self.trace_key:
                    blobs.append((*self.gxp.blob_rect(b), str(k + 1)))
        lo, hi = self.gxp.levels()
        try:
            px.gcxgc_figure(fn, self.gxp.Mdisp, self.fold.extent, xlim=(xa, xb) if use_view else None,
                            ylim=(ya, yb) if use_view else None, cmap=self.gxp.mpl_cmap(),
                            scale=self.gxp.scale_name, levels=(lo, hi), blobs=blobs, **opts)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, APP_NAME, f"Export failed:\n{e}")
            return
        self._done(fn)

    # ------------------------------------------------------------ 3D view / models
    def _show_3d(self, on):
        self.settings.setValue("3d/show", str(bool(on)).lower())
        if self.win3d is not None:
            self.win3d.setVisible(on)
        else:
            self.holder3d.setVisible(on)
        if on:
            self.view3d.schedule()

    def toggle_3d_popout(self):
        if self.win3d is None:                       # pop out
            self.view3d.drop_view()
            self.win3d = view3d.Pop3DWindow(f"3D view - {self.run.name if self.run else ''}")
            self.win3d.lay.addWidget(self.view3d)
            self.win3d.closed.connect(self._dock_3d)
            self.holder3d.hide()
            self.view3d.btn_pop.setText("Dock")
            self.win3d.show()
            self.view3d.restore_view()
        else:
            self.win3d.close()                       # -> _dock_3d

    def _dock_3d(self):
        if self.win3d is None:
            return
        w, self.win3d = self.win3d, None
        self.view3d.drop_view()
        self.holder3d.layout().addWidget(self.view3d)
        self.view3d.btn_pop.setText("Pop out")
        self.holder3d.setVisible(self.act_3d.isChecked())
        self.view3d.restore_view()
        w.deleteLater()

    def export_3d_model(self):
        if self.run is None or self.fold is None or self.gxp.T is None:
            QtWidgets.QMessageBox.information(self, APP_NAME, "3D models are made from GCxGC data - "
                                                              "open a GCxGC file (or set a modulation period) first.")
            return
        dlg = Model3DDialog(self, self.settings)
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return
        o = dlg.options()
        ext = {"glb": ".glb", "fbx": ".fbx", "stl": ".stl"}[o["fmt"]]
        stem = f"{self.run.name}_{self._trace_tag()}_mod{self.fold.P:g}s"
        fn = self._save_name("Save 3D model", stem + ext, {"glb": "glTF binary (*.glb)", "fbx": "FBX (*.fbx)",
                                                          "stl": "STL (*.stl)"}[o["fmt"]])
        if not fn:
            return
        if not fn.lower().endswith(ext):
            fn += ext
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            stl = o["fmt"] == "stl"
            width_m = o["width"] / 1000.0 if stl else o["width"]
            m, info = view3d.region_mesh(self.gxp, full=o["full"], max1=o["max1"], max2=o["max2"], width=width_m,
                                         stretched=o["stretched"], match_screen=o["match"], watertight=stl,
                                         height_scale=o["height"])
            name = self._safe(stem)
            data = me.to_glb(m, name, "GC-MS Viewer") if o["fmt"] == "glb" else \
                me.to_fbx(m, name, "GC-MS Viewer") if o["fmt"] == "fbx" else me.to_stl(m, name)
            with open(fn, "wb") as fh:
                fh.write(data)
        except Exception as e:
            QtWidgets.QApplication.restoreOverrideCursor()
            QtWidgets.QMessageBox.critical(self, APP_NAME, f"3D export failed:\n{e}")
            return
        QtWidgets.QApplication.restoreOverrideCursor()
        self.status.showMessage(f"Saved {fn}  ·  {len(m['I']) // 3:,} triangles  ·  {len(data) / 1e6:.1f} MB", 8000)

    def copy_spectrum(self):
        mz, ab = self.cur_spec
        dec = self.run.metadata.get("mz_decimals", 2)
        QtWidgets.QApplication.clipboard().setText("\n".join(f"{m:.{dec}f}\t{a:.0f}" for m, a in zip(mz, ab)))
        self.status.showMessage("Spectrum copied (m/z <tab> abundance)", 3000)

    def export_peaks_csv(self):
        two_d = self.is2d()
        if not (self.blobs if two_d else self.peaks):
            QtWidgets.QMessageBox.information(self, APP_NAME, "No peaks integrated yet.")
            return
        h, rows = self._blob_rows() if two_d else self._peak_rows()
        meta = [("Run", self.run.name), ("Source", self.run.source)]
        if two_d:
            meta += [("GCxGC", f"modulation {self.fold.P:g} s, phase shift {self.fold.phase:g} s"),
                     ("Volume units", f"{self._area_unit()} x seconds (sum above per-modulation baseline x scan interval)")]
        else:
            meta += [("Area units", f"{self._area_unit()} x seconds (trapezoidal, above baseline)")]
        fn = tables.export_table(self, self.settings, h, rows, meta,
                                 f"{self.run.name}_{'2D_' if two_d else ''}peaks",
                                 os.path.dirname(self.d_path or "~"), sheet="Peaks")
        if fn:
            self._done(fn)

    def _image_target(self, kind, default_title, default_stem, mirror_name=None):
        dlg = ImageExportDialog(self, kind, default_title, self.settings, mirror_name)
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return None, None, None
        opts, use_view = dlg.options()
        self._img_mirror = getattr(dlg, "include_mirror", False)
        filt = {".png": "PNG (*.png)", ".tif": "TIFF (*.tif *.tiff)", ".svg": "SVG (*.svg)", ".pdf": "PDF (*.pdf)"}[dlg.ext]
        fn = self._save_name("Save image", default_stem + dlg.ext, filt)
        if fn and not os.path.splitext(fn)[1]:
            fn += dlg.ext
        return fn, opts, use_view

    def export_chrom_image(self):
        if self.run is None:
            return
        lab = self.trace_key if not self.trace_key.startswith("EIC ") else self.trace_key.replace("EIC ", "EIC m/z ")
        fn, opts, use_view = self._image_target("chrom", f"{self.run.name} – {lab}",
                                                f"{self.run.name}_{self._trace_tag()}")
        if not fn:
            return
        xlim = tuple(self.chrom_vb.viewRange()[0]) if use_view else None
        peaks = [p for p in self.peaks if p.trace_key == self.trace_key]
        try:
            px.chromatogram_figure(fn, self.run.times, self.trace_y, xlim=xlim, peaks=peaks,
                                   ylabel=self.run.signal_label, **opts)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, APP_NAME, f"Export failed:\n{e}")
            return
        self._done(fn)

    def export_spec_image(self):
        if self.run is None or not self.sel_idx.size:
            return
        tag = self.sel_tag
        mname = f"{self.mirror[0].name} (match {self.mirror[0].match}, {self.mirror[0].library})" \
            if self.mirror is not None else None
        fn, opts, use_view = self._image_target("spec", f"{self.run.name} – {self.cur_spec_desc}",
                                                f"{self.run.name}_MS_{tag}", mname)
        if not fn:
            return
        xlim = tuple(self.spec_vb.viewRange()[0]) if use_view else None
        mz, ab = self.cur_spec
        try:
            mirror = None
            if self._img_mirror and self.mirror is not None:
                mirror = (mname, self.mirror[1], self.mirror[2])
            px.spectrum_figure(fn, mz, ab, xlim=xlim, decimals=self.run.metadata.get("mz_label_decimals", 1),
                               mirror=mirror, **opts)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, APP_NAME, f"Export failed:\n{e}")
            return
        self._done(fn)

    # ------------------------------------------------------------ help
    def show_help(self):
        QtWidgets.QMessageBox.information(self, "Controls", """
<b>Mouse modes</b> (toolbar, or keys 1 / 2 / 3)<br>
<b>1 Spectrum</b>: click the chromatogram for that scan's spectrum; drag to average a range.<br>
<b>2 Integrate</b>: drag from peak start to peak end. Ctrl+Z undoes; Delete removes selected rows.<br>
<b>3 Background</b>: click/drag a quiet region, then tick <i>Subtract background</i>.<br><br>
<b>Zooming</b> (both plots)<br>
Mouse wheel: zoom the x-axis around the cursor (y rescales automatically)<br>
Ctrl + wheel: zoom intensity only<br>
Right-drag: draw a zoom box<br>
Middle-drag: pan<br>
Right-click: step back out; Home: full view<br><br>
<b>Other</b><br>
← / → (with the chromatogram focused): previous / next scan<br>
Double-click a peak in the spectrum: plot its extracted ion chromatogram (EIC)<br>
F5: search open libraries · F6: send the spectrum to NIST MS Search<br>
EIC box accepts several m/z values separated by commas (summed)<br>
Drag a .D folder or .cdf file onto the window to open it.<br><br>
<b>GCxGC</b> (netCDF exports, e.g. ChromSpace)<br>
Modulation period is detected from the file name / signal; change it (and the phase shift) in the second toolbar<br>
Ctrl+2 / Ctrl+1: 2D view / raw 1D view<br>
Wheel zooms both axes (Shift: ¹tR only, Ctrl: ²tR only); right-drag draws a zoom box<br>
Drag a box: average spectrum (Spectrum mode), integrate a blob (Integrate mode), background (Background mode)<br>
← / →: previous / next modulation, ↑ / ↓: next / previous scan<br>
Colour bar handles (right of the plot) set the noise floor and saturation; Colours ▸ Edit… for schemes,
smoothing and display baseline<br>
3D view (beside the plot): left-drag rotate, wheel zoom, middle-drag pan; Pop out for its own window; Ctrl+3 show/hide
""")


def main():
    QtCore.QCoreApplication.setOrganizationName("UoP")
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    w = MainWindow()
    w.show()
    if len(sys.argv) > 1:
        QtCore.QTimer.singleShot(0, lambda: w.open_path(sys.argv[1]))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
