import { useEffect, useRef, useState } from 'react';
import type { RepoAction, RepoActionResponse } from '../apiClient';

export interface RepoDiffFileData {
  path: string;
  status: string;
  staged: boolean;
  unstaged: boolean;
  additions: number;
  deletions: number;
}

export interface RepoDiffData {
  statusShort: string;
  statusTruncated: boolean;
  diffStat: string;
  diff: string;
  truncated: boolean;
  files: RepoDiffFileData[];
  filesTruncated: boolean;
}

export interface FileDiff {
  path: string;
  body: string;
}

function diffHeaderPath(header: string): string {
  const rest = header.replace(/^diff --git /, '');
  const m = rest.match(/^(?:"a\/.*?"|a\/\S+)\s+(?:"b\/(.*?)"|b\/(\S+))\s*$/);
  if (m) return (m[1] ?? m[2] ?? '').trim() || rest;
  return rest;
}

// Split a unified diff blob into per-file chunks on diff --git
// headers. Pure: unit-tested, no DOM.
export function splitDiffByFile(diff: string): FileDiff[] {
  const out: FileDiff[] = [];
  let cur: string[] | null = null;
  for (const line of diff.split('\n')) {
    if (line.startsWith('diff --git ')) {
      if (cur) out.push({ path: diffHeaderPath(cur[0]), body: cur.join('\n') });
      cur = [line];
    } else if (cur) {
      cur.push(line);
    }
  }
  if (cur) out.push({ path: diffHeaderPath(cur[0]), body: cur.join('\n') });
  return out;
}

export type DiffLineKind = 'add' | 'del' | 'hunk' | 'meta' | 'ctx';

const META_PREFIXES = [
  'diff --git ',
  'index ',
  '--- ',
  '+++ ',
  'new file',
  'deleted file',
  'rename ',
  'similarity ',
  'dissimilarity ',
  'Binary files',
  '\\ No newline',
];

// Classify one unified-diff line for coloring (issue #942): the
// glyph alone must never be the only signal.
export function diffLineKind(line: string): DiffLineKind {
  if (line.startsWith('@@')) return 'hunk';
  if (META_PREFIXES.some((p) => line.startsWith(p))) return 'meta';
  if (line.startsWith('+')) return 'add';
  if (line.startsWith('-')) return 'del';
  return 'ctx';
}

// Human label for a porcelain status code (issue #942): the file
// list speaks words, not `M` / `??` glyphs.
export function fileStatusLabel(f: { status: string }): string {
  if (f.status === '??') return 'Untracked';
  if (f.status.includes('A')) return 'Added';
  if (f.status.includes('D')) return 'Deleted';
  if (f.status.includes('R')) return 'Renamed';
  if (f.status.includes('C')) return 'Copied';
  return 'Modified';
}

function fileStatusTone(f: { status: string }): string {
  const label = fileStatusLabel(f);
  if (label === 'Untracked') return 'untracked';
  if (label === 'Added') return 'add';
  if (label === 'Deleted') return 'del';
  return 'mod';
}

export interface DiffFileGroup {
  key: 'modified' | 'untracked';
  label: string;
  files: RepoDiffFileData[];
}

// Modified (tracked) first, then untracked; empty groups drop.
export function groupDiffFiles(files: RepoDiffFileData[]): DiffFileGroup[] {
  const modified = files.filter((f) => f.status !== '??');
  const untracked = files.filter((f) => f.status === '??');
  const groups: DiffFileGroup[] = [];
  if (modified.length > 0) groups.push({ key: 'modified', label: 'Modified', files: modified });
  if (untracked.length > 0) groups.push({ key: 'untracked', label: 'Untracked', files: untracked });
  return groups;
}

// Next changed-line position after `current`, wrapping to the
// first change. -1 when there are no changes at all.
export function nextChangeIndex(changes: number[], current: number): number {
  if (changes.length === 0) return -1;
  for (const idx of changes) {
    if (idx > current) return idx;
  }
  return changes[0];
}

// Initial per-file render cap (issue #942 long-diff scope): a 20+
// file diff opens collapsed and capped, never as a wall of text.
export const DIFF_PREVIEW_LINES = 120;
const DIFF_MORE_LINES = 240;

// One colored diff body: line-capped with an explicit show-more,
// a next-change jump, and a first-change scroll when activated
// (its file section opens), so changed lines are visible by
// default instead of hiding below the fold.
function DiffBody({
  body,
  active,
  testId,
}: {
  body: string;
  active: boolean;
  testId?: string;
}) {
  const [shown, setShown] = useState(DIFF_PREVIEW_LINES);
  const [cursorLine, setCursorLine] = useState(-1);
  const bodyRef = useRef<HTMLDivElement>(null);
  const lines = body.split('\n');
  const visible = lines.slice(0, shown);
  const hidden = lines.length - visible.length;
  const changes: number[] = [];
  visible.forEach((line, i) => {
    const kind = diffLineKind(line);
    if (kind === 'add' || kind === 'del') changes.push(i);
  });

  // Opening a file scrolls its body to the first changed line.
  useEffect(() => {
    if (!active) return;
    const box = bodyRef.current;
    if (!box || changes.length === 0) return;
    const el = box.children[changes[0]] as HTMLElement | undefined;
    if (el) box.scrollTop = el.offsetTop - box.offsetTop;
    // Only the open transition scrolls; body identity is stable.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, body]);

  function goNextChange() {
    const next = nextChangeIndex(changes, cursorLine);
    if (next < 0) return;
    setCursorLine(next);
    const box = bodyRef.current;
    const el = box?.children[next] as HTMLElement | undefined;
    if (box && el) box.scrollTop = el.offsetTop - box.offsetTop;
  }

  return (
    <>
      <div className="diff-body-wrap">
        <div className="diff-body" ref={bodyRef} data-testid={testId}>
          {visible.map((line, i) => {
            const kind = diffLineKind(line);
            const text = line === '' ? ' ' : line;
            if (kind === 'hunk') {
              // Full context rides the title; the row itself
              // ellipsizes instead of cutting mid-word (#942).
              return (
                <div key={i} className="diff-line diff-hunk" title={line}>
                  {text}
                </div>
              );
            }
            return (
              <div
                key={i}
                className={`diff-line diff-${kind}${i === cursorLine ? ' diff-current' : ''}`}
              >
                {text}
              </div>
            );
          })}
        </div>
        {lines.length > 20 && <div className="diff-fade" aria-hidden="true" />}
      </div>
      {(hidden > 0 || changes.length > 1) && (
        <div className="actions">
          {hidden > 0 && (
            <button
              type="button"
              data-testid="repo-diff-show-more"
              onClick={() => setShown((n) => n + DIFF_MORE_LINES)}
            >
              Show more ({hidden} more lines below)
            </button>
          )}
          {changes.length > 1 && (
            <button type="button" data-testid="repo-diff-next-change" onClick={goNextChange}>
              Next change ({changes.length} in view)
            </button>
          )}
        </div>
      )}
    </>
  );
}

function stageNote(f: RepoDiffFileData): string {
  if (f.staged && f.unstaged) return 'staged + unstaged';
  if (f.staged) return 'staged';
  return '';
}

interface DiffSection {
  path: string;
  body: string | null;
  file?: RepoDiffFileData;
}

// Status + diff for one dirty checkout with one-click git actions
// (issues #663, #927, #942). A real modal: overlay, dialog role,
// focus trap, and Escape to close. Per-file diffs start collapsed
// behind a grouped, click-to-jump file list.
export default function RepoDiffModal({
  repo,
  branch,
  data,
  error,
  onAction,
  onAskAgent,
  onClose,
}: {
  repo: string;
  branch: string;
  data: RepoDiffData | null;
  error: string | null;
  onAction: (action: RepoAction) => Promise<RepoActionResponse>;
  onAskAgent: (seed: string) => void;
  onClose: () => void;
}) {
  const [busy, setBusy] = useState<RepoAction | null>(null);
  const [result, setResult] = useState<{ ok: boolean; detail: string } | null>(null);
  const [discardArmed, setDiscardArmed] = useState(false);
  const [syncArmed, setSyncArmed] = useState(false);
  const [openFiles, setOpenFiles] = useState<Set<string>>(() => new Set());
  const [filter, setFilter] = useState('');
  const panelRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  // Modal chrome (issue #942): focus lands on Close, Tab cycles
  // inside the panel, Escape closes, and focus returns to the
  // control that opened the modal on unmount. The open modal also
  // locks page scroll (#1018) so the page behind stays put.
  useEffect(() => {
    const prev = typeof document === 'undefined' ? null : (document.activeElement as HTMLElement | null);
    const prevOverflow = typeof document === 'undefined' ? '' : document.body.style.overflow;
    if (typeof document !== 'undefined') document.body.style.overflow = 'hidden';
    closeRef.current?.focus();
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        e.preventDefault();
        onCloseRef.current();
        return;
      }
      if (e.key !== 'Tab') return;
      const panel = panelRef.current;
      if (!panel) return;
      const items = Array.from(
        panel.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary',
        ),
      ).filter((el) => el.offsetParent !== null || el === document.activeElement);
      if (items.length === 0) return;
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (e.shiftKey && (active === first || !panel.contains(active))) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      } else if (!panel.contains(active)) {
        e.preventDefault();
        first.focus();
      }
    }
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('keydown', onKey);
      if (typeof document !== 'undefined') document.body.style.overflow = prevOverflow;
      prev?.focus?.();
    };
  }, []);

  async function run(action: RepoAction) {
    if (busy) return;
    setDiscardArmed(false);
    setSyncArmed(false);
    setBusy(action);
    setResult(null);
    try {
      const r = await onAction(action);
      setResult({ ok: r.ok, detail: r.detail });
    } catch (err) {
      setResult({ ok: false, detail: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(null);
    }
  }

  function tapDiscard() {
    if (discardArmed) {
      void run('discard-untracked');
    } else {
      setSyncArmed(false);
      setDiscardArmed(true);
    }
  }

  function tapSync() {
    if (syncArmed) {
      void run('sync');
    } else {
      setDiscardArmed(false);
      setSyncArmed(true);
    }
  }

  const perFile = data ? splitDiffByFile(data.diff) : [];
  const counts = new Map(data?.files.map((f) => [f.path, f]) ?? []);
  const chunkPaths = new Set(perFile.map((f) => f.path));
  const sections: DiffSection[] = perFile.map((f) => ({
    path: f.path,
    body: f.body,
    file: counts.get(f.path),
  }));
  for (const f of data?.files ?? []) {
    if (!chunkPaths.has(f.path)) sections.push({ path: f.path, body: null, file: f });
  }
  const sectionIdByPath = new Map(sections.map((s, i) => [s.path, `repo-diff-section-${i}`]));
  // Single filterable file list (#1018): the query narrows both the
  // jump chips and the per-file stack, so a 19-file dialog collapses
  // to what matches. Jump chips stay for small lists (navigation
  // without typing); large unfiltered lists show sections only, so
  // file names no longer render twice.
  const q = filter.trim().toLowerCase();
  const matches = (p: string) =>
    !q || p.toLowerCase().includes(q);
  const visibleSections = sections.filter((s) => matches(s.path));
  const jumpFiles = (data?.files ?? []).filter((f) => matches(f.path));
  const groups = groupDiffFiles(
    q || (data?.files.length ?? 0) <= 8 ? jumpFiles : []);
  const actionsOff = !data || !!error || busy !== null;

  function jumpToFile(path: string) {
    setOpenFiles((prev) => new Set(prev).add(path));
    const id = sectionIdByPath.get(path);
    if (!id || typeof document === 'undefined') return;
    window.setTimeout(() => {
      document.getElementById(id)?.scrollIntoView({ block: 'start' });
    }, 0);
  }

  return (
    <div
      className="diff-overlay"
      data-testid="repo-diff-overlay"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <section
        ref={panelRef}
        className="detail diff-modal"
        role="dialog"
        aria-modal="true"
        data-testid="repo-diff-modal"
        aria-label={`Diff for ${repo}`}
      >
        <span className="row-title">
          Diff · {repo} @ {branch}
        </span>
        {error && (
          <p role="alert" data-testid="repo-diff-error">
            Couldn&apos;t load the diff: {error}
          </p>
        )}
        {!error && !data && (
          <p className="row-meta" role="status">
            Loading diff…
          </p>
        )}
        {!error && data && (
          <>
            <p className="row-meta">
              Changed files (
              {q
                ? `${visibleSections.length} of ${data.files.length}`
                : data.files.length}
              {data.filesTruncated ? '+, showing first' : ''})
            </p>
            <p className="row-meta">
              <label htmlFor="repo-diff-filter">Filter files</label>{' '}
              <input
                id="repo-diff-filter"
                data-testid="repo-diff-filter"
                type="search"
                placeholder="e.g. inbox"
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
              />
            </p>
            {data.files.length > 0 && groups.length > 0 ? (
              <div data-testid="repo-diff-files">
                {groups.map((g) => (
                  <div key={g.key} data-testid={`repo-diff-group-${g.key}`}>
                    <p className="row-meta diff-group-label">
                      {g.label} ({g.files.length})
                    </p>
                    <ul className="diff-file-list">
                      {g.files.map((f) => (
                        <li key={f.path}>
                          <button
                            type="button"
                            className="diff-file-jump"
                            data-testid="repo-diff-file-jump"
                            onClick={() => jumpToFile(f.path)}
                          >
                            <span className={`diff-badge diff-badge-${fileStatusTone(f)}`}>
                              {fileStatusLabel(f)}
                            </span>{' '}
                            {f.path}
                            {(f.additions > 0 || f.deletions > 0) && (
                              <span>
                                {' '}
                                +{f.additions}/-{f.deletions}
                              </span>
                            )}
                            {stageNote(f) && <span> ({stageNote(f)})</span>}
                          </button>
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
              </div>
            ) : data.files.length === 0 ? (
              <pre className="row-meta" data-testid="repo-diff-status">
                (clean)
              </pre>
            ) : null}
            {data.files.length > 0 && (
              <details data-testid="repo-diff-raw-wrap">
                <summary className="row-meta">
                  Raw status{data.statusTruncated ? ' (truncated)' : ''}
                </summary>
                <pre className="row-meta" data-testid="repo-diff-status">
                  {data.statusShort || '(clean)'}
                </pre>
                {data.diffStat && (
                  <pre className="row-meta" data-testid="repo-diff-stat">
                    {data.diffStat}
                  </pre>
                )}
              </details>
            )}
            {visibleSections.length > 0 && (
              <>
                <p className="row-meta">Per-file diffs</p>
                <div className="actions">
                  <button
                    type="button"
                    data-testid="repo-diff-expand-all"
                    onClick={() => setOpenFiles(
                      new Set(visibleSections.map((s) => s.path)))}
                  >
                    Expand all
                  </button>
                  <button
                    type="button"
                    data-testid="repo-diff-collapse-all"
                    onClick={() => setOpenFiles(new Set())}
                  >
                    Collapse all
                  </button>
                </div>
                <div data-testid="repo-diff-per-file">
                  {visibleSections.map((s) => {
                    const hit = s.file;
                    const countNote =
                      hit && (hit.additions > 0 || hit.deletions > 0)
                        ? ` (+${hit.additions}/-${hit.deletions})`
                        : '';
                    const open = openFiles.has(s.path);
                    return (
                      <details
                        key={s.path}
                        id={sectionIdByPath.get(s.path)}
                        data-testid="repo-diff-file"
                        open={open}
                        onToggle={(e) => {
                          const isOpen = e.currentTarget.open;
                          setOpenFiles((prev) => {
                            if (isOpen === prev.has(s.path)) return prev;
                            const next = new Set(prev);
                            if (isOpen) next.add(s.path);
                            else next.delete(s.path);
                            return next;
                          });
                        }}
                      >
                        <summary className="row-meta diff-file-summary">
                          {hit && (
                            <span className={`diff-badge diff-badge-${fileStatusTone(hit)}`}>
                              {fileStatusLabel(hit)}
                            </span>
                          )}{' '}
                          {s.path}
                          {countNote}
                        </summary>
                        {s.body !== null ? (
                          <DiffBody body={s.body} active={open} />
                        ) : (
                          <p className="row-meta">
                            {hit?.status === '??'
                              ? 'Untracked file — no diff until it is added.'
                              : 'No diff chunk for this file (the full diff was truncated).'}
                          </p>
                        )}
                      </details>
                    );
                  })}
                </div>
              </>
            )}
            {data.diff && (
              <details data-testid="repo-diff-full-wrap">
                <summary className="row-meta">
                  Full diff{data.truncated ? ' (truncated)' : ''}
                </summary>
                <DiffBody body={data.diff} active={false} testId="repo-diff-full" />
              </details>
            )}
            <div className="actions">
              <button
                type="button"
                data-testid="repo-diff-stash"
                disabled={actionsOff}
                onClick={() => void run('stash')}
              >
                {busy === 'stash' ? 'Stashing…' : 'Stash changes'}
              </button>
              <button
                type="button"
                className={`btn-danger${discardArmed ? ' armed' : ''}`}
                data-testid="repo-diff-discard"
                disabled={actionsOff}
                onClick={tapDiscard}
              >
                {busy === 'discard-untracked'
                  ? 'Discarding…'
                  : discardArmed
                    ? 'Tap again to confirm discard (deletes untracked files)'
                    : 'Discard untracked'}
              </button>
              <button
                type="button"
                data-testid="repo-diff-agent"
                disabled={actionsOff}
                onClick={() =>
                  onAskAgent(
                    `In ${repo}, review git status and diff, commit the current changes with a clear message, and push to origin.`,
                  )
                }
              >
                Commit &amp; Push via Chat agent
              </button>
              <button
                type="button"
                className={`btn-danger${syncArmed ? ' armed' : ''}`}
                data-testid="repo-diff-sync"
                disabled={actionsOff}
                onClick={tapSync}
              >
                {busy === 'sync'
                  ? 'Syncing…'
                  : syncArmed
                    ? 'Tap again to confirm rebase onto origin/main'
                    : 'Fetch & Rebase onto origin/main'}
              </button>
            </div>
            <p className="row-meta" data-testid="repo-diff-agent-hint">
              The Chat agent writes the commit message from the diff, commits, and pushes to
              origin. Discard permanently deletes untracked files; rebase rewrites local commits
              onto origin/main.
            </p>
            {result && (
              <p
                role={result.ok ? 'status' : 'alert'}
                data-testid="repo-diff-action-result"
              >
                {result.detail}
              </p>
            )}
          </>
        )}
        <div className="actions">
          <button type="button" ref={closeRef} data-testid="repo-diff-close" onClick={onClose}>
            Close
          </button>
        </div>
      </section>
    </div>
  );
}
