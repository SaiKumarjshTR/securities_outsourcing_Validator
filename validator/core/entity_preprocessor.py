"""
core/entity_preprocessor.py
-----------------------------
Convert SGML (with Carswell entities) to XML-parseable text.

Loads ALL 250+ entities from entities_list.txt dynamically at startup.
The file uses SGML SDATA format: <!ENTITY name SDATA "&name;" >

For standard HTML entities: maps to Unicode via html.entities module.
For Carswell-specific entities (sup-*, Nisga'a chars, etc.): manual Unicode mapping.

Usage:
    from validator.core.entity_preprocessor import preprocess_sgml, sgml_to_text
"""

import re
import unicodedata
import html.entities
from pathlib import Path
from typing import Optional


def _find_entities_file() -> Optional[Path]:
    """Search common locations for entities_list.txt."""
    candidates = [
        Path(__file__).parent.parent.parent.parent / "entities_list.txt",
        Path(__file__).parent.parent.parent.parent / "deploy" / "entities_list.txt",
        Path("securities-outsourcing-samples") / "entities_list.txt",
        Path("securities-outsourcing-samples") / "deploy" / "entities_list.txt",
        Path("/tmp/prev_sent/Carswell_DTD/entities_list.txt"),
    ]
    for p in candidates:
        try:
            if p.exists():
                return p
        except Exception:
            pass
    return None


def _build_entity_map() -> dict:
    """
    Build complete Carswell entity map: 250+ entities.

    Strategy:
      1. Seed with html.entities.name2codepoint (standard HTML entities).
      2. Overlay Carswell-specific entities (sup-*, Nisga'a, special symbols).
      3. Parse entities_list.txt to discover any remaining names.
      4. Ensure XML built-ins are always XML-safe.
    """
    # Step 1: HTML entity baseline
    entity_map = {name: chr(cp) for name, cp in html.entities.name2codepoint.items()}

    # Step 2: Carswell-specific entities not in HTML standard
    CUSTOM = {
        # French ordinal superscripts
        "sup-e":    "\u1d49",
        "sup-er":   "\u1d49r",
        "sup-ere":  "\u00e8re",
        "sup-ers":  "ers",
        "sup-r":    "\u02b3",
        "sup-rs":   "rs",
        "sup-re":   "re",
        "sup-res":  "res",
        "sup-lle":  "lle",
        "sup-me":   "me",
        "sup-os":   "os",
        "sup-egre": "\u00e8gre",
        "sup-ieme": "i\u00e8me",
        "sup-ier":  "ier",
        # Nisga'a / Gitxsan language characters
        "Gback":  "G\u0332",
        "gback":  "g\u0332",
        "Kback":  "K\u0332",
        "kback":  "k\u0332",
        "Glotta": "\u02c0",
        "glotta": "\u0294",
        "Xback":  "X\u0332",
        "xback":  "x\u0332",
        # Special Carswell symbols
        "dottab":  "\t",
        "newline": "\n",
        "mspace":  "\u2003",
        "wdash":   "\u2012",
        "caret":   "^",
        "block":   "\u2588",
        "check":   "\u2713",
        "square":  "\u25a1",
        "lsqb":    "[",
        "rsqb":    "]",
        "verbar":  "|",
        "rx":      "\u211e",
        "Ppeso":   "\u20b1",
        "male":    "\u2642",
        "female":  "\u2640",
        "ape":     "\u224a",
        "cap":     "\u2229",
        "cirf":    "\u25cf",
        "cir":     "\u25cb",
        "dtrif":   "\u25bc",
        "dtris":   "\u25bd",
        "utri":    "\u25b3",
        "utrif":   "\u25b2",
        "timesb":  "\u22a0",
        "oplus":   "\u2295",
        "spades":  "\u2660",
        "clubs":   "\u2663",
        "heart":   "\u2665",
        "diams":   "\u2666",
        "prime":   "\u2032",
        "Prime":   "\u2033",
        "epsiv":   "\u03b5",
        "ee":      "e",
        "smile":   "\u2323",
        "oinfin":  "\u29bc",
        # Extended Latin not in html.entities
        "Amacr":   "\u0100",
        "amacr":   "\u0101",
        "Emacr":   "\u0112",
        "emacr":   "\u0113",
        "Omacr":   "\u014c",
        "omacr":   "\u014d",
        "imacr":   "\u012b",
        "Gcaron":  "\u01e6",
        "gcaron":  "\u01e7",
        "Rcaron":  "\u0158",
        "rcaron":  "\u0159",
        "Ecaron":  "\u011a",
        "ecaron":  "\u011b",
        "Cacute":  "\u0106",
        "cacute":  "\u0107",
        "Scaron":  "\u0160",
        "scaron":  "\u0161",
        "Zcaron":  "\u017d",
        "zcaron":  "\u017e",
        "Lstrok":  "\u0141",
        "lstrok":  "\u0142",
        "odblac":  "\u0151",
        "Odblac":  "\u0150",
        "Itilde":  "\u0128",
        "itilde":  "\u0129",
        "Iogon":   "\u012e",
        "iogon":   "\u012f",
        "Oogon":   "\u01ea",
        "oogon":   "\u01eb",
        "ibreve":  "\u012d",
        "obreve":  "\u014f",
        "Tacute":  "T\u0301",
        "tacute":  "t\u0301",
        "Kacute":  "K\u0301",
        "kacute":  "k\u0301",
        "Scedil":  "\u015e",
        "scedil":  "\u015f",
        "Scirc":   "\u015c",
        "scirc":   "\u015d",
        "Lcedil":  "L\u0327",
        "lcedil":  "l\u0327",
        "ncedil":  "n\u0327",
        "erever":  "\u0258",
        "esh":     "\u0283",
        "eturn":   "\u01dd",
        "schwa":   "\u0259",
        "Eacuto":  "\u0118",
        "eacuto":  "\u0119",
        "Oacuto":  "\u014e",
        "oacuto":  "\u014f",
        "Ogravo":  "\u0150",
        "ogravo":  "\u0151",
        "Egravo":  "\u0116",
        "egravo":  "\u0117",
        "Iacuto":  "\u012e",
        "iacuto":  "\u012f",
        "Uacuto":  "\u016a",
        "uacuto":  "\u016b",
        "Nmacr":   "N\u0304",
        "nmacr":   "n\u0304",
        "Vmacr":   "V\u0304",
        "vmacr":   "v\u0304",
        "Xmacr":   "X\u0304",
        "xmacr":   "x\u0304",
        "Pmacr":   "P\u0304",
        "Dmacr":   "D\u0304",
        "cmacr":   "c\u0304",
    }
    entity_map.update(CUSTOM)

    # Step 3: Parse entities_list.txt for any remaining names
    ent_file = _find_entities_file()
    if ent_file:
        try:
            content = ent_file.read_text(encoding="utf-8", errors="replace")
            all_names = re.findall(r"<!ENTITY\s+(\S+)\s+SDATA", content)
            for name in all_names:
                if name not in entity_map:
                    lower = name.lower()
                    if lower in html.entities.name2codepoint:
                        entity_map[name] = chr(html.entities.name2codepoint[lower])
                    else:
                        entity_map[name] = ""   # safe removal
        except Exception:
            pass

    # Step 4: XML built-ins preserved for parser
    entity_map.update({
        "amp":  "&amp;",
        "lt":   "&lt;",
        "gt":   "&gt;",
        "apos": "&apos;",
        "quot": "&quot;",
    })

    return entity_map


# Module-level cached entity map (built once at import)
CARSWELL_ENTITY_MAP: dict = _build_entity_map()

# All valid entity names (250+) - used by structural validator
VALID_CARSWELL_ENTITY_NAMES: frozenset = frozenset(CARSWELL_ENTITY_MAP.keys())

# Compiled pattern to self-close known Carswell SGML void elements.
# These tags have no closing tag in Carswell DTD v4.7 (valid SGML, invalid XML).
# Example: <TBLCDEF COLWD="50" HALIGN="LEFT"> -> <TBLCDEF COLWD="50" HALIGN="LEFT"/>
_SGML_VOID_RE = re.compile(
    r"<(TBLCDEF|GRAPHIC|NEWLINE|DOTTAB|HR|BR)\b([^>]*)(?<!/)>",
    re.IGNORECASE,
)


def preprocess_sgml(raw: str) -> str:
    """
    Replace all &entity; references with Unicode (or safe XML entities).

    Steps:
    1. Strip SGML DOCTYPE / processing instructions.
    2. Replace known Carswell entities -> Unicode / XML-safe equivalents.
    3. Replace unknown entities with empty string (safe removal).

    Returns XML-parseable string.
    """
    # Strip DOCTYPE and SGML processing instructions.
    # The PI regex handles both:
    #   XML-style:   <?xml version="1.0"?>  (ends with ?>)
    #   SGML-style:  <?TBLROW 1>           (ends with > only)
    text = re.sub(r"<!DOCTYPE[^>]*>", "", raw, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<\?[^>]*>", "", text)
    # Self-close Carswell SGML void elements so the XML parser treats them
    # as empty elements rather than unclosed open tags.
    text = _SGML_VOID_RE.sub(r"<\1\2/>", text)

    def _replace_entity(m: re.Match) -> str:
        name = m.group(1)
        if name.startswith("#"):
            return m.group(0)   # numeric reference: leave for XML parser
        mapped = CARSWELL_ENTITY_MAP.get(name)
        if mapped is not None:
            return mapped
        return ""   # unknown entity: remove safely

    text = re.sub(r"&([#a-zA-Z][a-zA-Z0-9-]*);", _replace_entity, text)
    return text


def sgml_to_text(raw: str) -> str:
    """
    Strip all SGML tags and resolve entities -> plain Unicode text.
    Used by Level 1 content fidelity comparisons.
    """
    text = preprocess_sgml(raw)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_for_comparison(text: str) -> str:
    """
    Lowercase, NFC-normalise, collapse whitespace.
    Used when comparing PDF text vs SGML text.
    """
    text = unicodedata.normalize("NFC", text)
    text = text.lower()
    for src, tgt in [
        ("\u201c", '"'), ("\u201d", '"'),
        ("\u2018", "'"), ("\u2019", "'"),
        ("\u2014", "--"), ("\u2013", "-"),
        ("\u00a0", " "),
    ]:
        text = text.replace(src, tgt)
    text = re.sub(r"\s+", " ", text).strip()
    return text
