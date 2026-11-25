import { Background, Controls, Edge, Handle, MarkerType, Node, NodeProps, Position, ReactFlow } from '@xyflow/react';
import { Bot, CheckCircle2, FileCode2, Flame, GitPullRequest, RotateCcw, UserRound } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { fmtTokens, spentTokens, stageName } from './api';
import { Avatar, Empty } from './components';
import ArtifactEdge from './ArtifactEdge';
import Inspector, { InspectorTab } from './Inspector';
import RevisionWindow from './RevisionWindow';
import { Agent, Task } from './types';

const titles = ['Тех. документация', 'Ревью документации', 'Реализация', 'Ревью реализации', 'PR · Человек'];
const envs = ['VDS', 'VDS', 'Local Unity Worker', 'VDS', 'GitHub'];

function StageNode({ data, selected }: NodeProps) {
  const d = data as unknown as { title: string; env: string; agent?: Agent; active: boolean; held: boolean; status: string; tokens: number; done: boolean; fallback: boolean; human: boolean };
  return <div className={`card w-52 p-4 transition ${selected ? '!border-indigo-300 !bg-indigo-500/10 shadow-lg shadow-indigo-950 ring-2 ring-indigo-400/40' : d.active || d.held ? '!border-amber-400/70' : d.done ? '!border-emerald-700/70' : ''}`}>
    <Handle type="target" position={Position.Left} className="!bg-slate-500"/><Handle type="source" position={Position.Right} className="!bg-slate-500"/>
    <Handle id="return-target" type="target" position={Position.Top} className="!bg-slate-500"/><Handle id="return-source" type="source" position={Position.Bottom} className="!bg-slate-500"/>
    <div className="mb-3 flex items-start justify-between"><span className="text-xs font-semibold text-slate-200">{d.title}</span>{d.active ? <span className="h-2 w-2 animate-pulse rounded-full bg-amber-400"/> : d.done ? <CheckCircle2 size={15} className="text-emerald-500"/> : <span className="h-2 w-2 rounded-full bg-slate-700"/>}</div>
    <div className="flex items-center gap-2">{d.human ? <UserRound size={26} className="text-purple-400"/> : <Avatar agent={d.agent}/>}<div className="min-w-0"><div className="truncate text-[11px] text-slate-400">{d.human ? 'Ожидание merge' : d.agent?.name || 'Не назначен'}</div><div className="mt-0.5 text-[9px] uppercase tracking-wide text-slate-600">{d.env}</div></div></div>
    <div className="mt-3 flex items-center justify-between border-t border-slate-800 pt-2 text-[10px]"><span className={d.active || d.held ? 'text-amber-300' : d.status === 'error' ? 'text-rose-400' : 'text-slate-500'}>{d.held ? 'Нужен человек' : d.fallback ? 'Fallback' : d.active ? 'В работе' : d.done ? 'Пройден' : d.status === 'waiting_answer' ? 'Ждёт ответа' : d.status === 'error' ? 'Ошибка' : 'Ожидает'}</span><span className="flex items-center gap-1 text-orange-300/70"><Flame size={11}/>{fmtTokens(d.tokens)}</span></div>
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
  const nodes: Node[] = useMemo(() => titles.map((title, index) => {
    const stage = index + 1;
    const stageRuns = runs.filter(r => r.stage === stage);
    return { id: String(stage), type: 'stage', selected: stage === selectedStage, position: { x: index * 340, y: index % 2 ? 150 : 90 }, data: { title, env: envs[index], agent: agents.find(a => a.id === task.stage_agents[stage]), active: task.current_stage === stage && ['running', 'waiting_answer'].includes(task.status), held: task.current_stage === stage && task.status === 'needs_human', done: task.current_stage > stage, status: task.current_stage === stage ? task.status : '', tokens: stageRuns.reduce((sum, r) => sum + spentTokens(r), 0), fallback: stageRuns.some(r => r.is_fallback), human: stage === 5 } };
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
      result.push({ id: `${i}-${i + 1}`, type: 'artifact', data: { onOpen: () => openArtifactEdge(`${i}-${i + 1}`, i, i + 1) }, selected: selectedEdge === `${i}-${i + 1}`, source: String(i), target: String(i + 1), label, animated: task.current_stage === i + 1 && task.status === 'running', style: { stroke: task.current_stage > i ? '#6574ee' : '#33415c', strokeWidth: 1.5 }, labelStyle: { fill: '#aebad1', fontSize: 10 }, labelBgStyle: { fill: '#121c30', fillOpacity: 1 }, labelBgPadding: [7, 5] as [number, number], markerEnd: { type: MarkerType.ArrowClosed, color: task.current_stage > i ? '#6574ee' : '#33415c' } });
    }
    const reviews2 = runs.filter(r => r.stage === 2 && r.status === 'completed');
    const reviews4 = runs.filter(r => r.stage === 4 && r.status === 'completed');
    if (task.current_stage === 1 && reviews2.length) result.push({ id: 'return-doc', type: 'artifact', data: { onOpen: () => openArtifactEdge('return-doc', 2, 1) }, selected: selectedEdge === 'return-doc', source: '2', target: '1', sourceHandle: 'return-source', targetHandle: 'return-target', label: `Правки по доке ×${reviews2.length}`, style: { stroke: '#e66f87' }, labelStyle: { fill: '#fda4af', fontSize: 10 }, labelBgStyle: { fill: '#271423' }, markerEnd: { type: MarkerType.ArrowClosed, color: '#e66f87' } });
    if (task.current_stage === 3 && reviews4.length) result.push({ id: 'return-code', type: 'artifact', data: { onOpen: () => openArtifactEdge('return-code', 4, 3) }, selected: selectedEdge === 'return-code', source: '4', target: '3', sourceHandle: 'return-source', targetHandle: 'return-target', label: `Правки по коду ×${reviews4.length}`, style: { stroke: '#e66f87' }, labelStyle: { fill: '#fda4af', fontSize: 10 }, labelBgStyle: { fill: '#271423' }, markerEnd: { type: MarkerType.ArrowClosed, color: '#e66f87' } });
    return result;
  }, [task, runs, selectedEdge, openArtifactEdge]);
  return <main className="flex h-[calc(100vh-65px)] flex-col">
    <header className="flex items-center justify-between border-b border-slate-800 bg-slate-950/40 px-5 py-3"><div className="flex items-center gap-3"><GitPullRequest size={18} className="text-indigo-400"/><select className="!w-auto min-w-64 !border-0 !bg-transparent !py-1 text-sm font-medium" value={task.id} onChange={e => selectTask(Number(e.target.value))}>{tasks.map(t => <option key={t.id} value={t.id}>#{t.id} {t.title}</option>)}</select></div><span className="badge badge-orange"><Flame size={13}/>{fmtTokens(task.total_tokens)}{task.token_budget_effective ? ` из ${fmtTokens(task.token_budget_effective)}` : ''} токенов</span></header>
    <div className="grid min-h-0 flex-1 lg:grid-cols-[minmax(0,7fr)_minmax(330px,3fr)]">
      <div className="relative min-h-[400px] border-r border-slate-800"><ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes} fitView minZoom={.4} maxZoom={1.5} nodesDraggable={false} nodesConnectable={false} onNodeClick={(_, node) => { setSelectedStage(Number(node.id)); setSelectedEdge(''); setInspectorTab('stage'); }} onEdgeClick={(_, edge) => openArtifactEdge(edge.id, Number(edge.source), Number(edge.target))} colorMode="dark"><Background color="#1c2940" gap={24}/><Controls/></ReactFlow></div>
      <aside className="scrollbar min-h-0 overflow-y-auto bg-[#0d1524]"><Inspector task={task} stage={selectedStage} tab={inspectorTab} onTabChange={setInspectorTab} reload={reload} notify={notify}/></aside>
    </div>
    {revisionEdge && <RevisionWindow task={task} source={revisionEdge.source} target={revisionEdge.target} onClose={() => setRevisionEdge(null)}/>}
  </main>;
}
