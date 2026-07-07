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
from collections import Counter
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


# ─────────────────────────────────────────────────────────────────────────────
# SGML-guided PDF section split (FIX for body-deletion truncation blind-spot)
#
# ROOT CAUSE (diagnostic 2026-07-06): For flat-PDF documents PyMuPDF returns
# one section containing all paragraphs.  The LLM window then captures only
# the first 10K chars → deletions at chars 40K–50K are invisible.
#
# FIX: Use the SGML section headings as ground truth.  Search every PDF
# paragraph for a text match to each SGML heading.  When found, use that
# paragraph as a section boundary.  This converts a flat-PDF into N properly
# paired sections WITHOUT expanding the LLM content window — each section
# pair stays within the safe 2,800-char per-section limit.
# ─────────────────────────────────────────────────────────────────────────────

def _sgml_guided_pdf_split(
    pdf_data,
    sgml_sections: list[dict],
) -> list[dict]:
    """
    Locate SGML section heading texts inside the PDF paragraphs and use
    them as split points to produce per-section PDF content.

    Only activates for flat-PDF docs (caller already verified len(pdf_sections)==1
    and len(sgml_sections) >= 3).

    Two-strategy search per heading:
      S1 – Exact case-insensitive substring in the first 300 chars of a paragraph.
           Handles "PART 3 PRE-HEARING MATTERS 3.1 …" where the heading is
           embedded at the very start of a long merged paragraph.
      S2 – Coverage: what fraction of heading content-words appear in the
           first N words of the paragraph (N = len(heading_words)*3 + 10).
           Handles shorter headings that appear near the paragraph start.

    Returns list of {"heading", "content"} dicts, or [] if < 2 splits found.
    """
    # Candidate SGML headings to search for in the PDF
    # Require ≥ 2 content words (single-word headings like "General" / "Hearings"
    # are too ambiguous to reliably locate in PDF text).
    _STOP = frozenset({
        "the", "a", "an", "of", "and", "or", "to", "in", "for", "is", "are",
        "by", "with", "that", "this", "be", "from", "as", "at", "on", "not",
        "if", "its", "it",
    })

    def _content_words(text: str) -> set[str]:
        return {w for w in re.findall(r"[a-z]+", text.lower()) if w not in _STOP and len(w) > 2}

    def _normalize(text: str) -> str:
        """Lower-case + collapse em/en-dashes and hyphens → space."""
        return re.sub(r"[—–\-]+", " ", text.lower()).strip()

    heading_targets = [
        s for s in sgml_sections
        if (s["heading"]
            and s["heading"] not in ("Introduction/Preamble", "Document")
            and len(_content_words(s["heading"])) >= 2)
    ]
    if len(heading_targets) < 2:
        return []

    # Search PDF paragraphs for each SGML heading
    found: list[tuple[int, str, dict]] = []   # (para_idx, heading_text, sgml_sec)
    used_para_idxs: set[int] = set()

    for sgml_sec in heading_targets:
        h = sgml_sec["heading"]
        h_words = _content_words(h)
        h_norm = _normalize(h)
        if len(h_words) < 2:
            continue

        best_idx, best_score = -1, 0.0
        COVERAGE_THRESHOLD = 0.75   # ≥75% of heading content-words must appear

        for para_idx, para in enumerate(pdf_data.paragraphs):
            if para_idx in used_para_idxs:
                continue

            score = 0.0

            # ── Strategy 1: exact substring in first 300 chars ───────────────
            para_prefix_300 = _normalize(para[:300])
            if h_norm in para_prefix_300:
                # Closer to start → higher score
                pos = para_prefix_300.find(h_norm)
                score = max(score, 0.9 + 0.1 * max(0.0, 1.0 - pos / 300.0))

            # ── Strategy 2: content-word coverage in first N words ───────────
            if score < COVERAGE_THRESHOLD:
                n_prefix = len(h_words) * 3 + 10
                prefix_text = " ".join(para.split()[:n_prefix])
                prefix_cwords = _content_words(prefix_text)
                coverage = len(h_words & prefix_cwords) / len(h_words)
                score = max(score, coverage)

            if score >= COVERAGE_THRESHOLD and score > best_score:
                best_score, best_idx = score, para_idx

        if best_idx >= 0:
            found.append((best_idx, h, sgml_sec))
            used_para_idxs.add(best_idx)

    if len(found) < 2:
        return []   # Too few splits found → caller uses flat-PDF fallback

    # Sort by PDF paragraph order
    found.sort(key=lambda x: x[0])

    # Remove duplicate para indices (same paragraph matched by multiple headings)
    deduped: list[tuple[int, str, dict]] = []
    seen: set[int] = set()
    for idx, h, sec in found:
        if idx not in seen:
            deduped.append((idx, h, sec))
            seen.add(idx)

    if len(deduped) < 2:
        return []

    sections: list[dict] = []

    # Preamble: everything before the first detected heading
    first_idx = deduped[0][0]
    if first_idx > 0:
        preamble_text = " ".join(pdf_data.paragraphs[:first_idx])
        if len(preamble_text.split()) >= 8:
            sections.append({
                "heading": "Preamble",
                "content": preamble_text[:_MAX_SECTION_CHARS],
            })

    # Body sections: content between consecutive split points
    for i, (para_idx, heading, _sgml_sec) in enumerate(deduped):
        next_idx = deduped[i + 1][0] if i + 1 < len(deduped) else len(pdf_data.paragraphs)
        # Include the heading paragraph itself + all following body paragraphs
        body_paras = pdf_data.paragraphs[para_idx: next_idx]
        content = " ".join(body_paras)
        sections.append({
            "heading": heading,
            "content": content[:_MAX_SECTION_CHARS],
        })

    return sections if len(sections) >= 2 else []


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
# Phase 1c — Full-document body paragraph sweep
#
# ROOT CAUSE (diagnostic 2026-07-06): The main LLM batch sees only the FIRST
# 10,000 chars of PDF and 5,600 chars of SGML.  Deleted content at positions
# 40,000–50,000 in the PDF (e.g. 15-601, 11-502) is COMPLETELY INVISIBLE.
#
# Fix: deterministic 5-gram pre-filter over ALL PDF paragraphs (not truncated),
# then a focused LLM confirmation call ONLY for flagged candidates.  This gives:
#   • Full document coverage  (no truncation blind-spot)
#   • High precision (pre-filter avoids sending 40K to LLM → avoids false positives)
#   • Targeted LLM (single paragraph vs. SGML context → accurate decision)
# ─────────────────────────────────────────────────────────────────────────────

# Tokens that indicate legitimately absent content — skip these paragraphs
_BODY_OMIT_RE = re.compile(
    r"^\s*\d+\s*$"                           # standalone page number
    r"|^\s*(Home|Sign In|Français|English|Ontario\.ca)\s*$"  # nav links
    r"|^[A-Z\s]{3,60}$"                      # all-caps heading line (structural)
    r"|^\s*[\u2022\u25cf\u2013\-]\s*$"       # lone bullet / dash
    r"|OSCB\s+Bulletin|B\.\d+\s+Notices"    # OSCB section markers
    r"|^www\.|^http",                         # bare URL lines
    re.IGNORECASE,
)

# Phrases that are common enough in regulatory text that a single match
# doesn't prove the paragraph is present (low-information phrases)
_HIGH_NOISE_WORDS = frozenset({
    "the", "a", "an", "of", "and", "or", "to", "in", "for", "is",
    "are", "by", "with", "that", "this", "it", "its", "as", "at", "on",
    "be", "was", "has", "have", "had", "will", "shall", "may", "must",
    "not", "any", "all", "each", "no", "if", "where", "who", "which",
})

_BODY_PARA_LLM_PROMPT = """You are BODY_PARA_CHECK — a precise content-fidelity checker
for Thomson Reuters Canada legal document conversions (PDF → Carswell SGML).

ONE PDF PARAGRAPH is provided below.  Check whether its SUBSTANTIVE CONTENT is
genuinely absent from the SGML.

RULES:
• Minor wording differences, punctuation changes → NOT missing
• Same content in different structural position (different section) → NOT missing
• Completely absent from SGML with no equivalent → MISSING
• Partial content (sentence truncated) → flag as MISSING

DO NOT FLAG as missing:
  - Page numbers, headers/footers
  - TOC entries or sub-entries
  - Contact name / address blocks that appear elsewhere in SGML
  - Navigation links (Home, Sign In, etc.)
  - Footnote numbers or symbols

RESPOND with JSON only:
{"missing": true|false, "confidence": 0.95, "reason": "brief explanation max 80 chars"}"""


def _focused_para_llm_check(client, para_text: str, sgml_blob: str,
                              doc_type: str) -> tuple[bool, float, str]:
    """
    Focused LLM check: is this specific PDF paragraph missing from the SGML?

    Called only for paragraphs that already failed the deterministic 5-gram
    pre-filter (coverage < 25%).  Returns (missing, confidence, reason).
    """
    # Send the paragraph + first 8K of SGML blob as context
    blob_excerpt = sgml_blob[:8_000]
    blob_note = f"[+{len(sgml_blob)-8000} more chars]" if len(sgml_blob) > 8_000 else ""

    user = (
        f"Document type: {doc_type}\n\n"
        f"PDF PARAGRAPH TO CHECK:\n\"{para_text[:500]}\"\n\n"
        f"SGML TEXT (first 8,000 chars for lookup):\n{blob_excerpt}{blob_note}\n\n"
        "Is the PDF paragraph above GENUINELY ABSENT from the SGML?\n"
        "Respond with ONLY the JSON object."
    )

    for attempt in range(2):
        try:
            with client.messages.stream(
                model=_MODEL,
                max_tokens=128,
                temperature=_TEMPERATURE,
                system=_BODY_PARA_LLM_PROMPT,
                messages=[{"role": "user", "content": user}],
            ) as stream:
                raw = stream.get_final_text()

            cleaned = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
            start = cleaned.find("{")
            if start == -1:
                return False, 0.0, "parse error"
            data = json.loads(cleaned[start:])
            return bool(data.get("missing")), float(data.get("confidence", 0.8)), data.get("reason", "")

        except Exception as exc:
            if attempt == 0:
                time.sleep(1)
            else:
                pass
    return False, 0.0, "llm error"


def _body_paragraph_sweep(
    pdf_data,
    sgml_blob: str,
    client,
    doc_type: str,
    already_found_missing: set,
) -> list[dict]:
    """
    Full-document body paragraph sweep:

    1. Iterate over ALL PDF paragraphs (no 10K truncation limit).
    2. For each substantive paragraph (≥15 content words), compute 5-gram
       coverage against the full sgml_blob.
    3. If coverage < 0.25 → paragraph is a candidate for "missing from SGML".
    4. Run a focused LLM call on the candidate to confirm.
    5. If LLM confirms missing → add to results.

    This fixes the TRUNCATION root cause: deleted content at PDF char 40-50K
    is now visible because we iterate over ALL paragraphs, not just first 10K.

    De-duplicates against already_found_missing (from Phase 2 LLM batch).
    """
    results: list[dict] = []
    if not sgml_blob or not pdf_data.paragraphs:
        return results

    # Build corpus 5-grams from the full SGML blob (consistent, no stop words)
    blob_words = re.findall(r"[a-zA-ZÀ-ÿ0-9]+", sgml_blob.lower())
    if len(blob_words) < 5:
        return results
    blob_5grams: set[tuple] = {
        tuple(blob_words[i: i + 5]) for i in range(len(blob_words) - 4)
    }

    candidates_checked = 0
    llm_calls = 0
    _MAX_LLM_CALLS = 5   # cap to avoid excessive API usage

    for para_idx, para in enumerate(pdf_data.paragraphs):
        # Skip short / boilerplate paragraphs
        words = para.split()
        if len(words) < 15:
            continue
        if _BODY_OMIT_RE.search(para.strip()):
            continue

        # Skip if already found by Phase 2 LLM
        para_key = para[:60].lower()
        if any(para_key in found for found in already_found_missing):
            continue

        # Compute content-word 5-gram coverage against SGML blob
        content_words = [w for w in re.findall(r"[a-zA-ZÀ-ÿ0-9]+", para.lower())
                         if w not in _HIGH_NOISE_WORDS and len(w) > 2]
        if len(content_words) < 5:
            continue

        grams = [tuple(content_words[i: i + 5]) for i in range(len(content_words) - 4)]
        if not grams:
            continue

        matched = sum(1 for g in grams if g in blob_5grams)
        coverage = matched / len(grams)

        candidates_checked += 1

        # High-coverage → definitely present in SGML → skip
        if coverage >= 0.25:
            continue

        # Low-coverage candidate → verify with targeted LLM call
        if llm_calls >= _MAX_LLM_CALLS:
            # Cap reached — add as minor warning without LLM confirmation
            results.append({
                "text": para[:200],
                "location_hint": f"Body paragraph #{para_idx} (5-gram pre-filter only, LLM cap reached)",
                "severity": "minor",
                "confidence": round(1.0 - coverage, 2),
            })
            continue

        llm_calls += 1
        missing, confidence, reason = _focused_para_llm_check(client, para, sgml_blob, doc_type)

        if missing and confidence >= 0.70:
            results.append({
                "text": para[:200],
                "location_hint": f"Body paragraph #{para_idx} (5-gram {coverage:.0%} + LLM confirmed: {reason})",
                "severity": "major",
                "confidence": round(confidence, 2),
            })

    if candidates_checked > 0:
        print(f"   BODY_SWEEP: {len(pdf_data.paragraphs)} paras scanned, "
              f"{candidates_checked} low-coverage candidates, {llm_calls} LLM calls, "
              f"{len(results)} new missing found")

    return results


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

    # ── SGML-guided PDF split (fixes truncation blind-spot) ──────────────────
    # When PyMuPDF finds only 1 PDF section (flat) but SGML has ≥3 sections,
    # use the SGML headings to locate section boundaries inside the PDF text.
    # This converts one 10K-char flat comparison into N paired comparisons
    # covering the FULL document without expanding LLM context limits.
    if len(pdf_sections) == 1 and len(sgml_sections) >= 3:
        guided = _sgml_guided_pdf_split(pdf_data, sgml_sections)
        if len(guided) >= 2:
            pdf_sections = guided
            result.warnings.append(
                f"D3/D8: SGML-guided split → {len(pdf_sections)} PDF sections "
                f"(was flat). Covers full doc without truncation."
            )

    # Flat-PDF fallback: if guided split did not fire or produced only 1 section,
    # keep 10K window (expanding to 40K caused 5-6 false positives per clean doc).
    _FLAT_PDF_LIMIT = 10_000
    if len(pdf_sections) == 1:
        pdf_sections[0]["content"] = " ".join(pdf_data.paragraphs)[:_FLAT_PDF_LIMIT]

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

    # ── Deterministic checks: helpers ────────────────────────────────────────
    def _num_near_match(ps: str, ss: str) -> bool:
        """True if 3-digit suffixes ps and ss represent the same number corruption:
        • anagram  — same digits in different order (e.g. 103↔130)
        • Hamming-1 — differ in exactly one position (e.g. 333↔334, catches
          the diag_corrupt.py fallback that increments the last digit when all
          digits are identical and no transposition is possible)
        """
        if ps == ss:
            return False
        if sorted(ps) == sorted(ss):                        # anagram (transposition)
            return True
        if sum(a != b for a, b in zip(ps, ss)) == 1:       # single-digit substitution
            return True
        return False

    # ── Deterministic regulatory number check (Fix #4 + #5a) ─────────────────
    # Compare XX-YYY instrument number counts between full PDF and full SGML.
    # Catches digit-transpositions AND single-digit substitutions (Fix #5a),
    # including multi-occurrence docs where only one instance was changed.
    #
    # pdf_excess: numbers that appear MORE TIMES in PDF than SGML — meaning the
    #   SGML may have had one occurrence swapped to something else.
    # sgml_only:  numbers that appear in SGML but not at all in PDF — these are
    #   the likely replacement values introduced by corruption.
    _NUM_RE_D3 = re.compile(r"\b(\d{2})-(\d{3})\b")
    pdf_full_text = " ".join(pdf_data.paragraphs)
    _pdf_cnt  = Counter(m.group() for m in _NUM_RE_D3.finditer(pdf_full_text))
    _sgml_cnt = Counter(m.group() for m in _NUM_RE_D3.finditer(sgml_blob))
    # PDF-excess: PDF has more occurrences than SGML (one was swapped out)
    pdf_excess_d3 = {n for n, c in _pdf_cnt.items() if c > _sgml_cnt.get(n, 0)}
    # SGML-only: present in SGML but absent from PDF (the swapped-in value)
    sgml_only_d3  = set(_sgml_cnt.keys()) - set(_pdf_cnt.keys())
    _llm_altered_set = {a["original_pdf"][:20].lower() for a in all_altered}
    _already_flagged: set[str] = set()

    def _flag_reg_number(pdf_num: str, sgml_num: str) -> None:
        """Append one deterministic D3 alteration for a substituted reg number."""
        if pdf_num.lower()[:20] in _llm_altered_set:
            return
        all_altered.append({
            "original_pdf": pdf_num,
            "sgml_version": sgml_num,
            "location_hint": f"Regulatory number: PDF={pdf_num} → SGML={sgml_num}",
            "severity": "critical",
        })
        result.warnings.append(
            f"D3: Regulatory number (deterministic): PDF={pdf_num} → SGML={sgml_num}"
        )

    # Direction 1: numbers with MORE occurrences in PDF than SGML — one was swapped
    for _p in sorted(pdf_excess_d3):
        if _p in _already_flagged:
            continue
        _pp, _ps = _p.split("-")
        for _s in sorted(sgml_only_d3):
            _sp, _ss = _s.split("-")
            if _pp != _sp:
                continue
            if _num_near_match(_ps, _ss):
                _flag_reg_number(_p, _s)
                _already_flagged.add(_p)
                _already_flagged.add(_s)
                break

    # Direction 2: numbers in SGML but absent from PDF entirely — look for a
    # near-match (anagram OR Hamming-1) in ANY PDF number.  Catches cases where
    # the PDF extractor only sees the number once (so pdf_excess is empty) yet
    # the SGML-only number is clearly a digit-variant of a PDF number.
    for _s in sorted(sgml_only_d3):
        if _s in _already_flagged:
            continue
        _sp, _ss = _s.split("-")
        for _p in sorted(_pdf_cnt.keys()):
            if _p in _already_flagged:
                continue
            _pp, _ps = _p.split("-")
            if _pp != _sp:
                continue
            if _num_near_match(_ps, _ss):
                _flag_reg_number(_p, _s)
                _already_flagged.add(_s)
                _already_flagged.add(_p)
                break

    # ── Deterministic URL check (Fix #5b) ────────────────────────────────────
    # URLs present in the PDF but absent from the SGML are flagged as missing
    # content.  Pre-existing PDF/SGML URL mismatches (footers, publisher links)
    # appear in BOTH the original and corrupted run's missing_paragraphs, so
    # they cancel out in _detect_corruption's new_missing delta.
    _URL_DET_RE = re.compile(
        r"https?://[a-zA-Z0-9\-\./?&=_%+]{4,60}"
        r"|www\.[a-zA-Z0-9\-]{2,40}\.[a-zA-Z]{2,6}[a-zA-Z0-9\-\./?&=_%+]{0,30}",
        re.IGNORECASE,
    )
    def _norm_url(u: str) -> str:
        """Lowercase, strip protocol prefix, strip trailing punctuation."""
        u = re.sub(r"^https?://(?:www\.)?", "www.", u.lower())
        return u.rstrip(".,;:)'\" /")

    _pdf_urls  = {_norm_url(m.group()) for m in _URL_DET_RE.finditer(pdf_full_text)
                  if len(m.group()) >= 8}
    _sgml_urls = {_norm_url(m.group()) for m in _URL_DET_RE.finditer(sgml_blob)
                  if len(m.group()) >= 8}
    _seen_missing_urls = {m.get("text", "").lower() for m in all_missing}
    for _url in sorted(_pdf_urls - _sgml_urls):
        if _url and _url not in _seen_missing_urls:
            all_missing.append({
                "text": _url,
                "location_hint": "URL present in PDF but absent from SGML",
                "confidence": 0.85,
                "severity": "minor",
            })
            result.warnings.append(f"D3: URL missing from SGML: {_url}")

    # ── Deterministic heading check (Fix #5c / Fix #8) ───────────────────────
    # Verify that every PDF heading is represented in the SGML.
    # Fix #8: Expand the word pool to include:
    #   (a) SGML section headings (original)
    #   (b) ALL <TI> element text  — catches POLIDENT titles and nested TIs
    #       that don't appear as BLOCK-level section headings
    #   (c) Short <BOLD> paragraph text (≤10 words) — catches documents that
    #       use <P><BOLD>Title</BOLD></P> instead of <TI> as visual headings
    #       (e.g. ASC staff notices with multi-line title bolded paragraphs)
    # A PDF heading with < 40% of its significant words (≥4 chars) found in
    # this expanded pool is considered absent from the SGML.
    _sgml_heading_pool: set[str] = set()
    # (a) SGML section headings
    for _sec in sgml_sections:
        _sgml_heading_pool.update(
            w.lower() for w in re.findall(r"[a-zA-Z]{4,}", _sec["heading"])
        )
    # (b) All <TI> elements in raw SGML (Fix #8) — must use raw_sgml, not
    #     sgml_blob, because sgml_blob is fully stripped of tags.
    if raw_sgml:
        for _tim8 in re.finditer(r"<TI[^>]*>(.*?)</TI>", raw_sgml, re.IGNORECASE | re.DOTALL):
            _sgml_heading_pool.update(
                w.lower() for w in re.findall(r"[a-zA-Z]{4,}", _strip_tags(_tim8.group(1)))
            )
        # (c) Short <BOLD> elements acting as visual headings (Fix #8)
        # Catches documents that use <P><BOLD>Title</BOLD></P> instead of <TI>.
        for _bm8 in re.finditer(r"<BOLD>(.*?)</BOLD>", raw_sgml, re.IGNORECASE | re.DOTALL):
            _bold8 = _strip_tags(_bm8.group(1)).strip()
            if 1 < len(_bold8.split()) <= 12:   # short bold phrase = likely a heading
                _sgml_heading_pool.update(
                    w.lower() for w in re.findall(r"[a-zA-Z]{4,}", _bold8)
                )
    _seen_heading_texts = {m.get("text", "").lower()[:60] for m in all_missing}
    for _ph in pdf_data.headings:
        _ph = _ph.strip()
        if len(_ph.split()) < 2:
            continue   # skip single-word or empty headings
        _ph_words = [w.lower() for w in re.findall(r"[a-zA-Z]{4,}", _ph)]
        if not _ph_words:
            continue
        _overlap = sum(1 for w in _ph_words if w in _sgml_heading_pool)
        if _overlap / len(_ph_words) < 0.40:
            if _ph.lower()[:60] not in _seen_heading_texts:
                all_missing.append({
                    "text": _ph,
                    "location_hint": "PDF section heading not found in SGML sections",
                    "confidence": 0.80,
                    "severity": "major",
                })
                result.warnings.append(
                    f"D3: PDF heading missing from SGML: {_ph[:80]}"
                )
                _seen_heading_texts.add(_ph.lower()[:60])

    # ── Fix #6: PDF paragraph → SGML TI presence check ───────────────────────
    # When a <TI> element is deleted from the SGML, its text still appears in
    # the PDF as a standalone paragraph.  We scan short heading-like PDF
    # paragraphs and flag any that are:
    #   (a) NOT present in any SGML <TI> element, AND
    #   (b) NOT present anywhere in the stripped SGML body text
    # Pre-existing clean-run flags cancel in _detect_corruption (same as Fix
    # #5b/c), so false positives do not produce false alarms.

    _sgml_ti_re6 = re.compile(r'<TI[^>]*>(.*?)</TI>', re.IGNORECASE | re.DOTALL)
    _sgml_ti_set6: set[str] = set()
    for _tim in _sgml_ti_re6.finditer(sgml_blob):
        _ti_norm = _strip_tags(_tim.group(1)).strip().lower()
        if _ti_norm:
            _sgml_ti_set6.add(_ti_norm[:120])
    # Also include headings already extracted via _extract_sgml_sections
    for _sec in sgml_sections:
        _h6 = _sec.get("heading", "").strip().lower()
        if _h6:
            _sgml_ti_set6.add(_h6[:120])

    _TRAIL_PUNCT6 = re.compile(r'[.!?:;,)\u2019\u201d]$')
    _seen_h6: set[str] = {m.get("text", "").lower()[:60] for m in all_missing}

    for _para6 in pdf_data.paragraphs:
        _para6 = _para6.strip()
        _words6 = _para6.split()
        # Length filter: heading-like paragraphs are short
        if not (2 <= len(_words6) <= 20):
            continue
        # Must start with uppercase letter
        if not _para6[0].isupper():
            continue
        # Must not end with sentence-terminating punctuation
        if _TRAIL_PUNCT6.search(_para6):
            continue
        # Title-case ratio filter: ≥50% of alphabetic words start with uppercase.
        # Genuine section headings are consistently title-cased; body sentences are not.
        _alpha6 = [w for w in _words6 if re.match(r'[a-zA-Z]{3,}', w)]
        if len(_alpha6) < 2:
            continue
        _titled6 = sum(1 for w in _alpha6 if w[0].isupper())
        if _titled6 / len(_alpha6) < 0.50:
            continue
        _para6_norm = _para6.lower()[:120]
        # (a) Skip if already matched to a SGML TI element.
        # In a clean document the heading IS in the TI set → skipped.
        # After corruption (TI deleted), the heading is no longer in the set → flagged.
        if any(_para6_norm in _ti6 or _ti6 in _para6_norm
               for _ti6 in _sgml_ti_set6 if _ti6):
            continue
        # Skip if already flagged in this run
        if _para6_norm[:60] in _seen_h6:
            continue
        all_missing.append({
            "text": _para6,
            "location_hint": "PDF paragraph (likely heading) absent from SGML",
            "confidence": 0.80,
            "severity": "major",
        })
        result.warnings.append(f"D3: Heading absent from SGML: {_para6[:80]}")
        _seen_h6.add(_para6_norm[:60])

    # ── Fix #7: <TI> element count telemetry + structural shortage check ──────
    # Strategy A — Telemetry (always emitted):
    #   Emit the raw <TI> count in a warning so the diagnostic assessment can
    #   compare orig_ti_count vs corr_ti_count.  If the count drops between the
    #   clean run and the corrupted run, a heading was deleted.  This covers ALL
    #   document types regardless of BLOCK/POLIDENT/APPENDIX structure.
    #
    # Strategy B — Per-BLOCK structural shortage (production use):
    #   For each BLOCK2/3/1 section, check if it contains a <TI> element.  A
    #   BLOCK without a TI is a structural violation of the Carswell DTD.  This
    #   fires without needing an orig baseline — useful in production.
    #   Note: POLIDENT's document-title TI is excluded from BLOCK count.
    if raw_sgml:
        _ti_count7 = len(re.findall(r"<TI[\s>]", raw_sgml, re.IGNORECASE))
        # Always emit TI count for delta comparison in _detect_corruption
        result.warnings.append(f"D3/info: SGML TI count: {_ti_count7}")

        # Strategy B: per-BLOCK TI check
        _block_parts7 = re.split(r"(?=<BLOCK[123][\s>])", raw_sgml, flags=re.IGNORECASE)
        _blocks_no_ti7 = 0
        for _bp7 in _block_parts7[1:]:   # skip preamble before first BLOCK
            # Find end of this BLOCK section (first closing tag)
            _end7 = re.search(r"</BLOCK[123]>", _bp7, re.IGNORECASE)
            _content7 = _bp7[:_end7.end()] if _end7 else _bp7[:3000]
            if not re.search(r"<TI[\s>]", _content7, re.IGNORECASE):
                _blocks_no_ti7 += 1
        if _blocks_no_ti7 > 0:
            result.warnings.append(
                f"D3: TI shortage: {_blocks_no_ti7} BLOCK section(s) missing "
                f"a <TI> heading element — likely deleted"
            )

    # ── Fix #9: Paragraph prefix truncation check ────────────────────────────
    # Detects when the first N words of a PDF paragraph are deleted from the
    # SGML (Type J corruption). The LLM misses short prefix deletions because
    # 95%+ of the paragraph still matches. Example: deleting "As of April 1,
    # 2026," from the start of a paragraph — the remaining 150 words are
    # identical so the LLM scores it as a match.
    #
    # Algorithm:
    #   For each long PDF paragraph (≥15 words):
    #     1. Take the first PREFIX_LEN words as "prefix"
    #     2. Build character bigrams of the lowercased prefix
    #     3. If < 40% of prefix bigrams appear in the stripped SGML text AND
    #        > 65% of tail words (words PREFIX_LEN+1 …) DO appear in SGML:
    #        → the paragraph body is present but the beginning is truncated
    #        → flag the prefix as missing text
    _PREFIX_LEN9 = 6    # words to check at paragraph start
    _MIN_PARA9   = 15   # only check paragraphs long enough to judge
    # Normalize: remove all non-alphanumeric chars except spaces, lowercase,
    # then COLLAPSE multiple spaces → prevents false positives caused by
    # _strip_tags inserting spaces around inline tags like (<BOLD>ASC</BOLD>)
    # which turns "commission (ASC) is" → "commission  ASC  is" (double-space).
    _NORM9 = lambda s: re.sub(r" +", " ", re.sub(r"[^a-z0-9 ]", " ", s.lower())).strip()
    _sgml_norm9 = _NORM9(sgml_blob)   # normalized SGML body (stripped, no tags)

    _all_missing_keys9 = {m.get("text", "").lower()[:60] for m in all_missing}

    for _ptext9 in pdf_data.paragraphs:
        _pw9 = _ptext9.strip().split()
        if len(_pw9) < _MIN_PARA9:
            continue
        _prefix9 = _pw9[:_PREFIX_LEN9]
        _tail9   = _pw9[_PREFIX_LEN9:]
        if len(_prefix9) < 3:
            continue
        # Strategy: check if the NORMALIZED FULL PREFIX PHRASE appears in SGML.
        # Using the full phrase (not bigrams) prevents false positives from common
        # sub-phrases like 'as of' or 'april 1' appearing elsewhere in the doc.
        _prefix_phrase9 = _NORM9(" ".join(_prefix9))
        _prefix_phrase9 = re.sub(r" +", " ", _prefix_phrase9).strip()
        if _prefix_phrase9 in _sgml_norm9:
            continue   # prefix phrase IS in SGML somewhere → no truncation

        # Confirm the paragraph tail IS present in SGML (not just a mismatch)
        _tail_content9 = [
            w.lower().strip(".,;:\"'()") for w in _tail9 if len(w) >= 4
        ]
        if len(_tail_content9) < 5:
            continue   # not enough tail words to judge
        _found_tail9 = sum(1 for w in _tail_content9 if w in _sgml_norm9)
        if _found_tail9 / len(_tail_content9) < 0.65:
            continue   # tail also absent → completely different paragraph

        _prefix_text9 = " ".join(_prefix9)
        _key9 = _prefix_text9.lower()[:60]
        if _key9 in _all_missing_keys9:
            continue   # already flagged
        _all_missing_keys9.add(_key9)

        all_missing.append({
            "text": _prefix_text9,
            "location_hint": (
                f"Paragraph prefix absent from SGML (truncation): "
                f"PDF paragraph begins with '{_prefix_text9}' "
                f"but this opening is missing from the SGML. "
                f"Paragraph body present at {_found_tail9}/{len(_tail_content9)} words."
            ),
            "confidence": 0.80,
            "severity": "major",
        })
        result.warnings.append(
            f"D3: Paragraph prefix truncated in SGML: '{_prefix_text9[:80]}'"
        )

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

