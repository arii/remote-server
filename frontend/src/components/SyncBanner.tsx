import { useEffect, useState } from 'react';
import { clearOutbox, flushOutbox, getOutboxStatus, subscribeOutbox } from '../apiClient';
import type { OutboxStatus } from '../apiClient';

function queuedNoun(pending: number): string {
  return pending === 1 ? '1 action' : `${pending} actions`;
}

function bannerText(status: OutboxStatus): string {
  if (!status.online && status.pending === 0) {
    return "You're offline — actions will queue until reconnect.";
  }
  if (!status.online) {
    return `Offline — ${queuedNoun(status.pending)} queued. Syncs on reconnect.`;
  }
  if (status.syncing) return 'Syncing queued actions…';
  if (status.lastError) {
    return `${queuedNoun(status.pending)} queued — last sync failed: ${status.lastError}`;
  }
  return `${queuedNoun(status.pending)} queued.`;
}

// Sync status strip for the offline outbox (issue #404). Mounted
// once in App so every view inherits it; renders nothing while
// online with an empty queue.
export default function SyncBanner() {
  const [status, setStatus] = useState<OutboxStatus>(() => getOutboxStatus());
  useEffect(() => subscribeOutbox(() => setStatus(getOutboxStatus())), []);
  if (status.online && status.pending === 0 && !status.syncing) return null;
  return (
    <section data-testid="sync-banner" role="status" className="sync-banner">
      <span>{bannerText(status)}</span>
      {status.online && status.pending > 0 && !status.syncing && (
        <>
          {' '}
          <button type="button" data-testid="sync-retry" onClick={() => void flushOutbox()}>
            Sync now
          </button>
        </>
      )}
      {status.pending > 0 && !status.syncing && (
        <>
          {' '}
          <button type="button" data-testid="sync-discard" onClick={() => clearOutbox()}>
            Discard
          </button>
        </>
      )}
    </section>
  );
}
