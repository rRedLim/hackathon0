import { useMemo, useState } from 'react'
import { Area, AreaChart, CartesianGrid, Legend, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { API, downloadFile, errorMessage, qs, type HorizonMeta, type MapResponse, type SummaryResponse } from '../api'
import { ExternalLink, Kpi, RouteBadge, Section, Spinner, StateBox } from '../components'
import { clampDate, fmtDate, fmtNum, fmtPct, HEAT_GRADIENT, heatColor, hourLabel } from '../format'
import { notify, useApi, useMeta } from '../hooks'

const MIN_DATE = '2025-01-01'
const MAX_DATE = '2026-12-31'

const PRESETS: { date: string; label: string }[] = [
  { date: '2025-11-10', label: '10.11.2025' },
  { date: '2025-12-20', label: '20.12.2025' },
  { date: '2025-12-31', label: '31.12.2025' },
  { date: '2025-10-15', label: '15.10.2025 факт' },
  { date: '2026-03-09', label: '09.03.2026 сценарий' },
]

/** Режим сводки по самой дате — как Horizon.ofDate на бэкенде (границы из /meta). */
function modeInfo(date: string, horizons: HorizonMeta[]): { label: string; cls: string; history: boolean } {
  const histTo = horizons.find((h) => h.id === 'history')?.to ?? '2025-10-31'
  const yearFrom = horizons.find((h) => h.id === 'year')?.from ?? '2026-01-01'
  const m = date <= histTo ? 'history' : date >= yearFrom ? 'year' : 'forecast'
  if (m === 'history') return { label: 'факт', cls: 'mode-history', history: true }
  if (m === 'year') return { label: 'сценарий 2026', cls: 'mode-year', history: false }
  return { label: 'прогноз', cls: 'mode-forecast', history: false }
}

const fmtSigned = (v: number) => `${v > 0 ? '+' : v < 0 ? '−' : ''}${fmtNum(Math.abs(v))}`

function Delta({ pct }: { pct: number | null | undefined }) {
  if (pct === null || pct === undefined) return <span className="muted">—</span>
  return <span className={pct > 0 ? 'text-up' : pct < 0 ? 'text-down' : ''}>{fmtPct(pct, 1)}</span>
}

/** Тепловая карта «остановка × час» для одного маршрута и направления (данные /map). */
function SegmentHeatmap({ map, loading, error, onRetry }: { map: MapResponse | undefined; loading: boolean; error?: string; onRetry: () => void }) {
  const routesWithStops = useMemo(() => {
    const set = new Map<number, string>()
    for (const s of map?.stops ?? []) if (!set.has(s.route)) set.set(s.route, map?.routes.find((r) => r.route === s.route)?.color ?? '#607d8b')
    return [...set.entries()].sort((a, b) => a[0] - b[0])
  }, [map])
  const [route, setRoute] = useState<number | null>(null)
  const [direction, setDirection] = useState(0)
  const [activeRow, setActiveRow] = useState<string | null>(null)
  const r = route !== null && routesWithStops.some(([x]) => x === route) ? route : (routesWithStops.find(([x]) => x === 17)?.[0] ?? routesWithStops[0]?.[0] ?? null)
  const directions = useMemo(
    () => [...new Set((map?.stops ?? []).filter((s) => s.route === r).map((s) => s.direction))].sort(),
    [map, r],
  )
  const dir = directions.includes(direction) ? direction : (directions[0] ?? 0)
  const rows = useMemo(
    () => (map?.stops ?? []).filter((s) => s.route === r && s.direction === dir).sort((a, b) => a.seq - b.seq),
    [map, r, dir],
  )
  const max = Math.max(0, ...rows.flatMap((s) => s.hourly ?? []))
  const colTotals = Array.from({ length: 24 }, (_, h) => rows.reduce((a, s) => a + (s.hourly?.[h] ?? 0), 0))
  const first = rows[0]?.name
  const last = rows[rows.length - 1]?.name

  return (
    <Section
      title="Тепловая карта: остановки маршрута × час"
      extra={
        <>
          {loading && <Spinner />}
          <label className="field field-inline">
            <span>Маршрут</span>
            <select value={r ?? ''} onChange={(e) => { setRoute(Number(e.target.value)); setActiveRow(null) }}>
              {routesWithStops.map(([x]) => (
                <option key={x} value={x}>
                  {x}
                </option>
              ))}
            </select>
          </label>
          <label className="field field-inline">
            <span>Направление</span>
            <select value={dir} onChange={(e) => { setDirection(Number(e.target.value)); setActiveRow(null) }}>
              {directions.map((d) => (
                <option key={d} value={d}>
                  {d === 0 ? 'прямое' : 'обратное'}
                </option>
              ))}
            </select>
          </label>
        </>
      }
    >
      <StateBox loading={loading && !map} error={error} onRetry={onRetry} empty={map && !rows.length ? 'Для выбранного маршрута нет остановок в справочнике' : false}>
        <div className="muted small">
          Посадки на остановках по часам (прогноз модели, распределённый по остановкам априорными весами).
          {first && last ? ` Направление: ${first} → ${last}.` : ''} Клик по строке выделяет остановку.
        </div>
        <div className="seg-heat-legend">
          <span>0</span>
          <i style={{ background: HEAT_GRADIENT }} />
          <span>{fmtNum(max)} посадок/час</span>
        </div>
        <div className="seg-heat-wrap">
          <table className="seg-heat">
            <thead>
              <tr>
                <th className="seg-heat-name">№ · остановка</th>
                {Array.from({ length: 24 }, (_, h) => (
                  <th key={h}>{h}</th>
                ))}
                <th className="seg-heat-sum">Σ</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((s) => {
                const key = `${s.stopId}-${s.seq}`
                return (
                  <tr key={key} className={activeRow === key ? 'active' : undefined} onClick={() => setActiveRow(activeRow === key ? null : key)}>
                    <th className="seg-heat-name" title={s.name}>
                      <span className="muted">{s.seq}</span> {s.name}
                    </th>
                    {Array.from({ length: 24 }, (_, h) => {
                      const v = s.hourly?.[h] ?? 0
                      return <td key={h} style={{ background: heatColor(v, max) }} title={`${s.name}, ${hourLabel(h)}: ${fmtNum(v, 1)} посадок`} />
                    })}
                    <td className="seg-heat-sum">{fmtNum(s.total)}</td>
                  </tr>
                )
              })}
            </tbody>
            <tfoot>
              <tr>
                <th className="seg-heat-name">Σ по участку</th>
                {colTotals.map((v, h) => (
                  <td key={h} className="seg-heat-sum" title={`${hourLabel(h)}: ${fmtNum(v)}`}>
                    {v >= 1000 ? `${fmtNum(v / 1000, 1)}к` : fmtNum(v)}
                  </td>
                ))}
                <td className="seg-heat-sum">{fmtNum(colTotals.reduce((a, b) => a + b, 0))}</td>
              </tr>
            </tfoot>
          </table>
        </div>
      </StateBox>
    </Section>
  )
}

export default function SummaryPage() {
  const meta = useMeta()
  const [date, setDate] = useState('2025-11-10')
  const [precipMm, setPrecipMm] = useState(0)
  const [exporting, setExporting] = useState(false)

  const mode = modeInfo(date, meta.horizons)
  // для факта сценарий осадков бэкенд отклоняет (400) — не отправляем
  const params = { date, precip: precipMm > 0 && !mode.history ? `${date}:${precipMm}` : undefined }
  const sumRes = useApi<SummaryResponse>(`${API}/summary${qs(params)}`, 150)
  const mapRes = useApi<MapResponse>(`${API}/map${qs({ date })}`)
  const data = sumRes.data
  const changeDate = (d: string) => {
    setDate(d)
    if (modeInfo(d, meta.horizons).history) setPrecipMm(0)
  }
  const colorOf = (route: number) => data?.routes.find((r) => r.route === route)?.color ?? meta.routes.find((r) => r.route === route)?.color ?? '#607d8b'

  const chartRows = useMemo(() => {
    if (!data) return []
    return Array.from({ length: 24 }, (_, h) => {
      const row: Record<string, number> = { h }
      for (const r of data.routes) row[`r${r.route}`] = r.hourly?.[h] ?? 0
      return row
    })
  }, [data])
  const routesSorted = useMemo(() => [...(data?.routes ?? [])].sort((a, b) => b.total - a.total), [data])

  const exportXlsx = async () => {
    setExporting(true)
    try {
      await downloadFile(`${API}/summary/export${qs(params)}`)
    } catch (e) {
      notify(errorMessage(e))
    } finally {
      setExporting(false)
    }
  }

  return (
    <div className="page">
      <Section>
        <div className="row">
          <label className="field">
            <span>Дата</span>
            <input type="date" min={MIN_DATE} max={MAX_DATE} value={date} onChange={(e) => e.target.value && changeDate(clampDate(e.target.value, MIN_DATE, MAX_DATE))} />
          </label>
          <span className={`mode-badge ${mode.cls}`}>{mode.label}</span>
          <div className="quick-dates">
            {PRESETS.map((p) => (
              <button key={p.date} className={`btn btn-sm ${p.date === date ? 'btn-primary' : 'btn-ghost'}`} onClick={() => changeDate(p.date)}>
                {p.label}
              </button>
            ))}
          </div>
          <label className="field summary-precip">
            <span>
              Что если: осадки <b>{precipMm ? `${precipMm} мм` : 'как в прогнозе'}</b>
            </span>
            <input type="range" min={0} max={40} step={1} value={precipMm} onChange={(e) => setPrecipMm(Number(e.target.value))} disabled={mode.history} />
          </label>
          <span className="grow" />
          {sumRes.loading && <Spinner />}
          <button className="btn btn-primary" onClick={exportXlsx} disabled={exporting || !data}>
            {exporting ? 'Готовим файл…' : '⬇ Скачать сводку XLSX'}
          </button>
        </div>
      </Section>

      <StateBox loading={!data && sumRes.loading} error={sumRes.error} onRetry={sumRes.reload}>
        {data && (
          <>
            <div className="kpi-row">
              <Kpi
                label={`Посадки за ${fmtDate(data.date)}`}
                value={fmtNum(data.network.total)}
                sub={
                  data.network.weekAgoTotal != null ? (
                    <>
                      сопоставимый день ({fmtDate(data.network.weekAgoDate).slice(0, 5)}): {fmtNum(data.network.weekAgoTotal)} · <Delta pct={data.network.deltaWeekPct} />
                    </>
                  ) : (
                    'нет сопоставимого дня для сравнения'
                  )
                }
                accent="#1f5fa8"
              />
              <Kpi label="Пик сети" value={`${hourLabel(data.network.peakHour)}`} sub={`${fmtNum(data.network.peakValue)} посадок за час`} accent="#e31a1c" />
              <Kpi
                label="Усиление выпуска"
                value={data.fleet?.available ? `${fmtNum(data.fleet.extraVehicleHours)} ваг.-ч` : '—'}
                sub={data.fleet?.available ? `${fmtNum(data.fleet.candidateHours)} ч с перегрузкой` : 'расчёт усиления есть для ноября–декабря 2025'}
                accent="#e67e22"
              />
              <Kpi
                label="Резерв (экономия)"
                value={data.fleet?.available ? `−${fmtNum(data.fleet.reserveVehicleHours ?? 0)} ваг.-ч` : '—'}
                sub={
                  data.fleet?.available
                    ? `${fmtNum(data.fleet.reserveHours ?? 0)} ч с недогрузкой · сальдо ${fmtSigned((data.fleet.extraVehicleHours ?? 0) - (data.fleet.reserveVehicleHours ?? 0))} ваг.-ч`
                    : 'расчёт резерва есть для ноября–декабря 2025'
                }
                accent="#2f6fb0"
              />
              <Kpi
                label="Тип дня и погода"
                value={data.dayType?.label ?? '—'}
                sub={[
                  data.weather?.precipMm != null ? `осадки ${fmtNum(data.weather.precipMm, 1)} мм` : null,
                  data.weather?.tempC != null ? `${fmtNum(data.weather.tempC, 1)} °C` : null,
                  data.scenario ? 'сценарий «что если»' : null,
                ]
                  .filter(Boolean)
                  .join(' · ') || '—'}
              />
            </div>

            <div className="summary-top">
              <Section className="reco-card" title="Рекомендации диспетчеру" extra={<span className="muted small">расчёт {fmtNum(data.computeMs, 2)} мс</span>}>
                {data.recommendations.length ? (
                  <ol className="reco-list">
                    {data.recommendations.map((r, i) => (
                      <li key={i}>{r}</li>
                    ))}
                  </ol>
                ) : (
                  <div className="muted">Особых действий не требуется.</div>
                )}
              </Section>
              <Section title="Сеть по часам (по маршрутам)">
                <div className="chart-box chart-box-sm">
                  <ResponsiveContainer width="100%" height="100%">
                    <AreaChart data={chartRows} margin={{ top: 20, right: 12, left: 4, bottom: 0 }}>
                      <CartesianGrid strokeDasharray="3 3" vertical={false} />
                      <XAxis dataKey="h" tick={{ fontSize: 11 }} />
                      <YAxis tickFormatter={(v) => fmtNum(Number(v))} width={60} tick={{ fontSize: 11 }} />
                      <Tooltip labelFormatter={(h) => hourLabel(Number(h))} formatter={(v, n) => [fmtNum(Number(v)), String(n)]} />
                      <Legend wrapperStyle={{ fontSize: 11 }} />
                      {data.routes.map((r) => (
                        <Area
                          key={r.route}
                          dataKey={`r${r.route}`}
                          name={`${r.route}`}
                          stackId="net"
                          stroke={r.color}
                          fill={r.color}
                          fillOpacity={0.55}
                          isAnimationActive={false}
                        />
                      ))}
                      <ReferenceLine x={data.network.peakHour} stroke="#c0392b" strokeDasharray="4 3" label={{ value: 'пик', position: 'top', fontSize: 11, fill: '#c0392b' }} />
                    </AreaChart>
                  </ResponsiveContainer>
                </div>
              </Section>
            </div>

            <Section title="Маршруты">
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Маршрут</th>
                      <th className="num">Посадки за сутки</th>
                      <th className="num" title="Ближайший прошлый день с тем же днём недели и типом дня (праздники пропускаются)">
                        Δ к сопоставимому дню{data.network.weekAgoDate ? ` (${fmtDate(data.network.weekAgoDate).slice(0, 5)})` : ''}
                      </th>
                      <th className="num">Пик</th>
                      <th className="num">Доп. / резерв, ваг.-ч</th>
                      <th>Усиление по часам</th>
                      <th>Резерв по часам</th>
                    </tr>
                  </thead>
                  <tbody>
                    {routesSorted.map((r) => (
                      <tr key={r.route}>
                        <td>
                          <span className="route-cell">
                            <RouteBadge route={r.route} color={r.color} />
                            <span className="small">{r.name || <span className="muted">маршрут {r.route}</span>}</span>
                          </span>
                        </td>
                        <td className="num">{fmtNum(r.total)}</td>
                        <td className="num">
                          <Delta pct={r.deltaWeekPct} />
                        </td>
                        <td className="num">
                          {hourLabel(r.peakHour)} · {fmtNum(r.peakValue)}
                        </td>
                        <td className="num">
                          {r.extraVehicleHours ? <b className="text-down">+{fmtNum(r.extraVehicleHours)}</b> : <span className="muted">0</span>}
                          {' / '}
                          {r.reserveVehicleHours ? <b className="text-reserve">−{fmtNum(r.reserveVehicleHours)}</b> : <span className="muted">0</span>}
                        </td>
                        <td>
                          {r.reinforceHours?.length ? (
                            <span className="reinforce-list">
                              {r.reinforceHours.map((h) => (
                                <span
                                  key={h.hour}
                                  className="reinforce-chip"
                                  title={`прогноз ${fmtNum(h.pred)} посадок, выпуск ${fmtNum(h.nPlan, 1)} ваг., норма ${fmtNum(h.norm, 1)} на вагон`}
                                >
                                  {h.hour}:00 +{h.extra}
                                </span>
                              ))}
                            </span>
                          ) : (
                            <span className="muted small">{data.fleet?.available ? 'не требуется' : '—'}</span>
                          )}
                        </td>
                        <td>
                          {r.reserveHours?.length ? (
                            <span className="reinforce-list">
                              {r.reserveHours.map((h) => (
                                <span
                                  key={h.hour}
                                  className="reserve-chip"
                                  title={`прогноз ${fmtNum(h.pred)} посадок, выпуск ${fmtNum(h.nPlan, 1)} ваг., достаточно ${fmtNum(h.need)} ваг. (норма ${fmtNum(h.norm, 1)})`}
                                >
                                  {h.hour}:00 −{h.reserve}
                                </span>
                              ))}
                            </span>
                          ) : (
                            <span className="muted small">{data.fleet?.available ? 'нет' : '—'}</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Section>

            <SegmentHeatmap map={mapRes.data} loading={mapRes.loading} error={mapRes.error} onRetry={mapRes.reload} />

            <div className="grid-2">
              <Section title="Самые загруженные остановки">
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Остановка</th>
                        <th>Маршрут</th>
                        <th className="num">Пиковый час</th>
                        <th className="num">Посадок в пик</th>
                        <th className="num">За сутки</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.hotStops.map((s) => (
                        <tr key={`${s.stopId}-${s.route}-${s.direction}-${s.seq}`}>
                          <td>{s.name}</td>
                          <td>
                            <RouteBadge route={s.route} color={colorOf(s.route)} />{' '}
                            <span className="muted small">{s.direction === 0 ? 'прямое' : 'обратное'}</span>
                          </td>
                          <td className="num">{hourLabel(s.hour)}</td>
                          <td className="num">
                            <b>{fmtNum(s.value, 1)}</b>
                          </td>
                          <td className="num">{fmtNum(s.dayTotal)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </Section>
              <Section title="Действующие события и режимы">
                {data.events.length ? (
                  <ul className="notes-list">
                    {data.events.map((e, i) => (
                      <li key={i}>
                        <div>
                          <span className={`event-kind ${e.kind === 'regime' ? 'kind-regime' : 'kind-event'}`}>{e.kind === 'regime' ? 'режим' : 'событие'}</span>{' '}
                          <b>{e.name}</b>
                          {e.routes?.length ? (
                            <>
                              {' '}
                              ·{' '}
                              {e.routes.map((r) => (
                                <RouteBadge key={r} route={r} color={colorOf(r)} />
                              ))}
                            </>
                          ) : (
                            ' · все маршруты'
                          )}
                        </div>
                        <div className="muted small">{e.note}</div>
                        {e.source?.length ? (
                          <div className="src-links">
                            источники:{' '}
                            {e.source.map((u, k) => (
                              <span key={u}>
                                {k > 0 && ', '}
                                <ExternalLink href={u}>[{k + 1}]</ExternalLink>
                              </span>
                            ))}
                          </div>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <div className="muted">На эту дату особых событий и режимов нет.</div>
                )}
              </Section>
            </div>
          </>
        )}
      </StateBox>
    </div>
  )
}
