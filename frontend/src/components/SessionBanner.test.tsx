import { beforeEach, describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import SessionBanner, { sessionBannerText } from './SessionBanner';
import { __clearAuthExpired, fetchJobs, isAuthExpired } from '../apiClient';

beforeEach(() => {
  vi.unstubAllGlobals();
  __clearAuthExpired();
});

function mockLoginRedirectFetch() {
  return vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    redirected: true,
    url: 'https://github.com/login/oauth/authorize?client_id=x',
    json: async () => ({}),
    text: async () => '',
  });
}

describe('SessionBanner (issue #1035)', () => {
  it('renders nothing while the session is alive', () => {
    expect(isAuthExpired()).toBe(false);
    expect(renderToStaticMarkup(<SessionBanner />)).toBe('');
  });

  it('renders the re-login banner after a redirected fetch', async () => {
    vi.stubGlobal('fetch', mockLoginRedirectFetch());
    await expect(fetchJobs()).rejects.toThrow(/session expired/i);
    expect(isAuthExpired()).toBe(true);
    const html = renderToStaticMarkup(<SessionBanner />);
    expect(html).toContain('data-testid="session-banner"');
    expect(html).toContain('data-testid="session-relogin"');
    expect(html).toContain('Sign in again');
    expect(sessionBannerText()).toMatch(/session expired/i);
  });
});
