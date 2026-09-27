"""Производственный календарь РФ и классы дня для модели: 2025 (прогноз ноября–декабря) и 2026 (горизонт «год»).

Источник: isdayoff.ru (https://isdayoff.ru/api/getdata?year=YYYY&pre=1, коды 0/1/2; копия ml/external/calendar/isdayoff_YYYY.txt).
Фолбэк без сети: 2025 — постановление Правительства РФ № 1335 от 04.10.2024
(http://publication.pravo.gov.ru/document/0001202410040023), 2026 — встроенная копия ответа isdayoff.ru.
"""
from __future__ import annotations

import urllib.request

import numpy as np
import pandas as pd

from .config import CAL_DIR

OFF_WEEKDAYS_2025 = [
    "2025-01-01", "2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07", "2025-01-08",
    "2025-05-01", "2025-05-02", "2025-05-08", "2025-05-09", "2025-06-12", "2025-06-13",
    "2025-11-03", "2025-11-04", "2025-12-31",
]
WORK_WEEKENDS_2025 = ["2025-11-01"]
SHORT_DAYS_2025 = ["2025-03-07", "2025-04-30", "2025-06-11", "2025-11-01"]
# Официальные праздники (ст. 112 ТК РФ), в т. ч. выпавшие на выходные
OFFICIAL_HOLIDAYS_2025 = [
    "2025-01-01", "2025-01-02", "2025-01-03", "2025-01-04", "2025-01-05", "2025-01-06",
    "2025-01-07", "2025-01-08", "2025-02-23", "2025-03-08", "2025-05-01", "2025-05-09",
    "2025-06-12", "2025-11-04",
]
# Школьные каникулы Москвы — допущение (четвертная система), см. раздел 17 исследования
SCHOOL_HOLIDAYS = [
    ("2025-01-01", "2025-01-08"), ("2025-03-22", "2025-03-30"), ("2025-05-27", "2025-08-31"),
    ("2025-10-25", "2025-11-04"), ("2025-12-31", "2026-01-11"),
]
NYE = pd.Timestamp("2025-12-31")
# Праздники ст. 112 ТК РФ (месяц-день), для других лет
OFFICIAL_MD = ["01-01", "01-02", "01-03", "01-04", "01-05", "01-06", "01-07", "01-08",
               "02-23", "03-08", "05-01", "05-09", "06-12", "11-04"]
# Ответ https://isdayoff.ru/api/getdata?year=2026&pre=1 (проверен 27.09.2026): нерабочие будни 1–2, 5–9.01, 23.02, 9.03,
# 1.05, 11.05, 12.06, 4.11, 31.12; сокращённые 30.04, 8.05, 11.06, 3.11
ISDAYOFF_FALLBACK = {2026: (
    "1111111111100000110000011000001100000110000011000001110000110000011100001100000110000011000001100000"
    "1100000110000011000211100002111000011000001100000110000011000211100000110000011000001100000110000011"
    "0000011000001100000110000011000001100000110000011000001100000110000011000001100000110000011000001100"
    "00011021001100000110000011000001100000110000011000001100000110001")}


def _isdayoff_codes(offline: bool, year: int = 2025) -> str | None:
    f = CAL_DIR / f"isdayoff_{year}.txt"
    if f.exists():
        return f.read_text().strip()
    if offline:
        return None
    try:
        req = urllib.request.Request(f"https://isdayoff.ru/api/getdata?year={year}&pre=1",
                                     headers={"User-Agent": "tram-ml/1.0"})
        codes = urllib.request.urlopen(req, timeout=20).read().decode().strip()
        if len(codes) in (365, 366) and set(codes) <= set("0124"):
            CAL_DIR.mkdir(parents=True, exist_ok=True)
            f.write_text(codes)
            return codes
    except Exception:  # noqa: BLE001 — нет сети: фолбэк на встроенный список
        pass
    return None


def load_calendar(offline: bool = False, year: int = 2025) -> pd.DataFrame:
    if year != 2025:
        return _calendar_other_year(offline, year)
    days = pd.date_range("2025-01-01", "2025-12-31")
    cal = pd.DataFrame({"date": days})
    cal["dow"] = cal["date"].dt.dayofweek
    weekend = cal["dow"].to_numpy() >= 5
    codes = _isdayoff_codes(offline)
    if codes and len(codes) == len(days):
        c = np.array(list(codes))
        cal["is_off"], cal["is_short"] = c == "1", c == "2"
        cal.attrs["source"] = "isdayoff.ru"
    else:
        cal["is_off"] = weekend | cal["date"].isin(pd.to_datetime(OFF_WEEKDAYS_2025))
        cal.loc[cal["date"].isin(pd.to_datetime(WORK_WEEKENDS_2025)), "is_off"] = False
        cal["is_short"] = cal["date"].isin(pd.to_datetime(SHORT_DAYS_2025))
        cal.attrs["source"] = "постановление № 1335 (встроенный список)"
    cal["official"] = cal["date"].isin(pd.to_datetime(OFFICIAL_HOLIDAYS_2025))
    cal["work_weekend"] = weekend & ~cal["is_off"]
    cal["off_weekday"] = ~weekend & cal["is_off"]
    # Выходные внутри праздничных блоков (соседствуют с нерабочим будним или официальным праздником)
    hol_like = (cal["off_weekday"] | cal["official"]).to_numpy()
    near = np.convolve(hol_like.astype(int), np.ones(5, int), mode="same") > 0
    cal["hol_block"] = (cal["is_off"].to_numpy() & near) | (cal["date"] <= "2025-01-08").to_numpy()
    school = np.zeros(len(cal), bool)
    for a, b in SCHOOL_HOLIDAYS:
        school |= cal["date"].between(pd.Timestamp(a), pd.Timestamp(b)).to_numpy()
    cal["school"] = school

    lcls = np.array(["mon", "tt", "tt", "tt", "fri", "sat", "sun"])[cal["dow"]]
    lcls = np.where(cal["off_weekday"] | (cal["date"] <= "2025-01-08"), "hol", lcls)
    cal["lcls"] = lcls
    pcls = np.select([lcls == "fri", lcls == "sat", lcls == "sun", lcls == "hol"], ["fri", "sat", "sun", "hol"], "wd")
    cal["pcls"] = pcls
    # Особые дни, для которых класс берётся из соседнего и умножается на коэффициент
    special = np.full(len(cal), "", dtype=object)
    special[cal["work_weekend"].to_numpy()] = "worksat"
    special[(cal["date"] == NYE).to_numpy()] = "nye"
    special[cal["date"].isin(pd.to_datetime(["2025-12-29", "2025-12-30"])).to_numpy()] = "pre_ny"
    special[(cal["is_short"] & ~cal["work_weekend"]).to_numpy()] = "short"
    cal["special"] = special
    # «Чистый» день для оценки уровня/профиля: обычный будний/выходной вне праздничных блоков
    cal["regular"] = (cal["lcls"] != "hol") & ~cal["hol_block"] & (cal["special"] == "")
    return cal


def _calendar_other_year(offline: bool, year: int) -> pd.DataFrame:
    """Те же поля, что у 2025, для горизонта «год». Школьные каникулы не задаются (в модели не используются)."""
    days = pd.date_range(f"{year}-01-01", f"{year}-12-31")
    codes = _isdayoff_codes(offline, year)
    src = "isdayoff.ru"
    if not codes or len(codes) != len(days):
        codes, src = ISDAYOFF_FALLBACK.get(year), "isdayoff.ru (встроенная копия)"
    if not codes:
        raise RuntimeError(f"нет производственного календаря на {year} год: нужна сеть (isdayoff.ru)")
    cal = pd.DataFrame({"date": days})
    cal["dow"] = cal["date"].dt.dayofweek
    weekend = cal["dow"].to_numpy() >= 5
    c = np.array(list(codes))
    cal["is_off"], cal["is_short"] = c == "1", c == "2"
    cal.attrs["source"] = src
    cal["official"] = cal["date"].dt.strftime("%m-%d").isin(OFFICIAL_MD)
    cal["work_weekend"] = weekend & ~cal["is_off"]
    cal["off_weekday"] = ~weekend & cal["is_off"]
    ny_block = (cal["date"] <= pd.Timestamp(f"{year}-01-08")).to_numpy()
    hol_like = (cal["off_weekday"] | cal["official"]).to_numpy()
    near = np.convolve(hol_like.astype(int), np.ones(5, int), mode="same") > 0
    cal["hol_block"] = (cal["is_off"].to_numpy() & near) | ny_block
    cal["school"] = False
    lcls = np.array(["mon", "tt", "tt", "tt", "fri", "sat", "sun"])[cal["dow"]]
    lcls = np.where(cal["off_weekday"] | ny_block, "hol", lcls)
    cal["lcls"] = lcls
    cal["pcls"] = np.select([lcls == "fri", lcls == "sat", lcls == "sun", lcls == "hol"], ["fri", "sat", "sun", "hol"], "wd")
    special = np.full(len(cal), "", dtype=object)
    special[cal["work_weekend"].to_numpy()] = "worksat"
    nye = pd.Timestamp(f"{year}-12-31")
    if cal.loc[cal["date"] == nye, "is_off"].item():
        special[(cal["date"] == nye).to_numpy()] = "nye"
    pre = pd.to_datetime([f"{year}-12-29", f"{year}-12-30"])
    special[(cal["date"].isin(pre) & ~cal["is_off"]).to_numpy()] = "pre_ny"
    special[(cal["is_short"] & ~cal["work_weekend"]).to_numpy()] = "short"
    cal["special"] = special
    cal["regular"] = (cal["lcls"] != "hol") & ~cal["hol_block"] & (cal["special"] == "")
    return cal
