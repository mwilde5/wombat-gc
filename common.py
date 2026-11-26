"""Bits shared by the technique windows (NMR, FT-IR, LC-MS, CSV/XY, Compare)."""
from __future__ import annotations

import os

import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from export_dialogs import FILTERS, ImageExportDialog

pg.setConfigOptions(antialias=True, background="w", foreground="#222222")

APP_NAME = "WombatGC"          # one place to rename the app
__version__ = "2.2.1"


def settings():
    return QtCore.QSettings("UoP", "GCMSViewer")


class SendMixin:
    """'Report' menu: add figures, tables and report text from a window to the report builder or the
    figure maker.  A window lists what it can send in report_sources():
        [(label, kind, callable)]   kind = "image" | "table" | "text"
    Images are made by the window's normal image export (its options dialog is shown, then the file
    goes to the report/figure instead of a Save dialog); tables and text are taken from its
    'Copy for report' functions."""
    _send_to = None
    _send_file = None
    _send_caption = ""

    def report_sources(self):
        return []

    def add_report_menu(self, before=None):
        mb = self.menuBar()
        m = QtWidgets.QMenu("&Report", mb)
        if before is not None:
            mb.insertMenu(before.menuAction() if isinstance(before, QtWidgets.QMenu) else before, m)
        else:
            mb.addMenu(m)
        m.aboutToShow.connect(lambda m=m: self._fill_report_menu(m))
        self._fill_report_menu(m)
        return m

    def _fill_report_menu(self, m):
        m.clear()
        try:
            srcs = self.report_sources()
        except Exception:
            srcs = []
        if not srcs:
            m.addAction("Open some data to add it to a report").setEnabled(False)
        for label, kind, fn in srcs:
            m.addAction(f"Add {label} to report…" if kind == "image" else f"Add {label} to report",
                        lambda k=kind, f=fn: self.send_to("report", k, f))
        imgs = [(label, fn) for label, kind, fn in srcs if kind == "image"]
        if imgs:
            m.addSeparator()
            for label, fn in imgs:
                m.addAction(f"Send {label} to the figure maker…", lambda f=fn: self.send_to("figure", "image", f))
        m.addSeparator()
        m.addAction("Open report builder", lambda: self._deliver("report", None))
        m.addAction("Open figure maker", lambda: self._deliver("figure", None))

    def send_target_file(self, ext):
        """Called from the image-export code while sending: a temporary file instead of a Save dialog."""
        import tempfile
        d = os.path.join(tempfile.gettempdir(), "wombatgc-send")
        os.makedirs(d, exist_ok=True)
        fd, fn = tempfile.mkstemp(suffix=ext, dir=d)
        os.close(fd)
        self._send_file = fn
        return fn

    def send_to(self, target, kind, fn):
        import tables
        if kind == "image":
            self._send_to, self._send_file, self._send_caption = target, None, ""
            try:
                fn()
            finally:
                self._send_to = None
            path = self._send_file
            if path and os.path.exists(path) and os.path.getsize(path) > 0:
                self._deliver(target, dict(kind="image", path=path, caption=self._send_caption,
                                           source=self.windowTitle()))
            return
        got = []
        tables.CAPTURE = lambda **kw: got.append(kw)
        try:
            fn()
        finally:
            tables.CAPTURE = None
        if got:
            got[0]["source"] = self.windowTitle()
            self._deliver(target, got[0])
        else:
            self.statusBar().showMessage("Nothing to add yet", 4000)

    def _deliver(self, target, item):
        import report_builder
        report_builder.deliver(target, item, self)


class ToolWindow(SendMixin, QtWidgets.QMainWindow):
    """Base window: settings, status bar, sensible size, Home + Help menu entries."""
    sigHome = QtCore.Signal()
    TECHNIQUE = ""

    def __init__(self):
        super().__init__()
        self.settings = settings()
        self.status = self.statusBar()
        self.setAcceptDrops(True)
        self.setWindowTitle(f"{APP_NAME} – {self.TECHNIQUE}")

    def place(self, w=1450, h=900):
        scr = QtGui.QGuiApplication.primaryScreen()
        if scr is None:
            self.resize(w, h)
            return
        av = scr.availableGeometry()
        w, h = min(w, av.width() - 40), min(h, av.height() - 60)
        self.resize(w, h)
        self.move(av.x() + (av.width() - w) // 2, av.y() + max(0, (av.height() - h) // 3))

    def add_common_menus(self, file_menu: QtWidgets.QMenu):
        file_menu.addSeparator()
        a = file_menu.addAction("Home – choose another technique…", self.sigHome.emit)
        a.setShortcut("Ctrl+H")
        file_menu.addAction("Close window", self.close).setShortcut(QtGui.QKeySequence.Close)

    # -------- file dialogs
    def ask_open(self, title, filt, key="open_dir"):
        start = self.settings.value(key, os.path.expanduser("~"))
        fn, _ = QtWidgets.QFileDialog.getOpenFileName(self, title, start, filt)
        if fn:
            self.settings.setValue(key, os.path.dirname(fn))
        return fn

    def ask_open_dir(self, title, key="open_dir"):
        start = self.settings.value(key, os.path.expanduser("~"))
        d = QtWidgets.QFileDialog.getExistingDirectory(self, title, start)
        if d:
            self.settings.setValue(key, os.path.dirname(d))
        return d

    def save_name(self, title, default, filt):
        start = os.path.join(self.settings.value("export_dir", os.path.expanduser("~")), default)
        fn, _ = QtWidgets.QFileDialog.getSaveFileName(self, title, start, filt)
        if fn:
            self.settings.setValue("export_dir", os.path.dirname(fn))
        return fn

    def image_target(self, kind, default_title, default_stem, extras=None):
        dlg = ImageExportDialog(self, kind, default_title, self.settings, extras=extras, send_to=self._send_to)
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return None, None, None
        opts, use_view = dlg.options()
        if self._send_to:
            self._send_caption = opts.get("title", "")
            if self._send_to == "report":
                opts["title"] = ""          # the title becomes the figure caption instead
            return self.send_target_file(dlg.ext), opts, use_view
        fn = self.save_name("Save image", default_stem + dlg.ext, FILTERS[dlg.ext])
        if fn and not os.path.splitext(fn)[1]:
            fn += dlg.ext
        return fn, opts, use_view

    def done(self, fn):
        self.status.showMessage(f"Saved {fn}", 6000)

    def error(self, msg):
        QtWidgets.QMessageBox.critical(self, APP_NAME, msg)

    @staticmethod
    def safe(s):
        return "".join(c if c.isalnum() or c in "._-+" else "_" for c in s).strip("_") or "export"

    # -------- drag & drop
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        for u in e.mimeData().urls():
            p = u.toLocalFile()
            if p:
                self.open_path(p)
                break

    def open_path(self, path):          # overridden
        pass


def mode_buttons(toolbar: QtWidgets.QToolBar, modes, callback, parent):
    """Exclusive checkable mode actions; modes = [(key, label, tooltip, shortcut)]. Returns dict."""
    grp = QtGui.QActionGroup(parent)
    grp.setExclusive(True)
    acts = {}
    for key, label, tip, sc in modes:
        a = QtGui.QAction(label, parent)
        a.setCheckable(True)
        a.setToolTip(f"{tip}  ({sc})" if sc else tip)
        if sc:
            a.setShortcut(sc)
        a.triggered.connect(lambda _=False, k=key: callback(k))
        grp.addAction(a)
        toolbar.addAction(a)
        acts[key] = a
    return acts


def table_widget(headers, editable_cols=()):
    t = QtWidgets.QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().hide()
    t.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
    t.setEditTriggers(QtWidgets.QAbstractItemView.DoubleClicked | QtWidgets.QAbstractItemView.EditKeyPressed
                      if editable_cols else QtWidgets.QAbstractItemView.NoEditTriggers)
    hh = t.horizontalHeader()
    hh.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
    hh.setStretchLastSection(True)
    t._editable = set(editable_cols)
    return t


def set_row(table, r, values, editable=()):
    for c, v in enumerate(values):
        it = QtWidgets.QTableWidgetItem("" if v is None else str(v))
        if c not in editable:
            it.setFlags(it.flags() & ~QtCore.Qt.ItemIsEditable)
        table.setItem(r, c, it)


def hint_label(text):
    lab = QtWidgets.QLabel(text)
    lab.setWordWrap(True)
    lab.setStyleSheet("color:#555;")
    return lab
