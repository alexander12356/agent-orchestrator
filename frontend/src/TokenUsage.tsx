import { fmtTokens } from './api';
import type { TokenUsage as Usage } from './types';

const categories = [
  ['input_tokens', 'Вход', 'Вход без чтения из кэша', 'text-sky-300'],
  ['cache_tokens', 'Кэш', 'Прочитано из кэша', 'text-violet-300'],
  ['output_tokens', 'Выход', 'Сгенерировано моделью', 'text-emerald-300'],
] as const;

export default function TokenUsage({ usage }: { usage: Usage }) {
  const missing = usage.usage_partial && categories.every(([key]) => !usage[key]);
  return <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] tabular-nums">
    {categories.map(([key, label, description, color]) => {
      const count = usage[key] ?? 0;
      return <span key={key} className={color} title={missing ? description + ' · нет данных' : description + ' · подтверждено ' + count.toLocaleString('ru-RU')}>{label} {missing ? '—' : fmtTokens(count)}</span>;
    })}
    {!!usage.usage_partial && <span className="text-amber-300" title="По части запусков отсутствует статистика CLI">Неполные данные</span>}
  </span>;
}
