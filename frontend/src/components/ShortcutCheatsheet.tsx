import { APP_SHORTCUTS } from '../shortcuts';

// Visible hint bar for the app-level shortcuts (#808): one quiet
// line under the tab navs so keyboard users can discover the keys.
export function ShortcutHint() {
  return (
    <p className="row-meta" data-testid="shortcut-hint">
      Press <kbd>?</kbd> for keyboard shortcuts
    </p>
  );
}

// Cheatsheet overlay: rows render from APP_SHORTCUTS, so the list
// can never drift from the handler. Plain semantic markup reusing
// the app's row/action classes (no custom styling). Inline like the
// report dialog: no focus trap, Escape closes via the app handler,
// and focus lands on Close for keyboard users.
export function ShortcutCheatsheet({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  if (!open) return null;
  return (
    <section
      className="detail"
      role="dialog"
      aria-label="Keyboard shortcuts"
      data-testid="shortcut-cheatsheet"
    >
      <span className="row-title">Keyboard shortcuts</span>
      <dl>
        {APP_SHORTCUTS.map((s) => (
          <div key={s.key} data-testid="shortcut-row">
            <dt>
              <kbd>{s.key}</kbd>
            </dt>
            <dd>{s.label}</dd>
          </div>
        ))}
      </dl>
      <div className="actions">
        <button type="button" data-testid="shortcut-cheatsheet-close" onClick={onClose} autoFocus>
          Close
        </button>
      </div>
    </section>
  );
}
