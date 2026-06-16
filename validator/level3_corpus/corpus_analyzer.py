"""
level3_corpus/corpus_analyzer.py
──────────────────────────────────
ONE-TIME preprocessing: analyze all 98 vendor SGML files and build
statistical baselines for anomaly detection.

Run once with:
    python -m validator.level3_corpus.corpus_analyzer

Saves corpus_patterns.json which is loaded at runtime by pattern_matcher.py.

Key design note (blocker 2 resolution):
  L3 is ANOMALY DETECTION for new production documents, NOT an accuracy
  metric on vendor files (circular logic).  The corpus_patterns.json is
  used only when validating new pipeline-generated SGML.
"""

import json
import re
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

# ── Path configuration ─────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
OUTPUT_PATH = SCRIPT_DIR / "corpus_patterns.json"

# Jurisdiction → directory name mapping
JURI_DIR_MAP = {
    "Alberta": "Alberta",
    "British_Columbia": "British_Columbia",
    "CIRO_": "CIRO",
    "Manitoba": "Manitoba",
    "Montreal-Exchange": "Montreal",
    "NB_": "New_Brunswick",
    "NFLD_": "Newfoundland",
    "NS_": "Nova_Scotia",
    "NWT_": "NWT",
    "Ontario": "Ontario",
    "PEI": "PEI",
    "Quebec": "Quebec",
    "Saskatchewan": "Saskatchewan",
    "Toronto-Stock-Exchange": "TSX",
    "Yukon_": "Yukon",
}

# ── Label → document type classification ──────────────────────────────────────
def classify_doc_type(label: str, tags_used: set) -> str:
    """Classify into BLOCK-based (notice/announcement) or PART-based (instrument/rule)."""
    if "PART" in tags_used or "SEC" in tags_used:
        return "instrument"
    if any(x in label for x in ["Instrument", "Rule", "Regulation", "Order", "By-Law"]):
        return "instrument"
    return "notice"


def detect_jurisdiction_from_path(path: str) -> str:
    """Extract jurisdiction from the zip path."""
    parts = path.split("/")
    if len(parts) >= 2:
        return JURI_DIR_MAP.get(parts[1], "unknown")
    return "unknown"


def detect_jurisdiction_from_content(raw: str, label: str) -> str:
    """Fallback: use LABEL or document number."""
    if "OSC" in label or "Ontario" in label:
        return "Ontario"
    if "BC" in label or "British Columbia" in label:
        return "British_Columbia"
    if "TSX" in label or "TMX" in label:
        return "TSX"
    if "CIRO" in label:
        return "CIRO"
    if "ASC" in label or "Alberta" in label:
        return "Alberta"
    if "MSC" in label or "Manitoba" in label:
        return "Manitoba"
    if "CSA" in label:
        return "CSA_National"
    return "unknown"


def analyze_sgml_file(raw: str, fname: str) -> dict[str, Any]:
    """Extract statistical features from one SGML file."""
    tags = re.findall(r"<([A-Z][A-Z0-9]*)", raw)
    tag_counts = Counter(tags)
    unique_tags = set(tags)

    # POLIDOC attrs
    label = ""
    m = re.search(r'LABEL="([^"]+)"', raw)
    if m:
        label = m.group(1)

    # Nesting depth (count max stack depth)
    max_depth = 0
    depth = 0
    for match in re.finditer(r"<(/?)([A-Z][A-Z0-9]*)", raw):
        if match.group(1):
            depth = max(0, depth - 1)
        else:
            depth += 1
            max_depth = max(max_depth, depth)

    word_count = len(re.sub(r"<[^>]+>", " ", raw).split())
    line_count = len(raw.splitlines())

    return {
        "filename": fname.split("/")[-1],
        "label": label,
        "doc_type": classify_doc_type(label, unique_tags),
        "word_count": word_count,
        "line_count": line_count,
        "max_nesting_depth": max_depth,
        "tag_counts": dict(tag_counts.most_common(30)),
        "unique_tags": sorted(unique_tags),
        "table_count": tag_counts.get("SGMLTBL", 0),
        "footnote_count": tag_counts.get("FOOTNOTE", 0) + tag_counts.get("FN", 0),
        "has_graphics": "GRAPHIC" in unique_tags,
        "has_def": "DEF" in unique_tags,
        "has_part": "PART" in unique_tags,
        "block_levels": sorted(
            set(int(t[5:]) for t in unique_tags if t.startswith("BLOCK") and t[5:].isdigit())
        ),
    }


def build_corpus_stats(docs: list[dict]) -> dict[str, Any]:
    """Compute aggregate statistics for a group of documents."""
    if not docs:
        return {}

    word_counts = [d["word_count"] for d in docs]
    depths = [d["max_nesting_depth"] for d in docs]
    n = len(docs)

    avg_wc = sum(word_counts) / n
    std_wc = (sum((x - avg_wc) ** 2 for x in word_counts) / max(1, n)) ** 0.5

    avg_depth = sum(depths) / n
    std_depth = (sum((x - avg_depth) ** 2 for x in depths) / max(1, n)) ** 0.5

    # Most common tags (appear in ≥50% of docs in group)
    tag_presence: Counter = Counter()
    for d in docs:
        for t in d["unique_tags"]:
            tag_presence[t] += 1
    common_tags = [t for t, c in tag_presence.items() if c >= n * 0.5]

    # LABEL distribution
    label_dist = Counter(d["label"] for d in docs)

    return {
        "doc_count": n,
        "word_count": {
            "mean": avg_wc,
            "std": std_wc,
            "min": min(word_counts),
            "max": max(word_counts),
            "p10": sorted(word_counts)[max(0, int(n * 0.10))],
            "p90": sorted(word_counts)[min(n - 1, int(n * 0.90))],
        },
        "nesting_depth": {
            "mean": avg_depth,
            "std": std_depth,
            "max": max(depths),
        },
        "common_tags": common_tags,
        "label_distribution": dict(label_dist.most_common(10)),
    }


def run_corpus_analysis(juri_zip_path: str, output_path: str = None) -> dict:
    """
    Analyze all 98 vendor SGML files and save corpus_patterns.json.

    Parameters
    ----------
    juri_zip_path : str
        Path to juri.zip containing all vendor SGM files.
    output_path : str, optional
        Where to save the JSON. Defaults to corpus_patterns.json next to this file.
    """
    if output_path is None:
        output_path = str(OUTPUT_PATH)

    all_docs: list[dict] = []
    by_jurisdiction: dict[str, list] = defaultdict(list)
    by_doc_type: dict[str, list] = defaultdict(list)

    with zipfile.ZipFile(juri_zip_path) as z:
        sgm_files = [n for n in z.namelist() if n.endswith(".sgm")]
        print(f"Analyzing {len(sgm_files)} vendor SGML files...")

        for fname in sgm_files:
            try:
                raw = z.read(fname).decode("utf-8", errors="replace")
            except Exception as e:
                print(f"  SKIP {fname}: {e}")
                continue

            doc = analyze_sgml_file(raw, fname)
            doc["jurisdiction"] = detect_jurisdiction_from_path(fname)
            if doc["jurisdiction"] == "unknown":
                doc["jurisdiction"] = detect_jurisdiction_from_content(
                    raw, doc["label"]
                )

            all_docs.append(doc)
            by_jurisdiction[doc["jurisdiction"]].append(doc)
            by_doc_type[doc["doc_type"]].append(doc)

    print(f"  Analyzed {len(all_docs)} files")
    print(f"  Jurisdictions: {list(by_jurisdiction.keys())}")
    print(f"  Doc types: {dict((k, len(v)) for k, v in by_doc_type.items())}")

    # Build baselines
    corpus = {
        "version": "1.0",
        "total_docs": len(all_docs),
        "overall": build_corpus_stats(all_docs),
        "by_jurisdiction": {
            juri: build_corpus_stats(docs)
            for juri, docs in by_jurisdiction.items()
        },
        "by_doc_type": {
            dtype: build_corpus_stats(docs)
            for dtype, docs in by_doc_type.items()
        },
        # Combined: jurisdiction × doc_type
        "by_jurisdiction_and_type": {},
        # All known LABEL values
        "known_labels": sorted(set(d["label"] for d in all_docs if d["label"])),
        # Complete tag whitelist (confirmed from corpus)
        "all_tags_seen": sorted(set(t for d in all_docs for t in d["unique_tags"])),
    }

    # Build jurisdiction × doc_type baselines
    for juri, docs in by_jurisdiction.items():
        notices = [d for d in docs if d["doc_type"] == "notice"]
        instruments = [d for d in docs if d["doc_type"] == "instrument"]
        corpus["by_jurisdiction_and_type"][juri] = {
            "notice": build_corpus_stats(notices) if notices else {},
            "instrument": build_corpus_stats(instruments) if instruments else {},
        }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(corpus, f, indent=2, ensure_ascii=False)

    print(f"  Saved corpus patterns → {output_path}")
    return corpus


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python corpus_analyzer.py <path_to_juri.zip>")
        sys.exit(1)
    run_corpus_analysis(sys.argv[1])
