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
}

KRAKEN_URL = "https://api.kraken.com/0/public/OHLC"
KRAKEN_INTERVALS = {"1h": 60, "4h": 240, "1d": 1440, "1w": 10080}

# Funding rate de Binance Futures USDT-M (publico, sin auth)
BINANCE_FUNDING_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
BINANCE_FUNDING_SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "XRP": "XRPUSDT",
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

def fetch_funding_rate(asset: str) -> dict:
    """
    Funding rate de Binance USDT-M perpetuos. Indicador de sentimiento real:

    - Funding POSITIVO: longs pagan a shorts → mercado sobrecomprado emocionalmente
      * > 0.05% (8h)  → señal de saturación alcista, posible reversión bajista
      * > 0.10% (8h)  → muy sobrecomprado, alta probabilidad de corrección

    - Funding NEGATIVO: shorts pagan a longs → mercado sobrevendido emocionalmente
      * < -0.05% (8h) → señal de capitulación, posible rebote alcista
      * < -0.10% (8h) → muy sobrevendido, alta probabilidad de rebote

    Binance cobra/paga funding cada 8h. Los valores aqui son por periodo (8h).

    Fallback: si Binance bloquea (error 451 en algunos hosts), devuelve None
    y el sistema sigue funcionando sin este indicador.
    """
    symbol = BINANCE_FUNDING_SYMBOLS.get(asset)
    if not symbol:
        return {"rate": None, "available": False, "reason": "Asset no soportado"}

    try:
        resp = requests.get(
            BINANCE_FUNDING_URL,
            params={"symbol": symbol},
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        # 451: bloqueo legal regional | 403: bloqueo por geo o WAF
        if resp.status_code in (451, 403):
            return {"rate": None, "available": False, "reason": f"Binance bloqueado ({resp.status_code})"}
        resp.raise_for_status()
        data = resp.json()

        # lastFundingRate viene como decimal, ej. 0.0001 = 0.01% por periodo de 8h
        rate = float(data.get("lastFundingRate", 0))
        rate_pct = rate * 100  # convertir a porcentaje

        # Clasificacion
        if rate_pct > 0.10:
            sentiment = "EUFORIA ALCISTA"
            signal_impact = "BEARISH"  # contra-señal
            score_adj = -1
        elif rate_pct > 0.05:
            sentiment = "Sobrecompra emocional"
            signal_impact = "BEARISH"
            score_adj = -1
        elif rate_pct < -0.10:
            sentiment = "CAPITULACIÓN BAJISTA"
            signal_impact = "BULLISH"  # contra-señal
            score_adj = 1
        elif rate_pct < -0.05:
            sentiment = "Sobreventa emocional"
            signal_impact = "BULLISH"
            score_adj = 1
        else:
            sentiment = "Neutral"
            signal_impact = "NEUTRAL"
            score_adj = 0

        return {
            "rate": round(rate_pct, 4),
            "rate_annualized": round(rate_pct * 3 * 365, 2),  # 3 periodos/dia × 365 dias
            "sentiment": sentiment,
            "signal_impact": signal_impact,
            "score_adj": score_adj,
            "available": True,
        }
    except Exception as e:
        return {"rate": None, "available": False, "reason": f"Error: {str(e)[:50]}"}


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


def run_analysis() -> list:
    results = []
    for name, symbol in SYMBOLS.items():
        print(f"  Analizando {name}...")
        results.append(analyze(name, symbol))
        time.sleep(1)

    # Aplicar filtro de correlacion con BTC (ajusta señales de altcoins)
    results = apply_btc_correlation(results)
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
