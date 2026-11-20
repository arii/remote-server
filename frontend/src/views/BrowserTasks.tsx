import { useEffect, useRef, useState, type Dispatch, type FormEvent, type SetStateAction } from 'react';
import { browserTaskShotUrl, cancelBrowserTask, createBrowserTask, fetchBrowserTasks, isAuthExpired } from '../apiClient';
import type { BrowserTarget, BrowserTask } from '../apiClient';

const pending = (task: BrowserTask) => ['queued', 'running', 'cancelling'].includes(task.status);

export default function BrowserTasks({ target, paired, tasks, onChange }: {
  target: BrowserTarget;
  paired: boolean;
  tasks: BrowserTask[];
  onChange: Dispatch<SetStateAction<BrowserTask[]>>;
}) {
  const [instructions, setInstructions] = useState('');
  const [startUrl, setStartUrl] = useState('');
  const [mode, setMode] = useState<'check' | 'agent'>('check');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const continuePolling = useRef(false);

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    let polling = continuePolling.current;
    async function load() {
      // Quiet while the auth-expired banner is up (#1410); the chain
      // stays armed so polling resumes when the flag retires.
      if (isAuthExpired()) {
        if (active && polling) timer = setTimeout(() => void load(), 2000);
        return;
      }
      try {
        const rows = await fetchBrowserTasks();
        if (!active) return;
        onChange(rows);
        setError(null);
        polling = rows.some(pending);
        continuePolling.current = polling;
      } catch (cause) {
        if (!active) return;
        setError(String(cause));
      }
      if (active && polling) timer = setTimeout(() => void load(), 2000);
    }
    void load();
    return () => { active = false; clearTimeout(timer); };
  }, [refresh, onChange]);

  async function run(event: FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    try {
      const task = await createBrowserTask({ instructions, target, mode, ...(startUrl.trim() ? { start_url: startUrl.trim() } : {}) });
      continuePolling.current ||= pending(task);
      onChange((rows) => [task, ...rows.filter((row) => row.id !== task.id)]);
      setRefresh((value) => value + 1);
    } catch (cause) { setError(String(cause)); }
    finally { setSubmitting(false); }
  }

  async function cancel(id: string) {
    try {
      const task = await cancelBrowserTask(id);
      onChange((rows) => rows.map((row) => row.id === id ? task : row));
      setRefresh((value) => value + 1);
    } catch (cause) { setError(String(cause)); }
  }

  return <section className="browser-tasks" aria-label="Browser tasks">
    <div className="cobrowse-head"><h2 className="row-title">Tasks</h2><button type="button" onClick={() => setRefresh((value) => value + 1)}>Refresh tasks</button></div>
    <form onSubmit={(event) => void run(event)}>
      <label>Instructions<textarea required maxLength={20000} value={instructions} onChange={(event) => setInstructions(event.target.value)} /></label>
      <label>Start URL (optional)<input type="url" value={startUrl} onChange={(event) => setStartUrl(event.target.value)} /></label>
      <label>Mode<select value={mode} onChange={(event) => setMode(event.target.value as 'check' | 'agent')}><option value="check">Check (no LLM)</option><option value="agent">Agent</option></select></label>{' '}
      <button type="submit" disabled={submitting || !instructions.trim() || (target === 'paired' && !paired)}>{submitting ? 'Starting…' : 'Run'}</button>
    </form>
    {error && <p role="alert">{error}</p>}
    {tasks.length === 0 && <p className="row-meta">No tasks yet.</p>}
    {tasks.map((task) => <details key={task.id} className="browser-task-report">
      <summary>{task.instructions.slice(0, 80)} · {task.target === 'paired' ? 'My Chrome' : 'Shared browser'} · {task.status}</summary>
      {pending(task) && <button type="button" disabled={task.status === 'cancelling'} onClick={() => void cancel(task.id)}>Cancel</button>}
      {task.error && <p role="alert">{task.error}</p>}
      <p>URL: {task.url ?? 'Pending'}</p><p>Title: {task.title ?? 'Pending'}</p>
      <p>landed_on_login: {task.landed_on_login === undefined ? 'Pending' : String(task.landed_on_login)}</p>
      {task.summary && <p>{task.summary}</p>}
      {task.captures.map((capture) => <img key={capture.shot} loading="lazy" src={browserTaskShotUrl(task.id, capture.shot)} alt={`Task ${task.id} screenshot ${capture.shot + 1}`} />)}
      <details><summary>Observed text</summary><pre>{task.observed_text ?? ''}</pre></details>
    </details>)}
  </section>;
}
