"""
Backtest de la logica del bot sobre datos historicos.
Recorre velas pasadas, calcula la señal que el bot HABRIA dado en cada punto,
simula entrar con SL/TP y mide el resultado.

IMPORTANTE: un backtest NO es prediccion. Esta sobreajustado al pasado y no
captura del todo slippage ni condiciones de baja liquidez. Sirve para tener
una referencia historica, no para confiar ciegamente. El paper trading en vivo
es mas honesto porque opera sobre datos que el bot no "vio" antes.
"""

import pandas as pd
from datetime import datetime

FEE_PCT = 0.05  # taker Binance por lado


def run_backtest(df: pd.DataFrame, calculate_score_fn, get_levels_fn,
                 get_signal_fn, get_adaptive_threshold_fn,
                 min_history: int = 210) -> dict:
    """
    Recorre el df vela por vela simulando la operativa del bot.

    Para cada vela (a partir de min_history para tener indicadores validos):
      1. Calcula score y señal con los datos disponibles HASTA esa vela
      2. Si hay señal accionable y no hay trade abierto, entra con SL/TP1
      3. Si hay trade abierto, chequea si la vela toco SL o TP

    Devuelve estadisticas del periodo: nro de trades, win rate, PnL neto.
    """
    trades = []
    open_trade = None

    n = len(df)
    if n < min_history + 10:
        return {"error": f"Datos insuficientes: {n} velas (minimo {min_history+10})"}

    for i in range(min_history, n):
        window = df.iloc[:i+1]  # datos hasta la vela i (incluida)
        bar = df.iloc[i]
        high, low, close = bar["high"], bar["low"], bar["close"]

        # ── Gestionar trade abierto: chequear si la vela toco SL o TP ──
        if open_trade:
            d = open_trade["direction"]
            sl = open_trade["stop"]
            tp = open_trade["tp"]
            exit_price = None
            reason = None
            if d == "LONG":
                # Conservador: si la vela toco ambos, asumimos que toco el stop primero
                if low <= sl:
                    exit_price, reason = sl, "stop"
                elif high >= tp:
                    exit_price, reason = tp, "tp"
            else:  # SHORT
                if high >= sl:
                    exit_price, reason = sl, "stop"
                elif low <= tp:
                    exit_price, reason = tp, "tp"

            if exit_price:
                entry = open_trade["entry"]
                if d == "LONG":
                    pnl = (exit_price - entry) / entry * 100
                else:
                    pnl = (entry - exit_price) / entry * 100
                pnl_net = pnl - FEE_PCT * 2
                trades.append({
                    "direction": d, "entry": entry, "exit": exit_price,
                    "reason": reason, "pnl_pct": round(pnl, 3),
                    "pnl_net": round(pnl_net, 3),
                    "bars_held": i - open_trade["bar"],
                    "entry_time": str(open_trade["time"]),
                })
                open_trade = None

        # ── Buscar nueva entrada si no hay trade abierto ──
        if not open_trade:
            try:
                sc = calculate_score_fn(window)
                score = sc["score"]
                adx = sc.get("adx") or 0
                threshold = get_adaptive_threshold_fn(adx)
                # Señal simple por score vs umbral (sin filtros macro multi-tf,
                # que requieren los otros timeframes no disponibles en un solo df)
                if score >= threshold:
                    direction = "LONG"
                elif score <= -threshold:
                    direction = "SHORT"
                else:
                    direction = None

                if direction:
                    levels = get_levels_fn(window)
                    entry = close
                    if direction == "LONG":
                        sl, tp = levels.get("stop_long"), levels.get("tp1_long")
                    else:
                        sl, tp = levels.get("stop_short"), levels.get("tp1_short")
                    if sl and tp:
                        open_trade = {
                            "direction": direction, "entry": entry,
                            "stop": sl, "tp": tp, "bar": i,
                            "time": window.index[-1] if hasattr(window, "index") else i,
                        }
            except Exception:
                continue  # vela con datos incompletos, saltar

    # ── Estadisticas ──
    n_trades = len(trades)
    if n_trades == 0:
        return {"trades": 0, "note": "No se generaron trades en el periodo",
                "win_rate": None, "total_pnl_net": 0}

    wins = [t for t in trades if t["pnl_net"] > 0]
    losses = [t for t in trades if t["pnl_net"] <= 0]
    total_net = sum(t["pnl_net"] for t in trades)
    longs = [t for t in trades if t["direction"] == "LONG"]
    shorts = [t for t in trades if t["direction"] == "SHORT"]

    return {
        "trades": n_trades,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / n_trades * 100, 1),
        "total_pnl_net_pct": round(total_net, 2),
        "avg_win_pct": round(sum(t["pnl_net"] for t in wins) / len(wins), 2) if wins else None,
        "avg_loss_pct": round(sum(t["pnl_net"] for t in losses) / len(losses), 2) if losses else None,
        "longs": len(longs), "shorts": len(shorts),
        "long_win_rate": round(sum(1 for t in longs if t["pnl_net"]>0)/len(longs)*100,1) if longs else None,
        "short_win_rate": round(sum(1 for t in shorts if t["pnl_net"]>0)/len(shorts)*100,1) if shorts else None,
        "avg_bars_held": round(sum(t["bars_held"] for t in trades)/n_trades,1),
        "trade_list": trades[-30:],
        "warning": "Backtest sobre datos pasados. NO es prediccion ni garantia. "
                   "Sin slippage real. Usar como referencia, no como certeza.",
    }
