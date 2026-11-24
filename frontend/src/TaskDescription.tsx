import { useMemo } from 'react';
import ReactMarkdown from 'react-markdown';
import { Task } from './types';

function htmlToMarkdown(source: string) {
  const document = new DOMParser().parseFromString(source, 'text/html');
  const render = (node: ChildNode): string => {
    if (node.nodeType === Node.TEXT_NODE) return node.textContent || '';
    if (node.nodeType !== Node.ELEMENT_NODE) return '';
    const element = node as Element;
    const content = Array.from(element.childNodes).map(render).join('');
    switch (element.tagName) {
      case 'SCRIPT': case 'STYLE': return '';
      case 'P': case 'DIV': return `\n\n${content.trim()}\n\n`;
      case 'BR': return '\n';
      case 'STRONG': case 'B': return `**${content.trim()}**`;
      case 'EM': case 'I': return `*${content.trim()}*`;
      case 'UL': case 'OL': return `\n${content}`;
      case 'LI': return `- ${content.trim()}\n`;
      case 'H1': case 'H2': case 'H3': case 'H4': return `\n\n### ${content.trim()}\n\n`;
      case 'CODE': return `\`${content.trim()}\``;
      case 'PRE': return `\n\n\`\`\`\n${content.trim()}\n\`\`\`\n\n`;
      case 'A': {
        const href = element.getAttribute('href') || '';
        return /^https?:\/\//i.test(href) ? `[${content.trim() || href}](${href})` : content;
      }
      default: return content;
    }
  };
  return Array.from(document.body.childNodes).map(render).join('').replace(/\n{3,}/g, '\n\n').trim();
}

export default function TaskDescription({ task }: { task: Task }) {
  const description = useMemo(() => htmlToMarkdown(task.description || 'Описание не задано'), [task.description]);
  return <section className="mb-6 border-b border-slate-800 pb-5">
    <div className="mb-2 text-[10px] uppercase tracking-widest text-indigo-400">Описание задачи</div>
    <div className="flex items-start justify-between gap-3"><h2 className="text-sm font-semibold leading-5 text-slate-100">{task.title}</h2>{task.external_issue_url && <a className="shrink-0 text-[11px] text-indigo-400 hover:text-indigo-300" href={task.external_issue_url} target="_blank" rel="noreferrer">{task.external_issue_key || 'Yougile'} ↗</a>}</div>
    {(task.milestone || task.sprint) && <div className="mt-2 text-[11px] text-slate-500">{[task.milestone, task.sprint].filter(Boolean).join(' → ')}</div>}
    <div className="markdown mt-3 text-xs leading-5 text-slate-400"><ReactMarkdown>{description}</ReactMarkdown></div>
  </section>;
}
