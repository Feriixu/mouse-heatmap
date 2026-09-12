"""Statistics, CSV export, and heatmap generation."""

from __future__ import annotations

import csv
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence, TextIO

from .storage import iter_positions, open_database, position_bounds


@dataclass(frozen=True)
class MovementStats:
    point_count: int
    session_count: int
    duration_seconds: float
    active_seconds: float
    distance_pixels: float
    average_speed_px_s: float
    moving_speed_px_s: float
    maximum_speed_px_s: float
    min_x: int | None
    max_x: int | None
    min_y: int | None
    max_y: int | None


def calculate_stats(
    database_path: str | Path,
    session_ids: Sequence[int] | None = None,
    *,
    idle_gap_seconds: float = 1.0,
) -> MovementStats:
    """Calculate statistics, treating gaps over a threshold as inactive time."""
    if not math.isfinite(idle_gap_seconds) or idle_gap_seconds <= 0:
        raise ValueError("idle_gap_seconds must be finite and positive")
    idle_gap_ns = int(idle_gap_seconds * 1_000_000_000)
    connection = open_database(database_path)
    count = 0
    session_set: set[int] = set()
    session_first_timestamp: int | None = None
    session_last_timestamp: int | None = None
    total_duration_ns = 0
    previous: tuple[int, int, int, int] | None = None
    distance = 0.0
    active_distance = 0.0
    active_ns = 0
    maximum_speed = 0.0
    min_x = max_x = min_y = max_y = None

    try:
        for session_id, timestamp_ns, x, y in iter_positions(connection, session_ids):
            count += 1
            session_set.add(session_id)
            if previous is None or previous[0] != session_id:
                if (
                    session_first_timestamp is not None
                    and session_last_timestamp is not None
                ):
                    total_duration_ns += (
                        session_last_timestamp - session_first_timestamp
                    )
                session_first_timestamp = timestamp_ns
            session_last_timestamp = timestamp_ns
            min_x = x if min_x is None else min(min_x, x)
            max_x = x if max_x is None else max(max_x, x)
            min_y = y if min_y is None else min(min_y, y)
            max_y = y if max_y is None else max(max_y, y)

            if previous is not None and previous[0] == session_id:
                elapsed_ns = timestamp_ns - previous[1]
                segment = math.hypot(x - previous[2], y - previous[3])
                distance += segment
                if 0 < elapsed_ns <= idle_gap_ns and segment > 0:
                    active_ns += elapsed_ns
                    active_distance += segment
                    maximum_speed = max(
                        maximum_speed,
                        segment / (elapsed_ns / 1_000_000_000),
                    )
            previous = (session_id, timestamp_ns, x, y)
    finally:
        connection.close()

    if session_first_timestamp is not None and session_last_timestamp is not None:
        total_duration_ns += session_last_timestamp - session_first_timestamp
    duration = total_duration_ns / 1_000_000_000
    active = active_ns / 1_000_000_000
    return MovementStats(
        point_count=count,
        session_count=len(session_set),
        duration_seconds=duration,
        active_seconds=active,
        distance_pixels=distance,
        average_speed_px_s=distance / duration if duration else 0.0,
        moving_speed_px_s=active_distance / active if active else 0.0,
        maximum_speed_px_s=maximum_speed,
        min_x=min_x,
        max_x=max_x,
        min_y=min_y,
        max_y=max_y,
    )


def export_csv(
    database_path: str | Path,
    output: str | Path | TextIO,
    session_ids: Sequence[int] | None = None,
) -> int:
    """Export selected positions as CSV and return the number of rows written."""
    database = Path(database_path).expanduser().resolve()
    connection = open_database(database)
    should_close = not hasattr(output, "write")
    destination: Path | None = None
    temporary_path: Path | None = None
    if should_close:
        destination = Path(output).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination == database or (
            destination.exists() and os.path.samefile(destination, database)
        ):
            connection.close()
            raise ValueError("CSV output must not overwrite the recording database")
        temporary = tempfile.NamedTemporaryFile(
            mode="w",
            newline="",
            encoding="utf-8",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        )
        stream = temporary
        temporary_path = Path(temporary.name)
    else:
        stream = output

    count = 0
    succeeded = False
    try:
        writer = csv.writer(stream)
        writer.writerow(("session_id", "timestamp_ns", "timestamp_seconds", "x", "y"))
        for session_id, timestamp_ns, x, y in iter_positions(connection, session_ids):
            writer.writerow(
                (session_id, timestamp_ns, f"{timestamp_ns / 1_000_000_000:.9f}", x, y)
            )
            count += 1
        succeeded = True
    finally:
        connection.close()
        if should_close:
            stream.close()
            if not succeeded and temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
    if destination is not None and temporary_path is not None:
        os.replace(temporary_path, destination)
    return count


def _gaussian_blur(data, sigma: float):
    """Apply a small dependency-free (apart from NumPy) separable blur."""
    import numpy as np

    if sigma <= 0:
        return data
    radius = max(1, int(math.ceil(3 * sigma)))
    coordinates = np.arange(-radius, radius + 1, dtype=float)
    kernel = np.exp(-(coordinates**2) / (2 * sigma**2))
    kernel /= kernel.sum()

    def blur_axis(values, axis: int):
        padding = [(0, 0)] * values.ndim
        padding[axis] = (radius, radius)
        padded = np.pad(values, padding, mode="edge")
        return np.apply_along_axis(
            lambda row: np.convolve(row, kernel, mode="valid"),
            axis,
            padded,
        )

    return blur_axis(blur_axis(data, 0), 1)


def create_heatmap(
    database_path: str | Path,
    output_path: str | Path,
    *,
    session_ids: Sequence[int] | None = None,
    bins: int = 160,
    smoothing: float = 1.5,
    colormap: str = "inferno",
    title: str | None = None,
    show: bool = False,
) -> int:
    """Render selected positions to a PNG (or another Matplotlib format)."""
    if bins < 10:
        raise ValueError("bins must be at least 10")
    if not math.isfinite(smoothing) or smoothing < 0:
        raise ValueError("smoothing must be finite and cannot be negative")

    database = Path(database_path).expanduser().resolve()
    destination = Path(output_path).expanduser().resolve()
    if destination == database or (
        destination.exists() and database.exists() and os.path.samefile(destination, database)
    ):
        raise ValueError("Heatmap output must not overwrite the recording database")

    import matplotlib.pyplot as plt
    import numpy as np

    connection = open_database(database)
    try:
        connection.execute("BEGIN")
        count, min_x, max_x, min_y, max_y = position_bounds(connection, session_ids)
        if not count:
            raise ValueError("No recorded positions matched the selection")
        plot_min_x, plot_max_x = float(min_x), float(max_x)
        plot_min_y, plot_max_y = float(min_y), float(max_y)
        if plot_min_x == plot_max_x:
            plot_min_x -= 0.5
            plot_max_x += 0.5
        if plot_min_y == plot_max_y:
            plot_min_y -= 0.5
            plot_max_y += 0.5

        histogram_range = (
            (plot_min_x, plot_max_x),
            (plot_min_y, plot_max_y),
        )
        density = np.zeros((bins, bins), dtype=float)
        x_values: list[int] = []
        y_values: list[int] = []
        x_edges = y_edges = None
        for _, _, x, y in iter_positions(connection, session_ids):
            x_values.append(x)
            y_values.append(y)
            if len(x_values) >= 50_000:
                partial, x_edges, y_edges = np.histogram2d(
                    x_values, y_values, bins=bins, range=histogram_range
                )
                density += partial
                x_values.clear()
                y_values.clear()
        if x_values:
            partial, x_edges, y_edges = np.histogram2d(
                x_values, y_values, bins=bins, range=histogram_range
            )
            density += partial
    finally:
        connection.close()
    density = _gaussian_blur(density, smoothing)

    figure, axis = plt.subplots(figsize=(12, 7), constrained_layout=True)
    image = axis.imshow(
        density.T,
        origin="lower",
        extent=(x_edges[0], x_edges[-1], y_edges[0], y_edges[-1]),
        aspect="equal",
        interpolation="bilinear",
        cmap=colormap,
    )
    axis.invert_yaxis()  # Screen coordinates increase from top to bottom.
    axis.set_xlabel("Screen X (pixels)")
    axis.set_ylabel("Screen Y (pixels)")
    axis.set_title(title or f"Mouse position heatmap ({count:,} samples)")
    colorbar = figure.colorbar(image, ax=axis, shrink=0.85)
    colorbar.set_label("Recorded movement-event density")

    destination.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(destination, dpi=180)
    if show:
        plt.show()
    plt.close(figure)
    return count
