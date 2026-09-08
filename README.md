# Signal Bot v4 — Cripto + Acciones/CEDEARs

Sistema de análisis para **tomar decisiones**: comprar, vender, hacer trading o simplemente
entender cómo está un activo. Reemplaza al motor v3 (score de 17 indicadores) por un motor
basado en **estructura de mercado** y **soportes/resistencias reales**.

- Cripto (BTC, ETH, SOL, XRP, LINK): Kraken, marco principal 4H, long y short.
- Acciones, ETFs y CEDEARs: Yahoo Finance, marco principal 1D, solo long. El análisis se hace
  sobre el subyacente en USD (datos limpios) y al lado se muestra el precio del CEDEAR en pesos y
  el CCL implícito.
- Dashboard con gráfico (velas, EMAs, niveles dibujados con su fuerza), pestaña de acciones con
  watchlist editable, operaciones manuales, acertividad, paper trading y backtest.
- Alertas Telegram: señal nueva, cambio de lectura (ej. MANTENER → PROTEGER GANANCIAS), llegada a
  nivel fuerte, stops/TPs de tus operaciones, resumen diario de acciones.

> Es una herramienta de análisis, no ejecuta órdenes. Toda decisión es tuya.

---

## Cómo decide el motor (engine.py)

1. **Tendencia por timeframe** (`structure.py`): score −100..+100 que combina
   - *Estructura* (±40): máximos y mínimos crecientes = alcista, decrecientes = bajista.
     Detecta **BOS** (rompe el último swing a favor → continuación) y **CHoCH** (rompe el último
     swing en contra → primera señal objetiva de giro, mucho antes que la EMA200).
   - *Medias* (±30): precio vs EMA20/50/200 y pendiente de la EMA50.
   - *Fuerza direccional* (±30): DI+ vs DI− escalado por ADX.
   El **sesgo global** pondera 1W/1D/4H (cripto) o 1W/1D (acciones) y define el régimen
   (BULL FUERTE … BEAR FUERTE, LATERAL, TRANSICIÓN).
2. **Niveles estructurales** (`levels.py`): swings por fractales en 1H/4H/1D/1W, máximo/mínimo del
   día y semana anterior, pivots diarios/semanales, nodos de volumen (perfil de volumen que reparte
   el volumen de cada vela sobre su rango) y extremos de 52 semanas. Se agrupan en clusters (ancho
   ≈ 0.45 ATR) y cada nivel recibe una **fuerza 0-100** según timeframe de origen, cantidad de
   toques, recencia, volumen y si ya actuó como techo y piso (flip).
3. **Ubicación del precio**: EN SOPORTE / EN RESISTENCIA / RANGO ESTRECHO / EN MEDIO DEL RANGO.
4. **Timing 1H**: RSI6 girando, MACD, cierre sobre máximo previo, EMA20, VWAP.
5. **Setups**:
   - `PULLBACK`: tendencia a favor + precio apoyado en soporte (o rechazado en resistencia para
     short) + timing que no va en contra + RSI14 no extremo.
   - `BREAKOUT`: cierre sobre una resistencia (o bajo un soporte) de fuerza ≥ 25 con volumen > 1.3×
     promedio o nivel fuerte.
6. **Plan con niveles reales**: stop detrás del nivel (+0.5 ATR de buffer, tope 3 ATR), TP1/TP2 en las
   siguientes resistencias/soportes, TP3 por extensión. R/R real. Si el TP1 queda a menos de 1.2R
   la señal se **rechaza por falta de espacio**. Tamaño de posición por riesgo (`capital × risk_pct`).
7. **Agotamiento** (0-100): CHoCH en contra, divergencias RSI/OBV con swings reales, ADX cayendo
   desde pico, RSI6 extremo, MACD contrayéndose. ≥ 50 = posible giro → baja la confianza o dispara
   PROTEGER GANANCIAS / ESPERAR EL PISO.
8. **Confianza** ALTA/MEDIA/BAJA: alineación de timeframes, fuerza del nivel, timing, R/R ≥ 2,
   funding, agotamiento en contra, CHoCH en contra, y (cripto) correlación con BTC.
9. **Acción sugerida** en lenguaje claro: COMPRAR, COMPRAR (ruptura), VENDER / SHORT, MANTENER,
   MANTENER NO COMPRAR, VIGILAR ENTRADA, PROTEGER GANANCIAS, REDUCIR, EMPEZAR A ACUMULAR,
   NO COMPRAR TODAVÍA, FUERA DEL MERCADO, ESPERAR RUPTURA, ESPERAR — siempre con el porqué y los
   niveles concretos.
10. **Escenarios condicionales**: "si rompe X → objetivo Y; si pierde Z → siguiente soporte W".

---

## Instalación

```bash
pip install -r requirements.txt        # sin pandas-ta: indicadores en pandas puro
cp config.example.json config.json     # editar
python app.py                          # http://127.0.0.1:5000
```

`config.json`:

| clave | qué es |
|---|---|
| `telegram_token`, `telegram_chat_id` | BotFather. **Revocá el token viejo si lo compartiste.** |
| `dashboard_token` | clave para entrar al dashboard. Vacío = sin auth (solo local). |
| `host` | `127.0.0.1` (recomendado, entrar por túnel SSH/Tailscale) o `0.0.0.0` con token. |
| `capital_disponible`, `risk_pct` | para el tamaño de posición del plan (2 % por defecto). |
| `crypto_assets` | subconjunto de BTC, ETH, SOL, XRP, LINK. |
| `full_analysis_interval_hours` | ciclo cripto (4). Chequeo de precios cada `ops_check_interval_minutes`. |
| `stocks_enabled`, `stocks_hours_utc` | análisis de acciones a esas horas UTC (15 y 21 = media rueda y cierre). |
| `stocks_telegram_digest` | manda el resumen de acciones por Telegram en cada análisis programado. |
| `win_threshold_pct` | mínimo movimiento para contar WIN en acertividad (0.3 %). |

Probar el motor sin levantar el dashboard:

```bash
python -c "from data import *; from engine import *; import json
dfs = fetch_crypto_multi_tf('XBTUSD'); r = analyze_crypto('BTC', dfs, fetch_funding_rate('BTC'), 10000, 0.02)
print(r['summary']); print(r['action'], '-', r['action_detail']); print(json.dumps(r['plan'], indent=1))"
```

Probar la interfaz **sin internet** (datos inventados): `python run_offline.py`.

Backtest de 2 años con historia de Yahoo (tarda varios minutos): `python backtest.py BTC yahoo`
(o `kraken` para los últimos 4 meses, o desde la pestaña Acertividad).

---

## Watchlist de acciones

`data/watchlist.json` (se crea con tu cartera de abril 2026). Editable desde la pestaña
"Acciones y CEDEARs" o a mano:

```json
{"ticker": "LLY", "cedear": "LLY.BA", "ratio": 20, "type": "cedear", "name": "Eli Lilly", "holding": false}
```

- `ticker`: símbolo Yahoo del subyacente (`NVDA`, `SPY`) o de la acción argentina (`YPFD.BA`).
- `ratio`: CEDEARs por acción. **Verificar en BYMA/Comafi** — cambian con splits. Solo afecta el
  CCL implícito (`precio_cedear_ARS × ratio / precio_USD`).

---

## Deploy (DigitalOcean / cualquier VPS)

```bash
apt update && apt install -y python3-pip python3-venv git
git clone TU_REPO /root/signalbot && cd /root/signalbot
python3 -m venv venv && venv/bin/pip install -r requirements.txt
nano config.json    # token de Telegram, dashboard_token, host
```

`/etc/systemd/system/signalbot.service`:

```ini
[Unit]
Description=Signal Bot v4
After=network.target
[Service]
WorkingDirectory=/root/signalbot
ExecStart=/root/signalbot/venv/bin/python app.py
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now signalbot
journalctl -u signalbot -f
```

Acceso recomendado: dejar `host: 127.0.0.1` y abrir un túnel `ssh -L 5000:127.0.0.1:5000 root@IP`
(o Tailscale). Si abrís el puerto al público, usá `host: 0.0.0.0` **con `dashboard_token` fuerte**
y HTTPS detrás de Caddy/nginx.

---

## Estructura

```
indicators.py     EMA/RSI/MACD/ADX/ATR/BB/OBV/StochRSI/VWAP en pandas puro, resample
data.py           Kraken, funding (Binance→Bybit→OKX), Yahoo (acciones, CEDEARs, historia), sintético
levels.py         swings, perfil de volumen, pivots, clustering y fuerza de niveles
structure.py      estructura HH/HL, BOS/CHoCH, trend_score, divergencias con swings, agotamiento
engine.py         análisis completo, setups, plan por niveles, acción sugerida, escenarios, filtro BTC
stocks.py         watchlist, análisis de acciones/CEDEARs, CCL implícito, resumen
tracker.py        registro de señales, outcomes a tiempo exacto, estadísticas, evolución
paper_trading.py  operaciones ficticias con stop/TP por high-low, fees, profit factor, R promedio
operations.py     operaciones manuales (USDT-M / COIN-M), PnL, liquidación, alertas de cierre
telegram_bot.py   mensajes
backtest.py       backtest multi-timeframe sin lookahead
app.py            Flask + scheduler + auth + estado persistente
run_offline.py    dashboard con datos sintéticos (sin internet)
templates/dashboard.html, static/lightweight-charts...  UI
data/             (generado) signals.db, paper_trades.db, operations.db, watchlist.json, state.json, caches
```

## API

`/api/data` cripto · `/api/stocks` acciones · `/api/watchlist` (GET/POST, DELETE `/api/watchlist/<t>`)
· `/api/candles/<asset>?tf=4h&type=crypto|stock` · `/api/stats?type=` · `/api/evolution?type=` ·
`/api/paper?type=` · `/api/signals` · `/api/backtest/<asset>?source=yahoo|kraken` ·
`/api/operations/*` · `/api/refresh`, `/api/stocks/refresh`. Todas aceptan header `X-Token`.

## Qué cambió respecto de v3

- Sin `pandas-ta` (problemas con numpy); indicadores propios y causales.
- Soportes/resistencias estructurales multi-timeframe con fuerza, en vez de pivots de una vela 4H.
- Stop y objetivos en niveles reales con R/R real y rechazo por falta de espacio, en vez de múltiplos
  fijos de ATR.
- Tendencia graduada por estructura (BOS/CHoCH) en vez de alineación binaria de EMAs.
- Divergencias sobre swings reales (no mitades de ventana).
- Score sin indicadores redundantes (5 de los 17 medían lo mismo).
- Outcomes medidos con la vela histórica exacta; paper trading con high/low.
- Estado persistente (no re-manda todas las señales al reiniciar), lock del análisis, auth por
  token, host local por defecto.
- Pestaña de acciones/CEDEARs, gráfico con niveles, escenarios con niveles reales.
- Backtest multi-timeframe con 2 años de historia (Yahoo), no 720 velas.
