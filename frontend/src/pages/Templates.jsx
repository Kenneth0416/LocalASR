import { useState, useEffect } from 'react';
import api from '../api/client';
import Header from '../components/Header';
import { Plus, Trash2, Save, FileText } from 'lucide-react';

const emptyTemplate = {
  name: '',
  description: '',
  system_prompt: '',
  user_prompt: '',
  output_format: 'markdown',
  language: '',
};

export default function Templates() {
  const [templates, setTemplates] = useState([]);
  const [selected, setSelected] = useState(null);
  const [form, setForm] = useState(emptyTemplate);
  const [isNew, setIsNew] = useState(false);
  const [saving, setSaving] = useState(false);

  const loadTemplates = () => {
    api.get('/templates').then((res) => {
      setTemplates(res.data?.templates || []);
    }).catch(() => {});
  };

  useEffect(() => { loadTemplates(); }, []);

  const handleSelect = (t) => {
    setSelected(t);
    setIsNew(false);
    setForm({
      name: t.name,
      description: t.description,
      system_prompt: t.system_prompt,
      user_prompt: t.user_prompt,
      output_format: t.output_format,
      language: t.language,
    });
  };

  const handleNew = () => {
    setSelected(null);
    setIsNew(true);
    setForm(emptyTemplate);
  };

  const handleSave = async () => {
    if (!form.name.trim() || !form.system_prompt.trim() || !form.user_prompt.trim()) return;
    setSaving(true);
    try {
      if (isNew) {
        await api.post('/templates', form);
      } else {
        await api.put(`/templates/${selected.id}`, form);
      }
      loadTemplates();
      setIsNew(false);
    } catch {
      // error handled by interceptor
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (id) => {
    if (!confirm('確定要刪除這個模板嗎？')) return;
    try {
      await api.delete(`/templates/${id}`);
      if (selected?.id === id) {
        setSelected(null);
        setForm(emptyTemplate);
      }
      loadTemplates();
    } catch {
      // error handled by interceptor
    }
  };

  const presets = templates.filter((t) => t.is_preset);
  const customs = templates.filter((t) => !t.is_preset);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
      <Header title="模板管理">
        <button
          onClick={handleNew}
          style={{
            display: 'flex', alignItems: 'center', gap: 6,
            padding: '6px 12px', borderRadius: 20,
            background: 'var(--accent)', color: 'white',
            border: 'none', fontSize: 12, fontWeight: 600,
          }}
        >
          <Plus size={14} />
          新增模板
        </button>
      </Header>

      <div style={{ flex: 1, display: 'flex', overflow: 'hidden' }}>
        {/* Template list */}
        <div style={{
          width: 320, flexShrink: 0,
          borderRight: '1px solid var(--border)',
          overflowY: 'auto', background: 'var(--bg)',
        }}>
          {presets.length > 0 && (
            <div style={{ padding: '12px 16px 4px', fontSize: 11, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
              預設模板
            </div>
          )}
          {presets.map((t) => (
            <TemplateItem key={t.id} template={t} selected={selected?.id === t.id} onSelect={handleSelect} />
          ))}

          {customs.length > 0 && (
            <div style={{ padding: '12px 16px 4px', fontSize: 11, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
              自訂模板
            </div>
          )}
          {customs.map((t) => (
            <TemplateItem key={t.id} template={t} selected={selected?.id === t.id} onSelect={handleSelect} onDelete={handleDelete} />
          ))}

          {templates.length === 0 && (
            <div style={{ padding: 32, textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>
              尚無模板
            </div>
          )}
        </div>

        {/* Edit form */}
        <div style={{ flex: 1, overflowY: 'auto', background: 'var(--bg)', padding: 24 }}>
          {!selected && !isNew ? (
            <div style={{ padding: 32, textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>
              選擇左側模板進行編輯，或點擊「新增模板」
            </div>
          ) : (
            <div style={{ maxWidth: 640 }}>
              <h2 style={{ fontSize: 18, fontWeight: 700, marginBottom: 20 }}>
                {isNew ? '新增模板' : `編輯：${selected?.name || ''}`}
                {selected?.is_preset && (
                  <span style={{ marginLeft: 8, fontSize: 11, padding: '2px 6px', borderRadius: 4, background: 'var(--accent-dim)', color: 'var(--accent)' }}>
                    預設
                  </span>
                )}
              </h2>

              <FormField label="模板名稱">
                <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} style={inputStyle} />
              </FormField>

              <FormField label="用途說明">
                <input value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} style={inputStyle} />
              </FormField>

              <FormField label="System Prompt">
                <textarea
                  value={form.system_prompt}
                  onChange={(e) => setForm({ ...form, system_prompt: e.target.value })}
                  rows={4}
                  style={{ ...inputStyle, resize: 'vertical' }}
                />
              </FormField>

              <FormField label="User Prompt">
                <textarea
                  value={form.user_prompt}
                  onChange={(e) => setForm({ ...form, user_prompt: e.target.value })}
                  rows={6}
                  style={{ ...inputStyle, resize: 'vertical' }}
                />
                <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 4 }}>
                  使用 {'{transcript_text}'} 作為轉錄文字的佔位符
                </div>
              </FormField>

              <FormField label="輸出格式">
                <select value={form.output_format} onChange={(e) => setForm({ ...form, output_format: e.target.value })} style={inputStyle}>
                  <option value="markdown">Markdown</option>
                  <option value="plain">純文字</option>
                  <option value="json">JSON</option>
                </select>
              </FormField>

              <FormField label="輸出語言">
                <select value={form.language} onChange={(e) => setForm({ ...form, language: e.target.value })} style={inputStyle}>
                  <option value="">自動偵測</option>
                  <option value="Chinese">中文</option>
                  <option value="English">English</option>
                  <option value="Japanese">日本語</option>
                  <option value="Korean">한국어</option>
                </select>
              </FormField>

              <div style={{ display: 'flex', gap: 8, marginTop: 20 }}>
                <button
                  onClick={handleSave}
                  disabled={saving || !form.name.trim() || !form.system_prompt.trim() || !form.user_prompt.trim()}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 6,
                    padding: '8px 16px', borderRadius: 8,
                    background: 'var(--accent)', color: 'white',
                    border: 'none', fontSize: 13, fontWeight: 600,
                    opacity: saving ? 0.6 : 1,
                  }}
                >
                  <Save size={14} />
                  {saving ? '儲存中...' : '儲存'}
                </button>

                {selected?.is_preset && (
                  <span style={{ fontSize: 12, color: 'var(--text-muted)', alignSelf: 'center' }}>
                    預設模板不可刪除
                  </span>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function TemplateItem({ template, selected, onSelect, onDelete }) {
  return (
    <div
      onClick={() => onSelect(template)}
      style={{
        padding: '10px 16px', cursor: 'pointer',
        background: selected ? 'var(--accent-dim)' : 'transparent',
        borderLeft: selected ? '2px solid var(--accent)' : '2px solid transparent',
        transition: 'all 0.15s',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <FileText size={13} style={{ color: 'var(--text-muted)' }} />
          <span style={{ fontSize: 13, fontWeight: 500, color: selected ? 'var(--accent)' : 'var(--text)' }}>
            {template.name}
          </span>
          {template.is_preset && (
            <span style={{ fontSize: 10, padding: '1px 4px', borderRadius: 3, background: 'var(--accent-dim)', color: 'var(--accent)' }}>
              預設
            </span>
          )}
        </div>
        {onDelete && (
          <button
            onClick={(e) => { e.stopPropagation(); onDelete(template.id); }}
            style={{ color: 'var(--text-muted)', padding: 2, background: 'none', border: 'none', cursor: 'pointer' }}
          >
            <Trash2 size={13} />
          </button>
        )}
      </div>
      {template.description && (
        <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 4, paddingLeft: 19 }}>
          {template.description}
        </div>
      )}
    </div>
  );
}

function FormField({ label, children }) {
  return (
    <div style={{ marginBottom: 16 }}>
      <label style={{ display: 'block', fontSize: 12, fontWeight: 600, color: 'var(--text-muted)', marginBottom: 6 }}>
        {label}
      </label>
      {children}
    </div>
  );
}

const inputStyle = {
  width: '100%', padding: '8px 12px', borderRadius: 8,
  border: '1px solid var(--border)', background: 'var(--surface2)',
  color: 'var(--text)', fontSize: 13, fontFamily: 'inherit',
};
