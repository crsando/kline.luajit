import csv
import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from visualize import (
    aggregate_1m,
    build_payload,
    detect_breaks,
    generate_visuals,
    lod_ticks,
)


NORMALIZED_FIELDS = [
    "trading_day", "instrument", "event_time", "millis_of_day", "session",
    "last_price", "volume", "turnover", "delta_volume", "delta_turnover",
    "interval_vwap", "open_interest", "gap_millis", "quality_flags",
]


def tick(time, millis, session, price, delta_volume=1, oi=100):
    return {
        "trading_day": "20260109", "instrument": "IF2603", "event_time": time,
        "millis_of_day": str(millis), "session": session,
        "last_price": str(price), "volume": "0", "turnover": "0",
        "delta_volume": str(delta_volume), "delta_turnover": "0",
        "interval_vwap": str(price), "open_interest": str(oi),
        "gap_millis": "500", "quality_flags": "",
    }


class VisualizeTests(unittest.TestCase):
    def test_aggregate_1m_and_break_detection(self):
        ticks = [
            tick("2026-01-09 09:30:00.000", 34200000, "morning", 100, 0),
            tick("2026-01-09 09:30:30.000", 34230000, "morning", 102, 3),
            tick("2026-01-09 09:31:00.000", 34260000, "morning", 101, 2),
            tick("2026-01-09 13:00:00.000", 46800000, "afternoon", 103, 4),
        ]
        bars = aggregate_1m(ticks)
        self.assertEqual(len(bars), 3)
        self.assertEqual((bars[0]["open"], bars[0]["high"], bars[0]["close"]), (100, 102, 102))
        self.assertEqual(bars[0]["volume"], 3)
        breaks = detect_breaks(bars)
        self.assertEqual(len(breaks), 1)
        self.assertEqual(breaks[0]["label"], "午休")
        self.assertEqual(breaks[0]["x"], 1.5)

    def test_lod_preserves_first_last_min_max(self):
        rows = [
            tick("2026-01-09 09:30:00.000", 34200000, "morning", 100),
            tick("2026-01-09 09:30:00.200", 34200200, "morning", 105),
            tick("2026-01-09 09:30:00.500", 34200500, "morning", 95),
            tick("2026-01-09 09:30:00.900", 34200900, "morning", 101),
        ]
        sampled = lod_ticks(rows)
        self.assertEqual([float(row["last_price"]) for row in sampled], [100, 105, 95, 101])

    def test_payload_starts_zone_at_afternoon_boundary(self):
        ticks = [
            tick("2026-01-09 09:30:00.000", 34200000, "morning", 100, 0),
            tick("2026-01-09 09:31:00.000", 34260000, "morning", 101, 2),
            tick("2026-01-09 13:00:00.000", 46800000, "afternoon", 102, 2),
            tick("2026-01-09 13:01:00.000", 46860000, "afternoon", 103, 2),
        ]
        profile_rows = []
        for method in ("LTP", "VWAP"):
            for sigma in ("0", "1", "2", "4"):
                for price, volume in ((99, 1), (100, 4), (101, 2), (102, 1)):
                    profile_rows.append({"method": method, "sigma_ticks": sigma, "price": str(price), "volume": str(volume)})
        levels = [{
            "node_types": "VPOC", "center": "100", "lower": "99.8", "upper": "100.2",
            "available_time": "2026-01-09 11:30:00", "source": "LTP_RAW",
            "scale_persistence": "4", "prominence_share": "0.2",
        }]
        events = [{
            **levels[0], "role": "support", "outcome": "bounce",
            "touch_time": "2026-01-09 13:00:30.000", "touch_price": "100.2",
            "mfe_ticks": "5", "mae_ticks": "1",
        }]
        config = {"contracts": {"IF": {"tick_size": 0.2}}}
        payload = build_payload(
            "IF2603", {"root": "IF", "exchange": "CFFEX"}, ticks,
            profile_rows, levels, events, config,
        )
        self.assertEqual(payload["levels"][0]["startX"], 1.5)
        self.assertGreater(payload["events"][0]["x"], payload["levels"][0]["startX"])
        self.assertEqual(sorted(payload["profiles"]), ["LTP", "VWAP"])
        self.assertEqual(len(payload["bars"]), 4)

    def test_generate_manifest_and_offline_html(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            output = root / "visual"
            (data / "selected_ticks").mkdir(parents=True)
            asset = root / "echarts.min.js"
            asset.write_text("window.echarts={};", encoding="utf-8")
            config = root / "contracts.json"
            config.write_text(json.dumps({
                "contracts": {"IF": {"tick_size": 0.2}},
                "profile": {"smoothing_sigmas_ticks": [0, 1, 2, 4]},
            }), encoding="utf-8")
            selection_fields = ["root", "exchange", "selected", "instrument", "source_file"]
            with (data / "selected_contracts.csv").open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=selection_fields)
                writer.writeheader()
                writer.writerow({"root": "IF", "exchange": "CFFEX", "selected": "true", "instrument": "IF2603", "source_file": "IF2603_20260109.csv"})
            ticks = [
                tick("2026-01-09 09:30:00.000", 34200000, "morning", 100, 0),
                tick("2026-01-09 09:31:00.000", 34260000, "morning", 101, 2),
                tick("2026-01-09 13:00:00.000", 46800000, "afternoon", 102, 2),
            ]
            with gzip.open(data / "selected_ticks" / "IF2603_20260109.csv.gz", "wt", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=NORMALIZED_FIELDS)
                writer.writeheader()
                writer.writerows(ticks)
            profile_fields = ["instrument", "method", "sigma_ticks", "price", "volume"]
            with (data / "profiles.csv").open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=profile_fields)
                writer.writeheader()
                for method in ("LTP", "VWAP"):
                    for sigma in ("0", "1", "2", "4"):
                        for price, volume in ((99, 1), (100, 4), (101, 2), (102, 1)):
                            writer.writerow({"instrument": "IF2603", "method": method, "sigma_ticks": sigma, "price": price, "volume": volume})
            level_fields = ["instrument", "node_types", "center", "lower", "upper", "available_time", "source", "scale_persistence", "prominence_share"]
            level = {"instrument": "IF2603", "node_types": "VPOC", "center": "100", "lower": "99.8", "upper": "100.2", "available_time": "2026-01-09 11:30:00", "source": "LTP_RAW", "scale_persistence": "4", "prominence_share": "0.2"}
            with (data / "levels.csv").open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=level_fields)
                writer.writeheader()
                writer.writerow(level)
            event_fields = level_fields + ["role", "outcome", "touch_time", "touch_price", "mfe_ticks", "mae_ticks"]
            with (data / "events.csv").open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=event_fields)
                writer.writeheader()
            result = generate_visuals(data, output, config, asset, None, False, False)
            self.assertEqual(result["detail_html"], 1)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["instruments"]), 1)
            detail = output / manifest["instruments"][0]["detail"]
            source = detail.read_text(encoding="utf-8")
            self.assertIn("../assets/echarts.min.js", source)
            self.assertIn("availableTime", source)
            self.assertIn("zoneRender", source)
            self.assertIn("encode:{x:1,y:0}", source)
            self.assertTrue((output / "index.html").exists())


if __name__ == "__main__":
    unittest.main()
