import { CircleHelp, X } from 'lucide-react';
import { ReactNode } from 'react';
import { Agent, Task } from './types';
import { stageName } from './api';

export function Avatar({ agent, size = 32 }: { agent?: Agent | null; size?: number }) {
  const value = agent?.avatar_url || '👤';
  return <span className="avatar" style={{ width: size, height: size }}>{value.startsWith('/') ? <img src={value} alt="" /> : value}</span>;
}

export function QuestionIcon() {
  return <CircleHelp size={15} className="shrink-0 text-amber-300" aria-hidden="true"/>;
}

export function StageBadge({ task }: { task: Pick<Task, 'current_stage' | 'status' | 'hold_reason'> }) {
  const color = task.status === 'error' ? 'red' : ['needs_human', 'waiting_answer', 'paused'].includes(task.status) ? 'orange' : task.status === 'open' ? 'gray' : [ 'gray', 'yellow', 'blue', 'yellow', 'blue', 'purple', 'green' ][task.current_stage];
  return <span className={`badge badge-${color}`} title={task.hold_reason || undefined}>{task.current_stage === 0 && '🔒'} {task.status === 'waiting_answer' && <QuestionIcon/>} {task.status === 'needs_human' && '✋'} {task.status === 'stage_ready' && 'Ожидает запуска · '}{task.status === 'paused' && 'На паузе · '}{stageName(task.current_stage, task.status)}</span>;
}

export function Modal({ title, children, onClose, wide = false }: { title: string; children: ReactNode; onClose: () => void; wide?: boolean }) {
  return <div className="modal-backdrop" onMouseDown={e => e.target === e.currentTarget && onClose()}>
    <section className={`modal card ${wide ? '!w-[850px]' : ''}`} role="dialog" aria-modal="true" aria-label={title}>
      <header className="mb-5 flex items-center justify-between"><h2 className="text-lg font-semibold text-slate-100">{title}</h2><button className="icon-btn" onClick={onClose} aria-label="Закрыть"><X size={19}/></button></header>
      {children}
    </section>
  </div>;
}

export function Empty({ icon, title, text }: { icon: ReactNode; title: string; text: string }) {
  return <div className="flex h-56 flex-col items-center justify-center text-center text-slate-500"><div className="mb-3 opacity-60">{icon}</div><b className="text-sm text-slate-300">{title}</b><p className="mt-1 max-w-xs text-xs">{text}</p></div>;
}
