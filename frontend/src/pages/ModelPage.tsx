import { API, type QualityResponse, type Row } from '../api'
import { ExternalLink, RouteBadge, Section, StateBox } from '../components'
import { fmtDate, fmtNum, fmtScore, granularityLabel } from '../format'
import { useApi, useMeta } from '../hooks'

const LABELS: Record<string, string> = {
  horizon: 'Горизонт',
  folds: 'Фолды',
  fold: 'Фолд (обучение → проверка)',
  hourlyScore: 'Score (час)',
  hourlyScoreNoC: 'Score (час, без фолда C)',
  dailyScore: 'Score (сутки)',
  bias: 'Смещение',
  level: 'Уровень',
  score: 'Score',
  n: 'Наблюдений',
  medianAbsErr: 'Медиана |ошибки|',
  p80AbsErr: 'p80 |ошибки|',
  route: 'Маршрут',
}

const toCamel = (k: string) => k.replace(/_([a-z])/gi, (_, c: string) => c.toUpperCase())

function fmtCell(key: string, v: unknown): string {
  if (v === null || v === undefined || v === '') return '—'
  if (typeof v === 'number') {
    const k = toCamel(key)
    if (/score/i.test(k)) return fmtScore(v)
    if (k === 'bias' || /Err$/.test(k)) return `${v > 0 ? '+' : ''}${fmtNum(v * 100, 1)} %`
    if (Number.isInteger(v)) return fmtNum(v)
    return fmtNum(v, 2)
  }
  if (Array.isArray(v)) return v.join(', ')
  return String(v)
}

function GenericTable({ rows, routeColor }: { rows: Row[]; routeColor: (r: number) => string }) {
  if (!rows?.length) return <div className="muted">Нет данных</div>
  const keys = Object.keys(rows[0])
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            {keys.map((k) => (
              <th key={k} className={typeof rows[0][k] === 'number' && k !== 'route' ? 'num' : undefined}>
                {LABELS[toCamel(k)] ?? k}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              {keys.map((k) => (
                <td key={k} className={typeof r[k] === 'number' && k !== 'route' ? 'num' : undefined}>
                  {k === 'route' && typeof r[k] === 'number' ? <RouteBadge route={r[k] as number} color={routeColor(r[k] as number)} /> : fmtCell(k, r[k])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Sources({ urls }: { urls: string[] | null | undefined }) {
  if (!urls?.length) return null
  return (
    <div className="src-links">
      источники:{' '}
      {urls.map((u, i) => (
        <span key={u}>
          {i > 0 && ', '}
          <ExternalLink href={u}>[{i + 1}]</ExternalLink>
        </span>
      ))}
    </div>
  )
}

export default function ModelPage() {
  const meta = useMeta()
  const q = useApi<QualityResponse>(`${API}/quality`)
  const colorOf = (r: number) => meta.routes.find((x) => x.route === r)?.color ?? '#607d8b'
  const score = q.data?.leaderboardWapeScore ?? meta.model.leaderboardWapeScore

  return (
    <div className="page">
      <div className="model-hero card">
        <div className="hero-score">
          <div className="hero-score-val">{fmtScore(score)}</div>
          <div className="muted small">WAPE-score на лидерборде (1 − WAPE)</div>
        </div>
        <div className="hero-text">
          <h2>{meta.model.name || 'Модель прогноза'}</h2>
          <div className="formula">{meta.model.formula}</div>
          <div className="muted small">
            Версия {meta.model.version} · API: <a href="/swagger-ui.html" target="_blank" rel="noreferrer">Swagger UI</a> ·{' '}
            <a href="/v3/api-docs" target="_blank" rel="noreferrer">OpenAPI</a> ·{' '}
            <a href="/actuator/health" target="_blank" rel="noreferrer">health</a>
          </div>
        </div>
      </div>

      <StateBox loading={q.loading && !q.data} error={!q.data ? q.error : undefined} onRetry={q.reload}>
        {q.data && (
          <div className="grid-2">
            <Section title="Точность по горизонту прогноза">
              <GenericTable rows={q.data.byHorizon} routeColor={colorOf} />
            </Section>
            <Section title="Точность по уровню агрегации">
              <GenericTable rows={q.data.byLevel} routeColor={colorOf} />
            </Section>
            <Section title="Точность по маршрутам">
              <GenericTable rows={q.data.byRoute} routeColor={colorOf} />
            </Section>
            <Section title="Скользящая проверка (фолды)">
              <GenericTable rows={q.data.folds} routeColor={colorOf} />
            </Section>
          </div>
        )}
      </StateBox>

      <div className="grid-2">
        <Section title="Горизонты прогноза">
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Горизонт</th>
                  <th>Период</th>
                  <th>Гранулярность</th>
                </tr>
              </thead>
              <tbody>
                {meta.horizons.map((h) => (
                  <tr key={h.id}>
                    <td>{h.label}</td>
                    <td>
                      {fmtDate(h.from)} — {fmtDate(h.to)}
                    </td>
                    <td>{h.granularities.map(granularityLabel).join(', ')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
        <Section title="Область применимости и ограничения">
          <ul className="limits">
            <li>Модель валидна для 9 маршрутов с историей не менее 6 недель; маршрут 5 (запуск 16.12.2025) — холодный старт по аналогу (маршрут 7).</li>
            <li>Почасовой горизонт проверен на глубину до 61 дня (ноябрь–декабрь 2025).</li>
            <li>Годовой горизонт 2026 — качественный сценарий: уровень, сезонность и календарь, без внешних событий будущего.</li>
            <li>Остановочный уровень и участки — распределение посадок маршрута по априорным весам: в валидациях нет идентификатора остановки.</li>
            <li>
              Не учитываются: внеплановые происшествия и сходы, изменения тарифов, наполняемость вагонов (нет данных о высадках) — для таких
              случаев используйте корректирующие коэффициенты на вкладке «Сценарии».
            </li>
          </ul>
        </Section>
      </div>

      <Section title="Маршруты">
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Маршрут</th>
                <th>Название</th>
                <th className="num">Остановок</th>
                <th>Геометрия</th>
                <th className="num">Посадок в истории</th>
                <th className="num">Прогноз ноя–дек 2025</th>
                <th>Примечание</th>
              </tr>
            </thead>
            <tbody>
              {meta.routes.map((r) => (
                <tr key={r.route}>
                  <td>
                    <RouteBadge route={r.route} color={r.color} />
                  </td>
                  <td>{r.name || <span className="muted">нет в справочнике</span>}</td>
                  <td className="num">{r.stops ? fmtNum(r.stops) : '—'}</td>
                  <td>{r.geometry === 'gtfs' ? 'справочник ГТФС' : r.geometry === 'osm' ? 'OpenStreetMap' : <span className="muted">нет (только прогноз)</span>}</td>
                  <td className="num">{fmtNum(r.historyBoardings)}</td>
                  <td className="num">{fmtNum(r.forecastBoardings)}</td>
                  <td className="small">{r.note ?? ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      <Section title="Внешние источники данных">
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Источник</th>
                <th>Эффект</th>
                <th>Ссылка</th>
              </tr>
            </thead>
            <tbody>
              {meta.sources.map((s) => (
                <tr key={s.name}>
                  <td>{s.name}</td>
                  <td>{s.effect}</td>
                  <td className="url-cell">
                    <ExternalLink href={s.url}>{s.url.replace(/^https?:\/\/([^/?]+).*$/, '$1')}</ExternalLink>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      <div className="grid-2">
        <Section title="События, учтённые в модели">
          <ul className="notes-list">
            {meta.events.map((e) => (
              <li key={e.name}>
                <div>
                  <b>{e.name}</b> · {e.routes ? `маршруты ${e.routes.join(', ')}` : 'все маршруты'} · {fmtDate(e.start)} — {fmtDate(e.end)}
                  {e.hours ? ` · часы ${e.hours.join(', ')}` : ''} · множитель ×{e.mult}
                </div>
                <div className="muted small">{e.note}</div>
                <Sources urls={e.source} />
              </li>
            ))}
          </ul>
        </Section>
        <Section title="Режимы работы маршрутов">
          <ul className="notes-list">
            {meta.regimes.map((r, i) => (
              <li key={i}>
                <div>
                  <RouteBadge route={r.route} color={colorOf(r.route)} />{' '}
                  <b>{r.state === 'closed' ? 'не ходит' : r.state === 'short' ? 'укорочен' : r.state === 'exclude' ? 'исключено из обучения' : r.state}</b>{' '}
                  · {r.days === 'offdays' ? 'выходные' : 'все дни'} · {fmtDate(r.start)} — {fmtDate(r.end)}
                </div>
                <div className="muted small">{r.note}</div>
                <Sources urls={r.source} />
              </li>
            ))}
          </ul>
        </Section>
      </div>
    </div>
  )
}
