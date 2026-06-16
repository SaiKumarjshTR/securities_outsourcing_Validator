"""
level2_structural/structural_validator.py
───────────────────────────────────────────
Level 2: Structural Compliance Validator  (40 points total)

Categories
──────────
  A. DTD / Schema Compliance       8 pts  — valid tags, required attrs, encoding
  B. Tag Nesting Rules             8 pts  — BLOCK/PART hierarchies, forbidden nesting
  C. Entity Handling               6 pts  — entity format, no bare & < >
  D. Table Structure               4 pts  — TABLE/SGMLTBL pairing, row/cell structure
  E. Graphics / Images             4 pts  — GRAPHIC FILENAME, BMP format
  F. Content Rules                 4 pts  — URLs in LINE, empty tags, whitespace
  G. Legal Document Rules          6 pts  — POLIDENT, DEF→DEFP, STATREF, dates

All rules derived from:
  - 98 vendor corpus files (ground truth)
  - COMPLETE_KEYING_RULES_UPDATED.txt
  - Carswell DTD v4.7
"""

import re
from dataclasses import dataclass, field
from typing import Optional

from validator.core.entity_preprocessor import preprocess_sgml, VALID_CARSWELL_ENTITY_NAMES
from validator.core.location_tracker import (
    build_line_index,
    find_tag_line,
    find_all_tag_lines,
    find_tag_path,
    extract_context_snippet,
    loc_from_match,
)
from validator.core.fix_templates import enrich_issues
from validator.core.location_tracker import (
    build_line_index,
    find_tag_line,
    find_all_tag_lines,
    find_tag_path,
    extract_context_snippet,
    loc_from_match,
)
from validator.core.fix_templates import enrich_issues
from validator.core.sgml_parser import extract_structure, parse_sgml
from validator.core.valid_tags import (
    VALID_TAGS,
    get_invalid_tags,
    REQUIRED_CHILDREN,
    FORBIDDEN_PARENT_CHILD,
)
from validator.core.document_classifier import DocumentClass

# ── Entities allowed in Carswell SGML (250+ entities, loaded dynamically) ─────
# Falls back to the hardcoded baseline if entities_list.txt is unavailable.
VALID_ENTITIES: frozenset = VALID_CARSWELL_ENTITY_NAMES or frozenset({
    "mdash", "ndash", "nbsp", "ldquo", "rdquo", "lsquo", "rsquo",
    "bull", "hellip", "verbar", "check", "square", "dottab", "newline",
    "times", "plusmn", "copy", "ordm",
    "eacute", "egrave", "ecirc", "agrave", "ocirc", "Eacute",
    "sup-e", "sup-er",
    "amp", "lt", "gt", "apos", "quot",
})

# Required POLIDOC attributes
REQUIRED_POLIDOC_ATTRS = {"LANG", "LABEL", "ADDDATE"}

# Valid LANG values
VALID_LANG = {"EN", "FR"}

# Valid DATE LABEL values (from corpus)
VALID_DATE_LABELS = frozenset({
    "Effective", "Published", "Amended", "Revised",
    "Adopted", "Approved", "In Force", "Coming into Force",
})

# BMP filename pattern: SBxxxxxx.BMP (SB + exactly 6 digits)
BMP_FILENAME_RE = re.compile(r"^SB\d{6}\.BMP$", re.IGNORECASE)

# YYYYMMDD date pattern
YYYYMMDD_RE = re.compile(r"^\d{8}$")

# Bullet characters that must NOT appear at the start of ITEM content (B11)
BULLET_CHARS_RE = re.compile(r"^[\u2022\u00b7\u2013\u2014\u2019*\-]\s")


# ── Result dataclass ──────────────────────────────────────────────────────────
@dataclass
class L2Result:
    score: float = 0.0
    max_score: float = 40.0

    # Sub-scores
    schema_score: float = 0.0       # A: 0–8
    nesting_score: float = 0.0      # B: 0–8
    entity_score: float = 0.0       # C: 0–6
    table_score: float = 0.0        # D: 0–4
    graphics_score: float = 0.0     # E: 0–4
    content_score: float = 0.0      # F: 0–4
    legal_score: float = 0.0        # G: 0–6

    critical_failure: bool = False
    critical_reason: Optional[str] = None

    empty_item_count: int = 0        # completely empty <ITEM></ITEM> elements (content deleted)
    orphan_tblcell_count: int = 0   # <TBLCELL> elements outside <TBLROW> (row wrappers deleted)

    issues: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # Useful diagnostics
    unknown_tags: list[str] = field(default_factory=list)
    invalid_entities: list[str] = field(default_factory=list)
    xml_parseable: bool = True


def _add_issue(result: L2Result, category: str, severity: str, description: str,
               location: str = "", impact: str = "") -> None:
    result.issues.append({
        "level": "L2",
        "category": category,
        "severity": severity,
        "description": description,
        "location": location,
        "impact": impact,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Category A: DTD / Schema Compliance
# ─────────────────────────────────────────────────────────────────────────────
def _check_schema(
    raw: str,
    result: L2Result,
    doc_class: Optional[DocumentClass] = None,
    line_index: Optional[list] = None,
) -> None:
    """8 pts: valid tags, required attrs, encoding, XML parseability."""
    score = 8.0
    struct = extract_structure(raw)

    # A1 — XML parseability (after entity preprocessing)
    # NOTE: SGML is NOT XML. Vendor SGML files legitimately contain constructs
    # that are invalid XML (e.g. bare '&' in entity references, non-self-closing
    # empty elements, etc.). XML parse failure does NOT indicate a real error —
    # the validator falls back to regex-based parsing which is sufficient.
    # Per gold-standard audit: 35/98 (37%) of correct vendor files fail XML parse.
    # → Log as informational only. Zero score deduction.
    _, parse_errors = parse_sgml(raw)
    if parse_errors:
        result.xml_parseable = False
        result.warnings.append(
            f"[INFO] Document uses SGML constructs not valid in XML (expected). "
            f"Regex fallback active. Error: {parse_errors[0][:120]}"
        )

    # A2 — All tags in whitelist
    used_tags = struct.tags_used
    invalid = get_invalid_tags(used_tags)
    if invalid:
        result.unknown_tags = sorted(invalid)
        pts = min(4.0, len(invalid) * 1.0)
        score -= pts
        # NOTE: Unknown tags are a major structural issue but NOT fatal.
        # Vendor SGML may legitimately use tags from newer DTD versions (e.g.
        # <DIV>) that are valid SGML but not yet in the v4.7 whitelist. A
        # document can still be 99% correct with one extra tag. The score
        # deduction (-1 to -4 pts) is the appropriate signal. Setting
        # critical_failure=True forces REJECT regardless of overall score —
        # which is too aggressive for a single structural tag difference.
        # Per gold-standard audit: DIV is used in at least one correct vendor
        # CIRO instrument (93-101) and should not trigger REJECT.
        _add_issue(result, "dtd_schema", "major",
                   f"Unknown tags (not in Carswell DTD v4.7 whitelist): {sorted(invalid)}",
                   impact=f"-{pts:.0f} pts")

    # A3 — POLIDOC required attributes
    # TSX By-Laws/Forms use MISCLAW/LEGIDDOC root (not POLIDOC) — skip check
    attrs = struct.get_polidoc_attrs()
    if not attrs:
        if doc_class and doc_class.is_tsx_special:
            pass  # MISCLAW structure is valid for TSX By-Laws/Forms (0 pts deducted)
        else:
            score -= 3.0
            _add_issue(result, "dtd_schema", "critical",
                       "Missing <POLIDOC> root element or no attributes found.", impact="-3 pts")
    else:
        missing_attrs = REQUIRED_POLIDOC_ATTRS - set(attrs.keys())
        if missing_attrs:
            score -= len(missing_attrs) * 0.5
            loc = find_tag_line("POLIDOC", raw, line_index) if line_index else ""
            _add_issue(result, "dtd_schema", "major",
                       f"POLIDOC missing required attributes: {missing_attrs}",
                       location=loc,
                       impact=f"-{len(missing_attrs)*0.5:.1f} pts")

        lang = attrs.get("LANG", "")
        if lang and lang not in VALID_LANG:
            score -= 1.0
            loc = find_tag_line("POLIDOC", raw, line_index) if line_index else ""
            _add_issue(result, "dtd_schema", "major",
                       f"POLIDOC LANG='{lang}' invalid. Must be EN or FR.",
                       location=loc, impact="-1 pt")

        adddate = attrs.get("ADDDATE", "")
        if adddate and not YYYYMMDD_RE.match(adddate):
            score -= 0.5
            loc = find_tag_line("POLIDOC", raw, line_index) if line_index else ""
            _add_issue(result, "dtd_schema", "minor",
                       f"POLIDOC ADDDATE='{adddate}' not in YYYYMMDD format.",
                       location=loc, impact="-0.5 pts")

        # B13 — MODDATE attribute (if present) must be YYYYMMDD
        moddate = attrs.get("MODDATE", "")
        if moddate and not YYYYMMDD_RE.match(moddate):
            score -= 0.5
            loc = find_tag_line("POLIDOC", raw, line_index) if line_index else ""
            _add_issue(result, "dtd_schema", "minor",
                       f"POLIDOC MODDATE='{moddate}' not in YYYYMMDD format.",
                       location=loc, impact="-0.5 pts")

        # B7 — POLIDOC LABEL must not be an empty string
        label_val = attrs.get("LABEL", None)
        if label_val is not None and not label_val.strip():
            score -= 0.5
            loc = find_tag_line("POLIDOC", raw, line_index) if line_index else ""
            _add_issue(result, "dtd_schema", "minor",
                       "POLIDOC LABEL attribute is present but empty. "
                       "Spec: do not leave the LABEL attribute empty.",
                       location=loc, impact="-0.5 pts")

    # B15 — CONTAINR must have LABEL attribute (Carswell DTD: #REQUIRED)
    containr_tags = list(re.finditer(r"<CONTAINR([^>]*)>", raw))
    for i, ctm in enumerate(containr_tags):
        if not re.search(r"\bLABEL=", ctm.group(1)):
            score -= 0.5
            loc = loc_from_match(ctm, line_index) if line_index else ""
            _add_issue(result, "dtd_schema", "minor",
                       f"CONTAINR #{i + 1} missing required LABEL attribute. "
                       f"Per Carswell DTD, CONTAINR LABEL is #REQUIRED.",
                       location=loc,
                       impact="-0.5 pts")  # no cap — report every occurrence

    result.schema_score = max(0.0, score)


# ─────────────────────────────────────────────────────────────────────────────
# Category B: Tag Nesting Rules
# ─────────────────────────────────────────────────────────────────────────────
def _check_nesting(raw: str, result: L2Result, line_index: Optional[list] = None) -> None:
    """8 pts: BLOCK/PART hierarchies, forbidden parent-child pairs."""
    score = 8.0
    struct = extract_structure(raw)

    # B1 — Forbidden parent-child pairs (from corpus: P in TBLCELL, FOOTNOTE in TABLE)
    for parent, forbidden_child in FORBIDDEN_PARENT_CHILD:
        parent_blocks = struct.find_all_blocks(parent)
        occurrence_count = 0
        first_loc = ""
        for block in parent_blocks:
            if re.search(rf"<{forbidden_child}[\s>]", block):
                occurrence_count += 1
                if not first_loc and line_index:
                    # Locate first occurrence for the HITL line reference
                    m_loc = re.search(rf"<{forbidden_child}[\s>]", block)
                    if m_loc:
                        # Approximate absolute position using raw search
                        abs_m = re.search(
                            rf"<{parent}[\s>]", raw
                        )
                        if abs_m:
                            first_loc = loc_from_match(abs_m, line_index)
        if occurrence_count > 0:
            score -= 1.5
            suffix = f" ({occurrence_count} occurrence(s) — fix all)" if occurrence_count > 1 else ""
            _add_issue(result, "tag_nesting", "critical",
                       f"<{forbidden_child}> found inside <{parent}>{suffix}. "
                       f"This is forbidden by keying spec.",
                       location=first_loc,
                       impact="-1.5 pts")

    # B2 — BLOCK hierarchy: detect reverse nesting AND level-skipping (B1)
    # Reverse nesting: BLOCK1 inside BLOCK2 is invalid.
    # Level-skip (B1): BLOCK2 → BLOCK4 with no BLOCK3 in between is invalid.
    issues_found: set[str] = set()
    stack: list[str] = []
    for m in re.finditer(r"<(/?)([A-Z][A-Z0-9]*)", raw):
        closing = m.group(1) == "/"
        tag = m.group(2)
        if closing:
            if stack and stack[-1] == tag:
                stack.pop()
        else:
            if tag.startswith("BLOCK") and tag[5:].isdigit():
                this_level = int(tag[5:])
                # Find nearest BLOCK ancestor (used for level-skip detection)
                nearest_block = next(
                    (a for a in reversed(stack)
                     if a.startswith("BLOCK") and a[5:].isdigit()),
                    None,
                )
                # B2: reverse nesting check
                # NOTE: Per gold-standard audit, 29% of correct vendor files have
                # BLOCK number mismatches (e.g. BLOCK2 inside BLOCK3). This occurs
                # in legal docs where Schedules/Appendices reset heading levels, or
                # where parallel sections use non-sequential block numbering.
                # Reduce penalty: minor with a small per-doc cap rather than per-occurrence.
                for ancestor in stack:
                    if ancestor.startswith("BLOCK") and ancestor[5:].isdigit():
                        anc_level = int(ancestor[5:])
                        if this_level < anc_level:
                            key = f"{tag}_in_{ancestor}"
                            if key not in issues_found:
                                issues_found.add(key)
                                score -= 0.25
                                loc = loc_from_match(m, line_index) if line_index else ""
                                path = find_tag_path(tag, raw)
                                loc_detail = f"{loc}  path: {path}" if path else loc
                                _add_issue(result, "tag_nesting", "minor",
                                           f"<{tag}> (level {this_level}) nested inside "
                                           f"<{ancestor}> (level {anc_level}). "
                                           f"Lower-level BLOCK inside higher-level (check schedule/appendix reset).",
                                           location=loc_detail,
                                           impact="-0.25 pt")
                # B1: level-skip detection
                if nearest_block is not None:
                    parent_level = int(nearest_block[5:])
                    if this_level > parent_level + 1:
                        key = f"skip_{nearest_block}_to_{tag}"
                        if key not in issues_found:
                            issues_found.add(key)
                            score -= 1.0
                            loc = loc_from_match(m, line_index) if line_index else ""
                            _add_issue(result, "tag_nesting", "major",
                                       f"BLOCK level skip: <{nearest_block}> jumps directly to "
                                       f"<{tag}> (skipped level {parent_level + 1}). "
                                       f"Spec: never skip BLOCK levels.",
                                       location=loc,
                                       impact="-1.0 pt")
            stack.append(tag)

    # B3 — ITEM should appear inside P1/P2/LINE/DEFP, not bare in P
    # Check: ITEM inside P at root level (not inside P1/P2)
    bare_item = re.search(r"<P[^1-4>][^>]*>[^<]*<ITEM", raw)
    if bare_item:
        score -= 1.0
        _add_issue(result, "tag_nesting", "minor",
                   "<ITEM> found directly inside <P>. Should be inside <P1> or <P2>.",
                   impact="-1.0 pt")

    # B4 — SGMLTBL should always be inside TABLE (never standalone)
    raw_stripped_tables = re.sub(r"<TABLE[^>]*>.*?</TABLE>", "", raw, flags=re.DOTALL)
    if "<SGMLTBL" in raw_stripped_tables:
        score -= 2.0
        result.critical_failure = True
        result.critical_reason = "SGMLTBL found outside TABLE wrapper"
        _add_issue(result, "tag_nesting", "critical",
                   "<SGMLTBL> found outside of <TABLE> wrapper. "
                   "SGMLTBL must always be wrapped in TABLE.",
                   impact="-2.0 pts")

    # B8 — ITEM must contain P (spec: use <ITEM><P> for list items)
    item_blocks = re.findall(r"<ITEM[^>]*>(.*?)</ITEM>", raw, re.DOTALL)
    # Distinguish: completely empty ITEM (content deleted) vs ITEM with bare text (structure error)
    empty_items = sum(1 for ib in item_blocks if not re.search(r"\S", ib))
    bare_items  = sum(1 for ib in item_blocks if re.search(r"\S", ib) and not re.search(r"<P[\s>]", ib))
    if empty_items:
        # Completely empty ITEM — content was deleted, not just a tagging style issue.
        # Treat as major: -1.0 pt per empty ITEM (cap at 2.0 pts).
        result.empty_item_count = empty_items
        pts = min(2.0, empty_items * 1.0)
        score -= pts
        # Find line number of first empty ITEM so the HITL card shows a location
        first_empty_loc = ""
        if line_index:
            for _em in re.finditer(r"<ITEM[^>]*>([\s\S]*?)</ITEM>", raw):
                if not re.search(r"\S", _em.group(1)):
                    first_empty_loc = loc_from_match(_em, line_index)
                    break
        _add_issue(result, "tag_nesting", "major",
                   f"{empty_items} <ITEM> element(s) are completely empty (no content). "
                   f"List item content appears to have been deleted.",
                   location=first_empty_loc,
                   impact=f"-{pts:.1f} pt{'s' if pts != 1.0 else ''}")
    if bare_items:
        pts = min(1.0, bare_items * 0.5)
        score -= pts
        _add_issue(result, "tag_nesting", "minor",
                   f"{bare_items} <ITEM> element(s) do not contain <P>. "
                   f"Spec: use <ITEM><P> for list items (not bare text inside ITEM).",
                   impact=f"-{pts:.1f} pt")

    # B9 — Orphaned </P> closing tag with no matching opener.
    # When a <P> (or <P><BOLD>AND WHEREAS</BOLD>) opening tag is deleted, the
    # paragraph body becomes bare text followed by a </P> with no matching <P>.
    # Strategy: find every </P> and check whether the intervening text since the
    # previous <P> opener contains substantive bare text with no <P> wrapper.
    # Specifically: text between </P> and the next </P> that has no <P> inside it
    # and has >=6 meaningful words is an orphaned paragraph body.
    # Exclude FOOTNOTE bodies (multi-line footnote text is valid bare inside FREEFORM).
    _raw_no_fn_b9 = re.sub(r"<FOOTNOTE[^>]*>.*?</FOOTNOTE>", "", raw, flags=re.DOTALL)
    _orphan_hits: list[re.Match] = []
    for _om in re.finditer(r"</P>([\s\S]{1,600}?)</P>", _raw_no_fn_b9):
        _between = _om.group(1)
        # If there is a <P opener between the two </P> tags, this is a normal sequence
        if re.search(r"<P[\s>1-4]", _between):
            continue
        # Strip tag markup and count meaningful words
        _text_only = re.sub(r"<[^>]+>", "", _between)
        _words = [w for w in _text_only.split() if re.search(r"[a-zA-Z]{3,}", w)]
        if len(_words) >= 6:
            _orphan_hits.append(_om)

    if _orphan_hits:
        pts = min(2.0, len(_orphan_hits) * 1.0)
        score -= pts
        first_loc = loc_from_match(_orphan_hits[0], line_index) if line_index else ""
        _add_issue(result, "tag_nesting", "major",
                   f"{len(_orphan_hits)} paragraph(s) have a </P> closing tag with no "
                   f"matching <P> opener — the opening <P> tag appears to have been deleted.",
                   location=first_loc,
                   impact=f"-{pts:.1f} pt{'s' if pts != 1.0 else ''}")

    result.nesting_score = max(0.0, score)


# ─────────────────────────────────────────────────────────────────────────────
# Category C: Entity Handling
# ─────────────────────────────────────────────────────────────────────────────
def _check_entities(raw: str, result: L2Result, line_index: Optional[list] = None) -> None:
    """6 pts: valid entities, no bare & < >."""
    score = 6.0

    # C1 — Check all entity references are valid Carswell entities
    entities_used = re.findall(r"&([a-zA-Z][a-zA-Z0-9-]*);", raw)
    invalid_ents: list[str] = []
    for ent in set(entities_used):
        if ent not in VALID_ENTITIES:
            invalid_ents.append(f"&{ent};")

    if invalid_ents:
        result.invalid_entities = invalid_ents
        pts = min(3.0, len(invalid_ents) * 0.5)
        score -= pts
        _add_issue(result, "entity_handling", "major",
                   f"Unknown entities (not in CARSWELL.ENT): {invalid_ents[:5]}",
                   impact=f"-{pts:.1f} pts")

    # C2 — Bare ampersands (not part of entity ref) — only in text content
    # Strip entity refs and tag attrs first, then look for lone &
    content_only = re.sub(r"&[#a-zA-Z][a-zA-Z0-9-]*;", "", raw)
    content_only = re.sub(r"<[^>]+>", "", content_only)
    bare_amps = re.findall(r"&(?!\s)", content_only)
    if bare_amps:
        pts = min(2.0, len(bare_amps) * 0.5)
        score -= pts
        _add_issue(result, "entity_handling", "major",
                   f"Found {len(bare_amps)} bare '&' characters not part of entity references. "
                   f"Must be encoded as &amp;",
                   impact=f"-{pts:.1f} pts")

    # C3 — Bare < or > in text content (excluding tags themselves)
    # Strip all tags (uppercase), processing instructions, and DTD declarations
    stripped_tags = re.sub(r"</?[A-Z][^>]*>|<\?[^>]*>|<![^>]*>", "", raw)
    bare_lt = len(re.findall(r"<(?!\s*>)", stripped_tags))
    if bare_lt > 0:
        pts = min(1.0, bare_lt * 0.25)
        score -= pts
        # Find line numbers of first few occurrences for keyer guidance
        if line_index:
            lt_lines = []
            # Match any < not followed by uppercase letter, /, !, ?, or whitespace+>
            for bm in re.finditer(r"<(?![A-Z/!?\s])", raw):
                ln = loc_from_match(bm, line_index)
                if ln not in lt_lines:
                    lt_lines.append(ln)
                if len(lt_lines) >= 5:
                    break
            # If that found nothing, try broader match (e.g. bare < followed by space)
            if not lt_lines:
                for bm in re.finditer(r"<(?:[^A-Z/!?][^>]*|\s)", raw):
                    ln = loc_from_match(bm, line_index)
                    if ln not in lt_lines:
                        lt_lines.append(ln)
                    if len(lt_lines) >= 5:
                        break
            loc = "; ".join(lt_lines) if lt_lines else ""
        else:
            loc = ""
        _add_issue(result, "entity_handling", "minor",
                   f"Found {bare_lt} bare '<' characters in content. Should be &lt;",
                   location=loc,
                   impact=f"-{pts:.1f} pts")

    # B12 — Bare > in text content should be encoded as &gt;
    bare_gt = len(re.findall(r">", stripped_tags))
    if bare_gt > 0:
        pts = min(0.5, bare_gt * 0.1)
        score -= pts
        _add_issue(result, "entity_handling", "minor",
                   f"Found {bare_gt} bare '>' character(s) in content. Should be &gt;",
                   impact=f"-{pts:.1f} pts")

    result.entity_score = max(0.0, score)


# ─────────────────────────────────────────────────────────────────────────────
# Category D: Table Structure
# ─────────────────────────────────────────────────────────────────────────────
def _check_tables(raw: str, result: L2Result, line_index: Optional[list] = None) -> None:
    """4 pts: TABLE/SGMLTBL pairing, TBLBODY, no P in TBLCELL."""
    score = 4.0

    if "<TABLE" not in raw and "<SGMLTBL" not in raw:
        result.table_score = 4.0  # No tables → full marks
        return

    # D1 — Every TABLE must contain SGMLTBL
    table_blocks = re.findall(r"<TABLE[^>]*>(.*?)</TABLE>", raw, re.DOTALL)
    for i, tb in enumerate(table_blocks):
        if "<SGMLTBL" not in tb:
            score -= 1.0
            _add_issue(result, "table_structure", "major",
                       f"TABLE #{i+1} does not contain <SGMLTBL>. "
                       f"Every TABLE must wrap SGMLTBL.",
                       impact="-1.0 pt")
            break

    # D2 — P in TBLCELL is forbidden (confirmed: 0/98 vendor files have it)
    tblcell_blocks = re.findall(r"<TBLCELL[^>]*>(.*?)</TBLCELL>", raw, re.DOTALL)
    p_in_cell_count = 0
    for cell in tblcell_blocks:
        if re.search(r"<P[\s>]", cell):
            p_in_cell_count += 1
    if p_in_cell_count:
        score -= 1.5
        result.critical_failure = True
        result.critical_reason = f"<P> found inside <TBLCELL> ({p_in_cell_count} occurrences)"
        _add_issue(result, "table_structure", "critical",
                   f"<P> found inside <TBLCELL> in {p_in_cell_count} cell(s). "
                   f"Forbidden by keying spec. Confirmed: 0/98 vendor files use this.",
                   impact="-1.5 pts")

    # D3 — SGMLTBL must contain TBLBODY
    sgmltbl_blocks = re.findall(r"<SGMLTBL[^>]*>(.*?)</SGMLTBL>", raw, re.DOTALL)
    for i, sb in enumerate(sgmltbl_blocks):
        if "<TBLBODY" not in sb:
            score -= 0.5
            _add_issue(result, "table_structure", "minor",
                       f"SGMLTBL #{i+1} missing <TBLBODY>.",
                       impact="-0.5 pts")
            if i >= 2:  # Don't flood issues for large tables
                break

    # D4 — All TBLROW elements in each SGMLTBL must have the same number of TBLCELLs
    # NOTE: Tables with merged cells (colspan) produce varying cell counts per row.
    # This is valid SGML table encoding. Per gold-standard audit, 34% of correct
    # vendor files have this pattern. Reduce to minor with small deduction.
    for j, sb in enumerate(sgmltbl_blocks):
        rows = re.findall(r"<TBLROW[^>]*>(.*?)</TBLROW>", sb, re.DOTALL)
        if len(rows) >= 2:
            cell_counts = [len(re.findall(r"<TBLCELL", row)) for row in rows]
            if len(set(cell_counts)) > 1:
                score -= 0.25
                _add_issue(result, "table_structure", "minor",
                           f"SGMLTBL #{j+1} has inconsistent cell counts per row: "
                           f"{cell_counts[:8]}{'...' if len(cell_counts) > 8 else ''}. "
                           f"Likely merged cells (colspan). Verify table layout.",
                           impact="-0.25 pt")

    # D5 — Empty TBLCELL — informational only, no score deduction
    # Vendor SGML practice: empty cells are left blank without &nbsp; in many
    # correct vendor files (31% of gold-standard files do this). While the spec
    # recommends &nbsp;, it is not strictly enforced and should not reduce scoring.
    all_cells = re.findall(r"<TBLCELL[^>]*>(.*?)</TBLCELL>", raw, re.DOTALL)
    empty_cell_count = sum(1 for c in all_cells if not c.strip())
    if empty_cell_count:
        result.warnings.append(
            f"[INFO] {empty_cell_count} empty <TBLCELL> element(s) found. "
            f"Spec recommends &nbsp; for empty cells but this is not penalised."
        )

    # D6 — TBLCELL must appear inside TBLROW (orphan TBLCELL = deleted TBLROW wrapper)
    # Strategy: strip all well-formed TBLROW blocks; any remaining TBLCELL is orphaned.
    total_orphan = 0
    for j, sb in enumerate(sgmltbl_blocks):
        stripped = re.sub(r"<TBLROW[^>]*>.*?</TBLROW>", "", sb, flags=re.DOTALL)
        orphan_cells = len(re.findall(r"<TBLCELL[^>]*>", stripped))
        if orphan_cells:
            total_orphan += orphan_cells
            pts = min(2.0, orphan_cells * 0.5)
            score -= pts
            _add_issue(result, "table_structure", "major",
                       f"SGMLTBL #{j+1}: {orphan_cells} <TBLCELL> element(s) found outside "
                       f"<TBLROW>. Table row wrappers appear to have been deleted.",
                       impact=f"-{pts:.1f} pts")
    if total_orphan:
        result.orphan_tblcell_count = total_orphan

    result.table_score = max(0.0, score)


# ─────────────────────────────────────────────────────────────────────────────
# Category E: Graphics / Images
# ─────────────────────────────────────────────────────────────────────────────
def _check_graphics(raw: str, result: L2Result, line_index: Optional[list] = None) -> None:
    """4 pts: GRAPHIC FILENAME attr, SBxxxxxx.BMP format."""
    score = 4.0

    graphic_tags = list(re.finditer(r"<GRAPHIC([^>]*)>", raw))

    if not graphic_tags:
        result.graphics_score = 4.0  # No graphics → full marks
        return

    for i, gm in enumerate(graphic_tags):
        attrs = gm.group(1)
        loc = loc_from_match(gm, line_index) if line_index else ""
        # E1 — FILENAME attribute required
        fn_match = re.search(r'FILENAME="([^"]+)"', attrs)
        if not fn_match:
            score -= 1.0
            _add_issue(result, "graphics", "major",
                       f"GRAPHIC #{i+1} missing FILENAME attribute.",
                       location=loc,
                       impact="-1.0 pt")
            continue

        filename = fn_match.group(1)

        # E2 — BMP filename format: SBxxxxxx.BMP (SB + 6 digits)
        if not BMP_FILENAME_RE.match(filename):
            score -= 0.5
            _add_issue(result, "graphics", "minor",
                       f"GRAPHIC FILENAME='{filename}' does not match expected format "
                       f"SBxxxxxx.BMP (e.g., SB000001.BMP). "
                       f"Confirmed correct format from vendor corpus.",
                       location=loc,
                       impact="-0.5 pts")

    # E3 — GRAPHIC filenames must be unique within the file
    seen_fns: set[str] = set()
    duplicate_fns: list[str] = []
    for gm in graphic_tags:
        fn_m = re.search(r'FILENAME="([^"]+)"', gm.group(1))
        if fn_m:
            fn_upper = fn_m.group(1).upper()
            if fn_upper in seen_fns:
                duplicate_fns.append(fn_m.group(1))
            seen_fns.add(fn_upper)
    if duplicate_fns:
        score -= 1.0
        _add_issue(result, "graphics", "major",
                   f"Duplicate GRAPHIC FILENAME(s) within file: {duplicate_fns[:5]}. "
                   f"Spec: BMP filename must be unique across each file. "
                   f"Duplicate files overwrite each other at ingest.",
                   impact="-1.0 pt")

    # E4 — Each GRAPHIC must be directly preceded by <P>
    unwrapped_graphics = 0
    for gm in graphic_tags:
        start = gm.start()
        preceding = raw[max(0, start - 100):start].rstrip()
        if not re.search(r"<P(?:[^>]*)?>$", preceding):
            unwrapped_graphics += 1
    if unwrapped_graphics:
        pts = min(1.0, unwrapped_graphics * 0.5)
        score -= pts
        _add_issue(result, "graphics", "minor",
                   f"{unwrapped_graphics} <GRAPHIC> element(s) not directly preceded by <P>. "
                   "Spec: surround each <GRAPHIC> with <P> "
                   "(e.g. <P><GRAPHIC FILENAME=\"...\"></P>).",
                   impact=f"-{pts:.1f} pts")

    # E5 — GRAPHIC filename sequence: large gaps suggest missing BMP files (B14)
    sb_nums = []
    for gm in graphic_tags:
        fn_m = re.search(r'FILENAME="SB(\d{6})\.BMP"', gm.group(1), re.IGNORECASE)
        if fn_m:
            sb_nums.append(int(fn_m.group(1)))
    if len(sb_nums) >= 2:
        sb_sorted = sorted(sb_nums)
        large_gaps = [
            (sb_sorted[i], sb_sorted[i + 1])
            for i in range(len(sb_sorted) - 1)
            if sb_sorted[i + 1] - sb_sorted[i] > 100
        ]
        if large_gaps:
            pts = min(0.5, len(large_gaps) * 0.1)
            score -= pts
            _add_issue(result, "graphics", "minor",
                       f"GRAPHIC filename sequence has {len(large_gaps)} large gap(s) > 100 "
                       f"(e.g. SB{large_gaps[0][0]:06d} → SB{large_gaps[0][1]:06d}). "
                       f"May indicate missing BMP files.",
                       impact=f"-{pts:.1f} pts")

    result.graphics_score = max(0.0, score)


# ─────────────────────────────────────────────────────────────────────────────
# Category F: Content Rules
# ─────────────────────────────────────────────────────────────────────────────
def _check_content_rules(raw: str, result: L2Result, line_index: Optional[list] = None) -> None:
    """4 pts: URLs in LINE, empty tags, no empty N/TI (except EDITNOTE)."""
    score = 4.0

    # F1 — Empty <N></N> is forbidden
    empty_n_matches = list(re.finditer(r"<N>\s*</N>", raw))
    if empty_n_matches:
        pts = min(1.5, len(empty_n_matches) * 0.5)
        score -= pts
        if line_index:
            lines = [loc_from_match(m, line_index) for m in empty_n_matches[:5]]
            loc = "; ".join(lines)
        else:
            loc = ""
        _add_issue(result, "content_rules", "major",
                   f"Found {len(empty_n_matches)} empty <N></N> tag(s). "
                   f"Empty N tags are forbidden.",
                   location=loc,
                   impact=f"-{pts:.1f} pts")

    # F2 — Empty <TI></TI> is forbidden UNLESS inside EDITNOTE
    raw_no_editnote = re.sub(
        r"<EDITNOTE[^>]*>.*?</EDITNOTE>", "", raw, flags=re.DOTALL
    )
    empty_ti_matches = list(re.finditer(r"<TI>\s*</TI>", raw_no_editnote))
    if empty_ti_matches:
        pts = min(1.0, len(empty_ti_matches) * 0.5)
        score -= pts
        if line_index:
            lines = [loc_from_match(m, line_index) for m in empty_ti_matches[:5]]
            loc = "; ".join(lines)
        else:
            loc = ""
        _add_issue(result, "content_rules", "major",
                   f"Found {len(empty_ti_matches)} empty <TI></TI> tag(s) outside EDITNOTE. "
                   f"Empty TI tags are forbidden (exception: inside EDITNOTE).",
                   location=loc,
                   impact=f"-{pts:.1f} pts")

    # F3 — URLs should be in LINE tags, not bare in P
    # Heuristic: look for http/https in P content but not wrapped in EM or LINE
    p_blocks = re.findall(r"<P[^1-4>][^>]*>(.*?)</P>", raw, re.DOTALL)
    bare_url_count = 0
    for pb in p_blocks:
        # Remove LINE and EM wrappers (these are correct placements)
        stripped = re.sub(r"<(LINE|EM)[^>]*>.*?</(LINE|EM)>", "", pb, flags=re.DOTALL)
        if re.search(r"https?://\S+", stripped):
            bare_url_count += 1
    if bare_url_count:
        score -= min(1.0, bare_url_count * 0.25)
        _add_issue(result, "content_rules", "minor",
                   f"Found {bare_url_count} URL(s) in <P> not wrapped in <LINE> or <EM>. "
                   f"URLs should use <LINE> wrapper.",
                   impact=f"-{min(1.0, bare_url_count*0.25):.1f} pts")

    # F4 — TI should not end with a period
    ti_matches = list(re.finditer(r"<TI[^>]*>(.*?)</TI>", raw, re.DOTALL))
    ti_with_period = 0
    ti_period_lines = []
    for tim in ti_matches:
        text = re.sub(r"<[^>]+>", "", tim.group(1)).strip()
        if text.endswith("."):
            ti_with_period += 1
            if line_index and len(ti_period_lines) < 5:
                ti_period_lines.append(loc_from_match(tim, line_index))
    if ti_with_period > 0:
        score -= min(0.5, ti_with_period * 0.1)
        loc = "; ".join(ti_period_lines) if ti_period_lines else ""
        _add_issue(result, "content_rules", "minor",
                   f"{ti_with_period} <TI> element(s) end with a period. "
                   f"Headings should not end with periods.",
                   location=loc,
                   impact="-0.5 pts")

    # F5 — TI must not have its entire content wrapped in EM
    whole_em_ti = re.findall(r"<TI>\s*<EM>[^<]*</EM>\s*</TI>", raw)
    if whole_em_ti:
        pts = min(0.5, len(whole_em_ti) * 0.25)
        score -= pts
        _add_issue(result, "content_rules", "minor",
                   f"{len(whole_em_ti)} <TI> element(s) have their entire content wrapped "
                   f"in <EM>. Spec: do not surround the entire contents of <TI> with "
                   f"<EM> tags. Use <EM> only around specific references (Act names etc.).",
                   impact=f"-{pts:.1f} pts")

    # F6 — ITEM content must not begin with a bullet character (B11)
    # Spec: use <ITEM> as the structural list marker; do not add bullet chars inside
    item_blocks_f6 = re.findall(r"<ITEM[^>]*>(.*?)</ITEM>", raw, re.DOTALL)
    bullet_item_count = 0
    for ib in item_blocks_f6:
        text_content = re.sub(r"<[^>]+>", "", ib).strip()
        if BULLET_CHARS_RE.match(text_content):
            bullet_item_count += 1
    if bullet_item_count:
        pts = min(0.5, bullet_item_count * 0.1)
        score -= pts
        _add_issue(result, "content_rules", "minor",
                   f"{bullet_item_count} <ITEM> element(s) begin with a bullet character "
                   f"(\u2022, *, -, \u2013, \u2014). Spec: use <ITEM> as the structural list "
                   f"marker \u2014 do not add leading bullet characters inside the tag content.",
                   impact=f"-{pts:.1f} pts")

    result.content_score = max(0.0, score)


# ─────────────────────────────────────────────────────────────────────────────
# Category G: Legal Document Rules
# ─────────────────────────────────────────────────────────────────────────────
def _check_legal_rules(
    raw: str,
    result: L2Result,
    doc_class: Optional[DocumentClass] = None,
    line_index: Optional[list] = None,
) -> None:
    """6 pts: POLIDENT N+TI, DEF->DEFP, STATREF, DATE LABEL."""
    score = 6.0
    struct = extract_structure(raw)

    # G1 — POLIDENT must have N and TI (warning if missing, -2 pts each)
    doc_n = struct.get_polident_n()
    doc_ti = struct.get_polident_ti()
    polident_present = bool(re.search(r"<POLIDENT", raw))

    if not polident_present:
        # TSX By-Laws and Forms legitimately have no POLIDENT — not a failure
        if doc_class and doc_class.is_tsx_special:
            score -= 1.0   # minor deduction only
            _add_issue(result, "legal_rules", "warning",
                       "TSX special document (By-Law/Form) has no <POLIDENT>. "
                       "Accepted for TSX document class.",
                       impact="-1 pt")
        else:
            score -= 3.0
            result.critical_failure = True
            result.critical_reason = "Missing <POLIDENT> element"
            _add_issue(result, "legal_rules", "critical",
                       "Document has no <POLIDENT> element. Required for all Carswell documents.",
                       impact="-3 pts")
    else:
        polident_loc = find_tag_line("POLIDENT", raw, line_index) if line_index else ""
        if not doc_n:
            score -= 1.0
            _add_issue(result, "legal_rules", "minor",
                       "POLIDENT missing <N> (document number). Recommended but not strictly required.",
                       location=polident_loc,
                       impact="-1 pt")
        if not doc_ti:
            score -= 1.0
            _add_issue(result, "legal_rules", "minor",
                       "POLIDENT missing <TI> (document title). Recommended but not strictly required.",
                       location=polident_loc,
                       impact="-1 pt")

    # G2 — DEF structure: DEF must contain DEFP (from corpus: DEF→DEFP→TERM)
    def_blocks = re.findall(r"<DEF[^>]*>(.*?)</DEF>", raw, re.DOTALL)
    defp_missing_count = 0
    for db in def_blocks:
        if "<DEFP" not in db:
            defp_missing_count += 1
    if defp_missing_count:
        pts = min(2.0, defp_missing_count * 0.5)
        score -= pts
        _add_issue(result, "legal_rules", "major",
                   f"{defp_missing_count} <DEF> element(s) missing <DEFP>. "
                   f"Correct structure: DEF → DEFP → TERM (not DEFTERM).",
                   impact=f"-{pts:.1f} pts")

    # G3 — DATE LABEL attribute should be a recognised value
    date_tags = re.findall(r"<DATE([^>]*)>", raw)
    bad_date_labels: list[str] = []
    for dt in date_tags:
        lm = re.search(r'LABEL="([^"]+)"', dt)
        if lm and lm.group(1) not in VALID_DATE_LABELS:
            bad_date_labels.append(lm.group(1))
    if bad_date_labels:
        score -= min(0.5, len(bad_date_labels) * 0.25)
        _add_issue(result, "legal_rules", "minor",
                   f"Unrecognised DATE LABEL value(s): {bad_date_labels[:3]}. "
                   f"Expected: {sorted(VALID_DATE_LABELS)}",
                   impact="-0.5 pts")

    # G4 — FREEFORM must be present
    if "<FREEFORM" not in raw:
        score -= 2.0
        _add_issue(result, "legal_rules", "critical",
                   "Document has no <FREEFORM> element. Required structure: POLIDOC > POLIDENT + FREEFORM.",
                   impact="-2 pts")

    # G5 — Multiple POLIDOCs must be in reverse chronological order (newest first)
    polidoc_addates = re.findall(r'<POLIDOC[^>]*\bADDDATE="(\d{8})"', raw)
    if len(polidoc_addates) >= 2:
        out_of_order = [
            (polidoc_addates[i], polidoc_addates[i + 1])
            for i in range(len(polidoc_addates) - 1)
            if polidoc_addates[i] < polidoc_addates[i + 1]
        ]
        if out_of_order:
            pts = min(1.0, len(out_of_order) * 0.5)
            score -= pts
            _add_issue(result, "legal_rules", "minor",
                       f"POLIDOC elements not in reverse chronological order. "
                       f"Found ascending ADDDATE pairs: {out_of_order[:3]}. "
                       f"Spec: new data must appear at top of file (reverse chronological order).",
                       impact=f"-{pts:.1f} pts")

    result.legal_score = max(0.0, score)


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────
def validate_structure(
    raw_sgml: str,
    doc_class: Optional[DocumentClass] = None,
) -> L2Result:
    """
    Run all Level 2 structural checks on the raw SGML string.

    Parameters
    ----------
    raw_sgml : str
        Raw SGML content (as read from file).
    doc_class : DocumentClass, optional
        Pre-classification result from document_classifier.pre_classify().
        When provided, adjusts rules (e.g. TSX_SPECIAL skips POLIDENT check).

    Returns
    -------
    L2Result with score (0-40) and all issues found.
    """
    result = L2Result()

    if not raw_sgml.strip():
        result.critical_failure = True
        result.critical_reason = "Empty SGML content"
        result.score = 0.0
        return result

    # Build line index once — shared by all check functions for location tracking
    line_index = build_line_index(raw_sgml)

    _check_schema(raw_sgml, result, doc_class, line_index)
    _check_nesting(raw_sgml, result, line_index)
    _check_entities(raw_sgml, result, line_index)
    _check_tables(raw_sgml, result, line_index)
    _check_graphics(raw_sgml, result, line_index)
    _check_content_rules(raw_sgml, result, line_index)
    _check_legal_rules(raw_sgml, result, doc_class, line_index)

    result.score = (
        result.schema_score + result.nesting_score + result.entity_score +
        result.table_score + result.graphics_score +
        result.content_score + result.legal_score
    )

    # Enrich all issues with fix templates
    enrich_issues(result.issues)

    return result
