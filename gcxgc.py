"""
gcxgc.py - comprehensive 2D GC (GCxGC) folding and manual blob integration.

Folding: scan i at time t (s) belongs to modulation  j = floor((t + phase) / P)
and sits at second-dimension time  2tR = (t + phase) mod P.  The first-dimension
retention time (1tR) of a modulation is its start time, (j * P - phase) / 60 min,
the usual GCxGC convention.

Blob volume: for each modulation in the box, a baseline is drawn across that 2D slice
(straight line between the box edges, horizontal at the lower edge, or zero), and the
signal above it is summed and multiplied by the scan interval, giving counts·s.  This is
the same unit as 1D peak areas, so the volume of a modulated peak is comparable with the
area the same compound would give in 1D.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


class Fold:
    def __init__(self, times_min, period_s: float, phase_s: float = 0.0):
        t = np.asarray(times_min, float) * 60.0
        self.P = float(period_s)
        self.phase = float(phase_s)
        self.dt = float(np.median(np.diff(t))) if t.size > 1 else 0.01
        self.n2 = max(2, int(round(self.P / self.dt)))
        ph = t + self.phase
        col = np.floor(ph / self.P).astype(np.int64)
        self.col0 = int(col.min()) if col.size else 0
        self.col = col - self.col0
        self.row = np.clip((np.mod(ph, self.P) / self.dt).astype(np.int64), 0, self.n2 - 1)
        self.n1 = int(self.col.max()) + 1 if col.size else 1
        self.flat = self.col * self.n2 + self.row
        self.scan_map = np.full(self.n1 * self.n2, -1, dtype=np.int64)
        self.scan_map[self.flat] = np.arange(t.size)
        self.scan_map = self.scan_map.reshape(self.n1, self.n2)
        self.t = t

    # ---- coordinates ---------------------------------------------------------
    def col_start_min(self, j):
        """1tR (min) of modulation column j (start of the modulation)."""
        return ((np.asarray(j) + self.col0) * self.P - self.phase) / 60.0

    @property
    def extent(self):
        """(x0, x1, y0, y1): 1tR in minutes, 2tR in seconds."""
        x0 = float(self.col_start_min(0))
        return x0, x0 + self.n1 * self.P / 60.0, 0.0, self.n2 * self.dt

    def pixel(self, x_min, y_s):
        j = int(np.floor((x_min * 60.0 + self.phase) / self.P)) - self.col0
        r = int(np.floor(y_s / self.dt))
        return int(np.clip(j, 0, self.n1 - 1)), int(np.clip(r, 0, self.n2 - 1))

    def scan_at(self, x_min, y_s):
        j, r = self.pixel(x_min, y_s)
        s = self.scan_map[j, r]
        if s >= 0:
            return int(s)
        target = (j + self.col0) * self.P - self.phase + y_s
        return int(np.argmin(np.abs(self.t - target)))

    def coords_of_scan(self, i):
        """(1tR min, 2tR s) of scan i."""
        return float(self.col_start_min(self.col[i])), float(self.row[i] * self.dt)

    def box_pixels(self, x0, x1, y0, y1):
        (ja, ra), (jb, rb) = self.pixel(min(x0, x1), min(y0, y1)), self.pixel(max(x0, x1), max(y0, y1))
        return ja, jb, ra, rb

    def box_scans(self, j0, j1, r0, r1):
        s = self.scan_map[j0:j1 + 1, r0:r1 + 1].ravel()
        return np.sort(s[s >= 0])

    # ---- data ------------------------------------------------------------------
    def matrix(self, y):
        """Fold a per-scan signal into an (n1 modulations x n2 points) matrix."""
        y = np.asarray(y, float)
        return np.bincount(self.flat, weights=y, minlength=self.n1 * self.n2).reshape(self.n1, self.n2)


def transform(M, scale):
    """Display scaling for the 2D plot: linear, sqrt or log."""
    if scale == "sqrt":
        return np.sqrt(np.maximum(M, 0))
    if scale == "log":
        return np.log10(1 + np.maximum(M, 0))
    return M


BASELINES_2D = {
    "line": "Straight line across each modulation",
    "horizontal": "Horizontal at lower edge (each modulation)",
    "zero": "Zero",
}


@dataclass
class Blob:
    trace_key: str
    j0: int
    j1: int
    r0: int
    r1: int
    baseline: str
    rt1: float          # 1tR of apex modulation (min)
    rt2: float          # 2tR of apex (s)
    height: float
    volume: float       # counts·s
    apex_scan: int
    n_mod: int          # modulations carrying >5 % of the apex height
    note: str = ""
    match: int = 0
    library: str = ""


def integrate_box(fold: Fold, M: np.ndarray, j0, j1, r0, r1, trace_key, baseline="line"):
    j0, j1 = sorted((int(j0), int(j1)))
    r0, r1 = sorted((int(r0), int(r1)))
    if r1 - r0 < 2:
        return None
    sub = M[j0:j1 + 1, r0:r1 + 1].astype(float)
    if baseline == "zero":
        base = np.zeros_like(sub)
    elif baseline == "horizontal":
        base = np.minimum(sub[:, :1], sub[:, -1:]) * np.ones_like(sub)
    else:
        w = np.linspace(0.0, 1.0, sub.shape[1])[None, :]
        base = sub[:, :1] * (1 - w) + sub[:, -1:] * w
    above = sub - base
    k = int(np.argmax(above))
    ja, ra = divmod(k, sub.shape[1])
    height = float(above[ja, ra])
    volume = float(above.sum() * fold.dt)
    col_max = above.max(axis=1)
    n_mod = int((col_max > 0.05 * height).sum()) if height > 0 else 0
    apex_scan = int(fold.scan_map[j0 + ja, r0 + ra])
    return Blob(trace_key=trace_key, j0=j0, j1=j1, r0=r0, r1=r1, baseline=baseline,
                rt1=float(fold.col_start_min(j0 + ja)), rt2=float((r0 + ra) * fold.dt),
                height=height, volume=volume, apex_scan=apex_scan, n_mod=n_mod)


def blob_scans(fold: Fold, M: np.ndarray, blob: Blob, frac=0.10):
    """Scans inside the blob whose signal is at least `frac` of the apex: used for its spectrum."""
    sub = M[blob.j0:blob.j1 + 1, blob.r0:blob.r1 + 1]
    keep = sub >= frac * sub.max() if sub.size and sub.max() > 0 else np.zeros_like(sub, bool)
    s = fold.scan_map[blob.j0:blob.j1 + 1, blob.r0:blob.r1 + 1][keep]
    return np.sort(s[s >= 0])
