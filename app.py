"""
Dashboard web v2.1 con gestion de operaciones
"""

import json
import numpy as np
from flask import Flask, render_template, Response, request
from apscheduler.schedulers.background import BackgroundScheduler
from engine import run_analysis, build_global_summary
from telegram_bot import notify_if_signal, format_signal_message, send_message, format_regime_change_message, format_reversal_message, format_setup_message
from tracker import save_signal, update_outcomes, get_stats, get_all_signals, get_evolution_report
import paper_trading as paper
from operations import (
    create_operation, close_operation, update_levels,
    edit_operation, delete_operation,
    get_open_operations, get_closed_operations,
    enrich_open_with_market, get_summary_stats
)
from pathlib import Path
from datetime import datetime

app = Flask(__name__)

CONFIG_PATH = Path(__file__).parent / "config.json"
CACHE_PATH  = Path(__file__).parent / "data" / "last_results.json"

state = {
    "results": [],
    "last_update": None,
    "global_summary": None,  # resumen global del mercado (4 activos)
    "last_signals": {},
    "last_regimes": {},  # tracking de regimen anterior por activo (cambio de regimen)
    "last_reversal": {},  # tracking de alerta de giro ya enviada por activo
    "last_setup": {},  # tracking de setup de corto plazo por activo
    "last_signals_v2": {},  # tracking de señal v2 por activo (metodo Agustin)
    "operation_alerts_sent": {},  # tracking de alertas ya enviadas por operacion
}


def load_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def sanitize(obj):
    if isinstance(obj, dict):  return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):  return [sanitize(i) for i in obj]
    if isinstance(obj, (bool, np.bool_)): return bool(obj)
    if isinstance(obj, (np.integer,)): return int(obj)
    if isinstance(obj, (np.floating,)): return float(obj)
    if hasattr(obj, 'item'): return obj.item()
    return obj


def check_operation_alerts(operations: list):
    """Envia alertas Telegram para operaciones abiertas."""
    for op in operations:
        op_id = op["id"]
        sent = state["operation_alerts_sent"].setdefault(op_id, set())

        # Stop cercano (1.5%)
        if op["stop_close"] and "stop_warn" not in sent:
            msg = (f"AVISO: {op['asset']} {op['direction']}\n"
                   f"Stop Loss cercano ({op['dist_stop']}%)\n"
                   f"Precio: ${op['current_price']} | Stop: ${op['stop_loss']}\n"
                   f"PnL actual: {op['pnl_pct']}%")
            send_message(msg)
            sent.add("stop_warn")

        # TPs alcanzados
        for level in ["tp1", "tp2", "tp3"]:
            key = f"{level}_reached"
            if op.get(key) and level not in sent:
                msg = (f"TP {level.upper()} ALCANZADO: {op['asset']} {op['direction']}\n"
                       f"Precio: ${op['current_price']} | {level.upper()}: ${op[level]}\n"
                       f"PnL: {op['pnl_pct']}% (${op['pnl_usd']})\n"
                       f"Cerrar parcial recomendado")
                send_message(msg)
                sent.add(level)

        # Bot cambio contra la posicion
        if op["bot_changed_against"] and "bot_against" not in sent:
            msg = (f"AVISO: {op['asset']} {op['direction']}\n"
                   f"Bot cambio a {op['bot_current_signal']} - contra tu posicion\n"
                   f"Score: {op['bot_current_score']} | Confianza: {op['bot_current_confidence']}\n"
                   f"PnL actual: {op['pnl_pct']}%")
            send_message(msg)
            sent.add("bot_against")

        # Señal de cierre con alta urgencia
        cs = op.get("close_signal") or {}
        if cs.get("urgency") == "ALTA" and "close_high" not in sent:
            msg = (f"CERRAR {op['asset']} {op['direction']} (URGENCIA ALTA)\n"
                   f"Razones: {', '.join(cs.get('reasons', []))}\n"
                   f"Precio: ${op['current_price']} | PnL: {op['pnl_pct']}%")
            send_message(msg)
            sent.add("close_high")


def regime_direction(regime: str) -> str:
    """Agrupa los 9 regimenes en 3 direcciones para detectar cambios significativos."""
    if not regime:
        return "NEUTRO"
    if "BULL" in regime:
        return "ALCISTA"
    if "BEAR" in regime:
        return "BAJISTA"
    return "NEUTRO"  # LATERAL, LATERAL ESTRICTO, TRANSICIÓN


def refresh_data():
    """Analisis completo: indicadores + scoring + señales. Ciclo lento (4h)."""
    print(f"\n[{datetime.now().strftime('%H:%M UTC')}] Actualizando datos...")
    results = run_analysis()
    clean = sanitize(results)

    for r in clean:
        if r.get("error"): continue
        update_outcomes(r["name"], r["price"])
        # Paper trading v1 (sistema actual): actualizar abiertos y abrir nuevos
        paper.update_paper_trades(r["name"], r["price"], r.get("signal"), r.get("reversal"), version="v1")
        name = r["name"]
        signal = r["signal"]
        prev = state["last_signals"].get(name, "")
        is_actionable = "LONG" in signal or "SHORT" in signal
        changed = signal != prev
        if is_actionable and changed:
            save_signal(r)
            paper.open_paper_trade(r, version="v1")  # abrir operacion ficticia siguiendo la señal
            send_message(format_signal_message(r))
            print(f"  Nueva señal: {name} -> {signal}")
            state["last_signals"][name] = signal

        # ── Paper trading v2 (metodo Agustin): corre EN PARALELO, mismo mercado ──
        # Usa los mismos niveles SL/TP (misma gestion de riesgo) para aislar
        # la comparacion a la logica de señal de entrada.
        v2 = r.get("v2", {})
        v2_signal = v2.get("signal", "NEUTRAL")
        paper.update_paper_trades(name, r["price"], v2_signal, r.get("reversal"), version="v2")
        prev_v2 = state["last_signals_v2"].get(name, "")
        if ("LONG" in v2_signal or "SHORT" in v2_signal) and v2_signal != prev_v2:
            v2_result = {**r, "signal": v2_signal, "score": v2.get("score"),
                        "confidence": v2.get("confidence")}
            paper.open_paper_trade(v2_result, version="v2")
            print(f"  Nueva señal v2: {name} -> {v2_signal}")
        state["last_signals_v2"][name] = v2_signal

        # ── Alerta general de cambio de regimen (con o sin posicion abierta) ──
        regime = r.get("regime", "")
        prev_regime = state["last_regimes"].get(name)
        new_dir = regime_direction(regime)
        prev_dir = regime_direction(prev_regime) if prev_regime else None

        # Solo avisa si la DIRECCION cambio (evita ruido tipo BEAR -> BEAR DÉBIL).
        # No avisa en el primer ciclo (prev_regime None) para no spammear al arrancar.
        if prev_regime is not None and prev_dir != new_dir:
            send_message(format_regime_change_message(r, prev_regime))
            print(f"  Cambio de regimen: {name} {prev_regime} -> {regime}")

        state["last_regimes"][name] = regime

        # ── Alerta de posible giro de tendencia (agotamiento detectado) ──
        rev = r.get("reversal", {})
        if rev.get("reversing"):
            # Solo avisa una vez por episodio: si antes no estaba reversing
            was_reversing = state["last_reversal"].get(name, False)
            if not was_reversing:
                send_message(format_reversal_message(r))
                print(f"  Posible giro: {name} -> {rev.get('direction')} (score {rev.get('reversal_score')})")
            state["last_reversal"][name] = True
        else:
            state["last_reversal"][name] = False

        # ── Alerta de setup de corto plazo (timing 1H: rebote / continuacion) ──
        st = r.get("short_setup", {})
        setup = st.get("setup")
        if setup in ("REBOTE", "CONTINUACION"):
            # avisar solo cuando cambia el setup (no repetir el mismo cada ciclo)
            prev_setup = state["last_setup"].get(name)
            if setup != prev_setup:
                send_message(format_setup_message(r))
                print(f"  Setup 1H: {name} -> {setup} ({st.get('tipo')})")
            state["last_setup"][name] = setup
        else:
            state["last_setup"][name] = None

    CACHE_PATH.parent.mkdir(exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(clean, f, indent=2)

    state["results"] = clean
    state["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    state["global_summary"] = build_global_summary(clean)

    # Chequear operaciones abiertas y enviar alertas
    open_ops = get_open_operations()
    if open_ops:
        enriched = enrich_open_with_market(open_ops, clean)
        check_operation_alerts(enriched)

    print(f"  Analisis completo — {len(results)} activos | {len(open_ops)} ops abiertas")


def check_paper_realtime():
    """Chequeo rapido de SL/TP de paper trades (v1 y v2), corre siempre."""
    open_v1 = paper.get_paper_stats("v1")["open_trades"]
    open_v2 = paper.get_paper_stats("v2")["open_trades"]
    if not open_v1 and not open_v2:
        return
    try:
        from engine import SYMBOLS, fetch_candles
        assets = {t["asset"] for t in open_v1} | {t["asset"] for t in open_v2}
        for asset in assets:
            symbol = SYMBOLS.get(asset)
            if not symbol:
                continue
            try:
                df = fetch_candles(symbol, "1h", limit=2)
                last_price = float(df.iloc[-1]["close"])
                cached = next((r for r in state["results"] if r.get("name") == asset), {})
                paper.update_paper_trades(asset, last_price, cached.get("signal"),
                                          cached.get("reversal"), version="v1")
                v2 = cached.get("v2", {})
                paper.update_paper_trades(asset, last_price, v2.get("signal"),
                                          cached.get("reversal"), version="v2")
            except Exception as e:
                print(f"  Error paper check {asset}: {e}")
    except Exception as e:
        print(f"  Error en check_paper_realtime: {e}")


def check_operations_realtime():
    """
    Chequeo rapido cada 2 min para alertas de SL/TP/liquidacion.
    Usa precios actuales de Kraken sin recalcular indicadores.
    """
    check_paper_realtime()  # paper trades primero (independiente de ops manuales)
    open_ops = get_open_operations()
    if not open_ops:
        return  # nada mas que chequear

    try:
        # Fetch solo precios actuales (1 vela 1h, mucho mas rapido que analisis completo)
        from engine import SYMBOLS, fetch_candles
        market_snapshots = []

        for op in open_ops:
            asset = op["asset"]
            if not any(s["name"] == asset for s in market_snapshots):
                symbol = SYMBOLS.get(asset)
                if not symbol: continue
                try:
                    df = fetch_candles(symbol, "1h", limit=2)
                    last_price = float(df.iloc[-1]["close"])
                    # Buscar datos del bot en cache (signal/score actual)
                    cached = next((r for r in state["results"] if r.get("name") == asset), {})
                    market_snapshots.append({
                        "name": asset,
                        "price": last_price,
                        "signal": cached.get("signal", "NEUTRAL"),
                        "score": cached.get("score", 0),
                        "confidence": cached.get("confidence", "—"),
                        "close_long": cached.get("close_long", {"should_close": False}),
                        "close_short": cached.get("close_short", {"should_close": False}),
                    })
                except Exception as e:
                    print(f"  Error fetch precio {asset}: {e}")
                    continue

        if market_snapshots:
            enriched = enrich_open_with_market(open_ops, market_snapshots)
            check_operation_alerts(enriched)
    except Exception as e:
        print(f"  Error en check_operations_realtime: {e}")


# ── Rutas web ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/data")
def api_data():
    # Selector de timeframe (solo para vista). Default 4h = cache del scheduler.
    # 1h o 1d se recalculan en vivo SIN tocar registro de señales ni paper trading.
    tf = request.args.get("tf", "4h")
    if tf not in ("1h", "4h", "1d"):
        tf = "4h"

    if tf == "4h":
        results = state["results"]
        last_update = state["last_update"]
        global_summary = state["global_summary"]
    else:
        # Recalculo on-demand en el marco pedido (tarda unos segundos)
        from engine import run_analysis, build_global_summary
        results = sanitize(run_analysis(main_tf=tf))
        last_update = datetime.now().strftime("%Y-%m-%d %H:%M UTC") + f" (vista {tf})"
        global_summary = build_global_summary(results)

    return Response(json.dumps(sanitize({
        "results": results,
        "last_update": last_update,
        "global_summary": global_summary,
        "timeframe": tf,
        "config": {
            "capital": load_config().get("capital_disponible", 10000),
            "risk_pct": load_config().get("risk_pct", 0.025),
        }
    })), mimetype="application/json")


@app.route("/api/refresh")
def api_refresh():
    refresh_data()
    return Response(json.dumps({"ok": True, "last_update": state["last_update"]}),
                    mimetype="application/json")


@app.route("/api/stats")
def api_stats():
    return Response(json.dumps(sanitize(get_stats())), mimetype="application/json")


@app.route("/api/backtest/<asset>")
def api_backtest(asset):
    """Corre backtest de la logica sobre histrico de Kraken para un activo."""
    import backtest as bt
    from engine import SYMBOLS, fetch_candles, calculate_indicators, calculate_score, get_levels, get_adaptive_threshold
    asset = asset.upper()
    symbol = SYMBOLS.get(asset)
    if not symbol:
        return Response(json.dumps({"error": f"Activo {asset} no reconocido"}), mimetype="application/json")
    try:
        df = fetch_candles(symbol, "4h", limit=720)  # ~120 dias de velas 4h
        df = calculate_indicators(df)
        result = bt.run_backtest(df, calculate_score, get_levels, None, get_adaptive_threshold)
        result["asset"] = asset
        result["candles_analyzed"] = len(df)
        return Response(json.dumps(sanitize(result), indent=2), mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), mimetype="application/json")


@app.route("/api/paper")
def api_paper():
    """Estadisticas del paper trading automatico (validacion sin riesgo). ?version=v1|v2"""
    version = request.args.get("version", "v1")
    if version not in ("v1", "v2"):
        version = "v1"
    return Response(json.dumps(sanitize(paper.get_paper_stats(version)), indent=2),
                    mimetype="application/json")


@app.route("/api/paper/compare")
def api_paper_compare():
    """Compara v1 (sistema actual) vs v2 (metodo Agustin/Joven Inversor)."""
    return Response(json.dumps(sanitize(paper.get_paper_stats_compare()), indent=2),
                    mimetype="application/json")


@app.route("/api/evolution")
def api_evolution():
    """Reporte completo de evolucion del bot — pensado para analisis."""
    return Response(json.dumps(sanitize(get_evolution_report()), indent=2),
                    mimetype="application/json")


@app.route("/api/signals")
def api_signals():
    return Response(json.dumps(sanitize(get_all_signals(100))), mimetype="application/json")


# ── Operaciones ─────────────────────────────────────────────────────────────

@app.route("/api/operations/open")
def api_ops_open():
    ops = get_open_operations()
    enriched = enrich_open_with_market(ops, state["results"])
    return Response(json.dumps(sanitize(enriched)), mimetype="application/json")


@app.route("/api/operations/closed")
def api_ops_closed():
    ops = get_closed_operations(50)
    return Response(json.dumps(sanitize(ops)), mimetype="application/json")


@app.route("/api/operations/summary")
def api_ops_summary():
    return Response(json.dumps(sanitize(get_summary_stats())), mimetype="application/json")


@app.route("/api/operations/create", methods=["POST"])
def api_op_create():
    data = request.json
    # Limpiar tracking de alertas para nuevas operaciones
    result = create_operation(data)
    return Response(json.dumps(result), mimetype="application/json")


@app.route("/api/operations/<int:op_id>/close", methods=["POST"])
def api_op_close(op_id):
    data = request.json
    result = close_operation(op_id, float(data["exit_price"]), data.get("reason", "manual"))
    # Limpiar tracking
    state["operation_alerts_sent"].pop(op_id, None)
    return Response(json.dumps(sanitize(result)), mimetype="application/json")


@app.route("/api/operations/<int:op_id>/update", methods=["POST"])
def api_op_update(op_id):
    data = request.json
    result = update_levels(
        op_id,
        stop_loss=float(data["stop_loss"]) if data.get("stop_loss") else None,
        tp1=float(data["tp1"]) if data.get("tp1") else None,
        tp2=float(data["tp2"]) if data.get("tp2") else None,
        tp3=float(data["tp3"]) if data.get("tp3") else None,
    )
    return Response(json.dumps(result), mimetype="application/json")


@app.route("/api/operations/<int:op_id>/edit", methods=["POST"])
def api_op_edit(op_id):
    """Edita todos los campos de una operacion abierta (corregir errores)."""
    result = edit_operation(op_id, request.json)
    return Response(json.dumps(sanitize(result)), mimetype="application/json")


@app.route("/api/operations/<int:op_id>/delete", methods=["POST"])
def api_op_delete(op_id):
    """Borra una operacion de forma permanente."""
    result = delete_operation(op_id)
    state["operation_alerts_sent"].pop(op_id, None)
    return Response(json.dumps(result), mimetype="application/json")


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if CACHE_PATH.exists():
        try:
            with open(CACHE_PATH) as f:
                state["results"] = json.load(f)
            print("Cache cargado")
        except Exception: pass

    refresh_data()

    cfg = load_config()
    full_interval = cfg.get("full_analysis_interval_hours", 4)
    ops_interval = cfg.get("ops_check_interval_minutes", 2)

    scheduler = BackgroundScheduler()
    scheduler.add_job(refresh_data, "interval", hours=full_interval, id="full_analysis")
    scheduler.add_job(check_operations_realtime, "interval", minutes=ops_interval, id="ops_check")
    scheduler.start()
    print(f"Scheduler activo:")
    print(f"  - Analisis completo cada {full_interval}h")
    print(f"  - Check de operaciones cada {ops_interval}min")
    print("Dashboard en http://localhost:5000\n")
    app.run(host="0.0.0.0", port=5000, debug=False)
