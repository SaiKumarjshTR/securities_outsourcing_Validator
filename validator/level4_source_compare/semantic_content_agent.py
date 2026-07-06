"""
semantic_content_agent.py
══════════════════════════
Replaces D3 (text accuracy) + D8 (word gaps) with a Claude Opus agent
that semantically compares PDF source text against SGML output.

WHY this exists instead of deterministic checks:
  - SequenceMatcher aligns linear word sequences. PDF and SGML have the SAME
    content in DIFFERENT structural order (tables, lists, footnotes reordered).
    Every guard added to suppress one false positive creates a new false
    negative on another document. The cycle cannot be broken deterministically.
  - An LLM understands "Registration Requirements, Exemptions..." inside a
    <BOLD> tag IS the same text as in the PDF — no regex or ngram can reliably
    do this across the full range of Canadian securities document types.

What this agent checks (replaces D3 + D8):
  1. Is substantive PDF content faithfully present in the SGML?
  2. Is any PDF text genuinely MISSING (deletion/truncation)?
  3. Has any text been ALTERED (numbers changed, words replaced)?

What it does NOT check (stays deterministic):
  D2: Bold/italic tagging accuracy
  D4: Table count, image count, appendix presence
  D5: Section ordering
  D6: Entity encoding
  D7: Metadata (title, date, doc number)

Auth: Uses same TR AI Platform pattern as the conversion pipeline.
"""

from __future__ import annotations

import json
import re
import textwrap
from typing import Optional

import httpx
import requests

# ── TR Platform configuration (same as batch_runner_standalone.py) ────────────
_TR_AUTH_URL  = "https://aiplatform.gcs.int.thomsonreuters.com/v1/anthropic/token"
_WORKSPACE_ID = "Saikumar3Y0Z"
_MODEL        = "claude-opus-4-5"

# Maximum characters to send to LLM per call (keep under context window)
_MAX_PDF_CHARS  = 12_000
_MAX_SGML_CHARS = 14_000


# ── Auth helper ───────────────────────────────────────────────────────────────
def _get_anthropic_client():
    """Authenticate with TR AI Platform and return Anthropic client."""
    try:
        resp = requests.post(
            _TR_AUTH_URL,
            json={"workspace_id": _WORKSPACE_ID, "model_name": _MODEL},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        token = resp.json().get("anthropic_api_key") or resp.json().get("token", "")
        if not token:
            return None
        from anthropic import Anthropic
        return Anthropic(api_key=token, http_client=httpx.Client(verify=False))
    except Exception:
        return None


# ── System prompt ─────────────────────────────────────────────────────────────
_SYSTEM_PROMPT = textwrap.dedent("""
You are a legal document quality validator for Thomson Reuters Canada.
Your task: compare a PDF source document against its SGML conversion and
identify GENUINE content issues — not formatting differences.

RULES:
1. SGML tags (<P>, <BOLD>, <EM>, <ITEM>, <BLOCK2>, <TABLE>, <APPENDIX> etc.)
   are structural wrappers — they do NOT mean text is missing.
2. SGML entities (&rsquo; &mdash; &eacute; etc.) are encoding variants —
   treat them as equivalent to their Unicode characters.
3. These are LEGITIMATELY ABSENT from SGML and must NOT be flagged as missing:
   - Page numbers, running headers/footers, copyright lines
   - Table of Contents entries and dot-leaders (…………)
   - Website navigation elements (Home, Sign In, Français)
   - Footnote reference numbers at page bottom (e.g. "19 See NI 33-109")
   - TOC sub-entries (roman numerals: i. ii. iii. iv.)
   - Standalone date lines that are page headers ("June 9, 2023")
   - OSCB bulletin section markers ("B.1 Notices", "B.5 Rules")
4. Text REORDERED across sections is NOT missing — check PRESENCE, not position.
5. Text inside table cells counts as present even if PDF shows it as a paragraph.
6. Bold/italic formatting differences are NOT content issues.
7. Only flag text as MISSING if it is genuinely absent from the entire SGML.
8. Only flag text as ALTERED if numbers, proper names, or regulatory identifiers
   are materially different (e.g. "6.6.1" became "1.1.1", "reasonably" became
   "substantially").

RESPOND ONLY with valid JSON in this exact structure:
{
  "missing_text": [
    {
      "text": "<the missing text, max 200 chars>",
      "location_hint": "<section or context where it should appear>",
      "severity": "critical|major|minor",
      "confidence": 0.0-1.0
    }
  ],
  "altered_text": [
    {
      "original_pdf": "<text as it appears in PDF>",
      "sgml_version": "<text as it appears in SGML>",
      "location_hint": "<section>",
      "severity": "critical|major|minor"
    }
  ],
  "summary": "<one sentence summary of content fidelity>",
  "overall_fidelity": "high|medium|low"
}

If all content is faithfully present: return missing_text=[], altered_text=[],
overall_fidelity="high".
""").strip()


def _build_user_prompt(pdf_text: str, sgml_text: str, doc_type: str) -> str:
    """Build the comparison prompt with PDF and SGML content."""
    # Truncate to fit context window while keeping most important parts
    pdf_excerpt  = pdf_text[:_MAX_PDF_CHARS]
    sgml_excerpt = sgml_text[:_MAX_SGML_CHARS]

    # Warn if truncated
    pdf_note  = f"\n[... PDF truncated at {_MAX_PDF_CHARS} chars ...]" if len(pdf_text)  > _MAX_PDF_CHARS  else ""
    sgml_note = f"\n[... SGML truncated at {_MAX_SGML_CHARS} chars ...]" if len(sgml_text) > _MAX_SGML_CHARS else ""

    return textwrap.dedent(f"""
Document type: {doc_type}

=== PDF SOURCE TEXT (extracted by PyMuPDF — may have minor extraction artifacts) ===
{pdf_excerpt}{pdf_note}

=== SGML CONVERSION (tag-stripped for readability) ===
{sgml_excerpt}{sgml_note}

Compare the PDF source against the SGML conversion.
Identify any text that is GENUINELY MISSING or MATERIALLY ALTERED in the SGML.
Remember: reordering, tag wrapping, and entity encoding are NOT issues.
""").strip()


def _call_agent(pdf_text: str, sgml_text: str, doc_type: str,
                client) -> Optional[dict]:
    """Call Claude Opus and parse the JSON response."""
    user_msg = _build_user_prompt(pdf_text, sgml_text, doc_type)
    try:
        response = client.messages.create(
            model=_MODEL,
            max_tokens=2048,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = response.content[0].text.strip()

        # Extract JSON block (LLM sometimes wraps in markdown)
        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
        return json.loads(raw)
    except Exception as e:
        return {"error": str(e)}


# ── Main entry point ──────────────────────────────────────────────────────────
def check_text_semantic(
    pdf_data,          # _PDFData from source_validator._extract_pdf_data()
    sgml_data: dict,   # from source_validator._extract_sgml_text()
    result,            # L4Result to populate
    doc_type: str = "Notice",
) -> None:
    """
    Semantic replacement for D3 + D8.

    Populates:
      result.text_score            (D3 score, 0-8)
      result.text_coverage         (fraction 0-1)
      result.missing_paragraphs    (list of missing text snippets)
      result.word_gaps             (list of {missing, type} dicts)
      result.inline_changed_paragraphs (altered text)
    And adds issues to result.issues.
    """
    # Authenticate
    client = _get_anthropic_client()
    if client is None:
        result.warnings.append(
            "D3/D8 semantic agent: TR auth failed — falling back to deterministic."
        )
        _fallback_deterministic(pdf_data, sgml_data, result)
        return

    # Build plain-text representations
    pdf_text  = "\n\n".join(pdf_data.paragraphs) if pdf_data.paragraphs else ""
    sgml_text = sgml_data.get("text", "")   # already tag-stripped by _extract_sgml_text()

    if not pdf_text.strip():
        result.text_score = 8.0
        result.text_coverage = 1.0
        result.warnings.append("D3/D8 skipped: no PDF text extractable.")
        return

    # Call the agent
    result.warnings.append(f"D3/D8: SemanticContentAgent running (model: {_MODEL})…")
    agent_result = _call_agent(pdf_text, sgml_text, doc_type, client)

    if not agent_result or "error" in agent_result:
        err = agent_result.get("error", "unknown") if agent_result else "null response"
        result.warnings.append(f"D3/D8 agent failed ({err}) — falling back to deterministic.")
        _fallback_deterministic(pdf_data, sgml_data, result)
        return

    # ── Parse agent output into L4Result ─────────────────────────────────────
    missing   = agent_result.get("missing_text", [])
    altered   = agent_result.get("altered_text", [])
    fidelity  = agent_result.get("overall_fidelity", "high")
    summary   = agent_result.get("summary", "")

    result.missing_paragraphs = [m["text"] for m in missing]
    result.word_gaps = [
        {"missing": m["text"], "type": "semantic", "confidence": m.get("confidence", 1.0)}
        for m in missing
    ]
    result.inline_changed_paragraphs = [
        {"original": a["original_pdf"], "sgml": a["sgml_version"], "location": a.get("location_hint", "")}
        for a in altered
    ]

    # ── Score based on fidelity and issue count ───────────────────────────────
    critical_missing = [m for m in missing if m.get("severity") == "critical"]
    major_missing    = [m for m in missing if m.get("severity") == "major"]
    minor_missing    = [m for m in missing if m.get("severity") == "minor"]
    critical_altered = [a for a in altered if a.get("severity") == "critical"]
    major_altered    = [a for a in altered if a.get("severity") == "major"]

    if fidelity == "high" and not missing and not altered:
        text_score = 8.0
        result.text_coverage = 1.0
    elif fidelity == "high":
        deduct = len(minor_missing) * 0.3 + len(major_missing) * 0.5
        text_score = max(6.0, 8.0 - deduct)
        result.text_coverage = 0.95
    elif fidelity == "medium":
        deduct = len(critical_missing) * 2.0 + len(major_missing) * 1.0 + len(minor_missing) * 0.3
        deduct += len(critical_altered) * 2.0 + len(major_altered) * 0.5
        text_score = max(3.0, 8.0 - deduct)
        result.text_coverage = max(0.6, 1.0 - (len(missing) * 0.05))
    else:  # low
        text_score = max(1.0, 8.0 - len(critical_missing) * 2.5 - len(major_missing) * 1.5)
        result.text_coverage = max(0.2, 1.0 - (len(missing) * 0.08))

    result.text_score = text_score

    # ── Add structured issues ─────────────────────────────────────────────────
    for m in missing:
        severity = m.get("severity", "major")
        loc      = m.get("location_hint", "")
        conf     = m.get("confidence", 1.0)
        _add_issue_fn(result,
            "text_accuracy", severity,
            f"D3/D8-semantic — Missing content (confidence {conf:.0%}): "
            f"\"{m['text'][:120]}\"",
            location=loc,
            impact="Content absent from SGML",
        )

    for a in altered:
        severity = a.get("severity", "major")
        _add_issue_fn(result,
            "text_accuracy", severity,
            f"D3/D8-semantic — Altered content: PDF has \"{a['original_pdf'][:80]}\" "
            f"but SGML has \"{a['sgml_version'][:80]}\"",
            location=a.get("location_hint", ""),
            impact="Text materially changed from source",
        )

    if summary:
        result.warnings.append(f"D3/D8 agent summary: {summary}")


def _add_issue_fn(result, dimension, severity, description, location="", impact=""):
    """Helper that mirrors source_validator._add_issue."""
    result.issues.append({
        "level": "L4",
        "category": dimension,
        "severity": severity,
        "description": description,
        "location": location,
        "impact": impact,
    })


def _fallback_deterministic(pdf_data, sgml_data: dict, result) -> None:
    """
    Minimal deterministic fallback when LLM auth fails.
    Uses only the safest D3 check: paragraph coverage via 5-gram on SGML blob.
    Skips D8 entirely to avoid the false positive cycle.
    """
    from difflib import SequenceMatcher

    sgml_blob  = sgml_data.get("text", "").lower()
    sgml_words = sgml_blob.split()
    ngrams: set = {
        tuple(sgml_words[i:i + 5])
        for i in range(len(sgml_words) - 4)
    }

    _BULLET = frozenset({"o", "•", "◦", "▪", "▸", "→", "–", "-", ";", ","})

    def _norm_simple(text):
        import unicodedata
        text = re.sub(r"<[^>]+>", " ", text)
        text = unicodedata.normalize("NFC", text.lower())
        text = re.sub(r"\s+", " ", text).strip()
        return text

    meaningful = [p for p in pdf_data.paragraphs if len(p.split()) >= 8]
    if not meaningful:
        result.text_score   = 8.0
        result.text_coverage = 1.0
        return

    covered = 0
    missing = []
    for para in meaningful:
        words = [w for w in _norm_simple(para).split() if w not in _BULLET]
        if len(words) < 5:
            covered += 1
            continue
        grams = [tuple(words[i:i + 5]) for i in range(len(words) - 4)]
        matched = sum(1 for g in grams if g in ngrams)
        ratio   = matched / len(grams) if grams else 0
        if ratio >= 0.55 or (sum(1 for w in words if w in sgml_blob) / len(words)) >= 0.90:
            covered += 1
        else:
            missing.append(para[:100])

    coverage = covered / len(meaningful)
    result.text_coverage    = coverage
    result.missing_paragraphs = missing

    if coverage >= 0.92:
        result.text_score = 5.0
    elif coverage >= 0.80:
        result.text_score = 4.0
        _add_issue_fn(result, "text_accuracy", "minor",
                      f"D3-fallback — Coverage {coverage:.0%}, {len(missing)} paragraph(s) unmatched.")
    else:
        result.text_score = 2.0
        _add_issue_fn(result, "text_accuracy", "major",
                      f"D3-fallback — Low coverage {coverage:.0%}. {len(missing)} paragraph(s) missing.",
                      impact=f"{len(missing)} paragraphs")
