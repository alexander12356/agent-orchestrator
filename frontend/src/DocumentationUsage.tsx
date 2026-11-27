import { Flame } from 'lucide-react';
import { artifactUrl } from './ArtifactPage';
import TokenUsage from './TokenUsage';
import type { DocumentationUsage as Usage } from './types';

const prices = new Intl.NumberFormat('ru-RU', {
  style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 6,
});

const launchKinds = {
  draft: 'Черновик',
  revision: 'Доработка',
  legacy: 'Исторический расход',
  unrecorded: 'Данные запуска недоступны',
};
const launchStatuses: Record<string, string> = {
  running: 'В работе',
  interrupted: 'Прерван',
  error: 'Ошибка',
};

function estimate(cost: number | null, partial: boolean) {
  if (cost === null) return 'Стоимость недоступна';
  const value = cost > 0 && cost < 0.000001 ? '< $0,000001' : prices.format(cost);
  return partial ? `${value} по учтённым данным` : `≈ ${value}`;
}

export default function DocumentationUsage({ taskId, usage }: { taskId: number; usage: Usage }) {
  return <section className="mb-5 space-y-3">
    <h3 className="flex items-center gap-2 text-xs font-medium text-slate-300"><Flame size={14}/>Расход первого этапа</h3>
    <div className="space-y-1.5 rounded-lg border border-slate-800 bg-slate-900/50 p-3 text-[11px] text-slate-300">
      {usage.accepted_document && <a className="block text-indigo-300 hover:underline" href={artifactUrl(taskId, usage.accepted_document)} target="_blank" rel="noreferrer">
        Принятый документ{usage.accepted_attempt !== null ? ` · попытка ${usage.accepted_attempt}` : ''}
      </a>}
      <p className="text-orange-300">{usage.accepted_document ? 'До принятия документа' : 'Расход на документ'} · {estimate(usage.cost_usd, usage.cost_partial)}</p>
      {usage.cost_partial && <p className="text-amber-300">Неполная оценка стоимости</p>}
      <p>Возвратов документа с ревью · {usage.review_history_partial && '≥ '}{usage.doc_returns}</p>
      <p className="text-slate-400">Оценка в USD по сохранённым базовым тарифам за токены.</p>
      {usage.implementation_reviews.length > 0 && <details>
        <summary className="cursor-pointer text-indigo-300">Замечания по реализации · {usage.implementation_reviews.reduce((sum, review) => sum + review.comments.length + review.notes.length, 0)}</summary>
        {usage.implementation_reviews.map(review => <div key={review.attempt} className="mt-2 border-t border-slate-800 pt-2">
          <a className="text-indigo-300 hover:underline" href={artifactUrl(taskId, review.artifact)} target="_blank" rel="noreferrer">
            Ревью реализации · попытка {review.attempt} · {review.status === 'REJECTED' ? 'возврат' : 'принято'}
          </a>
          <ul className="mt-1 list-disc space-y-1 pl-4">
            {review.comments.map((comment, index) => <li key={index}>{comment}</li>)}
            {review.notes.map((note, index) => <li key={review.comments.length + index} className="text-slate-400">{note}</li>)}
          </ul>
        </div>)}
      </details>}
    </div>
    {usage.attempts.map(attempt => <div key={attempt.attempt} className="rounded-lg border border-slate-800 bg-slate-900/50 p-3 text-[11px]">
      <div className="font-medium text-slate-200">Попытка {attempt.attempt}{attempt.attempt === usage.accepted_attempt && ' · принятая версия'}</div>
      {attempt.launches.map((launch, index) => <div key={launch.id} className="mt-2 space-y-1 border-t border-slate-800 pt-2">
        <p className="text-slate-300">{launchKinds[launch.kind]}{launch.is_fallback ? ' · Fallback' : ''}{attempt.launches.length > 1 && ` · запуск ${index + 1}`}</p>
        {launchStatuses[launch.status] && <p className="text-slate-400">{launchStatuses[launch.status]}</p>}
        <p className="break-all text-slate-400">{launch.model_mixed ? 'Несколько моделей' : launch.model || 'Модель не определена'}{launch.cli && ` · ${launch.cli}`}</p>
        <TokenUsage usage={launch}/>
        <p className="text-orange-300">{launch.cost_usd === null && !launch.usage_partial && launch.model && !launch.model_mixed
          ? 'Тариф не найден' : estimate(launch.cost_usd, launch.usage_partial)}</p>
      </div>)}
      {attempt.error && <p className="mt-2 whitespace-pre-wrap break-words text-rose-400">{attempt.error}</p>}
    </div>)}
  </section>;
}
