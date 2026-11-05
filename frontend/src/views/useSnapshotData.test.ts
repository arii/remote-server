import { describe, expect, it } from 'vitest';
import { shouldShowSnapshotContent } from './useSnapshotData';

describe('stale-first snapshot rule (issue #907)', () => {
  it('shows the loader only when empty and loading', () => {
    expect(shouldShowSnapshotContent(true, true)).toBe(false);
  });

  it('keeps stale rows visible during reloads', () => {
    expect(shouldShowSnapshotContent(false, true)).toBe(true);
  });

  it('shows content whenever settled', () => {
    expect(shouldShowSnapshotContent(true, false)).toBe(true);
    expect(shouldShowSnapshotContent(false, false)).toBe(true);
  });
});
