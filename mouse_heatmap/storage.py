"""SQLite persistence for mouse recording sessions."""

from __future__ import annotations

import platform
import socket
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at_ns INTEGER NOT NULL,
    ended_at_ns INTEGER,
    sample_interval_ms REAL NOT NULL DEFAULT 0,
    label TEXT,
    hostname TEXT NOT NULL,
    platform TEXT NOT NULL,
    point_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    timestamp_ns INTEGER NOT NULL,
    x INTEGER NOT NULL,
    y INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_positions_session_time
    ON positions(session_id, timestamp_ns);
"""


@dataclass(frozen=True)
class Session:
    id: int
    started_at_ns: int
    ended_at_ns: int | None
    sample_interval_ms: float
    label: str | None
    hostname: str
    platform: str
    point_count: int


def connect_database(path: str | Path) -> sqlite3.Connection:
    """Open a writable database and ensure its schema exists."""
    database_path = Path(path).expanduser()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=30)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(SCHEMA)
    return connection


def open_database(path: str | Path) -> sqlite3.Connection:
    """Open an existing mouse database read-only without mutating it."""
    database_path = Path(path).expanduser().resolve()
    if not database_path.is_file():
        raise FileNotFoundError(f"Database does not exist: {database_path}")
    connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True, timeout=30)
    tables = {
        row[0]
        for row in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name IN ('sessions', 'positions')
            """
        )
    }
    if tables != {"sessions", "positions"}:
        connection.close()
        raise ValueError(f"Not a mouse position database: {database_path}")
    return connection


def create_session(
    connection: sqlite3.Connection,
    sample_interval_ms: float,
    label: str | None = None,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO sessions (
            started_at_ns, sample_interval_ms, label, hostname, platform
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            time.time_ns(),
            sample_interval_ms,
            label,
            socket.gethostname(),
            platform.platform(),
        ),
    )
    connection.commit()
    return int(cursor.lastrowid)


def finish_session(
    connection: sqlite3.Connection,
    session_id: int,
    ended_at_ns: int | None = None,
) -> None:
    """Mark a recording complete and cache its final point count."""
    connection.execute(
        """
        UPDATE sessions
        SET ended_at_ns = ?,
            point_count = (
                SELECT COUNT(*) FROM positions WHERE session_id = ?
            )
        WHERE id = ?
        """,
        (ended_at_ns or time.time_ns(), session_id, session_id),
    )
    connection.commit()


def get_session(connection: sqlite3.Connection, session_id: int) -> Session | None:
    row = connection.execute(
        """
        SELECT s.id, s.started_at_ns, s.ended_at_ns, s.sample_interval_ms,
               s.label, s.hostname, s.platform, COUNT(p.id)
        FROM sessions AS s
        LEFT JOIN positions AS p ON p.session_id = s.id
        WHERE s.id = ?
        GROUP BY s.id
        """,
        (session_id,),
    ).fetchone()
    return Session(*row) if row else None


def list_sessions(connection: sqlite3.Connection) -> list[Session]:
    rows = connection.execute(
        """
        SELECT s.id, s.started_at_ns, s.ended_at_ns, s.sample_interval_ms,
               s.label, s.hostname, s.platform, COUNT(p.id)
        FROM sessions AS s
        LEFT JOIN positions AS p ON p.session_id = s.id
        GROUP BY s.id
        ORDER BY s.started_at_ns DESC
        """
    ).fetchall()
    return [Session(*row) for row in rows]


def position_bounds(
    connection: sqlite3.Connection,
    session_ids: Sequence[int] | None = None,
) -> tuple[int, int | None, int | None, int | None, int | None]:
    """Return count and coordinate bounds for a position selection."""
    parameters: tuple[int, ...] = ()
    where = ""
    if session_ids:
        placeholders = ",".join("?" for _ in session_ids)
        where = f"WHERE session_id IN ({placeholders})"
        parameters = tuple(session_ids)
    row = connection.execute(
        f"SELECT COUNT(*), MIN(x), MAX(x), MIN(y), MAX(y) FROM positions {where}",
        parameters,
    ).fetchone()
    return row


def iter_positions(
    connection: sqlite3.Connection,
    session_ids: Sequence[int] | None = None,
    chunk_size: int = 10_000,
) -> Iterator[tuple[int, int, int, int]]:
    """Yield ``(session_id, timestamp_ns, x, y)`` ordered by session/time."""
    parameters: tuple[int, ...] = ()
    where = ""
    if session_ids:
        placeholders = ",".join("?" for _ in session_ids)
        where = f"WHERE session_id IN ({placeholders})"
        parameters = tuple(session_ids)

    cursor = connection.execute(
        f"""
        SELECT session_id, timestamp_ns, x, y
        FROM positions
        {where}
        ORDER BY session_id, timestamp_ns
        """,
        parameters,
    )
    while rows := cursor.fetchmany(chunk_size):
        yield from rows
