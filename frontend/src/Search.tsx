import { FormEvent, ReactNode, useState } from 'react';
import { Link } from 'react-router-dom';
import { isAxiosError } from 'axios';
import { api, errMsg } from './api';
import { btn, card, Err, input } from './ui';

interface Hit {
  chunk_id: string;
  document_id: string;
  filename: string;
  invoice_id: string | null;
  invoice_number: string | null;
  supplier_name: string | null;
  total: string | null;
  kind: string;
  snippet: string;
  score: number;
}

// Snippets mark matches as [[word]]; render them as bold text (never as HTML).
function Snippet({ text }: { text: string }) {
  return <p className="text-sm">{text.split(/\[\[|\]\]/).map((p, i) => (i % 2 ? <b key={i}>{p}</b> : p))}</p>;
}

// One result per invoice (or per document when a hit has no invoice). Hits arrive best-first,
// so the first hit of each group is its best match.
function groupHits(hits: Hit[]): { hit: Hit; matches: number }[] {
  const groups = new Map<string, { hit: Hit; matches: number }>();
  for (const h of hits) {
    const key = h.invoice_id ?? `doc-${h.document_id}`;
    const g = groups.get(key);
    if (g) g.matches += 1;
    else groups.set(key, { hit: h, matches: 1 });
  }
  return [...groups.values()];
}

function Field({ label, hint, children }: { label: string; hint: string; children: ReactNode }) {
  return (
    <label className="block text-sm font-medium leading-6">
      {label}
      <span className="mt-2 block">{children}</span>
      <span className="block text-xs font-normal text-muted mt-2 leading-5">{hint}</span>
    </label>
  );
}

function Chip({ text, onRemove }: { text: string; onRemove: () => void }) {
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-panel text-navy text-xs px-3 py-1.5">
      {text}
      <button type="button" aria-label={`Remove filter ${text}`} className="font-bold" onClick={onRemove}>×</button>
    </span>
  );
}

type Opts = { semantic: boolean; supplier: string; from: string; to: string; review: boolean };

export default function Search() {
  const [q, setQ] = useState('');
  const [semantic, setSemantic] = useState(false);
  const [supplier, setSupplier] = useState('');
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [review, setReview] = useState(false);
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const search = async (o: Opts) => {
    setErr(null);
    const filters = { supplier: o.supplier || undefined, date_from: o.from || undefined,
                      date_to: o.to || undefined, needs_review: o.review || undefined };
    try {
      const { data } = o.semantic
        ? await api.post('/search/semantic', { query: q, ...filters, limit: 50 })
        : await api.get('/search', { params: { q, ...filters, page_size: 50 } });
      setHits(o.semantic ? data : data.items);
    } catch (x) {
      setHits(null);
      setErr(o.semantic && isAxiosError(x) && x.response?.status === 503
        ? 'Smart search is unavailable right now. Normal search still works: untick "Smart search" and try again.'
        : errMsg(x));
    }
  };
  const cur = (): Opts => ({ semantic, supplier, from, to, review });
  const run = (e: FormEvent) => { e.preventDefault(); search(cur()); };
  // Removing a chip or clearing filters re-runs the search once a search has been made.
  const change = (patch: Partial<Opts>) => {
    const next = { ...cur(), ...patch };
    setSupplier(next.supplier); setFrom(next.from); setTo(next.to); setReview(next.review);
    if (hits !== null && q.trim()) search(next);
  };

  const chips: { text: string; remove: () => void }[] = [];
  if (supplier) chips.push({ text: `Supplier: ${supplier}`, remove: () => change({ supplier: '' }) });
  if (from) chips.push({ text: `From: ${from}`, remove: () => change({ from: '' }) });
  if (to) chips.push({ text: `To: ${to}`, remove: () => change({ to: '' }) });
  if (review) chips.push({ text: 'Needs review only', remove: () => change({ review: false }) });
  const results = hits ? groupHits(hits) : null;

  return (
    <div className="space-y-6 max-w-5xl">
      <form onSubmit={run} className={`${card} p-8 space-y-8`}>
        <div>
          <div className="flex gap-4">
            <input className={`${input} py-3`} placeholder="Invoice number, company name or any word" value={q}
                   onChange={(e) => setQ(e.target.value)} required aria-label="Search" />
            <button className={`${btn} px-8`}>Search</button>
          </div>
          <p className="text-xs text-muted mt-2">
            Type an invoice number, a company name or any word that appears on the invoice.
          </p>
        </div>

        <label className="flex items-start gap-3 text-sm border-t border-panel pt-6">
          <input type="checkbox" className="mt-1" checked={semantic} onChange={(e) => setSemantic(e.target.checked)} />
          <span>
            <span className="font-medium">Smart search</span>
            <span className="block text-xs text-muted">
              Finds related wording, not just exact words. Example: &quot;electricity&quot; also finds &quot;power bill&quot;.
            </span>
          </span>
        </label>

        <div className="grid gap-x-6 gap-y-6 sm:grid-cols-2 lg:grid-cols-3 border-t border-panel pt-6">
          <div className="space-y-6">
            <Field label="Supplier name" hint="Only invoices from this company. Part of the name is fine.">
              <input className={input} placeholder="e.g. Meridian" value={supplier} onChange={(e) => setSupplier(e.target.value)} />
            </Field>
            <label className="flex items-start gap-3 text-sm">
              <input type="checkbox" className="mt-1" checked={review} onChange={(e) => setReview(e.target.checked)} />
              <span>
                <span className="font-medium">Only invoices needing review</span>
                <span className="block text-xs text-muted">Shows invoices with warnings that a person should check.</span>
              </span>
            </label>
          </div>
          <Field label="Invoice date from" hint="Date printed on the invoice, not the upload date.">
            <input className={input} type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
          </Field>
          <Field label="Invoice date to" hint="Leave empty for no end date.">
            <input className={input} type="date" value={to} onChange={(e) => setTo(e.target.value)} />
          </Field>
        </div>
        <p className="text-xs text-muted -mt-2">Using a filter hides results that are not tied to a single invoice.</p>

        {chips.length > 0 && (
          <div className="flex flex-wrap items-center gap-3">
            {chips.map((c) => <Chip key={c.text} text={c.text} onRemove={c.remove} />)}
            <button type="button" className="text-navy underline text-xs"
                    onClick={() => change({ supplier: '', from: '', to: '', review: false })}>Clear filters</button>
          </div>
        )}
      </form>

      <Err msg={err} />
      {results && results.length === 0 && (
        <p className="text-muted">No invoices found. Try fewer words or clear the filters.</p>
      )}
      {results?.map(({ hit: h, matches }) => (
        <div key={h.invoice_id ?? `doc-${h.document_id}`} className={`${card} p-6 space-y-2`}>
          <Link className="text-navy underline font-medium" to={`/documents/${h.document_id}`}>
            {h.filename}{h.invoice_number ? ` · Invoice ${h.invoice_number}` : ''}
          </Link>
          {h.supplier_name && <p className="text-xs text-muted">{h.supplier_name}{h.total ? ` · Total ${h.total}` : ''}</p>}
          <div className="pt-1 leading-6"><Snippet text={h.snippet} /></div>
          {matches > 1 && <p className="text-xs text-muted">{matches} matching parts in this document</p>}
        </div>
      ))}
    </div>
  );
}
