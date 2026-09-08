"""
Acciones, ETFs y CEDEARs.

Watchlist editable (data/watchlist.json). Cada item:
  {
    "ticker": "NVDA",          # subyacente en Yahoo (NVDA, SPY, ...) o accion local (YPFD.BA)
    "cedear": "NVDA.BA",       # ticker del CEDEAR en BYMA (opcional)
    "ratio": 24,               # cuantos CEDEARs = 1 accion (VERIFICAR en byma/comafi, cambian con splits)
    "type": "cedear" | "arg" | "etf",
    "name": "NVIDIA",
    "holding": true            # esta en cartera (solo informativo)
  }

Analisis: el motor v4 sobre el SUBYACENTE en USD (datos limpios: 1H/1D/1W de
Yahoo). Para acciones argentinas (YPFD.BA) se analiza directamente el .BA.
Al lado se muestra el precio del CEDEAR en pesos y el CCL implicito:
    CCL = precio_cedear_ARS * ratio / precio_subyacente_USD

Las senales son SOLO LONG (COMPRAR / ACUMULAR / MANTENER / REDUCIR / VENDER /
ESPERAR), pensadas para cartera, no para trading apalancado.
"""

import json
import time
from pathlib import Path
from datetime import datetime, timezone

from data import fetch_stock_multi_tf, fetch_last_price_yahoo
from engine import prepare_frames, analyze_frames

DATA_DIR = Path(__file__).parent / "data"
WATCHLIST_PATH = DATA_DIR / "watchlist.json"

# Cartera inicial de Nicolas (abril 2026). Los ratios son los publicados por
# BYMA/Comafi a esa fecha — VERIFICAR antes de confiar en el CCL implicito,
# porque cambian con splits. Se pueden editar desde el dashboard.
DEFAULT_WATCHLIST = [
    {"ticker": "NVDA", "cedear": "NVDA.BA", "ratio": 24, "type": "cedear", "name": "NVIDIA", "holding": True},
    {"ticker": "MSFT", "cedear": "MSFT.BA", "ratio": 30, "type": "cedear", "name": "Microsoft", "holding": True},
    {"ticker": "META", "cedear": "META.BA", "ratio": 24, "type": "cedear", "name": "Meta", "holding": True},
    {"ticker": "MELI", "cedear": "MELI.BA", "ratio": 120, "type": "cedear", "name": "MercadoLibre", "holding": True},
    {"ticker": "GOOGL", "cedear": "GOOGL.BA", "ratio": 58, "type": "cedear", "name": "Alphabet", "holding": True},
    {"ticker": "AMD", "cedear": "AMD.BA", "ratio": 10, "type": "cedear", "name": "AMD", "holding": True},
    {"ticker": "NU", "cedear": "NU.BA", "ratio": 5, "type": "cedear", "name": "Nu Holdings", "holding": True},
    {"ticker": "TSM", "cedear": "TSM.BA", "ratio": 20, "type": "cedear", "name": "TSMC", "holding": True},
    {"ticker": "SPY", "cedear": "SPY.BA", "ratio": 20, "type": "etf", "name": "S&P 500 ETF", "holding": True},
    {"ticker": "VIST", "cedear": "VIST.BA", "ratio": 3, "type": "cedear", "name": "Vista Energy", "holding": True},
    {"ticker": "FCX", "cedear": "FCX.BA", "ratio": 5, "type": "cedear", "name": "Freeport-McMoRan", "holding": True},
    {"ticker": "URA", "cedear": "URA.BA", "ratio": 20, "type": "etf", "name": "Uranium ETF", "holding": True},
    {"ticker": "COPX", "cedear": "COPX.BA", "ratio": 5, "type": "etf", "name": "Copper Miners ETF", "holding": True},
    {"ticker": "YPFD.BA", "cedear": None, "ratio": None, "type": "arg", "name": "YPF (BYMA)", "holding": True},
    {"ticker": "PAMP.BA", "cedear": None, "ratio": None, "type": "arg", "name": "Pampa Energia (BYMA)", "holding": True},
    {"ticker": "CELU.BA", "cedear": None, "ratio": None, "type": "arg", "name": "Celulosa (BYMA)", "holding": True},
]


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Watchlist ─────────────────────────────────────────────────────────────────

def load_watchlist() -> list:
    DATA_DIR.mkdir(exist_ok=True)
    if WATCHLIST_PATH.exists():
        try:
            with open(WATCHLIST_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    save_watchlist(DEFAULT_WATCHLIST)
    return list(DEFAULT_WATCHLIST)


def save_watchlist(items: list):
    DATA_DIR.mkdir(exist_ok=True)
    with open(WATCHLIST_PATH, "w") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)


def add_to_watchlist(item: dict) -> dict:
    t = (item.get("ticker") or "").strip().upper()
    if not t:
        return {"ok": False, "error": "Ticker vacio"}
    wl = load_watchlist()
    if any(w["ticker"] == t for w in wl):
        return {"ok": False, "error": f"{t} ya esta en la lista"}
    typ = item.get("type") or ("arg" if t.endswith(".BA") else "cedear")
    ced = item.get("cedear")
    if typ != "arg" and not ced:
        ced = f"{t}.BA"
    wl.append({"ticker": t, "cedear": ced, "ratio": _num(item.get("ratio")), "type": typ,
               "name": item.get("name") or t, "holding": bool(item.get("holding", False))})
    save_watchlist(wl)
    return {"ok": True, "watchlist": wl}


def update_watchlist_item(ticker: str, changes: dict) -> dict:
    wl = load_watchlist()
    for w in wl:
        if w["ticker"] == ticker.upper():
            for k in ("cedear", "name", "type"):
                if k in changes:
                    w[k] = changes[k]
            if "ratio" in changes:
                w["ratio"] = _num(changes["ratio"])
            if "holding" in changes:
                w["holding"] = bool(changes["holding"])
            save_watchlist(wl)
            return {"ok": True, "watchlist": wl}
    return {"ok": False, "error": "Ticker no encontrado"}


def remove_from_watchlist(ticker: str) -> dict:
    wl = load_watchlist()
    new = [w for w in wl if w["ticker"] != ticker.upper()]
    if len(new) == len(wl):
        return {"ok": False, "error": "Ticker no encontrado"}
    save_watchlist(new)
    return {"ok": True, "watchlist": new}


def _num(v):
    try:
        return float(v) if v not in (None, "", "null") else None
    except Exception:
        return None


# ── Analisis ──────────────────────────────────────────────────────────────────

def analyze_stock(item: dict, capital: float = 10000, risk_pct: float = 0.02) -> dict:
    ticker = item["ticker"]
    try:
        dfs = fetch_stock_multi_tf(ticker, intraday=True)
        frames = prepare_frames(dfs)
        r = analyze_frames(item.get("name") or ticker, frames, asset_type="stock",
                           allow_short=False, funding=None, capital=capital, risk_pct=risk_pct)
        if r.get("error"):
            return {"ticker": ticker, "name": item.get("name"), "error": r["error"]}
        r["ticker"] = ticker
        r["type"] = item.get("type")
        r["holding"] = item.get("holding", False)
        r["currency"] = "ARS" if ticker.endswith(".BA") else "USD"

        # CEDEAR: precio en pesos + CCL implicito
        ced = item.get("cedear")
        ratio = item.get("ratio")
        r["cedear"] = None
        if ced:
            ars = fetch_last_price_yahoo(ced)
            if ars:
                ccl = (ars * ratio / r["price"]) if (ratio and r["price"]) else None
                r["cedear"] = {"ticker": ced, "price_ars": round(ars, 2), "ratio": ratio,
                               "ccl_implicito": round(ccl, 1) if ccl else None,
                               "precio_teorico_ars_por_cedear": None}
        r["action"] = _portfolio_action(r)
        return r
    except Exception as e:
        return {"ticker": ticker, "name": item.get("name"), "error": str(e)[:200]}


def _portfolio_action(r: dict) -> str:
    """
    Reetiqueta la accion del motor para uso de cartera (solo long).
    El motor ya devuelve COMPRAR / MANTENER / REDUCIR / PROTEGER / ESPERAR...
    Aca solo se normaliza para la pestaña de acciones.
    """
    a = r["action"]
    if a.startswith("COMPRAR"):
        return "COMPRAR" if r["confidence"] in ("ALTA", "MEDIA") else "ACUMULAR (chico)"
    if a.startswith("VENDER"):
        return "VENDER"
    return a


def analyze_watchlist(capital: float = 10000, risk_pct: float = 0.02, only: list | None = None) -> list:
    wl = load_watchlist()
    if only:
        wl = [w for w in wl if w["ticker"] in only]
    out = []
    for item in wl:
        print(f"  Analizando {item['ticker']}...")
        out.append(analyze_stock(item, capital, risk_pct))
        time.sleep(0.6)   # Yahoo rate limit
    return out


def build_stocks_summary(results: list) -> dict:
    valid = [r for r in results if not r.get("error")]
    if not valid:
        return {"headline": "Sin datos", "detail": ""}
    n = len(valid)
    up = [r["ticker"] for r in valid if r["bias"] >= 15]
    dn = [r["ticker"] for r in valid if r["bias"] <= -15]
    buys = [r["ticker"] for r in valid if r["action"].startswith(("COMPRAR", "ACUMULAR"))]
    sells = [r["ticker"] for r in valid if r["action"].startswith(("VENDER", "REDUCIR", "PROTEGER"))]
    spy = next((r for r in valid if r["ticker"] == "SPY"), None)
    headline = ("Mercado alcista" if len(up) >= n * 0.6 else "Mercado bajista" if len(dn) >= n * 0.6 else "Mercado mixto")
    parts = []
    if spy:
        parts.append(f"SPY (referencia) en regimen {spy['regime']}, {spy['position'].lower()}.")
    parts.append(f"{len(up)} de {n} en tendencia alcista, {len(dn)} bajista.")
    if buys:
        parts.append("Oportunidades de compra: " + ", ".join(buys) + ".")
    if sells:
        parts.append("Revisar / reducir: " + ", ".join(sells) + ".")
    if not buys and not sells:
        parts.append("Sin acciones concretas ahora: mantener y esperar pullbacks a soporte.")
    return {"headline": headline, "detail": " ".join(parts)}
