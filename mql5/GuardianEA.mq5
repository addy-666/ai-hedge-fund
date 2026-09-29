//+------------------------------------------------------------------+
//| GuardianEA.mq5 — independent safety net for the aifund engine     |
//| docs/06 §4, roadmap 5.7a. Runs inside the terminal, independent   |
//| of Python. Attach to ONE chart (any symbol), Algo Trading on.     |
//|                                                                    |
//| Every second:                                                     |
//|  1. HARD daily loss limit (wider than the engine's soft limit):   |
//|     equity <= day-start equity x (1 - limit) -> close every       |
//|     position with the engine magic, write halt.flag, notify.      |
//|  2. HARD max drawdown from the stored equity peak -> same.        |
//|  3. Engine heartbeat watch: heartbeat.txt older than N minutes -> |
//|     attach a k x ATR stop to every engine position without one    |
//|     (and, if configured, close positions held too long).          |
//| Every hour:                                                       |
//|  4. Economic calendar export for the next 7 days -> calendar.csv  |
//|     (UTC times) for the engine's news gate.                       |
//|                                                                    |
//| Files live in <Common>\Files\aifund\ (the engine finds it through |
//| terminal_info().commondata_path). The halt flag stays until the   |
//| operator deletes it; the engine refuses REARM while it exists.    |
//+------------------------------------------------------------------+
#property copyright "aifund"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>

input long   InpMagic                 = 26092801; // engine magic (engine.magic in trading.yaml)
input double InpHardDailyLossPct      = 4.0;      // % of day-start equity (engine soft limit + 1 point)
input double InpHardMaxDrawdownPct    = 12.0;     // % from the equity peak
input int    InpHeartbeatStaleMinutes = 10;       // engine heartbeat older than this -> protect positions
input double InpAtrK                  = 1.5;      // stop distance = k x ATR(14) on M15 when one is missing
input int    InpCloseAfterMinutes     = 0;        // 0 = off; else close engine positions held longer while stale
input string InpCalendarCurrencies    = "USD";    // comma-separated, e.g. "USD,EUR,JPY"
input bool   InpCalendarModerate      = false;    // also export MODERATE impact events

const string DIR       = "aifund";
const string HALT_FLAG = "aifund\\halt.flag";
const string HEARTBEAT = "aifund\\heartbeat.txt";
const string CALENDAR  = "aifund\\calendar.csv";
const string GV_DAY    = "aifund_guardian_day";
const string GV_START  = "aifund_guardian_day_start_equity";
const string GV_PEAK   = "aifund_guardian_peak_equity";

CTrade   g_trade;
datetime g_last_calendar = 0;
bool     g_stale_notified = false;

//+------------------------------------------------------------------+
int OnInit()
  {
   g_trade.SetExpertMagicNumber(InpMagic);
   g_trade.SetDeviationInPoints(50);
   FolderCreate(DIR, FILE_COMMON);
   RollDay();
   ExportCalendar();
   EventSetTimer(1);
   PrintFormat("Guardian EA started: magic %I64d, hard daily %.2f%%, hard drawdown %.2f%%, heartbeat %d min",
               InpMagic, InpHardDailyLossPct, InpHardMaxDrawdownPct, InpHeartbeatStaleMinutes);
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
  }

//+------------------------------------------------------------------+
void OnTimer()
  {
   RollDay();
   CheckHardLimits();
   if(FileIsExist(HALT_FLAG, FILE_COMMON))
      CloseEnginePositions("halt flag set");   // keep the book flat while halted
   WatchHeartbeat();
   if(TimeGMT() - g_last_calendar >= 3600)
      ExportCalendar();
  }

//+------------------------------------------------------------------+
//| Trading day and peak, persisted in terminal global variables     |
//+------------------------------------------------------------------+
int ServerDayKey()
  {
   MqlDateTime t;
   TimeToStruct(TimeTradeServer(), t);          // the broker's day (server midnight = rollover)
   return(t.year * 10000 + t.mon * 100 + t.day);
  }

void RollDay()
  {
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   int day = ServerDayKey();
   if(!GlobalVariableCheck(GV_DAY) || (int)GlobalVariableGet(GV_DAY) != day)
     {
      GlobalVariableSet(GV_DAY, day);
      GlobalVariableSet(GV_START, equity);
     }
   if(!GlobalVariableCheck(GV_PEAK) || equity > GlobalVariableGet(GV_PEAK))
      GlobalVariableSet(GV_PEAK, equity);
  }

//+------------------------------------------------------------------+
void CheckHardLimits()
  {
   if(FileIsExist(HALT_FLAG, FILE_COMMON))
      return;                                    // already halted: nothing new to decide
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double start  = GlobalVariableGet(GV_START);
   double peak   = GlobalVariableGet(GV_PEAK);
   string reason = "";
   if(start > 0 && equity <= start * (1.0 - InpHardDailyLossPct / 100.0))
      reason = StringFormat("HARD_DAILY_LOSS: equity %.2f <= %.2f (day start %.2f, limit %.2f%%)",
                            equity, start * (1.0 - InpHardDailyLossPct / 100.0), start, InpHardDailyLossPct);
   else if(peak > 0 && equity <= peak * (1.0 - InpHardMaxDrawdownPct / 100.0))
      reason = StringFormat("HARD_MAX_DRAWDOWN: equity %.2f <= %.2f (peak %.2f, limit %.2f%%)",
                            equity, peak * (1.0 - InpHardMaxDrawdownPct / 100.0), peak, InpHardMaxDrawdownPct);
   if(reason == "")
      return;
   CloseEnginePositions(reason);
   WriteHaltFlag(reason);
   SendNotification("aifund Guardian HALT: " + reason);
   Print("Guardian HALT: ", reason);
  }

void WriteHaltFlag(const string reason)
  {
   int h = FileOpen(HALT_FLAG, FILE_WRITE | FILE_TXT | FILE_COMMON | FILE_ANSI);
   if(h == INVALID_HANDLE)
     {
      PrintFormat("cannot write the halt flag (error %d)", GetLastError());
      return;
     }
   FileWriteString(h, reason + " at " + TimeToString(TimeGMT(), TIME_DATE | TIME_SECONDS) + " UTC\n");
   FileClose(h);
  }

void CloseEnginePositions(const string why)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || PositionGetInteger(POSITION_MAGIC) != InpMagic)
         continue;
      if(!g_trade.PositionClose(ticket))
         PrintFormat("close %I64u failed (%s): retcode %d", ticket, why, g_trade.ResultRetcode());
     }
  }

//+------------------------------------------------------------------+
//| Engine heartbeat: "<unix seconds> <ISO UTC> <state>"             |
//+------------------------------------------------------------------+
long HeartbeatAge()
  {
   if(!FileIsExist(HEARTBEAT, FILE_COMMON))
      return(-1);
   int h = FileOpen(HEARTBEAT, FILE_READ | FILE_TXT | FILE_COMMON | FILE_ANSI | FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(h == INVALID_HANDLE)
      return(-1);
   string line = FileReadString(h);
   FileClose(h);
   string parts[];
   if(StringSplit(line, ' ', parts) < 1)
      return(-1);
   long beat = StringToInteger(parts[0]);
   return(beat > 0 ? (long)TimeGMT() - beat : -1);
  }

void WatchHeartbeat()
  {
   long age = HeartbeatAge();
   bool stale = (age < 0 || age > InpHeartbeatStaleMinutes * 60);
   if(!stale)
     {
      g_stale_notified = false;
      return;
     }
   if(!g_stale_notified)
     {
      SendNotification(StringFormat("aifund Guardian: engine heartbeat stale (%I64d s) - protecting positions", age));
      g_stale_notified = true;
     }
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || PositionGetInteger(POSITION_MAGIC) != InpMagic)
         continue;
      string symbol = PositionGetString(POSITION_SYMBOL);
      datetime opened = (datetime)PositionGetInteger(POSITION_TIME);
      if(InpCloseAfterMinutes > 0 && TimeCurrent() - opened > InpCloseAfterMinutes * 60)
        {
         g_trade.PositionClose(ticket);
         continue;
        }
      if(PositionGetDouble(POSITION_SL) > 0)
         continue;
      AttachStop(ticket, symbol, (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE),
                 PositionGetDouble(POSITION_TP));
     }
  }

void AttachStop(const ulong ticket, const string symbol, const ENUM_POSITION_TYPE type, const double tp)
  {
   int handle = iATR(symbol, PERIOD_M15, 14);
   if(handle == INVALID_HANDLE)
      return;
   double atr[];
   int copied = CopyBuffer(handle, 0, 1, 1, atr);   // the last CLOSED bar
   IndicatorRelease(handle);
   if(copied != 1 || atr[0] <= 0)
      return;
   int    digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   double point  = SymbolInfoDouble(symbol, SYMBOL_POINT);
   double min    = (SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL) + 2) * point;
   double dist   = MathMax(InpAtrK * atr[0], min);
   double sl;
   if(type == POSITION_TYPE_BUY)
      sl = NormalizeDouble(SymbolInfoDouble(symbol, SYMBOL_BID) - dist, digits);
   else
      sl = NormalizeDouble(SymbolInfoDouble(symbol, SYMBOL_ASK) + dist, digits);
   if(!g_trade.PositionModify(ticket, sl, tp))
      PrintFormat("attach SL to %I64u failed: retcode %d", ticket, g_trade.ResultRetcode());
  }

//+------------------------------------------------------------------+
//| Calendar: time_utc,currency,impact,event                         |
//+------------------------------------------------------------------+
void ExportCalendar()
  {
   g_last_calendar = TimeGMT();
   long offset = (long)(TimeTradeServer() - TimeGMT());   // calendar times are server time
   datetime from = TimeTradeServer() - 3600;
   datetime to   = TimeTradeServer() + 7 * 86400;
   string currencies[];
   int n = StringSplit(InpCalendarCurrencies, ',', currencies);
   int h = FileOpen(CALENDAR + ".tmp", FILE_WRITE | FILE_TXT | FILE_COMMON | FILE_ANSI);
   if(h == INVALID_HANDLE)
     {
      PrintFormat("cannot write the calendar (error %d)", GetLastError());
      return;
     }
   FileWriteString(h, "time_utc,currency,impact,event\n");
   for(int c = 0; c < n; c++)
     {
      string currency = currencies[c];
      StringTrimLeft(currency);
      StringTrimRight(currency);
      MqlCalendarValue values[];
      if(CalendarValueHistory(values, from, to, NULL, currency) <= 0)
         continue;
      for(int i = 0; i < ArraySize(values); i++)
        {
         MqlCalendarEvent event;
         if(!CalendarEventById(values[i].event_id, event))
            continue;
         string impact;
         if(event.importance == CALENDAR_IMPORTANCE_HIGH)
            impact = "HIGH";
         else if(event.importance == CALENDAR_IMPORTANCE_MODERATE && InpCalendarModerate)
            impact = "MEDIUM";
         else
            continue;
         string when = TimeToString(values[i].time - offset, TIME_DATE | TIME_MINUTES);
         StringReplace(when, ".", "-");
         string name = event.name;
         StringReplace(name, ",", ";");
         FileWriteString(h, when + "," + currency + "," + impact + "," + name + "\n");
        }
     }
   FileClose(h);
   FileMove(CALENDAR + ".tmp", FILE_COMMON, CALENDAR, FILE_COMMON | FILE_REWRITE);
  }
//+------------------------------------------------------------------+
