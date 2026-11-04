import { useCallback, useEffect, useState } from 'react';

// Shared snapshot-loader hook: History and the activity timeline
// both load a feeds snapshot on mount with a manual refresh, an
// error slot, and a loading flag. One implementation so the two
// call sites never drift. The loader is a module function (stable
// across renders), so the mount effect runs exactly once.
export function useSnapshotData<T>(loadSnapshot: () => Promise<T>, initial: T) {
  const [data, setData] = useState<T>(initial);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await loadSnapshot());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [loadSnapshot]);

  useEffect(() => {
    void load();
  }, [load]);

  return { data, error, loading, load };
}
