import asyncio
import logging
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import config
from data_engine import initialize_mt5, fetch_multi_timeframe_data, fetch_correlated_asset_prices
from ai_brain import get_ai_decision, load_learned_rules
from execution import get_open_positions, close_position, execute_trade
from memory_store import append_trade_memory, reconcile_closed_trades, load_trade_memory
from auditor import run_audit

# Setup Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Main")

app = FastAPI(title="AI Hedge Fund Terminal")
templates = Jinja2Templates(directory="templates")

# Global Bot State
class BotState:
    def __init__(self):
        self.is_running = False
        self.interval = config.SCAN_INTERVAL_SECONDS
        self.risk_percent = config.DEFAULT_RISK_PERCENT
        self.equity = 0.0
        self.last_logic = ""
        self.last_confidence = 0
        self.last_signal = "HOLD"
        self.last_entry_price = None
        self.last_stop_loss = None
        self.last_take_profit = None
        self.trade_history = []
        self.open_positions = []
        self.learned_rules = []
        self.last_audit_at = None
        self.confirmed_fills = 0

bot_state = BotState()

# Request Models
class ControlRequest(BaseModel):
    action: str
    interval: int = None
    risk_percent: float = None

class CloseRequest(BaseModel):
    ticket: int

class AuditRequest(BaseModel):
    force: bool = False

async def trading_loop():
    """Infinite autonomous trading loop."""
    logger.info("[SYSTEM] AI Trading Loop Thread Started.")
    while True:
        if bot_state.is_running:
            try:
                for symbol in config.SYMBOLS:
                    if not bot_state.is_running:
                        break
                        
                    logger.info(f"[SYSTEM] Scanning {symbol}...")
                    
                    # 1. Fetch Market Data
                    market_data = fetch_multi_timeframe_data(symbol)
                    if not market_data:
                        continue
                    
                    correlated_prices = fetch_correlated_asset_prices(symbol, config.SYMBOLS)
                    market_data["correlated_prices"] = correlated_prices
                    
                    bot_state.equity = market_data["equity"]
                    
                    # 2. Get AI Decision
                    decision = get_ai_decision(market_data, symbol)
                    signal = decision.get("signal", "HOLD")
                    confidence = decision.get("confidence_score", 0)
                    logic = decision.get("logic", "")
                    
                    # Update state for dashboard
                    bot_state.last_signal = signal
                    bot_state.last_confidence = confidence
                    bot_state.last_logic = logic
                    
                    logger.info(f"[AI] {symbol} Signal: {signal} (Conf: {confidence}) - {logic}")
                    
                    if signal in ["BUY", "SELL"]:
                        # 3. Check Open Positions
                        open_pos = get_open_positions(symbol)
                        has_same = False
                        
                        if open_pos:
                            for p in open_pos:
                                p_type = "BUY" if p.type == 0 else "SELL"
                                if p_type == signal:
                                    logger.info(f"[DUPLICATE PREVENTED] Already long/short {symbol}. Ignoring.")
                                    has_same = True
                                else:
                                    logger.info(f"[REVERSAL] Closing opposite position {p.ticket} before entry.")
                                    close_position(p.ticket)
                        
                        if not has_same:
                            # 4. Execute Trade
                            sl = decision.get("stop_loss")
                            tp = decision.get("take_profit")
                            
                            try:
                                res = execute_trade(symbol, signal, sl, tp, bot_state.risk_percent)
                                bot_state.last_entry_price = res["price"]
                                bot_state.last_stop_loss = res["sl"]
                                bot_state.last_take_profit = res["tp"]
                                bot_state.confirmed_fills += 1
                                
                                # 5. Record Memory
                                mem_record = {
                                    "ticket": res["ticket"],
                                    "symbol": symbol,
                                    "type": signal,
                                    "entry_price": res["price"],
                                    "volume": res["volume"],
                                    "sl": sl,
                                    "tp": tp,
                                    "status": "CONFIRMED",
                                    "entry_time": datetime.now().isoformat(),
                                    "market_context": market_data,
                                    "exit_deal": None,
                                    "realized_pl": None,
                                    "outcome": None
                                }
                                append_trade_memory(mem_record)
                            except Exception as e:
                                logger.error(f"[EXECUTION] Failed to execute {signal} on {symbol}: {e}")
                                
                # After sweeping all symbols, reconcile and audit
                reconciled = reconcile_closed_trades()
                if reconciled > 0:
                    logger.info(f"[SYSTEM] Auto-triggering audit due to {reconciled} newly closed trades.")
                    audit_res = run_audit(force=False)
                    if audit_res.get("status") == "success":
                        bot_state.learned_rules = load_learned_rules("ALL")
                        
            except Exception as e:
                logger.error(f"[SYSTEM] Loop Error: {e}")
                
        await asyncio.sleep(bot_state.interval)

@app.on_event("startup")
async def startup_event():
    """Startup initialization."""
    logger.info("[SYSTEM] Initializing Terminal...")
    if initialize_mt5():
        reconciled = reconcile_closed_trades()
        logger.info(f"[SYSTEM] Initial reconciliation found {reconciled} closed trades.")
        bot_state.learned_rules = load_learned_rules("ALL")
        # Start background loop
        asyncio.create_task(trading_loop())
    else:
        logger.error("[SYSTEM] MT5 init failed. Cannot start.")

# --- API ENDPOINTS ---

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api/status")
async def get_status():
    open_pos = []
    positions = get_open_positions()
    if positions:
        for p in positions:
            open_pos.append({
                "ticket": p.ticket,
                "symbol": p.symbol,
                "type": "BUY" if p.type == 0 else "SELL",
                "volume": p.volume,
                "entry_price": p.price_open,
                "sl": p.sl,
                "tp": p.tp,
                "profit": p.profit
            })
    
    bot_state.open_positions = open_pos
    return {
        "is_running": bot_state.is_running,
        "equity": bot_state.equity,
        "interval": bot_state.interval,
        "risk_percent": bot_state.risk_percent,
        "last_signal": bot_state.last_signal,
        "last_confidence": bot_state.last_confidence,
        "last_logic": bot_state.last_logic,
        "last_entry_price": bot_state.last_entry_price,
        "last_stop_loss": bot_state.last_stop_loss,
        "last_take_profit": bot_state.last_take_profit,
        "confirmed_fills": bot_state.confirmed_fills,
        "open_positions": bot_state.open_positions,
        "learned_rules": bot_state.learned_rules
    }

@app.post("/api/control")
async def control_engine(req: ControlRequest):
    if req.action == "start":
        bot_state.is_running = True
    elif req.action == "stop":
        bot_state.is_running = False
        
    if req.interval is not None:
        bot_state.interval = req.interval
    if req.risk_percent is not None:
        bot_state.risk_percent = req.risk_percent
        
    logger.info(f"[SYSTEM] Engine {'STARTED' if bot_state.is_running else 'STOPPED'} | Interval: {bot_state.interval}s | Risk: {bot_state.risk_percent}%")
    return {"status": "success", "is_running": bot_state.is_running}

@app.post("/api/close")
async def close_trade(req: CloseRequest):
    res = close_position(req.ticket)
    return res

@app.post("/api/audit")
async def manual_audit(req: AuditRequest):
    res = run_audit(force=req.force)
    bot_state.learned_rules = load_learned_rules("ALL")
    return res

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
