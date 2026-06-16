"""
validator/__init__.py
─────────────────────
TR SGML Validator

Validates pipeline-generated SGML files against source PDFs using a
three-level scoring system:

  L1 Content Fidelity   (35 pts)
  L2 Structural         (40 pts)
  L3 Corpus Pattern     (25 pts)

Quick start
───────────
  from validator.validator_main import validate
  report = validate("output.sgm", "source.pdf")
  print(report.decision, report.total_score)
"""

__version__ = "1.0.0"
