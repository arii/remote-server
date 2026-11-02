import populated from './attention-populated.json';
import empty from './attention-empty.json';
import type { AttentionResponse } from '../attentionTypes';
import { useMock } from '../views/mockViews';

export function loadAttention(): AttentionResponse {
  const data = populated as AttentionResponse;
  return { updated_at: data.updated_at, items: [...data.items] };
}

export function loadAttentionEmpty(): AttentionResponse {
  const data = empty as AttentionResponse;
  return { updated_at: data.updated_at, items: [] };
}

export function shouldUseMock(): boolean {
  return useMock();
}
