import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Tasks from './Tasks';
import { Agent, Task } from './types';

const task: Task = {
  id: 7,
  title: 'Imported task',
  description: 'Description',
  external_issue_id: 'yougile-id',
  external_issue_key: 'ZEL-17',
  external_issue_url: 'https://ru.yougile.com/team/068505cc4436/#ZEL-17',
  milestone: 'Прототип',
  sprint: 'Спринт 1',
  current_stage: 1,
  status: 'open',
  branch_name: 'feat/task-7',
  merged_to_main: false,
  stage_agents: {},
  dependencies: [],
  total_tokens: 0,
  created_at: '2026-01-01T00:00:00Z',
};

const architect: Agent = {
  id: 1,
  name: 'Архитектор',
  avatar_url: '🏛️',
  role_prompt: '',
  concurrency_limit: 1,
  primary_config: { cli: 'custom', model: 'grok-4.7-high', environment: 'server' },
  fallback_configs: [],
  fallback_triggers: { rate_limit: true, exit_code: true, timeout: true, timeout_minutes: 20 },
};

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('Task deletion', () => {
  it('confirms and deletes a task', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal('fetch', fetchMock);
    vi.stubGlobal('confirm', vi.fn().mockReturnValue(true));
    const onDeleted = vi.fn();
    render(<Tasks tasks={[task]} agents={[]} reload={vi.fn()} openTask={vi.fn()} onDeleted={onDeleted} notify={vi.fn()}/>);

    expect(screen.getByRole('link', { name: 'ZEL-17 ↗' })).toHaveAttribute('href', 'https://ru.yougile.com/team/068505cc4436/#ZEL-17');
    expect(screen.queryByText('Description')).not.toBeInTheDocument();
    expect(screen.getByText('Прототип')).toBeInTheDocument();
    expect(screen.getByText('Спринт 1')).toBeInTheDocument();
    expect(screen.queryByText('yougile-id')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTitle('Удалить задачу'));

    await waitFor(() => expect(onDeleted).toHaveBeenCalledWith(7));
    expect(fetchMock).toHaveBeenCalledWith('/api/tasks/7', expect.objectContaining({ method: 'DELETE' }));
  });
});

describe('Task start', () => {
  it('starts the pipeline for an open task', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 202 }));
    vi.stubGlobal('fetch', fetchMock);
    const reload = vi.fn();
    render(<Tasks tasks={[task]} agents={[architect]} reload={reload} openTask={vi.fn()} onDeleted={vi.fn()} notify={vi.fn()}/>);

    fireEvent.click(screen.getByTitle('Запустить пайплайн'));

    await waitFor(() => expect(reload).toHaveBeenCalled());
    expect(fetchMock).toHaveBeenCalledWith('/api/tasks/7/start', expect.objectContaining({ method: 'POST' }));
  });
});

describe('Bot assignment', () => {
  it('saves an empty assignment instead of a default agent', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(task), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    render(<Tasks tasks={[task]} agents={[architect]} reload={vi.fn()} openTask={vi.fn()} onDeleted={vi.fn()} notify={vi.fn()}/>);

    fireEvent.click(screen.getByTitle('Редактировать'));
    const stage = screen.getByRole('combobox', { name: 'Этап 1' });
    fireEvent.change(stage, { target: { value: '1' } });
    fireEvent.change(stage, { target: { value: '' } });
    expect(stage).toHaveValue('');
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить задачу' }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const body = JSON.parse(fetchMock.mock.calls[0][1]?.body as string);
    expect(body.stage_agents).toEqual({});
    expect(body.token_budget).toBeNull();
  });

  it('saves the token budget of the task', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(task), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    render(<Tasks tasks={[task]} agents={[architect]} reload={vi.fn()} openTask={vi.fn()} onDeleted={vi.fn()} notify={vi.fn()}/>);

    fireEvent.click(screen.getByTitle('Редактировать'));
    fireEvent.change(screen.getByLabelText('Бюджет токенов'), { target: { value: '5000' } });
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить задачу' }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(JSON.parse(fetchMock.mock.calls[0][1]?.body as string).token_budget).toBe(5000);
  });
});

describe('Task waiting for a person', () => {
  it('is listed under Ожидают and continues instead of starting', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 202 }));
    vi.stubGlobal('fetch', fetchMock);
    const reload = vi.fn();
    render(<Tasks tasks={[{ ...task, current_stage: 2, status: 'needs_human', hold_reason: 'Бюджет токенов исчерпан' }]} agents={[architect]} reload={reload} openTask={vi.fn()} onDeleted={vi.fn()} notify={vi.fn()}/>);

    fireEvent.click(screen.getByRole('button', { name: 'Ожидают' }));
    expect(screen.getByText('Imported task')).toBeInTheDocument();
    expect(screen.queryByTitle('Запустить пайплайн')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTitle('Продолжить'));

    await waitFor(() => expect(reload).toHaveBeenCalled());
    expect(fetchMock).toHaveBeenCalledWith('/api/tasks/7/start', expect.objectContaining({ method: 'POST' }));
  });
});
