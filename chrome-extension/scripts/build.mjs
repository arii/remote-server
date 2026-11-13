#!/usr/bin/env node
// ============================================================================
// LocalForm AI Extension Build & Verification Script
// Validates manifest, checks all referenced assets, builds to dist/, creates zip
// ============================================================================

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execSync } from 'node:child_process';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const ROOT_DIR = path.resolve(__dirname, '..');
const EXT_SRC = path.join(ROOT_DIR, 'extension');
const DIST_DIR = path.join(ROOT_DIR, 'dist');

console.log('⚡ Building LocalForm AI Chrome Extension...\n');

// 1. Check extension directory
if (!fs.existsSync(EXT_SRC)) {
  console.error(`❌ Source directory not found: ${EXT_SRC}`);
  process.exit(1);
}

// 2. Validate manifest.json
const manifestPath = path.join(EXT_SRC, 'manifest.json');
if (!fs.existsSync(manifestPath)) {
  console.error(`❌ manifest.json not found in ${EXT_SRC}`);
  process.exit(1);
}

let manifest;
try {
  manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  console.log(`✅ manifest.json loaded (Name: "${manifest.name}", v${manifest.version})`);
} catch (err) {
  console.error(`❌ Failed to parse manifest.json: ${err.message}`);
  process.exit(1);
}

// 3. Verify referenced files
const requiredFiles = [
  manifest.background?.service_worker,
  manifest.side_panel?.default_path,
  ...(manifest.content_scripts?.flatMap(cs => cs.js) || []),
  ...Object.values(manifest.icons || {})
].filter(Boolean);

let missingCount = 0;
for (const relFile of requiredFiles) {
  const fullPath = path.join(EXT_SRC, relFile);
  if (fs.existsSync(fullPath)) {
    const stat = fs.statSync(fullPath);
    console.log(`  ✓ Asset verified: ${relFile} (${(stat.size / 1024).toFixed(1)} KB)`);
  } else {
    console.error(`  ✗ Missing asset: ${relFile}`);
    missingCount++;
  }
}

if (missingCount > 0) {
  console.error(`\n❌ Build failed: ${missingCount} required asset(s) missing.`);
  process.exit(1);
}

// 4. Ensure dist/ exists and copy all files
if (fs.existsSync(DIST_DIR)) {
  fs.rmSync(DIST_DIR, { recursive: true, force: true });
}
fs.mkdirSync(DIST_DIR, { recursive: true });

function copyRecursive(src, dest) {
  const stat = fs.statSync(src);
  if (stat.isDirectory()) {
    fs.mkdirSync(dest, { recursive: true });
    for (const child of fs.readdirSync(src)) {
      copyRecursive(path.join(src, child), path.join(dest, child));
    }
  } else {
    fs.copyFileSync(src, dest);
  }
}

copyRecursive(EXT_SRC, DIST_DIR);
console.log(`\n✅ Copied all extension files to: ${DIST_DIR}`);

// 5. Create .zip package for deployment
const zipPath = path.join(DIST_DIR, 'localform-ai-extension.zip');
try {
  execSync(
    `python3 -c "import zipfile, os; z = zipfile.ZipFile('${zipPath}', 'w', zipfile.ZIP_DEFLATED); [z.write(os.path.join(r, f), os.path.relpath(os.path.join(r, f), '${EXT_SRC}')) for r, d, fs_list in os.walk('${EXT_SRC}') for f in fs_list]; z.close()"`
  );
  if (fs.existsSync(zipPath)) {
    const zipStat = fs.statSync(zipPath);
    console.log(`✅ Packaged ZIP archive: ${zipPath} (${(zipStat.size / 1024).toFixed(1)} KB)`);
  }
} catch (e) {
  // Zip creation is optional
}

console.log('\n📦 Extension Build Complete!');
console.log('----------------------------------------------------');
console.log(`Unpacked Extension Path: ${DIST_DIR}`);
console.log(`Development Source Path: ${EXT_SRC}`);
console.log('\nTo load into Google Chrome:');
console.log('1. Open chrome://extensions in Chrome');
console.log('2. Toggle "Developer mode" on (top right)');
console.log('3. Click "Load unpacked" and select:');
console.log(`   ${DIST_DIR}`);
console.log('----------------------------------------------------\n');
