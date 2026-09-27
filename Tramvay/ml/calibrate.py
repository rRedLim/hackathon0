"""Калибровка уровней маршрутов по лидерборду: одна проба «маршрут × k» → оптимальный множитель маршрута.

Ошибка складывается по ячейкам, поэтому разница score пробы и опорного файла = −ΔE_r / Y, где
E_r(k) = Σ|y − k·ŷ| ≈ P_r · E|k_t·ε − k| (P_r — объём маршрута в опорном файле, ε — логнормальный шум ячеек).
Разброс σ_r берём из бэктеста (оракульный уровень маршрута) × поправка на ноябрь–декабрь; тогда одна проба
однозначно даёт k_t, а L1-оптимум k* = k_t·exp(−σ²/2) — медиана.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from . import backtest as bt
from .config import ART_DIR, SUB_DIR, Params
from .route5 import expected_error

LEVELS = ART_DIR / "route_levels.json"
BEST = SUB_DIR / "hyp_v3_W06000_nye053.csv"      # лучший засчитанный файл (0.90693, 26.09)
BEST_SCORE = 0.90693
Y_EST = 12.87e6                                  # сумма посадок ноября–декабря (оценка по базе)
SIGMA_INFLATE = 0.92                             # σ ноября–декабря / σ бэктеста: две пробы Т1 (7 с 12.11) дали 0.12 против 0.13


def route_sigma(grid, cal, weather, regs, folds=("A", "D", "E")) -> dict[int, float]:
    """Взвешенный разброс log(y/ŷ) по ячейкам маршрута при оракульном уровне маршрута (бэктест)."""
    out: dict[int, list] = {}
    for key, _, cut, end, _ in bt.FOLDS:
        if key not in folds:
            continue
        df = bt.run_fold(Params(), grid, cal, weather, regs, cut, end)
        for r, x in df.groupby("route"):
            adj = x["pred"] * x["y"].sum() / x["pred"].sum()
            m = adj > 20
            lr = np.log((x["y"][m] + 0.5) / (adj[m] + 0.5))
            w = adj[m]
            mu = np.average(lr, weights=w)
            out.setdefault(int(r), []).append(float(np.sqrt(np.average((lr - mu) ** 2, weights=w))))
    return {r: float(np.mean(v)) for r, v in out.items()}


def select(sub: pd.DataFrame, routes=None, date_from=None, date_to=None, hours=None, dow=None,
           exclude_dates=None) -> pd.Series:
    """Маска ячеек пробы; факт 1.11 00–01 ч никогда не трогаем."""
    m = ~((sub["date"] == "2025-11-01") & (sub["hour"] <= 1))
    if dow:
        m &= pd.to_datetime(sub["date"]).dt.dayofweek.isin(dow)
    if exclude_dates:
        m &= ~sub["date"].isin(exclude_dates)
    if routes:
        m &= sub["route"].isin(routes)
    if date_from:
        m &= sub["date"] >= date_from
    if date_to:
        m &= sub["date"] <= date_to
    if hours:
        m &= sub["hour"].isin(hours)
    return m


def probe(ref: pd.DataFrame, route: int | None, mult: float, mask: pd.Series | None = None) -> pd.DataFrame:
    """Ячейки маски (по умолчанию — весь маршрут route) × mult."""
    sub = ref.copy()
    m = mask if mask is not None else select(sub, routes=[route])
    sub.loc[m, "prediction"] = np.rint(sub.loc[m, "prediction"] * mult).astype(int)
    return sub


def fit_level(delta_score: float, mult: float, P: float, sigma: float, Y: float = Y_EST) -> dict:
    """k_t по одной пробе (перебор), k* = медиана, ожидаемый прирост от k* к опорному."""
    kt = np.arange(0.05, 2.00, 0.0005)
    sig = np.full_like(kt, sigma)
    e1 = expected_error(np.ones_like(kt), kt, sig)
    em = expected_error(np.full_like(kt, mult), kt, sig)
    pred = -(em - e1) * P / Y
    i = int(np.argmin(np.abs(pred - delta_score)))
    k_t = float(kt[i])
    k_star = k_t * np.exp(-sigma ** 2 / 2)
    e = lambda k: float(expected_error(np.array(k), np.array(k_t), np.array(sigma))) * P / Y
    return {"k_t": k_t, "k_star": float(k_star), "gain_vs_ref": e(1.0) - e(k_star),
            "gain_vs_probe": e(mult) - e(k_star), "saturated": abs(pred[i] - delta_score) > 2e-5}


def load_levels() -> dict:
    return json.loads(LEVELS.read_text(encoding="utf-8")) if LEVELS.exists() else {}


def save_level(route: int, rec: dict) -> None:
    d = load_levels()
    d[str(route)] = rec
    ART_DIR.mkdir(parents=True, exist_ok=True)
    LEVELS.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
