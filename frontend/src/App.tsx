import { Bot, CheckCircle2, LayoutList, Settings2, Share2, Wifi, WifiOff } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { api } from './api';
import Graph from './Graph';
import Settings from './Settings';
import Tasks from './Tasks';
import { Agent, Task } from './types';

export default function App() {
  const [tab, setTab] = useState<'tasks' | 'graph' | 'settings'>('tasks');
  const [tasks, setTasks] = useState<Task[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [selected, setSelected] = useState<Task | null>(null);
  const [online, setOnline] = useState(false);
  const [toast, setToast] = useState('');
  const notify = useCallback((message: string) => { setToast(message); window.setTimeout(() => setToast(''), 3500); }, []);
  const reload = useCallback(async () => {
    try {
      const [nextTasks, nextAgents, health] = await Promise.all([api<Task[]>('/tasks'), api<Agent[]>('/agents'), api<{worker_online: boolean}>('/health')]);
      setTasks(nextTasks); setAgents(nextAgents); setOnline(health.worker_online);
      if (selectedId) {
        const detail = await api<Task>(`/tasks/${selectedId}`); setSelected(detail);
      }
    } catch (e) { console.error(e); }
  }, [selectedId]);
  useEffect(() => { reload(); const timer = window.setInterval(reload, 2500); return () => window.clearInterval(timer); }, [reload]);
  const openTask = (id: number) => { setSelectedId(id); setTab('graph'); };
  return <div className="min-h-screen">
    <nav className="sticky top-0 z-40 flex h-[65px] items-center justify-between border-b border-slate-800 bg-[#0b1120e8] px-6 backdrop-blur-xl">
      <div className="flex items-center gap-7"><div className="flex items-center gap-2 font-semibold tracking-tight"><span className="flex h-8 w-8 items-center justify-center rounded-lg bg-indigo-500 shadow-lg shadow-indigo-950"><Bot size={19}/></span><span>Agent<span className="text-indigo-400">Flow</span></span></div><div className="flex gap-1">{[['tasks', 'Задачи', LayoutList], ['graph', 'Граф задачи', Share2], ['settings', 'Настройки', Settings2]].map(([id, title, Icon]) => <button key={id as string} disabled={id === 'graph' && !selected} onClick={() => setTab(id as typeof tab)} className={`flex items-center gap-2 rounded-lg px-3 py-2 text-xs transition ${tab === id ? 'bg-indigo-500/15 text-indigo-300' : 'text-slate-500 hover:bg-slate-800 hover:text-slate-300'}`}><Icon size={15}/>{title as string}</button>)}</div></div>
      <div className={`flex items-center gap-2 text-[11px] ${online ? 'text-emerald-400' : 'text-slate-500'}`}><span className={`h-1.5 w-1.5 rounded-full ${online ? 'bg-emerald-400' : 'bg-slate-600'}`}/>{online ? <Wifi size={14}/> : <WifiOff size={14}/>}Unity Worker · {online ? 'Online' : 'Offline'}</div>
    </nav>
    {tab === 'tasks' && <Tasks tasks={tasks} agents={agents} reload={reload} openTask={openTask} notify={notify}/>} 
    {tab === 'settings' && <Settings agents={agents} reload={reload} notify={notify}/>} 
    {tab === 'graph' && (selected ? <Graph task={selected} tasks={tasks} agents={agents} reload={reload} selectTask={setSelectedId} notify={notify}/> : <div className="p-10 text-center text-sm text-slate-500">Выберите задачу на вкладке «Задачи»</div>)}
    {toast && <div className="fixed bottom-6 right-6 z-[100] flex items-center gap-2 rounded-lg border border-emerald-800 bg-[#12251f] px-4 py-3 text-xs text-emerald-200 shadow-2xl"><CheckCircle2 size={16}/>{toast}</div>}
  </div>;
}
