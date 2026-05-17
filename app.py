"""
Dashboard web — Flask
Accesible desde browser, se actualiza cada 4 horas
"""

import json
import numpy as np
from flask import Flask, render_template, Response
from apscheduler.schedulers.background import BackgroundScheduler
from engine import run_analysis
from telegram_bot import notify_if_signal
from pathlib import Path
from datetime import datetime

app = Flask(__name__)

state = {
    "results": [],
    "last_update": None,
    "last_signals": {},
}

CACHE_PATH = Path(__file__).parent / "data" / "last_results.json"


def sanitize(obj):
    """Convierte tipos numpy/bool a tipos Python nativos para JSON."""
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(i) for i in obj]
    if isinstance(obj, (bool, np.bool_)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if hasattr(obj, 'item'):
        return obj.item()
    return obj


def refresh_data():
    """Corre el análisis y actualiza el estado global."""
    print(f"\n[{datetime.now().strftime('%H:%M UTC')}] Actualizando datos...")
    results = run_analysis()
    clean = sanitize(results)

    # Guardar en cache
    CACHE_PATH.parent.mkdir(exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(clean, f, indent=2)

    # Notificar por Telegram si hay señales
    for r in clean:
        state["last_signals"] = notify_if_signal(r, state["last_signals"])

    state["results"] = clean
    state["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    print(f"  Analisis completo — {len(results)} activos procesados")


# ── Rutas ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/data")
def api_data():
    payload = sanitize({
        "results": state["results"],
        "last_update": state["last_update"],
    })
    return Response(
        json.dumps(payload),
        mimetype="application/json"
    )


@app.route("/api/refresh")
def api_refresh():
    refresh_data()
    return Response(
        json.dumps({"ok": True, "last_update": state["last_update"]}),
        mimetype="application/json"
    )


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Cargar cache si existe
    if CACHE_PATH.exists():
        try:
            with open(CACHE_PATH) as f:
                state["results"] = json.load(f)
            print("Cache cargado")
        except Exception:
            pass

    # Primer análisis
    refresh_data()

    # Scheduler cada 4 horas
    scheduler = BackgroundScheduler()
    scheduler.add_job(refresh_data, "interval", hours=4)
    scheduler.start()
    print("\nScheduler activo — actualizacion cada 4 horas")
    print("Dashboard en http://localhost:5000\n")

    app.run(host="0.0.0.0", port=5000, debug=False)
