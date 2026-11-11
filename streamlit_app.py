"""
D4U Tracker | SAM - Incident Chatbot (Streamlit version).

A deterministic reporting assistant over a ServiceNow incident export.
- Upload a CSV/Excel export, then ask plain-language questions.
- All counts/filters/charts are computed in Python (exact, not an LLM guess).
- Runs anywhere Streamlit runs (incl. Streamlit Community Cloud).

NOTE on data: do NOT commit real incident exports to a public repo. Users upload
their own file at runtime; nothing is persisted server-side.
"""

import io
import re
import csv
from collections import Counter, defaultdict

import pandas as pd
import streamlit as st

from engine import (
    normalize_dataframe,
    handle_message,
    help_text,
    GROUP_DEFAULT,
)

st.set_page_config(page_title="D4U Tracker | SAM — Chatbot", page_icon="🩺", layout="wide")

# --------------------------------------------------------------------------- #
# Session state
# --------------------------------------------------------------------------- #
if "rows" not in st.session_state:
    st.session_state.rows = []          # list[dict] normalized
    st.session_state.columns = []
    st.session_state.source = None
    st.session_state.messages = []      # chat history: (role, payload)


def load_file(uploaded) -> tuple[bool, str]:
    name = uploaded.name.lower()
    data = uploaded.getvalue()
    try:
        if name.endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(data))
        else:
            # Robust CSV read: sniff delimiter, handle BOM/encodings.
            text = None
            for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
                try:
                    text = data.decode(enc)
                    break
                except Exception:
                    continue
            if text is None:
                return False, "Could not decode the file."
            try:
                delim = csv.Sniffer().sniff(text[:4096], delimiters=",;\t").delimiter
            except Exception:
                delim = ","
            df = pd.read_csv(io.StringIO(text), sep=delim, dtype=str).fillna("")
    except Exception as exc:
        return False, f"Could not read file: {exc}"

    rows, columns = normalize_dataframe(df)
    if not rows:
        return False, "No rows found in the file."
    st.session_state.rows = rows
    st.session_state.columns = columns
    st.session_state.source = uploaded.name
    return True, f"Loaded {len(rows)} incidents from {uploaded.name}."


# --------------------------------------------------------------------------- #
# Sidebar: load data
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("D4U Tracker | SAM")
    st.caption(f"Assignment group: {GROUP_DEFAULT}")
    uploaded = st.file_uploader("Upload ServiceNow export (CSV/Excel)",
                                type=["csv", "xlsx", "xls"])
    if uploaded is not None:
        if st.button("Load file", use_container_width=True):
            ok, msg = load_file(uploaded)
            (st.success if ok else st.error)(msg)

    if st.session_state.rows:
        st.success(f"{len(st.session_state.rows)} incidents loaded")
        st.caption(f"Source: {st.session_state.source}")
    else:
        st.info("No data loaded yet. Upload an export to begin.")

    st.divider()
    st.caption("Deterministic reporting tool. All counts computed in code — "
               "no AI model. Data stays in this session only.")


# --------------------------------------------------------------------------- #
# Rendering of result payloads
# --------------------------------------------------------------------------- #
PRI_EMOJI = {"1": "🔴", "2": "🟠", "3": "🟡", "4": "🟢", "5": "🔵"}


def _df_from_items(items):
    return pd.DataFrame([{
        "Number": it["number"],
        "Priority": it["priority"],
        "State": it["state"],
        "Short description": it["short_description"],
        "Study": it.get("study", ""),
        "TA": it.get("therapeutic_area", ""),
        "Assigned to": it["assigned_to"],
    } for it in items])


def render_payload(payload: dict, key: str = "0"):
    t = payload.get("type")

    if t == "text":
        st.markdown(payload["text"])

    elif t == "summary":
        c = st.columns(4)
        c[0].metric("Total", payload["total"])
        c[1].metric("Open", payload["open"])
        c[2].metric("Closed", payload["closed"])
        c[3].metric("Unassigned (open)", payload["unassigned_open"])
        st.caption("By priority")
        pr = pd.DataFrame(payload["by_priority"]).rename(columns={"rank": "Priority", "count": "Count"})
        pr["Priority"] = pr["Priority"].map(lambda r: f"P{r}")
        pr["Count"] = pd.to_numeric(pr["Count"], errors="coerce").fillna(0)
        st.bar_chart(pr.set_index("Priority"))
        st.caption(f"Source: {payload.get('source','')} · {payload.get('loaded_at','')}")

    elif t in ("incidents", "count"):
        if t == "count":
            st.metric(payload.get("label", "Count"), payload["value"])
        total, shown = payload["total"], payload["shown"]
        if total == 0:
            st.info("No matching incidents.")
            return
        cap = f"{total} incident(s)" + (f" — showing first {shown}" if shown < total else "")
        st.caption(cap)
        df = _df_from_items(payload["items"])
        st.dataframe(df, use_container_width=True, hide_index=True, key=f"df_{key}")
        st.download_button("Download these as CSV",
                           df.to_csv(index=False).encode("utf-8"),
                           file_name="d4u_filtered.csv", mime="text/csv",
                           key=f"dl_{key}")

    elif t == "group":
        st.caption(f"{payload['label']} — {payload['distinct']} group(s), {payload['total']} incidents")
        g = pd.DataFrame(payload["groups"]).rename(columns={"key": payload["label"], "count": "Count"})
        st.dataframe(g, use_container_width=True, hide_index=True, key=f"gdf_{key}")
        g["Count"] = pd.to_numeric(g["Count"], errors="coerce").fillna(0)
        chart_df = g.set_index(payload["label"])["Count"]
        st.bar_chart(chart_df)

    elif t == "chart":
        st.caption(f"{payload['title']} ({payload['total']} incidents)")
        # Build a DataFrame: index = month labels, columns = series.
        data = {}
        for d in payload["datasets"]:
            data[d["label"]] = d["data"]
        chart_df = pd.DataFrame(data, index=payload["labels"])
        st.bar_chart(chart_df)   # multi-series monthly chart

    else:
        st.write(payload)


# --------------------------------------------------------------------------- #
# Main area: title + chat
# --------------------------------------------------------------------------- #
st.title("D4U Tracker | SAM — Incident Chatbot")
st.caption("Ask about your exported incidents. Try the quick buttons or type a question. Type **help** for everything.")

# Quick action buttons
state = {"rows": st.session_state.rows, "columns": st.session_state.columns,
         "source": st.session_state.source}

qcols = st.columns(6)
quick = [
    ("Summary", "summary"),
    ("Open", "open incidents"),
    ("Open P2", "open P2"),
    ("Unassigned", "unassigned"),
    ("By study", "group by study"),
    ("Monthly by status", "monthly chart by status"),
]
pending_quick = None
for i, (label, q) in enumerate(quick):
    if qcols[i].button(label, use_container_width=True):
        pending_quick = q

# Replay chat history (unique key per message to avoid duplicate element IDs)
for idx, (role, payload) in enumerate(st.session_state.messages):
    with st.chat_message(role):
        if role == "user":
            st.markdown(payload)
        else:
            render_payload(payload, key=f"hist{idx}")

# Input (typed or from a quick button)
typed = st.chat_input("Ask about your D4U incidents…")
user_msg = typed or pending_quick

if user_msg:
    payload = handle_message(user_msg, state)
    st.session_state.messages.append(("user", user_msg))
    st.session_state.messages.append(("assistant", payload))
    st.rerun()
