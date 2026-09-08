"""
Registro de senales y medicion de acertividad.

Cada senal guarda el contexto completo (setup, regimen, sesgo, niveles, plan)
y se evalua a 3 horizontes usando la VELA HISTORICA correspondiente (no el
precio del momento en que corre el chequeo):
    cripto:   4h / 24h / 72h
    acciones: 1d / 5d / 20d  (en horas: 24 / 120 / 480)

WIN = el precio se movio a favor mas que win_threshold_pct (config, default 0.3 %).
"""

import sqlite3
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pandas as pd

DB_PATH = Path(__file__).parent / "data" / "signals.db"
CONFIG_PATH = Path(__file__).parent / "config.json"

HORIZONS = {"crypto": [4, 24, 72], "stock": [24, 120, 480]}


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _win_threshold() -> float:
    try:
        with open(CONFIG_PATH) as f:
            return float(json.load(f).get("win_threshold_pct", 0.3))
    except Exception:
        return 0.3


def get_conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS signals_v4 (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            asset       TEXT NOT NULL,
            asset_type  TEXT NOT NULL DEFAULT 'crypto',
            signal      TEXT NOT NULL,
            setup       TEXT,
            confidence  TEXT,
            regime      TEXT,
            bias        INTEGER,
            position    TEXT,
            price_entry REAL NOT NULL,
            stop        REAL,
            tp1         REAL,
            rr_tp1      REAL,
            context     TEXT,
            created_at  TEXT NOT NULL,
            h1_hours    INTEGER, h2_hours INTEGER, h3_hours INTEGER,
            price_h1 REAL, price_h2 REAL, price_h3 REAL,
            pct_h1 REAL, pct_h2 REAL, pct_h3 REAL,
            win_h1 INTEGER, win_h2 INTEGER, win_h3 INTEGER,
            checked_at  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_sig4_asset ON signals_v4(asset);
        CREATE INDEX IF NOT EXISTS idx_sig4_created ON signals_v4(created_at);
    """)
    conn.commit()
    conn.close()


def save_signal(r: dict) -> int:
    hz = HORIZONS.get(r.get("asset_type", "crypto"), HORIZONS["crypto"])
    plan = r.get("plan") or {}
    ctx = {
        "reasons": r.get("reasons"), "warnings": r.get("warnings"), "action": r.get("action"),
        "trends": {k: v["label"] for k, v in r.get("trends", {}).items()},
        "nearest_support": (r["levels"].get("nearest_support") or {}).get("price"),
        "nearest_resistance": (r["levels"].get("nearest_resistance") or {}).get("price"),
        "exhaustion": r.get("exhaustion", {}).get("score"),
        "funding": (r.get("funding") or {}).get("rate"),
    }
    conn = get_conn()
    cur = conn.execute("""
        INSERT INTO signals_v4 (asset, asset_type, signal, setup, confidence, regime, bias, position,
            price_entry, stop, tp1, rr_tp1, context, created_at, h1_hours, h2_hours, h3_hours)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (r.get("ticker") or r["name"], r.get("asset_type", "crypto"), r["signal"], r.get("setup"),
          r.get("confidence"), r.get("regime"), r.get("bias"), r.get("position"),
          r["price"], plan.get("stop"), plan.get("tp1"), plan.get("rr_tp1"),
          json.dumps(ctx, default=str), _utcnow().isoformat(), hz[0], hz[1], hz[2]))
    sid = cur.lastrowid
    conn.commit()
    conn.close()
    return sid


def _is_win(pct: float, signal: str, thr: float) -> int:
    if signal == "LONG":
        return int(pct > thr)
    if signal == "SHORT":
        return int(pct < -thr)
    return 0


def _price_at(df: pd.DataFrame, when: datetime) -> float | None:
    """Cierre de la primera vela cuyo tiempo de apertura es >= when."""
    if df is None or len(df) == 0:
        return None
    idx = df.index.searchsorted(when)
    if idx >= len(df):
        return None
    return float(df["close"].iloc[idx])


def update_outcomes(asset: str, df: pd.DataFrame):
    """
    df: velas (1h para cripto, 1d para acciones) con indice datetime.
    Completa los horizontes vencidos usando la vela historica exacta.
    """
    thr = _win_threshold()
    conn = get_conn()
    pending = conn.execute("""
        SELECT * FROM signals_v4 WHERE asset = ? AND price_h3 IS NULL
        ORDER BY created_at DESC LIMIT 100
    """, (asset,)).fetchall()
    now = _utcnow()
    for row in pending:
        created = datetime.fromisoformat(row["created_at"])
        updates = {}
        for h in (1, 2, 3):
            hours = row[f"h{h}_hours"]
            if row[f"price_h{h}"] is not None or hours is None:
                continue
            target = created + timedelta(hours=hours)
            if now < target:
                continue
            p = _price_at(df, target)
            if p is None:
                continue
            pct = (p - row["price_entry"]) / row["price_entry"] * 100
            updates[f"price_h{h}"] = p
            updates[f"pct_h{h}"] = round(pct, 3)
            updates[f"win_h{h}"] = _is_win(pct, row["signal"], thr)
        if updates:
            updates["checked_at"] = now.isoformat()
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(f"UPDATE signals_v4 SET {set_clause} WHERE id = ?",
                         list(updates.values()) + [row["id"]])
    conn.commit()
    conn.close()


def _rate(conn, where: str, params: tuple, h: int):
    r = conn.execute(f"""
        SELECT COUNT(*) total, SUM(win_h{h}) wins, AVG(pct_h{h}) avg_pct,
               AVG(CASE WHEN signal='LONG' THEN pct_h{h} ELSE -pct_h{h} END) avg_fav
        FROM signals_v4 WHERE win_h{h} IS NOT NULL AND {where}
    """, params).fetchone()
    if not r or not r["total"]:
        return None
    return {"total": r["total"], "wins": r["wins"] or 0,
            "rate": round((r["wins"] or 0) / r["total"] * 100, 1),
            "avg_move_favor": round(r["avg_fav"] or 0, 2)}


def get_stats(asset_type: str = "crypto") -> list:
    conn = get_conn()
    hz = HORIZONS[asset_type]
    assets = conn.execute("SELECT DISTINCT asset FROM signals_v4 WHERE asset_type = ?", (asset_type,)).fetchall()
    out = []
    for a in assets:
        asset = a["asset"]
        total = conn.execute("SELECT COUNT(*) n FROM signals_v4 WHERE asset = ?", (asset,)).fetchone()["n"]
        recent = conn.execute("""
            SELECT id, signal, setup, confidence, price_entry, created_at, pct_h1, pct_h2, pct_h3,
                   win_h1, win_h2, win_h3 FROM signals_v4 WHERE asset = ? ORDER BY created_at DESC LIMIT 10
        """, (asset,)).fetchall()
        out.append({"asset": asset, "total_signals": total,
                    "horizons": [f"{h}h" if h < 48 else f"{h // 24}d" for h in hz],
                    "h1": _rate(conn, "asset = ?", (asset,), 1),
                    "h2": _rate(conn, "asset = ?", (asset,), 2),
                    "h3": _rate(conn, "asset = ?", (asset,), 3),
                    "recent": [dict(r) for r in recent]})
    conn.close()
    return out


def get_all_signals(limit: int = 100, asset_type: str | None = None) -> list:
    conn = get_conn()
    q = "SELECT * FROM signals_v4"
    params = ()
    if asset_type:
        q += " WHERE asset_type = ?"
        params = (asset_type,)
    rows = conn.execute(q + " ORDER BY created_at DESC LIMIT ?", params + (limit,)).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["context"] = json.loads(d["context"]) if d["context"] else {}
        except Exception:
            pass
        out.append(d)
    return out


def get_evolution_report(asset_type: str = "crypto") -> dict:
    conn = get_conn()
    base = "asset_type = ?"
    rep = {"asset_type": asset_type, "horizons": HORIZONS[asset_type],
           "total_signals": conn.execute(f"SELECT COUNT(*) n FROM signals_v4 WHERE {base}", (asset_type,)).fetchone()["n"],
           "global": {f"h{h}": _rate(conn, base, (asset_type,), h) for h in (1, 2, 3)},
           "by_signal": {}, "by_setup": {}, "by_confidence": {}, "by_regime": {}, "by_asset": {}}
    for s in ("LONG", "SHORT"):
        rep["by_signal"][s] = {f"h{h}": _rate(conn, base + " AND signal = ?", (asset_type, s), h) for h in (1, 2, 3)}
    for s in ("PULLBACK", "BREAKOUT"):
        rep["by_setup"][s] = {f"h{h}": _rate(conn, base + " AND setup = ?", (asset_type, s), h) for h in (1, 2, 3)}
    for c in ("ALTA", "MEDIA", "BAJA"):
        rep["by_confidence"][c] = {f"h{h}": _rate(conn, base + " AND confidence = ?", (asset_type, c), h) for h in (1, 2, 3)}
    for lab, clause in (("BULL", "regime LIKE '%BULL%'"), ("BEAR", "regime LIKE '%BEAR%'"),
                        ("NEUTRO", "(regime LIKE '%LATERAL%' OR regime LIKE '%TRANSICION%')")):
        rep["by_regime"][lab] = {f"h{h}": _rate(conn, base + " AND " + clause, (asset_type,), h) for h in (1, 2, 3)}
    for a in conn.execute(f"SELECT DISTINCT asset FROM signals_v4 WHERE {base}", (asset_type,)).fetchall():
        rep["by_asset"][a["asset"]] = {f"h{h}": _rate(conn, base + " AND asset = ?", (asset_type, a["asset"]), h) for h in (1, 2, 3)}
    conn.close()
    return rep


init_db()
