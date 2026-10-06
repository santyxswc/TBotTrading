import numpy as np
import pandas as pd

# ───────────── CONFIGURACIÓN ─────────────
YEARS = 10
TARGET = {             # pesos objetivo sobre el total de la cartera
    "IWDA.AS": 0.70,   # núcleo (VWCE.DE solo cotiza desde jul-2019: no da 10 años)
    "EQQQ.DE": 0.15,   # satélite 1 (Nasdaq-100)
    "SMH": 0.15,       # satélite 2 (semiconductores, cotiza en USD)
}
SATELLITES = ["EQQQ.DE", "SMH"]
USD_TICKERS = ["SMH"]          # se convierten a EUR con EURUSD=X
UPPER, LOWER = 0.40, 0.20      # bandas del satélite: 30% ± 10 pp
COST = 0.001                   # coste de transacción: 0,1% sobre lo operado
RF = 0.0                       # tasa libre de riesgo para el Sharpe
TRADING_DAYS = 252


def download(years: int = YEARS) -> pd.DataFrame:
    """Precios ajustados (total return) en EUR, ventana común de todos los activos."""
    import yfinance as yf

    tickers = list(TARGET) + ["EURUSD=X"]
    px = yf.download(tickers, period=f"{years}y", auto_adjust=True, progress=False)["Close"]
    for t in USD_TICKERS:
        px[t] = px[t] / px["EURUSD=X"]
    px = px.drop(columns="EURUSD=X").ffill().dropna()
    return px[list(TARGET)]


def metrics(r: pd.Series) -> dict:
    """Rendimiento compuesto (CAGR), volatilidad anualizada y Max Drawdown."""
    years = (r.index[-1] - r.index[0]).days / 365.25
    wealth = np.concatenate([[1.0], (1 + r).cumprod().values])
    cagr = wealth[-1] ** (1 / years) - 1
    vol = r.std() * np.sqrt(TRADING_DAYS)
    mdd = (wealth / np.maximum.accumulate(wealth) - 1).min()
    return {
        "CAGR": cagr,
        "Volatilidad": vol,
        "Max DD": mdd,
        "Sharpe": (cagr - RF) / vol,
        "Calmar": cagr / abs(mdd),
    }


def backtest(rets: pd.DataFrame, mode: str):
    """mode: 'hold' (sin rebalancear), 'anual' (1 vez al año) o 'banda' (si satélite >=40% o <=20%)."""
    cols = list(TARGET)
    tgt = np.array([TARGET[c] for c in cols])
    is_sat = np.array([c in SATELLITES for c in cols])
    v = tgt.copy()                       # valor por activo (cartera inicial = 1)
    equity, n_reb = [], 0
    prev_year = rets.index[0].year

    for date, r in zip(rets.index, rets.values):
        v = v * (1 + r)
        total = v.sum()
        w_sat = v[is_sat].sum() / total

        trigger = False
        if mode == "banda":
            trigger = w_sat >= UPPER or w_sat <= LOWER
        elif mode == "anual":
            trigger = date.year != prev_year
        prev_year = date.year

        if trigger:
            traded = np.abs(total * tgt - v).sum() / 2
            total -= traded * COST
            v = total * tgt
            n_reb += 1
        equity.append(v.sum())

    eq = pd.Series(equity, index=rets.index)
    port = eq.pct_change()
    port.iloc[0] = eq.iloc[0] - 1
    return port, n_reb


def main():
    px = download()
    print(f"Ventana común: {px.index[0].date()} → {px.index[-1].date()}\n")
    rets = px.pct_change().dropna()

    rows = {t: metrics(rets[t]) for t in rets}
    for mode, label in [
        ("hold", "Cartera sin rebalancear"),
        ("anual", "Cartera rebalanceo anual"),
        ("banda", "Cartera banda 40%/20%"),
    ]:
        port, n = backtest(rets, mode)
        rows[f"{label} ({n} reb.)"] = metrics(port)

    df = pd.DataFrame(rows).T
    fmt = {c: "{:.1%}".format for c in ["CAGR", "Volatilidad", "Max DD"]}
    fmt.update({c: "{:.2f}".format for c in ["Sharpe", "Calmar"]})
    print(df.to_string(formatters=fmt))


if __name__ == "__main__":
    main()
