"""
mesh_export.py - solid 3D models of a GCxGC chromatogram: GLB, binary FBX 7.4 and STL.

The mesh and the GLB / FBX writers are a faithful Python port of the "GC×GC 3D chromatogram
from a single trace" page: a vertex-coloured height-map surface with walls and a flat base,
Y-up, in metres (X = first dimension, -Z = second dimension, Y = intensity).  For the same
input they produce the same files as that page (checked byte-for-byte in testing).

STL has no colours; it is written Z-up in millimetres with a watertight, triangulated base,
ready for slicing and 3D printing.
"""
from __future__ import annotations

import json
import math
import struct

import numpy as np

import colormaps as cmaps


# =============================================================================
# Mesh
# =============================================================================
def pool_rows(Z, max2):
    """Max-pool the 2nd-dimension rows (keeps peak tops), dropping an incomplete tail - as the page does."""
    Z = np.asarray(Z, float)
    pool = max(1, math.ceil(Z.shape[0] / max2))
    if pool == 1:
        return Z
    n = (Z.shape[0] // pool) * pool
    return Z[:n].reshape(-1, pool, Z.shape[1]).max(axis=1)


def build_mesh(Z, width=1.0, depth=0.42, height=0.3, base=0.02, stops=None, base_color="#2e3138",
               max2=200, colour_values=None, watertight_base=False):
    """
    Z: rows = 2nd-dimension points, columns = modulations (already scaled, >= 0).
    colour_values: optional array like Z (0-1) for colouring; default = Z / max, as on the page.
    watertight_base: triangulate the base around its rim (for STL / printing) instead of two triangles.
    Returns dict P (n,3) N (n,3) C (n,3) float32, I (m,) uint32.
    """
    stops = stops or cmaps.PRESETS["Viridis"]
    rows = pool_rows(Z, max2)
    cv = pool_rows(colour_values, max2) if colour_values is not None else None
    ny, nx = rows.shape
    zmax = float(rows.max()) or 1.0
    X = np.arange(nx) / (nx - 1) * width
    Zc = -np.arange(ny, dtype=float) / (ny - 1) * depth          # first row is -0.0, as in the page
    H = base + rows / zmax * height                        # (ny, nx)

    # top surface
    xs = np.tile(X, ny)
    zs = np.repeat(Zc, nx)
    P = [np.stack([xs, H.ravel(), zs], axis=1)]
    ip, im = np.minimum(nx - 1, np.arange(nx) + 1), np.maximum(0, np.arange(nx) - 1)
    jp, jm = np.minimum(ny - 1, np.arange(ny) + 1), np.maximum(0, np.arange(ny) - 1)
    dx = (H[:, ip] - H[:, im]) / (X[ip] - X[im])[None, :]
    dz = (H[jp, :] - H[jm, :]) / (Zc[jp] - Zc[jm])[:, None]
    n = np.stack([-dx, np.ones_like(dx), -dz], axis=-1).reshape(-1, 3)
    n = n / np.sqrt((n ** 2).sum(axis=1))[:, None]
    N = [n]
    vals = (cv if cv is not None else rows / zmax).ravel()
    C = [cmaps.colours_at(stops, vals)]
    jj, ii = np.meshgrid(np.arange(ny - 1), np.arange(nx - 1), indexing="ij")
    a = (jj * nx + ii).ravel()
    tri = np.stack([a, a + 1, a + nx + 1, a, a + nx + 1, a + nx], axis=1).reshape(-1)
    Ilist = [tri]

    # walls and base: un-indexed triangles with flat normals, in the page's order
    extra_p, extra_n, extra_i = [], [], []
    base_col = cmaps.hex_rgb(base_color)
    count = [nx * ny]

    def add_tri(vs, out):
        u = np.subtract(vs[1], vs[0])
        v = np.subtract(vs[2], vs[0])
        nn = np.cross(u, v)
        if np.dot(nn, out) < 0:
            vs = [vs[0], vs[2], vs[1]]
        for p in vs:
            extra_i.append(count[0])
            count[0] += 1
            extra_p.append(p)
            extra_n.append(out)

    def wall(pts, out):
        for k in range(len(pts) - 1):
            t0, t1 = pts[k], pts[k + 1]
            b0, b1 = [t0[0], 0.0, t0[2]], [t1[0], 0.0, t1[2]]
            add_tri([b0, b1, t1], out)
            add_tri([b0, t1, t0], out)

    front = [[X[i], H[0, i], 0.0] for i in range(nx)]
    back = [[X[i], H[ny - 1, i], -depth] for i in range(nx)]
    left = [[0.0, H[j, 0], Zc[j]] for j in range(ny)]
    right = [[width, H[j, nx - 1], Zc[j]] for j in range(ny)]
    wall(front, [0, 0, 1])
    wall(back, [0, 0, -1])
    wall(left, [-1, 0, 0])
    wall(right, [1, 0, 0])
    down = [0, -1, 0]
    if not watertight_base:
        c00, c10, c11, c01 = [0.0, 0.0, 0.0], [width, 0.0, 0.0], [width, 0.0, -depth], [0.0, 0.0, -depth]
        add_tri([c00, c01, c11], down)
        add_tri([c00, c11, c10], down)
    else:
        # fan from the centre to every rim segment, so wall and base edges match exactly
        rim = ([[X[i], 0.0, 0.0] for i in range(nx)] +
               [[width, 0.0, Zc[j]] for j in range(1, ny)] +
               [[X[i], 0.0, -depth] for i in range(nx - 2, -1, -1)] +
               [[0.0, 0.0, Zc[j]] for j in range(ny - 2, 0, -1)])
        centre = [width / 2, 0.0, -depth / 2]
        for k in range(len(rim)):
            add_tri([centre, rim[k], rim[(k + 1) % len(rim)]], down)

    P.append(np.array(extra_p, float).reshape(-1, 3))
    N.append(np.array(extra_n, float).reshape(-1, 3))
    C.append(np.tile(base_col, (len(extra_p), 1)))
    Ilist.append(np.array(extra_i, dtype=np.int64))
    return {
        "P": np.concatenate(P).astype(np.float32),
        "N": np.concatenate(N).astype(np.float32),
        "C": np.concatenate(C).astype(np.float32),
        "I": np.concatenate(Ilist).astype(np.uint32),
        "nx": nx, "ny": ny,
    }


# =============================================================================
# GLB
# =============================================================================
def _js_num(v):
    """Format a number the way JavaScript's JSON.stringify does (shortest round-trip, ints without .0)."""
    v = float(v)
    if v == int(v) and abs(v) < 1e21:
        return str(int(v))
    r = repr(v)
    if "e" in r:
        mant, ex = r.split("e")
        ex = int(ex)
        r = f"{mant}e{'+' if ex > 0 else '-'}{abs(ex)}"
    return r


def _js_json(o):
    if isinstance(o, dict):
        return "{" + ",".join(json.dumps(k) + ":" + _js_json(v) for k, v in o.items()) + "}"
    if isinstance(o, (list, tuple)):
        return "[" + ",".join(_js_json(v) for v in o) + "]"
    if isinstance(o, bool):
        return "true" if o else "false"
    if isinstance(o, (int, float, np.floating, np.integer)):
        return _js_num(o)
    return json.dumps(o, ensure_ascii=False)


def to_glb(m, name="Chromatogram", generator="GCxGC single-trace folder"):
    P, N, Cc, I = m["P"], m["N"], m["C"], m["I"]
    nv = len(P)
    lin = cmaps.srgb_to_linear(Cc.astype(np.float64)).astype(np.float32)
    mn = [float(x) for x in P.min(axis=0)]
    mx = [float(x) for x in P.max(axis=0)]
    parts = [P.tobytes(), N.tobytes(), lin.tobytes(), I.tobytes()]
    off, ln = [], 0
    for p in parts:
        off.append(ln)
        ln += len(p)
    binary = b"".join(parts)
    j = {
        "asset": {"version": "2.0", "generator": generator},
        "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0, "name": name}],
        "meshes": [{"name": name, "primitives": [{"attributes": {"POSITION": 0, "NORMAL": 1, "COLOR_0": 2},
                                                  "indices": 3, "material": 0}]}],
        "materials": [{"name": "VertexColour", "doubleSided": True,
                       "pbrMetallicRoughness": {"baseColorFactor": [1, 1, 1, 1], "metallicFactor": 0,
                                                "roughnessFactor": 0.75}}],
        "buffers": [{"byteLength": ln}],
        "bufferViews": [{"buffer": 0, "byteOffset": off[k], "byteLength": len(parts[k]),
                         "target": 34962 if k < 3 else 34963} for k in range(4)],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": nv, "type": "VEC3", "min": mn, "max": mx},
            {"bufferView": 1, "componentType": 5126, "count": nv, "type": "VEC3"},
            {"bufferView": 2, "componentType": 5126, "count": nv, "type": "VEC3"},
            {"bufferView": 3, "componentType": 5125, "count": int(len(I)), "type": "SCALAR"},
        ],
    }
    js = _js_json(j).encode("utf-8")
    jpad = (4 - len(js) % 4) % 4
    bpad = (4 - ln % 4) % 4
    total = 12 + 8 + len(js) + jpad + 8 + ln + bpad
    out = bytearray()
    out += struct.pack("<III", 0x46546C67, 2, total)
    out += struct.pack("<II", len(js) + jpad, 0x4E4F534A) + js + b" " * jpad
    out += struct.pack("<II", ln + bpad, 0x004E4942) + binary + b"\0" * bpad
    return bytes(out)


# =============================================================================
# Binary FBX 7.4
# =============================================================================
def _pI(v): return b"I" + struct.pack("<i", v)
def _pL(v): return b"L" + struct.pack("<q", v)
def _pD(v): return b"D" + struct.pack("<d", v)
def _pS(s): t = s.encode("utf-8") if isinstance(s, str) else bytes(s); return b"S" + struct.pack("<I", len(t)) + t
def _pR(b): return b"R" + struct.pack("<I", len(b)) + bytes(b)
def _parr(code, arr): raw = arr.tobytes(); return bytes([code]) + struct.pack("<III", len(arr), 0, len(raw)) + raw
def _pdA(a): return _parr(100, np.asarray(a, dtype="<f8"))
def _piA(a): return _parr(105, np.asarray(a, dtype="<i4"))
def _name_cls(n, cls): return n.encode() + b"\x00\x01" + cls.encode()


def to_fbx(m, name="Chromatogram", creator="GCxGC single-trace folder"):
    out = bytearray()

    def write_node(nd):
        nm, props, kids = (list(nd) + [[], []])[:3]
        hdr_at = len(out)
        out.extend(b"\0" * 12)
        nb = nm.encode()
        out.append(len(nb))
        out.extend(nb)
        plen = 0
        for p in props:
            out.extend(p)
            plen += len(p)
        if kids:
            for k in kids:
                write_node(k)
            out.extend(b"\0" * 13)
        struct.pack_into("<III", out, hdr_at, len(out), len(props), plen)

    def P70(lst):
        return ["Properties70", [], [["P", p] for p in lst]]

    P, Nn, Cc, I = m["P"], m["N"], m["C"], m["I"]
    nv = len(P)
    verts = P.astype(np.float64).ravel() * 100                     # metres -> cm
    pvi = I.astype(np.int64).copy()
    pvi[2::3] = ~pvi[2::3]
    cols = np.ones((nv, 4))
    cols[:, :3] = Cc.astype(np.float64)
    geom, model, mat = 1000001, 1000002, 1000003
    I_ = _pI
    S = _pS
    tree = [
        ["FBXHeaderExtension", [], [["FBXHeaderVersion", [I_(1003)]], ["FBXVersion", [I_(7400)]],
                                    ["Creator", [S(creator)]]]],
        ["FileId", [_pR(bytes([0x28, 0xb3, 0x2a, 0xeb, 0xb6, 0x24, 0xcc, 0xc2, 0xbf, 0xc8, 0xb0, 0x2a, 0xa9, 0x2b,
                               0xfc, 0xf1]))]],
        ["CreationTime", [S("1970-01-01 10:00:00:000")]],
        ["Creator", [S(creator)]],
        ["GlobalSettings", [], [["Version", [I_(1000)]], P70([
            [S("UpAxis"), S("int"), S("Integer"), S(""), I_(1)], [S("UpAxisSign"), S("int"), S("Integer"), S(""), I_(1)],
            [S("FrontAxis"), S("int"), S("Integer"), S(""), I_(2)], [S("FrontAxisSign"), S("int"), S("Integer"), S(""), I_(1)],
            [S("CoordAxis"), S("int"), S("Integer"), S(""), I_(0)], [S("CoordAxisSign"), S("int"), S("Integer"), S(""), I_(1)],
            [S("OriginalUpAxis"), S("int"), S("Integer"), S(""), I_(1)],
            [S("OriginalUpAxisSign"), S("int"), S("Integer"), S(""), I_(1)],
            [S("UnitScaleFactor"), S("double"), S("Number"), S(""), _pD(1)],
            [S("OriginalUnitScaleFactor"), S("double"), S("Number"), S(""), _pD(1)],
        ])]],
        ["Documents", [], [["Count", [I_(1)]], ["Document", [_pL(1000000), S(""), S("Scene")],
                                                  [P70([]), ["RootNode", [_pL(0)]]]]]],
        ["References", []],
        ["Definitions", [], [["Version", [I_(100)]], ["Count", [I_(4)]],
                             ["ObjectType", [S("GlobalSettings")], [["Count", [I_(1)]]]],
                             ["ObjectType", [S("Model")], [["Count", [I_(1)]]]],
                             ["ObjectType", [S("Geometry")], [["Count", [I_(1)]]]],
                             ["ObjectType", [S("Material")], [["Count", [I_(1)]]]]]],
        ["Objects", [], [
            ["Geometry", [_pL(geom), S(_name_cls(name, "Geometry")), S("Mesh")], [
                P70([]),
                ["GeometryVersion", [I_(124)]],
                ["Vertices", [_pdA(verts)]],
                ["PolygonVertexIndex", [_piA(pvi)]],
                ["LayerElementNormal", [I_(0)], [["Version", [I_(101)]], ["Name", [S("")]],
                                                 ["MappingInformationType", [S("ByVertice")]],
                                                 ["ReferenceInformationType", [S("Direct")]],
                                                 ["Normals", [_pdA(Nn.astype(np.float64).ravel())]]]],
                ["LayerElementColor", [I_(0)], [["Version", [I_(101)]], ["Name", [S("Col")]],
                                                ["MappingInformationType", [S("ByVertice")]],
                                                ["ReferenceInformationType", [S("Direct")]],
                                                ["Colors", [_pdA(cols.ravel())]]]],
                ["LayerElementMaterial", [I_(0)], [["Version", [I_(101)]], ["Name", [S("")]],
                                                   ["MappingInformationType", [S("AllSame")]],
                                                   ["ReferenceInformationType", [S("IndexToDirect")]],
                                                   ["Materials", [_piA([0])]]]],
                ["Layer", [I_(0)], [["Version", [I_(100)]],
                                    ["LayerElement", [], [["Type", [S("LayerElementNormal")]], ["TypedIndex", [I_(0)]]]],
                                    ["LayerElement", [], [["Type", [S("LayerElementColor")]], ["TypedIndex", [I_(0)]]]],
                                    ["LayerElement", [], [["Type", [S("LayerElementMaterial")]], ["TypedIndex", [I_(0)]]]]]],
            ]],
            ["Model", [_pL(model), S(_name_cls(name, "Model")), S("Mesh")],
             [["Version", [I_(232)]], P70([]), ["Shading", [bytes([67, 1])]], ["Culling", [S("CullingOff")]]]],
            ["Material", [_pL(mat), S(_name_cls("VertexColour", "Material")), S("")],
             [["Version", [I_(102)]], ["ShadingModel", [S("lambert")]], ["MultiLayer", [I_(0)]],
              P70([[S("DiffuseColor"), S("Color"), S(""), S("A"), _pD(1), _pD(1), _pD(1)]])]],
        ]],
        ["Connections", [], [["C", [S("OO"), _pL(model), _pL(0)]], ["C", [S("OO"), _pL(geom), _pL(model)]],
                             ["C", [S("OO"), _pL(mat), _pL(model)]]]],
    ]
    out += b"Kaydara FBX Binary  " + bytes([0, 0x1a, 0]) + struct.pack("<I", 7400)
    for nd in tree:
        write_node(nd)
    out += b"\0" * 13
    out += bytes([0xfa, 0xbc, 0xab, 0x09, 0xd0, 0xc8, 0xd4, 0x66, 0xb1, 0x76, 0xfb, 0x83, 0x1c, 0xf7, 0x26, 0x7e])
    out += b"\0" * 4
    pad = (16 - (len(out) % 16)) % 16
    out += b"\0" * (pad or 16)
    out += struct.pack("<I", 7400) + b"\0" * 120
    out += bytes([0xf8, 0x5a, 0x8c, 0x6a, 0xde, 0xf5, 0xd9, 0x7e, 0xec, 0xe9, 0x0c, 0xe3, 0x75, 0x8f, 0x29, 0x0b])
    return bytes(out)


# =============================================================================
# STL (binary, Z-up, millimetres)
# =============================================================================
def to_stl(m, name="Chromatogram", scale_to_mm=1000.0):
    P = m["P"].astype(np.float64) * scale_to_mm
    P = np.stack([P[:, 0], -P[:, 2], P[:, 1]], axis=1)              # Y-up -> Z-up (height = Z)
    tri = P[m["I"].astype(np.int64).reshape(-1, 3)]
    nrm = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    ln = np.linalg.norm(nrm, axis=1)
    ln[ln == 0] = 1
    nrm = nrm / ln[:, None]
    rec = np.zeros(len(tri), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
    rec["n"] = nrm
    rec["v"] = tri
    hdr = (f"GCxGC chromatogram {name}"[:79]).encode("ascii", "replace").ljust(80, b" ")
    return hdr + struct.pack("<I", len(tri)) + rec.tobytes()


# =============================================================================
def model_grid(T, j0, j1, r0, r1, max1=1000, smooth=True):
    """
    Cut the region of the (modulations x points) display matrix and prepare it as the
    page's Z (rows = 2nd dimension).  Fewer modulations than max1 are cubic-interpolated
    up (smooth surface); more are max-pooled down so peaks survive.
    """
    from scipy import ndimage
    R = np.asarray(T[j0:j1 + 1, r0:r1 + 1], float)
    n1 = R.shape[0]
    if n1 > max1:
        pool = math.ceil(n1 / max1)
        n = (n1 // pool) * pool
        R = R[:n].reshape(-1, pool, R.shape[1]).max(axis=1)
    elif smooth and n1 >= 4 and n1 < max1:
        f = min(8.0, max1 / n1)
        R = np.clip(ndimage.zoom(R, (f, 1), order=3, mode="nearest", grid_mode=True), R.min(), R.max())
    return np.maximum(R, 0).T
