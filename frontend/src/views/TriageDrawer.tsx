// TriageDrawer (D5, chat-first shell): the Review/Jobs/Queue (and,
// after the Briefing fold-down, Briefing) surfaces live behind the
// StatusStrip tap instead of owning parallel tabs. One section
// mounted at a time — Review and Jobs each bind window triage keys,
// so mounting both would double-bind j/k/Enter.
import { useState, type ReactNode } from 'react';
import ReviewView from './ReviewView';
import JobsView from './JobsView';
import QueueView from './QueueView';
// Briefing fold-down (D5): the digest, notification controls, and
// next actions render as a drawer section.
import BriefingView from './BriefingView';

export type TriageSection = 'review' | 'jobs' | 'queue' | 'briefing';

interface TriageDrawerProps {
  open: boolean;
  section: TriageSection;
  onSectionChange: (section: TriageSection) => void;
  onClose: () => void;
  repo: string;
  defaultBranch?: string;
  onAskAgent: (seed: string) => void;
  onOpenChat: () => void;
  // New-issue form request (threaded through to ReviewView).
  createRequest?: number;
  // Palette-dispatched td command (#1057): handed to the Jobs view
  // so it launches through the same progress/notice path as the
  // Jobs buttons.
  pendingTdCommand?: { command: string; nonce: number } | null;
  onPendingTdHandled?: (nonce: number) => void;
  // Briefing section plumbing: the digest hops to History, the
  // pipeline pills hop to the drawer's Review section (#902), and
  // a palette nonce bumps the re-fetch while the section is open.
  onOpenHistory: () => void;
  reloadRequest?: number;
}

export default function TriageDrawer({
  open,
  section,
  onSectionChange,
  onClose,
  repo,
  defaultBranch,
  onAskAgent,
  onOpenChat,
  createRequest = 0,
  pendingTdCommand = null,
  onPendingTdHandled,
  onOpenHistory,
  reloadRequest = 0,
}: TriageDrawerProps): ReactNode {
  // Last Review row selection, surviving section switches so Jobs
  // can prefill its PR field from row context. Drawer-local: the
  // selection belongs to the drawer visit, not the app session.
  const [lastPr, setLastPr] = useState<number | null>(null);
  if (!open) return null;
  return (
    <section className="detail triage-drawer" data-testid="triage-drawer" aria-label="Triage">
      <div className="triage-head">
        <button type="button" data-testid="triage-close" onClick={onClose}>
          Close
        </button>
      </div>
      {section === 'review' && (
        <ReviewView
          repo={repo}
          defaultBranch={defaultBranch}
          onAskAgent={onAskAgent}
          onSelectPr={setLastPr}
          createRequest={createRequest}
        />
      )}
      {section === 'jobs' && (
        <JobsView
          repo={repo}
          defaultBranch={defaultBranch}
          onAskAgent={onAskAgent}
          initialPr={lastPr}
          onOpenChat={onOpenChat}
          pendingTdCommand={pendingTdCommand}
          onPendingTdHandled={onPendingTdHandled}
        />
      )}
      {section === 'queue' && <QueueView repo={repo} />}
      {section === 'briefing' && (
        <BriefingView
          repo={repo}
          onAskAgent={onAskAgent}
          onOpenHistory={onOpenHistory}
          // Pipeline-pill fallback (#902): pills without a backend
          // URL hop to this drawer's Review section.
          onOpenReview={() => onSectionChange('review')}
          reloadRequest={reloadRequest}
          active={open}
        />
      )}
    </section>
  );
}
