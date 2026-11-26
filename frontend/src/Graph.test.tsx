import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { StageBadge } from './components';
import Graph from './Graph';
import { Task } from './types';

vi.mock('./Inspector', () => ({ default: () => null }));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
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

  rerender(view({ ...task, status: 'running' }));

  expect(container.querySelector('.lucide-circle-help')).not.toBeInTheDocument();
  expect(screen.getByText('В работе')).toBeInTheDocument();
});
