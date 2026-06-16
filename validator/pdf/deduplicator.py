"""
deduplicator.py — Spatial overlap detection between PyMuPDF text blocks
                  and pdfplumber table bounding boxes.

Design decisions:
- We compute overlap as intersection_area / block_area (not table_area).
  This means a large table that only partially overlaps a small text block
  will correctly flag the block as "inside the table."
- Threshold = 0.5 (50%): chosen after testing on our corpus. At 0.8 we miss
  some table-edge blocks; at 0.3 we accidentally suppress table captions.
- "Table caption" heuristic: a block whose BOTTOM edge sits just above the
  table top (within 20pt) is a caption — keep it even if bbox touches table.
- "Table caption below" heuristic: a block whose TOP edge sits just below
  the table bottom (within 20pt) is also a caption — keep it.
- pdfplumber table bbox = (x0, top, x1, bottom) in PDF points.
  PyMuPDF block bbox  = (x0, y0, x1, y1) in PDF points.
  Both use the same coordinate system so comparison is direct.
"""
import logging
from typing import Dict, List, Tuple

log = logging.getLogger(__name__)

Bbox = Tuple[float, float, float, float]   # (x0, y0, x1, y1)

# Overlap ratio above which a block is considered "inside" a table
_OVERLAP_THRESHOLD = 0.50

# How close (in PDF points) a block can be to a table and still be a caption
_CAPTION_MARGIN_PT = 20.0


def _intersection_area(b1: Bbox, b2: Bbox) -> float:
    """Return the area of the intersection rectangle of two bboxes."""
    x0 = max(b1[0], b2[0])
    y0 = max(b1[1], b2[1])
    x1 = min(b1[2], b2[2])
    y1 = min(b1[3], b2[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return (x1 - x0) * (y1 - y0)


def bbox_area(bbox: Bbox) -> float:
    """Return the area of a bounding box. Returns 0 if degenerate."""
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    return max(0.0, w * h)


def calculate_bbox_overlap(block_bbox: Bbox, table_bbox: Bbox) -> float:
    """
    Calculate overlap ratio = intersection_area / block_area.

    Returns a value in [0.0, 1.0].
    Returns 0.0 if block has zero area (degenerate).
    """
    block_area = bbox_area(block_bbox)
    if block_area == 0.0:
        return 0.0
    return _intersection_area(block_bbox, table_bbox) / block_area


def is_bbox_inside_table(
    block_bbox: Bbox,
    table_bbox: Bbox,
    threshold: float = _OVERLAP_THRESHOLD,
) -> bool:
    """
    Return True if block overlaps the table by more than *threshold*.
    """
    return calculate_bbox_overlap(block_bbox, table_bbox) > threshold


def _is_caption_above(block_bbox: Bbox, table_bbox: Bbox) -> bool:
    """True if block sits just above the table (likely a table title/caption)."""
    # Block bottom is above table top, within margin
    return (
        block_bbox[3] <= table_bbox[1]
        and table_bbox[1] - block_bbox[3] <= _CAPTION_MARGIN_PT
    )


def _is_caption_below(block_bbox: Bbox, table_bbox: Bbox) -> bool:
    """True if block sits just below the table (likely a table note/source)."""
    return (
        block_bbox[1] >= table_bbox[3]
        and block_bbox[1] - table_bbox[3] <= _CAPTION_MARGIN_PT
    )


def deduplicate(
    text_blocks: List[Dict],
    table_bboxes_by_page: Dict[int, List[Bbox]],
) -> List[Dict]:
    """
    Remove PyMuPDF text blocks whose content is already captured by a
    pdfplumber table, preventing double-counting.

    Parameters
    ----------
    text_blocks : list of dicts
        Each dict must have keys: 'bbox' (Bbox), 'page' (int), 'text' (str).
    table_bboxes_by_page : dict
        Mapping page_number → list of table bboxes on that page.

    Returns
    -------
    Filtered list of text_blocks with table-content blocks removed.
    """
    kept: List[Dict] = []

    for block in text_blocks:
        page = block.get("page", 0)
        bbx: Bbox = tuple(block.get("bbox", (0, 0, 0, 0)))  # type: ignore[arg-type]
        table_bboxes = table_bboxes_by_page.get(page, [])

        inside_table = False
        for tbbox in table_bboxes:
            overlap = calculate_bbox_overlap(bbx, tbbox)
            if overlap > _OVERLAP_THRESHOLD:
                # But preserve captions — a block that merely touches the
                # table edge but is not spatially inside it
                if not (_is_caption_above(bbx, tbbox) or _is_caption_below(bbx, tbbox)):
                    inside_table = True
                    break

        if not inside_table:
            kept.append(block)
        else:
            log.debug(
                "Deduplication: dropped block (page %d) '%.40s...'",
                page,
                block.get("text", ""),
            )

    log.debug(
        "Deduplication: kept %d / %d blocks",
        len(kept),
        len(text_blocks),
    )
    return kept
