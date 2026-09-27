"""Синтетическое «полное» расписание маршрута — демонстрация геопривязки по расписанию (pipeline/schedule_binding.py).

В справочнике организаторов лист «Расписание» — образец на 15 строк (один рейс), поэтому привязать реальные
валидации не к чему. Скрипт строит расписание-заглушку: реальный порядок остановок маршрута из справочника и
реальные номера выходов (bus_exit_no) из файла валидаций; каждый выход ездит туда-обратно с 5:30 до ~1:00,
2 минуты между остановками, 6 минут отстоя на конечной. Времена синтетические — доли остановок по такому
расписанию показывают работу цепочки, а не реальное распределение.

Запуск:  python pipeline/demo_synthetic_schedule.py --validations FILE --route 1 --out schedule.csv
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

STEP_MIN = 2       # минут между остановками
LAYOVER_MIN = 6    # отстой на конечной
START_MIN = 5 * 60 + 30
END_MIN = 25 * 60  # 01:00 следующих суток
EXIT_SHIFT_MIN = 4  # выходы стартуют с шагом 4 минуты


def build(route: int, ref: Path, validations: Path) -> pd.DataFrame:
    st = pd.read_excel(ref, sheet_name="Порядок_с_координатами")
    st = st[st["route_short_name"] == route].sort_values(["direction_id", "stop_sequence"])
    if st.empty:
        raise SystemExit(f"маршрута {route} нет в справочнике {ref}")
    raw = pd.read_csv(validations, sep=";", usecols=["ngpt_route", "bus_exit_no"], dtype=str)
    exits = sorted({int(x) for x in raw.loc[raw["ngpt_route"] == f"{route} трамвай", "bus_exit_no"].dropna()
                    if x.strip().isdigit()})
    if not exits:
        raise SystemExit(f"в {validations} нет выходов маршрута {route}")
    dirs = {d: g for d, g in st.groupby("direction_id")}
    rows, trip = [], 0
    for k, ex in enumerate(exits):
        t, d = START_MIN + k * EXIT_SHIFT_MIN, k % len(dirs)
        while t < END_MIN:
            g = dirs[list(dirs)[d]]
            trip += 1
            for i, s in enumerate(g.itertuples()):
                m = t + i * STEP_MIN
                hhmm = f"{m // 60:02d}:{m % 60:02d}"
                rows.append(dict(route_short_name=route, trip_id=trip, direction_id=s.direction_id, grafic=ex,
                                 stop_sequence=s.stop_sequence, stop_id=s.stop_id, stop_name=s.stop_name,
                                 arrival_time=hhmm, departure_time=hhmm))
            t += len(g) * STEP_MIN + LAYOVER_MIN
            d = (d + 1) % len(dirs)
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--validations", type=Path, required=True)
    ap.add_argument("--route", type=int, default=1)
    ap.add_argument("--ref", type=Path, default=Path("data/reference/tram_reference.xlsx"))
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    s = build(a.route, a.ref, a.validations)
    s.to_csv(a.out, index=False)
    print(f"[demo] маршрут {a.route}: выходов {s['grafic'].nunique()}, рейсов {s['trip_id'].nunique()}, "
          f"строк {len(s)} → {a.out}")


if __name__ == "__main__":
    main()
