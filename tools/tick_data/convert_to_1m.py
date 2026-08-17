#!/usr/bin/env python3
"""Convert CTP-style cumulative tick CSV files to kline.lua 1-minute bars."""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import chain
from pathlib import Path
from typing import Iterable


SHANGHAI = timezone(timedelta(hours=8))
HEADER = [
    "bar_time", "trading_day", "open", "high", "low", "close",
    "volume", "turnover", "open_interest", "settlement", "tick_count", "flags",
]

# All intervals are left-closed and right-open, matching KLINE.md.
COMMODITY_DAY = ((9 * 60, 10 * 60 + 15), (10 * 60 + 30, 11 * 60 + 30),
                 (13 * 60 + 30, 15 * 60))
CFFEX_INDEX = ((9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60))
CFFEX_BOND = ((9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60 + 15))

INDEX_ROOTS = frozenset({"if", "ic", "ih", "im"})
BOND_ROOTS = frozenset({"t", "tf", "ts", "tl"})

# SHFE/INE night sessions. The remaining night products close at 23:00.
NIGHT_0230 = frozenset({"au", "ag", "sc"})
NIGHT_0100 = frozenset({"cu", "al", "zn", "pb", "ni", "sn", "ss", "ao", "ad", "bc"})
NIGHT_2300 = frozenset({
    # SHFE / INE
    "rb", "hc", "bu", "ru", "fu", "sp", "br", "wr", "nr", "lu",
    # DCE
    "a", "b", "bz", "c", "cs", "eb", "eg", "i", "j", "jm", "l", "m",
    "op", "p", "pg", "pp", "v", "y",
    # CZCE
    "cf", "cy", "fg", "ma", "oi", "pf", "pr", "px", "rm", "sa", "sf",
    "sh", "sm", "sr", "ta", "ur", "zc",
})

NO_NIGHT = frozenset({
    # DCE
    "bb", "fb", "jd", "lg", "lh", "rr",
    # CZCE
    "ap", "cj", "jr", "lr", "pk", "pm", "ri", "rs", "wh",
    # INE / GFEX and newer day-only contracts in this data set
    "ec", "si", "lc", "ps", "pl", "pd", "pt",
})
KNOWN_ROOTS = INDEX_ROOTS | BOND_ROOTS | NIGHT_0230 | NIGHT_0100 | NIGHT_2300 | NO_NIGHT


@dataclass
class Bar:
    bar_time: datetime
    trading_day: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float
    open_interest: float
    tick_count: int
    night: bool


def symbol_root(symbol: str) -> str:
    match = re.match(r"[A-Za-z]+", symbol)
    if not match:
        raise ValueError(f"instrument has no alphabetic root: {symbol!r}")
    return match.group(0).lower()


def sessions_for(root: str) -> tuple[tuple[int, int], ...]:
    if root in INDEX_ROOTS:
        return CFFEX_INDEX
    if root in BOND_ROOTS:
        return CFFEX_BOND
    if root in NIGHT_0230:
        return ((21 * 60, 24 * 60), (0, 2 * 60 + 30), *COMMODITY_DAY)
    if root in NIGHT_0100:
        return ((21 * 60, 24 * 60), (0, 60), *COMMODITY_DAY)
    if root in NIGHT_2300:
        return ((21 * 60, 23 * 60), *COMMODITY_DAY)
    if root in NO_NIGHT:
        return COMMODITY_DAY
    raise ValueError(f"no trading-session mapping for root {root!r}")


def in_session(minute: int, sessions: tuple[tuple[int, int], ...]) -> bool:
    return any(start <= minute < end for start, end in sessions)


def source_datetime(trading_day: str, update_time: str, millis: str) -> datetime:
    day = datetime.strptime(trading_day, "%Y%m%d").date()
    hh, mm, ss = (int(part) for part in update_time.split(":"))
    # A CTP trading-day file stores the preceding calendar evening's night session.
    if hh >= 18:
        day -= timedelta(days=1)
    return datetime(day.year, day.month, day.day, hh, mm, ss,
                    int(millis or 0) * 1000, tzinfo=SHANGHAI)


def finite_number(value: str, field: str, path: Path, line_no: int) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise ValueError(f"{path}:{line_no}: invalid {field}: {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"{path}:{line_no}: non-finite {field}: {value!r}")
    return number


def source_identity(source: Path) -> tuple[str, str]:
    match = re.fullmatch(r"(.+)_([0-9]{8})", source.stem)
    if not match:
        raise ValueError(f"source filename must end in _YYYYMMDD.csv: {source.name}")
    return match.group(1), match.group(2)


def output_name(source: Path, trading_day: str) -> str:
    symbol, _ = source_identity(source)
    return f"{symbol}_1m_{trading_day}.csv"


def format_bar(bar: Bar) -> list[str]:
    flags = 1 | (4 if bar.night else 0)  # CLOSED | NIGHT
    return [
        bar.bar_time.strftime("%Y-%m-%d %H:%M:%S"), bar.trading_day,
        f"{bar.open:.2f}", f"{bar.high:.2f}", f"{bar.low:.2f}", f"{bar.close:.2f}",
        f"{bar.volume:.0f}", f"{bar.turnover:.2f}", f"{bar.open_interest:.0f}",
        "0.00", str(bar.tick_count), str(flags),
    ]


def convert_file(source_text: str, output_dir_text: str) -> dict[str, object]:
    source = Path(source_text)
    output_dir = Path(output_dir_text)
    rows = valid_ticks = filtered = bad_price = backwards = bars = resets = 0
    logical_symbol = instrument = root = trading_day = None
    source_trading_days: set[str] = set()
    current: Bar | None = None
    last_volume: float | None = None
    last_turnover: float | None = None
    last_time: datetime | None = None
    output_path: Path | None = None
    temp_path: Path | None = None

    with source.open("r", newline="", encoding="utf-8-sig") as src:
        reader = csv.DictReader(src)
        if reader.fieldnames is None or any(name not in reader.fieldnames for name in (
            "TradingDay", "InstrumentID", "UpdateTime", "UpdateMillisec", "LastPrice",
            "Volume", "Turnover", "OpenInterest",
        )):
            raise ValueError(f"{source}: unsupported header")

        first = next(reader, None)
        if first is None:
            raise ValueError(f"{source}: no data rows")
        records: Iterable[tuple[int, dict[str, str]]] = chain([(2, first)], enumerate(reader, 3))

        logical_symbol, trading_day = source_identity(source)
        instrument = first["InstrumentID"]
        root = symbol_root(logical_symbol)
        sessions = sessions_for(root)
        output_path = output_dir / output_name(source, trading_day)
        temp_path = output_path.with_name(f".{output_path.name}.tmp.{os.getpid()}")

        try:
            with temp_path.open("w", newline="", encoding="utf-8") as dst:
                dst.write(
                    f"# symbol={logical_symbol} period=1m tz=Asia/Shanghai "
                    f"generated_by=convert_to_1m.py\n"
                )
                writer = csv.writer(dst, lineterminator="\n")
                writer.writerow(HEADER)

                for line_no, row in records:
                    rows += 1
                    source_trading_days.add(row["TradingDay"])
                    if row["InstrumentID"] != instrument:
                        raise ValueError(f"{source}:{line_no}: mixed instruments")
                    tick_time = source_datetime(trading_day, row["UpdateTime"], row["UpdateMillisec"])
                    minute_of_day = tick_time.hour * 60 + tick_time.minute
                    price = finite_number(row["LastPrice"], "LastPrice", source, line_no)
                    if price <= 0:
                        bad_price += 1
                        continue
                    if last_time is not None and tick_time < last_time:
                        backwards += 1
                        continue
                    if not in_session(minute_of_day, sessions):
                        filtered += 1
                        continue

                    volume = finite_number(row["Volume"], "Volume", source, line_no)
                    turnover = finite_number(row["Turnover"], "Turnover", source, line_no)
                    open_interest = finite_number(row["OpenInterest"], "OpenInterest", source, line_no)
                    minute_start = tick_time.replace(second=0, microsecond=0)

                    volume_delta = turnover_delta = 0.0
                    if last_volume is not None:
                        volume_delta = volume - last_volume
                        turnover_delta = turnover - last_turnover  # type: ignore[operator]
                        if volume_delta < 0:
                            volume_delta = 0.0
                            resets += 1
                        if turnover_delta < 0:
                            turnover_delta = 0.0
                            resets += 1

                    if current is not None and minute_start > current.bar_time:
                        writer.writerow(format_bar(current))
                        bars += 1
                        current = None

                    night = tick_time.hour >= 18 or tick_time.hour < 3
                    if current is None:
                        current = Bar(minute_start, trading_day, price, price, price, price,
                                      volume_delta, turnover_delta, open_interest, 1, night)
                    else:
                        current.high = max(current.high, price)
                        current.low = min(current.low, price)
                        current.close = price
                        current.volume += volume_delta
                        current.turnover += turnover_delta
                        current.open_interest = open_interest
                        current.tick_count += 1

                    last_volume, last_turnover, last_time = volume, turnover, tick_time
                    valid_ticks += 1

                expected_source_days = {
                    trading_day,
                    (datetime.strptime(trading_day, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d"),
                }
                unexpected_days = source_trading_days - expected_source_days
                if unexpected_days:
                    raise ValueError(
                        f"{source}: unexpected source TradingDay values: {sorted(unexpected_days)}"
                    )
                if current is not None:
                    writer.writerow(format_bar(current))
                    bars += 1
                dst.flush()
                os.fsync(dst.fileno())
            os.replace(temp_path, output_path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

    return {
        "source": source.name, "output": output_path.name if output_path else None,
        "symbol": logical_symbol, "instrument": instrument, "root": root,
        "trading_day": trading_day, "source_trading_days": "|".join(sorted(source_trading_days)),
        "rows": rows, "valid_ticks": valid_ticks, "filtered": filtered,
        "bad_price": bad_price, "backwards": backwards, "bars": bars,
        "counter_resets": resets,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--workers", type=int, default=max(1, min(8, os.cpu_count() or 1)))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    sources = sorted(p for p in args.input_dir.glob("*.csv") if p.is_file())
    if not sources:
        print(f"no CSV files found in {args.input_dir}", file=sys.stderr)
        return 2
    args.output_dir.mkdir(parents=True, exist_ok=True)

    filename_roots = {symbol_root(p.name) for p in sources}
    unknown = sorted(filename_roots - KNOWN_ROOTS)
    if unknown:
        print(f"missing trading-session mappings: {', '.join(unknown)}", file=sys.stderr)
        return 2

    results: list[dict[str, object]] = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(convert_file, str(p), str(args.output_dir)): p for p in sources}
        for completed, future in enumerate(as_completed(futures), 1):
            try:
                result = future.result()
            except Exception as exc:
                print(f"FAILED {futures[future]}: {exc}", file=sys.stderr)
                for pending in futures:
                    pending.cancel()
                return 1
            results.append(result)
            if completed % 50 == 0 or completed == len(futures):
                print(f"converted {completed}/{len(futures)} files", file=sys.stderr, flush=True)

    results.sort(key=lambda item: str(item["source"]))
    report_path = args.output_dir / "conversion_report.csv"
    with report_path.open("w", newline="", encoding="utf-8") as report:
        fields = list(results[0])
        writer = csv.DictWriter(report, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(results)

    totals = {key: sum(int(item[key]) for item in results) for key in (
        "rows", "valid_ticks", "filtered", "bad_price", "backwards", "bars", "counter_resets",
    )}
    print(f"files={len(results)} " + " ".join(f"{key}={value}" for key, value in totals.items()))
    print(f"report={report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
