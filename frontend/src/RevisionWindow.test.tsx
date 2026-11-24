import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import RevisionWindow from './RevisionWindow';
import { Task } from './types';

vi.mock('mermaid', () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn(async () => ({ svg: '<svg class="flowchart"></svg>' })),
  },
}));

vi.mock('./api', () => ({
  api: vi.fn(async () => [
    { name: '01_tech_doc_v1.md', path: '/v1', size: 1 },
    { name: '01_tech_doc_v2.md', path: '/v2', size: 2 },
    { name: '02_doc_review_v1.json', path: '/r1', size: 1 },
  ]),
  stageName: (stage: number) => ['Ожидает влития в main', 'Тех. документация', 'Ревью документации', 'Реализация', 'Ревью реализации', 'PR'][stage],
}));

const task = {
  id: 7,
  title: 'Документ',
  description: 'Описание',
  current_stage: 2,
  status: 'running',
  branch_name: 'feat/task-7',
  merged_to_main: false,
  stage_agents: {},
  dependencies: [],
  total_tokens: 0,
  runs: [],
  created_at: '2026-11-24T00:00:00Z',
} as Task;

describe('RevisionWindow', () => {
  afterEach(cleanup);

  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => ({
      ok: true,
      text: async () => String(url).includes('v2') ? '# Документ v2' : '# Документ v1',
    })));
  });

  it('opens the latest revision of the source stage on a 1 → 2 edge', async () => {
    render(<RevisionWindow task={task} source={1} target={2} onClose={() => undefined} />);
    expect(screen.getByRole('dialog', { name: 'Результат этапа 1' })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Документ v2' })).toBeInTheDocument());
    expect(screen.getByRole('button', { name: /v2 · последняя/ })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText(/01_tech_doc_v2\.md/)).toBeInTheDocument();
  });

  it('switches to an older revision when its version is clicked', async () => {
    render(<RevisionWindow task={task} source={1} target={2} onClose={() => undefined} />);
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Документ v2' })).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'v1' }));
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Документ v1' })).toBeInTheDocument());
    expect(screen.getByRole('button', { name: 'v1' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('renders the review verdict as JSON', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, text: async () => '{"status":"REJECTED","comments":["Поправить"]}' })));
    render(<RevisionWindow task={task} source={2} target={1} onClose={() => undefined} />);
    await waitFor(() => expect(document.querySelector('pre')?.textContent).toContain('"status": "REJECTED"'));
  });

  it('shows an empty state when the stage has no revision yet', async () => {
    render(<RevisionWindow task={task} source={4} target={3} onClose={() => undefined} />);
    await waitFor(() => expect(screen.getByText('Результат этапа ещё не готов')).toBeInTheDocument());
  });
});
