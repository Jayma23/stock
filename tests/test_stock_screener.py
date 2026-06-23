from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pandas as pd

from stock_screener import (
    detect_breakout,
    drop_incomplete_current_day_bar,
    fetch_text,
    format_results,
    looks_like_common_equity,
    looks_like_common_symbol,
    normalize_stooq_symbol,
)


def make_history(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Date": pd.date_range("2025-01-01", periods=len(closes), freq="B"),
            "Close": closes,
        }
    )


class BreakoutLogicTests(unittest.TestCase):
    def test_detects_today_breakout(self) -> None:
        closes = [100.0] * 250 + [99.0, 101.0]
        result = detect_breakout(make_history(closes), ma_window=250, lookback_days=5)
        self.assertIsNotNone(result)
        self.assertEqual(result["signal_type"], "today")
        self.assertEqual(result["sessions_since_breakout"], 0)

    def test_detects_recent_breakout_within_week(self) -> None:
        closes = [100.0] * 250 + [99.0, 101.0, 102.0, 103.0]
        result = detect_breakout(make_history(closes), ma_window=250, lookback_days=5)
        self.assertIsNotNone(result)
        self.assertEqual(result["signal_type"], "recent_week")
        self.assertEqual(result["sessions_since_breakout"], 2)

    def test_rejects_breakout_if_latest_close_falls_back_below_ma(self) -> None:
        closes = [100.0] * 250 + [99.0, 101.0, 98.0]
        result = detect_breakout(make_history(closes), ma_window=250, lookback_days=5)
        self.assertIsNone(result)

    def test_rejects_if_latest_close_is_too_far_above_ma(self) -> None:
        closes = [100.0] * 250 + [99.0, 101.0, 110.0]
        result = detect_breakout(
            make_history(closes),
            ma_window=250,
            lookback_days=5,
            max_distance_pct=5.0,
        )
        self.assertIsNone(result)


class SymbolFormattingTests(unittest.TestCase):
    def test_rewrites_special_symbols_for_stooq(self) -> None:
        self.assertEqual(normalize_stooq_symbol("BRK.B"), "brk-b.us")
        self.assertEqual(normalize_stooq_symbol("RDS/A"), "rds-a.us")

    def test_filters_obviously_non_common_instruments(self) -> None:
        self.assertTrue(looks_like_common_equity("Acme Corp Common Stock"))
        self.assertTrue(looks_like_common_equity("Example ADR - American Depositary Shares"))
        self.assertFalse(looks_like_common_equity("Acme Warrant"))
        self.assertFalse(looks_like_common_equity("Acme Preferred Stock"))

    def test_strict_common_stock_excludes_funds_and_adrs(self) -> None:
        self.assertTrue(
            looks_like_common_equity(
                "Acme Holdings Class A Common Stock",
                strict_common_stock=True,
            )
        )
        self.assertFalse(
            looks_like_common_equity(
                "Example Growth Fund, Inc. Common Stock",
                strict_common_stock=True,
            )
        )
        self.assertFalse(
            looks_like_common_equity(
                "Example ADR - American Depositary Shares",
                strict_common_stock=True,
            )
        )

    def test_filters_obviously_non_common_symbol_shapes(self) -> None:
        self.assertTrue(looks_like_common_symbol("BRK.B"))
        self.assertFalse(looks_like_common_symbol("AMH$G"))
        self.assertFalse(looks_like_common_symbol("AIIA-U"))


class FetchResilienceTests(unittest.TestCase):
    def test_fetch_text_falls_back_to_curl_when_requests_returns_invalid_html(self) -> None:
        invalid_response = Mock()
        invalid_response.raise_for_status.return_value = None
        invalid_response.text = "<html>blocked</html>"
        session = Mock()
        session.get.return_value = invalid_response

        with TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "nasdaqlisted.txt"
            with patch("stock_screener.fetch_text_with_curl", return_value="Symbol|Security Name|\nAAPL|Apple Inc. Common Stock|\n") as curl_fetch:
                result = fetch_text(
                    "https://example.com/nasdaqlisted.txt",
                    cache_path,
                    refresh=True,
                    max_age_hours=20,
                    session=session,
                    validator=lambda text: text.startswith("Symbol|Security Name|"),
                    validation_label="invalid directory",
                )
                cached_text = cache_path.read_text(encoding="utf-8")

        self.assertIn("AAPL|Apple", result)
        self.assertEqual(cached_text, result)
        curl_fetch.assert_called_once()

    def test_format_results_preserves_headers_for_empty_runs(self) -> None:
        frame = format_results([])
        self.assertFalse(frame.columns.empty)
        self.assertEqual(frame.shape[0], 0)
        self.assertIn("symbol", frame.columns.tolist())


class DailyCloseGuardTests(unittest.TestCase):
    def test_drops_same_day_bar_before_market_close(self) -> None:
        frame = pd.DataFrame(
            {
                "Date": pd.to_datetime(["2026-03-18", "2026-03-19"]),
                "Close": [100.0, 101.0],
            }
        )
        result = drop_incomplete_current_day_bar(
            frame,
            now=datetime(2026, 3, 19, 12, 59, tzinfo=ZoneInfo("America/New_York")),
        )
        self.assertEqual(result["Date"].dt.strftime("%Y-%m-%d").tolist(), ["2026-03-18"])

    def test_keeps_same_day_bar_after_market_close(self) -> None:
        frame = pd.DataFrame(
            {
                "Date": pd.to_datetime(["2026-03-18", "2026-03-19"]),
                "Close": [100.0, 101.0],
            }
        )
        result = drop_incomplete_current_day_bar(
            frame,
            now=datetime(2026, 3, 19, 16, 1, tzinfo=ZoneInfo("America/New_York")),
        )
        self.assertEqual(result["Date"].dt.strftime("%Y-%m-%d").tolist(), ["2026-03-18", "2026-03-19"])


if __name__ == "__main__":
    unittest.main()
