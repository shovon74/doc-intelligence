export const card = 'bg-white rounded-2xl shadow-sm border border-panel p-5';
export const btn =
  'bg-navy text-white rounded-full px-4 py-2 text-sm font-medium hover:opacity-90 disabled:opacity-50';
export const btn2 =
  'bg-amber text-white rounded-full px-4 py-2 text-sm font-medium hover:opacity-90 disabled:opacity-50';
export const input = 'border border-gray-300 rounded-lg px-3 py-2 text-sm bg-white w-full';

const COLORS: Record<string, string> = {
  completed: 'text-green-700 bg-green-100',
  needs_review: 'text-amber bg-orange-100',
  failed: 'text-red-700 bg-red-100',
  error: 'text-red-700 bg-red-100',
  warning: 'text-amber bg-orange-100',
  info: 'text-navy bg-panel',
};

export function Badge({ text }: { text: string }) {
  const c = COLORS[text] ?? 'text-navy bg-blue-100';
  return <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${c}`}>{text.replace('_', ' ')}</span>;
}

export function Err({ msg }: { msg: string | null }) {
  return msg ? <p className="text-red-700 text-sm">{msg}</p> : null;
}
