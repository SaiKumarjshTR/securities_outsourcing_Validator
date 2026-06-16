"""
level3_corpus/jurisdiction_detector.py
────────────────────────────────────────
Detect the jurisdiction of a SGML document using a priority-ordered
multi-signal approach.

Priority order (from benchmark analysis):
  1. Directory path (most reliable — used in juri.zip structure)
  2. POLIDOC LABEL attribute
  3. POLIDENT N tag content
  4. LANG attribute (least useful — all 98 vendor files are EN)

Jurisdiction codes align with corpus_patterns.json keys.
"""

import re
from typing import Optional


# ── Label prefix → jurisdiction ───────────────────────────────────────────────
LABEL_TO_JURI: dict[str, str] = {
    "osc": "Ontario",
    "csa": "CSA_National",
    "ciro": "CIRO",
    "tmx": "TSX",
    "tsx": "TSX",
    "bcsc": "British_Columbia",
    "bc ": "British_Columbia",
    "bc instrument": "British_Columbia",
    "bc notice": "British_Columbia",
    "bc policy": "British_Columbia",
    "asc": "Alberta",
    "msc": "Manitoba",
    "ontario securities commission": "Ontario",
    "superintendent order": "Nova_Scotia",
    "advisory notice": "NWT",
    "blanket order": "British_Columbia",
    "csa multilateral": "CSA_National",
    "multilateral instrument": "CSA_National",
    "national instrument": "CSA_National",
}

# Document number prefix → jurisdiction (e.g., "11-312" → Ontario OSC range)
N_PREFIX_TO_JURI: dict[str, str] = {
    "11-": "CSA_National",
    "13-": "CSA_National",
    "23-": "CSA_National",
    "24-": "CSA_National",
    "25-": "British_Columbia",
    "31-": "CSA_National",
    "33-": "CSA_National",
    "35-": "Alberta",
    "41-": "CSA_National",
    "44-": "Nova_Scotia",
    "45-": "CSA_National",
    "51-": "Ontario",
    "71-": "CSA_National",
    "81-": "CSA_National",
    "91-": "Quebec",
    "93-": "Quebec",
    "94-": "Quebec",
    "96-": "British_Columbia",
    "TSX": "TSX",
    "By-Law": "TSX",
}


def detect_jurisdiction(
    sgml_content: str,
    file_path: Optional[str] = None,
) -> tuple[str, str]:
    """
    Detect document jurisdiction.

    Parameters
    ----------
    sgml_content : str
        Raw SGML content.
    file_path : str, optional
        File path or zip path (e.g., 'juri/Ontario/Ontario/11-502.sgm').

    Returns
    -------
    (jurisdiction_code, detection_method)
        jurisdiction_code : str — matches corpus_patterns.json keys
        detection_method  : str — which signal was used
    """

    # ── Signal 1: Directory path ──────────────────────────────────────────────
    if file_path:
        parts = file_path.replace("\\", "/").split("/")
        dir_name = None
        for part in parts:
            if part in {
                "Alberta", "British_Columbia", "CIRO_", "Manitoba",
                "Montreal-Exchange", "NB_", "NFLD_", "NS_", "NWT_",
                "Ontario", "PEI", "Quebec", "Saskatchewan",
                "Toronto-Stock-Exchange", "Yukon_",
            }:
                dir_name = part
                break

        if dir_name:
            JURI_MAP = {
                "Alberta": "Alberta",
                "British_Columbia": "British_Columbia",
                "CIRO_": "CIRO",
                "Manitoba": "Manitoba",
                "Montreal-Exchange": "Montreal",
                "NB_": "New_Brunswick",
                "NFLD_": "Newfoundland",
                "NS_": "Nova_Scotia",
                "NWT_": "NWT",
                "Ontario": "Ontario",
                "PEI": "PEI",
                "Quebec": "Quebec",
                "Saskatchewan": "Saskatchewan",
                "Toronto-Stock-Exchange": "TSX",
                "Yukon_": "Yukon",
            }
            return JURI_MAP[dir_name], "directory_path"

    # ── Signal 2: POLIDOC LABEL attribute ─────────────────────────────────────
    m = re.search(r'LABEL="([^"]+)"', sgml_content)
    if m:
        label_lower = m.group(1).lower()
        for prefix, juri in LABEL_TO_JURI.items():
            if label_lower.startswith(prefix):
                return juri, f"label:{m.group(1)}"
        # Partial matches
        if "csa" in label_lower:
            return "CSA_National", f"label_partial:{m.group(1)}"
        if "ciro" in label_lower:
            return "CIRO", f"label_partial:{m.group(1)}"
        if "tmx" in label_lower or "tsx" in label_lower or "tmx" in label_lower:
            return "TSX", f"label_partial:{m.group(1)}"

    # ── Signal 3: POLIDENT N tag ───────────────────────────────────────────────
    n_m = re.search(r"<POLIDENT[^>]*>.*?<N[^>]*>(.*?)</N>", sgml_content, re.DOTALL)
    if n_m:
        doc_num = re.sub(r"<[^>]+>", "", n_m.group(1)).strip()
        for prefix, juri in N_PREFIX_TO_JURI.items():
            if doc_num.startswith(prefix):
                return juri, f"doc_number:{doc_num}"

    return "unknown", "undetected"


def detect_doc_type(sgml_content: str) -> str:
    """
    Detect document type: 'notice' or 'instrument'.

    'instrument' = has PART/SEC hierarchy (formal regulation/rule structure)
    'notice'     = uses BLOCK hierarchy (announcements, staff notices)
    """
    has_part = bool(re.search(r"<PART[\s>]", sgml_content))
    has_sec = bool(re.search(r"<SEC[\s>]", sgml_content))
    if has_part or has_sec:
        return "instrument"
    return "notice"
