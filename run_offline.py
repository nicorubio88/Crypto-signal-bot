"""
Corre el dashboard SIN internet con datos SINTETICOS (para probar la interfaz
o el codigo). NO sirve para decidir nada: los precios son inventados.

    python run_offline.py
"""
import data
import stocks
import app
from data import synthetic_multi_tf, CRYPTO_SYMBOLS

_START = {"BTC": 60000, "ETH": 3000, "SOL": 150, "XRP": 0.6, "LINK": 15}
_BY_SYMBOL = {s: a for a, s in CRYPTO_SYMBOLS.items()}


def _crypto(symbol):
    asset = _BY_SYMBOL[symbol]
    return synthetic_multi_tf(seed=abs(hash(symbol)) % 50, start_price=_START[asset], regime="mixed")


def _stock(ticker, intraday=True):
    d = synthetic_multi_tf(seed=abs(hash(ticker)) % 100, start_price=100 + abs(hash(ticker)) % 400,
                           regime=["up", "mixed", "down"][abs(hash(ticker)) % 3])
    return {"1h": d["1h"], "1d": d["1d"], "1w": d["1w"]}


data.fetch_crypto_multi_tf = _crypto
data.fetch_kraken = lambda symbol, interval, limit=720, retries=3: _crypto(symbol)[interval].tail(limit)
data.fetch_funding_rate = lambda asset: {"available": True, "rate": 0.012, "sentiment": "Neutral",
                                          "impact": "NEUTRAL", "source": "fake"}
data.fetch_yahoo = lambda ticker, period="2y", interval="1d": _stock(ticker)[{"1h": "1h", "1d": "1d", "1wk": "1w"}[interval]]
stocks.fetch_stock_multi_tf = _stock
stocks.fetch_last_price_yahoo = lambda t: 15000.0
app.fetch_crypto_multi_tf = data.fetch_crypto_multi_tf
app.fetch_kraken = data.fetch_kraken
app.fetch_funding_rate = data.fetch_funding_rate

if __name__ == "__main__":
    main_src = open(app.__file__).read().split('if __name__ == "__main__":')[1]
    exec("\n".join(line[4:] for line in main_src.splitlines()), vars(app))
