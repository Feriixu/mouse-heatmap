"""Command-line interface for mouse position recording and analysis."""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from . import __version__
from .analysis import calculate_stats, create_heatmap, export_csv
from .recorder import record_positions
from .storage import get_session, list_sessions, open_database

DEFAULT_DATABASE = "mouse_positions.sqlite"


def _default_heatmap_output() -> str:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return f"{timestamp.replace('+00:00', 'Z')}.png"


def _open_with_default_application(path: str | Path) -> None:
    resolved = Path(path).expanduser().resolve()
    if sys.platform == "win32":
        os.startfile(resolved)  # type: ignore[attr-defined]
        return
    command = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen(
        [command, str(resolved)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _session_ids(values: Sequence[int] | None) -> list[int] | None:
    return list(dict.fromkeys(values)) if values else None


def _validate_sessions(database_path: str, session_ids: Sequence[int] | None) -> None:
    if not session_ids:
        return
    connection = open_database(database_path)
    try:
        missing = [value for value in session_ids if get_session(connection, value) is None]
    finally:
        connection.close()
    if missing:
        joined = ", ".join(str(value) for value in missing)
        raise ValueError(f"Unknown session id(s): {joined}")


def _format_timestamp(timestamp_ns: int | None) -> str:
    if timestamp_ns is None:
        return "recording"
    return datetime.fromtimestamp(timestamp_ns / 1_000_000_000).astimezone().isoformat(
        sep=" ", timespec="seconds"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mouse-heatmap",
        description="Record global mouse positions and analyze them later.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser("record", help="record global mouse movement")
    record.add_argument("--db", default=DEFAULT_DATABASE, help="SQLite database path")
    record.add_argument(
        "--sample-ms",
        type=float,
        default=0,
        help="minimum milliseconds between samples (default: every event)",
    )
    record.add_argument(
        "--duration",
        type=float,
        help="stop automatically after this many seconds",
    )
    record.add_argument("--label", help="optional name for this recording session")

    sessions = subparsers.add_parser("sessions", help="list recording sessions")
    sessions.add_argument("--db", default=DEFAULT_DATABASE, help="SQLite database path")

    heatmap = subparsers.add_parser("heatmap", help="create a heatmap image")
    heatmap.add_argument("--db", default=DEFAULT_DATABASE, help="SQLite database path")
    heatmap_sessions = heatmap.add_mutually_exclusive_group()
    heatmap_sessions.add_argument(
        "--session",
        type=int,
        action="append",
        help="session id to include; repeat to combine (default: most recent)",
    )
    heatmap_sessions.add_argument(
        "-a",
        "--all-sessions",
        action="store_true",
        help="include every recording session",
    )
    heatmap.add_argument(
        "--output",
        default=_default_heatmap_output(),
        help="image path (default: current RFC 3339 UTC timestamp)",
    )
    heatmap.add_argument("--bins", type=int, default=160, help="heatmap resolution")
    heatmap.add_argument(
        "--smoothing", type=float, default=1.5, help="Gaussian smoothing strength"
    )
    heatmap.add_argument("--cmap", default="inferno", help="Matplotlib color map")
    heatmap.add_argument("--title", help="custom chart title")
    heatmap.add_argument("--show", action="store_true", help="open a Matplotlib window")
    heatmap.add_argument(
        "--open",
        action="store_true",
        help="open the saved image in the default image application",
    )

    stats = subparsers.add_parser("stats", help="show movement statistics")
    stats.add_argument("--db", default=DEFAULT_DATABASE, help="SQLite database path")
    stats.add_argument(
        "--session",
        type=int,
        action="append",
        help="session id to include; repeat to combine (default: all)",
    )
    stats.add_argument(
        "--idle-gap",
        type=float,
        default=1.0,
        help="seconds without events before a movement gap is inactive (default: 1)",
    )

    export = subparsers.add_parser("export", help="export positions to CSV")
    export.add_argument("--db", default=DEFAULT_DATABASE, help="SQLite database path")
    export.add_argument(
        "--session",
        type=int,
        action="append",
        help="session id to include; repeat to combine (default: all)",
    )
    export.add_argument("--output", default="mouse_positions.csv", help="CSV path")
    return parser


def _record_command(arguments: argparse.Namespace) -> None:
    database = str(Path(arguments.db).expanduser())
    print(f"Recording global mouse movement to {database}")
    if arguments.duration is None:
        print("Press Ctrl+C to stop.")
    else:
        print(f"Recording for {arguments.duration:g} seconds...")
    session_id, count = record_positions(
        database,
        sample_interval_ms=arguments.sample_ms,
        duration_seconds=arguments.duration,
        label=arguments.label,
    )
    print(f"Saved {count:,} positions in session {session_id}.")


def _sessions_command(arguments: argparse.Namespace) -> None:
    connection = open_database(arguments.db)
    try:
        sessions = list_sessions(connection)
    finally:
        connection.close()
    if not sessions:
        print("No recording sessions found.")
        return

    print(
        f"{'ID':>4}  {'POINTS':>10}  {'SAMPLE':>9}  "
        f"{'STATUS':>9}  {'STARTED':<25}  LABEL"
    )
    for session in sessions:
        sample = "all" if session.sample_interval_ms == 0 else f"{session.sample_interval_ms:g}ms"
        status = "complete" if session.ended_at_ns is not None else "recording"
        print(
            f"{session.id:>4}  {session.point_count:>10,}  {sample:>9}  "
            f"{status:>9}  {_format_timestamp(session.started_at_ns):<25}  "
            f"{session.label or '-'}"
        )


def _heatmap_command(arguments: argparse.Namespace) -> None:
    sessions = _session_ids(arguments.session)
    if arguments.all_sessions:
        sessions = None
    elif sessions is None:
        connection = open_database(arguments.db)
        try:
            available_sessions = list_sessions(connection)
        finally:
            connection.close()
        if not available_sessions:
            raise ValueError("No recording sessions found")
        sessions = [available_sessions[0].id]
    else:
        _validate_sessions(arguments.db, sessions)
    count = create_heatmap(
        arguments.db,
        arguments.output,
        session_ids=sessions,
        bins=arguments.bins,
        smoothing=arguments.smoothing,
        colormap=arguments.cmap,
        title=arguments.title,
        show=arguments.show,
    )
    print(f"Created {Path(arguments.output).expanduser()} from {count:,} positions.")
    if arguments.open:
        _open_with_default_application(arguments.output)


def _stats_command(arguments: argparse.Namespace) -> None:
    sessions = _session_ids(arguments.session)
    _validate_sessions(arguments.db, sessions)
    stats = calculate_stats(
        arguments.db, sessions, idle_gap_seconds=arguments.idle_gap
    )
    if not stats.point_count:
        print("No recorded positions matched the selection.")
        return
    width = stats.max_x - stats.min_x if stats.min_x is not None else 0
    height = stats.max_y - stats.min_y if stats.min_y is not None else 0
    print(f"Sessions:             {stats.session_count:,}")
    print(f"Positions:            {stats.point_count:,}")
    print(f"Recorded span:        {stats.duration_seconds:,.2f} s")
    print(f"Estimated active time:{stats.active_seconds:>10,.2f} s")
    print(f"Distance traveled:    {stats.distance_pixels:,.1f} px")
    print(f"Average speed:        {stats.average_speed_px_s:,.1f} px/s")
    print(f"Moving speed:         {stats.moving_speed_px_s:,.1f} px/s")
    print(f"Maximum speed:        {stats.maximum_speed_px_s:,.1f} px/s")
    print(f"Coordinate bounds:    {stats.min_x},{stats.min_y} to {stats.max_x},{stats.max_y}")
    print(f"Covered span:         {width:,} x {height:,} px")


def _export_command(arguments: argparse.Namespace) -> None:
    sessions = _session_ids(arguments.session)
    _validate_sessions(arguments.db, sessions)
    count = export_csv(arguments.db, arguments.output, sessions)
    print(f"Exported {count:,} positions to {Path(arguments.output).expanduser()}.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    commands = {
        "record": _record_command,
        "sessions": _sessions_command,
        "heatmap": _heatmap_command,
        "stats": _stats_command,
        "export": _export_command,
    }
    try:
        commands[arguments.command](arguments)
    except (RuntimeError, ValueError, OSError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
