import { Check, ChevronRight, ListTodo, LockKeyhole, Plus, Search, Square, UserRound } from 'lucide-react';
import { FormEvent, useMemo, useState } from 'react';
import { api } from './api';
import { Avatar, Empty, Modal, StageBadge } from './components';
import { Agent, Task } from './types';

const filters = [['all', 'Все'], ['active', 'В работе'], ['waiting', 'Ожидают'], ['pr', 'Готово к PR'], ['done', 'Завершены']];

export default function Tasks({ tasks, agents, reload, openTask, notify }: { tasks: Task[]; agents: Agent[]; reload: () => void; openTask: (id: number) => void; notify: (message: string) => void }) {
  const [filter, setFilter] = useState('all');
  const [search, setSearch] = useState('');
  const [editing, setEditing] = useState<Task | 'new' | null>(null);
  const visible = useMemo(() => tasks.filter(task => {
    const byFilter = filter === 'all' || (filter === 'active' && ['running', 'queued', 'waiting_worker'].includes(task.status)) || (filter === 'waiting' && ['blocked_main', 'waiting_answer', 'error'].includes(task.status)) || (filter === 'pr' && task.current_stage === 5) || (filter === 'done' && task.current_stage === 6);
    return byFilter && `${task.id} ${task.title}`.toLowerCase().includes(search.toLowerCase());
  }), [tasks, filter, search]);
  return <main className="mx-auto max-w-[1500px] p-7">
    <div className="mb-6 flex flex-wrap items-center justify-between gap-4"><div><h1 className="text-2xl font-semibold tracking-tight">Задачи</h1><p className="mt-1 text-sm text-slate-500">Управление полным циклом разработки</p></div><button className="btn btn-primary" onClick={() => setEditing('new')}><Plus size={16}/> Новая задача</button></div>
    <section className="card overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 p-4">
        <div className="flex flex-wrap gap-1">{filters.map(([id, title]) => <button key={id} onClick={() => setFilter(id)} className={`rounded-lg px-3 py-2 text-xs transition ${filter === id ? 'bg-indigo-500/15 text-indigo-300' : 'text-slate-500 hover:bg-slate-800'}`}>{title}</button>)}</div>
        <div className="relative w-full sm:w-64"><Search size={15} className="absolute left-3 top-3 text-slate-600"/><input className="!py-2 !pl-9" placeholder="Поиск задач..." value={search} onChange={e => setSearch(e.target.value)}/></div>
      </div>
      <div className="overflow-x-auto"><table className="w-full min-w-[900px] border-collapse text-left"><thead><tr className="border-b border-slate-800 text-[11px] uppercase tracking-wider text-slate-500"><th className="px-5 py-3 font-medium">ID</th><th className="px-3 py-3 font-medium">Название таска</th><th className="px-3 py-3 font-medium">Этап</th><th className="px-3 py-3 font-medium">Исполнитель</th><th className="px-3 py-3 font-medium">Зависимости</th><th/></tr></thead>
      <tbody>{visible.map(task => <tr key={task.id} className="group border-b border-slate-800/60 transition hover:bg-slate-800/25">
        <td className="px-5 py-4 font-mono text-xs text-slate-500">#{task.id}</td>
        <td className="px-3 py-4"><button onClick={() => openTask(task.id)} className="text-left text-sm font-medium text-slate-200 hover:text-indigo-300">{task.title}</button>{task.external_issue_id && <div className="mt-1 text-[10px] text-slate-600">{task.external_issue_id}</div>}</td>
        <td className="px-3 py-4"><StageBadge task={task}/></td>
        <td className="px-3 py-4">{task.current_stage === 0 ? <span className="text-slate-600">—</span> : task.current_stage === 5 ? <span className="flex items-center gap-2 text-xs text-slate-400"><UserRound size={18}/> Человек</span> : <span className="flex items-center gap-2 text-xs"><Avatar agent={task.current_agent}/><span>{task.current_agent?.name || '—'}</span></span>}</td>
        <td className="max-w-xs px-3 py-4">{task.dependencies.length ? <div className="flex flex-col gap-1">{task.dependencies.map(dep => <span key={dep.id} className={`flex items-center gap-1.5 text-[11px] ${dep.merged_to_main ? 'text-slate-500' : 'text-amber-300/70'}`}>{dep.merged_to_main ? <Check size={12}/> : <LockKeyhole size={11}/>} #{dep.id} {dep.title}</span>)}</div> : <span className="text-slate-700">—</span>}</td>
        <td className="px-3"><button className="icon-btn opacity-0 group-hover:opacity-100" title="Редактировать" onClick={() => setEditing(task)}><ChevronRight size={17}/></button></td>
      </tr>)}</tbody></table></div>
      {!visible.length && <Empty icon={<ListTodo size={40}/>} title="Задач не найдено" text="Измените фильтр или создайте новую задачу"/>}
    </section>
    {editing && <TaskModal task={editing === 'new' ? null : editing} tasks={tasks} agents={agents} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); reload(); notify(editing === 'new' ? 'Задача создана' : 'Задача обновлена'); }}/>} 
  </main>;
}

function TaskModal({ task, tasks, agents, onClose, onSaved }: { task: Task | null; tasks: Task[]; agents: Agent[]; onClose: () => void; onSaved: () => void }) {
  const [title, setTitle] = useState(task?.title || '');
  const [description, setDescription] = useState(task?.description || '');
  const [external, setExternal] = useState(task?.external_issue_id || '');
  const [deps, setDeps] = useState<number[]>(task?.dependencies.map(d => d.id) || []);
  const [assigned, setAssigned] = useState<Record<number, number>>(task?.stage_agents || {});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setBusy(true); setError('');
    try {
      const body = JSON.stringify({ title, description, external_issue_id: external || null, dependencies: deps, stage_agents: assigned });
      await api(task ? `/tasks/${task.id}` : '/tasks', { method: task ? 'PUT' : 'POST', body }); onSaved();
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  return <Modal title={task ? `Редактировать задачу #${task.id}` : 'Новая задача'} onClose={onClose} wide><form onSubmit={submit} className="grid gap-5">
    <div className="grid gap-4 sm:grid-cols-2"><label>Название<input required autoFocus value={title} onChange={e => setTitle(e.target.value)} placeholder="Что нужно реализовать?"/></label><label>ID внешнего трекера<input value={external} onChange={e => setExternal(e.target.value)} placeholder="PROJ-452 или 123"/></label></div>
    <label>Описание (ТЗ)<textarea required rows={6} value={description} onChange={e => setDescription(e.target.value)} placeholder="Цель, требования и критерии приемки..."/></label>
    <div><div className="mb-2 text-xs text-slate-400">Зависимости</div><div className="grid max-h-36 gap-1 overflow-y-auto rounded-lg border border-slate-800 p-2">{tasks.filter(t => t.id !== task?.id).map(t => <label key={t.id} className="flex cursor-pointer grid-cols-none items-center gap-2 rounded p-2 text-xs hover:bg-slate-800"><input type="checkbox" className="!w-4 accent-indigo-500" checked={deps.includes(t.id)} onChange={() => setDeps(v => v.includes(t.id) ? v.filter(id => id !== t.id) : [...v, t.id])}/>#{t.id} {t.title}</label>)}{!tasks.filter(t => t.id !== task?.id).length && <span className="p-2 text-xs text-slate-600">Других задач пока нет</span>}</div></div>
    <div><div className="mb-2 text-xs text-slate-400">Назначение ботов</div><div className="grid gap-3 sm:grid-cols-2">{[1,2,3,4].map(stage => <label key={stage}>Этап {stage}<select value={assigned[stage] || ''} onChange={e => setAssigned({...assigned, [stage]: Number(e.target.value)})}><option value="">По умолчанию</option>{agents.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}</select></label>)}</div></div>
    {error && <div className="rounded-lg border border-rose-900 bg-rose-950/30 p-3 text-xs text-rose-300">{error}</div>}
    <footer className="flex justify-end gap-2"><button type="button" className="btn" onClick={onClose}>Отмена</button><button className="btn btn-primary" disabled={busy}>{busy ? 'Сохранение...' : 'Сохранить задачу'}</button></footer>
  </form></Modal>;
}
