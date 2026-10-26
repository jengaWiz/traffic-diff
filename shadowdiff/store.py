"""SQLite storage for recorded exchanges, replay runs and their results."""

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS exchanges (
    id INTEGER PRIMARY KEY,
    session TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    query TEXT NOT NULL DEFAULT '',
    req_headers TEXT NOT NULL DEFAULT '{}',
    req_body BLOB,
    status INTEGER NOT NULL,
    resp_headers TEXT NOT NULL DEFAULT '{}',
    resp_body BLOB,
    latency_ms REAL,
    recorded_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exchanges_session_path ON exchanges (session, path);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    session TEXT NOT NULL,
    target TEXT NOT NULL,
    concurrency INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    total INTEGER NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0,
    passed INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    started_at REAL,
    finished_at REAL
);

CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs (id),
    exchange_id INTEGER NOT NULL REFERENCES exchanges (id),
    status INTEGER,
    resp_body BLOB,
    latency_ms REAL,
    error TEXT,
    diffs TEXT NOT NULL DEFAULT '[]',
    breaking INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_results_run_breaking ON results (run_id, breaking);
"""


@dataclass
class Exchange:
    session: str
    method: str
    path: str
    query: str
    req_headers: dict[str, str]
    req_body: bytes
    status: int
    resp_headers: dict[str, str]
    resp_body: bytes
    latency_ms: float
    recorded_at: float = field(default_factory=time.time)
    id: int | None = None


@dataclass
class Result:
    exchange_id: int
    status: int | None
    resp_body: bytes | None
    latency_ms: float | None
    error: str | None = None
    diffs: list[dict[str, Any]] = field(default_factory=list)
    breaking: bool = False


@dataclass
class Run:
    id: int
    session: str
    target: str
    concurrency: int
    status: str
    total: int
    done: int
    passed: int
    failed: int
    started_at: float | None
    finished_at: float | None


class Store:
    def __init__(self, path: str = "shadowdiff.db"):
        self.path = path
        # The proxy writes from worker threads; callers serialize writes themselves.
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # exchanges

    def add_exchanges(self, exchanges: list[Exchange]) -> None:
        with self.conn:
            self.conn.executemany(
                "INSERT INTO exchanges (session, method, path, query, req_headers, req_body,"
                " status, resp_headers, resp_body, latency_ms, recorded_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        e.session,
                        e.method,
                        e.path,
                        e.query,
                        json.dumps(e.req_headers),
                        e.req_body,
                        e.status,
                        json.dumps(e.resp_headers),
                        e.resp_body,
                        e.latency_ms,
                        e.recorded_at,
                    )
                    for e in exchanges
                ],
            )

    def exchanges(self, session: str) -> list[Exchange]:
        rows = self.conn.execute(
            "SELECT * FROM exchanges WHERE session = ? ORDER BY id", (session,)
        ).fetchall()
        return [_exchange_from_row(r) for r in rows]

    def exchanges_by_id(self, ids: list[int]) -> dict[int, Exchange]:
        if not ids:
            return {}
        out: dict[int, Exchange] = {}
        # Stay under SQLite's bound-parameter limit.
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            placeholders = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"SELECT * FROM exchanges WHERE id IN ({placeholders})", chunk
            ).fetchall()
            out.update({r["id"]: _exchange_from_row(r) for r in rows})
        return out

    def sessions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT session, COUNT(*) AS exchanges, MIN(recorded_at) AS first_seen,"
            " MAX(recorded_at) AS last_seen FROM exchanges GROUP BY session ORDER BY session"
        ).fetchall()
        return [dict(r) for r in rows]

    # runs and results

    def create_run(self, session: str, target: str, concurrency: int, total: int) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO runs (session, target, concurrency, status, total, started_at)"
                " VALUES (?, ?, ?, 'running', ?, ?)",
                (session, target, concurrency, total, time.time()),
            )
        return cur.lastrowid

    def add_results(self, run_id: int, results: list[Result]) -> None:
        with self.conn:
            self.conn.executemany(
                "INSERT INTO results (run_id, exchange_id, status, resp_body, latency_ms,"
                " error, diffs, breaking) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id,
                        r.exchange_id,
                        r.status,
                        r.resp_body,
                        r.latency_ms,
                        r.error,
                        json.dumps(r.diffs),
                        int(r.breaking),
                    )
                    for r in results
                ],
            )

    def finish_run(self, run_id: int, status: str, done: int, passed: int, failed: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET status = ?, done = ?, passed = ?, failed = ?, finished_at = ?"
                " WHERE id = ?",
                (status, done, passed, failed, time.time(), run_id),
            )

    def run(self, run_id: int) -> Run | None:
        row = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return Run(**dict(row)) if row else None

    def latest_run_id(self) -> int | None:
        row = self.conn.execute("SELECT MAX(id) FROM runs").fetchone()
        return row[0]

    def results(self, run_id: int) -> list[Result]:
        rows = self.conn.execute(
            "SELECT * FROM results WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [
            Result(
                exchange_id=r["exchange_id"],
                status=r["status"],
                resp_body=r["resp_body"],
                latency_ms=r["latency_ms"],
                error=r["error"],
                diffs=json.loads(r["diffs"]),
                breaking=bool(r["breaking"]),
            )
            for r in rows
        ]


def _exchange_from_row(r: sqlite3.Row) -> Exchange:
    return Exchange(
        id=r["id"],
        session=r["session"],
        method=r["method"],
        path=r["path"],
        query=r["query"],
        req_headers=json.loads(r["req_headers"]),
        req_body=r["req_body"] or b"",
        status=r["status"],
        resp_headers=json.loads(r["resp_headers"]),
        resp_body=r["resp_body"] or b"",
        latency_ms=r["latency_ms"],
        recorded_at=r["recorded_at"],
    )
