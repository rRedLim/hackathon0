"""Структурная модель: уровень будня × отношение типа дня × суточный профиль × календарь × погода × режим.

    ŷ(r, d, h) = W(r) · R(r, класс(d), режим(r, d)) · K_кал(d) · f_погода(d) · S(r, класс_профиля(d), режим, h)

W — уровень Вт–Чт за последние недели (очищенный от погоды и аномалий), R — отношение типа дня к будню
той же недели, S — доли часов в сутках. Всё оценивается только по данным до отсечки (direct-прогноз без лагов).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import weather as wx
from .config import Params
from .regimes import NORMAL, default_mults, state_table

STABLE_ROUTES = [1, 11, 12, 17, 25, 26, 28]      # без режимов в истории — для оценки погодных β


def same_type_median(values: np.ndarray, types: np.ndarray, usable: np.ndarray, window: int = 14) -> np.ndarray:
    """Медиана того же типа дня в окне ±window дней, без самого дня и неиспользуемых дней."""
    n = len(values)
    out = np.full(n, np.nan)
    for i in range(n):
        lo, hi = max(0, i - window), min(n, i + window + 1)
        m = (types[lo:hi] == types[i]) & usable[lo:hi]
        m[i - lo] = False
        if m.sum() >= 2:
            out[i] = np.median(values[lo:hi][m])
    return out


@dataclass
class Fitted:
    cutoff: pd.Timestamp
    W: dict = field(default_factory=dict)            # route → уровень Вт–Чт (без погоды)
    R: dict = field(default_factory=dict)            # (route, cls, state) → (recent, winter)
    prof: dict = field(default_factory=dict)         # (route, pcls, state) → (recent[24], winter[24])
    beta: dict = field(default_factory=dict)
    tstat: dict = field(default_factory=dict)
    anomalies: dict = field(default_factory=dict)    # route → список аномальных дат


class StructuralModel:
    def __init__(self, params: Params, cal: pd.DataFrame, weather: pd.DataFrame | None, regimes: list[dict]):
        self.p = params
        self.cal = cal.set_index("date")
        self.weather = wx.transform(weather, params.precip_transform) if params.use_weather else None
        self.regs = regimes
        self.defaults = default_mults(regimes)

    # ───────────────────────────── обучение ─────────────────────────────
    def fit(self, grid: pd.DataFrame, cutoff: pd.Timestamp) -> "StructuralModel":
        p = self.p
        g = grid[grid["date"] <= cutoff]
        routes = sorted(g["route"].unique())
        dates = pd.DatetimeIndex(sorted(g["date"].unique()))
        cube = g.pivot_table(index=["route", "date"], columns="hour", values="boardings", aggfunc="sum") \
                .reindex(pd.MultiIndex.from_product([routes, dates])).fillna(0.0).to_numpy() \
                .reshape(len(routes), len(dates), 24)
        D = cube.sum(axis=2)
        c = self.cal.reindex(dates)
        st = state_table(self.regs, self.cal.reset_index(), routes).reindex(dates)
        lcls, pcls = c["lcls"].to_numpy(), c["pcls"].to_numpy()
        regular = c["regular"].to_numpy()
        self.routes, self.cutoff = routes, cutoff
        f = Fitted(cutoff=cutoff)

        # 1) погодные β по стабильным маршрутам (сеть без режимов)
        stable = [i for i, r in enumerate(routes) if r in STABLE_ROUTES]
        net = D[stable].sum(axis=0)
        exp_net = same_type_median(net, lcls, regular)
        if self.weather is not None:
            f.beta, f.tstat = wx.fit_betas(pd.Series(net, dates), pd.Series(exp_net, dates),
                                           pd.Series(regular, dates), self.weather, p.beta_default)
        wf = wx.factor(self.weather, dates, f.beta)
        Dn = D / wf

        # 2) аномальные дни маршрута (только в обычном режиме)
        good = np.zeros_like(D, dtype=bool)
        for i, r in enumerate(routes):
            s = st[r].to_numpy()
            normal = s == NORMAL
            usable = regular & normal & (D[i] > 0)
            e = same_type_median(Dn[i], lcls, usable)
            ratio = Dn[i] / e
            anom = normal & np.isfinite(ratio) & ((ratio < p.anomaly_low) | (ratio > p.anomaly_high))
            f.anomalies[r] = [d.strftime("%Y-%m-%d") for d in dates[anom & regular]]
            # в особых режимах берём все дни режима, выбросы гасит медиана
            good[i] = regular & (~anom) & ((s != NORMAL) | (D[i] > 0)) & (s != "exclude")

        # 3) базовый уровень недели: среднее Вт–Чт обычного режима
        week = dates.to_period("W-SUN")
        week_codes, week_idx = np.unique(week.asi8, return_inverse=True)
        base = np.full((len(routes), len(week_codes)), np.nan)
        for i, r in enumerate(routes):
            m = good[i] & (lcls == "tt") & (st[r].to_numpy() == NORMAL)
            s_ = pd.Series(Dn[i][m]).groupby(week_idx[m])
            cnt = s_.count()
            mean = s_.mean()[cnt >= 2]
            base[i, mean.index.to_numpy()] = mean.to_numpy()

        wref = (pd.Timestamp(p.winter_ref[0]), min(pd.Timestamp(p.winter_ref[1]), cutoff))
        in_winter = (dates >= wref[0]) & (dates <= wref[1])
        stale_from = cutoff - pd.Timedelta(days=7 * p.stale_weeks)

        # 4) отношения типов дня к будню недели
        for i, r in enumerate(routes):
            s = st[r].to_numpy()
            rb = base[i, week_idx]
            ratio = Dn[i] / rb
            for cls in ("mon", "fri", "sat", "sun"):
                for state in np.unique(s[lcls == cls]):
                    m = good[i] & (lcls == cls) & (s == state) & np.isfinite(ratio)
                    if not m.any():
                        continue
                    idx = np.flatnonzero(m)[-p.ratio_weeks:]
                    recent = float(np.median(ratio[idx]))
                    mw = m & in_winter
                    winter = float(np.median(ratio[mw])) if mw.sum() >= 3 else None
                    if dates[idx[-1]] < stale_from and winter is not None:
                        recent = winter
                    f.R[(r, cls, state)] = (recent, winter)
        # Пн/Пт: сжатие к сетевому значению (индексы по маршрутам между периодами не воспроизводятся)
        for cls in ("mon", "fri"):
            vals = [f.R[(r, cls, NORMAL)][0] for r in routes if (r, cls, NORMAL) in f.R]
            if vals:
                netv = float(np.median(vals))
                for r in routes:
                    if (r, cls, NORMAL) in f.R:
                        rec, win = f.R[(r, cls, NORMAL)]
                        f.R[(r, cls, NORMAL)] = (p.weekday_shrink * netv + (1 - p.weekday_shrink) * rec, None)

        # 5) уровень будня: Пн–Пт последних недель, приведённые к Вт–Чт
        lo = cutoff - pd.Timedelta(days=7 * p.level_weeks)
        for i, r in enumerate(routes):
            s = st[r].to_numpy()
            m = good[i] & np.isin(lcls, ["mon", "tt", "fri"]) & (s == NORMAL)
            win = m & (dates > lo)
            if win.sum() < 5:
                win = np.zeros_like(m)
                win[np.flatnonzero(m)[-5 * p.level_weeks:]] = True
            div = np.array([1.0 if cl == "tt" else f.R.get((r, cl, NORMAL), (1.0, None))[0] for cl in lcls[win]])
            v = Dn[i][win] / div
            if p.level_estimator == "median":
                f.W[r] = float(np.median(v))
            elif p.level_estimator == "trim" and len(v) >= 5:
                v = np.sort(v)[1:-1]
                f.W[r] = float(v.mean())
            else:
                f.W[r] = float(v.mean())

        # 6) суточные профили
        for i, r in enumerate(routes):
            s = st[r].to_numpy()
            for pc, need in (("wd", 4), ("fri", 1), ("sat", 1), ("sun", 1)):
                for state in np.unique(s[pcls == pc]):
                    m = good[i] & (pcls == pc) & (s == state) & (D[i] > 0)
                    if not m.any():
                        continue
                    idx = np.flatnonzero(m)[-need * p.profile_weeks:]
                    recent = self._profile(cube[i, idx], D[i, idx])
                    mw = m & in_winter
                    winter = self._profile(cube[i, mw], D[i, mw]) if mw.sum() >= 3 else None
                    if dates[idx[-1]] < stale_from and winter is not None:
                        recent = winter
                    f.prof[(r, pc, state)] = (recent, winter)
        self.f = f
        return self

    def _profile(self, y: np.ndarray, d: np.ndarray) -> np.ndarray:
        if self.p.profile_estimator == "median":
            sh = np.median(y / d[:, None], axis=0)
            return sh / sh.sum() if sh.sum() > 0 else sh
        return y.sum(axis=0) / max(d.sum(), 1e-9)

    # ───────────────────────────── прогноз ─────────────────────────────
    def _ratio(self, r: int, cls: str, state: str, alpha: float) -> float:
        if cls == "tt":
            return 1.0
        key = (r, cls, state)
        if key not in self.f.R:
            base = self.f.R.get((r, cls, NORMAL), (1.0, None))[0]
            return base * self.defaults.get((r, state), 1.0)
        rec, win = self.f.R[key]
        if state == NORMAL and win is not None and cls in ("sat", "sun"):
            return (1 - alpha) * rec + alpha * win
        return rec

    def _prof(self, r: int, pc: str, state: str, alpha: float) -> np.ndarray:
        key = (r, pc, state)
        if key not in self.f.prof:
            key = (r, pc, NORMAL)
        if key not in self.f.prof:
            key = (r, "wd", NORMAL)
        rec, win = self.f.prof[key]
        if win is not None and key[2] == NORMAL:
            return (1 - alpha) * rec + alpha * win
        return rec

    def day_plan(self, r: int, day: pd.Timestamp, state: str) -> tuple[float, np.ndarray]:
        """Дневной уровень (без погоды) и суточный профиль для маршрута r в день day."""
        p, c = self.p, self.cal.loc[day]
        a = p.winter_alpha.get(day.month, 0.0)
        cls, sp = c["lcls"], c["special"]
        W = self.f.W[r]
        if sp == "worksat":
            return W * p.worksat_vs_wd, self._prof(r, "fri", NORMAL, a)
        if sp == "nye":
            return W * self._ratio(r, "sat", state, a) * p.nye_vs_sat, self._prof(r, "sat", state, a)
        if cls == "hol":
            L = W * self._ratio(r, "sun", state, a) * p.hol_vs_sun
            prof = self._prof(r, "sun", state, a)
            if p.hol_profile == "satsun":
                prof = 0.5 * (prof + self._prof(r, "sat", state, a))
            return L, prof
        L = W * self._ratio(r, cls, state, a)
        if sp == "pre_ny":
            L *= p.pre_ny.get(day.strftime("%Y-%m-%d"), 1.0)
        if sp == "short":
            L *= p.short_vs_wd
            return L, self._prof(r, "fri", state, a)
        return L, self._prof(r, c["pcls"], state, a)

    def predict(self, dates: pd.DatetimeIndex, routes: list[int] | None = None) -> pd.DataFrame:
        routes = routes or self.routes
        st = state_table(self.regs, self.cal.reset_index(), routes).reindex(dates)
        wf = wx.factor(self.weather, dates, self.f.beta)
        out = np.zeros((len(routes), len(dates), 24))
        for i, r in enumerate(routes):
            for j, day in enumerate(dates):
                L, prof = self.day_plan(r, day, st.iloc[j][r])
                out[i, j] = L * wf[j] * self.p.level_mult * self.p.level_month.get(day.month, 1.0)                     * self.p.route_mult.get(r, 1.0) * prof
        idx = pd.MultiIndex.from_product([routes, dates, range(24)], names=["route", "date", "hour"])
        return pd.DataFrame({"pred": out.reshape(-1)}, index=idx).reset_index()
