import { FormEvent, ReactNode, useState } from 'react';
import { BrowserRouter, Link, Navigate, Route, Routes, useNavigate } from 'react-router-dom';
import { api, errMsg } from './api';
import { btn, card, Err, input } from './ui';
import Documents from './Documents';
import Detail from './Detail';
import Search from './Search';
import Crosscheck from './Crosscheck';

const LOGO = 'https://images.cottagehome.care/JeRtSremNqgkxL9F76cqM';
const Logo = ({ h }: { h: string }) => <img src={LOGO} alt="Cottage Home Care" className={h} />;

function Layout({ children }: { children: ReactNode }) {
  if (!localStorage.getItem('token')) return <Navigate to="/login" replace />;
  const signOut = () => {
    localStorage.removeItem('token');
    location.href = '/login';
  };
  return (
    <div className="max-w-6xl mx-auto p-4 space-y-4">
      <header className="flex items-center gap-6">
        <Link to="/"><Logo h="h-10" /></Link>
        <Link to="/" className="text-navy font-medium">Documents</Link>
        <Link to="/search" className="text-navy font-medium">Search</Link>
        <Link to="/crosscheck" className="text-navy font-medium">Compare</Link>
        <button onClick={signOut} className="ml-auto text-sm text-muted underline">Sign out</button>
      </header>
      {children}
    </div>
  );
}

function Login() {
  const nav = useNavigate();
  const [register, setRegister] = useState(false);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [err, setErr] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setErr(null);
    try {
      if (register) await api.post('/auth/register', { email, password });
      const { data } = await api.post('/auth/login', { email, password });
      localStorage.setItem('token', data.access_token);
      nav('/');
    } catch (x) {
      setErr(errMsg(x));
    }
  };

  return (
    <form onSubmit={submit} className={`${card} max-w-sm mx-auto mt-24 space-y-4`}>
      <Logo h="h-12 mx-auto" />
      <h1 className="text-navy text-xl font-semibold text-center">
        {register ? 'Create account' : 'Sign in'}
      </h1>
      <input className={input} type="email" placeholder="Email" value={email}
             onChange={(e) => setEmail(e.target.value)} required />
      <input className={input} type="password" placeholder="Password (8+ characters)" value={password}
             onChange={(e) => setPassword(e.target.value)} minLength={8} required />
      <Err msg={err} />
      <button className={`${btn} w-full`}>{register ? 'Register' : 'Sign in'}</button>
      <button type="button" className="text-sm text-muted underline w-full"
              onClick={() => setRegister(!register)}>
        {register ? 'I already have an account' : 'Create an account'}
      </button>
    </form>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/" element={<Layout><Documents /></Layout>} />
        <Route path="/documents/:id" element={<Layout><Detail /></Layout>} />
        <Route path="/search" element={<Layout><Search /></Layout>} />
        <Route path="/crosscheck" element={<Layout><Crosscheck /></Layout>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
