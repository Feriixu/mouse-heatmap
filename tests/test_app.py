from __future__ import annotations

import csv
import io
import math
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime
from unittest import mock
from pathlib import Path

from mouse_heatmap.analysis import calculate_stats, create_heatmap, export_csv
from mouse_heatmap.cli import _build_parser, main
from mouse_heatmap.recorder import PositionWriter, record_positions
from mouse_heatmap.storage import (
    connect_database,
    create_session,
    finish_session,
    list_sessions,
    position_bounds,
)


class MouseHeatmapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database = self.root / "positions.sqlite"
        connection = connect_database(self.database)
        try:
            first = create_session(connection, 0, "first")
            connection.executemany(
                """
                INSERT INTO positions (session_id, timestamp_ns, x, y)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (first, 1_000_000_000, 0, 0),
                    (first, 2_000_000_000, 3, 4),
                ],
            )
            connection.commit()
            finish_session(connection, first, 2_000_000_000)

            second = create_session(connection, 10, "second")
            connection.executemany(
                """
                INSERT INTO positions (session_id, timestamp_ns, x, y)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (second, 100_000_000_000, -2, 0),
                    (second, 102_000_000_000, -2, 6),
                ],
            )
            connection.commit()
            finish_session(connection, second, 102_000_000_000)
            self.first = first
            self.second = second
        finally:
            connection.close()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_sessions_and_bounds_are_persisted(self) -> None:
        connection = connect_database(self.database)
        try:
            sessions = list_sessions(connection)
            bounds = position_bounds(connection, [self.first])
        finally:
            connection.close()
        self.assertEqual([session.point_count for session in sessions], [2, 2])
        self.assertEqual(bounds, (2, 0, 3, 0, 4))

    def test_stats_sum_session_durations_without_idle_gap(self) -> None:
        stats = calculate_stats(self.database)
        self.assertEqual(stats.point_count, 4)
        self.assertEqual(stats.session_count, 2)
        self.assertAlmostEqual(stats.duration_seconds, 3)
        self.assertAlmostEqual(stats.distance_pixels, 11)
        self.assertAlmostEqual(stats.average_speed_px_s, 11 / 3)
        self.assertAlmostEqual(stats.maximum_speed_px_s, 5)
        self.assertEqual((stats.min_x, stats.max_x, stats.min_y, stats.max_y), (-2, 3, 0, 6))

    def test_csv_export_can_filter_sessions(self) -> None:
        stream = io.StringIO()
        count = export_csv(self.database, stream, [self.second])
        rows = list(csv.DictReader(io.StringIO(stream.getvalue())))
        self.assertEqual(count, 2)
        self.assertEqual([row["session_id"] for row in rows], [str(self.second)] * 2)
        self.assertEqual([row["x"] for row in rows], ["-2", "-2"])

    def test_default_heatmap_filename_is_rfc3339_utc(self) -> None:
        arguments = _build_parser().parse_args(["heatmap"])
        filename = Path(arguments.output)
        self.assertEqual(filename.suffix, ".png")
        timestamp = filename.stem
        self.assertTrue(timestamp.endswith("Z"))
        self.assertIsNotNone(
            datetime.fromisoformat(timestamp.replace("Z", "+00:00")).tzinfo
        )

    def test_heatmap_defaults_to_most_recent_session(self) -> None:
        with mock.patch("mouse_heatmap.cli.create_heatmap", return_value=2) as create:
            result = main(["heatmap", "--db", str(self.database)])
        self.assertEqual(result, 0)
        self.assertEqual(create.call_args.kwargs["session_ids"], [self.second])

    def test_heatmap_all_sessions_option_uses_every_session(self) -> None:
        with mock.patch("mouse_heatmap.cli.create_heatmap", return_value=4) as create:
            result = main(
                ["heatmap", "--db", str(self.database), "--all-sessions"]
            )
        self.assertEqual(result, 0)
        self.assertIsNone(create.call_args.kwargs["session_ids"])

    def test_heatmap_open_option_launches_default_application(self) -> None:
        output = self.root / "opened.png"
        with (
            mock.patch("mouse_heatmap.cli.create_heatmap", return_value=2),
            mock.patch("mouse_heatmap.cli._open_with_default_application") as opener,
        ):
            result = main(
                [
                    "heatmap",
                    "--db",
                    str(self.database),
                    "--output",
                    str(output),
                    "--open",
                ]
            )
        self.assertEqual(result, 0)
        opener.assert_called_once_with(str(output))

    def test_heatmap_is_created(self) -> None:
        output = self.root / "charts" / "heatmap.png"
        count = create_heatmap(
            self.database,
            output,
            session_ids=[self.first],
            bins=20,
            smoothing=1,
        )
        self.assertEqual(count, 2)
        self.assertTrue(output.is_file())
        self.assertGreater(output.stat().st_size, 1_000)

    def test_position_writer_flushes_queued_points(self) -> None:
        connection = connect_database(self.database)
        try:
            session = create_session(connection, 0, "writer")
        finally:
            connection.close()
        writer = PositionWriter(self.database, session)
        writer.start()
        writer.add(200_000_000_000, 20, 30)
        writer.add(201_000_000_000, 21, 31)
        writer.close()
        self.assertEqual(writer.points_written, 2)

    def test_cli_rejects_an_unknown_session(self) -> None:
        result = main(
            ["stats", "--db", str(self.database), "--session", "999999"]
        )
        self.assertEqual(result, 1)

    def test_read_command_does_not_create_a_missing_database(self) -> None:
        missing = self.root / "missing.sqlite"
        result = main(["sessions", "--db", str(missing)])
        self.assertEqual(result, 1)
        self.assertFalse(missing.exists())

    def test_export_cannot_overwrite_database(self) -> None:
        original_size = self.database.stat().st_size
        with self.assertRaisesRegex(ValueError, "must not overwrite"):
            export_csv(self.database, self.database)
        self.assertEqual(self.database.stat().st_size, original_size)
        connection = sqlite3.connect(self.database)
        try:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            connection.close()

    def test_heatmap_cannot_overwrite_database(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not overwrite"):
            create_heatmap(self.database, self.database)

    def test_live_session_count_is_calculated_from_positions(self) -> None:
        connection = connect_database(self.database)
        try:
            session = create_session(connection, 0, "live")
            connection.execute(
                """
                INSERT INTO positions (session_id, timestamp_ns, x, y)
                VALUES (?, ?, ?, ?)
                """,
                (session, 300_000_000_000, 1, 2),
            )
            connection.commit()
            live = next(item for item in list_sessions(connection) if item.id == session)
        finally:
            connection.close()
        self.assertEqual(live.point_count, 1)
        self.assertIsNone(live.ended_at_ns)

    def test_long_event_gap_is_not_counted_as_active_time(self) -> None:
        stats = calculate_stats(self.database, idle_gap_seconds=0.5)
        self.assertEqual(stats.active_seconds, 0)
        self.assertEqual(stats.moving_speed_px_s, 0)

    def test_recording_rejects_non_finite_options_before_startup(self) -> None:
        for sample_interval in (math.nan, math.inf):
            with self.subTest(sample_interval=sample_interval):
                with self.assertRaises(ValueError):
                    record_positions(
                        self.root / "invalid.sqlite",
                        sample_interval_ms=sample_interval,
                    )
        with self.assertRaises(ValueError):
            record_positions(self.root / "invalid.sqlite", duration_seconds=math.nan)
        self.assertFalse((self.root / "invalid.sqlite").exists())

    def test_timed_recording_flushes_and_finishes_session(self) -> None:
        class FakeListener:
            def __init__(self, on_move):
                self.on_move = on_move
                self.alive = False

            def start(self):
                self.alive = True

            def is_alive(self):
                return self.alive

            def stop(self):
                self.alive = False

            def join(self):
                return None

        class FakeController:
            position = (11, 22)

        fake_pynput = types.ModuleType("pynput")
        fake_pynput.mouse = types.SimpleNamespace(
            Listener=FakeListener,
            Controller=FakeController,
        )
        recorded_database = self.root / "recorded.sqlite"
        with mock.patch.dict(sys.modules, {"pynput": fake_pynput}):
            session_id, count = record_positions(
                recorded_database,
                duration_seconds=0.001,
                label="timed",
            )

        self.assertEqual(count, 1)
        connection = connect_database(recorded_database)
        try:
            session = next(
                item for item in list_sessions(connection) if item.id == session_id
            )
        finally:
            connection.close()
        self.assertEqual(session.point_count, 1)
        self.assertIsNotNone(session.ended_at_ns)


if __name__ == "__main__":
    unittest.main()
