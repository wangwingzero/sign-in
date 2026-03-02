const FAILED_REPORT_PATH = "failed_sites.json";
const STORAGE_KEYS = {
  baseAccounts: "manual_patch_base_accounts",
  resultAccounts: "manual_patch_result_accounts",
  failedReportOverrideEnabled: "manual_patch_failed_report_override_enabled",
  failedReportOverridePayload: "manual_patch_failed_report_override_payload",
  failedReportOverrideFileName: "manual_patch_failed_report_override_file_name",
  linuxdoAccounts: "linuxdo_extracted_accounts",
};

const statusEl = document.getElementById("status");
const failedMetaEl = document.getElementById("failedMeta");
const failedPreviewEl = document.getElementById("failedPreview");
const importFailedBtn = document.getElementById("importFailedBtn");
const failedFileInputEl = document.getElementById("failedFileInput");
const openMergeToolBtn = document.getElementById("openMergeToolBtn");
const baseAccountsEl = document.getElementById("baseAccounts");
const resultJsonEl = document.getElementById("resultJson");
const extractSummaryEl = document.getElementById("extractSummary");

const refreshBtn = document.getElementById("refreshBtn");
const openFailedBtn = document.getElementById("openFailedBtn");
const extractBtn = document.getElementById("extractBtn");
const extractCurrentBtn = document.getElementById("extractCurrentBtn");
const copyBtn = document.getElementById("copyBtn");

let failedSites = [];

function setStatus(message) {
  statusEl.textContent = message;
}

function storageGet(keys) {
  return new Promise((resolve) => {
    chrome.storage.local.get(keys, resolve);
  });
}

function storageSet(payload) {
  return new Promise((resolve) => {
    chrome.storage.local.set(payload, resolve);
  });
}

function storageRemove(keys) {
  return new Promise((resolve) => {
    chrome.storage.local.remove(keys, resolve);
  });
}

function safeJsonParse(text, fallback) {
  try {
    return JSON.parse(text);
  } catch {
    return fallback;
  }
}

function normalizeProvider(value) {
  return String(value || "").trim().toLowerCase();
}

function normalizeFailedSite(site) {
  return {
    provider: normalizeProvider(site.provider),
    account_name: String(site.account_name || "").trim(),
    api_user: String(site.api_user || "").trim(),
    site_url: String(site.site_url || "").trim(),
    login_url: String(site.login_url || "").trim(),
    oauth_login_url: String(site.oauth_login_url || "").trim(),
    reason: String(site.reason || "").trim(),
  };
}

function normalizeFailedReport(report, defaultSource = "unknown") {
  const failed = Array.isArray(report?.failed_sites) ? report.failed_sites : [];
  return {
    generated_at: report?.generated_at || new Date().toISOString(),
    source: String(report?.source || defaultSource),
    failed_count: failed.length,
    failed_sites: failed,
  };
}

function parseDomainFromUrl(rawUrl) {
  try {
    return new URL(rawUrl).hostname;
  } catch {
    return "";
  }
}

function normalizeHostname(hostname) {
  return String(hostname || "").trim().toLowerCase().replace(/^\.+/, "");
}

function isSameOrParentDomain(hostname, candidateDomain) {
  const host = normalizeHostname(hostname);
  const candidate = normalizeHostname(candidateDomain);
  if (!host || !candidate) return false;
  return host === candidate || host.endsWith(`.${candidate}`);
}

function getCookie(details) {
  return new Promise((resolve, reject) => {
    chrome.cookies.get(details, (cookie) => {
      const lastError = chrome.runtime.lastError;
      if (lastError) {
        reject(new Error(lastError.message || "读取 cookie 失败"));
        return;
      }
      resolve(cookie || null);
    });
  });
}

function getCookies(details) {
  return new Promise((resolve, reject) => {
    chrome.cookies.getAll(details, (cookies) => {
      const lastError = chrome.runtime.lastError;
      if (lastError) {
        reject(new Error(lastError.message || "读取 cookies 失败"));
        return;
      }
      resolve(Array.isArray(cookies) ? cookies : []);
    });
  });
}

function normalizeAccountRecord(input) {
  if (!input || typeof input !== "object") return null;
  const provider = normalizeProvider(input.provider || "");
  const apiUser = String(input.api_user || "").trim();
  if (!provider || !apiUser) return null;

  const name = String(input.name || `${provider}_${apiUser}`).trim() || `${provider}_${apiUser}`;
  const session = String(input.cookies?.session || "").trim();
  return {
    name,
    provider,
    cookies: { session },
    api_user: apiUser,
  };
}

function parseAccountsJsonArray(raw, sourceLabel = "账号配置") {
  const parsed = safeJsonParse(raw, null);
  if (!Array.isArray(parsed)) {
    throw new Error(`${sourceLabel} 不是 JSON 数组`);
  }
  return parsed.map(normalizeAccountRecord).filter(Boolean);
}

function dedupeByKey(items, keyFn) {
  const map = new Map();
  items.forEach((item) => {
    const key = keyFn(item);
    if (key) {
      map.set(key, item);
    }
  });
  return Array.from(map.values());
}

function renderFailedSites(report) {
  const normalized = normalizeFailedReport(report);
  const sites = normalized.failed_sites.map(normalizeFailedSite);
  failedSites = dedupeByKey(sites, (x) => `${x.provider}_${x.account_name}_${x.api_user}`);

  const generatedAt = normalized.generated_at
    ? String(normalized.generated_at).replace("T", " ").slice(0, 19)
    : "未知";
  const source = normalized.source ? ` | 来源 ${normalized.source}` : "";
  failedMetaEl.textContent = `失败 ${failedSites.length} 个 | 生成时间 ${generatedAt}${source}`;

  if (!failedSites.length) {
    failedPreviewEl.textContent = "暂无失败站点";
    return;
  }

  const lines = failedSites.slice(0, 8).map((site, idx) => {
    return `${idx + 1}. ${site.provider} / ${site.account_name || "未命名"}\n   ${site.reason || "无失败原因"}`;
  });
  if (failedSites.length > 8) {
    lines.push(`... 还有 ${failedSites.length - 8} 个`);
  }
  failedPreviewEl.textContent = lines.join("\n");
}

async function loadFailedReport() {
  const url = `${chrome.runtime.getURL(FAILED_REPORT_PATH)}?t=${Date.now()}`;
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`读取失败清单失败: HTTP ${response.status}`);
  }
  const report = normalizeFailedReport(await response.json(), "local");
  renderFailedSites(report);
}

async function importFailedReportFromFile(file) {
  const raw = await file.text();
  const parsed = safeJsonParse(raw, null);
  if (!parsed || !Array.isArray(parsed.failed_sites)) {
    throw new Error("导入失败：文件不是有效的 failed_sites.json 格式");
  }

  const report = normalizeFailedReport(parsed, "email_import");
  renderFailedSites(report);

  await storageSet({
    [STORAGE_KEYS.failedReportOverrideEnabled]: true,
    [STORAGE_KEYS.failedReportOverridePayload]: JSON.stringify(report),
    [STORAGE_KEYS.failedReportOverrideFileName]: file.name,
  });

  setStatus(`已导入失败清单：${file.name}（${failedSites.length} 个）。`);
}

async function clearImportedFailedReport() {
  await storageRemove([
    STORAGE_KEYS.failedReportOverrideEnabled,
    STORAGE_KEYS.failedReportOverridePayload,
    STORAGE_KEYS.failedReportOverrideFileName,
  ]);
}

async function restoreImportedFailedReport() {
  const data = await storageGet([
    STORAGE_KEYS.failedReportOverrideEnabled,
    STORAGE_KEYS.failedReportOverridePayload,
    STORAGE_KEYS.failedReportOverrideFileName,
  ]);

  if (!data[STORAGE_KEYS.failedReportOverrideEnabled]) {
    return false;
  }

  const payload = String(data[STORAGE_KEYS.failedReportOverridePayload] || "").trim();
  if (!payload) {
    return false;
  }

  const parsed = safeJsonParse(payload, null);
  if (!parsed || !Array.isArray(parsed.failed_sites)) {
    return false;
  }

  renderFailedSites(normalizeFailedReport(parsed, "email_import"));
  const fileName = String(data[STORAGE_KEYS.failedReportOverrideFileName] || "导入文件");
  setStatus(`已加载导入失败清单：${fileName}。可点“刷新失败清单”切回本地文件。`);
  return true;
}

function pickOpenUrl(site) {
  return site.login_url || site.oauth_login_url || site.site_url || "";
}

async function openAllFailedSites() {
  if (!failedSites.length) {
    setStatus("失败清单为空，先刷新。");
    return;
  }

  const urls = dedupeByKey(
    failedSites.map(pickOpenUrl).filter(Boolean),
    (x) => x
  );

  if (!urls.length) {
    setStatus("失败清单里没有可打开的 URL。");
    return;
  }

  for (const url of urls) {
    await chrome.tabs.create({ url, active: false });
    await new Promise((resolve) => setTimeout(resolve, 180));
  }

  setStatus(`已打开 ${urls.length} 个失败站点，请逐个人工登录。`);
}

async function getSessionByDomain(domain) {
  const normalizedDomain = normalizeHostname(domain);
  if (!normalizedDomain) return "";
  try {
    const cookies = await getCookies({ domain: normalizedDomain });
    const hit = cookies.find((c) => c.name === "session" && c.value);
    return hit ? hit.value : "";
  } catch {
    return "";
  }
}

async function getSessionByUrl(rawUrl) {
  const url = String(rawUrl || "").trim();
  if (!url) return "";

  try {
    const hit = await getCookie({ url, name: "session" });
    if (hit?.value) return String(hit.value);
  } catch {}

  const domain = parseDomainFromUrl(url);
  return getSessionByDomain(domain);
}

async function getApiUserFromTabId(tabId) {
  if (!tabId) return "";

  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        const keys = ["user", "newapi_user", "profile"];
        for (const k of keys) {
          const v = localStorage.getItem(k);
          if (!v) continue;
          try {
            const obj = JSON.parse(v);
            if (obj && (obj.id || obj.api_user || obj.user_id)) {
              return String(obj.id || obj.api_user || obj.user_id);
            }
          } catch {}
        }
        return "";
      },
    });
    return String(results?.[0]?.result || "").trim();
  } catch {
    return "";
  }
}

async function getApiUserFromOpenTab(domain) {
  const normalizedDomain = normalizeHostname(domain);
  if (!normalizedDomain) return "";

  const tabs = await chrome.tabs.query({ url: [`*://${normalizedDomain}/*`, `*://*.${normalizedDomain}/*`] });
  if (!tabs.length) return "";
  return getApiUserFromTabId(tabs[0].id);
}

function parseBaseAccounts() {
  const raw = baseAccountsEl.value.trim();
  if (!raw) return [];
  return parseAccountsJsonArray(raw, "当前 NEWAPI_ACCOUNTS");
}

function mergeAccountPair(existing, incoming) {
  const incomingSession = String(incoming?.cookies?.session || "").trim();
  const existingSession = String(existing?.cookies?.session || "").trim();
  return {
    name: String(incoming?.name || existing?.name || "").trim(),
    provider: normalizeProvider(incoming?.provider || existing?.provider),
    cookies: { session: incomingSession || existingSession },
    api_user: String(incoming?.api_user || existing?.api_user || "").trim(),
  };
}

function mergeAccounts(baseAccounts, newAccounts) {
  const map = new Map();
  const upsert = (item) => {
    const normalized = normalizeAccountRecord(item);
    if (!normalized) return;
    const key = `${normalized.provider}_${normalized.api_user}`;
    const existing = map.get(key);
    if (!existing) {
      map.set(key, normalized);
      return;
    }
    map.set(key, mergeAccountPair(existing, normalized));
  };

  baseAccounts.forEach(upsert);
  newAccounts.forEach(upsert);

  return Array.from(map.values()).sort((a, b) => {
    const byProvider = a.provider.localeCompare(b.provider);
    if (byProvider !== 0) return byProvider;
    return String(a.api_user).localeCompare(String(b.api_user));
  });
}

async function getCurrentActiveTab() {
  const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return tabs[0] || null;
}

function findFailedSiteByHostname(hostname) {
  const target = normalizeHostname(hostname);
  if (!target) return null;

  const exact = failedSites.find((site) => {
    const domain = parseDomainFromUrl(pickOpenUrl(site) || site.site_url);
    return normalizeHostname(domain) === target;
  });
  if (exact) return exact;

  return (
    failedSites.find((site) => {
      const domain = parseDomainFromUrl(pickOpenUrl(site) || site.site_url);
      return isSameOrParentDomain(target, domain) || isSameOrParentDomain(domain, target);
    }) || null
  );
}

function buildCurrentAccountRecord({ matchedSite, provider, apiUser, session }) {
  const providerForOutput = provider || "__FILL_PROVIDER__";
  const apiUserForOutput = apiUser || "__FILL_API_USER__";
  const fallbackName = `${providerForOutput}_${apiUserForOutput}`;
  return {
    name: String(matchedSite?.account_name || fallbackName).trim() || fallbackName,
    provider: providerForOutput,
    cookies: { session },
    api_user: String(apiUserForOutput),
  };
}

async function extractCurrentSiteCookieAndBuildSecret() {
  const tab = await getCurrentActiveTab();
  const tabUrl = String(tab?.url || "").trim();
  if (!tabUrl) {
    setStatus("未找到当前活动标签页，请先打开目标站点。");
    return;
  }

  if (!/^https?:\/\//i.test(tabUrl)) {
    setStatus("当前标签页不是 http/https 页面，无法读取站点 cookie。");
    return;
  }

  const hostname = normalizeHostname(parseDomainFromUrl(tabUrl));
  if (!hostname) {
    setStatus("无法解析当前站点域名。");
    return;
  }

  const session = await getSessionByUrl(tabUrl);
  if (!session) {
    setStatus(`当前站点 ${hostname} 未找到 session cookie。请先确认已登录并刷新页面。`);
    extractSummaryEl.textContent = `当前站点: ${hostname}\n未找到 session cookie。`;
    return;
  }

  let baseAccounts = [];
  try {
    baseAccounts = parseBaseAccounts();
  } catch (e) {
    setStatus(`解析当前 NEWAPI_ACCOUNTS 失败: ${e.message}`);
    return;
  }

  const matchedSite = findFailedSiteByHostname(hostname);
  const provider = normalizeProvider(matchedSite?.provider || "");
  const apiUserFromPage = await getApiUserFromTabId(tab?.id);
  const apiUser = String(apiUserFromPage || matchedSite?.api_user || "").trim();

  const currentRecord = buildCurrentAccountRecord({
    matchedSite,
    provider,
    apiUser,
    session,
  });

  const missingFields = [];
  if (!provider) missingFields.push("provider");
  if (!apiUser) missingFields.push("api_user");

  const merged = missingFields.length
    ? [currentRecord]
    : mergeAccounts(baseAccounts, [currentRecord]);

  resultJsonEl.value = JSON.stringify(merged, null, 2);

  const lines = [];
  lines.push(`当前站点: ${hostname}`);
  lines.push(`session: 已获取（长度 ${session.length}）`);
  if (matchedSite) {
    lines.push(`匹配失败清单: ${matchedSite.provider || "unknown"} / ${matchedSite.account_name || "unknown"}`);
  }
  if (missingFields.length) {
    lines.push(`缺少字段: ${missingFields.join(", ")}（已填占位符）`);
  } else {
    lines.push(`已合并结果，共 ${merged.length} 条。`);
  }
  extractSummaryEl.textContent = lines.join("\n");

  if (missingFields.length) {
    setStatus(`已提取当前站点 cookie，但缺少 ${missingFields.join(", ")}，请先改占位符再使用。`);
  } else {
    setStatus(`完成：已提取当前站点并生成 NEWAPI_ACCOUNTS（共 ${merged.length} 条）。`);
  }

  await storageSet({
    [STORAGE_KEYS.baseAccounts]: baseAccountsEl.value,
    [STORAGE_KEYS.resultAccounts]: resultJsonEl.value,
  });
}

async function extractFailedCookiesAndBuildSecret() {
  if (!failedSites.length) {
    setStatus("失败清单为空，先刷新。");
    return;
  }

  let baseAccounts = [];
  try {
    baseAccounts = parseBaseAccounts();
  } catch (e) {
    setStatus(`解析当前 NEWAPI_ACCOUNTS 失败: ${e.message}`);
    return;
  }

  const extracted = [];
  const missed = [];

  for (const site of failedSites) {
    const openUrl = pickOpenUrl(site) || site.site_url;
    const domain = parseDomainFromUrl(openUrl || site.site_url);
    if (!domain || !site.provider) {
      missed.push(`${site.provider || "unknown"}/${site.account_name || "unknown"}: 缺少域名或 provider`);
      continue;
    }

    const session = await getSessionByDomain(domain);
    if (!session) {
      missed.push(`${site.provider}/${site.account_name || "unknown"}: 未找到 session cookie`);
      continue;
    }

    const apiUserFromPage = await getApiUserFromOpenTab(domain);
    const apiUser = apiUserFromPage || site.api_user;
    if (!apiUser) {
      missed.push(`${site.provider}/${site.account_name || "unknown"}: 缺少 api_user`);
      continue;
    }

    extracted.push({
      name: site.account_name || `${site.provider}_${apiUser}`,
      provider: site.provider,
      cookies: { session },
      api_user: String(apiUser),
    });
  }

  const merged = mergeAccounts(baseAccounts, extracted);
  resultJsonEl.value = JSON.stringify(merged, null, 2);

  const lines = [];
  lines.push(`提取成功 ${extracted.length} 个，失败 ${missed.length} 个。`);
  if (missed.length) {
    lines.push("未成功项:");
    missed.slice(0, 12).forEach((x) => lines.push(`- ${x}`));
    if (missed.length > 12) {
      lines.push(`- ... 还有 ${missed.length - 12} 个`);
    }
  }

  extractSummaryEl.textContent = lines.join("\n");
  setStatus(`完成：已生成 NEWAPI_ACCOUNTS（共 ${merged.length} 条）。`);

  await storageSet({
    [STORAGE_KEYS.baseAccounts]: baseAccountsEl.value,
    [STORAGE_KEYS.resultAccounts]: resultJsonEl.value,
  });
}

async function copyResult() {
  const text = resultJsonEl.value.trim();
  if (!text) {
    setStatus("没有可复制的结果 JSON。");
    return;
  }
  await navigator.clipboard.writeText(text);
  setStatus("已复制生成结果，可直接粘贴到 GitHub Secret: NEWAPI_ACCOUNTS。");
}

async function restoreDraft() {
  const data = await storageGet([STORAGE_KEYS.baseAccounts, STORAGE_KEYS.resultAccounts]);
  if (data[STORAGE_KEYS.baseAccounts]) {
    baseAccountsEl.value = data[STORAGE_KEYS.baseAccounts];
  }
  if (data[STORAGE_KEYS.resultAccounts]) {
    resultJsonEl.value = data[STORAGE_KEYS.resultAccounts];
  }
}

refreshBtn.addEventListener("click", async () => {
  try {
    await loadFailedReport();
    await clearImportedFailedReport();
    setStatus("失败清单刷新成功（已切回本地 failed_sites.json）。");
  } catch (e) {
    setStatus(e.message);
  }
});

importFailedBtn.addEventListener("click", () => {
  failedFileInputEl.value = "";
  failedFileInputEl.click();
});

failedFileInputEl.addEventListener("change", async () => {
  const file = failedFileInputEl.files?.[0];
  if (!file) return;

  importFailedBtn.disabled = true;
  try {
    await importFailedReportFromFile(file);
  } catch (e) {
    setStatus(e.message || "导入失败清单失败。");
  } finally {
    importFailedBtn.disabled = false;
    failedFileInputEl.value = "";
  }
});

openMergeToolBtn.addEventListener("click", async () => {
  const url = chrome.runtime.getURL("merge.html");
  await chrome.tabs.create({ url, active: true });
  setStatus("已打开独立 JSON 合并去重工具（新标签页）。");
});

openFailedBtn.addEventListener("click", async () => {
  await openAllFailedSites();
});

extractBtn.addEventListener("click", async () => {
  extractBtn.disabled = true;
  try {
    await extractFailedCookiesAndBuildSecret();
  } finally {
    extractBtn.disabled = false;
  }
});

extractCurrentBtn.addEventListener("click", async () => {
  extractCurrentBtn.disabled = true;
  try {
    await extractCurrentSiteCookieAndBuildSecret();
  } finally {
    extractCurrentBtn.disabled = false;
  }
});

copyBtn.addEventListener("click", async () => {
  await copyResult();
});

// ==================== LinuxDO Cookie 提取 ====================

const linuxdoExtractBtn = document.getElementById("linuxdoExtractBtn");
const linuxdoClearBtn = document.getElementById("linuxdoClearBtn");
const linuxdoGenerateBtn = document.getElementById("linuxdoGenerateBtn");
const linuxdoCopyBtn = document.getElementById("linuxdoCopyBtn");
const linuxdoAccountListEl = document.getElementById("linuxdoAccountList");
const linuxdoMetaEl = document.getElementById("linuxdoMeta");
const linuxdoResultJsonEl = document.getElementById("linuxdoResultJson");
const linuxdoBaseAccountsEl = document.getElementById("linuxdoBaseAccounts");

const LINUXDO_DOMAIN = "linux.do";
const LINUXDO_COOKIE_NAMES = ["_t", "_forum_session"];

// 内存中保存已提取的 LinuxDO 账号列表
let linuxdoExtractedAccounts = [];

async function loadLinuxdoAccounts() {
  const data = await storageGet([STORAGE_KEYS.linuxdoAccounts]);
  const raw = data[STORAGE_KEYS.linuxdoAccounts];
  if (raw) {
    linuxdoExtractedAccounts = safeJsonParse(raw, []);
  }
  renderLinuxdoAccountList();
}

async function saveLinuxdoAccounts() {
  await storageSet({
    [STORAGE_KEYS.linuxdoAccounts]: JSON.stringify(linuxdoExtractedAccounts),
  });
}

function renderLinuxdoAccountList() {
  if (!linuxdoExtractedAccounts.length) {
    linuxdoAccountListEl.textContent = "尚未提取任何账号";
    linuxdoMetaEl.textContent = "登录 linux.do 后点击提取，逐个账号操作";
    return;
  }

  const lines = linuxdoExtractedAccounts.map((acc, idx) => {
    const cookieKeys = Object.keys(acc.cookies || {});
    const display = acc.email || acc.username || "未知用户";
    const forumTag = acc.forum_username ? ` [${acc.forum_username}]` : "";
    return `${idx + 1}. ${display}${forumTag} (${cookieKeys.length} 个 cookie)`;
  });
  linuxdoAccountListEl.textContent = lines.join("\n");
  linuxdoMetaEl.textContent = `已提取 ${linuxdoExtractedAccounts.length} 个账号的 Cookie`;
}

async function getLinuxdoUserInfo(tab) {
  // 返回 { username, email }，通过 Discourse API 分两步获取
  if (!tab?.id) return { username: "", email: "" };
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: async () => {
        let username = "";
        let email = "";

        // 第一步：获取用户名
        // 方法1: 通过 Discourse session API
        try {
          const resp = await fetch("/session/current.json", { credentials: "include" });
          if (resp.ok) {
            const data = await resp.json();
            username = data?.current_user?.username || "";
          }
        } catch {}

        // 方法2: 从 meta 标签
        if (!username) {
          const meta = document.querySelector('meta[name="current-user-username"]');
          if (meta?.content) username = meta.content;
        }

        // 方法3: 从 Discourse PreloadStore
        if (!username) {
          try {
            const preloaded = document.getElementById("data-preloaded");
            if (preloaded?.dataset?.preloaded) {
              const data = JSON.parse(preloaded.dataset.preloaded);
              const currentUser = data["currentUser"] ? JSON.parse(data["currentUser"]) : null;
              if (currentUser?.username) username = currentUser.username;
            }
          } catch {}
        }

        // 方法4: 从页面链接
        if (!username) {
          const link = document.querySelector("#current-user a[href*='/u/'], a.icon[href*='/u/']");
          if (link?.href) {
            const m = link.href.match(/\/u\/([^/]+)/);
            if (m) username = m[1];
          }
        }

        // 第二步：用用户名获取邮箱
        if (username) {
          try {
            const resp = await fetch(`/u/${username}/emails.json`, { credentials: "include" });
            if (resp.ok) {
              const data = await resp.json();
              email = data?.email || "";
            }
          } catch {}
        }

        return { username, email };
      },
    });
    const result = results?.[0]?.result || {};
    return {
      username: String(result.username || "").trim(),
      email: String(result.email || "").trim(),
    };
  } catch {
    return { username: "", email: "" };
  }
}

async function extractLinuxdoCookies() {
  // 检查当前标签页是否在 linux.do
  const tab = await getCurrentActiveTab();
  const tabUrl = String(tab?.url || "").trim();

  if (!tabUrl.includes("linux.do")) {
    setStatus("请先在浏览器中打开 linux.do 并登录，再点击提取。");
    return;
  }

  // 提取关键 Cookie
  const cookies = {};
  for (const name of LINUXDO_COOKIE_NAMES) {
    try {
      const cookie = await getCookie({ url: "https://linux.do", name });
      if (cookie?.value) {
        cookies[name] = cookie.value;
      }
    } catch {}
  }

  // 也尝试获取 cf_clearance（Cloudflare cookie，可选）
  try {
    const cfCookie = await getCookie({ url: "https://linux.do", name: "cf_clearance" });
    if (cfCookie?.value) {
      cookies["cf_clearance"] = cfCookie.value;
    }
  } catch {}

  if (!cookies._t && !cookies._forum_session) {
    setStatus("未找到 LinuxDO 登录 Cookie（_t / _forum_session）。请确认已登录。");
    return;
  }

  // 获取当前登录的用户信息（邮箱 + 用户名）
  const userInfo = await getLinuxdoUserInfo(tab);
  // 优先用邮箱作为标识（与 LINUXDO_ACCOUNTS 的 username 字段一致）
  const identifier = userInfo.email || userInfo.username || "";

  // 检查是否已提取过该用户（按邮箱或用户名匹配）
  const existingIdx = linuxdoExtractedAccounts.findIndex((acc) => {
    if (!identifier) return false;
    const id = identifier.toLowerCase();
    return (
      (acc.username && acc.username.toLowerCase() === id) ||
      (acc.email && acc.email.toLowerCase() === id) ||
      (acc.forum_username && acc.forum_username.toLowerCase() === id)
    );
  });

  const accountEntry = {
    username: identifier || `账号${linuxdoExtractedAccounts.length + 1}`,
    forum_username: userInfo.username || "",
    email: userInfo.email || "",
    cookies,
    extracted_at: new Date().toISOString(),
  };

  if (existingIdx >= 0) {
    // 更新已有记录
    linuxdoExtractedAccounts[existingIdx] = accountEntry;
    setStatus(`已更新 ${accountEntry.username} 的 Cookie（${Object.keys(cookies).length} 个）`);
  } else {
    linuxdoExtractedAccounts.push(accountEntry);
    setStatus(`已提取 ${accountEntry.username} 的 Cookie（${Object.keys(cookies).length} 个）`);
  }

  await saveLinuxdoAccounts();
  renderLinuxdoAccountList();
}

function generateLinuxdoAccountsJson() {
  if (!linuxdoExtractedAccounts.length) {
    setStatus("没有已提取的 LinuxDO 账号，请先提取。");
    return;
  }

  // 尝试解析已有的 LINUXDO_ACCOUNTS 配置（用于合并保留 password/browse_minutes）
  let existingAccounts = [];
  const baseRaw = linuxdoBaseAccountsEl.value.trim();
  if (baseRaw) {
    try {
      existingAccounts = JSON.parse(baseRaw);
      if (!Array.isArray(existingAccounts)) {
        setStatus("现有 LINUXDO_ACCOUNTS 不是 JSON 数组，请检查格式。");
        return;
      }
    } catch {
      setStatus("现有 LINUXDO_ACCOUNTS JSON 解析失败，请检查格式。");
      return;
    }
  }

  // 构建多维度匹配映射（邮箱、用户名、name 都可能匹配）
  const existingMap = new Map();
  for (const acc of existingAccounts) {
    const keys = [
      String(acc.username || "").trim().toLowerCase(),
      String(acc.name || "").trim().toLowerCase(),
    ].filter(Boolean);
    for (const key of keys) {
      if (!existingMap.has(key)) existingMap.set(key, acc);
    }
  }

  function findExisting(acc) {
    // 按邮箱、username、论坛用户名依次匹配
    const candidates = [
      String(acc.email || "").trim().toLowerCase(),
      String(acc.username || "").trim().toLowerCase(),
      String(acc.forum_username || "").trim().toLowerCase(),
    ].filter(Boolean);
    for (const key of candidates) {
      const found = existingMap.get(key);
      if (found) return found;
    }
    return null;
  }

  const accounts = linuxdoExtractedAccounts.map((acc) => {
    // 把 cookies 字典转成字符串格式: "_t=xxx; _forum_session=xxx"
    const cookieStr = Object.entries(acc.cookies || {})
      .map(([k, v]) => `${k}=${v}`)
      .join("; ");

    const existing = findExisting(acc);
    // 优先用邮箱作为 username（与 LINUXDO_ACCOUNTS 格式一致）
    const finalUsername = existing?.username || acc.email || acc.username || "";

    // 合并：用提取的新 cookie，保留已有的 password/browse_minutes/name 等
    const result = {
      username: finalUsername,
      password: existing?.password || "",
      name: existing?.name || acc.forum_username || finalUsername,
      browse_minutes: existing?.browse_minutes ?? 20,
      cookies: cookieStr,
    };
    // 保留已有配置中的 checkin_sites / exclude_sites
    if (existing?.checkin_sites) result.checkin_sites = existing.checkin_sites;
    if (existing?.exclude_sites) result.exclude_sites = existing.exclude_sites;
    return result;
  });

  // 把没有被提取到新 cookie 的已有账号也保留（原样输出）
  for (const existing of existingAccounts) {
    const existingKeys = [
      String(existing.username || "").trim().toLowerCase(),
      String(existing.name || "").trim().toLowerCase(),
    ].filter(Boolean);
    const alreadyIncluded = accounts.some((a) => {
      const aKey = (a.username || "").toLowerCase();
      return existingKeys.includes(aKey);
    });
    if (!alreadyIncluded && existingKeys.length) {
      accounts.push({ ...existing });
    }
  }

  const json = JSON.stringify(accounts, null, 2);
  linuxdoResultJsonEl.value = json;

  const mergedCount = existingAccounts.length ? `（已合并 ${existingAccounts.length} 个现有配置）` : "";
  setStatus(`已生成 LINUXDO_ACCOUNTS（${accounts.length} 个账号）${mergedCount}。可直接复制使用。`);
}

async function copyLinuxdoResult() {
  const text = linuxdoResultJsonEl.value.trim();
  if (!text) {
    setStatus("没有可复制的 LinuxDO 结果。先点「生成」。");
    return;
  }
  await navigator.clipboard.writeText(text);
  setStatus("已复制 LINUXDO_ACCOUNTS，粘贴到 GitHub Secret 即可。");
}

async function clearLinuxdoAccounts() {
  linuxdoExtractedAccounts = [];
  await saveLinuxdoAccounts();
  renderLinuxdoAccountList();
  linuxdoResultJsonEl.value = "";
  setStatus("已清空 LinuxDO 提取记录。");
}

linuxdoExtractBtn.addEventListener("click", async () => {
  linuxdoExtractBtn.disabled = true;
  try {
    await extractLinuxdoCookies();
  } finally {
    linuxdoExtractBtn.disabled = false;
  }
});

linuxdoClearBtn.addEventListener("click", async () => {
  await clearLinuxdoAccounts();
});

linuxdoGenerateBtn.addEventListener("click", () => {
  generateLinuxdoAccountsJson();
});

linuxdoCopyBtn.addEventListener("click", async () => {
  await copyLinuxdoResult();
});

(async function init() {
  await restoreDraft();
  await loadLinuxdoAccounts();
  const restored = await restoreImportedFailedReport();
  if (restored) {
    return;
  }
  try {
    await loadFailedReport();
    setStatus("已加载失败站点清单。按顺序：打开站点 -> 人工登录 -> 提取生成。 ");
  } catch (e) {
    setStatus(`${e.message}。请先 pull 最新仓库。`);
  }
})();
