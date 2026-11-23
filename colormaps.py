"""
colormaps.py - colour schemes for the GCxGC plot, 3D view and 3D models.

A scheme is a list of (position 0-1, '#rrggbb') stops.
  * Three GCxGC presets with zero signal = white ("Mike's preference" - the default - plus a
    contour-figure scheme and a banded 3D scheme).
  * Ten presets shared with the "GC×GC 3D chromatogram from a single trace" page,
    including its diverging "Blue → Red" (dark blue → white → dark red).
  * A few standard extras.
"""
from __future__ import annotations

import json

import numpy as np

DEFAULT = "Mike's preference"
RENAMED = {}   # old saved scheme names -> current names

# Positioned presets: (position, colour).  Zero signal is white.
_POS = {
    # white -> pale blue -> blue -> navy, with orange/yellow on the most intense peaks
    # (contour-figure look)
    "GCxGC: white → blue → orange": [
        (0.00, "#ffffff"), (0.10, "#dde9ff"), (0.25, "#a9c8ff"), (0.42, "#76a0f0"), (0.58, "#4f71b0"),
        (0.74, "#1d3a78"), (0.86, "#142a5c"), (0.93, "#d9902f"), (1.00, "#ffd84a")],
    # white -> lavender blue -> navy -> teal -> green -> yellow -> red
    # (the default GCxGC scheme)
    "Mike's preference": [
        (0.00, "#ffffff"), (0.10, "#d9dcf0"), (0.22, "#9aa3d6"), (0.36, "#586495"), (0.48, "#1e3266"),
        (0.60, "#1f6a80"), (0.70, "#2f9a6a"), (0.80, "#8cc63f"), (0.88, "#f2d53c"), (0.94, "#e8862f"),
        (1.00, "#c8202f")],
    # 3D surface look: white floor -> navy -> green -> orange -> crimson -> blue -> teal -> green ->
    # yellow; the repeating bands work as height contours on tall peaks
    "Banded 3D: white → navy → rainbow bands": [
        (0.00, "#f4f4f4"), (0.04, "#c9ccd6"), (0.10, "#1e2940"), (0.22, "#3fa34d"), (0.34, "#e6952e"),
        (0.46, "#b8264e"), (0.58, "#2f6fb0"), (0.70, "#2fa08a"), (0.82, "#5cb83e"), (1.00, "#e8e04a")],
}

_HEX = {
    # --- as in the single-trace -> 3D page
    "Viridis": ["#440154", "#482878", "#3e4989", "#31688e", "#26828e", "#1f9e89", "#35b779", "#6ece58",
                "#b5de2b", "#fde725"],
    "Jet": ["#000083", "#003caa", "#05ffff", "#ffff00", "#fa0000", "#800000"],
    "Hot": ["#000000", "#e60000", "#ffd200", "#ffffff"],
    "Electric": ["#000000", "#1e0064", "#780064", "#a05a00", "#e6c800", "#fffadc"],
    "Portland": ["#0c3383", "#0a88ba", "#f2d338", "#f28f38", "#d91e1e"],
    "Plasma": ["#0d0887", "#5302a3", "#8b0aa5", "#b83289", "#db5c68", "#f48849", "#febd2a", "#f0f921"],
    "Inferno": ["#000004", "#280b54", "#65156e", "#9f2a63", "#d44842", "#f57d15", "#fac127", "#fcffa4"],
    "Turbo": ["#30123b", "#4662d7", "#36aaf9", "#1ae4b6", "#72fe5e", "#c8ef34", "#faba39", "#f66b19",
              "#ca2a04", "#7a0403"],
    "Greys": ["#000000", "#ffffff"],
    "Blue → Red": ["#08306b", "#ffffff", "#b2182b"],
    # --- extras
    "Magma": ["#000004", "#1c1044", "#4f127b", "#812581", "#b5367a", "#e55064", "#fb8761", "#fec287", "#fcfdbf"],
    "Cividis": ["#00224e", "#123570", "#3b496c", "#575d6d", "#707173", "#8a8779", "#a69d75", "#c4b56c",
                "#e4cf5b", "#fee838"],
    "Greys (white → black)": ["#ffffff", "#000000"],
}
PRESETS = {DEFAULT: _POS[DEFAULT]}
PRESETS.update(_POS)
PRESETS.update({k: [(i / (len(v) - 1), c) for i, c in enumerate(v)] for k, v in _HEX.items()})
CUSTOM = "Custom"


def hex_rgb(h):
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)])


def rgb_hex(rgb):
    return "#" + "".join(f"{int(round(max(0, min(1, c)) * 255)):02x}" for c in rgb)


def colours_at(stops, v):
    """Vectorised: map values 0-1 to RGB (… x 3), linear interpolation between stops."""
    v = np.clip(np.asarray(v, float), 0, 1)
    pos = np.array([p for p, _ in stops])
    cols = np.array([hex_rgb(c) for _, c in stops])
    out = np.empty(v.shape + (3,))
    for k in range(3):
        out[..., k] = np.interp(v, pos, cols[:, k])
    return out


def to_pyqtgraph(stops):
    import pyqtgraph as pg
    return pg.ColorMap([p for p, _ in stops], [tuple(int(x * 255) for x in hex_rgb(c)) for _, c in stops])


def to_matplotlib(stops, name="scheme"):
    from matplotlib.colors import LinearSegmentedColormap
    return LinearSegmentedColormap.from_list(name, [(p, c) for p, c in stops], N=512)


def srgb_to_linear(c):
    c = np.asarray(c, float)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def dumps(stops):
    return json.dumps([[float(p), c] for p, c in stops])


def loads(s):
    try:
        st = [(float(p), str(c)) for p, c in json.loads(s)]
        return sorted(st) if len(st) >= 2 else None
    except Exception:
        return None
