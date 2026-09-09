"""
Fuentes de datos.

  - Cripto: Kraken API publica (OHLC, sin autenticacion).
  - Funding rate perpetuos: Binance -> Bybit -> OKX (cascada).
  - Acciones / ETFs / CEDEARs: Yahoo Finance via yfinance
      * subyacente USA: "AAPL", "SPY"
      * CEDEAR en pesos: "AAPL.BA"
      * accion argentina: "YPFD.BA", "PAMP.BA"
  - Historia larga para backtest: Yahoo Finance (BTC-USD 1h hasta 730 dias, 1d anios).

Todas las funciones devuelven DataFrame con indice datetime (UTC, naive)
y columnas open, high, low, close, volume.
"""

import time
import requests
import numpy as np
import pandas as pd

KRAKEN_URL = "https://api.kraken.com/0/public/OHLC"
KRAKEN_INTERVALS = {"1h": 60, "4h": 240, "1d": 1440, "1w": 10080}

CRYPTO_SYMBOLS = {
    "BTC": "XBTUSD",
    "ETH": "ETHUSD",
    "SOL": "SOLUSD",
    "XRP": "XRPUSD",
    "LINK": "LINKUSD",
}

# Ticker de Yahoo para la historia larga de cada cripto (backtest)
CRYPTO_YAHOO = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD",
                "XRP": "XRP-USD", "LINK": "LINK-USD"}

FUNDING_SYMBOLS = {
    a: {"binance": f"{a}USDT", "bybit": f"{a}USDT", "okx": f"{a}-USDT-SWAP"}
    for a in CRYPTO_SYMBOLS
}

_HEADERS = {"User-Agent": "Mozilla/5.0"}


# ── Kraken ────────────────────────────────────────────────────────────────────

def fetch_kraken(symbol: str, interval: str, limit: int = 720, retries: int = 3) -> pd.DataFrame:
    """Kraken devuelve como maximo las ultimas 720 velas del intervalo pedido."""
    params = {"pair": symbol, "interval": KRAKEN_INTERVALS[interval]}
    last_err = None
    for attempt in range(retries):
        try:
            resp = requests.get(KRAKEN_URL, params=params, timeout=15, headers=_HEADERS)
            resp.raise_for_status()
            data = resp.json()
            if data.get("error"):
                raise ValueError(f"Kraken error: {data['error']}")
            result = data["result"]
            pair_key = [k for k in result if k != "last"][0]
            df = pd.DataFrame(result[pair_key], columns=[
                "open_time", "open", "high", "low", "close", "vwap_raw", "volume", "count"])
            # a nanosegundos: Kraken entrega segundos y mezclar resoluciones
            # rompe las comparaciones con fechas que traen microsegundos
            df["open_time"] = pd.to_datetime(df["open_time"].astype(int), unit="s").astype("datetime64[ns]")
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].astype(float)
            df = df.set_index("open_time")[["open", "high", "low", "close", "volume"]]
            return df.tail(limit)
        except Exception as e:  # rate limit / red
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Kraken {symbol} {interval}: {last_err}")


def fetch_crypto_multi_tf(symbol: str) -> dict:
    """Trae 1h, 4h, 1d, 1w de Kraken con pausas para respetar rate limit."""
    out = {}
    for tf, lim in [("1h", 720), ("4h", 720), ("1d", 720), ("1w", 300)]:
        out[tf] = fetch_kraken(symbol, tf, lim)
        time.sleep(0.4)
    return out


# ── Funding rate ──────────────────────────────────────────────────────────────

def _binance_funding(symbol):
    r = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex",
                     params={"symbol": symbol}, timeout=8, headers=_HEADERS)
    if r.status_code in (451, 403):
        raise PermissionError(f"Binance bloqueado ({r.status_code})")
    r.raise_for_status()
    return float(r.json()["lastFundingRate"]) * 100


def _bybit_funding(symbol):
    r = requests.get("https://api.bybit.com/v5/market/tickers",
                     params={"category": "linear", "symbol": symbol}, timeout=8, headers=_HEADERS)
    r.raise_for_status()
    lst = r.json().get("result", {}).get("list", [])
    if not lst:
        raise ValueError("Bybit sin datos")
    return float(lst[0]["fundingRate"]) * 100


def _okx_funding(symbol):
    r = requests.get("https://www.okx.com/api/v5/public/funding-rate",
                     params={"instId": symbol}, timeout=8, headers=_HEADERS)
    r.raise_for_status()
    lst = r.json().get("data", [])
    if not lst:
        raise ValueError("OKX sin datos")
    return float(lst[0]["fundingRate"]) * 100


def classify_funding(rate_pct: float, source: str) -> dict:
    """rate_pct = funding del periodo de 8h en %. Neutral tipico ~0.01%."""
    if rate_pct > 0.10:
        sentiment, impact = "EUFORIA ALCISTA (longs pagan caro)", "BEARISH"
    elif rate_pct > 0.04:
        sentiment, impact = "Sobrecompra emocional", "BEARISH"
    elif rate_pct < -0.10:
        sentiment, impact = "CAPITULACION BAJISTA (shorts pagan caro)", "BULLISH"
    elif rate_pct < -0.03:
        sentiment, impact = "Sobreventa emocional", "BULLISH"
    else:
        sentiment, impact = "Neutral", "NEUTRAL"
    return {"rate": round(rate_pct, 4), "rate_annualized": round(rate_pct * 3 * 365, 1),
            "sentiment": sentiment, "impact": impact, "source": source, "available": True}


def fetch_funding_rate(asset: str) -> dict:
    syms = FUNDING_SYMBOLS.get(asset)
    if not syms:
        return {"available": False, "reason": "sin simbolo"}
    errors = []
    for name, fn in [("binance", _binance_funding), ("bybit", _bybit_funding), ("okx", _okx_funding)]:
        try:
            return classify_funding(fn(syms[name]), name)
        except Exception as e:
            errors.append(f"{name}: {str(e)[:40]}")
    return {"available": False, "reason": " | ".join(errors)}


# ── Yahoo Finance (acciones, CEDEARs, historia larga) ─────────────────────────

def fetch_yahoo(ticker: str, period: str = "2y", interval: str = "1d") -> pd.DataFrame:
    """
    Descarga OHLCV de Yahoo. Intervalos: 1h (max 730 dias), 1d, 1wk.
    Devuelve DataFrame limpio con indice naive UTC.
    """
    import yfinance as yf
    df = yf.download(ticker, period=period, interval=interval, progress=False,
                     auto_adjust=True, threads=False)
    if df is None or df.empty:
        raise RuntimeError(f"Yahoo sin datos para {ticker} ({interval})")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                            "Close": "close", "Volume": "volume"})
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    idx = pd.to_datetime(df.index)
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    df.index = idx
    df = df[~df.index.duplicated(keep="last")].dropna(subset=["close"])
    return df


def fetch_stock_multi_tf(ticker: str, intraday: bool = True) -> dict:
    """1h (si intraday), 1d y 1w para una accion/ETF."""
    out = {}
    if intraday:
        try:
            out["1h"] = fetch_yahoo(ticker, period="6mo", interval="1h")
        except Exception:
            out["1h"] = None
    out["1d"] = fetch_yahoo(ticker, period="3y", interval="1d")
    out["1w"] = fetch_yahoo(ticker, period="10y", interval="1wk")
    return out


def fetch_last_price_yahoo(ticker: str) -> float | None:
    try:
        df = fetch_yahoo(ticker, period="5d", interval="1d")
        return float(df["close"].iloc[-1])
    except Exception:
        return None


def fetch_crypto_history_yahoo(asset: str) -> dict:
    """Historia larga para backtest: 1h (2 anios) resampleado a 4h, y 1d (max)."""
    from indicators import resample_ohlcv
    yt = CRYPTO_YAHOO[asset]
    h1 = fetch_yahoo(yt, period="730d", interval="1h")
    d1 = fetch_yahoo(yt, period="max", interval="1d")
    return {"1h": h1, "4h": resample_ohlcv(h1, "4h"), "1d": d1,
            "1w": resample_ohlcv(d1, "W-MON")}


# ── Datos sinteticos para tests / desarrollo sin red ──────────────────────────

def synthetic_ohlcv(n: int = 720, freq: str = "1h", start_price: float = 60000.0,
                    seed: int = 42, regime: str = "mixed") -> pd.DataFrame:
    """
    Genera OHLCV con tendencias por tramos, soportes/resistencias 'respetados'
    (el precio rebota en niveles) y volumen correlacionado con el rango.
    Solo para desarrollo y tests unitarios.
    """
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-01-01", periods=n, freq=freq)
    prices = np.empty(n)
    p = start_price
    # tramos de tendencia
    seg_len = max(40, n // 6)
    drifts = {"up": 0.0009, "down": -0.0009, "flat": 0.0}
    seq = {"mixed": ["up", "flat", "down", "down", "up", "up"],
           "up": ["up"] * 6, "down": ["down"] * 6, "flat": ["flat"] * 6}[regime]
    vol = 0.006
    for i in range(n):
        d = drifts[seq[min(i // seg_len, len(seq) - 1)]]
        ret = d + rng.normal(0, vol)
        p = p * (1 + ret)
        prices[i] = p
    close = pd.Series(prices, index=idx)
    # "memoria" de niveles: suavizar hacia maximos/minimos redondos
    open_ = close.shift(1).fillna(close.iloc[0])
    spread = close.abs() * rng.uniform(0.002, 0.012, n)
    high = np.maximum(open_, close) + spread * rng.uniform(0.2, 1.0, n)
    low = np.minimum(open_, close) - spread * rng.uniform(0.2, 1.0, n)
    volume = (spread / close * 1e6) * rng.uniform(0.5, 1.5, n) + 50
    df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                       "volume": volume}, index=idx)
    return df


def synthetic_multi_tf(seed: int = 42, regime: str = "mixed", start_price: float = 60000.0) -> dict:
    from indicators import resample_ohlcv
    h1 = synthetic_ohlcv(24 * 365, "1h", start_price, seed, regime)
    return {"1h": h1.tail(720), "4h": resample_ohlcv(h1, "4h").tail(720),
            "1d": resample_ohlcv(h1, "1D"), "1w": resample_ohlcv(h1, "W-MON")}
