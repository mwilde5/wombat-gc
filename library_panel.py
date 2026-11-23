"""
library_panel.py - the "Library search" tab: open-library (MSP) search, NIST MS Search
hand-off, hit table, and web links.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import sys
import urllib.request
import zipfile

from PySide6 import QtCore, QtGui, QtWidgets

import ms_library as ml
import nist_search as ns

MASSBANK_API = "https://api.github.com/repos/MassBank/MassBank-data/releases?per_page=15"
# last release known to carry the NIST-format MSP under this name (checked Nov 2026)
MASSBANK_FALLBACK = "https://github.com/MassBank/MassBank-data/releases/download/2024.11/MassBank_NIST.msp"
MASSBANK_PAGE = "https://github.com/MassBank/MassBank-data/releases/latest"
MONA_PAGE = "https://mona.fiehnlab.ucdavis.edu/downloads"
IS_WINDOWS = sys.platform.startswith("win")


def app_data_dir():
    d = QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.AppDataLocation)
    if not d:
        d = os.path.join(os.path.expanduser("~"), ".gcms_viewer")
    os.makedirs(d, exist_ok=True)
    return d


def latest_massbank_msp():
    """
    Ask GitHub for recent MassBank-data releases and return (url, filename, tag) of the newest
    NIST-flavour MSP asset (plain, .gz or .zip).  Falls back to the 2024.11 file.
    """
    def score(name):
        n = name.lower()
        if ".msp" not in n and not n.endswith(".zip"):
            return -1
        if "riken" in n:
            return 0
        return 3 if "nist" in n else (2 if ".msp" in n else -1)
    try:
        req = urllib.request.Request(MASSBANK_API, headers={"User-Agent": "GCMS-Viewer",
                                                            "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            releases = json.load(r)
        for rel in releases:
            assets = sorted(rel.get("assets", []), key=lambda a: -score(a["name"]))
            if assets and score(assets[0]["name"]) > 0:
                a = assets[0]
                return a["browser_download_url"], a["name"], rel.get("tag_name", "")
    except Exception:
        pass
    return MASSBANK_FALLBACK, "MassBank_NIST.msp", "2024.11"


def unpack_if_needed(path):
    """Decompress .gz / .zip downloads; returns the path of the .msp file."""
    low = path.lower()
    if low.endswith(".gz"):
        out = path[:-3]
        with gzip.open(path, "rb") as fi, open(out, "wb") as fo:
            shutil.copyfileobj(fi, fo)
        os.remove(path)
        return out
    if low.endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".msp")]
            if not names:
                raise ValueError("The downloaded zip contains no .msp file")
            pick = next((n for n in names if "nist" in n.lower()), names[0])
            out = os.path.join(os.path.dirname(path), os.path.basename(pick))
            with z.open(pick) as fi, open(out, "wb") as fo:
                shutil.copyfileobj(fi, fo)
        os.remove(path)
        return out
    return path


def open_url(url):
    QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))


# =============================================================================
class LibrarySettingsDialog(QtWidgets.QDialog):
    def __init__(self, panel: "LibraryPanel"):
        super().__init__(panel)
        self.panel = panel
        self.setWindowTitle("Spectral libraries")
        self.resize(640, 520)
        v = QtWidgets.QVBoxLayout(self)

        g1 = QtWidgets.QGroupBox("Open libraries (MSP files, searched inside this app)")
        l1 = QtWidgets.QVBoxLayout(g1)
        self.list = QtWidgets.QListWidget()
        self.list.itemChanged.connect(self._toggled)
        l1.addWidget(self.list)
        h = QtWidgets.QHBoxLayout()
        b_add = QtWidgets.QPushButton("Add MSP file…")
        b_add.clicked.connect(self._add)
        b_rm = QtWidgets.QPushButton("Remove")
        b_rm.clicked.connect(self._remove)
        b_mb = QtWidgets.QPushButton("Download MassBank")
        b_mb.setToolTip("Downloads MassBank_NIST.msp (≈100k+ spectra, of which ≈13k are EI) from the MassBank GitHub releases")
        b_mb.clicked.connect(self._download_massbank)
        b_mona = QtWidgets.QPushButton("Get MoNA GC-MS…")
        b_mona.setToolTip("Opens the MoNA downloads page. Download the GC-MS spectra as MSP, then use 'Add MSP file…'")
        b_mona.clicked.connect(lambda: open_url(MONA_PAGE))
        for b in (b_add, b_rm, b_mb, b_mona):
            h.addWidget(b)
        l1.addLayout(h)
        tip = QtWidgets.QLabel("Any NIST-format .msp file works, including your own in-house libraries. "
                               "Each file is indexed once (a few seconds per 100k spectra) and cached.")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#555;")
        l1.addWidget(tip)
        v.addWidget(g1, 1)

        g2 = QtWidgets.QGroupBox("Search options")
        f2 = QtWidgets.QFormLayout(g2)
        s = panel.settings
        self.ei_only = QtWidgets.QCheckBox("EI spectra only (skip LC-MS/MS records)")
        self.ei_only.setChecked(s.value("lib/ei_only", "true") == "true")
        self.low_cut = QtWidgets.QCheckBox("Ignore library peaks below the run's lowest scanned m/z")
        self.low_cut.setChecked(s.value("lib/low_cut", "true") == "true")
        self.low_cut.setToolTip("e.g. if you scanned from m/z 50, library peaks at m/z 15-49 don't count against the match")
        self.nhits = QtWidgets.QSpinBox()
        self.nhits.setRange(5, 200)
        self.nhits.setValue(int(s.value("lib/nhits", 25)))
        f2.addRow(self.ei_only)
        f2.addRow(self.low_cut)
        f2.addRow("Hits to show", self.nhits)
        v.addWidget(g2)

        g3 = QtWidgets.QGroupBox("NIST MS Search (installed on this PC)")
        l3 = QtWidgets.QVBoxLayout(g3)
        h3 = QtWidgets.QHBoxLayout()
        self.nist_dir = QtWidgets.QLineEdit(panel.nist_dir() or "")
        self.nist_dir.setPlaceholderText(r"e.g. C:\NIST20\MSSEARCH")
        b_browse = QtWidgets.QPushButton("Browse…")
        b_browse.clicked.connect(self._browse_nist)
        b_detect = QtWidgets.QPushButton("Auto-detect")
        b_detect.clicked.connect(self._detect_nist)
        h3.addWidget(self.nist_dir, 1)
        h3.addWidget(b_browse)
        h3.addWidget(b_detect)
        l3.addLayout(h3)
        info = QtWidgets.QLabel(
            "The folder containing <b>nistms$.exe</b>. For hits to come back into this app, switch on "
            "automatic searching once in NIST: <i>Options ▸ Library Search Options ▸ Automation</i> tab ▸ "
            "tick <b>Automatic Search On</b>. Without it, NIST still opens with your spectrum loaded.")
        info.setWordWrap(True)
        info.setStyleSheet("color:#555;")
        l3.addWidget(info)
        if not IS_WINDOWS:
            g3.setEnabled(False)
            g3.setTitle(g3.title() + " - Windows only")
        v.addWidget(g3)

        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok)
        bb.accepted.connect(self.accept)
        v.addWidget(bb)
        self._fill()

    def _fill(self):
        self.list.blockSignals(True)
        self.list.clear()
        for e in self.panel.lib_entries:
            lib = self.panel.loaded.get(e["path"])
            n = f"   ({len(lib):,} spectra, {lib.n_ei:,} EI)" if lib else ""
            missing = "" if os.path.exists(e["path"]) else "   [file not found]"
            it = QtWidgets.QListWidgetItem(f"{os.path.basename(e['path'])}{n}{missing}")
            it.setToolTip(e["path"])
            it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
            it.setCheckState(QtCore.Qt.Checked if e["enabled"] else QtCore.Qt.Unchecked)
            self.list.addItem(it)
        self.list.blockSignals(False)

    def _toggled(self, item):
        r = self.list.row(item)
        self.panel.lib_entries[r]["enabled"] = item.checkState() == QtCore.Qt.Checked
        self.panel.save_entries()

    def _add(self):
        fns, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Add MSP library", self.panel.settings.value("lib/last_dir", os.path.expanduser("~")),
            "MSP libraries (*.msp *.MSP *.txt);;All files (*)")
        for fn in fns:
            self.panel.settings.setValue("lib/last_dir", os.path.dirname(fn))
            self.panel.add_library(fn)
        self._fill()

    def _remove(self):
        r = self.list.currentRow()
        if r >= 0:
            e = self.panel.lib_entries.pop(r)
            self.panel.loaded.pop(e["path"], None)
            self.panel.save_entries()
            self._fill()

    def _download_massbank(self):
        dest = self.panel.download_massbank()
        if dest:
            self.panel.add_library(dest)
            self._fill()

    def _browse_nist(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Select the NIST MSSEARCH folder",
                                                       self.nist_dir.text() or "C:\\")
        if d:
            self.nist_dir.setText(os.path.normpath(d))

    def _detect_nist(self):
        d = ns.find_mssearch()
        if d:
            self.nist_dir.setText(d)
        else:
            QtWidgets.QMessageBox.information(self, "NIST", "Couldn't find nistms$.exe in the usual places "
                                                            "(C:\\NIST*\\MSSEARCH). Use Browse… to pick the folder.")

    def accept(self):
        s = self.panel.settings
        s.setValue("lib/ei_only", str(self.ei_only.isChecked()).lower())
        s.setValue("lib/low_cut", str(self.low_cut.isChecked()).lower())
        s.setValue("lib/nhits", self.nhits.value())
        s.setValue("nist/dir", self.nist_dir.text().strip())
        super().accept()


# =============================================================================
class LibraryPanel(QtWidgets.QWidget):
    """
    get_query()  -> (mz, abundance, description, lowest_scanned_mz) or None
    set_mirror(hit|None, (mz, ab)|None) draws/clears the library spectrum under the query
    """
    COLS = ["#", "Name", "Match", "R.Match", "Prob %", "Formula", "MW", "CAS", "Library"]

    def __init__(self, settings: QtCore.QSettings, get_query, set_mirror, status, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.get_query = get_query
        self.set_mirror = set_mirror
        self.status = status
        self.loaded: dict[str, ml.SpectralLibrary] = {}
        self.hits: list[ml.Hit] = []
        self.nist_session = None
        self.nist_timer = QtCore.QTimer(self)
        self.nist_timer.setInterval(400)
        self.nist_timer.timeout.connect(self._poll_nist)
        self._load_entries()

        v = QtWidgets.QVBoxLayout(self)
        h = QtWidgets.QHBoxLayout()
        self.btn_search = QtWidgets.QPushButton("Search libraries")
        self.btn_search.setToolTip("Search the current spectrum against your open MSP libraries (F5)")
        self.btn_search.clicked.connect(self.search_open)
        self.btn_nist = QtWidgets.QPushButton("Search NIST")
        self.btn_nist.setToolTip("Send the current spectrum to NIST MS Search on this PC (F6)")
        self.btn_nist.clicked.connect(self.search_nist)
        if not IS_WINDOWS:
            self.btn_nist.setEnabled(False)
            self.btn_nist.setToolTip("NIST MS Search runs on Windows only")
        b_set = QtWidgets.QPushButton("Libraries…")
        b_set.clicked.connect(self.open_settings)
        self.chk_auto = QtWidgets.QCheckBox("Auto")
        self.chk_auto.setToolTip("Search the open libraries automatically every time the spectrum changes")
        self.chk_auto.setChecked(settings.value("lib/auto", "false") == "true")
        self.chk_auto.toggled.connect(lambda b: (settings.setValue("lib/auto", str(b).lower()),
                                                 b and self.search_open()))
        for w in (self.btn_search, self.btn_nist, b_set, self.chk_auto):
            h.addWidget(w)
        h.addStretch(1)
        v.addLayout(h)

        self.hdr = QtWidgets.QLabel("No search yet.")
        self.hdr.setWordWrap(True)
        self.hdr.setStyleSheet("color:#444;")
        self.hdr.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        v.addWidget(self.hdr)

        self.table = QtWidgets.QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setMinimumHeight(40)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QtWidgets.QHeaderView.Interactive)
        self.table.setColumnWidth(1, 230)
        self.table.itemSelectionChanged.connect(self._hit_selected)
        self.table.itemDoubleClicked.connect(lambda *_: self._open("webbook"))
        self.table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        v.addWidget(self.table, 1)

        h2 = QtWidgets.QHBoxLayout()
        self.b_wb = QtWidgets.QPushButton("NIST WebBook")
        self.b_wb.setToolTip("Open this compound in the NIST Chemistry WebBook (double-click a hit does the same)")
        self.b_wb.clicked.connect(lambda: self._open("webbook"))
        self.b_pc = QtWidgets.QPushButton("PubChem")
        self.b_pc.clicked.connect(lambda: self._open("pubchem"))
        self.b_rec = QtWidgets.QPushButton("Library record")
        self.b_rec.setToolTip("Open the original MassBank / MoNA record page")
        self.b_rec.clicked.connect(lambda: self._open("record"))
        for b in (self.b_wb, self.b_pc, self.b_rec):
            b.setEnabled(False)
            h2.addWidget(b)
        v.addLayout(h2)


    # ------------------------------------------------------------- settings
    def _load_entries(self):
        raw = self.settings.value("lib/entries", "") or ""
        self.lib_entries = []
        for line in str(raw).splitlines():
            if "|" in line:
                en, p = line.split("|", 1)
                self.lib_entries.append({"path": p, "enabled": en == "1"})

    def save_entries(self):
        self.settings.setValue("lib/entries", "\n".join(
            f"{'1' if e['enabled'] else '0'}|{e['path']}" for e in self.lib_entries))

    def add_library(self, path):
        path = os.path.abspath(path)
        if not any(e["path"] == path for e in self.lib_entries):
            self.lib_entries.append({"path": path, "enabled": True})
        self.loaded.pop(path, None)
        self.save_entries()
        self._ensure_loaded([path])

    def nist_dir(self):
        d = self.settings.value("nist/dir", "") or ""
        if not d and IS_WINDOWS:
            d = ns.find_mssearch() or ""
            if d:
                self.settings.setValue("nist/dir", d)
        return d

    def open_settings(self):
        LibrarySettingsDialog(self).exec()

    # ------------------------------------------------------------- loading / download
    def _ensure_loaded(self, paths):
        todo = [p for p in paths if p not in self.loaded and os.path.exists(p)]
        if not todo:
            return
        cache = os.path.join(app_data_dir(), "library_cache")
        dlg = QtWidgets.QProgressDialog("Loading library…", None, 0, 1000, self)
        dlg.setWindowTitle("Libraries")
        dlg.setMinimumDuration(300)
        dlg.setWindowModality(QtCore.Qt.ApplicationModal)
        for p in todo:
            dlg.setLabelText(f"Indexing {os.path.basename(p)} (first time only)…")

            def prog(done, total):
                dlg.setValue(int(1000 * done / max(total, 1)))
                QtWidgets.QApplication.processEvents()
            try:
                QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
                self.loaded[p] = ml.SpectralLibrary.load(p, cache, prog)
            except Exception as e:
                QtWidgets.QMessageBox.warning(self, "Library", f"Could not read {p}:\n{e}")
            finally:
                QtWidgets.QApplication.restoreOverrideCursor()
        dlg.close()

    def download_massbank(self):
        """Download the newest MassBank MSP; returns its path or None."""
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            url, name, tag = latest_massbank_msp()
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        stem = re.sub(r"\.(gz|zip)$", "", name, flags=re.I)
        stem = os.path.splitext(stem)[0]
        dest = os.path.join(app_data_dir(), "libraries", f"{stem}_{tag}{os.path.splitext(name)[1] if name.lower().endswith(('.gz', '.zip')) else '.msp'}")
        if not self.download(url, dest, f"MassBank {tag}"):
            return None
        try:
            return unpack_if_needed(dest)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "MassBank", f"Downloaded, but couldn't unpack it:\n{e}")
            return None

    def download(self, url, dest, label):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".part"
        dlg = QtWidgets.QProgressDialog(f"Downloading {label}…", "Cancel", 0, 1000, self)
        dlg.setWindowTitle("Download")
        dlg.setMinimumDuration(0)
        dlg.setWindowModality(QtCore.Qt.ApplicationModal)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "GCMS-Viewer"})
            with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                got = 0
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    dlg.setLabelText(f"Downloading {label}…  {got / 1e6:.0f}"
                                     + (f" / {total / 1e6:.0f} MB" if total else " MB"))
                    dlg.setValue(int(1000 * got / total) if total else (got // 100000) % 1000)
                    QtWidgets.QApplication.processEvents()
                    if dlg.wasCanceled():
                        raise InterruptedError
            os.replace(tmp, dest)
            return True
        except InterruptedError:
            pass
        except Exception as e:
            if QtWidgets.QMessageBox.question(
                    self, "Download failed",
                    f"Couldn't download {label} automatically:\n{e}\n\n"
                    "Open the download page in your browser instead? Save the .msp file, then add it with "
                    "'Add MSP file…'.") == QtWidgets.QMessageBox.Yes:
                open_url(MASSBANK_PAGE)
        finally:
            dlg.close()
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return False

    # ------------------------------------------------------------- searching
    def on_spectrum_changed(self):
        if self.chk_auto.isChecked() and self.lib_entries:
            self.search_open(quiet=True)
        else:
            self.set_mirror(None, None)
            if self.hits:
                self.hdr.setText(self.hdr.text().split("  ⚠")[0] + "  ⚠ spectrum has changed - search again")

    def search_open(self, quiet=False):
        q = self.get_query()
        if q is None:
            return
        enabled = [e["path"] for e in self.lib_entries if e["enabled"]]
        if not enabled:
            if not quiet:
                if QtWidgets.QMessageBox.question(
                        self, "No libraries",
                        "No open libraries set up yet.\n\nDownload MassBank now (free, ≈13k EI spectra)? "
                        "You can add MoNA or your own MSP files under 'Libraries…'.") == QtWidgets.QMessageBox.Yes:
                    dest = self.download_massbank()
                    if dest:
                        self.add_library(dest)
                        self.search_open()
            return
        self._ensure_loaded(enabled)
        libs = [self.loaded[p] for p in enabled if p in self.loaded]
        mz, ab, desc, low = q
        s = self.settings
        ei_only = s.value("lib/ei_only", "true") == "true"
        hits = ml.search_all(libs, mz, ab, top=int(s.value("lib/nhits", 25)), ei_only=ei_only,
                             low_mz_cut=low if s.value("lib/low_cut", "true") == "true" else None)
        n = sum(lib.n_ei if ei_only else len(lib) for lib in libs)
        self.hdr.setText(f"<b>{desc}</b> vs {', '.join(lib.name for lib in libs)} ({n:,} spectra)")
        self._show_hits(hits)

    def has_libraries(self):
        return any(e["enabled"] for e in self.lib_entries)

    def best_hit(self, mz, ab, label=""):
        """Top open-library hit for a spectrum (used by GCxGC 'Identify'), or None."""
        enabled = [e["path"] for e in self.lib_entries if e["enabled"]]
        if not enabled:
            QtWidgets.QMessageBox.information(
                self, "Libraries", "No open libraries set up yet - use 'Search libraries' or 'Libraries…' "
                                   "on the Library search tab to download MassBank or add an MSP file.")
            return None
        self._ensure_loaded(enabled)
        libs = [self.loaded[p] for p in enabled if p in self.loaded]
        q = self.get_query()
        low = q[3] if q else None
        s = self.settings
        hits = ml.search_all(libs, mz, ab, top=1, ei_only=s.value("lib/ei_only", "true") == "true",
                             low_mz_cut=low if s.value("lib/low_cut", "true") == "true" else None)
        return hits[0] if hits else None

    def search_nist(self):
        if not IS_WINDOWS:
            return
        q = self.get_query()
        if q is None:
            return
        d = self.nist_dir()
        if not d or not os.path.isfile(os.path.join(d, ns.EXE)):
            QtWidgets.QMessageBox.information(
                self, "NIST MS Search",
                "NIST MS Search wasn't found on this PC.\n\nIf it's installed, set its MSSEARCH folder "
                "under 'Libraries…' (usually C:\\NIST20\\MSSEARCH or similar).")
            self.open_settings()
            return
        mz, ab, desc, _ = q
        try:
            self.nist_session = ns.NistSession(d, [(desc, mz, ab)])
            self.nist_session.start()
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "NIST MS Search", str(e))
            self.nist_session = None
            return
        self.nist_desc = desc
        self.status(f"Sent to NIST MS Search ({d}) - waiting for results…")
        self.nist_timer.start()

    def _poll_nist(self):
        s = self.nist_session
        if s is None:
            self.nist_timer.stop()
            return
        if s.poll():
            self.nist_timer.stop()
            res = s.results()
            self.nist_session = None
            hits = []
            for hl in res[:1]:
                for h in hl["hits"]:
                    hits.append(ml.Hit(library=f"NIST {h['lib']}", index=-1, name=h["name"], match=h["mf"],
                                       rmatch=h["rmf"], formula=h["formula"], mw=h["mw"], cas=h["cas"],
                                       accession=h["id"], prob=h["prob"], source="nist"))
            self.hdr.setText(f"<b>{self.nist_desc}</b> vs NIST MS Search "
                             f"({len(hits)} hits - full details in the NIST window)")
            self._show_hits(hits)
            self.status("NIST search complete", 4000)
        elif s.elapsed() > 45:
            self.nist_timer.stop()
            s.restore()
            self.nist_session = None
            QtWidgets.QMessageBox.information(
                self, "NIST MS Search",
                "NIST opened, but didn't send results back.\n\nYour spectrum is in the NIST window. To get "
                "hits listed here as well, in NIST go to Options ▸ Library Search Options ▸ Automation "
                "and tick 'Automatic Search On'.")

    # ------------------------------------------------------------- hits
    def _show_hits(self, hits):
        self.hits = hits
        t = self.table
        t.blockSignals(True)
        t.setRowCount(len(hits))
        for r, h in enumerate(hits):
            vals = [str(r + 1), h.name, str(h.match), str(h.rmatch), h.prob, h.formula, h.mw,
                    h.cas if h.cas not in ("0", "0-00-0") else "", h.library]
            for c, val in enumerate(vals):
                it = QtWidgets.QTableWidgetItem(val)
                if c in (0, 2, 3, 4, 6):
                    it.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                if c == 2:
                    col = "#1a7f37" if h.match >= 800 else ("#9a6700" if h.match >= 650 else "#b42318")
                    it.setForeground(QtGui.QColor(col))
                    f = it.font()
                    f.setBold(True)
                    it.setFont(f)
                if c == 1:
                    it.setToolTip(h.name)
                t.setItem(r, c, it)
        t.blockSignals(False)
        if hits:
            t.selectRow(0)
            self._hit_selected()
        else:
            self.set_mirror(None, None)
            self.hdr.setText(self.hdr.text() + " - no matches")
            for b in (self.b_wb, self.b_pc, self.b_rec):
                b.setEnabled(False)

    def current_hit(self):
        rows = {i.row() for i in self.table.selectedIndexes()}
        return self.hits[min(rows)] if rows and min(rows) < len(self.hits) else None

    def _hit_selected(self):
        h = self.current_hit()
        self.b_wb.setEnabled(h is not None)
        self.b_pc.setEnabled(h is not None)
        self.b_rec.setEnabled(h is not None and ml.record_url(h) is not None)
        if h is None or h.source == "nist":
            self.set_mirror(None, None)
            return
        lib = next((lib for lib in self.loaded.values() if lib.name == h.library), None)
        self.set_mirror(h, lib.spectrum(h.index) if lib else None)

    def _open(self, which):
        h = self.current_hit()
        if h is None:
            return
        url = {"webbook": ml.webbook_url, "pubchem": ml.pubchem_url, "record": ml.record_url}[which](h)
        if url:
            open_url(url)

    def _menu(self, pos):
        h = self.current_hit()
        if h is None:
            return
        m = QtWidgets.QMenu(self)
        m.addAction("Open in NIST WebBook", lambda: self._open("webbook"))
        m.addAction("Open in PubChem", lambda: self._open("pubchem"))
        a = m.addAction("Open library record", lambda: self._open("record"))
        a.setEnabled(ml.record_url(h) is not None)
        m.addSeparator()
        m.addAction("Copy name", lambda: QtWidgets.QApplication.clipboard().setText(h.name))
        m.addAction("Copy hit list", self.copy_hits)
        m.exec(self.table.viewport().mapToGlobal(pos))

    def copy_hits(self):
        rows = ["\t".join(self.COLS)]
        for r, h in enumerate(self.hits):
            rows.append("\t".join([str(r + 1), h.name, str(h.match), str(h.rmatch), h.prob, h.formula,
                                   h.mw, h.cas, h.library]))
        QtWidgets.QApplication.clipboard().setText("\n".join(rows))
        self.status("Hit list copied", 3000)
