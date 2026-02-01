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
import json
import random

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

    def __init__(
        self,
        username: str,
        password: str,
        browse_count: int = 10,
        account_name: str | None = None,
    ):
        """初始化 LinuxDO 适配器

        Args:
            username: LinuxDO 用户名
            password: LinuxDO 密码
            browse_count: 浏览帖子数量（默认 10）
            account_name: 账号显示名称
        """
        self.username = username
        self.password = password
        self.browse_count = browse_count
        self._account_name = account_name or username

        self._browser_manager: BrowserManager | None = None
        self.client: httpx.Client | None = None
        self._cookies: dict = {}
        self._csrf_token: str | None = None
        self._browsed_count: int = 0
        self._total_time: int = 0

    @property
    def platform_name(self) -> str:
        return "LinuxDO"

    @property
    def account_name(self) -> str:
        return self._account_name

    async def login(self) -> bool:
        """通过浏览器登录 LinuxDO"""
        import os
        engine = get_browser_engine()
        logger.info(f"[{self.account_name}] 使用浏览器引擎: {engine}")

        # 支持通过环境变量控制 headless 模式（用于调试）
        headless = os.environ.get("BROWSER_HEADLESS", "true").lower() != "false"
        self._browser_manager = BrowserManager(engine=engine, headless=headless)
        await self._browser_manager.start()

        try:
            if engine == "nodriver":
                return await self._login_nodriver()
            elif engine == "drissionpage":
                return await self._login_drissionpage()
            else:
                return await self._login_playwright()
        except Exception as e:
            logger.error(f"[{self.account_name}] 登录失败: {e}")
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

    async def _login_nodriver(self) -> bool:
        """使用 nodriver 登录（优化版本，支持 GitHub Actions）"""
        tab = self._browser_manager.page

        # 1. 先访问首页，让 Cloudflare 验证
        logger.info(f"[{self.account_name}] 访问 LinuxDO 首页...")
        await tab.get(self.BASE_URL)

        # 2. 等待 Cloudflare 挑战完成
        cf_passed = await self._wait_for_cloudflare_nodriver(tab, timeout=30)
        if not cf_passed:
            # 尝试刷新页面
            logger.info(f"[{self.account_name}] 尝试刷新页面...")
            await tab.reload()
            cf_passed = await self._wait_for_cloudflare_nodriver(tab, timeout=20)
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
        for attempt in range(10):
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

        # 5. 填写用户名
        try:
            username_input = await tab.select('#login-account-name', timeout=5)
            if not username_input:
                username_input = await tab.select('input[name="login"]', timeout=3)
            if not username_input:
                username_input = await tab.select('input[type="text"]', timeout=3)

            if username_input:
                await username_input.click()
                await asyncio.sleep(0.3)
                await username_input.send_keys(self.username)
                logger.info(f"[{self.account_name}] 已输入用户名")
                await asyncio.sleep(0.5)
            else:
                logger.error(f"[{self.account_name}] 未找到用户名输入框")
                return False
        except Exception as e:
            logger.error(f"[{self.account_name}] 输入用户名失败: {e}")
            return False

        # 6. 填写密码
        try:
            password_input = await tab.select('#login-account-password', timeout=5)
            if not password_input:
                password_input = await tab.select('input[type="password"]', timeout=3)

            if password_input:
                await password_input.click()
                await asyncio.sleep(0.3)
                await password_input.send_keys(self.password)
                logger.info(f"[{self.account_name}] 已输入密码")
                await asyncio.sleep(0.5)
            else:
                logger.error(f"[{self.account_name}] 未找到密码输入框")
                return False
        except Exception as e:
            logger.error(f"[{self.account_name}] 输入密码失败: {e}")
            return False

        # 7. 点击登录按钮
        logger.info(f"[{self.account_name}] 点击登录按钮...")
        try:
            # 先等待一下确保表单完全加载
            await asyncio.sleep(1)

            # 方式1: 使用键盘 Enter 提交（最可靠）
            await tab.evaluate("""
                (function() {
                    const passwordInput = document.querySelector('#login-account-password') ||
                                          document.querySelector('input[type="password"]');
                    if (passwordInput) {
                        passwordInput.focus();
                    }
                })()
            """)
            await asyncio.sleep(0.3)

            # 发送 Enter 键
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
            logger.info(f"[{self.account_name}] 已发送 Enter 键提交表单")

        except Exception as e:
            logger.warning(f"[{self.account_name}] Enter 键提交失败: {e}，尝试点击按钮")
            # 回退到点击按钮
            try:
                await tab.evaluate("""
                    (function() {
                        const btn = document.querySelector('#login-button') ||
                                    document.querySelector('#signin-button') ||
                                    document.querySelector('button[type="submit"]') ||
                                    document.querySelector('input[type="submit"]');
                        if (btn) btn.click();
                    })()
                """)
                logger.info(f"[{self.account_name}] 使用 JS 点击登录按钮")
            except Exception as e2:
                logger.error(f"[{self.account_name}] 点击登录按钮也失败: {e2}")
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

            # 检查是否有错误提示
            if i == 5:
                error_msg = await tab.evaluate("""
                    (function() {
                        const err = document.querySelector('.alert-error, .error, #error-message, .flash-error');
                        return err ? err.innerText : '';
                    })()
                """)
                if error_msg:
                    logger.error(f"[{self.account_name}] 登录错误: {error_msg}")

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
                        message=f"成功浏览 {browsed} 个帖子（浏览器模式）",
                        details={"browsed": browsed, "mode": "browser"},
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

        # 随机选择帖子浏览
        browse_count = min(self.browse_count, len(topics))
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

        Returns:
            成功浏览的帖子数量
        """
        tab = self._browser_manager.page
        browsed_count = 0

        # 访问最新帖子页面
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
                for (let i = 0; i < Math.min(links.length, 15); i++) {
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
            logger.warning(f"[{self.account_name}] 未获取到帖子列表")
            return 0

        logger.info(f"[{self.account_name}] 找到 {len(topic_links)} 个帖子")

        # 随机选择帖子浏览
        browse_count = min(self.browse_count, len(topic_links))
        selected = random.sample(topic_links, browse_count)

        for i, topic in enumerate(selected):
            title = topic.get('title', 'Unknown')[:40]
            href = topic.get('href', '')

            logger.info(f"[{self.account_name}] [{i+1}/{browse_count}] 浏览: {title}...")

            try:
                # 访问帖子
                await tab.get(href)

                # 模拟阅读（随机等待 3-8 秒）
                read_time = random.uniform(3, 8)
                logger.debug(f"[{self.account_name}]   阅读 {read_time:.1f} 秒...")
                await asyncio.sleep(read_time)

                # 滚动页面
                await tab.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                await asyncio.sleep(1)

                browsed_count += 1
            except Exception as e:
                logger.warning(f"[{self.account_name}] 浏览帖子失败: {e}")

        logger.success(f"[{self.account_name}] 成功浏览 {browsed_count} 个帖子！")
        return browsed_count

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
