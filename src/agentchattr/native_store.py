"""Durable native notifications and locally owned Codex runtimes.

Each operation uses a separate SQLite connection: server threads, wrappers and
operator commands may use the same database. Runtime flock ownership serializes
wrapper/recovery mutations; SQLite transactions serialize notification writers.
"""

import contextlib
import fcntl
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
import uuid

log = logging.getLogger(__name__)
OPEN_STATES = ("pending", "submitting", "accepted", "uncertain", "failed")


class NativeStore:
    def __init__(self, data_dir):
        self.root = Path(data_dir) / "native"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.path = self.root / "deliveries.sqlite3"
        # Create privately before sqlite opens it (also protects persisted tokens).
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runtimes (
                    id TEXT PRIMARY KEY, identity_id TEXT UNIQUE NOT NULL,
                    base TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT UNIQUE NOT NULL, identity_id TEXT NOT NULL,
                    payload TEXT NOT NULL, prompt TEXT,
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempt_id TEXT, submission_id TEXT, turn_id TEXT,
                    detail TEXT NOT NULL DEFAULT '',
                    created REAL NOT NULL, updated REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS delivery_identity
                    ON deliveries(identity_id, seq);
                CREATE TABLE IF NOT EXISTS delivery_history (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL, state TEXT NOT NULL,
                    attempt_id TEXT, submission_id TEXT, turn_id TEXT,
                    detail TEXT NOT NULL, timestamp REAL NOT NULL
                );
            """)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, identity_id, payload):
        event_id = uuid.uuid4().hex
        now = time.time()
        with self.connect() as db:
            db.execute("INSERT INTO deliveries(id, identity_id, payload, created, updated) VALUES(?,?,?,?,?)",
                       (event_id, identity_id, json.dumps(payload), now, now))
        log.info("Native delivery %s pending identity=%s", event_id, identity_id)
        return event_id

    def create_runtime(self, base, registration, **data):
        runtime_id = uuid.uuid4().hex
        self.save_runtime(dict(data, id=runtime_id, base=base,
                               identity_id=registration["identity_id"], registration=registration))
        return self.runtime(runtime_id)

    def save_runtime(self, runtime):
        with self.connect() as db:
            db.execute("""INSERT INTO runtimes VALUES(?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET data=excluded.data""",
                       (runtime["id"], runtime["identity_id"], runtime["base"], json.dumps(runtime)))

    def runtime(self, runtime_id):
        with self.connect() as db:
            row = db.execute("SELECT data FROM runtimes WHERE id=?", (runtime_id,)).fetchone()
        if not row:
            raise ValueError(f"Unknown native runtime: {runtime_id}")
        return json.loads(row["data"])

    @contextlib.contextmanager
    def lock(self, runtime_id):
        # Validate IDs before using them as filenames, including CLI input.
        if len(runtime_id) != 32 or any(c not in "0123456789abcdef" for c in runtime_id):
            raise ValueError("Invalid native runtime ID")
        with (self.root / f"{runtime_id}.lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Runtime is already owned by a wrapper; stop it before recovery") from None
            try:
                yield handle
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def deliveries(self, identity_id=None, open_only=False):
        clauses, params = [], []
        if identity_id:
            clauses.append("identity_id=?")
            params.append(identity_id)
        if open_only:
            clauses.append("state IN ('pending','submitting','accepted','uncertain','failed')")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as db:
            rows = db.execute("SELECT * FROM deliveries" + where + " ORDER BY seq", params).fetchall()
        return [dict(row) for row in rows]

    def transition(self, event_id, state, *, expected_state=None, **fields):
        allowed = {"prompt", "attempt_id", "submission_id", "turn_id", "detail"}
        if fields.keys() - allowed or state not in (*OPEN_STATES, "completed", "discarded", "delivered"):
            raise ValueError("Invalid delivery update")
        with self.connect() as db:
            assignments = ["state=?", "updated=?"] + [f"{key}=?" for key in fields]
            condition = " AND state=?" if expected_state is not None else ""
            updated = db.execute(f"UPDATE deliveries SET {', '.join(assignments)} WHERE id=?{condition}",
                                 [state, time.time(), *fields.values(), event_id,
                                  *([expected_state] if expected_state is not None else [])])
            if expected_state is not None and not updated.rowcount:
                return False
            row = db.execute("SELECT * FROM deliveries WHERE id=?", (event_id,)).fetchone()
            if row is None:
                raise ValueError(f"Unknown delivery: {event_id}")
            db.execute("""INSERT INTO delivery_history
                (event_id,state,attempt_id,submission_id,turn_id,detail,timestamp)
                VALUES(?,?,?,?,?,?,?)""", (event_id, state, row["attempt_id"], row["submission_id"],
                                           row["turn_id"], row["detail"], time.time()))
        log.info("Native delivery %s %s submission=%s turn=%s", event_id, state,
                 row["submission_id"], row["turn_id"])
        return True

    def resolve(self, event_id, action):
        with self.connect() as db:
            row = db.execute("SELECT * FROM deliveries WHERE id=?", (event_id,)).fetchone()
            if not row:
                raise ValueError(f"Unknown delivery: {event_id}")
            runtime = db.execute("SELECT id FROM runtimes WHERE identity_id=?", (row["identity_id"],)).fetchone()
        if not runtime:
            raise ValueError("Delivery has no runtime yet")
        with self.lock(runtime["id"]):
            # A stopped wrapper cannot race this re-read or the operator decision.
            row = next(r for r in self.deliveries(row["identity_id"]) if r["id"] == event_id)
            if row["state"] not in ("uncertain", "failed", "pending"):
                raise ValueError(f"Cannot {action} a {row['state']} delivery; resume to reconcile it first")
            if action == "retry":
                self.transition(event_id, "pending", attempt_id=None, submission_id=None,
                                turn_id=None, detail="Operator requested retry; previous attempt may have run")
            elif action == "discard":
                self.transition(event_id, "discarded", detail="Operator discarded; does not cancel backend work")
            else:
                raise ValueError("Unknown delivery action")

    def listing(self):
        with self.connect() as db:
            rows = db.execute("""SELECT d.id, r.id AS runtime_id, d.state,
                d.submission_id, d.turn_id, d.detail FROM deliveries d
                LEFT JOIN runtimes r USING(identity_id) ORDER BY d.seq""").fetchall()
        return [dict(row) for row in rows]
