import React, { useEffect, useState } from 'react';
import axios from 'axios';

interface HealthData {
  status: string;
  database: string;
  redis: string;
  tesseract: string;
  llm: {
    status: string;
    active_provider: string;
    configured_providers: string[];
    fallback_enabled: boolean;
  };
}

export default function App() {
  const [health, setHealth] = useState<HealthData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    axios
      .get('/api/v1/health')
      .then((res) => {
        setHealth(res.data);
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message || 'Failed to connect to backend API');
        setLoading(false);
      });
  }, []);

  return (
    <div className="min-h-screen bg-slate-900 text-white flex flex-col items-center justify-center p-6">
      <div className="max-w-xl w-full bg-slate-800 rounded-xl shadow-2xl p-8 border border-slate-700">
        <div className="flex items-center space-x-3 mb-6">
          <div className="w-4 h-4 rounded-full bg-emerald-400 animate-pulse" />
          <h1 className="text-2xl font-bold tracking-tight">DocIntel Platform</h1>
        </div>

        <p className="text-slate-300 mb-6 text-sm">
          AI-Powered Document Intelligence System for high-volume invoice processing, OCR,
          validation, cross-check verification, and semantic search.
        </p>

        <div className="bg-slate-950 rounded-lg p-5 border border-slate-800 text-xs font-mono space-y-3">
          <div className="text-slate-400 border-b border-slate-800 pb-2 font-semibold">
            System & Component Health Status:
          </div>

          {loading && <div className="text-yellow-400">Connecting to API service...</div>}
          {error && <div className="text-rose-400">Connection error: {error}</div>}

          {health && (
            <div className="space-y-2">
              <div className="flex justify-between">
                <span className="text-slate-400">API Status:</span>
                <span className="text-emerald-400 font-bold">{health.status}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-400">PostgreSQL (pgvector):</span>
                <span className={health.database === 'connected' ? 'text-emerald-400' : 'text-rose-400'}>
                  {health.database}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-400">Redis Broker:</span>
                <span className={health.redis === 'connected' ? 'text-emerald-400' : 'text-rose-400'}>
                  {health.redis}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-400">Tesseract OCR:</span>
                <span className="text-sky-300">{health.tesseract}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-400">LLM Provider Status:</span>
                <span className="text-amber-300">
                  {health.llm.status} (configured: {health.llm.configured_providers.join(', ') || 'none'})
                </span>
              </div>
            </div>
          )}
        </div>

        <div className="mt-6 flex justify-between items-center text-xs text-slate-500">
          <span>Phase 0 Scaffold Active</span>
          <a
            href="/docs"
            target="_blank"
            rel="noreferrer"
            className="text-indigo-400 hover:text-indigo-300 underline font-semibold"
          >
            OpenAPI Documentation &rarr;
          </a>
        </div>
      </div>
    </div>
  );
}
