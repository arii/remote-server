import Dialog from './Dialog';
import { APP_SHORTCUTS } from '../shortcuts';

// Visible hint bar for the app-level shortcuts (#808): one quiet
// line under the tab navs so keyboard users can discover the keys.
export function ShortcutHint() {
  return (
    <p className="row-meta shortcut-hint" data-testid="shortcut-hint">
      Press <kbd>?</kbd> for keyboard shortcuts
    </p>
  );
}

// Cheatsheet overlay: rows render from APP_SHORTCUTS, so the list
// can never drift from the handler. Plain semantic markup reusing
// the app's row/action classes (no custom styling). Inline like the
// report dialog: no focus trap; Escape, outside-tap, and focus
// return live in the shared Dialog wrapper, and focus lands on
// Close for keyboard users.
export function ShortcutCheatsheet({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  return (
    <Dialog
      open={open}
      onClose={onClose}
      modal={false}
      label="Keyboard shortcuts"
      testId="shortcut-cheatsheet"
      className="detail"
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
    </Dialog>
  );
}
