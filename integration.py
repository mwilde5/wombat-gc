"""
integration.py - manual peak integration on a chromatogram trace.

Area is the trapezoidal integral of (signal - baseline) between the chosen start and
end scans, with time in SECONDS, so units are counts·s.  Agilent software uses its own
scaling, so absolute numbers will differ from ChemStation/MassHunter reports, but
ratios between peaks (and calibration curves built from these areas) are unaffected.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

_trapz = getattr(np, "trapezoid", None) or np.trapz   # numpy 1.x / 2.x

BASELINES = {
    "line": "Straight line between start and end",
    "horizontal": "Horizontal at lower end point",
    "zero": "Zero (drop to x-axis)",
}


@dataclass
class Peak:
    trace_key: str       # e.g. "TIC" or "EIC 176±0.5"
    i0: int              # first scan index
    i1: int              # last scan index
    baseline: str        # key of BASELINES
    t_start: float
    t_end: float
    rt_apex: float
    height: float
    area: float
    b0: float            # baseline value at start
    b1: float            # baseline value at end
    note: str = ""
    label_pos: tuple | None = None   # label position (data coords), None = just above the apex
    label_manual: bool = False       # True when the user dragged it there (kept as-is in exports)

    def baseline_at(self, t):
        if self.t_end == self.t_start:
            return np.full_like(np.asarray(t, float), self.b0)
        return self.b0 + (self.b1 - self.b0) * (np.asarray(t, float) - self.t_start) / (self.t_end - self.t_start)


def snap_to_valley(y: np.ndarray, i: int, window: int) -> int:
    """Move index i to the lowest point within ±window scans."""
    if window <= 0:
        return i
    a, b = max(0, i - window), min(len(y), i + window + 1)
    return a + int(np.argmin(y[a:b]))


def integrate(times: np.ndarray, y: np.ndarray, t0: float, t1: float, trace_key: str,
              baseline: str = "line", snap_window: int = 0) -> Peak | None:
    """Integrate trace y between retention times t0 and t1 (minutes)."""
    t0, t1 = sorted((t0, t1))
    i0 = int(np.argmin(np.abs(times - t0)))
    i1 = int(np.argmin(np.abs(times - t1)))
    if snap_window:
        # keep the two ends from snapping past each other
        w = min(snap_window, max(0, (i1 - i0) // 3))
        i0, i1 = snap_to_valley(y, i0, w), snap_to_valley(y, i1, w)
        i0, i1 = sorted((i0, i1))
    if i1 - i0 < 2:
        return None
    t = times[i0:i1 + 1]
    seg = y[i0:i1 + 1].astype(float)
    if baseline == "zero":
        b0 = b1 = 0.0
    elif baseline == "horizontal":
        b0 = b1 = float(min(seg[0], seg[-1]))
    else:
        b0, b1 = float(seg[0]), float(seg[-1])
    base = b0 + (b1 - b0) * (t - t[0]) / (t[-1] - t[0])
    above = seg - base
    area = float(_trapz(above, t * 60.0))
    k = int(np.argmax(above))
    return Peak(trace_key=trace_key, i0=i0, i1=i1, baseline=baseline,
                t_start=float(t[0]), t_end=float(t[-1]), rt_apex=float(t[k]),
                height=float(above[k]), area=area, b0=b0, b1=b1)


def recompute(peak: Peak, times: np.ndarray, y: np.ndarray) -> Peak:
    """Re-integrate an existing peak on (possibly changed) data, keeping its bounds."""
    p = integrate(times, y, times[peak.i0], times[peak.i1], peak.trace_key, peak.baseline, 0)
    if p is not None:
        p.note = peak.note
    return p or peak
