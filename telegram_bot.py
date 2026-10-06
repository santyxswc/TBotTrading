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
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

import Bot as engine

BASE = Path(__file__).parent
PORTFOLIO_FILE = BASE / "portfolio.json"
SUBS_FILE = BASE / "subscribers.json"
TZ = ZoneInfo("Europe/Madrid")
SUMMARY_TIME = time(22, 0, tzinfo=TZ)
NAMES = {"IWDA.AS": "Mundo (IWDA)", "EQQQ.DE": "Nasdaq-100 (EQQQ)", "SMH": "Semis (SMH)"}
COLORS = {"IWDA.AS": "#2a6fdb", "EQQQ.DE": "#1baa7f", "SMH": "#e08a1e"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("tbot")

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
    await chat.send_message(text, reply_markup=menu_markup(chat.id))


# ───────────── datos ─────────────
def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def fig_to_png(fig) -> io.BytesIO:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


SHORT = {t: t.split(".")[0] for t in engine.TARGET}
DESC = {
    "IWDA.AS": "miles de empresas grandes de países desarrollados (tu base, 70 %)",
    "EQQQ.DE": "las 100 mayores tecnológicas/no financieras del Nasdaq (15 %)",
    "SMH": "fabricantes de chips y semiconductores, más arriesgado (15 %)",
}


def fmt_pct(x: float, dec: int = 1) -> str:
    return f"{x:+.{dec}%}"


def price_table(px: pd.DataFrame) -> str:
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
    holdings = load_json(PORTFOLIO_FILE, {})
    values = pd.Series({t: holdings.get(t, 0) * px[t].iloc[-1] for t in engine.TARGET})
    return values, values.sum()


def chart_portfolio(values: pd.Series) -> io.BytesIO:
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
    w_sat = values[engine.SATELLITES].sum() / values.sum()
    if w_sat >= engine.UPPER:
        flag = f"⚠️ Satélites en {w_sat:.1%} (≥ {engine.UPPER:.0%}): <b>toca rebalancear</b>"
    elif w_sat <= engine.LOWER:
        flag = f"⚠️ Satélites en {w_sat:.1%} (≤ {engine.LOWER:.0%}): <b>toca rebalancear</b>"
    else:
        flag = f"✅ Satélites en {w_sat:.1%}, dentro de la banda {engine.LOWER:.0%}–{engine.UPPER:.0%}"
    return flag


def portfolio_explain(values: pd.Series) -> str:
    total = values.sum()
    lines = [band_status(values), "", "<b>Para volver exactamente al objetivo</b>", "<pre>"]
    for t in values.index:
        diff = engine.TARGET[t] * total - values[t]
        lines.append(f"{'Comprar' if diff >= 0 else 'Vender ':<8}{SHORT[t]:<6}{abs(diff):>9,.0f} €")
    lines += [
        "</pre>",
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
async def prices_1y() -> pd.DataFrame:
    return await asyncio.to_thread(engine.download, 1)


async def act_precio(send, photo):
    await send(price_table(await prices_1y()))
    await send(price_explain())


async def act_grafico(send, photo):
    px = await prices_1y()
    await photo(chart_prices(px), "Último año: evolución y caídas desde máximos")
    await send(chart_prices_explain(px))


async def act_cartera(send, photo):
    px = await prices_1y()
    values, total = portfolio_state(px)
    if total == 0:
        await send("Aún no hay posiciones. Copia <code>portfolio.example.json</code> a "
                   "<code>portfolio.json</code> y pon tus participaciones.")
        return
    await photo(chart_portfolio(values), f"Valor total: {total:,.2f} €")
    await send(portfolio_explain(values))


async def act_backtest(send, photo):
    await send("Calculando backtest, tarda unos segundos…")
    px = await asyncio.to_thread(engine.download)
    img, text = chart_backtest(px.pct_change().dropna())
    await photo(img, "Backtest a 10 años de las 3 estrategias")
    await send(text)


ACTIONS = {"precio": act_precio, "grafico": act_grafico, "cartera": act_cartera, "backtest": act_backtest}


def make_handlers(action):
    async def run(chat_send, chat_photo):
        try:
            await action(chat_send, chat_photo)
        except Exception:
            log.exception("fallo en acción")
            await chat_send("❌ No pude obtener los datos ahora mismo. Inténtalo de nuevo en un rato.")
    return run


async def dispatch(name: str, update: Update):
    chat = update.effective_chat

    async def send(text):
        await chat.send_message(text, parse_mode="HTML")

    async def photo(img, caption):
        await chat.send_photo(img, caption=caption)

    await make_handlers(ACTIONS[name])(send, photo)
    await show_menu(chat, "¿Qué más quieres ver?")


def command(name):
    async def handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await dispatch(name, update)
    return handler


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
    await show_menu(update.effective_chat, "Hola 👋 Soy el bot de la cartera. ¿Qué quieres ver?")


# ───────────── resumen diario ─────────────
def set_subscription(chat_id: int, on: bool):
    subs = set(load_json(SUBS_FILE, []))
    subs.add(chat_id) if on else subs.discard(chat_id)
    SUBS_FILE.write_text(json.dumps(sorted(subs)))


async def suscribir(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    set_subscription(update.effective_chat.id, True)
    await update.message.reply_text("✅ Te enviaré el resumen cada día laborable a las 22:00.")
    await show_menu(update.effective_chat)


async def cancelar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    set_subscription(update.effective_chat.id, False)
    await update.message.reply_text("Listo, resumen diario desactivado.")
    await show_menu(update.effective_chat)


async def daily_summary(ctx: ContextTypes.DEFAULT_TYPE):
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
    await app.bot.set_my_commands(COMMANDS)


def main():
    load_dotenv(BASE / ".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN en .env")
    app = Application.builder().token(token).post_init(post_init).build()
    app.add_handler(CommandHandler(["start", "menu", "ayuda"], menu))
    for name in ACTIONS:
        app.add_handler(CommandHandler(name, command(name)))
    app.add_handler(CommandHandler("suscribir", suscribir))
    app.add_handler(CommandHandler("cancelar", cancelar))
    app.add_handler(CallbackQueryHandler(on_button))
    app.job_queue.run_daily(daily_summary, SUMMARY_TIME, days=(0, 1, 2, 3, 4))
    log.info("Bot en marcha")
    app.run_polling()


if __name__ == "__main__":
    main()
