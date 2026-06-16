"""
Módulo de alertas por Telegram
Solo envía cuando el score supera el umbral — no spamea
"""

import requests
import json
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def send_message(text: str) -> bool:
    """Envía un mensaje por Telegram. Devuelve True si OK."""
    cfg = load_config()
    token = cfg.get("telegram_token", "")
    chat_id = cfg.get("telegram_chat_id", "")

    if not token or not chat_id:
        print("Telegram no configurado (falta config.json)")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"Error Telegram: {e}")
        return False


def build_executable_block(result: dict) -> str:
    """
    Arma un bloque ACCIONABLE: entrada, stop, objetivos y tamaño ya calculado
    segun el capital y riesgo del config. Pensado para copiar a Binance rapido
    y resolver el desfasaje entre que sale la señal y se ejecuta.
    """
    signal = result.get("signal", "")
    direction = "LONG" if "LONG" in signal else "SHORT" if "SHORT" in signal else None
    if not direction:
        return ""

    levels = result.get("levels", {})
    price = result.get("price", 0)
    if direction == "LONG":
        sl, tp1, tp2, tp3 = levels.get("stop_long"), levels.get("tp1_long"), levels.get("tp2_long"), levels.get("tp3_long")
    else:
        sl, tp1, tp2, tp3 = levels.get("stop_short"), levels.get("tp1_short"), levels.get("tp2_short"), levels.get("tp3_short")
    if sl is None:
        return ""

    cfg = load_config()
    capital  = cfg.get("capital_disponible", 10000)
    risk_pct = cfg.get("risk_pct", 0.025)
    leverage = cfg.get("default_leverage", 3)

    # Calculo de tamaño por riesgo
    stop_distance = abs(price - sl) / price if price > 0 else 0
    risk_amount = capital * risk_pct
    position_usd = risk_amount / stop_distance if stop_distance > 0 else 0
    contracts = position_usd / price if price > 0 else 0
    margin = position_usd / leverage if leverage > 0 else position_usd

    def fp(p):
        if p is None: return "—"
        if abs(p) >= 1000: return f"${p:,.2f}"
        if abs(p) >= 10: return f"${p:.2f}"
        return f"${p:.4f}"

    return "\n".join([
        "",
        "──────────────────",
        f"LISTO PARA EJECUTAR ({direction})",
        f"  Entrada:  {fp(price)}",
        f"  Stop:     {fp(sl)}  (riesgo {round(stop_distance*100,2)}%)",
        f"  TP1 (40%): {fp(tp1)}",
        f"  TP2 (40%): {fp(tp2)}",
        f"  TP3 (20%): {fp(tp3)}",
        "",
        f"  Tamaño: {fp(position_usd)} ({round(contracts,4)} {result.get('name','')})",
        f"  Margen {leverage}x: {fp(margin)}",
        f"  Riesgo asumido: {fp(risk_amount)} ({round(risk_pct*100,1)}% de {fp(capital)})",
        "──────────────────",
    ])


def format_signal_message(result: dict) -> str:
    """Formatea el mensaje de señal para Telegram — sin emojis ni HTML."""
    signal = result["signal"]
    name = result["name"]
    price = result["price"]
    score = result["score"]
    max_s = result["max_score"]
    confidence = result.get("confidence", "—")
    levels = result["levels"]

    is_long  = "LONG" in signal
    is_short = "SHORT" in signal

    if is_long:
        sl = levels["stop_long"]
        tp1 = levels["tp1_long"]
        tp2 = levels["tp2_long"]
        tp3 = levels["tp3_long"]
        direccion = "LONG"
    elif is_short:
        sl = levels["stop_short"]
        tp1 = levels["tp1_short"]
        tp2 = levels["tp2_short"]
        tp3 = levels["tp3_short"]
        direccion = "SHORT"
    else:
        sl = tp1 = tp2 = tp3 = None
        direccion = "NEUTRAL"

    # Formato adaptativo segun magnitud del precio
    def fmt_price(p):
        if p is None: return "—"
        if abs(p) >= 1000: return f"${p:,.2f}"
        elif abs(p) >= 10: return f"${p:.2f}"
        else: return f"${p:.4f}"

    lines = [
        f"=== SENAL {direccion} - {name}/USDT ===",
        f"Confianza: {confidence}",
        f"Score: {score}/{max_s} | Precio: {fmt_price(price)}",
        "",
        f"RÉGIMEN: {result.get('regime', '—')}",
        f"  {result.get('regime_description', '')}",
        f"  Bias: {result.get('regime_bias', '—')}",
        "",
        "TENDENCIAS MULTI-TIMEFRAME:",
        f"  1W: {result.get('trend_1w', '—')}",
        f"  1D: {result.get('trend_1d', '—')}",
        f"  1H: {result.get('trend_1h', '—')} {'(confirma)' if result.get('confirm_1h') else '(no confirma)'}",
        "",
        "INDICADORES CLAVE:",
        f"  ADX: {result.get('adx', '—')} {'(tendencia clara)' if result.get('market_trending') else '(lateral)'}",
        f"  RSI(6): {result['rsi6']} | RSI(20): {result['rsi20']}",
        f"  MACD hist: {result['macd_hist']}",
        f"  OBV trend: {result.get('obv_trend', '—')}",
    ]

    # Funding rate (sentimiento perpetuos)
    funding = result.get("funding", {})
    if funding.get("available"):
        rate = funding.get("rate", 0)
        src = funding.get("source", "")
        src_label = f" [{src}]" if src else ""
        lines.append(f"  Funding: {rate:+.4f}% ({funding.get('sentiment', '—')}){src_label}")

    # Divergencias - alta confiabilidad
    div = result.get("divergence", {})
    obv_div = result.get("obv_divergence", {})
    div_alerts = []
    if div.get("bullish"): div_alerts.append("Divergencia RSI ALCISTA")
    if div.get("bearish"): div_alerts.append("Divergencia RSI BAJISTA")
    if obv_div.get("bullish"): div_alerts.append("Divergencia OBV ALCISTA (acumulacion)")
    if obv_div.get("bearish"): div_alerts.append("Divergencia OBV BAJISTA (distribucion)")

    if div_alerts:
        lines.append("")
        lines.append("DIVERGENCIAS DETECTADAS:")
        for a in div_alerts:
            lines.append(f"  * {a}")

    # Avisos RSI extremos
    if result.get("rsi6_oversold"):
        lines.append("")
        lines.append(f"AVISO: RSI(6)={result['rsi6']} en sobreventa - posible rebote")
    if result.get("rsi6_overbought"):
        lines.append("")
        lines.append(f"AVISO: RSI(6)={result['rsi6']} en sobrecompra - posible correccion")

    # Bollinger Squeeze
    if result.get("bb_squeeze"):
        lines.append("")
        lines.append("AVISO: Bollinger Squeeze - movimiento fuerte proximo")

    # Niveles operativos
    if sl is not None:
        rr = levels.get("rr_ratio", "—")
        lines.append("")
        lines.append("NIVELES OPERATIVOS (perfil medio-agresivo):")
        lines.append(f"  Stop Loss: {fmt_price(sl)}")
        lines.append(f"  TP1 (40%): {fmt_price(tp1)}")
        lines.append(f"  TP2 (40%): {fmt_price(tp2)}")
        lines.append(f"  TP3 (20%): {fmt_price(tp3)}")
        lines.append(f"  R/R ratio: 1:{rr}")

    # Bloque ejecutable: entrada/stop/tamaño listos para copiar a Binance
    exec_block = build_executable_block(result)
    if exec_block:
        lines.append(exec_block)

    lines.append("")
    lines.append(result.get("updated_at", ""))

    return "\n".join(lines)


def format_reversal_message(result: dict) -> str:
    """
    Alerta de posible giro de tendencia (agotamiento detectado).
    Sirve para salir de posiciones a tiempo y para detectar pisos/techos.
    """
    name = result.get("name", "?")
    price = result.get("price", 0)
    rev = result.get("reversal", {})
    regime = result.get("regime", "—")
    direction = rev.get("direction", "—")

    def fmt_price(p):
        if p is None: return "—"
        if abs(p) >= 10: return f"${p:,.2f}"
        return f"${p:,.4f}"

    lines = [
        f"=== POSIBLE GIRO DE TENDENCIA - {name}/USDT ===",
        f"Precio: {fmt_price(price)}",
        f"Fuerza de la señal: {rev.get('reversal_score', 0)}/100",
        "",
        f"Régimen actual: {regime}",
        f"Posible giro hacia: {direction}",
        "",
        "SEÑALES DETECTADAS:",
    ]
    for s in rev.get("signals", []):
        lines.append(f"  * {s}")

    lines.append("")
    if direction == "ALCISTA":
        lines.append("LECTURA: la tendencia bajista muestra agotamiento.")
        lines.append("Si tenés SHORT, considerá proteger ganancias.")
        lines.append("Si esperás un piso para LONG, vigilá confirmación.")
    elif direction == "BAJISTA":
        lines.append("LECTURA: la tendencia alcista muestra agotamiento.")
        lines.append("Si tenés LONG, considerá proteger ganancias.")
        lines.append("Si esperás un techo para SHORT, vigilá confirmación.")

    lines.append("")
    lines.append("Nota: es una señal de alerta temprana, no confirmacion. Vigilá el proximo cierre.")
    lines.append("")
    lines.append(result.get("updated_at", ""))
    return "\n".join(lines)


def format_regime_change_message(result: dict, prev_regime: str) -> str:
    """
    Alerta de cambio de regimen del mercado (con o sin posicion abierta).
    Sirve para monitorear giros del mercado aunque no se opere.
    """
    name = result.get("name", "?")
    price = result.get("price", 0)
    regime = result.get("regime", "—")

    def fmt_price(p):
        if p is None:
            return "—"
        if p >= 10:
            return f"${p:,.2f}"
        return f"${p:,.4f}"

    lines = [
        f"=== CAMBIO DE REGIMEN - {name}/USDT ===",
        f"Precio: {fmt_price(price)}",
        "",
        f"ANTES:  {prev_regime}",
        f"AHORA:  {regime}",
        "",
        result.get("regime_description", ""),
        f"Bias: {result.get('regime_bias', '—')}",
        "",
        "TENDENCIAS:",
        f"  1W: {result.get('trend_1w', '—')} | "
        f"1D: {result.get('trend_1d', '—')} | "
        f"1H: {result.get('trend_1h', '—')}",
        f"  ADX: {result.get('adx', '—')} | Score: {result.get('score', '—')}/{result.get('max_score', 17)}",
    ]

    # Si ademas el bot sugiere cerrar una posicion en esta direccion, avisarlo
    cl = result.get("close_long", {})
    cs = result.get("close_short", {})
    if cl.get("should_close") and cl.get("urgency") in ("MEDIA", "ALTA"):
        lines.append("")
        lines.append(f"AVISO: si tenes LONG, considerar cierre ({cl['urgency']})")
        lines.append("  " + "; ".join(cl.get("reasons", [])))
    if cs.get("should_close") and cs.get("urgency") in ("MEDIA", "ALTA"):
        lines.append("")
        lines.append(f"AVISO: si tenes SHORT, considerar cierre ({cs['urgency']})")
        lines.append("  " + "; ".join(cs.get("reasons", [])))

    lines.append("")
    lines.append(result.get("updated_at", ""))

    return "\n".join(lines)


def notify_if_signal(result: dict, last_signals: dict) -> dict:
    """
    Envía alerta solo si la señal cambió respecto a la última vez.
    """
    if result.get("error"):
        return last_signals

    name = result["name"]
    signal = result["signal"]
    prev_signal = last_signals.get(name, "")

    is_actionable = "LONG" in signal or "SHORT" in signal
    changed = signal != prev_signal

    if is_actionable and changed:
        msg = format_signal_message(result)
        sent = send_message(msg)
        if sent:
            print(f"  Telegram enviado: {name} -> {signal}")
        last_signals[name] = signal

    return last_signals
