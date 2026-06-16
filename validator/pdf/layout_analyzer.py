"""
layout_analyzer.py — Multi-column detection, reading-order correction,
                     and page-break paragraph merging.

Design decisions:

COLUMN DETECTION
  We use a gap-based approach instead of k-means (no sklearn dependency,
  faster, and more robust for 2-column legal documents):
  - Project all block left-edges onto the X axis.
  - Find the largest horizontal gap in the middle 40-60% of the page width.
  - If that gap is > 15% of page width → two-column layout.
  - Three-column is detected by finding two gaps (rare in our corpus).
  - Per-page detection: some pages are single-column (title, TOC),
    others are two-column (body). We classify per page, not per document.

READING ORDER
  - Single-column: sort blocks by (y0) — top to bottom.
  - Two-column: assign each block to left/right column, then sort each
    column by y0, then concatenate left column + right column.
  - We use a 5pt tolerance for "same line" to handle slight y misalignments.

PAGE-BREAK MERGING
  Legal PDF paragraphs frequently split across pages. We merge if:
    1. Block A is the last block on page N
    2. Block B is the first block on page N+1
    3. Block A's text does NOT end with a sentence-terminal character
       (period, exclamation, question, colon, semicolon, closing bracket)
    4. Block B's text starts with a lowercase letter
    5. Block A and B have the same dominant font and approximately same size
    6. Neither block looks like a section heading (not all-caps, not bold heading)
  We do NOT use NLP tokenization (no nltk/spacy dependency).
"""
import re
import logging
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# Characters that definitively end a sentence / prevent merging
_SENTENCE_TERMINALS = re.compile(r'[.!?;:\]\)>""»]\s*$')

# Numbered list item that continues on next page — don't merge with previous
_LIST_ITEM_START = re.compile(r'^\s*(?:\d+[.)]\s|\([a-z]\)\s|[a-z][.)]\s|[-•–]\s)')

# All-caps heading pattern (very common in legal docs)
_ALL_CAPS_RE = re.compile(r'^[A-Z0-9\s\-–—:.,/()]+$')

# Heading indicators in text
_HEADING_KEYWORDS = re.compile(
    r'^\s*(?:Part|Section|Article|Schedule|Appendix|Chapter|Division|Annex)'
    r'\s+\d',
    re.IGNORECASE,
)

# Gap detection: fraction of page width to look for column gap
_GAP_SEARCH_MIN = 0.30   # don't look for gaps in left 30%
_GAP_SEARCH_MAX = 0.70   # don't look for gaps in right 30%
_MIN_GAP_FRAC   = 0.08   # gap must be ≥8% of page width to count as column split

# Y-tolerance (pts) for "same line" grouping
_Y_TOLERANCE = 5.0


def _block_left(block: Dict) -> float:
    return block["bbox"][0]


def _block_top(block: Dict) -> float:
    return block["bbox"][1]


def _block_right(block: Dict) -> float:
    return block["bbox"][2]


def _block_width(block: Dict) -> float:
    return block["bbox"][2] - block["bbox"][0]


def detect_column_layout(
    blocks: List[Dict],
    page_width: float,
) -> str:
    """
    Detect column layout for a single page.

    Returns: "single" | "two-column" | "three-column"
    """
    if not blocks or page_width <= 0:
        return "single"

    # Collect left-edge X coordinates of all substantial blocks (>10pt wide)
    lefts = sorted(
        {b["bbox"][0] for b in blocks if _block_width(b) > 10},
    )
    if len(lefts) < 3:
        return "single"

    # Search for gaps only in the middle zone of the page
    mid_min = page_width * _GAP_SEARCH_MIN
    mid_max = page_width * _GAP_SEARCH_MAX
    mid_lefts = [x for x in lefts if mid_min <= x <= mid_max]

    if not mid_lefts:
        return "single"

    # Find the largest gap between consecutive left-edges in the middle zone
    gaps: List[Tuple[float, float]] = []  # (gap_start_x, gap_size)
    prev = lefts[0]
    for x in lefts[1:]:
        if mid_min <= prev <= mid_max or mid_min <= x <= mid_max:
            gap = x - prev
            if gap >= page_width * _MIN_GAP_FRAC:
                gaps.append((prev, gap))
        prev = x

    if not gaps:
        return "single"

    gaps.sort(key=lambda g: g[1], reverse=True)

    if len(gaps) >= 2:
        return "three-column"
    return "two-column"


def _assign_column(block: Dict, split_x: float) -> int:
    """Return 0 for left column, 1 for right column."""
    block_center = (block["bbox"][0] + block["bbox"][2]) / 2
    return 0 if block_center < split_x else 1


def _find_column_split(blocks: List[Dict], page_width: float) -> float:
    """Find the X coordinate that splits a two-column page."""
    lefts = sorted({b["bbox"][0] for b in blocks if _block_width(b) > 10})
    best_gap = 0.0
    best_split = page_width / 2
    prev = lefts[0] if lefts else 0
    for x in lefts[1:]:
        gap = x - prev
        if gap > best_gap:
            best_gap = gap
            best_split = prev + gap / 2
        prev = x
    return best_split


def sort_blocks_by_reading_order(
    blocks: List[Dict],
    page_width: float,
) -> List[Dict]:
    """
    Sort blocks into correct reading order for the given page.

    Detects column layout automatically and handles:
    - Single column: simple top-to-bottom sort
    - Two-column: left column (top→bottom) then right column (top→bottom)
    - Three-column: three columns in order
    """
    if not blocks:
        return blocks

    layout = detect_column_layout(blocks, page_width)

    if layout == "single":
        return sorted(blocks, key=_block_top)

    if layout == "two-column":
        split_x = _find_column_split(blocks, page_width)
        left  = sorted([b for b in blocks if _assign_column(b, split_x) == 0], key=_block_top)
        right = sorted([b for b in blocks if _assign_column(b, split_x) == 1], key=_block_top)
        return left + right

    # three-column: find two splits
    lefts = sorted({b["bbox"][0] for b in blocks if _block_width(b) > 10})
    splits: List[float] = []
    prev = lefts[0] if lefts else 0
    gaps: List[Tuple[float, float]] = []
    for x in lefts[1:]:
        gaps.append((prev + (x - prev) / 2, x - prev))
        prev = x
    gaps.sort(key=lambda g: g[1], reverse=True)
    splits = sorted([g[0] for g in gaps[:2]])

    col0 = sorted([b for b in blocks if b["bbox"][0] < splits[0]], key=_block_top)
    col1 = sorted([b for b in blocks if splits[0] <= b["bbox"][0] < splits[1]], key=_block_top)
    col2 = sorted([b for b in blocks if b["bbox"][0] >= splits[1]], key=_block_top)
    return col0 + col1 + col2


def _looks_like_heading(block: Dict) -> bool:
    """
    Heuristic: return True if this block looks like a section heading
    that should not be merged with a previous paragraph.
    """
    text = block.get("text", "").strip()
    if not text:
        return False
    # Explicitly tagged as heading by formatting extractor
    if block.get("_heading", 0) > 0:
        return True
    # ALL CAPS text that's short (< 120 chars) = likely a heading
    if len(text) < 120 and _ALL_CAPS_RE.match(text):
        return True
    # Starts with "Part N" / "Section N" etc.
    if _HEADING_KEYWORDS.match(text):
        return True
    return False


def _looks_like_list_start(block: Dict) -> bool:
    text = block.get("text", "").strip()
    return bool(_LIST_ITEM_START.match(text))


def merge_page_breaks(blocks: List[Dict]) -> List[Dict]:
    """
    Merge paragraphs that continue across page boundaries.

    Works on a flat list of blocks already sorted in reading order
    (all pages, in page order). Blocks must have keys:
        'page' : int
        'text' : str
        'font' : str   (dominant font name)
        'size' : float (dominant font size)
        '_heading' : int (0 = body)

    Merged blocks get the page number of the first block.
    """
    if not blocks:
        return blocks

    merged: List[Dict] = []
    skip_next = False

    for i, block in enumerate(blocks):
        if skip_next:
            skip_next = False
            continue

        if i + 1 >= len(blocks):
            merged.append(block)
            continue

        next_block = blocks[i + 1]

        # Only consider merging consecutive pages
        if next_block.get("page", 0) != block.get("page", 0) + 1:
            merged.append(block)
            continue

        text_a = block.get("text", "").rstrip()
        text_b = next_block.get("text", "").lstrip()

        if not text_a or not text_b:
            merged.append(block)
            continue

        # Don't merge headings
        if _looks_like_heading(block) or _looks_like_heading(next_block):
            merged.append(block)
            continue

        # Don't merge into a list item start
        if _looks_like_list_start(next_block):
            merged.append(block)
            continue

        # Check sentence termination — if A ends with terminal, don't merge
        if _SENTENCE_TERMINALS.search(text_a):
            merged.append(block)
            continue

        # Next block must start with lowercase (continuation marker)
        if not text_b or not text_b[0].islower():
            merged.append(block)
            continue

        # Font/size must be approximately the same
        font_a = block.get("font", "")
        font_b = next_block.get("font", "")
        size_a = block.get("size", 0.0)
        size_b = next_block.get("size", 0.0)

        if font_a and font_b and font_a != font_b:
            merged.append(block)
            continue
        if abs(size_a - size_b) > 1.5:
            merged.append(block)
            continue

        # All checks passed — merge
        merged_block = dict(block)
        merged_block["text"] = text_a + " " + text_b
        # Keep the larger bbox union
        ba = block.get("bbox", (0, 0, 0, 0))
        bb = next_block.get("bbox", (0, 0, 0, 0))
        merged_block["bbox"] = (
            min(ba[0], bb[0]), min(ba[1], bb[1]),
            max(ba[2], bb[2]), max(ba[3], bb[3]),
        )
        log.debug(
            "Page-break merge: page %d → %d: '%.40s...'",
            block.get("page", 0),
            next_block.get("page", 0),
            text_a,
        )
        merged.append(merged_block)
        skip_next = True

    return merged
