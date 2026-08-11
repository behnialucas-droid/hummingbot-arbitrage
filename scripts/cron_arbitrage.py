"""
PRO ARBITRAGE SCANNER v5 — FIXED EDITION
11 Exchanges | 5 Pairs | Every-5-min Cron | TG Every Run

Fixes applied:
  [FIX 1] Cron: */5 * * * * (GitHub minimum; */1 was throttled — avg 66min gap)
  [FIX 2] State via git commit to arb_state.json (artifacts dont persist cross-run)
  [FIX 3] Telegram every run — no throttle (runs are 5+ min apart anyway)
"""

import os, sys, time, json, logging, urllib.request, urllib.parse, subprocess
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
import ccxt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("arb")

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT   = os.getenv("TELEGRAM_CHAT_ID", "")
PAPER_BALANCE   = float(os.getenv("PAPER_BALANCE",   "10000.0"))
MAX_TRADE_PCT   = float(os.getenv("MAX_TRADE_PCT",   "0.02"))
STOP_LOSS_PCT   = float(os.getenv("STOP_LOSS_PCT",   "0.10"))
MIN_PROFIT_PCT  = float(os.getenv("MIN_PROFIT_PCT",  "0.001"))

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

PAIRS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]

TIMEOUTS = {
    "mexc":      7000,
    "kucoin":    7000,
    "gateio":    9000,
    "htx":       7000,
    "bitget":    6000,
    "bingx":     6000,
    "bitmart":   6000,
    "phemex":    6000,
    "whitebit":  6000,
    "bitstamp":  6000,
    "digifinex": 6000,
}

# FIX 2: State file committed to repo — not /tmp (ephemeral, broken cross-run)
STATE_FILE = "arb_state.json"


def build_exchange(name: str) -> Optional[tuple]:
    try:
        klass = getattr(ccxt, name)
        ex    = klass({"enableRateLimit": False, "timeout": TIMEOUTS.get(name, 7000)})
        ex.load_markets()
        log.info(f"  INIT {name:12s}: {len(ex.markets):,} markets")
        return (name, ex)
    except Exception as e:
        log.warning(f"  SKIP {name:12s}: {str(e)[:60]}")
        return None


def build_exchanges() -> dict:
    names = list(FEES.keys())
    log.info(f"Initializing {len(names)} exchanges in parallel...")
    ready = {}
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        futs = {pool.submit(build_exchange, n): n for n in names}
        for fut in as_completed(futs):
            result = fut.result()
            if result:
                name, ex = result
                ready[name] = ex
    log.info(f"Ready: {len(ready)}/{len(names)} exchanges")
    return ready


def fetch_one(args) -> tuple:
    ex_name, exchange, symbol = args
    for attempt in range(2):
        try:
            t = exchange.fetch_ticker(symbol)
            ask, bid = t.get("ask"), t.get("bid")
            if ask and bid and ask > 0 and bid > 0:
                return (ex_name, symbol, {"ask": float(ask), "bid": float(bid)})
            return (ex_name, symbol, None)
        except ccxt.NetworkError:
            if attempt == 0:
                time.sleep(0.3)
        except Exception:
            break
    return (ex_name, symbol, None)


def fetch_all_prices(exchanges: dict) -> tuple:
    tasks  = [(n, ex, sym) for n, ex in exchanges.items() for sym in PAIRS]
    prices = {n: {} for n in exchanges}
    errors = []
    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        futs = {pool.submit(fetch_one, t): t for t in tasks}
        for fut in as_completed(futs):
            ex_name, symbol, data = fut.result()
            if data:
                prices[ex_name][symbol] = data
                log.info(f"  {ex_name:12s} | {symbol:10s} | Ask:{data['ask']:>12.4f}  Bid:{data['bid']:>12.4f}")
            else:
                errors.append(f"{ex_name}/{symbol}")
    return prices, errors


def find_opportunities(prices: dict, balance: float) -> list:
    """
    550 checks per scan: 11 exchanges x 10 combos x 5 pairs.
    Returns opportunities sorted by net_margin descending.
    """
    ids  = list(prices.keys())
    opps = []
    for buy_ex in ids:
        for sell_ex in ids:
            if buy_ex == sell_ex:
                continue
            for sym in PAIRS:
                bd = prices[buy_ex].get(sym)
                sd = prices[sell_ex].get(sym)
                if not bd or not sd:
                    continue
                total_fee    = FEES.get(buy_ex, 0.002) + FEES.get(sell_ex, 0.002)
                gross_margin = (sd["bid"] - bd["ask"]) / bd["ask"]
                net_margin   = gross_margin - total_fee
                if net_margin > MIN_PROFIT_PCT:
                    trade_usd = min(balance * MAX_TRADE_PCT, 2000.0)
                    qty       = trade_usd / bd["ask"]
                    opps.append({
                        "symbol":     sym,
                        "buy_ex":     buy_ex,
                        "sell_ex":    sell_ex,
                        "buy_price":  bd["ask"],
                        "sell_price": sd["bid"],
                        "net_margin": net_margin,
                        "trade_usd":  trade_usd,
                        "est_pnl":    qty * bd["ask"] * net_margin,
                    })
    opps.sort(key=lambda x: x["net_margin"], reverse=True)
    return opps


def send_telegram(msg: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        log.warning("Telegram credentials not set.")
        return False
    if len(msg) > 4000:
        msg = msg[:3990] + "\n...[truncated]"
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT, "text": msg}).encode()
        with urllib.request.urlopen(
            urllib.request.Request(url, data=data, method="POST"), timeout=15
        ) as r:
            resp = json.loads(r.read())
            if resp.get("ok"):
                log.info("Telegram OK.")
                return True
            log.warning(f"Telegram API error: {resp}")
            return False
    except Exception as e:
        log.error(f"Telegram failed: {e}")
        return False


def build_report(prices, opps, balance, total_pnl, scan_t, scan_n, errors, ex_count) -> str:
    now    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    dd     = (total_pnl / PAPER_BALANCE) * 100
    total  = sum(len(v) for v in prices.values())
    combos = ex_count * (ex_count - 1) * len(PAIRS)

    lines = [
        f"=== ARB SCANNER v5 | Scan #{scan_n} ===",
        f"Time: {now}  |  Speed: {scan_t:.1f}s",
        f"Coverage: {ex_count} exchanges x {len(PAIRS)} pairs = {total} live prices",
        f"Checks: {combos} arbitrage comparisons",
        "-" * 40,
        "LIVE PRICES (Ask):",
    ]
    for pair in PAIRS:
        row = []
        for ex in prices:
            d = prices[ex].get(pair)
            if d:
                row.append(f"{ex[:4].upper()}:{d['ask']:,.2f}")
        if row:
            lines.append(f"  {pair}: " + " | ".join(row))

    lines.append("-" * 40)
    if opps:
        lines.append(f">>> {len(opps)} OPPORTUNITY(IES) FOUND!")
        for i, o in enumerate(opps[:5], 1):
            lines.append(
                f"  #{i} {o['symbol']}: "
                f"{o['buy_ex'].upper()} @{o['buy_price']:,.4f} -> "
                f"{o['sell_ex'].upper()} @{o['sell_price']:,.4f} "
                f"| Net: {o['net_margin']*100:.3f}% | +${o['est_pnl']:.4f}"
            )
        best = opps[0]
        lines.append(
            f"\n[APPLIED] {best['symbol']}: "
            f"{best['buy_ex'].upper()} -> {best['sell_ex'].upper()} "
            f"| +${best['est_pnl']:.4f}"
        )
    else:
        lines.append("No opportunity this cycle (spreads < 0.1% after fees).")

    lines += [
        "-" * 40,
        f"Portfolio : ${balance:,.2f}",
        f"PnL       : ${total_pnl:+,.4f} ({dd:+.4f}%)",
        f"Start     : ${PAPER_BALANCE:,.2f}",
    ]
    if errors:
        lines.append(f"Skipped   : {', '.join(errors[:6])}")
    return "\n".join(lines)


def load_state() -> dict:
    """Load state from repo-committed file (survives across GitHub Actions runs)."""
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                state = json.load(f)
            log.info(f"State loaded: balance=${state.get('balance', PAPER_BALANCE):,.2f} scan#{state.get('scan_count', 0)}")
            return state
    except Exception as e:
        log.warning(f"State load failed: {e}")
    log.info("Starting fresh state.")
    return {"balance": PAPER_BALANCE, "total_pnl": 0.0, "scan_count": 0}


def save_state(s: dict) -> None:
    """Save state to repo file and git-commit so next run picks it up."""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(s, f, indent=2)
        log.info(f"State saved: balance=${s['balance']:,.2f} scan#{s['scan_count']}")
    except Exception as e:
        log.error(f"State write failed: {e}")
        return

    try:
        subprocess.run(["git", "add", STATE_FILE],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        status = subprocess.run(["git", "status", "--porcelain"],
                                capture_output=True, text=True)
        if STATE_FILE in status.stdout:
            subprocess.run(
                ["git", "commit", "-m", f"chore: arb state scan #{s['scan_count']} [skip ci]"],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            subprocess.run(["git", "pull", "--rebase"],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "push"],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log.info("State committed to GitHub.")
    except Exception as e:
        log.warning(f"Git commit failed (state saved locally): {e}")


def main():
    log.info("=" * 65)
    log.info("  PRO ARB SCANNER v5 — 11 EXCHANGES")
    log.info(f"  Exchanges : {', '.join(FEES.keys())}")
    log.info(f"  Pairs     : {', '.join(PAIRS)}")
    log.info(f"  Min profit: {MIN_PROFIT_PCT*100:.2f}% after fees")
    log.info("=" * 65)

    t0 = time.time()

    state      = load_state()
    balance    = state["balance"]
    total_pnl  = state["total_pnl"]
    scan_count = state["scan_count"] + 1

    if total_pnl < 0 and abs(total_pnl) / PAPER_BALANCE >= STOP_LOSS_PCT:
        msg = (
            f"STOP LOSS TRIGGERED!\n"
            f"Drawdown: {abs(total_pnl)/PAPER_BALANCE*100:.1f}%\n"
            f"Balance: ${balance:,.2f} | PnL: ${total_pnl:+,.2f}"
        )
        log.error(msg)
        send_telegram(msg)
        sys.exit(0)

    t1 = time.time()
    exchanges = build_exchanges()
    log.info(f"Market init: {time.time()-t1:.1f}s")

    if not exchanges:
        log.error("No exchanges available — aborting.")
        sys.exit(1)

    t2 = time.time()
    prices, errors = fetch_all_prices(exchanges)
    log.info(f"Price fetch: {time.time()-t2:.1f}s")

    opps = find_opportunities(prices, balance)

    if opps:
        best       = opps[0]
        balance   += best["est_pnl"]
        total_pnl += best["est_pnl"]

    save_state({"balance": balance, "total_pnl": total_pnl, "scan_count": scan_count})

    scan_t   = time.time() - t0
    ex_count = len(exchanges)
    combos   = ex_count * (ex_count - 1) * len(PAIRS)

    log.info(f"Scan #{scan_count} done in {scan_t:.1f}s | {combos} checks | Balance: ${balance:,.2f}")
    if opps:
        log.info(f"Best opp: {opps[0]['net_margin']*100:.3f}% net on {opps[0]['symbol']}")

    # FIX 3: Send Telegram EVERY run (no throttle — each run is already 5+ min apart)
    log.info(f"Sending Telegram (scan #{scan_count})...")
    report = build_report(prices, opps, balance, total_pnl, scan_t, scan_count, errors, ex_count)
    send_telegram(report)

    log.info("=" * 65)


if __name__ == "__main__":
    main()
