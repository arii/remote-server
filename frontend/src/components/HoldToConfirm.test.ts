import { describe, expect, it } from 'vitest';
import { shouldTickHaptic } from './HoldToConfirm';

describe('shouldTickHaptic', () => {
  it('ticks on quartile crossings', () => {
    expect(shouldTickHaptic(0, 0.26, false)).toBe(true);
    expect(shouldTickHaptic(0.26, 0.51, false)).toBe(true);
    expect(shouldTickHaptic(0.51, 0.76, false)).toBe(true);
  });

  it('does not tick within the same quartile or on completion', () => {
    expect(shouldTickHaptic(0, 0.1, false)).toBe(false);
    expect(shouldTickHaptic(0.3, 0.4, false)).toBe(false);
    expect(shouldTickHaptic(0.9, 1, false)).toBe(false);
  });

  it('suppresses ticks under reduced motion', () => {
    expect(shouldTickHaptic(0, 0.26, true)).toBe(false);
    expect(shouldTickHaptic(0.24, 0.51, true)).toBe(false);
  });
});
