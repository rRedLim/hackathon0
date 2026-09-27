"""Режимы маршрутов (ремонты, закрытия по выходным) из ml/regimes.json → состояние маршрута на каждый день."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .config import ML_DIR

NORMAL = "normal"


def load_regimes(path: Path = ML_DIR / "regimes.json", end_override: dict | None = None) -> list[dict]:
    """end_override: {маршрут: 'YYYY-MM-DD'} — сдвинуть конец режима выходных (гипотезы для лидерборда)."""
    regs = json.loads(Path(path).read_text(encoding="utf-8"))["regimes"]
    regs = copy.deepcopy(regs)
    for r in regs:
        if end_override and r["route"] in end_override and r["days"] == "offdays":
            r["end"] = end_override[r["route"]]
    return regs


def load_effects(path: Path = ML_DIR / "regimes.json") -> list[dict]:
    """Внешние события (effects) + поправки формы по лидерборду (calibration)."""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    return d.get("effects", []) + d.get("calibration", [])


def apply_effects(pred: pd.DataFrame, effects: list[dict], col: str = "pred") -> pd.DataFrame:
    """Внешние события как множители к ячейкам (route, date, hour); факт 1.11 00–01 ч не трогаем."""
    out = pred.copy()
    dates = pd.to_datetime(out["date"])
    tail = (dates == pd.Timestamp("2025-11-01")) & (out["hour"] <= 1)
    for e in effects:
        m = (dates >= pd.Timestamp(e["start"])) & (dates <= pd.Timestamp(e["end"])) & ~tail
        if e.get("routes"):
            m &= out["route"].isin(e["routes"])
        if e.get("hours"):
            m &= out["hour"].isin(e["hours"])
        if e.get("dow"):
            m &= dates.dt.dayofweek.isin(e["dow"])
        if e.get("exclude_dates"):
            m &= ~dates.isin(pd.to_datetime(e["exclude_dates"]))
        out.loc[m, col] = out.loc[m, col] * e["mult"]
    return out


def state_table(regs: list[dict], cal: pd.DataFrame, routes: list[int]) -> pd.DataFrame:
    """DataFrame [дата × маршрут] со строковым состоянием: normal / closed / short / exclude."""
    c = cal.set_index("date")
    st = pd.DataFrame(NORMAL, index=c.index, columns=routes, dtype=object)
    for r in regs:
        if r["route"] not in routes:
            continue
        span = (c.index >= pd.Timestamp(r["start"])) & (c.index <= pd.Timestamp(r["end"]))
        if r["days"] == "offdays":
            span &= c["is_off"].to_numpy()
        st.loc[span, r["route"]] = r["state"]
    return st


def default_mults(regs: list[dict]) -> dict:
    return {(r["route"], r["state"]): float(r.get("default_mult", 1.0)) for r in regs}


def describe(regs: list[dict]) -> list[str]:
    return [f"{r['route']}: {r['state']} ({r['days']}) {r['start']} … {r['end']}" for r in regs]


def is_normal(x) -> np.ndarray:
    return np.asarray(x) == NORMAL
