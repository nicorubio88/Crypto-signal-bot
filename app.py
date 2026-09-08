"""
Dashboard Flask + scheduler — Crypto & Stocks Signal Bot v4

  - Analisis cripto completo cada N horas (config), chequeo rapido cada 2 min.
  - Analisis de acciones/CEDEARs una vez por dia (config) y a pedido.
  - Estado persistente en data/state.json (no re-envia alertas al reiniciar).
  - Autenticacion por token (config "dashboard_token"). Si esta vacio, sin auth
    (solo para uso local).
"""

import json
import threading
from pathlib import Path
from datetime import datetime, timezone

import numpy as np
from flask import Flask, render_template, Response, request, make_response
from apscheduler.schedulers.background import BackgroundScheduler

from data import CRYPTO_SYMBOLS, fetch_crypto_multi_tf, fetch_kraken, fetch_funding_rate
from engine import analyze_crypto, apply_btc_filter, build_global_summary
from stocks import (analyze_watchlist, build_stocks_summary, load_watchlist, add_to_watchlist,
                    update_watchlist_item, remove_from_watchlist)
from telegram_bot import (send_message, format_signal_message, format_action_change_message,
                          format_level_message, format_stocks_digest)
from tracker import save_signal, update_outcomes, get_stats, get_all_signals, get_evolution_report
import paper_trading as paper
from operations import (create_operation, close_operation, update_levels, edit_operation,
                        delete_operation, get_open_operations, get_closed_operations,
                        enrich_open_with_market, get_summary_stats)
import backtest as bt

BASE = Path(__file__).parent
CONFIG_PATH = BASE / "config.json"
DATA_DIR = BASE / "data"
CACHE_CRYPTO = DATA_DIR / "last_crypto.json"
CACHE_STOCKS = DATA_DIR / "last_stocks.json"
STATE_PATH = DATA_DIR / "state.json"

app = Flask(__name__)
lock = threading.Lock()

state = {
    "crypto": [], "crypto_summary": None, "crypto_updated": None,
    "stocks": [], "stocks_summary": None, "stocks_updated": None,
    "last_signal": {}, "last_action": {}, "last_level_alert": {}, "op_alerts": {},
    "frames_1h": {},  # ultimo df 1h por activo (para outcomes / paper)
}


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def load_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def sanitize(o):
    if isinstance(o, dict):
        return {k: sanitize(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [sanitize(i) for i in o]
    if isinstance(o, (bool, np.bool_)):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return None if np.isnan(o) else float(o)
    if isinstance(o, float) and o != o:
        return None
    if hasattr(o, "isoformat"):
        return o.isoformat()
    return o


def _json(obj, status=200):
    return Response(json.dumps(sanitize(obj), ensure_ascii=False), status=status, mimetype="application/json")


# ── Estado persistente ────────────────────────────────────────────────────────

def save_state():
    DATA_DIR.mkdir(exist_ok=True)
    persist = {k: state[k] for k in ("last_signal", "last_action", "last_level_alert")}
    persist["op_alerts"] = {str(k): sorted(v) for k, v in state["op_alerts"].items()}
    with open(STATE_PATH, "w") as f:
        json.dump(persist, f)


def load_state():
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH) as f:
                p = json.load(f)
            for k in ("last_signal", "last_action", "last_level_alert"):
                state[k] = p.get(k, {})
            state["op_alerts"] = {int(k): set(v) for k, v in p.get("op_alerts", {}).items()}
        except Exception as e:
            print(f"No se pudo cargar state.json: {e}")
    for path, key, skey, ukey in ((CACHE_CRYPTO, "crypto", "crypto_summary", "crypto_updated"),
                                  (CACHE_STOCKS, "stocks", "stocks_summary", "stocks_updated")):
        if path.exists():
            try:
                with open(path) as f:
                    c = json.load(f)
                state[key], state[skey], state[ukey] = c["results"], c["summary"], c["updated"]
            except Exception:
                pass


# ── Alertas ───────────────────────────────────────────────────────────────────

def _notify_changes(r: dict, key: str):
    """Envia Telegram si cambio la senal, la accion sugerida o llego a un nivel fuerte."""
    signal = r["signal"]
    prev_sig = state["last_signal"].get(key, "NEUTRAL")
    if signal in ("LONG", "SHORT") and signal != prev_sig:
        save_signal(r)
        paper.open_trade(r)
        send_message(format_signal_message(r))
        print(f"  Senal: {key} -> {signal} {r.get('setup')} ({r['confidence']})")
    state["last_signal"][key] = signal

    action = r["action"]
    prev_action = state["last_action"].get(key)
    if prev_action is not None and action != prev_action and signal == "NEUTRAL":
        # cambio de lectura relevante (no avisar en el primer ciclo)
        important = any(w in action for w in ("PROTEGER", "REDUCIR", "ACUMULAR", "VENDER", "ESPERAR EL PISO", "RUPTURA"))
        if important:
            send_message(format_action_change_message(r, prev_action))
            print(f"  Cambio de lectura: {key} {prev_action} -> {action}")
    state["last_action"][key] = action

    # Llegada a nivel fuerte (una vez por nivel)
    lv = r["levels"]
    for kind, lvl in (("S", lv.get("nearest_support")), ("R", lv.get("nearest_resistance"))):
        if lvl and lvl["strength"] >= 60 and abs(lvl["distance_pct"]) <= 1.0:
            tag = f"{kind}:{round(lvl['price'], 2)}"
            if state["last_level_alert"].get(key) != tag:
                send_message(format_level_message(r, kind, lvl))
                state["last_level_alert"][key] = tag


def check_operation_alerts(ops: list):
    for op in ops:
        sent = state["op_alerts"].setdefault(op["id"], set())
        if op["stop_close"] and "stop_warn" not in sent:
            send_message(f"AVISO: {op['asset']} {op['direction']} — stop cercano ({op['dist_stop']}%)\n"
                         f"Precio ${op['current_price']} | Stop ${op['stop_loss']} | PnL {op['pnl_pct']}%")
            sent.add("stop_warn")
        for lvl in ("tp1", "tp2", "tp3"):
            if op.get(f"{lvl}_reached") and lvl not in sent:
                send_message(f"{lvl.upper()} ALCANZADO: {op['asset']} {op['direction']}\n"
                             f"Precio ${op['current_price']} | PnL {op['pnl_pct']}% (${op['pnl_usd']})\nCerrar parcial recomendado")
                sent.add(lvl)
        if op["bot_changed_against"] and "against" not in sent:
            send_message(f"AVISO: {op['asset']} {op['direction']} — el bot paso a {op['bot_current_signal']} (contra tu posicion)\n"
                         f"Lectura: {op.get('bot_action')} | PnL {op['pnl_pct']}%")
            sent.add("against")
        cs = op.get("close_signal") or {}
        if cs.get("urgency") == "ALTA" and "close_high" not in sent:
            send_message(f"CERRAR {op['asset']} {op['direction']} (URGENCIA ALTA)\n" + "\n".join(f"  * {x}" for x in cs["reasons"]) +
                         f"\nPrecio ${op['current_price']} | PnL {op['pnl_pct']}%")
            sent.add("close_high")


# ── Ciclos ────────────────────────────────────────────────────────────────────

def refresh_crypto():
    if not lock.acquire(blocking=False):
        print("refresh_crypto: ya hay un analisis corriendo")
        return
    try:
        cfg = load_config()
        capital, risk = cfg.get("capital_disponible", 10000), cfg.get("risk_pct", 0.02)
        print(f"\n[{_utcnow():%H:%M} UTC] Analisis cripto...")
        results = []
        for name, symbol in CRYPTO_SYMBOLS.items():
            if name not in cfg.get("crypto_assets", list(CRYPTO_SYMBOLS)):
                continue
            try:
                dfs = fetch_crypto_multi_tf(symbol)
                funding = fetch_funding_rate(name)
                r = analyze_crypto(name, dfs, funding, capital, risk)
                state["frames_1h"][name] = dfs["1h"]
            except Exception as e:
                r = {"name": name, "error": str(e)[:200]}
            results.append(r)
        results = apply_btc_filter(results)
        clean = sanitize(results)

        for r in clean:
            if r.get("error"):
                print(f"  {r['name']}: ERROR {r['error']}")
                continue
            df1h = state["frames_1h"].get(r["name"])
            if df1h is not None:
                update_outcomes(r["name"], df1h)
                last = df1h.iloc[-1]
                paper.update_trades(r["name"], float(last["high"]), float(last["low"]), float(last["close"]),
                                    r["signal"], r["exhaustion"])
            _notify_changes(r, r["name"])

        state["crypto"] = clean
        state["crypto_summary"] = build_global_summary(clean)
        state["crypto_updated"] = _utcnow().strftime("%Y-%m-%d %H:%M UTC")
        DATA_DIR.mkdir(exist_ok=True)
        with open(CACHE_CRYPTO, "w") as f:
            json.dump({"results": clean, "summary": state["crypto_summary"], "updated": state["crypto_updated"]}, f)
        save_state()

        ops = get_open_operations()
        if ops:
            check_operation_alerts(enrich_open_with_market(ops, clean))
        print(f"  Cripto listo: {len(clean)} activos")
    finally:
        lock.release()


def refresh_stocks(notify: bool = True):
    cfg = load_config()
    print(f"\n[{_utcnow():%H:%M} UTC] Analisis acciones/CEDEARs...")
    results = sanitize(analyze_watchlist(cfg.get("capital_disponible", 10000), cfg.get("risk_pct", 0.02)))
    for r in results:
        if r.get("error"):
            print(f"  {r.get('ticker')}: ERROR {r['error']}")
            continue
        try:
            from data import fetch_yahoo
            update_outcomes(r["ticker"], fetch_yahoo(r["ticker"], period="3mo", interval="1d"))
        except Exception:
            pass
        if r["signal"] in ("LONG", "SHORT") and state["last_signal"].get(r["ticker"]) != r["signal"]:
            save_signal(r)
            paper.open_trade(r)
        state["last_signal"][r["ticker"]] = r["signal"]
        state["last_action"][r["ticker"]] = r["action"]
    state["stocks"] = results
    state["stocks_summary"] = build_stocks_summary(results)
    state["stocks_updated"] = _utcnow().strftime("%Y-%m-%d %H:%M UTC")
    DATA_DIR.mkdir(exist_ok=True)
    with open(CACHE_STOCKS, "w") as f:
        json.dump({"results": results, "summary": state["stocks_summary"], "updated": state["stocks_updated"]}, f)
    save_state()
    if notify and cfg.get("stocks_telegram_digest", True):
        send_message(format_stocks_digest(results, state["stocks_summary"]))
    print(f"  Acciones listo: {len(results)} tickers")


def check_realtime():
    """Cada 2 min: precios actuales para paper trades y operaciones abiertas."""
    open_ops = get_open_operations()
    open_paper = {t["asset"] for t in paper.get_stats("crypto")["open_trades"]}
    assets = {op["asset"] for op in open_ops} | open_paper
    if not assets:
        return
    snapshots = []
    for asset in assets:
        symbol = CRYPTO_SYMBOLS.get(asset)
        if not symbol:
            continue
        try:
            df = fetch_kraken(symbol, "1h", limit=2)
            last = df.iloc[-1]
            cached = next((r for r in state["crypto"] if r.get("name") == asset and not r.get("error")), {})
            paper.update_trades(asset, float(last["high"]), float(last["low"]), float(last["close"]),
                                cached.get("signal"), cached.get("exhaustion"))
            snap = dict(cached) if cached else {"name": asset}
            snap["price"] = float(last["close"])
            snapshots.append(snap)
        except Exception as e:
            print(f"  Error precio {asset}: {e}")
    if open_ops and snapshots:
        check_operation_alerts(enrich_open_with_market(open_ops, snapshots))
        save_state()


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.before_request
def _auth():
    token = load_config().get("dashboard_token", "")
    if not token:
        return None
    if request.path == "/login" or request.path.startswith("/static/"):
        return None
    provided = request.cookies.get("token") or request.headers.get("X-Token") or request.args.get("token")
    if provided == token:
        return None
    if request.path == "/":
        return render_template("dashboard.html", need_login=True)
    return _json({"error": "no autorizado"}, 401)


@app.route("/login", methods=["POST"])
def login():
    token = load_config().get("dashboard_token", "")
    data = request.get_json(silent=True) or {}
    if data.get("token") == token:
        resp = make_response(_json({"ok": True}))
        resp.set_cookie("token", token, max_age=3600 * 24 * 90, httponly=True, samesite="Lax")
        return resp
    return _json({"ok": False, "error": "token incorrecto"}, 401)


# ── Rutas ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("dashboard.html", need_login=False)


@app.route("/api/data")
def api_data():
    cfg = load_config()
    return _json({"results": state["crypto"], "summary": state["crypto_summary"],
                  "last_update": state["crypto_updated"],
                  "config": {"capital": cfg.get("capital_disponible", 10000), "risk_pct": cfg.get("risk_pct", 0.02)}})


@app.route("/api/refresh")
def api_refresh():
    threading.Thread(target=refresh_crypto, daemon=True).start()
    return _json({"ok": True, "msg": "analisis cripto lanzado en segundo plano"})


@app.route("/api/stocks")
def api_stocks():
    return _json({"results": state["stocks"], "summary": state["stocks_summary"],
                  "last_update": state["stocks_updated"], "watchlist": load_watchlist()})


@app.route("/api/stocks/refresh")
def api_stocks_refresh():
    threading.Thread(target=refresh_stocks, kwargs={"notify": False}, daemon=True).start()
    return _json({"ok": True, "msg": "analisis de acciones lanzado en segundo plano"})


@app.route("/api/watchlist", methods=["GET", "POST"])
def api_watchlist():
    if request.method == "GET":
        return _json(load_watchlist())
    return _json(add_to_watchlist(request.get_json(force=True) or {}))


@app.route("/api/watchlist/<ticker>", methods=["POST", "DELETE"])
def api_watchlist_item(ticker):
    if request.method == "DELETE":
        return _json(remove_from_watchlist(ticker))
    return _json(update_watchlist_item(ticker, request.get_json(force=True) or {}))


@app.route("/api/candles/<asset>")
def api_candles(asset):
    """Velas + niveles para el grafico. ?tf=1h|4h|1d|1w  &type=crypto|stock"""
    tf = request.args.get("tf", "4h")
    typ = request.args.get("type", "crypto")
    try:
        if typ == "crypto":
            symbol = CRYPTO_SYMBOLS.get(asset.upper())
            if not symbol:
                return _json({"error": "activo desconocido"}, 404)
            df = fetch_kraken(symbol, tf if tf in ("1h", "4h", "1d", "1w") else "4h", 400)
        else:
            from data import fetch_yahoo
            period, interval = {"1h": ("3mo", "1h"), "1d": ("2y", "1d"), "1w": ("10y", "1wk")}.get(tf, ("2y", "1d"))
            df = fetch_yahoo(asset, period=period, interval=interval)
        from indicators import add_indicators
        add_indicators(df)
        rows = [{"time": int(t.timestamp()), "open": o, "high": h, "low": l, "close": c, "volume": v,
                 "ema20": None if np.isnan(e20) else e20, "ema50": None if np.isnan(e50) else e50,
                 "ema200": None if np.isnan(e200) else e200}
                for t, o, h, l, c, v, e20, e50, e200 in zip(df.index, df["open"], df["high"], df["low"], df["close"],
                                                           df["volume"], df["EMA20"], df["EMA50"], df["EMA200"])]
        pool = state["crypto"] if typ == "crypto" else state["stocks"]
        key = "name" if typ == "crypto" else "ticker"
        cached = next((r for r in pool if r.get(key, "").upper() == asset.upper() and not r.get("error")), None)
        return _json({"candles": rows, "levels": cached["levels"] if cached else None,
                      "plan": cached.get("plan") if cached else None})
    except Exception as e:
        return _json({"error": str(e)[:200]}, 500)


@app.route("/api/stats")
def api_stats():
    return _json(get_stats(request.args.get("type", "crypto")))


@app.route("/api/evolution")
def api_evolution():
    return _json(get_evolution_report(request.args.get("type", "crypto")))


@app.route("/api/signals")
def api_signals():
    return _json(get_all_signals(150, request.args.get("type")))


@app.route("/api/paper")
def api_paper():
    return _json(paper.get_stats(request.args.get("type", "crypto")))


@app.route("/api/backtest/<asset>")
def api_backtest(asset):
    try:
        return _json(bt.run_backtest_asset(asset.upper(), source=request.args.get("source", "yahoo")))
    except Exception as e:
        return _json({"error": str(e)[:300]}, 500)


# Operaciones manuales
@app.route("/api/operations/open")
def api_ops_open():
    return _json(enrich_open_with_market(get_open_operations(), state["crypto"]))


@app.route("/api/operations/closed")
def api_ops_closed():
    return _json(get_closed_operations(50))


@app.route("/api/operations/summary")
def api_ops_summary():
    return _json(get_summary_stats())


@app.route("/api/operations/create", methods=["POST"])
def api_op_create():
    return _json(create_operation(request.get_json(force=True)))


@app.route("/api/operations/<int:op_id>/close", methods=["POST"])
def api_op_close(op_id):
    d = request.get_json(force=True)
    res = close_operation(op_id, float(d["exit_price"]), d.get("reason", "manual"))
    state["op_alerts"].pop(op_id, None)
    return _json(res)


@app.route("/api/operations/<int:op_id>/update", methods=["POST"])
def api_op_update(op_id):
    d = request.get_json(force=True)
    return _json(update_levels(op_id, **{k: float(d[k]) for k in ("stop_loss", "tp1", "tp2", "tp3") if d.get(k)}))


@app.route("/api/operations/<int:op_id>/edit", methods=["POST"])
def api_op_edit(op_id):
    return _json(edit_operation(op_id, request.get_json(force=True)))


@app.route("/api/operations/<int:op_id>/delete", methods=["POST"])
def api_op_delete(op_id):
    state["op_alerts"].pop(op_id, None)
    return _json(delete_operation(op_id))


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    load_state()
    cfg = load_config()
    if not cfg.get("dashboard_token"):
        print("AVISO: dashboard sin token (config dashboard_token). Solo para uso local.")
    threading.Thread(target=refresh_crypto, daemon=True).start()
    if cfg.get("stocks_enabled", True) and not state["stocks"]:
        threading.Thread(target=refresh_stocks, kwargs={"notify": False}, daemon=True).start()

    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(refresh_crypto, "interval", hours=cfg.get("full_analysis_interval_hours", 4), id="crypto")
    sched.add_job(check_realtime, "interval", minutes=cfg.get("ops_check_interval_minutes", 2), id="realtime")
    if cfg.get("stocks_enabled", True):
        # Un analisis de acciones tras el cierre de Wall Street (21:30 UTC) y otro a mitad de rueda
        for hh in cfg.get("stocks_hours_utc", [15, 21]):
            sched.add_job(refresh_stocks, "cron", hour=hh, minute=35, id=f"stocks_{hh}")
    sched.start()
    host = cfg.get("host", "127.0.0.1")
    print(f"Dashboard en http://{host}:{cfg.get('port', 5000)}")
    app.run(host=host, port=cfg.get("port", 5000), debug=False, threaded=True)
