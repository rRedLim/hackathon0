import { createContext, useCallback, useContext, useEffect, useState, useSyncExternalStore } from 'react'
import { apiRequest, errorMessage, isAbort, type Meta } from './api'

// ---------- Глобальные уведомления об ошибках ----------

export interface Toast {
  id: number
  text: string
  kind: 'error' | 'info'
}

let toasts: Toast[] = []
let nextId = 1
const listeners = new Set<() => void>()

function emit() {
  for (const l of listeners) l()
}

export function notify(text: string, kind: Toast['kind'] = 'error') {
  // одинаковое сообщение не дублируем
  if (toasts.some((t) => t.text === text)) return
  const id = nextId++
  toasts = [...toasts.slice(-3), { id, text, kind }]
  emit()
  if (kind === 'info') setTimeout(() => dismiss(id), 4000)
}

export function dismiss(id: number) {
  toasts = toasts.filter((t) => t.id !== id)
  emit()
}

export function useToasts(): Toast[] {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb)
      return () => listeners.delete(cb)
    },
    () => toasts,
  )
}

// ---------- Загрузка данных ----------

export interface ApiState<T> {
  data: T | undefined
  loading: boolean
  error: string | undefined
  reload: () => void
}

/**
 * GET-запрос с отменой устаревших запросов (AbortController) и необязательным debounce.
 * url = null — запрос не выполняется. Предыдущие данные сохраняются на время загрузки.
 */
export function useApi<T>(url: string | null, debounceMs = 0): ApiState<T> {
  const [nonce, setNonce] = useState(0)
  const [res, setRes] = useState<{ key: string | null; data?: T; error?: string }>({ key: null })
  const key = url === null ? null : `${nonce}|${url}`

  useEffect(() => {
    if (url === null) return
    const k = `${nonce}|${url}`
    const ctrl = new AbortController()
    const timer = setTimeout(() => {
      apiRequest<T>(url, { signal: ctrl.signal })
        .then((d) => {
          if (!ctrl.signal.aborted) setRes({ key: k, data: d })
        })
        .catch((e: unknown) => {
          if (isAbort(e) || ctrl.signal.aborted) return
          const msg = errorMessage(e)
          setRes((prev) => ({ key: k, data: prev.data, error: msg }))
          notify(msg)
        })
    }, debounceMs)
    return () => {
      clearTimeout(timer)
      ctrl.abort()
    }
  }, [url, debounceMs, nonce])

  const reload = useCallback(() => setNonce((n) => n + 1), [])
  return {
    data: res.data,
    loading: key !== null && res.key !== key,
    error: res.key === key ? res.error : undefined,
    reload,
  }
}

// ---------- Справочник /meta ----------

export const MetaContext = createContext<Meta | null>(null)

export function useMeta(): Meta {
  const m = useContext(MetaContext)
  if (!m) throw new Error('MetaContext не инициализирован')
  return m
}

export function useRouteColor(): (route: number | string) => string {
  const meta = useMeta()
  return (route) => meta.routes.find((r) => String(r.route) === String(route))?.color ?? '#546e7a'
}
