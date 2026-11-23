"""
NMR reading and analysis (1D).

Reads
  * Bruker experiment folders (raw ``fid`` + processed ``pdata/N/1r``/``1i``), a ``pdata/N``
    folder, or a .zip of any of these
  * TopSpin "totxt"/ASCII exports  (index, intensity, Hz, ppm)
  * JCAMP-DX (.jdx / .dx) 1D NMR
  * any two-column ppm,intensity text (via the CSV importer)

Analysis
  * Fourier transform / phase / line broadening / baseline correction of raw FIDs
  * integration of regions, normalised to a chosen number of protons
  * peak picking with parabolic interpolation
  * first-order multiplet analysis (s, d, t, q, quint, …, dd, dt, td, ddd, …) with J in Hz,
    AB (roofed) quartets, everything else reported as m
  * journal-style report strings
  * 13C carbon types from DEPT-135 (and optionally DEPT-90)
"""
from __future__ import annotations

import itertools
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass, field

import numpy as np

# -----------------------------------------------------------------------------
# Solvents: residual 1H, 13C (ppm) and the usual water peak (Fulmer et al. 2010)
# -----------------------------------------------------------------------------
SOLVENTS = {
    "CDCl3": {"1H": [7.26], "13C": [77.16], "water": 1.56, "label": "CDCl₃"},
    "DMSO": {"1H": [2.50], "13C": [39.52], "water": 3.33, "label": "DMSO-d₆"},
    "CD3OD": {"1H": [3.31], "13C": [49.00], "water": 4.87, "label": "CD₃OD"},
    "MeOD": {"1H": [3.31], "13C": [49.00], "water": 4.87, "label": "CD₃OD"},
    "D2O": {"1H": [4.79], "13C": [], "water": None, "label": "D₂O"},
    "Acetone": {"1H": [2.05], "13C": [29.84, 206.26], "water": 2.84, "label": "acetone-d₆"},
    "C6D6": {"1H": [7.16], "13C": [128.06], "water": 0.40, "label": "C₆D₆"},
    "CD3CN": {"1H": [1.94], "13C": [1.32, 118.26], "water": 2.13, "label": "CD₃CN"},
    "CD2Cl2": {"1H": [5.32], "13C": [53.84], "water": 1.52, "label": "CD₂Cl₂"},
    "THF": {"1H": [3.58, 1.72], "13C": [67.21, 25.31], "water": 2.46, "label": "THF-d₈"},
    "Tol": {"1H": [7.09, 7.01, 6.97, 2.08], "13C": [137.48, 128.87, 127.96, 125.13, 20.43],
            "water": 0.43, "label": "toluene-d₈"},
}


def solvent_key(name: str | None):
    if not name:
        return None
    n = name.replace("-", "").replace("_", "").lower()
    for k in SOLVENTS:
        if k.lower() in n or n in k.lower():
            return k
    if "dmso" in n:
        return "DMSO"
    if "acetone" in n:
        return "Acetone"
    if "methanol" in n:
        return "CD3OD"
    if "chloroform" in n:
        return "CDCl3"
    return None


def nucleus_label(nuc: str):
    m = re.match(r"(\d+)([A-Za-z]+)", nuc or "")
    if not m:
        return nuc or ""
    sup = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")
    return m.group(1).translate(sup) + m.group(2)


# -----------------------------------------------------------------------------
# Data container
# -----------------------------------------------------------------------------
@dataclass
class NMRSpectrum:
    ppm: np.ndarray                      # descending
    real: np.ndarray
    imag: np.ndarray | None = None
    nucleus: str = "1H"
    sf: float = 0.0                      # spectrometer frequency for this nucleus, MHz
    solvent: str | None = None
    title: str = ""
    name: str = ""
    path: str = ""
    source: str = ""
    pulprog: str = ""
    fid: np.ndarray | None = None        # complex FID after digital filter removal
    sw_hz: float = 0.0
    params: dict = field(default_factory=dict)   # lb, phc0, phc1, si, offset (ppm of point 0)
    meta: dict = field(default_factory=dict)

    @property
    def is_dept(self):
        return "dept" in self.pulprog.lower()

    @property
    def dept_angle(self):
        m = re.search(r"dept\D*?(45|90|135)", self.pulprog.lower())
        return int(m.group(1)) if m else (135 if self.is_dept else None)

    @property
    def kind(self):
        if self.is_dept:
            return f"DEPT-{self.dept_angle}"
        return self.nucleus

    @property
    def hz_per_point(self):
        return abs(self.ppm[0] - self.ppm[-1]) * self.sf / max(1, len(self.ppm) - 1)

    def mhz_label(self):
        return f"{self.sf:.0f} MHz" if self.sf else ""


# -----------------------------------------------------------------------------
# Reading
# -----------------------------------------------------------------------------
def _read_title(pdata_dir):
    fn = os.path.join(pdata_dir, "title")
    if os.path.exists(fn):
        try:
            return open(fn, encoding="latin-1").read().strip()
        except OSError:
            pass
    return ""


def find_bruker_experiments(root):
    """Return experiment folders (containing acqus) below root, sorted."""
    out = []
    for dp, dn, fn in os.walk(root):
        if "acqus" in fn and ("fid" in fn or "ser" in fn or "pdata" in dn):
            out.append(dp)
        dn[:] = [d for d in dn if d != "pdata"]
    return sorted(out, key=lambda p: (len(p), p))


def describe_experiment(exp):
    """(pulse programme, nucleus, title) for a chooser list."""
    import nmrglue as ng
    try:
        dic = ng.bruker.read_acqus_file(exp)
        a = dic.get("acqus", {})
    except Exception:
        a = {}
    pd = os.path.join(exp, "pdata", "1")
    return a.get("PULPROG", "?"), a.get("NUC1", "?"), _read_title(pd).splitlines()[0] if _read_title(pd) else ""


def unpack_zip(path):
    tmp = tempfile.mkdtemp(prefix="nmr_")
    with zipfile.ZipFile(path) as z:
        for m in z.infolist():
            target = os.path.realpath(os.path.join(tmp, m.filename))
            if not target.startswith(os.path.realpath(tmp)):
                continue                       # skip unsafe paths
            z.extract(m, tmp)
    return tmp


def read_bruker(path, procno=None, use_processed=True):
    """Read a Bruker experiment folder (or a pdata/N folder)."""
    import nmrglue as ng
    path = os.path.abspath(path)
    if os.path.basename(os.path.dirname(path)) == "pdata":
        procno = procno or os.path.basename(path)
        path = os.path.dirname(os.path.dirname(path))
    pdir = None
    if procno is None and os.path.isdir(os.path.join(path, "pdata")):
        nums = sorted((d for d in os.listdir(os.path.join(path, "pdata")) if d.isdigit()), key=int)
        procno = nums[0] if nums else None
    if procno is not None:
        pdir = os.path.join(path, "pdata", str(procno))

    acq = ng.bruker.read_acqus_file(path).get("acqus", {})
    nuc = acq.get("NUC1", "1H")
    sw_hz = float(acq.get("SW_h", 0) or 0)
    pulprog = acq.get("PULPROG", "")
    solvent = acq.get("SOLVENT", None)
    fid = None
    if os.path.exists(os.path.join(path, "fid")):
        try:
            dic, raw = ng.bruker.read(path)
            if raw.ndim == 1:
                fid = ng.bruker.remove_digital_filter(dic, raw)
        except Exception:
            fid = None

    title = _read_title(pdir) if pdir else ""
    name = os.path.basename(os.path.dirname(path)) + f"/{os.path.basename(path)}"
    have_1r = pdir and os.path.exists(os.path.join(pdir, "1r"))
    if use_processed and have_1r:
        pdic, comps = ng.bruker.read_pdata(pdir, all_components=True, scale_data=True)
        if isinstance(comps, (list, tuple)):
            re_, im_ = np.asarray(comps[0], float), np.asarray(comps[1], float) if len(comps) > 1 else None
        else:
            re_, im_ = np.asarray(comps, float), None
        p = pdic["procs"]
        si = int(p.get("SI", re_.size))
        sf = float(p["SF"])
        off = float(p["OFFSET"])
        swp = float(p["SW_p"])
        ppm = off - np.arange(si) * (swp / sf) / si
        params = dict(lb=float(p.get("LB", 0.3)), phc0=float(p.get("PHC0", 0)), phc1=float(p.get("PHC1", 0)),
                      si=si, offset=off, sw_p=swp)
        sp = NMRSpectrum(ppm=ppm, real=re_, imag=im_, nucleus=nuc, sf=sf, solvent=solvent, title=title,
                         name=name, path=path, source="Bruker (processed by TopSpin)", pulprog=pulprog,
                         fid=fid, sw_hz=sw_hz, params=params)
    elif fid is not None:
        sf = float(acq.get("BF1", 0) or acq.get("SFO1", 0))
        o1 = float(acq.get("O1", 0))
        params = dict(lb=1.0 if not nuc.startswith("1H") else 0.3, phc0=0.0, phc1=0.0,
                      si=int(2 ** np.ceil(np.log2(max(fid.size, 2)))), sw_p=sw_hz,
                      offset=(o1 + sw_hz / 2) / sf if sf else 0.0)
        sp = NMRSpectrum(ppm=np.zeros(1), real=np.zeros(1), nucleus=nuc, sf=sf, solvent=solvent, title=title,
                         name=name, path=path, source="Bruker raw FID (processed here)", pulprog=pulprog,
                         fid=fid, sw_hz=sw_hz, params=params)
        process(sp, autophase=True)
    else:
        raise ValueError("No fid or processed spectrum (pdata/1/1r) found in this folder.")
    sp.meta.update({"Pulse programme": pulprog, "Nucleus": nuc, "Solvent": solvent or "",
                    "Scans (NS)": acq.get("NS", ""), "Spectrometer": f"{sf:.2f} MHz",
                    "Sweep width": f"{sw_hz:.0f} Hz", "Time-domain points (TD)": acq.get("TD", ""),
                    "Temperature": f"{acq.get('TE', '')} K"})
    return sp


def process(sp: NMRSpectrum, lb=None, phc0=None, phc1=None, si=None, autophase=False, baseline=True):
    """(Re)process the FID: exponential multiplication, zero filling, FT, phase, baseline."""
    import nmrglue as ng
    if sp.fid is None:
        raise ValueError("No FID available")
    p = sp.params
    if lb is not None:
        p["lb"] = lb
    if si is not None:
        p["si"] = si
    x = sp.fid.copy()
    if p.get("lb"):
        x = ng.proc_base.em(x, p["lb"] / sp.sw_hz)
    x[0] *= 0.5
    x = ng.proc_base.zf_size(x, int(p["si"]))
    s = ng.proc_base.fft(x)
    if autophase:
        try:
            ph = _autophase(s)
            p["phc0"], p["phc1"] = ph
        except Exception:
            pass
    if phc0 is not None:
        p["phc0"] = phc0
    if phc1 is not None:
        p["phc1"] = phc1
    y = ng.proc_base.ps(s, p0=-p["phc0"], p1=-p["phc1"])[::-1]
    si = int(p["si"])
    sf = sp.sf
    off = p.get("offset", 0.0)
    sw_p = p.get("sw_p", sp.sw_hz)
    sp.ppm = off - np.arange(si) * (sw_p / sf) / si
    sp.real = y.real.copy()
    sp.imag = y.imag.copy()
    if baseline:
        sp.real = sp.real - estimate_baseline(sp.real)
    return sp


def _autophase(s):
    """Return (phc0, phc1) in the convention of the Bruker PHC0/PHC1 parameters, chosen by the ACME entropy criterion."""
    import nmrglue as ng
    import scipy.optimize as so

    def score(ph):
        y = ng.proc_base.ps(s, p0=-ph[0], p1=-ph[1])
        return _acme(y.real)
    best = None
    for p0 in range(-180, 180, 30):
        r = so.minimize(score, [p0, 0.0], method="Nelder-Mead",
                        options=dict(xatol=0.05, fatol=1e-10, maxiter=400))
        if best is None or r.fun < best.fun:
            best = r
    p0, p1 = best.x
    p0 = (p0 + 180) % 360 - 180
    return float(p0), float(p1)


def _acme(y, gamma=1e-4):
    d = np.abs(np.diff(y))
    h = d / d.sum() if d.sum() else d
    h = h[h > 0]
    ent = -np.sum(h * np.log(h))
    neg = y[y < 0]
    pen = gamma * np.sum(neg ** 2) / (np.sum(y ** 2) or 1) * 1e4
    return ent + pen


def rephase(sp: NMRSpectrum, d0, d1):
    """Apply an extra phase correction (degrees) to processed real+imag data."""
    if sp.imag is None:
        return False
    z = sp.real + 1j * sp.imag
    n = z.size
    # pivot at the left edge (point 0), as for the Bruker PHC1 parameter
    ph = np.deg2rad(d0 + d1 * np.arange(n) / n)
    z = z * np.exp(1j * ph)
    sp.real, sp.imag = z.real, z.imag
    sp.params["phc0"] = sp.params.get("phc0", 0) + d0
    sp.params["phc1"] = sp.params.get("phc1", 0) + d1
    return True


def noise_mask(y, win=None):
    """Points that look like baseline (no signal)."""
    n = y.size
    win = win or max(16, n // 512)
    d = np.abs(np.diff(y, prepend=y[0]))
    # rolling std of the first difference
    k = np.ones(win) / win
    local = np.sqrt(np.convolve(d ** 2, k, mode="same"))
    thr = np.median(local) * 3
    m = local < thr
    # erode so peak feet are excluded
    from scipy.ndimage import binary_erosion
    return binary_erosion(m, iterations=win // 2)


def estimate_baseline(y, order=5):
    m = noise_mask(y)
    if m.sum() < order * 10:
        return np.zeros_like(y)
    x = np.linspace(-1, 1, y.size)
    c = np.polynomial.chebyshev.chebfit(x[m], y[m], order)
    return np.polynomial.chebyshev.chebval(x, c)


def noise_level(y):
    m = noise_mask(y)
    v = y[m] if m.sum() > 50 else y
    return float(1.4826 * np.median(np.abs(v - np.median(v)))) or float(np.std(y)) or 1.0


def read_topspin_ascii(path):
    """TopSpin totxt / 'ascii-spec' export: index, intensity, Hz, ppm (or a header block)."""
    rows = []
    meta = {}
    with open(path, encoding="latin-1") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            if s[0] in "#;":
                kv = re.match(r"[#;]\s*([^:=]+)[:=]\s*(.*)", s)
                if kv:
                    meta[kv.group(1).strip()] = kv.group(2).strip()
                continue
            parts = re.split(r"[,\t; ]+", s)
            try:
                rows.append([float(v) for v in parts])
            except ValueError:
                continue
    a = np.array([r for r in rows if len(r) == len(rows[0])])
    if a.ndim != 2 or a.shape[0] < 10:
        raise ValueError("Not a recognised NMR text export")
    if a.shape[1] >= 4:
        intens, hz, ppm = a[:, 1], a[:, 2], a[:, 3]
        sf = float(np.median(hz[np.abs(ppm) > 0.5] / ppm[np.abs(ppm) > 0.5]))
    elif a.shape[1] == 2:
        ppm, intens = a[:, 0], a[:, 1]
        sf = 0.0
    else:
        ppm, intens = a[:, -1], a[:, 1]
        sf = 0.0
    if ppm[0] < ppm[-1]:
        ppm, intens = ppm[::-1], intens[::-1]
    nuc = "1H" if ppm.max() - ppm.min() < 40 else "13C"
    if sf and nuc == "1H" and sf < 150:
        nuc = "13C" if ppm.max() > 60 else nuc
    title = ""
    tfile = os.path.join(os.path.dirname(path), "title")
    if os.path.exists(tfile):
        title = open(tfile, encoding="latin-1").read().strip()
    return NMRSpectrum(ppm=ppm, real=intens.astype(float), nucleus=nuc, sf=sf, title=title,
                       name=os.path.splitext(os.path.basename(path))[0], path=path,
                       source="TopSpin ASCII export", meta=meta)


def read_jcamp(path):
    import nmrglue as ng
    dic, data = ng.jcampdx.read(path)
    if isinstance(data, list):
        data = data[0]
    data = np.asarray(data, float)
    udic = ng.jcampdx.guess_udic(dic, data)
    uc = ng.fileiobase.uc_from_udic(udic)
    ppm = uc.ppm_scale()
    sf = float(udic[0].get("obs", 0) or 0)
    nuc = str(dic.get(".OBSERVENUCLEUS", ["^1H"])[0]).replace("^", "")
    if ppm[0] < ppm[-1]:
        ppm, data = ppm[::-1], data[::-1]
    return NMRSpectrum(ppm=ppm, real=data, nucleus=nuc, sf=sf,
                       title=str(dic.get("TITLE", [""])[0]),
                       name=os.path.splitext(os.path.basename(path))[0], path=path, source="JCAMP-DX",
                       solvent=str(dic.get(".SOLVENTNAME", [""])[0]) or None)


def from_xy(x, y, name="spectrum", nucleus=None, sf=0.0):
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    if x[0] < x[-1]:
        x, y = x[::-1], y[::-1]
    nuc = nucleus or ("1H" if x.max() - x.min() < 40 else "13C")
    return NMRSpectrum(ppm=x, real=y, nucleus=nuc, sf=sf, name=name, source="Text/CSV import")


def looks_like_nmr(path):
    p = path.rstrip("/\\")
    if os.path.isdir(p):
        if os.path.exists(os.path.join(p, "acqus")) or os.path.exists(os.path.join(p, "procs")):
            return True
        return bool(find_bruker_experiments(p)) if len(os.listdir(p)) < 200 else False
    low = p.lower()
    if low.endswith(".zip"):
        try:
            with zipfile.ZipFile(p) as z:
                names = z.namelist()
            return any(n.endswith("/acqus") or n == "acqus" for n in names)
        except Exception:
            return False
    if low.endswith((".jdx", ".dx", ".jcamp")):
        try:
            head = open(p, encoding="latin-1").read(4000).upper()
            return "NMR" in head
        except OSError:
            return False
    return False


def read_any(path, choose=None):
    """Read a spectrum from a folder, zip, ASCII export or JCAMP. ``choose(list)`` picks an
    experiment when a folder/zip contains several (returns index or None)."""
    p = path.rstrip("/\\")
    low = p.lower()
    if low.endswith(".zip"):
        root = unpack_zip(p)
        exps = find_bruker_experiments(root)
        if not exps:
            raise ValueError("No Bruker experiment (acqus file) inside this zip.")
        exp = exps[0]
        if len(exps) > 1 and choose:
            k = choose(exps)
            if k is None:
                return None
            exp = exps[k]
        sp = read_bruker(exp)
        sp.name = os.path.splitext(os.path.basename(p))[0] + ("" if len(exps) == 1 else f" ({os.path.basename(exp)})")
        sp.path = p
        return sp
    if os.path.isdir(p):
        if os.path.exists(os.path.join(p, "acqus")) or os.path.basename(os.path.dirname(p)) == "pdata":
            return read_bruker(p)
        exps = find_bruker_experiments(p)
        if not exps:
            raise ValueError("No Bruker experiment found in this folder.")
        exp = exps[0]
        if len(exps) > 1 and choose:
            k = choose(exps)
            if k is None:
                return None
            exp = exps[k]
        return read_bruker(exp)
    if low.endswith((".jdx", ".dx", ".jcamp")):
        return read_jcamp(p)
    return read_topspin_ascii(p)


# -----------------------------------------------------------------------------
# Peaks, integrals, multiplets
# -----------------------------------------------------------------------------
@dataclass
class Peak:
    ppm: float
    height: float
    index: int
    ctype: str = ""          # 13C: C, CH, CH2, CH3, "CH or CH3", solvent
    note: str = ""


def pick_peaks(sp: NMRSpectrum, lo=None, hi=None, threshold=None, min_snr=8.0, negative=False):
    """Peak picking in [lo, hi] ppm. threshold in intensity units (default min_snr × noise)."""
    from scipy.signal import find_peaks
    y = sp.real
    noise = noise_level(y)
    thr = threshold if threshold is not None else min_snr * noise
    m = np.ones(y.size, bool)
    if lo is not None:
        m &= sp.ppm >= lo
    if hi is not None:
        m &= sp.ppm <= hi
    out = []
    signs = (1, -1) if negative else (1,)
    dist = max(1, int(round(0.25 / max(sp.hz_per_point, 1e-6)))) if sp.sf else 1
    for sgn in signs:
        idx, _ = find_peaks(sgn * y, height=thr, prominence=thr * 0.5, distance=dist)
        for i in idx:
            if not m[i] or i in (0, y.size - 1):
                continue
            out.append(Peak(_refine(sp.ppm, y, i), float(y[i]), int(i)))
    out.sort(key=lambda p: -p.ppm)
    return out


def _refine(x, y, i):
    y0, y1, y2 = y[i - 1], y[i], y[i + 1]
    den = y0 - 2 * y1 + y2
    dx = 0.5 * (y0 - y2) / den if den else 0.0
    dx = max(-0.5, min(0.5, dx))
    return float(x[i] + dx * (x[i + 1] - x[i]))


def integrate(sp: NMRSpectrum, lo, hi):
    m = (sp.ppm >= lo) & (sp.ppm <= hi)
    if m.sum() < 2:
        return 0.0
    x, y = sp.ppm[m][::-1], sp.real[m][::-1]
    return float(np.sum((y[1:] + y[:-1]) * np.diff(x)) / 2)


def auto_regions(sp: NMRSpectrum, min_snr=10.0, split_hz=19.0, sat_frac=0.03):
    """Find one region per multiplet. Lines further apart than ``split_hz`` start a new region
    (19 Hz keeps large couplings such as trans-alkene ~16 Hz together). Weak lines near a
    strong one (13C satellites, spinning sidebands) don't start regions. Returns [(lo, hi)]."""
    from scipy.ndimage import uniform_filter1d
    y = sp.real
    sf = sp.sf or 400.0
    noise = noise_level(y)
    pk = pick_peaks(sp, min_snr=min_snr)
    if not pk:
        return []
    pos = np.array([p.ppm for p in pk]) * sf
    h = np.array([p.height for p in pk])
    keep = np.ones(pos.size, bool)
    for i in range(pos.size):
        near = np.abs(pos - pos[i]) < 150
        if h[i] < sat_frac * h[near].max():
            keep[i] = False
    pos, h, idx = pos[keep], h[keep], np.array([p.index for p in pk])[keep]
    groups = [[0]]
    for i in range(1, pos.size):
        (groups[-1].append(i) if abs(pos[i - 1] - pos[i]) <= split_hz else groups.append([i]))
    hzpp = sp.hz_per_point if sp.sf else 0.2
    ys = uniform_filter1d(y, max(1, int(round(0.5 / max(hzpp, 1e-6)))))
    n = y.size
    spans = []
    for g in groups:
        i0, i1 = int(idx[g].min()), int(idx[g].max())      # i0 = highest ppm
        local_thr = max(2 * noise, 0.002 * h[g].max())
        reach = int(round(50.0 / max(hzpp, 1e-6)))      # stop before most 13C satellites
        a0, a1 = i0, i1
        while i0 > 0 and a0 - i0 < reach and ys[i0 - 1] > local_thr:
            i0 -= 1
        while i1 < n - 1 and i1 - a1 < reach and ys[i1 + 1] > local_thr:
            i1 += 1
        pad = int(round(1.5 / max(hzpp, 1e-6)))
        spans.append([max(0, i0 - pad), min(n - 1, i1 + pad), int(idx[g].min()), int(idx[g].max())])
    # don't let neighbouring regions overlap: cut at the lowest point between them
    for a_, b_ in zip(spans[:-1], spans[1:]):
        if a_[1] >= b_[0]:
            lo, hi = a_[3], b_[2]
            cut = lo + int(np.argmin(ys[lo:hi + 1])) if hi > lo else (lo + hi) // 2
            a_[1], b_[0] = cut, min(cut + 1, b_[1])
    return [(float(sp.ppm[j]), float(sp.ppm[i])) for i, j, *_ in spans]


# ---- multiplet analysis -------------------------------------------------------
NAMES = {1: "d", 2: "t", 3: "q", 4: "quint", 5: "sext", 6: "sept", 7: "oct"}
MAX_COUPLINGS = 3


def _pattern(js, ns):
    """Line positions (Hz, relative to centre) and intensities for couplings js with n equivalent nuclei each."""
    pos = np.array([0.0])
    inten = np.array([1.0])
    for J, n in zip(js, ns):
        from math import comb
        offs = (np.arange(n + 1) - n / 2) * J
        w = np.array([comb(n, k) for k in range(n + 1)], float)
        pos = (pos[:, None] + offs[None, :]).ravel()
        inten = (inten[:, None] * w[None, :]).ravel()
    return pos, inten


def _merge(pos, inten, tol):
    o = np.argsort(pos)
    pos, inten = pos[o], inten[o]
    P, I = [pos[0]], [inten[0]]
    for p, w in zip(pos[1:], inten[1:]):
        if p - P[-1] < tol:
            tot = I[-1] + w
            P[-1] = (P[-1] * I[-1] + p * w) / tot
            I[-1] = tot
        else:
            P.append(p)
            I.append(w)
    return np.array(P), np.array(I)


@dataclass
class Multiplet:
    lo: float                    # region, ppm
    hi: float
    integral: float = 0.0
    nh: float = 0.0              # normalised
    centre: float = 0.0          # ppm
    mult: str = "m"
    js: list = field(default_factory=list)       # Hz, largest first
    lines: list = field(default_factory=list)    # ppm of lines used
    note: str = ""
    manual: bool = False         # user edited mult / J
    ab: tuple | None = None      # (δA, δB, J) for AB quartets
    solvent: bool = False        # flagged as solvent / water: left out of the report

    def label(self):
        if self.mult == "m":
            return "m"
        return self.mult


def analyse_multiplet(freqs_hz, heights, hz_res=0.15, tol=None):
    """Find the simplest first-order pattern that reproduces the observed lines.

    freqs_hz, heights: observed line positions (any order) and heights.
    Returns (mult, [J…] largest first, centre_hz) or ('m', [], centre) when nothing fits.
    """
    f = np.asarray(freqs_hz, float)
    h = np.asarray(heights, float)
    o = np.argsort(f)
    f, h = f[o], np.abs(h[o])
    n = f.size
    centre_w = float(np.sum(f * h) / np.sum(h)) if h.sum() else float(f.mean())
    if n == 0:
        return "m", [], 0.0
    if n == 1:
        return "s", [], float(f[0])
    tol = tol or max(0.7, 4 * hz_res)
    width = f[-1] - f[0]
    centre = (f[0] + f[-1]) / 2
    rel = f - centre
    hn = h / h.max()
    # candidate J values: all pairwise spacings and their halves/thirds (for overlapping lines)
    diffs = np.abs(f[:, None] - f[None, :])[np.triu_indices(n, 1)]
    cands = set()
    for d in diffs:
        for k in (1, 2, 3):
            if 1.0 <= d / k <= 20.5:
                cands.add(round(d / k, 2))
    cands = sorted(cands)
    best = None
    multiplicity_sets = [(1,), (2,), (3,), (4,), (5,), (6,),
                         (1, 1), (1, 2), (2, 1), (1, 3), (3, 1), (2, 2),
                         (1, 1, 1), (1, 1, 2), (1, 2, 1), (2, 1, 1)]
    for ns in multiplicity_sets:
        k = len(ns)
        if (np.prod([m + 1 for m in ns]) < n) or (n == 2 and k > 1):
            continue
        for js in itertools.combinations_with_replacement(cands[::-1], k) if k > 1 else ((c,) for c in cands):
            js = tuple(js)
            span = sum(J * m for J, m in zip(js, ns))
            if abs(span - width) > tol * 1.5:
                continue
            P, I = _merge(*_pattern(js, ns), tol * 0.5)
            if P.size != n:
                continue
            err = np.abs(P - rel)
            if err.max() > tol:
                continue
            In = I / I.max()
            # intensity agreement (roofing makes this imperfect, so it's a soft score)
            ierr = float(np.mean(np.abs(In - hn)))
            score = k * 1.0 + ierr * 2.0 + err.mean() / tol * 0.5
            if best is None or score < best[0]:
                best = (score, ns, js, ierr)
        if best is not None and best[1] == ns and best[3] < 0.35:
            break          # simplest acceptable pattern found
    if best is None or best[3] > 0.6:
        return "m", [], centre_w
    _, ns, js, _ = best
    # refine J from the observed lines: least squares on the assignment
    js = list(js)
    pairs = sorted(zip(js, ns), key=lambda t: -t[0])
    name = "".join(NAMES[m] if len(pairs) == 1 else (NAMES[m] if m > 1 else "d") for J, m in pairs)
    if len(pairs) > 1:
        name = "".join({1: "d", 2: "t", 3: "q"}.get(m, "m") for J, m in pairs)
    return name, [float(round(J, 1)) for J, m in pairs], float(centre)


def analyse_ab(freqs_hz, heights, tol=0.7):
    """Detect an AB quartet (4 lines, equal outer spacings, inner lines stronger).
    Returns (δA_hz, δB_hz, J) or None."""
    f = np.sort(np.asarray(freqs_hz, float))
    if f.size != 4:
        return None
    h = np.asarray(heights, float)[np.argsort(freqs_hz)]
    s1, s2, s3 = np.diff(f)
    if abs(s1 - s3) > tol or not (2.0 < (s1 + s3) / 2 < 20.5):
        return None
    if not (h[1] > h[0] * 1.2 and h[2] > h[3] * 1.2):
        return None
    J = (s1 + s3) / 2
    if abs(s2 - J) < tol and abs(h[1] - h[0]) / h.max() < 0.3:
        return None                    # looks like a plain 1:1:1:1 dd/ q
    c = f.mean()
    dv = np.sqrt(max((f[3] - f[0]) * (f[2] - f[1]), 0.0))
    return c + dv / 2, c - dv / 2, J


def analyse_region(sp: NMRSpectrum, m: Multiplet, min_snr=5.0):
    """Fill centre/mult/J for a region from its picked peaks (unless user-edited)."""
    sf = sp.sf or 400.0
    noise = noise_level(sp.real)
    pk = pick_peaks(sp, m.lo, m.hi, threshold=None, min_snr=min_snr)
    if pk:
        top = max(p.height for p in pk)
        pk = [p for p in pk if p.height > max(min_snr * noise, 0.04 * top)]
    m.lines = [p.ppm for p in pk]
    m.integral = integrate(sp, m.lo, m.hi)
    if m.manual:
        return m
    m.ab = None
    if not pk:
        m.mult, m.js, m.centre = "m", [], (m.lo + m.hi) / 2
        return m
    fz = np.array([p.ppm for p in pk]) * sf
    hz = np.array([p.height for p in pk])
    ab = analyse_ab(fz, hz)
    if ab:
        a, b, J = ab
        m.ab = (float(a / sf), float(b / sf), float(round(J, 1)))
        m.mult, m.js, m.centre = "AB", [float(round(J, 1))], float(fz.mean() / sf)
        return m
    res = sp.hz_per_point if sp.sf else 0.2
    mult, js, c = analyse_multiplet(fz, hz, res)
    m.mult, m.js, m.centre = mult, js, c / sf
    return m


def mark_solvent(sp: NMRSpectrum, regions):
    """Flag regions containing only the residual solvent / water peak."""
    k = solvent_key(sp.solvent)
    if not k or not sp.nucleus.startswith("1H"):
        return
    shifts = list(SOLVENTS[k]["1H"]) + ([SOLVENTS[k]["water"]] if SOLVENTS[k]["water"] else [])
    for m in regions:
        hits = [s for s in shifts if m.lo - 0.01 <= s <= m.hi + 0.01]
        if not hits:
            continue
        if len(m.lines) <= 1 and m.lines and any(abs(m.lines[0] - s) < 0.03 for s in hits):
            m.solvent = True
            w = SOLVENTS[k]["water"]
            m.note = "water (H₂O/HOD)" if w is not None and abs(hits[0] - w) < 0.05 \
                else f"residual {SOLVENTS[k]['label']}"
        else:
            m.note = (m.note + "; " if m.note else "") + f"includes residual {SOLVENTS[k]['label']} ({hits[0]:.2f})"


def normalise(regions, ref_index=None, ref_value=None):
    """Scale integrals so region ref_index = ref_value H (default: smallest = 1)."""
    use = [m for m in regions if not m.solvent and m.integral > 0]
    if not use:
        return 0.0
    if ref_index is None or not (0 <= ref_index < len(regions)) or regions[ref_index].integral <= 0:
        ref = min(m.integral for m in use)
        factor = 1.0 / ref
    else:
        factor = (ref_value or 1.0) / regions[ref_index].integral
    for m in regions:
        m.nh = m.integral * factor
    return factor


def best_scale(integrals, max_n=4):
    """Find the factor that turns the integrals into (nearly) whole numbers of H.
    Returns (factor, index of the reference signal, its nH)."""
    I = np.asarray(integrals, float)
    best = None
    for k, ik in enumerate(I):
        for n in range(1, max_n + 1):
            f = n / ik
            x = I * f
            if np.any(x < 0.6):
                continue
            err = np.mean(np.abs(x - np.round(x)))
            score = err + 0.004 * np.round(x).sum()
            if best is None or score < best[0]:
                best = (score, f)
    if best is None:
        k = int(np.argmin(I))
        return 1 / I[k], k, 1
    f = best[1]
    x = I * f
    k = int(np.argmin(np.abs(x - np.round(x)) / np.maximum(np.round(x), 1)))
    return f, k, int(round(x[k]))


# ---- reports -------------------------------------------------------------------
def _fmt_j(js):
    if not js:
        return ""
    if len(js) == 1:
        return f"J = {js[0]:.1f} Hz"
    return "J = " + ", ".join(f"{j:.1f}" for j in js) + " Hz"


def multiplet_text(m: Multiplet, decimals=2, int_h=True, unicode=True):
    nh = f"{int(round(m.nh))}H" if int_h else f"{m.nh:.2f}H"
    if m.mult == "AB" and m.ab:
        a, b, J = m.ab
        return (f"{a:.{decimals}f} (d, J = {J:.1f} Hz, {max(1, int(round(m.nh / 2)))}H), "
                f"{b:.{decimals}f} (d, J = {J:.1f} Hz, {max(1, int(round(m.nh / 2)))}H)")
    if m.mult == "m":
        return f"{m.hi_shift(decimals)}–{m.lo_shift(decimals)} (m, {nh})" if (m.hi - m.lo) * 1 > 0 else ""
    js = _fmt_j(m.js)
    inner = ", ".join(x for x in (m.mult, js, nh) if x)
    return f"{m.centre:.{decimals}f} ({inner})"


def _hi_shift(self, d):
    v = max(self.lines) if self.lines else self.hi
    return f"{v:.{d}f}"


def _lo_shift(self, d):
    v = min(self.lines) if self.lines else self.lo
    return f"{v:.{d}f}"


Multiplet.hi_shift = _hi_shift
Multiplet.lo_shift = _lo_shift


def report_1h(sp: NMRSpectrum, regions, decimals=2):
    head = f"{nucleus_label(sp.nucleus)} NMR"
    bits = [sp.mhz_label()]
    k = solvent_key(sp.solvent)
    if k:
        bits.append(SOLVENTS[k]["label"])
    head += f" ({', '.join(b for b in bits if b)})"
    parts = [multiplet_text(m, decimals) for m in sorted(regions, key=lambda m: -m.centre) if not m.solvent]
    return f"{head} δ " + ", ".join(p for p in parts if p) + "."


def report_13c(sp: NMRSpectrum, peaks, decimals=1, with_types=False):
    head = f"{nucleus_label(sp.nucleus)} NMR"
    bits = [sp.mhz_label()]
    k = solvent_key(sp.solvent)
    if k:
        bits.append(SOLVENTS[k]["label"])
    head += f" ({', '.join(b for b in bits if b)})"
    vals = []
    use = [p for p in sorted(peaks, key=lambda p: -p.ppm) if p.ctype != "solvent"]
    # peaks that would print identically get one more decimal place
    txt = [f"{p.ppm:.{decimals}f}" for p in use]
    extra = {i for i in range(len(txt)) for j in range(len(txt)) if i != j and txt[i] == txt[j]}
    for i, p in enumerate(use):
        s = f"{p.ppm:.{decimals + (1 if i in extra else 0)}f}"
        if with_types and p.ctype:
            s += f" ({p.ctype.replace('CH or CH3', 'CH/CH₃').replace('CH3', 'CH₃').replace('CH2', 'CH₂')})"
        vals.append(s)
    return f"{head} δ " + ", ".join(vals) + "."


def label_solvent_carbons(sp: NMRSpectrum, peaks):
    k = solvent_key(sp.solvent)
    if not k:
        return
    for s in SOLVENTS[k]["13C"]:
        for p in peaks:
            # the CDCl3 triplet spans ±0.25 ppm, DMSO septet ±0.45
            if abs(p.ppm - s) < 0.5:
                p.ctype = "solvent"


def assign_dept(peaks13, dept135=None, dept90=None, tol=0.06, rel_min=0.05):
    """Set ctype on 13C peaks from DEPT spectra (NMRSpectrum objects)."""
    def lookup(sp):
        if sp is None:
            return None
        pk = pick_peaks(sp, threshold=None, min_snr=6, negative=True)
        if not pk:
            return []
        top = max(abs(p.height) for p in pk)
        return [p for p in pk if abs(p.height) >= rel_min * top]
    d135 = lookup(dept135)
    d90 = lookup(dept90)
    for p in peaks13:
        if p.ctype == "solvent":
            continue
        if d135 is None:
            continue
        m = [q for q in d135 if abs(q.ppm - p.ppm) < tol]
        if not m:
            p.ctype = "C"
            continue
        q = min(m, key=lambda q: abs(q.ppm - p.ppm))
        if q.height < 0:
            p.ctype = "CH2"
        else:
            if d90 is not None:
                p.ctype = "CH" if any(abs(r.ppm - p.ppm) < tol and r.height > 0 for r in d90) else "CH3"
            else:
                p.ctype = "CH or CH3"
    return peaks13
