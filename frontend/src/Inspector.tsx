import TokenUsage from './TokenUsage';
import { Check, ExternalLink, Flame, FolderGit2, Hand, Pause, Play, RotateCcw, Send, TerminalSquare } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import TaskDescription from './TaskDescription';
import DocumentationUsage from './DocumentationUsage';
import Markdown from './Markdown';
import { api, canPausePipeline, canStartPipeline, fmtTokens, spentTokens, stageName } from './api';
import { artifactUrl } from './ArtifactPage';
import { Artifact, Task } from './types';

export type InspectorTab = 'description' | 'stage';

export default function Inspector({ task, stage, tab, onTabChange, reload, notify }: { task: Task; stage: number; tab: InspectorTab; onTabChange: (tab: InspectorTab) => void; reload: () => void; notify: (message: string) => void }) {
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [chatContent, setChatContent] = useState('');
  const [answer, setAnswer] = useState('');
  const [logs, setLogs] = useState('');
  const [busy, setBusy] = useState(false);
  const logRef = useRef<HTMLPreElement>(null);
  const chat = artifacts.find(artifact => artifact.name === '00_yougile_chat.md');
  const lastSent = task.runs?.slice().reverse().find(run => run.sent_prompt != null);
  const runsKey = (task.runs || []).map(run => `${run.id}:${run.status}:${run.attempt}`).join(',');
  useEffect(() => {
    if (tab !== 'description') return;
    const controller = new AbortController();
    let timer: number | undefined;
    const load = async () => {
      try {
        const list = await api<Artifact[]>(`/tasks/${task.id}/artifacts`, { signal: controller.signal });
        if (!controller.signal.aborted) setArtifacts(list);
      } catch {
        if (controller.signal.aborted) return;
      }
      if (!controller.signal.aborted && task.status === 'running') timer = window.setTimeout(load, 2500);
    };
    void load();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [task.id, task.status, runsKey, tab]);
  useEffect(() => {
    if (!chat) { setChatContent(''); return; }
    const controller = new AbortController();
    fetch(`/api/tasks/${task.id}/artifacts/${chat.name}`, { signal: controller.signal })
      .then(response => response.ok ? response.text() : '')
      .then(content => { if (!controller.signal.aborted) setChatContent(content); })
      .catch(() => { if (!controller.signal.aborted) setChatContent(''); });
    return () => controller.abort();
  }, [task.id, chat?.name]);
  useEffect(() => {
    setLogs('');
    const source = new EventSource(`/api/tasks/${task.id}/logs`);
    source.onmessage = e => { const data = JSON.parse(e.data); if (data.stage === stage) setLogs(v => (v + data.message).slice(-40000)); };
    return () => source.close();
  }, [task.id, stage]);
  useEffect(() => { if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight; }, [logs]);
  const action = async (kind: 'stage' | 'pipeline' | 'restart' | 'approve' | 'merge' | 'pause' | 'resume') => {
    const actions = {
      stage: [`start?stage=${stage}`, 'Этап поставлен в очередь'],
      pipeline: ['start?pipeline=true', 'Пайплайн поставлен в очередь'],
      restart: [`restart?stage=${stage}`, 'Запрошен перезапуск этапа'],
      approve: [`approve?stage=${stage}`, 'Этап утверждён'],
      merge: ['merge', 'Merge подтверждён'],
      pause: ['pause', 'Пайплайн приостановлен'],
      resume: ['start', 'Задача продолжена'],
    };
    const [path, message] = actions[kind];
    setBusy(true);
    try { await api(`/tasks/${task.id}/${path}`, { method: 'POST', body: kind === 'merge' ? '{}' : undefined }); notify(message); await reload(); }
    catch (e) { notify((e as Error).message); } finally { setBusy(false); }
  };
  const submitAnswer = async () => {
    if (!answer.trim()) return;
    try { await api(`/tasks/${task.id}/answer`, { method: 'POST', body: JSON.stringify({ answer }) }); setAnswer(''); notify('Ответ сохранен, документация перезапущена'); reload(); } catch (e) { notify((e as Error).message); }
  };
  const runs = (task.runs || []).filter(r => r.stage === stage);
  const nextStage = task.status === 'open' ? 1 : task.current_stage;
  const pipelineAvailable = canStartPipeline(task);
  const stageAvailable = pipelineAvailable || task.status === 'waiting_pr';
  const canRestart = !task.merged_to_main && !['open', 'blocked_main', 'done', 'paused'].includes(task.status) && stage <= task.current_stage;
  const stageUnavailableReason = stage > nextStage
    ? 'Сначала завершите предыдущие этапы'
    : !task.stage_agents[stage] ? 'Назначьте бота на этап' : undefined;
  return <div className="p-5">
    {pipelineAvailable && <button disabled={busy || !task.stage_agents[nextStage]} title={task.stage_agents[nextStage] ? undefined : `Назначьте бота на этап ${nextStage}`} className="btn btn-primary mb-3 w-full" onClick={() => action('pipeline')}><Play size={14}/>Запустить пайплайн</button>}
    {canPausePipeline(task) && <button disabled={busy} className="btn mb-3 w-full" onClick={() => action('pause')}><Pause size={14}/>Приостановить</button>}
    {task.status === 'paused' && <div className="mb-3 space-y-2"><p className="text-xs text-amber-300">На паузе · этап {task.current_stage}</p><button disabled={busy || !task.stage_agents[nextStage]} title={task.stage_agents[nextStage] ? undefined : `Назначьте бота на этап ${nextStage}`} className="btn btn-primary w-full" onClick={() => action('resume')}><Play size={14}/>Продолжить</button></div>}
    <div className="mb-5 flex border-b border-slate-800" role="tablist" aria-label="Инспектор задачи">{([['description', 'Описание'], ['stage', 'Этап']] as const).map(([id, title]) => <button key={id} role="tab" aria-selected={tab === id} onClick={() => onTabChange(id)} className={`-mb-px border-b-2 px-3 py-2 text-xs transition ${tab === id ? 'border-indigo-400 text-indigo-300' : 'border-transparent text-slate-500 hover:text-slate-300'}`}>{title}</button>)}</div>
    {task.status === 'needs_human' && <section className="mb-5 rounded-lg border border-amber-700/60 bg-amber-950/30 p-3 text-xs"><div className="flex items-start gap-2 text-amber-200"><Hand size={15} className="mt-0.5 shrink-0"/><div><b>Нужно решение человека</b>{task.hold_reason && <p className="mt-1 text-amber-200/80">{task.hold_reason}</p>}</div></div><button disabled={busy} className="btn btn-primary mt-3 w-full" onClick={() => action('resume')}><Play size={14}/>Продолжить</button></section>}
    {tab === 'description' && <>
      <TaskDescription task={task}/>
      {task.documentation_usage && <DocumentationUsage taskId={task.id} usage={task.documentation_usage}/>}
      <section className="mb-5">
        <h3 className="mb-2 text-xs font-medium text-slate-300">Последний отправленный промпт</h3>
        {lastSent ? <>
          <p className="mb-2 text-[11px] text-slate-500">{stageName(lastSent.stage)} · попытка {lastSent.attempt}</p>
          <pre className="scrollbar max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-slate-800 bg-slate-950/50 p-3 text-[11px] leading-relaxed text-slate-300">{lastSent.sent_prompt}</pre>
        </> : <p className="text-[11px] text-slate-600">Отправленный промпт пока недоступен</p>}
      </section>
      <section className="mb-5">
        <h3 className="mb-2 text-xs font-medium text-slate-300">Артефакты</h3>
        {artifacts.length ? <ul className="space-y-2">
          {artifacts.map(({ name }) => <li key={name}>
            <a className="flex items-center gap-2 break-all text-xs text-indigo-300 hover:underline" href={artifactUrl(task.id, name)} target="_blank" rel="noopener noreferrer">
              <ExternalLink size={13} className="shrink-0" aria-hidden="true"/>{name}
            </a>
          </li>)}
        </ul> : <p className="text-[11px] text-slate-600">Артефактов пока нет</p>}
      </section>
      <section><h3 className="mb-3 text-xs font-medium text-slate-300">Чат Yougile</h3>{chat ? <div className="markdown text-xs leading-5 text-slate-400"><Markdown text={chatContent}/></div> : <p className="text-[11px] text-slate-600">Чат не импортирован</p>}</section>
    </>}
    {tab === 'stage' && <>
      <div className="mb-5"><div className="mb-1 text-[10px] uppercase tracking-widest text-indigo-400">Инспектор · этап {stage}</div><h2 className="text-base font-semibold">{stageName(stage)}</h2></div>
      <section className="mb-5 space-y-2 text-[11px]">
        <h3 className="mb-2 font-medium text-slate-300">Пути и ссылки</h3>
        <div className="flex gap-2 break-all text-slate-500"><FolderGit2 size={14} className="shrink-0"/><span>{task.branch_name}</span>{task.pr_url && <a className="text-indigo-400" href={task.pr_url} target="_blank"><ExternalLink size={13}/></a>}</div>
        {stage === 3 && task.worktree_path && <div className="break-all text-slate-500">Worktree: {task.worktree_path}</div>}
      </section>
      {task.status === 'waiting_answer' && stage === 1 && <section className="mb-5"><textarea rows={3} value={answer} onChange={e => setAnswer(e.target.value)} placeholder="Ответ на вопросы бота..."/><button onClick={submitAnswer} className="btn btn-primary mt-2 w-full"><Send size={14}/>Отправить ответ</button></section>}
      {stage === 1 && task.documentation_usage ? <DocumentationUsage taskId={task.id} usage={task.documentation_usage}/> : <section className="mb-5"><h3 className="mb-2 flex items-center gap-2 text-xs font-medium text-slate-300"><Flame size={14}/>Расход токенов</h3>{runs.length ? <div className="space-y-2">{runs.map(r => <div key={r.id} className="rounded-lg border border-slate-800 bg-slate-900/50 p-3 text-[10px]"><div className="flex justify-between"><span>Попытка {r.attempt} {r.is_fallback && '· Fallback'}</span><span className="text-orange-300">{r.usage_partial && '≥ '}{fmtTokens(spentTokens(r))}</span></div><div className="mt-1 text-slate-600">{r.cli_used} {r.model_used}</div><TokenUsage usage={r}/>{r.error && <div className="mt-2 text-rose-400">{r.error}</div>}</div>)}</div> : <p className="text-[11px] text-slate-600">Запусков еще не было</p>}</section>}
      <section className="mb-5"><h3 className="mb-2 flex items-center gap-2 text-xs font-medium text-slate-300"><TerminalSquare size={14}/>Live Logs</h3><pre ref={logRef} className="scrollbar h-40 overflow-auto whitespace-pre-wrap rounded-lg border border-slate-800 bg-[#050912] p-3 font-mono text-[10px] leading-relaxed text-emerald-300/70">{logs || 'Ожидание вывода...'}</pre></section>
      <section className="flex flex-wrap gap-2 border-t border-slate-800 pt-4">
        {stage <= 4 && <>
          {stageAvailable && <button disabled={busy || !!stageUnavailableReason} title={stageUnavailableReason} className="btn btn-primary basis-full" onClick={() => action('stage')}><Play size={14}/>Запустить только этот этап</button>}
          {canRestart && <button disabled={busy} className="btn flex-1 !px-2 text-[11px]" onClick={() => action('restart')}><RotateCcw size={13}/>Перезапустить</button>}
          {canRestart && stage === task.current_stage && stage !== 3 && <button disabled={busy} className="btn flex-1 !px-2 text-[11px]" onClick={() => action('approve')}><Check size={13}/>Апрувнуть</button>}
        </>}
        {stage === 5 && <button disabled={busy || task.status !== 'waiting_pr'} className="btn btn-primary w-full" onClick={() => action('merge')}><Check size={14}/>Подтвердить Merge вручную</button>}
      </section>
    </>}
  </div>;
}
