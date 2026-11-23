"""
nist_search.py - hand spectra to a locally installed NIST MS Search program (Windows).

Uses the documented NIST MS Search automation interface:
  1. MSSEARCH\\AUTOIMP.MSD  contains the path of a "locator" file
  2. the locator file       contains the path of an MSP spectrum file + " OVERWRITE"
  3. run  nistms$.exe /INSTRUMENT /PAR=2
  4. NIST searches, shows the hits in its own window, writes MSSEARCH\\SRCRESLT.TXT
     and then MSSEARCH\\SRCREADY.TXT (the number of spectra searched)

For step 4 NIST must have automatic searching switched on:
  NIST MS Search > Options > Library Search Options > Automation tab >
  tick "Automatic Search On" (and set "Number of hits to print", e.g. 20).

Any existing AUTOIMP.MSD (ChemStation/MassHunter use the same mechanism) is backed up
and restored once the search has finished.
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
import sys
import tempfile
import time

EXE = "nistms$.exe"


def candidate_dirs():
    pats = []
    for drive in ("C:", "D:", "E:"):
        pats += [rf"{drive}\NIST*\MSSEARCH", rf"{drive}\*\NIST*\MSSEARCH", rf"{drive}\*\*\NIST*\MSSEARCH"]
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        base = os.environ.get(env)
        if base:
            pats += [os.path.join(base, "NIST*", "MSSEARCH"), os.path.join(base, "*", "NIST*", "MSSEARCH")]
    found = []
    for p in pats:
        for d in glob.glob(p):
            if os.path.isfile(os.path.join(d, EXE)) and d not in found:
                found.append(d)
    # newest library first (NIST23 > NIST20 > NIST17 ...)
    found.sort(key=lambda d: [int(x) for x in re.findall(r"\d+", d)] or [0], reverse=True)
    return found


def _short_path(path):
    """Windows 8.3 short form of a path, so a path with spaces survives NIST's 'path OVERWRITE' parsing."""
    if " " not in path or not sys.platform.startswith("win"):
        return path
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.windll.kernel32.GetShortPathNameW(path, buf, 1024)
        return buf.value if 0 < n < 1024 else path
    except Exception:
        return path


def find_mssearch():
    c = candidate_dirs()
    return c[0] if c else None


def write_msp(path, spectra):
    """spectra: list of (name, mz array, abundance array)."""
    with open(path, "w", encoding="ascii", errors="replace", newline="\r\n") as f:
        for name, mz, ab in spectra:
            # NIST EI libraries are nominal mass: sum peaks onto integer m/z
            acc = {}
            for m, a in zip(mz, ab):
                if a > 0:
                    acc[int(round(m))] = acc.get(int(round(m)), 0.0) + float(a)
            pairs = sorted(acc.items())
            mx = max((a for _, a in pairs), default=1)
            f.write(f"Name: {name[:200].replace(':', ' ')}\n")
            f.write(f"Num Peaks: {len(pairs)}\n")
            line = []
            for k, (m, a) in enumerate(pairs, 1):
                line.append(f"{m} {max(1, round(999 * a / mx))};")
                if k % 5 == 0:
                    f.write(" ".join(line) + "\n")
                    line = []
            if line:
                f.write(" ".join(line) + "\n")
            f.write("\n")


class NistSession:
    """One search hand-off. Call start(), then poll() until it returns True (or times out)."""

    def __init__(self, mssearch_dir, spectra, work_dir=None):
        self.dir = mssearch_dir
        self.spectra = spectra
        # keep the hand-off files beside NIST (its own folder must be writable anyway)
        self.work = work_dir or os.path.join(mssearch_dir, "gcms_viewer")
        self.autoimp = os.path.join(self.dir, "AUTOIMP.MSD")
        self.ready = os.path.join(self.dir, "SRCREADY.TXT")
        self.result = os.path.join(self.dir, "SRCRESLT.TXT")
        self._backup = None
        self._restored = False
        self.t0 = None

    def start(self):
        exe = os.path.join(self.dir, EXE)
        if not os.path.isfile(exe):
            raise FileNotFoundError(f"{EXE} not found in {self.dir}")
        try:
            os.makedirs(self.work, exist_ok=True)
        except OSError:
            self.work = os.path.join(tempfile.gettempdir(), "gcms_viewer_nist")
            os.makedirs(self.work, exist_ok=True)
        msp = os.path.join(self.work, "spectrum.msp")
        locator = os.path.join(self.work, "filespec.fil")
        write_msp(msp, self.spectra)
        with open(locator, "w", newline="\r\n") as f:
            f.write(f"{_short_path(msp)} OVERWRITE\n")
        if os.path.exists(self.autoimp):
            with open(self.autoimp, "r", errors="replace") as f:
                self._backup = f.read()
        try:
            with open(self.autoimp, "w", newline="\r\n") as f:
                f.write(_short_path(locator) + "\n")
        except PermissionError as e:
            raise PermissionError(
                f"Can't write {self.autoimp}.\nAsk IT to give your account write access to the "
                f"{self.dir} folder (NIST itself needs this to save results).") from e
        for fn in (self.ready, self.result):
            try:
                os.remove(fn)
            except FileNotFoundError:
                pass
        subprocess.Popen([exe, "/INSTRUMENT", "/PAR=2"], cwd=self.dir)
        self.t0 = time.time()

    def poll(self) -> bool:
        """True once SRCREADY.TXT has appeared (results are then in SRCRESLT.TXT)."""
        if os.path.exists(self.ready):
            time.sleep(0.2)   # let NIST finish writing
            self.restore()
            return True
        return False

    def elapsed(self):
        return time.time() - self.t0 if self.t0 else 0

    def restore(self):
        if self._restored:
            return
        self._restored = True
        if self._backup is not None:
            try:
                with open(self.autoimp, "w", newline="") as f:
                    f.write(self._backup)
            except OSError:
                pass

    def results(self):
        if not os.path.exists(self.result):
            return []
        with open(self.result, "r", encoding="cp1252", errors="replace") as f:
            return parse_srcreslt(f.read())


_hit_re = re.compile(r"^\s*Hit\s+(\d+)\s*:\s*(.*)$", re.I)


def parse_srcreslt(text):
    """
    Parse NIST SRCRESLT.TXT.  Returns [ {"unknown": str, "hits": [dict, ...]}, ... ]
    Hit lines look like:
      Hit 1  : <<n-Hexane>>; <<C6H14>>; MF: 999; RMF: 999; Prob: 89.48; CAS:110-54-3; Mw: 86; Lib: <<mainlib>>; Id: 27695; RI: 600.
    """
    out = []
    cur = None
    for line in text.splitlines():
        if line.lower().startswith("unknown"):
            name = line.split(":", 1)[1] if ":" in line else line
            name = re.split(r"\s+Compound in Library Factor\s*=", name)[0].strip()
            cur = {"unknown": name, "hits": []}
            out.append(cur)
            continue
        m = _hit_re.match(line)
        if not m or cur is None:
            continue
        body = m.group(2)
        quoted = re.findall(r"<<(.*?)>>", body)
        rest = re.sub(r"<<.*?>>", "", body)
        fields = {}
        for k, v in re.findall(r"([A-Za-z]+)\s*:\s*([^;]*)", rest):
            fields[k.lower()] = v.strip().rstrip(".")
        hit = {
            "rank": int(m.group(1)),
            "name": quoted[0] if quoted else "",
            "formula": quoted[1] if len(quoted) > 1 else "",
            "lib": quoted[2] if len(quoted) > 2 else fields.get("lib", ""),
            "mf": _int(fields.get("mf")), "rmf": _int(fields.get("rmf")),
            "prob": fields.get("prob", ""), "cas": fields.get("cas", ""),
            "mw": fields.get("mw", ""), "id": fields.get("id", ""), "ri": fields.get("ri", ""),
        }
        out[-1]["hits"].append(hit)
    return out


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0
