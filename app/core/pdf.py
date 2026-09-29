"""Server-side PDF rendering with correct Arabic shaping (RTL).

Uses reportlab + arabic-reshaper + python-bidi so Arabic text renders joined and
right-to-left. The Baloo Bhaijaan 2 TTF that ships with the app is registered as
the document font (it covers Arabic + Latin + digits), so PDFs match the on-screen
brand identity. A single reusable ``build_pdf`` turns a list of blocks
(title / meta / heading / table / paragraph / spacer) into PDF bytes, so every
report, receipt, and statement exports the same way.
"""
from __future__ import annotations

import io
import os
from functools import lru_cache

import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle)

_FONT = "Baloo"
_FONT_BOLD = "Baloo-Bold"
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FONT_DIR = os.path.join(_BASE, "static", "fonts")

# brand tokens (mirror app/static/css/brand.css)
NAVY = colors.HexColor("#0C2461")
BLUE = colors.HexColor("#1849A9")
LIGHT = colors.HexColor("#EEF2FB")
GREY = colors.HexColor("#64748B")


@lru_cache(maxsize=1)
def _register_fonts() -> bool:
    """Register the Arabic-capable TTFs once. Returns True on success."""
    try:
        pdfmetrics.registerFont(
            TTFont(_FONT, os.path.join(_FONT_DIR, "BalooBhaijaan2-Regular.ttf")))
        pdfmetrics.registerFont(
            TTFont(_FONT_BOLD, os.path.join(_FONT_DIR, "BalooBhaijaan2-Bold.ttf")))
        return True
    except Exception:  # pragma: no cover - font always ships
        return False


def ar(text) -> str:
    """Shape Arabic text (join letters) and apply the bidi algorithm so it
    renders right-to-left with embedded Latin/numbers in the correct order."""
    s = "" if text is None else str(text)
    if not s:
        return s
    try:
        return get_display(arabic_reshaper.reshape(s))
    except Exception:  # pragma: no cover
        return s


def _styles():
    _register_fonts()
    normal = ParagraphStyle(
        "ar", fontName=_FONT, fontSize=10, leading=16, alignment=2,  # RIGHT
        wordWrap="RTL")
    title = ParagraphStyle(
        "ar-title", parent=normal, fontName=_FONT_BOLD, fontSize=18,
        textColor=NAVY, leading=24, spaceAfter=2)
    heading = ParagraphStyle(
        "ar-h", parent=normal, fontName=_FONT_BOLD, fontSize=12,
        textColor=BLUE, spaceBefore=8, spaceAfter=4)
    meta = ParagraphStyle(
        "ar-meta", parent=normal, fontSize=9, textColor=GREY, leading=14)
    return {"normal": normal, "title": title, "heading": heading, "meta": meta}


def build_pdf(blocks, *, title=None, landscape=False, rtl=True) -> bytes:
    """Render *blocks* to PDF bytes.

    Each block is a dict with a ``type``:
      - ``title``   {text}
      - ``meta``    {pairs: [(label, value), ...]}  small grey key/value rows
      - ``heading`` {text}
      - ``paragraph`` {text}
      - ``spacer``  {height} (mm, default 4)
      - ``table``   {headers: [...], rows: [[...]], aligns?: ['R'|'L'|'C', ...],
                     totals?: [...] (bold last row), widths?: [w_mm, ...]}
    """
    _register_fonts()
    st = _styles()
    buf = io.BytesIO()
    pagesize = A4 if not landscape else (A4[1], A4[0])
    doc = SimpleDocTemplate(
        buf, pagesize=pagesize, rightMargin=16 * mm, leftMargin=16 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm, title=title or "")
    flow = []
    for b in blocks:
        t = b.get("type")
        if t == "title":
            flow.append(Paragraph(ar(b["text"]), st["title"]))
        elif t == "heading":
            flow.append(Paragraph(ar(b["text"]), st["heading"]))
        elif t == "paragraph":
            flow.append(Paragraph(ar(b["text"]), st["normal"]))
        elif t == "spacer":
            flow.append(Spacer(1, b.get("height", 4) * mm))
        elif t == "meta":
            rows = [[Paragraph(ar(f"<b>{lbl}:</b> {val}"), st["meta"])]
                    for lbl, val in b["pairs"]]
            tbl = Table(rows, hAlign="RIGHT")
            tbl.setStyle(TableStyle([
                ("TOPPADDING", (0, 0), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
            flow.append(tbl)
        elif t == "table":
            flow.append(_table(b, st))
    doc.build(flow)
    return buf.getvalue()


def _table(b, st):
    headers = b.get("headers", [])
    rows = b.get("rows", [])
    aligns = b.get("aligns")
    totals = b.get("totals")
    cell = ParagraphStyle("cell", parent=st["normal"], fontSize=9, leading=13)
    head = ParagraphStyle("hcell", parent=cell, fontName=_FONT_BOLD,
                          textColor=colors.white)

    def mk(v, style=cell):
        return Paragraph(ar(v), style)

    data = [[mk(h, head) for h in headers]] if headers else []
    for r in rows:
        data.append([mk(c) for c in r])
    if totals:
        bold = ParagraphStyle("tot", parent=cell, fontName=_FONT_BOLD)
        data.append([mk(c, bold) for c in totals])

    widths = None
    if b.get("widths"):
        widths = [w * mm for w in b["widths"]]
    tbl = Table(data, colWidths=widths, hAlign="RIGHT", repeatRows=1 if headers else 0)
    style = [
        ("FONTNAME", (0, 0), (-1, -1), _FONT),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D8DEE9")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]
    if headers:
        style += [("BACKGROUND", (0, 0), (-1, 0), NAVY),
                  ("TEXTCOLOR", (0, 0), (-1, 0), colors.white)]
        style += [("ROWBACKGROUNDS", (0, 1), (-1, -1),
                   [colors.white, LIGHT])]
    if totals:
        style += [("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#DCE6FA")),
                  ("LINEABOVE", (0, -1), (-1, -1), 0.8, NAVY)]
    # per-column alignment (default: first col right, numeric cols centered)
    if aligns:
        amap = {"R": "RIGHT", "L": "LEFT", "C": "CENTER"}
        for i, a in enumerate(aligns):
            style.append(("ALIGN", (i, 0), (i, -1), amap.get(a, "RIGHT")))
    tbl.setStyle(TableStyle(style))
    return tbl
