"""
Motor de datos y calculo de indicadores v2.1
Fuente: Kraken API publica

Capas de analisis:
  1. Filtro macro 1W (tendencia semanal)
  2. Filtro tendencia 1D
  3. Setup de entrada 4H (scoring principal)
  4. Confirmacion 1H (timing)

Indicadores: EMA20/50/200, Bollinger Bands + Width, RSI (6/14/20),
StochRSI, MACD, ADX + DI, OBV, VWAP diario, ATR, divergencias RSI, pivots.
"""

import time
import requests
import pandas as pd
import pandas_ta as ta
from datetime import datetime

# ── Configuracion ─────────────────────────────────────────────────────────────

SYMBOLS = {
    "BTC": "XBTUSD",
    "ETH": "ETHUSD",
    "SOL": "SOLUSD",
    "XRP": "XRPUSD",
    "LINK": "LINKUSD",
}

KRAKEN_URL = "https://api.kraken.com/0/public/OHLC"
KRAKEN_INTERVALS = {"1h": 60, "4h": 240, "1d": 1440, "1w": 10080}

# Funding rate - multiples fuentes con fallback en cascada
# Todos los exchanges pagan funding cada 8h y reportan el rate del periodo actual.
BINANCE_FUNDING_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
BYBIT_FUNDING_URL = "https://api.bybit.com/v5/market/tickers"
OKX_FUNDING_URL = "https://www.okx.com/api/v5/public/funding-rate"

# Simbolos por exchange (cada uno usa su propia nomenclatura)
FUNDING_SYMBOLS = {
    "BTC": {"binance": "BTCUSDT", "bybit": "BTCUSDT", "okx": "BTC-USDT-SWAP"},
    "ETH": {"binance": "ETHUSDT", "bybit": "ETHUSDT", "okx": "ETH-USDT-SWAP"},
    "SOL": {"binance": "SOLUSDT", "bybit": "SOLUSDT", "okx": "SOL-USDT-SWAP"},
    "XRP": {"binance": "XRPUSDT", "bybit": "XRPUSDT", "okx": "XRP-USDT-SWAP"},
    "LINK": {"binance": "LINKUSDT", "bybit": "LINKUSDT", "okx": "LINK-USDT-SWAP"},
}

# Score maximo teorico (suma de todos los pesos positivos)
# EMA: 3 | Bollinger MB: 1 | VWAP: 1 | MACD: 2 | StochRSI: 1 |
# RSI20: 1 | Divergencia RSI: 3 | OBV: 1 | Divergencia OBV: 2 | Volumen: 1 = 16
# + Funding rate: ±1 (opcional, si Binance esta disponible) = 17
MAX_SCORE = 17
SIGNAL_THRESHOLD = 9  # umbral base (se ajusta por ADX dinamicamente via get_adaptive_threshold)


# ── Fetch de velas ────────────────────────────────────────────────────────────

def fetch_candles(symbol: str, interval: str, limit: int = 300) -> pd.DataFrame:
    params = {"pair": symbol, "interval": KRAKEN_INTERVALS[interval]}
    resp = requests.get(KRAKEN_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise ValueError(f"Kraken error: {data['error']}")
    result = data["result"]
    pair_key = [k for k in result.keys() if k != "last"][0]
    raw = result[pair_key]
    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "vwap_raw", "volume", "count"
    ])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="s")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df.set_index("open_time", inplace=True)
    return df[["open", "high", "low", "close", "volume"]].tail(limit)


# ── Fetch funding rate (Binance Futures - sentimiento de perpetuos) ──────────

# ── Fetch funding rate (multi-fuente con fallback) ───────────────────────────

def _fetch_binance_funding(symbol: str) -> float:
    """Devuelve funding rate en porcentaje (8h). Lanza excepcion si falla."""
    resp = requests.get(BINANCE_FUNDING_URL, params={"symbol": symbol},
                        timeout=8, headers={"User-Agent": "Mozilla/5.0"})
    if resp.status_code in (451, 403):
        raise PermissionError(f"Binance bloqueado ({resp.status_code})")
    resp.raise_for_status()
    data = resp.json()
    return float(data["lastFundingRate"]) * 100


def _fetch_bybit_funding(symbol: str) -> float:
    """Devuelve funding rate en porcentaje (8h). Lanza excepcion si falla."""
    resp = requests.get(BYBIT_FUNDING_URL,
                        params={"category": "linear", "symbol": symbol},
                        timeout=8, headers={"User-Agent": "Mozilla/5.0"})
    if resp.status_code in (451, 403):
        raise PermissionError(f"Bybit bloqueado ({resp.status_code})")
    resp.raise_for_status()
    data = resp.json()
    lst = data.get("result", {}).get("list", [])
    if not lst:
        raise ValueError("Bybit sin datos")
    return float(lst[0]["fundingRate"]) * 100


def _fetch_okx_funding(symbol: str) -> float:
    """Devuelve funding rate en porcentaje (8h). Lanza excepcion si falla."""
    resp = requests.get(OKX_FUNDING_URL, params={"instId": symbol},
                        timeout=8, headers={"User-Agent": "Mozilla/5.0"})
    if resp.status_code in (451, 403):
        raise PermissionError(f"OKX bloqueado ({resp.status_code})")
    resp.raise_for_status()
    data = resp.json()
    lst = data.get("data", [])
    if not lst:
        raise ValueError("OKX sin datos")
    return float(lst[0]["fundingRate"]) * 100


def _classify_funding(rate_pct: float, source: str) -> dict:
    """Clasifica un funding rate (en %, periodo 8h) en sentimiento + ajuste de score."""
    if rate_pct > 0.10:
        sentiment, signal_impact, score_adj = "EUFORIA ALCISTA", "BEARISH", -1
    elif rate_pct > 0.05:
        sentiment, signal_impact, score_adj = "Sobrecompra emocional", "BEARISH", -1
    elif rate_pct < -0.10:
        sentiment, signal_impact, score_adj = "CAPITULACIÓN BAJISTA", "BULLISH", 1
    elif rate_pct < -0.05:
        sentiment, signal_impact, score_adj = "Sobreventa emocional", "BULLISH", 1
    else:
        sentiment, signal_impact, score_adj = "Neutral", "NEUTRAL", 0

    return {
        "rate": round(rate_pct, 4),
        "rate_annualized": round(rate_pct * 3 * 365, 2),  # 3 periodos/dia × 365
        "sentiment": sentiment,
        "signal_impact": signal_impact,
        "score_adj": score_adj,
        "source": source,
        "available": True,
    }


def fetch_funding_rate(asset: str) -> dict:
    """
    Funding rate de perpetuos. Indicador de sentimiento real del mercado.

    - Funding POSITIVO: longs pagan a shorts → sobrecomprado emocionalmente
      * > 0.10% (8h)  → euforia, alta probabilidad de correccion (bearish)
      * > 0.05% (8h)  → sobrecompra emocional (bearish)
    - Funding NEGATIVO: shorts pagan a longs → sobrevendido emocionalmente
      * < -0.10% (8h) → capitulacion, alta probabilidad de rebote (bullish)
      * < -0.05% (8h) → sobreventa emocional (bullish)

    Fallback en cascada: Binance → Bybit → OKX. Si los 3 fallan (raro),
    devuelve available=False y el sistema sigue funcionando sin este indicador.
    """
    symbols = FUNDING_SYMBOLS.get(asset)
    if not symbols:
        return {"rate": None, "available": False, "reason": "Asset no soportado"}

    # Cascada de fuentes: el primero que responda gana
    sources = [
        ("binance", _fetch_binance_funding, symbols["binance"]),
        ("bybit",   _fetch_bybit_funding,   symbols["bybit"]),
        ("okx",     _fetch_okx_funding,     symbols["okx"]),
    ]

    errors = []
    for name, fetcher, symbol in sources:
        try:
            rate_pct = fetcher(symbol)
            return _classify_funding(rate_pct, name)
        except Exception as e:
            errors.append(f"{name}: {str(e)[:30]}")
            continue

    # Los 3 fallaron
    return {
        "rate": None,
        "available": False,
        "reason": "Todas las fuentes fallaron — " + " | ".join(errors),
    }


# ── Calculo de indicadores ────────────────────────────────────────────────────

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"]
    h = df["high"]
    l = df["low"]
    v = df["volume"]

    # Medias moviles
    df["MA20"]   = ta.sma(c, 20)
    df["MA50"]   = ta.sma(c, 50)
    df["MA200"]  = ta.sma(c, 200)
    df["EMA20"]  = ta.ema(c, 20)
    df["EMA50"]  = ta.ema(c, 50)
    df["EMA200"] = ta.ema(c, 200)

    # Bollinger Bands + BB Width
    bb = ta.bbands(c, length=20, std=2)
    bb_up_col = [col for col in bb.columns if col.startswith("BBU")][0]
    bb_mb_col = [col for col in bb.columns if col.startswith("BBM")][0]
    bb_dn_col = [col for col in bb.columns if col.startswith("BBL")][0]
    df["BB_UP"] = bb[bb_up_col]
    df["BB_MB"] = bb[bb_mb_col]
    df["BB_DN"] = bb[bb_dn_col]
    df["BB_WIDTH"] = (df["BB_UP"] - df["BB_DN"]) / df["BB_MB"]

    # RSI multiple
    df["RSI6"]  = ta.rsi(c, 6)
    df["RSI14"] = ta.rsi(c, 14)
    df["RSI20"] = ta.rsi(c, 20)

    # Stochastic RSI (3,3,14,14)
    try:
        stoch = ta.stochrsi(c, length=14, rsi_length=14, k=3, d=3)
        if stoch is not None and not stoch.empty:
            k_col = [col for col in stoch.columns if "_K" in col]
            d_col = [col for col in stoch.columns if "_D" in col]
            if k_col: df["STOCH_K"] = stoch[k_col[0]]
            if d_col: df["STOCH_D"] = stoch[d_col[0]]
    except Exception:
        pass

    # MACD (12,26,9)
    macd = ta.macd(c, fast=12, slow=26, signal=9)
    macd_col  = [col for col in macd.columns if col.startswith("MACD_")][0]
    macds_col = [col for col in macd.columns if col.startswith("MACDs")][0]
    macdh_col = [col for col in macd.columns if col.startswith("MACDh")][0]
    df["MACD"]        = macd[macd_col]
    df["MACD_SIGNAL"] = macd[macds_col]
    df["MACD_HIST"]   = macd[macdh_col]

    # ADX (14)
    adx = ta.adx(h, l, c, length=14)
    adx_col = [col for col in adx.columns if col.startswith("ADX_")][0]
    dmp_col = [col for col in adx.columns if col.startswith("DMP_")][0]
    dmn_col = [col for col in adx.columns if col.startswith("DMN_")][0]
    df["ADX"]    = adx[adx_col]
    df["DI_POS"] = adx[dmp_col]
    df["DI_NEG"] = adx[dmn_col]

    # OBV
    df["OBV"]      = ta.obv(c, v)
    df["OBV_MA20"] = ta.sma(df["OBV"], 20)

    # ATR (14)
    df["ATR"] = ta.atr(h, l, c, length=14)

    # Volumen MA
    df["VOL_MA10"] = ta.sma(v, 10)

    return df


def add_daily_vwap(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calcula VWAP que se resetea por dia (estandar institucional).
    Usa typical price = (H+L+C)/3.
    """
    tp = (df["high"] + df["low"] + df["close"]) / 3
    pv = tp * df["volume"]
    day = df.index.normalize()
    df["VWAP"] = pv.groupby(day).cumsum() / df["volume"].groupby(day).cumsum()
    return df


# ── Divergencias RSI (correcta: minimos secuenciales) ─────────────────────────

def detect_rsi_divergence(df: pd.DataFrame, lookback: int = 30) -> dict:
    """
    Detecta divergencias usando dos minimos/maximos secuenciales reales.
    Logica:
      - Busca el minimo mas reciente en la primera mitad del lookback
      - Busca el minimo mas reciente en la segunda mitad
      - Compara cronologicamente: minimo viejo vs minimo nuevo

    Divergencia alcista: precio hace minimo mas bajo, RSI mas alto
    Divergencia bajista: precio hace maximo mas alto, RSI mas bajo
    """
    if len(df) < lookback + 2 or "RSI14" not in df.columns:
        return {"bullish": False, "bearish": False}

    recent = df.tail(lookback).dropna(subset=["RSI14"])
    if len(recent) < 10:
        return {"bullish": False, "bearish": False}

    # Dividir en dos mitades cronologicas
    mid       = len(recent) // 2
    first_half  = recent.iloc[:mid]
    second_half = recent.iloc[mid:]

    # ── Divergencia alcista: dos minimos ──────────────────────────────────────
    p_low_idx_1 = first_half["close"].idxmin()
    p_low_idx_2 = second_half["close"].idxmin()
    p_low_1 = first_half.loc[p_low_idx_1, "close"]
    p_low_2 = second_half.loc[p_low_idx_2, "close"]
    rsi_low_1 = first_half.loc[p_low_idx_1, "RSI14"]
    rsi_low_2 = second_half.loc[p_low_idx_2, "RSI14"]

    bullish = (
        p_low_2 < p_low_1            # precio segundo minimo mas bajo
        and rsi_low_2 > rsi_low_1 + 3 # RSI segundo minimo mas alto (margen 3 puntos)
        and rsi_low_1 < 40            # primer minimo en zona de sobreventa
    )

    # ── Divergencia bajista: dos maximos ──────────────────────────────────────
    p_high_idx_1 = first_half["close"].idxmax()
    p_high_idx_2 = second_half["close"].idxmax()
    p_high_1 = first_half.loc[p_high_idx_1, "close"]
    p_high_2 = second_half.loc[p_high_idx_2, "close"]
    rsi_high_1 = first_half.loc[p_high_idx_1, "RSI14"]
    rsi_high_2 = second_half.loc[p_high_idx_2, "RSI14"]

    bearish = (
        p_high_2 > p_high_1
        and rsi_high_2 < rsi_high_1 - 3
        and rsi_high_1 > 60           # primer maximo en zona de sobrecompra
    )

    return {"bullish": bool(bullish), "bearish": bool(bearish)}


# ── Divergencias OBV (precio vs volumen acumulado) ───────────────────────────

def detect_obv_divergence(df: pd.DataFrame, lookback: int = 30) -> dict:
    """
    Detecta divergencias entre precio y OBV.

    Divergencia alcista OBV: precio hace minimo mas bajo, OBV hace minimo mas alto
      → grandes manos compran mientras retail vende = posible reversion alcista

    Divergencia bajista OBV: precio hace maximo mas alto, OBV hace maximo mas bajo
      → grandes manos venden mientras retail compra = posible reversion bajista

    Mas confiable que divergencia RSI porque mide presion REAL de volumen,
    no solo momentum del precio.
    """
    if len(df) < lookback + 2 or "OBV" not in df.columns:
        return {"bullish": False, "bearish": False}

    recent = df.tail(lookback).dropna(subset=["OBV"])
    if len(recent) < 10:
        return {"bullish": False, "bearish": False}

    mid = len(recent) // 2
    first_half  = recent.iloc[:mid]
    second_half = recent.iloc[mid:]

    # ── Divergencia alcista ──────────────────────────────────────────────────
    p_low_idx_1 = first_half["close"].idxmin()
    p_low_idx_2 = second_half["close"].idxmin()
    p_low_1 = first_half.loc[p_low_idx_1, "close"]
    p_low_2 = second_half.loc[p_low_idx_2, "close"]
    obv_low_1 = first_half.loc[p_low_idx_1, "OBV"]
    obv_low_2 = second_half.loc[p_low_idx_2, "OBV"]

    # Margen de 0.5% para precio (significativo) y OBV creciendo (acumulacion)
    bullish = (
        p_low_2 < p_low_1 * 0.995
        and obv_low_2 > obv_low_1
    )

    # ── Divergencia bajista ──────────────────────────────────────────────────
    p_high_idx_1 = first_half["close"].idxmax()
    p_high_idx_2 = second_half["close"].idxmax()
    p_high_1 = first_half.loc[p_high_idx_1, "close"]
    p_high_2 = second_half.loc[p_high_idx_2, "close"]
    obv_high_1 = first_half.loc[p_high_idx_1, "OBV"]
    obv_high_2 = second_half.loc[p_high_idx_2, "OBV"]

    bearish = (
        p_high_2 > p_high_1 * 1.005
        and obv_high_2 < obv_high_1
    )

    return {"bullish": bool(bullish), "bearish": bool(bearish)}


# ── Pivots classicos (sobre vela anterior) ───────────────────────────────────

def calculate_pivots(df: pd.DataFrame) -> dict:
    """
    Pivots clasicos usando vela anterior cerrada (vela -2, ya que -1 puede estar activa).
    Estandar: pivot = (H+L+C)/3 de la vela previa.
    """
    if len(df) < 3:
        return {}
    prev = df.iloc[-3]  # vela cerrada previa a la ultima cerrada
    pivot = (prev["high"] + prev["low"] + prev["close"]) / 3
    rng   = prev["high"] - prev["low"]
    return {
        "pivot": round(pivot, 4),
        "r1": round(2 * pivot - prev["low"], 4),
        "r2": round(pivot + rng, 4),
        "s1": round(2 * pivot - prev["high"], 4),
        "s2": round(pivot - rng, 4),
    }


# ── Tendencia por timeframe ──────────────────────────────────────────────────

def get_trend(row: pd.Series) -> str:
    """
    Tendencia basada en alineacion de 3 EMAs (20, 50, 200).

    Estandar de la industria para evitar 'bear/bull traps':
    - ALCISTA: precio > EMA20 > EMA50 > EMA200 (alineacion completa)
    - BAJISTA: precio < EMA20 < EMA50 < EMA200 (alineacion completa)
    - LATERAL: cualquier desalineacion

    Para timeframes con pocos datos (1W con < 200 velas), usa EMA50 como fallback.
    """
    if pd.isna(row.get("EMA20")) or pd.isna(row.get("EMA50")):
        return "LATERAL"

    price = row["close"]
    ema20 = row["EMA20"]
    ema50 = row["EMA50"]
    ema200 = row.get("EMA200")

    # Si EMA200 no esta disponible (pocas velas), usar logica de 2 EMAs
    if pd.isna(ema200):
        if price > ema20 and ema20 > ema50:
            return "ALCISTA"
        if price < ema20 and ema20 < ema50:
            return "BAJISTA"
        return "LATERAL"

    # Logica completa con 3 EMAs (mas precisa)
    if price > ema20 and ema20 > ema50 and ema50 > ema200:
        return "ALCISTA"
    if price < ema20 and ema20 < ema50 and ema50 < ema200:
        return "BAJISTA"
    return "LATERAL"


# ── Régimen del mercado (separado de la señal operativa) ─────────────────────

def get_regime(trend_1w: str, trend_1d: str, trend_1h: str, adx: float) -> dict:
    """
    Clasifica el REGIMEN del mercado (en qué estado estamos) independiente
    de si hay buena oportunidad operativa AHORA.

    Diseño: separar "diagnóstico" de "tratamiento".
    - Régimen: ¿estamos en bear, bull, lateral?
    - Señal operativa: ¿es buen momento para entrar?

    Da contexto incluso cuando la señal es NEUTRAL.
    """
    if pd.isna(adx):
        adx = 0

    # Alinear timeframes
    tfs = [trend_1w, trend_1d, trend_1h]
    bullish_count = sum(1 for t in tfs if t == "ALCISTA")
    bearish_count = sum(1 for t in tfs if t == "BAJISTA")
    lateral_count = sum(1 for t in tfs if t == "LATERAL")

    # ── Régimen primario ────────────────────────────────────────────────────
    if bullish_count >= 2 and adx >= 25:
        if bullish_count == 3 and adx >= 35:
            regime = "BULL FUERTE"
            description = "Tendencia alcista clara en todos los timeframes con fuerza"
        elif bullish_count == 3:
            regime = "BULL"
            description = "Tendencia alcista en todos los timeframes"
        else:
            regime = "BULL DÉBIL"
            description = "Alcista predominante con un timeframe lateral o contrario"
    elif bearish_count >= 2 and adx >= 25:
        if bearish_count == 3 and adx >= 35:
            regime = "BEAR FUERTE"
            description = "Tendencia bajista clara en todos los timeframes con fuerza"
        elif bearish_count == 3:
            regime = "BEAR"
            description = "Tendencia bajista en todos los timeframes"
        else:
            regime = "BEAR DÉBIL"
            description = "Bajista predominante con un timeframe lateral o contrario"
    elif adx < 20:
        regime = "LATERAL ESTRICTO"
        description = "Sin tendencia clara (ADX muy bajo) — rangeo puro"
    elif lateral_count >= 2:
        regime = "LATERAL"
        description = "Múltiples timeframes laterales — sin convicción"
    else:
        regime = "TRANSICIÓN"
        description = "Timeframes desalineados — posible cambio de tendencia"

    # ── Bias direccional sugerido ──────────────────────────────────────────
    if "BULL" in regime:
        bias = "Sesgo LONG (buscar entradas en pullbacks)"
    elif "BEAR" in regime:
        bias = "Sesgo SHORT (buscar entradas en rebotes)"
    elif regime == "TRANSICIÓN":
        bias = "Esperar confirmación del próximo timeframe"
    else:
        bias = "No operar tendencia — eventualmente operar rangos"

    return {
        "regime": regime,
        "description": description,
        "bias": bias,
        "strength_score": bullish_count - bearish_count,  # -3 a +3
    }


# ── Sistema de scoring ponderado ──────────────────────────────────────────────

def calculate_score(df: pd.DataFrame) -> dict:
    """
    Score ponderado v2.2 (max 16 puntos).
    ADX < 25 = filtro obligatorio, no emite señal.
    """
    if len(df) < 3:
        return {"score": 0, "max_score": MAX_SCORE, "conditions": {},
                "market_trending": False, "adx": None, "di_pos": None, "di_neg": None,
                "rsi6_oversold": False, "rsi6_overbought": False,
                "bb_squeeze": False,
                "divergence": {"bullish": False, "bearish": False},
                "obv_divergence": {"bullish": False, "bearish": False},
                "warnings": ["Datos insuficientes"]}

    row      = df.iloc[-2]   # ultima vela CERRADA
    prev_row = df.iloc[-3]
    price    = row["close"]

    conditions = {}
    score = 0
    warnings = []

    # ── FILTRO OBLIGATORIO: ADX ───────────────────────────────────────────────
    adx_value = row.get("ADX")
    market_trending = bool(adx_value >= 25) if pd.notna(adx_value) else False
    conditions["ADX > 25 (mercado en tendencia)"] = market_trending

    if not market_trending:
        warnings.append(
            f"ADX {round(adx_value,1) if pd.notna(adx_value) else '?'} < 25 "
            "— mercado lateral, señal poco confiable"
        )

    # ── CAPA 1: TENDENCIA (max 5 puntos) ─────────────────────────────────────

    # EMA cruce 20/50 — 3 puntos (mas confiable)
    ema_bull = price > row["EMA20"] and row["EMA20"] > row["EMA50"]
    ema_bear = price < row["EMA20"] and row["EMA20"] < row["EMA50"]
    conditions["EMA 20/50 alineadas (alcista)"] = ema_bull
    conditions["EMA 20/50 alineadas (bajista)"] = ema_bear
    score += 3 if ema_bull else -3 if ema_bear else 0

    # Bollinger MB — 1 punto
    bb_above = price > row["BB_MB"]
    conditions["Precio > Bollinger MB"] = bb_above
    score += 1 if bb_above else -1

    # VWAP diario — 1 punto
    if pd.notna(row.get("VWAP")):
        vwap_above = price > row["VWAP"]
        conditions["Precio > VWAP diario"] = vwap_above
        score += 1 if vwap_above else -1

    # BB Width — informativo, no suma puntos
    bb_squeeze = False
    if pd.notna(row.get("BB_WIDTH")):
        recent_widths = df["BB_WIDTH"].tail(100).dropna()
        if len(recent_widths) > 20:
            p20 = recent_widths.quantile(0.20)
            bb_squeeze = row["BB_WIDTH"] < p20
    conditions["Bollinger Squeeze (señal proxima)"] = bb_squeeze

    # ── CAPA 2: MOMENTUM (max 7 puntos) ──────────────────────────────────────

    # MACD: posicion + aceleracion — 2 puntos
    macd_bull = (
        row["MACD"] > row["MACD_SIGNAL"]
        and row["MACD_HIST"] > prev_row["MACD_HIST"]
    )
    macd_bear = (
        row["MACD"] < row["MACD_SIGNAL"]
        and row["MACD_HIST"] < prev_row["MACD_HIST"]
    )
    conditions["MACD bullish (linea > señal + acelerando)"] = macd_bull
    conditions["MACD bearish (linea < señal + acelerando)"] = macd_bear
    score += 2 if macd_bull else -2 if macd_bear else 0

    # StochRSI — 1 punto
    stoch_bull = False
    stoch_bear = False
    if pd.notna(row.get("STOCH_K")) and pd.notna(row.get("STOCH_D")):
        stoch_bull = row["STOCH_K"] > row["STOCH_D"] and row["STOCH_K"] < 80
        stoch_bear = row["STOCH_K"] < row["STOCH_D"] and row["STOCH_K"] > 20
    conditions["StochRSI alcista (K>D, no sobrecompra)"] = stoch_bull
    conditions["StochRSI bajista (K<D, no sobreventa)"] = stoch_bear
    score += 1 if stoch_bull else -1 if stoch_bear else 0

    # RSI20 nivel — 1 punto
    rsi20_bull = pd.notna(row["RSI20"]) and row["RSI20"] > 55
    rsi20_bear = pd.notna(row["RSI20"]) and row["RSI20"] < 45
    conditions["RSI(20) > 55"] = rsi20_bull
    conditions["RSI(20) < 45"] = rsi20_bear
    score += 1 if rsi20_bull else -1 if rsi20_bear else 0

    # Divergencias RSI — 3 puntos (muy alta confiabilidad)
    div = detect_rsi_divergence(df)
    conditions["Divergencia RSI alcista"] = div["bullish"]
    conditions["Divergencia RSI bajista"] = div["bearish"]
    if div["bullish"]:
        score += 3
    elif div["bearish"]:
        score -= 3

    # ── CAPA 3: VOLUMEN (max 4 puntos) ────────────────────────────────────────

    # OBV vs MA20 — 1 punto (peso REDUCIDO cuando tendencia es muy fuerte)
    # Razon: en bears fuertes el OBV puede mentir por short-covering
    obv_bull = False
    if pd.notna(row.get("OBV_MA20")):
        obv_bull = row["OBV"] > row["OBV_MA20"]
    conditions["OBV > MA20 (presion compradora)"] = obv_bull

    # Si ADX > 40, el OBV pesa menos (la tendencia macro manda)
    adx_for_weight = float(adx_value) if pd.notna(adx_value) else 0
    obv_weight = 0.5 if adx_for_weight > 40 else 1.0
    score += obv_weight if obv_bull else -obv_weight

    # Divergencias OBV — 2 puntos (mas confiable que RSI por usar volumen real)
    # Igual: reducido a 1 si ADX > 40 (tendencia macro debe pesar mas)
    obv_div = detect_obv_divergence(df)
    conditions["Divergencia OBV alcista (acumulacion)"] = obv_div["bullish"]
    conditions["Divergencia OBV bajista (distribucion)"] = obv_div["bearish"]
    div_weight = 1.0 if adx_for_weight > 40 else 2.0
    if obv_div["bullish"]:
        score += div_weight
    elif obv_div["bearish"]:
        score -= div_weight

    # Volumen fuerte — 1 punto (solo premia, no penaliza)
    vol_strong = False
    if pd.notna(row.get("VOL_MA10")):
        vol_strong = row["volume"] > row["VOL_MA10"] * 1.5
    conditions["Volumen fuerte (> MA10 x 1.5)"] = vol_strong
    score += 1 if vol_strong else 0

    # RSI6 alertas especiales (no suman puntos)
    rsi6_oversold   = pd.notna(row["RSI6"]) and row["RSI6"] < 25
    rsi6_overbought = pd.notna(row["RSI6"]) and row["RSI6"] > 75

    return {
        "score": score,
        "max_score": MAX_SCORE,
        "conditions": conditions,
        "market_trending": market_trending,
        "adx": round(float(adx_value), 1) if pd.notna(adx_value) else None,
        "di_pos": round(float(row["DI_POS"]), 1) if pd.notna(row.get("DI_POS")) else None,
        "di_neg": round(float(row["DI_NEG"]), 1) if pd.notna(row.get("DI_NEG")) else None,
        "rsi6_oversold": rsi6_oversold,
        "rsi6_overbought": rsi6_overbought,
        "bb_squeeze": bool(bb_squeeze),
        "divergence": div,
        "obv_divergence": obv_div,
        "warnings": warnings,
    }


def get_adaptive_threshold(adx: float) -> int:
    """
    Umbral dinamico segun fuerza de tendencia (ADX).

    Logica: cuando el mercado grita en una direccion, no necesitamos 9 puntos
    para entrar. Cuando esta indeciso, somos mas estrictos.

    - ADX > 40: tendencia extremadamente fuerte → umbral 7 (no perder el move)
    - ADX 30-40: tendencia clara              → umbral 9 (base)
    - ADX 20-30: tendencia debil               → umbral 10 (mas estricto)
    - ADX < 20: lateral                        → umbral 12 (muy estricto)
    """
    if pd.isna(adx) or adx == 0:
        return 12  # sin datos = maximo cuidado
    if adx > 40:
        return 7
    if adx >= 30:
        return 9
    if adx >= 20:
        return 10
    return 12


# ── Señal final (incluye confirmacion 1H y filtros multi-timeframe) ──────────

def get_signal(score: float, trend_1h: str, trend_1d: str, trend_1w: str,
               market_trending: bool, adx: float = 0) -> dict:
    """
    Logica de señal con 4 filtros: ADX, 1W, 1D, 1H.
    Umbral adaptativo segun fuerza de tendencia.
    """
    reasons = []
    threshold = get_adaptive_threshold(adx)

    # Filtro 1: mercado en tendencia
    if not market_trending:
        return {"signal": "NEUTRAL",
                "reason": "Mercado lateral (ADX < 25)",
                "confidence": "—",
                "threshold_used": threshold}

    # Direccion segun score
    if score >= threshold:
        raw = "LONG"
    elif score <= -threshold:
        raw = "SHORT"
    else:
        return {"signal": "NEUTRAL",
                "reason": f"Score insuficiente ({score:.1f}, umbral {threshold} para ADX={adx:.1f})",
                "confidence": "—",
                "threshold_used": threshold}

    # Filtro 2: tendencia 1W
    if raw == "LONG" and trend_1w == "BAJISTA":
        return {"signal": "NEUTRAL",
                "reason": "Contra tendencia semanal (BAJISTA)",
                "confidence": "—",
                "threshold_used": threshold}
    if raw == "SHORT" and trend_1w == "ALCISTA":
        return {"signal": "NEUTRAL",
                "reason": "Contra tendencia semanal (ALCISTA)",
                "confidence": "—",
                "threshold_used": threshold}

    # Filtro 3: tendencia 1D
    if raw == "LONG" and trend_1d == "BAJISTA":
        return {"signal": "NEUTRAL",
                "reason": "Contra tendencia diaria",
                "confidence": "—",
                "threshold_used": threshold}
    if raw == "SHORT" and trend_1d == "ALCISTA":
        return {"signal": "NEUTRAL",
                "reason": "Contra tendencia diaria",
                "confidence": "—",
                "threshold_used": threshold}

    # Filtro 4: confirmacion 1H
    target = "ALCISTA" if raw == "LONG" else "BAJISTA"
    confirms_1h = trend_1h == target

    # Calculo de confianza (relativo al umbral usado)
    abs_score = abs(score)
    aligned_1d_1w = (trend_1d == trend_1w) and trend_1d in ("ALCISTA", "BAJISTA")

    # Score excepcional (1.3× umbral) + alineacion + confirmacion = ALTA
    if abs_score >= threshold * 1.3 and aligned_1d_1w and confirms_1h:
        confidence = "ALTA"
    elif abs_score >= threshold and confirms_1h:
        confidence = "MEDIA"
    else:
        confidence = "BAJA"
        reasons.append("1H no confirma — esperar timing")

    return {"signal": raw,
            "reason": " | ".join(reasons) if reasons else "",
            "confidence": confidence,
            "threshold_used": threshold}


# ── Señal de cierre (logica de ESTADO, no de cruce momentaneo) ───────────────

def detect_trend_reversal(df: pd.DataFrame, current_regime: str) -> dict:
    """
    Detecta AGOTAMIENTO / posible giro de tendencia ANTES de que se confirme.

    Motivacion: los datos mostraron que las señales SHORT envejecen mal (ganan
    en 4H, pierden en 72H). Esto pasa porque la tendencia se agota y rebota.
    Este detector busca señales tempranas de ese agotamiento.

    Combina 5 factores objetivos (cada uno suma al reversal_score 0-100):
      1. ADX cayendo desde un pico (la tendencia pierde fuerza motriz)  -> 25
      2. Divergencia activa (RSI u OBV) contra la tendencia              -> 25
      3. Cruce DI (DI+ y DI- se cruzan, cambio de dominancia)            -> 20
      4. RSI(6) en extremo (sobrecompra en bull / sobreventa en bear)    -> 15
      5. MACD histograma desacelerando (momentum perdiendo fuerza)       -> 15

    Devuelve:
      reversal_score: 0-100 (mayor = mas probable que la tendencia gire)
      reversing: bool (score >= 50)
      direction: hacia donde giraria ("ALCISTA"/"BAJISTA"/None)
      signals: lista de razones detectadas
    """
    if len(df) < 20:
        return {"reversal_score": 0, "reversing": False, "direction": None, "signals": []}

    row = df.iloc[-2]
    score = 0
    signals = []

    # Direccion de la tendencia actual (a partir del regimen)
    is_bull = "BULL" in (current_regime or "")
    is_bear = "BEAR" in (current_regime or "")

    # ── Factor 1: ADX cayendo desde un pico ───────────────────────────────
    # Si el ADX viene de un maximo reciente y ahora baja, la tendencia se enfria
    adx_series = df["ADX"].dropna()
    if len(adx_series) >= 6:
        adx_now = adx_series.iloc[-2]
        adx_peak = adx_series.iloc[-6:-1].max()
        if pd.notna(adx_now) and pd.notna(adx_peak) and adx_peak > 25:
            drop = adx_peak - adx_now
            if drop >= 4:  # cayo al menos 4 puntos desde el pico (agotamiento temprano)
                score += 25
                signals.append(f"ADX cayendo desde pico ({adx_peak:.0f}->{adx_now:.0f}): tendencia perdiendo fuerza")

    # ── Factor 2: Divergencia contra la tendencia ─────────────────────────
    rsi_div = detect_rsi_divergence(df)
    obv_div = detect_obv_divergence(df)
    if is_bear and (rsi_div.get("bullish") or obv_div.get("bullish")):
        score += 25
        tipo = "RSI" if rsi_div.get("bullish") else "OBV"
        signals.append(f"Divergencia {tipo} alcista en tendencia bajista: posible piso")
    elif is_bull and (rsi_div.get("bearish") or obv_div.get("bearish")):
        score += 25
        tipo = "RSI" if rsi_div.get("bearish") else "OBV"
        signals.append(f"Divergencia {tipo} bajista en tendencia alcista: posible techo")

    # ── Factor 3: Cruce de DI (cambio de dominancia direccional) ──────────
    if len(df) >= 4 and all(c in df.columns for c in ["DI_POS", "DI_NEG"]):
        di_pos_now = df["DI_POS"].iloc[-2]
        di_neg_now = df["DI_NEG"].iloc[-2]
        di_pos_prev = df["DI_POS"].iloc[-4]
        di_neg_prev = df["DI_NEG"].iloc[-4]
        if all(pd.notna(x) for x in [di_pos_now, di_neg_now, di_pos_prev, di_neg_prev]):
            # En bear: DI+ cruza por encima de DI- = giro alcista
            if is_bear and di_pos_prev < di_neg_prev and di_pos_now > di_neg_now:
                score += 20
                signals.append("Cruce DI+ sobre DI-: presion compradora tomando control")
            # En bull: DI- cruza por encima de DI+ = giro bajista
            elif is_bull and di_neg_prev < di_pos_prev and di_neg_now > di_pos_now:
                score += 20
                signals.append("Cruce DI- sobre DI+: presion vendedora tomando control")

    # ── Factor 4: RSI(6) en extremo ───────────────────────────────────────
    rsi6 = row.get("RSI6")
    if pd.notna(rsi6):
        if is_bear and rsi6 < 20:
            score += 15
            signals.append(f"RSI(6) en sobreventa extrema ({rsi6:.0f}): caida madura, rebote probable")
        elif is_bull and rsi6 > 80:
            score += 15
            signals.append(f"RSI(6) en sobrecompra extrema ({rsi6:.0f}): subida madura, correccion probable")

    # ── Factor 5: MACD histograma desacelerando ───────────────────────────
    if "MACD_HIST" in df.columns and len(df) >= 4:
        h_now = df["MACD_HIST"].iloc[-2]
        h_prev = df["MACD_HIST"].iloc[-3]
        h_prev2 = df["MACD_HIST"].iloc[-4]
        if all(pd.notna(x) for x in [h_now, h_prev, h_prev2]):
            # En bear el hist es negativo; si se achica (sube hacia 0) = desacelera la caida
            if is_bear and h_now < 0 and h_now > h_prev > h_prev2:
                score += 15
                signals.append("MACD histograma contrayendose: momentum bajista debilitando")
            elif is_bull and h_now > 0 and h_now < h_prev < h_prev2:
                score += 15
                signals.append("MACD histograma contrayendose: momentum alcista debilitando")

    # Direccion del giro potencial
    direction = None
    if score >= 50:
        if is_bear:
            direction = "ALCISTA"
        elif is_bull:
            direction = "BAJISTA"

    return {
        "reversal_score": score,
        "reversing": score >= 50,
        "direction": direction,
        "signals": signals,
    }


def get_close_signal(df: pd.DataFrame, open_signal: str) -> dict:
    """
    Detecta condiciones de cierre evaluando ESTADO ACTUAL.
    No requiere cruces exactos en la vela actual.
    """
    if len(df) < 3:
        return {"should_close": False, "urgency": "BAJA", "reasons": []}

    row   = df.iloc[-2]
    price = row["close"]
    reasons = []

    if open_signal == "LONG":
        # MACD bajo señal — estado bajista
        if pd.notna(row["MACD"]) and pd.notna(row["MACD_SIGNAL"]):
            if row["MACD"] < row["MACD_SIGNAL"]:
                reasons.append("MACD por debajo de su señal")

        # Precio bajo EMA20
        if pd.notna(row["EMA20"]) and price < row["EMA20"]:
            reasons.append("Precio por debajo de EMA20")

        # Sobrecompra extrema = momento de tomar ganancia
        if pd.notna(row["RSI6"]) and row["RSI6"] > 75:
            reasons.append(f"RSI(6) sobrecompra ({round(row['RSI6'],1)})")

        # ADX confirma direccion contraria
        if (pd.notna(row.get("ADX")) and pd.notna(row.get("DI_NEG"))
                and pd.notna(row.get("DI_POS"))):
            if row["ADX"] > 25 and row["DI_NEG"] > row["DI_POS"]:
                reasons.append("ADX confirma fuerza bajista")

    elif open_signal == "SHORT":
        if pd.notna(row["MACD"]) and pd.notna(row["MACD_SIGNAL"]):
            if row["MACD"] > row["MACD_SIGNAL"]:
                reasons.append("MACD por encima de su señal")

        if pd.notna(row["EMA20"]) and price > row["EMA20"]:
            reasons.append("Precio por encima de EMA20")

        if pd.notna(row["RSI6"]) and row["RSI6"] < 25:
            reasons.append(f"RSI(6) sobreventa ({round(row['RSI6'],1)})")

        if (pd.notna(row.get("ADX")) and pd.notna(row.get("DI_NEG"))
                and pd.notna(row.get("DI_POS"))):
            if row["ADX"] > 25 and row["DI_POS"] > row["DI_NEG"]:
                reasons.append("ADX confirma fuerza alcista")

    n = len(reasons)
    should_close = n >= 2
    urgency = "ALTA" if n >= 3 else "MEDIA" if n == 2 else "BAJA"

    return {"should_close": should_close, "urgency": urgency, "reasons": reasons}


# ── Niveles ATR ──────────────────────────────────────────────────────────────

def get_levels(df: pd.DataFrame) -> dict:
    """Stops y TPs escalonados (perfil medio-agresivo)."""
    last  = df.iloc[-2]
    price = last["close"]
    atr   = last["ATR"]

    sl_mult, tp1_mult, tp2_mult, tp3_mult = 1.5, 2.5, 4.0, 6.0

    return {
        "stop_long":  round(price - sl_mult * atr, 4),
        "tp1_long":   round(price + tp1_mult * atr, 4),
        "tp2_long":   round(price + tp2_mult * atr, 4),
        "tp3_long":   round(price + tp3_mult * atr, 4),
        "stop_short": round(price + sl_mult * atr, 4),
        "tp1_short":  round(price - tp1_mult * atr, 4),
        "tp2_short":  round(price - tp2_mult * atr, 4),
        "tp3_short":  round(price - tp3_mult * atr, 4),
        "atr":        round(float(atr), 4),
        "rr_ratio":   round(tp1_mult / sl_mult, 2),
    }


def calc_position_size(price: float, stop: float, capital: float = 10000,
                       risk_pct: float = 0.025) -> dict:
    """Tamaño de posicion para riesgo de 2.5% del capital."""
    risk_amount   = capital * risk_pct
    stop_distance = abs(price - stop) / price
    position_size = risk_amount / stop_distance if stop_distance > 0 else 0
    contracts     = position_size / price if price > 0 else 0
    return {
        "risk_amount":   round(risk_amount, 2),
        "stop_distance": round(stop_distance * 100, 2),
        "position_usd":  round(position_size, 2),
        "contracts":     round(contracts, 4),
    }


# ── Analisis completo ─────────────────────────────────────────────────────────

def analyze(name: str, symbol: str) -> dict:
    try:
        df_1h = fetch_candles(symbol, "1h", limit=200)
        time.sleep(0.5)
        df_4h = fetch_candles(symbol, "4h", limit=300)
        time.sleep(0.5)
        df_1d = fetch_candles(symbol, "1d", limit=300)
        time.sleep(0.5)
        df_1w = fetch_candles(symbol, "1w", limit=100)

        df_1h = calculate_indicators(df_1h)
        df_4h = calculate_indicators(df_4h)
        df_1d = calculate_indicators(df_1d)
        df_1w = calculate_indicators(df_1w)

        # VWAP solo tiene sentido en 4H y menores
        df_4h = add_daily_vwap(df_4h)
        df_1h = add_daily_vwap(df_1h)

        trend_1h = get_trend(df_1h.iloc[-2])
        trend_1d = get_trend(df_1d.iloc[-2])
        trend_1w = get_trend(df_1w.iloc[-2])

        score_data = calculate_score(df_4h)
        adx_val = score_data["adx"] or 0

        # Funding rate (Binance) - sentimiento de perpetuos
        funding = fetch_funding_rate(name)

        # Aplicar ajuste de funding al score si esta disponible
        if funding.get("available") and funding.get("score_adj"):
            score_data["score"] += funding["score_adj"]
            # Marcar la condicion para visibilidad
            if funding["score_adj"] > 0:
                score_data["conditions"]["Funding bajista extremo (sobreventa emocional)"] = True
            else:
                score_data["conditions"]["Funding alcista extremo (sobrecompra emocional)"] = True

        signal_data = get_signal(
            score_data["score"], trend_1h, trend_1d, trend_1w,
            score_data["market_trending"], adx_val
        )

        # Régimen del mercado (diagnóstico independiente de la señal)
        regime_data = get_regime(trend_1w, trend_1d, trend_1h, adx_val)

        levels = get_levels(df_4h)
        pivots = calculate_pivots(df_4h)
        last   = df_4h.iloc[-2]
        price  = round(float(last["close"]), 4)

        close_long  = get_close_signal(df_4h, "LONG")
        close_short = get_close_signal(df_4h, "SHORT")
        reversal = detect_trend_reversal(df_4h, regime_data["regime"])

        target_for_signal = "ALCISTA" if "LONG" in signal_data["signal"] else "BAJISTA" if "SHORT" in signal_data["signal"] else None
        confirm_1h = (trend_1h == target_for_signal) if target_for_signal else False

        return {
            "name": name,
            "symbol": symbol,
            "price": price,
            "score": round(score_data["score"], 1),
            "max_score": score_data["max_score"],
            "signal": signal_data["signal"],
            "signal_reason": signal_data["reason"],
            "confidence": signal_data["confidence"],
            "threshold_used": signal_data.get("threshold_used", SIGNAL_THRESHOLD),
            "regime": regime_data["regime"],
            "regime_description": regime_data["description"],
            "regime_bias": regime_data["bias"],
            "regime_strength": regime_data["strength_score"],
            "trend_1h": trend_1h,
            "trend_1d": trend_1d,
            "trend_1w": trend_1w,
            "confirm_1h": confirm_1h,
            "market_trending": score_data["market_trending"],
            "adx": score_data["adx"],
            "di_pos": score_data["di_pos"],
            "di_neg": score_data["di_neg"],
            "conditions": score_data["conditions"],
            "divergence": score_data["divergence"],
            "obv_divergence": score_data["obv_divergence"],
            "bb_squeeze": score_data["bb_squeeze"],
            "warnings": score_data["warnings"],
            "funding": funding,
            "rsi6":  round(float(last["RSI6"]), 1)  if pd.notna(last["RSI6"])  else None,
            "rsi14": round(float(last["RSI14"]), 1) if pd.notna(last["RSI14"]) else None,
            "rsi20": round(float(last["RSI20"]), 1) if pd.notna(last["RSI20"]) else None,
            "rsi6_oversold": score_data["rsi6_oversold"],
            "rsi6_overbought": score_data["rsi6_overbought"],
            "macd":      round(float(last["MACD"]), 4)      if pd.notna(last["MACD"]) else None,
            "macd_hist": round(float(last["MACD_HIST"]), 4) if pd.notna(last["MACD_HIST"]) else None,
            "ema20":     round(float(last["EMA20"]), 4)     if pd.notna(last["EMA20"]) else None,
            "ema50":     round(float(last["EMA50"]), 4)     if pd.notna(last["EMA50"]) else None,
            "bb_up":     round(float(last["BB_UP"]), 4)     if pd.notna(last["BB_UP"]) else None,
            "bb_mb":     round(float(last["BB_MB"]), 4)     if pd.notna(last["BB_MB"]) else None,
            "bb_dn":     round(float(last["BB_DN"]), 4)     if pd.notna(last["BB_DN"]) else None,
            "bb_width":  round(float(last["BB_WIDTH"]), 4)  if pd.notna(last["BB_WIDTH"]) else None,
            "obv_trend": "ALCISTA" if (pd.notna(last.get("OBV_MA20")) and last["OBV"] > last["OBV_MA20"])
                         else "BAJISTA" if pd.notna(last.get("OBV_MA20")) else "—",
            "vwap":      round(float(last["VWAP"]), 4) if pd.notna(last.get("VWAP")) else None,
            "levels":    levels,
            "pivots":    pivots,
            "close_long":  close_long,
            "close_short": close_short,
            "reversal":    reversal,
            "updated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
            "error": None,
        }

    except Exception as e:
        return {"name": name, "symbol": symbol, "error": str(e)}


def apply_btc_correlation(results: list) -> list:
    """
    Filtro post-analisis: si BTC esta en bear/bull fuerte, ajusta las señales
    de altcoins porque historicamente todos los altcoins siguen a BTC.

    - BTC en SHORT con confianza ALTA → bloquea LONGs en alts (cambia a NEUTRAL)
    - BTC en LONG con confianza ALTA  → bloquea SHORTs en alts
    - BTC en regimen BEAR FUERTE      → degrada confianza de LONGs alt
    - BTC en regimen BULL FUERTE      → degrada confianza de SHORTs alt
    """
    btc = next((r for r in results if r.get("name") == "BTC" and not r.get("error")), None)
    if not btc:
        return results  # sin BTC, no aplicamos filtro

    btc_signal = btc.get("signal", "NEUTRAL")
    btc_conf   = btc.get("confidence", "—")
    btc_regime = btc.get("regime", "LATERAL")

    for r in results:
        if r.get("name") == "BTC" or r.get("error"):
            continue

        # Caso 1: bloqueo total — BTC señal fuerte contra señal de altcoin
        if "SHORT" in btc_signal and btc_conf in ("ALTA", "MEDIA") and "LONG" in r["signal"]:
            r["btc_filter_applied"] = True
            r["btc_filter_reason"] = f"BTC en SHORT ({btc_conf}) — alts no rompen contra BTC"
            r["signal_original"] = r["signal"]
            r["confidence_original"] = r["confidence"]
            r["signal"] = "NEUTRAL"
            r["confidence"] = "—"
            r["signal_reason"] = (r.get("signal_reason", "") + f" | {r['btc_filter_reason']}").strip(" |")
            continue

        if "LONG" in btc_signal and btc_conf in ("ALTA", "MEDIA") and "SHORT" in r["signal"]:
            r["btc_filter_applied"] = True
            r["btc_filter_reason"] = f"BTC en LONG ({btc_conf}) — alts no rompen contra BTC"
            r["signal_original"] = r["signal"]
            r["confidence_original"] = r["confidence"]
            r["signal"] = "NEUTRAL"
            r["confidence"] = "—"
            r["signal_reason"] = (r.get("signal_reason", "") + f" | {r['btc_filter_reason']}").strip(" |")
            continue

        # Caso 2: degradacion de confianza — BTC regimen contrario a la señal del alt
        if "BEAR FUERTE" in btc_regime and "LONG" in r["signal"]:
            if r["confidence"] == "ALTA":
                r["confidence"] = "MEDIA"
            elif r["confidence"] == "MEDIA":
                r["confidence"] = "BAJA"
            r["btc_filter_applied"] = True
            r["btc_filter_reason"] = "BTC en bear fuerte — confianza degradada"

        if "BULL FUERTE" in btc_regime and "SHORT" in r["signal"]:
            if r["confidence"] == "ALTA":
                r["confidence"] = "MEDIA"
            elif r["confidence"] == "MEDIA":
                r["confidence"] = "BAJA"
            r["btc_filter_applied"] = True
            r["btc_filter_reason"] = "BTC en bull fuerte — confianza degradada"

    return results


def build_scenarios(r: dict) -> dict:
    """
    Genera lectura por capas (jerarquia multi-timeframe) + escenarios
    condicionales con niveles reales + que timeframe vigilar.

    Principio: el marco largo manda (fondo), el corto da timing. Un rebote 1H
    dentro de un 1W bajista es una oscilacion subordinada, no un cambio de tendencia.

    NO es prediccion: son condiciones determinadas por los niveles que el
    mercado ya marco. Si pasa X (que el precio rompa un nivel real), entonces Y.
    """
    if r.get("error"):
        return None

    price = r.get("price")
    if price is None:
        return None

    t1w = r.get("trend_1w", "LATERAL")
    t1d = r.get("trend_1d", "LATERAL")
    t1h = r.get("trend_1h", "LATERAL")
    regime = r.get("regime", "")

    # ── Capas (jerarquia) ─────────────────────────────────────────────────
    def label(tf):
        return {"ALCISTA": "alcista", "BAJISTA": "bajista", "LATERAL": "lateral"}.get(tf, "—")

    layers = {
        "fondo":      {"tf": "1W", "trend": label(t1w), "rol": "la marea grande, marca la dirección dominante"},
        "estructura": {"tf": "1D", "trend": label(t1d), "rol": "el medio plazo, confirma o frena al fondo"},
        "timing":     {"tf": "1H", "trend": label(t1h), "rol": "el corto plazo, da el momento de entrada"},
    }

    # ── Interpretacion de la jerarquia ────────────────────────────────────
    interpretacion = ""
    if t1w == "BAJISTA" and t1h == "ALCISTA":
        if t1d == "BAJISTA":
            interpretacion = ("El fondo es claramente bajista y el rebote de corto plazo va contra esa marea: "
                              "lo más probable es que sea una oscilación temporal (rebote técnico) dentro de la caída mayor, "
                              "no un cambio de dirección.")
        else:
            interpretacion = ("Fondo bajista con un rebote de corto plazo en curso. Mientras el medio plazo (1D) no gire alcista, "
                              "el rebote sigue siendo subordinado a la tendencia bajista mayor.")
    elif t1w == "ALCISTA" and t1h == "BAJISTA":
        if t1d == "ALCISTA":
            interpretacion = ("El fondo es claramente alcista y la baja de corto plazo va contra esa marea: "
                              "lo más probable es que sea una corrección temporal dentro de la subida mayor, no un cambio de dirección.")
        else:
            interpretacion = ("Fondo alcista con una corrección de corto plazo en curso. Mientras el medio plazo (1D) no gire bajista, "
                              "la corrección sigue subordinada a la tendencia alcista mayor.")
    elif t1w == t1d == t1h and t1w in ("ALCISTA", "BAJISTA"):
        dir_txt = "alcista" if t1w == "ALCISTA" else "bajista"
        interpretacion = (f"Las tres capas están alineadas en dirección {dir_txt}: es la situación de mayor convicción, "
                          f"el corto plazo confirma la tendencia de fondo.")
    elif t1w == "LATERAL" and t1d == "LATERAL":
        interpretacion = ("El fondo y el medio plazo están laterales: el mercado no tiene tendencia dominante. "
                          "Los movimientos de corto plazo (1H) son ruido dentro de un rango hasta que el 1D defina dirección.")
    else:
        interpretacion = ("Las capas no están alineadas: el mercado está en transición. "
                          "Conviene esperar a que el medio plazo (1D) defina hacia dónde se inclina antes de operar con la tendencia.")

    # ── Escenarios condicionales con niveles reales ───────────────────────
    # Junto todos los niveles relevantes y ubico el inmediato arriba/abajo
    niveles = []
    def add_level(val, nombre):
        if val is not None and val > 0:
            niveles.append((float(val), nombre))

    piv = r.get("pivots", {})
    add_level(r.get("ema20"), "EMA20")
    add_level(r.get("ema50"), "EMA50")
    add_level(r.get("vwap"), "VWAP")
    add_level(piv.get("r1"), "pivot R1")
    add_level(piv.get("r2"), "pivot R2")
    add_level(piv.get("s1"), "pivot S1")
    add_level(piv.get("s2"), "pivot S2")
    add_level(r.get("bb_up"), "banda superior Bollinger")
    add_level(r.get("bb_dn"), "banda inferior Bollinger")

    arriba = sorted([n for n in niveles if n[0] > price * 1.0015], key=lambda x: x[0])
    abajo = sorted([n for n in niveles if n[0] < price * 0.9985], key=lambda x: x[0], reverse=True)

    def fmt(v):
        return f"${v:,.2f}" if v >= 10 else f"${v:,.4f}"

    escenarios = []

    # Escenario alcista (ruptura del nivel inmediato superior)
    if arriba:
        nivel_val, nivel_nom = arriba[0]
        dist = (nivel_val / price - 1) * 100
        if t1w == "BAJISTA" or t1d == "BAJISTA":
            consecuencia = ("el rebote ganaría fuerza y habría que ver si contagia al 1D para girarlo alcista — "
                            "recién ahí el cambio de tendencia sería real, no solo un rebote")
        elif t1w == "ALCISTA":
            consecuencia = "la tendencia alcista de fondo se reforzaría y el avance tendría continuidad"
        else:
            consecuencia = "el mercado intentaría definir dirección al alza desde el rango actual"
        escenarios.append({
            "tipo": "alcista",
            "texto": f"Si rompe y sostiene arriba de {fmt(nivel_val)} ({nivel_nom}, +{dist:.1f}%), {consecuencia}.",
        })

    # Escenario bajista (perdida del nivel inmediato inferior)
    if abajo:
        nivel_val, nivel_nom = abajo[0]
        dist = (1 - nivel_val / price) * 100
        if t1w == "ALCISTA" or t1d == "ALCISTA":
            consecuencia = ("la corrección se profundizaría y habría que ver si contagia al 1D para girarlo bajista — "
                            "recién ahí el cambio de tendencia sería real, no solo una corrección")
        elif t1w == "BAJISTA":
            consecuencia = "la tendencia bajista de fondo retomaría el control y la caída tendría continuidad"
        else:
            consecuencia = "el mercado intentaría definir dirección a la baja desde el rango actual"
        escenarios.append({
            "tipo": "bajista",
            "texto": f"Si pierde {fmt(nivel_val)} ({nivel_nom}, -{dist:.1f}%), {consecuencia}.",
        })

    # ── Que timeframe vigilar ─────────────────────────────────────────────
    if t1w == "BAJISTA" and t1h == "ALCISTA":
        vigilar = ("Vigilá el 1D: es la bisagra. Si el 1D pasa de lateral/bajista a alcista, el rebote deja de ser "
                   "oscilación y empieza a ser cambio de tendencia real.")
    elif t1w == "ALCISTA" and t1h == "BAJISTA":
        vigilar = ("Vigilá el 1D: es la bisagra. Si el 1D pasa de lateral/alcista a bajista, la corrección deja de ser "
                   "temporal y empieza a ser cambio de tendencia real.")
    elif t1w == t1d == t1h and t1w in ("ALCISTA", "BAJISTA"):
        vigilar = ("Las tres capas ya están alineadas. Vigilá el 1H para timing de entrada y el ADX: "
                   "si el ADX baja, la tendencia pierde fuerza.")
    else:
        vigilar = ("Vigilá el 1D: mientras siga lateral, el mercado no tiene dirección dominante y los movimientos "
                   "de 1H son ruido dentro del rango.")

    return {
        "layers": layers,
        "interpretacion": interpretacion,
        "escenarios": escenarios,
        "vigilar": vigilar,
    }


def build_asset_summary(r: dict) -> str:
    """
    Genera un resumen en lenguaje claro (2-3 frases) interpretando los
    indicadores que ya tenemos. Es traduccion determinista, NO prediccion.

    Estructura: [diagnostico de regimen] + [que lo confirma/contradice] +
                [matiz accionable o de cautela].
    """
    if r.get("error"):
        return "Sin datos suficientes para este activo."

    regime = r.get("regime", "")
    score = r.get("score", 0)
    signal = r.get("signal", "NEUTRAL")
    rsi6 = r.get("rsi6")
    obv = r.get("obv_trend", "")
    t1w, t1d, t1h = r.get("trend_1w"), r.get("trend_1d"), r.get("trend_1h")
    bb_squeeze = r.get("bb_squeeze", False)
    funding = r.get("funding", {})
    obv_div = r.get("obv_divergence", {})
    rsi_div = r.get("divergence", {})

    frases = []

    # ── Frase 1: diagnostico del regimen ──────────────────────────────────
    if "BULL FUERTE" in regime:
        frases.append("Tendencia alcista fuerte y alineada en todos los marcos temporales.")
    elif "BULL" in regime:
        frases.append("Mercado en tendencia alcista, con sesgo comprador predominante.")
    elif "BEAR FUERTE" in regime:
        frases.append("Tendencia bajista fuerte y alineada en todos los marcos temporales.")
    elif "BEAR" in regime:
        frases.append("Mercado en tendencia bajista, con sesgo vendedor predominante.")
    elif "TRANSICI" in regime:
        # Detallar la transicion segun los timeframes
        if t1w == "BAJISTA" and t1h == "ALCISTA":
            frases.append("Estructura bajista de fondo con un rebote en curso en el corto plazo.")
        elif t1w == "ALCISTA" and t1h == "BAJISTA":
            frases.append("Estructura alcista de fondo con una corrección en curso en el corto plazo.")
        else:
            frases.append("Mercado en transición: los marcos temporales no están alineados.")
    elif "LATERAL ESTRICTO" in regime:
        frases.append("Mercado sin tendencia, rangeando con baja fuerza direccional.")
    else:
        frases.append("Mercado lateral, sin convicción clara en ninguna dirección.")

    # ── Frase 2: que confirma o contradice ────────────────────────────────
    confirmaciones = []
    contradicciones = []

    # OBV
    if obv == "ALCISTA":
        (confirmaciones if "BULL" in regime else contradicciones if "BEAR" in regime else confirmaciones).append("volumen comprador (OBV alcista)")
    elif obv == "BAJISTA":
        (confirmaciones if "BEAR" in regime else contradicciones if "BULL" in regime else contradicciones).append("volumen vendedor (OBV bajista)")

    # Divergencias (señales de posible giro)
    if obv_div.get("bullish"):
        contradicciones.append("acumulación detectada (divergencia OBV alcista)")
    if obv_div.get("bearish"):
        contradicciones.append("distribución detectada (divergencia OBV bajista)")
    if rsi_div.get("bullish"):
        contradicciones.append("divergencia RSI alcista (posible piso)")
    if rsi_div.get("bearish"):
        contradicciones.append("divergencia RSI bajista (posible techo)")

    # Funding (segun si apoya o contradice el regimen)
    if funding.get("available"):
        imp = funding.get("signal_impact")
        sent = funding.get("sentiment", "").lower()
        regime_alc = "BULL" in regime
        regime_baj = "BEAR" in regime
        if imp == "BULLISH":
            # funding negativo = presion alcista latente
            if regime_alc:
                confirmaciones.append(f"funding negativo ({sent})")
            else:
                contradicciones.append(f"funding negativo ({sent})")
        elif imp == "BEARISH":
            # funding alto = presion bajista latente
            if regime_baj:
                confirmaciones.append(f"funding alto ({sent})")
            else:
                contradicciones.append(f"funding alto ({sent})")

    if confirmaciones and not contradicciones:
        frases.append(f"Lo confirma el {', '.join(confirmaciones)}.")
    elif contradicciones and not confirmaciones:
        frases.append(f"Pero hay señales en contra: {', '.join(contradicciones)}.")
    elif confirmaciones and contradicciones:
        frases.append(f"Señales mixtas: a favor {', '.join(confirmaciones)}; en contra {', '.join(contradicciones)}.")

    # ── Frase 3: matiz accionable / cautela ───────────────────────────────
    matiz = []
    if "LONG" in signal:
        matiz.append(f"El bot emite señal de COMPRA (confianza {r.get('confidence','').lower()}).")
    elif "SHORT" in signal:
        matiz.append(f"El bot emite señal de VENTA (confianza {r.get('confidence','').lower()}).")
    else:
        # No hay señal: explicar por que
        if bb_squeeze:
            matiz.append("Volatilidad comprimida (Bollinger Squeeze): se acerca un movimiento brusco, conviene esperar el quiebre.")
        elif rsi6 is not None and rsi6 > 70 and ("BULL" in regime or t1h == "ALCISTA"):
            matiz.append("El impulso de corto plazo está sobrecomprado (RSI6 alto): el avance puede estar maduro.")
        elif rsi6 is not None and rsi6 < 30 and ("BEAR" in regime or t1h == "BAJISTA"):
            matiz.append("El impulso de corto plazo está sobrevendido (RSI6 bajo): la caída puede estar madura.")
        else:
            matiz.append("Sin señal operativa: el balance de indicadores no alcanza el umbral, conviene esperar confirmación.")

    frases.append(matiz[0])

    return " ".join(frases)


def build_global_summary(results: list) -> dict:
    """
    Resumen global del mercado combinando los 4 activos.
    Se apoya en BTC como lider (los altcoins suelen seguirlo).
    """
    valid = [r for r in results if not r.get("error")]
    if not valid:
        return {"headline": "Sin datos", "detail": "No hay análisis disponible."}

    btc = next((r for r in valid if r.get("name") == "BTC"), None)

    # Contar direcciones de regimen
    def direction(regime):
        if "BULL" in (regime or ""): return "alcista"
        if "BEAR" in (regime or ""): return "bajista"
        return "neutro"

    dirs = [direction(r.get("regime")) for r in valid]
    n_alc = dirs.count("alcista")
    n_baj = dirs.count("bajista")
    n_neu = dirs.count("neutro")

    # Señales activas
    longs = [r["name"] for r in valid if "LONG" in r.get("signal", "")]
    shorts = [r["name"] for r in valid if "SHORT" in r.get("signal", "")]

    # Squeeze global
    squeezes = [r["name"] for r in valid if r.get("bb_squeeze")]

    # ── Headline ──────────────────────────────────────────────────────────
    if n_baj >= 3:
        headline = "Mercado predominantemente bajista"
    elif n_alc >= 3:
        headline = "Mercado predominantemente alcista"
    elif n_neu >= 3:
        headline = "Mercado lateral / en transición"
    else:
        headline = "Mercado mixto, sin consenso direccional"

    # ── Detalle (2-3 frases) ──────────────────────────────────────────────
    frases = []

    # Frase BTC (lider)
    if btc:
        btc_dir = direction(btc.get("regime"))
        btc_regime = btc.get("regime", "")
        frases.append(f"BTC, que marca el ritmo, está en régimen {btc_regime} ({btc_dir}).")

    # Frase reparto
    reparto = []
    if n_alc: reparto.append(f"{n_alc} alcista{'s' if n_alc>1 else ''}")
    if n_baj: reparto.append(f"{n_baj} bajista{'s' if n_baj>1 else ''}")
    if n_neu: reparto.append(f"{n_neu} neutro{'s' if n_neu>1 else ''}")
    frases.append(f"De los 4 activos: {', '.join(reparto)}.")

    # Frase señales / squeeze
    if longs or shorts:
        partes = []
        if longs: partes.append(f"compra en {', '.join(longs)}")
        if shorts: partes.append(f"venta en {', '.join(shorts)}")
        frases.append(f"Señales activas: {'; '.join(partes)}.")
    elif len(squeezes) >= 2:
        frases.append(f"Sin señales activas, pero {len(squeezes)} activos tienen volatilidad comprimida ({', '.join(squeezes)}): posible movimiento brusco próximo.")
    else:
        frases.append("Sin señales operativas activas: el mercado no ofrece entradas de alta convicción ahora.")

    return {
        "headline": headline,
        "detail": " ".join(frases),
    }


def run_analysis() -> list:
    results = []
    for name, symbol in SYMBOLS.items():
        print(f"  Analizando {name}...")
        results.append(analyze(name, symbol))
        time.sleep(1)

    # Aplicar filtro de correlacion con BTC (ajusta señales de altcoins)
    results = apply_btc_correlation(results)

    # Generar resumen por activo (despues del filtro BTC para reflejar señal final)
    for r in results:
        if not r.get("error"):
            r["summary"] = build_asset_summary(r)
            r["scenarios"] = build_scenarios(r)

    return results


if __name__ == "__main__":
    print("Corriendo analisis v2.1...")
    results = run_analysis()
    for r in results:
        if r["error"]:
            print(f"\nERROR {r['name']}: {r['error']}")
        else:
            print(f"\n{'='*60}")
            print(f"{r['name']} | ${r['price']} | Score: {r['score']}/{r['max_score']}")
            print(f"Señal: {r['signal']} | Confianza: {r['confidence']}")
            if r["signal_reason"]:
                print(f"Motivo: {r['signal_reason']}")
            print(f"ADX: {r['adx']} | Trending: {r['market_trending']}")
            print(f"Tendencias: 1H={r['trend_1h']} | 1D={r['trend_1d']} | 1W={r['trend_1w']}")
            print(f"Confirma 1H: {r['confirm_1h']}")
            print(f"Divergencia: alcista={r['divergence']['bullish']} | bajista={r['divergence']['bearish']}")
            print(f"OBV: {r['obv_trend']} | BB Squeeze: {r['bb_squeeze']}")
            if r["warnings"]:
                for w in r["warnings"]:
                    print(f"  AVISO: {w}")
            if r["close_short"]["should_close"]:
                print(f"  CERRAR SHORT ({r['close_short']['urgency']}): {r['close_short']['reasons']}")
            if r["close_long"]["should_close"]:
                print(f"  CERRAR LONG ({r['close_long']['urgency']}): {r['close_long']['reasons']}")
