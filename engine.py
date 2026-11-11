"""
Deterministic query engine for the D4U SAM chatbot.

Pure Python. No AI model. Every count/filter/group/chart is computed by iterating
over the loaded rows, so results are exact and repeatable.

`handle_message(msg, state)` returns a payload dict the UI renders:
  {"type": "text"|"summary"|"incidents"|"count"|"group"|"chart", ...}
"""

import re

GROUP_DEFAULT = "RD_DATA4YOU_L2_COGNIZANT"

# --------------------------------------------------------------------------- #
# Column normalization (map ServiceNow export headers -> canonical keys)
# --------------------------------------------------------------------------- #
COLUMN_ALIASES = {
    "number": ["number", "task", "incident"],
    "short_description": ["short description", "short_description", "description", "summary"],
    "priority": ["priority", "pri"],
    "state": ["state", "status", "incident state"],
    "assigned_to": ["assigned to", "assigned_to", "assignee", "owner"],
    "assignment_group": ["assignment group", "assignment_group", "group"],
    "opened_at": ["opened", "opened at", "opened_at", "created", "sys_created_on"],
    "updated": ["updated", "updated at", "sys_updated_on", "last updated"],
    "category": ["category"],
    "caller": ["caller", "caller id", "requested for"],
    "study": ["u_study", "study", "study id", "study number"],
    "therapeutic_area": ["u_study.u_therapeutic_area", "therapeutic area", "therapeutic_area", "ta"],
    "idp": ["u_study.u_idp", "idp"],
}


def _canon(header: str) -> str:
    h = (header or "").strip().lower()
    for canonical, variants in COLUMN_ALIASES.items():
        if h in variants:
            return canonical
    return h


def normalize_dataframe(df):
    """Take a pandas DataFrame, return (rows: list[dict], columns: list[str])."""
    rename = {c: _canon(c) for c in df.columns}
    out = []
    cols = set()
    for _, r in df.iterrows():
        row = {}
        for orig, val in r.items():
            key = rename[orig]
            sval = "" if val is None else str(val).strip()
            if sval.lower() in ("nan", "none"):
                sval = ""
            row[key] = sval
            cols.add(key)
        out.append(row)
    return out, sorted(cols)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
OPEN_STATES = {"new", "in progress", "on hold", "open", "active", "work in progress", "assigned"}
CLOSED_STATES = {"resolved", "closed", "cancelled", "canceled", "closed complete", "closed/resolved"}

MONTH_NAMES = {"01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr", "05": "May", "06": "Jun",
               "07": "Jul", "08": "Aug", "09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec"}

GROUPABLE = {
    "study": "Study", "therapeutic_area": "Therapeutic area", "assigned_to": "Assigned to",
    "category": "Category", "priority": "Priority", "state": "State",
}


def _is_open(row) -> bool:
    st = str(row.get("state", "")).strip().lower()
    if not st:
        return True
    return st not in CLOSED_STATES


def _priority_rank(row) -> str:
    p = str(row.get("priority", "")).strip().lower()
    m = re.match(r"\s*(\d)", p)
    return m.group(1) if m else p


def _month_of(row) -> str:
    val = str(row.get("opened_at", "")).strip()
    m = re.search(r"^\s*\d{4}-(\d{2})-\d{2}", val)
    if m:
        return m.group(1)
    m2 = re.search(r"^\s*\d{1,2}[/-](\d{1,2})[/-]\d{2,4}", val)
    if m2:
        return m2.group(1).zfill(2)
    return ""


def _ym_of(row) -> str:
    val = str(row.get("opened_at", "")).strip()
    m = re.search(r"^\s*(\d{4})-(\d{2})-\d{2}", val)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m2 = re.search(r"^\s*(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", val)
    if m2:
        year = m2.group(3)
        if len(year) == 2:
            year = "20" + year
        return f"{year}-{m2.group(2).zfill(2)}"
    return ""


def known_values(rows, field):
    vals = set()
    for r in rows:
        v = (r.get(field) or "").strip()
        if v and v.lower() != "n/a":
            vals.add(v)
    return sorted(vals)


def known_assignees(rows):
    return known_values(rows, "assigned_to")


def _match_person(rows, low):
    best, best_len = None, 0
    for full in known_assignees(rows):
        base = re.sub(r"\s*\(.*?\)\s*$", "", full.lower()).strip()
        for t in [t for t in re.split(r"[\s,]+", base) if len(t) > 2]:
            if re.search(r"\b" + re.escape(t) + r"\b", low) and len(t) > best_len:
                best, best_len = full, len(t)
    return best


def _match_ta(rows, low):
    for ta in known_values(rows, "therapeutic_area"):
        if re.search(r"\b" + re.escape(ta.lower()) + r"\b", low):
            return ta
    return None


def _match_study(rows, msg):
    m = re.search(r"\b(\d{6,}[A-Za-z]{2,}\d{2,})\b", msg)
    if m:
        token = m.group(1).lower()
        for s in known_values(rows, "study"):
            if s.lower() == token:
                return s
        return m.group(1)
    m2 = re.search(r"\bstudy\s+([A-Za-z0-9]{5,})\b", msg, re.IGNORECASE)
    if m2:
        token = m2.group(1).lower()
        for s in known_values(rows, "study"):
            if token in s.lower():
                return s
    return None


# --------------------------------------------------------------------------- #
# Payload builders
# --------------------------------------------------------------------------- #
def _text(t): return {"type": "text", "text": t}


def _rows_payload(rows, limit=100):
    items = []
    for r in rows[:limit]:
        items.append({
            "number": r.get("number", ""),
            "priority": r.get("priority", ""),
            "priority_rank": _priority_rank(r),
            "state": r.get("state", ""),
            "open": _is_open(r),
            "short_description": r.get("short_description", ""),
            "assigned_to": r.get("assigned_to", "") or "Unassigned",
            "study": r.get("study", ""),
            "therapeutic_area": r.get("therapeutic_area", ""),
        })
    return {"type": "incidents", "total": len(rows), "shown": len(items), "items": items}


def _count_payload(label, rows):
    p = _rows_payload(rows)
    p["type"] = "count"
    p["label"] = label
    p["value"] = len(rows)
    return p


def _group_payload(field, rows):
    counts = {}
    for r in rows:
        key = (r.get(field) or "").strip() or "(none)"
        counts[key] = counts.get(key, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    return {"type": "group", "field": field, "label": GROUPABLE.get(field, field.title()),
            "total": len(rows), "distinct": len(counts),
            "groups": [{"key": k, "count": v} for k, v in ordered]}


def _monthly_chart_payload(field, rows, top_n=8):
    months = sorted({_ym_of(r) for r in rows if _ym_of(r)})
    series_totals, cell = {}, {}
    for r in rows:
        ym = _ym_of(r)
        if not ym:
            continue
        key = (r.get(field) or "").strip() or "(none)"
        series_totals[key] = series_totals.get(key, 0) + 1
        cell[(ym, key)] = cell.get((ym, key), 0) + 1
    ranked = sorted(series_totals, key=lambda k: -series_totals[k])
    keep = ranked[:top_n]
    has_other = len(ranked) > top_n
    keys = keep + (["Other"] if has_other else [])
    labels = [f"{MONTH_NAMES.get(ym.split('-')[1], ym)} {ym.split('-')[0]}" for ym in months]
    datasets = []
    for key in keys:
        data = []
        for ym in months:
            if key == "Other" and has_other:
                data.append(sum(cell.get((ym, k), 0) for k in ranked[top_n:]))
            else:
                data.append(cell.get((ym, key), 0))
        datasets.append({"label": key, "data": data})
    return {"type": "chart", "title": f"Incidents per month by {GROUPABLE.get(field, field)}",
            "labels": labels, "datasets": datasets,
            "total": len([r for r in rows if _ym_of(r)])}


def summary(rows, source):
    if not rows:
        return _text("No data loaded yet.")
    total = len(rows)
    open_n = sum(1 for r in rows if _is_open(r))
    by_pri = {}
    for r in rows:
        by_pri[_priority_rank(r) or "?"] = by_pri.get(_priority_rank(r) or "?", 0) + 1
    unassigned = sum(1 for r in rows if _is_open(r) and not (r.get("assigned_to") or "").strip())
    return {"type": "summary", "source": source, "total": total, "open": open_n,
            "closed": total - open_n, "unassigned_open": unassigned,
            "by_priority": [{"rank": k, "count": v} for k, v in sorted(by_pri.items())]}


# --------------------------------------------------------------------------- #
# Greetings / help
# --------------------------------------------------------------------------- #
GREETINGS = {"hi", "hii", "hello", "hey", "heya", "yo", "hola", "good morning",
             "good afternoon", "good evening", "morning", "namaste", "greetings", "howdy"}
THANKS = {"thanks", "thank you", "thx", "ty", "great", "awesome", "cool", "nice"}
BYES = {"bye", "goodbye", "see you", "cya", "later"}


def help_text(rows=None):
    loaded = bool(rows)

    def first(field, fb):
        if loaded:
            v = known_values(rows, field)
            if v:
                return v[0]
        return fb

    name = "a name"
    if loaded and known_assignees(rows):
        name = re.sub(r"\s*\(.*?\)\s*$", "", known_assignees(rows)[0]).split()[0]
    study = first("study", "77242113UCO3001")
    ta = first("therapeutic_area", "Oncology")

    return (
        f"**D4U ({GROUP_DEFAULT}) assistant.** Ask things like:\n\n"
        "**Status** — open incidents · resolved · closed · on hold · in progress · new · canceled\n\n"
        "**Priority** — open P2 · critical/high/moderate/low/planning\n\n"
        f"**People** — assigned to {name} · unassigned · how many worked by {name}\n\n"
        f"**Therapeutic area** — incidents in {ta} · how many cases in {ta}\n\n"
        f"**Study** — incidents for study {study}\n\n"
        "**Dates** — cases in november · opened in october\n\n"
        f"**Combine** — open P2 in {ta} assigned to {name} · unassigned in {ta}\n\n"
        "**Breakdowns** — group by study · breakdown by assignee · by category · by state\n\n"
        "**Charts** — monthly chart by status / assignee / study / priority\n\n"
        "**Lookup/search** — status of SINC0492904 · any text to search\n\n"
        "**Listing** — list all (no cap)\n\n"
        "_Counts are computed in code and reflect the loaded export (a snapshot)._"
    )


# --------------------------------------------------------------------------- #
# Main router
# --------------------------------------------------------------------------- #
def handle_message(msg: str, state: dict) -> dict:
    rows = state.get("rows") or []
    columns = state.get("columns") or []
    source = state.get("source")
    low = msg.lower().strip()
    clean = re.sub(r"[!.?,]+$", "", low).strip()

    if clean in GREETINGS:
        extra = (f"I have {len(rows)} incidents loaded. " if rows
                 else "Upload a ServiceNow export to begin. ")
        return _text("Hi! " + extra + "Type **help** to see what I can do.")
    if clean in THANKS:
        return _text("You're welcome! Anything else about your D4U incidents?")
    if clean in BYES:
        return _text("Goodbye! Come back anytime.")
    if not msg or low in ("help", "?", "commands", "menu", "options",
                          "what can you do", "capabilities", "guide"):
        return _text(help_text(rows))

    if not rows:
        return _text("No export loaded yet. Upload a CSV/Excel export in the sidebar.")

    if low in ("count", "summary", "stats", "overview", "total"):
        return summary(rows, source)

    # Charts
    if re.search(r"\b(chart|graph|plot|trend|trends|monthly|per month|over time)\b", low):
        cfield = ("state" if re.search(r"\b(status|state)\b", low) else
                  "assigned_to" if re.search(r"\b(assignee|assigned|person|people|owner)\b", low) else
                  "study" if re.search(r"\b(study|studies)\b", low) else
                  "priority" if re.search(r"\b(priority|severity)\b", low) else
                  "therapeutic_area" if re.search(r"\b(therapeutic|\bta\b)\b", low) else
                  "category" if re.search(r"\b(category|type)\b", low) else "state")
        if cfield not in columns:
            return _text(f"No '{cfield}' column to chart.")
        return _monthly_chart_payload(cfield, rows)

    # Group by
    if re.search(r"\b(group by|groupby|breakdown|break down|per|by study|by assignee|"
                 r"by category|by priority|by state|by ta|by therapeutic)\b", low) or low.startswith("by "):
        field = ("study" if re.search(r"\b(study|studies)\b", low) else
                 "therapeutic_area" if re.search(r"\b(therapeutic|\bta\b)\b", low) else
                 "assigned_to" if re.search(r"\b(assignee|assigned|person|people|owner)\b", low) else
                 "category" if re.search(r"\b(category|type|kind)\b", low) else
                 "priority" if re.search(r"\b(priority|severity)\b", low) else
                 "state" if re.search(r"\b(state|status)\b", low) else None)
        if field:
            if field not in columns:
                return _text(f"The export has no '{field}' column.")
            return _group_payload(field, rows)

    # Specific incident number
    m = re.search(r"\b((?:S?INC)\d{3,})\b", msg, re.IGNORECASE)
    if m:
        num = m.group(1).upper()
        match = [r for r in rows if str(r.get("number", "")).upper() == num]
        if not match:
            match = [r for r in rows if num in str(r.get("number", "")).upper()]
        return _rows_payload(match)

    # Filters
    wants_count = bool(re.search(r"\b(how many|no of|number of|count of|count|total)\b", low))
    wants_all = bool(re.search(r"\b(list all|show all|everything|full list|list|show me all)\b", low)) \
                or "all" in low.split()
    wants_open = bool(re.search(r"\b(open|active|pending)\b", low))
    wants_closed = bool(re.search(r"\b(resolved|closed|done)\b", low))
    wants_unassigned = "unassigned" in low or "no owner" in low

    PRIORITY_WORDS = {"1": ["p1", "priority 1", "critical"], "2": ["p2", "priority 2", "high"],
                      "3": ["p3", "priority 3", "moderate", "medium"], "4": ["p4", "priority 4", "low"],
                      "5": ["p5", "priority 5", "planning"]}
    wanted_priority = None
    for rank, words in PRIORITY_WORDS.items():
        if any(re.search(r"\b" + re.escape(w) + r"\b", low) for w in words):
            wanted_priority = rank
            break

    STATE_WORDS = {"On Hold": ["on hold", "onhold", "hold"],
                   "In Progress": ["in progress", "inprogress", "progress", "wip"],
                   "New": ["new"], "Resolved": ["resolved"], "Closed": ["closed"],
                   "Canceled": ["canceled", "cancelled"]}
    wanted_state = None
    for sname, words in STATE_WORDS.items():
        if any(re.search(r"\b" + re.escape(w) + r"\b", low) for w in words):
            wanted_state = sname
            break

    MONTHS = {"january": "01", "jan": "01", "february": "02", "feb": "02", "march": "03",
              "mar": "03", "april": "04", "apr": "04", "may": "05", "june": "06", "jun": "06",
              "july": "07", "jul": "07", "august": "08", "aug": "08", "september": "09",
              "sep": "09", "sept": "09", "october": "10", "oct": "10", "november": "11",
              "nov": "11", "december": "12", "dec": "12"}
    wanted_month = None
    for nm, num in MONTHS.items():
        if re.search(r"\b" + nm + r"\b", low):
            wanted_month = num
            break

    person = _match_person(rows, low)
    person_label = person
    if not person and "assigned to" in low:
        tail = low.split("assigned to", 1)[1].strip()
        if tail and tail != "me":
            person = person_label = tail

    ta = _match_ta(rows, low) if "therapeutic_area" in columns else None
    study = _match_study(rows, msg) if "study" in columns else None

    filtered, applied = rows, []
    if person:
        pl = person.lower()
        filtered = [r for r in filtered if pl in str(r.get("assigned_to", "")).lower()]
        applied.append(f"assigned to '{person_label}'")
    if wanted_state:
        filtered = [r for r in filtered if str(r.get("state", "")).strip().lower() == wanted_state.lower()]
        applied.append(wanted_state)
    if wanted_month:
        filtered = [r for r in filtered if _month_of(r) == wanted_month]
        applied.append(f"month {wanted_month}")
    if ta:
        filtered = [r for r in filtered if ta.lower() == str(r.get("therapeutic_area", "")).lower()]
        applied.append(f"TA '{ta}'")
    if study:
        filtered = [r for r in filtered if study.lower() == str(r.get("study", "")).lower()]
        applied.append(f"study '{study}'")
    if wants_unassigned:
        filtered = [r for r in filtered if not (r.get("assigned_to") or "").strip()]
        applied.append("unassigned")
    if wanted_priority:
        present = {_priority_rank(r) for r in rows}
        if wanted_priority not in present:
            have = ", ".join("P" + p for p in sorted(x for x in present if x))
            return _text(f"There are no P{wanted_priority} incidents in this export. "
                         f"Priorities present: {have}.")
        filtered = [r for r in filtered if _priority_rank(r) == wanted_priority]
        applied.append("P" + wanted_priority)
    if not wanted_state:
        if wants_open and not wants_closed:
            filtered = [r for r in filtered if _is_open(r)]
            applied.append("open")
        elif wants_closed and not wants_open:
            filtered = [r for r in filtered if not _is_open(r)]
            applied.append("resolved/closed")

    any_filter = bool(person or ta or study or wanted_state or wanted_month
                      or wants_unassigned or wanted_priority or wants_open or wants_closed)

    if any_filter:
        label = "Incidents " + (", ".join(applied) if applied else "")
        if wants_count:
            return _count_payload(label.strip(), filtered)
        return _rows_payload(filtered, limit=(len(filtered) if wants_all else 100))

    if wants_all or low.startswith("list") or "recent" in low or "show me" in low:
        if wants_count:
            return _count_payload("Total incidents", rows)
        return _rows_payload(rows, limit=(len(rows) if wants_all else 100))

    # Free-text search
    res = [r for r in rows if low in " ".join(
        str(r.get(f, "")) for f in ("short_description", "number", "assigned_to", "category")).lower()]
    if res:
        if wants_count:
            return _count_payload(f"Incidents matching '{msg}'", res)
        return _rows_payload(res, limit=(len(res) if wants_all else 100))

    sample = ", ".join(known_assignees(rows)[:8])
    return _text("I couldn't match that to a filter. Try `open P2`, `unassigned`, "
                 "`group by study`, `summary`, or `help`."
                 + (f"\n\nPeople in this export: {sample}…" if sample else ""))
