// DOM 元素
const inputText = document.getElementById("inputText");
const outputText = document.getElementById("outputText");
const mergeBtn = document.getElementById("mergeBtn");
const copyBtn = document.getElementById("copyBtn");
const clearBtn = document.getElementById("clearBtn");
const statusBox = document.getElementById("statusBox");

// 更新状态
function setStatus(message, type = "info") {
  statusBox.textContent = message;
  statusBox.className = `status ${type}`;
}

// 从文本中提取所有 JSON 数组
function extractJsonArrays(text) {
  const arrays = [];
  let depth = 0;
  let start = -1;
  
  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    
    if (char === "[") {
      if (depth === 0) start = i;
      depth++;
    } else if (char === "]") {
      depth--;
      if (depth === 0 && start !== -1) {
        const jsonStr = text.substring(start, i + 1);
        try {
          const arr = JSON.parse(jsonStr);
          if (Array.isArray(arr)) {
            arrays.push(arr);
          }
        } catch (e) {
          // 忽略解析失败的
        }
        start = -1;
      }
    }
  }
  
  return arrays;
}

// 智能合并配置（相同 provider+api_user 保留最新，按 provider 字母排序）
function mergeConfigs(allConfigs) {
  const configMap = new Map();
  
  for (const config of allConfigs) {
    // 验证必要字段
    if (!config.cookies?.session || !config.api_user) {
      continue;
    }
    
    // 补全 provider
    const provider = config.provider || "anyrouter";
    const key = `${provider}_${config.api_user}`;
    
    // 标准化配置
    const normalized = {
      name: config.name || `${provider}_${config.api_user}`,
      provider: provider,
      cookies: { session: config.cookies.session },
      api_user: String(config.api_user),
    };
    
    // 后面的覆盖前面的（保留最新）
    configMap.set(key, normalized);
  }
  
  // 按 provider 字母顺序排序（a-z）
  const result = Array.from(configMap.values());
  result.sort((a, b) => a.provider.localeCompare(b.provider));
  
  return result;
}

// 执行合并
function doMerge() {
  const text = inputText.value.trim();
  if (!text) {
    setStatus("⚠️ 请先粘贴 JSON 配置", "error");
    return;
  }
  
  // 提取所有 JSON 数组
  const arrays = extractJsonArrays(text);
  
  if (arrays.length === 0) {
    setStatus("❌ 未找到有效的 JSON 数组", "error");
    return;
  }
  
  // 合并所有配置
  const allConfigs = arrays.flat();
  const merged = mergeConfigs(allConfigs);
  
  if (merged.length === 0) {
    setStatus("❌ 未找到有效的账号配置", "error");
    return;
  }
  
  // 输出结果
  const jsonStr = JSON.stringify(merged, null, 2);
  outputText.value = jsonStr;
  
  // 统计
  const totalInput = allConfigs.length;
  const duplicates = totalInput - merged.length;
  
  let msg = `✅ 合并完成！共 ${merged.length} 个账号`;
  if (duplicates > 0) {
    msg += `（去除 ${duplicates} 个重复）`;
  }
  msg += `，来自 ${arrays.length} 个 JSON 数组`;
  
  setStatus(msg, "success");
}

// 复制结果
async function copyResult() {
  const text = outputText.value;
  if (!text) {
    setStatus("⚠️ 没有可复制的内容", "error");
    return;
  }
  
  try {
    await navigator.clipboard.writeText(text);
    copyBtn.textContent = "✅ 已复制!";
    setTimeout(() => {
      copyBtn.textContent = "📋 复制结果";
    }, 2000);
  } catch (e) {
    // 备用方案
    outputText.select();
    document.execCommand("copy");
    copyBtn.textContent = "✅ 已复制!";
    setTimeout(() => {
      copyBtn.textContent = "📋 复制结果";
    }, 2000);
  }
}

// 清空
function clearAll() {
  inputText.value = "";
  outputText.value = "";
  setStatus("把多个 JSON 配置粘贴到下方，点击「合并」自动去重整理", "info");
}

// 事件绑定
mergeBtn.addEventListener("click", doMerge);
copyBtn.addEventListener("click", copyResult);
clearBtn.addEventListener("click", clearAll);
