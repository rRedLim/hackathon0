const nf0 = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 })
const nf1 = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 })
const nf2 = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 })
const nf3 = new Intl.NumberFormat('ru-RU', { minimumFractionDigits: 3, maximumFractionDigits: 4 })

export function fmtNum(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  if (digits === 0) return nf0.format(v)
  if (digits === 1) return nf1.format(v)
  return nf2.format(v)
}

export function fmtScore(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return nf3.format(v)
}

export function fmtPct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  const s = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: digits, minimumFractionDigits: digits }).format(v)
  return `${v > 0 ? '+' : ''}${s} %`
}

export function fmtShare(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return `${nf1.format(v * 100)} %`
}

/** 2025-11-10 → 10.11.2025 */
export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso)
  return m ? `${m[3]}.${m[2]}.${m[1]}` : iso
}

const MONTHS_SHORT = ['янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']
export const MONTHS_FULL = [
  'Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
  'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь',
]
export const DOW_SHORT = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']

/** Метка периода из поля `t`: час / день / месяц / total. */
export function fmtPeriod(t: string): string {
  if (t === 'total') return 'Итого за период'
  let m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(t)
  if (m) return `${m[3]}.${m[2]}.${m[1]} ${m[4]}:${m[5]}`
  m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(t)
  if (m) return `${m[3]}.${m[2]}.${m[1]}`
  m = /^(\d{4})-(\d{2})$/.exec(t)
  if (m) return `${MONTHS_SHORT[Number(m[2]) - 1] ?? m[2]} ${m[1]}`
  return t
}

/** Короткая метка для оси графика. */
export function fmtTick(t: string): string {
  let m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(t)
  if (m) return `${m[3]}.${m[2]} ${m[4]}:00`
  m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(t)
  if (m) return `${m[3]}.${m[2]}`
  return fmtPeriod(t)
}

export function hourLabel(h: number): string {
  return `${String(h).padStart(2, '0')}:00`
}

export function clampDate(v: string, min: string, max: string): string {
  if (!v) return min
  if (v < min) return min
  if (v > max) return max
  return v
}

export function addDays(iso: string, days: number): string {
  const d = new Date(`${iso}T00:00:00Z`)
  d.setUTCDate(d.getUTCDate() + days)
  return d.toISOString().slice(0, 10)
}

export function daysBetween(from: string, to: string): number {
  return Math.round((Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / 86400000) + 1
}

/** Месяцы (1–12), попадающие в интервал дат. */
export function monthsInRange(from: string, to: string): number[] {
  const res: number[] = []
  let y = Number(from.slice(0, 4))
  let mo = Number(from.slice(5, 7))
  const ey = Number(to.slice(0, 4))
  const em = Number(to.slice(5, 7))
  while (y < ey || (y === ey && mo <= em)) {
    if (!res.includes(mo)) res.push(mo)
    mo += 1
    if (mo > 12) {
      mo = 1
      y += 1
    }
  }
  return res
}

export function granularityLabel(g: string): string {
  return ({ hour: 'по часам', day: 'по дням', month: 'по месяцам', total: 'итог за период' } as Record<string, string>)[g] ?? g
}

const HEAT_STOPS = [
  [255, 247, 200],
  [254, 204, 92],
  [253, 141, 60],
  [227, 26, 28],
  [128, 0, 38],
]

/** Последовательная шкала «светло-жёлтый → тёмно-красный»; 0 — нейтральный фон. */
export function heatColor(v: number, max: number): string {
  if (!(v > 0) || !(max > 0)) return '#f4f6f9'
  const t = Math.min(1, v / max)
  const x = t * (HEAT_STOPS.length - 1)
  const i = Math.min(HEAT_STOPS.length - 2, Math.floor(x))
  const f = x - i
  const c = HEAT_STOPS[i].map((a, k) => Math.round(a + (HEAT_STOPS[i + 1][k] - a) * f))
  return `rgb(${c[0]},${c[1]},${c[2]})`
}

export const HEAT_GRADIENT = `linear-gradient(90deg, ${HEAT_STOPS.map((c) => `rgb(${c.join(',')})`).join(', ')})`

const RESERVE_STOPS = [
  [232, 240, 250],
  [158, 196, 230],
  [74, 140, 200],
  [30, 84, 150],
]

/** Шкала резерва (снимаемые вагоны): светло- → тёмно-синий, отличается от красной шкалы усиления. */
export function reserveColor(v: number, max: number): string {
  if (!(v > 0) || !(max > 0)) return '#f4f6f9'
  const t = Math.min(1, v / max)
  const x = t * (RESERVE_STOPS.length - 1)
  const i = Math.min(RESERVE_STOPS.length - 2, Math.floor(x))
  const f = x - i
  const c = RESERVE_STOPS[i].map((a, k) => Math.round(a + (RESERVE_STOPS[i + 1][k] - a) * f))
  return `rgb(${c[0]},${c[1]},${c[2]})`
}

export const RESERVE_GRADIENT = `linear-gradient(90deg, ${RESERVE_STOPS.map((c) => `rgb(${c.join(',')})`).join(', ')})`

/** Вид алерта детектора: провал маршрута, всплеск, обвал всей сети. */
export function alertKindLabel(kind: string): string {
  return kind === 'surge' ? '▲ всплеск' : kind === 'network_drop' ? '▼ обвал сети' : '▼ провал'
}
