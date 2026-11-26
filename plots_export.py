"""
plots_export.py - publication-quality figure export (matplotlib).

Exact physical size (mm) and DPI are honoured, so a 160 x 90 mm figure at 600 dpi
is exactly 3780 x 2126 px.  SVG and PDF are vector and ignore DPI.
"""
from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.ticker import ScalarFormatter  # noqa: E402

MM = 1 / 25.4


def _style(ax, font):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.tick_params(labelsize=font, width=0.6, length=3)
    for s in ("left", "bottom"):
        ax.spines[s].set_linewidth(0.6)
    fmt = ScalarFormatter(useMathText=True)
    fmt.set_powerlimits((-3, 4))
    ax.yaxis.set_major_formatter(fmt)
    ax.yaxis.get_offset_text().set_fontsize(font)


def _fig(width_mm, height_mm):
    return plt.figure(figsize=(width_mm * MM, height_mm * MM), layout="constrained")


def chromatogram_figure(path, t, y, *, xlim=None, title="", ylabel="Abundance",
                        peaks=(), shade_peaks=True, label_peaks=True,
                        width_mm=160, height_mm=90, dpi=600, font=9, color="#000000",
                        markers=()):
    """markers: iterable of (rt, label) to draw as thin vertical lines (e.g. selected scan)."""
    fig = _fig(width_mm, height_mm)
    ax = fig.add_subplot(111)
    x0, x1 = xlim if xlim else (t[0], t[-1])
    m = (t >= x0) & (t <= x1)
    ax.plot(t, y, lw=0.7, color=color)
    ymax = float(y[m].max()) if m.any() else float(y.max())
    ymin = min(0.0, float(y[m].min()) if m.any() else 0.0)
    for p in peaks:
        if p.t_end < x0 or p.t_start > x1:
            continue
        seg_t = t[p.i0:p.i1 + 1]
        seg_y = y[p.i0:p.i1 + 1]
        base = p.baseline_at(seg_t)
        if shade_peaks:
            ax.fill_between(seg_t, base, seg_y, color="#4C78A8", alpha=0.30, lw=0)
            ax.plot([p.t_start, p.t_end], [p.b0, p.b1], lw=0.6, color="#C0392B")
        if label_peaks:
            ya = float(np.interp(p.rt_apex, t, y))
            ax.annotate(f"{p.rt_apex:.2f}", (p.rt_apex, ya), xytext=(0, 2), textcoords="offset points",
                        ha="center", va="bottom", fontsize=font - 1)
    for rt, lab in markers:
        ax.axvline(rt, lw=0.5, ls="--", color="#888888")
    ax.set_xlim(x0, x1)
    ax.set_ylim(ymin, ymax * (1.10 if label_peaks and peaks else 1.05))
    ax.set_xlabel("Retention time (min)", fontsize=font)
    ax.set_ylabel(ylabel, fontsize=font)
    if title:
        ax.set_title(title, fontsize=font, loc="left")
    _style(ax, font)
    fig.savefig(path, dpi=dpi)
    plt.close(fig)


def _pick_labels(mz, inten, n, min_sep):
    """Greedy: strongest peaks first, skipping any closer than min_sep to one already chosen."""
    chosen = []
    for k in np.argsort(inten)[::-1]:
        if len(chosen) >= n or inten[k] <= 0:
            break
        if all(abs(mz[k] - mz[c]) >= min_sep for c in chosen):
            chosen.append(k)
    return chosen


def spectrum_figure(path, mz, inten, *, xlim=None, title="", relative=True, n_labels=10,
                    decimals=1, width_mm=160, height_mm=90, dpi=600, font=9, color="#000000",
                    mirror=None, mirror_color="#C82828"):
    """mirror: optional (label, mz, intensity) library spectrum drawn downwards (head-to-tail plot)."""
    fig = _fig(width_mm, height_mm)
    ax = fig.add_subplot(111)
    mz = np.asarray(mz, float)
    inten = np.asarray(inten, float)
    if xlim is None:
        xlim = (np.floor(mz.min() / 10) * 10 - 5, np.ceil(mz.max() / 10) * 10 + 5) if mz.size else (0, 100)
    m = (mz >= xlim[0]) & (mz <= xlim[1])
    if relative and inten.size and inten[m].max() > 0:
        y = inten / inten[m].max() * 100
        ylabel = "Relative abundance (%)"
    else:
        y = inten
        ylabel = "Abundance"
    ax.vlines(mz[m], 0, y[m], lw=0.8, color=color)
    if n_labels and m.any():
        idx = np.nonzero(m)[0]
        for k in _pick_labels(mz[idx], y[idx], n_labels, (xlim[1] - xlim[0]) * 0.025):
            j = idx[k]
            ax.annotate(f"{mz[j]:.{decimals}f}", (mz[j], y[j]), xytext=(0, 2), textcoords="offset points",
                        ha="center", va="bottom", fontsize=font - 1)
    ymax = float(y[m].max()) if m.any() else 1.0
    ax.set_xlim(*xlim)
    ax.set_ylim(0, ymax * 1.12)
    if mirror is not None:
        mlabel, lmz, lab = mirror
        lmz, lab = np.asarray(lmz, float), np.asarray(lab, float)
        lm = (lmz >= xlim[0]) & (lmz <= xlim[1])
        ly = lab / (lab[lm].max() if lm.any() and lab[lm].max() > 0 else 1) * ymax
        ax.vlines(lmz[lm], 0, -ly[lm], lw=0.8, color=mirror_color)
        ax.axhline(0, lw=0.6, color="#555555")
        if n_labels and lm.any():
            idx = np.nonzero(lm)[0]
            for k in _pick_labels(lmz[idx], ly[idx], max(3, n_labels // 2), (xlim[1] - xlim[0]) * 0.03):
                j = idx[k]
                ax.annotate(f"{lmz[j]:.0f}", (lmz[j], -ly[j]), xytext=(0, -2), textcoords="offset points",
                            ha="center", va="top", fontsize=font - 1, color=mirror_color)
        ax.set_ylim(-ymax * 1.34, ymax * 1.12)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{abs(v):g}"))
        ax.text(0.01, 0.98, "Sample", transform=ax.transAxes, ha="left", va="top", fontsize=font - 1)
        ax.text(0.01, 0.02, f"Library: {mlabel}", transform=ax.transAxes, ha="left", va="bottom",
                fontsize=font - 1, color=mirror_color)
    ax.set_xlabel("m/z", fontsize=font, style="italic")
    ax.set_ylabel(ylabel, fontsize=font)
    if title:
        ax.set_title(title, fontsize=font, loc="left")
    fmt = ax.yaxis.get_major_formatter() if mirror is not None else None
    _style(ax, font)
    if fmt is not None:
        ax.yaxis.set_major_formatter(fmt)
    fig.savefig(path, dpi=dpi)
    plt.close(fig)


def gcxgc_figure(path, M, extent, *, xlim=None, ylim=None, cmap="turbo", scale="sqrt", levels=None,
                 interpolation="bicubic", blobs=(), colorbar=True, title="", width_mm=160, height_mm=100,
                 dpi=600, font=9, **_):
    """
    GCxGC colour plot.  M: (modulations x points) matrix; extent: (x0, x1, y0, y1) in min / s;
    levels: (low, high) in *display* units (after the sqrt/log transform), as set on screen;
    blobs: iterable of (x0, x1, y0, y1, label).
    """
    from matplotlib.patches import Rectangle
    from gcxgc import transform
    T = transform(np.asarray(M, float), scale)
    fig = _fig(width_mm, height_mm)
    ax = fig.add_subplot(111)
    vmin, vmax = levels if levels else (float(np.percentile(T, 50)), float(T.max()))
    im = ax.imshow(T.T, origin="lower", aspect="auto", extent=extent, cmap=cmap, vmin=vmin, vmax=vmax,
                   interpolation=interpolation, interpolation_stage="data", rasterized=True)
    for x0, x1, y0, y1, lab in blobs:
        ax.add_patch(Rectangle((x0, y0), x1 - x0, y1 - y0, fill=False, lw=0.7, ec="white"))
        ax.annotate(lab, (x0, y1), xytext=(1, 1), textcoords="offset points", fontsize=font - 2,
                    color="white", ha="left", va="bottom")
    ax.set_xlim(*(xlim or extent[:2]))
    ax.set_ylim(*(ylim or extent[2:]))
    ax.set_xlabel(r"$^{1}t_{R}$ (min)", fontsize=font)
    ax.set_ylabel(r"$^{2}t_{R}$ (s)", fontsize=font)
    ax.tick_params(labelsize=font, width=0.6, length=3)
    if title:
        ax.set_title(title, fontsize=font, loc="left")
    if colorbar:
        cb = fig.colorbar(im, ax=ax, pad=0.01, fraction=0.04)
        cb.ax.tick_params(labelsize=font - 1)
        cb.set_label({"sqrt": "√abundance", "log": "log₁₀(1 + abundance)"}.get(scale, "Abundance"),
                     fontsize=font)
    fig.savefig(path, dpi=dpi)
    plt.close(fig)


def _spread_labels(xs, min_gap):
    """Shift label x positions apart (keeps order) so neighbouring labels don't overlap."""
    xs = np.asarray(xs, float)
    if xs.size < 2:
        return xs
    o = np.argsort(xs)
    v = xs[o].copy()
    for _ in range(200):
        moved = False
        for i in range(1, v.size):
            if v[i] - v[i - 1] < min_gap:
                d = (min_gap - (v[i] - v[i - 1])) / 2
                v[i - 1] -= d
                v[i] += d
                moved = True
        if not moved:
            break
    out = np.empty_like(v)
    out[o] = v
    return out


def nmr_figure(path, ppm, y, *, xlim=None, title="", xlabel="δ (ppm)", regions=(), integral_curves=True,
               show_integrals=True, peak_labels=(), multiplet_labels=(), dept=None,
               width_mm=160, height_mm=90, dpi=600, font=9, color="#000000", hide_y=True, **_):
    """NMR-style plot (x reversed).

    regions: [(lo, hi, label)] - integral values drawn under the axis (and integral curves)
    peak_labels: [(ppm, text)] - labels with leader lines above the spectrum (13C / peak lists)
    multiplet_labels: [(ppm, text)] - e.g. "d, J = 7.5 Hz" above each multiplet
    dept: optional (ppm, y, label) drawn in a second panel underneath
    """
    fig = _fig(width_mm, height_mm)
    if dept is not None:
        gs = fig.add_gridspec(2, 1, height_ratios=[1.5, 1])
        ax = fig.add_subplot(gs[0])
        ax2 = fig.add_subplot(gs[1], sharex=ax)
    else:
        ax = fig.add_subplot(111)
        ax2 = None
    ppm = np.asarray(ppm, float)
    y = np.asarray(y, float)
    x0, x1 = (max(xlim), min(xlim)) if xlim else (ppm.max(), ppm.min())
    m = (ppm <= x0) & (ppm >= x1)
    ymax = float(y[m].max()) if m.any() else float(y.max())
    ymin = float(min(0.0, y[m].min() if m.any() else 0.0))
    ax.plot(ppm, y, lw=0.6, color=color)
    top = ymax
    if regions and integral_curves:
        ints = []
        for lo, hi, _lab in regions:
            r = (ppm >= lo) & (ppm <= hi)
            if r.sum() > 1:
                xi, yi = ppm[r], y[r]
                if xi.size > 1 and xi[0] < xi[-1]:
                    xi, yi = xi[::-1], yi[::-1]
                c = np.concatenate([[0], np.cumsum((yi[1:] + yi[:-1]) / 2 * -np.diff(xi))])
                ints.append((xi, c))
        cmax = max((c[-1] for _, c in ints), default=0) or 1
        for xi, c in ints:
            ax.plot(xi, ymax * 0.05 + c / cmax * ymax * 0.35, lw=0.6, color="#2a7a2a")
    labels = list(peak_labels) + list(multiplet_labels)
    label_frac = 0.0
    vis = []
    if labels:
        span = abs(x0 - x1)
        vis = [(p, t) for p, t in labels if x1 <= p <= x0]
    if vis:
        xs = np.array([p for p, _ in vis])
        axes_w_mm = width_mm * 0.92
        axes_h_mm = height_mm * (0.78 if ax2 is None else 0.45)
        gap = span * ((font + 1) * 0.3528 * 1.15) / axes_w_mm          # one rotated text line, in ppm
        lx = _spread_labels(xs, gap)
        # keep labels inside the plot
        lo_lim, hi_lim = x1 + gap * 0.6, x0 - gap * 0.6
        if lx.max() > hi_lim:
            lx -= lx.max() - hi_lim
        if lx.min() < lo_lim:
            lx += lo_lim - lx.min()
        nchar = max(len(t) for _, t in vis)
        label_frac = min(0.45, nchar * (font - 1) * 0.3528 * 0.62 / axes_h_mm + 0.06)
        ylab = ymax * 1.10
        for (p, t), xl in zip(vis, lx):
            yp = float(np.interp(p, ppm[::-1], y[::-1]))
            ax.plot([p, p, xl, xl], [yp + ymax * 0.02, ymax * 1.02, ymax * 1.07, ymax * 1.09],
                    lw=0.4, color="#666666")
            ax.text(xl, ylab, t, rotation=90, ha="center", va="bottom", fontsize=font - 1)
    bottom = ymin - (0.10 * ymax if (regions and show_integrals) else 0.02 * ymax)
    if regions and show_integrals:
        for lo, hi, lab in regions:
            if hi < x1 or lo > x0:
                continue
            yb = ymin - 0.03 * ymax
            ax.plot([hi, hi, lo, lo], [yb + 0.012 * ymax, yb, yb, yb + 0.012 * ymax], lw=0.6, color="#2a7a2a")
            ax.text((lo + hi) / 2, yb - 0.01 * ymax, lab, ha="center", va="top", fontsize=font - 1,
                    color="#2a7a2a")
    ax.set_xlim(x0, x1)
    if vis:
        ylab = ymax * 1.10
        top = (ylab - label_frac * bottom) / (1 - label_frac)
    ax.set_ylim(bottom, max(top, ymax * 1.05))
    if title:
        ax.set_title(title, fontsize=font, loc="left")
    _style(ax, font)
    if hide_y:
        ax.spines["left"].set_visible(False)
        ax.set_yticks([])
        ax.yaxis.get_offset_text().set_visible(False)
    if ax2 is not None:
        dp, dy, dlab = dept
        dp, dy = np.asarray(dp, float), np.asarray(dy, float)
        ax2.plot(dp, dy, lw=0.6, color="#1f3b73")
        ax2.axhline(0, lw=0.4, color="#999999")
        dm = (dp <= x0) & (dp >= x1)
        a = float(np.abs(dy[dm]).max()) if dm.any() else 1.0
        ax2.set_ylim(-a * 1.1, a * 1.1)
        ax2.set_title(dlab, fontsize=font - 1, loc="left")
        _style(ax2, font)
        ax2.spines["left"].set_visible(False)
        ax2.set_yticks([])
        ax2.yaxis.get_offset_text().set_visible(False)
        ax2.set_xlabel(xlabel, fontsize=font)
        plt.setp(ax.get_xticklabels(), visible=False)
    else:
        ax.set_xlabel(xlabel, fontsize=font)
    fig.savefig(path, dpi=dpi)
    plt.close(fig)


def compare_figure(path, traces, ids, groups, raw_times, *, stacked=True, xlim=None, numbers=True,
                   ylabel="Abundance", normalise=False, ys_raw=None, title="",
                   width_mm=160, height_mm=120, dpi=600, font=9, color=None, **_):
    """traces: [(name, aligned_t, y_display, colour, shift)] for visible samples, ids = their sample indices.
    groups: [(number, rt, {sample_index: Peak|None})] - shaded peaks / 'n.d.' markers."""
    fig = _fig(width_mm, height_mm)
    n = len(traces)
    if stacked and n > 1:
        axes = fig.subplots(n, 1, sharex=True)
    else:
        axes = [fig.add_subplot(111)] * max(1, n)
    x0, x1 = xlim if xlim else (min(t[1].min() for t in traces), max(t[1].max() for t in traces))
    for j, ((name, t, y, col, shift), k) in enumerate(zip(traces, ids)):
        ax = axes[j]
        m = (t >= x0) & (t <= x1)
        ax.plot(t, y, lw=0.7, color=col, label=name)
        ymax = float(y[m].max()) if m.any() else float(y.max())
        ymin = float(min(0, y[m].min() if m.any() else 0))
        for num, rt, peaks in groups:
            if not (x0 <= rt <= x1):
                continue
            p = peaks.get(k)
            if p is None:
                if stacked:
                    ax.axvline(rt, lw=0.6, ls="--", color="#c0392b")
                    ax.text(rt, ymax * 0.92, "n.d.", fontsize=font - 2, color="#c0392b", ha="center", va="top",
                            rotation=90)
                continue
            seg_t = t[p.i0:p.i1 + 1]
            seg_y = y[p.i0:p.i1 + 1]
            base = p.baseline_at(raw_times[k][p.i0:p.i1 + 1])
            if normalise and ys_raw is not None:
                yr = ys_raw[k]
                top = float(yr.max() - np.median(yr)) or 1.0
                base = (base - np.median(yr)) / top * 100
            ax.fill_between(seg_t, base, seg_y, color=col if not stacked else "#F39C12", alpha=0.35, lw=0)
            if numbers and (stacked or j == 0):
                ia = int(np.argmax(seg_y - base))
                ax.annotate(str(num), (seg_t[ia], seg_y[ia]), xytext=(0, 2), textcoords="offset points",
                            ha="center", va="bottom", fontsize=font - 1)
        if stacked or j == 0:
            ax.set_xlim(x0, x1)
            ax.set_ylim(ymin, ymax * 1.15)
            _style(ax, font)
        if stacked:
            ax.set_title(name if len(name) <= 70 else name[:68] + "…", loc="left", fontsize=font - 1, color=col,
                         fontweight="bold", pad=2)
    if not stacked:
        ax = axes[0]
        tops = [float(y[(t >= x0) & (t <= x1)].max()) for _, t, y, _, _ in traces if ((t >= x0) & (t <= x1)).any()]
        ax.set_ylim(min(0, ax.get_ylim()[0]), (max(tops) if tops else 1) * 1.15)
        ax.legend(fontsize=font - 1, frameon=False, loc="upper right")
        ax.set_ylabel(ylabel, fontsize=font)
    else:
        fig.supylabel(ylabel, fontsize=font)
    axes[-1].set_xlabel("Retention time (min)", fontsize=font)
    if title:
        fig.suptitle(title, fontsize=font)
    fig.savefig(path, dpi=dpi)
    plt.close(fig)


def gcxgc_grid_figure(path, panels, *, cmap="turbo", xlim=None, ylim=None, boxes=(), interpolation="bicubic",
                      colorbar=False, show_blobs=True, title="", width_mm=170, height_mm=120, dpi=600, font=8,
                      bg=None, **_):
    """Side-by-side GCxGC plots. panels: [(name, T_display, extent, (lo, hi))];
    boxes: [(number, (x0, x1, y0, y1), [found per panel], selected)]."""
    from matplotlib.patches import Rectangle
    n = len(panels)
    cols = 1 if n == 1 else 2 if n <= 4 else 3
    rows = int(np.ceil(n / cols))
    fig = _fig(width_mm, height_mm)
    axes = np.atleast_1d(fig.subplots(rows, cols, sharex=True, sharey=True, squeeze=False)).ravel()
    for ax in axes[n:]:
        ax.set_visible(False)
    for k, (name, T, extent, lv) in enumerate(panels):
        ax = axes[k]
        ax.imshow(np.asarray(T, float).T, origin="lower", aspect="auto", extent=extent, cmap=cmap,
                  vmin=lv[0], vmax=lv[1], interpolation=interpolation, interpolation_stage="data",
                  rasterized=True)
        if show_blobs:
            for num, (x0, x1, y0, y1), found, sel in boxes:
                ok = found[k] if k < len(found) else False
                col = ("#ffa500" if sel else "white") if ok else "#ff5050"
                ax.add_patch(Rectangle((x0, y0), x1 - x0, y1 - y0, fill=False, lw=0.9 if sel else 0.6, ec=col,
                                       ls="-" if ok else "--"))
                ax.annotate(f"{num}" + ("" if ok else " n.d."), (x0, y1), xytext=(1, 1), textcoords="offset points",
                            fontsize=font - 2, color=col, ha="left", va="bottom")
        ax.set_xlim(*(xlim or extent[:2]))
        ax.set_ylim(*(ylim or extent[2:]))
        short = name if len(name) <= 38 else name[:36] + "…"
        ax.set_title(short, loc="left", fontsize=font, pad=2)
        ax.tick_params(labelsize=font - 1, width=0.5, length=2)
    fig.supxlabel(r"$^{1}t_{R}$ (min)", fontsize=font)
    fig.supylabel(r"$^{2}t_{R}$ (s)", fontsize=font)
    if title:
        fig.suptitle(title, fontsize=font)
    fig.savefig(path, dpi=dpi)
    plt.close(fig)


def xy_figure(path, traces, *, xlim=None, title="", xlabel="x", ylabel="y", reverse_x=False, labels=(),
              labels_down=False, sticks=False, regions=(), offset=0.0, legend=True,
              width_mm=160, height_mm=90, dpi=600, font=9, color="#000000", **_):
    """General spectrum plot. traces: [(name, x, y, colour)]; labels: [(x, text)] for the first trace;
    labels_down: labels hang below minima (IR in %T); regions: [(lo, hi, text)] shaded integrals."""
    fig = _fig(width_mm, height_mm)
    ax = fig.add_subplot(111)
    first = traces[0]
    xs_all = np.concatenate([np.asarray(t[1], float) for t in traces])
    x0, x1 = (min(xlim), max(xlim)) if xlim else (float(xs_all.min()), float(xs_all.max()))
    ymin, ymax = np.inf, -np.inf
    for k, (name, x, y, col) in enumerate(traces):
        x = np.asarray(x, float)
        y = np.asarray(y, float) + k * offset
        m = (x >= x0) & (x <= x1)
        c = col or (color if k == 0 else None)
        if sticks:
            ax.vlines(x[m], 0, y[m], lw=0.8, color=c, label=name)
        else:
            ax.plot(x, y, lw=0.7, color=c, label=name)
        if m.any():
            ymin, ymax = min(ymin, float(y[m].min())), max(ymax, float(y[m].max()))
    if not np.isfinite(ymin):
        ymin, ymax = 0.0, 1.0
    span = ymax - ymin or 1.0
    name, fx, fy, fc = first
    fx, fy = np.asarray(fx, float), np.asarray(fy, float)
    for lo, hi, txt in regions:
        r = (fx >= lo) & (fx <= hi)
        if r.sum() > 1:
            xx, yy = fx[r], fy[r]
            base = yy[0] + (yy[-1] - yy[0]) * (xx - xx[0]) / (xx[-1] - xx[0])
            ax.fill_between(xx, base, yy, color="#4C78A8", alpha=0.3, lw=0)
            k = int(np.argmax(yy - base))
            ax.annotate(txt, (xx[k], yy[k]), xytext=(0, 2), textcoords="offset points", ha="center",
                        va="bottom", fontsize=font - 1, color="#2b4f7a")
    lab_room = 0.0
    vis = [(p, t) for p, t in labels if x0 <= p <= x1]
    if vis:
        o = np.argsort([p for p, _ in vis])
        vis = [vis[i] for i in o]
        xs = np.array([p for p, _ in vis])
        gap = (x1 - x0) * ((font + 1) * 0.3528 * 1.15) / (width_mm * 0.9)
        lx = _spread_labels(xs, gap)
        lo_lim, hi_lim = x0 + gap * 0.6, x1 - gap * 0.6
        if lx.max() > hi_lim:
            lx -= lx.max() - hi_lim
        if lx.min() < lo_lim:
            lx += lo_lim - lx.min()
        nchar = max(len(t) for _, t in vis)
        lab_room = min(0.45, nchar * (font - 1) * 0.3528 * 0.62 / (height_mm * 0.78) + 0.06)
        for (p, t), xl in zip(vis, lx):
            yp = float(np.interp(p, fx, fy))
            if labels_down:
                yl = ymin - 0.03 * span
                ax.plot([p, p, xl, xl], [yp - 0.01 * span, ymin - 0.005 * span, ymin - 0.02 * span, yl],
                        lw=0.4, color="#666666")
                ax.text(xl, yl - 0.005 * span, t, rotation=90, ha="center", va="top", fontsize=font - 1)
            else:
                yl = ymax + 0.03 * span
                ax.plot([p, p, xl, xl], [yp + 0.01 * span, ymax + 0.005 * span, ymax + 0.02 * span, yl],
                        lw=0.4, color="#666666")
                ax.text(xl, yl + 0.005 * span, t, rotation=90, ha="center", va="bottom", fontsize=font - 1)
    lo_y = ymin - (0.04 * span if not sticks else 0)
    hi_y = ymax + 0.06 * span
    total = hi_y - lo_y
    if vis and labels_down:
        lo_y = (lo_y - lab_room * hi_y) / (1 - lab_room) if lab_room < 1 else lo_y - total
    elif vis:
        hi_y = (hi_y - lab_room * lo_y) / (1 - lab_room) if lab_room < 1 else hi_y + total
    if sticks:
        lo_y = 0
    ax.set_xlim((x1, x0) if reverse_x else (x0, x1))
    ax.set_ylim(lo_y, hi_y)
    ax.set_xlabel(xlabel, fontsize=font)
    ax.set_ylabel(ylabel, fontsize=font)
    if title:
        ax.set_title(title, fontsize=font, loc="left")
    if legend and len(traces) > 1:
        ax.legend(fontsize=font - 1, frameon=False)
    _style(ax, font)
    fig.savefig(path, dpi=dpi)
    plt.close(fig)


def spectra_stack_figure(path, panels, *, ref_name="", title="", width_mm=120, height_mm=None, dpi=600, font=8,
                         n_labels=6, **_):
    """Mass spectra stacked one per sample. panels: [(title, colour, mz, ab, decimals, ref_or_None)]."""
    n = len(panels)
    height_mm = height_mm or max(45, 38 * n)
    fig = _fig(width_mm, height_mm)
    axes = np.atleast_1d(fig.subplots(n, 1, sharex=True, squeeze=False)).ravel()
    lo = min(float(np.min(p[2])) for p in panels)
    hi = max(float(np.max(p[2])) for p in panels)
    x0, x1 = max(0.0, np.floor(lo / 10) * 10 - 5), np.ceil(hi / 10) * 10 + 5
    for ax, (ttl, col, mz, ab, dec, ref) in zip(axes, panels):
        ax.vlines(mz, 0, ab, color=col, lw=0.8)
        top = 100.0
        lowest = 0.0
        if ref is not None:
            ax.vlines(ref[0], 0, -np.asarray(ref[1]), color="#888888", lw=0.8)
            ax.axhline(0, color="#999999", lw=0.4)
            lowest = -105.0
            ax.text(0.995, 0.04, f"reference: {ref_name}", transform=ax.transAxes, ha="right", va="bottom",
                    fontsize=font - 2, color="#777777")
        order = np.argsort(ab)[::-1]
        used = []
        for i in order:
            if len(used) >= n_labels or ab[i] < 3:
                break
            if any(abs(mz[i] - u) < (x1 - x0) * 0.035 for u in used):
                continue
            used.append(mz[i])
            ax.annotate(f"{mz[i]:.{dec}f}", (mz[i], ab[i]), xytext=(0, 1.5), textcoords="offset points",
                        ha="center", va="bottom", fontsize=font - 2)
        ax.set_xlim(x0, x1)
        ax.set_ylim(lowest, top * 1.18)
        ax.set_title(ttl if len(ttl) <= 80 else ttl[:78] + "…", loc="left", fontsize=font - 1, color=col,
                     fontweight="bold", pad=2)
        _style(ax, font)
    axes[-1].set_xlabel("m/z", fontsize=font)
    fig.supylabel("Relative abundance (%)", fontsize=font)
    if title:
        fig.suptitle(title, fontsize=font)
    fig.savefig(path, dpi=dpi)
    plt.close(fig)
