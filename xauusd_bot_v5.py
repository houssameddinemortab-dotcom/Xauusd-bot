"""
╔══════════════════════════════════════════════════════════════════════╗
║     XAUUSD SIGNAL BOT v5 — EDITION PROFESSIONNELLE                 ║
║                                                                      ║
║  ✅ v4 : Multi-TF H1+M15, Sessions, Stop 10 pertes, Dashboard      ║
║  🆕 v5 : RSI 14 comme filtre de confirmation                        ║
║  🆕 v5 : MMA (SMA) 50 et 100 pour biais directionnel               ║
║  🆕 v5 : Score minimum réduit à 1 (plus de signaux)                ║
║  🆕 v5 : OB Ratio 0.4 / FVG Gap 0.2 (critères assouplis)           ║
║  🆕 v5 : Money Management (% capital par trade)                     ║
║  🆕 v5 : Notification Win/Loss automatique                          ║
║  🆕 v5 : Backtesting sur données historiques                        ║
║                                                                      ║
║  DÉPENDANCES :  pip install requests pandas                         ║
║  LANCEMENT   :  python xauusd_bot_v5.py                             ║
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

TELEGRAM_TOKEN     = "8675193878:AAEKJoJDyDKkVGuSOO7qNAKNHP5ZissLTqE"
TELEGRAM_CHAT_ID   = "6387333974"
TWELVEDATA_API_KEY = "1a6449e9febd41c08736d0340aedc75a"

SYMBOL             = "XAU/USD"
TF_ENTRY           = "15min"
TF_CONFIRM         = "1h"
LOOKBACK_CANDLES   = 100
CHECK_INTERVAL     = 300        # 5 minutes

# Sessions (heures UTC)
SESSION_LONDON_START  = 8
SESSION_LONDON_END    = 17
SESSION_NEWYORK_START = 13
SESSION_NEWYORK_END   = 22

# ── SMC (critères assouplis v5) ───────────────────────
SWING_LOOKBACK  = 10
OB_BODY_RATIO   = 0.4       # v4=0.6 → v5=0.4 (plus permissif)
FVG_MIN_GAP     = 0.2       # v4=0.5 → v5=0.2 (plus permissif)
MIN_SCORE       = 1         # v4=2   → v5=1   (plus de signaux)

# ── Indicateurs v5 ───────────────────────────────────
RSI_PERIOD      = 14
RSI_OVERBOUGHT  = 65        # Seuil RSI surachat (SHORT)
RSI_OVERSOLD    = 35        # Seuil RSI survente (LONG)
SMA_FAST        = 50        # MMA rapide
SMA_SLOW        = 100       # MMA lente

# ── Gestion risque ────────────────────────────────────
CAPITAL         = 1000.0    # Capital total en $
RISK_PERCENT    = 1.0       # % du capital risqué par trade
RISK_REWARD     = 2.0
SL_ATR_MULT     = 1.5
TP_ATR_MULT     = 3.0
MAX_LOSSES      = 10

# Fichiers
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
#  1. ÉTAT PERSISTANT
# ══════════════════════════════════════════════════════

def load_state() -> dict:
    default = {
        "consecutive_losses": 0,
        "total_signals"     : 0,
        "total_wins"        : 0,
        "total_losses"      : 0,
        "total_pnl"         : 0.0,
        "capital"           : CAPITAL,
        "bot_stopped"       : False,
        "last_direction"    : None,
        "last_signal"       : None,
        "signals_history"   : [],
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                default.update(data)
        except Exception:
            pass
    return default


def save_state(state: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.error(f"Erreur sauvegarde état : {e}")


def record_signal(state: dict, signal: dict) -> dict:
    state["total_signals"] += 1
    lot_size = compute_lot_size(state["capital"], signal["entry"], signal["sl"])
    entry = {
        "id"        : state["total_signals"],
        "timestamp" : signal["timestamp"],
        "direction" : signal["direction"],
        "entry"     : signal["entry"],
        "sl"        : signal["sl"],
        "tp"        : signal["tp"],
        "rr"        : signal["rr"],
        "score"     : signal["score"],
        "rsi"       : signal.get("rsi", "—"),
        "sma_bias"  : signal.get("sma_bias", "—"),
        "session"   : signal.get("session", "—"),
        "lot_size"  : lot_size,
        "result"    : "En cours",
        "pnl"       : 0.0,
    }
    state["last_signal"] = entry
    state["signals_history"].insert(0, entry)
    state["signals_history"] = state["signals_history"][:50]
    save_state(state)
    return entry


# ══════════════════════════════════════════════════════
#  2. MONEY MANAGEMENT 🆕
# ══════════════════════════════════════════════════════

def compute_lot_size(capital: float, entry: float, sl: float) -> float:
    """
    Calcule la taille de position selon le % de capital risqué.
    Formule : Lot = (Capital × Risk%) / (|Entry - SL| × 100)
    """
    risk_amount = capital * (RISK_PERCENT / 100)
    sl_distance = abs(entry - sl)
    if sl_distance == 0:
        return 0.01
    lot = risk_amount / (sl_distance * 100)
    return round(max(0.01, lot), 2)


def update_result(state: dict, current_price: float):
    """
    Vérifie si le dernier signal a atteint TP ou SL.
    Met à jour Win/Loss automatiquement.
    """
    if not state.get("last_signal"):
        return
    sig = state["last_signal"]
    if sig["result"] != "En cours":
        return

    direction = sig["direction"]
    tp = sig["tp"]
    sl = sig["sl"]
    entry = sig["entry"]
    lot = sig.get("lot_size", 0.01)

    hit_tp = (direction == "LONG"  and current_price >= tp) or \
             (direction == "SHORT" and current_price <= tp)
    hit_sl = (direction == "LONG"  and current_price <= sl) or \
             (direction == "SHORT" and current_price >= sl)

    if hit_tp:
        pnl = abs(tp - entry) * lot * 100
        sig["result"] = "Win"
        sig["pnl"]    = round(pnl, 2)
        state["total_wins"]        += 1
        state["consecutive_losses"]  = 0
        state["capital"]            += pnl
        state["total_pnl"]          += pnl
        save_state(state)
        send_result_notification(sig, "WIN", pnl, state["capital"])
        log.info(f"✅ WIN #{sig['id']} | +{pnl:.2f}$ | Capital: {state['capital']:.2f}$")

    elif hit_sl:
        pnl = abs(sl - entry) * lot * 100
        sig["result"] = "Loss"
        sig["pnl"]    = round(-pnl, 2)
        state["total_losses"]       += 1
        state["consecutive_losses"] += 1
        state["capital"]            -= pnl
        state["total_pnl"]          -= pnl
        save_state(state)
        send_result_notification(sig, "LOSS", -pnl, state["capital"])
        log.info(f"❌ LOSS #{sig['id']} | -{pnl:.2f}$ | Capital: {state['capital']:.2f}$")


# ══════════════════════════════════════════════════════
#  3. FILTRE SESSIONS
# ══════════════════════════════════════════════════════

def get_active_session() -> Optional[str]:
    hour = datetime.now(timezone.utc).hour
    in_london  = SESSION_LONDON_START  <= hour < SESSION_LONDON_END
    in_newyork = SESSION_NEWYORK_START <= hour < SESSION_NEWYORK_END
    if in_london and in_newyork:
        return "London + New York"
    elif in_london:
        return "London"
    elif in_newyork:
        return "New York"
    return None


# ══════════════════════════════════════════════════════
#  4. DONNÉES — TWELVE DATA
# ══════════════════════════════════════════════════════

def fetch_ohlcv(timeframe: str, n: int = LOOKBACK_CANDLES) -> pd.DataFrame:
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
            log.error(f"[{timeframe}] {data.get('message')}")
            return pd.DataFrame()
        if "values" not in data:
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
#  5. INDICATEURS TECHNIQUES
# ══════════════════════════════════════════════════════

def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def compute_sma(df: pd.DataFrame, period: int) -> pd.Series:
    """MMA (Moyenne Mobile Arithmétique) = SMA."""
    return df["Close"].rolling(window=period).mean()


def compute_rsi(df: pd.DataFrame, period: int = RSI_PERIOD) -> float:
    """RSI — Relative Strength Index."""
    delta  = df["Close"].diff()
    gain   = delta.clip(lower=0).ewm(span=period, adjust=False).mean()
    loss   = (-delta.clip(upper=0)).ewm(span=period, adjust=False).mean()
    rs     = gain / loss.replace(0, np.nan)
    rsi    = 100 - (100 / (1 + rs))
    return round(float(rsi.iloc[-1]), 2)


def get_sma_bias(df: pd.DataFrame) -> Optional[str]:
    """
    Biais directionnel via MMA 50 et MMA 100.
    BULL : Prix > SMA50 > SMA100
    BEAR : Prix < SMA50 < SMA100
    """
    if len(df) < SMA_SLOW:
        return None
    sma50  = float(compute_sma(df, SMA_FAST).iloc[-1])
    sma100 = float(compute_sma(df, SMA_SLOW).iloc[-1])
    price  = float(df["Close"].iloc[-1])

    if price > sma50 > sma100:
        return "BULL"
    if price < sma50 < sma100:
        return "BEAR"
    return "NEUTRE"


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


# ══════════════════════════════════════════════════════
#  6. MOTEUR SMC + RSI + MMA — v5
# ══════════════════════════════════════════════════════

def analyse_market(df_m15: pd.DataFrame, df_h1: pd.DataFrame, session: str) -> Optional[dict]:
    """
    Analyse complète v5 :
    ─ MMA 50/100 sur H1  → biais directionnel principal
    ─ RSI 14 sur M15     → filtre surachat/survente
    ─ BOS/CHoCH sur M15  → structure de marché
    ─ Order Block M15    → zone d'entrée
    ─ FVG M15            → imbalance
    Score minimum : 1/5
    """
    if df_m15.empty or len(df_m15) < max(SWING_LOOKBACK * 2 + 5, SMA_SLOW):
        log.warning("Données insuffisantes.")
        return None

    # ── Calculs indicateurs ────────────────────────────
    atr_val  = float(compute_atr(df_m15).iloc[-1])
    price    = float(df_m15["Close"].iloc[-1])
    rsi_val  = compute_rsi(df_m15)
    sma_bias = get_sma_bias(df_h1) if len(df_h1) >= SMA_SLOW else get_sma_bias(df_m15)

    swing_h, swing_l   = detect_swing_points(df_m15)
    bos_choch          = detect_bos_choch(df_m15, swing_h, swing_l)
    bull_ob, bear_ob   = detect_order_blocks(df_m15)
    bull_fvg, bear_fvg = detect_fvg(df_m15)

    log.info(f"RSI:{rsi_val} | SMA Biais:{sma_bias} | BOS:{bos_choch} | Price:{price:.2f}")

    # ══ Score LONG ════════════════════════════════════
    long_score, long_reasons = 0, []

    # 1. MMA Biais haussier
    if sma_bias == "BULL":
        long_score += 1
        long_reasons.append(f"📊 MMA50 > MMA100 → Biais HAUSSIER")

    # 2. RSI survente (opportunité achat)
    if rsi_val <= RSI_OVERSOLD:
        long_score += 1
        long_reasons.append(f"📉 RSI survente : {rsi_val} ≤ {RSI_OVERSOLD}")
    elif rsi_val < 50:
        long_score += 0  # Neutre, pas de pénalité

    # 3. BOS/CHoCH haussier
    if bos_choch in ("BOS_BULL", "CHOCH_BULL"):
        long_score += 1
        long_reasons.append(f"📈 {bos_choch} sur M15")

    # 4. Order Block haussier
    if bull_ob and bull_ob["low"] <= price <= bull_ob["high"]:
        long_score += 1
        long_reasons.append(f"🟩 Bullish OB [{bull_ob['low']:.2f} – {bull_ob['high']:.2f}]")

    # 5. FVG haussier
    if bull_fvg and bull_fvg["bottom"] <= price <= bull_fvg["top"]:
        long_score += 1
        long_reasons.append(f"⚡ Bullish FVG [{bull_fvg['bottom']:.2f} – {bull_fvg['top']:.2f}]")

    # ══ Score SHORT ═══════════════════════════════════
    short_score, short_reasons = 0, []

    # 1. MMA Biais baissier
    if sma_bias == "BEAR":
        short_score += 1
        short_reasons.append(f"📊 MMA50 < MMA100 → Biais BAISSIER")

    # 2. RSI surachat (opportunité vente)
    if rsi_val >= RSI_OVERBOUGHT:
        short_score += 1
        short_reasons.append(f"📈 RSI surachat : {rsi_val} ≥ {RSI_OVERBOUGHT}")

    # 3. BOS/CHoCH baissier
    if bos_choch in ("BOS_BEAR", "CHOCH_BEAR"):
        short_score += 1
        short_reasons.append(f"📉 {bos_choch} sur M15")

    # 4. Order Block baissier
    if bear_ob and bear_ob["low"] <= price <= bear_ob["high"]:
        short_score += 1
        short_reasons.append(f"🟥 Bearish OB [{bear_ob['low']:.2f} – {bear_ob['high']:.2f}]")

    # 5. FVG baissier
    if bear_fvg and bear_fvg["bottom"] <= price <= bear_fvg["top"]:
        short_score += 1
        short_reasons.append(f"⚡ Bearish FVG [{bear_fvg['bottom']:.2f} – {bear_fvg['top']:.2f}]")

    # ══ Sélection finale ══════════════════════════════
    if long_score >= MIN_SCORE and long_score >= short_score and rsi_val < RSI_OVERBOUGHT:
        direction = "LONG"
        sl        = round(price - SL_ATR_MULT * atr_val, 2)
        tp        = round(price + TP_ATR_MULT * atr_val, 2)
        reasons, score = long_reasons, long_score

    elif short_score >= MIN_SCORE and rsi_val > RSI_OVERSOLD:
        direction = "SHORT"
        sl        = round(price + SL_ATR_MULT * atr_val, 2)
        tp        = round(price - TP_ATR_MULT * atr_val, 2)
        reasons, score = short_reasons, short_score

    else:
        log.info(f"Pas de confluence | LONG:{long_score} SHORT:{short_score}")
        return None

    rr = abs(tp - price) / abs(sl - price) if abs(sl - price) != 0 else 0
    if rr < RISK_REWARD:
        log.info(f"R:R insuffisant : {rr:.2f}")
        return None

    return {
        "direction" : direction,
        "entry"     : round(price, 2),
        "sl"        : sl,
        "tp"        : tp,
        "rr"        : round(rr, 2),
        "atr"       : round(atr_val, 2),
        "rsi"       : rsi_val,
        "sma_bias"  : sma_bias or "—",
        "score"     : score,
        "max_score" : 5,
        "reasons"   : reasons,
        "session"   : session,
        "timestamp" : datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


# ══════════════════════════════════════════════════════
#  7. BACKTESTING 🆕
# ══════════════════════════════════════════════════════

def run_backtest(df: pd.DataFrame) -> dict:
    """
    Backtesting simplifié sur les données historiques M15.
    Simule les signaux et calcule le Win Rate estimé.
    """
    log.info("🔬 Lancement du backtesting...")
    results = {"wins": 0, "losses": 0, "signals": 0, "pnl": 0.0}
    capital = CAPITAL

    window_size = max(SWING_LOOKBACK * 2 + 5, SMA_SLOW) + 10

    for i in range(window_size, len(df) - 5):
        slice_df = df.iloc[:i].copy()

        try:
            rsi_val  = compute_rsi(slice_df)
            sma_bias = get_sma_bias(slice_df)
            atr_val  = float(compute_atr(slice_df).iloc[-1])
            price    = float(slice_df["Close"].iloc[-1])

            swing_h, swing_l = detect_swing_points(slice_df)
            bos_choch        = detect_bos_choch(slice_df, swing_h, swing_l)
            bull_ob, bear_ob = detect_order_blocks(slice_df)

            direction = None
            sl = tp = 0

            if (sma_bias == "BULL" or rsi_val <= RSI_OVERSOLD or
                    bos_choch in ("BOS_BULL", "CHOCH_BULL") or
                    (bull_ob and bull_ob["low"] <= price <= bull_ob["high"])):
                direction = "LONG"
                sl = round(price - SL_ATR_MULT * atr_val, 2)
                tp = round(price + TP_ATR_MULT * atr_val, 2)

            elif (sma_bias == "BEAR" or rsi_val >= RSI_OVERBOUGHT or
                    bos_choch in ("BOS_BEAR", "CHOCH_BEAR") or
                    (bear_ob and bear_ob["low"] <= price <= bear_ob["high"])):
                direction = "SHORT"
                sl = round(price + SL_ATR_MULT * atr_val, 2)
                tp = round(price - TP_ATR_MULT * atr_val, 2)

            if not direction:
                continue

            # Simuler le résultat sur les 5 prochaines bougies
            future = df.iloc[i:i + 5]
            lot    = compute_lot_size(capital, price, sl)
            hit_tp = any(
                (direction == "LONG"  and float(r["High"]) >= tp) or
                (direction == "SHORT" and float(r["Low"])  <= tp)
                for _, r in future.iterrows()
            )
            hit_sl = any(
                (direction == "LONG"  and float(r["Low"])  <= sl) or
                (direction == "SHORT" and float(r["High"]) >= sl)
                for _, r in future.iterrows()
            )

            results["signals"] += 1
            if hit_tp and not hit_sl:
                pnl = abs(tp - price) * lot * 100
                results["wins"] += 1
                results["pnl"]  += pnl
                capital += pnl
            elif hit_sl:
                pnl = abs(sl - price) * lot * 100
                results["losses"] += 1
                results["pnl"]    -= pnl
                capital -= pnl

        except Exception:
            continue

    total = results["wins"] + results["losses"]
    results["winrate"]      = round(results["wins"] / total * 100, 1) if total > 0 else 0
    results["final_capital"] = round(capital, 2)
    results["pnl"]           = round(results["pnl"], 2)

    log.info(
        f"📊 Backtest terminé | Signaux:{results['signals']} | "
        f"W:{results['wins']} L:{results['losses']} | "
        f"WR:{results['winrate']}% | PnL:{results['pnl']}$"
    )
    return results


# ══════════════════════════════════════════════════════
#  8. TELEGRAM
# ══════════════════════════════════════════════════════

def send_telegram(message: str) -> bool:
    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("✅ Telegram envoyé.")
        return True
    except requests.RequestException as e:
        log.error(f"❌ Erreur Telegram : {e}")
        return False


def build_signal_message(signal: dict, sig_id: int, state: dict) -> str:
    emoji  = "🟢🚀" if signal["direction"] == "LONG" else "🔴📉"
    arrow  = "▲ LONG (ACHAT)" if signal["direction"] == "LONG" else "▼ SHORT (VENTE)"
    lot    = compute_lot_size(state["capital"], signal["entry"], signal["sl"])
    risk_usd = round(state["capital"] * RISK_PERCENT / 100, 2)
    sl_pts = abs(signal["entry"] - signal["sl"])
    tp_pts = abs(signal["tp"] - signal["entry"])
    reasons_text = "\n".join(f"   • {r}" for r in signal["reasons"])

    rsi_emoji = "🔵" if signal["rsi"] <= RSI_OVERSOLD else "🟠" if signal["rsi"] >= RSI_OVERBOUGHT else "⚪"
    sma_emoji = "🟢" if signal["sma_bias"] == "BULL" else "🔴" if signal["sma_bias"] == "BEAR" else "⚪"

    return (
        f"{emoji} *SIGNAL #{sig_id} — XAUUSD {arrow}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ *Heure*       : `{signal['timestamp']}`\n"
        f"🏙 *Session*     : `{signal['session']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 *ENTRÉE*      : `{signal['entry']} $`\n"
        f"🛡 *STOP LOSS*   : `{signal['sl']} $`  (-{sl_pts:.2f}$)\n"
        f"🎯 *TAKE PROFIT* : `{signal['tp']} $`  (+{tp_pts:.2f}$)\n"
        f"📊 *R:R*         : `1 : {signal['rr']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{rsi_emoji} *RSI 14*      : `{signal['rsi']}`\n"
        f"{sma_emoji} *MMA 50/100*  : `{signal['sma_bias']}`\n"
        f"📐 *ATR*         : `{signal['atr']} $`\n"
        f"⭐ *Score*       : `{signal['score']}/{signal['max_score']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 *Money Management :*\n"
        f"   • Capital    : `{state['capital']:.2f} $`\n"
        f"   • Risque     : `{RISK_PERCENT}%` = `{risk_usd} $`\n"
        f"   • Lot size   : `{lot}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"*📋 Confluences :*\n{reasons_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ _Signal indicatif. Gérez toujours votre risque._"
    )


def send_result_notification(sig: dict, result: str, pnl: float, capital: float):
    """Notification automatique Win/Loss. 🆕"""
    if result == "WIN":
        emoji = "✅🎉"
        color = "GAIN"
        pnl_str = f"+{pnl:.2f} $"
    else:
        emoji = "❌😔"
        color = "PERTE"
        pnl_str = f"{pnl:.2f} $"

    msg = (
        f"{emoji} *RÉSULTAT — Signal #{sig['id']}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Direction  : `{sig['direction']}`\n"
        f"💰 Entrée     : `{sig['entry']} $`\n"
        f"🎯 Résultat   : *{result}* — {color}\n"
        f"💵 PnL        : `{pnl_str}`\n"
        f"🏦 Capital    : `{capital:.2f} $`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    send_telegram(msg)


def send_backtest_notification(results: dict):
    """Envoie le rapport de backtest sur Telegram. 🆕"""
    msg = (
        f"🔬 *RAPPORT BACKTEST — XAUUSD M15*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *Signaux testés* : `{results['signals']}`\n"
        f"✅ *Wins*           : `{results['wins']}`\n"
        f"❌ *Losses*         : `{results['losses']}`\n"
        f"🎯 *Win Rate*       : `{results['winrate']}%`\n"
        f"💵 *PnL total*      : `{results['pnl']} $`\n"
        f"🏦 *Capital final*  : `{results['final_capital']} $`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"_Backtest sur {LOOKBACK_CANDLES} bougies M15_"
    )
    send_telegram(msg)


def send_startup_message(state: dict, backtest: dict = None):
    bt_line = ""
    if backtest:
        bt_line = (
            f"🔬 *Backtest* : WR `{backtest['winrate']}%` | "
            f"PnL `{backtest['pnl']}$` sur `{backtest['signals']}` signaux\n"
        )
    msg = (
        f"🤖 *XAUUSD Signal Bot v5* ✅\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 Symbole   : `XAU/USD`\n"
        f"⏱ M15 + H1  | Scan `{CHECK_INTERVAL}s`\n"
        f"📊 RSI `{RSI_PERIOD}` | MMA `{SMA_FAST}`/`{SMA_SLOW}`\n"
        f"🏙 Sessions  : London & New York\n"
        f"🛑 Stop auto : `{MAX_LOSSES}` pertes consécutives\n"
        f"💼 Risque    : `{RISK_PERCENT}%` par trade\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏦 *Capital* : `{state['capital']:.2f} $`\n"
        f"📈 W/L       : `{state['total_wins']}`W / `{state['total_losses']}`L\n"
        f"{bt_line}"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"_En attente de setups..._"
    )
    send_telegram(msg)


def send_stop_alert(state: dict):
    msg = (
        f"🚨 *BOT ARRÊTÉ — {MAX_LOSSES} PERTES CONSÉCUTIVES* 🚨\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Total signaux : `{state['total_signals']}`\n"
        f"✅ Gains         : `{state['total_wins']}`\n"
        f"❌ Pertes        : `{state['total_losses']}`\n"
        f"💵 PnL total     : `{state['total_pnl']:.2f} $`\n"
        f"🏦 Capital final : `{state['capital']:.2f} $`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"_Réinitialisez `bot_state.json` pour reprendre._"
    )
    send_telegram(msg)


# ══════════════════════════════════════════════════════
#  9. DASHBOARD HTML v5
# ══════════════════════════════════════════════════════

def generate_dashboard(state: dict, backtest: dict = None):
    signals  = state.get("signals_history", [])
    total    = state["total_signals"]
    wins     = state["total_wins"]
    losses   = state["total_losses"]
    cons_l   = state["consecutive_losses"]
    capital  = state["capital"]
    total_pnl= state["total_pnl"]
    winrate  = round((wins / total * 100) if total > 0 else 0, 1)

    bt_section = ""
    if backtest:
        bt_color = "green" if backtest["pnl"] >= 0 else "red"
        bt_section = f"""
        <div class="stat-card">
            <div class="label">🔬 BT Win Rate</div>
            <div class="value {'green' if backtest['winrate'] >= 50 else 'red'}">{backtest['winrate']}%</div>
        </div>
        <div class="stat-card">
            <div class="label">🔬 BT PnL</div>
            <div class="value {bt_color}">{backtest['pnl']}$</div>
        </div>"""

    rows = ""
    for s in signals:
        d_badge = (
            '<span class="badge long">▲ LONG</span>'  if s["direction"] == "LONG"
            else '<span class="badge short">▼ SHORT</span>'
        )
        r_badge = (
            '<span class="badge win">✅ WIN</span>'    if s["result"] == "Win"
            else '<span class="badge loss">❌ LOSS</span>' if s["result"] == "Loss"
            else '<span class="badge pending">⏳ En cours</span>'
        )
        pnl_str  = f'+{s["pnl"]}$' if s.get("pnl", 0) > 0 else f'{s.get("pnl",0)}$'
        pnl_cls  = "tp" if s.get("pnl", 0) > 0 else "sl"
        rows += f"""
        <tr>
            <td>#{s['id']}</td>
            <td>{s['timestamp']}</td>
            <td>{d_badge}</td>
            <td>{s['entry']}$</td>
            <td class="sl">{s['sl']}$</td>
            <td class="tp">{s['tp']}$</td>
            <td>1:{s['rr']}</td>
            <td>{s.get('rsi','—')}</td>
            <td>{s.get('sma_bias','—')}</td>
            <td>{s['score']}</td>
            <td>{s.get('lot_size','—')}</td>
            <td>{s.get('session','—')}</td>
            <td class="{pnl_cls}">{pnl_str}</td>
            <td>{r_badge}</td>
        </tr>"""

    stopped = '<div class="stopped-banner">🚨 BOT ARRÊTÉ — 10 pertes consécutives</div>' \
              if state.get("bot_stopped") else ""

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>XAUUSD Bot v5 — Dashboard</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Syne:wght@400;700;800&display=swap');
  :root{{--bg:#0a0c0f;--surface:#111418;--border:#1e2530;--gold:#f5c842;--gold2:#e8a020;--green:#22c55e;--red:#ef4444;--blue:#3b82f6;--muted:#6b7280;--text:#e5e7eb;}}
  *{{margin:0;padding:0;box-sizing:border-box;}}
  body{{background:var(--bg);color:var(--text);font-family:'JetBrains Mono',monospace;min-height:100vh;}}
  header{{background:linear-gradient(135deg,#0f1117,#1a1506);border-bottom:1px solid var(--gold2);padding:20px 28px;display:flex;align-items:center;justify-content:space-between;}}
  .logo{{font-family:'Syne',sans-serif;font-size:1.4rem;font-weight:800;color:var(--gold);}}
  .logo span{{color:var(--gold2);}}
  .ver{{font-size:0.7rem;color:var(--muted);margin-left:10px;}}
  .live{{display:flex;align-items:center;gap:8px;font-size:0.72rem;color:var(--green);}}
  .live::before{{content:'';width:8px;height:8px;border-radius:50%;background:var(--green);animation:pulse 1.5s infinite;}}
  @keyframes pulse{{0%,100%{{opacity:1;transform:scale(1);}}50%{{opacity:.5;transform:scale(1.4);}}}}
  .stopped-banner{{background:linear-gradient(90deg,#7f1d1d,#450a0a);border:1px solid #ef4444;color:#fca5a5;text-align:center;padding:14px;font-weight:700;}}
  .stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:14px;padding:24px 28px;}}
  .stat-card{{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:18px;text-align:center;}}
  .stat-card:hover{{border-color:var(--gold2);}}
  .label{{font-size:0.65rem;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:8px;}}
  .value{{font-family:'Syne',sans-serif;font-size:1.8rem;font-weight:800;color:var(--gold);}}
  .value.green{{color:var(--green);}} .value.red{{color:var(--red);}} .value.blue{{color:var(--blue);}}
  .section-title{{font-family:'Syne',sans-serif;font-size:.9rem;font-weight:700;color:var(--gold);padding:0 28px 14px;text-transform:uppercase;letter-spacing:2px;}}
  .tw{{padding:0 28px 40px;overflow-x:auto;}}
  table{{width:100%;border-collapse:collapse;font-size:.78rem;}}
  thead th{{background:var(--surface);color:var(--muted);text-transform:uppercase;font-size:.6rem;letter-spacing:1px;padding:10px 12px;text-align:left;border-bottom:2px solid var(--gold2);}}
  tbody tr{{border-bottom:1px solid var(--border);transition:background .15s;}}
  tbody tr:hover{{background:rgba(245,200,66,.04);}}
  tbody td{{padding:10px 12px;}}
  .sl{{color:var(--red);}} .tp{{color:var(--green);}}
  .badge{{display:inline-block;padding:2px 8px;border-radius:20px;font-size:.65rem;font-weight:700;}}
  .badge.long{{background:rgba(34,197,94,.15);color:var(--green);border:1px solid rgba(34,197,94,.3);}}
  .badge.short{{background:rgba(239,68,68,.15);color:var(--red);border:1px solid rgba(239,68,68,.3);}}
  .badge.win{{background:rgba(34,197,94,.15);color:var(--green);border:1px solid rgba(34,197,94,.3);}}
  .badge.loss{{background:rgba(239,68,68,.15);color:var(--red);border:1px solid rgba(239,68,68,.3);}}
  .badge.pending{{background:rgba(245,200,66,.12);color:var(--gold);border:1px solid rgba(245,200,66,.3);}}
  footer{{text-align:center;padding:20px;color:var(--muted);font-size:.65rem;border-top:1px solid var(--border);}}
</style>
</head>
<body>
<header>
  <div><span class="logo">XAU<span>/USD</span> Bot</span><span class="ver">v5</span></div>
  <div class="live">LIVE</div>
</header>
{stopped}
<div class="stats">
  <div class="stat-card"><div class="label">Signaux</div><div class="value blue">{total}</div></div>
  <div class="stat-card"><div class="label">Gains</div><div class="value green">{wins}</div></div>
  <div class="stat-card"><div class="label">Pertes</div><div class="value red">{losses}</div></div>
  <div class="stat-card"><div class="label">Win Rate</div><div class="value {'green' if winrate>=50 else 'red'}">{winrate}%</div></div>
  <div class="stat-card"><div class="label">PnL Total</div><div class="value {'green' if total_pnl>=0 else 'red'}">{total_pnl:.0f}$</div></div>
  <div class="stat-card"><div class="label">Capital</div><div class="value">{capital:.0f}$</div></div>
  <div class="stat-card"><div class="label">Pertes conséc.</div><div class="value {'red' if cons_l>=7 else ''}">{cons_l}/{MAX_LOSSES}</div></div>
  {bt_section}
</div>
<div class="section-title">📋 Historique des Signaux</div>
<div class="tw">
  <table>
    <thead><tr><th>#</th><th>Heure</th><th>Dir.</th><th>Entrée</th><th>SL</th><th>TP</th><th>R:R</th><th>RSI</th><th>MMA</th><th>Score</th><th>Lot</th><th>Session</th><th>PnL</th><th>Résultat</th></tr></thead>
    <tbody>{rows if rows else '<tr><td colspan="14" style="text-align:center;color:#6b7280;padding:30px">Aucun signal encore généré</td></tr>'}</tbody>
  </table>
</div>
<footer>XAUUSD Signal Bot v5 — SMC + RSI + MMA 50/100 — Multi-TF — {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}</footer>
</body></html>"""

    try:
        with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
            f.write(html)
        log.info(f"📊 Dashboard v5 mis à jour.")
    except Exception as e:
        log.error(f"Erreur dashboard : {e}")


# ══════════════════════════════════════════════════════
#  10. BOUCLE PRINCIPALE
# ══════════════════════════════════════════════════════

def main():
    log.info("═" * 60)
    log.info("  XAUUSD Signal Bot v5 — Edition Professionnelle")
    log.info("═" * 60)

    state = load_state()

    if state.get("bot_stopped"):
        log.warning("⛔ Bot arrêté — réinitialisez bot_state.json")
        send_telegram("⛔ *Bot arrêté.* Réinitialisez `bot_state.json` pour reprendre.")
        return

    # ── Backtest au démarrage ─────────────────────────
    backtest_result = None
    try:
        log.info("🔬 Backtest de démarrage...")
        df_bt = fetch_ohlcv(TF_ENTRY, LOOKBACK_CANDLES)
        time.sleep(2)
        if not df_bt.empty:
            backtest_result = run_backtest(df_bt)
            send_backtest_notification(backtest_result)
    except Exception as e:
        log.error(f"Erreur backtest : {e}")

    send_startup_message(state, backtest_result)
    generate_dashboard(state, backtest_result)

    while True:
        try:
            log.info(f"── Scan {datetime.now().strftime('%H:%M:%S')} ──")

            # Stop auto
            if state["consecutive_losses"] >= MAX_LOSSES:
                state["bot_stopped"] = True
                save_state(state)
                send_stop_alert(state)
                generate_dashboard(state, backtest_result)
                log.error(f"🚨 {MAX_LOSSES} pertes — Bot arrêté.")
                break

            # Vérification Win/Loss signal précédent
            if state.get("last_signal") and state["last_signal"]["result"] == "En cours":
                df_check = fetch_ohlcv(TF_ENTRY, 5)
                time.sleep(1)
                if not df_check.empty:
                    update_result(state, float(df_check["Close"].iloc[-1]))
                    generate_dashboard(state, backtest_result)

            # Filtre session
            session = get_active_session()
            if not session:
                log.info("⏸ Hors session — scan ignoré.")
                time.sleep(CHECK_INTERVAL)
                continue

            log.info(f"🏙 Session : {session}")

            # Données
            df_m15 = fetch_ohlcv(TF_ENTRY,   LOOKBACK_CANDLES)
            time.sleep(2)
            df_h1  = fetch_ohlcv(TF_CONFIRM, LOOKBACK_CANDLES)

            # Analyse
            signal = analyse_market(df_m15, df_h1, session)

            if signal:
                if signal["direction"] != state["last_direction"]:
                    entry  = record_signal(state, signal)
                    sig_id = entry["id"]
                    msg    = build_signal_message(signal, sig_id, state)
                    if send_telegram(msg):
                        state["last_direction"] = signal["direction"]
                        log.info(f"✅ Signal #{sig_id} {signal['direction']} | E:{signal['entry']} SL:{signal['sl']} TP:{signal['tp']}")
                    generate_dashboard(state, backtest_result)
                else:
                    log.info(f"Signal {signal['direction']} ignoré (doublon).")
            else:
                log.info("Pas de setup valide.")

        except KeyboardInterrupt:
            log.info("⏹ Arrêt manuel.")
            send_telegram("⏹ *Bot arrêté manuellement.*")
            break
        except Exception as e:
            log.error(f"Erreur : {e}", exc_info=True)

        log.info(f"Prochain scan dans {CHECK_INTERVAL}s...\n")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
