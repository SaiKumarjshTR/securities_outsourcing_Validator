"""
level1_content/sgml_extractor.py
──────────────────────────────────
Extract structured content from SGML for content fidelity comparison.

Produces:
  - Paragraph texts list
  - TI (section heading) texts list
  - Table count (SGMLTBL elements)
  - Footnote count (FOOTNOTE / FN elements)
  - Raw word count
"""

import re
from dataclasses import dataclass, field
from typing import Optional

from validator.core.entity_preprocessor import preprocess_sgml, normalize_for_comparison


# Tags whose text content counts as paragraphs
PARAGRAPH_TAGS = frozenset({"P", "P1", "P2", "P3", "P4", "SECP", "PARAP", "SPARAP", "SSECP"})

# Tags whose content is EXCLUDED from body text (metadata, not substantive)
EXCLUDED_TAGS = frozenset({"POLIDENT", "TBLCDEF", "TBLCDEFS"})

# Contact / admin sections to skip (determined at runtime by content signals)
CONTACT_KEYWORDS = frozenset({
    "for more information", "pour de plus amples", "contact:",
    "questions may be directed", "if you have questions",
    "please contact", "correspondence should be",
})


@dataclass
class SGMLContent:
    paragraphs: list[str] = field(default_factory=list)
    """Body paragraphs, entities resolved, tags stripped."""

    headings: list[str] = field(default_factory=list)
    """<TI> tag texts (section headings)."""

    table_count: int = 0
    footnote_count: int = 0
    raw_word_count: int = 0
    line_count: int = 0

    # Document metadata
    label: Optional[str] = None
    lang: Optional[str] = None
    doc_number: Optional[str] = None
    doc_title: Optional[str] = None

    extraction_ok: bool = True
    error: Optional[str] = None


def _inner_text(tag_content: str) -> str:
    """Strip sub-tags and return plain text."""
    text = re.sub(r"<[^>]+>", " ", tag_content)
    return re.sub(r"\s+", " ", text).strip()


def extract_sgml_content(raw: str) -> SGMLContent:
    """
    Parse a raw SGML string and extract structured content.

    Parameters
    ----------
    raw : str
        Raw SGML file contents (as read from disk).

    Returns
    -------
    SGMLContent dataclass.
    """
    result = SGMLContent()

    if not raw.strip():
        result.extraction_ok = False
        result.error = "Empty SGML content"
        return result

    result.line_count = len(raw.splitlines())

    # ── Resolve entities, keep tag structure ─────────────────────────────────
    text = preprocess_sgml(raw)

    # ── Extract POLIDOC attributes ────────────────────────────────────────────
    m = re.search(r"<POLIDOC\s+([^>]+)>", text)
    if m:
        attrs_str = m.group(1)
        for attr in ("LABEL", "LANG", "ADDDATE", "MODDATE"):
            am = re.search(rf'{attr}="([^"]*)"', attrs_str)
            if am:
                if attr == "LABEL":
                    result.label = am.group(1)
                elif attr == "LANG":
                    result.lang = am.group(1)

    # ── Extract POLIDENT N and TI ─────────────────────────────────────────────
    polident_m = re.search(
        r"<POLIDENT[^>]*>(.*?)</POLIDENT>", text, re.DOTALL
    )
    if polident_m:
        pi_inner = polident_m.group(1)
        n_m = re.search(r"<N[^>]*>(.*?)</N>", pi_inner, re.DOTALL)
        if n_m:
            result.doc_number = _inner_text(n_m.group(1))
        ti_m = re.search(r"<TI[^>]*>(.*?)</TI>", pi_inner, re.DOTALL)
        if ti_m:
            result.doc_title = _inner_text(ti_m.group(1))

    # ── Count tables and footnotes ────────────────────────────────────────────
    result.table_count = len(re.findall(r"<SGMLTBL[\s>]", text))
    result.footnote_count = len(
        re.findall(r"<FOOTNOTE[\s>]", text)
    ) + len(re.findall(r"<FN[\s>]", text))

    # ── Extract TI (section headings) ─────────────────────────────────────────
    ti_matches = re.findall(r"<TI[^>]*>(.*?)</TI>", text, re.DOTALL)
    headings: list[str] = []
    for tm in ti_matches:
        cleaned = _inner_text(tm)
        if cleaned:
            headings.append(normalize_for_comparison(cleaned))
    result.headings = headings

    # ── Extract body paragraphs ───────────────────────────────────────────────
    # Strategy: strip POLIDENT block, then find all paragraph-level tags
    # Remove POLIDENT block (metadata, not content)
    body = re.sub(
        r"<POLIDENT[^>]*>.*?</POLIDENT>", "", text, flags=re.DOTALL
    )
    # Remove TABLE structures (handled separately by table_count)
    body_no_tables = re.sub(
        r"<TABLE[^>]*>.*?</TABLE>", " [TABLE] ", body, flags=re.DOTALL
    )
    # Remove FOOTNOTE blocks (handled separately)
    body_no_fn = re.sub(
        r"<FOOTNOTE[^>]*>.*?</FOOTNOTE>", " [FOOTNOTE] ", body_no_tables, flags=re.DOTALL
    )

    paragraphs: list[str] = []

    # Collect all paragraph-level tag contents
    para_pattern = "|".join(PARAGRAPH_TAGS)
    for m2 in re.finditer(
        rf"<({para_pattern})[^>]*>(.*?)</\1>", body_no_fn, re.DOTALL
    ):
        para_text = _inner_text(m2.group(2))
        if len(para_text.split()) < 3:
            continue  # skip trivially short "paragraphs"

        # Skip contact/admin paragraphs
        lower = para_text.lower()
        if any(kw in lower for kw in CONTACT_KEYWORDS):
            continue

        paragraphs.append(normalize_for_comparison(para_text))

    result.paragraphs = paragraphs
    result.raw_word_count = len(" ".join(paragraphs).split())

    return result
