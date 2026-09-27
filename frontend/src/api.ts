// Типизированный клиент REST API /api/v1 (контракт — docs/api.md).

export type HorizonId = 'day' | 'month' | 'year' | 'history'
export type Granularity = 'hour' | 'day' | 'month' | 'total'

export interface RouteMeta {
  route: number
  name: string
  color: string
  stops: number
  geometry: 'gtfs' | 'osm' | null
  historyBoardings: number
  forecastBoardings: number
  note: string | null
}

export interface HorizonMeta {
  id: HorizonId
  label: string
  from: string
  to: string
  defaultGranularity: Granularity
  granularities: Granularity[]
}

export interface EventMeta {
  name: string
  routes: number[] | null
  start: string
  end: string
  hours: number[] | null
  mult: number
  note: string
  source: string[]
}

export interface RegimeMeta {
  route: number
  state: string
  days: string
  start: string
  end: string
  note: string
  source: string[]
}

export interface SourceMeta {
  name: string
  effect: string
  url: string
}

export interface Meta {
  model: { name: string; version: string; leaderboardWapeScore: number; formula: string }
  routes: RouteMeta[]
  horizons: HorizonMeta[]
  events: EventMeta[]
  regimes: RegimeMeta[]
  sources: SourceMeta[]
  weather: { beta: number; monthNormMm: Record<string, number> }
}

export interface Stop {
  stopId: string
  route: number
  direction: number
  seq: number
  name: string
  lat: number
  lon: number
  weight: number
}

export interface RouteStops {
  route: number
  directions: { direction: number; stops: Stop[] }[]
}

export interface ForecastPoint {
  t: string
  base: number
  pred: number
  lo: number | null
  hi: number | null
}

export interface ForecastSeries {
  key: string
  label: string
  color: string
  points: ForecastPoint[]
}

export interface Impact {
  base: number
  pred: number
  delta: number
  deltaPct: number
}

export interface ForecastResponse {
  query: Record<string, unknown>
  unit: string
  series: ForecastSeries[]
  totals: Impact
  byRoute: (Impact & { route: number })[]
  notes: string[]
  computeMs: number
}

export interface MapRoute {
  route: number
  color: string
  name: string
  hourly: number[]
  total: number
  lines: { direction: number; coords: [number, number][] }[]
}

export interface MapStop extends Stop {
  hourly: number[]
  total: number
}

export interface MapResponse {
  date: string
  mode: 'history' | 'forecast' | 'year'
  dayType: { code: number; label: string } | null
  weather: { precipMm: number | null; tempC: number | null } | null
  routes: MapRoute[]
  stops: MapStop[]
  maxStopHour: number
}

export interface FleetSummary {
  route: number
  normBpv: number
  fleetMax: number
  serviceHours: number
  candHours: number
  extraVehicleHours: number
  feasibleVehicleHours?: number
  topHours: string
  candShare: number
  reserveHours?: number
  reserveVehicleHours?: number
  reserveTopHours?: string
  plannedVehicleHours?: number
  reserveShare?: number
}

export interface FleetReserve {
  route: number
  date: string
  hour: number
  pred: number
  nPlan: number
  bpv: number
  norm: number
  need: number
  reserve: number
}

export interface FleetCandidate {
  route: number
  date: string
  hour: number
  pred: number
  nPlan: number
  bpv: number
  norm: number
  extra: number
  extraFeasible?: number
}

export interface FleetResponse {
  summary: FleetSummary[]
  candidates: FleetCandidate[]
  reserve?: FleetReserve[]
  method?: string
  reserveMethod?: string
}

export interface MapLiveResponse {
  date: string
  available: boolean
  lastHour: number | null
  receivedTotal: number
  routes: { route: number; hourly: number[] }[]
  alerts: MonitoringAlert[]
}

export type Row = Record<string, unknown>

export interface QualityResponse {
  leaderboardWapeScore: number
  byHorizon: Row[]
  byLevel: Row[]
  byRoute: Row[]
  folds: Row[]
}

export interface CalendarDay {
  date: string
  code: number
  dow: number
  label: string
  precipMm: number | null
  tempC: number | null
}

export interface IngestResult {
  received: number
  accepted: number
  rejected: number
  boardings: number
  cells: number
  [k: string]: unknown
}

export interface MonitoringResponse {
  date: string
  forecastSource?: string
  lastHour?: number | null
  hoursCovered?: number[]
  coveredActual?: number
  coveredForecast?: number | null
  cells: { route: number; hour: number; actual: number; forecast: number }[]
  /** по docs/api.md */
  totals?: { actual: number; forecast: number }
  /** фактический ответ backend (плоские поля) */
  actualTotal?: number
  forecastTotal?: number
  receivedTotal?: number
  boardingsTotal?: number
  wapeScore: number | null
  alerts?: MonitoringAlert[]
  alertParams?: { threshold: number; minHours: number; minForecast?: number }
}

export interface MonitoringAlert {
  route: number
  fromHour: number
  toHour: number
  hours: number
  actual: number
  forecast: number
  deviationPct: number
  kind: 'drop' | 'surge'
  message: string
}

export interface SimulateResult extends IngestResult {
  simulated?: boolean
}

export interface SummaryReinforceHour {
  hour: number
  pred: number
  nPlan: number
  norm: number
  extra: number
}

export interface SummaryRoute {
  route: number
  name: string
  color: string
  total: number
  peakHour: number
  peakValue: number
  hourly: number[]
  weekAgoTotal: number | null
  deltaWeekPct: number | null
  extraVehicleHours: number
  reinforceHours: SummaryReinforceHour[]
  reserveVehicleHours?: number
  reserveHours?: SummaryReserveHour[]
}

export interface SummaryReserveHour {
  hour: number
  pred: number
  nPlan: number
  norm: number
  need: number
  reserve: number
}

export interface SummaryResponse {
  date: string
  mode: 'history' | 'forecast' | 'year'
  dayType: { code: number; label: string } | null
  weather: { precipMm: number | null; tempC: number | null } | null
  network: {
    total: number
    peakHour: number
    peakValue: number
    hourly: number[]
    weekAgoDate: string | null
    weekAgoTotal: number | null
    deltaWeekPct: number | null
  }
  routes: SummaryRoute[]
  hotStops: { stopId: string; route: number; direction: number; seq: number; name: string; hour: number; value: number; dayTotal: number }[]
  fleet: { available: boolean; candidateHours: number; extraVehicleHours: number; reserveHours?: number; reserveVehicleHours?: number } | null
  events: { kind: 'event' | 'regime' | string; name: string; routes: number[] | null; note: string; source: string[] | null }[]
  recommendations: string[]
  scenario: boolean
  computeMs: number
}

export class ApiError extends Error {
  readonly status: number
  readonly parameter?: string
  constructor(message: string, status: number, parameter?: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.parameter = parameter
  }
}

const UNAVAILABLE = 'Сервер недоступен. Проверьте, что backend запущен, и повторите попытку.'

export function isAbort(e: unknown): boolean {
  return e instanceof DOMException && e.name === 'AbortError'
}

export function errorMessage(e: unknown): string {
  if (e instanceof ApiError) return e.message
  if (e instanceof Error) return e.message
  return String(e)
}

async function toApiError(res: Response): Promise<ApiError> {
  const text = await res.text().catch(() => '')
  let detail = ''
  let parameter: string | undefined
  if (text) {
    try {
      const body = JSON.parse(text) as { detail?: unknown; title?: unknown; message?: unknown; parameter?: unknown }
      const d = body.detail ?? body.message ?? body.title
      if (typeof d === 'string') detail = d
      if (typeof body.parameter === 'string') parameter = body.parameter
    } catch {
      // тело не JSON (например, HTML-страница прокси) — сообщение сформируем по коду
    }
  }
  if (!detail) {
    if ([502, 503, 504].includes(res.status) || (res.status === 500 && !text)) {
      return new ApiError(UNAVAILABLE, res.status)
    }
    const byCode: Record<number, string> = {
      400: 'Некорректный запрос',
      404: 'Данные не найдены',
      413: 'Слишком большая выгрузка — сузьте период или список маршрутов',
      415: 'Неверный формат данных',
      500: 'Внутренняя ошибка сервера',
    }
    detail = byCode[res.status] ?? `Ошибка сервера (HTTP ${res.status})`
  }
  return new ApiError(detail, res.status, parameter)
}

/** Скачивание выгрузки: ошибки сервера (400/413, problem+json) показываются пользователю, а не сохраняются в файл. */
export async function downloadFile(url: string): Promise<void> {
  let res: Response
  try {
    res = await fetch(url)
  } catch {
    throw new ApiError(UNAVAILABLE, 0)
  }
  if (!res.ok) throw await toApiError(res)
  const blob = await res.blob()
  const m = /filename="([^"]+)"/.exec(res.headers.get('Content-Disposition') ?? '')
  const link = document.createElement('a')
  link.href = URL.createObjectURL(blob)
  link.download = m?.[1] ?? 'forecast'
  document.body.appendChild(link)
  link.click()
  link.remove()
  setTimeout(() => URL.revokeObjectURL(link.href), 30_000)
}

export async function apiRequest<T>(
  url: string,
  init: { method?: string; body?: BodyInit; contentType?: string; signal?: AbortSignal } = {},
): Promise<T> {
  let res: Response
  try {
    res = await fetch(url, {
      method: init.method ?? 'GET',
      body: init.body,
      signal: init.signal,
      headers: {
        Accept: 'application/json, application/problem+json',
        ...(init.contentType ? { 'Content-Type': init.contentType } : {}),
      },
    })
  } catch (e) {
    if (isAbort(e)) throw e
    throw new ApiError(UNAVAILABLE, 0)
  }
  if (!res.ok) throw await toApiError(res)
  if (res.status === 204) return undefined as T
  const text = await res.text()
  if (!text) return undefined as T
  try {
    return JSON.parse(text) as T
  } catch {
    if (text.trimStart().startsWith('<')) throw new ApiError(UNAVAILABLE, res.status)
    return text as T
  }
}

export type QueryValue = string | number | boolean | null | undefined | (string | number)[]

/** Собирает query-строку; пустые значения пропускаются, массивы — повторяющиеся параметры. */
export function qs(params: Record<string, QueryValue>): string {
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue
    if (Array.isArray(v)) {
      for (const item of v) sp.append(k, String(item))
    } else {
      sp.append(k, String(v))
    }
  }
  const s = sp.toString()
  return s ? `?${s}` : ''
}

export const API = '/api/v1'

/** Горизонты по контракту — на случай, если /meta их не вернул. */
export const DEFAULT_HORIZONS: HorizonMeta[] = [
  { id: 'day', label: 'День (почасовой)', from: '2025-11-01', to: '2025-12-31', defaultGranularity: 'hour', granularities: ['hour', 'day', 'month', 'total'] },
  { id: 'month', label: 'Месяц', from: '2025-11-01', to: '2025-12-31', defaultGranularity: 'day', granularities: ['day', 'month', 'total'] },
  { id: 'year', label: 'Год (сценарий 2026)', from: '2026-01-01', to: '2026-12-31', defaultGranularity: 'month', granularities: ['hour', 'day', 'month', 'total'] },
  { id: 'history', label: 'История (факт)', from: '2025-01-01', to: '2025-10-31', defaultGranularity: 'day', granularities: ['hour', 'day', 'month', 'total'] },
]

/** Приводит ответ /meta к ожидаемой форме (терпимо к snake_case и отсутствующим полям). */
export function normalizeMeta(raw: unknown): Meta {
  const m = (raw ?? {}) as Record<string, unknown>
  const model = (m.model ?? {}) as Record<string, unknown>
  const routes = Array.isArray(m.routes) ? (m.routes as Record<string, unknown>[]) : []
  const weather = (m.weather ?? {}) as Record<string, unknown>
  const horizons = Array.isArray(m.horizons) && m.horizons.length ? (m.horizons as HorizonMeta[]) : DEFAULT_HORIZONS
  return {
    model: {
      name: String(model.name ?? ''),
      version: String(model.version ?? ''),
      leaderboardWapeScore: Number(model.leaderboardWapeScore ?? model.leaderboard_wape_score ?? 0),
      formula: String(model.formula ?? ''),
    },
    routes: routes.map((r) => ({
      route: Number(r.route),
      name: String(r.name ?? ''),
      color: String(r.color ?? '#607d8b'),
      stops: Number(r.stops ?? 0),
      geometry: (r.geometry ?? null) as RouteMeta['geometry'],
      historyBoardings: Number(r.historyBoardings ?? r.history_boardings ?? 0),
      forecastBoardings: Number(r.forecastBoardings ?? r.forecast_boardings ?? 0),
      note: (r.note ?? null) as string | null,
    })),
    horizons: horizons.map((h) => {
      const fallback = DEFAULT_HORIZONS.find((d) => d.id === h.id)
      return {
        ...h,
        granularities: h.granularities?.length ? h.granularities : (fallback?.granularities ?? ['day']),
        defaultGranularity: h.defaultGranularity ?? fallback?.defaultGranularity ?? 'day',
      }
    }),
    events: Array.isArray(m.events) ? (m.events as EventMeta[]) : [],
    regimes: Array.isArray(m.regimes) ? (m.regimes as RegimeMeta[]) : [],
    sources: Array.isArray(m.sources) ? (m.sources as SourceMeta[]) : [],
    weather: {
      beta: typeof weather.beta === 'number' ? weather.beta : -0.012,
      monthNormMm: (weather.monthNormMm ?? {}) as Record<string, number>,
    },
  }
}
