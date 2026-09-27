import { useState, type ReactNode } from 'react'
import type { RouteMeta } from './api'
import { dismiss, useToasts } from './hooks'

export function Toasts() {
  const items = useToasts()
  if (!items.length) return null
  return (
    <div className="toasts" role="alert" aria-live="assertive">
      {items.map((t) => (
        <div key={t.id} className={`toast toast-${t.kind}`}>
          <span className="toast-icon">{t.kind === 'error' ? '!' : 'i'}</span>
          <span className="toast-text">{t.text}</span>
          <button className="toast-close" onClick={() => dismiss(t.id)} aria-label="Закрыть">
            ×
          </button>
        </div>
      ))}
    </div>
  )
}

export function Spinner({ label = 'Загрузка…' }: { label?: string }) {
  return (
    <span className="spinner-wrap">
      <span className="spinner" aria-hidden />
      <span>{label}</span>
    </span>
  )
}

/** Блок состояния: загрузка / ошибка с повтором / пусто. */
export function StateBox({
  loading,
  error,
  empty,
  onRetry,
  children,
}: {
  loading?: boolean
  error?: string
  empty?: string | false
  onRetry?: () => void
  children?: ReactNode
}) {
  if (error)
    return (
      <div className="state-box state-error">
        <div>
          <b>Не удалось загрузить данные.</b> {error}
        </div>
        {onRetry && (
          <button className="btn" onClick={onRetry}>
            Повторить
          </button>
        )}
      </div>
    )
  if (loading)
    return (
      <div className="state-box">
        <Spinner />
      </div>
    )
  if (empty) return <div className="state-box state-empty">{empty}</div>
  return <>{children}</>
}

export function RouteChips({
  routes,
  selected,
  onChange,
  compact,
}: {
  routes: RouteMeta[]
  selected: number[]
  onChange: (v: number[]) => void
  compact?: boolean
}) {
  const all = selected.length === routes.length
  const toggle = (r: number) =>
    onChange(selected.includes(r) ? selected.filter((x) => x !== r) : [...selected, r].sort((a, b) => a - b))
  return (
    <div className={`chips ${compact ? 'chips-compact' : ''}`}>
      <button
        className={`chip chip-all ${all ? 'on' : ''}`}
        onClick={() => onChange(all ? [] : routes.map((r) => r.route))}
        title={all ? 'Снять все' : 'Выбрать все'}
      >
        {all ? 'Все' : 'Выбрать все'}
      </button>
      {routes.map((r) => {
        const on = selected.includes(r.route)
        return (
          <button
            key={r.route}
            className={`chip ${on ? 'on' : ''}`}
            style={on ? { background: r.color, borderColor: r.color } : { borderColor: r.color, color: r.color }}
            onClick={() => toggle(r.route)}
            title={r.name || `Маршрут ${r.route}`}
          >
            {r.route}
          </button>
        )
      })}
    </div>
  )
}

export function Kpi({ label, value, sub, accent }: { label: string; value: ReactNode; sub?: ReactNode; accent?: string }) {
  return (
    <div className="kpi" style={accent ? { borderTopColor: accent } : undefined}>
      <div className="kpi-label">{label}</div>
      <div className="kpi-value">{value}</div>
      {sub !== undefined && <div className="kpi-sub">{sub}</div>}
    </div>
  )
}

export function Pager({ page, pages, onPage }: { page: number; pages: number; onPage: (p: number) => void }) {
  if (pages <= 1) return null
  return (
    <div className="pager">
      <button className="btn btn-sm" disabled={page <= 0} onClick={() => onPage(0)}>
        «
      </button>
      <button className="btn btn-sm" disabled={page <= 0} onClick={() => onPage(page - 1)}>
        ‹
      </button>
      <span>
        стр. {page + 1} из {pages}
      </span>
      <button className="btn btn-sm" disabled={page >= pages - 1} onClick={() => onPage(page + 1)}>
        ›
      </button>
      <button className="btn btn-sm" disabled={page >= pages - 1} onClick={() => onPage(pages - 1)}>
        »
      </button>
    </div>
  )
}

export interface Column<T> {
  key: string
  title: string
  render: (row: T) => ReactNode
  num?: boolean
}

export function DataTable<T>({
  rows,
  columns,
  pageSize = 50,
  empty = 'Нет данных',
  maxHeight,
}: {
  rows: T[]
  columns: Column<T>[]
  pageSize?: number
  empty?: string
  maxHeight?: number
}) {
  const [page, setPage] = useState(0)
  const pages = Math.max(1, Math.ceil(rows.length / pageSize))
  const p = Math.min(page, pages - 1)
  const slice = rows.slice(p * pageSize, (p + 1) * pageSize)
  return (
    <div>
      <div className="table-wrap" style={maxHeight ? { maxHeight } : undefined}>
        <table className="table">
          <thead>
            <tr>
              {columns.map((c) => (
                <th key={c.key} className={c.num ? 'num' : undefined}>
                  {c.title}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {slice.length === 0 ? (
              <tr>
                <td colSpan={columns.length} className="muted center">
                  {empty}
                </td>
              </tr>
            ) : (
              slice.map((row, i) => (
                <tr key={p * pageSize + i}>
                  {columns.map((c) => (
                    <td key={c.key} className={c.num ? 'num' : undefined}>
                      {c.render(row)}
                    </td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
      <div className="table-foot">
        <span className="muted">строк: {rows.length.toLocaleString('ru-RU')}</span>
        <Pager page={p} pages={pages} onPage={setPage} />
      </div>
    </div>
  )
}

export function RouteBadge({ route, color }: { route: number | string; color: string }) {
  return (
    <span className="route-badge" style={{ background: color }}>
      {route}
    </span>
  )
}

export function Section({ title, extra, children, className }: { title?: ReactNode; extra?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`card ${className ?? ''}`}>
      {(title || extra) && (
        <div className="card-head">
          {title && <h3>{title}</h3>}
          {extra && <div className="card-extra">{extra}</div>}
        </div>
      )}
      {children}
    </section>
  )
}

export function ExternalLink({ href, children }: { href: string; children?: ReactNode }) {
  if (!/^https?:\/\//.test(href)) return <span className="muted">{children ?? href}</span>
  return (
    <a href={href} target="_blank" rel="noreferrer noopener">
      {children ?? href}
    </a>
  )
}
