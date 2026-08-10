"""
╔══════════════════════════════════════════════════════════════╗
║       PRO ARBITRAGE SCANNER v2 — PARALLEL EDITION           ║
║  4 Exchanges | 5 Pairs | Parallel Fetch | TG Every 5 min    ║
╚══════════════════════════════════════════════════════════════╝
Paper Trading | Starting balance: $10,000 USDT
"""

import os, sys, time, json, logging, urllib.request, urllib.parse
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
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
MAX_TRADE_PCT  = float(os.getenv("MAX_TRADE_PCT",   "0.02"))    # 2% per trade
STOP_LOSS_PCT  = float(os.getenv("STOP_LOSS_PCT",   "0.10"))    # 10% max drawdown
MIN_PROFIT_PCT = float(os.getenv("MIN_PROFIT_PCT",  "0.001"))   # 0.1% after fees

# Exchange taker fees (conservative)
FEES = {
    "mexc":   0.0010,
    "kucoin": 0.0010,
    "gateio": 0.0010,
    "htx":    0.0020,
}

# Pairs to scan — most volatile and high-volume
PAIRS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]

# ─── Exchange Init ─────────────────────────────────────────────────────────────
def build_exchanges() -> dict:
    opts = {"enableRateLimit": False, "timeout": 8000}  # RateLimit off for parallel
    return {
        "mexc":   ccxt.mexc(opts),
        "kucoin": ccxt.kucoin(opts),
        "gateio": ccxt.gateio(opts),
        "htx":    ccxt.htx(opts),
    }

# ─── Parallel Price Fetcher ────────────────────────────────────────────────────
def fetch_one(args) -> tuple:
    """Fetch a single ticker. Returns (ex_name, symbol, data_or_None)."""
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
                time.sleep(1)
        except Exception:
            break
    return (ex_name, symbol, None)

def fetch_all_prices(exchanges: dict) -> tuple[dict, list]:
    """
    Fetch ALL tickers in parallel using a thread pool.
    Returns (prices_dict, errors_list).
    ~9s serial → ~2s parallel.
    """
    tasks = [
        (ex_name, ex, symbol)
        for ex_name, ex in exchanges.items()
        for symbol in PAIRS
    ]

    prices: dict = {ex_name: {} for ex_name in exchanges}
    errors: list  = []

    # max_workers = number of tasks (all in parallel)
    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        futures = {pool.submit(fetch_one, task): task for task in tasks}
        for future in as_completed(futures):
            ex_name, symbol, data = future.result()
            if data:
                prices[ex_name][symbol] = data
                log.info(f"  {ex_name:8s} | {symbol:10s} | Ask:{data['ask']:>12.4f} Bid:{data['bid']:>12.4f}")
            else:
                errors.append(f"{ex_name}/{symbol}")

    return prices, errors

# ─── Arbitrage Detection ──────────────────────────────────────────────────────
def find_opportunities(prices: dict, balance: float) -> list:
    exchange_ids = list(prices.keys())
    opps = []

    for i, buy_ex in enumerate(exchange_ids):
        for j, sell_ex in enumerate(exchange_ids):
            if i == j:
                continue
            for symbol in PAIRS:
                bd = prices[buy_ex].get(symbol)
                sd = prices[sell_ex].get(symbol)
                if not bd or not sd:
                    continue

                total_fee    = FEES.get(buy_ex, 0.001) + FEES.get(sell_ex, 0.001)
                gross_margin = (sd["bid"] - bd["ask"]) / bd["ask"]
                net_margin   = gross_margin - total_fee

                if net_margin > MIN_PROFIT_PCT:
                    trade_usd = min(balance * MAX_TRADE_PCT, 2000.0)
                    qty       = trade_usd / bd["ask"]
                    opps.append({
                        "symbol":    symbol,
                        "buy_ex":    buy_ex,
                        "sell_ex":   sell_ex,
                        "buy_price": bd["ask"],
                        "sell_price":sd["bid"],
                        "net_margin":net_margin,
                        "trade_usd": trade_usd,
                        "est_pnl":   qty * bd["ask"] * net_margin,
                    })

    opps.sort(key=lambda x: x["net_margin"], reverse=True)
    return opps

# ─── Telegram ─────────────────────────────────────────────────────────────────
def send_telegram(msg: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        log.warning("No Telegram credentials.")
        return False
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"
        }).encode()
        with urllib.request.urlopen(
            urllib.request.Request(url, data=data, method="POST"), timeout=15
        ) as r:
            ok = json.loads(r.read()).get("ok", False)
            if ok:
                log.info("✅ Telegram sent.")
            return ok
    except Exception as e:
        log.error(f"Telegram error: {e}")
        return False

# ─── Report Builder ───────────────────────────────────────────────────────────
def build_report(prices, opps, balance, total_pnl, scan_t, scan_n, errors) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    dd  = (total_pnl / PAPER_BALANCE) * 100

    lines = [
        f"<b>🤖 ARB Scanner v2 — Scan #{scan_n}</b>",
        f"🕐 {now}  ⚡ {scan_t:.1f}s",
        "─────────────────────────",
        "📊 <b>Live Prices (Ask/Bid)</b>",
    ]

    for pair in PAIRS:
        row = []
        for ex in prices:
            d = prices[ex].get(pair)
            if d:
                row.append(f"{ex[:4].upper()}:{d['ask']:,.2f}")
        if row:
            lines.append(f"<b>{pair}</b>: " + " | ".join(row))

    lines.append("─────────────────────────")

    if opps:
        lines.append(f"🎯 <b>{len(opps)} Opportunity(ies)!</b>")
        for i, o in enumerate(opps[:5], 1):
            lines.append(
                f"#{i} <b>{o['symbol']}</b> "
                f"Buy {o['buy_ex'].upper()} @ {o['buy_price']:,.2f} → "
                f"Sell {o['sell_ex'].upper()} @ {o['sell_price']:,.2f} | "
                f"Net <b>{o['net_margin']*100:.3f}%</b> | "
                f"Est. <b>+${o['est_pnl']:.2f}</b>"
            )
        best = opps[0]
        lines.append(
            f"\n✅ Applied: {best['symbol']} "
            f"{best['buy_ex'].upper()}→{best['sell_ex'].upper()} "
            f"+${best['est_pnl']:.2f}"
        )
    else:
        lines.append("😴 No opportunity this cycle (spreads < 0.1% after fees)")

    lines += [
        "─────────────────────────",
        f"💰 Balance: <b>${balance:,.2f}</b> | PnL: <b>${total_pnl:+,.2f}</b> ({dd:+.2f}%)",
        f"📡 {len(prices)} exchanges | {len(PAIRS)} pairs | {len(prices)*len(PAIRS)} prices/scan",
    ]
    if errors:
        lines.append(f"⚠️ Skipped: {', '.join(errors[:4])}")
    lines.append("🔄 Next: ~5 min")
    return "\n".join(lines)

# ─── State Persistence ────────────────────────────────────────────────────────
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
    log.info("═"*60)
    log.info("  PRO ARB SCANNER v2 — PARALLEL EDITION")
    log.info(f"  Pairs: {', '.join(PAIRS)}")
    log.info(f"  Min profit: {MIN_PROFIT_PCT*100:.2f}% after fees")
    log.info("═"*60)

    t0 = time.time()

    state      = load_state()
    balance    = state["balance"]
    total_pnl  = state["total_pnl"]
    scan_count = state["scan_count"] + 1

    # Stop-loss guard
    if total_pnl < 0 and abs(total_pnl) / PAPER_BALANCE >= STOP_LOSS_PCT:
        msg = (f"⛔ STOP LOSS TRIGGERED!\n"
               f"Drawdown: {abs(total_pnl)/PAPER_BALANCE*100:.1f}%\n"
               f"Balance: ${balance:,.2f} | PnL: ${total_pnl:+,.2f}")
        log.error(msg)
        send_telegram(msg)
        sys.exit(0)

    exchanges = build_exchanges()
    log.info(f"Fetching {len(exchanges)*len(PAIRS)} prices in parallel...")

    prices, errors = fetch_all_prices(exchanges)

    opps = find_opportunities(prices, balance)

    if opps:
        best       = opps[0]
        trade_pnl  = best["est_pnl"]
        balance   += trade_pnl
        total_pnl += trade_pnl

    save_state({"balance": balance, "total_pnl": total_pnl, "scan_count": scan_count})

    scan_t = time.time() - t0
    log.info(f"Scan #{scan_count} done in {scan_t:.1f}s | Balance: ${balance:,.2f}")

    report = build_report(prices, opps, balance, total_pnl, scan_t, scan_count, errors)
    send_telegram(report)

    log.info("═"*60)


if __name__ == "__main__":
    main()
