import axios from 'axios';

const api = axios.create({
  baseURL: '/api',
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
});

let toastCallback = null;

export function setToastCallback(cb) {
  toastCallback = cb;
}

api.interceptors.response.use(
  (res) => res,
  (err) => {
    const msg = err.response?.data?.detail || err.message || 'Request failed';
    if (toastCallback) toastCallback({ type: 'error', message: msg });
    return Promise.reject(err);
  }
);

export default api;
