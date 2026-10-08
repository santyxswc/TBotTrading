"""!
@file telegram_bot.py
@brief Bot de Telegram privado para seguir la cartera núcleo-satélite.

Expone en Telegram los cálculos del motor (@ref Bot.py):

- `/precio`: tabla de precios en EUR con variación a 1 día, 1 mes y 1 año.
- `/grafico`: evolución del último año y caídas desde máximos.
- `/cartera`: reparto actual frente al objetivo y órdenes para rebalancear.
- `/backtest`: comparativa a 10 años de las estrategias hold, anual y banda.
- `/suscribir` y `/cancelar`: resumen diario los días laborables a las 22:00.
- `/id`: muestra el chat ID para poder autorizarlo.

Además, cada día laborable comprueba la banda de los satélites y avisa a los
chats autorizados cuando cambia su estado (ver @ref telegram_bot.check_alerts).

**Configuración** (archivo `.env` junto al script):
- `TELEGRAM_BOT_TOKEN`: token del bot dado por @@BotFather.
- `ALLOWED_CHAT_IDS`: lista de chat IDs autorizados separados por comas.

**Archivos de estado** (en la carpeta del script, ignorados por git):
- `portfolio.json`: participaciones de cada ETF (plantilla en `portfolio.example.json`).
- `subscribers.json`: chats suscritos al resumen diario.
- `alert_state.json`: último estado de banda notificado.

@author santyxswc
"""

import asyncio
import io
import json
import logging
import os
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TimedOut
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    TypeHandler,
)

import Bot as engine

## Carpeta del script; base para `.env` y los archivos de estado.
BASE = Path(__file__).parent
## Participaciones por ticker (`{"IWDA.AS": 12.5, ...}`).
PORTFOLIO_FILE = BASE / "portfolio.json"
## Lista de chat IDs suscritos al resumen diario.
SUBS_FILE = BASE / "subscribers.json"
## Último estado de banda notificado (`{"state": "ok"}`), para no repetir alertas.
ALERT_STATE_FILE = BASE / "alert_state.json"
## Zona horaria en la que se programan los trabajos diarios.
TZ = ZoneInfo("Europe/Madrid")
## Hora del resumen diario a los suscriptores.
SUMMARY_TIME = time(22, 0, tzinfo=TZ)
## Hora de la comprobación de alertas de rebalanceo.
ALERT_TIME = time(22, 5, tzinfo=TZ)  # tras el cierre de EE. UU. (SMH)
## Margen (en tanto por uno) antes de la banda a partir del cual se lanza el aviso previo.
NEAR = 0.02  # aviso previo a 2 puntos de la banda
## Nombre legible de cada ticker para gráficos y textos.
NAMES = {"IWDA.AS": "Mundo (IWDA)", "EQQQ.DE": "Nasdaq-100 (EQQQ)", "SMH": "Semis (SMH)"}
## Color fijo de cada ticker en todos los gráficos.
COLORS = {"IWDA.AS": "#2a6fdb", "EQQQ.DE": "#1baa7f", "SMH": "#e08a1e"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
## Logger del bot.
log = logging.getLogger("tbot")
# httpx imprime la URL completa de cada petición, que incluye el token del bot: no lo registramos
logging.getLogger("httpx").setLevel(logging.WARNING)

## Comandos `(nombre, descripción)` que se registran en el menú de Telegram al arrancar.
COMMANDS = [
    ("menu", "Abrir el menú"),
    ("precio", "Precios actuales"),
    ("grafico", "Gráfico del último año"),
    ("cartera", "Estado de tu cartera"),
    ("backtest", "Backtest a 10 años"),
    ("suscribir", "Activar resumen diario"),
    ("cancelar", "Desactivar resumen diario"),
]


def menu_markup(chat_id: int) -> InlineKeyboardMarkup:
    """!
    @brief Teclado en línea del menú principal.

    El último botón alterna entre activar y desactivar el resumen diario
    según si el chat ya está suscrito.

    @param chat_id Chat para el que se construye el menú.
    @return Teclado con los botones de precios, gráfico, cartera, backtest y suscripción.
    """
    subscribed = chat_id in load_json(SUBS_FILE, [])
    toggle = (
        InlineKeyboardButton("🔕 Desactivar resumen diario", callback_data="cancelar")
        if subscribed
        else InlineKeyboardButton("🔔 Activar resumen diario", callback_data="suscribir")
    )
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💶 Precios", callback_data="precio"),
             InlineKeyboardButton("📈 Gráfico 1 año", callback_data="grafico")],
            [InlineKeyboardButton("🥧 Cartera", callback_data="cartera"),
             InlineKeyboardButton("🧪 Backtest", callback_data="backtest")],
            [toggle],
        ]
    )


async def show_menu(chat, text: str = "¿Qué quieres ver?"):
    """!
    @brief Envía el menú principal a un chat.
    @param chat Chat de Telegram (`telegram.Chat`) al que se envía.
    @param text Texto que acompaña al teclado.
    """
    await chat.send_message(text, reply_markup=menu_markup(chat.id))


# ───────────── datos ─────────────
def load_json(path: Path, default):
    """!
    @brief Lee un archivo JSON tolerando que no exista o esté corrupto.
    @param path Ruta del archivo.
    @param default Valor devuelto si el archivo no existe o no es JSON válido.
    @return Contenido decodificado o @p default.
    """
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def fig_to_png(fig) -> io.BytesIO:
    """!
    @brief Renderiza una figura de matplotlib a PNG en memoria y la cierra.
    @param fig Figura de matplotlib.
    @return Buffer con la imagen, posicionado al inicio y listo para enviar.
    """
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


## Ticker sin sufijo de bolsa (`"IWDA.AS"` → `"IWDA"`), para tablas estrechas.
SHORT = {t: t.split(".")[0] for t in engine.TARGET}
## Descripción en lenguaje claro de cada ETF.
DESC = {
    "IWDA.AS": "miles de empresas grandes de países desarrollados (tu base, 70 %)",
    "EQQQ.DE": "las 100 mayores tecnológicas/no financieras del Nasdaq (15 %)",
    "SMH": "fabricantes de chips y semiconductores, más arriesgado (15 %)",
}


def fmt_pct(x: float, dec: int = 1) -> str:
    """!
    @brief Formatea un tanto por uno como porcentaje con signo (`0.052` → `"+5.2%"`).
    @param x Valor en tanto por uno.
    @param dec Número de decimales.
    @return Cadena formateada.
    """
    return f"{x:+.{dec}%}"


def price_table(px: pd.DataFrame) -> str:
    """!
    @brief Tabla HTML con el último precio y la variación a 1 día, 1 mes y 1 año.

    La variación diaria se calcula contra el último precio distinto, porque
    @ref Bot.download rellena con el valor anterior los días sin mercado.

    @param px Precios del último año (salida de @ref prices_1y).
    @return Texto en HTML de Telegram (`<pre>` monoespaciado).
    """
    last = px.iloc[-1]
    # download() rellena con ffill los días sin cotización: comparamos con el último precio distinto
    prev = pd.Series({t: px[t][px[t] != last[t]].iloc[-1] for t in px})
    m1 = px.asof(px.index[-1] - pd.Timedelta(days=30))
    y1 = px.iloc[0]
    lines = ["<b>Precios (EUR)</b>", f"<i>Datos al {px.index[-1].date()}</i>", "<pre>",
             f"{'':<8}{'Precio':>7}{'1 día':>8}{'1 mes':>8}{'1 año':>8}"]
    for t in px:
        d1 = last[t] / prev[t] - 1
        arrow = "🟢" if d1 >= 0 else "🔴"
        lines.append(f"{arrow} {SHORT[t]:<5}{last[t]:>7.2f}{fmt_pct(d1, 2):>8}"
                     f"{fmt_pct(last[t] / m1[t] - 1):>8}{fmt_pct(last[t] / y1[t] - 1):>8}")
    lines.append("</pre>")
    return "\n".join(lines)


def price_explain() -> str:
    """!
    @brief Explicación en lenguaje claro de los ETF y de cómo leer @ref price_table.
    @return Texto en HTML de Telegram.
    """
    lines = ["<b>¿Qué significa cada cosa?</b>"]
    lines += [f"• <b>{SHORT[t]}</b>: {DESC[t]}." for t in engine.TARGET]
    lines += [
        "",
        "<b>Cómo leer la tabla</b>",
        "• <b>Precio</b>: lo que cuesta una participación (como una “acción” del fondo), en euros, "
        "al último cierre.",
        "• <b>1 día / 1 mes / 1 año</b>: cuánto ha subido o bajado desde entonces. "
        "+5 % quiere decir que 100 € se habrían convertido en 105 €.",
        "• 🟢 sube hoy · 🔴 baja hoy.",
        "",
        "ℹ️ SMH cotiza en dólares y se convierte a euros, así que también se mueve por el tipo de cambio. "
        "Si un día no hubo mercado, se repite el último precio. Son datos informativos, no una recomendación.",
    ]
    return "\n".join(lines)


def chart_prices(px: pd.DataFrame) -> io.BytesIO:
    """!
    @brief Gráfico del último año: evolución en base 100 y caída desde máximos.

    - Panel superior: precio normalizado a 100 al inicio, con el máximo
      marcado y la variación final anotada.
    - Panel inferior: drawdown (%) de cada ETF respecto a su máximo.

    @param px Precios del último año.
    @return Imagen PNG en memoria.
    """
    norm = px / px.iloc[0] * 100
    under = (px / px.cummax() - 1) * 100
    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(8, 6.4), sharex=True,
                                  gridspec_kw={"height_ratios": [3, 1.4], "hspace": 0.08})
    for t in norm:
        ax.plot(norm.index, norm[t], label=NAMES[t], color=COLORS[t], lw=1.8)
        ax.scatter(norm[t].idxmax(), norm[t].max(), color=COLORS[t], s=28, zorder=3)
        ax.annotate(fmt_pct(norm[t].iloc[-1] / 100 - 1), (norm.index[-1], norm[t].iloc[-1]),
                    xytext=(6, 0), textcoords="offset points", color=COLORS[t],
                    va="center", fontweight="bold")
        ax2.fill_between(under.index, under[t], 0, color=COLORS[t], alpha=0.12)
        ax2.plot(under.index, under[t], color=COLORS[t], lw=1.1)
    ax.axhline(100, color="gray", lw=0.8, ls="--")
    ax.set_xlim(right=norm.index[-1] + pd.Timedelta(days=40))
    ax.set_title("Si hubieras invertido 100 € hace un año (EUR)")
    ax.set_ylabel("Valor de tus 100 €")
    ax.legend(frameon=False, loc="upper left")
    ax.grid(alpha=0.25)
    ax2.set_ylabel("Caída desde\nsu máximo (%)")
    ax2.grid(alpha=0.25)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b %y"))
    return fig_to_png(fig)


def chart_prices_explain(px: pd.DataFrame) -> str:
    """!
    @brief Resumen del último año (mejor y peor ETF) y guía para leer @ref chart_prices.
    @param px Precios del último año.
    @return Texto en HTML de Telegram.
    """
    norm = px / px.iloc[0] * 100
    under = px.iloc[-1] / px.max() - 1
    lines = ["<b>Resumen del último año</b>"]
    for t in px:
        lines.append(f"• {NAMES[t]}: 100 € → <b>{norm[t].iloc[-1]:.0f} €</b> "
                     f"({fmt_pct(norm[t].iloc[-1] / 100 - 1)}) · hoy {under[t]:.1%} respecto a su máximo")
    best, worst = norm.iloc[-1].idxmax(), norm.iloc[-1].idxmin()
    lines += [
        f"\n🏆 Mejor: {SHORT[best]} · 🐢 Peor: {SHORT[worst]}",
        "",
        "<b>Cómo leer el gráfico</b>",
        "• <b>Arriba:</b> todos empiezan en 100 para poder compararlos. Si una línea está en 120, "
        "ha subido un 20 % desde el inicio. El punto marca su mejor momento.",
        "• <b>Abajo:</b> cuánto está cada uno por debajo de su máximo. 0 % = en máximos; "
        "−10 % = ha caído un 10 % desde su punto más alto. Las zonas profundas son las “malas rachas”.",
        "• Más tecnología y chips (EQQQ, SMH) suele significar más subida… y más caídas.",
    ]
    return "\n".join(lines)


def portfolio_state(px: pd.DataFrame):
    """!
    @brief Valor actual de cada posición según `portfolio.json`.

    Los tickers que no aparecen en el archivo cuentan como 0 participaciones.

    @param px Precios en EUR; se usa la última fila.
    @return Tupla `(values, total)`: serie con el valor en EUR por ticker y su suma.
    """
    holdings = load_json(PORTFOLIO_FILE, {})
    values = pd.Series({t: holdings.get(t, 0) * px[t].iloc[-1] for t in engine.TARGET})
    return values, values.sum()


def chart_portfolio(values: pd.Series) -> io.BytesIO:
    """!
    @brief Gráfico de la cartera: dona con el reparto y barras actual vs. objetivo.
    @param values Valor en EUR por ticker (de @ref portfolio_state).
    @return Imagen PNG en memoria.
    """
    total = values.sum()
    w = values / total
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(9.5, 4.4))
    _, _, pcts = a1.pie(w, colors=[COLORS[t] for t in w.index], autopct="%1.1f%%",
                        startangle=90, pctdistance=0.78, wedgeprops={"width": 0.45})
    for t in pcts:
        t.set_color("white")
    a1.text(0, 0, f"{total:,.0f} €", ha="center", va="center", fontsize=15, fontweight="bold")
    a1.legend([f"{NAMES[t]}: {values[t]:,.0f} €" for t in w.index], loc="upper center",
              bbox_to_anchor=(0.5, 0.02), frameon=False)
    a1.set_title("Dónde está tu dinero")
    x = range(len(w))
    for i, t in enumerate(w.index):
        cur, tgt = w[t] * 100, engine.TARGET[t] * 100
        a2.bar(i - 0.2, cur, 0.4, color=COLORS[t])
        a2.bar(i + 0.2, tgt, 0.4, color="#c9ced6")
        a2.text(i - 0.2, cur + 1, f"{cur:.1f}", ha="center", fontsize=9)
        a2.text(i + 0.2, tgt + 1, f"{tgt:.0f}", ha="center", fontsize=9, color="#6b7280")
    a2.set_xticks(list(x), [SHORT[t] for t in w.index])
    a2.set_ylabel("% de la cartera")
    a2.set_title("Lo que tienes (color) vs objetivo (gris)")
    a2.spines[["top", "right"]].set_visible(False)
    return fig_to_png(fig)


def band_status(values: pd.Series) -> str:
    """!
    @brief Línea de estado del peso de los satélites frente a la banda.
    @param values Valor en EUR por ticker.
    @return Texto HTML: aviso de rebalanceo si está fuera de
            [@ref Bot.LOWER, @ref Bot.UPPER] o confirmación si está dentro.
    """
    w_sat = values[engine.SATELLITES].sum() / values.sum()
    if w_sat >= engine.UPPER:
        flag = f"⚠️ Satélites en {w_sat:.1%} (≥ {engine.UPPER:.0%}): <b>toca rebalancear</b>"
    elif w_sat <= engine.LOWER:
        flag = f"⚠️ Satélites en {w_sat:.1%} (≤ {engine.LOWER:.0%}): <b>toca rebalancear</b>"
    else:
        flag = f"✅ Satélites en {w_sat:.1%}, dentro de la banda {engine.LOWER:.0%}–{engine.UPPER:.0%}"
    return flag


def rebalance_table(values: pd.Series) -> str:
    """!
    @brief Importe a comprar o vender de cada ETF para volver a @ref Bot.TARGET.
    @param values Valor en EUR por ticker.
    @return Tabla HTML (`<pre>`). No incluye comisiones ni impuestos.
    """
    total = values.sum()
    lines = ["<pre>"]
    for t in values.index:
        diff = engine.TARGET[t] * total - values[t]
        lines.append(f"{'Comprar' if diff >= 0 else 'Vender ':<8}{SHORT[t]:<6}{abs(diff):>9,.0f} €")
    lines.append("</pre>")
    return "\n".join(lines)


def portfolio_explain(values: pd.Series) -> str:
    """!
    @brief Texto que acompaña a @ref chart_portfolio con el estado de la banda,
           órdenes de rebalanceo y guía de lectura.
    @param values Valor en EUR por ticker.
    @return Texto en HTML de Telegram.
    """
    lines = [band_status(values), "", "<b>Para volver exactamente al objetivo</b>", rebalance_table(values)]
    lines += [
        "<b>Cómo leerlo</b>",
        "• <b>Dona:</b> qué parte de tu dinero está en cada fondo.",
        "• <b>Barras:</b> color = lo que tienes hoy · gris = lo que te propusiste. "
        "Si el color supera mucho al gris, ese fondo pesa de más.",
        f"• <b>Satélites</b> = EQQQ + SMH (los más arriesgados). Objetivo 30 %. Mientras estén entre "
        f"{engine.LOWER:.0%} y {engine.UPPER:.0%} no hay que hacer nada; fuera de esa banda conviene rebalancear.",
        "• <b>Rebalancear</b> = vender un poco de lo que ha subido y comprar lo que se ha quedado atrás. "
        "Mantiene el riesgo que elegiste. Las cifras de arriba no incluyen comisiones ni impuestos.",
    ]
    return "\n".join(lines)


def chart_backtest(rets: pd.DataFrame) -> tuple[io.BytesIO, str]:
    """!
    @brief Gráfico y tabla del backtest de las tres estrategias de @ref Bot.backtest.

    - Panel superior: evolución de 1 € invertido con cada estrategia; las
      etiquetas finales se separan para que no se solapen.
    - Panel inferior: drawdown (%) de cada estrategia.

    @param rets Rentabilidades diarias del periodo completo.
    @return Tupla `(imagen PNG, texto HTML)` con la tabla de métricas y la explicación.
    """
    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(8, 6.4), sharex=True,
                                  gridspec_kw={"height_ratios": [3, 1.4], "hspace": 0.08})
    rows, ends = {}, []
    for mode, label in [("hold", "Sin rebalancear"), ("anual", "Anual"), ("banda", "Banda 40/20")]:
        port, n = engine.backtest(rets, mode)
        wealth = (1 + port).cumprod()
        line, = ax.plot(wealth.index, wealth, label=f"{label} ({n} reb.)", lw=1.8)
        ends.append([wealth.iloc[-1], line.get_color()])
        dd = (wealth / wealth.cummax() - 1) * 100
        ax2.plot(dd.index, dd, color=line.get_color(), lw=1.1)
        rows[label] = engine.metrics(port) | {"Final": wealth.iloc[-1]}
    ax.set_xlim(right=wealth.index[-1] + pd.Timedelta(days=330))
    ends.sort()
    gap = 0.28  # separación mínima entre etiquetas para que no se solapen
    ys = [e[0] for e in ends]
    for i in range(1, len(ys)):
        ys[i] = max(ys[i], ys[i - 1] + gap)
    for (end, color), y in zip(ends, ys):
        ax.annotate(f"{end:.1f} €", (wealth.index[-1], end), xytext=(wealth.index[-1] + pd.Timedelta(days=40), y),
                    color=color, va="center", fontweight="bold",
                    arrowprops={"arrowstyle": "-", "color": color, "lw": 0.8})
    ax.set_title("Backtest 10 años: qué habría pasado con 1 € invertido")
    ax.set_ylabel("Valor de tu 1 €")
    ax.legend(frameon=False, loc="upper left")
    ax.grid(alpha=0.25)
    ax2.set_ylabel("Caída desde\nsu máximo (%)")
    ax2.grid(alpha=0.25)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    lines = ["<pre>", f"{'':<16}{'Anual':>6}{'Vol':>7}{'Peor':>7}{'Sharpe':>7}"]
    for k, m in rows.items():
        lines.append(f"{k:<16}{m['CAGR']:>6.1%}{m['Volatilidad']:>7.1%}{m['Max DD']:>7.1%}{m['Sharpe']:>7.2f}")
    lines += ["</pre>", "<b>En cristiano</b>"]
    lines += [f"• {k}: 1 € → <b>{m['Final']:.1f} €</b>" for k, m in rows.items()]
    lines += [
        "",
        "<b>Qué es cada columna</b>",
        "• <b>Anual</b>: rentabilidad media por año (CAGR), con el interés compuesto.",
        "• <b>Vol</b>: volatilidad, cuánto “se mueve” el valor. Más alta = más sustos.",
        "• <b>Peor</b>: la mayor caída que hubo desde un máximo hasta el mínimo siguiente "
        "(el peor momento que habrías vivido).",
        "• <b>Sharpe</b>: ganancia por cada unidad de riesgo. Por encima de 1 se considera buena.",
        "",
        "<b>Cómo leer el gráfico</b>",
        "• Arriba, la línea que acaba más alta ganó más. Abajo, los valles son las caídas: "
        "cuanto más profundos, peor se pasó.",
        "• <i>Rebalanceo anual</i>: ajustar la cartera 1 vez al año. <i>Banda</i>: solo ajustar cuando "
        "los satélites salen del 20–40 %. <i>Sin rebalancear</i>: no tocar nada.",
        "⚠️ Es el pasado simulado (con un 0,1 % de coste al rebalancear). No garantiza el futuro.",
    ]
    return fig_to_png(fig), "\n".join(lines)


# ───────────── acciones (sirven para comandos y botones) ─────────────
# Cada acción recibe dos corrutinas: send(texto_html) y photo(imagen, pie).
async def prices_1y() -> pd.DataFrame:
    """!
    @brief Descarga un año de precios sin bloquear el bucle de eventos.
    @return Precios en EUR del último año (ver @ref Bot.download).
    """
    return await asyncio.to_thread(engine.download, 1)


async def act_precio(send, photo):
    """!
    @brief Acción `precio`: tabla de precios y su explicación.
    @param send Corrutina que envía un texto HTML al chat.
    @param photo Corrutina que envía una imagen con pie de foto (no se usa).
    """
    await send(price_table(await prices_1y()))
    await send(price_explain())


async def act_grafico(send, photo):
    """!
    @brief Acción `grafico`: gráfico del último año y su resumen.
    @param send Corrutina que envía un texto HTML al chat.
    @param photo Corrutina que envía una imagen con pie de foto.
    """
    px = await prices_1y()
    await photo(chart_prices(px), "Último año: evolución y caídas desde máximos")
    await send(chart_prices_explain(px))


async def act_cartera(send, photo):
    """!
    @brief Acción `cartera`: gráfico de la cartera, estado de la banda y rebalanceo.

    Si `portfolio.json` no existe o está vacío, indica cómo crearlo.

    @param send Corrutina que envía un texto HTML al chat.
    @param photo Corrutina que envía una imagen con pie de foto.
    """
    px = await prices_1y()
    values, total = portfolio_state(px)
    if total == 0:
        await send("Aún no hay posiciones. Copia <code>portfolio.example.json</code> a "
                   "<code>portfolio.json</code> y pon tus participaciones.")
        return
    await photo(chart_portfolio(values), f"Valor total: {total:,.2f} €")
    await send(portfolio_explain(values))


async def act_backtest(send, photo):
    """!
    @brief Acción `backtest`: descarga 10 años y muestra @ref chart_backtest.
    @param send Corrutina que envía un texto HTML al chat.
    @param photo Corrutina que envía una imagen con pie de foto.
    """
    await send("Calculando backtest, tarda unos segundos…")
    px = await asyncio.to_thread(engine.download)
    img, text = chart_backtest(px.pct_change().dropna())
    await photo(img, "Backtest a 10 años de las 3 estrategias")
    await send(text)


## Acciones disponibles por nombre; el mismo nombre sirve de comando y de `callback_data`.
ACTIONS = {"precio": act_precio, "grafico": act_grafico, "cartera": act_cartera, "backtest": act_backtest}


def make_handlers(action):
    """!
    @brief Envuelve una acción para que los errores lleguen al usuario como mensaje.

    Un `TimedOut` de Telegram y cualquier otra excepción (p. ej. fallo al
    descargar datos) se registran en el log y se responde con un aviso, en
    lugar de dejar el chat sin respuesta.

    @param action Una de las corrutinas de @ref ACTIONS.
    @return Corrutina `run(chat_send, chat_photo)` que ejecuta la acción protegida.
    """
    async def run(chat_send, chat_photo):
        try:
            await action(chat_send, chat_photo)
        except TimedOut:
            log.warning("Telegram tardó demasiado en responder")
            await chat_send("⏳ Telegram tardó demasiado en responder. Inténtalo de nuevo en un momento.")
        except Exception:
            log.exception("fallo en acción")
            await chat_send("❌ No pude obtener los datos ahora mismo. Inténtalo de nuevo en un rato.")
    return run


async def dispatch(name: str, update: Update):
    """!
    @brief Ejecuta una acción en el chat del update y vuelve a mostrar el menú.
    @param name Clave de @ref ACTIONS.
    @param update Update de Telegram (comando o pulsación de botón).
    """
    chat = update.effective_chat

    async def send(text):
        await chat.send_message(text, parse_mode="HTML")

    async def photo(img, caption):
        await chat.send_photo(img, caption=caption)

    await make_handlers(ACTIONS[name])(send, photo)
    await show_menu(chat, "¿Qué más quieres ver?")


def command(name):
    """!
    @brief Crea el handler de un comando `/name` que lanza la acción homónima.
    @param name Clave de @ref ACTIONS.
    @return Callback compatible con `CommandHandler`.
    """
    async def handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await dispatch(name, update)
    return handler


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """!
    @brief Handler de los botones del menú en línea.

    `suscribir`/`cancelar` cambian la suscripción y actualizan el teclado en
    el mismo mensaje; el resto de valores conocidos ejecutan su acción.

    @param update Update con la `callback_query`.
    @param ctx Contexto de python-telegram-bot.
    """
    q = update.callback_query
    chat = update.effective_chat
    if q.data in ("suscribir", "cancelar"):
        on = q.data == "suscribir"
        set_subscription(chat.id, on)
        await q.answer("Resumen diario activado ✅" if on else "Resumen diario desactivado")
        await q.edit_message_reply_markup(menu_markup(chat.id))
    elif q.data in ACTIONS:
        await q.answer()
        await dispatch(q.data, update)
    else:
        await q.answer()


async def menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """!
    @brief Handler de `/start`, `/menu` y `/ayuda`: saluda y muestra el menú.
    @param update Update de Telegram.
    @param ctx Contexto de python-telegram-bot.
    """
    await show_menu(update.effective_chat, "Hola 👋 Soy el bot de la cartera. ¿Qué quieres ver?")


# ───────────── alertas de rebalanceo ─────────────
def band_state(values: pd.Series) -> str:
    """!
    @brief Clasifica el peso de los satélites respecto a la banda.

    @param values Valor en EUR por ticker.
    @return Uno de:
            - `"upper"`: ≥ @ref Bot.UPPER (hay que rebalancear).
            - `"lower"`: ≤ @ref Bot.LOWER (hay que rebalancear).
            - `"near_upper"`: a menos de @ref NEAR del límite superior.
            - `"near_lower"`: a menos de @ref NEAR del límite inferior.
            - `"ok"`: zona tranquila.
    """
    w = values[engine.SATELLITES].sum() / values.sum()
    if w >= engine.UPPER:
        return "upper"
    if w <= engine.LOWER:
        return "lower"
    if w >= engine.UPPER - NEAR:
        return "near_upper"
    if w <= engine.LOWER + NEAR:
        return "near_lower"
    return "ok"


def alert_text(state: str, values: pd.Series) -> str:
    """!
    @brief Mensaje de alerta para un estado de @ref band_state.

    Fuera de la banda incluye la @ref rebalance_table; cerca de ella solo
    recuerda los límites.

    @param state Estado devuelto por @ref band_state.
    @param values Valor en EUR por ticker.
    @return Texto en HTML de Telegram.
    """
    w = values[engine.SATELLITES].sum() / values.sum()
    head = {
        "upper": "🚨 <b>Toca rebalancear</b>: los satélites pesan demasiado.",
        "lower": "🚨 <b>Toca rebalancear</b>: los satélites pesan muy poco.",
        "near_upper": "⚠️ <b>Atención</b>: los satélites se acercan al límite superior.",
        "near_lower": "⚠️ <b>Atención</b>: los satélites se acercan al límite inferior.",
        "ok": "✅ Los satélites han vuelto a la zona tranquila de la banda.",
    }[state]
    text = f"{head}\n{band_status(values)}"
    if state in ("upper", "lower"):
        text += ("\n\n<b>Para volver al objetivo</b>\n" + rebalance_table(values) +
                 "Rebalancear = vender lo que pesa de más y comprar lo que pesa de menos.")
    elif state != "ok":
        text += f"\nBanda de acción: {engine.LOWER:.0%}–{engine.UPPER:.0%} (objetivo 30 %). Aún no hay que hacer nada."
    return text


async def check_alerts(ctx: ContextTypes.DEFAULT_TYPE):
    """!
    @brief Trabajo diario: avisa solo cuando cambia el estado respecto al último chequeo,
           para no repetir mensajes.

    Compara @ref band_state con el guardado en `alert_state.json`; si es
    distinto, envía @ref alert_text a todos los chats autorizados y guarda
    el nuevo estado. No hace nada si la cartera está vacía.

    @param ctx Contexto del job; `ctx.bot_data["allowed"]` contiene los chats autorizados.
    """
    px = await prices_1y()
    values, total = portfolio_state(px)
    if total == 0:
        return
    state = band_state(values)
    last = load_json(ALERT_STATE_FILE, {}).get("state", "ok")
    if state == last:
        return
    for chat_id in ctx.bot_data["allowed"]:
        try:
            await ctx.bot.send_message(chat_id, alert_text(state, values), parse_mode="HTML")
        except Exception:
            log.exception("no pude enviar la alerta a %s", chat_id)
    ALERT_STATE_FILE.write_text(json.dumps({"state": state}))


# ───────────── acceso restringido ─────────────
def parse_allowed(raw: str | None) -> set[int]:
    """!
    @brief Convierte `ALLOWED_CHAT_IDS` en un conjunto de enteros.

    Ignora espacios y entradas no numéricas; admite IDs negativos (grupos).

    @param raw Valor de la variable de entorno, p. ej. `"123, -456"`, o `None`.
    @return Conjunto de chat IDs (vacío si no hay ninguno válido).
    """
    return {int(x) for x in (raw or "").replace(" ", "").split(",") if x.lstrip("-").isdigit()}


async def my_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """!
    @brief Handler de `/id`: responde con el chat ID y cómo autorizarlo.

    Es el único comando que @ref guard deja pasar a chats no autorizados.

    @param update Update de Telegram.
    @param ctx Contexto de python-telegram-bot.
    """
    cid = update.effective_chat.id
    await update.message.reply_text(
        f"Tu chat ID es {cid}\nPara autorizarlo, ponlo en .env:\nALLOWED_CHAT_IDS={cid}")


async def guard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """!
    @brief Bloquea a cualquier chat que no esté en ALLOWED_CHAT_IDS (salvo /id, para poder configurarlo).

    Se registra en el grupo -1 para ejecutarse antes que el resto de
    handlers. A los chats no autorizados les responde que el bot es privado
    y corta el procesamiento.

    @param update Update entrante de cualquier tipo.
    @param ctx Contexto; `ctx.bot_data["allowed"]` contiene los chats autorizados.
    @exception ApplicationHandlerStop Si el chat no está autorizado.
    """
    chat = update.effective_chat
    if chat is None or chat.id in ctx.bot_data["allowed"]:
        return
    msg = update.effective_message
    if msg and (msg.text or "").split("@")[0].strip() == "/id":
        return
    log.warning("acceso denegado al chat %s", chat.id)
    if msg:
        await msg.reply_text("🔒 Este bot es privado.")
    elif update.callback_query:
        await update.callback_query.answer("Bot privado", show_alert=True)
    raise ApplicationHandlerStop


# ───────────── resumen diario ─────────────
def set_subscription(chat_id: int, on: bool):
    """!
    @brief Añade o quita un chat de `subscribers.json`.
    @param chat_id Chat a modificar.
    @param on `True` para suscribir, `False` para cancelar.
    """
    subs = set(load_json(SUBS_FILE, []))
    subs.add(chat_id) if on else subs.discard(chat_id)
    SUBS_FILE.write_text(json.dumps(sorted(subs)))


async def suscribir(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """!
    @brief Handler de `/suscribir`: activa el resumen diario para el chat.
    @param update Update de Telegram.
    @param ctx Contexto de python-telegram-bot.
    """
    set_subscription(update.effective_chat.id, True)
    await update.message.reply_text("✅ Te enviaré el resumen cada día laborable a las 22:00.")
    await show_menu(update.effective_chat)


async def cancelar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """!
    @brief Handler de `/cancelar`: desactiva el resumen diario para el chat.
    @param update Update de Telegram.
    @param ctx Contexto de python-telegram-bot.
    """
    set_subscription(update.effective_chat.id, False)
    await update.message.reply_text("Listo, resumen diario desactivado.")
    await show_menu(update.effective_chat)


async def daily_summary(ctx: ContextTypes.DEFAULT_TYPE):
    """!
    @brief Trabajo diario: envía la tabla de precios y el estado de la cartera a los suscriptores.

    Si un envío falla (p. ej. el usuario bloqueó el bot), se registra y se
    continúa con el siguiente.

    @param ctx Contexto del job de python-telegram-bot.
    """
    subs = load_json(SUBS_FILE, [])
    if not subs:
        return
    px = await prices_1y()
    text = price_table(px)
    values, total = portfolio_state(px)
    if total > 0:
        text += f"\n\n<b>Cartera:</b> {total:,.2f} €\n{band_status(values)}"
    for chat_id in subs:
        try:
            await ctx.bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception:
            log.exception("no pude enviar a %s", chat_id)


async def post_init(app: Application):
    """!
    @brief Registra @ref COMMANDS en Telegram una vez inicializada la aplicación.
    @param app Aplicación de python-telegram-bot.
    """
    await app.bot.set_my_commands(COMMANDS)


def main():
    """!
    @brief Punto de entrada: configura y arranca el bot en modo polling.

    Lee `.env`, construye la aplicación con timeouts amplios (subir imágenes
    puede tardar), registra @ref guard antes de todos los handlers, los
    comandos y botones, y programa @ref daily_summary y @ref check_alerts
    de lunes a viernes.

    @exception SystemExit Si falta `TELEGRAM_BOT_TOKEN`.
    """
    load_dotenv(BASE / ".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN en .env")
    allowed = parse_allowed(os.getenv("ALLOWED_CHAT_IDS"))
    if not allowed:
        log.warning("ALLOWED_CHAT_IDS vacío: nadie puede usar el bot. Escríbele /id para conocer tu ID.")
    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .connect_timeout(15)
        .read_timeout(30)
        .write_timeout(60)  # subir imágenes puede tardar con conexiones lentas
        .build()
    )
    app.bot_data["allowed"] = allowed
    app.add_handler(TypeHandler(Update, guard), group=-1)
    app.add_handler(CommandHandler("id", my_id))
    app.add_handler(CommandHandler(["start", "menu", "ayuda"], menu))
    for name in ACTIONS:
        app.add_handler(CommandHandler(name, command(name)))
    app.add_handler(CommandHandler("suscribir", suscribir))
    app.add_handler(CommandHandler("cancelar", cancelar))
    app.add_handler(CallbackQueryHandler(on_button))
    app.job_queue.run_daily(daily_summary, SUMMARY_TIME, days=(0, 1, 2, 3, 4))
    app.job_queue.run_daily(check_alerts, ALERT_TIME, days=(0, 1, 2, 3, 4))
    log.info("Bot en marcha")
    app.run_polling()


if __name__ == "__main__":
    main()
