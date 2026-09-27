"""Корректирующие коэффициенты поверх готового прогноза — для ползунков интерфейса («что, если»).

Модель не переобучается: почасовой прогноз (build_final, year_forecast или ml/outputs/forecast_hourly_*.csv)
умножается на коэффициенты. 14 640 ячеек пересчитываются за миллисекунды, поэтому изменение видно сразу.
  • погода — осадки дня, мм: k = exp(β · (log1p(мм) − норма месяца)). Норма — средний log1p суточных осадков этого
    месяца в 2025 году: обычная погода месяца уже заложена в базовый прогноз, поправка — только отклонение от неё.
    β = −0.012: вне обучающей выборки улучшает 5 из 5 фолдов (регрессия на январе–октябре даёт −0.026, t = −5.5);
  • сезон — множитель на месяц, {12: 1.03};
  • маршрут — множитель на маршрут, {17: 0.95};
  • события — список в формате ml/regimes.json → effects: {routes, start, end, hours, dow, mult}
    (перекрытие, ремонт, концерт, отмена рейсов).
Факт 1.11 00–01 ч не меняется.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import FC_START
from .regimes import apply_effects
from .weather import daily_precip

WEATHER_BETA = -0.012


def month_norm(precip: pd.Series) -> pd.Series:
    """Средний log1p суточных осадков по месяцу года (1…12) — «обычная погода», заложенная в базовый прогноз."""
    return np.log1p(precip).groupby(precip.index.month).mean()


def weather_factor(dates: pd.Series, precip_mm: dict | pd.Series | str, beta: float = WEATHER_BETA,
                   offline: bool = True) -> np.ndarray:
    """Множитель дня по осадкам. precip_mm: {дата: мм} для изменённых дней (остальные = 1) или 'archive' —
    фактические осадки 2025 года из ml/external/weather."""
    hist = daily_precip(offline)
    if isinstance(precip_mm, str):
        if precip_mm != "archive" or hist is None:
            raise ValueError("precip_mm: {дата: мм} или 'archive' (нужен ml/external/weather/precip_moscow_2025_daily.csv)")
        precip_mm = hist
    mm = pd.Series(precip_mm, dtype=float)
    mm.index = pd.to_datetime(mm.index)
    norm = month_norm(hist) if hist is not None else pd.Series(0.0, index=range(1, 13))
    x = pd.Series(mm.reindex(dates).to_numpy(), index=dates.index)
    k = np.exp(beta * (np.log1p(x) - dates.dt.month.map(norm)))
    return k.fillna(1.0).to_numpy()


def adjust(base: pd.DataFrame, precip_mm: dict | pd.Series | str | None = None, beta: float = WEATHER_BETA,
           month_mult: dict | None = None, route_mult: dict | None = None, events: list[dict] | None = None,
           offline: bool = True) -> pd.DataFrame:
    """base: route, date, hour, pred (или prediction). Возвращает те же строки + base (исходный прогноз) и k."""
    col = "pred" if "pred" in base else "prediction"
    out = base.copy()
    dates = pd.to_datetime(out["date"])
    k = np.ones(len(out))
    if precip_mm is not None:
        k *= weather_factor(dates, precip_mm, beta, offline)
    if month_mult:
        k *= dates.dt.month.map(month_mult).fillna(1.0).to_numpy()
    if route_mult:
        k *= out["route"].map(route_mult).fillna(1.0).to_numpy()
    k[((dates == FC_START) & (out["hour"] <= 1)).to_numpy()] = 1.0
    out["base"] = out[col]
    out[col] = out[col] * k
    if events:
        out = apply_effects(out, events, col)
    out["k"] = np.where(out["base"] > 0, out[col] / out["base"].where(out["base"] > 0, 1), 1.0).round(4)
    return out


def impact(adj: pd.DataFrame, by: str = "route") -> pd.DataFrame:
    """Сводка изменения по ключу (route, date, …) и строка сети (all): base, pred, delta, delta_pct."""
    col = "pred" if "pred" in adj else "prediction"
    g = adj.groupby(by)[["base", col]].sum().rename(columns={col: "pred"})
    g.index = g.index.astype(str)
    g.loc["all"] = g.sum()
    g = np.rint(g).astype(int)
    g["delta"] = g["pred"] - g["base"]
    g["delta_pct"] = (100 * g["delta"] / g["base"].where(g["base"] > 0)).round(2)
    return g.rename_axis(by).reset_index()
