"""
formatting_extractor.py — Bold, italic, heading detection from PyMuPDF spans.

Design decisions:
- Trust font FLAGS first (most reliable for digital PDFs), font NAME second.
- Both can disagree — e.g. some PDFs set the Bold flag but use a regular-weight
  font name variant. We OR both checks so we never miss a detection.
- Heading level is determined by relative font size vs the body baseline.
  We compute the body baseline dynamically (modal font size on that page),
  not a hardcoded threshold, because legal PDFs vary from 8pt to 14pt body.
- "Fake bold" (thicker stroke via PDF graphics state, no flag set) is
  detectable via the 'color' trick in some PDFs but is rare in our corpus;
  we don't over-engineer for it.
"""
import re
import logging
from typing import Dict, List, Optional
from collections import Counter

log = logging.getLogger(__name__)

# ── Font name pattern sets ────────────────────────────────────────────────────
_BOLD_NAME_RE = re.compile(
    r"(?:^|[,-])(?:Bold|Bd|Black|Heavy|Semibold|SemiBold|Demi|ExtraBold)"
    r"|Bold(?:MT)?$",
    re.IGNORECASE,
)
_ITALIC_NAME_RE = re.compile(
    r"(?:^|[,-])(?:Italic|It|Oblique|Slanted|Cursive)"
    r"|(?:Italic|Oblique)(?:MT)?$",
    re.IGNORECASE,
)

# PyMuPDF font flag bit positions
_FLAG_SUPERSCRIPT = 1 << 0   # bit 0 — superscript
_FLAG_ITALIC      = 1 << 1   # bit 1 — italic
_FLAG_BOLD        = 1 << 4   # bit 4 — bold


def is_bold(span: Dict) -> bool:
    """
    Return True if this PyMuPDF span represents bold text.

    Checks:
      1. Font flag bit 4 (most reliable)
      2. Font name contains a bold keyword variant
    """
    flags = span.get("flags", 0)
    if flags & _FLAG_BOLD:
        return True
    font_name = span.get("font", "")
    if font_name and _BOLD_NAME_RE.search(font_name):
        return True
    return False


def is_italic(span: Dict) -> bool:
    """
    Return True if this PyMuPDF span represents italic text.

    Checks:
      1. Font flag bit 1 (most reliable)
      2. Font name contains an italic/oblique keyword variant
    """
    flags = span.get("flags", 0)
    if flags & _FLAG_ITALIC:
        return True
    font_name = span.get("font", "")
    if font_name and _ITALIC_NAME_RE.search(font_name):
        return True
    return False


def is_superscript(span: Dict) -> bool:
    """Return True if span is a superscript (footnote marker candidate)."""
    return bool(span.get("flags", 0) & _FLAG_SUPERSCRIPT)


def compute_body_font_size(spans: List[Dict]) -> float:
    """
    Compute the modal (most common) font size across all spans on a page.
    This is the body text baseline — headings will be larger than this.

    Rounds sizes to nearest 0.5pt to group near-identical sizes together.
    Falls back to 10.0 if no spans provided.
    """
    if not spans:
        return 10.0
    rounded = [round(s.get("size", 10.0) * 2) / 2 for s in spans]
    counter = Counter(rounded)
    modal = counter.most_common(1)[0][0]
    return modal


def detect_heading_level(size: float, body_size: float) -> int:
    """
    Determine heading level (1–4) based on font size relative to body.

    Returns:
        0  — not a heading (size ≤ body + 0.5)
        1  — H1: size > body + 6
        2  — H2: size > body + 3
        3  — H3: size > body + 1.5
        4  — H4: size > body + 0.5
    """
    diff = size - body_size
    if diff > 6:
        return 1
    if diff > 3:
        return 2
    if diff > 1.5:
        return 3
    if diff > 0.5:
        return 4
    return 0


def detect_heading_level_block(
    size: float,
    body_size: float,
    is_bold_block: bool,
    text: str,
) -> int:
    """
    Heading detection for a full text block (paragraph level).

    Extends the span-level size heuristic with a bold+short heuristic:
    regulatory PDFs use bold same-size headings (ALL-CAPS or Title-Case)
    that have zero size delta and are missed by detect_heading_level alone.

    Rules (applied in order — first match wins):
      Size-based (from detect_heading_level):
        H1 / H2 / H3 / H4 as before.
      Bold + short (same-size headings common in CSA/OSC/BC docs):
        ALL-CAPS bold, ≤ 15 words, no sentence-end punctuation → H2
        Title-Case bold, ≤ 10 words, no sentence-end punctuation → H3
        Any bold, ≤ 6 words,  no sentence-end punctuation → H4
    """
    # Size-based takes priority if it fires
    size_level = detect_heading_level(size, body_size)
    if size_level > 0:
        return size_level

    if not is_bold_block:
        return 0

    stripped = text.strip()
    if not stripped:
        return 0

    # Reject likely body sentences: end with . ! ? or are very long
    if stripped[-1] in ".!?" and len(stripped.split()) > 6:
        return 0

    words = stripped.split()
    wc = len(words)
    has_lower = any(c.islower() for c in stripped)
    is_all_caps = (not has_lower and stripped.isupper()) if stripped.replace(" ", "").isalpha() else False

    # ALL-CAPS bold → H2
    if is_all_caps and wc <= 15:
        return 2

    # Title-Case bold (first letter of most words capitalised) → H3
    cap_words = sum(1 for w in words if w and w[0].isupper())
    is_title_case = has_lower and (cap_words / max(wc, 1)) >= 0.6
    if is_title_case and wc <= 10:
        return 3

    # Short bold phrase → H4 (require at least one real word ≥ 3 alphanumeric chars)
    if wc <= 6 and any(len(re.sub(r'[^a-zA-Z0-9]', '', w)) >= 3 for w in words):
        return 4

    return 0


def classify_span(span: Dict, body_size: float) -> Dict:
    """
    Enrich a raw PyMuPDF span dict with computed formatting flags.

    Returns the same dict with added keys:
        _bold     : bool
        _italic   : bool
        _super    : bool
        _heading  : int  (0 = not heading, 1–4 = heading level)
    """
    size = span.get("size", body_size)
    span["_bold"]    = is_bold(span)
    span["_italic"]  = is_italic(span)
    span["_super"]   = is_superscript(span)
    span["_heading"] = detect_heading_level(size, body_size)
    return span


def classify_spans_for_page(spans: List[Dict]) -> List[Dict]:
    """
    Classify an entire page's worth of spans in one call.
    Computes body size dynamically from the page spans.
    """
    body_size = compute_body_font_size(spans)
    return [classify_span(s, body_size) for s in spans]
