"""
core/sgml_parser.py
────────────────────
Parse a Carswell SGML file into an xml.etree.ElementTree after entity
preprocessing.  Falls back to a regex-based structural extractor if the
XML parse fails (e.g., due to residual SGML quirks).

Usage:
    from validator.core.sgml_parser import parse_sgml, extract_structure
"""

import re
import xml.etree.ElementTree as ET
from typing import Optional

from validator.core.entity_preprocessor import preprocess_sgml


# ── Primary parser: xml.etree after entity cleanup ───────────────────────────
def parse_sgml(raw: str) -> tuple[Optional[ET.Element], list[str]]:
    """
    Parse SGML string to ElementTree root.

    Returns
    -------
    (root, errors)
        root   — ET.Element if parse succeeded, None otherwise
        errors — list of parse error messages
    """
    errors: list[str] = []
    clean = preprocess_sgml(raw)

    try:
        root = ET.fromstring(clean)
        return root, errors
    except ET.ParseError as e:
        errors.append(f"XML parse error: {e}")

    # Second attempt: wrap in synthetic root if the file is a fragment
    try:
        root = ET.fromstring(f"<_root_>{clean}</_root_>")
        # Return the actual POLIDOC child if present
        polidoc = root.find("POLIDOC")
        if polidoc is not None:
            return polidoc, errors
        return root, errors
    except ET.ParseError as e2:
        errors.append(f"XML parse error (wrapped): {e2}")

    return None, errors


# ── Structural extractor: regex-based fallback ────────────────────────────────
class SGMLStructure:
    """
    Lightweight structural representation extracted via regex when
    full XML parsing fails.
    """

    def __init__(self, raw: str):
        self.raw = raw
        self._tags_used: Optional[set[str]] = None
        self._tag_locations: Optional[dict[str, list[int]]] = None

    @property
    def tags_used(self) -> set[str]:
        if self._tags_used is None:
            self._tags_used = set(re.findall(r"<([A-Z][A-Z0-9]*)", self.raw))
        return self._tags_used

    def get_tag_locations(self) -> dict[str, list[int]]:
        """Map tag name → list of line numbers where it appears."""
        if self._tag_locations is None:
            lines = self.raw.splitlines()
            result: dict[str, list[int]] = {}
            for i, line in enumerate(lines, start=1):
                for tag in re.findall(r"<([A-Z][A-Z0-9]*)", line):
                    result.setdefault(tag, []).append(i)
            self._tag_locations = result
        return self._tag_locations

    def get_attribute(self, tag: str, attr: str) -> Optional[str]:
        """Return first value of attribute `attr` on tag `tag`."""
        pattern = rf"<{tag}\s[^>]*{attr}=\"([^\"]*)\""
        m = re.search(pattern, self.raw)
        return m.group(1) if m else None

    def get_polidoc_attrs(self) -> dict[str, str]:
        attrs: dict[str, str] = {}
        m = re.search(r"<POLIDOC\s+([^>]+)>", self.raw)
        if m:
            for am in re.finditer(r'(\w+)="([^"]*)"', m.group(1)):
                attrs[am.group(1)] = am.group(2)
        return attrs

    def get_polident_n(self) -> Optional[str]:
        m = re.search(r"<POLIDENT[^>]*>.*?<N[^>]*>(.*?)</N>", self.raw, re.DOTALL)
        return m.group(1).strip() if m else None

    def get_polident_ti(self) -> Optional[str]:
        m = re.search(r"<POLIDENT[^>]*>.*?<TI[^>]*>(.*?)</TI>", self.raw, re.DOTALL)
        if m:
            return re.sub(r"<[^>]+>", "", m.group(1)).strip()
        return None

    def get_all_ti_texts(self) -> list[str]:
        """Extract all <TI> text values from the document."""
        items = re.findall(r"<TI[^>]*>(.*?)</TI>", self.raw, re.DOTALL)
        results = []
        for item in items:
            text = re.sub(r"<[^>]+>", "", item)
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                results.append(text)
        return results

    def count_tag(self, tag: str) -> int:
        return len(re.findall(rf"<{tag}[\s>]", self.raw))

    def get_direct_children(self, parent_tag: str) -> list[str]:
        """
        Return child tag names for the FIRST occurrence of parent_tag.
        Naive: looks for child tags within parent's open/close span.
        """
        m = re.search(
            rf"<{parent_tag}[^>]*>(.*?)</{parent_tag}>",
            self.raw, re.DOTALL
        )
        if not m:
            return []
        inner = m.group(1)
        # Remove nested content one level deeper
        children = re.findall(r"<([A-Z][A-Z0-9]*)", inner)
        return children

    def find_all_blocks(self, tag: str) -> list[str]:
        """Return content of all <tag>...</tag> blocks."""
        return re.findall(
            rf"<{tag}[^>]*>(.*?)</{tag}>", self.raw, re.DOTALL
        )


def extract_structure(raw: str) -> SGMLStructure:
    """Return an SGMLStructure for the given raw SGML string."""
    return SGMLStructure(raw)
