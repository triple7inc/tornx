from chalk import chalk,COLORS

import argparse
import hashlib
import json
import math
import mmap
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


TIMESTAMP_KEY = re.compile(rb'"([0-9]{9,})"\s*:\s*\[')
GRAPH_WINDOW = re.compile(r"^([1-9][0-9]*)([mhdw])$")
PROGRESS_BYTES = 128 * 1024 * 1024
GRAPH_MAX_POINTS = 20_000


def debug(enabled, message, color=COLORS.cyan):
    if enabled:
        print(chalk(f"[debug] {message}", color=color), file=sys.stderr)


def format_date(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def parse_graph_window(value):
    value = value.strip().lower()
    if value in {"all", "every", "snapshot", "snapshots"}:
        return 0
    match = GRAPH_WINDOW.fullmatch(value)
    if match is None:
        raise argparse.ArgumentTypeError(
            "graph window must be all or a duration such as 15m, 1h, 1d, or 1w"
        )
    amount = int(match.group(1))
    multiplier = {"m": 60, "h": 3600, "d": 86400, "w": 604800}[
        match.group(2)
    ]
    return amount * multiplier


def format_window(seconds):
    if seconds == 0:
        return "every snapshot"
    for unit_seconds, suffix in ((604800, "w"), (86400, "d"), (3600, "h"), (60, "m")):
        if seconds % unit_seconds == 0:
            return f"{seconds // unit_seconds}{suffix}"
    return f"{seconds}s"


class GraphData:
    def __init__(self, file_size, max_points, window_seconds, debug_enabled):
        self.file_size = file_size
        self.max_points = max_points
        self.window_seconds = window_seconds
        self.debug_enabled = debug_enabled
        self.stride = None if window_seconds is None else 1
        self.seen = 0
        self.last_bucket = None
        self.timestamps = []
        self.prices = {}
        if window_seconds is not None:
            debug(
                debug_enabled,
                f"Graph resolution: {format_window(window_seconds)}",
            )

    def configure(self, record_size):
        estimated_records = max(1, math.ceil(self.file_size / max(record_size, 1)))
        self.stride = max(1, math.ceil(estimated_records / self.max_points))
        debug(
            self.debug_enabled,
            f"Graph sampling every {self.stride:,} snapshot(s) "
            f"(~{min(estimated_records, self.max_points):,} points per stock)",
        )

    def add(self, timestamp, value_start, value_end, mapped, force=False):
        index = self.seen
        self.seen += 1
        if self.window_seconds is None:
            if not force and index % self.stride:
                return
        elif self.window_seconds:
            bucket = timestamp // self.window_seconds
            if bucket == self.last_bucket:
                return
            self.last_bucket = bucket
        if self.timestamps and self.timestamps[-1] == timestamp:
            return

        snapshot = json.loads(mapped[value_start:value_end])
        if not isinstance(snapshot, list):
            raise ValueError(f"Stock snapshot {timestamp} is not a list")

        old_length = len(self.timestamps)
        for values in self.prices.values():
            values.append(None)
        for stock in snapshot:
            if not isinstance(stock, dict) or "id" not in stock or "price" not in stock:
                raise ValueError(f"Malformed stock entry in snapshot {timestamp}")
            symbol = str(stock["id"])
            try:
                price = float(stock["price"])
            except (TypeError, ValueError) as error:
                raise ValueError(
                    f"Invalid price for {symbol} in snapshot {timestamp}"
                ) from error
            if symbol not in self.prices:
                self.prices[symbol] = [None] * old_length + [price]
            else:
                self.prices[symbol][-1] = price
        self.timestamps.append(timestamp)

    def show(self):
        if not self.timestamps:
            raise ValueError("No stock snapshots were available for the graph")

        import matplotlib
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
        import numpy as np
        from matplotlib.widgets import CheckButtons

        order = sorted(range(len(self.timestamps)), key=self.timestamps.__getitem__)
        timestamps = np.asarray(
            [self.timestamps[index] for index in order], dtype=np.float64
        )
        dates = timestamps / 86400.0
        first_date = format_date(int(timestamps[0]))
        last_date = format_date(int(timestamps[-1]))
        resolution = (
            format_window(self.window_seconds)
            if self.window_seconds is not None
            else f"{len(timestamps):,} evenly sampled snapshots"
        )

        matplotlib.rcParams["agg.path.chunksize"] = 10_000
        plt.style.use("dark_background")
        figure, axes = plt.subplots(figsize=(15, 9))
        figure.subplots_adjust(left=0.07, right=0.82, top=0.90, bottom=0.10)
        try:
            figure.canvas.manager.set_window_title("Stock Price History")
        except AttributeError:
            pass

        lines = {}
        for symbol, values in sorted(self.prices.items()):
            prices = np.asarray([values[index] for index in order], dtype=np.float64)
            line, = axes.plot(dates, prices, linewidth=0.8, label=symbol)
            lines[symbol] = line

        locator = mdates.AutoDateLocator(minticks=4, maxticks=12)
        axes.xaxis.set_major_locator(locator)
        axes.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        axes.set_title(
            f"Stock prices: {first_date} - {last_date}\n{resolution}"
        )
        axes.set_xlabel("Date (UTC)")
        axes.set_ylabel("Price")
        axes.grid(True, alpha=0.2)
        axes.margins(x=0)

        selector_axes = figure.add_axes((0.835, 0.08, 0.15, 0.82))
        symbols = list(lines)
        selector = CheckButtons(selector_axes, symbols, [True] * len(symbols))
        selector_axes.set_title("Stocks (click to toggle)", fontsize=9)
        for label in selector.labels:
            label.set_fontsize(8)

        def toggle(symbol):
            line = lines[symbol]
            line.set_visible(not line.get_visible())
            figure.canvas.draw_idle()

        def zoom(event):
            if event.inaxes is not axes or event.xdata is None or event.ydata is None:
                return
            scale = 0.8 if event.button == "up" else 1.25
            x_min, x_max = axes.get_xlim()
            y_min, y_max = axes.get_ylim()
            axes.set_xlim(
                event.xdata - (event.xdata - x_min) * scale,
                event.xdata + (x_max - event.xdata) * scale,
            )
            axes.set_ylim(
                event.ydata - (event.ydata - y_min) * scale,
                event.ydata + (y_max - event.ydata) * scale,
            )
            figure.canvas.draw_idle()

        selector.on_clicked(toggle)
        figure.canvas.mpl_connect("scroll_event", zoom)
        plt.show()
        return len(timestamps), len(lines)

def find_earliest_run(
    cache_path,
    minimum_run,
    before_timestamp,
    debug_enabled=False,
    graph_enabled=False,
    graph_max_points=GRAPH_MAX_POINTS,
    graph_window=None,
):
    file_size = cache_path.stat().st_size
    debug(
        debug_enabled,
        f"Scanning {cache_path} ({file_size / (1024 * 1024):,.1f} MiB)",
    )
    debug(
        debug_enabled,
        f"Considering snapshots before {format_date(before_timestamp)}",
    )

    best = None
    current_digest = None
    current_count = 0
    current_first = None
    current_last = None
    records = 0
    next_progress = PROGRESS_BYTES
    started = time.perf_counter()
    graph_data = (
        GraphData(file_size, graph_max_points, graph_window, debug_enabled)
        if graph_enabled
        else None
    )

    def finish_run():
        nonlocal best
        if current_count >= minimum_run:
            run_start = min(current_first, current_last)
            if best is None or run_start < min(best[1], best[2]):
                best = (current_count, current_first, current_last)
            debug(
                debug_enabled,
                f"Repeated run: {current_count:,} records, "
                f"{format_date(min(current_first, current_last))} to "
                f"{format_date(max(current_first, current_last))}",
                COLORS.yellow,
            )

    def consume(timestamp, value_start, value_end, view):
        nonlocal current_digest, current_count, current_first, current_last, records
        digest = (
            value_end - value_start,
            hashlib.blake2b(view[value_start:value_end], digest_size=16).digest(),
        )
        records += 1
        if digest == current_digest:
            current_count += 1
            current_last = timestamp
        else:
            finish_run()
            current_digest = digest
            current_count = 1
            current_first = timestamp
            current_last = timestamp

    def process_snapshot(
        timestamp, value_start, value_end, mapped, view, record_size, force=False
    ):
        if graph_data is not None:
            if graph_data.stride is None:
                graph_data.configure(record_size)
            graph_data.add(
                timestamp, value_start, value_end, mapped, force=force
            )
        if timestamp < before_timestamp:
            consume(timestamp, value_start, value_end, view)

    with cache_path.open("rb", buffering=0) as cache_file:
        if file_size == 0:
            return None, 0, 0.0, graph_data
        with mmap.mmap(cache_file.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            view = memoryview(mapped)
            try:
                previous_match = None
                for match in TIMESTAMP_KEY.finditer(mapped):
                    if previous_match is not None:
                        value_end = mapped.rfind(
                            b"]", previous_match.end() - 1, match.start()
                        )
                        if value_end < 0:
                            raise ValueError("Malformed stock snapshot in cache")
                        timestamp = int(previous_match.group(1))
                        process_snapshot(
                            timestamp,
                            previous_match.end() - 1,
                            value_end + 1,
                            mapped,
                            view,
                            match.start() - previous_match.start(),
                        )
                    previous_match = match

                    if match.start() >= next_progress:
                        elapsed = max(time.perf_counter() - started, 0.001)
                        percent = match.start() * 100 / file_size
                        speed = match.start() / (1024 * 1024) / elapsed
                        debug(
                            debug_enabled,
                            f"{percent:5.1f}% - {records:,} records - "
                            f"{speed:,.1f} MiB/s",
                        )
                        while next_progress <= match.start():
                            next_progress += PROGRESS_BYTES

                if previous_match is not None:
                    value_end = mapped.rfind(b"]", previous_match.end() - 1)
                    if value_end < 0:
                        raise ValueError("Malformed final stock snapshot in cache")
                    timestamp = int(previous_match.group(1))
                    process_snapshot(
                        timestamp,
                        previous_match.end() - 1,
                        value_end + 1,
                        mapped,
                        view,
                        file_size - previous_match.start(),
                        force=True,
                    )
                finish_run()
            finally:
                view.release()

    return best, records, time.perf_counter() - started, graph_data


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Find the earliest sequence of consecutive identical stock-price snapshots."
        )
    )
    parser.add_argument(
        "cache",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().with_name("stocks_cache.json"),
        help="cache file to scan (default: stocks_cache.json beside this script)",
    )
    parser.add_argument(
        "--minimum-run",
        type=int,
        default=3,
        help="minimum number of equal snapshots considered a repeated run (default: 3)",
    )
    parser.add_argument(
        "--before-year",
        type=int,
        default=2005,
        help="only search snapshots before January 1 of this year (default: 2005)",
    )
    parser.add_argument(
        "--graph",
        action="store_true",
        help="open a zoomable interactive stock graph window",
    )
    parser.add_argument(
        "--graph-points",
        type=int,
        default=GRAPH_MAX_POINTS,
        help="maximum sampled snapshots per stock in the graph (default: 20000)",
    )
    parser.add_argument(
        "--graph-window",
        nargs="?",
        const=-1,
        type=parse_graph_window,
        metavar="WINDOW",
        help=(
            "open the graph window; optionally use one closing snapshot per "
            "15m, 1h, 1d, or 1w window, or 'all' for every snapshot"
        ),
    )
    parser.add_argument("--debug", action="store_true", help="show scan progress")
    args = parser.parse_args()

    if args.minimum_run < 2:
        parser.error("--minimum-run must be at least 2")
    if not 1971 <= args.before_year <= 9999:
        parser.error("--before-year must be between 1971 and 9999")
    if args.graph_points < 100:
        parser.error("--graph-points must be at least 100")
    if not args.cache.is_file():
        parser.error(f"cache file does not exist: {args.cache}")

    try:
        graph_enabled = args.graph or args.graph_window is not None
        graph_window = None if args.graph_window == -1 else args.graph_window
        before_timestamp = int(
            datetime(args.before_year, 1, 1, tzinfo=timezone.utc).timestamp()
        )
        best, records, elapsed, graph_data = find_earliest_run(
            args.cache,
            args.minimum_run,
            before_timestamp,
            args.debug,
            graph_enabled,
            args.graph_points,
            graph_window,
        )
    except (OSError, ValueError) as error:
        print(chalk(f"Error: {error}", color=COLORS.red), file=sys.stderr)
        return 1

    debug(
        args.debug,
        f"Finished {records:,} records in {elapsed:,.2f}s",
        COLORS.green,
    )
    if best is None or best[0] < args.minimum_run:
        print(
            chalk(
                f"No sequence of at least {args.minimum_run} identical snapshots found.",
                color=COLORS.yellow,
            )
        )
    else:
        count, first, last = best
        start, end = sorted((first, last))
        print(chalk("Earliest repeated-price sequence", color=COLORS.green))
        print(f"Search:      before {args.before_year}")
        print(f"Date:        {format_date(start)} ({start})")
        print(f"Through:     {format_date(end)} ({end})")
        print(f"Snapshots:   {count:,}")
        print(f"Time span:   {(end - start) / 3600:,.2f} hours")

    if graph_data is not None:
        print(chalk("Opening interactive graph window...", color=COLORS.green))
        try:
            points, stocks = graph_data.show()
            debug(
                args.debug,
                f"Displayed {stocks:,} stocks using {points:,} snapshots",
                COLORS.green,
            )
        except (ImportError, RuntimeError, ValueError) as error:
            print(chalk(f"Could not open graph: {error}", color=COLORS.red))
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
