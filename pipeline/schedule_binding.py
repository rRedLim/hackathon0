"""Геопривязка посадок к остановкам по расписанию (выход/график → рейс → остановка).

В валидациях нет остановки (place_id — площадка/депо), но есть маршрут (ngpt_route), выход
(bus_exit_no = график, grafic в расписании), бортовой номер (garage_number) и время. По расписанию
выхода восстанавливается, у какой остановки был вагон в момент валидации:

  1. расписание (лист «Расписание» справочника или CSV той же структуры) → по (маршрут, график)
     список рейсов с упорядоченными остановками и временем отправления в минутах суток.
     Рейс — составной ключ (маршрут, график, service_id, trip_id, trip_num, shift_num) из тех колонок,
     что есть: trip_id у организаторов — шаблон рейса (вариант маршрута/направления), повторяется для всех
     рейсов графика в этом направлении, сам рейс различает trip_num. Если колонки trip_num нет, а номера
     остановок в группе повторяются, группа делится на рейсы по сбросу stop_sequence (в порядке строк файла).
     Времена рейса по порядку остановок должны не убывать, допускается один переход через полночь
     (падение больше 12 ч: 23:55 → 00:05; далее +1440 мин; «24:10» тоже понимается). Немонотонный рейс
     исключается и считается в отчёте (schedule_checks.trips_non_monotonic);
  2. посадка = validation_result == 1, маршрут — число из ngpt_route, время — tran_date_time
     (так же, как ingest ML и IngestService backend);
  3. тип дня: если в расписании есть service_id, берутся только рейсы, действующие в дату посадки
     (для посадки после полуночи на рейсе, начатом накануне, — в предыдущую дату). Правило service_id:
     --calendar (CSV как GTFS calendar.txt: service_id, monday…sunday = 0/1, необязательные
     start_date/end_date YYYYMMDD) → иначе эвристика по названию (будни/weekday/рабочие → пн–пт,
     выходные/weekend/нерабочие → сб–вс, суббота/sat/сб → сб, воскресенье/sun/вс → вс,
     ежедневно/daily → все дни) → иначе (например, числовой 3172953) — действует во все дни.
     Праздники не учитываются;
  4. кандидаты — рейсы графика, у которых [первое отправление − tolerance, последнее + tolerance]
     содержит время посадки; выбирается ближайший; остановка — последняя, отправление с которой
     ≤ время + slack (вагон стоит у неё или только что отошёл). Если время попадает внутрь нескольких
     рейсов (gap = 0) и они дают разные (направление, остановку), посадка получает статус ambiguous
     (в доли остановок не идёт). Рейс, закончившийся ровно в момент посадки, уступает начинающемуся;
  5. если дата посадки есть в «Наряде», бортовой номер сверяется с вагоном графика (vehicle_check).

Расписание — шаблон по времени суток: дата посадки с датой расписания не сопоставляется (в справочнике
расписание и наряд — на 2026 год, валидации — 2025); по дате определяется только день недели.

Каждая посадка получает статус: bound / ambiguous / no_schedule_for_route / no_schedule_for_grafic /
outside_trips / bad_record. Отчёт (JSON) — итоги по статусам и маршрутам, проверки расписания
(schedule_checks) и правило каждого service_id (service_mapping). Доли остановок пишутся в stop_weights.csv
(формат, который читает build_service_data.build_stops) только по маршрутам с достаточным покрытием и
только по явному флагу --write-weights.

Запуск:
  python pipeline/schedule_binding.py --validations FILE [--schedule data/reference/tram_reference.xlsx]
      [--calendar calendar.csv] [--tolerance 3] [--slack 0.5] [--report out.json] [--bound-out bound.csv]
      [--agg-out agg.csv] [--write-weights PATH] [--min-bound 1000] [--min-stop-share 0.8]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_service_data import load_reference_stops  # noqa: E402

STATUSES = ["bound", "no_schedule_for_route", "no_schedule_for_grafic", "outside_trips", "ambiguous", "bad_record"]
VCOLS = ["tran_date_time", "validation_result", "ngpt_route", "bus_exit_no", "garage_number"]
DAY = 1440
KEY = ["service_id", "trip_id", "trip_num", "shift_num"]  # рейс внутри (маршрут, график); нет колонки — ""
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
AMBIGUOUS = "ambiguous"
# эвристика service_id → дни недели (0 = пн), если нет --calendar; проверяется по порядку, дни объединяются
_L = r"(?<![a-zа-яё])"  # граница слова без учёта «_» и цифр: sat_sun, будни2025
SERVICE_PATTERNS = [
    (r"нерабоч|non.?work", {5, 6}),
    (r"weekday|workday|будн|(?<!не)рабоч", {0, 1, 2, 3, 4}),
    (r"weekend|выходн", {5, 6}),
    (rf"saturday|суббот|{_L}sat(?![a-z])|{_L}сб(?![а-яё])", {5}),
    (rf"sunday|воскрес|{_L}sun(?![a-z])|{_L}вс(?![а-яё])", {6}),
    (r"daily|every.?day|ежеднев", set(range(7))),
]


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
    service_id: str = ""  # "" — колонки нет: рейс действует во все дни
    trip_num: str = ""
    shift_num: str = ""


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


def id_key(v) -> str:
    """Идентификатор как строка: 3172953, '3172953', 3172953.0 → '3172953'; пусто/NaN → ''."""
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return ""
    s = str(v).strip()
    return s[:-2] if s.endswith(".0") and s[:-2].isdigit() else s


def load_schedule(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(path, sheet_name="Расписание", header=1)
    else:
        df = pd.read_csv(path, sep=None, engine="python")
    df = df.rename(columns={"route_short_name": "route", "direction_id": "direction", "stop_sequence": "seq"})
    for c, default in (("direction", 0), ("stop_name", ""), ("service_id", ""), ("trip_num", ""), ("shift_num", "")):
        if c not in df:
            df[c] = default
    out = pd.DataFrame({
        "route": pd.to_numeric(df["route"], errors="coerce"),
        "grafic": df["grafic"].map(grafic_key),
        **{c: df[c].map(id_key) for c in KEY},
        "direction": pd.to_numeric(df["direction"], errors="coerce").fillna(0).astype(int),
        "seq": pd.to_numeric(df["seq"], errors="coerce"),
        "stop_id": df["stop_id"].astype(str).str.replace(r"\.0$", "", regex=True),
        "stop_name": df["stop_name"].fillna("").astype(str),
        "dep": df["departure_time"].map(hhmm_to_min),
    })
    out = out.dropna(subset=["route", "grafic", "seq", "dep"])
    out["route"] = out["route"].astype(int)
    out["seq"] = out["seq"].astype(int)
    # порядок строк внутри рейса сохраняется (нужен для деления повторённого шаблона), остановки сортирует build_trips
    return out.sort_values(["route", "grafic", *KEY], kind="stable").reset_index(drop=True)


def unwrap_midnight(dep: np.ndarray) -> np.ndarray | None:
    """Времена по порядку остановок → неубывающие минуты; не более одного перехода через полночь
    (падение больше 12 ч, дальше +1440). None — рейс немонотонный (время убывает иначе)."""
    dep = np.asarray(dep, dtype=float).copy()
    wrapped = False
    for i in range(1, len(dep)):
        if dep[i] < dep[i - 1] - 1e-9:
            if wrapped or dep[i - 1] - dep[i] < DAY / 2:
                return None
            dep[i:] += DAY
            wrapped = True
    return dep


def build_trips(sched: pd.DataFrame, stats: dict | None = None) -> dict[tuple[int, str], list[Trip]]:
    """(маршрут, график) → рейсы по времени начала. stats (если передан) заполняется итогами проверок."""
    trips: dict[tuple[int, str], list[Trip]] = {}
    st = dict(trip_key_columns=["route", "grafic"] + [c for c in KEY if (sched[c] != "").any()],
              trips=0, template_groups_split=0, trips_from_split=0, trips_non_monotonic=0,
              non_monotonic_examples=[])
    for (route, grafic, *key), g in sched.groupby(["route", "grafic", *KEY], sort=False):
        segs = [g]
        seq = g["seq"].to_numpy()
        if len(np.unique(seq)) < len(seq):  # шаблон повторён без trip_num: новый рейс — сброс номера остановки
            cut = (np.flatnonzero(seq[1:] <= seq[:-1]) + 1).tolist()
            segs = [g.iloc[a:b] for a, b in zip([0, *cut], [*cut, len(g)])]
            st["template_groups_split"] += 1
            st["trips_from_split"] += len(segs)
        for s in segs:
            s = s.sort_values("seq", kind="stable")
            dep = unwrap_midnight(s["dep"].to_numpy(dtype=float))
            if dep is None:
                st["trips_non_monotonic"] += 1
                if len(st["non_monotonic_examples"]) < 10:
                    st["non_monotonic_examples"].append("/".join([str(route), grafic, *key]))
                continue
            service_id, trip_id, trip_num, shift_num = key
            trips.setdefault((route, grafic), []).append(Trip(
                route, grafic, trip_id, int(s["direction"].iloc[0]), s["stop_id"].tolist(),
                s["stop_name"].tolist(), s["seq"].tolist(), dep, service_id, trip_num, shift_num))
            st["trips"] += 1
    for lst in trips.values():
        lst.sort(key=lambda t: t.dep[0])
    if st["trips_non_monotonic"]:
        log(f"внимание: {st['trips_non_monotonic']} рейсов с немонотонным временем исключены, "
            f"например {st['non_monotonic_examples'][:3]}")
    if stats is not None:
        stats.update(st)
    return trips


# ---------------------------------------------------------------- тип дня (service_id)
def service_days(service_id: str) -> frozenset[int] | None:
    """Эвристика: дни недели (0 = пн) по названию service_id; None — не распознан (действует во все дни)."""
    s = str(service_id).lower()
    days = set()
    for pat, d in SERVICE_PATTERNS:
        if re.search(pat, s):
            days |= d
    return frozenset(days) if days else None


def _parse_date(x) -> date | None:
    x = id_key(x).replace("-", "")
    return datetime.strptime(x[:8], "%Y%m%d").date() if x else None


def load_calendar(path: Path) -> dict[str, tuple[frozenset[int], date | None, date | None]]:
    """GTFS-подобный calendar: service_id, monday…sunday (0/1), необязательные start_date/end_date."""
    c = pd.read_csv(path, sep=None, engine="python", dtype=str)
    c.columns = c.columns.str.strip().str.lower()
    missing = [col for col in ["service_id", *WEEKDAYS] if col not in c]
    if missing:
        raise SystemExit(f"в календаре {path} нет колонок {missing}")
    out = {}
    for r in c.to_dict("records"):
        days = frozenset(i for i, w in enumerate(WEEKDAYS) if id_key(r[w]).lower() in ("1", "true"))
        out[id_key(r["service_id"])] = (days, _parse_date(r.get("start_date")), _parse_date(r.get("end_date")))
    return out


def service_active(service_id: str, day: date, calendar: dict | None = None) -> bool:
    """Действует ли рейс с этим service_id в дату day: календарь → эвристика → во все дни."""
    if not service_id:
        return True
    if calendar and service_id in calendar:
        days, start, end = calendar[service_id]
        return day.weekday() in days and (start is None or day >= start) and (end is None or day <= end)
    days = service_days(service_id)
    return days is None or day.weekday() in days


def service_mapping(trips: dict[tuple[int, str], list[Trip]], calendar: dict | None = None) -> dict:
    """Для отчёта: по каждому service_id расписания — источник правила и дни недели."""
    out = {}
    for sid in sorted({t.service_id for lst in trips.values() for t in lst if t.service_id}):
        if calendar and sid in calendar:
            days, start, end = calendar[sid]
            out[sid] = dict(source="calendar", days=[WEEKDAYS[d] for d in sorted(days)],
                            start_date=start and start.isoformat(), end_date=end and end.isoformat())
        elif (days := service_days(sid)) is not None:
            out[sid] = dict(source="heuristic", days=[WEEKDAYS[d] for d in sorted(days)])
        else:
            out[sid] = dict(source="all_days", days=WEEKDAYS)
    return out


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
def locate(trips: list[Trip], t: float, tolerance: float, slack: float,
           active=None) -> tuple[Trip, int] | str | None:
    """Рейс и индекс остановки для минуты суток t; None — вне всех рейсов графика; AMBIGUOUS — время внутри
    нескольких рейсов с разными (направление, остановка). active(service_id, сдвиг дней) → bool фильтрует
    рейсы по типу дня (сдвиг −1 — рейс начат накануне)."""
    best, best_gap, inside = None, None, []
    for tr in trips:
        for off, tt in ((0, t), (-1, t + DAY)):  # посадка после полуночи на рейсе, начатом накануне
            if active is not None and not active(tr.service_id, off):
                continue
            first, last = tr.dep[0], tr.dep[-1]
            if first - tolerance <= tt <= last + tolerance:
                gap = max(first - tt, tt - last, 0.0)  # 0 — внутри рейса, иначе удалённость от него
                i = max(int(np.searchsorted(tr.dep, tt + slack, side="right")) - 1, 0)
                if gap == 0:
                    inside.append((tr, i, tt))
                if best_gap is None or gap < best_gap:
                    best, best_gap = (tr, i), gap
    if len(inside) > 1:
        # рейс, закончившийся ровно сейчас, уступает начинающемуся (вагон уже на следующем рейсе)
        running = [c for c in inside if c[2] < c[0].dep[-1]] or inside
        if len({(tr.direction, tr.stop_ids[i]) for tr, i, _ in running}) > 1:
            return AMBIGUOUS
        return running[0][0], running[0][1]
    return best


def bind(v: pd.DataFrame, trips: dict[tuple[int, str], list[Trip]], tolerance: float = 3.0,
         slack: float = 0.5, naryad: pd.DataFrame | None = None, calendar: dict | None = None) -> pd.DataFrame:
    """Статус и остановка для каждой посадки (результат нормализации normalize).

    calendar — результат load_calendar; None — эвристика по названию service_id (см. service_active)."""
    v = v.copy()
    n = len(v)
    status = np.full(n, "bad_record", dtype=object)
    cols = {c: np.full(n, None, dtype=object)
            for c in ("trip_id", "trip_num", "service_id", "direction", "seq", "stop_id", "stop_name")}
    sched_routes = {r for r, _ in trips}
    ok = (v["route"].notna() & v["grafic"].notna() & v["tmin"].notna()).to_numpy()
    route = v["route"].to_numpy()
    grafic = v["grafic"].to_numpy()
    tmin = v["tmin"].to_numpy()
    dates = v["date"].to_numpy()
    has_service = any(tr.service_id for lst in trips.values() for tr in lst)
    cache: dict[tuple[str, str, int], bool] = {}

    def day_filter(d: str):
        def active(sid: str, off: int) -> bool:
            k = (sid, d, off)
            if k not in cache:
                cache[k] = service_active(sid, date.fromisoformat(d) + timedelta(days=off), calendar)
            return cache[k]
        return active
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
        hit = locate(lst, float(tmin[k]), tolerance, slack, day_filter(dates[k]) if has_service else None)
        if hit is None:
            status[k] = "outside_trips"
            continue
        if hit == AMBIGUOUS:
            status[k] = AMBIGUOUS
            continue
        tr, i = hit
        status[k] = "bound"
        cols["trip_id"][k], cols["direction"][k], cols["seq"][k] = tr.trip_id, tr.direction, tr.seqs[i]
        cols["trip_num"][k], cols["service_id"][k] = tr.trip_num, tr.service_id
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
    ap.add_argument("--calendar", type=Path,
                    help="CSV как GTFS calendar.txt: service_id, monday…sunday (0/1), [start_date, end_date]; "
                         "без него — эвристика по названию service_id, нераспознанный действует во все дни")
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
    checks: dict = {}
    trips = build_trips(sched, checks)
    calendar = load_calendar(a.calendar) if a.calendar else None
    naryad = load_naryad(a.schedule) if a.schedule.suffix.lower() in (".xlsx", ".xls") else None
    log(f"расписание: {len(sched)} строк, {sum(map(len, trips.values()))} рейсов, "
        f"{len(trips)} графиков, маршруты {sorted({r for r, _ in trips})}")

    parts, rows_total, not_boarding = [], 0, 0
    for chunk in read_validations(a.validations):
        rows_total += len(chunk)
        nv = normalize(chunk)
        not_boarding += len(chunk) - len(nv)
        parts.append(bind(nv, trips, a.tolerance, a.slack, naryad, calendar))
    res = pd.concat(parts, ignore_index=True) if parts else bind(normalize(pd.DataFrame(columns=VCOLS)), trips)

    params = dict(validations=a.validations.name, schedule=a.schedule.name, schedule_rows=int(len(sched)),
                  calendar=a.calendar.name if a.calendar else None,
                  tolerance_min=a.tolerance, slack_min=a.slack, min_bound=a.min_bound,
                  min_stop_share=a.min_stop_share,
                  schedule_grafics={str(r): sorted({g for rr, g in trips if rr == r}) for r in sorted({r for r, _ in trips})})
    rep = report(res, rows_total, not_boarding, params)
    rep["schedule_checks"] = checks
    rep["service_mapping"] = service_mapping(trips, calendar)
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
