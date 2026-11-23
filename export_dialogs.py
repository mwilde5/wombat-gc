"""Shared image-export dialog (size in mm, dpi, format, title, technique-specific options)."""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets

FILTERS = {".png": "PNG (*.png)", ".tif": "TIFF (*.tif *.tiff)", ".svg": "SVG (*.svg)", ".pdf": "PDF (*.pdf)"}


class ImageExportDialog(QtWidgets.QDialog):
    def __init__(self, parent, kind, default_title, settings: QtCore.QSettings, mirror_name=None,
                 extras=None):
        """extras: [(key, label, default_bool)] check boxes for other kinds (nmr, ir, xy …)."""
        super().__init__(parent)
        self.kind = kind
        self.s = settings
        self.setWindowTitle({"chrom": "Export chromatogram image", "spec": "Export spectrum image",
                             "2d": "Export GCxGC 2D image"}.get(kind, "Export image"))
        self.extras = extras or []
        f = QtWidgets.QFormLayout(self)
        g = lambda k, d: self.s.value(f"img/{k}", d)  # noqa: E731

        self.fmt = QtWidgets.QComboBox()
        self.fmt.addItems(["PNG", "TIFF", "SVG (vector)", "PDF (vector)"])
        self.fmt.setCurrentIndex(int(g("fmt", 0)))
        self.dpi = QtWidgets.QSpinBox(); self.dpi.setRange(72, 2400); self.dpi.setSingleStep(100)
        self.dpi.setValue(int(g("dpi", 600))); self.dpi.setSuffix(" dpi")
        self.w = QtWidgets.QDoubleSpinBox(); self.w.setRange(30, 600); self.w.setValue(float(g("w", 160))); self.w.setSuffix(" mm")
        self.h = QtWidgets.QDoubleSpinBox(); self.h.setRange(20, 600); self.h.setValue(float(g("h", 90))); self.h.setSuffix(" mm")
        self.font = QtWidgets.QSpinBox(); self.font.setRange(5, 24); self.font.setValue(int(g("font", 9))); self.font.setSuffix(" pt")
        self.view = QtWidgets.QComboBox(); self.view.addItems(["Current zoom", "Full range"])
        self.view.setCurrentIndex(int(g("view", 0)))
        self.title = QtWidgets.QLineEdit(default_title)
        self.color = QtWidgets.QComboBox(); self.color.addItems(["Black", "Dark blue"])
        self.color.setCurrentIndex(int(g("color", 0)))

        f.addRow("Format", self.fmt)
        f.addRow("Resolution", self.dpi)
        f.addRow("Width", self.w)
        f.addRow("Height", self.h)
        f.addRow("Font size", self.font)
        f.addRow("Range", self.view)
        f.addRow("Title", self.title)
        if kind != "2d":
            f.addRow("Line colour", self.color)
        if kind == "2d":
            self.interp = QtWidgets.QComboBox()
            self.interp.addItems(["Bicubic (smooth)", "Bilinear", "None (raw pixels)"])
            self.interp.setCurrentIndex(int(g("interp", 0)))
            self.cbar = QtWidgets.QCheckBox("Colour bar")
            self.cbar.setChecked(g("cbar", "true") == "true")
            self.blobs_chk = QtWidgets.QCheckBox("Show integrated peaks (boxes + numbers)")
            self.blobs_chk.setChecked(g("blobs", "true") == "true")
            f.addRow("Smoothing", self.interp)
            f.addRow("", self.cbar)
            f.addRow("", self.blobs_chk)
            note = QtWidgets.QLabel("Colours, scale and the colour range (noise floor / saturation) "
                                    "are taken from the screen.")
            note.setWordWrap(True)
            note.setStyleSheet("color:#555;")
            f.addRow(note)
        elif kind not in ("chrom", "spec"):
            self.extra_boxes = {}
            for key, label, default in (extras or []):
                cb = QtWidgets.QCheckBox(label)
                cb.setChecked(g(f"{kind}_{key}", "true" if default else "false") == "true")
                self.extra_boxes[key] = cb
                f.addRow("", cb)
        elif kind == "chrom":
            self.shade = QtWidgets.QCheckBox("Shade integrated peaks"); self.shade.setChecked(g("shade", "true") == "true")
            self.lab = QtWidgets.QCheckBox("Label integrated peaks with RT"); self.lab.setChecked(g("lab", "true") == "true")
            f.addRow("", self.shade)
            f.addRow("", self.lab)
        else:
            self.nlab = QtWidgets.QSpinBox(); self.nlab.setRange(0, 50); self.nlab.setValue(int(g("nlab", 10)))
            self.rel = QtWidgets.QCheckBox("Relative abundance (base peak = 100 %)"); self.rel.setChecked(g("rel", "true") == "true")
            f.addRow("Label top peaks", self.nlab)
            f.addRow("", self.rel)
            self.mir = QtWidgets.QCheckBox(f"Include library match below (head-to-tail):\n{mirror_name}"
                                           if mirror_name else "Include library match (none selected)")
            self.mir.setChecked(bool(mirror_name) and g("mir", "true") == "true")
            self.mir.setEnabled(bool(mirror_name))
            f.addRow("", self.mir)
        self.px = QtWidgets.QLabel()
        f.addRow("Pixel size", self.px)
        for wdg in (self.dpi, self.w, self.h):
            wdg.valueChanged.connect(self._update_px)
        self.fmt.currentIndexChanged.connect(self._update_px)
        self._update_px()
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def _update_px(self):
        if self.fmt.currentIndex() >= 2:
            self.px.setText("vector - scales to any size")
            self.dpi.setEnabled(False)
        else:
            self.dpi.setEnabled(True)
            self.px.setText(f"{round(self.w.value() / 25.4 * self.dpi.value())} × "
                            f"{round(self.h.value() / 25.4 * self.dpi.value())} px")

    @property
    def ext(self):
        return [".png", ".tif", ".svg", ".pdf"][self.fmt.currentIndex()]

    def options(self):
        s = self.s
        s.setValue("img/fmt", self.fmt.currentIndex()); s.setValue("img/dpi", self.dpi.value())
        s.setValue("img/w", self.w.value()); s.setValue("img/h", self.h.value())
        s.setValue("img/font", self.font.value()); s.setValue("img/view", self.view.currentIndex())
        s.setValue("img/color", self.color.currentIndex())
        o = dict(width_mm=self.w.value(), height_mm=self.h.value(), dpi=self.dpi.value(),
                 font=self.font.value(), title=self.title.text(),
                 color=["#000000", "#1f3b73"][self.color.currentIndex()])
        if self.kind == "2d":
            s.setValue("img/interp", self.interp.currentIndex())
            s.setValue("img/cbar", str(self.cbar.isChecked()).lower())
            s.setValue("img/blobs", str(self.blobs_chk.isChecked()).lower())
            o.pop("color", None)
            o.update(interpolation=["bicubic", "bilinear", "nearest"][self.interp.currentIndex()],
                     colorbar=self.cbar.isChecked(), show_blobs=self.blobs_chk.isChecked())
        elif self.kind not in ("chrom", "spec"):
            for key, cb in self.extra_boxes.items():
                s.setValue(f"img/{self.kind}_{key}", str(cb.isChecked()).lower())
                o[key] = cb.isChecked()
        elif self.kind == "chrom":
            s.setValue("img/shade", str(self.shade.isChecked()).lower()); s.setValue("img/lab", str(self.lab.isChecked()).lower())
            o.update(shade_peaks=self.shade.isChecked(), label_peaks=self.lab.isChecked())
        else:
            s.setValue("img/nlab", self.nlab.value()); s.setValue("img/rel", str(self.rel.isChecked()).lower())
            if self.mir.isEnabled():
                s.setValue("img/mir", str(self.mir.isChecked()).lower())
            o.update(n_labels=self.nlab.value(), relative=self.rel.isChecked())
            self.include_mirror = self.mir.isChecked()
        return o, self.view.currentIndex() == 0
