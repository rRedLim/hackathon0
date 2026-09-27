"""Временные фолды «обучение до отсечки → прогноз вперёд» и подбор параметров структурной модели.

Все статистики считаются строго по данным до отсечки. Режимы маршрутов из regimes.json считаются известными
(как и для ноября–декабря: они берутся из новостей, а не из данных).
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd

from .config import Params
from .metrics import breakdown, wape_score
from .structural import StructuralModel

# (ключ, описание, последний день истории, последний день прогноза, участвует в выборе параметров)
FOLDS = [
    ("A", "≤31.01 → фев–мар (зима)", "2025-01-31", "2025-03-31", True),
    ("B", "≤31.03 → апр–май (майские)", "2025-03-31", "2025-05-31", True),
    ("C", "≤31.08 → сен–окт (смена сезона и режимов)", "2025-08-31", "2025-10-31", False),
    ("D", "≤30.09 → окт", "2025-09-30", "2025-10-31", True),
    ("E", "≤12.10 → 13–31.10", "2025-10-12", "2025-10-31", True),
]


def run_fold(params: Params, grid, cal, weather, regs, cutoff: str, end: str,
             model_cls=StructuralModel) -> pd.DataFrame:
    cut, end = pd.Timestamp(cutoff), pd.Timestamp(end)
    # множители маршрутов/месяцев откалиброваны по лидерборду под ноябрь–декабрь — в бэктест не переносим
    params = replace(params, route_mult={}, level_month={})
    m = model_cls(params, cal, weather, regs).fit(grid, cut)
    dates = pd.date_range(cut + pd.Timedelta(days=1), end)
    pred = m.predict(dates)
    fact = grid[(grid["date"] > cut) & (grid["date"] <= end)].rename(columns={"boardings": "y"})
    return fact.merge(pred, on=["route", "date", "hour"], how="left")


def evaluate(params: Params, grid, cal, weather, regs, folds=FOLDS, detail: bool = False):
    rows, details = [], {}
    for key, name, cut, end, _ in folds:
        df = run_fold(params, grid, cal, weather, regs, cut, end)
        rows.append({"fold": key, "name": name, "score": wape_score(df["y"], df["pred"]),
                     "bias": df["pred"].sum() / df["y"].sum() - 1})
        if detail:
            details[key] = df
    res = pd.DataFrame(rows).set_index("fold")
    return (res, details) if detail else res


def selection_score(res: pd.DataFrame, folds=FOLDS) -> float:
    use = [k for k, *_, sel in folds if sel]
    return float(res.loc[use, "score"].mean())


SEARCH_SPACE = {
    "level_weeks": [2, 3, 4, 5, 6],
    "level_estimator": ["mean", "median", "trim"],
    "ratio_weeks": [4, 6, 8, 12],
    "profile_weeks": [2, 3, 4, 6, 8],
    "profile_estimator": ["sum", "median"],
    "use_weather": [True, False],
    "precip_transform": ["linear", "log1p", "cap5", "cap10"],
    "hol_profile": ["sun", "satsun"],
    "hol_vs_sun": [0.85, 0.9, 0.95, 1.0, 1.05],
    "anomaly_low": [0.6, 0.7, 0.8],
    "weekday_shrink": [0.0, 0.5, 1.0],
}


def search(base: Params, grid, cal, weather, regs, rounds: int = 2, log=print) -> tuple[Params, pd.DataFrame]:
    """Покоординатный поиск: по очереди меняем один параметр, берём лучшее по среднему фолдов A, B, D, E."""
    best = base
    best_s = selection_score(evaluate(best, grid, cal, weather, regs))
    log(f"  старт: {best_s:.5f}")
    hist = []
    for rnd in range(rounds):
        changed = False
        for name, values in SEARCH_SPACE.items():
            for v in values:
                if getattr(best, name) == v:
                    continue
                cand = replace(best, **{name: v})
                s = selection_score(evaluate(cand, grid, cal, weather, regs))
                hist.append({"round": rnd, "param": name, "value": v, "score": s})
                if s > best_s + 1e-5:
                    best, best_s, changed = cand, s, True
                    log(f"  {name} = {v}: {s:.5f}  ↑")
        if not changed:
            break
    return best, pd.DataFrame(hist)


def report(params: Params, grid, cal, weather, regs, log=print) -> pd.DataFrame:
    res, det = evaluate(params, grid, cal, weather, regs, detail=True)
    for k, row in res.iterrows():
        log(f"  {k} {row['name']:<42} WAPE-score {row['score']:.4f}  смещение {row['bias']:+.1%}")
    log(f"  среднее по фолдам выбора (A, B, D, E): {selection_score(res):.4f}")
    all_df = pd.concat([d.assign(fold=k) for k, d in det.items()])
    br = breakdown(all_df[all_df["fold"].isin(["A", "D", "E"])], "route")
    log("  по маршрутам (A+D+E): " + ", ".join(f"{r}: {s:.3f}" for r, s in br["score"].items()))
    return all_df


# горизонт = дней после отсечки
HORIZON_BUCKETS = [(1, 7, "1–7 дней"), (8, 14, "8–14 дней"), (15, 30, "15–30 дней"), (31, 61, "31–61 день")]


def _score(df: pd.DataFrame) -> float:
    return wape_score(df["y"], df["pred"])


def error_profile(params: Params, grid, cal, weather, regs, folds=FOLDS) -> dict[str, pd.DataFrame]:
    """Область определения в цифрах: ошибка по горизонту, по маршрутам и по уровню агрегации (все 5 фолдов).

    Уровни: час (как метрика лидерборда), сутки маршрута, месяц маршрута (|прогноз / факт − 1|).
    """
    frames = []
    for key, name, cut, end, _ in folds:
        df = run_fold(params, grid, cal, weather, regs, cut, end)
        frames.append(df.assign(fold=key, h=(df["date"] - pd.Timestamp(cut)).dt.days))
    df = pd.concat(frames, ignore_index=True)
    day = df.groupby(["fold", "route", "date"], as_index=False).agg(y=("y", "sum"), pred=("pred", "sum"), h=("h", "first"))

    rows = []
    for lo, hi, label in HORIZON_BUCKETS:
        m, md = df["h"].between(lo, hi), day["h"].between(lo, hi)
        noc = m & (df["fold"] != "C")    # C: 1.09 — начало учебного года и ремонта 7/50, смена режима
        rows.append({"horizon": label, "folds": ",".join(sorted(df.loc[m, "fold"].unique())),
                     "hourly_score": round(_score(df[m]), 4), "hourly_score_no_C": round(_score(df[noc]), 4),
                     "daily_score": round(_score(day[md]), 4),
                     "bias": round(df.loc[m, "pred"].sum() / df.loc[m, "y"].sum() - 1, 4)})
    by_h = pd.DataFrame(rows)

    by_route = pd.DataFrame([{"route": r, "hourly_score": round(_score(g), 4),
                              "daily_score": round(_score(day[day["route"] == r]), 4),
                              "bias": round(g["pred"].sum() / g["y"].sum() - 1, 4)}
                             for r, g in df.groupby("route")])

    # месяц маршрута: только полные месяцы внутри фолда (E — 19 дней — не в счёт)
    mon = df.assign(month=df["date"].dt.to_period("M")).groupby(["fold", "route", "month"]) \
            .agg(y=("y", "sum"), pred=("pred", "sum"), days=("date", "nunique")).reset_index()
    mon = mon[mon["days"] >= 28]
    err = (mon["pred"] / mon["y"] - 1).abs()
    net = mon.groupby(["fold", "month"])[["y", "pred"]].sum()
    nerr = (net["pred"] / net["y"] - 1).abs()
    levels = pd.DataFrame([
        {"level": "час маршрута (метрика)", "score": round(_score(df), 4), "n": len(df)},
        {"level": "сутки маршрута", "score": round(_score(day), 4), "n": len(day)},
        {"level": "месяц маршрута", "score": round(1 - (mon["pred"] - mon["y"]).abs().sum() / mon["y"].sum(), 4),
         "n": len(mon), "median_abs_err": round(float(err.median()), 4), "p80_abs_err": round(float(err.quantile(0.8)), 4)},
        {"level": "месяц сети", "score": round(1 - (net["pred"] - net["y"]).abs().sum() / net["y"].sum(), 4),
         "n": len(net), "median_abs_err": round(float(nerr.median()), 4), "p80_abs_err": round(float(nerr.quantile(0.8)), 4)},
    ])
    return {"by_horizon": by_h, "by_route": by_route, "by_level": levels}
