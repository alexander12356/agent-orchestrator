const base = '/api';

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

export const fmtTokens = (n: number) => n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n || 0);
export const stageName = (stage: number, status?: string) => status === 'waiting_answer' ? '1.5. Ожидает ответа по ТЗ' : ['Ожидает влития в main', 'Тех. документация', 'Ревью документации', 'Реализация', 'Ревью реализации', 'Готово к PR', 'Завершено'][stage];
