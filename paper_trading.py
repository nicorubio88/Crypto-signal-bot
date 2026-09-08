"""
Paper trading automatico v4.

El bot abre operaciones FICTICIAS con cada senal (entrada, stop y objetivos
del plan basado en niveles) y las cierra cuando:
  1. La vela toca el stop o el TP1 (se chequea con HIGH/LOW, no solo cierre;
     si una misma vela toca ambos se asume stop primero: conservador).
  2. El bot emite senal contraria.
  3. Detector de agotamiento marca giro en contra (score >= 60).
  4. Vence el tiempo maximo (48 h cripto / 15 dias acciones).

Mide el resultado neto de fees (0.05 % por lado).
"""

import sqlite3
from pathlib import Path
from datetime import datetime, timezone

DB_PATH = Path(__file__).parent / "data" / "paper_trades.db"
FEE_PCT = 0.05
MAX_HOURS = {"crypto": 48, "stock": 24 * 15}


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS paper_v4 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset TEXT NOT NULL, asset_type TEXT NOT NULL DEFAULT 'crypto',
            direction TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN',
            setup TEXT, confidence TEXT, regime TEXT, bias INTEGER,
            entry_price REAL NOT NULL, stop_loss REAL, take_profit REAL, tp2 REAL,
            exit_price REAL, exit_reason TEXT, pnl_pct REAL, pnl_pct_net REAL, r_multiple REAL,
            best_price REAL, opened_at TEXT NOT NULL, closed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_p4_status ON paper_v4(status);
        CREATE INDEX IF NOT EXISTS idx_p4_asset ON paper_v4(asset);
    """)
    conn.commit()
    conn.close()


def has_open(asset: str) -> bool:
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) n FROM paper_v4 WHERE asset=? AND status='OPEN'", (asset,)).fetchone()["n"]
    conn.close()
    return n > 0


def open_trade(r: dict) -> dict:
    plan = r.get("plan")
    if r.get("signal") not in ("LONG", "SHORT") or not plan or plan.get("rejected"):
        return {"ok": False, "error": "sin plan operable"}
    asset = r.get("ticker") or r["name"]
    if has_open(asset):
        return {"ok": False, "error": "ya hay paper abierto"}
    conn = get_conn()
    conn.execute("""
        INSERT INTO paper_v4 (asset, asset_type, direction, setup, confidence, regime, bias,
            entry_price, stop_loss, take_profit, tp2, best_price, opened_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (asset, r.get("asset_type", "crypto"), r["signal"], r.get("setup"), r.get("confidence"),
          r.get("regime"), r.get("bias"), plan["entry"], plan["stop"], plan["tp1"], plan["tp2"],
          plan["entry"], _utcnow().isoformat()))
    conn.commit()
    conn.close()
    return {"ok": True}


def _close(conn, t, exit_price, reason):
    entry, d = t["entry_price"], t["direction"]
    pnl = (exit_price - entry) / entry * 100 if d == "LONG" else (entry - exit_price) / entry * 100
    risk = abs(entry - t["stop_loss"]) if t["stop_loss"] else None
    r_mult = ((exit_price - entry) if d == "LONG" else (entry - exit_price)) / risk if risk else None
    conn.execute("""UPDATE paper_v4 SET status='CLOSED', exit_price=?, exit_reason=?, pnl_pct=?,
                    pnl_pct_net=?, r_multiple=?, closed_at=? WHERE id=?""",
                 (exit_price, reason, round(pnl, 3), round(pnl - 2 * FEE_PCT, 3),
                  round(r_mult, 2) if r_mult is not None else None, _utcnow().isoformat(), t["id"]))


def update_trades(asset: str, high: float, low: float, close: float,
                  current_signal: str | None = None, exhaustion: dict | None = None):
    """Chequea cierres para los paper abiertos del activo con la ultima vela (o tick)."""
    conn = get_conn()
    trades = conn.execute("SELECT * FROM paper_v4 WHERE asset=? AND status='OPEN'", (asset,)).fetchall()
    now = _utcnow()
    for t in trades:
        d, sl, tp = t["direction"], t["stop_loss"], t["take_profit"]
        # 1. Stop / TP con high-low
        if d == "LONG":
            if sl and low <= sl:
                _close(conn, t, sl, "stop_loss"); continue
            if tp and high >= tp:
                _close(conn, t, tp, "take_profit"); continue
        else:
            if sl and high >= sl:
                _close(conn, t, sl, "stop_loss"); continue
            if tp and low <= tp:
                _close(conn, t, tp, "take_profit"); continue
        # 2. Senal contraria
        if current_signal and ((d == "LONG" and current_signal == "SHORT") or (d == "SHORT" and current_signal == "LONG")):
            _close(conn, t, close, "senal_contraria"); continue
        # 3. Agotamiento en contra
        if exhaustion and exhaustion.get("score", 0) >= 60:
            against = (d == "LONG" and exhaustion.get("direction") == "BAJISTA") or \
                      (d == "SHORT" and exhaustion.get("direction") == "ALCISTA")
            if against:
                _close(conn, t, close, "agotamiento"); continue
        # 4. Tiempo maximo
        hours = (now - datetime.fromisoformat(t["opened_at"])).total_seconds() / 3600
        if hours >= MAX_HOURS.get(t["asset_type"], 48):
            _close(conn, t, close, "tiempo_max"); continue
        # best price
        best = t["best_price"] if t["best_price"] is not None else t["entry_price"]
        best = max(best, high) if d == "LONG" else min(best, low)
        conn.execute("UPDATE paper_v4 SET best_price=? WHERE id=?", (best, t["id"]))
    conn.commit()
    conn.close()


def get_stats(asset_type: str = "crypto") -> dict:
    conn = get_conn()
    closed = conn.execute("SELECT * FROM paper_v4 WHERE status='CLOSED' AND asset_type=? ORDER BY closed_at DESC", (asset_type,)).fetchall()
    open_now = conn.execute("SELECT * FROM paper_v4 WHERE status='OPEN' AND asset_type=? ORDER BY opened_at DESC", (asset_type,)).fetchall()
    conn.close()
    n = len(closed)
    wins = [t for t in closed if (t["pnl_pct_net"] or 0) > 0]
    losses = [t for t in closed if (t["pnl_pct_net"] or 0) <= 0]
    reasons = {}
    for t in closed:
        reasons[t["exit_reason"] or "?"] = reasons.get(t["exit_reason"] or "?", 0) + 1
    by_setup = {}
    for s in ("PULLBACK", "BREAKOUT"):
        sub = [t for t in closed if t["setup"] == s]
        if sub:
            by_setup[s] = {"n": len(sub), "win_rate": round(sum(1 for t in sub if (t["pnl_pct_net"] or 0) > 0) / len(sub) * 100, 1),
                           "pnl_net": round(sum(t["pnl_pct_net"] or 0 for t in sub), 2)}
    gross_win = sum(t["pnl_pct_net"] for t in wins)
    gross_loss = -sum(t["pnl_pct_net"] for t in losses)
    return {
        "asset_type": asset_type, "closed_count": n, "open_count": len(open_now),
        "win_rate": round(len(wins) / n * 100, 1) if n else None,
        "total_pnl_net_pct": round(sum(t["pnl_pct_net"] or 0 for t in closed), 2),
        "avg_win_pct": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss_pct": round(-gross_loss / len(losses), 2) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "avg_r": round(sum(t["r_multiple"] or 0 for t in closed) / n, 2) if n else None,
        "by_reason": reasons, "by_setup": by_setup,
        "open_trades": [dict(t) for t in open_now], "recent_closed": [dict(t) for t in closed[:20]],
    }


init_db()
