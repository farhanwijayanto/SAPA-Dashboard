import React, { useEffect, useRef, useState } from 'react';
import api from '../api';
import { Camera, CheckCircle2, XCircle, AlertCircle } from 'lucide-react';

type EdgeStatus = {
  ts: string;
  is_valid: boolean | null;
  employee_id: string | null;
  message: string | null;
  stale: boolean;
  age_ms: number;
};

const Edge: React.FC = () => {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const uploadBusyRef = useRef(false);
  const [cameraError, setCameraError] = useState('');
  const [status, setStatus] = useState<EdgeStatus | null>(null);
  const [now, setNow] = useState(() => new Date());
  const [lastUploadAt, setLastUploadAt] = useState<number | null>(null);
  const [uploadError, setUploadError] = useState('');

  useEffect(() => {
    const start = async () => {
      if (!window.isSecureContext && window.location.hostname !== 'localhost' && window.location.hostname !== '127.0.0.1') {
        setCameraError('Webcam access requires HTTPS (or localhost).');
        return;
      }
      try {
        setCameraError('');
        const stream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
        streamRef.current = stream;
        const v = videoRef.current;
        if (v) {
          v.srcObject = stream;
          try {
            await v.play();
          } catch {}
        }
      } catch {
        setCameraError('Webcam tidak bisa diakses. Pastikan izin camera aktif dan tidak dipakai aplikasi lain.');
      }
    };
    start();
    return () => {
      const s = streamRef.current;
      if (s) s.getTracks().forEach(t => t.stop());
      streamRef.current = null;
      if (videoRef.current) videoRef.current.srcObject = null;
    };
  }, []);

  useEffect(() => {
    const t = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(t);
  }, []);

  useEffect(() => {
    let alive = true;
    const canvas = document.createElement('canvas');
    const ctx = canvas.getContext('2d', { willReadFrequently: false });
    const tick = async () => {
      if (!alive) return;
      if (cameraError) return;
      const v = videoRef.current;
      if (!v) return;
      if (v.readyState < 2) return;
      const vw = v.videoWidth || 0;
      const vh = v.videoHeight || 0;
      if (!vw || !vh || !ctx) return;
      if (uploadBusyRef.current) return;
      uploadBusyRef.current = true;
      try {
        const targetW = Math.min(640, vw);
        const targetH = Math.round((targetW / vw) * vh);
        canvas.width = targetW;
        canvas.height = targetH;
        ctx.drawImage(v, 0, 0, targetW, targetH);
        const blob: Blob | null = await new Promise((resolve) =>
          canvas.toBlob((b) => resolve(b), 'image/jpeg', 0.7)
        );
        if (!blob) return;
        const fd = new FormData();
        fd.append('frame', blob, 'frame.jpg');
        const ingestKey = (import.meta.env as any).VITE_EDGE_INGEST_KEY as string | undefined;
        await api.post('/edge/frame', fd, {
          headers: ingestKey ? { 'X-EDGE-KEY': ingestKey } : undefined,
        });
        setUploadError('');
        setLastUploadAt((prev) => {
          const nowMs = Date.now();
          if (!prev || nowMs - prev >= 1000) return nowMs;
          return prev;
        });
      } catch (e: any) {
        const status = e?.response?.status;
        const detail = e?.response?.data?.detail;
        const suffix = detail ? `: ${String(detail)}` : '';
        if (typeof status === 'number') {
          setUploadError(`Gagal mengirim frame (${status})${suffix}`);
        } else {
          setUploadError((prev) => prev || 'Gagal mengirim frame ke server.');
        }
      } finally {
        uploadBusyRef.current = false;
      }
    };
    const loop = async () => {
      if (!alive) return;
      await tick();
      if (!alive) return;
      window.setTimeout(loop, 350);
    };
    loop();
    return () => {
      alive = false;
    };
  }, [cameraError]);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const res = await api.get('/edge/status', { params: { t: Date.now() } });
        if (!alive) return;
        setStatus(res.data);
      } catch {}
    };
    const poll = async () => {
      if (!alive) return;
      await tick();
      if (!alive) return;
      window.setTimeout(poll, 800);
    };
    poll();
    return () => {
      alive = false;
    };
  }, []);

  const state: 'idle' | 'ok' | 'fail' = !status || status.stale || status.is_valid === null
    ? 'idle'
    : status.is_valid
      ? 'ok'
      : 'fail';

  const borderClass =
    state === 'ok'
      ? 'border-emerald-400 shadow-emerald-500/40 ring-4 ring-emerald-400/50'
      : state === 'fail'
        ? 'border-red-500 shadow-red-500/40 ring-4 ring-red-500/50'
        : 'border-slate-400/60 shadow-slate-500/10';

  const badgeClass =
    state === 'ok'
      ? 'bg-emerald-500/20 text-emerald-100 border-emerald-400/40'
      : state === 'fail'
        ? 'bg-red-500/20 text-red-100 border-red-400/40'
        : 'bg-slate-500/15 text-slate-200 border-white/10';

  const label =
    state === 'ok'
      ? 'PRESENSI BERHASIL'
      : state === 'fail'
        ? 'PRESENSI GAGAL'
        : 'MENUNGGU...';

  const sub =
    (status && !status.stale && (status.message || (status.employee_id ? `ID: ${status.employee_id}` : null))) || 'Arahkan wajah ke kamera';

  const tsText = status && !status.stale ? new Date(status.ts).toLocaleString('id-ID') : '';
  const clockText = now.toLocaleTimeString('en-GB', { hour12: false });
  const uploadText = uploadError
    ? uploadError
    : lastUploadAt
      ? `Upload OK • ${Math.max(0, Math.round((Date.now() - lastUploadAt) / 100) / 10)}s lalu`
      : 'Upload: menunggu kamera...';

  const icon = state === 'ok'
    ? <CheckCircle2 size={18} className="text-emerald-300" />
    : state === 'fail'
      ? <XCircle size={18} className="text-red-300" />
      : <AlertCircle size={18} className="text-slate-200" />;

  return (
    <div className="min-h-screen w-full bg-slate-950 text-white flex items-center justify-center p-3 sm:p-6">
      <div
        className={`w-[96vw] h-[72vh] sm:h-[78vh] lg:h-[92vh] rounded-3xl border-4 ${borderClass} shadow-2xl overflow-hidden relative`}
      >
        <div className="relative w-full h-full bg-slate-900">
          <video
            ref={videoRef}
            autoPlay
            playsInline
            muted
            className="absolute inset-0 w-full h-full object-cover transform -scale-x-100"
          />
          {cameraError && (
            <div className="absolute inset-0 flex items-center justify-center bg-black/60 px-6 text-center">
              <div className="max-w-md">
                <div className="mx-auto mb-3 w-12 h-12 rounded-2xl bg-white/10 flex items-center justify-center">
                  <Camera size={24} />
                </div>
                <div className="text-sm font-black">{cameraError}</div>
                <div className="text-xs font-bold text-white/70 mt-2">Jika di server edge (non-localhost), gunakan HTTPS.</div>
              </div>
            </div>
          )}

          <div className="absolute inset-0 pointer-events-none p-6 flex flex-col justify-between">
            <div className="flex items-center justify-between">
              <div className={`inline-flex items-center gap-2 px-3 py-2 rounded-2xl border ${badgeClass} backdrop-blur-md`}>
                {icon}
                <div className="text-xs font-black tracking-widest">{label}</div>
              </div>
              <div className="px-3 py-2 rounded-2xl bg-black/30 border border-white/10 text-xs font-black text-white/70 backdrop-blur-md">
                {clockText}
              </div>
            </div>

            {/* Center banner for OK / FAIL */}
            {state !== 'idle' && (
              <div className="absolute inset-x-0 bottom-6 flex items-center justify-center px-4 pointer-events-none">
                <div
                  className={`inline-flex items-center gap-2 px-4 py-2 rounded-2xl border backdrop-blur-md shadow-lg ${
                    state === 'ok'
                      ? 'bg-emerald-500/30 border-emerald-300/60 text-emerald-50'
                      : 'bg-red-500/30 border-red-300/60 text-red-50'
                  }`}
                >
                  {state === 'ok' ? <CheckCircle2 size={16} /> : <XCircle size={16} />}
                  <div className="text-xs font-black tracking-wide">
                    {state === 'ok' ? 'Presensi Berhasil' : 'Presensi Gagal'}
                    <span className="font-bold opacity-90"> — {state === 'ok' ? 'Wajah Terdaftar' : 'Wajah Tidak Dikenali'}</span>
                    {state === 'ok' && status?.employee_id ? <span className="font-bold opacity-80"> • ID {status.employee_id}</span> : null}
                  </div>
                </div>
              </div>
            )}

            <div className="flex items-end justify-between">
              <div className="max-w-[70%]">
                <div className="text-sm font-black">{sub}</div>
                <div className="text-xs font-bold text-white/70 mt-1">
                  {status?.stale ? 'Tidak ada event terbaru' : status ? `Waktu: ${tsText} • ${Math.round(status.age_ms / 100) / 10}s lalu` : 'Mengambil status...'}
                </div>
                <div className="text-xs font-bold text-white/70 mt-1">
                  {uploadText}
                </div>
              </div>
              <div className="hidden sm:flex items-center gap-2 px-3 py-2 rounded-2xl bg-black/30 border border-white/10 text-xs font-black text-white/70 backdrop-blur-md">
                <Camera size={16} />
                Presensi Kamera
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

export default Edge;
