"""
validator/core/diff_generator.py
──────────────────────────────────
Actionable, line-specific fix engine for the HITL reviewer.

Given a raw SGML string and an L4Result from the validator, generates a list
of ActionableFix objects — each with:

  • Exact SGML line number
  • The current (wrong) SGML snippet
  • The suggested (correct) SGML snippet
  • PDF evidence (what the source PDF says)
  • Confidence level (high / medium / low)
  • auto_fixable flag (safe to apply with one click)
  • highlight_lines (which SGML lines to colour in the UI)

Dimension coverage
──────────────────
  D6 Encoding   → high confidence, auto-fixable (exact Unicode → entity)
  D7 Metadata   → high confidence (POLIDOC attribute corrections)
  D2 Tagging    → medium-high confidence (text search → <BOLD>/<EM>/<TI> wrap)
  D5 Ordering   → high confidence (inverted section pair, line numbers)
  D3 Text       → medium confidence (heuristic paragraph placement near <TI>)
  D4 Tables     → informational (PDF table data shown as insertion guide)

Usage
─────
  from validator.core.diff_generator import generate_fixes, get_highlight_map
  from validator.level4_source_compare.source_validator import validate_source_comparison

  l4 = validate_source_comparison(raw_sgml, pdf_path)
  fixes = generate_fixes(raw_sgml, l4)
  highlight_map = get_highlight_map(fixes)   # {line_no: "#ffe4b5", ...}
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Optional

from validator.level4_source_compare.source_validator import (
    L4Result,
    UNICODE_TO_ENTITY,
    _norm,
    _is_omittable,
)
from validator.level2_structural.structural_validator import L2Result


# ── ActionableFix dataclass ───────────────────────────────────────────────────

@dataclass
class ActionableFix:
    """One actionable fix for a specific SGML issue detected by the validator."""
    dimension: str          # "D2", "D3", "D4", "D5", "D6", "D7"
    severity: str           # "critical", "major", "minor"
    description: str        # plain-English description of the problem
    line_number: int        # 1-based SGML line number (0 = could not locate)
    line_content: str       # current text at that line (truncated to 120 chars)
    context_before: str     # 5-line SGML context snippet showing the problem
    suggested_fix: str      # corrected SGML snippet (or guidance if not auto-fixable)
    pdf_evidence: str       # what the source PDF shows (human-readable)
    pdf_page: int           # PDF page number to navigate to (0 = unknown)
    confidence: str         # "high", "medium", "low"
    auto_fixable: bool      # True = safe to apply programmatically in one click
    highlight_lines: list[int] = field(default_factory=list)  # 1-based lines to colour
    # For auto-fix application:
    _fix_old: str = field(default="", repr=False)  # exact string to replace (if auto_fixable)
    _fix_new: str = field(default="", repr=False)  # replacement string (if auto_fixable)


# ── Shared utilities ──────────────────────────────────────────────────────────

def _context(lines: list[str], center_idx: int, radius: int = 2) -> str:
    """Return a numbered code snippet centred on center_idx (0-based)."""
    start = max(0, center_idx - radius)
    end = min(len(lines), center_idx + radius + 1)
    out = []
    for i in range(start, end):
        marker = "► " if i == center_idx else "  "
        out.append(f"{i + 1:5d} {marker}{lines[i]}")
    return "\n".join(out)


def _find_tag_line(lines: list[str], pattern: str, flags: int = 0) -> int:
    """Return 0-based index of first line matching regex, or -1."""
    rx = re.compile(pattern, flags)
    for i, line in enumerate(lines):
        if rx.search(line):
            return i
    return -1


def _find_text_in_sgml(lines: list[str], text: str) -> list[int]:
    """
    Return 0-based line indices where `text` appears in SGML text content
    (outside tag markup).  Case-normalised via _norm().
    """
    norm_text = _norm(text)
    if not norm_text or len(norm_text.split()) < 2:
        return []
    matches = []
    for i, line in enumerate(lines):
        text_part = re.sub(r"<[^>]+>", " ", line)
        if norm_text in _norm(text_part):
            matches.append(i)
    return matches


def _wrap_in_tag(line: str, span_text: str, tag: str) -> str:
    """Wrap `span_text` in `<tag>...</tag>` within `line`.  Returns original if not found."""
    if span_text in line:
        return line.replace(span_text, f"<{tag}>{span_text}</{tag}>", 1)
    # Case-insensitive fallback
    idx = line.lower().find(span_text.lower())
    if idx >= 0:
        actual = line[idx: idx + len(span_text)]
        return line[:idx] + f"<{tag}>{actual}</{tag}>" + line[idx + len(span_text):]
    return line


def _find_all_ti_lines(lines: list[str]) -> list[tuple[int, str]]:
    """
    Return list of (0-based line index, heading_text) for every <TI>...</TI> in the SGML.
    Handles TI tags that may span multiple lines by joining the whole text first.
    """
    joined = "\n".join(lines)
    result = []
    for m in re.finditer(r"<TI[^>]*>(.*?)</TI>", joined, re.DOTALL):
        line_idx = joined[: m.start()].count("\n")
        heading_text = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        result.append((line_idx, heading_text))
    return result


# ─────────────────────────────────────────────────────────────────────────────
# D6  Encoding — Unicode characters that must be SGML entities
# Confidence: HIGH   Auto-fixable: YES
# ─────────────────────────────────────────────────────────────────────────────

def _fixes_d6(lines: list[str]) -> list[ActionableFix]:
    fixes: list[ActionableFix] = []
    for i, line in enumerate(lines):
        # Only inspect text content — skip inside tag markup
        text_only = re.sub(r"<[^>]+>", "\x00", line)
        for char, entity in UNICODE_TO_ENTITY.items():
            if char not in text_only:
                continue
            corrected = line.replace(char, entity)
            char_name = unicodedata.name(char, repr(char))
            fixes.append(ActionableFix(
                dimension="D6",
                severity="major",
                description=f"Raw Unicode {repr(char)} ({char_name}) must be encoded as {entity}",
                line_number=i + 1,
                line_content=line.strip()[:120],
                context_before=_context(lines, i),
                suggested_fix=_context(
                    lines[:i] + [corrected] + lines[i + 1:], i
                ),
                pdf_evidence="",
                pdf_page=0,
                confidence="high",
                auto_fixable=True,
                highlight_lines=[i + 1],
                _fix_old=line,
                _fix_new=corrected,
            ))
    return fixes


# ─────────────────────────────────────────────────────────────────────────────
# D7  Metadata — POLIDOC attribute mismatches
# Confidence: HIGH   Auto-fixable: YES for LANG, NO for N/ADDDATE
# ─────────────────────────────────────────────────────────────────────────────

def _fixes_d7(lines: list[str], l4: L4Result) -> list[ActionableFix]:
    fixes: list[ActionableFix] = []
    if not l4.metadata_mismatches:
        return fixes

    polidoc_idx = _find_tag_line(lines, r"<POLIDOC")

    for mismatch in l4.metadata_mismatches:

        # ── LANG mismatch ─────────────────────────────────────────────────────
        if "LANG=" in mismatch:
            m = re.search(r"LANG='?([A-Z]{2})'?.*?'?([A-Z]{2})'?$", mismatch)
            expected_lang = l4.d7_expected_lang or (m.group(2) if m else "")
            if not expected_lang or polidoc_idx < 0:
                continue
            line = lines[polidoc_idx]
            # Find current LANG value
            lm = re.search(r'LANG="([^"]*)"', line)
            current_lang = lm.group(1) if lm else ""
            corrected = line.replace(f'LANG="{current_lang}"', f'LANG="{expected_lang}"')
            fixes.append(ActionableFix(
                dimension="D7",
                severity="major",
                description=(
                    f"LANG attribute is '{current_lang}' but PDF text character "
                    f"frequency analysis suggests '{expected_lang}'"
                ),
                line_number=polidoc_idx + 1,
                line_content=line.strip()[:120],
                context_before=_context(lines, polidoc_idx),
                suggested_fix=_context(
                    lines[:polidoc_idx] + [corrected] + lines[polidoc_idx + 1:],
                    polidoc_idx,
                ),
                pdf_evidence=(
                    f"PDF language detected as '{expected_lang}' by French character "
                    f"frequency analysis (é, è, ê, à, â, ç, etc.)"
                ),
                pdf_page=1,
                confidence="medium",
                auto_fixable=True,
                highlight_lines=[polidoc_idx + 1],
                _fix_old=line,
                _fix_new=corrected,
            ))

        # ── Doc number (<N> tag) mismatch ─────────────────────────────────────
        elif "<N>=" in mismatch or "not found in PDF" in mismatch:
            n_idx = _find_tag_line(lines, r"<N[\s>]")
            if n_idx < 0:
                continue
            fixes.append(ActionableFix(
                dimension="D7",
                severity="major",
                description=(
                    f"Document number in <N> tag not found on PDF first pages. "
                    f"Detail: {mismatch}"
                ),
                line_number=n_idx + 1,
                line_content=lines[n_idx].strip()[:120],
                context_before=_context(lines, n_idx),
                suggested_fix=(
                    f"Verify the <N> tag value against the PDF cover page.\n"
                    f"PDF doc number detected: '{l4.d7_pdf_doc_number or '(not detected)'}'\n"
                    f"Current SGML: {lines[n_idx].strip()}"
                ),
                pdf_evidence=(
                    f"PDF first-page doc number: '{l4.d7_pdf_doc_number or 'not detected'}'"
                ),
                pdf_page=1,
                confidence="high",
                auto_fixable=False,
                highlight_lines=[n_idx + 1],
            ))

        # ── ADDDATE mismatch ──────────────────────────────────────────────────
        elif "ADDDATE=" in mismatch:
            idx = polidoc_idx if polidoc_idx >= 0 else _find_tag_line(lines, r"ADDDATE=")
            if idx < 0:
                continue
            fixes.append(ActionableFix(
                dimension="D7",
                severity="major",
                description=f"ADDDATE date gap > 5 years vs PDF date. {mismatch}",
                line_number=idx + 1,
                line_content=lines[idx].strip()[:120],
                context_before=_context(lines, idx),
                suggested_fix=(
                    "Check ADDDATE value on <POLIDOC> tag.\n"
                    "ADDDATE is the keying date, not the publication date — "
                    "a gap > 5 years is unusually large and should be verified."
                ),
                pdf_evidence="PDF publication date detected — compare with ADDDATE value.",
                pdf_page=1,
                confidence="high",
                auto_fixable=False,
                highlight_lines=[idx + 1],
            ))

    return fixes


# ─────────────────────────────────────────────────────────────────────────────
# D2  Tagging — bold / italic / heading spans not wrapped in correct tags
# Confidence: HIGH if exact text match; MEDIUM if fuzzy
# ─────────────────────────────────────────────────────────────────────────────

# Maximum D2 fix suggestions per span type. Large instruments can have 100+
# untagged bold/italic spans (section numbers like "1. (1)"). Showing all of
# them buries the truly actionable fixes in noise. Cap each type so HITL sees
# the most representative examples and is not overwhelmed.
_MAX_D2_BOLD_FIXES    = 10
_MAX_D2_ITALIC_FIXES  = 10
_MAX_D2_HEADING_FIXES = 10


def _fixes_d2(lines: list[str], l4: L4Result) -> list[ActionableFix]:
    fixes: list[ActionableFix] = []

    # ── Bold spans → <BOLD> ─────────────────────────────────────────────────────────────────────────
    bold_fixes: list[ActionableFix] = []
    for span in l4.d2_untagged_bold:
        if _is_omittable(span) or len(span.split()) < 2:
            continue
        match_lines = _find_text_in_sgml(lines, span)
        if match_lines:
            i = match_lines[0]
            line = lines[i]
            corrected = _wrap_in_tag(line, span, "BOLD")
            exact = corrected != line
            fixes.append(ActionableFix(
                dimension="D2",
                severity="major",
                description=f"Bold text from PDF not wrapped in <BOLD>: '{span[:50]}'",
                line_number=i + 1,
                line_content=line.strip()[:120],
                context_before=_context(lines, i),
                suggested_fix=_context(
                    lines[:i] + [corrected] + lines[i + 1:], i
                ) if exact else f"Wrap the text in <BOLD>...</BOLD>:\n<BOLD>{span}</BOLD>",
                pdf_evidence="This text is rendered in bold font in the source PDF.",
                pdf_page=0,
                confidence="high" if exact else "medium",
                auto_fixable=exact,
                highlight_lines=[i + 1],
                _fix_old=line if exact else "",
                _fix_new=corrected if exact else "",
            ))
        else:
            # Text not found — cannot locate line
            bold_fixes.append(ActionableFix(
                dimension="D2",
                severity="minor",
                description=f"Bold text from PDF not found in SGML: '{span[:50]}'",
                line_number=0,
                line_content="",
                context_before="",
                suggested_fix=f"Search SGML for this text and wrap in <BOLD>...</BOLD>:\n<BOLD>{span}</BOLD>",
                pdf_evidence="This text is bold in the source PDF but could not be found in the SGML.",
                pdf_page=0,
                confidence="low",
                auto_fixable=False,
                highlight_lines=[],
            ))
        if len(bold_fixes) >= _MAX_D2_BOLD_FIXES:
            break
    fixes.extend(bold_fixes)

    # ── Italic spans → <EM> ─────────────────────────────────────────────────────────────────────────
    italic_fixes: list[ActionableFix] = []
    for span in l4.d2_untagged_italic:
        if _is_omittable(span) or len(span.split()) < 2:
            continue
        match_lines = _find_text_in_sgml(lines, span)
        if match_lines:
            i = match_lines[0]
            line = lines[i]
            corrected = _wrap_in_tag(line, span, "EM")
            exact = corrected != line
            fixes.append(ActionableFix(
                dimension="D2",
                severity="minor",
                description=f"Italic text from PDF not wrapped in <EM>: '{span[:50]}'",
                line_number=i + 1,
                line_content=line.strip()[:120],
                context_before=_context(lines, i),
                suggested_fix=_context(
                    lines[:i] + [corrected] + lines[i + 1:], i
                ) if exact else f"Wrap the text in <EM>...</EM>:\n<EM>{span}</EM>",
                pdf_evidence="This text is rendered in italic font in the source PDF.",
                pdf_page=0,
                confidence="high" if exact else "medium",
                auto_fixable=exact,
                highlight_lines=[i + 1],
                _fix_old=line if exact else "",
                _fix_new=corrected if exact else "",
            ))
        if len(italic_fixes) >= _MAX_D2_ITALIC_FIXES:
            break
    fixes.extend(italic_fixes)

    # ── Untagged headings → <TI> ──────────────────────────────────────────────────────────────────
    heading_fixes: list[ActionableFix] = []
    for heading in l4.d2_untagged_headings:
        if _is_omittable(heading) or len(heading.split()) < 2:
            continue
        match_lines = _find_text_in_sgml(lines, heading)
        if match_lines:
            i = match_lines[0]
            line = lines[i]
            # Heading restructuring is complex — don't auto-fix, just locate
            fixes.append(ActionableFix(
                dimension="D2",
                severity="major",
                description=f"Heading detected by font-size in PDF, not tagged as <TI>: '{heading[:50]}'",
                line_number=i + 1,
                line_content=line.strip()[:120],
                context_before=_context(lines, i),
                suggested_fix=(
                    f"This line should be a <TI> heading.\n"
                    f"Current: {line.strip()}\n"
                    f"Suggested: <TI>{heading}</TI>"
                ),
                pdf_evidence="PDF font-size analysis: this text is ≥15% larger than body text.",
                pdf_page=0,
                confidence="medium",
                auto_fixable=False,   # heading restructuring changes BLOCK structure
                highlight_lines=[i + 1],
            ))
        if len(heading_fixes) >= _MAX_D2_HEADING_FIXES:
            break
    fixes.extend(heading_fixes)

    return fixes


# ─────────────────────────────────────────────────────────────────────────────
# D5  Ordering — section heading inversions
# Confidence: HIGH   Auto-fixable: NO (section swap is too risky to automate)
# ─────────────────────────────────────────────────────────────────────────────

def _fixes_d5(lines: list[str], l4: L4Result) -> list[ActionableFix]:
    fixes: list[ActionableFix] = []
    if not l4.d5_inverted_pairs:
        return fixes

    ti_index = _find_all_ti_lines(lines)

    for h_before, h_after in l4.d5_inverted_pairs[:3]:
        # h_before appears before h_after in SGML, but should appear AFTER in correct order
        line_before = -1
        line_after = -1
        for line_idx, ti_text in ti_index:
            if SequenceMatcher(None, _norm(ti_text), h_before).ratio() >= 0.65 and line_before < 0:
                line_before = line_idx
            if SequenceMatcher(None, _norm(ti_text), h_after).ratio() >= 0.65 and line_after < 0:
                line_after = line_idx

        if line_before >= 0 and line_after >= 0:
            fixes.append(ActionableFix(
                dimension="D5",
                severity="major",
                description=(
                    f"Section order mismatch: "
                    f"'{h_before[:40]}' (line {line_before + 1}) "
                    f"appears BEFORE '{h_after[:40]}' (line {line_after + 1}) in SGML, "
                    f"but should appear AFTER it (per PDF)."
                ),
                line_number=line_before + 1,
                line_content=lines[line_before].strip()[:120],
                context_before=_context(lines, line_before),
                suggested_fix=(
                    f"The section starting at line {line_before + 1} must move "
                    f"to AFTER the section at line {line_after + 1}.\n\n"
                    f"PDF order:  '{h_after[:40]}' → then → '{h_before[:40]}'\n"
                    f"SGML order: '{h_before[:40]}' → then → '{h_after[:40]}'  ← WRONG\n\n"
                    f"Move the entire BLOCK/SEC from line {line_before + 1} to after line {line_after + 1}."
                ),
                pdf_evidence=(
                    f"In the PDF, '{h_after[:40]}' precedes '{h_before[:40]}'"
                ),
                pdf_page=0,
                confidence="high",
                auto_fixable=False,
                highlight_lines=[line_before + 1, line_after + 1],
            ))

    return fixes


# ─────────────────────────────────────────────────────────────────────────────
# D3  Missing paragraphs — heuristic placement
# Confidence: MEDIUM   Auto-fixable: NO
# ─────────────────────────────────────────────────────────────────────────────

# Maximum D3 fix suggestions per document. Large instruments (e.g. NI 93-101)
# include companion policy and appendix text in the PDF that is intentionally
# absent from the main SGML file. Generating hundreds of fix suggestions for
# this case is misleading noise for HITL reviewers.
_MAX_D3_FIXES = 15


def _fixes_d3_truncated(lines: list[str], l4: L4Result) -> list[ActionableFix]:
    """
    D3-d: Paragraphs present in SGML but with leading text deleted.

    For each truncated paragraph, locate the SGML line, show the SGML version
    versus what the PDF had, and tell the HITL reviewer what prefix is missing.
    """
    fixes: list[ActionableFix] = []
    if not l4.truncated_paragraphs:
        return fixes

    for para in l4.truncated_paragraphs[:_MAX_D3_FIXES]:
        norm_words = _norm(para).split()
        # Try to find the paragraph in SGML using its LATER words (since leading words are gone).
        # If the initial window fails (heavily truncated or different encoding), try progressively
        # later word windows until a match is found.
        sgml_line_idx = -1
        for _offset in [5, 10, 15, 20, 25]:
            _end = _offset + 10
            if _end > len(norm_words):
                break
            search_text = " ".join(norm_words[_offset:_end])
            match_lines = _find_text_in_sgml(lines, search_text)
            if match_lines:
                sgml_line_idx = match_lines[0]
                break
        # Last resort: first 8 words (paragraph might be only lightly truncated)
        if sgml_line_idx < 0 and len(norm_words) >= 4:
            search_text = " ".join(norm_words[:8])
            match_lines = _find_text_in_sgml(lines, search_text)
            sgml_line_idx = match_lines[0] if match_lines else -1

        # The deleted prefix: first 5 words of the PDF paragraph
        missing_prefix = " ".join(para.split()[:8])
        sgml_snippet = lines[sgml_line_idx].strip()[:120] if sgml_line_idx >= 0 else "(line not located)"

        fixes.append(ActionableFix(
            dimension="D3",
            severity="major",
            description=(
                f"Paragraph has text deleted from its beginning. "
                f"PDF starts: '{para[:80]}' — "
                f"SGML is missing: '{missing_prefix}'"
            ),
            line_number=sgml_line_idx + 1 if sgml_line_idx >= 0 else 0,
            line_content=sgml_snippet[:120],
            context_before=_context(lines, sgml_line_idx) if sgml_line_idx >= 0 else "",
            suggested_fix=(
                f"The SGML paragraph is missing its opening text.\n\n"
                f"PDF source starts with:\n  '{para[:200]}'\n\n"
                f"SGML has (truncated version):\n  '{sgml_snippet}'\n\n"
                f"Prepend the missing prefix:\n"
                f"  '{missing_prefix}...'"
            ),
            pdf_evidence=f'PDF full paragraph: "{para[:300]}"',
            pdf_page=0,
            confidence="high",
            auto_fixable=False,
            highlight_lines=[sgml_line_idx + 1] if sgml_line_idx >= 0 else [],
        ))

    return fixes


def _fixes_d3_mutations(lines: list[str], l4: L4Result) -> list[ActionableFix]:
    """
    D3-e: Paragraphs present in SGML but with inline word additions/deletions.

    For each mutation, find the SGML line and show a word-level diff so the
    HITL reviewer knows exactly which words were changed.
    """
    fixes: list[ActionableFix] = []
    if not l4.inline_changed_paragraphs:
        return fixes

    for mut in l4.inline_changed_paragraphs[:_MAX_D3_FIXES]:
        # Find the SGML line that has the mutated text
        sgml_snippet_words = mut.get("sgml_text", "").split()
        search_anchor = " ".join(sgml_snippet_words[:8]) if len(sgml_snippet_words) >= 8 else mut.get("sgml_text", "")[:60]
        match_lines = _find_text_in_sgml(lines, search_anchor)
        sgml_line_idx = match_lines[0] if match_lines else -1

        deleted = mut.get("deleted_words", [])
        inserted = mut.get("inserted_words", [])
        ratio = mut.get("ratio", 0.0)

        # Build human-readable diff description
        diff_parts = []
        if deleted:
            diff_parts.append(f"DELETED from PDF: {deleted[:10]}")
        if inserted:
            diff_parts.append(f"ADDED in SGML (not in PDF): {inserted[:10]}")

        diff_display = "\n".join(diff_parts) if diff_parts else "minor wording differences"

        fixes.append(ActionableFix(
            dimension="D3",
            severity="major",
            description=(
                f"Paragraph text was modified (similarity {ratio:.0%}). "
                + (f"Words deleted: {deleted[:5]} " if deleted else "")
                + (f"Words added: {inserted[:5]}" if inserted else "")
            ),
            line_number=sgml_line_idx + 1 if sgml_line_idx >= 0 else 0,
            line_content=(
                lines[sgml_line_idx].strip()[:120] if sgml_line_idx >= 0 else mut.get("sgml_text", "")[:120]
            ),
            context_before=_context(lines, sgml_line_idx) if sgml_line_idx >= 0 else "",
            suggested_fix=(
                f"Word-level diff between PDF source and SGML:\n\n"
                f"{diff_display}\n\n"
                f"PDF original:\n  '{mut.get('pdf_text', '')[:300]}'\n\n"
                f"SGML current:\n  '{mut.get('sgml_text', '')[:300]}'\n\n"
                f"Correct the SGML to match the PDF source exactly."
            ),
            pdf_evidence=f'PDF paragraph: "{mut.get("pdf_text", "")[:250]}"',
            pdf_page=0,
            confidence="medium",
            auto_fixable=False,
            highlight_lines=[sgml_line_idx + 1] if sgml_line_idx >= 0 else [],
        ))

    return fixes


def _fixes_d3_short_lines(lines: list[str], l4: L4Result) -> list[ActionableFix]:
    """
    D3-f: Short lines (3-4 words) from PDF/DOCX not found anywhere in SGML.

    These are often contact lines, date phrases, or short labels that were
    silently deleted from the SGML during processing.
    """
    fixes: list[ActionableFix] = []
    if not l4.missing_short_lines:
        return fixes

    # Find a reasonable insertion point near the end of the document
    fallback_line = len(lines) - 1
    for i in range(len(lines) - 1, max(0, len(lines) - 20), -1):
        if re.search(r"</FREEFORM>|</POLIDOC>|</BLOCK", lines[i]):
            fallback_line = max(0, i - 1)
            break

    for short_line in l4.missing_short_lines[:10]:
        fixes.append(ActionableFix(
            dimension="D3",
            severity="minor",
            description=f"Short line from PDF not found in SGML: '{short_line}'",
            line_number=fallback_line + 1,
            line_content=lines[fallback_line].strip()[:120] if fallback_line < len(lines) else "",
            context_before=_context(lines, fallback_line),
            suggested_fix=(
                f"This short text from the PDF is absent from the SGML:\n\n"
                f"  '{short_line}'\n\n"
                f"Check if it should appear as a <P> paragraph or contact line."
            ),
            pdf_evidence=f'PDF line: "{short_line}"',
            pdf_page=0,
            confidence="medium",
            auto_fixable=False,
            highlight_lines=[],
        ))

    return fixes


def _fixes_d3(lines: list[str], l4: L4Result) -> list[ActionableFix]:
    """
    For each missing paragraph, find the most likely insertion point by:
    1. Matching PDF heading before the paragraph against SGML <TI> tags
    2. Suggesting insertion after the matching <TI> block
    """
    fixes: list[ActionableFix] = []
    if not l4.missing_paragraphs:
        return fixes

    ti_index = _find_all_ti_lines(lines)
    pdf_headings = l4.pdf_headings  # stored from PDF extraction

    for missing_para in l4.missing_paragraphs:
        if len(missing_para.split()) < 5:
            continue

        # Heuristic: find the best matching PDF heading
        best_ti_line = -1
        best_heading = ""
        for h in pdf_headings:
            nh = _norm(h)
            if len(nh.split()) < 2:
                continue
            for ti_line_idx, ti_text in ti_index:
                ratio = SequenceMatcher(None, _norm(ti_text), nh).ratio()
                if ratio >= 0.65 and best_ti_line < 0:
                    best_ti_line = ti_line_idx
                    best_heading = ti_text
                    break
            if best_ti_line >= 0:
                break

        # If no heading match, find end of last block as fallback
        if best_ti_line < 0:
            for i in range(len(lines) - 1, max(0, len(lines) - 30), -1):
                if re.search(r"</FREEFORM>|</BLOCK|</POLIDOC>", lines[i]):
                    best_ti_line = i - 1
                    break

        insertion_line = best_ti_line if best_ti_line >= 0 else len(lines) - 1

        # Scan forward from heading to find a good insertion point (after first <P> or end of section)
        if best_ti_line >= 0:
            for j in range(best_ti_line + 1, min(best_ti_line + 25, len(lines))):
                if re.search(r"</SEC>|</BLOCK|<SEC\b|<BLOCK\d", lines[j]):
                    insertion_line = j - 1
                    break
                if re.search(r"<P[ >]|<P\d[ >]", lines[j]):
                    insertion_line = j + 1

        location_desc = (
            f"near line {insertion_line + 1}"
            + (f" (after <TI>{best_heading[:40]}</TI>)" if best_heading else "")
        )

        fixes.append(ActionableFix(
            dimension="D3",
            severity="major",
            description=f"Paragraph from PDF not found in SGML: '{missing_para[:70]}...'",
            line_number=insertion_line + 1,
            line_content=(
                lines[insertion_line].strip()[:120]
                if 0 <= insertion_line < len(lines) else ""
            ),
            context_before=_context(lines, insertion_line),
            suggested_fix=(
                f"Insert missing paragraph {location_desc}:\n\n"
                f"<P>{missing_para[:300]}</P>"
                + (" [truncated — see PDF evidence for full text]"
                   if len(missing_para) > 300 else "")
            ),
            pdf_evidence=f'Source PDF paragraph: "{missing_para[:200]}"',
            pdf_page=0,
            confidence="medium",
            auto_fixable=False,
            highlight_lines=[insertion_line + 1] if insertion_line >= 0 else [],
        ))

    return fixes[:_MAX_D3_FIXES]


# ─────────────────────────────────────────────────────────────────────────────
# D4  Completeness — missing tables / schedules
# Confidence: LOW (PDF table extraction is heuristic)   Auto-fixable: NO
# ─────────────────────────────────────────────────────────────────────────────

def _fixes_d4(lines: list[str], l4: L4Result) -> list[ActionableFix]:
    fixes: list[ActionableFix] = []
    for issue in l4.issues:
        if issue.get("category") != "completeness":
            continue
        desc = issue.get("description", "")
        sev = issue.get("severity", "minor")

        if "no <TABLE>" in desc or ("table" in desc.lower() and "mismatch" in desc.lower()):
            # Find insertion point: just before </FREEFORM> or </POLIDOC>
            insertion_idx = len(lines) - 1
            for i in range(len(lines) - 1, -1, -1):
                if re.search(r"</FREEFORM>|</POLIDOC>", lines[i]):
                    insertion_idx = max(0, i - 1)
                    break
            fixes.append(ActionableFix(
                dimension="D4",
                severity=sev,
                description=desc[:120],
                line_number=insertion_idx + 1,
                line_content=(
                    lines[insertion_idx].strip()[:120]
                    if insertion_idx < len(lines) else ""
                ),
                context_before=_context(lines, insertion_idx),
                suggested_fix=(
                    "Copy the table from the source PDF and encode as:\n\n"
                    "<TABLE>\n"
                    "<SGMLTBL>\n"
                    "  <TBLBODY>\n"
                    "    <TBLROW>\n"
                    "      <TBLCELL>Column 1</TBLCELL>\n"
                    "      <TBLCELL>Column 2</TBLCELL>\n"
                    "    </TBLROW>\n"
                    "  </TBLBODY>\n"
                    "</SGMLTBL>\n"
                    "</TABLE>"
                ),
                pdf_evidence="Open source PDF and navigate to the missing table. Check the PDF viewer.",
                pdf_page=0,
                confidence="low",
                auto_fixable=False,
                highlight_lines=[insertion_idx + 1],
            ))

        elif "schedule" in desc.lower() or "appendix" in desc.lower():
            fixes.append(ActionableFix(
                dimension="D4",
                severity="minor",
                description=desc[:120],
                line_number=len(lines),
                line_content=lines[-1].strip()[:120] if lines else "",
                context_before=_context(lines, len(lines) - 1),
                suggested_fix=(
                    "Add Schedule/Appendix content at the end of the document:\n\n"
                    "<SCHEDDOC>\n  <!-- schedule content here -->\n</SCHEDDOC>"
                ),
                pdf_evidence="PDF contains Schedule or Appendix content not present in SGML.",
                pdf_page=0,
                confidence="low",
                auto_fixable=False,
                highlight_lines=[len(lines)],
            ))

    return fixes


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

_SEVERITY_RANK = {"critical": 0, "major": 1, "minor": 2}


# ─────────────────────────────────────────────────────────────────────────────
# D4-g/h  Contact details — emails, phones, URLs, postal codes
# Confidence: HIGH (exact pattern match)   Auto-fixable: NO
# ─────────────────────────────────────────────────────────────────────────────

def _fixes_contact_details(lines: list[str], l4: L4Result) -> list[ActionableFix]:
    """
    Generate HITL fix cards for missing contact details detected by check_contact_details().

    Covers: email addresses (D4-g), phone numbers (D4-g), URLs/hyperlinks (D4-h),
    Canadian postal codes (D4-g), and SGML-only items that don't appear in the PDF
    (flagged as possible fabrication or copy-paste error).
    """
    fixes: list[ActionableFix] = []

    # Find a general insertion region near the end of the document body
    fallback_line = len(lines) - 1
    for i in range(len(lines) - 1, max(0, len(lines) - 30), -1):
        if re.search(r"</FREEFORM>|</POLIDOC>|</BLOCK", lines[i]):
            fallback_line = max(0, i - 1)
            break

    # ── Missing emails ────────────────────────────────────────────────────────
    for email in l4.missing_emails[:8]:
        # Try to find the nearest context in SGML (partial domain match)
        domain = email.split("@")[-1] if "@" in email else ""
        match_lines = _find_text_in_sgml(lines, domain) if domain else []
        ln_idx = match_lines[0] if match_lines else fallback_line

        fixes.append(ActionableFix(
            dimension="D4",
            severity="major",
            description=f"Email address from PDF missing in SGML: '{email}'",
            line_number=ln_idx + 1,
            line_content=lines[ln_idx].strip()[:120] if ln_idx < len(lines) else "",
            context_before=_context(lines, ln_idx),
            suggested_fix=(
                f"The email address '{email}' appears in the source PDF but is "
                f"absent from the SGML.\n\n"
                f"Add to the appropriate location:\n  {email}"
            ),
            pdf_evidence=f'Source PDF email: "{email}"',
            pdf_page=0,
            confidence="high",
            auto_fixable=False,
            highlight_lines=[ln_idx + 1] if ln_idx >= 0 else [],
        ))

    # ── Missing phone numbers ─────────────────────────────────────────────────
    for phone in l4.missing_phones[:5]:
        fixes.append(ActionableFix(
            dimension="D4",
            severity="major",
            description=f"Phone number from PDF missing in SGML: '{phone}'",
            line_number=fallback_line + 1,
            line_content=lines[fallback_line].strip()[:120] if fallback_line < len(lines) else "",
            context_before=_context(lines, fallback_line),
            suggested_fix=(
                f"Phone number '{phone}' appears in source PDF but is absent from SGML.\n\n"
                f"Add to the contact information section:\n  {phone}"
            ),
            pdf_evidence=f'Source PDF phone: "{phone}"',
            pdf_page=0,
            confidence="high",
            auto_fixable=False,
            highlight_lines=[],
        ))

    # ── Missing URLs / hyperlinks ─────────────────────────────────────────────
    for url in l4.missing_urls[:8]:
        # Try to find a nearby line via domain name
        domain_part = re.sub(r"https?://|www\.", "", url).split("/")[0]
        match_lines = _find_text_in_sgml(lines, domain_part[:30]) if len(domain_part) > 5 else []
        ln_idx = match_lines[0] if match_lines else fallback_line

        fixes.append(ActionableFix(
            dimension="D4",
            severity="major",
            description=f"Hyperlink/URL from PDF missing in SGML: '{url[:80]}'",
            line_number=ln_idx + 1,
            line_content=lines[ln_idx].strip()[:120] if ln_idx < len(lines) else "",
            context_before=_context(lines, ln_idx),
            suggested_fix=(
                f"URL '{url}' is present in the source PDF (either as visible text or "
                f"as a hyperlink annotation) but is absent from the SGML.\n\n"
                f"If this is an external reference, add using appropriate SGML markup:\n"
                f'  <XREF HREF="{url}">{url}</XREF>'
            ),
            pdf_evidence=f'Source PDF URL: "{url}"',
            pdf_page=0,
            confidence="high",
            auto_fixable=False,
            highlight_lines=[ln_idx + 1] if ln_idx >= 0 else [],
        ))

    # ── Missing postal codes ──────────────────────────────────────────────────
    for postal in l4.missing_postal_codes[:5]:
        fixes.append(ActionableFix(
            dimension="D4",
            severity="minor",
            description=f"Canadian postal code from PDF missing in SGML: '{postal}'",
            line_number=fallback_line + 1,
            line_content=lines[fallback_line].strip()[:120] if fallback_line < len(lines) else "",
            context_before=_context(lines, fallback_line),
            suggested_fix=(
                f"Postal code '{postal}' from the source PDF is absent from the SGML.\n"
                f"Verify the address block and add the postal code."
            ),
            pdf_evidence=f'Source PDF postal code: "{postal}"',
            pdf_page=0,
            confidence="high",
            auto_fixable=False,
            highlight_lines=[],
        ))

    # ── Extra items in SGML (not in PDF — possible fabrication) ──────────────
    extra_items = (
        [(e, "email") for e in l4.extra_emails[:3]] +
        [(p, "phone") for p in l4.extra_phones[:3]] +
        [(u[:60], "URL") for u in l4.extra_urls[:3]]
    )
    for item_val, item_type in extra_items:
        match_lines = _find_text_in_sgml(lines, item_val[:30])
        ln_idx = match_lines[0] if match_lines else fallback_line
        fixes.append(ActionableFix(
            dimension="D4",
            severity="minor",
            description=(
                f"SGML contains {item_type} NOT present in source PDF: '{item_val}' "
                f"— possible copy-paste error or fabrication."
            ),
            line_number=ln_idx + 1,
            line_content=lines[ln_idx].strip()[:120] if ln_idx < len(lines) else "",
            context_before=_context(lines, ln_idx),
            suggested_fix=(
                f"The {item_type} '{item_val}' appears in the SGML but was NOT found "
                f"in the source PDF.\n\n"
                f"Verify against the source PDF and remove if it does not appear there."
            ),
            pdf_evidence=f"This {item_type} was NOT found in the source PDF text or annotations.",
            pdf_page=0,
            confidence="medium",
            auto_fixable=False,
            highlight_lines=[ln_idx + 1] if ln_idx >= 0 else [],
        ))

    return fixes


# ─────────────────────────────────────────────────────────────────────────────
# L2  Structural — every structural issue as an individual HITL fix card
# Confidence: HIGH (exact regex match)   Auto-fixable: NO (structural changes)
# ─────────────────────────────────────────────────────────────────────────────

_L2_CATEGORY_LABEL = {
    "dtd_schema":       "DTD / Schema",
    "tag_nesting":      "Tag Nesting",
    "entity_handling":  "Entity Encoding",
    "table_structure":  "Table Structure",
    "graphics":         "Graphics",
    "content_rules":    "Content Rules",
    "legal_structure":  "Legal Structure",
}


def _fixes_l2(lines: list[str], l2_issues: list[dict]) -> list[ActionableFix]:
    """Convert every L2 structural issue into an individual HITL fix card.

    Each issue already carries a `location` string like 'line 42' produced by
    the structural validator's location_tracker.  We parse out the line number
    so the card can highlight the exact SGML line.
    """
    fixes: list[ActionableFix] = []
    for issue in l2_issues:
        category    = issue.get("category", "structural")
        severity    = issue.get("severity", "major")
        description = issue.get("description", "")
        location    = issue.get("location", "")
        impact      = issue.get("impact", "")

        # Parse line number from strings like 'line 42', 'line 42 col 5',
        # 'line 42  path: BLOCK1/P'
        line_no = 0
        if location:
            m = re.search(r"line\s+(\d+)", location, re.IGNORECASE)
            if m:
                line_no = int(m.group(1))

        ctx         = _context(lines, line_no - 1) if 0 < line_no <= len(lines) else ""
        line_content = lines[line_no - 1].strip()[:120] if 0 < line_no <= len(lines) else ""
        cat_label   = _L2_CATEGORY_LABEL.get(category, category.replace("_", " ").title())
        impact_note = f"  ({impact})" if impact else ""

        fixes.append(ActionableFix(
            dimension="L2",
            severity=severity,
            description=f"[{cat_label}] {description}{impact_note}",
            line_number=line_no,
            line_content=line_content,
            context_before=ctx,
            suggested_fix=(
                f"Structural issue — manual correction required.\n{description}"
            ),
            pdf_evidence="",
            pdf_page=0,
            confidence="high",
            auto_fixable=False,
            highlight_lines=[line_no] if line_no > 0 else [],
        ))
    return fixes


def generate_fixes(
    raw_sgml: str,
    l4_result: L4Result,
    l2_result: Optional[L2Result] = None,
) -> list[ActionableFix]:
    """
    Generate all actionable fixes for the given SGML, L4 result, and L2 result.

    Parameters
    ----------
    raw_sgml   : str               — raw SGML file content
    l4_result  : L4Result          — result from validate_source_comparison()
    l2_result  : L2Result | None   — result from validate_structure(); when
                                     provided, every L2 issue gets its own
                                     HITL fix card with a line number.

    Returns
    -------
    List of ActionableFix sorted by severity (critical → major → minor),
    then by dimension (L2 → D2 → D3 → D4 → D5 → D6 → D7).
    """
    lines = raw_sgml.splitlines()
    all_fixes: list[ActionableFix] = []

    # L2: structural issues — ALL occurrences, each as its own fix card
    if l2_result and l2_result.issues:
        all_fixes.extend(_fixes_l2(lines, l2_result.issues))

    # D6: always run — no PDF needed, highest confidence
    all_fixes.extend(_fixes_d6(lines))

    # D7: metadata mismatches
    if l4_result.metadata_mismatches:
        all_fixes.extend(_fixes_d7(lines, l4_result))

    # D2: tagging — ALL occurrences (no cap)
    if (l4_result.d2_untagged_bold
            or l4_result.d2_untagged_italic
            or l4_result.d2_untagged_headings):
        all_fixes.extend(_fixes_d2(lines, l4_result))

    # D5: ordering (requires d5_inverted_pairs populated by check_ordering)
    if l4_result.d5_inverted_pairs:
        all_fixes.extend(_fixes_d5(lines, l4_result))

    # D3: missing paragraphs
    if l4_result.missing_paragraphs:
        all_fixes.extend(_fixes_d3(lines, l4_result))

    # D3-d: Truncated paragraphs (leading text deleted)
    if l4_result.truncated_paragraphs:
        all_fixes.extend(_fixes_d3_truncated(lines, l4_result))

    # D3-e: Inline word mutations (paragraphs present but changed)
    if l4_result.inline_changed_paragraphs:
        all_fixes.extend(_fixes_d3_mutations(lines, l4_result))

    # D3-f: Short lines missing
    if l4_result.missing_short_lines:
        all_fixes.extend(_fixes_d3_short_lines(lines, l4_result))

    # D4: completeness (table count, image, footnote etc.)
    all_fixes.extend(_fixes_d4(lines, l4_result))

    # D4-g/h: Contact details — emails, phones, URLs, postal codes
    _has_contact_issues = (
        l4_result.missing_emails or l4_result.extra_emails or
        l4_result.missing_phones or l4_result.extra_phones or
        l4_result.missing_urls or l4_result.extra_urls or
        l4_result.missing_postal_codes
    )
    if _has_contact_issues:
        all_fixes.extend(_fixes_contact_details(lines, l4_result))

    # Sort: severity first, then dimension, then located fixes before unlocated
    all_fixes.sort(key=lambda f: (
        _SEVERITY_RANK.get(f.severity, 9),
        f.dimension,
        0 if f.line_number > 0 else 1,
    ))

    return all_fixes


def get_highlight_map(fixes: list[ActionableFix]) -> dict[int, str]:
    """
    Return {line_number: css_colour} for all problem lines across all fixes.

    Colour by severity (most severe wins if a line has multiple issues):
      critical → #ffcccc  (red)
      major    → #ffe4b5  (orange)
      minor    → #fffacd  (yellow)
    """
    _colours = {"critical": "#ffcccc", "major": "#ffe4b5", "minor": "#fffacd"}
    result: dict[int, tuple[int, str]] = {}  # {line: (rank, colour)}
    for fix in fixes:
        colour = _colours.get(fix.severity, "#fffacd")
        rank = _SEVERITY_RANK.get(fix.severity, 9)
        for ln in fix.highlight_lines:
            if ln not in result or rank < result[ln][0]:
                result[ln] = (rank, colour)
    return {ln: v[1] for ln, v in result.items()}


def apply_auto_fixes(raw_sgml: str, fixes: list[ActionableFix]) -> tuple[str, int]:
    """
    Apply all auto_fixable fixes to `raw_sgml` in one pass.

    Returns
    -------
    (corrected_sgml, count_applied)
    """
    corrected = raw_sgml
    applied = 0
    for fix in fixes:
        if fix.auto_fixable and fix._fix_old and fix._fix_new:
            if fix._fix_old in corrected:
                corrected = corrected.replace(fix._fix_old, fix._fix_new, 1)
                applied += 1
    return corrected, applied
