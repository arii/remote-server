import type { DetailResponse, HealthStatus, ListResponse } from './types';
import listFixture from './mocks/list.json';
import detailFixture from './mocks/detail.json';
import emptyFixture from './mocks/empty.json';
import errorFixture from './mocks/error.json';

// Mock data loader: serves the EXACT fixture JSON shapes from
// tests/fixtures/sessions/ (mirrored under src/mocks/ so Vite can bundle them).
// Swap these functions for fetch() calls against the real API later.

export function loadSessionList(useEmpty = false): ListResponse {
  const data = (useEmpty ? emptyFixture : listFixture) as ListResponse;
  return { sessions: [...data.sessions] };
}

export function loadSessionDetail(file: string): DetailResponse | { error: string } {
  if (file === (detailFixture as DetailResponse).file) {
    const d = detailFixture as DetailResponse;
    return { ...d, turns: [...d.turns] };
  }
  return { ...(errorFixture as { error: string }) };
}

export function loadHealth(sessions: number): HealthStatus {
  return { ok: true, sessions, timestamp: Date.now() };
}
