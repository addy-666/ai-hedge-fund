import logging
import math
import MetaTrader5 as mt5

logger = logging.getLogger("Execution")

def calculate_position_size(symbol: str, stop_loss_price: float, risk_percent: float, equity: float) -> float:
    """Calculates lot size based on equity risk and geometric stop distance."""
    info = mt5.symbol_info(symbol)
    if not info:
        raise ValueError(f"Symbol {symbol} not found in MT5.")
        
    risk_money = equity * (risk_percent / 100.0)
    
    current_price = info.ask
    price_distance = abs(current_price - stop_loss_price)
    
    if price_distance <= 0:
        return info.volume_min
        
    # Standard formula: Risk Money / (Stop Distance * Tick Value / Tick Size)
    # Using contract size simplification
    value_per_point = info.trade_tick_value / info.trade_tick_size
    lots = risk_money / (price_distance * value_per_point)
    
    # Clamp and round to step
    lots = math.floor(lots / info.volume_step) * info.volume_step
    lots = max(info.volume_min, min(lots, info.volume_max))
    
    return round(lots, 2)

def get_open_positions(symbol=None):
    """Retrieves all open positions, optionally filtered by symbol."""
    if symbol:
        return mt5.positions_get(symbol=symbol)
    return mt5.positions_get()

def close_position(ticket_or_pos) -> dict:
    """Closes an active MT5 position via an opposite market order."""
    if isinstance(ticket_or_pos, int):
        pos = mt5.positions_get(ticket=ticket_or_pos)
        if not pos:
            return {"status": "error", "message": "Position not found"}
        pos = pos[0]
    else:
        pos = ticket_or_pos
        
    symbol = pos.symbol
    lot = pos.volume
    order_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price = mt5.symbol_info_tick(symbol).bid if order_type == mt5.ORDER_TYPE_SELL else mt5.symbol_info_tick(symbol).ask
    
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot,
        "type": order_type,
        "position": pos.ticket,
        "price": price,
        "deviation": 20,
        "magic": 999999,
        "comment": "AI Auto-Close",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    
    res = mt5.order_send(request)
    if res.retcode != mt5.TRADE_RETCODE_DONE:
        logger.error(f"[EXECUTION] Failed to close position {pos.ticket}. Error: {res.retcode}")
        return {"status": "error", "message": f"MT5 Retcode: {res.retcode}"}
        
    logger.info(f"[EXECUTION] Successfully closed position {pos.ticket}")
    return {"status": "success", "ticket": pos.ticket, "deal": res.deal}

def execute_trade(symbol: str, signal: str, stop_loss: float, take_profit: float, risk_percent: float) -> dict:
    """Places a new market order into MT5."""
    account = mt5.account_info()
    if not account:
        raise ConnectionError("Lost connection to MT5 server.")
        
    volume = calculate_position_size(symbol, stop_loss, risk_percent, account.equity)
    order_type = mt5.ORDER_TYPE_BUY if signal == "BUY" else mt5.ORDER_TYPE_SELL
    tick = mt5.symbol_info_tick(symbol)
    price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
    
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "price": price,
        "sl": stop_loss,
        "tp": take_profit,
        "deviation": 20,
        "magic": 999999,
        "comment": "AI Brain Entry",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    
    logger.info(f"[EXECUTION] Sending {signal} request for {symbol} ({volume} lots)")
    res = mt5.order_send(request)
    
    if res.retcode != mt5.TRADE_RETCODE_DONE:
        raise Exception(f"MT5 Order Error: {res.retcode} - {res.comment}")
        
    logger.info(f"[TRADE] Executed {signal} on {symbol}. Ticket: {res.order}, Deal: {res.deal}")
    
    return {
        "ticket": res.order,
        "deal": res.deal,
        "price": res.price,
        "volume": res.volume,
        "sl": stop_loss,
        "tp": take_profit
    }
