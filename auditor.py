import json
import logging
import requests
from datetime import datetime, timedelta
import config
from memory_store import load_trade_memory, reconcile_closed_trades

logger = logging.getLogger("Auditor")

def load_rules_document() -> dict:
    if not config.RULES_FILE.exists():
        return {"rules": [], "last_audit_at": None, "trades_analyzed": 0}
    try:
        with open(config.RULES_FILE, "r") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {"rules": [], "last_audit_at": None, "trades_analyzed": 0}

def save_rules_document(doc: dict):
    with open(config.RULES_FILE, "w") as f:
        json.dump(doc, f, indent=4)

def run_audit(force=False) -> dict:
    """
    Self-Learning Core: Analyzes recent losses using DeepSeek and creates penalty rules.
    """
    reconciled = reconcile_closed_trades()
    logger.info(f"[AUDITOR] Reconciled {reconciled} trades before audit.")
    
    doc = load_rules_document()
    
    # Cooldown Check
    if not force and doc.get("last_audit_at"):
        last_audit = datetime.fromisoformat(doc["last_audit_at"])
        if datetime.now() - last_audit < timedelta(hours=config.AUDIT_COOLDOWN_HOURS):
            logger.info("[AUDITOR] Audit cooldown active. Skipping.")
            return {"status": "cooldown_active"}
            
    memory = load_trade_memory()
    closed_trades = [t for t in memory if t.get("status") == "CLOSED" and t.get("realized_pl") is not None]
    
    # Sort by exit time descending, take last 50
    closed_trades.sort(key=lambda x: x.get("exit_time", ""), reverse=True)
    recent_trades = closed_trades[:50]
    
    if len(recent_trades) < 3:
        logger.info("[AUDITOR] Insufficient closed trades to run audit.")
        return {"status": "insufficient_trades"}
        
    loss_trades = [t for t in recent_trades if t.get("outcome") == "LOSS"]
    if not loss_trades:
        logger.info("[AUDITOR] No recent losses found. Excellent performance.")
        return {"status": "no_losses"}
        
    logger.info(f"[AUDITOR] Running deep audit on {len(loss_trades)} losing trades...")
    
    # Prepare summary for DeepSeek
    loss_summaries = []
    for t in loss_trades:
        ctx = t.get("market_context", {})
        loss_summaries.append({
            "symbol": t.get("symbol"),
            "type": t.get("type"),
            "realized_pl": t.get("realized_pl"),
            "h1_atr": ctx.get("h1_data", {}).get("ATR_14"),
            "h1_rel_vol": ctx.get("h1_data", {}).get("REL_VOL"),
            "d1_atr": ctx.get("daily_data", {}).get("ATR_14"),
            "correlated_assets": ctx.get("correlated_prices", {})
        })
        
    prompt = f"""
    You are the Chief Risk Officer and Quantitative Auditor for an AI Hedge Fund.
    Analyze the following recent loss trades. Identify any recurring mathematical or structural patterns that led to these losses 
    (e.g., low-volume breakdowns, overextended ATR traps, adverse correlations).
    
    LOSS TRADES:
    {json.dumps(loss_summaries, indent=2)}
    
    Based on your analysis, define strict, machine-readable rules to penalize future AI confidence scores when these conditions reappear.
    
    CRITICAL: YOU MUST RESPOND IN PURE JSON FORMAT EXACTLY LIKE THIS:
    {{
      "new_rules": [
        {{
          "affected_symbol": "EURUSDm",
          "setup": "Description of the toxic setup to avoid",
          "confidence_reduction_points": 25,
          "sample_size": 3,
          "evidence": "Observed losses during low relative volume"
        }}
      ]
    }}
    Do not output markdown code blocks. Output ONLY valid JSON.
    """
    
    headers = {
        "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": config.DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": "You output strict, raw JSON without markdown formatting."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1
    }
    
    try:
        response = requests.post(f"{config.DEEPSEEK_API_BASE}/chat/completions", headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
        raw_json = data["choices"][0]["message"]["content"]
        
        # Clean potential markdown wrapping
        raw_json = raw_json.replace("```json", "").replace("```", "").strip()
        new_rules_data = json.loads(raw_json)
        
        # Merge into document
        for rule in new_rules_data.get("new_rules", []):
            rule["discovered_at"] = datetime.now().isoformat()
            doc["rules"].append(rule)
            logger.info(f"[LEARNING] Discovered new rule for {rule.get('affected_symbol')}: {rule.get('setup')}")
            
        doc["last_audit_at"] = datetime.now().isoformat()
        doc["trades_analyzed"] = len(recent_trades)
        save_rules_document(doc)
        
        return {"status": "success", "new_rules_count": len(new_rules_data.get("new_rules", []))}
        
    except Exception as e:
        logger.error(f"[AUDITOR] DeepSeek API Error during audit: {e}")
        return {"status": "error", "message": str(e)}
