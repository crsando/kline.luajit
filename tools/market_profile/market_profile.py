#!/usr/bin/env python3
"""Day-session tick Volume Profile research pipeline.

The pipeline is intentionally standard-library only. It selects one liquid concrete
contract per day-only product, normalizes cumulative CTP snapshots, builds raw and
multi-scale profiles, discovers persistent nodes, and evaluates frozen morning
levels on the afternoon tick path.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import html
import json
import math
import os
import re
import shutil
import statistics
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Tuple


RAW_FIELDS = [
    "TradingDay", "InstrumentID", "UpdateTime", "UpdateMillisec", "LastPrice",
    "Volume", "Turnover", "OpenInterest",
]
NORMALIZED_FIELDS = [
    "trading_day", "instrument", "event_time", "millis_of_day", "session",
    "last_price", "volume", "turnover", "delta_volume", "delta_turnover",
    "interval_vwap", "open_interest", "gap_millis", "quality_flags",
]
LEVEL_FIELDS = [
    "trading_day", "root", "instrument", "available_time", "node_types",
    "center", "lower", "upper", "source", "raw_volume_share", "prominence_share",
    "width_ticks", "scale_persistence", "ltp_vwap_distance_ticks",
]
EVENT_FIELDS = LEVEL_FIELDS + [
    "role", "touch_time", "touch_price", "outcome", "resolution_time",
    "resolution_price", "reaction_ticks", "penetration_ticks", "mfe_ticks",
    "mae_ticks", "minutes_to_resolution",
]


@dataclass
class ContractStats:
    root: str
    instrument: str
    source_file: str
    total_volume: float = 0.0
    nonzero_updates: int = 0
    active_minutes: int = 0
    price_bins: int = 0
    valid_ticks: int = 0
    filtered_ticks: int = 0
    backwards_ticks: int = 0
    counter_resets: int = 0
    tick_misaligned: int = 0
    session_minutes: int = 0

    @property
    def coverage(self) -> float:
        return self.active_minutes / self.session_minutes if self.session_minutes else 0.0


@dataclass
class NormalizedStats:
    rows: int = 0
    positive_volume_rows: int = 0
    assigned_volume: float = 0.0
    counter_resets: int = 0
    turnover_resets: int = 0
    long_gaps: int = 0
    invalid_interval_vwap: int = 0


@dataclass
class Peak:
    index: int
    value: float
    prominence: float
    width: float
    scales: Set[float]


@dataclass
class ProfileBundle:
    root: str
    instrument: str
    trading_day: str
    tick_size: float
    multiplier: float
    available_time: str
    min_bin: int
    max_bin: int
    profiles: Dict[str, Dict[float, List[float]]]
    totals: Dict[str, float]
    minute_ranges_ticks: List[float]


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        config = json.load(fh)
    if config.get("schema_version") != 1:
        raise ValueError("unsupported contracts.json schema_version")
    return config


def symbol_root(text: str) -> str:
    match = re.match(r"([A-Za-z]+)", text)
    if not match:
        raise ValueError("cannot determine product root from %r" % text)
    return match.group(1).upper()


def source_identity(path: Path) -> Tuple[str, str]:
    match = re.fullmatch(r"(.+)_([0-9]{8})", path.stem)
    if not match:
        raise ValueError("source filename must end in _YYYYMMDD.csv: %s" % path.name)
    return match.group(1), match.group(2)


def hhmm_to_minute(value: str) -> int:
    hour, minute = (int(part) for part in value.split(":"))
    return hour * 60 + minute


def parse_millis_of_day(update_time: str, update_millisec: str) -> int:
    hour, minute, second = (int(part) for part in update_time.split(":"))
    return ((hour * 60 + minute) * 60 + second) * 1000 + int(update_millisec or 0)


def compile_sessions(config: Mapping[str, object], group: str) -> dict:
    raw = config["session_groups"][group]  # type: ignore[index]
    result = {"available_time": raw["available_time"]}
    for label in ("morning", "afternoon"):
        result[label] = [
            (hhmm_to_minute(start), hhmm_to_minute(end))
            for start, end in raw[label]
        ]
    return result


def session_label(millis_of_day: int, sessions: Mapping[str, object]) -> Optional[str]:
    minute = millis_of_day // 60000
    for label in ("morning", "afternoon"):
        for start, end in sessions[label]:  # type: ignore[index]
            if start <= minute < end:
                return label
    return None


def session_minute_count(sessions: Mapping[str, object]) -> int:
    return sum(end - start for label in ("morning", "afternoon") for start, end in sessions[label])  # type: ignore[index]


def finite_float(value: str, field: str, path: Path, line_no: int) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("%s:%d invalid %s=%r" % (path, line_no, field, value)) from exc
    if not math.isfinite(number):
        raise ValueError("%s:%d non-finite %s=%r" % (path, line_no, field, value))
    return number


def price_bin(price: float, tick_size: float) -> int:
    return int(round(price / tick_size))


def aligned_to_tick(price: float, tick_size: float, tolerance: float = 1e-6) -> bool:
    scaled = price / tick_size
    return abs(scaled - round(scaled)) <= tolerance


def iter_source_rows(path: Path) -> Iterator[Tuple[int, dict]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or any(field not in reader.fieldnames for field in RAW_FIELDS):
            raise ValueError("unsupported source header: %s" % path)
        yield from enumerate(reader, 2)


def scan_contract(path: Path, root: str, meta: Mapping[str, object], config: Mapping[str, object]) -> ContractStats:
    logical_symbol, _ = source_identity(path)
    sessions = compile_sessions(config, str(meta["session_group"]))
    stats = ContractStats(root=root, instrument="", source_file=path.name)
    # Contract selection is part of level construction, so it may only use morning data.
    stats.session_minutes = sum(end - start for start, end in sessions["morning"])
    last_time: Optional[int] = None
    last_volume: Optional[float] = None
    active_minutes: Set[int] = set()
    bins: Set[int] = set()
    tick_size = float(meta["tick_size"])

    for line_no, row in iter_source_rows(path):
        if not stats.instrument:
            stats.instrument = row["InstrumentID"]
        elif row["InstrumentID"] != stats.instrument:
            raise ValueError("%s:%d mixes instruments" % (path, line_no))
        millis = parse_millis_of_day(row["UpdateTime"], row["UpdateMillisec"])
        label = session_label(millis, sessions)
        price = finite_float(row["LastPrice"], "LastPrice", path, line_no)
        if label is None or price <= 0:
            stats.filtered_ticks += 1
            continue
        if last_time is not None and millis < last_time:
            stats.backwards_ticks += 1
            continue
        volume = finite_float(row["Volume"], "Volume", path, line_no)
        delta = 0.0
        if last_volume is not None:
            delta = volume - last_volume
            if delta < 0:
                stats.counter_resets += 1
                delta = 0.0
        if delta > 0 and label == "morning":
            stats.total_volume += delta
            stats.nonzero_updates += 1
            active_minutes.add(millis // 60000)
            bins.add(price_bin(price, tick_size))
        if not aligned_to_tick(price, tick_size):
            stats.tick_misaligned += 1
        stats.valid_ticks += 1
        last_time, last_volume = millis, volume

    if not stats.instrument:
        stats.instrument = logical_symbol
    stats.active_minutes = len(active_minutes)
    stats.price_bins = len(bins)
    return stats


def liquidity_reasons(stats: ContractStats, config: Mapping[str, object]) -> List[str]:
    limits = config["liquidity"]  # type: ignore[index]
    reasons = []
    if stats.total_volume < float(limits["min_volume"]):
        reasons.append("volume")
    if stats.nonzero_updates < int(limits["min_nonzero_updates"]):
        reasons.append("nonzero_updates")
    if stats.coverage < float(limits["min_active_minute_coverage"]):
        reasons.append("active_minute_coverage")
    if stats.price_bins < int(limits["min_price_bins"]):
        reasons.append("price_bins")
    if stats.backwards_ticks:
        reasons.append("backwards_ticks")
    if stats.tick_misaligned:
        reasons.append("tick_alignment")
    return reasons


def select_contracts(input_dir: Path, config: Mapping[str, object]) -> Tuple[List[dict], List[ContractStats]]:
    metadata = config["contracts"]  # type: ignore[index]
    by_root: Dict[str, List[ContractStats]] = defaultdict(list)
    files_by_root: Dict[str, List[Path]] = defaultdict(list)
    for path in sorted(input_dir.glob("*.csv")):
        logical, _ = source_identity(path)
        root = symbol_root(logical)
        if root not in metadata or "连续" in logical:
            continue
        files_by_root[root].append(path)

    all_stats: List[ContractStats] = []
    rows: List[dict] = []
    for root in sorted(metadata):
        meta = metadata[root]
        for path in files_by_root.get(root, []):
            stats = scan_contract(path, root, meta, config)
            by_root[root].append(stats)
            all_stats.append(stats)
        candidates = by_root.get(root, [])
        best = max(candidates, key=lambda item: (item.total_volume, item.nonzero_updates, item.source_file), default=None)
        if best is None:
            rows.append({
                "root": root, "exchange": meta["exchange"], "selected": "false",
                "instrument": "", "source_file": "", "total_volume": "0",
                "nonzero_updates": "0", "active_minutes": "0", "session_minutes": "0",
                "coverage": "0", "price_bins": "0", "exclusion_reasons": "no_source_file",
            })
            continue
        reasons = liquidity_reasons(best, config)
        rows.append({
            "root": root, "exchange": meta["exchange"], "selected": str(not reasons).lower(),
            "instrument": best.instrument, "source_file": best.source_file,
            "total_volume": format_number(best.total_volume),
            "nonzero_updates": str(best.nonzero_updates), "active_minutes": str(best.active_minutes),
            "session_minutes": str(best.session_minutes), "coverage": "%.6f" % best.coverage,
            "price_bins": str(best.price_bins), "exclusion_reasons": "|".join(reasons),
        })
    return rows, all_stats


def format_number(value: float, decimals: int = 10) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return ("%.*f" % (decimals, value)).rstrip("0").rstrip(".")


def write_csv(path: Path, rows: Iterable[Mapping[str, object]], fields: Sequence[str]) -> None:
    temp = path.with_name(".%s.tmp.%d" % (path.name, os.getpid()))
    with temp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temp, path)


def normalize_selected(source: Path, destination: Path, root: str, meta: Mapping[str, object], config: Mapping[str, object]) -> NormalizedStats:
    _, trading_day = source_identity(source)
    sessions = compile_sessions(config, str(meta["session_group"]))
    multiplier = float(meta["multiplier"])
    turnover_divisor = float(meta.get("turnover_divisor", multiplier))
    tick_size = float(meta["tick_size"])
    max_gap = int(config["profile"]["max_gap_millis"])  # type: ignore[index]
    max_vwap_distance = float(config["profile"]["max_interval_vwap_distance_ticks"])  # type: ignore[index]
    stats = NormalizedStats()
    last_time: Optional[int] = None
    last_volume: Optional[float] = None
    last_turnover: Optional[float] = None
    instrument: Optional[str] = None
    temp = destination.with_name(".%s.tmp.%d" % (destination.name, os.getpid()))

    with gzip.open(temp, "wt", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=NORMALIZED_FIELDS, lineterminator="\n")
        writer.writeheader()
        for line_no, row in iter_source_rows(source):
            instrument = instrument or row["InstrumentID"]
            if row["InstrumentID"] != instrument:
                raise ValueError("%s:%d mixes instruments" % (source, line_no))
            millis = parse_millis_of_day(row["UpdateTime"], row["UpdateMillisec"])
            label = session_label(millis, sessions)
            price = finite_float(row["LastPrice"], "LastPrice", source, line_no)
            if label is None or price <= 0:
                continue
            if last_time is not None and millis < last_time:
                continue
            volume = finite_float(row["Volume"], "Volume", source, line_no)
            turnover = finite_float(row["Turnover"], "Turnover", source, line_no)
            open_interest = finite_float(row["OpenInterest"], "OpenInterest", source, line_no)
            delta_volume = delta_turnover = 0.0
            flags: List[str] = []
            gap = 0 if last_time is None else millis - last_time
            if last_volume is not None:
                delta_volume = volume - last_volume
                if delta_volume < 0:
                    delta_volume = 0.0
                    stats.counter_resets += 1
                    flags.append("VOLUME_RESET")
                delta_turnover = turnover - last_turnover  # type: ignore[operator]
                if delta_turnover < 0:
                    delta_turnover = 0.0
                    stats.turnover_resets += 1
                    flags.append("TURNOVER_RESET")
            if gap > max_gap:
                stats.long_gaps += 1
                flags.append("LONG_GAP")
            interval_vwap: Optional[float] = None
            if delta_volume > 0:
                if delta_turnover > 0:
                    candidate = delta_turnover / (delta_volume * turnover_divisor)
                    distance_ticks = abs(candidate - price) / tick_size
                    if math.isfinite(candidate) and candidate > 0 and distance_ticks <= max_vwap_distance:
                        interval_vwap = candidate
                if interval_vwap is None:
                    # Keep both profiles volume-conserving while making degraded intervals auditable.
                    interval_vwap = price
                    stats.invalid_interval_vwap += 1
                    flags.append("VWAP_FALLBACK_LTP")
            event_time = "%s-%s-%s %s.%03d" % (
                trading_day[:4], trading_day[4:6], trading_day[6:8], row["UpdateTime"],
                int(row["UpdateMillisec"] or 0),
            )
            writer.writerow({
                "trading_day": trading_day, "instrument": instrument, "event_time": event_time,
                "millis_of_day": millis, "session": label,
                "last_price": format_number(price), "volume": format_number(volume),
                "turnover": format_number(turnover, 4), "delta_volume": format_number(delta_volume),
                "delta_turnover": format_number(delta_turnover, 4),
                "interval_vwap": "" if interval_vwap is None else format_number(interval_vwap, 10),
                "open_interest": format_number(open_interest), "gap_millis": gap,
                "quality_flags": "|".join(flags),
            })
            stats.rows += 1
            if delta_volume > 0:
                stats.positive_volume_rows += 1
                stats.assigned_volume += delta_volume
            last_time, last_volume, last_turnover = millis, volume, turnover
    os.replace(temp, destination)
    return stats


def gaussian_kernel(sigma: float) -> List[float]:
    if sigma <= 0:
        return [1.0]
    radius = max(1, int(math.ceil(4 * sigma)))
    values = [math.exp(-0.5 * (offset / sigma) ** 2) for offset in range(-radius, radius + 1)]
    total = sum(values)
    return [value / total for value in values]


def smooth_profile(values: Sequence[float], sigma: float) -> List[float]:
    if sigma <= 0:
        return list(values)
    kernel = gaussian_kernel(sigma)
    radius = len(kernel) // 2
    output = []
    for index in range(len(values)):
        value = 0.0
        for k_index, weight in enumerate(kernel):
            source = index + k_index - radius
            if 0 <= source < len(values):
                value += values[source] * weight
        output.append(value)
    original_total = sum(values)
    smoothed_total = sum(output)
    if smoothed_total > 0:
        output = [value * original_total / smoothed_total for value in output]
    return output


def load_normalized(path: Path) -> Iterator[dict]:
    with gzip.open(path, "rt", newline="", encoding="utf-8") as fh:
        yield from csv.DictReader(fh)


def build_profile(normalized: Path, root: str, meta: Mapping[str, object], config: Mapping[str, object]) -> ProfileBundle:
    tick_size = float(meta["tick_size"])
    multiplier = float(meta["multiplier"])
    histograms: Dict[str, Dict[int, float]] = {"LTP": defaultdict(float), "VWAP": defaultdict(float)}
    minute_ohlc: Dict[str, List[float]] = {}
    instrument = trading_day = ""

    for row in load_normalized(normalized):
        instrument = row["instrument"]
        trading_day = row["trading_day"]
        if row["session"] != "morning":
            continue
        price = float(row["last_price"])
        minute = row["event_time"][:16]
        if minute not in minute_ohlc:
            minute_ohlc[minute] = [price, price]
        else:
            minute_ohlc[minute][0] = min(minute_ohlc[minute][0], price)
            minute_ohlc[minute][1] = max(minute_ohlc[minute][1], price)
        delta_volume = float(row["delta_volume"])
        if delta_volume <= 0:
            continue
        histograms["LTP"][price_bin(price, tick_size)] += delta_volume
        if row["interval_vwap"]:
            interval_vwap = float(row["interval_vwap"])
            histograms["VWAP"][price_bin(interval_vwap, tick_size)] += delta_volume

    if not histograms["LTP"]:
        raise ValueError("no morning volume for %s" % normalized)
    if not histograms["VWAP"]:
        raise ValueError("no valid interval VWAP volume for %s" % normalized)
    min_bin = min(min(histograms[method]) for method in histograms)
    max_bin = max(max(histograms[method]) for method in histograms)
    sigmas = [float(value) for value in config["profile"]["smoothing_sigmas_ticks"]]  # type: ignore[index]
    profiles: Dict[str, Dict[float, List[float]]] = {}
    totals: Dict[str, float] = {}
    for method, histogram in histograms.items():
        raw = [histogram.get(index, 0.0) for index in range(min_bin, max_bin + 1)]
        totals[method] = sum(raw)
        profiles[method] = {sigma: smooth_profile(raw, sigma) for sigma in sigmas}
        for sigma, values in profiles[method].items():
            if not math.isclose(sum(values), totals[method], rel_tol=1e-9, abs_tol=1e-6):
                raise AssertionError("smoothing does not conserve volume")
    ranges = [(high - low) / tick_size for low, high in minute_ohlc.values()]
    sessions = compile_sessions(config, str(meta["session_group"]))
    available = "%s-%s-%s %s" % (
        trading_day[:4], trading_day[4:6], trading_day[6:8], sessions["available_time"]
    )
    return ProfileBundle(
        root=root, instrument=instrument, trading_day=trading_day, tick_size=tick_size,
        multiplier=multiplier, available_time=available, min_bin=min_bin, max_bin=max_bin,
        profiles=profiles, totals=totals, minute_ranges_ticks=ranges,
    )


def plateau_peak_indices(values: Sequence[float]) -> List[int]:
    peaks = []
    index = 0
    while index < len(values):
        end = index
        while end + 1 < len(values) and math.isclose(values[end + 1], values[index], rel_tol=1e-12, abs_tol=1e-12):
            end += 1
        left = values[index - 1] if index > 0 else -math.inf
        right = values[end + 1] if end + 1 < len(values) else -math.inf
        if values[index] > 0 and values[index] > left and values[end] > right:
            peaks.append((index + end) // 2)
        index = end + 1
    return peaks


def peak_properties(values: Sequence[float], index: int) -> Tuple[float, float]:
    height = values[index]
    left_min = height
    cursor = index
    while cursor > 0:
        cursor -= 1
        left_min = min(left_min, values[cursor])
        if values[cursor] > height:
            break
    right_min = height
    cursor = index
    while cursor + 1 < len(values):
        cursor += 1
        right_min = min(right_min, values[cursor])
        if values[cursor] > height:
            break
    prominence = max(0.0, height - max(left_min, right_min))
    half_height = height - prominence / 2
    left = index
    while left > 0 and values[left - 1] >= half_height:
        left -= 1
    right = index
    while right + 1 < len(values) and values[right + 1] >= half_height:
        right += 1
    return prominence, float(right - left + 1)


def detect_persistent_peaks(profile_by_sigma: Mapping[float, Sequence[float]], total: float, config: Mapping[str, object]) -> List[Peak]:
    threshold = float(config["profile"]["min_peak_prominence_share"]) * total  # type: ignore[index]
    tolerance = int(config["profile"]["method_match_ticks"])  # type: ignore[index]
    minimum_scales = int(config["profile"]["min_scale_persistence"])  # type: ignore[index]
    observations: List[Tuple[int, float, float, float]] = []
    for sigma, values in sorted(profile_by_sigma.items()):
        for index in plateau_peak_indices(values):
            prominence, width = peak_properties(values, index)
            if prominence >= threshold:
                observations.append((index, sigma, prominence, width))
    clusters: List[List[Tuple[int, float, float, float]]] = []
    for observation in sorted(observations):
        target = None
        for cluster in clusters:
            center = statistics.median(item[0] for item in cluster)
            if abs(observation[0] - center) <= tolerance and observation[1] not in {item[1] for item in cluster}:
                target = cluster
                break
        if target is None:
            clusters.append([observation])
        else:
            target.append(observation)
    peaks = []
    reference = profile_by_sigma[min(profile_by_sigma, key=lambda value: abs(value - 2.0))]
    for cluster in clusters:
        scales = {item[1] for item in cluster}
        if len(scales) < minimum_scales:
            continue
        index = int(round(statistics.median(item[0] for item in cluster)))
        peaks.append(Peak(
            index=index, value=reference[index], prominence=max(item[2] for item in cluster),
            width=statistics.median(item[3] for item in cluster), scales=scales,
        ))
    return sorted(peaks, key=lambda peak: peak.index)


def raw_value_area(values: Sequence[float], fraction: float) -> Tuple[int, int, int, int, int]:
    if not values or sum(values) <= 0:
        raise ValueError("value area requires positive profile")
    maximum = max(values)
    maxima = [index for index, value in enumerate(values) if math.isclose(value, maximum)]
    volume_center = sum(index * value for index, value in enumerate(values)) / sum(values)
    # Disconnected equal maxima are separate nodes, not one wide plateau.
    poc = min(maxima, key=lambda index: (abs(index - volume_center), index))
    poc_low = poc
    while poc_low > 0 and math.isclose(values[poc_low - 1], maximum):
        poc_low -= 1
    poc_high = poc
    while poc_high + 1 < len(values) and math.isclose(values[poc_high + 1], maximum):
        poc_high += 1
    left = right = poc
    accumulated = values[poc]
    target = sum(values) * fraction
    while accumulated < target and (left > 0 or right + 1 < len(values)):
        left_value = values[left - 1] if left > 0 else -1.0
        right_value = values[right + 1] if right + 1 < len(values) else -1.0
        if left_value == right_value and left_value >= 0:
            left -= 1
            accumulated += values[left]
            if right + 1 < len(values):
                right += 1
                accumulated += values[right]
        elif left_value > right_value:
            left -= 1
            accumulated += values[left]
        else:
            right += 1
            accumulated += values[right]
    return poc, left, right, poc_low, poc_high


def level_row(bundle: ProfileBundle, node_types: str, center_bin: float, lower_bin: float, upper_bin: float,
              source: str, volume_share: float = 0.0, prominence_share: float = 0.0,
              width_ticks: float = 1.0, persistence: int = 1, distance: float = 0.0) -> dict:
    return {
        "trading_day": bundle.trading_day, "root": bundle.root, "instrument": bundle.instrument,
        "available_time": bundle.available_time, "node_types": node_types,
        "center": format_number(center_bin * bundle.tick_size),
        "lower": format_number(lower_bin * bundle.tick_size),
        "upper": format_number(upper_bin * bundle.tick_size), "source": source,
        "raw_volume_share": "%.8f" % volume_share,
        "prominence_share": "%.8f" % prominence_share,
        "width_ticks": "%.4f" % width_ticks, "scale_persistence": str(persistence),
        "ltp_vwap_distance_ticks": "%.4f" % distance,
    }


def merge_levels(levels: List[dict], tick_size: float, merge_ticks: int) -> List[dict]:
    if not levels:
        return []
    ordered = sorted(levels, key=lambda row: float(row["center"]))
    merged = [dict(ordered[0])]
    for row in ordered[1:]:
        current = merged[-1]
        current_types = set(current["node_types"].split("+"))
        row_types = set(row["node_types"].split("+"))
        current_acceptance = bool(current_types & {"HVN", "VPOC"})
        row_acceptance = bool(row_types & {"HVN", "VPOC"})
        incompatible = ("LVN" in current_types and row_acceptance) or ("LVN" in row_types and current_acceptance)
        if not incompatible and float(row["lower"]) <= float(current["upper"]) + merge_ticks * tick_size:
            types = sorted(set(current["node_types"].split("+")) | set(row["node_types"].split("+")))
            sources = sorted(set(current["source"].split("+")) | set(row["source"].split("+")))
            low = min(float(current["lower"]), float(row["lower"]))
            high = max(float(current["upper"]), float(row["upper"]))
            current["node_types"] = "+".join(types)
            current["source"] = "+".join(sources)
            current["lower"] = format_number(low)
            current["upper"] = format_number(high)
            current["center"] = format_number((low + high) / 2)
            for key in ("raw_volume_share", "prominence_share", "width_ticks", "scale_persistence"):
                current[key] = str(max(float(current[key]), float(row[key])))
            current["ltp_vwap_distance_ticks"] = str(min(
                float(current["ltp_vwap_distance_ticks"]), float(row["ltp_vwap_distance_ticks"])
            ))
        else:
            merged.append(dict(row))
    return merged


def discover_levels(bundle: ProfileBundle, config: Mapping[str, object]) -> List[dict]:
    fraction = float(config["profile"]["value_area_fraction"])  # type: ignore[index]
    raw = bundle.profiles["LTP"][0.0]
    total = bundle.totals["LTP"]
    poc, val, vah, poc_low, poc_high = raw_value_area(raw, fraction)
    levels = [
        level_row(bundle, "VPOC", bundle.min_bin + poc, bundle.min_bin + poc_low,
                  bundle.min_bin + poc_high, "LTP_RAW", raw[poc] / total,
                  width_ticks=poc_high - poc_low + 1),
        level_row(bundle, "VAL", bundle.min_bin + val, bundle.min_bin + val - 1,
                  bundle.min_bin + val + 1, "LTP_RAW"),
        level_row(bundle, "VAH", bundle.min_bin + vah, bundle.min_bin + vah - 1,
                  bundle.min_bin + vah + 1, "LTP_RAW"),
    ]

    peaks_by_method = {
        method: detect_persistent_peaks(bundle.profiles[method], bundle.totals[method], config)
        for method in ("LTP", "VWAP")
    }
    tolerance = int(config["profile"]["method_match_ticks"])  # type: ignore[index]
    matched: List[Tuple[Peak, Peak]] = []
    used_vwap: Set[int] = set()
    for ltp in peaks_by_method["LTP"]:
        candidates = [
            (abs(ltp.index - peak.index), idx, peak)
            for idx, peak in enumerate(peaks_by_method["VWAP"])
            if idx not in used_vwap and abs(ltp.index - peak.index) <= tolerance
        ]
        if not candidates:
            continue
        distance, idx, vwap = min(candidates)
        used_vwap.add(idx)
        matched.append((ltp, vwap))
        center = bundle.min_bin + (ltp.index + vwap.index) / 2
        width = max(1.0, statistics.median([ltp.width, vwap.width]))
        levels.append(level_row(
            bundle, "HVN", center, center - width / 2, center + width / 2,
            "LTP_SMOOTH+VWAP_SMOOTH",
            volume_share=ltp.value / bundle.totals["LTP"],
            prominence_share=ltp.prominence / bundle.totals["LTP"],
            width_ticks=width, persistence=min(len(ltp.scales), len(vwap.scales)),
            distance=float(distance),
        ))

    valley_depth_min = float(config["profile"]["min_valley_depth"])  # type: ignore[index]
    minimum_scales = int(config["profile"]["min_scale_persistence"])  # type: ignore[index]
    common_sigmas = sorted(set(bundle.profiles["LTP"]) & set(bundle.profiles["VWAP"]))
    for (left_ltp, left_vwap), (right_ltp, right_vwap) in zip(matched, matched[1:]):
        if right_ltp.index - left_ltp.index < 3 or right_vwap.index - left_vwap.index < 3:
            continue
        observations = []
        for sigma in common_sigmas:
            ltp_values = bundle.profiles["LTP"][sigma]
            vwap_values = bundle.profiles["VWAP"][sigma]
            ltp_valley = min(
                range(left_ltp.index + 1, right_ltp.index), key=lambda idx: ltp_values[idx]
            )
            vwap_valley = min(
                range(left_vwap.index + 1, right_vwap.index), key=lambda idx: vwap_values[idx]
            )
            distance = abs(ltp_valley - vwap_valley)
            if distance > tolerance:
                continue
            ltp_height = min(ltp_values[left_ltp.index], ltp_values[right_ltp.index])
            vwap_height = min(vwap_values[left_vwap.index], vwap_values[right_vwap.index])
            ltp_depth = 1.0 - ltp_values[ltp_valley] / ltp_height if ltp_height > 0 else 0.0
            vwap_depth = 1.0 - vwap_values[vwap_valley] / vwap_height if vwap_height > 0 else 0.0
            depth = min(ltp_depth, vwap_depth)
            if depth >= valley_depth_min:
                observations.append(((ltp_valley + vwap_valley) / 2, distance, depth, sigma))
        if len(observations) < minimum_scales:
            continue
        median_position = statistics.median(item[0] for item in observations)
        stable = [item for item in observations if abs(item[0] - median_position) <= tolerance]
        if len(stable) < minimum_scales:
            continue
        center_offset = statistics.median(item[0] for item in stable)
        center = bundle.min_bin + center_offset
        levels.append(level_row(
            bundle, "LVN", center, center - 1, center + 1,
            "LTP_SMOOTH+VWAP_SMOOTH",
            prominence_share=statistics.median(item[2] for item in stable),
            width_ticks=2, persistence=len(stable),
            distance=statistics.median(item[1] for item in stable),
        ))

    occupied = [index for index, value in enumerate(raw) if value > 0]
    if occupied:
        levels.append(level_row(bundle, "EDGE_LOW", bundle.min_bin + occupied[0],
                                bundle.min_bin + occupied[0] - 1, bundle.min_bin + occupied[0] + 1,
                                "LTP_RAW"))
        levels.append(level_row(bundle, "EDGE_HIGH", bundle.min_bin + occupied[-1],
                                bundle.min_bin + occupied[-1] - 1, bundle.min_bin + occupied[-1] + 1,
                                "LTP_RAW"))
    return merge_levels(levels, bundle.tick_size, int(config["profile"]["merge_distance_ticks"]))  # type: ignore[index]


def profile_rows(bundle: ProfileBundle) -> Iterator[dict]:
    for method, profiles in bundle.profiles.items():
        for sigma, values in sorted(profiles.items()):
            total = bundle.totals[method]
            for offset, value in enumerate(values):
                yield {
                    "trading_day": bundle.trading_day, "root": bundle.root,
                    "instrument": bundle.instrument, "method": method,
                    "sigma_ticks": format_number(sigma),
                    "price": format_number((bundle.min_bin + offset) * bundle.tick_size),
                    "volume": format_number(value, 8),
                    "volume_share": "%.10f" % (value / total if total else 0.0),
                }


def parse_event_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f")


def evaluate_level(level: Mapping[str, object], normalized: Path, bundle: ProfileBundle,
                   config: Mapping[str, object]) -> Optional[dict]:
    afternoon = [row for row in load_normalized(normalized) if row["session"] == "afternoon"]
    if len(afternoon) < 2:
        return None
    available = datetime.strptime(str(level["available_time"]), "%Y-%m-%d %H:%M:%S")
    if any(parse_event_time(row["event_time"]) <= available for row in afternoon):
        raise AssertionError("look-ahead guard: afternoon tick is not after available_time")
    lower, upper = float(level["lower"]), float(level["upper"])
    tick = bundle.tick_size
    ranges = bundle.minute_ranges_ticks or [2.0]
    median_range = statistics.median(ranges)
    evaluation = config["evaluation"]  # type: ignore[index]
    reaction_ticks = max(int(evaluation["min_reaction_ticks"]), int(round(
        median_range * float(evaluation["reaction_median_range_fraction"])
    )))
    penetration_ticks = max(int(evaluation["min_penetration_ticks"]), int(round(
        median_range * float(evaluation["penetration_median_range_fraction"])
    )))

    previous = float(afternoon[0]["last_price"])
    touch_index = None
    role = ""
    for index in range(1, len(afternoon)):
        current = float(afternoon[index]["last_price"])
        if previous > upper and current <= upper:
            role, touch_index = "support", index
            break
        if previous < lower and current >= lower:
            role, touch_index = "resistance", index
            break
        previous = current
    if touch_index is None:
        return None

    touch = afternoon[touch_index]
    touch_price = float(touch["last_price"])
    outcome = "timeout"
    resolution = afternoon[-1]
    mfe = mae = 0.0
    for row in afternoon[touch_index:]:
        price = float(row["last_price"])
        if role == "support":
            mfe = max(mfe, (price - touch_price) / tick)
            mae = max(mae, (touch_price - price) / tick)
            if price >= upper + reaction_ticks * tick:
                outcome, resolution = "bounce", row
                break
            if price <= lower - penetration_ticks * tick:
                outcome, resolution = "break", row
                break
        else:
            mfe = max(mfe, (touch_price - price) / tick)
            mae = max(mae, (price - touch_price) / tick)
            if price <= lower - reaction_ticks * tick:
                outcome, resolution = "bounce", row
                break
            if price >= upper + penetration_ticks * tick:
                outcome, resolution = "break", row
                break
    touch_time = parse_event_time(touch["event_time"])
    resolution_time = parse_event_time(resolution["event_time"])
    event = dict(level)
    event.update({
        "role": role, "touch_time": touch["event_time"],
        "touch_price": touch["last_price"], "outcome": outcome,
        "resolution_time": resolution["event_time"],
        "resolution_price": resolution["last_price"],
        "reaction_ticks": reaction_ticks, "penetration_ticks": penetration_ticks,
        "mfe_ticks": "%.4f" % mfe, "mae_ticks": "%.4f" % mae,
        "minutes_to_resolution": "%.4f" % ((resolution_time - touch_time).total_seconds() / 60),
    })
    return event


def summarize_events(levels: Sequence[dict], events: Sequence[dict]) -> List[dict]:
    grouped_levels: Dict[str, int] = defaultdict(int)
    grouped_events: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for level in levels:
        grouped_levels[level["node_types"]] += 1
    for event in events:
        grouped_events[event["node_types"]][event["outcome"]] += 1
    rows = []
    for node_type in sorted(grouped_levels):
        outcomes = grouped_events[node_type]
        touched = sum(outcomes.values())
        resolved = outcomes["bounce"] + outcomes["break"]
        rows.append({
            "node_types": node_type, "levels": grouped_levels[node_type], "touched": touched,
            "no_touch": grouped_levels[node_type] - touched, "bounce": outcomes["bounce"],
            "break": outcomes["break"], "timeout": outcomes["timeout"],
            "bounce_rate_resolved": "%.6f" % (outcomes["bounce"] / resolved if resolved else 0.0),
        })
    return rows


def table_html(rows: Sequence[Mapping[str, object]], fields: Sequence[str], limit: Optional[int] = None) -> str:
    shown = rows if limit is None else rows[:limit]
    head = "".join("<th>%s</th>" % html.escape(field) for field in fields)
    body = "".join(
        "<tr>%s</tr>" % "".join("<td>%s</td>" % html.escape(str(row.get(field, ""))) for field in fields)
        for row in shown
    )
    return "<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (head, body)


def profile_svg(bundle: ProfileBundle, levels: Sequence[dict]) -> str:
    values = bundle.profiles["LTP"][0.0]
    width, height, margin = 760, 260, 45
    max_value = max(values) or 1.0
    points = []
    for index, value in enumerate(values):
        x = margin + value / max_value * (width - 2 * margin)
        y = height - margin - index / max(1, len(values) - 1) * (height - 2 * margin)
        points.append("%.2f,%.2f" % (x, y))
    lines = []
    low_price = bundle.min_bin * bundle.tick_size
    high_price = bundle.max_bin * bundle.tick_size
    for level in levels:
        center = float(level["center"])
        y = height - margin - (center - low_price) / max(bundle.tick_size, high_price - low_price) * (height - 2 * margin)
        lines.append(
            '<line x1="%d" y1="%.2f" x2="%d" y2="%.2f" class="level"/>'
            '<text x="%d" y="%.2f">%s %.6g</text>' %
            (margin, y, width - margin, y, width - margin + 4, y + 4,
             html.escape(level["node_types"]), center)
        )
    return (
        '<svg viewBox="0 0 %d %d" role="img">'
        '<polyline points="%s" class="profile"/>%s'
        '<text x="4" y="%d">%.6g</text><text x="4" y="16">%.6g</text></svg>' %
        (width, height, " ".join(points), "".join(lines), height - margin, low_price, high_price)
    )


def render_report(path: Path, selections: Sequence[dict], bundles: Sequence[ProfileBundle],
                  levels: Sequence[dict], events: Sequence[dict], summary: Sequence[dict], config: Mapping[str, object]) -> None:
    selected = [row for row in selections if row["selected"] == "true"]
    sections = []
    for bundle in bundles:
        bundle_levels = [row for row in levels if row["instrument"] == bundle.instrument]
        bundle_events = [row for row in events if row["instrument"] == bundle.instrument]
        sections.append(
            "<section><h2>%s <small>%s</small></h2>%s%s</section>" % (
                html.escape(bundle.instrument), html.escape(bundle.root), profile_svg(bundle, bundle_levels),
                table_html(bundle_events, ["node_types", "center", "role", "touch_time", "outcome", "mfe_ticks", "mae_ticks"]),
            )
        )
    document = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>Day-session Tick Volume Profile smoke test</title>
<style>
body{font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#17202a;background:#fff;margin:24px;line-height:1.45}
h1,h2{margin:20px 0 8px} small{color:#667085;font-weight:400} .notice{border-left:4px solid #d97706;padding:10px 14px;background:#fff7ed}
table{border-collapse:collapse;width:100%%;margin:8px 0 20px}th,td{border-bottom:1px solid #ddd;padding:6px;text-align:right}th:first-child,td:first-child{text-align:left}
section{page-break-inside:avoid;border-top:1px solid #ddd;padding-top:6px}svg{width:100%%;height:260px;background:#fafafa}.profile{fill:none;stroke:#2563eb;stroke-width:1.5}.level{stroke:#dc2626;stroke-width:1;stroke-dasharray:4 3}svg text{font-size:10px;fill:#475467}
</style><body>
<h1>纯日盘 Tick Volume Profile</h1>
<p class="notice">这是 20260109 单日、上午构造下午验证的 smoke test。它验证实现与事件定义，不代表统计显著或可交易收益。</p>
<h2>运行摘要</h2>
<p>Selected contracts: %d · Frozen levels: %d · Afternoon first-touch events: %d</p>
%s
<h2>代表合约</h2>%s
<h2>逐品种 Profile 与事件</h2>%s
</body></html>""" % (
        len(selected), len(levels), len(events),
        table_html(summary, ["node_types", "levels", "touched", "bounce", "break", "timeout", "bounce_rate_resolved"]),
        table_html(selected, ["root", "instrument", "total_volume", "nonzero_updates", "coverage", "price_bins"]),
        "".join(sections),
    )
    temp = path.with_name(".%s.tmp.%d" % (path.name, os.getpid()))
    temp.write_text(document, encoding="utf-8")
    os.replace(temp, path)


def prepare_output(output_dir: Path, overwrite: bool) -> None:
    if output_dir.exists() and any(output_dir.iterdir()):
        if not overwrite:
            raise FileExistsError("output directory is not empty; use --overwrite: %s" % output_dir)
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "selected_ticks").mkdir()


def run_pipeline(input_dir: Path, output_dir: Path, config_path: Path, overwrite: bool = False) -> dict:
    config = load_config(config_path)
    prepare_output(output_dir, overwrite)
    selections, all_stats = select_contracts(input_dir, config)
    selection_fields = [
        "root", "exchange", "selected", "instrument", "source_file", "total_volume",
        "nonzero_updates", "active_minutes", "session_minutes", "coverage", "price_bins",
        "exclusion_reasons",
    ]
    write_csv(output_dir / "selected_contracts.csv", selections, selection_fields)
    selected = [row for row in selections if row["selected"] == "true"]
    metadata = config["contracts"]
    normalized_stats = []
    bundles: List[ProfileBundle] = []
    levels: List[dict] = []
    profile_output_rows: List[dict] = []
    events: List[dict] = []

    for index, selection in enumerate(selected, 1):
        root = selection["root"]
        source = input_dir / selection["source_file"]
        destination = output_dir / "selected_ticks" / (source.stem + ".csv.gz")
        stats = normalize_selected(source, destination, root, metadata[root], config)
        normalized_stats.append({"root": root, "instrument": selection["instrument"], **asdict(stats)})
        bundle = build_profile(destination, root, metadata[root], config)
        bundles.append(bundle)
        profile_output_rows.extend(profile_rows(bundle))
        found = discover_levels(bundle, config)
        levels.extend(found)
        for level in found:
            event = evaluate_level(level, destination, bundle, config)
            if event is not None:
                events.append(event)
        print("processed %d/%d %s" % (index, len(selected), bundle.instrument), file=sys.stderr, flush=True)

    profile_fields = ["trading_day", "root", "instrument", "method", "sigma_ticks", "price", "volume", "volume_share"]
    write_csv(output_dir / "profiles.csv", profile_output_rows, profile_fields)
    write_csv(output_dir / "levels.csv", levels, LEVEL_FIELDS)
    write_csv(output_dir / "events.csv", events, EVENT_FIELDS)
    summary = summarize_events(levels, events)
    summary_fields = ["node_types", "levels", "touched", "no_touch", "bounce", "break", "timeout", "bounce_rate_resolved"]
    write_csv(output_dir / "summary.csv", summary, summary_fields)
    write_csv(output_dir / "normalization_report.csv", normalized_stats, [
        "root", "instrument", "rows", "positive_volume_rows", "assigned_volume",
        "counter_resets", "turnover_resets", "long_gaps", "invalid_interval_vwap",
    ])
    with (output_dir / "run_config.json").open("w", encoding="utf-8") as fh:
        json.dump(config, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    render_report(output_dir / "report.html", selections, bundles, levels, events, summary, config)
    result = {
        "selected_contracts": len(selected), "profiles_rows": len(profile_output_rows),
        "levels": len(levels), "events": len(events),
        "bounce": sum(event["outcome"] == "bounce" for event in events),
        "break": sum(event["outcome"] == "break" for event in events),
        "timeout": sum(event["outcome"] == "timeout" for event in events),
        "output_dir": str(output_dir),
    }
    with (output_dir / "result.json").open("w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return result


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    default_config = Path(__file__).with_name("contracts.json")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--config", type=Path, default=default_config)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        result = run_pipeline(args.input_dir, args.output_dir, args.config, args.overwrite)
    except Exception as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
