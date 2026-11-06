import { useEffect, useState } from 'react';
import { isAuthExpired, subscribeAuthExpired } from '../apiClient';

// Session-expiry strip (issue #1035). Mounted once in App so every
// view inherits it; renders nothing while the proxy session is
// alive. A reload re-enters through oauth2-proxy, which re-runs the
// login dance and lands back on the current view.
export function sessionBannerText(): string {
  return 'Session expired — API calls are failing. Sign in again to resume live data.';
}

export default function SessionBanner() {
  const [expired, setExpired] = useState<boolean>(() => isAuthExpired());
  useEffect(() => subscribeAuthExpired(() => setExpired(isAuthExpired())), []);
  if (!expired) return null;
  return (
    <section data-testid="session-banner" role="alert" className="session-banner">
      <span>{sessionBannerText()}</span>{' '}
      <button type="button" data-testid="session-relogin" onClick={() => window.location.reload()}>
        Sign in again
      </button>
    </section>
  );
}
