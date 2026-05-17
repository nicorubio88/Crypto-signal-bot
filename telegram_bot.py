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


def format_signal_message(result: dict) -> str:
    """Formatea el mensaje de señal para Telegram — sin emojis ni HTML."""
    signal = result["signal"]
    name = result["name"]
    price = result["price"]
    score = result["score"]
    max_s = result["max_score"]
    trend = result["trend_1d"]
    levels = result["levels"]

    is_long  = "LONG" in signal
    is_short = "SHORT" in signal

    if is_long:
        sl = levels["stop_long"]
        tp1 = levels["tp1_long"]
        tp2 = levels["tp2_long"]
        direccion = "LONG"
    elif is_short:
        sl = levels["stop_short"]
        tp1 = levels["tp1_short"]
        tp2 = levels["tp2_short"]
        direccion = "SHORT"
    else:
        sl = tp1 = tp2 = None
        direccion = "NEUTRAL"

    lines = [
        f"=== SENAL {direccion} - {name}/USDT ===",
        f"Score: {score}/{max_s} | Precio: ${price:,}",
        f"Tendencia 1D: {trend}",
        f"RSI(6): {result['rsi6']} | RSI(20): {result['rsi20']}",
        f"MACD hist: {result['macd_hist']}",
        "",
        "Condiciones:",
    ]

    for cond, val in result["conditions"].items():
        estado = "SI" if val else "NO"
        lines.append(f"  [{estado}] {cond}")

    if result.get("rsi6_oversold"):
        lines.append("")
        lines.append("AVISO: RSI(6) en sobreventa - posible rebote")
    if result.get("rsi6_overbought"):
        lines.append("")
        lines.append("AVISO: RSI(6) en sobrecompra - posible correccion")

    if sl:
        lines.append("")
        lines.append(f"Stop Loss: ${sl:,}")
        lines.append(f"TP1: ${tp1:,}")
        lines.append(f"TP2: ${tp2:,}")

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
