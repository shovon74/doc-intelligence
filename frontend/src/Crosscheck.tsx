import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, Doc, errMsg, IN_FLIGHT, Page } from './api';
import { btn, card, Err, input } from './ui';

interface Ref {
  invoice_id: string;
  invoice_number: string | null;
  supplier_name: string | null;
  total: string | null;
}
interface Diff { field: string; a: string | null; b: string | null }
interface Result {
  status: 'match' | 'mismatch';
  summary: {
    invoices_a: number; invoices_b: number; paired: number;
    differences: number; only_in_a: number; only_in_b: number;
  };
  pairs: { a: Ref; b: Ref; differences: Diff[] }[];
  only_in_a: Ref[];
  only_in_b: Ref[];
}
interface Check { id: string; document_a_id: string; document_b_id: string; status: string; result: Result }

const label = (r: Ref) =>
  [r.invoice_number ? `Invoice ${r.invoice_number}` : 'No invoice number', r.supplier_name, r.total && `Total ${r.total}`]
    .filter(Boolean).join(' · ');

function OnlyList({ title, items }: { title: string; items: Ref[] }) {
  return (
    <div className={`${card} space-y-2`}>
      <h3 className="text-navy font-semibold text-sm">{title} ({items.length})</h3>
      {items.length === 0
        ? <p className="text-xs text-muted">None.</p>
        : <ul className="text-sm space-y-1">{items.map((i) => <li key={i.invoice_id}>{label(i)}</li>)}</ul>}
    </div>
  );
}

export default function Crosscheck() {
  const [a, setA] = useState('');
  const [b, setB] = useState('');
  const [mode, setMode] = useState<'upload' | 'existing'>('upload');
  const [ledger, setLedger] = useState<File | null>(null);
  const [step, setStep] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [check, setCheck] = useState<Check | null>(null);

  const docs = useQuery({
    queryKey: ['crosscheck-docs'],
    queryFn: async () => (await api.get<Page<Doc>>('/documents', { params: { page_size: 100 } })).data.items,
  });
  const ready = (docs.data ?? []).filter((d) => d.status === 'completed' || d.status === 'needs_review');
  const name = (id: string) => ready.find((d) => d.id === id)?.original_filename ?? id;

  // Upload the ledger, process it, and wait until its invoices are extracted.
  const prepareLedger = async (file: File): Promise<string> => {
    setStep('Uploading ledger…');
    const form = new FormData();
    form.append('files', file);
    const { data } = await api.post('/documents', form);
    if (data.rejected?.length) throw new Error(data.rejected[0].message);
    const doc = data.documents[0] as { id: string; status: string };
    let status = doc.status;
    if (status === 'uploaded' || status === 'failed') {
      await api.post(`/documents/${doc.id}/reprocess`);
      status = 'queued';
    }
    setStep('Reading ledger rows…');
    const deadline = Date.now() + 5 * 60_000;
    while (IN_FLIGHT.includes(status) && Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 2000));
      const st = (await api.get(`/documents/${doc.id}/status`)).data;
      status = st.status;
      if (status === 'failed') throw new Error(st.error_message ?? 'The ledger could not be processed.');
    }
    if (status === 'failed') throw new Error('The ledger could not be processed.');
    if (IN_FLIGHT.includes(status)) throw new Error('The ledger is still processing. Try again in a moment.');
    return doc.id;
  };

  const compare = async () => {
    setErr(null);
    setCheck(null);
    setBusy(true);
    try {
      const bId = mode === 'upload' && ledger ? await prepareLedger(ledger) : b;
      setStep('Checking…');
      const { data } = await api.post<Check>('/crosscheck', { document_a_id: a, document_b_id: bId });
      setCheck(data);
      docs.refetch();
    } catch (x) {
      setErr(x instanceof Error ? x.message : errMsg(x));
    } finally {
      setBusy(false);
      setStep(null);
    }
  };

  const picker = (value: string, set: (v: string) => void, text: string) => (
    <label className="block text-sm font-medium flex-1">
      {text}
      <select className={`${input} mt-2`} value={value} onChange={(e) => set(e.target.value)}>
        <option value="">Choose a document…</option>
        {ready.map((d) => <option key={d.id} value={d.id}>{d.original_filename}</option>)}
      </select>
    </label>
  );

  const r = check?.result;
  return (
    <div className="space-y-6 max-w-5xl">
      <div className={`${card} p-8 space-y-4`}>
        <h2 className="text-navy text-lg font-semibold">Ledger Check</h2>
        <p className="text-sm text-muted">
          Pick an invoice that is already in the system, then upload the ledger (XLSX or CSV) to check it against.
          Invoices are paired by invoice number and supplier, then every field is compared.
        </p>
        <div className="flex gap-4 flex-wrap items-start">
          {picker(a, setA, 'Invoice in the system (A)')}
          <div className="flex-1 text-sm font-medium">
            <div className="flex items-center gap-3">
              <span>Ledger to check against (B)</span>
              <button type="button" className="ml-auto text-xs text-muted underline"
                      onClick={() => setMode(mode === 'upload' ? 'existing' : 'upload')}>
                {mode === 'upload' ? 'Use an already uploaded document' : 'Upload a ledger file'}
              </button>
            </div>
            {mode === 'upload' ? (
              <input className={`${input} mt-2`} type="file" accept=".xlsx,.xls,.csv"
                     onChange={(e) => setLedger(e.target.files?.[0] ?? null)} />
            ) : (
              <select className={`${input} mt-2`} value={b} onChange={(e) => setB(e.target.value)}>
                <option value="">Choose a document…</option>
                {ready.map((d) => <option key={d.id} value={d.id}>{d.original_filename}</option>)}
              </select>
            )}
            {mode === 'upload' && (
              <p className="text-xs text-muted mt-1 font-normal">XLSX, XLS or CSV export from your CRM or ledger.</p>
            )}
          </div>
        </div>
        <button className={`${btn} px-8`} onClick={compare}
                disabled={!a || busy || (mode === 'upload' ? !ledger : !b || a === b)}>
          {busy ? step ?? 'Working…' : 'Check'}
        </button>
        {mode === 'existing' && a && a === b && <p className="text-xs text-muted">Choose two different documents.</p>}
      </div>

      <Err msg={err} />

      {r && check && (
        <div className="space-y-4">
          <div className={`${card} flex items-center gap-4 flex-wrap`}>
            <span className={`rounded-full px-3 py-1 text-sm font-semibold ${
              r.status === 'match' ? 'text-green-700 bg-green-100' : 'text-red-700 bg-red-100'}`}>
              {r.status === 'match' ? 'Match' : 'Mismatch'}
            </span>
            <span className="text-sm text-muted">
              {r.summary.invoices_a} invoice(s) in A · {r.summary.invoices_b} in B · {r.summary.paired} paired ·{' '}
              {r.summary.differences} difference(s)
            </span>
            <span className="text-xs ml-auto space-x-3">
              <Link className="text-navy underline" to={`/documents/${check.document_a_id}`}>Open A</Link>
              <Link className="text-navy underline" to={`/documents/${check.document_b_id}`}>Open B</Link>
            </span>
          </div>

          {r.pairs.map((p) => (
            <div key={`${p.a.invoice_id}-${p.b.invoice_id}`} className={`${card} space-y-3`}>
              <h3 className="text-navy font-semibold text-sm">{label(p.a)}</h3>
              {p.differences.length === 0 ? (
                <p className="text-sm text-green-700">All fields match.</p>
              ) : (
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-muted text-xs">
                      <th className="py-1 pr-4">Field</th>
                      <th className="py-1 pr-4">{name(check.document_a_id)}</th>
                      <th className="py-1">{name(check.document_b_id)}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {p.differences.map((d) => (
                      <tr key={d.field} className="border-t border-panel">
                        <td className="py-1 pr-4 font-medium">{d.field}</td>
                        <td className="py-1 pr-4">{d.a ?? <span className="text-muted">missing</span>}</td>
                        <td className="py-1">{d.b ?? <span className="text-muted">missing</span>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          ))}

          <div className="grid gap-4 sm:grid-cols-2">
            <OnlyList title="Only in A (not found in B)" items={r.only_in_a} />
            <OnlyList title="Only in B (other rows, not checked)" items={r.only_in_b} />
          </div>
        </div>
      )}
    </div>
  );
}
