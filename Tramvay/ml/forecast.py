"""Прогноз ноября–декабря: обучение на всей истории, факт 1.11 00–01 ч из хвоста test.csv, маршрут 5 отдельно."""
from __future__ import annotations

import numpy as np
import pandas as pd

from pathlib import Path

from . import route5 as r5
from .calendar_ru import load_calendar
from .config import DATA_DIR, FC_END, FC_START, HIST_END, HIST_ROUTES, NEW_ROUTE, Params, Route5
from .data import load_grid, nov1_tail
from .regimes import apply_effects, load_effects, load_regimes
from .structural import StructuralModel
from .submission import to_submission
from .weather import daily_precip, load_weather, redistribute


def forecast_base(params: Params, grid, cal, weather, regs, tail: pd.DataFrame | None):
    """Непрерывный прогноз 9 маршрутов + маршрут 5 нулями. Возвращает (DataFrame, обученная модель)."""
    model = StructuralModel(params, cal, weather, regs).fit(grid, HIST_END)
    dates = pd.date_range(FC_START, FC_END)
    pred = model.predict(dates, HIST_ROUTES)
    if tail is not None and len(tail):
        # часы 0–1 первого ноября известны: хвост test.csv полный (последние валидации ~01:40, ночью пусто)
        nov1 = (pred["date"] == FC_START) & (pred["hour"] <= 1)
        fact = tail.assign(date=pd.to_datetime(tail["date"])).set_index(["route", "date", "hour"])["boardings"]
        keys = pd.MultiIndex.from_frame(pred.loc[nov1, ["route", "date", "hour"]])
        pred.loc[nov1, "pred"] = fact.reindex(keys).fillna(0).to_numpy(float)
    r5 = pd.DataFrame({"route": NEW_ROUTE, "date": np.repeat(dates, 24), "hour": np.tile(np.arange(24), len(dates)),
                       "pred": 0.0})
    return pd.concat([pred, r5], ignore_index=True), model


def build_final(data_dir: Path = DATA_DIR, offline: bool = False,
                params: Params | None = None) -> tuple[pd.DataFrame, dict]:
    """Итоговый сабмит целиком из кода: модель → события/калибровки → маршрут 5 → погода → сетка 14 640 строк.

    Все параметры — значения по умолчанию из ml/config.py и ml/regimes.json (результат лидерборда 0.91250).
    """
    p, cfg = params or Params(), Route5()
    grid = load_grid(data_dir)
    cal = load_calendar(offline)
    weather = load_weather(offline) if p.use_weather else None
    regs, effects = load_regimes(), load_effects()
    pred, model = forecast_base(p, grid, cal, weather, regs, nov1_tail(data_dir))
    # маршрут 5: доли часов аналога берём из прогноза 7-го до событий, затем события ко всем маршрутам
    f5 = r5.forecast(pred, cal, cfg.weekday_level, cfg, p)
    full = apply_effects(pd.concat([pred[pred["route"] != NEW_ROUTE], f5], ignore_index=True), effects)
    info = {"calendar": cal.attrs.get("source"), "level_W": model.f.W, "effects": [e["name"] for e in effects]}
    if p.weather_beta:
        precip = daily_precip(offline)
        if precip is None:
            raise RuntimeError("нет осадков Open-Meteo: нужен ml/artifacts/precip_moscow_2025_daily.csv или сеть")
        # маршрут 5 и факт 1.11 00–01 ч не трогаем
        d = pd.to_datetime(full["date"])
        mask = (full["route"] != NEW_ROUTE) & ~((d == FC_START) & (full["hour"] <= 1))
        full = redistribute(full, precip, p.weather_beta, mask)
        info["effects"].append(f"погода (осадки, β = {p.weather_beta})")
    return to_submission(full, data_dir), info
