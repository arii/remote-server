import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import AgentDrawer from './AgentDrawer';

function launchTag(html: string): string {
  const at = html.indexOf('>Launch<');
  expect(at).toBeGreaterThan(-1);
  return html.slice(html.lastIndexOf('<button', at), at);
}

describe('Launch stays disabled until a task is typed (#1491 finding 8)', () => {
  it('disables Launch and says why when the task box is empty', () => {
    const html = renderToStaticMarkup(
      <AgentDrawer repo="remote-server" initialProviders={['agy']} initialStatus={{ agy: true }} />,
    );
    expect(launchTag(html)).toContain('disabled');
    expect(html).toContain('Describe the task to enable Launch.');
  });
});
