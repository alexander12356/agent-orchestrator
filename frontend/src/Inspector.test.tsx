import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from './api';
import Inspector from './Inspector';
import { Task } from './types';

afterEach(() => cleanup());

vi.mock('mermaid', () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn(async () => ({ svg: '<svg class="flowchart"></svg>' })),
  },
}));

vi.mock('./api', async (importOriginal) => ({
  ...await importOriginal<typeof import('./api')>(),
  api: vi.fn(async (path: string) => (String(path).includes('artifacts') ? [{ name: '01_tech_doc_v1.md', path: '/tmp/01_tech_doc_v1.md', size: 20 }] : {})),
  fmtTokens: (value: number) => String(value),
  spentTokens: (run: { input_tokens?: number; output_tokens?: number; cache_tokens?: number }) => (run.input_tokens || 0) + (run.output_tokens || 0) + (run.cache_tokens || 0),
  stageName: () => 'Тех. документация',
}));

const task = {
  id: 7,
  title: 'Документ',
  description: 'Описание',
  current_stage: 1,
  status: 'running',
  branch_name: 'feat/task-7',
  merged_to_main: false,
  stage_agents: { 1: 1 },
  dependencies: [],
  total_tokens: 0,
  runs: [],
  created_at: '2026-11-24T00:00:00Z',
} as Task;

const props = { task, stage: 1, reload: () => undefined, notify: () => undefined, onTabChange: () => undefined };

describe('Inspector', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, text: async () => '# Документ\n\n```mermaid\nflowchart TD\nA-->B\n```' })));
    vi.stubGlobal('EventSource', class { onmessage: ((event: MessageEvent) => void) | null = null; close() {} });
  });

  it('does not show the artifact on the stage tab', () => {
    render(<Inspector {...props} tab="stage" requestedArtifact="01_tech_doc_v1.md" />);
    expect(screen.queryByRole('heading', { name: 'Артефакты' })).not.toBeInTheDocument();
    expect(screen.getByText('Live Logs')).toBeInTheDocument();
  });

  it('renders a mermaid diagram on the document tab', async () => {
    const { container } = render(<Inspector {...props} tab="document" />);
    await waitFor(() => expect(container.querySelector('svg.flowchart')).toBeTruthy());
  });

  it('explains why the task waits for a person and lets it continue', async () => {
    const held = { ...task, current_stage: 2, status: 'needs_human', hold_reason: 'Ревью отклонило документацию 4 раз подряд' };
    render(<Inspector {...props} task={held} stage={2} tab="description" />);
    expect(screen.getByText('Ревью отклонило документацию 4 раз подряд')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Продолжить' }));

    await waitFor(() => expect(api).toHaveBeenCalledWith('/tasks/7/start', expect.objectContaining({ method: 'POST' })));
  });

  it('offers nothing to continue for a running task', () => {
    render(<Inspector {...props} tab="description" />);
    expect(screen.queryByRole('button', { name: 'Продолжить' })).not.toBeInTheDocument();
  });

  it('offers the pipeline and the selected eligible stage as separate actions', async () => {
    const paused = { ...task, status: 'stage_ready', current_stage: 2, stage_agents: { 1: 1, 2: 1, 3: 1 } };
    const { rerender } = render(<Inspector {...props} task={paused} stage={3} tab="stage" />);
    expect(screen.getByRole('button', { name: 'Запустить только этот этап' })).toBeDisabled();

    rerender(<Inspector {...props} task={paused} stage={1} tab="stage" />);
    fireEvent.click(screen.getByRole('button', { name: 'Запустить только этот этап' }));
    await waitFor(() => expect(api).toHaveBeenCalledWith('/tasks/7/start?stage=1', expect.objectContaining({ method: 'POST' })));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Запустить пайплайн' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Запустить пайплайн' }));
    await waitFor(() => expect(api).toHaveBeenCalledWith('/tasks/7/start?pipeline=true', expect.objectContaining({ method: 'POST' })));
  });

  it('can restart a failed stage alongside the separate start actions', async () => {
    const failed = { ...task, status: 'error', current_stage: 2, stage_agents: { 1: 1, 2: 1 } };
    render(<Inspector {...props} task={failed} stage={2} tab="stage" />);

    expect(screen.getByRole('button', { name: 'Запустить только этот этап' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Запустить пайплайн' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Перезапустить' }));

    await waitFor(() => expect(api).toHaveBeenCalledWith('/tasks/7/restart?stage=2', expect.objectContaining({ method: 'POST' })));
  });
});
