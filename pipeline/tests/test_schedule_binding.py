"""Тесты геопривязки посадок по расписанию: python -m pytest pipeline/tests -q"""
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "pipeline"))
import schedule_binding as sb  # noqa: E402

XLSX = ROOT / "data" / "reference" / "tram_reference.xlsx"


def sched_df() -> pd.DataFrame:
    """Маршрут 7, график 301: рейс A (прямое, 10:00…10:20) и рейс B (обратное, 23:50…00:10 через полночь)."""
    rows = []
    for i, t in enumerate(["10:00", "10:05", "10:10", "10:15", "10:20"], 1):
        rows.append(dict(route_short_name=7, grafic=301, trip_id="A", direction_id=0, stop_sequence=i,
                         stop_id=100 + i, stop_name=f"S{i}", departure_time=t))
    for i, t in enumerate(["23:50", "23:55", "00:00", "00:05", "00:10"], 1):
        rows.append(dict(route_short_name=7, grafic=301, trip_id="B", direction_id=1, stop_sequence=i,
                         stop_id=200 + i, stop_name=f"R{i}", departure_time=t))
    return pd.DataFrame(rows)


@pytest.fixture
def trips(tmp_path):
    p = tmp_path / "sched.csv"
    sched_df().to_csv(p, index=False)
    return sb.build_trips(sb.load_schedule(p))


def raw(rows):
    """rows: (время, validation_result, ngpt_route, bus_exit_no[, garage])."""
    return pd.DataFrame([dict(tran_date_time=r[0], validation_result=str(r[1]), ngpt_route=r[2],
                              bus_exit_no=str(r[3]), garage_number=str(r[4] if len(r) > 4 else 31001))
                         for r in rows])


def run(trips, rows, **kw):
    return sb.bind(sb.normalize(raw(rows)), trips, **kw)


def test_schedule_and_midnight(trips):
    a, b = trips[(7, "301")]
    assert (a.trip_id, b.trip_id) == ("A", "B")
    assert a.dep.tolist() == [600, 605, 610, 615, 620]
    assert b.dep.tolist() == [1430, 1435, 1440, 1445, 1450]  # после полуночи — продолжение за 1440


@pytest.mark.parametrize("ts, stop", [
    ("2025-10-31 10:00:00", "101"),   # ровно отправление с начальной
    ("2025-10-31 10:05:00", "102"),   # ровно отправление со 2-й
    ("2025-10-31 10:07:30", "102"),   # между 2-й и 3-й — вагон только что отошёл от 2-й
    ("2025-10-31 10:09:45", "103"),   # за 15 с до отправления (slack 0.5 мин) — уже у 3-й
    ("2025-10-31 09:58:00", "101"),   # до первого отправления, в пределах tolerance
    ("2025-10-31 10:22:00", "105"),   # после последнего, в пределах tolerance
    ("2025-10-31 23:57:00", "202"),   # рейс через полночь, до полуночи
    ("2025-11-01 00:01:00", "203"),   # рейс через полночь, после полуночи
    ("2025-11-01 00:12:00", "205"),   # после конечной рейса через полночь, в пределах tolerance
])
def test_bound_stop(trips, ts, stop):
    r = run(trips, [(ts, 1, "7 трамвай", 301)])
    assert r["status"].tolist() == ["bound"]
    assert r["stop_id"].iloc[0] == stop


def test_statuses(trips):
    r = run(trips, [
        ("2025-10-31 09:50:00", 1, "7 трамвай", 301),    # до рейса больше tolerance
        ("2025-10-31 12:00:00", 1, "7 трамвай", 301),    # между рейсами
        ("2025-10-31 10:05:00", 1, "7 трамвай", 999),    # неизвестный график
        ("2025-10-31 10:05:00", 1, "11 трамвай", 301),   # маршрута нет в расписании
        ("мусор", 1, "7 трамвай", 301),                  # время не разбирается
        ("2025-10-31 10:05:00", 1, "трамвай", 301),      # маршрут не разбирается
        ("2025-10-31 10:05:00", 1, "7 трамвай", ""),     # нет выхода
    ])
    assert r["status"].tolist() == ["outside_trips", "outside_trips", "no_schedule_for_grafic",
                                    "no_schedule_for_route", "bad_record", "bad_record", "bad_record"]
    assert r["stop_id"].isna().all()


def test_failed_validation_is_not_boarding(trips):
    v = sb.normalize(raw([("2025-10-31 10:05:00", 90, "7 трамвай", 301),
                          ("2025-10-31 10:05:00", 1, "7 трамвай", 301)]))
    assert len(v) == 1


def test_tolerance_param(trips):
    r = run(trips, [("2025-10-31 09:58:00", 1, "7 трамвай", 301)], tolerance=1)
    assert r["status"].iloc[0] == "outside_trips"


def test_vehicle_check(trips):
    nar = pd.DataFrame(dict(date=["2025-10-31"], route=[7], grafic=["301"], garage=["31001"]))
    r = sb.bind(sb.normalize(raw([("2025-10-31 10:05:00", 1, "7 трамвай", 301, 31001),
                                  ("2025-10-31 10:06:00", 1, "7 трамвай", 301, 31555),
                                  ("2025-10-30 10:05:00", 1, "7 трамвай", 301, 31001),
                                  ("2025-10-31 12:00:00", 1, "7 трамвай", 301, 31001)])), trips, naryad=nar)
    assert r["vehicle_check"].tolist() == ["match", "mismatch", "no_naryad", None]


def test_report_and_aggregate(trips):
    r = run(trips, [("2025-10-31 10:05:00", 1, "7 трамвай", 301), ("2025-10-31 10:06:00", 1, "7 трамвай", 301),
                    ("2025-10-31 12:00:00", 1, "7 трамвай", 301), ("2025-10-31 10:05:00", 1, "11 трамвай", 1)])
    rep = sb.report(r, rows_total=5, not_boarding=1, params={})
    assert rep["by_status"]["bound"] == 2 and rep["boardings"] == 4 and rep["share_bound"] == 0.5
    assert rep["by_route"]["7"]["bound"] == 2 and rep["by_route"]["11"]["no_schedule_for_route"] == 1
    agg = sb.aggregate(r)
    assert agg.to_dict("records") == [dict(route=7, direction=0, stop_id="102", stop_name="S2", hour=10, boardings=2)]


def weights_case(trips, n_per_stop, stops):
    rows = [(f"2025-10-31 10:{m:02d}:10", 1, "7 трамвай", 301) for m in stops for _ in range(n_per_stop)]
    route_stops = pd.DataFrame(dict(route=7, stop_id=[str(s) for s in [101, 102, 103, 104, 105, 101]]))
    return sb.stop_shares(run(trips, rows), route_stops, min_bound=10, min_stop_share=0.8)


def test_weights_threshold_passed(trips, tmp_path):
    w, cover = weights_case(trips, 5, [0, 5, 10, 15])  # 4 из 5 остановок = 80 %, 20 посадок
    assert cover["7"]["weights_written"]
    # сумма по строкам маршрута (101 — в двух направлениях) = 1
    rows = pd.DataFrame(dict(stop_id=["101", "102", "103", "104", "105", "101"]))
    total = rows.merge(w, on="stop_id")["weight"].sum()
    assert total == pytest.approx(1.0, abs=1e-5)
    assert w.set_index("stop_id")["weight"].to_dict() == {"101": 0.125, "102": 0.25, "103": 0.25,
                                                         "104": 0.25, "105": 0.0}
    # слияние: строки других маршрутов в существующем файле сохраняются
    p = tmp_path / "stop_weights.csv"
    pd.DataFrame(dict(route=[1, 7], stop_id=["x", "old"], weight=[1.0, 1.0])).to_csv(p, index=False)
    sb.write_weights(w, p)
    out = pd.read_csv(p, dtype={"stop_id": str})
    assert set(out["route"]) == {1, 7} and "old" not in set(out["stop_id"]) and len(out) == 6


@pytest.mark.parametrize("n, stops", [(5, [0, 5, 10]),     # 60 % остановок — мало
                                       (2, [0, 5, 10, 15])])  # 8 посадок < 10 — мало
def test_weights_threshold_failed(trips, n, stops):
    w, cover = weights_case(trips, n, stops)
    assert w.empty and not cover["7"]["weights_written"]


def test_cli_does_not_write_weights_without_coverage(tmp_path):
    v = tmp_path / "v.csv"
    raw([("2025-10-31 06:55:10", 1, "1 трамвай", 206), ("2025-10-31 06:55:20", 90, "1 трамвай", 206)]) \
        .assign(tran_no=1)[["tran_no"] + sb.VCOLS].to_csv(v, sep=";", index=False)
    out = tmp_path / "w.csv"
    rep = sb.main(["--validations", str(v), "--schedule", str(XLSX), "--stops-ref", str(XLSX),
                   "--write-weights", str(out), "--report", str(tmp_path / "r.json")])
    assert rep["rows_total"] == 2 and rep["not_boarding"] == 1 and rep["by_status"]["bound"] == 1
    assert not out.exists()  # 1 посадка < порога — файл не создаётся, априорные веса остаются


def test_real_sample_schedule():
    sched = sb.load_schedule(XLSX)
    assert len(sched) == 15 and set(sched["route"]) == {1} and set(sched["grafic"]) == {"206"}
    trips = sb.build_trips(sched)
    (tr,) = trips[(1, "206")]
    assert len(tr.stop_ids) == 15 and tr.dep[0] == 6 * 60 + 53 and tr.dep[-1] == 7 * 60 + 11
    r = sb.bind(sb.normalize(raw([("2025-10-31 07:00:30", 1, "1 трамвай", 206)])), trips)
    assert r["stop_name"].iloc[0] == "Чертаново Центральное"


# ---------------------------------------------------------------- составной ключ рейса, тип дня, ambiguous
def trip_rows(times, stop0, route=7, grafic=301, **extra):
    """Строки одного рейса: остановки stop0+1… с отправлениями times; extra — trip_id, trip_num, service_id…"""
    return [dict(route_short_name=route, grafic=grafic, stop_sequence=i, stop_id=stop0 + i, stop_name=f"N{stop0 + i}",
                 departure_time=t, **{"direction_id": 0, **extra}) for i, t in enumerate(times, 1)]


def make_trips(tmp_path, rows, stats=None):
    p = tmp_path / "sched.csv"
    pd.DataFrame(rows).to_csv(p, index=False)
    return sb.build_trips(sb.load_schedule(p), stats)


MORNING = ["10:00", "10:05", "10:10", "10:15", "10:20"]
NOON = ["12:00", "12:05", "12:10", "12:15", "12:20"]


def test_repeated_trip_id_distinct_trip_num(tmp_path):
    # формат организаторов: trip_id — шаблон рейса, повторяется; рейсы различает trip_num (строки вперемешку)
    rows = trip_rows(NOON, 100, trip_id=2040920, trip_num=2, shift_num=1, service_id=3172953) \
        + trip_rows(MORNING, 100, trip_id=2040920, trip_num=1, shift_num=1, service_id=3172953)
    stats = {}
    trips = make_trips(tmp_path, rows[::-1], stats)
    a, b = trips[(7, "301")]
    assert (a.trip_id, a.trip_num, b.trip_num) == ("2040920", "1", "2")
    assert a.dep.tolist() == [600, 605, 610, 615, 620] and b.dep.tolist() == [720, 725, 730, 735, 740]
    assert stats["trips"] == 2 and stats["trips_non_monotonic"] == 0
    assert stats["trip_key_columns"] == ["route", "grafic", "service_id", "trip_id", "trip_num", "shift_num"]
    r = run(trips, [("2025-10-31 10:10:00", 1, "7 трамвай", 301), ("2025-10-31 12:07:30", 1, "7 трамвай", 301),
                    ("2025-10-31 11:00:00", 1, "7 трамвай", 301)])
    assert r["status"].tolist() == ["bound", "bound", "outside_trips"]
    assert r["stop_id"].tolist()[:2] == ["103", "102"] and r["trip_num"].tolist()[:2] == ["1", "2"]


def test_repeated_trip_id_without_trip_num_is_split(tmp_path):
    # нет trip_num, trip_id повторён: рейсы разделяются по сбросу stop_sequence, а не склеиваются в один
    stats = {}
    trips = make_trips(tmp_path, trip_rows(MORNING, 100, trip_id="T") + trip_rows(NOON, 100, trip_id="T"), stats)
    assert [t.dep[0] for t in trips[(7, "301")]] == [600, 720]
    assert stats["template_groups_split"] == 1 and stats["trips_from_split"] == 2
    r = run(trips, [("2025-10-31 12:10:00", 1, "7 трамвай", 301)])
    assert r["stop_id"].tolist() == ["103"]


def test_non_monotonic_trip_excluded(tmp_path):
    stats = {}
    rows = trip_rows(["10:00", "10:05", "09:50", "10:15"], 100, trip_id="BAD") + trip_rows(NOON, 200, trip_id="OK")
    trips = make_trips(tmp_path, rows, stats)
    assert [t.trip_id for t in trips[(7, "301")]] == ["OK"]
    assert stats["trips_non_monotonic"] == 1 and stats["non_monotonic_examples"] == ["7/301//BAD//"]
    r = run(trips, [("2025-10-31 10:05:00", 1, "7 трамвай", 301)])
    assert r["status"].tolist() == ["outside_trips"]  # не привязываем по сломанному рейсу


@pytest.mark.parametrize("times, expected", [
    ([600, 605, 610], [600, 605, 610]),
    ([1430, 1435, 0, 5], [1430, 1435, 1440, 1445]),     # один переход через полночь
    ([1440, 1445, 1450], [1440, 1445, 1450]),           # 24:00+ уже монотонно
    ([600, 605, 590], None),                            # убывание меньше 12 ч — ошибка, а не полночь
    ([1430, 5, 1420, 10], None),                        # второй «переход»
])
def test_unwrap_midnight(times, expected):
    got = sb.unwrap_midnight(times)
    assert (got is None and expected is None) or got.tolist() == expected


WEEKDAY_ID, WEEKEND_ID = 3172953, 3172954


def overlap_rows(wd=WEEKDAY_ID, we=WEEKEND_ID):
    """График 301: будний рейс 10:00…10:20 (остановки 101…) и выходной 10:02…10:22 (остановки 201…)."""
    return trip_rows(MORNING, 100, trip_id="A", trip_num=1, service_id=wd) \
        + trip_rows(["10:02", "10:07", "10:12", "10:17", "10:22"], 200, trip_id="B", trip_num=1, service_id=we,
                    direction_id=1)


def write_calendar(tmp_path, rows):
    p = tmp_path / "calendar.csv"
    pd.DataFrame(rows, columns=["service_id", *sb.WEEKDAYS, "start_date", "end_date"]).to_csv(p, index=False)
    return p


FRI, SAT = "2025-10-31 10:05:00", "2025-11-01 10:05:00"


def test_service_id_resolved_by_calendar(tmp_path):
    trips = make_trips(tmp_path, overlap_rows())
    cal = sb.load_calendar(write_calendar(tmp_path, [(WEEKDAY_ID, 1, 1, 1, 1, 1, 0, 0, "", ""),
                                                     (WEEKEND_ID, 0, 0, 0, 0, 0, 1, 1, "", "")]))
    r = sb.bind(sb.normalize(raw([(FRI, 1, "7 трамвай", 301), (SAT, 1, "7 трамвай", 301)])), trips, calendar=cal)
    assert r["status"].tolist() == ["bound", "bound"]
    assert r["stop_id"].tolist() == ["102", "201"] and r["service_id"].tolist() == ["3172953", "3172954"]
    assert sb.service_mapping(trips, cal)["3172954"]["days"] == ["saturday", "sunday"]


def test_calendar_date_range(tmp_path):
    trips = make_trips(tmp_path, overlap_rows())
    cal = sb.load_calendar(write_calendar(tmp_path, [(WEEKDAY_ID, 1, 1, 1, 1, 1, 1, 1, "20251101", "20251231"),
                                                     (WEEKEND_ID, 1, 1, 1, 1, 1, 1, 1, "20251001", "20251031")]))
    r = sb.bind(sb.normalize(raw([(FRI, 1, "7 трамвай", 301), (SAT, 1, "7 трамвай", 301)])), trips, calendar=cal)
    assert r["stop_id"].tolist() == ["201", "102"]


def test_service_id_heuristic_without_calendar(tmp_path):
    trips = make_trips(tmp_path, overlap_rows("Будни", "Выходные"))
    r = run(trips, [(FRI, 1, "7 трамвай", 301), (SAT, 1, "7 трамвай", 301)])
    assert r["stop_id"].tolist() == ["102", "201"]
    assert sb.service_mapping(trips)["Будни"]["source"] == "heuristic"


@pytest.mark.parametrize("sid, days", [
    ("weekday", {0, 1, 2, 3, 4}), ("Рабочие дни", {0, 1, 2, 3, 4}), ("нерабочие", {5, 6}),
    ("WEEKEND_2025", {5, 6}), ("sat_sun", {5, 6}), ("Суббота", {5}), ("вс", {6}), ("daily", set(range(7))),
    ("3172953", None), ("всегда", None), ("sunset", None),
])
def test_service_days_heuristic(sid, days):
    got = sb.service_days(sid)
    assert (got is None and days is None) or set(got) == days


def test_ambiguous_when_service_unresolvable(tmp_path):
    trips = make_trips(tmp_path, overlap_rows())  # числовые service_id без календаря — действуют во все дни
    r = run(trips, [(FRI, 1, "7 трамвай", 301), ("2025-10-31 09:59:00", 1, "7 трамвай", 301)])
    assert r["status"].tolist() == ["ambiguous", "bound"]  # 09:59 — вне обоих рейсов, ближайший — будний
    assert pd.isna(r["stop_id"].iloc[0]) and pd.isna(r["vehicle_check"].iloc[0])
    rep = sb.report(r, rows_total=2, not_boarding=0, params={})
    assert rep["by_status"]["ambiguous"] == 1 and rep["by_route"]["7"]["ambiguous"] == 1
    w, cover = sb.stop_shares(r, pd.DataFrame(dict(route=7, stop_id=["101"])), min_bound=1, min_stop_share=0)
    assert cover["7"]["bound"] == 1  # ambiguous в доли остановок не идёт


def test_same_stop_or_handover_is_not_ambiguous(tmp_path):
    # рейс A кончается в 10:20 на конечной, рейс B того же графика стартует в 10:20: посадка — на рейс B
    rows = trip_rows(MORNING, 100, trip_id="A") + trip_rows(["10:20", "10:25", "10:30"], 200, trip_id="B")
    r = run(make_trips(tmp_path, rows), [("2025-10-31 10:20:00", 1, "7 трамвай", 301)])
    assert r["status"].tolist() == ["bound"] and r["stop_id"].tolist() == ["201"]


def test_service_after_midnight_uses_previous_day(tmp_path):
    # будний рейс 23:50…00:10: посадка в сб 00:05 — рейс пятницы, в вс 00:05 — рейс субботы, его нет
    trips = make_trips(tmp_path, trip_rows(["23:50", "23:55", "00:00", "00:05", "00:10"], 100,
                                           trip_id="N", service_id="weekday"))
    r = run(trips, [("2025-11-01 00:05:00", 1, "7 трамвай", 301), ("2025-11-02 00:05:00", 1, "7 трамвай", 301)])
    assert r["status"].tolist() == ["bound", "outside_trips"] and r["stop_id"].iloc[0] == "104"


def test_cli_calendar_and_report(tmp_path):
    s = tmp_path / "s.csv"
    pd.DataFrame(overlap_rows()).to_csv(s, index=False)
    cal = write_calendar(tmp_path, [(WEEKDAY_ID, 1, 1, 1, 1, 1, 0, 0, "", ""), (WEEKEND_ID, 0, 0, 0, 0, 0, 1, 1, "", "")])
    v = tmp_path / "v.csv"
    raw([(FRI, 1, "7 трамвай", 301), (SAT, 1, "7 трамвай", 301)]).to_csv(v, sep=";", index=False)
    base = ["--validations", str(v), "--schedule", str(s), "--stops-ref", str(s)]
    rep = sb.main(base + ["--calendar", str(cal)])
    assert rep["by_status"]["bound"] == 2 and rep["by_status"]["ambiguous"] == 0
    assert rep["params"]["calendar"] == "calendar.csv" and rep["service_mapping"]["3172953"]["source"] == "calendar"
    assert rep["schedule_checks"]["trips"] == 2
    rep = sb.main(base)  # без календаря числовые service_id не различить — ambiguous, а не первый попавшийся
    assert rep["by_status"]["ambiguous"] == 2 and rep["service_mapping"]["3172953"]["source"] == "all_days"
