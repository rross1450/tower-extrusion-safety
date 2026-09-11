"""Tower Extrusion Safety Procedures — Flask + Jinja2 + SQLite."""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

ROOT = Path(__file__).resolve().parent
CATALOG_PATH = ROOT / "src" / "lib" / "catalog.json"
DB_PATH = ROOT / "data" / "safety.db"

PPE_LABEL = {
    "glasses": "Safety glasses",
    "face-shield": "Face shield",
    "hearing": "Hearing protection",
    "cut-gloves": "Cut-resistant gloves",
    "chem-gloves": "Chemical gloves",
    "boots": "Steel-toe boots",
    "hard-hat": "Hard hat",
    "respirator": "Respirator",
    "weld-helmet": "Welding helmet",
    "fr-clothing": "FR clothing",
    "high-vis": "High-visibility vest",
}

ENERGY_LABEL = {
    "electrical": "Electrical",
    "hydraulic": "Hydraulic",
    "pneumatic": "Pneumatic",
    "thermal": "Thermal",
    "gravity": "Gravity",
    "chemical": "Chemical",
    "steam": "Steam",
    "kinetic": "Kinetic",
    "gas": "Fuel gas",
}

AREA_ORDER = ["Shipping 1", "Press 5"]

DISCLAIMER = (
    "This card supports the site energy-control program (OSHA 29 CFR 1910.147). "
    "It does not replace training, a written LOTO procedure, or a qualified person. "
    "If the machine does not match this card, stop and call your supervisor."
)

app = Flask(
    __name__,
    static_folder="static",
    template_folder="templates",
)
app.secret_key = "tower-extrusion-safety-local"


def normalize_barcode(code: str | None) -> str:
    return (code or "").strip().upper()

def area_barcode_prefix(area: str) -> str:
    """Map area name to a short Code 39-safe prefix."""
    a = (area or "").strip().upper()
    mapping = {
        "SHIPPING 1": "SHP",
        "PRESS 5": "PRS",
    }
    if a in mapping:
        return mapping[a]
    letters = re.sub(r"[^A-Z0-9]", "", a)
    if len(letters) >= 3:
        return letters[:3]
    return (letters + "XXX")[:3]


def next_barcode(area: str) -> str:
    """Generate the next unused M-XXX-NNNN barcode for an area."""
    prefix = area_barcode_prefix(area)
    db = get_db()
    rows = db.execute("SELECT barcode FROM machines").fetchall()
    used = {normalize_barcode(r["barcode"]) for r in rows}
    pattern = re.compile(rf"^M-{re.escape(prefix)}-(\d+)$")
    max_n = 0
    for code in used:
        m = pattern.match(code)
        if m:
            max_n = max(max_n, int(m.group(1)))
    n = max_n + 1
    while True:
        candidate = f"M-{prefix}-{n:04d}"
        if candidate not in used:
            return candidate
        n += 1



def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS machines (
            id TEXT PRIMARY KEY,
            barcode TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            area TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS active_lockouts (
            machine_id TEXT PRIMARY KEY,
            person_name TEXT NOT NULL,
            department TEXT NOT NULL,
            locked_at TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS event_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            action TEXT NOT NULL,
            machine_id TEXT NOT NULL,
            machine_name TEXT NOT NULL,
            person_name TEXT NOT NULL,
            department TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT ''
        );
        """
    )
    db.commit()
    ensure_reason_columns(db)
    count = db.execute("SELECT COUNT(*) AS c FROM machines").fetchone()["c"]
    if count == 0:
        seed_machines(db)


def ensure_reason_columns(db: sqlite3.Connection):
    """Add reason columns on older DBs created before this field existed."""
    for table in ("active_lockouts", "event_log"):
        cols = {r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}
        if "reason" not in cols:
            db.execute(f"ALTER TABLE {table} ADD COLUMN reason TEXT NOT NULL DEFAULT ''")
    db.commit()


def seed_machines(db: sqlite3.Connection | None = None):
    db = db or get_db()
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    for m in catalog:
        db.execute(
            """
            INSERT OR REPLACE INTO machines (id, barcode, name, area, payload)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                m["id"],
                normalize_barcode(m["barcode"]),
                m["name"],
                m["area"],
                json.dumps(m, ensure_ascii=False),
            ),
        )
    db.commit()
    return len(catalog)


def row_to_machine(row: sqlite3.Row) -> dict:
    data = json.loads(row["payload"])
    data["id"] = row["id"]
    data["barcode"] = row["barcode"]
    data["name"] = row["name"]
    data["area"] = row["area"]
    return data


def list_machines(q: str | None = None) -> list[dict]:
    db = get_db()
    rows = db.execute(
        "SELECT id, barcode, name, area, payload FROM machines ORDER BY area, name"
    ).fetchall()
    machines = [row_to_machine(r) for r in rows]
    if q:
        needle = q.strip().lower()
        machines = [
            m
            for m in machines
            if needle in m["name"].lower()
            or needle in m.get("bay", "").lower()
            or needle in m["barcode"].lower()
            or needle in m.get("type", "").lower()
            or needle in m["area"].lower()
            or needle in m["id"].lower()
        ]
    return machines


def get_machine_by_barcode(code: str) -> dict | None:
    db = get_db()
    row = db.execute(
        "SELECT id, barcode, name, area, payload FROM machines WHERE barcode = ?",
        (normalize_barcode(code),),
    ).fetchone()
    return row_to_machine(row) if row else None


def get_machine_by_id(machine_id: str) -> dict | None:
    db = get_db()
    row = db.execute(
        "SELECT id, barcode, name, area, payload FROM machines WHERE id = ?",
        (machine_id,),
    ).fetchone()
    return row_to_machine(row) if row else None


def group_by_area(machines: list[dict]) -> list[dict]:
    buckets: dict[str, list] = {}
    for m in machines:
        buckets.setdefault(m["area"], []).append(m)
    ordered = []
    seen = set()
    for area in AREA_ORDER:
        if area in buckets:
            ordered.append({"area": area, "machines": buckets[area]})
            seen.add(area)
    for area, ms in buckets.items():
        if area not in seen:
            ordered.append({"area": area, "machines": ms})
    return ordered


def known_areas() -> list[str]:
    db = get_db()
    rows = db.execute("SELECT DISTINCT area FROM machines ORDER BY area").fetchall()
    found = [r["area"] for r in rows]
    ordered: list[str] = []
    for a in AREA_ORDER:
        ordered.append(a)
    for a in found:
        if a not in ordered:
            ordered.append(a)
    return ordered


def active_lockouts() -> list[dict]:
    db = get_db()
    rows = db.execute(
        """
        SELECT a.machine_id, a.person_name, a.department, a.locked_at, a.reason,
               m.name, m.barcode, m.area, m.payload
        FROM active_lockouts a
        JOIN machines m ON m.id = a.machine_id
        ORDER BY a.locked_at DESC
        """
    ).fetchall()
    out = []
    for r in rows:
        payload = json.loads(r["payload"])
        out.append(
            {
                "machine_id": r["machine_id"],
                "person_name": r["person_name"],
                "department": r["department"],
                "locked_at": r["locked_at"],
                "reason": r["reason"] or "",
                "name": r["name"],
                "barcode": r["barcode"],
                "area": r["area"],
                "bay": payload.get("bay", ""),
            }
        )
    return out


def get_lockout(machine_id: str) -> dict | None:
    db = get_db()
    row = db.execute(
        "SELECT machine_id, person_name, department, locked_at, reason FROM active_lockouts WHERE machine_id = ?",
        (machine_id,),
    ).fetchone()
    return dict(row) if row else None


def is_builder() -> bool:
    return request.args.get("builder") == "1" or request.form.get("builder") == "1"


def builder_qs() -> str:
    return "?builder=1" if is_builder() else ""


@app.context_processor
def inject_globals():
    return {
        "ppe_label": PPE_LABEL,
        "energy_label": ENERGY_LABEL,
        "disclaimer": DISCLAIMER,
        "builder": is_builder(),
        "builder_qs": builder_qs(),
    }


@app.before_request
def ensure_db():
    init_db()


@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    machines = list_machines(q or None)
    grouped = group_by_area(machines)
    locks = active_lockouts()
    return render_template(
        "index.html",
        machines=machines,
        grouped=grouped,
        q=q,
        active_count=len(locks),
        title="Tower Extrusion Safety Procedures",
    )


@app.route("/scan", methods=["GET", "POST"])
def scan():
    error = None
    if request.method == "POST":
        code = normalize_barcode(request.form.get("barcode"))
        if not code:
            error = "Enter a barcode tag."
        else:
            machine = get_machine_by_barcode(code)
            if machine:
                return redirect(url_for("machine_page", barcode=machine["barcode"]) + builder_qs())
            error = f"No machine matches tag {code}."
    return render_template("scan.html", error=error, title="Scan tag")


@app.route("/m/<barcode>", methods=["GET", "POST"])
def machine_page(barcode):
    machine = get_machine_by_barcode(barcode)
    if not machine:
        abort(404)
    lock = get_lockout(machine["id"])
    message = None

    if request.method == "POST":
        action = request.form.get("action")
        person = (request.form.get("person_name") or "").strip()
        department = (request.form.get("department") or "").strip()
        reason = (request.form.get("reason") or "").strip()
        if action in ("lockout", "restart"):
            if not person or not department:
                flash("Name and department are required.", "error")
                return redirect(url_for("machine_page", barcode=machine["barcode"]) + builder_qs())
            if action == "lockout" and not reason:
                flash("Specific reason for lockout is required.", "error")
                return redirect(url_for("machine_page", barcode=machine["barcode"]) + builder_qs())
            if action == "lockout":
                required_steps = [s.get("id") for s in machine.get("lockout", []) if s.get("id")]
                checked = set(request.form.getlist("lockout_step"))
                if required_steps and not set(required_steps).issubset(checked):
                    flash("Check off every lockout step before submitting.", "error")
                    return redirect(url_for("machine_page", barcode=machine["barcode"]) + builder_qs())
            db = get_db()
            now = utc_now()
            if action == "lockout":
                db.execute(
                    """
                    INSERT INTO event_log (created_at, action, machine_id, machine_name, person_name, department, reason)
                    VALUES (?, 'lockout', ?, ?, ?, ?, ?)
                    """,
                    (now, machine["id"], machine["name"], person, department, reason),
                )
                db.execute(
                    """
                    INSERT INTO active_lockouts (machine_id, person_name, department, locked_at, reason)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(machine_id) DO UPDATE SET
                        person_name = excluded.person_name,
                        department = excluded.department,
                        locked_at = excluded.locked_at,
                        reason = excluded.reason
                    """,
                    (machine["id"], person, department, now, reason),
                )
                db.commit()
                flash(f"Lockout logged for {machine['name']}.", "ok")
            else:
                db.execute(
                    """
                    INSERT INTO event_log (created_at, action, machine_id, machine_name, person_name, department, reason)
                    VALUES (?, 'restart', ?, ?, ?, ?, ?)
                    """,
                    (now, machine["id"], machine["name"], person, department, ""),
                )
                db.execute(
                    "DELETE FROM active_lockouts WHERE machine_id = ?",
                    (machine["id"],),
                )
                db.commit()
                flash(f"Restart logged — active lockout cleared for {machine['name']}.", "ok")
            return redirect(url_for("machine_page", barcode=machine["barcode"]) + builder_qs())

    return render_template(
        "machine.html",
        machine=machine,
        lock=lock,
        title=machine["name"],
        message=message,
    )


@app.route("/active")
def active():
    locks = active_lockouts()
    return render_template(
        "active.html",
        locks=locks,
        title="Active lockouts",
    )


@app.route("/admin/log")
def admin_log():
    db = get_db()
    rows = db.execute(
        """
        SELECT e.id, e.created_at, e.action, e.machine_id, e.machine_name,
               e.person_name, e.department, e.reason, m.barcode
        FROM event_log e
        LEFT JOIN machines m ON m.id = e.machine_id
        ORDER BY e.id DESC
        LIMIT 500
        """
    ).fetchall()
    return render_template(
        "admin_log.html",
        events=rows,
        title="Admin log",
    )


@app.route("/builder/new", methods=["GET", "POST"])
def builder_new():
    if request.method == "POST":
        return _save_machine_from_form(is_new=True)
    blank = {
        "id": "",
        "barcode": "",
        "name": "",
        "model": "",
        "type": "",
        "area": "Press 5",
        "bay": "",
        "photo": "",
        "lockoutPhoto": "",
        "manufacturer": "Tower Extrusions",
        "serial": "",
        "ppe": ["glasses", "boots"],
        "energy": [],
        "hazards": [],
        "lockout": [],
        "inspect": [],
        "restart": [],
        "emergency": {
            "estop": "",
            "firstAid": "",
            "fire": "",
            "supervisor": "",
            "radio": "",
        },
        "notes": [],
        "permits": [],
    }
    return render_template(
        "builder_edit.html",
        machine=blank,
        is_new=True,
        title="Add machine",
        ppe_keys=list(PPE_LABEL.keys()),
        known_areas=known_areas(),
    )


@app.route("/builder/edit/<machine_id>", methods=["GET", "POST"])
def builder_edit(machine_id):
    machine = get_machine_by_id(machine_id)
    if not machine:
        abort(404)
    if request.method == "POST":
        return _save_machine_from_form(is_new=False, existing_id=machine_id)
    return render_template(
        "builder_edit.html",
        machine=machine,
        is_new=False,
        title=f"Edit {machine['name']}",
        ppe_keys=list(PPE_LABEL.keys()),
        known_areas=known_areas(),
    )


def _parse_json_field(name: str, default):
    raw = request.form.get(name, "").strip()
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        flash(f"Invalid JSON in {name}.", "error")
        return None


def _save_machine_from_form(is_new: bool, existing_id: str | None = None):
    name = (request.form.get("name") or "").strip()
    barcode = normalize_barcode(request.form.get("barcode"))
    area = (request.form.get("area") or "").strip() or "Press 5"
    machine_id = (request.form.get("id") or "").strip() or existing_id or str(uuid.uuid4())[:8]

    if not name:
        flash("Name is required.", "error")
        if is_new:
            return redirect(url_for("builder_new"))
        return redirect(url_for("builder_edit", machine_id=existing_id or machine_id))

    if not barcode:
        if is_new:
            barcode = next_barcode(area)
        else:
            existing = get_machine_by_id(existing_id) if existing_id else None
            barcode = existing["barcode"] if existing else next_barcode(area)

    ppe = request.form.getlist("ppe")
    energy = _parse_json_field("energy_json", [])
    hazards = _parse_json_field("hazards_json", [])
    lockout = _parse_json_field("lockout_json", [])
    inspect = _parse_json_field("inspect_json", [])
    restart = _parse_json_field("restart_json", [])
    notes_raw = request.form.get("notes", "")
    notes = [n.strip() for n in notes_raw.splitlines() if n.strip()]
    permits_raw = request.form.get("permits", "")
    permits = [p.strip() for p in permits_raw.splitlines() if p.strip()]

    if None in (energy, hazards, lockout, inspect, restart):
        if is_new:
            return redirect(url_for("builder_new"))
        return redirect(url_for("builder_edit", machine_id=existing_id or machine_id))

    payload = {
        "id": machine_id,
        "barcode": barcode,
        "name": name,
        "model": (request.form.get("model") or "").strip(),
        "type": (request.form.get("type") or "").strip(),
        "area": area,
        "bay": (request.form.get("bay") or "").strip(),
        "photo": (request.form.get("photo") or "").strip(),
        "lockoutPhoto": (request.form.get("lockoutPhoto") or "").strip(),
        "manufacturer": (request.form.get("manufacturer") or "").strip(),
        "serial": (request.form.get("serial") or "").strip(),
        "ppe": ppe,
        "energy": energy,
        "hazards": hazards,
        "lockout": lockout,
        "inspect": inspect,
        "restart": restart,
        "emergency": {
            "estop": (request.form.get("estop") or "").strip(),
            "firstAid": (request.form.get("firstAid") or "").strip(),
            "fire": (request.form.get("fire") or "").strip(),
            "supervisor": (request.form.get("supervisor") or "").strip(),
            "radio": (request.form.get("radio") or "").strip(),
        },
        "notes": notes,
        "permits": permits,
    }

    db = get_db()
    if is_new:
        exists = db.execute(
            "SELECT 1 FROM machines WHERE id = ? OR barcode = ?",
            (machine_id, barcode),
        ).fetchone()
        if exists:
            flash("A machine with that id or barcode already exists.", "error")
            return redirect(url_for("builder_new"))
        db.execute(
            "INSERT INTO machines (id, barcode, name, area, payload) VALUES (?, ?, ?, ?, ?)",
            (machine_id, barcode, name, area, json.dumps(payload, ensure_ascii=False)),
        )
    else:
        db.execute(
            """
            UPDATE machines
            SET barcode = ?, name = ?, area = ?, payload = ?
            WHERE id = ?
            """,
            (barcode, name, area, json.dumps(payload, ensure_ascii=False), existing_id),
        )
    db.commit()
    flash(f"Saved {name}.", "ok")
    return redirect(url_for("machine_page", barcode=barcode))


if __name__ == "__main__":
    with app.app_context():
        init_db()
        n = get_db().execute("SELECT COUNT(*) AS c FROM machines").fetchone()["c"]
        print(f"DB ready with {n} machines at {DB_PATH}")
    app.run(host="0.0.0.0", port=8080, debug=False)
