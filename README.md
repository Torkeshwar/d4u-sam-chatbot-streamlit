# D4U Tracker | SAM — Incident Chatbot (Streamlit)

Streamlit version of the D4U incident assistant, deployable free on
**Streamlit Community Cloud**. Same deterministic logic as the FastAPI version
(all counts computed in Python — no AI model), with a Streamlit UI.

> The original FastAPI app lives in a separate folder/repo and is unchanged.

## Run locally

```powershell
cd d4u-sam-chatbot-streamlit
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Opens at http://localhost:8501.

## Use

1. Upload a ServiceNow incident export (CSV/Excel) in the sidebar → **Load file**.
   (A `sample_demo.csv` with fake data is included to try it out.)
2. Ask questions or use the quick buttons:
   - `summary`, `open incidents`, `open P2`, `unassigned`
   - `assigned to <name>`, `incidents in Oncology`, `cases in november`
   - `group by study`, `breakdown by assignee`
   - `monthly chart by status`
   - `status of SINC...`, or any text to search
   - `help` for the full guide

## Deploy to Streamlit Community Cloud (free)

1. Push this folder to a GitHub repo.
2. On https://share.streamlit.io → **New app** → pick the repo/branch.
3. Set **Main file path** to `streamlit_app.py`.
4. Deploy.

## IMPORTANT — data handling

- **Do NOT commit real incident exports** to a public repo. The `.gitignore`
  blocks `incident*.csv`, `*_export.csv`, and `*.xlsx` by default.
- Only the synthetic `sample_demo.csv` (fake data) is included for demos.
- Users upload their own file at runtime; it lives only in the session.

## Files

| File | Purpose |
|---|---|
| `streamlit_app.py` | Streamlit UI (upload, chat, rendering) |
| `engine.py` | Deterministic query engine (shared logic, no AI) |
| `requirements.txt` | Dependencies |
| `sample_demo.csv` | Synthetic sample for demos |
