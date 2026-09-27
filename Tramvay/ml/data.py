"""Загрузка таргета: полная сетка маршрут × дата × час, дневные суммы, факт 1.11 из хвоста test.csv."""
from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
import pandas as pd

from .config import ART_DIR, DATA_DIR, FC_START, HIST_END, HIST_START, HIST_ROUTES


def load_grid(data_dir: Path = DATA_DIR) -> pd.DataFrame:
    """labels train+test → полная сетка 9 маршрутов × 304 дня × 24 часа, пропуски (ночь) = 0."""
    parts = []
    for split in ("train", "test"):
        f = data_dir / "labels" / f"labels_day_{split}.csv"
        if not f.exists():
            raise SystemExit(f"Не найден {f}")
        parts.append(pd.read_csv(f, sep=";", parse_dates=["date"]))
    lab = pd.concat(parts, ignore_index=True)
    lab = lab[(lab["date"] >= HIST_START) & (lab["date"] <= HIST_END)]
    lab = lab.groupby(["route", "date", "hour"], as_index=False)["boardings"].sum()
    idx = pd.MultiIndex.from_product([HIST_ROUTES, pd.date_range(HIST_START, HIST_END), range(24)],
                                     names=["route", "date", "hour"])
    g = lab.set_index(["route", "date", "hour"])["boardings"].reindex(idx, fill_value=0).reset_index()
    g["boardings"] = g["boardings"].astype(float)
    return g


def to_cube(g: pd.DataFrame) -> tuple[np.ndarray, list[int], pd.DatetimeIndex]:
    """Сетка → массив [маршрут, день, час]."""
    routes = sorted(g["route"].unique())
    dates = pd.DatetimeIndex(sorted(g["date"].unique()))
    cube = g.set_index(["route", "date", "hour"])["boardings"].unstack("hour").reindex(
        pd.MultiIndex.from_product([routes, dates])).to_numpy().reshape(len(routes), len(dates), 24)
    return np.nan_to_num(cube), routes, dates


def nov1_tail(data_dir: Path = DATA_DIR) -> pd.DataFrame:
    """Успешные посадки 1.11 из «хвоста» test.csv (часы 0–1) — это факт периода прогноза.

    Берём из своего кэша, иначе из агрегата сырых валидаций (py -m ml ingest), иначе один проход по test.csv.
    """
    own = ART_DIR / "nov1_tail.csv"
    raw = ART_DIR / "raw_route_hour.parquet"
    if own.exists():
        return pd.read_csv(own, parse_dates=["date"])
    if raw.exists():
        h = pd.read_parquet(raw)
        h["date"] = pd.to_datetime(h["date"])
        t = h[h["date"] >= FC_START][["route", "date", "hour", "boardings"]]
    else:
        t = scan_test_tail(data_dir / "test.csv")
        if t is None:
            return pd.DataFrame(columns=["route", "date", "hour", "boardings"])
    t = t.groupby(["route", "date", "hour"], as_index=False)["boardings"].sum()
    ART_DIR.mkdir(parents=True, exist_ok=True)
    t.to_csv(own, index=False)
    return t


def scan_test_tail(f: Path) -> pd.DataFrame | None:
    """Один потоковый проход по test.csv: успешные валидации с датой ноября (хвост 1.11)."""
    if not f.exists():
        return None
    rows: dict[tuple, int] = {}
    with open(f, encoding="utf-8", newline="") as fh:
        rd = csv.reader(fh, delimiter=";")
        head = next(rd)
        i_t, i_v, i_r = head.index("tran_date_time"), head.index("validation_result"), head.index("ngpt_route")
        for row in rd:
            ts = row[i_t]
            if ts.startswith("2025-11") and row[i_v] == "1":
                key = (int(row[i_r].split()[0]), ts[:10], int(ts[11:13]))
                rows[key] = rows.get(key, 0) + 1
    t = pd.DataFrame([(*k, v) for k, v in rows.items()], columns=["route", "date", "hour", "boardings"])
    t["date"] = pd.to_datetime(t["date"])
    return t
