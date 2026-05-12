import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../api';
import { Lock, User, Contact, ArrowRight, ShieldCheck, Sun, Moon } from 'lucide-react';
import { motion } from 'framer-motion';
import axios from 'axios';

const Login: React.FC = () => {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [id, setId] = useState('');
  const [error, setError] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [theme, setTheme] = useState<'light' | 'dark'>(() => (localStorage.getItem('theme') === 'dark' ? 'dark' : 'light'));
  const navigate = useNavigate();

  const applyTheme = (t: 'light' | 'dark') => {
    const root = document.documentElement;
    const isDark = t === 'dark';
    root.classList.toggle('dark', isDark);
    document.body.classList.toggle('dark', isDark);
    root.style.colorScheme = isDark ? 'dark' : 'light';
    try {
      localStorage.setItem('theme', t);
    } catch {}
  };

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsLoading(true);
    setError('');
    try {
      const userId = Number(id);
      if (!Number.isInteger(userId)) {
        setError('User ID must be a number.');
        setIsLoading(false);
        return;
      }

      const response = await api.post('/login', {
        user_id: userId,
        username,
        password,
      });
      localStorage.setItem('token', response.data.access_token);
      try {
        localStorage.setItem('role', response.data.role);
        localStorage.setItem('username', username);
        localStorage.setItem('permissions', JSON.stringify(response.data.permissions || []));
      } catch {}

      const payload = JSON.parse(atob(response.data.access_token.split('.')[1]));
      if (!localStorage.getItem('role')) localStorage.setItem('role', payload.role);
      if (!localStorage.getItem('username')) localStorage.setItem('username', payload.sub);

      const activityKey = 'activity_events';
      const now = new Date().toISOString();
      const prev = (() => {
        try {
          const raw = localStorage.getItem(activityKey);
          return raw ? JSON.parse(raw) : [];
        } catch {
          return [];
        }
      })();
      const next = [
        { ts: now, type: 'login', message: `Login success: ${payload.sub} (${payload.role})` },
        ...prev,
      ].slice(0, 50);
      localStorage.setItem(activityKey, JSON.stringify(next));

      try {
        await api.post('/audit', { event_type: 'login', message: 'Login success' });
      } catch {}
      
      setTimeout(() => navigate('/dashboard'), 500);
    } catch (err) {
      if (axios.isAxiosError(err)) {
        const statusCode = err.response?.status;
        if (!err.response) {
          setError(`Tidak bisa terhubung ke API (${api.defaults.baseURL}). Pastikan backend jalan di port 8080.`);
        } else if (statusCode === 401) {
          setError('Invalid credentials. Pastikan sudah menjalankan seed (manager/admin) lalu coba lagi.');
          const activityKey = 'activity_events';
          const now = new Date().toISOString();
          const prev = (() => {
            try {
              const raw = localStorage.getItem(activityKey);
              return raw ? JSON.parse(raw) : [];
            } catch {
              return [];
            }
          })();
          const next = [
            { ts: now, type: 'security_alert', message: `Failed login attempt: ${username} (${id})` },
            ...prev,
          ].slice(0, 50);
          try {
            localStorage.setItem(activityKey, JSON.stringify(next));
          } catch {}
        } else if (statusCode === 404) {
          setError(`Login gagal (404). Endpoint /login tidak ditemukan di ${api.defaults.baseURL}. Pastikan backend FastAPI jalan di http://${window.location.hostname}:8080, lalu restart frontend.`);
        } else if (statusCode === 500) {
          const raw = err.response?.data as any;
          const headers = (err.response?.headers ?? {}) as Record<string, any>;
          const contentType = String(headers['content-type'] ?? '');
          const text =
            typeof raw === 'string'
              ? raw
              : raw && typeof raw === 'object'
                ? JSON.stringify(raw)
                : String(raw ?? '');
          const combined = [text, err.message, err.response?.statusText].filter(Boolean).join(' ');
          const looksLikeProxyUpstreamError =
            /ECONNREFUSED|proxy|socket|upstream/i.test(combined) ||
            (contentType.includes('text/html') && /Internal Server Error/i.test(text));
          if (looksLikeProxyUpstreamError) {
            setError(`Tidak bisa terhubung ke API (${api.defaults.baseURL}). Pastikan backend jalan di port 8080.`);
          } else {
            const detail =
              (raw as any)?.detail ??
              (typeof raw === 'string' ? raw : null) ??
              'Terjadi error.';
            setError(`Login gagal (500). ${detail}`);
          }
        } else {
          const detail =
            (err.response?.data as any)?.detail ??
            (typeof err.response?.data === 'string' ? err.response?.data : null) ??
            'Terjadi error.';
          setError(`Login gagal (${statusCode}). ${detail}`);
        }
      } else {
        setError('Login gagal. Coba lagi.');
      }
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-slate-50 dark:bg-[#0F172A] relative overflow-hidden font-sans">
      {/* Animated Background Elements */}
      <div className="absolute top-0 left-0 w-full h-full overflow-hidden pointer-events-none">
        <div className="absolute top-[-10%] left-[-10%] w-[40%] h-[40%] bg-indigo-500/10 blur-[120px] rounded-full animate-pulse"></div>
        <div className="absolute bottom-[-10%] right-[-10%] w-[40%] h-[40%] bg-cyan-500/10 blur-[120px] rounded-full animate-pulse" style={{ animationDelay: '1s' }}></div>
      </div>

      <motion.div 
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.6, ease: "easeOut" }}
        className="relative z-10 w-full max-w-md px-6"
      >
        <div className="bg-white dark:bg-white/5 backdrop-blur-2xl border border-slate-200 dark:border-white/10 p-10 rounded-[40px] shadow-2xl">
          <div className="flex justify-end -mt-2 mb-4">
            <button
              type="button"
              onClick={() => {
                const next = theme === 'dark' ? 'light' : 'dark';
                setTheme(next);
              }}
              className="p-2 rounded-xl bg-slate-50 hover:bg-slate-100 text-slate-700 dark:bg-white/5 dark:hover:bg-white/10 dark:text-slate-200 transition-all border border-slate-200 dark:border-white/10"
            >
              {theme === 'dark' ? <Sun size={18} /> : <Moon size={18} />}
            </button>
          </div>
          <div className="text-center mb-10">
            <motion.div 
              initial={{ scale: 0.5 }}
              animate={{ scale: 1 }}
              className="inline-flex items-center justify-center w-16 h-16 rounded-2xl bg-gradient-to-tr from-indigo-500 to-cyan-400 mb-6 shadow-xl shadow-indigo-500/20"
            >
              <ShieldCheck size={32} className="text-white" />
            </motion.div>
            <h2 className="text-3xl font-black text-slate-900 dark:text-white tracking-tight mb-2">SAPA DASHBOARD</h2>
            <p className="text-slate-500 dark:text-slate-400 text-sm font-medium">IoT Attendance Management System</p>
          </div>

          {error && (
            <motion.div 
              initial={{ opacity: 0, x: -10 }}
              animate={{ opacity: 1, x: 0 }}
              className="bg-red-500/10 border border-red-500/20 text-red-400 px-4 py-3 rounded-2xl text-xs font-bold mb-6 flex items-center"
            >
              <ArrowRight size={14} className="mr-2 rotate-180" /> {error}
            </motion.div>
          )}

          <form onSubmit={handleLogin} className="space-y-5">
            <div className="space-y-2">
              <label className="text-[10px] font-black text-slate-500 uppercase tracking-widest ml-1">Username</label>
              <div className="relative group">
                <span className="absolute inset-y-0 left-0 pl-4 flex items-center text-slate-500 group-focus-within:text-indigo-400 transition-colors">
                  <User size={18} />
                </span>
                <input
                  type="text"
                  className="w-full pl-12 pr-4 py-4 bg-slate-50 dark:bg-white/5 border border-slate-200 dark:border-white/10 rounded-2xl text-slate-900 dark:text-white placeholder-slate-500 dark:placeholder-slate-600 focus:outline-none focus:border-indigo-500/50 focus:ring-4 focus:ring-indigo-500/10 transition-all"
                  placeholder="Enter username"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  required
                />
              </div>
            </div>

            <div className="space-y-2">
              <label className="text-[10px] font-black text-slate-500 uppercase tracking-widest ml-1">User ID</label>
              <div className="relative group">
                <span className="absolute inset-y-0 left-0 pl-4 flex items-center text-slate-500 group-focus-within:text-indigo-400 transition-colors">
                  <Contact size={18} />
                </span>
                <input
                  type="text"
                  className="w-full pl-12 pr-4 py-4 bg-slate-50 dark:bg-white/5 border border-slate-200 dark:border-white/10 rounded-2xl text-slate-900 dark:text-white placeholder-slate-500 dark:placeholder-slate-600 focus:outline-none focus:border-indigo-500/50 focus:ring-4 focus:ring-indigo-500/10 transition-all"
                  placeholder="Input ID"
                  value={id}
                  onChange={(e) => setId(e.target.value)}
                  required
                />
              </div>
            </div>

            <div className="space-y-2">
              <label className="text-[10px] font-black text-slate-500 uppercase tracking-widest ml-1">Password</label>
              <div className="relative group">
                <span className="absolute inset-y-0 left-0 pl-4 flex items-center text-slate-500 group-focus-within:text-indigo-400 transition-colors">
                  <Lock size={18} />
                </span>
                <input
                  type="password"
                  className="w-full pl-12 pr-4 py-4 bg-slate-50 dark:bg-white/5 border border-slate-200 dark:border-white/10 rounded-2xl text-slate-900 dark:text-white placeholder-slate-500 dark:placeholder-slate-600 focus:outline-none focus:border-indigo-500/50 focus:ring-4 focus:ring-indigo-500/10 transition-all"
                  placeholder="••••••••"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                />
              </div>
            </div>

            <button
              type="submit"
              disabled={isLoading}
              className="w-full bg-gradient-to-r from-indigo-600 to-indigo-500 hover:from-indigo-500 hover:to-indigo-400 text-white py-4 rounded-2xl font-black text-sm uppercase tracking-widest shadow-xl shadow-indigo-600/20 active:scale-[0.98] transition-all flex items-center justify-center space-x-2 disabled:opacity-50"
            >
              {isLoading ? (
                <div className="w-5 h-5 border-2 border-white/30 border-t-white rounded-full animate-spin"></div>
              ) : (
                <>
                  <span>Sign In</span>
                  <ArrowRight size={18} />
                </>
              )}
            </button>
          </form>

          <p className="mt-8 text-center text-slate-500 text-xs font-medium">
            Authorized Personnel Only
          </p>
        </div>
        
        <div className="mt-8 flex justify-center space-x-6">
          <div className="flex items-center text-slate-600 text-[10px] font-bold uppercase tracking-widest">
            <div className="w-1.5 h-1.5 bg-emerald-500 rounded-full mr-2"></div>
            Cloud Server: Online
          </div>
          <div className="flex items-center text-slate-600 text-[10px] font-bold uppercase tracking-widest">
            <div className="w-1.5 h-1.5 bg-emerald-500 rounded-full mr-2"></div>
            Edge Nodes: Active
          </div>
        </div>
      </motion.div>
    </div>
  );
};

export default Login;
