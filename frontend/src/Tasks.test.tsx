import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Tasks from './Tasks';
import { Task } from './types';

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

afterEach(() => vi.unstubAllGlobals());

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
