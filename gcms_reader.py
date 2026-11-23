"""
gcms_reader.py - read Agilent GC-MS .D folders (ChemStation and MassHunter).

Supported:
  * ChemStation / MSD ChemStation   ->  <name>.D/DATA.MS   (own reader, raw 0.05 Da m/z kept)
  * MassHunter GC/MS (quadrupole)   ->  <name>.D/AcqData/MSScan.bin + MSPeak.bin  (via rainbow-api)
  * MassHunter profile-only data    ->  <name>.D/AcqData/MSProfile.bin            (via rainbow-api)
  * Agilent detector signals        ->  <name>.D/FID1A.ch, FID2B.ch, TCD… (ChemStation / OpenLab, via rainbow-api)
  * ANDI-MS netCDF (.cdf)           ->  any vendor's export, e.g. ChromSpace (Markes BenchTOF),
                                        ChromaTOF, MassHunter, Xcalibur; 1D or GCxGC

MassHunter GC/MS Acquisition usually writes BOTH a data.ms and an AcqData folder.
By default the AcqData (MassHunter) data are preferred; pass prefer="chemstation"
to use data.ms instead.

Every run is held as per-scan peak lists packed into flat arrays, so spectra keep
the m/z precision the instrument stored and EICs are quick to compute.
"""
from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass, field

import numpy as np

# ChemStation abundance encoding: top 2 bits = exponent (power of 8), low 14 bits = mantissa
_POW8 = np.array([1, 8, 64, 512], dtype=np.uint64)


@dataclass
class GCMSRun:
    path: str
    name: str
    source: str                 # human-readable description of the file actually read
    times: np.ndarray           # retention time per scan, minutes
    mz: np.ndarray              # all m/z values, concatenated scan after scan
    intensity: np.ndarray       # matching abundances (float64)
    offsets: np.ndarray         # scan i occupies mz[offsets[i]:offsets[i+1]]
    metadata: dict = field(default_factory=dict)
    stored_tic: np.ndarray | None = None   # TIC as recorded by the instrument (pre-compression)

    # ---- derived quantities -------------------------------------------------
    def __post_init__(self):
        self._scan_index = np.repeat(np.arange(len(self.times)), np.diff(self.offsets))
        summed = np.bincount(self._scan_index, weights=self.intensity,
                             minlength=len(self.times))
        # Use the instrument's own TIC when present so exports match ChemStation /
        # MassHunter exactly; otherwise sum the stored peaks.
        if self.stored_tic is not None and len(self.stored_tic) == len(self.times):
            self.tic = np.asarray(self.stored_tic, dtype=float)
        else:
            self.tic = summed

    @property
    def n_scans(self) -> int:
        return len(self.times)

    @property
    def has_spectra(self) -> bool:
        """False for single-channel detectors (FID, TCD, ECD...)."""
        return self.mz.size > 0

    @property
    def signal_label(self) -> str:
        if self.has_spectra:
            return "Abundance"
        u = self.metadata.get("signal_unit", "")
        return f"Signal ({u})" if u else "Signal"

    @property
    def trace_name(self) -> str:
        return "TIC" if self.has_spectra else self.metadata.get("detector", "Signal")

    def scan_at(self, rt: float) -> int:
        """Index of the scan closest to a retention time (min)."""
        return int(np.argmin(np.abs(self.times - rt)))

    def spectrum(self, i: int):
        """Raw (m/z, abundance) of one scan."""
        a, b = self.offsets[i], self.offsets[i + 1]
        return self.mz[a:b].copy(), self.intensity[a:b].copy()

    def spectrum_range(self, i0: int, i1: int, bin_width: float | None = None):
        """
        Average spectrum over scans i0..i1 (inclusive).
        Peaks are merged on a grid of `bin_width` Da (default: the finest grid the
        data allow - 0.05 Da for ChemStation, 0.01 Da for MassHunter centroids).
        """
        i0, i1 = sorted((int(i0), int(i1)))
        if i0 == i1 and bin_width is None:
            return self.spectrum(i0)
        a, b = self.offsets[i0], self.offsets[i1 + 1]
        mz, inten = self.mz[a:b], self.intensity[a:b]
        if mz.size == 0:
            return mz.copy(), inten.copy()
        bw = bin_width or self.metadata.get("mz_grid", 0.01)
        keys = np.round(mz / bw).astype(np.int64)
        uk, inv = np.unique(keys, return_inverse=True)
        summed = np.bincount(inv, weights=inten)
        # intensity-weighted m/z inside each bin
        mz_w = np.bincount(inv, weights=mz * inten) / np.where(summed > 0, summed, 1)
        mz_w = np.where(summed > 0, mz_w, uk * bw)
        return mz_w, summed / (i1 - i0 + 1)

    def spectrum_scans(self, idx, bin_width=None):
        """
        Average spectrum over an arbitrary set of scan indices (e.g. a GCxGC box).
        Contiguous runs of scans are sliced directly, so this is fast even for big boxes.
        """
        idx = np.unique(np.asarray(idx, dtype=np.int64))
        if idx.size == 0:
            return np.zeros(0), np.zeros(0)
        if idx.size == 1 and bin_width is None:
            return self.spectrum(int(idx[0]))
        if idx[-1] - idx[0] + 1 == idx.size:
            return self.spectrum_range(int(idx[0]), int(idx[-1]), bin_width)
        breaks = np.nonzero(np.diff(idx) != 1)[0]
        starts = np.concatenate([[idx[0]], idx[breaks + 1]])
        ends = np.concatenate([idx[breaks], [idx[-1]]])
        sl = [np.arange(self.offsets[a], self.offsets[b + 1]) for a, b in zip(starts, ends)]
        pts = np.concatenate(sl) if sl else np.zeros(0, dtype=np.int64)
        mz, inten = self.mz[pts], self.intensity[pts]
        if mz.size == 0:
            return mz.astype(float), inten.astype(float)
        bw = bin_width or self.metadata.get("mz_grid", 0.01)
        keys = np.round(mz / bw).astype(np.int64)
        uk, inv = np.unique(keys, return_inverse=True)
        summed = np.bincount(inv, weights=inten)
        mz_w = np.bincount(inv, weights=mz * inten) / np.where(summed > 0, summed, 1)
        mz_w = np.where(summed > 0, mz_w, uk * bw)
        return mz_w, summed / idx.size

    def subtract(self, spec, bkg):
        """Background-subtract spectrum `bkg` from `spec` (both (mz, I)), clip at zero."""
        bw = self.metadata.get("mz_grid", 0.01)
        smz, si = spec
        bmz, bi = bkg
        sk = np.round(smz / bw).astype(np.int64)
        bk = dict(zip(np.round(bmz / bw).astype(np.int64), bi))
        out = si - np.array([bk.get(k, 0.0) for k in sk])
        keep = out > 0
        return smz[keep], out[keep]

    def eic(self, target_mz: float, tol: float = 0.5) -> np.ndarray:
        """Extracted ion chromatogram: summed abundance within target ± tol."""
        m = np.abs(self.mz - target_mz) <= tol
        return np.bincount(self._scan_index[m], weights=self.intensity[m],
                           minlength=self.n_scans)


# ---------------------------------------------------------------------------
# ChemStation DATA.MS
# ---------------------------------------------------------------------------
def _read_cstr(f, offset):
    f.seek(offset)
    n = f.read(1)[0]
    return f.read(n).decode("latin-1", "replace").strip()


def read_chemstation_ms(path: str) -> GCMSRun:
    with open(path, "rb") as f:
        raw = f.read()
    if struct.unpack(">I", raw[:4])[0] != 0x01320000:
        raise ValueError(f"{path} is not a ChemStation MS file (bad header)")
    with open(path, "rb") as f:
        ftype = _read_cstr(f, 0x4)
        meta = {
            "file_type": ftype,
            "sample": _read_cstr(f, 0x18),
            "operator": _read_cstr(f, 0x94),
            "acquired": _read_cstr(f, 0xB2),
            "instrument": _read_cstr(f, 0xC6),
            "method": _read_cstr(f, 0xE4),
        }
    if ftype == "MSD Spectral File":
        n_scans = struct.unpack(">I", raw[0x116:0x11A])[0]
    else:
        n_scans = struct.unpack("<H", raw[0x142:0x144])[0]
    pos = struct.unpack(">H", raw[0x10A:0x10C])[0] * 2 - 2

    times = np.empty(n_scans)
    counts = np.empty(n_scans, dtype=np.int64)
    tic = np.empty(n_scans)
    chunks = []
    for i in range(n_scans):
        # scan record: [len u16][time u32 ms][6 bytes][npeaks u16][4 bytes][npeaks*(mz u16, ab u16)][10 bytes]
        times[i] = struct.unpack_from(">I", raw, pos + 2)[0] / 60000.0
        npk = struct.unpack_from(">H", raw, pos + 12)[0]
        start = pos + 18
        chunks.append(raw[start:start + 4 * npk])
        counts[i] = npk
        tic[i] = struct.unpack_from(">I", raw, start + 4 * npk + 6)[0]
        pos = start + 4 * npk + 10
    buf = b"".join(chunks)
    total = int(counts.sum())
    mz = np.ndarray(total, ">u2", buf, 0, 4).astype(np.float64) / 20.0
    enc = np.ndarray(total, ">u2", buf, 2, 4).astype(np.uint64)
    inten = (_POW8[enc >> 14] * (enc & 0x3FFF)).astype(np.float64)

    # ChemStation writes each scan high m/z -> low; store ascending
    offsets = np.concatenate([[0], np.cumsum(counts)])
    for i in range(n_scans):
        a, b = offsets[i], offsets[i + 1]
        if b - a > 1 and mz[a] > mz[b - 1]:
            mz[a:b] = mz[a:b][::-1]
            inten[a:b] = inten[a:b][::-1]

    meta["mz_grid"] = 0.05
    meta["mz_decimals"] = 2          # precision written to CSV
    meta["mz_label_decimals"] = 1    # precision shown on plots
    d = os.path.dirname(os.path.abspath(path))
    return GCMSRun(path=d, name=_run_name(d), source="ChemStation DATA.MS",
                   times=times, mz=mz, intensity=inten, offsets=offsets, metadata=meta,
                   stored_tic=tic)


# ---------------------------------------------------------------------------
# MassHunter AcqData
# ---------------------------------------------------------------------------
def read_masshunter(acq: str) -> GCMSRun:
    try:
        from rainbow.agilent import masshunter as mh
    except ImportError as e:  # pragma: no cover
        raise ImportError("Reading MassHunter files needs:  pip install rainbow-api python-lzf") from e

    d = os.path.dirname(os.path.abspath(acq))
    meta = _masshunter_meta(acq)
    if os.path.exists(os.path.join(acq, "MSPeak.bin")):
        c = mh.parse_mspeakdata(acq)
        times = np.asarray(c.xlabels, dtype=float)
        mzs, ints = c._mz, c._intensities
        source = "MassHunter MSPeak.bin (centroid)"
        stored_tic = _masshunter_tic(acq, mh, len(times))
        if getattr(c, "_mz_is_quantized", True):
            meta["mz_grid"], meta["mz_decimals"], meta["mz_label_decimals"] = 0.01, 2, 1
        else:
            meta["mz_grid"], meta["mz_decimals"], meta["mz_label_decimals"] = 0.0001, 4, 4
    elif os.path.exists(os.path.join(acq, "MSProfile.bin")):
        res = mh.parse_msdata(acq)
        res = res[0] if isinstance(res, list) else res
        times = np.asarray(res.xlabels, dtype=float)
        mzs, ints = [], []
        for i in range(len(times)):
            m, a = res.mass_labels(i), res.intensities(i) if hasattr(res, "intensities") else res.data[i]
            mzs.append(np.asarray(m, float)); ints.append(np.asarray(a, float))
        source = "MassHunter MSProfile.bin (profile)"
        stored_tic = _masshunter_tic(acq, mh, len(times))
        meta["mz_grid"], meta["mz_decimals"], meta["mz_label_decimals"] = 0.0001, 4, 4
    else:
        raise FileNotFoundError(f"No MSPeak.bin or MSProfile.bin in {acq}")

    counts = np.array([len(m) for m in mzs], dtype=np.int64)
    offsets = np.concatenate([[0], np.cumsum(counts)])
    mz = np.concatenate([np.asarray(m, float) for m in mzs]) if counts.sum() else np.zeros(0)
    inten = np.concatenate([np.asarray(a, float) for a in ints]) if counts.sum() else np.zeros(0)
    # make sure every scan is sorted by m/z
    for i in range(len(counts)):
        a, b = offsets[i], offsets[i + 1]
        if b - a > 1:
            o = np.argsort(mz[a:b], kind="stable")
            mz[a:b], inten[a:b] = mz[a:b][o], inten[a:b][o]
    return GCMSRun(path=d, name=_run_name(d), source=source, times=times,
                   mz=mz, intensity=inten, offsets=offsets, metadata=meta,
                   stored_tic=stored_tic)


def _masshunter_tic(acq, mh, n):
    try:
        recs = mh.read_scan_records(os.path.join(acq, "MSScan.bin"),
                                    mh.parse_scan_xsd(os.path.join(acq, "MSScan.xsd")),
                                    mh.count_scans(acq))
        t = np.array([r.get("TIC", np.nan) for r in recs], dtype=float)
        return t if len(t) == n and np.all(np.isfinite(t)) else None
    except Exception:
        return None


def _masshunter_meta(acq):
    meta = {}
    try:
        import xml.etree.ElementTree as ET
        root = ET.parse(os.path.join(acq, "Contents.xml")).getroot()
        for tag in ("AcquiredTime", "InstrumentName", "AcqSoftwareVersion"):
            el = root.find(tag)
            if el is not None and el.text:
                meta[tag] = el.text.strip()
        si = os.path.join(acq, "sample_info.xml")
        if os.path.exists(si):
            for f in ET.parse(si).getroot().iter("Field"):
                n, v = f.findtext("Name"), f.findtext("Value")
                if n and v and n in ("Sample Name", "Method", "Operator", "Sample Position", "Comment"):
                    meta[n] = v.strip()
    except Exception:
        pass
    return meta


# ---------------------------------------------------------------------------
# ANDI-MS netCDF
# ---------------------------------------------------------------------------
def _nc_text(v):
    if isinstance(v, bytes):
        return v.decode("latin-1", "replace").strip("\x00 ").strip()
    try:
        return b"".join(np.asarray(v).ravel().tolist()).decode("latin-1", "replace").strip("\x00 ").strip()
    except Exception:
        return str(v)


def read_netcdf(path: str) -> GCMSRun:
    """Read an ANDI-MS (AIA) netCDF file - the standard export of nearly all GC-MS software."""
    from scipy.io import netcdf_file
    try:
        f = netcdf_file(path, "r", mmap=False)
        get = lambda k: f.variables[k][:].copy() if k in f.variables else None  # noqa: E731
        attrs = {k: _nc_text(v) if isinstance(v, bytes) else v for k, v in f._attributes.items()}
        inst = {k: _nc_text(f.variables[k][:]) for k in ("instrument_mfr", "instrument_model", "instrument_name")
                if k in f.variables}
    except TypeError:
        # netCDF-4 / HDF5 flavour: needs the netCDF4 package
        import netCDF4  # noqa: F401
        f = netCDF4.Dataset(path)
        f.set_auto_mask(False)
        get = lambda k: np.asarray(f.variables[k][:]) if k in f.variables else None  # noqa: E731
        attrs = {k: f.getncattr(k) for k in f.ncattrs()}
        inst = {}
    times = get("scan_acquisition_time").astype(float)
    counts = get("point_count").astype(np.int64)
    mz = get("mass_values")
    inten = get("intensity_values")
    tic = get("total_intensity")
    f.close()
    big = mz.size > 20_000_000          # halve memory for very large exports
    ftype = np.float32 if big else np.float64
    mz = mz.astype(ftype) * float(attrs.get("raw_data_mass_factor", 1.0) or 1.0)
    inten = inten.astype(ftype) * float(attrs.get("raw_data_intensity_factor", 1.0) or 1.0)
    tunits = str(attrs.get("raw_data_time_units", "Seconds")).lower()
    times = times / 60.0 if "min" not in tunits else times
    offsets = np.concatenate([[0], np.cumsum(counts)])
    # make sure each scan is sorted by m/z
    if counts.size and np.any(np.diff(mz) < 0):
        sidx = np.repeat(np.arange(counts.size), counts)
        o = np.lexsort((mz, sidx))
        mz, inten = mz[o], inten[o]
    meta = {
        "source_file": str(attrs.get("source_file_reference", "")),
        "experiment_date": str(attrs.get("experiment_date_time_stamp", "")),
        "experiment_type": str(attrs.get("experiment_type", "")),
        "ionisation": str(attrs.get("test_ionization_mode", "")),
        "instrument": " ".join(v for v in inst.values() if v),
        "source_format": str(attrs.get("source_file_format", "")),
    }
    method = str(attrs.get("measurement_data", ""))
    m = re.search(r"^Name=(.+)$", method, re.M)
    if m:
        meta["method"] = m.group(1).strip()
    # TOF exports carry fractional m/z that jitter from scan to scan; averaging on a 1 Da grid
    # (intensity-weighted mean m/z kept) gives clean spectra and matches nominal-mass libraries.
    frac = np.abs(mz[:5000] - np.round(mz[:5000])) if mz.size else np.zeros(1)
    hi_res = mz.size > 0 and np.median(frac) > 0.002
    meta["mz_grid"] = 1.0 if hi_res else 0.05
    meta["mz_decimals"] = 4 if hi_res else 2
    meta["mz_label_decimals"] = 0 if hi_res else 1
    stem = os.path.basename(path)
    for ext in (".cdf", ".nc", ".CDF", ".NC"):
        if stem.endswith(ext):
            stem = stem[: -len(ext)]
    return GCMSRun(path=os.path.abspath(path), name=stem, source="ANDI netCDF" +
                   (f" ({meta['source_format']})" if meta["source_format"] else ""),
                   times=times, mz=mz, intensity=inten, offsets=offsets, metadata=meta,
                   stored_tic=tic)


def guess_modulation_period(run: GCMSRun, name_hint: str = "", signal_only_ok: bool = True):
    """
    Returns (period_s or None, reason).  A modulated (GCxGC) signal repeats every period, so its
    autocorrelation has a genuine peak at the period that stands clear of the dip at half the
    period.  A '4s' / '6 sec' token in the file or method name is accepted when the signal agrees;
    otherwise the strongest such peak in 0.8-15 s is used (only if signal_only_ok).
    """
    dt = float(np.median(np.diff(run.times * 60))) if run.n_scans > 2 else 0
    if dt <= 0 or run.n_scans < 2000:
        return None, ""
    x = run.tic - run.tic.mean()
    n = min(len(x), 400000)
    x = x[:n]
    F = np.fft.rfft(x, n=2 * n)
    ac = np.fft.irfft(F * np.conj(F))[:n]
    if not ac[0]:
        return None, ""
    ac = ac / ac[0]

    def prominence(k):
        if k < 8 or 2 * k >= len(ac):
            return 0.0
        w = max(2, k // 10)
        if ac[k] < ac[k - w:k + w + 1].max() - 1e-12:        # must be a local maximum
            return 0.0
        trough = ac[k // 2 - w:k // 2 + w + 1].min()           # dip half-way between repeats
        second = ac[2 * k - w:2 * k + w + 1].max() - ac[(3 * k) // 2 - w:(3 * k) // 2 + w + 1].min()
        return float(ac[k] - trough) if second > 0.002 else 0.0

    def best_near(p):
        k0 = int(round(p / dt))
        w = max(2, int(0.02 * k0))
        ks = range(max(8, k0 - w), k0 + w + 1)
        return max(ks, key=prominence) if ks else k0

    text = " ".join([name_hint, run.name, str(run.metadata.get("method", ""))])
    for tok in re.findall(r"(?:^|[_\-\s])(\d+(?:[.p]\d+)?)\s*(?:s|sec|secs|s_PM|PM)(?=[_\-\s.]|$)", text, re.I):
        p = float(tok.replace("p", "."))
        if 0.5 <= p <= 20 and prominence(best_near(p)) > 0.02:
            return p, f"{p:g} s (from the file name, confirmed by the signal)"
    if not signal_only_ok:
        return None, ""
    lo, hi = int(0.8 / dt), min(int(15 / dt), len(ac) // 2 - 1)
    if hi <= lo:
        return None, ""
    cand = [k for k in range(lo + 1, hi) if ac[k] >= ac[k - 1] and ac[k] >= ac[k + 1]]
    if not cand:
        return None, ""
    k = max(cand, key=prominence)
    if prominence(k) > 0.03:
        p = round(k * dt, 2)
        return p, f"{p:g} s (detected from the signal - please check)"
    return None, ""


# ---------------------------------------------------------------------------
def _run_name(d):
    b = os.path.basename(os.path.normpath(d))
    return b[:-2] if b.lower().endswith(".d") else b


def _find_ci(folder, name):
    """Case-insensitive file lookup inside a folder."""
    try:
        for f in os.listdir(folder):
            if f.lower() == name.lower():
                return os.path.join(folder, f)
    except FileNotFoundError:
        pass
    return None


def read_signal(path: str) -> GCMSRun:
    """Single-channel Agilent detector file (.ch): FID, TCD, ECD, NPD..."""
    try:
        from rainbow.agilent import chemstation as cs
    except ImportError as e:  # pragma: no cover
        raise ImportError("Reading detector (.ch) files needs:  pip install rainbow-api") from e
    f = cs.parse_ch(path)
    if f is None:
        raise ValueError(f"{os.path.basename(path)} could not be decoded")
    y = np.asarray(f.data, float).reshape(len(f.xlabels), -1)[:, 0]
    times = np.asarray(f.xlabels, float)
    md = dict(getattr(f, "metadata", {}) or {})
    sig = md.get("signal") or os.path.splitext(os.path.basename(path))[0]
    det = re.sub(r"\d.*$", "", sig).upper() or "Signal"        # "FID1A" -> "FID"
    meta = {"detector": det, "signal": sig, "signal_unit": md.get("unit", ""),
            "sample": md.get("notebook", ""), "acquired": md.get("date", ""), "method": md.get("method", ""),
            "mz_grid": 1.0, "mz_decimals": 2, "mz_label_decimals": 0}
    d = os.path.dirname(os.path.abspath(path))
    z = np.zeros(0)
    return GCMSRun(path=d, name=_run_name(d), source=f"Agilent {sig}.ch ({det})", times=times,
                   mz=z, intensity=z.copy(), offsets=np.zeros(len(times) + 1, dtype=np.int64),
                   metadata=meta, stored_tic=y)


def signal_files(d_path: str) -> list[str]:
    """Detector signal files (.ch) in a .D folder, e.g. ['FID1A.ch', 'FID2B.ch']."""
    try:
        return sorted(f for f in os.listdir(d_path) if f.lower().endswith(".ch"))
    except OSError:
        return []


def available_sources(d_path: str) -> list[str]:
    out = []
    acq = _find_ci(d_path, "AcqData")
    if acq and (_find_ci(acq, "MSPeak.bin") or _find_ci(acq, "MSProfile.bin")):
        out.append("masshunter")
    if _find_ci(d_path, "data.ms"):
        out.append("chemstation")
    out += ["ch:" + f for f in signal_files(d_path)]
    return out


def read_d(d_path: str, prefer: str = "masshunter") -> GCMSRun:
    """
    Open an Agilent .D folder (or a DATA.MS file directly).
    prefer: "masshunter" or "chemstation" - which file to use when both are present.
    """
    d_path = os.path.abspath(d_path)
    if os.path.isfile(d_path):
        if d_path.lower().endswith((".cdf", ".nc")):
            return read_netcdf(d_path)
        return read_chemstation_ms(d_path)
    srcs = available_sources(d_path)
    if not srcs:
        raise FileNotFoundError(
            f"No GC-MS data found in {d_path}\n(looked for DATA.MS and AcqData/MSPeak.bin / MSProfile.bin)")
    choice = prefer if prefer in srcs else srcs[0]
    if choice.startswith("ch:"):
        return read_signal(os.path.join(d_path, choice[3:]))
    if choice == "masshunter":
        return read_masshunter(_find_ci(d_path, "AcqData"))
    return read_chemstation_ms(_find_ci(d_path, "data.ms"))
