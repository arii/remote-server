import { useEffect, useMemo, useRef, useState } from 'react';
import { filterCommands, type PaletteCommand } from './commandRegistry';

// Ctrl+K / Cmd+K, from anywhere including inside inputs: the chord
// never collides with plain typing, so no editable-focus guard.
export function isPaletteHotkey(e: {
  key: string;
  ctrlKey: boolean;
  metaKey: boolean;
  altKey?: boolean;
}): boolean {
  return (e.key === 'k' || e.key === 'K') && (e.ctrlKey || e.metaKey) && !e.altKey;
}

// Wrap-around cursor for ArrowUp/ArrowDown; null means "no rows".
export function movePaletteSelection(
  cursor: number | null,
  dir: 1 | -1,
  count: number,
): number | null {
  if (count <= 0) return null;
  if (cursor === null) return dir === 1 ? 0 : count - 1;
  return (cursor + dir + count) % count;
}

// Fuzzy command palette: an input over the registry, ArrowUp/Down to
// move, Enter to run, Esc or overlay click to close. Focus stays in
// the input (rows are pointer-only, tabIndex -1) so triage keys, which
// skip editable focus, never fire while the palette is open.
export default function CommandPalette({
  open,
  commands,
  onClose,
  onRun,
}: {
  open: boolean;
  commands: PaletteCommand[];
  onClose: () => void;
  onRun: (cmd: PaletteCommand) => void;
}) {
  const [query, setQuery] = useState('');
  const [cursor, setCursor] = useState<number | null>(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const filtered = useMemo(() => filterCommands(commands, query), [commands, query]);

  // Fresh query per open; focus the input so typing filters at once.
  useEffect(() => {
    if (!open) return;
    setQuery('');
    setCursor(0);
    const t = window.setTimeout(() => inputRef.current?.focus(), 0);
    return () => window.clearTimeout(t);
  }, [open]);

  // Clamp the cursor when the filter shrinks the list.
  useEffect(() => {
    setCursor((c) => {
      if (filtered.length === 0) return null;
      if (c === null || c >= filtered.length) return 0;
      return c;
    });
  }, [filtered.length]);

  if (!open) return null;

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Escape') {
      e.preventDefault();
      onClose();
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      setCursor((c) => movePaletteSelection(c, 1, filtered.length));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setCursor((c) => movePaletteSelection(c, -1, filtered.length));
    } else if (e.key === 'Enter') {
      const cmd = cursor !== null ? filtered[cursor] : undefined;
      if (cmd) {
        e.preventDefault();
        onRun(cmd);
      }
    }
  }

  const activeId = cursor !== null && filtered[cursor] ? `palette-item-${filtered[cursor].id}` : undefined;

  return (
    <div
      className="palette-overlay"
      data-testid="palette-overlay"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className="palette"
        role="dialog"
        aria-label="Command palette"
        data-testid="command-palette"
        onKeyDown={onKeyDown}
      >
        <input
          ref={inputRef}
          className="palette-input"
          data-testid="palette-input"
          role="combobox"
          autoFocus
          aria-label="Command search"
          aria-expanded={filtered.length > 0}
          aria-controls="palette-list"
          aria-activedescendant={activeId}
          aria-autocomplete="list"
          autoComplete="off"
          placeholder="Type a command…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        {filtered.length === 0 ? (
          <p className="row-meta palette-empty" data-testid="palette-empty" role="status">
            No matching commands.
          </p>
        ) : (
          <ul className="palette-list" id="palette-list" role="listbox" data-testid="palette-list">
            {filtered.map((cmd, i) => (
              <li
                key={cmd.id}
                id={`palette-item-${cmd.id}`}
                role="option"
                aria-selected={i === cursor}
                data-testid={`palette-item-${cmd.id}`}
                className={`palette-item${i === cursor ? ' selected' : ''}`}
                onMouseEnter={() => setCursor(i)}
              >
                <button
                  type="button"
                  tabIndex={-1}
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => onRun(cmd)}
                >
                  <span className="palette-label">{cmd.label}</span>
                  <span className="row-meta palette-hint">{cmd.hint}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
        <p className="row-meta palette-foot">Up/Down move · Enter runs · Esc closes</p>
      </div>
    </div>
  );
}
