"""
view3d.py - interactive 3D surface of the GCxGC chromatogram, shown in a dockable side panel
that follows the 2D view (same region, colours, scaling and colour range), plus the
shared code that turns the current view into an exportable 3D model.
"""
from __future__ import annotations

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

import mesh_export as me

try:
    import pyqtgraph.opengl as gl
    GL_ERROR = None
except Exception as e:          # PyOpenGL missing or no OpenGL on this PC
    gl = None
    GL_ERROR = str(e)

def _reset_shader_cache():
    """
    pyqtgraph compiles its GL shaders once and caches them globally, but a compiled shader
    belongs to one OpenGL context.  Clear the cache before making a GL view in a new window.
    """
    try:
        from pyqtgraph.opengl import shaders
        for prog in shaders.ShaderProgram.names.values():
            prog.prog = None
            for sh in getattr(prog, "shaders", []):
                sh.compiled = None
    except Exception:
        pass
    # some item classes (lines, scatter, …) keep their own class-level compiled program
    for name in dir(gl):
        cls = getattr(gl, name, None)
        if isinstance(cls, type) and "_shaderProgram" in getattr(cls, "__dict__", {}):
            cls._shaderProgram = None


def _register_shader():
    """Soft 'headlight' shading: colours stay true to the 2D plot, slopes still read as 3D."""
    if gl is None:
        return "shaded"
    from pyqtgraph.opengl import shaders
    if "gcxgc_lit" not in shaders.ShaderProgram.names:
        vert = shaders.ShaderProgram.names["shaded"].shaders[0].code
        frag = """
            #ifdef GL_ES
            precision mediump float;
            #endif
            varying vec4 v_color;
            varying vec3 v_normal;
            void main() {
                vec3 n = normalize(v_normal);
                float d = abs(dot(n, normalize(vec3(0.25, 0.55, 1.0))));
                float s = pow(max(dot(n, normalize(vec3(0.0, 0.35, 1.0))), 0.0), 24.0);
                vec3 rgb = v_color.rgb * (0.58 + 0.47 * d) + vec3(0.10 * s);
                gl_FragColor = vec4(min(rgb, vec3(1.0)), v_color.a);
            }
        """
        shaders.ShaderProgram("gcxgc_lit", [shaders.VertexShader(vert), shaders.FragmentShader(frag)])
    return "gcxgc_lit"


if gl is not None:
    class SafeGLView(gl.GLViewWidget):
        """GL view that records an OpenGL start-up failure instead of erroring on every repaint."""
        failed = None

        def initializeGL(self):
            try:
                super().initializeGL()
            except Exception as e:      # e.g. Remote Desktop / very old drivers
                self.failed = str(e) or "OpenGL could not start"

        def paintGL(self, *a, **k):
            if self.failed or not self.isValid():
                return
            try:
                super().paintGL(*a, **k)
            except Exception as e:
                self.failed = str(e)

GL_HELP = ("The 3D view couldn't start OpenGL on this PC (common over Remote Desktop or with very old "
           "graphics drivers).\n\nEverything else - including 3D model export (GLB / FBX / STL) - still works.\n"
           "To try software rendering, set the environment variable  QT_OPENGL=software  and restart.")


ASPECT_STRETCHED = (2.4, 1.0, 0.7)    # 1D : 2D : height, as on the single-trace -> 3D page
ASPECT_SQUARE = (1.0, 1.0, 0.7)


def region_mesh(gxp, full=False, max1=800, max2=200, width=1.0, stretched=True, match_screen=True,
                watertight=False, height_scale=1.0, smooth=None):
    """
    Solid mesh of the 2D panel's current region (or the whole run), using its scaling,
    display baseline, colour scheme and - if match_screen - its colour range.
    Returns (mesh, info dict).
    """
    f = gxp.fold
    T = gxp.T
    if full:
        j0, j1, r0, r1 = 0, f.n1 - 1, 0, f.n2 - 1
    else:
        j0, j1, r0, r1 = gxp.view_box_pixels()
    if j1 - j0 < 1 or r1 - r0 < 1:
        return None, {}
    smooth = gxp.smooth if smooth is None else smooth
    Z = me.model_grid(T, j0, j1, r0, r1, max1=max1, smooth=smooth)
    cv = None
    if match_screen:
        lo, hi = gxp.levels()
        cv = np.clip((Z - lo) / ((hi - lo) or 1.0), 0, 1)
    ax, ay, az = ASPECT_STRETCHED if stretched else ASPECT_SQUARE
    W = float(width)
    m = me.build_mesh(Z, width=W, depth=W * ay / ax, height=W * az / ax * height_scale, base=W * 0.02,
                      stops=gxp.stops, base_color=gxp.base_colour, max2=max2, colour_values=cv,
                      watertight_base=watertight)
    info = {"x0": float(f.col_start_min(j0)), "x1": float(f.col_start_min(j1 + 1)),
            "y0": r0 * f.dt, "y1": (r1 + 1) * f.dt, "width": W, "depth": W * ay / ax,
            "zmax": float(me.pool_rows(Z, max2).max()) or 1.0, "base": W * 0.02,
            "height": W * az / ax * height_scale}
    return m, info


class Pop3DWindow(QtWidgets.QWidget):
    """Separate top-level window holding the 3D panel; closing it docks the panel back."""
    closed = QtCore.Signal()

    def __init__(self, title):
        super().__init__(None, QtCore.Qt.Window)
        self.setWindowTitle(title)
        self.lay = QtWidgets.QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.resize(950, 680)

    def closeEvent(self, e):
        self.closed.emit()
        super().closeEvent(e)


class View3DPanel(QtWidgets.QWidget):
    """Sits beside the 2D plot, or pops out into its own window."""
    sigPop = QtCore.Signal()

    def __init__(self, gxp, settings: QtCore.QSettings, parent=None):
        super().__init__(parent)
        self.gxp = gxp
        self.s = settings
        self.marker_xy = None
        self.info = None
        self._mesh_item = None
        self._marker_item = None
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        h = QtWidgets.QHBoxLayout()
        self.follow = QtWidgets.QCheckBox("Follow 2D view")
        self.follow.setToolTip("Show the region currently zoomed in the 2D plot (otherwise the whole run)")
        self.follow.setChecked(settings.value("3d/follow", "true") == "true")
        self.follow.toggled.connect(self._follow_changed)
        h.addWidget(self.follow)
        h.addWidget(QtWidgets.QLabel("Height"))
        self.height = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.height.setRange(10, 300)
        self.height.setValue(int(settings.value("3d/height", 100)))
        self.height.setMaximumWidth(110)
        self.height.setToolTip("Vertical exaggeration")
        self.height.valueChanged.connect(lambda val: (self.s.setValue("3d/height", val), self.schedule()))
        h.addWidget(self.height)
        self.colour_mode = QtWidgets.QComboBox()
        self.colour_mode.addItems(["Colours as 2D", "Colour by height"])
        self.colour_mode.setToolTip("As 2D: same colour range as the 2D plot (tall peaks saturate).\n"
                                    "By height: the scheme runs from floor to the tallest peak, so banded\n"
                                    "schemes climb up each peak like contour lines.")
        self.colour_mode.setCurrentIndex(int(settings.value("3d/colour_mode", 0)))
        self.colour_mode.currentIndexChanged.connect(lambda i: (self.s.setValue("3d/colour_mode", i), self.schedule()))
        h.addWidget(self.colour_mode)
        self.detail = QtWidgets.QComboBox()
        self.detail.addItems(["Normal detail", "High detail"])
        self.detail.setCurrentIndex(int(settings.value("3d/detail", 0)))
        self.detail.currentIndexChanged.connect(lambda i: (self.s.setValue("3d/detail", i), self.schedule()))
        h.addWidget(self.detail)
        h.addStretch(1)
        self.btn_reset = QtWidgets.QToolButton()
        self.btn_reset.setText("Reset camera")
        self.btn_reset.clicked.connect(self.reset_camera)
        self.btn_snap = QtWidgets.QToolButton()
        self.btn_snap.setText("Save view…")
        self.btn_snap.setToolTip("Save this 3D view as a PNG image")
        self.btn_snap.clicked.connect(self.save_view)
        self.btn_pop = QtWidgets.QToolButton()
        self.btn_pop.setText("Pop out")
        self.btn_pop.setToolTip("Open the 3D view in its own window (click again to dock it back)")
        self.btn_pop.clicked.connect(self.sigPop)
        for b in (self.btn_reset, self.btn_snap, self.btn_pop):
            h.addWidget(b)
        v.addLayout(h)

        if gl is None:
            msg = QtWidgets.QLabel("The 3D view needs OpenGL, which isn't available here:\n"
                                   f"{GL_ERROR}\n\nInstall it with:  pip install PyOpenGL\n"
                                   "3D model export (GLB / FBX / STL) still works.")
            msg.setWordWrap(True)
            msg.setAlignment(QtCore.Qt.AlignCenter)
            v.addWidget(msg, 1)
            self.view = None
            for w in (self.follow, self.height, self.detail, self.btn_reset, self.btn_snap):
                w.setEnabled(False)
        else:
            self.view = None
            self._vlay = v
            self._make_view()
        self.lbl = QtWidgets.QLabel("")
        self.lbl.setStyleSheet("color:#666;")
        self.lbl.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        v.addWidget(self.lbl)
        self.lbl_hint = "Left-drag: rotate · Wheel: zoom · Middle-drag (or Ctrl+left): pan"

        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(150)
        self.timer.timeout.connect(self.rebuild)
        gxp.sigDisplayChanged.connect(self.schedule)
        gxp.sigViewChanged.connect(self._view_changed)
        gxp.sigMarker.connect(self.set_marker)

    def _make_view(self, camera=None):
        _reset_shader_cache()
        self.view = SafeGLView()
        self.view.setMinimumSize(200, 150)
        self._vlay.insertWidget(1, self.view, 1)
        self._mesh_item = None
        self._marker_item = None
        if camera:
            self.view.opts.update(camera)
        else:
            self.reset_camera()

    def drop_view(self):
        """Remove the GL widget before the panel moves window (OpenGL contexts can't move)."""
        if gl is None or self.view is None:
            return
        self._cam = {k: self.view.opts[k] for k in ("center", "distance", "elevation", "azimuth")}
        old = self.view
        self.view = None
        self._mesh_item = None
        self._marker_item = None
        self._vlay.removeWidget(old)
        old.hide()
        old.setParent(None)
        old.deleteLater()

    def restore_view(self):
        """Create a fresh GL widget in the panel's new window and redraw."""
        if gl is None or self.view is not None:
            return
        self._make_view(getattr(self, "_cam", None))
        self.schedule()

    def showEvent(self, e):
        super().showEvent(e)
        self.schedule()

    def _follow_changed(self, on):
        self.s.setValue("3d/follow", str(on).lower())
        self.schedule()

    def _view_changed(self):
        if self.follow.isChecked():
            self.schedule()

    def schedule(self):
        if self.isVisible():
            self.timer.start()

    # ---- drawing -------------------------------------------------------------------------
    def rebuild(self):
        if self.view is None or self.gxp.T is None or self.gxp.fold is None:
            return
        if self.view.failed or (self.view.isVisible() and not self.view.isValid()):
            self.lbl.setText(GL_HELP.split("\n")[0])
            self.lbl.setWordWrap(True)
            self.lbl.setToolTip(GL_HELP)
            return
        hi_detail = self.detail.currentIndex() == 1
        try:
            m, info = region_mesh(self.gxp, full=not self.follow.isChecked(),
                                  max1=600 if hi_detail else 260, max2=200 if hi_detail else 110,
                                  width=2.4, stretched=True, height_scale=self.height.value() / 100.0,
                                  match_screen=self.colour_mode.currentIndex() == 0)
        except Exception as e:  # never let the side panel break the app
            self.lbl.setText(f"3D view: {e}")
            return
        if m is None:
            return
        self.info = info
        P = m["P"]
        verts = np.stack([P[:, 0], -P[:, 2], P[:, 1]], axis=1)       # Y-up -> Z-up for the GL view
        faces = m["I"].reshape(-1, 3)
        cols = np.ones((len(P), 4), np.float32)
        cols[:, :3] = m["C"]
        md = gl.MeshData(vertexes=verts, faces=faces, vertexColors=cols)
        if self._mesh_item is None:
            self._mesh_item = gl.GLMeshItem(meshdata=md, smooth=True, shader=_register_shader(), glOptions="opaque")
            self.view.addItem(self._mesh_item)
        else:
            self._mesh_item.setMeshData(meshdata=md, smooth=True)
        bg = QtGui.QColor(self.gxp.bg_colour)
        self.view.setBackgroundColor(bg)
        self._draw_marker()
        tris = len(faces)
        self.lbl.setText(f"¹tR {info['x0']:.2f}–{info['x1']:.2f} min · ²tR {info['y0']:.2f}–{info['y1']:.2f} s · "
                         f"{tris:,} triangles   |   {self.lbl_hint}")

    def set_marker(self, x_min, y_s):
        self.marker_xy = (x_min, y_s)
        self._draw_marker()

    def _draw_marker(self):
        if self.view is None:
            return
        if self._marker_item is not None:
            self.view.removeItem(self._marker_item)
            self._marker_item = None
        if self.marker_xy is None or self.info is None:
            return
        i = self.info
        x, y = self.marker_xy
        if not (i["x0"] <= x <= i["x1"] and i["y0"] <= y <= i["y1"]):
            return
        gx = (x - i["x0"]) / (i["x1"] - i["x0"]) * i["width"]
        gy = (y - i["y0"]) / (i["y1"] - i["y0"]) * i["depth"]
        # a short pin standing on the surface at the selected point
        f = self.gxp.fold
        j, r = f.pixel(x, y)
        h = float(max(self.gxp.T[j, r], 0))
        z0 = i["base"] + min(h / i["zmax"], 1.0) * i["height"]
        pin = 0.18 * i["height"] + 0.02
        pts = np.array([[gx, gy, z0 + 0.004], [gx, gy, z0 + pin]])
        self._marker_item = gl.GLLinePlotItem(pos=pts, color=(0.85, 0.1, 0.1, 1), width=4, antialias=True)
        self.view.addItem(self._marker_item)

    def reset_camera(self):
        if self.view is None:
            return
        self.view.opts["center"] = QtGui.QVector3D(1.2, 0.5, 0.15)
        self.view.setCameraPosition(distance=3.2, elevation=32, azimuth=-112)

    def save_view(self):
        if self.view is None:
            return
        start = self.s.value("export_dir", "")
        fn, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save 3D view", start + "/3D_view.png", "PNG (*.png)")
        if fn:
            self.view.grabFramebuffer().save(fn)
