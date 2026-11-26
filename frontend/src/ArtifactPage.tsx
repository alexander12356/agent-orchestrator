import { useEffect, useState } from 'react';
import Markdown from './Markdown';

export function artifactUrl(taskId: number, name: string) {
  if (name.toLowerCase().endsWith('.md')) {
    return `/artifact?${new URLSearchParams({ task: String(taskId), name })}`;
  }
  return `/api/tasks/${taskId}/artifacts/${encodeURIComponent(name)}`;
}

export default function ArtifactPage({ taskId, name }: { taskId: number; name: string }) {
  const [content, setContent] = useState<string>();
  const [error, setError] = useState(false);

  useEffect(() => {
    document.title = name;
    const controller = new AbortController();
    fetch(`/api/tasks/${taskId}/artifacts/${encodeURIComponent(name)}`, { signal: controller.signal })
      .then(response => {
        if (!response.ok) throw new Error();
        return response.text();
      })
      .then(text => { if (!controller.signal.aborted) setContent(text); })
      .catch(() => { if (!controller.signal.aborted) setError(true); });
    return () => controller.abort();
  }, [taskId, name]);

  return <main className="mx-auto max-w-5xl p-6">
    <h1 className="mb-4 break-all text-lg font-semibold">{name}</h1>
    <div className="card p-5">
      {error ? <p role="alert" className="text-sm text-rose-400">Не удалось загрузить артефакт</p>
        : content === undefined ? <p className="text-sm text-slate-500">Загрузка артефакта…</p>
        : <div className="markdown"><Markdown text={content}/></div>}
    </div>
  </main>;
}
