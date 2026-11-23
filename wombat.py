#!/usr/bin/env python3
"""
WombatGC – analytical data viewer for teaching and research.

Start screen: pick the kind of data (GC-MS / GC-FID / GCxGC / NMR / FT-IR / LC-MS / CSV) or just
open a file and let the app work out what it is.

Run:  python wombat.py  [optional file or folder]
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import traceback

from PySide6 import QtCore, QtGui, QtWidgets

import common
from common import APP_NAME, __version__

TECHNIQUES = [
    # key, title, description, module, class
    ("gc", "GC-MS · GC-FID", "Agilent .D (ChemStation / MassHunter), netCDF (.cdf)\n"
                            "TIC/EIC, spectra, library search, integration", "gcms_viewer", "MainWindow"),
    ("gcxgc", "GCxGC", "ChromSpace / ANDI netCDF exports\n2D and 3D plots, blob integration, 3D models",
     "gcms_viewer", "MainWindow"),
    ("nmr", "NMR", "Bruker folders or zips, TopSpin ASCII, JCAMP-DX\nIntegrals, multiplets & J, ¹³C/DEPT, reports",
     "nmr_view", "NMRWindow"),
    ("ir", "FT-IR", "Bruker OPUS (.0, .1 …), JCAMP-DX, CSV\nPeak labels and data tables",
     "spectrum_view", "IRWindow"),
    ("lcms", "LC-MS", "Thermo .raw, mzML / mzXML\n+ve / −ve mode, exact-mass XICs, spectra",
     "lcms_view", "LCMSWindow"),
    ("csv", "CSV / text data", "Any x,y (or x,y,z) columns – you choose\nthe columns and the kind of data",
     "spectrum_view", "XYWindow"),
    ("compare", "Compare samples", "Up to 5 chromatograms or spectra side by side;\nclick a peak to find it in every sample",
     "compare_view", "CompareWindow"),
]


def detect(path: str):
    """Guess the technique for a file or folder. Returns a key from TECHNIQUES or None."""
    p = path.rstrip("/\\")
    low = p.lower()
    if low.endswith(".d") and os.path.isdir(p):
        return "gc"
    if low.endswith((".cdf", ".nc")):
        return "gc"
    if low.endswith((".raw", ".mzml", ".mzxml")):
        return "lcms"
    try:
        import nmr
        if nmr.looks_like_nmr(p):
            return "nmr"
    except Exception:
        pass
    if low.endswith((".jdx", ".dx", ".jcamp")):
        try:
            head = open(p, encoding="latin-1").read(4000).upper()
            if "INFRARED" in head or "1/CM" in head or "IR SPECTRUM" in head:
                return "ir"
            if "MASS" in head:
                return "csv"
        except OSError:
            pass
        return "ir"
    if os.path.isfile(p):
        ext = os.path.splitext(low)[1]
        if ext[1:].isdigit():                               # OPUS .0 .1 .2 …
            return "ir"
        if ext in (".csv", ".txt", ".tsv", ".asc", ".dat", ".xy", ".prn"):
            try:
                import nmr
                nmr.read_topspin_ascii(p)
                with open(p, encoding="latin-1") as f:
                    first = [next(f, "") for _ in range(3)]
                if all(len([v for v in l.replace(",", " ").split() if v]) == 4 for l in first if l.strip()):
                    return "nmr"
            except Exception:
                pass
            return "csv"
    if os.path.isdir(p):
        try:
            if any(n.lower().endswith(".d") for n in os.listdir(p)):
                return "gc"
        except OSError:
            pass
    return None


class StartWindow(QtWidgets.QWidget):
    def __init__(self, app_ctl):
        super().__init__()
        self.ctl = app_ctl
        self.setWindowTitle(APP_NAME)
        self.setAcceptDrops(True)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(28, 22, 28, 18)
        head = QtWidgets.QLabel(f"<span style='font-size:22pt;font-weight:600'>{APP_NAME}</span>"
                                f"<span style='font-size:11pt;color:#666'>&nbsp;&nbsp;analytical data viewer · v{__version__}</span>")
        v.addWidget(head)
        sub = QtWidgets.QLabel("What would you like to look at? You can also drag a file or folder onto this window.")
        sub.setStyleSheet("color:#444;font-size:10.5pt;")
        v.addWidget(sub)
        v.addSpacing(8)
        grid = QtWidgets.QGridLayout()
        grid.setSpacing(10)
        for k, (key, title, desc, mod, cls) in enumerate(TECHNIQUES):
            avail = self.ctl.available(mod)
            b = self._card(title, desc, avail)
            if not avail:
                b.setToolTip("Not installed in this version")
            b.clicked.connect(lambda _=False, kk=key: self.ctl.open_technique(kk))
            grid.addWidget(b, k // 2, k % 2)
        any_btn = self._card("Open any file…", "The app works out what kind of data it is", True, accent=True)
        any_btn.clicked.connect(self.open_any)
        n = len(TECHNIQUES)
        grid.addWidget(any_btn, n // 2, n % 2)
        v.addLayout(grid, 1)
        v.addSpacing(6)
        rec_lab = QtWidgets.QLabel("<b>Recent</b>")
        v.addWidget(rec_lab)
        self.recent = QtWidgets.QListWidget()
        self.recent.setMaximumHeight(130)
        self.recent.itemActivated.connect(lambda it: self.ctl.open_path(it.data(QtCore.Qt.UserRole)))
        v.addWidget(self.recent)
        foot = QtWidgets.QLabel("Double-click a recent file to open it again.")
        foot.setStyleSheet("color:#777;")
        v.addWidget(foot)
        self.refresh_recent()

    @staticmethod
    def _card(title, desc, enabled=True, accent=False):
        b = QtWidgets.QPushButton()
        b.setEnabled(enabled)
        b.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        b.setMinimumSize(260, 84)
        b.setCursor(QtCore.Qt.PointingHandCursor)
        bg, border, hov = ("#e6edf9", "#9fb3d9", "#d7e2f6") if accent else ("#f6f8fc", "#c9cfdb", "#e8eef9")
        b.setStyleSheet(f"QPushButton{{border:1px solid {border};border-radius:8px;background:{bg};}}"
                        f"QPushButton:hover{{background:{hov};border-color:#7d93c4}}"
                        "QPushButton:disabled{background:#f3f3f3;border-color:#ddd}")
        lay = QtWidgets.QVBoxLayout(b)
        lay.setContentsMargins(14, 8, 14, 8)
        col = "#1f3b73" if accent else ("#222" if enabled else "#aaa")
        sub = "#555" if enabled else "#bbb"
        lab = QtWidgets.QLabel(f"<div style='font-size:11.5pt;font-weight:600;color:{col}'>{title}</div>"
                               f"<div style='font-size:9.5pt;color:{sub}'>{desc.replace(chr(10), '<br>')}</div>")
        lab.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        lab.setStyleSheet("background:transparent;border:none")
        lay.addWidget(lab)
        return b

    def refresh_recent(self):
        self.recent.clear()
        for p in self.ctl.recent():
            q = p.rstrip("/\\")
            name = os.path.basename(q)
            if name.isdigit():                       # Bruker experiment number: show its parent too
                name = f"{os.path.basename(os.path.dirname(q))}/{name}"
            it = QtWidgets.QListWidgetItem(f"{name}    —    {os.path.dirname(p)}")
            it.setData(QtCore.Qt.UserRole, p)
            if not os.path.exists(p):
                it.setForeground(QtGui.QColor("#aaa"))
            self.recent.addItem(it)

    def open_any(self):
        s = common.settings()
        dlg = QtWidgets.QFileDialog(self, "Open a data file (choose a folder with the button below for .D / Bruker)",
                                    s.value("open_dir", os.path.expanduser("~")))
        dlg.setFileMode(QtWidgets.QFileDialog.ExistingFile)
        dlg.setNameFilters(["All data (*)", "GC (*.cdf *.ch *.ms)", "NMR (*.zip *.jdx *.txt)",
                            "FT-IR (*.0 *.1 *.2 *.jdx *.dx)", "LC-MS (*.raw *.mzML *.mzXML)",
                            "Text (*.csv *.txt *.tsv)"])
        folder_btn = QtWidgets.QPushButton("Choose a folder instead…")
        chosen = {}

        def pick_folder():
            d = QtWidgets.QFileDialog.getExistingDirectory(self, "Choose a .D folder or Bruker experiment folder",
                                                           s.value("open_dir", os.path.expanduser("~")))
            if d:
                chosen["p"] = d
                dlg.reject()
        folder_btn.clicked.connect(pick_folder)
        lay = dlg.layout()
        if isinstance(lay, QtWidgets.QGridLayout):
            dlg.setOption(QtWidgets.QFileDialog.DontUseNativeDialog, True)
            lay.addWidget(folder_btn, lay.rowCount(), 0, 1, -1)
        if dlg.exec() and dlg.selectedFiles():
            chosen["p"] = dlg.selectedFiles()[0]
        p = chosen.get("p")
        if not p:
            return
        s.setValue("open_dir", os.path.dirname(p))
        base = os.path.basename(p)
        if base in ("fid", "acqus", "1r", "procs") or base.upper() in ("DATA.MS", "MSPEAK.BIN"):
            p = os.path.dirname(p)
        self.ctl.open_path(p)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        for u in e.mimeData().urls():
            if u.toLocalFile():
                self.ctl.open_path(u.toLocalFile())


class AppController(QtCore.QObject):
    """Keeps one start screen and any number of technique windows."""

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.windows = []
        self.start = StartWindow(self)
        self.start.resize(820, 640)

    # --------------------------------------------------------------- helpers
    @staticmethod
    def available(module):
        try:
            return importlib.util.find_spec(module) is not None
        except Exception:
            return False

    def recent(self):
        r = common.settings().value("recent_all", []) or []
        return [r] if isinstance(r, str) else list(r)

    def add_recent(self, p):
        s = common.settings()
        r = [x for x in self.recent() if x != p]
        s.setValue("recent_all", ([p] + r)[:12])
        self.start.refresh_recent()

    def show_start(self):
        self.start.refresh_recent()
        self.start.show()
        self.start.raise_()
        self.start.activateWindow()

    # --------------------------------------------------------------- windows
    def _make(self, key):
        spec = next(t for t in TECHNIQUES if t[0] == key)
        mod = importlib.import_module(spec[3])
        w = getattr(mod, spec[4])()
        if hasattr(w, "sigHome"):
            w.sigHome.connect(self.show_start)
        if hasattr(w, "sigCompare"):
            w.sigCompare.connect(self.open_compare)
        if hasattr(w, "sigImport"):
            w.sigImport.connect(lambda res, src=w: self.route_import(res, src))
        w.destroyed.connect(lambda *_: self._forget(w))
        w.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
        self.windows.append(w)
        w.show()
        return w

    def _forget(self, w):
        self.windows = [x for x in self.windows if x is not w]
        try:
            if not self.windows and not self.start.isVisible():
                self.show_start()
        except RuntimeError:
            pass

    def open_technique(self, key):
        try:
            w = self._make(key)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self.start, APP_NAME, f"Could not start this module:\n{e}\n\n"
                                                                 f"{traceback.format_exc(limit=3)}")
            return None
        self.start.hide()
        # ask for a file straight away – that's what everyone does next
        opener = {"gc": "open_dialog", "gcxgc": "open_cdf_dialog", "nmr": "open_dialog", "ir": "open_dialog",
                  "lcms": "open_dialog", "csv": "open_dialog"}.get(key)
        if opener and hasattr(w, opener):
            QtCore.QTimer.singleShot(150, getattr(w, opener))
        return w

    def open_path(self, path):
        key = detect(path)
        if key is None:
            QtWidgets.QMessageBox.information(self.start, APP_NAME,
                                              f"I couldn't tell what kind of data this is:\n{path}\n\n"
                                              "Pick the technique on the start screen and open it from there.")
            return None
        try:
            w = self._make(key)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self.start, APP_NAME, f"Could not start this module:\n{e}")
            return None
        self.start.hide()
        self.add_recent(path)
        QtCore.QTimer.singleShot(0, lambda: w.open_path(path))
        return w

    def route_import(self, res, source=None):
        """A CSV import that belongs in another window (chromatogram, GCxGC, NMR, IR, x,y)."""
        import csv_import as ci
        try:
            if res.kind in ("chrom", "gcxgc_trace", "gcxgc_xyz"):
                run, period = ci.to_run(res)
                w = self._make("gcxgc" if period else "gc")
                w.open_run(run, period)
            elif res.kind == "nmr":
                w = self._make("nmr")
                w.set_spectrum(ci.to_nmr(res))
            else:
                w = self._make("ir" if res.kind == "ir" else "csv")
                sps = ci.to_spectra(res)
                w.set_spectrum(sps[0])
                for sp in sps[1:]:
                    w.add_overlay(sp)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self.start, APP_NAME, f"Could not open the imported data:\n{e}\n\n"
                                                                 f"{traceback.format_exc(limit=3)}")
            return
        self.add_recent(res.path)
        if source is not None and getattr(source, "main", 1) is None:
            source.close()            # the empty window the import started from

    def open_compare(self, paths=()):
        try:
            w = self._make("compare")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self.start, APP_NAME, f"Could not start the comparison view:\n{e}")
            return None
        for p in paths or []:
            if p:
                QtCore.QTimer.singleShot(0, lambda pp=p: w.add_path(pp))
        return w


def resource(name):
    """Path of a file shipped with the app (works from source and in the installed app)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def selftest(ctl, app):
    """Used by the installer builds: open every window (and any files given), then quit.
    Exit code 0 = OK. Results are printed and written to wombatgc-selftest.log (windowed
    builds on Windows have no console)."""
    log = open("wombatgc-selftest.log", "w", encoding="utf-8")

    def print(*a, **k):                      # noqa: A001 - also log to file
        import builtins
        builtins.print(*a, **k)
        log.write(" ".join(str(x) for x in a) + "\n")
        log.flush()
    ok = True
    for key, *_ in TECHNIQUES:
        try:
            w = ctl._make(key)
            for _ in range(20):
                app.processEvents()
            w.close()
            app.processEvents()
            print(f"selftest {key}: ok", flush=True)
        except Exception:
            ok = False
            print(f"selftest {key}: FAILED\n{traceback.format_exc()}", flush=True)
    for p in [a for a in sys.argv[1:] if not a.startswith("-")]:
        try:
            w = ctl.open_path(p)
            for _ in range(60):
                app.processEvents()
            data = getattr(w, "run", None) or getattr(w, "sp", None) or getattr(w, "main", None) \
                or getattr(w, "samples", None)
            print(f"selftest open {os.path.basename(p)}: {'ok' if data else 'NOTHING LOADED'} "
                  f"({type(w).__name__})", flush=True)
            ok &= bool(data)
            w.close()
        except Exception:
            ok = False
            print(f"selftest open {p}: FAILED\n{traceback.format_exc()}", flush=True)
    for mod in ("nmrglue", "openpyxl", "opentfraw", "brukeropusreader", "rainbow", "scipy.signal", "matplotlib.pyplot"):
        try:
            importlib.import_module(mod)
            print(f"selftest import {mod}: ok", flush=True)
        except Exception as e:
            ok = False
            print(f"selftest import {mod}: FAILED {e}", flush=True)
    return 0 if ok else 1


def main():
    QtCore.QCoreApplication.setOrganizationName("UoP")
    QtWidgets.QApplication.setHighDpiScaleFactorRoundingPolicy(
        QtCore.Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    if os.path.exists(resource("icon.png")):
        app.setWindowIcon(QtGui.QIcon(resource("icon.png")))
    ctl = AppController(app)
    if "--selftest" in sys.argv:
        sys.exit(selftest(ctl, app))
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        if ctl.open_path(args[0]) is None:
            ctl.show_start()
    else:
        ctl.show_start()
    app._ctl = ctl
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
