import { ExternalLink, FileText, History } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import Markdown from './Markdown';
import { Modal } from './components';
import { api, stageName } from './api';
import { stageArtifactRevisions } from './graphUtils';
import { Artifact, Task } from './types';

const resultTitles: Record<number, string> = {
  1: 'Техническая документация',
  2: 'Вердикт ревью документации',
  3: 'Отчёт о реализации',
  4: 'Вердикт ревью реализации',
};

const revisionOf = (name: string) => Number(name.match(/_v(\d+)\./)?.[1] || 0);

/** A modal window with the latest revision produced by the edge's source stage. */
export default function RevisionWindow({ task, source, target, onClose }: { task: Task; source: number; target: number; onClose: () => void }) {
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [selectedName, setSelectedName] = useState('');
  const [content, setContent] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const revisions = useMemo(() => stageArtifactRevisions(artifacts, source), [artifacts, source]);
  const active = revisions.find(artifact => artifact.name === selectedName) || revisions.at(-1);
  const isJson = active?.name.endsWith('.json') || false;
  const runsKey = (task.runs || []).map(run => `${run.id}:${run.status}:${run.attempt}`).join(',');
  const displayContent = useMemo(() => {
    if (!isJson) return content;
    try { return JSON.stringify(JSON.parse(content), null, 2); } catch { return content; }
  }, [content, isJson]);

  useEffect(() => {
    setSelectedName('');
  }, [source, target]);

  useEffect(() => {
    const handler = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [onClose]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api<Artifact[]>(`/tasks/${task.id}/artifacts`)
      .then(list => { if (!cancelled) setArtifacts(list); })
      .catch(() => { if (!cancelled) setArtifacts([]); });
    return () => { cancelled = true; };
  }, [task.id, task.status, runsKey]);

  useEffect(() => {
    if (!active) { setContent(''); setLoading(false); return; }
    let cancelled = false;
    setLoading(true);
    setError('');
    fetch(`/api/tasks/${task.id}/artifacts/${encodeURIComponent(active.name)}`)
      .then(response => { if (!response.ok) throw new Error(`Не удалось загрузить ${active.name}`); return response.text(); })
      .then(text => { if (!cancelled) setContent(text); })
      .catch(reason => { if (!cancelled) setError(reason instanceof Error ? reason.message : 'Не удалось загрузить ревизию'); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [task.id, active?.name]);

  return createPortal(
    <Modal title={`Результат этапа ${source}`} onClose={onClose} wide>
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-[10px] uppercase tracking-widest text-indigo-400"><History size={13}/>Этап {source} → Этап {target}</div>
          <div className="mt-1 text-sm font-medium text-slate-200">{resultTitles[source] || stageName(source)}</div>
          <div className="mt-0.5 text-[11px] text-slate-500">{stageName(source)} → {stageName(target)}</div>
        </div>
        {revisions.length > 0 && <div className="flex flex-wrap items-center gap-1" role="group" aria-label="Ревизии">
          {revisions.map((artifact, index) => <button key={artifact.name} type="button" onClick={() => setSelectedName(artifact.name)} aria-pressed={artifact.name === active?.name} className={`rounded-md border px-2.5 py-1 text-[11px] transition ${artifact.name === active?.name ? 'border-indigo-400 bg-indigo-500/20 text-indigo-200' : 'border-slate-700 text-slate-400 hover:border-slate-500 hover:text-slate-200'}`}>v{revisionOf(artifact.name)}{index === revisions.length - 1 ? ' · последняя' : ''}</button>)}
        </div>}
      </div>
      {!revisions.length ? <div className="card flex flex-col items-center justify-center px-6 py-14 text-center text-slate-500"><FileText size={30} className="mb-3 opacity-60"/><b className="text-sm text-slate-300">Результат этапа ещё не готов</b><p className="mt-1 max-w-sm text-xs">Окно откроет последнюю ревизию, как только этап {source} создаст артефакт.</p></div> : <div className="scrollbar max-h-[60vh] min-h-40 overflow-y-auto rounded-xl border border-slate-800 bg-[#080e1b] p-4">
        {loading ? <p className="py-8 text-center text-xs text-slate-500">Загрузка ревизии…</p> : error ? <p className="py-8 text-center text-xs text-rose-400">{error}</p> : isJson ? <pre className="whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-slate-300">{displayContent}</pre> : <div className="markdown"><Markdown text={displayContent}/></div>}
      </div>}
      <footer className="mt-4 flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0 text-[10px] text-slate-600">{active ? <span className="break-all">storage/tasks/{task.id}/{active.name}</span> : <span>{task.branch_name}</span>}</div>
        <div className="flex gap-2">
          {active && <a className="btn !px-3 text-[11px]" href={`/api/tasks/${task.id}/artifacts/${encodeURIComponent(active.name)}`} target="_blank" rel="noreferrer"><ExternalLink size={13}/>В новой вкладке</a>}
          <button type="button" className="btn btn-primary !px-4 text-[11px]" onClick={onClose}>Закрыть</button>
        </div>
      </footer>
    </Modal>,
    document.body,
  );
}
