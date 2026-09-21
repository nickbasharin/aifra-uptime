// infra/scripts/indexnow.ts
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile, writeFile, mkdir, rename } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
function platformOrigin(value) {
  const url = new URL(value);
  assert(url.protocol === "https:" && !url.username && !url.password);
  assert(url.pathname === "/" && !url.search && !url.hash);
  return url.origin;
}
function sitemapPages(xml, origin) {
  assert(!/<!DOCTYPE|<!ENTITY/i.test(xml), "Unsupported sitemap entities");
  const urls = [...xml.matchAll(/<loc>([^<>]+)<\/loc>/g)].map((match) => {
    const raw = match[1].replaceAll("&amp;", "&");
    const url = new URL(raw);
    assert(url.origin === origin && !url.username && !url.password, "Foreign origin");
    assert(!url.search && !url.hash && !/%|\\/.test(url.pathname), "Noncanonical URL");
    assert(!/^\/(?:p|v1|api|admin|remote)(?:\/|$)/.test(url.pathname), "Private route");
    return url;
  });
  assert(urls.length > 0 && urls.length <= 1e3, "Unexpected sitemap size");
  return [...new Set(urls.filter((url) => url.pathname.endsWith("/")).map(String))];
}
function indexableHash(html, url, robotsHeader = "") {
  assert(!/noindex|none/i.test(robotsHeader), "Header excludes indexing");
  for (const meta of html.matchAll(/<meta\b[^>]*>/gi)) {
    if (/name\s*=\s*["'](?:robots|googlebot|bingbot)["']/i.test(meta[0]))
      assert(!/\b(?:noindex|none)\b/i.test(meta[0]), "Meta excludes indexing");
  }
  assert(
    /<meta\s+name="robots"\s+content="index, follow"/i.test(html),
    "Not indexable"
  );
  const canonical = html.match(/<link\s+rel="canonical"\s+href="([^"]+)"/i)?.[1];
  assert.equal(canonical, url, "Canonical mismatch");
  assert(/<h1(?:\s|>)/i.test(html), "Missing public page");
  return createHash("sha256").update(html).digest("hex");
}
function changedPages(current, previous) {
  if (previous) {
    assert.equal(previous.version, 1);
    assert.equal(previous.origin, current.origin);
  }
  return Object.keys(current.pages).filter(
    (url) => previous?.pages[url] !== current.pages[url]
  );
}
async function notifyIndexNow(options) {
  const origin = platformOrigin(options.origin);
  const fetcher = options.fetcher ?? fetch;
  const get = async (url) => {
    const response2 = await fetcher(url, {
      redirect: "error",
      signal: AbortSignal.timeout(2e4),
      headers: { "User-Agent": "AIfra-IndexNow/1.0" }
    });
    assert.equal(response2.status, 200, `Public URL unavailable: ${url}`);
    return response2;
  };
  const config = await (await get(origin + "/indexnow.json")).json();
  assert.equal(config.origin, origin);
  assert(/^[a-f0-9]{32}$/.test(config.key), "Invalid public verification key");
  const keyLocation = `${origin}/${config.key}.txt`;
  assert.equal((await (await get(keyLocation)).text()).trim(), config.key);
  const urls = sitemapPages(await (await get(origin + "/sitemap.xml")).text(), origin);
  const current = { version: 1, origin, pages: {} };
  for (const url of urls) {
    const response2 = await get(url);
    assert(response2.headers.get("content-type")?.includes("text/html"));
    current.pages[url] = indexableHash(
      await response2.text(),
      url,
      response2.headers.get("x-robots-tag") ?? ""
    );
  }
  let previous;
  try {
    previous = JSON.parse(await readFile(options.stateFile, "utf8"));
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  const changed = changedPages(current, previous);
  if (!options.submit || changed.length === 0)
    return { pagesChecked: urls.length, changed: changed.length, submitted: false };
  const response = await fetcher("https://api.indexnow.org/indexnow", {
    method: "POST",
    redirect: "error",
    signal: AbortSignal.timeout(3e4),
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      host: new URL(origin).host,
      key: config.key,
      keyLocation,
      urlList: changed
    })
  });
  assert(
    [200, 202].includes(response.status),
    `IndexNow rejected notification: ${response.status}`
  );
  await mkdir(path.dirname(options.stateFile), { recursive: true });
  const temp = options.stateFile + ".next";
  await writeFile(temp, JSON.stringify(current, null, 2) + "\n");
  await rename(temp, options.stateFile);
  return {
    pagesChecked: urls.length,
    changed: changed.length,
    submitted: true,
    status: response.status,
    result: response.status === 200 ? "received_not_indexing_proof" : "received_key_validation_pending"
  };
}
if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  const report = await notifyIndexNow({
    origin: process.env.SITE_ORIGIN ?? "",
    stateFile: process.env.INDEXNOW_STATE ?? ".stage5-local/indexnow/state.json",
    submit: process.argv.slice(2).includes("--submit")
  });
  console.log(JSON.stringify({ checkedAt: (/* @__PURE__ */ new Date()).toISOString(), ...report }));
}
export {
  changedPages,
  indexableHash,
  notifyIndexNow,
  platformOrigin,
  sitemapPages
};
