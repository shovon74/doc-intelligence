import { FormEvent, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, errMsg, LLMSettings } from './api';
import { btn, card, Err, input } from './ui';

type Provider = 'gemini' | 'openai';

function Form({ cur, onDone, onCancel }: { cur: LLMSettings; onDone: () => void; onCancel?: () => void }) {
  const qc = useQueryClient();
  const [provider, setProvider] = useState<Provider>(cur.provider ?? 'gemini');
  const [model, setModel] = useState(cur.models[cur.provider ?? 'gemini'] ?? cur.default_models.gemini);
  const [key, setKey] = useState('');
  const [err, setErr] = useState<string | null>(null);
  const hasKey = cur.has_key[provider];

  const pick = (p: Provider) => {
    setProvider(p);
    setModel(cur.models[p] ?? cur.default_models[p]);
    setKey('');
  };
  const save = useMutation({
    mutationFn: async () => (await api.post('/settings/llm', { provider, model, api_key: key || null })).data,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['llm-settings'] });
      qc.invalidateQueries({ queryKey: ['providers'] });
      onDone();
    },
    onError: (x) => setErr(errMsg(x)),
  });
  const submit = (e: FormEvent) => { e.preventDefault(); setErr(null); save.mutate(); };

  return (
    <form onSubmit={submit} className={`${card} w-full max-w-md space-y-4`}>
      <h2 className="text-navy text-lg font-semibold">Connect an AI model</h2>
      <p className="text-sm text-muted">
        Choose a provider and paste your API key. It is saved once on the server and never shown again.
      </p>
      <div className="flex gap-2">
        {(['gemini', 'openai'] as Provider[]).map((p) => (
          <button type="button" key={p} onClick={() => pick(p)}
                  className={`flex-1 rounded-full px-4 py-2 text-sm font-medium border
                    ${provider === p ? 'bg-navy text-white border-navy' : 'bg-white text-navy border-gray-300'}`}>
            {p === 'gemini' ? 'Gemini' : 'OpenAI'}
          </button>
        ))}
      </div>
      <label className="block text-sm text-navy">Model name
        <input className={`${input} mt-1`} value={model} onChange={(e) => setModel(e.target.value)} required />
      </label>
      <label className="block text-sm text-navy">API key
        <input className={`${input} mt-1`} type="password" autoComplete="off" value={key}
               placeholder={hasKey ? 'Saved. Leave empty to keep it' : 'Paste your key'}
               onChange={(e) => setKey(e.target.value)} required={!hasKey} />
      </label>
      <p className="text-xs text-muted">
        Search embeddings use the same provider. If documents are already indexed, switching provider
        means reprocessing them.
      </p>
      <Err msg={err} />
      <div className="flex gap-2">
        <button className={`${btn} flex-1`} disabled={save.isPending}>
          {save.isPending ? 'Checking key…' : 'Save and continue'}
        </button>
        {onCancel && <button type="button" className="text-sm text-muted underline px-3" onClick={onCancel}>Cancel</button>}
      </div>
    </form>
  );
}

/** Blocks the app with a setup prompt until an LLM key is configured. */
export function LLMGate() {
  const q = useQuery({ queryKey: ['llm-settings'], queryFn: async () => (await api.get<LLMSettings>('/settings/llm')).data });
  if (!q.data || q.data.configured) return null;
  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4">
      <Form cur={q.data} onDone={() => undefined} />
    </div>
  );
}

/** Header button to change provider, model or key later. */
export function LLMSettingsButton() {
  const [open, setOpen] = useState(false);
  const q = useQuery({ queryKey: ['llm-settings'], queryFn: async () => (await api.get<LLMSettings>('/settings/llm')).data });
  return (
    <>
      <button onClick={() => setOpen(true)} disabled={!q.data} className="text-sm text-muted underline">AI settings</button>
      {open && q.data && (
        <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4">
          <Form cur={q.data} onDone={() => setOpen(false)} onCancel={() => setOpen(false)} />
        </div>
      )}
    </>
  );
}
