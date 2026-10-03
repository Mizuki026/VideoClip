import { describe, expect, it } from 'vitest';
import { shouldCleanupAcceptanceData } from '../e2e/acceptance-data';

describe('acceptance data cleanup policy', () => {
  it('cleans generated data only after a passing browser run', () => {
    expect(shouldCleanupAcceptanceData('passed')).toBe(true);
    expect(shouldCleanupAcceptanceData('failed')).toBe(false);
    expect(shouldCleanupAcceptanceData('timedOut')).toBe(false);
    expect(shouldCleanupAcceptanceData('interrupted')).toBe(false);
    expect(shouldCleanupAcceptanceData(undefined)).toBe(false);
  });
});
