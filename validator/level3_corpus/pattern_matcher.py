"""
level3_corpus/pattern_matcher.py
──────────────────────────────────
Level 3: Corpus Pattern Matching  (25 points total)

Purpose: Anomaly detection for NEW production documents.
Compare a document's statistical features against the vendor corpus baseline.

Scoring
───────
  1. Jurisdiction Detection  (5 pts)  — detect + validate LABEL consistency
  2. Statistical Baseline   (10 pts)  — word count, depth, tag counts vs corpus
  3. Pattern Compliance     (10 pts)  — tag patterns, structural coherence

IMPORTANT: L3 is NOT run on vendor corpus files themselves (circular logic).
  It is used only for new pipeline-generated SGML in production.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Any

from validator.level3_corpus.jurisdiction_detector import (
    detect_jurisdiction,
    detect_doc_type,
)

# ── Load corpus patterns ───────────────────────────────────────────────────────
_PATTERNS_PATH = Path(__file__).parent / "corpus_patterns.json"
_CORPUS: Optional[dict] = None


def load_corpus() -> dict:
    """Load corpus_patterns.json (cached after first load)."""
    global _CORPUS
    if _CORPUS is None:
        if not _PATTERNS_PATH.exists():
            raise FileNotFoundError(
                f"corpus_patterns.json not found at {_PATTERNS_PATH}. "
                f"Run: python -m validator.level3_corpus.corpus_analyzer <path_to_juri.zip>"
            )
        with open(_PATTERNS_PATH, encoding="utf-8") as f:
            _CORPUS = json.load(f)
    return _CORPUS


# ── Result dataclass ───────────────────────────────────────────────────────────
@dataclass
class L3Result:
    score: float = 0.0
    max_score: float = 25.0

    # Sub-scores
    jurisdiction_score: float = 0.0   # 0–5
    statistical_score: float = 0.0    # 0–10
    pattern_score: float = 0.0        # 0–10

    detected_jurisdiction: str = "unknown"
    detection_method: str = ""
    detected_doc_type: str = "notice"

    # Statistics
    word_count: int = 0
    corpus_word_mean: float = 0.0
    corpus_word_std: float = 0.0
    word_count_z: float = 0.0

    similar_label_in_corpus: bool = False
    unknown_patterns: list[str] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    corpus_available: bool = True


def _add_issue(result: L3Result, category: str, severity: str,
               description: str, impact: str = "") -> None:
    result.issues.append({
        "level": "L3",
        "category": category,
        "severity": severity,
        "description": description,
        "impact": impact,
    })


def _get_baseline(corpus: dict, jurisdiction: str, doc_type: str) -> Optional[dict]:
    """Get the most specific baseline available."""
    # Try jurisdiction × doc_type
    jdt = corpus.get("by_jurisdiction_and_type", {})
    if jurisdiction in jdt and doc_type in jdt[jurisdiction]:
        bl = jdt[jurisdiction][doc_type]
        if bl.get("doc_count", 0) >= 3:  # Need at least 3 docs for a reliable baseline
            return bl

    # Fall back to jurisdiction
    bj = corpus.get("by_jurisdiction", {})
    if jurisdiction in bj and bj[jurisdiction].get("doc_count", 0) >= 2:
        return bj[jurisdiction]

    # Fall back to doc_type
    bt = corpus.get("by_doc_type", {})
    if doc_type in bt:
        return bt[doc_type]

    # Fall back to overall
    return corpus.get("overall")


# ─────────────────────────────────────────────────────────────────────────────
# Check 1: Jurisdiction Detection
# ─────────────────────────────────────────────────────────────────────────────
def _check_jurisdiction(raw: str, file_path: Optional[str],
                        corpus: dict, result: L3Result) -> None:
    """5 pts: detect jurisdiction and validate label consistency."""
    score = 5.0

    juri, method = detect_jurisdiction(raw, file_path)
    result.detected_jurisdiction = juri
    result.detection_method = method

    # J1 — If jurisdiction unknown, penalise
    if juri == "unknown":
        score -= 2.0
        _add_issue(result, "jurisdiction", "minor",
                   "Could not determine jurisdiction from path, label, or document number. "
                   "Corpus comparison will use overall baseline.",
                   impact="-2 pts")

    # J2 — Validate LABEL is a known label
    m = re.search(r'LABEL="([^"]+)"', raw)
    if m:
        label = m.group(1)
        known_labels = set(corpus.get("known_labels", []))
        if label not in known_labels:
            score -= 1.0
            result.warnings.append(
                f"POLIDOC LABEL='{label}' not seen in vendor corpus. "
                f"May be a new document type or a typo."
            )
            _add_issue(result, "jurisdiction", "minor",
                       f"Unknown LABEL value: '{label}'. Not found in 98 vendor files.",
                       impact="-1 pt")
        else:
            result.similar_label_in_corpus = True

    result.jurisdiction_score = max(0.0, score)


# ─────────────────────────────────────────────────────────────────────────────
# Check 2: Statistical Baseline
# ─────────────────────────────────────────────────────────────────────────────
def _check_statistics(raw: str, corpus: dict, result: L3Result) -> None:
    """10 pts: word count, nesting depth, tag counts vs corpus baseline."""
    score = 10.0

    # Extract document statistics
    text = re.sub(r"<[^>]+>", " ", raw)
    word_count = len(text.split())
    result.word_count = word_count

    tags = re.findall(r"<([A-Z][A-Z0-9]*)", raw)
    unique_tags = set(tags)

    # Nesting depth
    max_depth = 0
    depth = 0
    for m in re.finditer(r"<(/?)([A-Z][A-Z0-9]*)", raw):
        if m.group(1):
            depth = max(0, depth - 1)
        else:
            depth += 1
            max_depth = max(max_depth, depth)

    baseline = _get_baseline(corpus, result.detected_jurisdiction, result.detected_doc_type)
    if not baseline:
        result.warnings.append("No corpus baseline available for comparison.")
        result.statistical_score = 7.0  # Partial credit when no baseline
        return

    # S1 — Word count z-score
    wc_stats = baseline.get("word_count", {})
    if wc_stats.get("mean") and wc_stats.get("std", 0) > 0:
        mean = wc_stats["mean"]
        std = wc_stats["std"]
        z = (word_count - mean) / std
        result.word_count_z = z
        result.corpus_word_mean = mean
        result.corpus_word_std = std

        if z < -4.0:
            # Document is far BELOW baseline — likely missing content.
            score -= 4.0
            result.anomalies.append(
                f"Word count {word_count} is {z:+.1f}σ from corpus mean "
                f"({mean:.0f} ± {std:.0f}). Possible content truncation."
            )
            _add_issue(result, "statistical_baseline", "major",
                       f"Word count anomaly: {word_count} words (z={z:+.1f}). "
                       f"Corpus baseline for {result.detected_jurisdiction}/{result.detected_doc_type}: "
                       f"{mean:.0f} ± {std:.0f} words. Document may be truncated.",
                       impact="-4 pts")
        elif z < -3.0:
            # Moderately below baseline — worth flagging.
            score -= 2.0
            result.anomalies.append(
                f"Word count {word_count} is {z:+.1f}σ from corpus mean ({mean:.0f})."
            )
            _add_issue(result, "statistical_baseline", "minor",
                       f"Word count below expected range: {word_count} words (z={z:+.1f}).",
                       impact="-2 pts")
        elif z > 4.0:
            # Document is ABOVE baseline — large comprehensive instrument, not an error.
            # Log as informational only (no score deduction).
            result.anomalies.append(
                f"Word count {word_count} is {z:+.1f}σ above corpus mean "
                f"({mean:.0f} ± {std:.0f}). Large/comprehensive document."
            )
            _add_issue(result, "statistical_baseline", "warning",
                       f"Word count {word_count} exceeds corpus baseline "
                       f"(z={z:+.1f}, mean={mean:.0f}). Document is larger than typical — "
                       f"possibly includes companion policy or appendix material.",
                       impact="0 pts (informational)")

    # S2 — Nesting depth
    depth_stats = baseline.get("nesting_depth", {})
    corpus_max_depth = depth_stats.get("max", 20)
    if max_depth > corpus_max_depth + 5:
        score -= 2.0
        result.anomalies.append(
            f"Nesting depth {max_depth} exceeds corpus maximum {corpus_max_depth} by 5+."
        )
        _add_issue(result, "statistical_baseline", "minor",
                   f"Nesting depth {max_depth} unusually deep (corpus max={corpus_max_depth}).",
                   impact="-2 pts")

    # S3 — Tag presence: common corpus tags should be present
    common_tags = set(baseline.get("common_tags", []))
    if common_tags:
        # Exclude metadata tags from this check
        structural_common = common_tags - {"POLIDOC", "POLIDENT", "FREEFORM", "N", "TI", "DATE"}
        missing_common = structural_common - unique_tags
        if len(missing_common) > 3:
            score -= 1.0
            result.warnings.append(
                f"Missing {len(missing_common)} tags common in {result.detected_jurisdiction} corpus: "
                f"{sorted(missing_common)[:5]}"
            )

    result.statistical_score = max(0.0, score)


# ─────────────────────────────────────────────────────────────────────────────
# Check 3: Pattern Compliance
# ─────────────────────────────────────────────────────────────────────────────
def _check_patterns(raw: str, corpus: dict, result: L3Result) -> None:
    """10 pts: structural patterns, tag hierarchy coherence."""
    score = 10.0

    doc_type = result.detected_doc_type

    # P1 — Notice docs should use BLOCK hierarchy, not PART
    if doc_type == "notice":
        if "<PART" in raw or "<SEC " in raw:
            # This may be OK if it's actually an instrument mislabelled
            result.warnings.append(
                "Document uses PART/SEC hierarchy but classified as 'notice' type. "
                "Verify document type detection."
            )

    # P2 — Instrument docs should have PART or SEC
    if doc_type == "instrument":
        if "<PART" not in raw and "<SEC " not in raw:
            score -= 3.0
            result.anomalies.append(
                "Document classified as 'instrument' but has no PART or SEC tags. "
                "Expected PART > SEC > SSEC hierarchy."
            )
            _add_issue(result, "pattern_compliance", "major",
                       "Instrument-type document missing PART/SEC hierarchy structure.",
                       impact="-3 pts")

    # P3 — Document must have FREEFORM (100% of vendor corpus)
    if "<FREEFORM" not in raw:
        score -= 3.0
        _add_issue(result, "pattern_compliance", "critical",
                   "No <FREEFORM> element. All 98 vendor files have FREEFORM. "
                   "This indicates a structural problem.",
                   impact="-3 pts")

    # P4 — TABLE must be paired with SGMLTBL
    table_count = len(re.findall(r"<TABLE[\s>]", raw))
    sgmltbl_count = len(re.findall(r"<SGMLTBL[\s>]", raw))
    if table_count != sgmltbl_count:
        score -= 2.0
        _add_issue(result, "pattern_compliance", "major",
                   f"TABLE/SGMLTBL count mismatch: TABLE={table_count}, SGMLTBL={sgmltbl_count}. "
                   f"In corpus, TABLE always wraps exactly one SGMLTBL.",
                   impact="-2 pts")

    # P5 — Check that POLIDOC has both POLIDENT and FREEFORM as immediate-ish children
    # EXEMPT: TSX By-Laws and Forms use MISCLAW/LEGIDDOC root (no POLIDENT is valid)
    has_polident = "<POLIDENT" in raw
    is_tsx_special = bool(re.search(r"<(MISCLAW|LEGIDDOC)[\s>]", raw))
    if not has_polident and not is_tsx_special:
        score -= 2.0
        _add_issue(result, "pattern_compliance", "critical",
                   "No <POLIDENT> found. All vendor docs have POLIDENT inside POLIDOC.",
                   impact="-2 pts")

    # P6 — Corpus label validation (known LABEL values)
    baseline = _get_baseline(corpus, result.detected_jurisdiction, result.detected_doc_type)
    if baseline:
        label_dist = baseline.get("label_distribution", {})
        m = re.search(r'LABEL="([^"]+)"', raw)
        if m and label_dist:
            doc_label = m.group(1)
            if doc_label not in label_dist:
                # Not necessarily wrong, just unusual
                result.warnings.append(
                    f"LABEL='{doc_label}' not seen in {result.detected_jurisdiction} corpus. "
                    f"Known labels: {list(label_dist.keys())[:5]}"
                )

    result.pattern_score = max(0.0, score)


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────
def validate_against_corpus(
    raw_sgml: str,
    file_path: Optional[str] = None,
) -> L3Result:
    """
    Run Level 3 corpus pattern matching on a SGML document.

    Parameters
    ----------
    raw_sgml  : str          — raw SGML content
    file_path : str, optional — used for jurisdiction detection from path

    Returns
    -------
    L3Result with score (0–25) and anomaly report.
    """
    result = L3Result()
    result.detected_doc_type = detect_doc_type(raw_sgml)

    try:
        corpus = load_corpus()
    except FileNotFoundError as e:
        result.corpus_available = False
        result.warnings.append(str(e))
        result.score = 0.0
        result.warnings.append("L3 skipped: corpus_patterns.json not available.")
        return result

    _check_jurisdiction(raw_sgml, file_path, corpus, result)
    _check_statistics(raw_sgml, corpus, result)
    _check_patterns(raw_sgml, corpus, result)

    result.score = (
        result.jurisdiction_score +
        result.statistical_score +
        result.pattern_score
    )

    return result
