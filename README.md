# Crypto Signal Bot v2.2

Sistema de señales automatizado para crypto perpetuos (BTC, ETH, SOL, XRP).
Motor de análisis técnico multi-timeframe con gestión integrada de operaciones.

## Stack
- **Python 3.11+**
- **Kraken API pública** (sin autenticación, sin cuenta necesaria)
- **pandas + pandas-ta** (indicadores técnicos)
- **Flask** (dashboard web)
- **APScheduler** (análisis cada 4h, alertas cada 2min)
- **Telegram Bot API** (notificaciones push)
- **SQLite** (tracking de acertividad + gestión de operaciones)

---

## Características v2.2

### Motor de análisis
- **16 indicadores técnicos**: EMA(20/50/200), Bollinger Bands, MACD, RSI(6/20), StochRSI, ADX, OBV, ATR, VWAP diario, Pivots clásicos
- **Detección de divergencias**: RSI y OBV (acumulación/distribución institucional)
- **Sistema de scoring ponderado**: ±16 puntos, umbral ±9 para emitir señal
- **Análisis multi-timeframe**: 1W → 1D → 4H → 1H con confirmación
- **Filtros anti-trampa**: ADX obligatorio, EMA200 para evitar bear/bull rallies

### Dashboard interactivo
4 pestañas:
1. **Señales en vivo**: cards con score, tendencias, indicadores, niveles S/R dinámicos, condiciones
2. **Mis operaciones**: CRUD completo, tracking PnL en tiempo real, alertas contextuales (TP/SL/liquidación)
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
Mensaje completo con 18 datos clave:
- Confianza (ALTA/MEDIA/BAJA)
- Tendencias 1W/1D/1H + confirmación
- ADX + trending
- RSI(6/20), MACD, OBV trend
- Divergencias RSI/OBV detectadas
- Niveles operativos (SL, TP1/2/3, R/R ratio)

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
Debe mostrar análisis de 4 activos sin errores.

### 5. Correr el dashboard
```bash
python app.py
```
Abrí http://localhost:5000

**Nota:** Primera ejecución crea automáticamente:
- `/data/signals.db` (tracking)
- `/data/operations.db` (gestión de ops)
- `/data/last_results.json` (cache)

---

## Deploy en DigitalOcean (producción)

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
git clone https://github.com/TU_USUARIO/CryptobotNico.git
cd Crypto-signal-bot
```

### 5. Instalar dependencias Python
```bash
pip3 install --break-system-packages -r requirements.txt
```
(flag necesario en Ubuntu 24)

### 6. Configurar `config.json`
```bash
nano config.json
```
Pegá tu config con tokens reales.

### 7. Crear servicio systemd
```bash
nano /etc/systemd/system/cryptobot.service
```

```ini
[Unit]
Description=Crypto Signal Bot v2.2
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

Debe mostrar: `Active: active (running)`

### 9. Ver logs en vivo
```bash
journalctl -u cryptobot -f
```

Esperá a ver:
```
Scheduler activo:
  - Analisis completo cada 4h
  - Check de operaciones cada 2min
Dashboard en http://0.0.0.0:5000
```

### 10. Abrir firewall
```bash
ufw allow 5000/tcp
ufw reload
```

### 11. Acceder al dashboard
Desde cualquier dispositivo en tu red o internet:
```
http://TU_IP_DROPLET:5000
```

**Tip:** Guardá la IP en favoritos del celular para acceso rápido.

---

## Comandos útiles

### Ver logs
```bash
journalctl -u cryptobot -n 50 --no-pager
journalctl -u cryptobot -f  # modo follow
```

### Reiniciar servicio
```bash
systemctl restart cryptobot
```

### Parar servicio
```bash
systemctl stop cryptobot
```

### Actualizar código desde GitHub
```bash
cd /root/Crypto-signal-bot
git pull origin main
systemctl restart cryptobot
```

### Backup de bases de datos
```bash
cp /root/Crypto-signal-bot/data/*.db ~/backup/
```

---

## Estructura del proyecto

```
Crypto-signal-bot/
├── engine.py              # Motor de análisis técnico (indicadores + scoring)
├── tracker.py             # Sistema de acertividad (win rate por timeframe)
├── operations.py          # Gestión de operaciones (CRUD + alertas)
├── telegram_bot.py        # Notificaciones Telegram
├── app.py                 # Dashboard Flask + scheduler
├── config.json            # Configuración (tokens, capital, intervalos)
├── requirements.txt       # Dependencias Python
├── templates/
│   └── dashboard.html     # UI del dashboard (4 pestañas)
└── data/                  # Generado automáticamente
    ├── signals.db         # SQLite: señales + outcomes
    ├── operations.db      # SQLite: operaciones manuales
    └── last_results.json  # Cache del último análisis
```

---

## Sistema de scoring v2.2

### Capas ponderadas (max ±16 puntos)
| Capa | Peso | Indicadores |
|------|------|-------------|
| Tendencia | ±3 | EMA 20/50/200 alineadas |
| Bandas + VWAP | ±2 | Bollinger MB + VWAP diario |
| Momentum | ±3 | MACD + StochRSI |
| Fuerza relativa | ±1 | RSI(20) |
| Divergencias RSI | ±3 | Precio vs RSI (mínimos/máximos) |
| Volumen | ±3 | OBV vs MA20 + divergencia OBV |
| Presión compradora | +1 | Volumen > 1.5× MA10 (solo suma) |

### Filtros obligatorios
- **ADX ≥ 25**: mercado debe tener tendencia definida
- **1W alineado**: no operar contra tendencia semanal
- **1D confirmado**: tendencia diaria debe apoyar
- **1H confirmado**: trigger de entrada en 1 hora

### Señales
- **Score ≥ +9 + filtros OK** → LONG (confianza ALTA/MEDIA/BAJA según confirmación 1H y alineación 1W-1D)
- **Score ≤ -9 + filtros OK** → SHORT (ídem)
- **-8 a +8** → NEUTRAL (no opera)

### Ejemplo real (BTC -8/16)
```
Score: -8 (bajista pero sin señal)
¿Por qué NEUTRAL si está -8?
→ 1W: LATERAL (bloqueando)
→ 1D: LATERAL (bloqueando)
→ ADX: 35.8 (OK)

Si 1D se vuelve BAJISTA → emite SHORT con confianza MEDIA
```

---

## Indicadores técnicos utilizados

| Indicador | Parámetros | Uso principal |
|-----------|-----------|---------------|
| EMA | 20, 50, 200 | Tendencia multi-nivel + filtro bear/bull trap |
| Bollinger Bands | 20, 2σ | Sobreextensión + squeeze (volatilidad) |
| MACD | 12, 26, 9 | Momentum direccional + cruces |
| RSI | 6, 20 | Sobreventa/compra + divergencias |
| StochRSI | 3, 3, 14 | Timing de entrada (% dentro del rango) |
| ADX | 14 | Fuerza de tendencia (filtro obligatorio) |
| OBV | acumulado | Presión institucional + divergencias |
| ATR | 14 | Stop loss y TPs (1.5× ATR) |
| VWAP | diario | Precio institucional promedio |
| Pivots | clásicos | Soportes/resistencias dinámicos |

---

## Acertividad realista

El sistema aplica **threshold de 0.3%** para considerar WIN/LOSS:
- Movimiento < 0.3% → dentro de fees + slippage → **no cuenta**
- Solo movimientos reales > threshold se consideran

**Por qué importa:** Un bot con 70% win rate sin threshold puede tener 45% real. Este bot reporta la métrica que realmente importa para operar.

---

## Troubleshooting

### "Error 451 Unavailable For Legal Reasons"
Binance bloquea algunos servidores. El bot usa Kraken como backup — no requiere acción.

### Dashboard no carga
```bash
# Verificar que el servicio esté corriendo
systemctl status cryptobot

# Verificar firewall
ufw status | grep 5000

# Ver logs de error
journalctl -u cryptobot -n 50
```

### Telegram no envía mensajes
1. Verificá token en `config.json`
2. Verificá que el bot esté iniciado (escribile /start)
3. Verificá chat_id correcto
4. Probá: `curl -X POST https://api.telegram.org/botTU_TOKEN/sendMessage -d chat_id=TU_CHAT_ID -d text="test"`

### Base de datos corrupta
```bash
cd /root/Crypto-signal-bot/data
rm signals.db operations.db
systemctl restart cryptobot
```
Se recrean automáticamente.

---

## Roadmap

- [ ] Soporte para más exchanges (Bybit, OKX)
- [ ] Backtesting histórico con datos reales
- [ ] Machine learning para optimizar pesos
- [ ] Webhook para ejecución automática (con aprobación manual)
- [ ] Modo paper trading integrado

---

## Licencia
MIT

## Disclaimer
Este bot es una **herramienta de análisis**, no ejecuta operaciones automáticamente.
Toda decisión de trading es responsabilidad del usuario.
Crypto trading implica riesgo de pérdida total del capital.
