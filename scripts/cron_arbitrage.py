import os
import ccxt
import asyncio
import logging
import sys

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

async def send_telegram_alert(bot_token, chat_id, message):
    if not bot_token or not chat_id:
        logger.warning("Telegram credentials not fully provided. Skipping alert.")
        return
    import urllib.request
    import urllib.parse
    
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = urllib.parse.urlencode({'chat_id': chat_id, 'text': message}).encode('utf-8')
    try:
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=10)
        logger.info("Telegram alert sent.")
    except Exception as e:
        logger.error(f"Failed to send telegram alert: {e}")

async def main():
    logger.info("Starting Single-Execution Arbitrage Scan (RUN_ONCE mode)")
    
    # Load Environment Variables
    binance_api_key = os.getenv("BINANCE_API_KEY")
    binance_api_secret = os.getenv("BINANCE_API_SECRET")
    
    kucoin_api_key = os.getenv("KUCOIN_API_KEY")
    kucoin_api_secret = os.getenv("KUCOIN_API_SECRET")
    kucoin_api_passphrase = os.getenv("KUCOIN_API_PASSPHRASE")
    
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
    
    symbol = os.getenv("ARBITRAGE_SYMBOL", "BTC/USDT")
    min_profit_margin = float(os.getenv("MIN_PROFIT_MARGIN", "0.002")) # 0.2% default
    trade_size = float(os.getenv("TRADE_SIZE", "0.001")) # Amount of BTC to trade
    
    try:
        # Initialize Exchanges for Public Data (No API Keys needed)
        bybit = ccxt.bybit({
            'enableRateLimit': True
        })
        
        kucoin = ccxt.kucoin({
            'enableRateLimit': True
        })
        
        logger.info(f"Checking prices for {symbol} on Bybit and KuCoin...")
        
        # Fetch Tickers
        bybit_ticker = bybit.fetch_ticker(symbol)
        kucoin_ticker = kucoin.fetch_ticker(symbol)
        
        bybit_ask = bybit_ticker['ask']
        bybit_bid = bybit_ticker['bid']
        
        kucoin_ask = kucoin_ticker['ask']
        kucoin_bid = kucoin_ticker['bid']
        
        logger.info(f"Bybit   | Ask: {bybit_ask}, Bid: {bybit_bid}")
        logger.info(f"KuCoin  | Ask: {kucoin_ask}, Bid: {kucoin_bid}")
        
        # Calculate Arbitrage Opportunities
        # Scenario 1: Buy on Bybit, Sell on KuCoin
        profit_margin_1 = (kucoin_bid - bybit_ask) / bybit_ask
        # Scenario 2: Buy on KuCoin, Sell on Bybit
        profit_margin_2 = (bybit_bid - kucoin_ask) / kucoin_ask
        
        best_scenario = None
        best_margin = 0
        
        if profit_margin_1 > min_profit_margin:
            best_scenario = "Buy Bybit, Sell KuCoin"
            best_margin = profit_margin_1
        elif profit_margin_2 > min_profit_margin:
            best_scenario = "Buy KuCoin, Sell Bybit"
            best_margin = profit_margin_2
            
        if best_scenario:
            msg = f"🚀 Arbitrage Opportunity Found!\nSymbol: {symbol}\nAction: {best_scenario}\nMargin: {best_margin*100:.2f}%"
            logger.info(msg)
            await send_telegram_alert(telegram_bot_token, telegram_chat_id, msg)
            
            # --- EXECUTION LOGIC (Uncomment to enable real trading) ---
            # if best_scenario == "Buy Binance, Sell KuCoin":
            #     binance.create_market_buy_order(symbol, trade_size)
            #     kucoin.create_market_sell_order(symbol, trade_size)
            # elif best_scenario == "Buy KuCoin, Sell Binance":
            #     kucoin.create_market_buy_order(symbol, trade_size)
            #     binance.create_market_sell_order(symbol, trade_size)
            # logger.info("Trades executed.")
            
        else:
            logger.info("No profitable arbitrage opportunities found in this cycle.")
            
    except Exception as e:
        logger.error(f"Error during execution: {e}")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
