import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useDropzone } from 'react-dropzone';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, Doc, errMsg, IN_FLIGHT, Page } from './api';
import { Badge, btn, btn2, card, Err, input } from './ui';

const STATUSES = ['queued', 'processing', 'extracting', 'validating', 'completed', 'needs_review', 'failed'];

function Upload() {
  const qc = useQueryClient();
  const [provider, setProvider] = useState('');
  const [notes, setNotes] = useState<string[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const providers = useQuery({ queryKey: ['providers'], queryFn: async () => (await api.get('/providers')).data });

  const send = async (files: File[]) => {
    setErr(null);
    const form = new FormData();
    files.forEach((f) => form.append('files', f));
    if (provider) form.append('llm_provider', provider);
    try {
      const { data } = await api.post('/documents', form);
      setNotes([
        ...data.documents.map((d: { filename: string; duplicate: boolean }) =>
          `${d.filename}: ${d.duplicate ? 'already uploaded' : 'queued'}`),
        ...data.rejected.map((r: { filename: string; message: string }) => `${r.filename}: ${r.message}`),
      ]);
      qc.invalidateQueries({ queryKey: ['documents'] });
    } catch (x) {
      setErr(errMsg(x));
    }
  };
  const { getRootProps, getInputProps, isDragActive } = useDropzone({ onDrop: send });

  return (
    <section className={`${card} space-y-3`}>
      <div className="flex items-center gap-3">
        <h2 className="text-navy font-semibold">Upload documents</h2>
        <select className={`${input} w-auto ml-auto`} value={provider} onChange={(e) => setProvider(e.target.value)}>
          <option value="">Default provider</option>
          {(providers.data?.llm.options ?? []).map((p: string) => <option key={p}>{p}</option>)}
        </select>
      </div>
      <div {...getRootProps()} className={`border-2 border-dashed rounded-2xl p-8 text-center text-muted cursor-pointer
        ${isDragActive ? 'border-amber bg-orange-50' : 'border-gray-300 bg-panel'}`}>
        <input {...getInputProps()} />
        Drop PDF, image, XLSX/XLS or CSV files here, or click to choose
      </div>
      <Err msg={err} />
      {notes.map((n, i) => <p key={i} className="text-sm text-muted">{n}</p>)}
    </section>
  );
}

export default function Documents() {
  const qc = useQueryClient();
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState('');
  const [review, setReview] = useState(false);
  const [q, setQ] = useState('');
  const params = { page, status: status || undefined, needs_review: review || undefined, q: q || undefined };

  const list = useQuery({
    queryKey: ['documents', params],
    queryFn: async () => (await api.get<Page<Doc>>('/documents', { params })).data,
    refetchInterval: (query) =>
      query.state.data?.items.some((d) => IN_FLIGHT.includes(d.status)) ? 3000 : false,
  });
  const refresh = () => qc.invalidateQueries({ queryKey: ['documents'] });
  const reprocess = useMutation({ mutationFn: (id: string) => api.post(`/documents/${id}/reprocess`), onSuccess: refresh });
  const reprocessAll = useMutation({ mutationFn: () => api.post('/documents/reprocess-failed'), onSuccess: refresh });

  const data = list.data;
  const pages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;

  return (
    <div className="space-y-4">
      <Upload />
      <section className={`${card} space-y-3`}>
        <div className="flex flex-wrap items-center gap-3">
          <input className={`${input} w-48`} placeholder="Search filename" value={q}
                 onChange={(e) => { setQ(e.target.value); setPage(1); }} />
          <select className={`${input} w-auto`} value={status} onChange={(e) => { setStatus(e.target.value); setPage(1); }}>
            <option value="">All statuses</option>
            {STATUSES.map((s) => <option key={s} value={s}>{s.replace('_', ' ')}</option>)}
          </select>
          <label className="text-sm flex items-center gap-1">
            <input type="checkbox" checked={review} onChange={(e) => { setReview(e.target.checked); setPage(1); }} />
            Needs review
          </label>
          <button className={`${btn2} ml-auto`} onClick={() => reprocessAll.mutate()}>Reprocess all failed</button>
        </div>
        <Err msg={list.isError ? errMsg(list.error) : null} />
        <table className="w-full text-sm">
          <thead className="text-left text-muted">
            <tr><th className="py-2">File</th><th>Status</th><th>Progress</th><th>Invoices</th><th /></tr>
          </thead>
          <tbody>
            {data?.items.map((d) => (
              <tr key={d.id} className="border-t border-panel">
                <td className="py-2"><Link className="text-navy underline" to={`/documents/${d.id}`}>{d.original_filename}</Link></td>
                <td>
                  <Badge text={d.status} />
                  {d.error_message && <p className="text-xs text-red-700 max-w-xs">{d.error_message}</p>}
                </td>
                <td className="w-40">
                  <div className="h-2 bg-panel rounded-full overflow-hidden">
                    <div className="h-2 bg-navy" style={{ width: `${d.progress_percent}%` }} />
                  </div>
                </td>
                <td>{d.invoice_count}{d.needs_review_count > 0 && <span className="text-amber"> ({d.needs_review_count} to review)</span>}</td>
                <td className="text-right">
                  {d.status === 'failed' && <button className={btn} onClick={() => reprocess.mutate(d.id)}>Reprocess</button>}
                </td>
              </tr>
            ))}
            {data?.items.length === 0 && <tr><td colSpan={5} className="py-6 text-center text-muted">No documents yet.</td></tr>}
          </tbody>
        </table>
        <div className="flex items-center gap-3 text-sm">
          <button className={btn} disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</button>
          <span>Page {page} of {pages}</span>
          <button className={btn} disabled={page >= pages} onClick={() => setPage(page + 1)}>Next</button>
        </div>
      </section>
    </div>
  );
}
