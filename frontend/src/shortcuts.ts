import { useEffect, useRef } from 'react';
import { isEditableFocus, type Focusable } from './triageKeys';
import { isKeyboardSend } from './views/ChatView';

// App-level keyboard shortcuts (#808): tab switches, chat send, the
// cheatsheet (?), and the palette chord (Ctrl+K, #902: the cheatsheet
// yields — one shortcut, one surface). Matching is pure (struct fakes,
// no DOM) so the suite covers it without a browser; useAppShortcuts
// is thin wiring in the triageKeys style.
export interface KeyPress {
  key: string;
  ctrlKey: boolean;
  metaKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
}

export type AppTab = 'now' | 'chat' | 'briefing' | 'history';

export type AppShortcutAction = AppTab | 'send' | 'help' | 'help-close' | 'palette';

export interface AppShortcutDef {
  key: string;
  label: string;
  action: AppShortcutAction;
}

// ONE table: the cheatsheet renders from this, so adding a shortcut
// is one entry + one matcher case + test.
export const APP_SHORTCUTS: AppShortcutDef[] = [
  { key: '1', label: 'Go to Now', action: 'now' },
  { key: '2', label: 'Go to Chat', action: 'chat' },
  { key: '3', label: 'Go to Briefing', action: 'briefing' },
  { key: '4', label: 'Go to History', action: 'history' },
  { key: 'Ctrl+Enter', label: 'Send chat message (chat tab)', action: 'send' },
  { key: '?', label: 'Toggle this cheatsheet', action: 'help' },
  { key: 'Ctrl+K', label: 'Open command palette', action: 'palette' },
  { key: 'Esc', label: 'Close cheatsheet', action: 'help-close' },
];

export interface ShortcutState {
  // Focus sits in an editable control: bare keys must never hijack
  // typing. Ctrl+K still fires (the palette owns that chord from
  // anywhere, #902); Ctrl+Enter is owned by the focused control (the
  // composer already sends on it), so the app level skips it rather
  // than double-sending.
  typing: boolean;
  // A modal/drawer owns the screen: tabs and send stay out of the
  // way, but the cheatsheet and the palette remain reachable.
  suspended: boolean;
  helpOpen: boolean;
}

export function matchAppShortcut(e: KeyPress, s: ShortcutState): AppShortcutAction | null {
  if (e.key === 'Escape') return s.helpOpen ? 'help-close' : null;
  const mod = e.ctrlKey || e.metaKey;
  // Exact 'k', no Shift: Ctrl+Shift+K opens devtools in Firefox and
  // must never be hijacked. Ctrl+K is the palette's chord alone
  // (#902); the cheatsheet answers to ? only.
  if (mod && !e.altKey && !e.shiftKey && e.key === 'k') return 'palette';
  if (isKeyboardSend(e)) return !s.typing && !s.suspended ? 'send' : null;
  if (mod || e.altKey) return null;
  if (s.typing) return null;
  // The cheatsheet stays reachable under a modal; tabs do not.
  if (e.key === '?') return 'help';
  if (s.suspended) return null;
  switch (e.key) {
    case '1':
      return 'now';
    case '2':
      return 'chat';
    case '3':
      return 'briefing';
    case '4':
      return 'history';
    default:
      return null;
  }
}

// Minimal structural shape so tests can pass fakes (no DOM needed).
export interface SendButtonDoc {
  querySelector(sel: string): { disabled: boolean; click(): void } | null;
}

// App-level send: clicks the chat Send button. No-op (false) when the
// chat tab is not mounted or the button is disabled (empty composer,
// streaming) — never a dead or double send.
export function clickChatSend(
  doc?: SendButtonDoc | undefined,
): boolean {
  const root: SendButtonDoc | undefined =
    doc ?? (typeof document === 'undefined' ? undefined : (document as unknown as SendButtonDoc));
  const btn = root?.querySelector('[data-testid="chat-send"]') ?? null;
  if (!btn || btn.disabled) return false;
  btn.click();
  return true;
}

export function useAppShortcuts(opts: {
  suspended: boolean;
  helpOpen: boolean;
  onTab: (t: AppTab) => void;
  onSend: () => void;
  onToggleHelp: () => void;
  onCloseHelp: () => void;
  onPalette: () => void;
}): void {
  const ref = useRef(opts);
  ref.current = opts;
  const suspendedRef = useRef(opts.suspended);
  suspendedRef.current = opts.suspended;
  const helpOpenRef = useRef(opts.helpOpen);
  helpOpenRef.current = opts.helpOpen;

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const o = ref.current;
      const active = typeof document === 'undefined' ? null : document.activeElement;
      const action = matchAppShortcut(
        {
          key: e.key,
          ctrlKey: e.ctrlKey,
          metaKey: e.metaKey,
          altKey: e.altKey,
          shiftKey: e.shiftKey,
        },
        {
          typing: isEditableFocus(active as Focusable | null),
          suspended: suspendedRef.current,
          helpOpen: helpOpenRef.current,
        },
      );
      if (!action) return;
      e.preventDefault();
      if (action === 'send') o.onSend();
      else if (action === 'help') o.onToggleHelp();
      else if (action === 'help-close') o.onCloseHelp();
      else if (action === 'palette') o.onPalette();
      else o.onTab(action);
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
    // Refs mirror every option; the listener attaches once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}
