import { useMemo, useState, type ReactNode } from 'react'
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { API, downloadFile, errorMessage, qs, type ForecastResponse, type QueryValue } from '../api'
import { Kpi, RouteBadge, RouteChips, Section, Spinner, StateBox } from '../components'
import { clampDate, DOW_SHORT, fmtDate, fmtNum, fmtPct, fmtPeriod, fmtTick, hourLabel, MONTHS_FULL, monthsInRange } from '../format'
import { notify, useApi, useMeta } from '../hooks'

const download = (url: string) => {
  downloadFile(url).catch((e: unknown) => notify(errorMessage(e)))
}

type Base = 'day' | 'year'

interface PrecipDay {
  date: string
  mm: number
}

interface ScenarioEvent {
  id: number
  routes: number[] // пусто = все маршруты
  start: string
  end: string
  mult: number
  allDay: boolean
  hourFrom: number
  hourTo: number
}

const HOURS = Array.from({ length: 24 }, (_, h) => h)
let eventSeq = 1

function multParam(m: Record<number, number>): string | undefined {
  const parts = Object.entries(m)
    .filter(([, v]) => Math.abs(v - 1) > 1e-9)
    .map(([k, v]) => `${k}:${Number(v.toFixed(3))}`)
  return parts.length ? parts.join(',') : undefined
}

function SliderRow({
  label,
  value,
  onChange,
  min = 0.5,
  max = 1.5,
  step = 0.01,
}: {
  label: ReactNode
  value: number
  onChange: (v: number) => void
  min?: number
  max?: number
  step?: number
}) {
  const changed = Math.abs(value - 1) > 1e-9
  return (
    <div className={`slider-row ${changed ? 'changed' : ''}`}>
      <span className="slider-label">{label}</span>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
      />
      <span className="slider-val num">×{value.toFixed(2)}</span>
      <button className="icon-btn" title="Сбросить к 1" disabled={!changed} onClick={() => onChange(1)}>
        ↺
      </button>
    </div>
  )
}

export default function ScenarioPage() {
  const meta = useMeta()
  const [base, setBase] = useState<Base>('day')
  const horizon = meta.horizons.find((h) => h.id === base) ?? meta.horizons[0]
  const [focus, setFocus] = useState<'all' | number>('all')
  const defaultBeta = meta.weather.beta
  const [beta, setBeta] = useState(defaultBeta)
  const [precip, setPrecip] = useState<PrecipDay[]>([])
  const [pDate, setPDate] = useState('2025-12-05')
  const [pMm, setPMm] = useState(15)
  const [monthMult, setMonthMult] = useState<Record<number, number>>({})
  const [routeMult, setRouteMult] = useState<Record<number, number>>({})
  const [dowMult, setDowMult] = useState<Record<number, number>>({})
  const [events, setEvents] = useState<ScenarioEvent[]>([])
  const [evForm, setEvForm] = useState<Omit<ScenarioEvent, 'id'>>({
    routes: [],
    start: '2025-12-20',
    end: '2025-12-20',
    mult: 1.3,
    allDay: false,
    hourFrom: 10,
    hourTo: 18,
  })
  const [showModelEvents, setShowModelEvents] = useState(false)

  const reset = () => {
    setBeta(defaultBeta)
    setPrecip([])
    setMonthMult({})
    setRouteMult({})
    setDowMult({})
    setEvents([])
  }

  const switchBase = (b: Base) => {
    const d = b === 'day' ? '2025-12-20' : '2026-06-12'
    setBase(b)
    reset()
    setFocus('all')
    setPDate(b === 'day' ? '2025-12-05' : '2026-06-15')
    setEvForm((f) => ({ ...f, start: d, end: d }))
  }

  const months = monthsInRange(horizon.from, horizon.to)

  const params: Record<string, QueryValue> = useMemo(() => {
    const ev = events.map((e) => {
      const r = e.routes.length === 0 || e.routes.length === meta.routes.length ? '*' : e.routes.join('|')
      const hours = e.allDay ? '' : `:${Math.min(e.hourFrom, e.hourTo)}-${Math.max(e.hourFrom, e.hourTo)}`
      return `${r}:${e.start}:${e.end}:${e.mult}${hours}`
    })
    return {
      horizon: base,
      split: 'route',
      granularity: 'day',
      precip: precip.length ? precip.map((p) => `${p.date}:${p.mm}`).join(',') : undefined,
      beta: precip.length && beta !== defaultBeta ? beta : undefined,
      monthMult: multParam(monthMult),
      routeMult: multParam(routeMult),
      dowMult: multParam(dowMult),
      event: ev,
    }
  }, [base, precip, beta, defaultBeta, monthMult, routeMult, dowMult, events, meta.routes.length])

  const res = useApi<ForecastResponse>(`${API}/forecast${qs(params)}`, 250)
  const data = res.data

  const chartRows = useMemo(() => {
    if (!data) return []
    const rows = new Map<string, { t: string; base: number; pred: number }>()
    for (const s of data.series) {
      if (focus !== 'all' && s.key !== String(focus)) continue
      for (const p of s.points) {
        const r = rows.get(p.t) ?? { t: p.t, base: 0, pred: 0 }
        r.base += p.base
        r.pred += p.pred
        rows.set(p.t, r)
      }
    }
    return [...rows.values()].sort((a, b) => a.t.localeCompare(b.t))
  }, [data, focus])

  const changedCount =
    precip.length +
    events.length +
    Object.values(monthMult).filter((v) => v !== 1).length +
    Object.values(routeMult).filter((v) => v !== 1).length +
    Object.values(dowMult).filter((v) => v !== 1).length


  const focusColor = focus === 'all' ? '#1f5fa8' : (meta.routes.find((r) => r.route === focus)?.color ?? '#1f5fa8')
  const exportHref = (format: 'csv' | 'xlsx') => `${API}/export${qs({ ...params, format, level: 'route' })}`

  const applyPreset = (kind: 'rain' | 'fest' | 'repair' | 'winter') => {
    if (kind === 'rain') {
      const d = base === 'day' ? '2025-12-05' : '2026-07-08'
      setPrecip((p) => [...p.filter((x) => x.date !== d), { date: d, mm: 30 }])
    } else if (kind === 'fest') {
      const d = base === 'day' ? '2025-12-20' : '2026-06-12'
      setEvents((e) => [...e, { id: eventSeq++, routes: [], start: d, end: d, mult: 1.35, allDay: false, hourFrom: 12, hourTo: 22 }])
    } else if (kind === 'repair') {
      const [s, e] = base === 'day' ? ['2025-11-17', '2025-11-23'] : ['2026-07-01', '2026-07-31']
      setEvents((ev) => [...ev, { id: eventSeq++, routes: [11], start: s, end: e, mult: 0.5, allDay: true, hourFrom: 0, hourTo: 23 }])
    } else {
      setMonthMult((m) => {
        const next = { ...m }
        for (const mo of months) if ([12, 1, 2].includes(mo)) next[mo] = 0.92
        return next
      })
    }
  }

  return (
    <div className="page scenario-page">
      <div className="scenario-layout">
        <div className="scenario-controls">
          <Section
            title="Базовый прогноз"
            extra={
              <button className="btn btn-sm" onClick={reset} disabled={!changedCount}>
                Сбросить
              </button>
            }
          >
            <div className="seg-control">
              <button className={base === 'day' ? 'on' : ''} onClick={() => switchBase('day')}>
                Ноябрь–декабрь 2025
              </button>
              <button className={base === 'year' ? 'on' : ''} onClick={() => switchBase('year')}>
                Год 2026
              </button>
            </div>
            <div className="muted small">
              {horizon.label}: {fmtDate(horizon.from)} — {fmtDate(horizon.to)}. Любое изменение сразу пересчитывает прогноз.
            </div>
            <div className="presets">
              <span className="muted small">Быстрые сценарии:</span>
              <button className="btn btn-sm btn-ghost" onClick={() => applyPreset('rain')}>
                ливень 30 мм
              </button>
              <button className="btn btn-sm btn-ghost" onClick={() => applyPreset('fest')}>
                городской праздник ×1,35
              </button>
              <button className="btn btn-sm btn-ghost" onClick={() => applyPreset('repair')}>
                ремонт на 11-м ×0,5
              </button>
              <button className="btn btn-sm btn-ghost" onClick={() => applyPreset('winter')}>
                холодная зима −8 %
              </button>
            </div>
          </Section>

          <Section title="Погода: осадки">
            <div className="row">
              <label className="field">
                <span>Дата</span>
                <input
                  type="date"
                  min={horizon.from}
                  max={horizon.to}
                  value={pDate}
                  onChange={(e) => setPDate(clampDate(e.target.value, horizon.from, horizon.to))}
                />
              </label>
              <label className="field grow">
                <span>
                  Осадки: <b>{pMm} мм</b>
                </span>
                <input type="range" min={0} max={40} step={1} value={pMm} onChange={(e) => setPMm(Number(e.target.value))} />
              </label>
              <button
                className="btn btn-primary"
                onClick={() => setPrecip((p) => [...p.filter((x) => x.date !== pDate), { date: pDate, mm: pMm }].sort((a, b) => a.date.localeCompare(b.date)))}
              >
                Добавить
              </button>
            </div>
            {precip.length > 0 && (
              <div className="tag-list">
                {precip.map((p) => (
                  <span key={p.date} className="tag">
                    {fmtDate(p.date)}: {p.mm} мм
                    <button onClick={() => setPrecip((x) => x.filter((y) => y.date !== p.date))} aria-label="Удалить">
                      ×
                    </button>
                  </span>
                ))}
              </div>
            )}
            <div className="row">
              <label className="field">
                <span>β (чувствительность)</span>
                <input type="number" step={0.001} value={beta} onChange={(e) => setBeta(Number(e.target.value))} style={{ width: 110 }} />
              </label>
              <div className="muted small grow">
                k = exp(β·(ln(1+мм) − норма месяца)); по умолчанию β = {defaultBeta}. Применяется к добавленным дням.
              </div>
            </div>
          </Section>

          <Section title="Сезон: множители месяцев">
            {months.map((m) => (
              <SliderRow key={m} label={MONTHS_FULL[m - 1]} value={monthMult[m] ?? 1} onChange={(v) => setMonthMult((x) => ({ ...x, [m]: v }))} />
            ))}
          </Section>

          <Section title="Маршруты: множители">
            {meta.routes.map((r) => (
              <SliderRow
                key={r.route}
                label={<RouteBadge route={r.route} color={r.color} />}
                value={routeMult[r.route] ?? 1}
                onChange={(v) => setRouteMult((x) => ({ ...x, [r.route]: v }))}
              />
            ))}
          </Section>

          <Section title="Дни недели: множители">
            {DOW_SHORT.map((d, i) => (
              <SliderRow key={d} label={d} value={dowMult[i + 1] ?? 1} onChange={(v) => setDowMult((x) => ({ ...x, [i + 1]: v }))} />
            ))}
          </Section>

          <Section title="События">
            <div className="field">
              <span>Маршруты (не выбрано — все)</span>
              <RouteChips routes={meta.routes} selected={evForm.routes} onChange={(v) => setEvForm((f) => ({ ...f, routes: v }))} compact />
            </div>
            <div className="row">
              <label className="field">
                <span>Начало</span>
                <input
                  type="date"
                  min={horizon.from}
                  max={horizon.to}
                  value={evForm.start}
                  onChange={(e) => {
                    const v = clampDate(e.target.value, horizon.from, horizon.to)
                    setEvForm((f) => ({ ...f, start: v, end: f.end < v ? v : f.end }))
                  }}
                />
              </label>
              <label className="field">
                <span>Конец</span>
                <input
                  type="date"
                  min={horizon.from}
                  max={horizon.to}
                  value={evForm.end}
                  onChange={(e) => {
                    const v = clampDate(e.target.value, horizon.from, horizon.to)
                    setEvForm((f) => ({ ...f, end: v, start: f.start > v ? v : f.start }))
                  }}
                />
              </label>
              <label className="field">
                <span>Множитель</span>
                <input
                  type="number"
                  min={0}
                  max={5}
                  step={0.05}
                  value={evForm.mult}
                  onChange={(e) => setEvForm((f) => ({ ...f, mult: Math.max(0, Number(e.target.value)) }))}
                  style={{ width: 80 }}
                />
              </label>
            </div>
            <div className="row">
              <label className="check-inline">
                <input type="checkbox" checked={evForm.allDay} onChange={(e) => setEvForm((f) => ({ ...f, allDay: e.target.checked }))} /> весь день
              </label>
              {!evForm.allDay && (
                <>
                  <label className="field">
                    <span>Часы с</span>
                    <select value={evForm.hourFrom} onChange={(e) => setEvForm((f) => ({ ...f, hourFrom: Number(e.target.value) }))}>
                      {HOURS.map((h) => (
                        <option key={h} value={h}>
                          {hourLabel(h)}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="field">
                    <span>по</span>
                    <select value={evForm.hourTo} onChange={(e) => setEvForm((f) => ({ ...f, hourTo: Number(e.target.value) }))}>
                      {HOURS.map((h) => (
                        <option key={h} value={h}>
                          {hourLabel(h)}
                        </option>
                      ))}
                    </select>
                  </label>
                </>
              )}
              <button className="btn btn-primary" onClick={() => setEvents((e) => [...e, { ...evForm, id: eventSeq++ }])}>
                Добавить событие
              </button>
            </div>
            {events.length > 0 && (
              <ul className="event-list">
                {events.map((e) => (
                  <li key={e.id}>
                    <span>
                      {e.routes.length === 0 ? 'все маршруты' : `маршруты ${e.routes.join(', ')}`} · {fmtDate(e.start)}
                      {e.end !== e.start ? ` — ${fmtDate(e.end)}` : ''} · {e.allDay ? 'весь день' : `${hourLabel(Math.min(e.hourFrom, e.hourTo))}–${hourLabel(Math.max(e.hourFrom, e.hourTo) + 1)}`} · ×
                      {e.mult}
                    </span>
                    <button className="icon-btn" onClick={() => setEvents((x) => x.filter((y) => y.id !== e.id))} aria-label="Удалить событие">
                      ×
                    </button>
                  </li>
                ))}
              </ul>
            )}
            <label className="check-inline">
              <input type="checkbox" checked={showModelEvents} onChange={(e) => setShowModelEvents(e.target.checked)} /> показать события, уже учтённые в модели
            </label>
            {showModelEvents && (
              <ul className="model-events">
                {meta.events.map((e) => (
                  <li key={e.name}>
                    <b>{e.name}</b> · {e.routes ? `маршруты ${e.routes.join(', ')}` : 'все маршруты'} · {fmtDate(e.start)} — {fmtDate(e.end)}
                    {e.hours ? ` · часы ${e.hours.join(', ')}` : ''} · ×{e.mult}
                    <div className="muted small">{e.note}</div>
                  </li>
                ))}
                {meta.regimes.map((r, i) => (
                  <li key={`r${i}`}>
                    <b>Режим маршрута {r.route}</b> ({r.state}, {r.days === 'offdays' ? 'выходные' : 'все дни'}) · {fmtDate(r.start)} — {fmtDate(r.end)}
                    <div className="muted small">{r.note}</div>
                  </li>
                ))}
              </ul>
            )}
          </Section>
        </div>

        <div className="scenario-results">
          <div className="kpi-row">
            <Kpi label="Базовый прогноз" value={data ? fmtNum(data.totals.base) : '—'} sub="посадки за горизонт" />
            <Kpi label="Сценарий" value={data ? fmtNum(data.totals.pred) : '—'} sub={`изменений: ${changedCount}`} accent="#1f5fa8" />
            <Kpi
              label="Изменение"
              value={
                data ? (
                  <span className={data.totals.delta > 0 ? 'text-up' : data.totals.delta < 0 ? 'text-down' : ''}>
                    {data.totals.delta > 0 ? '+' : ''}
                    {fmtNum(data.totals.delta)}
                  </span>
                ) : (
                  '—'
                )
              }
              sub={data ? fmtPct(data.totals.deltaPct, 2) : undefined}
            />
          </div>
          <Section
            title="База и сценарий по дням"
            extra={
              <>
                {res.loading && <Spinner label="Пересчёт…" />}
                {data && <span className="muted small">расчёт {fmtNum(data.computeMs, 2)} мс</span>}
                <select value={String(focus)} onChange={(e) => setFocus(e.target.value === 'all' ? 'all' : Number(e.target.value))}>
                  <option value="all">вся сеть</option>
                  {meta.routes.map((r) => (
                    <option key={r.route} value={r.route}>
                      маршрут {r.route}
                    </option>
                  ))}
                </select>
              </>
            }
          >
            <StateBox loading={!data && res.loading} error={res.error} onRetry={res.reload}>
              <div className="chart-box">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={chartRows} margin={{ top: 10, right: 20, left: 10, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" vertical={false} />
                    <XAxis dataKey="t" tickFormatter={(t) => fmtTick(String(t))} minTickGap={24} tick={{ fontSize: 11 }} />
                    <YAxis tickFormatter={(v) => fmtNum(Number(v))} width={70} tick={{ fontSize: 11 }} />
                    <Tooltip labelFormatter={(t) => fmtPeriod(String(t))} formatter={(v, n) => [fmtNum(Number(v)), String(n)]} />
                    <Legend />
                    <Line dataKey="base" name="База (модель)" stroke="#8a94a6" strokeDasharray="5 4" strokeWidth={1.6} dot={false} isAnimationActive={false} />
                    <Line dataKey="pred" name="Сценарий" stroke={focusColor} strokeWidth={2} dot={false} isAnimationActive={false} />
                  </LineChart>
                </ResponsiveContainer>
              </div>
              {data?.notes?.length ? (
                <ul className="notes">
                  {data.notes.map((n, i) => (
                    <li key={i}>{n}</li>
                  ))}
                </ul>
              ) : null}
            </StateBox>
          </Section>
          <Section
            title="Влияние по маршрутам"
            extra={
              <>
                <button className="btn btn-sm" onClick={() => download(exportHref('csv'))}>
                  ⬇ CSV сценария
                </button>
                <button className="btn btn-sm" onClick={() => download(exportHref('xlsx'))}>
                  ⬇ XLSX сценария
                </button>
              </>
            }
          >
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Маршрут</th>
                    <th className="num">База</th>
                    <th className="num">Сценарий</th>
                    <th className="num">Δ</th>
                    <th className="num">Δ, %</th>
                  </tr>
                </thead>
                <tbody>
                  {(data?.byRoute ?? []).map((r) => (
                    <tr key={r.route} className={focus === r.route ? 'row-active' : undefined} onClick={() => setFocus(focus === r.route ? 'all' : r.route)}>
                      <td>
                        <RouteBadge route={r.route} color={meta.routes.find((x) => x.route === r.route)?.color ?? '#607d8b'} />
                      </td>
                      <td className="num">{fmtNum(r.base)}</td>
                      <td className="num">{fmtNum(r.pred)}</td>
                      <td className={`num ${r.delta > 0 ? 'text-up' : r.delta < 0 ? 'text-down' : ''}`}>{fmtNum(r.delta)}</td>
                      <td className={`num ${r.delta > 0 ? 'text-up' : r.delta < 0 ? 'text-down' : ''}`}>{fmtPct(r.deltaPct, 2)}</td>
                    </tr>
                  ))}
                </tbody>
                {data && (
                  <tfoot>
                    <tr>
                      <td>Итого</td>
                      <td className="num">{fmtNum(data.totals.base)}</td>
                      <td className="num">{fmtNum(data.totals.pred)}</td>
                      <td className="num">{fmtNum(data.totals.delta)}</td>
                      <td className="num">{fmtPct(data.totals.deltaPct, 2)}</td>
                    </tr>
                  </tfoot>
                )}
              </table>
            </div>
            <div className="muted small">Клик по строке — показать маршрут на графике.</div>
          </Section>
        </div>
      </div>
    </div>
  )
}
