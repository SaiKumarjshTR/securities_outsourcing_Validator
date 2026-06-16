"""
config.py — TR SGML Pipeline: Deployment Configuration
═══════════════════════════════════════════════════════
All environment-specific settings live here.
No hardcoded paths appear anywhere else in the codebase.

Configuration priority (highest → lowest):
  1. Environment variables (set in shell or .env file)
  2. Defaults defined below (safe for single-machine installs)

Environment variables
─────────────────────
  CORPUS_DIR       Path to the folder containing matched .sgm + .pdf file pairs
                   (required only for regression tests; HITL app does not need it)

  DECISIONS_FILE   Path to the JSONL file where HITL decisions are written
                   Default: decisions/hitl_decisions.jsonl  (relative to this file)

  PYTHONUTF8       Set to 1 — always required when running on Windows

Usage
─────
  from config import CORPUS_DIR, DECISIONS_FILE
"""

import os
from pathlib import Path

# ── Base directory (the folder that contains this file) ───────────────────────
BASE_DIR = Path(__file__).parent.resolve()

# ── Corpus directory ──────────────────────────────────────────────────────────
# Used by regression tests only.  Set via environment variable so the path
# is not hardcoded in any source file.
#
#   Windows PowerShell:  $env:CORPUS_DIR = "D:\data\sgm_pairs"
#   Linux / macOS:       export CORPUS_DIR=/data/sgm_pairs
#
_corpus_env = os.environ.get("CORPUS_DIR", "").strip()
CORPUS_DIR: Path | None = Path(_corpus_env) if _corpus_env else None

# ── HITL decisions log ────────────────────────────────────────────────────────
# Written by hitl_review.py whenever the reviewer records a decision.
_decisions_env = os.environ.get("DECISIONS_FILE", "").strip()
DECISIONS_FILE: Path = (
    Path(_decisions_env)
    if _decisions_env
    else BASE_DIR / "decisions" / "hitl_decisions.jsonl"
)
DECISIONS_FILE.parent.mkdir(parents=True, exist_ok=True)


def require_corpus_dir() -> Path:
    """
    Return CORPUS_DIR, raising a clear error if it has not been set.
    Call this from regression tests, not from the HITL app.
    """
    if CORPUS_DIR is None or not CORPUS_DIR.exists():
        raise EnvironmentError(
            "\n"
            "CORPUS_DIR is not set or does not exist.\n"
            "Set it before running regression tests:\n"
            "\n"
            "  Windows PowerShell:\n"
            '    $env:CORPUS_DIR = "D:\\data\\your_sgm_pdf_folder"\n'
            "\n"
            "  Linux / macOS:\n"
            "    export CORPUS_DIR=/data/your_sgm_pdf_folder\n"
        )
    return CORPUS_DIR
