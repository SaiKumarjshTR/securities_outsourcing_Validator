"""
level1_content/pdf_extractor.py
────────────────────────────────
Extract structured content from a PDF using PyMuPDF (fitz).

Produces:
  - Cleaned paragraph list  (headers/footers/page-numbers stripped)
  - Section headings list   (bold or larger-font lines)
  - Table count
  - Footnote count
  - Raw word count

Design notes from benchmark (90 vendor pairs):
  • TSX/TMX PDFs from the TMX website contain navigation chrome
    ("Home Trading ...", stock tickers, copyright footer).  The vendor
    keyers intentionally exclude this — we detect it via the
    "repeated-on-every-page" heuristic.
  • Some amending instruments have short PDFs; their SGML is longer
    because the full consolidated text is included.  We do NOT penalise
    this — our L1 check is one-directional (SGML should not LOSE content).
"""

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

try:
    import fitz  # PyMuPDF
    _FITZ_AVAILABLE = True
except ImportError:
    _FITZ_AVAILABLE = False


@dataclass
class PDFContent:
    paragraphs: list[str] = field(default_factory=list)
    """Cleaned paragraphs, headers/footers removed."""

    headings: list[str] = field(default_factory=list)
    """Section headings detected by font size/bold heuristic."""

    table_count: int = 0
    footnote_count: int = 0
    raw_word_count: int = 0
    clean_word_count: int = 0
    page_count: int = 0
    extraction_ok: bool = True
    error: Optional[str] = None

    # Metadata
    detected_doc_type: str = "unknown"
    """'web_scraped', 'scanned', 'native_pdf', 'unknown'"""


def _normalize(text: str) -> str:
    """NFC + lowercase + collapse whitespace."""
    text = unicodedata.normalize("NFC", text)
    text = text.lower()
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _is_page_number(text: str) -> bool:
    stripped = text.strip()
    return bool(re.fullmatch(r"\d{1,4}", stripped))


def _is_likely_web_chrome(lines: list[str]) -> bool:
    """
    Detect if this PDF is a web-scraped HTML page (e.g., TMX website).
    Signal: the first meaningful lines contain website navigation tokens.
    """
    combined = " ".join(lines[:10]).lower()
    web_signals = [
        "sign in", "français", "trading status", "home trading",
        "toronto stock exchange trading notices",
        "copyright © 20", "tmx group limited",
    ]
    return sum(1 for s in web_signals if s in combined) >= 2


def extract_pdf_content(pdf_path_or_bytes) -> PDFContent:
    """
    Extract structured content from a PDF file.

    Parameters
    ----------
    pdf_path_or_bytes : str | bytes
        File path string or raw PDF bytes.

    Returns
    -------
    PDFContent dataclass.
    """
    result = PDFContent()

    if not _FITZ_AVAILABLE:
        result.extraction_ok = False
        result.error = "PyMuPDF (fitz) is not installed."
        return result

    try:
        if isinstance(pdf_path_or_bytes, (bytes, bytearray)):
            doc = fitz.open(stream=pdf_path_or_bytes, filetype="pdf")
        else:
            doc = fitz.open(pdf_path_or_bytes)
    except Exception as e:
        result.extraction_ok = False
        result.error = f"Cannot open PDF: {e}"
        return result

    result.page_count = len(doc)

    # ── Pass 1: gather all text blocks per page ──────────────────────────────
    page_line_sets: list[list[str]] = []
    all_raw_lines: list[str] = []
    table_count = 0

    for page in doc:
        blocks = page.get_text("dict")["blocks"]
        page_lines: list[str] = []

        for block in blocks:
            if block.get("type") != 0:
                continue  # skip image blocks
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                line_text = " ".join(s["text"] for s in spans).strip()
                if not line_text:
                    continue
                page_lines.append(line_text)
                all_raw_lines.append(line_text)

        # Count tables via PyMuPDF's find_tables
        try:
            tbls = page.find_tables()
            if tbls and tbls.tables:
                table_count += len(tbls.tables)
        except Exception:
            pass

        page_line_sets.append(page_lines)

    result.table_count = table_count
    result.raw_word_count = len(" ".join(all_raw_lines).split())

    # ── Detect web-scraped PDF (TMX / TSX bulletins) ─────────────────────────
    if _is_likely_web_chrome(all_raw_lines):
        result.detected_doc_type = "web_scraped"

    # ── Identify running headers/footers (appear on ≥40% of pages) ──────────
    n_pages = max(1, len(page_line_sets))
    line_page_count: dict[str, int] = defaultdict(int)
    for page_lines in page_line_sets:
        seen_this_page: set[str] = set()
        for line in page_lines:
            norm = _normalize(line)
            if norm not in seen_this_page:
                line_page_count[norm] += 1
                seen_this_page.add(norm)

    repeat_threshold = max(2, n_pages * 0.4)
    running_headers: set[str] = {
        norm for norm, cnt in line_page_count.items()
        if cnt >= repeat_threshold
    }

    # ── Pass 2: filter artifacts, extract paragraphs + headings ─────────────
    font_sizes: list[float] = []
    for page in doc:
        blocks = page.get_text("dict")["blocks"]
        for block in blocks:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    font_sizes.append(span.get("size", 10.0))

    median_font = sorted(font_sizes)[len(font_sizes) // 2] if font_sizes else 10.0
    heading_font_threshold = median_font * 1.15  # 15% larger than median

    paragraphs: list[str] = []
    headings: list[str] = []
    footnote_count = 0

    for page in doc:
        blocks = page.get_text("dict")["blocks"]
        for block in blocks:
            if block.get("type") != 0:
                continue

            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue

                line_text = " ".join(s["text"] for s in spans).strip()
                if not line_text:
                    continue

                norm = _normalize(line_text)

                # Skip running headers/footers
                if norm in running_headers:
                    continue
                # Skip page numbers
                if _is_page_number(line_text):
                    continue
                # Skip "Continued on next page" style lines
                if re.search(r"(continued on|suite à la|page \d+\s*of)", norm):
                    continue
                # Skip copyright lines
                if "copyright ©" in norm or "all rights reserved" in norm:
                    continue
                # Skip web navigation
                if result.detected_doc_type == "web_scraped":
                    if any(s in norm for s in ["sign in", "français", "trading status", "home trading"]):
                        continue

                # Detect footnote markers (small text or leading digit/*)
                max_size = max((s.get("size", 10) for s in spans), default=10)
                if max_size < median_font * 0.85:
                    if re.match(r"^[\d\*†‡§]+\s", line_text):
                        footnote_count += 1
                        paragraphs.append(line_text)
                        continue

                # Detect headings
                is_bold = any(s.get("flags", 0) & 16 for s in spans)
                is_large = max_size >= heading_font_threshold
                stripped = line_text.strip()
                word_count = len(stripped.split())
                # Require minimum 15 chars to exclude page numbers and stray bold chars.
                # Exclude pure-number lines (e.g. "1." "2.1" "42").
                if (
                    (is_bold or is_large)
                    and len(stripped) >= 15
                    and word_count <= 20
                    and not re.match(r"^\d+(\.\d+)*\.?\s*$", stripped)
                ):
                    headings.append(line_text)

                paragraphs.append(line_text)

    # Deduplicate headings: remove near-duplicates (>=80% word overlap).
    # This removes repeated running headers that bloat the heading list.
    deduped: list[str] = []
    for h in headings:
        h_words = set(re.sub(r"[^a-z0-9\s]", "", h.lower()).split())
        is_dup = False
        for seen in deduped:
            s_words = set(re.sub(r"[^a-z0-9\s]", "", seen.lower()).split())
            union = h_words | s_words
            if union and len(h_words & s_words) / len(union) >= 0.80:
                is_dup = True
                break
        if not is_dup:
            deduped.append(h)
    headings = deduped

    result.paragraphs = paragraphs
    result.headings = headings
    result.footnote_count = footnote_count
    result.clean_word_count = len(" ".join(paragraphs).split())

    doc.close()
    return result
