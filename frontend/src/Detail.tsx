import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, DocDetail, errMsg, IN_FLIGHT, Invoice, Issue, Line } from './api';
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
  if (doc.mime_type === 'application/pdf') return (
      <div className="space-y-2">
        <iframe src={`${url}#navpanes=0&view=FitH`} title="Original" className="w-full h-[85vh] rounded-lg" />
        <a className="text-navy underline text-sm" href={url} target="_blank" rel="noreferrer">Open in new tab</a>
      </div>
    );
  if (doc.mime_type.startsWith('image/') && doc.file_ext !== 'tiff') return <img src={url} alt="Original" className="w-full rounded-lg" />;
  return <a className="text-navy underline" href={url} download={doc.original_filename}>Download {doc.original_filename}</a>;
}

/** Index of the line an issue or flag belongs to, e.g. "line_items[2].quantity" -> 2. */
const lineIndex = (field: string | null): number | null => {
  const m = field?.match(/^line_items\[(\d+)\]/);
  return m ? Number(m[1]) : null;
};
const blankToNull = (v: string) => (v.trim() === '' ? null : v.trim());

/** One editable line. Warnings for the line show right under it. */
function LineRow({ invoiceId, line, issues, notFound, onDone, onCancel, onAccept, onConfirm }: {
  invoiceId: string; line?: Line; issues: Issue[]; notFound: string[];
  onDone: () => void; onCancel?: () => void; onAccept: (id: string) => void; onConfirm?: () => void;
}) {
  const init = {
    description: line?.description ?? '', quantity: line?.quantity ?? '',
    unit_price: line?.unit_price ?? '', line_total: line?.line_total ?? '',
  };
  const [d, setD] = useState(init);
  const [err, setErr] = useState<string | null>(null);
  const keys = Object.keys(init) as (keyof typeof init)[];
  const dirty = keys.some((k) => d[k] !== init[k]);
  const hasValue = keys.some((k) => d[k].trim() !== '');

  const save = useMutation({
    mutationFn: () => {
      const body: Record<string, string | null> = {};
      keys.forEach((k) => { if (!line || d[k] !== init[k]) body[k] = blankToNull(d[k]); });
      return line
        ? api.patch(`/invoices/${invoiceId}/line-items/${line.id}`, body)
        : api.post(`/invoices/${invoiceId}/line-items`, body);
    },
    onSuccess: () => { setErr(null); onDone(); },
    onError: (x) => setErr(errMsg(x)),
  });
  const remove = useMutation({
    mutationFn: () => api.delete(`/invoices/${invoiceId}/line-items/${line!.id}`),
    onSuccess: onDone,
    onError: (x) => setErr(errMsg(x)),
  });

  // Suggestions for a "quantity x price != total" warning. They fill the boxes; you still press Save.
  const q = Number(d.quantity), t = Number(d.line_total);
  const price = d.quantity.trim() && q !== 0 && d.line_total.trim() && Number.isFinite(t / q) ? (t / q).toFixed(2) : null;
  const expected = Number(d.quantity) * Number(d.unit_price);
  const canSetTotal = d.quantity.trim() !== '' && d.unit_price.trim() !== '' && Number.isFinite(expected);
  const flagged = issues.length > 0 || notFound.length > 0;
  const cell = `${input} ${flagged ? 'border-amber' : ''}`;

  return (
    <div className={`rounded-lg p-2 space-y-1 ${flagged ? 'bg-orange-50' : ''}`}>
      <div className="grid grid-cols-[1fr_4.5rem_6rem_6rem_auto] gap-2 items-center">
        <input className={cell} placeholder="Description" value={d.description}
               onChange={(e) => setD({ ...d, description: e.target.value })} />
        <input className={cell} placeholder="Qty" inputMode="decimal" value={d.quantity}
               onChange={(e) => setD({ ...d, quantity: e.target.value })} />
        <input className={cell} placeholder="Unit price" inputMode="decimal" value={d.unit_price}
               onChange={(e) => setD({ ...d, unit_price: e.target.value })} />
        <input className={cell} placeholder="Total" inputMode="decimal" value={d.line_total}
               onChange={(e) => setD({ ...d, line_total: e.target.value })} />
        <div className="flex gap-2 text-sm whitespace-nowrap">
          <button className="text-navy underline disabled:opacity-40 disabled:no-underline"
                  disabled={!dirty || !hasValue || save.isPending} onClick={() => save.mutate()}>
            {save.isPending ? 'Saving…' : line ? 'Save' : 'Add'}
          </button>
          {line
            ? <button className="text-red-700 underline" disabled={remove.isPending}
                      onClick={() => window.confirm('Remove this line?') && remove.mutate()}>Delete</button>
            : <button className="text-muted underline" onClick={onCancel}>Cancel</button>}
        </div>
      </div>
      {issues.map((i) => (
        <p key={i.id} className="text-xs text-amber flex gap-x-3 flex-wrap">
          <span>{i.message}</span>
          {i.code === 'LINE_MATH_MISMATCH' && price && (
            <button className="underline" onClick={() => setD({ ...d, unit_price: price })}>Set unit price to {price}</button>
          )}
          {i.code === 'LINE_MATH_MISMATCH' && canSetTotal && (
            <button className="underline" onClick={() => setD({ ...d, line_total: expected.toFixed(2) })}>Set total to {expected.toFixed(2)}</button>
          )}
          <button className="text-muted underline" onClick={() => onAccept(i.id)}>Accept as is</button>
        </p>
      ))}
      {notFound.length > 0 && (
        <p className="text-xs text-amber flex gap-x-3 flex-wrap">
          <span>Not found in the document text: {notFound.join(', ')}. Check this line.</span>
          {onConfirm && <button className="underline" onClick={onConfirm}>Mark as correct</button>}
        </p>
      )}
      <Err msg={err} />
    </div>
  );
}

function LineTable({ inv, onChanged, onAccept, onConfirm }: {
  inv: Invoice; onChanged: () => void; onAccept: (id: string) => void; onConfirm: (fields: string[]) => void;
}) {
  const [adding, setAdding] = useState(false);
  return (
    <div className="space-y-2">
      <div className="flex items-center">
        <h4 className="font-medium">Line items</h4>
        {!adding && <button className="ml-auto text-sm text-navy underline" onClick={() => setAdding(true)}>+ Add line</button>}
      </div>
      {inv.line_items.length === 0 && !adding && (
        <p className="text-xs text-muted">No line items. Add one if the document has lines that were not picked up.</p>
      )}
      {inv.line_items.map((l) => {
        const flags = (inv.review_reason ?? [])
          .filter((r) => r.startsWith('UNGROUNDED:line_items[') && lineIndex(r.slice(11)) === l.position)
          .map((r) => r.slice(11).replace(/^line_items\[\d+\]\.?/, '') || 'line');
        return (
          <LineRow key={`${l.id}:${l.description}:${l.quantity}:${l.unit_price}:${l.line_total}`}
                   invoiceId={inv.id} line={l} notFound={flags} onAccept={onAccept} onDone={onChanged}
                   onConfirm={() => onConfirm([`line_items[${l.position}]`])}
                   issues={inv.issues.filter((i) => !i.resolved && lineIndex(i.field) === l.position)} />
        );
      })}
      {adding && (
        <LineRow invoiceId={inv.id} issues={[]} notFound={[]} onAccept={onAccept}
                 onDone={() => { setAdding(false); onChanged(); }} onCancel={() => setAdding(false)} />
      )}
    </div>
  );
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
  const confirm = useMutation({
    mutationFn: (fields: string[]) => api.post(`/invoices/${inv.id}/confirm`, { fields }),
    onSuccess: onChanged,
    onError: (x) => setErr(errMsg(x)),
  });
  const dirty = FIELDS.some(([k]) => form[k] !== (inv[k] ?? ''));
  const headerUngrounded = (inv.review_reason ?? [])
    .filter((r) => r.startsWith('UNGROUNDED:') && !r.startsWith('UNGROUNDED:line_items'))
    .map((r) => r.slice(11));
  // The Issues panel is for invoice-level fields; line problems show on their own row.
  const issues = inv.issues.filter((i) => lineIndex(i.field) === null).sort((a, b) => ORDER[a.severity] - ORDER[b.severity]);

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
        {save.isPending ? 'Applying…' : 'Apply corrections'}
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
      {headerUngrounded.length > 0 && (
        <p className="text-sm text-amber flex gap-x-3 flex-wrap">
          <span>Not found in the document text: {headerUngrounded.join(', ')}. Check and correct these values.</span>
          <button className="underline" disabled={confirm.isPending}
                  onClick={() => confirm.mutate(headerUngrounded)}>Mark as correct</button>
        </p>
      )}

      <LineTable inv={inv} onChanged={onChanged} onAccept={(id) => resolve.mutate(id)}
                 onConfirm={(fields) => confirm.mutate(fields)} />
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
  const navigate = useNavigate();
  const remove = useMutation({
    mutationFn: () => api.delete(`/documents/${id}`),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['documents'] }); navigate('/'); },
  });
  const reprocess = useMutation({
    mutationFn: () => api.post(`/documents/${id}/reprocess`),
    onSuccess: refresh,
  });
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
      {!IN_FLIGHT.includes(doc.status) && (
        <div className="flex gap-2">
          {doc.status !== 'uploaded' && <button className={btn} disabled={reprocess.isPending}
                  onClick={() => { if (window.confirm('Reprocess this document? Extracted data will be replaced.')) reprocess.mutate(); }}>Reprocess</button>}
          <button className={btn} disabled={remove.isPending}
                  onClick={() => { if (window.confirm(`Delete ${doc.original_filename} and its extracted data?`)) remove.mutate(); }}>Delete</button>
        </div>
      )}
      <Err msg={start.isError ? errMsg(start.error) : reprocess.isError ? errMsg(reprocess.error) : remove.isError ? errMsg(remove.error) : null} />
      <div className="grid lg:grid-cols-[1.2fr_1fr] gap-4 items-start">
        <div className={`${card} lg:sticky lg:top-2`}><Preview doc={doc} /></div>
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
