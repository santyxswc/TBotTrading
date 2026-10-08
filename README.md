# TBotTrading

Bot de Telegram privado para seguir una cartera **núcleo-satélite** de ETF y
motor de backtest para comparar estrategias de rebalanceo.

| Ticker    | Papel                        | Peso objetivo |
|-----------|------------------------------|---------------|
| `IWDA.AS` | Núcleo: mundo desarrollado   | 70 %          |
| `EQQQ.DE` | Satélite: Nasdaq-100         | 15 %          |
| `SMH`     | Satélite: semiconductores (USD → EUR) | 15 % |

Los satélites suman un 30 % objetivo. La estrategia de **banda** solo
rebalancea cuando su peso conjunto sale del rango 20 %–40 %.

## Archivos

| Archivo                  | Descripción |
|--------------------------|-------------|
| `Bot.py`                 | Motor: descarga de precios (Yahoo Finance), métricas (CAGR, volatilidad, Max DD, Sharpe, Calmar) y backtest de las estrategias *hold*, *anual* y *banda*. Ejecutable por sí solo. |
| `telegram_bot.py`        | Bot de Telegram: comandos, menú con botones, gráficos, resumen diario y alertas de rebalanceo. |
| `requirements.txt`       | Dependencias con versión fijada. |
| `.env.example`           | Plantilla de configuración (token y chats autorizados). |
| `portfolio.example.json` | Plantilla de posiciones (participaciones por ticker). |
| `Doxyfile`               | Configuración para generar esta documentación con Doxygen. |

Archivos generados en tiempo de ejecución (ignorados por git):
`.env`, `portfolio.json`, `subscribers.json`, `alert_state.json`.

## Instalación

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                       # rellena TELEGRAM_BOT_TOKEN
cp portfolio.example.json portfolio.json   # pon tus participaciones
```

## Configuración

Variables de `.env`:

- `TELEGRAM_BOT_TOKEN`: token del bot creado con `@BotFather`.
- `ALLOWED_CHAT_IDS`: chat IDs autorizados, separados por comas. Si está
  vacío nadie puede usar el bot; escríbele `/id` para conocer el tuyo.

`portfolio.json` contiene el número de participaciones de cada ETF:

```json
{ "IWDA.AS": 120, "EQQQ.DE": 15, "SMH": 8 }
```

## Uso

Backtest por consola:

```bash
python Bot.py
```

Bot de Telegram:

```bash
python telegram_bot.py
```

### Comandos del bot

| Comando                     | Qué hace |
|-----------------------------|----------|
| `/start`, `/menu`, `/ayuda` | Abre el menú con botones. |
| `/precio`                   | Precios en EUR y variación a 1 día, 1 mes y 1 año. |
| `/grafico`                  | Evolución del último año y caídas desde máximos. |
| `/cartera`                  | Reparto actual vs. objetivo, estado de la banda y cuánto comprar/vender. |
| `/backtest`                 | Comparativa a 10 años de las tres estrategias. |
| `/suscribir` / `/cancelar`  | Activa o desactiva el resumen diario. |
| `/id`                       | Muestra tu chat ID (funciona aunque no estés autorizado). |

### Tareas programadas (lunes a viernes, hora de Madrid)

- **22:00**: resumen diario a los chats suscritos.
- **22:05**: comprobación de la banda; avisa a los chats autorizados solo
  cuando el estado cambia (dentro, cerca del límite o fuera de la banda).

## Documentación

El código está documentado con comentarios Doxygen (docstrings `"""!`).
Para generar la documentación HTML:

```bash
doxygen            # genera docs/html/index.html
```

> Los datos y simulaciones son informativos y no constituyen una
> recomendación de inversión.
