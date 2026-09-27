"""Где усиливать выпуск: часы, в которых прогноз посадок на вагон выше исторической нормы маршрута.

Посадки на вагон в час — не наполненность салона: пассажиры выходят, а наполненность зависит от дальности
поездки. Поэтому процентов загрузки и вместимости вагона здесь нет, только сравнение с собственной историей маршрута:
  • вагон-час — вагон (garage_number) хотя бы с одной успешной валидацией в этом часу (сырые валидации, ml/ingest.py);
  • норма маршрута — 90-й перцентиль посадок на вагон-час в обычном режиме, январь–октябрь 2025;
  • выпуск на прогнозный день — медиана вагонов в этот час за последние 4 недели обычного режима того же
    типа дня (будни / Сб / Вс и праздники);
  • кандидат на усиление — час, где прогноз посадок / выпуск > нормы; сколько вагонов добавить, чтобы вернуться
    к норме: ceil(прогноз / норма) − выпуск (extra — верхняя оценка);
  • реалистичная добавка (extra_feasible) не больше, чем маршрут уже выпускал одновременно в истории
    (максимум вагонов в час в обычном режиме): парк маршрута конечен, остальное — только перераспределение.

Резерв выпуска (reserve) — обратная задача, экономия вагоно-часов: час, где прогноз посадок на вагон ниже
половины нормы. Сколько вагонов оставить: need = max(ceil(прогноз / (0.7 · норма)), 2) — нагрузка остаётся
не выше 70 % нормы; снять можно не больше 30 % планового выпуска часа, чтобы интервал движения не вырос
критично. Не оцениваются: дни, когда маршрут не в обычном режиме (закрыт, укорочен), и ячейки с событиями,
снижающими число валидаций сильнее чем вдвое (бесплатный проезд 31.12: пассажиры едут, а валидаций нет), и
часы 0–6 и 23: выход вагонов на линию и сход в депо — вагон работает неполный час.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import NEW_ROUTE
from .ingest import load_raw
from .regimes import NORMAL, state_table

NORM_Q = 0.90
PLAN_DAYS = {"wd": 20, "sat": 4, "sun": 4}           # последние 4 недели каждого типа дня
RESERVE_BELOW = 0.5      # кандидат на резерв: посадки на вагон ниже этой доли нормы
RESERVE_TARGET = 0.7     # после снятия вагонов нагрузка не выше этой доли нормы
RESERVE_MAX_SHARE = 0.3  # снимаем не больше этой доли планового выпуска часа
RESERVE_MIN_VEH = 2      # минимальный выпуск в час
RESERVE_HOURS = range(7, 23)  # 7:00–22:59: в 0–1 и 5–6 ч вагоны выходят на линию и сходят в депо, час неполный


def plan_class(cal: pd.DataFrame) -> pd.Series:
    """Тип дня для выпуска: wd (Пн–Пт, в т. ч. рабочая суббота) / sat / sun (Вс, праздники, 31.12)."""
    c = cal.set_index("date")
    cls = np.where(c["is_off"] & (c["dow"] == 5), "sat", np.where(c["is_off"], "sun", "wd"))
    return pd.Series(cls, index=c.index)


def _prepare(forecast: pd.DataFrame, grid: pd.DataFrame, cal: pd.DataFrame, regs: list[dict]):
    """Прогноз с плановым выпуском по часам (n_plan), норма и максимум вагонов маршрута, состояние маршрута."""
    f = forecast.rename(columns={"prediction": "pred"}).assign(date=lambda d: pd.to_datetime(d["date"]))
    f = f[f["route"] != NEW_ROUTE]
    routes = sorted(f["route"].unique())
    v = load_raw()[["route", "date", "hour", "n_veh"]].merge(grid, on=["route", "date", "hour"], how="left")
    st = state_table(regs, cal, routes).stack().rename("state").rename_axis(["date", "route"]).reset_index()
    creg = cal.set_index("date")["regular"]
    v = v.merge(st, on=["date", "route"], how="left")
    v = v[(v["state"] == NORMAL) & v["date"].map(creg).fillna(False).astype(bool) & (v["n_veh"] > 0)]
    v["bpv"] = v["boardings"] / v["n_veh"]
    norm = v.groupby("route")["bpv"].quantile(NORM_Q)
    fleet_max = v.groupby("route")["n_veh"].max()

    pc = plan_class(cal)
    v["cls"] = v["date"].map(pc)
    plan = []
    for (r, cls), g in v.groupby(["route", "cls"]):
        last = np.sort(g["date"].unique())[-PLAN_DAYS[cls]:]
        plan.append(g[g["date"].isin(last)].groupby("hour")["n_veh"].median().rename("n_plan")
                    .reset_index().assign(route=r, cls=cls))
    plan = pd.concat(plan, ignore_index=True)

    f["cls"] = f["date"].map(pc)
    f = f.merge(plan, on=["route", "cls", "hour"], how="left")
    f = f[f["n_plan"] > 0]
    f["norm"] = f["route"].map(norm)
    f["bpv"] = f["pred"] / f["n_plan"]
    return f, norm, fleet_max, st


def reinforcement(forecast: pd.DataFrame, grid: pd.DataFrame, cal: pd.DataFrame, regs: list[dict]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """forecast: route, date, hour, pred (или prediction). Возвращает (часы-кандидаты, сводку по маршрутам)."""
    f, norm, fleet_max, _ = _prepare(forecast, grid, cal, regs)
    cand = f[f["bpv"] > f["norm"]].copy()
    cand["extra"] = (np.ceil(cand["pred"] / cand["norm"]) - cand["n_plan"]).clip(lower=1).astype(int)
    room = (cand["route"].map(fleet_max) - np.ceil(cand["n_plan"])).clip(lower=0)
    cand["extra_feasible"] = np.minimum(cand["extra"], room).astype(int)
    cand = cand.assign(n_plan=cand["n_plan"].round(1), norm=cand["norm"].round(1), bpv=cand["bpv"].round(1),
                       pred=np.rint(cand["pred"]).astype(int), date=cand["date"].dt.strftime("%Y-%m-%d"))
    cand = cand[["route", "date", "hour", "pred", "n_plan", "bpv", "norm", "extra", "extra_feasible"]] \
        .sort_values(["route", "date", "hour"])

    hours = cand.groupby("route")["hour"].agg(lambda h: ", ".join(f"{x}:00" for x in h.value_counts().index[:3]))
    summary = pd.DataFrame({
        "norm_bpv": norm.round(1),
        "fleet_max": fleet_max,
        "service_hours": f.groupby("route").size(),
        "cand_hours": cand.groupby("route").size(),
        "extra_vehicle_hours": cand.groupby("route")["extra"].sum(),
        "feasible_vehicle_hours": cand.groupby("route")["extra_feasible"].sum(),
        "top_hours": hours,
    }).fillna({"cand_hours": 0, "extra_vehicle_hours": 0, "feasible_vehicle_hours": 0, "top_hours": "—"})
    summary["cand_share"] = (summary["cand_hours"] / summary["service_hours"]).round(3)
    summary = summary.astype({"cand_hours": int, "extra_vehicle_hours": int, "feasible_vehicle_hours": int}).rename_axis("route").reset_index()
    return cand, summary


def reserve(forecast: pd.DataFrame, grid: pd.DataFrame, cal: pd.DataFrame, regs: list[dict],
            effects: list[dict]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Часы, где выпуск можно сократить (экономия вагоно-часов), и сводка по маршрутам."""
    f, norm, _, st = _prepare(forecast, grid, cal, regs)
    f = f.merge(st, on=["date", "route"], how="left")
    f = f[(f["state"] == NORMAL) & f["hour"].isin(RESERVE_HOURS)]
    for e in effects:  # события, при которых валидаций сильно меньше, чем пассажиров (бесплатный проезд)
        if e["mult"] >= 0.5:
            continue
        m = (f["date"] >= pd.Timestamp(e["start"])) & (f["date"] <= pd.Timestamp(e["end"]))
        if e.get("routes"):
            m &= f["route"].isin(e["routes"])
        if e.get("hours"):
            m &= f["hour"].isin(e["hours"])
        f = f[~m]
    need = np.maximum(np.ceil(f["pred"] / (RESERVE_TARGET * f["norm"])), RESERVE_MIN_VEH)
    cap = np.floor(RESERVE_MAX_SHARE * f["n_plan"])
    f = f.assign(need=need, reserve=np.minimum(np.floor(f["n_plan"] - need), cap))
    res = f[(f["bpv"] < RESERVE_BELOW * f["norm"]) & (f["reserve"] >= 1)].copy()
    res = res.assign(n_plan=res["n_plan"].round(1), norm=res["norm"].round(1), bpv=res["bpv"].round(1),
                     pred=np.rint(res["pred"]).astype(int), need=res["need"].astype(int),
                     reserve=res["reserve"].astype(int), date=res["date"].dt.strftime("%Y-%m-%d"))
    res = res[["route", "date", "hour", "pred", "n_plan", "bpv", "norm", "need", "reserve"]] \
        .sort_values(["route", "date", "hour"])
    hours = res.groupby("route")["hour"].agg(lambda h: ", ".join(f"{x}:00" for x in h.value_counts().index[:3]))
    summary = pd.DataFrame({
        "reserve_hours": res.groupby("route").size(),
        "reserve_vehicle_hours": res.groupby("route")["reserve"].sum(),
        "reserve_top_hours": hours,
        "planned_vehicle_hours": f.groupby("route")["n_plan"].sum().round(0),
    }).fillna({"reserve_hours": 0, "reserve_vehicle_hours": 0, "reserve_top_hours": "—"})
    summary = summary.astype({"reserve_hours": int, "reserve_vehicle_hours": int, "planned_vehicle_hours": int})
    summary["reserve_share"] = (summary["reserve_vehicle_hours"] / summary["planned_vehicle_hours"]).round(3)
    return res, summary.rename_axis("route").reset_index()
