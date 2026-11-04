// Shared td-ux config loader (#880): one place for the
// scripts/config/ux-audit.config.json read + UX_BASE_URL override,
// so the runners cannot drift apart. Imported by the ux runners,
// which td invokes externally (this file has no entry point).
// fallow-ignore-file unused-file
import fs from 'fs';
import path from 'path';

export interface UxViewport {
  name: string;
  width: number;
  height: number;
}

export interface UxViewports {
  desktop: UxViewport[];
  mobile: UxViewport[];
}

export function loadUxConfig(): {
  baseUrl: string;
  viewports: UxViewports;
} {
  const config = JSON.parse(
    fs.readFileSync(
      path.join(
        process.cwd(),
        'scripts',
        'config',
        'ux-audit.config.json',
      ),
      'utf-8',
    ),
  );
  return {
    baseUrl: process.env.UX_BASE_URL || config.baseUrl,
    viewports: config.viewports,
  };
}
