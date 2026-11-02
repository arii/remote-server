export type AttentionSeverity = 'crit' | 'warn' | 'info';

export interface AttentionAction {
  label: string;
  method: string;
  href: string;
  destructive: boolean;
  intent: string;
}

export interface AttentionItem {
  id: string;
  kind: string;
  title: string;
  detail: string;
  repo: string;
  severity: AttentionSeverity;
  action: AttentionAction;
  created_at: number;
}

export interface AttentionResponse {
  updated_at: number;
  items: AttentionItem[];
}
