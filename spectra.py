"""
1D spectra (FT-IR, UV-Vis, Raman, mass spectra, generic x,y): readers, peak picking,
IR band descriptions and text reports.
"""
from __future__ import annotations

import csv
import io
import os
import re
from dataclasses import dataclass, field

import numpy as np

KINDS = {
    # key: (label, x label, y label, x reversed, default style)
    "ir": ("FT-IR spectrum", "Wavenumber (cm⁻¹)", "Transmittance (%)", True, "line"),
    "raman": ("Raman spectrum", "Raman shift (cm⁻¹)", "Intensity", False, "line"),
    "uvvis": ("UV-Vis spectrum", "Wavelength (nm)", "Absorbance", False, "line"),
    "fluor": ("Fluorescence spectrum", "Wavelength (nm)", "Intensity", False, "line"),
    "ms": ("Mass spectrum", "m/z", "Relative abundance (%)", False, "sticks"),
    "xrd": ("XRD pattern", "2θ (°)", "Intensity (counts)", False, "line"),
    "generic": ("x,y data", "x", "y", False, "line"),
}


@dataclass
class Spectrum:
    x: np.ndarray
    y: np.ndarray
    kind: str = "generic"
    name: str = ""
    path: str = ""
    source: str = ""
    x_label: str = ""
    y_label: str = ""
    y_mode: str = "raw"          # IR: "T" (fraction), "T%", "A"; others "raw"
    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        self.x = np.asarray(self.x, float)
        self.y = np.asarray(self.y, float)
        o = np.argsort(self.x)
        if not np.all(o == np.arange(o.size)):
            self.x, self.y = self.x[o], self.y[o]
        if not self.x_label:
            self.x_label = KINDS.get(self.kind, KINDS["generic"])[1]
        if not self.y_label:
            self.y_label = KINDS.get(self.kind, KINDS["generic"])[2]

    # ----- IR conversions -----
    def absorbance(self):
        if self.y_mode == "A":
            return self.y
        t = self.y / 100.0 if self.y_mode == "T%" else self.y
        return -np.log10(np.clip(t, 1e-6, None))

    def transmittance_pct(self):
        if self.y_mode == "T%":
            return self.y
        if self.y_mode == "T":
            return self.y * 100.0
        return 100.0 * 10 ** (-self.y)


# -----------------------------------------------------------------------------
# readers
# -----------------------------------------------------------------------------
def is_opus(path):
    ext = os.path.splitext(path)[1]
    if not ext[1:].isdigit():
        return False
    try:
        with open(path, "rb") as f:
            head = f.read(4)
        return head == b"\x0a\x0a\xfe\xfe"
    except OSError:
        return False


def read_opus(path):
    from brukeropusreader import read_file
    d = read_file(path)
    block = "AB" if "AB" in d else next((k for k in ("ScSm", "IgSm") if k in d), None)
    if block is None:
        raise ValueError("No spectrum block found in this OPUS file")
    par = d.get(f"{block} Data Parameter", {})
    n = int(par.get("NPT", len(d[block])))
    y = np.asarray(d[block], float)[:n]
    x = np.linspace(float(par.get("FXV", 0)), float(par.get("LXV", n)), n)
    acq = d.get("Acquisition", {})
    plf = str(acq.get("PLF", "")).upper()
    if block == "AB":
        if plf == "AB":
            mode = "A"
        elif plf == "TR":
            mode = "T" if np.nanmax(y) <= 1.5 else "T%"
        else:
            mode = "T" if 0 < np.nanmedian(y) <= 1.5 and np.nanmax(y) <= 1.5 else "A"
    else:
        mode = "raw"
    smp = d.get("Sample", {})
    opt = d.get("Optik", {})
    meta = {"Sample name": smp.get("SNM", ""), "Accessory": opt.get("ACC", ""),
            "Instrument": smp.get("SFM", ""), "Resolution": f"{acq.get('RES', '')} cm⁻¹",
            "Scans": acq.get("NSS", ""), "Detector": opt.get("DTC", ""),
            "Date": f"{par.get('DAT', '')} {par.get('TIM', '')}".strip(), "Block": block}
    name = smp.get("SNM") or os.path.splitext(os.path.basename(path))[0]
    sp = Spectrum(x=x, y=y, kind="ir", name=name, path=path, source="Bruker OPUS", y_mode=mode,
                  meta={k: v for k, v in meta.items() if v not in ("", None, " cm⁻¹")})
    return sp


def _jcamp_fields(text):
    fields = {}
    for m in re.finditer(r"^##([^=]+)=(.*)$", text, re.M):
        key = re.sub(r"[\s\-_/]", "", m.group(1)).upper()
        fields.setdefault(key, m.group(2).strip())
    return fields


def read_jcamp(path):
    """JCAMP-DX 1D (IR, UV-Vis, Raman, MS). Handles XYDATA (incl. compressed) and PEAK TABLE."""
    text = open(path, encoding="latin-1").read()
    f = _jcamp_fields(text)
    dtype = f.get("DATATYPE", f.get("DATA TYPE", "")).upper()
    xu = f.get("XUNITS", "").upper()
    yu = f.get("YUNITS", "").upper()
    x = y = None
    if "XYDATA" in f:
        try:
            import warnings
            import nmrglue as ng
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                dic, data = ng.jcampdx.read(path)
            yy = np.asarray(data[0] if isinstance(data, list) else data, float).ravel()
            if yy.size < 2 or not np.isfinite(yy).any():
                raise ValueError("nmrglue could not decode XYDATA")
            fx = float(f.get("FIRSTX", 0))
            lx = float(f.get("LASTX", yy.size - 1))
            x = np.linspace(fx, lx, yy.size)
            y = yy * (float(f.get("YFACTOR", 1) or 1) if abs(np.nanmax(np.abs(yy))) > 1e4 else 1.0)
        except Exception:
            x = y = None
        if x is None:
            # plain AFFN fallback: "x y1 y2 y3 ..."
            body = text.split("##XYDATA=", 1)[1].split("##", 1)[0].splitlines()[1:]
            xs, ys = [], []
            xf = float(f.get("XFACTOR", 1) or 1)
            yf = float(f.get("YFACTOR", 1) or 1)
            dx = (float(f["LASTX"]) - float(f["FIRSTX"])) / (int(float(f["NPOINTS"])) - 1)
            for line in body:
                vals = [float(v) for v in re.split(r"[\s,]+", line.strip()) if v]
                if not vals:
                    continue
                x0 = vals[0] * xf
                for k, v in enumerate(vals[1:]):
                    xs.append(x0 + k * dx)
                    ys.append(v * yf)
            x, y = np.array(xs), np.array(ys)
    elif "PEAKTABLE" in f or "XYPOINTS" in f:
        key = "##PEAK TABLE=" if "##PEAK TABLE=" in text else "##XYPOINTS="
        body = text.split(key, 1)[1].split("##", 1)[0].splitlines()[1:]
        pts = []
        for line in body:
            for pair in re.split(r"[;\s]+", line.strip()):
                if "," in pair:
                    a, b = pair.split(",")[:2]
                    try:
                        pts.append((float(a), float(b)))
                    except ValueError:
                        pass
        a = np.array(pts)
        xf = float(f.get("XFACTOR", 1) or 1)
        yf = float(f.get("YFACTOR", 1) or 1)
        x, y = a[:, 0] * xf, a[:, 1] * yf
    else:
        raise ValueError("No XYDATA or PEAK TABLE in this JCAMP file")
    if "INFRARED" in dtype or "1/CM" in xu:
        kind = "ir"
    elif "RAMAN" in dtype:
        kind = "raman"
    elif "MASS" in dtype:
        kind = "ms"
    elif "UV" in dtype or "NANOMETER" in xu:
        kind = "uvvis"
    else:
        kind = "generic"
    mode = "raw"
    if kind == "ir":
        if "ABSORB" in yu:
            mode = "A"
        elif "TRANSMIT" in yu:
            mode = "T%" if np.nanmax(y) > 1.5 else "T"
    sp = Spectrum(x=x, y=y, kind=kind, name=f.get("TITLE", os.path.splitext(os.path.basename(path))[0]),
                  path=path, source="JCAMP-DX", y_mode=mode,
                  meta={k: f[k] for k in ("ORIGIN", "OWNER", "DATE", "SAMPLINGPROCEDURE", "SPECTROMETERDATASYSTEM",
                                          "MOLFORM", "CASREGISTRYNO") if k in f})
    return sp


# -----------------------------------------------------------------------------
# delimited text (used by the CSV importer)
# -----------------------------------------------------------------------------
@dataclass
class Table:
    columns: list            # header names
    data: np.ndarray         # (rows, cols) float, NaN where not numeric
    delimiter: str
    decimal: str
    skipped: int             # header/comment lines skipped
    raw_header: list = field(default_factory=list)


def sniff_table(path, max_lines=200000):
    """Read a delimited text file of numbers with optional header lines.
    Detects delimiter (comma, semicolon, tab, spaces) and decimal comma."""
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            text = raw.decode(enc)
            if enc == "utf-16" and "\x00" in text[:200]:
                continue
            break
        except UnicodeDecodeError:
            continue
    lines = text.splitlines()[:max_lines]
    sample = [l for l in lines[:400] if l.strip()]

    def score(delim, dec):
        counts = []
        good = 0
        for l in sample:
            parts = _split(l, delim)
            nums = sum(1 for p in parts if _num(p, dec) is not None)
            if nums >= 1 and nums == len(parts):
                good += 1
                counts.append(len(parts))
        if not counts:
            return (0, 0)
        mode = max(set(counts), key=counts.count)
        return (good * (mode >= 2), mode)
    best = None
    for delim in (",", ";", "\t", " "):
        for dec in (".", ","):
            if delim == "," and dec == ",":
                continue
            sc = score(delim, dec)
            if best is None or sc > best[0]:
                best = (sc, delim, dec)
    (_, ncol), delim, dec = best
    if ncol < 1:
        raise ValueError("No columns of numbers found in this file")
    rows, header, skipped = [], [], 0
    first_numeric = None
    for i, l in enumerate(lines):
        if not l.strip():
            continue
        parts = _split(l, delim)
        vals = [_num(p, dec) for p in parts]
        if sum(v is not None for v in vals) >= max(1, len(parts) - 1) and len(parts) >= min(2, ncol):
            if first_numeric is None:
                first_numeric = i
            rows.append([v if v is not None else np.nan for v in vals])
        elif first_numeric is None:
            header.append(l)
            skipped += 1
    width = max(len(r) for r in rows)
    data = np.full((len(rows), width), np.nan)
    for r, row in enumerate(rows):
        data[r, :len(row)] = row
    cols = None
    if header:
        h = _split(header[-1], delim)
        if len(h) == width and not all(_num(x, dec) is not None for x in h):
            cols = [x.strip().strip('"') or f"Column {k + 1}" for k, x in enumerate(h)]
    if cols is None:
        cols = [f"Column {k + 1}" for k in range(width)]
    return Table(cols, data, delim, dec, skipped, header)


def _split(line, delim):
    if delim == " ":
        return line.split()
    return next(csv.reader(io.StringIO(line), delimiter=delim)) if '"' in line else line.split(delim)


def _num(s, dec):
    s = s.strip().strip('"')
    if not s:
        return None
    if dec == ",":
        s = s.replace(".", "").replace(",", ".") if s.count(",") == 1 and s.count(".") > 0 else s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def guess_kind(x, y, colnames=(), filename=""):
    """Best guess at what an x,y pair is, for the importer's default choice."""
    names = " ".join(colnames).lower() + " " + filename.lower()
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    if not x.size:
        return "generic"
    lo, hi = float(np.min(x)), float(np.max(x))
    if any(w in names for w in ("wavenumber", "cm-1", "cm^-1", "1/cm", "ftir", "ft-ir", "atr")):
        return "ir"
    if "raman" in names:
        return "raman"
    if any(w in names for w in ("ppm", "nmr", "chemical shift")):
        return "nmr"
    if any(w in names for w in ("m/z", "mz", "mass")):
        return "ms"
    if any(w in names for w in ("nm", "wavelength", "uv", "vis", "abs")) and 150 <= lo and hi <= 1200:
        return "uvvis"
    if any(w in names for w in ("2theta", "2θ", "xrd", "theta")):
        return "xrd"
    if any(w in names for w in ("time", "min", "rt", "retention", "tic")):
        return "chrom"
    if 350 <= lo and hi <= 4500 and hi - lo > 1500:
        return "ir"
    if -2 <= lo and hi <= 16 and x.size > 4000:
        return "nmr"
    if 150 <= lo and hi <= 1100 and x.size < 5000:
        return "uvvis"
    return "generic"


# -----------------------------------------------------------------------------
# analysis
# -----------------------------------------------------------------------------
@dataclass
class Band:
    x: float
    y: float                # value on the displayed scale at the band
    height: float           # on the analysis scale (absorbance for IR)
    width: float = 0.0      # FWHM in x units
    label: str = ""         # s / m / w (+ br)
    note: str = ""          # assignment typed by the user


def noise_level(y):
    d = np.diff(np.asarray(y, float))
    if d.size < 10:
        return float(np.std(y)) or 1.0
    return float(1.4826 * np.median(np.abs(d - np.median(d))) / np.sqrt(2)) or float(np.std(y)) or 1e-9


def pick_bands(x, a, lo=None, hi=None, min_rel=0.03, min_sep=None, max_n=60):
    """Peak maxima of a (absorbance-like) signal. min_rel: prominence as a fraction of the range."""
    from scipy.signal import find_peaks, peak_widths
    x = np.asarray(x, float)
    a = np.asarray(a, float)
    m = np.ones(x.size, bool)
    if lo is not None:
        m &= x >= lo
    if hi is not None:
        m &= x <= hi
    rng = float(np.nanmax(a[m]) - np.nanmin(a[m])) if m.any() else 1.0
    step = float(np.median(np.abs(np.diff(x)))) or 1.0
    dist = max(1, int(round((min_sep or 6 * step) / step)))
    idx, props = find_peaks(a, prominence=max(min_rel * rng, 4 * noise_level(a)), distance=dist)
    idx = np.array([i for i in idx if m[i] and 0 < i < x.size - 1], int)
    if idx.size == 0:
        return []
    if idx.size > max_n:
        from scipy.signal import peak_prominences
        prom = peak_prominences(a, idx)[0]
        idx = np.sort(idx[np.argsort(prom)[::-1][:max_n]])
    w = peak_widths(a, idx, rel_height=0.5)[0] * step
    out = []
    for i, wi in zip(idx, w):
        y0, y1, y2 = a[i - 1], a[i], a[i + 1]
        den = y0 - 2 * y1 + y2
        dx = 0.5 * (y0 - y2) / den if den else 0.0
        xi = float(x[i] + max(-0.5, min(0.5, dx)) * (x[i + 1] - x[i]))
        out.append(Band(xi, float(a[i]), float(a[i]), float(wi)))
    return out


def classify_ir(bands, a_all):
    """s / m / w by absorbance relative to the strongest band; 'br' when FWHM > 75 cm-1."""
    if not bands:
        return
    base = float(np.nanpercentile(a_all, 2))
    top = max(b.height - base for b in bands) or 1.0
    for b in bands:
        r = (b.height - base) / top
        lab = "s" if r >= 0.6 else ("m" if r >= 0.3 else "w")
        if b.width > 75:
            lab = "br" if r < 0.6 else "br, s"
        b.label = lab


def ir_report(bands, technique="ATR", order="desc", with_labels=True):
    vals = sorted(bands, key=lambda b: -b.x if order == "desc" else b.x)
    parts = []
    for b in vals:
        s = f"{b.x:.0f}"
        if with_labels and b.label:
            s += f" ({b.label})"
        if b.note:
            s += f" [{b.note}]"
        parts.append(s)
    head = f"IR ({technique}) νmax/cm⁻¹" if technique else "IR νmax/cm⁻¹"
    return f"{head} " + ", ".join(parts) + "."


def rubberband_baseline(x, y):
    """Lower convex hull of the spectrum (classic rubber-band baseline), for y where peaks go UP."""
    pts = np.column_stack([x, y])
    o = np.argsort(pts[:, 0])
    pts = pts[o]
    hull = []
    for p in pts:
        while len(hull) >= 2:
            (x1, y1), (x2, y2) = hull[-2], hull[-1]
            if (x2 - x1) * (p[1] - y1) - (y2 - y1) * (p[0] - x1) <= 0:
                hull.pop()
            else:
                break
        hull.append(tuple(p))
    hx, hy = np.array(hull).T
    base = np.interp(pts[:, 0], hx, hy)
    out = np.empty_like(base)
    out[o] = base
    return out


def integrate_xy(x, y, lo, hi, baseline="line"):
    m = (x >= lo) & (x <= hi)
    if m.sum() < 2:
        return 0.0, 0.0, 0.0
    xx, yy = x[m], y[m]
    if baseline == "line":
        b = yy[0] + (yy[-1] - yy[0]) * (xx - xx[0]) / (xx[-1] - xx[0])
    else:
        b = np.zeros_like(yy)
    above = yy - b
    area = float(np.sum((above[1:] + above[:-1]) * np.diff(xx)) / 2)
    k = int(np.argmax(above))
    return area, float(xx[k]), float(above[k])
