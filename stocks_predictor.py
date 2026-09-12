from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import argparse,json,math,subprocess,sys,threading,time
from chalk import chalk,COLORS
from datetime import datetime
from pathlib import Path
try:
    import numpy as np
except ImportError as error:
    raise SystemExit(
        "NumPy is required. Install it with: python -m pip install numpy"
    ) from error


REFRESH_DAYS = 30
INTERVAL = 15 * 60
LAGS = (1, 2, 4, 8, 16, 32, 96)
MEAN_WINDOWS = (4, 16, 96)
VOLATILITY_WINDOWS = (16, 96)
LOOKBACK = max(LAGS + MEAN_WINDOWS + VOLATILITY_WINDOWS)
MIN_EVENT_ROWS = 24
MIN_TRAINING_ROWS = MIN_EVENT_ROWS
MARKET_MOVE_FRACTION = 0.30
MOVEMENT_DECAY = 0.99
RIDGE_ALPHAS = (1_000.0, 10_000.0, 100_000.0, 1_000_000.0)
APP_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
DEFAULT_CACHE_FILE = APP_DIR / "stocks_cache.json"


def nonnegative_integer(value):
    value = int(value)
    if value < 0:
        raise argparse.ArgumentTypeError("value must be zero or greater")
    return value


def positive_integer(value):
    value = int(value)
    if value < 1:
        raise argparse.ArgumentTypeError("value must be at least one")
    return value


def server_port(value):
    value = int(value)
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return value


def stock_watch_list(value):
    stocks = frozenset(
        stock.strip().casefold()
        for stock in value.split(",")
        if stock.strip()
    )
    if not stocks:
        raise argparse.ArgumentTypeError(
            "--watch must contain at least one stock name"
        )
    return stocks


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Refresh Torn stock history and produce machine-learned "
            "15-minute BUY/SELL forecasts."
        )
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=DEFAULT_CACHE_FILE,
        help=(
            "stocks_cache.json path "
            "(default: the file beside this program)"
        ),
    )
    parser.add_argument(
        "--days",
        type=positive_integer,
        default=REFRESH_DAYS,
        help="calendar days requested from stocks_cacher (default: 30)",
    )
    parser.add_argument(
        "--skip",
        "--skip-refresh",
        dest="skip",
        action="store_true",
        help="read the existing cache without running stocks_cacher first",
    )
    parser.add_argument(
        "--top",
        default=10,
        type=nonnegative_integer,
        help="number of strongest forecasts to show; 0 shows every stock",
    )
    parser.add_argument(
        "--sort",
        choices=("probability", "roi", "price"),
        default="probability",
        help=(
            "sort descending by probability, predicted ROI, or current price "
            "(default: probability)"
        ),
    )
    parser.add_argument(
        "--watch",
        type=stock_watch_list,
        default=frozenset(),
        metavar="STOCKS",
        help=(
            "comma-separated stock names to highlight in yellow "
            "(case-insensitive; example: tci,ewm)"
        ),
    )
    parser.add_argument(
        "--server",
        action="store_true",
        help=(
            "serve continuously refreshed predictions as JSON on all "
            "network interfaces and redraw the console every 15 minutes"
        ),
    )
    parser.add_argument(
        "--port",
        type=server_port,
        default=8676,
        help="HTTP port used by --server (default: 8676)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="write machine-readable prediction JSON",
    )
    return parser.parse_args()


def cacher_command(cache_file):
    directory = cache_file.resolve().parent
    executable_names = (
        ("stocks_cacher.exe", "stocks_cacher")
        if sys.platform == "win32"
        else ("stocks_cacher", "stocks_cacher.exe")
    )
    for name in executable_names:
        executable = directory / name
        if executable.is_file():
            return [str(executable)]

    source = directory / "stocks_cacher.py"
    if source.is_file() and not getattr(sys, "frozen", False):
        return [sys.executable, str(source)]
    return None


def refresh_cache(cache_file, days, quiet=False):
    command = cacher_command(cache_file)
    if command is None:
        if cache_file.is_file():
            print(
                "Warning: stocks_cacher was not found beside the cache; "
                "using the existing cache.",
                file=sys.stderr,
            )
            return
        raise FileNotFoundError(
            f"stocks_cacher and {cache_file.name} were not found in "
            f"{cache_file.resolve().parent}"
        )

    command.extend(("--days", str(days), "--sort"))
    if not quiet:
        print(f"Refreshing {days} days of stock history; waiting for cacher...")
    completed = subprocess.run(
        command,
        capture_output=quiet,
        text=quiet,
        check=False,
    )
    if completed.returncode == 0:
        return

    detail = ""
    if quiet:
        detail = (completed.stderr or completed.stdout or "").strip()
    message = (
        f"stocks_cacher exited with code {completed.returncode}"
        + (f": {detail}" if detail else "")
    )
    if cache_file.is_file():
        print(f"Warning: {message}; using the existing cache.", file=sys.stderr)
        return
    raise RuntimeError(message)


def load_cache(cache_file):
    try:
        with cache_file.open("r", encoding="utf-8") as file:
            raw_cache = json.load(file)
    except json.JSONDecodeError as error:
        raise ValueError(f"{cache_file} is not valid JSON: {error}") from error
    if not isinstance(raw_cache, dict):
        raise ValueError("stocks_cache.json must contain an object keyed by timestamp")

    snapshots = []
    for raw_timestamp, raw_stocks in raw_cache.items():
        try:
            timestamp = int(raw_timestamp)
        except (TypeError, ValueError):
            continue
        if timestamp % INTERVAL or not isinstance(raw_stocks, list):
            continue

        stocks = {}
        for stock in raw_stocks:
            if not isinstance(stock, dict):
                continue
            stock_id = stock.get("id")
            price = stock.get("price")
            if not isinstance(stock_id, str) or not stock_id:
                continue
            try:
                price = float(price)
            except (TypeError, ValueError):
                continue
            if math.isfinite(price) and price > 0:
                stocks[stock_id] = price
        if stocks:
            snapshots.append((timestamp, stocks))

    snapshots.sort(key=lambda snapshot: snapshot[0])
    if len(snapshots) < LOOKBACK + MIN_TRAINING_ROWS + 2:
        raise ValueError(
            "not enough valid 15-minute snapshots: "
            f"found {len(snapshots)}, need at least "
            f"{LOOKBACK + MIN_TRAINING_ROWS + 2}"
        )
    return snapshots


def price_matrix(snapshots):
    latest_stocks = set(snapshots[-1][1])
    minimum_coverage = max(
        LOOKBACK + MIN_TRAINING_ROWS + 2,
        int(len(snapshots) * 0.70),
    )
    counts = {}
    for _, stocks in snapshots:
        for stock_id in stocks:
            counts[stock_id] = counts.get(stock_id, 0) + 1
    stock_ids = sorted(
        stock_id
        for stock_id in latest_stocks
        if counts.get(stock_id, 0) >= minimum_coverage
    )
    if not stock_ids:
        raise ValueError("no stocks have enough history to train a model")

    timestamps = np.asarray(
        [timestamp for timestamp, _ in snapshots],
        dtype=np.int64,
    )
    prices = np.full((len(snapshots), len(stock_ids)), np.nan, dtype=np.float64)
    indexes = {stock_id: index for index, stock_id in enumerate(stock_ids)}
    for row, (_, stocks) in enumerate(snapshots):
        for stock_id, price in stocks.items():
            column = indexes.get(stock_id)
            if column is not None:
                prices[row, column] = price

    first_complete = max(
        int(np.flatnonzero(np.isfinite(prices[:, column]))[0])
        for column in range(prices.shape[1])
    )
    row_numbers = np.arange(len(timestamps))
    for column in range(prices.shape[1]):
        known = np.isfinite(prices[:, column])
        previous = np.maximum.accumulate(np.where(known, row_numbers, -1))
        fillable = previous >= 0
        prices[fillable, column] = prices[previous[fillable], column]
    return timestamps[first_complete:], stock_ids, prices[first_complete:]


def feature_rows(timestamps, log_returns, base_rows):
    safe_returns = np.nan_to_num(log_returns, nan=0.0)
    prefix = np.vstack(
        (
            np.zeros((1, safe_returns.shape[1]), dtype=np.float64),
            np.cumsum(safe_returns, axis=0),
        )
    )
    squared_prefix = np.vstack(
        (
            np.zeros((1, safe_returns.shape[1]), dtype=np.float64),
            np.cumsum(safe_returns * safe_returns, axis=0),
        )
    )

    features = []

    def add_feature_block(block):
        features.append(
            np.column_stack(
                (
                    np.mean(block, axis=1),
                    np.median(block, axis=1),
                    np.std(block, axis=1),
                    np.mean(block > 0.0, axis=1),
                    np.mean(np.abs(block) > 1e-12, axis=1),
                )
            )
        )

    for lag in LAGS:
        add_feature_block(safe_returns[base_rows - lag])
    for window in MEAN_WINDOWS:
        mean = (prefix[base_rows] - prefix[base_rows - window]) / window
        add_feature_block(mean)
    for window in VOLATILITY_WINDOWS:
        mean = (prefix[base_rows] - prefix[base_rows - window]) / window
        mean_square = (
            squared_prefix[base_rows] - squared_prefix[base_rows - window]
        ) / window
        add_feature_block(
            np.sqrt(np.maximum(mean_square - mean * mean, 0.0))
        )

    if int(timestamps[-1]) - int(timestamps[0]) >= 56 * 86400:
        seconds = timestamps[base_rows].astype(np.float64)
        daily_phase = 2.0 * np.pi * (seconds % 86400.0) / 86400.0
        weekly_phase = 2.0 * np.pi * (seconds % 604800.0) / 604800.0
        features.append(
            np.column_stack(
                (
                    np.sin(daily_phase),
                    np.cos(daily_phase),
                    np.sin(weekly_phase),
                    np.cos(weekly_phase),
                )
            )
        )
    return np.hstack(features)


def prepare_training_data(timestamps, prices):
    log_prices = np.log(prices)
    log_returns = np.diff(log_prices, axis=0)
    continuous = np.diff(timestamps) == INTERVAL
    log_returns[~continuous] = np.nan
    stock_moves = np.abs(np.nan_to_num(log_returns, nan=0.0)) > 1e-12
    market_moves = np.mean(stock_moves, axis=1) >= MARKET_MOVE_FRACTION

    candidate_rows = np.arange(LOOKBACK, len(timestamps) - 1)
    bad_prefix = np.concatenate(([0], np.cumsum(~continuous)))
    continuous_rows = (
        bad_prefix[candidate_rows + 1]
        - bad_prefix[candidate_rows - LOOKBACK]
        == 0
    )
    event_rows = candidate_rows[continuous_rows & market_moves[candidate_rows]]
    if len(event_rows) < MIN_EVENT_ROWS:
        raise ValueError(
            "not enough independent market-movement windows: "
            f"found {len(event_rows)}, need at least {MIN_EVENT_ROWS}"
        )

    latest_row = len(timestamps) - 1
    if not np.all(continuous[latest_row - LOOKBACK : latest_row]):
        raise ValueError(
            f"the latest prediction needs {LOOKBACK} consecutive "
            "15-minute snapshots"
        )

    x = feature_rows(timestamps, log_returns, event_rows)
    y = log_returns[event_rows]
    latest_x = feature_rows(
        timestamps,
        log_returns,
        np.asarray([latest_row]),
    )
    return (
        x,
        y,
        latest_x,
        event_rows,
        market_moves,
        stock_moves,
        continuous,
    )


def cache_needs_bootstrap(cache_file):
    try:
        snapshots = load_cache(cache_file)
        timestamps, _, prices = price_matrix(snapshots)
        prepare_training_data(timestamps, prices)
    except (OSError, ValueError):
        return True
    return False


def standardized_design(x, mean=None, scale=None):
    if mean is None:
        mean = np.mean(x, axis=0)
        scale = np.std(x, axis=0)
        scale[scale < 1e-12] = 1.0
    standardized = (x - mean) / scale
    design = np.column_stack((np.ones(len(x)), standardized))
    return design, mean, scale


def ridge_coefficients(design, targets, alpha):
    gram = design.T @ design
    penalty = np.eye(gram.shape[0], dtype=np.float64) * alpha
    penalty[0, 0] = 0.0
    try:
        return np.linalg.solve(gram + penalty, design.T @ targets)
    except np.linalg.LinAlgError:
        return np.linalg.lstsq(gram + penalty, design.T @ targets, rcond=None)[0]


def train_and_predict(x, y, latest_x):
    validation_size = max(8, int(round(len(x) * 0.25)))
    validation_size = min(validation_size, len(x) - 16)
    split = len(x) - validation_size
    outer_train_x, validation_x = x[:split], x[split:]
    outer_train_y, validation_y = y[:split], y[split:]

    inner_validation_size = max(6, int(round(len(outer_train_x) * 0.25)))
    inner_validation_size = min(
        inner_validation_size,
        len(outer_train_x) - 10,
    )
    inner_split = len(outer_train_x) - inner_validation_size
    inner_train_x = outer_train_x[:inner_split]
    inner_train_y = outer_train_y[:inner_split]
    inner_validation_x = outer_train_x[inner_split:]
    inner_validation_y = outer_train_y[inner_split:]

    inner_design, inner_mean, inner_scale = standardized_design(inner_train_x)
    inner_validation_design, _, _ = standardized_design(
        inner_validation_x,
        inner_mean,
        inner_scale,
    )
    best = None
    for alpha in RIDGE_ALPHAS:
        coefficients = ridge_coefficients(
            inner_design,
            inner_train_y,
            alpha,
        )
        inner_prediction = inner_validation_design @ coefficients
        loss = float(
            np.mean((inner_validation_y - inner_prediction) ** 2)
        )
        if best is None or loss < best[0]:
            best = (loss, alpha)
    _, alpha = best

    outer_design, outer_mean, outer_scale = standardized_design(outer_train_x)
    outer_coefficients = ridge_coefficients(
        outer_design,
        outer_train_y,
        alpha,
    )
    validation_design, _, _ = standardized_design(
        validation_x,
        outer_mean,
        outer_scale,
    )
    validation_prediction = validation_design @ outer_coefficients

    full_design, full_mean, full_scale = standardized_design(x)
    full_coefficients = ridge_coefficients(full_design, y, alpha)
    latest_design, _, _ = standardized_design(latest_x, full_mean, full_scale)
    forecast = (latest_design @ full_coefficients)[0]

    historical_limit = np.quantile(np.abs(y), 0.995, axis=0) * 3.0
    forecast = np.clip(
        forecast,
        -np.maximum(historical_limit, 1e-6),
        np.maximum(historical_limit, 1e-6),
    )
    return forecast, validation_y, validation_prediction, alpha


def movement_probabilities(market_moves, stock_moves, continuous):
    valid_rows = np.flatnonzero(continuous)
    if len(valid_rows) < 2:
        stock_count = stock_moves.shape[1]
        return 0.5, np.full(stock_count, 0.5), np.full(stock_count, 0.5)

    adjacent = np.diff(valid_rows) == 1
    previous_rows = valid_rows[:-1][adjacent]
    next_rows = valid_rows[1:][adjacent]
    previous_states = market_moves[previous_rows]
    next_states = market_moves[next_rows]
    current_row = valid_rows[-1]
    ages = (current_row - next_rows).astype(np.float64)
    weights = MOVEMENT_DECAY**ages

    matching = previous_states == market_moves[current_row]
    matching_weight = weights[matching]
    markov_probability = (
        1.0 + np.sum(matching_weight * next_states[matching])
    ) / (2.0 + np.sum(matching_weight))
    base_probability = (
        1.0 + np.sum(weights * next_states)
    ) / (2.0 + np.sum(weights))
    market_probability = float(
        np.clip(
            0.80 * markov_probability + 0.20 * base_probability,
            0.01,
            0.99,
        )
    )

    event_rows = np.flatnonzero(market_moves & continuous)
    event_ages = (current_row - event_rows).astype(np.float64)
    event_weights = MOVEMENT_DECAY**event_ages
    participation = (
        1.0
        + np.sum(
            event_weights[:, None] * stock_moves[event_rows],
            axis=0,
        )
    ) / (2.0 + np.sum(event_weights))
    stock_probability = np.clip(
        market_probability * participation,
        0.005,
        0.99,
    )
    return market_probability, stock_probability, participation


def confidence_scores(
    validation_y,
    validation_prediction,
    stock_move_probability,
    event_count,
):
    stock_count = validation_y.shape[1]
    historical_accuracy = np.full(stock_count, 0.5, dtype=np.float64)
    direction_probability = np.full(stock_count, 0.5, dtype=np.float64)
    moving_observations = np.zeros(stock_count, dtype=np.int64)
    probability_cap = 0.02 + 0.08 * min(1.0, event_count / 200.0)

    for column in range(stock_count):
        moving = np.abs(validation_y[:, column]) > 1e-12
        observations = int(np.sum(moving))
        moving_observations[column] = observations
        if observations == 0:
            continue

        actual = validation_y[moving, column]
        predicted = validation_prediction[moving, column]
        hits = int(np.sum(np.sign(predicted) == np.sign(actual)))
        historical_accuracy[column] = hits / observations
        posterior = (hits + 10.0) / (observations + 20.0)
        direction_probability[column] = np.clip(
            posterior,
            0.5 - probability_cap,
            0.5 + probability_cap,
        )

    action_probability = np.clip(
        stock_move_probability * direction_probability,
        0.0,
        0.99,
    )
    return (
        action_probability,
        direction_probability,
        historical_accuracy,
        moving_observations,
        validation_y.shape[0],
    )


def make_predictions(
    timestamps,
    stock_ids,
    prices,
    conditional_forecast,
    action_probability,
    stock_move_probability,
    direction_probability,
    historical_accuracy,
    moving_observations,
    sort_by,
    watched_stocks,
):
    current_prices = prices[-1]
    target_timestamp = int(timestamps[-1]) + INTERVAL
    predictions = []
    for index, stock_id in enumerate(stock_ids):
        conditional_log_return = float(conditional_forecast[index])
        conditional_roi = math.expm1(conditional_log_return)
        expected_roi = (
            conditional_roi * float(stock_move_probability[index])
        )
        expected_price = current_prices[index] * (1.0 + expected_roi)
        roi_percent = expected_roi * 100.0
        conditional_roi_percent = conditional_roi * 100.0
        action = "BUY" if roi_percent >= 0.0 else "SELL"
        predictions.append(
            {
                "stock": stock_id,
                "action": action,
                "action_timestamp": int(timestamps[-1]),
                "target_timestamp": target_timestamp,
                "current_price": float(current_prices[index]),
                "predicted_price": expected_price,
                "roi_percent": roi_percent,
                "conditional_roi_percent": conditional_roi_percent,
                "probability_percent": float(
                    action_probability[index] * 100.0
                ),
                "move_probability_percent": float(
                    stock_move_probability[index] * 100.0
                ),
                "direction_probability_percent": float(
                    direction_probability[index] * 100.0
                ),
                "backtest_accuracy_percent": float(
                    historical_accuracy[index] * 100.0
                ),
                "backtest_moving_windows": int(moving_observations[index]),
                "watched": stock_id.casefold() in watched_stocks,
            }
        )
    sort_fields = {
        "probability": "probability_percent",
        "roi": "roi_percent",
        "price": "current_price",
    }
    predictions.sort(
        key=lambda result: result[sort_fields[sort_by]],
        reverse=True,
    )
    return predictions


def local_time(timestamp):
    return datetime.fromtimestamp(timestamp).isoformat(
        sep=" ",
        timespec="minutes",
    )


def print_text(result, top):
    predictions = result["predictions"]
    timestamps = result["timestamps"]
    selected = predictions if top == 0 else predictions[:top]
    latest = int(timestamps[-1])
    target = latest + INTERVAL
    print()
    print("15-minute Torn stock forecast")
    print(
        f"History: {len(timestamps):,} snapshots | "
        f"{result['effective_market_moves']:,} independent market moves | "
        f"{result['validation_windows']:,} untouched validation moves"
    )
    print(
        "Model: adaptive movement hurdle + strongly regularized "
        f"market-factor ridge (alpha={result['ridge_alpha']:g})"
    )
    print(
        f"Next-window market movement probability: "
        f"{result['market_move_probability_percent']:.1f}% | "
        f"data quality: {result['data_quality']}"
    )
    print(
        f"Act at {local_time(latest)}; forecast and reassess at "
        f"{local_time(target)}"
    )
    print()
    print(
        f"{'STOCK':<7}{'ACTION':<8}{'CURRENT':>12}{'15M PRICE':>13}"
        f"{'ROI':>10}{'MOVE':>9}{'DIR':>9}{'RIGHT':>9}"
        f"{'OOS':>8}{'N':>5}"
    )
    print("-" * 90)
    for prediction in selected:
        line = (
            f"{prediction['stock']:<7}{prediction['action']:<8}"
            f"{prediction['current_price']:>12.2f}"
            f"{prediction['predicted_price']:>13.2f}"
            f"{prediction['roi_percent']:>+9.4f}%"
            f"{prediction['move_probability_percent']:>8.1f}%"
            f"{prediction['direction_probability_percent']:>8.1f}%"
            f"{prediction['probability_percent']:>8.1f}%"
            f"{prediction['backtest_accuracy_percent']:>7.1f}%"
            f"{prediction['backtest_moving_windows']:>5d}"
        )
        print(
            chalk(line, color=COLORS.yellow)
            if prediction["watched"]
            else line
        )
    print()
    print(
        "MOVE is P(price changes); DIR is calibrated P(direction correct | "
        "move); RIGHT is their product. ROI is probability-weighted. "
        "All probabilities are estimates, not guarantees."
    )
    if result["data_quality"] == "LOW":
        print(
            chalk(
                "Warning: the cache contains too few independent market "
                "moves for high-confidence direction forecasts.",
                color=COLORS.yellow,
            )
        )


def calculate_predictions(args, cache_file):
    snapshots = load_cache(cache_file)
    timestamps, stock_ids, prices = price_matrix(snapshots)
    (
        x,
        y,
        latest_x,
        _,
        market_moves,
        stock_moves,
        continuous,
    ) = prepare_training_data(timestamps, prices)
    (
        conditional_forecast,
        validation_y,
        validation_prediction,
        alpha,
    ) = train_and_predict(
        x,
        y,
        latest_x,
    )
    (
        market_move_probability,
        stock_move_probability,
        _,
    ) = movement_probabilities(market_moves, stock_moves, continuous)
    effective_market_moves = int(np.sum(market_moves & continuous))
    (
        action_probability,
        direction_probability,
        historical_accuracy,
        moving_observations,
        validation_rows,
    ) = confidence_scores(
        validation_y,
        validation_prediction,
        stock_move_probability,
        effective_market_moves,
    )
    predictions = make_predictions(
        timestamps,
        stock_ids,
        prices,
        conditional_forecast,
        action_probability,
        stock_move_probability,
        direction_probability,
        historical_accuracy,
        moving_observations,
        args.sort,
        args.watch,
    )
    repeated_ratio = 1.0 - (
        effective_market_moves / max(1, int(np.sum(continuous)))
    )
    if effective_market_moves < 100 or repeated_ratio > 0.50:
        data_quality = "LOW"
    elif effective_market_moves < 200:
        data_quality = "MEDIUM"
    else:
        data_quality = "HIGH"
    return {
        "model": "adaptive_movement_hurdle_factor_ridge_v2",
        "timestamps": timestamps,
        "training_windows": len(x),
        "validation_windows": validation_rows,
        "effective_market_moves": effective_market_moves,
        "repeated_snapshot_percent": repeated_ratio * 100.0,
        "market_move_probability_percent": market_move_probability * 100.0,
        "data_quality": data_quality,
        "ridge_alpha": alpha,
        "predictions": predictions,
    }


def selected_predictions(result, top):
    predictions = result["predictions"]
    return predictions if top == 0 else predictions[:top]


def standalone_payload(result, args):
    timestamps = result["timestamps"]
    last_cached_timestamp = int(timestamps[-1])
    return {
        "model": result["model"],
        "probability_semantics": (
            "P(price moves) * P(predicted direction is correct | price moves)"
        ),
        "interval_minutes": INTERVAL // 60,
        "latest_timestamp": last_cached_timestamp,
        "last_cached_timestamp": last_cached_timestamp,
        "last_cached_time": local_time(last_cached_timestamp),
        "target_timestamp": last_cached_timestamp + INTERVAL,
        "training_windows": result["training_windows"],
        "validation_windows": result["validation_windows"],
        "effective_market_moves": result["effective_market_moves"],
        "repeated_snapshot_percent": result["repeated_snapshot_percent"],
        "market_move_probability_percent": result[
            "market_move_probability_percent"
        ],
        "data_quality": result["data_quality"],
        "ridge_alpha": result["ridge_alpha"],
        "sorted_by": args.sort,
        "predictions": selected_predictions(result, args.top),
    }


def server_payload(result, args, next_refresh_timestamp, last_error=None):
    timestamps = result["timestamps"]
    last_cached_timestamp = int(timestamps[-1])
    predictions = selected_predictions(result, args.top)
    payload = {
        "status": "ok" if last_error is None else "stale",
        "model": result["model"],
        "probability_semantics": (
            "P(price moves) * P(predicted direction is correct | price moves)"
        ),
        #"listen_address": "0.0.0.0",
        #"port": args.port,
        "interval_minutes": INTERVAL // 60,
        "latest_timestamp": last_cached_timestamp,
        "last_cached_timestamp": last_cached_timestamp,
        "last_cached_time": local_time(last_cached_timestamp),
        "target_timestamp": last_cached_timestamp + INTERVAL,
        "target_time": local_time(last_cached_timestamp + INTERVAL),
        "generated_timestamp": int(time.time()),
        "next_refresh_timestamp": next_refresh_timestamp,
        "training_windows": result["training_windows"],
        "validation_windows": result["validation_windows"],
        "effective_market_moves": result["effective_market_moves"],
        "repeated_snapshot_percent": result["repeated_snapshot_percent"],
        "market_move_probability_percent": result[
            "market_move_probability_percent"
        ],
        "data_quality": result["data_quality"],
        "ridge_alpha": result["ridge_alpha"],
        "sorted_by": args.sort,
        #"top": args.top,
        "buy":[
            prediction
            for prediction in predictions
            if prediction["action"] == "BUY"
        ],
        "sell":[
            prediction
            for prediction in predictions
            if prediction["action"] == "SELL"
        ],
    }
    if last_error is not None:payload["last_error"]=str(last_error)
    return(payload)


class PredictionRequestHandler(BaseHTTPRequestHandler):
    server_version = "StocksPredictor/1.0"
    sys_version = ""

    def send_json(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.partition("?")[0] != "/":
            self.send_json(
                404,
                b'{"status":"not_found","buy":[],"sell":[]}',
            )
            return
        status, body = self.server.current_response()
        self.send_json(status, body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format, *args):
        return


class PredictionHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address):
        self.response_lock = threading.Lock()
        self.response_status = 503
        self.response_body = json.dumps(
            {
                "status": "starting",
                "listen_address": "0.0.0.0",
                "interval_minutes": INTERVAL // 60,
                "last_cached_timestamp": None,
                "buy": [],
                "sell": [],
            }
        ).encode("utf-8")
        super().__init__(address, PredictionRequestHandler)

    def replace_payload(self, payload, status=200):
        body = json.dumps(payload, indent=2).encode("utf-8")
        with self.response_lock:
            self.response_status = status
            self.response_body = body

    def current_response(self):
        with self.response_lock:
            return self.response_status, self.response_body


def next_interval_timestamp():
    now = time.time()
    return (int(now) // INTERVAL + 1) * INTERVAL


def clear_console():
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="")


def display_server_state(result, args, next_refresh, error=None):
    clear_console()
    if result is not None:
        print_text(result, args.top)
    else:
        print("\nWaiting for the first successful prediction...")
    print()
    print(
        f"JSON server: http://0.0.0.0:{args.port}/ "
        "(localhost, LAN, and externally routed interfaces)"
    )
    if result is not None:
        last_cached = int(result["timestamps"][-1])
        print(
            f"Last cached 15-minute timestamp: {last_cached} "
            f"({local_time(last_cached)})"
        )
    print(
        f"Next one-day cache refresh: {local_time(next_refresh)} "
        f"({next_refresh})"
    )
    if error is not None:
        print(chalk(f"Last refresh failed: {error}", color=COLORS.red))
    sys.stdout.flush()


def server_refresh_loop(http_server, args, cache_file, stop_event):
    last_result = None
    refresh_days = REFRESH_DAYS if cache_needs_bootstrap(cache_file) else 1
    while not stop_event.is_set():
        error = None
        try:
            refresh_cache(cache_file, refresh_days, quiet=True)
            last_result = calculate_predictions(args, cache_file)
            refresh_days = 1
        except Exception as caught_error:
            error = caught_error

        next_refresh = next_interval_timestamp()
        if last_result is not None:
            http_server.replace_payload(
                server_payload(last_result, args, next_refresh, error)
            )
        else:
            http_server.replace_payload(
                {
                    "status": "error",
                    "listen_address": "0.0.0.0",
                    "port": args.port,
                    "interval_minutes": INTERVAL // 60,
                    "last_cached_timestamp": None,
                    "next_refresh_timestamp": next_refresh,
                    "last_error": str(error),
                    "buy": [],
                    "sell": [],
                },
                status=503,
            )
        display_server_state(last_result, args, next_refresh, error)
        stop_event.wait(max(0.0, next_refresh - time.time()))


def run_server(args, cache_file):
    http_server = PredictionHTTPServer(("0.0.0.0", args.port))
    stop_event = threading.Event()
    refresh_thread = threading.Thread(
        target=server_refresh_loop,
        args=(http_server, args, cache_file, stop_event),
        name="stock-refresh",
        daemon=True,
    )
    refresh_thread.start()
    try:
        http_server.serve_forever(poll_interval=0.5)
    finally:
        stop_event.set()
        http_server.server_close()
        refresh_thread.join(timeout=2.0)
    return 0


def main():
    args = parse_args()
    cache_file = args.cache.expanduser().resolve()
    try:
        if cache_file.name.lower() != "stocks_cache.json":
            raise ValueError(
                "--cache must name a stocks_cache.json file because "
                "stocks_cacher always writes that filename"
            )
        if args.server:
            return run_server(args, cache_file)
        if not args.skip:
            refresh_cache(cache_file, args.days, quiet=args.json)
        result = calculate_predictions(args, cache_file)
        if args.json:
            print(json.dumps(standalone_payload(result, args), indent=2))
        else:
            print_text(result, args.top)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Prediction failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
