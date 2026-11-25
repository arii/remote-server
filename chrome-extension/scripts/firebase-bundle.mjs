#!/usr/bin/env node
// Bundles the Firebase AI Logic SDK into extension/vendor/firebase-ai.bundle.js.
// The extension has no other bundler; this mirrors how legacy-matcher.mjs
// emits its generated twin during the build. The output is gitignored.

import fs from "node:fs";
import path from "node:path";
import { execSync } from "node:child_process";

export function emitFirebaseBundle() {
  const root = path.resolve(import.meta.dirname, "..");
  const shim = path.join(root, "scripts", "firebase-shim.mjs");
  const outDir = path.join(root, "extension", "vendor");
  const out = path.join(outDir, "firebase-ai.bundle.js");
  fs.mkdirSync(outDir, { recursive: true });
  execSync(
    `npx esbuild "${shim}" --bundle --format=esm --minify --outfile="${out}"`,
    { stdio: "inherit" }
  );
  const kb = (fs.statSync(out).size / 1024).toFixed(1);
  return { out, kb };
}
