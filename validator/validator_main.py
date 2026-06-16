"""
validator_main.py
──────────────────
Main orchestrator for the TR SGML Validator.

Validates a pipeline-generated SGML file against its source PDF using
a three-level scoring system:

  L1 Content Fidelity   — 35 pts  (text coverage, headings, tables, footnotes)
  L2 Structural         — 40 pts  (tag schema, nesting, entities, legal structure)
  L3 Corpus Pattern     — 25 pts  (jurisdiction baseline, statistical anomaly)
  ─────────────────────────────────
  Total                 — 100 pts

Decision thresholds
───────────────────
  ≥ 95: ACCEPT
  ≥ 90: ACCEPT_WITH_WARNINGS
  ≥ 85: REVIEW
   < 85: REJECT  (or any critical failure)

Usage (CLI)
───────────
  python -m validator.validator_main  <sgml_file>  [pdf_file]
  python -m validator.validator_main  --batch  <directory>

Usage (API)
───────────
  from validator.validator_main import validate
  report = validate("output.sgm", "source.pdf")
  print(report.decision, report.total_score)
"""

import sys
import argparse
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from validator.level1_content.pdf_extractor import extract_pdf_content
from validator.level1_content.sgml_extractor import extract_sgml_content
from validator.level1_content.content_validator import validate_content, L1Result
from validator.level2_structural.structural_validator import validate_structure, L2Result
from validator.level3_corpus.pattern_matcher import validate_against_corpus, L3Result
from validator.level4_source_compare.source_validator import validate_source_comparison, L4Result
from validator.core.document_classifier import pre_classify

# ── Decision thresholds ────────────────────────────────────────────────────────
# Recalibrated against 98-file vendor corpus (ground truth verified SGML).
# Lower thresholds reflect real-world PDF extraction variance and format differences.
THRESHOLD_ACCEPT = 90               # was 95: vendor corpus avg now 90.9
THRESHOLD_ACCEPT_WITH_WARNINGS = 85  # was 90
THRESHOLD_REVIEW = 80               # was 85

# Minimum pass score for individual levels (below = automatic REVIEW)
L1_MIN_PASS = 20   # out of 35 — "most content is there" (57%)
L2_MIN_PASS = 30   # out of 40 — "mostly well-formed"
L4_MIN_PASS = 10   # out of 30 — source comparison must not be catastrophically wrong

# Score weights: L1(35) + L2(40) + L3(25) + L4(30) = 130 pts → normalised to 100
_TOTAL_MAX = 130.0


# ── ValidationReport dataclass ─────────────────────────────────────────────────
@dataclass
class ValidationReport:
    # Files
    sgml_path: str = ""
    pdf_path: str = ""

    # Scores (raw)
    l1_score: float = 0.0
    l2_score: float = 0.0
    l3_score: float = 0.0
    l4_score: float = 0.0
    total_score: float = 0.0       # raw sum
    normalised_score: float = 0.0  # scaled to 100

    # Sub-results
    l1: Optional[L1Result] = None
    l2: Optional[L2Result] = None
    l3: Optional[L3Result] = None
    l4: Optional[L4Result] = None

    # Outcome
    decision: str = "REJECT"     # ACCEPT | ACCEPT_WITH_WARNINGS | REVIEW | REJECT
    critical_failures: list[str] = field(default_factory=list)
    all_issues: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # Metadata
    error: Optional[str] = None  # set if validation itself threw an exception

    def to_dict(self) -> dict:
        """Serialise to a JSON-compatible dict."""
        return {
            "sgml_path": self.sgml_path,
            "pdf_path": self.pdf_path,
            "scores": {
                "l1_content_fidelity": round(self.l1_score, 2),
                "l2_structural": round(self.l2_score, 2),
                "l3_corpus_pattern": round(self.l3_score, 2),
                "l4_source_comparison": round(self.l4_score, 2),
                "total_raw": round(self.total_score, 2),
                "normalised": round(self.normalised_score, 2),
            },
            "decision": self.decision,
            "critical_failures": self.critical_failures,
            "issues": self.all_issues,
            "warnings": self.warnings,
            "l1_details": {
                "text_completeness_score": round(self.l1.text_score, 2) if self.l1 else None,
                "section_completeness_score": round(self.l1.section_score, 2) if self.l1 else None,
                "table_completeness_score": round(self.l1.table_score, 2) if self.l1 else None,
                "footnote_completeness_score": round(self.l1.footnote_score, 2) if self.l1 else None,
                "critical_failure": self.l1.critical_failure if self.l1 else False,
            } if self.l1 else None,
            "l2_details": {
                "schema_score": round(self.l2.schema_score, 2) if self.l2 else None,
                "nesting_score": round(self.l2.nesting_score, 2) if self.l2 else None,
                "entity_score": round(self.l2.entity_score, 2) if self.l2 else None,
                "table_score": round(self.l2.table_score, 2) if self.l2 else None,
                "graphics_score": round(self.l2.graphics_score, 2) if self.l2 else None,
                "content_rules_score": round(self.l2.content_score, 2) if self.l2 else None,
                "legal_structure_score": round(self.l2.legal_score, 2) if self.l2 else None,
                "critical_failure": self.l2.critical_failure if self.l2 else False,
            } if self.l2 else None,
            "l3_details": {
                "jurisdiction_score": round(self.l3.jurisdiction_score, 2) if self.l3 else None,
                "statistical_score": round(self.l3.statistical_score, 2) if self.l3 else None,
                "pattern_score": round(self.l3.pattern_score, 2) if self.l3 else None,
                "detected_jurisdiction": self.l3.detected_jurisdiction if self.l3 else None,
                "detected_doc_type": self.l3.detected_doc_type if self.l3 else None,
                "anomalies": self.l3.anomalies if self.l3 else [],
            } if self.l3 else None,
            "l4_details": {
                "tagging_score": round(self.l4.tagging_score, 2) if self.l4 else None,
                "text_score": round(self.l4.text_score, 2) if self.l4 else None,
                "completeness_score": round(self.l4.completeness_score, 2) if self.l4 else None,
                "ordering_score": round(self.l4.ordering_score, 2) if self.l4 else None,
                "encoding_score": round(self.l4.encoding_score, 2) if self.l4 else None,
                "metadata_score": round(self.l4.metadata_score, 2) if self.l4 else None,
                "text_coverage": round(self.l4.text_coverage, 3) if self.l4 else None,
                "pdf_text_extractable": self.l4.pdf_text_extractable if self.l4 else None,
                "encoding_violations": self.l4.encoding_violations if self.l4 else [],
                "metadata_mismatches": self.l4.metadata_mismatches if self.l4 else [],
            } if self.l4 else None,
            "error": self.error,
        }


# ── Main validate function ─────────────────────────────────────────────────────
def validate(
    sgml_path: str,
    pdf_path: Optional[str] = None,
    docx_path: Optional[str] = None,
    run_l3: bool = True,
) -> ValidationReport:
    """
    Validate a pipeline-generated SGML file against its source PDF.

    Parameters
    ----------
    sgml_path  : str           — path to the pipeline-output SGML file
    pdf_path   : str, optional — path to the source PDF. When None, L1 is skipped
                                  and only L2/L3/L4-D6 (encoding) validation runs.
    docx_path  : str, optional — path to the ABBYY-generated DOCX (intermediate file).
                                  When provided, D3 uses two-stage comparison to
                                  separate ABBYY errors from pipeline errors, reducing
                                  false positives from 20-30 % to ~5-10 %.
    run_l3     : bool          — set False to skip corpus comparison

    Returns
    -------
    ValidationReport  (normalised_score is the primary field for decisions)
    """
    report = ValidationReport(
        sgml_path=str(sgml_path),
        pdf_path=str(pdf_path) if pdf_path else "",
    )

    try:
        # ── Read SGML ─────────────────────────────────────────────────────────
        raw_sgml = Path(sgml_path).read_text(encoding="utf-8", errors="replace")

        # ── Pre-classification (RUNS BEFORE L1/L2/L3) ──────────────────────
        # Detects: AMENDMENT (36/98), TSX_SPECIAL (5/98), INSTRUMENT, NOTICE
        # Prevents 47+ false failures in downstream validation layers.
        doc_class = pre_classify(
            raw_sgml,
            file_path=str(sgml_path),
            filename=Path(sgml_path).name,
        )
        report.warnings.append(
            f"[Pre-classified] type={doc_class.doc_type} "
            f"lang={doc_class.lang} "
            f"jur={doc_class.jurisdiction} "
            f"tsx_special={doc_class.is_tsx_special} "
            f"amendment={doc_class.has_quote} "
            f"confidence={doc_class.confidence:.0%}"
        )

        # ── Level 1: Content Fidelity ─────────────────────────────────────
        # When no PDF is provided (vendor SGML-only mode), skip L1 and give
        # neutral half-marks so the doc is not unfairly penalised.
        if pdf_path and Path(pdf_path).exists():
            pdf_content = extract_pdf_content(str(pdf_path))
            sgml_content = extract_sgml_content(raw_sgml)
            l1 = validate_content(pdf_content, sgml_content, doc_class)
        else:
            l1 = L1Result(score=17.5)   # half of 35 — neutral
            l1.issues = []
            l1.warnings = ["L1 skipped — no source PDF provided"]
        report.l1 = l1
        report.l1_score = l1.score

        # ── Level 2: Structural ───────────────────────────────────────────────
        l2 = validate_structure(raw_sgml, doc_class)
        report.l2 = l2
        report.l2_score = l2.score

        # ── Level 3: Corpus Pattern ───────────────────────────────────────────
        if run_l3:
            l3 = validate_against_corpus(raw_sgml, file_path=str(sgml_path))
            # When corpus is unavailable, do NOT score 0/25 (would block ACCEPT).
            # Give neutral half-marks so documents are not incorrectly rejected on
            # days when corpus_patterns.json is missing/corrupted.
            if not l3.corpus_available:
                l3.score = 12.5
                l3.warnings.append(
                    "L3: corpus_patterns.json unavailable — neutral score (12.5/25) assigned. "
                    "Corpus file must be restored for full validation."
                )
        else:
            l3 = L3Result(score=0.0, corpus_available=False)
            l3.warnings.append("L3 skipped by caller.")
        report.l3 = l3
        report.l3_score = l3.score

        # ── Level 4: Source Comparison (D2–D7) ───────────────────────────────
        l4 = validate_source_comparison(
            raw_sgml,
            pdf_path=str(pdf_path) if pdf_path else None,
            docx_path=str(docx_path) if docx_path else None,
        )
        report.l4 = l4
        report.l4_score = l4.score

        # ── Total Score (raw → normalised to 100) ────────────────────────
        # When PDF is unavailable only D6 (encoding, max 3 pts) runs in L4.
        # Scoring that 3/30 against the full 130-pt max would suppress good
        # documents unfairly, so we adjust the denominator:
        #   full run:   L1(35)+L2(40)+L3(25)+L4(30) = 130
        #   no-PDF run: L1(35)+L2(40)+L3(25)+D6(3)  = 103
        #   no-L3 run:  L1(35)+L2(40)+L4(30)        = 105
        _l3_max = 25.0 if run_l3 else 0.0
        _effective_max = (_l3_max + 35.0 + 40.0 + 30.0) if l4.pdf_available else (_l3_max + 35.0 + 40.0 + 3.0)
        report.total_score = (
            report.l1_score + report.l2_score +
            report.l3_score + report.l4_score
        )
        report.normalised_score = min(100.0, (report.total_score / _effective_max) * 100.0)

        # ── Collect All Issues ────────────────────────────────────────────────
        report.all_issues = (
            list(l1.issues if hasattr(l1, "issues") and l1.issues else []) +
            list(l2.issues if l2.issues else []) +
            list(l3.issues if l3.issues else []) +
            list(l4.issues if l4.issues else [])
        )
        report.warnings = (
            list(l1.warnings if hasattr(l1, "warnings") and l1.warnings else []) +
            list(l2.warnings if l2.warnings else []) +
            list(l3.warnings if l3.warnings else []) +
            list(l4.warnings if l4.warnings else [])
        )

        # ── Critical Failure Detection ────────────────────────────────────────
        if hasattr(l1, "critical_failure") and l1.critical_failure:
            report.critical_failures.append("L1_CRITICAL: Text content critically deficient")
        if l2.critical_failure:
            report.critical_failures.append("L2_CRITICAL: Structural fatal error detected")

        # L2_EMPTY_ITEM: Completely empty <ITEM> elements — content was deleted.
        # Like L4_EMPTY_FOOTNOTE, forces at least REVIEW (not REJECT) so a human
        # can assess whether content deletion is intentional.
        _empty_items = getattr(l2, "empty_item_count", 0)
        if _empty_items > 0:
            report.critical_failures.append(
                f"L2_EMPTY_ITEM: {_empty_items} <ITEM> element(s) are completely empty — "
                f"list item content appears to have been deleted. Human review required."
            )

        # L2_ORPHAN_TBLCELL: TBLCELL elements found outside TBLROW — table row wrappers deleted.
        # Forces REVIEW so a human can assess whether table content was intentionally removed.
        _orphan_tblcell = getattr(l2, "orphan_tblcell_count", 0)
        if _orphan_tblcell > 0:
            report.critical_failures.append(
                f"L2_ORPHAN_TBLCELL: {_orphan_tblcell} <TBLCELL> element(s) found outside "
                f"<TBLROW> — table row wrappers appear to have been deleted. Human review required."
            )

        # ── L4 content-deletion escalation ───────────────────────────────────
        # ONLY trigger on LLM-confirmed issues (Opus-verified truncations and
        # mutations). Deterministic inline_changed_paragraphs have a high FP
        # rate and are already reflected in the L4 score deduction.
        # This prevents the 89% false-positive rate on correct vendor files.
        #
        # Double-confirmation: LLM outputs are non-deterministic — the same
        # file can produce different mutation/truncation counts on different
        # runs. To reduce these non-determinism FPs (~5% rate observed on
        # clean vendor files), we re-run L4 once if it flags LLM issues and
        # only escalate when BOTH runs independently confirm the finding.
        if l4 and (
            getattr(l4, "llm_confirmed_truncations", 0) > 0
            or getattr(l4, "llm_confirmed_mutations", 0) > 0
        ):
            try:
                l4_confirm = validate_source_comparison(
                    raw_sgml,
                    pdf_path=str(pdf_path) if pdf_path else None,
                    docx_path=str(docx_path) if docx_path else None,
                )
                _confirmed_trunc = min(
                    getattr(l4, "llm_confirmed_truncations", 0),
                    getattr(l4_confirm, "llm_confirmed_truncations", 0),
                )
                _confirmed_mut = min(
                    getattr(l4, "llm_confirmed_mutations", 0),
                    getattr(l4_confirm, "llm_confirmed_mutations", 0),
                )
            except Exception:
                # If confirmation run fails, fall back to first-run counts
                _confirmed_trunc = getattr(l4, "llm_confirmed_truncations", 0)
                _confirmed_mut   = getattr(l4, "llm_confirmed_mutations", 0)
            if _confirmed_trunc > 0 or _confirmed_mut > 0:
                report.critical_failures.append(
                    f"L4_CONTENT_CHANGED: {_confirmed_trunc} LLM-confirmed "
                    f"truncated paragraph(s) and {_confirmed_mut} LLM-confirmed "
                    f"mutated paragraph(s) — human review required"
                )

        # D4-fn: Empty FREEFORM blocks inside FOOTNOTE tags — deterministic,
        # no LLM required. Legitimate SGML never has empty footnote bodies.
        _empty_fn = getattr(l4, "empty_footnote_bodies", 0)
        if _empty_fn > 0:
            report.critical_failures.append(
                f"L4_EMPTY_FOOTNOTE: {_empty_fn} footnote body/bodies completely "
                f"empty — text was removed from inside a <FOOTNOTE><FREEFORM> block. "
                f"Human review required to restore deleted content."
            )

        # ── Decision ──────────────────────────────────────────────────────────
        report.decision = _make_decision(
            normalised=report.normalised_score,
            l1=report.l1_score,
            l2=report.l2_score,
            l4=report.l4_score,
            critical_failures=report.critical_failures,
            l4_pdf_available=l4.pdf_available if l4 else True,
        )

        # ── Pattern B mitigation: ACCEPT despite PDF paragraphs missing ───────
        # High-baseline documents (>97% score) can have content deleted without
        # the score dropping below the ACCEPT threshold.  When the L1 content
        # check detected that one or more PDF paragraphs are absent from the SGML,
        # downgrade ACCEPT → ACCEPT_WITH_WARNINGS so a human reviewer is directed
        # to inspect the specific D3 fix cards.
        # NOTE: We do NOT downgrade REVIEW or REJECT — those already require review.
        _l1_missing = getattr(l1, "total_missing_para_count", 0) if l1 else 0
        if report.decision == "ACCEPT" and _l1_missing > 0:
            report.decision = "ACCEPT_WITH_WARNINGS"
            report.warnings.append(
                f"Downgraded ACCEPT → ACCEPT_WITH_WARNINGS: "
                f"{_l1_missing} PDF paragraph(s) not found in SGML (L1 content check). "
                "Review D3 fix cards for missing content details."
            )

    except Exception as exc:
        report.error = traceback.format_exc()
        report.decision = "REJECT"
        report.warnings.append(f"Validation error: {exc}")

    return report


def _make_decision(
    normalised: float,
    l1: float,
    l2: float,
    l4: float,
    critical_failures: list[str],
    l4_pdf_available: bool = True,
) -> str:
    """Apply decision thresholds with critical-failure override."""

    # L4 content-deletion issues force REVIEW (not REJECT) — human must check
    # but document is not necessarily broken structurally.
    _l4_content_changed = any(
        ("L4_CONTENT_CHANGED" in f or "L4_EMPTY_FOOTNOTE" in f or
         "L2_EMPTY_ITEM" in f or "L2_ORPHAN_TBLCELL" in f)
        for f in (critical_failures or [])
    )
    _hard_failures = [
        f for f in (critical_failures or [])
        if "L4_CONTENT_CHANGED" not in f and "L4_EMPTY_FOOTNOTE" not in f
        and "L2_EMPTY_ITEM" not in f and "L2_ORPHAN_TBLCELL" not in f
    ]

    if _hard_failures:
        return "REJECT"

    # Hard floor on structural quality
    if l2 < L2_MIN_PASS:
        return "REJECT"

    # Hard floor on content presence
    if l1 < L1_MIN_PASS:
        return "REVIEW"

    # Hard floor on source comparison — only meaningful when PDF was available.
    # Without a PDF only D6 (encoding, max 3 pts) runs; the low raw score is
    # expected and must not force every no-PDF document into REVIEW.
    if l4_pdf_available and l4 < L4_MIN_PASS:
        return "REVIEW"

    # L4 content-deletion escalation: force at least REVIEW regardless of total score
    if _l4_content_changed:
        return "REVIEW"

    if normalised >= THRESHOLD_ACCEPT:
        return "ACCEPT"
    if normalised >= THRESHOLD_ACCEPT_WITH_WARNINGS:
        return "ACCEPT_WITH_WARNINGS"
    if normalised >= THRESHOLD_REVIEW:
        return "REVIEW"
    return "REJECT"


# ── CLI ────────────────────────────────────────────────────────────────────────
def _print_report(report: ValidationReport) -> None:
    """Pretty-print a validation report to stdout."""
    import json
    d = report.to_dict()
    print(json.dumps(d, indent=2, ensure_ascii=False))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="TR SGML Validator — validate pipeline output against source PDF"
    )
    parser.add_argument("sgml", nargs="?", help="SGML file to validate")
    parser.add_argument("pdf", nargs="?", help="Source PDF file (optional)")
    parser.add_argument("--batch", metavar="DIR",
                        help="Validate all .sgm files in directory")
    parser.add_argument("--no-l3", action="store_true",
                        help="Skip Level 3 corpus comparison")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON (default: JSON)")
    args = parser.parse_args(argv)

    if args.batch:
        return _batch_mode(args.batch, run_l3=not args.no_l3)

    if not args.sgml:
        parser.print_help()
        return 1

    report = validate(args.sgml, args.pdf, run_l3=not args.no_l3)
    _print_report(report)
    return 0 if report.decision in ("ACCEPT", "ACCEPT_WITH_WARNINGS") else 1


def _batch_mode(directory: str, run_l3: bool = True) -> int:
    """Validate all .sgm files in a directory."""
    import json
    from pathlib import Path

    sgm_files = list(Path(directory).rglob("*.sgm"))
    if not sgm_files:
        print(f"No .sgm files found in {directory}")
        return 1

    results = []
    pass_count = 0
    total = len(sgm_files)

    for sgm_path in sorted(sgm_files):
        # Try to find matching PDF
        pdf_path = sgm_path.with_suffix(".pdf")
        if not pdf_path.exists():
            pdf_path = None

        report = validate(str(sgm_path), str(pdf_path) if pdf_path else None, run_l3=run_l3)
        results.append(report.to_dict())

        status = "+" if report.decision in ("ACCEPT", "ACCEPT_WITH_WARNINGS") else "x"
        if report.decision in ("ACCEPT", "ACCEPT_WITH_WARNINGS"):
            pass_count += 1
        print(
            f"  {status} {sgm_path.name:40s} "
            f"L1={report.l1_score:5.1f} L2={report.l2_score:5.1f} L3={report.l3_score:5.1f} "
            f"Total={report.total_score:5.1f} → {report.decision}"
        )

    print(f"\nSummary: {pass_count}/{total} passed ({100*pass_count/total:.1f}%)")

    out_path = Path(directory) / "validation_results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"Results written to {out_path}")

    return 0 if pass_count == total else 1


if __name__ == "__main__":
    sys.exit(main())
