from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sqlite3
from datetime import datetime
from functools import partial
from html import escape
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = Path(os.environ.get("STOCK_TRACKER_OUTPUT_DIR", BASE_DIR / "output")).resolve()
DATA_DIR = Path(os.environ.get("STOCK_TRACKER_DATA_DIR", BASE_DIR / "data")).resolve()
WEB_DIR = BASE_DIR / "tracker_web"
CACHE_DIR = Path(os.environ.get("STOCK_TRACKER_CACHE_DIR", BASE_DIR / ".cache")).resolve()
JOURNAL_PATH = DATA_DIR / "stock_journal.json"
NOTES_DB_PATH = Path(os.environ.get("STOCK_TRACKER_DB_PATH", DATA_DIR / "stock_tracker.db")).resolve()
LIGHTWEIGHT_CHARTS_URL = "https://unpkg.com/lightweight-charts@5.0.9/dist/lightweight-charts.standalone.production.js"
AUTH_USER = os.environ.get("STOCK_TRACKER_BASIC_AUTH_USER", "").strip()
AUTH_PASSWORD = os.environ.get("STOCK_TRACKER_BASIC_AUTH_PASSWORD", "").strip()

STATUS_OPTIONS = ["观察", "研究中", "候选", "已排除", "已持有"]
SCAN_FILE_PATTERN = re.compile(
    r"^breakouts_\d{4}-\d{2}-\d{2}(?:_3day)?(?:_strict_common)?\.csv$"
)


def ensure_journal_file() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not JOURNAL_PATH.exists():
        JOURNAL_PATH.write_text("{}\n", encoding="utf-8")


def get_db_connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(NOTES_DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def migrate_legacy_journal(connection: sqlite3.Connection) -> None:
    ensure_journal_file()
    row = connection.execute("SELECT COUNT(*) AS count FROM notes").fetchone()
    if row is None or int(row["count"]) != 0:
        return

    with JOURNAL_PATH.open("r", encoding="utf-8") as handle:
        legacy_payload = json.load(handle)

    if not legacy_payload:
        return

    connection.executemany(
        """
        INSERT OR REPLACE INTO notes (symbol, status, note, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        [
            (
                str(symbol).strip().upper(),
                str(item.get("status") or "观察"),
                str(item.get("note") or ""),
                str(item.get("updated_at") or datetime.now().isoformat(timespec="seconds")),
            )
            for symbol, item in legacy_payload.items()
        ],
    )
    connection.commit()


def init_storage() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with get_db_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS notes (
                symbol TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                note TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.commit()
        migrate_legacy_journal(connection)


def load_journal() -> dict[str, dict[str, Any]]:
    ensure_journal_file()
    with JOURNAL_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_journal(payload: dict[str, dict[str, Any]]) -> None:
    ensure_journal_file()
    JOURNAL_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def tradingview_symbol(exchange: str, symbol: str) -> str:
    exchange_map = {
        "NASDAQ": "NASDAQ",
        "NYSE": "NYSE",
        "NYSE American": "AMEX",
    }
    prefix = exchange_map.get(exchange, exchange)
    return f"{prefix}:{symbol}"


def safe_symbol_filename(symbol: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in symbol.upper())


def scan_label(path: Path) -> str:
    stem = path.stem
    date_part = stem.replace("breakouts_", "").split("_")[0]
    labels: list[str] = [date_part]
    if "3day" in stem:
        labels.append("3天内")
    else:
        labels.append("5天内")

    if "strict_common" in stem:
        labels.append("严格普通股")
    else:
        labels.append("默认过滤")

    return " / ".join(labels)


def scan_sort_key(path: Path) -> tuple[str, int, int]:
    stem = path.stem
    strict = 1 if "strict_common" in stem else 0
    days = 3 if "3day" in stem else 5
    return (stem, strict, -days)


def is_primary_scan_file(path: Path) -> bool:
    return bool(SCAN_FILE_PATTERN.fullmatch(path.name))


def list_scan_files() -> list[Path]:
    candidates = (path for path in OUTPUT_DIR.glob("breakouts_*.csv") if is_primary_scan_file(path))
    return sorted(candidates, key=scan_sort_key, reverse=True)


def sanitize_scan_filename(filename: str) -> Path:
    candidate = (OUTPUT_DIR / filename).resolve()
    if candidate.parent != OUTPUT_DIR.resolve() or not candidate.exists() or candidate.suffix.lower() != ".csv":
        raise FileNotFoundError(filename)
    return candidate


def dataframe_to_rows(frame: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in frame.to_dict(orient="records"):
        row = {
            "symbol": raw.get("symbol"),
            "name": raw.get("name"),
            "exchange": raw.get("exchange"),
            "breakout_date": raw.get("breakout_date"),
            "sessions_since_breakout": int(raw.get("sessions_since_breakout", 0)),
            "latest_close": float(raw.get("latest_close", 0.0)),
            "latest_ma250": float(raw.get("latest_ma250", 0.0)),
            "latest_premium_pct": float(raw.get("latest_premium_pct", 0.0)),
            "signal_type": raw.get("signal_type"),
        }
        row["tv_symbol"] = tradingview_symbol(row["exchange"], row["symbol"])
        rows.append(row)
    return rows


def load_scan(path: Path) -> dict[str, Any]:
    frame = pd.read_csv(path)
    rows = dataframe_to_rows(frame)
    return {
        "file": path.name,
        "label": scan_label(path),
        "count": len(rows),
        "rows": rows,
    }


def list_scans() -> list[dict[str, Any]]:
    scans: list[dict[str, Any]] = []
    for path in list_scan_files():
        frame = pd.read_csv(path)
        scans.append(
            {
                "file": path.name,
                "label": scan_label(path),
                "count": len(frame.index),
                "updated_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
            }
        )
    return scans


def get_symbol_history(symbol: str) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    for path in list_scan_files():
        frame = pd.read_csv(path)
        subset = frame.loc[frame["symbol"] == symbol]
        if subset.empty:
            continue
        raw = subset.iloc[0].to_dict()
        history.append(
            {
                "file": path.name,
                "label": scan_label(path),
                "breakout_date": raw.get("breakout_date"),
                "sessions_since_breakout": int(raw.get("sessions_since_breakout", 0)),
                "latest_close": float(raw.get("latest_close", 0.0)),
                "latest_ma250": float(raw.get("latest_ma250", 0.0)),
                "latest_premium_pct": float(raw.get("latest_premium_pct", 0.0)),
            }
        )
    return history


def load_price_frame(symbol: str) -> pd.DataFrame | None:
    cache_name = f"{safe_symbol_filename(symbol)}.csv"
    candidates = [
        CACHE_DIR / "prices" / "yfinance" / cache_name,
        CACHE_DIR / "prices" / "stooq" / cache_name,
    ]

    for path in candidates:
        if not path.exists():
            continue

        frame = pd.read_csv(path, parse_dates=["Date"])
        if frame.empty or "Close" not in frame.columns:
            continue

        normalized = frame.copy()
        for column in ("Open", "High", "Low"):
            if column not in normalized.columns:
                normalized[column] = normalized["Close"]
        if "Volume" not in normalized.columns:
            normalized["Volume"] = 0

        numeric_columns = ["Open", "High", "Low", "Close", "Volume"]
        for column in numeric_columns:
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")

        normalized = normalized.dropna(subset=["Date", "Open", "High", "Low", "Close"])
        normalized = normalized.drop_duplicates(subset=["Date"]).sort_values("Date").reset_index(drop=True)
        if not normalized.empty:
            return normalized

    return None


def build_price_payload(symbol: str, bars: int = 420) -> dict[str, Any] | None:
    frame = load_price_frame(symbol)
    if frame is None or len(frame.index) < 2:
        return None

    window = max(bars, 280)
    frame = frame.tail(window).copy()
    frame["ma120"] = frame["Close"].rolling(window=120, min_periods=120).mean()
    frame["ma250"] = frame["Close"].rolling(window=250, min_periods=250).mean()

    candles: list[dict[str, Any]] = []
    volume: list[dict[str, Any]] = []
    ma120: list[dict[str, Any]] = []
    ma250: list[dict[str, Any]] = []

    for raw in frame.to_dict(orient="records"):
        date_key = pd.Timestamp(raw["Date"]).strftime("%Y-%m-%d")
        open_price = float(raw["Open"])
        close_price = float(raw["Close"])
        candle = {
            "time": date_key,
            "open": round(open_price, 4),
            "high": round(float(raw["High"]), 4),
            "low": round(float(raw["Low"]), 4),
            "close": round(close_price, 4),
        }
        candles.append(candle)
        volume.append(
            {
                "time": date_key,
                "value": int(float(raw.get("Volume", 0.0) or 0.0)),
                "color": "#14b8a6" if close_price >= open_price else "#f87171",
            }
        )

        if pd.notna(raw["ma120"]):
            ma120.append({"time": date_key, "value": round(float(raw["ma120"]), 4)})
        if pd.notna(raw["ma250"]):
            ma250.append({"time": date_key, "value": round(float(raw["ma250"]), 4)})

    latest = frame.iloc[-1]
    latest_ma120 = None if pd.isna(latest["ma120"]) else round(float(latest["ma120"]), 4)
    latest_ma250 = None if pd.isna(latest["ma250"]) else round(float(latest["ma250"]), 4)

    return {
        "symbol": symbol,
        "candles": candles,
        "volume": volume,
        "ma120": ma120,
        "ma250": ma250,
        "latest": {
            "date": pd.Timestamp(latest["Date"]).strftime("%Y-%m-%d"),
            "close": round(float(latest["Close"]), 4),
            "ma120": latest_ma120,
            "ma250": latest_ma250,
        },
    }


def get_symbol_notes(symbol: str) -> dict[str, Any]:
    normalized_symbol = str(symbol).strip().upper()
    with get_db_connection() as connection:
        row = connection.execute(
            """
            SELECT symbol, status, note, updated_at
            FROM notes
            WHERE symbol = ?
            """,
            (normalized_symbol,),
        ).fetchone()

    if row is None:
        return {
            "symbol": normalized_symbol,
            "status": "观察",
            "note": "",
            "updated_at": None,
        }

    return {
        "symbol": row["symbol"],
        "status": row["status"],
        "note": row["note"],
        "updated_at": row["updated_at"],
    }


def update_symbol_notes(symbol: str, status: str, note: str) -> dict[str, Any]:
    normalized_symbol = str(symbol).strip().upper()
    payload = {
        "symbol": normalized_symbol,
        "status": status if status in STATUS_OPTIONS else "观察",
        "note": note,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }

    with get_db_connection() as connection:
        connection.execute(
            """
            INSERT INTO notes (symbol, status, note, updated_at)
            VALUES (:symbol, :status, :note, :updated_at)
            ON CONFLICT(symbol) DO UPDATE SET
                status = excluded.status,
                note = excluded.note,
                updated_at = excluded.updated_at
            """,
            payload,
        )
        connection.commit()
    return payload


def build_chart_page(symbol: str, scan_file: str | None) -> str:
    del scan_file
    safe_title = escape(symbol or "AAPL")
    symbol_json = json.dumps(symbol or "AAPL", ensure_ascii=False)
    charts_url_json = json.dumps(LIGHTWEIGHT_CHARTS_URL, ensure_ascii=False)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>{safe_title}</title>
    <style>
      html, body {{
        width: 100%;
        height: 100%;
        margin: 0;
        background: #ffffff;
        overflow: hidden;
        color: #1f2937;
        font-family: "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif;
      }}

      * {{
        box-sizing: border-box;
      }}

      .chart-shell {{
        width: 100%;
        height: 100%;
        display: flex;
        flex-direction: column;
        background:
          radial-gradient(circle at top left, rgba(37, 99, 235, 0.08), transparent 24%),
          linear-gradient(180deg, #ffffff 0%, #f8fafc 100%);
      }}

      .topbar {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 16px;
        padding: 12px 16px 8px;
        border-bottom: 1px solid rgba(148, 163, 184, 0.2);
        background: rgba(255, 255, 255, 0.84);
        backdrop-filter: blur(8px);
      }}

      .title-wrap {{
        display: flex;
        flex-direction: column;
        gap: 4px;
      }}

      .title-wrap strong {{
        font-size: 15px;
      }}

      .title-wrap span {{
        font-size: 12px;
        color: #64748b;
      }}

      .legend {{
        display: flex;
        flex-wrap: wrap;
        justify-content: flex-end;
        gap: 8px;
      }}

      .legend-pill {{
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 6px 10px;
        border-radius: 999px;
        border: 1px solid rgba(148, 163, 184, 0.24);
        background: rgba(255, 255, 255, 0.88);
        font-size: 12px;
        color: #475569;
      }}

      .legend-pill strong {{
        font-size: 13px;
      }}

      .legend-close strong {{
        color: #111827;
      }}

      .legend-ma120 {{
        border-color: rgba(37, 99, 235, 0.22);
      }}

      .legend-ma120 strong {{
        color: #2563eb;
      }}

      .legend-ma250 {{
        border-color: rgba(225, 29, 72, 0.22);
      }}

      .legend-ma250 strong {{
        color: #e11d48;
      }}

      #chart {{
        flex: 1;
        min-height: 0;
      }}

      .status-bar {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 16px;
        padding: 10px 16px;
        border-top: 1px solid rgba(148, 163, 184, 0.2);
        font-size: 12px;
        color: #64748b;
        background: rgba(255, 255, 255, 0.92);
      }}

      .status-bar a {{
        color: #14532d;
        text-decoration: none;
        font-weight: 700;
      }}
    </style>
  </head>
  <body>
    <div class="chart-shell">
      <div class="topbar">
        <div class="title-wrap">
          <strong id="symbol-label">{safe_title}</strong>
          <span>可缩放、可拖拽，默认显示 120MA / 250MA</span>
        </div>
        <div class="legend">
          <span class="legend-pill legend-close">收盘 <strong id="legend-close">--</strong></span>
          <span class="legend-pill legend-ma120">120MA <strong id="legend-ma120">--</strong></span>
          <span class="legend-pill legend-ma250">250MA <strong id="legend-ma250">--</strong></span>
        </div>
      </div>
      <div id="chart"></div>
      <div class="status-bar">
        <span id="status-text">正在加载图表数据...</span>
        <a id="open-link" href="https://www.tradingview.com/" target="_blank" rel="noreferrer">在 TradingView 打开</a>
      </div>
    </div>
    <script src={charts_url_json}></script>
    <script>
      const symbol = {symbol_json};
      const chartHost = document.getElementById("chart");
      const statusText = document.getElementById("status-text");
      const openLink = document.getElementById("open-link");
      const closeEl = document.getElementById("legend-close");
      const ma120El = document.getElementById("legend-ma120");
      const ma250El = document.getElementById("legend-ma250");

      openLink.href = `https://www.tradingview.com/symbols/${{symbol.replace(":", "-")}}/`;

      function formatValue(value) {{
        if (value === null || value === undefined || Number.isNaN(Number(value))) {{
          return "--";
        }}
        return Number(value).toFixed(2);
      }}

      function setLegend(values) {{
        closeEl.textContent = formatValue(values.close);
        ma120El.textContent = formatValue(values.ma120);
        ma250El.textContent = formatValue(values.ma250);
      }}

      async function loadPrices() {{
        const response = await fetch(`/api/prices?symbol=${{encodeURIComponent(symbol.split(":").pop())}}`);
        if (!response.ok) {{
          throw new Error(`价格数据加载失败: ${{response.status}}`);
        }}
        return response.json();
      }}

      function buildChart(payload) {{
        const chart = LightweightCharts.createChart(chartHost, {{
          width: chartHost.clientWidth,
          height: chartHost.clientHeight,
          layout: {{
            background: {{ color: "#ffffff" }},
            textColor: "#475569",
            attributionLogo: false,
          }},
          grid: {{
            vertLines: {{ color: "#eef2f7" }},
            horzLines: {{ color: "#eef2f7" }},
          }},
          rightPriceScale: {{
            borderColor: "#e2e8f0",
            scaleMargins: {{ top: 0.08, bottom: 0.24 }},
          }},
          timeScale: {{
            borderColor: "#e2e8f0",
            timeVisible: true,
            secondsVisible: false,
          }},
          crosshair: {{
            mode: LightweightCharts.CrosshairMode.Normal,
          }},
          localization: {{
            locale: "zh-CN",
          }},
          handleScroll: {{
            mouseWheel: true,
            pressedMouseMove: true,
            horzTouchDrag: true,
            vertTouchDrag: false,
          }},
          handleScale: {{
            axisPressedMouseMove: true,
            mouseWheel: true,
            pinch: true,
          }},
        }});

        const candleSeries = chart.addSeries(LightweightCharts.CandlestickSeries, {{
          upColor: "#14b8a6",
          downColor: "#ef4444",
          borderVisible: false,
          wickUpColor: "#14b8a6",
          wickDownColor: "#f87171",
          priceLineVisible: true,
          lastValueVisible: true,
        }});

        const ma120Series = chart.addSeries(LightweightCharts.LineSeries, {{
          color: "#2563eb",
          lineWidth: 2,
          priceLineVisible: false,
          lastValueVisible: true,
          crosshairMarkerVisible: false,
        }});

        const ma250Series = chart.addSeries(LightweightCharts.LineSeries, {{
          color: "#e11d48",
          lineWidth: 2,
          priceLineVisible: false,
          lastValueVisible: true,
          crosshairMarkerVisible: false,
        }});

        const volumeSeries = chart.addSeries(LightweightCharts.HistogramSeries, {{
          priceFormat: {{ type: "volume" }},
          priceScaleId: "",
        }});

        volumeSeries.priceScale().applyOptions({{
          scaleMargins: {{
            top: 0.78,
            bottom: 0,
          }},
        }});

        candleSeries.setData(payload.candles);
        ma120Series.setData(payload.ma120);
        ma250Series.setData(payload.ma250);
        volumeSeries.setData(payload.volume);
        chart.timeScale().fitContent();
        setLegend(payload.latest);
        statusText.textContent = `本地交互图，最新数据到 ${{payload.latest.date}}。鼠标滚轮缩放，拖拽平移。`;

        chart.subscribeCrosshairMove((param) => {{
          if (!param.time) {{
            setLegend(payload.latest);
            return;
          }}

          const candle = param.seriesData.get(candleSeries);
          const ma120Point = param.seriesData.get(ma120Series);
          const ma250Point = param.seriesData.get(ma250Series);
          setLegend({{
            close: candle?.close ?? payload.latest.close,
            ma120: ma120Point?.value ?? payload.latest.ma120,
            ma250: ma250Point?.value ?? payload.latest.ma250,
          }});
        }});

        const resizeObserver = new ResizeObserver(() => {{
          chart.applyOptions({{
            width: chartHost.clientWidth,
            height: chartHost.clientHeight,
          }});
        }});
        resizeObserver.observe(chartHost);
      }}

      loadPrices()
        .then((payload) => {{
          if (!payload.candles?.length) {{
            throw new Error("没有可用价格数据");
          }}
          buildChart(payload);
        }})
        .catch((error) => {{
          statusText.textContent = error.message;
          chartHost.innerHTML = '<div style="padding:24px;color:#64748b">这只股票暂时没有可用的K线缓存数据。</div>';
        }});
    </script>
  </body>
</html>
"""


class StockTrackerHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, directory: str | None = None, **kwargs: Any) -> None:
        super().__init__(*args, directory=directory or str(WEB_DIR), **kwargs)

    def is_authorized(self) -> bool:
        if not AUTH_USER or not AUTH_PASSWORD:
            return True

        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False

        try:
            decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        except Exception:
            return False

        return decoded == f"{AUTH_USER}:{AUTH_PASSWORD}"

    def require_authorization(self) -> bool:
        if self.is_authorized():
            return True

        body = "需要访问账号密码".encode("utf-8")
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("WWW-Authenticate", 'Basic realm="Stock Tracker"')
        self.end_headers()
        self.wfile.write(body)
        return False

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_api_get(self, parsed: Any) -> None:
        query = parse_qs(parsed.query)

        if parsed.path == "/api/scans":
            self.send_json({"scans": list_scans(), "status_options": STATUS_OPTIONS})
            return

        if parsed.path == "/api/health":
            self.send_json(
                {
                    "status": "ok",
                    "database": str(NOTES_DB_PATH),
                    "output_dir": str(OUTPUT_DIR),
                }
            )
            return

        if parsed.path == "/api/scan":
            filename = query.get("file", [None])[0]
            if not filename:
                self.send_json({"error": "缺少 file 参数"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                scan = load_scan(sanitize_scan_filename(filename))
            except FileNotFoundError:
                self.send_json({"error": "扫描文件不存在"}, HTTPStatus.NOT_FOUND)
                return
            self.send_json(scan)
            return

        if parsed.path == "/api/symbol":
            symbol = query.get("symbol", [None])[0]
            if not symbol:
                self.send_json({"error": "缺少 symbol 参数"}, HTTPStatus.BAD_REQUEST)
                return
            self.send_json(
                {
                    "symbol": symbol,
                    "history": get_symbol_history(symbol),
                    "notes": get_symbol_notes(symbol),
                }
            )
            return

        if parsed.path == "/api/prices":
            symbol = query.get("symbol", [None])[0]
            if not symbol:
                self.send_json({"error": "缺少 symbol 参数"}, HTTPStatus.BAD_REQUEST)
                return
            payload = build_price_payload(str(symbol).strip().upper())
            if payload is None:
                self.send_json({"error": "没有找到价格缓存"}, HTTPStatus.NOT_FOUND)
                return
            self.send_json(payload)
            return

        self.send_json({"error": "未知接口"}, HTTPStatus.NOT_FOUND)

    def handle_api_post(self, parsed: Any) -> None:
        if parsed.path != "/api/notes":
            self.send_json({"error": "未知接口"}, HTTPStatus.NOT_FOUND)
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_json({"error": "请求体不是合法 JSON"}, HTTPStatus.BAD_REQUEST)
            return

        symbol = str(payload.get("symbol") or "").strip().upper()
        if not symbol:
            self.send_json({"error": "symbol 不能为空"}, HTTPStatus.BAD_REQUEST)
            return

        status = str(payload.get("status") or "观察")
        note = str(payload.get("note") or "")
        saved = update_symbol_notes(symbol, status, note)
        self.send_json(saved, HTTPStatus.OK)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not self.require_authorization():
            return

        if parsed.path.startswith("/api/"):
            self.handle_api_get(parsed)
            return

        if parsed.path == "/chart":
            query = parse_qs(parsed.query)
            symbol = query.get("symbol", ["NASDAQ:AAPL"])[0]
            scan_file = query.get("file", [None])[0]
            body = build_chart_page(symbol, scan_file).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not self.require_authorization():
            return

        if parsed.path.startswith("/api/"):
            self.handle_api_post(parsed)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")


def run_server(host: str, port: int) -> None:
    init_storage()
    handler = partial(StockTrackerHandler, directory=str(WEB_DIR))
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Stock tracker ready: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the local stock tracker app.")
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "127.0.0.1"),
        help="Bind host. Default: HOST env or 127.0.0.1",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8765")),
        help="Listen port. Default: PORT env or 8765",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run_server(args.host, args.port)
