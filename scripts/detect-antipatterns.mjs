#!/usr/bin/env node
/**
 * Detect banned antipatterns in THIS repo (td preflight gate).
 *
 * td invokes this script during preflight/audit with BoomTick-oriented
 * targets (src/features, src/pages, ...) that do not exist here. When none
 * of the passed targets exist, the gate falls back to the repo-aware
 * CHECK_PATHS below instead of checking nothing and passing.
 *
 * --json contract (td-compatible, mirrors BoomTick's detector):
 *   { violations: {file: [{line, pattern, severity, value, message}]},
 *     config: {rules: [{name, severity, message}]},
 *     status: "ok" | "error",
 *     summary: {filesChecked, violations} }
 * Exit 1 when any violation is found, 0 when clean (--count-only always 0).
 *
 * Suppressions (same idea as BoomTick's impeccable-ignore):
 *   antipattern-ignore-file  anywhere in a file skips the file
 *   antipattern-ignore       on a line skips violations on that line
 *
 * Known limitations: single-line matching only (a shell=True split across
 * lines is missed); f-string/template interpolations are treated as opaque
 * string content; the detector skips its own file (its rule table would
 * otherwise self-match, e.g. the DangerouslySetInnerHTML pattern literal).
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const SELF_BASENAME = path.basename(fileURLToPath(import.meta.url));
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

// Repo-aware default scan roots, used when no CLI target exists on disk.
const CHECK_PATHS = ['backend', 'core_cli', 'deep-agent', 'frontend/src', 'scripts', '.github/workflows'];
const AUDIT_EXTENSIONS = ['.py', '.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.sh', '.yml', '.yaml'];
const SKIP_DIRS = new Set([
  'node_modules', '.git', '__pycache__', '.venv', 'dist', 'build',
  'coverage', 'chroma_db', '.boomtick',
]);
const IGNORE_FILE_MARKER = 'antipattern-ignore-file';
const IGNORE_LINE_MARKER = 'antipattern-ignore';

// source: 'code' matches code only (strings+comments blanked),
// 'nocomments' keeps strings (argv payloads) but blanks comments,
// 'raw' matches the untouched text (for comment-comparable rules).
const RULES = [
  {
    name: 'ShellTrue',
    severity: 'major',
    exts: ['.py'],
    source: 'code',
    patterns: [/shell\s*=\s*True/g],
    message: 'shell=True is banned: argv must never run through a shell (see deep-agent/s5tools.py discipline).',
  },
  {
    name: 'OsSystem',
    severity: 'major',
    exts: ['.py'],
    source: 'code',
    patterns: [/(?<![\w.])os\.system\s*\(/g],
    message: 'os.system() is banned: use subprocess argv with a timeout (see deep-agent/s5tools.run).',
  },
  {
    name: 'NpxTscSquat',
    severity: 'major',
    exts: ['.py', '.sh'],
    source: 'nocomments',
    patterns: [/npx\s+tsc\b/g, /\(\s*["']npx["']\s*,\s*["']tsc["']\s*\)/g],
    message: '`npx tsc` fetches the `tsc` squat placeholder, not TypeScript (see #517).',
  },
  {
    name: 'HardcodedCheckout',
    severity: 'major',
    exts: ['.py'],
    source: 'raw',
    pathTest: (p) => /(?:^|\/)deep-agent\/[^\/]+\.py$/.test(p) && !/(?:^|\/)s5tools\.py$/.test(p),
    patterns: [/\/srv\/remote-server/g],
    message: 'Hardcoded /srv/remote-server breaks worktree hermeticity; resolve via cli.repo_root() (see #540). s5tools.py mapping exempt.',
  },
  {
    name: 'DangerouslySetInnerHTML',
    severity: 'major',
    exts: ['.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs'],
    source: 'code',
    patterns: [/dangerouslySetInnerHTML/g],
    message: 'dangerouslySetInnerHTML is banned: render structured content instead of raw HTML.',
  },
  {
    name: 'DynamicCodeExec',
    severity: 'major',
    exts: ['.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs'],
    source: 'code',
    patterns: [/(?<![\w$.])eval\s*\(/g, /new\s+Function\s*\(/g],
    message: 'eval()/new Function() is banned: no dynamic code execution in the frontend.',
  },
  {
    name: 'CurlPipeShell',
    severity: 'major',
    exts: ['.sh'],
    source: 'code',
    patterns: [/curl\b[^\n]{0,200}\|\s*(bash|sh)\b/g],
    message: 'curl-piped-to-shell is banned: vendor the script or pin its hash instead.',
  },
  {
    name: 'BareEnvKey',
    severity: 'major',
    exts: ['.yml', '.yaml'],
    source: 'raw',
    patterns: [/^([ \t]*)env:[ \t]*(?!\r?\n\1[ \t]+)/gm],
    message: 'Bare env: keys are invalid in GitHub Actions workflows. Provide values or remove the key.',
  },
];

const LANG_OF_EXT = {
  '.py': 'py',
  '.ts': 'js', '.tsx': 'js', '.js': 'js', '.jsx': 'js', '.mjs': 'js', '.cjs': 'js',
  '.sh': 'sh',
  '.yml': 'raw', '.yaml': 'raw',
};

// Space-preserving blanker: strings/comments become spaces, newlines kept,
// so offsets and line numbers stay valid for the original text.
function blanked(text, keep) {
  let out = '';
  for (let i = 0; i < text.length; i++) {
    out += keep[i] ? text[i] : (text[i] === '\n' ? '\n' : ' ');
  }
  return out;
}

function stripPython(text, blankStrings) {
  const keep = new Array(text.length).fill(true);
  let i = 0;
  const blankRange = (a, b) => { for (let k = a; k < b; k++) keep[k] = false; };
  while (i < text.length) {
    const c = text[i];
    if (c === '#') {
      let j = i;
      while (j < text.length && text[j] !== '\n') j++;
      blankRange(i, j);
      i = j;
    } else if (c === "'" || c === '"') {
      const triple = text.startsWith(c.repeat(3), i);
      const delim = triple ? c.repeat(3) : c;
      let j = i + delim.length;
      while (j < text.length) {
        if (text[j] === '\\') { j += 2; continue; }
        if (text.startsWith(delim, j)) { j += delim.length; break; }
        if (!triple && text[j] === '\n') break;
        j++;
      }
      if (blankStrings) blankRange(i, j);
      i = j;
    } else {
      i++;
    }
  }
  return blanked(text, keep);
}

function stripJs(text, blankStrings) {
  const keep = new Array(text.length).fill(true);
  let i = 0;
  const blankRange = (a, b) => { for (let k = a; k < b; k++) keep[k] = false; };
  while (i < text.length) {
    const c = text[i];
    const d = text[i + 1];
    if (c === '/' && d === '/') {
      let j = i;
      while (j < text.length && text[j] !== '\n') j++;
      blankRange(i, j);
      i = j;
    } else if (c === '/' && d === '*') {
      const j = text.indexOf('*/', i + 2);
      const end = j === -1 ? text.length : j + 2;
      blankRange(i, end);
      i = end;
    } else if (c === "'" || c === '"' || c === '`') {
      let j = i + 1;
      while (j < text.length) {
        if (text[j] === '\\') { j += 2; continue; }
        if (text[j] === c) { j++; break; }
        if (c !== '`' && text[j] === '\n') break;
        j++;
      }
      if (blankStrings) blankRange(i, j);
      i = j;
    } else {
      i++;
    }
  }
  return blanked(text, keep);
}

function stripShell(text, blankStrings) {
  const keep = new Array(text.length).fill(true);
  let i = 0;
  const blankRange = (a, b) => { for (let k = a; k < b; k++) keep[k] = false; };
  while (i < text.length) {
    const c = text[i];
    if (c === '\\' && i + 1 < text.length) {
      i += 2;
    } else if (c === '#') {
      let j = i;
      while (j < text.length && text[j] !== '\n') j++;
      blankRange(i, j);
      i = j;
    } else if (c === "'") {
      const j = text.indexOf("'", i + 1);
      const end = j === -1 ? text.length : j + 1;
      if (blankStrings) blankRange(i, end);
      i = end;
    } else if (c === '"') {
      let j = i + 1;
      while (j < text.length) {
        if (text[j] === '\\') { j += 2; continue; }
        if (text[j] === '"') { j++; break; }
        j++;
      }
      if (blankStrings) blankRange(i, j);
      i = j;
    } else {
      i++;
    }
  }
  return blanked(text, keep);
}

const STRIPPERS = { py: stripPython, js: stripJs, sh: stripShell };

function lineOffsets(text) {
  const offsets = [0];
  for (let i = 0; i < text.length; i++) {
    if (text[i] === '\n') offsets.push(i + 1);
  }
  return offsets;
}

function lineOf(offsets, index) {
  let low = 0;
  let high = offsets.length - 1;
  while (low <= high) {
    const mid = Math.floor((low + high) / 2);
    if (offsets[mid] <= index) low = mid + 1;
    else high = mid - 1;
  }
  return low;
}

function checkContent(content, displayPath) {
  if (content.includes(IGNORE_FILE_MARKER)) return [];
  // 'stdin' has no extension: apply every language group (deduped below).
  const ext = path.extname(displayPath).toLowerCase();
  const fileLangs = ext ? [LANG_OF_EXT[ext]].filter(Boolean) : ['py', 'js', 'sh', 'raw'];
  const lines = content.split('\n');
  const offsets = lineOffsets(content);
  const sources = {};
  const sourceFor = (lang, mode) => {
    if (mode === 'raw' || lang === 'raw') return content;
    const key = `${lang}:${mode}`;
    if (!sources[key]) {
      sources[key] = STRIPPERS[lang](content, mode === 'code');
    }
    return sources[key];
  };

  const violations = [];
  const seen = new Set();
  for (const rule of RULES) {
    if (rule.pathTest && !rule.pathTest(displayPath)) continue;
    const ruleLangs = new Set(rule.exts.map((e) => LANG_OF_EXT[e]));
    const langs = fileLangs.filter((l) => ruleLangs.has(l));
    if (langs.length === 0) continue;
    for (const lang of langs) {
      const source = sourceFor(lang, rule.source);
      for (const pattern of rule.patterns) {
        const re = new RegExp(pattern.source, pattern.flags);
        for (const match of source.matchAll(re)) {
          const line = lineOf(offsets, match.index);
          const lineText = lines[line - 1] || '';
          if (lineText.includes(IGNORE_LINE_MARKER)) continue;
          const value = match[0].replace(/\s+/g, ' ').slice(0, 80);
          const key = `${rule.name}:${line}:${value}`;
          if (seen.has(key)) continue;
          seen.add(key);
          violations.push({
            line,
            pattern: rule.name,
            severity: rule.severity,
            value,
            message: rule.message,
          });
        }
      }
    }
  }
  return violations.sort((a, b) => a.line - b.line);
}

function checkFile(absPath, displayPath) {
  let content;
  try {
    content = fs.readFileSync(absPath, 'utf8');
  } catch {
    return [];
  }
  return checkContent(content, displayPath);
}

function displayPathFor(absPath) {
  const rel = path.relative(ROOT, absPath);
  if (rel && !rel.startsWith('..') && !path.isAbsolute(rel)) return rel;
  return absPath;
}

function collectAuditFiles(targets) {
  const results = new Set();
  const isAuditFile = (filepath) => AUDIT_EXTENSIONS.some((ext) => filepath.endsWith(ext));

  const walk = (dir) => {
    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      if (entry.isSymbolicLink()) continue;
      if (entry.isDirectory()) {
        if (SKIP_DIRS.has(entry.name)) continue;
        walk(path.join(dir, entry.name));
      } else if (entry.isFile() && isAuditFile(entry.name)) {
        if (entry.name === SELF_BASENAME) continue;
        results.add(path.join(dir, entry.name));
      }
    }
  };

  for (const target of targets) {
    const abs = path.isAbsolute(target) ? target : path.resolve(process.cwd(), target);
    if (!fs.existsSync(abs)) continue;
    let stat;
    try {
      stat = fs.statSync(abs);
    } catch {
      continue;
    }
    if (stat.isDirectory()) {
      walk(abs);
    } else if (stat.isFile() && isAuditFile(abs) && path.basename(abs) !== SELF_BASENAME) {
      results.add(abs);
    }
  }
  return Array.from(results).sort();
}

function printHelp() {
  console.log(`Usage: node scripts/detect-antipatterns.mjs [--json] [--count-only] [targets...]

Scans backend, core_cli, deep-agent, frontend/src, scripts and
.github/workflows for banned patterns (exit 1 on any violation).

Targets: files or directories to scan. When none of the passed targets
exist (e.g. td's BoomTick-oriented src/... defaults), the repo-aware
defaults above are scanned instead. Pass - to scan stdin.

Rules: ${RULES.map((r) => r.name).join(', ')}`);
}

async function readStdin() {
  let content = '';
  process.stdin.setEncoding('utf8');
  for await (const chunk of process.stdin) content += chunk;
  return content;
}

async function main() {
  const args = process.argv.slice(2);
  const isJson = args.includes('--json');
  const isCountOnly = args.includes('--count-only');
  const targets = args.filter((a) => !a.startsWith('--'));

  if (args.includes('--help') || args.includes('-h')) {
    printHelp();
    process.exit(0);
  }

  const allViolations = {};
  let filesChecked = 0;

  if (targets.includes('-')) {
    const violations = checkContent(await readStdin(), 'stdin');
    filesChecked = 1;
    if (violations.length > 0) allViolations.stdin = violations;
  } else {
    const resolved = targets.filter((t) => {
      const abs = path.isAbsolute(t) ? t : path.resolve(process.cwd(), t);
      return fs.existsSync(abs);
    });
    // None of the caller's targets exist: scan the real repo instead of
    // checking nothing and passing.
    const effective = resolved.length > 0
      ? resolved
      : CHECK_PATHS.map((p) => path.join(ROOT, p));
    const files = collectAuditFiles(effective);
    filesChecked = files.length;
    for (const file of files) {
      const violations = checkFile(file, displayPathFor(file));
      if (violations.length > 0) allViolations[displayPathFor(file)] = violations;
    }
  }

  const total = Object.values(allViolations).flat().length;

  if (isCountOnly) {
    process.stdout.write(`${total}\n`);
    process.exit(0);
  }

  if (isJson) {
    process.stdout.write(JSON.stringify({
      violations: allViolations,
      config: {
        rules: RULES.map((r) => ({ name: r.name, severity: r.severity, message: r.message })),
      },
      status: total > 0 ? 'error' : 'ok',
      summary: { filesChecked, violations: total },
    }, null, 2) + '\n');
    process.exit(total > 0 ? 1 : 0);
  }

  if (total === 0) {
    console.log(`No antipatterns detected (${filesChecked} files checked).`);
    process.exit(0);
  }
  console.log(`${total} antipattern${total === 1 ? '' : 's'} detected in ${Object.keys(allViolations).length} files (${filesChecked} checked):\n`);
  for (const [file, violations] of Object.entries(allViolations)) {
    console.log(file);
    for (const v of violations) {
      console.log(`  Line ${v.line}: [${v.pattern}] ${v.value} - ${v.message}`);
    }
    console.log();
  }
  process.exit(1);
}

main();
