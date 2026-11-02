import populated from './attention-populated.json';
import empty from './attention-empty.json';
import type { AttentionResponse } from '../attentionTypes';

export function loadAttention(): AttentionResponse {
  const data = populated as unknown as AttentionResponse;
  return { updated_at: data.updated_at, items: [...data.items] };
}

export function loadAttentionEmpty(): AttentionResponse {
  const data = empty as unknown as AttentionResponse;
  return { updated_at: data.updated_at, items: [] };
}
