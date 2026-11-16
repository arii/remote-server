// @vitest-environment happy-dom
import { act, StrictMode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import ChatView from './ChatView';
import { postChatStream } from '../apiClient';

vi.mock('./AgentDrawer', () => ({ default: () => null }));
vi.mock('../apiClient', async (original) => ({
  ...await original<typeof import('../apiClient')>(),
  postChatStream: vi.fn(),
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
  vi.mocked(postChatStream).mockResolvedValue(new Response('data: {"token":"hello"}\n\n', { status: 200 }));
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

it('sends an explicit Now handoff exactly once in StrictMode (#1317)', async () => {
  const used = vi.fn();
  await act(async () => root.render(<StrictMode><ChatView repo="r" seed="Investigate" sendSeed onSeedUsed={used} /></StrictMode>));
  expect(postChatStream).toHaveBeenCalledTimes(1);
  expect(vi.mocked(postChatStream).mock.calls[0].slice(0, 2)).toEqual(['Investigate', 'r']);
  expect(used).toHaveBeenCalledTimes(1);
  expect(host.textContent).toContain('Investigate');
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
