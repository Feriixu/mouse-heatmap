"""Global mouse movement recorder."""

from __future__ import annotations

import math
import queue
import signal
import sqlite3
import threading
import time
from pathlib import Path
from typing import Final

from .storage import connect_database, create_session, finish_session

_STOP: Final = object()


class PositionWriter:
    """Write callback data to SQLite away from the input listener thread."""

    def __init__(self, database_path: str | Path, session_id: int) -> None:
        self.database_path = Path(database_path).expanduser()
        self.session_id = session_id
        # Blocking a producer briefly is preferable to silently losing positions.
        self.queue: queue.Queue[tuple[int, int, int] | object] = queue.Queue(
            maxsize=100_000
        )
        self.error: BaseException | None = None
        self.points_written = 0
        self.thread = threading.Thread(
            target=self._run,
            name="mouse-position-writer",
            daemon=False,
        )

    def start(self) -> None:
        self.thread.start()

    def add(self, timestamp_ns: int, x: int, y: int) -> None:
        while self.error is None:
            try:
                self.queue.put((timestamp_ns, x, y), timeout=0.1)
                return
            except queue.Full:
                continue
        raise RuntimeError("Position writer is no longer running") from self.error

    def close(self) -> None:
        if self.thread.is_alive():
            self.queue.put(_STOP)
        self.thread.join()
        if self.error is not None:
            raise RuntimeError("Could not save mouse positions") from self.error

    def _run(self) -> None:
        connection: sqlite3.Connection | None = None
        batch: list[tuple[int, int, int, int]] = []
        try:
            connection = connect_database(self.database_path)
            while True:
                try:
                    item = self.queue.get(timeout=0.25)
                except queue.Empty:
                    item = None

                if item is _STOP:
                    if batch:
                        self._flush(connection, batch)
                    break
                if item is not None:
                    timestamp_ns, x, y = item
                    batch.append((self.session_id, timestamp_ns, x, y))
                if len(batch) >= 500 or (item is None and batch):
                    self._flush(connection, batch)
        except BaseException as exc:  # Preserve writer failures for the main thread.
            self.error = exc
        finally:
            if connection is not None:
                connection.close()

    def _flush(
        self,
        connection: sqlite3.Connection,
        batch: list[tuple[int, int, int, int]],
    ) -> None:
        connection.executemany(
            """
            INSERT INTO positions (session_id, timestamp_ns, x, y)
            VALUES (?, ?, ?, ?)
            """,
            batch,
        )
        connection.commit()
        self.points_written += len(batch)
        batch.clear()


def record_positions(
    database_path: str | Path,
    *,
    sample_interval_ms: float = 0,
    duration_seconds: float | None = None,
    label: str | None = None,
) -> tuple[int, int]:
    """Record global mouse positions until interrupted or duration expires.

    A sample interval of zero records every movement event delivered by the OS.
    Returns the session id and number of saved points.
    """
    if not math.isfinite(sample_interval_ms) or sample_interval_ms < 0:
        raise ValueError("sample_interval_ms must be finite and cannot be negative")
    if duration_seconds is not None and (
        not math.isfinite(duration_seconds) or duration_seconds <= 0
    ):
        raise ValueError("duration_seconds must be finite and positive")

    try:
        from pynput import mouse
    except (ImportError, OSError) as exc:
        raise RuntimeError(
            "Global mouse support is unavailable. Install the project dependencies "
            "and make sure a graphical desktop session is active."
        ) from exc

    database_path = Path(database_path).expanduser()
    connection = connect_database(database_path)
    session_id = create_session(connection, sample_interval_ms, label)
    writer = PositionWriter(database_path, session_id)
    try:
        writer.start()
    except BaseException:
        finish_session(connection, session_id)
        connection.close()
        raise

    minimum_interval_ns = int(sample_interval_ms * 1_000_000)
    last_sample_monotonic_ns = 0

    def on_move(x: int, y: int) -> None:
        nonlocal last_sample_monotonic_ns
        monotonic_ns = time.monotonic_ns()
        if (
            minimum_interval_ns
            and monotonic_ns - last_sample_monotonic_ns < minimum_interval_ns
        ):
            return
        last_sample_monotonic_ns = monotonic_ns
        writer.add(time.time_ns(), int(x), int(y))

    listener = None
    pending_error: BaseException | None = None
    stop_requested = threading.Event()
    previous_signal_handlers: dict[int, object] = {}

    def request_stop(_signum, _frame) -> None:
        stop_requested.set()

    for signal_name in ("SIGTERM", "SIGHUP"):
        signal_number = getattr(signal, signal_name, None)
        if signal_number is None:
            continue
        try:
            previous_signal_handlers[signal_number] = signal.getsignal(signal_number)
            signal.signal(signal_number, request_stop)
        except ValueError:
            # Signal handlers can only be installed from the main thread.
            previous_signal_handlers.clear()
            break

    started = time.monotonic()
    try:
        listener = mouse.Listener(on_move=on_move)
        listener.start()
        # Capture the initial location even if the mouse remains still.
        initial_x, initial_y = mouse.Controller().position
        on_move(initial_x, initial_y)
        while listener.is_alive() and not stop_requested.is_set():
            if writer.error is not None:
                raise RuntimeError("The position writer stopped unexpectedly") from writer.error
            if duration_seconds is not None:
                remaining = duration_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    break
                time.sleep(min(0.2, remaining))
            else:
                time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    except BaseException as exc:
        pending_error = exc
    finally:
        for signal_number, previous_handler in previous_signal_handlers.items():
            signal.signal(signal_number, previous_handler)
        if listener is not None:
            listener.stop()
            try:
                listener.join()
            except BaseException as exc:
                pending_error = pending_error or exc
        try:
            writer.close()
        except BaseException as exc:
            pending_error = pending_error or exc
        try:
            finish_session(connection, session_id)
        finally:
            connection.close()

    if pending_error is not None:
        raise RuntimeError("Mouse recording stopped unexpectedly") from pending_error
    return session_id, writer.points_written
