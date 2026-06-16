"""
test_excel_regression.py — Batch regression runner for the Excel → SGML validator.

Usage:
    $env:PYTHONUTF8=1
    $env:CORPUS_DIR = "C:\\path\\to\\corpus"   # folder with .sgm + .xlsx pairs
    python validator/tests/test_excel_regression.py

    # Filter by minimum score threshold (default 0):
    python validator/tests/test_excel_regression.py --min-score 80

    # Write a full JSON results file:
    python validator/tests/test_excel_regression.py --out results.json

    # Verbose — print each file's issues:
    python validator/tests/test_excel_regression.py -v

CORPUS_DIR must contain matched pairs:
    document.sgm   (generated SGML output)
    document.xlsx  (source Excel — optional but recommended)

Files without a matching .xlsx are validated with L2/L3/L4 checks only.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

# ── path bootstrap ────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent.parent.parent   # → deployment root
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from config import require_corpus_dir                       # noqa: E402
from pipeline.excel_validator import validate               # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
def _find_pairs(corpus: Path) -> list[tuple[Path, Path | None]]:
    """
    Return (sgm_path, xlsx_path_or_None) for every .sgm file found
    (recursively) under *corpus*.
    """
    pairs = []
    for sgm in sorted(corpus.rglob('*.sgm')):
        xl = sgm.with_suffix('.xlsx')
        pairs.append((sgm, xl if xl.exists() else None))
    return pairs


def _bar(value: float, width: int = 20) -> str:
    filled = round(value / 100 * width)
    return '█' * filled + '░' * (width - filled)


# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description='Excel → SGML batch regression runner',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        '--min-score', type=float, default=0.0, metavar='N',
        help='Only report files scoring below N (default: show all)',
    )
    parser.add_argument(
        '--out', metavar='FILE', default=None,
        help='Write full JSON results to FILE',
    )
    parser.add_argument(
        '-v', '--verbose', action='store_true',
        help='Print individual issue lines for each file',
    )
    args = parser.parse_args()

    corpus = require_corpus_dir()
    pairs  = _find_pairs(corpus)

    if not pairs:
        print(f'ERROR: No .sgm files found under {corpus}', file=sys.stderr)
        sys.exit(1)

    print(f'\nExcel → SGML Regression Suite')
    print(f'Corpus : {corpus}')
    print(f'Files  : {len(pairs)} .sgm file(s)')
    print('─' * 72)

    results: list[dict] = []
    decision_counts: Counter = Counter()
    issue_freq: Counter      = Counter()
    score_buckets: Counter   = Counter()
    failed: list[str]        = []

    for sgm_path, xlsx_path in pairs:
        try:
            r = validate(sgm_path, xlsx_path)
        except Exception as exc:  # noqa: BLE001
            print(f'  ERROR  {sgm_path.name}: {exc}')
            failed.append(sgm_path.name)
            continue

        sc   = r['scores']
        norm = sc['normalised']
        dec  = r['decision']
        decision_counts[dec] += 1

        bucket = f'{int(norm // 10) * 10}–{int(norm // 10) * 10 + 9}'
        score_buckets[bucket] += 1

        for layer in ('L1', 'L2', 'L3', 'L4'):
            for iss in r['issues'].get(layer, []):
                issue_freq[iss.get('id', '?')] += 1

        results.append(r)

        # Per-file line
        flag = ''
        if norm < args.min_score or dec == 'REJECT':
            flag = '  ◄ BELOW THRESHOLD'

        print(
            f'  {dec:<24} {norm:>5.1f}%  {_bar(norm)}  {sgm_path.name}{flag}'
        )

        if args.verbose:
            for layer in ('L1', 'L2', 'L3', 'L4'):
                for iss in r['issues'].get(layer, []):
                    loc = f' line {iss["line"]}' if iss.get('line') else ''
                    print(f'    [{layer}] {iss["sev"]:<8} '
                          f'{iss["id"]}: {iss["msg"]}{loc}')

    # ── Summary ───────────────────────────────────────────────────────────────
    n = len(results)
    if n == 0:
        print('\nNo files processed successfully.')
        sys.exit(1)

    scores = [r['scores']['normalised'] for r in results]
    avg    = sum(scores) / n
    mn, mx = min(scores), max(scores)

    print('\n' + '═' * 72)
    print(f'  Files processed : {n}  (errors: {len(failed)})')
    print(f'  Score avg/min/max : {avg:.1f}% / {mn:.1f}% / {mx:.1f}%')
    print()
    print('  Decision breakdown:')
    for dec in ('ACCEPT', 'ACCEPT_WITH_WARNINGS', 'REVIEW', 'REJECT'):
        cnt = decision_counts[dec]
        pct = 100 * cnt / n if n else 0
        print(f'    {dec:<24} {cnt:>4}  ({pct:>5.1f}%)')

    print()
    print('  Score distribution (10-pt buckets):')
    for bucket in sorted(score_buckets):
        cnt = score_buckets[bucket]
        print(f'    {bucket}  {cnt:>4}  {_bar(cnt * 100 // n if n else 0, 15)}')

    print()
    print('  Top 10 most frequent issue codes:')
    for check_id, cnt in issue_freq.most_common(10):
        print(f'    {check_id:<12} {cnt:>5} occurrence(s)')

    if failed:
        print()
        print(f'  Files with errors ({len(failed)}):')
        for fn in failed:
            print(f'    {fn}')

    print('═' * 72)

    # ── JSON output ───────────────────────────────────────────────────────────
    if args.out:
        out_path = Path(args.out)
        out_path.write_text(
            json.dumps(results, indent=2, ensure_ascii=False),
            encoding='utf-8',
        )
        print(f'\n  Full results → {out_path}')

    # Return non-zero if any REJECT
    if decision_counts.get('REJECT', 0):
        sys.exit(1)


if __name__ == '__main__':
    main()
