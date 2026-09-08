"""
Motor de analisis v4 — tendencia por estructura + soportes/resistencias reales.

Flujo por activo:
  1. Indicadores en cada timeframe.
  2. Tendencia por timeframe (structure.trend_score) y sesgo global ponderado.
  3. Niveles estructurales (levels.build_levels).
  4. Ubicacion del precio respecto de los niveles: en soporte, en resistencia,
     en medio del rango, rompiendo.
  5. Timing en el marco corto (1H): momentum girando a favor.
  6. Setup: PULLBACK (compra en soporte con tendencia a favor), BREAKOUT
     (ruptura de resistencia con volumen), o nada.
  7. Stop y objetivos con NIVELES REALES (stop detras del soporte, TP en la
     siguiente resistencia), R/R real, y rechazo si no hay espacio.
  8. Agotamiento / giro (structure.exhaustion) como freno.
  9. Accion sugerida en lenguaje claro para tres usos: trading, comprar/vender
     en cartera, y entender la situacion.

El mismo motor sirve para cripto (long/short, marco 4H) y acciones/CEDEARs
(solo long, marco 1D).
"""

from datetime import datetime, timezone
import numpy as np
import pandas as pd

from indicators import add_indicators, add_daily_vwap
from levels import build_levels, find_swings, TF_FRACTAL
from structure import trend_score, exhaustion, divergences

# Ponderacion de los timeframes en el sesgo global
BIAS_WEIGHTS = {
    "crypto": {"1w": 0.30, "1d": 0.35, "4h": 0.35},
    "stock": {"1w": 0.45, "1d": 0.55},
}


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _lab(level, fem=False):
    """Etiqueta de fuerza en minuscula con genero (soporte fuerte / resistencia media)."""
    if not level:
        return ""
    m = {"FUERTE": "fuerte", "MEDIO": "media" if fem else "medio", "DEBIL": "debil"}
    return m.get(level["label"], level["label"].lower())


def _fmt(p):
    if p is None:
        return "—"
    if abs(p) >= 1000:
        return f"${p:,.0f}"
    if abs(p) >= 10:
        return f"${p:,.2f}"
    return f"${p:,.4f}"


def prepare_frames(dfs: dict) -> dict:
    """Agrega indicadores (y VWAP diario a los intradiarios). Ignora None."""
    out = {}
    for tf, df in dfs.items():
        if df is None or len(df) < 30:
            continue
        d = df.copy()
        add_indicators(d)
        if tf in ("1h", "4h"):
            add_daily_vwap(d)
        out[tf] = d
    return out


# ── Timing en el marco corto ──────────────────────────────────────────────────

def timing_state(df: pd.DataFrame) -> dict:
    """
    Momentum de corto plazo en la ultima vela cerrada: cuenta senales de giro
    alcista y bajista (RSI6 girando, MACD hist, cierre sobre el maximo previo,
    precio vs EMA20 y VWAP).
    """
    if df is None or len(df) < 5:
        return {"bull": 0, "bear": 0, "direction": "—", "detail": []}
    r, p = df.iloc[-2], df.iloc[-3]
    bull, bear, det = 0, 0, []
    if pd.notna(r["RSI6"]) and pd.notna(p["RSI6"]):
        if r["RSI6"] > p["RSI6"] and r["RSI6"] > 40:
            bull += 1; det.append("RSI6 girando al alza")
        elif r["RSI6"] < p["RSI6"] and r["RSI6"] < 60:
            bear += 1; det.append("RSI6 girando a la baja")
    if pd.notna(r["MACD_HIST"]) and pd.notna(p["MACD_HIST"]):
        if r["MACD_HIST"] > p["MACD_HIST"]:
            bull += 1; det.append("MACD acelerando al alza")
        else:
            bear += 1; det.append("MACD acelerando a la baja")
    if r["close"] > p["high"]:
        bull += 1; det.append("cierre sobre el maximo previo")
    elif r["close"] < p["low"]:
        bear += 1; det.append("cierre bajo el minimo previo")
    if pd.notna(r["EMA20"]):
        if r["close"] > r["EMA20"]:
            bull += 1
        else:
            bear += 1
    vw = r.get("VWAP")
    if vw is not None and pd.notna(vw):
        if r["close"] > vw:
            bull += 1; det.append("sobre VWAP")
        else:
            bear += 1; det.append("bajo VWAP")
    direction = "ALCISTA" if bull >= bear + 2 else "BAJISTA" if bear >= bull + 2 else "NEUTRO"
    return {"bull": bull, "bear": bear, "direction": direction, "detail": det,
            "rsi6": round(float(r["RSI6"]), 1) if pd.notna(r["RSI6"]) else None}


# ── Analisis principal ────────────────────────────────────────────────────────

def analyze_frames(name: str, frames: dict, asset_type: str = "crypto",
                   allow_short: bool = True, funding: dict | None = None,
                   capital: float = 10000, risk_pct: float = 0.02) -> dict:
    """
    frames: dict tf -> DataFrame YA con indicadores (prepare_frames).
    asset_type: "crypto" (marco 4H) o "stock" (marco 1D).
    """
    ref_tf = "4h" if asset_type == "crypto" else "1d"
    ref = frames.get(ref_tf)
    if ref is None or len(ref) < 60:
        return {"name": name, "error": f"Datos insuficientes en {ref_tf}"}

    last_closed = ref.iloc[-2]
    price = float(ref["close"].iloc[-1])          # ultimo precio conocido
    price_closed = float(last_closed["close"])
    atr = float(last_closed["ATR"]) if pd.notna(last_closed["ATR"]) else float((ref["high"] - ref["low"]).tail(14).mean())

    # ── Tendencias por timeframe ──
    swings = {tf: find_swings(df, TF_FRACTAL[tf], TF_FRACTAL[tf]) for tf, df in frames.items() if tf in TF_FRACTAL}
    trends = {tf: trend_score(df, swings.get(tf), TF_FRACTAL[tf]) for tf, df in frames.items() if tf in TF_FRACTAL}
    weights = BIAS_WEIGHTS[asset_type]
    bias = sum(trends[tf]["score"] * w for tf, w in weights.items() if tf in trends) / \
        max(sum(w for tf, w in weights.items() if tf in trends), 1e-9)
    bias = int(round(bias))
    labels = [trends[tf]["label"] for tf in weights if tf in trends]
    n_up = sum("ALCISTA" in l for l in labels)
    n_dn = sum("BAJISTA" in l for l in labels)
    aligned = (n_up == len(labels)) or (n_dn == len(labels))

    if bias >= 55 and aligned:
        regime = "BULL FUERTE"
    elif bias >= 20:
        regime = "BULL" if n_dn == 0 else "BULL DEBIL"
    elif bias <= -55 and aligned:
        regime = "BEAR FUERTE"
    elif bias <= -20:
        regime = "BEAR" if n_up == 0 else "BEAR DEBIL"
    elif n_up and n_dn:
        regime = "TRANSICION"
    else:
        regime = "LATERAL"

    # ── Niveles ──
    lv = build_levels(frames, price, ref_tf=ref_tf, asset_type=asset_type)
    tol = lv["tolerance"]
    sup, res = lv["nearest_support"], lv["nearest_resistance"]
    at_level = lv["at_level"]

    # Ubicacion respecto de los niveles
    near_sup = sup is not None and (price - sup["hi"]) <= 1.2 * tol
    near_res = res is not None and (res["lo"] - price) <= 1.2 * tol
    if at_level:
        # El precio esta DENTRO de una zona: si esta sobre el centro y la ultima
        # vela cerro sobre el, la zona actua de soporte (apoyo / retest); si no,
        # de resistencia (techo que todavia no supero).
        if price >= at_level["price"] and price_closed >= at_level["lo"]:
            near_sup, at_role = True, "S"
        else:
            near_res, at_role = True, "R"
    else:
        at_role = None
    sup_zone = at_level if at_role == "S" else sup
    res_zone = at_level if at_role == "R" else res
    room_up = max(0.0, (res_zone["hi"] / price - 1) * 100) if res_zone else None
    room_dn = max(0.0, (1 - sup_zone["lo"] / price) * 100) if sup_zone else None

    if near_sup and not near_res:
        position = "EN SOPORTE"
    elif near_res and not near_sup:
        position = "EN RESISTENCIA"
    elif near_sup and near_res:
        position = "RANGO ESTRECHO"
    else:
        position = "EN MEDIO DEL RANGO"

    # ── Ruptura (breakout) en la ultima vela cerrada del marco de referencia ──
    vol_ok = pd.notna(last_closed["VOL_MA20"]) and last_closed["volume"] > 1.3 * last_closed["VOL_MA20"]
    prev2 = ref.iloc[-3]
    breakout_up = breakout_dn = None
    for r_ in lv["resistances"] + ([at_level] if at_level else []):
        if r_ and prev2["close"] < r_["hi"] <= price_closed and r_["strength"] >= 25:
            breakout_up = r_; break
    for s_ in lv["supports"] + ([at_level] if at_level else []):
        if s_ and prev2["close"] > s_["lo"] >= price_closed and s_["strength"] >= 25:
            breakout_dn = s_; break

    # ── Timing corto y agotamiento ──
    timing = timing_state(frames.get("1h") if "1h" in frames else ref)
    exh = exhaustion(ref, swings.get(ref_tf, []), trends[ref_tf]["label"])
    rsi14 = float(last_closed["RSI14"]) if pd.notna(last_closed["RSI14"]) else 50.0
    ref_struct = trends[ref_tf]["structure"]

    # ── Funding (solo cripto) ──
    funding = funding or {"available": False}
    f_impact = funding.get("impact", "NEUTRAL") if funding.get("available") else "NEUTRAL"

    # ── Deteccion de setup ──
    setup, direction, reasons, warnings = None, None, [], []
    bull_bias = bias >= 15
    bear_bias = bias <= -15

    if bull_bias and near_sup and sup_zone and timing["direction"] != "BAJISTA" and rsi14 < 70:
        setup, direction = "PULLBACK", "LONG"
        reasons.append(f"Tendencia {regime.lower()} y precio apoyado en soporte {_lab(sup_zone)} ({_fmt(sup_zone['price'])})")
    elif breakout_up and bias >= -10 and (vol_ok or breakout_up["strength"] >= 50) and timing["direction"] != "BAJISTA":
        setup, direction = "BREAKOUT", "LONG"
        reasons.append(f"Ruptura de resistencia {_lab(breakout_up, True)} en {_fmt(breakout_up['price'])}" + (" con volumen" if vol_ok else ""))
    elif allow_short and bear_bias and near_res and res_zone and timing["direction"] != "ALCISTA" and rsi14 > 30:
        setup, direction = "PULLBACK", "SHORT"
        reasons.append(f"Tendencia {regime.lower()} y precio rechazado en resistencia {_lab(res_zone, True)} ({_fmt(res_zone['price'])})")
    elif allow_short and breakout_dn and bias <= 10 and (vol_ok or breakout_dn["strength"] >= 50) and timing["direction"] != "ALCISTA":
        setup, direction = "BREAKOUT", "SHORT"
        reasons.append(f"Perdida de soporte {_lab(breakout_dn)} en {_fmt(breakout_dn['price'])}" + (" con volumen" if vol_ok else ""))

    # ── Stop / objetivos con niveles reales ──
    plan = None
    if direction:
        entry = price
        if direction == "LONG":
            base = sup_zone if setup == "PULLBACK" else (breakout_up or sup_zone)
            stop = (base["lo"] if base else entry) - 0.5 * atr
            if entry - stop > 3.0 * atr:          # stop demasiado lejos -> usar ATR
                stop = entry - 1.8 * atr
            targets = [r_["price"] for r_ in lv["resistances"] if r_["price"] > entry + 0.5 * atr]
            tp3_fallback = entry + 4.5 * atr
        else:
            base = res_zone if setup == "PULLBACK" else (breakout_dn or res_zone)
            stop = (base["hi"] if base else entry) + 0.5 * atr
            if stop - entry > 3.0 * atr:
                stop = entry + 1.8 * atr
            targets = [s_["price"] for s_ in lv["supports"] if s_["price"] < entry - 0.5 * atr]
            tp3_fallback = entry - 4.5 * atr
        risk = abs(entry - stop)
        tps = targets[:3]
        while len(tps) < 3:
            # completar con extensiones de ATR si faltan niveles
            last = tps[-1] if tps else entry
            tps.append(last + (2.0 * atr if direction == "LONG" else -2.0 * atr))
        rr1 = abs(tps[0] - entry) / risk if risk > 0 else 0
        rr2 = abs(tps[1] - entry) / risk if risk > 0 else 0
        risk_amount = capital * risk_pct
        pos_usd = risk_amount / (risk / entry) if risk > 0 else 0
        plan = {"direction": direction, "entry": round(entry, 6), "stop": round(stop, 6),
                "tp1": round(tps[0], 6), "tp2": round(tps[1], 6), "tp3": round(tps[2], 6),
                "risk_pct": round(risk / entry * 100, 2), "rr_tp1": round(rr1, 2), "rr_tp2": round(rr2, 2),
                "risk_usd": round(risk_amount, 2), "position_usd": round(pos_usd, 2),
                "units": round(pos_usd / entry, 6) if entry else 0,
                "stop_basis": (base["sources"][0] if base and base.get("sources") else "ATR")}
        # Rechazo por falta de espacio
        if rr1 < 1.2:
            warnings.append(f"Sin espacio: el primer objetivo ({_fmt(tps[0])}) queda a solo {rr1:.1f}R del riesgo")
            setup, direction = None, None
            plan["rejected"] = True

    # ── Confianza ──
    confidence = "—"
    conf_pts = 0
    if direction:
        conf_pts += 2 if aligned else 1 if abs(bias) >= 35 else 0
        zone = sup_zone if direction == "LONG" else res_zone
        if setup == "PULLBACK" and zone:
            conf_pts += 2 if zone["strength"] >= 60 else 1 if zone["strength"] >= 30 else 0
        if setup == "BREAKOUT":
            conf_pts += 2 if vol_ok else 0
        want = "ALCISTA" if direction == "LONG" else "BAJISTA"
        conf_pts += 1 if timing["direction"] == want else 0
        conf_pts += 1 if plan and plan["rr_tp1"] >= 2 else 0
        if exh["reversing"] and exh["direction"] != want:
            conf_pts -= 2; warnings.append("Agotamiento detectado en contra del setup")
        if (direction == "LONG" and f_impact == "BEARISH") or (direction == "SHORT" and f_impact == "BULLISH"):
            conf_pts -= 1; warnings.append(f"Funding en contra: {funding.get('sentiment')}")
        if (direction == "LONG" and f_impact == "BULLISH") or (direction == "SHORT" and f_impact == "BEARISH"):
            conf_pts += 1
        if ref_struct.get("choch") and ref_struct["choch"] != want:
            conf_pts -= 2; warnings.append(f"Cambio de caracter {ref_struct['choch'].lower()} en {ref_tf.upper()}")
        confidence = "ALTA" if conf_pts >= 5 else "MEDIA" if conf_pts >= 3 else "BAJA"

    signal = direction or "NEUTRAL"

    # ── Accion sugerida (lenguaje claro) ──
    action, action_detail = _suggest_action(asset_type, allow_short, signal, setup, regime, bias,
                                            position, sup_zone, res_zone, exh, ref_struct, rsi14,
                                            room_up, room_dn, timing, confidence, warnings)

    # ── Escenarios condicionales con niveles reales ──
    scenarios = _scenarios(price, lv, regime, ref_tf)

    # ── Resumen ──
    summary = _summary(name, regime, bias, trends, weights, position, sup_zone, res_zone, exh,
                       ref_struct, action, signal, confidence, funding)

    return {
        "name": name, "asset_type": asset_type, "price": round(price, 6),
        "price_closed": round(price_closed, 6), "atr": round(atr, 6),
        "signal": signal, "setup": setup, "confidence": confidence, "reasons": reasons,
        "warnings": warnings + exh["signals"][:2] if exh["reversing"] else warnings,
        "action": action, "action_detail": action_detail,
        "regime": regime, "bias": bias, "aligned": aligned,
        "trends": {tf: {"label": t["label"], "score": t["score"], "components": t["components"],
                        "structure": t["structure"]["structure"], "bos": t["structure"].get("bos"),
                        "choch": t["structure"].get("choch"), "adx": t.get("adx"),
                        "last_high": t["structure"].get("last_high"), "last_low": t["structure"].get("last_low")}
                   for tf, t in trends.items()},
        "position": position, "room_up_pct": round(room_up, 2) if room_up is not None else None,
        "room_down_pct": round(room_dn, 2) if room_dn is not None else None,
        "levels": {"supports": lv["supports"], "resistances": lv["resistances"],
                   "at_level": at_level, "key_levels": lv["key_levels"],
                   "tolerance": lv["tolerance"], "volume_profile": lv["volume_profile"],
                   "nearest_support": sup_zone, "nearest_resistance": res_zone},
        "plan": plan, "timing": timing, "exhaustion": exh,
        "breakout": {"up": breakout_up["price"] if breakout_up else None,
                     "down": breakout_dn["price"] if breakout_dn else None, "volume_ok": bool(vol_ok)},
        "indicators": {
            "rsi6": round(float(last_closed["RSI6"]), 1) if pd.notna(last_closed["RSI6"]) else None,
            "rsi14": round(rsi14, 1),
            "macd_hist": round(float(last_closed["MACD_HIST"]), 5) if pd.notna(last_closed["MACD_HIST"]) else None,
            "adx": trends[ref_tf].get("adx"), "di_pos": trends[ref_tf].get("di_pos"), "di_neg": trends[ref_tf].get("di_neg"),
            "ema20": round(float(last_closed["EMA20"]), 6) if pd.notna(last_closed["EMA20"]) else None,
            "ema50": round(float(last_closed["EMA50"]), 6) if pd.notna(last_closed["EMA50"]) else None,
            "ema200": round(float(last_closed["EMA200"]), 6) if pd.notna(last_closed["EMA200"]) else None,
            "bb_width": round(float(last_closed["BB_WIDTH"]), 5) if pd.notna(last_closed["BB_WIDTH"]) else None,
            "bb_squeeze": _bb_squeeze(ref),
            "obv_trend": "ALCISTA" if pd.notna(last_closed["OBV_MA20"]) and last_closed["OBV"] > last_closed["OBV_MA20"] else "BAJISTA",
            "vwap": round(float(last_closed["VWAP"]), 6) if "VWAP" in last_closed and pd.notna(last_closed["VWAP"]) else None,
            "volume_rel": round(float(last_closed["volume"] / last_closed["VOL_MA20"]), 2) if pd.notna(last_closed["VOL_MA20"]) and last_closed["VOL_MA20"] > 0 else None,
        },
        "funding": funding, "scenarios": scenarios, "summary": summary,
        "ref_tf": ref_tf, "updated_at": _utcnow().strftime("%Y-%m-%d %H:%M UTC"), "error": None,
    }


def _bb_squeeze(df):
    w = df["BB_WIDTH"].dropna().tail(120)
    if len(w) < 30:
        return False
    return bool(w.iloc[-1] < w.quantile(0.2))


def _suggest_action(asset_type, allow_short, signal, setup, regime, bias, position, sup, res,
                    exh, struct, rsi14, room_up, room_dn, timing, confidence, warnings):
    """Devuelve (accion_corta, explicacion) pensada para decidir."""
    bull = bias >= 15
    bear = bias <= -15
    s = _fmt(sup["price"]) if sup else "—"
    r = _fmt(res["price"]) if res else "—"

    if signal == "LONG":
        if setup == "PULLBACK":
            return ("COMPRAR", f"Entrada a favor de tendencia en soporte {s}. Stop debajo del soporte, objetivo en {r}. Confianza {confidence.lower()}.")
        return ("COMPRAR (ruptura)", f"Rompio resistencia. Entrar con stop bajo el nivel roto y objetivo en {r}. Si vuelve a caer bajo el nivel, salir: ruptura falsa. Confianza {confidence.lower()}.")
    if signal == "SHORT":
        if setup == "PULLBACK":
            return ("VENDER / SHORT", f"Rechazo en resistencia {r} con tendencia bajista. Stop arriba de la resistencia, objetivo en {s}. Confianza {confidence.lower()}.")
        return ("VENDER / SHORT (ruptura)", f"Perdio soporte. Short con stop sobre el nivel perdido y objetivo en {s}. Confianza {confidence.lower()}.")

    # Sin setup: explicar que hacer segun contexto
    if exh["reversing"]:
        if exh["direction"] == "BAJISTA":
            return ("PROTEGER GANANCIAS", f"Tendencia alcista con senales de agotamiento ({exh['score']}/100). Si tenes posicion, subir stop o tomar parcial. No comprar hasta que apoye en {s}.")
        return ("ESPERAR EL PISO", f"Tendencia bajista con senales de agotamiento ({exh['score']}/100). Si tenes short, cubrir parcial. Para comprar esperar confirmacion arriba de {r}.")
    if struct.get("choch") == "BAJISTA":
        return ("REDUCIR", f"Perdio el ultimo minimo relevante: la estructura alcista se rompio. Reducir exposicion, no promediar. Soporte siguiente {s}.")
    if struct.get("choch") == "ALCISTA":
        return ("EMPEZAR A ACUMULAR", f"Supero el ultimo maximo relevante: primera senal objetiva de giro alcista. Entradas chicas, con stop bajo {s}.")
    if bull:
        if position == "EN RESISTENCIA":
            return ("MANTENER, NO COMPRAR", f"Tendencia alcista pero el precio esta contra la resistencia {r}. Comprar aca es pagar caro: esperar ruptura con volumen o pullback a {s}.")
        if position == "EN SOPORTE":
            return ("VIGILAR ENTRADA", f"Precio en soporte {s} con tendencia alcista, pero el corto plazo todavia no gira (timing {timing['direction'].lower()}). Esperar vela de confirmacion.")
        rr = f"{room_dn:.1f}% al soporte {s}, {room_up:.1f}% a la resistencia {r}" if room_up is not None and room_dn is not None else ""
        return ("MANTENER", f"Tendencia alcista, precio en medio del rango ({rr}). Sin ventaja para entrar ahora: mejor esperar pullback a {s}.")
    if bear:
        if position == "EN SOPORTE":
            return ("NO COMPRAR TODAVIA", f"Tendencia bajista apoyando en soporte {s}. Puede rebotar, pero contra la tendencia. Esperar que recupere {r} para pensar en comprar.")
        if position == "EN RESISTENCIA":
            return ("VENDER / NO ENTRAR", f"Tendencia bajista y precio en resistencia {r}: zona de venta, no de compra." + (" Short posible si el 1H confirma." if allow_short else ""))
        return ("FUERA DEL MERCADO", f"Tendencia bajista, precio en medio del rango. Sin motivo para comprar; el siguiente soporte es {s}.")
    if struct.get("structure") == "COMPRESION":
        return ("ESPERAR RUPTURA", f"Rango que se comprime entre {s} y {r}: se viene un movimiento fuerte. Operar recien cuando rompa con volumen.")
    return ("ESPERAR", f"Sin tendencia definida (sesgo {bias:+d}). Rango entre {s} y {r}: comprar solo en soporte con confirmacion, vender en resistencia.")


def _scenarios(price, lv, regime, ref_tf):
    out = []
    res = lv["resistances"]
    sup = lv["supports"]
    if res:
        r1 = res[0]
        nxt = res[1]["price"] if len(res) > 1 else None
        txt = f"Si rompe y sostiene sobre {_fmt(r1['price'])} ({_lab(r1, True)}, +{r1['distance_pct']:.1f}%)"
        txt += f", el siguiente objetivo es {_fmt(nxt)}" if nxt else ", queda sin resistencias cercanas"
        if "BEAR" in regime:
            txt += "; en tendencia bajista, primero seria un rebote, no un giro, hasta que el 1D cambie"
        out.append({"tipo": "alcista", "texto": txt + "."})
    if sup:
        s1 = sup[0]
        nxt = sup[1]["price"] if len(sup) > 1 else None
        txt = f"Si pierde {_fmt(s1['price'])} ({_lab(s1)}, {s1['distance_pct']:.1f}%)"
        txt += f", el siguiente soporte es {_fmt(nxt)}" if nxt else ", no hay soporte cercano debajo"
        if "BULL" in regime:
            txt += "; en tendencia alcista seria una correccion mas profunda, no un giro, mientras respete el 1D"
        out.append({"tipo": "bajista", "texto": txt + "."})
    return out


def _summary(name, regime, bias, trends, weights, position, sup, res, exh, struct, action, signal, confidence, funding):
    tfl = " / ".join(f"{tf.upper()} {trends[tf]['label'].lower()}" for tf in weights if tf in trends)
    f = []
    f.append(f"{name}: regimen {regime} (sesgo {bias:+d}) — {tfl}.")
    if struct.get("structure") in ("ALCISTA", "BAJISTA"):
        f.append(f"La estructura del marco principal hace {'maximos y minimos crecientes' if struct['structure']=='ALCISTA' else 'maximos y minimos decrecientes'}" +
                 (f", con ruptura {struct['bos'].lower()} confirmada" if struct.get("bos") else "") + ".")
    elif struct.get("structure") == "COMPRESION":
        f.append("La estructura se esta comprimiendo (maximos mas bajos y minimos mas altos): se acerca una ruptura.")
    if sup and res:
        f.append(f"Precio {position.lower()}: soporte {_lab(sup)} en {_fmt(sup['price'])} ({sup['distance_pct']:+.1f}%) y resistencia {_lab(res, True)} en {_fmt(res['price'])} ({res['distance_pct']:+.1f}%).")
    if exh["reversing"]:
        f.append(f"Atencion: agotamiento {exh['score']}/100, posible giro {exh['direction'].lower()}.")
    if funding.get("available") and funding.get("impact") != "NEUTRAL":
        f.append(f"Funding {funding['rate']:+.3f}%: {funding['sentiment'].lower()}.")
    if signal != "NEUTRAL":
        f.append(f"Senal {signal} ({confidence.lower()}). Accion: {action}.")
    else:
        f.append(f"Accion sugerida: {action}.")
    return " ".join(f)


# ── Correlacion con BTC (solo cripto) ────────────────────────────────────────

def apply_btc_filter(results: list) -> list:
    btc = next((r for r in results if r.get("name") == "BTC" and not r.get("error")), None)
    if not btc:
        return results
    for r in results:
        if r.get("error") or r["name"] == "BTC":
            continue
        if btc["bias"] <= -35 and r["signal"] == "LONG" and r["confidence"] != "BAJA":
            r["confidence"] = "BAJA"; r["warnings"].append("BTC en tendencia bajista: los alts rara vez suben solos")
        if btc["bias"] >= 35 and r["signal"] == "SHORT" and r["confidence"] != "BAJA":
            r["confidence"] = "BAJA"; r["warnings"].append("BTC en tendencia alcista: shorts en alts con poco recorrido")
    return results


def build_global_summary(results: list) -> dict:
    valid = [r for r in results if not r.get("error")]
    if not valid:
        return {"headline": "Sin datos", "detail": "No hay analisis disponible."}
    btc = next((r for r in valid if r["name"] == "BTC"), None)
    n_up = sum(r["bias"] >= 15 for r in valid)
    n_dn = sum(r["bias"] <= -15 for r in valid)
    n = len(valid)
    if n_dn >= max(3, n * 0.6):
        headline = "Mercado predominantemente bajista"
    elif n_up >= max(3, n * 0.6):
        headline = "Mercado predominantemente alcista"
    else:
        headline = "Mercado mixto / en transicion"
    parts = []
    if btc:
        parts.append(f"BTC, que marca el ritmo, esta en regimen {btc['regime']} (sesgo {btc['bias']:+d}), {btc['position'].lower()}.")
    parts.append(f"De {n} activos: {n_up} alcistas, {n_dn} bajistas, {n - n_up - n_dn} sin tendencia.")
    sig = [f"{r['name']} {r['signal']}" for r in valid if r["signal"] != "NEUTRAL"]
    parts.append("Senales activas: " + ", ".join(sig) + "." if sig else "Sin senales de entrada ahora: no hay setups con ventaja clara.")
    return {"headline": headline, "detail": " ".join(parts)}


# ── Entrada de alto nivel para cripto ────────────────────────────────────────

def analyze_crypto(name: str, dfs: dict, funding: dict | None, capital: float, risk_pct: float) -> dict:
    frames = prepare_frames(dfs)
    return analyze_frames(name, frames, "crypto", True, funding, capital, risk_pct)
