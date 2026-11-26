import { ChevronRight } from 'lucide-react';
import { useState } from 'react';
import { Task } from './types';

type Branch = Map<string, Branch>;

const emptyLabels = ['Без проекта', 'Без майлстона', 'Без спринта'];
const collator = new Intl.Collator('ru', { numeric: true });

export function taskPath(task: Task) {
  return [task.project, task.milestone, task.sprint].map(value => value?.trim() || '');
}

export function buildTaskHierarchy(tasks: Task[]): Branch {
  const root: Branch = new Map();
  for (const task of tasks) {
    let branch = root;
    for (const value of taskPath(task)) {
      const children = branch.get(value) ?? new Map();
      branch.set(value, children);
      branch = children;
    }
  }
  return root;
}

export function availableTaskPath(root: Branch, path: string[]) {
  const available: string[] = [];
  let branch = root;
  for (const value of path) {
    const children = branch.get(value);
    if (!children) break;
    available.push(value);
    branch = children;
  }
  return available;
}

export function taskPathLabel(path: string[]) {
  return path.map((value, index) => value || emptyLabels[index]).join(' → ') || 'Все проекты';
}

export default function TaskHierarchy({ root, selected, onSelect }: {
  root: Branch;
  selected: string[];
  onSelect: (path: string[]) => void;
}) {
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const selectedKey = JSON.stringify(selected);
  const labelClass = (active: boolean) => `min-w-0 flex-1 rounded px-2 py-1.5 text-left text-xs [overflow-wrap:anywhere] ${active ? 'bg-indigo-500/15 text-indigo-300' : 'text-slate-300 hover:bg-slate-800'}`;

  function toggle(key: string) {
    setExpanded(previous => {
      const next = new Set(previous);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function renderBranch(branch: Branch, ancestors: string[]) {
    if (!branch.size) return null;
    const entries = [...branch.entries()].sort(([a], [b]) => {
      if (!a) return 1;
      if (!b) return -1;
      return collator.compare(a, b);
    });
    return <ul className={ancestors.length ? 'ml-3 border-l border-slate-800 pl-1' : 'mt-1'}>
      {entries.map(([value, children]) => {
        const path = [...ancestors, value];
        const key = JSON.stringify(path);
        const label = value || emptyLabels[ancestors.length];
        const open = expanded.has(key);
        const active = key === selectedKey;
        return <li key={key}>
          <div className="flex items-start gap-1">
            {children.size > 0
              ? <button type="button" className="icon-btn shrink-0" aria-label={`${open ? 'Свернуть' : 'Раскрыть'} ${label}`} aria-expanded={open} onClick={() => toggle(key)}>
                <ChevronRight size={14} aria-hidden="true" className={open ? 'rotate-90' : ''}/>
              </button>
              : <span className="w-[26px] shrink-0"/>}
            <button type="button" aria-pressed={active} className={labelClass(active)} onClick={() => {
              onSelect(path);
              if (children.size && !open) toggle(key);
            }}>{label}</button>
          </div>
          {open && renderBranch(children, path)}
        </li>;
      })}
    </ul>;
  }

  return <nav className="card p-3" aria-label="Проекты, майлстоны и спринты">
    <h2 className="mb-2 px-2 text-sm font-medium text-slate-200">Проекты</h2>
    <button type="button" aria-pressed={!selected.length} className={`w-full ${labelClass(!selected.length)}`} onClick={() => onSelect([])}>Все проекты</button>
    {renderBranch(root, [])}
  </nav>;
}
