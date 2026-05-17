"""
Sistema de tracking de señales y cálculo de acertividad.
Base de datos SQLite — sin dependencias externas.
"""

import sqlite3
import json
from pathlib import Path
from datetime import datetime, timedelta

DB_PATH = Path(__file__).parent / "data" / "signals.db"


def get_conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Crea las tablas si no existen."""
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            asset       TEXT NOT NULL,
            signal      TEXT NOT NULL,
            score       INTEGER,
            price_entry REAL NOT NULL,
            trend_1d    TEXT,
            rsi6        REAL,
            rsi20       REAL,
            macd_hist   REAL,
            conditions  TEXT,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS outcomes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id   INTEGER NOT NULL,
            asset       TEXT NOT NULL,
            price_entry REAL NOT NULL,
            price_4h    REAL,
            price_24h   REAL,
            price_72h   REAL,
            pct_4h      REAL,
            pct_24h     REAL,
            pct_72h     REAL,
            win_4h      INTEGER,
            win_24h     INTEGER,
            win_72h     INTEGER,
            checked_at  TEXT,
            FOREIGN KEY (signal_id) REFERENCES signals(id)
        );
    """)
    conn.commit()
    conn.close()


# ── Guardar señal ────────────────────────────────────────────────────────────

def save_signal(result: dict) -> int:
    """
    Guarda una señal nueva en la DB.
    Devuelve el ID de la señal guardada.
    """
    conn = get_conn()
    cur = conn.execute("""
        INSERT INTO signals
            (asset, signal, score, price_entry, trend_1d, rsi6, rsi20, macd_hist, conditions, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        result["name"],
        result["signal"],
        result["score"],
        result["price"],
        result.get("trend_1d", ""),
        result.get("rsi6"),
        result.get("rsi20"),
        result.get("macd_hist"),
        json.dumps(result.get("conditions", {})),
        datetime.utcnow().isoformat(),
    ))

    signal_id = cur.lastrowid

    # Crear fila de outcome vacía para completar después
    conn.execute("""
        INSERT INTO outcomes (signal_id, asset, price_entry, checked_at)
        VALUES (?, ?, ?, ?)
    """, (signal_id, result["name"], result["price"], None))

    conn.commit()
    conn.close()
    return signal_id


# ── Actualizar outcomes ──────────────────────────────────────────────────────

def update_outcomes(asset: str, current_price: float):
    """
    Para cada señal pendiente de ese asset, verifica si pasaron 4H/24H/72H
    y calcula el resultado (win/loss).
    """
    conn = get_conn()

    # Traer señales sin outcome completo
    pending = conn.execute("""
        SELECT s.id, s.signal, s.price_entry, s.created_at,
               o.price_4h, o.price_24h, o.price_72h
        FROM signals s
        JOIN outcomes o ON o.signal_id = s.id
        WHERE s.asset = ?
          AND (o.price_72h IS NULL)
        ORDER BY s.created_at DESC
        LIMIT 50
    """, (asset,)).fetchall()

    now = datetime.utcnow()

    for row in pending:
        created = datetime.fromisoformat(row["created_at"])
        elapsed = now - created
        signal_dir = "LONG" if "LONG" in row["signal"] else "SHORT" if "SHORT" in row["signal"] else None

        if not signal_dir:
            continue

        updates = {}

        # 4H — después de 4 horas
        if elapsed >= timedelta(hours=4) and row["price_4h"] is None:
            pct = ((current_price - row["price_entry"]) / row["price_entry"]) * 100
            win = 1 if (signal_dir == "LONG" and pct > 0) or (signal_dir == "SHORT" and pct < 0) else 0
            updates["price_4h"] = current_price
            updates["pct_4h"] = round(pct, 3)
            updates["win_4h"] = win

        # 24H
        if elapsed >= timedelta(hours=24) and row["price_24h"] is None:
            pct = ((current_price - row["price_entry"]) / row["price_entry"]) * 100
            win = 1 if (signal_dir == "LONG" and pct > 0) or (signal_dir == "SHORT" and pct < 0) else 0
            updates["price_24h"] = current_price
            updates["pct_24h"] = round(pct, 3)
            updates["win_24h"] = win

        # 72H
        if elapsed >= timedelta(hours=72) and row["price_72h"] is None:
            pct = ((current_price - row["price_entry"]) / row["price_entry"]) * 100
            win = 1 if (signal_dir == "LONG" and pct > 0) or (signal_dir == "SHORT" and pct < 0) else 0
            updates["price_72h"] = current_price
            updates["pct_72h"] = round(pct, 3)
            updates["win_72h"] = win

        if updates:
            updates["checked_at"] = now.isoformat()
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            vals = list(updates.values()) + [row["id"], asset]
            conn.execute(f"""
                UPDATE outcomes SET {set_clause}
                WHERE signal_id = ? AND asset = ?
            """, vals)

    conn.commit()
    conn.close()


# ── Estadísticas ─────────────────────────────────────────────────────────────

def get_stats() -> list:
    """
    Calcula estadísticas de acertividad por activo.
    """
    conn = get_conn()

    assets = conn.execute("SELECT DISTINCT asset FROM signals").fetchall()
    stats = []

    for row in assets:
        asset = row["asset"]

        total = conn.execute(
            "SELECT COUNT(*) as n FROM signals WHERE asset = ?", (asset,)
        ).fetchone()["n"]

        def win_rate(col):
            r = conn.execute(f"""
                SELECT
                    COUNT(*) as total,
                    SUM({col}) as wins,
                    AVG(ABS(pct_{col.replace('win_','')})) as avg_pct
                FROM outcomes
                WHERE asset = ? AND {col} IS NOT NULL
            """, (asset,)).fetchone()
            if not r or not r["total"]:
                return None
            return {
                "total": r["total"],
                "wins": r["wins"] or 0,
                "rate": round((r["wins"] or 0) / r["total"] * 100, 1),
                "avg_pct": round(r["avg_pct"] or 0, 2),
            }

        # Últimas 10 señales
        recent = conn.execute("""
            SELECT s.signal, s.price_entry, s.created_at, s.score,
                   o.win_4h, o.win_24h, o.win_72h,
                   o.pct_4h, o.pct_24h, o.pct_72h
            FROM signals s
            LEFT JOIN outcomes o ON o.signal_id = s.id
            WHERE s.asset = ?
            ORDER BY s.created_at DESC
            LIMIT 10
        """, (asset,)).fetchall()

        stats.append({
            "asset": asset,
            "total_signals": total,
            "win_4h":  win_rate("win_4h"),
            "win_24h": win_rate("win_24h"),
            "win_72h": win_rate("win_72h"),
            "recent": [dict(r) for r in recent],
        })

    conn.close()
    return stats


def get_all_signals(limit: int = 100) -> list:
    """Trae las últimas N señales con sus outcomes."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT s.id, s.asset, s.signal, s.score, s.price_entry,
               s.trend_1d, s.rsi6, s.rsi20, s.created_at,
               o.price_4h, o.price_24h, o.price_72h,
               o.pct_4h, o.pct_24h, o.pct_72h,
               o.win_4h, o.win_24h, o.win_72h
        FROM signals s
        LEFT JOIN outcomes o ON o.signal_id = s.id
        ORDER BY s.created_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# Inicializar DB al importar
init_db()
