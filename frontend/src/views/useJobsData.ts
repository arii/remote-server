import { useEffect, useState } from 'react';
import { fetchPrDetails, fetchPrs } from '../apiClient';
import { mergeBlockReason } from '../reviewTypes';
import { useMock } from './mockViews';
import type { PrRefState } from './jobsUtils';

// Approved-and-green PR count for the merge row. null = unknown
// (fail closed: the merge button stays disabled until the list loads).
export function useMergeReady(repo: string): number | null {
  const [mergeReady, setMergeReady] = useState<number | null>(null);
  useEffect(() => {
    let cancelled = false;
    setMergeReady(null);
    if (useMock()) return;
    (async () => {
      try {
        const live = await fetchPrs(repo);
        if (!cancelled) {
          setMergeReady(live.prs.filter((p) => mergeBlockReason(p) === null).length);
        }
      } catch {
        if (!cancelled) setMergeReady(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [repo]);
  return mergeReady;
}

// Debounced PR reference state behind the td relevance gates.
export function usePrRefState(pr: string, repo: string): PrRefState | null {
  const [prState, setPrState] = useState<PrRefState | null>(null);
  useEffect(() => {
    const n = pr.trim();
    setPrState(null);
    if (!/^\d+$/.test(n) || useMock()) return;
    let cancelled = false;
    const num = Number(n);
    const t = window.setTimeout(() => {
      (async () => {
        try {
          const d = await fetchPrDetails(repo, num);
          if (!cancelled) setPrState({ n: num, ci: d.ci, conflicted: d.conflicted });
        } catch {
          if (!cancelled) setPrState(null);
        }
      })();
    }, 500);
    return () => {
      cancelled = true;
      window.clearTimeout(t);
    };
  }, [pr, repo]);
  return prState;
}
