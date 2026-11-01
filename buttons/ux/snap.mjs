// UX snapshots for the buttons page. Loads the local preview
// server (serve.sh) at phone + desktop widths, clicks each tab,
// and saves screenshots under <outdir> (default buttons/ux/shots).
//
// Usage: node snap.mjs [baseUrl] [outdir]
//   baseUrl default http://127.0.0.1:18099/buttons
//   outdir default buttons/ux/shots
//
// Self-contained Playwright: imports "playwright" directly (pinned in package.json).
// Automatically resolves available Chromium / chrome-headless-shell binary if needed,
// and launches with --no-sandbox.

import { existsSync, readdirSync, mkdirSync } from "fs";
import { dirname, join } from "path";
import { fileURLToPath } from "url";
import { chromium } from "playwright";

const __dirname = dirname(fileURLToPath(import.meta.url));
const defaultOutDir = join(__dirname, "shots");

const base = process.argv[2] || "http://127.0.0.1:18099/buttons";
const outdir = process.argv[3] || defaultOutDir;
mkdirSync(outdir, { recursive: true });

const TABS = ["actions", "prs", "briefing", "pi", "project", "jobs", "ledger"];
const WIDTHS = [
  { name: "phone", width: 390, height: 844 },
  { name: "desktop", width: 1440, height: 900 },
];

function scanPlaywrightDir(pwDir) {
  if (!existsSync(pwDir)) return undefined;
  let entries = [];
  try {
    entries = readdirSync(pwDir);
  } catch {
    return undefined;
  }

  for (const entry of entries) {
    if (entry.startsWith("chromium_headless_shell-")) {
      const p = join(pwDir, entry, "chrome-headless-shell-linux64", "chrome-headless-shell");
      if (existsSync(p)) return p;
    }
  }

  for (const entry of entries) {
    if (entry.startsWith("chromium-")) {
      const p = join(pwDir, entry, "chrome-linux64", "chrome");
      if (existsSync(p)) return p;
    }
  }

  return undefined;
}

function findChromiumExecutable() {
  if (process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH && existsSync(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH)) {
    return process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
  }

  const rootCheck = scanPlaywrightDir("/root/.cache/ms-playwright");
  if (rootCheck) return rootCheck;

  if (existsSync("/home")) {
    try {
      for (const u of readdirSync("/home")) {
        const p = scanPlaywrightDir(join("/home", u, ".cache/ms-playwright"));
        if (p) return p;
      }
    } catch {}
  }
  return undefined;
}

const execPath = findChromiumExecutable();
const launchOptions = {
  headless: true,
  args: ["--no-sandbox", "--disable-setuid-sandbox"],
};

if (execPath) {
  launchOptions.executablePath = execPath;
  console.log(`Using chromium executable: ${execPath}`);
} else {
  console.log("Using default Playwright chromium browser.");
}

const browser = await chromium.launch(launchOptions);
const errors = [];

try {
  for (const w of WIDTHS) {
    const page = await browser.newPage({
      viewport: { width: w.width, height: w.height },
    });
    page.on("pageerror", (e) => errors.push(`${w.name}: ${e.message}`));

    console.log(`Loading ${base} at ${w.name} (${w.width}x${w.height})...`);
    await page.goto(base, { waitUntil: "networkidle" });

    for (const tab of TABS) {
      console.log(`  Clicking tab [data-tab="${tab}"]...`);
      const tabSelector = `button[data-tab="${tab}"]`;
      await page.waitForSelector(tabSelector, { state: "visible" });
      await page.click(tabSelector);
      // Wait for content rendering and transition
      await page.waitForTimeout(600);
      const shotPath = join(outdir, `${w.name}-${tab}.png`);
      await page.screenshot({ path: shotPath });
      console.log(`  Saved screenshot: ${shotPath}`);
    }
    await page.close();
  }
} finally {
  await browser.close();
}

console.log("Screenshots captured in:", outdir);
if (errors.length > 0) {
  console.error("Page errors detected:", JSON.stringify(errors, null, 2));
  process.exit(1);
} else {
  console.log("Zero page errors.");
}
