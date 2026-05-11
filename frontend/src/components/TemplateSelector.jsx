import { useState, useEffect } from 'react';
import api from '../api/client';

export default function TemplateSelector({ value, onChange, disabled }) {
  const [templates, setTemplates] = useState([]);

  useEffect(() => {
    api.get('/templates').then((res) => {
      setTemplates(res.data?.templates || []);
    }).catch(() => {});
  }, []);

  const presets = templates.filter((t) => t.is_preset);
  const customs = templates.filter((t) => !t.is_preset);

  return (
    <select
      value={value || ''}
      onChange={(e) => onChange(e.target.value || null)}
      disabled={disabled}
      style={{
        padding: '4px 8px', borderRadius: 6,
        border: '1px solid var(--border)',
        background: 'var(--surface2)', color: 'var(--text-muted)',
        fontSize: 11, fontFamily: 'inherit', maxWidth: 140,
      }}
    >
      {presets.length > 0 && (
        <optgroup label="預設模板">
          {presets.map((t) => (
            <option key={t.id} value={t.id}>{t.name}</option>
          ))}
        </optgroup>
      )}
      {customs.length > 0 && (
        <optgroup label="自訂模板">
          {customs.map((t) => (
            <option key={t.id} value={t.id}>{t.name}</option>
          ))}
        </optgroup>
      )}
    </select>
  );
}
