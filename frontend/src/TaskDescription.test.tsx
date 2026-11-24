import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import TaskDescription from './TaskDescription';
import { Task } from './types';

const task: Task = {
  id: 63,
  title: 'Провайдеры NodeCanvas',
  description: '<p><strong>Связи</strong></p><ul><li>Опирается на <a href="https://example.com/docs">NodeCanvas</a></li></ul><script>alert(1)</script>',
  external_issue_id: 'uuid',
  external_issue_key: 'ZEL-17',
  external_issue_url: 'https://ru.yougile.com/team/068505cc4436/#ZEL-17',
  milestone: 'Прототип',
  sprint: 'Спринт 1',
  current_stage: 1,
  status: 'open',
  branch_name: 'feat/task-63',
  merged_to_main: false,
  stage_agents: {},
  dependencies: [],
  total_tokens: 0,
  created_at: '2026-01-01T00:00:00Z',
};

describe('TaskDescription', () => {
  it('renders Yougile HTML as formatted safe markdown', () => {
    render(<TaskDescription task={task}/>);

    expect(screen.getByText('Связи').tagName).toBe('STRONG');
    expect(screen.getByRole('link', { name: 'NodeCanvas' })).toHaveAttribute('href', 'https://example.com/docs');
    expect(screen.getByRole('link', { name: 'ZEL-17 ↗' })).toBeInTheDocument();
    expect(screen.getByText('Прототип → Спринт 1')).toBeInTheDocument();
    expect(screen.queryByText(/<p>|alert\(1\)/)).not.toBeInTheDocument();
  });
});
