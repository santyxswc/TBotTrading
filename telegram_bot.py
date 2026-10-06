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

MENU = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("💶 Precios", callback_data="precio"),
         InlineKeyboardButton("📈 Gráfico 1 año", callback_data="grafico")],
        [InlineKeyboardButton("🥧 Cartera", callback_data="cartera"),
         InlineKeyboardButton("🧪 Backtest", callback_data="backtest")],
    ]
)


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


def price_table(px: pd.DataFrame) -> str:
    last = px.iloc[-1]
    # download() rellena con ffill los días sin cotización: comparamos con el último precio distinto
    prev = pd.Series({t: px[t][px[t] != last[t]].iloc[-1] for t in px})
    lines = ["<b>Precios (EUR)</b>", f"<i>{px.index[-1].date()}</i>", "<pre>"]
    for t in px:
        chg = last[t] / prev[t] - 1
        arrow = "🟢" if chg >= 0 else "🔴"
        lines.append(f"{arrow} {NAMES[t]:<18}{last[t]:>8.2f} {chg:>+7.2%}")
    lines.append("</pre>")
    return "\n".join(lines)


def chart_prices(px: pd.DataFrame) -> io.BytesIO:
    norm = px / px.iloc[0] * 100
    fig, ax = plt.subplots(figsize=(8, 4.5))
    for t in norm:
        ax.plot(norm.index, norm[t], label=NAMES[t], color=COLORS[t], lw=1.8)
    ax.axhline(100, color="gray", lw=0.8, ls="--")
    ax.set_title("Rendimiento último año (base 100, EUR)")
    ax.legend(frameon=False)
    ax.grid(alpha=0.25)
    fig.autofmt_xdate()
    return fig_to_png(fig)


def portfolio_state(px: pd.DataFrame):
    holdings = load_json(PORTFOLIO_FILE, {})
    values = pd.Series({t: holdings.get(t, 0) * px[t].iloc[-1] for t in engine.TARGET})
    return values, values.sum()


def chart_portfolio(values: pd.Series) -> io.BytesIO:
    w = values / values.sum()
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(9, 4))
    _, _, pcts = a1.pie(w, colors=[COLORS[t] for t in w.index], autopct="%1.1f%%",
                        startangle=90, pctdistance=0.78, wedgeprops={"width": 0.45})
    for t in pcts:
        t.set_color("white")
    a1.legend([NAMES[t] for t in w.index], loc="upper center", bbox_to_anchor=(0.5, 0.02),
              frameon=False, ncol=1)
    a1.set_title("Pesos actuales")
    x = range(len(w))
    a2.bar([i - 0.2 for i in x], w.values * 100, 0.4, label="Actual", color="#2a6fdb")
    a2.bar([i + 0.2 for i in x], [engine.TARGET[t] * 100 for t in w.index], 0.4,
           label="Objetivo", color="#c9ced6")
    a2.set_xticks(list(x), [t.split(".")[0] for t in w.index])
    a2.set_ylabel("%")
    a2.set_title("Actual vs objetivo")
    a2.legend(frameon=False)
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


def chart_backtest(rets: pd.DataFrame) -> tuple[io.BytesIO, str]:
    fig, ax = plt.subplots(figsize=(8, 4.5))
    rows = {}
    for mode, label in [("hold", "Sin rebalancear"), ("anual", "Anual"), ("banda", "Banda 40/20")]:
        port, n = engine.backtest(rets, mode)
        ax.plot(port.index, (1 + port).cumprod(), label=f"{label} ({n} reb.)", lw=1.8)
        m = engine.metrics(port)
        rows[label] = m
    ax.set_title("Backtest 10 años (valor de 1 € invertido)")
    ax.legend(frameon=False)
    ax.grid(alpha=0.25)
    lines = ["<pre>", f"{'':<16}{'CAGR':>7}{'Vol':>7}{'MaxDD':>8}{'Sharpe':>7}"]
    for k, m in rows.items():
        lines.append(f"{k:<16}{m['CAGR']:>7.1%}{m['Volatilidad']:>7.1%}{m['Max DD']:>8.1%}{m['Sharpe']:>7.2f}")
    lines.append("</pre>")
    return fig_to_png(fig), "\n".join(lines)


# ───────────── acciones (sirven para comandos y botones) ─────────────
async def prices_1y() -> pd.DataFrame:
    return await asyncio.to_thread(engine.download, 1)


async def act_precio(send, photo):
    await send(price_table(await prices_1y()))


async def act_grafico(send, photo):
    await photo(chart_prices(await prices_1y()), "Último año, base 100")


async def act_cartera(send, photo):
    px = await prices_1y()
    values, total = portfolio_state(px)
    if total == 0:
        await send("Aún no hay posiciones. Edita <code>portfolio.json</code> con tus participaciones.")
        return
    await photo(chart_portfolio(values), f"Valor total: {total:,.2f} €")
    await send(band_status(values))


async def act_backtest(send, photo):
    await send("Calculando backtest, tarda unos segundos…")
    px = await asyncio.to_thread(engine.download)
    img, table = chart_backtest(px.pct_change().dropna())
    await photo(img, "")
    await send(table)


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


def command(name):
    async def handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await dispatch(name, update)
    return handler


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await dispatch(q.data, update)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hola 👋 Soy el bot de la cartera.\n\n"
        "/precio · /grafico · /cartera · /backtest\n"
        "/suscribir – resumen diario a las 22:00 (Madrid)\n"
        "/cancelar – dejar de recibirlo",
        reply_markup=MENU,
    )


# ───────────── resumen diario ─────────────
def set_subscription(chat_id: int, on: bool):
    subs = set(load_json(SUBS_FILE, []))
    subs.add(chat_id) if on else subs.discard(chat_id)
    SUBS_FILE.write_text(json.dumps(sorted(subs)))


async def suscribir(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    set_subscription(update.effective_chat.id, True)
    await update.message.reply_text("✅ Te enviaré el resumen cada día laborable a las 22:00.")


async def cancelar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    set_subscription(update.effective_chat.id, False)
    await update.message.reply_text("Listo, resumen diario desactivado.")


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


def main():
    load_dotenv(BASE / ".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN en .env")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "ayuda"], start))
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
