# PyInstaller spec for WombatGC (Windows and macOS).  Run from the repository root:
#     pyinstaller packaging/wombatgc.spec
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
sys.path.insert(0, ROOT)
from common import APP_NAME, __version__  # noqa: E402

app_modules = ["wombat", "common", "gcms_viewer", "gcms_reader", "integration", "plots_export", "tables",
               "export_dialogs", "ms_library", "nist_search", "library_panel", "gcxgc", "gcxgc_view",
               "view_widgets", "view3d", "mesh_export", "colormaps", "nmr", "nmr_view", "spectra",
               "spectrum_view", "csv_import", "lcms", "lcms_view", "compare_view"]
hidden = app_modules + collect_submodules("pyqtgraph", filter=lambda m: ".examples" not in m and ".tests" not in m) \
    + collect_submodules("nmrglue", filter=lambda m: ".tests" not in m) \
    + collect_submodules("rainbow") + collect_submodules("brukeropusreader") + collect_submodules("opentfraw") \
    + ["OpenGL", "OpenGL.GL", "OpenGL.arrays.numpymodule", "openpyxl", "scipy.signal", "scipy.ndimage",
       "scipy.optimize", "scipy.io", "scipy.interpolate"]
datas = collect_data_files("pyqtgraph", excludes=["**/examples/**", "**/tests/**"]) \
    + collect_data_files("openpyxl") + [(os.path.join(ROOT, "icon.png"), ".")]

a = Analysis(
    [os.path.join(ROOT, "wombat.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "IPython", "jupyter", "pytest", "sphinx"],
    noarchive=False,
)
pyz = PYZ(a.pure)
icon_win = os.path.join(SPECPATH, "icon.ico")
icon_mac = os.path.join(SPECPATH, "icon.icns")
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name=APP_NAME,
    console=False,
    icon=icon_win if sys.platform == "win32" else icon_mac,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name=APP_NAME, upx=False)
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=icon_mac,
        bundle_identifier="uk.ac.plymouth.wombatgc",
        version=__version__,
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": __version__,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "NSRequiresAquaSystemAppearance": False,
        },
    )
