import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import ErrorBoundary, { installGlobalErrorHandlers } from './components/ErrorBoundary.tsx'

// ★ 先装全局错误捕获，再挂 React。
//   顺序很重要：如果 React 在渲染期就抛错，`window.onerror` 必须已经就位，
//   否则页面会是一片纯白、什么都不说（这个坑真的踩过 ——
//   见 ErrorBoundary.tsx 顶部的说明）。
installGlobalErrorHandlers()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
)
