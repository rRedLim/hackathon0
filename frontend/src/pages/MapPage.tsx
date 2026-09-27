import { GeoJSONSource, Map as MapLibre, Marker, NavigationControl, Popup, setWorkerUrl, type StyleSpecification } from 'maplibre-gl'
import maplibreWorkerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url'
import type { Feature, FeatureCollection, LineString } from 'geojson'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Area, AreaChart, Bar, BarChart, Cell, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { API, apiRequest, errorMessage, isAbort, qs, type FleetResponse, type MapLiveResponse, type MapResponse, type MapStop } from '../api'
import { RouteBadge, RouteChips, Spinner } from '../components'
import { clampDate, fmtDate, fmtNum, hourLabel } from '../format'
import { useApi, useMeta } from '../hooks'

const MIN_DATE = '2025-01-01'
const MAX_DATE = '2026-12-31'

// MapLibre 6 грузит web-worker относительно своего модуля — при сборке Vite указываем URL явно.
setWorkerUrl(maplibreWorkerUrl)

type Basemap = 'osm' | 'esri'

// CARTO light_all без API-ключа отдаёт заглушку «API KEY REQUIRED», поэтому используем бесключевые подложки.
const BASEMAPS: Record<Basemap, { label: string; tiles: string[]; attribution: string; saturation: number; opacity: number }> = {
  osm: {
    label: 'OSM',
    tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
    attribution: '© участники OpenStreetMap',
    saturation: -0.75,
    opacity: 0.85,
  },
  esri: {
    label: 'Светлая',
    tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}'],
    attribution: 'Esri, HERE, Garmin, © OpenStreetMap contributors',
    saturation: 0,
    opacity: 1,
  },
}

const STYLE: StyleSpecification = {
  version: 8,
  sources: Object.fromEntries(
    (Object.keys(BASEMAPS) as Basemap[]).map((k) => [
      k,
      { type: 'raster', tiles: BASEMAPS[k].tiles, tileSize: 256, attribution: BASEMAPS[k].attribution, maxzoom: 19 },
    ]),
  ),
  layers: [
    { id: 'bg', type: 'background', paint: { 'background-color': '#f2f3f0' } },
    ...(Object.keys(BASEMAPS) as Basemap[]).map((k, i) => ({
      id: `base-${k}`,
      type: 'raster' as const,
      source: k,
      layout: { visibility: i === 0 ? ('visible' as const) : ('none' as const) },
      paint: { 'raster-saturation': BASEMAPS[k].saturation, 'raster-opacity': BASEMAPS[k].opacity },
    })),
  ],
}

const FLEET_FROM = '2025-11-01'
const FLEET_TO = '2025-12-31'

const EMPTY_FC: FeatureCollection = { type: 'FeatureCollection', features: [] }

function modeOf(date: string): { label: string; cls: string } {
  if (date <= '2025-10-31') return { label: 'факт', cls: 'mode-history' }
  if (date <= '2025-12-31') return { label: 'прогноз', cls: 'mode-forecast' }
  return { label: 'сценарий 2026', cls: 'mode-year' }
}

const stopKey = (s: { route: number; direction: number; seq: number }) => `${s.route}:${s.direction}:${s.seq}`

function StopPopup({ stop, hour, color, live }: { stop: MapStop; hour: number; color: string; live?: boolean }) {
  const data = stop.hourly.map((v, h) => ({ h, v }))
  return (
    <div className="stop-popup">
      <div className="stop-popup-title">{stop.name}</div>
      <div className="stop-popup-sub">
        <RouteBadge route={stop.route} color={color} /> направление {stop.direction === 0 ? 'прямое' : 'обратное'} · №{stop.seq}
      </div>
      <div className="stop-popup-vals">
        <div>
          <span className="muted">в {hourLabel(hour)}</span>
          <b>{fmtNum(stop.hourly[hour] ?? 0, 1)}</b>
        </div>
        <div>
          <span className="muted">за сутки</span>
          <b>{fmtNum(stop.total)}</b>
        </div>
        <div>
          <span className="muted">доля маршрута</span>
          <b>{fmtNum(stop.weight * 100, 2)} %</b>
        </div>
      </div>
      <div style={{ width: 256, height: 74 }}>
        <BarChart width={256} height={74} data={data} margin={{ top: 4, right: 0, left: 0, bottom: 0 }}>
          <XAxis dataKey="h" tick={{ fontSize: 9 }} interval={3} tickLine={false} height={14} />
          <Bar dataKey="v" isAnimationActive={false}>
            {data.map((d) => (
              <Cell key={d.h} fill={d.h === hour ? '#c0392b' : color} fillOpacity={d.h === hour ? 1 : 0.55} />
            ))}
          </Bar>
        </BarChart>
      </div>
      <div className="muted small">
        {live ? 'факт из потока (доля остановки от посадок маршрута)' : 'посадки по часам'}, {hourLabel(hour)} выделен
      </div>
    </div>
  )
}

export default function MapPage() {
  const meta = useMeta()
  const [date, setDate] = useState('2025-11-10')
  const [routes, setRoutes] = useState<number[]>(() => meta.routes.map((r) => r.route))
  const [hour, setHour] = useState(8)
  const [playing, setPlaying] = useState(false)
  const [hoverKey, setHoverKey] = useState<string | null>(null)
  const [pinnedKey, setPinnedKey] = useState<string | null>(null)
  const [ready, setReady] = useState(false)
  const [basemap, setBasemap] = useState<Basemap>('osm')
  const [showFleet, setShowFleet] = useState(true)
  const markersRef = useRef<Marker[]>([])
  const [liveMode, setLiveMode] = useState(false)
  const [live, setLive] = useState<{ data: MapLiveResponse; at: Date } | null>(null)
  const [liveError, setLiveError] = useState<string | null>(null)
  const liveInitRef = useRef<string | null>(null)

  const containerRef = useRef<HTMLDivElement>(null)
  const mapRef = useRef<MapLibre | null>(null)
  const popupRef = useRef<Popup | null>(null)
  const fittedRef = useRef(false)
  const popupNode = useMemo(() => document.createElement('div'), [])

  const allSelected = routes.length === meta.routes.length
  const url = routes.length ? `${API}/map${qs({ date, routes: allSelected ? undefined : routes.join(',') })}` : null
  const res = useApi<MapResponse>(url)
  const fleetPeriod = date >= FLEET_FROM && date <= FLEET_TO
  const fleetRes = useApi<FleetResponse>(showFleet && fleetPeriod ? `${API}/fleet${qs({ from: date, to: date })}` : null)
  const fleetData = showFleet && fleetPeriod ? fleetRes.data : undefined
  const data = routes.length ? res.data : undefined

  const colorOf = useCallback(
    (route: number) =>
      data?.routes.find((r) => r.route === route)?.color ?? meta.routes.find((r) => r.route === route)?.color ?? '#546e7a',
    [data, meta.routes],
  )

  // --- инициализация карты
  useEffect(() => {
    if (!containerRef.current) return
    const map = new MapLibre({
      container: containerRef.current,
      style: STYLE,
      center: [37.62, 55.75],
      zoom: 10.5,
      attributionControl: { compact: true },
    })
    mapRef.current = map
    map.addControl(new NavigationControl({ showCompass: false }), 'top-left')
    map.on('load', () => {
      map.addSource('lines', { type: 'geojson', data: EMPTY_FC })
      map.addSource('stops', { type: 'geojson', data: EMPTY_FC })
      map.addLayer({
        id: 'lines-casing',
        type: 'line',
        source: 'lines',
        layout: { 'line-join': 'round', 'line-cap': 'round' },
        paint: { 'line-color': '#ffffff', 'line-width': 6.5, 'line-opacity': 0.85 },
      })
      map.addLayer({
        id: 'lines',
        type: 'line',
        source: 'lines',
        layout: { 'line-join': 'round', 'line-cap': 'round' },
        paint: { 'line-color': ['get', 'color'], 'line-width': 3.4, 'line-opacity': 0.8 },
      })
      // усиление выпуска: пульсирующий ореол + пунктир поверх линий маршрутов-кандидатов
      map.addLayer({
        id: 'lines-reinforce-halo',
        type: 'line',
        source: 'lines',
        filter: ['in', ['get', 'route'], ['literal', []]],
        layout: { 'line-join': 'round', 'line-cap': 'round' },
        paint: {
          'line-color': '#e31a1c',
          'line-width': 13,
          'line-blur': 3,
          'line-opacity': 0.35,
          'line-opacity-transition': { duration: 600, delay: 0 },
        },
      })
      map.addLayer({
        id: 'lines-reinforce',
        type: 'line',
        source: 'lines',
        filter: ['in', ['get', 'route'], ['literal', []]],
        layout: { 'line-join': 'round', 'line-cap': 'butt' },
        paint: { 'line-color': '#b00020', 'line-width': 3, 'line-dasharray': [2, 1.5] },
      })
      map.addLayer({
        id: 'stops',
        type: 'circle',
        source: 'stops',
        layout: { 'circle-sort-key': ['get', 'norm'] },
        paint: {
          'circle-radius': ['case', ['boolean', ['get', 'nodata'], false], 3, ['interpolate', ['linear'], ['get', 'norm'], 0, 2.5, 0.25, 6, 1, 18]],
          'circle-color': [
            'case',
            ['boolean', ['get', 'nodata'], false],
            '#c3c8cf',
            [
            'interpolate',
            ['linear'],
            ['get', 'norm'],
            0,
            '#fff5cc',
            0.2,
            '#fed976',
            0.45,
            '#fd8d3c',
            0.7,
            '#e31a1c',
            1,
            '#800026',
            ],
          ],
          'circle-opacity': ['interpolate', ['linear'], ['get', 'norm'], 0, 0.55, 1, 0.92],
          'circle-stroke-color': ['get', 'color'],
          'circle-stroke-width': 1.5,
        },
      })
      map.on('mousemove', 'stops', (e) => {
        map.getCanvas().style.cursor = 'pointer'
        const k = e.features?.[0]?.properties?.key
        if (typeof k === 'string') setHoverKey(k)
      })
      map.on('mouseleave', 'stops', () => {
        map.getCanvas().style.cursor = ''
        setHoverKey(null)
      })
      map.on('click', 'stops', (e) => {
        const k = e.features?.[0]?.properties?.key
        if (typeof k === 'string') setPinnedKey(k)
      })
      setReady(true)
    })
    return () => {
      popupRef.current?.remove()
      popupRef.current = null
      map.remove()
      mapRef.current = null
    }
  }, [])

  // --- линии маршрутов (меняются только с данными)
  useEffect(() => {
    const map = mapRef.current
    if (!ready || !map) return
    const features: Feature[] = []
    for (const r of data?.routes ?? []) {
      for (const l of r.lines ?? []) {
        if (l.coords?.length >= 2)
          features.push({
            type: 'Feature',
            properties: { route: r.route, color: r.color, direction: l.direction },
            geometry: { type: 'LineString', coordinates: l.coords },
          })
      }
    }
    map.getSource<GeoJSONSource>('lines')?.setData({ type: 'FeatureCollection', features })
    if (!fittedRef.current && features.length) {
      let minX = 180
      let minY = 90
      let maxX = -180
      let maxY = -90
      for (const f of features) {
        for (const [x, y] of (f.geometry as LineString).coordinates) {
          minX = Math.min(minX, x)
          minY = Math.min(minY, y)
          maxX = Math.max(maxX, x)
          maxY = Math.max(maxY, y)
        }
      }
      map.fitBounds(
        [
          [minX, minY],
          [maxX, maxY],
        ],
        { padding: 40, duration: 0, maxZoom: 13 },
      )
      fittedRef.current = true
    }
  }, [data, ready])

  // --- режим «Факт из потока (live)»: опрос /map/live каждые 5 с, пауза при скрытой вкладке
  useEffect(() => {
    if (!liveMode) return
    let stopped = false
    let ctrl: AbortController | null = null
    const tick = () => {
      if (document.hidden) return
      ctrl?.abort()
      ctrl = new AbortController()
      apiRequest<MapLiveResponse>(`${API}/map/live${qs({ date })}`, { signal: ctrl.signal })
        .then((d) => {
          if (stopped) return
          setLive({ data: d, at: new Date() })
          setLiveError(null)
          if (liveInitRef.current !== date && d.lastHour != null) {
            liveInitRef.current = date
            setHour(d.lastHour)
          }
        })
        .catch((e: unknown) => {
          if (stopped || isAbort(e)) return
          setLiveError(errorMessage(e))
        })
    }
    tick()
    const id = setInterval(tick, 5000)
    const onVisible = () => {
      if (!document.hidden) tick()
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      stopped = true
      clearInterval(id)
      ctrl?.abort()
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [liveMode, date])

  const liveData = liveMode && live && live.data.date === date ? live.data : undefined
  const liveLast = liveData?.available ? liveData.lastHour : null
  const liveNoData = liveMode && liveData !== undefined && (liveLast === null || hour > liveLast)
  const liveByRoute = useMemo(() => {
    const m = new Map<number, number[]>()
    for (const r of liveData?.routes ?? []) m.set(r.route, r.hourly ?? [])
    return m
  }, [liveData])
  const liveAlerts = useMemo(() => (liveData?.alerts ?? []).filter((a) => routes.includes(a.route)), [liveData, routes])
  /** значение остановки в режиме live: факт маршрута за час × доля остановки */
  const liveStopHourly = useCallback(
    (s: MapStop) => {
      const arr = liveByRoute.get(s.route) ?? []
      return Array.from({ length: 24 }, (_, h) => (liveLast !== null && h <= liveLast ? (arr[h] ?? 0) * s.weight : 0))
    },
    [liveByRoute, liveLast],
  )

  // --- остановки: значения текущего часа
  const stopsByKey = useMemo(() => {
    const m = new Map<string, MapStop>()
    for (const s of data?.stops ?? []) m.set(stopKey(s), s)
    return m
  }, [data])

  useEffect(() => {
    const map = mapRef.current
    if (!ready || !map) return
    const max = data?.maxStopHour && data.maxStopHour > 0 ? data.maxStopHour : 1
    const features: Feature[] = (data?.stops ?? [])
      .filter((s) => Number.isFinite(s.lat) && Number.isFinite(s.lon))
      .map((s) => {
        const v = liveMode ? (liveByRoute.get(s.route)?.[hour] ?? 0) * s.weight : (s.hourly?.[hour] ?? 0)
        return {
          type: 'Feature',
          properties: { key: stopKey(s), color: colorOf(s.route), norm: Math.min(1, Math.max(0, v / max)), nodata: liveMode && (liveNoData || !liveData) },
          geometry: { type: 'Point', coordinates: [s.lon, s.lat] },
        }
      })
    map.getSource<GeoJSONSource>('stops')?.setData({ type: 'FeatureCollection', features })
  }, [data, hour, ready, colorOf, liveMode, liveByRoute, liveNoData, liveData])

  // --- всплывающая карточка остановки
  const activeKey = pinnedKey ?? hoverKey
  const activeStop = activeKey ? stopsByKey.get(activeKey) : undefined
  useEffect(() => {
    const map = mapRef.current
    if (!map || !ready) return
    if (!activeStop) {
      popupRef.current?.remove()
      return
    }
    if (!popupRef.current) {
      popupRef.current = new Popup({ closeButton: true, closeOnClick: false, maxWidth: '300px', offset: 14 }).setDOMContent(popupNode)
      popupRef.current.on('close', () => setPinnedKey(null))
    }
    popupRef.current.setLngLat([activeStop.lon, activeStop.lat])
    if (!popupRef.current.isOpen()) popupRef.current.addTo(map)
  }, [activeStop, ready, popupNode])

  // --- усиление выпуска в текущий час: подсветка линий и значки «+N ваг.»
  const reinforceNow = useMemo(() => {
    const out = new Map<number, { extra: number; pred: number; nPlan: number; norm: number }>()
    for (const c of fleetData?.candidates ?? []) {
      if (c.date !== date || c.hour !== hour || !routes.includes(c.route)) continue
      const prev = out.get(c.route)
      out.set(c.route, { extra: (prev?.extra ?? 0) + c.extra, pred: c.pred, nPlan: c.nPlan, norm: c.norm })
    }
    return out
  }, [fleetData, date, hour, routes])
  const reserveNow = useMemo(() => {
    const out = new Map<number, { reserve: number; pred: number; nPlan: number; need: number }>()
    for (const c of fleetData?.reserve ?? []) {
      if (c.date !== date || c.hour !== hour || !routes.includes(c.route)) continue
      out.set(c.route, { reserve: (out.get(c.route)?.reserve ?? 0) + c.reserve, pred: c.pred, nPlan: c.nPlan, need: c.need })
    }
    return out
  }, [fleetData, date, hour, routes])
  const reserveDay = useMemo(() => {
    const out = new Map<number, number>()
    for (const c of fleetData?.reserve ?? []) if (c.date === date && routes.includes(c.route)) out.set(c.route, (out.get(c.route) ?? 0) + c.reserve)
    return out
  }, [fleetData, date, routes])
  const reinforceDay = useMemo(() => {
    const out = new Map<number, number>()
    for (const c of fleetData?.candidates ?? []) if (c.date === date && routes.includes(c.route)) out.set(c.route, (out.get(c.route) ?? 0) + c.extra)
    return out
  }, [fleetData, date, routes])

  useEffect(() => {
    const map = mapRef.current
    if (!ready || !map) return
    const list = [...reinforceNow.keys()]
    const filter: ['in', ['get', string], ['literal', number[]]] = ['in', ['get', 'route'], ['literal', list]]
    map.setFilter('lines-reinforce-halo', filter)
    map.setFilter('lines-reinforce', filter)
    for (const m of markersRef.current) m.remove()
    markersRef.current = []
    for (const [route, v] of reinforceNow) {
      const r = data?.routes.find((x) => x.route === route)
      const line = r?.lines?.find((l) => l.direction === 0 && l.coords?.length >= 2) ?? r?.lines?.find((l) => l.coords?.length >= 2)
      if (!line) continue
      const el = document.createElement('div')
      el.className = 'reinforce-marker'
      el.style.borderColor = r?.color ?? '#b00020'
      el.title = `Маршрут ${route}: прогноз ${Math.round(v.pred)} посадок в ${hourLabel(hour)}, выпуск ${v.nPlan} ваг., норма ${v.norm} на вагон`
      el.innerHTML = `<b style="background:${r?.color ?? '#b00020'}">${route}</b>+${v.extra} ваг.`
      markersRef.current.push(new Marker({ element: el }).setLngLat(line.coords[Math.floor(line.coords.length / 2)]).addTo(map))
    }
    // резерв — второстепенно: приглушённый значок «−N ваг.» на трети линии
    for (const [route, v] of reserveNow) {
      if (reinforceNow.has(route)) continue
      const r = data?.routes.find((x) => x.route === route)
      const line = r?.lines?.find((l) => l.direction === 0 && l.coords?.length >= 2) ?? r?.lines?.find((l) => l.coords?.length >= 2)
      if (!line) continue
      const el = document.createElement('div')
      el.className = 'reserve-marker'
      el.title = `Маршрут ${route}: прогноз ${Math.round(v.pred)} посадок в ${hourLabel(hour)}, выпуск ${v.nPlan} ваг. — достаточно ${v.need}, можно снять ${v.reserve}`
      el.innerHTML = `<b style="background:${r?.color ?? '#5b86b8'}">${route}</b>−${v.reserve} ваг.`
      markersRef.current.push(new Marker({ element: el }).setLngLat(line.coords[Math.floor(line.coords.length / 3)]).addTo(map))
    }
  }, [reinforceNow, reserveNow, data, ready, hour])

  useEffect(() => () => markersRef.current.forEach((m) => m.remove()), [])

  // пульсация ореола
  const pulsing = reinforceNow.size > 0
  useEffect(() => {
    const map = mapRef.current
    if (!ready || !map || !pulsing) return
    let on = false
    const id = setInterval(() => {
      on = !on
      map.setPaintProperty('lines-reinforce-halo', 'line-opacity', on ? 0.6 : 0.2)
    }, 650)
    return () => clearInterval(id)
  }, [pulsing, ready])

  // --- переключение подложки
  useEffect(() => {
    const map = mapRef.current
    if (!ready || !map) return
    for (const k of Object.keys(BASEMAPS) as Basemap[])
      map.setLayoutProperty(`base-${k}`, 'visibility', k === basemap ? 'visible' : 'none')
  }, [basemap, ready])

  // --- анимация по часам
  useEffect(() => {
    if (!playing) return
    const id = setInterval(() => setHour((h) => (h + 1) % 24), 700)
    return () => clearInterval(id)
  }, [playing])

  const mode = modeOf(date)
  const routesNoGeo = (data?.routes ?? []).filter((r) => !r.lines?.some((l) => l.coords?.length >= 2))
  const networkHourly = useMemo(() => {
    const arr = Array.from({ length: 24 }, (_, h) => ({ h, v: 0 }))
    for (const r of data?.routes ?? []) r.hourly?.forEach((v, h) => { if (arr[h]) arr[h].v += v })
    return arr
  }, [data])
  const maxRouteHour = Math.max(1, ...(data?.routes ?? []).flatMap((r) => r.hourly ?? [0]))
  const dayTotal = (data?.routes ?? []).reduce((a, r) => a + (r.total ?? 0), 0)
  const sortedRoutes = [...(data?.routes ?? [])].sort((a, b) => (b.hourly?.[hour] ?? 0) - (a.hourly?.[hour] ?? 0))
  const topStops = useMemo(
    () => [...(data?.stops ?? [])].sort((a, b) => (b.hourly?.[hour] ?? 0) - (a.hourly?.[hour] ?? 0)).slice(0, 5),
    [data, hour],
  )

  return (
    <div className="map-page">
      <div className="toolbar">
        <label className="field">
          <span>Дата</span>
          <input
            type="date"
            min={MIN_DATE}
            max={MAX_DATE}
            value={date}
            onChange={(e) => e.target.value && setDate(clampDate(e.target.value, MIN_DATE, MAX_DATE))}
          />
        </label>
        <span className={`mode-badge ${mode.cls}`}>{mode.label}</span>
        <div className="quick-dates">
          <button className="btn btn-sm btn-ghost" onClick={() => setDate('2025-10-15')}>
            факт 15.10.2025
          </button>
          <button className="btn btn-sm btn-ghost" onClick={() => setDate('2025-11-10')}>
            прогноз 10.11.2025
          </button>
          <button className="btn btn-sm btn-ghost" onClick={() => setDate('2026-05-20')}>
            сценарий 20.05.2026
          </button>
        </div>
        <div className="field grow">
          <span>Маршруты</span>
          <RouteChips routes={meta.routes} selected={routes} onChange={setRoutes} compact />
        </div>
      </div>
      <div className="toolbar toolbar-hours">
        <button className={`btn ${playing ? 'btn-warn' : 'btn-primary'} play-btn`} onClick={() => setPlaying((p) => !p)} disabled={!data}>
          {playing ? '❚❚ Пауза' : '▶ Воспроизвести'}
        </button>
        <div className="hour-now">{hourLabel(hour)}</div>
        <div className="hour-slider">
          <input type="range" min={0} max={23} step={1} value={hour} onChange={(e) => setHour(Number(e.target.value))} aria-label="Час суток" />
          <div className="hour-ticks">
            {Array.from({ length: 24 }, (_, h) => (
              <span key={h} className={h === hour ? 'on' : ''} onClick={() => setHour(h)}>
                {h % 3 === 0 ? h : '·'}
              </span>
            ))}
          </div>
        </div>
        <div className="seg-control seg-sm live-switch">
          <button className={!liveMode ? 'on' : ''} onClick={() => setLiveMode(false)}>
            Прогноз
          </button>
          <button
            className={liveMode ? 'on on-live' : ''}
            onClick={() => {
              liveInitRef.current = null
              setPlaying(false)
              setLiveMode(true)
            }}
          >
            Факт из потока (live)
          </button>
        </div>
        <label className="check-inline fleet-toggle" title="Усиление — прогноз посадок на вагон выше нормы; резерв — ниже 50 % нормы">
          <input type="checkbox" checked={showFleet} onChange={(e) => setShowFleet(e.target.checked)} /> Показать усиление выпуска
        </label>
      </div>
      <div className="map-body">
        <div className="map-wrap">
          <div ref={containerRef} className="map" />
          {res.loading && (
            <div className="map-loading">
              <Spinner label="Загрузка данных карты…" />
            </div>
          )}
          <div className="basemap-switch seg-control seg-sm">
            {(Object.keys(BASEMAPS) as Basemap[]).map((k) => (
              <button key={k} className={basemap === k ? 'on' : ''} onClick={() => setBasemap(k)}>
                {BASEMAPS[k].label}
              </button>
            ))}
          </div>
          {!routes.length && <div className="map-hint">Выберите хотя бы один маршрут</div>}
          {liveMode && liveData?.available && (
            <div className="live-badge">
              <span className="live-dot" /> LIVE · обновлено {live?.at.toLocaleTimeString('ru-RU')} · данные до {liveLast !== null ? `${String(liveLast + 1).padStart(2, '0')}:00` : '—'}
              {liveNoData && <span className="live-nodata"> · в {hourLabel(hour)} данных ещё нет</span>}
            </div>
          )}
          {liveMode && liveData && !liveData.available && (
            <div className="map-hint live-empty">
              <div>
                Нет принятых данных за {fmtDate(date)}: загрузите валидации или запустите симуляцию на вкладке «Мониторинг».
              </div>
              <a className="btn btn-primary" href="#/monitoring">
                Перейти в «Мониторинг»
              </a>
            </div>
          )}
          {liveMode && !liveData && !liveError && (
            <div className="map-loading">
              <Spinner label="Подключение к потоку…" />
            </div>
          )}
          {liveMode && liveError && <div className="map-hint state-error">Поток недоступен: {liveError}</div>}
          <div className="map-legend">
            <div className="legend-title">
              {liveMode ? 'Факт посадок' : 'Посадки'} на остановке в {hourLabel(hour)}
            </div>
            <div className="legend-scale">
              {[0.05, 0.25, 0.5, 0.75, 1].map((n) => (
                <div key={n} className="legend-item">
                  <span
                    className="legend-dot"
                    style={{
                      width: 2 * (n <= 0.25 ? 2.5 + (n / 0.25) * 3.5 : 6 + ((n - 0.25) / 0.75) * 12),
                      height: 2 * (n <= 0.25 ? 2.5 + (n / 0.25) * 3.5 : 6 + ((n - 0.25) / 0.75) * 12),
                      background: n < 0.2 ? '#fff5cc' : n < 0.45 ? '#fed976' : n < 0.7 ? '#fd8d3c' : n < 1 ? '#e31a1c' : '#800026',
                    }}
                  />
                  <span>{fmtNum((data?.maxStopHour ?? 0) * n)}</span>
                </div>
              ))}
            </div>
            <div className="legend-note">
              обводка — цвет маршрута; шкала от максимума прогноза дня
              {liveMode && (
                <>
                  <br />
                  <span className="legend-dot legend-dot-inline" style={{ width: 8, height: 8, background: '#c3c8cf' }} /> серые — нет данных за час
                </>
              )}
            </div>
          </div>
        </div>
        <aside className="side">
          <div className="side-block">
            <div className="side-date">
              {fmtDate(date)} <span className={`mode-badge ${mode.cls}`}>{data?.mode === 'history' ? 'факт' : data?.mode === 'year' ? 'сценарий 2026' : data?.mode === 'forecast' ? 'прогноз' : mode.label}</span>
            </div>
            <div className="side-facts">
              <span>
                Тип дня: <b>{data?.dayType?.label ?? '—'}</b>
              </span>
              <span>
                Осадки: <b>{data?.weather?.precipMm != null ? `${fmtNum(data.weather.precipMm, 1)} мм` : '—'}</b>
              </span>
              <span>
                Температура: <b>{data?.weather?.tempC != null ? `${fmtNum(data.weather.tempC, 1)} °C` : '—'}</b>
              </span>
            </div>
            <div className="side-total">
              Сеть за сутки: <b>{fmtNum(dayTotal)}</b> посадок · в {hourLabel(hour)}: <b>{fmtNum(networkHourly[hour]?.v ?? 0)}</b>
            </div>
          </div>
          {liveMode && liveData?.available && (
            <div className="side-block">
              <div className="side-title">
                Факт и прогноз в {hourLabel(hour)} {liveNoData && <span className="muted small">— данных ещё нет</span>}
              </div>
              <table className="live-table">
                <thead>
                  <tr>
                    <th />
                    <th className="num">факт</th>
                    <th className="num">прогноз</th>
                    <th className="num">Δ</th>
                  </tr>
                </thead>
                <tbody>
                  {(data?.routes ?? []).map((r) => {
                    const act = liveByRoute.get(r.route)?.[hour] ?? 0
                    const fc = r.hourly?.[hour] ?? 0
                    const d = !liveNoData && fc > 0 ? (act / fc - 1) * 100 : null
                    const al = liveAlerts.filter((a) => a.route === r.route)
                    const alNow = al.some((a) => hour >= a.fromHour && hour <= a.toHour)
                    return (
                      <tr key={r.route} className={alNow ? 'live-alert-row' : undefined}>
                        <td>
                          <RouteBadge route={r.route} color={r.color} />
                          {al.length > 0 && (
                            <span className="live-warn" title={al.map((a) => a.message).join('\n')}>
                              ⚠
                            </span>
                          )}
                        </td>
                        <td className="num">{liveNoData ? '—' : fmtNum(act)}</td>
                        <td className="num muted">{fmtNum(fc)}</td>
                        <td className={`num ${d === null ? '' : Math.abs(d) >= 25 ? (d < 0 ? 'text-down' : 'text-warn') : ''}`}>
                          {d === null ? '—' : `${d > 0 ? '+' : ''}${fmtNum(d, 0)} %`}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
              {liveAlerts.length > 0 && (
                <ul className="alerts">
                  {liveAlerts.map((a, i) => (
                    <li key={i} className={`alert alert-${a.kind === 'drop' ? 'drop' : 'surge'}`}>
                      <div className="alert-top">
                        <RouteBadge route={a.route} color={colorOf(a.route)} />
                        <span className="alert-kind">{a.kind === 'drop' ? '▼ провал' : '▲ всплеск'}</span>
                        <span className="alert-hours">
                          {hourLabel(a.fromHour)}–{hourLabel((a.toHour + 1) % 24)}
                        </span>
                      </div>
                      <div className="alert-msg">{a.message}</div>
                    </li>
                  ))}
                </ul>
              )}
              <div className="muted small">
                Принято записей: {fmtNum(liveData.receivedTotal)}. Значения остановок — факт маршрута × доля остановки.
              </div>
            </div>
          )}
          {showFleet && (
            <div className="side-block">
              <div className="side-title">Усиление в {hourLabel(hour)}</div>
              {!fleetPeriod ? (
                <div className="muted small">Расчёт усиления выпуска есть только для прогнозного периода (ноябрь–декабрь 2025).</div>
              ) : fleetRes.loading && !fleetRes.data ? (
                <Spinner />
              ) : reinforceNow.size ? (
                <div className="reinforce-now">
                  {[...reinforceNow.entries()]
                    .sort((a, b) => b[1].extra - a[1].extra)
                    .map(([route, v]) => (
                      <span key={route} className="reinforce-now-item" title={`прогноз ${fmtNum(v.pred)} посадок, выпуск ${fmtNum(v.nPlan, 1)} ваг.`}>
                        <RouteBadge route={route} color={colorOf(route)} /> +{v.extra} ваг.
                      </span>
                    ))}
                </div>
              ) : (
                <div className="muted small">В этот час выпуск справляется — усиление не требуется.</div>
              )}
              {fleetPeriod && reinforceDay.size > 0 && (
                <div className="muted small">
                  За сутки: {[...reinforceDay.entries()].sort((a, b) => b[1] - a[1]).map(([r, v]) => `${r}: +${v}`).join(', ')} ваг.-ч
                </div>
              )}
              {fleetPeriod && reserveNow.size > 0 && (
                <div className="reserve-now">
                  <span className="muted small">Резерв в {hourLabel(hour)}:</span>
                  {[...reserveNow.entries()]
                    .sort((a, b) => b[1].reserve - a[1].reserve)
                    .map(([route, v]) => (
                      <span key={route} className="reserve-chip" title={`выпуск ${fmtNum(v.nPlan, 1)} ваг., достаточно ${v.need}`}>
                        {route}: −{v.reserve}
                      </span>
                    ))}
                </div>
              )}
              {fleetPeriod && reserveDay.size > 0 && (
                <div className="muted small">
                  Резерв за сутки: −{fmtNum([...reserveDay.values()].reduce((a, b) => a + b, 0))} ваг.-ч; сальдо{' '}
                  {(() => {
                    const bal = [...reinforceDay.values()].reduce((a, b) => a + b, 0) - [...reserveDay.values()].reduce((a, b) => a + b, 0)
                    return `${bal > 0 ? '+' : bal < 0 ? '−' : ''}${fmtNum(Math.abs(bal))}`
                  })()}{' '}
                  ваг.-ч
                </div>
              )}
            </div>
          )}
          <div className="side-block">
            <div className="side-title">Динамика сети по часам</div>
            <div style={{ height: 110 }}>
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart
                  data={networkHourly}
                  margin={{ top: 6, right: 6, left: 0, bottom: 0 }}
                  onClick={(s) => {
                    const idx = Number(s?.activeTooltipIndex)
                    if (Number.isFinite(idx)) setHour(idx)
                  }}
                >
                  <XAxis dataKey="h" tick={{ fontSize: 10 }} interval={2} tickLine={false} />
                  <YAxis hide />
                  <Tooltip formatter={(v) => [fmtNum(Number(v)), 'посадки']} labelFormatter={(h) => hourLabel(Number(h))} />
                  <Area type="monotone" dataKey="v" stroke="#1f5fa8" fill="#1f5fa8" fillOpacity={0.18} isAnimationActive={false} />
                  <ReferenceLine x={hour} stroke="#c0392b" strokeWidth={2} />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          </div>
          <div className="side-block">
            <div className="side-title">
              Маршруты: в {hourLabel(hour)} / за сутки
            </div>
            {!data && !res.loading && <div className="muted">Нет данных</div>}
            <div className="route-bars">
              {sortedRoutes.map((r) => {
                const v = r.hourly?.[hour] ?? 0
                return (
                  <div key={r.route} className="route-bar-row" title={r.name}>
                    <RouteBadge route={r.route} color={r.color} />
                    <div className="route-bar-track">
                      <div className="route-bar-fill" style={{ width: `${(v / maxRouteHour) * 100}%`, background: r.color }} />
                    </div>
                    <span className="num route-bar-val">{fmtNum(v)}</span>
                    <span className="num muted route-bar-total">{fmtNum(r.total)}</span>
                  </div>
                )
              })}
            </div>
          </div>
          {topStops.length > 0 && (
            <div className="side-block">
              <div className="side-title">Самые загруженные остановки в {hourLabel(hour)}</div>
              <ol className="top-stops">
                {topStops.map((s) => (
                  <li key={stopKey(s)} onClick={() => setPinnedKey(stopKey(s))}>
                    <RouteBadge route={s.route} color={colorOf(s.route)} /> <span className="grow">{s.name}</span>
                    <b className="num">{fmtNum(s.hourly?.[hour] ?? 0)}</b>
                  </li>
                ))}
              </ol>
            </div>
          )}
          {routesNoGeo.length > 0 && (
            <div className="side-block">
              <div className="side-title">Без геометрии на карте</div>
              <div className="no-geo">
                {routesNoGeo.map((r) => (
                  <RouteBadge key={r.route} route={r.route} color={r.color} />
                ))}
              </div>
              <div className="muted small">нет координат остановок в справочнике — только прогноз по маршруту</div>
            </div>
          )}
        </aside>
      </div>
      {activeStop &&
        createPortal(
          liveMode ? (
            <StopPopup
              stop={{ ...activeStop, hourly: liveStopHourly(activeStop), total: liveStopHourly(activeStop).reduce((a, b) => a + b, 0) }}
              hour={hour}
              color={colorOf(activeStop.route)}
              live
            />
          ) : (
            <StopPopup stop={activeStop} hour={hour} color={colorOf(activeStop.route)} />
          ),
          popupNode,
        )}
    </div>
  )
}
