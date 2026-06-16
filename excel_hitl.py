"""
excel_hitl.py — TR Excel → SGML HITL Review
============================================
Feature-complete HITL reviewer for Excel-sourced SGML.

Features (matches PDF HITL Review):
  • Auto-loads SGML from last Excel pipeline run (via session_state carry-over)
  • Score breakdown with visual progress bars (L1–L4)
  • Highlighted SGML view — problem lines coloured by severity
  • Line-numbered editor with sync-scroll gutter
  • Actionable fixes panel — auto-fixable + manual, apply all or one-by-one
  • Download SGML + Save to disk
  • Decision logging → DECISIONS_FILE (shared with PDF HITL)
  • Decision History section with stats

Run standalone:
    $env:PYTHONUTF8=1
    streamlit run excel_hitl.py
"""
from __future__ import annotations

import html
import json
import sys
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

import streamlit as st
import streamlit.components.v1 as _cmp

# ── path bootstrap ────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
for _p in [str(_HERE), str(_HERE / "pipeline")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from config import DECISIONS_FILE                                # noqa: E402
from pipeline.excel_validator import (                           # noqa: E402
    validate,
    generate_excel_fixes,
    apply_excel_fixes,
)

# ── Constants ─────────────────────────────────────────────────────────────────
_DECISION_COLOURS = {
    "ACCEPT":               "#1a7f37",
    "ACCEPT_WITH_WARNINGS": "#9a6700",
    "REVIEW":               "#0550ae",
    "REJECT":               "#cf222e",
}
_SEV_BG = {
    "CRITICAL": "#ffcccc",
    "HIGH":     "#ffe4b5",
    "MEDIUM":   "#fffacd",
    "LOW":      "#e8f5e9",
    "INFO":     "#e3f2fd",
}
_SEV_HLIGHT = {
    "CRITICAL": "#ff4b4b44",
    "HIGH":     "#ff8c0044",
    "MEDIUM":   "#ffd70044",
    "LOW":      "#90ee9044",
}
_SEV_ICONS = {
    "CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🟢", "INFO": "🔵",
}
_LAYER_BARS = [
    ("L1_source_fidelity",    "L1 Source Fidelity", "#4c8bf5"),
    ("L2_structural",         "L2 Structural",      "#34a853"),
    ("L3_doctype_compliance", "L3 Doc-Type",        "#fbbc04"),
    ("L4_data_integrity",     "L4 Data Integrity",  "#ea4335"),
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def _colour_badge(decision: str) -> str:
    colour = _DECISION_COLOURS.get(decision, "#666")
    return (
        f'<span style="background:{colour};color:white;padding:3px 14px;'
        f'border-radius:4px;font-weight:bold;font-size:1rem">{decision}</span>'
    )


def _score_bar(label: str, score: float, max_score: float, colour: str) -> None:
    pct = min(100.0, score / max_score * 100) if max_score else 0
    st.markdown(
        f'<div style="margin-bottom:8px">'
        f'<div style="display:flex;justify-content:space-between;font-size:.85rem">'
        f'<span><b>{label}</b></span>'
        f'<span>{score:.0f}/{max_score} &nbsp;({pct:.0f}%)</span></div>'
        f'<div style="background:#e0e0e0;border-radius:4px;height:10px">'
        f'<div style="background:{colour};width:{pct}%;height:10px;border-radius:4px">'
        f'</div></div></div>',
        unsafe_allow_html=True,
    )


def _sev_badge(sev: str) -> str:
    col = _SEV_BG.get(sev, "#ccc")
    return (
        f'<span style="background:{col};color:#111;padding:1px 7px;'
        f'border-radius:4px;font-size:0.8em;font-weight:bold">{sev}</span>'
    )


def _render_sgml_highlighted(sgml_text: str, highlight_map: dict[int, str]) -> str:
    """Render SGML as scrollable HTML with line numbers + coloured problem lines."""
    lines = sgml_text.splitlines()
    parts = [
        '<div style="font-family:\'Courier New\',Courier,monospace;font-size:11.5px;'
        'overflow:auto;max-height:540px;border:1px solid #d0d0d0;'
        'border-radius:4px;padding:6px 8px;background:#fafafa;'
        'white-space:pre;line-height:1.5">'
    ]
    for i, line in enumerate(lines, 1):
        esc = html.escape(line)
        ln_span = (
            f'<span style="color:#aaa;user-select:none;margin-right:6px">'
            f'{i:4d} │</span>'
        )
        if i in highlight_map:
            bg = highlight_map[i]
            parts.append(
                f'<span style="display:block;background:{bg}">'
                f'{ln_span}{esc}</span>'
            )
        else:
            parts.append(f'<span style="display:block">{ln_span}{esc}</span>')
    parts.append("</div>")
    return "".join(parts)


def _build_highlight_map(fixes: list) -> dict[int, str]:
    """Map line number → highlight background colour (most severe wins)."""
    SEV_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
    best: dict[int, tuple] = {}
    for fix in fixes:
        ln  = fix.get("line", 0)
        sev = fix.get("severity", "LOW")
        if ln and sev in _SEV_HLIGHT:
            cur = best.get(ln)
            if cur is None or SEV_RANK[sev] < SEV_RANK[cur[0]]:
                best[ln] = (sev, _SEV_HLIGHT[sev])
    return {ln: v[1] for ln, v in best.items()}


def _render_line_numbered_editor(sgml_text: str) -> None:
    """Render a CodeMirror-style line-numbered textarea (sync-scroll gutter)."""
    lines = sgml_text.splitlines()
    gutter_rows = "".join(
        f'<div style="height:1.45em;line-height:1.45em;color:#94a3b8;'
        f'text-align:right;padding-right:8px;font-size:11.5px;'
        f'font-family:Consolas,monospace;user-select:none">{i}</div>'
        for i in range(1, len(lines) + 2)
    )
    escaped = html.escape(sgml_text)
    editor_html = (
        '<div style="display:flex;border:1px solid #d1d5db;border-radius:6px;'
        'overflow:hidden;background:#fff;font-size:11.5px;font-family:Consolas,monospace">'
        '<div id="gutter" style="background:#f8fafc;border-right:1px solid #e2e8f0;'
        f'padding:8px 0;min-width:46px;overflow:hidden;flex-shrink:0">{gutter_rows}</div>'
        '<textarea id="ta" style="flex:1;border:none;outline:none;resize:none;height:360px;'
        'font-size:11.5px;line-height:1.45em;padding:8px 10px;font-family:Consolas,monospace;'
        f'white-space:pre;overflow-wrap:normal;overflow-x:auto;background:#fff" '
        f'spellcheck="false">{escaped}</textarea></div>'
        '<script>(function(){{'
        'var ta=document.getElementById("ta");'
        'var g=document.getElementById("gutter");'
        'function syncLines(){{'
        'var n=ta.value.split("\\n").length;var h="";'
        'for(var i=1;i<=n+1;i++){{'
        'h+=\'<div style="height:1.45em;line-height:1.45em;color:#94a3b8;text-align:right;'
        'padding-right:8px;font-size:11.5px;font-family:Consolas,monospace;user-select:none">\'+i+"</div>";'
        '}}g.innerHTML=h;g.scrollTop=ta.scrollTop;}}'
        'ta.addEventListener("input",syncLines);'
        'ta.addEventListener("scroll",function(){{g.scrollTop=ta.scrollTop;}});'
        '}})();</script>'
    )
    _cmp.html(editor_html, height=382, scrolling=False)


def _save_decision(record: dict) -> None:
    DECISIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with DECISIONS_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _load_past_decisions() -> list[dict]:
    if not DECISIONS_FILE.exists():
        return []
    records = []
    for line in DECISIONS_FILE.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return [r for r in records if r.get("source") == "excel_hitl"]


# ── Page config ───────────────────────────────────────────────────────────────
# Guard: validator_app.py owns set_page_config when hosting both review modes.
# pages/3_Excel_HITL_Review.py (converter) exec()s without _SKIP_PAGE_CONFIG,
# so this runs normally in that context.
if not globals().get("_SKIP_PAGE_CONFIG"):
    st.set_page_config(
        page_title="Excel → SGML HITL Review",
        page_icon="📊",
        layout="wide",
    )

st.title("📊 Excel → SGML HITL Review")
st.caption(
    "Upload a generated SGML file (and optionally the source Excel) "
    "to validate, inspect issues, and apply one-click fixes."
)

# ── Session state init ────────────────────────────────────────────────────────
for _k in ["report", "fixes", "sgml_text", "sgm_name", "corrected", "sgm_path"]:
    if _k not in st.session_state:
        st.session_state[_k] = None

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("📂 Current Document")

    # Auto-load from Excel pipeline session carry-over
    _auto_sgml: str | None = st.session_state.get("last_excel_sgml_text")
    _auto_name: str        = st.session_state.get("last_excel_sgml_name") or "output.sgm"
    _auto_xlsx_bytes: bytes | None = st.session_state.get("last_excel_xlsx_bytes")
    _auto_xlsx_name: str           = st.session_state.get("last_excel_xlsx_name") or "source.xlsx"

    if _auto_sgml:
        _xlsx_banner = f'<span style="color:#0369a1">{_auto_xlsx_name}</span><br>' if _auto_xlsx_bytes else ""
        st.markdown(
            f'<div style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:6px;'
            f'padding:8px 10px;font-size:0.82em;color:#0c4a6e">'
            f'<b>📌 Loaded from pipeline</b><br>'
            f'{_xlsx_banner}'
            f'<span style="color:#0369a1">{_auto_name}</span></div>',
            unsafe_allow_html=True,
        )
        sgm_upload  = None
        xlsx_upload = None
    else:
        st.markdown(
            '<div style="background:#fefce8;border:1px solid #fde047;border-radius:6px;'
            'padding:8px 10px;font-size:0.82em;color:#713f12;margin-bottom:8px">'
            '<b>📤 Manual Upload Mode</b><br>'
            'Upload your SGML file and optionally the source Excel.</div>',
            unsafe_allow_html=True,
        )
        sgm_upload = st.file_uploader(
            "① Upload SGML file",
            type=["sgm", "sgml", "xml", "txt"],
            help="The .sgm file to validate",
            key="excel_sgml_upload",
        )
        xlsx_upload = st.file_uploader(
            "② Upload Excel file (optional)",
            type=["xlsx", "xls"],
            help="Source Excel file for L1 source comparison",
            key="excel_xlsx_upload",
        )
        st.markdown("---")
        if sgm_upload:
            st.markdown(f"✅ SGML: `{sgm_upload.name}`")
        if xlsx_upload:
            st.markdown(f"✅ Excel: `{xlsx_upload.name}`")
        if not sgm_upload:
            st.info("Upload an SGML file above to begin validation.")

    st.markdown("---")
    if st.session_state.report is not None:
        st.button("🔄 Re-validate", key="excel_revalidate_btn",
                  help="Re-run validator on current SGML")

# ── Resolve SGML source (upload > auto-loaded) ───────────────────────────────
if sgm_upload is not None:
    _resolved_sgml = sgm_upload.getvalue().decode("utf-8", errors="replace")
    _resolved_name = sgm_upload.name
elif _auto_sgml:
    _resolved_sgml = _auto_sgml
    _resolved_name = _auto_name
else:
    _resolved_sgml = None
    _resolved_name = None

# ── Auto-validate on new source (run_key pattern — same as PDF HITL) ─────────
_run_key = f"{_resolved_name}|{hash(_resolved_sgml) if _resolved_sgml else 'none'}"
_need_run = (
    _resolved_sgml is not None
    and (
        st.session_state.get("_excel_last_run") != _run_key
        or st.session_state.get("excel_revalidate_btn", False)
    )
)
if _need_run:
    raw_sgml = _resolved_sgml
    name     = _resolved_name

    with st.spinner("Running Excel SGML validator (L1–L4)…"):
        _sgm_tmp = tempfile.NamedTemporaryFile(
            delete=False, suffix=".sgm", mode="w", encoding="utf-8"
        )
        _sgm_tmp.write(raw_sgml)
        _sgm_tmp.close()
        sgm_path = Path(_sgm_tmp.name)

        xlsx_path: Optional[Path] = None
        if xlsx_upload:
            _xl_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
            _xl_tmp.write(xlsx_upload.getvalue())
            _xl_tmp.close()
            xlsx_path = Path(_xl_tmp.name)
        elif _auto_xlsx_bytes:
            _xl_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
            _xl_tmp.write(_auto_xlsx_bytes)
            _xl_tmp.close()
            xlsx_path = Path(_xl_tmp.name)

        report    = validate(sgm_path, xlsx_path)
        sgml_text = sgm_path.read_text(encoding="utf-8", errors="replace")
        fixes     = generate_excel_fixes(sgml_text, report)

    st.session_state.report         = report
    st.session_state.fixes          = fixes
    st.session_state.sgml_text      = sgml_text
    st.session_state.sgm_name       = name
    st.session_state.sgm_path       = sgm_path
    st.session_state.corrected      = None
    st.session_state._excel_last_run = _run_key

# ── Nothing yet ───────────────────────────────────────────────────────────────
if st.session_state.report is None:
    if _resolved_sgml is None:
        st.info("Upload an SGML file in the sidebar to begin validation.")
    else:
        st.info("⏳ Preparing validation…")
        st.rerun()
    st.stop()

report    = st.session_state.report
fixes     = st.session_state.fixes
sgml_text = st.session_state.sgml_text
sgm_name  = st.session_state.sgm_name
sgm_path  = st.session_state.sgm_path

current_sgml: str = st.session_state.corrected or sgml_text

sc       = report["scores"]
decision = report["decision"]

# ── Score / decision header ───────────────────────────────────────────────────
col_title, col_badge = st.columns([5, 2])
with col_title:
    st.subheader(f"📄 {sgm_name}")
with col_badge:
    st.markdown(_colour_badge(decision), unsafe_allow_html=True)

norm  = sc["normalised"]
total = sc["total"]
tmax  = sc["total_max"]
st.markdown(
    f"**Normalised score: {norm:.1f} / 100** &nbsp;|&nbsp; "
    f"Raw: {total} / {tmax} &nbsp;|&nbsp; "
    f"Doc type: `{report.get('doc_label', report.get('doc_type', '?'))}`"
)

# ── Score breakdown bars ──────────────────────────────────────────────────────
with st.expander("📊 Score breakdown", expanded=False):
    for key, label, colour in _LAYER_BARS:
        v = sc.get(key, {})
        _score_bar(label, v.get("score", 0), v.get("max", 1), colour)

st.markdown("---")

# ── Actionable Fixes Panel ────────────────────────────────────────────────────
_auto_fixes   = [f for f in fixes if f.get("auto_fixable")]
_manual_fixes = [f for f in fixes if not f.get("auto_fixable")]
_fix_count    = len(fixes)
_auto_count   = len(_auto_fixes)

with st.expander(
    f"🔧 Actionable Fixes ({_fix_count} found — {_auto_count} auto-fixable)",
    expanded=_fix_count > 0,
):
    if not fixes:
        st.success("✅ No actionable fixes found — document looks correct.")
    else:
        if _auto_fixes:
            col_ab, col_am = st.columns([1, 3])
            col_ab.metric("🤖 Auto-fixable", _auto_count)
            col_am.metric("✏️ Manual review", len(_manual_fixes))
            if st.button(f"⚡ Apply all {_auto_count} auto-fix(es)", type="primary"):
                corrected, applied = apply_excel_fixes(current_sgml, _auto_fixes)
                st.session_state.corrected = corrected
                current_sgml = corrected
                st.success(f"✅ Applied {applied} fix(es). Download SGML in the editor below.")

        st.markdown("---")

        SEV_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
        fixes_by_sev: dict[str, list] = {}
        for fix in fixes:
            fixes_by_sev.setdefault(fix.get("severity", "LOW"), []).append(fix)

        for sev in SEV_ORDER:
            sev_fixes = fixes_by_sev.get(sev, [])
            if not sev_fixes:
                continue
            icon = _SEV_ICONS.get(sev, "⚪")
            st.markdown(f"#### {icon} {sev} — {len(sev_fixes)} issue(s)")
            for idx, fix in enumerate(sev_fixes):
                line_suffix = f" — line {fix['line']}" if fix.get("line") else ""
                with st.expander(
                    f"{fix['check_id']}  {_sev_badge(sev)}  "
                    f"{fix['description'][:70]}{line_suffix}",
                    expanded=(sev == "CRITICAL"),
                ):
                    st.markdown(
                        f'<div style="background:{_SEV_BG.get(sev, "#fffacd")};'
                        f'padding:6px 10px;border-radius:4px;margin-bottom:8px;font-size:.9rem">'
                        f'<b>{fix["description"]}</b></div>',
                        unsafe_allow_html=True,
                    )
                    ic1, ic2, ic3 = st.columns(3)
                    ic1.caption(f"📍 Line {fix['line']}" if fix.get("line") else "📍 Location unknown")
                    ic2.caption("⚡ Auto-fixable" if fix.get("auto_fixable") else "✏️ Manual")
                    ic3.caption(f"Check: `{fix.get('check_id','?')}`")
                    if fix.get("context_before"):
                        st.markdown("**Current SGML:**")
                        st.code(fix["context_before"], language="xml")
                    if fix.get("suggested_fix"):
                        st.markdown("**Suggested fix:**")
                        st.code(fix["suggested_fix"], language="xml")
                    for d in fix.get("detail", [])[:5]:
                        st.text(f"  → {d}")
                    if fix.get("auto_fixable") and fix.get("suggested_fix"):
                        if st.button("⚡ Apply this fix",
                                     key=f"apply_{sev}_{idx}_{fix.get('line',0)}"):
                            corrected, applied = apply_excel_fixes(current_sgml, [fix])
                            if applied:
                                st.session_state.corrected = corrected
                                current_sgml = corrected
                                st.success(f"Fix applied on line {fix.get('line','?')}.")
                            else:
                                st.warning("Could not apply — text may already be corrected.")

st.markdown("---")

# ── Side-by-side: SGML editor (left) | Issues + Excel stats (right) ──────────
col_sgml, col_issues = st.columns(2)

with col_sgml:
    highlight_map = _build_highlight_map(fixes)
    problem_count = len(highlight_map)
    colour_legend = (
        " &nbsp;🔴 red=critical &nbsp;🟠 orange=high &nbsp;🟡 yellow=medium"
        if highlight_map else ""
    )
    st.markdown(
        f"### ✏️ SGML &nbsp;<small style='font-size:.8rem;color:#666'>"
        f"{problem_count} problem line(s) highlighted{colour_legend}</small>",
        unsafe_allow_html=True,
    )

    # Highlighted read-only view
    st.markdown(
        _render_sgml_highlighted(current_sgml, highlight_map),
        unsafe_allow_html=True,
    )

    # ── postMessage bridge: editor iframe → Streamlit textarea ────────────
    st.markdown("**✏️ Edit SGML**", unsafe_allow_html=True)
    _cmp.html("""<script>
(function(){
  var pw=window.parent;
  if(pw._sgmlBridgeExcel)return;
  pw._sgmlBridgeExcel=true;
  pw.addEventListener('message',function(e){
    if(!e.data||e.data.type!=='excel_sgml_editor_update')return;
    var val=e.data.value;
    var ta=pw.document.querySelector('textarea[aria-label="excel_sgml_editor_backing"]');
    if(!ta){
      var all=pw.document.querySelectorAll('textarea');
      var best=0;
      for(var i=0;i<all.length;i++){
        var v=all[i].value||'';
        if(v.indexOf('<')>=0&&v.length>best){best=v.length;ta=all[i];}
      }
    }
    if(!ta)return;
    var setter=Object.getOwnPropertyDescriptor(
      pw.HTMLTextAreaElement.prototype,'value').set;
    setter.call(ta,val);
    ta.dispatchEvent(new pw.Event('input',{bubbles:true}));
  });
})();
</script>""", height=1, scrolling=False)

    # ── Self-contained editor: gutter + textarea in ONE iframe ────────────
    _n_lines = current_sgml.count('\n') + 1
    _escaped  = html.escape(current_sgml)
    _editor_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
*{{box-sizing:border-box;margin:0;padding:0}}
html,body{{height:100%;overflow:hidden;
  font-family:'Courier New',monospace;font-size:13px}}
.bar{{display:flex;align-items:center;gap:8px;padding:3px 8px;
  background:#f1f5f9;border:1px solid #d1d5db;
  border-radius:4px 4px 0 0;font-size:11px;color:#475569;white-space:nowrap;
  flex-shrink:0}}
.bar b{{color:#1e293b}}
#gtl{{width:55px;padding:1px 4px;border:1px solid #cbd5e1;border-radius:3px;
  font-family:inherit;font-size:11px}}
.btn{{padding:2px 10px;border-radius:3px;border:1px solid #94a3b8;
  background:#fff;cursor:pointer;font-size:11px;color:#334155}}
.btn:hover{{background:#e2e8f0}}
.abtn{{background:#2563eb;color:#fff;border-color:#1d4ed8;font-weight:600}}
.abtn:hover{{background:#1d4ed8}}
#msg{{color:#16a34a;font-style:italic;font-size:10px}}
.wrap{{display:flex;flex:1;border:1px solid #d1d5db;border-top:none;
  border-radius:0 0 4px 4px;overflow:hidden;height:430px}}
#gutter{{background:#f8fafc;color:#94a3b8;text-align:right;
  padding:8px 6px 8px 4px;border-right:2px solid #e2e8f0;
  overflow:hidden;user-select:none;white-space:pre;
  line-height:1.5;font-size:13px;min-width:52px;flex-shrink:0}}
#ed{{flex:1;padding:8px;border:none;outline:none;resize:none;
  font-family:'Courier New',monospace;font-size:13px;line-height:1.5;
  overflow-y:scroll;white-space:pre;tab-size:2;color:#1a1a1a;background:#fff}}
</style></head><body style="display:flex;flex-direction:column;height:100%">
<div class="bar">
  Lines:&nbsp;<b id="lc">{_n_lines}</b>&nbsp;│&nbsp;Go&nbsp;to&nbsp;line:
  <input id="gtl" type="number" min="1" max="{_n_lines}">
  <button class="btn" id="gob">Go</button>
  &nbsp;│&nbsp;
  <button class="abtn btn" id="applyb">✓&nbsp;Apply&nbsp;Changes</button>
  &nbsp;<span id="msg"></span>
</div>
<div class="wrap">
  <div id="gutter"></div>
  <textarea id="ed" spellcheck="false">{_escaped}</textarea>
</div>
<script>
var ed=document.getElementById('ed');
var g=document.getElementById('gutter');
var lc=document.getElementById('lc');
var msg=document.getElementById('msg');

function buildGutter(n){{
  var a=[];
  for(var i=1;i<=n;i++) a.push(('    '+i).slice(-4));
  return a.join('\\n');
}}

function syncGutter(){{
  var n=ed.value.split('\\n').length;
  if(parseInt(lc.textContent)!==n){{
    lc.textContent=n;
    g.textContent=buildGutter(n);
  }}
  g.scrollTop=ed.scrollTop;
}}

g.textContent=buildGutter({_n_lines});

ed.addEventListener('scroll', function(){{ g.scrollTop=ed.scrollTop; }}, {{passive:true}});
ed.addEventListener('input',  syncGutter);

function goToLine(){{
  var n=parseInt(document.getElementById('gtl').value);
  if(!isFinite(n)||n<1) return;
  var lines=ed.value.split('\\n');
  if(n>lines.length) n=lines.length;
  var lh=parseFloat(getComputedStyle(ed).lineHeight)||20;
  var visLines=Math.floor(ed.clientHeight/lh);
  ed.scrollTop=Math.max(0,(n-1-Math.floor(visLines/2))*lh);
  g.scrollTop=ed.scrollTop;
  var pos=0;
  for(var i=0;i<n-1;i++) pos+=lines[i].length+1;
  ed.focus();
  ed.setSelectionRange(pos, pos+(lines[n-1]||'').length);
}}

document.getElementById('gob').addEventListener('click', goToLine);
document.getElementById('gtl').addEventListener('keydown', function(e){{
  if(e.key==='Enter') goToLine();
}});

document.getElementById('applyb').addEventListener('click', function(){{
  window.parent.postMessage({{type:'excel_sgml_editor_update', value:ed.value}}, '*');
  msg.textContent='✓ Applied — now Save or Download';
  setTimeout(function(){{ msg.textContent=''; }}, 5000);
}});
</script></body></html>"""

    _cmp.html(_editor_html, height=468, scrolling=False)

    # Hide the backing textarea — JS bridge target only, not for users
    st.markdown(
        "<style>[data-testid='stTextAreaRootElement']"
        ":has(textarea[aria-label='excel_sgml_editor_backing'])"
        "{display:none!important}</style>",
        unsafe_allow_html=True,
    )

    # ── Backing textarea (hidden — aria-label used by the JS bridge) ──────
    edited = st.text_area(
        label="excel_sgml_editor_backing",
        value=current_sgml,
        height=1,
        key="excel_sgml_editor",
        label_visibility="collapsed",
    )
    if edited != current_sgml:
        st.session_state.corrected = edited
        current_sgml = edited

    dl_col, save_col = st.columns(2)
    with dl_col:
        out_name = (
            Path(sgm_name).stem + (".fixed.sgm" if st.session_state.corrected else ".sgm")
        )
        st.download_button(
            "⬇️ Download SGML",
            data=current_sgml.encode("utf-8"),
            file_name=out_name,
            mime="text/plain",
        )
    with save_col:
        if st.button("💾 Save to disk", key="save_excel_sgml"):
            if sgm_path and sgm_path.exists():
                sgm_path.write_text(current_sgml, encoding="utf-8")
                st.success(f"Saved → {sgm_path}")
            else:
                st.warning("No disk path available — use Download instead.")

with col_issues:
    st.markdown("### ⚠️ All Validator Issues")
    all_issues_flat: list[dict] = []
    for lvl in ["L1", "L2", "L3", "L4"]:
        for iss in report.get("issues", {}).get(lvl, []):
            all_issues_flat.append({**iss, "_level": lvl})

    if not all_issues_flat:
        st.success("No issues found.")
    else:
        by_level: dict[str, list] = {}
        for iss in all_issues_flat:
            by_level.setdefault(iss["_level"], []).append(iss)
        for lvl in ["L1", "L2", "L3", "L4"]:
            lvl_issues = by_level.get(lvl, [])
            if not lvl_issues:
                continue
            st.markdown(f"**{lvl}** — {len(lvl_issues)} issue(s)")
            for iss in sorted(
                lvl_issues,
                key=lambda x: {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2,
                               "LOW": 3, "INFO": 4}.get(x.get("sev", ""), 5),
            ):
                icon = _SEV_ICONS.get(iss.get("sev", ""), "⚪")
                st.markdown(
                    f"{icon} **[{iss.get('sev','')}]** "
                    f"`{iss.get('id','')}` — {iss.get('msg','')}",
                )

    # Excel source stats
    st.markdown("---")
    st.markdown("### 📊 Excel Source Stats")
    xl_stats = report.get("stats", {})
    n_sheets = xl_stats.get("xl_sheets", 0)
    n_cells  = xl_stats.get("cell_count", 0)
    if n_sheets == 0:
        st.info("No Excel file provided — L1 source comparison was skipped.")
    else:
        c1, c2 = st.columns(2)
        c1.metric("Excel sheets", n_sheets)
        c2.metric("TBLCELL(s) in SGML", n_cells)

st.markdown("---")

# ── Decision panel ────────────────────────────────────────────────────────────
st.markdown("### 🧑‍⚖️ Human Decision")
dcol1, dcol2 = st.columns([3, 1])
with dcol1:
    reviewer_note = st.text_area(
        "Reviewer note (required for REJECT / REVIEW overrides)",
        height=80,
        key="excel_reviewer_note",
        placeholder=(
            "e.g. 'TBLCELL count mismatch is acceptable — source has merged cells'"
        ),
    )
with dcol2:
    _options = [
        "(keep validator decision)", "ACCEPT",
        "ACCEPT_WITH_WARNINGS", "REVIEW", "REJECT",
    ]
    human_decision = st.radio("Override decision", options=_options, key="excel_human_decision")

if st.button("✅ Record Decision", type="primary", key="excel_record_decision"):
    final  = decision if human_decision == "(keep validator decision)" else human_decision
    record = {
        "timestamp":            datetime.utcnow().isoformat(),
        "source":               "excel_hitl",
        "sgml_file":            sgm_name,
        "validator_decision":   decision,
        "human_decision":       final,
        "normalised_score":     round(norm, 1),
        "reviewer_note":        reviewer_note,
        "fixes_found":          _fix_count,
        "auto_fixes_available": _auto_count,
        "scores":               sc,
    }
    _save_decision(record)
    colour = _DECISION_COLOURS.get(final, "#666")
    st.markdown(
        f'<div style="background:{colour};color:white;padding:10px;'
        f'border-radius:6px;font-size:1.1rem">'
        f'Decision recorded: <b>{final}</b></div>',
        unsafe_allow_html=True,
    )

with st.expander("🔍 Full validation JSON", expanded=False):
    st.json(report)

st.markdown("---")

# ── Decision History ──────────────────────────────────────────────────────────
st.markdown("## 📋 Decision History")
_records = _load_past_decisions()
if not _records:
    st.info("No Excel HITL decisions recorded yet.")
else:
    st.caption(f"{len(_records)} decision(s) in `{DECISIONS_FILE.name}` (Excel source only)")
    _counts = Counter(r["human_decision"] for r in _records)
    _hcols = st.columns(4)
    for _hcol, _key in zip(_hcols, ["ACCEPT", "ACCEPT_WITH_WARNINGS", "REVIEW", "REJECT"]):
        _hcol.metric(_key, _counts.get(_key, 0))
    st.markdown("---")
    for rec in reversed(_records):
        colour = _DECISION_COLOURS.get(rec["human_decision"], "#666")
        ts     = rec.get("timestamp", "")[:19].replace("T", " ")
        fname  = Path(rec.get("sgml_file", "?")).name
        score  = rec.get("normalised_score", "?")
        note   = rec.get("reviewer_note", "")
        nfixes = rec.get("fixes_found", "")
        st.markdown(
            f'<span style="background:{colour};color:white;padding:2px 8px;'
            f'border-radius:3px">{rec["human_decision"]}</span> &nbsp; '
            f'`{fname}` &nbsp; score={score}'
            + (f" &nbsp; fixes={nfixes}" if nfixes else "")
            + f" &nbsp; {ts}"
            + (f"  \n> _{note}_" if note else ""),
            unsafe_allow_html=True,
        )
