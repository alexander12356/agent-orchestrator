import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { StageBadge } from './components';
import Graph from './Graph';
import { StageRun, Task } from './types';

vi.mock('./Inspector', () => ({ default: () => null }));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it('updates the attempt count for each automated stage from its runs', () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
  const task: Task = {
    id: 1, title: 'Задача', status: 'running', current_stage: 2,
    description: '', branch_name: 'feat/task-1', merged_to_main: false,
    stage_agents: {}, dependencies: [], total_tokens: 0, created_at: '2026-11-26T00:00:00Z',
  };
  const run: StageRun = {
    id: 1, task_id: task.id, stage: 1, attempt: 1, status: 'completed', started_at: task.created_at,
    is_fallback: false, input_tokens: 0, output_tokens: 0, cache_tokens: 0, tokens_estimated: false,
  };
  const props = { tasks: [task], agents: [], reload: vi.fn(), selectTask: vi.fn(), notify: vi.fn() };
  const { rerender } = render(<Graph {...props} task={{ ...task, runs: [
    run, { ...run, id: 2, stage: 2, status: 'error' },
  ] }}/>);
  const stage = (title: string) => within(screen.getByText(title).closest('.react-flow__node') as HTMLElement);

  expect(stage('Тех. документация').getByText('Попыток: 1')).toBeInTheDocument();
  expect(stage('Ревью документации').getByText('Попыток: 1')).toBeInTheDocument();
  expect(stage('Реализация').getByText('Попыток: 0')).toBeInTheDocument();
  expect(stage('PR · Человек').queryByText(/Попыток/)).toBeNull();

  rerender(<Graph {...props} task={{ ...task, runs: [
    run, { ...run, id: 2, stage: 2, status: 'error' },
    { ...run, id: 3, stage: 2, attempt: 2, status: 'running' },
  ] }}/>);

  expect(stage('Ревью документации').getByText('Попыток: 2')).toBeInTheDocument();
});

it('shows the all-attempt total and token categories on each stage node', () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
  const task: Task = {
    id: 1, title: 'Задача', status: 'running', current_stage: 2,
    description: '', branch_name: 'feat/task-1', merged_to_main: false,
    stage_agents: {}, dependencies: [], total_tokens: 0, created_at: '2026-11-26T00:00:00Z',
  };
  const run: StageRun = {
    id: 1, task_id: task.id, stage: 1, attempt: 1, status: 'completed', started_at: task.created_at,
    is_fallback: false, input_tokens: 100, output_tokens: 10, cache_tokens: 40, tokens_estimated: false,
  };
  render(<Graph task={{ ...task, runs: [run, { ...run, id: 2, stage: 2, input_tokens: 999 }] }} tasks={[task]} agents={[]} reload={vi.fn()} selectTask={vi.fn()} notify={vi.fn()}/>);

  const stage = within(screen.getByText('Тех. документация').closest('.react-flow__node') as HTMLElement);
  expect(stage.getByText('150')).toBeInTheDocument();
  expect(stage.getByText('Вход 100')).toBeInTheDocument();
  expect(stage.getByText('Кэш 40')).toBeInTheDocument();
  expect(stage.getByText('Выход 10')).toBeInTheDocument();
});

it('shows a question on the task and only on the stage awaiting an answer', () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
  const task: Task = {
    id: 1, title: 'Задача', status: 'waiting_answer', current_stage: 1,
    description: '', branch_name: 'feat/task-1', merged_to_main: false,
    stage_agents: {}, dependencies: [], total_tokens: 0, created_at: '2026-11-26T00:00:00Z',
  };
  const props = { tasks: [task], agents: [], reload: vi.fn(), selectTask: vi.fn(), notify: vi.fn() };
  const view = (currentTask: Task) => <>
    <StageBadge task={currentTask}/>
    <Graph {...props} task={currentTask}/>
  </>;
  const { container, rerender } = render(view(task));
  const badge = screen.getByText(/Ожидает ответа по ТЗ/);
  const stage = screen.getByText('Ждёт ответа').closest('.react-flow__node');

  expect(badge.querySelector('.lucide-circle-help')).toBeInTheDocument();
  expect(stage).toHaveAttribute('data-id', '1');
  expect(stage?.querySelector('.lucide-circle-help')).toBeInTheDocument();
  expect(container.querySelectorAll('.lucide-circle-help')).toHaveLength(2);
  expect(container.querySelector('.lucide-loader-circle')).not.toBeInTheDocument();

  rerender(view({ ...task, status: 'running', current_stage: 2 }));

  expect(container.querySelector('.lucide-circle-help')).not.toBeInTheDocument();
  const runningStage = screen.getByText('В работе').closest('.react-flow__node');
  expect(runningStage).toHaveAttribute('data-id', '2');
  expect(runningStage?.querySelector('.lucide-loader-circle')).toHaveClass('motion-safe:animate-spin');
  expect(container.querySelectorAll('.lucide-loader-circle')).toHaveLength(1);
  expect(screen.getByText('Пройден').closest('.react-flow__node')).toHaveAttribute('data-id', '1');

  rerender(view({ ...task, status: 'paused', current_stage: 2 }));

  expect(screen.getByText('На паузе').closest('.react-flow__node')?.querySelector('.lucide-circle-pause')).toBeInTheDocument();
  expect(container.querySelector('.lucide-loader-circle')).not.toBeInTheDocument();

  rerender(view({ ...task, status: 'waiting_pr', current_stage: 5 }));

  expect(screen.getByText('Ожидание merge').closest('.react-flow__node')).toHaveAttribute('data-id', '5');
  expect(container.querySelector('.lucide-loader-circle')).not.toBeInTheDocument();
});
