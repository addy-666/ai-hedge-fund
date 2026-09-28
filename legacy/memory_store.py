import json
import logging
from datetime import datetime
import MetaTrader5 as mt5
from config import MEMORY_FILE

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("MemoryStore")

def append_trade_memory(record: dict):
    """
    Appends a new trade record to memory.json.
    Expected record shape:
    {
        "ticket": int,
        "symbol": str,
        "type": "BUY" | "SELL",
        "entry_price": float,
        "volume": float,
        "sl": float,
        "tp": float,
        "status": "CONFIRMED",
        "entry_time": str(datetime.now()),
        "market_context": dict,
        "exit_deal": None,
        "realized_pl": 0.0,
        "outcome": None
    }
    """
    memory = load_trade_memory()
    memory.append(record)
    
    try:
        with open(MEMORY_FILE, "w") as f:
            json.dump(memory, f, indent=4)
        logger.info(f"[SYSTEM] Appended trade memory for ticket {record.get('ticket')}")
    except Exception as e:
        logger.error(f"[SYSTEM] Failed to write memory: {e}")

def load_trade_memory() -> list:
    """Loads all trade memory from memory.json."""
    if not MEMORY_FILE.exists():
        return []
    try:
        with open(MEMORY_FILE, "r") as f:
            return json.load(f)
    except json.JSONDecodeError:
        logger.warning("[SYSTEM] Corrupted memory.json, returning empty list.")
        return []

def reconcile_closed_trades() -> int:
    """
    Scans CONFIRMED records without an exit_deal.
    Queries MT5 historical deals for the ticket.
    If closed, updates status to CLOSED, calculates P/L, and persists.
    Returns the count of newly reconciled trades.
    """
    memory = load_trade_memory()
    reconciled_count = 0
    updated = False
    
    # Needs a 30-day window to catch recently closed trades
    date_from = datetime.now().timestamp() - (30 * 24 * 60 * 60)
    date_to = datetime.now().timestamp() + (24 * 60 * 60)
    
    deals = mt5.history_deals_get(date_from, date_to)
    if deals is None:
        return 0
        
    deals_by_position = {}
    for deal in deals:
        if deal.position_id not in deals_by_position:
            deals_by_position[deal.position_id] = []
        deals_by_position[deal.position_id].append(deal)
        
    for record in memory:
        if record.get("status") == "CONFIRMED" and not record.get("exit_deal"):
            pos_id = record.get("ticket")
            
            # Find exit deal (deal.entry == 1 is DEAL_ENTRY_OUT)
            pos_deals = deals_by_position.get(pos_id, [])
            exit_deals = [d for d in pos_deals if d.entry == 1]
            
            if exit_deals:
                # Trade is closed
                exit_deal = exit_deals[-1]
                record["status"] = "CLOSED"
                record["exit_deal"] = exit_deal.ticket
                record["exit_price"] = exit_deal.price
                record["exit_time"] = datetime.fromtimestamp(exit_deal.time).isoformat()
                record["realized_pl"] = exit_deal.profit
                
                if exit_deal.profit > 0:
                    record["outcome"] = "WIN"
                elif exit_deal.profit < 0:
                    record["outcome"] = "LOSS"
                else:
                    record["outcome"] = "BREAKEVEN"
                    
                logger.info(f"[TRADE] Reconciled CLOSED trade {pos_id}: {record['outcome']} ({record['realized_pl']})")
                reconciled_count += 1
                updated = True
                
    if updated:
        with open(MEMORY_FILE, "w") as f:
            json.dump(memory, f, indent=4)
            
    return reconciled_count
