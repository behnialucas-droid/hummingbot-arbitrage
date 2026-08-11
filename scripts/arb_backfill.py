#!/usr/bin/env python3
"""
ARB BACKFILL — 5-Day Historical Opportunity Scanner
====================================================
Fetches 5m OHLCV from 11 exchanges for past 5 days,
finds all cross-exchange arbitrage opportunities missed,
reports summary + top trades to Telegram.

Run: python scripts/arb_backfill.py
"""

import os, json, time, logging, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import ccxt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=__import__("sys").stdout,
)
log = logging.getLogger("backfill")

# ─── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")

FEES = {
    "mexc":      0.0010,
    "kucoin":    0.0010,
    "gateio":    0.0010,
    "htx":       0.0020,
    "bitget":    0.0010,
    "bingx":     0.0010,
    "bitmart":   0.0025,
    "phemex":    0.0010,
    "whitebit":  0.0010,
    "bitstamp":  0.0050,
    "digifinex": 0.0020,
}

PAIRS   = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]
TF      = "5m"
DAYS    = 5          # look-back window
LIMIT   = 1440       # 5m candles in 5 days = 5 * 24 * 12 = 1440
MIN_NET = 0.001      # 0.1% net after fees

TIMEOUTS = {
    "mexc": 10000, "kucoin": 10000, "gateio": 12000, "htx": 10000,
    "bitget": 8000, "bingx": 8000, "bitmart": 8000, "phemex": 8000,
    "whitebit": 8000, "bitstamp": 8000, "digifinex": 8000,
}

# ─── Telegram ─────────────────────────────────────────────────────────────────
def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        log.warning("No Telegram credentials.")
        return
    chunks = [msg[i:i+4000] for i in range(0, len(msg), 4000)]
    for chunk in chunks:
        try:
            url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT, "text": chunk}).encode()
            with urllib.request.urlopen(
                urllib.request.Request(url, data=data, method="POST"), timeout=15
            ) as r:
                resp = json.loads(r.read())
                if not resp.get("ok"):
                    log.warning(f"TG err: {resp}")
        except Exception as e:
            log.error(f"TG failed: {e}")

# ─── Build Exchange ────────────────────────────────────────────────────────────
def build_exchange(name: str):
    try:
        ex = getattr(ccxt, name)({"enableRateLimit": True, "timeout": TIMEOUTS.get(name, 9000)})
        ex.load_markets()
        log.info(f"  INIT {name:12s}: {len(ex.markets):,} markets")
        return name, ex
    except Exception as e:
        log.warning(f"  SKIP {name:12s}: {str(e)[:55]}")
        return name, None

# ─── Fetch OHLCV ──────────────────────────────────────────────────────────────
def fetch_ohlcv_one(args):
    """Fetch 5m OHLCV for one exchange+pair. Returns list of (ts_ms, close)."""
    name, ex, pair = args
    # Check if pair exists on this exchange
    sym = pair.replace("/", "/")  # ccxt uses slash format
    if sym not in ex.markets:
        return name, pair, []
    try:
        candles = ex.fetch_ohlcv(sym, TF, limit=LIMIT)
        # candles: [[ts_ms, open, high, low, close, volume], ...]
        result = [(c[0], c[4]) for c in candles if c[4]]  # (ts_ms, close)
        log.info(f"  {name:12s} | {pair:10s}: {len(result)} candles")
        return name, pair, result
    except Exception as e:
        log.warning(f"  {name:12s} | {pair:10s}: {str(e)[:50]}")
        return name, pair, []

# ─── Align and Scan ───────────────────────────────────────────────────────────
def find_missed_opportunities(ohlcv_data: dict) -> list:
    """
    ohlcv_data: {pair: {exchange: {ts_ms: close_price}}}
    Returns list of missed opportunities sorted by net_margin desc.
    """
    all_opps = []

    for pair in PAIRS:
        ex_data = ohlcv_data.get(pair, {})
        if len(ex_data) < 2:
            continue

        # Build timestamp-aligned price dict
        # {ts: {ex: close}}
        ts_prices = {}
        for ex, candles in ex_data.items():
            for ts, close in candles.items():
                if ts not in ts_prices:
                    ts_prices[ts] = {}
                ts_prices[ts][ex] = close

        log.info(f"{pair}: {len(ts_prices)} aligned timestamps across {len(ex_data)} exchanges")

        # For each timestamp: check all exchange combos
        for ts, prices_at_ts in ts_prices.items():
            if len(prices_at_ts) < 2:
                continue

            exchanges = list(prices_at_ts.keys())
            for buy_ex in exchanges:
                for sell_ex in exchanges:
                    if buy_ex == sell_ex:
                        continue
                    buy_price  = prices_at_ts[buy_ex]
                    sell_price = prices_at_ts[sell_ex]
                    total_fee  = FEES.get(buy_ex, 0.002) + FEES.get(sell_ex, 0.002)
                    gross      = (sell_price - buy_price) / buy_price
                    net        = gross - total_fee

                    if net > MIN_NET:
                        trade_usd = 200.0  # conservative $200 per trade
                        qty       = trade_usd / buy_price
                        est_pnl   = qty * buy_price * net
                        all_opps.append({
                            "ts":         ts,
                            "ts_str":     datetime.utcfromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M"),
                            "pair":       pair,
                            "buy_ex":     buy_ex,
                            "sell_ex":    sell_ex,
                            "buy_price":  buy_price,
                            "sell_price": sell_price,
                            "net_margin": net,
                            "est_pnl":    est_pnl,
                        })

    all_opps.sort(key=lambda x: x["net_margin"], reverse=True)
    return all_opps

# ─── Report Builder ───────────────────────────────────────────────────────────
def build_backfill_report(opps: list, ex_ok: list, ex_skip: list, scan_time: float) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"=== ARB BACKFILL REPORT ===",
        f"Scanned: {DAYS} days | {TF} candles",
        f"Exchanges OK: {len(ex_ok)} / {len(FEES)}",
        f"Skipped: {', '.join(ex_skip) if ex_skip else 'none'}",
        f"Scan time: {scan_time:.0f}s",
        f"Generated: {now}",
        "-" * 40,
    ]

    if not opps:
        lines.append("NO MISSED OPPORTUNITIES found above 0.1% net threshold.")
        lines.append("Market was efficient in this period.")
        return "\n".join(lines)

    # Summary by pair
    from collections import defaultdict
    by_pair = defaultdict(list)
    for o in opps:
        by_pair[o["pair"]].append(o)

    total_est_pnl = sum(o["est_pnl"] for o in opps[:50])  # top 50 opportunities

    lines.append(f"TOTAL OPPORTUNITIES: {len(opps)}")
    lines.append(f"(on $200/trade, top 50 est PnL: ${total_est_pnl:.2f})")
    lines.append("-" * 40)

    # Per pair summary
    for pair, pair_opps in by_pair.items():
        best = pair_opps[0]
        lines.append(
            f"{pair}: {len(pair_opps)} opps | "
            f"Best: {best['net_margin']*100:.3f}% net "
            f"({best['buy_ex'].upper()}→{best['sell_ex'].upper()} @ {best['ts_str']})"
        )

    lines.append("-" * 40)
    lines.append("TOP 10 OPPORTUNITIES:")
    for i, o in enumerate(opps[:10], 1):
        lines.append(
            f"#{i} {o['ts_str']} | {o['pair']}: "
            f"{o['buy_ex'].upper()} @{o['buy_price']:,.4f} -> "
            f"{o['sell_ex'].upper()} @{o['sell_price']:,.4f} | "
            f"Net: {o['net_margin']*100:.3f}% | +${o['est_pnl']:.3f}"
        )

    lines.append("-" * 40)
    # Best route summary
    route_counts = {}
    for o in opps:
        route = f"{o['buy_ex']}→{o['sell_ex']}"
        route_counts[route] = route_counts.get(route, 0) + 1
    top_routes = sorted(route_counts.items(), key=lambda x: x[1], reverse=True)[:5]
    lines.append("TOP ROUTES (most frequent opportunities):")
    for route, count in top_routes:
        lines.append(f"  {route}: {count} times")

    return "\n".join(lines)

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 65)
    log.info(f"  ARB BACKFILL: {DAYS} days | {TF} | {len(FEES)} exchanges | {len(PAIRS)} pairs")
    log.info("=" * 65)
    t0 = time.time()

    # Step 1: Init all 11 exchanges in parallel
    log.info("Step 1: Initializing exchanges...")
    exchanges = {}
    with ThreadPoolExecutor(max_workers=len(FEES)) as pool:
        futs = {pool.submit(build_exchange, n): n for n in FEES}
        for fut in as_completed(futs):
            name, ex = fut.result()
            if ex:
                exchanges[name] = ex

    ex_ok   = list(exchanges.keys())
    ex_skip = [n for n in FEES if n not in exchanges]
    log.info(f"Ready: {len(ex_ok)}/{len(FEES)} | Skipped: {ex_skip}")

    if len(ex_ok) < 2:
        send_telegram("Backfill ERROR: fewer than 2 exchanges available.")
        return

    # Step 2: Fetch OHLCV for all exchange+pair combos in parallel
    log.info("Step 2: Fetching OHLCV...")
    tasks = [(name, ex, pair) for name, ex in exchanges.items() for pair in PAIRS]

    # ohlcv_data[pair][exchange] = {ts_ms: close}
    ohlcv_data = {p: {} for p in PAIRS}

    with ThreadPoolExecutor(max_workers=min(len(tasks), 30)) as pool:
        futs = {pool.submit(fetch_ohlcv_one, t): t for t in tasks}
        for fut in as_completed(futs):
            name, pair, candles = fut.result()
            if candles:
                ohlcv_data[pair][name] = {ts: close for ts, close in candles}

    log.info("OHLCV fetch complete.")

    # Step 3: Find missed opportunities
    log.info("Step 3: Scanning for missed opportunities...")
    opps = find_missed_opportunities(ohlcv_data)

    scan_time = time.time() - t0
    log.info(f"Found {len(opps)} opportunities in {scan_time:.1f}s")

    # Step 4: Build and send report
    report = build_backfill_report(opps, ex_ok, ex_skip, scan_time)
    log.info("=" * 65)
    log.info(report)
    log.info("=" * 65)

    send_telegram(report)
    log.info("Backfill complete.")


if __name__ == "__main__":
    main()
