// Shared inline error + retry for fetch failures (#907): errors
// name the problem and the recovery in one line. Callers keep
// their own testids.
export default function RetryableError({
  testid,
  message,
  retryTestid,
  onRetry,
}: {
  testid: string;
  message: string;
  retryTestid: string;
  onRetry: () => void;
}) {
  return (
    <p role="alert" data-testid={testid}>
      {message}{' '}
      <button type="button" data-testid={retryTestid} onClick={onRetry}>
        Retry
      </button>
    </p>
  );
}
