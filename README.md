# Crypto Signal Bot v3.0

Sistema de señales automatizado para crypto perpetuos (BTC, ETH, SOL, XRP).
Motor de análisis técnico multi-timeframe con detección de régimen del mercado,
funding rate de perpetuos, filtro de correlación BTC y gestión integrada de operaciones.

## Stack
- **Python 3.11+**
- **Kraken API pública** (sin autenticación, datos OHLC)
- **Binance Futures API pública** (funding rate, fallback graceful)
- **pandas + pandas-ta** (indicadores técnicos)
- **Flask** (dashboard web)
- **APScheduler** (análisis cada 4h, alertas cada 2min)
- **Telegram Bot API** (notificaciones push)
- **SQLite** (tracking de acertividad + gestión de operaciones)

---

## Características v3.0

### Nuevo en v3.0
- **Régimen del mercado**: clasificación independiente de la señal (BULL FUERTE / BULL / BULL DÉBIL / LATERAL / BEAR DÉBIL / BEAR / BEAR FUERTE / TRANSICIÓN)
- **Funding rate**: sentimiento real de perpetuos desde Binance Futures
- **Umbral adaptativo**: el umbral de señal cambia según fuerza de tendencia (ADX)
- **Filtro de correlación BTC**: bloquea señales de altcoins contrarias a BTC

### Motor de análisis
- **17 indicadores técnicos**: EMA(20/50/200), Bollinger Bands, MACD, RSI(6/20), StochRSI, ADX, OBV, ATR, VWAP diario, Pivots dinámicos, Funding Rate
- **Detección de divergencias**: RSI y OBV (acumulación/distribución institucional)
- **Sistema de scoring ponderado**: ±17 puntos, umbral adaptativo según ADX
- **Análisis multi-timeframe**: 1W → 1D → 4H → 1H con confirmación
- **Filtros anti-trampa**: ADX obligatorio, EMA200 para evitar bear/bull rallies

### Dashboard interactivo
4 pestañas:
1. **Señales en vivo**: cards con régimen, funding, score, tendencias, indicadores, niveles S/R dinámicos
2. **Mis operaciones**: CRUD completo, tracking PnL en tiempo real, alertas contextuales
3. **Acertividad**: win rate por timeframe (4H/24H/72H) con threshold realista (0.3%)
4. **Historial**: todas las señales emitidas con outcomes

### Gestión de operaciones
- Soporte **USDT-M** (tamaño en USD) y **COIN-M** (tamaño en crypto)
- Calculadora automática de tamaño (capital × 2.5% riesgo)
- Niveles sugeridos por ATR (SL + 3 TPs con split 40/40/20)
- Alertas Telegram en tiempo real (< 2 min):
  - Stop cercano (< 1.5% distancia)
  - TP1/TP2/TP3 alcanzado
  - Bot cambió contra tu posición
  - Señal de cierre urgente

### Notificaciones Telegram
Mensaje completo con régimen del mercado, tendencias multi-timeframe, ADX, RSI, MACD, OBV, divergencias, funding rate y niveles operativos completos.

---

## Sistema de scoring v3.0

### Capas ponderadas (max ±17 puntos)
| Capa | Peso | Indicadores |
|------|------|-------------|
| Tendencia | ±3 | EMA 20/50/200 alineadas |
| Bandas + VWAP | ±2 | Bollinger MB + VWAP diario |
| Momentum | ±3 | MACD + StochRSI |
| Fuerza relativa | ±1 | RSI(20) |
| Divergencias RSI | ±3 | Precio vs RSI (mínimos/máximos) |
| Volumen | ±3 | OBV vs MA20 + divergencia OBV (peso reducido si ADX > 40) |
| Presión compradora | +1 | Volumen > 1.5× MA10 (solo suma) |
| Funding rate | ±1 | Sentimiento perpetuos (opcional, si Binance disponible) |

### Umbral adaptativo según ADX

```
ADX > 40 (tendencia muy fuerte)  → umbral 7  (no perder el move)
ADX 30-40 (tendencia clara)      → umbral 9  (base)
ADX 20-30 (tendencia débil)      → umbral 10 (más estricto)
ADX < 20 (lateral)               → umbral 12 (muy estricto)
```

### Filtros obligatorios
- **ADX ≥ 25**: mercado debe tener tendencia definida
- **1W alineado**: no operar contra tendencia semanal
- **1D confirmado**: tendencia diaria debe apoyar
- **1H confirmado**: trigger de entrada en 1 hora

### Filtro de correlación BTC
- BTC en SHORT (ALTA/MEDIA) → bloquea LONGs en altcoins
- BTC en LONG (ALTA/MEDIA) → bloquea SHORTs en altcoins
- BTC en BEAR FUERTE → degrada confianza de LONGs en alts
- BTC en BULL FUERTE → degrada confianza de SHORTs en alts

### Régimen del mercado (diagnóstico separado de la señal)

| Régimen | Condición | Bias sugerido |
|---------|-----------|---------------|
| BULL FUERTE | 3 TFs alcistas + ADX > 35 | LONG en pullbacks |
| BULL | 3 TFs alcistas | LONG en pullbacks |
| BULL DÉBIL | 2 TFs alcistas | LONG con cautela |
| LATERAL ESTRICTO | ADX < 20 | No operar tendencia |
| LATERAL | 2 TFs laterales | No operar tendencia |
| TRANSICIÓN | TFs desalineados | Esperar confirmación |
| BEAR DÉBIL | 2 TFs bajistas | SHORT con cautela |
| BEAR | 3 TFs bajistas | SHORT en rebotes |
| BEAR FUERTE | 3 TFs bajistas + ADX > 35 | SHORT en rebotes |

---

## Setup local (desarrollo)

### 1. Clonar
```bash
git clone https://github.com/TU_USUARIO/CryptobotNico.git
cd CryptobotNico
```

### 2. Instalar dependencias
```bash
pip install -r requirements.txt
```

### 3. Configurar Telegram

Creá `config.json` en la raíz:
```json
{
  "telegram_token": "TU_TOKEN",
  "telegram_chat_id": "TU_CHAT_ID",
  "capital_disponible": 10000,
  "risk_pct": 0.025,
  "full_analysis_interval_hours": 4,
  "ops_check_interval_minutes": 2,
  "win_threshold_pct": 0.3
}
```

**Obtener token:**
1. Telegram → @BotFather
2. `/newbot` → seguí pasos
3. Copiá el TOKEN

**Obtener chat_id:**
1. Escribile algo a tu bot
2. Abrí: `https://api.telegram.org/botTU_TOKEN/getUpdates`
3. Buscá `"chat":{"id":123456789}` → copiá el número

### 4. Probar el motor
```bash
python engine.py
```

### 5. Correr el dashboard
```bash
python app.py
```
Abrí http://localhost:5000

---

## Deploy en DigitalOcean

### 1. Crear Droplet
- **OS**: Ubuntu 24.04 LTS
- **Plan**: Basic (1GB RAM / 1 vCPU) — $6/mes
- **Región**: la más cercana a vos

### 2. SSH inicial
```bash
ssh root@TU_IP_DROPLET
```

### 3. Instalar dependencias del sistema
```bash
apt update && apt upgrade -y
apt install -y python3-pip python3-venv git
```

### 4. Clonar repo
```bash
cd /root
git clone https://github.com/TU_USUARIO/CryptobotNico.git Crypto-signal-bot
cd Crypto-signal-bot
```

### 5. Instalar dependencias Python
```bash
pip3 install --break-system-packages -r requirements.txt
```

### 6. Configurar `config.json`
```bash
nano config.json
```

### 7. Crear servicio systemd
```bash
nano /etc/systemd/system/cryptobot.service
```

```ini
[Unit]
Description=Crypto Signal Bot v3.0
After=network.target

[Service]
User=root
WorkingDirectory=/root/Crypto-signal-bot
ExecStart=/usr/bin/python3 app.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### 8. Activar y arrancar
```bash
systemctl daemon-reload
systemctl enable cryptobot
systemctl start cryptobot
systemctl status cryptobot
```

### 9. Abrir firewall
```bash
ufw allow 5000/tcp
ufw reload
```

### 10. Acceder al dashboard
```
http://TU_IP_DROPLET:5000
```

---

## Comandos útiles

```bash
# Ver logs
journalctl -u cryptobot -n 50 --no-pager
journalctl -u cryptobot -f  # modo follow

# Reiniciar
systemctl restart cryptobot

# Actualizar desde GitHub
cd /root/Crypto-signal-bot
git pull origin main
systemctl restart cryptobot

# Backup
cp /root/Crypto-signal-bot/data/*.db ~/backup/
```

---

## Estructura del proyecto

```
Crypto-signal-bot/
├── engine.py              # Motor de análisis técnico v3.0
├── tracker.py             # Sistema de acertividad
├── operations.py          # Gestión de operaciones (USDT-M + COIN-M)
├── telegram_bot.py        # Notificaciones Telegram
├── app.py                 # Dashboard Flask + scheduler dual
├── config.json            # Configuración
├── requirements.txt
├── templates/
│   └── dashboard.html     # UI con 4 pestañas
└── data/                  # Generado automáticamente
    ├── signals.db
    ├── operations.db
    └── last_results.json
```

---

## Acertividad realista

El sistema aplica **threshold de 0.3%** para considerar WIN/LOSS:
- Movimiento < 0.3% → dentro de fees + slippage → no cuenta
- Solo movimientos > threshold se consideran

Un bot con 70% win rate sin threshold puede tener 45% real. Este bot reporta la métrica que importa.

---

## Troubleshooting

### Funding rate "no disponible"
Binance bloquea desde algunos hosts (403/451). El sistema sigue funcionando sin este indicador — perdés el ajuste de ±1 punto pero el resto opera normal.

### Dashboard no carga
```bash
systemctl status cryptobot
ufw status | grep 5000
journalctl -u cryptobot -n 50
```

### Telegram no envía
1. Verificá token en `config.json`
2. Escribile `/start` al bot
3. Verificá chat_id

### Base de datos corrupta
```bash
cd /root/Crypto-signal-bot/data
rm signals.db operations.db
systemctl restart cryptobot
```

---

## Roadmap

- [ ] Soporte para Bybit, OKX (fallback funding rate)
- [ ] Backtesting histórico
- [ ] Machine learning para optimizar pesos
- [ ] Modo paper trading integrado

---

## Licencia
MIT

## Disclaimer
Este bot es una **herramienta de análisis**, no ejecuta operaciones automáticamente.
Toda decisión de trading es responsabilidad del usuario.
Crypto trading implica riesgo de pérdida total del capital.
