import { useMemo, useRef, useState } from 'react'
import { Bar, CartesianGrid, ComposedChart, Legend, Line, ReferenceArea, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { API, apiRequest, errorMessage, qs, type IngestResult, type MonitoringResponse, type SimulateResult } from '../api'
import { Kpi, RouteBadge, Section, Spinner, StateBox } from '../components'
import { fmtDate, fmtNum, fmtScore, hourLabel } from '../format'
import { notify, useApi, useMeta } from '../hooks'

const SAMPLE = `tran_no;device_no;tran_date_time;begin_date_time;input_date_time;crd_hashcode;validation_result;tran_type_id;place_id;good_type;pass_route;ngpt_route;bus_exit_no;garage_number
1;1863694;2025-11-01 08:14:30;2025-11-01 08:14:00;2025-11-01 08:15:27;97a29a2b976630bf7aaf326bee9a267d;1;52;39710;СКМ МГТ;НГПТ;12 трамвай;207;31293
2;1818867;2025-11-01 08:27:37;2025-11-01 08:27:00;2025-11-01 08:28:17;20177219734355940e4d6794040ea817;1;52;39710;КОШЕЛЕК;НГПТ;7 трамвай;210;31345
3;1818867;2025-11-01 08:29:02;2025-11-01 08:29:00;2025-11-01 08:30:11;3974c12f22f1d141f60b1780114ce01a;2;52;39710;КОШЕЛЕК;НГПТ;7 трамвай;210;31345`

export default function MonitoringPage() {
  const meta = useMeta()
  const [date, setDate] = useState('2025-10-31')
  const [chartRoute, setChartRoute] = useState<number | 'all'>('all')
  const [threshold, setThreshold] = useState(25)
  const [minHours, setMinHours] = useState(2)
  const mon = useApi<MonitoringResponse>(`${API}/monitoring${qs({ date, threshold: threshold / 100, minHours })}`, 200)
  const data = mon.data
  const colorOf = (r: number) => meta.routes.find((x) => x.route === r)?.color ?? '#607d8b'

  const [text, setText] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const [sending, setSending] = useState(false)
  const [result, setResult] = useState<IngestResult | null>(null)
  const [ingestError, setIngestError] = useState<string | null>(null)
  const [confirmReset, setConfirmReset] = useState(false)

  // симуляция потока для демонстрации детектора
  const [simDate, setSimDate] = useState('2025-11-10')
  const [simToHour, setSimToHour] = useState(14)
  const [simRoute, setSimRoute] = useState<number | 'none'>(7)
  const [simFrom, setSimFrom] = useState(9)
  const [simTo, setSimTo] = useState(12)
  const [simMult, setSimMult] = useState(0.4)
  const [simResult, setSimResult] = useState<SimulateResult | null>(null)

  const simulate = async () => {
    setSending(true)
    setIngestError(null)
    try {
      const q = qs({
        date: simDate,
        toHour: simToHour,
        anomalyRoute: simRoute === 'none' ? undefined : simRoute,
        anomalyFromHour: simRoute === 'none' ? undefined : Math.min(simFrom, simTo),
        anomalyToHour: simRoute === 'none' ? undefined : Math.max(simFrom, simTo),
        anomalyMult: simRoute === 'none' ? undefined : simMult,
        seed: 42,
      })
      const r = await apiRequest<SimulateResult>(`${API}/ingest/simulate${q}`, { method: 'POST' })
      setSimResult(r)
      setResult(null)
      notify(`Симуляция: сгенерировано ${fmtNum(r.accepted)} валидаций за ${fmtDate(simDate)}`, 'info')
      setDate(simDate)
      mon.reload()
    } catch (e) {
      const msg = errorMessage(e)
      setIngestError(msg)
      notify(msg)
    } finally {
      setSending(false)
    }
  }

  const hourly = useMemo(() => {
    const arr = Array.from({ length: 24 }, (_, h) => ({ h, actual: 0, forecast: 0, has: false }))
    for (const c of data?.cells ?? []) {
      if (chartRoute !== 'all' && c.route !== chartRoute) continue
      const r = arr[c.hour]
      if (!r) continue
      r.actual += c.actual
      r.forecast += c.forecast
      r.has = true
    }
    return arr
  }, [data, chartRoute])

  const lastHour = data?.lastHour ?? null
  const hasIngest = lastHour !== null
  // «Принятые» часы считает сервер: факт по сети ≥ 50 % прогноза (незавершённый поток не штрафуется)
  const covered = useMemo(() => new Set(data?.hoursCovered ?? []), [data])
  const coveredLabel = (data?.hoursCovered ?? []).length
    ? `${hourLabel(Math.min(...covered))}–${hourLabel(Math.max(...covered))} (${covered.size} ч)`
    : 'нет принятых часов'

  const byRoute = useMemo(() => {
    const m = new Map<number, { route: number; actual: number; forecast: number; forecastDay: number; absErr: number }>()
    for (const c of data?.cells ?? []) {
      const r = m.get(c.route) ?? { route: c.route, actual: 0, forecast: 0, forecastDay: 0, absErr: 0 }
      r.forecastDay += c.forecast
      if (covered.has(c.hour)) {
        r.actual += c.actual
        r.forecast += c.forecast
        r.absErr += Math.abs(c.actual - c.forecast)
      }
      m.set(c.route, r)
    }
    return [...m.values()].sort((a, b) => a.route - b.route)
  }, [data, covered])
  const winActual = byRoute.reduce((a, r) => a + r.actual, 0)
  const winForecast = byRoute.reduce((a, r) => a + r.forecast, 0)
  const serverActual = data?.totals?.actual ?? data?.actualTotal
  const alerts = data?.alerts ?? []
  const chartAlerts = alerts.filter((a) => chartRoute === 'all' || a.route === chartRoute)

  const send = async (body: BodyInit) => {
    setSending(true)
    setIngestError(null)
    try {
      const r = await apiRequest<IngestResult>(`${API}/ingest/validations`, { method: 'POST', body, contentType: 'text/csv' })
      setResult(r)
      notify(`Принято записей: ${fmtNum(r.accepted)} из ${fmtNum(r.received)}`, 'info')
      mon.reload()
    } catch (e) {
      const msg = errorMessage(e)
      setIngestError(msg)
      notify(msg)
    } finally {
      setSending(false)
    }
  }

  const resetIngest = async () => {
    setConfirmReset(false)
    setSending(true)
    try {
      await apiRequest<unknown>(`${API}/ingest`, { method: 'DELETE' })
      setResult(null)
      setSimResult(null)
      notify('Принятые данные сброшены', 'info')
      mon.reload()
    } catch (e) {
      notify(errorMessage(e))
    } finally {
      setSending(false)
    }
  }

  return (
    <div className="page">
      <div className="grid-2 grid-2-wide">
        <Section title="Сверка факта с прогнозом" extra={mon.loading ? <Spinner /> : undefined}>
          <div className="row">
            <label className="field">
              <span>Дата</span>
              <input type="date" min="2025-01-01" max="2026-12-31" value={date} onChange={(e) => e.target.value && setDate(e.target.value)} />
            </label>
            <button className="btn btn-sm btn-ghost" onClick={() => setDate('2025-10-31')}>
              31.10.2025
            </button>
            <button className="btn btn-sm btn-ghost" onClick={() => setDate('2025-11-01')}>
              01.11.2025
            </button>
            <label className="field">
              <span>График</span>
              <select value={String(chartRoute)} onChange={(e) => setChartRoute(e.target.value === 'all' ? 'all' : Number(e.target.value))}>
                <option value="all">вся сеть</option>
                {meta.routes.map((r) => (
                  <option key={r.route} value={r.route}>
                    маршрут {r.route}
                  </option>
                ))}
              </select>
            </label>
            <button className="btn btn-sm" onClick={mon.reload}>
              Обновить
            </button>
          </div>
          <StateBox
            loading={!data && mon.loading}
            error={!data ? mon.error : undefined}
            onRetry={mon.reload}
          >
            {data && (
              <>
                <div className="kpi-row">
                  <Kpi
                    label="WAPE-score (принятые часы)"
                    value={data.wapeScore != null ? fmtScore(data.wapeScore) : '—'}
                    sub={hasIngest ? coveredLabel : 'нет принятых данных'}
                    accent="#1f5fa8"
                  />
                  <Kpi label="Факт в принятых часах" value={hasIngest ? fmtNum(winActual) : '—'} sub={serverActual != null ? `за сутки: ${fmtNum(serverActual)}` : fmtDate(data.date)} />
                  <Kpi label="Прогноз в принятых часах" value={hasIngest ? fmtNum(winForecast) : '—'} sub={data.forecastSource ?? ''} />
                  <Kpi label="Последний час данных" value={hasIngest ? hourLabel(lastHour) : '—'} sub={data.receivedTotal != null ? `получено записей: ${fmtNum(data.receivedTotal)}` : undefined} />
                </div>
                <div className="detector">
                  <div className="detector-head">
                    <b>Детектор смены режима</b>
                    <label className="field field-inline">
                      <span>порог отклонения</span>
                      <input type="range" min={10} max={60} step={5} value={threshold} onChange={(e) => setThreshold(Number(e.target.value))} />
                      <b className="num">{threshold} %</b>
                    </label>
                    <label className="field field-inline">
                      <span>не менее</span>
                      <select value={minHours} onChange={(e) => setMinHours(Number(e.target.value))}>
                        {[1, 2, 3, 4, 5, 6].map((h) => (
                          <option key={h} value={h}>
                            {h} ч подряд
                          </option>
                        ))}
                      </select>
                    </label>
                  </div>
                  {!hasIngest ? (
                    <div className="muted small">Алерты появятся после приёма данных за дату (загрузите CSV или запустите симуляцию справа).</div>
                  ) : alerts.length === 0 ? (
                    <div className="alert-ok">Отклонений от прогноза выше {threshold} % дольше {minHours} ч не обнаружено — режим работы штатный.</div>
                  ) : (
                    <ul className="alerts">
                      {alerts.map((a, i) => (
                        <li key={i} className={`alert alert-${a.kind === 'drop' ? 'drop' : 'surge'}`}>
                          <div className="alert-top">
                            <RouteBadge route={a.route} color={colorOf(a.route)} />
                            <span className="alert-kind">{a.kind === 'drop' ? '▼ провал' : '▲ всплеск'}</span>
                            <span className="alert-hours">
                              {hourLabel(a.fromHour)}–{hourLabel((a.toHour + 1) % 24)} · {a.hours} ч
                            </span>
                            <span className="grow" />
                            <span className="num">
                              факт {fmtNum(a.actual)} / прогноз {fmtNum(a.forecast)} · <b>{a.deviationPct > 0 ? '+' : ''}{fmtNum(a.deviationPct, 1)} %</b>
                            </span>
                          </div>
                          <div className="alert-msg">{a.message}</div>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
                {hasIngest && (
                  <div className="muted small">
                    WAPE-score считается по «принятым» часам — где факт по сети набрал не меньше половины прогноза: поток
                    данных приходит постепенно, и единичные запоздавшие записи не должны портить оценку.
                  </div>
                )}
                {!hasIngest && (
                  <div className="hint">За {fmtDate(data.date)} принятых данных нет — загрузите валидации в форме справа. На графике — прогноз на сутки.</div>
                )}
                {data.cells.length > 0 && (
                  <>
                    <div className="chart-box chart-box-sm">
                      <ResponsiveContainer width="100%" height="100%">
                        <ComposedChart data={hourly} margin={{ top: 10, right: 16, left: 6, bottom: 0 }}>
                          <CartesianGrid strokeDasharray="3 3" vertical={false} />
                          <XAxis dataKey="h" tickFormatter={(h) => String(h)} tick={{ fontSize: 11 }} />
                          <YAxis tickFormatter={(v) => fmtNum(Number(v))} width={60} tick={{ fontSize: 11 }} />
                          <Tooltip labelFormatter={(h) => hourLabel(Number(h))} formatter={(v, n) => [fmtNum(Number(v), 1), String(n)]} />
                          <Legend />
                          {chartAlerts.map((a, i) => (
                            <ReferenceArea
                              key={i}
                              x1={a.fromHour}
                              x2={a.toHour}
                              fill={a.kind === 'drop' ? '#e31a1c' : '#f39c12'}
                              fillOpacity={0.12}
                              stroke={a.kind === 'drop' ? '#e31a1c' : '#f39c12'}
                              strokeOpacity={0.4}
                              ifOverflow="extendDomain"
                            />
                          ))}
                          <Bar dataKey="actual" name="Факт (приём)" fill="#1f5fa8" fillOpacity={0.75} isAnimationActive={false} />
                          <Line dataKey="forecast" name="Прогноз" stroke="#e67e22" strokeWidth={2} dot={false} isAnimationActive={false} />
                        </ComposedChart>
                      </ResponsiveContainer>
                    </div>
                    <div className="table-wrap">
                      <table className="table">
                        <thead>
                          <tr>
                            <th>Маршрут</th>
                            <th className="num">Факт (принятые часы)</th>
                            <th className="num">Прогноз (принятые часы)</th>
                            <th className="num">Прогноз на сутки</th>
                            <th className="num">Отклонение</th>
                            <th className="num">WAPE-score</th>
                          </tr>
                        </thead>
                        <tbody>
                          {byRoute.map((r) => {
                            const diff = hasIngest && r.forecast ? (r.actual / r.forecast - 1) * 100 : null
                            const sc = hasIngest && r.actual > 0 ? Math.max(0, 1 - r.absErr / r.actual) : null
                            return (
                              <tr key={r.route}>
                                <td>
                                  <RouteBadge route={r.route} color={colorOf(r.route)} />
                                </td>
                                <td className="num">{hasIngest ? fmtNum(r.actual) : '—'}</td>
                                <td className="num">{hasIngest ? fmtNum(r.forecast) : '—'}</td>
                                <td className="num">{fmtNum(r.forecastDay)}</td>
                                <td className="num">{diff === null ? '—' : `${diff > 0 ? '+' : ''}${fmtNum(diff, 1)} %`}</td>
                                <td className="num">{sc === null ? '—' : fmtScore(sc)}</td>
                              </tr>
                            )
                          })}
                        </tbody>
                      </table>
                    </div>
                  </>
                )}
              </>
            )}
          </StateBox>
        </Section>

        <div className="side-col">
        <Section title="Симуляция потока для демонстрации детектора">
          <div className="muted small">
            Сервер генерирует поток валидаций из прогноза (с шумом) и внедряет аномалию на выбранном маршруте — так можно показать работу
            детектора без реального потока. Это <b>симуляция</b>, не фактические данные.
          </div>
          <div className="row">
            <label className="field">
              <span>Дата</span>
              <input type="date" min="2025-01-01" max="2026-12-31" value={simDate} onChange={(e) => e.target.value && setSimDate(e.target.value)} />
            </label>
            <label className="field">
              <span>Поток до часа</span>
              <select value={simToHour} onChange={(e) => setSimToHour(Number(e.target.value))}>
                {Array.from({ length: 24 }, (_, h) => (
                  <option key={h} value={h}>
                    {hourLabel(h)}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>Аномалия: маршрут</span>
              <select value={String(simRoute)} onChange={(e) => setSimRoute(e.target.value === 'none' ? 'none' : Number(e.target.value))}>
                <option value="none">без аномалии</option>
                {meta.routes.map((r) => (
                  <option key={r.route} value={r.route}>
                    {r.route}
                  </option>
                ))}
              </select>
            </label>
          </div>
          {simRoute !== 'none' && (
            <div className="row">
              <label className="field">
                <span>Часы с</span>
                <select value={simFrom} onChange={(e) => setSimFrom(Number(e.target.value))}>
                  {Array.from({ length: 24 }, (_, h) => (
                    <option key={h} value={h}>
                      {hourLabel(h)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>по</span>
                <select value={simTo} onChange={(e) => setSimTo(Number(e.target.value))}>
                  {Array.from({ length: 24 }, (_, h) => (
                    <option key={h} value={h}>
                      {hourLabel(h)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>Множитель</span>
                <input type="number" min={0} max={2} step={0.1} value={simMult} onChange={(e) => setSimMult(Math.min(2, Math.max(0, Number(e.target.value))))} style={{ width: 80 }} />
              </label>
            </div>
          )}
          <div className="row">
            <button className="btn btn-primary" disabled={sending} onClick={simulate}>
              Запустить симуляцию
            </button>
            {simResult && (
              <span className="muted small">
                сгенерировано {fmtNum(simResult.accepted)} валидаций, посадок {fmtNum(simResult.boardings)}, ячеек {fmtNum(simResult.cells)}
              </span>
            )}
          </div>
        </Section>
        <Section title="Приём потоковых данных (валидации)">
          <div className="muted small">
            CSV в формате train.csv / test.csv: разделитель «;», заголовок, нужны колонки <code>tran_date_time</code>,{' '}
            <code>validation_result</code>, <code>ngpt_route</code>. Посадка — <code>validation_result = 1</code>.
          </div>
          <div className="row">
            <input
              ref={fileRef}
              type="file"
              accept=".csv,text/csv"
              onChange={(e) => {
                setFile(e.target.files?.[0] ?? null)
                setIngestError(null)
              }}
            />
            <button className="btn btn-primary" disabled={!file || sending} onClick={() => file && send(file)}>
              Отправить файл
            </button>
          </div>
          {file && (
            <div className="muted small">
              Файл: {file.name}, {fmtNum(file.size / 1024, 1)} КБ
            </div>
          )}
          <textarea
            className="csv-input"
            rows={9}
            placeholder="…или вставьте строки CSV с заголовком"
            value={text}
            onChange={(e) => setText(e.target.value)}
            spellCheck={false}
          />
          <div className="row">
            <button className="btn btn-primary" disabled={!text.trim() || sending} onClick={() => send(text)}>
              Отправить текст
            </button>
            <button className="btn btn-ghost" onClick={() => setText(SAMPLE)}>
              Вставить пример
            </button>
            <span className="grow" />
            {confirmReset ? (
              <>
                <span className="small">Удалить все принятые данные?</span>
                <button className="btn btn-warn" onClick={resetIngest}>
                  Да, сбросить
                </button>
                <button className="btn" onClick={() => setConfirmReset(false)}>
                  Отмена
                </button>
              </>
            ) : (
              <button className="btn" disabled={sending} onClick={() => setConfirmReset(true)}>
                Сбросить
              </button>
            )}
          </div>
          {sending && <Spinner label="Отправка…" />}
          {ingestError && <div className="state-box state-error">{ingestError}</div>}
          {result && (
            <div className="kpi-row">
              <Kpi label="Получено строк" value={fmtNum(result.received)} />
              <Kpi label="Принято" value={fmtNum(result.accepted)} accent="#27ae60" />
              <Kpi label="Отклонено" value={fmtNum(result.rejected)} accent={result.rejected ? '#c0392b' : undefined} />
              <Kpi label="Посадок" value={fmtNum(result.boardings)} />
              <Kpi label="Ячеек маршрут×час" value={fmtNum(result.cells)} />
            </div>
          )}
        </Section>
        </div>
      </div>
    </div>
  )
}
