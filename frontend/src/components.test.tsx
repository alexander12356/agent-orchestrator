import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { StageBadge } from './components';

describe('StageBadge', () => {
  it('shows a separate waiting-for-answer stage', () => {
    render(<StageBadge task={{ current_stage: 1, status: 'waiting_answer' }} />);
    expect(screen.getByText(/Ожидает ответа по ТЗ/)).toBeInTheDocument();
  });
  it('shows imported open tasks', () => {
    render(<StageBadge task={{ current_stage: 1, status: 'open' }} />);
    expect(screen.getByText('Open')).toBeInTheDocument();
  });
  it('shows completed tasks', () => {
    render(<StageBadge task={{ current_stage: 6, status: 'done' }} />);
    expect(screen.getByText('Завершено')).toBeInTheDocument();
  });
  it('asks for a person and explains why', () => {
    render(<StageBadge task={{ current_stage: 2, status: 'needs_human', hold_reason: 'Ревью отклонило документацию 4 раз подряд' }} />);
    const badge = screen.getByText(/Нужен человек \(этап 2\)/);
    expect(badge).toHaveClass('badge-orange');
    expect(badge).toHaveAttribute('title', 'Ревью отклонило документацию 4 раз подряд');
  });
});
