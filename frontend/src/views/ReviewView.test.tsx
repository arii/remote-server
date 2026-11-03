import { describe, expect, it } from 'vitest';
import { applyOptimisticMerge, nextPrAfterMerge, revertOptimisticMerge } from './ReviewView';

describe('optimistic merge row state', () => {
  it('applies the pending state without mutating the prior set', () => {
    const prev = new Set([7]);
    const next = applyOptimisticMerge(prev, 12);
    expect(next.has(12)).toBe(true);
    expect(prev.has(12)).toBe(false);
  });

  it('restores the exact prior state on failure', () => {
    const prev = new Set([7]);
    const pending = applyOptimisticMerge(prev, 12);
    expect(revertOptimisticMerge(pending, 12)).toEqual(prev);
    expect(revertOptimisticMerge(pending, 12).has(12)).toBe(false);
  });

  it('leaves unrelated rows untouched', () => {
    const pending = applyOptimisticMerge(new Set([7, 9]), 12);
    expect([...pending].sort((a, b) => a - b)).toEqual([7, 9, 12]);
    expect([...revertOptimisticMerge(pending, 12)].sort((a, b) => a - b)).toEqual([7, 9]);
  });
});

describe('next PR after merge', () => {
  it('advances to the next row when the merged PR is in the middle', () => {
    expect(nextPrAfterMerge([3, 7, 9], 7)).toBe(9);
  });

  it('wraps to the first row when the merged PR was last', () => {
    expect(nextPrAfterMerge([3, 7, 9], 9)).toBe(3);
  });

  it('returns null when there is no other PR', () => {
    expect(nextPrAfterMerge([7], 7)).toBeNull();
  });

  it('returns null when the merged PR is not in the list', () => {
    expect(nextPrAfterMerge([3, 7, 9], 12)).toBeNull();
  });
});
