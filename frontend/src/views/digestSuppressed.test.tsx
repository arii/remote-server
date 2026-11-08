// @vitest-environment happy-dom
import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, describe, expect, expectTypeOf, it } from 'vitest';
import type { DigestResults } from '../apiClient';
import { digestResultLine } from './BriefingView';

// Type-boundary regression (boomtick #1222 item 7): the backend
// answers {suppressed: reason} instead of per-channel results, and
// the old code could only read it through an as-unknown-as cast
// (BriefingView.tsx:302, test :403). DigestResults widens the type
// so the raw shape compiles cast-free — and the runtime line is
// unchanged. Real-DOM mounting follows Dialog.test.tsx: the
// suppressed line is asserted through textContent, not markup.

// Dialog.test.tsx mount harness: mount for real, restore after each.
let root: Root | null = null;
let host: HTMLDivElement | null = null;

function mount(ui: ReactElement) {
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => {
    root!.render(ui);
  });
}

afterEach(() => {
  act(() => {
    root?.unmount();
  });
  root = null;
  host?.remove();
  host = null;
});

describe('digest suppressed shape (boomtick #1222 item 7)', () => {
  it('DigestResults accepts the raw suppressed shape with no cast', () => {
    // This assignment is the regression test: pre-fix, results was
    // Record<string, DigestChannelResult> and this exact literal
    // needed `as unknown as` to compile.
    const raw: DigestResults = { suppressed: "event 'digest' muted" };
    expectTypeOf(raw).toMatchTypeOf<DigestResults>();
    expect(digestResultLine(raw, 'STAMP')).toBe(
      "Digest not sent — event 'digest' muted.",
    );
  });

  it('suppressed wins over per-channel entries and never reads as sent', () => {
    const mixed: DigestResults = {
      suppressed: 'quiet hours',
      ntfy: { ok: true, status: 200 },
    };
    const line = digestResultLine(mixed, 'STAMP');
    expect(line).toBe('Digest not sent — quiet hours.');
    expect(line).not.toContain('sent STAMP');
  });

  it('keeps per-channel formatting for the non-suppressed shape', () => {
    expect(
      digestResultLine({ ntfy: { ok: true, status: 200 } }, 'STAMP'),
    ).toBe('Digest sent STAMP via ntfy.');
    expect(
      digestResultLine(
        {
          ntfy: { ok: true },
          email: { ok: false, error: 'boom' },
        },
        'STAMP',
      ),
    ).toBe('Digest partially sent STAMP — ntfy ok; email failed (boom).');
  });

  it('treats a stray string channel value as no report, not a crash', () => {
    // DigestResults permits string values, so a non-suppressed
    // string must not blow up the channel filter.
    const odd: DigestResults = { ntfy: 'odd' };
    expect(digestResultLine(odd, 'STAMP')).toBe(
      'Digest built but delivered nowhere — no channel reported a send.',
    );
  });

  it('renders the suppressed line in a real DOM mount', () => {
    const raw: DigestResults = { suppressed: "event 'digest' muted" };
    mount(<p data-testid="digest-line">{digestResultLine(raw, 'STAMP')}</p>);
    const el = document.querySelector('[data-testid="digest-line"]');
    if (!el) throw new Error('expected the digest line to mount');
    expect(el.textContent).toBe("Digest not sent — event 'digest' muted.");
  });
});
