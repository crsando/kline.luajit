from __future__ import annotations

import argparse
import csv
import fcntl
import os
import re
import tempfile
from bisect import bisect_left, bisect_right
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date, time, timedelta
from pathlib import Path
from typing import TextIO

import akshare as ak
import pandas as pd

SOURCE_COLUMNS = ["datetime", "open", "high", "low", "close", "volume", "hold"]
KLINE_COLUMNS = [
    "bar_time",
    "trading_day",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
    "open_interest",
    "settlement",
    "tick_count",
    "flags",
]
PERIODS = (1, 5, 15, 30, 60)
CLOSED_FLAG = 0x01
NIGHT_FLAG = 0x04
SOURCE_NAME = "akshare-sina"
SCHEMA_VERSION = "v1"


def parse_clock(value: str) -> time:
    match = re.fullmatch(r"(\d{2}):(\d{2})", value)
    if not match:
        raise argparse.ArgumentTypeError(f"invalid time {value!r}; expected HH:MM")
    hour, minute = map(int, match.groups())
    if hour > 23 or minute > 59:
        raise argparse.ArgumentTypeError(f"invalid time {value!r}; expected HH:MM")
    return time(hour, minute)


def validate_symbol(symbol: str) -> str:
    symbol = symbol.upper()
    if not re.fullmatch(r"[A-Z0-9_.-]+", symbol):
        raise ValueError(f"invalid Sina futures symbol: {symbol!r}")
    return symbol


def default_data_home() -> Path:
    if value := os.environ.get("KLINE_DATA_HOME"):
        return Path(value).expanduser()
    if value := os.environ.get("XDG_DATA_HOME"):
        return Path(value).expanduser() / "kline"
    return Path.home() / ".local" / "share" / "kline"


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def lock_path(data_home: Path, *parts: object) -> Path:
    name = "__".join(str(part) for part in parts)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:__[A-Za-z0-9_.-]+)*", name):
        raise ValueError(f"invalid lock name: {name!r}")
    return data_home / "locks" / f"{name}.lock"


def fetch_sina_minutes(symbol: str, period: int) -> pd.DataFrame:
    frame = ak.futures_zh_minute_sina(symbol=symbol, period=str(period))
    if frame.empty:
        raise RuntimeError(f"Sina returned no data for {symbol} period={period}")
    return validate_source_frame(frame)


def fetch_sina_trade_dates() -> list[date]:
    frame = ak.tool_trade_date_hist_sina()
    if "trade_date" not in frame.columns or frame.empty:
        raise RuntimeError("AKShare Sina trade calendar returned no trade_date values")
    values = pd.to_datetime(frame["trade_date"], errors="raise").dt.date
    return sorted(set(values.tolist()))


def validate_source_frame(frame: pd.DataFrame) -> pd.DataFrame:
    missing = [column for column in SOURCE_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"AKShare response is missing columns: {', '.join(missing)}")

    result = frame[SOURCE_COLUMNS].copy()
    result["datetime"] = pd.to_datetime(result["datetime"], errors="raise")
    for column in SOURCE_COLUMNS[1:]:
        result[column] = pd.to_numeric(result[column], errors="coerce")
    bad = result[SOURCE_COLUMNS[1:]].isna().any(axis=1)
    if bad.any():
        raise ValueError(f"AKShare response contains {int(bad.sum())} rows with invalid numbers")
    if result["datetime"].duplicated().any():
        duplicates = int(result["datetime"].duplicated(keep=False).sum())
        raise ValueError(f"AKShare response contains {duplicates} duplicate datetime rows")
    return result.sort_values("datetime", kind="stable").reset_index(drop=True)


def next_weekday(value: date, *, include_current: bool) -> date:
    current = value if include_current else value + timedelta(days=1)
    while current.weekday() >= 5:
        current += timedelta(days=1)
    return current


def calendar_day(
    natural_day: date,
    *,
    after_night_open: bool,
    trade_dates: Sequence[date] | None,
) -> date:
    if trade_dates is None:
        return next_weekday(natural_day, include_current=not after_night_open)

    position = (
        bisect_right(trade_dates, natural_day)
        if after_night_open
        else bisect_left(trade_dates, natural_day)
    )
    if position >= len(trade_dates):
        raise ValueError(f"trade calendar does not cover bars after {natural_day.isoformat()}")
    return trade_dates[position]


def normalize_sina_frame(
    frame: pd.DataFrame,
    *,
    period: int,
    timestamp_mode: str,
    trade_dates: Sequence[date] | None,
    night_start: time = time(18, 0),
    day_start: time = time(8, 0),
) -> pd.DataFrame:
    source = validate_source_frame(frame)
    if period not in PERIODS:
        raise ValueError(f"unsupported period: {period}")
    if timestamp_mode not in {"end", "start"}:
        raise ValueError(f"unsupported timestamp mode: {timestamp_mode}")

    bar_times = source["datetime"]
    if timestamp_mode == "end":
        bar_times = bar_times - timedelta(minutes=period)

    trading_days: list[int] = []
    flags: list[int] = []
    for stamp in bar_times.array.to_pydatetime():
        is_evening = stamp.time() >= night_start
        is_after_midnight = stamp.time() < day_start
        trading_day = calendar_day(
            stamp.date(),
            after_night_open=is_evening,
            trade_dates=trade_dates,
        )
        trading_days.append(int(trading_day.strftime("%Y%m%d")))
        flags.append(CLOSED_FLAG | (NIGHT_FLAG if is_evening or is_after_midnight else 0))

    result = pd.DataFrame(
        {
            "bar_time": bar_times,
            "trading_day": trading_days,
            "open": source["open"],
            "high": source["high"],
            "low": source["low"],
            "close": source["close"],
            "volume": source["volume"],
            "turnover": 0.0,
            "open_interest": source["hold"],
            "settlement": 0.0,
            "tick_count": 0,
            "flags": flags,
        }
    )
    return result[KLINE_COLUMNS].sort_values("bar_time", kind="stable").reset_index(drop=True)


@contextmanager
def atomic_text_writer(path: Path) -> Iterator[TextIO]:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()


def atomic_write_raw(frame: pd.DataFrame, path: Path) -> None:
    with atomic_text_writer(path) as handle:
        frame.to_csv(handle, index=False)


def read_existing_raw(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=SOURCE_COLUMNS)
    return validate_source_frame(pd.read_csv(path))


def write_raw_years(
    frame: pd.DataFrame,
    data_home: Path,
    *,
    symbol: str,
    period: int,
) -> list[Path]:
    source = validate_source_frame(frame)
    paths: list[Path] = []
    for year, incoming in source.groupby(source["datetime"].dt.year, sort=True):
        path = (
            data_home
            / "raw"
            / SOURCE_NAME
            / symbol
            / f"{period}m"
            / str(int(year))
            / f"{symbol}_{period}m_sina.csv"
        )
        lock = lock_path(data_home, "raw", SOURCE_NAME, symbol, f"{period}m", int(year))
        with file_lock(lock):
            existing = read_existing_raw(path)
            merged = (
                incoming.copy()
                if existing.empty
                else pd.concat([existing, incoming], ignore_index=True)
            )
            merged["datetime"] = pd.to_datetime(merged["datetime"], errors="raise")
            merged = (
                merged.drop_duplicates(subset=["datetime"], keep="last")
                .sort_values("datetime", kind="stable")
                .reset_index(drop=True)
            )
            atomic_write_raw(merged, path)
        paths.append(path)
    return paths


def read_metadata(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8") as handle:
        first_line = handle.readline().strip()
    if not first_line.startswith("#"):
        raise ValueError(f"existing kline file {path} has no metadata header")
    return {
        key: value
        for token in first_line[1:].split()
        if "=" in token
        for key, value in [token.split("=", 1)]
    }


def read_existing_kline(
    path: Path,
    *,
    expected_metadata: dict[str, str] | None = None,
) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=KLINE_COLUMNS)
    metadata = read_metadata(path)
    for key, expected in (expected_metadata or {}).items():
        if metadata.get(key) != expected:
            raise ValueError(
                f"existing kline file {path} has {key}={metadata.get(key)!r}; "
                f"expected {expected!r}; use another output directory"
            )
    frame = pd.read_csv(path, comment="#")
    missing = [column for column in KLINE_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"existing kline file {path} is missing: {', '.join(missing)}")
    frame["bar_time"] = pd.to_datetime(frame["bar_time"], errors="raise")
    return frame[KLINE_COLUMNS]


def format_number(value: object, decimals: int) -> str:
    return f"{float(value):.{decimals}f}"


def atomic_write_kline(
    frame: pd.DataFrame,
    path: Path,
    *,
    symbol: str,
    period: int,
    timestamp_mode: str,
    calendar_mode: str,
    night_start: time,
    day_start: time,
    price_decimals: int,
) -> None:
    with atomic_text_writer(path) as handle:
        handle.write(
            f"# symbol={symbol} period={period}m tz=Asia/Shanghai schema={SCHEMA_VERSION} "
            f"source=akshare/sina source_time={timestamp_mode} calendar={calendar_mode} "
            f"night_start={night_start.strftime('%H:%M')} "
            f"day_start={day_start.strftime('%H:%M')}\n"
        )
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(KLINE_COLUMNS)
        for row in frame.itertuples(index=False):
            writer.writerow(
                [
                    row.bar_time.strftime("%Y-%m-%d %H:%M:%S"),
                    int(row.trading_day),
                    format_number(row.open, price_decimals),
                    format_number(row.high, price_decimals),
                    format_number(row.low, price_decimals),
                    format_number(row.close, price_decimals),
                    format_number(row.volume, 0),
                    format_number(row.turnover, 2),
                    format_number(row.open_interest, 0),
                    format_number(row.settlement, price_decimals),
                    int(row.tick_count),
                    int(row.flags),
                ]
            )


def write_kline_days(
    frame: pd.DataFrame,
    data_home: Path,
    *,
    symbol: str,
    period: int,
    timestamp_mode: str,
    calendar_mode: str = "sina",
    night_start: time = time(18, 0),
    day_start: time = time(8, 0),
    price_decimals: int,
) -> list[Path]:
    paths: list[Path] = []
    expected_metadata = {
        "symbol": symbol,
        "period": f"{period}m",
        "schema": SCHEMA_VERSION,
        "source": "akshare/sina",
        "source_time": timestamp_mode,
        "calendar": calendar_mode,
        "night_start": night_start.strftime("%H:%M"),
        "day_start": day_start.strftime("%H:%M"),
    }
    for trading_day, incoming in frame.groupby("trading_day", sort=True):
        year = str(int(trading_day))[:4]
        path = (
            data_home
            / "canonical"
            / SCHEMA_VERSION
            / SOURCE_NAME
            / symbol
            / f"{period}m"
            / year
            / f"{symbol}_{period}m_{int(trading_day)}.csv"
        )
        lock = lock_path(
            data_home,
            "canonical-v1",
            SOURCE_NAME,
            symbol,
            f"{period}m",
            int(trading_day),
        )
        with file_lock(lock):
            existing = read_existing_kline(path, expected_metadata=expected_metadata)
            merged = (
                incoming.copy()
                if existing.empty
                else pd.concat([existing, incoming], ignore_index=True)
            )
            merged["bar_time"] = pd.to_datetime(merged["bar_time"], errors="raise")
            merged = (
                merged.drop_duplicates(subset=["bar_time"], keep="last")
                .sort_values("bar_time", kind="stable")
                .reset_index(drop=True)
            )
            atomic_write_kline(
                merged,
                path,
                symbol=symbol,
                period=period,
                timestamp_mode=timestamp_mode,
                calendar_mode=calendar_mode,
                night_start=night_start,
                day_start=day_start,
                price_decimals=price_decimals,
            )
        paths.append(path)
    return paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch Sina futures bars with AKShare and convert them for kline.luajit",
    )
    parser.add_argument("--symbol", default="RB0", help="uppercase Sina symbol, e.g. RB0 or RB2610")
    parser.add_argument(
        "--period", type=int, choices=PERIODS, default=1, help="bar period in minutes"
    )
    parser.add_argument(
        "--data-home",
        "--output-dir",
        dest="data_home",
        type=Path,
        default=default_data_home(),
        help="shared K-line root; defaults to KLINE_DATA_HOME or XDG data home",
    )
    parser.add_argument(
        "--timestamp-mode",
        choices=("end", "start"),
        default="end",
        help="interpret Sina datetime as bar end or bar start; default: end",
    )
    parser.add_argument(
        "--calendar",
        choices=("sina", "weekday"),
        default="sina",
        help="trade-day inference source; weekday ignores Chinese holidays",
    )
    parser.add_argument("--night-start", type=parse_clock, default=time(18, 0))
    parser.add_argument("--day-start", type=parse_clock, default=time(8, 0))
    parser.add_argument("--price-decimals", type=int, default=2)
    return parser


def run(args: argparse.Namespace) -> None:
    symbol = validate_symbol(args.symbol)
    source = fetch_sina_minutes(symbol, args.period)
    trade_dates = fetch_sina_trade_dates() if args.calendar == "sina" else None
    normalized = normalize_sina_frame(
        source,
        period=args.period,
        timestamp_mode=args.timestamp_mode,
        trade_dates=trade_dates,
        night_start=args.night_start,
        day_start=args.day_start,
    )

    raw_paths = write_raw_years(source, args.data_home, symbol=symbol, period=args.period)
    kline_paths = write_kline_days(
        normalized,
        args.data_home,
        symbol=symbol,
        period=args.period,
        timestamp_mode=args.timestamp_mode,
        calendar_mode=args.calendar,
        night_start=args.night_start,
        day_start=args.day_start,
        price_decimals=args.price_decimals,
    )

    print(f"data home: {args.data_home}")
    print(f"source rows: {len(source)}")
    print(f"source range: {source.iloc[0]['datetime']} -> {source.iloc[-1]['datetime']}")
    print(f"raw files: {len(raw_paths)}")
    for path in raw_paths:
        print(f"  {path}")
    print(f"kline files: {len(kline_paths)}")
    for path in kline_paths:
        print(f"  {path}")


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.price_decimals < 0:
        parser.error("--price-decimals must be non-negative")
    run(args)


if __name__ == "__main__":
    main()
