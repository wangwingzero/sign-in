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
    return os.environ.get("DEBUG", "").lower() in ("true", "1", "yes") or os.environ.get(
        "NEWAPI_DEBUG", ""
    ).lower() in ("true", "1", "yes")


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
            url = tab.target.url if hasattr(tab, "target") else "unknown"
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
                elif response.status_code == 403:
                    return False, "HTTP 403 被拦截(Cookie过期或Cloudflare)", details
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

    @staticmethod
    def _to_float(val) -> float:
        """将 nodriver 返回的值转为 float（处理 {'type': 'number', 'value': 123} 格式）"""
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, dict):
            return float(val.get('value', 0))
        return float(val)

    async def _wait_for_cloudflare(self, tab, timeout: int = 60) -> bool:
        """等待 Cloudflare 挑战完成（支持 5 秒盾和 Turnstile 验证）

        核心策略：
        1. 先等 3 秒让非交互式挑战（5 秒盾）自动完成
        2. 如果还在 CF 页面，多种方式定位 Turnstile checkbox 并点击
        3. 最多点击 5 次，每次间隔 5 秒等待验证结果
        """
        logger.info(f"[{self.account_name}] 检测 Cloudflare 挑战...")
        start_time = asyncio.get_event_loop().time()
        turnstile_click_count = 0
        max_turnstile_clicks = 5
        # 先等一小段时间让非交互式挑战自动完成
        initial_wait_done = False

        while asyncio.get_event_loop().time() - start_time < timeout:
            try:
                title = await tab.evaluate("document.title")
                title_lower = title.lower() if title else ""

                # 检测是否仍在 Cloudflare 挑战页面（标题匹配）
                cf_indicators = [
                    "just a moment", "checking your browser", "please wait",
                    "verifying", "checking", "challenge",
                    "请稍候", "验证", "确认",
                ]
                is_cf_title = any(ind in title_lower for ind in cf_indicators)

                # 检测 Turnstile iframe 或容器是否存在（DOM 匹配）
                has_cf_element = await tab.evaluate(r"""
                    (function() {
                        // 方法1: 查找任何 cloudflare 相关的 iframe
                        const iframes = document.querySelectorAll('iframe');
                        for (const iframe of iframes) {
                            const src = (iframe.src || '').toLowerCase();
                            if (src.includes('cloudflare')) return true;
                        }
                        // 方法2: 查找 Turnstile 容器
                        if (document.querySelector('.cf-turnstile, div[data-sitekey], #cf-turnstile, #challenge-running, #challenge-stage')) return true;
                        // 方法3: 查找验证相关文字（覆盖中英文）
                        const bodyText = document.body?.innerText || '';
                        const cfTexts = [
                            '确认您是真人', '验证您是真人', '验证您是否是真人',
                            '请完成以下操作', '需要先检查您的连接',
                            'verify you are human', 'checking if the site connection is secure'
                        ];
                        return cfTexts.some(t => bodyText.toLowerCase().includes(t.toLowerCase()));
                    })()
                """)

                is_cf_page = is_cf_title or has_cf_element

                if not is_cf_page and title:
                    logger.success(f"[{self.account_name}] Cloudflare 验证通过！")
                    await self._save_debug_screenshot(tab, "cf_passed")
                    return True

                # 前 3 秒只等待，不点击（让非交互式挑战自动完成）
                elapsed = asyncio.get_event_loop().time() - start_time
                if not initial_wait_done and elapsed < 3:
                    logger.debug(f"[{self.account_name}] 等待非交互式挑战自动完成... ({elapsed:.0f}s)")
                    await asyncio.sleep(1)
                    continue
                initial_wait_done = True

                if is_cf_page and turnstile_click_count < max_turnstile_clicks:
                    # 多种方式定位 Turnstile checkbox
                    iframe_rect = await tab.evaluate(r"""
                        (function() {
                            const iframes = document.querySelectorAll('iframe');

                            // 策略1: 查找 src 包含 cloudflare 的 iframe（最可靠）
                            for (const iframe of iframes) {
                                const src = (iframe.src || '').toLowerCase();
                                if (src.includes('cloudflare')) {
                                    const rect = iframe.getBoundingClientRect();
                                    if (rect.width > 0 && rect.height > 0) {
                                        return [rect.x, rect.y, rect.width, rect.height, 'cf-iframe'];
                                    }
                                }
                            }

                            // 策略2: .cf-turnstile / div[data-sitekey] 容器
                            const container = document.querySelector('.cf-turnstile') ||
                                              document.querySelector('div[data-sitekey]') ||
                                              document.querySelector('#cf-turnstile') ||
                                              document.querySelector('#challenge-stage');
                            if (container) {
                                const rect = container.getBoundingClientRect();
                                if (rect.width > 0 && rect.height > 0) {
                                    return [rect.x, rect.y, rect.width, rect.height, 'cf-container'];
                                }
                            }

                            // 策略3: 查找 Turnstile 尺寸的 iframe（宽 280-350, 高 50-80）
                            for (const iframe of iframes) {
                                const rect = iframe.getBoundingClientRect();
                                if (rect.width >= 280 && rect.width <= 350 &&
                                    rect.height >= 50 && rect.height <= 80) {
                                    return [rect.x, rect.y, rect.width, rect.height, 'size-match-iframe'];
                                }
                            }

                            // 策略4: 放宽尺寸匹配 — 任何可见的小 iframe
                            for (const iframe of iframes) {
                                const rect = iframe.getBoundingClientRect();
                                if (rect.width >= 200 && rect.width <= 500 &&
                                    rect.height >= 40 && rect.height <= 120 &&
                                    rect.y > 0) {
                                    return [rect.x, rect.y, rect.width, rect.height, 'any-small-iframe'];
                                }
                            }

                            // 策略5: 在托管挑战页面上，查找 checkbox 容器
                            // 托管页面整页都是 CF 挑战，checkbox 在页面中部偏上
                            const bodyText = document.body?.innerText || '';
                            if (bodyText.includes('确认您是真人') || bodyText.includes('verify you are human')) {
                                // 查找包含复选框的 label 或 span
                                const labels = document.querySelectorAll('label, span, div');
                                for (const el of labels) {
                                    const text = (el.innerText || '').trim();
                                    if (text === '确认您是真人' || text === 'Verify you are human') {
                                        const rect = el.getBoundingClientRect();
                                        if (rect.width > 0 && rect.height > 0) {
                                            return [rect.x, rect.y, rect.width, rect.height, 'checkbox-label'];
                                        }
                                    }
                                }
                                // 最后兜底：返回页面上 checkbox 区域的大致位置
                                // 托管挑战页面的 checkbox 通常在 (215, 175) 附近，宽 170, 高 25
                                return [215, 170, 170, 30, 'managed-page-guess'];
                            }

                            return null;
                        })()
                    """)

                    if iframe_rect and isinstance(iframe_rect, (list, tuple)) and len(iframe_rect) >= 4:
                        try:
                            x = self._to_float(iframe_rect[0])
                            y = self._to_float(iframe_rect[1])
                            w = self._to_float(iframe_rect[2])
                            h = self._to_float(iframe_rect[3])
                            method = iframe_rect[4] if len(iframe_rect) > 4 else 'N/A'

                            # checkbox 在元素内左侧偏移约 30px, 垂直居中
                            # 对于 managed-page-guess 和 checkbox-label，点击元素左侧
                            if method in ('checkbox-label', 'managed-page-guess'):
                                click_x = x + 15
                                click_y = y + h / 2
                            else:
                                click_x = x + 30
                                click_y = y + h / 2

                            logger.info(
                                f"[{self.account_name}] 发现 Turnstile "
                                f"({method}, pos: {x:.0f},{y:.0f}, size: {w:.0f}x{h:.0f}), "
                                f"点击 ({click_x:.0f}, {click_y:.0f})"
                            )

                            await tab.mouse_click(click_x, click_y)
                            turnstile_click_count += 1
                            logger.info(f"[{self.account_name}] 已点击 Turnstile (第 {turnstile_click_count} 次)")
                            await asyncio.sleep(5)  # 等待验证结果
                        except Exception as e:
                            logger.debug(f"[{self.account_name}] 点击 Turnstile 失败: {e}")
                    else:
                        logger.debug(f"[{self.account_name}] 未找到 Turnstile iframe/容器，等待...")

                    # 注意：CF 冻结页面上截图会挂起 60 秒+，不在循环中截图
            except Exception as e:
                logger.debug(f"[{self.account_name}] 检查页面状态出错: {e}")
            await asyncio.sleep(2)

        logger.warning(f"[{self.account_name}] Cloudflare 验证超时")
        return False

    async def _wait_for_cloudflare_with_retry(self, tab, max_retries: int = 5) -> bool:
        """带重试的 Cloudflare 验证（核心策略：多刷新多尝试，nodriver 有概率绕过）

        碰到 CF 的核心就是多尝试几次，每次刷新页面让 nodriver 有新的机会绕过。
        短等待 + 多重试 + 快刷新 = 高成功率。

        Args:
            tab: nodriver 标签页
            max_retries: 最大重试次数（默认 5 次）

        Returns:
            是否通过 Cloudflare 验证
        """
        for attempt in range(max_retries):
            logger.info(f"[{self.account_name}] Cloudflare 验证尝试 {attempt + 1}/{max_retries}...")

            # 每次等待 15-20 秒，不要太长（CF 要么很快过，要么需要刷新重来）
            timeout = 20 if attempt == 0 else 15

            # 等待 Cloudflare 验证
            cf_passed = await self._wait_for_cloudflare(tab, timeout=timeout)

            if cf_passed:
                if attempt > 0:
                    logger.success(f"[{self.account_name}] 第 {attempt + 1} 次尝试通过 Cloudflare！")
                return True

            # 最后一次尝试失败，不再重试
            if attempt >= max_retries - 1:
                logger.error(f"[{self.account_name}] Cloudflare 验证失败，已重试 {max_retries} 次")
                return False

            # 短暂等待后刷新页面重来（核心：让 nodriver 有新的机会绕过 CF）
            wait_time = 3 + attempt * 2  # 3s, 5s, 7s, 9s 递增
            logger.warning(
                f"[{self.account_name}] Cloudflare 未通过，"
                f"等待 {wait_time}s 后刷新重试（{attempt + 2}/{max_retries}）..."
            )
            await asyncio.sleep(wait_time)

            # 刷新页面，给 nodriver 一次全新的机会
            logger.info(f"[{self.account_name}] 刷新页面...")
            await tab.reload()
            await asyncio.sleep(3)  # 等待页面开始加载

        return False

    async def _login_linuxdo(self, tab) -> bool:
        """登录 LinuxDO（参考 linuxdo.py 的成功实现，使用 JS 直接赋值）
        
        Discourse 论坛的登录表单是模态框形式，访问 /login 会自动触发模态框。
        如果模态框没有自动弹出，需要手动点击登录按钮。
        """
        # 1. 先访问首页，让 Cloudflare 验证
        logger.info(f"[{self.account_name}] 访问 LinuxDO 首页...")
        await tab.get(self.LINUXDO_URL)
        await self._log_page_info(tab, "linuxdo_home")

        # 2. 等待 Cloudflare 挑战完成（多次重试策略）
        cf_passed = await self._wait_for_cloudflare_with_retry(tab, max_retries=3)
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

        # 3. 访问登录页面（Discourse 会自动弹出登录模态框）
        logger.info(f"[{self.account_name}] 访问登录页面...")
        await tab.get(self.LINUXDO_LOGIN_URL)

        # 等待页面加载完成
        await asyncio.sleep(3)
        await self._log_page_info(tab, "linuxdo_login_page")
        await self._save_debug_screenshot(tab, "linuxdo_login_page")

        # 4. 等待登录表单加载（模态框形式）
        login_form_found = False
        for attempt in range(15):  # 增加到 15 次尝试
            try:
                has_input = await tab.evaluate("""
                    (function() {
                        return !!document.querySelector('#login-account-name');
                    })()
                """)
                if has_input:
                    logger.info(f"[{self.account_name}] 登录表单已加载")
                    login_form_found = True
                    break
            except Exception:
                pass

            # 如果表单没出现，尝试点击登录按钮触发模态框
            if attempt == 5:
                logger.info(f"[{self.account_name}] 尝试点击登录按钮触发模态框...")
                try:
                    clicked = await tab.evaluate("""
                        (function() {
                            // 查找登录按钮（多种可能的选择器）
                            const selectors = [
                                '.login-button',
                                'button.login-button',
                                '.header-buttons .login-button',
                                'a.login-button',
                                '[class*="login"]',
                                'button:contains("登录")',
                                'a:contains("登录")'
                            ];
                            for (const sel of selectors) {
                                try {
                                    const btn = document.querySelector(sel);
                                    if (btn && btn.offsetParent !== null) {
                                        btn.click();
                                        return 'clicked: ' + sel;
                                    }
                                } catch (e) {}
                            }
                            
                            // 备用：查找包含"登录"文字的按钮
                            const allButtons = document.querySelectorAll('button, a');
                            for (const btn of allButtons) {
                                const text = (btn.innerText || '').trim();
                                if (text === '登录' || text === 'Log In' || text === 'Login') {
                                    btn.click();
                                    return 'clicked text: ' + text;
                                }
                            }
                            return null;
                        })()
                    """)
                    if clicked:
                        logger.info(f"[{self.account_name}] {clicked}")
                        await asyncio.sleep(2)
                except Exception as e:
                    logger.debug(f"[{self.account_name}] 点击登录按钮失败: {e}")

            await asyncio.sleep(1)

        if not login_form_found:
            logger.error(f"[{self.account_name}] 登录表单未加载")
            await self._save_debug_screenshot(tab, "login_form_not_found")
            return False

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

            if fill_result != "success":
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
            current_url = tab.target.url if hasattr(tab, "target") else ""

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
        current_url = tab.target.url if hasattr(tab, "target") else ""

        if "login" in current_url.lower():
            logger.error(f"[{self.account_name}] 登录失败，仍在登录页面")
            await self._save_debug_screenshot(tab, "login_failed_still_on_page")
            return False

        logger.success(f"[{self.account_name}] LinuxDO 登录成功！")
        await self._save_debug_screenshot(tab, "linuxdo_login_success")
        return True

    async def _oauth_login_and_get_session(self, tab) -> tuple[str | None, str | None]:
        """通过 LinuxDO OAuth 登录并获取 session 和 api_user"""

        # 如果 provider 配置了 oauth_path，直接导航（跳过按钮检测，适用于 Wong 等 SPA 站点）
        oauth_path = getattr(self.provider, 'oauth_path', None)
        if oauth_path:
            oauth_url = f"{self.provider.domain}{oauth_path}"
            logger.info(f"[{self.account_name}] 直接 OAuth 导航: {oauth_url}")
            await tab.get(oauth_url)
            await asyncio.sleep(5)

            # 检查是否到了授权页或已自动回来
            current_url = tab.target.url if hasattr(tab, "target") else ""
            if "linux.do" in current_url and "authorize" in current_url.lower():
                logger.info(f"[{self.account_name}] 到达授权页，点击允许...")
                await tab.evaluate(r"""
                    (function() {
                        const links = document.querySelectorAll('a');
                        for (const a of links) {
                            if (/允许/.test(a.innerText) && a.href) {
                                window.location.href = a.href;
                                return true;
                            }
                        }
                        return false;
                    })()
                """)
                await asyncio.sleep(5)

            # 等待所有重定向完成（OAuth callback → set cookie → /console）
            provider_host = self.provider.domain.replace("https://", "").replace("http://", "")
            for _redir_wait in range(15):
                await asyncio.sleep(1)
                current_url = tab.target.url if hasattr(tab, "target") else ""
                if provider_host in current_url and "oauth" not in current_url.lower():
                    logger.success(f"[{self.account_name}] 直接 OAuth 成功！URL: {current_url}")
                    # 多等 2 秒确保 cookie 设置完成
                    await asyncio.sleep(2)
                    return await self._extract_session_from_browser(tab)

            current_url = tab.target.url if hasattr(tab, "target") else ""
            if provider_host in current_url:
                logger.success(f"[{self.account_name}] 直接 OAuth 成功（仍在 OAuth 路径）: {current_url}")
                await asyncio.sleep(2)
                return await self._extract_session_from_browser(tab)
            else:
                logger.warning(f"[{self.account_name}] 直接 OAuth 未返回站点: {current_url}")

        login_url = f"{self.provider.domain}{self.provider.login_path}"
        logger.info(f"[{self.account_name}] 访问站点登录页: {login_url}")

        await tab.get(login_url)
        await asyncio.sleep(5)
        await self._log_page_info(tab, "provider_login_page")
        await self._save_debug_screenshot(tab, "provider_login_page")
        await self._wait_for_cloudflare_with_retry(tab)

        # 检查是否已经登录
        current_url = tab.target.url if hasattr(tab, "target") else ""
        if self.provider.domain in current_url and "login" not in current_url.lower():
            logger.success(f"[{self.account_name}] 已登录，直接获取 session")
            await self._save_debug_screenshot(tab, "already_logged_in")
            return await self._extract_session_from_browser(tab)

        # 等待 SPA 页面渲染（最多 5 秒）
        await asyncio.sleep(5)

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

        # 先检查并勾选用户协议复选框（某些站点如 techstar 使用 Semi Design UI）
        try:
            checkbox_result = await tab.evaluate(r"""
                (function() {
                    // 策略1: 查找 Semi Design 的 checkbox 组件（.semi-checkbox）
                    const semiCheckboxes = document.querySelectorAll('.semi-checkbox');
                    for (const cb of semiCheckboxes) {
                        const text = (cb.innerText || cb.textContent || '').toLowerCase();
                        const parentText = (cb.parentElement?.innerText || '').toLowerCase();
                        const combinedText = text + ' ' + parentText;
                        
                        // 匹配用户协议、隐私政策等关键词
                        if (combinedText.includes('协议') || combinedText.includes('政策') || 
                            combinedText.includes('同意') || combinedText.includes('阅读') ||
                            combinedText.includes('agree') || combinedText.includes('terms') ||
                            combinedText.includes('policy') || combinedText.includes('privacy')) {
                            // 检查是否已勾选（Semi Design 使用 .semi-checkbox-checked 类）
                            if (!cb.classList.contains('semi-checkbox-checked')) {
                                cb.click();
                                return 'semi-checkbox clicked: ' + text.substring(0, 50);
                            }
                            return 'semi-checkbox already checked';
                        }
                    }
                    
                    // 策略2: 查找标准的 input[type="checkbox"]
                    const checkboxes = document.querySelectorAll('input[type="checkbox"]');
                    for (const cb of checkboxes) {
                        const label = cb.closest('label') || document.querySelector(`label[for="${cb.id}"]`);
                        const parent = cb.parentElement;
                        const text = (label?.innerText || parent?.innerText || '').toLowerCase();
                        
                        if (text.includes('协议') || text.includes('政策') || 
                            text.includes('同意') || text.includes('阅读') ||
                            text.includes('agree') || text.includes('terms') ||
                            text.includes('policy') || text.includes('privacy')) {
                            if (!cb.checked) {
                                cb.click();
                                return 'input-checkbox clicked: ' + text.substring(0, 50);
                            }
                            return 'input-checkbox already checked';
                        }
                    }
                    
                    // 策略3: 查找包含协议文字的可点击区域（div/span/label）
                    const clickables = document.querySelectorAll('div, span, label');
                    for (const el of clickables) {
                        const text = (el.innerText || '').toLowerCase();
                        // 精确匹配"我已阅读并同意"这类文字
                        if ((text.includes('我已阅读') || text.includes('i have read')) && 
                            (text.includes('同意') || text.includes('agree'))) {
                            // 查找内部的 checkbox 或直接点击
                            const innerCb = el.querySelector('input[type="checkbox"], .semi-checkbox');
                            if (innerCb) {
                                innerCb.click();
                                return 'inner-checkbox clicked';
                            }
                            // 如果没有内部 checkbox，点击整个区域
                            el.click();
                            return 'agreement-area clicked: ' + text.substring(0, 50);
                        }
                    }
                    
                    return null;
                })()
            """)
            if checkbox_result:
                logger.info(f"[{self.account_name}] 用户协议复选框: {checkbox_result}")
                await asyncio.sleep(1)  # 等待 UI 更新
        except Exception as e:
            logger.debug(f"[{self.account_name}] 检查用户协议复选框失败: {e}")

        # 点击 LinuxDO OAuth 按钮（返回位置 + mouse_click 物理点击，Semi Design 等组件的 JS .click() 不可靠）
        # 多次重试：SPA 站点（如 Wong）的按钮需要等待 JS 框架渲染完成
        clicked = False
        for attempt in range(10):
            try:
                # 返回按钮位置 [x, y, w, h, description]，用于 mouse_click
                btn_rect = await tab.evaluate(r"""
                    (function() {
                        function getRect(el, desc) {
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) {
                                return [rect.x, rect.y, rect.width, rect.height, desc];
                            }
                            return null;
                        }

                        // 策略1: 查找 href 包含 linuxdo 或 oauth 的链接
                        const links = document.querySelectorAll('a[href*="linuxdo"], a[href*="oauth/linuxdo"]');
                        for (const link of links) {
                            const r = getRect(link, 'link: ' + (link.href||'').substring(0,50));
                            if (r) return r;
                        }

                        // 策略2: 文本匹配 LINUX DO
                        const allClickable = document.querySelectorAll('button, a, [role="button"], div[onclick], span[onclick]');
                        const patterns = [
                            /linux\s*do/i, /通过.*linux/i, /使用.*linux/i,
                            /continue.*linux/i, /login.*linux/i,
                            /第三方.*登录/i, /其他.*登录/i, /更多.*方式/i
                        ];
                        for (const el of allClickable) {
                            const text = (el.innerText || el.textContent || '').trim();
                            for (const pattern of patterns) {
                                if (pattern.test(text)) {
                                    const r = getRect(el, 'text: ' + text.substring(0,30));
                                    if (r) return r;
                                }
                            }
                        }

                        // 策略3: linuxdo 图标
                        const icons = document.querySelectorAll('img[src*="linuxdo"], svg[class*="linuxdo"]');
                        for (const icon of icons) {
                            const parent = icon.closest('button, a, [role="button"]') || icon.parentElement;
                            if (parent) {
                                const r = getRect(parent, 'icon-parent');
                                if (r) return r;
                            }
                        }

                        // 策略4: "使用...继续" 图标按钮（Wong 等）
                        for (const el of allClickable) {
                            const text = (el.innerText || '').replace(/\s+/g, '').trim();
                            if (/使用.*继续/.test(text) || /continue/i.test(text)) {
                                if (el.querySelector('img, svg') || el.className.includes('tertiary')) {
                                    const r = getRect(el, 'icon-btn: ' + (el.innerText||'').trim().substring(0,20));
                                    if (r) return r;
                                }
                            }
                        }

                        // 策略5: 按钮内图片 alt/src 含 linux
                        const allBtns = document.querySelectorAll('button, a, [role="button"]');
                        for (const btn of allBtns) {
                            for (const img of btn.querySelectorAll('img')) {
                                const s = ((img.alt||'') + (img.src||'')).toLowerCase();
                                if (s.includes('linux') || s.includes('oauth') || s.includes('connect')) {
                                    const r = getRect(btn, 'img-btn: ' + (img.src||'').substring(0,30));
                                    if (r) return r;
                                }
                            }
                        }

                        return null;
                    })()
                """)

                if btn_rect and isinstance(btn_rect, (list, tuple)) and len(btn_rect) >= 4:
                    x = self._to_float(btn_rect[0])
                    y = self._to_float(btn_rect[1])
                    w = self._to_float(btn_rect[2])
                    h = self._to_float(btn_rect[3])
                    desc = btn_rect[4] if len(btn_rect) > 4 else '?'
                    if isinstance(desc, dict):
                        desc = desc.get('value', desc)
                    click_x = x + w / 2
                    click_y = y + h / 2
                    logger.info(
                        f"[{self.account_name}] 找到 OAuth 按钮: {desc}, "
                        f"物理点击 ({click_x:.0f}, {click_y:.0f})"
                    )
                    await tab.mouse_click(click_x, click_y)
                    await self._save_debug_screenshot(tab, "oauth_button_clicked")
                    clicked = True
                    break

                logger.debug(f"[{self.account_name}] 第 {attempt + 1} 次尝试未找到 OAuth 按钮")
            except Exception as e:
                logger.debug(f"[{self.account_name}] 查找 OAuth 按钮出错: {e}")
            await asyncio.sleep(1)

        if not clicked and not oauth_path:
            # 登录页没找到 OAuth 按钮，尝试注册页（不对已配 oauth_path 的站点走注册页）
            register_url = f"{self.provider.domain}/register"
            logger.info(f"[{self.account_name}] 登录页未找到 OAuth 按钮，尝试注册页: {register_url}")
            await tab.get(register_url)
            await asyncio.sleep(3)
            await self._save_debug_screenshot(tab, "register_page")

            # 在注册页重新查找 OAuth 按钮（同样用 mouse_click）
            for attempt in range(3):
                try:
                    btn_rect = await tab.evaluate(r"""
                        (function() {
                            function getRect(el, desc) {
                                const rect = el.getBoundingClientRect();
                                if (rect.width > 0 && rect.height > 0)
                                    return [rect.x, rect.y, rect.width, rect.height, desc];
                                return null;
                            }
                            const links = document.querySelectorAll('a[href*="linuxdo"], a[href*="oauth/linuxdo"]');
                            for (const link of links) {
                                const r = getRect(link, 'link: ' + (link.href||'').substring(0,50));
                                if (r) return r;
                            }
                            const allClickable = document.querySelectorAll('button, a, [role="button"], div[onclick], span[onclick]');
                            const patterns = [
                                /linux\s*do/i, /使用.*linux/i, /通过.*linux/i,
                                /continue.*linux/i, /login.*linux/i
                            ];
                            for (const el of allClickable) {
                                const text = (el.innerText || el.textContent || '').trim();
                                for (const p of patterns) {
                                    if (p.test(text)) {
                                        const r = getRect(el, 'text: ' + text.substring(0,30));
                                        if (r) return r;
                                    }
                                }
                            }
                            // "使用...继续" 图标按钮
                            for (const el of allClickable) {
                                const text = (el.innerText || '').replace(/\s+/g, '').trim();
                                if (/使用.*继续/.test(text)) {
                                    const r = getRect(el, 'icon-btn: ' + (el.innerText||'').trim().substring(0,20));
                                    if (r) return r;
                                }
                            }
                            return null;
                        })()
                    """)
                    if btn_rect and isinstance(btn_rect, (list, tuple)) and len(btn_rect) >= 4:
                        x = self._to_float(btn_rect[0])
                        y = self._to_float(btn_rect[1])
                        w = self._to_float(btn_rect[2])
                        h = self._to_float(btn_rect[3])
                        desc = btn_rect[4] if len(btn_rect) > 4 else '?'
                        if isinstance(desc, dict):
                            desc = desc.get('value', desc)
                        click_x = x + w / 2
                        click_y = y + h / 2
                        logger.info(
                            f"[{self.account_name}] 注册页找到 OAuth: {desc}, "
                            f"物理点击 ({click_x:.0f}, {click_y:.0f})"
                        )
                        await tab.mouse_click(click_x, click_y)
                        await self._save_debug_screenshot(tab, "register_oauth_clicked")
                        clicked = True
                        break
                except Exception as e:
                    logger.debug(f"[{self.account_name}] 注册页查找 OAuth 按钮出错: {e}")
                await asyncio.sleep(1)

            if not clicked:
                logger.warning(f"[{self.account_name}] 登录页和注册页均未找到 LinuxDO OAuth 按钮")
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
                    t_url = t.target.url if hasattr(t, "target") else ""
                    if "connect.linux.do" in t_url or "authorize" in t_url.lower():
                        logger.info(f"[{self.account_name}] 找到授权标签页: {t_url}")
                        await t.bring_to_front()
                        tab = t
                        await asyncio.sleep(1)
                        break

            current_url = tab.target.url if hasattr(tab, "target") else ""

            # 如果已经在目标站点且不是登录页，获取 session
            if self.provider.domain in current_url and "login" not in current_url.lower():
                logger.success(f"[{self.account_name}] OAuth 登录成功！")
                return await self._extract_session_from_browser(tab)

            # 如果在授权页面，点击允许
            if "linux.do" in current_url and ("authorize" in current_url.lower() or "oauth" in current_url.lower()):
                logger.info(f"[{self.account_name}] 检测到授权页面，尝试点击允许...")
                await self._save_debug_screenshot(tab, "oauth_authorize_page")
                await asyncio.sleep(2)

                # Debug: 打印页面上所有按钮
                if self._debug:
                    try:
                        buttons_info = await tab.evaluate("""
                            (function() {
                                const results = [];
                                const buttons = document.querySelectorAll('button, a, input[type="submit"], [role="button"]');
                                for (const btn of buttons) {
                                    results.push({
                                        tag: btn.tagName,
                                        text: (btn.innerText || btn.value || '').trim().substring(0, 30),
                                        class: btn.className.substring(0, 50)
                                    });
                                }
                                return JSON.stringify(results);
                            })()
                        """)
                        logger.debug(f"[{self.account_name}] 授权页面按钮: {buttons_info}")
                    except Exception as e:
                        logger.debug(f"[{self.account_name}] 获取按钮信息失败: {e}")

                try:
                    # 三种方式尝试点击"允许"按钮
                    # 策略1: JS click + 直接导航 href（最可靠）
                    # 策略2: mouse_click 物理点击（备选）
                    click_result = await tab.evaluate(r"""
                        (function() {
                            const elements = document.querySelectorAll('a, button, input[type="submit"], [role="button"]');

                            // 查找"允许"按钮
                            let target = null;
                            for (const el of elements) {
                                const text = (el.innerText || el.value || el.textContent || '').trim();
                                if (/允许/.test(text) || /authorize/i.test(text) ||
                                    /allow/i.test(text) || /accept/i.test(text)) {
                                    target = el;
                                    break;
                                }
                            }

                            // 兜底：红色按钮（LinuxDO 授权页的允许按钮是红色）
                            if (!target) {
                                for (const el of elements) {
                                    const cls = el.className || '';
                                    if (cls.includes('btn-danger') || cls.includes('bg-red')) {
                                        target = el;
                                        break;
                                    }
                                }
                            }

                            if (!target) return null;

                            const text = (target.innerText || '').trim().substring(0, 10);

                            // 尝试1: 如果是 <a> 标签且有 href，直接导航（最可靠）
                            if (target.tagName === 'A' && target.href && !target.href.startsWith('javascript:')) {
                                const href = target.href;
                                window.location.href = href;
                                return ['navigated', text, href.substring(0, 80)];
                            }

                            // 尝试2: JS click
                            target.click();

                            // 尝试3: 如果是 form 内的按钮，提交表单
                            const form = target.closest('form');
                            if (form) {
                                form.submit();
                                return ['form-submitted', text, ''];
                            }

                            return ['clicked', text, ''];
                        })()
                    """)
                    if click_result and isinstance(click_result, (list, tuple)):
                        action = click_result[0] if len(click_result) > 0 else '?'
                        text = click_result[1] if len(click_result) > 1 else '?'
                        detail = click_result[2] if len(click_result) > 2 else ''
                        # 处理 nodriver 的包装格式
                        if isinstance(action, dict):
                            action = action.get('value', action)
                        if isinstance(text, dict):
                            text = text.get('value', text)
                        if isinstance(detail, dict):
                            detail = detail.get('value', detail)
                        logger.info(
                            f"[{self.account_name}] 授权按钮操作: {action} "
                            f"(按钮: {text}) {detail}"
                        )
                        await asyncio.sleep(3)
                    else:
                        logger.warning(f"[{self.account_name}] 未找到允许按钮")
                except Exception as e:
                    logger.warning(f"[{self.account_name}] 点击允许按钮失败: {e}")

            # 检查所有标签页是否有已登录的
            for t in browser.tabs:
                t_url = t.target.url if hasattr(t, "target") else ""
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

        # 提取 provider 域名用于过滤 cookie
        provider_domain = self.provider.domain.replace("https://", "").replace("http://", "")

        try:
            import nodriver.cdp.network as cdp_network

            # 先确保在 provider 域名上（触发 session cookie 设置）
            current_url = tab.target.url if hasattr(tab, "target") else ""
            logger.info(f"[{self.account_name}] 当前 URL: {current_url}")
            if provider_domain not in current_url:
                logger.info(f"[{self.account_name}] 导航到站点主页确保 session 设置...")
                await tab.get(self.provider.domain)
                await asyncio.sleep(3)

            # 先尝试从 JS document.cookie 获取（最直接）
            try:
                js_cookies = await tab.evaluate("document.cookie")
                if js_cookies and isinstance(js_cookies, dict):
                    js_cookies = js_cookies.get('value', '')
                if js_cookies:
                    logger.info(f"[{self.account_name}] JS document.cookie: {str(js_cookies)[:200]}")
                    # 解析 document.cookie 字符串
                    for pair in str(js_cookies).split(";"):
                        pair = pair.strip()
                        if "=" in pair:
                            name, value = pair.split("=", 1)
                            if name.strip() == "session" and value.strip():
                                session_cookie = value.strip()
                                logger.info(f"[{self.account_name}] 从 JS 获取到 session: {session_cookie[:30]}...")
                else:
                    logger.info(f"[{self.account_name}] JS document.cookie 为空")
            except Exception as e:
                logger.debug(f"[{self.account_name}] JS document.cookie 读取失败: {e}")

            # 重试获取 session（有些站点 cookie 设置有延迟）
            for attempt in range(3):
                all_cookies = await tab.send(cdp_network.get_all_cookies())

                # 打印 cookie 摘要（帮助定位 session 提取问题）
                provider_cookies = [
                    f"{c.name}={c.value[:20]}...(d={c.domain})"
                    for c in all_cookies if provider_domain in (c.domain or "") or (c.domain or "").lstrip(".") in provider_domain
                ]
                all_domains = list({c.domain for c in all_cookies})
                logger.info(f"[{self.account_name}] cookies 域名: {all_domains}")
                logger.info(f"[{self.account_name}] {provider_domain} 匹配cookies: {provider_cookies}")

                # 按 provider 域名过滤，查找 session cookie
                # NewAPI 站点的 session cookie 可能叫不同的名字
                session_cookie_names = ["session", "_session", "connect.sid", "token", "auth_token"]
                for cookie in all_cookies:
                    cookie_domain = cookie.domain or ""
                    # 匹配 provider 域名（包括子域名）
                    if provider_domain not in cookie_domain and cookie_domain.lstrip(".") not in provider_domain:
                        continue
                    if cookie.name in session_cookie_names and cookie.value:
                        session_cookie = cookie.value
                        logger.info(f"[{self.account_name}] 获取到 session ({cookie.name}): {session_cookie[:30]}...")
                    elif cookie.name == self.provider.api_user_key and cookie.value:
                        api_user = cookie.value
                        logger.info(f"[{self.account_name}] 获取到 api_user (cookie): {api_user}")

                if session_cookie:
                    break

                # 没找到 session，等一下再试
                if attempt < 2:
                    logger.debug(f"[{self.account_name}] 未找到 session cookie，等待重试... ({attempt + 1}/3)")
                    await asyncio.sleep(2)

            # 仍然没有 session？尝试直接触发 OAuth 登录（适用于 Wong 等注册后需要额外登录的站点）
            if not session_cookie:
                logger.info(f"[{self.account_name}] 未找到 session，尝试直接触发 OAuth 登录...")
                # 常见的 NewAPI OAuth 登录路径
                oauth_paths = ["/oauth/linuxdo", "/api/oauth/linuxdo", "/auth/linuxdo"]
                for oauth_path in oauth_paths:
                    try:
                        oauth_url = f"{self.provider.domain}{oauth_path}"
                        logger.info(f"[{self.account_name}] 尝试直接访问: {oauth_url}")
                        await tab.get(oauth_url)
                        await asyncio.sleep(5)

                        # 检查是否到了 LinuxDO 授权页（自动同意）
                        current_url = tab.target.url if hasattr(tab, "target") else ""
                        if "linux.do" in current_url and "authorize" in current_url.lower():
                            # 点击允许
                            await tab.evaluate(r"""
                                (function() {
                                    const links = document.querySelectorAll('a');
                                    for (const a of links) {
                                        if (/允许/.test(a.innerText) && a.href) {
                                            window.location.href = a.href;
                                            return true;
                                        }
                                    }
                                    return false;
                                })()
                            """)
                            await asyncio.sleep(5)

                        # 再次检查 session cookie
                        all_cookies = await tab.send(cdp_network.get_all_cookies())
                        for cookie in all_cookies:
                            cookie_domain = cookie.domain or ""
                            if provider_domain not in cookie_domain and cookie_domain.lstrip(".") not in provider_domain:
                                continue
                            if cookie.name == "session" and cookie.value:
                                session_cookie = cookie.value
                                logger.success(f"[{self.account_name}] 直接 OAuth 登录获取到 session: {session_cookie[:30]}...")
                                break

                        if session_cookie:
                            break
                    except Exception as e:
                        logger.debug(f"[{self.account_name}] 尝试 {oauth_path} 失败: {e}")

            # 如果没有从 cookie 获取到 api_user，尝试从 localStorage 获取
            if not api_user:
                # NewAPI 将用户信息存储在 localStorage 的 'user' 键中
                api_user = await tab.evaluate("""
                    (function() {
                        // 尝试从 localStorage 的 user 对象获取 id
                        try {
                            const userStr = localStorage.getItem('user');
                            if (userStr) {
                                const user = JSON.parse(userStr);
                                if (user && user.id) {
                                    return String(user.id);
                                }
                            }
                        } catch (e) {}

                        // 尝试其他可能的键名
                        const keys = ['user_id', 'userId', 'new-api-user', 'api_user'];
                        for (const key of keys) {
                            const val = localStorage.getItem(key);
                            if (val) return val;
                        }

                        return null;
                    })()
                """)
                if api_user:
                    logger.info(f"[{self.account_name}] 从 localStorage 获取到 api_user: {api_user}")

            # 如果还是没有，尝试调用 API 获取用户信息
            if not api_user and session_cookie:
                logger.info(f"[{self.account_name}] 尝试通过 API 获取用户信息...")
                try:
                    user_info = await tab.evaluate(f"""
                        (async function() {{
                            try {{
                                const resp = await fetch('{self.provider.domain}/api/user/self', {{
                                    credentials: 'include'
                                }});
                                const data = await resp.json();
                                if (data.success && data.data && data.data.id) {{
                                    return String(data.data.id);
                                }}
                            }} catch (e) {{}}
                            return null;
                        }})()
                    """)
                    if user_info:
                        api_user = user_info
                        logger.info(f"[{self.account_name}] 从 API 获取到 api_user: {api_user}")
                except Exception as e:
                    logger.debug(f"[{self.account_name}] API 获取用户信息失败: {e}")

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
                    # 401/403/过期 都应该触发 OAuth 回退
                    elif "过期" not in message and "401" not in message and "403" not in message:
                        return CheckinResult(
                            platform=f"NewAPI ({self.provider_name})",
                            account=self.account_name,
                            status=CheckinStatus.FAILED,
                            message=message,
                            details=details,
                        )
                    logger.warning(f"[{self.account_name}] Cookie 已过期或被拦截({message})，尝试 OAuth 登录...")

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
            # 存储完整凭据，供 manager 提取并缓存（下次可直接用 Cookie+API）
            details["_cached_session"] = session_cookie
            details["_cached_api_user"] = api_user

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
