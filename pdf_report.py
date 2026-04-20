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
    Flowable,
    HRFlowable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)


class _Bookmark(Flowable):
    """Zero-size flowable that drops a named PDF destination at its position."""
    def __init__(self, name: str):
        super().__init__()
        self._name = name
        self.width = 0
        self.height = 0

    def draw(self):
        self.canv.bookmarkPage(self._name)

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

YELLOW   = HexColor("#F6D44B")
BLACK    = HexColor("#0B0B0B")
CHARCOAL = HexColor("#1A1A1A")
GRAY     = HexColor("#6E6E6E")
LIGHT_GRAY = HexColor("#E8E8E8")
WHITE    = HexColor("#FFFFFF")
LINK_BLUE = HexColor("#2563EB")

# Module-level font name holders (populated by _register_fonts)
FONT_REGULAR = "Helvetica"
FONT_BOLD    = "Helvetica-Bold"
FONT_ITALIC  = "Helvetica-Oblique"

_FONTS_REGISTERED = False

# Cached yellow-tinted logo (loaded once, reused on every page)
_YELLOW_LOGO: "ImageReader | None" = None
_YELLOW_LOGO_LOADED = False


def _get_yellow_logo() -> "ImageReader | None":
    """Return a yellow-tinted ImageReader for the Hemut logo, cached after first load."""
    global _YELLOW_LOGO, _YELLOW_LOGO_LOADED
    if _YELLOW_LOGO_LOADED:
        return _YELLOW_LOGO
    _YELLOW_LOGO_LOADED = True
    if not LOGO.exists():
        log.warning("Logo file not found: %s", LOGO)
        return None
    try:
        from PIL import Image
        img = Image.open(str(LOGO))
        # Normalise to RGBA so we always have an alpha channel
        if img.mode == "RGBA":
            pass
        elif img.mode in ("RGB", "L", "P"):
            img = img.convert("RGBA")
        else:
            img = img.convert("RGBA")
        r_ch, g_ch, b_ch, alpha = img.split()
        # Replace colour channels with Hemut yellow (#F6D44B = 246, 212, 75)
        yr = Image.new("L", img.size, 246)
        yg = Image.new("L", img.size, 212)
        yb = Image.new("L", img.size, 75)
        yellow = Image.merge("RGBA", (yr, yg, yb, alpha))
        _YELLOW_LOGO = ImageReader(yellow)
        log.info("Logo tinted yellow successfully")
        return _YELLOW_LOGO
    except Exception as exc:
        log.warning("Logo yellow-tint failed: %s", exc)
        return None


# ── Font registration ──────────────────────────────────────────────────────────

def _register_fonts() -> None:
    global FONT_REGULAR, FONT_BOLD, FONT_ITALIC, _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return
    if _INTER_REGULAR.exists() and _INTER_BOLD.exists():
        try:
            pdfmetrics.registerFont(TTFont("Inter", str(_INTER_REGULAR)))
            pdfmetrics.registerFont(TTFont("Inter-Bold", str(_INTER_BOLD)))
            italic_name = "Inter"
            if _INTER_ITALIC.exists():
                pdfmetrics.registerFont(TTFont("Inter-Italic", str(_INTER_ITALIC)))
                italic_name = "Inter-Italic"
            pdfmetrics.registerFontFamily(
                "Inter",
                normal="Inter",
                bold="Inter-Bold",
                italic=italic_name,
                boldItalic="Inter-Bold",
            )
            FONT_REGULAR, FONT_BOLD, FONT_ITALIC = "Inter", "Inter-Bold", italic_name
            log.info("Inter font loaded successfully (italic=%s)", italic_name)
        except Exception as exc:
            log.warning("Inter font registration failed, falling back to Helvetica: %s", exc)
            FONT_REGULAR, FONT_BOLD, FONT_ITALIC = "Helvetica", "Helvetica-Bold", "Helvetica-Oblique"
    else:
        log.warning("Inter font files not found at %s — using Helvetica", _FONT_DIR)
        FONT_REGULAR, FONT_BOLD, FONT_ITALIC = "Helvetica", "Helvetica-Bold", "Helvetica-Oblique"
    _FONTS_REGISTERED = True


# ── Markdown parser ────────────────────────────────────────────────────────────

def _md_to_xml(text: str, style_citations: bool = False) -> str:
    """
    Escape XML special chars, convert **bold** markers, and optionally
    style [N] inline citation markers as small yellow superscript-style text.
    """
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    if style_citations:
        # De-duplicate consecutive identical citations like [4][4][4] → [4]
        text = re.sub(r'(\[\d+\])(\s*\1)+', r'\1', text)
        # Style each [N] as small yellow bold marker
        text = re.sub(
            r'\[(\d+)\]',
            lambda m: (
                f'<font color="#F6D44B" size="7.5"><b>[{m.group(1)}]</b></font>'
            ),
            text,
        )
    return text


def _preprocess_digest(text: str) -> str:
    """
    1. Strip Perplexity <think>…</think> blocks and any echoed prompt before them.
    2. Strip the Verification Notes block (and the --- divider before it).
    3. Strip any inline [UNVERIFIED] / [PARTIAL] / [SOURCE_UNREACHABLE] verification tags.
    4. Move the Sources section to the very end of the document.
    """
    # Strip echoed prompt + think block (everything up to and including </think>)
    if "<think>" in text.lower():
        text = re.sub(r"^[\s\S]*?</think>\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)

    # Remove "---\n\n## Verification Notes ..." footer block
    text = re.sub(r'\n?---\s*\n+#{1,3}\s+Verification Notes[\s\S]*$', '', text)
    text = re.sub(r'\n#{1,3}\s+Verification Notes[\s\S]*$', '', text)

    # Strip verification status tags that clutter the PDF
    text = re.sub(r'\s*\[(UNVERIFIED|PARTIAL|SOURCE_UNREACHABLE)\]', '', text)

    # Extract Sources section and move to the very end
    src_pat = re.compile(r'\n(#{2,3}\s+Sources\b[^\n]*\n)([\s\S]*?)(?=\n#{2,3}\s+|\Z)')
    m = src_pat.search(text)
    if m:
        sources_heading = m.group(1)
        sources_body = m.group(2)
        text = text[:m.start()] + text[m.end():]
        text = text.rstrip() + '\n\n' + sources_heading + sources_body.rstrip()

    return text.strip()


def _parse_markdown(text: str) -> list[dict]:
    """
    Parse full_digest markdown into tokens.
    Token types: "h2", "h3", "bullet", "rule", "para"

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
        elif s == "---":
            tokens.append({"type": "rule"})
        elif s:
            tokens.append({"type": "para", "text": s})
        # blank lines: skip (spacing handled via spaceBefore / spaceAfter)
    return tokens


# ── Paragraph styles ───────────────────────────────────────────────────────────

def _build_styles() -> dict[str, ParagraphStyle]:
    reg, bold, italic = FONT_REGULAR, FONT_BOLD, FONT_ITALIC
    return {
        "title": ParagraphStyle(
            "ReportTitle",
            fontName=bold,
            fontSize=22,
            leading=29,
            textColor=CHARCOAL,
            spaceAfter=22,
        ),
        "h2": ParagraphStyle(
            "Heading2",
            fontName=bold,
            fontSize=14,
            leading=19,
            textColor=CHARCOAL,
            spaceBefore=20,
            spaceAfter=7,
            borderPad=0,
        ),
        "h3": ParagraphStyle(
            "Heading3",
            fontName=bold,
            fontSize=11.5,
            leading=16,
            textColor=CHARCOAL,
            spaceBefore=13,
            spaceAfter=4,
        ),
        "para": ParagraphStyle(
            "BodyPara",
            fontName=reg,
            fontSize=10.5,
            leading=17,
            textColor=CHARCOAL,
            spaceAfter=8,
        ),
        "bullet": ParagraphStyle(
            "Bullet",
            fontName=reg,
            fontSize=10.5,
            leading=16,
            textColor=CHARCOAL,
            leftIndent=18,
            firstLineIndent=0,
            spaceAfter=5,
        ),
        "source_heading": ParagraphStyle(
            "SourceHeading",
            fontName=bold,
            fontSize=14,
            leading=19,
            textColor=CHARCOAL,
            spaceBefore=20,
            spaceAfter=10,
        ),
        "source": ParagraphStyle(
            "Source",
            fontName=reg,
            fontSize=9.5,
            leading=15,
            textColor=CHARCOAL,
            leftIndent=26,
            firstLineIndent=-26,
            spaceAfter=6,
            wordWrap="LTR",
        ),
    }


# ── Canvas page callbacks ──────────────────────────────────────────────────────

def _draw_cover(canvas, doc, title: str = ""):
    """Cover page: black bg, large yellow logo watermark (lower), title text overlaid top-left."""
    canvas.saveState()

    # ── Black background ──────────────────────────────────────────────────────
    canvas.setFillColor(BLACK)
    canvas.rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT, fill=1, stroke=0)

    # ── Large yellow logo watermark — lower-centre, large, text overlaid on top ─
    yellow_logo = _get_yellow_logo()
    logo_size = PAGE_HEIGHT * 0.80          # tall enough to dominate lower half
    logo_x = (PAGE_WIDTH - logo_size) / 2  # horizontally centred
    logo_y = -PAGE_HEIGHT * 0.04           # slight bleed off the bottom edge
    if yellow_logo is not None:
        canvas.drawImage(
            yellow_logo,
            logo_x, logo_y,
            width=logo_size, height=logo_size,
            mask="auto",
        )
    elif LOGO.exists():
        canvas.drawImage(
            ImageReader(str(LOGO)),
            logo_x, logo_y,
            width=logo_size, height=logo_size,
            mask="auto",
        )

    # ── Small logo icon + "Hemut" wordmark — top-left ─────────────────────────
    icon_size = 0.46 * inch
    icon_x = MARGIN_X
    icon_y = PAGE_HEIGHT - 1.1 * inch
    wm_font_sz = 16
    wm_x = icon_x + icon_size + 0.14 * inch
    wm_y = icon_y + (icon_size - wm_font_sz * 0.72) / 2
    if yellow_logo is not None:
        canvas.drawImage(yellow_logo, icon_x, icon_y,
                         width=icon_size, height=icon_size, mask="auto")
    elif LOGO.exists():
        canvas.drawImage(ImageReader(str(LOGO)), icon_x, icon_y,
                         width=icon_size, height=icon_size, mask="auto")
    canvas.setFillColor(WHITE)
    canvas.setFont(FONT_BOLD, wm_font_sz)
    canvas.drawString(wm_x, wm_y, "Hemut")

    # ── "Deep Research" (white) + "Report" (yellow) headline ─────────────────
    canvas.setFillColor(WHITE)
    canvas.setFont(FONT_BOLD, 42)
    canvas.drawString(MARGIN_X, PAGE_HEIGHT - 2.30 * inch, "Deep Research")
    canvas.setFillColor(YELLOW)
    canvas.setFont(FONT_BOLD, 42)
    canvas.drawString(MARGIN_X, PAGE_HEIGHT - 2.98 * inch, "Report")

    # ── Generated date ─────────────────────────────────────────────────────────
    canvas.setFillColor(HexColor("#AAAAAA"))
    canvas.setFont(FONT_REGULAR, 11.5)
    canvas.drawString(MARGIN_X, PAGE_HEIGHT - 3.62 * inch,
                      f"Generated {date.today().isoformat()}")

    canvas.restoreState()


def _draw_content_page(canvas, doc):
    """TOC and body pages: white bg, small yellow logo icon top-right, page number bottom-center."""
    canvas.saveState()
    canvas.setFillColor(WHITE)
    canvas.rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT, fill=1, stroke=0)

    # Small yellow logo — top-right
    yellow_logo = _get_yellow_logo()
    size = 0.28 * inch
    lx = PAGE_WIDTH - MARGIN_X - size
    ly = PAGE_HEIGHT - MARGIN_Y + 0.08 * inch
    if yellow_logo is not None:
        canvas.drawImage(yellow_logo, lx, ly, width=size, height=size, mask="auto")
    elif LOGO.exists():
        canvas.drawImage(ImageReader(str(LOGO)), lx, ly, width=size, height=size, mask="auto")

    # Page number — bottom center
    page_num = canvas.getPageNumber()
    canvas.setFillColor(GRAY)
    canvas.setFont(FONT_REGULAR, 9)
    canvas.drawCentredString(PAGE_WIDTH / 2, 0.52 * inch, str(page_num))

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
    story.append(Spacer(1, 0.18 * inch))
    story.append(Paragraph(_md_to_xml(report_title), styles["title"]))

    # Yellow separator below title
    story.append(HRFlowable(
        width="100%", thickness=1.5, color=YELLOW,
        spaceBefore=4, spaceAfter=18,
    ))

    in_sources = False
    sources_started = False

    for token in tokens:
        t = token["type"]

        if t == "rule":
            story.append(HRFlowable(
                width="100%", thickness=0.5, color=LIGHT_GRAY,
                spaceBefore=6, spaceAfter=6,
            ))
            continue

        text = token.get("text", "")

        if t == "h2":
            # Detect Sources section — starts a new page for clean separation
            is_sources = bool(re.match(
                r'^(sources|\d+\.?\s+sources)\s*$', text.strip().lower()
            ))
            if is_sources and not sources_started:
                sources_started = True
                in_sources = True
                # Sources gets its own page with a named destination for internal links
                story.append(PageBreak())
                story.append(_Bookmark("sources_section"))
                p = Paragraph(_md_to_xml(text), styles["source_heading"])
                p._toc_level = 2
                p._toc_text = text
                story.append(p)
                story.append(HRFlowable(
                    width="100%", thickness=1.0, color=YELLOW,
                    spaceBefore=2, spaceAfter=12,
                ))
            else:
                in_sources = False
                xml_text = _md_to_xml(text)
                p = Paragraph(xml_text, styles["h2"])
                p._toc_level = 2
                p._toc_text = text
                story.append(p)

        elif t == "h3":
            xml_text = _md_to_xml(text)
            p = Paragraph(xml_text, styles["h3"])
            p._toc_level = 3
            p._toc_text = text
            story.append(p)

        elif t == "bullet":
            xml_text = _md_to_xml(text, style_citations=True)
            story.append(Paragraph(f"\u2022\u00a0{xml_text}", styles["bullet"]))

        else:  # para
            if in_sources:
                src_xml = _render_source_line(text)
                if src_xml is not None:
                    story.append(Paragraph(src_xml, styles["source"]))
                    continue

            xml_text = _md_to_xml(text, style_citations=True)
            story.append(Paragraph(xml_text, styles["para"]))

    return story


def _render_source_line(text: str) -> str | None:
    """
    Parse a source line and return ReportLab XML or None if it doesn't look like a source.

    Handles formats:
      [N] https://url
      [N] https://url Title text
      [N] Title text https://url
      [N] Title text - https://url
      [N] Title text (https://url)
    """
    # Must start with [N]
    m_num = re.match(r'^\[(\d+)\]\s*(.*)', text.strip())
    if not m_num:
        return None

    n = m_num.group(1)
    rest = m_num.group(2).strip()

    # Extract URL from anywhere in rest
    url_m = re.search(r'(https?://\S+)', rest)
    if url_m:
        raw_url = url_m.group(1).rstrip(')')  # strip trailing ) from (url)
        # Title is everything before the URL, cleaned up
        title = rest[:url_m.start()].strip().rstrip('-–—').strip()
        # Also check for title after URL
        after = rest[url_m.end():].strip().lstrip(')').strip()
        if not title and after:
            title = after

        safe_url = raw_url.replace("&", "&amp;")
        # Display: show title if available, otherwise truncated URL
        if title:
            title_safe = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            display = f"{title_safe} — <link href=\"{safe_url}\" color=\"#2563EB\">{safe_url[:70] + '...' if len(raw_url) > 70 else safe_url}</link>"
        else:
            disp_url = raw_url if len(raw_url) <= 80 else raw_url[:77] + "..."
            disp_safe = disp_url.replace("&", "&amp;")
            display = f'<link href="{safe_url}" color="#2563EB">{disp_safe}</link>'

        return (
            f'<font color="#F6D44B"><b>[{n}]</b></font>\u00a0{display}'
        )

    # No URL found — render as plain text with citation marker
    if rest:
        rest_safe = rest.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f'<font color="#F6D44B"><b>[{n}]</b></font>\u00a0{rest_safe}'

    return None


# ── TOC entry flowable with dot leaders ───────────────────────────────────────

class _TocEntry(Flowable):
    """
    A single Table-of-Contents row rendered at canvas level so we can draw
    precise dot leaders between the title and the page number.

    Layout:  [num]  title text ·····················  page
    """
    _DOT_R   = 0.85   # dot radius in points
    _DOT_GAP = 4.8    # centre-to-centre spacing between dots

    def __init__(self, num: str, title: str, page: str, is_sub: bool = False):
        super().__init__()
        self._num   = num    # e.g. "3." or "" for sub-entries
        self._title = title
        self._page  = page
        self._is_sub = is_sub
        # Heights include bottom breathing room
        self.width  = 0
        self.height = 20 if is_sub else 28

    def wrap(self, availWidth, availHeight):
        self.width = availWidth
        return (self.width, self.height)

    def draw(self):
        c = self.canv
        sub  = self._is_sub
        fsz  = 9.5 if sub else 11.0
        indent = 20.0 if sub else 0.0

        num_font   = FONT_REGULAR if sub else FONT_BOLD
        title_font = FONT_REGULAR if sub else FONT_BOLD
        num_col    = GRAY         if sub else YELLOW
        text_col   = GRAY         if sub else CHARCOAL

        # Vertical baseline — centre text within row height
        y = (self.height - fsz) / 2

        # ── Number prefix ────────────────────────────────────────────────────
        x = indent
        if self._num:
            c.setFillColor(num_col)
            c.setFont(num_font, fsz)
            num_str = self._num + "\u2002"   # en-space after number
            num_w   = c.stringWidth(num_str, num_font, fsz)
            c.drawString(x, y, num_str)
            x += num_w

        # ── Page number (right-aligned, drawn first to know x position) ─────
        c.setFillColor(text_col)
        c.setFont(FONT_REGULAR, fsz)
        page_w  = c.stringWidth(self._page, FONT_REGULAR, fsz)
        page_x  = self.width - page_w
        c.drawString(page_x, y, self._page)

        # ── Title (truncate if it would collide with dots + page number) ────
        max_title_w = page_x - x - 22   # 22pt reserved for dots
        title = self._title
        c.setFont(title_font, fsz)
        title_w = c.stringWidth(title, title_font, fsz)
        if title_w > max_title_w:
            while len(title) > 3 and c.stringWidth(title + "…", title_font, fsz) > max_title_w:
                title = title[:-1]
            title = title.rstrip() + "…"
            title_w = c.stringWidth(title, title_font, fsz)
        c.setFillColor(text_col)
        c.drawString(x, y, title)

        # ── Dot leaders ──────────────────────────────────────────────────────
        dot_x0 = x + title_w + 5
        dot_x1 = page_x - 5
        dot_y  = y + fsz * 0.33          # sit slightly above baseline
        c.setFillColor(HexColor("#C0C0C0"))
        cx = dot_x0 + self._DOT_R
        while cx + self._DOT_R < dot_x1:
            c.circle(cx, dot_y, self._DOT_R, fill=1, stroke=0)
            cx += self._DOT_GAP


# ── Cover + TOC PDF builder ────────────────────────────────────────────────────

def _build_cover_toc_pdf(
    toc_entries: list[tuple[int, str, int]],
    output_path: Path,
    title: str = "",
) -> None:
    """
    Render a 2-page PDF: page 1 = cover, page 2 = TOC.
    toc_entries: [(level, title, absolute_final_page_number)]
    Uses _TocEntry flowables with dot leaders for a clean, professional TOC.
    """
    toc_title_style = ParagraphStyle(
        "TocHeading",
        fontName=FONT_BOLD,
        fontSize=22,
        leading=28,
        textColor=CHARCOAL,
        spaceAfter=0,
    )

    story: list = [
        Spacer(1, 1),   # placeholder keeps page 1 alive for cover callback
        PageBreak(),
        Spacer(1, 0.70 * inch),
        Paragraph("Table of Contents", toc_title_style),
        HRFlowable(width="100%", thickness=2.0, color=YELLOW,
                   spaceBefore=6, spaceAfter=18),
    ]

    h2_counter = 0
    prev_was_main = False

    for level, entry_title, page in toc_entries:
        if level == 2:
            # Extra breathing room before each main entry (except the first)
            if prev_was_main:
                story.append(Spacer(1, 3))
            h2_counter += 1
            story.append(_TocEntry(
                num=f"{h2_counter}.",
                title=entry_title,
                page=str(page),
                is_sub=False,
            ))
            prev_was_main = True
        elif level == 3:
            story.append(_TocEntry(
                num="",
                title=entry_title,
                page=str(page),
                is_sub=True,
            ))
            prev_was_main = False

    if h2_counter == 0:
        fallback_style = ParagraphStyle(
            "TocFallback", fontName=FONT_REGULAR, fontSize=11,
            textColor=GRAY, leading=16,
        )
        story.append(Paragraph("No sections detected.", fallback_style))

    def _cover_cb(canvas, doc):
        _draw_cover(canvas, doc, title=title)

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=LETTER,
        leftMargin=MARGIN_X,
        rightMargin=MARGIN_X,
        topMargin=MARGIN_Y,
        bottomMargin=MARGIN_Y,
        title="Hemut Deep Research Report",
        author="Hemut",
    )
    doc.build(story, onFirstPage=_cover_cb, onLaterPages=_draw_content_page)


# ── Post-merge citation link injector ─────────────────────────────────────────

def _inject_citation_links(doc: fitz.Document) -> None:
    """
    After merging cover+TOC+body, find the Sources page and inject clickable
    link annotations on every [N] marker in the body so they jump to Sources.
    """
    # Find the Sources page — look for a page whose first meaningful text is "Sources"
    sources_page_idx: int | None = None
    for i in range(doc.page_count - 1, -1, -1):
        text = doc[i].get_text().strip()
        if text.startswith("Sources") or "\nSources\n" in text or text.split("\n")[:3].count("Sources"):
            sources_page_idx = i
            break

    if sources_page_idx is None:
        log.warning("Could not find Sources page for citation link injection")
        return

    log.info("Sources page found at index %d (page %d)", sources_page_idx, sources_page_idx + 1)

    # Build the link target: top of the Sources page
    sources_page = doc[sources_page_idx]
    target = {"kind": fitz.LINK_GOTO, "page": sources_page_idx, "to": fitz.Point(0, 0), "zoom": 0}

    # Pattern: [N] where N is one or more digits
    citation_pat = re.compile(r'\[\d+\]')

    injected = 0
    for page_idx in range(doc.page_count):
        if page_idx == sources_page_idx:
            continue  # skip the sources page itself
        page = doc[page_idx]
        # Search for all [N] occurrences as text
        instances = page.search_for("[", quads=False)  # start with [ then refine
        # Use get_text with dict to find exact positions of [N] spans
        blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]
        for block in blocks:
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    span_text = span["text"]
                    for m in citation_pat.finditer(span_text):
                        # Calculate bounding rect for this match within the span
                        char_list = span.get("chars")
                        if char_list:
                            # Use char-level bboxes for precision
                            start, end = m.start(), m.end() - 1
                            if start < len(char_list) and end < len(char_list):
                                x0 = char_list[start]["origin"][0]
                                y0 = span["bbox"][1]
                                x1 = char_list[end]["bbox"][2]
                                y1 = span["bbox"][3]
                                rect = fitz.Rect(x0, y0, x1, y1)
                                page.insert_link({**target, "from": rect})
                                injected += 1
                        else:
                            # Fall back to full span bbox (less precise but works)
                            rect = fitz.Rect(span["bbox"])
                            page.insert_link({**target, "from": rect})
                            injected += 1

    log.info("Injected %d citation links → Sources page %d", injected, sources_page_idx + 1)


# ── Public entry point ─────────────────────────────────────────────────────────

def generate_report_pdf(title: str, full_digest: str, output_path: Path) -> Path:
    """
    Render `full_digest` markdown into a Hemut-branded PDF at `output_path`.

    The PDF structure:
      Page 1  – Black cover with Hemut branding, date, and research title
      Page 2  – Table of Contents (h2 sections only, accurate page numbers)
      Page 3+ – Body pages (white, small logo + yellow accent top, page numbers)

    Returns `output_path` on success. Raises on failure — caller should wrap
    in try/except so a PDF failure never blocks the research result.
    """
    _register_fonts()

    cleaned = _preprocess_digest(full_digest)
    tokens = _parse_markdown(cleaned)
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
            topMargin=MARGIN_Y + 0.1 * inch,
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
        _build_cover_toc_pdf(toc_entries, tmp_cover_toc, title=title)

        # ── Merge: cover+TOC then body pages ─────────────────────────────────
        output_path.parent.mkdir(parents=True, exist_ok=True)
        final = fitz.open()
        with fitz.open(str(tmp_cover_toc)) as cover_doc:
            final.insert_pdf(cover_doc)
        with fitz.open(str(tmp_body)) as body_fitz:
            final.insert_pdf(body_fitz)

        # ── Post-merge: inject [N] → Sources page links via PyMuPDF ──────────
        _inject_citation_links(final)

        final.save(str(output_path))
        final.close()

        log.info("PDF generated  path=%s  pages=%d", output_path.name,
                 2 + body_doc.page)
        return output_path

    finally:
        tmp_body.unlink(missing_ok=True)
        tmp_cover_toc.unlink(missing_ok=True)
