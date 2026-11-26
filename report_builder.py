"""
report_builder.py - put figures, tables and report text from the data windows together into a
student or client report and export it as PDF or Word (.docx).

Content arrives from any window's Report menu (Add … to report), from the figure maker, or is typed
here.  A report can be saved (.wgreport, a JSON file with the images inside) and opened again later.
"""
from __future__ import annotations

import base64
import html
import io
import json
import os
from dataclasses import asdict, dataclass, field

from PySide6 import QtCore, QtGui, QtWidgets

from common import APP_NAME, __version__, settings

MM_PX = 96 / 25.4            # document units are 1/96 inch
PAGE_W, PAGE_H = 210.0, 297.0
MARGIN_L = MARGIN_R = 20.0
MARGIN_T, MARGIN_B = 32.0, 20.0
BODY_W = PAGE_W - MARGIN_L - MARGIN_R

KIND_NAMES = {"heading": "Heading", "text": "Text", "image": "Figure", "table": "Table", "pagebreak": "Page break"}


# =============================================================================
# model
@dataclass
class Block:
    kind: str                       # heading | text | image | table | pagebreak
    text: str = ""                  # heading text, or caption for figures / tables
    html: str = ""                  # text blocks (rich text)
    png: bytes = b""                # figures
    width_mm: float = 160.0
    header: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    note: str = ""                  # where it came from
    prompt: bool = False            # template prompt not yet edited (left out of exports)

    def summary(self):
        if self.kind == "heading":
            return self.text or "(heading)"
        if self.kind == "text":
            t = QtGui.QTextDocument()
            t.setHtml(self.html)
            s = " ".join(t.toPlainText().split())
            return (s[:70] + "…") if len(s) > 70 else (s or "(empty text)")
        if self.kind == "image":
            return self.text or "(figure)"
        if self.kind == "table":
            return self.text or f"({len(self.rows)} rows)"
        return "────────"

    def to_json(self):
        d = asdict(self)
        d["png"] = base64.b64encode(self.png).decode("ascii") if self.png else ""
        return d

    @classmethod
    def from_json(cls, d):
        d = dict(d)
        d["png"] = base64.b64decode(d.get("png") or "")
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


def _p(text):
    return f"<p>{html.escape(text)}</p>"


STUDENT_FIELDS = [("title", "Report title", "Laboratory report"), ("name", "Name", ""), ("sid", "Student ID", ""),
                  ("module", "Module", ""), ("sample", "Experiment / sample", ""), ("date", "Date", "@today")]
CLIENT_FIELDS = [("title", "Report title", "Analytical report"), ("number", "Report number", ""),
                 ("client", "Client", ""), ("contact", "Client point of contact", ""),
                 ("samples", "Sample ID(s)", ""), ("received", "Date received", ""),
                 ("reported", "Date reported", "@today"), ("analyst", "Analyst", "")]


def _prompt(t):
    return Block("text", html=_p(t), prompt=True)


TEMPLATES = {
    "student": dict(
        name="Student – laboratory report", style="student", fields=STUDENT_FIELDS,
        blocks=lambda: [Block("heading", "Aim"), _prompt("[State the aim of the experiment.]"),
                        Block("heading", "Experimental"),
                        _prompt("[Instrument, column, method conditions and sample preparation.]"),
                        Block("heading", "Results"),
                        _prompt("[Add figures, tables and report text from the data windows with "
                                "Report ▸ Add … to report. They are inserted after the selected item.]"),
                        Block("heading", "Discussion"), _prompt("[Interpret your results.]"),
                        Block("heading", "Conclusion"), _prompt("[Summarise what you found.]"),
                        Block("heading", "References"), _prompt("[List your references.]")]),
    "characterisation": dict(
        name="Student – compound characterisation", style="student",
        fields=STUDENT_FIELDS[:5] + [("compound", "Compound", "")] + STUDENT_FIELDS[5:],
        blocks=lambda: [Block("heading", "Compound"),
                        _prompt("[Name, molecular formula, molecular weight and structure "
                                "(insert a structure image, or build a figure in the figure maker).]"),
                        Block("heading", "Characterisation data"),
                        _prompt("[Add the NMR and IR report text from the data windows: "
                                "Report ▸ Add the report text to report.]"),
                        Block("heading", "Spectra"),
                        _prompt("[Add the annotated spectra and chromatograms.]"),
                        Block("heading", "Discussion"),
                        _prompt("[Assign the signals and comment on purity.]")]),
    "client": dict(
        name="Client – analysis report", style="client", fields=CLIENT_FIELDS,
        blocks=lambda: [Block("heading", "Summary"), _prompt("[Summary of the findings.]"),
                        Block("heading", "Samples"),
                        Block("table", "Samples analysed", header=["Sample ID", "Description", "Date received"],
                              rows=[["", "", ""]], prompt=True),
                        Block("heading", "Method"),
                        _prompt("[Technique, instrument, column / conditions and any sample preparation.]"),
                        Block("heading", "Results"), _prompt("[Results, with figures and tables.]"),
                        Block("heading", "Comments"),
                        _prompt("[Limitations, uncertainty and any other notes.]")]),
    "blank": dict(name="Blank student report", style="student", fields=STUDENT_FIELDS, blocks=lambda: []),
    "blank_client": dict(name="Blank client report", style="client", fields=CLIENT_FIELDS, blocks=lambda: []),
}


class Report:
    def __init__(self, template="student"):
        t = TEMPLATES[template]
        self.template = template
        self.style = t["style"]
        today = QtCore.QDate.currentDate().toString("d MMMM yyyy")
        self.fields = {k: (today if d == "@today" else d) for k, _, d in t["fields"]}
        self.blocks: list[Block] = t["blocks"]()
        self.path = None

    @property
    def field_defs(self):
        return TEMPLATES[self.template]["fields"]

    def to_json(self):
        return {"app": f"{APP_NAME} report", "version": 1, "template": self.template,
                "fields": self.fields, "blocks": [b.to_json() for b in self.blocks]}

    @classmethod
    def from_json(cls, d):
        r = cls(d.get("template", "student") if d.get("template") in TEMPLATES else "student")
        r.fields.update(d.get("fields", {}))
        r.blocks = [Block.from_json(b) for b in d.get("blocks", [])]
        return r

    def export_blocks(self):
        return [b for b in self.blocks if not b.prompt]


# =============================================================================
# rich text helpers
def rich_paragraphs(src_html):
    """[[(text, bold, italic, underline, 'super'|'sub'|'')]] from rich text (one list per paragraph)."""
    doc = QtGui.QTextDocument()
    doc.setHtml(src_html)
    out = []
    b = doc.begin()
    VA = QtGui.QTextCharFormat.VerticalAlignment
    while b.isValid():
        runs = []
        it = b.begin()
        while not it.atEnd():
            f = it.fragment()
            if f.isValid():
                cf = f.charFormat()
                va = cf.verticalAlignment()
                pos = "super" if va == VA.AlignSuperScript else "sub" if va == VA.AlignSubScript else ""
                runs.append((f.text(), cf.fontWeight() >= 600, cf.fontItalic(), cf.fontUnderline(), pos))
            it += 1
        out.append(runs)
        b = b.next()
    while out and not "".join(r[0] for r in out[-1]).strip():
        out.pop()
    return out


def clean_html(src_html):
    """Rich text -> simple HTML (p, b, i, u, sup, sub, br) for the report document."""
    paras = []
    for runs in rich_paragraphs(src_html):
        s = ""
        for text, b, i, u, pos in runs:
            t = html.escape(text).replace(" ", "<br>")
            if pos:
                t = f"<{'sup' if pos == 'super' else 'sub'}>{t}</{'sup' if pos == 'super' else 'sub'}>"
            if u:
                t = f"<u>{t}</u>"
            if i:
                t = f"<i>{t}</i>"
            if b:
                t = f"<b>{t}</b>"
            s += t
        paras.append(f"<p>{s or '&nbsp;'}</p>")
    return "".join(paras)


def letterhead():
    s = settings()
    lines = [x for x in (s.value("report/facility", "University of Plymouth") or "").splitlines()]
    return s.value("report/logo", "") or "", lines


def load_logo(path):
    if not path or not os.path.exists(path):
        return None
    if path.lower().endswith(".svg"):
        from PySide6 import QtSvg
        r = QtSvg.QSvgRenderer(path)
        if not r.isValid():
            return None
        sz = r.defaultSize()
        k = 1200 / max(1, sz.height())
        img = QtGui.QImage(int(sz.width() * k), 1200, QtGui.QImage.Format_ARGB32)
        img.fill(0)
        p = QtGui.QPainter(img)
        r.render(p)
        p.end()
        return img
    img = QtGui.QImage(path)
    return None if img.isNull() else img


def png_bytes(img: QtGui.QImage):
    ba = QtCore.QByteArray()
    buf = QtCore.QBuffer(ba)
    buf.open(QtCore.QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(ba)


# =============================================================================
# HTML (preview and PDF)
CSS = """
body { font-family: Arial, Helvetica, sans-serif; font-size: 10pt; color: #111; }
h1 { font-size: 17pt; font-weight: 600; margin: 0 0 6px 0; color: #1f3b73; }
h2 { font-size: 12.5pt; font-weight: 600; margin: 14px 0 4px 0; color: #1f3b73; }
p { margin: 0 0 6px 0; }
p.cap { font-size: 9pt; color: #333; margin: 2px 0 10px 0; }
p.cap-t { font-size: 9pt; color: #333; margin: 8px 0 3px 0; }
table.fields td { padding: 2px 10px 2px 0; }
table.data th { background: #e8ecf4; font-weight: 600; }
"""


def report_html(rep: Report, *, preview=False, image_url=lambda k: f"img{k}"):
    out = [f"<html><head><style>{CSS}</style></head><body>"]
    f = rep.fields
    out.append(f"<h1>{html.escape(f.get('title', '') or 'Report')}</h1>")
    rows = [(lab, f.get(k, "")) for k, lab, _ in rep.field_defs if k != "title" and f.get(k, "")]
    if rows:
        out.append('<table class="fields" cellspacing="0" cellpadding="2">')
        for lab, v in rows:
            out.append(f"<tr><td><b>{html.escape(lab)}</b></td><td>{html.escape(v)}</td></tr>")
        out.append("</table><hr>")
    nfig = ntab = 0
    blocks = rep.blocks if preview else rep.export_blocks()
    for k, b in enumerate(blocks):
        dim = ' style="color:#9a9a9a"' if (preview and b.prompt) else ""
        if b.kind == "heading":
            out.append(f"<h2>{html.escape(b.text)}</h2>")
        elif b.kind == "text":
            body = clean_html(b.html)
            out.append(f"<div{dim}>{body}</div>")
        elif b.kind == "image" and b.png:
            nfig += 1
            w = min(b.width_mm, BODY_W) * MM_PX
            out.append(f'<p align="center"><img src="{image_url(id(b))}" width="{w:.0f}"></p>')
            cap = f"<b>Figure {nfig}.</b> {html.escape(b.text)}" if b.text else f"<b>Figure {nfig}.</b>"
            out.append(f'<p class="cap" align="center">{cap}</p>')
        elif b.kind == "table":
            ntab += 1
            cap = f"<b>Table {ntab}.</b> {html.escape(b.text)}" if b.text else f"<b>Table {ntab}.</b>"
            out.append(f'<p class="cap-t"{dim}>{cap}</p>')
            out.append('<table class="data" border="1" cellspacing="0" cellpadding="3" width="100%"'
                       ' style="border-collapse:collapse; border-color:#999;">')
            out.append("<tr>" + "".join(f"<th>{html.escape(str(h))}</th>" for h in b.header) + "</tr>")
            for r in b.rows:
                out.append("<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in r) + "</tr>")
            out.append("</table><p></p>")
        elif b.kind == "pagebreak":
            out.append('<p style="page-break-after:always">' +
                       ('<span style="color:#999">— page break —</span>' if preview else "&nbsp;") + "</p>")
    out.append("</body></html>")
    return "".join(out)


def _layout_device():
    img = QtGui.QImage(4, 4, QtGui.QImage.Format_RGB32)
    img.setDotsPerMeterX(round(96 / 0.0254))
    img.setDotsPerMeterY(round(96 / 0.0254))
    return img


def build_document(rep: Report):
    doc = QtGui.QTextDocument()
    dev = _layout_device()
    doc.documentLayout().setPaintDevice(dev)
    doc._dev = dev                      # keep alive
    doc.setDefaultFont(QtGui.QFont("Arial", 10))
    doc.setDocumentMargin(0)
    for b in rep.export_blocks():
        if b.kind == "image" and b.png:
            img = QtGui.QImage.fromData(b.png)
            if img.hasAlphaChannel():                       # flatten onto white
                flat = QtGui.QImage(img.size(), QtGui.QImage.Format_RGB32)
                flat.fill(QtGui.QColor("white"))
                q = QtGui.QPainter(flat)
                q.drawImage(0, 0, img)
                q.end()
                img = flat
            doc.addResource(QtGui.QTextDocument.ImageResource, QtCore.QUrl(f"img{id(b)}"), img)
    doc.setHtml(report_html(rep))
    doc.setPageSize(QtCore.QSizeF(BODY_W * MM_PX, (PAGE_H - MARGIN_T - MARGIN_B) * MM_PX))
    return doc


def _font(pt, bold=False):
    """Font for drawing in 1/96-inch units (the painter is scaled to the PDF's resolution)."""
    f = QtGui.QFont("Arial")
    f.setPixelSize(max(1, round(pt * 96 / 72)))
    f.setBold(bold)
    return f


def _footer_left(rep):
    f = rep.fields
    if rep.style == "client":
        return f"Report no. {f.get('number')}" if f.get("number") else f.get("title", "")
    return " · ".join(x for x in (f.get("name", ""), f.get("sid", "")) if x)


def export_pdf(rep: Report, path, dpi=300):
    doc = build_document(rep)
    n = doc.pageCount()
    w = QtGui.QPdfWriter(path)
    w.setPageSize(QtGui.QPageSize(QtGui.QPageSize.A4))
    w.setPageMargins(QtCore.QMarginsF(0, 0, 0, 0))
    w.setResolution(dpi)
    w.setTitle(rep.fields.get("title", "Report"))
    w.setCreator(f"{APP_NAME} {__version__}")
    p = QtGui.QPainter(w)
    p.setRenderHint(QtGui.QPainter.LosslessImageRendering, True)     # keep figures PNG-sharp, no JPEG
    p.setRenderHint(QtGui.QPainter.Antialiasing, True)
    k = dpi / 96.0
    logo_path, lines = letterhead()
    logo = load_logo(logo_path) if rep.style == "client" or settings().value("report/logo_student", "false") == "true" \
        else None
    body_h = (PAGE_H - MARGIN_T - MARGIN_B) * MM_PX
    small = _font(8)
    for i in range(n):
        if i:
            w.newPage()
        p.save()
        p.scale(k, k)
        # header
        x0, x1 = MARGIN_L * MM_PX, (PAGE_W - MARGIN_R) * MM_PX
        top = 10 * MM_PX
        hh = 15 * MM_PX
        if logo is not None:
            lw = hh * logo.width() / max(1, logo.height())
            if lw > 70 * MM_PX:
                lw = 70 * MM_PX
                lh = lw * logo.height() / max(1, logo.width())
            else:
                lh = hh
            p.drawImage(QtCore.QRectF(x0, top + (hh - lh) / 2, lw, lh), logo)
        if rep.style == "client" and lines:
            p.setPen(QtGui.QColor("#1f3b73"))
            y = top + 3
            for j, ln in enumerate(lines[:4]):
                f = _font(9 if j == 0 else 7.5, j == 0)
                p.setFont(f)
                fm = QtGui.QFontMetricsF(f)
                p.drawText(QtCore.QPointF(x1 - fm.horizontalAdvance(ln), y + fm.ascent()), ln)
                y += fm.height()
        elif rep.style == "student":
            p.setFont(small)
            p.setPen(QtGui.QColor("#666666"))
            t = rep.fields.get("title", "")
            fm = QtGui.QFontMetricsF(small)
            p.drawText(QtCore.QPointF(x1 - fm.horizontalAdvance(t), top + hh - 4), t)
        p.setPen(QtGui.QPen(QtGui.QColor("#1f3b73"), 0.8))
        p.drawLine(QtCore.QPointF(x0, top + hh + 4), QtCore.QPointF(x1, top + hh + 4))
        # body
        p.save()
        p.translate(MARGIN_L * MM_PX, MARGIN_T * MM_PX - i * body_h)
        clip = QtCore.QRectF(0, i * body_h, BODY_W * MM_PX, body_h)
        p.setClipRect(clip)
        doc.drawContents(p, clip)
        p.restore()
        # footer
        p.setFont(small)
        p.setPen(QtGui.QColor("#555555"))
        fy = (PAGE_H - 10) * MM_PX
        p.drawLine(QtCore.QPointF(x0, fy - 14), QtCore.QPointF(x1, fy - 14))
        p.drawText(QtCore.QPointF(x0, fy), _footer_left(rep))
        pg_txt = f"Page {i + 1} of {n}"
        fm = QtGui.QFontMetricsF(small)
        p.drawText(QtCore.QPointF(x1 - fm.horizontalAdvance(pg_txt), fy), pg_txt)
        p.restore()
    p.end()
    return n


# =============================================================================
# Word
def export_docx(rep: Report, path):
    import docx
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Mm, Pt, RGBColor

    blue = RGBColor(0x1F, 0x3B, 0x73)
    d = docx.Document()
    st = d.styles["Normal"]
    st.font.name = "Arial"
    st.font.size = Pt(10)
    for name, size in (("Title", 17), ("Heading 1", 12.5)):
        s = d.styles[name]
        s.font.name = "Arial"
        s.font.size = Pt(size)
        s.font.color.rgb = blue
        s.font.bold = True
    sec = d.sections[0]
    sec.page_width, sec.page_height = Mm(PAGE_W), Mm(PAGE_H)
    sec.left_margin, sec.right_margin = Mm(MARGIN_L), Mm(MARGIN_R)
    sec.top_margin, sec.bottom_margin = Mm(MARGIN_T), Mm(MARGIN_B)
    sec.header_distance, sec.footer_distance = Mm(10), Mm(8)

    # header: logo left, facility right
    logo_path, lines = letterhead()
    use_logo = rep.style == "client" or settings().value("report/logo_student", "false") == "true"
    logo = load_logo(logo_path) if use_logo else None
    hdr = sec.header
    ht = hdr.add_table(rows=1, cols=2, width=Mm(BODY_W))
    ht.alignment = WD_TABLE_ALIGNMENT.CENTER
    c0, c1 = ht.rows[0].cells
    if logo is not None:
        run = c0.paragraphs[0].add_run()
        hmm = 15.0
        wmm = hmm * logo.width() / max(1, logo.height())
        if wmm > 70:
            run.add_picture(io.BytesIO(png_bytes(logo)), width=Mm(70))
        else:
            run.add_picture(io.BytesIO(png_bytes(logo)), height=Mm(hmm))
    if rep.style == "client":
        for j, ln in enumerate(lines[:4]):
            par = c1.paragraphs[0] if j == 0 else c1.add_paragraph()
            par.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            par.paragraph_format.space_after = Pt(0)
            r = par.add_run(ln)
            r.font.size = Pt(9 if j == 0 else 7.5)
            r.font.bold = j == 0
            r.font.color.rgb = blue
    else:
        par = c1.paragraphs[0]
        par.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        r = par.add_run(rep.fields.get("title", ""))
        r.font.size = Pt(8)
        r.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
    if hdr.paragraphs and not hdr.paragraphs[0].text:
        hp = hdr.paragraphs[0]._p
        hp.getparent().remove(hp)

    # footer: left text, right "Page x of y"
    from docx.enum.text import WD_TAB_ALIGNMENT
    ft = sec.footer
    fp = ft.paragraphs[0]
    fp.style = d.styles["Normal"]              # the Footer style's own centre/right tabs would get in the way
    fp.paragraph_format.tab_stops.add_tab_stop(Mm(BODY_W), WD_TAB_ALIGNMENT.RIGHT)

    def small(par, txt=None):
        r = par.add_run(txt)
        r.font.size = Pt(8)
        r.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
        return r

    def field(par, code):
        for tag, txt in (("begin", None), ("instr", code), ("separate", None), ("text", "1"), ("end", None)):
            r = small(par)
            if tag == "instr":
                e = OxmlElement("w:instrText")
                e.set(qn("xml:space"), "preserve")
                e.text = f" {code} "
            elif tag == "text":
                e = OxmlElement("w:t")
                e.text = txt
            else:
                e = OxmlElement("w:fldChar")
                e.set(qn("w:fldCharType"), tag)
            r._r.append(e)

    small(fp, _footer_left(rep) + "\tPage ")
    field(fp, "PAGE")
    small(fp, " of ")
    field(fp, "NUMPAGES")

    # title + fields
    d.add_paragraph(rep.fields.get("title", "") or "Report", style="Title")
    rows = [(lab, rep.fields.get(k, "")) for k, lab, _ in rep.field_defs if k != "title" and rep.fields.get(k, "")]
    if rows:
        t = d.add_table(rows=len(rows), cols=2)
        t.autofit = False
        for (lab, v), row in zip(rows, t.rows):
            for c, w_, txt, bold in ((row.cells[0], 45, lab, True), (row.cells[1], BODY_W - 45, v, False)):
                c.width = Mm(w_)
                par = c.paragraphs[0]
                par.paragraph_format.space_after = Pt(1)
                par.paragraph_format.space_before = Pt(1)
                par.add_run(txt).bold = bold
        bot = d.add_paragraph()
        bdr = OxmlElement("w:pBdr")
        b_ = OxmlElement("w:bottom")
        for k_, v_ in (("w:val", "single"), ("w:sz", "6"), ("w:space", "1"), ("w:color", "1F3B73")):
            b_.set(qn(k_), v_)
        bdr.append(b_)
        bot._p.get_or_add_pPr().append(bdr)

    def add_runs(par, runs):
        for text, b, i, u, pos in runs:
            parts = text.split(" ")
            for j, part in enumerate(parts):
                if j:
                    par.add_run().add_break()
                if not part:
                    continue
                r = par.add_run(part)
                r.bold, r.italic, r.underline = b or None, i or None, u or None
                if pos == "super":
                    r.font.superscript = True
                elif pos == "sub":
                    r.font.subscript = True

    nfig = ntab = 0
    for b in rep.export_blocks():
        if b.kind == "heading":
            d.add_heading(b.text, level=1)
        elif b.kind == "text":
            for runs in rich_paragraphs(b.html):
                add_runs(d.add_paragraph(), runs)
        elif b.kind == "image" and b.png:
            nfig += 1
            par = d.add_paragraph()
            par.alignment = WD_ALIGN_PARAGRAPH.CENTER
            par.paragraph_format.keep_with_next = True
            par.add_run().add_picture(io.BytesIO(b.png), width=Mm(min(b.width_mm, BODY_W)))
            cap = d.add_paragraph()
            cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
            r = cap.add_run(f"Figure {nfig}. ")
            r.bold = True
            r.font.size = Pt(9)
            r = cap.add_run(b.text)
            r.font.size = Pt(9)
        elif b.kind == "table":
            ntab += 1
            cap = d.add_paragraph()
            cap.paragraph_format.keep_with_next = True
            r = cap.add_run(f"Table {ntab}. ")
            r.bold = True
            r.font.size = Pt(9)
            r = cap.add_run(b.text)
            r.font.size = Pt(9)
            ncol = max([len(b.header)] + [len(r_) for r_ in b.rows]) or 1
            t = d.add_table(rows=1 + len(b.rows), cols=ncol)
            t.style = "Table Grid"
            for j, h in enumerate(b.header):
                c = t.rows[0].cells[j]
                c.paragraphs[0].add_run(str(h)).bold = True
                sh = OxmlElement("w:shd")
                sh.set(qn("w:val"), "clear")
                sh.set(qn("w:color"), "auto")
                sh.set(qn("w:fill"), "E8ECF4")
                c._tc.get_or_add_tcPr().append(sh)
            for i_, row in enumerate(b.rows):
                for j, v in enumerate(row[:ncol]):
                    t.rows[i_ + 1].cells[j].paragraphs[0].add_run(str(v))
            for row in t.rows:
                for c in row.cells:
                    for par in c.paragraphs:
                        par.paragraph_format.space_after = Pt(0)
                        for r in par.runs:
                            r.font.size = Pt(9)
            d.add_paragraph()
        elif b.kind == "pagebreak":
            d.add_page_break()
    d.core_properties.title = rep.fields.get("title", "")
    d.core_properties.author = rep.fields.get("analyst") or rep.fields.get("name") or ""
    d.save(path)


# =============================================================================
# UI
class TextEditor(QtWidgets.QWidget):
    changed = QtCore.Signal()

    def __init__(self):
        super().__init__()
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        tb = QtWidgets.QToolBar()
        tb.setIconSize(QtCore.QSize(16, 16))
        self.edit = QtWidgets.QTextEdit()
        self.edit.setAcceptRichText(True)
        self.edit.setFont(QtGui.QFont("Arial", 10))
        for txt, tip, fn, key in (("B", "Bold (Ctrl+B)", self._bold, "Ctrl+B"),
                                  ("I", "Italic (Ctrl+I)", self._italic, "Ctrl+I"),
                                  ("U", "Underline (Ctrl+U)", self._under, "Ctrl+U"),
                                  ("x²", "Superscript (Ctrl+Shift+=)", lambda: self._va("super"), "Ctrl+Shift+="),
                                  ("x₂", "Subscript (Ctrl+=)", lambda: self._va("sub"), "Ctrl+=")):
            a = tb.addAction(txt, fn)
            a.setToolTip(tip)
            a.setShortcut(key)
            a.setShortcutContext(QtCore.Qt.WidgetWithChildrenShortcut)
        tb.addSeparator()
        for ch in ("°", "±", "×", "μ", "δ", "ν", "λ", "α", "β", "→", "⇌", "≈", "≤", "≥", "−"):
            tb.addAction(ch, lambda c=ch: self.edit.insertPlainText(c)).setToolTip(f"Insert {ch}")
        v.addWidget(tb)
        v.addWidget(self.edit, 1)
        self.edit.textChanged.connect(self.changed.emit)

    def _merge(self, fmt):
        c = self.edit.textCursor()
        if not c.hasSelection():
            c.select(QtGui.QTextCursor.WordUnderCursor)
        c.mergeCharFormat(fmt)
        self.edit.mergeCurrentCharFormat(fmt)

    def _bold(self):
        f = QtGui.QTextCharFormat()
        f.setFontWeight(QtGui.QFont.Normal if self.edit.fontWeight() >= 600 else QtGui.QFont.Bold)
        self._merge(f)

    def _italic(self):
        f = QtGui.QTextCharFormat()
        f.setFontItalic(not self.edit.fontItalic())
        self._merge(f)

    def _under(self):
        f = QtGui.QTextCharFormat()
        f.setFontUnderline(not self.edit.fontUnderline())
        self._merge(f)

    def _va(self, kind):
        VA = QtGui.QTextCharFormat.VerticalAlignment
        cur = self.edit.currentCharFormat().verticalAlignment()
        want = VA.AlignSuperScript if kind == "super" else VA.AlignSubScript
        f = QtGui.QTextCharFormat()
        f.setVerticalAlignment(VA.AlignNormal if cur == want else want)
        self._merge(f)


class ReportWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = settings()
        self.rep = Report("student")
        self.dirty = False
        self._loading = False
        self.setWindowTitle(f"{APP_NAME} – Report builder")
        self._build()
        self._load_report()
        self.resize(1250, 860)

    # ---------------------------------------------------------------- UI
    def _build(self):
        mb = self.menuBar()
        fm = mb.addMenu("&File")
        nm = fm.addMenu("New report")
        for key, t in TEMPLATES.items():
            nm.addAction(t["name"], lambda k=key: self.new_report(k))
        fm.addAction("Open report…", self.open_report).setShortcut("Ctrl+O")
        fm.addAction("Save", self.save_report).setShortcut("Ctrl+S")
        fm.addAction("Save as…", lambda: self.save_report(True)).setShortcut("Ctrl+Shift+S")
        fm.addSeparator()
        fm.addAction("Export PDF…", self.export_pdf).setShortcut("Ctrl+P")
        fm.addAction("Export Word (.docx)…", self.export_docx).setShortcut("Ctrl+E")
        fm.addSeparator()
        fm.addAction("Close", self.close)
        im = mb.addMenu("&Insert")
        im.addAction("Heading", lambda: self.insert(Block("heading", "New section")))
        im.addAction("Text", lambda: self.insert(Block("text", html="<p></p>")))
        im.addAction("Image from file (structure, photo…)…", self.insert_image_file)
        im.addAction("Image from clipboard", self.insert_clipboard_image).setShortcut("Ctrl+Shift+V")
        im.addAction("Table", lambda: self.insert(Block("table", "", header=["Column 1", "Column 2", "Column 3"],
                                                        rows=[["", "", ""], ["", "", ""]])))
        im.addAction("Page break", lambda: self.insert(Block("pagebreak")))
        lm = mb.addMenu("&Letterhead")
        lm.addAction("Logo…", self.choose_logo)
        lm.addAction("Remove logo", lambda: (self.settings.remove("report/logo"), self._refresh_preview()))
        lm.addAction("Organisation name and address…", self.edit_facility)
        self.act_logo_student = lm.addAction("Use the logo on student reports too")
        self.act_logo_student.setCheckable(True)
        self.act_logo_student.setChecked(self.settings.value("report/logo_student", "false") == "true")
        self.act_logo_student.toggled.connect(lambda on: self.settings.setValue("report/logo_student",
                                                                                 str(on).lower()))
        hm = mb.addMenu("&Help")
        hm.addAction("How to build a report", self.show_help)

        tb = self.addToolBar("Main")
        tb.setMovable(False)
        tb.addAction("New…", self.new_report_dialog)
        tb.addAction("Open…", self.open_report)
        tb.addAction("Save", self.save_report)
        tb.addSeparator()
        tb.addAction("Export PDF…", self.export_pdf)
        tb.addAction("Export Word…", self.export_docx)

        # left: details + contents
        left = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(left)
        lv.setContentsMargins(6, 6, 6, 6)
        self.tmpl_label = QtWidgets.QLabel()
        self.tmpl_label.setStyleSheet("font-weight:600; color:#1f3b73;")
        lv.addWidget(self.tmpl_label)
        self.fields_box = QtWidgets.QGroupBox("Report details")
        self.fields_form = QtWidgets.QFormLayout(self.fields_box)
        lv.addWidget(self.fields_box)
        lv.addWidget(QtWidgets.QLabel("<b>Contents</b>  (drag to reorder)"))
        self.list = QtWidgets.QListWidget()
        self.list.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.list.model().rowsMoved.connect(self._rows_moved)
        self.list.currentRowChanged.connect(self._select_block)
        lv.addWidget(self.list, 1)
        hb = QtWidgets.QHBoxLayout()
        add = QtWidgets.QToolButton()
        add.setText("Add ▾")
        add.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        add.setMenu(im)
        hb.addWidget(add)
        for txt, tip, fn in (("↑", "Move up", lambda: self.move(-1)), ("↓", "Move down", lambda: self.move(1)),
                             ("Duplicate", "Duplicate the selected item", self.duplicate),
                             ("Remove", "Remove the selected item", self.remove)):
            b = QtWidgets.QPushButton(txt)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            hb.addWidget(b)
        lv.addLayout(hb)
        dele = QtGui.QShortcut(QtGui.QKeySequence.Delete, self.list)
        dele.setContext(QtCore.Qt.WidgetShortcut)
        dele.activated.connect(self.remove)

        # right: editor + preview
        self.tabs = QtWidgets.QTabWidget()
        self.editor = QtWidgets.QStackedWidget()
        self.tabs.addTab(self.editor, "Edit")
        self.preview = QtWidgets.QTextBrowser()
        self.preview.setOpenLinks(False)
        self.tabs.addTab(self.preview, "Preview")
        self.tabs.currentChanged.connect(lambda i: self._refresh_preview() if i == 1 else None)

        # editors
        self.ed_empty = QtWidgets.QLabel("Select an item in the contents list to edit it.\n\n"
                                         "Add figures, tables and report text from the data windows with "
                                         "Report ▸ Add … to report.")
        self.ed_empty.setAlignment(QtCore.Qt.AlignCenter)
        self.ed_empty.setWordWrap(True)
        self.ed_empty.setStyleSheet("color:#666;")
        self.editor.addWidget(self.ed_empty)

        w = QtWidgets.QWidget()
        f = QtWidgets.QFormLayout(w)
        self.ed_heading = QtWidgets.QLineEdit()
        self.ed_heading.textEdited.connect(self._heading_edited)
        f.addRow("Heading", self.ed_heading)
        self.editor.addWidget(w)

        self.ed_text = TextEditor()
        self.ed_text.changed.connect(self._text_edited)
        self.editor.addWidget(self.ed_text)

        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        self.ed_img = QtWidgets.QLabel()
        self.ed_img.setAlignment(QtCore.Qt.AlignCenter)
        self.ed_img.setMinimumHeight(200)
        self.ed_img.setStyleSheet("background:white; border:1px solid #ccc;")
        self.ed_img.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Expanding)
        v.addWidget(self.ed_img, 1)
        f = QtWidgets.QFormLayout()
        self.ed_img_cap = QtWidgets.QLineEdit()
        self.ed_img_cap.setPlaceholderText("Caption (shown as “Figure n. …”)")
        self.ed_img_cap.textEdited.connect(self._caption_edited)
        self.ed_img_w = QtWidgets.QDoubleSpinBox()
        self.ed_img_w.setRange(20, BODY_W)
        self.ed_img_w.setSuffix(" mm")
        self.ed_img_w.setDecimals(0)
        self.ed_img_w.valueChanged.connect(self._width_edited)
        self.ed_img_info = QtWidgets.QLabel()
        self.ed_img_info.setStyleSheet("color:#666;")
        f.addRow("Caption", self.ed_img_cap)
        f.addRow("Width on the page", self.ed_img_w)
        f.addRow("", self.ed_img_info)
        hb = QtWidgets.QHBoxLayout()
        b = QtWidgets.QPushButton("Replace image…")
        b.clicked.connect(self.replace_image)
        hb.addWidget(b)
        b = QtWidgets.QPushButton("Save image as…")
        b.clicked.connect(self.save_image)
        hb.addWidget(b)
        b = QtWidgets.QPushButton("Open in figure maker")
        b.setToolTip("Add structures, labels and arrows to this figure")
        b.clicked.connect(self.image_to_figure)
        hb.addWidget(b)
        hb.addStretch(1)
        v.addLayout(f)
        v.addLayout(hb)
        self.editor.addWidget(w)

        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        f = QtWidgets.QFormLayout()
        self.ed_tab_cap = QtWidgets.QLineEdit()
        self.ed_tab_cap.setPlaceholderText("Caption (shown as “Table n. …”)")
        self.ed_tab_cap.textEdited.connect(self._caption_edited)
        f.addRow("Caption", self.ed_tab_cap)
        v.addLayout(f)
        self.ed_table = QtWidgets.QTableWidget()
        self.ed_table.itemChanged.connect(self._table_edited)
        self.ed_table.horizontalHeader().sectionDoubleClicked.connect(self._rename_column)
        v.addWidget(self.ed_table, 1)
        hb = QtWidgets.QHBoxLayout()
        for txt, fn in (("+ Row", lambda: self._table_shape(1, 0)), ("− Row", lambda: self._table_shape(-1, 0)),
                        ("+ Column", lambda: self._table_shape(0, 1)), ("− Column", lambda: self._table_shape(0, -1))):
            b = QtWidgets.QPushButton(txt)
            b.clicked.connect(fn)
            hb.addWidget(b)
        hb.addWidget(QtWidgets.QLabel("Double-click a column title to rename it."))
        hb.addStretch(1)
        v.addLayout(hb)
        self.editor.addWidget(w)

        lab = QtWidgets.QLabel("Page break – the next item starts on a new page.")
        lab.setAlignment(QtCore.Qt.AlignCenter)
        self.editor.addWidget(lab)
        self.EDIT_PAGE = {"heading": 1, "text": 2, "image": 3, "table": 4, "pagebreak": 5}

        split = QtWidgets.QSplitter()
        split.addWidget(left)
        split.addWidget(self.tabs)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        split.setSizes([430, 800])
        self.setCentralWidget(split)
        self.status = self.statusBar()

    # ---------------------------------------------------------------- report <-> UI
    def _load_report(self):
        self._loading = True
        t = TEMPLATES[self.rep.template]
        self.tmpl_label.setText(t["name"])
        while self.fields_form.rowCount():
            self.fields_form.removeRow(0)
        self.field_edits = {}
        for k, lab, _ in self.rep.field_defs:
            e = QtWidgets.QLineEdit(self.rep.fields.get(k, ""))
            e.textEdited.connect(lambda txt, k=k: self._field_edited(k, txt))
            self.fields_form.addRow(lab, e)
            self.field_edits[k] = e
        self._fill_list()
        self._loading = False
        self._select_block(self.list.currentRow())
        self._update_title()

    def _fill_list(self, select=None):
        self.list.blockSignals(True)
        self.list.clear()
        nfig = ntab = 0
        for b in self.rep.blocks:
            label = KIND_NAMES[b.kind]
            if b.kind == "image":
                nfig += 1
                label = f"Figure {nfig}"
            elif b.kind == "table":
                ntab += 1
                label = f"Table {ntab}"
            txt = f"{b.summary()}" if b.kind == "heading" else f"   {label}: {b.summary()}" \
                if b.kind in ("image", "table") else f"   {b.summary()}"
            it = QtWidgets.QListWidgetItem(txt)
            it.setData(QtCore.Qt.UserRole, id(b))
            if b.kind == "heading":
                f = it.font()
                f.setBold(True)
                it.setFont(f)
            if b.prompt:
                it.setForeground(QtGui.QColor("#999999"))
                it.setToolTip("Template prompt – edit it to include it in the report (prompts are left out "
                              "of exports until you change them)")
            elif b.note:
                it.setToolTip(f"From {b.note}")
            self.list.addItem(it)
        if select is not None and 0 <= select < self.list.count():
            self.list.setCurrentRow(select)
        self.list.blockSignals(False)

    def _update_title(self):
        name = os.path.basename(self.rep.path) if self.rep.path else "untitled"
        self.setWindowTitle(f"{APP_NAME} – Report builder – {name}{' *' if self.dirty else ''}")

    def _mark(self, relist=False):
        if self._loading:
            return
        self.dirty = True
        self._update_title()
        if relist:
            r = self.list.currentRow()
            self._fill_list(r)
        if self.tabs.currentIndex() == 1:
            self._refresh_preview()

    def _cur(self):
        r = self.list.currentRow()
        return self.rep.blocks[r] if 0 <= r < len(self.rep.blocks) else None

    def _select_block(self, row):
        b = self._cur()
        self._loading = True
        try:
            if b is None:
                self.editor.setCurrentIndex(0)
                return
            self.editor.setCurrentIndex(self.EDIT_PAGE[b.kind])
            if b.kind == "heading":
                self.ed_heading.setText(b.text)
            elif b.kind == "text":
                self.ed_text.edit.setHtml(b.html)
                if b.prompt:                      # typing replaces the prompt
                    self.ed_text.edit.selectAll()
            elif b.kind == "image":
                self.ed_img_cap.setText(b.text)
                self.ed_img_w.setValue(b.width_mm)
                img = QtGui.QImage.fromData(b.png)
                self._img_cache = img
                self._show_image()
                dpi = img.width() / (b.width_mm / 25.4) if b.width_mm else 0
                self.ed_img_info.setText(f"{img.width()} × {img.height()} px  ·  ≈ {dpi:.0f} dpi at this width"
                                         + (f"  ·  from {b.note}" if b.note else ""))
            elif b.kind == "table":
                self.ed_tab_cap.setText(b.text)
                self._fill_table(b)
        finally:
            self._loading = False
        if self.tabs.currentIndex() == 1:
            self._refresh_preview()

    def _show_image(self):
        img = getattr(self, "_img_cache", None)
        if img is None or img.isNull():
            self.ed_img.clear()
            return
        sz = self.ed_img.size()
        self.ed_img.setPixmap(QtGui.QPixmap.fromImage(img.scaled(
            max(50, sz.width() - 10), max(50, sz.height() - 10), QtCore.Qt.KeepAspectRatio,
            QtCore.Qt.SmoothTransformation)))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.editor.currentIndex() == 3:
            QtCore.QTimer.singleShot(0, self._show_image)

    def _fill_table(self, b):
        t = self.ed_table
        t.blockSignals(True)
        ncol = max([len(b.header)] + [len(r) for r in b.rows]) if (b.header or b.rows) else 1
        t.setColumnCount(ncol)
        t.setRowCount(len(b.rows))
        t.setHorizontalHeaderLabels([str(h) for h in b.header] + [""] * (ncol - len(b.header)))
        for i, r in enumerate(b.rows):
            for j in range(ncol):
                t.setItem(i, j, QtWidgets.QTableWidgetItem(str(r[j]) if j < len(r) else ""))
        t.resizeColumnsToContents()
        t.blockSignals(False)

    # ---------------------------------------------------------------- edits
    def _field_edited(self, k, txt):
        self.rep.fields[k] = txt
        self._mark()

    def _clear_prompt(self, b):
        if b.prompt:
            b.prompt = False
            r = self.list.currentRow()
            it = self.list.item(r)
            if it is not None:
                it.setForeground(QtGui.QColor("#000000"))
                it.setToolTip("")

    def _heading_edited(self, txt):
        b = self._cur()
        if b:
            b.text = txt
            self.list.currentItem().setText(txt)
            self._mark()

    def _text_edited(self):
        b = self._cur()
        if b is None or self._loading or b.kind != "text":
            return
        b.html = self.ed_text.edit.toHtml()
        self._clear_prompt(b)
        it = self.list.currentItem()
        if it is not None:
            it.setText(f"   {b.summary()}")
        self._mark()

    def _caption_edited(self, txt):
        b = self._cur()
        if b:
            b.text = txt
            self._clear_prompt(b)
            self._mark(relist=True)

    def _width_edited(self, v):
        b = self._cur()
        if b and not self._loading:
            b.width_mm = float(v)
            img = getattr(self, "_img_cache", None)
            if img is not None and v:
                dpi = img.width() / (v / 25.4)
                self.ed_img_info.setText(f"{img.width()} × {img.height()} px  ·  ≈ {dpi:.0f} dpi at this width")
            self._mark()

    def _table_edited(self, item):
        b = self._cur()
        if b is None or self._loading or b.kind != "table":
            return
        self._table_to_block(b)
        self._clear_prompt(b)
        self._mark()

    def _table_to_block(self, b):
        t = self.ed_table
        b.header = [(t.horizontalHeaderItem(j).text() if t.horizontalHeaderItem(j) else "")
                    for j in range(t.columnCount())]
        b.rows = [[(t.item(i, j).text() if t.item(i, j) else "") for j in range(t.columnCount())]
                  for i in range(t.rowCount())]

    def _rename_column(self, j):
        b = self._cur()
        if b is None or b.kind != "table":
            return
        cur = self.ed_table.horizontalHeaderItem(j).text() if self.ed_table.horizontalHeaderItem(j) else ""
        txt, ok = QtWidgets.QInputDialog.getText(self, "Column title", "Title:", text=cur)
        if ok:
            self.ed_table.setHorizontalHeaderItem(j, QtWidgets.QTableWidgetItem(txt))
            self._table_to_block(b)
            self._clear_prompt(b)
            self._mark()

    def _table_shape(self, dr, dc):
        b = self._cur()
        if b is None or b.kind != "table":
            return
        t = self.ed_table
        t.blockSignals(True)
        if dr > 0:
            t.insertRow(t.currentRow() + 1 if t.currentRow() >= 0 else t.rowCount())
        elif dr < 0 and t.rowCount() > 0:
            t.removeRow(t.currentRow() if t.currentRow() >= 0 else t.rowCount() - 1)
        if dc > 0:
            c = t.currentColumn() + 1 if t.currentColumn() >= 0 else t.columnCount()
            t.insertColumn(c)
            t.setHorizontalHeaderItem(c, QtWidgets.QTableWidgetItem(f"Column {c + 1}"))
        elif dc < 0 and t.columnCount() > 1:
            t.removeColumn(t.currentColumn() if t.currentColumn() >= 0 else t.columnCount() - 1)
        t.blockSignals(False)
        self._table_to_block(b)
        self._clear_prompt(b)
        self._mark()

    # ---------------------------------------------------------------- list operations
    def _default_slot(self):
        """With nothing selected, new content goes at the end of the Results (or Spectra) section."""
        bl = self.rep.blocks
        for name in ("Results", "Spectra", "Characterisation data"):
            for i, b in enumerate(bl):
                if b.kind == "heading" and b.text.strip().lower() == name.lower():
                    j = i + 1
                    while j < len(bl) and bl[j].kind != "heading":
                        j += 1
                    return j
        return len(bl)

    def insert(self, b: Block, select=True):
        r = self.list.currentRow()
        pos = r + 1 if 0 <= r < len(self.rep.blocks) else self._default_slot()
        self.rep.blocks.insert(pos, b)
        self._fill_list(pos if select else r)
        if select:
            self._select_block(pos)
        self._mark()
        return pos

    def move(self, d):
        r = self.list.currentRow()
        if r < 0 or not (0 <= r + d < len(self.rep.blocks)):
            return
        bl = self.rep.blocks
        bl[r], bl[r + d] = bl[r + d], bl[r]
        self._fill_list(r + d)
        self._mark()

    def duplicate(self):
        b = self._cur()
        if b is not None:
            self.insert(Block.from_json(b.to_json()))

    def remove(self):
        r = self.list.currentRow()
        if 0 <= r < len(self.rep.blocks):
            del self.rep.blocks[r]
            self._fill_list(min(r, len(self.rep.blocks) - 1))
            self._select_block(self.list.currentRow())
            self._mark()

    def _rows_moved(self, *_):
        ids = [self.list.item(i).data(QtCore.Qt.UserRole) for i in range(self.list.count())]
        by_id = {id(b): b for b in self.rep.blocks}
        self.rep.blocks = [by_id[i] for i in ids if i in by_id]
        QtCore.QTimer.singleShot(0, lambda: self._fill_list(self.list.currentRow()))
        self._mark()

    # ---------------------------------------------------------------- incoming content
    def add_item(self, item):
        kind = item.get("kind")
        src = item.get("source", "")
        if kind == "image":
            path = item["path"]
            if path.lower().endswith(".svg"):
                img = load_logo(path)
                data = png_bytes(img) if img is not None else b""
            else:
                with open(path, "rb") as fh:
                    data = fh.read()
            img = QtGui.QImage.fromData(data)
            w_mm = 160.0
            if not img.isNull() and img.dotsPerMeterX() > 0:
                w_mm = min(BODY_W, img.width() / img.dotsPerMeterX() * 1000)
            b = Block("image", item.get("caption", ""), png=data, width_mm=round(w_mm), note=src)
        elif kind == "table":
            b = Block("table", item.get("title", ""), header=item.get("header", []), rows=item.get("rows", []),
                      note=src)
        elif kind == "text":
            h = item.get("html") or f"<p>{html.escape(item.get('text', ''))}</p>"
            b = Block("text", html=h, note=src)
        else:
            return
        self.insert(b)
        self.status.showMessage(f"Added {KIND_NAMES[b.kind].lower()} from {src}" if src else "Added", 5000)

    def insert_image_file(self):
        fn, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Insert image", self.settings.value("open_dir", os.path.expanduser("~")),
            "Images (*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.svg)")
        if fn:
            self.add_item(dict(kind="image", path=fn, caption="", source=os.path.basename(fn)))

    def insert_clipboard_image(self):
        md = QtWidgets.QApplication.clipboard().mimeData()
        img = None
        if md.hasImage():
            img = QtGui.QImage(md.imageData())
        elif md.hasFormat("image/svg+xml"):
            from PySide6 import QtSvg
            r = QtSvg.QSvgRenderer(md.data("image/svg+xml"))
            if r.isValid():
                sz = r.defaultSize()
                k = 2400 / max(1, sz.width())
                img = QtGui.QImage(int(sz.width() * k), int(sz.height() * k), QtGui.QImage.Format_ARGB32)
                img.fill(QtGui.QColor("white"))
                p = QtGui.QPainter(img)
                r.render(p)
                p.end()
        if img is None or img.isNull():
            self.status.showMessage("There is no image on the clipboard", 4000)
            return
        self.insert(Block("image", "", png=png_bytes(img), width_mm=min(BODY_W, 120), note="clipboard"))

    def replace_image(self):
        b = self._cur()
        if b is None or b.kind != "image":
            return
        fn, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Replace image", "",
                                                      "Images (*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.svg)")
        if fn:
            img = load_logo(fn)
            if img is not None:
                b.png = png_bytes(img)
                self._select_block(self.list.currentRow())
                self._mark()

    def save_image(self):
        b = self._cur()
        if b is None or b.kind != "image":
            return
        fn, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save image", "figure.png", "PNG (*.png)")
        if fn:
            with open(fn, "wb") as fh:
                fh.write(b.png)

    def image_to_figure(self):
        b = self._cur()
        if b is None or b.kind != "image":
            return
        import tempfile
        fd, fn = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        with open(fn, "wb") as fh:
            fh.write(b.png)
        deliver("figure", dict(kind="image", path=fn, caption=b.text, source="report"), self)

    # ---------------------------------------------------------------- letterhead
    def choose_logo(self):
        fn, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Choose logo", "",
                                                      "Images (*.png *.jpg *.jpeg *.svg)")
        if fn:
            if load_logo(fn) is None:
                QtWidgets.QMessageBox.warning(self, APP_NAME, "That file couldn't be read as an image.")
                return
            self.settings.setValue("report/logo", fn)
            self.status.showMessage("Logo set – it appears in the header of client reports", 5000)
            self._refresh_preview()

    def edit_facility(self):
        cur = self.settings.value("report/facility", "University of Plymouth")
        txt, ok = QtWidgets.QInputDialog.getMultiLineText(
            self, "Letterhead", "Shown at the top right of client reports (first line in bold):", cur)
        if ok:
            self.settings.setValue("report/facility", txt)
            self._refresh_preview()

    # ---------------------------------------------------------------- preview
    def _refresh_preview(self):
        doc = QtGui.QTextDocument()
        for b in self.rep.blocks:
            if b.kind == "image" and b.png:
                doc.addResource(QtGui.QTextDocument.ImageResource, QtCore.QUrl(f"img{id(b)}"),
                                QtGui.QImage.fromData(b.png))
        head = ""
        logo_path, lines = letterhead()
        if self.rep.style == "client" or self.act_logo_student.isChecked():
            img = load_logo(logo_path)
            if img is not None:
                doc.addResource(QtGui.QTextDocument.ImageResource, QtCore.QUrl("logo"), img)
                hpx = 15 * MM_PX
                head += f'<img src="logo" height="{hpx:.0f}">'
        if self.rep.style == "client" and lines:
            head = (f'<table width="100%"><tr><td>{head}</td><td align="right" style="color:#1f3b73">'
                    + "<br>".join((f"<b>{html.escape(ln)}</b>" if j == 0 else html.escape(ln))
                                  for j, ln in enumerate(lines[:4])) + "</td></tr></table>")
        h = report_html(self.rep, preview=True)
        note = ('<p style="color:#999; font-size:8pt">Grey text is a template prompt and is left out of the '
                'exported report until you edit it.</p>') if any(b.prompt for b in self.rep.blocks) else ""
        h = h.replace("<body>", f"<body>{head}{'<hr>' if head else ''}{note}", 1)
        doc.setHtml(h)
        sb = self.preview.verticalScrollBar().value()
        self.preview.setDocument(doc)
        self._preview_doc = doc
        self.preview.verticalScrollBar().setValue(sb)

    # ---------------------------------------------------------------- files
    def _confirm_discard(self):
        if not self.dirty:
            return True
        r = QtWidgets.QMessageBox.question(
            self, APP_NAME, "Save the current report first?",
            QtWidgets.QMessageBox.Save | QtWidgets.QMessageBox.Discard | QtWidgets.QMessageBox.Cancel)
        if r == QtWidgets.QMessageBox.Save:
            return self.save_report()
        return r == QtWidgets.QMessageBox.Discard

    def new_report_dialog(self):
        m = QtWidgets.QMenu(self)
        for key, t in TEMPLATES.items():
            m.addAction(t["name"], lambda k=key: self.new_report(k))
        m.exec(QtGui.QCursor.pos())

    def new_report(self, template):
        if not self._confirm_discard():
            return
        self.rep = Report(template)
        self.settings.setValue("report/template", template)
        self.dirty = False
        self._load_report()

    def open_report(self, fn=None):
        if not self._confirm_discard():
            return
        if not fn:
            fn, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "Open report", self.settings.value("export_dir", os.path.expanduser("~")),
                f"{APP_NAME} report (*.wgreport)")
        if not fn:
            return
        try:
            with open(fn, encoding="utf-8") as fh:
                self.rep = Report.from_json(json.load(fh))
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, APP_NAME, f"Couldn't open the report:\n{e}")
            return
        self.rep.path = fn
        self.dirty = False
        self._load_report()

    def save_report(self, as_new=False):
        fn = self.rep.path
        if as_new or not fn:
            stem = self.rep.fields.get("number") or self.rep.fields.get("title") or "report"
            stem = "".join(c if c.isalnum() or c in "._-" else "_" for c in stem)
            fn, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "Save report", os.path.join(self.settings.value("export_dir", os.path.expanduser("~")),
                                                  stem + ".wgreport"), f"{APP_NAME} report (*.wgreport)")
            if not fn:
                return False
            if not fn.lower().endswith(".wgreport"):
                fn += ".wgreport"
        try:
            with open(fn, "w", encoding="utf-8") as fh:
                json.dump(self.rep.to_json(), fh)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, APP_NAME, f"Couldn't save:\n{e}")
            return False
        self.rep.path = fn
        self.dirty = False
        self._update_title()
        self.status.showMessage(f"Saved {fn}", 5000)
        return True

    def _export_name(self, ext, filt):
        f = self.rep.fields
        stem = f.get("number") or f.get("title") or "report"
        if self.rep.style == "student" and f.get("name"):
            stem = f"{stem}_{f['name']}"
        stem = "".join(c if c.isalnum() or c in "._-" else "_" for c in stem)
        fn, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export report", os.path.join(self.settings.value("export_dir", os.path.expanduser("~")),
                                                stem + ext), filt)
        if fn:
            self.settings.setValue("export_dir", os.path.dirname(fn))
            if not fn.lower().endswith(ext):
                fn += ext
        return fn

    def _warn_logo(self):
        if self.rep.style == "client" and load_logo(letterhead()[0]) is None:
            self.status.showMessage("No logo set – choose one with Letterhead ▸ Logo…", 8000)

    def export_pdf(self, fn=None):
        fn = fn or self._export_name(".pdf", "PDF (*.pdf)")
        if not fn:
            return
        try:
            n = export_pdf(self.rep, fn)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, APP_NAME, f"PDF export failed:\n{e}")
            return
        self.status.showMessage(f"Saved {fn} ({n} page{'s' if n != 1 else ''})", 8000)
        self._warn_logo()

    def export_docx(self, fn=None):
        fn = fn or self._export_name(".docx", "Word document (*.docx)")
        if not fn:
            return
        try:
            export_docx(self.rep, fn)
        except ImportError:
            QtWidgets.QMessageBox.critical(self, APP_NAME, "Word export needs the python-docx package "
                                                           "(pip install python-docx).")
            return
        except PermissionError:
            QtWidgets.QMessageBox.critical(self, APP_NAME, "Couldn't write the file – is it open in Word?")
            return
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, APP_NAME, f"Word export failed:\n{e}")
            return
        self.status.showMessage(f"Saved {fn}", 8000)
        self._warn_logo()

    def closeEvent(self, e):
        if self._confirm_discard():
            e.accept()
        else:
            e.ignore()

    def show_help(self):
        QtWidgets.QMessageBox.information(self, "Report builder", """
<b>Choose a template</b> with <i>New</i>: student laboratory report, compound characterisation, or a client
analysis report (report number, client, client point of contact, sample IDs, dates, analyst).<br><br>
<b>Add content</b> from any data window with <i>Report ▸ Add … to report</i>: chromatograms and spectra
(as shown, with your labels), peak tables, and NMR / IR report text. New items go after the selected item.
<i>Insert</i> adds headings, text, tables, page breaks and images (structures, photos, figures from the
figure maker).<br><br>
<b>Grey items</b> are template prompts; they are left out of the export until you edit them.<br><br>
<b>Export</b> as PDF or Word. Figures are numbered automatically; images keep the resolution they were made
at (600 dpi by default).<br><br>
<b>Letterhead</b>: set the logo and the organisation name/address shown on client reports.
They are saved on this computer, not in the report file.<br><br>
<b>Save</b> keeps the report (with its images) as a .wgreport file you can open and keep working on.
""")


# =============================================================================
_windows = {}


def window(target):
    w = _windows.get(target)
    try:
        if w is not None:
            w.isVisible()
    except RuntimeError:
        w = None
    if w is None:
        if target == "report":
            w = ReportWindow()
            t = settings().value("report/template", "student")
            if t in TEMPLATES and t != "student":
                w.rep = Report(t)
                w._load_report()
        else:
            import figure_maker
            w = figure_maker.FigureWindow()
        _windows[target] = w
    return w


def deliver(target, item, parent=None):
    """Show the report builder / figure maker and hand it an item (None = just open it)."""
    w = window(target)
    if not w.isVisible():
        w.show()
    w.raise_()
    w.activateWindow()
    if item is not None:
        w.add_item(item)
    return w
