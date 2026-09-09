"""
Backtest multi-timeframe del motor v4.

Recorre la historia vela a vela en el marco de referencia (4H cripto / 1D
acciones), reconstruye los otros timeframes SOLO con datos anteriores a esa
vela (sin lookahead), corre analyze_frames y simula el plan (entrada al
precio de cierre, stop/TP1 con high-low, salida por senal contraria, agotamiento
o tiempo maximo). Fees 0.05 % por lado.

Fuente de datos:
  - "yahoo": BTC-USD 1h de 2 anios (se resamplea a 4h/1d/1w) — recomendado.
  - "kraken": solo las ultimas 720 velas (unos 4 meses en 4H).
  - "synthetic": datos generados, solo para probar el codigo.

Un backtest no predice nada: sirve para saber si la logica tiene ventaja
estadistica y para calibrar. El paper trading en vivo es la validacion honesta.
"""

import time
import pandas as pd
from datetime import timedelta

from indicators import resample_ohlcv
from engine import prepare_frames, analyze_frames

FEE_PCT = 0.05
MAX_BARS = {"crypto": 12, "stock": 15}   # 12 velas de 4h = 48h; 15 dias
SPLITS = (0.40, 0.40, 0.20)              # reparto de las salidas parciales


def _pnl(entry, price, direction):
    return (price - entry) / entry * 100 if direction == "LONG" else (entry - price) / entry * 100


def run_backtest(h1: pd.DataFrame, asset_type: str = "crypto", name: str = "BT",
                 step: int = 1, min_bars: int = 250, max_bars: int | None = None,
                 allow_short: bool = True, progress: bool = False) -> dict:
    """
    h1: velas de 1h (cripto) o 1d (acciones) de toda la historia.
    """
    if asset_type == "crypto":
        ref_all = resample_ohlcv(h1, "4h")
        ref_tf = "4h"
    else:
        ref_all = h1
        ref_tf = "1d"
    n = len(ref_all)
    if n < min_bars + 20:
        return {"error": f"historia insuficiente ({n} velas de {ref_tf})"}
    end = n if max_bars is None else min(n, min_bars + max_bars)

    trades, open_trade = [], None
    signals_seen = 0
    t0 = time.time()
    for i in range(min_bars, end, step):
        bar_time = ref_all.index[i]
        bar = ref_all.iloc[i]
        # Datos disponibles: todo lo ANTERIOR a la apertura de la vela i, mas la vela i
        # como "vela actual" (el motor usa iloc[-2] como ultima cerrada).
        ref = ref_all.iloc[max(0, i - 500): i + 1]
        if asset_type == "crypto":
            h = h1[h1.index < bar_time + timedelta(hours=4)].tail(400)
            d = resample_ohlcv(h1[h1.index < bar_time + timedelta(hours=4)], "1D").tail(400)
            w = resample_ohlcv(h1[h1.index < bar_time + timedelta(hours=4)], "W-MON")
            frames_raw = {"1h": h, "4h": ref, "1d": d, "1w": w}
        else:
            d = ref
            w = resample_ohlcv(h1.iloc[: i + 1], "W-MON")
            frames_raw = {"1d": d, "1w": w}

        # ── Gestion del trade abierto con la vela i ──
        # Mismas salidas parciales 40/40/20 que el paper trading en vivo, para que
        # backtest y paper midan exactamente la misma estrategia.
        if open_trade:
            hi, lo, cl = float(bar["high"]), float(bar["low"]), float(bar["close"])
            ot = open_trade
            d_ = ot["direction"]
            stage = ot["stage"]
            restante = sum(SPLITS[stage:])
            golpe_stop = (d_ == "LONG" and lo <= ot["stop"]) or (d_ == "SHORT" and hi >= ot["stop"])
            if golpe_stop:
                motivo = "stop" if stage == 0 else "stop_breakeven" if stage == 1 else "stop_ganancia"
                _record(trades, ot, ot["stop"], motivo, i, restante)
                open_trade = None
            else:
                while ot["stage"] < 3:
                    tp = ot["tps"][ot["stage"]]
                    if not ((d_ == "LONG" and hi >= tp) or (d_ == "SHORT" and lo <= tp)):
                        break
                    if ot["stage"] == 2:
                        _record(trades, ot, tp, "tp_final", i, SPLITS[2])
                        open_trade = None
                        break
                    ot["realized"] += _pnl(ot["entry"], tp, d_) * SPLITS[ot["stage"]]
                    ot["stage"] += 1
                    ot["stop"] = ot["entry"] if ot["stage"] == 1 else ot["tps"][0]
                if open_trade and i - ot["bar"] >= MAX_BARS[asset_type]:
                    _record(trades, ot, cl, "tiempo", i, sum(SPLITS[ot["stage"]:]))
                    open_trade = None

        try:
            frames = prepare_frames(frames_raw)
            r = analyze_frames(name, frames, asset_type, allow_short, None, 10000, 0.02)
        except Exception:
            continue
        if r.get("error"):
            continue

        if open_trade:
            # salida por senal contraria o agotamiento fuerte
            d_ = open_trade["direction"]
            exh = r["exhaustion"]
            if (r["signal"] in ("LONG", "SHORT") and r["signal"] != d_) or \
               (exh["score"] >= 60 and exh["direction"] and exh["direction"] != ("ALCISTA" if d_ == "LONG" else "BAJISTA")):
                _record(trades, open_trade, float(bar["close"]), "senal", i, sum(SPLITS[open_trade["stage"]:]))
                open_trade = None

        if not open_trade and r["signal"] in ("LONG", "SHORT") and r.get("plan") and not r["plan"].get("rejected"):
            signals_seen += 1
            p = r["plan"]
            open_trade = {"direction": r["signal"], "entry": float(bar["close"]), "stop": p["stop"],
                          "tps": [p["tp1"], p["tp2"], p["tp3"]], "stage": 0, "realized": 0.0,
                          "risk0": abs(float(bar["close"]) - p["stop"]) / float(bar["close"]) * 100,
                          "bar": i, "time": str(bar_time)[:16], "setup": r["setup"], "confidence": r["confidence"],
                          "regime": r["regime"], "opportunity": r.get("opportunity")}
        if progress and (i - min_bars) % 200 == 0:
            print(f"  {i}/{end} velas, {len(trades)} trades, {time.time() - t0:.0f}s")

    if open_trade:
        _record(trades, open_trade, float(ref_all["close"].iloc[end - 1]), "abierto_al_final",
                end - 1, sum(SPLITS[open_trade["stage"]:]))

    return _stats(trades, n_bars=end - min_bars, ref_tf=ref_tf, name=name) | {"elapsed_s": round(time.time() - t0, 1)}


def _record(trades, ot, exit_price, reason, bar_i, frac_restante=1.0):
    """Consolida el trade sumando lo ya realizado en las salidas parciales."""
    e, d = ot["entry"], ot["direction"]
    total = ot.get("realized", 0.0) + _pnl(e, exit_price, d) * frac_restante
    risk = ot.get("risk0") or (abs(e - ot["stop"]) / e * 100)
    trades.append({**{k: ot[k] for k in ("direction", "setup", "confidence", "regime", "time")},
                   "opportunity": ot.get("opportunity"),
                   "entry": round(e, 4), "exit": round(exit_price, 4), "reason": reason,
                   "pnl_pct": round(total, 3), "pnl_net": round(total - 2 * FEE_PCT, 3),
                   "r": round(total / risk, 2) if risk else None, "bars": bar_i - ot["bar"]})


def _stats(trades, n_bars, ref_tf, name):
    n = len(trades)
    if n == 0:
        return {"asset": name, "trades": 0, "note": "sin trades en el periodo", "bars": n_bars}
    wins = [t for t in trades if t["pnl_net"] > 0]
    losses = [t for t in trades if t["pnl_net"] <= 0]
    gw = sum(t["pnl_net"] for t in wins)
    gl = -sum(t["pnl_net"] for t in losses)
    eq, peak, mdd = 0.0, 0.0, 0.0
    for t in trades:
        eq += t["pnl_net"]; peak = max(peak, eq); mdd = min(mdd, eq - peak)

    def grp(key):
        out = {}
        for v in sorted({t[key] for t in trades if t[key]}):
            sub = [t for t in trades if t[key] == v]
            out[v] = {"n": len(sub), "win_rate": round(sum(1 for t in sub if t["pnl_net"] > 0) / len(sub) * 100, 1),
                      "pnl_net": round(sum(t["pnl_net"] for t in sub), 2),
                      "avg_r": round(sum(t["r"] or 0 for t in sub) / len(sub), 2)}
        return out

    return {
        "asset": name, "ref_tf": ref_tf, "bars": n_bars, "trades": n,
        "wins": len(wins), "losses": len(losses), "win_rate": round(len(wins) / n * 100, 1),
        "total_pnl_net_pct": round(sum(t["pnl_net"] for t in trades), 2),
        "avg_win_pct": round(gw / len(wins), 2) if wins else None,
        "avg_loss_pct": round(-gl / len(losses), 2) if losses else None,
        "profit_factor": round(gw / gl, 2) if gl > 0 else None,
        "avg_r": round(sum(t["r"] or 0 for t in trades) / n, 2),
        "expectancy_pct": round(sum(t["pnl_net"] for t in trades) / n, 3),
        "max_drawdown_pct": round(mdd, 2),
        "avg_bars_held": round(sum(t["bars"] for t in trades) / n, 1),
        "by_direction": grp("direction"), "by_setup": grp("setup"), "by_confidence": grp("confidence"),
        "by_reason": {r: sum(1 for t in trades if t["reason"] == r) for r in {t["reason"] for t in trades}},
        "trade_list": trades[-40:],
        "warning": "Backtest sobre el pasado, sin slippage. Referencia, no garantia.",
    }


def run_backtest_asset(asset: str, source: str = "yahoo", **kw) -> dict:
    from data import fetch_crypto_history_yahoo, fetch_kraken, CRYPTO_SYMBOLS, synthetic_ohlcv
    if source == "synthetic":
        h1 = synthetic_ohlcv(24 * 400, "1h", 60000, seed=1, regime="mixed")
    elif source == "kraken":
        h1 = fetch_kraken(CRYPTO_SYMBOLS[asset], "1h", 720)
        kw.setdefault("min_bars", 60)
    else:
        h1 = fetch_crypto_history_yahoo(asset)["1h"]
    res = run_backtest(h1, "crypto", asset, **kw)
    res["source"] = source
    return res


if __name__ == "__main__":
    import sys, json
    asset = sys.argv[1] if len(sys.argv) > 1 else "BTC"
    source = sys.argv[2] if len(sys.argv) > 2 else "yahoo"
    print(f"Backtest {asset} ({source})...")
    r = run_backtest_asset(asset, source, progress=True)
    r.pop("trade_list", None)
    print(json.dumps(r, indent=2, ensure_ascii=False))
