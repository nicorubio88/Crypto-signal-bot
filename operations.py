"""
Sistema de gestion de operaciones manuales.
Una posicion activa por activo (BTC, ETH, SOL, XRP).
Solo perpetuos con apalancamiento.
"""

import sqlite3
import json
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).parent / "data" / "operations.db"


def get_conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS operations (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            asset         TEXT NOT NULL,
            direction     TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'OPEN',
            contract_type TEXT NOT NULL DEFAULT 'USDT-M',

            -- Entrada
            entry_price   REAL NOT NULL,
            size_units    REAL NOT NULL,
            size_usd      REAL NOT NULL,
            leverage      INTEGER NOT NULL DEFAULT 1,
            margin_mode   TEXT NOT NULL DEFAULT 'ISOLATED',
            margin_usd    REAL NOT NULL,

            -- Niveles
            stop_loss     REAL,
            tp1           REAL,
            tp2           REAL,
            tp3           REAL,
            liquidation   REAL,

            -- Alertas
            tp1_hit       INTEGER DEFAULT 0,
            tp2_hit       INTEGER DEFAULT 0,
            tp3_hit       INTEGER DEFAULT 0,
            stop_warning_sent INTEGER DEFAULT 0,

            -- Cierre
            exit_price    REAL,
            exit_reason   TEXT,
            pnl_usd       REAL,
            pnl_pct       REAL,

            -- Metadata
            notes         TEXT,
            opened_at     TEXT NOT NULL,
            closed_at     TEXT,

            -- Snapshot del bot al abrir
            bot_signal_at_open    TEXT,
            bot_score_at_open     INTEGER,
            bot_confidence_at_open TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_status ON operations(status);
        CREATE INDEX IF NOT EXISTS idx_asset ON operations(asset);
    """)

    # Migracion: agregar contract_type si la tabla existia sin esa columna
    cursor = conn.execute("PRAGMA table_info(operations)")
    columns = [row[1] for row in cursor.fetchall()]
    if "contract_type" not in columns:
        conn.execute("ALTER TABLE operations ADD COLUMN contract_type TEXT NOT NULL DEFAULT 'USDT-M'")
        print("Migracion DB: columna contract_type agregada")

    conn.commit()
    conn.close()


# ── Calculos financieros ──────────────────────────────────────────────────────

def calc_liquidation_price(entry: float, leverage: int, direction: str,
                            contract_type: str = "USDT-M",
                            maintenance_margin: float = 0.005) -> float:
    """
    Precio de liquidacion aproximado (Binance perpetuos).

    USDT-M (lineal, margen en USDT):
      LONG:  entrada * (1 - 1/leverage + MM)
      SHORT: entrada * (1 + 1/leverage - MM)

    COIN-M (inverso, margen en la propia crypto):
      LONG:  entrada / (1 + 1/leverage - MM)
      SHORT: entrada / (1 - 1/leverage + MM)

    La formula inversa es distinta porque la garantia esta en la crypto, no
    en USDT. Validada contra Binance real (error < 0.5%).
    MM ~ 0.5% para crypto mayor (Binance usa tablas escalonadas por tamaño).
    """
    if leverage <= 0:
        return 0

    if contract_type == "COIN-M":
        if direction == "LONG":
            return entry / (1 + 1/leverage - maintenance_margin)
        return entry / (1 - 1/leverage + maintenance_margin)
    else:
        if direction == "LONG":
            return entry * (1 - 1/leverage + maintenance_margin)
        return entry * (1 + 1/leverage - maintenance_margin)


def calc_pnl(entry: float, current: float, size_units: float,
             direction: str) -> dict:
    """Calcula PnL en USD y %."""
    if direction == "LONG":
        pnl_usd = (current - entry) * size_units
        pnl_pct = ((current - entry) / entry) * 100
    else:
        pnl_usd = (entry - current) * size_units
        pnl_pct = ((entry - current) / entry) * 100
    return {"pnl_usd": round(pnl_usd, 2), "pnl_pct": round(pnl_pct, 2)}


def calc_distance(current: float, target: float) -> float:
    """Distancia porcentual a un nivel."""
    if not target or current <= 0:
        return 0
    return round(((target - current) / current) * 100, 2)


def calc_breakeven(entry: float, direction: str, fee_pct: float = 0.05) -> float:
    """Precio de equilibrio considerando fees (taker 0.05% Binance)."""
    fee = fee_pct / 100 * 2  # entrada + salida
    if direction == "LONG":
        return entry * (1 + fee)
    return entry * (1 - fee)


# ── CRUD operaciones ──────────────────────────────────────────────────────────

def has_open_position(asset: str) -> bool:
    conn = get_conn()
    r = conn.execute(
        "SELECT COUNT(*) as n FROM operations WHERE asset = ? AND status = 'OPEN'",
        (asset,)
    ).fetchone()
    conn.close()
    return r["n"] > 0


def create_operation(data: dict) -> dict:
    """
    Crea nueva operacion. Valida que no haya otra abierta del mismo activo.
    Soporta USDT-M (tamaño en USD) y COIN-M (tamaño en crypto).
    """
    if has_open_position(data["asset"]):
        return {"ok": False, "error": f"Ya tenés una posición abierta de {data['asset']}"}

    entry      = float(data["entry_price"])
    leverage   = int(data.get("leverage", 1))
    contract_type = data.get("contract_type", "USDT-M")

    # Calcular size_units y size_usd segun tipo de contrato
    if contract_type == "COIN-M":
        # Tamaño en crypto (BTC, ETH, SOL, XRP)
        size_units = float(data["size_units"])
        size_usd   = entry * size_units
    else:
        # USDT-M: tamaño en USD
        size_usd   = float(data.get("size_usd", 0))
        size_units = size_usd / entry if entry > 0 else 0

    margin_usd = size_usd / leverage if leverage > 0 else size_usd

    liquidation = calc_liquidation_price(entry, leverage, data["direction"], contract_type)

    conn = get_conn()
    cur = conn.execute("""
        INSERT INTO operations
            (asset, direction, contract_type, entry_price, size_units, size_usd,
             leverage, margin_mode, margin_usd,
             stop_loss, tp1, tp2, tp3, liquidation,
             notes, opened_at,
             bot_signal_at_open, bot_score_at_open, bot_confidence_at_open)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data["asset"],
        data["direction"],
        data.get("contract_type", "USDT-M"),
        entry,
        size_units,
        size_usd,
        leverage,
        data.get("margin_mode", "ISOLATED"),
        margin_usd,
        data.get("stop_loss"),
        data.get("tp1"),
        data.get("tp2"),
        data.get("tp3"),
        liquidation,
        data.get("notes", ""),
        datetime.utcnow().isoformat(),
        data.get("bot_signal", ""),
        data.get("bot_score", 0),
        data.get("bot_confidence", ""),
    ))
    op_id = cur.lastrowid
    conn.commit()
    conn.close()
    return {"ok": True, "id": op_id}


def edit_operation(op_id: int, data: dict) -> dict:
    """
    Edita los campos principales de una operacion ABIERTA y recalcula
    liquidacion, size_usd y margen. Permite corregir errores de carga.
    """
    conn = get_conn()
    op = conn.execute("SELECT * FROM operations WHERE id = ? AND status = 'OPEN'",
                       (op_id,)).fetchone()
    if not op:
        conn.close()
        return {"ok": False, "error": "Operación no encontrada o ya cerrada"}

    op = dict(op)
    direction     = data.get("direction", op["direction"])
    contract_type = data.get("contract_type", op["contract_type"])
    entry         = float(data.get("entry_price", op["entry_price"]))
    leverage      = int(data.get("leverage", op["leverage"]))

    if contract_type == "COIN-M":
        size_units = float(data.get("size_units", op["size_units"]))
        size_usd   = entry * size_units
    else:
        size_usd   = float(data.get("size_usd", op["size_usd"]))
        size_units = size_usd / entry if entry > 0 else 0

    margin_usd  = size_usd / leverage if leverage > 0 else size_usd
    liquidation = calc_liquidation_price(entry, leverage, direction, contract_type)

    conn.execute("""
        UPDATE operations SET
            direction = ?, contract_type = ?, entry_price = ?,
            size_units = ?, size_usd = ?, leverage = ?, margin_usd = ?,
            liquidation = ?, stop_loss = ?, tp1 = ?, tp2 = ?, tp3 = ?, notes = ?
        WHERE id = ? AND status = 'OPEN'
    """, (
        direction, contract_type, entry,
        size_units, size_usd, leverage, margin_usd, liquidation,
        data.get("stop_loss", op["stop_loss"]),
        data.get("tp1", op["tp1"]),
        data.get("tp2", op["tp2"]),
        data.get("tp3", op["tp3"]),
        data.get("notes", op["notes"]),
        op_id,
    ))
    conn.commit()
    conn.close()
    return {"ok": True, "liquidation": round(liquidation, 4)}


def delete_operation(op_id: int) -> dict:
    """Borra una operacion de forma permanente (abierta o cerrada)."""
    conn = get_conn()
    op = conn.execute("SELECT id FROM operations WHERE id = ?", (op_id,)).fetchone()
    if not op:
        conn.close()
        return {"ok": False, "error": "Operación no encontrada"}
    conn.execute("DELETE FROM operations WHERE id = ?", (op_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


def update_levels(op_id: int, stop_loss: float = None, tp1: float = None,
                  tp2: float = None, tp3: float = None) -> dict:
    """Actualiza stop loss y TPs de operacion abierta."""
    conn = get_conn()
    updates = {}
    if stop_loss is not None: updates["stop_loss"] = stop_loss
    if tp1 is not None: updates["tp1"] = tp1
    if tp2 is not None: updates["tp2"] = tp2
    if tp3 is not None: updates["tp3"] = tp3
    if not updates:
        return {"ok": False, "error": "Sin cambios"}
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    vals = list(updates.values()) + [op_id]
    conn.execute(f"UPDATE operations SET {set_clause} WHERE id = ? AND status = 'OPEN'", vals)
    conn.commit()
    conn.close()
    return {"ok": True}


def close_operation(op_id: int, exit_price: float, reason: str = "manual") -> dict:
    """Cierra operacion y calcula PnL final."""
    conn = get_conn()
    op = conn.execute("SELECT * FROM operations WHERE id = ? AND status = 'OPEN'",
                       (op_id,)).fetchone()
    if not op:
        conn.close()
        return {"ok": False, "error": "Operación no encontrada o ya cerrada"}

    pnl = calc_pnl(op["entry_price"], exit_price, op["size_units"], op["direction"])

    conn.execute("""
        UPDATE operations SET
            status = 'CLOSED',
            exit_price = ?,
            exit_reason = ?,
            pnl_usd = ?,
            pnl_pct = ?,
            closed_at = ?
        WHERE id = ?
    """, (exit_price, reason, pnl["pnl_usd"], pnl["pnl_pct"],
          datetime.utcnow().isoformat(), op_id))
    conn.commit()
    conn.close()
    return {"ok": True, "pnl": pnl}


def get_open_operations() -> list:
    """Trae todas las operaciones abiertas."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM operations WHERE status = 'OPEN' ORDER BY opened_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_closed_operations(limit: int = 50) -> list:
    """Trae operaciones cerradas."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM operations WHERE status = 'CLOSED'
        ORDER BY closed_at DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def enrich_open_with_market(operations: list, market_data: list) -> list:
    """
    Enriquece operaciones abiertas con datos de mercado en tiempo real.
    """
    market_map = {r["name"]: r for r in market_data if not r.get("error")}
    enriched = []

    for op in operations:
        asset = op["asset"]
        m = market_map.get(asset, {})
        current = m.get("price", op["entry_price"])

        pnl = calc_pnl(op["entry_price"], current, op["size_units"], op["direction"])
        breakeven = calc_breakeven(op["entry_price"], op["direction"])

        # Distancias en %
        dist_stop = calc_distance(current, op["stop_loss"]) if op["stop_loss"] else None
        dist_tp1  = calc_distance(current, op["tp1"]) if op["tp1"] else None
        dist_tp2  = calc_distance(current, op["tp2"]) if op["tp2"] else None
        dist_tp3  = calc_distance(current, op["tp3"]) if op["tp3"] else None
        dist_liq  = calc_distance(current, op["liquidation"]) if op["liquidation"] else None

        # Tiempo en operacion
        opened    = datetime.fromisoformat(op["opened_at"])
        elapsed   = datetime.utcnow() - opened
        hours     = elapsed.total_seconds() / 3600
        time_str  = f"{int(hours)}h {int((hours % 1) * 60)}m" if hours < 24 else f"{int(hours / 24)}d {int(hours % 24)}h"

        # Estado de TPs
        if op["direction"] == "LONG":
            tp1_reached = op["tp1"] and current >= op["tp1"]
            tp2_reached = op["tp2"] and current >= op["tp2"]
            tp3_reached = op["tp3"] and current >= op["tp3"]
            stop_hit    = op["stop_loss"] and current <= op["stop_loss"]
            stop_close  = op["stop_loss"] and dist_stop and abs(dist_stop) < 1.5
        else:
            tp1_reached = op["tp1"] and current <= op["tp1"]
            tp2_reached = op["tp2"] and current <= op["tp2"]
            tp3_reached = op["tp3"] and current <= op["tp3"]
            stop_hit    = op["stop_loss"] and current >= op["stop_loss"]
            stop_close  = op["stop_loss"] and dist_stop and abs(dist_stop) < 1.5

        # Alertas de cierre alineadas con la posicion
        close_signal = None
        if op["direction"] == "LONG":
            cl = m.get("close_long", {})
            if cl.get("should_close"):
                close_signal = cl
        else:
            cs = m.get("close_short", {})
            if cs.get("should_close"):
                close_signal = cs

        # Cambio de signal del bot
        bot_changed_against = False
        current_signal = m.get("signal", "NEUTRAL")
        if op["direction"] == "LONG" and "SHORT" in current_signal:
            bot_changed_against = True
        elif op["direction"] == "SHORT" and "LONG" in current_signal:
            bot_changed_against = True

        # R/R realizado
        rr_realized = None
        if op["stop_loss"] and op["tp1"]:
            risk = abs(op["entry_price"] - op["stop_loss"])
            current_profit = abs(current - op["entry_price"])
            rr_realized = round(current_profit / risk, 2) if risk > 0 else 0
            if (op["direction"] == "LONG" and current < op["entry_price"]) or \
               (op["direction"] == "SHORT" and current > op["entry_price"]):
                rr_realized = -rr_realized

        enriched.append({
            **dict(op),
            "current_price": current,
            "breakeven": round(breakeven, 4),
            "pnl_usd": pnl["pnl_usd"],
            "pnl_pct": pnl["pnl_pct"],
            "dist_stop": dist_stop,
            "dist_tp1": dist_tp1,
            "dist_tp2": dist_tp2,
            "dist_tp3": dist_tp3,
            "dist_liq": dist_liq,
            "stop_close": bool(stop_close),
            "stop_hit": bool(stop_hit),
            "tp1_reached": bool(tp1_reached),
            "tp2_reached": bool(tp2_reached),
            "tp3_reached": bool(tp3_reached),
            "elapsed": time_str,
            "elapsed_hours": round(hours, 1),
            "close_signal": close_signal,
            "bot_current_signal": current_signal,
            "bot_current_score": m.get("score", 0),
            "bot_current_confidence": m.get("confidence", "—"),
            "bot_changed_against": bot_changed_against,
            "rr_realized": rr_realized,
        })

    return enriched


def get_summary_stats() -> dict:
    """Estadisticas globales de operaciones cerradas."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
               SUM(pnl_usd) as total_pnl,
               AVG(CASE WHEN pnl_usd > 0 THEN pnl_pct END) as avg_win_pct,
               AVG(CASE WHEN pnl_usd <= 0 THEN pnl_pct END) as avg_loss_pct
        FROM operations WHERE status = 'CLOSED'
    """).fetchone()
    conn.close()

    total = rows["total"] or 0
    wins  = rows["wins"] or 0
    return {
        "total":         total,
        "wins":          wins,
        "losses":        total - wins,
        "win_rate":      round(wins / total * 100, 1) if total > 0 else 0,
        "total_pnl":     round(rows["total_pnl"] or 0, 2),
        "avg_win_pct":   round(rows["avg_win_pct"] or 0, 2),
        "avg_loss_pct":  round(rows["avg_loss_pct"] or 0, 2),
    }


# Inicializar DB
init_db()
