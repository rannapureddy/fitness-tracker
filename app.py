import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from functools import wraps

from flask import Flask, jsonify, request, send_from_directory

DB_PATH = os.environ.get("DB_PATH", "/data/tracker.db")
CONFIG_DIR = os.environ.get(
    "CONFIG_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")
)
# Plan seed files live under config/plans/, discovered recursively; later
# files (sorted by path) win on a duplicate date. See load_combined_seed().
PLAN_DIR = os.environ.get("PLAN_DIR", os.path.join(CONFIG_DIR, "plans"))
PACE_GUIDE_PATH = os.path.join(CONFIG_DIR, "pace_guide.json")
RACE_DISTANCES_PATH = os.path.join(CONFIG_DIR, "race_distances.json")
MILE_METERS = 1609.344

app = Flask(__name__, static_folder="static", static_url_path="")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def require_json(view):
    """Parses the JSON body and passes it as the first arg; 400s if missing/invalid."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        payload = request.get_json(silent=True)
        if payload is None:
            return jsonify({"error": "invalid or missing JSON body"}), 400
        return view(payload, *args, **kwargs)
    return wrapper


@contextmanager
def get_db():
    """Open a connection for one request/operation. Schema setup lives in init_db(), not here."""
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """
    Create tables and run schema migrations once, at process startup.
    Also enables WAL mode so concurrent gunicorn workers don't hit
    "database is locked" on startup.
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS entries (date TEXT PRIMARY KEY, data TEXT NOT NULL)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS plan (
                date TEXT PRIMARY KEY,
                activity TEXT,
                miles REAL,
                pace TEXT,
                modified INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        _ensure_plan_modified_column(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS weight (
                date TEXT PRIMARY KEY,
                weight REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pace_prs (
                id TEXT PRIMARY KEY,
                time TEXT NOT NULL,
                distance_meters REAL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _ensure_plan_modified_column(conn):
    """Migrate a plan table created before the `modified` column existed."""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(plan)").fetchall()]
    if "modified" not in cols:
        conn.execute("ALTER TABLE plan ADD COLUMN modified INTEGER NOT NULL DEFAULT 0")
        conn.commit()


def _flatten_nested_seed(data, filename):
    """
    Flattens the nested {year: {month: [day entries]}} seed format into a
    flat list of entries with a "date" key, e.g.:
        {"2026": {"08": [{"day": 3, "activity": "Rest"}, ...]}}
    Raises ValueError on a malformed shape so a typo fails loudly at startup.
    """
    flat = []
    for year_str, months in data.items():
        try:
            year = int(year_str)
        except (TypeError, ValueError):
            raise ValueError(f"{filename}: top-level key {year_str!r} is not a valid year")
        if not isinstance(months, dict):
            raise ValueError(f"{filename}: value for year {year_str} must be an object of months")
        for month_str, days in months.items():
            try:
                month = int(month_str)
            except (TypeError, ValueError):
                raise ValueError(f"{filename}: month key {month_str!r} under year {year_str} is not valid")
            if not isinstance(days, list):
                raise ValueError(f"{filename}: {year_str}/{month_str} must be a list of day entries")
            for e in days:
                if not isinstance(e, dict) or "day" not in e or "activity" not in e:
                    raise ValueError(
                        f"{filename}: entry under {year_str}/{month_str} is missing "
                        f"required 'day' or 'activity' field: {e}"
                    )
                try:
                    day = int(e["day"])
                except (TypeError, ValueError):
                    raise ValueError(f"{filename}: 'day' value {e['day']!r} under {year_str}/{month_str} is not valid")
                flat_entry = {k: v for k, v in e.items() if k != "day"}
                flat_entry["date"] = f"{year:04d}-{month:02d}-{day:02d}"
                flat.append(flat_entry)
    return flat


def _find_plan_seed_files():
    """Recursively finds every *.json file under PLAN_DIR, sorted for deterministic processing order."""
    found = []
    for root, dirs, files in os.walk(PLAN_DIR):
        dirs.sort()
        for name in sorted(files):
            if name.lower().endswith(".json"):
                found.append(os.path.join(root, name))
    found.sort()
    return found


def load_combined_seed():
    """
    Loads and merges every *.json seed file found recursively under PLAN_DIR,
    in sorted-path order, into one plan list. Returns None if none are found.
    A date appearing in more than one file: the later-processed file wins
    (logged as a warning, since it likely means a seed-authoring mistake).
    """
    combined = {}
    found_any = False
    for path in _find_plan_seed_files():
        filename = os.path.relpath(path, PLAN_DIR)
        found_any = True
        with open(path) as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError(
                f"{filename} must be a nested {{year: {{month: [days]}}}} object"
            )
        entries = _flatten_nested_seed(raw, filename)
        for e in entries:
            if "date" not in e or "activity" not in e:
                raise ValueError(
                    f"plan seed entry in {filename} is missing required "
                    f"'date' or 'activity' field: {e}"
                )
            # miles/pace are optional in the seed JSON — normalize so
            # downstream code always sees both keys.
            e.setdefault("miles", 0)
            e.setdefault("pace", None)
            if e["date"] in combined:
                logger.warning(
                    "Duplicate plan date %s found in %s; overriding earlier entry",
                    e["date"], filename,
                )
            combined[e["date"]] = e
    if not found_any:
        return None
    return sorted(combined.values(), key=lambda e: e["date"])


def sync_plan_from_seed(conn):
    """
    Brings the `plan` table in line with the seed files: inserts new dates,
    updates unmodified ones, leaves user-customized ones (modified=1) alone,
    and deletes dates removed from the seed (unless customized).
    Returns a summary dict of what changed.
    """
    summary = {"inserted": 0, "updated": 0, "skipped_modified": 0, "deleted": 0}

    seed = load_combined_seed()
    if seed is None:
        return summary
    seed_dates = {e["date"] for e in seed}

    existing = {
        row[0]: {
            "activity": row[1],
            "miles": row[2],
            "pace": row[3],
            "modified": row[4],
        }
        for row in conn.execute(
            "SELECT date, activity, miles, pace, modified FROM plan"
        ).fetchall()
    }

    for e in seed:
        cur = existing.get(e["date"])
        if cur is None:
            conn.execute(
                """
                INSERT INTO plan (date, activity, miles, pace, modified)
                VALUES (:date, :activity, :miles, :pace, 0)
                """,
                e,
            )
            summary["inserted"] += 1
        elif cur["modified"]:
            summary["skipped_modified"] += 1
        else:
            changed = any(
                cur[field] != e.get(field)
                for field in ("activity", "miles", "pace")
            )
            if changed:
                conn.execute(
                    """
                    UPDATE plan SET activity = :activity, miles = :miles, pace = :pace
                    WHERE date = :date
                    """,
                    e,
                )
                summary["updated"] += 1

    for date, cur in existing.items():
        if date not in seed_dates and not cur["modified"]:
            conn.execute("DELETE FROM plan WHERE date = ?", (date,))
            summary["deleted"] += 1

    conn.commit()
    if any(summary.values()):
        logger.info("plan_seed sync: %s", summary)
    return summary


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _validate_entry_payload(payload):
    """
    Light structural validation for a logged entry — catches wrong types
    (string where a number's expected, etc.) before they break the PR/stats
    calculations downstream. Returns an error string, or None if it's sane.
    """
    if not isinstance(payload, dict):
        return "payload must be a JSON object"

    if "kind" in payload and payload["kind"] is not None and not isinstance(payload["kind"], str):
        return "kind must be a string"

    for field in ("miles", "timeSeconds"):
        if field in payload and payload[field] is not None and not _is_number(payload[field]):
            return f"{field} must be a number"

    if "pace" in payload and payload["pace"] is not None and not isinstance(payload["pace"], str):
        return "pace must be a string"

    if "exercises" in payload and payload["exercises"] is not None:
        exercises = payload["exercises"]
        if not isinstance(exercises, dict):
            return "exercises must be an object mapping exercise name to a list of sets"
        for ex_name, sets in exercises.items():
            if not isinstance(sets, list):
                return f"exercises.{ex_name} must be a list of sets"
            for s in sets:
                if not isinstance(s, dict):
                    return f"exercises.{ex_name} sets must be objects with weight/reps"
                for f in ("weight", "reps"):
                    if f in s and s[f] is not None and not _is_number(s[f]):
                        return f"exercises.{ex_name}.{f} must be a number"

    if "reps" in payload and payload["reps"] is not None:
        reps = payload["reps"]
        if not isinstance(reps, list):
            return "reps must be a list"
        for r in reps:
            if not isinstance(r, dict):
                return "each rep must be an object with meters/seconds"
            for f in ("meters", "seconds"):
                if f in r and r[f] is not None and not _is_number(r[f]):
                    return f"reps.{f} must be a number"

    for field in ("warmup", "cooldown"):
        if field in payload and payload[field] is not None:
            block = payload[field]
            if not isinstance(block, dict):
                return f"{field} must be an object"
            if "miles" in block and block["miles"] is not None and not _is_number(block["miles"]):
                return f"{field}.miles must be a number"

    # A run/race/hybrid entry must always carry a real distance and time —
    # PR/pace/stats calculations assume every logged run is timed, and a
    # distance-only entry silently corrupts "fastest pace" style metrics.
    if payload.get("kind") in ("run", "race", "hybrid"):
        miles = payload.get("miles")
        time_seconds = payload.get("timeSeconds")
        if not _is_number(miles) or miles <= 0:
            return "a run entry requires a positive miles value"
        if not _is_number(time_seconds) or time_seconds <= 0:
            return "a run entry requires a positive timeSeconds value"

    return None


@app.route("/api/entries", methods=["GET"])
def list_entries():
    with get_db() as conn:
        rows = conn.execute("SELECT date, data FROM entries").fetchall()
    return jsonify({date: json.loads(data) for date, data in rows})


@app.route("/api/entries/<date>", methods=["POST"])
@require_json
def save_entry(payload, date):
    error = _validate_entry_payload(payload)
    if error:
        return jsonify({"error": error}), 400
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO entries (date, data) VALUES (?, ?)
            ON CONFLICT(date) DO UPDATE SET data = excluded.data
            """,
            (date, json.dumps(payload)),
        )
    return jsonify({"ok": True})


@app.route("/api/entries/<date>", methods=["DELETE"])
def delete_entry(date):
    with get_db() as conn:
        existing = conn.execute("SELECT 1 FROM entries WHERE date = ?", (date,)).fetchone()
        if not existing:
            return jsonify({"error": "no entry for that date"}), 404
        conn.execute("DELETE FROM entries WHERE date = ?", (date,))
    return jsonify({"ok": True})


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


def _serve_config_json(path, filename):
    """Serves a static JSON reference file from config/, 404/500 on missing/bad JSON."""
    if not os.path.exists(path):
        return jsonify({"error": f"{filename} not found"}), 404
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.exception("Failed to read/parse %s", filename)
        return jsonify({"error": f"could not read {filename}"}), 500
    return jsonify(data)


def _parse_clock(clock):
    """Parses a "M:SS" or "H:MM:SS" clock string into total seconds (float)."""
    parts = str(clock).strip().split(":")
    if len(parts) == 2:
        h, (m, s) = 0, parts
    elif len(parts) == 3:
        h, m, s = parts
    else:
        raise ValueError(f"not a valid clock time: {clock!r}")
    return int(h) * 3600 + int(m) * 60 + float(s)


def _format_pace(seconds_per_unit):
    """Formats a per-unit pace in seconds as "M:SS", rounded to the nearest second."""
    total = round(seconds_per_unit)
    m, s = divmod(total, 60)
    return f"{m}:{s:02d}"


def _recompute_paces(guide):
    """
    Recomputes every entry in guide["paces"] whose "source.pr_id" points at
    a PR in guide["prs"], using that PR's time/distance. Entries marked
    "manual": true, or with no "source", are left untouched. Mutates and
    returns `guide`. Raises ValueError on a bad/missing reference so a
    stale or typo'd pr_id fails loudly instead of silently keeping an old
    pace value.
    """
    prs_by_id = {pr["id"]: pr for pr in guide.get("prs", []) if "id" in pr}
    for p in guide.get("paces", []):
        if p.get("manual"):
            continue
        source = p.get("source")
        if not source or "pr_id" not in source:
            continue
        pr = prs_by_id.get(source["pr_id"])
        if pr is None:
            raise ValueError(f"pace {p.get('type')!r} references unknown pr_id {source['pr_id']!r}")

        pr_meters = pr.get("distance_meters")
        if not isinstance(pr_meters, (int, float)) or pr_meters <= 0:
            raise ValueError(f"PR {pr.get('id')!r} has an invalid distance_meters value")
        try:
            pr_seconds = _parse_clock(pr["time"])
        except (KeyError, ValueError):
            raise ValueError(f"PR {pr.get('id')!r} has an invalid time value")

        unit_meters = p.get("unit_meters") if p.get("unit") == "rep" else MILE_METERS
        if not isinstance(unit_meters, (int, float)) or unit_meters <= 0:
            raise ValueError(f"pace {p.get('type')!r} has an invalid unit_meters value")

        p["pace"] = _format_pace(pr_seconds / (pr_meters / unit_meters))
    return guide


def _load_pace_guide_seed():
    with open(PACE_GUIDE_PATH) as f:
        return json.load(f)


def _load_pace_guide_seed_or_error():
    """
    Shared existence/parse check for pace_guide.json, used by all three
    pace-guide routes. Returns (seed_dict, None) on success, or
    (None, (response, status)) so a route can `seed, err = ...; if err:
    return err` instead of repeating the same try/except three times.
    """
    if not os.path.exists(PACE_GUIDE_PATH):
        return None, (jsonify({"error": "pace_guide.json not found"}), 404)
    try:
        return _load_pace_guide_seed(), None
    except (OSError, json.JSONDecodeError):
        logger.exception("Failed to read pace_guide.json")
        return None, (jsonify({"error": "could not read pace_guide.json"}), 500)


def _apply_pr_overrides(guide, conn):
    """
    Overlays user-saved PR overrides from the `pace_prs` table onto the
    seed guide's `prs` list (matching by id). Marks each overridden PR
    with "modified": true so the UI can show it's been customized, same
    as the `plan` table does for workout days. Seed PRs with no override
    row are left exactly as authored in pace_guide.json.
    """
    overrides = {
        row[0]: {"time": row[1], "distance_meters": row[2]}
        for row in conn.execute("SELECT id, time, distance_meters FROM pace_prs").fetchall()
    }
    for pr in guide.get("prs", []):
        o = overrides.get(pr.get("id"))
        if o is None:
            pr["modified"] = False
            continue
        pr["time"] = o["time"]
        if o["distance_meters"] is not None:
            pr["distance_meters"] = o["distance_meters"]
        pr["modified"] = True
    return guide


def _build_pace_guide(conn, seed=None):
    """
    Overlays any DB PR overrides onto `seed` (loading it from disk if not
    already provided) and recomputes derived paces.
    """
    guide = seed if seed is not None else _load_pace_guide_seed()
    _apply_pr_overrides(guide, conn)
    _recompute_paces(guide)
    return guide


@app.route("/api/pace-guide", methods=["GET"])
def get_pace_guide():
    seed, err = _load_pace_guide_seed_or_error()
    if err:
        return err
    try:
        with get_db() as conn:
            guide = _build_pace_guide(conn, seed)
    except ValueError as e:
        logger.exception("Failed to build pace guide")
        return jsonify({"error": str(e)}), 500
    return jsonify(guide)


@app.route("/api/pace-guide/prs", methods=["POST"])
@require_json
def update_pace_guide_prs(payload):
    """
    Saves one or more PR overrides (time, optionally distance_meters) to
    the `pace_prs` table — mirroring how `plan` overrides plan_seed.json —
    then returns the seed guide with overrides applied and paces
    recomputed. The seed file itself is never modified, so redeploying it
    (e.g. to add a new PR or pace type) won't clobber your saved times.
    Body: {"prs": [{"id": "pr_5k", "time": "22:10"}, ...]}
    """
    updates = payload.get("prs")
    if not isinstance(updates, list) or not updates:
        return jsonify({"error": "prs must be a non-empty list"}), 400

    seed, err = _load_pace_guide_seed_or_error()
    if err:
        return err

    known_ids = {pr["id"] for pr in seed.get("prs", []) if "id" in pr}

    cleaned = []
    for u in updates:
        if not isinstance(u, dict) or "id" not in u or "time" not in u:
            return jsonify({"error": "each pr update needs an 'id' and a 'time'"}), 400
        if u["id"] not in known_ids:
            return jsonify({"error": f"unknown PR id {u['id']!r}"}), 400
        try:
            _parse_clock(u["time"])
        except ValueError:
            return jsonify({"error": f"invalid time {u['time']!r} for PR {u['id']!r}"}), 400
        dist = u.get("distance_meters")
        if dist is not None and (not isinstance(dist, (int, float)) or dist <= 0):
            return jsonify({"error": f"invalid distance_meters for PR {u['id']!r}"}), 400
        cleaned.append((u["id"], u["time"], dist))

    with get_db() as conn:
        for pr_id, time_str, dist in cleaned:
            conn.execute(
                """
                INSERT INTO pace_prs (id, time, distance_meters) VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    time = excluded.time,
                    distance_meters = COALESCE(excluded.distance_meters, pace_prs.distance_meters)
                """,
                (pr_id, time_str, dist),
            )
        try:
            guide = _build_pace_guide(conn, seed)
        except ValueError as e:
            # Roll back the just-written overrides rather than leave a PR
            # saved that produces an invalid pace calculation.
            conn.rollback()
            return jsonify({"error": str(e)}), 400

    return jsonify(guide)


@app.route("/api/pace-guide/prs/<pr_id>/reset", methods=["POST"])
def reset_pace_guide_pr(pr_id):
    """Discards a saved PR override and reverts that PR to the pace_guide.json seed value."""
    seed, err = _load_pace_guide_seed_or_error()
    if err:
        return err
    if pr_id not in {pr.get("id") for pr in seed.get("prs", [])}:
        return jsonify({"error": f"unknown PR id {pr_id!r}"}), 404

    with get_db() as conn:
        conn.execute("DELETE FROM pace_prs WHERE id = ?", (pr_id,))
        guide = _build_pace_guide(conn, seed)
    return jsonify(guide)


@app.route("/api/race-distances", methods=["GET"])
def get_race_distances():
    return _serve_config_json(RACE_DISTANCES_PATH, "race_distances.json")


@app.route("/api/plan", methods=["GET"])
def get_plan():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT date, activity, miles, pace, modified FROM plan ORDER BY date"
        ).fetchall()
    plan = [
        {
            "date": r[0],
            "activity": r[1],
            "miles": r[2],
            "pace": r[3],
            "modified": bool(r[4]),
        }
        for r in rows
    ]
    return jsonify(plan)


@app.route("/api/plan/sync", methods=["POST"])
def sync_plan():
    """Re-read plan_seed.json and apply any updates to unmodified days."""
    with get_db() as conn:
        summary = sync_plan_from_seed(conn)
    return jsonify({"ok": True, **summary})


@app.route("/api/plan/<date>", methods=["POST"])
@require_json
def update_plan_day(payload, date):
    activity = payload.get("activity")
    miles = payload.get("miles")
    pace = payload.get("pace")
    if not activity:
        return jsonify({"error": "activity is required"}), 400
    with get_db() as conn:
        existing = conn.execute("SELECT 1 FROM plan WHERE date = ?", (date,)).fetchone()
        if not existing:
            return jsonify({"error": "no plan entry for that date"}), 404
        conn.execute(
            "UPDATE plan SET activity = ?, miles = ?, pace = ?, modified = 1 WHERE date = ?",
            (activity, miles, pace, date),
        )
    return jsonify({"ok": True})


@app.route("/api/plan/<date>/reset", methods=["POST"])
def reset_plan_day(date):
    """Discard a user edit for one day and restore it from plan_seed.json."""
    with get_db() as conn:
        existing = conn.execute("SELECT 1 FROM plan WHERE date = ?", (date,)).fetchone()
        if not existing:
            return jsonify({"error": "no plan entry for that date"}), 404
        conn.execute("UPDATE plan SET modified = 0 WHERE date = ?", (date,))
        sync_plan_from_seed(conn)
    return jsonify({"ok": True})


@app.route("/api/weight", methods=["GET"])
def list_weight():
    with get_db() as conn:
        rows = conn.execute("SELECT date, weight FROM weight ORDER BY date").fetchall()
    return jsonify([{"date": r[0], "weight": r[1]} for r in rows])


@app.route("/api/weight/<date>", methods=["POST"])
@require_json
def save_weight(payload, date):
    weight = payload.get("weight")
    if weight is None:
        return jsonify({"error": "weight is required"}), 400
    try:
        weight = float(weight)
    except (TypeError, ValueError):
        return jsonify({"error": "weight must be a number"}), 400
    if weight <= 0:
        return jsonify({"error": "weight must be positive"}), 400
    # Mirrors the 200 lb cap enforced client-side; kept here too so the
    # limit holds even if a request bypasses the frontend clamp.
    if weight > 200:
        return jsonify({"error": "weight must be 200 lb or less"}), 400
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO weight (date, weight) VALUES (?, ?)
            ON CONFLICT(date) DO UPDATE SET weight = excluded.weight
            """,
            (date, weight),
        )
    return jsonify({"ok": True})


@app.route("/api/weight/<date>", methods=["DELETE"])
def delete_weight(date):
    with get_db() as conn:
        existing = conn.execute("SELECT 1 FROM weight WHERE date = ?", (date,)).fetchone()
        if not existing:
            return jsonify({"error": "no weight entry for that date"}), 404
        conn.execute("DELETE FROM weight WHERE date = ?", (date,))
    return jsonify({"ok": True})


def _run_startup_sync():
    try:
        init_db()
        with get_db() as conn:
            sync_plan_from_seed(conn)
    except Exception:
        logger.exception("Startup init/plan_seed sync failed")


_run_startup_sync()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)