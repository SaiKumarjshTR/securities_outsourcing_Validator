"""
core/document_classifier.py
-----------------------------
Pre-classification of SGML documents BEFORE validation.
Prevents 47 false failures by detecting document type first.

Document types:
  NOTICE        - 79/98 vendor files. Notices, bulletins, circulars.
  INSTRUMENT    - 18/98. Rules, Regulations, Instruments (has PART/SEC).
  AMENDMENT     - 36/98. Contains QUOTE tag (delta text only, low word ratio is OK).
  TSX_SPECIAL   - 5/98. TSX By-Laws and Forms (no POLIDENT - legitimate).
"""

import re
from dataclasses import dataclass

# Filename markers that indicate TSX special documents
TSX_SPECIAL_FILENAME_MARKERS = (
    "By-Law", "By-law", "ByLaw", "by-law", "bylaw", "Form", "form",
    "Capital-Markets-Tribunal",
)


@dataclass
class DocumentClass:
    """Pre-classification result. Passed to all validation layers."""
    lang: str = "EN"
    doc_type: str = "NOTICE"         # NOTICE | INSTRUMENT | AMENDMENT | TSX_SPECIAL
    jurisdiction: str = "Unknown"
    has_quote: bool = False           # True = amendment (QUOTE tag present)
    has_polident: bool = True         # False only for TSX By-Laws/Forms
    is_tsx_special: bool = False      # True for the 5 TSX docs without POLIDENT
    has_part_sec: bool = False        # True for PART/SEC-based documents
    label: str = ""                   # POLIDOC LABEL attribute value
    adddate: str = ""                 # POLIDOC ADDDATE attribute
    confidence: float = 1.0          # Classification confidence (0-1)


def detect_jurisdiction(sgml_content: str, file_path: str) -> str:
    """Detect jurisdiction from file path or SGML label."""
    path_up = file_path.replace("\\", "/").upper()

    # Path-based detection (most reliable)
    path_map = [
        ("ALBERTA", "Alberta"), ("BRITISHCOLUMBIA", "BritishColumbia"),
        ("MANITOBA", "Manitoba"), ("NEWBRUNSWICK", "NewBrunswick"),
        ("NEWFOUNDLAND", "Newfoundland"), ("NFLD", "Newfoundland"),
        ("NOVASCOTIA", "NovScotia"), ("/NWT/", "NWT"), ("NUNAVUT", "Nunavut"),
        ("ONTARIO", "Ontario"), ("PEI", "PEI"), ("QUEBEC", "Quebec"),
        ("SASKATCHEWAN", "Saskatchewan"), ("TSX", "TSX"), ("TMX", "TSX"),
        ("CIRO", "CIRO"), ("IIROC", "CIRO"),
    ]
    for keyword, jur in path_map:
        if keyword in path_up:
            return jur

    # Label-based detection
    label_m = re.search(r'LABEL="([^"]+)"', sgml_content)
    if label_m:
        label = label_m.group(1).upper()
        label_map = [
            ("ASC", "Alberta"), ("BCSC", "BritishColumbia"), ("MSC", "Manitoba"),
            ("NSSSC", "NovScotia"), ("OSC", "Ontario"), ("SFSA", "Saskatchewan"),
            ("TSX", "TSX"), ("TMX", "TSX"), ("CIRO", "CIRO"), ("IIROC", "CIRO"),
            ("CSA", "CSA"), ("NATIONAL INSTRUMENT", "National"), ("NI ", "National"),
        ]
        for keyword, jur in label_map:
            if keyword in label:
                return jur

    return "Unknown"


def pre_classify(
    sgml_content: str,
    file_path: str = "",
    filename: str = "",
) -> DocumentClass:
    """
    Pre-classify a SGML document before any validation rules are applied.

    This prevents:
      13 false failures from amendment word-count critical failure
       5 false failures from missing POLIDENT in valid TSX docs
      29 false failures from heading check (indirectly, via doc type context)

    Args:
        sgml_content: Raw SGML file content
        file_path:    Full path to the SGML file
        filename:     Just the filename (basename)
    """
    doc = DocumentClass()

    # 1. Language
    lang_m = re.search(r'LANG="(EN|FR)"', sgml_content)
    doc.lang = lang_m.group(1) if lang_m else "EN"

    # 2. Amendment detection (MOST IMPORTANT)
    # Primary signal: QUOTE tag = delta text only.
    # Secondary signal: filename contains "amending" or "amendment" or similar.
    doc.has_quote = bool(re.search(r'<QUOTE[\s>]', sgml_content))
    _filename_lower = (filename or file_path.replace("\\", "/").rsplit("/", 1)[-1]).lower()
    _is_amending_filename = any(
        kw in _filename_lower for kw in ("amending", "amendment", "implementing")
    )

    # 3. POLIDENT presence (93/98 files; 5 exceptions are TSX By-Laws/Forms)
    doc.has_polident = bool(re.search(r'<POLIDENT[\s>]', sgml_content))

    # 4. POLIDOC metadata
    label_m = re.search(r'LABEL="([^"]+)"', sgml_content)
    doc.label = label_m.group(1) if label_m else ""
    adddate_m = re.search(r'ADDDATE="([^"]+)"', sgml_content)
    doc.adddate = adddate_m.group(1) if adddate_m else ""

    # 5. Structural detection
    doc.has_part_sec = bool(
        re.search(r'<PART[\s>]', sgml_content) or
        re.search(r'<SEC[\s>]', sgml_content)
    )

    # 6. Jurisdiction
    doc.jurisdiction = detect_jurisdiction(sgml_content, file_path or filename)

    # 7. TSX Special detection
    basename = filename or file_path.replace("\\", "/").rsplit("/", 1)[-1]
    is_tsx = doc.jurisdiction == "TSX"
    has_special_name = any(m in basename for m in TSX_SPECIAL_FILENAME_MARKERS)
    doc.is_tsx_special = (not doc.has_polident) and (is_tsx or has_special_name)

    # 8. Document type classification
    if doc.is_tsx_special:
        doc.doc_type = "TSX_SPECIAL"
        doc.confidence = 0.95
    elif doc.has_quote or _is_amending_filename:
        # Amendment instruments: may also have PART/SEC (amending a regulation)
        doc.doc_type = "AMENDMENT"
        doc.confidence = 0.98 if doc.has_quote else 0.85
    elif doc.has_part_sec:
        doc.doc_type = "INSTRUMENT"
        doc.confidence = 0.90
    else:
        doc.doc_type = "NOTICE"
        doc.confidence = 0.85

    return doc
