from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import csv
import io
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Callable, Iterable, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import yfinance as yf
except ImportError:  # pragma: no cover - optional dependency at runtime
    yf = None

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
STOOQ_DAILY_URL = "https://stooq.com/q/d/l/?s={symbol}&i=d"

DEFAULT_CACHE_DIR = Path(".cache")
DEFAULT_CACHE_MAX_AGE_HOURS = 20
DEFAULT_LOOKBACK_DAYS = 5
DEFAULT_MA_WINDOW = 250
DEFAULT_WORKERS = 8
DEFAULT_PROVIDER = "yfinance"
DEFAULT_YFINANCE_BATCH_SIZE = 100
MARKET_TIMEZONE = ZoneInfo("America/New_York")
MARKET_CLOSE_TIME = dt_time(16, 0)
RESULT_COLUMNS = [
    "symbol",
    "name",
    "exchange",
    "breakout_date",
    "breakout_close",
    "breakout_ma250",
    "breakout_premium_pct",
    "latest_date",
    "latest_close",
    "latest_ma250",
    "latest_premium_pct",
    "sessions_since_breakout",
    "signal_type",
]

EXCHANGE_MAP = {
    "A": "NYSE American",
    "N": "NYSE",
    "P": "NYSE Arca",
    "Q": "NASDAQ",
    "V": "IEX",
    "Z": "Cboe",
}

THREAD_LOCAL = threading.local()

NON_EQUITY_TERMS = (
    " ETF",
    " ETN",
    " ETMF",
    " Warrant",
    " Warrants",
    " Rights",
    " Right",
    " Unit",
    " Units",
    " Trust",
    " Fund",
    " Notes",
    " Note",
    " Bond",
    " Preferred",
    " Preference",
    " Contingent Value Right",
    " Rate Reset",
)

NON_COMMON_DEPOSITARY_TERMS = (
    "Depositary Share",
    "Depositary Shares",
    "Preference Shares",
)

COMMON_STOCK_HINTS = (
    "Common Stock",
    "Common Shares",
    "Ordinary Shares",
    "Ordinary Share",
    "Class A Common Stock",
    "Class B Common Stock",
    "American Depositary Shares",
    "American Depositary Share",
)

NON_COMMON_SYMBOL_MARKERS = ("$",)
NON_COMMON_SYMBOL_SUFFIXES = ("-R", "-RT", "-U", "-W", "-WS")
STRICT_COMMON_STOCK_HINTS = (
    "Common Stock",
    "Common Shares",
    "Ordinary Shares",
    "Ordinary Share",
)
STRICT_NON_COMMON_NAME_TERMS = (
    " Fund",
    " Funds",
    " Cert",
    " Certs",
    " Certificate",
    " Certificates",
    " American Depositary",
    " Depositary Share",
    " Depositary Shares",
)


@dataclass(frozen=True)
class Security:
    symbol: str
    name: str
    exchange: str
    is_etf: bool
    source: str


@dataclass(frozen=True)
class BreakoutResult:
    symbol: str
    name: str
    exchange: str
    breakout_date: str
    breakout_close: float
    breakout_ma250: float
    breakout_premium_pct: float
    latest_date: str
    latest_close: float
    latest_ma250: float
    latest_premium_pct: float
    sessions_since_breakout: int
    signal_type: str


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        backoff_factor=1.0,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=32)
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/133.0.0.0 Safari/537.36"
            )
        }
    )
    return session


def get_thread_session() -> requests.Session:
    session = getattr(THREAD_LOCAL, "session", None)
    if session is None:
        session = build_session()
        THREAD_LOCAL.session = session
    return session


def safe_symbol_filename(symbol: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in symbol.upper())


def cache_is_fresh(path: Path, max_age_hours: int) -> bool:
    if not path.exists():
        return False
    age_seconds = time.time() - path.stat().st_mtime
    return age_seconds < max_age_hours * 3600


def read_valid_cache(
    cache_path: Path,
    *,
    validator: Callable[[str], bool] | None,
) -> Optional[str]:
    if not cache_path.exists():
        return None
    cached_text = cache_path.read_text(encoding="utf-8")
    if validator is not None and not validator(cached_text):
        return None
    return cached_text


def fetch_text_with_curl(url: str, *, timeout: int) -> str:
    result = subprocess.run(
        [
            "curl",
            "--fail",
            "--silent",
            "--show-error",
            "--location",
            "--max-time",
            str(timeout),
            url,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def has_pipe_header(expected_header: str) -> Callable[[str], bool]:
    def validator(raw_text: str) -> bool:
        for line in raw_text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            return stripped.startswith(expected_header)
        return False

    return validator


def fetch_text(
    url: str,
    cache_path: Path,
    *,
    refresh: bool,
    max_age_hours: int,
    timeout: int = 20,
    session: Optional[requests.Session] = None,
    validator: Callable[[str], bool] | None = None,
    validation_label: str | None = None,
) -> str:
    if not refresh and cache_is_fresh(cache_path, max_age_hours):
        cached_text = read_valid_cache(cache_path, validator=validator)
        if cached_text is not None:
            return cached_text

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    current_session = session or build_session()
    errors: list[str] = []
    try:
        response = current_session.get(url, timeout=timeout)
        response.raise_for_status()
        response_text = response.text
        if validator is not None and not validator(response_text):
            raise ValueError(validation_label or f"Unexpected response payload from {url}")
        cache_path.write_text(response_text, encoding="utf-8")
        return response_text
    except (requests.RequestException, ValueError) as exc:
        errors.append(str(exc))

    try:
        response_text = fetch_text_with_curl(url, timeout=timeout)
        if validator is not None and not validator(response_text):
            raise ValueError(validation_label or f"Unexpected response payload from {url}")
        cache_path.write_text(response_text, encoding="utf-8")
        return response_text
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError) as exc:
        errors.append(str(exc))

    cached_text = read_valid_cache(cache_path, validator=validator)
    if cached_text is not None:
        return cached_text
    raise RuntimeError("; ".join(errors))


def parse_pipe_file(raw_text: str) -> list[dict[str, str]]:
    lines = [line for line in raw_text.splitlines() if line.strip()]
    lines = [line for line in lines if not line.startswith("File Creation Time")]
    reader = csv.DictReader(lines, delimiter="|")
    return [dict(row) for row in reader]


def looks_like_common_equity(security_name: str, *, strict_common_stock: bool = False) -> bool:
    normalized = f" {security_name.strip()} "
    normalized_lower = normalized.lower()
    has_common_hint = any(hint.lower() in normalized_lower for hint in COMMON_STOCK_HINTS)

    if any(term.lower() in normalized_lower for term in NON_EQUITY_TERMS):
        return False

    if strict_common_stock and any(term.lower() in normalized_lower for term in STRICT_NON_COMMON_NAME_TERMS):
        return False

    if has_common_hint:
        if strict_common_stock:
            return any(hint.lower() in normalized_lower for hint in STRICT_COMMON_STOCK_HINTS)
        return True

    if any(term.lower() in normalized_lower for term in NON_COMMON_DEPOSITARY_TERMS):
        return False

    return not strict_common_stock


def looks_like_common_symbol(symbol: str) -> bool:
    normalized = symbol.strip().upper()
    if any(marker in normalized for marker in NON_COMMON_SYMBOL_MARKERS):
        return False
    if any(normalized.endswith(suffix) for suffix in NON_COMMON_SYMBOL_SUFFIXES):
        return False
    return True


def normalize_stooq_symbol(symbol: str) -> str:
    return symbol.strip().lower().replace(".", "-").replace("/", "-") + ".us"


def fetch_universe(
    cache_dir: Path,
    *,
    include_etfs: bool,
    include_non_common: bool,
    strict_common_stock: bool,
    refresh: bool,
    max_age_hours: int,
) -> list[Security]:
    session = build_session()
    nasdaq_raw = fetch_text(
        NASDAQ_LISTED_URL,
        cache_dir / "universe" / "nasdaqlisted.txt",
        refresh=refresh,
        max_age_hours=max_age_hours,
        session=session,
        validator=has_pipe_header("Symbol|Security Name|"),
        validation_label="Nasdaq listed universe response was not a valid pipe-delimited symbol directory",
    )
    other_raw = fetch_text(
        OTHER_LISTED_URL,
        cache_dir / "universe" / "otherlisted.txt",
        refresh=refresh,
        max_age_hours=max_age_hours,
        session=session,
        validator=has_pipe_header("ACT Symbol|Security Name|"),
        validation_label="Other listed universe response was not a valid pipe-delimited symbol directory",
    )

    seen: set[str] = set()
    universe: list[Security] = []

    for row in parse_pipe_file(nasdaq_raw):
        symbol = (row.get("Symbol") or "").strip()
        name = (row.get("Security Name") or "").strip()
        if not symbol or symbol in seen:
            continue
        if row.get("Test Issue") == "Y":
            continue
        if not include_non_common and not looks_like_common_symbol(symbol):
            continue
        is_etf = row.get("ETF") == "Y"
        if is_etf and not include_etfs:
            continue
        if not include_non_common and not looks_like_common_equity(
            name,
            strict_common_stock=strict_common_stock,
        ):
            continue
        seen.add(symbol)
        universe.append(
            Security(
                symbol=symbol,
                name=name,
                exchange="NASDAQ",
                is_etf=is_etf,
                source="nasdaqlisted",
            )
        )

    for row in parse_pipe_file(other_raw):
        symbol = (row.get("ACT Symbol") or "").strip()
        name = (row.get("Security Name") or "").strip()
        if not symbol or symbol in seen:
            continue
        if row.get("Test Issue") == "Y":
            continue
        if not include_non_common and not looks_like_common_symbol(symbol):
            continue
        is_etf = row.get("ETF") == "Y"
        if is_etf and not include_etfs:
            continue
        if not include_non_common and not looks_like_common_equity(
            name,
            strict_common_stock=strict_common_stock,
        ):
            continue
        seen.add(symbol)
        universe.append(
            Security(
                symbol=symbol,
                name=name,
                exchange=EXCHANGE_MAP.get((row.get("Exchange") or "").strip(), "Other"),
                is_etf=is_etf,
                source="otherlisted",
            )
        )

    universe.sort(key=lambda item: item.symbol)
    return universe


def load_symbols_from_inputs(
    symbols_text: Optional[str],
    symbols_file: Optional[Path],
) -> Optional[list[str]]:
    symbols: list[str] = []
    if symbols_text:
        for symbol in symbols_text.split(","):
            cleaned = symbol.strip().upper()
            if cleaned:
                symbols.append(cleaned)

    if symbols_file:
        for line in symbols_file.read_text(encoding="utf-8").splitlines():
            cleaned = line.strip().upper()
            if cleaned and not cleaned.startswith("#"):
                symbols.append(cleaned)

    if not symbols:
        return None

    deduped = sorted(set(symbols))
    return deduped


def read_history_cache(cache_path: Path) -> Optional[pd.DataFrame]:
    if not cache_path.exists():
        return None

    frame = pd.read_csv(cache_path, parse_dates=["Date"])
    if frame.empty or "Close" not in frame.columns:
        return None

    frame = frame.dropna(subset=["Date", "Close"]).drop_duplicates(subset=["Date"])
    frame = frame.sort_values("Date").reset_index(drop=True)
    return frame


def write_history_cache(cache_path: Path, history: pd.DataFrame) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    history.to_csv(cache_path, index=False)


def history_cache_path(cache_dir: Path, symbol: str, provider: str) -> Path:
    return cache_dir / "prices" / provider / f"{safe_symbol_filename(symbol)}.csv"


def normalize_yfinance_symbol(symbol: str) -> str:
    return symbol.strip().upper().replace(".", "-").replace("/", "-")


def chunked(items: list[Security], size: int) -> Iterable[list[Security]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def extract_yfinance_history(
    downloaded: pd.DataFrame,
    yahoo_symbol: str,
) -> Optional[pd.DataFrame]:
    if downloaded.empty:
        return None

    if isinstance(downloaded.columns, pd.MultiIndex):
        available_symbols = set(downloaded.columns.get_level_values("Ticker"))
        if yahoo_symbol not in available_symbols:
            return None
        frame = downloaded.xs(yahoo_symbol, axis=1, level="Ticker").copy()
    else:
        frame = downloaded.copy()

    if "Close" not in frame.columns:
        return None

    frame = frame.reset_index()
    if "Date" not in frame.columns:
        return None

    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")

    numeric_columns = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    for column in numeric_columns:
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")

    frame = frame.dropna(subset=["Date", "Close"]).drop_duplicates(subset=["Date"])
    frame = frame.sort_values("Date").reset_index(drop=True)
    frame = drop_incomplete_current_day_bar(frame)
    return frame


def drop_incomplete_current_day_bar(
    frame: pd.DataFrame,
    *,
    now: datetime | None = None,
) -> pd.DataFrame:
    if frame.empty or "Date" not in frame.columns:
        return frame

    now_ny = now.astimezone(MARKET_TIMEZONE) if now is not None else datetime.now(MARKET_TIMEZONE)
    if now_ny.time() >= MARKET_CLOSE_TIME:
        return frame

    normalized = frame.copy()
    normalized["Date"] = pd.to_datetime(normalized["Date"], errors="coerce")
    normalized = normalized.dropna(subset=["Date"]).reset_index(drop=True)
    if normalized.empty:
        return normalized

    latest_date = pd.Timestamp(normalized["Date"].iloc[-1]).date()
    if latest_date < now_ny.date():
        return normalized

    return normalized.loc[normalized["Date"].dt.date < now_ny.date()].reset_index(drop=True)


def download_yfinance_batch(batch: list[Security]) -> dict[str, Optional[pd.DataFrame]]:
    if yf is None:
        raise RuntimeError("yfinance is not installed. Run `python3 -m pip install -r requirements.txt`.")

    if not batch:
        return {}

    symbol_map = {normalize_yfinance_symbol(item.symbol): item.symbol for item in batch}
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        downloaded = yf.download(
            " ".join(symbol_map.keys()),
            period="3y",
            interval="1d",
            auto_adjust=False,
            progress=False,
            threads=False,
        )

    histories: dict[str, Optional[pd.DataFrame]] = {}
    for yahoo_symbol, original_symbol in symbol_map.items():
        histories[original_symbol] = extract_yfinance_history(downloaded, yahoo_symbol)

    return histories


def fetch_stooq_price_history(
    symbol: str,
    cache_dir: Path,
    *,
    refresh: bool,
    max_age_hours: int,
) -> Optional[pd.DataFrame]:
    stooq_symbol = normalize_stooq_symbol(symbol)
    cache_path = history_cache_path(cache_dir, symbol, "stooq")
    raw_text = fetch_text(
        STOOQ_DAILY_URL.format(symbol=stooq_symbol),
        cache_path,
        refresh=refresh,
        max_age_hours=max_age_hours,
        session=get_thread_session(),
    )

    if raw_text.strip().startswith("No data"):
        return None

    if not raw_text.lstrip().startswith("Date,"):
        return None

    frame = pd.read_csv(io.StringIO(raw_text), parse_dates=["Date"])
    if frame.empty or "Close" not in frame.columns:
        return None

    numeric_columns = ["Open", "High", "Low", "Close", "Volume"]
    for column in numeric_columns:
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")

    frame = frame.dropna(subset=["Date", "Close"]).drop_duplicates(subset=["Date"])
    frame = frame.sort_values("Date").reset_index(drop=True)
    return frame


def detect_breakout(
    history: pd.DataFrame,
    *,
    ma_window: int,
    lookback_days: int,
    max_distance_pct: Optional[float] = None,
) -> Optional[dict[str, object]]:
    if len(history) < ma_window + 2:
        return None

    frame = history.copy()
    frame["ma"] = frame["Close"].rolling(window=ma_window, min_periods=ma_window).mean()
    frame = frame.dropna(subset=["ma"]).reset_index(drop=True)
    if len(frame) < 2:
        return None

    frame["is_above_ma"] = frame["Close"] > frame["ma"]
    frame["crossed_up"] = frame["is_above_ma"] & ~frame["is_above_ma"].shift(1, fill_value=False)
    frame["premium_pct"] = (frame["Close"] / frame["ma"] - 1.0) * 100.0

    latest = frame.iloc[-1]
    if not bool(latest["is_above_ma"]):
        return None

    recent = frame.tail(min(lookback_days, len(frame)))
    matches = recent[recent["crossed_up"]]
    if matches.empty:
        return None

    breakout = matches.iloc[-1]
    if max_distance_pct is not None and float(latest["premium_pct"]) > max_distance_pct:
        return None

    breakout_index = int(matches.index[-1])
    sessions_since_breakout = len(frame) - 1 - breakout_index

    return {
        "breakout_date": breakout["Date"].date().isoformat(),
        "breakout_close": float(breakout["Close"]),
        "breakout_ma250": float(breakout["ma"]),
        "breakout_premium_pct": float(breakout["premium_pct"]),
        "latest_date": latest["Date"].date().isoformat(),
        "latest_close": float(latest["Close"]),
        "latest_ma250": float(latest["ma"]),
        "latest_premium_pct": float(latest["premium_pct"]),
        "sessions_since_breakout": sessions_since_breakout,
        "signal_type": "today" if sessions_since_breakout == 0 else "recent_week",
    }


def build_breakout_result(
    security: Security,
    history: Optional[pd.DataFrame],
    *,
    ma_window: int,
    lookback_days: int,
    max_distance_pct: Optional[float],
) -> Optional[BreakoutResult]:
    if history is None:
        return None

    breakout = detect_breakout(
        history,
        ma_window=ma_window,
        lookback_days=lookback_days,
        max_distance_pct=max_distance_pct,
    )
    if breakout is None:
        return None

    return BreakoutResult(
        symbol=security.symbol,
        name=security.name,
        exchange=security.exchange,
        **breakout,
    )


def screen_symbol_with_stooq(
    security: Security,
    cache_dir: Path,
    *,
    ma_window: int,
    lookback_days: int,
    refresh: bool,
    max_age_hours: int,
    max_distance_pct: Optional[float],
) -> Optional[BreakoutResult]:
    history = fetch_stooq_price_history(
        security.symbol,
        cache_dir,
        refresh=refresh,
        max_age_hours=max_age_hours,
    )
    return build_breakout_result(
        security,
        history,
        ma_window=ma_window,
        lookback_days=lookback_days,
        max_distance_pct=max_distance_pct,
    )


def screen_universe_with_yfinance(
    universe: list[Security],
    cache_dir: Path,
    *,
    ma_window: int,
    lookback_days: int,
    refresh: bool,
    max_age_hours: int,
    max_distance_pct: Optional[float],
    batch_size: int,
) -> list[BreakoutResult]:
    results: list[BreakoutResult] = []
    processed = 0

    for batch in chunked(universe, batch_size):
        histories: dict[str, Optional[pd.DataFrame]] = {}
        to_download: list[Security] = []

        for security in batch:
            cache_path = history_cache_path(cache_dir, security.symbol, "yfinance")
            if not refresh and cache_is_fresh(cache_path, max_age_hours):
                histories[security.symbol] = read_history_cache(cache_path)
            else:
                to_download.append(security)

        if to_download:
            downloaded_histories = download_yfinance_batch(to_download)
            for security in to_download:
                history = downloaded_histories.get(security.symbol)
                if history is not None:
                    write_history_cache(history_cache_path(cache_dir, security.symbol, "yfinance"), history)
                histories[security.symbol] = history

        for security in batch:
            result = build_breakout_result(
                security,
                histories.get(security.symbol),
                ma_window=ma_window,
                lookback_days=lookback_days,
                max_distance_pct=max_distance_pct,
            )
            if result is not None:
                results.append(result)

        processed += len(batch)
        print(
            f"Progress: {processed}/{len(universe)} screened, {len(results)} matches.",
            file=sys.stderr,
        )

    return results


def screen_universe_with_stooq(
    universe: list[Security],
    cache_dir: Path,
    *,
    ma_window: int,
    lookback_days: int,
    refresh: bool,
    max_age_hours: int,
    max_distance_pct: Optional[float],
    workers: int,
) -> list[BreakoutResult]:
    results: list[BreakoutResult] = []
    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                screen_symbol_with_stooq,
                security,
                cache_dir,
                ma_window=ma_window,
                lookback_days=lookback_days,
                refresh=refresh,
                max_age_hours=max_age_hours,
                max_distance_pct=max_distance_pct,
            ): security.symbol
            for security in universe
        }

        for future in concurrent.futures.as_completed(futures):
            completed += 1
            symbol = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # pragma: no cover - defensive logging path
                print(f"[warn] {symbol}: {exc}", file=sys.stderr)
                continue

            if result is not None:
                results.append(result)

            if completed % 100 == 0 or completed == len(universe):
                print(
                    f"Progress: {completed}/{len(universe)} screened, {len(results)} matches.",
                    file=sys.stderr,
                )

    return results


def screen_universe(
    universe: list[Security],
    cache_dir: Path,
    *,
    provider: str,
    ma_window: int,
    lookback_days: int,
    refresh: bool,
    max_age_hours: int,
    max_distance_pct: Optional[float],
    workers: int,
    batch_size: int,
) -> list[BreakoutResult]:
    if provider == "yfinance":
        return screen_universe_with_yfinance(
            universe,
            cache_dir,
            ma_window=ma_window,
            lookback_days=lookback_days,
            refresh=refresh,
            max_age_hours=max_age_hours,
            max_distance_pct=max_distance_pct,
            batch_size=batch_size,
        )

    return screen_universe_with_stooq(
        universe,
        cache_dir,
        ma_window=ma_window,
        lookback_days=lookback_days,
        refresh=refresh,
        max_age_hours=max_age_hours,
        max_distance_pct=max_distance_pct,
        workers=workers,
    )


def format_results(results: list[BreakoutResult]) -> pd.DataFrame:
    frame = pd.DataFrame(asdict(item) for item in results)
    if frame.empty:
        return pd.DataFrame(columns=RESULT_COLUMNS)
    return frame.sort_values(
        by=["sessions_since_breakout", "latest_premium_pct", "symbol"],
        ascending=[True, True, True],
    ).reset_index(drop=True)


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find US stocks whose closing price just crossed above the 250-day moving average."
    )
    parser.add_argument(
        "--symbols",
        help="Comma-separated ticker list. If omitted, the script pulls the full US listed universe.",
    )
    parser.add_argument(
        "--symbols-file",
        type=Path,
        help="Optional text file with one ticker per line.",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help="How many recent trading sessions count as 'just broke above'. Default: 5.",
    )
    parser.add_argument(
        "--ma-window",
        type=int,
        default=DEFAULT_MA_WINDOW,
        help="Moving average window size in trading days. Default: 250.",
    )
    parser.add_argument(
        "--max-distance-pct",
        type=float,
        help="Optional cap on how far the latest close can sit above the MA, in percent.",
    )
    parser.add_argument(
        "--provider",
        choices=("yfinance", "stooq"),
        default=DEFAULT_PROVIDER,
        help="Market data provider. Default: yfinance.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help="Concurrent download workers for the stooq provider. Default: 8.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_YFINANCE_BATCH_SIZE,
        help="Ticker batch size for yfinance downloads. Default: 100.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help="Cache directory for universe and price data. Default: .cache",
    )
    parser.add_argument(
        "--cache-max-age-hours",
        type=int,
        default=DEFAULT_CACHE_MAX_AGE_HOURS,
        help="How long cached files stay fresh. Default: 20 hours.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore cache and redownload universe and price data.",
    )
    parser.add_argument(
        "--include-etfs",
        action="store_true",
        help="Include ETFs in the screen. Default is to exclude ETFs.",
    )
    parser.add_argument(
        "--include-non-common",
        action="store_true",
        help="Include non-common equity instruments such as preferreds, rights, and units.",
    )
    parser.add_argument(
        "--strict-common-stock",
        action="store_true",
        help="Use a stricter filter that keeps plain common/ordinary shares and excludes ADRs, funds, and certificates.",
    )
    parser.add_argument(
        "--max-symbols",
        type=int,
        help="Optional hard cap for quick smoke runs.",
    )
    parser.add_argument(
        "--output-csv",
        type=Path,
        help="Optional output CSV path for matched results.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    user_symbols = load_symbols_from_inputs(args.symbols, args.symbols_file)

    if user_symbols is None:
        print("Loading official US symbol universe...", file=sys.stderr)
        universe = fetch_universe(
            args.cache_dir,
            include_etfs=args.include_etfs,
            include_non_common=args.include_non_common,
            strict_common_stock=args.strict_common_stock,
            refresh=args.refresh,
            max_age_hours=args.cache_max_age_hours,
        )
    else:
        universe = [
            Security(
                symbol=symbol,
                name="Custom symbol",
                exchange="CUSTOM",
                is_etf=False,
                source="manual",
            )
            for symbol in user_symbols
        ]

    if args.max_symbols:
        universe = universe[: args.max_symbols]

    print(f"Screening {len(universe)} symbols...", file=sys.stderr)
    results = screen_universe(
        universe,
        args.cache_dir,
        provider=args.provider,
        ma_window=args.ma_window,
        lookback_days=args.lookback_days,
        refresh=args.refresh,
        max_age_hours=args.cache_max_age_hours,
        max_distance_pct=args.max_distance_pct,
        workers=args.workers,
        batch_size=args.batch_size,
    )

    frame = format_results(results)
    if frame.empty:
        print("No breakouts found for the current run.")
    else:
        display_columns = [
            "symbol",
            "exchange",
            "breakout_date",
            "sessions_since_breakout",
            "latest_close",
            "latest_ma250",
            "latest_premium_pct",
        ]
        print(frame[display_columns].to_string(index=False))

    if args.output_csv:
        args.output_csv.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(args.output_csv, index=False)
        print(f"Saved {len(frame)} matches to {args.output_csv}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
