# TR SGML Validator

Deterministic SGML validation tool for Carswell/TR legal publishing pipeline.  
Validates pipeline-generated SGML files against their source PDFs across four scoring levels.

---

## Scoring System

| Level | Dimension | Points |
|---|---|---|
| L1 | Content Fidelity (text coverage, headings, tables, footnotes) | 35 |
| L2 | Structural Compliance (tags, nesting, entities, legal structure) | 40 |
| L3 | Corpus Pattern (jurisdiction baseline, statistical anomaly) | 25 |
| L4 | Source Comparison (PDF ↔ SGML word-level diff, contact info) | 30 |
| | **Total (normalised to 100)** | **100** |

**Decision thresholds:** ACCEPT ≥ 90 · ACCEPT_WITH_WARNINGS ≥ 85 · REVIEW ≥ 80 · REJECT < 80

---

## Requirements

- Python 3.10+  
- Windows (for production); cross-platform for validation-only mode

```bash
pip install -r requirements.txt
```

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `PYTHONUTF8` | Yes | Set to `1` — always required on Windows |
| `CORPUS_DIR` | Tests only | Path to folder with `.sgm` + `.pdf` pairs for regression tests |
| `DECISIONS_FILE` | Optional | Override path for HITL decision log (default: `decisions/hitl_decisions.jsonl`) |

---

## Running the HITL Review App

```powershell
$env:PYTHONUTF8 = "1"
streamlit run validator_app.py
```

Opens at `http://localhost:8501` — upload an SGML + PDF pair to validate.

---

## Running the Validator Programmatically

```python
from validator.validator_main import validate

report = validate("output.sgm", pdf_path="source.pdf")
print(report.decision)       # ACCEPT / ACCEPT_WITH_WARNINGS / REVIEW / REJECT
print(report.normalised_score)   # 0–100
for issue in report.l4_result.issues:
    print(issue)
```

---

## Project Structure

```
├── validator/                        # Core validator package
│   ├── validator_main.py             # Orchestrator — call validate() from here
│   ├── core/                         # Shared utilities (parser, entities, diff)
│   ├── level1_content/               # L1: text coverage, headings, tables, footnotes
│   ├── level2_structural/            # L2: tag schema, nesting, entity handling
│   ├── level3_corpus/                # L3: jurisdiction corpus patterns
│   ├── level4_source_compare/        # L4: PDF↔SGML word diff (D8/D8-b/D8-c/D9)
│   ├── pdf/                          # PDF extraction utilities
│   ├── reports/                      # Report generation (text, CSV)
│   └── tests/                        # Regression test suite
├── pipeline/                         # Excel pipeline integration
├── config.py                         # Environment-variable driven config (no hardcoded paths)
├── validator_app.py                  # Streamlit entry point (PDF HITL + Excel HITL)
├── hitl_review.py                    # PDF HITL review UI
├── excel_hitl.py                     # Excel HITL review UI
├── entities_list.txt                 # Carswell SGML entity definitions (250+ entities)
├── .streamlit/config.toml            # Streamlit server config (port 8501, headless)
├── decisions/                        # HITL decision log (runtime, git-ignored)
└── requirements.txt
```

---

## Key L4 Checks (D8 / D8-b / D8-c / D9)

| Check | Description |
|---|---|
| **D8** | Global word-level diff — phrases of ≥4 words present in PDF but absent from SGML |
| **D8-b** | Paragraph truncation — SGML covers < 60% of a PDF paragraph's words |
| **D8-c** | Short bold phrases (2–3 words) from PDF completely absent from SGML |
| **D9** | Contact info gaps — phone numbers, email addresses, URLs in PDF but not SGML |

---

## No External Service Dependencies

- No LLM / API calls
- No ABBYY runtime required (DOCX path is optional; PDF-only validation is the default)
- No hardcoded machine paths — all paths resolved relative to `config.py` or via env vars
