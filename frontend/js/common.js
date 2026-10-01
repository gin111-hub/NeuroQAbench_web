// Shared helpers for both pages.
// All fetches go through qbFetch so a future auth / header change happens in one place.

window.QB_HEADERS = { "X-QAbench-Client": "1" };

window.qbFetch = async function (url, opts = {}) {
  const headers = Object.assign({}, window.QB_HEADERS, opts.headers || {});
  const res = await fetch(url, Object.assign({}, opts, { headers }));
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`fetch ${url} failed: ${res.status} ${text}`);
  }
  return res.json();
};

// A tiny label-shortening helper used by chip rows.
window.qbShortModel = function (name) {
  return name
    .replace("azure-", "")
    .replace("deepseek-", "ds-")
    .replace("qwen3.5-", "qwen-");
};
