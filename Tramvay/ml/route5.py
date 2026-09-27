"""Маршрут 5: холодный старт от аналога и подбор уровня W по лидерборду.

Правило (фиксировано для серии сабмитов):
  • до 15.12 — нули; 16.12 — 0.6 × W (неполный первый день);
  • с 17.12 — будни W, Сб 0.61·W, Вс 0.51·W, суточный профиль маршрута 7 для того же дня;
  • 29–31.12 — те же календарные множители, что у всех маршрутов (29.12 ×0.90, 30.12 ×0.85, 31.12 = Сб × 0.85).

Разница score двух сабмитов, отличающихся только маршрутом 5, равна разнице Σ|y5 − ŷ5| / Y и от базы не зависит.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import FC_END, FC_START, NEW_ROUTE, Params, Route5


def daily_factors(cal: pd.DataFrame, cfg: Route5, params: Params) -> pd.Series:
    """Множитель к W на каждый день ноября–декабря."""
    c = cal.set_index("date").loc[FC_START:FC_END]
    launch = pd.Timestamp(cfg.launch)
    out = pd.Series(0.0, index=c.index)
    for d, row in c.iterrows():
        if d < launch:
            continue
        if d == launch:
            k = cfg.launch_day_mult
        elif row["special"] == "nye":
            k = cfg.sat_vs_wd * params.nye_vs_sat
        elif row["lcls"] == "hol":
            k = cfg.sun_vs_wd * params.hol_vs_sun
        elif row["lcls"] == "sat":
            k = cfg.sat_vs_wd
        elif row["lcls"] == "sun":
            k = cfg.sun_vs_wd
        else:
            k = 1.0
            if row["special"] == "pre_ny":
                k *= params.pre_ny.get(d.strftime("%Y-%m-%d"), 1.0)
        out[d] = k
    return out


def forecast(base: pd.DataFrame, cal: pd.DataFrame, W: float, cfg: Route5 = Route5(),
             params: Params = Params()) -> pd.DataFrame:
    """base — непрерывный (float) прогноз базы с колонками route, date, hour, pred; из него берутся доли часов аналога."""
    a = base[base["route"] == cfg.analog].copy()
    tot = a.groupby("date")["pred"].transform("sum")
    a["share"] = np.where(tot > 0, a["pred"] / tot, 0.0)
    f = daily_factors(cal, cfg, params)
    a["pred"] = W * a["date"].map(f).to_numpy() * a["share"].to_numpy()
    a["route"] = NEW_ROUTE
    return a[["route", "date", "hour", "pred"]]


def units(cal: pd.DataFrame, cfg: Route5 = Route5(), params: Params = Params()) -> float:
    """P = Σ множителей: объём маршрута 5 за период = P · W."""
    return float(daily_factors(cal, cfg, params).sum())


def expected_error(W: np.ndarray, Wt: np.ndarray, sigma: np.ndarray, n: int = 400) -> np.ndarray:
    """E|W_t·ε − W| на единицу P, ε ~ логнормальное со средним 1 и разбросом σ (квантильная сетка)."""
    from scipy.stats import norm
    z = norm.ppf((np.arange(n) + 0.5) / n)
    eps = np.exp(sigma[..., None] * z - sigma[..., None] ** 2 / 2)
    return np.abs(Wt[..., None] * eps - np.asarray(W)[..., None]).mean(axis=-1)


def next_w(scores: dict[float, float], P: float, Y: float, step: float = 500.0) -> dict:
    """Где искать оптимум W по трём (и более) сабмитам одной серии.

    Ошибка маршрута 5: E(W) = Σ|y − W·p| ≈ P · E|W_t·ε − W|, где W_t — истинный будний уровень, ε — отклонение
    ячейки (логнормальное, разброс σ). Разности score между сабмитами = −ΔE / Y. По ним подбираем (W_t, σ)
    перебором; L1-оптимум W* = медиана = W_t·exp(−σ²/2). Y — сумма посадок всех маршрутов (оценка по базе).
    """
    ws = np.array(sorted(scores), float)
    s = np.array([scores[w] for w in ws])
    obs = s[1:] - s[0]                                   # Δscore относительно первого
    Wt = np.arange(1_000, 40_001, 100.0)
    sig = np.linspace(0.05, 1.5, 59)
    WT, SG = np.meshgrid(Wt, sig, indexing="ij")
    E = np.stack([expected_error(np.full_like(WT, w), WT, SG) for w in ws], axis=-1) * P / Y
    pred = -(E[..., 1:] - E[..., :1])
    loss = ((pred - obs) ** 2).sum(axis=-1)
    i, j = np.unravel_index(np.argmin(loss), loss.shape)
    w_t, sigma = float(WT[i, j]), float(SG[i, j])
    w_star = w_t * np.exp(-sigma ** 2 / 2)
    rmse = float(np.sqrt(loss[i, j] / len(obs)))
    order = np.argsort(-s)
    best, second = ws[order[0]], ws[order[1]]
    if w_star > ws[-1] + 0.25 * (ws[-1] - ws[-2]):
        where = "выше сетки (дальше самого большого W)"
        w4 = min(w_star, ws[-1] + (ws[-1] - ws[-2]))
    elif w_star < ws[0] - 0.25 * (ws[1] - ws[0]):
        where = "ниже сетки (меньше самого маленького W)"
        w4 = max(w_star, ws[0] - (ws[1] - ws[0]) / 2, 2_000)
    else:
        where = "внутри сетки, между двумя лучшими" if min(best, second) <= w_star <= max(best, second)             else "рядом с лучшим W"
        w4 = w_star
    w4 = float(np.round(w4 / step) * step)
    # ожидаемый прирост 4-го сабмита к лучшему из уже отправленных (по подобранной модели)
    e = lambda w: float(expected_error(np.array(w), np.array(w_t), np.array(sigma))) * P / Y
    gain = e(best) - e(w4)
    needed = w4 not in scores and gain >= 5e-5
    secants = [{"from": a, "to": b, "d_score": sb - sa, "max_d_score": (b - a) * P / Y}
               for a, b, sa, sb in zip(ws, ws[1:], s, s[1:])]
    return {"best": float(best), "second": float(second), "secants": secants, "w_true": w_t, "sigma": sigma,
            "w_star": float(w_star), "fit_rmse": rmse, "optimum_side": where, "w4": w4, "gain": gain,
            "needed": needed}
