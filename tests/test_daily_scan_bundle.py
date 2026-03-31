from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

import daily_scan_bundle


def sample_row(symbol: str, breakout_date: str, sessions_since_breakout: int, exchange: str = "NASDAQ") -> dict[str, object]:
    return {
        "symbol": symbol,
        "name": f"{symbol} Inc.",
        "exchange": exchange,
        "breakout_date": breakout_date,
        "breakout_close": 100.0,
        "breakout_ma250": 99.5,
        "breakout_premium_pct": 0.5,
        "latest_date": "2026-03-30",
        "latest_close": 101.0,
        "latest_ma250": 100.0,
        "latest_premium_pct": 1.0 + sessions_since_breakout,
        "sessions_since_breakout": sessions_since_breakout,
        "signal_type": "today" if sessions_since_breakout == 0 else "recent_week",
    }


class BundleGenerationTests(unittest.TestCase):
    def test_generate_bundle_writes_comparison_and_watchlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)
            previous = output_dir / "breakouts_2026-03-27_3day_strict_common.csv"
            current = output_dir / "breakouts_2026-03-30_3day_strict_common.csv"

            pd.DataFrame(
                [
                    sample_row("AAPL", "2026-03-27", 0, "NASDAQ"),
                    sample_row("MSFT", "2026-03-27", 0, "NYSE"),
                ]
            ).to_csv(previous, index=False)

            pd.DataFrame(
                [
                    sample_row("AAPL", "2026-03-30", 0, "NASDAQ"),
                    sample_row("NVDA", "2026-03-30", 0, "NYSE American"),
                ]
            ).to_csv(current, index=False)

            summary = daily_scan_bundle.generate_bundle_for_scan(current, output_dir)

            added_csv = output_dir / "breakouts_2026-03-30_added_vs_2026-03-27.csv"
            added_md = output_dir / "breakouts_2026-03-30_added_vs_2026-03-27.md"
            report_md = output_dir / "breakouts_2026-03-30_3day_strict_common_report_zh.md"
            watchlist = output_dir / "tradingview_watchlist_2026-03-30_3day_strict_common.txt"

            self.assertEqual(summary["count"], 2)
            self.assertEqual(summary["added_count"], 1)
            self.assertEqual(summary["removed_count"], 1)
            self.assertTrue(added_csv.exists())
            self.assertTrue(added_md.exists())
            self.assertTrue(report_md.exists())
            self.assertTrue(watchlist.exists())
            self.assertEqual(pd.read_csv(added_csv)["symbol"].tolist(), ["NVDA"])
            self.assertEqual(watchlist.read_text(encoding="utf-8"), "NASDAQ:AAPL,AMEX:NVDA")

    def test_find_previous_scan_matches_same_variant_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)
            (output_dir / "breakouts_2026-03-27_3day_strict_common.csv").write_text("symbol\nAAPL\n", encoding="utf-8")
            (output_dir / "breakouts_2026-03-28_5day_strict_common.csv").write_text("symbol\nMSFT\n", encoding="utf-8")
            (output_dir / "breakouts_2026-03-29_3day.csv").write_text("symbol\nNVDA\n", encoding="utf-8")
            current = output_dir / "breakouts_2026-03-30_3day_strict_common.csv"
            current.write_text("symbol\nGOOG\n", encoding="utf-8")

            previous = daily_scan_bundle.find_previous_scan(current, output_dir)

            self.assertIsNotNone(previous)
            self.assertEqual(previous.path.name, "breakouts_2026-03-27_3day_strict_common.csv")


if __name__ == "__main__":
    unittest.main()
