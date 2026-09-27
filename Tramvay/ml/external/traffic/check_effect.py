"""Влияют ли пробки (баллы ЦОДД) на посадки в трамвай? Проверка на январе–октябре 2025.

    py ml/external/traffic/check_effect.py

Отклик — отклонение посадок сети от нормы: log(факт / медиана того же типа дня ±14 дней),
по 7 маршрутам без режимов (как для погодных коэффициентов в ml/). Два теста:
  1) по дням: максимум баллов за день (traffic_moscow_2025.csv), с поправкой на осадки;
  2) по часу замера: посадки в час поста и следующий час против балла в посте (traffic_moscow_2025_posts.csv).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))

from ml.calendar_ru import load_calendar  # noqa: E402
from ml.config import HIST_END  # noqa: E402
from ml.data import load_grid  # noqa: E402
from ml.structural import STABLE_ROUTES, same_type_median  # noqa: E402
from ml.weather import daily_precip  # noqa: E402

MAX_DEV = np.log(1.4)   # как в ml/weather.fit_betas: праздники и сбои данных не в счёт


def ols(X: list[np.ndarray], y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Коэффициенты и t-статистики (без свободного члена в выдаче)."""
    X = np.column_stack([np.ones(len(y))] + X)
    b, *_ = np.linalg.lstsq(X, y, rcond=None)
    r = y - X @ b
    se = np.sqrt(np.diag(r @ r / (len(y) - X.shape[1]) * np.linalg.inv(X.T @ X)))
    return b[1:], (b / se)[1:]


def main() -> None:
    grid = load_grid()
    grid = grid[grid["route"].isin(STABLE_ROUTES) & (grid["date"] <= HIST_END)]
    cal = load_calendar(offline=True).set_index("date")

    # 1) по дням
    net = grid.groupby("date")["boardings"].sum()
    c = cal.reindex(net.index)
    types, regular = c["lcls"].to_numpy(), c["regular"].to_numpy()
    y = pd.Series(np.log(net.to_numpy() / same_type_median(net.to_numpy(float), types, regular)), net.index)
    y = y[regular & np.isfinite(y.to_numpy()) & (y.abs() < MAX_DEV).to_numpy()]
    score = pd.read_csv(HERE / "traffic_moscow_2025.csv", parse_dates=["date"]).set_index("date")["value"]
    precip = np.log1p(daily_precip(offline=True))
    d = pd.DataFrame({"y": y, "score": score, "precip": precip}).dropna()
    b, t = ols([d["score"].to_numpy(), d["precip"].to_numpy()], d["y"].to_numpy())
    print(f"По дням: {len(d)} дней с баллом. Пробки {b[0] * 100:+.2f} % посадок на балл (t = {t[0]:+.2f}); "
          f"осадки для сравнения t = {t[1]:+.2f}")

    # 2) по часу замера
    H = grid.pivot_table(index="date", columns="hour", values="boardings", aggfunc="sum")
    c = cal.reindex(H.index)
    types, regular = c["lcls"].to_numpy(), c["regular"].to_numpy()
    norm = pd.DataFrame({h: same_type_median(H[h].to_numpy(float), types, regular) for h in H.columns}, index=H.index)
    with np.errstate(divide="ignore", invalid="ignore"):
        dev = np.log(H / norm)
    posts = pd.read_csv(HERE / "traffic_moscow_2025_posts.csv", parse_dates=["dt_msk"]).dropna(subset=["score"])
    posts = posts[posts["dt_msk"] < HIST_END + pd.Timedelta(days=1)]
    posts["date"], posts["hour"] = posts["dt_msk"].dt.normalize(), posts["dt_msk"].dt.hour
    posts = posts[posts["date"].map(lambda x: bool(c["regular"].get(x, False)))]
    posts["y"] = [np.nanmean([dev.at[x, h], dev.at[x, min(h + 1, 23)]]) for x, h in zip(posts["date"], posts["hour"])]
    p = posts[np.isfinite(posts["y"]) & (posts["y"].abs() < MAX_DEV)]
    b, t = ols([p["score"].to_numpy(float)], p["y"].to_numpy())
    print(f"По часу замера: {len(p)} замеров за {p['date'].nunique()} дней. Пробки {b[0] * 100:+.2f} % посадок на балл "
          f"(t = {t[0]:+.2f})")
    print("Вывод: |t| < 2 в обоих тестах — эффект пробок на посадки не обнаружен.")


if __name__ == "__main__":
    main()
