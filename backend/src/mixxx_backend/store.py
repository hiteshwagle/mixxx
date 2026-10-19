import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


class Store:
    def __init__(self, path: Path):
        self.lock = threading.RLock()
        self.owner = sqlite3.connect(
            path.with_suffix(".owner.sqlite3"), timeout=0
        )
        try:
            self.owner.execute("BEGIN EXCLUSIVE")
            self.owner.execute("CREATE TABLE IF NOT EXISTS owner (id INTEGER)")
        except sqlite3.OperationalError as error:
            self.owner.close()
            raise RuntimeError(
                "Another backend already owns this data directory"
            ) from error
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY CHECK(version = 1)
            );
            INSERT OR IGNORE INTO schema_version VALUES (1);
            CREATE TABLE IF NOT EXISTS documents (
                kind TEXT NOT NULL,
                id TEXT NOT NULL,
                body TEXT NOT NULL,
                PRIMARY KEY(kind, id)
            );
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL NOT NULL,
                kind TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                body TEXT NOT NULL
            );
        """)
        self.db.commit()

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def get(self, kind, identifier):
        with self.lock:
            row = self.db.execute(
                "SELECT body FROM documents WHERE kind=? AND id=?",
                (kind, identifier),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def all(self, kind):
        with self.lock:
            return [
                json.loads(row[0])
                for row in self.db.execute(
                    "SELECT body FROM documents WHERE kind=? ORDER BY id",
                    (kind,),
                )
            ]

    def put(self, kind, identifier, data):
        self.db.execute(
            "INSERT INTO documents VALUES (?, ?, ?) "
            "ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body",
            (kind, identifier, json.dumps(data, allow_nan=False)),
        )

    def event(self, kind, identifier, data):
        self.db.execute(
            "INSERT INTO events(created_at,kind,entity_id,body) "
            "VALUES (?,?,?,?)",
            (time.time(), kind, identifier, json.dumps(data)),
        )

    def events(self, after, limit):
        with self.lock:
            return [
                {**dict(row), "body": json.loads(row["body"])}
                for row in self.db.execute(
                    "SELECT * FROM events WHERE sequence>? "
                    "ORDER BY sequence LIMIT ?",
                    (after, limit),
                )
            ]

    def close(self):
        with self.lock:
            self.db.close()
            self.owner.close()


def new_id():
    return str(uuid4())
