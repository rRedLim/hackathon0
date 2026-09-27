"""Геопривязка посадок к остановкам по расписанию (выход/график → рейс → остановка).

В валидациях нет остановки (place_id — площадка/депо), но есть маршрут (ngpt_route), выход
(bus_exit_no = график, grafic в расписании), бортовой номер (garage_number) и время. По расписанию
выхода восстанавливается, у какой остановки был вагон в момент валидации:

  1. расписание (лист «Расписание» справочника или CSV той же структуры) → по (маршрут, график)
     список рейсов с упорядоченными остановками и временем отправления в минутах суток;
     рейсы через полночь (время убывает или ≥ 24:00) продолжаются за 1440 мин;
  2. посадка = validation_result == 1, маршрут — число из ngpt_route, время — tran_date_time
     (так же, как ingest ML и IngestService backend);
  3. кандидаты — рейсы графика, у которых [первое отправление − tolerance, последнее + tolerance]
     содержит время посадки; остановка — последняя, отправление с которой ≤ время + slack
     (вагон стоит у неё или только что отошёл);
  4. если дата посадки есть в «Наряде», бортовой номер сверяется с вагоном графика (vehicle_check).

Расписание — шаблон по времени суток: дата посадки с датой расписания не сопоставляется (в справочнике
расписание и наряд — на 2026 год, валидации — 2025). Тип дня (service_id) не учитывается.

Каждая посадка получает статус: bound / no_schedule_for_route / no_schedule_for_grafic / outside_trips /
bad_record. Отчёт (JSON) — итоги по статусам и маршрутам. Доли остановок пишутся в stop_weights.csv
(формат, который читает build_service_data.build_stops) только по маршрутам с достаточным покрытием и
только по явному флагу --write-weights.

Запуск:
  python pipeline/schedule_binding.py --validations FILE [--schedule data/reference/tram_reference.xlsx]
      [--tolerance 3] [--slack 0.5] [--report out.json] [--bound-out bound.csv] [--agg-out agg.csv]
      [--write-weights PATH] [--min-bound 1000] [--min-stop-share 0.8]
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_service_data import load_reference_stops  # noqa: E402

STATUSES = ["bound", "no_schedule_for_route", "no_schedule_for_grafic", "outside_trips", "bad_record"]
VCOLS = ["tran_date_time", "validation_result", "ngpt_route", "bus_exit_no", "garage_number"]
DAY = 1440


def log(msg: str) -> None:
    print(f"[binding] {msg}", flush=True)


# ---------------------------------------------------------------- расписание
@dataclass
class Trip:
    route: int
    grafic: str
    trip_id: str
    direction: int
    stop_ids: list[str]
    stop_names: list[str]
    seqs: list[int]
    dep: np.ndarray  # минуты от начала суток отправления рейса, неубывающие (через полночь > 1440)


def hhmm_to_min(s) -> float:
    """'06:53' / '06:53:30' / '25:10' → минуты; пусто/мусор → NaN."""
    try:
        p = [int(x) for x in str(s).strip().split(":")]
        return p[0] * 60 + p[1] + (p[2] / 60 if len(p) > 2 else 0)
    except (ValueError, IndexError):
        return float("nan")


def grafic_key(v) -> str | None:
    """Выход/график как строка без ведущих нулей и «.0»: 206, '206', '0206', 206.0 → '206'."""
    try:
        return str(int(float(str(v).strip())))
    except ValueError:
        return None


def load_schedule(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(path, sheet_name="Расписание", header=1)
    else:
        df = pd.read_csv(path, sep=None, engine="python")
    df = df.rename(columns={"route_short_name": "route", "direction_id": "direction", "stop_sequence": "seq"})
    for c, default in (("direction", 0), ("stop_name", "")):
        if c not in df:
            df[c] = default
    out = pd.DataFrame({
        "route": pd.to_numeric(df["route"], errors="coerce"),
        "grafic": df["grafic"].map(grafic_key),
        "trip_id": df["trip_id"].astype(str),
        "direction": pd.to_numeric(df["direction"], errors="coerce").fillna(0).astype(int),
        "seq": pd.to_numeric(df["seq"], errors="coerce"),
        "stop_id": df["stop_id"].astype(str).str.replace(r"\.0$", "", regex=True),
        "stop_name": df["stop_name"].fillna("").astype(str),
        "dep": df["departure_time"].map(hhmm_to_min),
    })
    out = out.dropna(subset=["route", "grafic", "seq", "dep"])
    out["route"] = out["route"].astype(int)
    out["seq"] = out["seq"].astype(int)
    return out.sort_values(["route", "grafic", "trip_id", "seq"]).reset_index(drop=True)


def build_trips(sched: pd.DataFrame) -> dict[tuple[int, str], list[Trip]]:
    trips: dict[tuple[int, str], list[Trip]] = {}
    for (route, grafic, trip_id), g in sched.groupby(["route", "grafic", "trip_id"], sort=False):
        dep = g["dep"].to_numpy(dtype=float).copy()
        for i in range(1, len(dep)):  # переход через полночь внутри рейса
            while dep[i] < dep[i - 1] - 1e-9:
                dep[i] += DAY
        trips.setdefault((route, grafic), []).append(Trip(
            route, grafic, trip_id, int(g["direction"].iloc[0]), g["stop_id"].tolist(),
            g["stop_name"].tolist(), g["seq"].tolist(), dep))
    for lst in trips.values():
        lst.sort(key=lambda t: t.dep[0])
    return trips


def load_naryad(path: Path) -> pd.DataFrame:
    """Наряд: дата × маршрут × график → бортовой номер вагона (depot_number) и интервал смены."""
    try:
        n = pd.read_excel(path, sheet_name="Наряд", header=1)
    except (ValueError, FileNotFoundError, KeyError):
        return pd.DataFrame(columns=["date", "route", "grafic", "garage"])
    return pd.DataFrame({
        "date": pd.to_datetime(n["date"].astype(str), format="%Y%m%d", errors="coerce").dt.strftime("%Y-%m-%d"),
        "route": pd.to_numeric(n["route_short_name"], errors="coerce"),
        "grafic": n["grafic"].map(grafic_key),
        "garage": n["depot_number"].map(grafic_key),
    }).dropna()


# ---------------------------------------------------------------- валидации
def read_validations(path: Path, chunksize: int = 200_000):
    """Потоковое чтение сырого CSV (';'), только нужные колонки, всё как строки."""
    yield from pd.read_csv(path, sep=";", usecols=VCOLS, dtype=str, chunksize=chunksize)


def normalize(v: pd.DataFrame) -> pd.DataFrame:
    """Посадки (validation_result == 1) с маршрутом, графиком, датой и минутой суток."""
    v = v[v["validation_result"].astype(str).str.strip() == "1"].copy()
    ts = pd.to_datetime(v["tran_date_time"], format="%Y-%m-%d %H:%M:%S", errors="coerce")
    v["route"] = pd.to_numeric(v["ngpt_route"].astype(str).str.extract(r"^\s*(\d+)")[0], errors="coerce")
    v["grafic"] = v["bus_exit_no"].map(grafic_key)
    v["garage"] = v["garage_number"].map(grafic_key)
    v["date"] = ts.dt.strftime("%Y-%m-%d")
    v["hour"] = ts.dt.hour
    v["tmin"] = ts.dt.hour * 60 + ts.dt.minute + ts.dt.second / 60
    return v[["tran_date_time", "date", "hour", "tmin", "route", "grafic", "garage"]]


# ---------------------------------------------------------------- привязка
def locate(trips: list[Trip], t: float, tolerance: float, slack: float) -> tuple[Trip, int] | None:
    """Рейс и индекс остановки для минуты суток t; None — вне всех рейсов графика."""
    best, best_gap = None, None
    for tr in trips:
        for tt in (t, t + DAY):  # посадка после полуночи на рейсе, начатом накануне
            first, last = tr.dep[0], tr.dep[-1]
            if first - tolerance <= tt <= last + tolerance:
                gap = max(first - tt, tt - last, 0.0)  # 0 — внутри рейса, иначе удалённость от него
                if best_gap is None or gap < best_gap:
                    i = int(np.searchsorted(tr.dep, tt + slack, side="right")) - 1
                    best, best_gap = (tr, max(i, 0)), gap
    return best


def bind(v: pd.DataFrame, trips: dict[tuple[int, str], list[Trip]], tolerance: float = 3.0,
         slack: float = 0.5, naryad: pd.DataFrame | None = None) -> pd.DataFrame:
    """Статус и остановка для каждой посадки (результат нормализации normalize)."""
    v = v.copy()
    n = len(v)
    status = np.full(n, "bad_record", dtype=object)
    cols = {c: np.full(n, None, dtype=object) for c in ("trip_id", "direction", "seq", "stop_id", "stop_name")}
    sched_routes = {r for r, _ in trips}
    ok = (v["route"].notna() & v["grafic"].notna() & v["tmin"].notna()).to_numpy()
    route = v["route"].to_numpy()
    grafic = v["grafic"].to_numpy()
    tmin = v["tmin"].to_numpy()
    for k in range(n):
        if not ok[k]:
            continue
        r, g = int(route[k]), grafic[k]
        if r not in sched_routes:
            status[k] = "no_schedule_for_route"
            continue
        lst = trips.get((r, g))
        if not lst:
            status[k] = "no_schedule_for_grafic"
            continue
        hit = locate(lst, float(tmin[k]), tolerance, slack)
        if hit is None:
            status[k] = "outside_trips"
            continue
        tr, i = hit
        status[k] = "bound"
        cols["trip_id"][k], cols["direction"][k], cols["seq"][k] = tr.trip_id, tr.direction, tr.seqs[i]
        cols["stop_id"][k], cols["stop_name"][k] = tr.stop_ids[i], tr.stop_names[i]
    v["status"] = status
    for c, a in cols.items():
        v[c] = a
    v["vehicle_check"] = vehicle_check(v, naryad)
    return v


def vehicle_check(v: pd.DataFrame, naryad: pd.DataFrame | None) -> pd.Series:
    """match / mismatch — бортовой номер совпал / не совпал с нарядом на эту дату; no_naryad — даты нет в наряде."""
    res = pd.Series("no_naryad", index=v.index, dtype=object)
    res[v["status"] != "bound"] = None
    if naryad is None or naryad.empty:
        return res
    garages = naryad.groupby(["date", "route", "grafic"])["garage"].apply(set).to_dict()
    b = v["status"] == "bound"
    for idx, d, r, g, gar in zip(v.index[b], v["date"][b], v["route"][b], v["grafic"][b], v["garage"][b]):
        s = garages.get((d, int(r), g))
        if s is not None:
            res[idx] = "match" if gar in s else "mismatch"
    return res


# ---------------------------------------------------------------- отчёт и доли
def report(bound: pd.DataFrame, rows_total: int, not_boarding: int, params: dict) -> dict:
    by_status = {s: int((bound["status"] == s).sum()) for s in STATUSES}
    boardings = int(len(bound))
    by_route = {}
    for r, g in bound.dropna(subset=["route"]).groupby("route"):
        c = g["status"].value_counts().to_dict()
        by_route[str(int(r))] = dict(boardings=int(len(g)), bound=int(c.get("bound", 0)),
                                     share_bound=round(c.get("bound", 0) / len(g), 6),
                                     **{s: int(c.get(s, 0)) for s in STATUSES[1:]})
    return dict(params=params, rows_total=rows_total, not_boarding=not_boarding, boardings=boardings,
                by_status=by_status, share_bound=round(by_status["bound"] / boardings, 6) if boardings else 0.0,
                vehicle_check=bound["vehicle_check"].dropna().value_counts().to_dict(), by_route=by_route)


def aggregate(bound: pd.DataFrame) -> pd.DataFrame:
    """Привязанные посадки → маршрут × направление × остановка × час."""
    b = bound[bound["status"] == "bound"]
    return (b.groupby(["route", "direction", "stop_id", "stop_name", "hour"]).size().rename("boardings")
            .reset_index().astype({"route": int, "direction": int, "hour": int}))


def stop_shares(bound: pd.DataFrame, route_stops: pd.DataFrame, min_bound: int = 1000,
                min_stop_share: float = 0.8) -> tuple[pd.DataFrame, dict]:
    """Доли остановок в формате stop_weights.csv (route, stop_id, weight) по маршрутам с достаточным покрытием.

    route_stops — строки остановок сервиса (route, stop_id), как в stops.csv. Сумма weight по строкам
    маршрута = 1: доля остановки делится на число её строк (конечная бывает в обоих направлениях).
    Маршрут без покрытия не попадает в результат — у него остаются априорные веса.
    """
    b = bound[bound["status"] == "bound"]
    rows, cover = [], {}
    for r, rs in route_stops.groupby("route"):
        rb = b[b["route"] == r]
        cnt = rb["stop_id"].value_counts()
        ids = rs["stop_id"].astype(str)
        known = cnt[cnt.index.isin(set(ids))]
        observed = ids[ids.isin(known.index)].nunique() / max(ids.nunique(), 1)
        passed = bool(known.sum() >= min_bound and observed >= min_stop_share)
        cover[str(int(r))] = dict(bound=int(len(rb)), bound_on_service_stops=int(known.sum()),
                                  stops=int(ids.nunique()), stops_observed_share=round(float(observed), 4),
                                  weights_written=passed)
        if not passed:
            continue
        nrows = ids.value_counts()
        for sid in ids.unique():
            rows.append(dict(route=int(r), stop_id=sid,
                             weight=round(float(known.get(sid, 0)) / known.sum() / nrows[sid], 6)))
    return pd.DataFrame(rows, columns=["route", "stop_id", "weight"]), cover


def write_weights(w: pd.DataFrame, path: Path) -> None:
    """Слияние с существующим файлом: строки прошедших порог маршрутов заменяются, остальные сохраняются."""
    if path.exists():
        old = pd.read_csv(path, dtype={"stop_id": str})
        w = pd.concat([old[~old["route"].isin(w["route"].unique())], w], ignore_index=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    w.sort_values(["route", "stop_id"]).to_csv(path, index=False)


def service_route_stops(xlsx: Path, sched: pd.DataFrame) -> pd.DataFrame:
    """Остановки сервиса (лист «Порядок_с_координатами», как в build_stops); нет листа — остановки расписания."""
    try:
        st, _ = load_reference_stops(xlsx)
        return pd.DataFrame({"route": st["route"].astype(int), "stop_id": st["stop_id"].astype(str)})
    except (ValueError, FileNotFoundError, KeyError):
        return sched[["route", "stop_id"]].drop_duplicates()


# ---------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--validations", required=True, type=Path)
    ap.add_argument("--schedule", default="data/reference/tram_reference.xlsx", type=Path)
    ap.add_argument("--stops-ref", default="data/reference/tram_reference.xlsx", type=Path,
                    help="справочник с листом «Порядок_с_координатами» (остановки сервиса)")
    ap.add_argument("--tolerance", default=3.0, type=float, help="мин до первого / после последнего отправления рейса")
    ap.add_argument("--slack", default=0.5, type=float, help="мин: посадка чуть раньше отправления — ещё эта остановка")
    ap.add_argument("--report", type=Path)
    ap.add_argument("--bound-out", type=Path, help="CSV привязанных посадок")
    ap.add_argument("--agg-out", type=Path, help="CSV маршрут × направление × остановка × час")
    ap.add_argument("--write-weights", type=Path, help="куда писать stop_weights.csv (по умолчанию не пишется)")
    ap.add_argument("--min-bound", default=1000, type=int)
    ap.add_argument("--min-stop-share", default=0.8, type=float)
    a = ap.parse_args(argv)

    sched = load_schedule(a.schedule)
    trips = build_trips(sched)
    naryad = load_naryad(a.schedule) if a.schedule.suffix.lower() in (".xlsx", ".xls") else None
    log(f"расписание: {len(sched)} строк, {sum(map(len, trips.values()))} рейсов, "
        f"{len(trips)} графиков, маршруты {sorted({r for r, _ in trips})}")

    parts, rows_total, not_boarding = [], 0, 0
    for chunk in read_validations(a.validations):
        rows_total += len(chunk)
        nv = normalize(chunk)
        not_boarding += len(chunk) - len(nv)
        parts.append(bind(nv, trips, a.tolerance, a.slack, naryad))
    res = pd.concat(parts, ignore_index=True) if parts else bind(normalize(pd.DataFrame(columns=VCOLS)), trips)

    params = dict(validations=a.validations.name, schedule=a.schedule.name, schedule_rows=int(len(sched)),
                  tolerance_min=a.tolerance, slack_min=a.slack, min_bound=a.min_bound,
                  min_stop_share=a.min_stop_share,
                  schedule_grafics={str(r): sorted({g for rr, g in trips if rr == r}) for r in sorted({r for r, _ in trips})})
    rep = report(res, rows_total, not_boarding, params)
    w, cover = stop_shares(res, service_route_stops(a.stops_ref, sched), a.min_bound, a.min_stop_share)
    rep["weights_coverage"] = {r: c for r, c in cover.items() if c["bound"] or r in rep["by_route"]}
    log(f"строк {rows_total}, посадок {rep['boardings']}, привязано {rep['by_status']['bound']} "
        f"({rep['share_bound']:.2%}); статусы {rep['by_status']}")

    b = res[res["status"] == "bound"]
    if a.bound_out:
        b.to_csv(a.bound_out, index=False)
    if a.agg_out:
        aggregate(res).to_csv(a.agg_out, index=False)
    if a.write_weights:
        if len(w):
            write_weights(w, a.write_weights)
            log(f"доли остановок → {a.write_weights}: маршруты {sorted(w['route'].unique().tolist())}")
        else:
            log(f"ни один маршрут не прошёл порог (≥ {a.min_bound} посадок и ≥ {a.min_stop_share:.0%} остановок) — "
                f"{a.write_weights} не изменён, остаются априорные веса")
    if a.report:
        a.report.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"отчёт → {a.report}")
    return rep


if __name__ == "__main__":
    main()
