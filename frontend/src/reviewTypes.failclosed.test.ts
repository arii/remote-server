import { describe, expect, it } from 'vitest';
import {
  VIEWER_UNKNOWN,
  approveBlockReason,
  approveMergeEligible,
  isOwnPr,
  isViewerUnknown,
} from './reviewTypes';
import type { PrRow } from './reviewTypes';

// Independent fail-closed viewer tests: an "unknown" viewer must
// disable Approve (it is not "not yours"), while an absent viewer
// keeps the prior legacy behavior (Approve stays enabled, the
// endpoint 400 is the backstop).

function row(over: Partial<PrRow> = {}): PrRow {
  return {
    number: 7,
    title: 'Fix thing',
    author: 'teammate',
    branch: 'fix-thing',
    isDraft: false,
    ciStatus: 'passing',
    ciLabel: 'passing',
    reviewDecision: '',
    mergeable: 'MERGEABLE',
    url: 'https://github.com/arii/remote-server/pull/7',
    ...over,
  };
}

describe('viewer unknown sentinel', () => {
  it('is the literal "unknown" string', () => {
    expect(VIEWER_UNKNOWN).toBe('unknown');
  });

  it('detects unknown case-insensitively, rejects absent/other viewers', () => {
    expect(isViewerUnknown('unknown')).toBe(true);
    expect(isViewerUnknown('UNKNOWN')).toBe(true);
    expect(isViewerUnknown('Unknown')).toBe(true);
    expect(isViewerUnknown(undefined)).toBe(false);
    expect(isViewerUnknown('')).toBe(false);
    expect(isViewerUnknown('octo')).toBe(false);
  });
});

describe('approve fail-closed on unknown viewer', () => {
  it('blocks unknown while absent-viewer keeps prior not-yours behavior', () => {
    const pr = row();
    // Fail-closed: unknown blocks even though the author differs.
    expect(approveBlockReason(pr, 'unknown')).not.toBeNull();
    expect(approveBlockReason(pr, 'UNKNOWN')).not.toBeNull();
    // Prior legacy behavior pinned: no viewer means "not yours".
    expect(approveBlockReason(pr)).toBeNull();
    expect(approveBlockReason(pr, '')).toBeNull();
    expect(approveBlockReason(pr, 'someone-else')).toBeNull();
  });

  it('blocks via the unknown path, not authorship matching', () => {
    const pr = row({ author: 'teammate' });
    expect(isOwnPr(pr, 'unknown')).toBe(false);
    expect(approveBlockReason(pr, 'unknown')).not.toBeNull();
  });

  it('keeps draft/approved precedence over the unknown block', () => {
    expect(approveBlockReason(row({ isDraft: true }), 'unknown')).toBe('is a draft');
    expect(approveBlockReason(row({ reviewDecision: 'APPROVED' }), 'unknown')).toBe(
      'already approved',
    );
  });

  it('makes otherwise-eligible rows ineligible under unknown, eligible without a viewer', () => {
    const pr = row();
    expect(approveMergeEligible(pr, 'unknown')).toBe(false);
    expect(approveMergeEligible(pr, 'someone-else')).toBe(true);
    expect(approveMergeEligible(pr, undefined)).toBe(true);
  });
});
