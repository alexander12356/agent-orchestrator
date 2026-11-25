import type { Task } from './types';

const base = '/api';

export const canStartPipeline = (task: Task) =>
  !task.merged_to_main && ['open', 'stage_ready', 'needs_human', 'error'].includes(task.status);

export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(base + path, { ...options, headers: { 'Content-Type': 'application/json', ...options?.headers } });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try { message = (await response.json()).detail || message; } catch { /* non-json response */ }
    throw new Error(message);
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}

export const spentTokens = (run: { input_tokens?: number; output_tokens?: number; cache_tokens?: number }) => (run.input_tokens || 0) + (run.output_tokens || 0) + (run.cache_tokens || 0);
export const fmtTokens = (n: number) => n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n || 0);
export const stageName = (stage: number, status?: string) => status === 'open' ? 'Open' : status === 'waiting_answer' ? '1.5. Ожидает ответа по ТЗ' : status === 'needs_human' ? `Нужен человек (этап ${stage})` : ['Ожидает влития в main', 'Тех. документация', 'Ревью документации', 'Реализация', 'Ревью реализации', 'Готово к PR', 'Завершено'][stage];
