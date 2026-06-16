"""
tests/test_all_vendor_files.py
────────────────────────────────
Calibration harness: run the validator on all 90 vendor PDF-SGM pairs
and measure accuracy against targets:

  Target:  ≥98% of vendor files score ACCEPT or ACCEPT_WITH_WARNINGS
  Target:  <2% false-positive REJECT rate on good vendor docs

Usage
─────
  python -m validator.tests.test_all_vendor_files  <juri_zip>  [pdf_dir]
  python -m validator.tests.test_all_vendor_files  <juri_zip>  [pdf_dir]  --no-l3
  python -m validator.tests.test_all_vendor_files  <juri_zip>  [pdf_dir]  --xlsx

Arguments
─────────
  juri_zip  : path to juri.zip containing vendor .sgm files
  pdf_dir   : directory containing matching PDFs (named as DOCNUM.pdf)
               if omitted, L1 content check is skipped
"""

import argparse
import json
import sys
import zipfile
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

# Ensure the repo root is on sys.path when run directly
_REPO_ROOT = Path(__file__).parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from validator.validator_main import validate, ValidationReport
from validator.reports.report_generator import (
    save_excel_report,
    format_summary,
)


def find_pdf(pdf_dir: Optional[Path], sgm_name: str) -> Optional[str]:
    """Find the matching PDF for a given SGM filename."""
    if not pdf_dir:
        return None

    # Try exact basename match
    stem = Path(sgm_name).stem
    candidates = [
        pdf_dir / f"{stem}.pdf",
        pdf_dir / f"{stem.upper()}.pdf",
        pdf_dir / f"{stem.lower()}.pdf",
    ]
    # Also search subdirectories
    for path in candidates:
        if path.exists():
            return str(path)

    # Deep search
    matches = list(pdf_dir.rglob(f"{stem}.pdf"))
    if matches:
        return str(matches[0])
    return None


def run_all_vendor_tests(
    juri_zip: str,
    pdf_dir: Optional[str] = None,
    run_l3: bool = True,
    output_xlsx: Optional[str] = None,
) -> tuple[list[ValidationReport], dict]:
    """
    Run validator on all vendor files in juri.zip.

    PDFs are extracted from juri.zip itself (co-located with SGMs at the
    same path, just with .pdf extension).  The pdf_dir argument can be used
    to override with an external directory.

    Returns
    -------
    (reports, summary_stats)
    """
    pdf_dir_path = Path(pdf_dir) if pdf_dir else None
    reports: list[ValidationReport] = []

    with zipfile.ZipFile(juri_zip) as z:
        all_names = z.namelist()
        sgm_files = sorted([n for n in all_names if n.endswith(".sgm")])
        pdf_names_in_zip = set(n for n in all_names if n.endswith(".pdf"))
        print(f"\nFound {len(sgm_files)} vendor SGM files in {Path(juri_zip).name}")
        print(f"Found {len(pdf_names_in_zip)} PDFs in zip for pairing")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)

            for i, fname in enumerate(sgm_files, 1):
                # Extract SGM to temp file
                data = z.read(fname)
                sgm_name = fname.split("/")[-1]
                sgm_tmp = tmp_path / sgm_name
                sgm_tmp.write_bytes(data)

                # Find matching PDF — first from external dir, then from zip
                pdf_file = find_pdf(pdf_dir_path, sgm_name) if pdf_dir_path else None
                if not pdf_file:
                    # Try co-located PDF in zip (same path, .pdf extension)
                    pdf_zip_path = fname.replace(".sgm", ".pdf")
                    if pdf_zip_path in pdf_names_in_zip:
                        pdf_tmp = tmp_path / sgm_name.replace(".sgm", ".pdf")
                        if not pdf_tmp.exists():
                            pdf_tmp.write_bytes(z.read(pdf_zip_path))
                        pdf_file = str(pdf_tmp)

                # Run validation
                report = validate(str(sgm_tmp), pdf_file, run_l3=run_l3)
                # Preserve the original zip path for jurisdiction detection
                report.sgml_path = fname
                reports.append(report)

                status = {
                    "ACCEPT": "+ ACCEPT",
                    "ACCEPT_WITH_WARNINGS": "~ WARN",
                    "REVIEW": "? REVIEW",
                    "REJECT": "x REJECT",
                }.get(report.decision, "? UNKNOWN")

                pdf_marker = "+" if pdf_file else "-"
                print(
                    f"  [{i:3d}/{len(sgm_files)}] [{pdf_marker}PDF] "
                    f"{sgm_name:35s} "
                    f"L1={report.l1_score:5.1f} L2={report.l2_score:5.1f} "
                    f"L3={report.l3_score:5.1f} "
                    f"T={report.total_score:5.1f} -> {status}"
                )

                if report.critical_failures:
                    for cf in report.critical_failures:
                        print(f"          !! {cf}")

    # ── Summary statistics ─────────────────────────────────────────────────────
    decisions = Counter(r.decision for r in reports)
    total = len(reports)
    passed = decisions.get("ACCEPT", 0) + decisions.get("ACCEPT_WITH_WARNINGS", 0)

    # Scores
    l1s = [r.l1_score for r in reports]
    l2s = [r.l2_score for r in reports]
    l3s = [r.l3_score for r in reports]
    totals = [r.total_score for r in reports]

    def stats(vals):
        n = len(vals)
        if n == 0:
            return {}
        avg = sum(vals) / n
        return {
            "mean": round(avg, 2),
            "min": round(min(vals), 2),
            "max": round(max(vals), 2),
            "p10": round(sorted(vals)[max(0, int(n * 0.10))], 2),
            "p90": round(sorted(vals)[min(n - 1, int(n * 0.90))], 2),
        }

    # By jurisdiction
    by_juri: dict[str, list] = defaultdict(list)
    for r in reports:
        juri = r.l3.detected_jurisdiction if r.l3 else "unknown"
        by_juri[juri].append(r)

    juri_stats = {}
    for juri, jreports in by_juri.items():
        jd = Counter(jr.decision for jr in jreports)
        jpass = jd.get("ACCEPT", 0) + jd.get("ACCEPT_WITH_WARNINGS", 0)
        juri_stats[juri] = {
            "count": len(jreports),
            "passed": jpass,
            "pass_rate": round(100 * jpass / len(jreports), 1),
            "decisions": dict(jd),
        }

    summary = {
        "total": total,
        "passed": passed,
        "pass_rate_pct": round(100 * passed / total, 2) if total else 0,
        "decisions": dict(decisions),
        "target_met": (100 * passed / total) >= 98.0 if total else False,
        "l1_score_stats": stats(l1s),
        "l2_score_stats": stats(l2s),
        "l3_score_stats": stats(l3s),
        "total_score_stats": stats(totals),
        "by_jurisdiction": juri_stats,
    }

    # ── Print results ──────────────────────────────────────────────────────────
    print(format_summary(reports, title="Vendor Corpus Calibration Results"))
    print(f"  Pass rate:  {summary['pass_rate_pct']:.1f}%  (target: ≥98%)")
    target_label = "[PASS] TARGET MET" if summary["target_met"] else "[FAIL] TARGET NOT MET"
    print(f"  {target_label}")
    print()

    # Failing files detail
    failing = [r for r in reports if r.decision in ("REJECT", "REVIEW")]
    if failing:
        print(f"  Files needing attention ({len(failing)}):")
        for r in failing[:20]:  # show at most 20
            name = Path(r.sgml_path).name
            issues_str = "; ".join(
                i.get("description", "")[:60]
                for i in (r.all_issues or [])[:3]
            )
            print(f"    {r.decision:8s} {name:40s} {r.total_score:5.1f}")
            if issues_str:
                print(f"             -> {issues_str}")

    # ── Save Excel ─────────────────────────────────────────────────────────────
    if output_xlsx:
        save_excel_report(reports, output_xlsx)

    # ── Save JSON summary ──────────────────────────────────────────────────────
    summary_path = Path(juri_zip).parent / "calibration_summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"  Summary saved → {summary_path}")

    return reports, summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Calibrate validator on all 98 vendor SGM files"
    )
    parser.add_argument("juri_zip", help="Path to juri.zip")
    parser.add_argument("pdf_dir", nargs="?", help="Directory containing source PDFs")
    parser.add_argument("--no-l3", action="store_true",
                        help="Skip L3 corpus comparison (for bootstrapping)")
    parser.add_argument("--xlsx", metavar="FILE",
                        help="Save Excel report to this file")
    args = parser.parse_args(argv)

    _, summary = run_all_vendor_tests(
        juri_zip=args.juri_zip,
        pdf_dir=args.pdf_dir,
        run_l3=not args.no_l3,
        output_xlsx=args.xlsx,
    )

    return 0 if summary.get("target_met") else 1


if __name__ == "__main__":
    sys.exit(main())
