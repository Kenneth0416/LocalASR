import React, { useState, createContext, useContext, useCallback } from 'react';

const ToastContext = createContext(null);

export function useToast() {
  return useContext(ToastContext);
}

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([]);

  const addToast = useCallback((toast) => {
    const id = Date.now() + Math.random();
    setToasts((prev) => [...prev, { id, ...toast }]);
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 4000);
  }, []);

  return (
    <ToastContext.Provider value={addToast}>
      {children}
      <div style={{ position: 'fixed', top: 16, right: 16, zIndex: 9999, display: 'flex', flexDirection: 'column', gap: 8 }}>
        {toasts.map((t) => (
          <div
            key={t.id}
            style={{
              padding: '10px 16px',
              borderRadius: 8,
              background: t.type === 'error' ? 'var(--red-dim)' : t.type === 'success' ? 'var(--green-dim)' : 'var(--accent-dim)',
              color: t.type === 'error' ? 'var(--red)' : t.type === 'success' ? 'var(--green)' : 'var(--accent)',
              border: `1px solid ${t.type === 'error' ? 'var(--red)' : t.type === 'success' ? 'var(--green)' : 'var(--accent)'}30`,
              fontSize: 13,
              fontWeight: 500,
              maxWidth: 320,
              animation: 'slideIn 0.2s ease',
            }}
          >
            {t.message}
          </div>
        ))}
      </div>
      <style>{`
        @keyframes slideIn {
          from { transform: translateX(20px); opacity: 0; }
          to { transform: translateX(0); opacity: 1; }
        }
      `}</style>
    </ToastContext.Provider>
  );
}
