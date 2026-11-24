import { Bot, GripVertical, Link2, Plus, Save, Settings2, ShieldCheck, Trash2, Upload } from 'lucide-react';
import { FormEvent, useEffect, useState } from 'react';
import { api } from './api';
import { Avatar, Modal } from './components';
import { Agent, ModelConfig, Settings as SettingsType } from './types';

const emptyConfig: ModelConfig = { cli: 'custom', model: '', environment: 'server', command: '' };
const emptyAgent: Omit<Agent, 'id'> = { name: '', avatar_url: '🤖', role_prompt: '', concurrency_limit: 1, primary_config: emptyConfig, fallback_configs: [], fallback_triggers: { rate_limit: true, exit_code: true, timeout: true, timeout_minutes: 20 } };

export default function Settings({ agents, reload, notify }: { agents: Agent[]; reload: () => void; notify: (message: string) => void }) {
  const [editing, setEditing] = useState<Agent | 'new' | null>(null);
  const [settings, setSettings] = useState<SettingsType | null>(null);
  const [integrationState, setIntegrationState] = useState<{ busy: boolean; error: string }>({ busy: false, error: '' });
  useEffect(() => { api<SettingsType>('/settings').then(setSettings); }, []);
  const persistSettings = async () => {
    if (!settings) return;
    await api('/settings', { method: 'PUT', body: JSON.stringify(settings) });
    setSettings(await api<SettingsType>('/settings'));
  };
  const saveSettings = async (e: FormEvent) => {
    e.preventDefault();
    try { await persistSettings(); notify('Настройки интеграций сохранены'); } catch (error) { setIntegrationState({ busy: false, error: (error as Error).message }); }
  };
  const yougileAction = async (path: string, success: string) => {
    setIntegrationState({ busy: true, error: '' });
    try {
      await persistSettings();
      await api(path, { method: 'POST' });
      setIntegrationState({ busy: false, error: '' });
      notify(success);
    } catch (error) { setIntegrationState({ busy: false, error: (error as Error).message }); }
  };
  const importTasks = async () => {
    setIntegrationState({ busy: true, error: '' });
    try {
      await persistSettings();
      const result = await api<{ imported: number; updated: number; skipped: number }>('/integrations/yougile/import', { method: 'POST' });
      setIntegrationState({ busy: false, error: '' });
      notify(`Импорт: ${result.imported} новых, ${result.updated} обновлено, ${result.skipped} пропущено`);
      reload();
    } catch (error) { setIntegrationState({ busy: false, error: (error as Error).message }); }
  };
  return <main className="mx-auto max-w-6xl space-y-8 p-7">
    <div><h1 className="text-2xl font-semibold">Настройка ботов и интеграций</h1><p className="mt-1 text-sm text-slate-500">Модели, очереди и подключения к внешним системам</p></div>
    <section><div className="mb-4 flex items-center justify-between"><h2 className="flex items-center gap-2 text-sm font-medium"><Bot size={17} className="text-indigo-400"/>Профили ботов</h2><button className="btn" onClick={() => setEditing('new')}><Plus size={15}/>Добавить бота</button></div>
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">{agents.map(agent => <button key={agent.id} onClick={() => setEditing(agent)} className="card p-5 text-left transition hover:-translate-y-0.5 hover:border-indigo-500/50"><div className="flex items-center gap-3"><Avatar agent={agent} size={42}/><div><h3 className="text-sm font-medium">{agent.name}</h3><span className="text-[10px] text-slate-500">Лимит: {agent.concurrency_limit} параллельно</span></div></div><p className="mt-4 line-clamp-2 h-8 text-[11px] text-slate-500">{agent.role_prompt || 'Системный промпт не задан'}</p><div className="mt-4 flex items-center justify-between border-t border-slate-800 pt-3 text-[10px]"><span className="text-indigo-300">{agent.primary_config.cli} · {agent.primary_config.model || 'модель по умолчанию'}</span><span className="text-slate-600">{agent.primary_config.environment === 'server' ? 'VDS' : 'Unity Worker'}</span></div></button>)}</div>
    </section>
    {settings && <form onSubmit={saveSettings} className="card p-6"><div className="mb-5 flex items-center justify-between"><h2 className="flex items-center gap-2 text-sm font-medium"><Link2 size={17} className="text-indigo-400"/>Интеграции и Webhooks</h2><button className="btn btn-primary"><Save size={14}/>Сохранить</button></div>
      <div className="rounded-lg border border-slate-800 p-4">
        <div className="grid items-end gap-4 md:grid-cols-[1fr_auto_auto]"><label>Yougile API Key<input type="password" value={settings.tracker_api_token} onChange={e => setSettings({...settings, tracker_api_token: e.target.value})} placeholder="Ключ REST API v2"/></label><button type="button" className="btn" disabled={integrationState.busy} onClick={() => yougileAction('/integrations/yougile/test', 'Соединение с Yougile установлено')}><ShieldCheck size={14}/>Проверить подключение</button><button type="button" className="btn" disabled={integrationState.busy} onClick={() => yougileAction('/integrations/yougile/webhook', 'Webhook Yougile зарегистрирован')}><Link2 size={14}/>Создать webhook</button></div>
        <div className="mt-4 grid items-end gap-4 border-t border-slate-800 pt-4 md:grid-cols-[1fr_1fr_1fr_auto]"><label>Название проекта Yougile<input value={settings.yougile_project} onChange={e => setSettings({...settings, yougile_project: e.target.value})} placeholder="Например, Unity"/></label><label>Имя доски<input value={settings.yougile_board} onChange={e => setSettings({...settings, yougile_board: e.target.value})} placeholder="Например, Development"/></label><label>Стикер (необязательно)<input value={settings.yougile_sticker} onChange={e => setSettings({...settings, yougile_sticker: e.target.value})} placeholder="Development.Код"/></label><button type="button" className="btn btn-primary" disabled={integrationState.busy} onClick={importTasks}><Upload size={14}/>{integrationState.busy ? 'Выполняется...' : 'Импортировать задачи'}</button></div>
        <label className="mt-4">URL команды Yougile<input value={settings.yougile_team_url} onChange={e => setSettings({...settings, yougile_team_url: e.target.value})} placeholder="https://ru.yougile.com/team/.../"/></label>
        <p className="mt-3 text-[11px] text-slate-500">Импортируются задачи из колонок Open и Ready вместе с описанием и чатом. Стикер вида Development.Код ограничивает импорт выбранным состоянием. Задачи получают статус Open и не запускают ботов.</p>
        {settings.yougile_webhook_id && <p className="mt-2 text-[11px] text-emerald-400">Webhook подключен: {settings.yougile_webhook_id}</p>}{integrationState.error && <p className="mt-2 text-xs text-rose-400">{integrationState.error}</p>}
      </div>
      <div className="mt-6 grid gap-4">{[['Git webhook', settings.git_webhook_url, 'git_webhook_secret'], ['Yougile webhook', settings.tracker_webhook_url, 'tracker_webhook_secret']].map(([title, url, key]) => <div key={key} className="grid items-end gap-3 rounded-lg border border-slate-800 p-4 md:grid-cols-[1fr_280px]"><label>{title} URL<input readOnly value={url}/></label><label>Secret<input type="password" value={settings[key as keyof SettingsType]} onChange={e => setSettings({...settings, [key]: e.target.value})}/></label></div>)}<div className="grid items-end gap-3 rounded-lg border border-slate-800 p-4 md:grid-cols-[1fr_280px]"><label>Worker WebSocket<input readOnly value={settings.worker_websocket_url}/></label><label>Worker Token<input type="password" value={settings.worker_token} onChange={e => setSettings({...settings, worker_token: e.target.value})}/></label></div></div>
      <p className="mt-4 flex items-center gap-2 text-[10px] text-slate-600"><ShieldCheck size={13}/>Git Webhook проверяется по HMAC SHA-256, Yougile — по token в callback URL. Секреты хранятся в локальной SQLite БД.</p>
    </form>}
    {editing && <AgentModal agent={editing === 'new' ? null : editing} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); reload(); notify('Профиль бота сохранен'); }}/>} 
  </main>;
}

function AgentModal({ agent, onClose, onSaved }: { agent: Agent | null; onClose: () => void; onSaved: () => void }) {
  const [data, setData] = useState<Omit<Agent, 'id'>>(agent ? { ...agent } : { ...emptyAgent, primary_config: {...emptyConfig} });
  const [error, setError] = useState('');
  const updateConfig = (config: ModelConfig) => setData({...data, primary_config: config});
  const submit = async (e: FormEvent) => { e.preventDefault(); try { await api(agent ? `/agents/${agent.id}` : '/agents', { method: agent ? 'PUT' : 'POST', body: JSON.stringify(data) }); onSaved(); } catch (e) { setError((e as Error).message); } };
  const move = (index: number, delta: number) => { const next = [...data.fallback_configs]; const target = index + delta; if (target < 0 || target >= next.length) return; [next[index], next[target]] = [next[target], next[index]]; setData({...data, fallback_configs: next}); };
  return <Modal title={agent ? `Настройка: ${agent.name}` : 'Новый бот'} onClose={onClose} wide><form onSubmit={submit} className="space-y-5">
    <div className="grid gap-4 sm:grid-cols-[1fr_150px_110px]"><label>Имя бота<input required value={data.name} onChange={e => setData({...data, name: e.target.value})}/></label><label>Аватарка (иконка)<input value={data.avatar_url} onChange={e => setData({...data, avatar_url: e.target.value})}/></label><label>Concurrency<input type="number" min={1} value={data.concurrency_limit} onChange={e => setData({...data, concurrency_limit: Number(e.target.value)})}/></label></div>
    <label>Роль (системный промпт)<textarea rows={4} value={data.role_prompt} onChange={e => setData({...data, role_prompt: e.target.value})}/></label>
    <ConfigEditor title="Основная модель" value={data.primary_config} onChange={updateConfig}/>
    <div><div className="mb-2 flex items-center justify-between"><span className="text-xs text-slate-400">Fallback Chain</span><button type="button" className="btn !py-1 text-[11px]" onClick={() => setData({...data, fallback_configs: [...data.fallback_configs, {...emptyConfig}]})}><Plus size={13}/>Добавить резерв</button></div><div className="space-y-3">{data.fallback_configs.map((config, i) => <div key={i} className="relative rounded-lg border border-slate-800 pt-2"><div className="absolute right-2 top-2 flex gap-1"><button type="button" className="icon-btn" onClick={() => move(i, -1)}>↑</button><button type="button" className="icon-btn" onClick={() => move(i, 1)}>↓</button><button type="button" className="icon-btn" onClick={() => setData({...data, fallback_configs: data.fallback_configs.filter((_, n) => n !== i)})}><Trash2 size={13}/></button></div><ConfigEditor title={`Резерв ${i + 1}`} value={config} onChange={value => setData({...data, fallback_configs: data.fallback_configs.map((v, n) => n === i ? value : v)})}/></div>)}</div></div>
    <div className="flex flex-wrap items-center gap-5 rounded-lg border border-slate-800 p-3 text-xs text-slate-400">{[['rate_limit', 'Rate Limit'], ['exit_code', 'Exit Code != 0'], ['timeout', 'Таймаут']].map(([key, title]) => <label key={key} className="flex grid-cols-none items-center gap-2"><input className="!w-4" type="checkbox" checked={data.fallback_triggers[key as 'rate_limit']} onChange={e => setData({...data, fallback_triggers: {...data.fallback_triggers, [key]: e.target.checked}})}/>{title}</label>)}<label className="!w-28">Минут<input type="number" min={1} value={data.fallback_triggers.timeout_minutes} onChange={e => setData({...data, fallback_triggers: {...data.fallback_triggers, timeout_minutes: Number(e.target.value)}})}/></label></div>
    {error && <div className="text-xs text-rose-400">{error}</div>}<footer className="flex justify-end gap-2"><button type="button" className="btn" onClick={onClose}>Отмена</button><button className="btn btn-primary">Сохранить профиль</button></footer>
  </form></Modal>;
}

function ConfigEditor({ title, value, onChange }: { title: string; value: ModelConfig; onChange: (config: ModelConfig) => void }) {
  return <div className="rounded-lg border border-slate-800 p-4"><div className="mb-3 text-xs font-medium text-slate-300">{title}</div><div className="grid gap-3 sm:grid-cols-3"><label>CLI<select value={value.cli} onChange={e => onChange({...value, cli: e.target.value as ModelConfig['cli']})}><option value="claude">Claude Code</option><option value="codex">Codex CLI</option><option value="gemini">Gemini CLI</option><option value="aider">Aider</option><option value="custom">Custom CLI</option></select></label><label>Модель<input value={value.model} onChange={e => onChange({...value, model: e.target.value})} placeholder="model-id"/></label><label>Среда<select value={value.environment} onChange={e => onChange({...value, environment: e.target.value as 'server' | 'worker'})}><option value="server">Server (VDS)</option><option value="worker">Local Worker</option></select></label></div><label className="mt-3">Custom command / переопределение команды<input value={value.command || ''} onChange={e => onChange({...value, command: e.target.value})} placeholder="python agent.py --task {task_id} --stage {stage}"/></label></div>;
}
