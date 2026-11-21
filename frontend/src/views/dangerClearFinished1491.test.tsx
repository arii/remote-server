import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import DangerZone from './DangerZone';

describe('Clear finished runs asks for confirmation like the other danger rows (#1491 finding 21)', () => {
  it('labels the Clear finished trigger as asking for confirmation', () => {
    const html = renderToStaticMarkup(
      <DangerZone
        mergeReady={null}
        finishedCount={2}
        runCount={3}
        onDangerAction={() => {}}
        onClear={() => {}}
        onTakeover={() => {}}
      />,
    );
    const at = html.indexOf('data-testid="danger-clear-finished"');
    const end = html.indexOf('data-testid="danger-clear-all"');
    expect(at).toBeGreaterThan(-1);
    expect(html.slice(at, end)).toContain('Clear finished runs (asks for confirmation)');
  });
});
