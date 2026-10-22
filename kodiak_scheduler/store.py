"""SQLite storage: every repair request we've collected, plus caches and settings.

Job statuses:
  needs_address  - we know someone called/texted but haven't got an address yet
  new            - has an address, waiting to be put on a schedule
  scheduled      - exported on a crew sheet for scheduled_date
  done           - repair finished
  cancelled      - customer cancelled / duplicate / not a repair
"""

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

OPEN_STATUSES = ("needs_address", "new", "scheduled")
STATUSES = ("needs_address", "new", "scheduled", "done", "cancelled")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT DEFAULT '',
    name TEXT DEFAULT '',
    address TEXT DEFAULT '',
    address_locked INTEGER DEFAULT 0,
    issue TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    last_message TEXT DEFAULT '',
    urgent INTEGER DEFAULT 0,
    source TEXT DEFAULT 'manual',
    source_ref TEXT DEFAULT '',
    status TEXT DEFAULT 'new',
    lat REAL,
    lng REAL,
    geo_quality TEXT DEFAULT '',
    service_minutes INTEGER,
    received_at TEXT,
    created_at TEXT,
    updated_at TEXT,
    scheduled_date TEXT DEFAULT '',
    crew TEXT DEFAULT '',
    stop_order INTEGER
);
CREATE INDEX IF NOT EXISTS jobs_phone ON jobs(phone);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS geocache (
    query TEXT PRIMARY KEY, lat REAL, lng REAL, quality TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS seen_activity (id TEXT PRIMARY KEY, seen_at TEXT);
"""

JOB_FIELDS = (
    "phone", "name", "address", "address_locked", "issue", "notes", "last_message", "urgent",
    "source", "source_ref", "status", "lat", "lng", "geo_quality", "service_minutes",
    "received_at", "scheduled_date", "crew", "stop_order",
)


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Store:
    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        with self.conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def conn(self):
        with self._lock:
            c = sqlite3.connect(self.path)
            c.row_factory = sqlite3.Row
            try:
                yield c
                c.commit()
            finally:
                c.close()

    # Jobs -----------------------------------------------------------------

    def add_job(self, **fields):
        fields = {k: v for k, v in fields.items() if k in JOB_FIELDS}
        stamp = now_iso()
        fields.setdefault("received_at", stamp)
        fields.setdefault("status", "new" if fields.get("address") else "needs_address")
        fields["created_at"] = fields["updated_at"] = stamp
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self.conn() as c:
            cur = c.execute(f"INSERT INTO jobs ({cols}) VALUES ({marks})", list(fields.values()))
            return cur.lastrowid

    def update_job(self, job_id, **fields):
        fields = {k: v for k, v in fields.items() if k in JOB_FIELDS}
        if not fields:
            return
        fields["updated_at"] = now_iso()
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE jobs SET {sets} WHERE id = ?", [*fields.values(), job_id])

    def delete_job(self, job_id):
        with self.conn() as c:
            c.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def get_job(self, job_id):
        with self.conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(row) if row else None

    def jobs(self, statuses=None):
        q = "SELECT * FROM jobs"
        args = []
        if statuses:
            q += " WHERE status IN (%s)" % ", ".join("?" for _ in statuses)
            args = list(statuses)
        q += " ORDER BY urgent DESC, received_at ASC, id ASC"
        with self.conn() as c:
            return [dict(r) for r in c.execute(q, args)]

    def open_job_for_phone(self, phone):
        if not phone:
            return None
        with self.conn() as c:
            row = c.execute(
                "SELECT * FROM jobs WHERE phone = ? AND status IN (%s) ORDER BY id DESC LIMIT 1"
                % ", ".join("?" for _ in OPEN_STATUSES),
                (phone, *OPEN_STATUSES),
            ).fetchone()
            return dict(row) if row else None

    def recent_closed_job_for_phone(self, phone, since_iso):
        with self.conn() as c:
            row = c.execute(
                "SELECT * FROM jobs WHERE phone = ? AND status IN ('done', 'cancelled') "
                "AND updated_at >= ? ORDER BY id DESC LIMIT 1",
                (phone, since_iso),
            ).fetchone()
            return dict(row) if row else None

    def counts(self):
        with self.conn() as c:
            return {r["status"]: r["n"] for r in c.execute(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}

    # Geocode cache ----------------------------------------------------------

    def geocache_get(self, query):
        with self.conn() as c:
            row = c.execute("SELECT lat, lng, quality FROM geocache WHERE query = ?", (query,)).fetchone()
            return dict(row) if row else None

    def geocache_put(self, query, lat, lng, quality):
        with self.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO geocache (query, lat, lng, quality, updated_at) VALUES (?, ?, ?, ?, ?)",
                (query, lat, lng, quality, now_iso()),
            )

    # Settings / sync bookkeeping -------------------------------------------

    def meta_get(self, key, default=None):
        with self.conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return json.loads(row["value"]) if row else default

    def meta_set(self, key, value):
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(value)))

    def seen(self, activity_id):
        with self.conn() as c:
            return c.execute("SELECT 1 FROM seen_activity WHERE id = ?", (activity_id,)).fetchone() is not None

    def mark_seen(self, activity_ids):
        stamp = now_iso()
        with self.conn() as c:
            c.executemany("INSERT OR IGNORE INTO seen_activity (id, seen_at) VALUES (?, ?)",
                          [(a, stamp) for a in activity_ids])
