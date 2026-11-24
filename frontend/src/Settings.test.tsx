import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Settings from './Settings';

const settings = {
  tracker_api_token: '',
  yougile_project: '',
  yougile_board: '',
  yougile_sticker: '',
  yougile_team_url: 'https://ru.yougile.com/team/068505cc4436/',
  tracker_webhook_secret: '',
  yougile_webhook_id: '',
  git_webhook_secret: '',
  worker_token: '',
  git_webhook_url: 'https://orchestrator.test/api/webhooks/git',
  tracker_webhook_url: 'https://orchestrator.test/api/webhooks/tracker',
  worker_websocket_url: 'wss://orchestrator.test/ws/worker',
};

afterEach(() => vi.unstubAllGlobals());

describe('Yougile settings', () => {
  it('shows only Yougile tracker actions', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(settings), { status: 200 })));
    render(<Settings agents={[]} reload={vi.fn()} notify={vi.fn()}/>);

    expect(await screen.findByLabelText('Yougile API Key')).toBeInTheDocument();
    expect(screen.getByLabelText('Название проекта Yougile')).toBeInTheDocument();
    expect(screen.getByLabelText('Имя доски')).toBeInTheDocument();
    expect(screen.getByLabelText('Стикер (необязательно)')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Импортировать задачи' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Проверить подключение' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Создать webhook' })).toBeInTheDocument();
    expect(screen.queryByText(/Jira|Linear|YouTrack|Custom API/)).not.toBeInTheDocument();
  });
});
