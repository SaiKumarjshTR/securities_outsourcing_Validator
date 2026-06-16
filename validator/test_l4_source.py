"""Smoke test for the L4 source comparison validator."""
import sys
sys.path.insert(0, ".")

from validator.level4_source_compare.source_validator import validate_source_comparison

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
errors = 0

def check(label, condition):
    global errors
    print(f"  {PASS if condition else FAIL}  {label}")
    if not condition:
        errors += 1
    return condition

# Clean SGML with correct entities
CLEAN_SGML = """<POLIDOC LANG="EN" LABEL="OSC Staff Notice 51-737" ADDDATE="20251212">
<POLIDENT><N>51-737</N><TI>Corporate Finance Division 2025 Annual Report</TI></POLIDENT>
<FREEFORM>
<BLOCK2><TI>Introduction</TI>
<P>We are proud to share our Corporate Finance Division 2025 Annual Report.</P>
<P>This Report reflects the first operational year of the Corporate Finance Division.</P>
</BLOCK2>
<BLOCK2><TI>Overview of Activities</TI>
<P>The Division continues to prioritize our core regulatory operations.</P>
</BLOCK2>
</FREEFORM></POLIDOC>"""

print("\n── D6 Encoding checks ───────────────────────────────────────────────────")
r = validate_source_comparison(CLEAN_SGML, pdf_path=None)
check("D6 full score (clean SGML)", r.encoding_score == 3.0)
check("No issues on clean SGML", len(r.issues) == 0)
check("Warning about no PDF", any("No source PDF" in w for w in r.warnings))

# SGML with raw Unicode that should be entities
BAD_ENC = CLEAN_SGML.replace(
    "Annual Report.",
    "Annual Report \u2014 Q4 results showed 10\u00b0C above average \u2013 record."
)
r2 = validate_source_comparison(BAD_ENC, pdf_path=None)
check("D6 deducts for em-dash U+2014", r2.encoding_score < 3.0)
check("Encoding violations listed", len(r2.encoding_violations) > 0)
check("D6 issue raised", any("D6" in i["description"] for i in r2.issues))

print("\n── D6 Smart quote detection ─────────────────────────────────────────────")
BAD_QUOTES = CLEAN_SGML.replace(
    "Annual Report.",
    'Annual Report \u201cremarkable\u201d performance.'
)
r3 = validate_source_comparison(BAD_QUOTES, pdf_path=None)
check("Curly quotes detected", any("\u201c" in str(v) or "ldquo" in str(v) for v in r3.encoding_violations))

print("\n── Validator main integration ───────────────────────────────────────────")
from validator.validator_main import validate
import tempfile, os
with tempfile.NamedTemporaryFile(mode='w', suffix='.sgm', delete=False, encoding='utf-8') as f:
    f.write(CLEAN_SGML)
    tmp = f.name
try:
    report = validate(tmp, pdf_path=None, run_l3=False)
    check("validate() returns report", report is not None)
    check("L4 result present", report.l4 is not None)
    check("L4 score in report", report.l4_score >= 0)
    check("normalised_score computed", 0 <= report.normalised_score <= 100)
    check("all_issues list exists", isinstance(report.all_issues, list))
    print(f"  Score: L2={report.l2_score:.1f}  L4={report.l4_score:.1f}  "
          f"normalised={report.normalised_score:.1f}  decision={report.decision}")
finally:
    os.unlink(tmp)

print()
print("=" * 60)
if errors == 0:
    print(f"  {PASS}  All L4 smoke tests passed")
else:
    print(f"  {FAIL}  {errors} test(s) failed")
sys.exit(errors)
