"""
Table export shared by every module.

    export_table(parent, settings, header, rows, meta, default_stem)
        -> asks for a file name; .xlsx (Excel), .csv or .txt chosen by the extension/filter
    copy_table(header, rows, title)
        -> puts the table on the clipboard as both an HTML table (pastes into Word /
           PowerPoint / Google Docs as a real table) and tab-separated text (pastes into Excel)
"""
from __future__ import annotations

import csv
import html
import os

from PySide6 import QtCore, QtWidgets

FILTERS = "Excel workbook (*.xlsx);;CSV (*.csv);;Text, tab separated (*.txt)"


def _num(v):
    """Turn numeric strings back into numbers so Excel sorts / sums them."""
    if isinstance(v, (int, float)):
        return v
    s = str(v).strip()
    try:
        if s and s.lstrip("-").replace(".", "", 1).isdigit():
            return int(s) if s.lstrip("-").isdigit() else float(s)
    except ValueError:
        pass
    return v


def write_xlsx(fn, header, rows, meta=(), sheet="Table"):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    ws = wb.active
    ws.title = sheet[:31]
    r = 1
    for k, v in meta:
        ws.cell(r, 1, k).font = Font(italic=True, color="666666")
        ws.cell(r, 2, str(v))
        r += 1
    if meta:
        r += 1
    for c, h in enumerate(header, 1):
        ws.cell(r, c, h).font = Font(bold=True)
    ws.freeze_panes = ws.cell(r + 1, 1)
    for row in rows:
        r += 1
        for c, v in enumerate(row, 1):
            ws.cell(r, c, _num(v))
    for c, h in enumerate(header, 1):
        width = max([len(str(h))] + [len(str(row[c - 1])) for row in rows if c - 1 < len(row)])
        ws.column_dimensions[get_column_letter(c)].width = min(60, max(8, width + 2))
    wb.save(fn)


def write_csv(fn, header, rows, meta=(), delimiter=","):
    with open(fn, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=delimiter)
        for k, v in meta:
            w.writerow([f"# {k}", v])
        w.writerow(header)
        w.writerows(rows)


def export_table(parent, settings: QtCore.QSettings, header, rows, meta=(), default_stem="table",
                 start_dir=None, sheet="Table"):
    """Ask for a file and save. Returns the file name or None."""
    if not rows:
        QtWidgets.QMessageBox.information(parent, "Export table", "The table is empty.")
        return None
    last = settings.value("table_ext", ".xlsx")
    folder = settings.value("export_dir", start_dir or os.path.expanduser("~"))
    filt = {".xlsx": "Excel workbook (*.xlsx)", ".csv": "CSV (*.csv)", ".txt": "Text, tab separated (*.txt)"}
    fn, chosen = QtWidgets.QFileDialog.getSaveFileName(parent, "Export table",
                                                       os.path.join(folder, default_stem + last),
                                                       FILTERS, filt.get(last, ""))
    if not fn:
        return None
    ext = os.path.splitext(fn)[1].lower()
    if ext not in (".xlsx", ".csv", ".txt"):
        ext = ".xlsx" if "xlsx" in chosen else ".txt" if "txt" in chosen else ".csv"
        fn += ext
    settings.setValue("export_dir", os.path.dirname(fn))
    settings.setValue("table_ext", ext)
    try:
        if ext == ".xlsx":
            write_xlsx(fn, header, rows, meta, sheet)
        else:
            write_csv(fn, header, rows, meta, "\t" if ext == ".txt" else ",")
    except PermissionError:
        QtWidgets.QMessageBox.critical(parent, "Export table",
                                       f"Couldn't write {os.path.basename(fn)} - is it open in Excel?")
        return None
    except Exception as e:
        QtWidgets.QMessageBox.critical(parent, "Export table", f"Export failed:\n{e}")
        return None
    return fn


def table_html(header, rows, title=None):
    esc = lambda s: html.escape(str(s))
    out = ['<meta charset="utf-8">']
    if title:
        out.append(f"<p><b>{esc(title)}</b></p>")
    out.append('<table border="1" cellspacing="0" cellpadding="3" style="border-collapse:collapse;'
               'font-family:Calibri,Arial,sans-serif;font-size:10pt">')
    out.append("<tr>" + "".join(f'<th style="background:#e8ecf4">{esc(h)}</th>' for h in header) + "</tr>")
    for r in rows:
        out.append("<tr>" + "".join(f"<td>{esc(v)}</td>" for v in r) + "</tr>")
    out.append("</table>")
    return "".join(out)


# When set (by "Add to report"), copy_table / copy_text hand their content to this callable
# instead of the clipboard: CAPTURE(kind="table", header=, rows=, title=) or CAPTURE(kind="text", ...).
CAPTURE = None


def copy_table(header, rows, title=None, extra_text=None):
    """Copy as a real table (Word/PowerPoint) and as tab-separated text (Excel)."""
    if CAPTURE is not None:
        CAPTURE(kind="table", header=[str(h) for h in header], rows=[[str(v) for v in r] for r in rows],
                title=title or "", note=extra_text or "")
        return
    md = QtCore.QMimeData()
    tsv = "\n".join("\t".join(map(str, r)) for r in [header] + list(rows))
    h = table_html(header, rows, title)
    if extra_text:
        tsv += "\n\n" + extra_text
        h += f"<p>{html.escape(extra_text)}</p>"
    md.setText(tsv)
    md.setHtml(h)
    QtWidgets.QApplication.clipboard().setMimeData(md)


def copy_text(text, rich=None):
    if CAPTURE is not None:
        CAPTURE(kind="text", text=text, html=rich or "")
        return
    md = QtCore.QMimeData()
    md.setText(text)
    if rich:
        md.setHtml(rich)
    QtWidgets.QApplication.clipboard().setMimeData(md)


def export_buttons(parent, on_export, on_copy, export_text="Export table…", copy_text_="Copy table"):
    """Two buttons used under every table."""
    w = QtWidgets.QWidget(parent)
    lay = QtWidgets.QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    b1 = QtWidgets.QPushButton(export_text)
    b1.setToolTip("Save as Excel (.xlsx), CSV or tab-separated text")
    b1.clicked.connect(on_export)
    b2 = QtWidgets.QPushButton(copy_text_)
    b2.setToolTip("Copies a real table: paste into Word/PowerPoint for a formatted table, or into Excel")
    b2.clicked.connect(on_copy)
    lay.addWidget(b1)
    lay.addWidget(b2)
    lay.addStretch(1)
    return w
