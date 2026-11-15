"""SQLite 访问层：读数、换表记录、表计台账、还原批次、事件。

命名卷 metercalc-data 挂到 /app/data；重建数据：docker compose down -v 后重跑。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from . import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS meters (
    meter_id   TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    location   TEXT NOT NULL DEFAULT '',
    full_scale REAL NOT NULL DEFAULT 99999.9,
    ct_ratio   REAL NOT NULL DEFAULT 1.0
);
CREATE TABLE IF NOT EXISTS readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    meter_id    TEXT NOT NULL,
    read_at     TEXT NOT NULL,
    reading_kwh REAL NOT NULL,
    kind        TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'manual',
    precision   INTEGER NOT NULL DEFAULT 1,
    imported_at TEXT NOT NULL,
    UNIQUE (meter_id, read_at, kind, source)
);
CREATE TABLE IF NOT EXISTS changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    meter_id    TEXT NOT NULL,
    change_date TEXT NOT NULL,
    payload     TEXT NOT NULL,
    UNIQUE (meter_id, change_date)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    month      TEXT NOT NULL,
    meters     TEXT NOT NULL,
    payload    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_versions (
    version_id   TEXT PRIMARY KEY,
    batch_key    TEXT NOT NULL UNIQUE,
    ledger_key   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    month        TEXT NOT NULL,
    month_order  TEXT NOT NULL,
    payload      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    summary TEXT NOT NULL,
    detail  TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

ACTIVE_LEDGER_KEY = "active_ledger_version"


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or settings.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def session(db_path: Path | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.DatabaseError:
        return set()


def _migrate(conn: sqlite3.Connection) -> None:
    """老库升级：读数表补 source/precision 并放宽唯一键；runs 补 batch_key。"""
    columns = _columns(conn, "readings")
    if columns and "source" not in columns:
        conn.executescript(
            """
            ALTER TABLE readings RENAME TO readings_legacy;
            CREATE TABLE readings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                meter_id    TEXT NOT NULL,
                read_at     TEXT NOT NULL,
                reading_kwh REAL NOT NULL,
                kind        TEXT NOT NULL,
                source      TEXT NOT NULL DEFAULT 'manual',
                precision   INTEGER NOT NULL DEFAULT 1,
                imported_at TEXT NOT NULL,
                UNIQUE (meter_id, read_at, kind, source)
            );
            INSERT INTO readings (meter_id, read_at, reading_kwh, kind, source, precision, imported_at)
            SELECT meter_id, read_at, reading_kwh, kind, 'manual', 1, imported_at FROM readings_legacy;
            DROP TABLE readings_legacy;
            """
        )
    run_columns = _columns(conn, "runs")
    if run_columns and "batch_key" not in run_columns:
        conn.execute("ALTER TABLE runs ADD COLUMN batch_key TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_batch_key ON runs(batch_key)")


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate(conn)
    conn.commit()


# --------------------------------------------------------------------------- 表计台账


def upsert_meters(conn: sqlite3.Connection, meters: list[dict]) -> None:
    for meter in meters:
        conn.execute(
            """
            INSERT INTO meters (meter_id, name, location, full_scale, ct_ratio)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(meter_id) DO UPDATE SET
                name = excluded.name,
                location = excluded.location,
                full_scale = excluded.full_scale,
                ct_ratio = excluded.ct_ratio
            """,
            (
                meter["meter_id"],
                meter.get("name", ""),
                meter.get("location", ""),
                float(meter.get("full_scale") or settings.DEFAULT_FULL_SCALE),
                float(meter.get("ct_ratio") or 1.0),
            ),
        )


def all_meters(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM meters ORDER BY meter_id").fetchall()
    return [
        {
            "meter_id": row["meter_id"],
            "name": row["name"],
            "location": row["location"],
            "full_scale": float(row["full_scale"]),
            "ct_ratio": float(row["ct_ratio"]),
        }
        for row in rows
    ]


# --------------------------------------------------------------------------- 读数


def replace_readings(conn: sqlite3.Connection, rows: list[dict], now: str) -> int:
    conn.execute("DELETE FROM readings")
    conn.executemany(
        """
        INSERT INTO readings (meter_id, read_at, reading_kwh, kind, source, precision, imported_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                row["meter_id"],
                row["read_at"].isoformat(),
                float(row["reading_kwh"]),
                row["kind"],
                row.get("source") or "manual",
                int(row.get("precision", 1)),
                now,
            )
            for row in rows
        ],
    )
    return len(rows)


def all_readings(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT meter_id, read_at, reading_kwh, kind, source, precision FROM readings "
        "ORDER BY meter_id, read_at, kind, source"
    ).fetchall()
    return [
        {
            "meter_id": row["meter_id"],
            "read_at": date.fromisoformat(row["read_at"]),
            "reading_kwh": float(row["reading_kwh"]),
            "kind": row["kind"],
            "source": row["source"],
            "precision": int(row["precision"]),
        }
        for row in rows
    ]


def count_readings(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM readings").fetchone()
    return int(row["c"])


def reading_span(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT MIN(read_at) AS lo, MAX(read_at) AS hi FROM readings").fetchone()
    return {"date_from": row["lo"], "date_to": row["hi"]}


# --------------------------------------------------------------------------- 换表记录


def replace_changes(conn: sqlite3.Connection, changes: list[dict]) -> int:
    conn.execute("DELETE FROM changes")
    for change in changes:
        conn.execute(
            "INSERT INTO changes (meter_id, change_date, payload) VALUES (?, ?, ?)",
            (change["meter_id"], change["change_date"], json.dumps(change, ensure_ascii=False)),
        )
    return len(changes)


def all_changes(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT payload FROM changes ORDER BY change_date").fetchall()
    return [json.loads(row["payload"]) for row in rows]


def count_changes(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM changes").fetchone()
    return int(row["c"])


# --------------------------------------------------------------------------- 还原批次


def save_run(conn: sqlite3.Connection, payload: dict) -> None:
    batch_key = payload.get("batch_key") or ""
    if batch_key:
        existing = conn.execute(
            "SELECT run_id FROM runs WHERE batch_key = ?", (batch_key,)
        ).fetchone()
        if existing:
            # 同批重试：复用原批次号，不产生第二个批次（避免用量重复计入）
            payload = dict(payload)
            payload["run_id"] = existing["run_id"]
    conn.execute(
        "INSERT OR REPLACE INTO runs (run_id, created_at, month, meters, batch_key, payload) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            payload["run_id"],
            payload["created_at"],
            payload.get("month") or "",
            ",".join(payload.get("selected") or []),
            batch_key,
            json.dumps(payload, ensure_ascii=False),
        ),
    )


def find_run_by_batch(conn: sqlite3.Connection, batch_key: str) -> dict | None:
    if not batch_key:
        return None
    row = conn.execute(
        "SELECT payload FROM runs WHERE batch_key = ? ORDER BY rowid DESC LIMIT 1", (batch_key,)
    ).fetchone()
    return json.loads(row["payload"]) if row else None


def load_run(conn: sqlite3.Connection, run_id: str) -> dict | None:
    row = conn.execute("SELECT payload FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return json.loads(row["payload"]) if row else None


def latest_run(conn: sqlite3.Connection, month: str | None = None) -> dict | None:
    if month:
        row = conn.execute(
            "SELECT payload FROM runs WHERE month = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (month,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT payload FROM runs ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
    return json.loads(row["payload"]) if row else None


def list_runs(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT run_id, created_at, month, meters, batch_key FROM runs "
        "ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {
            "run_id": row["run_id"],
            "created_at": row["created_at"],
            "month": row["month"],
            "meters": row["meters"].split(",") if row["meters"] else [],
            "batch_key": row["batch_key"] or "",
        }
        for row in rows
    ]


# --------------------------------------------------------------------------- 台账版本（原子发布 / 回滚）


def active_ledger(conn: sqlite3.Connection) -> dict | None:
    version_id = get_meta(conn, ACTIVE_LEDGER_KEY)
    if not version_id:
        return None
    row = conn.execute(
        "SELECT * FROM ledger_versions WHERE version_id = ?", (version_id,)
    ).fetchone()
    return _ledger_row(row) if row else None


def ledger_by_batch(conn: sqlite3.Connection, batch_key: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM ledger_versions WHERE batch_key = ?", (batch_key,)
    ).fetchone()
    return _ledger_row(row) if row else None


def load_ledger_version(conn: sqlite3.Connection, version_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM ledger_versions WHERE version_id = ?", (version_id,)
    ).fetchone()
    return _ledger_row(row) if row else None


def ledger_history(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM ledger_versions ORDER BY rowid DESC LIMIT ?", (limit,)
    ).fetchall()
    return [
        {
            "version_id": row["version_id"],
            "batch_key": row["batch_key"],
            "ledger_key": row["ledger_key"],
            "created_at": row["created_at"],
            "run_id": row["run_id"],
            "month": row["month"],
            "month_order": json.loads(row["month_order"] or "[]"),
        }
        for row in rows
    ]


def _ledger_row(row: sqlite3.Row) -> dict:
    return {
        "version_id": row["version_id"],
        "batch_key": row["batch_key"],
        "ledger_key": row["ledger_key"],
        "created_at": row["created_at"],
        "run_id": row["run_id"],
        "month": row["month"],
        "month_order": json.loads(row["month_order"] or "[]"),
        "payload": json.loads(row["payload"] or "{}"),
    }


def save_ledger_version(conn: sqlite3.Connection, version: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO ledger_versions "
        "(version_id, batch_key, ledger_key, created_at, run_id, month, month_order, payload) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            version["version_id"],
            version["batch_key"],
            version["ledger_key"],
            version["created_at"],
            version["run_id"],
            version["month"],
            json.dumps(version.get("month_order") or [], ensure_ascii=False),
            json.dumps(version["payload"], ensure_ascii=False),
        ),
    )


def activate_ledger(conn: sqlite3.Connection, version_id: str) -> None:
    """一次性切换：只改 meta 指针，历史版本保留用于回滚。"""
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (ACTIVE_LEDGER_KEY, version_id),
    )


# --------------------------------------------------------------------------- 事件 / 元数据


def add_event(conn: sqlite3.Connection, kind: str, summary: str, detail: dict, now: str) -> None:
    conn.execute(
        "INSERT INTO events (at, kind, summary, detail) VALUES (?, ?, ?, ?)",
        (now, kind, summary, json.dumps(detail, ensure_ascii=False)),
    )


def list_events(conn: sqlite3.Connection, limit: int = 30) -> list[dict]:
    rows = conn.execute(
        "SELECT at, kind, summary, detail FROM events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [
        {
            "at": row["at"],
            "kind": row["kind"],
            "summary": row["summary"],
            "detail": json.loads(row["detail"] or "{}"),
        }
        for row in rows
    ]


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
