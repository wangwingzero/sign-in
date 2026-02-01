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
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    pass

from loguru import logger

from utils.oauth_helpers import is_oauth_related_url

# 浏览器引擎类型
BrowserEngine = Literal["nodriver", "drissionpage", "camoufox", "patchright"]

# 默认使用 nodriver（最难被 Cloudflare 检测）
DEFAULT_ENGINE: BrowserEngine = "nodriver"

# Camoufox 用户数据目录
CAMOUFOX_PROFILE_DIR = Path(".camoufox_profile")


class BrowserStartupError(Exception):
    """浏览器启动失败异常。

    当浏览器无法启动时抛出此异常，包含详细的环境信息和修复建议。

    Attributes:
        message: 错误消息
        environment_info: 环境信息字符串
        suggestions: 修复建议字符串

    Requirements:
        - 8.4: 提供清晰的错误消息，包含环境信息和修复建议
    """

    def __init__(
        self,
        message: str,
        environment_info: str = "",
        suggestions: str = ""
    ):
        self.message = message
        self.environment_info = environment_info
        self.suggestions = suggestions

        # 构建完整的错误消息
        full_message = f"浏览器启动失败: {message}"
        if environment_info:
            full_message += f"\n\n环境信息:\n{environment_info}"
        if suggestions:
            full_message += f"\n\n可能的解决方案:\n{suggestions}"

        super().__init__(full_message)


class TabManager:
    """管理 OAuth 流程中的浏览器标签页。

    用于检测新标签页的打开、切换到指定标签页、以及识别 OAuth 相关的标签页。

    Attributes:
        browser: nodriver 浏览器实例
        _initial_tab_count: 记录的初始标签页数量
        _initial_tabs: 记录的初始标签页列表

    Requirements:
        - 1.3: 使用 bring_to_front() 切换到新标签页
        - 3.1: 通过比较点击前后的标签页数量识别新标签页
        - 3.2: 通过检查 URL 是否包含 "linux.do" 或 "oauth" 识别正确的 OAuth 标签页
        - 3.3: 调用 bring_to_front() 确保标签页处于活动状态
    """

    def __init__(self, browser: Any):
        """初始化 TabManager。

        Args:
            browser: nodriver 浏览器实例（通过 uc.start() 返回）
        """
        self.browser = browser
        self._initial_tab_count: int = 0
        self._initial_tabs: list = []

    def record_tab_count(self) -> int:
        """记录当前标签页数量（在 OAuth 点击前调用）。

        在点击 OAuth 按钮之前调用此方法，记录当前的标签页数量，
        以便后续检测是否有新标签页打开。

        Returns:
            当前标签页数量

        Requirements:
            - 3.1: 通过比较点击前后的标签页数量识别新标签页
        """
        if self.browser is None:
            self._initial_tab_count = 0
            self._initial_tabs = []
            return 0

        # nodriver 的 browser.tabs 是标签页列表
        tabs = self.browser.tabs if hasattr(self.browser, 'tabs') else []
        self._initial_tab_count = len(tabs)
        # 记录初始标签页的 target.target_id 用于后续比较
        self._initial_tabs = [
            getattr(tab.target, 'target_id', id(tab))
            for tab in tabs
        ]

        logger.debug(f"记录初始标签页数量: {self._initial_tab_count}")
        return self._initial_tab_count

    async def detect_new_tab(self, timeout: int = 5) -> Any | None:
        """检测 OAuth 点击后是否打开了新标签页。

        在点击 OAuth 按钮后调用此方法，等待并检测是否有新标签页打开。
        通过比较当前标签页列表与之前记录的列表来识别新标签页。

        Args:
            timeout: 等待新标签页的超时时间（秒）

        Returns:
            新打开的标签页对象，如果没有新标签页则返回 None

        Requirements:
            - 3.1: 通过比较点击前后的标签页数量识别新标签页
        """
        if self.browser is None:
            return None

        start_time = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start_time < timeout:
            # 获取当前标签页列表
            current_tabs = self.browser.tabs if hasattr(self.browser, 'tabs') else []
            current_count = len(current_tabs)

            # 检查是否有新标签页
            if current_count > self._initial_tab_count:
                # 找出新增的标签页
                for tab in current_tabs:
                    tab_id = getattr(tab.target, 'target_id', id(tab))
                    if tab_id not in self._initial_tabs:
                        logger.info(f"检测到新标签页: {getattr(tab.target, 'url', 'unknown')}")
                        return tab

            await asyncio.sleep(0.3)

        logger.debug(f"未检测到新标签页（等待 {timeout}s）")
        return None

    async def switch_to_tab(self, tab: Any) -> None:
        """切换到指定的标签页。

        使用 bring_to_front() 方法将指定标签页置于前台，确保其处于活动状态。

        Args:
            tab: 要切换到的标签页对象

        Requirements:
            - 1.3: 使用 bring_to_front() 切换到新标签页
            - 3.3: 调用 bring_to_front() 确保标签页处于活动状态
        """
        if tab is None:
            logger.warning("无法切换到空标签页")
            return

        try:
            # nodriver 使用 bring_to_front() 方法
            if hasattr(tab, 'bring_to_front'):
                await tab.bring_to_front()
                logger.info(f"已切换到标签页: {getattr(tab.target, 'url', 'unknown')}")
            else:
                logger.warning("标签页不支持 bring_to_front() 方法")
        except Exception as e:
            logger.error(f"切换标签页失败: {e}")
            raise

    async def find_oauth_tab(self) -> Any | None:
        """查找 OAuth 相关的标签页。

        遍历所有标签页，通过检查 URL 是否包含 OAuth 相关关键词
        （如 "linux.do"、"oauth"）来识别正确的 OAuth 标签页。

        Returns:
            OAuth 相关的标签页对象，如果未找到则返回 None

        Requirements:
            - 3.2: 通过检查 URL 是否包含 "linux.do" 或 "oauth" 识别正确的 OAuth 标签页
        """
        if self.browser is None:
            return None

        tabs = self.browser.tabs if hasattr(self.browser, 'tabs') else []

        for tab in tabs:
            # 获取标签页 URL
            url = getattr(tab.target, 'url', '') if hasattr(tab, 'target') else ''

            # 使用 oauth_helpers 中的函数检查 URL 是否与 OAuth 相关
            if is_oauth_related_url(url):
                logger.info(f"找到 OAuth 相关标签页: {url}")
                return tab

        logger.debug("未找到 OAuth 相关标签页")
        return None


class URLMonitor:
    """使用 CDP 监控 URL 变化。

    在 OAuth 流程中监控当前标签页的 URL 变化，使用 CDP 的 get_frame_tree()
    方法获取准确的 URL，而不是依赖可能不准确的 tab.target.url。

    Attributes:
        tab: nodriver 标签页实例
        poll_interval: URL 轮询间隔（秒），默认 0.5 秒

    Requirements:
        - 1.4: 如果 OAuth 按钮点击没有打开新标签页，则监控当前标签页的 URL 变化
        - 1.5: 使用 CDP 的 page.get_frame_tree() 获取准确的当前 URL
        - 2.1: 以固定间隔（500ms）轮询当前 URL
        - 2.2: 使用 CDP 的 get_frame_tree() 获取准确的 URL，而不是依赖 tab.target.url
        - 2.5: 如果 URL 在超时时间内没有变化，返回超时错误
    """

    def __init__(self, tab: Any, poll_interval: float = 0.5):
        """初始化 URLMonitor。

        Args:
            tab: nodriver 标签页实例（通过 browser.get() 返回）
            poll_interval: URL 轮询间隔（秒），默认 0.5 秒
                          Requirements 2.1 要求 500ms 间隔
        """
        self.tab = tab
        self.poll_interval = poll_interval

    async def get_current_url(self) -> str:
        """使用 CDP get_frame_tree() 获取准确的当前 URL。

        首先尝试使用 CDP 的 Page.getFrameTree 命令获取准确的 URL，
        如果 CDP 调用失败，则回退到 tab.target.url。

        Returns:
            当前页面的 URL 字符串

        Requirements:
            - 1.5: 使用 CDP 的 page.get_frame_tree() 获取准确的当前 URL
            - 2.2: 使用 CDP 的 get_frame_tree() 获取准确的 URL
        """
        if self.tab is None:
            return ""

        try:
            # 使用 CDP 的 Page.getFrameTree 获取准确的 URL
            # nodriver 通过 tab.send() 发送 CDP 命令
            import nodriver.cdp.page as cdp_page

            frame_tree_result = await self.tab.send(cdp_page.get_frame_tree())

            if frame_tree_result and hasattr(frame_tree_result, 'frame'):
                url = frame_tree_result.frame.url
                logger.debug(f"CDP get_frame_tree() 获取 URL: {url}")
                return url or ""

        except Exception as e:
            logger.debug(f"CDP get_frame_tree() 失败，回退到 tab.target.url: {e}")

        # 回退方案：使用 tab.target.url
        try:
            if hasattr(self.tab, 'target') and hasattr(self.tab.target, 'url'):
                url = self.tab.target.url or ""
                logger.debug(f"使用 tab.target.url 获取 URL: {url}")
                return url
        except Exception as e:
            logger.debug(f"获取 tab.target.url 失败: {e}")

        return ""

    async def wait_for_url_contains(self, pattern: str, timeout: int = 30) -> str:
        """等待 URL 包含指定的模式。

        以固定间隔（poll_interval）轮询当前 URL，直到 URL 包含指定的模式
        或超时。使用 CDP 获取准确的 URL。

        Args:
            pattern: 要匹配的 URL 模式（子字符串）
            timeout: 超时时间（秒），默认 30 秒
                    Requirements 2.5 要求 30 秒超时

        Returns:
            匹配到模式时的 URL

        Raises:
            TimeoutError: 如果在超时时间内未找到匹配的 URL

        Requirements:
            - 2.1: 以固定间隔（500ms）轮询当前 URL
            - 2.5: 如果 URL 在超时时间内没有变化，返回超时错误
        """
        if not pattern:
            raise ValueError("URL 模式不能为空")

        logger.info(f"等待 URL 包含 '{pattern}'（超时: {timeout}s）")

        start_time = asyncio.get_event_loop().time()
        last_url = ""

        while asyncio.get_event_loop().time() - start_time < timeout:
            current_url = await self.get_current_url()

            # 记录 URL 变化
            if current_url != last_url:
                logger.debug(f"URL 变化: {last_url} -> {current_url}")
                last_url = current_url

            # 检查 URL 是否包含目标模式
            if pattern.lower() in current_url.lower():
                logger.info(f"URL 匹配成功: {current_url}")
                return current_url

            # 按照 Requirements 2.1 的要求，以 500ms 间隔轮询
            await asyncio.sleep(self.poll_interval)

        # 超时，抛出 TimeoutError（Requirements 2.5）
        elapsed = asyncio.get_event_loop().time() - start_time
        error_msg = f"等待 URL 包含 '{pattern}' 超时（{elapsed:.1f}s），当前 URL: {last_url}"
        logger.warning(error_msg)
        raise TimeoutError(error_msg)


class CookieRetriever:
    """使用 CDP 获取 session cookie。

    在 OAuth 流程完成后，使用 CDP 的 network.get_cookies() 方法准确获取
    session cookie。支持按 cookie 名称和域名（包括子域名）进行匹配。

    Attributes:
        browser: BrowserManager 实例
        domain: 目标域名（用于匹配 cookie）

    Requirements:
        - 6.1: OAuth 流程完成并重定向回目标站点后，从浏览器获取所有 cookies
        - 6.2: 使用 CDP 的 network.get_cookies() 进行准确的 cookie 获取
        - 6.3: 通过 cookie 名称（"session"）和域名进行匹配
        - 6.4: 如果 OAuth 完成后未找到 session cookie，等待最多 5 秒并重试
    """

    def __init__(self, browser_manager: "BrowserManager", domain: str):
        """初始化 CookieRetriever。

        Args:
            browser_manager: BrowserManager 实例，用于访问浏览器
            domain: 目标域名，用于匹配 cookie（如 "example.com"）
        """
        self.browser = browser_manager
        self.domain = domain.lower().lstrip(".")  # 规范化域名

    def _domain_matches(self, cookie_domain: str) -> bool:
        """检查 cookie 域名是否匹配目标域名（包括子域名）。

        Cookie 域名匹配规则：
        - 精确匹配：cookie_domain == target_domain
        - 子域名匹配：cookie_domain 以 "." + target_domain 结尾
        - 前缀点匹配：cookie_domain 去掉前缀点后匹配

        Args:
            cookie_domain: cookie 的域名

        Returns:
            如果域名匹配则返回 True

        Examples:
            target_domain = "example.com"
            - "example.com" -> True (精确匹配)
            - ".example.com" -> True (前缀点匹配)
            - "sub.example.com" -> True (子域名)
            - ".sub.example.com" -> True (子域名带前缀点)
            - "other.com" -> False
        """
        if not cookie_domain:
            return False

        # 规范化 cookie 域名：去掉前缀点，转小写
        normalized_cookie_domain = cookie_domain.lower().lstrip(".")

        # 精确匹配
        if normalized_cookie_domain == self.domain:
            return True

        # 子域名匹配：cookie 域名以 ".target_domain" 结尾
        # 例如：sub.example.com 匹配 example.com
        if normalized_cookie_domain.endswith("." + self.domain):
            return True

        # 反向子域名匹配：target 域名以 ".cookie_domain" 结尾
        # 例如：target=sub.example.com, cookie=example.com
        return bool(self.domain.endswith("." + normalized_cookie_domain))

    async def _get_cookies_via_cdp(self) -> list:
        """使用 CDP network.get_cookies() 获取 cookies。

        优先使用 CDP 命令获取准确的 cookie 信息，如果失败则回退到
        浏览器的内置方法。

        Returns:
            Cookie 列表，每个 cookie 是一个对象或字典

        Requirements:
            - 6.2: 使用 CDP 的 network.get_cookies() 进行准确的 cookie 获取
        """
        if self.browser.engine != "nodriver":
            # 非 nodriver 引擎使用 BrowserManager 的 get_cookies 方法
            return await self.browser.get_cookies()

        # nodriver 引擎使用 CDP
        try:
            import nodriver.cdp.network as cdp_network

            tab = self.browser.page
            if tab is None:
                logger.warning("nodriver tab 为空，无法获取 cookies")
                return []

            # 使用 CDP network.get_all_cookies() 获取所有 cookies
            # 这比 get_cookies(urls) 更可靠，因为不需要指定 URL
            cookies = await tab.send(cdp_network.get_all_cookies())

            logger.debug(f"CDP get_all_cookies() 返回 {len(cookies)} 个 cookies")
            return cookies

        except Exception as e:
            logger.warning(f"CDP get_all_cookies() 失败，回退到内置方法: {e}")
            # 回退到 BrowserManager 的 get_cookies 方法
            return await self.browser.get_cookies()

    def _find_session_cookie(self, cookies: list) -> str | None:
        """从 cookie 列表中查找匹配的 session cookie。

        遍历所有 cookies，查找名称为 "session" 且域名匹配目标域名的 cookie。

        Args:
            cookies: Cookie 列表

        Returns:
            匹配的 session cookie 值，未找到返回 None

        Requirements:
            - 6.3: 通过 cookie 名称（"session"）和域名进行匹配
        """
        # 打印所有 cookies 用于调试
        session_cookies_found = []
        for cookie in cookies:
            # 获取 cookie 属性（支持对象和字典两种格式）
            if isinstance(cookie, dict):
                cookie_name = cookie.get("name", "")
                cookie_domain = cookie.get("domain", "")
                cookie_value = cookie.get("value", "")
            else:
                # nodriver CDP 返回的是 Cookie 对象
                cookie_name = getattr(cookie, "name", "")
                cookie_domain = getattr(cookie, "domain", "")
                cookie_value = getattr(cookie, "value", "")

            # 记录所有 session cookie（不管域名）
            if cookie_name == "session":
                session_cookies_found.append({
                    "domain": cookie_domain,
                    "value_preview": cookie_value[:20] + "..." if len(cookie_value) > 20 else cookie_value
                })

            # 检查名称是否为 "session"
            if cookie_name != "session":
                continue

            # 检查域名是否匹配
            if self._domain_matches(cookie_domain):
                logger.debug(
                    f"找到匹配的 session cookie: "
                    f"name={cookie_name}, domain={cookie_domain}"
                )
                return cookie_value

        # 如果没找到匹配的，打印所有 session cookies 用于调试
        if session_cookies_found:
            logger.debug(f"找到 {len(session_cookies_found)} 个 session cookie，但域名不匹配 {self.domain}:")
            for sc in session_cookies_found:
                logger.debug(f"  - domain={sc['domain']}, value={sc['value_preview']}")

        return None

    async def get_session_cookie(self, max_retries: int = 3) -> str | None:
        """获取指定域名的 session cookie。

        使用 CDP network.get_cookies() 获取准确的 cookie 信息，
        并按名称（"session"）和域名进行匹配。如果未立即找到，
        会进行重试。

        Args:
            max_retries: 最大重试次数，默认 3 次
                        Requirements 6.4 要求等待最多 5 秒

        Returns:
            session cookie 的值，未找到返回 None

        Requirements:
            - 6.1: OAuth 流程完成并重定向回目标站点后，从浏览器获取所有 cookies
            - 6.2: 使用 CDP 的 network.get_cookies() 进行准确的 cookie 获取
            - 6.3: 通过 cookie 名称（"session"）和域名进行匹配
            - 6.4: 如果 OAuth 完成后未找到 session cookie，等待最多 5 秒并重试
        """
        logger.info(f"获取 session cookie (domain={self.domain}, max_retries={max_retries})")

        # 计算每次重试的延迟，确保总等待时间约为 5 秒（Requirements 6.4）
        # 例如：3 次重试，延迟分别为 1s, 1.5s, 2s，总计约 4.5s
        base_delay = 1.0

        for attempt in range(max_retries):
            try:
                # 获取所有 cookies
                cookies = await self._get_cookies_via_cdp()

                logger.debug(f"第 {attempt + 1} 次尝试，获取到 {len(cookies)} 个 cookies")

                # 查找匹配的 session cookie
                session_value = self._find_session_cookie(cookies)

                if session_value:
                    logger.info(f"成功获取 session cookie (尝试 {attempt + 1}/{max_retries})")
                    return session_value

                # 未找到，准备重试
                if attempt < max_retries - 1:
                    # 计算延迟：使用递增延迟
                    delay = base_delay * (1 + attempt * 0.5)
                    logger.debug(
                        f"未找到 session cookie，{delay:.1f}s 后重试 "
                        f"({attempt + 1}/{max_retries})"
                    )
                    await asyncio.sleep(delay)

            except Exception as e:
                logger.warning(f"获取 cookies 时出错 (尝试 {attempt + 1}/{max_retries}): {e}")

                if attempt < max_retries - 1:
                    delay = base_delay * (1 + attempt * 0.5)
                    await asyncio.sleep(delay)

        logger.warning(
            f"未找到 session cookie (domain={self.domain}, "
            f"尝试 {max_retries} 次)"
        )
        return None


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
        """启动 nodriver 浏览器（最强反检测）

        GitHub Actions 环境配置说明：
        - Requirement 8.1: 当 DISPLAY 环境变量设置时（Xvfb 可用），使用非 headless 模式
        - Requirement 8.2: 当以 root 用户运行时，设置 sandbox=False
        - Requirement 8.3: 配置适当的浏览器参数以确保 CI 环境稳定性
        """
        import nodriver as uc

        # 检测 CI 环境
        display_set = bool(os.environ.get("DISPLAY"))
        is_github_actions = bool(os.environ.get("GITHUB_ACTIONS"))
        is_ci = bool(os.environ.get("CI")) or is_github_actions

        # 检测是否以 root 用户运行（GitHub Actions 默认以 root 运行）
        is_root = os.geteuid() == 0 if hasattr(os, 'geteuid') else False

        logger.info(
            f"启动 nodriver 浏览器 (headless={self.headless}, "
            f"DISPLAY={display_set}, CI={is_ci}, root={is_root})"
        )

        # Requirement 8.3: CI 环境稳定性参数
        browser_args = [
            "--disable-dev-shm-usage",  # 避免 /dev/shm 空间不足问题
            "--disable-gpu",             # CI 环境通常没有 GPU
            "--no-first-run",            # 跳过首次运行向导
            "--no-service-autorun",      # 禁用后台服务自动运行
            "--password-store=basic",    # 使用基本密码存储
        ]

        # CI 环境额外参数
        if is_ci:
            browser_args.extend([
                "--disable-background-networking",  # 减少后台网络活动
                "--disable-default-apps",           # 禁用默认应用
                "--disable-extensions",             # 禁用扩展
                "--disable-sync",                   # 禁用同步
                "--metrics-recording-only",         # 仅记录指标
                "--mute-audio",                     # 静音
                "--no-default-browser-check",       # 跳过默认浏览器检查
            ])
            logger.debug("CI 环境检测到，添加额外稳定性参数")

        # Requirement 8.1: 当 DISPLAY 设置时（Xvfb 可用），使用非 headless 模式
        # headless 模式容易被反爬虫系统检测，使用 Xvfb 虚拟显示可以绕过检测
        use_headless = self.headless and not display_set

        if display_set and self.headless:
            logger.info(
                f"检测到 DISPLAY={os.environ.get('DISPLAY')}，"
                f"切换到非 headless 模式（使用 Xvfb 虚拟显示）"
            )

        # Requirement 8.2: 在 CI 环境或 root 用户下，必须设置 sandbox=False
        # Chrome 的沙箱机制在 CI 环境（如 GitHub Actions）中经常无法正常工作
        # 即使不是 root 用户，CI 环境的容器/虚拟机也可能缺少必要的内核功能
        use_sandbox = not (is_root or is_ci)

        if is_root:
            logger.info("检测到 root 用户，设置 sandbox=False")
        elif is_ci:
            logger.info("检测到 CI 环境，设置 sandbox=False 以确保兼容性")

        # Requirement 8.4: 提供清晰的错误消息
        # 尝试启动浏览器，捕获并提供有用的错误信息
        # 使用 Config 对象进行配置，确保 sandbox 设置生效
        try:
            # 在初始化时传入 browser_args（Config.browser_args 是只读属性）
            config = uc.Config(
                headless=use_headless,
                sandbox=use_sandbox,
                browser_args=browser_args,
                user_data_dir=self.user_data_dir,
            )

            logger.info(
                f"nodriver 配置: headless={config.headless}, "
                f"sandbox={config.sandbox}, args={len(browser_args)}"
            )

            self._nodriver_browser = await uc.start(config=config)
        except Exception as e:
            # 构建详细的环境信息用于调试
            env_info = self._build_environment_info(
                display_set=display_set,
                is_ci=is_ci,
                is_root=is_root,
                use_headless=use_headless,
                use_sandbox=use_sandbox
            )

            # 根据错误类型提供具体的修复建议
            error_msg = str(e).lower()
            suggestions = self._get_browser_startup_suggestions(error_msg, env_info)

            logger.error(
                f"nodriver 浏览器启动失败\n"
                f"错误: {e}\n"
                f"环境信息:\n{env_info}\n"
                f"可能的解决方案:\n{suggestions}"
            )
            raise BrowserStartupError(
                message=str(e),
                environment_info=env_info,
                suggestions=suggestions
            ) from e

        # 获取主标签页 - browser.get(url) 返回 Tab 对象
        try:
            self._nodriver_tab = await self._nodriver_browser.get("about:blank")
        except Exception as e:
            env_info = self._build_environment_info(
                display_set=display_set,
                is_ci=is_ci,
                is_root=is_root,
                use_headless=use_headless,
                use_sandbox=use_sandbox
            )
            logger.error(
                f"nodriver 获取初始标签页失败\n"
                f"错误: {e}\n"
                f"环境信息:\n{env_info}"
            )
            # 清理已启动的浏览器
            if self._nodriver_browser:
                with contextlib.suppress(Exception):
                    self._nodriver_browser.stop()
            raise BrowserStartupError(
                message=f"获取初始标签页失败: {e}",
                environment_info=env_info,
                suggestions="- 检查浏览器是否正常启动\n- 尝试增加启动超时时间"
            ) from e

        logger.info(
            f"nodriver 浏览器启动成功 "
            f"(headless={use_headless}, sandbox={use_sandbox})"
        )

    def _build_environment_info(
        self,
        display_set: bool,
        is_ci: bool,
        is_root: bool,
        use_headless: bool,
        use_sandbox: bool
    ) -> str:
        """构建环境信息字符串用于错误诊断。

        Args:
            display_set: DISPLAY 环境变量是否设置
            is_ci: 是否在 CI 环境中运行
            is_root: 是否以 root 用户运行
            use_headless: 实际使用的 headless 设置
            use_sandbox: 实际使用的 sandbox 设置

        Returns:
            格式化的环境信息字符串
        """
        import platform
        import shutil

        # 检测 Chrome 可执行文件
        chrome_paths = [
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
        ]
        chrome_found = None
        for path in chrome_paths:
            if shutil.which(path):
                chrome_found = shutil.which(path)
                break

        return (
            f"  - 操作系统: {platform.system()} {platform.release()}\n"
            f"  - Python 版本: {platform.python_version()}\n"
            f"  - DISPLAY 设置: {display_set} ({os.environ.get('DISPLAY', '未设置')})\n"
            f"  - CI 环境: {is_ci}\n"
            f"  - GitHub Actions: {bool(os.environ.get('GITHUB_ACTIONS'))}\n"
            f"  - Root 用户: {is_root}\n"
            f"  - Headless 模式: {use_headless}\n"
            f"  - Sandbox 模式: {use_sandbox}\n"
            f"  - Chrome 路径: {chrome_found or '未找到'}"
        )

    def _get_browser_startup_suggestions(self, error_msg: str, _env_info: str) -> str:
        """根据错误消息提供具体的修复建议。

        Args:
            error_msg: 错误消息（小写）
            _env_info: 环境信息字符串（保留用于未来扩展）

        Returns:
            格式化的修复建议字符串
        """
        suggestions = []

        # 连接失败
        if "connect" in error_msg or "timeout" in error_msg:
            suggestions.append("- 增加启动超时时间（start_timeout 参数）")
            suggestions.append("- 检查是否有残留的 Chrome 进程（pkill -f chrome）")
            suggestions.append("- 清理用户数据目录中的锁文件")

        # 权限问题
        if "sandbox" in error_msg or "permission" in error_msg or "root" in error_msg:
            suggestions.append("- 确保设置了 sandbox=False（以 root 运行时必需）")
            suggestions.append("- 添加 --no-sandbox 浏览器参数")

        # 找不到浏览器
        if "not found" in error_msg or "no such file" in error_msg or "executable" in error_msg:
            suggestions.append("- 安装 Chrome 或 Chromium 浏览器")
            suggestions.append("- 在 GitHub Actions 中使用 browser-actions/setup-chrome@latest")
            suggestions.append("- 手动指定浏览器路径（browser_executable_path 参数）")

        # 显示问题
        if "display" in error_msg or "x11" in error_msg or "screen" in error_msg:
            suggestions.append("- 在 CI 环境中使用 Xvfb 虚拟显示")
            suggestions.append("- 设置 DISPLAY 环境变量（如 :99）")
            suggestions.append("- 使用 xvfb-run 运行脚本")

        # /dev/shm 空间不足
        if "shm" in error_msg or "shared memory" in error_msg:
            suggestions.append("- 添加 --disable-dev-shm-usage 浏览器参数")
            suggestions.append("- 在 Docker 中增加 /dev/shm 大小")

        # 通用建议
        if not suggestions:
            suggestions.append("- 检查 Chrome/Chromium 是否正确安装")
            suggestions.append("- 确保有足够的系统资源（内存、磁盘空间）")
            suggestions.append("- 查看 nodriver 文档获取更多信息")

        return "\n".join(suggestions)

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

    async def wait_for_cloudflare(self, timeout: int = 30, tab: Any = None):
        """等待 Cloudflare 验证完成。

        Args:
            timeout: 超时时间（秒）
            tab: 可选的标签页对象（用于 nodriver 切换标签页后）
        """
        logger.info("等待 Cloudflare 验证...")

        # 如果传入了 tab 参数，使用它；否则使用默认的 _nodriver_tab
        current_tab = tab if tab is not None else self._nodriver_tab

        start_time = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start_time < timeout:
            try:
                if self.engine == "nodriver":
                    # nodriver 异步获取页面信息
                    title = await current_tab.get_content()
                    content = title  # get_content 返回 HTML
                    page_title = current_tab.target.title or ""
                    title = page_title
                elif self.engine == "drissionpage":
                    title = self._drission_page.title
                    content = self._drission_page.html
                else:
                    title = await self._page.title()
                    content = await self._page.content()

                # Cloudflare 验证页面的指示器（英文和中文）
                cf_indicators = [
                    # 英文
                    "Just a moment",
                    "Checking your browser",
                    "Please wait",
                    "cf-browser-verification",
                    "challenge-running",
                    # 中文
                    "确认您是真人",
                    "请稍候",
                    "正在检查您的浏览器",
                    "请等待",
                    # Turnstile 相关
                    "cf-turnstile",
                    "turnstile-wrapper",
                ]

                is_cf_page = any(indicator in title or indicator in content for indicator in cf_indicators)

                if not is_cf_page:
                    logger.info("Cloudflare 验证通过")
                    return True

                # nodriver: 尝试点击 Turnstile 复选框
                if self.engine == "nodriver":
                    try:
                        # 尝试多种选择器
                        turnstile = await current_tab.select("#cf-turnstile", timeout=1)
                        if not turnstile:
                            turnstile = await current_tab.select(".cf-turnstile", timeout=1)
                        if not turnstile:
                            turnstile = await current_tab.select("iframe[src*='challenges.cloudflare.com']", timeout=1)
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
