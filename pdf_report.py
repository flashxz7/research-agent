# -*- coding: utf-8 -*-
"""
Hemut branded PDF generator for deep research reports.

Two-pass strategy:
  Pass 1 – render body pages via ReportLab, track heading → page number.
  Pass 2 – build cover + TOC PDF with accurate page numbers (+2 offset).
  Merge  – fitz combines cover+TOC (2 pages) + body pages into final PDF.
"""
import logging
import re
from datetime import date
from pathlib import Path

import fitz  # PyMuPDF
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

log = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
LOGO = ROOT / "Hemut_Logo Icon Transarent (1).png"
_FONT_DIR = ROOT / "tools" / "fonts" / "inter" / "extras" / "ttf"
_INTER_REGULAR = _FONT_DIR / "Inter-Regular.ttf"
_INTER_BOLD = _FONT_DIR / "Inter-Bold.ttf"
_INTER_ITALIC = _FONT_DIR / "Inter-Italic.ttf"

# ── Design constants ───────────────────────────────────────────────────────────
PAGE_WIDTH, PAGE_HEIGHT = LETTER
MARGIN_X = 1.05 * inch
MARGIN_Y = 1.0 * inch

YELLOW  = HexColor("#F6D44B")
BLACK   = HexColor("#0B0B0B")
CHARCOAL = HexColor("#1E1E1E")
GRAY    = HexColor("#6E6E6E")
WHITE   = HexColor("#FFFFFF")

# Module-level font name holders (populated by _register_fonts)
FONT_REGULAR = "Helvetica"
FONT_BOLD    = "Helvetica-Bold"
FONT_ITALIC  = "Helvetica-Oblique"

_FONTS_REGISTERED = False


# ── Font registration ──────────────────────────────────────────────────────────

def _register_fonts() -> None:
    global FONT_REGULAR, FONT_BOLD, FONT_ITALIC, _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return
    if _INTER_REGULAR.exists() and _INTER_BOLD.exists():
        try:
            pdfmetrics.registerFont(TTFont("Inter", str(_INTER_REGULAR)))
            pdfmetrics.registerFont(TTFont("Inter-Bold", str(_INTER_BOLD)))
            if _INTER_ITALIC.exists():
                pdfmetrics.registerFont(TTFont("Inter-Italic", str(_INTER_ITALIC)))
                FONT_REGULAR, FONT_BOLD, FONT_ITALIC = "Inter", "Inter-Bold", "Inter-Italic"
            else:
                FONT_REGULAR, FONT_BOLD, FONT_ITALIC = "Inter", "Inter-Bold", "Inter"
        except Exception as exc:
            log.warning("Inter font registration failed, using Helvetica: %s", exc)
            FONT_REGULAR, FONT_BOLD, FONT_ITALIC = "Helvetica", "Helvetica-Bold", "Helvetica-Oblique"
    else:
        FONT_REGULAR, FONT_BOLD, FONT_ITALIC = "Helvetica", "Helvetica-Bold", "Helvetica-Oblique"
    _FONTS_REGISTERED = True


# ── Markdown parser ────────────────────────────────────────────────────────────

def _md_to_xml(text: str) -> str:
    """Escape XML special chars, then convert **bold** markers."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    return text


def _parse_markdown(text: str) -> list[dict]:
    """
    Parse full_digest markdown into tokens.
    Token types: "h2", "h3", "bullet", "para"

    Any leading '# Title' line is skipped — the title is rendered separately
    via the `title` argument passed to generate_report_pdf().
    """
    tokens: list[dict] = []
    for line in text.splitlines():
        s = line.rstrip()
        if s.startswith("# "):
            continue  # skip document-level title
        elif s.startswith("## "):
            tokens.append({"type": "h2", "text": s[3:].strip()})
        elif s.startswith("### "):
            tokens.append({"type": "h3", "text": s[4:].strip()})
        elif s.startswith("- ") or s.startswith("* "):
            tokens.append({"type": "bullet", "text": s[2:].strip()})
        elif s:
            tokens.append({"type": "para", "text": s})
        # blank lines: skip (spacing handled via spaceBefore / spaceAfter)
    return tokens


# ── Paragraph styles ───────────────────────────────────────────────────────────

def _build_styles() -> dict[str, ParagraphStyle]:
    reg, bold = FONT_REGULAR, FONT_BOLD
    return {
        "title": ParagraphStyle(
            "ReportTitle",
            fontName=bold,
            fontSize=20,
            leading=26,
            textColor=CHARCOAL,
            spaceAfter=18,
        ),
        "h2": ParagraphStyle(
            "Heading2",
            fontName=bold,
            fontSize=13,
            leading=18,
            textColor=CHARCOAL,
            spaceBefore=14,
            spaceAfter=6,
        ),
        "h3": ParagraphStyle(
            "Heading3",
            fontName=bold,
            fontSize=11,
            leading=16,
            textColor=CHARCOAL,
            spaceBefore=10,
            spaceAfter=4,
        ),
        "para": ParagraphStyle(
            "BodyPara",
            fontName=reg,
            fontSize=10,
            leading=15,
            textColor=CHARCOAL,
            spaceAfter=6,
        ),
        "bullet": ParagraphStyle(
            "Bullet",
            fontName=reg,
            fontSize=10,
            leading=15,
            textColor=CHARCOAL,
            leftIndent=14,
            spaceAfter=3,
        ),
    }


# ── Canvas page callbacks ──────────────────────────────────────────────────────

def _draw_cover(canvas, doc):
    """Cover page: black bg, large centered logo watermark, title text."""
    canvas.saveState()

    # Black background
    canvas.setFillColor(BLACK)
    canvas.rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT, fill=1, stroke=0)

    # Large centered logo watermark (full yellow)
    if LOGO.exists():
        logo_size = PAGE_WIDTH * 0.8
        logo_x = (PAGE_WIDTH - logo_size) / 2
        logo_y = (PAGE_HEIGHT - logo_size) / 2
        try:
            from PIL import Image
            img = Image.open(str(LOGO)).convert("RGBA")
            yellow_img = Image.new("RGBA", img.size, (246, 212, 75, 255))
            yellow_img.putalpha(img.split()[3])
            canvas.drawImage(
                ImageReader(yellow_img),
                logo_x, logo_y,
                width=logo_size, height=logo_size,
                mask="auto",
            )
        except Exception:
            canvas.drawImage(
                ImageReader(str(LOGO)),
                logo_x, logo_y,
                width=logo_size, height=logo_size,
                mask="auto",
            )

    # Small logo + "Hemut" wordmark — top-left
    logo_small = 0.5 * inch
    logo_x_small = MARGIN_X
    logo_y_small = PAGE_HEIGHT - 1.17 * inch
    wordmark_font_size = 18
    wordmark_x = logo_x_small + logo_small + 0.18 * inch
    wordmark_y = logo_y_small + (logo_small - (wordmark_font_size * 0.72)) / 2
    if LOGO.exists():
        canvas.drawImage(
            ImageReader(str(LOGO)),
            logo_x_small, logo_y_small,
            width=logo_small, height=logo_small,
            mask="auto",
        )
    canvas.setFillColor(WHITE)
    canvas.setFont(FONT_BOLD, wordmark_font_size)
    canvas.drawString(wordmark_x, wordmark_y, "Hemut")

    # "Deep Research" (white) + "Report" (yellow) title block
    canvas.setFillColor(WHITE)
    canvas.setFont(FONT_BOLD, 36)
    canvas.drawString(MARGIN_X, PAGE_HEIGHT - 2.3 * inch, "Deep Research")
    canvas.setFillColor(YELLOW)
    canvas.drawString(MARGIN_X, PAGE_HEIGHT - 2.85 * inch, "Report")

    # Generated date
    canvas.setFillColor(WHITE)
    canvas.setFont(FONT_REGULAR, 13)
    canvas.drawString(
        MARGIN_X,
        PAGE_HEIGHT - 3.4 * inch,
        f"Generated {date.today().isoformat()}",
    )

    canvas.restoreState()


def _draw_content_page(canvas, doc):
    """TOC and body pages: white bg, small logo icon top-right only."""
    canvas.saveState()
    canvas.setFillColor(WHITE)
    canvas.rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT, fill=1, stroke=0)
    if LOGO.exists():
        size = 0.28 * inch
        canvas.drawImage(
            ImageReader(str(LOGO)),
            PAGE_WIDTH - MARGIN_X - size,
            PAGE_HEIGHT - MARGIN_Y + 0.08 * inch,
            width=size, height=size,
            mask="auto",
        )
    canvas.restoreState()


# ── ReportDoc: tracks heading page numbers ─────────────────────────────────────

class _ReportDoc(SimpleDocTemplate):
    """SimpleDocTemplate that records page numbers for TOC headings."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.toc_entries: list[tuple[int, str, int]] = []  # (level, text, page)

    def afterFlowable(self, flowable):
        if hasattr(flowable, "_toc_level"):
            self.toc_entries.append((flowable._toc_level, flowable._toc_text, self.page))


# ── Story builder ──────────────────────────────────────────────────────────────

def _build_body_story(report_title: str, tokens: list[dict], styles: dict) -> list:
    story: list = []

    # Report title as the first element on page 1
    story.append(Paragraph(_md_to_xml(report_title), styles["title"]))

    for token in tokens:
        t = token["type"]
        text = _md_to_xml(token["text"])

        if t == "h2":
            p = Paragraph(text, styles["h2"])
            p._toc_level = 2
            p._toc_text = token["text"]
            story.append(p)
        elif t == "h3":
            p = Paragraph(text, styles["h3"])
            p._toc_level = 3
            p._toc_text = token["text"]
            story.append(p)
        elif t == "bullet":
            story.append(Paragraph(f"\u2022\u00a0{text}", styles["bullet"]))
        else:
            story.append(Paragraph(text, styles["para"]))

    return story


# ── Cover + TOC PDF builder ────────────────────────────────────────────────────

def _build_cover_toc_pdf(
    toc_entries: list[tuple[int, str, int]],
    output_path: Path,
) -> None:
    """
    Render a 2-page PDF: page 1 = cover (_draw_cover), page 2 = TOC (_draw_content_page).
    toc_entries: [(level, title, absolute_final_page_number)]
    """
    toc_title_style = ParagraphStyle(
        "TocHeading",
        fontName=FONT_BOLD,
        fontSize=20,
        leading=26,
        textColor=CHARCOAL,
        spaceAfter=14,
    )
    toc_main_style = ParagraphStyle(
        "TocMain",
        fontName=FONT_REGULAR,
        fontSize=11,
        leading=16,
        textColor=CHARCOAL,
        leftIndent=0,
        spaceAfter=4,
    )
    toc_sub_style = ParagraphStyle(
        "TocSub",
        fontName=FONT_REGULAR,
        fontSize=10,
        leading=14,
        textColor=CHARCOAL,
        leftIndent=16,
        spaceAfter=3,
    )
    page_num_main_style = ParagraphStyle(
        "TocPageMain",
        fontName=FONT_BOLD,
        fontSize=11,
        leading=16,
        textColor=CHARCOAL,
        alignment=2,
    )
    page_num_sub_style = ParagraphStyle(
        "TocPageSub",
        fontName=FONT_REGULAR,
        fontSize=10,
        leading=14,
        textColor=CHARCOAL,
        alignment=2,
    )

    rows: list[list] = []
    h2_counter = 0
    for level, title, page in toc_entries:
        safe = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if level == 2:
            h2_counter += 1
            rows.append([
                Paragraph(f"{h2_counter}. {safe}", toc_main_style),
                Paragraph(str(page), page_num_main_style),
            ])
        else:
            rows.append([
                Paragraph(safe, toc_sub_style),
                Paragraph(str(page), page_num_sub_style),
            ])

    if not rows:
        rows.append([
            Paragraph("No sections detected", toc_main_style),
            Paragraph("-", page_num_main_style),
        ])

    col_widths = [PAGE_WIDTH - 2 * MARGIN_X - 0.85 * inch, 0.85 * inch]
    toc_table = Table(rows, colWidths=col_widths, hAlign="LEFT")
    toc_table.setStyle(TableStyle([
        ("VALIGN",       (0, 0), (-1, -1), "TOP"),
        ("ALIGN",        (1, 0), (1, -1), "RIGHT"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING",   (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 2),
    ]))

    # Page 1 = cover (story content is invisible behind the black cover background)
    # Page 2 = TOC
    story = [
        Spacer(1, 1),   # placeholder so page 1 exists
        PageBreak(),
        Spacer(1, 0.65 * inch),
        Paragraph("Table of Contents", toc_title_style),
        toc_table,
    ]

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=LETTER,
        leftMargin=MARGIN_X,
        rightMargin=MARGIN_X,
        topMargin=MARGIN_Y,
        bottomMargin=MARGIN_Y,
        title="Hemut Research Report",
        author="Hemut",
    )
    doc.build(story, onFirstPage=_draw_cover, onLaterPages=_draw_content_page)


# ── Public entry point ─────────────────────────────────────────────────────────

def generate_report_pdf(title: str, full_digest: str, output_path: Path) -> Path:
    """
    Render `full_digest` markdown into a Hemut-branded PDF at `output_path`.

    The PDF structure:
      Page 1  – Black cover with Hemut branding and date
      Page 2  – Table of Contents (auto-generated, accurate page numbers)
      Page 3+ – Body pages (white, small logo top-right, formatted research content)

    Returns `output_path` on success. Raises on failure — caller should wrap
    in try/except so a PDF failure never blocks the research result.
    """
    _register_fonts()

    tokens = _parse_markdown(full_digest)
    styles = _build_styles()
    story = _build_body_story(title, tokens, styles)

    tmp_body = output_path.with_suffix(".body_tmp.pdf")
    tmp_cover_toc = output_path.with_suffix(".covertoc_tmp.pdf")

    try:
        # ── Pass 1: render body pages, collect heading→page mappings ────────
        body_doc = _ReportDoc(
            str(tmp_body),
            pagesize=LETTER,
            leftMargin=MARGIN_X,
            rightMargin=MARGIN_X,
            topMargin=MARGIN_Y,
            bottomMargin=MARGIN_Y,
        )
        body_doc.build(
            story,
            onFirstPage=_draw_content_page,
            onLaterPages=_draw_content_page,
        )
        raw_entries = body_doc.toc_entries  # [(level, text, body_page_1indexed)]

        # ── Pass 2: build cover + TOC with final page numbers ──────────────
        # Body page N → final PDF page N+2 (cover=1, TOC=2)
        toc_entries = [(level, text, page + 2) for level, text, page in raw_entries]
        _build_cover_toc_pdf(toc_entries, tmp_cover_toc)

        # ── Merge: cover+TOC then body pages ─────────────────────────────────
        output_path.parent.mkdir(parents=True, exist_ok=True)
        final = fitz.open()
        with fitz.open(str(tmp_cover_toc)) as cover_doc:
            final.insert_pdf(cover_doc)
        with fitz.open(str(tmp_body)) as body_fitz:
            final.insert_pdf(body_fitz)
        final.save(str(output_path))
        final.close()

        log.info("PDF generated  path=%s  pages=%d", output_path.name,
                 2 + body_doc.page)
        return output_path

    finally:
        tmp_body.unlink(missing_ok=True)
        tmp_cover_toc.unlink(missing_ok=True)
