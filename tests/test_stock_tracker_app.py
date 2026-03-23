from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

import stock_tracker_app


class ScanFileFilteringTests(unittest.TestCase):
    def test_includes_primary_scan_files_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)
            primary = output_dir / "breakouts_2026-03-21_3day_strict_common.csv"
            comparison = output_dir / "breakouts_2026-03-21_added_vs_2026-03-19.csv"
            report = output_dir / "breakouts_2026-03-21_report.csv"
            primary.write_text("symbol\nAAPL\n", encoding="utf-8")
            comparison.write_text("symbol\nMSFT\n", encoding="utf-8")
            report.write_text("symbol\nNVDA\n", encoding="utf-8")

            original_output_dir = stock_tracker_app.OUTPUT_DIR
            try:
                stock_tracker_app.OUTPUT_DIR = output_dir
                files = stock_tracker_app.list_scan_files()
            finally:
                stock_tracker_app.OUTPUT_DIR = original_output_dir

        self.assertEqual([path.name for path in files], [primary.name])


class PriceLoadingTests(unittest.TestCase):
    def test_load_price_frame_downloads_and_caches_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)
            downloaded = pd.DataFrame(
                {
                    "Date": pd.to_datetime(["2026-03-19", "2026-03-20"]),
                    "Open": [10.0, 10.5],
                    "High": [10.8, 11.0],
                    "Low": [9.9, 10.4],
                    "Close": [10.6, 10.9],
                    "Volume": [1000, 1200],
                }
            )

            original_cache_dir = stock_tracker_app.CACHE_DIR
            original_download_price_frame = stock_tracker_app.download_price_frame
            try:
                stock_tracker_app.CACHE_DIR = cache_dir
                stock_tracker_app.download_price_frame = lambda symbol: downloaded.copy()
                frame = stock_tracker_app.load_price_frame("AAPL")
                cache_file_exists = (cache_dir / "prices" / "yfinance" / "AAPL.csv").exists()
            finally:
                stock_tracker_app.CACHE_DIR = original_cache_dir
                stock_tracker_app.download_price_frame = original_download_price_frame

        self.assertIsNotNone(frame)
        self.assertEqual(frame["Close"].tolist(), [10.6, 10.9])
        self.assertTrue(cache_file_exists)


if __name__ == "__main__":
    unittest.main()
