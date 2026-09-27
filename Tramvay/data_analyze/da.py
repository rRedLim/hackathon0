"""
da.py — разведочный анализ и визуализация данных трека
«ИИ-прогноз загрузки трамвайных маршрутов» (Хакатон Московского транспорта).

Что делает:
  * строит полную сетку маршрут × дата × час из labels (пропуски = 0 посадок);
  * графики структуры спроса: тренд, сезонность, профили суток, дни недели,
    праздники, режимы маршрутов (закрытия по выходным), аномальные дни;
  * внешние факторы: производственный календарь РФ (isdayoff.ru), погода
    (Open-Meteo archive), школьные каникулы — с оценкой эффекта на данных;
  * геопривязка: карта остановок из справочников (PNG + интерактивный HTML/Leaflet);
  * бэктест простого профильного бейзлайна в постановке задачи (горизонт 61 день);
  * проверка формата и «продолжения» файла сабмита;
  * (--raw) потоковая агрегация сырых train/test.csv (~10 ГБ): отказы валидаций,
    типы билетов, пересадки, вагоны на линии и нагрузка на вагон, уникальные
    пассажиры, сверка с labels. Результат кэшируется в parquet.

Запуск:
  py da.py                          # всё, кроме сырых CSV
  py da.py --raw                    # + агрегация сырых CSV (один раз, дальше из кэша)
  py da.py --submission sub.csv     # проверить свой сабмит
  py da.py --offline                # без сети: погода/календарь из кэша или встроенные

Результат: data_analyze/output/*.png, report.html, map_stops.html,
           summary.json, cache/*
"""
from __future__ import annotations

import argparse
import html
import json
import math
import sys
import textwrap
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Rectangle
from matplotlib.ticker import FuncFormatter, PercentFormatter

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# ─────────────────────────────── константы задачи ───────────────────────────────
ALL_ROUTES = [1, 5, 7, 11, 12, 17, 25, 26, 28, 50]           # маршруты сабмита
HIST_START, HIST_END = pd.Timestamp("2025-01-01"), pd.Timestamp("2025-10-31")
SPLIT_DATE = pd.Timestamp("2025-09-01")                      # train | test
FC_START, FC_END = pd.Timestamp("2025-11-01"), pd.Timestamp("2025-12-31")
HORIZON = 61
WAPE_MAX_BALL = 0.88                                         # порог максимального балла
MOSCOW = (55.7558, 37.6173)

DAYTYPES = ["Пн–Чт", "Пт", "Сб", "Вс", "Праздник"]
DOW_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS_RU = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
MONTHS_RU_FULL = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль",
                  "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]

# Производственный календарь 2025 (фолбэк, если isdayoff.ru недоступен).
# Источник: постановление Правительства РФ № 1335 от 04.10.2024, isdayoff.ru.
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
# Школьные каникулы Москвы — ДОПУЩЕНИЕ: четвертная система, рекомендации Минпросвещения
# (https://www.garant.ru/news/1844818/). У модульной системы даты другие — правьте здесь.
SCHOOL_HOLIDAYS = [
    ("2024-12-29", "2025-01-08", "зимние"),
    ("2025-03-22", "2025-03-30", "весенние"),
    ("2025-05-27", "2025-08-31", "летние"),
    ("2025-10-25", "2025-11-04", "осенние"),
    ("2025-12-31", "2026-01-11", "зимние"),
]

SOURCES = {
    "Производственный календарь": "https://isdayoff.ru/api/getdata?year=2025&pre=1 "
                                  "(постановление № 1335: http://publication.pravo.gov.ru/document/0001202410040023)",
    "Погода (факт, ERA5/IFS)": "https://archive-api.open-meteo.com/v1/archive",
    "Школьные каникулы": "https://www.garant.ru/news/1844818/",
    "Геометрия маршрутов/остановок": "dataset/spravochniki/Хакатон_справочники_трамвай_10_маршрутов.xlsx",
}

# ─────────────────────────────── палитра и стиль ────────────────────────────────
SURFACE, INK, INK2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, NEUTRAL = "#e1e0d9", "#c3c2b7", "#f0efec"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
BLUE = {100: "#cde2fb", 150: "#b7d3f6", 200: "#9ec5f4", 250: "#86b6ef", 300: "#6da7ec",
        350: "#5598e7", 400: "#3987e5", 450: "#2a78d6", 500: "#256abf", 550: "#1c5cab",
        600: "#184f95", 650: "#104281", 700: "#0d366b"}
CRITICAL, SERIOUS = "#d03b3b", "#ec835a"
CONTEXT = "#d6d5ce"                                   # серые «фоновые» линии других маршрутов
SEQ = LinearSegmentedColormap.from_list("seq_blue", [BLUE[k] for k in sorted(BLUE)])
SEQ.set_bad(SURFACE)
DIV = LinearSegmentedColormap.from_list("div_red_blue", ["#a8302f", "#e34948", NEUTRAL, BLUE[400], BLUE[600]])
DIV.set_bad(SURFACE)
DAYTYPE_COLOR = dict(zip(DAYTYPES, SERIES[:5]))
BAR = dict(edgecolor=SURFACE, linewidth=1.2)        # зазор цвета фона между соседними столбцами


def setup_style() -> None:
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "font.family": ["Segoe UI", "DejaVu Sans"], "font.size": 9.5,
        "text.color": INK, "axes.labelcolor": INK2, "axes.labelsize": 9,
        "xtick.color": MUTED, "ytick.color": MUTED, "xtick.labelsize": 8.5, "ytick.labelsize": 8.5,
        "xtick.labelcolor": INK2, "ytick.labelcolor": INK2,
        "axes.edgecolor": AXIS, "axes.linewidth": 0.8,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "grid.linestyle": "-",
        "axes.axisbelow": True, "axes.spines.top": False, "axes.spines.right": False,
        "axes.titlesize": 10, "axes.titleweight": "semibold", "axes.titlelocation": "left",
        "axes.titlecolor": INK, "axes.titlepad": 6,
        "lines.linewidth": 1.4, "lines.solid_capstyle": "round", "lines.solid_joinstyle": "round",
        "legend.frameon": False, "legend.fontsize": 8.5,
        "figure.dpi": 100, "savefig.dpi": 150,
    })


# ─────────────────────────────── утилиты ───────────────────────────────
def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def num(x: float, d: int = 1) -> str:
    """Число в русской записи: пробел для тысяч, запятая для дробной части."""
    s = f"{x:,.{d}f}".replace(",", " ").replace(".", ",")
    return s


def compact(x: float) -> str:
    ax = abs(x)
    if ax >= 1e6:
        return f"{num(x / 1e6, 2)} млн"
    if ax >= 1e4:
        return f"{num(x / 1e3, 1)} тыс."
    return num(x, 0)


THOUSANDS = FuncFormatter(lambda x, _: num(x, 0))
KFMT = FuncFormatter(lambda x, _: (num(x / 1000, 0) + "K") if abs(x) >= 1000 else num(x, 0))


def month_axis(ax, fmt_full: bool = False) -> None:
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    names = MONTHS_RU_FULL if fmt_full else MONTHS_RU
    ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _: names[mdates.num2date(x).month - 1]))


def ink_for(rgba) -> str:
    r, g, b = rgba[:3]
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return INK if lum > 0.55 else "#ffffff"


def wape_score(y, yhat) -> float:
    y, yhat = np.asarray(y, float), np.asarray(yhat, float)
    return max(0.0, 1.0 - np.abs(y - yhat).sum() / max(y.sum(), 1e-9))


class Figures:
    """Сохраняет PNG и собирает подписи для HTML-отчёта."""

    def __init__(self, out: Path):
        self.out = out
        self.items: list[dict] = []

    def new(self, w: float, h: float, title: str, subtitle: str | None = None, legend_rows: int = 0):
        """Фигура с шапкой: заголовок, подзаголовок (с переносом), место под общую легенду."""
        lines = textwrap.wrap(subtitle, width=int((w - 0.4) / 0.068)) if subtitle else []
        legend_top = 0.5 + 0.19 * len(lines) + (0.08 if lines else 0)
        top_in = legend_top + 0.3 * legend_rows + 0.05
        h += top_in - 0.5                       # высота графиков не зависит от длины шапки
        fig = plt.figure(figsize=(w, h), layout="constrained")
        fig.get_layout_engine().set(rect=(0, 0, 1, 1 - top_in / h), h_pad=0.06, w_pad=0.06)
        fig.text(0.14 / w, 1 - 0.14 / h, title, ha="left", va="top", fontsize=14, weight="semibold", color=INK)
        if lines:
            fig.text(0.14 / w, 1 - 0.5 / h, "\n".join(lines), ha="left", va="top", fontsize=9.5,
                     color=INK2, linespacing=1.35)
        fig._legend_top = legend_top
        return fig

    def legend(self, fig, handles, ncol=6):
        w, h = fig.get_size_inches()
        top = getattr(fig, "_legend_top", 0.5)
        return fig.legend(handles=handles, loc="upper left", bbox_to_anchor=(0.08 / w, 1 - top / h), ncol=ncol,
                          frameon=False, handlelength=1.6, columnspacing=1.4, fontsize=9, borderaxespad=0)

    def save(self, fig, name: str, section: str, title: str, caption: str) -> None:
        path = self.out / f"{name}.png"
        fig.savefig(path)
        plt.close(fig)
        self.items.append(dict(file=path.name, section=section, title=title, caption=caption))
        log(f"  ✓ {path.name}")


def line_key(color, lw=2.0, label=""):
    return Line2D([0], [0], color=color, lw=lw, label=label)


def dot_key(color, label=""):
    return Line2D([0], [0], color=color, marker="o", lw=0, markersize=6, label=label)


# ─────────────────────────────── загрузка данных ───────────────────────────────
def load_labels(data_dir: Path) -> pd.DataFrame:
    parts = []
    for split in ("train", "test"):
        f = data_dir / "labels" / f"labels_day_{split}.csv"
        if not f.exists():
            raise SystemExit(f"Не найден {f}. Укажите --data путь к распакованному dataset.")
        parts.append(pd.read_csv(f, sep=";", parse_dates=["date"]))
    lab = pd.concat(parts, ignore_index=True)
    lab = lab.groupby(["route", "date", "hour"], as_index=False)["boardings"].sum()
    routes = sorted(lab["route"].unique())
    dates = pd.date_range(HIST_START, HIST_END)
    idx = pd.MultiIndex.from_product([routes, dates, range(24)], names=["route", "date", "hour"])
    g = lab.set_index(["route", "date", "hour"])["boardings"].reindex(idx, fill_value=0).reset_index()
    g["split"] = np.where(g["date"] < SPLIT_DATE, "train", "test")
    return g


def fetch(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "tram-da/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def load_calendar(cache: Path, offline: bool) -> pd.DataFrame:
    days = pd.date_range("2025-01-01", "2025-12-31")
    f = cache / "isdayoff_2025.txt"
    codes, source = None, "встроенный список (постановление № 1335)"
    if f.exists():
        codes, source = f.read_text().strip(), "isdayoff.ru (кэш)"
    elif not offline:
        try:
            codes = fetch("https://isdayoff.ru/api/getdata?year=2025&pre=1", 20).decode().strip()
            if len(codes) == len(days) and set(codes) <= set("0124"):
                f.write_text(codes)
                source = "isdayoff.ru"
            else:
                codes = None
        except Exception as e:  # noqa: BLE001
            log(f"  isdayoff.ru недоступен ({e}); беру встроенный календарь")
    cal = pd.DataFrame({"date": days})
    cal["dow"] = cal["date"].dt.dayofweek
    weekend = cal["dow"] >= 5
    if codes and len(codes) == len(days):
        c = np.array(list(codes))
        cal["is_off"] = c == "1"
        cal["is_short"] = c == "2"
    else:
        cal["is_off"] = weekend | cal["date"].isin(pd.to_datetime(OFF_WEEKDAYS_2025))
        cal.loc[cal["date"].isin(pd.to_datetime(WORK_WEEKENDS_2025)), "is_off"] = False
        cal["is_short"] = cal["date"].isin(pd.to_datetime(SHORT_DAYS_2025))
    cal["is_official"] = cal["date"].isin(pd.to_datetime(OFFICIAL_HOLIDAYS_2025))
    cal["work_weekend"] = weekend & ~cal["is_off"]
    cal["off_weekday"] = ~weekend & cal["is_off"]
    school = np.zeros(len(cal), bool)
    for a, b, _ in SCHOOL_HOLIDAYS:
        school |= cal["date"].between(pd.Timestamp(a), pd.Timestamp(b)).to_numpy()
    cal["school"] = school

    def daytype(r):
        if r.off_weekday or r.is_official or r.date <= pd.Timestamp("2025-01-08"):
            return "Праздник"
        if r.work_weekend:
            return "Пт"          # рабочая сокращённая суббота ведёт себя как предпраздничный будний
        return ("Пн–Чт", "Пн–Чт", "Пн–Чт", "Пн–Чт", "Пт", "Сб", "Вс")[r.dow]

    cal["daytype"] = [daytype(r) for r in cal.itertuples()]
    cal.attrs["source"] = source
    return cal


def load_weather(cache: Path, offline: bool) -> pd.DataFrame | None:
    f = cache / "weather_moscow_2025_hourly.csv"
    if not f.exists():
        if offline:
            return None
        url = ("https://archive-api.open-meteo.com/v1/archive?"
               f"latitude={MOSCOW[0]}&longitude={MOSCOW[1]}&start_date=2025-01-01&end_date=2025-12-31"
               "&hourly=temperature_2m,precipitation,rain,snowfall,snow_depth,wind_speed_10m"
               "&timezone=Europe%2FMoscow")
        try:
            data = json.loads(fetch(url, 90))
            h = pd.DataFrame(data["hourly"])
            h.to_csv(f, index=False)
        except Exception as e:  # noqa: BLE001
            log(f"  Open-Meteo недоступен ({e}); погодные графики пропущены")
            return None
    h = pd.read_csv(f, parse_dates=["time"])
    h["date"] = h["time"].dt.normalize()
    h["hour"] = h["time"].dt.hour
    day = h.groupby("date").agg(
        t_mean=("temperature_2m", "mean"), t_min=("temperature_2m", "min"), t_max=("temperature_2m", "max"),
        precip=("precipitation", "sum"), rain=("rain", "sum"), snowfall=("snowfall", "sum"),
        snow_depth=("snow_depth", "mean"), wind=("wind_speed_10m", "mean"),
    ).reset_index()
    day["snow_depth"] *= 100                                  # м → см
    day.attrs["hourly"] = h
    return day


def load_reference(data_dir: Path):
    f = data_dir / "spravochniki" / "Хакатон_справочники_трамвай_10_маршрутов.xlsx"
    if not f.exists():
        return None, None
    x = pd.ExcelFile(f)
    routes = x.parse(x.sheet_names[0], header=1)
    stops = x.parse(x.sheet_names[-1])                       # «Порядок_с_координатами»
    routes = routes[pd.to_numeric(routes["route_short_name"], errors="coerce").notna()].copy()
    routes["route_short_name"] = routes["route_short_name"].astype(int)
    stops = stops.dropna(subset=["stop_lat", "stop_lon"]).copy()
    stops["stop_lat"] = stops["stop_lat"].astype(float)
    stops["stop_lon"] = stops["stop_lon"].astype(float)
    return routes, stops


# ─────────────────────────────── производные таблицы ───────────────────────────────
def expected_same_type(values: np.ndarray, types: np.ndarray, usable: np.ndarray, window: int = 14) -> np.ndarray:
    """Медиана того же типа дня в окне ±window дней (без самого дня и неиспользуемых дней)."""
    n = len(values)
    out = np.full(n, np.nan)
    for i in range(n):
        lo, hi = max(0, i - window), min(n, i + window + 1)
        m = (types[lo:hi] == types[i]) & usable[lo:hi]
        m[i - lo] = False
        if m.sum() >= 2:
            out[i] = np.median(values[lo:hi][m])
    return out


def build_daily(g: pd.DataFrame, cal: pd.DataFrame):
    daily = g.groupby(["date", "route"])["boardings"].sum().unstack("route")
    c = cal.set_index("date").reindex(daily.index)
    ratio = pd.DataFrame(index=daily.index, columns=daily.columns, dtype=float)
    usable = (c["daytype"] != "Праздник").to_numpy()
    for r in daily.columns:
        v = daily[r].to_numpy(float)
        exp = expected_same_type(v, c["daytype"].to_numpy(), usable & (v > 0))
        ratio[r] = v / exp
    return daily, c, ratio


# ─────────────────────────────── графики: структура спроса ───────────────────────────────
def shade_holidays(ax, c: pd.DataFrame, school: bool = True) -> None:
    for d in c.index[c["daytype"] == "Праздник"]:
        ax.axvspan(d - pd.Timedelta(hours=12), d + pd.Timedelta(hours=12), color=GRID, lw=0, zorder=0)
    if school:
        for a, b, _ in SCHOOL_HOLIDAYS:
            a, b = max(pd.Timestamp(a), c.index.min()), min(pd.Timestamp(b), c.index.max())
            if a <= b:
                ax.axvspan(a, b + pd.Timedelta(days=1), ymin=0, ymax=0.025, color=BLUE[300], lw=0, zorder=1)


def split_line(ax, label: bool = True) -> None:
    ax.axvline(SPLIT_DATE, color=INK2, lw=0.8, zorder=2)
    if label:
        ax.text(SPLIT_DATE, 1.0, "  test →", transform=ax.get_xaxis_transform(), va="top",
                ha="left", fontsize=8, color=INK2)
        ax.text(SPLIT_DATE, 1.0, "← train  ", transform=ax.get_xaxis_transform(), va="top",
                ha="right", fontsize=8, color=INK2)


def fig_network_daily(F: Figures, daily, c, S):
    net = daily.sum(axis=1)
    roll = net.rolling(7, center=True).mean()
    S["network"] = dict(total=int(net.sum()), mean_daily=float(net.mean()), days=int(len(net)),
                        routes=[int(r) for r in daily.columns])
    fig = F.new(13, 5.2, "Сеть: посадки в сутки, январь–октябрь 2025",
                f"Σ {compact(net.sum())} посадок · {len(daily.columns)} маршрутов с разметкой · "
                f"в среднем {compact(net.mean())} в сутки · серые полосы — праздники/нерабочие дни, "
                f"синяя черта снизу — школьные каникулы (допущение)", legend_rows=1)
    ax = fig.subplots()
    shade_holidays(ax, c)
    ax.plot(net.index, net.values, color=BLUE[200], lw=0.9, label="сутки")
    ax.plot(roll.index, roll.values, color=SERIES[0], lw=1.8, label="среднее за 7 дней")
    split_line(ax)
    for d in [net.idxmax(), net.idxmin()]:
        ax.annotate(f"{d:%d.%m} · {compact(net[d])}", (d, net[d]), xytext=(6, 4 if d == net.idxmax() else -12),
                    textcoords="offset points", fontsize=8.5, color=INK)
        ax.scatter([d], [net[d]], s=20, color=SERIES[0], edgecolor=SURFACE, linewidth=1.5, zorder=4)
    ax.set_ylim(0, None)
    ax.yaxis.set_major_formatter(THOUSANDS)
    ax.set_ylabel("посадок в сутки")
    month_axis(ax, True)
    ax.margins(x=0.005)
    F.legend(fig, [line_key(BLUE[200], 1.2, "сутки"), line_key(SERIES[0], 2, "среднее за 7 дней"),
                   Patch(color=GRID, label="праздник / нерабочий"), Patch(color=BLUE[300], label="школьные каникулы")], ncol=4)
    F.save(fig, "01_network_daily", "Структура спроса", "Сеть по дням",
           "Недельный цикл, праздничные провалы (январь, май, июнь), летний спад и рост осенью. "
           "Уровень в сентябре–октябре — ближайший ориентир для ноября–декабря.")


def fig_routes_daily(F: Figures, daily, c, route_names):
    order = daily.mean().sort_values(ascending=False).index
    n = len(order)
    ncol = 3
    nrow = math.ceil(n / ncol)
    fig = F.new(14, 2.35 * nrow + 0.8, "Маршруты: посадки в сутки",
                "Тонкая линия — сутки, жирная — скользящее среднее за 7 дней. Шкала Y у каждого маршрута своя.")
    axes = fig.subplots(nrow, ncol, sharex=True).ravel()
    for ax, r in zip(axes, order):
        s = daily[r]
        shade_holidays(ax, c, school=False)
        ax.plot(s.index, s.values, color=BLUE[200], lw=0.7)
        ax.plot(s.index, s.rolling(7, center=True).mean(), color=SERIES[0], lw=1.6)
        split_line(ax, label=False)
        name = route_names.get(r, "")
        name = name if len(name) <= 34 else name[:33] + "…"
        ax.set_title(f"№{r} · {compact(s.mean())}/сут" + (f"  ·  {name}" if name else ""), fontsize=9.5)
        ax.set_ylim(0, None)
        ax.yaxis.set_major_formatter(KFMT)
        month_axis(ax)
    for ax in axes[n:]:
        ax.set_visible(False)
    F.save(fig, "02_routes_daily", "Структура спроса", "Маршруты по дням",
           "Режимы маршрутов: летний провал №7 (ремонт), нулевые выходные №50 с сентября, скачки №26 и №25. "
           "Такие сдвиги уровня — главный источник ошибки на 61-дневном горизонте.")


def heatmap(ax, data: pd.DataFrame, cmap, vmin, vmax, fmt=None, fontsize=8, center=None):
    im = ax.imshow(data.to_numpy(float), aspect="auto", cmap=cmap, vmin=vmin, vmax=vmax, interpolation="nearest")
    ax.set_xticks(range(data.shape[1]), [str(x) for x in data.columns])
    ax.set_yticks(range(data.shape[0]), [str(x) for x in data.index])
    ax.grid(False)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.tick_params(length=0)
    if fmt:
        arr = data.to_numpy(float)
        for i in range(arr.shape[0]):
            for j in range(arr.shape[1]):
                v = arr[i, j]
                if np.isfinite(v):
                    ax.text(j, i, fmt(v), ha="center", va="center", fontsize=fontsize,
                            color=ink_for(im.cmap(im.norm(v))))
    return im


def fig_route_month(F: Figures, daily, S):
    m = daily.groupby(daily.index.month)
    share = (m.sum().T / m.sum().sum(axis=1)) * 100
    mean_d = m.mean().T
    idx = mean_d.div(daily.mean(), axis=0) * 100
    order = daily.mean().sort_values(ascending=False).index
    share, idx = share.loc[order], idx.loc[order]
    cols = [MONTHS_RU[i - 1] for i in share.columns]
    share.columns = idx.columns = cols
    share.index = idx.index = [f"№{r}" for r in order]
    S["month_index"] = {k: {c: round(float(v), 1) for c, v in row.items()} for k, row in idx.iterrows()}
    fig = F.new(14, 5.4, "Сезонность и доли маршрутов по месяцам",
                "Слева — доля маршрута в посадках сети, %. Справа — средние сутки месяца к среднему маршрута за янв–окт (=100).")
    a1, a2 = fig.subplots(1, 2)
    im1 = heatmap(a1, share, SEQ, 0, share.to_numpy().max(), lambda v: num(v, 1))
    a1.set_title("Доля в сети, %")
    im2 = heatmap(a2, idx, DIV, 55, 145, lambda v: num(v, 0))
    a2.set_title("Индекс месяца, среднее маршрута = 100")
    fig.colorbar(im1, ax=a1, shrink=0.8, pad=0.01).outline.set_visible(False)
    fig.colorbar(im2, ax=a2, shrink=0.8, pad=0.01).outline.set_visible(False)
    F.save(fig, "03_route_month", "Структура спроса", "Сезонность по месяцам",
           "Весна и октябрь — пик, лето — спад. У каждого маршрута свой тренд, поэтому «тренд сети × доля маршрута» "
           "работает хуже, чем уровень по каждому маршруту отдельно.")


def fig_weekday_hour(F: Figures, g, c):
    recent = g[g["date"] >= SPLIT_DATE].merge(c[["daytype", "dow"]], left_on="date", right_index=True)
    recent = recent[recent["daytype"] != "Праздник"]
    order = recent.groupby("route")["boardings"].sum().sort_values(ascending=False).index
    ncol, nrow = 3, math.ceil(len(order) / 3)
    fig = F.new(14, 2.3 * nrow + 0.8, "Неделя × час: средние посадки (сентябрь–октябрь, без праздников)",
                "Каждый маршрут в своей шкале: тёмнее — больше посадок. Подписан пиковый час.")
    axes = fig.subplots(nrow, ncol).ravel()
    for ax, r in zip(axes, order):
        t = recent[recent["route"] == r].pivot_table(index="dow", columns="hour", values="boardings", aggfunc="mean")
        t = t.reindex(index=range(7), columns=range(24))
        im = ax.imshow(t.to_numpy(float), aspect="auto", cmap=SEQ, interpolation="nearest")
        ax.set_yticks(range(7), DOW_RU)
        ax.set_xticks([0, 6, 12, 18, 23], ["0", "6", "12", "18", "23"])
        ax.grid(False)
        ax.tick_params(length=0)
        for s in ax.spines.values():
            s.set_visible(False)
        i, j = np.unravel_index(np.nanargmax(t.to_numpy(float)), t.shape)
        ax.text(j, i, num(t.iloc[i, j], 0), ha="center", va="center", fontsize=7,
                color=ink_for(im.cmap(im.norm(t.iloc[i, j]))), weight="semibold")
        ax.set_title(f"№{r}")
    for ax in axes[len(order):]:
        ax.set_visible(False)
    F.save(fig, "04_weekday_hour", "Структура спроса", "Неделя × час",
           "Два пика в будни (утро 7–9, вечер 16–19), плоский дневной профиль в выходные. "
           "У маршрута №50 выходные в сен–окт почти пустые — режим закрытия.")


def fig_daytype_profiles(F: Figures, g, c):
    d = g.merge(c[["daytype"]], left_on="date", right_index=True)
    # доля часа = Σ посадок часа / Σ посадок за сутки этого типа (почти пустые дни закрытий не искажают форму)
    num_ = d.groupby(["route", "daytype", "hour"])["boardings"].sum()
    prof = num_ / num_.groupby(["route", "daytype"]).transform("sum") * 100
    order = g.groupby("route")["boardings"].sum().sort_values(ascending=False).index
    ncol, nrow = 3, math.ceil(len(order) / 3)
    fig = F.new(14, 2.4 * nrow + 0.8, "Суточный профиль по типам дня: доля часа в сутках, %",
                "Форма суток зависит от типа дня сильнее, чем от маршрута. Праздник ≈ воскресенье.", legend_rows=1)
    axes = fig.subplots(nrow, ncol, sharex=True, sharey=True).ravel()
    for ax, r in zip(axes, order):
        for dt in DAYTYPES:
            if (r, dt) in prof.index.droplevel(2):
                s = prof.loc[(r, dt)]
                ax.plot(s.index, s.values, color=DAYTYPE_COLOR[dt], lw=1.4)
        ax.set_title(f"№{r}")
        ax.set_xticks([0, 6, 12, 18, 23])
        ax.set_xlim(0, 23)
        ax.yaxis.set_major_formatter(PercentFormatter(decimals=0))
    for ax in axes[len(order):]:
        ax.set_visible(False)
    F.legend(fig, [line_key(DAYTYPE_COLOR[d], 2, d) for d in DAYTYPES], ncol=5)
    F.save(fig, "05_daytype_profiles", "Структура спроса", "Профили суток по типам дня",
           "Базовый признак модели — профиль «маршрут × тип дня × час». Для праздника разумно брать среднее Сб и Вс.")


def fig_weekday_index(F: Figures, daily, c, S):
    d = daily.copy()
    d["Сеть"] = daily.sum(axis=1)
    cc = c.reindex(d.index)
    week = d.index.to_period("W-SUN")
    bad_weeks = list(set(week[cc["daytype"].eq("Праздник").to_numpy()]))
    keep = ~week.isin(bad_weeks)
    d, cc, week = d[keep], cc[keep], week[keep]
    rows = {}
    for r in d.columns:
        s = d[r]
        base = s[cc["dow"].isin([1, 2, 3])].groupby(week[cc["dow"].isin([1, 2, 3]).to_numpy()]).mean()
        ratio = s / base.reindex(week).to_numpy()
        ratio = ratio.replace([np.inf, -np.inf], np.nan)
        rows[r] = ratio.groupby(cc["dow"].to_numpy()).median()
    t = pd.DataFrame(rows).T
    t.columns = DOW_RU
    t.index = [f"№{r}" if r != "Сеть" else "Сеть" for r in t.index]
    S["weekday_index_network"] = {k: round(float(v), 3) for k, v in t.loc["Сеть"].items()}
    fig = F.new(9, 5.6, "Индекс дня недели к среднему Вт–Чт той же недели",
                "Медиана по неделям без праздников. 1,00 = обычный будний день.")
    ax = fig.subplots()
    im = heatmap(ax, t, DIV, 0.4, 1.6, lambda v: num(v, 2), fontsize=8.5)
    ax.axhline(len(t) - 1.5, color=SURFACE, lw=3)
    fig.colorbar(im, ax=ax, shrink=0.8, pad=0.01).outline.set_visible(False)
    F.save(fig, "06_weekday_index", "Структура спроса", "Индекс дня недели",
           "Понедельник чуть ниже Вт–Чт, пятница — около 1, выходные сильно ниже и различаются по маршрутам: "
           "коэффициент выходного надо считать по каждому маршруту.")


def fig_calendar(F: Figures, daily, c, ratio, S):
    order = daily.mean().sort_values(ascending=False).index
    start = daily.index.min() - pd.Timedelta(days=daily.index.min().dayofweek)
    nweeks = (daily.index.max() - start).days // 7 + 1
    fig = F.new(14, 1.15 * len(order) + 1.6, "Календарь суточных посадок по маршрутам",
                "Цвет — доля от 95-го перцентиля маршрута. Кружок — аномально низкий день (< 60 % от медианы того же "
                "типа дня в окне ±2 недели): сбой, ремонт или закрытие.", legend_rows=1)
    axes = fig.subplots(len(order), 1, sharex=True)
    anomalies = []
    for ax, r in zip(axes, order):
        s = daily[r]
        arr = np.full((7, nweeks), np.nan)
        wk = ((s.index - start).days // 7).to_numpy()
        dw = s.index.dayofweek.to_numpy()
        arr[dw, wk] = s.to_numpy() / np.percentile(s, 95)
        im = ax.imshow(arr, aspect="auto", cmap=SEQ, vmin=0, vmax=1.1, interpolation="nearest")
        low = ratio[r] < 0.6
        ax.scatter(wk[low.to_numpy()], dw[low.to_numpy()], s=16, facecolor="none", edgecolor=CRITICAL, lw=1.2)
        for d in ratio.index[low.to_numpy()]:
            anomalies.append(dict(route=int(r), date=f"{d:%Y-%m-%d}", boardings=int(s[d]),
                                  ratio=round(float(ratio.at[d, r]), 2)))
        ax.set_yticks([0, 3, 6], ["Пн", "Чт", "Вс"])
        ax.set_ylabel(f"№{r}", rotation=0, ha="right", va="center", fontsize=10, color=INK, weight="semibold")
        ax.grid(False)
        ax.tick_params(length=0)
        for sp in ax.spines.values():
            sp.set_visible(False)
    firsts = pd.date_range(daily.index.min(), daily.index.max(), freq="MS")
    axes[-1].set_xticks([(d - start).days // 7 for d in firsts], [MONTHS_RU[d.month - 1] for d in firsts])
    cb = fig.colorbar(im, ax=axes, shrink=0.35, pad=0.01, location="right")
    cb.outline.set_visible(False)
    S["anomalies_low"] = sorted(anomalies, key=lambda a: a["ratio"])[:40]
    F.legend(fig, [Line2D([0], [0], marker="o", lw=0, markerfacecolor="none", markeredgecolor=CRITICAL,
                          markersize=6, label="аномально низкий день")], ncol=1)
    F.save(fig, "07_calendar", "Режимы и аномалии", "Календарь маршрутов",
           "Видно смену режимов: закрытые выходные (№50 с сентября, №7 летом), единичные сбойные дни. "
           "Такие дни нужно исключать при оценке уровня (робастная медиана / фильтр Хампеля).")


def fig_weekend_ratio(F: Figures, daily, c, S):
    cc = c.reindex(daily.index)
    ok = cc["daytype"] != "Праздник"
    wk = daily.index.to_period("W-SUN")
    we = daily[ok & (cc["dow"] >= 5)].groupby(wk[(ok & (cc["dow"] >= 5)).to_numpy()]).mean()
    wd = daily[ok & (cc["dow"] < 5)].groupby(wk[(ok & (cc["dow"] < 5)).to_numpy()]).mean()
    rr = (we / wd).dropna(how="all")
    rr.index = rr.index.start_time
    order = daily.mean().sort_values(ascending=False).index
    ncol, nrow = 3, math.ceil(len(order) / 3)
    fig = F.new(14, 2.2 * nrow + 0.8, "Режим выходных: средние Сб–Вс / средние Пн–Пт по неделям",
                "Синяя линия — маршрут, серые — остальные для контекста. Резкий обвал = закрытие по выходным (ремонт путей).")
    axes = fig.subplots(nrow, ncol, sharex=True, sharey=True).ravel()
    shifts = {}
    for ax, r in zip(axes, order):
        for o in order:
            if o != r:
                ax.plot(rr.index, rr[o], color=CONTEXT, lw=0.8)
        ax.plot(rr.index, rr[r], color=SERIES[0], lw=1.8)
        last = rr[r].dropna()
        ax.scatter([last.index[-1]], [last.iloc[-1]], s=18, color=SERIES[0], edgecolor=SURFACE, lw=1.5, zorder=3)
        ax.annotate(num(last.iloc[-1], 2), (last.index[-1], last.iloc[-1]), xytext=(4, 0),
                    textcoords="offset points", va="center", fontsize=8, color=INK)
        split_line(ax, label=False)
        ax.set_title(f"№{r}")
        ax.set_ylim(0, 1.05)
        month_axis(ax)
        recent, before = last[last.index >= "2025-09-29"].mean(), last[(last.index >= "2025-03-01") & (last.index < "2025-06-01")].median()
        shifts[int(r)] = dict(last_4w=round(float(recent), 3), spring_median=round(float(before), 3))
    for ax in axes[len(order):]:
        ax.set_visible(False)
    S["weekend_ratio"] = shifts
    F.save(fig, "08_weekend_ratio", "Режимы и аномалии", "Режим выходных",
           "Коэффициент выходных меняется во времени у отдельных маршрутов — это «режим», его нужно брать из "
           "последних недель и из новостей Мосгортранса, а не из среднего за год.")


def fig_holidays(F: Figures, daily, c, S):
    net = daily.sum(axis=1)
    cc = c.reindex(net.index)
    hol = cc.index[(cc["daytype"] == "Праздник") | cc["is_official"]]
    normal = cc["daytype"] != "Праздник"
    rows = []
    for d in hol:
        win = (net.index >= d - pd.Timedelta(days=21)) & (net.index <= d + pd.Timedelta(days=21)) & normal.to_numpy()
        sun = net[win & (cc["dow"] == 6).to_numpy()].median()
        sat = net[win & (cc["dow"] == 5).to_numpy()].median()
        wd = net[win & (cc["dow"].isin([1, 2, 3])).to_numpy()].median()
        rows.append(dict(date=d, sun=net[d] / sun, sat=net[d] / sat, wd=net[d] / wd))
    h = pd.DataFrame(rows).set_index("date")
    per_route = {}
    for r in daily.columns:
        vals = []
        for d in hol:
            if cc.at[d, "dow"] >= 5:
                continue
            win = (daily.index >= d - pd.Timedelta(days=21)) & (daily.index <= d + pd.Timedelta(days=21)) & normal.to_numpy()
            sun = daily.loc[win & (cc["dow"] == 6).to_numpy(), r].median()
            if sun > 0:
                vals.append(daily.at[d, r] / sun)
        per_route[r] = np.median(vals) if vals else np.nan
    pr = pd.Series(per_route).sort_values()
    S["holidays"] = dict(network_to_sunday_median=round(float(h["sun"].median()), 3),
                         network_to_saturday_median=round(float(h["sat"].median()), 3),
                         per_route_to_sunday={int(k): round(float(v), 3) for k, v in pr.items()})
    fig = F.new(14, 6.4, "Праздники: посадки к обычным выходным рядом (±3 недели)",
                f"Сеть: праздничный день ≈ {num(h['sun'].median(), 2)} × воскресенья и "
                f"≈ {num(h['sat'].median(), 2)} × субботы (медианы). Линия 1,0 — уровень обычного выходного.", legend_rows=1)
    gs = fig.add_gridspec(1, 2, width_ratios=[1.6, 1])
    a1, a2 = fig.add_subplot(gs[0]), fig.add_subplot(gs[1])
    y = np.arange(len(h))
    bh = 0.36
    a1.barh(y - bh / 2, h["sun"], height=bh, color=SERIES[0], **BAR)
    a1.barh(y + bh / 2, h["sat"], height=bh, color=SERIES[1], **BAR)
    a1.axvline(1, color=INK2, lw=0.9)
    a1.set_yticks(y, [f"{d:%d.%m} {DOW_RU[d.dayofweek]}" for d in h.index])
    a1.invert_yaxis()
    a1.grid(axis="y", visible=False)
    a1.set_title("Сеть, каждый праздничный день")
    a2.barh(np.arange(len(pr)), pr.values, height=0.4, color=SERIES[0])
    a2.axvline(1, color=INK2, lw=0.9)
    a2.set_xlim(0, 1.15)
    a2.set_yticks(np.arange(len(pr)), [f"№{r}" for r in pr.index])
    a2.grid(axis="y", visible=False)
    for i, v in enumerate(pr.values):
        a2.text(v + 0.01, i, num(v, 2), va="center", fontsize=8, color=INK)
    a2.set_title("Маршруты: нерабочий будний / воскресенье (медиана)")
    F.legend(fig, [Patch(color=SERIES[0], label="к воскресенью"), Patch(color=SERIES[1], label="к субботе")], ncol=2)
    F.save(fig, "09_holidays", "Внешние факторы", "Праздники",
           "Нерабочие будни ведут себя как выходные; январские каникулы ниже обычного воскресенья. "
           "Для 3–4 ноября и 31 декабря — профиль «среднее Сб и Вс» и поправочный множитель.")


def fig_level_trend(F: Figures, daily):
    roll = daily.rolling(28, center=True).mean()
    base = daily[(daily.index >= "2025-02-01") & (daily.index < "2025-03-01")].mean()
    idx = roll / base * 100
    order = daily.mean().sort_values(ascending=False).index
    ncol, nrow = 3, math.ceil(len(order) / 3)
    fig = F.new(14, 2.2 * nrow + 0.8, "Уровень маршрутов: скользящее среднее 28 дней, февраль = 100",
                "Синяя линия — маршрут, серые — остальные. Метка — значение на конец окна (середина октября).")
    axes = fig.subplots(nrow, ncol, sharex=True, sharey=True).ravel()
    for ax, r in zip(axes, order):
        for o in order:
            if o != r:
                ax.plot(idx.index, idx[o], color=CONTEXT, lw=0.8)
        ax.axhline(100, color=AXIS, lw=0.8)
        ax.plot(idx.index, idx[r], color=SERIES[0], lw=1.8)
        last = idx[r].dropna()
        ax.scatter([last.index[-1]], [last.iloc[-1]], s=18, color=SERIES[0], edgecolor=SURFACE, lw=1.5, zorder=3)
        ax.annotate(num(last.iloc[-1], 0), (last.index[-1], last.iloc[-1]), xytext=(4, 0),
                    textcoords="offset points", va="center", fontsize=8, color=INK)
        ax.set_title(f"№{r}")
        month_axis(ax)
    for ax in axes[len(order):]:
        ax.set_visible(False)
    F.save(fig, "10_level_trend", "Структура спроса", "Уровень и тренд",
           "Тренды у маршрутов разные (у одних осенний рост заметно выше февраля, у других нет). "
           "Уровень надо оценивать по маршруту на последних неделях — деревья его не экстраполируют.")


def fig_hour_weight(F: Figures, g, c, S):
    by_h = g.groupby("hour")["boardings"].sum()
    share = by_h / by_h.sum() * 100
    top = share.sort_values(ascending=False)
    top8 = top.iloc[:8]
    S["hour_share_top8"] = dict(hours=sorted(int(h) for h in top8.index), share=round(float(top8.sum()), 1))
    d = g[(g["date"] >= SPLIT_DATE)].merge(c[["daytype"]], left_on="date", right_index=True)
    d = d[d["daytype"] == "Пн–Чт"]
    t = d.groupby(["route", "hour"])["boardings"].agg(["std", "mean"])
    cv = (t["std"] / t["mean"]).unstack("hour").reindex(columns=range(5, 24))
    order = g.groupby("route")["boardings"].sum().sort_values(ascending=False).index
    cv = cv.loc[order]
    cv.index = [f"№{r}" for r in cv.index]
    fig = F.new(14, 5.6, "Где сосредоточена метрика и где прогноз труднее",
                f"WAPE взвешивает ячейки объёмом: 8 самых загруженных часов дают {num(top8.sum(), 0)} % всех посадок. "
                "Справа — день-к-дню разброс (CV) в будни Пн–Чт сен–окт.", legend_rows=1)
    gs = fig.add_gridspec(1, 2, width_ratios=[1, 1.5])
    a1, a2 = fig.add_subplot(gs[0]), fig.add_subplot(gs[1])
    colors = [SERIES[0] if h in top8.index else BLUE[200] for h in share.index]
    a1.bar(share.index, share.values, width=0.72, color=colors, **BAR)
    a1.yaxis.set_major_formatter(PercentFormatter(decimals=0))
    a1.set_xticks(range(0, 24, 2))
    a1.set_xlabel("час")
    a1.set_title("Доля часа в сумме посадок (янв–окт)")
    a1.grid(axis="x", visible=False)
    im = heatmap(a2, cv, SEQ, 0, min(0.6, float(np.nanmax(cv.to_numpy()))), lambda v: num(v * 100, 0), fontsize=7)
    a2.set_title("Коэффициент вариации по дням, % (час × маршрут)")
    a2.set_xlabel("час")
    fig.colorbar(im, ax=a2, shrink=0.8, pad=0.01).outline.set_visible(False)
    F.legend(fig, [Patch(color=SERIES[0], label="8 самых загруженных часов"), Patch(color=BLUE[200], label="остальные")], ncol=2)
    F.save(fig, "11_hour_weight", "Структура спроса", "Вес часов в метрике",
           "Ошибка в пиковые часы стоит дороже всего; ночные часы почти не влияют на WAPE. "
           "Высокий CV ранним утром и поздним вечером — «пачки» трамваев и сдвиги выпуска.")


# ─────────────────────────────── внешние факторы ───────────────────────────────
def fig_forecast_calendar(F: Figures, cal: pd.DataFrame, S):
    c = cal[(cal["date"] >= FC_START) & (cal["date"] <= FC_END)].copy()

    def kind(r):
        if r.work_weekend:
            return "Рабочая суббота (сокр.)"
        if r.off_weekday or (r.is_official and r.dow < 5):
            return "Праздник / нерабочий"
        if r.is_off:
            return "Выходной"
        if r.is_short:
            return "Сокращённый"
        return "Рабочий"

    c["kind"] = [kind(r) for r in c.itertuples()]
    style = {"Рабочий": ("#f0efec", "Р"), "Выходной": (BLUE[100], "В"), "Праздник / нерабочий": ("#f6c3ad", "П"),
             "Рабочая суббота (сокр.)": ("#bfe8d7", "РС"), "Сокращённый": ("#fbe3a6", "С")}
    S["forecast_calendar"] = {k: [f"{d:%d.%m}" for d in c.loc[c["kind"] == k, "date"]]
                              for k in style if k not in ("Рабочий", "Выходной")}
    S["forecast_calendar"]["counts"] = c["kind"].value_counts().to_dict()
    fig = F.new(13, 5.6, "Период прогноза: ноябрь–декабрь 2025",
                "Производственный календарь (постановление № 1335): 1.11 — рабочая сокращённая суббота, 3–4.11 и 31.12 — "
                "нерабочие. Черта внизу клетки — школьные каникулы (допущение).", legend_rows=2)
    axes = fig.subplots(1, 2)
    for ax, month in zip(axes, [11, 12]):
        m = c[c["date"].dt.month == month]
        first_dow = m["date"].iloc[0].dayofweek
        for r in m.itertuples():
            pos = first_dow + r.date.day - 1
            row, col = pos // 7, pos % 7
            color, code = style[r.kind]
            ax.add_patch(Rectangle((col + 0.04, -row - 0.96), 0.92, 0.92, color=color, lw=0))
            ax.text(col + 0.12, -row - 0.14, str(r.date.day), ha="left", va="top", fontsize=11, color=INK, weight="semibold")
            ax.text(col + 0.88, -row - 0.86, code, ha="right", va="bottom", fontsize=8, color=INK2)
            if r.school:
                ax.add_patch(Rectangle((col + 0.12, -row - 0.9), 0.4, 0.07, color=INK2, lw=0))
        ax.set_xlim(0, 7)
        ax.set_ylim(-6.05, 0.5)
        for j, dn in enumerate(DOW_RU):
            ax.text(j + 0.5, 0.2, dn, ha="center", va="center", fontsize=9, color=INK2)
        ax.set_title(f"{MONTHS_RU_FULL[month - 1]} 2025", fontsize=11)
        ax.axis("off")
    F.legend(fig, [Patch(color=v[0], label=f"{v[1]} — {k}") for k, v in style.items()] +
             [Patch(color=INK2, label="школьные каникулы")], ncol=3)
    F.save(fig, "12_forecast_calendar", "Внешние факторы", "Календарь периода прогноза",
           "Особые дни ноября–декабря, для которых нужны отдельные множители: 1.11, 3–4.11, 31.12 "
           "и предновогодняя неделя.")


def daily_residual(daily, c):
    net = daily.sum(axis=1)
    cc = c.reindex(net.index)
    usable = (cc["daytype"] != "Праздник").to_numpy()
    exp = expected_same_type(net.to_numpy(float), cc["daytype"].to_numpy(), usable)
    return pd.Series(net.to_numpy() / exp - 1, index=net.index) * 100, usable


def ols(x, y):
    x, y = np.asarray(x, float), np.asarray(y, float)
    m = np.isfinite(x) & np.isfinite(y)
    x, y = x[m], y[m]
    X = np.column_stack([np.ones_like(x), x])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    s2 = resid @ resid / max(len(x) - 2, 1)
    se = np.sqrt(np.diag(s2 * np.linalg.inv(X.T @ X)))
    return beta[0], beta[1], se[1], beta[1] / se[1], len(x)


def fig_weather_timeline(F: Figures, w, daily, c):
    res, usable = daily_residual(daily, c)
    fig = F.new(14, 8.6, "Погода в Москве в 2025 году и отклонение спроса сети",
                "Open-Meteo archive (ERA5/IFS), центр Москвы. Нижняя панель — посадки сети к медиане того же типа "
                "дня в окне ±2 недели, %. Ноябрь–декабрь — факт погоды для справки (в момент прогноза неизвестен).")
    axes = fig.subplots(4, 1, sharex=True, height_ratios=[1.1, 1, 0.8, 1])
    for ax in axes:
        ax.axvspan(FC_START, FC_END + pd.Timedelta(days=1), color=NEUTRAL, lw=0, zorder=0)
    a = axes[0]
    a.fill_between(w["date"], w["t_min"], w["t_max"], color=SERIES[0], alpha=0.12, lw=0)
    a.plot(w["date"], w["t_mean"], color=SERIES[0], lw=1.3)
    a.axhline(0, color=AXIS, lw=0.8)
    a.set_ylabel("°C")
    a.set_title("Температура: средняя за сутки и диапазон мин–макс")
    a = axes[1]
    a.bar(w["date"], w["precip"], width=0.8, color=SERIES[0])
    a.set_ylabel("мм")
    a.set_title("Осадки за сутки")
    a = axes[2]
    a.fill_between(w["date"], 0, w["snow_depth"], color=SERIES[0], alpha=0.12, lw=0)
    a.plot(w["date"], w["snow_depth"], color=SERIES[0], lw=1.3)
    a.set_ylabel("см")
    a.set_title("Высота снежного покрова")
    a = axes[3]
    r = res[usable]
    a.bar(r.index, r.values, width=0.8, color=np.where(r.values >= 0, BLUE[400], SERIES[7]))
    a.axhline(0, color=INK2, lw=0.8)
    a.set_ylim(-40, 40)
    a.yaxis.set_major_formatter(PercentFormatter(decimals=0))
    a.set_title("Отклонение посадок сети от ожидаемого (праздники исключены)")
    month_axis(a)
    axes[0].text(FC_START + pd.Timedelta(days=30), axes[0].get_ylim()[1], "период прогноза", ha="center",
                 va="top", fontsize=8.5, color=INK2)
    F.save(fig, "13_weather_timeline", "Внешние факторы", "Погода: хронология",
           "Сопоставление погоды и отклонений спроса по дням. Дни с сильными осадками чаще совпадают "
           "с отрицательными отклонениями.")


def fig_weather_effect(F: Figures, w, daily, c, S):
    res, usable = daily_residual(daily, c)
    ww = w.set_index("date").reindex(res.index)
    t_anom = ww["t_mean"] - ww["t_mean"].rolling(29, center=True, min_periods=10).mean()
    ok = usable & (res.abs() < 40).to_numpy()
    df = pd.DataFrame({"res": res, "precip": ww["precip"], "snow": ww["snowfall"], "tanom": t_anom})[ok]
    fig = F.new(14, 8.8, "Эффект погоды на спрос: отклонение посадок vs осадки, снег, температура",
                "Точка — день (сеть, праздники и аварийные дни исключены). Круги — среднее по корзинам ± 95 % ДИ, "
                "оранжевая линия — МНК. Внизу — наклон по осадкам для каждого маршрута.")
    gs = fig.add_gridspec(2, 3, height_ratios=[1.2, 1])
    specs = [("precip", "Осадки за сутки, мм", [-0.01, 0.5, 2, 5, 10, 60], "мм"),
             ("snow", "Снегопад за сутки, см", [-0.01, 0.2, 1, 3, 30], "см"),
             ("tanom", "Аномалия температуры, °C (к 29-дневному среднему)", [-30, -6, -3, 0, 3, 6, 30], "°C")]
    eff = {}
    for k, (col, xlabel, bins, unit) in enumerate(specs):
        ax = fig.add_subplot(gs[0, k])
        x, y = df[col], df["res"]
        ax.scatter(x, y, s=12, color=BLUE[250], alpha=0.55, lw=0)
        b0, b1, se, t, n = ols(x, y)
        xs = np.linspace(np.nanmin(x), np.nanmax(x), 50)
        ax.plot(xs, b0 + b1 * xs, color=SERIES[1], lw=1.8)
        cut = pd.cut(x, bins)
        grp = y.groupby(cut, observed=True).agg(["mean", "std", "count"])
        mids = x.groupby(cut, observed=True).mean()
        keep = grp["count"] >= 5                              # редкие корзины дают бессмысленный ДИ
        grp, mids = grp[keep], mids[keep]
        ci = 1.96 * grp["std"] / np.sqrt(grp["count"].clip(lower=1))
        ax.errorbar(mids.to_numpy(), grp["mean"].to_numpy(), yerr=ci.to_numpy(), fmt="o", color=SERIES[0], ms=6, mec=SURFACE, mew=1.5,
                    elinewidth=1.2, capsize=0, zorder=4)
        ax.axhline(0, color=INK2, lw=0.8)
        ax.set_xlabel(xlabel)
        ax.set_ylim(-35, 25)
        ax.yaxis.set_major_formatter(PercentFormatter(decimals=0))
        ax.set_title(f"{num(b1, 2)} % на {unit}  (t = {num(t, 1)}, n = {n})")
        eff[col] = dict(slope_pct_per_unit=round(float(b1), 3), t=round(float(t), 2), n=int(n))
    S["weather_effect_network"] = eff
    # по маршрутам: наклон по осадкам
    rows = []
    cc = c.reindex(daily.index)
    use = (cc["daytype"] != "Праздник").to_numpy()
    for r in daily.columns:
        v = daily[r].to_numpy(float)
        exp = expected_same_type(v, cc["daytype"].to_numpy(), use & (v > 0))
        rr = (v / exp - 1) * 100
        m = use & np.isfinite(rr) & (np.abs(rr) < 40)
        _, b1, se, t, n = ols(ww["precip"].to_numpy()[m], rr[m])
        rows.append((r, b1, se, t))
    pr = pd.DataFrame(rows, columns=["route", "b", "se", "t"]).sort_values("b")
    S["weather_effect_routes_precip"] = {int(r.route): dict(slope=round(float(r.b), 3), t=round(float(r.t), 2))
                                         for r in pr.itertuples()}
    ax = fig.add_subplot(gs[1, :])
    xpos = np.arange(len(pr))
    ax.bar(xpos, pr["b"], width=0.32, color=SERIES[0])
    ax.errorbar(xpos, pr["b"], yerr=1.96 * pr["se"], fmt="none", ecolor=INK2, elinewidth=1.2, capsize=0)
    ax.axhline(0, color=INK2, lw=0.8)
    ax.set_xticks(xpos, [f"№{r}" for r in pr["route"]])
    ax.set_ylabel("% посадок на 1 мм осадков")
    ax.grid(axis="x", visible=False)
    ax.set_title("Наклон по осадкам по маршрутам (± 95 % ДИ)")
    F.save(fig, "14_weather_effect", "Внешние факторы", "Погода: оценка эффекта",
           "Количественное подтверждение эффекта погоды (для критерия «внешние источники» и ползунка k_weather). "
           "Эффект второго порядка по сравнению с календарём и режимами.")


def fig_school(F: Figures, daily, c, S):
    cc = c.reindex(daily.index)
    wd = (cc["daytype"].isin(["Пн–Чт", "Пт"])).to_numpy()
    windows = [("весенние", ("2025-03-24", "2025-03-28"), ("2025-03-10", "2025-03-21")),
               ("осенние", ("2025-10-27", "2025-10-31"), ("2025-10-13", "2025-10-24"))]
    res = {}
    d = daily.copy()
    d["Сеть"] = daily.sum(axis=1)
    for name, (a, b), (pa_, pb) in windows:
        cur = d[(d.index >= a) & (d.index <= b) & wd].mean()
        prev = d[(d.index >= pa_) & (d.index <= pb) & wd].mean()
        res[name] = (cur / prev - 1) * 100
    t = pd.DataFrame(res)
    order = ["Сеть"] + list(daily.mean().sort_values(ascending=False).index)
    t = t.loc[order]
    S["school_holidays_effect_pct"] = {str(k): {n: round(float(v), 2) for n, v in row.items()} for k, row in t.iterrows()}
    fig = F.new(13, 5.2, "Школьные каникулы: будни каникул к двум предыдущим неделям, %",
                "Весенние: 24–28.03 vs 10–21.03; осенние: 27–31.10 vs 13–24.10. Даты — допущение (четвертная система); "
                "эффект смешан с сезонным трендом.", legend_rows=1)
    ax = fig.subplots()
    x = np.arange(len(t))
    bw = 0.24
    ax.bar(x - bw / 2, t["весенние"], width=bw, color=SERIES[0], **BAR)
    ax.bar(x + bw / 2, t["осенние"], width=bw, color=SERIES[1], **BAR)
    ax.axhline(0, color=INK2, lw=0.8)
    ax.set_xticks(x, [f"№{r}" if r != "Сеть" else "Сеть" for r in t.index])
    ax.yaxis.set_major_formatter(PercentFormatter(decimals=0))
    ax.grid(axis="x", visible=False)
    for xpos, v in zip((-bw / 2, bw / 2), t.loc["Сеть"]):        # подписываем только сеть
        ax.text(xpos, v + (0.3 if v >= 0 else -0.3), num(v, 1) + " %",
                ha="center", va="bottom" if v >= 0 else "top", fontsize=8, color=INK)
    F.legend(fig, [Patch(color=SERIES[0], label="весенние"), Patch(color=SERIES[1], label="осенние")], ncol=2)
    F.save(fig, "15_school_holidays", "Внешние факторы", "Школьные каникулы",
           "Флаг каникул — малый, но измеримый фактор календаря; итоговое решение о признаке — по фолдам.")


# ─────────────────────────────── геопривязка ───────────────────────────────
def fig_stops_map(F: Figures, ref_routes, stops, daily, g, c, out: Path, S):
    if stops is None:
        log("  справочники не найдены, карта пропущена")
        return
    in_data = [r for r in sorted(stops["route_short_name"].unique()) if r in ALL_ROUTES]
    colors = {r: SERIES[i] for i, r in enumerate(in_data[:8])}
    names = {}
    if ref_routes is not None:
        names = dict(zip(ref_routes["route_short_name"], ref_routes["route_long_name"]))
    fig = F.new(12, 11, "Трамвайные маршруты из справочника: остановки и трассы",
                f"Цветом — маршруты, которые есть в сабмите ({', '.join('№' + str(r) for r in in_data)}); серым — "
                "маршруты справочника без данных валидаций. Трасса — направление 0 по порядку остановок.")
    ax = fig.subplots()
    lat0 = math.radians(stops["stop_lat"].mean())
    for r in sorted(stops["route_short_name"].unique()):
        s = stops[(stops["route_short_name"] == r) & (stops["direction_id"] == 0)].sort_values("stop_sequence")
        col = colors.get(r, CONTEXT)
        z = 3 if r in colors else 2
        ax.plot(s["stop_lon"], s["stop_lat"], color=col, lw=2.2 if r in colors else 1.4, zorder=z)
        allst = stops[stops["route_short_name"] == r]
        ax.scatter(allst["stop_lon"], allst["stop_lat"], s=10, color=col, edgecolor=SURFACE, lw=0.8, zorder=z + 1)
        if len(s):
            mid = s.iloc[len(s) // 2]
            ax.annotate(f"№{r}", (mid["stop_lon"], mid["stop_lat"]), xytext=(5, 5), textcoords="offset points",
                        fontsize=9.5 if r in colors else 8.5, color=INK if r in colors else MUTED,
                        weight="semibold" if r in colors else "normal", zorder=6)
    ax.set_aspect(1 / math.cos(lat0))
    ax.set_xlabel("долгота")
    ax.set_ylabel("широта")
    handles = [line_key(colors[r], 2.2, f"№{r} · {str(names.get(r, ''))[:52]}") for r in in_data]
    handles.append(line_key(CONTEXT, 1.6, "нет в данных валидаций"))
    ax.legend(handles=handles, loc="lower left", fontsize=8.5, frameon=True, facecolor=SURFACE, edgecolor=GRID)
    F.save(fig, "16_stops_map", "Геопривязка", "Карта остановок",
           "Пересечение справочника с данными — только маршруты 1, 5, 7, 11, 12; остановочная детализация "
           "возможна через доли посадок по остановкам (bottom-up). Интерактивная версия — map_stops.html.")
    S["reference"] = dict(route_date_start={str(k): str(v) for k, v in zip(ref_routes["route_short_name"],
                                                                        ref_routes["route_date_start"])}
                          if ref_routes is not None else {},
                          routes_in_reference=[int(r) for r in sorted(stops["route_short_name"].unique())],
                          overlap_with_data=[int(r) for r in in_data], n_stops=int(stops["stop_id"].nunique()))
    wd = g[g["date"] >= SPLIT_DATE].merge(c[["daytype"]], left_on="date", right_index=True)
    prof = wd[wd["daytype"] == "Пн–Чт"].groupby(["route", "hour"])["boardings"].mean().unstack("hour")
    write_leaflet_map(stops, names, colors, daily, prof, out / "map_stops.html")


def sparkline_svg(values, w=160, h=36) -> str:
    v = np.asarray(values, float)
    if not np.isfinite(v).any() or v.max() <= 0:
        return ""
    xs = np.linspace(2, w - 2, len(v))
    ys = h - 2 - (v / v.max()) * (h - 6)
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}"><polyline points="{pts}" fill="none" '
            f'stroke="#2a78d6" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/></svg>')


def write_leaflet_map(stops, names, colors, daily, prof, path: Path) -> None:
    feats = []
    for r in sorted(stops["route_short_name"].unique()):
        s0 = stops[(stops["route_short_name"] == r) & (stops["direction_id"] == 0)].sort_values("stop_sequence")
        sts = stops[stops["route_short_name"] == r].drop_duplicates("stop_id")
        info = f"<b>№{r}</b> · {html.escape(str(names.get(r, '')))}"
        spark = ""
        if r in daily.columns:
            s = daily[r]
            info += f"<br>в среднем {compact(s.mean())} посадок/сутки (янв–окт), окт: {compact(s[s.index.month == 10].mean())}"
        else:
            info += "<br>нет разметки посадок в labels"
        if r in prof.index:
            spark = "<br>будни Пн–Чт, сен–окт, посадки по часам 0–23:<br>" + sparkline_svg(prof.loc[r].to_numpy())
        feats.append(dict(route=int(r), color=colors.get(r, "#a3a29b"), active=r in colors, info=info, spark=spark,
                          line=s0[["stop_lat", "stop_lon"]].round(6).values.tolist(),
                          stops=[[round(a, 6), round(b, 6), html.escape(str(n))] for a, b, n in
                                 sts[["stop_lat", "stop_lon", "stop_name"]].itertuples(index=False)]))
    page = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Трамвайные маршруты</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<style>html,body,#map{height:100%;margin:0}body{font:14px system-ui,"Segoe UI",sans-serif}
.lg{background:#fcfcfb;padding:8px 10px;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.2);line-height:1.6}
.lg i{display:inline-block;width:18px;height:3px;border-radius:2px;margin-right:6px;vertical-align:middle}</style>
</head><body><div id="map"></div><script>
const F = __DATA__;
const map = L.map('map').setView([55.75, 37.62], 11);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 19,
  attribution: '&copy; OpenStreetMap contributors'}).addTo(map);
const all = [];
F.sort((a, b) => a.active - b.active).forEach(f => {
  L.polyline(f.line, {color: f.color, weight: f.active ? 5 : 3, opacity: f.active ? .9 : .6})
    .bindTooltip(f.info + f.spark, {sticky: true}).addTo(map);
  all.push(...f.line);
  f.stops.forEach(s => L.circleMarker([s[0], s[1]], {radius: 4, color: '#fcfcfb', weight: 2,
    fillColor: f.color, fillOpacity: 1}).bindTooltip('№' + f.route + ' · ' + s[2]).addTo(map));
});
if (all.length) map.fitBounds(all, {padding: [20, 20]});
const lg = L.control({position: 'bottomleft'});
lg.onAdd = () => { const d = L.DomUtil.create('div', 'lg');
  d.innerHTML = F.filter(f => f.active).map(f => `<div><i style="background:${f.color}"></i>№${f.route}</div>`).join('')
    + '<div><i style="background:#a3a29b"></i>нет в данных валидаций</div>'; return d; };
lg.addTo(map);
</script></body></html>"""
    path.write_text(page.replace("__DATA__", json.dumps(feats, ensure_ascii=False)), encoding="utf-8")
    log(f"  ✓ {path.name}")


# ─────────────────────────────── бэктест бейзлайна ───────────────────────────────
def profile_forecast(g: pd.DataFrame, cal: pd.DataFrame, cutoff: pd.Timestamp, horizon: int = HORIZON,
                     weeks: int = 4) -> pd.DataFrame:
    """Профиль последних `weeks` недель: маршрут × тип дня × час; праздник = среднее Сб и Вс."""
    c = cal.set_index("date")["daytype"]
    hist = g[(g["date"] < cutoff) & (g["date"] >= cutoff - pd.Timedelta(days=7 * weeks))].copy()
    hist["daytype"] = hist["date"].map(c)
    hist = hist[hist["daytype"] != "Праздник"]
    prof = hist.groupby(["route", "daytype", "hour"])["boardings"].mean().unstack("daytype")
    prof["Праздник"] = (prof["Сб"] + prof["Вс"]) / 2
    prof = prof.stack().rename("pred").reset_index()
    dates = pd.date_range(cutoff, periods=horizon)
    fut = pd.MultiIndex.from_product([sorted(g["route"].unique()), dates, range(24)],
                                     names=["route", "date", "hour"]).to_frame(index=False)
    fut["daytype"] = fut["date"].map(c)
    fut = fut.merge(prof, on=["route", "daytype", "hour"], how="left").fillna({"pred": 0})
    fut["pred"] = fut["pred"].round()
    return fut


def fig_backtest(F: Figures, g, cal, S):
    folds = [pd.Timestamp("2025-05-01"), pd.Timestamp("2025-07-01"), pd.Timestamp("2025-09-01")]
    names = {f: f"{MONTHS_RU[f.month - 1]}–{MONTHS_RU[f.month]}" for f in folds}
    scores, preds = {}, {}
    for f in folds:
        p = profile_forecast(g, cal, f)
        m = p.merge(g[["route", "date", "hour", "boardings"]], on=["route", "date", "hour"])
        preds[f] = m
        sc = {int(r): wape_score(x["boardings"], x["pred"]) for r, x in m.groupby("route")}
        sc["Сеть"] = wape_score(m["boardings"], m["pred"])
        scores[f] = sc
    t = pd.DataFrame({names[f]: scores[f] for f in folds})
    order = ["Сеть"] + [int(r) for r in g.groupby("route")["boardings"].sum().sort_values(ascending=False).index]
    t = t.loc[order]
    S["backtest_profile4w"] = {"folds": {names[f]: round(float(scores[f]["Сеть"]), 4) for f in folds},
                               "mean": round(float(t.loc["Сеть"].mean()), 4),
                               "per_route": {str(k): {c: round(float(v), 4) for c, v in row.items()} for k, row in t.iterrows()}}
    last = preds[folds[-1]]
    net = last.groupby("date")[["boardings", "pred"]].sum()
    err_h = last.assign(ae=(last["boardings"] - last["pred"]).abs()).groupby("hour")["ae"].sum()
    err_h = err_h / err_h.sum() * 100
    fig = F.new(14, 9.2, "Бэктест бейзлайна в постановке задачи: горизонт 61 день",
                f"Профиль последних 4 недель (маршрут × тип дня × час), праздник = среднее Сб и Вс. WAPE-score сети по фолдам: "
                + ", ".join(f"{names[f]} {num(scores[f]['Сеть'], 3)}" for f in folds)
                + f". Порог максимального балла — {num(WAPE_MAX_BALL, 2)}.", legend_rows=1)
    gs = fig.add_gridspec(2, 2, height_ratios=[1, 1], width_ratios=[1.5, 1])
    a1 = fig.add_subplot(gs[0, :])
    x = np.arange(len(t))
    bw = 0.2
    for k, col in enumerate(t.columns):
        a1.bar(x + (k - 1) * bw, t[col], width=bw, color=SERIES[k], **BAR)
    a1.axhline(WAPE_MAX_BALL, color=INK2, lw=1)
    a1.text(len(t) - 0.5, WAPE_MAX_BALL, f"  {num(WAPE_MAX_BALL, 2)} — максимум баллов", va="bottom", ha="right",
            fontsize=8.5, color=INK2)
    a1.set_xticks(x, [f"№{r}" if r != "Сеть" else "Сеть" for r in t.index])
    a1.set_ylim(max(0, t.min().min() - 0.1), 1)
    a1.grid(axis="x", visible=False)
    a1.set_title("WAPE-score по маршрутам и фолдам (fit до начала фолда → прогноз 61 день)")
    a2 = fig.add_subplot(gs[1, 0])
    a2.plot(net.index, net["boardings"], color=SERIES[0], lw=1.6)
    a2.plot(net.index, net["pred"], color=SERIES[1], lw=1.6)
    a2.yaxis.set_major_formatter(KFMT)
    a2.set_title(f"Сеть по дням, фолд {names[folds[-1]]}: факт vs прогноз")
    a2.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    a2.legend(handles=[line_key(SERIES[0], 2, "факт"), line_key(SERIES[1], 2, "прогноз")], loc="lower left")
    a3 = fig.add_subplot(gs[1, 1])
    a3.bar(err_h.index, err_h.values, width=0.72, color=SERIES[0], **BAR)
    a3.yaxis.set_major_formatter(PercentFormatter(decimals=0))
    a3.set_xticks(range(0, 24, 3))
    a3.grid(axis="x", visible=False)
    a3.set_title("Доля абсолютной ошибки по часам")
    F.legend(fig, [Patch(color=SERIES[k], label=f"фолд {c}") for k, c in enumerate(t.columns)], ncol=3)
    F.save(fig, "17_backtest", "Модель и сабмит", "Бэктест бейзлайна",
           "Разброс между фолдами больше разницы между моделями — решения принимать по среднему нескольких фолдов. "
           "Самые большие провалы — смена режима маршрута внутри горизонта.")


# ─────────────────────────────── проверка сабмита ───────────────────────────────
def check_submission(path: Path) -> tuple[pd.DataFrame | None, list[str]]:
    problems = []
    with open(path, encoding="utf-8-sig") as fh:
        header = fh.readline().strip()
    if header != "route;date;hour;prediction":
        problems.append(f"заголовок «{header}», ожидается «route;date;hour;prediction»")
    try:
        s = pd.read_csv(path, sep=";")
    except Exception as e:  # noqa: BLE001
        return None, [f"не читается: {e}"]
    need = {"route", "date", "hour", "prediction"}
    if not need <= set(s.columns):
        return None, problems + [f"нет колонок {need - set(s.columns)}"]
    if len(s) != len(ALL_ROUTES) * HORIZON * 24:
        problems.append(f"строк {len(s)}, ожидается {len(ALL_ROUTES) * HORIZON * 24}")
    s["prediction"] = pd.to_numeric(s["prediction"], errors="coerce")
    if s["prediction"].isna().any():
        problems.append(f"нечисловых/пустых прогнозов: {int(s['prediction'].isna().sum())}")
    if (s["prediction"] < 0).any():
        problems.append(f"отрицательных прогнозов: {int((s['prediction'] < 0).sum())}")
    s["date"] = pd.to_datetime(s["date"], errors="coerce")
    grid = pd.MultiIndex.from_product([ALL_ROUTES, pd.date_range(FC_START, FC_END), range(24)])
    keys = pd.MultiIndex.from_frame(s[["route", "date", "hour"]])
    if keys.duplicated().any():
        problems.append(f"дубликатов ключей: {int(keys.duplicated().sum())}")
    miss = grid.difference(keys)
    extra = keys.difference(grid)
    def key(k):
        r, d, h = k
        return f"{r};{pd.Timestamp(d):%Y-%m-%d};{h}" if pd.notna(d) else f"{r};<битая дата>;{h}"

    if len(miss):
        problems.append(f"не хватает ключей: {len(miss)} (например {key(miss[0])})")
    if len(extra):
        problems.append(f"лишних ключей: {len(extra)} (например {key(extra[0])})")
    return s, problems


def fig_submission(F: Figures, sub_path: Path, daily, S):
    s, problems = check_submission(sub_path)
    S["submission"] = dict(file=str(sub_path), ok=not problems, problems=problems)
    status = "формат OK" if not problems else "ПРОБЛЕМЫ: " + "; ".join(problems)
    log(f"  сабмит {sub_path.name}: {status}")
    if s is None:
        return
    fd = s.dropna(subset=["date"]).groupby(["date", "route"])["prediction"].sum().unstack("route")
    hist = daily[daily.index >= "2025-08-01"]
    ratios = {}
    fig = F.new(15, 6.6, f"Сабмит «{sub_path.name}»: продолжение истории по дням",
                f"{status}. Синим — факт авг–окт, оранжевым — прогноз ноя–дек. Метка — средние сутки прогноза к октябрю.", legend_rows=1)
    axes = fig.subplots(2, 5, sharex=True).ravel()
    for ax, r in zip(axes, ALL_ROUTES):
        if r in hist.columns:
            ax.plot(hist.index, hist[r], color=SERIES[0], lw=1.1)
        if r in fd.columns:
            ax.plot(fd.index, fd[r], color=SERIES[1], lw=1.1)
        oct_mean = daily.loc[daily.index.month == 10, r].mean() if r in daily.columns else np.nan
        fc_mean = fd[r].mean() if r in fd.columns else np.nan
        ratio = fc_mean / oct_mean if oct_mean and np.isfinite(oct_mean) and oct_mean > 0 else np.nan
        ratios[r] = None if not np.isfinite(ratio) else round(float(ratio), 3)
        ax.set_title(f"№{r}" + (f" · ×{num(ratio, 2)} к окт" if np.isfinite(ratio) else " · нет истории"))
        ax.set_ylim(0, None)
        if r in fd.columns and fd[r].max() == 0 and r not in hist.columns:
            ax.set_ylim(0, 1)
            ax.text(0.5, 0.5, "прогноз = 0", transform=ax.transAxes, ha="center", fontsize=9, color=INK2)
        ax.yaxis.set_major_formatter(KFMT)
        month_axis(ax)
    S["submission"]["forecast_to_october"] = {str(k): v for k, v in ratios.items()}
    F.legend(fig, [line_key(SERIES[0], 2, "факт"), line_key(SERIES[1], 2, "прогноз")], ncol=2)
    F.save(fig, "18_submission", "Модель и сабмит", "Проверка сабмита",
           "Перед каждой отправкой: формат (14 640 строк, «;», полная сетка ключей) и правдоподобность уровня "
           "относительно октября — провалы/скачки на стыке видны сразу.")


# ─────────────────────────────── сырые CSV (опционально) ───────────────────────────────
RAW_TABLES = ["hourly", "codes", "goods", "chains", "trantype", "places", "vehicles", "cards", "net_cards", "meta"]


def aggregate_raw(data_dir: Path, cache: Path) -> None:
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.csv as pacsv

    cols = ["tran_date_time", "crd_hashcode", "validation_result", "tran_type_id", "place_id",
            "good_type", "pass_route", "ngpt_route", "garage_number"]
    parts: dict[str, list] = defaultdict(list)
    meta = []
    t0 = time.time()
    for fname in ("train.csv", "test.csv"):
        fpath = data_dir / fname
        if not fpath.exists():
            log(f"  нет {fpath}, пропускаю")
            continue
        size = fpath.stat().st_size
        reader = pacsv.open_csv(
            fpath,
            read_options=pacsv.ReadOptions(block_size=64 << 20, use_threads=True),
            parse_options=pacsv.ParseOptions(delimiter=";"),
            convert_options=pacsv.ConvertOptions(include_columns=cols, strings_can_be_null=True,
                                                 column_types={c: pa.string() for c in cols}))
        rows = bad = noroute = 0
        nb = 0
        for batch in reader:
            nb += 1
            t = pa.Table.from_batches([batch])
            rows += t.num_rows
            ts = t["tran_date_time"]
            valid = pc.fill_null(pc.match_substring_regex(ts, r"^\d{4}-\d\d-\d\d \d\d"), False)
            bad += t.num_rows - pc.sum(pc.cast(valid, pa.int64())).as_py()
            t = t.filter(valid)
            route = pc.struct_field(pc.extract_regex(t["ngpt_route"], r"^(?P<r>\d+)"), [0])
            route = pc.cast(route, pa.int16())
            has_route = pc.is_valid(route)
            noroute += t.num_rows - pc.sum(pc.cast(has_route, pa.int64())).as_py()
            ok = pc.fill_null(pc.equal(t["validation_result"], "1"), False)
            pr = t["pass_route"]
            rail = pc.fill_null(pc.match_substring_regex(pr, "Мосметро|МЦК|МЦД|ММТС"), False)
            multi = pc.fill_null(pc.match_substring(pr, "."), False)
            cat = pc.case_when(pc.make_struct(pc.is_null(pr), rail, multi, field_names=["n", "r", "m"]),
                               *[pa.scalar(s, pa.string()) for s in
                                 ("нет данных", "с метро/МЦК/МЦД", "с НГПТ", "одна поездка")])
            x = pa.table({
                "route": route, "date": pc.utf8_slice_codeunits(t["tran_date_time"], 0, 10),
                "hour": pc.cast(pc.utf8_slice_codeunits(t["tran_date_time"], 11, 13), pa.int8()),
                "ok": pc.cast(ok, pa.int32()), "okb": ok, "vr": t["validation_result"], "tt": t["tran_type_id"],
                "place": t["place_id"], "good": t["good_type"], "cat": cat, "garage": t["garage_number"],
                "crd": t["crd_hashcode"],
            }).filter(has_route)
            parts["hourly"].append(x.group_by(["route", "date", "hour"]).aggregate([("ok", "sum"), ([], "count_all")]))
            parts["codes"].append(x.group_by(["route", "date", "vr"]).aggregate([([], "count_all")]))
            parts["trantype"].append(x.group_by(["route", "tt", "ok"]).aggregate([([], "count_all")]))
            parts["places"].append(x.group_by(["route", "place"]).aggregate([([], "count_all")]))
            xo = x.filter(x["okb"])
            parts["goods"].append(xo.group_by(["route", "date", "good"]).aggregate([([], "count_all")]))
            parts["chains"].append(xo.group_by(["route", "date", "hour", "cat"]).aggregate([([], "count_all")]))
            parts["vehicles"].append(xo.filter(pc.is_valid(xo["garage"])).group_by(["route", "date", "hour", "garage"]).aggregate([]))
            xs = xo.filter(pc.fill_null(pc.starts_with(xo["crd"], "0"), False))   # 1/16 выборка карт по хешу
            parts["cards"].append(xs.group_by(["route", "date", "crd"]).aggregate([([], "count_all")]))
            mm = pc.min_max(x["date"]) if x.num_rows else None
            meta.append(dict(file=fname, batch=nb, rows=t.num_rows,
                             dmin=str(mm["min"].as_py()) if mm else "", dmax=str(mm["max"].as_py()) if mm else ""))
            if nb % 10 == 0:
                done = min(1.0, nb * (64 << 20) / size)
                log(f"  {fname}: {rows / 1e6:6.1f} млн строк · ~{done * 100:3.0f}% · {time.time() - t0:5.0f} с")
        log(f"  {fname}: всего {rows:,} строк, битых дат {bad:,}, без маршрута {noroute:,}")
        meta.append(dict(file=fname, batch=-1, rows=rows, bad=bad, noroute=noroute))

    # колонки результата group_by берём по именам: их порядок зависит от версии pyarrow
    def agg(tb, keys, aggs, names):
        res = tb.group_by(keys).aggregate(aggs)
        return res.select(keys + list(names)).rename_columns(keys + list(names.values()))

    def sum_up(name, keys):
        return agg(pa.concat_tables(parts[name]), keys, [("count_all", "sum")], {"count_all_sum": "count_all"}).to_pandas()

    out = {}
    hourly = agg(pa.concat_tables(parts["hourly"]), ["route", "date", "hour"],
                 [("ok_sum", "sum"), ("count_all", "sum")], {"ok_sum_sum": "ok", "count_all_sum": "all"}).to_pandas()
    hourly["fail"] = hourly["all"] - hourly["ok"]
    out["hourly"] = hourly.drop(columns="all")
    out["codes"] = sum_up("codes", ["route", "date", "vr"])
    out["trantype"] = sum_up("trantype", ["route", "tt", "ok"])
    out["places"] = sum_up("places", ["route", "place"])
    out["goods"] = sum_up("goods", ["route", "date", "good"])
    out["chains"] = sum_up("chains", ["route", "date", "hour", "cat"])
    veh = pa.concat_tables(parts["vehicles"]).group_by(["route", "date", "hour", "garage"]).aggregate([])
    out["vehicles"] = agg(veh, ["route", "date", "hour"], [([], "count_all")], {"count_all": "n_veh"}).to_pandas()
    cards = agg(pa.concat_tables(parts["cards"]), ["route", "date", "crd"], [("count_all", "sum")],
                {"count_all_sum": "trips"})
    out["cards"] = agg(cards, ["route", "date"], [([], "count_all"), ("trips", "sum")],
                       {"count_all": "cards_s", "trips_sum": "trips_s"}).to_pandas()
    net = cards.group_by(["date", "crd"]).aggregate([])
    out["net_cards"] = agg(net, ["date"], [([], "count_all")], {"count_all": "cards_s"}).to_pandas()
    out["meta"] = pd.DataFrame(meta).astype(str)
    for k, v in out.items():
        v.to_parquet(cache / f"raw_{k}.parquet", index=False)
    log(f"  сырые данные агрегированы за {time.time() - t0:.0f} с → {cache}")


def load_raw(cache: Path) -> dict | None:
    files = {k: cache / f"raw_{k}.parquet" for k in RAW_TABLES}
    if not all(f.exists() for f in files.values()):
        return None
    raw = {k: pd.read_parquet(f) for k, f in files.items()}
    for k, v in raw.items():
        if "date" in v.columns:
            v["date"] = pd.to_datetime(v["date"], errors="coerce")
        if "route" in v.columns:
            v["route"] = v["route"].astype(int)
    return raw


def fig_raw(F: Figures, raw: dict, g: pd.DataFrame, c: pd.DataFrame, S):
    hourly = raw["hourly"]
    R = {}
    # 1. сверка с labels
    rh = hourly[(hourly["date"] >= HIST_START) & (hourly["date"] <= HIST_END)]
    m = g.merge(rh[["route", "date", "hour", "ok"]], on=["route", "date", "hour"], how="outer").fillna(0)
    diff = (m["ok"] - m["boardings"])
    per_route = m.groupby("route")[["ok", "boardings"]].sum()
    rel = (per_route["ok"] / per_route["boardings"].replace(0, np.nan) - 1) * 100
    tail = hourly[(hourly["date"] < HIST_START) | (hourly["date"] > HIST_END)].groupby(["date", "route"])["ok"].sum()
    routes_raw = hourly.groupby("route")["ok"].sum()
    R["labels_check"] = dict(cells=int(len(m)), cells_diff=int((diff != 0).sum()), max_abs_diff=int(diff.abs().max()),
                             rel_diff_pct_by_route={int(k): round(float(v), 4) for k, v in rel.items()},
                             outside_period={f"{d:%Y-%m-%d}/№{r}": int(v) for (d, r), v in tail.items()},
                             ok_by_route_raw={int(k): int(v) for k, v in routes_raw.items()})
    r5 = raw["codes"][raw["codes"]["route"] == 5]
    R["route5_raw"] = {f"{d:%Y-%m-%d}/код {v}": int(n) for d, v, n in r5[["date", "vr", "count_all"]].itertuples(index=False)}
    start5 = S.get("reference", {}).get("route_date_start", {}).get("5")
    tail_h = hourly[hourly["date"] > HIST_END]
    tail_hours = sorted(tail_h.loc[tail_h["ok"] > 0, "hour"].unique().tolist())
    tail_r = tail_h.groupby("route")["ok"].sum().reindex(ALL_ROUTES).fillna(0)
    ok_all = (diff == 0).all()
    fig = F.new(13, 4.6, "Сверка сырых CSV с labels и «хвост» в период прогноза",
                (f"Агрегация таргета воспроизведена точно: {num(len(m), 0)} ячеек маршрут×дата×час, расхождений "
                 f"{num((diff != 0).sum(), 0)}. " if ok_all else
                 f"ВНИМАНИЕ: {num((diff != 0).sum(), 0)} расхождений из {num(len(m), 0)} ячеек, макс. |Δ| = "
                 f"{num(diff.abs().max(), 0)}. ")
                + f"В test.csv есть посадки за {FC_START:%d.%m.%Y}: {num(tail_r.sum(), 0)} (часы "
                + ", ".join(map(str, tail_hours)) + ") — единственные известные факты периода прогноза. "
                + f"Маршрут 5: успешных посадок {num(routes_raw.get(5, 0), 0)}, всего строк {num(r5['count_all'].sum(), 0)}"
                + (f"; по справочнику запуск {start5}" if start5 else "") + " — нужен cold start по аналогу.")
    ax = fig.subplots()
    ax.bar(np.arange(len(tail_r)), tail_r.values, width=0.32, color=SERIES[0])
    ax.set_xticks(np.arange(len(tail_r)), [f"№{r}" for r in tail_r.index])
    ax.grid(axis="x", visible=False)
    ax.set_ylabel("посадок")
    ax.set_title(f"Посадки за {FC_START:%d.%m.%Y} из хвоста test.csv по маршрутам")
    for i, v in enumerate(tail_r.values):
        ax.text(i, v, num(v, 0), ha="center", va="bottom", fontsize=8.5, color=INK)
    F.save(fig, "20_raw_labels_check", "Сырые данные", "Сверка агрегации",
           "Подтверждение корректной агрегации целевой величины (validation_result = 1, дата/час из tran_date_time) — "
           "часть критерия 2в «воспроизводимый пайплайн». Хвост 01.11 можно подставить в сабмит как факт.")

    # 2. коды валидаций
    codes = raw["codes"].copy()
    codes["month"] = codes["date"].dt.month
    tot = codes.groupby(["route", "month"])["count_all"].sum()
    bad = codes[codes["vr"] != "1"].groupby(["route", "month"])["count_all"].sum()
    fr = (bad / tot * 100).unstack("month").reindex(columns=range(1, 11))
    fr = fr[fr.index.isin(g["route"].unique())]
    fr = fr.loc[fr.mean(axis=1).sort_values(ascending=False).index]
    fr.columns = [MONTHS_RU[i - 1] for i in fr.columns]
    fr.index = [f"№{r}" for r in fr.index]
    top = codes[codes["vr"] != "1"].groupby("vr")["count_all"].sum().sort_values(ascending=False).head(10)
    top_share = top / codes["count_all"].sum() * 100
    R["fail_share_pct"] = round(float(bad.sum() / tot.sum() * 100), 2)
    fig = F.new(14, 5.4, "Неуспешные валидации (validation_result ≠ 1)",
                f"В среднем {num(R['fail_share_pct'], 1)} % всех валидаций — отказы. В таргет они не входят, "
                "но всплеск отказов — маркер проблем оборудования/данных в конкретный месяц.")
    gs = fig.add_gridspec(1, 2, width_ratios=[1.6, 1])
    a1, a2 = fig.add_subplot(gs[0]), fig.add_subplot(gs[1])
    im = heatmap(a1, fr, SEQ, 0, float(np.nanmax(fr.to_numpy())), lambda v: num(v, 1), fontsize=7.5)
    a1.set_title("Доля отказов, % (маршрут × месяц)")
    fig.colorbar(im, ax=a1, shrink=0.8, pad=0.01).outline.set_visible(False)
    a2.barh(np.arange(len(top_share)), top_share.values, height=0.42, color=SERIES[0])
    a2.set_yticks(np.arange(len(top_share)), [f"код {k}" for k in top_share.index])
    a2.invert_yaxis()
    a2.xaxis.set_major_formatter(PercentFormatter(decimals=1))
    a2.grid(axis="y", visible=False)
    a2.set_title("Топ кодов отказа, % всех валидаций")
    F.save(fig, "21_raw_validation_codes", "Сырые данные", "Отказы валидаций",
           "Отказы исключаются из целевой величины; их динамика помогает находить сбойные дни.")

    # 3. типы билетов
    goods = raw["goods"]
    gt = goods.groupby(["route", "good"])["count_all"].sum()
    top_goods = goods.groupby("good")["count_all"].sum().sort_values(ascending=False).head(9).index
    share = gt.unstack("good").fillna(0)
    share["Прочие"] = share.drop(columns=top_goods).sum(axis=1)
    share = share[list(top_goods) + ["Прочие"]]
    share = share.div(share.sum(axis=1), axis=0) * 100
    share = share.loc[share.index.isin(g["route"].unique())]
    share.index = [f"№{r}" for r in share.index]
    R["top_ticket_types"] = [str(x) for x in top_goods]
    fig = F.new(14, 5.4, "Структура оплаты: доля типов билетов в посадках, %", "Топ-9 типов билетов, остальные — «Прочие».")
    ax = fig.subplots()
    im = heatmap(ax, share, SEQ, 0, float(share.to_numpy().max()), lambda v: num(v, 0), fontsize=8)
    ax.set_xticklabels(share.columns, rotation=25, ha="right")
    fig.colorbar(im, ax=ax, shrink=0.8, pad=0.01).outline.set_visible(False)
    F.save(fig, "22_raw_tickets", "Сырые данные", "Типы билетов",
           "Доля проездных (30/90/365 дней) — «ядро» регулярных пассажиров, устойчивое к погоде; "
           "разовые (Кошелёк, ББК) чувствительнее к внешним факторам.")

    # 4. пересадки
    ch = raw["chains"].merge(c[["daytype"]], left_on="date", right_index=True)
    by_r = ch.groupby(["route", "cat"])["count_all"].sum().unstack("cat").fillna(0)
    by_r = by_r.div(by_r.sum(axis=1), axis=0) * 100
    by_r = by_r.loc[by_r.index.isin(g["route"].unique())]
    wd = ch[ch["daytype"] == "Пн–Чт"].groupby(["hour", "cat"])["count_all"].sum().unstack("cat").fillna(0)
    wd = wd.div(wd.sum(axis=1), axis=0) * 100
    R["transfer_share_pct"] = {str(k): round(float(v), 2) for k, v in
                               (ch.groupby("cat")["count_all"].sum() / ch["count_all"].sum() * 100).items()}
    cats = [k for k in ["с метро/МЦК/МЦД", "с НГПТ"] if k in by_r.columns]
    fig = F.new(14, 5.4, "Пересадки: цепочка поездки пассажира (pass_route)",
                "Доля посадок, перед которыми в цепочке были метро/МЦК/МЦД или другой наземный транспорт.",
                legend_rows=1)
    a1, a2 = fig.subplots(1, 2)
    x = np.arange(len(by_r))
    bw = 0.28
    for k, cat in enumerate(cats):
        a1.bar(x + (k - 0.5) * bw, by_r[cat], width=bw, color=SERIES[k], **BAR)
        a2.plot(wd.index, wd[cat], color=SERIES[k], lw=1.8)
    a1.set_xticks(x, [f"№{r}" for r in by_r.index])
    a1.yaxis.set_major_formatter(PercentFormatter(decimals=1))
    a1.grid(axis="x", visible=False)
    a1.set_title("По маршрутам, % посадок")
    a2.yaxis.set_major_formatter(PercentFormatter(decimals=1))
    a2.set_xticks(range(0, 24, 3))
    a2.set_title("Сеть по часам, будни Пн–Чт, % посадок")
    F.legend(fig, [Patch(color=SERIES[k], label=cat) for k, cat in enumerate(cats)], ncol=2)
    F.save(fig, "23_raw_transfers", "Сырые данные", "Пересадки",
           "Маршруты-«фидеры» метро сильнее зависят от работы метро и пересадочных узлов — полезный геопризнак "
           "для переноса модели на новые маршруты/остановки.")

    # 5. вагоны на линии и нагрузка на вагон
    v = raw["vehicles"].merge(hourly[["route", "date", "hour", "ok"]], on=["route", "date", "hour"])
    v = v.merge(c[["daytype"]], left_on="date", right_index=True)
    v = v[(v["date"] >= SPLIT_DATE) & (v["daytype"] == "Пн–Чт")]
    veh = v.groupby(["route", "hour"])["n_veh"].median().unstack("hour").reindex(columns=range(5, 24))
    load = v.assign(lpv=v["ok"] / v["n_veh"]).groupby(["route", "hour"])["lpv"].median().unstack("hour").reindex(columns=range(5, 24))
    order = [r for r in g.groupby("route")["boardings"].sum().sort_values(ascending=False).index if r in veh.index]
    veh, load = veh.loc[order], load.loc[order]
    veh.index = load.index = [f"№{r}" for r in order]
    R["peak_load_per_vehicle"] = {k: round(float(row.max()), 1) for k, row in load.iterrows()}
    fig = F.new(14, 8.4, "Подвижной состав: вагоны на линии и посадки на вагон в час",
                "Будни Пн–Чт, сентябрь–октябрь, медиана. Вагон = уникальный garage_number с успешной валидацией в этот час.")
    a1, a2 = fig.subplots(2, 1)
    im1 = heatmap(a1, veh, SEQ, 0, float(np.nanmax(veh.to_numpy())), lambda v_: num(v_, 0), fontsize=7.5)
    a1.set_title("Вагонов на линии (медиана)")
    im2 = heatmap(a2, load, SEQ, 0, float(np.nanmax(load.to_numpy())), lambda v_: num(v_, 0), fontsize=7.5)
    a2.set_title("Посадок на вагон за час (медиана) — прокси наполненности")
    a2.set_xlabel("час")
    fig.colorbar(im1, ax=a1, shrink=0.8, pad=0.01).outline.set_visible(False)
    fig.colorbar(im2, ax=a2, shrink=0.8, pad=0.01).outline.set_visible(False)
    F.save(fig, "24_raw_vehicles_load", "Сырые данные", "Вагоны и нагрузка",
           "Бизнес-ценность: где посадок на вагон больше всего — кандидаты на усиление выпуска в пик; "
           "где мало — на сокращение интервала/выпуска.")

    # 6. уникальные пассажиры (оценка по 1/16 выборке хешей карт)
    nc = raw["net_cards"].set_index("date")["cards_s"].sort_index() * 16
    nc = nc[(nc.index >= HIST_START) & (nc.index <= HIST_END)]
    cd = raw["cards"].merge(c[["daytype"]], left_on="date", right_index=True)
    cd = cd[(cd["date"] >= SPLIT_DATE) & (cd["daytype"] == "Пн–Чт")]
    tpp = (cd.groupby("route")["trips_s"].sum() / cd.groupby("route")["cards_s"].sum()).sort_values()
    tpp = tpp[tpp.index.isin(g["route"].unique())]
    R["unique_passengers_per_day_mean"] = int(nc.mean())
    fig = F.new(14, 5.2, "Пассажиры: оценка уникальных карт в сутки",
                "Оценка по детерминированной 1/16 выборке хешей карт (crd_hashcode начинается с «0») × 16. "
                "Справа — поездок на карту в сутки на маршруте (Пн–Чт, сен–окт).")
    gs = fig.add_gridspec(1, 2, width_ratios=[1.7, 1])
    a1, a2 = fig.add_subplot(gs[0]), fig.add_subplot(gs[1])
    a1.plot(nc.index, nc.values, color=BLUE[200], lw=0.8)
    a1.plot(nc.index, nc.rolling(7, center=True).mean(), color=SERIES[0], lw=1.8)
    a1.yaxis.set_major_formatter(KFMT)
    a1.set_ylim(0, None)
    month_axis(a1)
    a1.set_title("Уникальных карт в сутки, сеть")
    a2.barh(np.arange(len(tpp)), tpp.values, height=0.42, color=SERIES[0])
    a2.set_yticks(np.arange(len(tpp)), [f"№{r}" for r in tpp.index])
    a2.grid(axis="y", visible=False)
    for i, val in enumerate(tpp.values):
        a2.text(val + 0.01, i, num(val, 2), va="center", fontsize=8, color=INK)
    a2.set_xlim(0, tpp.max() * 1.12)
    a2.set_title("Поездок на карту в сутки")
    F.save(fig, "25_raw_passengers", "Сырые данные", "Уникальные пассажиры",
           "Большинство пассажиров делают 1–2 поездки на маршруте в сутки (туда-обратно) — "
           "основа для оценки спроса в пассажирах, а не только в валидациях.")
    S["raw"] = R


# ─────────────────────────────── HTML-отчёт ───────────────────────────────
def write_report(F: Figures, S: dict, out: Path) -> None:
    tiles = []
    n = S.get("network", {})
    if n:
        tiles += [("Посадок янв–окт", compact(n["total"])), ("В среднем в сутки", compact(n["mean_daily"])),
                  ("Маршрутов с разметкой", str(len(n["routes"])))]
    bt = S.get("backtest_profile4w")
    if bt:
        tiles.append(("WAPE-score бейзлайна (ср. 3 фолдов)", num(bt["mean"], 3)))
    we = S.get("weather_effect_network", {}).get("precip")
    if we:
        tiles.append(("Осадки: эффект на 1 мм", f"{num(we['slope_pct_per_unit'], 2)} %"))
    hol = S.get("holidays")
    if hol:
        tiles.append(("Праздник к воскресенью", f"× {num(hol['network_to_sunday_median'], 2)}"))

    findings = []
    wi = S.get("weekday_index_network")
    if wi:
        findings.append("Индекс дня недели (сеть, к Вт–Чт): " + ", ".join(f"{k} {num(v, 2)}" for k, v in wi.items()) + ".")
    if hol:
        findings.append(f"Праздничный день ≈ {num(hol['network_to_sunday_median'], 2)} × воскресенья и "
                        f"{num(hol['network_to_saturday_median'], 2)} × субботы (медиана по праздникам янв–июн).")
    ht = S.get("hour_share_top8")
    if ht:
        findings.append(f"8 самых загруженных часов ({', '.join(map(str, ht['hours']))}) дают {num(ht['share'], 0)} % "
                        "всех посадок — основной вес WAPE.")
    if we:
        findings.append(f"Осадки: {num(we['slope_pct_per_unit'], 2)} % посадок сети на 1 мм за сутки "
                        f"(t = {num(we['t'], 1)}, n = {we['n']}).")
    sc = S.get("school_holidays_effect_pct", {}).get("Сеть")
    if sc:
        findings.append("Школьные каникулы (будни к двум предыдущим неделям): " +
                        ", ".join(f"{k} {num(v, 1)} %" for k, v in sc.items()) + ".")
    if bt:
        findings.append("Бэктест профиля 4 недель, горизонт 61 день: " +
                        ", ".join(f"{k} {num(v, 3)}" for k, v in bt["folds"].items()) + ".")
    wr = S.get("weekend_ratio", {})
    changed = [f"№{r}: {num(v['spring_median'], 2)} → {num(v['last_4w'], 2)}" for r, v in wr.items()
               if np.isfinite(v["last_4w"]) and np.isfinite(v["spring_median"])
               and abs(v["last_4w"] - v["spring_median"]) > 0.15]
    if changed:
        findings.append("Сменился режим выходных (Сб–Вс/Пн–Пт, весна → последние 4 недели): " + "; ".join(changed) + ".")
    an = S.get("anomalies_low", [])
    if an:
        findings.append("Самые аномальные дни: " + "; ".join(f"№{a['route']} {a['date']} (×{num(a['ratio'], 2)})"
                                                            for a in an[:6]) + ".")
    raw = S.get("raw")
    if raw:
        lc = raw["labels_check"]
        findings.append(f"Сырые CSV → labels: {lc['cells_diff']} расхождений на {num(lc['cells'], 0)} ячеек. "
                        f"Хвост test.csv за 01.11: {num(sum(lc['outside_period'].values()), 0)} посадок (факт периода прогноза). "
                        f"Маршрут 5: успешных посадок {num(lc['ok_by_route_raw'].get(5, 0), 0)} — нужен cold start.")
        tr = raw.get("transfer_share_pct", {})
        findings.append(f"Отказы валидаций: {num(raw['fail_share_pct'], 1)} %; пересадка с метро/МЦК/МЦД перед посадкой: "
                        f"{num(tr.get('с метро/МЦК/МЦД', 0), 1)} %; ≈ {compact(raw['unique_passengers_per_day_mean'])} "
                        "уникальных карт в сутки.")
    sub = S.get("submission")
    if sub:
        findings.append(f"Сабмит {Path(sub['file']).name}: " + ("формат OK." if sub["ok"] else "; ".join(sub["problems"])))

    sections = defaultdict(list)
    for it in F.items:
        sections[it["section"]].append(it)
    body = []
    for sec, items in sections.items():
        body.append(f'<h2>{html.escape(sec)}</h2>')
        for it in items:
            body.append(f'<figure><img loading="lazy" src="{it["file"]}" alt="{html.escape(it["title"])}">'
                        f'<figcaption><b>{html.escape(it["title"])}.</b> {html.escape(it["caption"])}</figcaption></figure>')
    src = "".join(f"<li><b>{html.escape(k)}</b>: {html.escape(v)}</li>" for k, v in SOURCES.items())
    page = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Трамваи: EDA</title>
<style>
:root{{--bg:#f9f9f7;--card:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--line:#e1e0d9;--accent:#2a78d6}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#0d0d0d;--card:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--line:#2c2c2a;--accent:#3987e5}}}}
:root[data-theme="dark"]{{--bg:#0d0d0d;--card:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--line:#2c2c2a;--accent:#3987e5}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,"Segoe UI",sans-serif}}
main{{max-width:1200px;margin:0 auto;padding:24px 16px 64px}}
h1{{font-size:26px;margin:0 0 4px}} h2{{font-size:19px;margin:36px 0 12px;padding-top:12px;border-top:1px solid var(--line)}}
.sub{{color:var(--ink2);margin:0 0 20px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:16px 0}}
.tile{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}}
.tile .l{{color:var(--ink2);font-size:13px}} .tile .v{{font-size:26px;font-weight:600}}
ul.f li{{margin:4px 0}} figure{{margin:0 0 22px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px}}
figure img{{width:100%;height:auto;display:block;border-radius:6px;background:#fcfcfb}}
figcaption{{color:var(--ink2);font-size:14px;padding:8px 4px 2px}} a{{color:var(--accent)}}
</style></head><body><main>
<h1>Трамвайные маршруты: разведочный анализ</h1>
<p class="sub">Хакатон Московского транспорта · данные янв–окт 2025 · прогноз ноя–дек 2025 (61 день × 24 ч × 10 маршрутов) ·
сгенерировано da.py {time.strftime('%Y-%m-%d %H:%M')}</p>
<div class="tiles">{''.join(f'<div class="tile"><div class="l">{html.escape(a)}</div><div class="v">{html.escape(b)}</div></div>' for a, b in tiles)}</div>
<h2>Ключевые наблюдения</h2><ul class="f">{''.join(f'<li>{html.escape(x)}</li>' for x in findings)}</ul>
<p><a href="map_stops.html">Интерактивная карта маршрутов →</a> · <a href="summary.json">summary.json</a></p>
{''.join(body)}
<h2>Внешние источники</h2><ul>{src}</ul>
</main></body></html>"""
    (out / "report.html").write_text(page, encoding="utf-8")
    log("  ✓ report.html")


# ─────────────────────────────── main ───────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(description="EDA и визуализация данных трамвайного трека")
    ap.add_argument("--data", type=Path, default=ROOT / "dataset", help="папка распакованного dataset")
    ap.add_argument("--out", type=Path, default=HERE / "output", help="куда писать графики и отчёт")
    ap.add_argument("--raw", action="store_true", help="агрегировать сырые train/test.csv (~10 ГБ), если нет кэша")
    ap.add_argument("--rebuild-raw", action="store_true", help="пересобрать кэш сырых агрегатов")
    ap.add_argument("--offline", action="store_true", help="не обращаться к сети (погода/календарь)")
    ap.add_argument("--submission", type=Path, default=None, help="файл сабмита для проверки")
    args = ap.parse_args()

    out, cache = args.out, args.out / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    setup_style()
    F, S = Figures(out), {}
    t0 = time.time()

    log("Загрузка labels, календаря, погоды, справочников…")
    g = load_labels(args.data)
    cal = load_calendar(cache, args.offline)
    weather = load_weather(cache, args.offline)
    ref_routes, stops = load_reference(args.data)
    route_names = {}
    if ref_routes is not None:
        route_names = {int(r): str(n) for r, n in zip(ref_routes["route_short_name"], ref_routes["route_long_name"])}
    daily, c, ratio = build_daily(g, cal)
    S["calendar_source"] = cal.attrs.get("source")
    S["routes_without_labels"] = [r for r in ALL_ROUTES if r not in daily.columns]
    log(f"  сетка {len(g):,} строк · маршруты {list(daily.columns)} · календарь: {cal.attrs.get('source')}")

    log("Структура спроса…")
    fig_network_daily(F, daily, c, S)
    fig_routes_daily(F, daily, c, route_names)
    fig_route_month(F, daily, S)
    fig_weekday_hour(F, g, c)
    fig_daytype_profiles(F, g, c)
    fig_weekday_index(F, daily, c, S)
    log("Режимы и аномалии…")
    fig_calendar(F, daily, c, ratio, S)
    fig_weekend_ratio(F, daily, c, S)
    fig_level_trend(F, daily)
    fig_hour_weight(F, g, c, S)
    log("Внешние факторы…")
    fig_holidays(F, daily, c, S)
    fig_forecast_calendar(F, cal, S)
    if weather is not None:
        fig_weather_timeline(F, weather, daily, c)
        fig_weather_effect(F, weather, daily, c, S)
    fig_school(F, daily, c, S)
    log("Геопривязка…")
    fig_stops_map(F, ref_routes, stops, daily, g, c, out, S)
    log("Бэктест и сабмит…")
    fig_backtest(F, g, cal, S)
    sub = args.submission
    if sub is None:
        for cand in (ROOT / "submission.csv", args.data / "test_submission.csv"):
            if cand.exists():
                sub = cand
                break
    if sub is not None and sub.exists():
        fig_submission(F, sub, daily, S)

    if args.raw or args.rebuild_raw or load_raw(cache) is not None:
        if args.rebuild_raw or load_raw(cache) is None:
            log("Агрегация сырых CSV (потоково, pyarrow)…")
            aggregate_raw(args.data, cache)
        raw = load_raw(cache)
        if raw is not None:
            log("Графики по сырым данным…")
            fig_raw(F, raw, g, c, S)
    else:
        log("Сырые CSV пропущены (запустите с --raw для отказов, билетов, пересадок, вагонов, пассажиров)")

    S["sources"] = SOURCES
    (out / "summary.json").write_text(json.dumps(S, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    write_report(F, S, out)
    log(f"Готово за {time.time() - t0:.0f} с: {len(F.items)} графиков → {out / 'report.html'}")


if __name__ == "__main__":
    main()
