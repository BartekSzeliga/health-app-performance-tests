// Pays one Przelewy24 SANDBOX transaction in a headless browser, like a customer would: opens the checkout URL,
// picks a test payment method and confirms it. Screenshots of every step go to the output directory.
// Usage: node scripts/isolation/pay_sandbox.mjs <checkoutUrl> <outputDir> [explore]
// Uses Playwright from the sibling web repository (no extra install). Only sandbox.przelewy24.pl is allowed.
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

const require = createRequire(new URL("../../../health-app-frontend/package.json", import.meta.url));
const { chromium } = require("@playwright/test");
const [url, outDir, mode] = process.argv.slice(2);
if (!url || !new URL(url).hostname.endsWith("sandbox.przelewy24.pl")) {
  throw new Error("Only Przelewy24 SANDBOX checkout URLs are allowed");
}
fs.mkdirSync(outDir, { recursive: true });
const browser = await chromium.launch({ channel: "chrome", headless: true });
const page = await browser.newPage({ locale: "pl-PL", viewport: { width: 1280, height: 900 } });
let step = 0;
const shot = async (name) => {
  step += 1;
  await page.screenshot({ path: path.join(outDir, `${String(step).padStart(2, "0")}-${name}.png`), fullPage: true });
};
try {
  await page.goto(url, { waitUntil: "networkidle", timeout: 60000 });
  await shot("checkout");
  if (mode === "explore") {
    console.log(JSON.stringify({ url: page.url(), title: await page.title() }));
    console.log((await page.locator("body").innerText()).slice(0, 2000));
  } else {
    // Cookie notice, bank transfer, the sandbox test bank ("Mój bank - testowy system płatności"), "Zapłać".
    await page.getByText("Rozumiem").click().catch(() => undefined);
    await page.getByText("Przelew online i tradycyjny").click();
    await page.waitForTimeout(1500);
    await shot("banks");
    const before = page.url();
    await page.getByText("mBank").first().click();
    await page.waitForURL((u) => u.toString() !== before, { timeout: 45000 });
    await page.waitForLoadState("networkidle").catch(() => undefined);
    await shot("test-bank");
    await page.getByRole("button", { name: "Zapłać", exact: true }).click();
    await page.waitForURL((u) => !u.hostname.endsWith("przelewy24.pl"), { timeout: 60000 }).catch(() => undefined);
    await shot("returned");
    console.log(JSON.stringify({ finalUrl: page.url() }));
  }
} finally {
  await browser.close();
}
