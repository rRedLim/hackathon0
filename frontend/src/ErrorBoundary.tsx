import { Component, type ReactNode } from 'react'
import { errorMessage } from './api'

/** Ловит ошибки рендера страницы, чтобы падение одной вкладки не оставляло пустой экран. */
export class ErrorBoundary extends Component<{ children: ReactNode }, { error: unknown }> {
  state: { error: unknown } = { error: null }

  static getDerivedStateFromError(error: unknown) {
    return { error }
  }

  render() {
    if (this.state.error === null) return this.props.children
    return (
      <div className="page">
        <div className="state-box state-error">
          <div>
            <b>Ошибка отображения страницы.</b> {errorMessage(this.state.error)}
            <div className="small">Остальные вкладки работают — переключитесь на другую вкладку или попробуйте ещё раз.</div>
          </div>
          <button className="btn" onClick={() => this.setState({ error: null })}>
            Повторить
          </button>
        </div>
      </div>
    )
  }
}
