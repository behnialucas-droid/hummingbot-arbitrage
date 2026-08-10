"""
╔══════════════════════════════════════════════════════════════╗
║       PRO ARBITRAGE SCANNER v3 — FAST & STABLE              ║
║  4 Exchanges | 5 Pairs | Pre-loaded Markets | Fixed TG       ║
╚══════════════════════════════════════════════════════════════╝
Paper Trading | Starting balance: $10,000 USDT
"""

import os, sys, time, json, logging, urllib.request, urllib.parse
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from typing import Optional
import ccxt

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("arb")

# ─── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")
PAPER_BALANCE  = float(os.getenv("PAPER_BALANCE",  "10000.0"))
MAX_TRADE_PCT  = float(os.getenv("MAX_TRADE_PCT",   "0.02"))
STOP_LOSS_PCT  = float(os.getenv("STOP_LOSS_PCT",   "0.10"))
MIN_PROFIT_PCT = float(os.getenv("MIN_PROFIT_PCT",  "0.001"))   # 0.1% after fees

FEES = {
    "mexc":   0.0010,
    "kucoin": 0.0010,
    "gateio": 0.0010,
    "htx":    0.0020,
}

PAIRS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]

# Per-exchange timeout (Gate.io needs slightly more for market init)
TIMEOUTS = {
    "mexc":   6000,
    "kucoin": 6000,
    "gateio": 8000,
    "htx":    6000,
}

# ─── Exchange Init + Market Pre-load ──────────────────────────────────────────
def init_exchange(name: str, ex: ccxt.Exchange) -> Optional[ccxt.Exchange]:
    """Pre-load markets so fetch_ticker won't trigger a slow first-call init."""
    try:
        ex.load_markets()
        log.info(f"  {name}: {len(ex.markets)} markets loaded")
        return ex
    except Exception as e:
        log.warning(f"  {name}: market load failed — {e}")
        return None

def build_exchanges() -> dict:
    raw = {
        "mexc":   ccxt.mexc(  {"enableRateLimit": False, "timeout": TIMEOUTS["mexc"]}),
        "kucoin": ccxt.kucoin({"enableRateLimit": False, "timeout": TIMEOUTS["kucoin"]}),
        "gateio": ccxt.gateio({"enableRateLimit": False, "timeout": TIMEOUTS["gateio"]}),
        "htx":    ccxt.htx(   {"enableRateLimit": False, "timeout": TIMEOUTS["htx"]}),
    }
    # Pre-load all markets in parallel
    ready = {}
    log.info("Pre-loading markets in parallel...")
    with ThreadPoolExecutor(max_workers=len(raw)) as pool:
        futs = {pool.submit(init_exchange, n, ex): n for n, ex in raw.items()}
        for fut in futs:
            name = futs[fut]
            result = fut.result()
            if result:
                ready[name] = result
    log.info(f"Ready exchanges: {list(ready.keys())}")
    return ready

# ─── Parallel Price Fetch ─────────────────────────────────────────────────────
def fetch_one(args) -> tuple:
    ex_name, exchange, symbol = args
    for attempt in range(2):
        try:
            t = exchange.fetch_ticker(symbol)
            ask, bid = t.get("ask"), t.get("bid")
            if ask and bid and ask > 0 and bid > 0:
                return (ex_name, symbol, {"ask": ask, "bid": bid})
            return (ex_name, symbol, None)
        except ccxt.NetworkError:
            if attempt == 0:
                time.sleep(0.5)
        except Exception:
            break
    return (ex_name, symbol, None)

def fetch_all_prices(exchanges: dict) -> tuple:
    tasks  = [(n, ex, sym) for n, ex in exchanges.items() for sym in PAIRS]
    prices = {n: {} for n in exchanges}
    errors = []

    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        futs = {pool.submit(fetch_one, t): t for t in tasks}
        for fut in futs:
            ex_name, symbol, data = fut.result()
            if data:
                prices[ex_name][symbol] = data
                log.info(f"  {ex_name:8s} | {symbol:10s} | Ask:{data['ask']:>12.4f}  Bid:{data['bid']:>12.4f}")
            else:
                errors.append(f"{ex_name}/{symbol}")

    return prices, errors

# ─── Arbitrage Detection ──────────────────────────────────────────────────────
def find_opportunities(prices: dict, balance: float) -> list:
    ids  = list(prices.keys())
    opps = []
    for i, buy_ex in enumerate(ids):
        for j, sell_ex in enumerate(ids):
            if i == j:
                continue
            for sym in PAIRS:
                bd = prices[buy_ex].get(sym)
                sd = prices[sell_ex].get(sym)
                if not bd or not sd:
                    continue
                total_fee    = FEES.get(buy_ex, 0.001) + FEES.get(sell_ex, 0.001)
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

# ─── Telegram (plain text — no HTML to avoid parse errors) ───────────────────
def send_telegram(msg: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        log.warning("No Telegram credentials.")
        return False
    # Trim if over Telegram limit
    if len(msg) > 4000:
        msg = msg[:3990] + "\n...[truncated]"
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT,
            "text":    msg,
            # No parse_mode — plain text is most robust
        }).encode()
        with urllib.request.urlopen(
            urllib.request.Request(url, data=data, method="POST"), timeout=15
        ) as r:
            resp = json.loads(r.read())
            if resp.get("ok"):
                log.info("Telegram sent OK.")
                return True
            log.warning(f"Telegram API error: {resp}")
            return False
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
        return False

# ─── Report (plain text) ──────────────────────────────────────────────────────
def build_report(prices, opps, balance, total_pnl, scan_t, scan_n, errors) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    dd  = (total_pnl / PAPER_BALANCE) * 100
    total_prices = sum(len(v) for v in prices.values())

    lines = [
        f"=== ARB Scanner v3 | Scan #{scan_n} ===",
        f"Time: {now}  |  Speed: {scan_t:.1f}s",
        f"Markets: {len(prices)} exchanges x {len(PAIRS)} pairs = {total_prices} live prices",
        "-" * 36,
        "LIVE PRICES (Ask / Bid):",
    ]

    for pair in PAIRS:
        row = []
        for ex in prices:
            d = prices[ex].get(pair)
            if d:
                row.append(f"{ex[:4].upper()}: {d['ask']:,.2f}")
        if row:
            lines.append(f"  {pair}: " + " | ".join(row))

    lines.append("-" * 36)

    if opps:
        lines.append(f">>> {len(opps)} ARBITRAGE OPPORTUNITY(IES) FOUND!")
        for i, o in enumerate(opps[:5], 1):
            lines.append(
                f"  #{i} {o['symbol']}: "
                f"Buy {o['buy_ex'].upper()} @{o['buy_price']:,.2f} -> "
                f"Sell {o['sell_ex'].upper()} @{o['sell_price']:,.2f} | "
                f"Net: {o['net_margin']*100:.3f}% | Est. +${o['est_pnl']:.2f}"
            )
        best = opps[0]
        lines.append(
            f"\n[APPLIED] {best['symbol']}: "
            f"{best['buy_ex'].upper()} -> {best['sell_ex'].upper()} "
            f"| +${best['est_pnl']:.2f}"
        )
    else:
        lines.append("No opportunity this cycle (spreads < 0.1% after fees).")

    lines += [
        "-" * 36,
        f"Portfolio: ${balance:,.2f} | PnL: ${total_pnl:+,.2f} ({dd:+.2f}%)",
        f"Start: ${PAPER_BALANCE:,.2f}",
    ]
    if errors:
        lines.append(f"Skipped: {', '.join(errors[:5])}")
    lines.append("Next scan: ~5 min")
    return "\n".join(lines)

# ─── State ────────────────────────────────────────────────────────────────────
STATE = "/tmp/arb_state.json"

def load_state():
    try:
        if os.path.exists(STATE):
            return json.load(open(STATE))
    except Exception:
        pass
    return {"balance": PAPER_BALANCE, "total_pnl": 0.0, "scan_count": 0}

def save_state(s):
    try:
        json.dump(s, open(STATE, "w"))
    except Exception:
        pass

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("  PRO ARB SCANNER v3")
    log.info(f"  Pairs: {', '.join(PAIRS)}")
    log.info(f"  Min profit: {MIN_PROFIT_PCT*100:.2f}% after fees")
    log.info("=" * 60)

    t0 = time.time()

    state      = load_state()
    balance    = state["balance"]
    total_pnl  = state["total_pnl"]
    scan_count = state["scan_count"] + 1

    # Stop-loss guard
    if total_pnl < 0 and abs(total_pnl) / PAPER_BALANCE >= STOP_LOSS_PCT:
        msg = (
            f"STOP LOSS TRIGGERED!\n"
            f"Drawdown: {abs(total_pnl)/PAPER_BALANCE*100:.1f}%\n"
            f"Balance: ${balance:,.2f} | PnL: ${total_pnl:+,.2f}"
        )
        log.error(msg)
        send_telegram(msg)
        sys.exit(0)

    # Step 1: Build + pre-load markets (parallel)
    t1 = time.time()
    exchanges = build_exchanges()
    log.info(f"Market pre-load: {time.time()-t1:.1f}s")

    # Step 2: Fetch all prices (parallel)
    t2 = time.time()
    prices, errors = fetch_all_prices(exchanges)
    log.info(f"Price fetch: {time.time()-t2:.1f}s")

    # Step 3: Detect arbitrage
    opps = find_opportunities(prices, balance)

    if opps:
        best       = opps[0]
        balance   += best["est_pnl"]
        total_pnl += best["est_pnl"]

    save_state({"balance": balance, "total_pnl": total_pnl, "scan_count": scan_count})

    scan_t = time.time() - t0
    log.info(f"Scan #{scan_count} complete in {scan_t:.1f}s | Balance: ${balance:,.2f}")

    report = build_report(prices, opps, balance, total_pnl, scan_t, scan_count, errors)
    send_telegram(report)

    log.info("=" * 60)


if __name__ == "__main__":
    main()
