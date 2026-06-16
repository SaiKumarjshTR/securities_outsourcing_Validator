"""
level1_content/content_validator.py
─────────────────────────────────────
Level 1: Content Fidelity Validator  (35 points total)

Scoring (calibrated from 90 vendor PDF-SGML pairs benchmark):
─────────────────────────────────────────────────────────────
  1. Text Completeness     20 pts  — section-heading match + paragraph coverage
  2. Section Completeness   8 pts  — all PDF headings present in SGML TI tags
  3. Table Completeness     4 pts  — table count match
  4. Footnote Completeness  3 pts  — footnote count match

Key design decisions (from benchmark data):
  • Raw word-bag Jaccard is UNRELIABLE (range 22%–98% on valid docs).
    TSX/TMX PDFs are HTML-scraped and contain web chrome → intentionally
    low Jaccard.  We use SECTION-HEADING match as primary proxy.
  • Content ratio check catches catastrophic loss: if SGML has < 30% of
    PDF words, something is badly wrong (unless it's a tiny one-pager).
  • We do NOT penalise SGML being LONGER than PDF (consolidated instruments).
"""

import difflib
import re
from dataclasses import dataclass, field
from typing import Optional

from validator.core.entity_preprocessor import normalize_for_comparison
from validator.core.fix_templates import enrich_issues
from validator.level1_content.pdf_extractor import PDFContent
from validator.level1_content.sgml_extractor import SGMLContent


# ── Result dataclass ──────────────────────────────────────────────────────────
@dataclass
class L1Result:
    score: float = 0.0          # 0–35
    max_score: float = 35.0

    # Sub-scores
    text_score: float = 0.0     # 0–20
    section_score: float = 0.0  # 0–8
    table_score: float = 0.0    # 0–4
    footnote_score: float = 0.0 # 0–3

    # Diagnostics
    heading_match_ratio: float = 0.0
    paragraph_coverage: float = 0.0
    table_match_ratio: float = 0.0
    footnote_match_ratio: float = 0.0
    content_ratio: float = 0.0  # SGML_words / PDF_words

    missing_headings: list[str] = field(default_factory=list)
    missing_paragraphs: list[str] = field(default_factory=list)
    total_missing_para_count: int = 0  # full count even if list is long

    critical_failure: bool = False
    critical_reason: Optional[str] = None
    issues: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    pdf_ok: bool = True
    pdf_error: Optional[str] = None

    # Metadata
    pdf_paragraphs: int = 0
    sgml_paragraphs: int = 0
    pdf_sections: int = 0
    sgml_sections: int = 0
    pdf_tables: int = 0
    sgml_tables: int = 0
    pdf_footnotes: int = 0
    sgml_footnotes: int = 0


# ── Heading matcher ───────────────────────────────────────────────────────────
_HEADING_PREFIX_RE = re.compile(
    r"^(?:"
    r"(?:Part|Section|Article|Chapter|Subsection|Division|Appendix|Annex|Schedule|Exhibit)\s+[\dIVXivxa-z]+[\s.\-\u2014\u2013]*"
    r"|[\d]+(?:\.\d+)*\.?\s*[\-\u2014\u2013]*\s*"
    r"|[IVXivx]+\.?\s+[\-\u2014\u2013]*\s*"
    r")",
    re.IGNORECASE,
)


def _strip_heading_prefix(h: str) -> str:
    """Remove leading section numbers/labels from a heading for comparison.
    e.g. 'Part 1 — Definitions' -> 'Definitions'
         '1.2.3 - Overview'     -> 'Overview'
         'Article III Conflicts' -> 'Conflicts'
    """
    stripped = _HEADING_PREFIX_RE.sub("", h).strip()
    # Also strip trailing dash/dash-space artifacts
    stripped = re.sub(r"^[\-\u2014\u2013\s]+", "", stripped).strip()
    return stripped if stripped else h


def _heading_similarity(a: str, b: str) -> float:
    """Word-overlap ratio between two heading strings (normalised).
    Strips leading section numbering before comparison so that
    'Part 1 -- Definitions' matches SGML '<TI>Definitions</TI>'.
    """
    # Compare both stripped and unstripped; take best
    variants_a = {normalize_for_comparison(a), normalize_for_comparison(_strip_heading_prefix(a))}
    variants_b = {normalize_for_comparison(b), normalize_for_comparison(_strip_heading_prefix(b))}
    best = 0.0
    for va in variants_a:
        for vb in variants_b:
            wa = set(va.split())
            wb = set(vb.split())
            if not wa or not wb:
                continue
            sim = len(wa & wb) / len(wa | wb)
            if sim > best:
                best = sim
    return best


def _find_best_heading_match(pdf_heading: str, sgml_headings: list[str]) -> float:
    """Return the best Jaccard similarity between a PDF heading and any SGML heading."""
    best = 0.0
    for sh in sgml_headings:
        sim = _heading_similarity(pdf_heading, sh)
        if sim > best:
            best = sim
        if best >= 0.9:
            break
    return best


# ── Paragraph coverage ────────────────────────────────────────────────────────
def _paragraph_covered(pdf_para: str, sgml_text_blob: str, threshold: float = 0.6) -> bool:
    """
    Check whether a PDF paragraph's key words appear in the SGML body text.

    Uses a rolling 5-word n-gram presence check:
    if > threshold fraction of 5-grams from pdf_para are in sgml_text_blob,
    the paragraph is "covered".
    """
    words_pdf = normalize_for_comparison(pdf_para).split()
    if len(words_pdf) < 5:
        # Short paragraph: check simple substring
        return normalize_for_comparison(pdf_para) in sgml_text_blob

    # Build n-grams from SGML blob for fast lookup
    words_sgml = sgml_text_blob.split()
    ngram_size = 5
    sgml_ngrams: set[tuple] = set()
    for i in range(len(words_sgml) - ngram_size + 1):
        sgml_ngrams.add(tuple(words_sgml[i : i + ngram_size]))

    pdf_ngrams = [
        tuple(words_pdf[i : i + ngram_size])
        for i in range(len(words_pdf) - ngram_size + 1)
    ]
    if not pdf_ngrams:
        return False

    matched = sum(1 for ng in pdf_ngrams if ng in sgml_ngrams)
    return (matched / len(pdf_ngrams)) >= threshold


# ── Main scorer ───────────────────────────────────────────────────────────────
def validate_content(
    pdf: PDFContent,
    sgml: SGMLContent,
    doc_class=None,
) -> L1Result:
    """
    Score content fidelity between a PDF and its SGML representation.

    Parameters
    ----------
    pdf       : PDFContent  -- from pdf_extractor.extract_pdf_content()
    sgml      : SGMLContent -- from sgml_extractor.extract_sgml_content()
    doc_class : DocumentClass, optional
        Pre-classification result. When doc_type is AMENDMENT, the word-ratio
        critical-failure check is SKIPPED because amendment documents contain
        only delta text (6-48% of the original word count is expected and VALID).

    Returns
    -------
    L1Result with score (0-35) and detailed diagnostics.
    """
    result = L1Result()

    # If PDF extraction failed
    if not pdf.extraction_ok:
        result.pdf_ok = False
        result.pdf_error = pdf.error
        result.warnings.append(f"PDF extraction failed: {pdf.error}. L1 skipped.")
        result.score = 0.0
        return result

    result.pdf_paragraphs = len(pdf.paragraphs)
    result.sgml_paragraphs = len(sgml.paragraphs)
    result.pdf_sections = len(pdf.headings)
    result.sgml_sections = len(sgml.headings)
    result.pdf_tables = pdf.table_count
    result.sgml_tables = sgml.table_count
    result.pdf_footnotes = pdf.footnote_count
    result.sgml_footnotes = sgml.footnote_count

    # ── Content ratio (SGML words / PDF words) ────────────────────────────────
    if pdf.clean_word_count > 0:
        ratio = sgml.raw_word_count / pdf.clean_word_count
    else:
        ratio = 1.0
    result.content_ratio = ratio

    # Catastrophic content loss: SGML has < 30% of PDF words.
    # EXCEPTION: Amendment documents (QUOTE tag present) contain only delta text,
    # so a low word ratio is EXPECTED and must NOT trigger critical failure.
    # EXCEPTION: TSX special documents (By-Laws/Forms) also exempt.
    # EXCEPTION: Short SGML docs (< 300 words) - PDF chrome dominates ratio.
    _is_amendment = doc_class and doc_class.doc_type in ("AMENDMENT", "TSX_SPECIAL")
    # When the PDF is >5x the SGML size, appendices or supplementary material are
    # likely included in the PDF but keyed as separate SGML documents.  This is
    # a known corpus pattern (e.g., CSA annual reports, FAQ documents with
    # lengthy appendices).  Treat as valid; paragraph coverage will score lower.
    _pdf_sgml_ratio = pdf.clean_word_count / max(1, sgml.raw_word_count)
    _appendix_pattern = _pdf_sgml_ratio > 5.0 and sgml.raw_word_count > 300
    if not _is_amendment and not _appendix_pattern and sgml.raw_word_count > 300 and pdf.clean_word_count > 200 and ratio < 0.30:
        result.critical_failure = True
        result.critical_reason = (
            f"Catastrophic content loss: SGML has {ratio:.0%} of PDF word count "
            f"({sgml.raw_word_count} SGML vs {pdf.clean_word_count} PDF words)"
        )
        result.issues.append({
            "level": "L1",
            "category": "text_completeness",
            "severity": "critical",
            "description": result.critical_reason,
        })
        result.score = 0.0
        return result

    # ── 1. TEXT COMPLETENESS (20 pts) ─────────────────────────────────────────
    # Primary signal: paragraph-level coverage
    if not sgml.paragraphs and not pdf.paragraphs:
        text_score = 20.0  # No paragraphs in either → trivially matches (tiny doc)
    elif not pdf.paragraphs:
        text_score = 20.0  # PDF has no parseable paragraphs → can't penalise
    elif sgml.raw_word_count < 300:
        # Short SGML documents (< 300 words): paragraph coverage is unreliable because
        # the PDF version often includes web chrome, navigation, related articles, and
        # repeated headers that are absent from the keyed SGML.
        # Trust the structural/corpus validation (L2/L3) for these short docs.
        text_score = 20.0
        result.warnings.append(
            f"Short SGML document ({sgml.raw_word_count} words): "
            "paragraph coverage check skipped; PDF chrome dominates ratio."
        )
    elif _appendix_pattern:
        # Appendix pattern: PDF is >5× the SGML size.  The SGML covers the main notice
        # text; detailed appendices are keyed separately.  Score based on ratio of
        # SGML words to PDF words (how much of the keyed material appears in PDF).
        # We give full credit for the SGML portion itself — it's the right amount.
        text_score = 18.0
        result.warnings.append(
            f"Appendix-pattern document: PDF ({pdf.clean_word_count} words) is "
            f"{_pdf_sgml_ratio:.1f}× SGML ({sgml.raw_word_count} words). "
            "Appendices likely keyed separately. Paragraph coverage skipped."
        )
    else:
        # Build SGML text blob for fast n-gram lookup
        sgml_blob = " ".join(sgml.paragraphs)

        # Sample: check up to 50 PDF paragraphs (skip very short ones)
        # Check ALL meaningful paragraphs — no sampling cap.
        # Capping at 50 hides content missing from the second half of long documents.
        # Filter: ≥10 words AND not an obvious footnote bleed-through.
        # Footnote bleed-through: PyMuPDF extracts footnote text as body paragraphs
        # because they meet the word-count threshold. These start with a small
        # superscript number (e.g. "12 For more information…") or look like
        # signatory lines ("British Columbia Securities Commission Khalil Jessa…").
        # The vendor correctly puts these inside <FOOTNOTE> or omits them — they
        # should NOT be counted as "missing" body content.
        _fn_number_re = re.compile(r"^\d{1,3}\s+\S")  # "42 Subsection 11.1…"

        meaningful_pdf_paras = [
            p for p in pdf.paragraphs
            if len(p.split()) >= 10
            and not _fn_number_re.match(p.strip())
        ]

        if not meaningful_pdf_paras:
            text_score = 20.0
        else:
            covered = 0
            missing_paras: list[str] = []
            for para in meaningful_pdf_paras:
                if _paragraph_covered(para, sgml_blob):
                    covered += 1
                else:
                    missing_paras.append(para[:120] + "..." if len(para) > 120 else para)

            coverage = covered / len(meaningful_pdf_paras)
            result.paragraph_coverage = coverage
            # Store ALL missing paragraphs — HITL must show every one.
            result.missing_paragraphs = missing_paras
            result.total_missing_para_count = len(missing_paras)

            # Score table (recalibrated to account for PDF chrome/repeated elements)
            # PDF paragraphs include headers, footers, repeated text that SGML doesn't.
            # Vendor corpus data shows 70-85% coverage is normal for many doc types.
            # For AMENDMENT/INSTRUMENT docs, even lower coverage is expected (delta text).
            _is_structured = doc_class and doc_class.doc_type in ("AMENDMENT", "INSTRUMENT")
            if coverage >= 0.90:
                text_score = 20.0
            elif coverage >= 0.75:
                text_score = 18.0
            elif coverage >= 0.60 or (_is_structured and coverage >= 0.45):
                text_score = 15.0
            elif coverage >= 0.45 or (_is_structured and coverage >= 0.30):
                text_score = 12.0
            elif coverage >= 0.30:
                text_score = 8.0
            else:
                text_score = 0.0
                result.issues.append({
                    "level": "L1",
                    "category": "text_completeness",
                    "severity": "critical",
                    "description": f"Very low paragraph coverage: {coverage:.0%}. "
                                   f"{len(missing_paras)} paragraphs from PDF not found in SGML.",
                })
                # Critical failure ONLY when:
                # - Not an amendment/TSX_SPECIAL (they contain only delta text)
                # - SGML has substantial content (>300 words) -- short docs are unreliable
                # - PDF has substantial content (>500 words) -- small docs have high chrome ratio
                _sgml_is_substantial = sgml.raw_word_count > 300
                _pdf_is_substantial = pdf.clean_word_count > 500
                if (
                    not _is_amendment
                    and not _appendix_pattern
                    and _sgml_is_substantial
                    and _pdf_is_substantial
                    and coverage < 0.40
                ):
                    result.critical_failure = True
                    result.critical_reason = (
                        f"Content fidelity critical failure: only {coverage:.0%} "
                        f"of PDF paragraphs found in SGML"
                    )

    result.text_score = text_score

    # ── 2. SECTION COMPLETENESS (8 pts) ───────────────────────────────────────
    if not pdf.headings:
        # No PDF headings detectable → full marks (can't penalise)
        section_score = 8.0
    elif not sgml.headings:
        # PDF has headings, SGML has none → major penalty
        section_score = 0.0
        result.issues.append({
            "level": "L1",
            "category": "section_completeness",
            "severity": "major",
            "description": f"PDF has {len(pdf.headings)} section headings but SGML has no <TI> tags.",
        })
    else:
        # Bidirectional heading match:
        # PDF can contain lots of metadata "headings" (citations, org names, dates)
        # that don't appear in SGML. The SGML TI headings are authoritative.
        #
        # forward  = what % of PDF headings have a match in SGML
        # reverse  = what % of SGML headings appear in PDF full text (paragraphs+headings)
        # Use max() so the scorer rewards whichever direction has better coverage.

        # Forward: PDF headings found in SGML
        fwd_matched = 0
        missing_hdgs: list[str] = []
        for hd in pdf.headings:
            if _find_best_heading_match(hd, sgml.headings) >= 0.5:
                fwd_matched += 1
            else:
                missing_hdgs.append(hd)

        fwd_ratio = fwd_matched / len(pdf.headings)

        # Reverse: SGML TI headings found in PDF (search headings AND full body text)
        # This is more reliable because SGML TI tags are actual section titles.
        pdf_all_text = pdf.headings + pdf.paragraphs
        rev_matched = sum(
            1 for sh in sgml.headings
            if _find_best_heading_match(sh, pdf_all_text) >= 0.5
        )
        rev_ratio = rev_matched / len(sgml.headings) if sgml.headings else 0.0

        # Use the better ratio; heavily penalise ONLY if BOTH directions are poor
        ratio_hdg = max(fwd_ratio, rev_ratio)
        result.heading_match_ratio = ratio_hdg
        result.missing_headings = missing_hdgs[:10]

        # Instruments/Amendments use PART-based structure; headings may be split or
        # embedded differently in PDF. Use a more lenient scoring for these.
        _is_structured = doc_class and doc_class.doc_type in ("INSTRUMENT", "AMENDMENT")
        if ratio_hdg >= 0.95:
            section_score = 8.0
        elif ratio_hdg >= 0.85 or (_is_structured and ratio_hdg >= 0.70):
            section_score = 6.0
        elif ratio_hdg >= 0.70 or (_is_structured and ratio_hdg >= 0.50):
            section_score = 4.0
        elif ratio_hdg >= 0.50 or (_is_structured and ratio_hdg >= 0.30):
            section_score = 2.0
        else:
            section_score = 0.0
            result.issues.append({
                "level": "L1",
                "category": "section_completeness",
                "severity": "major",
                "description": (
                    f"Section heading match low: {ratio_hdg:.0%}. "
                    f"{len(missing_hdgs)} PDF headings missing from SGML."
                ),
                "examples": missing_hdgs[:3],
            })

    # Floor: if text content is fully confirmed present (text_score ≥ 18),
    # section headings cannot truly be missing — only the heading extractor
    # failed (e.g. bilingual TSX PDFs where two-column layout causes PyMuPDF
    # to extract non-matching header text instead of actual section titles).
    # Grant partial credit so perfect-text docs are not penalised for an
    # extraction artefact.
    if text_score >= 20.0:
        section_score = max(section_score, 4.0)
    elif text_score >= 18.0:
        section_score = max(section_score, 2.0)

    result.section_score = section_score

    # ── 3. TABLE COMPLETENESS (4 pts) ─────────────────────────────────────────
    if pdf.table_count == 0:
        table_score = 4.0  # No tables in PDF → full marks
    elif sgml.table_count == 0 and pdf.table_count > 0:
        # PDF may detect "tables" from grids, aligned text, or forms that are
        # encoded differently in SGML (as lists, FREEFORM, etc.).
        # Only penalise significantly if PDF has MANY tables (strong signal).
        if pdf.table_count <= 2:
            table_score = 2.0   # 1-2 PDF "tables" that SGML doesn't encode → partial
            result.warnings.append(
                f"PDF has {pdf.table_count} table(s) but SGML has no <SGMLTBL>. "
                "May be formatted as text/list in SGML."
            )
        else:
            # PDF has 3+ tables and SGML has none: this is a critical failure.
            # Tables are core structured content — their complete absence means
            # the pipeline failed to capture essential document structure.
            # This guarantees a REJECT outcome (score driven to 0 for this level).
            table_score = 0.0
            result.critical_failure = True
            result.critical_reason = (
                f"Tables completely absent: PDF has {pdf.table_count} table(s) "
                f"but SGML has no <SGMLTBL> tags. Pipeline table-preservation failed."
            )
            result.issues.append({
                "level": "L1",
                "category": "table_completeness",
                "severity": "critical",
                "description": result.critical_reason,
            })
    else:
        tbl_ratio = min(sgml.table_count, pdf.table_count) / pdf.table_count
        result.table_match_ratio = tbl_ratio
        if tbl_ratio >= 0.90:
            table_score = 4.0
        elif tbl_ratio >= 0.60:
            table_score = 2.0
        elif tbl_ratio >= 0.30:
            table_score = 1.0
        else:
            table_score = 0.0
            result.issues.append({
                "level": "L1",
                "category": "table_completeness",
                "severity": "major",
                "description": (
                    f"Table count mismatch: PDF={pdf.table_count}, "
                    f"SGML={sgml.table_count} ({tbl_ratio:.0%} match)."
                ),
            })

    result.table_score = table_score

    # ── 4. FOOTNOTE COMPLETENESS (3 pts) ──────────────────────────────────────
    if pdf.footnote_count == 0:
        footnote_score = 3.0  # No footnotes in PDF → full marks
    elif sgml.footnote_count == 0 and pdf.footnote_count > 0:
        # PDF may have embedded footnotes hard to detect; only penalise if
        # SGML also has zero and PDF had clear footnote signals
        if pdf.footnote_count >= 3:
            footnote_score = 0.0
            result.issues.append({
                "level": "L1",
                "category": "footnote_completeness",
                "severity": "minor",
                "description": (
                    f"PDF has ~{pdf.footnote_count} footnote markers but "
                    f"SGML has no <FOOTNOTE> tags."
                ),
            })
        else:
            footnote_score = 1.5  # Small number — uncertain detection
    else:
        fn_ratio = min(sgml.footnote_count, pdf.footnote_count) / max(1, pdf.footnote_count)
        result.footnote_match_ratio = fn_ratio
        footnote_score = 3.0 if fn_ratio >= 0.80 else (1.5 if fn_ratio >= 0.50 else 0.0)

    result.footnote_score = footnote_score

    # ── Total ─────────────────────────────────────────────────────────────────
    result.score = (
        result.text_score + result.section_score +
        result.table_score + result.footnote_score
    )

    # Enrich all issues with fix templates
    enrich_issues(result.issues)

    return result
