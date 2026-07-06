"""
semantic_content_agent.py  —  v2
══════════════════════════════════
Replaces D3 (text accuracy) + D8 (word gaps) with a multi-call Claude Opus
agent that semantically compares PDF source text against SGML output.

────────────────────────────────────────────────────────────────────────────
WHY a full agentic replacement instead of deterministic rules
────────────────────────────────────────────────────────────────────────────
Root cause of the recurring false-positive / false-negative cycle:
  SequenceMatcher aligns LINEAR word sequences.  PDF and SGML carry the SAME
  content in DIFFERENT structural order (tables extracted differently, contact
  blocks merged or split, footnotes reordered).  Every guard added to suppress
  one false positive creates a new false negative on the next document.  The
  cycle cannot be broken deterministically because the problem is SEMANTIC.

────────────────────────────────────────────────────────────────────────────
Architecture  —  mirrors SequentialSGMLLayer v14 pattern exactly
────────────────────────────────────────────────────────────────────────────
Phase 1 — DETERMINISTIC (no LLM)
  Extract PDF sections  : split pdf_data.paragraphs at heading boundaries
  Extract SGML sections : split raw_sgml at <BLOCK2/3> tag boundaries
  Pair sections         : fuzzy-match PDF headings to SGML headings

Phase 2 — LLM (batched, streaming, single-turn per batch)
  For every batch of SECTION_BATCH_SIZE (3) section pairs:
    • Build a structured input: PDF section text vs. SGML section text
    • Single-shot LLM call → JSON array, one result per section pair
    • Retry once on parse error (for attempt in range(2))
  Aggregate results across all batches into L4Result fields.

This mirrors the pipeline:
  StructuralAgent  → batches of 10 paras → JSON array
  InlineAgent      → batches of 15 paras → JSON array
  ValidatorAgent   → batches of  8 paras → JSON array
  ContentAgent v2  → batches of  3 section pairs → JSON array

────────────────────────────────────────────────────────────────────────────
What this agent checks (replaces D3 + D8)
────────────────────────────────────────────────────────────────────────────
  1.  Is substantive PDF content faithfully present in the SGML?
  2.  Is any PDF text genuinely MISSING (deletion / truncation)?
  3.  Has any text been ALTERED (numbers, names, regulatory IDs changed)?

What it does NOT check  (stays deterministic elsewhere)
  D2: Bold/italic tagging      D4: Table/image/appendix counts
  D5: Section ordering         D6: Entity encoding
  D7: Metadata (title, date)   D9: Contact-info block

Auth: Same TR AI Platform pattern as batch_runner_standalone.py.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from typing import Optional

import httpx
import requests

# ── TR Platform configuration (identical to batch_runner_standalone.py) ───────
_TR_AUTH_URL  = "https://aiplatform.gcs.int.thomsonreuters.com/v1/anthropic/token"
_WORKSPACE_ID = "Saikumar3Y0Z"
_MODEL        = "claude-opus-4-5"   # Opus 4.5 — same model as pipeline Stage 1/2/3

# Agent parameters — mirrors pipeline SYSTEM_CONFIG / AGENT_CONFIG
_SECTION_BATCH    = 3      # section pairs per LLM call  (cf. pipeline batch_size=10)
_MAX_SECTION_CHARS = 2_800  # max chars per section in LLM call (pair → ~5.6 K total)
_MAX_TOKENS       = 2048   # max LLM response tokens
_TEMPERATURE      = 0.0    # deterministic — same as all pipeline agents

# ── Cached auth client  ───────────────────────────────────────────────────────
# Authenticate ONCE at module import.  All calls reuse the same Anthropic client
# (same pattern as _get_opus_client() in SequentialSGMLLayer v14).
_cached_client = None


def _get_client():
    """Return a cached Anthropic client, authenticating on first call."""
    global _cached_client
    if _cached_client is not None:
        return _cached_client
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
        _cached_client = Anthropic(api_key=token, http_client=httpx.Client(verify=False))
        return _cached_client
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 — DETERMINISTIC: section extraction
# Mirrors pipeline: no LLM here, just structure preparation
# ─────────────────────────────────────────────────────────────────────────────

def _strip_tags(raw: str) -> str:
    """Strip SGML/XML tags and decode common entities to plain text."""
    _ENTITIES = {
        "&rsquo;": "'", "&lsquo;": "'", "&rdquo;": '"', "&ldquo;": '"',
        "&mdash;": "—", "&ndash;": "–", "&amp;": "&", "&lt;": "<",
        "&gt;": ">", "&nbsp;": " ", "&eacute;": "é", "&egrave;": "è",
        "&ecirc;": "ê", "&agrave;": "à", "&acirc;": "â", "&ocirc;": "ô",
        "&ucirc;": "û", "&iuml;": "ï", "&ccedil;": "ç", "&OElig;": "Œ",
        "&oelig;": "œ", "&Eacute;": "É", "&euro;": "€", "&reg;": "®",
        "&copy;": "©", "&trade;": "™", "&bull;": "•", "&hellip;": "…",
    }
    text = re.sub(r"<[^>]+>", " ", raw)
    for ent, rep in _ENTITIES.items():
        text = text.replace(ent, rep)
    text = re.sub(r"&#\d+;", " ", text)
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_sgml_sections(raw_sgml: str) -> list[dict]:
    """
    Split SGML into logical sections.

    Priority order (try the highest structural level with ≥2 occurrences):
      1. BLOCK2  — most common (CSA Notices, Instruments)
      2. BLOCK3  — used when there are no BLOCK2 (sub-section-only docs)
      3. BLOCK1  — used in Annex-style documents (multi-part national instruments,
                    local amendment notices like 11-349)
      4. Fallback — treat entire SGML as one section

    Strategy: split at every <BLOCKn opening tag; each slice starts with the tag
    and runs to the next occurrence.  Nested BLOCK tags are fine — they appear
    inside the slice as raw text that gets stripped for the content string.

    Returns list of {"heading": str, "content": str, "raw_len": int}.
    """
    def _split_at(raw: str, tag: str) -> list[str]:
        """Split raw_sgml at every opening <tag> occurrence."""
        return re.split(rf"(?=<{tag}[\s>])", raw)

    # Try block levels in priority order
    parts: list[str] = []
    for block_tag in ("BLOCK2", "BLOCK3", "BLOCK1"):
        candidate = _split_at(raw_sgml, block_tag)
        if len(candidate) >= 3:   # ≥2 sections found (first part is preamble)
            parts = candidate
            break

    # Fallback: no block tags — treat whole SGML as one section
    if len(parts) < 2:
        full_text = _strip_tags(raw_sgml)
        return [{"heading": "Document", "content": full_text[:_MAX_SECTION_CHARS],
                 "raw_len": len(full_text)}]

    sections: list[dict] = []

    # ── Include preamble (everything before the first BLOCKn) as a section ───
    # This is critical: the preamble contains the document intro and conclusion
    # paragraphs (e.g. "The text of rule and policy consolidations on the
    # websites of CSA members will be updated...").  If the business deletes a
    # paragraph from the preamble, the agent MUST see the preamble explicitly —
    # it cannot rely on the blob alone because a short deleted paragraph blends
    # invisibly into 6000 chars of surrounding text.
    preamble_raw = parts[0]
    preamble_text = _strip_tags(preamble_raw).strip()
    if len(preamble_text.split()) >= 10:   # only add if preamble has substance
        sections.append({
            "heading": "Introduction/Preamble",
            "content": preamble_text[:_MAX_SECTION_CHARS],
            "raw_len": len(preamble_text),
        })

    for part in parts[1:]:   # body sections after the first BLOCK
        # Extract heading from first <TI> in this block
        ti_m = re.search(r"<TI[^>]*>(.*?)</TI>", part, re.DOTALL)
        heading = _strip_tags(ti_m.group(1)) if ti_m else ""
        # Strip all tags for the content
        content = _strip_tags(part)
        # Remove the heading from the start of content to avoid duplication
        if heading and content.lower().startswith(heading.lower()):
            content = content[len(heading):].lstrip(" ,.-\n")
        sections.append({
            "heading": heading[:200],
            "content": content[:_MAX_SECTION_CHARS],
            "raw_len": len(content),
        })

    return sections or [{"heading": "Document",
                         "content": _strip_tags(raw_sgml)[:_MAX_SECTION_CHARS],
                         "raw_len": len(raw_sgml)}]


def _extract_pdf_sections(pdf_data) -> list[dict]:
    """
    Split PDF paragraphs into logical sections using pdf_data.headings.

    pdf_data.headings — lines detected as headings by font-size analysis.
    pdf_data.paragraphs — all content lines (headings included if ≥4 words).

    We walk through paragraphs; when a paragraph closely matches a known
    heading we start a new section.  This gives us section-scoped PDF text
    to match against SGML sections.

    Returns list of {"heading": str, "content": str}.
    """
    if not pdf_data.paragraphs:
        return []

    # Normalise heading set for fuzzy matching
    heading_set = set()
    for h in pdf_data.headings:
        heading_set.add(h.lower().strip())

    def _is_heading(text: str) -> bool:
        t = text.lower().strip()
        if t in heading_set:
            return True
        # Also treat short ALL-CAPS lines as headings
        words = text.split()
        if len(words) <= 12 and all(c.isupper() or not c.isalpha() for c in text if c.strip()):
            return True
        return False

    sections: list[dict] = []
    current_heading = "Preamble"
    current_paras: list[str] = []

    for para in pdf_data.paragraphs:
        if _is_heading(para) and len(para.split()) <= 15:
            # Flush current section
            if current_paras:
                content = " ".join(current_paras)
                sections.append({"heading": current_heading,
                                  "content": content[:_MAX_SECTION_CHARS]})
            current_heading = para
            current_paras = []
        else:
            current_paras.append(para)

    # Flush final section
    if current_paras:
        content = " ".join(current_paras)
        sections.append({"heading": current_heading,
                          "content": content[:_MAX_SECTION_CHARS]})

    # Fallback: no headings detected → one big section
    if not sections:
        all_text = " ".join(pdf_data.paragraphs)
        sections = [{"heading": "Document", "content": all_text[:_MAX_SECTION_CHARS]}]

    return sections


def _pair_sections(pdf_sections: list[dict],
                   sgml_sections: list[dict]) -> list[dict]:
    """
    Pair PDF sections to SGML sections by heading similarity.

    Special case: if the PDF has only ONE section (flat document with no
    detected headings), we compare it against all SGML sections combined.
    This handles Annex-style documents (e.g. 11-349: 4 BLOCK1 annexes) where
    PyMuPDF merges the whole PDF into one big paragraph.

    Normal case: for each PDF section, find the SGML section whose heading has
    the highest word-overlap ratio.  Many-to-one is allowed.

    Returns list of {"pdf": dict, "sgml": dict, "match_score": float}.
    """
    if not sgml_sections:
        return [{"pdf": p, "sgml": {"heading": "SGML", "content": ""}, "match_score": 0.0}
                for p in pdf_sections]

    # ── Special case: flat PDF (single section) ───────────────────────────────
    # Combine all SGML sections into one view so the agent sees the full document.
    # NOTE: diagnostic assessment showed expanding beyond 5,600 chars causes LLM
    # to generate false-positive missing-paragraph findings.  Keep combined limit.
    if len(pdf_sections) == 1:
        combined_heading = " | ".join(s["heading"] for s in sgml_sections if s["heading"])[:200]
        combined_content = "\n\n".join(
            f"[{s['heading']}]\n{s['content']}" for s in sgml_sections
        )[:_MAX_SECTION_CHARS * 2]   # 5,600 chars — proven safe limit
        return [{
            "pdf": pdf_sections[0],
            "sgml": {"heading": combined_heading, "content": combined_content},
            "match_score": 1.0,      # forced match — whole doc vs whole doc
        }]

    # ── Normal case: word-overlap pairing ────────────────────────────────────
    def _words(text: str) -> set[str]:
        return set(re.findall(r"[a-zA-ZÀ-ÿ0-9]+", text.lower()))

    pairs: list[dict] = []
    for ps in pdf_sections:
        pdf_words = _words(ps["heading"]) | _words(ps["content"][:200])
        best_score = 0.0
        best_sgml = sgml_sections[0]
        for ss in sgml_sections:
            sgml_words = _words(ss["heading"]) | _words(ss["content"][:200])
            if not pdf_words or not sgml_words:
                continue
            overlap = len(pdf_words & sgml_words) / max(len(pdf_words), len(sgml_words))
            # Boost when headings share at least one substantive word
            h_overlap = _words(ps["heading"]) & _words(ss["heading"])
            if h_overlap:
                overlap = min(1.0, overlap + 0.2)
            if overlap > best_score:
                best_score = overlap
                best_sgml = ss
        pairs.append({"pdf": ps, "sgml": best_sgml, "match_score": best_score})

    return pairs


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — LLM: section-pair comparison
# Mirrors pipeline:  streaming, temperature=0.0, JSON array, retry once
# ─────────────────────────────────────────────────────────────────────────────

# System prompt — single responsibility: content fidelity within a section pair
_SYSTEM_PROMPT = """You are CONTENT_AGENT — a precision quality-validator for
Thomson Reuters Canada legal document conversions (PDF → Carswell SGML).

YOUR ONLY JOB: for each section pair given, report whether the substantive
regulatory text of the PDF section is faithfully present in the SGML section.

IMPORTANT DISTINCTIONS — do NOT flag these as issues:
• SGML tags (<P>, <BLOCK2>, <BOLD>, <EM>, <ITEM>, <TABLE>, etc.) are structural
  wrappers.  Their presence or absence is NOT a content issue.
• SGML entities (&rsquo; &mdash; &eacute; etc.) are Unicode equivalents.
• LEGITIMATELY ABSENT from SGML (never flag as missing):
  - Page numbers, running headers/footers, copyright footers
  - Table of Contents entries and dot-leaders (………)
  - Website NAVIGATION LINKS only: e.g. "Home", "Sign In", "Français", "Ontario.ca"
    (IMPORTANT: regulatory text ABOUT websites — e.g. "text of rule and policy
    consolidations on the websites of CSA members will be updated to reflect
    these amendments" — is substantive body text and MUST be in the SGML)
  - Footnote definitions at page bottom ("19 See NI 33-109 …")
  - TOC sub-entries (roman numerals i. ii. iii. iv.)
  - Standalone date-only lines used as page headers ("June 9, 2023")
  - OSCB bulletin section markers ("B.1 Notices / Avis", "B.5 Rules")
  - Regulatory filing form numbers when they appear as sidebar labels
• Content present in a DIFFERENT section of the SGML is NOT missing — it is
  simply reordered.  Only flag text as missing if it is absent from the entire
  SGML document (the [FULL SGML BLOB] is provided for this check).
• Bold / italic formatting differences are NOT content issues.
• Minor punctuation or capitalisation differences are NOT content issues.

FLAG as issues ONLY:
• Text that is genuinely absent from the entire SGML document — this includes
  short closing paragraphs, website-update notices, and "Please refer questions
  to" contact lines that are present in the PDF but entirely absent from SGML
• Numbers, proper names, or regulatory identifiers materially changed
  (e.g. section "14.1.3" became "4.1.3"; "(i.1)" became "(i.(1)")
• Sentences or clauses truncated mid-way without equivalent elsewhere

OUTPUT: a JSON array — one entry per input section pair, in the same order.
Each entry must be:
{
  "section_idx": <integer, 0-based, matching input>,
  "missing_text": [
    {"text": "<missing content, max 200 chars>",
     "location_hint": "<where in PDF section>",
     "severity": "critical|major|minor",
     "confidence": 0.95}
  ],
  "altered_text": [
    {"original_pdf": "<PDF text>",
     "sgml_version": "<SGML text>",
     "location_hint": "<section>",
     "severity": "critical|major|minor"}
  ],
  "fidelity": "high|medium|low"
}
Return missing_text=[], altered_text=[], fidelity="high" when the section is
faithfully converted.  Return ONLY the JSON array — no prose."""


def _call_batch(client, pairs_batch: list[dict], doc_type: str,
                sgml_blob: str) -> list[dict]:
    """
    Single-shot LLM call for a batch of ≤SECTION_BATCH section pairs.

    Mirrors pipeline _call_structural_llm / _call_em_llm:
    - Uses client.messages.stream()
    - temperature=0.0
    - Retry once on parse error (for attempt in range(2))
    - Returns list of per-section results (same length as pairs_batch)
    """
    # Build user message: structured section pairs + full SGML blob for reorder check
    user = (
        f"Document type: {doc_type}\n\n"
        "For each section pair below, check whether the PDF section content "
        "is faithfully present in the SGML section AND in the full SGML blob.\n\n"
    )
    for idx, pair in enumerate(pairs_batch):
        pdf_h = pair["pdf"]["heading"]
        pdf_c = pair["pdf"]["content"]
        sgml_h = pair["sgml"]["heading"]
        sgml_c = pair["sgml"]["content"]
        match  = pair["match_score"]
        user += (
            f"--- SECTION PAIR {idx} ---\n"
            f"PDF SECTION: [{pdf_h}]\n{pdf_c}\n\n"
            f"SGML SECTION (best match, score={match:.2f}): [{sgml_h}]\n{sgml_c}\n\n"
        )

    # Provide full SGML blob for cross-section presence check
    blob_excerpt = sgml_blob[:6000]
    blob_note = f"\n[blob truncated — {len(sgml_blob)} total chars]" if len(sgml_blob) > 6000 else ""
    user += (
        f"--- FULL SGML BLOB (for cross-section presence check) ---\n"
        f"{blob_excerpt}{blob_note}\n\n"
        f"Return ONLY the JSON array, one entry per section pair (0 to {len(pairs_batch)-1})."
    )

    default = [{"section_idx": i, "missing_text": [], "altered_text": [],
                 "fidelity": "high"} for i in range(len(pairs_batch))]

    for attempt in range(2):
        try:
            with client.messages.stream(
                model=_MODEL,
                max_tokens=_MAX_TOKENS,
                temperature=_TEMPERATURE,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user}],
            ) as stream:
                raw = stream.get_final_text()

            # Clean up potential markdown code fence
            cleaned = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
            start = cleaned.find("[")
            if start == -1:
                raise ValueError("No JSON array in response")
            data = json.loads(cleaned[start:])

            # Validate length — pad or trim to match batch size
            while len(data) < len(pairs_batch):
                data.append({"section_idx": len(data), "missing_text": [],
                              "altered_text": [], "fidelity": "high"})
            return data[:len(pairs_batch)]

        except Exception as exc:
            if attempt == 0:
                print(f"   ⚠️  CONTENT_AGENT retry (attempt 1): {exc}")
                time.sleep(2)
            else:
                print(f"   ⚠️  CONTENT_AGENT failed after retry: {exc}")

    return default


# ─────────────────────────────────────────────────────────────────────────────
# Scoring helper — fixed: altered_text deducts from score in ALL fidelity branches
# ─────────────────────────────────────────────────────────────────────────────

def _compute_score(all_missing: list, all_altered: list) -> tuple[float, float]:
    """
    Returns (text_score 0-8, text_coverage 0-1).

    Deductions are applied for both missing AND altered content in all branches.
    (v1 bug: altered_text was ignored in the 'high' fidelity scoring path.)
    """
    critical_m = sum(1 for m in all_missing if m.get("severity") == "critical")
    major_m    = sum(1 for m in all_missing if m.get("severity") == "major")
    minor_m    = sum(1 for m in all_missing if m.get("severity") == "minor")
    critical_a = sum(1 for a in all_altered if a.get("severity") == "critical")
    major_a    = sum(1 for a in all_altered if a.get("severity") == "major")
    minor_a    = sum(1 for a in all_altered if a.get("severity") == "minor")

    total_issues = len(all_missing) + len(all_altered)

    deduct = (
        critical_m * 2.5 + major_m * 1.0 + minor_m * 0.3 +
        critical_a * 2.0 + major_a * 0.5 + minor_a * 0.1
    )

    text_score   = max(0.0, min(8.0, 8.0 - deduct))
    text_coverage = max(0.0, 1.0 - (total_issues * 0.04))
    if total_issues == 0:
        text_coverage = 1.0

    return text_score, text_coverage


# ─────────────────────────────────────────────────────────────────────────────
# Issue helper — mirrors source_validator._add_issue() shape
# ─────────────────────────────────────────────────────────────────────────────

def _add_issue_fn(result, dimension: str, severity: str, description: str,
                  location: str = "", impact: str = "") -> None:
    result.issues.append({
        "level": "L4",
        "category": dimension,
        "severity": severity,
        "description": description,
        "location": location,
        "impact": impact,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1b — Focused preamble sentence-level LLM comparison
#
# The main _call_batch sees the "flat PDF" (entire document in one section pair)
# and does high-level semantic comparison — it misses single-sentence PARTIAL
# deletions (prefix truncations) in the intro.  This dedicated call compares
# the intro paragraphs SENTENCE-BY-SENTENCE with a verbatim-focused prompt.
# ─────────────────────────────────────────────────────────────────────────────

_PREAMBLE_SYSTEM_PROMPT = """You are PREAMBLE_COMPARE — a precision sentence-level
comparator for Thomson Reuters Canada legal document conversions (PDF → Carswell SGML).

YOUR TASK: Compare each numbered PDF introduction paragraph SENTENCE BY SENTENCE against
the SGML preamble text.  Find sentences that are DELETED or have their BEGINNING TRUNCATED.

VERBATIM RULE: "CSA members will be updated" is DIFFERENT from "The text of rule and
policy consolidations on the websites of CSA members will be updated" — if the PDF has
the longer version but the SGML only has the shorter version (with the beginning missing),
that is ALTERED content and must be flagged.

DO NOT FLAG:
  - Contact person names and addresses (may appear in different format elsewhere)
  - NI instrument titles that appear as SGML headings in other document sections
  - Website NAVIGATION LINKS: "Home", "Sign In", "Français", "English"
  - Page numbers or running headers
  - Minor punctuation differences or capitalisation differences
  - Sentences that appear LATER in the SGML (out of order is acceptable)

OUTPUT: JSON array — empty [] if all sentences are faithfully present, otherwise:
[
  {
    "type": "missing",
    "pdf_text": "<exact PDF sentence, max 200 chars>",
    "severity": "major",
    "confidence": 0.90
  },
  {
    "type": "altered",
    "pdf_text": "<full PDF sentence, max 200 chars>",
    "sgml_version": "<truncated SGML version>",
    "issue": "prefix deleted",
    "severity": "major",
    "confidence": 0.95
  }
]
Return ONLY the JSON array — no prose."""


def _call_preamble_compare(client, pdf_intro_paragraphs: list[str],
                            sgml_preamble_text: str, sgml_blob: str) -> dict:
    """
    Focused verbatim sentence-level LLM comparison for the intro/preamble.

    Returns {"missing": [...], "altered": [...]} using the standard schema.
    """
    if not pdf_intro_paragraphs or not sgml_preamble_text:
        return {"missing": [], "altered": []}

    numbered = "\n".join(f"[{i + 1}] {p}" for i, p in enumerate(pdf_intro_paragraphs))

    user = (
        "PDF INTRODUCTION PARAGRAPHS (each may contain multiple sentences):\n"
        f"{numbered}\n\n"
        f"SGML PREAMBLE TEXT:\n{sgml_preamble_text[:2_500]}\n\n"
        f"FULL SGML BLOB (for cross-section presence verification):\n{sgml_blob[:4_000]}\n\n"
        "INSTRUCTIONS:\n"
        "1. For each PDF paragraph, split it into individual sentences at period+space boundaries.\n"
        "2. For each sentence (≥8 words), check if it appears in the SGML — verbatim or near-verbatim.\n"
        "3. If a sentence's FIRST 5+ WORDS are NOT in the SGML but the ENDING WORDS ARE, "
        "the sentence was TRUNCATED (beginning deleted) — flag as 'altered'.\n"
        "4. If the sentence is entirely absent, flag as 'missing'.\n"
        "5. Look especially for sentences about websites, update notices, or policy references "
        "whose opening clause may have been removed from the SGML.\n\n"
        "Return ONLY the JSON array. Return [] if all content is faithfully present."
    )

    result: dict = {"missing": [], "altered": []}

    for attempt in range(2):
        try:
            with client.messages.stream(
                model=_MODEL,
                max_tokens=1_024,
                temperature=_TEMPERATURE,
                system=_PREAMBLE_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user}],
            ) as stream:
                raw = stream.get_final_text()

            cleaned = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
            start = cleaned.find("[")
            if start == -1:
                return result
            data = json.loads(cleaned[start:])

            for item in data:
                itype = item.get("type", "")
                if itype == "missing":
                    result["missing"].append({
                        "text": item.get("pdf_text", "")[:200],
                        "location_hint": "Introduction/Preamble (preamble-compare)",
                        "severity": item.get("severity", "major"),
                        "confidence": item.get("confidence", 0.9),
                    })
                elif itype == "altered":
                    result["altered"].append({
                        "original_pdf": item.get("pdf_text", "")[:200],
                        "sgml_version": item.get("sgml_version", "")[:200],
                        "location_hint": "Introduction/Preamble (preamble-compare)",
                        "severity": item.get("severity", "major"),
                    })
            return result

        except Exception as exc:
            if attempt == 0:
                print(f"   ⚠️  PREAMBLE_COMPARE retry: {exc}")
                time.sleep(2)
            else:
                print(f"   ⚠️  PREAMBLE_COMPARE failed: {exc}")

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point — called by source_validator.py
# ─────────────────────────────────────────────────────────────────────────────

def check_text_semantic(
    pdf_data,               # _PDFData from source_validator._extract_pdf_data()
    sgml_data: dict,        # from source_validator._extract_sgml_text()
    result,                 # L4Result to populate
    doc_type: str = "Notice",
    raw_sgml: str = "",     # full raw SGML string (for section extraction + blob check)
) -> None:
    """
    Agentic replacement for D3 (check_text_accuracy) + D8 (check_word_gaps).

    Architecture (mirrors SequentialSGMLLayer v14):
      Phase 1 — Deterministic (no LLM):
        • Extract SGML sections from BLOCK2 boundaries
        • Extract PDF sections from heading boundaries
        • Pair sections by heading word-overlap (fuzzy, deterministic)

      Phase 2 — LLM (batched, streaming, single-turn per batch):
        • For each batch of SECTION_BATCH pairs:
          – Build structured user message (PDF section vs SGML section + blob)
          – Single-shot LLM call with streaming → JSON array
          – Retry once on parse error
        • Aggregate all batch results

    Populates:
      result.text_score, result.text_coverage,
      result.missing_paragraphs, result.word_gaps,
      result.inline_changed_paragraphs, result.issues, result.warnings
    """
    # ── Guard: nothing to compare ─────────────────────────────────────────────
    if not pdf_data.paragraphs:
        result.text_score    = 8.0
        result.text_coverage = 1.0
        result.warnings.append("D3/D8 skipped: no PDF text extractable.")
        return

    # ── Authenticate (cached) ─────────────────────────────────────────────────
    client = _get_client()
    if client is None:
        result.warnings.append(
            "D3/D8: TR auth failed — falling back to deterministic 5-gram check."
        )
        _fallback_deterministic(pdf_data, sgml_data, result)
        return

    result.warnings.append(
        f"D3/D8: SemanticContentAgent v2 running (model: {_MODEL}, "
        f"section-batch={_SECTION_BATCH})…"
    )

    # ── Phase 1: Section extraction ───────────────────────────────────────────
    sgml_sections = _extract_sgml_sections(raw_sgml) if raw_sgml else []
    pdf_sections  = _extract_pdf_sections(pdf_data)

    # If SGML has no block structure, fall back to one big comparison
    if not sgml_sections:
        sgml_sections = [{"heading": "Document",
                           "content": sgml_data.get("text", "")[:_MAX_SECTION_CHARS]}]

    # Flat-PDF fix: for flat documents (PyMuPDF found no heading boundaries)
    # use more of the PDF text than the default _MAX_SECTION_CHARS.
    # NOTE: diagnostic assessment (2026-07-06) showed that expanding to 40K
    # causes the LLM to generate 5-6 false-positive "missing" paragraphs on
    # clean documents — precision degrades unacceptably.  Keep the limit at
    # 10,000 chars which balances precision vs recall for the current LLM.
    _FLAT_PDF_LIMIT = 10_000
    _full_flat_pdf_text: str = ""   # kept for future multi-window option
    if len(pdf_sections) == 1:
        _full_flat_pdf_text = " ".join(pdf_data.paragraphs)
        pdf_sections[0]["content"] = _full_flat_pdf_text[:_FLAT_PDF_LIMIT]

    # Pair sections
    pairs = _pair_sections(pdf_sections, sgml_sections)

    n_pdf  = len(pdf_sections)
    n_sgml = len(sgml_sections)
    n_pair = len(pairs)
    result.warnings.append(
        f"D3/D8: {n_pdf} PDF sections × {n_sgml} SGML sections → {n_pair} pairs, "
        f"{(n_pair + _SECTION_BATCH - 1) // _SECTION_BATCH} LLM call(s)."
    )

    # SGML blob for cross-section presence checks (strip tags once)
    sgml_blob = sgml_data.get("text", "") if sgml_data else _strip_tags(raw_sgml)

    # ── Phase 1b: Focused preamble sentence-level comparison ─────────────────
    # The main section-batch LLM call (Phase 2) does high-level semantic
    # comparison and misses single-sentence PARTIAL deletions (prefix
    # truncations) in the intro.  This dedicated call compares the intro
    # paragraphs sentence-by-sentence with a verbatim-focused prompt.
    preamble_issues: dict = {"missing": [], "altered": []}
    preamble_sgml_section = next(
        (s for s in sgml_sections
         if "preamble" in s["heading"].lower() or "introduction" in s["heading"].lower()),
        None,
    )

    _ANNEX_HEADING_RE = re.compile(
        r"^(Annex|Schedule|Appendix|Local\s+Amendments?\s+to)\s+[A-Z]",
        re.IGNORECASE,
    )

    if preamble_sgml_section and client and re.search(
        r'LABEL="Annex"', raw_sgml or ""
    ):
        # Only run preamble-compare for Local Amendment Notice documents
        # (SGML contains <APPENDIX LABEL="Annex"...>) — the specific document
        # class where the preamble is clearly separated from the Annex body
        # sections and where single-sentence deletions in the intro are
        # business-reported issues (11-349 pattern).
        # BLOCK1-structured policy manuals and instruments use the main LLM
        # batch for their section-level comparison and do not need this extra call.
        pdf_intro: list[str] = []
        for para in pdf_data.paragraphs:
            if _ANNEX_HEADING_RE.match(para.strip()):
                break
            if len(para.split()) >= 8:
                pdf_intro.append(para)
            if len(pdf_intro) >= 8:   # first 8 substantive intro paragraphs
                break
        if pdf_intro:
            print("   CONTENT_AGENT preamble-compare: "
                  f"{len(pdf_intro)} PDF intro paragraph(s)")
            preamble_issues = _call_preamble_compare(
                client, pdf_intro, preamble_sgml_section["content"], sgml_blob
            )

    # ── Phase 2: Batched LLM comparison ──────────────────────────────────────
    all_missing: list[dict] = []
    all_altered: list[dict] = []
    all_summaries: list[str] = []
    batch_num = 0

    for batch_start in range(0, len(pairs), _SECTION_BATCH):
        batch = pairs[batch_start: batch_start + _SECTION_BATCH]
        batch_num += 1
        print(f"   CONTENT_AGENT batch {batch_num}: {len(batch)} section pair(s)")

        section_results = _call_batch(client, batch, doc_type, sgml_blob)

        for item in section_results:
            all_missing.extend(item.get("missing_text", []))
            all_altered.extend(item.get("altered_text", []))
            fid = item.get("fidelity", "high")
            if fid != "high":
                all_summaries.append(
                    f"Section {item.get('section_idx', '?')}: fidelity={fid}"
                )

    # ── Merge preamble check results into main results ────────────────────────
    # De-duplicate: skip items already found by Phase 2 LLM
    llm_missing_texts = {m["text"][:60].lower() for m in all_missing}
    for pm in preamble_issues.get("missing", []):
        if pm["text"][:60].lower() not in llm_missing_texts:
            all_missing.append(pm)

    llm_altered_pdfs = {a["original_pdf"][:60].lower() for a in all_altered}
    for pa in preamble_issues.get("altered", []):
        if pa["original_pdf"][:60].lower() not in llm_altered_pdfs:
            all_altered.append(pa)

    # ── Aggregate into L4Result ───────────────────────────────────────────────
    result.missing_paragraphs = [m["text"] for m in all_missing]
    result.word_gaps = [
        {
            "missing":    m["text"],
            "type":       "semantic",
            "confidence": m.get("confidence", 1.0),
            "location":   m.get("location_hint", ""),
        }
        for m in all_missing
    ]
    result.inline_changed_paragraphs = [
        {
            "original": a["original_pdf"],
            "sgml":     a["sgml_version"],
            "location": a.get("location_hint", ""),
        }
        for a in all_altered
    ]

    # Score (fixed: uses unified deduction formula for both missing + altered)
    text_score, text_coverage = _compute_score(all_missing, all_altered)
    result.text_score    = text_score
    result.text_coverage = text_coverage

    # ── Add structured issues to result ──────────────────────────────────────
    for m in all_missing:
        sev  = m.get("severity", "major")
        loc  = m.get("location_hint", "")
        conf = m.get("confidence", 1.0)
        _add_issue_fn(
            result, "text_accuracy", sev,
            f"D3/D8-semantic — Missing content (confidence {conf:.0%}): "
            f"\"{m['text'][:120]}\"",
            location=loc,
            impact="Content absent from SGML",
        )

    for a in all_altered:
        sev = a.get("severity", "major")
        _add_issue_fn(
            result, "text_accuracy", sev,
            f"D3/D8-semantic — Altered: PDF=\"{a['original_pdf'][:80]}\" "
            f"→ SGML=\"{a['sgml_version'][:80]}\"",
            location=a.get("location_hint", ""),
            impact="Text materially changed from source",
        )

    if all_summaries:
        result.warnings.append(
            "D3/D8 low-fidelity sections: " + "; ".join(all_summaries)
        )
    else:
        result.warnings.append("D3/D8: all sections high-fidelity.")


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic fallback  (used only when TR auth fails)
# Better than v1: uses the old D3 5-gram coverage approach, but skips D8
# to avoid the SequenceMatcher false-positive cycle.
# ─────────────────────────────────────────────────────────────────────────────

def _fallback_deterministic(pdf_data, sgml_data: dict, result) -> None:
    """
    Minimal deterministic fallback when LLM auth fails.
    5-gram coverage check (D3 only) — D8 skipped to avoid false-positive cycle.
    Scores conservatively (≤5.0) to signal reduced confidence.
    """
    sgml_blob  = sgml_data.get("text", "").lower()
    sgml_words = sgml_blob.split()
    ngrams: set = {
        tuple(sgml_words[i: i + 5])
        for i in range(len(sgml_words) - 4)
    }

    _STOP = frozenset({"o", "•", "◦", "▪", "▸", "→", "–", "-", ";", ","})

    def _norm(text: str) -> str:
        text = re.sub(r"<[^>]+>", " ", text)
        text = unicodedata.normalize("NFC", text.lower())
        return re.sub(r"\s+", " ", text).strip()

    meaningful = [p for p in pdf_data.paragraphs if len(p.split()) >= 8]
    if not meaningful:
        result.text_score    = 8.0
        result.text_coverage = 1.0
        return

    covered = 0
    missing: list[str] = []
    for para in meaningful:
        words = [w for w in _norm(para).split() if w not in _STOP]
        if len(words) < 5:
            covered += 1
            continue
        grams = [tuple(words[i: i + 5]) for i in range(len(words) - 4)]
        gram_hit = sum(1 for g in grams if g in ngrams) / len(grams) if grams else 0
        word_hit = sum(1 for w in words if w in sgml_blob) / len(words)
        if gram_hit >= 0.55 or word_hit >= 0.90:
            covered += 1
        else:
            missing.append(para[:100])

    coverage = covered / len(meaningful)
    result.text_coverage      = coverage
    result.missing_paragraphs = missing

    if coverage >= 0.92:
        result.text_score = 5.0   # conservative ceiling when using fallback
    elif coverage >= 0.80:
        result.text_score = 4.0
        _add_issue_fn(
            result, "text_accuracy", "minor",
            f"D3-fallback — Coverage {coverage:.0%}, {len(missing)} para(s) unmatched. "
            f"(LLM auth failed — deterministic result only.)"
        )
    else:
        result.text_score = 2.0
        _add_issue_fn(
            result, "text_accuracy", "major",
            f"D3-fallback — Low coverage {coverage:.0%}. {len(missing)} para(s) missing. "
            f"(LLM auth failed — deterministic result only.)",
            impact=f"{len(missing)} paragraphs",
        )

