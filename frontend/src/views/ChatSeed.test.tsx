// @vitest-environment happy-dom
import { act, StrictMode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import ChatView, { saveChatMessages, saveChatSessionId } from './ChatView';
import { fetchChatSessionStatus, postChatStream, reattachChatStream } from '../apiClient';

vi.mock('./AgentDrawer', () => ({ default: () => null }));
vi.mock('../apiClient', async (original) => ({
  ...await original<typeof import('../apiClient')>(),
  postChatStream: vi.fn(),
  fetchChatSessionStatus: vi.fn(),
  reattachChatStream: vi.fn(),
  fetchAgentRuns: vi.fn().mockResolvedValue({ runs: [] }),
  fetchBudget: vi.fn().mockRejectedValue(new Error('Budget unavailable')),
  fetchRepoMetrics: vi.fn().mockRejectedValue(new Error('Metrics unavailable')),
  fetchRouteHint: vi.fn().mockRejectedValue(new Error('Hint unavailable')),
}));
let root: Root;
let host: HTMLDivElement;
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  localStorage.clear();
  sessionStorage.clear();
  host = document.createElement('div');
  document.body.append(host);
  root = createRoot(host);
  vi.mocked(fetchChatSessionStatus).mockResolvedValue({
    ok: true, session_id: 'stored', state: 'done', text: 'Previous answer', phase: '', elapsed: 0,
  });
  vi.mocked(postChatStream).mockResolvedValue(new Response('event: done\ndata: {"text":"New answer"}\n\n'));
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

it('sends an explicit Now handoff exactly once in StrictMode (#1317)', async () => {
  const used = vi.fn();
  let finish!: () => void;
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      finish = () => {
        controller.enqueue(new TextEncoder().encode('event: token\ndata: {"token":"Completed reply"}\n\nevent: done\ndata: {"text":"Completed reply"}\n\n'));
        controller.close();
      };
    },
  });
  vi.mocked(postChatStream).mockResolvedValue(new Response(body));
  await act(async () => root.render(<StrictMode><ChatView repo="r" seed="Investigate" sendSeed onSeedUsed={used} /></StrictMode>));
  expect(postChatStream).toHaveBeenCalledTimes(1);
  expect(vi.mocked(postChatStream).mock.calls[0].slice(0, 2)).toEqual(['Investigate', 'r']);
  expect(used).toHaveBeenCalledTimes(1);
  expect(host.textContent).toContain('Investigate');
  expect(vi.mocked(postChatStream).mock.calls[0][4]!.aborted).toBe(false);
  await act(async () => host.querySelector<HTMLButtonElement>('[data-testid="chat-templates"]')!.click());
  expect(host.querySelector('[data-testid="chat-templates-panel"]')).not.toBeNull();
  expect(host.textContent!.match(/Reply streaming —/g)).toHaveLength(1);
  await act(async () => finish());
  expect(host.textContent).toContain('Completed reply');
  expect(host.textContent).not.toContain('Reply streaming —');
});

it('finishes a stored live turn before sending the Now seed exactly once in StrictMode', async () => {
  const used = vi.fn();
  saveChatSessionId('stored');
  saveChatMessages([{ role: 'user', text: 'Previous question' }]);
  vi.mocked(fetchChatSessionStatus).mockResolvedValue({
    ok: true, session_id: 'stored', state: 'live', text: '', phase: '', elapsed: 0,
  });
  let finish!: () => void;
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      finish = () => {
        controller.enqueue(new TextEncoder().encode('event: done\ndata: {"text":"Previous answer"}\n\n'));
        controller.close();
      };
    },
  });
  vi.mocked(reattachChatStream).mockResolvedValue(new Response(body));
  await act(async () => root.render(<StrictMode><ChatView repo="r" seed="Next question" sendSeed onSeedUsed={used} /></StrictMode>));
  expect(reattachChatStream).toHaveBeenCalledTimes(1);
  expect(postChatStream).not.toHaveBeenCalled();
  expect(used).not.toHaveBeenCalled();
  await act(async () => finish());
  expect(postChatStream).toHaveBeenCalledTimes(1);
  expect(vi.mocked(postChatStream).mock.calls[0].slice(0, 3)).toEqual(['Next question', 'r', 'stored']);
  expect(vi.mocked(postChatStream).mock.calls[0][4]!.aborted).toBe(false);
  expect(used).toHaveBeenCalledTimes(1);
  const history = host.querySelector('[data-testid="chat-history"]')!;
  expect(Array.from(history.querySelectorAll('li > p')).map((row) => row.textContent)).toEqual([
    'Previous question', 'Previous answer', 'Next question', 'New answer',
  ]);
});

it.each([false, true])('backfills a finished reply before sending, without duplication when already saved=%s', async (saved) => {
  saveChatSessionId('stored');
  saveChatMessages([
    { role: 'user', text: 'Previous question' },
    ...(saved ? [{ role: 'assistant' as const, text: 'Previous answer' }] : []),
  ]);
  await act(async () => root.render(<ChatView repo="r" seed="Next question" sendSeed onSeedUsed={() => {}} />));
  expect(fetchChatSessionStatus).toHaveBeenCalledWith('stored');
  expect(postChatStream).toHaveBeenCalledTimes(1);
  expect(vi.mocked(postChatStream).mock.calls[0][2]).toBe('stored');
  const history = host.querySelector('[data-testid="chat-history"]')!;
  expect(Array.from(history.querySelectorAll('li > p')).map((row) => row.textContent)).toEqual([
    'Previous question', 'Previous answer', 'Next question', 'New answer',
  ]);
});

it('still resumes stored conversations on an ordinary Chat mount', async () => {
  saveChatSessionId('stored');
  await act(async () => root.render(<ChatView repo="r" seed={null} onSeedUsed={() => {}} />));
  expect(fetchChatSessionStatus).toHaveBeenCalledWith('stored');
  expect(host.textContent).toContain('Previous answer');
  expect(postChatStream).not.toHaveBeenCalled();
});

it('stages legacy seeds without sending and shows the idle composer', async () => {
  await act(async () => root.render(<ChatView repo="r" seed="Edit first" onSeedUsed={() => {}} />));
  expect(host.querySelector<HTMLTextAreaElement>('[data-testid="chat-prompt"]')!.value).toBe('Edit first');
  expect(postChatStream).not.toHaveBeenCalled();
  expect(host.querySelector<HTMLDetailsElement>('[data-testid="chat-past-runs"]')!.open).toBe(false);
  expect(host.textContent).toContain('Past runs (0)');
});

it('does not send an empty handoff', async () => {
  await act(async () => root.render(<ChatView repo="r" seed={null} sendSeed onSeedUsed={() => {}} />));
  expect(postChatStream).not.toHaveBeenCalled();
  expect(host.querySelector('[data-testid="chat-send"]')).not.toBeNull();
});

it('surfaces a failed Now handoff in the existing Chat error state', async () => {
  vi.mocked(postChatStream).mockRejectedValue(new Error('Chat unavailable'));
  await act(async () => root.render(<ChatView repo="r" seed="Investigate" sendSeed onSeedUsed={() => {}} />));
  expect(host.textContent).toContain('Chat unavailable');
  expect(host.querySelector('[data-testid="chat-retry"]')).not.toBeNull();
});
