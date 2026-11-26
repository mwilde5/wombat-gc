"""
structure_editor.py - draw chemical structures and reaction schemes with Ketcher (EPAM Systems,
Apache-2.0), built into the app and running offline in a web view.

Drawings go to the figure maker (as vector graphics) or the report, can be copied or saved
(PNG, SVG, MOL, SMILES), and give the formula, molecular weight, exact mass and adduct m/z
values; an open LC-MS window can plot the extracted-ion chromatogram straight away.

The editor itself is a small web app in ketcher_app/ (built with npm into ketcher_app/dist;
the installers include the built copy).
"""
from __future__ import annotations

import functools
import http.server
import json
import mimetypes
import os
import sys
import tempfile
import threading

from PySide6 import QtCore, QtGui, QtWidgets

from common import APP_NAME, settings

READ_FILTER = ("Structure files (*.mol *.sdf *.rxn *.ket *.cdxml *.smi *.smiles *.txt);;All files (*)")


def ketcher_dir():
    """Folder with the built editor (index.html), or None."""
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [os.path.join(getattr(sys, "_MEIPASS", here), "ketcher"),
             os.path.join(here, "ketcher"),
             os.path.join(here, "ketcher_app", "dist")]
    for c in cands:
        if os.path.exists(os.path.join(c, "index.html")):
            return c
    return None


_webengine_error = ""


def webengine_available():
    global _webengine_error
    try:
        from PySide6 import QtWebEngineWidgets  # noqa: F401
        return True
    except Exception as e:
        _webengine_error = f"{type(e).__name__}: {e}"
        return False


# ---------------------------------------------------------------------------- local web server
# Browsers refuse to run module scripts and WebAssembly from file:// pages, so the editor is served
# from a tiny server that only listens on this computer (127.0.0.1).
_server = None


class _Quiet(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".js": "text/javascript", ".mjs": "text/javascript", ".wasm": "application/wasm",
                      ".css": "text/css", ".svg": "image/svg+xml", ".json": "application/json"}

    def log_message(self, *a):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()


def server_url():
    global _server
    d = ketcher_dir()
    if d is None:
        return None
    if _server is None:
        mimetypes.add_type("application/wasm", ".wasm")
        handler = functools.partial(_Quiet, directory=d)
        _server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        _server.daemon_threads = True
        threading.Thread(target=_server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{_server.server_address[1]}/index.html"


# ---------------------------------------------------------------------------- helpers
def svg_natural_mm(svg_bytes):
    import figure_maker
    w, h = figure_maker._svg_size_pt(svg_bytes)
    return w * 25.4 / 72, h * 25.4 / 72


def render_svg(svg_bytes, dpi=600, scale=1.0):
    """SVG -> white-background QImage at dpi (scale enlarges the drawing)."""
    from PySide6 import QtSvg
    r = QtSvg.QSvgRenderer(QtCore.QByteArray(svg_bytes))
    wmm, hmm = svg_natural_mm(svg_bytes)
    w = max(1, round(wmm * scale / 25.4 * dpi))
    h = max(1, round(hmm * scale / 25.4 * dpi))
    img = QtGui.QImage(w, h, QtGui.QImage.Format_RGB32)
    img.fill(QtGui.QColor("white"))
    dpm = round(dpi / 0.0254)
    img.setDotsPerMeterX(dpm)
    img.setDotsPerMeterY(dpm)
    p = QtGui.QPainter(img)
    p.setRenderHints(QtGui.QPainter.Antialiasing | QtGui.QPainter.TextAntialiasing |
                     QtGui.QPainter.SmoothPixmapTransform)
    r.render(p, QtCore.QRectF(0, 0, w, h))
    p.end()
    return img


def _png(img):
    ba = QtCore.QByteArray()
    buf = QtCore.QBuffer(ba)
    buf.open(QtCore.QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(ba)


def _fmt_formula_html(f):
    import re
    return re.sub(r"(?<=[A-Za-z\)])(\d+)", r"<sub>\1</sub>", f or "")


# ---------------------------------------------------------------------------- window
class StructureWindow(QtWidgets.QMainWindow):
    sigReady = QtCore.Signal()

    def __init__(self):
        super().__init__()
        self.settings = settings()
        self.setWindowTitle(f"{APP_NAME} – Structure editor")
        self.status = self.statusBar()
        self.ready = False
        self._pending = {}
        self._seq = 0
        self.snap = None
        self.target_item = None          # figure-maker picture being edited
        self.view = None
        self._build()
        self.resize(1300, 860)

    # ---------------------------------------------------------------- UI
    def _build(self):
        mb = self.menuBar()
        fm = mb.addMenu("&File")
        fm.addAction("New (clear)", self.clear).setShortcut("Ctrl+N")
        fm.addAction("Open structure file…", self.open_file).setShortcut("Ctrl+O")
        fm.addAction("Enter SMILES…", self.enter_smiles)
        fm.addSeparator()
        fm.addAction("Save as image (PNG / SVG)…", self.save_image).setShortcut("Ctrl+E")
        fm.addAction("Save as MOL / RXN…", self.save_molfile)
        fm.addAction("Copy SMILES", self.copy_smiles)
        fm.addSeparator()
        fm.addAction("Close", self.close)
        hm = mb.addMenu("&Help")
        hm.addAction("How to draw structures", self.show_help).setShortcut("F1")

        tb = self.addToolBar("Use")
        tb.setMovable(False)
        tb.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
        self.act_update = tb.addAction("Update figure", self.update_figure_item)
        self.act_update.setToolTip("Put the edited structure back into the figure maker")
        self.act_update.setVisible(False)
        tb.addAction("Insert into figure maker", self.to_figure).setToolTip(
            "Place the drawing on the figure maker's page (vector graphic – sharp at any size)")
        tb.addAction("Add to report", self.to_report).setToolTip("Add the drawing to the report builder")
        tb.addAction("Copy image", self.copy_image).setToolTip("Copy for Word, PowerPoint or the figure maker")
        tb.addSeparator()
        tb.addAction("Save image…", self.save_image)
        tb.addAction("Open file…", self.open_file)

        central = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        self.holder = QtWidgets.QStackedWidget()
        lay.addWidget(self.holder, 1)

        # side panel: properties
        side = QtWidgets.QWidget()
        side.setFixedWidth(290)
        sv = QtWidgets.QVBoxLayout(side)
        box = QtWidgets.QGroupBox("Properties")
        f = QtWidgets.QFormLayout(box)
        self.lab_formula = QtWidgets.QLabel("–")
        self.lab_mw = QtWidgets.QLabel("–")
        self.lab_mono = QtWidgets.QLabel("–")
        for w in (self.lab_formula, self.lab_mw, self.lab_mono):
            w.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        f.addRow("Formula", self.lab_formula)
        f.addRow("Mol. weight", self.lab_mw)
        f.addRow("Exact mass", self.lab_mono)
        self.smiles = QtWidgets.QLineEdit()
        self.smiles.setReadOnly(True)
        self.smiles.setPlaceholderText("SMILES")
        f.addRow("SMILES", self.smiles)
        b = QtWidgets.QPushButton("Copy properties")
        b.setToolTip("Copy formula, MW, exact mass and SMILES (paste into a report or notebook)")
        b.clicked.connect(self.copy_properties)
        f.addRow(b)
        sv.addWidget(box)

        ms = QtWidgets.QGroupBox("Mass spectrometry")
        g = QtWidgets.QVBoxLayout(ms)
        self.adducts = QtWidgets.QTableWidget(0, 2)
        self.adducts.setHorizontalHeaderLabels(["Ion", "m/z"])
        self.adducts.verticalHeader().hide()
        self.adducts.horizontalHeader().setStretchLastSection(True)
        self.adducts.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.adducts.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.adducts.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.adducts.doubleClicked.connect(lambda *_: self.plot_xic())
        g.addWidget(self.adducts, 1)
        b = QtWidgets.QPushButton("Plot XIC in the LC-MS window")
        b.setToolTip("Extracted-ion chromatogram of the selected ion in the open LC-MS window "
                     "(or double-click a row)")
        b.clicked.connect(self.plot_xic)
        g.addWidget(b)
        sv.addWidget(ms, 1)
        hint = QtWidgets.QLabel("Properties update as you draw. A reaction scheme shows the properties of "
                                "everything on the canvas.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#666;")
        sv.addWidget(hint)
        lay.addWidget(side)
        self.setCentralWidget(central)

        msg = QtWidgets.QLabel()
        msg.setWordWrap(True)
        msg.setAlignment(QtCore.Qt.AlignCenter)
        msg.setStyleSheet("color:#555; font-size:11pt;")
        self.msg = msg
        self.holder.addWidget(msg)

        url = server_url()
        if not webengine_available():
            msg.setText("The structure editor needs the Qt WebEngine component.\n\n"
                        "Installed copies of the app include it. When running from the source code, "
                        "install it with:  pip install PySide6-WebEngine")
            return
        if url is None:
            msg.setText("The structure editor isn't built in this copy.\n\nInstalled copies include it. "
                        "From the source code, build it once with Node.js:\n"
                        "cd ketcher_app  then  npm ci  then  npm run build")
            return
        from PySide6 import QtWebEngineCore, QtWebEngineWidgets

        class Page(QtWebEngineCore.QWebEnginePage):
            def __init__(self, log, parent=None):
                super().__init__(parent)
                self.log = log

            def javaScriptConsoleMessage(self, level, message, line, source):
                if len(self.log) < 60:
                    self.log.append(f"js {level.name if hasattr(level, 'name') else level}: {message} "
                                    f"({os.path.basename(source or '')}:{line})")

        self.log = [f"url {url}"]
        self.view = QtWebEngineWidgets.QWebEngineView()
        self.view.setPage(Page(self.log, self.view))
        page = self.view.page()
        page.profile().setHttpCacheType(QtWebEngineCore.QWebEngineProfile.NoCache)
        page.loadFinished.connect(lambda ok: self.log.append(f"page loaded: {ok}"))
        page.renderProcessTerminated.connect(
            lambda status, code: self.log.append(f"render process ended: {status} (exit code {code})"))
        self.holder.addWidget(self.view)
        msg.setText("Starting the structure editor…")
        self.view.load(QtCore.QUrl(url))
        self._ready_timer = QtCore.QTimer(self, interval=250)
        self._ready_timer.timeout.connect(self._check_ready)
        self._ready_timer.start()
        self._t0 = QtCore.QElapsedTimer()
        self._t0.start()
        self.poll = QtCore.QTimer(self, interval=1200)
        self.poll.timeout.connect(self._refresh_properties)

    def diagnostics(self):
        return "\n".join(getattr(self, "log", [f"no web view ({_webengine_error or ketcher_dir()})"]))

    def _check_ready(self):
        def got(v):
            if v and not self.ready:
                self.ready = True
                self._ready_timer.stop()
                self.holder.setCurrentWidget(self.view)
                self.poll.start()
                self.sigReady.emit()
        if self._t0.elapsed() > 90000:
            self._ready_timer.stop()
            self.msg.setText("The structure editor didn't start. Close this window and try again.")
            return
        self.view.page().runJavaScript("!!(window.wg && window.wg.ready)", 0, got)

    # ---------------------------------------------------------------- JS bridge
    def call(self, expr, done, fail=None, timeout_ms=30000):
        """Run a JS expression returning a value or promise; done(value) when it finishes."""
        if not self.ready:
            self.status.showMessage("The editor is still starting…", 3000)
            return
        self._seq += 1
        rid = f"r{self._seq}"
        self.view.page().runJavaScript(f"wg.run({json.dumps(rid)}, () => ({expr}))", 0)
        t = QtCore.QElapsedTimer()
        t.start()

        def poll():
            def got(v):
                if not v or not isinstance(v, str):      # JS null arrives as None or ''
                    if t.elapsed() > timeout_ms:
                        (fail or self._fail)("timed out")
                    else:
                        QtCore.QTimer.singleShot(40, poll)
                    return
                r = json.loads(v)
                if r.get("ok"):
                    done(r.get("value"))
                else:
                    (fail or self._fail)(r.get("value"))
            self.view.page().runJavaScript(f"wg.take({json.dumps(rid)})", 0, got)
        QtCore.QTimer.singleShot(30, poll)

    def _fail(self, msg):
        self.status.showMessage(f"Structure editor: {msg}", 8000)

    def snapshot(self, done, images=False):
        self.call(f"wg.snapshot({'true' if images else 'false'})", done)

    # ---------------------------------------------------------------- properties
    def _refresh_properties(self):
        if not self.isVisible() or not self.ready:
            return
        self.snapshot(self._show_properties)

    def _show_properties(self, snap):
        key = (snap or {}).get("ket")
        if self.snap is not None and self.snap.get("ket") == key:
            return
        self.snap = snap
        if not snap or snap.get("empty"):
            for w in (self.lab_formula, self.lab_mw, self.lab_mono):
                w.setText("–")
            self.smiles.clear()
            self.adducts.setRowCount(0)
            return
        f = " ".join(str(snap.get("formula") or "").replace("[", "").replace("]", "").split())
        f = f.replace(" ", "").replace(">", "  →  ").replace("+", " + ")
        self.lab_formula.setText(_fmt_formula_html(f) or "–")
        mw = snap.get("mw")
        mono = snap.get("mono")
        self.lab_mw.setText(f"{float(mw):.2f} g/mol" if _num(mw) else (str(mw) if mw else "–"))
        self.lab_mono.setText(f"{float(mono):.4f}" if _num(mono) else (str(mono) if mono else "–"))
        self.smiles.setText(snap.get("smiles") or "")
        self.smiles.setCursorPosition(0)
        self.adducts.setRowCount(0)
        if _num(mono) and not snap.get("reaction") and "." not in (snap.get("smiles") or ""):
            import lcms
            rows = [(name, lcms.adduct_mz(float(mono), name)) for name in lcms.ADDUCTS]
            self.adducts.setRowCount(len(rows))
            for i, (name, mz) in enumerate(rows):
                self.adducts.setItem(i, 0, QtWidgets.QTableWidgetItem(name))
                it = QtWidgets.QTableWidgetItem(f"{mz:.4f}")
                it.setData(QtCore.Qt.UserRole, mz)
                self.adducts.setItem(i, 1, it)
            self.adducts.selectRow(0)
            self.adducts.resizeColumnToContents(0)

    def copy_properties(self):
        s = self.snap
        if not s or s.get("empty"):
            return
        mw, mono = s.get("mw"), s.get("mono")
        text = (f"{s.get('formula', '')}; MW {float(mw):.2f} g/mol; exact mass {float(mono):.4f}; "
                f"SMILES {s.get('smiles', '')}") if _num(mw) and _num(mono) else s.get("smiles", "")
        QtWidgets.QApplication.clipboard().setText(text)
        self.status.showMessage("Copied", 3000)

    def plot_xic(self):
        r = self.adducts.currentRow()
        if r < 0:
            self.status.showMessage("Draw a single structure first", 4000)
            return
        mz = self.adducts.item(r, 1).data(QtCore.Qt.UserRole)
        ion = self.adducts.item(r, 0).text()
        try:
            import lcms_view
        except Exception:
            return
        wins = [w for w in QtWidgets.QApplication.topLevelWidgets()
                if isinstance(w, lcms_view.LCMSWindow) and w.isVisible() and w.run is not None]
        if not wins:
            QtWidgets.QApplication.clipboard().setText(f"{mz:.5f}")
            self.status.showMessage(f"{ion} m/z {mz:.4f} copied – open an LC-MS file to plot its XIC", 6000)
            return
        w = wins[0]
        w.eic_edit.setText(f"{mz:.5f}")
        w._eic_entered()
        w.raise_()
        w.activateWindow()
        self.status.showMessage(f"XIC of {ion} m/z {mz:.4f} plotted in {w.windowTitle()}", 6000)

    # ---------------------------------------------------------------- output
    def _with_drawing(self, fn):
        def got(snap):
            if not snap or snap.get("empty"):
                self.status.showMessage("The canvas is empty – draw a structure first", 4000)
                return
            self.snap = snap if snap.get("svg") else self.snap
            fn(snap)
        self.snapshot(got, images=True)

    def _svg_file(self, snap):
        fd, fn = tempfile.mkstemp(suffix=".svg", dir=_tmpdir())
        os.close(fd)
        with open(fn, "w", encoding="utf-8") as fh:
            fh.write(snap["svg"])
        return fn

    def to_figure(self):
        def go(snap):
            import report_builder
            w = report_builder.deliver("figure", None, self)
            w.add_structure(snap["svg"].encode("utf-8"), snap["ket"])
        self._with_drawing(go)

    def update_figure_item(self):
        it = self.target_item

        def go(snap):
            try:
                it.replace_svg(snap["svg"].encode("utf-8"), snap["ket"])
            except RuntimeError:
                self.status.showMessage("That figure item no longer exists – use Insert into figure maker", 5000)
                return
            sc = it.scene()
            if sc is not None:
                sc.edited()
                w = sc.views()[0].window() if sc.views() else None
                if w is not None:
                    w.raise_()
                    w.activateWindow()
            self.status.showMessage("Figure updated", 4000)
        if it is not None:
            self._with_drawing(go)

    def edit_item(self, item):
        """Open a figure-maker structure for editing."""
        self.target_item = item
        self.act_update.setVisible(True)

        def load():
            self.call(f"window.ketcher.setMolecule({json.dumps(item.meta.get('ket', ''))})",
                      lambda _v: self._refresh_properties())
        if self.ready:
            load()
        else:
            self.sigReady.connect(load, QtCore.Qt.SingleShotConnection)

    def to_report(self):
        def go(snap):
            import report_builder
            svg = snap["svg"].encode("utf-8")
            img = render_svg(svg, int(self.settings.value("img/dpi", 600)))
            fd, fn = tempfile.mkstemp(suffix=".png", dir=_tmpdir())
            os.close(fd)
            img.save(fn, "PNG")
            cap = snap.get("formula") or ""
            report_builder.deliver("report", dict(kind="image", path=fn, caption="", source="the structure editor"
                                                  + (f" ({cap})" if cap else "")), self)
        self._with_drawing(go)

    def copy_image(self):
        def go(snap):
            svg = snap["svg"].encode("utf-8")
            md = QtCore.QMimeData()
            md.setImageData(render_svg(svg, 300))
            md.setData("image/svg+xml", QtCore.QByteArray(svg))
            QtWidgets.QApplication.clipboard().setMimeData(md)
            self.status.showMessage("Structure copied – paste into Word, PowerPoint or the figure maker", 5000)
        self._with_drawing(go)

    def save_image(self):
        def go(snap):
            fn, filt = QtWidgets.QFileDialog.getSaveFileName(
                self, "Save structure image", os.path.join(self.settings.value("export_dir", os.path.expanduser("~")),
                                                           "structure.png"),
                "PNG (*.png);;SVG (*.svg);;TIFF (*.tif)")
            if not fn:
                return
            self.settings.setValue("export_dir", os.path.dirname(fn))
            ext = os.path.splitext(fn)[1].lower() or (".svg" if "SVG" in filt else ".tif" if "TIFF" in filt else ".png")
            if not os.path.splitext(fn)[1]:
                fn += ext
            if ext == ".svg":
                with open(fn, "w", encoding="utf-8") as fh:
                    fh.write(snap["svg"])
            else:
                img = render_svg(snap["svg"].encode("utf-8"), int(self.settings.value("img/dpi", 600)))
                img.save(fn, "TIFF" if ext in (".tif", ".tiff") else "PNG")
            self.status.showMessage(f"Saved {fn}", 5000)
        self._with_drawing(go)

    def save_molfile(self):
        def go(snap):
            rxn = snap.get("reaction")
            ext = ".rxn" if rxn else ".mol"
            fn, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "Save structure", os.path.join(self.settings.value("export_dir", os.path.expanduser("~")),
                                                     "structure" + ext), f"{ext[1:].upper()} (*{ext})")
            if fn:
                if not os.path.splitext(fn)[1]:
                    fn += ext
                with open(fn, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(snap.get("molfile") or "")
                self.status.showMessage(f"Saved {fn}", 5000)
        self.snapshot(lambda s: go(s) if s and not s.get("empty") else
                      self.status.showMessage("The canvas is empty", 3000))

    def copy_smiles(self):
        self.snapshot(lambda s: (QtWidgets.QApplication.clipboard().setText(s.get("smiles") or ""),
                                 self.status.showMessage("SMILES copied", 3000)) if s and not s.get("empty") else None)

    # ---------------------------------------------------------------- input
    def set_structure(self, text, done=None):
        self.call(f"window.ketcher.setMolecule({json.dumps(text)})",
                  lambda _v: (self._refresh_properties(), done and done()),
                  lambda e: QtWidgets.QMessageBox.warning(self, APP_NAME, f"Couldn't read that structure:\n{e}"))

    def clear(self):
        self.target_item = None
        self.act_update.setVisible(False)
        self.set_structure("")

    def open_file(self, fn=None):
        if not fn:
            fn, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "Open structure", self.settings.value("open_dir", os.path.expanduser("~")), READ_FILTER)
        if not fn:
            return
        self.settings.setValue("open_dir", os.path.dirname(fn))
        try:
            with open(fn, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as e:
            QtWidgets.QMessageBox.critical(self, APP_NAME, str(e))
            return
        self.set_structure(text)

    def enter_smiles(self):
        s, ok = QtWidgets.QInputDialog.getText(self, "SMILES", "SMILES (e.g. CC(=O)CC(C)=O):")
        if ok and s.strip():
            self.set_structure(s.strip())

    def add_item(self, item):
        pass

    def closeEvent(self, e):
        if getattr(self, "poll", None):
            self.poll.stop()
        super().closeEvent(e)

    def show_help(self):
        QtWidgets.QMessageBox.information(self, "Structure editor", """
<b>Drawing</b>: pick a bond, atom or ring on the left/top toolbars and click on the canvas; drag from an atom
to add a bond. Click an atom and type a symbol (O, N, Cl …) to change it. Ctrl+Z undoes.<br><br>
<b>Reactions and tautomers</b>: the arrow tool (right-hand toolbar) adds reaction, equilibrium (⇌) and
other arrows, and <i>+</i> signs – e.g. keto ⇌ enol tautomerism.<br><br>
<b>Opening</b>: MOL, SDF, RXN, CDXML, KET or SMILES files, or <i>File ▸ Enter SMILES</i>. You can also paste
SMILES or a molfile onto the canvas.<br><br>
<b>Using the drawing</b>: <i>Insert into figure maker</i> (vector, sharp at any size; double-click it there
to come back and edit), <i>Add to report</i>, <i>Copy image</i>, or <i>Save image</i> (PNG/TIFF at 600 dpi,
or SVG).<br><br>
<b>Properties</b>: formula, molecular weight, exact (monoisotopic) mass and adduct m/z values. Double-click
an ion to plot its extracted-ion chromatogram in an open LC-MS window.<br><br>
The editor is Ketcher (EPAM Systems, Apache-2.0 licence) and works without an internet connection.
""")


def _num(v):
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _tmpdir():
    d = os.path.join(tempfile.gettempdir(), "wombatgc-send")
    os.makedirs(d, exist_ok=True)
    return d


_window = None


def window():
    global _window
    try:
        if _window is not None:
            _window.isVisible()
    except RuntimeError:
        _window = None
    if _window is None:
        _window = StructureWindow()
    return _window


def open_editor(item=None):
    w = window()
    w.show()
    w.raise_()
    w.activateWindow()
    if item is not None:
        w.edit_item(item)
    return w
