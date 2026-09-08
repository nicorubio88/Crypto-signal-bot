"""
Indicadores tecnicos en pandas/numpy puro (sin pandas-ta).

Todos son causales: el valor en la vela i solo usa velas <= i, asi que
se pueden usar en backtest sin lookahead.
"""

import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0)
    dn = -delta.clip(upper=0)
    # Wilder smoothing
    avg_up = up.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    avg_dn = dn.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = avg_up / avg_dn.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    # Sin caidas en la ventana -> RSI 100 (evita NaN por division por cero)
    out = out.where(avg_dn != 0, 100.0)
    out[avg_up.isna() | avg_dn.isna()] = np.nan
    return out


def macd(close: pd.Series, fast=12, slow=26, signal=9):
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return line, sig, line - sig


def true_range(h, l, c) -> pd.Series:
    prev_c = c.shift(1)
    return pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)


def atr(h, l, c, n: int = 14) -> pd.Series:
    return true_range(h, l, c).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def adx(h, l, c, n: int = 14):
    """Devuelve (ADX, DI+, DI-) con suavizado de Wilder."""
    up = h.diff()
    dn = -l.diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=h.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=h.index)
    tr = true_range(h, l, c)
    atr_w = tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    di_plus = 100 * plus_dm.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / atr_w
    di_minus = 100 * minus_dm.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / atr_w
    dx = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, np.nan)
    adx_s = dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return adx_s, di_plus, di_minus


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0):
    mid = sma(close, n)
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    return mid + k * sd, mid, mid - k * sd


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    direction = np.sign(close.diff()).fillna(0)
    return (direction * volume).cumsum()


def stoch_rsi(close: pd.Series, n: int = 14, k: int = 3, d: int = 3):
    r = rsi(close, n)
    lo = r.rolling(n, min_periods=n).min()
    hi = r.rolling(n, min_periods=n).max()
    st = 100 * (r - lo) / (hi - lo).replace(0, np.nan)
    kk = st.rolling(k, min_periods=k).mean()
    dd = kk.rolling(d, min_periods=d).mean()
    return kk, dd


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Agrega el set completo de indicadores al DataFrame OHLCV (in place y devuelve)."""
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
    df["EMA20"] = ema(c, 20)
    df["EMA50"] = ema(c, 50)
    df["EMA200"] = ema(c, 200)
    df["SMA20"] = sma(c, 20)
    df["RSI6"] = rsi(c, 6)
    df["RSI14"] = rsi(c, 14)
    df["MACD"], df["MACD_SIGNAL"], df["MACD_HIST"] = macd(c)
    df["ADX"], df["DI_POS"], df["DI_NEG"] = adx(h, l, c, 14)
    df["ATR"] = atr(h, l, c, 14)
    df["BB_UP"], df["BB_MB"], df["BB_DN"] = bollinger(c, 20, 2)
    df["BB_WIDTH"] = (df["BB_UP"] - df["BB_DN"]) / df["BB_MB"]
    df["OBV"] = obv(c, v)
    df["OBV_MA20"] = sma(df["OBV"], 20)
    df["VOL_MA20"] = sma(v, 20)
    df["STOCH_K"], df["STOCH_D"] = stoch_rsi(c)
    return df


def add_daily_vwap(df: pd.DataFrame) -> pd.DataFrame:
    """VWAP que se resetea cada dia (solo tiene sentido en marcos intradiarios)."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    pv = tp * df["volume"]
    day = df.index.normalize()
    cum_v = df["volume"].groupby(day).cumsum().replace(0, np.nan)
    df["VWAP"] = pv.groupby(day).cumsum() / cum_v
    return df


def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resamplea OHLCV a un marco mayor ('4h', '1D', 'W-MON'...)."""
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    out = df.resample(rule, label="left", closed="left").agg(agg).dropna(subset=["open"])
    return out
