# 绕过 Cloudflare 心得总结

## 成功经验（2026年2月验证有效）

### 1. 核心要点：使用 nodriver + Xvfb 虚拟显示

**关键配置：**
- 使用 `nodriver`（不是 Selenium/Playwright），它直接使用 CDP 协议，不基于 WebDriver
- 在 GitHub Actions 中配合 Xvfb 虚拟显示，使用**非 headless 模式**
- 设置 `sandbox=False`（CI 环境必需）

**为什么 nodriver 能绕过 Cloudflare：**
1. 不使用 WebDriver 协议，没有 `navigator.webdriver` 特征
2. 直接通过 Chrome DevTools Protocol (CDP) 控制浏览器
3. 配合 Xvfb 虚拟显示，浏览器认为自己在真实桌面环境运行

### 2. GitHub Actions 配置

```yaml
# 安装 Xvfb 和 Chrome
- name: Install Xvfb and Chrome
  run: |
    sudo apt-get update
    sudo apt-get install -y xvfb google-chrome-stable

# 启动虚拟显示
- name: Run script
  env:
    DISPLAY: ":99"
  run: |
    Xvfb :99 -screen 0 1920x1080x24 &
    sleep 2
    python your_script.py
```

### 3. nodriver 启动参数

```python
import nodriver as uc

# 关键参数
browser_args = [
    "--disable-blink-features=AutomationControlled",  # 隐藏自动化特征
    "--disable-dev-shm-usage",  # 避免 /dev/shm 空间不足
    "--no-first-run",
    "--window-size=1920,1080",
]

config = uc.Config(
    headless=False,  # 非 headless 模式（配合 Xvfb）
    sandbox=False,   # CI 环境必须关闭沙箱
    browser_args=browser_args,
)

browser = await uc.start(config=config)
```

### 4. 处理 Cloudflare 挑战

```python
async def wait_for_cloudflare(tab, timeout=30):
    """等待 Cloudflare 挑战完成"""
    start_time = time.time()
    
    while time.time() - start_time < timeout:
        title = await tab.evaluate("document.title")
        
        # Cloudflare 挑战页面特征
        cf_indicators = ["just a moment", "checking your browser", "please wait"]
        
        if not any(ind in title.lower() for ind in cf_indicators):
            return True  # 挑战通过
        
        await asyncio.sleep(2)
    
    return False
```

### 5. 重试机制

nodriver 在 CI 环境中启动可能不稳定，需要重试：

```python
async def start_browser_with_retry(max_retries=3):
    for attempt in range(max_retries):
        try:
            browser = await uc.start(config=config)
            return browser
        except Exception as e:
            if attempt < max_retries - 1:
                await asyncio.sleep(2)
            else:
                raise
```

## 失败经验

### 1. 不要使用的方法

- ❌ **Selenium + ChromeDriver** - 容易被检测到 `navigator.webdriver`
- ❌ **Playwright/Patchright headless 模式** - 缺少渲染栈，容易被识别
- ❌ **纯 Cookie 模式** - Cookie 有效期短，且 GitHub Actions IP 容易被限流 (429)
- ❌ **curl_cffi 直接请求** - 无法通过 JS 挑战

### 2. 常见错误

- **429 Too Many Requests** - GitHub Actions 的数据中心 IP 被限流
- **Failed to connect to browser** - nodriver 启动失败，需要重试
- **Timeout** - Cloudflare 挑战未通过，可能需要更长等待时间

## 最佳实践总结

1. **使用 nodriver + Xvfb + 非 headless 模式**
2. **添加重试机制**（nodriver 启动不稳定，CI 环境建议 5 次重试）
3. **先访问首页等待 Cloudflare 通过，再访问登录页**
4. **模拟真实用户行为**（滚动、延迟、随机点赞、偶尔回滚）
5. **保存 Cookie 到缓存**，下次优先使用 Cookie 登录

## 2026年2月3日更新：登录表单填写问题

### 问题现象

nodriver 的 `send_keys()` 方法在 CI 环境中可能丢失字符，导致登录失败：
```
登录错误: Please enter your email or username, and password.
```

日志显示"已输入用户名"、"已输入密码"，但实际内容没有正确填入。

### 解决方案：使用 JavaScript 直接赋值

不要使用 `element.send_keys()`，改用 JS 直接设置 `input.value`：

```python
# ❌ 不可靠的方式
username_input = await tab.select('#login-account-name')
await username_input.send_keys(username)  # 可能丢失字符

# ✅ 可靠的方式：JS 直接赋值
await tab.evaluate(f"""
    (function() {{
        const input = document.querySelector('#login-account-name');
        if (input) {{
            input.focus();
            input.value = '{username}';
            input.dispatchEvent(new Event('input', {{ bubbles: true }}));
            input.dispatchEvent(new Event('change', {{ bubbles: true }}));
            return true;
        }}
        return false;
    }})()
""")
```

**注意事项：**
- 密码中的特殊字符（单引号 `'`、反斜杠 `\`）需要转义
- 必须触发 `input` 和 `change` 事件，否则表单验证可能不识别

### nodriver 启动重试次数

CI 环境中 nodriver 启动不稳定（`Failed to connect to browser`），建议：
- 本地环境：3 次重试
- CI 环境：5 次重试
- 每次重试间隔递增：2s, 4s, 6s...

实测第 3 次尝试通常能成功启动。

## 2026年2月更新：浏览行为优化

### 模拟真实用户阅读

为了避免被 Discourse 论坛检测为机器人，浏览行为需要模拟真实用户：

1. **滚动间隔 5-8 秒** - 模拟真实阅读速度
2. **随机滚动距离 200-500px** - 避免机械化的固定步长
3. **偶尔回滚（20% 概率）** - 模拟回看之前内容
4. **按时间控制浏览** - 而不是按帖子数量
5. **随机点赞（30% 概率）** - 增加互动行为

```python
# 浏览配置示例
config = {
    "scroll_delay": (5, 8),      # 每次滚动间隔 5-8 秒
    "like_chance": 0.3,          # 30% 概率点赞
    "scroll_back_chance": 0.2,   # 20% 概率回滚
}
```

## 参考资源

- [nodriver GitHub](https://github.com/ultrafunkamsterdam/nodriver)
- [Bypassing Cloudflare with Nodriver](https://substack.thewebscraping.club/p/bypassing-cloudflare-with-nodriver)
- [Bypass Cloudflare for GitHub Action](https://github.com/marketplace/actions/bypass-cloudflare-for-github-action)
