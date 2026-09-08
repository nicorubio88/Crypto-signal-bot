"""
Soportes y resistencias ESTRUCTURALES.

Fuentes de niveles (todas causales, sin lookahead):
  1. Swing highs / swing lows por fractales en cada timeframe (1H, 4H, 1D, 1W)
  2. Maximo / minimo del dia anterior y de la semana anterior (PDH/PDL, PWH/PWL)
  3. Pivots clasicos calculados sobre la vela DIARIA y SEMANAL previa
  4. Nodos de alto volumen (HVN) del perfil de volumen (volumen distribuido
     sobre el rango high-low de cada vela, no solo en el cierre)
  5. Maximo y minimo del periodo largo (rango de 1 anio en diario)

Los candidatos se agrupan en clusters (dentro de una tolerancia proporcional
al ATR) y cada cluster recibe una FUERZA que combina: peso del timeframe
de origen, cantidad de toques, recencia, volumen negociado en la zona y si
ya fue soporte y resistencia (flip).

Salida: lista de soportes (debajo del precio, del mas cercano al mas lejano)
y resistencias (arriba del precio), con fuerza 0-100 y explicacion.
"""

import math
import numpy as np
import pandas as pd

TF_WEIGHT = {"1w": 4.0, "1d": 3.0, "4h": 2.0, "1h": 1.0}
TF_LABEL = {"1w": "1W", "1d": "1D", "4h": "4H", "1h": "1H"}
# Cuantas velas hacia atras mirar por timeframe para buscar swings
TF_LOOKBACK = {"1w": 260, "1d": 400, "4h": 500, "1h": 400}
# Cuantas velas a cada lado para confirmar un fractal
TF_FRACTAL = {"1w": 2, "1d": 3, "4h": 3, "1h": 4}
# Vida media (en velas) para el decaimiento por recencia
TF_HALFLIFE = {"1w": 80, "1d": 120, "4h": 200, "1h": 200}


# ── Swings por fractales ──────────────────────────────────────────────────────

def find_swings(df: pd.DataFrame, left: int = 3, right: int = 3) -> list:
    """
    Swing high en i: high[i] es el maximo estricto de las 'left' velas previas
    y >= las 'right' velas siguientes. Swing low simetrico.
    Solo se confirman swings con 'right' velas posteriores cerradas (sin lookahead).
    Devuelve lista de dicts ordenada cronologicamente.
    """
    if df is None or len(df) < left + right + 1:
        return []
    highs = df["high"].values
    lows = df["low"].values
    vols = df["volume"].values if "volume" in df else np.zeros(len(df))
    n = len(df)
    swings = []
    for i in range(left, n - right):
        hw = highs[i - left:i + right + 1]
        lw = lows[i - left:i + right + 1]
        if highs[i] == hw.max() and (highs[i] > highs[i - left:i]).all():
            swings.append({"pos": i, "time": df.index[i], "price": float(highs[i]),
                           "kind": "H", "volume": float(vols[i])})
        if lows[i] == lw.min() and (lows[i] < lows[i - left:i]).all():
            swings.append({"pos": i, "time": df.index[i], "price": float(lows[i]),
                           "kind": "L", "volume": float(vols[i])})
    return swings


# ── Perfil de volumen ─────────────────────────────────────────────────────────

def volume_profile(df: pd.DataFrame, bins: int = 48, lookback: int = 300) -> dict:
    """
    Distribuye el volumen de cada vela uniformemente entre los bins que cubre
    su rango high-low. Devuelve POC, value area (70%) y nodos de alto volumen.
    """
    d = df.tail(lookback)
    if len(d) < 20:
        return {}
    lo, hi = float(d["low"].min()), float(d["high"].max())
    if hi <= lo:
        return {}
    edges = np.linspace(lo, hi, bins + 1)
    width = edges[1] - edges[0]
    vol = np.zeros(bins)
    for h, l, v in zip(d["high"].values, d["low"].values, d["volume"].values):
        if v <= 0:
            continue
        i0 = int(np.clip((l - lo) / width, 0, bins - 1))
        i1 = int(np.clip((h - lo) / width, 0, bins - 1))
        span = i1 - i0 + 1
        vol[i0:i1 + 1] += v / span
    total = vol.sum()
    if total <= 0:
        return {}
    centers = (edges[:-1] + edges[1:]) / 2
    poc_i = int(vol.argmax())
    # Value area: expandir desde el POC hasta cubrir 70 %
    lo_i = hi_i = poc_i
    covered = vol[poc_i]
    while covered / total < 0.70 and (lo_i > 0 or hi_i < bins - 1):
        left = vol[lo_i - 1] if lo_i > 0 else -1
        right = vol[hi_i + 1] if hi_i < bins - 1 else -1
        if left >= right:
            lo_i -= 1; covered += vol[lo_i]
        else:
            hi_i += 1; covered += vol[hi_i]
    # HVN: maximos locales con volumen > 1.4x el promedio
    avg = total / bins
    hvn = []
    for i in range(bins):
        l_ok = i == 0 or vol[i] >= vol[i - 1]
        r_ok = i == bins - 1 or vol[i] >= vol[i + 1]
        if l_ok and r_ok and vol[i] > 1.4 * avg:
            hvn.append({"price": float(centers[i]), "rel": float(vol[i] / avg)})
    hvn = sorted(hvn, key=lambda x: -x["rel"])[:6]
    return {"poc": float(centers[poc_i]), "vah": float(edges[hi_i + 1]), "val": float(edges[lo_i]),
            "hvn": hvn, "bin_width": float(width),
            "profile": [{"price": float(c), "vol": float(v / avg)} for c, v in zip(centers, vol)]}


# ── Pivots clasicos ───────────────────────────────────────────────────────────

def classic_pivots(bar: pd.Series) -> dict:
    p = (bar["high"] + bar["low"] + bar["close"]) / 3
    rng = bar["high"] - bar["low"]
    return {"P": float(p), "R1": float(2 * p - bar["low"]), "S1": float(2 * p - bar["high"]),
            "R2": float(p + rng), "S2": float(p - rng)}


# ── Construccion de niveles ───────────────────────────────────────────────────

def _cluster(cands: list, tol: float) -> list:
    """Agrupa candidatos ordenados por precio en clusters de ancho ~tol."""
    cands = sorted(cands, key=lambda c: c["price"])
    clusters = []
    for c in cands:
        if clusters:
            cl = clusters[-1]
            width_ok = (c["price"] - min(m["price"] for m in cl["members"])) <= 2.0 * tol
            if abs(c["price"] - cl["center"]) <= tol and width_ok:
                cl["members"].append(c)
                w = sum(m["w"] for m in cl["members"])
                cl["center"] = sum(m["price"] * m["w"] for m in cl["members"]) / w
                continue
        clusters.append({"center": c["price"], "members": [c]})
    return clusters


def build_levels(dfs: dict, price: float, ref_tf: str = "4h", asset_type: str = "crypto",
                 max_each: int = 5) -> dict:
    """
    dfs: {"1h": df, "4h": df, "1d": df, "1w": df} con columnas OHLCV (+ ATR opcional).
    price: precio actual.
    ref_tf: marco cuyo ATR define la tolerancia de agrupamiento.
    """
    ref = dfs.get(ref_tf)
    if ref is None:
        ref = dfs.get("1d")
    atr_ref = None
    if ref is not None and "ATR" in ref and pd.notna(ref["ATR"].iloc[-1]):
        atr_ref = float(ref["ATR"].iloc[-1])
    if not atr_ref:
        # fallback: rango medio de las ultimas 14 velas
        atr_ref = float((ref["high"] - ref["low"]).tail(14).mean())
    tol = max(0.45 * atr_ref, price * 0.0025)

    cands = []
    now_pos = {}

    # 1. Swings por timeframe
    for tf, df in dfs.items():
        if df is None or len(df) < 30 or tf not in TF_WEIGHT:
            continue
        d = df.tail(TF_LOOKBACK[tf])
        fr = TF_FRACTAL[tf]
        sw = find_swings(d, fr, fr)
        n = len(d)
        hl = TF_HALFLIFE[tf]
        for s in sw:
            age = n - 1 - s["pos"]
            rec = 0.35 + 0.65 * math.exp(-age / hl)
            cands.append({"price": s["price"], "w": TF_WEIGHT[tf] * rec, "tf": tf,
                          "src": f"swing {'alto' if s['kind']=='H' else 'bajo'} {TF_LABEL[tf]}",
                          "kind": s["kind"], "time": s["time"]})

    # 2. Maximo/minimo del dia y semana anterior + pivots
    d1, w1 = dfs.get("1d"), dfs.get("1w")
    if d1 is not None and len(d1) >= 3:
        prev = d1.iloc[-2]
        cands.append({"price": float(prev["high"]), "w": 2.0, "tf": "1d", "src": "maximo dia anterior", "kind": "H", "time": d1.index[-2]})
        cands.append({"price": float(prev["low"]), "w": 2.0, "tf": "1d", "src": "minimo dia anterior", "kind": "L", "time": d1.index[-2]})
        pv = classic_pivots(prev)
        for k in ("R1", "S1", "R2", "S2"):
            cands.append({"price": pv[k], "w": 0.8, "tf": "1d", "src": f"pivot diario {k}", "kind": "H" if k[0] == "R" else "L", "time": d1.index[-2]})
        # Extremos del ultimo anio (diario)
        yr = d1.tail(252)
        cands.append({"price": float(yr["high"].max()), "w": 3.0, "tf": "1d", "src": "maximo 52 semanas", "kind": "H", "time": yr["high"].idxmax()})
        cands.append({"price": float(yr["low"].min()), "w": 3.0, "tf": "1d", "src": "minimo 52 semanas", "kind": "L", "time": yr["low"].idxmin()})
    if w1 is not None and len(w1) >= 3:
        prev = w1.iloc[-2]
        cands.append({"price": float(prev["high"]), "w": 3.0, "tf": "1w", "src": "maximo semana anterior", "kind": "H", "time": w1.index[-2]})
        cands.append({"price": float(prev["low"]), "w": 3.0, "tf": "1w", "src": "minimo semana anterior", "kind": "L", "time": w1.index[-2]})
        pv = classic_pivots(prev)
        for k in ("R1", "S1"):
            cands.append({"price": pv[k], "w": 1.2, "tf": "1w", "src": f"pivot semanal {k}", "kind": "H" if k[0] == "R" else "L", "time": w1.index[-2]})

    # 3. Perfil de volumen (marco de referencia: 4H cripto / 1D acciones)
    vp_df = dfs.get("4h") if asset_type == "crypto" else dfs.get("1d")
    vp = volume_profile(vp_df, bins=48, lookback=300 if asset_type == "crypto" else 250) if vp_df is not None else {}
    for node in vp.get("hvn", []):
        cands.append({"price": node["price"], "w": 1.0 + min(node["rel"], 4) * 0.5, "tf": "vp",
                      "src": f"nodo de volumen ({node['rel']:.1f}x)", "kind": "V", "time": None})
    if vp.get("poc"):
        cands.append({"price": vp["poc"], "w": 2.0, "tf": "vp", "src": "POC (mayor volumen)", "kind": "V", "time": None})

    if not cands:
        return {"supports": [], "resistances": [], "tolerance": tol, "atr": atr_ref, "volume_profile": vp}

    # 4. Clustering
    clusters = _cluster(cands, tol)
    levels = []
    for cl in clusters:
        m = cl["members"]
        raw = sum(x["w"] for x in m)
        touches = sum(1 for x in m if x["kind"] in ("H", "L"))
        kinds = {x["kind"] for x in m}
        flip = "H" in kinds and "L" in kinds  # fue techo y piso -> nivel muy respetado
        if flip:
            raw *= 1.25
        if touches >= 3:
            raw *= 1.15
        tfs = sorted({x["tf"] for x in m if x["tf"] in TF_WEIGHT}, key=lambda t: -TF_WEIGHT[t])
        # Resumen de fuentes (agrupado)
        src_count = {}
        for x in m:
            src_count[x["src"]] = src_count.get(x["src"], 0) + 1
        sources = [f"{k} x{v}" if v > 1 else k for k, v in
                   sorted(src_count.items(), key=lambda kv: -kv[1])][:4]
        last_touch = max((x["time"] for x in m if x["time"] is not None), default=None)
        levels.append({"price": float(cl["center"]), "raw": raw, "touches": touches,
                       "flip": flip, "tfs": [TF_LABEL[t] for t in tfs], "sources": sources,
                       "last_touch": str(last_touch)[:16] if last_touch is not None else None,
                       "lo": min(x["price"] for x in m), "hi": max(x["price"] for x in m)})

    max_raw = max(l["raw"] for l in levels)
    for l in levels:
        l["strength"] = int(round(100 * l["raw"] / max_raw))
        l["label"] = "FUERTE" if l["strength"] >= 60 else "MEDIO" if l["strength"] >= 30 else "DEBIL"
        l["distance_pct"] = round((l["price"] / price - 1) * 100, 2)
        del l["raw"]

    # Nivel actual (precio dentro de la zona) y reparto
    at_level = [l for l in levels if l["lo"] - tol * 0.5 <= price <= l["hi"] + tol * 0.5]
    supports = sorted([l for l in levels if l["price"] < price and l not in at_level],
                      key=lambda l: -l["price"])
    resistances = sorted([l for l in levels if l["price"] > price and l not in at_level],
                         key=lambda l: l["price"])

    # Filtrar niveles debiles muy lejanos para no ensuciar: quedarse con los
    # max_each mas cercanos, pero asegurando que los FUERTES dentro de +-25 % entren.
    def pick(lst):
        near = lst[:max_each]
        strong = [l for l in lst[max_each:] if l["label"] == "FUERTE" and abs(l["distance_pct"]) <= 25]
        return near + strong[:2]

    supports, resistances = pick(supports), pick(resistances)
    key_levels = sorted(levels, key=lambda l: -l["strength"])[:5]

    return {
        "supports": supports,
        "resistances": resistances,
        "at_level": at_level[0] if at_level else None,
        "nearest_support": supports[0] if supports else None,
        "nearest_resistance": resistances[0] if resistances else None,
        "key_levels": key_levels,
        "tolerance": round(tol, 6),
        "atr": round(atr_ref, 6),
        "volume_profile": {k: vp.get(k) for k in ("poc", "vah", "val")} if vp else {},
        "profile_bins": vp.get("profile", []) if vp else [],
        "all_levels": sorted(levels, key=lambda l: l["price"]),
    }


def room_to_level(price: float, level: dict | None) -> float | None:
    """Distancia en % al nivel (positiva si esta arriba, negativa si abajo)."""
    if not level:
        return None
    return round((level["price"] / price - 1) * 100, 2)
