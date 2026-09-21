import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import PublicApp from './PublicApp'
import './index.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
        {import.meta.env.VITE_PUBLIC_BETA === 'true' ? <PublicApp /> : <App />}
    </React.StrictMode>,
)
