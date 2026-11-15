import { defineConfig } from "vitest/config";

// Unit harness for the extension's pure logic (extension/lib/ ES modules).
// happy-dom backs the fixture-driven tests the same way the frontend suite
// runs; no bundler or Chrome launch is involved.
export default defineConfig({
  test: {
    environment: "happy-dom",
    include: ["tests/**/*.test.js"]
  }
});
