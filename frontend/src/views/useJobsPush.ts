import { useEffect, useRef, useState } from 'react';
import { subscribeJobsPush } from '../apiClient';
import type { JobRecord } from '../apiTypes';
import { useMock } from './mockViews';

// Job-list push subscription. Returns channel liveness so the view
// runs its polling loop only while the channel is down; the stream
// reports live on its first frame (the server snapshots
// immediately, so no wait). Silent fallback: on drop, live flips
// false and polling covers the gap; push resumes on next mount.
export function useJobsPush(onJobs: (jobs: JobRecord[]) => void): boolean {
  const [live, setLive] = useState(false);
  const cbRef = useRef(onJobs);
  cbRef.current = onJobs;

  useEffect(() => {
    if (useMock()) return;
    return subscribeJobsPush({
      onJobs: (jobs) => cbRef.current(jobs),
      onLive: setLive,
    });
  }, []);

  return live;
}
