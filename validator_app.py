"""
validator_app.py — TR SGML Validator v1.1  |  Navigation Entry Point
=====================================================================
Entry point for SGMLValidator.exe.

Two review modes accessible via the sidebar:
  • PDF HITL Review   — upload SGML + PDF   (default)
  • Excel HITL Review — upload SGML + Excel

No LLMs, no API keys, no ABBYY — purely deterministic validation.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.resolve()

# Ensure root is first on sys.path so 'import config' and 'import validator' work
for _p in [str(_ROOT)]:
    if _p in sys.path:
        sys.path.remove(_p)
sys.path.insert(0, str(_ROOT))

# Evict any stale cached modules from previous hot-reload
for _key in list(sys.modules.keys()):
    if _key == "config" or _key.startswith("config."):
        del sys.modules[_key]

import streamlit as st

# ── Page config (owned here — sub-scripts skip via _SKIP_PAGE_CONFIG) ─────────
st.set_page_config(
    page_title="TR SGML Validator — HITL Review",
    page_icon="⚖️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Navigation sidebar ─────────────────────────────────────────────────────────
_PAGES = ["PDF HITL Review", "Excel HITL Review"]

if "nav_page" not in st.session_state:
    st.session_state["nav_page"] = "PDF HITL Review"

with st.sidebar:
    st.markdown(
        "<div style='font-size:0.72rem;font-weight:700;color:#6b7280;"
        "text-transform:uppercase;letter-spacing:0.07em;margin-bottom:4px'>"
        "Review Mode</div>",
        unsafe_allow_html=True,
    )
    for _pg in _PAGES:
        if st.session_state["nav_page"] == _pg:
            # Active page — shown as bold highlighted label (not a button)
            st.markdown(
                f'<div style="font-weight:700;font-size:0.97rem;padding:7px 12px;'
                f'background:#eff6ff;border-left:3px solid #2563eb;'
                f'border-radius:0 6px 6px 0;color:#1d4ed8;margin-bottom:3px">'
                f'{_pg}</div>',
                unsafe_allow_html=True,
            )
        else:
            # Inactive page — clickable button
            if st.button(_pg, key=f"_nav_{_pg}", width="stretch"):
                st.session_state["nav_page"] = _pg
                st.rerun()
    st.markdown("<hr style='margin:8px 0 4px 0'>", unsafe_allow_html=True)

# ── Route to selected review page ─────────────────────────────────────────────
_selected    = st.session_state["nav_page"]
_script_path = _ROOT / ("hitl_review.py" if _selected == "PDF HITL Review" else "excel_hitl.py")
_exec_ns     = {
    "__name__": "__main__",
    "__file__": str(_script_path),
    "_SKIP_PAGE_CONFIG": True,   # validator_app.py already called set_page_config
}

exec(
    compile(_script_path.read_text(encoding="utf-8"), str(_script_path), "exec"),
    _exec_ns,
)
