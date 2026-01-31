#!/usr/bin/env python3
"""
浏览器自动化工具

支持四种浏览器引擎：
1. nodriver - 不基于 WebDriver/Selenium，直接使用 CDP，最难被检测（推荐）
2. DrissionPage - 不基于 WebDriver，较难被 Cloudflare 检测
3. Camoufox - 反检测 Firefox（需要额外下载组件）
4. Patchright - 反检测 Chromium（备用）

GitHub Actions 环境需要配合 Xvfb 虚拟显示使用。
"""

import asyncio
import contextlib
import os
from pathlib import Path
from typing import Literal

from loguru import logger

# 浏览器引擎类型
BrowserEngine = Literal["nodriver", "drissionpage", "camoufox", "patchright"]

# 默认使用 nodriver（最难被 Cloudflare 检测）
DEFAULT_ENGINE: BrowserEngine = "nodriver"

# Camoufox 用户数据目录
CAMOUFOX_PROFILE_DIR = Path(".camoufox_profile")


class BrowserManager:
    """浏览器管理器，统一管理多种浏览器引擎。"""

    def __init__(
        self,
        engine: BrowserEngine = DEFAULT_ENGINE,
        headless: bool = True,
        user_data_dir: str | None = None,
    ):
        """初始化浏览器管理器。

        Args:
            engine: 浏览器引擎类型
            headless: 是否无头模式（nodriver/DrissionPage 在 Xvfb 下应设为 False）
            user_data_dir: 用户数据目录（用于持久化 Cookie）
        """
        self.engine = engine
        self.headless = headless
        self.user_data_dir = user_data_dir

        self._browser = None
        self._context = None
        self._page = None
        self._playwright = None  # Patchright 专用
        self._camoufox = None    # Camoufox 专用
        self._drission_page = None  # DrissionPage 专用
        self._nodriver_browser = None  # nodriver 专用
        self._nodriver_tab = None  # nodriver 专用

    async def start(self):
        """启动浏览器"""
        if self.engine == "nodriver":
            await self._start_nodriver()
        elif self.engine == "drissionpage":
            await self._start_drissionpage()
        elif self.engine == "camoufox":
            await self._start_camoufox()
        else:
            await self._start_patchright()

        return self

    async def _start_nodriver(self):
        """启动 nodriver 浏览器（最强反检测）"""
        import nodriver as uc

        logger.info(f"启动 nodriver 浏览器 (headless={self.headless})")

        # nodriver 配置
        # GitHub Actions 以 root 运行，必须设置 sandbox=False
        browser_args = [
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--no-first-run",
            "--no-service-autorun",
            "--password-store=basic",
        ]

        # 在 GitHub Actions 上使用 Xvfb 时，不要设置 headless
        use_headless = self.headless and not os.environ.get("DISPLAY")

        self._nodriver_browser = await uc.start(
            headless=use_headless,
            sandbox=False,  # GitHub Actions 以 root 运行必须设置
            browser_args=browser_args,
            user_data_dir=self.user_data_dir,
        )

        # 获取主标签页 - browser.get(url) 返回 Tab 对象
        self._nodriver_tab = await self._nodriver_browser.get("about:blank")

        logger.info("nodriver 浏览器启动成功")

    async def _start_drissionpage(self):
        """启动 DrissionPage 浏览器"""
        from DrissionPage import ChromiumOptions, ChromiumPage

        logger.info(f"启动 DrissionPage 浏览器 (headless={self.headless})")

        co = ChromiumOptions()

        # GitHub Actions 环境配置
        co.set_argument("--no-sandbox")
        co.set_argument("--disable-dev-shm-usage")
        co.set_argument("--disable-gpu")

        # 设置 User-Agent
        co.set_user_agent(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )

        # 在 GitHub Actions 上使用 Xvfb 时，不要设置 headless
        # Xvfb 会提供虚拟显示，让浏览器以为在正常环境运行
        if self.headless and not os.environ.get("DISPLAY"):
            # 本地测试时使用 headless
            co.headless()

        # DrissionPage 是同步的，需要在线程中运行
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as executor:
            self._drission_page = await asyncio.get_event_loop().run_in_executor(
                executor, lambda: ChromiumPage(co)
            )

        logger.info("DrissionPage 浏览器启动成功")

    async def _start_camoufox(self):
        """启动 Camoufox 浏览器"""
        try:
            from camoufox.async_api import AsyncCamoufox
        except ImportError:
            logger.error("Camoufox 未安装，请运行: pip install camoufox[geoip] && camoufox fetch")
            raise

        profile_dir = self.user_data_dir or str(CAMOUFOX_PROFILE_DIR)
        Path(profile_dir).mkdir(parents=True, exist_ok=True)

        logger.info(f"启动 Camoufox 浏览器 (headless={self.headless}, profile={profile_dir})")

        import platform
        headless_mode = ("virtual" if platform.system() == "Linux" else True) if self.headless else False

        # 网络受限时禁用 geoip 和 addons
        import os as _os
        enable_geoip = _os.environ.get("CAMOUFOX_GEOIP", "true").lower() == "true"

        self._camoufox = AsyncCamoufox(
            headless=headless_mode,
            humanize=True,
            geoip=enable_geoip,
            disable_coop=True,
            exclude_addons=["UBO"],  # 排除 uBlock Origin 避免下载
            os=["windows", "macos", "linux"],
        )

        self._browser = await self._camoufox.__aenter__()
        self._page = await self._browser.new_page()

        logger.info("Camoufox 浏览器启动成功")

    async def _start_patchright(self):
        """启动 Patchright 浏览器"""
        from patchright.async_api import async_playwright

        logger.info(f"启动 Patchright 浏览器 (headless={self.headless})")

        self._playwright = await async_playwright().start()

        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ]
        )

        self._context = await self._browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )

        self._page = await self._context.new_page()

        logger.info("Patchright 浏览器启动成功")

    @property
    def page(self):
        """获取当前页面"""
        if self.engine == "nodriver":
            return self._nodriver_tab
        if self.engine == "drissionpage":
            return self._drission_page
        return self._page

    @property
    def browser(self):
        """获取浏览器实例"""
        if self.engine == "nodriver":
            return self._nodriver_browser
        if self.engine == "drissionpage":
            return self._drission_page
        return self._browser

    @property
    def context(self):
        """获取浏览器上下文"""
        if self.engine == "nodriver":
            return self._nodriver_browser
        if self.engine == "drissionpage":
            return self._drission_page
        if self.engine == "camoufox":
            return self._browser
        return self._context

    async def get_cookies(self) -> list:
        """获取所有 Cookie"""
        if self.engine == "nodriver":
            # nodriver 使用 CDP 获取 cookies
            cookies = await self._nodriver_browser.cookies.get_all()
            return cookies
        if self.engine == "drissionpage":
            return self._drission_page.cookies()
        if self.engine == "camoufox":
            return await self._browser.cookies()
        return await self._context.cookies()

    async def get_cookie(self, name: str, domain: str) -> str | None:
        """获取指定 Cookie 的值。

        Args:
            name: Cookie 名称
            domain: Cookie 域名

        Returns:
            Cookie 值，未找到返回 None
        """
        cookies = await self.get_cookies()
        for cookie in cookies:
            if self.engine == "nodriver":
                # nodriver 返回的是 Cookie 对象
                cookie_name = getattr(cookie, "name", None)
                cookie_domain = getattr(cookie, "domain", "")
                cookie_value = getattr(cookie, "value", None)
            elif isinstance(cookie, dict):
                cookie_name = cookie.get("name")
                cookie_domain = cookie.get("domain", "")
                cookie_value = cookie.get("value")
            else:
                cookie_name = getattr(cookie, "name", None)
                cookie_domain = getattr(cookie, "domain", "")
                cookie_value = getattr(cookie, "value", None)

            if cookie_name == name and domain in cookie_domain:
                return cookie_value
        return None

    async def wait_for_cloudflare(self, timeout: int = 30):
        """等待 Cloudflare 验证完成。

        Args:
            timeout: 超时时间（秒）
        """
        logger.info("等待 Cloudflare 验证...")

        start_time = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start_time < timeout:
            try:
                if self.engine == "nodriver":
                    # nodriver 异步获取页面信息
                    title = await self._nodriver_tab.get_content()
                    content = title  # get_content 返回 HTML
                    page_title = self._nodriver_tab.target.title or ""
                    title = page_title
                elif self.engine == "drissionpage":
                    title = self._drission_page.title
                    content = self._drission_page.html
                else:
                    title = await self._page.title()
                    content = await self._page.content()

                cf_indicators = [
                    "Just a moment",
                    "Checking your browser",
                    "Please wait",
                    "cf-browser-verification",
                    "challenge-running",
                ]

                is_cf_page = any(indicator in title or indicator in content for indicator in cf_indicators)

                if not is_cf_page:
                    logger.info("Cloudflare 验证通过")
                    return True

                # nodriver: 尝试点击 Turnstile 复选框
                if self.engine == "nodriver":
                    try:
                        turnstile = await self._nodriver_tab.select("#cf-turnstile", timeout=1)
                        if turnstile:
                            logger.info("检测到 Turnstile，尝试点击...")
                            await turnstile.click()
                            await asyncio.sleep(3)
                    except Exception:
                        pass

                # DrissionPage: 尝试点击 Turnstile 复选框
                elif self.engine == "drissionpage":
                    turnstile = self._drission_page.ele("@id=cf-turnstile", timeout=1)
                    if turnstile:
                        logger.info("检测到 Turnstile，尝试点击...")
                        turnstile.click()
                        await asyncio.sleep(3)

            except Exception as e:
                logger.debug(f"检查 Cloudflare 状态时出错: {e}")

            await asyncio.sleep(1)

        logger.warning(f"Cloudflare 验证超时 ({timeout}s)")
        return False

    async def close(self):
        """关闭浏览器"""
        if self.engine == "nodriver":
            with contextlib.suppress(Exception):
                if self._nodriver_browser:
                    self._nodriver_browser.stop()
        elif self.engine == "drissionpage":
            with contextlib.suppress(Exception):
                if self._drission_page:
                    self._drission_page.quit()
        elif self.engine == "camoufox":
            with contextlib.suppress(Exception):
                if self._page:
                    await self._page.close()
            with contextlib.suppress(Exception):
                if self._camoufox:
                    await self._camoufox.__aexit__(None, None, None)
        else:
            with contextlib.suppress(Exception):
                if self._page:
                    await self._page.close()
            with contextlib.suppress(Exception):
                if self._context:
                    await self._context.close()
            with contextlib.suppress(Exception):
                if self._browser:
                    await self._browser.close()
            with contextlib.suppress(Exception):
                if self._playwright:
                    await self._playwright.stop()

        logger.info("浏览器已关闭")

    async def __aenter__(self):
        """异步上下文管理器入口"""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器出口"""
        await self.close()


async def create_browser(
    engine: BrowserEngine = DEFAULT_ENGINE,
    headless: bool = True,
    user_data_dir: str | None = None,
) -> BrowserManager:
    """创建浏览器实例。

    Args:
        engine: 浏览器引擎类型
        headless: 是否无头模式
        user_data_dir: 用户数据目录

    Returns:
        BrowserManager 实例
    """
    manager = BrowserManager(engine=engine, headless=headless, user_data_dir=user_data_dir)
    await manager.start()
    return manager


def get_browser_engine() -> BrowserEngine:
    """获取配置的浏览器引擎。

    优先级：
    1. 环境变量 BROWSER_ENGINE
    2. 默认值 (nodriver)
    """
    engine = os.environ.get("BROWSER_ENGINE", DEFAULT_ENGINE).lower()
    if engine in ("nodriver", "drissionpage", "camoufox", "patchright"):
        return engine
    return DEFAULT_ENGINE
