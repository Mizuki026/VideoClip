import { afterEach, describe, expect, it, vi } from 'vitest';
import { getCsrfToken } from './api';

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
}

describe('CSRF bootstrap requests', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('shares concurrent token fetches so duplicate React bootstrap effects keep cookie and state aligned', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ csrf_token: 'one-ephemeral-token' }));
    vi.stubGlobal('fetch', fetchMock);

    const results = await Promise.all([getCsrfToken(), getCsrfToken(), getCsrfToken()]);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(results).toEqual([
      { csrf_token: 'one-ephemeral-token' },
      { csrf_token: 'one-ephemeral-token' },
      { csrf_token: 'one-ephemeral-token' },
    ]);

    await getCsrfToken();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
