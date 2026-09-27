import { lazy, Suspense, useEffect, useMemo, useState } from 'react'
import { API, normalizeMeta } from './api'
import { Spinner, StateBox, Toasts } from './components'
import { fmtScore } from './format'
import { MetaContext, useApi } from './hooks'

const MapPage = lazy(() => import('./pages/MapPage'))
const SummaryPage = lazy(() => import('./pages/SummaryPage'))
const ForecastPage = lazy(() => import('./pages/ForecastPage'))
const ScenarioPage = lazy(() => import('./pages/ScenarioPage'))
const FleetPage = lazy(() => import('./pages/FleetPage'))
const ModelPage = lazy(() => import('./pages/ModelPage'))
const MonitoringPage = lazy(() => import('./pages/MonitoringPage'))

const TABS = [
  { id: 'map', label: 'Карта' },
  { id: 'summary', label: 'Сводка' },
  { id: 'forecast', label: 'Прогноз' },
  { id: 'scenario', label: 'Сценарии' },
  { id: 'fleet', label: 'Выпуск' },
  { id: 'model', label: 'Модель и данные' },
  { id: 'monitoring', label: 'Мониторинг' },
] as const

type TabId = (typeof TABS)[number]['id']

function readHash(): TabId {
  const h = window.location.hash.replace(/^#\/?/, '')
  return (TABS.find((t) => t.id === h)?.id ?? 'map') as TabId
}

export default function App() {
  const [tab, setTab] = useState<TabId>(readHash)
  useEffect(() => {
    const on = () => setTab(readHash())
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])

  const metaRes = useApi<unknown>(`${API}/meta`)
  const meta = useMemo(() => (metaRes.data ? normalizeMeta(metaRes.data) : null), [metaRes.data])

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <svg width="26" height="26" viewBox="0 0 32 32" aria-hidden>
            <rect width="32" height="32" rx="7" fill="#1f5fa8" />
            <path d="M9 8h14v11a3 3 0 0 1-3 3h-8a3 3 0 0 1-3-3z" fill="#fff" />
            <rect x="11" y="10" width="10" height="5" rx="1" fill="#1f5fa8" />
          </svg>
          <div>
            <div className="brand-title">ИИ-прогноз загрузки трамвайных маршрутов</div>
            <div className="brand-sub">Москва · пассажиропоток по маршрутам, остановкам и участкам</div>
          </div>
        </div>
        <nav className="tabs">
          {TABS.map((t) => (
            <a key={t.id} href={`#/${t.id}`} className={`tab ${tab === t.id ? 'active' : ''}`}>
              {t.label}
            </a>
          ))}
        </nav>
        {meta && (
          <div className="score-badge" title={`Модель ${meta.model.version}`}>
            WAPE-score <b>{fmtScore(meta.model.leaderboardWapeScore)}</b>
          </div>
        )}
      </header>
      <main className="main">
        {!meta ? (
          <div className="page">
            <StateBox
              loading={metaRes.loading || (!metaRes.error && !metaRes.data)}
              error={metaRes.error}
              onRetry={metaRes.reload}
            />
          </div>
        ) : (
          <MetaContext.Provider value={meta}>
            <Suspense
              fallback={
                <div className="page">
                  <div className="state-box">
                    <Spinner />
                  </div>
                </div>
              }
            >
              {tab === 'map' && <MapPage />}
              {tab === 'summary' && <SummaryPage />}
              {tab === 'forecast' && <ForecastPage />}
              {tab === 'scenario' && <ScenarioPage />}
              {tab === 'fleet' && <FleetPage />}
              {tab === 'model' && <ModelPage />}
              {tab === 'monitoring' && <MonitoringPage />}
            </Suspense>
          </MetaContext.Provider>
        )}
      </main>
      <Toasts />
    </div>
  )
}
