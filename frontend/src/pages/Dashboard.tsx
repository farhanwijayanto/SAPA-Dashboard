import React, { useEffect, useMemo, useRef, useState } from 'react';
import api from '../api';
import { 
  Camera, ClipboardList, UserPlus, Trash2, LogOut, 
  Clock, LayoutDashboard, Settings, 
  Search, Bell, ChevronLeft, ChevronRight, Menu, X,
  CheckCircle2, AlertCircle, Users, Download, ChevronUp, Sun, Moon,
  Wifi, WifiOff, DoorOpen, DoorClosed, Eye, Info, XCircle, Briefcase
} from 'lucide-react';
import { format } from 'date-fns';
import { motion, AnimatePresence } from 'framer-motion';
import { 
  AreaChart, Area, XAxis, YAxis, CartesianGrid, 
  Tooltip, ResponsiveContainer
} from 'recharts';

interface ActivityEvent {
  ts: string;
  type: string;
  message: string;
}

interface SystemMetrics {
  updated_at: string;
  disk_path: string;
  disk_total_bytes: number;
  disk_used_bytes: number;
  disk_free_bytes: number;
  memory_total_bytes: number | null;
  memory_available_bytes: number | null;
}

interface AttendanceLog {
  employee_id: string;
  timestamp: string;
  status: string;
  reason?: string;
  direction?: string;
  employee: {
    name: string;
    dob: string;
    division: string;
    position: string;
  };
}

interface Employee {
  id: string;
  name: string;
  dob: string;
  division: string;
  position: string;
}

interface AppUser {
  id: number;
  username: string;
  email?: string | null;
  role: string;
  permissions?: string[];
}

interface ProfileUser {
  id: number;
  username: string;
  email?: string | null;
  full_name?: string | null;
  avatar_url?: string | null;
  role: string;
  permissions?: string[];
}

const Dashboard: React.FC = () => {
  const [logs, setLogs] = useState<AttendanceLog[]>([]);
  const [role] = useState(localStorage.getItem('role') || '');
  const [permissions] = useState<string[]>(() => {
    try {
      const raw = localStorage.getItem('permissions');
      const parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed) ? parsed.filter((x) => typeof x === 'string') : [];
    } catch {
      return [];
    }
  });
  const [username, setUsername] = useState(localStorage.getItem('username') || '');
  const [manualAttendance, setManualAttendance] = useState({ employee_id: '', direction: 'in', status: 'permission', reason: '' });
  const [newEmployee, setNewEmployee] = useState({ name: '', dob: '', division: '', position: '' });
  const [faceFile, setFaceFile] = useState<File | null>(null);
  const [employees, setEmployees] = useState<Employee[]>([]);
  const [streamUrl] = useState(() => {
    const raw = (api.defaults.baseURL || '/api').toString();
    const base = raw.endsWith('/') ? raw.slice(0, -1) : raw;
    return `${base}/edge/frame.jpg`;
  });
  const [edgeFrameNonce, setEdgeFrameNonce] = useState(() => Date.now());
  const [edgeFrameOk, setEdgeFrameOk] = useState(false);
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [activeTab, setActiveTab] = useState<'dashboard' | 'employees' | 'logs' | 'manual' | 'add_employee' | 'roles' | 'system' | 'profile'>('dashboard');
  const [employeesPage, setEmployeesPage] = useState(1);
  const [employeesPageInput, setEmployeesPageInput] = useState('1');
  const [logsPage, setLogsPage] = useState(1);
  const [logsPageInput, setLogsPageInput] = useState('1');
  const [livePage, setLivePage] = useState(1);
  const [loginAuditPage, setLoginAuditPage] = useState(1);
  const [selectedDate, setSelectedDate] = useState<string>(() => format(new Date(), 'yyyy-MM-dd'));
  const [selectedDirection, setSelectedDirection] = useState<string>('');
  const [selectedEmployeeId, setSelectedEmployeeId] = useState<string>('');
  const [selectedStatus, setSelectedStatus] = useState<string>('');
  const registerVideoRef = useRef<HTMLVideoElement | null>(null);
  const registerStreamRef = useRef<MediaStream | null>(null);
  const [isRegisterScanning, setIsRegisterScanning] = useState(false);
  const [capturedPreviewUrl, setCapturedPreviewUrl] = useState<string>('');
  const [registerWebcamError, setRegisterWebcamError] = useState('');
  const [activityEvents, setActivityEvents] = useState<ActivityEvent[]>([]);
  const activityKey = 'activity_events';
  const logsScrollRef = useRef<HTMLDivElement | null>(null);
  const [showScrollTop, setShowScrollTop] = useState(false);
  const [systemMetrics, setSystemMetrics] = useState<SystemMetrics | null>(null);
  const [systemLoading, setSystemLoading] = useState(false);
  const [systemError, setSystemError] = useState('');
  const [gateLoading, setGateLoading] = useState(false);
  const [gateMessage, setGateMessage] = useState('');
  const [gateStatus, setGateStatus] = useState<{
    status: string;
    last_action: string;
    timestamp: string;
    iot_connected?: boolean;
    iot_last_seen?: string | null;
    iot_age_ms?: number | null;
  } | null>(null);
  const [iotStatus, setIotStatus] = useState<{ connected: boolean; last_seen: string | null; age_ms: number | null } | null>(null);
  const [loginAudit, setLoginAudit] = useState<Array<{ username: string; status: string; timestamp: string; role?: string | null; user_id?: number | null }>>([]);
  const [loginAuditLoading, setLoginAuditLoading] = useState(false);
  const [faceViewerOpen, setFaceViewerOpen] = useState(false);
  const [selectedEmployeeForFaces, setSelectedEmployeeForFaces] = useState<string>('');
  const [selectedEmployeeNameForFaces, setSelectedEmployeeNameForFaces] = useState<string>('');
  const [employeeFaces, setEmployeeFaces] = useState<any[]>([]);
  const [facesLoading, setFacesLoading] = useState(false);
  const [toasts, setToasts] = useState<Array<{ id: number; tone: 'success' | 'info' | 'warning' | 'error'; title: string; message: string }>>([]);

  // Confirm dialog
  const [confirmState, setConfirmState] = useState<{
    open: boolean;
    title: string;
    message: string;
    confirmLabel: string;
    tone: 'danger' | 'primary';
    onConfirm: (() => void | Promise<void>) | null;
  }>({ open: false, title: '', message: '', confirmLabel: 'Confirm', tone: 'primary', onConfirm: null });
  const [confirmBusy, setConfirmBusy] = useState(false);

  const askConfirm = (opts: {
    title: string;
    message: string;
    confirmLabel?: string;
    tone?: 'danger' | 'primary';
    onConfirm: () => void | Promise<void>;
  }) => {
    setConfirmState({
      open: true,
      title: opts.title,
      message: opts.message,
      confirmLabel: opts.confirmLabel || 'Confirm',
      tone: opts.tone || 'primary',
      onConfirm: opts.onConfirm,
    });
  };

  const closeConfirm = () => {
    if (confirmBusy) return;
    setConfirmState(s => ({ ...s, open: false, onConfirm: null }));
  };

  const runConfirm = async () => {
    if (!confirmState.onConfirm) return;
    try {
      setConfirmBusy(true);
      await confirmState.onConfirm();
    } finally {
      setConfirmBusy(false);
      setConfirmState(s => ({ ...s, open: false, onConfirm: null }));
    }
  };

  // Roles
  type RoleStat = {
    id: number;
    division: string;
    position: string;
    description?: string | null;
    total: number;
    active_today: number;
    inactive_today: number;
  };
  const [roles, setRoles] = useState<RoleStat[]>([]);
  const [rolesLoading, setRolesLoading] = useState(false);
  const [newRole, setNewRole] = useState({ division: '', position: '', description: '' });
  const [createRoleSaving, setCreateRoleSaving] = useState(false);

  const pushToast = (tone: 'success' | 'info' | 'warning' | 'error', title: string, message: string) => {
    const id = Date.now() + Math.random();
    setToasts(prev => [...prev, { id, tone, title, message }]);
    window.setTimeout(() => {
      setToasts(prev => prev.filter(t => t.id !== id));
    }, 4500);
  };

  const dismissToast = (id: number) => {
    setToasts(prev => prev.filter(t => t.id !== id));
  };
  const [users, setUsers] = useState<AppUser[]>([]);
  const [usersLoading, setUsersLoading] = useState(false);
  const [createUserOpen, setCreateUserOpen] = useState(false);
  const [createUserSaving, setCreateUserSaving] = useState(false);
  const [createUserMessage, setCreateUserMessage] = useState('');
  const [createUserForm, setCreateUserForm] = useState({
    username: '',
    full_name: '',
    email: '',
    password: '',
    role: 'custom' as 'admin' | 'custom',
    customRole: '',
    permissions: [] as string[],
  });
  const [me, setMe] = useState<ProfileUser | null>(null);
  const [profileForm, setProfileForm] = useState({ id: '', username: '', full_name: '', email: '' });
  const [profileSaving, setProfileSaving] = useState(false);
  const [profileMessage, setProfileMessage] = useState('');
  const [avatarFile, setAvatarFile] = useState<File | null>(null);
  const [avatarPreviewUrl, setAvatarPreviewUrl] = useState<string>('');
  const [avatarUploading, setAvatarUploading] = useState(false);
  const [avatarMessage, setAvatarMessage] = useState('');
  const [passwordOld, setPasswordOld] = useState('');
  const [passwordNew, setPasswordNew] = useState('');
  const [passwordNew2, setPasswordNew2] = useState('');
  const [passwordSaving, setPasswordSaving] = useState(false);
  const [passwordMessage, setPasswordMessage] = useState('');
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const [notificationsReadAt, setNotificationsReadAt] = useState<string>('');
  const notificationsRef = useRef<HTMLDivElement | null>(null);
  const [theme, setTheme] = useState<'light' | 'dark'>(() => (localStorage.getItem('theme') === 'dark' ? 'dark' : 'light'));
  const [now, setNow] = useState(() => new Date());
  const [isMobile, setIsMobile] = useState(() => {
    try {
      return window.matchMedia ? window.matchMedia('(max-width: 639px)').matches : window.innerWidth < 640;
    } catch {
      return false;
    }
  });
  const [mobileNavOpen, setMobileNavOpen] = useState(false);

  const edgeFrameSrc = streamUrl
    ? `${streamUrl}${streamUrl.includes('?') ? '&' : '?'}t=${edgeFrameNonce}`
    : '';

  const isManager = role === 'manager';
  const isAdmin = role === 'admin';
  const hasFeature = (key: string) => {
    if (isManager) return true;
    if (permissions.includes('*')) return true;
    if (permissions.length === 0) return isAdmin;
    return permissions.includes(key);
  };

  const showEmployeesTab = isManager || isAdmin || hasFeature('view_employees');
  const showLogsTab = isManager || isAdmin || hasFeature('view_logs');
  const showManualTab = isManager || isAdmin || hasFeature('manual_attendance');
  const showAddEmployeeTab = isManager;
  const showRolesTab = isManager;
  const showSystemTab = isManager || isAdmin || hasFeature('view_system');

  useEffect(() => {
    if (activeTab === 'employees' && !showEmployeesTab) setActiveTab('dashboard');
    if (activeTab === 'logs' && !showLogsTab) setActiveTab('dashboard');
    if (activeTab === 'manual' && !showManualTab) setActiveTab('dashboard');
    if (activeTab === 'add_employee' && !showAddEmployeeTab) setActiveTab('dashboard');
    if (activeTab === 'roles' && !showRolesTab) setActiveTab('dashboard');
    if (activeTab === 'system' && !showSystemTab) setActiveTab('dashboard');
  }, [activeTab, showEmployeesTab, showLogsTab, showManualTab, showAddEmployeeTab, showRolesTab, showSystemTab]);

  useEffect(() => {
    fetchEmployees();
    if (isManager) {
      fetchRoles();
    }
  }, []);

  useEffect(() => {
    if (!showRolesTab) return;
    if (activeTab === 'roles' || activeTab === 'add_employee' || activeTab === 'manual') {
      fetchRoles();
    }
  }, [activeTab, showRolesTab]);

  useEffect(() => {
    try {
      const raw = localStorage.getItem(activityKey);
      setActivityEvents(raw ? JSON.parse(raw) : []);
    } catch {
      setActivityEvents([]);
    }
  }, []);

  useEffect(() => {
    try {
      const raw = localStorage.getItem('notifications_read_at');
      setNotificationsReadAt(raw || '');
    } catch {
      setNotificationsReadAt('');
    }
  }, []);

  useEffect(() => {
    const onDocMouseDown = (e: MouseEvent) => {
      const el = notificationsRef.current;
      if (!el) return;
      if (!el.contains(e.target as Node)) {
        setNotificationsOpen(false);
      }
    };
    document.addEventListener('mousedown', onDocMouseDown);
    return () => document.removeEventListener('mousedown', onDocMouseDown);
  }, []);

  useEffect(() => {
    const root = document.documentElement;
    const isDark = theme === 'dark';
    root.classList.toggle('dark', isDark);
    document.body.classList.toggle('dark', isDark);
    root.style.colorScheme = isDark ? 'dark' : 'light';
    try {
      localStorage.setItem('theme', theme);
    } catch {}
  }, [theme]);

  useEffect(() => {
    const t = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(t);
  }, []);

  useEffect(() => {
    const mq = window.matchMedia ? window.matchMedia('(max-width: 639px)') : null;
    const apply = () => setIsMobile(mq ? mq.matches : window.innerWidth < 640);
    apply();
    if (mq) {
      mq.addEventListener('change', apply);
      return () => mq.removeEventListener('change', apply);
    }
    window.addEventListener('resize', apply);
    return () => window.removeEventListener('resize', apply);
  }, []);

  useEffect(() => {
    if (!isMobile) {
      setMobileNavOpen(false);
      return;
    }
    setIsSidebarCollapsed(false);
  }, [isMobile]);

  useEffect(() => {
    const t = window.setInterval(() => setEdgeFrameNonce(Date.now()), 350);
    return () => window.clearInterval(t);
  }, []);

  useEffect(() => {
    fetchLogs();
    const interval = setInterval(fetchLogs, 5000);
    return () => clearInterval(interval);
  }, [selectedDate, selectedDirection, selectedEmployeeId, selectedStatus]);

  useEffect(() => {
    return () => {
      stopRegisterScan();
    };
  }, []);

  const fetchEmployees = async () => {
    try {
      const response = await api.get('/employees/');
      setEmployees(response.data);
    } catch (err) {
      console.error('Failed to fetch employees', err);
    }
  };

  const fetchRoles = async () => {
    setRolesLoading(true);
    try {
      const response = await api.get('/roles/stats');
      setRoles(response.data);
    } catch {
      try {
        const fallback = await api.get('/roles/');
        setRoles((fallback.data as any[]).map(r => ({ ...r, total: 0, active_today: 0, inactive_today: 0 })));
      } catch {
        setRoles([]);
      }
    } finally {
      setRolesLoading(false);
    }
  };

  const handleCreateRole = async (e: React.FormEvent) => {
    e.preventDefault();
    const division = newRole.division.trim();
    const position = newRole.position.trim();
    if (!division || !position) {
      pushToast('warning', 'Field belum lengkap', 'Division dan Position wajib diisi.');
      return;
    }
    setCreateRoleSaving(true);
    try {
      await api.post('/roles/', {
        division,
        position,
        description: newRole.description.trim() || null,
      });
      setNewRole({ division: '', position: '', description: '' });
      await fetchRoles();
      pushToast('success', 'Role berhasil ditambahkan', `${division} • ${position}`);
    } catch (err: any) {
      const status = err?.response?.status;
      const detail = err?.response?.data?.detail;
      let message = '';
      if (typeof detail === 'string') {
        message = detail;
      } else if (Array.isArray(detail)) {
        message = detail.map((d: any) => d?.msg || JSON.stringify(d)).join('; ');
      } else if (status === 404) {
        message = 'Endpoint /roles/ tidak ditemukan. Restart backend untuk memuat fitur Role.';
      } else if (status === 500) {
        message = 'Backend error. Restart backend agar tabel roles dibuat (bisa juga DB belum migrasi).';
      } else if (!err?.response) {
        message = 'Tidak bisa terhubung ke API.';
      } else {
        message = `Gagal (${status || '?'}). Coba lagi.`;
      }
      pushToast('error', 'Gagal menambahkan role', message);
    } finally {
      setCreateRoleSaving(false);
    }
  };

  const handleDeleteRole = async (roleId: number, label: string) => {
    askConfirm({
      title: 'Hapus Role',
      message: `Apakah kamu yakin ingin menghapus role "${label}"? Tindakan ini tidak bisa dibatalkan.`,
      confirmLabel: 'Hapus Role',
      tone: 'danger',
      onConfirm: async () => {
        try {
          await api.delete(`/roles/${roleId}`);
          await fetchRoles();
          pushToast('info', 'Role dihapus', label);
        } catch (err: any) {
          const detail = err?.response?.data?.detail;
          pushToast('error', 'Gagal menghapus role', String(detail || 'Coba lagi.'));
        }
      },
    });
  };

  const fetchLogs = async () => {
    try {
      const params: any = {};
      if (selectedDate) params.date = selectedDate;
      if (selectedDirection) params.direction = selectedDirection;
      if (selectedEmployeeId) params.employee_id = selectedEmployeeId;
      if (selectedStatus) params.status = selectedStatus;
      const response = await api.get('/attendance/', { params });
      setLogs(response.data);
    } catch (err) {
      console.error('Failed to fetch logs', err);
    }
  };

  const formatBytes = (bytes: number | null) => {
    if (bytes === null) return '--';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let b = bytes;
    let i = 0;
    while (b >= 1024 && i < units.length - 1) {
      b /= 1024;
      i++;
    }
    return `${b.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
  };

  const fetchSystemMetrics = async () => {
    setSystemLoading(true);
    setSystemError('');
    try {
      const res = await api.get('/system/metrics');
      setSystemMetrics(res.data);
    } catch (e: any) {
      setSystemError('Failed to load system metrics.');
      setSystemMetrics(null);
    } finally {
      setSystemLoading(false);
    }
  };

  const fetchGateStatus = async () => {
    try {
      const res = await api.get('/gate/status');
      setGateStatus(res.data);
    } catch (e: any) {
      setGateStatus(null);
    }
  };

  const fetchIotStatus = async () => {
    try {
      const res = await api.get('/iot/status');
      setIotStatus({
        connected: !!res.data?.connected,
        last_seen: res.data?.last_seen ?? null,
        age_ms: res.data?.age_ms ?? null,
      });
    } catch {
      setIotStatus({ connected: false, last_seen: null, age_ms: null });
    }
  };

  const fetchLoginAudit = async () => {
    setLoginAuditLoading(true);
    try {
      const res = await api.get('/audit/logins', { params: { limit: 100 } });
      setLoginAudit(Array.isArray(res.data) ? res.data : []);
    } catch {
      setLoginAudit([]);
    } finally {
      setLoginAuditLoading(false);
    }
  };

  const controlGate = async (action: 'open' | 'close') => {
    setGateLoading(true);
    setGateMessage('');
    try {
      await api.post('/gate/control', { action, gate_id: 'default' });
      setGateMessage(`Gate ${action}ed successfully`);
      await fetchGateStatus();
      setTimeout(() => setGateMessage(''), 3000);
    } catch (e: any) {
      const msg = e.response?.data?.detail || `Failed to ${action} gate`;
      setGateMessage(`Error: ${msg}`);
    } finally {
      setGateLoading(false);
    }
  };

  const fetchEmployeeFaces = async (employeeId: string) => {
    setFacesLoading(true);
    try {
      const res = await api.get(`/employees/${employeeId}/faces`);
      setEmployeeFaces(res.data.faces || []);
    } catch (e: any) {
      setEmployeeFaces([]);
    } finally {
      setFacesLoading(false);
    }
  };

  const openFaceViewer = async (employeeId: string, employeeName?: string) => {
    setSelectedEmployeeForFaces(employeeId);
    setSelectedEmployeeNameForFaces(employeeName || '');
    setFaceViewerOpen(true);
    await fetchEmployeeFaces(employeeId);
  };

  useEffect(() => {
    if (activeTab !== 'system') return;
    if (role === 'manager') {
      fetchSystemMetrics();
    } else {
      setSystemMetrics(null);
      setSystemError('');
    }
    fetchGateStatus();
    fetchIotStatus();
    if (role === 'manager') {
      fetchLoginAudit();
    }
    const t = role === 'manager' ? window.setInterval(fetchSystemMetrics, 10000) : null;
    const gt = window.setInterval(() => {
      fetchGateStatus();
      fetchIotStatus();
    }, 3000);
    const lt = role === 'manager' ? window.setInterval(fetchLoginAudit, 10000) : null;
    return () => {
      if (t) window.clearInterval(t);
      window.clearInterval(gt);
      if (lt) window.clearInterval(lt);
    };
  }, [activeTab, role]);

  useEffect(() => {
    if (activeTab !== 'employees') return;
    if (role !== 'manager') return;
    fetchUsers();
  }, [activeTab, role]);

  useEffect(() => {
    if (activeTab !== 'profile') return;
    fetchMe();
  }, [activeTab]);

  useEffect(() => {
    if (activeTab !== 'employees') return;
    setEmployeesPage(1);
  }, [activeTab, searchQuery]);

  useEffect(() => {
    setEmployeesPageInput(String(employeesPage));
  }, [employeesPage]);

  useEffect(() => {
    if (activeTab !== 'logs') return;
    setLogsPage(1);
  }, [activeTab, searchQuery, selectedDate, selectedDirection, selectedEmployeeId, selectedStatus]);

  useEffect(() => {
    setLogsPageInput(String(logsPage));
  }, [logsPage]);

  const appendActivity = async (type: string, message: string) => {
    const now = new Date().toISOString();
    setActivityEvents(prev => {
      const next = [{ ts: now, type, message }, ...prev].slice(0, 50);
      try {
        localStorage.setItem(activityKey, JSON.stringify(next));
      } catch {}
      return next;
    });
    try {
      await api.post('/audit', { event_type: type, message });
    } catch {}
  };

  const fetchUsers = async () => {
    setUsersLoading(true);
    try {
      const res = await api.get('/users/');
      setUsers(res.data);
    } catch {
      setUsers([]);
    } finally {
      setUsersLoading(false);
    }
  };

  const handleDeleteUser = async (userId: number) => {
    const target = users.find(u => u.id === userId);
    const label = target?.username ? `${target.username} (ID ${userId})` : `User ID ${userId}`;
    askConfirm({
      title: 'Hapus User',
      message: `Apakah kamu yakin ingin menghapus ${label}? Tindakan ini tidak bisa dibatalkan.`,
      confirmLabel: 'Hapus User',
      tone: 'danger',
      onConfirm: async () => {
        try {
          await api.delete(`/users/${userId}`);
          await fetchUsers();
          appendActivity('user_delete', `User deleted: ${userId}`);
          pushToast('info', 'User dihapus', label);
        } catch {
          pushToast('error', 'Gagal menghapus user', 'Coba lagi.');
        }
      },
    });
  };

  const openCreateUser = () => {
    const existingRoles = Array.from(new Set(users.map((u) => (u.role || '').trim()).filter(Boolean))).sort((a, b) =>
      a.localeCompare(b)
    );
    const defaultCustomRole = existingRoles.find((r) => r !== 'manager' && r !== 'admin') || '';
    setCreateUserMessage('');
    setCreateUserForm({
      username: '',
      full_name: '',
      email: '',
      password: '',
      role: 'custom',
      customRole: defaultCustomRole,
      permissions: [],
    });
    setCreateUserOpen(true);
  };

  const toggleCreateUserPermission = (key: string) => {
    setCreateUserForm((prev) => {
      const current = prev.permissions || [];
      const set = new Set(current);
      if (set.has(key)) set.delete(key);
      else set.add(key);
      if (set.has('*') && set.size > 1) {
        return { ...prev, permissions: ['*'] };
      }
      return { ...prev, permissions: Array.from(set) };
    });
  };

  const handleCreateUser = async (e: React.FormEvent) => {
    e.preventDefault();
    setCreateUserSaving(true);
    setCreateUserMessage('');
    try {
      const usernameValue = createUserForm.username.trim();
      const fullNameValue = createUserForm.full_name.trim();
      const emailValue = createUserForm.email.trim();
      const passwordValue = createUserForm.password;
      const roleValue = (createUserForm.role === 'custom' ? createUserForm.customRole : createUserForm.role).trim();

      if (!usernameValue) {
        setCreateUserMessage('Username wajib diisi.');
        return;
      }
      if (usernameValue.length < 3) {
        setCreateUserMessage('Username minimal 3 karakter.');
        return;
      }
      if (!fullNameValue) {
        setCreateUserMessage('Nama wajib diisi.');
        return;
      }
      if (!emailValue) {
        setCreateUserMessage('Email wajib diisi.');
        return;
      }
      if (!passwordValue || passwordValue.length < 6) {
        setCreateUserMessage('Password minimal 6 karakter.');
        return;
      }
      if (!roleValue) {
        setCreateUserMessage('Role wajib diisi.');
        return;
      }
      if (roleValue === 'manager') {
        setCreateUserMessage('Role manager tidak bisa dibuat manual.');
        return;
      }

      const permsRaw = (createUserForm.permissions || []).filter((p) => typeof p === 'string' && p.trim());
      const perms = permsRaw.includes('*') ? ['*'] : Array.from(new Set(permsRaw));

      const payload = {
        username: usernameValue,
        full_name: fullNameValue,
        email: emailValue,
        password: passwordValue,
        role: roleValue,
        permissions: perms,
      };

      const res = await api.post('/users/', payload);
      await fetchUsers();
      appendActivity('user_create', `User created: ${res.data?.id} (${res.data?.role || roleValue})`);
      setCreateUserOpen(false);
    } catch (err: any) {
      const statusCode = err?.response?.status;
      const detail = err?.response?.data?.detail;
      if (statusCode === 409) {
        setCreateUserMessage(String(detail || 'Username/email already exists.'));
      } else if (statusCode === 400) {
        setCreateUserMessage(String(detail || 'Invalid input.'));
      } else {
        setCreateUserMessage('Gagal membuat user.');
      }
    } finally {
      setCreateUserSaving(false);
    }
  };

  const resolveBackendUrl = (path: string | null | undefined) => {
    if (!path) return '';
    const base = String(api.defaults.baseURL || '');
    if (base.startsWith('http')) return `${base}${path}`;
    return `${base}${path}`;
  };

  const fetchMe = async () => {
    setProfileMessage('');
    setAvatarMessage('');
    setPasswordMessage('');
    try {
      const res = await api.get('/users/me');
      const u: ProfileUser = res.data;
      setMe(u);
      setProfileForm({
        id: String(u.id ?? ''),
        username: u.username ?? '',
        full_name: (u.full_name ?? '') as string,
        email: (u.email ?? '') as string,
      });
    } catch {
      setMe(null);
      setProfileMessage('Failed to load profile.');
    }
  };

  const handleProfileSave = async (e: React.FormEvent) => {
    e.preventDefault();
    setProfileSaving(true);
    setProfileMessage('');
    try {
      const idNum = Number(profileForm.id);
      if (!Number.isInteger(idNum)) {
        setProfileMessage('ID harus berupa angka.');
        return;
      }
      const payload = {
        id: idNum,
        username: profileForm.username.trim(),
        full_name: profileForm.full_name.trim() ? profileForm.full_name.trim() : null,
        email: profileForm.email.trim() ? profileForm.email.trim() : null,
      };
      const res = await api.patch('/users/me', payload);
      const updated: ProfileUser = res.data.user;
      setMe(updated);
      setProfileForm({
        id: String(updated.id ?? ''),
        username: updated.username ?? '',
        full_name: (updated.full_name ?? '') as string,
        email: (updated.email ?? '') as string,
      });

      if (res.data.access_token) {
        localStorage.setItem('token', res.data.access_token);
      }
      localStorage.setItem('username', updated.username);
      setUsername(updated.username);
      setProfileMessage('Profile updated.');
    } catch (err: any) {
      const statusCode = err?.response?.status;
      const detail = err?.response?.data?.detail;
      if (statusCode === 409) {
        setProfileMessage(String(detail || 'Conflict.'));
      } else {
        setProfileMessage('Failed to update profile.');
      }
    } finally {
      setProfileSaving(false);
    }
  };

  const handleAvatarPick = (file: File | null) => {
    setAvatarMessage('');
    if (avatarPreviewUrl) URL.revokeObjectURL(avatarPreviewUrl);
    setAvatarPreviewUrl('');
    setAvatarFile(null);

    if (!file) return;
    if (file.type !== 'image/png' && file.type !== 'image/jpeg') {
      setAvatarMessage('Hanya file PNG/JPEG yang diperbolehkan.');
      return;
    }
    setAvatarFile(file);
    setAvatarPreviewUrl(URL.createObjectURL(file));
  };

  const handleAvatarUpload = async () => {
    if (!avatarFile) {
      setAvatarMessage('Pilih file PNG/JPEG terlebih dahulu.');
      return;
    }
    setAvatarUploading(true);
    setAvatarMessage('');
    try {
      const fd = new FormData();
      fd.append('avatar', avatarFile);
      const res = await api.post('/users/me/avatar', fd, {
        headers: { 'Content-Type': 'multipart/form-data' },
      });
      const updated: ProfileUser = res.data.user;
      setMe(updated);
      setAvatarMessage('Photo updated.');
      handleAvatarPick(null);
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      setAvatarMessage(String(detail || 'Failed to upload photo.'));
    } finally {
      setAvatarUploading(false);
    }
  };

  const handleChangePassword = async (e: React.FormEvent) => {
    e.preventDefault();
    setPasswordSaving(true);
    setPasswordMessage('');
    try {
      if (passwordNew !== passwordNew2) {
        setPasswordMessage('Password baru tidak sama.');
        return;
      }
      await api.post('/users/me/change-password', {
        old_password: passwordOld,
        new_password: passwordNew,
      });
      setPasswordOld('');
      setPasswordNew('');
      setPasswordNew2('');
      setPasswordMessage('Password updated.');
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      setPasswordMessage(String(detail || 'Failed to change password.'));
    } finally {
      setPasswordSaving(false);
    }
  };

  const attachStreamToVideo = async (videoEl: HTMLVideoElement | null, stream: MediaStream | null) => {
    if (!videoEl || !stream) return;
    videoEl.srcObject = stream;
    try {
      await videoEl.play();
    } catch {
      try {
        videoEl.muted = true;
        await videoEl.play();
      } catch {}
    }
  };

  const waitNextFrame = () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));

  const startRegisterScan = async () => {
    if (!window.isSecureContext && window.location.hostname !== 'localhost' && window.location.hostname !== '127.0.0.1') {
      pushToast('warning', 'Akses kamera membutuhkan HTTPS', 'Gunakan https pada VPS/domain, atau jalankan via localhost.');
      return;
    }
    try {
      setRegisterWebcamError('');
      const stream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
      registerStreamRef.current = stream;
      setIsRegisterScanning(true);
      await waitNextFrame();
      await attachStreamToVideo(registerVideoRef.current, registerStreamRef.current);
    } catch (err) {
      setIsRegisterScanning(false);
      setRegisterWebcamError('Webcam tidak bisa diakses. Pastikan izin camera aktif dan tidak dipakai aplikasi lain.');
      pushToast('error', 'Kamera tidak tersedia', 'Izin kamera ditolak atau perangkat tidak terdeteksi.');
    }
  };

  const stopRegisterScan = () => {
    const stream = registerStreamRef.current;
    if (stream) {
      stream.getTracks().forEach(t => t.stop());
    }
    registerStreamRef.current = null;
    if (registerVideoRef.current) {
      try {
        registerVideoRef.current.pause();
      } catch {}
      registerVideoRef.current.srcObject = null;
    }
    setIsRegisterScanning(false);
  };

  const captureRegisterFace = async () => {
    const v = registerVideoRef.current;
    if (!v) return;
    if (v.videoWidth === 0 || v.videoHeight === 0) {
      pushToast('warning', 'Kamera belum siap', 'Tunggu beberapa detik lalu coba Capture lagi.');
      return;
    }
    const canvas = document.createElement('canvas');
    canvas.width = v.videoWidth;
    canvas.height = v.videoHeight;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.drawImage(v, 0, 0, canvas.width, canvas.height);
    const blob: Blob | null = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.9));
    if (!blob) return;
    const file = new File([blob], 'face.jpg', { type: 'image/jpeg' });
    setFaceFile(file);
    if (capturedPreviewUrl) URL.revokeObjectURL(capturedPreviewUrl);
    setCapturedPreviewUrl(URL.createObjectURL(blob));
    stopRegisterScan();
  };

  const STATUS_LABELS_ID: Record<string, string> = {
    present: 'Presensi Hadir',
    permission: 'Izin',
    sick: 'Izin Sakit',
    leave: 'Izin Cuti',
    half_day: 'Izin Setengah Hari',
    other: 'Izin Lainnya',
  };

  const toastToneForStatus = (status: string): 'success' | 'info' | 'warning' => {
    if (status === 'present') return 'success';
    if (status === 'sick') return 'warning';
    return 'info';
  };

  const handleManualSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const submitted = {
      ...manualAttendance,
      employee_id: manualAttendance.employee_id.trim(),
    };

    if (!submitted.employee_id) {
      pushToast('warning', 'Employee ID kosong', 'Isi Employee ID terlebih dahulu.');
      return;
    }

    const employee = employees.find(emp => emp.id === submitted.employee_id);
    if (!employee) {
      pushToast(
        'error',
        'Employee ID tidak ditemukan',
        `Tidak ada karyawan dengan ID ${submitted.employee_id}. Periksa kembali ID-nya.`
      );
      return;
    }

    try {
      await api.post('/attendance/', submitted);
      setManualAttendance({ employee_id: '', direction: 'in', status: 'permission', reason: '' });
      fetchLogs();
      appendActivity(
        'manual_attendance',
        `Manual ${submitted.direction.toUpperCase()} ${submitted.status} for ${submitted.employee_id} (${employee.name})`
      );

      const statusLabel = STATUS_LABELS_ID[submitted.status] || submitted.status;
      const tone = toastToneForStatus(submitted.status);

      const title =
        submitted.status === 'present'
          ? 'Presensi Manual Berhasil'
          : `${statusLabel} Berhasil Ditambahkan`;
      const message =
        submitted.status === 'present'
          ? `Presensi Manual Berhasil ditambahkan untuk ID ${submitted.employee_id} (${employee.name})`
          : `${statusLabel} berhasil ditambahkan untuk ID ${submitted.employee_id} (${employee.name})`;

      pushToast(tone, title, message);
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      pushToast('error', 'Gagal menambahkan presensi', String(detail || 'Periksa koneksi atau coba lagi.'));
    }
  };

  const handleAddEmployee = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!faceFile) {
      pushToast('warning', 'Face scan diperlukan', 'Klik Start Scan lalu Capture terlebih dahulu.');
      return;
    }
    if (!newEmployee.division || !newEmployee.position) {
      pushToast('warning', 'Field belum lengkap', 'Pilih Division dan Position.');
      return;
    }
    try {
      const created = await api.post('/employees/', newEmployee);
      const createdEmployee: Employee = created.data;
      const fd = new FormData();
      fd.append('face_image', faceFile);
      await api.post(`/employees/${createdEmployee.id}/face`, fd, {
        headers: { 'Content-Type': 'multipart/form-data' },
      });
      const justAddedName = newEmployee.name;
      setNewEmployee({ name: '', dob: '', division: '', position: '' });
      setFaceFile(null);
      if (capturedPreviewUrl) URL.revokeObjectURL(capturedPreviewUrl);
      setCapturedPreviewUrl('');
      await fetchEmployees();
      if (isManager) await fetchRoles();
      appendActivity('employee_create', `Employee created: ${createdEmployee.id} - ${justAddedName}`);
      pushToast(
        'success',
        'Employee berhasil ditambahkan',
        `ID ${createdEmployee.id}${justAddedName ? ` (${justAddedName})` : ''}`
      );
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      let msg = '';
      if (typeof detail === 'string') msg = detail;
      else if (Array.isArray(detail)) msg = detail.map((d: any) => d?.msg || JSON.stringify(d)).join('; ');
      else if (!err?.response) msg = 'Tidak bisa terhubung ke API.';
      else msg = `Gagal (${err.response.status || '?'}). Coba lagi.`;
      pushToast('error', 'Gagal menambahkan employee', msg);
    }
  };

  const handleDeleteEmployee = async (empId: string) => {
    const target = employees.find(e => e.id === empId);
    const label = target?.name ? `${target.name} (ID ${empId})` : `ID ${empId}`;
    askConfirm({
      title: 'Hapus Employee',
      message: `Apakah kamu yakin ingin menghapus ${label}? Foto wajah dan data terkait juga akan dihapus.`,
      confirmLabel: 'Hapus Employee',
      tone: 'danger',
      onConfirm: async () => {
        try {
          await api.delete(`/employees/${empId}`);
          pushToast('info', 'Employee dihapus', label);
          await fetchEmployees();
          if (isManager) await fetchRoles();
        } catch (err) {
          pushToast('error', 'Gagal menghapus employee', 'Coba lagi.');
        }
      },
    });
  };

  const handleLogout = () => {
    localStorage.clear();
    window.location.href = '/login';
  };

  const filteredLogs = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    if (!q) return logs;
    return logs.filter(l => {
      const name = l.employee?.name?.toLowerCase() || '';
      const id = (l.employee_id || '').toLowerCase();
      return name.includes(q) || id.includes(q);
    });
  }, [logs, searchQuery]);

  const filteredEmployees = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    if (!q) return employees;
    return employees.filter(e => {
      return e.name.toLowerCase().includes(q) || e.id.toLowerCase().includes(q);
    });
  }, [employees, searchQuery]);

  // Live Attendance: today-only logs (auto resets when day changes)
  const liveTodayLogs = useMemo(() => {
    const todayKey = format(now, 'yyyy-MM-dd');
    return logs.filter(l => {
      try {
        const d = new Date(l.timestamp);
        if (isNaN(d.getTime())) return false;
        return format(d, 'yyyy-MM-dd') === todayKey;
      } catch {
        return false;
      }
    });
  }, [logs, now]);

  const livePerPage = 5;
  const liveTotalPages = Math.max(1, Math.ceil(liveTodayLogs.length / livePerPage));
  useEffect(() => {
    if (livePage > liveTotalPages) setLivePage(liveTotalPages);
    if (livePage < 1) setLivePage(1);
  }, [livePage, liveTotalPages]);
  const pagedLiveLogs = useMemo(() => {
    const start = (livePage - 1) * livePerPage;
    return liveTodayLogs.slice(start, start + livePerPage);
  }, [liveTodayLogs, livePage]);

  // Login Activity: 5 rows per page
  const loginAuditPerPage = 5;
  const loginAuditTotalPages = Math.max(1, Math.ceil(loginAudit.length / loginAuditPerPage));
  useEffect(() => {
    if (loginAuditPage > loginAuditTotalPages) setLoginAuditPage(loginAuditTotalPages);
    if (loginAuditPage < 1) setLoginAuditPage(1);
  }, [loginAuditPage, loginAuditTotalPages]);
  const pagedLoginAudit = useMemo(() => {
    const start = (loginAuditPage - 1) * loginAuditPerPage;
    return loginAudit.slice(start, start + loginAuditPerPage);
  }, [loginAudit, loginAuditPage]);

  const buildPageButtons = (total: number, current: number): (number | '...')[] => {
    if (total <= 7) {
      const out: (number | '...')[] = [];
      for (let i = 1; i <= total; i++) out.push(i);
      return out;
    }
    const out: (number | '...')[] = [1];
    const left = Math.max(2, current - 1);
    const right = Math.min(total - 1, current + 1);
    if (left > 2) out.push('...');
    for (let i = left; i <= right; i++) out.push(i);
    if (right < total - 1) out.push('...');
    out.push(total);
    return out;
  };

  const logsPerPage = 30;
  const logsTotalPages = Math.max(1, Math.ceil(filteredLogs.length / logsPerPage));

  useEffect(() => {
    if (logsPage > logsTotalPages) setLogsPage(logsTotalPages);
    if (logsPage < 1) setLogsPage(1);
  }, [logsPage, logsTotalPages]);

  const pagedLogs = useMemo(() => {
    const start = (logsPage - 1) * logsPerPage;
    return filteredLogs.slice(start, start + logsPerPage);
  }, [filteredLogs, logsPage]);

  const logsPageButtons = useMemo(() => {
    const pages: (number | '...')[] = [];
    if (logsTotalPages <= 7) {
      for (let i = 1; i <= logsTotalPages; i++) pages.push(i);
      return pages;
    }
    pages.push(1);
    const left = Math.max(2, logsPage - 1);
    const right = Math.min(logsTotalPages - 1, logsPage + 1);
    if (left > 2) pages.push('...');
    for (let i = left; i <= right; i++) pages.push(i);
    if (right < logsTotalPages - 1) pages.push('...');
    pages.push(logsTotalPages);
    return pages;
  }, [logsPage, logsTotalPages]);

  const commitLogsPageInput = () => {
    const n = Number(logsPageInput);
    if (!Number.isFinite(n)) return;
    const asInt = Math.floor(n);
    const clamped = Math.min(logsTotalPages, Math.max(1, asInt));
    setLogsPage(clamped);
  };

  const employeesPerPage = 15;
  const employeesTotalPages = Math.max(1, Math.ceil(filteredEmployees.length / employeesPerPage));

  useEffect(() => {
    if (employeesPage > employeesTotalPages) setEmployeesPage(employeesTotalPages);
    if (employeesPage < 1) setEmployeesPage(1);
  }, [employeesPage, employeesTotalPages]);

  const pagedEmployees = useMemo(() => {
    const start = (employeesPage - 1) * employeesPerPage;
    return filteredEmployees.slice(start, start + employeesPerPage);
  }, [filteredEmployees, employeesPage]);

  const employeePageButtons = useMemo(() => {
    const pages: (number | '...')[] = [];
    if (employeesTotalPages <= 7) {
      for (let i = 1; i <= employeesTotalPages; i++) pages.push(i);
      return pages;
    }
    pages.push(1);
    const left = Math.max(2, employeesPage - 1);
    const right = Math.min(employeesTotalPages - 1, employeesPage + 1);
    if (left > 2) pages.push('...');
    for (let i = left; i <= right; i++) pages.push(i);
    if (right < employeesTotalPages - 1) pages.push('...');
    pages.push(employeesTotalPages);
    return pages;
  }, [employeesPage, employeesTotalPages]);

  const commitEmployeesPageInput = () => {
    const n = Number(employeesPageInput);
    if (!Number.isFinite(n)) return;
    const asInt = Math.floor(n);
    const clamped = Math.min(employeesTotalPages, Math.max(1, asInt));
    setEmployeesPage(clamped);
  };

  const unreadNotificationsCount = useMemo(() => {
    let readAt = 0;
    try {
      readAt = notificationsReadAt ? new Date(notificationsReadAt).getTime() : 0;
      if (Number.isNaN(readAt)) readAt = 0;
    } catch {
      readAt = 0;
    }
    return activityEvents.filter(e => {
      try {
        const t = new Date(e.ts).getTime();
        return !Number.isNaN(t) && t > readAt;
      } catch {
        return false;
      }
    }).length;
  }, [activityEvents, notificationsReadAt]);

  const toggleNotifications = () => {
    setNotificationsOpen(prev => {
      const next = !prev;
      if (next) {
        const now = new Date().toISOString();
        setNotificationsReadAt(now);
        try {
          localStorage.setItem('notifications_read_at', now);
        } catch {}
      }
      return next;
    });
  };

  const notificationTargetTab = (type: string) => {
    if (type === 'employee_create') return 'employees';
    if (type === 'manual_attendance') return 'logs';
    if (type === 'security_alert') return 'system';
    if (type === 'login') return 'system';
    return 'system';
  };

  const chartData = useMemo(() => {
    return logs
      .slice(0, 24)
      .reverse()
      .map(l => {
        let timeStr = '00:00';
        try {
          const date = new Date(l.timestamp);
          if (!isNaN(date.getTime())) {
            timeStr = format(date, 'HH:mm');
          }
        } catch (e) {}
        return {
          time: timeStr,
          present: l.status === 'present' ? 1 : 0,
        };
      });
  }, [logs]);

  const exportCsv = () => {
    const rows = filteredLogs.map(l => ({
      employee_id: l.employee_id,
      name: l.employee?.name,
      division: l.employee?.division,
      position: l.employee?.position,
      date: (() => {
        try {
          const d = new Date(l.timestamp);
          return !isNaN(d.getTime()) ? format(d, 'yyyy-MM-dd') : '';
        } catch {
          return '';
        }
      })(),
      time: (() => {
        try {
          const d = new Date(l.timestamp);
          return !isNaN(d.getTime()) ? format(d, 'HH:mm:ss') : '';
        } catch {
          return '';
        }
      })(),
      direction: l.direction || '',
      status: l.status,
      reason: l.reason || '',
    }));

    const header = Object.keys(rows[0] || { employee_id: '', name: '', division: '', position: '', date: '', time: '', direction: '', status: '', reason: '' });
    const escape = (v: any) => `"${String(v ?? '').replaceAll('"', '""')}"`;
    const csv = [
      header.join(','),
      ...rows.map(r => header.map(h => escape((r as any)[h])).join(',')),
    ].join('\n');

    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `attendance_${selectedDate || 'all'}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="flex min-h-screen h-[100dvh] bg-slate-50 dark:bg-slate-950 overflow-hidden font-sans text-slate-900 dark:text-slate-100">
      {isMobile && mobileNavOpen && (
        <div
          className="fixed inset-0 bg-black/50 z-40"
          onClick={() => setMobileNavOpen(false)}
        />
      )}
      {/* Sidebar */}
      <motion.aside 
        initial={false}
        animate={{ width: isMobile ? '280px' : (isSidebarCollapsed ? '80px' : '280px') }}
        className={`bg-slate-900 text-slate-300 flex flex-col shadow-2xl overflow-hidden ${
          isMobile
            ? `fixed inset-y-0 left-0 z-50 w-[280px] max-w-[85vw] transform transition-transform duration-300 ease-in-out ${
                mobileNavOpen ? 'translate-x-0' : '-translate-x-full'
              }`
            : 'relative z-50 transition-all duration-300 ease-in-out'
        }`}
      >
        <div className="p-4 sm:p-6 flex items-center justify-between">
          {!isSidebarCollapsed && (
            <motion.div 
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              className="text-xl font-black bg-gradient-to-r from-indigo-400 to-cyan-400 bg-clip-text text-transparent"
            >
              SAPA DASHBOARD
            </motion.div>
          )}
          {isMobile ? (
            <button
              onClick={() => setMobileNavOpen(false)}
              className="p-2 hover:bg-slate-800 rounded-lg transition-colors text-slate-400 hover:text-white"
            >
              <X size={20} />
            </button>
          ) : (
            <button 
              onClick={() => setIsSidebarCollapsed(!isSidebarCollapsed)}
              className="p-2 hover:bg-slate-800 rounded-lg transition-colors text-slate-400 hover:text-white"
            >
              {isSidebarCollapsed ? <ChevronRight size={20} /> : <ChevronLeft size={20} />}
            </button>
          )}
        </div>

        <div className="flex-1 overflow-y-auto overscroll-contain">
          <nav className="px-3 sm:px-4 space-y-2 mt-4">
            <SidebarItem
              icon={<LayoutDashboard size={20} />}
              label="Dashboard"
              active={activeTab === 'dashboard'}
              collapsed={isSidebarCollapsed}
              onClick={() => {
                setActiveTab('dashboard');
                if (isMobile) setMobileNavOpen(false);
              }}
            />
            {showEmployeesTab && (
              <SidebarItem
                icon={<Users size={20} />}
                label="Employees"
                active={activeTab === 'employees'}
                collapsed={isSidebarCollapsed}
                onClick={() => {
                  setActiveTab('employees');
                  if (isMobile) setMobileNavOpen(false);
                }}
              />
            )}
            {showLogsTab && (
              <SidebarItem
                icon={<ClipboardList size={20} />}
                label="Logs"
                active={activeTab === 'logs'}
                collapsed={isSidebarCollapsed}
                onClick={() => {
                  setActiveTab('logs');
                  if (isMobile) setMobileNavOpen(false);
                }}
              />
            )}
            {showManualTab && (
              <SidebarItem
                icon={<Clock size={20} />}
                label="Manual Input"
                active={activeTab === 'manual'}
                collapsed={isSidebarCollapsed}
                onClick={() => {
                  setActiveTab('manual');
                  if (isMobile) setMobileNavOpen(false);
                }}
              />
            )}
            {showAddEmployeeTab && (
              <SidebarItem
                icon={<UserPlus size={20} />}
                label="Add Employee"
                active={activeTab === 'add_employee'}
                collapsed={isSidebarCollapsed}
                onClick={() => {
                  setActiveTab('add_employee');
                  if (isMobile) setMobileNavOpen(false);
                }}
              />
            )}
            {showRolesTab && (
              <SidebarItem
                icon={<Briefcase size={20} />}
                label="Role Employee"
                active={activeTab === 'roles'}
                collapsed={isSidebarCollapsed}
                onClick={() => {
                  setActiveTab('roles');
                  if (isMobile) setMobileNavOpen(false);
                }}
              />
            )}
            {showSystemTab && (
              <SidebarItem
                icon={<Settings size={20} />}
                label="System"
                active={activeTab === 'system'}
                collapsed={isSidebarCollapsed}
                onClick={() => {
                  setActiveTab('system');
                  if (isMobile) setMobileNavOpen(false);
                }}
              />
            )}
          </nav>
        </div>

        <div className="p-4 border-t border-slate-800">
          <div
            onClick={() => {
              setActiveTab('profile');
              if (isMobile) setMobileNavOpen(false);
            }}
            className="flex items-center p-2 rounded-xl hover:bg-slate-800 transition-all group cursor-pointer"
          >
            <div className="w-10 h-10 rounded-full bg-gradient-to-tr from-indigo-500 to-purple-500 flex items-center justify-center text-white font-bold shadow-lg shrink-0">
              {username?.[0].toUpperCase()}
            </div>
            {!isSidebarCollapsed && (
              <div className="ml-3 overflow-hidden">
                <p className="text-sm font-bold text-white truncate">{username}</p>
                <p className="text-xs text-slate-500 capitalize">{role}</p>
              </div>
            )}
            {!isSidebarCollapsed && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  handleLogout();
                }}
                className="ml-auto text-slate-500 hover:text-red-400 transition-colors p-2"
              >
                <LogOut size={18} />
              </button>
            )}
          </div>
        </div>
      </motion.aside>

      {/* Main Content */}
      <main className="flex-1 flex flex-col min-w-0 bg-slate-50 dark:bg-slate-950">
        {/* Top Header */}
        <header className="h-16 sm:h-20 bg-white dark:bg-slate-900 border-b border-slate-200 dark:border-slate-800 px-4 sm:px-8 flex items-center justify-between shrink-0 z-40">
          <div className="flex items-center gap-3 flex-1 min-w-0 sm:max-w-xl">
            <button
              onClick={() => setMobileNavOpen(true)}
              className="sm:hidden p-2.5 text-slate-500 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800 rounded-xl transition-all shrink-0"
            >
              <Menu size={20} />
            </button>
            <div className="relative w-full group">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400 group-focus-within:text-indigo-500 transition-colors" size={18} />
              <input 
                type="text" 
                placeholder="Search employees or logs..."
                className="w-full pl-10 pr-4 py-2.5 bg-slate-50 dark:bg-slate-800 border-none rounded-2xl text-sm focus:ring-2 focus:ring-indigo-500/20 transition-all outline-none text-slate-900 dark:text-slate-100 placeholder:text-slate-400"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
              />
            </div>
          </div>

          <div className="flex items-center space-x-2 sm:space-x-4 ml-3 sm:ml-6">
            <button
              onClick={() => setTheme(t => (t === 'dark' ? 'light' : 'dark'))}
              className="p-2.5 text-slate-500 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800 rounded-xl transition-all"
            >
              {theme === 'dark' ? <Sun size={20} /> : <Moon size={20} />}
            </button>
            <div ref={notificationsRef} className="relative">
              <button
                onClick={toggleNotifications}
                className="p-2.5 text-slate-500 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800 rounded-xl relative transition-all"
              >
                <Bell size={20} />
                {unreadNotificationsCount > 0 && (
                  <span className="absolute -top-1 -right-1 min-w-5 h-5 px-1 bg-red-500 text-white rounded-full text-[10px] font-black flex items-center justify-center border-2 border-white">
                    {unreadNotificationsCount > 99 ? '99+' : unreadNotificationsCount}
                  </span>
                )}
              </button>

              {notificationsOpen && (
                <div className="absolute right-0 mt-3 w-[calc(100vw-2rem)] max-w-[360px] bg-white dark:bg-slate-900 rounded-3xl shadow-2xl border border-slate-100 dark:border-slate-800 overflow-hidden z-50">
                  <div className="px-5 py-4 border-b border-slate-100 dark:border-slate-800 flex items-center justify-between">
                    <div>
                      <div className="text-sm font-black text-slate-800 dark:text-slate-100">Notifications</div>
                      <div className="text-xs font-bold text-slate-500 dark:text-slate-400">Login, employee updates, and alerts</div>
                    </div>
                    <button
                      onClick={() => {
                        setActivityEvents([]);
                        try {
                          localStorage.removeItem(activityKey);
                        } catch {}
                        const now = new Date().toISOString();
                        setNotificationsReadAt(now);
                        try {
                          localStorage.setItem('notifications_read_at', now);
                        } catch {}
                      }}
                      className="px-3 py-2 rounded-2xl bg-slate-50 dark:bg-slate-800 hover:bg-slate-100 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-100 text-xs font-black transition-all border border-slate-200 dark:border-slate-700"
                    >
                      Clear
                    </button>
                  </div>
                  <div className="max-h-[420px] overflow-y-auto p-3">
                    {activityEvents.length === 0 ? (
                      <div className="px-4 py-10 text-center text-sm font-bold text-slate-400">
                        No notifications.
                      </div>
                    ) : (
                      <div className="space-y-2">
                        {activityEvents.slice(0, 12).map((e) => {
                          const isAlert = e.type === 'security_alert';
                          return (
                            <button
                              key={`${e.ts}-${e.type}-${e.message}`}
                              onClick={() => {
                                setActiveTab(notificationTargetTab(e.type) as any);
                                setNotificationsOpen(false);
                              }}
                              className="w-full text-left px-4 py-3 rounded-2xl hover:bg-slate-50 dark:hover:bg-slate-800 transition-all border border-transparent hover:border-slate-100 dark:hover:border-slate-800"
                            >
                              <div className="flex items-start justify-between gap-4">
                                <div className="min-w-0">
                                  <div className={isAlert ? 'text-[10px] font-black uppercase tracking-widest text-red-600' : 'text-[10px] font-black uppercase tracking-widest text-slate-400'}>
                                    {e.type.replaceAll('_', ' ')}
                                  </div>
                                  <div className="text-sm font-bold text-slate-800 dark:text-slate-100 truncate">
                                    {e.message}
                                  </div>
                                </div>
                                <div className="text-xs font-bold text-slate-400 whitespace-nowrap">
                                  {(() => {
                                    try {
                                      const d = new Date(e.ts);
                                      return !isNaN(d.getTime()) ? format(d, 'HH:mm') : '';
                                    } catch {
                                      return '';
                                    }
                                  })()}
                                </div>
                              </div>
                            </button>
                          );
                        })}
                      </div>
                    )}
                  </div>
                </div>
              )}
            </div>
            <div className="h-8 w-px bg-slate-200 dark:bg-slate-700 mx-2"></div>
            <div className="text-right hidden sm:block">
              <p className="text-xs text-slate-400 dark:text-slate-300 font-medium">Today's Date</p>
              <p className="text-sm font-bold text-slate-700 dark:text-slate-100">{format(new Date(), 'EEEE, dd MMM')}</p>
            </div>
          </div>
        </header>

        {/* Scrollable Content */}
        <div className="flex-1 overflow-y-auto overscroll-contain p-4 sm:p-8 pb-24 sm:pb-8 space-y-8 scroll-smooth">
          {activeTab === 'dashboard' && (
            <>
          <motion.div 
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6"
          >
            <StatCard 
              title="Active Employees" 
              value={logs.filter(l => l.status === 'present').length} 
              icon={<CheckCircle2 className="text-emerald-500" />} 
              color="emerald"
              trend="+12%"
            />
            <StatCard 
              title="Sick/Permission" 
              value={logs.filter(l => l.status !== 'present').length} 
              icon={<AlertCircle className="text-amber-500" />} 
              color="amber"
              trend="-2%"
            />
            <StatCard 
              title="System Health" 
              value="Optimal" 
              icon={<LayoutDashboard className="text-indigo-500" />} 
              color="indigo"
              isString
            />
            <StatCard 
              title="Edge Connection" 
              value="Stable" 
              icon={<Camera className="text-cyan-500" />} 
              color="cyan"
              isString
            />
          </motion.div>

          <div className="grid grid-cols-1 xl:grid-cols-3 gap-8">
            {/* Real-time Camera Feed */}
            <motion.div 
              initial={{ opacity: 0, scale: 0.95 }}
              animate={{ opacity: 1, scale: 1 }}
              className="xl:col-span-2 bg-slate-900 rounded-3xl p-1 shadow-2xl overflow-hidden group"
            >
              <div className="relative aspect-video rounded-[22px] overflow-hidden bg-slate-800">
                {streamUrl ? (
                  <img
                    src={edgeFrameSrc}
                    alt=""
                    className={`w-full h-full object-cover transform -scale-x-100 opacity-90 group-hover:opacity-100 transition-opacity ${edgeFrameOk ? '' : 'invisible'}`}
                    onLoad={() => setEdgeFrameOk(true)}
                    onError={() => setEdgeFrameOk(false)}
                  />
                ) : (
                  <div className="absolute inset-0 flex flex-col items-center justify-center text-slate-600">
                    <Camera size={64} className="mb-4 animate-pulse" />
                    <p className="text-sm font-medium">NO CAMERA SOURCE</p>
                  </div>
                )}
                {streamUrl && !edgeFrameOk && (
                  <div className="absolute inset-0 flex flex-col items-center justify-center bg-black/40 text-white px-4 text-center">
                    <Camera size={56} className="mb-3 animate-pulse" />
                    <div className="text-sm font-black uppercase tracking-widest">Waiting for Edge Camera</div>
                    <div className="mt-1 text-xs font-semibold text-white/80">
                      Buka halaman <span className="font-black">/edge</span> di edge device untuk mengaktifkan kamera.
                    </div>
                  </div>
                )}
                {/* Overlay UI - hidden until frame is live */}
                {edgeFrameOk && (
                  <div className="absolute inset-0 pointer-events-none p-6 flex flex-col justify-between">
                    <div className="flex justify-between items-start">
                      <div className="bg-black/40 backdrop-blur-md px-3 py-1.5 rounded-full border border-white/10 flex items-center space-x-2">
                        <div className="w-2 h-2 rounded-full bg-red-500 animate-ping"></div>
                        <span className="text-[10px] font-black text-white uppercase tracking-widest">Edge Camera</span>
                      </div>
                      <div className="bg-black/40 backdrop-blur-md px-3 py-1.5 rounded-full border border-white/10 text-[10px] font-bold text-white/70">
                        {format(now, 'HH:mm:ss')}
                      </div>
                    </div>
                    <div className="flex justify-between items-end">
                      <div className="bg-black/40 backdrop-blur-md px-3 py-1.5 rounded-full border border-white/10 text-[10px] font-bold text-white/70">
                        Source: /edge
                      </div>
                      <div className="opacity-0">.</div>
                    </div>
                    {/* Scanline Effect */}
                    <div className="absolute inset-0 bg-[linear-gradient(rgba(18,16,16,0)_50%,rgba(0,0,0,0.1)_50%),linear-gradient(90deg,rgba(255,0,0,0.03),rgba(0,255,0,0.01),rgba(0,0,255,0.03))] bg-[length:100%_2px,3px_100%] pointer-events-none opacity-20"></div>
                  </div>
                )}
              </div>
            </motion.div>

            {/* Live Attendance (right column, beside the edge camera) */}
            <motion.div
              initial={{ opacity: 0, x: 20 }}
              animate={{ opacity: 1, x: 0 }}
              className="bg-white dark:bg-slate-900 rounded-3xl shadow-xl border border-slate-100 dark:border-slate-800 overflow-hidden flex flex-col"
            >
              <div className="p-5 border-b border-slate-50 dark:border-slate-800 flex items-start justify-between gap-3">
                <div>
                  <h3 className="text-base font-black text-slate-800 dark:text-slate-100 flex items-center">
                    <ClipboardList className="mr-2 text-indigo-500" size={18} /> Live Attendance
                  </h3>
                  <p className="text-xs text-slate-500 dark:text-slate-400 font-medium mt-0.5">
                    Hari ini ({format(now, 'yyyy-MM-dd')}) — auto-reset
                  </p>
                </div>
                <div className="text-[10px] font-black text-slate-400 uppercase tracking-widest whitespace-nowrap">
                  {liveTodayLogs.length} entri
                </div>
              </div>
              <div className="p-4 flex-1 flex flex-col">
                {liveTodayLogs.length === 0 ? (
                  <div className="flex-1 flex items-center justify-center text-sm font-bold text-slate-400 py-8 text-center">
                    Belum ada presensi hari ini.
                  </div>
                ) : (
                  <>
                    <div className="space-y-2 flex-1">
                      {pagedLiveLogs.map((l) => (
                        <div key={`${l.employee_id}-${l.timestamp}-${l.direction}`} className="flex items-start justify-between bg-slate-50 dark:bg-slate-800/60 rounded-2xl px-4 py-3">
                          <div className="min-w-0">
                            <div className="text-[9px] font-black uppercase tracking-widest text-slate-400">
                              {(() => {
                                const d = (l.direction || 'in').toLowerCase();
                                if (d === 'in') return 'ATTEND';
                                if (d === 'out') return 'PRESENT';
                                return (l.direction || '').toUpperCase();
                              })()} {l.status}
                            </div>
                            <div className="text-xs font-black text-slate-800 dark:text-slate-100 truncate">
                              {l.employee?.name ? `${l.employee.name} (${l.employee_id})` : l.employee_id}
                            </div>
                            {l.reason && (
                              <div className="text-[10px] font-bold text-slate-500 dark:text-slate-400 truncate">
                                {l.reason}
                              </div>
                            )}
                          </div>
                          <div className="text-[10px] font-bold text-slate-400 ml-3 whitespace-nowrap">
                            {(() => {
                              try {
                                const d = new Date(l.timestamp);
                                return !isNaN(d.getTime()) ? format(d, 'HH:mm:ss') : '';
                              } catch {
                                return '';
                              }
                            })()}
                          </div>
                        </div>
                      ))}
                    </div>
                    {liveTotalPages > 1 && (
                      <div className="mt-4 flex items-center justify-center gap-1.5 flex-wrap">
                        <button
                          onClick={() => setLivePage(p => Math.max(1, p - 1))}
                          disabled={livePage <= 1}
                          className="px-2.5 py-1.5 rounded-lg bg-slate-50 dark:bg-slate-800 hover:bg-slate-100 dark:hover:bg-slate-700 disabled:opacity-50 text-slate-700 dark:text-slate-100 text-[10px] font-black transition-all border border-slate-200 dark:border-slate-700"
                        >
                          Prev
                        </button>
                        {buildPageButtons(liveTotalPages, livePage).map((p, idx) =>
                          p === '...' ? (
                            <div key={`live-dots-${idx}`} className="px-1.5 text-slate-400 dark:text-slate-500 text-[10px] font-black">…</div>
                          ) : (
                            <button
                              key={`live-${p}`}
                              onClick={() => setLivePage(p)}
                              className={
                                p === livePage
                                  ? 'px-2.5 py-1.5 rounded-lg bg-indigo-600 text-white text-[10px] font-black transition-all shadow-md shadow-indigo-500/20'
                                  : 'px-2.5 py-1.5 rounded-lg bg-slate-50 dark:bg-slate-800 hover:bg-slate-100 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-100 text-[10px] font-black transition-all border border-slate-200 dark:border-slate-700'
                              }
                            >
                              {p}
                            </button>
                          )
                        )}
                        <button
                          onClick={() => setLivePage(p => Math.min(liveTotalPages, p + 1))}
                          disabled={livePage >= liveTotalPages}
                          className="px-2.5 py-1.5 rounded-lg bg-slate-50 dark:bg-slate-800 hover:bg-slate-100 dark:hover:bg-slate-700 disabled:opacity-50 text-slate-700 dark:text-slate-100 text-[10px] font-black transition-all border border-slate-200 dark:border-slate-700"
                        >
                          Next
                        </button>
                      </div>
                    )}
                  </>
                )}
              </div>
            </motion.div>
          </div>

          {/* Hourly Activity - full width below */}
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            className="bg-white dark:bg-slate-900 rounded-3xl p-6 shadow-xl border border-slate-100 dark:border-slate-800 flex flex-col"
          >
            <div className="flex items-center justify-between mb-6">
              <h3 className="text-lg font-bold text-slate-800 dark:text-slate-100 flex items-center">
                <ClipboardList className="mr-2 text-indigo-500" /> Hourly Activity
              </h3>
              <div className="flex items-center gap-2 px-3 py-1.5 bg-slate-50 dark:bg-slate-800 rounded-xl text-xs font-bold text-slate-700 dark:text-slate-200">
                <UserPlus size={14} className="text-indigo-500" />
                Recent: <span className="font-black">{logs.length}</span>
              </div>
            </div>
            <div className="h-72">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={chartData}>
                  <defs>
                    <linearGradient id="colorPresent" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="#6366f1" stopOpacity={0.3}/>
                      <stop offset="95%" stopColor="#6366f1" stopOpacity={0}/>
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#f1f5f9" />
                  <XAxis dataKey="time" axisLine={false} tickLine={false} tick={{fontSize: 10, fill: '#94a3b8'}} />
                  <YAxis hide />
                  <Tooltip
                    contentStyle={{ borderRadius: '12px', border: 'none', boxShadow: '0 10px 15px -3px rgb(0 0 0 / 0.1)' }}
                  />
                  <Area type="monotone" dataKey="present" stroke="#6366f1" strokeWidth={3} fillOpacity={1} fill="url(#colorPresent)" />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          </motion.div>
            </>
          )}

          {activeTab === 'employees' && showEmployeesTab && (
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              className="bg-white dark:bg-slate-900 rounded-3xl shadow-xl border border-slate-100 dark:border-slate-800 overflow-hidden"
            >
              <div className="p-4 sm:p-8 border-b border-slate-50 dark:border-slate-800 flex items-center justify-between">
                <div>
                  <h2 className="text-2xl font-black text-slate-800 dark:text-slate-100">Employees</h2>
                  <p className="text-sm text-slate-500 dark:text-slate-400 font-medium">All registered employees</p>
                </div>
                <button
                  onClick={fetchEmployees}
                  className="px-4 py-2 bg-slate-50 dark:bg-slate-800 hover:bg-slate-100 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-100 rounded-xl text-sm font-black transition-all border border-slate-200 dark:border-slate-700"
                >
                  Refresh
                </button>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-left">
                  <thead>
                    <tr className="bg-slate-50/50 dark:bg-slate-800/50">
                      <th className="px-4 sm:px-8 py-3 sm:py-4 text-[10px] sm:text-xs font-black text-slate-400 uppercase tracking-widest">ID</th>
                      <th className="px-4 sm:px-8 py-3 sm:py-4 text-[10px] sm:text-xs font-black text-slate-400 uppercase tracking-widest">Name</th>
                      <th className="px-4 sm:px-8 py-3 sm:py-4 text-[10px] sm:text-xs font-black text-slate-400 uppercase tracking-widest">Division</th>
                      <th className="px-4 sm:px-8 py-3 sm:py-4 text-[10px] sm:text-xs font-black text-slate-400 uppercase tracking-widest">Position</th>
                      <th className="px-4 sm:px-8 py-3 sm:py-4 text-[10px] sm:text-xs font-black text-slate-400 uppercase tracking-widest text-right">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-50 dark:divide-slate-800">
                    {pagedEmployees.map(e => (
                      <tr key={e.id} className="hover:bg-slate-50/80 dark:hover:bg-slate-800/40 transition-all">
                        <td className="px-4 sm:px-8 py-4 sm:py-5 text-xs sm:text-sm font-black text-indigo-600">{e.id}</td>
                        <td className="px-4 sm:px-8 py-4 sm:py-5 text-xs sm:text-sm font-bold text-slate-800 dark:text-slate-100">{e.name}</td>
                        <td className="px-4 sm:px-8 py-4 sm:py-5 text-xs sm:text-sm font-bold text-slate-600 dark:text-slate-300">{e.division}</td>
                        <td className="px-4 sm:px-8 py-4 sm:py-5 text-xs sm:text-sm font-bold text-slate-600 dark:text-slate-300">{e.position}</td>
                        <td className="px-4 sm:px-8 py-4 sm:py-5 text-right">
                          <div className="flex items-center justify-end gap-2">
                            <button
                              onClick={() => openFaceViewer(e.id, e.name)}
                              className="inline-flex items-center px-3 py-2 rounded-xl bg-blue-50 dark:bg-blue-900/20 hover:bg-blue-100 dark:hover:bg-blue-900/30 text-blue-600 text-xs font-black transition-all border border-blue-200 dark:border-blue-900/40"
                            >
                              <Eye size={14} className="mr-2" /> View Faces
                            </button>
                            {role === 'manager' && (
                              <button
                                onClick={() => handleDeleteEmployee(e.id)}
                                className="inline-flex items-center px-3 py-2 rounded-xl bg-red-50 dark:bg-red-900/20 hover:bg-red-100 dark:hover:bg-red-900/30 text-red-600 text-xs font-black transition-all border border-red-200 dark:border-red-900/40"
                              >
                                <Trash2 size={14} className="mr-2" /> Delete
                              </button>
                            )}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="p-6 border-t border-slate-100 flex flex-col sm:flex-row items-center justify-between gap-4">
                <div className="text-xs font-bold text-slate-500">
                  Showing {(filteredEmployees.length === 0) ? 0 : ((employeesPage - 1) * employeesPerPage + 1)}–
                  {Math.min(employeesPage * employeesPerPage, filteredEmployees.length)} of {filteredEmployees.length}
                </div>
                <div className="flex items-center gap-2 flex-wrap justify-center">
                  <button
                    onClick={() => setEmployeesPage(p => Math.max(1, p - 1))}
                    disabled={employeesPage <= 1}
                    className="px-3 py-2 rounded-xl bg-slate-50 hover:bg-slate-100 disabled:opacity-50 text-slate-700 text-xs font-black transition-all border border-slate-200"
                  >
                    Prev
                  </button>
                  {employeePageButtons.map((p, idx) =>
                    p === '...' ? (
                      <div key={`dots-${idx}`} className="px-2 text-slate-400 text-xs font-black">
                        ...
                      </div>
                    ) : (
                      <button
                        key={p}
                        onClick={() => setEmployeesPage(p)}
                        className={
                          p === employeesPage
                            ? 'px-3 py-2 rounded-xl bg-indigo-600 text-white text-xs font-black transition-all shadow-lg shadow-indigo-500/20'
                            : 'px-3 py-2 rounded-xl bg-slate-50 hover:bg-slate-100 text-slate-700 text-xs font-black transition-all border border-slate-200'
                        }
                      >
                        {p}
                      </button>
                    )
                  )}
                  <div className="flex items-center gap-2 px-2">
                    <div className="text-[10px] font-black uppercase tracking-widest text-slate-400">
                      Page
                    </div>
                  <input
                      type="number"
                      min={1}
                      max={employeesTotalPages}
                      value={employeesPageInput}
                      onChange={(e) => setEmployeesPageInput(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') {
                          e.preventDefault();
                          commitEmployeesPageInput();
                        }
                      }}
                      className="w-20 px-3 py-2 rounded-xl bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 text-slate-700 dark:text-slate-100 text-xs font-black outline-none focus:ring-2 focus:ring-indigo-500/20"
                    />
                    <button
                      onClick={commitEmployeesPageInput}
                      className="px-3 py-2 rounded-xl bg-slate-900 hover:bg-slate-800 text-white text-xs font-black transition-all"
                    >
                      Go
                    </button>
                    <div className="text-xs font-bold text-slate-400">
                      / {employeesTotalPages}
                    </div>
                  </div>
                  <button
                    onClick={() => setEmployeesPage(p => Math.min(employeesTotalPages, p + 1))}
                    disabled={employeesPage >= employeesTotalPages}
                    className="px-3 py-2 rounded-xl bg-slate-50 hover:bg-slate-100 disabled:opacity-50 text-slate-700 text-xs font-black transition-all border border-slate-200"
                  >
                    Next
                  </button>
                </div>
              </div>
            </motion.div>
          )}

          {activeTab === 'manual' && showManualTab && (
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              className="bg-white dark:bg-slate-900 rounded-3xl shadow-xl border border-slate-100 dark:border-slate-800 overflow-hidden"
            >
              <div className="p-4 sm:p-8 border-b border-slate-50 dark:border-slate-800 flex items-center justify-between">
                <div>
                  <h2 className="text-2xl font-black text-slate-800 dark:text-slate-100">Manual Permission Input</h2>
                  <p className="text-sm text-slate-500 dark:text-slate-400 font-medium">Create manual permission/sick/present record</p>
                </div>
              </div>
              <div className="p-4 sm:p-8">
                <form onSubmit={handleManualSubmit} className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <div>
                    <input
                      type="text"
                      placeholder="Employee ID"
                      className="w-full bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl px-4 py-3 text-sm font-bold text-slate-900 dark:text-slate-100 placeholder-slate-500 dark:placeholder-slate-500 outline-none focus:ring-2 focus:ring-indigo-500/20"
                      value={manualAttendance.employee_id}
                      onChange={(e) => setManualAttendance({ ...manualAttendance, employee_id: e.target.value })}
                      required
                    />
                    {(() => {
                      const trimmed = manualAttendance.employee_id.trim();
                      if (!trimmed) return null;
                      const matched = employees.find(emp => emp.id === trimmed);
                      if (matched) {
                        return (
                          <div className="mt-2 rounded-xl bg-emerald-50 dark:bg-emerald-900/20 border border-emerald-100 dark:border-emerald-900/40 px-3 py-2.5">
                            <div className="text-[10px] font-black text-emerald-600 dark:text-emerald-400 uppercase tracking-widest inline-flex items-center">
                              <CheckCircle2 size={12} className="mr-1.5" /> Karyawan ditemukan
                            </div>
                            <div className="mt-1 grid grid-cols-1 sm:grid-cols-3 gap-2 text-xs font-bold text-slate-700 dark:text-slate-200">
                              <div>
                                <div className="text-[9px] font-black uppercase tracking-widest text-slate-400">Name</div>
                                <div className="truncate">{matched.name}</div>
                              </div>
                              <div>
                                <div className="text-[9px] font-black uppercase tracking-widest text-slate-400">Division</div>
                                <div className="truncate">{matched.division || '--'}</div>
                              </div>
                              <div>
                                <div className="text-[9px] font-black uppercase tracking-widest text-slate-400">Position</div>
                                <div className="truncate">{matched.position || '--'}</div>
                              </div>
                            </div>
                          </div>
                        );
                      }
                      return (
                        <div className="mt-2 px-3 py-2.5 rounded-xl bg-rose-50 dark:bg-rose-900/20 border border-rose-100 dark:border-rose-900/40 text-xs font-black text-rose-600 dark:text-rose-300 inline-flex items-center">
                          <AlertCircle size={12} className="mr-1.5" />
                          ID tidak ditemukan
                        </div>
                      );
                    })()}
                  </div>
                  <select
                    className="w-full bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl px-4 py-3 text-sm font-bold text-slate-900 dark:text-slate-100 outline-none focus:ring-2 focus:ring-indigo-500/20"
                    value={manualAttendance.direction}
                    onChange={(e) => setManualAttendance({ ...manualAttendance, direction: e.target.value })}
                  >
                    <option value="in">Attend</option>
                    <option value="out">Absent</option>
                  </select>
                  <select
                    className="w-full bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl px-4 py-3 text-sm font-bold text-slate-900 dark:text-slate-100 outline-none focus:ring-2 focus:ring-indigo-500/20"
                    value={manualAttendance.status}
                    onChange={(e) => setManualAttendance({ ...manualAttendance, status: e.target.value })}
                  >
                    <option value="permission">Permission</option>
                    <option value="sick">Sick</option>
                    <option value="present">Present</option>
                    <option value="leave">Leave</option>
                    <option value="half_day">Half Day</option>
                    <option value="other">Others</option>
                  </select>
                  <div className="md:col-span-2">
                    <textarea
                      placeholder="Reason"
                      className="w-full bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl px-4 py-3 text-sm font-bold text-slate-900 dark:text-slate-100 placeholder-slate-500 dark:placeholder-slate-500 outline-none focus:ring-2 focus:ring-indigo-500/20 min-h-28"
                      value={manualAttendance.reason}
                      onChange={(e) => setManualAttendance({ ...manualAttendance, reason: e.target.value })}
                    />
                  </div>
                  <div className="md:col-span-2">
                    <button className="w-full bg-indigo-600 hover:bg-indigo-700 text-white py-3 rounded-xl text-sm font-black transition-all shadow-lg shadow-indigo-500/20">
                      Submit
                    </button>
                  </div>
                </form>
              </div>
            </motion.div>
          )}

          {activeTab === 'add_employee' && showAddEmployeeTab && (
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              className="bg-white dark:bg-slate-900 rounded-3xl shadow-xl border border-slate-100 dark:border-slate-800 overflow-hidden"
            >
              <div className="p-4 sm:p-8 border-b border-slate-50 dark:border-slate-800 flex items-center justify-between">
                <div>
                  <h2 className="text-2xl font-black text-slate-800 dark:text-slate-100">Add Employee</h2>
                  <p className="text-sm text-slate-500 dark:text-slate-400 font-medium">Register a new employee + face scan</p>
                </div>
              </div>
              <div className="p-4 sm:p-8 grid grid-cols-1 xl:grid-cols-2 gap-6">
                <form onSubmit={handleAddEmployee} className="space-y-4">
                  <input
                    type="text"
                    value="Auto-generated (6 digit)"
                    disabled
                    className="w-full bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl px-4 py-3 text-sm font-bold outline-none text-slate-700 dark:text-slate-200 disabled:opacity-70"
                  />
                  <input
                    type="text"
                    placeholder="Name"
                    className="w-full bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl px-4 py-3 text-sm font-bold text-slate-900 dark:text-slate-100 placeholder-slate-500 dark:placeholder-slate-500 outline-none focus:ring-2 focus:ring-indigo-500/20"
                    value={newEmployee.name}
                    onChange={(e) => setNewEmployee({ ...newEmployee, name: e.target.value })}
                    required
                  />
                  <input
                    type="date"
                    className="w-full bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl px-4 py-3 text-sm font-bold text-slate-900 dark:text-slate-100 outline-none focus:ring-2 focus:ring-indigo-500/20"
                    value={newEmployee.dob}
                    onChange={(e) => setNewEmployee({ ...newEmployee, dob: e.target.value })}
                    required
                  />
                  {(() => {
                    const divisions = Array.from(new Set(roles.map(r => r.division))).sort();
                    const positionsForDivision = roles
                      .filter(r => r.division === newEmployee.division)
                      .map(r => r.position);
                    const positions = Array.from(new Set(positionsForDivision)).sort();
                    return (
                      <>
                        <select
                          className="w-full bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl px-4 py-3 text-sm font-bold text-slate-900 dark:text-slate-100 outline-none focus:ring-2 focus:ring-indigo-500/20"
                          value={newEmployee.division}
                          onChange={(e) => setNewEmployee({ ...newEmployee, division: e.target.value, position: '' })}
                          required
                        >
                          <option value="">— Pilih Division —</option>
                          {divisions.map(d => <option key={d} value={d}>{d}</option>)}
                        </select>
                        <select
                          className="w-full bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl px-4 py-3 text-sm font-bold text-slate-900 dark:text-slate-100 outline-none focus:ring-2 focus:ring-indigo-500/20 disabled:opacity-60"
                          value={newEmployee.position}
                          onChange={(e) => setNewEmployee({ ...newEmployee, position: e.target.value })}
                          required
                          disabled={!newEmployee.division}
                        >
                          <option value="">{newEmployee.division ? '— Pilih Position —' : '— Pilih Division dulu —'}</option>
                          {positions.map(p => <option key={p} value={p}>{p}</option>)}
                        </select>
                        {divisions.length === 0 && (
                          <div className="text-xs font-bold text-amber-600 dark:text-amber-400 inline-flex items-center">
                            <AlertCircle size={12} className="mr-1.5" />
                            Belum ada Role. Tambahkan role di tab "Role Employee" dulu.
                          </div>
                        )}
                      </>
                    );
                  })()}
                  <button className="w-full bg-indigo-600 hover:bg-indigo-700 text-white py-3 rounded-xl text-sm font-black transition-all shadow-lg shadow-indigo-500/20">
                    Add Employee
                  </button>
                </form>

                <div className="rounded-3xl border border-slate-100 dark:border-slate-800 bg-slate-50 dark:bg-slate-950 overflow-hidden">
                  <div className="p-5 flex items-center justify-between">
                    <div className="text-xs font-black text-slate-500 dark:text-slate-400 uppercase tracking-widest">
                      Face Scan
                    </div>
                    <div className="flex items-center gap-2">
                      {isRegisterScanning ? (
                        <>
                          <button
                            type="button"
                            onClick={captureRegisterFace}
                            className="px-3 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-700 text-white text-xs font-black transition-all"
                          >
                            Capture
                          </button>
                          <button
                            type="button"
                            onClick={stopRegisterScan}
                            className="px-3 py-2 rounded-xl bg-white dark:bg-slate-800 hover:bg-slate-100 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-100 text-xs font-black transition-all border border-slate-200 dark:border-slate-700"
                          >
                            Stop
                          </button>
                        </>
                      ) : (
                        <button
                          type="button"
                          onClick={startRegisterScan}
                          className="px-3 py-2 rounded-xl bg-white dark:bg-slate-800 hover:bg-slate-100 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-100 text-xs font-black transition-all border border-slate-200 dark:border-slate-700"
                        >
                          Start Scan
                        </button>
                      )}
                    </div>
                  </div>
                  <div className="aspect-video bg-slate-950 relative">
                    {isRegisterScanning ? (
                      <div className="absolute inset-0">
                        <video
                          ref={registerVideoRef}
                          autoPlay
                          playsInline
                          muted
                          className="w-full h-full object-cover transform -scale-x-100"
                        />
                        {registerWebcamError && (
                          <div className="absolute inset-0 flex items-center justify-center bg-black/40 text-white text-xs font-bold px-4 text-center">
                            {registerWebcamError}
                          </div>
                        )}
                      </div>
                    ) : capturedPreviewUrl ? (
                      <img
                        src={capturedPreviewUrl}
                        alt="Captured Face"
                        className="w-full h-full object-cover transform -scale-x-100"
                      />
                    ) : (
                      <div className="absolute inset-0 flex items-center justify-center text-slate-500 text-xs font-bold">
                        No scan captured
                      </div>
                    )}
                  </div>
                </div>
              </div>
            </motion.div>
          )}

          {activeTab === 'roles' && showRolesTab && (
            <div className="space-y-6">
              <motion.div
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                className="bg-white dark:bg-slate-900 rounded-3xl shadow-xl border border-slate-100 dark:border-slate-800 overflow-hidden"
              >
                <div className="p-4 sm:p-8 border-b border-slate-50 dark:border-slate-800 flex items-center justify-between gap-4 flex-wrap">
                  <div>
                    <h2 className="text-2xl font-black text-slate-800 dark:text-slate-100 inline-flex items-center">
                      <Briefcase size={20} className="mr-2 text-indigo-500" /> Role Employee
                    </h2>
                    <p className="text-sm text-slate-500 dark:text-slate-400 font-medium">
                      Kelola Division & Position perusahaan, lihat jumlah anggota per role.
                    </p>
                  </div>
                  <button
                    onClick={fetchRoles}
                    disabled={rolesLoading}
                    className="px-4 py-2 bg-slate-50 dark:bg-slate-800 hover:bg-slate-100 dark:hover:bg-slate-700 disabled:opacity-50 text-slate-700 dark:text-slate-100 rounded-xl text-sm font-black transition-all border border-slate-200 dark:border-slate-700"
                  >
                    Refresh
                  </button>
                </div>

                <div className="p-4 sm:p-8 border-b border-slate-50 dark:border-slate-800">
                  <form onSubmit={handleCreateRole} className="grid grid-cols-1 md:grid-cols-4 gap-3">
                    <input
                      type="text"
                      placeholder="Division (mis: Production)"
                      className="bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl px-4 py-3 text-sm font-bold text-slate-900 dark:text-slate-100 placeholder-slate-500 outline-none focus:ring-2 focus:ring-indigo-500/20"
                      value={newRole.division}
                      onChange={(e) => setNewRole({ ...newRole, division: e.target.value })}
                      required
                    />
                    <input
                      type="text"
                      placeholder="Position (mis: Operator)"
                      className="bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl px-4 py-3 text-sm font-bold text-slate-900 dark:text-slate-100 placeholder-slate-500 outline-none focus:ring-2 focus:ring-indigo-500/20"
                      value={newRole.position}
                      onChange={(e) => setNewRole({ ...newRole, position: e.target.value })}
                      required
                    />
                    <input
                      type="text"
                      placeholder="Deskripsi (opsional)"
                      className="bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl px-4 py-3 text-sm font-bold text-slate-900 dark:text-slate-100 placeholder-slate-500 outline-none focus:ring-2 focus:ring-indigo-500/20"
                      value={newRole.description}
                      onChange={(e) => setNewRole({ ...newRole, description: e.target.value })}
                    />
                    <button
                      type="submit"
                      disabled={createRoleSaving}
                      className="bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white rounded-xl text-sm font-black transition-all shadow-lg shadow-indigo-500/20"
                    >
                      {createRoleSaving ? 'Menyimpan…' : 'Tambah Role'}
                    </button>
                  </form>
                </div>

                <div className="p-4 sm:p-8">
                  {rolesLoading && roles.length === 0 ? (
                    <div className="text-sm font-bold text-slate-400 text-center py-12">Loading…</div>
                  ) : roles.length === 0 ? (
                    <div className="flex flex-col items-center text-center py-12">
                      <div className="w-16 h-16 rounded-2xl bg-slate-100 dark:bg-slate-800 flex items-center justify-center mb-3">
                        <Briefcase size={28} className="text-slate-400" />
                      </div>
                      <div className="text-sm font-black text-slate-700 dark:text-slate-200">Belum ada Role</div>
                      <div className="text-xs font-bold text-slate-400 mt-1">Tambahkan role pertama menggunakan form di atas.</div>
                    </div>
                  ) : (
                    <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4">
                      {roles.map(r => (
                        <div
                          key={r.id}
                          className="rounded-3xl border border-slate-100 dark:border-slate-800 bg-slate-50 dark:bg-slate-950 p-5 flex flex-col gap-3"
                        >
                          <div className="flex items-start justify-between gap-3">
                            <div className="min-w-0">
                              <div className="text-[10px] font-black uppercase tracking-widest text-slate-400">{r.division}</div>
                              <div className="text-base font-black text-slate-800 dark:text-slate-100 truncate">{r.position}</div>
                              {r.description && (
                                <div className="text-xs font-bold text-slate-500 dark:text-slate-400 mt-1 line-clamp-2">
                                  {r.description}
                                </div>
                              )}
                            </div>
                            <button
                              onClick={() => handleDeleteRole(r.id, `${r.division} • ${r.position}`)}
                              className="p-2 text-slate-300 hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20 rounded-xl transition-all"
                              title="Hapus role"
                            >
                              <Trash2 size={16} />
                            </button>
                          </div>

                          <div className="grid grid-cols-3 gap-2">
                            <div className="rounded-2xl bg-white dark:bg-slate-900 border border-slate-100 dark:border-slate-800 px-3 py-3 text-center">
                              <div className="text-[9px] font-black uppercase tracking-widest text-slate-400">Total</div>
                              <div className="text-lg font-black text-slate-800 dark:text-slate-100">{r.total}</div>
                            </div>
                            <div className="rounded-2xl bg-emerald-50 dark:bg-emerald-900/20 border border-emerald-100 dark:border-emerald-900/40 px-3 py-3 text-center">
                              <div className="text-[9px] font-black uppercase tracking-widest text-emerald-600 dark:text-emerald-300">Aktif</div>
                              <div className="text-lg font-black text-emerald-700 dark:text-emerald-200">{r.active_today}</div>
                            </div>
                            <div className="rounded-2xl bg-rose-50 dark:bg-rose-900/20 border border-rose-100 dark:border-rose-900/40 px-3 py-3 text-center">
                              <div className="text-[9px] font-black uppercase tracking-widest text-rose-600 dark:text-rose-300">Tidak Aktif</div>
                              <div className="text-lg font-black text-rose-700 dark:text-rose-200">{r.inactive_today}</div>
                            </div>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </motion.div>
            </div>
          )}

          {activeTab === 'profile' && (
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              className="bg-white dark:bg-slate-900 rounded-3xl shadow-xl border border-slate-100 dark:border-slate-800 overflow-hidden"
            >
              <div className="p-4 sm:p-8 border-b border-slate-50 dark:border-slate-800 flex items-center justify-between">
                <div>
                  <h2 className="text-2xl font-black text-slate-800 dark:text-slate-100">Profile</h2>
                  <p className="text-sm text-slate-500 dark:text-slate-400 font-medium">Update account details and password</p>
                </div>
              </div>

              <div className="p-4 sm:p-8 grid grid-cols-1 lg:grid-cols-2 gap-8">
                <div className="rounded-3xl border border-slate-100 dark:border-slate-800 bg-slate-50 dark:bg-slate-950 p-6">
                  <div className="text-xs font-black text-slate-500 dark:text-slate-400 uppercase tracking-widest mb-4">Profile Info</div>

                  {profileMessage && (
                    <div className="mb-4 rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-5 py-4 text-sm font-bold text-slate-700 dark:text-slate-100">
                      {profileMessage}
                    </div>
                  )}

                  <div className="flex items-center gap-4 mb-6">
                    <div className="w-16 h-16 rounded-2xl bg-slate-200 overflow-hidden flex items-center justify-center">
                      {avatarPreviewUrl ? (
                        <img src={avatarPreviewUrl} alt="Avatar Preview" className="w-full h-full object-cover" />
                      ) : me?.avatar_url ? (
                        <img src={resolveBackendUrl(me.avatar_url)} alt="Avatar" className="w-full h-full object-cover" />
                      ) : (
                        <div className="text-slate-500 font-black text-lg">
                          {username?.[0].toUpperCase()}
                        </div>
                      )}
                    </div>
                    <div className="flex-1">
                      <div className="text-sm font-black text-slate-800 dark:text-slate-100">{me?.username || username}</div>
                      <div className="text-xs font-bold text-slate-500 dark:text-slate-400 capitalize">{me?.role || role}</div>
                      <div className="mt-3 flex flex-wrap gap-3">
                        <input
                          type="file"
                          accept="image/png,image/jpeg"
                          onChange={(e) => handleAvatarPick(e.target.files?.[0] || null)}
                          className="block w-full text-xs font-bold text-slate-600 dark:text-slate-300"
                        />
                        <button
                          type="button"
                          onClick={handleAvatarUpload}
                          disabled={avatarUploading || !avatarFile}
                          className="px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white text-xs font-black transition-all shadow-lg shadow-indigo-500/20"
                        >
                          {avatarUploading ? 'Uploading…' : 'Upload Photo'}
                        </button>
                      </div>
                      {avatarMessage && (
                        <div className="mt-3 text-xs font-bold text-slate-600">
                          {avatarMessage}
                        </div>
                      )}
                    </div>
                  </div>

                  <form onSubmit={handleProfileSave} className="space-y-4">
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                      <div>
                        <div className="text-[10px] font-black uppercase tracking-widest text-slate-400 mb-2">Username</div>
                        <input
                          type="text"
                          className="w-full bg-white border border-slate-200 rounded-xl px-4 py-3 text-sm font-bold outline-none focus:ring-2 focus:ring-indigo-500/20"
                          value={profileForm.username}
                          onChange={(e) => setProfileForm({ ...profileForm, username: e.target.value })}
                          required
                        />
                      </div>
                      <div>
                        <div className="text-[10px] font-black uppercase tracking-widest text-slate-400 mb-2">ID</div>
                        <input
                          type="text"
                          className="w-full bg-white border border-slate-200 rounded-xl px-4 py-3 text-sm font-bold outline-none focus:ring-2 focus:ring-indigo-500/20"
                          value={profileForm.id}
                          onChange={(e) => setProfileForm({ ...profileForm, id: e.target.value })}
                          required
                        />
                      </div>
                    </div>

                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                      <div>
                        <div className="text-[10px] font-black uppercase tracking-widest text-slate-400 mb-2">Name</div>
                        <input
                          type="text"
                          className="w-full bg-white border border-slate-200 rounded-xl px-4 py-3 text-sm font-bold outline-none focus:ring-2 focus:ring-indigo-500/20"
                          value={profileForm.full_name}
                          onChange={(e) => setProfileForm({ ...profileForm, full_name: e.target.value })}
                        />
                      </div>
                      <div>
                        <div className="text-[10px] font-black uppercase tracking-widest text-slate-400 mb-2">Email</div>
                        <input
                          type="email"
                          className="w-full bg-white border border-slate-200 rounded-xl px-4 py-3 text-sm font-bold outline-none focus:ring-2 focus:ring-indigo-500/20"
                          value={profileForm.email}
                          onChange={(e) => setProfileForm({ ...profileForm, email: e.target.value })}
                        />
                      </div>
                    </div>

                    <button
                      disabled={profileSaving}
                      className="w-full bg-slate-900 hover:bg-slate-800 disabled:opacity-50 text-white py-3 rounded-xl text-sm font-black transition-all"
                    >
                      {profileSaving ? 'Saving…' : 'Save Profile'}
                    </button>
                  </form>
                </div>

                <div className="rounded-3xl border border-slate-100 bg-slate-50 p-6">
                  <div className="text-xs font-black text-slate-500 uppercase tracking-widest mb-4">Change Password</div>

                  {passwordMessage && (
                    <div className="mb-4 rounded-2xl border border-slate-200 bg-white px-5 py-4 text-sm font-bold text-slate-700">
                      {passwordMessage}
                    </div>
                  )}

                  <form onSubmit={handleChangePassword} className="space-y-4">
                    <div>
                      <div className="text-[10px] font-black uppercase tracking-widest text-slate-400 mb-2">Old Password</div>
                      <input
                        type="password"
                        className="w-full bg-white border border-slate-200 rounded-xl px-4 py-3 text-sm font-bold outline-none focus:ring-2 focus:ring-indigo-500/20"
                        value={passwordOld}
                        onChange={(e) => setPasswordOld(e.target.value)}
                        required
                      />
                    </div>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                      <div>
                        <div className="text-[10px] font-black uppercase tracking-widest text-slate-400 mb-2">New Password</div>
                        <input
                          type="password"
                          className="w-full bg-white border border-slate-200 rounded-xl px-4 py-3 text-sm font-bold outline-none focus:ring-2 focus:ring-indigo-500/20"
                          value={passwordNew}
                          onChange={(e) => setPasswordNew(e.target.value)}
                          required
                        />
                      </div>
                      <div>
                        <div className="text-[10px] font-black uppercase tracking-widest text-slate-400 mb-2">Confirm New Password</div>
                        <input
                          type="password"
                          className="w-full bg-white border border-slate-200 rounded-xl px-4 py-3 text-sm font-bold outline-none focus:ring-2 focus:ring-indigo-500/20"
                          value={passwordNew2}
                          onChange={(e) => setPasswordNew2(e.target.value)}
                          required
                        />
                      </div>
                    </div>
                    <button
                      disabled={passwordSaving}
                      className="w-full bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white py-3 rounded-xl text-sm font-black transition-all shadow-lg shadow-indigo-500/20"
                    >
                      {passwordSaving ? 'Saving…' : 'Change Password'}
                    </button>
                  </form>
                </div>
              </div>
            </motion.div>
          )}

          {activeTab === 'employees' && role === 'manager' && (
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              className="bg-white dark:bg-slate-900 rounded-3xl shadow-xl border border-slate-100 dark:border-slate-800 overflow-hidden"
            >
              <div className="p-4 sm:p-8 border-b border-slate-50 dark:border-slate-800 flex items-center justify-between">
                <div>
                  <h2 className="text-2xl font-black text-slate-800 dark:text-slate-100">Users</h2>
                  <p className="text-sm text-slate-500 dark:text-slate-400 font-medium">Manager/Admin accounts</p>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    onClick={openCreateUser}
                    className="px-4 py-2 bg-indigo-600 hover:bg-indigo-700 text-white rounded-xl text-sm font-black transition-all shadow-lg shadow-indigo-500/20 inline-flex items-center"
                  >
                    <UserPlus size={16} className="mr-2" /> Add User/Role
                  </button>
                  <button
                    onClick={fetchUsers}
                    className="px-4 py-2 bg-slate-50 dark:bg-slate-800 hover:bg-slate-100 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-100 rounded-xl text-sm font-black transition-all border border-slate-200 dark:border-slate-700"
                  >
                    Refresh
                  </button>
                </div>
              </div>
              <div className="p-6">
                {usersLoading ? (
                  <div className="text-sm font-bold text-slate-400">Loading...</div>
                ) : users.length === 0 ? (
                  <div className="text-sm font-bold text-slate-400">No users.</div>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full text-left">
                      <thead>
                        <tr className="bg-slate-50/50 dark:bg-slate-800/50">
                          <th className="px-4 sm:px-6 py-3 sm:py-4 text-[10px] sm:text-xs font-black text-slate-400 uppercase tracking-widest">ID</th>
                          <th className="px-4 sm:px-6 py-3 sm:py-4 text-[10px] sm:text-xs font-black text-slate-400 uppercase tracking-widest">Username</th>
                          <th className="px-4 sm:px-6 py-3 sm:py-4 text-[10px] sm:text-xs font-black text-slate-400 uppercase tracking-widest">Role</th>
                          <th className="px-4 sm:px-6 py-3 sm:py-4 text-[10px] sm:text-xs font-black text-slate-400 uppercase tracking-widest">Email</th>
                          <th className="px-4 sm:px-6 py-3 sm:py-4 text-[10px] sm:text-xs font-black text-slate-400 uppercase tracking-widest text-right">Actions</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-slate-50 dark:divide-slate-800">
                        {users.map(u => (
                          <tr key={u.id} className="hover:bg-slate-50/80 dark:hover:bg-slate-800/40 transition-all">
                            <td className="px-4 sm:px-6 py-4 sm:py-5 text-xs sm:text-sm font-black text-indigo-600">{u.id}</td>
                            <td className="px-4 sm:px-6 py-4 sm:py-5 text-xs sm:text-sm font-bold text-slate-800 dark:text-slate-100">{u.username}</td>
                            <td className="px-4 sm:px-6 py-4 sm:py-5 text-xs sm:text-sm font-bold text-slate-600 dark:text-slate-300 capitalize">{u.role}</td>
                            <td className="px-4 sm:px-6 py-4 sm:py-5 text-xs sm:text-sm font-bold text-slate-600 dark:text-slate-300">{u.email || '--'}</td>
                            <td className="px-4 sm:px-6 py-4 sm:py-5 text-right">
                              {u.role === 'admin' ? (
                                <button
                                  onClick={() => handleDeleteUser(u.id)}
                                  disabled={u.username === username}
                                  className="inline-flex items-center px-3 py-2 rounded-xl bg-red-50 dark:bg-red-900/20 hover:bg-red-100 dark:hover:bg-red-900/30 text-red-600 text-xs font-black transition-all border border-red-200 dark:border-red-900/40 disabled:opacity-60 disabled:hover:bg-red-50"
                                >
                                  <Trash2 size={14} className="mr-2" /> Delete
                                </button>
                              ) : (
                                <span className="text-xs font-bold text-slate-400">—</span>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </motion.div>
          )}

          {activeTab === 'system' && showSystemTab && (
            <>
              {/* Gate Control - Available for both manager and admin */}
              <motion.div
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                className="bg-white dark:bg-slate-900 rounded-3xl shadow-xl border border-slate-100 dark:border-slate-800 overflow-hidden"
              >
                <div className="p-4 sm:p-8 border-b border-slate-50 dark:border-slate-800 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                  <div>
                    <h2 className="text-2xl font-black text-slate-800 dark:text-slate-100">Gate Control</h2>
                    <p className="text-sm text-slate-500 dark:text-slate-400 font-medium">Manual gate open/close. Status diambil dari MQTT (perangkat IoT).</p>
                  </div>
                  <div className="flex items-center gap-2">
                    <span
                      className={`inline-flex items-center px-3 py-1.5 rounded-full text-[10px] font-black uppercase tracking-widest border ${
                        iotStatus?.connected
                          ? 'bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-900/20 dark:text-emerald-300 dark:border-emerald-900/40'
                          : 'bg-rose-50 text-rose-700 border-rose-200 dark:bg-rose-900/20 dark:text-rose-300 dark:border-rose-900/40'
                      }`}
                    >
                      {iotStatus?.connected ? <Wifi size={12} className="mr-1.5" /> : <WifiOff size={12} className="mr-1.5" />}
                      {iotStatus?.connected ? 'Gate ONLINE' : 'Gate OFFLINE'}
                    </span>
                    <span
                      className={`inline-flex items-center px-3 py-1.5 rounded-full text-[10px] font-black uppercase tracking-widest border ${
                        gateStatus?.status === 'open'
                          ? 'bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-900/20 dark:text-amber-300 dark:border-amber-900/40'
                          : 'bg-slate-50 text-slate-700 border-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:border-slate-700'
                      }`}
                    >
                      {gateStatus?.status === 'open' ? <DoorOpen size={12} className="mr-1.5" /> : <DoorClosed size={12} className="mr-1.5" />}
                      Gate {gateStatus?.status?.toUpperCase() || 'CLOSED'}
                    </span>
                  </div>
                </div>

                <div className="p-4 sm:p-8 space-y-4">
                  {gateStatus?.last_action && gateStatus.last_action !== 'none' && (
                    <div className="text-xs font-bold text-slate-500 dark:text-slate-400">
                      Last action: <span className="text-slate-800 dark:text-slate-100">{gateStatus.last_action}</span>
                      {gateStatus.timestamp ? ` @ ${new Date(gateStatus.timestamp).toLocaleString('id-ID')}` : ''}
                    </div>
                  )}
                  {iotStatus && !iotStatus.connected && (
                    <div className="rounded-2xl px-5 py-4 text-sm font-bold bg-rose-50 dark:bg-rose-900/20 border border-rose-100 dark:border-rose-900/30 text-rose-700 dark:text-rose-200">
                      Perangkat IoT (ESP32) belum terdeteksi di MQTT. Tombol nonaktif sampai device online.
                    </div>
                  )}
                  {gateMessage && (
                    <div className={`rounded-2xl px-5 py-4 text-sm font-bold ${
                      gateMessage.includes('Error')
                        ? 'bg-rose-50 dark:bg-rose-900/20 border border-rose-100 dark:border-rose-900/30 text-rose-700 dark:text-rose-200'
                        : 'bg-emerald-50 dark:bg-emerald-900/20 border border-emerald-100 dark:border-emerald-900/30 text-emerald-700 dark:text-emerald-200'
                    }`}>
                      {gateMessage}
                    </div>
                  )}

                  <div className="flex flex-col sm:flex-row gap-4">
                    <button
                      onClick={() => controlGate('open')}
                      disabled={gateLoading || !iotStatus?.connected}
                      className="flex-1 inline-flex items-center justify-center px-6 py-3 bg-emerald-500 hover:bg-emerald-600 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-xl text-sm font-black transition-all"
                    >
                      <DoorOpen size={16} className="mr-2" />
                      {gateLoading ? 'Processing...' : 'Open Gate'}
                    </button>
                    <button
                      onClick={() => controlGate('close')}
                      disabled={gateLoading || !iotStatus?.connected}
                      className="flex-1 inline-flex items-center justify-center px-6 py-3 bg-red-500 hover:bg-red-600 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-xl text-sm font-black transition-all"
                    >
                      <DoorClosed size={16} className="mr-2" />
                      {gateLoading ? 'Processing...' : 'Close Gate'}
                    </button>
                  </div>
                </div>
              </motion.div>

              {/* Login Activity - Manager only, sourced from VPS MongoDB audit_logs */}
              {role === 'manager' && (
                <motion.div
                  initial={{ opacity: 0, y: 20 }}
                  animate={{ opacity: 1, y: 0 }}
                  className="bg-white dark:bg-slate-900 rounded-3xl shadow-xl border border-slate-100 dark:border-slate-800 overflow-hidden"
                >
                  <div className="p-4 sm:p-8 border-b border-slate-50 dark:border-slate-800 flex items-center justify-between">
                    <div>
                      <h2 className="text-2xl font-black text-slate-800 dark:text-slate-100">Login Activity</h2>
                      <p className="text-sm text-slate-500 dark:text-slate-400 font-medium">Tersimpan di VPS (Username, Status, Timestamp)</p>
                    </div>
                    <button
                      onClick={fetchLoginAudit}
                      disabled={loginAuditLoading}
                      className="px-4 py-2 bg-slate-50 dark:bg-slate-800 hover:bg-slate-100 dark:hover:bg-slate-700 disabled:opacity-50 text-slate-700 dark:text-slate-100 rounded-xl text-sm font-black transition-all border border-slate-200 dark:border-slate-700"
                    >
                      Refresh
                    </button>
                  </div>
                  <div className="p-6">
                    {loginAuditLoading && loginAudit.length === 0 ? (
                      <div className="text-sm font-bold text-slate-400">Loading…</div>
                    ) : loginAudit.length === 0 ? (
                      <div className="text-sm font-bold text-slate-400">Belum ada aktivitas login.</div>
                    ) : (
                      <>
                        <div className="overflow-x-auto">
                          <table className="w-full text-left">
                            <thead>
                              <tr className="bg-slate-50/50 dark:bg-slate-800/50">
                                <th className="px-4 py-3 text-[10px] font-black text-slate-400 uppercase tracking-widest">Username</th>
                                <th className="px-4 py-3 text-[10px] font-black text-slate-400 uppercase tracking-widest">Status</th>
                                <th className="px-4 py-3 text-[10px] font-black text-slate-400 uppercase tracking-widest">Timestamp</th>
                              </tr>
                            </thead>
                            <tbody className="divide-y divide-slate-50 dark:divide-slate-800">
                              {pagedLoginAudit.map((row, idx) => (
                                <tr key={`${row.username}-${row.timestamp}-${idx}`} className="hover:bg-slate-50/80 dark:hover:bg-slate-800/40 transition-all">
                                  <td className="px-4 py-3 text-sm font-bold text-slate-800 dark:text-slate-100">
                                    {row.username || '--'}
                                    {row.role ? <span className="ml-2 text-[10px] font-black text-slate-400 uppercase">{row.role}</span> : null}
                                  </td>
                                  <td className="px-4 py-3">
                                    <span className={`px-3 py-1 rounded-xl text-[10px] font-black uppercase tracking-widest border ${
                                      row.status === 'success'
                                        ? 'bg-emerald-50 text-emerald-600 border-emerald-100 dark:bg-emerald-900/20 dark:text-emerald-300 dark:border-emerald-900/40'
                                        : 'bg-rose-50 text-rose-600 border-rose-100 dark:bg-rose-900/20 dark:text-rose-300 dark:border-rose-900/40'
                                    }`}>
                                      {row.status}
                                    </span>
                                  </td>
                                  <td className="px-4 py-3 text-xs font-bold text-slate-500">
                                    {(() => {
                                      try {
                                        const d = new Date(row.timestamp);
                                        return !isNaN(d.getTime()) ? format(d, 'yyyy-MM-dd HH:mm:ss') : '';
                                      } catch { return ''; }
                                    })()}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                        {loginAuditTotalPages > 1 && (
                          <div className="mt-5 flex flex-col sm:flex-row items-center justify-between gap-3">
                            <div className="text-xs font-bold text-slate-500 dark:text-slate-400">
                              Halaman {loginAuditPage} dari {loginAuditTotalPages} ({loginAudit.length} entri)
                            </div>
                            <div className="flex items-center gap-2 flex-wrap justify-center">
                              <button
                                onClick={() => setLoginAuditPage(p => Math.max(1, p - 1))}
                                disabled={loginAuditPage <= 1}
                                className="px-3 py-2 rounded-xl bg-slate-50 dark:bg-slate-800 hover:bg-slate-100 dark:hover:bg-slate-700 disabled:opacity-50 text-slate-700 dark:text-slate-100 text-xs font-black transition-all border border-slate-200 dark:border-slate-700"
                              >
                                Prev
                              </button>
                              {buildPageButtons(loginAuditTotalPages, loginAuditPage).map((p, idx) =>
                                p === '...' ? (
                                  <div key={`la-dots-${idx}`} className="px-2 text-slate-400 dark:text-slate-500 text-xs font-black">…</div>
                                ) : (
                                  <button
                                    key={`la-${p}`}
                                    onClick={() => setLoginAuditPage(p)}
                                    className={
                                      p === loginAuditPage
                                        ? 'px-3 py-2 rounded-xl bg-indigo-600 text-white text-xs font-black transition-all shadow-lg shadow-indigo-500/20'
                                        : 'px-3 py-2 rounded-xl bg-slate-50 dark:bg-slate-800 hover:bg-slate-100 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-100 text-xs font-black transition-all border border-slate-200 dark:border-slate-700'
                                    }
                                  >
                                    {p}
                                  </button>
                                )
                              )}
                              <button
                                onClick={() => setLoginAuditPage(p => Math.min(loginAuditTotalPages, p + 1))}
                                disabled={loginAuditPage >= loginAuditTotalPages}
                                className="px-3 py-2 rounded-xl bg-slate-50 dark:bg-slate-800 hover:bg-slate-100 dark:hover:bg-slate-700 disabled:opacity-50 text-slate-700 dark:text-slate-100 text-xs font-black transition-all border border-slate-200 dark:border-slate-700"
                              >
                                Next
                              </button>
                            </div>
                          </div>
                        )}
                      </>
                    )}
                  </div>
                </motion.div>
              )}

              {role === 'manager' && (
                <motion.div
                  initial={{ opacity: 0, y: 20 }}
                  animate={{ opacity: 1, y: 0 }}
                  className="bg-white dark:bg-slate-900 rounded-3xl shadow-xl border border-slate-100 dark:border-slate-800 overflow-hidden"
                >
                  <div className="p-4 sm:p-8 border-b border-slate-50 dark:border-slate-800 flex items-center justify-between">
                    <div>
                      <h2 className="text-2xl font-black text-slate-800 dark:text-slate-100">System</h2>
                      <p className="text-sm text-slate-500 dark:text-slate-400 font-medium">VPS resource usage (RAM + storage)</p>
                    </div>
                    <button
                      onClick={fetchSystemMetrics}
                      disabled={systemLoading}
                      className="px-4 py-2 bg-slate-50 dark:bg-slate-800 hover:bg-slate-100 dark:hover:bg-slate-700 disabled:opacity-50 text-slate-700 dark:text-slate-100 rounded-xl text-sm font-black transition-all border border-slate-200 dark:border-slate-700"
                    >
                      Refresh
                    </button>
                  </div>

                  <div className="p-4 sm:p-8 space-y-6">
                    {systemError && (
                      <div className="bg-rose-50 dark:bg-rose-900/20 border border-rose-100 dark:border-rose-900/30 text-rose-700 dark:text-rose-200 rounded-2xl px-5 py-4 text-sm font-bold">
                        {systemError}
                      </div>
                    )}

                    {!systemMetrics && !systemError && (
                      <div className="text-sm font-bold text-slate-400">
                        {systemLoading ? 'Loading metrics…' : 'No metrics available.'}
                      </div>
                    )}

                    {systemMetrics && (
                      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                        <div className="rounded-3xl border border-slate-100 dark:border-slate-800 bg-slate-50 dark:bg-slate-950 p-6">
                          <div className="flex items-start justify-between">
                            <div>
                              <div className="text-[10px] font-black uppercase tracking-widest text-slate-400">Memory</div>
                              <div className="text-xl font-black text-slate-800 dark:text-slate-100 mt-1">
                                {systemMetrics.memory_total_bytes === null ? '--' : formatBytes((systemMetrics.memory_total_bytes ?? 0) - (systemMetrics.memory_available_bytes ?? 0))}
                                <span className="text-sm font-bold text-slate-500 dark:text-slate-400"> / {formatBytes(systemMetrics.memory_total_bytes)}</span>
                              </div>
                            </div>
                            <div className="text-xs font-bold text-slate-400">
                              {systemMetrics.updated_at ? `Updated ${format(new Date(systemMetrics.updated_at), 'HH:mm:ss')}` : ''}
                            </div>
                          </div>
                          <div className="mt-5 h-3 bg-white dark:bg-slate-900 rounded-full overflow-hidden border border-slate-100 dark:border-slate-800">
                            {systemMetrics.memory_total_bytes && systemMetrics.memory_available_bytes !== null ? (
                              <div
                                className="h-full bg-indigo-600"
                                style={{
                                  width: `${Math.min(
                                    100,
                                    Math.max(
                                      0,
                                      ((systemMetrics.memory_total_bytes - systemMetrics.memory_available_bytes) / systemMetrics.memory_total_bytes) * 100
                                    )
                                  )}%`,
                                }}
                              />
                            ) : (
                              <div className="h-full bg-slate-200 dark:bg-slate-700 w-1/3" />
                            )}
                          </div>
                          <div className="mt-3 text-xs font-bold text-slate-500 dark:text-slate-400">
                            Available: {formatBytes(systemMetrics.memory_available_bytes)}
                          </div>
                        </div>

                        <div className="rounded-3xl border border-slate-100 dark:border-slate-800 bg-slate-50 dark:bg-slate-950 p-6">
                          <div className="flex items-start justify-between">
                            <div>
                              <div className="text-[10px] font-black uppercase tracking-widest text-slate-400">Storage</div>
                              <div className="text-xl font-black text-slate-800 dark:text-slate-100 mt-1">
                                {formatBytes(systemMetrics.disk_used_bytes)}
                                <span className="text-sm font-bold text-slate-500 dark:text-slate-400"> / {formatBytes(systemMetrics.disk_total_bytes)}</span>
                              </div>
                            </div>
                            <div className="text-xs font-bold text-slate-400">
                              {systemMetrics.disk_path}
                            </div>
                          </div>
                          <div className="mt-5 h-3 bg-white dark:bg-slate-900 rounded-full overflow-hidden border border-slate-100 dark:border-slate-800">
                            <div
                              className="h-full bg-emerald-600"
                              style={{
                                width: `${Math.min(100, Math.max(0, (systemMetrics.disk_used_bytes / systemMetrics.disk_total_bytes) * 100))}%`,
                              }}
                            />
                          </div>
                          <div className="mt-3 text-xs font-bold text-slate-500 dark:text-slate-400">
                            Free: {formatBytes(systemMetrics.disk_free_bytes)}
                          </div>
                        </div>
                      </div>
                    )}
                  </div>
                </motion.div>
              )}
            </>
          )}

          {/* Attendance Logs Table */}
          {activeTab === 'logs' && showLogsTab && (
          <motion.div 
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            className="bg-white dark:bg-slate-900 rounded-3xl shadow-xl border border-slate-100 dark:border-slate-800 overflow-hidden"
          >
            <div className="p-4 sm:p-8 border-b border-slate-50 dark:border-slate-800 flex flex-col sm:flex-row sm:items-center justify-between gap-4">
              <div>
                <h2 className="text-2xl font-black text-slate-800 dark:text-slate-100">Attendance Logs</h2>
                <p className="text-sm text-slate-500 dark:text-slate-400 font-medium">Monitoring all entries and exits</p>
              </div>
              <div className="flex flex-col sm:flex-row sm:items-center gap-2 w-full sm:w-auto">
                <input
                  type="date"
                  className="w-full sm:w-auto px-3 py-2 bg-slate-50 dark:bg-slate-800 rounded-xl text-sm font-bold text-slate-700 dark:text-slate-100 border border-slate-200 dark:border-slate-700 outline-none"
                  value={selectedDate}
                  onChange={(e) => setSelectedDate(e.target.value)}
                />
                <select
                  className="w-full sm:w-auto px-3 py-2 bg-slate-50 dark:bg-slate-800 rounded-xl text-sm font-bold text-slate-700 dark:text-slate-100 border border-slate-200 dark:border-slate-700 outline-none"
                  value={selectedDirection}
                  onChange={(e) => setSelectedDirection(e.target.value)}
                >
                  <option value="">ALL</option>
                  <option value="in">ATTEND</option>
                  <option value="out">PRESENT</option>
                </select>
                <select
                  className="w-full sm:w-auto px-3 py-2 bg-slate-50 dark:bg-slate-800 rounded-xl text-sm font-bold text-slate-700 dark:text-slate-100 border border-slate-200 dark:border-slate-700 outline-none"
                  value={selectedStatus}
                  onChange={(e) => setSelectedStatus(e.target.value)}
                >
                  <option value="">ALL</option>
                  <option value="present">PRESENT</option>
                  <option value="permission">PERMISSION</option>
                  <option value="sick">SICK</option>
                  <option value="leave">LEAVE</option>
                  <option value="half_day">HALF DAY</option>
                  <option value="other">OTHERS</option>
                </select>
                <select
                  className="w-full sm:w-auto px-3 py-2 bg-slate-50 dark:bg-slate-800 rounded-xl text-sm font-bold text-slate-700 dark:text-slate-100 border border-slate-200 dark:border-slate-700 outline-none"
                  value={selectedEmployeeId}
                  onChange={(e) => setSelectedEmployeeId(e.target.value)}
                >
                  <option value="">ALL EMPLOYEES</option>
                  {employees.map(emp => (
                    <option key={emp.id} value={emp.id}>{emp.id} - {emp.name}</option>
                  ))}
                </select>
                <button
                  onClick={exportCsv}
                  className="w-full sm:w-auto justify-center px-4 py-2 bg-indigo-600 hover:bg-indigo-700 text-white rounded-xl text-sm font-black transition-all shadow-lg shadow-indigo-500/20 flex items-center"
                >
                  <Download size={16} className="mr-2" /> Export CSV
                </button>
              </div>
            </div>
            
            <div className="relative">
              <div
                ref={logsScrollRef}
                onScroll={(e) => {
                  const el = e.currentTarget;
                  setShowScrollTop(el.scrollTop > 120);
                }}
                className="max-h-[65vh] overflow-auto"
              >
              <table className="w-full text-left">
                <thead>
                  <tr className="bg-slate-50/50 dark:bg-slate-800/50">
                    <th className="px-4 sm:px-8 py-3 sm:py-4 text-[10px] sm:text-xs font-black text-slate-400 uppercase tracking-widest">Employee</th>
                    <th className="px-4 sm:px-8 py-3 sm:py-4 text-[10px] sm:text-xs font-black text-slate-400 uppercase tracking-widest">Division</th>
                    <th className="px-4 sm:px-8 py-3 sm:py-4 text-[10px] sm:text-xs font-black text-slate-400 uppercase tracking-widest">Date</th>
                    <th className="px-4 sm:px-8 py-3 sm:py-4 text-[10px] sm:text-xs font-black text-slate-400 uppercase tracking-widest">Time</th>
                    <th className="px-4 sm:px-8 py-3 sm:py-4 text-[10px] sm:text-xs font-black text-slate-400 uppercase tracking-widest">Dir</th>
                    <th className="px-4 sm:px-8 py-3 sm:py-4 text-[10px] sm:text-xs font-black text-slate-400 uppercase tracking-widest">Status</th>
                    {role === 'manager' && <th className="px-4 sm:px-8 py-3 sm:py-4 text-[10px] sm:text-xs font-black text-slate-400 uppercase tracking-widest">Actions</th>}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-50 dark:divide-slate-800">
                  <AnimatePresence mode='popLayout'>
                    {pagedLogs.map((log) => (
                      <motion.tr 
                        layout
                        initial={{ opacity: 0 }}
                        animate={{ opacity: 1 }}
                        exit={{ opacity: 0 }}
                        key={`${log.employee_id}-${log.timestamp}`} 
                        className="hover:bg-slate-50/80 dark:hover:bg-slate-800/40 transition-all group cursor-default"
                      >
                        <td className="px-4 sm:px-8 py-4 sm:py-5">
                          <div className="flex items-center">
                            <div className="w-9 h-9 sm:w-10 sm:h-10 rounded-2xl bg-slate-100 dark:bg-slate-800 flex items-center justify-center text-slate-500 dark:text-slate-200 mr-3 sm:mr-4 font-bold group-hover:bg-indigo-50 dark:group-hover:bg-indigo-900/20 group-hover:text-indigo-600 dark:group-hover:text-indigo-300 transition-colors">
                              {log.employee?.name[0]}
                            </div>
                            <div>
                              <p className="text-xs sm:text-sm font-bold text-slate-800 dark:text-slate-100">{log.employee?.name}</p>
                              <p className="text-xs text-slate-400 font-medium">{log.employee_id}</p>
                            </div>
                          </div>
                        </td>
                        <td className="px-4 sm:px-8 py-4 sm:py-5">
                          <span className="text-xs sm:text-sm font-bold text-slate-600 dark:text-slate-300">{log.employee?.division}</span>
                        </td>
                        <td className="px-4 sm:px-8 py-4 sm:py-5 text-xs font-bold text-slate-500">
                          {(() => {
                            try {
                              const date = new Date(log.timestamp);
                              return !isNaN(date.getTime()) ? format(date, 'yyyy-MM-dd') : '';
                            } catch (e) {
                              return '';
                            }
                          })()}
                        </td>
                        <td className="px-4 sm:px-8 py-4 sm:py-5">
                          <div className="flex items-center text-slate-500 dark:text-slate-400">
                            <Clock size={14} className="mr-2 opacity-50" />
                            <span className="text-xs font-bold">
                              {(() => {
                                try {
                                  const date = new Date(log.timestamp);
                                  return !isNaN(date.getTime()) ? format(date, 'HH:mm:ss') : '--:--:--';
                                } catch (e) {
                                  return '--:--:--';
                                }
                              })()}
                            </span>
                          </div>
                        </td>
                        <td className="px-4 sm:px-8 py-4 sm:py-5 text-xs font-black text-slate-600 dark:text-slate-300 uppercase">
                          {(() => {
                            const d = (log.direction || '').toLowerCase();
                            if (d === 'in') return 'ATTEND';
                            if (d === 'out') return 'PRESENT';
                            return log.direction || '';
                          })()}
                        </td>
                        <td className="px-4 sm:px-8 py-4 sm:py-5">
                          <StatusBadge status={log.status} reason={log.reason} />
                        </td>
                        {role === 'manager' && (
                          <td className="px-4 sm:px-8 py-4 sm:py-5">
                            <button 
                              onClick={() => handleDeleteEmployee(log.employee_id)}
                              className="p-2 text-slate-300 hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20 rounded-xl transition-all"
                            >
                              <Trash2 size={18} />
                            </button>
                          </td>
                        )}
                      </motion.tr>
                    ))}
                  </AnimatePresence>
                </tbody>
              </table>
              </div>
              {showScrollTop && (
                <button
                  type="button"
                  onClick={() => logsScrollRef.current?.scrollTo({ top: 0, behavior: 'smooth' })}
                  className="absolute bottom-6 right-6 w-11 h-11 rounded-2xl bg-indigo-600 hover:bg-indigo-700 text-white shadow-xl shadow-indigo-500/20 flex items-center justify-center transition-all"
                  aria-label="Scroll to top"
                >
                  <ChevronUp size={18} />
                </button>
              )}
            </div>
            <div className="p-6 border-t border-slate-100 dark:border-slate-800 flex flex-col sm:flex-row items-center justify-between gap-4">
              <div className="text-xs font-bold text-slate-500 dark:text-slate-400">
                Showing {(filteredLogs.length === 0) ? 0 : ((logsPage - 1) * logsPerPage + 1)}–
                {Math.min(logsPage * logsPerPage, filteredLogs.length)} of {filteredLogs.length}
              </div>
              <div className="flex items-center gap-2 flex-wrap justify-center">
                <button
                  onClick={() => setLogsPage(p => Math.max(1, p - 1))}
                  disabled={logsPage <= 1}
                  className="px-3 py-2 rounded-xl bg-slate-50 dark:bg-slate-800 hover:bg-slate-100 dark:hover:bg-slate-700 disabled:opacity-50 text-slate-700 dark:text-slate-100 text-xs font-black transition-all border border-slate-200 dark:border-slate-700"
                >
                  Prev
                </button>
                {logsPageButtons.map((p, idx) =>
                  p === '...' ? (
                    <div key={`dots-${idx}`} className="px-2 text-slate-400 dark:text-slate-500 text-xs font-black">
                      ...
                    </div>
                  ) : (
                    <button
                      key={p}
                      onClick={() => setLogsPage(p)}
                      className={
                        p === logsPage
                          ? 'px-3 py-2 rounded-xl bg-indigo-600 text-white text-xs font-black transition-all shadow-lg shadow-indigo-500/20'
                          : 'px-3 py-2 rounded-xl bg-slate-50 dark:bg-slate-800 hover:bg-slate-100 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-100 text-xs font-black transition-all border border-slate-200 dark:border-slate-700'
                      }
                    >
                      {p}
                    </button>
                  )
                )}
                <div className="flex items-center gap-2 px-2">
                  <div className="text-[10px] font-black uppercase tracking-widest text-slate-400 dark:text-slate-500">
                    Page
                  </div>
                  <input
                    type="number"
                    min={1}
                    max={logsTotalPages}
                    value={logsPageInput}
                    onChange={(e) => setLogsPageInput(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') {
                        e.preventDefault();
                        commitLogsPageInput();
                      }
                    }}
                    className="w-20 px-3 py-2 rounded-xl bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 text-slate-700 dark:text-slate-100 text-xs font-black outline-none focus:ring-2 focus:ring-indigo-500/20"
                  />
                  <button
                    onClick={commitLogsPageInput}
                    className="px-3 py-2 rounded-xl bg-slate-900 hover:bg-slate-800 text-white text-xs font-black transition-all"
                  >
                    Go
                  </button>
                  <div className="text-xs font-bold text-slate-400">
                    / {logsTotalPages}
                  </div>
                </div>
                <button
                  onClick={() => setLogsPage(p => Math.min(logsTotalPages, p + 1))}
                  disabled={logsPage >= logsTotalPages}
                  className="px-3 py-2 rounded-xl bg-slate-50 dark:bg-slate-800 hover:bg-slate-100 dark:hover:bg-slate-700 disabled:opacity-50 text-slate-700 dark:text-slate-100 text-xs font-black transition-all border border-slate-200 dark:border-slate-700"
                >
                  Next
                </button>
              </div>
            </div>
          </motion.div>
          )}
        </div>
      </main>

      <AnimatePresence>
        {createUserOpen && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-[100] bg-black/50 backdrop-blur-sm flex items-center justify-center p-4"
            onMouseDown={() => {
              if (createUserSaving) return;
              setCreateUserOpen(false);
            }}
          >
            <motion.div
              initial={{ opacity: 0, y: 12, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 12, scale: 0.98 }}
              transition={{ duration: 0.15 }}
              className="w-full max-w-2xl bg-white dark:bg-slate-900 rounded-3xl shadow-2xl border border-slate-100 dark:border-slate-800 overflow-hidden"
              onMouseDown={(e) => e.stopPropagation()}
            >
              <div className="p-6 border-b border-slate-100 dark:border-slate-800 flex items-start justify-between gap-4">
                <div>
                  <div className="text-lg font-black text-slate-900 dark:text-slate-50">Tambah User/Role</div>
                  <div className="text-sm font-bold text-slate-500 dark:text-slate-400">ID dibuat otomatis (random)</div>
                </div>
                <button
                  type="button"
                  disabled={createUserSaving}
                  onClick={() => setCreateUserOpen(false)}
                  className="px-3 py-2 rounded-xl bg-slate-50 dark:bg-slate-800 hover:bg-slate-100 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-100 text-xs font-black transition-all border border-slate-200 dark:border-slate-700 disabled:opacity-60"
                >
                  Close
                </button>
              </div>

              <form onSubmit={handleCreateUser} className="p-6 space-y-5">
                {createUserMessage && (
                  <div className="text-sm font-bold text-rose-600 bg-rose-50 border border-rose-200 rounded-2xl px-4 py-3">
                    {createUserMessage}
                  </div>
                )}

                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <div>
                    <div className="text-[10px] font-black uppercase tracking-widest text-slate-400 mb-2">ID</div>
                    <input
                      type="text"
                      value="(auto)"
                      disabled
                      className="w-full bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl px-4 py-3 text-sm font-black text-slate-500 dark:text-slate-300 outline-none"
                    />
                  </div>
                  <div>
                    <div className="text-[10px] font-black uppercase tracking-widest text-slate-400 mb-2">Role</div>
                    <select
                      value={createUserForm.role}
                      onChange={(e) => setCreateUserForm((p) => ({ ...p, role: e.target.value as any }))}
                      className="w-full bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl px-4 py-3 text-sm font-black outline-none focus:ring-2 focus:ring-indigo-500/20"
                    >
                      <option value="custom">Role Baru</option>
                      <option value="admin">Admin</option>
                    </select>
                  </div>
                </div>

                {createUserForm.role === 'custom' && (
                  <div>
                    <div className="text-[10px] font-black uppercase tracking-widest text-slate-400 mb-2">Nama Role</div>
                    <input
                      type="text"
                      value={createUserForm.customRole}
                      onChange={(e) => setCreateUserForm((p) => ({ ...p, customRole: e.target.value }))}
                      className="w-full bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl px-4 py-3 text-sm font-bold outline-none focus:ring-2 focus:ring-indigo-500/20"
                      placeholder="contoh: supervisor"
                      required
                    />
                  </div>
                )}

                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <div>
                    <div className="text-[10px] font-black uppercase tracking-widest text-slate-400 mb-2">Username (untuk login)</div>
                    <input
                      type="text"
                      value={createUserForm.username}
                      onChange={(e) => setCreateUserForm((p) => ({ ...p, username: e.target.value }))}
                      className="w-full bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl px-4 py-3 text-sm font-bold outline-none focus:ring-2 focus:ring-indigo-500/20"
                      placeholder="contoh: user01"
                      required
                    />
                  </div>
                  <div>
                    <div className="text-[10px] font-black uppercase tracking-widest text-slate-400 mb-2">Nama</div>
                    <input
                      type="text"
                      value={createUserForm.full_name}
                      onChange={(e) => setCreateUserForm((p) => ({ ...p, full_name: e.target.value }))}
                      className="w-full bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl px-4 py-3 text-sm font-bold outline-none focus:ring-2 focus:ring-indigo-500/20"
                      placeholder="Nama lengkap"
                      required
                    />
                  </div>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <div>
                    <div className="text-[10px] font-black uppercase tracking-widest text-slate-400 mb-2">Email</div>
                    <input
                      type="email"
                      value={createUserForm.email}
                      onChange={(e) => setCreateUserForm((p) => ({ ...p, email: e.target.value }))}
                      className="w-full bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl px-4 py-3 text-sm font-bold outline-none focus:ring-2 focus:ring-indigo-500/20"
                      placeholder="email@domain.com"
                      required
                    />
                  </div>
                  <div>
                    <div className="text-[10px] font-black uppercase tracking-widest text-slate-400 mb-2">Password</div>
                    <input
                      type="password"
                      value={createUserForm.password}
                      onChange={(e) => setCreateUserForm((p) => ({ ...p, password: e.target.value }))}
                      className="w-full bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl px-4 py-3 text-sm font-bold outline-none focus:ring-2 focus:ring-indigo-500/20"
                      required
                      minLength={6}
                    />
                  </div>
                </div>

                <div>
                  <div className="flex items-center justify-between mb-2">
                    <div className="text-[10px] font-black uppercase tracking-widest text-slate-400">Akses Fitur</div>
                    <button
                      type="button"
                      onClick={() => {
                        const hasAll = (createUserForm.permissions || []).includes('*');
                        setCreateUserForm((p) => ({ ...p, permissions: hasAll ? [] : ['*'] }));
                      }}
                      className="text-xs font-black text-indigo-600 hover:text-indigo-700"
                    >
                      {(createUserForm.permissions || []).includes('*') ? 'Clear' : 'Pilih Semua'}
                    </button>
                  </div>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    {[
                      { key: 'view_employees', label: 'Employees' },
                      { key: 'view_logs', label: 'Logs' },
                      { key: 'manual_attendance', label: 'Manual Input' },
                      { key: 'view_system', label: 'System' },
                      { key: '*', label: 'Semua Fitur' },
                    ].map((p) => (
                      <label
                        key={p.key}
                        className="flex items-center gap-3 bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-2xl px-4 py-3 cursor-pointer"
                      >
                        <input
                          type="checkbox"
                          checked={(createUserForm.permissions || []).includes(p.key)}
                          onChange={() => toggleCreateUserPermission(p.key)}
                          className="h-4 w-4"
                        />
                        <span className="text-sm font-black text-slate-800 dark:text-slate-100">{p.label}</span>
                      </label>
                    ))}
                  </div>
                  <div className="text-xs font-bold text-slate-500 dark:text-slate-400 mt-2">
                    Catatan: fitur Add Employee, Users management, dan System metrics tetap khusus manager.
                  </div>
                </div>

                <div className="flex items-center justify-end gap-2 pt-2">
                  <button
                    type="button"
                    disabled={createUserSaving}
                    onClick={() => setCreateUserOpen(false)}
                    className="px-4 py-3 rounded-xl bg-slate-50 dark:bg-slate-800 hover:bg-slate-100 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-100 text-sm font-black transition-all border border-slate-200 dark:border-slate-700 disabled:opacity-60"
                  >
                    Batal
                  </button>
                  <button
                    type="submit"
                    disabled={createUserSaving}
                    className="px-4 py-3 rounded-xl bg-indigo-600 hover:bg-indigo-700 disabled:opacity-60 text-white text-sm font-black transition-all shadow-lg shadow-indigo-500/20"
                  >
                    {createUserSaving ? 'Menyimpan…' : 'Simpan'}
                  </button>
                </div>
              </form>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Face Viewer Modal */}
      <AnimatePresence>
        {faceViewerOpen && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-[100] bg-black/60 backdrop-blur-sm flex items-center justify-center p-4"
            onMouseDown={() => setFaceViewerOpen(false)}
          >
            <motion.div
              initial={{ opacity: 0, y: 12, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 12, scale: 0.98 }}
              transition={{ duration: 0.15 }}
              className="w-full max-w-3xl bg-white dark:bg-slate-900 rounded-3xl shadow-2xl border border-slate-100 dark:border-slate-800 overflow-hidden"
              onMouseDown={(e) => e.stopPropagation()}
            >
              <div className="p-6 border-b border-slate-100 dark:border-slate-800 flex items-start justify-between gap-4">
                <div>
                  <div className="text-lg font-black text-slate-900 dark:text-slate-50 inline-flex items-center">
                    <Eye size={18} className="mr-2" /> Face Snapshots
                  </div>
                  <div className="text-sm font-bold text-slate-500 dark:text-slate-400 mt-1">
                    Employee ID: <span className="text-slate-800 dark:text-slate-100">{selectedEmployeeForFaces || '--'}</span>
                  </div>
                  <div className="text-sm font-bold text-slate-500 dark:text-slate-400 mt-0.5">
                    Employee Name: <span className="text-slate-800 dark:text-slate-100">{selectedEmployeeNameForFaces || '--'}</span>
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => setFaceViewerOpen(false)}
                  className="px-3 py-2 rounded-xl bg-slate-50 dark:bg-slate-800 hover:bg-slate-100 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-100 text-xs font-black transition-all border border-slate-200 dark:border-slate-700"
                >
                  Close
                </button>
              </div>
              <div className="p-6 max-h-[80vh] overflow-y-auto">
                {facesLoading ? (
                  <div className="text-sm font-bold text-slate-400 text-center py-12">Loading…</div>
                ) : employeeFaces.length === 0 ? (
                  <div className="flex flex-col items-center py-12 text-center">
                    <div className="w-16 h-16 rounded-2xl bg-slate-100 dark:bg-slate-800 flex items-center justify-center mb-3">
                      <Camera size={28} className="text-slate-400" />
                    </div>
                    <div className="text-sm font-black text-slate-700 dark:text-slate-200">
                      Belum ada gambar wajah
                    </div>
                    <div className="text-xs font-bold text-slate-400 mt-1">
                      Karyawan ini belum di-scan wajahnya saat registrasi.
                    </div>
                  </div>
                ) : (
                  <div className="flex flex-col items-center gap-6">
                    {employeeFaces.map((f) => (
                      <a
                        key={f.filename}
                        href={resolveBackendUrl(f.url)}
                        target="_blank"
                        rel="noreferrer"
                        className="block rounded-3xl overflow-hidden border border-slate-100 dark:border-slate-800 bg-slate-50 dark:bg-slate-950 shadow-lg hover:shadow-2xl transition-all"
                      >
                        <img
                          src={resolveBackendUrl(f.url)}
                          alt={f.filename}
                          onError={(ev) => {
                            (ev.currentTarget as HTMLImageElement).style.display = 'none';
                          }}
                          className="block w-auto h-auto max-w-[600px] max-h-[60vh] object-contain mx-auto"
                        />
                        <div className="px-4 py-3 text-[10px] font-black uppercase tracking-widest text-slate-500 dark:text-slate-400 text-center">
                          {f.filename}
                        </div>
                      </a>
                    ))}
                  </div>
                )}
                <div className="mt-6 text-xs font-bold text-slate-500 dark:text-slate-400 text-center">
                  AI face recognition di edge server menggunakan gambar ini sebagai referensi (face embedding) untuk match wajah live.
                </div>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Confirm Dialog */}
      <AnimatePresence>
        {confirmState.open && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-[150] bg-black/50 backdrop-blur-sm flex items-center justify-center p-4"
            onMouseDown={() => closeConfirm()}
          >
            <motion.div
              initial={{ opacity: 0, y: 12, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 12, scale: 0.98 }}
              transition={{ duration: 0.15 }}
              className="w-full max-w-md bg-white dark:bg-slate-900 rounded-3xl shadow-2xl border border-slate-100 dark:border-slate-800 overflow-hidden"
              onMouseDown={(e) => e.stopPropagation()}
            >
              <div className="p-6 flex items-start gap-4">
                <div
                  className={`shrink-0 w-12 h-12 rounded-2xl flex items-center justify-center ${
                    confirmState.tone === 'danger'
                      ? 'bg-rose-50 dark:bg-rose-900/20 text-rose-600 dark:text-rose-300'
                      : 'bg-indigo-50 dark:bg-indigo-900/20 text-indigo-600 dark:text-indigo-300'
                  }`}
                >
                  {confirmState.tone === 'danger' ? <Trash2 size={22} /> : <AlertCircle size={22} />}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="text-base font-black text-slate-900 dark:text-slate-50">{confirmState.title}</div>
                  <div className="text-sm font-bold text-slate-500 dark:text-slate-400 mt-1 break-words">
                    {confirmState.message}
                  </div>
                </div>
              </div>
              <div className="px-6 pb-6 flex items-center justify-end gap-2">
                <button
                  type="button"
                  disabled={confirmBusy}
                  onClick={closeConfirm}
                  className="px-4 py-2.5 rounded-xl bg-slate-50 dark:bg-slate-800 hover:bg-slate-100 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-100 text-sm font-black transition-all border border-slate-200 dark:border-slate-700 disabled:opacity-60"
                >
                  Batal
                </button>
                <button
                  type="button"
                  disabled={confirmBusy}
                  onClick={runConfirm}
                  className={`px-4 py-2.5 rounded-xl text-sm font-black text-white transition-all shadow-lg disabled:opacity-60 ${
                    confirmState.tone === 'danger'
                      ? 'bg-rose-600 hover:bg-rose-700 shadow-rose-500/20'
                      : 'bg-indigo-600 hover:bg-indigo-700 shadow-indigo-500/20'
                  }`}
                >
                  {confirmBusy ? 'Memproses…' : confirmState.confirmLabel}
                </button>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Toasts */}
      <div className="fixed top-4 right-4 z-[200] flex flex-col gap-3 w-[calc(100vw-2rem)] max-w-sm pointer-events-none">
        <AnimatePresence>
          {toasts.map(t => {
            const toneStyles = {
              success: 'border-emerald-200 bg-white text-emerald-700 dark:bg-slate-900 dark:border-emerald-900/40 dark:text-emerald-200',
              info: 'border-indigo-200 bg-white text-indigo-700 dark:bg-slate-900 dark:border-indigo-900/40 dark:text-indigo-200',
              warning: 'border-amber-200 bg-white text-amber-700 dark:bg-slate-900 dark:border-amber-900/40 dark:text-amber-200',
              error: 'border-rose-200 bg-white text-rose-700 dark:bg-slate-900 dark:border-rose-900/40 dark:text-rose-200',
            }[t.tone];
            const Icon =
              t.tone === 'success' ? CheckCircle2
              : t.tone === 'info' ? Info
              : t.tone === 'warning' ? AlertCircle
              : XCircle;
            return (
              <motion.div
                key={t.id}
                layout
                initial={{ opacity: 0, x: 24, scale: 0.96 }}
                animate={{ opacity: 1, x: 0, scale: 1 }}
                exit={{ opacity: 0, x: 24, scale: 0.96 }}
                transition={{ duration: 0.18 }}
                className={`pointer-events-auto rounded-2xl border shadow-2xl px-4 py-3 flex items-start gap-3 ${toneStyles}`}
              >
                <Icon size={20} className="shrink-0 mt-0.5" />
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-black truncate">{t.title}</div>
                  <div className="text-xs font-bold text-slate-600 dark:text-slate-300 mt-0.5 break-words">{t.message}</div>
                </div>
                <button
                  type="button"
                  onClick={() => dismissToast(t.id)}
                  className="shrink-0 p-1 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-800 text-slate-400"
                >
                  <X size={16} />
                </button>
              </motion.div>
            );
          })}
        </AnimatePresence>
      </div>
    </div>
  );
};

const SidebarItem = ({
  icon,
  label,
  active = false,
  collapsed = false,
  onClick,
}: {
  icon: any;
  label: string;
  active?: boolean;
  collapsed?: boolean;
  onClick: () => void;
}) => (
  <button
    type="button"
    onClick={onClick}
    className={`
      w-full flex items-center p-3 rounded-xl cursor-pointer transition-all duration-200 group
      ${active ? 'bg-indigo-600 text-white shadow-lg shadow-indigo-600/20' : 'hover:bg-slate-800 text-slate-400 hover:text-slate-200'}
    `}
  >
    <span className={`${active ? 'text-white' : 'group-hover:scale-110 transition-transform'}`}>{icon}</span>
    {!collapsed && <span className="ml-3 text-sm font-bold">{label}</span>}
    {!collapsed && active && <div className="ml-auto w-1.5 h-1.5 bg-white rounded-full"></div>}
  </button>
);

const STAT_ICON_BG: Record<string, string> = {
  emerald: 'bg-emerald-50 dark:bg-emerald-900/20',
  amber: 'bg-amber-50 dark:bg-amber-900/20',
  indigo: 'bg-indigo-50 dark:bg-indigo-900/20',
  cyan: 'bg-cyan-50 dark:bg-cyan-900/20',
};

const StatCard = ({ title, value, icon, color, trend, isString = false }: any) => (
  <div className="bg-white dark:bg-slate-900 p-6 rounded-3xl shadow-sm border border-slate-100 dark:border-slate-800 hover:shadow-xl hover:scale-[1.02] transition-all duration-300 group">
    <div className="flex items-center justify-between mb-4">
      <div className={`p-3 rounded-2xl ${STAT_ICON_BG[color] || 'bg-slate-50'} group-hover:scale-110 transition-transform`}>
        {icon}
      </div>
      {trend && (
        <span className={`text-xs font-bold px-2 py-1 rounded-full ${trend.startsWith('+') ? 'bg-emerald-50 text-emerald-600' : 'bg-rose-50 text-rose-600'}`}>
          {trend}
        </span>
      )}
    </div>
    <p className="text-sm font-medium text-slate-500 dark:text-slate-400 mb-1">{title}</p>
    <p className={`text-2xl font-black text-slate-800 dark:text-slate-100 ${isString ? 'text-xl' : ''}`}>{value}</p>
  </div>
);

const StatusBadge = ({ status, reason }: { status: string, reason?: string }) => {
  const styles: any = {
    present: "bg-emerald-50 text-emerald-600 border-emerald-100",
    sick: "bg-rose-50 text-rose-600 border-rose-100",
    permission: "bg-amber-50 text-amber-600 border-amber-100",
    other: "bg-slate-50 text-slate-700 border-slate-200",
    leave: "bg-indigo-50 text-indigo-700 border-indigo-200",
    half_day: "bg-cyan-50 text-cyan-700 border-cyan-200",
  };
  const labels: any = {
    present: "PRESENT",
    permission: "PERMISSION",
    sick: "SICK",
    other: "OTHERS",
    leave: "LEAVE",
    half_day: "HALF DAY",
  };

  return (
    <div className="flex flex-col">
      <span className={`px-3 py-1.5 rounded-xl text-[10px] font-black uppercase tracking-widest border w-fit ${styles[status] || "bg-slate-50 text-slate-700 border-slate-200"}`}>
        {labels[status] || String(status || '').toUpperCase()}
      </span>
      {reason && <span className="text-[10px] text-slate-400 mt-1 font-medium">{reason}</span>}
    </div>
  );
};

export default Dashboard;
