"""
LC-MS window: the chromatography window adapted for high-resolution LC-MS
(Thermo .raw, mzML, mzXML): polarity / MS-level selector, ppm extracted-ion chromatograms,
exact-mass calculator for adducts, 4-decimal spectra.
"""
from __future__ import annotations

import os
import traceback

import numpy as np
from PySide6 import QtCore, QtWidgets

import gcms_viewer as gv
import lcms
from common import APP_NAME


class FormulaDialog(QtWidgets.QDialog):
    """Formula + adduct -> exact m/z; plots the XIC and checks the spectrum shown for a match."""

    def __init__(self, win: "LCMSWindow"):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Exact m/z from a formula")
        f = QtWidgets.QFormLayout(self)
        self.formula = QtWidgets.QLineEdit(win.settings.value("lc/formula", ""))
        self.formula.setPlaceholderText("e.g. C22H30N2O2S  or a neutral monoisotopic mass")
        self.adduct = QtWidgets.QComboBox()
        pol = win.run.metadata.get("polarity", "+") if win.run is not None else "+"
        for a in lcms.ADDUCTS:
            if (pol == "-") == a.endswith("-"):
                self.adduct.addItem(a)
        for a in lcms.ADDUCTS:
            if self.adduct.findText(a) < 0:
                self.adduct.addItem(a)
        self.out = QtWidgets.QLabel("")
        self.out.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.out.setWordWrap(True)
        f.addRow("Formula (neutral M)", self.formula)
        f.addRow("Ion", self.adduct)
        f.addRow(self.out)
        bb = QtWidgets.QDialogButtonBox()
        self.b_plot = bb.addButton("Plot XIC", QtWidgets.QDialogButtonBox.AcceptRole)
        bb.addButton(QtWidgets.QDialogButtonBox.Close)
        bb.accepted.connect(self._plot)
        bb.rejected.connect(self.reject)
        f.addRow(bb)
        self.formula.textChanged.connect(self._update)
        self.adduct.currentIndexChanged.connect(self._update)
        self.mz = None
        self._update()

    def _update(self, *_):
        txt = self.formula.text().strip()
        if not txt:
            self.out.setText("Type a molecular formula.")
            self.mz = None
            return
        try:
            try:
                M = float(txt)
            except ValueError:
                M = lcms.mono_mass(txt)
            mz = lcms.adduct_mz(M, self.adduct.currentText())
        except Exception as e:
            self.out.setText(f"<span style='color:#c0392b'>{e}</span>")
            self.mz = None
            return
        self.mz = mz
        msg = f"Monoisotopic M = <b>{M:.5f}</b> Da<br>{self.adduct.currentText()}: <b>m/z {mz:.5f}</b>"
        spec = getattr(self.win, "cur_spec", None)
        if spec is not None and len(spec[0]):
            smz, sab = spec
            near = np.abs(smz - mz) <= max(0.02, mz * 20e-6)
            if near.any():
                j = np.nonzero(near)[0][np.argmax(sab[near])]
                err = lcms.ppm_error(smz[j], mz)
                rel = 100 * sab[j] / sab.max()
                msg += (f"<br>In the spectrum shown: m/z {smz[j]:.5f} ({rel:.1f} % of base peak), "
                        f"<b>{err:+.1f} ppm</b>")
            else:
                msg += "<br>Not in the spectrum shown (within 20 ppm)."
        self.out.setText(msg)

    def _plot(self):
        if self.mz is None:
            return
        self.win.settings.setValue("lc/formula", self.formula.text())
        self.win.eic_edit.setText(f"{self.mz:.5f}")
        self.win._eic_entered()
        self.win.status.showMessage(f"XIC of {self.formula.text()} {self.adduct.currentText()} "
                                    f"m/z {self.mz:.5f} ± {self.win.eic_tol.value():g} ppm", 8000)


class LCMSWindow(gv.MainWindow):
    TECHNIQUE = "LC-MS"

    def __init__(self):
        self.lc = None
        self._lc_path = None
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} – LC-MS")
        self.act_open.setText("Open LC-MS…")
        self.act_open.setToolTip("Open a Thermo .raw, mzML or mzXML file (Ctrl+O)")
        self.act_open_cdf.setVisible(False)
        self.tb2.setVisible(False)
        self.src_combo.setToolTip("Polarity and MS level: files run in both +ve and −ve mode (or with MS/MS) "
                                  "contain several sets of scans – pick the one to show")
        self.eic_tol.blockSignals(True)
        self.eic_tol.setRange(0.1, 1000)
        self.eic_tol.setDecimals(1)
        self.eic_tol.setSingleStep(1)
        self.eic_tol.setSuffix(" ppm")
        self.eic_tol.setValue(float(self.settings.value("lc/ppm", 5)))
        self.eic_tol.setToolTip("Extracted-ion window in ppm of the m/z (5 ppm at m/z 300 = ±0.0015)")
        self.eic_tol.valueChanged.connect(lambda v: self.settings.setValue("lc/ppm", v))
        self.eic_tol.blockSignals(False)
        self.eic_edit.setPlaceholderText("m/z, e.g. 387.2097")
        # EI libraries don't apply to ESI spectra
        k = self.tabs.indexOf(self.lib_panel)
        if k >= 0:
            self.tabs.removeTab(k)
        self.lib_panel.on_spectrum_changed = lambda: None
        for a in self.menuBar().actions():
            if a.text().replace("&", "") == "Library":
                a.setVisible(False)
        tb = self.findChildren(QtWidgets.QToolBar)[0]
        tb.addSeparator()
        act = tb.addAction("m/z from formula…", self.formula_dialog)
        act.setShortcut("Ctrl+F")
        act.setToolTip("Exact m/z of [M+H]+, [M−H]−, [M+Na]+ … from a formula; plots its XIC (Ctrl+F)")

    # ------------------------------------------------------------ opening
    def open_dialog(self):
        start = self.settings.value("lc_dir", os.path.expanduser("~"))
        fn, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Open LC-MS data", start,
                                                      "LC-MS (*.raw *.RAW *.mzML *.mzml *.mzXML *.mzxml);;All files (*)")
        if fn:
            self.settings.setValue("lc_dir", os.path.dirname(fn))
            self.open_path(fn)

    def open_cdf_dialog(self):
        self.open_dialog()

    def open_path(self, path, prefer=None):
        low = path.lower()
        if not low.endswith((".raw", ".mzml", ".mzxml")):
            return super().open_path(path, prefer)
        path = os.path.abspath(path)
        if self.lc is None or self._lc_path != path:
            prog = QtWidgets.QProgressDialog(f"Reading {os.path.basename(path)}…", None, 0, 0, self)
            prog.setWindowModality(QtCore.Qt.WindowModal)
            prog.setMinimumDuration(400)
            QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
            try:
                self.lc = lcms.read_any(path, progress=lambda k, n: (prog.setMaximum(n), prog.setValue(k),
                                                                     QtWidgets.QApplication.processEvents()))
                self._lc_path = path
            except Exception as e:
                QtWidgets.QApplication.restoreOverrideCursor()
                prog.close()
                msg = str(e)
                if "opentfraw" in msg or isinstance(e, ImportError):
                    msg += "\n\nReading Thermo .raw files needs the 'opentfraw' package (pip install opentfraw)."
                QtWidgets.QMessageBox.critical(self, APP_NAME, f"Could not read {path}\n\n{msg}\n\n"
                                                               f"{traceback.format_exc(limit=3)}")
                return
            QtWidgets.QApplication.restoreOverrideCursor()
            prog.close()
        segs = self.lc.segments()
        if not segs:
            QtWidgets.QMessageBox.warning(self, APP_NAME, "No scans in this file.")
            return
        keys = [s[0] for s in segs]
        if prefer is None or prefer not in keys:
            saved = self.settings.value("lc/segment", "")
            prefer = next((k for k in keys if f"{k[0]}{k[1]}" == saved), keys[0])
        self.settings.setValue("lc/segment", f"{prefer[0]}{prefer[1]}")
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            run = lcms.make_run(self.lc, prefer)
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        same_file = self.d_path == path and self.run is not None and \
            self.run.metadata.get("polarity") == prefer[0] and self.run.metadata.get("ms_level") == prefer[1]
        self.run, self.d_path = run, path
        self.settings.setValue(self._fold_key(), "0|0")          # never treat LC-MS as GCxGC
        self._add_recent(path)
        self.src_combo.blockSignals(True)
        self.src_combo.clear()
        for key, label, n in segs:
            self.src_combo.addItem(label, key)
        self.src_combo.setCurrentIndex(keys.index(prefer))
        self.src_combo.setEnabled(len(segs) > 1)
        self.src_combo.blockSignals(False)
        self._after_load(run, same_file)
        if len(segs) > 1:
            self.status.showMessage("This file has " + ", ".join(s[1].split(" · ")[0] + " " + s[1].split(" · ")[1]
                                                                 for s in segs) +
                                    " – switch with the Data box", 10000)

    def _source_changed(self, i):
        if self.d_path and i >= 0 and self.lc is not None:
            self.open_path(self.d_path, prefer=self.src_combo.itemData(i))

    # ------------------------------------------------------------ traces
    def _current_trace(self):
        r = self.run
        if self.trace_combo.currentIndex() == 1:
            masses = self._eic_masses()
            if masses:
                tol = self.eic_tol.value()
                y = np.zeros(r.n_scans)
                for m in masses:
                    y += r.eic(m, tol)
                return "EIC " + "+".join(f"{m:.4f}" for m in masses) + f" ±{tol:g}", y
        return r.trace_name, r.tic

    def _update_trace(self, reset_view=False):
        super()._update_trace(reset_view)
        if self.trace_key.startswith("EIC "):
            self.chrom_title.setText(self.chrom_title.text() + " ppm")

    def _fill_info(self):
        super()._fill_info()
        if self.lc is None or self.run is None:
            return
        extra = [f"Scans in file: {self.lc.rt.size}  ({', '.join(s[1] for s in self.lc.segments())})"]
        for k, v in self.lc.meta.items():
            if v:
                extra.append(f"{k}: {v}")
        filters = self.run.metadata.get("filters") or []
        if filters:
            extra.append("Scan filter: " + " | ".join(filters))
        self.info.setPlainText(self.info.toPlainText() + "\n" + "\n".join(extra))

    def formula_dialog(self):
        if self.run is None:
            self.status.showMessage("Open a file first", 3000)
            return
        FormulaDialog(self).exec()
