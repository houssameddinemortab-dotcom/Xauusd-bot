"""
╔══════════════════════════════════════════════════════════════════════╗
║     XAUUSD SIGNAL BOT v4 — EDITION COMPLÈTE                        ║
║                                                                      ║
║  ✅ Multi-Timeframe (H1 confirmation + M15 entrée)                  ║
║  ✅ Filtre Sessions (Londres 08-17h / New York 13-22h UTC)          ║
║  ✅ Stop automatique après 10 pertes consécutives                   ║
║  ✅ Dashboard HTML des signaux                                       ║
║  ✅ Signaux complets sur Telegram (Entrée + TP + SL)                ║
║                                                                      ║
║  DÉPENDANCES :  pip install requests pandas                         ║
║  LANCEMENT   :  python xauusd_bot_v4.py                             ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import requests
import pandas as pd
import numpy as np
import time
import logging
import json
import os
from datetime import datetime, timezone
from typing import Optional

# ══════════════════════════════════════════════════════
#  CONFIGURATION
# ══════════════════════════════════════════════════════

TELEGRAM_TOKEN     = "8646521885:AAEJIUSg1FG65hLr7NecLnp6r5unethPBNE"
TELEGRAM_CHAT_ID   = "6387333974"
TWELVEDATA_API_KEY = "1a6449e9febd41c08736d0340aedc75a"

SYMBOL             = "XAU/USD"
TF_ENTRY           = "15min"     # Timeframe entrée (M15)
TF_CONFIRM         = "1h"        # Timeframe confirmation (H1)
LOOKBACK_CANDLES   = 100
CHECK_INTERVAL     = 300         # 5 minutes

# Filtre de sessions (heures UTC)
SESSION_LONDON_START   = 8
SESSION_LONDON_END     = 17
SESSION_NEWYORK_START  = 13
SESSION_NEWYORK_END    = 22

# SMC
SWING_LOOKBACK  = 10
OB_BODY_RATIO   = 0.6
FVG_MIN_GAP     = 0.5

# Gestion risque
RISK_REWARD      = 2.0
SL_ATR_MULT      = 1.5
TP_ATR_MULT      = 3.0
MAX_LOSSES       = 10           # Stop automatique après N pertes

# Fichiers locaux
STATE_FILE     = "bot_state.json"
DASHBOARD_FILE = "dashboard.html"

# ══════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("xauusd_bot.log", encoding="utf-8"),
    ]
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════
#  1. ÉTAT PERSISTANT (pertes, historique signaux)
# ══════════════════════════════════════════════════════

def load_state() -> dict:
    """Charge l'état du bot depuis le fichier JSON."""
    default = {
        "consecutive_losses": 0,
        "total_signals"     : 0,
        "total_wins"        : 0,
        "total_losses"      : 0,
        "bot_stopped"       : False,
        "last_direction"    : None,
        "signals_history"   : [],
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                default.update(data)
                return default
        except Exception:
            pass
    return default


def save_state(state: dict):
    """Sauvegarde l'état du bot."""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.error(f"Erreur sauvegarde état : {e}")


def record_signal(state: dict, signal: dict):
    """Ajoute un signal à l'historique."""
    state["total_signals"] += 1
    entry = {
        "id"       : state["total_signals"],
        "timestamp": signal["timestamp"],
        "direction": signal["direction"],
        "entry"    : signal["entry"],
        "sl"       : signal["sl"],
        "tp"       : signal["tp"],
        "rr"       : signal["rr"],
        "score"    : signal["score"],
        "session"  : signal.get("session", "—"),
        "tf_confirm": signal.get("tf_confirm", "—"),
        "result"   : "En cours",
    }
    state["signals_history"].insert(0, entry)
    state["signals_history"] = state["signals_history"][:50]  # Garde les 50 derniers
    save_state(state)
    return entry


# ══════════════════════════════════════════════════════
#  2. FILTRE SESSIONS DE TRADING
# ══════════════════════════════════════════════════════

def get_active_session() -> Optional[str]:
    """
    Retourne la session active : 'London', 'New York', 'London+New York' ou None.
    Heures en UTC.
    """
    hour = datetime.now(timezone.utc).hour
    in_london   = SESSION_LONDON_START   <= hour < SESSION_LONDON_END
    in_newyork  = SESSION_NEWYORK_START  <= hour < SESSION_NEWYORK_END

    if in_london and in_newyork:
        return "London + New York"
    elif in_london:
        return "London"
    elif in_newyork:
        return "New York"
    return None


# ══════════════════════════════════════════════════════
#  3. DONNÉES — TWELVE DATA (Multi-Timeframe)
# ══════════════════════════════════════════════════════

def fetch_ohlcv(timeframe: str, n: int = LOOKBACK_CANDLES) -> pd.DataFrame:
    """Télécharge les bougies OHLCV pour un timeframe donné."""
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol"    : SYMBOL,
        "interval"  : timeframe,
        "outputsize": n,
        "apikey"    : TWELVEDATA_API_KEY,
        "format"    : "JSON",
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") == "error":
            log.error(f"[{timeframe}] Twelve Data : {data.get('message')}")
            return pd.DataFrame()

        if "values" not in data:
            log.error(f"[{timeframe}] Réponse inattendue.")
            return pd.DataFrame()

        df = pd.DataFrame(data["values"])
        df = df.rename(columns={
            "datetime": "Date", "open": "Open",
            "high": "High", "low": "Low", "close": "Close",
        })
        df["Date"] = pd.to_datetime(df["Date"], utc=True)
        df = df.set_index("Date")
        for col in ["Open", "High", "Low", "Close"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna().sort_index()

        log.info(f"[{timeframe}] {len(df)} bougies | Close: {df['Close'].iloc[-1]:.2f}$")
        return df

    except requests.RequestException as e:
        log.error(f"[{timeframe}] Erreur réseau : {e}")
        return pd.DataFrame()


# ══════════════════════════════════════════════════════
#  4. INDICATEURS TECHNIQUES
# ══════════════════════════════════════════════════════

def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def compute_ema(df: pd.DataFrame, period: int) -> pd.Series:
    return df["Close"].ewm(span=period, adjust=False).mean()


def detect_swing_points(df: pd.DataFrame, lookback: int = SWING_LOOKBACK):
    highs      = df["High"]
    lows       = df["Low"]
    swing_high = highs == highs.rolling(lookback * 2 + 1, center=True).max()
    swing_low  = lows  == lows.rolling(lookback * 2 + 1, center=True).min()
    return swing_high, swing_low


def detect_bos_choch(df, swing_high, swing_low) -> Optional[str]:
    sh_idx = swing_high[swing_high].index
    sl_idx = swing_low[swing_low].index
    if sh_idx.empty or sl_idx.empty:
        return None

    price   = float(df["Close"].iloc[-1])
    last_sh = df.loc[sh_idx[-1], "High"] if len(sh_idx) >= 1 else None
    prev_sh = df.loc[sh_idx[-2], "High"] if len(sh_idx) >= 2 else None
    last_sl = df.loc[sl_idx[-1], "Low"]  if len(sl_idx) >= 1 else None
    prev_sl = df.loc[sl_idx[-2], "Low"]  if len(sl_idx) >= 2 else None

    if last_sh and price > last_sh:
        return "BOS_BULL" if (prev_sh and last_sh > prev_sh) else "CHOCH_BULL"
    if last_sl and price < last_sl:
        return "BOS_BEAR" if (prev_sl and last_sl < prev_sl) else "CHOCH_BEAR"
    return None


def detect_order_blocks(df):
    bullish_ob = None
    bearish_ob = None
    window = df.tail(20).copy()
    for i in range(1, len(window) - 1):
        c = window.iloc[i]
        n = window.iloc[i + 1]
        body  = abs(float(c["Close"]) - float(c["Open"]))
        wick  = float(c["High"]) - float(c["Low"])
        ratio = body / wick if wick != 0 else 0
        if float(c["Close"]) < float(c["Open"]) and float(n["Close"]) > float(n["Open"]) and ratio >= OB_BODY_RATIO:
            bullish_ob = {"high": float(c["High"]), "low": float(c["Low"])}
        if float(c["Close"]) > float(c["Open"]) and float(n["Close"]) < float(n["Open"]) and ratio >= OB_BODY_RATIO:
            bearish_ob = {"high": float(c["High"]), "low": float(c["Low"])}
    return bullish_ob, bearish_ob


def detect_fvg(df):
    bullish_fvg = None
    bearish_fvg = None
    for i in range(2, len(df)):
        p2   = df.iloc[i - 2]
        curr = df.iloc[i]
        if float(curr["Low"]) - float(p2["High"]) > FVG_MIN_GAP:
            bullish_fvg = {"top": float(curr["Low"]), "bottom": float(p2["High"])}
        if float(p2["Low"]) - float(curr["High"]) > FVG_MIN_GAP:
            bearish_fvg = {"top": float(p2["Low"]), "bottom": float(curr["High"])}
    return bullish_fvg, bearish_fvg


def get_h1_bias(df_h1: pd.DataFrame) -> Optional[str]:
    """
    Biais directionnel sur H1 via EMA 20/50.
    Retourne 'BULL', 'BEAR' ou None.
    """
    if df_h1.empty or len(df_h1) < 50:
        return None
    ema20 = compute_ema(df_h1, 20).iloc[-1]
    ema50 = compute_ema(df_h1, 50).iloc[-1]
    price = float(df_h1["Close"].iloc[-1])

    if price > ema20 > ema50:
        return "BULL"
    if price < ema20 < ema50:
        return "BEAR"
    return None


# ══════════════════════════════════════════════════════
#  5. MOTEUR SMC — MULTI-TIMEFRAME
# ══════════════════════════════════════════════════════

def analyse_market(df_m15: pd.DataFrame, df_h1: pd.DataFrame, session: str) -> Optional[dict]:
    """
    Analyse multi-timeframe :
    - H1 donne le biais directionnel (EMA 20/50)
    - M15 génère le signal (BOS/CHoCH + OB + FVG)
    Confluence minimum : score ≥ 2/3
    """
    if df_m15.empty or len(df_m15) < SWING_LOOKBACK * 2 + 5:
        log.warning("Données M15 insuffisantes.")
        return None

    h1_bias = get_h1_bias(df_h1)
    atr_val = float(compute_atr(df_m15).iloc[-1])
    price   = float(df_m15["Close"].iloc[-1])

    swing_h, swing_l   = detect_swing_points(df_m15)
    bos_choch          = detect_bos_choch(df_m15, swing_h, swing_l)
    bull_ob, bear_ob   = detect_order_blocks(df_m15)
    bull_fvg, bear_fvg = detect_fvg(df_m15)

    # ── Score LONG ────────────────────────────────
    long_score, long_reasons = 0, []

    if h1_bias == "BULL":
        long_score += 1
        long_reasons.append("🕐 Biais H1 HAUSSIER (EMA 20 > EMA 50)")

    if bos_choch in ("BOS_BULL", "CHOCH_BULL"):
        long_score += 1
        long_reasons.append(f"📈 {bos_choch} sur M15")

    if bull_ob and bull_ob["low"] <= price <= bull_ob["high"]:
        long_score += 1
        long_reasons.append(f"🟩 Bullish OB [{bull_ob['low']:.2f} – {bull_ob['high']:.2f}]")

    if bull_fvg and bull_fvg["bottom"] <= price <= bull_fvg["top"]:
        long_score += 1
        long_reasons.append(f"⚡ Bullish FVG [{bull_fvg['bottom']:.2f} – {bull_fvg['top']:.2f}]")

    # ── Score SHORT ───────────────────────────────
    short_score, short_reasons = 0, []

    if h1_bias == "BEAR":
        short_score += 1
        short_reasons.append("🕐 Biais H1 BAISSIER (EMA 20 < EMA 50)")

    if bos_choch in ("BOS_BEAR", "CHOCH_BEAR"):
        short_score += 1
        short_reasons.append(f"📉 {bos_choch} sur M15")

    if bear_ob and bear_ob["low"] <= price <= bear_ob["high"]:
        short_score += 1
        short_reasons.append(f"🟥 Bearish OB [{bear_ob['low']:.2f} – {bear_ob['high']:.2f}]")

    if bear_fvg and bear_fvg["bottom"] <= price <= bear_fvg["top"]:
        short_score += 1
        short_reasons.append(f"⚡ Bearish FVG [{bear_fvg['bottom']:.2f} – {bear_fvg['top']:.2f}]")

    # ── Sélection finale ──────────────────────────
    if long_score >= 2 and long_score >= short_score:
        direction = "LONG"
        sl        = round(price - SL_ATR_MULT * atr_val, 2)
        tp        = round(price + TP_ATR_MULT * atr_val, 2)
        reasons, score = long_reasons, long_score

    elif short_score >= 2:
        direction = "SHORT"
        sl        = round(price + SL_ATR_MULT * atr_val, 2)
        tp        = round(price - TP_ATR_MULT * atr_val, 2)
        reasons, score = short_reasons, short_score

    else:
        log.info(f"Pas de confluence | LONG:{long_score} SHORT:{short_score} | Biais H1:{h1_bias}")
        return None

    rr = abs(tp - price) / abs(sl - price) if abs(sl - price) != 0 else 0
    if rr < RISK_REWARD:
        log.info(f"R:R insuffisant : {rr:.2f} < {RISK_REWARD}")
        return None

    return {
        "direction" : direction,
        "entry"     : round(price, 2),
        "sl"        : sl,
        "tp"        : tp,
        "rr"        : round(rr, 2),
        "atr"       : round(atr_val, 2),
        "score"     : score,
        "max_score" : 4,
        "reasons"   : reasons,
        "tf_confirm": f"H1:{h1_bias or '—'} | M15:Signal",
        "session"   : session,
        "timestamp" : datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


# ══════════════════════════════════════════════════════
#  6. TELEGRAM — MESSAGE COMPLET
# ══════════════════════════════════════════════════════

def build_signal_message(signal: dict, sig_id: int, consecutive_losses: int) -> str:
    emoji = "🟢🚀" if signal["direction"] == "LONG" else "🔴📉"
    arrow = "▲ LONG (ACHAT)" if signal["direction"] == "LONG" else "▼ SHORT (VENTE)"
    reasons_text = "\n".join(f"   • {r}" for r in signal["reasons"])
    sl_pips = abs(signal["entry"] - signal["sl"])
    tp_pips = abs(signal["tp"]   - signal["entry"])

    return (
        f"{emoji} *SIGNAL #{sig_id} — XAUUSD {arrow}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ *Heure*      : `{signal['timestamp']}`\n"
        f"🏙 *Session*    : `{signal['session']}`\n"
        f"📊 *Timeframe*  : `{signal['tf_confirm']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 *ENTRÉE*     : `{signal['entry']} $`\n"
        f"🛡 *STOP LOSS*  : `{signal['sl']} $`  ({sl_pips:.2f}$)\n"
        f"🎯 *TAKE PROFIT*: `{signal['tp']} $`  (+{tp_pips:.2f}$)\n"
        f"📐 *R:R*        : `1 : {signal['rr']}`\n"
        f"📏 *ATR*        : `{signal['atr']} $`\n"
        f"⭐ *Score*      : `{signal['score']}/{signal['max_score']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"*📋 Confluences :*\n{reasons_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ *Pertes consécutives* : `{consecutive_losses}/{MAX_LOSSES}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"_⚠️ Signal indicatif uniquement. Gérez toujours votre risque._"
    )


def send_telegram(message: str) -> bool:
    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("✅ Message Telegram envoyé.")
        return True
    except requests.RequestException as e:
        log.error(f"❌ Erreur Telegram : {e}")
        return False


def send_stop_alert(state: dict):
    """Alerte arrêt automatique après MAX_LOSSES pertes."""
    msg = (
        f"🚨 *BOT ARRÊTÉ AUTOMATIQUEMENT* 🚨\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⛔ *{MAX_LOSSES} pertes consécutives atteintes*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *Statistiques session :*\n"
        f"   • Total signaux : `{state['total_signals']}`\n"
        f"   • Gains         : `{state['total_wins']}`\n"
        f"   • Pertes        : `{state['total_losses']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔄 _Réinitialisez le compteur dans `{STATE_FILE}` pour reprendre._"
    )
    send_telegram(msg)


def send_startup_message(state: dict):
    msg = (
        f"🤖 *XAUUSD Signal Bot v4 démarré* ✅\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 Symbole   : `XAU/USD` (Gold)\n"
        f"⏱ Entrée    : `{TF_ENTRY}` | Confirmation : `{TF_CONFIRM}`\n"
        f"🔄 Scan      : toutes les `{CHECK_INTERVAL}s`\n"
        f"🏙 Sessions  : `Londres 08-17h` & `New York 13-22h UTC`\n"
        f"🛑 Stop auto : après `{MAX_LOSSES}` pertes consécutives\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *Historique :* `{state['total_signals']}` signaux | "
        f"`{state['total_wins']}` W / `{state['total_losses']}` L\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"_En attente de setups de qualité..._"
    )
    send_telegram(msg)


# ══════════════════════════════════════════════════════
#  7. DASHBOARD HTML
# ══════════════════════════════════════════════════════

def generate_dashboard(state: dict):
    """Génère un fichier HTML de suivi des signaux."""
    signals = state.get("signals_history", [])
    total   = state["total_signals"]
    wins    = state["total_wins"]
    losses  = state["total_losses"]
    cons_l  = state["consecutive_losses"]
    winrate = round((wins / total * 100) if total > 0 else 0, 1)

    rows = ""
    for s in signals:
        direction_badge = (
            '<span class="badge long">▲ LONG</span>'  if s["direction"] == "LONG"
            else '<span class="badge short">▼ SHORT</span>'
        )
        result_badge = (
            '<span class="badge win">✅ WIN</span>'    if s["result"] == "Win"
            else '<span class="badge loss">❌ LOSS</span>' if s["result"] == "Loss"
            else '<span class="badge pending">⏳ En cours</span>'
        )
        rows += f"""
        <tr>
            <td>#{s['id']}</td>
            <td>{s['timestamp']}</td>
            <td>{direction_badge}</td>
            <td>{s['entry']} $</td>
            <td class="sl">{s['sl']} $</td>
            <td class="tp">{s['tp']} $</td>
            <td>1:{s['rr']}</td>
            <td>{s['score']}</td>
            <td>{s.get('session','—')}</td>
            <td>{result_badge}</td>
        </tr>"""

    stopped_banner = ""
    if state.get("bot_stopped"):
        stopped_banner = '<div class="stopped-banner">🚨 BOT ARRÊTÉ — 10 pertes consécutives</div>'

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>XAUUSD Signal Bot — Dashboard</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Syne:wght@400;700;800&display=swap');

  :root {{
    --bg: #0a0c0f;
    --surface: #111418;
    --border: #1e2530;
    --gold: #f5c842;
    --gold2: #e8a020;
    --green: #22c55e;
    --red: #ef4444;
    --blue: #3b82f6;
    --muted: #6b7280;
    --text: #e5e7eb;
  }}

  * {{ margin:0; padding:0; box-sizing:border-box; }}

  body {{
    background: var(--bg);
    color: var(--text);
    font-family: 'JetBrains Mono', monospace;
    min-height: 100vh;
  }}

  header {{
    background: linear-gradient(135deg, #0f1117 0%, #1a1506 100%);
    border-bottom: 1px solid var(--gold2);
    padding: 24px 32px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }}

  .logo {{
    font-family: 'Syne', sans-serif;
    font-size: 1.6rem;
    font-weight: 800;
    color: var(--gold);
    letter-spacing: -0.5px;
  }}

  .logo span {{ color: var(--gold2); }}

  .live-dot {{
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 0.75rem;
    color: var(--green);
  }}

  .live-dot::before {{
    content: '';
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--green);
    animation: pulse 1.5s infinite;
  }}

  @keyframes pulse {{
    0%, 100% {{ opacity: 1; transform: scale(1); }}
    50% {{ opacity: 0.5; transform: scale(1.4); }}
  }}

  .stopped-banner {{
    background: linear-gradient(90deg, #7f1d1d, #450a0a);
    border: 1px solid #ef4444;
    color: #fca5a5;
    text-align: center;
    padding: 14px;
    font-weight: 700;
    font-size: 1rem;
    letter-spacing: 0.5px;
  }}

  .stats-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 16px;
    padding: 28px 32px;
  }}

  .stat-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px;
    text-align: center;
    transition: border-color 0.2s;
  }}

  .stat-card:hover {{ border-color: var(--gold2); }}

  .stat-card .label {{
    font-size: 0.7rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 8px;
  }}

  .stat-card .value {{
    font-family: 'Syne', sans-serif;
    font-size: 2rem;
    font-weight: 800;
    color: var(--gold);
  }}

  .stat-card .value.green {{ color: var(--green); }}
  .stat-card .value.red   {{ color: var(--red); }}
  .stat-card .value.blue  {{ color: var(--blue); }}

  .section-title {{
    font-family: 'Syne', sans-serif;
    font-size: 1rem;
    font-weight: 700;
    color: var(--gold);
    padding: 0 32px 16px;
    text-transform: uppercase;
    letter-spacing: 2px;
  }}

  .table-wrapper {{
    padding: 0 32px 40px;
    overflow-x: auto;
  }}

  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 0.82rem;
  }}

  thead th {{
    background: var(--surface);
    color: var(--muted);
    text-transform: uppercase;
    font-size: 0.65rem;
    letter-spacing: 1px;
    padding: 12px 14px;
    text-align: left;
    border-bottom: 2px solid var(--gold2);
  }}

  tbody tr {{
    border-bottom: 1px solid var(--border);
    transition: background 0.15s;
  }}

  tbody tr:hover {{ background: rgba(245,200,66,0.04); }}

  tbody td {{
    padding: 12px 14px;
    color: var(--text);
  }}

  .sl  {{ color: var(--red); }}
  .tp  {{ color: var(--green); }}

  .badge {{
    display: inline-block;
    padding: 3px 10px;
    border-radius: 20px;
    font-size: 0.7rem;
    font-weight: 700;
  }}

  .badge.long    {{ background: rgba(34,197,94,0.15); color: var(--green); border: 1px solid rgba(34,197,94,0.3); }}
  .badge.short   {{ background: rgba(239,68,68,0.15); color: var(--red);   border: 1px solid rgba(239,68,68,0.3); }}
  .badge.win     {{ background: rgba(34,197,94,0.15); color: var(--green); border: 1px solid rgba(34,197,94,0.3); }}
  .badge.loss    {{ background: rgba(239,68,68,0.15); color: var(--red);   border: 1px solid rgba(239,68,68,0.3); }}
  .badge.pending {{ background: rgba(245,200,66,0.12); color: var(--gold); border: 1px solid rgba(245,200,66,0.3); }}

  footer {{
    text-align: center;
    padding: 24px;
    color: var(--muted);
    font-size: 0.7rem;
    border-top: 1px solid var(--border);
  }}
</style>
</head>
<body>

<header>
  <div class="logo">XAU<span>/USD</span> Signal Bot</div>
  <div class="live-dot">LIVE — Mise à jour auto</div>
</header>

{stopped_banner}

<div class="stats-grid">
  <div class="stat-card">
    <div class="label">Total Signaux</div>
    <div class="value blue">{total}</div>
  </div>
  <div class="stat-card">
    <div class="label">Gains</div>
    <div class="value green">{wins}</div>
  </div>
  <div class="stat-card">
    <div class="label">Pertes</div>
    <div class="value red">{losses}</div>
  </div>
  <div class="stat-card">
    <div class="label">Win Rate</div>
    <div class="value {'green' if winrate >= 50 else 'red'}">{winrate}%</div>
  </div>
  <div class="stat-card">
    <div class="label">Pertes Consécutives</div>
    <div class="value {'red' if cons_l >= 7 else 'value'}">{cons_l}/{MAX_LOSSES}</div>
  </div>
</div>

<div class="section-title">📋 Historique des Signaux</div>

<div class="table-wrapper">
  <table>
    <thead>
      <tr>
        <th>#</th>
        <th>Heure</th>
        <th>Direction</th>
        <th>Entrée</th>
        <th>Stop Loss</th>
        <th>Take Profit</th>
        <th>R:R</th>
        <th>Score</th>
        <th>Session</th>
        <th>Résultat</th>
      </tr>
    </thead>
    <tbody>
      {rows if rows else '<tr><td colspan="10" style="text-align:center;color:#6b7280;padding:40px">Aucun signal encore généré</td></tr>'}
    </tbody>
  </table>
</div>

<footer>
  XAUUSD Signal Bot v4 — SMC + Price Action — Multi-Timeframe<br>
  Généré le {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
</footer>

</body>
</html>"""

    try:
        with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
            f.write(html)
        log.info(f"📊 Dashboard mis à jour : {DASHBOARD_FILE}")
    except Exception as e:
        log.error(f"Erreur génération dashboard : {e}")


# ══════════════════════════════════════════════════════
#  8. BOUCLE PRINCIPALE
# ══════════════════════════════════════════════════════

def main():
    log.info("═" * 60)
    log.info("  XAUUSD Signal Bot v4 — Edition Complète")
    log.info("═" * 60)

    state = load_state()

    # Vérification arrêt persistant
    if state.get("bot_stopped"):
        log.warning("⛔ Bot arrêté suite à 10 pertes. Réinitialisez bot_state.json pour reprendre.")
        send_telegram("⛔ *Bot toujours arrêté.* Réinitialisez `bot_state.json` pour reprendre.")
        return

    send_startup_message(state)
    generate_dashboard(state)

    while True:
        try:
            now_str = datetime.now().strftime("%H:%M:%S")
            log.info(f"── Scan {now_str} ──────────────────────────")

            # Vérification stop
            if state["consecutive_losses"] >= MAX_LOSSES:
                state["bot_stopped"] = True
                save_state(state)
                send_stop_alert(state)
                generate_dashboard(state)
                log.error(f"🚨 {MAX_LOSSES} pertes consécutives — Bot arrêté.")
                break

            # Filtre session
            session = get_active_session()
            if not session:
                log.info("⏸ Hors session London/New York — scan ignoré.")
                time.sleep(CHECK_INTERVAL)
                continue

            log.info(f"🏙 Session active : {session}")

            # Données multi-timeframe
            df_m15 = fetch_ohlcv(TF_ENTRY,   LOOKBACK_CANDLES)
            time.sleep(1)  # Pause pour respecter la limite API
            df_h1  = fetch_ohlcv(TF_CONFIRM, 60)

            # Analyse
            signal = analyse_market(df_m15, df_h1, session)

            if signal:
                if signal["direction"] != state["last_direction"]:
                    entry = record_signal(state, signal)
                    sig_id = entry["id"]

                    message = build_signal_message(signal, sig_id, state["consecutive_losses"])
                    if send_telegram(message):
                        state["last_direction"] = signal["direction"]
                        log.info(
                            f"✅ Signal #{sig_id} {signal['direction']} | "
                            f"E:{signal['entry']} SL:{signal['sl']} TP:{signal['tp']} | "
                            f"Session:{session}"
                        )
                    generate_dashboard(state)
                else:
                    log.info(f"Signal {signal['direction']} ignoré (doublon).")
            else:
                log.info("Pas de setup valide cette session.")

        except KeyboardInterrupt:
            log.info("⏹ Bot arrêté manuellement.")
            send_telegram("⏹ *Bot arrêté manuellement.*")
            break
        except Exception as e:
            log.error(f"Erreur inattendue : {e}", exc_info=True)

        log.info(f"Prochain scan dans {CHECK_INTERVAL}s...\n")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
