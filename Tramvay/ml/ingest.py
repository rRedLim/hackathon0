"""Приём и нормализация сырых валидаций: train.csv + test.csv → посадки и вагоны по маршруту и часу.

Правила из описания датасета:
  • посадка = успешная валидация (validation_result == 1);
  • время — tran_date_time (input_date_time ненадёжно), дата и час берутся из строки как есть, «хвост» файла
    в следующий месяц относится к своей дате;
  • маршрут — число из ngpt_route («25 трамвай» → 25).
Вагон-час — уникальный garage_number хотя бы с одной успешной валидацией на маршруте в этот час.

    py -m ml ingest     # ~2–3 мин на 10 ГБ, результат ml/artifacts/raw_route_hour.parquet + сверка с labels
"""
from __future__ import annotations

import time
from pathlib import Path

import pandas as pd

from .config import ART_DIR, DATA_DIR
from .data import load_grid

RAW_FILE = ART_DIR / "raw_route_hour.parquet"     # route, date, hour, boardings, n_veh
COLS = ["tran_date_time", "validation_result", "ngpt_route", "garage_number"]


def aggregate_raw(data_dir: Path = DATA_DIR, log=print) -> pd.DataFrame:
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.csv as pacsv

    t0 = time.time()
    counts, vehicles, stats = [], [], []
    for fname in ("train.csv", "test.csv"):
        f = data_dir / fname
        if not f.exists():
            raise FileNotFoundError(f"нет {f}")
        reader = pacsv.open_csv(
            f, read_options=pacsv.ReadOptions(block_size=64 << 20, use_threads=True),
            parse_options=pacsv.ParseOptions(delimiter=";"),
            convert_options=pacsv.ConvertOptions(include_columns=COLS, strings_can_be_null=True,
                                                 column_types={c: pa.string() for c in COLS}))
        rows = bad = noroute = 0
        for batch in reader:
            t = pa.Table.from_batches([batch])
            rows += t.num_rows
            valid = pc.fill_null(pc.match_substring_regex(t["tran_date_time"], r"^\d{4}-\d\d-\d\d \d\d"), False)
            ok = pc.fill_null(pc.equal(t["validation_result"], "1"), False)
            bad += t.num_rows - pc.sum(pc.cast(valid, pa.int64())).as_py()
            t = t.filter(pc.and_(valid, ok))
            route = pc.cast(pc.struct_field(pc.extract_regex(t["ngpt_route"], r"^(?P<r>\d+)"), [0]), pa.int16())
            has = pc.is_valid(route)
            noroute += t.num_rows - pc.sum(pc.cast(has, pa.int64())).as_py()
            x = pa.table({"route": route, "date": pc.utf8_slice_codeunits(t["tran_date_time"], 0, 10),
                          "hour": pc.cast(pc.utf8_slice_codeunits(t["tran_date_time"], 11, 13), pa.int8()),
                          "garage": t["garage_number"]}).filter(has)
            counts.append(x.group_by(["route", "date", "hour"]).aggregate([([], "count_all")]))
            vehicles.append(x.filter(pc.is_valid(x["garage"])).group_by(["route", "date", "hour", "garage"]).aggregate([]))
        stats.append(f"{fname}: {rows:,} строк, битых дат {bad:,}, успешных без маршрута {noroute:,}")
        log(f"  {stats[-1]} · {time.time() - t0:.0f} с")

    # колонки результата group_by — по именам: их порядок зависит от версии pyarrow
    c = pa.concat_tables(counts).group_by(["route", "date", "hour"]).aggregate([("count_all", "sum")])
    c = c.select(["route", "date", "hour", "count_all_sum"]).rename_columns(["route", "date", "hour", "boardings"])
    v = pa.concat_tables(vehicles).group_by(["route", "date", "hour", "garage"]).aggregate([])
    v = v.group_by(["route", "date", "hour"]).aggregate([([], "count_all")])
    v = v.select(["route", "date", "hour", "count_all"]).rename_columns(["route", "date", "hour", "n_veh"])
    out = c.to_pandas().merge(v.to_pandas(), on=["route", "date", "hour"], how="left")
    out = out.fillna({"n_veh": 0}).astype({"route": int, "hour": int, "boardings": int, "n_veh": int})
    out = out.sort_values(["route", "date", "hour"], ignore_index=True)
    ART_DIR.mkdir(parents=True, exist_ok=True)
    out.to_parquet(RAW_FILE, index=False)
    log(f"  агрегировано за {time.time() - t0:.0f} с → {RAW_FILE.relative_to(ART_DIR.parent.parent)}")
    return out


def load_raw(data_dir: Path = DATA_DIR, log=print) -> pd.DataFrame:
    if RAW_FILE.exists():
        raw = pd.read_parquet(RAW_FILE)
    else:
        log("нет агрегата сырых валидаций — считаю из train.csv/test.csv (~2–3 мин)")
        raw = aggregate_raw(data_dir, log)
    return raw.assign(date=pd.to_datetime(raw["date"]))


def check_labels(raw: pd.DataFrame, data_dir: Path = DATA_DIR) -> dict:
    """Сверка своей агрегации с labels организаторов по всем ячейкам января–октября."""
    lab = load_grid(data_dir)
    m = lab.merge(raw[["route", "date", "hour", "boardings"]].rename(columns={"boardings": "raw"}),
                  on=["route", "date", "hour"], how="left").fillna({"raw": 0})
    diff = (m["boardings"] - m["raw"]).abs()
    outside = raw[raw["date"] > lab["date"].max()]
    return {"cells": len(m), "cells_diff": int((diff > 0).sum()), "max_abs_diff": int(diff.max()),
            "boardings": int(m["boardings"].sum()), "after_period": int(outside["boardings"].sum())}
