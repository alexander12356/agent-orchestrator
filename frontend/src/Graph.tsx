import TokenUsage from './TokenUsage';
import { Background, Controls, Edge, Handle, MarkerType, Node, NodeProps, Position, ReactFlow } from '@xyflow/react';
import { CheckCircle2, Circle, CircleAlert, CirclePause, Clock, Flame, GitPullRequest, LoaderCircle, Lock, UserRound } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { fmtTokens, spentTokens } from './api';
import { Avatar, QuestionIcon } from './components';
import ArtifactEdge from './ArtifactEdge';
import Inspector, { InspectorTab } from './Inspector';
import RevisionWindow from './RevisionWindow';
import { Agent, Task } from './types';

const titles = ['Тех. документация', 'Ревью документации', 'Реализация', 'Ревью реализации', 'PR · Человек'];
const envs = ['VDS', 'VDS', 'Local Unity Worker', 'VDS', 'GitHub'];

const waitingToStart = { icon: Clock, label: 'Ожидает запуска', color: 'text-slate-400' };
const upcomingStatus = { icon: Circle, label: 'Ожидает', color: 'text-slate-500' };
const stageStatuses: Record<string, { icon: typeof Circle | typeof QuestionIcon; label: string; color: string } | undefined> = {
  running: { icon: LoaderCircle, label: 'В работе', color: 'text-amber-300' },
  queued: { icon: Clock, label: 'В очереди', color: 'text-slate-400' },
  open: waitingToStart,
  stage_ready: waitingToStart,
  waiting_worker: { icon: Clock, label: 'Ждёт воркера', color: 'text-slate-400' },
  waiting_answer: { icon: QuestionIcon, label: 'Ждёт ответа', color: 'text-amber-300' },
  needs_human: { icon: UserRound, label: 'Нужен человек', color: 'text-amber-300' },
  waiting_pr: { icon: UserRound, label: 'Ожидание merge', color: 'text-amber-300' },
  paused: { icon: CirclePause, label: 'На паузе', color: 'text-amber-300' },
  error: { icon: CircleAlert, label: 'Ошибка', color: 'text-rose-400' },
  done: { icon: CheckCircle2, label: 'Пройден', color: 'text-emerald-500' },
  blocked_main: { icon: Lock, label: 'Ждёт зависимость', color: 'text-slate-400' },
};

type StageGraphNode = Node<{
  title: string;
  env: string;
  agent?: Agent;
  model?: string;
  status: string;
  attempts: number;
  tokens: number;
  fallback: boolean;
  human: boolean;
}, 'stage'>;

function StageNode({ data: d, selected }: NodeProps<StageGraphNode>) {
  const { icon: StatusIcon, label, color } = stageStatuses[d.status] ?? upcomingStatus;
  const running = d.status === 'running';
  const attention = ['running', 'waiting_answer', 'needs_human'].includes(d.status);
  return <div className={`card w-52 p-4 transition ${selected ? '!border-indigo-300 !bg-indigo-500/10 shadow-lg shadow-indigo-950 ring-2 ring-indigo-400/40' : attention ? '!border-amber-400/70' : d.status === 'done' ? '!border-emerald-700/70' : ''}`}>
    <Handle id="forward-target" type="target" position={Position.Top} className="!bg-slate-500"/>
    <Handle id="forward-source" type="source" position={Position.Right} className="!bg-slate-500"/>
    <Handle id="return-target" type="target" position={Position.Bottom} className="!bg-slate-500"/>
    <Handle id="return-source" type="source" position={Position.Left} className="!bg-slate-500"/>
    <div className="mb-3 flex items-start justify-between gap-2">
      <span className="text-xs font-semibold text-slate-200">{d.title}</span>
      <StatusIcon size={15} className={`shrink-0 ${color}${running ? ' motion-safe:animate-spin' : ''}`} aria-hidden="true"/>
    </div>
    <div className="flex items-center gap-2">{d.human ? <UserRound size={26} className="text-purple-400"/> : <Avatar agent={d.agent}/>}<div className="min-w-0"><div className="truncate text-[11px] text-slate-400">{d.human ? 'Ручной этап' : d.agent?.name || 'Не назначен'}</div><div className="mt-0.5 text-[9px] uppercase tracking-wide text-slate-600">{d.env}</div></div></div>
    {d.model && <div className="mt-2 truncate text-[10px] text-indigo-300" title={`Модель: ${d.model}`}>Модель: {d.model}</div>}
    <div className="mt-3 flex items-center justify-between gap-2 border-t border-slate-800 pt-2 text-[10px]">
      <div>
        <div className={color}>{label}{d.fallback && ' · Fallback'}</div>
        {!d.human && <div className="mt-1 text-slate-400">Попыток: {d.attempts}</div>}
      </div>
      <span className="flex shrink-0 items-center gap-1 text-orange-300/70"><Flame size={11}/>{fmtTokens(d.tokens)}</span>
    </div>
  </div>;
}

const nodeTypes = { stage: StageNode };
const edgeTypes = { artifact: ArtifactEdge };

export default function Graph({ task, tasks, agents, reload, selectTask, notify }: { task: Task; tasks: Task[]; agents: Agent[]; reload: () => void; selectTask: (id: number) => void; notify: (message: string) => void }) {
  const [selectedStage, setSelectedStage] = useState(Math.min(Math.max(task.current_stage, 1), 5));
  const [selectedEdge, setSelectedEdge] = useState('');
  const [revisionEdge, setRevisionEdge] = useState<{ source: number; target: number } | null>(null);
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>('description');
  useEffect(() => {
    setSelectedStage(Math.min(Math.max(task.current_stage, 1), 5));
    setSelectedEdge('');
    setRevisionEdge(null);
    setInspectorTab('description');
  }, [task.id]);
  const runs = task.runs || [];
  const nodes: StageGraphNode[] = useMemo(() => titles.map((title, index) => {
    const stage = index + 1;
    const stageRuns = runs.filter(r => r.stage === stage);
    const latestRun = stageRuns.at(-1);
    const agent = agents.find(a => a.id === task.stage_agents[stage]);
    const config = latestRun?.cli_used ? { cli: latestRun.cli_used, model: latestRun.model_used } : agent?.primary_config;
    const model = stage < 5 && config
      ? config.model?.trim() || (config.cli === 'custom' ? 'не указана' : `по умолчанию (${config.cli})`)
      : undefined;
    const currentStage = task.status === 'blocked_main' && task.current_stage === 0 ? 1 : task.current_stage;
    const status = stage === currentStage ? task.status : stage < currentStage ? 'done' : '';
    return { id: String(stage), type: 'stage', selected: stage === selectedStage, position: { x: index * 244, y: index * 204 }, data: { title, env: envs[index], agent, model, status, attempts: stageRuns.length, tokens: stageRuns.reduce((sum, r) => sum + spentTokens(r), 0), fallback: latestRun?.is_fallback ?? false, human: stage === 5 } };
  }), [task, runs, agents, selectedStage]);
  const latest = (stage: number) => runs.filter(r => r.stage === stage && r.status === 'completed').at(-1);
  const openArtifactEdge = useCallback((id: string, source: number, target: number) => {
    setSelectedStage(source);
    setSelectedEdge(id);
    setRevisionEdge({ source, target });
  }, []);
  const edges: Edge[] = useMemo(() => {
    const result: Edge[] = [];
    for (let i = 1; i <= 4; i++) {
      const docRun = latest(i);
      let label = ['Тех. документация', 'Дока утверждена', 'Git Diff + Отчёт', `Ветка ${task.branch_name}`][i - 1];
      if (docRun) label += ` • ${fmtTokens(spentTokens(docRun))} tok`;
      const color = task.current_stage > i ? '#6574ee' : '#52617d';
      result.push({ id: `${i}-${i + 1}`, type: 'artifact', data: { onOpen: () => openArtifactEdge(`${i}-${i + 1}`, i, i + 1) }, selected: selectedEdge === `${i}-${i + 1}`, source: String(i), target: String(i + 1), sourceHandle: 'forward-source', targetHandle: 'forward-target', label, animated: task.current_stage === i + 1 && task.status === 'running', style: { stroke: color }, markerEnd: { type: MarkerType.ArrowClosed, color } });
    }
    const reviews2 = runs.filter(r => r.stage === 2 && r.status === 'completed');
    const reviews4 = runs.filter(r => r.stage === 4 && r.status === 'completed');
    if (task.current_stage === 1 && reviews2.length) result.push({ id: 'return-doc', type: 'artifact', data: { onOpen: () => openArtifactEdge('return-doc', 2, 1) }, selected: selectedEdge === 'return-doc', source: '2', target: '1', sourceHandle: 'return-source', targetHandle: 'return-target', label: `Правки по доке ×${reviews2.length}`, style: { stroke: '#e66f87' }, markerEnd: { type: MarkerType.ArrowClosed, color: '#e66f87' } });
    if (task.current_stage === 3 && reviews4.length) result.push({ id: 'return-code', type: 'artifact', data: { onOpen: () => openArtifactEdge('return-code', 4, 3) }, selected: selectedEdge === 'return-code', source: '4', target: '3', sourceHandle: 'return-source', targetHandle: 'return-target', label: `Правки по коду ×${reviews4.length}`, style: { stroke: '#e66f87' }, markerEnd: { type: MarkerType.ArrowClosed, color: '#e66f87' } });
    return result;
  }, [task, runs, selectedEdge, openArtifactEdge]);
  return <main className="flex h-[calc(100vh-65px)] flex-col">
    <header className="flex items-center justify-between border-b border-slate-800 bg-slate-950/40 px-5 py-3"><div className="flex items-center gap-3"><GitPullRequest size={18} className="text-indigo-400"/><select className="!w-auto min-w-64 !border-0 !bg-transparent !py-1 text-sm font-medium" value={task.id} onChange={e => selectTask(Number(e.target.value))}>{tasks.map(t => <option key={t.id} value={t.id}>#{t.id} {t.title}</option>)}</select></div><div className="flex flex-wrap items-center justify-end gap-2"><TokenUsage usage={task}/>{!!task.token_budget_effective && <span className="badge badge-orange">Бюджет {fmtTokens(task.token_budget_effective)}</span>}</div></header>
    <div className="grid min-h-0 flex-1 lg:grid-cols-[minmax(0,7fr)_minmax(330px,3fr)]">
      <div className="relative min-h-[400px] border-r border-slate-800"><ReactFlow key={task.id} nodes={nodes} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes} fitView minZoom={.25} maxZoom={1.5} nodesDraggable={false} nodesConnectable={false} onNodeClick={(_, node) => { setSelectedStage(Number(node.id)); setSelectedEdge(''); setInspectorTab('stage'); }} onEdgeClick={(_, edge) => openArtifactEdge(edge.id, Number(edge.source), Number(edge.target))} colorMode="dark"><Background color="#1c2940" gap={24}/><Controls/></ReactFlow></div>
      <aside className="scrollbar min-h-0 overflow-y-auto bg-[#0d1524]"><Inspector key={task.id} task={task} stage={selectedStage} tab={inspectorTab} onTabChange={setInspectorTab} reload={reload} notify={notify}/></aside>
    </div>
    {revisionEdge && <RevisionWindow task={task} source={revisionEdge.source} target={revisionEdge.target} onClose={() => setRevisionEdge(null)}/>}
  </main>;
}
