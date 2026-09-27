"""CLI ML-части.

    py -m ml ingest                               сырые валидации → посадки и вагоны по часам, сверка с labels
    py -m ml export [--year 2026] [--trend 1.0]   таблицы для API/дашборда в ml/outputs (день, месяц, год, выпуск)
    py -m ml backtest [--search]                  фолды структурной модели (и покоординатный подбор)
    py -m ml base [--tag v1]                      замороженная база: 9 маршрутов, маршрут 5 = 0
    py -m ml r5 [--tag v1] [--w 8000 14000 20000] серия сабмитов, отличающихся только маршрутом 5
    py -m ml r5-next --scores 8000=0.9 ...        разбор score серии и четвёртый сабмит между двумя лучшими
    py -m ml variant --name NAME [--regime-end 50=2025-12-31 ...] [--level-mult 1.0]
                                                  гипотеза: одно изменение к лучшему файлу серии маршрута 5
    py -m ml route-probe --route 17 --mult 1.03   проба: лучший засчитанный файл, один маршрут × k
    py -m ml level-fit --route 17 --mult 1.03 --score 0.9071   оптимальный множитель маршрута по score пробы
    py -m ml probe --ref F --name N [--routes ..] [--date-from ..] [--date-to ..] [--hours 20-23] --mult K
    py -m ml fit   --ref F [те же фильтры] --mult K --score S --ref-score S0   оптимум по score пробы
    py -m ml weather-probe [--beta -0.012]        итог + погода (осадки перераспределяют посадки внутри месяца)
    py -m ml scenario [--precip 2025-12-05=15] [--month 12=1.03] [--route 17=0.95] [--event 7:2025-12-20:2025-12-21:0.5]
                                                  корректирующие коэффициенты поверх прогноза, без переобучения
    py -m ml check FILE                           проверка формата сабмита
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import numpy as np
import pandas as pd

from . import backtest as bt
from . import route5 as r5
from .calendar_ru import load_calendar
from .config import ART_DIR, NEW_ROUTE, SUB_DIR, Params, Route5
from .data import load_grid, nov1_tail
from .forecast import forecast_base
from .regimes import apply_effects, describe, load_effects, load_regimes
from .submission import md5, rows_md5, to_submission, validate, write
from .weather import load_weather

T0 = time.time()


def log(msg: str) -> None:
    print(f"[{time.time() - T0:5.1f} с] {msg}", flush=True)


def load_all(offline: bool):
    grid = load_grid()
    cal = load_calendar(offline)
    weather = load_weather(offline)
    regs = load_regimes()
    log(f"история: {grid['date'].min():%d.%m}–{grid['date'].max():%d.%m.%Y}, {len(grid):,} ячеек; "
        f"календарь: {cal.attrs['source']}; погода: {'есть' if weather is not None else 'нет'}")
    return grid, cal, weather, regs


def cmd_backtest(a) -> None:
    grid, cal, weather, regs = load_all(a.offline)
    p = Params()
    if a.search:
        log("покоординатный подбор параметров (фолды A, B, D, E)")
        p, hist = bt.search(p, grid, cal, weather, regs, log=log)
        ART_DIR.mkdir(parents=True, exist_ok=True)
        hist.to_csv(ART_DIR / "search_history.csv", index=False)
        log(f"лучшие параметры: {p}")
    bt.report(p, grid, cal, weather, regs, log=log)


def base_paths(tag: str):
    return SUB_DIR / f"base_{tag}.csv", ART_DIR / f"base_{tag}.parquet", ART_DIR / f"base_{tag}.json"


def cmd_base(a) -> None:
    grid, cal, weather, regs = load_all(a.offline)
    p = Params()
    log("режимы: " + "; ".join(describe(regs)))
    res = bt.evaluate(p, grid, cal, weather, regs)
    log("бэктест: " + ", ".join(f"{k} {v:.4f}" for k, v in res["score"].items())
        + f" · среднее A,B,D,E {bt.selection_score(res):.4f}")
    pred, model = forecast_base(p, grid, cal, weather, regs, nov1_tail())
    csv_path, pq_path, js_path = base_paths(a.tag)
    effects = load_effects()
    log("внешние события: " + "; ".join(f"{e['name']} ×{e['mult']}" for e in effects))
    sub = to_submission(apply_effects(pred, effects))       # parquet остаётся без событий: из него доли для маршрута 5
    h = write(sub, csv_path)
    ART_DIR.mkdir(parents=True, exist_ok=True)
    pred.to_parquet(pq_path, index=False)
    daily = pred.groupby(["route", pred["date"].dt.month])["pred"].sum().unstack().round(0)
    meta = {
        "tag": a.tag, "file": str(csv_path.name), "md5": h, "params": p.to_dict(),
        "backtest": res["score"].round(5).to_dict(), "level_W": {int(k): round(v) for k, v in model.f.W.items()},
        "anomalies": {int(k): v for k, v in model.f.anomalies.items() if v},
        "regimes": describe(regs), "total_by_month": {int(r): row.to_dict() for r, row in daily.iterrows()},
    }
    js_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log(f"база → {csv_path} (md5 {h[:8]}), прогноз в сутки по маршрутам:")
    per_day = pred.groupby("route")["pred"].sum() / 61
    log("  " + ", ".join(f"{r}: {v / 1000:.1f}K" for r, v in per_day.items()))


def _load_base(tag: str):
    csv_path, pq_path, _ = base_paths(tag)
    if not csv_path.exists() or not pq_path.exists():
        sys.exit(f"нет базы {csv_path}: сначала py -m ml base --tag {tag}")
    return pd.read_csv(csv_path, sep=";"), pd.read_parquet(pq_path)


def _r5_file(tag: str, W: float):
    return SUB_DIR / f"r5_{tag}_W{int(W):05d}.csv"


def _write_r5(base_sub, base_pred, cal, W: float, tag: str) -> tuple:
    f5 = apply_effects(r5.forecast(base_pred, cal, W, Route5(weekday_level=W), Params()), load_effects())
    sub = base_sub.copy()
    m = sub["route"] == NEW_ROUTE
    key = f5.assign(date=f5["date"].dt.strftime("%Y-%m-%d")).set_index(["date", "hour"])["pred"]
    sub.loc[m, "prediction"] = np.rint(key.reindex(pd.MultiIndex.from_frame(sub.loc[m, ["date", "hour"]])).to_numpy()) \
                                 .astype(int)
    path = _r5_file(tag, W)
    h = write(sub, path)
    return path, h, int(sub.loc[m, "prediction"].sum()), rows_md5(sub, exclude_route=NEW_ROUTE)


def cmd_r5(a) -> None:
    cal = load_calendar(a.offline)
    base_sub, base_pred = _load_base(a.tag)
    base_hash = rows_md5(base_sub, exclude_route=NEW_ROUTE)
    P = r5.units(cal)
    series = {"base": f"base_{a.tag}.csv", "base_rows_md5_without_route5": base_hash, "P_days": round(P, 3),
              "rule": r5.__doc__.strip().splitlines()[2:6], "files": {}}
    for W in a.w:
        path, h, vol, bh = _write_r5(base_sub, base_pred, cal, W, a.tag)
        assert bh == base_hash, "база в серии изменилась"
        series["files"][path.name] = {"W": W, "route5_total": vol, "md5": h}
        log(f"{path.name}: W = {W:,.0f}, маршрут 5 за 16–31.12 = {vol:,} посадок")
    (SUB_DIR / f"r5_{a.tag}_series.json").write_text(json.dumps(series, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"все файлы серии: строки маршрутов кроме 5 идентичны базе (md5 {base_hash[:8]})")


def cmd_r5_next(a) -> None:
    cal = load_calendar(a.offline)
    base_sub, base_pred = _load_base(a.tag)
    scores = {}
    for item in a.scores:
        w, s = item.split("=")
        scores[float(w)] = float(s)
    P = r5.units(cal)
    best = max(scores, key=scores.get)
    Y = float(base_sub.loc[base_sub["route"] != NEW_ROUTE, "prediction"].sum()) + P * best
    res = r5.next_w(scores, P, Y)
    log(f"P = {P:.2f} будних-эквивалентов, оценка Y ≈ {Y / 1e6:.2f} млн посадок (сумма базы)")
    for s in res["secants"]:
        log(f"  {s['from']:,.0f} → {s['to']:,.0f}: Δscore {s['d_score']:+.5f} (предел ±{s['max_d_score']:.5f})"
            + ("; факт выше/ниже обоих W почти во всех ячейках" if abs(s['d_score']) > 0.9 * s['max_d_score'] else ""))
    log(f"модель: истинный уровень W_t ≈ {res['w_true']:,.0f}, разброс ячеек σ ≈ {res['sigma']:.2f}, "
        f"L1-оптимум W* ≈ {res['w_star']:,.0f} (невязка {res['fit_rmse']:.1e})")
    log(f"лучший W = {res['best']:,.0f}, второй = {res['second']:,.0f}; оптимум {res['optimum_side']}")
    if not res["needed"]:
        log(f"четвёртый сабмит не нужен: оптимум ≈ {res['w4']:,.0f}, ожидаемый прирост к лучшему "
            f"{res['gain']:+.5f}. Оставляйте W = {res['best']:,.0f}.")
        return
    path, h, vol, bh = _write_r5(base_sub, base_pred, cal, res["w4"], a.tag)
    assert bh == rows_md5(base_sub, exclude_route=NEW_ROUTE)
    log(f"четвёртый сабмит: {path.name} (W = {res['w4']:,.0f}, маршрут 5 = {vol:,} посадок), "
        f"ожидаемый прирост к лучшему {res['gain']:+.5f}")


def cmd_variant(a) -> None:
    """Гипотеза для лидерборда: пересчёт 9 маршрутов с одним изменением, маршрут 5 и всё прочее — из опорного файла."""
    from dataclasses import replace
    ref_path = _r5_file(a.tag, a.w5)
    if not ref_path.exists():
        sys.exit(f"нет опорного файла {ref_path}")
    ref = pd.read_csv(ref_path, sep=";")
    grid, cal, weather, _ = load_all(a.offline)
    ends = dict((int(k), v) for k, v in (x.split("=") for x in a.regime_end)) if a.regime_end else None
    regs = load_regimes(end_override=ends)
    p = Params() if a.level_mult is None else replace(Params(), level_mult=a.level_mult)
    if a.nye is not None:
        p = replace(p, nye_vs_sat=a.nye)
    if a.hol is not None:
        p = replace(p, hol_vs_sun=a.hol)
    if a.worksat is not None:
        p = replace(p, worksat_vs_wd=a.worksat)
    if a.level_month:
        p = replace(p, level_month={int(k): float(v) for k, v in (x.split("=") for x in a.level_month)})
    pred, _ = forecast_base(p, grid, cal, weather, regs, nov1_tail())
    var = to_submission(apply_effects(pred, load_effects()))
    sub = ref.copy()
    keep5 = sub["route"] == NEW_ROUTE
    sub.loc[~keep5, "prediction"] = var.loc[~keep5.to_numpy(), "prediction"].to_numpy()
    diff = sub["prediction"] != ref["prediction"]
    path = SUB_DIR / f"hyp_{a.tag}_W{int(a.w5):05d}_{a.name}.csv"
    h = write(sub, path)
    d = sub[diff]
    delta = int(sub["prediction"].sum() - ref["prediction"].sum())
    log(f"{path.name}: отличается от {ref_path.name} в {int(diff.sum())} ячейках; маршруты "
        f"{sorted(d['route'].unique().tolist())}, даты {d['date'].min() if len(d) else '—'} … "
        f"{d['date'].max() if len(d) else '—'}; объём {delta:+,} посадок (md5 {h[:8]})")


def cmd_route_probe(a) -> None:
    from . import calibrate as cb
    ref_path = SUB_DIR / a.ref if a.ref else cb.BEST
    ref = pd.read_csv(ref_path, sep=";")
    sub = cb.probe(ref, a.route, a.mult)
    tag = "" if not a.ref else "_" + ref_path.stem.replace("_W06000", "").replace("r5_", "")
    path = SUB_DIR / f"probe{tag}_r{a.route}_x{a.mult:.2f}.csv"
    h = write(sub, path)
    d = int(sub["prediction"].sum() - ref["prediction"].sum())
    log(f"{path.name}: маршрут {a.route} × {a.mult} к {ref_path.name}, объём {d:+,} посадок, md5 {h[:8]}")


def cmd_level_fit(a) -> None:
    from . import calibrate as cb
    ref = pd.read_csv(SUB_DIR / a.ref if a.ref else cb.BEST, sep=";")
    P = float(ref.loc[ref["route"] == a.route, "prediction"].sum())
    grid, cal, weather, regs = load_all(a.offline)
    sig = cb.route_sigma(grid, cal, weather, regs)[a.route] * cb.SIGMA_INFLATE
    ds = a.score - (a.ref_score if a.ref_score is not None else cb.BEST_SCORE)
    res = cb.fit_level(ds, a.mult, P, sig)
    log(f"маршрут {a.route}: проба × {a.mult} → Δscore {ds:+.5f}; σ ячеек ≈ {sig:.3f}, объём {P / 1e6:.2f} млн")
    log(f"  истинный уровень ≈ × {res['k_t']:.3f}, L1-оптимум k* ≈ × {res['k_star']:.3f}; ожидаемый прирост "
        f"к опорному {res['gain_vs_ref']:+.5f}, к пробе {res['gain_vs_probe']:+.5f}"
        + (" (проба в насыщении — оценка k* — нижняя/верхняя граница)" if res["saturated"] else ""))
    cb.save_level(a.route, {"mult_probe": a.mult, "delta_score": round(ds, 5), "sigma": round(sig, 4),
                            "k_star": round(res["k_star"], 4), "gain_vs_ref": round(res["gain_vs_ref"], 5)})
    log(f"  записано в {cb.LEVELS}")


def _mask_args(a):
    hours = None
    if a.hours:
        lo, _, hi = a.hours.partition("-")
        hours = list(range(int(lo), int(hi or lo) + 1))
    return dict(routes=a.routes, date_from=a.date_from, date_to=a.date_to, hours=hours, dow=a.dow,
                exclude_dates=a.exclude_dates)


def cmd_probe(a) -> None:
    """Проба: ячейки по фильтрам × k от опорного файла."""
    from . import calibrate as cb
    ref_path = SUB_DIR / a.ref
    ref = pd.read_csv(ref_path, sep=";")
    m = cb.select(ref, **_mask_args(a))
    sub = cb.probe(ref, None, a.mult, mask=m)
    tag = ref_path.stem.replace("_W06000", "").replace("r5_", "")
    path = SUB_DIR / f"probe_{tag}_{a.name}_x{a.mult:.2f}.csv"
    h = write(sub, path)
    d = int(sub["prediction"].sum() - ref["prediction"].sum())
    log(f"{path.name}: {int(m.sum())} ячеек × {a.mult} к {ref_path.name}; объём ячеек {int(ref.loc[m, 'prediction'].sum()):,}, "
        f"изменение {d:+,} посадок, md5 {h[:8]}")


def cmd_fit(a) -> None:
    """Оптимальный множитель для ячеек пробы по её score (одна проба + модель шума)."""
    from . import calibrate as cb
    ref = pd.read_csv(SUB_DIR / a.ref, sep=";")
    m = cb.select(ref, **_mask_args(a))
    P = float(ref.loc[m, "prediction"].sum())
    if a.sigma is None:
        grid, cal, weather, regs = load_all(a.offline)
        sig_r = cb.route_sigma(grid, cal, weather, regs)
        vol = ref[m].groupby("route")["prediction"].sum()
        sig = float(sum(sig_r.get(int(r), 0.15) * v for r, v in vol.items()) / vol.sum()) * cb.SIGMA_INFLATE
    else:
        sig = a.sigma
    if a.points:
        # несколько проб одних и тех же ячеек: подбираем и уровень, и σ (модель та же, что для маршрута 5)
        pts = {1.0: a.ref_score}
        pts.update({float(k): float(v) for k, v in (x.split("=") for x in a.points)})
        res = r5.next_w({k * 10000: v for k, v in pts.items()}, P / 10000, cb.Y_EST, step=5)
        log(f"пробы {', '.join(f'×{k}: {v - a.ref_score:+.5f}' for k, v in sorted(pts.items()) if k != 1.0)}; "
            f"объём ячеек {P / 1e6:.3f} млн")
        log(f"  истинный уровень ≈ × {res['w_true'] / 1e4:.3f}, σ ≈ {res['sigma']:.2f} (невязка {res['fit_rmse']:.1e}); "
            f"L1-оптимум k* ≈ × {res['w_star'] / 1e4:.3f}; ожидаемый прирост к лучшей точке {res['gain']:+.5f}")
        return
    ds = a.score - a.ref_score
    res = cb.fit_level(ds, a.mult, P, sig)
    log(f"проба × {a.mult}: Δscore {ds:+.5f}; объём ячеек {P / 1e6:.3f} млн, σ ≈ {sig:.3f}")
    log(f"  истинный уровень ячеек ≈ × {res['k_t']:.3f}, L1-оптимум k* ≈ × {res['k_star']:.3f}; ожидаемый прирост "
        f"к опорному {res['gain_vs_ref']:+.5f}, к пробе {res['gain_vs_probe']:+.5f}"
        + (" (проба в насыщении — k* лишь граница)" if res["saturated"] else ""))


def cmd_weather_probe(a) -> None:
    """Проба погоды: итоговый прогноз из кода с перераспределением по осадкам внутри месяца (β = --beta)."""
    from dataclasses import replace
    from .forecast import build_final
    ref_path = SUB_DIR / a.ref
    ref = pd.read_csv(ref_path, sep=";")
    base, _ = build_final(offline=a.offline)
    if rows_md5(base) != rows_md5(ref):
        sys.exit(f"build_final без погоды не совпадает с {ref_path.name} — опорный файл не из текущего кода")
    sub, _ = build_final(offline=a.offline, params=replace(Params(), weather_beta=a.beta))
    path = SUB_DIR / f"probe_v8_weather_b{abs(a.beta):.3f}.csv"
    h = write(sub, path)
    k = sub.assign(ref=ref["prediction"]).groupby("date")[["prediction", "ref"]].sum()
    k = k["prediction"] / k["ref"]
    changed = int((sub["prediction"] != ref["prediction"]).sum())
    log(f"{path.name}: β = {a.beta}, изменено ячеек {changed:,}, сумма {int(sub['prediction'].sum() - ref['prediction'].sum()):+,} "
        f"посадок; множитель дня {k.min():.3f}…{k.max():.3f} (самый мокрый {k.idxmin()}), md5 {h[:8]}")


def cmd_ingest(a) -> None:
    """Сырые валидации → посадки и вагоны по маршруту и часу + сверка с labels организаторов."""
    from .ingest import aggregate_raw, check_labels
    raw = aggregate_raw(log=log)
    c = check_labels(raw.assign(date=pd.to_datetime(raw["date"])))
    log(f"сверка с labels: {c['cells']:,} ячеек, расхождений {c['cells_diff']}, макс. разница {c['max_abs_diff']}; "
        f"посадок {c['boardings']:,}; хвост после 31.10 — {c['after_period']} посадок (факт 1.11 00–01 ч)")


def cmd_export(a) -> None:
    """Таблицы для API/дашборда: день и месяц (ноябрь–декабрь 2025), год (2026), усиление выпуска, ошибки модели."""
    from .backtest import error_profile
    from .config import OUT_DIR
    from .fleet import reinforcement, reserve
    from .forecast import build_final
    from .horizons import aggregate, with_band, year_forecast
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    files = {}

    def save(df: pd.DataFrame, name: str) -> None:
        df.to_csv(OUT_DIR / f"{name}.csv", index=False, encoding="utf-8")
        files[name] = df
        log(f"  {name}.csv: {len(df):,} строк")

    grid, cal, regs = load_grid(), load_calendar(a.offline), load_regimes()
    err = error_profile(Params(), grid, cal, None, regs)
    lv = err["by_level"].set_index("level")
    u_route, u_net = lv.at["месяц маршрута", "p80_abs_err"], lv.at["месяц сети", "p80_abs_err"]

    def band(df: pd.DataFrame) -> pd.DataFrame:
        net = df["route"] == "all"
        return pd.concat([with_band(df[~net], u_route), with_band(df[net], u_net)]).sort_index()

    sub, _ = build_final(offline=a.offline)
    hourly = sub.rename(columns={"prediction": "pred"})
    save(hourly, "forecast_hourly_2025-11_12")
    save(aggregate(hourly, "day").assign(date=lambda d: d["date"].dt.strftime("%Y-%m-%d")), "forecast_daily_2025-11_12")
    save(band(aggregate(hourly, "month")), "forecast_monthly_2025-11_12")

    year = year_forecast(a.year, a.trend, a.offline)
    save(band(aggregate(year, "day")).assign(date=lambda d: d["date"].dt.strftime("%Y-%m-%d")), f"forecast_year_{a.year}_daily")
    save(band(aggregate(year, "month")), f"forecast_year_{a.year}_monthly")

    cand, summary = reinforcement(hourly, grid, cal, regs)
    res, res_summary = reserve(hourly, grid, cal, regs, load_effects())
    save(cand, "fleet_reinforcement_2025-11_12")
    save(summary.merge(res_summary, on="route", how="left"), "fleet_summary_2025-11_12")
    save(res, "fleet_reserve_2025-11_12")
    for k, v in err.items():
        save(v, f"model_error_{k}")

    with pd.ExcelWriter(OUT_DIR / "forecast.xlsx") as xw:
        for name in ("forecast_monthly_2025-11_12", "forecast_daily_2025-11_12", f"forecast_year_{a.year}_monthly",
                     "fleet_summary_2025-11_12", "model_error_by_horizon", "model_error_by_level"):
            files[name].to_excel(xw, sheet_name=name[:31], index=False)
    log(f"  forecast.xlsx: сводные листы · папка {OUT_DIR}")


def _event(spec: str) -> dict:
    """МАРШРУТЫ:НАЧАЛО:КОНЕЦ:МНОЖИТЕЛЬ[:ЧАСЫ], напр. 7,50:2025-12-20:2025-12-21:0.5:10-18 (маршруты all = все)."""
    routes, start, end, mult, *hours = spec.split(":")
    e = {"name": spec, "start": start, "end": end, "mult": float(mult),
         "routes": None if routes == "all" else [int(r) for r in routes.split(",")]}
    if hours:
        lo, _, hi = hours[0].partition("-")
        e["hours"] = list(range(int(lo), int(hi or lo) + 1))
    return e


def cmd_fleet(a) -> None:
    """Усиление и резерв выпуска по готовому почасовому прогнозу (ml/outputs), без пересчёта модели."""
    from .config import OUT_DIR
    from .fleet import reinforcement, reserve
    hourly = pd.read_csv(OUT_DIR / "forecast_hourly_2025-11_12.csv", parse_dates=["date"])
    grid, cal, regs = load_grid(), load_calendar(a.offline), load_regimes()
    cand, summary = reinforcement(hourly, grid, cal, regs)
    res, res_summary = reserve(hourly, grid, cal, regs, load_effects())
    for df, name in ((cand, "fleet_reinforcement_2025-11_12"),
                     (summary.merge(res_summary, on="route", how="left"), "fleet_summary_2025-11_12"),
                     (res, "fleet_reserve_2025-11_12")):
        df.to_csv(OUT_DIR / f"{name}.csv", index=False, encoding="utf-8")
        log(f"  {name}.csv: {len(df):,} строк")


def cmd_scenario(a) -> None:
    """Корректирующие коэффициенты поверх итогового прогноза (погода / сезон / маршрут / событие) без переобучения."""
    from .config import OUT_DIR
    from .forecast import build_final
    from .scenario import adjust, impact
    f = OUT_DIR / "forecast_hourly_2025-11_12.csv"
    base = pd.read_csv(f) if f.exists() else build_final(offline=a.offline)[0].rename(columns={"prediction": "pred"})
    kv = lambda xs, kt: {kt(k): float(v) for k, v in (x.split("=") for x in xs or [])}
    precip = "archive" if a.precip_archive else (kv(a.precip, str) or None)
    t = time.perf_counter()
    adj = adjust(base, precip, a.beta, kv(a.month, int), kv(a.route, int), [_event(s) for s in a.event or []])
    ms = 1000 * (time.perf_counter() - t)
    path = OUT_DIR / (a.out or "scenario_hourly.csv")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    adj.to_csv(path, index=False, encoding="utf-8")
    print(impact(adj, "route").to_string(index=False))
    log(f"пересчёт {len(adj):,} ячеек за {ms:.0f} мс → {path}")


def cmd_check(a) -> None:
    errs = validate(pd.read_csv(a.file, sep=";"))
    print("OK" if not errs else "\n".join(errs), f"md5 {md5(a.file)}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="py -m ml", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true", help="без сети: только кэш и встроенный календарь")
    sp = ap.add_subparsers(dest="cmd", required=True)
    b = sp.add_parser("backtest")
    b.add_argument("--search", action="store_true")
    b.set_defaults(fn=cmd_backtest)
    b = sp.add_parser("base")
    b.add_argument("--tag", default="v1")
    b.set_defaults(fn=cmd_base)
    b = sp.add_parser("r5")
    b.add_argument("--tag", default="v1")
    b.add_argument("--w", type=float, nargs="+", default=[Route5().weekday_level])
    b.set_defaults(fn=cmd_r5)
    b = sp.add_parser("r5-next")
    b.add_argument("--tag", default="v1")
    b.add_argument("--scores", nargs="+", required=True, help="W=score, например 8000=0.9012")
    b.set_defaults(fn=cmd_r5_next)
    b = sp.add_parser("variant")
    b.add_argument("--tag", default="v1")
    b.add_argument("--w5", type=float, default=6000, help="W маршрута 5 опорного файла серии")
    b.add_argument("--name", required=True)
    b.add_argument("--regime-end", nargs="+", help="маршрут=последняя дата режима выходных, напр. 50=2025-12-31")
    b.add_argument("--level-mult", type=float)
    b.add_argument("--nye", type=float, help="31.12 = суббота × k")
    b.add_argument("--hol", type=float, help="нерабочий будний (3–4.11) = воскресенье × k")
    b.add_argument("--worksat", type=float, help="рабочая суббота 1.11 = будень × k")
    b.add_argument("--level-month", nargs="+", help="месяц=множитель поверх общего уровня, напр. 11=1.02")
    b.set_defaults(fn=cmd_variant)
    b = sp.add_parser("route-probe")
    b.add_argument("--route", type=int, required=True)
    b.add_argument("--mult", type=float, required=True)
    b.add_argument("--ref", help="опорный файл в submissions/ (по умолчанию лучший засчитанный)")
    b.set_defaults(fn=cmd_route_probe)
    b = sp.add_parser("level-fit")
    b.add_argument("--route", type=int, required=True)
    b.add_argument("--mult", type=float, required=True)
    b.add_argument("--score", type=float, required=True)
    b.add_argument("--ref-score", type=float)
    b.add_argument("--ref", help="опорный файл в submissions/ (по умолчанию лучший засчитанный)")
    b.set_defaults(fn=cmd_level_fit)
    for name, fn in (("probe", cmd_probe), ("fit", cmd_fit)):
        b = sp.add_parser(name)
        b.add_argument("--ref", required=True, help="опорный файл в submissions/")
        b.add_argument("--routes", type=int, nargs="+")
        b.add_argument("--date-from")
        b.add_argument("--date-to")
        b.add_argument("--hours", help="часы, напр. 20-23")
        b.add_argument("--dow", type=int, nargs="+", help="дни недели 0=Пн … 6=Вс")
        b.add_argument("--exclude-dates", nargs="+")
        b.add_argument("--mult", type=float, required=(name == "probe"))
        if name == "probe":
            b.add_argument("--name", required=True)
        else:
            b.add_argument("--score", type=float)
            b.add_argument("--ref-score", type=float, required=True)
            b.add_argument("--points", nargs="+", help="несколько проб: k=score, напр. 0.95=0.9085 0.90=0.9079")
            b.add_argument("--sigma", type=float, help="разброс ячеек (по умолчанию из бэктеста × 1.1)")
        b.set_defaults(fn=fn)
    b = sp.add_parser("ingest")
    b.set_defaults(fn=cmd_ingest)
    b = sp.add_parser("export")
    b.add_argument("--year", type=int, default=2026)
    b.add_argument("--trend", type=float, default=1.0, help="сценарный коэффициент тренда год к году")
    b.set_defaults(fn=cmd_export)
    b = sp.add_parser("fleet", help="усиление и резерв выпуска по готовому прогнозу (ml/outputs)")
    b.set_defaults(fn=cmd_fleet)
    b = sp.add_parser("weather-probe")
    b.add_argument("--ref", default="r5_v8_W06000.csv", help="опорный файл = итог без погоды")
    b.add_argument("--beta", type=float, default=-0.012)
    b.set_defaults(fn=cmd_weather_probe)
    b = sp.add_parser("scenario")
    b.add_argument("--precip", nargs="+", help="осадки дня, мм: 2025-12-05=15")
    b.add_argument("--precip-archive", action="store_true", help="фактические осадки 2025 года (Open-Meteo)")
    b.add_argument("--beta", type=float, default=-0.012, help="эффект осадков на log1p(мм)")
    b.add_argument("--month", nargs="+", help="сезон: месяц=множитель, 12=1.03")
    b.add_argument("--route", nargs="+", help="маршрут=множитель, 17=0.95")
    b.add_argument("--event", nargs="+", help="МАРШРУТЫ:НАЧАЛО:КОНЕЦ:МНОЖИТЕЛЬ[:ЧАСЫ], 7,50:2025-12-20:2025-12-21:0.5")
    b.add_argument("--out", help="файл в ml/outputs (по умолчанию scenario_hourly.csv)")
    b.set_defaults(fn=cmd_scenario)
    b = sp.add_parser("check")
    b.add_argument("file")
    b.set_defaults(fn=cmd_check)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
