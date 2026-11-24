import { Check, ExternalLink, Flame, FolderGit2, RotateCcw, Send, TerminalSquare } from 'lucide-react';
import { memo, useEffect, useRef, useState } from 'react';
import TaskDescription from './TaskDescription';
import Markdown from './Markdown';
import { api, fmtTokens, spentTokens, stageName } from './api';
import { latestDocumentArtifact } from './graphUtils';
import { Artifact, Task } from './types';

export type InspectorTab = 'description' | 'stage' | 'document';

const DocumentView = memo(function DocumentView({ name, content }: { name?: string; content: string }) {
  return <section><div className="mb-3 flex items-center justify-between"><h3 className="text-xs font-medium text-slate-300">Последняя ревизия документации</h3>{name && <span className="text-[10px] text-slate-600">{name}</span>}</div><div className="card min-h-48 p-4">{name ? <div className="markdown text-xs leading-5 text-slate-400"><Markdown text={content}/></div> : <p className="py-8 text-center text-[11px] text-slate-600">Документация еще не создана</p>}</div></section>;
});

export default function Inspector({ task, stage, requestedArtifact: _requestedArtifact = '', tab, onTabChange, reload, notify }: { task: Task; stage: number; requestedArtifact?: string; tab: InspectorTab; onTabChange: (tab: InspectorTab) => void; reload: () => void; notify: (message: string) => void }) {
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [documentContent, setDocumentContent] = useState('');
  const [chatContent, setChatContent] = useState('');
  const [answer, setAnswer] = useState('');
  const [logs, setLogs] = useState('');
  const [busy, setBusy] = useState(false);
  const logRef = useRef<HTMLPreElement>(null);
  const document = latestDocumentArtifact(artifacts);
  const chat = artifacts.find(artifact => artifact.name === '00_yougile_chat.md');
  const runsKey = (task.runs || []).map(run => `${run.id}:${run.status}:${run.attempt}`).join(',');
  useEffect(() => { api<Artifact[]>(`/tasks/${task.id}/artifacts`).then(setArtifacts).catch(() => setArtifacts([])); }, [task.id, task.status, runsKey]);
  useEffect(() => {
    if (!document) { setDocumentContent(''); return; }
    fetch(`/api/tasks/${task.id}/artifacts/${document.name}`).then(response => response.text()).then(setDocumentContent);
  }, [task.id, document?.name]);
  useEffect(() => {
    if (!chat) { setChatContent(''); return; }
    fetch(`/api/tasks/${task.id}/artifacts/${chat.name}`).then(response => response.text()).then(setChatContent);
  }, [task.id, chat?.name]);
  useEffect(() => {
    setLogs('');
    const source = new EventSource(`/api/tasks/${task.id}/logs`);
    source.onmessage = e => { const data = JSON.parse(e.data); if (data.stage === stage) setLogs(v => (v + data.message).slice(-40000)); };
    return () => source.close();
  }, [task.id, stage]);
  useEffect(() => { if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight; }, [logs]);
  const action = async (kind: 'restart' | 'approve' | 'merge') => {
    setBusy(true);
    try { await api(`/tasks/${task.id}/${kind}${kind === 'merge' ? '' : `?stage=${stage}`}`, { method: 'POST', body: kind === 'merge' ? '{}' : undefined }); notify(kind === 'restart' ? 'Текущая сессия остановлена, запущена новая' : kind === 'approve' ? 'Этап утвержден' : 'Merge подтвержден'); reload(); }
    catch (e) { notify((e as Error).message); } finally { setBusy(false); }
  };
  const submitAnswer = async () => {
    if (!answer.trim()) return;
    try { await api(`/tasks/${task.id}/answer`, { method: 'POST', body: JSON.stringify({ answer }) }); setAnswer(''); notify('Ответ сохранен, документация перезапущена'); reload(); } catch (e) { notify((e as Error).message); }
  };
  const runs = (task.runs || []).filter(r => r.stage === stage);
  return <div className="p-5">
    <div className="mb-5 flex border-b border-slate-800" role="tablist" aria-label="Инспектор задачи">{([['description', 'Описание'], ['stage', 'Этап'], ['document', 'Документ']] as [InspectorTab, string][]).map(([id, title]) => <button key={id} role="tab" aria-selected={tab === id} onClick={() => onTabChange(id)} className={`-mb-px border-b-2 px-3 py-2 text-xs transition ${tab === id ? 'border-indigo-400 text-indigo-300' : 'border-transparent text-slate-500 hover:text-slate-300'}`}>{title}</button>)}</div>
    {tab === 'description' && <div><TaskDescription task={task}/><section><h3 className="mb-3 text-xs font-medium text-slate-300">Чат Yougile</h3>{chat ? <div className="markdown text-xs leading-5 text-slate-400"><Markdown text={chatContent}/></div> : <p className="text-[11px] text-slate-600">Чат не импортирован</p>}</section></div>}
    {tab === 'document' && <DocumentView name={document?.name} content={documentContent}/>}
    {tab === 'stage' && <>
      <div className="mb-5"><div className="mb-1 text-[10px] uppercase tracking-widest text-indigo-400">Инспектор · этап {stage}</div><h2 className="text-base font-semibold">{stageName(stage)}</h2></div>
      <section className="mb-5 space-y-2 text-[11px]">
        <h3 className="mb-2 font-medium text-slate-300">Пути и ссылки</h3>
        <div className="flex gap-2 break-all text-slate-500"><FolderGit2 size={14} className="shrink-0"/><span>{task.branch_name}</span>{task.pr_url && <a className="text-indigo-400" href={task.pr_url} target="_blank"><ExternalLink size={13}/></a>}</div>
        {stage === 3 && task.worktree_path && <div className="break-all text-slate-500">Worktree: {task.worktree_path}</div>}
      </section>
      {task.status === 'waiting_answer' && stage === 1 && <section className="mb-5"><textarea rows={3} value={answer} onChange={e => setAnswer(e.target.value)} placeholder="Ответ на вопросы бота..."/><button onClick={submitAnswer} className="btn btn-primary mt-2 w-full"><Send size={14}/>Отправить ответ</button></section>}
      <section className="mb-5"><h3 className="mb-2 flex items-center gap-2 text-xs font-medium text-slate-300"><Flame size={14}/>Расход токенов</h3>{runs.length ? <div className="space-y-2">{runs.map(r => <div key={r.id} className="rounded-lg border border-slate-800 bg-slate-900/50 p-3 text-[10px]"><div className="flex justify-between"><span>Попытка {r.attempt} {r.is_fallback && '· Fallback'}</span><span className="text-orange-300">{r.tokens_estimated && '~'}{fmtTokens(spentTokens(r))}</span></div><div className="mt-1 text-slate-600">{r.cli_used} {r.model_used}</div><div className="mt-2 grid grid-cols-3 gap-1 text-slate-500"><span>In: {fmtTokens(r.input_tokens)}</span><span>Out: {fmtTokens(r.output_tokens)}</span><span>Cache: {fmtTokens(r.cache_tokens)}</span></div>{r.error && <div className="mt-2 text-rose-400">{r.error}</div>}</div>)}</div> : <p className="text-[11px] text-slate-600">Запусков еще не было</p>}</section>
      <section className="mb-5"><h3 className="mb-2 flex items-center gap-2 text-xs font-medium text-slate-300"><TerminalSquare size={14}/>Live Logs</h3><pre ref={logRef} className="scrollbar h-40 overflow-auto whitespace-pre-wrap rounded-lg border border-slate-800 bg-[#050912] p-3 font-mono text-[10px] leading-relaxed text-emerald-300/70">{logs || 'Ожидание вывода...'}</pre></section>
      <section className="flex flex-wrap gap-2 border-t border-slate-800 pt-4">{stage <= 4 && <><button disabled={busy} className="btn flex-1 !px-2 text-[11px]" onClick={() => action('restart')}><RotateCcw size={13}/>Перезапустить</button>{stage !== 3 && <button disabled={busy} className="btn btn-primary flex-1 !px-2 text-[11px]" onClick={() => action('approve')}><Check size={13}/>Апрувнуть</button>}</>}{stage === 5 && <button disabled={busy} className="btn btn-primary w-full" onClick={() => action('merge')}><Check size={14}/>Подтвердить Merge вручную</button>}</section>
    </>}
  </div>;
}
