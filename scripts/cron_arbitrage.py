"""
╔══════════════════════════════════════════════════════════════╗
║         PRO ARBITRAGE SCANNER — hummingbot-arbitrage         ║
║  4 Exchanges | 5 Pairs | Risk Management | Full TG Reports   ║
╚══════════════════════════════════════════════════════════════╝
Paper Trading Mode — No real orders are placed.
Starting balance: $10,000 USDT (simulated)
"""

import os
import sys
import time
import json
import logging
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

import ccxt

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("arb")

# ─── Configuration ────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")

# Starting paper-trading balance
PAPER_BALANCE  = float(os.getenv("PAPER_BALANCE", "10000.0"))   # USDT
MAX_TRADE_PCT  = float(os.getenv("MAX_TRADE_PCT",  "0.02"))      # 2% per trade
STOP_LOSS_PCT  = float(os.getenv("STOP_LOSS_PCT",  "0.10"))      # 10% max drawdown
MIN_PROFIT_PCT = float(os.getenv("MIN_PROFIT_PCT", "0.001"))     # 0.1% after fees

# Exchange fees (maker/taker average, conservative)
FEES = {
    "mexc":    0.0010,   # 0.10%
    "kucoin":  0.0010,   # 0.10%
    "gateio":  0.0010,   # 0.10%
    "htx":     0.0020,   # 0.20%
}

# Pairs to scan (most liquid cross-exchange pairs)
PAIRS = [
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "BNB/USDT",
    "XRP/USDT",
]

# ─── Exchange Factory ─────────────────────────────────────────────────────────
def build_exchanges() -> dict:
    """Create exchange connectors — public data only, no API keys needed."""
    opts = {"enableRateLimit": True, "timeout": 10000}
    return {
        "mexc":   ccxt.mexc(opts),
        "kucoin": ccxt.kucoin(opts),
        "gateio": ccxt.gateio(opts),
        "htx":    ccxt.htx(opts),
    }

# ─── Fetch Ticker with Retry ──────────────────────────────────────────────────
def fetch_ticker_safe(exchange, symbol: str, retries: int = 2) -> Optional[dict]:
    for attempt in range(retries + 1):
        try:
            ticker = exchange.fetch_ticker(symbol)
            ask = ticker.get("ask")
            bid = ticker.get("bid")
            if ask and bid and ask > 0 and bid > 0:
                return ticker
            return None
        except ccxt.NetworkError as e:
            if attempt < retries:
                time.sleep(1.5)
                continue
            log.warning(f"Network error {exchange.id}/{symbol}: {e}")
            return None
        except ccxt.ExchangeError as e:
            log.warning(f"Exchange error {exchange.id}/{symbol}: {e}")
            return None
        except Exception as e:
            log.warning(f"Unexpected error {exchange.id}/{symbol}: {e}")
            return None

# ─── Arbitrage Detection ──────────────────────────────────────────────────────
def find_opportunities(prices: dict, balance: float) -> list:
    """
    Find all arbitrage opportunities across all exchange pairs.
    Returns list of opportunity dicts sorted by net profit descending.
    """
    opportunities = []
    exchange_ids = list(prices.keys())

    for i in range(len(exchange_ids)):
        for j in range(len(exchange_ids)):
            if i == j:
                continue
            buy_ex  = exchange_ids[i]
            sell_ex = exchange_ids[j]

            for symbol in PAIRS:
                buy_data  = prices[buy_ex].get(symbol)
                sell_data = prices[sell_ex].get(symbol)
                if not buy_data or not sell_data:
                    continue

                buy_ask  = buy_data["ask"]   # price to buy on buy_ex
                sell_bid = sell_data["bid"]  # price to sell on sell_ex

                # Net profit after fees on both legs
                total_fee = FEES.get(buy_ex, 0.001) + FEES.get(sell_ex, 0.001)
                gross_margin = (sell_bid - buy_ask) / buy_ask
                net_margin   = gross_margin - total_fee

                if net_margin > MIN_PROFIT_PCT:
                    # Position sizing: max 2% of balance, capped at $2,000
                    max_trade_usd = min(balance * MAX_TRADE_PCT, 2000.0)
                    trade_qty     = max_trade_usd / buy_ask
                    estimated_pnl = trade_qty * buy_ask * net_margin

                    opportunities.append({
                        "symbol":       symbol,
                        "buy_exchange":  buy_ex,
                        "sell_exchange": sell_ex,
                        "buy_price":    buy_ask,
                        "sell_price":   sell_bid,
                        "gross_margin": gross_margin,
                        "net_margin":   net_margin,
                        "trade_usd":    max_trade_usd,
                        "trade_qty":    trade_qty,
                        "est_pnl":      estimated_pnl,
                        "fee_pct":      total_fee,
                    })

    # Sort by net profit, best first
    opportunities.sort(key=lambda x: x["net_margin"], reverse=True)
    return opportunities

# ─── Telegram ─────────────────────────────────────────────────────────────────
def send_telegram(message: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        log.warning("Telegram credentials missing — skipping notification.")
        return False
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id":    TELEGRAM_CHAT,
            "text":       message,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                log.info("✅ Telegram report sent.")
                return True
            else:
                log.warning(f"Telegram API returned not-ok: {result}")
                return False
    except Exception as e:
        log.error(f"Failed to send Telegram message: {e}")
        return False

# ─── Report Builder ───────────────────────────────────────────────────────────
def build_report(
    prices:        dict,
    opportunities: list,
    balance:       float,
    total_pnl:     float,
    scan_time:     float,
    scan_num:      int,
    errors:        list,
) -> str:
    now    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    drawdown = (total_pnl / PAPER_BALANCE) * 100

    lines = [
        f"<b>🤖 Arbitrage Bot — Scan #{scan_num}</b>",
        f"🕐 {now}",
        f"⚡ Scan time: {scan_time:.1f}s",
        "─────────────────────────",
    ]

    # ── Price Table ──
    lines.append("📊 <b>Live Prices (Ask / Bid)</b>")
    for pair in PAIRS:
        pair_lines = []
        for ex, data in prices.items():
            d = data.get(pair)
            if d:
                pair_lines.append(
                    f"  {ex.upper()[:4]}: {d['ask']:,.2f}/{d['bid']:,.2f}"
                )
        if pair_lines:
            lines.append(f"\n<b>{pair}</b>")
            lines.extend(pair_lines)

    lines.append("─────────────────────────")

    # ── Opportunities ──
    if opportunities:
        lines.append(f"🎯 <b>{len(opportunities)} Opportunity(ies) Found!</b>")
        for i, opp in enumerate(opportunities[:5], 1):  # show top 5
            lines.append(
                f"\n<b>#{i} {opp['symbol']}</b>\n"
                f"  Buy  {opp['buy_exchange'].upper()} @ {opp['buy_price']:,.2f}\n"
                f"  Sell {opp['sell_exchange'].upper()} @ {opp['sell_price']:,.2f}\n"
                f"  Net: {opp['net_margin']*100:.3f}% | Est. PnL: +${opp['est_pnl']:.2f}\n"
                f"  Size: ${opp['trade_usd']:.0f} ({opp['trade_qty']:.6f} units)"
            )
        # Apply best trade to paper balance
        best     = opportunities[0]
        new_balance = balance + best["est_pnl"]
        lines.append(
            f"\n✅ <b>Best trade applied (paper):</b>\n"
            f"  {best['symbol']}: Buy {best['buy_exchange'].upper()} → "
            f"Sell {best['sell_exchange'].upper()}\n"
            f"  Profit: +${best['est_pnl']:.2f}"
        )
    else:
        lines.append("😴 No profitable opportunities this cycle.")
        lines.append("  (All spreads below 0.1% after fees)")

    lines.append("─────────────────────────")

    # ── Portfolio ──
    lines.append("💰 <b>Paper Portfolio</b>")
    lines.append(f"  Balance:    ${balance:,.2f}")
    lines.append(f"  Total PnL:  ${total_pnl:+,.2f} ({drawdown:+.2f}%)")
    lines.append(f"  Start:      ${PAPER_BALANCE:,.2f}")

    # ── Errors (if any) ──
    if errors:
        lines.append("─────────────────────────")
        lines.append(f"⚠️ <b>Warnings ({len(errors)}):</b>")
        for err in errors[:3]:
            lines.append(f"  • {err}")

    lines.append("─────────────────────────")
    lines.append("🔄 Next scan in ~5 min")

    return "\n".join(lines)

# ─── State File (Persistent Paper Balance) ────────────────────────────────────
STATE_FILE = "/tmp/arb_state.json"

def load_state() -> dict:
    """Load paper balance state from file (persists within one GH Actions run)."""
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {"balance": PAPER_BALANCE, "total_pnl": 0.0, "scan_count": 0}

def save_state(state: dict):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    log.info("═" * 60)
    log.info("  PRO ARBITRAGE SCANNER — Starting")
    log.info(f"  Pairs:     {', '.join(PAIRS)}")
    log.info(f"  Min Profit: {MIN_PROFIT_PCT*100:.2f}% (after fees)")
    log.info("═" * 60)

    t_start = time.time()
    errors  = []

    # ── Load state ──
    state       = load_state()
    balance     = state["balance"]
    total_pnl   = state["total_pnl"]
    scan_count  = state["scan_count"] + 1

    # ── Stop-loss guard ──
    drawdown_pct = abs(total_pnl) / PAPER_BALANCE if total_pnl < 0 else 0
    if drawdown_pct >= STOP_LOSS_PCT:
        msg = (
            f"⛔ STOP LOSS TRIGGERED!\n"
            f"Drawdown: {drawdown_pct*100:.1f}% exceeds limit of {STOP_LOSS_PCT*100:.0f}%\n"
            f"Balance: ${balance:,.2f} | PnL: ${total_pnl:+,.2f}\n"
            f"Bot paused. Manual review required."
        )
        log.error(msg)
        send_telegram(msg)
        sys.exit(0)

    # ── Build exchanges ──
    exchanges = build_exchanges()
    log.info(f"Exchanges: {', '.join(exchanges.keys())}")

    # ── Fetch all prices ──
    prices = {}
    for ex_name, ex in exchanges.items():
        prices[ex_name] = {}
        for pair in PAIRS:
            ticker = fetch_ticker_safe(ex, pair)
            if ticker:
                prices[ex_name][pair] = {
                    "ask": ticker["ask"],
                    "bid": ticker["bid"],
                }
                log.info(f"  {ex_name:8s} | {pair:10s} | Ask: {ticker['ask']:12.4f} | Bid: {ticker['bid']:12.4f}")
            else:
                errors.append(f"{ex_name}/{pair}: fetch failed")

    # ── Find opportunities ──
    opportunities = find_opportunities(prices, balance)

    # ── Apply best opportunity to paper balance ──
    if opportunities:
        best     = opportunities[0]
        trade_pnl = best["est_pnl"]
        balance  += trade_pnl
        total_pnl += trade_pnl
        log.info(
            f"📈 Best opportunity: {best['symbol']} | "
            f"Buy {best['buy_exchange']} → Sell {best['sell_exchange']} | "
            f"Net {best['net_margin']*100:.3f}% | PnL +${trade_pnl:.2f}"
        )

    # ── Save updated state ──
    save_state({"balance": balance, "total_pnl": total_pnl, "scan_count": scan_count})

    scan_time = time.time() - t_start
    log.info(f"Scan #{scan_count} completed in {scan_time:.1f}s | Balance: ${balance:,.2f}")

    # ── Build & Send full Telegram report ──
    report = build_report(
        prices=prices,
        opportunities=opportunities,
        balance=balance,
        total_pnl=total_pnl,
        scan_time=scan_time,
        scan_num=scan_count,
        errors=errors,
    )
    send_telegram(report)

    log.info("═" * 60)
    log.info("  Done.")
    log.info("═" * 60)


if __name__ == "__main__":
    main()
