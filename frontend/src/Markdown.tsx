import { memo, ReactNode, useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import mermaid from 'mermaid';

let mermaidReady = false;

function MermaidDiagram({ chart }: { chart: string }) {
  const host = useRef<HTMLDivElement>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    const target = host.current;
    if (!target) return;
    if (!mermaidReady) {
      mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'dark' });
      mermaidReady = true;
    }
    const sandbox = document.createElement('div');
    sandbox.setAttribute('aria-hidden', 'true');
    sandbox.style.cssText = 'position:fixed;left:-10000px;top:0;width:800px;visibility:hidden;pointer-events:none';
    document.body.appendChild(sandbox);
    let cancelled = false;
    const id = `mmd${Math.random().toString(36).slice(2)}`;
    mermaid.render(id, chart, sandbox).then(result => {
      if (!cancelled) target.innerHTML = result.svg;
    }).catch((reason: unknown) => {
      if (!cancelled) setError(reason instanceof Error ? reason.message : 'Не удалось отобразить диаграмму');
    }).finally(() => sandbox.remove());
    return () => {
      cancelled = true;
      sandbox.remove();
    };
  }, [chart]);
  if (error) return <pre className="text-rose-400">{error}</pre>;
  return <div ref={host} className="mermaid-diagram" />;
}

function diagramChart(children: ReactNode): string | null {
  const child = Array.isArray(children) ? children[0] : children;
  if (!child || typeof child !== 'object' || !('props' in child)) return null;
  const props = (child as { props?: { className?: string; children?: ReactNode } }).props;
  if (!String(props?.className || '').includes('language-mermaid')) return null;
  return String(props?.children ?? '').replace(/\n$/, '');
}

const markdownComponents = {
  pre({ children }: { children?: ReactNode }) {
    const chart = diagramChart(children);
    if (chart !== null) return <MermaidDiagram chart={chart} />;
    return <pre>{children}</pre>;
  },
};

const Markdown = memo(function Markdown({ text }: { text: string }) {
  return <ReactMarkdown components={markdownComponents}>{text}</ReactMarkdown>;
});

export default Markdown;
