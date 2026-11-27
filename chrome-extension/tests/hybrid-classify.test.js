import { describe, it, expect } from 'vitest';
import { describeCloudFailure } from '../extension/lib/hybrid-session.js';

describe('describeCloudFailure', () => {
  it('classifies permission-denied code as rejection', () => {
    const err = { code: 'permission-denied' };
    expect(describeCloudFailure(err)).toBe(
      "The cloud request was rejected (App Check or access). Nothing was answered by the cloud; on-device inference only works if Chrome's built-in model is available."
    );
  });

  it('classifies message containing [403] Forbidden as rejection', () => {
    const err = { message: '[403] Forbidden' };
    expect(describeCloudFailure(err)).toBe(
      "The cloud request was rejected (App Check or access). Nothing was answered by the cloud; on-device inference only works if Chrome's built-in model is available."
    );
  });

  it('does not classify quota 4031 exceeded as rejection', () => {
    const err = { message: 'quota 4031 exceeded' };
    expect(describeCloudFailure(err)).toBe(
      'The cloud request failed: quota 4031 exceeded'
    );
  });
});
