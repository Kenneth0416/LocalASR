import { FileText } from 'lucide-react';

export default function EmptyState({ icon: Icon = FileText, title, subtitle, action }) {
  return (
    <div style={{
      display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
      gap: 12, padding: 48, textAlign: 'center', color: 'var(--text-muted)',
    }}>
      <Icon size={40} strokeWidth={1.2} />
      <div style={{ fontSize: 15, fontWeight: 600, color: 'var(--text)' }}>{title}</div>
      {subtitle && <div style={{ fontSize: 13 }}>{subtitle}</div>}
      {action}
    </div>
  );
}
