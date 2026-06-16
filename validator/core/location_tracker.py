"""
core/location_tracker.py
─────────────────────────
Utilities to resolve character offsets → line numbers and tag paths
for use in error location reporting.

Usage in structural_validator.py:
    from validator.core.location_tracker import (
        build_line_index, offset_to_line, find_tag_line,
        find_tag_path, extract_context_snippet,
    )

    line_index = build_line_index(raw)
    loc = find_tag_line("BLOCK3", raw, line_index)
    # → "Line 247"

    path = find_tag_path("BLOCK3", raw)
    # → "POLIDOC > FREEFORM > BLOCK2[3] > BLOCK3"

    ctx = extract_context_snippet(raw, line_no=247, context_lines=2)
    # → "<BLOCK2>\\n  <BLOCK3>..."
"""

import re
from typing import Optional


# ── Line index ────────────────────────────────────────────────────────────────

def build_line_index(text: str) -> list[int]:
    """
    Return a list of character offsets for the start of each line (0-indexed).

    line_index[0] = 0        (start of line 1)
    line_index[1] = n        (start of line 2)
    ...

    len(line_index) == total number of lines in text.
    """
    offsets = [0]
    for m in re.finditer(r"\n", text):
        offsets.append(m.end())
    return offsets


def offset_to_line(offset: int, line_index: list[int]) -> int:
    """
    Convert a character offset to a 1-based line number using binary search.
    """
    lo, hi = 0, len(line_index) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if line_index[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1  # 1-based


# ── Tag line finder ───────────────────────────────────────────────────────────

def find_tag_line(
    tag_name: str,
    raw: str,
    line_index: list[int],
    occurrence: int = 1,
) -> str:
    """
    Find the line number of the nth occurrence of <TAG_NAME ...> in raw SGML.

    Returns a string like "Line 247" or "" if not found.

    Parameters
    ----------
    tag_name   : SGML tag name (without < >), e.g. "BLOCK3"
    raw        : full SGML content string
    line_index : from build_line_index(raw)
    occurrence : which occurrence to return (1-based)
    """
    pattern = re.compile(rf"<{re.escape(tag_name)}[\s>/]", re.IGNORECASE)
    count = 0
    for m in pattern.finditer(raw):
        count += 1
        if count == occurrence:
            line_no = offset_to_line(m.start(), line_index)
            return f"Line {line_no}"
    return ""


def find_all_tag_lines(
    tag_name: str,
    raw: str,
    line_index: list[int],
    max_results: int = 10,
) -> list[int]:
    """
    Return line numbers (1-based ints) of all occurrences of <TAG_NAME>.
    Capped at max_results to avoid flooding.
    """
    pattern = re.compile(rf"<{re.escape(tag_name)}[\s>/]", re.IGNORECASE)
    lines = []
    for m in pattern.finditer(raw):
        lines.append(offset_to_line(m.start(), line_index))
        if len(lines) >= max_results:
            break
    return lines


def find_pattern_line(
    pattern: "re.Pattern",
    raw: str,
    line_index: list[int],
    occurrence: int = 1,
) -> str:
    """
    Find the line number of the nth match of a compiled regex pattern.
    Returns "Line N" or "".
    """
    count = 0
    for m in pattern.finditer(raw):
        count += 1
        if count == occurrence:
            return f"Line {offset_to_line(m.start(), line_index)}"
    return ""


# ── Tag path builder ──────────────────────────────────────────────────────────

def find_tag_path(
    target_tag: str,
    raw: str,
    occurrence: int = 1,
) -> str:
    """
    Walk the SGML tag stream to find the ancestor path at the point where
    target_tag first appears.

    Returns a string like:
        "POLIDOC > FREEFORM > BLOCK2[3] > BLOCK3"
    or "" if not found.

    Uses a simplified stack-based parser — counts sibling occurrences to
    produce indexed paths (e.g. BLOCK2[3] = 3rd BLOCK2 encountered).

    Limitations:
    - Self-closing or void tags are treated as open-only (no closing slash).
    - Tags with content-embedded < (after escaping) may confuse the parser.
      This is acceptable because the location tracker is a best-effort helper.
    """
    # Tags that are self-closing / void in Carswell SGML (no content, no </TAG>)
    VOID_TAGS = frozenset({
        "GRAPHIC", "NEWLINE", "DOTTAB", "HR", "BR",
    })

    stack: list[str] = []           # tag names currently open
    counts: dict[str, int] = {}     # total opens per tag (for indexing)
    found_count = 0

    for m in re.finditer(r"<(/?)([A-Z][A-Z0-9]*)([^>]*)>", raw, re.IGNORECASE):
        is_closing = bool(m.group(1))
        tag = m.group(2).upper()

        if is_closing:
            # Pop from stack
            for i in range(len(stack) - 1, -1, -1):
                if stack[i] == tag:
                    stack.pop(i)
                    break
        else:
            counts[tag] = counts.get(tag, 0) + 1
            if tag.upper() == target_tag.upper():
                found_count += 1
                if found_count == occurrence:
                    # Build path: each ancestor gets index if count > 1
                    path_parts = []
                    seen: dict[str, int] = {}
                    for ancestor in stack:
                        seen[ancestor] = seen.get(ancestor, 0) + 1
                    # Re-walk stack to produce indexed names
                    for ancestor in stack:
                        path_parts.append(ancestor)
                    # Append the target itself with its global occurrence index
                    idx = counts[tag]
                    path_parts.append(f"{tag}[{idx}]")
                    return " > ".join(path_parts)
            if tag not in VOID_TAGS:
                stack.append(tag)

    return ""


# ── Context snippet extractor ─────────────────────────────────────────────────

def extract_context_snippet(
    raw: str,
    line_no: int,
    context_lines: int = 2,
    max_chars_per_line: int = 120,
) -> str:
    """
    Extract `context_lines` lines before and after `line_no` from raw text.

    Returns a compact multi-line string suitable for embedding in an issue dict.
    Line numbers are shown as a prefix, e.g.:
        245: <BLOCK2>
        246:   <N>1.</N>
      → 247:   <BLOCK3>       ← error line (marked with →)
        248:     <TI>Overview</TI>
        249:   </BLOCK3>
    """
    lines = raw.splitlines()
    total = len(lines)
    start = max(0, line_no - 1 - context_lines)          # 0-based
    end   = min(total, line_no - 1 + context_lines + 1)  # 0-based exclusive
    parts = []
    for i in range(start, end):
        lineno_1 = i + 1
        prefix = "  → " if lineno_1 == line_no else "    "
        text = lines[i][:max_chars_per_line]
        parts.append(f"{prefix}{lineno_1:4d}: {text}")
    return "\n".join(parts)


# ── Convenience: location string from pattern match ──────────────────────────

def loc_from_match(m: "re.Match", line_index: list[int]) -> str:
    """Return 'Line N' string for a regex match object."""
    return f"Line {offset_to_line(m.start(), line_index)}"


def loc_from_offset(offset: int, line_index: list[int]) -> str:
    """Return 'Line N' string for a character offset."""
    return f"Line {offset_to_line(offset, line_index)}"
