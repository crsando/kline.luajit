import csv
import gzip
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from market_profile import (
    NORMALIZED_FIELDS,
    ProfileBundle,
    build_profile,
    compile_sessions,
    detect_persistent_peaks,
    evaluate_level,
    gaussian_kernel,
    load_normalized,
    normalize_selected,
    raw_value_area,
    select_contracts,
    session_label,
    smooth_profile,
)


SOURCE_HEADER = [
    "TradingDay", "InstrumentID", "UpdateTime", "UpdateMillisec", "LastPrice",
    "Volume", "BidPrice1", "BidVolume1", "AskPrice1", "AskVolume1",
    "AveragePrice", "Turnover", "OpenInterest", "UpperLimitPrice", "LowerLimitPrice",
]


def minimal_config():
    return {
        "schema_version": 1,
        "session_groups": {
            "index": {
                "morning": [["09:30", "11:30"]],
                "afternoon": [["13:00", "15:00"]],
                "available_time": "11:30:00",
            }
        },
        "contracts": {
            "IF": {
                "exchange": "CFFEX", "session_group": "index",
                "tick_size": 0.2, "multiplier": 300,
            }
        },
        "liquidity": {
            "min_volume": 1, "min_nonzero_updates": 1,
            "min_active_minute_coverage": 0, "min_price_bins": 1,
        },
        "profile": {
            "value_area_fraction": 0.7,
            "smoothing_sigmas_ticks": [0, 1, 2, 4],
            "min_scale_persistence": 3,
            "method_match_ticks": 2,
            "min_peak_prominence_share": 0.01,
            "min_valley_depth": 0.25,
            "merge_distance_ticks": 1,
            "max_gap_millis": 5000,
            "max_interval_vwap_distance_ticks": 100,
        },
        "evaluation": {
            "reaction_median_range_fraction": 0.5,
            "penetration_median_range_fraction": 0.25,
            "min_reaction_ticks": 2,
            "min_penetration_ticks": 1,
        },
    }


def source_row(instrument, time, price, volume, turnover):
    return {
        "TradingDay": "20260109", "InstrumentID": instrument,
        "UpdateTime": time, "UpdateMillisec": "0", "LastPrice": str(price),
        "Volume": str(volume), "BidPrice1": str(price - 0.2), "BidVolume1": "1",
        "AskPrice1": str(price + 0.2), "AskVolume1": "1", "AveragePrice": "0",
        "Turnover": str(turnover), "OpenInterest": "100", "UpperLimitPrice": "9999",
        "LowerLimitPrice": "1",
    }


def write_source(path, instrument, final_volume, afternoon_volume=None):
    afternoon_volume = final_volume + 1 if afternoon_volume is None else afternoon_volume
    rows = [
        source_row(instrument, "09:29:00", 100, 0, 0),
        source_row(instrument, "09:30:00", 100, 10, 300000),
        source_row(instrument, "09:30:01", 100.2, final_volume, final_volume * 100.2 * 300),
        source_row(instrument, "13:00:00", 101, afternoon_volume, afternoon_volume * 101 * 300),
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SOURCE_HEADER, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


class MarketProfileTests(unittest.TestCase):
    def test_session_boundaries_are_left_closed_right_open(self):
        sessions = compile_sessions(minimal_config(), "index")
        self.assertIsNone(session_label((9 * 60 + 29) * 60000, sessions))
        self.assertEqual(session_label((9 * 60 + 30) * 60000, sessions), "morning")
        self.assertIsNone(session_label((11 * 60 + 30) * 60000, sessions))
        self.assertEqual(session_label(13 * 60 * 60000, sessions), "afternoon")
        self.assertIsNone(session_label(15 * 60 * 60000, sessions))

    def test_gaussian_smoothing_conserves_volume(self):
        values = [0, 10, 0, 20, 0, 5]
        for sigma in (0, 1, 2, 4):
            smoothed = smooth_profile(values, sigma)
            self.assertTrue(math.isclose(sum(smoothed), sum(values), rel_tol=1e-12))
            self.assertTrue(all(value >= 0 for value in smoothed))
        self.assertTrue(math.isclose(sum(gaussian_kernel(2)), 1.0))

    def test_raw_value_area_keeps_raw_poc(self):
        values = [0, 2, 10, 4, 2, 0]
        poc, val, vah, poc_low, poc_high = raw_value_area(values, 0.7)
        self.assertEqual((poc, poc_low, poc_high), (2, 2, 2))
        self.assertLessEqual(val, poc)
        self.assertGreaterEqual(vah, poc)
        self.assertGreaterEqual(sum(values[val:vah + 1]), sum(values) * 0.7)
        disconnected = [10, 0, 10]
        _, _, _, plateau_low, plateau_high = raw_value_area(disconnected, 0.7)
        self.assertEqual((plateau_low, plateau_high), (0, 0))

    def test_persistent_peak_detection(self):
        raw = [0.0] * 31
        raw[8] = 100
        raw[22] = 80
        raw[7] = raw[9] = 25
        raw[21] = raw[23] = 20
        profiles = {sigma: smooth_profile(raw, sigma) for sigma in (0, 1, 2, 4)}
        peaks = detect_persistent_peaks(profiles, sum(raw), minimal_config())
        indices = [peak.index for peak in peaks]
        self.assertTrue(any(abs(index - 8) <= 1 for index in indices))
        self.assertTrue(any(abs(index - 22) <= 1 for index in indices))
        self.assertTrue(all(len(peak.scales) >= 3 for peak in peaks))

    def test_representative_contract_is_highest_volume_concrete_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_source(root / "IF2601_20260109.csv", "IF2601", 20)
            write_source(root / "IF2603_20260109.csv", "IF2603", 50)
            write_source(root / "IF主力连续_20260109.csv", "IF2603", 100)
            rows, _ = select_contracts(root, minimal_config())
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["instrument"], "IF2603")
            self.assertEqual(rows[0]["source_file"], "IF2603_20260109.csv")
            self.assertEqual(rows[0]["selected"], "true")

    def test_representative_selection_does_not_use_afternoon_volume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_source(root / "IF2601_20260109.csv", "IF2601", 50, afternoon_volume=51)
            write_source(root / "IF2603_20260109.csv", "IF2603", 20, afternoon_volume=500)
            rows, _ = select_contracts(root, minimal_config())
            self.assertEqual(rows[0]["instrument"], "IF2601")

    def test_normalization_differences_cumulative_volume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "IF2603_20260109.csv"
            write_source(source, "IF2603", 50)
            destination = root / "ticks.csv.gz"
            stats = normalize_selected(
                source, destination, "IF", minimal_config()["contracts"]["IF"], minimal_config()
            )
            rows = list(load_normalized(destination))
            self.assertEqual(float(rows[0]["delta_volume"]), 0)
            self.assertEqual(float(rows[1]["delta_volume"]), 40)
            self.assertEqual(float(rows[2]["delta_volume"]), 1)
            self.assertEqual(stats.assigned_volume, 41)
            self.assertTrue(rows[1]["interval_vwap"])
            bundle = build_profile(
                destination, "IF", minimal_config()["contracts"]["IF"], minimal_config()
            )
            self.assertEqual(bundle.totals["LTP"], bundle.totals["VWAP"])

    def test_afternoon_event_uses_frozen_morning_level(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ticks.csv.gz"
            fields = [
                {"time": "13:00:00.000", "price": 110},
                {"time": "13:00:01.000", "price": 104},
                {"time": "13:00:02.000", "price": 101},
                {"time": "13:00:03.000", "price": 103},
            ]
            with gzip.open(path, "wt", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=NORMALIZED_FIELDS, lineterminator="\n")
                writer.writeheader()
                for item in fields:
                    writer.writerow({
                        "trading_day": "20260109", "instrument": "TEST",
                        "event_time": "2026-01-09 " + item["time"],
                        "millis_of_day": "", "session": "afternoon",
                        "last_price": item["price"], "volume": 0, "turnover": 0,
                        "delta_volume": 0, "delta_turnover": 0, "interval_vwap": "",
                        "open_interest": 0, "gap_millis": 1000, "quality_flags": "",
                    })
            bundle = ProfileBundle(
                root="X", instrument="TEST", trading_day="20260109", tick_size=1,
                multiplier=1, available_time="2026-01-09 11:30:00", min_bin=90,
                max_bin=110, profiles={}, totals={}, minute_ranges_ticks=[4],
            )
            level = {
                "trading_day": "20260109", "root": "X", "instrument": "TEST",
                "available_time": "2026-01-09 11:30:00", "node_types": "HVN",
                "center": "100", "lower": "99", "upper": "101", "source": "TEST",
                "raw_volume_share": "0", "prominence_share": "0", "width_ticks": "2",
                "scale_persistence": "4", "ltp_vwap_distance_ticks": "0",
            }
            event = evaluate_level(level, path, bundle, minimal_config())
            self.assertIsNotNone(event)
            self.assertEqual(event["role"], "support")
            self.assertEqual(event["outcome"], "bounce")
            bad = dict(level)
            bad["available_time"] = "2026-01-09 14:00:00"
            with self.assertRaises(AssertionError):
                evaluate_level(bad, path, bundle, minimal_config())


if __name__ == "__main__":
    unittest.main()
