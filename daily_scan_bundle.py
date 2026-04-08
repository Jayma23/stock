from __future__ import annotations

import argparse
import io
import re
import subprocess
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import pandas as pd

import stock_screener

APP_TIMEZONE = ZoneInfo("America/Los_Angeles")
PRIMARY_SCAN_PATTERN = re.compile(
    r"^breakouts_(?P<date>\d{4}-\d{2}-\d{2})(?:_(?P<days>\d+)day)?(?P<strict>_strict_common)?\.csv$"
)
TRADINGVIEW_EXCHANGE_MAP = {
    "NASDAQ": "NASDAQ",
    "NYSE": "NYSE",
    "NYSE American": "AMEX",
}


@dataclass(frozen=True)
class ScanDescriptor:
    path: Path
    run_date: date
    lookback_days: int
    strict_common_stock: bool
    source_ref: str | None = None


def parse_primary_scan(path: Path) -> ScanDescriptor | None:
    match = PRIMARY_SCAN_PATTERN.fullmatch(path.name)
    if match is None:
        return None
    lookback_days = int(match.group("days") or 5)
    strict_common_stock = bool(match.group("strict"))
    return ScanDescriptor(
        path=path,
        run_date=date.fromisoformat(match.group("date")),
        lookback_days=lookback_days,
        strict_common_stock=strict_common_stock,
    )


def find_previous_scan(current_scan: Path, output_dir: Path) -> ScanDescriptor | None:
    current_descriptor = parse_primary_scan(current_scan)
    if current_descriptor is None:
        raise ValueError(f"Unsupported scan filename: {current_scan.name}")

    candidates: dict[tuple[date, int, bool], ScanDescriptor] = {}
    for path in output_dir.glob("breakouts_*.csv"):
        descriptor = parse_primary_scan(path)
        if descriptor is None:
            continue
        if descriptor.path.resolve() == current_descriptor.path.resolve():
            continue
        if descriptor.lookback_days != current_descriptor.lookback_days:
            continue
        if descriptor.strict_common_stock != current_descriptor.strict_common_stock:
            continue
        if descriptor.run_date >= current_descriptor.run_date:
            continue
        candidates[(descriptor.run_date, descriptor.lookback_days, descriptor.strict_common_stock)] = descriptor

    for descriptor in find_previous_scans_in_git_history(current_descriptor):
        key = (descriptor.run_date, descriptor.lookback_days, descriptor.strict_common_stock)
        candidates.setdefault(key, descriptor)

    if not candidates:
        return None
    return max(candidates.values(), key=lambda item: item.run_date)


def repo_root() -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    root = result.stdout.strip()
    return Path(root) if root else None


def discover_git_refs() -> list[str]:
    refs: list[str] = []
    for candidate in ("origin/main", "main", "HEAD"):
        try:
            subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", candidate],
                check=True,
                capture_output=True,
                text=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
        refs.append(candidate)
    return refs


def find_previous_scans_in_git_history(current_descriptor: ScanDescriptor) -> list[ScanDescriptor]:
    root = repo_root()
    if root is None:
        return []

    current_path = current_descriptor.path.resolve()
    try:
        output_relative = current_path.parent.relative_to(root).as_posix()
    except ValueError:
        return []

    candidates: dict[tuple[date, int, bool], ScanDescriptor] = {}
    for ref in discover_git_refs():
        try:
            result = subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", ref, "--", output_relative],
                check=True,
                capture_output=True,
                text=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue

        for repo_relative in result.stdout.splitlines():
            descriptor = parse_primary_scan(Path(repo_relative))
            if descriptor is None:
                continue
            if descriptor.lookback_days != current_descriptor.lookback_days:
                continue
            if descriptor.strict_common_stock != current_descriptor.strict_common_stock:
                continue
            if descriptor.run_date >= current_descriptor.run_date:
                continue
            key = (descriptor.run_date, descriptor.lookback_days, descriptor.strict_common_stock)
            candidates.setdefault(
                key,
                ScanDescriptor(
                    path=root / repo_relative,
                    run_date=descriptor.run_date,
                    lookback_days=descriptor.lookback_days,
                    strict_common_stock=descriptor.strict_common_stock,
                    source_ref=ref,
                ),
            )
    return list(candidates.values())


def read_scan_frame(descriptor: ScanDescriptor) -> pd.DataFrame:
    if descriptor.source_ref is None:
        return pd.read_csv(descriptor.path)

    root = repo_root()
    if root is None:
        raise RuntimeError("Unable to resolve repository root for git-backed scan")

    repo_relative = descriptor.path.relative_to(root).as_posix()
    result = subprocess.run(
        ["git", "show", f"{descriptor.source_ref}:{repo_relative}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return pd.read_csv(io.StringIO(result.stdout))


def exchange_prefix(exchange: str) -> str:
    return TRADINGVIEW_EXCHANGE_MAP.get(str(exchange).strip(), str(exchange).strip())


def format_price(value: float) -> str:
    return f"{float(value):.2f}"


def format_pct(value: float) -> str:
    return f"{float(value):.2f}%"


def distribution_text(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "无"
    counts = (
        frame["sessions_since_breakout"]
        .value_counts()
        .sort_index()
        .items()
    )
    return ", ".join(f"{int(days)}天 {int(count)} 只" for days, count in counts)


def rows_by_symbols(frame: pd.DataFrame) -> dict[str, dict[str, object]]:
    if frame.empty:
        return {}
    keyed: dict[str, dict[str, object]] = {}
    for row in frame.to_dict(orient="records"):
        symbol = str(row.get("symbol") or "").strip().upper()
        if symbol:
            keyed[symbol] = row
    return keyed


def write_tradingview_watchlist(frame: pd.DataFrame, output_path: Path) -> None:
    symbols: list[str] = []
    for row in frame.to_dict(orient="records"):
        symbol = str(row.get("symbol") or "").strip().upper()
        exchange = str(row.get("exchange") or "").strip()
        if not symbol or not exchange:
            continue
        symbols.append(f"{exchange_prefix(exchange)}:{symbol}")
    output_path.write_text(",".join(symbols), encoding="utf-8")


def write_added_markdown(
    output_path: Path,
    added_frame: pd.DataFrame,
    *,
    run_date: str,
    latest_date: str,
    previous_date: str | None,
    lookback_days: int,
    strict_common_stock: bool,
) -> None:
    lines = [
        f"# {run_date} 相对 {previous_date or '无历史基准'} 新增股票表",
        "",
        f"- 扫描口径: {lookback_days}天内 / {'严格普通股' if strict_common_stock else '默认过滤'}",
        f"- 运行日期: {run_date}",
        f"- 最新已完成收盘日: {latest_date}",
        f"- 新增数量: {len(added_frame.index)}",
        "",
    ]

    if added_frame.empty:
        lines.append("本期没有新增股票。")
    else:
        lines.extend(
            [
                "| 代码 | 交易所 | 突破日期 | 距今交易日 | 最新收盘 | 最新MA250 | 高于250日线 |",
                "| --- | --- | --- | ---: | ---: | ---: | ---: |",
            ]
        )
        for row in added_frame.to_dict(orient="records"):
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(row["symbol"]),
                        str(row["exchange"]),
                        str(row["breakout_date"]),
                        str(int(row["sessions_since_breakout"])),
                        format_price(row["latest_close"]),
                        format_price(row["latest_ma250"]),
                        format_pct(row["latest_premium_pct"]),
                    ]
                )
                + " |"
            )

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_report_markdown(
    output_path: Path,
    current_frame: pd.DataFrame,
    removed_rows: list[dict[str, object]],
    *,
    run_date: str,
    latest_date: str,
    previous_date: str | None,
    added_count: int,
    lookback_days: int,
    strict_common_stock: bool,
) -> None:
    same_day = current_frame.loc[current_frame["sessions_since_breakout"] == 0].head(30)
    after_close = latest_date == run_date
    timing_text = (
        f"运行时间为美东收盘后，结果已包含 {latest_date} 收盘数据。"
        if after_close
        else f"运行时间早于美东收盘，当前结果基于最近已完成收盘日 {latest_date}。"
    )

    lines = [
        f"# {run_date} {'严格普通股' if strict_common_stock else '默认过滤'} {lookback_days}日突破扫描更新",
        "",
        f"- 运行日期: {run_date}",
        f"- 最新已完成收盘日: {latest_date}",
        f"- 说明: {timing_text}",
        f"- 命中数量: {len(current_frame.index)}",
        f"- 相比 {previous_date or '无历史基准'} 版本新增: {added_count} 只",
    ]

    if previous_date:
        lines.append(f"- 相比 {previous_date} 版本移除: {len(removed_rows)} 只")

    lines.extend(
        [
            f"- 分布: {distribution_text(current_frame)}",
            "",
            "## 今天刚突破（0天，前30只）",
            "",
        ]
    )

    if same_day.empty:
        lines.append("本期没有今天刚突破的股票。")
    else:
        for row in same_day.to_dict(orient="records"):
            lines.append(
                f"- {row['symbol']} | {row['exchange']} | 收盘 {format_price(row['latest_close'])} | 高于250日线 {format_pct(row['latest_premium_pct'])}"
            )

    lines.extend(["", "## 本期移除", ""])
    if not removed_rows:
        lines.append("本期没有移除股票。")
    else:
        for row in removed_rows:
            lines.append(
                f"- {row['symbol']} | {row['exchange']} | 上一期突破日 {row['breakout_date']} | 最新收盘 {format_price(row['latest_close'])} | 高于250日线 {format_pct(row['latest_premium_pct'])}"
            )

    lines.extend(
        [
            "",
            "## 完整文件",
            "",
            f"- CSV: `output/breakouts_{run_date}_{lookback_days}day{'_strict_common' if strict_common_stock else ''}.csv`",
            f"- TradingView: `output/tradingview_watchlist_{run_date}_{lookback_days}day{'_strict_common' if strict_common_stock else ''}.txt`",
        ]
    )
    if previous_date:
        lines.append(f"- 新增对比 CSV: `output/breakouts_{run_date}_added_vs_{previous_date}.csv`")

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def generate_bundle_for_scan(current_scan: Path, output_dir: Path) -> dict[str, object]:
    descriptor = parse_primary_scan(current_scan)
    if descriptor is None:
        raise ValueError(f"Unsupported scan filename: {current_scan.name}")

    current_frame = pd.read_csv(current_scan)
    if not current_frame.empty:
        current_frame = current_frame.sort_values(
            by=["sessions_since_breakout", "latest_premium_pct", "symbol"],
            ascending=[True, True, True],
        ).reset_index(drop=True)
        current_frame.to_csv(current_scan, index=False)

    latest_date = (
        str(current_frame["latest_date"].iloc[0])
        if not current_frame.empty and "latest_date" in current_frame.columns
        else descriptor.run_date.isoformat()
    )

    previous_descriptor = find_previous_scan(current_scan, output_dir)
    previous_frame = pd.DataFrame()
    previous_date: str | None = None
    if previous_descriptor is not None:
        previous_frame = read_scan_frame(previous_descriptor)
        previous_date = previous_descriptor.run_date.isoformat()

    current_symbols = rows_by_symbols(current_frame)
    previous_symbols = rows_by_symbols(previous_frame)

    added_symbols = sorted(set(current_symbols) - set(previous_symbols))
    removed_symbols = sorted(set(previous_symbols) - set(current_symbols))

    added_frame = (
        current_frame.loc[current_frame["symbol"].isin(added_symbols)]
        .sort_values(by=["sessions_since_breakout", "latest_premium_pct", "symbol"], ascending=[True, True, True])
        .reset_index(drop=True)
        if added_symbols
        else current_frame.head(0).copy()
    )
    removed_rows = [previous_symbols[symbol] for symbol in removed_symbols]
    removed_rows.sort(
        key=lambda row: (
            str(row.get("breakout_date") or ""),
            str(row.get("symbol") or ""),
        ),
        reverse=True,
    )

    report_path = output_dir / f"breakouts_{descriptor.run_date.isoformat()}_{descriptor.lookback_days}day{'_strict_common' if descriptor.strict_common_stock else ''}_report_zh.md"
    watchlist_path = output_dir / f"tradingview_watchlist_{descriptor.run_date.isoformat()}_{descriptor.lookback_days}day{'_strict_common' if descriptor.strict_common_stock else ''}.txt"
    added_csv_path = None
    added_md_path = None

    if previous_date:
        added_csv_path = output_dir / f"breakouts_{descriptor.run_date.isoformat()}_added_vs_{previous_date}.csv"
        added_md_path = output_dir / f"breakouts_{descriptor.run_date.isoformat()}_added_vs_{previous_date}.md"
        added_frame.to_csv(added_csv_path, index=False)
        write_added_markdown(
            added_md_path,
            added_frame,
            run_date=descriptor.run_date.isoformat(),
            latest_date=latest_date,
            previous_date=previous_date,
            lookback_days=descriptor.lookback_days,
            strict_common_stock=descriptor.strict_common_stock,
        )

    write_tradingview_watchlist(current_frame, watchlist_path)
    write_report_markdown(
        report_path,
        current_frame,
        removed_rows,
        run_date=descriptor.run_date.isoformat(),
        latest_date=latest_date,
        previous_date=previous_date,
        added_count=len(added_frame.index),
        lookback_days=descriptor.lookback_days,
        strict_common_stock=descriptor.strict_common_stock,
    )

    return {
        "current_csv": current_scan,
        "report_md": report_path,
        "watchlist_txt": watchlist_path,
        "added_csv": added_csv_path,
        "added_md": added_md_path,
        "count": len(current_frame.index),
        "added_count": len(added_frame.index),
        "removed_count": len(removed_rows),
        "latest_date": latest_date,
    }


def build_output_csv_path(output_dir: Path, run_date: date, lookback_days: int, strict_common_stock: bool) -> Path:
    suffix = f"_{lookback_days}day"
    if strict_common_stock:
        suffix += "_strict_common"
    return output_dir / f"breakouts_{run_date.isoformat()}{suffix}.csv"


def run_daily_scan(
    *,
    run_date: date,
    lookback_days: int,
    strict_common_stock: bool,
    refresh: bool,
    provider: str,
    cache_dir: Path,
    output_dir: Path,
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_csv = build_output_csv_path(output_dir, run_date, lookback_days, strict_common_stock)
    argv = [
        "--lookback-days",
        str(lookback_days),
        "--provider",
        provider,
        "--cache-dir",
        str(cache_dir),
        "--output-csv",
        str(output_csv),
    ]
    if refresh:
        argv.append("--refresh")
    if strict_common_stock:
        argv.append("--strict-common-stock")

    exit_code = stock_screener.main(argv)
    if exit_code != 0:
        raise SystemExit(exit_code)
    return generate_bundle_for_scan(output_csv, output_dir)


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the standard daily breakout scan bundle.")
    parser.add_argument(
        "--run-date",
        default=datetime.now(APP_TIMEZONE).date().isoformat(),
        help="Filename date to stamp on outputs. Default: today in America/Los_Angeles.",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=3,
        help="Breakout lookback window. Default: 3.",
    )
    parser.add_argument(
        "--provider",
        choices=("yfinance", "stooq"),
        default="yfinance",
        help="Market data provider. Default: yfinance.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(".cache"),
        help="Cache directory. Default: .cache.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output"),
        help="Output directory. Default: output.",
    )
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="Use cached market data when possible.",
    )
    parser.add_argument(
        "--non-strict-common-stock",
        action="store_true",
        help="Use the looser common stock filter instead of strict common stock mode.",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    summary = run_daily_scan(
        run_date=date.fromisoformat(args.run_date),
        lookback_days=args.lookback_days,
        strict_common_stock=not args.non_strict_common_stock,
        refresh=not args.no_refresh,
        provider=args.provider,
        cache_dir=args.cache_dir,
        output_dir=args.output_dir,
    )
    print(
        f"Bundle ready: {summary['count']} matches, {summary['added_count']} added, "
        f"{summary['removed_count']} removed. Latest close date: {summary['latest_date']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
