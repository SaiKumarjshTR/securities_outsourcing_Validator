"""
Regression test: run L4 source comparison on all matched PDF+SGM pairs
in the final_results_30_03_26 vendor corpus.

Reports per-dimension averages, decision distribution, and any files
that score critically low on L4.
"""
import sys
sys.path.insert(0, ".")

from pathlib import Path
from collections import defaultdict
from validator.level4_source_compare.source_validator import validate_source_comparison
from config import require_corpus_dir

CORPUS = require_corpus_dir()

# Collect vendor SGM + matching PDF pairs (exclude _TR.sgm reference files)
pairs: list[tuple[Path, Path]] = []
for sgm in sorted(CORPUS.rglob("*.sgm")):
    if sgm.stem.endswith("_TR"):
        continue
    pdf = sgm.with_suffix(".pdf")
    if pdf.exists():
        pairs.append((sgm, pdf))

print(f"Found {len(pairs)} matched SGM+PDF pairs\n")

# Score accumulators
totals = defaultdict(float)
counts = defaultdict(int)
low_score_files: list[tuple[str, float, list]] = []
errors: list[tuple[str, str]] = []

for sgm, pdf in pairs:
    try:
        raw = sgm.read_text(encoding="utf-8", errors="replace")
        r = validate_source_comparison(raw, pdf_path=str(pdf))

        totals["score"] += r.score
        totals["D2"] += r.tagging_score
        totals["D3"] += r.text_score
        totals["D4"] += r.completeness_score
        totals["D5"] += r.ordering_score
        totals["D6"] += r.encoding_score
        totals["D7"] += r.metadata_score
        totals["cov"] += r.text_coverage
        counts["total"] += 1

        pct = r.score / 30.0
        if pct < 0.70:
            counts["critical"] += 1
            issues = [i["description"][:70] for i in r.issues[:2]]
            low_score_files.append((sgm.name, r.score, issues))
        elif pct < 0.85:
            counts["warning"] += 1
        else:
            counts["pass"] += 1

    except Exception as e:
        errors.append((sgm.name, str(e)[:80]))
        counts["error"] += 1

n = counts["total"]
if n == 0:
    print("No files processed.")
    sys.exit(1)

print(f"{'Dimension':<20} {'Avg':>6} {'Max':>5} {'%':>6}")
print("-" * 40)
dims = [("D2 Tagging", "D2", 5), ("D3 Text", "D3", 8),
        ("D4 Completeness", "D4", 7), ("D5 Ordering", "D5", 4),
        ("D6 Encoding", "D6", 3), ("D7 Metadata", "D7", 3)]
for label, key, mx in dims:
    avg = totals[key] / n
    print(f"{label:<20} {avg:>6.2f} {mx:>5}  {avg/mx:>5.0%}")
print("-" * 40)
avg_total = totals["score"] / n
avg_cov   = totals["cov"] / n
print(f"{'TOTAL L4':<20} {avg_total:>6.2f} {'30':>5}  {avg_total/30:>5.0%}")
print(f"{'Text coverage':<20} {avg_cov:>6.1%}")
print()

print(f"Decision distribution (out of {n} files):")
print(f"  PASS  (>=85%): {counts['pass']:>4}  ({counts['pass']/n:>5.0%})")
print(f"  WARN  (70-85%): {counts['warning']:>4}  ({counts['warning']/n:>5.0%})")
print(f"  CRIT  (<70%):  {counts['critical']:>4}  ({counts['critical']/n:>5.0%})")
if errors:
    print(f"  ERROR:         {counts['error']:>4}")
print()

if low_score_files:
    print(f"Files scoring <70% on L4 ({len(low_score_files)}):")
    for name, score, issues in sorted(low_score_files, key=lambda x: x[1]):
        print(f"  {name:<45} {score:.1f}/30")
        for iss in issues:
            print(f"    > {iss}")
    print()

if errors:
    print(f"Errors ({len(errors)}):")
    for name, err in errors[:5]:
        print(f"  {name}: {err}")
