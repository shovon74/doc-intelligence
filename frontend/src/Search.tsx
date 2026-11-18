import { FormEvent, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, errMsg } from './api';
import { btn, card, Err, input } from './ui';

interface Hit {
  chunk_id: string;
  document_id: string;
  filename: string;
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

export default function Search() {
  const [q, setQ] = useState('');
  const [semantic, setSemantic] = useState(false);
  const [supplier, setSupplier] = useState('');
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [review, setReview] = useState(false);
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const run = async (e: FormEvent) => {
    e.preventDefault();
    setErr(null);
    const filters = { supplier: supplier || undefined, date_from: from || undefined,
                      date_to: to || undefined, needs_review: review || undefined };
    try {
      const { data } = semantic
        ? await api.post('/search/semantic', { query: q, ...filters })
        : await api.get('/search', { params: { q, ...filters, page_size: 50 } });
      setHits(semantic ? data : data.items);
    } catch (x) {
      setHits(null);
      setErr(errMsg(x));
    }
  };

  return (
    <div className="space-y-4">
      <form onSubmit={run} className={`${card} space-y-3`}>
        <div className="flex gap-3">
          <input className={input} placeholder="Search invoices and documents" value={q}
                 onChange={(e) => setQ(e.target.value)} required />
          <button className={btn}>Search</button>
        </div>
        <div className="flex flex-wrap items-center gap-3 text-sm">
          <label className="flex items-center gap-1">
            <input type="checkbox" checked={semantic} onChange={(e) => setSemantic(e.target.checked)} /> Semantic (by meaning)
          </label>
          <input className={`${input} w-40`} placeholder="Supplier" value={supplier} onChange={(e) => setSupplier(e.target.value)} />
          <input className={`${input} w-40`} type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
          <input className={`${input} w-40`} type="date" value={to} onChange={(e) => setTo(e.target.value)} />
          <label className="flex items-center gap-1">
            <input type="checkbox" checked={review} onChange={(e) => setReview(e.target.checked)} /> Needs review
          </label>
        </div>
      </form>
      <Err msg={err} />
      {hits?.length === 0 && <p className="text-muted">No results.</p>}
      {hits?.map((h) => (
        <div key={h.chunk_id} className={`${card} space-y-1`}>
          <Link className="text-navy underline font-medium" to={`/documents/${h.document_id}`}>
            {h.filename}{h.invoice_number ? ` · ${h.invoice_number}` : ''}
          </Link>
          {h.supplier_name && <p className="text-xs text-muted">{h.supplier_name}{h.total ? ` · ${h.total}` : ''}</p>}
          <Snippet text={h.snippet} />
        </div>
      ))}
    </div>
  );
}
