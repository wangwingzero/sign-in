#!/usr/bin/env python3
"""
LinuxDo 论坛签到适配器

使用 Patchright (反检测 Playwright) + curl_cffi 实现自动签到。

Requirements:
- 2.3: 使用 Patchright 替代 DrissionPage 提升反检测能力
- 2.5: 保持浏览帖子、随机点赞功能
"""

import asyncio
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
        """初始化 Patchright 浏览器"""
        self._playwright = await async_playwright().start()
        
        # Patchright 自带反检测，使用 chromium
        # headless=True 用于 GitHub Actions 等无显示器环境
        self.browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ]
        )
        
        # 随机化 User-Agent 和 viewport（每个账户不同）
        self._user_agent = self._get_random_user_agent()
        viewport = self._get_random_viewport()
        
        logger.debug(f"使用 UA: {self._user_agent}")
        logger.debug(f"使用 viewport: {viewport['width']}x{viewport['height']}")
        
        # 创建上下文，设置随机化的 viewport 和 user agent
        self.context = await self.browser.new_context(
            viewport=viewport,
            user_agent=self._user_agent,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        
        self.page = await self.context.new_page()
    
    def _init_session(self) -> None:
        """初始化 HTTP 会话（使用与浏览器相同的 User-Agent）"""
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self._user_agent or self._get_random_user_agent(),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
    
    async def _wait_for_cloudflare(self, timeout: int = 30) -> bool:
        """等待 Cloudflare 挑战完成
        
        通过检测 cf_clearance Cookie 或页面特定元素来判断验证是否通过
        
        Args:
            timeout: 最大等待时间（秒）
            
        Returns:
            是否通过验证
        """
        for i in range(timeout):
            # 检查是否有 cf_clearance Cookie
            cookies = await self.context.cookies()
            has_clearance = any(c["name"] == "cf_clearance" for c in cookies)
            
            # 检查页面是否有登录表单（说明已通过 Cloudflare）
            login_form = await self.page.query_selector("#login-form, .login-modal, #login-account-name")
            
            if has_clearance or login_form:
                logger.info(f"Cloudflare 验证通过 (耗时 {i+1} 秒)")
                return True
            
            # 检查是否还在 Cloudflare 挑战页面
            challenge_frame = await self.page.query_selector("iframe[src*='challenges.cloudflare.com']")
            if challenge_frame:
                logger.debug(f"检测到 Cloudflare 挑战，等待中... ({i+1}/{timeout})")
            
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
        
        # Step 1: 用浏览器访问登录页，通过 Cloudflare 检测
        logger.info("访问登录页面...")
        await self.page.goto(LOGIN_URL, wait_until="domcontentloaded")
        
        # Step 2: 等待 Cloudflare 挑战完成
        logger.info("等待 Cloudflare 验证...")
        cf_passed = await self._wait_for_cloudflare()
        if not cf_passed:
            logger.error("Cloudflare 验证超时")
            return False
        
        # Step 3: 模拟人类行为
        await self._simulate_human_behavior()
        
        # Step 4: 等待登录表单完全加载（Discourse SPA 需要时间）
        logger.info("等待登录表单...")
        try:
            await self.page.wait_for_selector("#login-account-name", timeout=15000)
            # 额外等待确保 Ember.js 完全渲染和事件绑定
            await asyncio.sleep(3)
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
        
        # 检查是否登录成功
        user_ele = await self.page.query_selector("#current-user")
        if user_ele:
            logger.info("登录成功!")
        else:
            # 尝试访问首页确认登录状态
            await self.page.goto(HOME_URL, wait_until="domcontentloaded")
            await asyncio.sleep(3)
            user_ele = await self.page.query_selector("#current-user")
            if not user_ele:
                content = await self.page.content()
                if "avatar" not in content and "current-user" not in content:
                    logger.debug(f"页面内容片段: {content[:500]}")
                    logger.error("登录验证失败")
                    return False
            logger.info("登录成功!")
        
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
        
        # 添加 Connect 信息到详情
        if self._connect_info:
            details["connect_info"] = self._connect_info
        
        # 收集热门话题
        await self._collect_hot_topics()
        if self._hot_topics:
            details["hot_topics"] = self._hot_topics
        
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
        """点击并浏览主题帖，优先选择楼层数多的帖子以高效增加已读数
        
        策略：
        1. 从未读/新帖子页面收集帖子
        2. 用 API 获取每个帖子的楼层数
        3. 按楼层数排序，优先处理长帖子
        4. 直接发送 timings API 标记所有楼层为已读
        """
        # 收集帖子 URL
        topic_urls = []
        
        # 1. 先尝试获取未读帖子
        unread_urls = await self._get_topics_from_page("https://linux.do/unread")
        if unread_urls:
            logger.info(f"发现 {len(unread_urls)} 个未读帖子")
            topic_urls.extend(unread_urls)
        
        # 2. 再获取新帖子
        new_urls = await self._get_topics_from_page("https://linux.do/new")
        if new_urls:
            logger.info(f"发现 {len(new_urls)} 个新帖子")
            for url in new_urls:
                if url not in topic_urls:
                    topic_urls.append(url)
        
        # 3. 从热门帖子补充（通常楼层数更多）
        if len(topic_urls) < 20:
            logger.info("从热门帖子补充")
            top_urls = await self._get_topics_from_page("https://linux.do/top")
            for url in top_urls:
                if url not in topic_urls:
                    topic_urls.append(url)
        
        # 4. 从首页补充
        if len(topic_urls) < 20:
            logger.info("从首页补充")
            home_urls = await self._get_topics_from_page("https://linux.do/")
            for url in home_urls:
                if url not in topic_urls:
                    topic_urls.append(url)
        
        if not topic_urls:
            logger.error("没有可浏览的帖子")
            return 0
        
        logger.info(f"共收集到 {len(topic_urls)} 个帖子，获取楼层信息...")
        
        # 5. 获取每个帖子的楼层数，按楼层数排序
        topics_with_info = []
        for url in topic_urls[:50]:  # 最多检查 50 个
            topic_id = self._extract_topic_id(url)
            if topic_id:
                info = self._get_topic_info(topic_id)
                if info:
                    topics_with_info.append({
                        "url": url,
                        "topic_id": topic_id,
                        "posts": info["highest_post_number"],
                        "title": info.get("title", "")[:20],
                    })
        
        # 按楼层数降序排序（优先处理长帖子）
        topics_with_info.sort(key=lambda x: x["posts"], reverse=True)
        
        if topics_with_info:
            top_5 = topics_with_info[:5]
            logger.info(f"楼层数最多的帖子: {[(t['title'], t['posts']) for t in top_5]}")
        
        # 6. 按时间浏览（循环浏览直到达到目标时长）
        start_time = time.time()
        browsed_count = 0
        total_posts_read = 0
        topic_index = 0
        
        while True:
            elapsed = time.time() - start_time
            if elapsed >= self.browse_duration:
                logger.info(f"已达到目标浏览时间 {self.browse_duration} 秒")
                break
            
            # 如果帖子列表遍历完了，从头开始（循环浏览）
            if topic_index >= len(topics_with_info):
                if browsed_count == 0:
                    logger.warning("没有成功浏览任何帖子")
                    break
                logger.info("帖子列表已遍历完，从头开始循环浏览...")
                topic_index = 0
                # 重新打乱顺序，避免重复模式
                random.shuffle(topics_with_info)
            
            topic = topics_with_info[topic_index]
            remaining = self.browse_duration - elapsed
            logger.info(f"浏览第 {browsed_count + 1} 个帖子 ({topic['posts']}楼)，已用时 {elapsed:.0f}s，剩余 {remaining:.0f}s")
            
            success = await self._click_one_topic(topic["url"])
            if success:
                total_posts_read += topic["posts"]
            browsed_count += 1
            topic_index += 1
            
            # 帖子之间短暂间隔
            await asyncio.sleep(random.uniform(0.5, 1.5))
        
        total_time = time.time() - start_time
        logger.info(f"浏览完成，共浏览 {browsed_count} 个帖子，标记 {total_posts_read} 个楼层为已读，用时 {total_time:.0f} 秒")
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
        """从 URL 提取帖子 ID"""
        try:
            # URL 格式: https://linux.do/t/topic/123456 或 https://linux.do/t/topic/123456/1
            import re
            match = re.search(r'/t/topic/(\d+)', url)
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
        """浏览帖子内容，逐个楼层停留确保标记为已读
        
        Discourse 论坛的阅读标记机制：
        - 每个楼层需要在视口内停留约 1 秒才会触发 timings
        - 右侧蓝点消失表示该楼层已被标记为已读
        - 我们停留 2 秒以上确保稳定触发
        
        反检测策略：
        - 随机停留时间（2-4秒）
        - 偶尔回滚模拟回看
        - 随机鼠标移动
        - 不规则的浏览节奏
        """
        # 等待帖子内容加载（随机时间）
        await asyncio.sleep(random.uniform(0.8, 1.5))
        
        # 获取所有楼层
        posts = await page.query_selector_all(".topic-post")
        
        if not posts:
            logger.warning("未找到楼层元素，使用滚动模式")
            await self._browse_post_fallback(page)
            return
        
        post_count = len(posts)
        logger.info(f"帖子共有 {post_count} 个楼层，开始逐个浏览")
        
        # 随机决定浏览多少楼层（避免每次都浏览固定数量）
        max_posts_to_read = min(post_count, random.randint(15, 25))
        browsed_count = 0
        last_scroll_back_index = -5  # 记录上次回滚的位置，避免频繁回滚
        
        for i, post in enumerate(posts[:max_posts_to_read]):
            try:
                # 滚动到该楼层
                await post.scroll_into_view_if_needed()
                
                # 随机鼠标移动到帖子区域（模拟真实阅读）
                if random.random() < 0.6:
                    await self._random_mouse_move(page, post)
                
                # 停留时间：基础 2-4 秒，偶尔更长（模拟仔细阅读）
                if random.random() < 0.15:
                    # 15% 概率仔细阅读，停留更久
                    wait_time = random.uniform(4.0, 7.0)
                    logger.debug(f"楼层 {i+1}，仔细阅读 {wait_time:.1f} 秒")
                else:
                    wait_time = random.uniform(2.0, 4.0)
                    logger.debug(f"楼层 {i+1}/{max_posts_to_read}，停留 {wait_time:.1f} 秒")
                
                await asyncio.sleep(wait_time)
                browsed_count += 1
                
                # 8% 概率回滚查看之前的内容（模拟回看）
                if i > 3 and i - last_scroll_back_index > 3 and random.random() < 0.08:
                    scroll_back = random.randint(1, min(3, i))
                    logger.debug(f"回滚查看第 {i+1-scroll_back} 楼")
                    await posts[i - scroll_back].scroll_into_view_if_needed()
                    await asyncio.sleep(random.uniform(1.5, 3.0))
                    last_scroll_back_index = i
                    # 滚回当前位置
                    await post.scroll_into_view_if_needed()
                    await asyncio.sleep(random.uniform(0.5, 1.0))
                
                # 6% 概率提前退出（模拟失去兴趣）
                if i > 5 and random.random() < 0.06:
                    logger.info(f"随机退出，已浏览 {browsed_count} 个楼层")
                    break
                
                # 偶尔短暂停顿（模拟思考或分心）
                if random.random() < 0.1:
                    await asyncio.sleep(random.uniform(0.3, 0.8))
                    
            except Exception as e:
                logger.debug(f"浏览楼层 {i+1} 失败: {e}")
                continue
        
        logger.info(f"完成楼层浏览，共浏览 {browsed_count} 个楼层")
    
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
        for _ in range(3):  # 最多尝试 3 次
            try:
                # 检查是否有 dialog 弹窗
                dialog_holder = await page.query_selector("#dialog-holder.dialog-container")
                if not dialog_holder:
                    return
                
                # 检查弹窗是否可见
                is_visible = await dialog_holder.is_visible()
                if not is_visible:
                    return
                
                logger.info("检测到弹窗，尝试关闭...")
                
                # 方法1: 点击 overlay 背景关闭
                overlay = await page.query_selector(".dialog-overlay[data-a11y-dialog-hide]")
                if overlay:
                    await overlay.click(force=True, timeout=2000)
                    await asyncio.sleep(0.5)
                    continue
                
                # 方法2: 点击关闭按钮
                close_btn = await page.query_selector(".dialog-close")
                if close_btn:
                    await close_btn.click(force=True, timeout=2000)
                    await asyncio.sleep(0.5)
                    continue
                
                # 方法3: 按 Escape 键
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.5)
                
            except Exception as e:
                logger.debug(f"关闭弹窗尝试失败: {e}")
                # 最后手段：用 JS 直接移除弹窗
                try:
                    await page.evaluate("document.querySelector('#dialog-holder')?.remove()")
                    logger.info("用 JS 移除了弹窗")
                except Exception:
                    pass
                break

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
