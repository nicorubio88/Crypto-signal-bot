"""
Dashboard web — Flask
Accesible desde browser, se actualiza cada 4 horas
"""

from flask import Flask, render_template, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from engine import run_analysis
from telegram_bot import notify_if_signal
import json
from pathlib import Path
from datetime import datetime

app = Flask(__name__)

# Estado global del bot
state = {
    "results": [],
    "last_update": None,
    "last_signals": {},
}

CACHE_PATH = Path(__file__).parent / "data" / "last_results.json"


def refresh_data():
    """Corre el análisis y actualiza el estado global."""
    print(f"\n[{datetime.utcnow().strftime('%H:%M UTC')}] Actualizando datos...")
    results = run_analysis()

    # Guardar en cache
    CACHE_PATH.parent.mkdir(exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(results, f, indent=2)

    # Notificar por Telegram si hay señales
    for r in results:
        state["last_signals"] = notify_if_signal(r, state["last_signals"])

    state["results"] = results
    state["last_update"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    print(f"  ✅ Análisis completo — {len(results)} activos procesados")


# ── Rutas ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/data")
def api_data():
    """Endpoint JSON que consume el dashboard."""
    return jsonify({
        "results": state["results"],
        "last_update": state["last_update"],
    })


@app.route("/api/refresh")
def api_refresh():
    """Fuerza una actualización manual."""
    refresh_data()
    return jsonify({"ok": True, "last_update": state["last_update"]})


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Cargar cache si existe (para que el dashboard muestre algo al arrancar)
    if CACHE_PATH.exists():
        with open(CACHE_PATH) as f:
            state["results"] = json.load(f)
        print("📂 Cache cargado")

    # Primer análisis al arrancar
    refresh_data()

    # Scheduler — cada 4 horas
    scheduler = BackgroundScheduler()
    scheduler.add_job(refresh_data, "interval", hours=4)
    scheduler.start()
    print("\n⏰ Scheduler activo — actualización cada 4 horas")

    # Servidor web
    print("🌐 Dashboard en http://localhost:5000\n")
    app.run(host="0.0.0.0", port=5000, debug=False)
