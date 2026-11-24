import { Check, ExternalLink, FileText, Flame, FolderGit2, RotateCcw, Send, TerminalSquare } from 'lucide-react';
import { useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import TaskDescription from './TaskDescription';
import { api, fmtTokens, stageName } from './api';
import { latestDocumentArtifact } from './graphUtils';
import { Artifact, Task } from './types';

export type InspectorTab = 'description' | 'stage' | 'document';

export default function Inspector({ task, stage, requestedArtifact = '', tab, onTabChange, reload, notify }: { task: Task; stage: number; requestedArtifact?: string; tab: InspectorTab; onTabChange: (tab: InspectorTab) => void; reload: () => void; notify: (message: string) => void }) {
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [selected, setSelected] = useState('');
  const [content, setContent] = useState('');
  const [documentContent, setDocumentContent] = useState('');
  const [chatContent, setChatContent] = useState('');
  const [answer, setAnswer] = useState('');
  const [logs, setLogs] = useState('');
  const [busy, setBusy] = useState(false);
  const prefix = ['', '01_', '02_', '03_', '04_', ''][stage];
  const stageArtifacts = artifacts.filter(a => a.name.startsWith(prefix));
  const document = latestDocumentArtifact(artifacts);
  const chat = artifacts.find(artifact => artifact.name === '00_yougile_chat.md');
  useEffect(() => { api<Artifact[]>(`/tasks/${task.id}/artifacts`).then(setArtifacts).catch(() => setArtifacts([])); }, [task.id, task.runs, task.status]);
  useEffect(() => {
    if (requestedArtifact && stageArtifacts.some(artifact => artifact.name === requestedArtifact)) setSelected(requestedArtifact);
    else setSelected(stageArtifacts.at(-1)?.name || '');
  }, [task.id, stage, artifacts, requestedArtifact]);
  useEffect(() => {
    if (!selected) { setContent(''); return; }
    fetch(`/api/tasks/${task.id}/artifacts/${selected}`).then(async response => {
      if (!response.ok) throw new Error((await response.json()).detail || 'Не удалось загрузить артефакт');
      return response.text();
    }).then(setContent).catch(e => setContent(e.message));
  }, [selected]);
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
  const action = async (kind: 'restart' | 'approve' | 'merge') => {
    setBusy(true);
    try { await api(`/tasks/${task.id}/${kind}${kind === 'merge' ? '' : `?stage=${stage}`}`, { method: 'POST', body: kind === 'merge' ? '{}' : undefined }); notify(kind === 'restart' ? 'Этап перезапущен' : kind === 'approve' ? 'Этап утвержден' : 'Merge подтвержден'); reload(); }
    catch (e) { notify((e as Error).message); } finally { setBusy(false); }
  };
  const submitAnswer = async () => {
    if (!answer.trim()) return;
    try { await api(`/tasks/${task.id}/answer`, { method: 'POST', body: JSON.stringify({ answer }) }); setAnswer(''); notify('Ответ сохранен, документация перезапущена'); reload(); } catch (e) { notify((e as Error).message); }
  };
  const runs = (task.runs || []).filter(r => r.stage === stage);
  let json: { status?: string; summary?: string; comments?: string[]; questions?: string[] } | null = null;
  if (selected.endsWith('.json')) { try { json = JSON.parse(content); } catch { json = null; } }
  return <div className="p-5">
    <div className="mb-5 flex border-b border-slate-800" role="tablist" aria-label="Инспектор задачи">{([['description', 'Описание'], ['stage', 'Этап'], ['document', 'Документ']] as [InspectorTab, string][]).map(([id, title]) => <button key={id} role="tab" aria-selected={tab === id} onClick={() => onTabChange(id)} className={`-mb-px border-b-2 px-3 py-2 text-xs transition ${tab === id ? 'border-indigo-400 text-indigo-300' : 'border-transparent text-slate-500 hover:text-slate-300'}`}>{title}</button>)}</div>
    {tab === 'description' && <div><TaskDescription task={task}/><section><h3 className="mb-3 text-xs font-medium text-slate-300">Чат Yougile</h3>{chat ? <div className="markdown text-xs leading-5 text-slate-400"><ReactMarkdown>{chatContent}</ReactMarkdown></div> : <p className="text-[11px] text-slate-600">Чат не импортирован</p>}</section></div>}
    {tab === 'document' && <section><div className="mb-3 flex items-center justify-between"><h3 className="text-xs font-medium text-slate-300">Последняя ревизия документации</h3>{document && <span className="text-[10px] text-slate-600">{document.name}</span>}</div><div className="card min-h-48 p-4">{document ? <div className="markdown text-xs leading-5 text-slate-400"><ReactMarkdown>{documentContent}</ReactMarkdown></div> : <p className="py-8 text-center text-[11px] text-slate-600">Документация еще не создана</p>}</div></section>}
    {tab === 'stage' && <>
      <div className="mb-5"><div className="mb-1 text-[10px] uppercase tracking-widest text-indigo-400">Инспектор · этап {stage}</div><h2 className="text-base font-semibold">{stageName(stage)}</h2></div>
      <section className="mb-5 space-y-2 text-[11px]">
        <h3 className="mb-2 font-medium text-slate-300">Пути и ссылки</h3>
        <div className="flex gap-2 break-all text-slate-500"><FolderGit2 size={14} className="shrink-0"/><span>{task.branch_name}</span>{task.pr_url && <a className="text-indigo-400" href={task.pr_url} target="_blank"><ExternalLink size={13}/></a>}</div>
        {stage === 3 && task.worktree_path && <div className="break-all text-slate-500">Worktree: {task.worktree_path}</div>}
        {selected && <div className="break-all text-slate-600">{stageArtifacts.find(artifact => artifact.name === selected)?.path}</div>}
      </section>
      <section className="mb-5">
        <div className="mb-2 flex items-center justify-between"><h3 className="flex items-center gap-2 text-xs font-medium text-slate-300"><FileText size={14}/>Артефакты</h3>{stageArtifacts.length > 0 && <select className="!w-24 !px-2 !py-1 text-[10px]" value={selected} onChange={e => setSelected(e.target.value)}>{stageArtifacts.map(a => <option key={a.name}>{a.name}</option>)}</select>}</div>
        <div className="card max-h-80 min-h-24 overflow-y-auto p-3">
          {json ? <div><span className={`badge ${json.status === 'APPROVED' ? 'badge-green' : json.status === 'REJECTED' ? 'badge-red' : 'badge-orange'}`}>{json.status || 'Вопросы по ТЗ'}</span>{json.summary && <p className="mt-3 text-xs text-slate-300">{json.summary}</p>}{json.comments && <ul className="mt-3 list-disc space-y-1 pl-4 text-xs text-slate-400">{json.comments.map((c, i) => <li key={i}>{c}</li>)}</ul>}{json.questions && <ol className="mt-3 list-decimal space-y-2 pl-4 text-xs text-slate-400">{json.questions.map((q, i) => <li key={i}>{q}</li>)}</ol>}</div> : selected ? <div className="markdown"><ReactMarkdown>{content}</ReactMarkdown></div> : <p className="py-5 text-center text-[11px] text-slate-600">Артефакт еще не создан</p>}
        </div>
        {task.status === 'waiting_answer' && stage === 1 && <div className="mt-3"><textarea rows={3} value={answer} onChange={e => setAnswer(e.target.value)} placeholder="Ответ на вопросы бота..."/><button onClick={submitAnswer} className="btn btn-primary mt-2 w-full"><Send size={14}/>Отправить ответ</button></div>}
      </section>
      <section className="mb-5"><h3 className="mb-2 flex items-center gap-2 text-xs font-medium text-slate-300"><Flame size={14}/>Расход токенов</h3>{runs.length ? <div className="space-y-2">{runs.map(r => <div key={r.id} className="rounded-lg border border-slate-800 bg-slate-900/50 p-3 text-[10px]"><div className="flex justify-between"><span>Попытка {r.attempt} {r.is_fallback && '· Fallback'}</span><span className="text-orange-300">{r.tokens_estimated && '~'}{fmtTokens(r.input_tokens + r.output_tokens)}</span></div><div className="mt-1 text-slate-600">{r.cli_used} {r.model_used}</div><div className="mt-2 grid grid-cols-3 gap-1 text-slate-500"><span>In: {fmtTokens(r.input_tokens)}</span><span>Out: {fmtTokens(r.output_tokens)}</span><span>Cache: {fmtTokens(r.cache_tokens)}</span></div>{r.error && <div className="mt-2 text-rose-400">{r.error}</div>}</div>)}</div> : <p className="text-[11px] text-slate-600">Запусков еще не было</p>}</section>
      <section className="mb-5"><h3 className="mb-2 flex items-center gap-2 text-xs font-medium text-slate-300"><TerminalSquare size={14}/>Live Logs</h3><pre className="scrollbar h-40 overflow-auto whitespace-pre-wrap rounded-lg border border-slate-800 bg-[#050912] p-3 font-mono text-[10px] leading-relaxed text-emerald-300/70">{logs || 'Ожидание вывода...'}</pre></section>
      <section className="flex flex-wrap gap-2 border-t border-slate-800 pt-4">{stage <= 4 && <><button disabled={busy} className="btn flex-1 !px-2 text-[11px]" onClick={() => action('restart')}><RotateCcw size={13}/>Перезапустить</button>{stage !== 3 && <button disabled={busy} className="btn btn-primary flex-1 !px-2 text-[11px]" onClick={() => action('approve')}><Check size={13}/>Апрувнуть</button>}</>}{stage === 5 && <button disabled={busy} className="btn btn-primary w-full" onClick={() => action('merge')}><Check size={14}/>Подтвердить Merge вручную</button>}</section>
    </>}
  </div>;
}
