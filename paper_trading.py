"""
Paper trading automatico.
El bot abre operaciones FICTICIAS siguiendo sus propias señales, con SL/TP,
y mide el resultado real de operar esas señales — sin arriesgar plata.

Esto es distinto del tracker (que mide si el precio subio/bajo a 4h/24h/72h).
Aca se simula operativa real: se entra, se pone stop y objetivo, y se cierra
cuando toca uno de los dos o cuando la señal se invierte. Da la validacion
honesta que falta antes de pensar en ejecucion real.
"""

import sqlite3
import json
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).parent / "data" / "paper_trades.db"

# Comisiones realistas (Binance taker ~0.05% por lado, ida + vuelta)
FEE_PCT = 0.05

# ── Reglas de salida (gatillos) ───────────────────────────────────────────────
# Basado en los datos: la ventana buena es ~24H, el 72H se desploma.
MAX_HOURS_OPEN     = 48     # gatillo 2: limite de tiempo (freno duro)
# TRAILING DESACTIVADO (jul-2026): los datos del paper trading mostraron que
# cortaba las ganadoras en +0.9% promedio (ninguna llego a TP) mientras las
# perdedoras iban al stop completo (-3.3%) => PnL total negativo pese a 67% de
# acierto. El backtest sin trailing dio +3.44% con TPs de +2.5/+4.8%.
# Se reactivara solo si nuevos datos lo justifican, con umbrales recalibrados.
TRAILING_ENABLED   = False
TRAILING_GIVEBACK  = 0.40   # (sin efecto mientras TRAILING_ENABLED=False)
MIN_PROFIT_TO_TRAIL = 1.0   # (sin efecto mientras TRAILING_ENABLED=False)


def get_conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS paper_trades (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            asset        TEXT NOT NULL,
            direction    TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'OPEN',

            entry_price  REAL NOT NULL,
            stop_loss    REAL,
            take_profit  REAL,

            exit_price   REAL,
            exit_reason  TEXT,
            pnl_pct      REAL,
            pnl_pct_net  REAL,

            -- contexto del bot al abrir
            score        REAL,
            confidence   TEXT,
            regime       TEXT,
            adx          REAL,

            opened_at    TEXT NOT NULL,
            closed_at    TEXT,
            best_price   REAL,
            version      TEXT NOT NULL DEFAULT 'v1'
        );
        CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_trades(status);
        CREATE INDEX IF NOT EXISTS idx_paper_asset  ON paper_trades(asset);
    """)
    # Migraciones idempotentes para bases de datos creadas ANTES de estas
    # columnas (asi no se rompe si ya tenias paper trades registrados).
    # IMPORTANTE: el ALTER TABLE tiene que ejecutarse ANTES de crear cualquier
    # indice sobre esa columna, o falla con "no such column" en tablas viejas.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(paper_trades)")]
    if "best_price" not in cols:
        conn.execute("ALTER TABLE paper_trades ADD COLUMN best_price REAL")
    if "version" not in cols:
        conn.execute("ALTER TABLE paper_trades ADD COLUMN version TEXT NOT NULL DEFAULT 'v1'")
    # El indice de version se crea DESPUES de garantizar que la columna existe.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_version ON paper_trades(version)")
    conn.commit()
    conn.close()


def has_open_paper(asset: str, version: str = "v1") -> bool:
    conn = get_conn()
    r = conn.execute(
        "SELECT COUNT(*) n FROM paper_trades WHERE asset=? AND status='OPEN' AND version=?",
        (asset, version)
    ).fetchone()
    conn.close()
    return r["n"] > 0


def open_paper_trade(result: dict, version: str = "v1") -> dict:
    """
    Abre una operacion de papel a partir de una señal del bot.
    Usa los niveles de SL/TP del propio analisis (TP1 como objetivo).
    Una sola posicion de papel abierta por activo Y VERSION a la vez
    (v1 y v2 pueden tener posiciones simultaneas del mismo activo, para
    poder compararlas corriendo en paralelo sobre el mismo mercado).
    """
    signal = result.get("signal", "")
    direction = "LONG" if "LONG" in signal else "SHORT" if "SHORT" in signal else None
    if not direction:
        return {"ok": False, "error": "señal no operable"}

    asset = result["name"]
    if has_open_paper(asset, version):
        return {"ok": False, "error": "ya hay paper abierto"}

    entry = result["price"]
    levels = result.get("levels", {})
    if direction == "LONG":
        sl = levels.get("stop_long")
        tp = levels.get("tp1_long")
    else:
        sl = levels.get("stop_short")
        tp = levels.get("tp1_short")

    conn = get_conn()
    conn.execute("""
        INSERT INTO paper_trades
            (asset, direction, entry_price, stop_loss, take_profit,
             score, confidence, regime, adx, opened_at, best_price, version)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        asset, direction, entry, sl, tp,
        result.get("score"), result.get("confidence", ""),
        result.get("regime", ""), result.get("adx"),
        datetime.utcnow().isoformat(), entry, version,
    ))
    conn.commit()
    conn.close()
    return {"ok": True, "asset": asset, "direction": direction,
            "entry": entry, "sl": sl, "tp": tp, "version": version}


def _close(conn, trade, exit_price, reason):
    """Cierra un trade de papel y calcula PnL con y sin fees."""
    entry = trade["entry_price"]
    direction = trade["direction"]
    if direction == "LONG":
        pnl = (exit_price - entry) / entry * 100
    else:
        pnl = (entry - exit_price) / entry * 100
    pnl_net = pnl - (FEE_PCT * 2)  # ida + vuelta

    conn.execute("""
        UPDATE paper_trades SET
            status='CLOSED', exit_price=?, exit_reason=?,
            pnl_pct=?, pnl_pct_net=?, closed_at=?
        WHERE id=?
    """, (exit_price, reason, round(pnl, 3), round(pnl_net, 3),
          datetime.utcnow().isoformat(), trade["id"]))


def update_paper_trades(asset: str, current_price: float, current_signal: str = None,
                        reversal: dict = None, version: str = "v1"):
    """
    Para los paper abiertos del activo Y VERSION, chequea las condiciones de cierre.

    Orden de prioridad de cierre:
      1. Stop loss / Take profit (niveles fijos)
      2. Señal invertida (el bot cambio de opinion)
      3. GATILLO GIRO: detector de reversion en contra de la posicion
      4. GATILLO TIEMPO: la señal lleva mas de MAX_HOURS_OPEN abierta
      5. GATILLO TRAILING: devolvio mucho de la ganancia maxima (desactivado)

    Los gatillos 3-5 atacan el problema medido: las señales envejecen mal
    pasadas ~24-48h. Se busca cerrar antes de la zona donde se desploma el 72H.
    """
    conn = get_conn()
    open_trades = conn.execute(
        "SELECT * FROM paper_trades WHERE asset=? AND status='OPEN' AND version=?",
        (asset, version)
    ).fetchall()

    now = datetime.utcnow()

    for t in open_trades:
        direction = t["direction"]
        entry = t["entry_price"]
        sl = t["stop_loss"]
        tp = t["take_profit"]
        closed = False

        # ── 1. Stop loss / Take profit ──
        if direction == "LONG":
            if sl and current_price <= sl:
                _close(conn, t, sl, "stop_loss"); closed = True
            elif tp and current_price >= tp:
                _close(conn, t, tp, "take_profit"); closed = True
        else:  # SHORT
            if sl and current_price >= sl:
                _close(conn, t, sl, "stop_loss"); closed = True
            elif tp and current_price <= tp:
                _close(conn, t, tp, "take_profit"); closed = True
        if closed:
            continue

        # ── 2. Señal invertida ──
        if current_signal:
            opp = ("SHORT" in current_signal and direction == "LONG") or \
                  ("LONG" in current_signal and direction == "SHORT")
            if opp:
                _close(conn, t, current_price, "señal_invertida")
                continue

        # ── 3. GATILLO GIRO: detector de reversion en contra ──
        # Si tenemos SHORT y el detector marca giro ALCISTA (o LONG y giro BAJISTA)
        if reversal and reversal.get("reversing"):
            rev_dir = reversal.get("direction")
            against = (direction == "SHORT" and rev_dir == "ALCISTA") or \
                      (direction == "LONG" and rev_dir == "BAJISTA")
            if against:
                _close(conn, t, current_price, "giro_tendencia")
                continue

        # ── 4. GATILLO TIEMPO: señal demasiado vieja ──
        opened = datetime.fromisoformat(t["opened_at"])
        hours_open = (now - opened).total_seconds() / 3600
        if hours_open >= MAX_HOURS_OPEN:
            _close(conn, t, current_price, "tiempo_max")
            continue

        # ── 5. GATILLO TRAILING: protege ganancia ──
        # Actualizar el mejor precio alcanzado y chequear si devolvio mucho
        best = t["best_price"] if t["best_price"] is not None else entry
        if direction == "LONG":
            best = max(best, current_price)
            max_profit = (best - entry) / entry * 100
            cur_profit = (current_price - entry) / entry * 100
        else:  # SHORT
            best = min(best, current_price)
            max_profit = (entry - best) / entry * 100
            cur_profit = (entry - current_price) / entry * 100

        # Guardar el mejor precio actualizado (se sigue trackeando para analisis)
        conn.execute("UPDATE paper_trades SET best_price=? WHERE id=?", (best, t["id"]))

        # Trailing solo si esta habilitado (desactivado por evidencia, ver arriba)
        if TRAILING_ENABLED and max_profit >= MIN_PROFIT_TO_TRAIL:
            giveback = (max_profit - cur_profit) / max_profit if max_profit > 0 else 0
            if giveback >= TRAILING_GIVEBACK:
                _close(conn, t, current_price, "trailing")
                continue

    conn.commit()
    conn.close()


def get_paper_stats(version: str = "v1") -> dict:
    """Estadisticas de las operaciones de papel cerradas + abiertas, por version."""
    conn = get_conn()

    closed = conn.execute(
        "SELECT * FROM paper_trades WHERE status='CLOSED' AND version=? ORDER BY closed_at DESC",
        (version,)
    ).fetchall()
    open_now = conn.execute(
        "SELECT * FROM paper_trades WHERE status='OPEN' AND version=? ORDER BY opened_at DESC",
        (version,)
    ).fetchall()

    n = len(closed)
    wins = sum(1 for t in closed if (t["pnl_pct_net"] or 0) > 0)
    total_net = sum((t["pnl_pct_net"] or 0) for t in closed)
    avg_win = [t["pnl_pct_net"] for t in closed if (t["pnl_pct_net"] or 0) > 0]
    avg_loss = [t["pnl_pct_net"] for t in closed if (t["pnl_pct_net"] or 0) <= 0]

    # Desglose por motivo de cierre
    reasons = {}
    for t in closed:
        r = t["exit_reason"] or "?"
        reasons[r] = reasons.get(r, 0) + 1

    conn.close()
    return {
        "version": version,
        "closed_count": n,
        "open_count": len(open_now),
        "win_rate": round(wins / n * 100, 1) if n else None,
        "wins": wins,
        "losses": n - wins,
        "total_pnl_net_pct": round(total_net, 2),
        "avg_win_pct": round(sum(avg_win) / len(avg_win), 2) if avg_win else None,
        "avg_loss_pct": round(sum(avg_loss) / len(avg_loss), 2) if avg_loss else None,
        "by_reason": reasons,
        "open_trades": [dict(t) for t in open_now],
        "recent_closed": [dict(t) for t in closed[:15]],
        "note": "Operativa simulada con SL/TP y fees. Mide el bot operando en vivo, sin riesgo.",
    }


def get_paper_stats_compare() -> dict:
    """
    Compara v1 (sistema actual, 17 indicadores) vs v2 (metodo Agustin/Joven
    Inversor: RSI-1H + divergencia + POC + Fibonacci + VWAP-1H breakout).
    Mismos stops/objetivos en ambas (misma gestion de riesgo) — la unica
    variable que cambia es la logica de generacion de la señal de entrada.
    """
    v1 = get_paper_stats("v1")
    v2 = get_paper_stats("v2")
    return {
        "v1": v1,
        "v2": v2,
        "note": "v1 = sistema actual (17 indicadores). v2 = metodo Agustin "
                "(RSI-1H + divergencia + POC + Fibonacci + VWAP-1H). "
                "Mismos stops/objetivos en las dos — se compara solo la señal de entrada. "
                "Con pocas operaciones cerradas la comparacion no es concluyente todavia.",
    }


init_db()
