import { TRIAGE_HINT } from '../triageKeys';

// Keyboard triage hint shared by Review and Jobs. The triage-hint class
// lets coarse-pointer CSS hide it on the phone-first UI.
export default function TriageHint({ extra = '' }: { extra?: string }) {
  return (
    <p className="row-meta triage-hint" data-testid="triage-hint">
      {TRIAGE_HINT}
      {extra}
    </p>
  );
}
