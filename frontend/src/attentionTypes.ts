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
  severity: string;
  action: AttentionAction;
  created_at: string;
}

export interface AttentionResponse {
  updated_at: string;
  items: AttentionItem[];
}
