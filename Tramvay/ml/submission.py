"""Сабмит: сетка 14 640 строк в порядке test_submission.csv, проверка формата, запись, контрольная сумма."""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd

from .config import DATA_DIR, FC_END, FC_START, N_SUB_ROWS, ROUTES

KEYS = ["route", "date", "hour"]


def template(data_dir: Path = DATA_DIR) -> pd.DataFrame:
    f = data_dir / "test_submission.csv"
    if f.exists():
        t = pd.read_csv(f, sep=";", usecols=KEYS)
    else:
        idx = pd.MultiIndex.from_product([ROUTES, pd.date_range(FC_START, FC_END).strftime("%Y-%m-%d"), range(24)],
                                         names=KEYS)
        t = idx.to_frame(index=False)
    t["date"] = t["date"].astype(str)
    return t


def to_submission(pred: pd.DataFrame, data_dir: Path = DATA_DIR) -> pd.DataFrame:
    """pred: route, date (Timestamp|str), hour, pred (float) → сабмит с целыми неотрицательными прогнозами."""
    p = pred.copy()
    p["date"] = pd.to_datetime(p["date"]).dt.strftime("%Y-%m-%d")
    sub = template(data_dir).merge(p[KEYS + ["pred"]], on=KEYS, how="left")
    if sub["pred"].isna().any():
        miss = sub[sub["pred"].isna()].iloc[0]
        raise ValueError(f"нет прогноза для {miss['route']};{miss['date']};{miss['hour']}")
    sub["prediction"] = np.rint(sub.pop("pred").clip(lower=0)).astype(int)
    return sub


def validate(sub: pd.DataFrame | Path) -> list[str]:
    if not isinstance(sub, pd.DataFrame):
        sub = pd.read_csv(sub, sep=";")
    errs = []
    if list(sub.columns) != KEYS + ["prediction"]:
        errs.append(f"колонки {list(sub.columns)} вместо route;date;hour;prediction")
        return errs
    if len(sub) != N_SUB_ROWS:
        errs.append(f"{len(sub)} строк вместо {N_SUB_ROWS}")
    if sub.duplicated(KEYS).any():
        errs.append(f"дубликатов ключей: {int(sub.duplicated(KEYS).sum())}")
    want = template().assign(k=1)
    got = sub.assign(date=sub["date"].astype(str))
    if len(want.merge(got, on=KEYS)) != len(want):
        errs.append("сетка ключей не совпадает с test_submission.csv")
    if sub["prediction"].isna().any() or (sub["prediction"] < 0).any():
        errs.append("пустые или отрицательные прогнозы")
    return errs


def write(sub: pd.DataFrame, path: Path) -> str:
    errs = validate(sub)
    if errs:
        raise ValueError("; ".join(errs))
    path.parent.mkdir(parents=True, exist_ok=True)
    sub.to_csv(path, sep=";", index=False, lineterminator="\n", encoding="utf-8")
    return md5(path)


def md5(path: Path) -> str:
    return hashlib.md5(Path(path).read_bytes()).hexdigest()


def rows_md5(sub: pd.DataFrame, exclude_route: int | None = None) -> str:
    """Контрольная сумма строк без выбранного маршрута — доказывает, что база в серии одна и та же."""
    s = sub if exclude_route is None else sub[sub["route"] != exclude_route]
    return hashlib.md5(s.to_csv(sep=";", index=False).encode()).hexdigest()
