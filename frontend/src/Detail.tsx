import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, DocDetail, errMsg, IN_FLIGHT, Invoice } from './api';
import { Badge, btn, btn2, card, Err, input } from './ui';

type Field = 'invoice_number' | 'invoice_date' | 'supplier_name' | 'customer_name' | 'subtotal' | 'tax' | 'discount' | 'total' | 'currency';
const FIELDS: [Field, string, string][] = [
  ['invoice_number', 'Invoice number', 'text'], ['invoice_date', 'Date', 'date'],
  ['supplier_name', 'Supplier', 'text'], ['customer_name', 'Customer', 'text'],
  ['subtotal', 'Subtotal', 'text'], ['tax', 'Tax', 'text'], ['discount', 'Discount', 'text'],
  ['total', 'Total', 'text'], ['currency', 'Currency', 'text'],
];
const ORDER = { error: 0, warning: 1, info: 2 };

function Preview({ doc }: { doc: DocDetail }) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    let made: string | null = null;
    api.get(`/documents/${doc.id}/file`, { responseType: 'blob' })
      .then((r) => { made = URL.createObjectURL(r.data); setUrl(made); })
      .catch(() => setUrl(null));
    return () => { if (made) URL.revokeObjectURL(made); };
  }, [doc.id]);

  if (!url) return <p className="text-muted text-sm">Loading preview…</p>;
  if (doc.mime_type === 'application/pdf') return <iframe src={url} title="Original" className="w-full h-[75vh] rounded-lg" />;
  if (doc.mime_type.startsWith('image/') && doc.file_ext !== 'tiff') return <img src={url} alt="Original" className="w-full rounded-lg" />;
  return <a className="text-navy underline" href={url} download={doc.original_filename}>Download {doc.original_filename}</a>;
}

function InvoiceCard({ inv, onChanged }: { inv: Invoice; onChanged: () => void }) {
  const [form, setForm] = useState<Record<string, string>>(
    Object.fromEntries(FIELDS.map(([k]) => [k, inv[k] ?? ''])));
  const [err, setErr] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: () => {
      const body: Record<string, string | null> = {};
      FIELDS.forEach(([k]) => { if (form[k] !== (inv[k] ?? '')) body[k] = form[k].trim() === '' ? null : form[k]; });
      return api.patch(`/invoices/${inv.id}`, body);
    },
    onSuccess: () => { setErr(null); onChanged(); },
    onError: (x) => setErr(errMsg(x)),
  });
  const resolve = useMutation({
    mutationFn: (id: string) => api.post(`/invoices/${inv.id}/resolve-issue/${id}`),
    onSuccess: onChanged,
    onError: (x) => setErr(errMsg(x)),
  });
  const dirty = FIELDS.some(([k]) => form[k] !== (inv[k] ?? ''));
  const issues = [...inv.issues].sort((a, b) => ORDER[a.severity] - ORDER[b.severity]);

  return (
    <div className={`${card} space-y-4`}>
      <div className="flex items-center gap-2">
        <h3 className="text-navy font-semibold">Invoice {inv.invoice_number ?? '(no number)'}</h3>
        {inv.needs_review && <Badge text="needs_review" />}
        {inv.is_corrected && <Badge text="corrected" />}
      </div>

      <div className="grid grid-cols-2 gap-3">
        {FIELDS.map(([k, label, type]) => (
          <label key={k} className="text-xs text-muted">
            {label}
            <input className={`${input} mt-1`} type={type} value={form[k]}
                   onChange={(e) => setForm({ ...form, [k]: e.target.value })} />
          </label>
        ))}
      </div>
      <Err msg={err} />
      <button className={btn} disabled={!dirty || save.isPending} onClick={() => save.mutate()}>
        {save.isPending ? 'Saving…' : 'Save and re-validate'}
      </button>

      {issues.length > 0 && (
        <div className="space-y-2">
          <h4 className="font-medium">Issues</h4>
          {issues.map((i) => (
            <div key={i.id} className={`flex items-start gap-2 text-sm ${i.resolved ? 'opacity-50' : ''}`}>
              <Badge text={i.severity} />
              <span className="flex-1">{i.message}</span>
              {!i.resolved && <button className="text-navy underline" onClick={() => resolve.mutate(i.id)}>Resolve</button>}
              {i.resolved && <span className="text-muted">resolved</span>}
            </div>
          ))}
        </div>
      )}
      {inv.review_reason?.some((r) => r.startsWith('UNGROUNDED:')) && (
        <p className="text-sm text-amber">
          Not found in the document text: {inv.review_reason.filter((r) => r.startsWith('UNGROUNDED:'))
            .map((r) => r.slice(11)).join(', ')}. Check and correct these values.
        </p>
      )}

      {inv.line_items.length > 0 && (
        <table className="w-full text-sm">
          <thead className="text-left text-muted"><tr><th>Description</th><th>Qty</th><th>Unit price</th><th>Total</th></tr></thead>
          <tbody>
            {inv.line_items.map((l) => (
              <tr key={l.position} className="border-t border-panel">
                <td className="py-1">{l.description}</td><td>{l.quantity}</td><td>{l.unit_price}</td><td>{l.line_total}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export default function Detail() {
  const { id } = useParams();
  const qc = useQueryClient();
  const { data: doc, isError, error } = useQuery({
    queryKey: ['document', id],
    queryFn: async () => (await api.get<DocDetail>(`/documents/${id}`)).data,
    refetchInterval: (q) => (q.state.data && IN_FLIGHT.includes(q.state.data.status) ? 3000 : false),
  });
  const refresh = () => qc.invalidateQueries({ queryKey: ['document', id] });
  const start = useMutation({
    mutationFn: () => api.post(`/documents/${id}/reprocess`),
    onSuccess: refresh,
  });

  if (isError) return <Err msg={errMsg(error)} />;
  if (!doc) return <p>Loading…</p>;
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <Link to="/" className="text-navy underline">← Documents</Link>
        <h1 className="text-navy text-xl font-semibold">{doc.original_filename}</h1>
        <Badge text={doc.status} />
        {IN_FLIGHT.includes(doc.status) && <span className="text-sm text-muted">{Math.round(doc.progress_percent)}%</span>}
      </div>
      <Err msg={doc.error_message} />
      {doc.status === 'uploaded' && (
        <div className={`${card} flex items-center gap-3`}>
          <p className="flex-1 text-sm">Not processed yet, so there is no extracted data. Start processing to extract it
            (the document is sent to the extraction model).</p>
          <button className={btn2} disabled={start.isPending}
                  onClick={() => { if (window.confirm('Start processing this document?')) start.mutate(); }}>Start processing</button>
          <Link to="/" className="border border-gray-300 rounded-full px-4 py-2 text-sm">Cancel</Link>
        </div>
      )}
      <Err msg={start.isError ? errMsg(start.error) : null} />
      <div className="grid md:grid-cols-2 gap-4 items-start">
        <div className={card}><Preview doc={doc} /></div>
        <div className="space-y-4">
          {doc.invoices.map((inv) => (
            <InvoiceCard key={`${inv.id}-${inv.corrections.length}`} inv={inv} onChanged={refresh} />
          ))}
          {doc.invoices.length === 0 && !IN_FLIGHT.includes(doc.status) && doc.status !== 'uploaded' && !doc.error_message &&
            <p className="text-muted">No invoices were extracted.</p>}
          <div className={card}>
            <h4 className="font-medium mb-2">Processing history</h4>
            {doc.events.map((e, i) => (
              <p key={i} className="text-sm text-muted">
                {new Date(e.created_at).toLocaleTimeString()} · {e.stage} · {e.status}{e.message ? ` — ${e.message}` : ''}
              </p>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
