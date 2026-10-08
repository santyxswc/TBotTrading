"""!
@file Bot.py
@brief Motor de datos y backtest de la cartera núcleo-satélite.

Descarga precios ajustados (total return) de los ETF de la cartera, los
convierte a EUR, calcula métricas de riesgo/rentabilidad y simula tres
estrategias de rebalanceo:

- **hold**: comprar y no tocar nunca la cartera.
- **anual**: volver a los pesos objetivo una vez al año.
- **banda**: rebalancear solo cuando el peso conjunto de los satélites sale
  de la banda [@ref Bot.LOWER, @ref Bot.UPPER].

Se puede ejecutar directamente (`python Bot.py`) para imprimir la tabla
comparativa del backtest, y además lo importa @ref telegram_bot.py como
motor de cálculo (`import Bot as engine`).

@author santyxswc
"""

import numpy as np
import pandas as pd

# ───────────── CONFIGURACIÓN ─────────────
## Años de histórico que se descargan por defecto para el backtest.
YEARS = 10
## @brief Pesos objetivo de cada ETF sobre el total de la cartera (suman 1).
#
# - `IWDA.AS`: núcleo, renta variable global desarrollada (70 %).
# - `EQQQ.DE`: satélite 1, Nasdaq-100 (15 %).
# - `SMH`: satélite 2, semiconductores; cotiza en USD (15 %).
TARGET = {             # pesos objetivo sobre el total de la cartera
    "IWDA.AS": 0.70,   # núcleo (VWCE.DE solo cotiza desde jul-2019: no da 10 años)
    "EQQQ.DE": 0.15,   # satélite 1 (Nasdaq-100)
    "SMH": 0.15,       # satélite 2 (semiconductores, cotiza en USD)
}
## Tickers que forman la parte satélite (su peso conjunto es el que controla la banda).
SATELLITES = ["EQQQ.DE", "SMH"]
## Tickers cotizados en USD; se convierten a EUR dividiendo por `EURUSD=X`.
USD_TICKERS = ["SMH"]          # se convierten a EUR con EURUSD=X
## Límite superior de la banda: peso de satélites a partir del cual se rebalancea (30 % + 10 pp).
UPPER = 0.40
## Límite inferior de la banda: peso de satélites por debajo del cual se rebalancea (30 % − 10 pp).
LOWER = 0.20
## Coste de transacción aplicado sobre el importe operado en cada rebalanceo (0,1 %).
COST = 0.001                   # coste de transacción: 0,1% sobre lo operado
## Tasa libre de riesgo anual usada en el ratio de Sharpe.
RF = 0.0                       # tasa libre de riesgo para el Sharpe
## Sesiones bursátiles por año, para anualizar la volatilidad diaria.
TRADING_DAYS = 252


def download(years: int = YEARS) -> pd.DataFrame:
    """!
    @brief Precios ajustados (total return) en EUR, ventana común de todos los activos.

    Descarga de Yahoo Finance los cierres ajustados de los tickers de
    @ref TARGET y el cambio `EURUSD=X`, convierte a EUR los de
    @ref USD_TICKERS, rellena hacia delante los días sin cotización y recorta
    a las fechas en las que todos los activos tienen precio.

    @param years Número de años de histórico a descargar.
    @return DataFrame indexado por fecha con una columna por ticker de
            @ref TARGET (en ese mismo orden) y precios en EUR.
    @note `yfinance` se importa dentro de la función para que el módulo se
          pueda importar sin red ni dependencia instalada.
    """
    import yfinance as yf

    tickers = list(TARGET) + ["EURUSD=X"]
    px = yf.download(tickers, period=f"{years}y", auto_adjust=True, progress=False)["Close"]
    for t in USD_TICKERS:
        px[t] = px[t] / px["EURUSD=X"]
    px = px.drop(columns="EURUSD=X").ffill().dropna()
    return px[list(TARGET)]


def metrics(r: pd.Series) -> dict:
    """!
    @brief Rendimiento compuesto (CAGR), volatilidad anualizada y Max Drawdown.

    También calcula los ratios de Sharpe (con @ref RF) y Calmar.

    @param r Serie de rentabilidades diarias simples, indexada por fecha.
    @return Diccionario con las claves:
            - `"CAGR"`: tasa de crecimiento anual compuesta.
            - `"Volatilidad"`: desviación típica anualizada (√@ref TRADING_DAYS).
            - `"Max DD"`: peor caída desde un máximo (valor negativo).
            - `"Sharpe"`: (CAGR − RF) / volatilidad.
            - `"Calmar"`: CAGR / |Max DD|.
    """
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
    """!
    @brief Simula la cartera con una estrategia de rebalanceo.

    Parte de una cartera de valor 1 repartida según @ref TARGET y la hace
    evolucionar día a día con las rentabilidades de cada activo. Cuando la
    estrategia lo indica, vuelve a los pesos objetivo descontando
    @ref COST sobre el importe operado.

    @param rets DataFrame de rentabilidades diarias con las columnas de @ref TARGET.
    @param mode Estrategia:
                - `'hold'`: sin rebalancear.
                - `'anual'`: una vez al año, el primer día de cada año nuevo.
                - `'banda'`: si los satélites pesan ≥ @ref UPPER o ≤ @ref LOWER.
    @return Tupla `(port, n_reb)`: serie de rentabilidades diarias de la
            cartera y número de rebalanceos realizados.
    """
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
    """!
    @brief Punto de entrada por consola.

    Descarga @ref YEARS años de precios y muestra una tabla con las métricas
    de cada ETF por separado y de las tres estrategias de @ref backtest.
    """
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
