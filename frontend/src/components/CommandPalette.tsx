import { useEffect, useMemo, useRef, useState } from 'react';
import { commandSection, filterCommands, type PaletteCommand } from './commandRegistry';

// The palette chord (Ctrl+K / Cmd+K) lives in shortcuts.ts beside
// every other app chord (#902): one table, one listener, so the
// chord can never double-fire. This module owns only the dialog.

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

// Section headers over the flat filtered list (issue #839): a
// header row opens each contiguous section run; item rows keep
// their filtered-list index so the cursor math is untouched.
export type PaletteListRow =
  | { kind: 'header'; key: string; section: string }
  | { kind: 'item'; cmd: PaletteCommand; index: number };

export function groupedRows(filtered: PaletteCommand[]): PaletteListRow[] {
  const rows: PaletteListRow[] = [];
  let lastSection = '';
  let headers = 0;
  filtered.forEach((cmd, index) => {
    const section = commandSection(cmd);
    if (section !== lastSection) {
      lastSection = section;
      headers += 1;
      rows.push({ kind: 'header', key: `h${headers}-${section}`, section });
    }
    rows.push({ kind: 'item', cmd, index });
  });
  return rows;
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
            {groupedRows(filtered).map((row) =>
              row.kind === 'header' ? (
                <li
                  key={row.key}
                  role="presentation"
                  data-testid="palette-section"
                  className="palette-section"
                  aria-hidden="true"
                >
                  {row.section}
                </li>
              ) : (
                <li
                  key={row.cmd.id}
                  id={`palette-item-${row.cmd.id}`}
                  role="option"
                  aria-selected={row.index === cursor}
                  data-testid={`palette-item-${row.cmd.id}`}
                  className={`palette-item${row.index === cursor ? ' selected' : ''}`}
                  onMouseEnter={() => setCursor(row.index)}
                >
                  <button
                    type="button"
                    tabIndex={-1}
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => onRun(row.cmd)}
                  >
                    <span className="palette-label">{row.cmd.label}</span>
                    <span className="row-meta palette-hint">{row.cmd.hint}</span>
                  </button>
                </li>
              ),
            )}
          </ul>
        )}
        <p className="row-meta palette-foot">
          Up/Down move · Enter runs · Esc closes · &gt; actions · @ repos · # review · / chat
        </p>
      </div>
    </div>
  );
}
