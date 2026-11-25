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

vi.mock('./api', () => ({
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
});
