# Tower Extrusion Safety Procedures

Flask + Jinja + SQLite site for plant lockout / tagout machine cards.

## Features

- Cobalt UI with Tower Extrusions branding
- Areas: Shipping 1, Press 5 (add more from the **Add** tab)
- Scan barcode (camera or typed tag) and print Code 39 labels
- Machine cards: PPE, energy, hazards, lockout, inspect, restart, emergency, notes
- Lockout checklist with name, department, and **specific reason for lockout**
- Restart clears that machine’s active lockout; admin event log
- Blank barcode on Add auto-generates the next tag for the area (e.g. `M-PRS-0006`)

## Run locally

```bash
python -m venv .venv
# Windows:
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python app.py
# macOS / Linux:
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

Open [http://127.0.0.1:8080/](http://127.0.0.1:8080/).

Binds `0.0.0.0:8080`. Machines seed from `src/lib/catalog.json` into SQLite at `data/safety.db` on first run.

## Stack

Flask, Jinja templates, SQLite. Not a PWA / installable app.
