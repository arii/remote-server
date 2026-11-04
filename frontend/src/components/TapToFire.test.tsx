import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import AgentDrawer from '../views/AgentDrawer';
import TapToFire, { confirmPrompt } from './TapToFire';

describe('tap-to-fire launch (issue #354)', () => {
  it('agent idle renders a Launch button with no hold gesture', () => {
    const html = renderToStaticMarkup(
      <AgentDrawer repo="remote-server" initialProviders={['muse', 'agy']} />,
    );
    expect(html).toContain('Launch');
    expect(html).not.toContain('Hold to arm');
    expect(html).not.toContain('hold-disarmed');
  });
});

describe('TapToFire', () => {
  it('direct variant renders one plain button with the label', () => {
    const html = renderToStaticMarkup(<TapToFire label="Launch" onFire={() => {}} />);
    expect(html).toContain('data-testid="tap-fire"');
    expect(html).toContain('>Launch</button>');
    expect(html).not.toContain('data-testid="tap-confirm"');
    expect(html).not.toContain('>Confirm</button>');
    expect(html).not.toContain('>Cancel</button>');
  });

  it('confirm variant hides the inline confirm until tapped', () => {
    const html = renderToStaticMarkup(
      <TapToFire label="Merge approved PR" confirm="This cannot be undone." onFire={() => {}} />,
    );
    expect(html).toContain('data-testid="tap-fire"');
    expect(html).toContain('asks for confirmation');
    expect(html).not.toContain('data-testid="tap-confirm"');
    expect(html).not.toContain('>Confirm</button>');
    expect(html).not.toContain('>Cancel</button>');
  });

  it('builds the inline confirm prompt text', () => {
    expect(confirmPrompt('Merge approved PR', 'This cannot be undone.')).toBe(
      'Merge approved PR? This cannot be undone.',
    );
  });

  it('passes disabled through to the trigger', () => {
    const html = renderToStaticMarkup(<TapToFire label="Launch" disabled onFire={() => {}} />);
    expect(html).toContain('disabled');
  });

  it('names the trigger with the per-row name when provided (issue #580)', () => {
    const html = renderToStaticMarkup(
      <TapToFire label="Delete" name="Delete run-0" confirm="This cannot be undone." onFire={() => {}} />,
    );
    expect(html).toContain('aria-label="Delete run-0 (asks for confirmation)"');
    expect(html).toContain('>Delete</button>');
  });

  it('defaults the accessible name to the label', () => {
    const html = renderToStaticMarkup(
      <TapToFire label="Merge approved PR" confirm="This cannot be undone." onFire={() => {}} />,
    );
    expect(html).toContain('aria-label="Merge approved PR (asks for confirmation)"');
    const direct = renderToStaticMarkup(<TapToFire label="Launch" onFire={() => {}} />);
    expect(direct).toContain('aria-label="Launch"');
  });
});
