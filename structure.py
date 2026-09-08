"""
Tendencia por ESTRUCTURA de mercado + medias + fuerza direccional.

Para cada timeframe se calcula un trend_score de -100 a +100 que combina:
  - Estructura (±40): secuencia de swings. Maximos y minimos crecientes (HH/HL)
    es alcista; decrecientes (LH/LL) es bajista. Detecta ademas:
      * BOS  (break of structure): el precio rompe el ultimo swing a favor de
        la tendencia -> continuacion confirmada.
      * CHoCH (change of character): el precio rompe el ultimo swing EN CONTRA
        de la tendencia -> primera senal objetiva de giro, mucho antes que la EMA200.
  - Medias (±30): precio vs EMA20/50/200 y pendiente de la EMA50.
  - Fuerza direccional (±30): DI+ vs DI- escalado por ADX.

Tambien detecta divergencias RSI y OBV usando swings REALES (no mitades de ventana).
"""

import numpy as np
import pandas as pd
from levels import find_swings


def _last_swings(swings: list, kind: str, n: int = 3) -> list:
    return [s for s in swings if s["kind"] == kind][-n:]


def structure_state(df: pd.DataFrame, swings: list) -> dict:
    """
    Clasifica la estructura con los ultimos 2 swing highs y 2 swing lows
    confirmados, y detecta BOS / CHoCH con el ultimo cierre.
    """
    highs = _last_swings(swings, "H", 2)
    lows = _last_swings(swings, "L", 2)
    close = float(df["close"].iloc[-1])
    out = {"structure": "INDEFINIDA", "hh": None, "hl": None, "lh": None, "ll": None,
           "bos": None, "choch": None, "last_high": None, "last_low": None}
    if len(highs) < 2 or len(lows) < 2:
        return out

    hh = highs[-1]["price"] > highs[-2]["price"]
    hl = lows[-1]["price"] > lows[-2]["price"]
    lh = highs[-1]["price"] < highs[-2]["price"]
    ll = lows[-1]["price"] < lows[-2]["price"]
    out.update({"hh": hh, "hl": hl, "lh": lh, "ll": ll,
                "last_high": highs[-1]["price"], "last_low": lows[-1]["price"],
                "prev_high": highs[-2]["price"], "prev_low": lows[-2]["price"]})

    if hh and hl:
        structure = "ALCISTA"
    elif lh and ll:
        structure = "BAJISTA"
    elif hh and ll:
        structure = "EXPANSION"   # rango que se ensancha (volatil, sin direccion)
    elif lh and hl:
        structure = "COMPRESION"  # triangulo: se viene una ruptura
    else:
        structure = "MIXTA"
    out["structure"] = structure

    # Rupturas con el ultimo cierre (la vela actual puede estar abierta: usamos
    # la ultima cerrada para confirmar, la abierta solo como "en curso")
    closed = float(df["close"].iloc[-2]) if len(df) >= 2 else close
    if structure == "ALCISTA":
        if closed > highs[-1]["price"]:
            out["bos"] = "ALCISTA"
        if closed < lows[-1]["price"]:
            out["choch"] = "BAJISTA"
    elif structure == "BAJISTA":
        if closed < lows[-1]["price"]:
            out["bos"] = "BAJISTA"
        if closed > highs[-1]["price"]:
            out["choch"] = "ALCISTA"
    else:
        if closed > highs[-1]["price"]:
            out["bos"] = "ALCISTA"
        elif closed < lows[-1]["price"]:
            out["bos"] = "BAJISTA"
    return out


def trend_score(df: pd.DataFrame, swings: list | None = None, fractal: int = 3) -> dict:
    """Score de tendencia -100..100 para un timeframe, con desglose."""
    if df is None or len(df) < 30:
        return {"score": 0, "label": "SIN DATOS", "structure": {}, "components": {}}
    if swings is None:
        swings = find_swings(df, fractal, fractal)
    st = structure_state(df, swings)
    row = df.iloc[-2] if len(df) >= 2 else df.iloc[-1]   # ultima vela cerrada
    price = float(row["close"])

    # ── Estructura (±40) ──
    s_struct = {"ALCISTA": 30, "BAJISTA": -30, "EXPANSION": 0, "COMPRESION": 0,
                "MIXTA": 0, "INDEFINIDA": 0}[st["structure"]]
    if st["structure"] == "MIXTA":
        s_struct = 12 if st.get("hl") else -12 if st.get("lh") else 0
    if st["bos"] == "ALCISTA":
        s_struct += 10
    elif st["bos"] == "BAJISTA":
        s_struct -= 10
    if st["choch"] == "BAJISTA":
        s_struct = min(s_struct, 0) - 15
    elif st["choch"] == "ALCISTA":
        s_struct = max(s_struct, 0) + 15
    s_struct = int(np.clip(s_struct, -40, 40))

    # ── Medias (±30) ──
    s_ema = 0
    e20, e50, e200 = row.get("EMA20"), row.get("EMA50"), row.get("EMA200")
    if pd.notna(e20):
        s_ema += 8 if price > e20 else -8
    if pd.notna(e50):
        s_ema += 8 if price > e50 else -8
        # pendiente de la EMA50 en las ultimas 10 velas
        if len(df) > 12 and pd.notna(df["EMA50"].iloc[-12]):
            slope = (e50 / df["EMA50"].iloc[-12] - 1) * 100
            s_ema += int(np.clip(slope * 4, -6, 6))
    if pd.notna(e200):
        s_ema += 8 if price > e200 else -8
    s_ema = int(np.clip(s_ema, -30, 30))

    # ── Fuerza direccional (±30) ──
    s_di = 0
    adx, dip, din = row.get("ADX"), row.get("DI_POS"), row.get("DI_NEG")
    if pd.notna(adx) and pd.notna(dip) and pd.notna(din):
        strength = float(np.clip((adx - 15) / 25, 0, 1))   # ADX 15 -> 0, ADX 40 -> 1
        direction = 1 if dip > din else -1
        s_di = int(round(30 * strength * direction))

    score = int(np.clip(s_struct + s_ema + s_di, -100, 100))
    if score >= 55:
        label = "ALCISTA FUERTE"
    elif score >= 20:
        label = "ALCISTA"
    elif score <= -55:
        label = "BAJISTA FUERTE"
    elif score <= -20:
        label = "BAJISTA"
    else:
        label = "LATERAL"

    return {"score": score, "label": label, "structure": st,
            "components": {"estructura": s_struct, "medias": s_ema, "fuerza": s_di},
            "adx": round(float(adx), 1) if pd.notna(adx) else None,
            "di_pos": round(float(dip), 1) if pd.notna(dip) else None,
            "di_neg": round(float(din), 1) if pd.notna(din) else None}


# ── Divergencias con swings reales ───────────────────────────────────────────

def divergences(df: pd.DataFrame, swings: list, col: str = "RSI14") -> dict:
    """
    Divergencia alcista: ultimo swing low del precio mas bajo que el anterior,
    pero el indicador (RSI u OBV) en ese swing es mas alto -> la caida pierde fuerza.
    Divergencia bajista: simetrica en los swing highs.
    Solo cuenta si el segundo swing es reciente (ultimas 25 velas).
    """
    out = {"bullish": False, "bearish": False, "detail": ""}
    if col not in df.columns:
        return out
    n = len(df)
    lows = _last_swings(swings, "L", 2)
    highs = _last_swings(swings, "H", 2)
    ind = df[col].values
    if len(lows) == 2 and n - 1 - lows[-1]["pos"] <= 25:
        a, b = lows[-2], lows[-1]
        ia, ib = ind[a["pos"]], ind[b["pos"]]
        if b["price"] < a["price"] and pd.notna(ia) and pd.notna(ib) and ib > ia * (1.02 if col == "OBV" else 1) + (2 if col.startswith("RSI") else 0):
            out["bullish"] = True
            out["detail"] = f"precio hizo minimo mas bajo pero {col} mas alto"
    if len(highs) == 2 and n - 1 - highs[-1]["pos"] <= 25:
        a, b = highs[-2], highs[-1]
        ia, ib = ind[a["pos"]], ind[b["pos"]]
        if b["price"] > a["price"] and pd.notna(ia) and pd.notna(ib) and ib < ia * (0.98 if col == "OBV" else 1) - (2 if col.startswith("RSI") else 0):
            out["bearish"] = True
            out["detail"] = f"precio hizo maximo mas alto pero {col} mas bajo"
    return out


def exhaustion(df: pd.DataFrame, swings: list, trend_label: str) -> dict:
    """
    Detector de agotamiento / posible giro (0-100). Combina:
      CHoCH contra la tendencia (30), divergencia RSI u OBV contra (25),
      ADX cayendo desde pico (20), RSI6 extremo (15), MACD hist contrayendose (10).
    """
    score, signals = 0, []
    is_bull = "ALCISTA" in trend_label
    is_bear = "BAJISTA" in trend_label
    if not (is_bull or is_bear) or len(df) < 20:
        return {"score": 0, "reversing": False, "direction": None, "signals": []}

    st = structure_state(df, swings)
    if is_bull and st["choch"] == "BAJISTA":
        score += 30; signals.append(f"Perdio el ultimo minimo relevante ({st['last_low']:.4g}): cambio de caracter bajista")
    if is_bear and st["choch"] == "ALCISTA":
        score += 30; signals.append(f"Supero el ultimo maximo relevante ({st['last_high']:.4g}): cambio de caracter alcista")

    rsi_div = divergences(df, swings, "RSI14")
    obv_div = divergences(df, swings, "OBV")
    if is_bull and (rsi_div["bearish"] or obv_div["bearish"]):
        score += 25; signals.append("Divergencia bajista (%s)" % ("OBV: distribucion" if obv_div["bearish"] else "RSI"))
    if is_bear and (rsi_div["bullish"] or obv_div["bullish"]):
        score += 25; signals.append("Divergencia alcista (%s)" % ("OBV: acumulacion" if obv_div["bullish"] else "RSI"))

    adx_s = df["ADX"].dropna()
    if len(adx_s) >= 8:
        peak = adx_s.iloc[-8:-1].max(); now = adx_s.iloc[-2]
        if peak > 25 and peak - now >= 4:
            score += 20; signals.append(f"ADX cayendo desde pico ({peak:.0f} -> {now:.0f}): la tendencia pierde fuerza")

    rsi6 = df["RSI6"].iloc[-2]
    if pd.notna(rsi6):
        if is_bull and rsi6 > 80:
            score += 15; signals.append(f"RSI(6) en sobrecompra extrema ({rsi6:.0f})")
        if is_bear and rsi6 < 20:
            score += 15; signals.append(f"RSI(6) en sobreventa extrema ({rsi6:.0f})")

    h = df["MACD_HIST"].iloc[-4:-1].values
    if len(h) == 3 and not np.isnan(h).any():
        if is_bull and h[2] > 0 and h[2] < h[1] < h[0]:
            score += 10; signals.append("MACD histograma contrayendose: momentum alcista debilitando")
        if is_bear and h[2] < 0 and h[2] > h[1] > h[0]:
            score += 10; signals.append("MACD histograma contrayendose: momentum bajista debilitando")

    direction = None
    if score >= 50:
        direction = "BAJISTA" if is_bull else "ALCISTA"
    return {"score": min(score, 100), "reversing": score >= 50, "direction": direction,
            "signals": signals, "rsi_div": rsi_div, "obv_div": obv_div}
