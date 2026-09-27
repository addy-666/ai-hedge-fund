import os
from pathlib import Path
from dotenv import load_dotenv
import MetaTrader5 as mt5

# Load environment variables from .env file
load_dotenv()

# Base Directories
BASE_DIR = Path(__file__).resolve().parent
MEMORY_FILE = BASE_DIR / "memory.json"
RULES_FILE = BASE_DIR / "new_rules.json"

# --- DEEPSEEK CONFIGURATION ---
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_BASE = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")

# --- MT5 CONFIGURATION ---
MT5_LOGIN = int(os.getenv("MT5_LOGIN", 0)) if os.getenv("MT5_LOGIN") else 0
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")
MT5_SERVER = os.getenv("MT5_SERVER", "")
MT5_PATH = os.getenv("MT5_PATH", None)
if MT5_PATH == "":
    MT5_PATH = None

# --- RISK & ENGINE SETTINGS ---
DEFAULT_RISK_PERCENT = float(os.getenv("DEFAULT_RISK_PERCENT", 1.0))
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", 30))
CONFIDENCE_THRESHOLD = int(os.getenv("CONFIDENCE_THRESHOLD", 65))

# Parse comma-separated symbols
symbols_env = os.getenv("SYMBOLS", "XAUUSD")
SYMBOLS = [s.strip() for s in symbols_env.split(",") if s.strip()]

# --- OBSIDIAN VAULT PATH ---
VAULT_PATH = os.getenv("VAULT_PATH", str(BASE_DIR.parent / "TRADING BRAIN" / "wiki"))

# --- TECHNICAL INDICATOR & TIMEFRAME CONSTANTS ---
TIMEFRAMES = [mt5.TIMEFRAME_H1, mt5.TIMEFRAME_D1]
EMA_PERIOD = 200
RSI_PERIOD = 14
ATR_PERIOD = 14
VOL_MA_PERIOD = 20

# --- AUDITOR SETTINGS ---
AUDIT_TRADE_THRESHOLD = int(os.getenv("AUDIT_TRADE_THRESHOLD", 10))
AUDIT_COOLDOWN_HOURS = int(os.getenv("AUDIT_COOLDOWN_HOURS", 12))
