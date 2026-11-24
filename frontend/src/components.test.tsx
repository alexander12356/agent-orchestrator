import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { StageBadge } from './components';

describe('StageBadge', () => {
  it('shows a separate waiting-for-answer stage', () => {
    render(<StageBadge task={{ current_stage: 1, status: 'waiting_answer' }} />);
    expect(screen.getByText(/Ожидает ответа по ТЗ/)).toBeInTheDocument();
  });
  it('shows completed tasks', () => {
    render(<StageBadge task={{ current_stage: 6, status: 'done' }} />);
    expect(screen.getByText('Завершено')).toBeInTheDocument();
  });
});
