import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { isStalled, RunsPane, STALL_SECONDS, StreamProgress, streamProgress } from './ChatView';
import type { AgentRun } from '../apiClient';

describe('stream progress (issue #376)', () => {
  it('formats elapsed alone before anything arrives', () => {
    expect(streamProgress(0, null, 0)).toBe('Working… 0s');
    expect(streamProgress(45, null, 0)).toBe('Working… 45s');
  });

  it('appends phase and token count', () => {
    expect(streamProgress(45, 'prefill', 0)).toBe('Working… 45s · prefill');
    expect(streamProgress(45, 'prefill', 12)).toBe('Working… 45s · prefill · 12 tok');
    expect(streamProgress(45, null, 12)).toBe('Working… 45s · 12 tok');
  });

  it('renders the progress line', () => {
    const html = renderToStaticMarkup(
      <StreamProgress elapsed={45} phase="prefill" tokens={12} />,
    );
    expect(html).toContain('data-testid="chat-progress"');
    expect(html).toContain('Working… 45s · prefill · 12 tok');
  });
});

describe('stall watchdog (issue #376)', () => {
  it('flags streams silent past the stall window', () => {
    expect(STALL_SECONDS).toBe(180);
    expect(isStalled(0, 179_999)).toBe(false);
    expect(isStalled(0, 180_000)).toBe(false);
    expect(isStalled(0, 180_001)).toBe(true);
  });
});

function pastRun(over: Partial<AgentRun> = {}): AgentRun {
  return {
    id: 'run-0',
    script: 'deep_chat.py',
    model: 'm',
    thread: 't',
    started_ts: 0,
    duration_s: 1,
    ok: true,
    tools: [],
    tokens: 0,
    preview: 'hello',
    has_prompt: true,
    ...over,
  };
}

function paneHtml(runs: AgentRun[], notice: string | null = null): string {
  return renderToStaticMarkup(
    <RunsPane
      runs={runs}
      selected={null}
      detail={null}
      onSelect={() => {}}
      onContinue={() => {}}
      onDelete={() => {}}
      onClearFinished={() => {}}
      notice={notice}
    />,
  );
}

describe('past runs manage (issue #412)', () => {
  it('renders continue and delete per row plus clear finished', () => {
    const html = paneHtml([pastRun()]);
    expect(html).toContain('data-testid="chat-run-continue-run-0"');
    expect(html).toContain('>Continue</button>');
    expect(html).toContain('data-testid="chat-run-delete-run-0"');
    expect(html).toContain('data-testid="chat-runs-clear"');
    expect(html).toContain('>Clear finished</button>');
  });

  it('disables continue when the run has no stored prompt', () => {
    const html = paneHtml([pastRun({ id: 'run-1', has_prompt: false })]);
    expect(html).toContain('data-testid="chat-run-continue-run-1" disabled=""');
    expect(html).toContain('No stored prompt for this run');
  });

  it('enables continue when the run has a stored prompt', () => {
    const html = paneHtml([pastRun({ has_prompt: true })]);
    expect(html).not.toContain('disabled=""');
  });

  it('shows the runs notice', () => {
    const html = paneHtml([pastRun()], 'Deleted run-0.');
    expect(html).toContain('data-testid="chat-runs-notice"');
    expect(html).toContain('Deleted run-0.');
  });

  it('hides the pane when there are no runs', () => {
    expect(paneHtml([])).toBe('');
  });
});
