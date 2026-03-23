from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
