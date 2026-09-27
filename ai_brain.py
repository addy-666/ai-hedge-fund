import json
import logging
import requests
import config
from auditor import load_rules_document

logger = logging.getLogger("AIBrain")

def load_learned_rules(symbol: str) -> list:
    """Returns active rules targeting the given symbol or ALL."""
    doc = load_rules_document()
    active_rules = []
    for rule in doc.get("rules", []):
        aff_sym = rule.get("affected_symbol", "ALL")
        if aff_sym == "ALL" or aff_sym == symbol:
            active_rules.append(rule)
    return active_rules

def get_ai_decision(market_data: dict, symbol: str) -> dict:
    """
    Calls DeepSeek API to get a trading decision (BUY, SELL, HOLD).
    Forces HOLD if confidence < CONFIDENCE_THRESHOLD.
    """
    active_rules = load_learned_rules(symbol)
    
    rules_context = ""
    if active_rules:
        rules_context = "\nACTIVE RISK AUDIT RULES (MANDATORY ENFORCEMENT):\n"
        for i, r in enumerate(active_rules):
            rules_context += f"Rule {i+1}: IF {r.get('setup')} -> REDUCE CONFIDENCE BY {r.get('confidence_reduction_points')} POINTS.\n"

    prompt = f"""
    You are an elite, risk-first Quantitative Trading AI for a Hedge Fund.
    Evaluate the following multi-timeframe market data for {symbol}.
    
    MARKET DATA:
    {json.dumps(market_data, indent=2)}
    {rules_context}
    
    INSTRUCTIONS:
    1. Determine the optimal signal: "BUY", "SELL", or "HOLD".
    2. Assign a confidence_score (0-100). If it is below {config.CONFIDENCE_THRESHOLD}, the system will force a HOLD.
    3. Provide strict geometric Stop Loss (SL) and Take Profit (TP). 
       - For BUY: SL < Ask Price < TP
       - For SELL: TP < Bid Price < SL
    4. Provide a concise logic explanation.
    
    CRITICAL: RESPOND ONLY IN VALID JSON FORMAT. NO MARKDOWN, NO EXPLANATION TEXT outside the JSON block.
    {{
        "signal": "BUY",
        "confidence_score": 85,
        "stop_loss": 1.0900,
        "take_profit": 1.1000,
        "logic": "H1 EMA200 support confirmed with bullish RSI divergence."
    }}
    """
    
    headers = {
        "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": config.DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": "You are a rigid quantitative AI. You output ONLY valid JSON without any markdown wrapping."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1
    }
    
    try:
        response = requests.post(f"{config.DEEPSEEK_API_BASE}/chat/completions", headers=headers, json=payload)
        response.raise_for_status()
        raw_json = response.json()["choices"][0]["message"]["content"]
        
        # Clean markdown if present
        raw_json = raw_json.replace("```json", "").replace("```", "").strip()
        decision = json.loads(raw_json)
        
        # Validate confidence threshold
        conf = decision.get("confidence_score", 0)
        if conf < config.CONFIDENCE_THRESHOLD and decision.get("signal") in ["BUY", "SELL"]:
            logger.info(f"[AI] {symbol} Signal downgraded to HOLD (Confidence {conf} < {config.CONFIDENCE_THRESHOLD})")
            decision["signal"] = "HOLD"
            
        # Validate geometric stops
        signal = decision.get("signal")
        sl = float(decision.get("stop_loss", 0.0))
        tp = float(decision.get("take_profit", 0.0))
        ask = market_data.get("ask", 0.0)
        bid = market_data.get("bid", 0.0)
        
        atr = market_data.get("h1_data", {}).get("ATR_14", 0.0)
        if atr == 0.0:
            atr = ask * 0.005 # Fallback to 0.5%
            
        is_invalid_buy = signal == "BUY" and not (sl < ask and tp > ask)
        is_invalid_sell = signal == "SELL" and not (sl > bid and tp < bid)
        
        if is_invalid_buy:
            logger.warning(f"[AI] Invalid BUY geometric stops for {symbol}. Recalculating using ATR...")
            decision["stop_loss"] = ask - (1.5 * atr)
            decision["take_profit"] = ask + (3.0 * atr)
            
        if is_invalid_sell:
            logger.warning(f"[AI] Invalid SELL geometric stops for {symbol}. Recalculating using ATR...")
            decision["stop_loss"] = bid + (1.5 * atr)
            decision["take_profit"] = bid - (3.0 * atr)
            
        return decision
        
    except Exception as e:
        logger.error(f"[AI] DeepSeek API Error for {symbol}: {e}")
        return {"signal": "HOLD", "confidence_score": 0, "logic": f"Error: {str(e)}"}
