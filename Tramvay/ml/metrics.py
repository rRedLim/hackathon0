"""WAPE-score и разбивки ошибки."""
from __future__ import annotations

import numpy as np
import pandas as pd


def wape_score(y, yhat) -> float:
    y, yhat = np.asarray(y, float), np.asarray(yhat, float)
    return max(0.0, 1.0 - np.abs(y - yhat).sum() / y.sum())


def breakdown(df: pd.DataFrame, by: str, y: str = "y", pred: str = "pred") -> pd.DataFrame:
    """Доля абсолютной ошибки и WAPE-score по группам; err_share в сумме = 1 − score сети."""
    tot = df[y].sum()
    g = df.assign(ae=(df[y] - df[pred]).abs()).groupby(by)
    out = pd.DataFrame({"y": g[y].sum(), "pred": g[pred].sum(), "ae": g["ae"].sum()})
    out["score"] = 1 - out["ae"] / out["y"]
    out["err_share"] = out["ae"] / tot
    out["bias"] = out["pred"] / out["y"] - 1
    return out
