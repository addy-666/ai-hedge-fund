import logging
import MetaTrader5 as mt5
import pandas as pd
import pandas_ta as ta
import config

logger = logging.getLogger("DataEngine")

def initialize_mt5() -> bool:
    """Connects to MT5 terminal."""
    if config.MT5_PATH:
        init_res = mt5.initialize(path=config.MT5_PATH)
    else:
        init_res = mt5.initialize()
        
    if not init_res:
        logger.error(f"[SYSTEM] initialize() failed, error code: {mt5.last_error()}")
        return False
        
    if config.MT5_LOGIN:
        auth = mt5.login(config.MT5_LOGIN, password=config.MT5_PASSWORD, server=config.MT5_SERVER)
        if not auth:
            logger.error(f"[SYSTEM] login() failed, error code: {mt5.last_error()}")
            return False
            
    account_info = mt5.account_info()
    if account_info:
        logger.info(f"[SYSTEM] Connected to {config.MT5_SERVER}. Equity: {account_info.equity}, Balance: {account_info.balance}")
    return True

def calculate_indicators(df: pd.DataFrame) -> dict:
    """
    Computes EMA(200), RSI(14), ATR(14), and Relative Volume.
    Returns the latest scalar values and the recent 10-bar context.
    """
    if df is None or len(df) < config.EMA_PERIOD:
        return {}
        
    # Ensure column names match pandas-ta expectations
    df = df.rename(columns={'time': 'datetime'})
    
    # Calculate indicators
    df['EMA_200'] = ta.ema(df['close'], length=config.EMA_PERIOD)
    df['RSI_14'] = ta.rsi(df['close'], length=config.RSI_PERIOD)
    df['ATR_14'] = ta.atr(df['high'], df['low'], df['close'], length=config.ATR_PERIOD)
    
    # Relative Volume (Tick Volume / 20-period MA of Tick Volume)
    df['VOL_MA'] = df['tick_volume'].rolling(window=config.VOL_MA_PERIOD).mean()
    df['REL_VOL'] = df['tick_volume'] / df['VOL_MA']
    
    latest = df.iloc[-1]
    
    return {
        "EMA_200": float(latest['EMA_200']) if pd.notna(latest['EMA_200']) else None,
        "RSI_14": float(latest['RSI_14']) if pd.notna(latest['RSI_14']) else None,
        "ATR_14": float(latest['ATR_14']) if pd.notna(latest['ATR_14']) else None,
        "REL_VOL": float(latest['REL_VOL']) if pd.notna(latest['REL_VOL']) else None,
        "recent_closes": df['close'].tail(10).tolist(),
        "recent_highs": df['high'].tail(10).tolist(),
        "recent_lows": df['low'].tail(10).tolist(),
    }

def fetch_multi_timeframe_data(symbol: str) -> dict:
    """
    Fetches H1 and D1 bars, computes indicators, and returns a structured market snapshot.
    """
    if not mt5.symbol_select(symbol, True):
        logger.error(f"[SYSTEM] Failed to select {symbol}")
        return {}
        
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return {}
        
    # Fetch Bars (250 bars to ensure enough data for 200 EMA)
    h1_rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 250)
    d1_rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, 250)
    
    h1_df = pd.DataFrame(h1_rates)
    d1_df = pd.DataFrame(d1_rates)
    
    h1_indicators = calculate_indicators(h1_df) if not h1_df.empty else {}
    d1_indicators = calculate_indicators(d1_df) if not d1_df.empty else {}
    
    account_info = mt5.account_info()
    
    return {
        "symbol": symbol,
        "bid": float(tick.bid),
        "ask": float(tick.ask),
        "spread": float(tick.ask - tick.bid),
        "equity": float(account_info.equity) if account_info else 0.0,
        "balance": float(account_info.balance) if account_info else 0.0,
        "h1_data": h1_indicators,
        "daily_data": d1_indicators
    }

def fetch_correlated_asset_prices(current_symbol: str, all_symbols: list) -> dict:
    """Fetches the current tick for all other configured symbols to provide correlation context."""
    correlated = {}
    for sym in all_symbols:
        if sym != current_symbol:
            if mt5.symbol_select(sym, True):
                tick = mt5.symbol_info_tick(sym)
                if tick:
                    correlated[sym] = {"bid": float(tick.bid), "ask": float(tick.ask)}
    return correlated
