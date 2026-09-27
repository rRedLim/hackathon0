"""Сборка данных веб-сервиса из артефактов ML-модели, справочников и внешних источников.

Модули пайплайна (см. docs/architecture.md):
  1. приём/нормализация  — история посадок (ingest ML: сырые валидации → маршрут × дата × час);
  2. геопривязка         — остановки и порядок следования из справочника ГТФС (+ OSM для маршрутов,
                            которых нет в справочнике), априорные веса посадок по остановкам;
  3. ML-прогноз          — готовые таблицы горизонтов день/месяц/год (py -m ml export);
  4. внешние факторы     — осадки (Open-Meteo), производственный календарь (isdayoff.ru), события (regimes.json).

Результат — каталог service_data/ с компактными CSV/JSON, которые backend целиком держит в памяти.

Запуск:  python pipeline/build_service_data.py [--ml Tramvay] [--ref data/reference] [--out service_data]
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROUTES = [1, 5, 7, 11, 12, 17, 25, 26, 28, 50]
COLORS = {1: "#e6194b", 5: "#f58231", 7: "#3cb44b", 11: "#4363d8", 12: "#911eb4",
          17: "#008080", 25: "#9a6324", 26: "#f032e6", 28: "#808000", 50: "#000075"}
HUB = re.compile(r"метро|вокзал|мцк|мцд|станци|платформа|рынок|площадь|пл\.", re.I)


def log(msg: str) -> None:
    print(f"[pipeline] {msg}", flush=True)


# ---------------------------------------------------------------- геопривязка
def stop_weights(seq: pd.DataFrame) -> pd.Series:
    """Априорная доля посадок остановки внутри направления.

    Посадки по остановкам в данных отсутствуют (place_id — площадка/депо), поэтому прогноз маршрута
    распределяется по остановкам априорными весами:
      • конечная прибытия — почти нет посадок (×0.05), начальная — ×1.5;
      • пересадочные узлы (метро, вокзалы, МЦК/МЦД, площади, рынки) — ×2;
      • к концу направления посадок меньше: ×(1 − 0.5 · доля пройденного пути).
    Веса заменяются фактическими долями, как только появятся валидации с геопозицией (stop_weights.csv).
    """
    n = len(seq)
    pos = (seq["seq"].rank(method="first") - 1) / max(n - 1, 1)
    w = 1.0 - 0.5 * pos
    w *= seq["stop_name"].fillna("").str.contains(HUB).map({True: 2.0, False: 1.0})
    w.iloc[0] *= 1.5
    w.iloc[-1] *= 0.05
    return w


def load_reference_stops(xlsx: Path) -> tuple[pd.DataFrame, dict]:
    x = pd.ExcelFile(xlsx)
    st = x.parse("Порядок_с_координатами")
    st = st.dropna(subset=["stop_lat", "stop_lon"])
    routes = x.parse(x.sheet_names[0], header=1)
    names = {int(r.route_short_name): str(r.route_long_name) for r in routes.itertuples()}
    st = st[st["route_short_name"].isin(ROUTES)]
    out = st.rename(columns={"route_short_name": "route", "direction_id": "direction", "stop_sequence": "seq",
                             "stop_lat": "lat", "stop_lon": "lon", "stop_name": "stop_name"})
    out["source"] = "gtfs"
    return out[["route", "direction", "seq", "stop_id", "stop_name", "lat", "lon", "source"]], names


def load_osm_stops(path: Path) -> tuple[pd.DataFrame, dict]:
    """OSM (Overpass) — relation route=tram, участники с ролью stop*/platform*; для маршрутов вне справочника."""
    if not path.exists():
        return pd.DataFrame(), {}
    d = json.loads(path.read_text(encoding="utf-8"))
    nodes = {e["id"]: e for e in d["elements"] if e["type"] == "node"}
    rows, names, ndirs = [], {}, {}
    rels = sorted((e for e in d["elements"] if e["type"] == "relation"), key=lambda e: e["id"])
    for rel in rels:
        t = rel.get("tags", {})
        if not str(t.get("ref", "")).isdigit():
            continue
        route = int(t["ref"])
        direction = ndirs.get(route, 0)
        if direction > 1:  # берём два направления (две relation маршрута)
            continue
        members = [m for m in rel["members"] if m["type"] == "node" and m["role"].startswith("stop")]
        if len(members) < 3:
            members = [m for m in rel["members"] if m["type"] == "node" and m["role"].startswith("platform")]
        pts = [nodes[m["ref"]] for m in members if m["ref"] in nodes and "lat" in nodes[m["ref"]]]
        if len(pts) < 3:
            continue
        ndirs[route] = direction + 1
        for i, n in enumerate(pts, 1):
            rows.append(dict(route=route, direction=direction, seq=i, stop_id=f"osm{n['id']}",
                             stop_name=n.get("tags", {}).get("name", f"Остановка {i}"),
                             lat=n["lat"], lon=n["lon"], source="osm"))
        if route not in names:
            names[route] = re.sub(r"^[^:]*:\s*", "", t.get("name", "")).replace(" => ", " - ")
    return pd.DataFrame(rows), names


def build_stops(ref: Path, out: Path, only_routes_without_gtfs: bool = True) -> tuple[pd.DataFrame, dict]:
    gtfs, names = load_reference_stops(ref / "tram_reference.xlsx")
    osm, osm_names = load_osm_stops(ref / "osm_tram_routes.json")
    if len(osm):
        osm = osm[~osm["route"].isin(gtfs["route"].unique())] if only_routes_without_gtfs else osm
        for r, n in osm_names.items():
            names.setdefault(r, n)
    st = pd.concat([gtfs, osm], ignore_index=True)
    st = st.sort_values(["route", "direction", "seq"]).reset_index(drop=True)
    st["stop_id"] = st["stop_id"].astype(str)
    st["w"] = 0.0
    for (_, _), g in st.groupby(["route", "direction"]):
        st.loc[g.index, "w"] = stop_weights(g).to_numpy()
    # доля маршрута: направления делят посадки поровну, внутри направления — по весам
    ndir = st.groupby("route")["direction"].transform("nunique")
    st["weight"] = st["w"] / st.groupby(["route", "direction"])["w"].transform("sum") / ndir
    override = ref / "stop_weights.csv"
    if override.exists():  # фактические доли остановок, когда появятся валидации с геопозицией
        ow = pd.read_csv(override, dtype={"stop_id": str}).set_index(["route", "stop_id"])["weight"]
        key = list(zip(st["route"], st["stop_id"]))
        st["weight"] = [ow.get(k, w) for k, w in zip(key, st["weight"])]
    st["lat"] = st["lat"].astype(float).round(6)
    st["lon"] = st["lon"].astype(float).round(6)
    st["weight"] = st["weight"].round(6)
    st[["route", "direction", "seq", "stop_id", "stop_name", "lat", "lon", "weight", "source"]].to_csv(
        out / "stops.csv", index=False)
    log(f"остановки: {len(st)} ({st['route'].nunique()} маршрутов: "
        f"{sorted(st['route'].unique().tolist())}), источник: {st['source'].value_counts().to_dict()}")
    return st, names


# ---------------------------------------------------------------- прогнозы и история
def copy_forecasts(ml: Path, out: Path) -> None:
    o = ml / "ml" / "outputs"
    h = pd.read_csv(o / "forecast_hourly_2025-11_12.csv")
    assert len(h) == 14640 and set(h["route"]) == set(ROUTES), "ожидается сетка 10 × 61 × 24"
    assert (h["pred"] >= 0).all()
    h.to_csv(out / "forecast_hourly.csv", index=False)
    y = pd.read_csv(o / "forecast_year_2026_daily.csv")
    y = y[y["route"].astype(str) != "all"]
    y.to_csv(out / "forecast_year_daily.csv", index=False)
    for src, dst in [("forecast_monthly_2025-11_12.csv", "forecast_monthly.csv"),
                     ("forecast_year_2026_monthly.csv", "forecast_year_monthly.csv"),
                     ("fleet_reinforcement_2025-11_12.csv", "fleet_reinforcement.csv"),
                     ("fleet_summary_2025-11_12.csv", "fleet_summary.csv"),
                     ("fleet_reserve_2025-11_12.csv", "fleet_reserve.csv"),
                     ("model_error_by_horizon.csv", "model_error_by_horizon.csv"),
                     ("model_error_by_level.csv", "model_error_by_level.csv"),
                     ("model_error_by_route.csv", "model_error_by_route.csv")]:
        shutil.copy(o / src, out / dst)
    log(f"прогноз: день {len(h)} ячеек, год {len(y)} дней×маршрутов")


def build_history(ml: Path, out: Path) -> None:
    raw = pd.read_parquet(ml / "ml" / "artifacts" / "raw_route_hour.parquet")
    raw["date"] = pd.to_datetime(raw["date"]).dt.strftime("%Y-%m-%d")
    raw = raw[(raw["date"] >= "2025-01-01") & (raw["date"] <= "2025-10-31")]
    raw = raw[raw["route"].isin(ROUTES)].sort_values(["route", "date", "hour"])
    raw[["route", "date", "hour", "boardings", "n_veh"]].to_csv(out / "history_hourly.csv", index=False)
    log(f"история: {len(raw)} ячеек, {raw['boardings'].sum():,} посадок, {raw['date'].min()} … {raw['date'].max()}")


def build_calendar(ml: Path, out: Path) -> None:
    rows = []
    for year in (2025, 2026):
        s = (ml / "ml" / "external" / "calendar" / f"isdayoff_{year}.txt").read_text().strip()
        d0 = date(year, 1, 1)
        for i, ch in enumerate(s):
            d = d0 + timedelta(days=i)
            rows.append(dict(date=d.isoformat(), code=int(ch), dow=d.isoweekday()))
    pd.DataFrame(rows).to_csv(out / "calendar.csv", index=False)
    log(f"календарь: {len(rows)} дней (0 — рабочий, 1 — выходной, 2 — сокращённый)")


def build_weather(ml: Path, out: Path) -> None:
    p = pd.read_csv(ml / "ml" / "external" / "weather" / "precip_moscow_2025_daily.csv")
    hourly = pd.read_csv(ml / "ml" / "external" / "weather" / "weather_moscow_2025_hourly.csv")
    tcol = next((c for c in hourly.columns if c.startswith("temperature")), None)
    if tcol:
        tcol_time = next(c for c in hourly.columns if "time" in c or c == "date")
        hourly["date"] = pd.to_datetime(hourly[tcol_time]).dt.strftime("%Y-%m-%d")
        t = hourly.groupby("date")[tcol].mean().round(1).rename("temp_c")
        p = p.merge(t, left_on="date", right_index=True, how="left")
    p["precip_mm"] = p["precip_mm"].round(2)
    p.to_csv(out / "weather_daily.csv", index=False)
    log(f"погода: {len(p)} дней")


def build_meta(ml: Path, out: Path, names: dict, stops: pd.DataFrame) -> None:
    regimes = json.loads((ml / "ml" / "regimes.json").read_text(encoding="utf-8"))
    h = pd.read_csv(out / "history_hourly.csv")
    f = pd.read_csv(out / "forecast_hourly.csv")
    routes = []
    for r in ROUTES:
        st = stops[stops["route"] == r]
        hist = h[h["route"] == r]
        routes.append(dict(
            route=r, name=names.get(r, ""), color=COLORS[r],
            stops=int(st["stop_id"].nunique()), geometry=str(st["source"].iloc[0]) if len(st) else None,
            historyBoardings=int(hist["boardings"].sum()),
            forecastBoardings=int(f.loc[f["route"] == r, "pred"].sum()),
            note="Запуск 16.12.2025, холодный старт от аналога (маршрут 7)" if r == 5 else None))
    events = [dict(name=e.get("name"), routes=e.get("routes"), start=e["start"], end=e["end"],
                   hours=e.get("hours"), mult=e["mult"], note=e.get("note"), source=e.get("source"))
              for e in regimes.get("effects", [])]
    reg = [dict(route=r["route"], state=r["state"], days=r["days"], start=r["start"], end=r["end"],
                note=r.get("note"), source=r.get("source")) for r in regimes["regimes"]]
    meta = dict(
        model=dict(name="Структурная модель: уровень × тип дня × профиль суток × календарь × режим × события",
                   leaderboardWapeScore=0.9125, version="r5_v8_W06000",
                   formula="ŷ = W(маршрут)·R(маршрут, тип дня, режим)·S(маршрут, тип дня, час)·K_календарь·K_маршрут·K_события"),
        routes=routes, events=events, regimes=reg,
        sources=[
            dict(name="Производственный календарь РФ 2025–2026", effect="праздники, переносы, рабочая суббота 1.11",
                 url="https://isdayoff.ru/api/getdata?year=2025&pre=1"),
            dict(name="Погода Open-Meteo (ERA5): осадки, температура",
                 effect="−0.64 % посадок на мм осадков (t = −6.2); корректирующий коэффициент",
                 url="https://archive-api.open-meteo.com/v1/archive?latitude=55.7558&longitude=37.6173"
                     "&start_date=2025-01-01&end_date=2025-12-31&hourly=temperature_2m,precipitation"
                     "&timezone=Europe%2FMoscow"),
            dict(name="Пробки ЦОДД (баллы 0–10)", effect="проверено: влияния нет (t = −0.35), в прогноз не включены",
                 url="https://t.me/s/DtOperativno"),
            dict(name="Ремонт в Протопоповском пер. (7, 50 по выходным)", effect="+0.0063 к score",
                 url="https://www.mskagency.ru/materials/3523754"),
            dict(name="Запуск трамвайного диаметра Т1 (12.11.2025)", effect="отток с 7-го ≈ 6 %",
                 url="https://www.mos.ru/mayor/themes/13679050/"),
            dict(name="Бесплатный проезд 31.12 с 20:00", effect="валидаций почти нет",
                 url="https://t.me/DtRoad/55780"),
            dict(name="Запуск маршрута 5 (16.12.2025)", effect="холодный старт от аналога",
                 url="https://www.mos.ru/mayor/themes/13888050/"),
            dict(name="Справочник ГТФС (остановки, порядок следования)", effect="геопривязка маршрутов 1, 5, 7, 11, 12",
                 url="dataset.zip → spravochniki/"),
            dict(name="OpenStreetMap: трамвайные маршруты (relation route=tram)",
                 effect="геопривязка маршрутов 17, 25, 26, 28, 50 — нет в справочнике ГТФС",
                 url="https://api.openstreetmap.org/api/0.6/relation/540033/full.json (relation id: 540033, 540139, "
                     "1538169, 1538170, 1689026, 1689064, 3184022, 3184023, 3186264, 3186265; список — Overpass "
                     "rel[route=tram][ref~\"^(17|25|26|28|50)$\"](55.55,37.35,55.95,37.85))"),
        ],
    )
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    log("meta.json")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ml", default="Tramvay", type=Path)
    ap.add_argument("--ref", default="data/reference", type=Path)
    ap.add_argument("--out", default="service_data", type=Path)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    copy_forecasts(a.ml, a.out)
    build_history(a.ml, a.out)
    build_calendar(a.ml, a.out)
    build_weather(a.ml, a.out)
    stops, names = build_stops(a.ref, a.out)
    build_meta(a.ml, a.out, names, stops)
    log(f"готово → {a.out}/")


if __name__ == "__main__":
    main()
