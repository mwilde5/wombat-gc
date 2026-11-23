"""
ms_library.py - offline mass-spectral library search against MSP files.

Works with any NIST-format .msp library, e.g.
  * MassBank   (MassBank_NIST.msp, from the MassBank-data GitHub releases)
  * MoNA       (GC-MS export from https://mona.fiehnlab.ucdavis.edu/downloads)
  * your own in-house libraries exported as MSP

Spectra are reduced to nominal mass and compared with the Stein & Scott (1994)
weighted dot product:
    weight = (m/z)^3 * intensity^0.6,   Match = 999 * cos^2
"Reverse match" ignores query peaks that are absent from the library spectrum
(useful for spectra with co-eluting interferences).

Each library is parsed once and cached as a compact .npz index, so reloading is fast.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass

import numpy as np
from scipy import sparse

MAX_MZ = 2000
MZ_POW = 3.0
INT_POW = 0.6
CACHE_VERSION = 4

_num = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")
_quoted = re.compile(r'"[^"]*"')


# ---------------------------------------------------------------------------
# MSP parsing
# ---------------------------------------------------------------------------
def _norm_key(k: str) -> str:
    return re.sub(r"[^a-z0-9#]", "", k.lower())


def iter_msp(path, progress=None):
    """
    Yield (meta: dict, mz: ndarray, intensity: ndarray) for every record in an MSP file.
    Keys in meta are normalised to lower case without spaces/underscores ("precursormz", "cas#", ...).
    progress(bytes_read, total_bytes) is called now and then.
    """
    total = os.path.getsize(path)
    meta, peaks, n_expected, in_peaks = {}, [], 0, False
    read = 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f):
            read += len(line)
            if progress and lineno % 200000 == 0:
                progress(read, total)
            s = line.strip()
            if not s:
                if meta or peaks:
                    if peaks:
                        yield meta, *_peaks_to_arrays(peaks)
                    meta, peaks, in_peaks = {}, [], False
                continue
            if in_peaks:
                if ":" in s and not s[0].isdigit() and not s.startswith("("):
                    # a new record that wasn't separated by a blank line
                    if s.lower().startswith("name:"):
                        if peaks:
                            yield meta, *_peaks_to_arrays(peaks)
                        meta, peaks, in_peaks = {}, [], False
                    else:
                        continue
                else:
                    nums = _num.findall(_quoted.sub(" ", s))
                    for i in range(0, len(nums) - 1, 2):
                        peaks.append((float(nums[i]), float(nums[i + 1])))
                    continue
            if ":" in s:
                key, _, val = s.partition(":")
                nk = _norm_key(key)
                if nk == "numpeaks":
                    try:
                        n_expected = int(_num.findall(val)[0])
                    except (IndexError, ValueError):
                        n_expected = 0
                    in_peaks = n_expected > 0
                    continue
                if nk == "name" and (meta or peaks):
                    if peaks:
                        yield meta, *_peaks_to_arrays(peaks)
                    meta, peaks = {}, []
                # NIST style:  "CAS#: 64-17-5;  NIST#: 1234"
                if ";" in val and re.search(r";\s*[A-Za-z#]+\s*:", val):
                    first, *rest = val.split(";")
                    meta[nk] = first.strip()
                    for seg in rest:
                        if ":" in seg:
                            k2, _, v2 = seg.partition(":")
                            meta[_norm_key(k2)] = v2.strip()
                else:
                    val = val.strip()
                    if nk == "comments" or nk == "comment":
                        meta["comments"] = meta.get("comments", "") + " " + val
                        # MoNA packs metadata into quoted "key=value" pairs in Comments
                        for kv in re.findall(r'"([^"=]+)=([^"]*)"', val):
                            meta.setdefault(_norm_key(kv[0]), kv[1].strip())
                    else:
                        meta[nk] = val
        if peaks:
            yield meta, *_peaks_to_arrays(peaks)
    if progress:
        progress(total, total)


def _peaks_to_arrays(peaks):
    a = np.asarray(peaks, dtype=float)
    return a[:, 0], a[:, 1]


def is_ei_spectrum(meta: dict) -> bool:
    """Best guess whether a library record is an EI (GC-MS) spectrum rather than LC-MS/MS."""
    for k in ("precursormz", "precursortype"):
        v = meta.get(k, "").strip()
        if v and v.lower() not in ("0", "na", "n/a", "none"):
            return False
    st = (meta.get("spectrumtype", "") + " " + meta.get("mstype", "")).upper()
    if re.search(r"MS[2-9]", st):
        return False
    probe = " ".join(meta.get(k, "") for k in
                     ("instrumenttype", "ionization", "ionizationtype", "instrument")).upper()
    tokens = set(re.split(r"[^A-Z0-9]+", probe))
    if tokens & {"ESI", "APCI", "APPI", "MALDI", "CI", "PCI", "NCI", "FAB", "FD", "FI", "LC", "DART"}:
        return False
    if tokens & {"EI", "GC"} or "ELECTRON IMPACT" in probe or "ELECTRON IONIZATION" in probe:
        return True
    return True    # no precursor and no LC hints: typical EI library entries look like this


# ---------------------------------------------------------------------------
# Library index
# ---------------------------------------------------------------------------
META_FIELDS = ("name", "formula", "mw", "cas", "inchikey", "inchi", "smiles", "accession", "ri")


def _pick(meta, *keys):
    for k in keys:
        v = meta.get(k)
        if v:
            return v.strip()
    return ""


@dataclass
class Hit:
    library: str
    index: int
    name: str
    match: int
    rmatch: int
    formula: str = ""
    mw: str = ""
    cas: str = ""
    inchikey: str = ""
    inchi: str = ""
    accession: str = ""
    prob: str = ""
    source: str = "open"      # "open" or "nist"


class SpectralLibrary:
    def __init__(self, path, name, W, P, meta, ei, raw):
        self.path, self.name = path, name
        self.W = W            # csr: rows = spectra, unit-norm weighted vectors
        self.P = P            # csr: same pattern, squared weights (for range-restricted norms)
        self.meta = meta      # dict of field -> list[str]
        self.ei = ei          # bool array
        self.raw = raw        # csr: nominal-mass intensities scaled to 999 (for plotting)

    def __len__(self):
        return self.W.shape[0]

    @property
    def n_ei(self):
        return int(self.ei.sum())

    # ---- building / caching ------------------------------------------------
    @classmethod
    def load(cls, path, cache_dir, progress=None):
        path = os.path.abspath(path)
        st = os.stat(path)
        key = hashlib.sha1(f"{path}|{st.st_size}|{int(st.st_mtime)}|{CACHE_VERSION}".encode()).hexdigest()[:16]
        os.makedirs(cache_dir, exist_ok=True)
        cache = os.path.join(cache_dir, f"{key}.npz")
        name = os.path.splitext(os.path.basename(path))[0]
        if os.path.exists(cache):
            try:
                return cls._from_cache(path, name, cache)
            except Exception:
                pass
        lib = cls._build(path, name, progress)
        lib._save(cache)
        return lib

    @classmethod
    def _build(cls, path, name, progress=None):
        rows, cols, vals = [], [], []
        meta = {f: [] for f in META_FIELDS}
        ei = []
        n = 0
        for m, mz, inten in iter_msp(path, progress):
            nom = np.rint(mz).astype(np.int64)
            ok = (nom >= 1) & (nom < MAX_MZ) & (inten > 0)
            if not ok.any():
                continue
            nom, inten = nom[ok], inten[ok]
            u, inv = np.unique(nom, return_inverse=True)
            s = np.bincount(inv, weights=inten)
            s = s / s.max() * 999.0
            rows.append(np.full(u.size, n, dtype=np.int32))
            cols.append(u.astype(np.int32))
            vals.append(s.astype(np.float32))
            meta["name"].append(_pick(m, "name") or f"record {n + 1}")
            meta["formula"].append(_pick(m, "formula"))
            meta["mw"].append(_pick(m, "mw", "exactmass", "molecularweight", "nominalmass"))
            meta["cas"].append(_pick(m, "cas#", "cas", "casno", "casnumber", "casrn"))
            meta["inchikey"].append(_pick(m, "inchikey"))
            meta["inchi"].append(_pick(m, "inchi"))
            meta["smiles"].append(_pick(m, "smiles"))
            meta["accession"].append(_pick(m, "db#", "accession", "id", "nist#", "spectrumid"))
            meta["ri"].append(_pick(m, "retentionindex", "ri", "kovatsri"))
            ei.append(is_ei_spectrum(m))
            n += 1
        if n == 0:
            raise ValueError(f"No spectra found in {path} - is it an MSP file?")
        r, c, v = np.concatenate(rows), np.concatenate(cols), np.concatenate(vals)
        raw = sparse.csr_matrix((v, (r, c)), shape=(n, MAX_MZ), dtype=np.float32)
        return cls._finish(path, name, raw, meta, np.array(ei, bool))

    @classmethod
    def _finish(cls, path, name, raw, meta, ei):
        mzw = np.arange(MAX_MZ, dtype=np.float64) ** MZ_POW
        W = raw.astype(np.float64)
        W.data = np.power(W.data, INT_POW) * mzw[W.indices]
        P = W.copy()
        P.data = P.data ** 2
        norms = np.sqrt(np.asarray(P.sum(axis=1)).ravel())
        norms[norms == 0] = 1
        D = sparse.diags(1 / norms)
        W = (D @ W).tocsr()
        P = (D @ D @ P).tocsr()
        return cls(path, name, W, P, meta, ei, raw)

    def _save(self, cache):
        np.savez_compressed(
            cache, data=self.raw.data, indices=self.raw.indices, indptr=self.raw.indptr,
            shape=np.array(self.raw.shape), ei=self.ei,
            meta=np.frombuffer(json.dumps(self.meta).encode("utf-8"), dtype=np.uint8))

    @classmethod
    def _from_cache(cls, path, name, cache):
        z = np.load(cache)
        raw = sparse.csr_matrix((z["data"], z["indices"], z["indptr"]), shape=tuple(z["shape"]))
        meta = json.loads(bytes(z["meta"]).decode("utf-8"))
        return cls._finish(path, name, raw, meta, z["ei"])

    # ---- searching -----------------------------------------------------------
    def spectrum(self, i):
        row = self.raw.getrow(i)
        return row.indices.astype(float), row.data.astype(float)

    def search(self, mz, inten, *, top=30, ei_only=True, low_mz_cut=None, min_match=0):
        q = query_vector(mz, inten)
        if q is None:
            return []
        colmask = np.ones(MAX_MZ)
        if low_mz_cut:
            colmask[:int(np.floor(low_mz_cut))] = 0
        qm = q * colmask
        qn = np.sqrt((qm ** 2).sum())
        if qn == 0:
            return []
        num = self.W @ qm                                   # library rows are unit-norm over all m/z
        lib_norm = np.sqrt(np.maximum(self.P @ colmask, 1e-300))   # re-normalise over the query's m/z range
        fwd = (num / (lib_norm * qn)) ** 2
        # reverse: only query peaks at m/z present in the library spectrum
        pat = self.P.copy()
        pat.data[:] = 1
        q_in_lib = np.sqrt(np.maximum(pat @ (qm ** 2), 1e-300))
        rev = (num / (lib_norm * q_in_lib)) ** 2
        fwd[~np.isfinite(fwd)] = 0
        rev[~np.isfinite(rev)] = 0
        if ei_only:
            fwd = np.where(self.ei, fwd, 0)
        k = min(top, len(fwd))
        idx = np.argpartition(-fwd, k - 1)[:k] if k < len(fwd) else np.arange(len(fwd))
        idx = idx[np.argsort(-fwd[idx])]
        out = []
        for i in idx:
            mf = int(round(999 * fwd[i]))
            if mf <= min_match:
                continue
            g = lambda f: self.meta[f][i] if f in self.meta else ""  # noqa: E731
            out.append(Hit(library=self.name, index=int(i), name=g("name"), match=mf,
                           rmatch=int(round(999 * min(rev[i], 1.0))), formula=g("formula"), mw=g("mw"),
                           cas=g("cas"), inchikey=g("inchikey"), inchi=g("inchi"),
                           accession=g("accession")))
        return out


def query_vector(mz, inten):
    mz = np.asarray(mz, float)
    inten = np.asarray(inten, float)
    nom = np.rint(mz).astype(int)
    ok = (nom >= 1) & (nom < MAX_MZ) & (inten > 0)
    if not ok.any():
        return None
    v = np.bincount(nom[ok], weights=inten[ok], minlength=MAX_MZ)[:MAX_MZ]
    v = v / v.max() * 999.0
    return np.power(v, INT_POW) * np.arange(MAX_MZ, dtype=float) ** MZ_POW


def search_all(libraries, mz, inten, top=30, **kw):
    hits = []
    for lib in libraries:
        hits += lib.search(mz, inten, top=top, **kw)
    hits.sort(key=lambda h: -h.match)
    return hits[:top]


# ---------------------------------------------------------------------------
# Web links
# ---------------------------------------------------------------------------
def _quote(s):
    from urllib.parse import quote
    return quote(s, safe="")


def webbook_url(hit: Hit) -> str:
    cas = re.sub(r"[^0-9]", "", hit.cas or "")
    if cas and cas != "0":
        return f"https://webbook.nist.gov/cgi/cbook.cgi?ID=C{cas}&Units=SI&Mask=200#Mass-Spec"
    if hit.inchi and hit.inchi.startswith("InChI="):
        from urllib.parse import quote
        return "https://webbook.nist.gov/cgi/inchi/" + quote(hit.inchi, safe="/()")
    return f"https://webbook.nist.gov/cgi/cbook.cgi?Name={_quote(hit.name)}&Units=SI"


def pubchem_url(hit: Hit) -> str:
    term = hit.inchikey or (hit.cas if hit.cas and hit.cas != "0" else "") or hit.name
    return f"https://pubchem.ncbi.nlm.nih.gov/#query={_quote(term)}"


def record_url(hit: Hit) -> str | None:
    acc = (hit.accession or "").strip()
    if not acc or hit.source == "nist":
        return None
    if acc.upper().startswith("MSBNK-") or re.match(r"^[A-Z]{2,3}\d{6}$", acc) and "massbank" in hit.library.lower():
        return f"https://massbank.eu/MassBank/RecordDisplay?id={_quote(acc)}"
    if "mona" in hit.library.lower():
        return f"https://mona.fiehnlab.ucdavis.edu/spectra/display/{_quote(acc)}"
    return None
