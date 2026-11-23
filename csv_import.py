"""
Import any delimited text / CSV of numbers: the user picks the columns and says what the data are.
The result is routed to the right window (GC, GCxGC, NMR, FT-IR, or the general x,y window).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np
from PySide6 import QtCore, QtWidgets

import spectra as S

TYPES = [
    # key, label, needs (x, y[, z]) description, target window
    ("chrom", "Chromatogram (time, signal) – GC, HPLC…", "x = time, y = signal"),
    ("gcxgc_trace", "GCxGC – single raw trace (time, signal)", "x = time, y = signal; give the modulation period"),
    ("gcxgc_xyz", "GCxGC – x, y, z (¹tR, ²tR, intensity)", "x = ¹tR, y = ²tR, z = intensity"),
    ("ir", "FT-IR spectrum", "x = wavenumber, y = %T or absorbance"),
    ("raman", "Raman spectrum", "x = Raman shift, y = intensity"),
    ("uvvis", "UV-Vis spectrum", "x = wavelength, y = absorbance"),
    ("fluor", "Fluorescence spectrum", "x = wavelength, y = intensity"),
    ("ms", "Mass spectrum (m/z, intensity)", "x = m/z, y = intensity"),
    ("nmr", "NMR spectrum (ppm, intensity)", "x = δ (ppm), y = intensity"),
    ("xrd", "XRD pattern (2θ, intensity)", "x = 2θ, y = intensity"),
    ("generic", "Something else (x, y)", "any x and one or more y columns"),
]
TYPE_KEYS = [t[0] for t in TYPES]


@dataclass
class ImportResult:
    kind: str
    x: np.ndarray
    ys: list            # list of (name, y) – several for overlays
    z: np.ndarray | None
    name: str
    path: str
    x_label: str = ""
    y_label: str = ""
    time_unit: str = "min"
    period_s: float = 0.0
    ir_mode: str = "T%"
    nmr_nucleus: str = "1H"
    nmr_mhz: float = 0.0


class ImportDialog(QtWidgets.QDialog):
    def __init__(self, parent, path, settings: QtCore.QSettings, forced_kind=None):
        super().__init__(parent)
        self.path = path
        self.s = settings
        self.setWindowTitle(f"Import {os.path.basename(path)}")
        self.resize(900, 640)
        self.table = S.sniff_table(path)
        t = self.table
        v = QtWidgets.QVBoxLayout(self)
        info = QtWidgets.QLabel(f"Found <b>{t.data.shape[0]:,}</b> rows × <b>{t.data.shape[1]}</b> columns "
                                f"(separator: <b>{ {',': 'comma', ';': 'semicolon', chr(9): 'tab', ' ': 'spaces'}[t.delimiter] }</b>, "
                                f"decimal: <b>{'comma' if t.decimal == ',' else 'point'}</b>"
                                + (f", {t.skipped} header line{'s' if t.skipped != 1 else ''} skipped" if t.skipped else "")
                                + ")")
        v.addWidget(info)
        prev = QtWidgets.QTableWidget(min(12, t.data.shape[0]), t.data.shape[1])
        prev.setHorizontalHeaderLabels(t.columns)
        prev.verticalHeader().hide()
        prev.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        for r in range(prev.rowCount()):
            for c in range(t.data.shape[1]):
                val = t.data[r, c]
                prev.setItem(r, c, QtWidgets.QTableWidgetItem("" if np.isnan(val) else f"{val:g}"))
        prev.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        prev.setMaximumHeight(240)
        v.addWidget(prev)
        if t.raw_header:
            hdr = QtWidgets.QLabel("Header: " + " | ".join(h.strip() for h in t.raw_header[-3:])[:300])
            hdr.setStyleSheet("color:#666;")
            hdr.setWordWrap(True)
            v.addWidget(hdr)

        form = QtWidgets.QFormLayout()
        self.kind = QtWidgets.QComboBox()
        for key, label, _ in TYPES:
            self.kind.addItem(label, key)
        sig = self._signature()
        saved = self.s.value(f"import/{sig}")
        guess = forced_kind or (saved.split("|")[0] if saved else None)
        if guess is None:
            x0 = t.data[:, 0]
            guess = S.guess_kind(x0, t.data[:, 1] if t.data.shape[1] > 1 else x0, t.columns, os.path.basename(path))
            if guess == "chrom" and t.data.shape[1] >= 3 and self._looks_xyz():
                guess = "gcxgc_xyz"
        self.kind.setCurrentIndex(TYPE_KEYS.index(guess) if guess in TYPE_KEYS else len(TYPES) - 1)
        form.addRow("What is this data?", self.kind)
        self.help = QtWidgets.QLabel("")
        self.help.setStyleSheet("color:#555;")
        form.addRow("", self.help)

        self.xcol = QtWidgets.QComboBox()
        self.ycols = QtWidgets.QListWidget()
        self.ycols.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.ycols.setMaximumHeight(90)
        self.zcol = QtWidgets.QComboBox()
        for c in t.columns:
            self.xcol.addItem(c)
            self.ycols.addItem(c)
            self.zcol.addItem(c)
        n = len(t.columns)
        self.xcol.setCurrentIndex(0)
        self.ycols.item(min(1, n - 1)).setSelected(True)
        self.zcol.setCurrentIndex(min(2, n - 1))
        if saved:
            try:
                _, xs, ys, zs = saved.split("|")
                self.xcol.setCurrentIndex(int(xs))
                self.ycols.clearSelection()
                for k in ys.split(","):
                    if k:
                        self.ycols.item(int(k)).setSelected(True)
                self.zcol.setCurrentIndex(int(zs))
            except Exception:
                pass
        form.addRow("x column", self.xcol)
        self.ylab = QtWidgets.QLabel("y column(s)")
        form.addRow(self.ylab, self.ycols)
        self.zlab = QtWidgets.QLabel("z column (intensity)")
        form.addRow(self.zlab, self.zcol)

        self.time_unit = QtWidgets.QComboBox()
        self.time_unit.addItems(["minutes", "seconds"])
        self.tlab = QtWidgets.QLabel("Time is in")
        form.addRow(self.tlab, self.time_unit)
        self.period = QtWidgets.QDoubleSpinBox()
        self.period.setRange(0, 60)
        self.period.setDecimals(2)
        self.period.setSuffix(" s")
        self.period.setValue(float(self.s.value("import/period", 4.0)))
        self.plab = QtWidgets.QLabel("Modulation period")
        form.addRow(self.plab, self.period)
        self.irmode = QtWidgets.QComboBox()
        self.irmode.addItems(["Transmittance (%)", "Transmittance (0–1)", "Absorbance"])
        self.irlab = QtWidgets.QLabel("y values are")
        form.addRow(self.irlab, self.irmode)
        self.nuc = QtWidgets.QComboBox()
        self.nuc.addItems(["1H", "13C", "19F", "31P", "other"])
        self.mhz = QtWidgets.QDoubleSpinBox()
        self.mhz.setRange(0, 1500)
        self.mhz.setSuffix(" MHz")
        self.mhz.setToolTip("Spectrometer frequency for this nucleus (e.g. 400 for ¹H on a 400 MHz magnet, "
                            "101 for ¹³C on the same magnet) – needed to give J in Hz")
        self.mhz.setValue(float(self.s.value("import/mhz", 400)))
        hn = QtWidgets.QHBoxLayout()
        hn.addWidget(self.nuc)
        hn.addWidget(self.mhz)
        self.nlab = QtWidgets.QLabel("<b>Nucleus / spectrometer MHz</b>")
        self.nwrap = QtWidgets.QWidget()
        self.nwrap.setLayout(hn)
        form.addRow(self.nlab, self.nwrap)
        self.xname = QtWidgets.QLineEdit()
        self.yname = QtWidgets.QLineEdit()
        self.xname_lab = QtWidgets.QLabel("x axis label")
        self.yname_lab = QtWidgets.QLabel("y axis label")
        form.addRow(self.xname_lab, self.xname)
        form.addRow(self.yname_lab, self.yname)
        self.remember = QtWidgets.QCheckBox("Remember these choices for files with the same columns")
        self.remember.setChecked(True)
        form.addRow("", self.remember)
        v.addLayout(form)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.button(QtWidgets.QDialogButtonBox.Ok).setText("Open")
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self._kind_changed()
        self.result = None

    def _signature(self):
        t = self.table
        return "_".join(c.lower().replace(" ", "")[:12] for c in t.columns)[:80] + f"_{t.data.shape[1]}"

    def _looks_xyz(self):
        x = self.table.data[:, 0]
        x = x[np.isfinite(x)]
        return x.size > 100 and np.unique(x).size < x.size / 5

    def _kind_changed(self, *_):
        k = self.kind.currentData()
        self.help.setText(dict((a, c) for a, _, c in TYPES)[k])
        xyz = k == "gcxgc_xyz"
        for w in (self.zcol, self.zlab):
            w.setVisible(xyz)
        self.ylab.setText("y column (²tR)" if xyz else ("y column(s) – select several to overlay"
                                                         if k in ("ir", "raman", "uvvis", "fluor", "generic", "xrd")
                                                         else "y column"))
        for w in (self.time_unit, self.tlab):
            w.setVisible(k in ("chrom", "gcxgc_trace", "gcxgc_xyz"))
        self.tlab.setText("¹tR is in" if xyz else "Time is in")
        for w in (self.period, self.plab):
            w.setVisible(k == "gcxgc_trace")
        for w in (self.irmode, self.irlab):
            w.setVisible(k == "ir")
        if k == "ir":
            y = self._first_y()
            if y is not None:
                mx, md = np.nanmax(y), np.nanmedian(y)
                self.irmode.setCurrentIndex(0 if mx > 5 else (1 if md > 0.4 else 2))
        for w in (self.nwrap, self.nlab):
            w.setVisible(k == "nmr")
        gen = k in ("generic", "xrd", "fluor", "uvvis", "raman")
        for w in (self.xname, self.yname, self.xname_lab, self.yname_lab):
            w.setVisible(gen)
        if gen:
            lab = S.KINDS.get(k, S.KINDS["generic"])
            self.xname.setText(self.xcol.currentText() if k == "generic" else lab[1])
            self.yname.setText(self.ycols.selectedItems()[0].text() if (k == "generic" and self.ycols.selectedItems())
                               else lab[2])

    def _first_y(self):
        rows = self._ysel()
        return self.table.data[:, rows[0]] if rows else None

    def _ysel(self):
        return sorted(self.ycols.row(i) for i in self.ycols.selectedItems())

    def _accept(self):
        k = self.kind.currentData()
        t = self.table
        xi = self.xcol.currentIndex()
        yis = self._ysel()
        if not yis:
            QtWidgets.QMessageBox.information(self, "Import", "Choose at least one y column.")
            return
        x = t.data[:, xi]
        good = np.isfinite(x)
        if k == "gcxgc_xyz":
            zi = self.zcol.currentIndex()
            y = t.data[:, yis[0]]
            z = t.data[:, zi]
            good &= np.isfinite(y) & np.isfinite(z)
            res = ImportResult(k, x[good], [(t.columns[yis[0]], y[good])], z[good],
                               os.path.splitext(os.path.basename(self.path))[0], self.path,
                               time_unit="s" if self.time_unit.currentIndex() == 1 else "min")
        else:
            ys = []
            for yi in yis if k in ("ir", "raman", "uvvis", "fluor", "generic", "xrd") else yis[:1]:
                y = t.data[:, yi]
                ok = good & np.isfinite(y)
                ys.append((t.columns[yi], (x[ok], y[ok])))
            res = ImportResult(k, x[good], ys, None, os.path.splitext(os.path.basename(self.path))[0], self.path,
                               x_label=self.xname.text(), y_label=self.yname.text(),
                               time_unit="s" if self.time_unit.currentIndex() == 1 else "min",
                               period_s=self.period.value(), ir_mode=["T%", "T", "A"][self.irmode.currentIndex()],
                               nmr_nucleus=self.nuc.currentText(), nmr_mhz=self.mhz.value())
        if self.remember.isChecked():
            self.s.setValue(f"import/{self._signature()}",
                            f"{k}|{xi}|{','.join(map(str, yis))}|{self.zcol.currentIndex()}")
        if k == "gcxgc_trace":
            self.s.setValue("import/period", self.period.value())
        if k == "nmr":
            self.s.setValue("import/mhz", self.mhz.value())
        self.result = res
        self.accept()


# -----------------------------------------------------------------------------
# turning an import into the objects each window understands
# -----------------------------------------------------------------------------
def to_run(res: ImportResult):
    """Chromatogram / GCxGC import -> gcms_reader.GCMSRun (single channel). Returns (run, period_s)."""
    import gcms_reader as gr
    period = None
    if res.kind == "gcxgc_xyz":
        x1 = np.asarray(res.x, float)
        x2 = np.asarray(res.ys[0][1], float)
        z = np.asarray(res.z, float)
        if res.time_unit == "s":
            x1 = x1 / 60.0
        o = np.lexsort((x2, x1))
        x1, x2, z = x1[o], x2[o], z[o]
        u2 = np.unique(x2)
        dt2 = float(np.median(np.diff(u2))) if u2.size > 1 else 0.01
        period = float(u2.max() - u2.min() + dt2)
        times = x1 + (x2 - u2.min()) / 60.0          # absolute time (min)
        y = z
    else:
        x, y = res.ys[0][1]
        times = np.asarray(x, float) / (60.0 if res.time_unit == "s" else 1.0)
        o = np.argsort(times)
        times, y = times[o], np.asarray(y, float)[o]
        if res.kind == "gcxgc_trace":
            period = res.period_s or None
    z0 = np.zeros(0)
    meta = {"detector": "Signal", "signal": res.ys[0][0], "signal_unit": "", "mz_grid": 1.0,
            "mz_decimals": 2, "mz_label_decimals": 0}
    run = gr.GCMSRun(path=res.path, name=res.name, source=f"Text import ({os.path.basename(res.path)})",
                     times=times, mz=z0, intensity=z0.copy(), offsets=np.zeros(len(times) + 1, dtype=np.int64),
                     metadata=meta, stored_tic=np.asarray(y, float))
    return run, period


def to_spectra(res: ImportResult):
    out = []
    kind = res.kind if res.kind in S.KINDS else "generic"
    for name, (x, y) in res.ys:
        sp = S.Spectrum(x=x, y=y, kind=kind, name=res.name if len(res.ys) == 1 else f"{res.name} – {name}",
                        path=res.path, source="Text import",
                        x_label=res.x_label if kind in ("generic", "xrd", "fluor", "uvvis", "raman") else "",
                        y_label=res.y_label if kind in ("generic", "xrd", "fluor", "uvvis", "raman") else "",
                        y_mode=res.ir_mode if kind == "ir" else "raw")
        out.append(sp)
    return out


def to_nmr(res: ImportResult):
    import nmr
    x, y = res.ys[0][1]
    sp = nmr.from_xy(x, y, name=res.name, nucleus=res.nmr_nucleus if res.nmr_nucleus != "other" else None,
                     sf=res.nmr_mhz)
    sp.path = res.path
    return sp
