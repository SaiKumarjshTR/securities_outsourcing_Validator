"""
core/valid_tags.py
──────────────────
Authoritative 77-tag whitelist derived from all 1,410 corpus files.
Date verified: 2026-04-21

Any tag NOT in VALID_TAGS appearing in a production SGML is flagged as invalid.
"""

# ── Complete whitelist from corpus analysis ───────────────────────────────────
VALID_TAGS: frozenset[str] = frozenset({
    # Core document structure
    "POLIDOC", "POLIDENT", "FREEFORM", "QUOTE",

    # Block hierarchy (BLOCK0 is real — used in Companion Policies, Orders)
    "BLOCK0", "BLOCK1", "BLOCK2", "BLOCK3", "BLOCK4", "BLOCK5", "BLOCK6",

    # Part / Section hierarchy (Type B instruments/rules)
    "PART", "SEC", "SECP", "SSEC", "SSECP",
    "PARA", "PARAP", "SPARA", "SPARAP",
    "MDIV", "MDIV1",

    # Definitions (actual structure: DEF → DEFP → TERM, NOT DEFTERM)
    "DEF", "DEFP", "TERM",

    # Content elements
    "TI", "N",
    "P", "P1", "P2", "P3", "P4",
    "ITEM", "LINE",

    # Table structure (TABLE always wraps SGMLTBL; TBL is a legacy simple-table variant)
    "TABLE", "SGMLTBL", "TBL",
    "TBLCDEFS", "TBLCDEF",
    "TBLHEAD", "TBLBODY", "TBLROWS", "TBLROW", "TBLCELL",
    "TBLNOTE", "TBLNOTES",

    # Legal / regulatory structures
    "CL", "CLP", "SCL", "SCLP",
    "STATREF", "STATHIST", "REG", "REFN",
    "ACTUNDER",

    # Document containers
    "APPENDIX", "ARTICLE", "CHAPTER",
    "FORM", "SCHEDDOC", "LEGIDDOC", "MISCLAW", "CONTAINR",

    # Inline formatting (ITALIC is a legacy alias for EM — 56 corpus files use it)
    "BOLD", "EM", "SUP", "SUB", "STRIKE", "ITALIC",

    # Special / metadata
    "GRAPHIC", "LINKTEXT",
    "EDITNOTE",
    "DATE", "CITE",
    "FOOTNOTE", "FN",
    "QUOTE",   # also structural (instruments wrap rules in QUOTE)
    "APPENDIX",

    # Formula / math
    "DF",
})


# ── Lookup helpers ────────────────────────────────────────────────────────────
def is_valid_tag(tag_name: str) -> bool:
    """Return True if tag_name is in the authoritative whitelist."""
    return tag_name in VALID_TAGS


def get_invalid_tags(used_tags: set[str]) -> set[str]:
    """Return the set of tags from used_tags that are NOT in VALID_TAGS."""
    return used_tags - VALID_TAGS


# ── Tags that require specific children (for nesting validation) ──────────────
REQUIRED_CHILDREN: dict[str, list[str]] = {
    # POLIDOC must have POLIDENT and FREEFORM
    "POLIDOC":  ["POLIDENT", "FREEFORM"],
    # DEF must contain DEFP (confirmed from corpus; TERM is inside DEFP)
    "DEF":      ["DEFP"],
    # TABLE must wrap SGMLTBL
    "TABLE":    ["SGMLTBL"],
    # SGMLTBL must contain TBLBODY (TBLHEAD and TBLCDEFS are common but optional)
    "SGMLTBL":  ["TBLBODY"],
}

# Tags that must NOT appear as children of other tags
FORBIDDEN_PARENT_CHILD: list[tuple[str, str]] = [
    # <P> must not appear directly inside <TBLCELL>  (confirmed: 0/98 vendor files)
    ("TBLCELL", "P"),
    # FOOTNOTE must not appear inside TABLE
    ("TABLE",   "FOOTNOTE"),
    ("SGMLTBL", "FOOTNOTE"),
    ("TBLROW",  "FOOTNOTE"),
    ("TBLCELL", "FOOTNOTE"),
]

# Tags that are self-contained (cannot nest another of themselves)
NON_RECURSIVE_TAGS: frozenset[str] = frozenset({
    "TI", "N", "P", "P1", "P2", "P3", "P4",
    "BOLD", "EM", "STRIKE", "SUP", "SUB",
    "DATE", "CITE", "TERM",
})
