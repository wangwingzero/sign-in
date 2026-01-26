#!/usr/bin/env python3
"""
AnyRouter/AgentRouter 签到适配器

从 anyrouter-check-in/checkin.py 迁移的签到逻辑，使用 Playwright + httpx。

Requirements:
- 2.4: 保持 Playwright WAF bypass 逻辑
- 2.6: 保持余额查询和变化检测功能
"""

import json
import tempfile
from typing import Optional

import httpx
from loguru import logger
from playwright.async_api import async_playwright

from platforms.base import BasePlatformAdapter, CheckinResult, CheckinStatus
from utils.config import AnyRouterAccount, ProviderConfig


class AnyRouterAdapter(BasePlatformAdapter):
    """AnyRouter/AgentRouter 签到适配器
    
    使用 Playwright 获取 WAF cookies，httpx 进行 API 请求。
    支持多 provider 配置和余额查询。
    """
    
    def __init__(
        self,
        account: AnyRouterAccount,
        provider_config: ProviderConfig,
        account_index: int = 0,
    ):
        """初始化 AnyRouter 适配器
        
        Args:
            account: 账号配置
            provider_config: Provider 配置
            account_index: 账号索引（用于显示）
        """
        self.account = account
        self.provider_config = provider_config
        self.account_index = account_index
        
        self.client: Optional[httpx.Client] = None
        self.waf_cookies: Optional[dict] = None
        self._user_info: Optional[dict] = None
    
    @property
    def platform_name(self) -> str:
        return f"AnyRouter ({self.provider_config.name})"
    
    @property
    def account_name(self) -> str:
        return self.account.get_display_name(self.account_index)
    
    async def login(self) -> bool:
        """获取 WAF cookies（如需要）"""
        # 解析用户 cookies
        user_cookies = self._parse_cookies(self.account.cookies)
        if not user_cookies:
            logger.error(f"[{self.account_name}] 无效的 cookies 配置")
            return False
        
        # 获取 WAF cookies（如需要）
        if self.provider_config.needs_waf_cookies():
            login_url = f"{self.provider_config.domain}{self.provider_config.login_path}"
            self.waf_cookies = await self._get_waf_cookies(login_url)
            if not self.waf_cookies:
                logger.error(f"[{self.account_name}] 无法获取 WAF cookies")
                return False
        else:
            logger.info(f"[{self.account_name}] 无需 WAF bypass，直接使用用户 cookies")
            self.waf_cookies = {}
        
        # 合并 cookies
        all_cookies = {**self.waf_cookies, **user_cookies}
        
        # 初始化 HTTP 客户端
        self.client = httpx.Client(http2=True, timeout=30.0)
        self.client.cookies.update(all_cookies)
        
        return True
    
    async def checkin(self) -> CheckinResult:
        """执行签到操作"""
        headers = self._build_headers()
        
        # 获取用户信息
        self._user_info = self._get_user_info(headers)
        
        details = {}
        if self._user_info and self._user_info.get("success"):
            details["balance"] = f"${self._user_info['quota']}"
            details["used"] = f"${self._user_info['used_quota']}"
            logger.info(f"[{self.account_name}] {self._user_info['display']}")
        elif self._user_info:
            logger.warning(f"[{self.account_name}] {self._user_info.get('error', 'Unknown error')}")
        
        # 执行签到
        if self.provider_config.needs_manual_check_in():
            success = self._execute_check_in(headers)
            if success:
                return CheckinResult(
                    platform=self.platform_name,
                    account=self.account_name,
                    status=CheckinStatus.SUCCESS,
                    message="签到成功",
                    details=details if details else None,
                )
            else:
                return CheckinResult(
                    platform=self.platform_name,
                    account=self.account_name,
                    status=CheckinStatus.FAILED,
                    message="签到失败",
                    details=details if details else None,
                )
        else:
            # 自动签到（通过获取用户信息触发）
            logger.info(f"[{self.account_name}] 签到已自动完成（通过用户信息请求触发）")
            return CheckinResult(
                platform=self.platform_name,
                account=self.account_name,
                status=CheckinStatus.SUCCESS,
                message="签到成功（自动触发）",
                details=details if details else None,
            )
    
    async def get_status(self) -> dict:
        """获取余额信息"""
        if self._user_info:
            return self._user_info
        
        if not self.client:
            return {"success": False, "error": "Client not initialized"}
        
        headers = self._build_headers()
        self._user_info = self._get_user_info(headers)
        return self._user_info or {"success": False, "error": "Failed to get user info"}
    
    async def cleanup(self) -> None:
        """清理 HTTP 客户端"""
        if self.client:
            self.client.close()
            self.client = None
    
    def _parse_cookies(self, cookies_data) -> dict:
        """解析 cookies 数据"""
        if isinstance(cookies_data, dict):
            return cookies_data
        
        if isinstance(cookies_data, str):
            cookies_dict = {}
            for cookie in cookies_data.split(";"):
                if "=" in cookie:
                    key, value = cookie.strip().split("=", 1)
                    cookies_dict[key] = value
            return cookies_dict
        
        return {}
    
    async def _get_waf_cookies(self, login_url: str) -> Optional[dict]:
        """使用 Playwright 获取 WAF cookies"""
        logger.info(f"[{self.account_name}] 启动浏览器获取 WAF cookies...")
        
        required_cookies = self.provider_config.waf_cookie_names or []
        
        async with async_playwright() as p:
            with tempfile.TemporaryDirectory() as temp_dir:
                context = await p.chromium.launch_persistent_context(
                    user_data_dir=temp_dir,
                    headless=True,
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1920, "height": 1080},
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                        "--disable-web-security",
                        "--disable-features=VizDisplayCompositor",
                        "--no-sandbox",
                    ],
                )
                
                page = await context.new_page()
                
                try:
                    logger.info(f"[{self.account_name}] 访问登录页面获取初始 cookies...")
                    
                    await page.goto(login_url, wait_until="networkidle")
                    
                    try:
                        await page.wait_for_function(
                            'document.readyState === "complete"',
                            timeout=5000,
                        )
                    except Exception:
                        await page.wait_for_timeout(3000)
                    
                    cookies = await page.context.cookies()
                    
                    waf_cookies = {}
                    for cookie in cookies:
                        cookie_name = cookie.get("name")
                        cookie_value = cookie.get("value")
                        if cookie_name in required_cookies and cookie_value is not None:
                            waf_cookies[cookie_name] = cookie_value
                    
                    logger.info(f"[{self.account_name}] 获取到 {len(waf_cookies)} 个 WAF cookies")
                    
                    missing_cookies = [c for c in required_cookies if c not in waf_cookies]
                    
                    if missing_cookies:
                        logger.error(f"[{self.account_name}] 缺少 WAF cookies: {missing_cookies}")
                        await context.close()
                        return None
                    
                    logger.success(f"[{self.account_name}] 成功获取所有 WAF cookies")
                    await context.close()
                    return waf_cookies
                    
                except Exception as e:
                    logger.error(f"[{self.account_name}] 获取 WAF cookies 时出错: {e}")
                    await context.close()
                    return None
    
    def _build_headers(self) -> dict:
        """构建请求头"""
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Referer": self.provider_config.domain,
            "Origin": self.provider_config.domain,
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            self.provider_config.api_user_key: self.account.api_user,
        }
    
    def _get_user_info(self, headers: dict) -> dict:
        """获取用户信息"""
        user_info_url = f"{self.provider_config.domain}{self.provider_config.user_info_path}"
        
        try:
            response = self.client.get(user_info_url, headers=headers, timeout=30)
            
            if response.status_code == 200:
                data = response.json()
                if data.get("success"):
                    user_data = data.get("data", {})
                    quota = round(user_data.get("quota", 0) / 500000, 2)
                    used_quota = round(user_data.get("used_quota", 0) / 500000, 2)
                    return {
                        "success": True,
                        "quota": quota,
                        "used_quota": used_quota,
                        "display": f"💰 当前余额: ${quota}, 已使用: ${used_quota}",
                    }
            return {
                "success": False,
                "error": f"获取用户信息失败: HTTP {response.status_code}",
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"获取用户信息失败: {str(e)[:50]}...",
            }
    
    def _execute_check_in(self, headers: dict) -> bool:
        """执行签到请求"""
        logger.info(f"[{self.account_name}] 执行签到请求")
        
        checkin_headers = headers.copy()
        checkin_headers.update({
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        })
        
        sign_in_url = f"{self.provider_config.domain}{self.provider_config.sign_in_path}"
        
        try:
            response = self.client.post(sign_in_url, headers=checkin_headers, timeout=30)
            
            logger.info(f"[{self.account_name}] 响应状态码: {response.status_code}")
            
            if response.status_code == 200:
                try:
                    result = response.json()
                    if result.get("ret") == 1 or result.get("code") == 0 or result.get("success"):
                        logger.success(f"[{self.account_name}] 签到成功!")
                        return True
                    else:
                        error_msg = result.get("msg", result.get("message", "Unknown error"))
                        logger.error(f"[{self.account_name}] 签到失败 - {error_msg}")
                        return False
                except json.JSONDecodeError:
                    if "success" in response.text.lower():
                        logger.success(f"[{self.account_name}] 签到成功!")
                        return True
                    else:
                        logger.error(f"[{self.account_name}] 签到失败 - 无效的响应格式")
                        return False
            else:
                logger.error(f"[{self.account_name}] 签到失败 - HTTP {response.status_code}")
                return False
        except Exception as e:
            logger.error(f"[{self.account_name}] 签到请求异常: {e}")
            return False
