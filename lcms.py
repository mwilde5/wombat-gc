"""
LC-MS reading (Thermo .raw via opentfraw, mzML, mzXML), polarity / MS-level segments, and
exact-mass helpers (formula -> monoisotopic mass -> adduct m/z).

Each segment (e.g. positive-mode MS1) becomes a gcms_reader.GCMSRun, so the chromatography
window can show it; extracted-ion tolerances are in ppm.
"""
from __future__ import annotations

import base64
import os
import re
import zlib
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

import numpy as np

import gcms_reader as gr


@dataclass
class LCData:
    path: str
    name: str
    source: str
    rt: np.ndarray                 # minutes, per scan
    polarity: np.ndarray           # '+', '-' or '?'
    ms_level: np.ndarray           # int
    filters: list
    precursor: np.ndarray          # m/z or nan
    tic: np.ndarray
    mz: np.ndarray
    intensity: np.ndarray
    offsets: np.ndarray
    meta: dict = field(default_factory=dict)

    def segments(self):
        """[(key, label, n_scans)] - key = (polarity, ms_level)."""
        out = []
        for pol in ("+", "-", "?"):
            for lvl in sorted(set(int(x) for x in self.ms_level)):
                n = int(np.sum((self.polarity == pol) & (self.ms_level == lvl)))
                if n:
                    name = {"+": "Positive", "-": "Negative", "?": "Unknown polarity"}[pol]
                    out.append(((pol, lvl), f"{name} mode · MS{lvl if lvl > 1 else ''}{' (full scan)' if lvl == 1 else ''}"
                                f" · {n} scans", n))
        return out


class HRRun(gr.GCMSRun):
    """GCMSRun whose EIC tolerance is in ppm (high-resolution data)."""

    def eic(self, target_mz: float, tol: float = 5.0) -> np.ndarray:
        da = abs(target_mz) * tol * 1e-6
        a = np.searchsorted(self._sorted_mz, target_mz - da, side="left")
        b = np.searchsorted(self._sorted_mz, target_mz + da, side="right")
        sel = self._sorted_order[a:b]
        return np.bincount(self._scan_index[sel], weights=self.intensity[sel], minlength=self.n_scans)

    def __post_init__(self):
        super().__post_init__()
        self._sorted_order = np.argsort(self.mz, kind="stable")
        self._sorted_mz = self.mz[self._sorted_order]


def make_run(d: LCData, key):
    pol, lvl = key
    sel = np.nonzero((d.polarity == pol) & (d.ms_level == lvl))[0]
    lens = np.diff(d.offsets)[sel]
    offs = np.concatenate([[0], np.cumsum(lens)]).astype(np.int64)
    if sel.size == d.rt.size:
        idx = slice(None)                                   # every scan: no copy needed
    else:
        idx = np.concatenate([np.arange(d.offsets[i], d.offsets[i + 1]) for i in sel]) if sel.size else np.zeros(0, int)
    meta = dict(d.meta)
    meta.update({"mz_grid": 0.002, "mz_decimals": 5, "mz_label_decimals": 4, "polarity": pol, "ms_level": lvl,
                 "filters": sorted(set(d.filters[i] for i in sel))[:5]})
    label = {"+": "+ve", "-": "−ve", "?": ""}[pol] + (f" MS{lvl}" if lvl > 1 else "")
    run = HRRun(path=d.path, name=d.name, source=f"{d.source} · {label}".strip(" ·"),
                times=d.rt[sel], mz=d.mz[idx], intensity=d.intensity[idx], offsets=offs, metadata=meta,
                stored_tic=d.tic[sel] if np.all(np.isfinite(d.tic[sel])) else None)
    run.precursor = d.precursor[sel]
    run.filters = [d.filters[i] for i in sel]
    return run


# -----------------------------------------------------------------------------
# readers
# -----------------------------------------------------------------------------
def read_thermo(path, progress=None):
    import opentfraw
    r = opentfraw.RawFile(path)
    st = r.scan_table()
    n = len(st["scan_number"])
    mzs, ins, offs = [], [], [0]
    for k, s in enumerate(r.iter_scans()):
        mz = np.asarray(s["mz"], float)
        it = np.asarray(s["intensity"], float)
        mzs.append(mz)
        ins.append(it)
        offs.append(offs[-1] + mz.size)
        if progress and k % 200 == 0:
            progress(k, n)
    pol = np.array([p if p in ("+", "-") else "?" for p in st["polarity"]])
    prec = np.array([np.nan if p is None else float(p) for p in st["precursor_mz"]])
    meta = {"Instrument": getattr(r, "instrument_model", "") or "", "Acquired": str(getattr(r, "acquisition_date", "") or "")}
    try:
        si = r.sample_info if not callable(r.sample_info) else r.sample_info()
        if isinstance(si, dict):
            for k in ("sample_name", "sample_id", "comment", "vial"):
                if si.get(k):
                    meta[k.replace("_", " ").capitalize()] = si[k]
    except Exception:
        pass
    return LCData(path=path, name=os.path.splitext(os.path.basename(path))[0],
                  source="Thermo .raw (opentfraw)", rt=np.asarray(st["retention_time"], float),
                  polarity=pol, ms_level=np.asarray(st["ms_level"], int), filters=list(st["filter_string"]),
                  precursor=prec, tic=np.asarray(st["total_ion_current"], float),
                  mz=np.concatenate(mzs) if mzs else np.zeros(0), intensity=np.concatenate(ins) if ins else np.zeros(0),
                  offsets=np.asarray(offs, np.int64), meta=meta)


def _decode(text, bits64, compressed, network_order=False):
    raw = base64.b64decode(text or b"")
    if compressed:
        raw = zlib.decompress(raw)
    dt = np.dtype((">" if network_order else "<") + ("f8" if bits64 else "f4"))
    return np.frombuffer(raw, dtype=dt).astype(float)


def read_mzml(path, progress=None):
    ns = ""
    rts, pols, lvls, filt, prec, tics, mzs, ins, offs = [], [], [], [], [], [], [], [], [0]
    for ev, el in ET.iterparse(path, events=("end",)):
        tag = el.tag.rsplit("}", 1)[-1]
        if tag != "spectrum":
            continue
        cv = {}
        for p in el.iter():
            if p.tag.endswith("cvParam"):
                cv[p.get("accession")] = (p.get("value"), p.get("unitAccession") or p.get("unitName"))
        lvl = int(cv.get("MS:1000511", ("1", None))[0] or 1)
        pol = "+" if "MS:1000130" in cv else ("-" if "MS:1000129" in cv else "?")
        rt_v, rt_u = cv.get("MS:1000016", ("0", None))
        rt = float(rt_v or 0)
        if rt_u in ("UO:0000010", "second"):
            rt /= 60.0
        arrays = {}
        for bda in el.iter():
            if not bda.tag.endswith("binaryDataArray"):
                continue
            acc = {p.get("accession") for p in bda if p.tag.endswith("cvParam")}
            b = next((x for x in bda if x.tag.endswith("binary")), None)
            kind = "mz" if "MS:1000514" in acc else ("int" if "MS:1000515" in acc else None)
            if kind and b is not None:
                arrays[kind] = _decode(b.text, "MS:1000523" in acc, "MS:1000574" in acc)
        mz = arrays.get("mz", np.zeros(0))
        it = arrays.get("int", np.zeros(0))
        n = min(mz.size, it.size)
        mzs.append(mz[:n])
        ins.append(it[:n])
        offs.append(offs[-1] + n)
        rts.append(rt)
        pols.append(pol)
        lvls.append(lvl)
        filt.append(cv.get("MS:1000512", ("", None))[0] or "")
        prec.append(float(cv["MS:1000744"][0]) if "MS:1000744" in cv else np.nan)
        tic = cv.get("MS:1000285", (None, None))[0]
        tics.append(float(tic) if tic else float(it[:n].sum()))
        el.clear()
        if progress and len(rts) % 200 == 0:
            progress(len(rts), 0)
    return LCData(path=path, name=os.path.splitext(os.path.basename(path))[0], source="mzML",
                  rt=np.array(rts), polarity=np.array(pols), ms_level=np.array(lvls, int), filters=filt,
                  precursor=np.array(prec, float), tic=np.array(tics),
                  mz=np.concatenate(mzs) if mzs else np.zeros(0), intensity=np.concatenate(ins) if ins else np.zeros(0),
                  offsets=np.asarray(offs, np.int64))


def read_mzxml(path, progress=None):
    rts, pols, lvls, filt, prec, tics, mzs, ins, offs = [], [], [], [], [], [], [], [], [0]
    for ev, el in ET.iterparse(path, events=("end",)):
        if el.tag.rsplit("}", 1)[-1] != "scan":
            continue
        lvl = int(el.get("msLevel", 1))
        pol = el.get("polarity", "?")
        pol = pol if pol in ("+", "-") else "?"
        m = re.match(r"PT([\d.]+)S", el.get("retentionTime", "PT0S"))
        rt = float(m.group(1)) / 60.0 if m else 0.0
        pk = next((x for x in el if x.tag.endswith("peaks")), None)
        if pk is not None and pk.text:
            data = _decode(pk.text, pk.get("precision", "32") == "64", pk.get("compressionType") == "zlib",
                           network_order=True)
            mz, it = data[0::2], data[1::2]
        else:
            mz = it = np.zeros(0)
        pr = next((x for x in el if x.tag.endswith("precursorMz")), None)
        mzs.append(mz)
        ins.append(it)
        offs.append(offs[-1] + mz.size)
        rts.append(rt)
        pols.append(pol)
        lvls.append(lvl)
        filt.append(el.get("filterLine", ""))
        prec.append(float(pr.text) if pr is not None and pr.text else np.nan)
        tics.append(float(el.get("totIonCurrent", it.sum())))
        # child <scan> elements (MS2 nested in MS1) are handled by their own end events
        for c in list(el):
            if c.tag.endswith("peaks"):
                el.remove(c)
    return LCData(path=path, name=os.path.splitext(os.path.basename(path))[0], source="mzXML",
                  rt=np.array(rts), polarity=np.array(pols), ms_level=np.array(lvls, int), filters=filt,
                  precursor=np.array(prec, float), tic=np.array(tics),
                  mz=np.concatenate(mzs) if mzs else np.zeros(0), intensity=np.concatenate(ins) if ins else np.zeros(0),
                  offsets=np.asarray(offs, np.int64))


def read_any(path, progress=None):
    low = path.lower()
    if low.endswith(".raw"):
        return read_thermo(path, progress)
    if low.endswith(".mzml"):
        return read_mzml(path, progress)
    if low.endswith(".mzxml"):
        return read_mzxml(path, progress)
    raise ValueError("LC-MS files must be Thermo .raw, .mzML or .mzXML")


# -----------------------------------------------------------------------------
# exact masses
# -----------------------------------------------------------------------------
MONO = {"H": 1.00782503207, "C": 12.0, "N": 14.0030740048, "O": 15.99491461956, "S": 31.97207100,
        "P": 30.97376163, "F": 18.99840322, "Cl": 34.96885268, "Br": 78.9183371, "I": 126.904473,
        "Na": 22.9897692809, "K": 38.96370668, "Si": 27.9769265325, "B": 11.0093054, "Se": 79.9165213,
        "Li": 7.01600455, "Mg": 23.9850417, "Ca": 39.96259098, "Fe": 55.9349375, "Cu": 62.9295975,
        "Zn": 63.9291422, "D": 2.01410178}
ELECTRON = 0.00054857990946
ADDUCTS = {
    "[M+H]+": (1, MONO["H"] - ELECTRON, 1),
    "[M+Na]+": (1, MONO["Na"] - ELECTRON, 1),
    "[M+K]+": (1, MONO["K"] - ELECTRON, 1),
    "[M+NH4]+": (1, MONO["N"] + 4 * MONO["H"] - ELECTRON, 1),
    "[M+2H]2+": (1, 2 * (MONO["H"] - ELECTRON), 2),
    "[2M+H]+": (2, MONO["H"] - ELECTRON, 1),
    "[M]+ (radical cation)": (1, -ELECTRON, 1),
    "[M-H]-": (1, -(MONO["H"] - ELECTRON), 1),
    "[M+Cl]-": (1, MONO["Cl"] + ELECTRON, 1),
    "[M+HCOO]-": (1, MONO["C"] + MONO["H"] + 2 * MONO["O"] + ELECTRON, 1),
    "[M-2H]2-": (1, -2 * (MONO["H"] - ELECTRON), 2),
    "[2M-H]-": (2, -(MONO["H"] - ELECTRON), 1),
}


def parse_formula(f):
    """'C10H12O2', 'C6H5Cl', 'CH3(CH2)2OH' -> {element: count}."""
    f = f.replace(" ", "")

    def parse(s, i=0):
        counts = {}
        while i < len(s):
            ch = s[i]
            if ch == "(":
                sub, i = parse(s, i + 1)
                m = re.match(r"\d+", s[i:])
                k = int(m.group()) if m else 1
                i += len(m.group()) if m else 0
                for e, c in sub.items():
                    counts[e] = counts.get(e, 0) + c * k
            elif ch == ")":
                return counts, i + 1
            else:
                m = re.match(r"([A-Z][a-z]?)(\d*)", s[i:])
                if not m:
                    raise ValueError(f"Can't read the formula at '{s[i:]}'")
                e, n = m.group(1), int(m.group(2) or 1)
                if e not in MONO:
                    raise ValueError(f"Unknown element {e}")
                counts[e] = counts.get(e, 0) + n
                i += len(m.group())
        return counts, i
    counts, _ = parse(f)
    return counts


def mono_mass(formula):
    return sum(MONO[e] * n for e, n in parse_formula(formula).items())


def adduct_mz(formula_or_mass, adduct):
    M = float(formula_or_mass) if isinstance(formula_or_mass, (int, float)) else mono_mass(formula_or_mass)
    nM, add, z = ADDUCTS[adduct]
    return (nM * M + add) / z


def ppm_error(measured, theoretical):
    return (measured - theoretical) / theoretical * 1e6
