import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism';

const markdownStyles = {
  h1: {
    color: 'var(--text)',
    fontSize: 18,
    fontWeight: 'bold',
    marginTop: 16,
    marginBottom: 8,
  },
  h2: {
    color: 'var(--text)',
    fontSize: 16,
    fontWeight: 'bold',
    marginTop: 14,
    marginBottom: 6,
  },
  h3: {
    color: 'var(--text)',
    fontSize: 14,
    fontWeight: 'bold',
    marginTop: 12,
    marginBottom: 4,
  },
  p: {
    color: 'var(--text)',
    fontSize: 13,
    lineHeight: 1.7,
    margin: '0 0 8px',
  },
  ul: {
    color: 'var(--text)',
    fontSize: 13,
    paddingLeft: 20,
  },
  ol: {
    color: 'var(--text)',
    fontSize: 13,
    paddingLeft: 20,
  },
  li: {
    marginBottom: 4,
  },
  blockquote: {
    borderLeft: '3px solid var(--accent)',
    color: 'var(--text-muted)',
    paddingLeft: 12,
    margin: '8px 0',
  },
  table: {
    borderCollapse: 'collapse',
    fontSize: 13,
    width: '100%',
  },
  th: {
    backgroundColor: 'var(--surface)',
    padding: '8px 12px',
    textAlign: 'left',
    fontWeight: 600,
    border: '1px solid var(--border)',
  },
  td: {
    padding: '8px 12px',
    borderTop: '1px solid var(--border)',
    border: '1px solid var(--border)',
  },
  a: {
    color: 'var(--accent)',
  },
  hr: {
    border: 'none',
    borderTop: '1px solid var(--border)',
    margin: '16px 0',
  },
  strong: {
    color: 'var(--text)',
    fontWeight: 'bold',
  },
  em: {
    color: 'var(--text-muted)',
    fontStyle: 'italic',
  },
};

function CodeBlock({ node, inline, className, children, ...props }) {
  const match = /language-(\w+)/.exec(className || '');
  const language = match ? match[1] : '';

  if (!inline && (match || String(children).includes('\n'))) {
    return (
      <SyntaxHighlighter
        style={oneDark}
        language={language || 'text'}
        PreTag="div"
        customStyle={{
          backgroundColor: 'var(--surface2)',
          border: '1px solid var(--surface)',
          borderRadius: 8,
          margin: '8px 0',
          fontSize: 12,
        }}
        {...props}
      >
        {String(children).replace(/\n$/, '')}
      </SyntaxHighlighter>
    );
  }

  return (
    <code
      style={{
        backgroundColor: 'var(--surface2)',
        color: 'var(--accent)',
        fontFamily: "'JetBrains Mono', monospace",
        fontSize: 12,
        padding: '2px 5px',
        borderRadius: 4,
      }}
      {...props}
    >
      {children}
    </code>
  );
}

export default function MarkdownRenderer({ children }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        h1: ({ node, ...props }) => <h1 style={markdownStyles.h1} {...props} />,
        h2: ({ node, ...props }) => <h2 style={markdownStyles.h2} {...props} />,
        h3: ({ node, ...props }) => <h3 style={markdownStyles.h3} {...props} />,
        p: ({ node, ...props }) => <p style={markdownStyles.p} {...props} />,
        ul: ({ node, ...props }) => <ul style={markdownStyles.ul} {...props} />,
        ol: ({ node, ...props }) => <ol style={markdownStyles.ol} {...props} />,
        li: ({ node, ...props }) => <li style={markdownStyles.li} {...props} />,
        blockquote: ({ node, ...props }) => (
          <blockquote style={markdownStyles.blockquote} {...props} />
        ),
        table: ({ node, ...props }) => (
          <table style={markdownStyles.table} {...props} />
        ),
        th: ({ node, ...props }) => <th style={markdownStyles.th} {...props} />,
        td: ({ node, ...props }) => <td style={markdownStyles.td} {...props} />,
        a: ({ node, ...props }) => <a style={markdownStyles.a} {...props} />,
        hr: ({ node, ...props }) => <hr style={markdownStyles.hr} {...props} />,
        strong: ({ node, ...props }) => (
          <strong style={markdownStyles.strong} {...props} />
        ),
        em: ({ node, ...props }) => <em style={markdownStyles.em} {...props} />,
        code: CodeBlock,
      }}
    >
      {children}
    </ReactMarkdown>
  );
}
