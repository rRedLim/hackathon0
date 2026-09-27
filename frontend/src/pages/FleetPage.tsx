import { useMemo, useState } from 'react'
import { API, qs, type FleetCandidate, type FleetReserve, type FleetResponse } from '../api'
import { type Column, DataTable, Kpi, RouteBadge, RouteChips, Section, Spinner, StateBox } from '../components'
import { clampDate, daysBetween, fmtDate, fmtNum, fmtShare, heatColor, hourLabel, RESERVE_GRADIENT, reserveColor } from '../format'
import { useApi, useMeta } from '../hooks'

const HOURS = Array.from({ length: 24 }, (_, h) => h)

type Mode = 'reinforce' | 'reserve'

/** Час с изменением выпуска: для усиления value = +вагоны, для резерва value = −вагоны (по модулю). */
interface HourItem {
  route: number
  date: string
  hour: number
  value: number
}

const REINFORCE_METHOD =
  'Единица — вагон-час: прогноз посадок маршрута за час делится на плановый выпуск (число вагонов на линии в этот час по истории). ' +
  'Норма маршрута — 90-й перцентиль посадок на вагон-час в истории. Час становится кандидатом на усиление, если прогнозная нагрузка ' +
  'выше нормы; добавка = ⌈прогноз / норма⌉ − выпуск. Реализуемость проверяется дважды: по парку маршрута (максимум выходов ' +
  'на линии за час в истории) и по парку сети (сумма выпуска всех маршрутов в час не больше исторического максимума сети).'

const RESERVE_METHOD =
  'Резерв (экономия) — часы 7:00–22:59, где прогноз посадок на вагон ниже 50 % нормы маршрута. Оставляем ' +
  'need = max(⌈прогноз / (0,7 · норма)⌉, 2) вагонов; снимаем не больше 30 % планового выпуска часа. Не оцениваются дни с особым ' +
  'режимом маршрута и вечер 31.12 (бесплатный проезд).'

export default function FleetPage() {
  const meta = useMeta()
  const day = meta.horizons.find((h) => h.id === 'day')
  const minD = day?.from ?? '2025-11-01'
  const maxD = day?.to ?? '2025-12-31'
  const [mode, setMode] = useState<Mode>('reinforce')
  const [routes, setRoutes] = useState<number[]>(() => meta.routes.map((r) => r.route))
  const [from, setFrom] = useState(minD)
  const [to, setTo] = useState(maxD)
  const [heatMode, setHeatMode] = useState<'route' | 'date'>('route')
  const [heatMetric, setHeatMetric] = useState<'sum' | 'count'>('sum')
  const [heatRoute, setHeatRoute] = useState<number | 'all'>('all')
  const [fRoute, setFRoute] = useState<number | 'all'>('all')
  const [fDate, setFDate] = useState('')
  const [fMin, setFMin] = useState(1)

  const all = routes.length === meta.routes.length
  const url = routes.length ? `${API}/fleet${qs({ routes: all ? undefined : routes.join(','), from, to })}` : null
  const res = useApi<FleetResponse>(url)
  const data = routes.length ? res.data : undefined
  const colorOf = (r: number) => meta.routes.find((x) => x.route === r)?.color ?? '#607d8b'
  const isReserve = mode === 'reserve'
  const days = data?.period?.days ?? daysBetween(from, to)
  // плановый выпуск и часы работы известны только за весь горизонт: за неполный период доли не считаются
  const fullPeriod = data?.period?.full ?? (from === minD && to === maxD)

  const totals = useMemo(() => {
    const s = data?.summary ?? []
    const sum = (f: (r: (typeof s)[number]) => number | undefined) => s.reduce((a, r) => a + (f(r) ?? 0), 0)
    return {
      extra: sum((r) => r.extraVehicleHours),
      feasible: sum((r) => r.feasibleVehicleHours ?? r.extraVehicleHours),
      network: sum((r) => r.networkVehicleHours ?? r.feasibleVehicleHours ?? r.extraVehicleHours),
      networkFleetMax: s[0]?.networkFleetMax,
      cand: sum((r) => r.candHours),
      service: sum((r) => r.serviceHours ?? undefined),
      reserve: sum((r) => r.reserveVehicleHours),
      reserveHours: sum((r) => r.reserveHours),
      planned: sum((r) => r.plannedVehicleHours ?? undefined),
      topExtra: [...s].sort((a, b) => b.extraVehicleHours - a.extraVehicleHours)[0],
      topReserve: [...s].sort((a, b) => (b.reserveVehicleHours ?? 0) - (a.reserveVehicleHours ?? 0))[0],
    }
  }, [data])
  const balance = totals.extra - totals.reserve

  const items: HourItem[] = useMemo(
    () =>
      isReserve
        ? (data?.reserve ?? []).map((r) => ({ route: r.route, date: r.date, hour: r.hour, value: r.reserve }))
        : (data?.candidates ?? []).map((c) => ({ route: c.route, date: c.date, hour: c.hour, value: c.extra })),
    [data, isReserve],
  )

  const heat = useMemo(() => {
    const src = items.filter((c) => heatMode === 'route' || heatRoute === 'all' || c.route === heatRoute)
    const rowsMap = new Map<string, number[]>()
    for (const c of src) {
      const key = heatMode === 'route' ? String(c.route) : c.date
      let arr = rowsMap.get(key)
      if (!arr) {
        arr = new Array<number>(24).fill(0)
        rowsMap.set(key, arr)
      }
      if (c.hour >= 0 && c.hour < 24) arr[c.hour] += heatMetric === 'sum' ? c.value : 1
    }
    let keys = [...rowsMap.keys()]
    if (heatMode === 'route') {
      keys = (data?.summary ?? []).map((s) => String(s.route))
      for (const k of keys) if (!rowsMap.has(k)) rowsMap.set(k, new Array<number>(24).fill(0))
    } else keys.sort()
    const max = Math.max(0, ...[...rowsMap.values()].flat())
    const colTotals = HOURS.map((h) => keys.reduce((a, k) => a + (rowsMap.get(k)?.[h] ?? 0), 0))
    return { keys, rows: rowsMap, max, colTotals }
  }, [items, data, heatMode, heatMetric, heatRoute])
  const cellColor = isReserve ? reserveColor : heatColor

  const candRows = useMemo(
    () =>
      (data?.candidates ?? [])
        .filter((c) => (fRoute === 'all' || c.route === fRoute) && (!fDate || c.date === fDate) && c.extra >= fMin)
        .sort((a, b) => b.extra - a.extra || b.bpv / b.norm - a.bpv / a.norm),
    [data, fRoute, fDate, fMin],
  )
  const reserveRows = useMemo(
    () =>
      (data?.reserve ?? [])
        .filter((c) => (fRoute === 'all' || c.route === fRoute) && (!fDate || c.date === fDate) && c.reserve >= fMin)
        .sort((a, b) => b.reserve - a.reserve || a.bpv / a.norm - b.bpv / b.norm),
    [data, fRoute, fDate, fMin],
  )

  const baseCols = <T extends FleetCandidate | FleetReserve>(): Column<T>[] => [
    { key: 'r', title: 'Маршрут', render: (c) => <RouteBadge route={c.route} color={colorOf(c.route)} /> },
    { key: 'd', title: 'Дата', render: (c) => fmtDate(c.date) },
    { key: 'h', title: 'Час', render: (c) => `${hourLabel(c.hour)}–${hourLabel((c.hour + 1) % 24)}` },
    { key: 'p', title: 'Прогноз посадок', num: true, render: (c) => fmtNum(c.pred) },
    { key: 'n', title: 'Выпуск (план), ваг.', num: true, render: (c) => fmtNum(c.nPlan, 1) },
    { key: 'b', title: 'Посадок на вагон', num: true, render: (c) => fmtNum(c.bpv, 1) },
    { key: 'no', title: 'Норма', num: true, render: (c) => fmtNum(c.norm, 1) },
    {
      key: 'o',
      title: 'Загрузка от нормы',
      num: true,
      render: (c) => <span className={c.bpv > c.norm ? 'text-down' : 'text-reserve'}>{c.norm > 0 ? `${fmtNum((c.bpv / c.norm) * 100, 0)} %` : '—'}</span>,
    },
  ]
  const candCols: Column<FleetCandidate>[] = [
    ...baseCols<FleetCandidate>(),
    { key: 'e', title: 'Нужно добавить', num: true, render: (c) => <b>+{fmtNum(c.extra)}</b> },
    { key: 'en', title: 'В пределах парка сети', num: true, render: (c) => (c.extraNetwork === undefined ? '—' : `+${fmtNum(c.extraNetwork)}`) },
  ]
  const reserveCols: Column<FleetReserve>[] = [
    ...baseCols<FleetReserve>(),
    { key: 'need', title: 'Оставить вагонов', num: true, render: (c) => fmtNum(c.need) },
    { key: 'e', title: 'Снять вагонов', num: true, render: (c) => <b className="text-reserve">−{fmtNum(c.reserve)}</b> },
  ]

  const reserveMissing = !!data && data.reserve === undefined

  return (
    <div className="page">
      <Section
        title={isReserve ? 'Где можно сократить выпуск (резерв)' : 'Где усиливать выпуск'}
        extra={
          <>
            {res.loading && <Spinner />}
            <div className="seg-control">
              <button className={!isReserve ? 'on' : ''} onClick={() => setMode('reinforce')}>
                Усиление
              </button>
              <button className={isReserve ? 'on on-reserve' : ''} onClick={() => setMode('reserve')}>
                Резерв (экономия)
              </button>
            </div>
          </>
        }
      >
        <div className="row">
          <label className="field">
            <span>С</span>
            <input type="date" min={minD} max={maxD} value={from} onChange={(e) => { const v = clampDate(e.target.value, minD, maxD); setFrom(v); if (to < v) setTo(v) }} />
          </label>
          <label className="field">
            <span>По</span>
            <input type="date" min={minD} max={maxD} value={to} onChange={(e) => { const v = clampDate(e.target.value, minD, maxD); setTo(v); if (from > v) setFrom(v) }} />
          </label>
          <div className="field grow">
            <span>Маршруты</span>
            <RouteChips routes={meta.routes} selected={routes} onChange={setRoutes} compact />
          </div>
        </div>
        <div className="method">
          <b>Метод.</b> {isReserve ? (data?.reserveMethod ?? RESERVE_METHOD) : (data?.method ?? REINFORCE_METHOD)}
        </div>
      </Section>

      {!routes.length ? (
        <div className="state-box state-empty">Выберите хотя бы один маршрут</div>
      ) : (
        <StateBox loading={!data && res.loading} error={res.error} onRetry={res.reload}>
          {data && (
            <>
              <div className="balance-line">
                <span>
                  Баланс выпуска за {fmtDate(from)} — {fmtDate(to)}:
                </span>
                <span className="bal-plus">Усиление +{fmtNum(totals.extra)} ваг.-ч</span>
                <span className="bal-minus">Резерв −{fmtNum(totals.reserve)} ваг.-ч</span>
                <span className={`bal-total ${balance > 0 ? 'bal-plus' : 'bal-minus'}`}>
                  сальдо {balance > 0 ? '+' : balance < 0 ? '−' : ''}
                  {fmtNum(Math.abs(balance))} ваг.-ч
                </span>
                <span className="muted small">
                  {balance <= 0
                    ? 'резерва в тихие часы по вагоно-часам хватает, чтобы покрыть потребность в усилении в пики; это потребность, а не план — парк депо в данных не виден'
                    : 'усиление в пики больше, чем высвобождается в тихие часы'}
                </span>
              </div>

              {isReserve ? (
                <div className="kpi-row">
                  <Kpi label="Экономия, вагоно-часов" value={`−${fmtNum(totals.reserve)}`} sub={`${fmtNum(totals.reserveHours)} ч с недогрузкой`} accent="#2f6fb0" />
                  <Kpi label="В среднем за день" value={`−${fmtNum(totals.reserve / Math.max(1, days), 1)}`} sub={`${days} дн.`} accent="#2f6fb0" />
                  <Kpi
                    label="Доля от планового выпуска"
                    value={fullPeriod && totals.planned ? fmtShare(totals.reserve / totals.planned) : '—'}
                    sub={
                      fullPeriod && totals.planned
                        ? `плановый выпуск ${fmtNum(totals.planned)} ваг.-ч`
                        : 'считается только за весь период 01.11–31.12'
                    }
                  />
                  {totals.topReserve && (totals.topReserve.reserveVehicleHours ?? 0) > 0 && (
                    <Kpi
                      label="Больше всего резерва"
                      value={<RouteBadge route={totals.topReserve.route} color={colorOf(totals.topReserve.route)} />}
                      sub={`−${fmtNum(totals.topReserve.reserveVehicleHours)} ваг.-ч · ${totals.topReserve.reserveTopHours ?? ''}`}
                    />
                  )}
                </div>
              ) : (
                <div className="kpi-row">
                  <Kpi label="Нужно доп. вагоно-часов" value={`+${fmtNum(totals.extra)}`} sub={`${fmtDate(from)} — ${fmtDate(to)}, в среднем ${fmtNum(totals.extra / Math.max(1, days), 1)} в день`} accent="#c0392b" />
                  <Kpi
                    label="В пределах парка сети"
                    value={`+${fmtNum(totals.network)}`}
                    sub={`в пределах парка маршрутов +${fmtNum(totals.feasible)}${totals.networkFleetMax ? `; сеть выпускала одновременно не больше ${fmtNum(totals.networkFleetMax)} вагонов` : ''}`}
                  />
                  <Kpi
                    label="Часов-кандидатов"
                    value={fmtNum(totals.cand)}
                    sub={fullPeriod && totals.service ? `из ${fmtNum(totals.service)} часов работы (${fmtShare(totals.cand / totals.service)})` : `${days} дн.; доля от часов работы — только за весь период`}
                  />
                  {totals.topExtra && (
                    <Kpi
                      label="Больше всего нужно"
                      value={<RouteBadge route={totals.topExtra.route} color={colorOf(totals.topExtra.route)} />}
                      sub={`+${fmtNum(totals.topExtra.extraVehicleHours)} ваг.-ч · пики ${totals.topExtra.topHours}`}
                    />
                  )}
                </div>
              )}

              {isReserve && reserveMissing && (
                <div className="hint">Сервер не вернул расчёт резерва (поле reserve) — обновите backend.</div>
              )}

              <Section title={`Сводка по маршрутам за ${fmtDate(from)} — ${fmtDate(to)}`}>
                <div className="table-wrap">
                  {isReserve ? (
                    <table className="table">
                      <thead>
                        <tr>
                          <th>Маршрут</th>
                          <th className="num">Норма, посадок/ваг.-ч</th>
                          <th className="num">Плановый выпуск, ваг.-ч</th>
                          <th className="num">Часов с недогрузкой</th>
                          <th className="num">Резерв, ваг.-ч</th>
                          <th className="num">Доля от плана</th>
                          <th>Часы резерва чаще всего</th>
                          <th className="num">Усиление, ваг.-ч</th>
                          <th className="num">Сальдо</th>
                        </tr>
                      </thead>
                      <tbody>
                        {data.summary.map((s) => {
                          const bal = s.extraVehicleHours - (s.reserveVehicleHours ?? 0)
                          return (
                            <tr key={s.route}>
                              <td>
                                <RouteBadge route={s.route} color={colorOf(s.route)} />
                              </td>
                              <td className="num">{fmtNum(s.normBpv, 1)}</td>
                              <td className="num">{fmtNum(s.plannedVehicleHours)}</td>
                              <td className="num">{fmtNum(s.reserveHours)}</td>
                              <td className="num">
                                <b className="text-reserve">−{fmtNum(s.reserveVehicleHours ?? 0)}</b>
                              </td>
                              <td className="num">
                                <span className="share-bar">
                                  <i style={{ width: `${Math.min(100, (s.reserveShare ?? 0) * 100 * 4)}%`, background: '#2f6fb0' }} />
                                </span>
                                {fmtShare(s.reserveShare)}
                              </td>
                              <td>{s.reserveTopHours || '—'}</td>
                              <td className="num">+{fmtNum(s.extraVehicleHours)}</td>
                              <td className={`num ${bal > 0 ? 'text-down' : 'text-reserve'}`}>
                                {bal > 0 ? '+' : bal < 0 ? '−' : ''}
                                {fmtNum(Math.abs(bal))}
                              </td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                  ) : (
                    <table className="table">
                      <thead>
                        <tr>
                          <th>Маршрут</th>
                          <th className="num">Норма, посадок/ваг.-ч</th>
                          <th className="num" title="Максимум выходов (графиков) маршрута на линии за час в истории">
                            Парк маршрута, выходов
                          </th>
                          <th className="num">Часов работы</th>
                          <th className="num">Часов-кандидатов</th>
                          <th className="num">Доля</th>
                          <th className="num">Нужно, ваг.-ч</th>
                          <th className="num" title="Добавка не больше, чем маршрут уже выпускал одновременно">
                            В пределах парка маршрута
                          </th>
                          <th className="num" title="Сумма выпуска всех маршрутов в час не больше исторического максимума сети; излишек срезан с наименее перегруженных маршрутов">
                            В пределах парка сети
                          </th>
                          <th>Пиковые часы</th>
                        </tr>
                      </thead>
                      <tbody>
                        {data.summary.map((s) => (
                          <tr key={s.route}>
                            <td>
                              <RouteBadge route={s.route} color={colorOf(s.route)} />
                            </td>
                            <td className="num">{fmtNum(s.normBpv, 1)}</td>
                            <td className="num">{fmtNum(s.fleetMax)}</td>
                            <td className="num">{fmtNum(s.serviceHours)}</td>
                            <td className="num">{fmtNum(s.candHours)}</td>
                            <td className="num">
                              <span className="share-bar">
                                <i style={{ width: `${Math.min(100, (s.candShare ?? 0) * 100)}%`, background: colorOf(s.route) }} />
                              </span>
                              {fmtShare(s.candShare)}
                            </td>
                            <td className="num">
                              <b>{fmtNum(s.extraVehicleHours)}</b>
                            </td>
                            <td className="num">{fmtNum(s.feasibleVehicleHours ?? s.extraVehicleHours)}</td>
                            <td className="num">{fmtNum(s.networkVehicleHours)}</td>
                            <td>{s.topHours || '—'}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              </Section>

              <Section
                title={isReserve ? 'Тепловая карта резерва' : 'Тепловая карта усиления'}
                extra={
                  <>
                    <div className="seg-control seg-sm">
                      <button className={heatMode === 'route' ? 'on' : ''} onClick={() => setHeatMode('route')}>
                        час × маршрут
                      </button>
                      <button className={heatMode === 'date' ? 'on' : ''} onClick={() => setHeatMode('date')}>
                        час × дата
                      </button>
                    </div>
                    {heatMode === 'date' && (
                      <select value={String(heatRoute)} onChange={(e) => setHeatRoute(e.target.value === 'all' ? 'all' : Number(e.target.value))}>
                        <option value="all">все маршруты</option>
                        {data.summary.map((s) => (
                          <option key={s.route} value={s.route}>
                            маршрут {s.route}
                          </option>
                        ))}
                      </select>
                    )}
                    <select value={heatMetric} onChange={(e) => setHeatMetric(e.target.value as 'sum' | 'count')}>
                      <option value="sum">{isReserve ? 'снимаемые вагоны (сумма)' : 'доп. вагоны (сумма)'}</option>
                      <option value="count">{isReserve ? 'число часов с недогрузкой' : 'число часов-кандидатов'}</option>
                    </select>
                  </>
                }
              >
                {isReserve && (
                  <div className="seg-heat-legend">
                    <span>0</span>
                    <i style={{ background: RESERVE_GRADIENT }} />
                    <span>
                      {fmtNum(heat.max)} {heatMetric === 'sum' ? 'ваг.' : 'ч'}
                    </span>
                  </div>
                )}
                {heat.keys.length === 0 ? (
                  <div className="state-box state-empty">
                    {isReserve ? 'Часов с недогрузкой нет — сокращать выпуск не требуется' : 'Кандидатов на усиление нет — выпуск справляется с прогнозом'}
                  </div>
                ) : (
                  <div className="heatmap-wrap">
                    <table className="heatmap">
                      <thead>
                        <tr>
                          <th />
                          {HOURS.map((h) => (
                            <th key={h}>{h}</th>
                          ))}
                          <th>Σ</th>
                        </tr>
                      </thead>
                      <tbody>
                        {heat.keys.map((k) => {
                          const row = heat.rows.get(k) ?? []
                          return (
                            <tr key={k}>
                              <th>{heatMode === 'route' ? <RouteBadge route={k} color={colorOf(Number(k))} /> : fmtDate(k).slice(0, 5)}</th>
                              {HOURS.map((h) => (
                                <td
                                  key={h}
                                  style={{ background: cellColor(row[h] ?? 0, heat.max) }}
                                  title={`${heatMode === 'route' ? `Маршрут ${k}` : fmtDate(k)}, ${hourLabel(h)}: ${isReserve ? '−' : '+'}${fmtNum(row[h] ?? 0)}`}
                                  className={(row[h] ?? 0) / Math.max(1, heat.max) > 0.55 ? 'dark' : undefined}
                                >
                                  {heatMode === 'route' && row[h] ? fmtNum(row[h]) : ''}
                                </td>
                              ))}
                              <td className="heat-sum">{fmtNum(row.reduce((a, b) => a + b, 0))}</td>
                            </tr>
                          )
                        })}
                      </tbody>
                      <tfoot>
                        <tr>
                          <th>Σ</th>
                          {heat.colTotals.map((v, h) => (
                            <td key={h} className="heat-sum">
                              {v ? fmtNum(v) : ''}
                            </td>
                          ))}
                          <td className="heat-sum">{fmtNum(heat.colTotals.reduce((a, b) => a + b, 0))}</td>
                        </tr>
                      </tfoot>
                    </table>
                  </div>
                )}
              </Section>

              <Section title={isReserve ? 'Часы, где можно снять вагоны' : 'Часы-кандидаты на усиление'}>
                <div className="row">
                  <label className="field">
                    <span>Маршрут</span>
                    <select value={String(fRoute)} onChange={(e) => setFRoute(e.target.value === 'all' ? 'all' : Number(e.target.value))}>
                      <option value="all">все</option>
                      {data.summary.map((s) => (
                        <option key={s.route} value={s.route}>
                          {s.route}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="field">
                    <span>Дата</span>
                    <input type="date" min={from} max={to} value={fDate} onChange={(e) => setFDate(e.target.value)} />
                  </label>
                  {fDate && (
                    <button className="btn btn-sm btn-ghost" onClick={() => setFDate('')}>
                      все даты
                    </button>
                  )}
                  <label className="field">
                    <span>{isReserve ? 'Мин. снятие, вагонов' : 'Мин. добавка, вагонов'}</span>
                    <input type="number" min={0} max={20} value={fMin} onChange={(e) => setFMin(Math.max(0, Number(e.target.value)))} style={{ width: 80 }} />
                  </label>
                </div>
                {isReserve ? (
                  <DataTable rows={reserveRows} columns={reserveCols} pageSize={50} maxHeight={560} empty="Нет часов, подходящих под фильтр" />
                ) : (
                  <DataTable rows={candRows} columns={candCols} pageSize={50} maxHeight={560} empty="Нет часов, подходящих под фильтр" />
                )}
              </Section>
            </>
          )}
        </StateBox>
      )}
    </div>
  )
}
