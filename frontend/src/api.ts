import axios from 'axios';

export const api = axios.create({ baseURL: '/api/v1' });

api.interceptors.request.use((c) => {
  const t = localStorage.getItem('token');
  if (t) c.headers.Authorization = `Bearer ${t}`;
  return c;
});

api.interceptors.response.use(
  (r) => r,
  (e) => {
    if (e.response?.status === 401 && !location.pathname.startsWith('/login')) {
      localStorage.removeItem('token');
      location.href = '/login';
    }
    return Promise.reject(e);
  },
);

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export const errMsg = (e: any): string =>
  e?.response?.data?.error?.message ?? 'Something went wrong. Please try again.';

export const IN_FLIGHT = ['queued', 'processing', 'extracting', 'validating'];

export interface Doc {
  id: string;
  original_filename: string;
  file_ext: string;
  mime_type: string;
  status: string;
  progress_percent: number;
  error_message: string | null;
  invoice_count: number;
  needs_review_count: number;
  created_at: string;
}
export interface Issue {
  id: string;
  code: string;
  severity: 'error' | 'warning' | 'info';
  field: string | null;
  message: string;
  resolved: boolean;
}
export interface Line {
  position: number;
  description: string | null;
  quantity: string | null;
  unit_price: string | null;
  line_total: string | null;
}
export interface Invoice {
  id: string;
  invoice_number: string | null;
  invoice_date: string | null;
  supplier_name: string | null;
  customer_name: string | null;
  subtotal: string | null;
  tax: string | null;
  discount: string | null;
  total: string | null;
  currency: string | null;
  needs_review: boolean;
  review_reason: string[] | null;
  is_corrected: boolean;
  line_items: Line[];
  issues: Issue[];
  corrections: unknown[];
}
export interface DocDetail extends Doc {
  invoices: Invoice[];
  events: { stage: string; status: string; message: string | null; created_at: string }[];
}
export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}
