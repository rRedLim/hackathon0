import { useMemo, useState } from 'react'
import {
  Area,
  Bar,
  BarChart,
  CartesianGrid,
  ComposedChart,
  ErrorBar,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { API, downloadFile, errorMessage, qs, type ForecastResponse, type Granularity, type HorizonId, type QueryValue, type RouteStops } from '../api'
import { type Column, DataTable, Kpi, RouteBadge, RouteChips, Section, Spinner, StateBox } from '../components'
import { clampDate, daysBetween, fmtDate, fmtNum, fmtPeriod, fmtTick, granularityLabel, hourLabel } from '../format'
import { notify, useApi, useMeta } from '../hooks'

const download = (url: string | undefined) => {
  if (url) downloadFile(url).catch((e: unknown) => notify(errorMessage(e)))
}

type Level = 'route' | 'stop' | 'segment'

const HOURS = Array.from({ length: 24 }, (_, h) => h)

function defaultRange(h: { id: HorizonId; from: string; to: string }): [string, string] {
  if (h.id === 'day') return ['2025-11-10', '2025-11-16']
  return [h.from, h.to]
}

interface TableRow {
  t: string
  label: string
  color: string
  pred: number
  lo: number | null
  hi: number | null
}

export default function ForecastPage() {
  const meta = useMeta()
  const horizons = meta.horizons
  const [horizon, setHorizon] = useState<HorizonId>('day')
  const h = horizons.find((x) => x.id === horizon) ?? horizons[0]
  const [range, setRange] = useState<[string, string]>(() => defaultRange(h))
  const [granularity, setGranularity] = useState<Granularity>(h.defaultGranularity)
  const [routes, setRoutes] = useState<number[]>(() => meta.routes.map((r) => r.route))
  const [level, setLevel] = useState<Level>('route')
  const geoRoutes = meta.routes.filter((r) => r.stops > 0)
  const [stopRoute, setStopRoute] = useState<number>(geoRoutes[0]?.route ?? 1)
  const [stopDir, setStopDir] = useState(0)
  const [stopIds, setStopIds] = useState<string[]>([])
  const [stopFilter, setStopFilter] = useState('')
  const [segFrom, setSegFrom] = useState<number | null>(null)
  const [segTo, setSegTo] = useState<number | null>(null)
  const [hourFrom, setHourFrom] = useState(0)
  const [hourTo, setHourTo] = useState(23)
  const [split, setSplit] = useState<'route' | 'none'>('route')
  const [exportLevel, setExportLevel] = useState<'route' | 'stop'>('route')

  const changeHorizon = (id: HorizonId) => {
    const nh = horizons.find((x) => x.id === id)
    if (!nh) return
    setHorizon(id)
    setRange(defaultRange(nh))
    setGranularity(nh.defaultGranularity)
  }

  const stopsRes = useApi<RouteStops>(level !== 'route' ? `${API}/routes/${stopRoute}/stops` : null)
  const dirStops = useMemo(() => {
    const d = stopsRes.data?.directions.find((x) => x.direction === stopDir) ?? stopsRes.data?.directions[0]
    return [...(d?.stops ?? [])].sort((a, b) => a.seq - b.seq)
  }, [stopsRes.data, stopDir])
  const directions = stopsRes.data?.directions.map((d) => d.direction) ?? [0, 1]

  const segA = segFrom ?? dirStops[0]?.seq ?? null
  const segB = segTo ?? dirStops[dirStops.length - 1]?.seq ?? null

  const params: Record<string, QueryValue> | null = useMemo(() => {
    const [from, to] = range
    const p: Record<string, QueryValue> = {
      horizon,
      from,
      to,
      hourFrom: Math.min(hourFrom, hourTo),
      hourTo: Math.max(hourFrom, hourTo),
      granularity,
      split,
    }
    if (level === 'route') {
      if (!routes.length) return null
      if (routes.length !== meta.routes.length) p.routes = routes.join(',')
    } else if (level === 'stop') {
      if (!stopIds.length) return null
      p.stops = stopIds.join(',')
      p.routes = String(stopRoute) // только выбранный маршрут, хотя остановку могут обслуживать и другие
    } else {
      if (segA === null || segB === null) return null
      p.segment = `${stopRoute}:${stopDir}:${Math.min(segA, segB)}-${Math.max(segA, segB)}`
    }
    return p
  }, [range, horizon, hourFrom, hourTo, granularity, split, level, routes, meta.routes.length, stopIds, segA, segB, stopRoute, stopDir])

  const url = params ? `${API}/forecast${qs(params)}` : null
  const res = useApi<ForecastResponse>(url, 150)
  const data = params ? res.data : undefined

  const chart = useMemo(() => {
    if (!data) return null
    const rows = new Map<string, Record<string, number | string | [number, number]>>()
    let hasBand = false
    for (const s of data.series) {
      for (const p of s.points) {
        let row = rows.get(p.t)
        if (!row) {
          row = { t: p.t }
          rows.set(p.t, row)
        }
        row[s.key] = p.pred
        if (p.lo != null && p.hi != null) {
          row[`${s.key}__band`] = [p.lo, p.hi]
          hasBand = true
        }
      }
    }
    const arr = [...rows.values()].sort((a, b) => String(a.t).localeCompare(String(b.t)))
    return { rows: arr, hasBand, points: arr.length * data.series.length }
  }, [data])

  const tableRows: TableRow[] = useMemo(() => {
    if (!data) return []
    const out: TableRow[] = []
    for (const s of data.series)
      for (const p of s.points) out.push({ t: p.t, label: s.label, color: s.color, pred: p.pred, lo: p.lo, hi: p.hi })
    out.sort((a, b) => a.t.localeCompare(b.t) || a.label.localeCompare(b.label, 'ru', { numeric: true }))
    return out
  }, [data])

  const isHistory = horizon === 'history'
  const valueTitle = isHistory ? 'Факт, посадки' : 'Прогноз, посадки'
  const columns: Column<TableRow>[] = [
    { key: 't', title: 'Период', render: (r) => fmtPeriod(r.t) },
    {
      key: 's',
      title: 'Ряд',
      render: (r) => (
        <span className="series-cell">
          <i style={{ background: r.color }} />
          {r.label}
        </span>
      ),
    },
    { key: 'p', title: valueTitle, num: true, render: (r) => fmtNum(r.pred, 1) },
    { key: 'lo', title: 'Нижн. граница', num: true, render: (r) => fmtNum(r.lo) },
    { key: 'hi', title: 'Верхн. граница', num: true, render: (r) => fmtNum(r.hi) },
  ]

  // оценка числа строк выгрузки (лимит сервера — 1 000 000)
  const days = daysBetween(range[0], range[1])
  const hoursN = Math.abs(hourTo - hourFrom) + 1
  const periods = granularity === 'hour' ? days * hoursN : granularity === 'day' ? days : granularity === 'month' ? Math.ceil(days / 28) : 1
  const seriesN = exportLevel === 'route' && level === 'route' && split === 'route' ? routes.length : 1
  const routeStopsN = meta.routes.filter((r) => routes.includes(r.route)).reduce((s, r) => s + (r.stops ?? 0), 0)
  const stopsN =
    exportLevel === 'stop'
      ? level === 'stop'
        ? stopIds.length
        : level === 'segment'
          ? Math.abs((segB ?? 0) - (segA ?? 0)) + 1
          : routeStopsN
      : 1
  const exportRows = periods * seriesN * stopsN
  const exportTooBig = exportRows > 1_000_000
  const exportHref = (format: 'csv' | 'xlsx') => (params ? `${API}/export${qs({ ...params, format, level: exportLevel })}` : undefined)

  const tooManyPoints = (chart?.points ?? 0) > 20000

  return (
    <div className="page">
      <div className="controls-grid">
        <Section title="Горизонт и период">
          <div className="seg-control">
            {horizons.map((x) => (
              <button key={x.id} className={x.id === horizon ? 'on' : ''} onClick={() => changeHorizon(x.id)}>
                {x.id === 'day' ? 'День' : x.id === 'month' ? 'Месяц' : x.id === 'year' ? 'Год' : 'История-факт'}
              </button>
            ))}
          </div>
          <div className="muted small">
            {h.label}: {fmtDate(h.from)} — {fmtDate(h.to)}
          </div>
          <div className="row">
            <label className="field">
              <span>С</span>
              <input
                type="date"
                min={h.from}
                max={h.to}
                value={range[0]}
                onChange={(e) => {
                  const v = clampDate(e.target.value, h.from, h.to)
                  setRange(([, t]) => [v, t < v ? v : t])
                }}
              />
            </label>
            <label className="field">
              <span>По</span>
              <input
                type="date"
                min={h.from}
                max={h.to}
                value={range[1]}
                onChange={(e) => {
                  const v = clampDate(e.target.value, h.from, h.to)
                  setRange(([f]) => [f > v ? v : f, v])
                }}
              />
            </label>
          </div>
          <div className="row">
            <label className="field">
              <span>Часы с</span>
              <select value={hourFrom} onChange={(e) => setHourFrom(Number(e.target.value))}>
                {HOURS.map((x) => (
                  <option key={x} value={x}>
                    {hourLabel(x)}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>по</span>
              <select value={hourTo} onChange={(e) => setHourTo(Number(e.target.value))}>
                {HOURS.map((x) => (
                  <option key={x} value={x}>
                    {hourLabel(x)}
                  </option>
                ))}
              </select>
            </label>
            <button className="btn btn-sm btn-ghost" onClick={() => { setHourFrom(7); setHourTo(9) }}>
              утро 7–10
            </button>
            <button className="btn btn-sm btn-ghost" onClick={() => { setHourFrom(17); setHourTo(19) }}>
              вечер 17–20
            </button>
            <button className="btn btn-sm btn-ghost" onClick={() => { setHourFrom(0); setHourTo(23) }}>
              сутки
            </button>
          </div>
          <div className="row">
            <label className="field">
              <span>Гранулярность</span>
              <select value={granularity} onChange={(e) => setGranularity(e.target.value as Granularity)}>
                {h.granularities.map((g) => (
                  <option key={g} value={g}>
                    {granularityLabel(g)}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>Ряды</span>
              <select value={split} onChange={(e) => setSplit(e.target.value as 'route' | 'none')}>
                <option value="route">по маршрутам</option>
                <option value="none">сумма</option>
              </select>
            </label>
          </div>
        </Section>

        <Section title="Уровень агрегации">
          <div className="seg-control">
            <button className={level === 'route' ? 'on' : ''} onClick={() => setLevel('route')}>
              Маршрут
            </button>
            <button className={level === 'stop' ? 'on' : ''} onClick={() => { setLevel('stop'); setExportLevel('stop') }}>
              Остановки
            </button>
            <button className={level === 'segment' ? 'on' : ''} onClick={() => setLevel('segment')}>
              Участок
            </button>
          </div>
          {level === 'route' && <RouteChips routes={meta.routes} selected={routes} onChange={setRoutes} />}
          {level !== 'route' && (
            <>
              <div className="row">
                <label className="field">
                  <span>Маршрут</span>
                  <select
                    value={stopRoute}
                    onChange={(e) => {
                      setStopRoute(Number(e.target.value))
                      setStopIds([])
                      setSegFrom(null)
                      setSegTo(null)
                    }}
                  >
                    {geoRoutes.map((r) => (
                      <option key={r.route} value={r.route}>
                        {r.route} — {r.name || 'без названия'}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  <span>Направление</span>
                  <select
                    value={stopDir}
                    onChange={(e) => {
                      setStopDir(Number(e.target.value))
                      setSegFrom(null)
                      setSegTo(null)
                    }}
                  >
                    {directions.map((d) => (
                      <option key={d} value={d}>
                        {d === 0 ? 'прямое (0)' : 'обратное (1)'}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <StateBox loading={stopsRes.loading && !stopsRes.data} error={stopsRes.error} onRetry={stopsRes.reload}>
                {level === 'stop' ? (
                  <div>
                    <div className="row">
                      <input className="grow" placeholder="Поиск остановки…" value={stopFilter} onChange={(e) => setStopFilter(e.target.value)} />
                      <span className="muted small">выбрано: {stopIds.length}</span>
                      <button className="btn btn-sm btn-ghost" onClick={() => setStopIds([])} disabled={!stopIds.length}>
                        очистить
                      </button>
                    </div>
                    <div className="check-list">
                      {dirStops
                        .filter((s) => !stopFilter || s.name.toLowerCase().includes(stopFilter.toLowerCase()))
                        .map((s) => (
                          <label key={`${s.stopId}-${s.seq}`} className="check-item">
                            <input
                              type="checkbox"
                              checked={stopIds.includes(s.stopId)}
                              onChange={(e) =>
                                setStopIds((ids) => (e.target.checked ? [...ids, s.stopId] : ids.filter((x) => x !== s.stopId)))
                              }
                            />
                            <span className="muted">№{s.seq}</span> {s.name}
                            <span className="muted small grow-right">{fmtNum(s.weight * 100, 2)} %</span>
                          </label>
                        ))}
                    </div>
                    {!stopIds.length && <div className="hint">Отметьте одну или несколько остановок</div>}
                  </div>
                ) : (
                  <div className="row">
                    <label className="field grow">
                      <span>От остановки</span>
                      <select value={segA ?? ''} onChange={(e) => setSegFrom(Number(e.target.value))}>
                        {dirStops.map((s) => (
                          <option key={s.seq} value={s.seq}>
                            №{s.seq} {s.name}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="field grow">
                      <span>До остановки</span>
                      <select value={segB ?? ''} onChange={(e) => setSegTo(Number(e.target.value))}>
                        {dirStops.map((s) => (
                          <option key={s.seq} value={s.seq}>
                            №{s.seq} {s.name}
                          </option>
                        ))}
                      </select>
                    </label>
                  </div>
                )}
              </StateBox>
              <div className="muted small">
                Остановочный уровень: посадки маршрута распределяются по остановкам по априорным весам (в валидациях нет id
                остановки). Доступны маршруты со справочником остановок.
              </div>
            </>
          )}
        </Section>

        <Section title="Выгрузка">
          <div className="row">
            <label className="field">
              <span>Детализация файла</span>
              <select value={exportLevel} onChange={(e) => setExportLevel(e.target.value as 'route' | 'stop')}>
                <option value="route">по маршрутам</option>
                <option value="stop">по остановкам</option>
              </select>
            </label>
          </div>
          <div className="row">
            {params && !exportTooBig ? (
              <>
                <button className="btn btn-primary" onClick={() => download(exportHref('csv'))}>
                  ⬇ CSV
                </button>
                <button className="btn btn-primary" onClick={() => download(exportHref('xlsx'))}>
                  ⬇ XLSX
                </button>
              </>
            ) : (
              <>
                <button className="btn" disabled>
                  ⬇ CSV
                </button>
                <button className="btn" disabled>
                  ⬇ XLSX
                </button>
              </>
            )}
          </div>
          <div className={`small ${exportTooBig ? 'text-warn' : 'muted'}`}>
            {exportTooBig
              ? `Оценка ≈ ${fmtNum(exportRows)} строк — больше лимита 1 000 000. Сузьте период, часы или укрупните гранулярность.`
              : `Те же параметры, что на графике. Оценка ≈ ${fmtNum(exportRows)} строк. CSV — разделитель «;», UTF-8 (открывается в Excel).`}
          </div>
        </Section>
      </div>

      {!params ? (
        <div className="state-box state-empty">
          {level === 'route' ? 'Выберите хотя бы один маршрут' : level === 'stop' ? 'Выберите остановки' : 'Выберите участок'}
        </div>
      ) : (
        <>
          <div className="kpi-row">
            <Kpi
              label={isHistory ? 'Факт за период' : 'Прогноз за период'}
              value={data ? fmtNum(data.totals.pred) : '—'}
              sub={`${data?.unit ?? 'посадки'} · ${fmtDate(range[0])} — ${fmtDate(range[1])}, ${hourLabel(Math.min(hourFrom, hourTo))}–${String(Math.max(hourFrom, hourTo) + 1).padStart(2, '0')}:00`}
              accent="#1f5fa8"
            />
            <Kpi label="В среднем за день" value={data ? fmtNum(data.totals.pred / Math.max(1, days)) : '—'} sub={`${days} дн.`} />
            {(data?.byRoute ?? []).slice(0, 10).map((r) => (
              <Kpi
                key={r.route}
                label={`Маршрут ${r.route}`}
                value={fmtNum(r.pred)}
                sub={data && data.totals.pred > 0 ? `${fmtNum((r.pred / data.totals.pred) * 100, 1)} % сети` : undefined}
                accent={meta.routes.find((x) => x.route === r.route)?.color}
              />
            ))}
          </div>

          <Section
            title={`${isHistory ? 'Факт' : 'Прогноз'} посадок, ${granularityLabel(granularity)}`}
            extra={
              <>
                {res.loading && <Spinner />}
                {data && <span className="muted small">расчёт {fmtNum(data.computeMs, 2)} мс</span>}
              </>
            }
          >
            <StateBox loading={res.loading && !data} error={res.error} onRetry={res.reload} empty={data && !data.series.length ? 'Нет данных для выбранных параметров' : false}>
              {chart && data && (
                tooManyPoints ? (
                  <div className="state-box state-empty">
                    Слишком много точек для графика ({fmtNum(chart.points)}). Укрупните гранулярность или сузьте период — таблица и выгрузка
                    доступны.
                  </div>
                ) : granularity === 'total' ? (
                  <div className="chart-box">
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart
                        data={data.series.map((s) => {
                          const p = s.points[0]
                          return { name: s.label, pred: p?.pred ?? 0, err: p?.lo != null && p?.hi != null ? [p.pred - p.lo, p.hi - p.pred] : [0, 0], color: s.color }
                        })}
                        margin={{ top: 10, right: 20, left: 20, bottom: 0 }}
                      >
                        <CartesianGrid strokeDasharray="3 3" vertical={false} />
                        <XAxis dataKey="name" tick={{ fontSize: 11 }} />
                        <YAxis tickFormatter={(v) => fmtNum(Number(v))} width={80} />
                        <Tooltip formatter={(v) => fmtNum(Number(v))} />
                        <Bar dataKey="pred" name={isHistory ? 'Факт' : 'Прогноз'} isAnimationActive={false}>
                          <ErrorBar dataKey="err" width={6} stroke="#333" />
                        </Bar>
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                ) : (
                  <div className="chart-box">
                    <ResponsiveContainer width="100%" height="100%">
                      <ComposedChart data={chart.rows} margin={{ top: 10, right: 20, left: 10, bottom: 0 }}>
                        <CartesianGrid strokeDasharray="3 3" vertical={false} />
                        <XAxis dataKey="t" tickFormatter={(t) => fmtTick(String(t))} minTickGap={28} tick={{ fontSize: 11 }} />
                        <YAxis tickFormatter={(v) => fmtNum(Number(v))} width={70} tick={{ fontSize: 11 }} />
                        <Tooltip
                          labelFormatter={(t) => fmtPeriod(String(t))}
                          formatter={(v, name) =>
                            Array.isArray(v) ? [`${fmtNum(Number(v[0]))} … ${fmtNum(Number(v[1]))}`, String(name)] : [fmtNum(Number(v), 1), String(name)]
                          }
                        />
                        <Legend />
                        {data.series.map((s) =>
                          chart.hasBand ? (
                            <Area
                              key={`${s.key}-band`}
                              dataKey={`${s.key}__band`}
                              name={`${s.label}: интервал 80 %`}
                              stroke="none"
                              fill={s.color}
                              fillOpacity={0.14}
                              isAnimationActive={false}
                              legendType="none"
                            />
                          ) : null,
                        )}
                        {data.series.map((s) => (
                          <Line
                            key={s.key}
                            dataKey={s.key}
                            name={s.label}
                            stroke={s.color}
                            strokeWidth={1.8}
                            dot={chart.rows.length <= 40}
                            isAnimationActive={false}
                            type="monotone"
                          />
                        ))}
                      </ComposedChart>
                    </ResponsiveContainer>
                  </div>
                )
              )}
              {data?.notes?.length ? (
                <ul className="notes">
                  {data.notes.map((n, i) => (
                    <li key={i}>{n}</li>
                  ))}
                </ul>
              ) : null}
            </StateBox>
          </Section>

          <Section title="Таблица значений" extra={<span className="muted small">по периоду и ряду</span>}>
            <DataTable rows={tableRows} columns={columns} pageSize={50} maxHeight={520} />
          </Section>

          {data && data.byRoute.length > 0 && (
            <Section title="Итоги по маршрутам">
              <div className="route-totals">
                {data.byRoute.map((r) => (
                  <div key={r.route} className="route-total">
                    <RouteBadge route={r.route} color={meta.routes.find((x) => x.route === r.route)?.color ?? '#607d8b'} />
                    <span className="grow">{meta.routes.find((x) => x.route === r.route)?.name || `Маршрут ${r.route}`}</span>
                    <b className="num">{fmtNum(r.pred)}</b>
                  </div>
                ))}
              </div>
            </Section>
          )}
        </>
      )}
    </div>
  )
}
