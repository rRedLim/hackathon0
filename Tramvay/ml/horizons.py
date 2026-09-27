"""Горизонты прогноза: день (по часам), месяц, год.

• День и месяц — основной прогноз ноября–декабря 2025 (build_final), почасовой; месяц = сумма дней.
• Год (2026) — качественный сценарий по дням и месяцам. В истории 10 месяцев и нет ни одного годового цикла,
  поэтому сезонность нельзя отделить от тренда. Месяц m 2026 года строится как тот же месяц 2025 года,
  переложенный на календарь 2026: модель обучается с отсечкой в конце месяца m 2025 (уровень, отношения
  типов дня и профили этого сезона) и прогнозирует месяц m 2026 с его днями недели, праздниками и переносами.
  Для ноября–декабря опора — наш прогноз 2025 года, откалиброванный на лидерборде. Поверх этого — известные
  структурные изменения: Т1 (7-й × 0.94 с 12.11.2025) и маршрут 5 (с 16.12.2025, как аналог 7-го).
  Тренд год к году из 10 месяцев не оценивается: trend = 1 — сценарный коэффициент.
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd

from .calendar_ru import load_calendar
from .config import DATA_DIR, HIST_END, HIST_ROUTES, NEW_ROUTE, Params, Route5
from .data import load_grid
from .regimes import apply_effects, load_regimes
from .structural import StructuralModel

T1_MULT = 0.94          # отток с 7-го на Т1 (ml/regimes.json → effects), с 12.11.2025 — постоянно
NYE_FREE_MULT = 0.10    # 31.12 с 20:00 бесплатный проезд (так было в 2025 году)


def year_effects(year: int) -> list[dict]:
    return [
        {"name": "Т1", "routes": [7], "start": f"{year}-01-01", "end": f"{year}-12-31", "mult": T1_MULT},
        {"name": "31.12 бесплатный проезд", "start": f"{year}-12-31", "end": f"{year}-12-31",
         "hours": [20, 21, 22, 23], "mult": NYE_FREE_MULT},
    ]


def year_forecast(year: int = 2026, trend: float = 1.0, offline: bool = False, data_dir=DATA_DIR,
                  params: Params | None = None) -> pd.DataFrame:
    """Почасовой прогноз на год (route, date, hour, pred). Почасовая форма — справочно: валидирован только
    горизонт 61 день; для года используйте дневные и месячные суммы (aggregate)."""
    P = params or Params()
    grid = load_grid(data_dir)
    cal = pd.concat([load_calendar(offline), load_calendar(offline, year)], ignore_index=True)
    regs = load_regimes()
    parts = []
    # янв–окт: «тот же месяц прошлого года» — модель с отсечкой в конце месяца, без поправок ноября–декабря
    same = replace(P, level_mult=1.0, level_month={}, route_mult={}, winter_alpha={})
    for m in range(1, HIST_END.month + 1):
        cut = pd.Timestamp(2025, m, 1) + pd.offsets.MonthEnd(0)
        model = StructuralModel(same, cal, None, regs).fit(grid, cut)
        parts.append(model.predict(pd.date_range(f"{year}-{m:02d}-01", periods=cut.day), HIST_ROUTES))
    # ноя–дек: модель итогового прогноза (отсечка 31.10, уровень ×1.02 и множители маршрутов с лидерборда)
    pre_ny = {**P.pre_ny, **{f"{year}{k[4:]}": v for k, v in P.pre_ny.items()}}
    final = StructuralModel(replace(P, pre_ny=pre_ny), cal, None, regs).fit(grid, HIST_END)
    parts.append(final.predict(pd.date_range(f"{year}-{HIST_END.month + 1:02d}-01", f"{year}-12-31"), HIST_ROUTES))
    pred = apply_effects(pd.concat(parts, ignore_index=True), year_effects(year))
    # маршрут 5: форма и сезонность 7-го, будний уровень 6 000 (как в ноябре–декабре 2025) → масштаб к уровню 7-го
    cfg = Route5()
    w7 = final.f.W[cfg.analog] * P.level_mult * P.route_mult.get(cfg.analog, 1.0) * T1_MULT
    r5 = pred[pred["route"] == cfg.analog].assign(route=NEW_ROUTE)
    r5["pred"] *= cfg.weekday_level / w7
    out = pd.concat([pred, r5], ignore_index=True)
    out["pred"] *= trend
    return out.sort_values(["route", "date", "hour"], ignore_index=True)


def aggregate(hourly: pd.DataFrame, level: str, col: str = "pred") -> pd.DataFrame:
    """level: 'day' → route, date; 'month' → route, month. Добавляет строки сети (route = 'all')."""
    df = hourly.assign(date=pd.to_datetime(hourly["date"]))
    key = df["date"].dt.normalize() if level == "day" else df["date"].dt.strftime("%Y-%m")
    name = "date" if level == "day" else "month"
    g = df.assign(**{name: key}).groupby(["route", name], as_index=False)[col].sum()
    net = g.groupby(name, as_index=False)[col].sum().assign(route="all")
    out = pd.concat([g.astype({"route": str}), net[["route", name, col]]], ignore_index=True)
    out[col] = np.rint(out[col]).astype(int)
    return out


def with_band(df: pd.DataFrame, u: float, col: str = "pred") -> pd.DataFrame:
    """Интервал ±u (доля) — ошибка месячного объёма маршрута на фолдах; для горизонта «год» это нижняя граница."""
    return df.assign(lo=np.rint(df[col] * (1 - u)).astype(int), hi=np.rint(df[col] * (1 + u)).astype(int))
