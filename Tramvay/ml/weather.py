"""Погода Open-Meteo (ERA5/IFS, центр Москвы) → дневной множитель спроса.

Источник: https://archive-api.open-meteo.com/v1/archive (CC BY 4.0). Коэффициенты оцениваются OLS
по сетевым дневным суммам относительно медианы того же типа дня ±14 дней, только на данных до отсечки.
"""
from __future__ import annotations

import json
import urllib.request

import numpy as np
import pandas as pd

from .config import MOSCOW, WEATHER_DIR

FEATURES = ["precip", "snowfall", "t_anom"]
# дневные осадки 2025 (мм) из почасового архива Open-Meteo — чтобы всё воспроизводилось без сети
PRECIP_FILE = WEATHER_DIR / "precip_moscow_2025_daily.csv"


def load_weather(offline: bool = False) -> pd.DataFrame | None:
    f = WEATHER_DIR / "weather_moscow_2025_hourly.csv"
    if not f.exists():
        if offline:
            return None
        url = ("https://archive-api.open-meteo.com/v1/archive?"
               f"latitude={MOSCOW[0]}&longitude={MOSCOW[1]}&start_date=2025-01-01&end_date=2025-12-31"
               "&hourly=temperature_2m,precipitation,rain,snowfall,snow_depth,wind_speed_10m"
               "&timezone=Europe%2FMoscow")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "tram-ml/1.0"})
            data = json.loads(urllib.request.urlopen(req, timeout=90).read())
            WEATHER_DIR.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(data["hourly"]).to_csv(f, index=False)
        except Exception:  # noqa: BLE001 — нет сети: модель работает без погоды
            return None
    h = pd.read_csv(f, parse_dates=["time"])
    h["date"] = h["time"].dt.normalize()
    day = h.groupby("date").agg(t_mean=("temperature_2m", "mean"), precip=("precipitation", "sum"),
                                snowfall=("snowfall", "sum")).reset_index()
    # аномалия температуры к скользящей 29-дневной норме (центрированной)
    day["t_anom"] = day["t_mean"] - day["t_mean"].rolling(29, center=True, min_periods=10).mean()
    day["precip_mm"] = day["precip"]
    return day.set_index("date")


def transform(w: pd.DataFrame | None, how: str) -> pd.DataFrame | None:
    """Форма эффекта осадков: linear — мм; log1p — насыщение (ливень 20 мм ≈ 3× эффекта 2 мм, а не 10×)."""
    if w is None:
        return None
    w = w.copy()
    if how == "log1p":
        w["precip"] = np.log1p(w["precip_mm"])
    elif how.startswith("cap"):
        w["precip"] = w["precip_mm"].clip(upper=float(how[3:]))
    else:
        w["precip"] = w["precip_mm"]
    return w


def fit_betas(net_daily: pd.Series, expected: pd.Series, clean: pd.Series, w: pd.DataFrame,
              default: dict, min_days: int = 20) -> tuple[dict, dict]:
    """OLS log(факт/ожидание) ~ осадки + снегопад + аномалия температуры. Возвращает (β, t-статистики)."""
    y = np.log(net_daily / expected)
    ok = clean & np.isfinite(y) & (y.abs() < np.log(1.4))
    df = w.reindex(y.index)[FEATURES].assign(y=y)[ok].dropna()
    if len(df) < min_days:
        return dict(default), {k: np.nan for k in FEATURES}
    X = np.column_stack([np.ones(len(df)), df[FEATURES].to_numpy()])
    coef, *_ = np.linalg.lstsq(X, df["y"].to_numpy(), rcond=None)
    resid = df["y"].to_numpy() - X @ coef
    sigma2 = resid @ resid / max(1, len(df) - X.shape[1])
    se = np.sqrt(np.diag(sigma2 * np.linalg.inv(X.T @ X)))
    beta = {k: float(coef[i + 1]) for i, k in enumerate(FEATURES)}
    tstat = {k: float(coef[i + 1] / se[i + 1]) for i, k in enumerate(FEATURES)}
    # незначимые коэффициенты (|t| < 2) обнуляем — поправка второго порядка не должна добавлять шум
    beta = {k: (v if abs(tstat[k]) >= 2 else 0.0) for k, v in beta.items()}
    return beta, tstat


def daily_precip(offline: bool = False) -> pd.Series | None:
    """Суточные осадки, мм (индекс — дата): из ml/external/weather, иначе из архива Open-Meteo (и сохраняем копию)."""
    if PRECIP_FILE.exists():
        return pd.read_csv(PRECIP_FILE, parse_dates=["date"]).set_index("date")["precip_mm"]
    w = load_weather(offline)
    if w is None:
        return None
    s = w["precip_mm"].rename("precip_mm")
    WEATHER_DIR.mkdir(parents=True, exist_ok=True)
    s.rename_axis("date").reset_index().to_csv(PRECIP_FILE, index=False)
    return s


def redistribute(pred: pd.DataFrame, precip_mm: pd.Series, beta: float, mask=None,
                 col: str = "pred") -> pd.DataFrame:
    """Погода как отклонение от нормы месяца: множитель дня exp(β·log1p(мм)) нормирован так, что сумма ячеек
    mask за каждый месяц не меняется. Погода только перераспределяет посадки между сухими и дождливыми днями
    и не спорит с уровнем, откалиброванным по лидерборду."""
    out = pred.copy()
    dates = pd.to_datetime(out["date"])
    m = np.ones(len(out), bool) if mask is None else np.asarray(mask, bool)
    f = np.exp(beta * np.log1p(precip_mm.reindex(dates).fillna(0.0).to_numpy()))
    month = dates.dt.to_period("M")
    v = out[col].where(m, 0.0)
    norm = v.groupby(month).transform("sum") / (v * f).groupby(month).transform("sum")
    out.loc[m, col] = (out[col] * f * norm)[m]
    return out


def factor(w: pd.DataFrame | None, dates: pd.DatetimeIndex, beta: dict) -> np.ndarray:
    """exp(Σ β·x) для каждого дня; без погоды — единицы."""
    if w is None:
        return np.ones(len(dates))
    x = w.reindex(dates)[FEATURES].fillna(0.0)
    return np.exp(sum(beta.get(k, 0.0) * x[k].to_numpy() for k in FEATURES))
