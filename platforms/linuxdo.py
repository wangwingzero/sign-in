#!/usr/bin/env python3
"""
LinuxDo 论坛签到适配器

使用 Patchright (反检测 Playwright) + curl_cffi 实现自动签到。

Requirements:
- 2.3: 使用 Patchright 替代 DrissionPage 提升反检测能力
- 2.5: 保持浏览帖子、随机点赞功能
"""

import asyncio
import os
import random
import time
from typing import Optional

from bs4 import BeautifulSoup
from curl_cffi import requests
from loguru import logger
from patchright.async_api import async_playwright, Browser, Page
from tabulate import tabulate

from platforms.base import BasePlatformAdapter, CheckinResult, CheckinStatus
from utils.retry import retry_decorator

# LinuxDo URLs
HOME_URL = "https://linux.do/"
LOGIN_URL = "https://linux.do/login"
SESSION_URL = "https://linux.do/session"
CSRF_URL = "https://linux.do/session/csrf"


class LinuxDoAdapter(BasePlatformAdapter):
    """LinuxDo 论坛签到适配器
    
    使用 Patchright 进行浏览器自动化（反检测），curl_cffi 进行 API 请求。
    支持浏览帖子和随机点赞功能。
    
    账号等级策略：
    - level=1 (激进): 快速刷帖，每批 15-25 楼，延迟短
    - level=2 (中等): 平衡模式，每批 8-15 楼，延迟中等
    - level=3 (保守): 慢速浏览，每批 5-10 楼，延迟长
    """
    
    # 不同等级的浏览策略配置
    LEVEL_CONFIGS = {
        1: {  # 激进模式（1级号）- 约 80 分钟
            "duration_min": 4200,  # 70 分钟
            "duration_max": 5400,  # 90 分钟
            "batch_size_min": 15,
            "batch_size_max": 25,
            "read_time_min": 0.5,  # 每楼阅读时间（秒）
            "read_time_max": 1.5,
            "batch_delay_min": 0.3,
            "batch_delay_max": 0.8,
            "max_posts_per_topic": 100,  # 每帖最多读多少楼
            "time_per_post_min": 1000,  # timings 每楼时间（毫秒）
            "time_per_post_max": 2000,
        },
        2: {  # 中等模式（2级号）- 约 40 分钟
            "duration_min": 2100,  # 35 分钟
            "duration_max": 2700,  # 45 分钟
            "batch_size_min": 8,
            "batch_size_max": 15,
            "read_time_min": 1.0,
            "read_time_max": 3.0,
            "batch_delay_min": 0.5,
            "batch_delay_max": 1.5,
            "max_posts_per_topic": 50,
            "time_per_post_min": 2000,
            "time_per_post_max": 4000,
        },
        3: {  # 保守模式（3级号）- 约 20 分钟
            "duration_min": 1000,  # 17 分钟
            "duration_max": 1400,  # 23 分钟
            "batch_size_min": 5,
            "batch_size_max": 10,
            "read_time_min": 2.0,
            "read_time_max": 5.0,
            "batch_delay_min": 1.0,
            "batch_delay_max": 3.0,
            "max_posts_per_topic": 30,
            "time_per_post_min": 3000,
            "time_per_post_max": 6000,
        },
    }
    
    def __init__(
        self,
        username: str,
        password: str,
        browse_enabled: bool = True,
        browse_duration: int = 120,
        level: int = 2,
        account_name: Optional[str] = None,
    ):
        """初始化 LinuxDo 适配器
        
        Args:
            username: LinuxDo 用户名
            password: LinuxDo 密码
            browse_enabled: 是否启用浏览帖子功能
            browse_duration: 浏览时长（秒），默认 120 秒（2分钟）
            level: 账号等级 1=激进 2=中等 3=保守，默认 2
            account_name: 账号显示名称（可选）
        """
        self.username = username
        self.password = password
        self.browse_enabled = browse_enabled
        self.browse_duration = browse_duration
        self.level = max(1, min(3, level))  # 限制在 1-3
        self._account_name = account_name
        
        # 获取当前等级的配置
        self._level_config = self.LEVEL_CONFIGS[self.level]
        
        self._playwright = None
        self.browser: Optional[Browser] = None
        self.context = None
        self.page: Optional[Page] = None
        self.session: Optional[requests.Session] = None
        self._connect_info: Optional[dict] = None
        self._hot_topics: list[dict] = []
        
        # 随机化的 User-Agent（在初始化时生成，保持一致性）
        self._user_agent: Optional[str] = None
    
    @property
    def platform_name(self) -> str:
        return "LinuxDo"
    
    @property
    def account_name(self) -> str:
        return self._account_name if self._account_name else self.username
    
    def _get_random_user_agent(self) -> str:
        """生成随机 User-Agent（模拟不同的 Chrome 版本和系统）

        注意：版本号需要定期更新以匹配当前主流浏览器版本
        """
        # 2025年1月主流 Chrome 版本（保持更新）
        chrome_versions = [
            "128.0.0.0", "129.0.0.0", "130.0.0.0", "131.0.0.0", "132.0.0.0",
            "133.0.0.0", "134.0.0.0", "135.0.0.0", "136.0.0.0",
        ]

        # Windows 10/11 的不同版本
        windows_versions = [
            "Windows NT 10.0; Win64; x64",  # Windows 10/11 64位
        ]

        # macOS 版本（增加多样性）
        macos_versions = [
            "Macintosh; Intel Mac OS X 10_15_7",
            "Macintosh; Intel Mac OS X 11_6_0",
            "Macintosh; Intel Mac OS X 12_6_0",
            "Macintosh; Intel Mac OS X 13_4_0",
            "Macintosh; Intel Mac OS X 14_0_0",
        ]

        chrome_ver = random.choice(chrome_versions)

        # 70% Windows, 30% macOS
        if random.random() < 0.7:
            os_ver = random.choice(windows_versions)
        else:
            os_ver = random.choice(macos_versions)

        return (
            f"Mozilla/5.0 ({os_ver}) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{chrome_ver} Safari/537.36"
        )
    
    def _get_random_viewport(self) -> dict:
        """生成随机 viewport 尺寸（模拟不同的屏幕分辨率）

        注意：避免使用 1280x720，这是 Playwright 默认值，容易被检测
        """
        viewports = [
            {"width": 1920, "height": 1080},  # 1080p（最常见）
            {"width": 1920, "height": 1080},  # 权重加倍
            {"width": 1366, "height": 768},   # 常见笔记本
            {"width": 1536, "height": 864},   # 缩放后的 1080p
            {"width": 1440, "height": 900},   # MacBook
            {"width": 1600, "height": 900},   # 16:9 宽屏
            {"width": 1680, "height": 1050},  # 16:10 宽屏
            {"width": 1280, "height": 800},   # 常见笔记本
            {"width": 1512, "height": 982},   # MacBook Pro 14"
            {"width": 1728, "height": 1117},  # MacBook Pro 16"
        ]
        return random.choice(viewports)
    
    async def _init_browser(self) -> None:
        """初始化 Patchright 浏览器
        
        使用持久化上下文 (Persistent Context) + 真实 Chrome 来绕过 Cloudflare 检测。
        关键配置：
        - channel="chrome" 使用系统安装的真实 Chrome
        - no_viewport=True 不设置 viewport，使用默认窗口大小
        - 不设置 user_agent，使用浏览器默认值
        
        根据环境自动选择 headless 模式：
        - GitHub Actions (CI=true): headless=True
        - 本地开发: headless=False（可通过 HEADLESS=true 强制无头）
        """
        self._playwright = await async_playwright().start()
        
        # 自动检测是否在 CI 环境（GitHub Actions 会设置 CI=true）
        is_ci = os.environ.get("CI", "").lower() == "true"
        # 也支持手动设置 HEADLESS 环境变量
        force_headless = os.environ.get("HEADLESS", "").lower() == "true"
        use_headless = is_ci or force_headless
        
        logger.info(f"浏览器模式: {'无头 (headless)' if use_headless else '有头 (headed)'}")
        
        # 使用持久化上下文来绕过 Cloudflare CDP 检测
        # 创建临时用户数据目录
        import tempfile
        user_data_dir = tempfile.mkdtemp(prefix="patchright_")
        self._user_data_dir = user_data_dir  # 保存以便清理
        
        # 关键配置：
        # 1. channel="chrome" - 使用真实 Chrome 而不是 Chromium
        # 2. no_viewport=True - 不设置 viewport，减少被检测的风险
        # 3. 不设置 user_agent - 使用浏览器默认值
        self.context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            channel="chrome",  # 使用真实 Chrome
            headless=use_headless,
            no_viewport=True,  # 不设置 viewport
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",  # 最大化窗口
            ]
        )
        
        # 持久化上下文自带一个页面，或者创建新页面
        if self.context.pages:
            self.page = self.context.pages[0]
        else:
            self.page = await self.context.new_page()
        
        # 持久化上下文没有单独的 browser 对象
        self.browser = None
        
        # 设置默认 User-Agent（用于 HTTP 请求）
        self._user_agent = self._get_random_user_agent()
    
    def _init_session(self) -> None:
        """初始化 HTTP 会话（使用与浏览器相同的 User-Agent）"""
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self._user_agent or self._get_random_user_agent(),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
    
    async def cleanup(self) -> None:
        """清理资源（关闭浏览器、HTTP 客户端、临时目录等）"""
        try:
            # 关闭持久化上下文
            if self.context:
                await self.context.close()
                self.context = None
            
            # 关闭 Playwright
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None
            
            # 清理临时用户数据目录
            if hasattr(self, '_user_data_dir') and self._user_data_dir:
                import shutil
                try:
                    shutil.rmtree(self._user_data_dir, ignore_errors=True)
                except Exception:
                    pass
                self._user_data_dir = None
            
            # 关闭 HTTP 会话
            if self.session:
                self.session.close()
                self.session = None
                
            logger.debug("资源清理完成")
        except Exception as e:
            logger.warning(f"清理资源时出错: {e}")
    
    async def _wait_for_cloudflare(self, timeout: int = 60) -> bool:
        """等待 Cloudflare 挑战完成
        
        通过多种方式检测验证是否通过：
        1. cf_clearance Cookie 出现
        2. 登录表单元素出现
        3. 页面标题不再是 "Just a moment"
        4. 挑战容器消失
        
        Args:
            timeout: 最大等待时间（秒），默认 60 秒
            
        Returns:
            是否通过验证
        """
        for i in range(timeout):
            try:
                # 方法1: 检查页面标题是否还是 Cloudflare 挑战页
                title = await self.page.title()
                is_cloudflare_page = title and "just a moment" in title.lower()
                
                if not is_cloudflare_page:
                    # 标题已变化，可能已通过
                    
                    # 方法2: 检查是否有 cf_clearance Cookie
                    cookies = await self.context.cookies()
                    has_clearance = any(c["name"] == "cf_clearance" for c in cookies)
                    
                    # 方法3: 检查页面是否有登录表单（Discourse 登录页特征元素）
                    # Discourse 使用 #login-account-name
                    login_form = await self.page.query_selector(
                        "#login-account-name, #login-account-password, "
                        ".login-modal, .login-page, #login-button"
                    )
                    
                    # 方法4: 检查是否有 Discourse 特征元素
                    discourse_ele = await self.page.query_selector(
                        "#main-outlet, .d-header, body.discourse, .ember-application"
                    )
                    
                    if has_clearance or login_form or discourse_ele:
                        logger.info(f"Cloudflare 验证通过 (耗时 {i+1} 秒)")
                        return True
                    
                    # 如果标题不是 Cloudflare 但也没检测到 Discourse 元素，
                    # 可能页面还在加载，继续等待
                    if i > 5:  # 等待超过 5 秒后，如果标题已变化就认为通过
                        logger.info(f"Cloudflare 验证可能已通过 (标题已变化，耗时 {i+1} 秒)")
                        return True
                
                # 检查是否还在 Cloudflare 挑战页面
                challenge_running = await self.page.query_selector("#challenge-running, #challenge-stage")
                challenge_frame = await self.page.query_selector("iframe[src*='challenges.cloudflare.com']")
                
                if challenge_running or challenge_frame:
                    if i % 5 == 0:  # 每 5 秒打印一次
                        logger.debug(f"检测到 Cloudflare 挑战，等待中... ({i+1}/{timeout})")
                
                await asyncio.sleep(1)
                
            except Exception as e:
                logger.debug(f"Cloudflare 检测出错: {e}")
                await asyncio.sleep(1)
        
        return False
    
    async def _simulate_human_behavior(self) -> None:
        """模拟人类行为，帮助通过 Cloudflare 检测"""
        try:
            # 随机移动鼠标
            for _ in range(random.randint(2, 4)):
                x = random.randint(100, 800)
                y = random.randint(100, 600)
                await self.page.mouse.move(x, y)
                await asyncio.sleep(random.uniform(0.1, 0.3))
            
            # 随机滚动
            await self.page.mouse.wheel(0, random.randint(100, 300))
            await asyncio.sleep(random.uniform(0.5, 1.0))
            
            # 额外等待，让页面 JS 完全执行
            await asyncio.sleep(random.uniform(1.0, 2.0))
        except Exception as e:
            logger.debug(f"模拟人类行为时出错: {e}")
    
    async def login(self) -> bool:
        """执行登录操作 - 纯浏览器表单登录
        
        策略：
        1. 用 Patchright 浏览器访问登录页，通过 Cloudflare 验证
        2. 等待 Discourse SPA 完全加载（关键！）
        3. 填写表单并点击登录按钮
        4. 等待登录完成
        """
        # 启动前随机预热延迟（1-5秒），模拟人类打开浏览器的准备时间
        warmup_delay = random.uniform(1.0, 5.0)
        logger.debug(f"预热延迟 {warmup_delay:.1f} 秒...")
        await asyncio.sleep(warmup_delay)

        logger.info("开始登录 LinuxDo")
        
        await self._init_browser()
        self._init_session()
        
        # Step 1: 先访问首页，让 Discourse 初始化
        # 这是绕过 Cloudflare 后让 Discourse SPA 正确加载的关键
        # 使用 domcontentloaded 而不是 load，避免等待所有资源导致超时
        logger.info("访问首页...")
        try:
            await self.page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            logger.warning(f"首页加载超时，尝试继续: {e}")
        
        # 等待 Cloudflare 验证
        logger.info("等待 Cloudflare 验证...")
        cf_passed = await self._wait_for_cloudflare()
        if not cf_passed:
            logger.error("Cloudflare 验证超时")
            return False
        
        # 等待 Discourse 加载
        logger.info("等待 Discourse 初始化...")
        await asyncio.sleep(3)
        
        # 检查 Discourse 是否加载
        discourse_ele = await self.page.query_selector("#main-outlet, .d-header")
        if not discourse_ele:
            logger.warning("Discourse 未加载，等待更长时间...")
            for i in range(30):
                await asyncio.sleep(1)
                discourse_ele = await self.page.query_selector("#main-outlet, .d-header")
                if discourse_ele:
                    logger.info(f"Discourse 已加载 (等待 {i+1} 秒)")
                    break
        
        # Step 2: 访问登录页
        logger.info("访问登录页面...")
        try:
            await self.page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            logger.warning(f"登录页加载超时，尝试继续: {e}")
        
        # Step 3: 模拟人类行为
        await self._simulate_human_behavior()
        
        # Step 4: 等待登录表单完全加载（Discourse SPA 需要时间）
        logger.info("等待登录表单...")
        await asyncio.sleep(3)  # 先等待一下
        try:
            # Discourse 登录表单用户名字段是 #login-account-name
            await self.page.wait_for_selector("#login-account-name", timeout=30000)
            # 额外等待确保 Ember.js 完全渲染和事件绑定
            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"登录表单加载超时: {e}")
            return False
        
        # Step 5: 填写用户名
        logger.info("填写登录信息...")
        username_locator = self.page.locator("#login-account-name")
        password_locator = self.page.locator("#login-account-password")
        
        # 点击并输入用户名
        await username_locator.click()
        await asyncio.sleep(random.uniform(0.3, 0.6))
        await username_locator.type(self.username, delay=random.randint(80, 150))
        await asyncio.sleep(random.uniform(0.5, 1.0))
        
        # Step 6: 填写密码
        await password_locator.click()
        await asyncio.sleep(random.uniform(0.3, 0.6))
        await password_locator.type(self.password, delay=random.randint(80, 150))
        await asyncio.sleep(random.uniform(0.8, 1.5))
        
        # Step 7: 点击登录按钮
        logger.info("点击登录按钮...")
        login_button = self.page.locator("#login-button")
        
        # 等待按钮可点击
        await login_button.wait_for(state="visible", timeout=5000)
        await asyncio.sleep(0.5)
        
        # 使用真实点击（不是 dispatch_event）
        await login_button.click()
        
        # Step 8: 等待登录结果
        logger.info("等待登录结果...")
        
        # 等待页面变化：要么出现用户头像，要么出现错误信息
        try:
            # 等待用户头像出现（登录成功）或错误信息出现
            await self.page.wait_for_selector(
                "#current-user, .alert-error, .login-error, #modal-alert",
                timeout=30000
            )
        except Exception:
            pass
        
        await asyncio.sleep(2)
        
        # Step 9: 检查登录结果
        # 检查是否有错误信息
        error_ele = await self.page.query_selector(".alert-error, .login-error, #modal-alert")
        if error_ele:
            error_text = await error_ele.inner_text()
            if error_text.strip():
                logger.error(f"登录失败: {error_text}")
                return False
        
        # Step 10: 多种方式验证登录状态
        # 方法1: 检查 #current-user 元素
        user_ele = await self.page.query_selector("#current-user")
        if user_ele:
            logger.info("登录成功! (检测到用户头像)")
        else:
            # 方法2: 检查 body 是否没有 .anon 类（已登录用户没有这个类）
            has_anon = await self.page.evaluate("document.body.classList.contains('anon')")
            if not has_anon:
                logger.info("登录成功! (body 无 anon 类)")
            else:
                # 方法3: 尝试访问首页再次确认
                await self.page.goto(HOME_URL, wait_until="domcontentloaded")
                await asyncio.sleep(3)
                
                # 再次检查
                user_ele = await self.page.query_selector("#current-user")
                has_anon = await self.page.evaluate("document.body.classList.contains('anon')")
                
                if user_ele or not has_anon:
                    logger.info("登录成功! (首页验证)")
                else:
                    # 方法4: 使用 Discourse API 检查
                    user_info = await self.page.evaluate("Discourse.User.current()")
                    if user_info:
                        logger.info(f"登录成功! (API 验证: {user_info.get('username', 'unknown')})")
                    else:
                        logger.error("登录验证失败")
                        return False
        
        # Step 10: 同步浏览器 Cookie 到 curl_cffi session（用于后续 API 请求）
        browser_cookies = await self.context.cookies()
        for cookie in browser_cookies:
            if "linux.do" in cookie.get("domain", ""):
                self.session.cookies.set(
                    cookie["name"], 
                    cookie["value"], 
                    domain=cookie.get("domain", ".linux.do")
                )
        
        # 获取 Connect 信息
        self._fetch_connect_info()
        
        return True
    
    async def checkin(self) -> CheckinResult:
        """执行签到操作（浏览帖子）"""
        details = {}
        
        # 添加 Connect 信息到详情（如果之前获取成功）
        if self._connect_info:
            details["connect_info"] = self._connect_info
        
        # 跳过热门话题收集，直接进帖子浏览
        # await self._collect_hot_topics()
        # if self._hot_topics:
        #     details["hot_topics"] = self._hot_topics
        
        if not self.browse_enabled:
            return CheckinResult(
                platform=self.platform_name,
                account=self.account_name,
                status=CheckinStatus.SUCCESS,
                message="登录成功（浏览功能已禁用）",
                details=details if details else None,
            )
        
        # 浏览帖子
        try:
            browse_count = await self._click_topics()
            if browse_count == 0:
                return CheckinResult(
                    platform=self.platform_name,
                    account=self.account_name,
                    status=CheckinStatus.FAILED,
                    message="浏览帖子失败",
                    details=details if details else None,
                )
            
            return CheckinResult(
                platform=self.platform_name,
                account=self.account_name,
                status=CheckinStatus.SUCCESS,
                message=f"登录成功，浏览了 {browse_count} 个帖子",
                details=details if details else None,
            )
        except Exception as e:
            logger.error(f"浏览帖子异常: {e}")
            return CheckinResult(
                platform=self.platform_name,
                account=self.account_name,
                status=CheckinStatus.FAILED,
                message=f"浏览帖子异常: {str(e)}",
                details=details if details else None,
            )
    
    async def get_status(self) -> dict:
        """获取 Connect 信息"""
        if self._connect_info:
            return self._connect_info
        self._fetch_connect_info()
        return self._connect_info or {}
    
    async def cleanup(self) -> None:
        """清理浏览器资源"""
        if self.page:
            try:
                await self.page.close()
            except Exception:
                pass
            self.page = None
        
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
            self.context = None
        
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
            self.browser = None
        
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        
        if self.session:
            self.session = None
    
    def _fetch_connect_info(self) -> None:
        """获取 Connect 信息"""
        logger.info("获取 Connect 信息")
        try:
            headers = {
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,image/apng,*/*;q=0.8,"
                    "application/signed-exchange;v=b3;q=0.7"
                ),
            }
            resp = self.session.get(
                "https://connect.linux.do/",
                headers=headers,
                impersonate="chrome136",
            )
            soup = BeautifulSoup(resp.text, "html.parser")
            rows = soup.select("table tr")
            
            info = {}
            table_data = []
            
            for row in rows:
                cells = row.select("td")
                if len(cells) >= 3:
                    project = cells[0].text.strip()
                    current = cells[1].text.strip() or "0"
                    requirement = cells[2].text.strip() or "0"
                    info[project] = {"current": current, "requirement": requirement}
                    table_data.append([project, current, requirement])
            
            if table_data:
                print("--------------Connect Info-----------------")
                print(tabulate(table_data, headers=["项目", "当前", "要求"], tablefmt="pretty"))
            
            self._connect_info = info
        except Exception as e:
            logger.warning(f"获取 Connect 信息失败: {e}")
            self._connect_info = {}
    
    async def _click_topics(self) -> int:
        """简单直接地浏览帖子 - 直接点击链接浏览，避免多次 goto 触发检测
        
        策略：
        1. 当前页面已经是首页（登录后自动跳转）
        2. 直接点击帖子链接进去浏览
        3. 用浏览器后退返回，不用 goto 刷新
        4. 继续点击下一个帖子
        """
        # 检查当前是否在首页，如果不是才导航
        current_url = self.page.url
        if "linux.do" not in current_url or "/login" in current_url:
            logger.info("导航到首页...")
            await self.page.goto("https://linux.do/latest", wait_until="domcontentloaded")
            await asyncio.sleep(random.uniform(1.0, 2.0))
        else:
            logger.info("已在首页，直接开始浏览...")
            # 短暂等待确保页面稳定
            await asyncio.sleep(random.uniform(0.5, 1.0))
        await asyncio.sleep(random.uniform(1.5, 2.5))
        
        # 等待帖子列表加载 - 尝试多种选择器
        selectors_to_try = [
            ".topic-list-item",
            "tr[data-topic-id]",
            ".topic-list tr",
            "#list-area tr",
        ]
        
        topic_rows = []
        for selector in selectors_to_try:
            try:
                await self.page.wait_for_selector(selector, timeout=5000)
                topic_rows = await self.page.locator(selector).all()
                if topic_rows:
                    logger.info(f"使用选择器 {selector} 找到 {len(topic_rows)} 个帖子")
                    break
            except Exception:
                continue
        
        if not topic_rows:
            # 最后尝试：直接获取所有链接
            logger.warning("标准选择器未找到帖子，尝试获取所有帖子链接...")
            all_links = await self.page.locator("a[href*='/t/']").all()
            if all_links:
                logger.info(f"找到 {len(all_links)} 个帖子链接")
                # 直接使用链接
                return await self._browse_links_directly(all_links)
            
            logger.warning("没有找到任何帖子")
            return 0
        
        logger.info(f"发现 {len(topic_rows)} 个帖子")
        
        # 决定浏览多少个
        max_topics = min(len(topic_rows), max(5, self.browse_duration // 20))
        logger.info(f"计划浏览 {max_topics} 个帖子")
        
        browsed_count = 0
        start_time = time.time()
        
        for i in range(max_topics):
            # 检查是否超时
            elapsed = time.time() - start_time
            if elapsed >= self.browse_duration:
                logger.info(f"已达到目标浏览时间 {self.browse_duration} 秒")
                break
            
            try:
                # 重新获取帖子列表（因为 DOM 可能变化）
                for selector in selectors_to_try:
                    topic_rows = await self.page.locator(selector).all()
                    if topic_rows:
                        break
                
                if i >= len(topic_rows):
                    break
                
                row = topic_rows[i]
                
                # 获取标题链接
                title_link = row.locator("a.title, .main-link a, a.raw-link").first
                if await title_link.count() == 0:
                    continue
                
                title = await title_link.inner_text()
                title = title.strip()[:30]
                
                # 检查是否是屏蔽分类
                row_text = await row.inner_text()
                if any(blocked in row_text for blocked in self.BLOCKED_CATEGORIES):
                    logger.debug(f"跳过屏蔽分类: {title}")
                    continue
                
                logger.info(f"[{browsed_count + 1}] 点击进入: {title}...")
                
                # 先关闭可能存在的弹窗（避免拦截点击）
                await self._dismiss_dialog(self.page)
                
                # 直接点击链接进入帖子（不用 goto，更自然）
                # 使用 force=True 绕过可能的拦截检查
                await title_link.click(force=True)
                await asyncio.sleep(random.uniform(1.5, 2.5))
                
                # 等待帖子内容加载
                try:
                    await self.page.wait_for_selector(".topic-post, .post-stream", timeout=10000)
                except Exception:
                    pass
                
                # 浏览帖子
                await self._browse_post(self.page)
                browsed_count += 1
                
                # 用浏览器后退返回首页（不用 goto 刷新，避免被检测）
                logger.debug("后退返回首页...")
                await self.page.go_back(wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(0.8, 1.5))
                
            except Exception as e:
                logger.warning(f"浏览帖子失败: {e}")
                # 如果出错，尝试返回首页
                try:
                    await self.page.go_back(wait_until="domcontentloaded")
                    await asyncio.sleep(1)
                except Exception:
                    pass
                continue
        
        total_time = time.time() - start_time
        logger.info(f"浏览完成，共浏览 {browsed_count} 个帖子，用时 {total_time:.0f} 秒")
        return browsed_count
    
    async def _browse_links_directly(self, links: list) -> int:
        """直接浏览链接列表（备用方法）"""
        browsed_count = 0
        start_time = time.time()
        max_topics = min(len(links), max(5, self.browse_duration // 20))
        
        for i, link in enumerate(links[:max_topics]):
            elapsed = time.time() - start_time
            if elapsed >= self.browse_duration:
                break
            
            try:
                href = await link.get_attribute("href")
                if not href or "/t/" not in href:
                    continue
                
                if not href.startswith("http"):
                    href = f"https://linux.do{href}"
                
                title = await link.inner_text()
                title = title.strip()[:30]
                logger.info(f"[{browsed_count + 1}] 浏览: {title}...")
                
                new_page = await self.context.new_page()
                await new_page.goto(href, wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(1.5, 2.5))
                
                await self._browse_post(new_page)
                
                if random.random() < 0.3:
                    await self._click_like(new_page)
                
                browsed_count += 1
                await new_page.close()
                await asyncio.sleep(random.uniform(0.5, 1.5))
                
            except Exception as e:
                logger.warning(f"浏览链接失败: {e}")
                continue
        
        total_time = time.time() - start_time
        logger.info(f"浏览完成，共浏览 {browsed_count} 个帖子，用时 {total_time:.0f} 秒")
        return browsed_count
    
    async def _get_topics_from_page(self, page_url: str) -> list[str]:
        """从指定页面获取帖子链接列表"""
        topic_urls = []
        
        try:
            await self.page.goto(page_url, wait_until="domcontentloaded")
            await asyncio.sleep(random.uniform(1.5, 2.5))
            
            topic_rows = await self.page.query_selector_all("#list-area tr")
            
            for row in topic_rows:
                try:
                    # 检查是否包含屏蔽分类
                    row_text = await row.inner_text()
                    should_skip = False
                    for blocked in self.BLOCKED_CATEGORIES:
                        if blocked in row_text:
                            should_skip = True
                            break
                    if should_skip:
                        continue
                    
                    # 获取链接
                    title_link = await row.query_selector(".title")
                    if not title_link:
                        continue
                    
                    href = await title_link.get_attribute("href")
                    if href:
                        if not href.startswith("http"):
                            href = f"https://linux.do{href}"
                        topic_urls.append(href)
                except Exception:
                    continue
                    
        except Exception as e:
            logger.warning(f"获取 {page_url} 帖子列表失败: {e}")
        
        return topic_urls
    
    async def _click_one_topic(self, topic_url: str) -> bool:
        """点击单个主题帖，模拟真实阅读行为标记楼层为已读
        
        策略（避免 403 检测）：
        1. 打开页面，等待 SPA 内容完全加载
        2. 分批滚动浏览，每次滚动后发送当前可见楼层的 timings
        3. 每批之间添加随机延迟，模拟真实阅读
        4. 限制每个帖子最多阅读的楼层数（根据等级配置）
        """
        topic_id = self._extract_topic_id(topic_url)
        if not topic_id:
            logger.warning(f"无法提取帖子 ID: {topic_url}")
            return False
        
        new_page = await self.context.new_page()
        try:
            # 1. 打开页面，等待 SPA 内容加载
            await new_page.goto(topic_url, wait_until="domcontentloaded")
            
            # 等待 Discourse SPA 内容加载完成（等待帖子元素出现）
            try:
                await new_page.wait_for_selector(".topic-post", timeout=10000)
            except Exception:
                # 如果超时，可能是页面加载慢，继续尝试
                logger.debug(f"等待帖子元素超时，继续尝试...")
            
            # 额外等待确保 Ember.js 渲染完成
            await asyncio.sleep(random.uniform(1.5, 3.0))
            
            # 获取帖子标题
            title = await new_page.evaluate("document.title") or ""
            title = title.replace(" - LinuxDo", "").replace(" - LINUX DO", "")[:30]
            
            # 2. 分批滚动并发送 timings
            posts_read = await self._scroll_and_read(new_page, topic_id, title)
            
            # 3. 30% 概率点赞
            if posts_read > 0 and random.random() < 0.3:
                await self._click_like(new_page)
            
            return posts_read > 0
        except Exception as e:
            logger.warning(f"浏览帖子失败: {e}")
            return False
        finally:
            try:
                await new_page.close()
            except Exception:
                pass
    
    async def _scroll_and_read(self, page: Page, topic_id: int, title: str) -> int:
        """滚动页面并分批发送 timings，模拟真实阅读
        
        根据账号等级 (self.level) 调整浏览策略：
        - level=1: 激进，快速刷楼
        - level=2: 中等，平衡速度和安全
        - level=3: 保守，慢速浏览
        
        Args:
            page: 浏览器页面
            topic_id: 帖子 ID
            title: 帖子标题（用于日志）
            
        Returns:
            成功标记的楼层数
        """
        cfg = self._level_config
        total_read = 0
        max_posts = cfg["max_posts_per_topic"]
        batch_size = random.randint(cfg["batch_size_min"], cfg["batch_size_max"])
        current_post = 1
        
        # 获取总楼层数
        total_posts = await page.evaluate("""
            () => {
                const posts = document.querySelectorAll('.topic-post');
                return posts.length;
            }
        """) or 0
        
        # 如果页面没有加载出帖子，跳过
        if total_posts == 0:
            logger.warning(f"帖子「{title}...」未加载出内容，跳过")
            return 0
        
        level_names = {1: "激进", 2: "中等", 3: "保守"}
        logger.info(f"帖子「{title}...」共 {total_posts} 楼，等级{self.level}({level_names[self.level]})，每批 {batch_size} 楼")
        
        while current_post <= min(total_posts, max_posts):
            # 计算本批要读的楼层
            end_post = min(current_post + batch_size - 1, total_posts, max_posts)
            
            # 滚动到对应位置
            await self._scroll_to_post(page, end_post)
            
            # 等待一段时间模拟阅读（根据等级调整）
            read_time = random.uniform(cfg["read_time_min"], cfg["read_time_max"]) * (end_post - current_post + 1)
            read_time = min(read_time, 20.0)  # 最多等 20 秒
            await asyncio.sleep(read_time)
            
            # 发送这批楼层的 timings
            success = await self._send_timings_batch(
                page, topic_id, current_post, end_post
            )
            
            if success:
                total_read += (end_post - current_post + 1)
            
            current_post = end_post + 1
            
            # 批次之间随机延迟（根据等级调整）
            if current_post <= min(total_posts, max_posts):
                await asyncio.sleep(random.uniform(cfg["batch_delay_min"], cfg["batch_delay_max"]))
        
        if total_read > 0:
            logger.info(f"✓ 帖子「{title}...」已读 {total_read} 楼")
        
        return total_read
    
    async def _scroll_to_post(self, page: Page, post_number: int) -> None:
        """滚动到指定楼层"""
        try:
            await page.evaluate(f"""
                () => {{
                    const posts = document.querySelectorAll('.topic-post');
                    const targetPost = posts[{post_number - 1}];
                    if (targetPost) {{
                        targetPost.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
                    }} else {{
                        // 如果找不到具体楼层，就滚动一段距离
                        window.scrollBy({{ top: 800, behavior: 'smooth' }});
                    }}
                }}
            """)
        except Exception:
            pass
    
    async def _send_timings_batch(
        self, page: Page, topic_id: int, start_post: int, end_post: int
    ) -> bool:
        """发送一批楼层的 timings
        
        Args:
            page: 浏览器页面
            topic_id: 帖子 ID
            start_post: 起始楼层号
            end_post: 结束楼层号
            
        Returns:
            是否成功
        """
        try:
            cfg = self._level_config
            # 每楼阅读时间（根据等级调整）
            time_per_post = random.randint(cfg["time_per_post_min"], cfg["time_per_post_max"])
            post_count = end_post - start_post + 1
            total_time = post_count * time_per_post
            
            # 构建 timings 参数（添加随机波动）
            timings_params = "&".join([
                f"timings%5B{i}%5D={time_per_post + random.randint(-500, 500)}" 
                for i in range(start_post, end_post + 1)
            ])
            
            body = f"topic_id={topic_id}&topic_time={total_time}&{timings_params}"
            
            result = await page.evaluate(f"""
                (async () => {{
                    try {{
                        const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content;
                        if (!csrfToken) return {{ success: false, error: 'no csrf token' }};
                        
                        const resp = await fetch('/topics/timings', {{
                            method: 'POST',
                            headers: {{
                                'Content-Type': 'application/x-www-form-urlencoded',
                                'X-CSRF-Token': csrfToken,
                                'X-Requested-With': 'XMLHttpRequest'
                            }},
                            body: `{body}`
                        }});
                        
                        return {{ success: resp.ok, status: resp.status }};
                    }} catch (e) {{
                        return {{ success: false, error: e.message }};
                    }}
                }})();
            """)
            
            if result and result.get("success"):
                return True
            else:
                # 使用 warning 级别日志，方便排查问题
                error_info = result.get("error", "unknown") if result else "no result"
                status = result.get("status", "N/A") if result else "N/A"
                logger.warning(f"timings 批次失败: status={status}, error={error_info}")
                return False
                
        except Exception as e:
            logger.warning(f"发送 timings 批次异常: {e}")
            return False

    def _extract_topic_id(self, url: str) -> Optional[int]:
        """从 URL 提取帖子 ID
        
        支持多种 Discourse URL 格式：
        - https://linux.do/t/topic/123456
        - https://linux.do/t/some-slug/123456
        - https://linux.do/t/some-slug/123456/5 (带楼层号)
        """
        try:
            import re
            # 通用模式：/t/ 后面跟 slug，再跟 /数字
            match = re.search(r'/t/[^/]+/(\d+)', url)
            if match:
                return int(match.group(1))
        except Exception:
            pass
        return None
    
    def _get_topic_info(self, topic_id: int) -> Optional[dict]:
        """通过 API 获取帖子信息（包括总楼层数）
        
        Returns:
            dict with keys: highest_post_number, posts_count, title
        """
        try:
            resp = self.session.get(
                f"https://linux.do/t/topic/{topic_id}.json",
                impersonate="chrome136",
            )
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "highest_post_number": data.get("highest_post_number", 1),
                    "posts_count": data.get("posts_count", 1),
                    "title": data.get("title", ""),
                }
        except Exception as e:
            logger.debug(f"获取帖子信息失败: {e}")
        return None
    
    async def _browse_post(self, page: Page) -> None:
        """浏览帖子内容 - 触发 Discourse 原生阅读追踪
        
        Discourse 阅读追踪机制（关键发现）：
        1. screen-track.js 每秒检查视口内的帖子
        2. 帖子需要在视口内停留足够时间（基于内容长度）
        3. 3分钟无滚动会暂停追踪
        4. 可以用 screenTrack.flush() 强制同步到服务器
        5. 蓝点消失需要：last_read_post_number >= highest_post_number
        
        策略：
        1. 慢速滚动，让每个帖子在视口内停留足够时间
        2. 定期触发 flush() 强制同步
        3. 模拟真实用户行为（鼠标移动、偶尔回滚）
        4. 确保最小浏览时间（即使帖子很短）
        5. 滚动到底部后额外停留，确保最后一个帖子被记录
        """
        # 等待帖子内容加载
        await asyncio.sleep(random.uniform(2.0, 3.0))
        
        # 确保页面处于活跃状态
        await page.bring_to_front()
        
        # 从 URL 获取 topic_id
        current_url = page.url
        topic_id = self._extract_topic_id(current_url)
        
        logger.info(f"开始浏览帖子 (topic_id={topic_id})...")
        
        # 检查 Discourse 环境是否可用
        has_discourse = await page.evaluate("typeof Discourse !== 'undefined'")
        if not has_discourse:
            logger.warning("Discourse 环境不可用，使用回退模式")
            await self._browse_post_fallback(page)
            return
        
        # 获取帖子的楼层信息
        post_info = await page.evaluate("""
            () => {
                const posts = document.querySelectorAll('.topic-post[data-post-number]');
                return Array.from(posts).map(p => ({
                    id: parseInt(p.getAttribute('data-post-id')),
                    number: parseInt(p.getAttribute('data-post-number'))
                })).filter(p => !isNaN(p.number));
            }
        """) or []
        
        total_posts = len(post_info)
        highest_post_number = max([p["number"] for p in post_info]) if post_info else 0
        
        if total_posts > 0:
            logger.info(f"检测到 {total_posts} 个楼层，最高楼层号: {highest_post_number}")
        
        scroll_count = 0
        max_scrolls = 50  # 增加最大滚动次数
        flush_interval = 4  # 每 4 次滚动 flush 一次
        
        # 最小浏览时间（秒）- 即使帖子很短也要停留这么久
        min_browse_time = random.uniform(15, 25)
        start_time = time.time()
        
        # 先在顶部停留一会，让第一个帖子被记录
        logger.debug("在顶部停留，记录第一个帖子...")
        await asyncio.sleep(random.uniform(3.0, 5.0))
        
        while scroll_count < max_scrolls:
            try:
                elapsed = time.time() - start_time
                
                # 获取当前页面状态
                scroll_info = await page.evaluate("""
                    () => ({
                        scrollHeight: document.body.scrollHeight,
                        scrollTop: window.scrollY,
                        clientHeight: window.innerHeight
                    })
                """)
                
                current_height = scroll_info["scrollHeight"]
                scroll_position = scroll_info["scrollTop"] + scroll_info["clientHeight"]
                
                # 检查是否到达底部
                at_bottom = scroll_position >= current_height - 100
                
                if at_bottom:
                    # 到达底部后，停留足够时间让最后的帖子被记录
                    logger.debug(f"到达底部，停留等待记录... (已浏览 {elapsed:.0f}s)")
                    
                    # 在底部停留 5-8 秒，确保最后的帖子被记录
                    await asyncio.sleep(random.uniform(5.0, 8.0))
                    
                    # 触发 flush
                    await self._flush_screen_track(page)
                    
                    # 检查是否达到最小浏览时间
                    if elapsed >= min_browse_time:
                        logger.debug(f"达到最小浏览时间 {min_browse_time:.0f}s，准备退出")
                        break
                    else:
                        # 还没到最小时间，回滚一点继续浏览
                        logger.debug(f"未达到最小浏览时间，回滚继续...")
                        await page.mouse.wheel(0, -random.randint(300, 600))
                        await asyncio.sleep(random.uniform(2.0, 4.0))
                
                # 使用鼠标滚轮滚动（触发 Discourse 的滚动事件监听）
                # 关键：滚动距离要小，让帖子在视口内停留更长时间
                scroll_amount = random.randint(150, 300)
                await page.mouse.wheel(0, scroll_amount)
                scroll_count += 1
                
                # 停留阅读时间（关键：每个帖子至少需要 2-3 秒）
                # Discourse 基于内容长度计算所需时间，短帖子 1-2 秒，长帖子更久
                wait_time = random.uniform(2.5, 4.5)
                await asyncio.sleep(wait_time)
                
                # 偶尔移动鼠标（保持页面活跃，防止 3 分钟空闲暂停）
                if random.random() < 0.6:
                    try:
                        viewport = await page.evaluate("({ w: window.innerWidth, h: window.innerHeight })")
                        x = random.randint(200, max(201, viewport["w"] - 200))
                        y = random.randint(200, max(201, viewport["h"] - 200))
                        await page.mouse.move(x, y)
                    except Exception:
                        pass
                
                # 偶尔回滚一点（模拟真实阅读行为）
                if scroll_count > 3 and random.random() < 0.2:
                    back_amount = random.randint(100, 250)
                    await page.mouse.wheel(0, -back_amount)
                    await asyncio.sleep(random.uniform(1.5, 3.0))
                
                # 定期强制 flush（确保阅读时间同步到服务器）
                if scroll_count % flush_interval == 0:
                    await self._flush_screen_track(page)
                    
            except Exception as e:
                logger.warning(f"滚动时出错: {e}")
                break
        
        # 最终处理：确保阅读被记录
        total_time = time.time() - start_time
        logger.info(f"帖子浏览完成，滚动 {scroll_count} 次，用时 {total_time:.0f}s")
        
        # 最终 flush
        await self._flush_screen_track(page)
        
        # 额外等待让 Discourse 完成同步
        await asyncio.sleep(2.0)
        
        # 如果 topic_id 有效，尝试用 bulk API 强制标记已读（作为保底）
        if topic_id:
            await self._mark_topic_read(page, topic_id)
        
        # 到达底部后点赞
        await self._click_like(page)
    
    async def _flush_screen_track(self, page: Page) -> bool:
        """强制 flush screen-track 服务，同步阅读时间到服务器"""
        try:
            flush_result = await page.evaluate("""
                () => {
                    try {
                        // 方法1: 使用 Discourse.__container__（最可靠）
                        if (typeof Discourse !== 'undefined' && Discourse.__container__) {
                            const screenTrack = Discourse.__container__.lookup('service:screen-track');
                            if (screenTrack) {
                                // 尝试多种 flush 方法
                                if (typeof screenTrack.flush === 'function') {
                                    screenTrack.flush();
                                    return { success: true, method: 'container.flush' };
                                }
                                if (typeof screenTrack.sendNextConsolidatedTiming === 'function') {
                                    screenTrack.sendNextConsolidatedTiming();
                                    return { success: true, method: 'container.sendNext' };
                                }
                                // 尝试直接调用 consolidateTimings
                                if (typeof screenTrack.consolidateTimings === 'function') {
                                    screenTrack.consolidateTimings();
                                    return { success: true, method: 'container.consolidate' };
                                }
                            }
                        }
                        
                        // 方法2: 使用 require（某些版本可能需要）
                        if (typeof require !== 'undefined') {
                            try {
                                const st = require('discourse/services/screen-track').default;
                                if (st && typeof st.flush === 'function') {
                                    st.flush();
                                    return { success: true, method: 'require' };
                                }
                            } catch (e) {}
                        }
                        
                        return { success: false, error: 'no flush method found' };
                    } catch (e) {
                        return { success: false, error: e.message };
                    }
                }
            """)
            
            if flush_result and flush_result.get("success"):
                logger.debug(f"flush 成功: {flush_result.get('method')}")
                return True
            return False
        except Exception as e:
            logger.debug(f"flush 失败: {e}")
            return False
    
    async def _mark_topic_read(self, page: Page, topic_id: int) -> bool:
        """使用 Discourse bulk API 强制标记主题为已读（保底方案）
        
        这是最可靠的方式，直接调用 /topics/bulk 接口
        """
        try:
            result = await page.evaluate(f"""
                (async () => {{
                    try {{
                        const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content;
                        if (!csrfToken) return {{ success: false, error: 'no csrf token' }};
                        
                        // 使用 bulk API 标记已读
                        const resp = await fetch('/topics/bulk', {{
                            method: 'PUT',
                            headers: {{
                                'Content-Type': 'application/json',
                                'X-CSRF-Token': csrfToken,
                                'X-Requested-With': 'XMLHttpRequest'
                            }},
                            body: JSON.stringify({{
                                topic_ids: [{topic_id}],
                                operation: {{ type: 'change_notification_level', notification_level_id: 1 }}
                            }})
                        }});
                        
                        // 也尝试 dismiss_posts 操作
                        const resp2 = await fetch('/topics/bulk', {{
                            method: 'PUT',
                            headers: {{
                                'Content-Type': 'application/json',
                                'X-CSRF-Token': csrfToken,
                                'X-Requested-With': 'XMLHttpRequest'
                            }},
                            body: JSON.stringify({{
                                topic_ids: [{topic_id}],
                                operation: {{ type: 'dismiss_posts' }}
                            }})
                        }});
                        
                        return {{ 
                            success: resp.ok || resp2.ok, 
                            status1: resp.status,
                            status2: resp2.status
                        }};
                    }} catch (e) {{
                        return {{ success: false, error: e.message }};
                    }}
                }})();
            """)
            
            if result and result.get("success"):
                logger.debug(f"bulk API 标记已读成功")
                return True
            else:
                logger.debug(f"bulk API 标记已读失败: {result}")
                return False
        except Exception as e:
            logger.debug(f"bulk API 调用异常: {e}")
            return False
    
    async def _sync_cookies_from_browser(self) -> None:
        """从浏览器同步 cookies 到 curl_cffi session"""
        try:
            browser_cookies = await self.context.cookies()
            for cookie in browser_cookies:
                if "linux.do" in cookie.get("domain", ""):
                    self.session.cookies.set(
                        cookie["name"],
                        cookie["value"],
                        domain=cookie.get("domain", ".linux.do")
                    )
            logger.debug(f"同步了 {len(browser_cookies)} 个 cookies")
        except Exception as e:
            logger.warning(f"同步 cookies 失败: {e}")
    
    def _send_timings_via_curl(self, topic_id: int, post_numbers: list, referer_url: str) -> bool:
        """用 curl_cffi 发送 timings 请求（绕过 Playwright 检测）
        
        注意：如果网络无法直接连接 linux.do，此方法会失败。
        在这种情况下，会回退到浏览器内的 fetch。
        
        Args:
            topic_id: 帖子 ID
            post_numbers: 楼层号列表
            referer_url: 当前帖子 URL（用于 Referer 头）
            
        Returns:
            是否成功
        """
        if not post_numbers or not hasattr(self, '_csrf_token') or not self._csrf_token:
            return False
        
        try:
            cfg = self._level_config
            # 每楼阅读时间（毫秒）
            time_per_post = random.randint(cfg["time_per_post_min"], cfg["time_per_post_max"])
            total_time = len(post_numbers) * time_per_post
            
            # 构建表单数据
            data = {
                "topic_id": str(topic_id),
                "topic_time": str(total_time),
            }
            for p in post_numbers:
                data[f"timings[{p}]"] = str(time_per_post + random.randint(-300, 300))
            
            # 发送请求（设置较短的超时）
            resp = self.session.post(
                "https://linux.do/topics/timings",
                data=data,
                headers={
                    "X-CSRF-Token": self._csrf_token,
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": referer_url,
                    "Origin": "https://linux.do",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                },
                impersonate="chrome136",
                timeout=10,  # 10秒超时
            )
            
            if resp.status_code == 200:
                logger.debug(f"timings 发送成功: {len(post_numbers)} 个楼层 (curl_cffi)")
                return True
            else:
                logger.warning(f"timings 发送失败: status={resp.status_code}")
                return False
                
        except Exception as e:
            logger.debug(f"curl_cffi 发送 timings 失败: {e}，将使用浏览器 fetch")
            return False
    
    async def _send_timings_via_browser(self, page: Page, topic_id: int, post_numbers: list) -> bool:
        """用浏览器内的 fetch 发送 timings 请求
        
        当 curl_cffi 无法连接时使用此方法作为回退。
        
        Args:
            page: 浏览器页面
            topic_id: 帖子 ID
            post_numbers: 楼层号列表
            
        Returns:
            是否成功
        """
        if not post_numbers:
            return True
            
        try:
            cfg = self._level_config
            time_per_post = random.randint(cfg["time_per_post_min"], cfg["time_per_post_max"])
            total_time = len(post_numbers) * time_per_post
            
            # 构建 timings 对象
            timings_obj = {str(p): time_per_post + random.randint(-300, 300) for p in post_numbers}
            
            result = await page.evaluate(f"""
                (async () => {{
                    try {{
                        const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content;
                        if (!csrfToken) return {{ success: false, error: 'no csrf token' }};
                        
                        const timings = {timings_obj};
                        const formData = new URLSearchParams();
                        formData.append('topic_id', '{topic_id}');
                        formData.append('topic_time', '{total_time}');
                        
                        for (const [postNum, time] of Object.entries(timings)) {{
                            formData.append('timings[' + postNum + ']', time);
                        }}
                        
                        const resp = await fetch('/topics/timings', {{
                            method: 'POST',
                            headers: {{
                                'Content-Type': 'application/x-www-form-urlencoded',
                                'X-CSRF-Token': csrfToken,
                                'X-Requested-With': 'XMLHttpRequest'
                            }},
                            body: formData.toString()
                        }});
                        
                        return {{ success: resp.ok, status: resp.status }};
                    }} catch (e) {{
                        return {{ success: false, error: e.message }};
                    }}
                }})();
            """)
            
            if result and result.get("success"):
                logger.debug(f"timings 发送成功: {len(post_numbers)} 个楼层 (browser fetch)")
                return True
            else:
                error_info = result.get("error", "unknown") if result else "no result"
                logger.debug(f"browser fetch 发送 timings 失败: {error_info}")
                return False
                
        except Exception as e:
            logger.warning(f"browser fetch 发送 timings 异常: {e}")
            return False
    
    async def _send_timings_for_posts(self, page: Page, topic_id: int, post_numbers: list) -> bool:
        """发送指定楼层的 timings 到 Discourse API（已弃用，改用 _send_timings_via_curl）
        
        Args:
            page: 浏览器页面
            topic_id: 帖子 ID
            post_numbers: 楼层号列表
            
        Returns:
            是否成功
        """
        if not post_numbers:
            return True
            
        try:
            cfg = self._level_config
            # 每楼阅读时间（毫秒）
            time_per_post = random.randint(cfg["time_per_post_min"], cfg["time_per_post_max"])
            total_time = len(post_numbers) * time_per_post
            
            # 构建 timings 参数
            timings_obj = {str(p): time_per_post + random.randint(-300, 300) for p in post_numbers}
            
            result = await page.evaluate(f"""
                (async () => {{
                    try {{
                        const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content;
                        if (!csrfToken) return {{ success: false, error: 'no csrf token' }};
                        
                        const timings = {timings_obj};
                        const formData = new URLSearchParams();
                        formData.append('topic_id', '{topic_id}');
                        formData.append('topic_time', '{total_time}');
                        
                        for (const [postNum, time] of Object.entries(timings)) {{
                            formData.append('timings[' + postNum + ']', time);
                        }}
                        
                        const resp = await fetch('/topics/timings', {{
                            method: 'POST',
                            headers: {{
                                'Content-Type': 'application/x-www-form-urlencoded',
                                'X-CSRF-Token': csrfToken,
                                'X-Requested-With': 'XMLHttpRequest'
                            }},
                            body: formData.toString()
                        }});
                        
                        return {{ success: resp.ok, status: resp.status }};
                    }} catch (e) {{
                        return {{ success: false, error: e.message }};
                    }}
                }})();
            """)
            
            if result and result.get("success"):
                logger.debug(f"timings 发送成功: {len(post_numbers)} 个楼层")
                return True
            else:
                error_info = result.get("error", "unknown") if result else "no result"
                logger.debug(f"timings 发送失败: {error_info}")
                return False
                
        except Exception as e:
            logger.debug(f"发送 timings 异常: {e}")
            return False
    
    async def _random_mouse_move(self, page: Page, element) -> None:
        """随机移动鼠标到元素区域内（模拟真实阅读行为）"""
        try:
            box = await element.bounding_box()
            if not box:
                return
            
            # 在元素区域内随机选择一个点（不是正中心）
            x = box["x"] + random.uniform(box["width"] * 0.1, box["width"] * 0.9)
            y = box["y"] + random.uniform(box["height"] * 0.2, box["height"] * 0.8)
            
            # 移动鼠标（带有轻微的随机偏移模拟手抖）
            jitter_x = random.uniform(-3, 3)
            jitter_y = random.uniform(-3, 3)
            
            await page.mouse.move(x + jitter_x, y + jitter_y)
        except Exception:
            pass  # 鼠标移动失败不影响主流程
    
    async def _browse_post_fallback(self, page: Page) -> None:
        """回退的滚动浏览模式（当无法获取楼层元素时使用）

        使用平滑滚动和随机停留时间，偶尔回滚模拟真实用户

        反检测策略：
        - 使用 smooth 平滑滚动代替瞬间跳转
        - 随机滚动距离和停留时间
        - 偶尔回滚模拟回看行为
        """
        start_time = time.time()
        min_browse_time = random.uniform(8, 15)
        scroll_count = 0

        for _ in range(15):
            # 随机滚动距离（模拟不同的滚动习惯）
            scroll_distance = random.randint(300, 800)

            # 使用平滑滚动（更自然，不易被检测）
            await page.evaluate(f"""
                window.scrollBy({{
                    top: {scroll_distance},
                    behavior: 'smooth'
                }})
            """)
            scroll_count += 1

            # 等待平滑滚动完成
            await asyncio.sleep(random.uniform(0.3, 0.6))

            # 随机停留时间
            if random.random() < 0.2:
                # 20% 概率停留更久（仔细阅读）
                await asyncio.sleep(random.uniform(3.5, 6.0))
            else:
                await asyncio.sleep(random.uniform(2.0, 4.0))

            # 10% 概率回滚一点（模拟回看）
            if scroll_count > 2 and random.random() < 0.1:
                back_distance = random.randint(150, 400)
                await page.evaluate(f"""
                    window.scrollBy({{
                        top: -{back_distance},
                        behavior: 'smooth'
                    }})
                """)
                await asyncio.sleep(random.uniform(1.0, 2.5))

            # 随机鼠标移动
            if random.random() < 0.4:
                viewport = await page.evaluate("({w: window.innerWidth, h: window.innerHeight})")
                x = random.uniform(100, viewport["w"] - 100)
                y = random.uniform(100, viewport["h"] - 100)
                await page.mouse.move(x, y)

            elapsed = time.time() - start_time
            at_bottom = await page.evaluate(
                "window.scrollY + window.innerHeight >= document.body.scrollHeight - 50"
            )

            if at_bottom and elapsed >= min_browse_time:
                break

            # 5% 概率提前退出
            if elapsed > 6 and random.random() < 0.05:
                break
    
    async def _dismiss_dialog(self, page: Page) -> None:
        """关闭页面上可能存在的弹窗"""
        for attempt in range(5):  # 最多尝试 5 次
            try:
                # 检查是否有 dialog 弹窗
                dialog_holder = await page.query_selector("#dialog-holder.dialog-container")
                if not dialog_holder:
                    return
                
                # 检查弹窗是否可见
                is_visible = await dialog_holder.is_visible()
                if not is_visible:
                    return
                
                if attempt == 0:
                    logger.info("检测到弹窗，尝试关闭...")
                
                # 方法1: 按 Escape 键（最可靠）
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.3)
                
                # 检查是否已关闭
                dialog_holder = await page.query_selector("#dialog-holder.dialog-container")
                if not dialog_holder or not await dialog_holder.is_visible():
                    logger.info("弹窗已关闭 (Escape)")
                    return
                
                # 方法2: 点击 overlay 背景关闭
                overlay = await page.query_selector(".dialog-overlay[data-a11y-dialog-hide]")
                if overlay:
                    await overlay.click(force=True, timeout=2000)
                    await asyncio.sleep(0.3)
                    continue
                
                # 方法3: 点击关闭按钮
                close_btn = await page.query_selector(".dialog-close")
                if close_btn:
                    await close_btn.click(force=True, timeout=2000)
                    await asyncio.sleep(0.3)
                    continue
                
                # 方法4: 用 JS 直接移除弹窗
                await page.evaluate("document.querySelector('#dialog-holder')?.remove()")
                logger.info("用 JS 移除了弹窗")
                return
                
            except Exception as e:
                logger.debug(f"关闭弹窗尝试 {attempt+1} 失败: {e}")
                # 最后手段：用 JS 直接移除弹窗
                try:
                    await page.evaluate("document.querySelector('#dialog-holder')?.remove()")
                    logger.info("用 JS 移除了弹窗")
                    return
                except Exception:
                    pass

    async def _click_like(self, page: Page) -> None:
        """点赞帖子"""
        try:
            # 先关闭可能存在的弹窗
            await self._dismiss_dialog(page)
            
            like_button = await page.query_selector(".discourse-reactions-reaction-button")
            if like_button:
                logger.info("找到未点赞的帖子，准备点赞")
                # 使用 force=True 强制点击，跳过可操作性检查
                await like_button.click(timeout=5000, force=True)
                logger.info("点赞成功")
                await asyncio.sleep(random.uniform(1, 2))
            else:
                logger.info("帖子可能已经点过赞了")
        except Exception as e:
            logger.warning(f"点赞失败（不影响签到）: {e}")
    
    # 需要屏蔽的分类
    BLOCKED_CATEGORIES = {"公告", "运营反馈"}
    
    async def _collect_hot_topics(self) -> None:
        """收集热门话题（按浏览量排序的新帖子）"""
        logger.info("收集热门话题...")
        self._hot_topics = []
        
        try:
            # 获取所有话题行
            topic_rows = await self.page.query_selector_all("#list-area tr")
            
            for row in topic_rows:
                try:
                    # 获取标题链接
                    title_link = await row.query_selector(".title")
                    if not title_link:
                        continue
                    
                    title = await title_link.inner_text()
                    title = title.strip()
                    url = await title_link.get_attribute("href")
                    if url and not url.startswith("http"):
                        url = f"https://linux.do{url}"
                    
                    # 获取整行文本，检查是否包含屏蔽分类
                    row_text = await row.inner_text()
                    should_skip = False
                    for blocked in self.BLOCKED_CATEGORIES:
                        if blocked in row_text:
                            should_skip = True
                            break
                    if should_skip:
                        continue
                    
                    # 获取浏览量
                    views_ele = await row.query_selector(".views")
                    views_text = await views_ele.inner_text() if views_ele else "0"
                    views = self._parse_number(views_text.strip())
                    
                    # 获取回复数
                    replies_ele = await row.query_selector(".replies")
                    replies_text = await replies_ele.inner_text() if replies_ele else "0"
                    replies = self._parse_number(replies_text.strip())
                    
                    if title and views > 0:
                        self._hot_topics.append({
                            "title": title[:50] + "..." if len(title) > 50 else title,
                            "url": url,
                            "views": views,
                            "replies": replies,
                        })
                except Exception:
                    continue
            
            # 按浏览量排序，取 Top 10
            self._hot_topics.sort(key=lambda x: x["views"], reverse=True)
            self._hot_topics = self._hot_topics[:10]
            
            logger.info(f"收集到 {len(self._hot_topics)} 个热门话题")
            
        except Exception as e:
            logger.warning(f"收集热门话题失败: {e}")
    
    def _parse_number(self, text: str) -> int:
        """解析数字文本（支持 1.2k, 3.5万 等格式）"""
        text = text.strip().lower()
        if not text:
            return 0
        
        try:
            if "k" in text:
                return int(float(text.replace("k", "")) * 1000)
            if "万" in text:
                return int(float(text.replace("万", "")) * 10000)
            if "m" in text:
                return int(float(text.replace("m", "")) * 1000000)
            return int(text.replace(",", ""))
        except (ValueError, AttributeError):
            return 0
