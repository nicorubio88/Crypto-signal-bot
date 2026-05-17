# Crypto Signal Bot 🤖

Bot de señales para BTC, ETH, SOL y XRP.
Consume API pública de Binance — sin cuenta, sin riesgo.

## Stack
- **Python 3.11+**
- **Binance Futures API** (pública, sin autenticación)
- **pandas-ta** (indicadores técnicos)
- **Flask** (dashboard web)
- **APScheduler** (actualización cada 4H)
- **Telegram Bot API** (alertas)

---

## Setup local (desarrollo)

### 1. Clonar y entrar
```bash
git clone https://github.com/TU_USUARIO/crypto-bot.git
cd crypto-bot
```

### 2. Instalar dependencias
```bash
pip install -r requirements.txt
```

### 3. Configurar Telegram
Editá `config.json`:
```json
{
  "telegram_token": "TU_TOKEN",
  "telegram_chat_id": "TU_CHAT_ID"
}
```

Para obtener el token:
1. Abrí Telegram → buscá @BotFather
2. Escribí /newbot → seguí los pasos
3. BotFather te da el TOKEN

Para obtener el chat_id:
1. Escribile algo a tu bot
2. Abrí en browser: `https://api.telegram.org/botTOKEN/getUpdates`
3. Copiá el valor de `"id"` dentro de `"chat"`

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
- Ubuntu 22.04 LTS
- 1GB RAM / 1 vCPU (suficiente)
- Región más cercana

### 2. Conectarse por SSH
```bash
ssh root@TU_IP_DROPLET
```

### 3. Instalar Python y dependencias
```bash
apt update && apt install -y python3-pip python3-venv git
git clone https://github.com/TU_USUARIO/crypto-bot.git
cd crypto-bot
pip3 install -r requirements.txt
```

### 4. Configurar como servicio (corre 24/7)
```bash
nano /etc/systemd/system/cryptobot.service
```

Pegá esto:
```ini
[Unit]
Description=Crypto Signal Bot
After=network.target

[Service]
User=root
WorkingDirectory=/root/crypto-bot
ExecStart=/usr/bin/python3 app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable cryptobot
systemctl start cryptobot
systemctl status cryptobot
```

### 5. Abrir el puerto 5000
```bash
ufw allow 5000
```

### 6. Acceder al dashboard
Desde cualquier browser o celular:
```
http://TU_IP_DROPLET:5000
```

---

## Estructura del proyecto
```
crypto-bot/
├── engine.py          # Motor de datos e indicadores
├── telegram_bot.py    # Alertas por Telegram
├── app.py             # Dashboard Flask
├── config.json        # Token Telegram y configuración
├── requirements.txt
├── templates/
│   └── dashboard.html # UI del dashboard
└── data/
    └── last_results.json  # Cache (generado automáticamente)
```

---

## Indicadores utilizados

| Indicador | Parámetros | Uso |
|-----------|-----------|-----|
| EMA | 20, 50, 200 | Tendencia |
| Bollinger Bands | 20, 2 | Tendencia + timing |
| MACD | 12, 26, 9 | Momentum |
| RSI | 6, 20, 50 | Momentum + sobreventa/compra |
| Volumen vs MA10 | — | Confirmación |

## Sistema de scoring
- **≥ +4**: Señal LONG 🟢
- **≤ -4**: Señal SHORT 🔴
- **Entre -3 y +3**: NEUTRAL ⚪
- Filtro: tendencia 1D — no opera contra la marea grande
