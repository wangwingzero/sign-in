  // NewAPI 站点配置 - 默认站点
const DEFAULT_SITES = {
  wong: { domain: "wzw.pp.ua", name: "WONG公益站", provider: "wong", url: "https://wzw.pp.ua" },
  elysiver: { domain: "elysiver.h-e.top", name: "Elysiver", provider: "elysiver", url: "https://elysiver.h-e.top" },
  kfcapi: { domain: "kfc-api.sxxe.net", name: "KFC API", provider: "kfcapi", url: "https://kfc-api.sxxe.net" },
  duckcoding: { domain: "free.duckcoding.com", name: "Free DuckCoding", provider: "duckcoding", url: "https://free.duckcoding.com" },
  runanytime: { domain: "runanytime.hxi.me", name: "随时跑路", provider: "runanytime", url: "https://runanytime.hxi.me" },
  neb: { domain: "ai.zzhdsgsss.xyz", name: "NEB公益站", provider: "neb", url: "https://ai.zzhdsgsss.xyz" },
  mitchll: { domain: "api.mitchll.com", name: "Mitchll-api", provider: "mitchll", url: "https://api.mitchll.com" },
  kingo: { domain: "new-api-bxhm.onrender.com", name: "Kingo API", provider: "kingo", url: "https://new-api-bxhm.onrender.com" },
  techstar: { domain: "aidrouter.qzz.io", name: "TechnologyStar", provider: "techstar", url: "https://aidrouter.qzz.io" },
  lightllm: { domain: "lightllm.online", name: "轻のLLM", provider: "lightllm", url: "https://lightllm.online" },
  hotaru: { domain: "api.hotaruapi.top", name: "Hotaru API", provider: "hotaru", url: "https://api.hotaruapi.top" },
  dev88: { domain: "api.dev88.tech", name: "DEV88公益站", provider: "dev88", url: "https://api.dev88.tech" },
  huan: { domain: "ai.huan666.de", name: "huan公益站", provider: "huan", url: "https://ai.huan666.de" },
  anyrouter: { domain: "anyrouter.top", name: "AnyRouter", provider: "anyrouter", url: "https://anyrouter.top" },
};

// 当前站点配置（从 storage 加载，可自定义）
let SITES_CONFIG = { ...DEFAULT_SITES };

let extractedConfigs = [];
let savedConfigs = []; // 已保存的配置
let editingSiteId = null; // 当前编辑的站点 ID
let failedSitesReport = { failed_sites: [] }; // GitHub Action 失败站点报告

// 从 storage 加载站点配置
async function loadSitesConfig() {
  return new Promise((resolve) => {
    chrome.storage.local.get(['sites_config'], (result) => {
      if (result.sites_config && Object.keys(result.sites_config).length > 0) {
        SITES_CONFIG = result.sites_config;
      } else {
        SITES_CONFIG = { ...DEFAULT_SITES };
      }
      resolve(SITES_CONFIG);
    });
  });
}

// 保存站点配置到 storage
async function saveSitesConfig() {
  return new Promise((resolve) => {
    chrome.storage.local.set({ sites_config: SITES_CONFIG }, resolve);
  });
}

// 从 storage 加载已保存的配置
async function loadSavedConfigs() {
  return new Promise((resolve) => {
    chrome.storage.local.get(['newapi_configs'], (result) => {
      savedConfigs = result.newapi_configs || [];
      resolve(savedConfigs);
    });
  });
}

// 保存配置到 storage
async function saveConfigs(configs) {
  return new Promise((resolve) => {
    chrome.storage.local.set({ newapi_configs: configs }, resolve);
  });
}

// 智能合并配置（相同 provider+api_user 更新，不同则追加，按 provider 字母排序）
function mergeConfigs(existingConfigs, newConfigs) {
  const merged = [...existingConfigs];
  let updated = 0;
  let added = 0;
  
  for (const newConfig of newConfigs) {
    const key = `${newConfig.provider}_${newConfig.api_user}`;
    const existingIndex = merged.findIndex(
      c => `${c.provider}_${c.api_user}` === key
    );
    
    if (existingIndex >= 0) {
      // 更新已有配置
      merged[existingIndex] = newConfig;
      updated++;
    } else {
      // 追加新配置
      merged.push(newConfig);
      added++;
    }
  }
  
  // 按 provider 字母顺序排序（a-z）
  merged.sort((a, b) => a.provider.localeCompare(b.provider));
  
  return { merged, updated, added };
}

// 去重（provider + api_user）
function dedupeByProviderApiUser(configs) {
  const m = new Map();
  for (const c of configs) {
    if (!c || !c.provider || !c.api_user) continue;
    m.set(`${c.provider}_${c.api_user}`, c);
  }
  return Array.from(m.values()).sort((a, b) => a.provider.localeCompare(b.provider));
}

function buildFailedTemplateAccounts() {
  const failedSites = Array.isArray(failedSitesReport.failed_sites) ? failedSitesReport.failed_sites : [];
  const templates = [];

  failedSites.forEach((site, idx) => {
    const provider = String(site.provider || "unknown").trim();
    if (!provider) return;
    const rawApiUser = String(site.api_user || "").trim();
    const apiUser = rawApiUser || `REPLACE_ME_${provider}_${idx + 1}`;
    const accountName = site.account_name || `${provider}_${apiUser}`;

    templates.push({
      name: accountName,
      provider: provider,
      cookies: { session: "REPLACE_ME_SESSION" },
      api_user: apiUser,
    });
  });

  return dedupeByProviderApiUser(templates);
}

function renderFailedSitesPreview() {
  const failedSites = Array.isArray(failedSitesReport.failed_sites) ? failedSitesReport.failed_sites : [];
  if (!failedSites.length) {
    failedSitesMeta.textContent = "0 个失败";
    failedSitesPreview.textContent = "暂无失败站点数据";
    return;
  }

  const generatedAt = failedSitesReport.generated_at || "";
  const generatedText = generatedAt ? generatedAt.replace("T", " ").slice(0, 19) : "未知时间";
  failedSitesMeta.textContent = `${failedSites.length} 个失败 · ${generatedText}`;

  const lines = failedSites.slice(0, 8).map((site, idx) => {
    const provider = site.provider || "unknown";
    const account = site.account_name || "-";
    const reason = String(site.reason || "").replace(/\s+/g, " ").slice(0, 60);
    return `${idx + 1}. ${provider} / ${account}\n   ${reason}`;
  });
  if (failedSites.length > 8) {
    lines.push(`... 还有 ${failedSites.length - 8} 个失败站点`);
  }
  failedSitesPreview.textContent = lines.join("\n");
}

async function loadFailedSitesReport(showStatus = true) {
  try {
    const url = `${chrome.runtime.getURL("failed_sites.json")}?t=${Date.now()}`;
    const response = await fetch(url);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    failedSitesReport = data && typeof data === "object" ? data : { failed_sites: [] };
    if (!Array.isArray(failedSitesReport.failed_sites)) {
      failedSitesReport.failed_sites = [];
    }
    renderFailedSitesPreview();
    if (showStatus) {
      setStatus(`📌 已加载失败站点 ${failedSitesReport.failed_sites.length} 个`, "info");
    }
  } catch (e) {
    failedSitesReport = { failed_sites: [] };
    renderFailedSitesPreview();
    if (showStatus) {
      setStatus("⚠️ 未读取到失败站点清单（请先 pull 最新仓库）", "error");
    }
  }
}

async function openFailedSites() {
  const failedSites = Array.isArray(failedSitesReport.failed_sites) ? failedSitesReport.failed_sites : [];
  if (!failedSites.length) {
    setStatus("⚠️ 没有失败站点可打开", "error");
    return;
  }

  const visited = new Set();
  let opened = 0;
  for (const site of failedSites) {
    const target = site.login_url || site.oauth_login_url || site.site_url || "";
    if (!target || visited.has(target)) continue;
    visited.add(target);
    await chrome.tabs.create({ url: target, active: false });
    opened += 1;
    await new Promise((r) => setTimeout(r, 200));
  }

  setStatus(`✅ 已打开 ${opened} 个失败站点页面`, "success");
}

async function copyFailedTemplate() {
  const templates = buildFailedTemplateAccounts();
  if (!templates.length) {
    setStatus("⚠️ 失败站点模板为空", "error");
    return;
  }
  await navigator.clipboard.writeText(JSON.stringify(templates, null, 2));
  setStatus(`✅ 已复制失败站点模板（${templates.length} 条）`, "success");
}

async function copyNewapiAccountsForSecret() {
  await loadSavedConfigs();
  const localConfigs = Array.isArray(savedConfigs) ? savedConfigs : [];
  const anyrouterTemplates = buildFailedTemplateAccounts().filter((x) => x.provider === "anyrouter");
  const merged = dedupeByProviderApiUser([...localConfigs, ...anyrouterTemplates]);

  if (!merged.length) {
    setStatus("⚠️ 没有可导出的 NEWAPI_ACCOUNTS 数据", "error");
    return;
  }

  await navigator.clipboard.writeText(JSON.stringify(merged, null, 2));
  const placeholders = merged.filter((x) => String(x.cookies?.session || "").startsWith("REPLACE_ME")).length;
  const suffix = placeholders > 0 ? `，含 ${placeholders} 条占位（需替换 session）` : "";
  setStatus(`✅ 已复制 NEWAPI_ACCOUNTS（${merged.length} 条${suffix}）`, "success");
}

// DOM 元素
const extractBtn = document.getElementById("extractBtn");
const openAllBtn = document.getElementById("openAllBtn");
const copyBtn = document.getElementById("copyBtn");
const viewSavedBtn = document.getElementById("viewSavedBtn");
const importBtn = document.getElementById("importBtn");
const mergeToolBtn = document.getElementById("mergeToolBtn");
const clearBtn = document.getElementById("clearBtn");
const importBox = document.getElementById("importBox");
const importText = document.getElementById("importText");
const doImportBtn = document.getElementById("doImportBtn");
const statusBox = document.getElementById("statusBox");
const selectAllBtn = document.getElementById("selectAllBtn");
const selectNoneBtn = document.getElementById("selectNoneBtn");
const sitesList = document.getElementById("sitesList");
const resultsBox = document.getElementById("resultsBox");
const resultsList = document.getElementById("resultsList");
const outputBox = document.getElementById("outputBox");
const refreshFailedBtn = document.getElementById("refreshFailedBtn");
const openFailedBtn = document.getElementById("openFailedBtn");
const copyFailedTemplateBtn = document.getElementById("copyFailedTemplateBtn");
const copySecretBtn = document.getElementById("copySecretBtn");
const failedSitesMeta = document.getElementById("failedSitesMeta");
const failedSitesPreview = document.getElementById("failedSitesPreview");

// 站点管理相关 DOM
const manageSitesBtn = document.getElementById("manageSitesBtn");
const siteModal = document.getElementById("siteModal");
const closeModalBtn = document.getElementById("closeModalBtn");
const modalTitle = document.getElementById("modalTitle");
const sitesListView = document.getElementById("sitesListView");
const siteFormView = document.getElementById("siteFormView");
const manageSitesList = document.getElementById("manageSitesList");
const addSiteBtn = document.getElementById("addSiteBtn");
const cancelFormBtn = document.getElementById("cancelFormBtn");
const saveFormBtn = document.getElementById("saveFormBtn");
const siteNameInput = document.getElementById("siteName");
const siteProviderInput = document.getElementById("siteProvider");
const siteDomainInput = document.getElementById("siteDomain");
const siteUrlInput = document.getElementById("siteUrl");

// 更新状态
function setStatus(message, type = "info") {
  statusBox.textContent = message;
  statusBox.className = `status ${type}`;
}

// 渲染站点选择列表
function renderSitesList() {
  sitesList.innerHTML = "";
  for (const [siteId, config] of Object.entries(SITES_CONFIG)) {
    const label = document.createElement("label");
    label.className = "site-checkbox";
    label.innerHTML = `
      <input type="checkbox" data-site="${siteId}" checked>
      <span>${config.name}</span>
    `;
    sitesList.appendChild(label);
  }
}

// 获取选中的站点
function getSelectedSites() {
  const checkboxes = sitesList.querySelectorAll('input[type="checkbox"]:checked');
  return Array.from(checkboxes).map(cb => cb.dataset.site);
}

// 全选/全不选
function selectAll(checked) {
  sitesList.querySelectorAll('input[type="checkbox"]').forEach(cb => cb.checked = checked);
}

// 获取指定域名的 session cookie
async function getSessionCookie(domain) {
  return new Promise((resolve) => {
    chrome.cookies.getAll({ domain: domain }, (cookies) => {
      const sessionCookie = cookies.find((c) => c.name === "session");
      resolve(sessionCookie ? sessionCookie.value : null);
    });
  });
}

// 从 localStorage 获取用户信息（需要在页面上下文执行）
async function getUserInfoFromPage(domain) {
  // 尝试查找已打开的标签页
  const tabs = await chrome.tabs.query({ url: `*://${domain}/*` });

  if (tabs.length === 0) {
    return { username: null, api_user: null };
  }

  try {
    // 添加超时处理，5秒超时
    const timeoutPromise = new Promise((_, reject) => 
      setTimeout(() => reject(new Error("timeout")), 5000)
    );
    
    const scriptPromise = chrome.scripting.executeScript({
      target: { tabId: tabs[0].id },
      func: () => {
        try {
          const userStr = localStorage.getItem("user");
          if (userStr) {
            const user = JSON.parse(userStr);
            return {
              username: user.username || user.display_name || null,
              api_user: user.id ? String(user.id) : null,
            };
          }
        } catch (e) {
          console.error("解析 localStorage 失败:", e);
        }
        return { username: null, api_user: null };
      },
    });

    const results = await Promise.race([scriptPromise, timeoutPromise]);
    return results[0]?.result || { username: null, api_user: null };
  } catch (e) {
    console.error("执行脚本失败或超时:", e);
    return { username: null, api_user: null };
  }
}

// 提取所有站点配置
async function extractAll() {
  const selectedSites = getSelectedSites();
  if (selectedSites.length === 0) {
    setStatus("⚠️ 请至少选择一个站点", "error");
    return;
  }
  
  extractBtn.disabled = true;
  extractBtn.textContent = "⏳ 提取中...";
  setStatus("正在提取各站点配置...", "info");

  // 先加载已保存的配置
  await loadSavedConfigs();
  
  extractedConfigs = [];
  resultsList.innerHTML = "";
  resultsBox.style.display = "block";

  for (const siteId of selectedSites) {
    const config = SITES_CONFIG[siteId];
    const { domain, name, provider } = config;

    // 创建结果项
    const item = document.createElement("div");
    item.className = "result-item";
    item.innerHTML = `
      <a href="#" class="name site-link" data-url="${config.url}">${name}</a>
      <span class="status-icon">⏳</span>
    `;
    resultsList.appendChild(item);
    
    // 添加点击事件打开网页
    item.querySelector(".site-link").addEventListener("click", (e) => {
      e.preventDefault();
      chrome.tabs.create({ url: config.url });
    });

    // 获取 session cookie
    const session = await getSessionCookie(domain);

    if (!session) {
      item.querySelector(".status-icon").textContent = "❌";
      continue;
    }

    // 获取用户信息
    const userInfo = await getUserInfoFromPage(domain);
    const api_user = userInfo.api_user;
    const username = userInfo.username;

    if (!api_user) {
      // 有 cookie 但没有 api_user，提示用户
      item.querySelector(".status-icon").textContent = "⚠️";
      item.title = "找到 session 但未获取到 api_user，请打开该站点页面后重试";
      continue;
    }

    // 生成配置
    const accountName = username || `${provider}_${api_user}`;
    extractedConfigs.push({
      name: accountName,
      provider: provider,
      cookies: { session: session },
      api_user: api_user,
    });

    item.querySelector(".status-icon").textContent = "✅";
  }

  // 显示结果
  if (extractedConfigs.length > 0) {
    // 智能合并到已保存的配置
    const { merged, updated, added } = mergeConfigs(savedConfigs, extractedConfigs);
    
    // 保存合并后的配置
    await saveConfigs(merged);
    savedConfigs = merged;
    
    const jsonStr = JSON.stringify(merged, null, 2);
    outputBox.textContent = jsonStr;
    outputBox.style.display = "block";
    copyBtn.style.display = "block";
    
    let statusMsg = `✅ 提取 ${extractedConfigs.length} 个`;
    if (updated > 0) statusMsg += `，更新 ${updated} 个`;
    if (added > 0) statusMsg += `，新增 ${added} 个`;
    statusMsg += `（共 ${merged.length} 个账号）`;
    setStatus(statusMsg, "success");
    
    // 更新 extractedConfigs 为合并后的结果
    extractedConfigs = merged;
  } else {
    setStatus("❌ 未提取到任何配置，请先登录各站点", "error");
  }

  extractBtn.disabled = false;
  extractBtn.textContent = "📦 提取签到配置";
}

// 复制到剪贴板
async function copyToClipboard() {
  if (extractedConfigs.length === 0) return;

  // 使用 2 空格缩进格式化 JSON
  const jsonStr = JSON.stringify(extractedConfigs, null, 2);
  await navigator.clipboard.writeText(jsonStr);

  copyBtn.textContent = "✅ 已复制!";
  setTimeout(() => {
    copyBtn.textContent = "📋 复制 JSON 到剪贴板";
  }, 2000);
}

// 一键打开所有站点
async function openAllSites() {
  const selectedSites = getSelectedSites();
  if (selectedSites.length === 0) {
    setStatus("⚠️ 请至少选择一个站点", "error");
    return;
  }
  
  openAllBtn.disabled = true;
  openAllBtn.textContent = "⏳ 打开中...";
  
  for (const siteId of selectedSites) {
    const config = SITES_CONFIG[siteId];
    await chrome.tabs.create({ url: config.url, active: false });
    // 稍微延迟避免一次性打开太多
    await new Promise(r => setTimeout(r, 200));
  }
  
  openAllBtn.disabled = false;
  openAllBtn.textContent = "🌐 一键打开所有站点";
  setStatus(`✅ 已打开 ${selectedSites.length} 个站点，请逐个登录后再提取`, "success");
}

// 查看已保存的配置
async function viewSaved() {
  await loadSavedConfigs();
  
  if (savedConfigs.length === 0) {
    setStatus("📂 暂无已保存的配置", "info");
    outputBox.style.display = "none";
    copyBtn.style.display = "none";
    return;
  }
  
  extractedConfigs = savedConfigs;
  const jsonStr = JSON.stringify(savedConfigs, null, 2);
  outputBox.textContent = jsonStr;
  outputBox.style.display = "block";
  copyBtn.style.display = "block";
  setStatus(`📂 已保存 ${savedConfigs.length} 个账号配置`, "info");
}

// 清空配置
async function clearConfigs() {
  if (!confirm("确定要清空所有已保存的配置吗？")) return;
  
  await saveConfigs([]);
  savedConfigs = [];
  extractedConfigs = [];
  outputBox.style.display = "none";
  copyBtn.style.display = "none";
  setStatus("🗑️ 已清空所有配置", "info");
}

// 显示/隐藏导入框
function toggleImportBox() {
  importBox.style.display = importBox.style.display === "none" ? "block" : "none";
}

// 打开合并工具窗口
function openMergeTool() {
  chrome.windows.create({
    url: chrome.runtime.getURL("merge.html"),
    type: "popup",
    width: 800,
    height: 700
  });
}

// 执行导入
async function doImport() {
  const text = importText.value.trim();
  if (!text) {
    setStatus("⚠️ 请粘贴 JSON 配置", "error");
    return;
  }
  
  let newConfigs;
  try {
    newConfigs = JSON.parse(text);
    if (!Array.isArray(newConfigs)) {
      throw new Error("不是数组");
    }
  } catch (e) {
    setStatus("❌ JSON 格式错误: " + e.message, "error");
    return;
  }
  
  // 验证并补全配置
  const validConfigs = [];
  for (const config of newConfigs) {
    if (!config.cookies?.session || !config.api_user) {
      continue; // 跳过无效配置
    }
    // 补全 provider（如果没有）
    if (!config.provider) {
      config.provider = "anyrouter"; // 默认
    }
    validConfigs.push({
      name: config.name || `${config.provider}_${config.api_user}`,
      provider: config.provider,
      cookies: { session: config.cookies.session },
      api_user: String(config.api_user),
    });
  }
  
  if (validConfigs.length === 0) {
    setStatus("❌ 未找到有效配置", "error");
    return;
  }
  
  // 加载已保存的配置并合并
  await loadSavedConfigs();
  const { merged, updated, added } = mergeConfigs(savedConfigs, validConfigs);
  
  // 保存
  await saveConfigs(merged);
  savedConfigs = merged;
  extractedConfigs = merged;
  
  // 显示结果
  const jsonStr = JSON.stringify(merged, null, 2);
  outputBox.textContent = jsonStr;
  outputBox.style.display = "block";
  copyBtn.style.display = "block";
  importBox.style.display = "none";
  importText.value = "";
  
  setStatus(`✅ 导入成功！更新 ${updated} 个，新增 ${added} 个（共 ${merged.length} 个）`, "success");
}

// 事件绑定
extractBtn.addEventListener("click", extractAll);
openAllBtn.addEventListener("click", openAllSites);
copyBtn.addEventListener("click", copyToClipboard);
viewSavedBtn.addEventListener("click", viewSaved);
importBtn.addEventListener("click", toggleImportBox);
doImportBtn.addEventListener("click", doImport);
clearBtn.addEventListener("click", clearConfigs);
mergeToolBtn.addEventListener("click", openMergeTool);
selectAllBtn.addEventListener("click", () => selectAll(true));
selectNoneBtn.addEventListener("click", () => selectAll(false));
refreshFailedBtn.addEventListener("click", () => loadFailedSitesReport(true));
openFailedBtn.addEventListener("click", openFailedSites);
copyFailedTemplateBtn.addEventListener("click", copyFailedTemplate);
copySecretBtn.addEventListener("click", copyNewapiAccountsForSecret);

// 站点管理事件绑定
manageSitesBtn.addEventListener("click", openSiteManager);
closeModalBtn.addEventListener("click", closeSiteManager);
addSiteBtn.addEventListener("click", () => showSiteForm(null));
cancelFormBtn.addEventListener("click", showSitesList);
saveFormBtn.addEventListener("click", saveSite);

// 点击模态框外部关闭
siteModal.addEventListener("click", (e) => {
  if (e.target === siteModal) closeSiteManager();
});

// 站点管理函数
function openSiteManager() {
  siteModal.style.display = "flex";
  showSitesList();
  renderManageSitesList();
}

function closeSiteManager() {
  siteModal.style.display = "none";
  editingSiteId = null;
}

function showSitesList() {
  sitesListView.style.display = "block";
  siteFormView.style.display = "none";
  modalTitle.textContent = "站点管理";
}

function showSiteForm(siteId) {
  sitesListView.style.display = "none";
  siteFormView.style.display = "block";
  editingSiteId = siteId;
  
  if (siteId && SITES_CONFIG[siteId]) {
    // 编辑模式
    modalTitle.textContent = "编辑站点";
    const site = SITES_CONFIG[siteId];
    siteNameInput.value = site.name;
    siteProviderInput.value = site.provider;
    siteProviderInput.disabled = true; // 编辑时不能改 provider
    siteDomainInput.value = site.domain;
    siteUrlInput.value = site.url;
  } else {
    // 添加模式
    modalTitle.textContent = "添加站点";
    siteNameInput.value = "";
    siteProviderInput.value = "";
    siteProviderInput.disabled = false;
    siteDomainInput.value = "";
    siteUrlInput.value = "";
  }
}

function renderManageSitesList() {
  manageSitesList.innerHTML = "";
  
  for (const [siteId, config] of Object.entries(SITES_CONFIG)) {
    const item = document.createElement("div");
    item.className = "manage-site-item";
    item.innerHTML = `
      <div class="manage-site-info">
        <div class="manage-site-name">${config.name}</div>
        <div class="manage-site-domain">${config.domain}</div>
      </div>
      <div class="manage-site-actions">
        <button class="btn-icon edit" data-id="${siteId}" title="编辑">✏️</button>
        <button class="btn-icon delete" data-id="${siteId}" title="删除">🗑️</button>
      </div>
    `;
    manageSitesList.appendChild(item);
  }
  
  // 绑定编辑和删除事件
  manageSitesList.querySelectorAll(".edit").forEach(btn => {
    btn.addEventListener("click", () => showSiteForm(btn.dataset.id));
  });
  
  manageSitesList.querySelectorAll(".delete").forEach(btn => {
    btn.addEventListener("click", () => deleteSite(btn.dataset.id));
  });
}

async function saveSite() {
  const name = siteNameInput.value.trim();
  const provider = siteProviderInput.value.trim().toLowerCase();
  const domain = siteDomainInput.value.trim();
  const url = siteUrlInput.value.trim();
  
  // 验证
  if (!name || !provider || !domain || !url) {
    alert("请填写所有字段");
    return;
  }
  
  // 检查 provider 是否重复（仅新增时）
  if (!editingSiteId && SITES_CONFIG[provider]) {
    alert("Provider ID 已存在，请使用其他名称");
    return;
  }
  
  // 确定使用的 key（编辑时用原 ID，新增时用 provider）
  const siteKey = editingSiteId || provider;
  
  // 保存
  SITES_CONFIG[siteKey] = { name, provider: siteKey, domain, url };
  await saveSitesConfig();
  
  // 刷新界面
  renderSitesList();
  renderManageSitesList();
  showSitesList();
  setStatus(`✅ 站点 "${name}" 已保存`, "success");
}

async function deleteSite(siteId) {
  const site = SITES_CONFIG[siteId];
  if (!site) return;
  
  if (!confirm(`确定要删除站点 "${site.name}" 吗？`)) return;
  
  delete SITES_CONFIG[siteId];
  await saveSitesConfig();
  
  renderSitesList();
  renderManageSitesList();
  setStatus(`🗑️ 站点 "${site.name}" 已删除`, "info");
}

// 初始化
async function init() {
  await loadSitesConfig();
  renderSitesList();
  await loadFailedSitesReport(false);
  
  const configs = await loadSavedConfigs();
  if (configs.length > 0) {
    setStatus(`📂 已保存 ${configs.length} 个账号，点击提取更新或追加`, "info");
  }
  
  // 绑定 LinuxDO 提取按钮
  document.getElementById("extractLinuxdoBtn").addEventListener("click", extractLinuxdoCookies);
  document.getElementById("copyLinuxdoBtn").addEventListener("click", copyLinuxdoConfig);
}

// LinuxDO Cookie 提取
let linuxdoConfig = null;

async function extractLinuxdoCookies() {
  const btn = document.getElementById("extractLinuxdoBtn");
  btn.disabled = true;
  btn.textContent = "⏳ 提取中...";
  setStatus("正在提取 LinuxDO Cookie...", "info");
  
  try {
    // 获取 LinuxDO 的所有 Cookie
    const cookies = await new Promise((resolve) => {
      chrome.cookies.getAll({ domain: "linux.do" }, resolve);
    });
    
    // 提取关键 Cookie
    const cookieMap = {};
    const importantCookies = ["_forum_session", "_t", "cf_clearance"];
    
    for (const cookie of cookies) {
      if (importantCookies.includes(cookie.name)) {
        cookieMap[cookie.name] = cookie.value;
      }
    }
    
    // 检查是否有必要的 Cookie
    if (!cookieMap._forum_session && !cookieMap._t) {
      setStatus("❌ 未找到 LinuxDO Cookie，请先登录 linux.do", "error");
      btn.disabled = false;
      btn.textContent = "🐧 提取 LinuxDO Cookie";
      return;
    }
    
    // 尝试获取用户名（从打开的标签页）
    let username = null;
    const tabs = await chrome.tabs.query({ url: "*://linux.do/*" });
    
    if (tabs.length > 0) {
      try {
        const results = await chrome.scripting.executeScript({
          target: { tabId: tabs[0].id },
          func: () => {
            // 尝试多种方式获取用户名
            // 1. 从 Discourse 的 User 对象
            if (window.Discourse && window.Discourse.User && window.Discourse.User.current()) {
              return window.Discourse.User.current().username;
            }
            // 2. 从页面元素
            const userLink = document.querySelector(".current-user a[data-user-card]");
            if (userLink) {
              return userLink.dataset.userCard;
            }
            // 3. 从头像链接
            const avatar = document.querySelector(".header-dropdown-toggle.current-user img");
            if (avatar && avatar.alt) {
              return avatar.alt;
            }
            return null;
          },
        });
        username = results[0]?.result;
      } catch (e) {
        console.log("获取用户名失败:", e);
      }
    }
    
    // 构建 Cookie 字符串
    const cookieStr = Object.entries(cookieMap)
      .map(([k, v]) => `${k}=${v}`)
      .join("; ");
    
    // 生成配置
    linuxdoConfig = [{
      name: username || "LinuxDO账号",
      cookies: cookieStr,
      level: 2,
      browse_enabled: true
    }];
    
    // 显示结果
    const outputEl = document.getElementById("linuxdoOutput");
    const resultEl = document.getElementById("linuxdoResult");
    
    outputEl.textContent = JSON.stringify(linuxdoConfig, null, 2);
    resultEl.style.display = "block";
    
    // 保存到 storage
    await new Promise((resolve) => {
      chrome.storage.local.set({ linuxdo_config: linuxdoConfig }, resolve);
    });
    
    const cookieCount = Object.keys(cookieMap).length;
    setStatus(`✅ 成功提取 LinuxDO Cookie (${cookieCount} 个)${username ? `，用户: ${username}` : ""}`, "success");
    
  } catch (e) {
    console.error("提取 LinuxDO Cookie 失败:", e);
    setStatus("❌ 提取失败: " + e.message, "error");
  }
  
  btn.disabled = false;
  btn.textContent = "🐧 提取 LinuxDO Cookie";
}

async function copyLinuxdoConfig() {
  if (!linuxdoConfig) {
    // 尝试从 storage 加载
    const result = await new Promise((resolve) => {
      chrome.storage.local.get(["linuxdo_config"], resolve);
    });
    linuxdoConfig = result.linuxdo_config;
  }
  
  if (!linuxdoConfig) {
    setStatus("⚠️ 请先提取 LinuxDO Cookie", "error");
    return;
  }
  
  const jsonStr = JSON.stringify(linuxdoConfig, null, 2);
  await navigator.clipboard.writeText(jsonStr);
  
  const btn = document.getElementById("copyLinuxdoBtn");
  btn.textContent = "✅ 已复制!";
  setTimeout(() => {
    btn.textContent = "📋 复制 LinuxDO 配置";
  }, 2000);
}

init();
