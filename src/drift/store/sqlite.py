"""SQLite persistence: runs, append-only run events (FR-30), findings history (FR-33), posts (FR-26)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from drift.diff.tagging import Baseline, ChangeSet
from drift.rules.base import Finding

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    run_date TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    detection_complete INTEGER NOT NULL DEFAULT 0,
    status TEXT,
    exit_code INTEGER,
    matrix_version TEXT,
    summary TEXT
);
CREATE INDEX IF NOT EXISTS runs_by_date ON runs(run_date);

CREATE TABLE IF NOT EXISTS run_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    ts TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_run ON run_events(run_id, seq);
CREATE TRIGGER IF NOT EXISTS run_events_append_only
BEFORE UPDATE ON run_events BEGIN SELECT RAISE(ABORT, 'run_events is append-only'); END;

CREATE TABLE IF NOT EXISTS run_clusters (
    run_id TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    reachable INTEGER NOT NULL,
    PRIMARY KEY (run_id, cluster_id)
);

CREATE TABLE IF NOT EXISTS findings_history (
    run_id TEXT NOT NULL,
    run_date TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    finding_id TEXT NOT NULL,
    rule TEXT NOT NULL,
    subject TEXT NOT NULL,
    severity TEXT NOT NULL,
    tag TEXT NOT NULL,          -- NEW/ONGOING/WORSENED/NEW_CLUSTER/RESOLVED, or STALE (carried)
    finding TEXT NOT NULL       -- full Finding JSON
);
CREATE INDEX IF NOT EXISTS history_by_cluster ON findings_history(cluster_id, run_date);
CREATE INDEX IF NOT EXISTS history_by_run ON findings_history(run_id);

CREATE TABLE IF NOT EXISTS posts (
    run_date TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    forced INTEGER NOT NULL DEFAULT 0
);
"""

STALE = "STALE"


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # ------------------------------------------------------------------------------ runs

    def create_run(self, run_id: str, run_date: date, started_at: str) -> None:
        self._conn.execute(
            "INSERT INTO runs(run_id, run_date, started_at) VALUES (?, ?, ?)",
            (run_id, run_date.isoformat(), started_at),
        )

    def finish_run(
        self, run_id: str, finished_at: str, status: str, exit_code: int, summary: dict[str, Any] | None = None
    ) -> None:
        self._conn.execute(
            "UPDATE runs SET finished_at=?, status=?, exit_code=?, summary=? WHERE run_id=?",
            (finished_at, status, exit_code, json.dumps(summary or {}), run_id),
        )

    def run(self, run_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    # ---------------------------------------------------------------------------- events

    def append_event(self, run_id: str, step_id: str, kind: str, ts: str, payload_json: str) -> None:
        self._conn.execute(
            "INSERT INTO run_events(run_id, step_id, kind, ts, payload) VALUES (?, ?, ?, ?, ?)",
            (run_id, step_id, kind, ts, payload_json),
        )

    def events(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM run_events WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]

    # ----------------------------------------------------------------- detection + changes

    def save_detection(
        self,
        run_id: str,
        run_date: date,
        matrix_version: str,
        reachability: dict[str, bool],
        changes: ChangeSet,
    ) -> None:
        """Persist tonight's state. Marks the run as a valid baseline for later runs (D6)."""
        d = run_date.isoformat()
        rows: list[tuple[str, ...]] = []
        for t in changes.tagged:
            rows.append(_hist(run_id, d, t.finding, t.tag.value))
        for f in changes.stale:
            rows.append(_hist(run_id, d, f, STALE))
        for f in changes.resolved:
            rows.append(_hist(run_id, d, f, "RESOLVED"))
        with self._tx() as c:
            c.execute("DELETE FROM findings_history WHERE run_id=?", (run_id,))
            c.execute("DELETE FROM run_clusters WHERE run_id=?", (run_id,))
            c.executemany(
                "INSERT INTO findings_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            c.executemany(
                "INSERT INTO run_clusters VALUES (?, ?, ?)",
                [(run_id, cid, int(ok)) for cid, ok in reachability.items()],
            )
            c.execute(
                "UPDATE runs SET detection_complete=1, matrix_version=? WHERE run_id=?",
                (matrix_version, run_id),
            )

    def load_baseline(self, before: date) -> Baseline | None:
        """Latest run with completed detection whose run date is strictly before ``before`` (D6, D27)."""
        row = self._conn.execute(
            "SELECT run_id FROM runs WHERE detection_complete=1 AND run_date < ? "
            "ORDER BY run_date DESC, started_at DESC, rowid DESC LIMIT 1",
            (before.isoformat(),),
        ).fetchone()
        if row is None:
            return None
        run_id = row["run_id"]
        clusters = frozenset(
            r["cluster_id"] for r in self._conn.execute("SELECT cluster_id FROM run_clusters WHERE run_id=?", (run_id,))
        )
        findings = tuple(
            Finding.model_validate_json(r["finding"])
            for r in self._conn.execute(
                "SELECT finding FROM findings_history WHERE run_id=? AND tag != 'RESOLVED' ORDER BY rowid", (run_id,)
            )
        )
        return Baseline(run_id=run_id, clusters=clusters, findings=findings)

    # --------------------------------------------------------------------------- history

    def history(self, cluster: str, since: date | None = None) -> list[dict[str, Any]]:
        """FR-33: findings over time for one cluster (by ID). Only the latest completed run of each date
        counts: re-running a date replaces that date's record (FR-26)."""
        q = (
            "SELECT h.run_id, h.run_date, h.rule, h.subject, h.severity, h.tag, h.finding FROM findings_history h "
            "WHERE h.cluster_id=? AND h.run_id = (SELECT r.run_id FROM runs r WHERE r.run_date = h.run_date "
            "AND r.detection_complete=1 ORDER BY r.started_at DESC, r.rowid DESC LIMIT 1)"
        )
        args: list[Any] = [cluster]
        if since:
            q += " AND h.run_date >= ?"
            args.append(since.isoformat())
        rows = self._conn.execute(q + " ORDER BY h.run_date, h.rowid", args).fetchall()
        return [dict(r) | {"finding": json.loads(r["finding"])} for r in rows]

    def prune(self, today: date, retention_days: int) -> int:
        """Delete runs (and their events/history) older than the retention window. Returns runs removed."""
        cutoff = (today - timedelta(days=retention_days)).isoformat()
        with self._tx() as c:
            ids = [r["run_id"] for r in c.execute("SELECT run_id FROM runs WHERE run_date < ?", (cutoff,))]
            for table in ("findings_history", "run_clusters", "run_events", "runs"):
                c.executemany(f"DELETE FROM {table} WHERE run_id=?", [(i,) for i in ids])
            c.execute("DELETE FROM posts WHERE run_date < ?", (cutoff,))
        return len(ids)

    # ----------------------------------------------------------------------------- posts

    def post_for(self, run_date: date) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM posts WHERE run_date=?", (run_date.isoformat(),)).fetchone()
        return dict(row) if row else None

    def record_post(self, run_date: date, run_id: str, posted_at: str, forced: bool) -> None:
        self._conn.execute(
            "INSERT INTO posts(run_date, run_id, posted_at, forced) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(run_date) DO UPDATE SET run_id=excluded.run_id, posted_at=excluded.posted_at, "
            "forced=excluded.forced",
            (run_date.isoformat(), run_id, posted_at, int(forced)),
        )


def _hist(run_id: str, run_date: str, f: Finding, tag: str) -> tuple[str, ...]:
    return (
        run_id,
        run_date,
        f.cluster_id,
        f.id,
        f.rule.value,
        f.subject,
        f.severity.name,
        tag,
        f.model_dump_json(),
    )
