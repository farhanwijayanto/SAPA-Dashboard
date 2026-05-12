import axios from 'axios';

const envBaseUrl = import.meta.env.VITE_API_URL;

const defaultBaseUrl = envBaseUrl || '/api';

const api = axios.create({
  baseURL: defaultBaseUrl,
});

api.interceptors.request.use((config) => {
  const token = localStorage.getItem('token');
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

export default api;
