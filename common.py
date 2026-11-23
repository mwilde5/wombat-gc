"""Bits shared by the technique windows (NMR, FT-IR, LC-MS, CSV/XY, Compare)."""
from __future__ import annotations

import os

import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from export_dialogs import FILTERS, ImageExportDialog

pg.setConfigOptions(antialias=True, background="w", foreground="#222222")

APP_NAME = "WombatGC"          # one place to rename the app
__version__ = "2.0"


def settings():
    return QtCore.QSettings("UoP", "GCMSViewer")


class ToolWindow(QtWidgets.QMainWindow):
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
        dlg = ImageExportDialog(self, kind, default_title, self.settings, extras=extras)
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return None, None, None
        opts, use_view = dlg.options()
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
