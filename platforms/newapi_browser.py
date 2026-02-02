#!/usr/bin/env python3
"""
NewAPI 站点浏览器自动签到模块

支持两种登录方式（按优先级）：
1. Cookie 方式（优先）- 使用配置文件中的 session 和 api_user
2. LinuxDO OAuth 方式（回退）- Cookie 失效时自动使用浏览器登录

适用于 session 过期时间短的站点（如 hotaru, lightllm）。

参考 linuxdo.py 的成功实现：
- 使用 JS 直接赋值填写表单（而不是 send_keys）
- 先访问首页通过 Cloudflare，再访问登录页
- 使用 BrowserManager 管理浏览器

Debug 模式：
- 设置环境变量 DEBUG=true 或 NEWAPI_DEBUG=true 开启
- 开启后会保存截图、打印详细日志
"""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

import httpx
from loguru import logger

from platforms.base import CheckinResult, CheckinStatus
from utils.browser import BrowserManager, get_browser_engine
from utils.config import DEFAULT_PROVIDERS, ProviderConfig


def is_debug_mode() -> bool:
    """检查是否开启 debug 模式"""
    return (
        os.environ.get("DEBUG", "").lower() in ("true", "1", "yes") or
        os.environ.get("NEWAPI_DEBUG", "").lower() in ("true", "1", "yes")
    )


class NewAPIBrowserCheckin:
    """NewAPI 站点浏览器自动签到（支持 Cookie 和 OAuth 两种方式）"""

    LINUXDO_URL = "https://linux.do"
    LINUXDO_LOGIN_URL = "https://linux.do/login"

    def __init__(
        self,
        provider_name: str,
        linuxdo_username: str | None = None,
        linuxdo_password: str | None = None,
        cookies: dict | str | None = None,
        api_user: str | None = None,
        account_name: str | None = None,
    ):
        """初始化"""
        self.provider_name = provider_name
        self.linuxdo_username = linuxdo_username
        self.linuxdo_password = linuxdo_password
        self._preset_cookies = self._parse_cookies(cookies)
        self._preset_api_user = api_user
        self._account_name = account_name or f"{provider_name}_{linuxdo_username or 'unknown'}"

        # 获取 provider 配置
        if provider_name in DEFAULT_PROVIDERS:
            self.provider = ProviderConfig.from_dict(provider_name, DEFAULT_PROVIDERS[provider_name])
        else:
            raise ValueError(f"未知的 provider: {provider_name}")

        # 运行时状态
        self._browser_manager: BrowserManager | None = None
        self._session_cookie: str | None = None
        self._api_user: str | None = None
        self._login_method: str = "unknown"

        # Debug 模式
        self._debug = is_debug_mode()
        self._debug_dir: Path | None = None
        if self._debug:
            self._debug_dir = Path("debug_screenshots")
            self._debug_dir.mkdir(exist_ok=True)
            logger.info(f"[{self._account_name}] Debug 模式已开启，截图保存到: {self._debug_dir}")

    def _parse_cookies(self, cookies: dict | str | None) -> dict:
        """解析 Cookie 为字典格式"""
        if not cookies:
            return {}
        if isinstance(cookies, dict):
            return cookies
        result = {}
        if isinstance(cookies, str):
            for item in cookies.split(";"):
                item = item.strip()
                if "=" in item:
                    key, value = item.split("=", 1)
                    result[key.strip()] = value.strip()
        return result

    @property
    def account_name(self) -> str:
        return self._account_name

    async def _save_debug_screenshot(self, tab, name: str) -> None:
        """保存调试截图（仅在 debug 模式下）"""
        if not self._debug or not self._debug_dir:
            return
        try:
            timestamp = datetime.now().strftime("%H%M%S")
            filename = f"{self._account_name}_{timestamp}_{name}.png"
            filepath = self._debug_dir / filename
            await tab.save_screenshot(str(filepath))
            logger.debug(f"[{self._account_name}] 截图已保存: {filepath}")
        except Exception as e:
            logger.debug(f"[{self._account_name}] 截图保存失败: {e}")

    async def _log_page_info(self, tab, context: str) -> None:
        """记录页面信息（仅在 debug 模式下）"""
        if not self._debug:
            return
        try:
            url = tab.target.url if hasattr(tab, 'target') else "unknown"
            title = await tab.evaluate("document.title") or "unknown"
            logger.debug(f"[{self._account_name}] [{context}] URL: {url}, Title: {title}")
        except Exception as e:
            logger.debug(f"[{self._account_name}] [{context}] 获取页面信息失败: {e}")

    async def _checkin_with_cookies(self, session_cookie: str, api_user: str) -> tuple[bool, str, dict]:
        """使用 Cookie 方式签到（HTTP 请求）"""
        details = {}
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Content-Type": "application/json",
            self.provider.api_user_key: api_user,
        }
        cookies = {"session": session_cookie}

        try:
            async with httpx.AsyncClient(timeout=30.0, cookies=cookies) as client:
                # 获取用户信息
                user_info_url = f"{self.provider.domain}{self.provider.user_info_path}"
                logger.info(f"[{self.account_name}] 获取用户信息: {user_info_url}")

                response = await client.get(user_info_url, headers=headers)

                if response.status_code == 200:
                    data = response.json()
                    if data.get("success"):
                        user_data = data.get("data", {})
                        quota = round(user_data.get("quota", 0) / 500000, 2)
                        used_quota = round(user_data.get("used_quota", 0) / 500000, 2)
                        details["balance"] = f"${quota}"
                        details["used"] = f"${used_quota}"
                        logger.info(f"[{self.account_name}] 余额: ${quota}, 已用: ${used_quota}")
                    else:
                        return False, f"Cookie 无效: {data.get('message')}", details
                elif response.status_code == 401:
                    return False, "Cookie 已过期", details
                else:
                    return False, f"HTTP {response.status_code}", details

                # 执行签到
                if self.provider.needs_manual_check_in():
                    checkin_url = f"{self.provider.domain}{self.provider.sign_in_path}"
                    logger.info(f"[{self.account_name}] 执行签到: {checkin_url}")

                    response = await client.post(checkin_url, headers=headers)

                    if response.status_code == 200:
                        data = response.json()
                        msg = data.get("message") or data.get("msg") or ""
                        if data.get("success") or "已签到" in msg or "签到成功" in msg:
                            logger.success(f"[{self.account_name}] {msg or '签到成功'}")
                            return True, msg or "签到成功", details
                        elif "已签到" in msg:
                            return True, msg, details
                        return False, msg or "签到失败", details
                    elif response.status_code == 401:
                        return False, "Cookie 已过期", details
                    return False, f"HTTP {response.status_code}", details
                return True, "签到成功（自动触发）", details

        except httpx.TimeoutException:
            return False, "请求超时", details
        except Exception as e:
            logger.error(f"[{self.account_name}] 签到请求失败: {e}")
            return False, f"请求失败: {e}", details

    async def _wait_for_cloudflare(self, tab, timeout: int = 30) -> bool:
        """等待 Cloudflare 挑战完成（参考 linuxdo.py）"""
        logger.info(f"[{self.account_name}] 检测 Cloudflare 挑战...")
        start_time = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start_time < timeout:
            try:
                title = await tab.evaluate("document.title")
                cf_indicators = ["just a moment", "checking your browser", "please wait", "verifying"]
                title_lower = title.lower() if title else ""
                is_cf_page = any(ind in title_lower for ind in cf_indicators)

                if not is_cf_page and title:
                    logger.success(f"[{self.account_name}] Cloudflare 验证通过！")
                    await self._save_debug_screenshot(tab, "cf_passed")
                    return True
                if is_cf_page:
                    logger.debug(f"[{self.account_name}] 等待 Cloudflare... 标题: {title}")
                    if self._debug:
                        await self._save_debug_screenshot(tab, "cf_waiting")
            except Exception as e:
                logger.debug(f"[{self.account_name}] 检查页面状态出错: {e}")
            await asyncio.sleep(2)

        logger.warning(f"[{self.account_name}] Cloudflare 验证超时")
        await self._save_debug_screenshot(tab, "cf_timeout")
        return False

    async def _login_linuxdo(self, tab) -> bool:
        """登录 LinuxDO（参考 linuxdo.py 的成功实现，使用 JS 直接赋值）"""
        # 1. 先访问首页，让 Cloudflare 验证
        logger.info(f"[{self.account_name}] 访问 LinuxDO 首页...")
        await tab.get(self.LINUXDO_URL)
        await self._log_page_info(tab, "linuxdo_home")

        # 2. 等待 Cloudflare 挑战完成
        cf_passed = await self._wait_for_cloudflare(tab, timeout=30)
        if not cf_passed:
            logger.info(f"[{self.account_name}] 尝试刷新页面...")
            await tab.reload()
            cf_passed = await self._wait_for_cloudflare(tab, timeout=20)
            if not cf_passed:
                logger.error(f"[{self.account_name}] Cloudflare 验证失败")
                await self._save_debug_screenshot(tab, "linuxdo_cf_failed")
                return False

        # 检查是否已经登录
        try:
            is_logged_in = await tab.evaluate("""
                (function() {
                    const userMenu = document.querySelector('.current-user');
                    return !!userMenu;
                })()
            """)
            if is_logged_in:
                logger.success(f"[{self.account_name}] LinuxDO 已登录")
                await self._save_debug_screenshot(tab, "linuxdo_already_logged")
                return True
        except Exception:
            pass

        # 3. 访问登录页面
        logger.info(f"[{self.account_name}] 访问登录页面...")
        await tab.get(self.LINUXDO_LOGIN_URL)
        await asyncio.sleep(5)
        await self._log_page_info(tab, "linuxdo_login_page")
        await self._save_debug_screenshot(tab, "linuxdo_login_page")

        # 4. 等待登录表单加载
        for _ in range(10):
            try:
                has_input = await tab.evaluate("""
                    (function() {
                        return !!document.querySelector('#login-account-name');
                    })()
                """)
                if has_input:
                    logger.info(f"[{self.account_name}] 登录表单已加载")
                    break
            except Exception:
                pass
            await asyncio.sleep(1)

        # 5. 使用 JS 直接赋值填写表单（参考 linuxdo.py，比 send_keys 更可靠）
        try:
            escaped_username = self.linuxdo_username.replace("\\", "\\\\").replace("'", "\\'")
            escaped_password = self.linuxdo_password.replace("\\", "\\\\").replace("'", "\\'")

            fill_result = await tab.evaluate(f"""
                (function() {{
                    const usernameInput = document.querySelector('#login-account-name');
                    const passwordInput = document.querySelector('#login-account-password');
                    if (!usernameInput || !passwordInput) return 'error: inputs not found';

                    usernameInput.focus();
                    usernameInput.value = '{escaped_username}';
                    usernameInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    usernameInput.dispatchEvent(new Event('change', {{ bubbles: true }}));

                    passwordInput.focus();
                    passwordInput.value = '{escaped_password}';
                    passwordInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    passwordInput.dispatchEvent(new Event('change', {{ bubbles: true }}));

                    return 'success';
                }})()
            """)

            if fill_result != 'success':
                logger.error(f"[{self.account_name}] 填写表单失败: {fill_result}")
                return False
            logger.info(f"[{self.account_name}] 已填写用户名和密码")
        except Exception as e:
            logger.error(f"[{self.account_name}] 填写表单失败: {e}")
            return False

        # 6. 点击登录按钮
        logger.info(f"[{self.account_name}] 点击登录按钮...")
        await self._save_debug_screenshot(tab, "before_login_click")
        await asyncio.sleep(1)
        try:
            clicked = await tab.evaluate("""
                (function() {
                    const btn = document.querySelector('#login-button');
                    if (btn) { btn.click(); return true; }
                    return false;
                })()
            """)
            if not clicked:
                logger.error(f"[{self.account_name}] 未找到登录按钮")
                await self._save_debug_screenshot(tab, "login_button_not_found")
                return False
        except Exception as e:
            logger.error(f"[{self.account_name}] 点击登录失败: {e}")
            return False

        # 7. 等待登录完成
        logger.info(f"[{self.account_name}] 等待登录完成...")
        for i in range(60):
            await asyncio.sleep(1)
            current_url = tab.target.url if hasattr(tab, 'target') else ""

            if current_url and "login" not in current_url.lower() and "linux.do" in current_url:
                logger.info(f"[{self.account_name}] 页面已跳转: {current_url}")
                break

            if i % 5 == 0:
                try:
                    error_msg = await tab.evaluate("""
                        (function() {
                            const el = document.querySelector('.alert-error, .login-error');
                            return el ? el.innerText.trim() : '';
                        })()
                    """)
                    if error_msg:
                        logger.error(f"[{self.account_name}] 登录错误: {error_msg}")
                        await self._save_debug_screenshot(tab, "login_error")
                        return False
                except Exception:
                    pass

            if i % 10 == 0:
                logger.debug(f"[{self.account_name}] 等待登录... ({i}s)")
                if self._debug and i > 0:
                    await self._save_debug_screenshot(tab, f"login_waiting_{i}s")

        await asyncio.sleep(2)
        current_url = tab.target.url if hasattr(tab, 'target') else ""

        if "login" in current_url.lower():
            logger.error(f"[{self.account_name}] 登录失败，仍在登录页面")
            await self._save_debug_screenshot(tab, "login_failed_still_on_page")
            return False

        logger.success(f"[{self.account_name}] LinuxDO 登录成功！")
        await self._save_debug_screenshot(tab, "linuxdo_login_success")
        return True

    async def _oauth_login_and_get_session(self, tab) -> tuple[str | None, str | None]:
        """通过 LinuxDO OAuth 登录并获取 session 和 api_user"""
        login_url = f"{self.provider.domain}{self.provider.login_path}"
        logger.info(f"[{self.account_name}] 访问站点登录页: {login_url}")

        await tab.get(login_url)
        await asyncio.sleep(5)
        await self._log_page_info(tab, "provider_login_page")
        await self._save_debug_screenshot(tab, "provider_login_page")
        await self._wait_for_cloudflare(tab, timeout=15)

        # 检查是否已经登录
        current_url = tab.target.url if hasattr(tab, 'target') else ""
        if self.provider.domain in current_url and "login" not in current_url.lower():
            logger.success(f"[{self.account_name}] 已登录，直接获取 session")
            await self._save_debug_screenshot(tab, "already_logged_in")
            return await self._extract_session_from_browser(tab)

        # 等待页面加载并查找 LinuxDO 按钮
        logger.info(f"[{self.account_name}] 查找 LinuxDO OAuth 登录按钮...")
        await asyncio.sleep(3)

        # Debug 模式：打印页面上所有可点击元素帮助调试
        if self._debug:
            try:
                page_text = await tab.evaluate("document.body.innerText.substring(0, 500)")
                logger.debug(f"[{self.account_name}] 页面内容: {page_text[:200]}...")

                # 列出所有可能的登录按钮
                buttons_info = await tab.evaluate("""
                    (function() {
                        const results = [];
                        const elements = document.querySelectorAll('button, a, [role="button"], [onclick]');
                        for (const el of elements) {
                            const text = (el.innerText || el.textContent || '').trim();
                            const href = el.href || el.getAttribute('href') || '';
                            if (text || href) {
                                results.push({
                                    tag: el.tagName,
                                    text: text.substring(0, 50),
                                    href: href.substring(0, 80),
                                    class: el.className.substring(0, 50)
                                });
                            }
                        }
                        return JSON.stringify(results.slice(0, 20));
                    })()
                """)
                logger.debug(f"[{self.account_name}] 页面按钮列表: {buttons_info}")
            except Exception as e:
                logger.debug(f"[{self.account_name}] 获取页面信息失败: {e}")

        # 点击 LinuxDO OAuth 按钮（使用多种匹配策略）
        clicked = False
        for attempt in range(5):
            try:
                # 策略1: 查找包含 linuxdo 的链接（最可靠）
                clicked_result = await tab.evaluate("""
                    (function() {
                        // 策略1: 查找 href 包含 linuxdo 或 oauth 的链接
                        const links = document.querySelectorAll('a[href*="linuxdo"], a[href*="oauth/linuxdo"]');
                        for (const link of links) {
                            link.click();
                            return 'clicked link: ' + (link.href || '').substring(0, 60);
                        }

                        // 策略2: 查找文本包含 LINUX DO 的按钮/链接（不区分大小写，支持多种写法）
                        const allClickable = document.querySelectorAll('button, a, [role="button"], div[onclick], span[onclick]');
                        const patterns = [
                            /linux\s*do/i,           // LINUX DO, LinuxDO, linux do
                            /通过.*linux/i,          // 通过 LINUX DO 登录
                            /使用.*linux/i,          // 使用 LINUX DO 登录
                            /continue.*linux/i,      // Continue with LinuxDO
                            /login.*linux/i,         // Login with LinuxDO
                            /第三方.*登录/i,          // 第三方登录（可能是展开按钮）
                            /其他.*登录/i,           // 其他登录方式
                            /更多.*方式/i            // 更多登录方式
                        ];

                        for (const el of allClickable) {
                            const text = (el.innerText || el.textContent || '').trim();
                            for (const pattern of patterns) {
                                if (pattern.test(text)) {
                                    el.click();
                                    return 'clicked text: ' + text.substring(0, 40);
                                }
                            }
                        }

                        // 策略3: 查找包含 linuxdo 图标的元素
                        const icons = document.querySelectorAll('img[src*="linuxdo"], svg[class*="linuxdo"], i[class*="linuxdo"]');
                        for (const icon of icons) {
                            const parent = icon.closest('button, a, [role="button"]') || icon.parentElement;
                            if (parent) {
                                parent.click();
                                return 'clicked icon parent';
                            }
                        }

                        return null;
                    })()
                """)

                if clicked_result:
                    logger.info(f"[{self.account_name}] {clicked_result}")
                    await self._save_debug_screenshot(tab, "oauth_button_clicked")
                    clicked = True
                    break

                logger.debug(f"[{self.account_name}] 第 {attempt + 1} 次尝试未找到 OAuth 按钮")
            except Exception as e:
                logger.debug(f"[{self.account_name}] 查找 OAuth 按钮出错: {e}")
            await asyncio.sleep(1)

        if not clicked:
            logger.warning(f"[{self.account_name}] 未找到 LinuxDO OAuth 按钮")
            await self._save_debug_screenshot(tab, "oauth_button_not_found")

        # 等待 OAuth 授权
        logger.info(f"[{self.account_name}] 等待 OAuth 授权...")
        await asyncio.sleep(3)
        browser = self._browser_manager.browser

        # 处理授权页面（可能在新标签页）
        for i in range(30):
            # 检查新标签页
            if len(browser.tabs) > 1:
                for t in browser.tabs:
                    t_url = t.target.url if hasattr(t, 'target') else ""
                    if "connect.linux.do" in t_url or "authorize" in t_url.lower():
                        logger.info(f"[{self.account_name}] 找到授权标签页: {t_url}")
                        await t.bring_to_front()
                        tab = t
                        await asyncio.sleep(1)
                        break

            current_url = tab.target.url if hasattr(tab, 'target') else ""

            # 如果已经在目标站点且不是登录页，获取 session
            if self.provider.domain in current_url and "login" not in current_url.lower():
                logger.success(f"[{self.account_name}] OAuth 登录成功！")
                return await self._extract_session_from_browser(tab)

            # 如果在授权页面，点击允许
            if "linux.do" in current_url and ("authorize" in current_url.lower() or "oauth" in current_url.lower()):
                logger.info(f"[{self.account_name}] 检测到授权页面，尝试点击允许...")
                await self._save_debug_screenshot(tab, "oauth_authorize_page")
                await asyncio.sleep(2)
                try:
                    clicked = await tab.evaluate("""
                        (function() {
                            const els = document.querySelectorAll('button, a, input[type="submit"]');
                            for (const el of els) {
                                const text = (el.innerText || el.value || '').trim();
                                if (text === '允许' || text.includes('允许')) {
                                    el.click();
                                    return 'clicked: ' + text;
                                }
                            }
                            return null;
                        })()
                    """)
                    if clicked:
                        logger.info(f"[{self.account_name}] {clicked}")
                        await asyncio.sleep(3)
                except Exception:
                    pass

            # 检查所有标签页是否有已登录的
            for t in browser.tabs:
                t_url = t.target.url if hasattr(t, 'target') else ""
                if self.provider.domain in t_url and "login" not in t_url.lower():
                    await t.bring_to_front()
                    await self._save_debug_screenshot(t, "oauth_success")
                    return await self._extract_session_from_browser(t)

            await asyncio.sleep(1)
            if i % 5 == 0 and i > 0:
                logger.debug(f"[{self.account_name}] 等待 OAuth 完成... ({i}s)")
                if self._debug:
                    await self._save_debug_screenshot(tab, f"oauth_waiting_{i}s")

        logger.error(f"[{self.account_name}] OAuth 登录超时")
        await self._save_debug_screenshot(tab, "oauth_timeout")
        return None, None

    async def _extract_session_from_browser(self, tab) -> tuple[str | None, str | None]:
        """从浏览器提取 session 和 api_user"""
        session_cookie = None
        api_user = None

        try:
            import nodriver.cdp.network as cdp_network
            all_cookies = await tab.send(cdp_network.get_all_cookies())

            for cookie in all_cookies:
                if cookie.name == "session":
                    session_cookie = cookie.value
                    logger.info(f"[{self.account_name}] 获取到 session: {session_cookie[:30]}...")
                elif cookie.name == self.provider.api_user_key:
                    api_user = cookie.value
                    logger.info(f"[{self.account_name}] 获取到 api_user: {api_user}")

            if not api_user:
                api_user = await tab.evaluate(f'''
                    localStorage.getItem("{self.provider.api_user_key}") ||
                    localStorage.getItem("user_id")
                ''')
                if api_user:
                    logger.info(f"[{self.account_name}] 从 localStorage 获取到 api_user: {api_user}")

        except Exception as e:
            logger.error(f"[{self.account_name}] 提取 session 失败: {e}")

        return session_cookie, api_user

    async def run(self) -> CheckinResult:
        """执行完整的签到流程"""
        try:
            # 1. 优先尝试使用预设的 Cookie
            if self._preset_cookies and self._preset_api_user:
                session_cookie = self._preset_cookies.get("session")
                if session_cookie:
                    logger.info(f"[{self.account_name}] 尝试使用预设 Cookie 签到...")
                    success, message, details = await self._checkin_with_cookies(session_cookie, self._preset_api_user)

                    if success:
                        self._login_method = "cookie"
                        details["login_method"] = "cookie"
                        return CheckinResult(
                            platform=f"NewAPI ({self.provider_name})",
                            account=self.account_name,
                            status=CheckinStatus.SUCCESS,
                            message=message,
                            details=details,
                        )
                    elif "过期" not in message and "401" not in message:
                        return CheckinResult(
                            platform=f"NewAPI ({self.provider_name})",
                            account=self.account_name,
                            status=CheckinStatus.FAILED,
                            message=message,
                            details=details,
                        )
                    logger.warning(f"[{self.account_name}] Cookie 已过期，尝试 OAuth 登录...")

            # 2. Cookie 无效，使用浏览器 OAuth 登录
            if not self.linuxdo_username or not self.linuxdo_password:
                return CheckinResult(
                    platform=f"NewAPI ({self.provider_name})",
                    account=self.account_name,
                    status=CheckinStatus.FAILED,
                    message="Cookie 无效且未提供 LinuxDO 账号密码",
                )

            # 启动浏览器（参考 linuxdo.py 使用 BrowserManager）
            logger.info(f"[{self.account_name}] 启动浏览器进行 OAuth 登录...")

            is_ci = bool(os.environ.get("CI")) or bool(os.environ.get("GITHUB_ACTIONS"))
            display_set = bool(os.environ.get("DISPLAY"))

            # 默认使用非 headless 模式（更容易绕过 Cloudflare）
            # 只有明确设置 BROWSER_HEADLESS=true 才使用 headless
            headless = os.environ.get("BROWSER_HEADLESS", "false").lower() == "true"

            if is_ci and display_set:
                headless = False
                logger.info(f"[{self.account_name}] CI 环境使用非 headless 模式")

            engine = get_browser_engine()
            max_retries = 5 if is_ci else 3
            self._browser_manager = BrowserManager(engine=engine, headless=headless)
            await self._browser_manager.start(max_retries=max_retries)

            tab = self._browser_manager.page

            # 登录 LinuxDO
            if not await self._login_linuxdo(tab):
                return CheckinResult(
                    platform=f"NewAPI ({self.provider_name})",
                    account=self.account_name,
                    status=CheckinStatus.FAILED,
                    message="LinuxDO 登录失败",
                )

            # OAuth 登录并获取 session
            session_cookie, api_user = await self._oauth_login_and_get_session(tab)

            if not session_cookie or not api_user:
                return CheckinResult(
                    platform=f"NewAPI ({self.provider_name})",
                    account=self.account_name,
                    status=CheckinStatus.FAILED,
                    message="OAuth 登录失败，无法获取 session",
                )

            # 3. 使用新获取的 session 签到
            logger.info(f"[{self.account_name}] 使用新 session 签到...")
            success, message, details = await self._checkin_with_cookies(session_cookie, api_user)

            self._login_method = "oauth"
            details["login_method"] = "oauth"
            details["new_session"] = session_cookie[:20] + "..."
            details["new_api_user"] = api_user

            return CheckinResult(
                platform=f"NewAPI ({self.provider_name})",
                account=self.account_name,
                status=CheckinStatus.SUCCESS if success else CheckinStatus.FAILED,
                message=message,
                details=details,
            )

        except Exception as e:
            logger.error(f"[{self.account_name}] 签到异常: {e}")
            return CheckinResult(
                platform=f"NewAPI ({self.provider_name})",
                account=self.account_name,
                status=CheckinStatus.FAILED,
                message=f"签到异常: {str(e)}",
            )

        finally:
            if self._browser_manager:
                logger.info(f"[{self.account_name}] 关闭浏览器...")
                await self._browser_manager.close()


async def browser_checkin_newapi(
    provider_name: str,
    linuxdo_username: str | None = None,
    linuxdo_password: str | None = None,
    cookies: dict | str | None = None,
    api_user: str | None = None,
    account_name: str | None = None,
) -> CheckinResult:
    """便捷函数：使用浏览器签到 NewAPI 站点"""
    checker = NewAPIBrowserCheckin(
        provider_name=provider_name,
        linuxdo_username=linuxdo_username,
        linuxdo_password=linuxdo_password,
        cookies=cookies,
        api_user=api_user,
        account_name=account_name,
    )
    return await checker.run()


def load_linuxdo_accounts(config_path: str = "签到账户/签到账户linuxdo.json") -> list[dict]:
    """从配置文件加载 LinuxDO 账户

    配置格式：
    [
        {
            "username": "email@example.com",
            "password": "password",
            "name": "显示名称",
            "level": 3,
            "browse_enabled": true
        }
    ]
    """
    try:
        with open(config_path, encoding="utf-8") as f:
            accounts = json.load(f)
        logger.info(f"从 {config_path} 加载了 {len(accounts)} 个 LinuxDO 账户")
        return accounts
    except FileNotFoundError:
        logger.warning(f"配置文件不存在: {config_path}")
        return []
    except Exception as e:
        logger.error(f"加载配置文件失败: {e}")
        return []
