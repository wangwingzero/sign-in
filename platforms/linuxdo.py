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
    """
    
    def __init__(
        self,
        username: str,
        password: str,
        browse_enabled: bool = True,
        account_name: Optional[str] = None,
    ):
        """初始化 LinuxDo 适配器
        
        Args:
            username: LinuxDo 用户名
            password: LinuxDo 密码
            browse_enabled: 是否启用浏览帖子功能
            account_name: 账号显示名称（可选）
        """
        self.username = username
        self.password = password
        self.browse_enabled = browse_enabled
        self._account_name = account_name
        
        self._playwright = None
        self.browser: Optional[Browser] = None
        self.context = None
        self.page: Optional[Page] = None
        self.session: Optional[requests.Session] = None
        self._connect_info: Optional[dict] = None
        self._hot_topics: list[dict] = []
    
    @property
    def platform_name(self) -> str:
        return "LinuxDo"
    
    @property
    def account_name(self) -> str:
        return self._account_name if self._account_name else self.username
    
    async def _init_browser(self) -> None:
        """初始化 Patchright 浏览器"""
        self._playwright = await async_playwright().start()
        
        # Patchright 自带反检测，使用 chromium
        self.browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ]
        )
        
        # 创建上下文，设置 viewport 和 user agent
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        
        self.page = await self.context.new_page()
    
    def _init_session(self) -> None:
        """初始化 HTTP 会话"""
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36 Edg/142.0.0.0"
            ),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
    
    async def login(self) -> bool:
        """执行登录操作"""
        logger.info("开始登录 LinuxDo")
        
        await self._init_browser()
        self._init_session()
        
        # Step 1: 获取 CSRF Token
        logger.info("获取 CSRF token...")
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36 Edg/142.0.0.0"
            ),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": LOGIN_URL,
        }
        
        try:
            resp_csrf = self.session.get(CSRF_URL, headers=headers, impersonate="chrome136")
            csrf_data = resp_csrf.json()
            csrf_token = csrf_data.get("csrf")
            logger.info(f"CSRF Token 获取成功: {csrf_token[:10]}...")
        except Exception as e:
            logger.error(f"获取 CSRF Token 失败: {e}")
            return False
        
        # Step 2: 登录
        logger.info("正在登录...")
        headers.update({
            "X-CSRF-Token": csrf_token,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://linux.do",
        })
        
        data = {
            "login": self.username,
            "password": self.password,
            "second_factor_method": "1",
            "timezone": "Asia/Shanghai",
        }
        
        try:
            resp_login = self.session.post(
                SESSION_URL, data=data, impersonate="chrome136", headers=headers
            )
            
            if resp_login.status_code == 200:
                response_json = resp_login.json()
                if response_json.get("error"):
                    logger.error(f"登录失败: {response_json.get('error')}")
                    return False
                logger.info("登录成功!")
            else:
                logger.error(f"登录失败，状态码: {resp_login.status_code}")
                return False
        except Exception as e:
            logger.error(f"登录请求异常: {e}")
            return False
        
        # 获取 Connect 信息
        self._fetch_connect_info()
        
        # Step 3: 同步 Cookie 到 Patchright
        logger.info("同步 Cookie 到 Patchright...")
        cookies_dict = self.session.cookies.get_dict()
        
        cookies_list = [
            {
                "name": name,
                "value": value,
                "domain": ".linux.do",
                "path": "/",
            }
            for name, value in cookies_dict.items()
        ]
        
        await self.context.add_cookies(cookies_list)
        
        logger.info("Cookie 设置完成，导航至 linux.do...")
        await self.page.goto(HOME_URL, wait_until="domcontentloaded")
        
        # 等待页面加载
        await asyncio.sleep(5)
        
        # 验证登录状态
        try:
            user_ele = await self.page.query_selector("#current-user")
            if not user_ele:
                content = await self.page.content()
                if "avatar" in content:
                    logger.info("登录验证成功 (通过 avatar)")
                    return True
                logger.error("登录验证失败 (未找到 current-user)")
                return False
            logger.info("登录验证成功")
            return True
        except Exception as e:
            logger.warning(f"登录验证异常: {e}")
            return True  # 继续执行
    
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
        """点击并浏览主题帖，返回浏览数量"""
        try:
            # 获取所有话题行
            topic_rows = await self.page.query_selector_all("#list-area tr")
        except Exception:
            topic_rows = []
        
        if not topic_rows:
            logger.error("未找到主题帖")
            return 0
        
        # 获取所有 href，过滤掉公告和运营反馈
        topic_urls = []
        for row in topic_rows:
            try:
                # 检查分类
                category_ele = await row.query_selector(".category-name")
                if category_ele:
                    category = await category_ele.inner_text()
                    if category.strip() in self.BLOCKED_CATEGORIES:
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
        
        if not topic_urls:
            logger.error("过滤后没有可浏览的帖子")
            return 0
        
        logger.info(f"发现 {len(topic_urls)} 个可浏览帖子，随机选择浏览")
        
        # 随机选择 5-15 个帖子
        browse_count = random.randint(5, 15)
        actual_count = min(browse_count, len(topic_urls))
        selected_urls = random.sample(topic_urls, actual_count)
        
        for url in selected_urls:
            await self._click_one_topic(url)
        
        logger.info(f"浏览了 {actual_count} 个帖子")
        return actual_count
    
    async def _click_one_topic(self, topic_url: str) -> bool:
        """点击单个主题帖"""
        new_page = await self.context.new_page()
        try:
            await new_page.goto(topic_url, wait_until="domcontentloaded")
            if random.random() < 0.3:
                await self._click_like(new_page)
            await self._browse_post(new_page)
            return True
        except Exception as e:
            logger.warning(f"浏览帖子失败: {e}")
            return False
        finally:
            try:
                await new_page.close()
            except Exception:
                pass
    
    async def _browse_post(self, page: Page) -> None:
        """浏览帖子内容"""
        prev_url = None
        
        for _ in range(10):
            scroll_distance = random.randint(550, 650)
            logger.info(f"向下滚动 {scroll_distance} 像素...")
            await page.evaluate(f"window.scrollBy(0, {scroll_distance})")
            logger.info(f"已加载页面: {page.url}")
            
            if random.random() < 0.03:
                logger.success("随机退出浏览")
                break
            
            at_bottom = await page.evaluate(
                "window.scrollY + window.innerHeight >= document.body.scrollHeight"
            )
            current_url = page.url
            
            if current_url != prev_url:
                prev_url = current_url
            elif at_bottom and prev_url == current_url:
                logger.success("已到达页面底部，退出浏览")
                break
            
            wait_time = random.uniform(2, 4)
            logger.info(f"等待 {wait_time:.2f} 秒...")
            await asyncio.sleep(wait_time)
    
    async def _click_like(self, page: Page) -> None:
        """点赞帖子"""
        try:
            like_button = await page.query_selector(".discourse-reactions-reaction-button")
            if like_button:
                logger.info("找到未点赞的帖子，准备点赞")
                await like_button.click()
                logger.info("点赞成功")
                await asyncio.sleep(random.uniform(1, 2))
            else:
                logger.info("帖子可能已经点过赞了")
        except Exception as e:
            logger.error(f"点赞失败: {e}")
    
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
                    
                    # 获取分类
                    category_ele = await row.query_selector(".category-name")
                    category = await category_ele.inner_text() if category_ele else ""
                    category = category.strip()
                    
                    # 跳过屏蔽的分类
                    if category in self.BLOCKED_CATEGORIES:
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
                            "category": category,
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
