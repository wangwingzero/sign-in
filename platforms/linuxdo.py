#!/usr/bin/env python3
"""
LinuxDO 论坛自动浏览帖子适配器

功能：
1. 登录 LinuxDO 论坛
2. 获取帖子列表
3. 模拟浏览帖子（发送 timings 请求标记为已读）
4. 增加在线时间

Discourse API:
- GET /latest.json - 获取最新帖子列表
- GET /t/{topic_id}.json - 获取帖子详情
- POST /topics/timings - 标记帖子为已读
"""

import asyncio
import contextlib
import hashlib
import json
import os
import random
import time
from pathlib import Path

import httpx
import nodriver
from loguru import logger

from platforms.base import BasePlatformAdapter, CheckinResult, CheckinStatus
from utils.browser import BrowserManager, get_browser_engine


class LinuxDOAdapter(BasePlatformAdapter):
    """LinuxDO 论坛自动浏览适配器"""

    BASE_URL = "https://linux.do"
    LATEST_URL = "https://linux.do/latest.json"
    TOP_URL = "https://linux.do/top.json"
    TIMINGS_URL = "https://linux.do/topics/timings"

    # Cookie 持久化文件路径
    COOKIE_CACHE_DIR = ".linuxdo_cookies"

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        account_name: str | None = None,
        browse_minutes: int = 20,
        cookies: dict | str | None = None,
    ):
        """初始化 LinuxDO 适配器

        Args:
            username: LinuxDO 用户名（Cookie 模式可选）
            password: LinuxDO 密码（Cookie 模式可选）
            account_name: 账号显示名称
            browse_minutes: 浏览时长（分钟，默认 20）
            cookies: 预设的 Cookie（优先使用，跳过浏览器登录）
        """
        self.username = username
        self.password = password
        self._account_name = account_name or username or "LinuxDO"
        self.browse_minutes = browse_minutes
        self._preset_cookies = self._parse_cookies(cookies)

        self._browser_manager: BrowserManager | None = None
        self.client: httpx.Client | None = None
        self._cookies: dict = {}
        self._csrf_token: str | None = None
        self._browsed_count: int = 0
        self._total_time: int = 0
        self._likes_given: int = 0  # 记录点赞数
        self._login_method: str = "unknown"  # 记录登录方式

    def _parse_cookies(self, cookies: dict | str | None) -> dict:
        """解析 Cookie 为字典格式"""
        if not cookies:
            return {}

        if isinstance(cookies, dict):
            return cookies

        # 解析字符串格式: "_forum_session=xxx; _t=xxx"
        result = {}
        if isinstance(cookies, str):
            for item in cookies.split(";"):
                item = item.strip()
                if "=" in item:
                    key, value = item.split("=", 1)
                    result[key.strip()] = value.strip()
        return result

    def _get_cookie_cache_path(self) -> Path:
        """获取 Cookie 缓存文件路径"""
        cache_dir = Path(self.COOKIE_CACHE_DIR)
        cache_dir.mkdir(exist_ok=True)

        # 使用用户名或账号名作为文件名
        safe_name = (self.username or self._account_name or "default").replace("/", "_").replace("\\", "_")
        return cache_dir / f"{safe_name}.json"

    def _load_cached_cookies(self) -> dict:
        """从缓存加载 Cookie"""
        cache_path = self._get_cookie_cache_path()
        if not cache_path.exists():
            return {}

        try:
            with open(cache_path, encoding="utf-8") as f:
                data = json.load(f)

            # 检查是否过期（默认 7 天）
            saved_time = data.get("saved_at", 0)
            max_age = 7 * 24 * 3600  # 7 天
            if time.time() - saved_time > max_age:
                logger.info(f"[{self.account_name}] 缓存的 Cookie 已过期，将重新登录")
                return {}

            cookies = data.get("cookies", {})
            if cookies:
                logger.info(f"[{self.account_name}] 从缓存加载了 {len(cookies)} 个 Cookie")
            return cookies

        except Exception as e:
            logger.warning(f"[{self.account_name}] 加载缓存 Cookie 失败: {e}")
            return {}

    def _save_cookies_to_cache(self) -> None:
        """保存 Cookie 到缓存"""
        if not self._cookies:
            return

        cache_path = self._get_cookie_cache_path()
        try:
            data = {
                "cookies": self._cookies,
                "saved_at": time.time(),
                "username": self.username,
            }
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            logger.info(f"[{self.account_name}] Cookie 已保存到缓存")
        except Exception as e:
            logger.warning(f"[{self.account_name}] 保存 Cookie 缓存失败: {e}")

    @property
    def platform_name(self) -> str:
        return "LinuxDO"

    @property
    def account_name(self) -> str:
        return self._account_name

    async def login(self) -> bool:
        """登录 LinuxDO

        登录优先级：
        1. 预设的 Cookie（配置文件中提供）
        2. 缓存的 Cookie（上次登录保存）
        3. 浏览器登录（用户名密码）
        """
        # 优先级 1: 使用预设的 Cookie
        if self._preset_cookies:
            logger.info(f"[{self.account_name}] 尝试使用预设 Cookie 登录...")
            if await self._login_with_cookies(self._preset_cookies):
                self._login_method = "preset_cookie"
                return True
            logger.warning(f"[{self.account_name}] 预设 Cookie 无效，尝试其他方式")

        # 优先级 2: 使用缓存的 Cookie
        cached_cookies = self._load_cached_cookies()
        if cached_cookies:
            logger.info(f"[{self.account_name}] 尝试使用缓存 Cookie 登录...")
            if await self._login_with_cookies(cached_cookies):
                self._login_method = "cached_cookie"
                return True
            logger.warning(f"[{self.account_name}] 缓存 Cookie 无效，尝试浏览器登录")

        # 优先级 3: 浏览器登录已不可用（LinuxDO 启用了人机验证 CAPTCHA）
        # 自动化脚本无法通过交互式 CAPTCHA，只能通过手动提取 Cookie 登录
        if not self.username or not self.password:
            logger.error(f"[{self.account_name}] Cookie 无效且未提供用户名密码，无法登录")
            return False

        logger.warning(
            f"[{self.account_name}] Cookie 无效或过期，"
            f"且 LinuxDO 已启用人机验证（CAPTCHA），无法自动登录。"
            f"请手动登录后提取 Cookie 更新配置。"
        )
        return False

    async def _login_with_cookies(self, cookies: dict) -> bool:
        """使用 Cookie 直接登录（跳过浏览器）

        Args:
            cookies: Cookie 字典

        Returns:
            是否登录成功
        """
        self._cookies = cookies.copy()
        self._csrf_token = cookies.get("_forum_session")
        self._init_http_client()

        # 验证 Cookie 是否有效
        try:
            headers = self._build_headers()
            response = self.client.get(f"{self.BASE_URL}/session/current.json", headers=headers)

            if response.status_code == 200:
                data = response.json()
                current_user = data.get("current_user")
                if current_user:
                    username = current_user.get("username", "Unknown")
                    logger.success(f"[{self.account_name}] Cookie 登录成功！用户: {username}")
                    return True

            logger.debug(f"[{self.account_name}] Cookie 验证失败: {response.status_code}")
            return False

        except Exception as e:
            logger.debug(f"[{self.account_name}] Cookie 验证出错: {e}")
            return False

    async def _login_via_browser(self) -> bool:
        """通过浏览器登录 LinuxDO

        使用 nodriver（最强反检测），在 CI 环境中增加重试次数。
        配合 Xvfb 使用非 headless 模式以绕过 Cloudflare 检测。
        """
        engine = get_browser_engine()
        # 默认不回退，避免在 Cloudflare 场景下切换引擎导致额外不稳定
        fallback_engine = os.environ.get("BROWSER_FALLBACK_ENGINE", "").strip().lower()
        logger.info(f"[{self.account_name}] 使用浏览器引擎: {engine}")

        # CI 环境检测
        is_ci = bool(os.environ.get("CI")) or bool(os.environ.get("GITHUB_ACTIONS"))
        display_set = bool(os.environ.get("DISPLAY"))

        # 支持通过环境变量控制 headless 模式（用于调试）
        headless = os.environ.get("BROWSER_HEADLESS", "true").lower() != "false"

        # 在 CI 环境中，如果有 Xvfb（DISPLAY 已设置），优先使用非 headless 模式
        if is_ci and display_set:
            headless = False
            logger.info(f"[{self.account_name}] CI 环境检测到 DISPLAY={os.environ.get('DISPLAY')}，使用非 headless 模式")

        # 构建引擎尝试顺序：优先主引擎，nodriver 失败时可回退到 patchright
        engine_candidates: list[str] = [engine]
        if (
            engine == "nodriver"
            and fallback_engine in ("drissionpage", "camoufox", "patchright")
            and fallback_engine != engine
        ):
            engine_candidates.append(fallback_engine)

        last_error: Exception | None = None

        for idx, candidate in enumerate(engine_candidates):
            if idx > 0:
                logger.warning(f"[{self.account_name}] 主引擎失败，回退到 {candidate} 重试登录")

            # CI 环境中 nodriver 启动不稳定，增加重试次数到 5 次
            max_retries = 5 if (is_ci and candidate == "nodriver") else 3

            try:
                user_data_dir = None
                if candidate == "nodriver":
                    persist_profile = os.environ.get("LINUXDO_NODRIVER_PERSIST_PROFILE", "false").lower() == "true"
                    if persist_profile:
                        profile_root = Path(os.environ.get("LINUXDO_NODRIVER_PROFILE_DIR", ".nodriver_profiles"))
                        profile_root.mkdir(parents=True, exist_ok=True)
                        account_key = hashlib.sha1(self.account_name.encode("utf-8")).hexdigest()[:16]
                        user_data_dir = str(profile_root / f"acct_{account_key}")
                        logger.info(f"[{self.account_name}] nodriver 复用配置目录: {user_data_dir}")

                self._browser_manager = BrowserManager(
                    engine=candidate,
                    headless=headless,
                    user_data_dir=user_data_dir,
                )
                await self._browser_manager.start(max_retries=max_retries)

                # 获取实际使用的引擎（兼容 BrowserManager 内部 fallback）
                actual_engine = self._browser_manager.engine

                if actual_engine == "nodriver":
                    success = await self._login_nodriver()
                elif actual_engine == "drissionpage":
                    success = await self._login_drissionpage()
                else:
                    success = await self._login_playwright()

                if success:
                    return True

                logger.warning(f"[{self.account_name}] 使用引擎 {actual_engine} 登录失败")
            except Exception as e:
                last_error = e
                logger.error(f"[{self.account_name}] 使用引擎 {candidate} 登录失败: {e}")
            finally:
                # 仅在还要继续尝试下一引擎时清理当前浏览器
                if idx < len(engine_candidates) - 1 and self._browser_manager:
                    with contextlib.suppress(Exception):
                        await self._browser_manager.close()
                    self._browser_manager = None

        if last_error:
            logger.error(f"[{self.account_name}] 浏览器登录最终失败: {last_error}")
        return False

    async def _wait_for_cloudflare_nodriver(self, tab, timeout: int = 30) -> bool:
        """等待 Cloudflare 挑战完成（nodriver 专用）

        Args:
            tab: nodriver 标签页
            timeout: 超时时间（秒）

        Returns:
            是否通过 Cloudflare 验证
        """
        logger.info(f"[{self.account_name}] 检测 Cloudflare 挑战...")

        start_time = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start_time < timeout:
            try:
                # 获取页面标题
                title = await tab.evaluate("document.title")

                # Cloudflare 挑战页面的特征
                cf_indicators = [
                    "just a moment",
                    "checking your browser",
                    "please wait",
                    "verifying",
                    "something went wrong",
                ]

                title_lower = title.lower() if title else ""

                # 检查是否还在 Cloudflare 挑战中
                is_cf_page = any(ind in title_lower for ind in cf_indicators)

                if not is_cf_page and title and "linux" in title_lower:
                    logger.success(f"[{self.account_name}] Cloudflare 挑战通过！页面标题: {title}")
                    return True

                if is_cf_page:
                    logger.debug(f"[{self.account_name}] 等待 Cloudflare... 当前标题: {title}")

            except Exception as e:
                logger.debug(f"[{self.account_name}] 检查页面状态时出错: {e}")

            await asyncio.sleep(2)

        logger.warning(f"[{self.account_name}] 等待 Cloudflare 超时 ({timeout}s)")
        return False

    async def _wait_for_cloudflare_with_retry(self, tab, max_retries: int = 3) -> bool:
        """带重试的 Cloudflare 验证（核心策略：多次尝试）

        根据心得文档：碰到 CF 的核心就是多尝试几次
        使用指数退避策略：5s -> 15s -> 30s

        Args:
            tab: nodriver 标签页
            max_retries: 最大重试次数（默认 3 次）

        Returns:
            是否通过 Cloudflare 验证
        """
        # 指数退避等待时间（秒）
        retry_delays = [5, 15, 30]

        for attempt in range(max_retries):
            logger.info(f"[{self.account_name}] Cloudflare 验证尝试 {attempt + 1}/{max_retries}...")

            # 第一次尝试用较长超时，后续用较短超时
            timeout = 30 if attempt == 0 else 20

            # 等待 Cloudflare 验证
            cf_passed = await self._wait_for_cloudflare_nodriver(tab, timeout=timeout)

            if cf_passed:
                if attempt > 0:
                    logger.success(f"[{self.account_name}] 第 {attempt + 1} 次尝试通过 Cloudflare！")
                return True

            # 最后一次尝试失败，不再重试
            if attempt >= max_retries - 1:
                logger.error(f"[{self.account_name}] Cloudflare 验证失败，已重试 {max_retries} 次")
                return False

            # 指数退避等待
            wait_time = retry_delays[min(attempt, len(retry_delays) - 1)]
            logger.warning(
                f"[{self.account_name}] Cloudflare 验证失败，"
                f"等待 {wait_time}s 后重试（{attempt + 2}/{max_retries}）..."
            )
            await asyncio.sleep(wait_time)

            # 刷新页面重新尝试
            logger.info(f"[{self.account_name}] 刷新页面...")
            await tab.reload()
            await asyncio.sleep(3)  # 等待页面开始加载

        return False

    async def _login_nodriver(self) -> bool:
        """使用 nodriver 登录（优化版本，支持 GitHub Actions）"""
        tab = self._browser_manager.page

        # 1. 先访问首页，让 Cloudflare 验证
        logger.info(f"[{self.account_name}] 访问 LinuxDO 首页...")
        await tab.get(self.BASE_URL)

        # 2. 等待 Cloudflare 挑战完成（多次重试策略）
        cf_passed = await self._wait_for_cloudflare_with_retry(tab, max_retries=3)
        if not cf_passed:
            logger.error(f"[{self.account_name}] Cloudflare 验证失败")
            return False

        # 3. 访问登录页面
        logger.info(f"[{self.account_name}] 访问登录页面...")
        await tab.get(f"{self.BASE_URL}/login")
        await asyncio.sleep(3)

        # 4. 等待登录表单加载
        logger.info(f"[{self.account_name}] 等待登录表单加载...")
        await asyncio.sleep(5)

        # 使用 JS 等待输入框出现
        for _ in range(10):
            try:
                has_input = await tab.evaluate("""
                    (function() {
                        const input = document.querySelector('#login-account-name') ||
                                      document.querySelector('input[name="login"]') ||
                                      document.querySelector('input[type="text"]');
                        return !!input;
                    })()
                """)
                if has_input:
                    logger.info(f"[{self.account_name}] 登录表单已加载")
                    break
            except Exception:
                pass
            await asyncio.sleep(1)

        # 5. 填写用户名（使用 JS 直接赋值，避免 send_keys 丢失字符）
        try:
            # 使用 JS 直接设置输入框的值，比 send_keys 更可靠
            username_filled = await tab.evaluate(f"""
                (function() {{
                    const input = document.querySelector('#login-account-name') ||
                                  document.querySelector('input[name="login"]') ||
                                  document.querySelector('input[type="text"]');
                    if (input) {{
                        input.focus();
                        input.value = '{self.username}';
                        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        return true;
                    }}
                    return false;
                }})()
            """)

            if username_filled:
                logger.info(f"[{self.account_name}] 已输入用户名")
                await asyncio.sleep(0.5)
            else:
                logger.error(f"[{self.account_name}] 未找到用户名输入框")
                return False
        except Exception as e:
            logger.error(f"[{self.account_name}] 输入用户名失败: {e}")
            return False

        # 6. 填写密码（使用 JS 直接赋值）
        try:
            # 转义密码中的特殊字符（单引号、反斜杠）
            escaped_password = self.password.replace("\\", "\\\\").replace("'", "\\'")

            password_filled = await tab.evaluate(f"""
                (function() {{
                    const input = document.querySelector('#login-account-password') ||
                                  document.querySelector('input[type="password"]');
                    if (input) {{
                        input.focus();
                        input.value = '{escaped_password}';
                        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        return true;
                    }}
                    return false;
                }})()
            """)

            if password_filled:
                logger.info(f"[{self.account_name}] 已输入密码")
                await asyncio.sleep(0.5)
            else:
                logger.error(f"[{self.account_name}] 未找到密码输入框")
                return False
        except Exception as e:
            logger.error(f"[{self.account_name}] 输入密码失败: {e}")
            return False

        # 7. 点击登录按钮（使用 JS 点击，比 nodriver 原生 click 更可靠）
        logger.info(f"[{self.account_name}] 点击登录按钮...")
        try:
            # 先等待一下确保表单完全加载
            await asyncio.sleep(1)

            # 使用 JS 点击登录按钮（经测试比 nodriver 原生 click 更可靠）
            clicked = await tab.evaluate("""
                (function() {
                    const btn = document.querySelector('#login-button') ||
                                document.querySelector('#signin-button') ||
                                document.querySelector('button[type="submit"]') ||
                                document.querySelector('input[type="submit"]');
                    if (btn) {
                        btn.click();
                        return true;
                    }
                    return false;
                })()
            """)

            if clicked:
                logger.info(f"[{self.account_name}] 已使用 JS 点击登录按钮")
            else:
                logger.warning(f"[{self.account_name}] 未找到登录按钮，尝试 Enter 键提交")
                # 回退到 Enter 键
                await tab.send(nodriver.cdp.input_.dispatch_key_event(
                    type_="keyDown",
                    key="Enter",
                    code="Enter",
                    windows_virtual_key_code=13,
                    native_virtual_key_code=13,
                ))
                await tab.send(nodriver.cdp.input_.dispatch_key_event(
                    type_="keyUp",
                    key="Enter",
                    code="Enter",
                    windows_virtual_key_code=13,
                    native_virtual_key_code=13,
                ))

        except Exception as e:
            logger.error(f"[{self.account_name}] 点击登录按钮失败: {e}")
            return False

        # 8. 等待登录完成
        logger.info(f"[{self.account_name}] 等待登录完成...")
        for i in range(60):  # 增加到 60 秒
            await asyncio.sleep(1)

            # 检查 URL 是否变化
            current_url = tab.target.url if hasattr(tab, 'target') else ""
            if "login" not in current_url.lower() and current_url:
                logger.info(f"[{self.account_name}] 页面已跳转: {current_url}")
                break

            # 检查是否有错误提示（每 5 秒检查一次）
            if i % 5 == 0:
                error_msg = await tab.evaluate("""
                    (function() {
                        // 检查各种错误提示元素
                        const selectors = [
                            '.alert-error',
                            '.error',
                            '#error-message',
                            '.flash-error',
                            '.login-error',
                            '#login-error',
                            '.ember-view.alert.alert-error',
                            '[class*="error"]'
                        ];
                        for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            if (el && el.innerText && el.innerText.trim()) {
                                return el.innerText.trim();
                            }
                        }
                        return '';
                    })()
                """)
                if error_msg:
                    logger.error(f"[{self.account_name}] 登录错误: {error_msg}")
                    return False

            if i % 10 == 0:
                logger.debug(f"[{self.account_name}] 等待登录... ({i}s)")

        await asyncio.sleep(2)

        # 9. 检查登录状态
        current_url = tab.target.url if hasattr(tab, 'target') else ""
        logger.info(f"[{self.account_name}] 当前 URL: {current_url}")

        if "login" in current_url.lower():
            logger.error(f"[{self.account_name}] 登录失败，仍在登录页面")
            return False

        logger.success(f"[{self.account_name}] 登录成功！")

        # 10. 获取 cookies
        logger.info(f"[{self.account_name}] 获取 cookies...")
        try:
            import nodriver.cdp.network as cdp_network
            all_cookies = await tab.send(cdp_network.get_all_cookies())
            for cookie in all_cookies:
                self._cookies[cookie.name] = cookie.value
            logger.info(f"[{self.account_name}] 获取到 {len(self._cookies)} 个 cookies")

            # 打印关键 cookies
            for key in ['_forum_session', '_t', 'cf_clearance']:
                if key in self._cookies:
                    logger.debug(f"[{self.account_name}]   {key}: {self._cookies[key][:30]}...")
        except Exception as e:
            logger.warning(f"[{self.account_name}] 获取 cookies 失败: {e}")

        # 获取 CSRF token
        self._csrf_token = self._cookies.get('_forum_session')

        # 初始化 HTTP 客户端
        self._init_http_client()

        return True

    async def _login_drissionpage(self) -> bool:
        """使用 DrissionPage 登录"""
        import time
        page = self._browser_manager.page

        logger.info(f"[{self.account_name}] 访问 LinuxDO 登录页面...")
        page.get(f"{self.BASE_URL}/login")
        time.sleep(2)

        await self._browser_manager.wait_for_cloudflare(timeout=30)

        # 填写登录表单
        username_input = page.ele('#login-account-name', timeout=10)
        if username_input:
            username_input.input(self.username)
            time.sleep(0.5)

        password_input = page.ele('#login-account-password', timeout=5)
        if password_input:
            password_input.input(self.password)
            time.sleep(0.5)

        login_btn = page.ele('#login-button', timeout=5)
        if login_btn:
            login_btn.click()
            time.sleep(5)

        # 获取 cookies
        for cookie in page.cookies():
            self._cookies[cookie['name']] = cookie['value']

        self._init_http_client()
        return True

    async def _login_playwright(self) -> bool:
        """使用 Playwright 登录"""
        page = self._browser_manager.page

        await page.goto(f"{self.BASE_URL}/login", wait_until="networkidle")
        await self._browser_manager.wait_for_cloudflare(timeout=30)
        await asyncio.sleep(2)

        await page.fill('#login-account-name', self.username)
        await asyncio.sleep(0.5)
        await page.fill('#login-account-password', self.password)
        await asyncio.sleep(0.5)

        await page.click('#login-button')
        await asyncio.sleep(5)

        cookies = await self._browser_manager.context.cookies()
        for cookie in cookies:
            self._cookies[cookie['name']] = cookie['value']

        self._init_http_client()
        return True

    def _init_http_client(self):
        """初始化 HTTP 客户端"""
        self.client = httpx.Client(timeout=30.0)
        for name, value in self._cookies.items():
            self.client.cookies.set(name, value, domain="linux.do")

    def _build_headers(self) -> dict:
        """构建请求头"""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": self.BASE_URL,
            "Origin": self.BASE_URL,
            "X-Requested-With": "XMLHttpRequest",
        }
        if self._csrf_token:
            headers["X-CSRF-Token"] = self._csrf_token
        return headers

    async def checkin(self) -> CheckinResult:
        """执行浏览帖子操作"""
        logger.info(f"[{self.account_name}] 开始浏览帖子...")

        # 优先使用浏览器直接浏览（更真实）
        if self._browser_manager and self._browser_manager.engine == "nodriver":
            try:
                browsed = await self._browse_topics_via_browser()
                if browsed > 0:
                    return CheckinResult(
                        platform=self.platform_name,
                        account=self.account_name,
                        status=CheckinStatus.SUCCESS,
                        message=f"成功浏览 {browsed} 个帖子，点赞 {self._likes_given} 次",
                        details={
                            "browsed": browsed,
                            "likes": self._likes_given,
                            "browse_minutes": self.browse_minutes,
                            "mode": "browser",
                        },
                    )
            except Exception as e:
                logger.warning(f"[{self.account_name}] 浏览器浏览失败，回退到 API 模式: {e}")

        # 回退到 HTTP API 模式
        topics = self._get_topics()
        if not topics:
            return CheckinResult(
                platform=self.platform_name,
                account=self.account_name,
                status=CheckinStatus.FAILED,
                message="获取帖子列表失败",
            )

        # 随机选择帖子浏览（API 模式固定浏览 10 个）
        browse_count = min(10, len(topics))
        selected_topics = random.sample(topics, browse_count)

        logger.info(f"[{self.account_name}] 将浏览 {browse_count} 个帖子（API 模式）")

        for i, topic in enumerate(selected_topics):
            topic_id = topic.get("id")
            title = topic.get("title", "Unknown")[:30]

            logger.info(f"[{self.account_name}] [{i+1}/{browse_count}] 浏览: {title}...")

            success = self._browse_topic(topic_id)
            if success:
                self._browsed_count += 1

            # 随机延迟，模拟真实阅读
            delay = random.uniform(3, 8)
            await asyncio.sleep(delay)

        details = {
            "browsed": self._browsed_count,
            "total_time": f"{self._total_time // 1000}s",
            "mode": "api",
        }

        if self._browsed_count > 0:
            return CheckinResult(
                platform=self.platform_name,
                account=self.account_name,
                status=CheckinStatus.SUCCESS,
                message=f"成功浏览 {self._browsed_count} 个帖子",
                details=details,
            )
        else:
            return CheckinResult(
                platform=self.platform_name,
                account=self.account_name,
                status=CheckinStatus.FAILED,
                message="浏览帖子失败",
                details=details,
            )

    async def _browse_topics_via_browser(self) -> int:
        """使用浏览器直接浏览帖子（更真实的浏览行为）

        浏览行为：
        - 尽量把每个帖子都看完（滚动到底部）
        - 每次滚动间隔 5-8 秒，模拟真实阅读
        - 偶尔回滚模拟回看行为
        - 过程中随机点赞（30% 概率）
        - Level 影响总浏览时长：
          - L1: 60 分钟（需要多刷时间的账号）
          - L2: 30 分钟（正常账号）
          - L3: 15 分钟（快速浏览）
        - 按时间控制浏览，而不是按帖子数量

        Returns:
            成功浏览的帖子数量
        """
        tab = self._browser_manager.page
        browsed_count = 0

        # 使用 browse_minutes 设置总浏览时长
        total_minutes = self.browse_minutes
        total_seconds = total_minutes * 60

        # 浏览配置 - 模拟真实用户行为
        config = {
            "scroll_delay": (3, 6),   # 每次滚动间隔 3-6 秒
            "like_chance": 0.3,       # 30% 概率点赞
            "scroll_back_chance": 0.2,  # 20% 概率回滚（模拟回看）
        }

        logger.info(
            f"[{self.account_name}] 浏览模式: {total_minutes} 分钟 "
            f"(滚动间隔: {config['scroll_delay'][0]}-{config['scroll_delay'][1]}s)"
        )

        # 记录开始时间
        start_time = time.time()
        end_time = start_time + total_seconds

        # 已浏览的帖子 URL 集合（避免重复）
        browsed_urls = set()

        while time.time() < end_time:
            # 计算剩余时间
            remaining = int(end_time - time.time())
            remaining_min = remaining // 60
            remaining_sec = remaining % 60

            logger.info(
                f"[{self.account_name}] 剩余时间: {remaining_min}分{remaining_sec}秒, "
                f"已浏览: {browsed_count} 个帖子"
            )

            # 访问最新帖子页面获取新帖子
            logger.info(f"[{self.account_name}] 访问最新帖子页面...")
            await tab.get(f"{self.BASE_URL}/latest")
            await asyncio.sleep(5)

            # 等待帖子列表加载
            for _ in range(10):
                has_topics = await tab.evaluate("document.querySelectorAll('a.title').length > 0")
                if has_topics:
                    break
                await asyncio.sleep(1)

            # 获取帖子链接
            topic_links_json = await tab.evaluate("""
                (function() {
                    const links = document.querySelectorAll('a.title.raw-link, a.title[href*="/t/"]');
                    const result = [];
                    for (let i = 0; i < Math.min(links.length, 30); i++) {
                        const a = links[i];
                        if (a.href && a.href.includes('/t/')) {
                            result.push({
                                href: a.href,
                                title: (a.innerText || a.textContent || '').trim().substring(0, 50)
                            });
                        }
                    }
                    return JSON.stringify(result);
                })()
            """)

            # 解析 JSON 结果
            topic_links = []
            if topic_links_json and isinstance(topic_links_json, str):
                try:
                    topic_links = json.loads(topic_links_json)
                except json.JSONDecodeError:
                    logger.warning(f"[{self.account_name}] JSON 解析失败")
            elif isinstance(topic_links_json, list):
                topic_links = topic_links_json

            if not topic_links:
                logger.warning(f"[{self.account_name}] 未获取到帖子列表，等待后重试...")
                await asyncio.sleep(10)
                continue

            # 过滤掉已浏览的帖子
            new_topics = [t for t in topic_links if t.get('href') not in browsed_urls]

            if not new_topics:
                logger.info(f"[{self.account_name}] 所有帖子都已浏览，刷新页面获取新帖子...")
                await asyncio.sleep(30)  # 等待一段时间再刷新
                continue

            # 随机打乱顺序
            random.shuffle(new_topics)

            # 浏览帖子直到时间用完或帖子看完
            for topic in new_topics:
                # 检查时间是否用完
                if time.time() >= end_time:
                    break

                title = topic.get('title', 'Unknown')[:40]
                href = topic.get('href', '')

                logger.info(f"[{self.account_name}] [{browsed_count + 1}] 浏览: {title}...")

                try:
                    # 访问帖子
                    await tab.get(href)
                    await asyncio.sleep(random.uniform(3, 5))  # 等待页面加载

                    # 分步滚动到底部（模拟真实阅读，尽量看完整个帖子）
                    await self._scroll_and_read(tab, config)

                    # 随机点赞
                    if random.random() < config['like_chance']:
                        liked = await self._try_like_post(tab)
                        if liked:
                            self._likes_given += 1

                    browsed_count += 1
                    browsed_urls.add(href)

                except Exception as e:
                    logger.warning(f"[{self.account_name}] 浏览帖子失败: {e}")

        # 计算实际浏览时间
        actual_time = int(time.time() - start_time)
        actual_min = actual_time // 60
        actual_sec = actual_time % 60

        logger.success(
            f"[{self.account_name}] 浏览完成！"
            f"共浏览 {browsed_count} 个帖子，点赞 {self._likes_given} 次，"
            f"实际用时: {actual_min}分{actual_sec}秒"
        )
        return browsed_count

    async def _scroll_and_read(self, tab, config: dict) -> None:
        """分步滚动页面，模拟真实阅读行为

        核心策略：
        - 每次滚动间隔 5-8 秒，模拟真实阅读速度
        - 滚动距离随机（200-500px），避免机械化
        - 偶尔回滚一小段，模拟回看行为
        - 尽量把帖子看完（滚动到底部）

        Args:
            tab: 浏览器标签页
            config: 浏览配置（包含 scroll_delay, scroll_back_chance）
        """
        scroll_delay_min, scroll_delay_max = config['scroll_delay']
        scroll_back_chance = config.get('scroll_back_chance', 0.2)

        # 获取页面高度
        page_height = await tab.evaluate("document.body.scrollHeight")
        viewport_height = await tab.evaluate("window.innerHeight")

        # 计算需要滚动的总距离
        total_scroll = max(0, page_height - viewport_height)

        if total_scroll <= 0:
            # 页面不需要滚动，直接等待一段时间
            delay = random.uniform(scroll_delay_min, scroll_delay_max)
            logger.debug(f"[{self.account_name}]   页面无需滚动，阅读 {delay:.1f}s...")
            await asyncio.sleep(delay)
            return

        current_scroll = 0
        scroll_count = 0

        # 持续滚动直到到达底部
        while current_scroll < total_scroll:
            scroll_count += 1

            # 随机滚动距离（200-500px），模拟真实滚动
            scroll_distance = random.randint(200, 500)

            # 偶尔回滚一小段（模拟回看行为）
            if scroll_count > 2 and random.random() < scroll_back_chance:
                back_distance = random.randint(50, 150)
                current_scroll = max(0, current_scroll - back_distance)
                await tab.evaluate(f"window.scrollTo({{top: {current_scroll}, behavior: 'smooth'}})")
                logger.debug(f"[{self.account_name}]   ↑ 回滚 {back_distance}px（模拟回看）")
                await asyncio.sleep(random.uniform(1, 2))

            # 滚动一步
            current_scroll = min(current_scroll + scroll_distance, total_scroll)
            await tab.evaluate(f"window.scrollTo({{top: {current_scroll}, behavior: 'smooth'}})")

            # 等待 5-8 秒，模拟真实阅读
            delay = random.uniform(scroll_delay_min, scroll_delay_max)
            progress = int(current_scroll / total_scroll * 100)
            logger.debug(f"[{self.account_name}]   滚动 {scroll_count} ({progress}%)，阅读 {delay:.1f}s...")
            await asyncio.sleep(delay)

        # 确保滚动到底部
        await tab.evaluate("window.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'})")

        # 在底部停留一会儿（3-5 秒）
        final_read = random.uniform(3, 5)
        logger.debug(f"[{self.account_name}]   底部阅读 {final_read:.1f}s...")
        await asyncio.sleep(final_read)

    async def _try_like_post(self, tab) -> bool:
        """尝试给帖子点赞

        Args:
            tab: 浏览器标签页

        Returns:
            是否成功点赞
        """
        try:
            # 查找可点赞的按钮（未点赞状态）
            # Discourse 的点赞按钮通常有 like 相关的 class
            liked = await tab.evaluate("""
                (function() {
                    // 查找第一个帖子的点赞按钮（排除已点赞的）
                    const likeButtons = document.querySelectorAll(
                        'button.like:not(.has-like), ' +
                        'button[class*="like"]:not(.liked):not(.has-like), ' +
                        '.post-controls button.toggle-like:not(.has-like)'
                    );

                    // 随机选择一个点赞按钮（如果有多个）
                    if (likeButtons.length > 0) {
                        const randomIndex = Math.floor(Math.random() * Math.min(likeButtons.length, 3));
                        const btn = likeButtons[randomIndex];
                        if (btn && !btn.disabled) {
                            btn.click();
                            return true;
                        }
                    }
                    return false;
                })()
            """)

            if liked:
                logger.debug(f"[{self.account_name}]   👍 点赞成功")
                await asyncio.sleep(random.uniform(0.5, 1.5))  # 点赞后短暂等待
                return True

        except Exception as e:
            logger.debug(f"[{self.account_name}]   点赞失败: {e}")

        return False

    def _get_topics(self) -> list:
        """获取帖子列表"""
        headers = self._build_headers()

        try:
            # 获取最新帖子
            response = self.client.get(self.LATEST_URL, headers=headers)
            if response.status_code == 200:
                data = response.json()
                topics = data.get("topic_list", {}).get("topics", [])
                logger.info(f"[{self.account_name}] 获取到 {len(topics)} 个帖子")
                return topics
        except Exception as e:
            logger.error(f"[{self.account_name}] 获取帖子列表失败: {e}")

        return []

    def _browse_topic(self, topic_id: int) -> bool:
        """浏览单个帖子（发送 timings 请求）

        根据 Discourse API，/topics/timings 接口参数格式：
        - topic_id: 帖子 ID
        - topic_time: 总阅读时间（毫秒）
        - timings[n]: 第 n 楼的阅读时间（毫秒）
        """
        headers = self._build_headers()
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"

        # 先获取帖子详情
        try:
            topic_url = f"{self.BASE_URL}/t/{topic_id}.json"
            response = self.client.get(topic_url, headers=headers)
            if response.status_code != 200:
                return False

            topic_data = response.json()
            posts = topic_data.get("post_stream", {}).get("posts", [])

            if not posts:
                return False

            # 构建 timings 数据
            # 模拟阅读时间：总时间 5-30 秒
            total_time = random.randint(5000, 30000)
            self._total_time += total_time

            # timings 格式: timings[post_number]=milliseconds
            timings_data = {
                "topic_id": topic_id,
                "topic_time": total_time,
            }

            # 为每个帖子分配阅读时间（最多前 5 个帖子）
            post_count = min(len(posts), 5)
            time_per_post = total_time // post_count

            for post in posts[:post_count]:
                post_number = post.get("post_number", 1)
                # 每个帖子的时间略有随机波动
                post_time = time_per_post + random.randint(-500, 500)
                timings_data[f"timings[{post_number}]"] = max(1000, post_time)

            # 发送 timings 请求
            response = self.client.post(
                self.TIMINGS_URL,
                headers=headers,
                data=timings_data,
            )

            if response.status_code == 200:
                return True
            else:
                logger.debug(f"timings 请求返回: {response.status_code}")
                return False

        except Exception as e:
            logger.debug(f"浏览帖子 {topic_id} 失败: {e}")
            return False

    async def get_status(self) -> dict:
        """获取浏览状态"""
        return {
            "browsed_count": self._browsed_count,
            "total_time": self._total_time,
        }

    async def cleanup(self) -> None:
        """清理资源"""
        if self._browser_manager:
            with contextlib.suppress(Exception):
                await self._browser_manager.close()
            self._browser_manager = None

        if self.client:
            self.client.close()
            self.client = None
