// UX snapshots for the buttons page. Loads the local preview
// server (serve.sh) at phone + desktop widths, clicks each tab,
// and saves screenshots under <outdir> (default /tmp/buttons-ux).
//
// Usage: node snap.mjs [baseUrl] [outdir]
//   baseUrl default http://localhost:18099/buttons
//
// Playwright recipe that works on the S5 dev VM:
// createRequire('/home/hatch/workspace/portfolio-src/package.json')
// and launch with the pinned chromium executable + --no-sandbox.
import { mkdirSync } from "fs";
import { createRequire } from "module";

const req = createRequire("/home/hatch/workspace/portfolio-src/package.json");
const { chromium } = req("@playwright/test");

const base = process.argv[2] || "http://localhost:18099/buttons";
const outdir = process.argv[3] || "/tmp/buttons-ux";
mkdirSync(outdir, { recursive: true });

const TABS = ["actions", "briefing", "pi", "project", "jobs", "ledger"];
const WIDTHS = [
  { name: "phone", width: 390, height: 844 },
  { name: "desktop", width: 1440, height: 900 },
];

const browser = await chromium.launch({
  executablePath:
    "/home/hatch/.cache/ms-playwright/chromium-1248/chrome-linux64/chrome",
  args: ["--no-sandbox"],
});
const errors = [];
for (const w of WIDTHS) {
  const page = await browser.newPage({
    viewport: { width: w.width, height: w.height },
  });
  page.on("pageerror", (e) => errors.push(`${w.name}: ${e.message}`));
  await page.goto(base, { waitUntil: "networkidle" });
  for (const tab of TABS) {
    await page.click(`button[data-tab="${tab}"]`);
    await page.waitForTimeout(1200);
    await page.screenshot({ path: `${outdir}/${w.name}-${tab}.png` });
  }
  await page.close();
}
await browser.close();
console.log("screenshots in", outdir);
console.log("page errors:", JSON.stringify(errors));
