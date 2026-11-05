import { useEffect, useRef, useState } from 'react';

// One-line binding hint shown where triage happens (Review, Jobs).
export const TRIAGE_HINT = 'j/k move · Enter opens';

// Selection-index reducer: move the keyboard cursor one step, clamped to
// the list. A null cursor means "nowhere yet": j lands on the first row,
// k on the last.
export function moveSelection(
  cursor: number | null,
  dir: 1 | -1,
  count: number,
): number | null {
  if (count <= 0) return null;
  if (cursor === null) return dir === 1 ? 0 : count - 1;
  const next = cursor + dir;
  if (next < 0) return 0;
  if (next >= count) return count - 1;
  return next;
}

// Minimal structural shape so tests can pass fakes (no DOM needed).
export interface Focusable {
  tagName?: string;
  isContentEditable?: boolean;
}

// True when keyboard focus sits in an editable control: triage keys must
// never hijack typing, including inside the agent drawer, a disclosure
// form, or any modal.
export function isEditableFocus(el: Focusable | null | undefined): boolean {
  if (!el) return false;
  if (el.isContentEditable) return true;
  const tag = (el.tagName || '').toUpperCase();
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
}

// True when Enter already has a native target (button, link, summary):
// native activation wins and the triage cursor stays out of the way.
export function isActivationFocus(el: Focusable | null | undefined): boolean {
  if (isEditableFocus(el)) return true;
  const tag = ((el && el.tagName) || '').toUpperCase();
  return tag === 'BUTTON' || tag === 'A' || tag === 'SUMMARY';
}

export interface TriageKeysOptions {
  count: number;
  // Drawer-open / modal-editing flag from the parent: while true, no
  // triage key is handled (agent drawer, PTY terminal, modal flows).
  suspended?: boolean;
  onOpen: (index: number) => void;
  // Optional defer binding (Review only): d defers the cursor row.
  // Reversible by design, so it needs no hold-to-confirm.
  onDefer?: (index: number) => void;
  // Optional agent binding: c seeds the agent from the cursor row.
  onAgent?: (index: number) => void;
  // Optional row locator for scrolling the cursor into view.
  rowTestId?: (index: number) => string;
}

export type TriageAction = 'open' | 'defer' | 'agent';

// Letter-key triage (#962): o opens like Enter, D defers like d,
// c asks the agent. Pure so the matrix stays unit-tested; the hook
// below only adds focus guards and cursor bounds.
export function resolveTriageKey(
  key: string,
  wired: { defer: boolean; agent: boolean },
): TriageAction | null {
  if (key === 'o' || key === 'O') return 'open';
  if (key === 'd' || key === 'D') return wired.defer ? 'defer' : null;
  if (key === 'c' || key === 'C') return wired.agent ? 'agent' : null;
  return null;
}

// j/k moves a cursor over `count` rows, Enter opens the cursor row,
// d defers it when onDefer is wired (Review only).
// Wiring only (no destructive bindings: hold-to-confirm stays pointer
// or focused-button+Enter). Returns [cursor, setCursor].
export function useTriageKeys({
  count,
  suspended = false,
  onOpen,
  onDefer,
  onAgent,
  rowTestId,
}: TriageKeysOptions): [number | null, (c: number | null) => void] {
  const [cursor, setCursor] = useState<number | null>(null);
  const openRef = useRef(onOpen);
  openRef.current = onOpen;
  const deferRef = useRef(onDefer);
  deferRef.current = onDefer;
  const agentRef = useRef(onAgent);
  agentRef.current = onAgent;
  const cursorRef = useRef(cursor);
  cursorRef.current = cursor;
  const countRef = useRef(count);
  countRef.current = count;
  const rowTestIdRef = useRef(rowTestId);
  rowTestIdRef.current = rowTestId;

  // Clamp a stale cursor when the list shrinks or reloads.
  useEffect(() => {
    if (cursor !== null && cursor >= count) {
      setCursor(count > 0 ? count - 1 : null);
    }
  }, [count, cursor]);

  // Keep the cursor row visible (nearest scroll, no page jumps).
  useEffect(() => {
    const resolve = rowTestIdRef.current;
    if (cursor === null || !resolve || typeof document === 'undefined') return;
    try {
      document
        .querySelector(`[data-testid="${resolve(cursor)}"]`)
        ?.scrollIntoView({ block: 'nearest' });
    } catch {
      // Non-essential affordance; never break triage on scroll failure.
    }
  }, [cursor]);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (suspended || e.metaKey || e.ctrlKey || e.altKey) return;
      const active = typeof document === 'undefined' ? null : document.activeElement;
      if (e.key === 'j' || e.key === 'k') {
        if (isEditableFocus(active)) return;
        const next = moveSelection(
          cursorRef.current,
          e.key === 'j' ? 1 : -1,
          countRef.current,
        );
        if (next !== null) {
          e.preventDefault();
          setCursor(next);
        }
        return;
      }
      if (e.key === 'Enter') {
        if (isActivationFocus(active)) return;
        const at = cursorRef.current;
        if (at === null || at >= countRef.current) return;
        e.preventDefault();
        openRef.current(at);
        return;
      }
      if (
        e.key === 'd' ||
        e.key === 'D' ||
        e.key === 'o' ||
        e.key === 'O' ||
        e.key === 'c' ||
        e.key === 'C'
      ) {
        // Same guards as j/k: no modifiers (checked above), suspended
        // flag, and never while typing in an editable control.
        if (isEditableFocus(active)) return;
        const action = resolveTriageKey(e.key, {
          defer: !!deferRef.current,
          agent: !!agentRef.current,
        });
        const at = cursorRef.current;
        if (!action || at === null || at >= countRef.current) return;
        e.preventDefault();
        if (action === 'open') openRef.current(at);
        else if (action === 'defer') deferRef.current?.(at);
        else agentRef.current?.(at);
      }
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [suspended]);

  return [cursor, setCursor];
}
