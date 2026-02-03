#!/usr/bin/env python3
"""
平台管理器

协调所有平台的签到任务，汇总结果并发送通知。

简化版：
- linuxdo: 浏览 LinuxDO 帖子
- newapi: 所有 NewAPI 架构站点的签到（使用 Cookie + API）
- 支持 401/403 失败后自动使用浏览器 OAuth 重试
"""

import ssl
import tempfile

import httpx
from loguru import logger

from platforms.base import CheckinResult, CheckinStatus
from platforms.linuxdo import LinuxDOAdapter
from utils.config import DEFAULT_PROVIDERS, AppConfig
from utils.notify import NotificationManager


def _create_ssl_context() -> ssl.SSLContext:
    """创建兼容旧服务器的 SSL 上下文"""
    ctx = ssl.create_default_context()
    ctx.set_ciphers('DEFAULT')
    ctx.options |= 0x4  # ssl.OP_LEGACY_SERVER_CONNECT
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class PlatformManager:
    """平台管理器"""

    def __init__(self, config: AppConfig):
        self.config = config
        self.notify = NotificationManager()
        self.results: list[CheckinResult] = []
        # 缓存 LinuxDO 账户，用于浏览器回退登录
        self._linuxdo_accounts: list[dict] = []
        self._load_linuxdo_accounts()

    def _load_linuxdo_accounts(self) -> None:
        """加载 LinuxDO 账户用于浏览器回退登录（不用于浏览帖子）"""
        # 从配置中获取 LinuxDO 账户，仅用于 OAuth 登录
        if self.config.linuxdo_accounts:
            for acc in self.config.linuxdo_accounts:
                self._linuxdo_accounts.append({
                    "username": acc.username,
                    "password": acc.password,
                    "name": acc.name,
                })
        if self._linuxdo_accounts:
            logger.info(f"已加载 {len(self._linuxdo_accounts)} 个 LinuxDO 账户用于浏览器回退登录")

    async def run_all(self) -> list[CheckinResult]:
        """运行所有平台签到"""
        self.results = []

        # LinuxDO 浏览帖子
        linuxdo_results = await self._run_all_linuxdo()
        self.results.extend(linuxdo_results)

        # NewAPI 站点签到
        newapi_results = await self._run_all_newapi()
        self.results.extend(newapi_results)

        return self.results

    async def run_platform(self, platform: str) -> list[CheckinResult]:
        """运行指定平台签到"""
        self.results = []
        platform_lower = platform.lower()

        if platform_lower == "linuxdo":
            results = await self._run_all_linuxdo()
            self.results.extend(results)
        elif platform_lower == "newapi":
            results = await self._run_all_newapi()
            self.results.extend(results)
        else:
            raise ValueError(f"未知平台: {platform}")

        return self.results

    async def _run_all_linuxdo(self) -> list[CheckinResult]:
        """运行 LinuxDO 浏览帖子"""
        if not self.config.linuxdo_accounts:
            return []

        results = []
        for i, account in enumerate(self.config.linuxdo_accounts):
            if not account.browse_linuxdo:
                logger.info(f"[{account.get_display_name(i)}] 跳过浏览帖子")
                continue

            logger.info(f"开始执行 LinuxDO 浏览: {account.get_display_name(i)}")

            # 从 level 计算浏览数量：L1=多看(10个), L2=一般(7个), L3=快速(5个)
            # 但如果用户指定了 browse_count，优先使用用户的设置
            level = getattr(account, 'level', 2) if hasattr(account, 'level') else 2

            adapter = LinuxDOAdapter(
                username=account.username,
                password=account.password,
                browse_count=account.browse_count,
                account_name=account.get_display_name(i),
                level=level,
            )

            try:
                result = await adapter.run()
                results.append(result)
            except Exception as e:
                logger.error(f"LinuxDO 浏览异常: {e}")
                results.append(CheckinResult(
                    platform="LinuxDO",
                    account=account.get_display_name(i),
                    status=CheckinStatus.FAILED,
                    message=f"浏览异常: {str(e)}",
                ))

        return results

    async def _run_all_newapi(self) -> list[CheckinResult]:
        """运行所有 NewAPI 站点签到（支持 Cookie + API，失败后浏览器回退）"""
        if not self.config.anyrouter_accounts:
            return []

        results = []
        # 记录需要浏览器回退的账户
        failed_accounts = []

        for i, account in enumerate(self.config.anyrouter_accounts):
            account_name = account.get_display_name(i)
            provider_name = account.provider

            # 获取 provider 配置
            provider = self.config.providers.get(provider_name)
            if not provider:
                # 尝试从默认配置获取
                if provider_name in DEFAULT_PROVIDERS:
                    from utils.config import ProviderConfig
                    provider = ProviderConfig.from_dict(provider_name, DEFAULT_PROVIDERS[provider_name])
                else:
                    logger.warning(f"[{account_name}] Provider '{provider_name}' 未找到，跳过")
                    results.append(CheckinResult(
                        platform=f"NewAPI ({provider_name})",
                        account=account_name,
                        status=CheckinStatus.SKIPPED,
                        message=f"Provider '{provider_name}' 未配置",
                    ))
                    continue

            logger.info(f"开始签到: {account_name} ({provider_name})")

            # 检查是否需要直接使用浏览器 OAuth（某些站点有 Cloudflare 保护）
            if provider.bypass_method == "browser_oauth":
                logger.info(f"[{account_name}] 站点需要浏览器 OAuth 登录")
                failed_accounts.append({
                    "account": account,
                    "provider": provider,
                    "account_name": account_name,
                    "original_result": None,
                })
                continue

            try:
                result = await self._checkin_newapi(account, provider, account_name)

                # 检查是否需要浏览器回退（401/403 错误）
                if result.status == CheckinStatus.FAILED:
                    msg = result.message or ""
                    if "401" in msg or "403" in msg or "过期" in msg:
                        logger.warning(f"[{account_name}] Cookie 失效，标记为需要浏览器回退")
                        failed_accounts.append({
                            "account": account,
                            "provider": provider,
                            "account_name": account_name,
                            "original_result": result,
                        })
                        continue  # 先不添加结果，等浏览器回退后再添加

                results.append(result)
            except Exception as e:
                logger.error(f"[{account_name}] 签到异常: {e}")
                results.append(CheckinResult(
                    platform=f"NewAPI ({provider_name})",
                    account=account_name,
                    status=CheckinStatus.FAILED,
                    message=f"签到异常: {str(e)}",
                ))

        # 处理需要浏览器回退的账户
        if failed_accounts and self._linuxdo_accounts:
            logger.info(f"开始浏览器回退登录，共 {len(failed_accounts)} 个失败账户")
            browser_results = await self._browser_fallback_checkin(failed_accounts)
            results.extend(browser_results)
        elif failed_accounts:
            # 没有 LinuxDO 账户，直接返回失败结果
            logger.warning("没有配置 LinuxDO 账户，无法进行浏览器回退登录")
            for item in failed_accounts:
                original_result = item.get("original_result")
                if original_result:
                    results.append(original_result)
                else:
                    # 对于需要浏览器 OAuth 但没有 LinuxDO 账户的情况
                    results.append(CheckinResult(
                        platform=f"NewAPI ({item['provider'].name})",
                        account=item['account_name'],
                        status=CheckinStatus.FAILED,
                        message="需要浏览器 OAuth 登录但未配置 LinuxDO 账户",
                    ))

        return results

    async def _browser_fallback_checkin(self, failed_accounts: list[dict]) -> list[CheckinResult]:
        """使用浏览器 OAuth 登录进行回退签到"""
        from platforms.newapi_browser import browser_checkin_newapi

        results = []
        # 使用第一个 LinuxDO 账户进行登录
        linuxdo_account = self._linuxdo_accounts[0]
        linuxdo_username = linuxdo_account["username"]
        linuxdo_password = linuxdo_account["password"]

        logger.info(f"使用 LinuxDO 账户 [{linuxdo_account.get('name', linuxdo_username)}] 进行浏览器回退登录")

        for item in failed_accounts:
            account = item["account"]
            provider = item["provider"]
            account_name = item["account_name"]

            logger.info(f"[{account_name}] 尝试浏览器 OAuth 登录...")

            try:
                # 使用浏览器签到模块
                result = await browser_checkin_newapi(
                    provider_name=provider.name,
                    linuxdo_username=linuxdo_username,
                    linuxdo_password=linuxdo_password,
                    cookies=account.cookies if hasattr(account, 'cookies') else None,
                    api_user=account.api_user if hasattr(account, 'api_user') else None,
                    account_name=account_name,
                )

                if result.status == CheckinStatus.SUCCESS:
                    logger.success(f"[{account_name}] 浏览器回退签到成功！")
                else:
                    logger.error(f"[{account_name}] 浏览器回退签到失败: {result.message}")

                results.append(result)

            except Exception as e:
                logger.error(f"[{account_name}] 浏览器回退签到异常: {e}")
                # 返回原始失败结果或创建新的失败结果
                original_result = item.get("original_result")
                if original_result:
                    original_result.message = f"{original_result.message} (浏览器回退也失败: {e})"
                    results.append(original_result)
                else:
                    results.append(CheckinResult(
                        platform=f"NewAPI ({provider.name})",
                        account=account_name,
                        status=CheckinStatus.FAILED,
                        message=f"浏览器 OAuth 登录失败: {e}",
                    ))

        return results

    async def _checkin_newapi(self, account, provider, account_name: str) -> CheckinResult:
        """执行单个 NewAPI 站点签到"""
        # 提取 session cookie
        session_cookie = self._extract_session_cookie(account.cookies)
        if not session_cookie:
            return CheckinResult(
                platform=f"NewAPI ({provider.name})",
                account=account_name,
                status=CheckinStatus.FAILED,
                message="无效的 session cookie",
            )

        # 构建请求
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/138.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": provider.domain,
            "Origin": provider.domain,
            provider.api_user_key: account.api_user,
        }

        cookies = {"session": session_cookie}
        details = {}

        # 如果需要 WAF bypass，先获取 WAF cookies
        if provider.needs_waf_cookies():
            waf_cookies = await self._get_waf_cookies(provider, account_name)
            if waf_cookies:
                cookies.update(waf_cookies)
            else:
                logger.warning(f"[{account_name}] 无法获取 WAF cookies，尝试直接请求")

        ssl_ctx = _create_ssl_context()
        async with httpx.AsyncClient(timeout=30.0, verify=ssl_ctx) as client:
            # 1. 获取用户信息
            user_info_url = f"{provider.domain}{provider.user_info_path}"
            try:
                resp = await client.get(user_info_url, headers=headers, cookies=cookies)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("success"):
                        user_data = data.get("data", {})
                        quota = round(user_data.get("quota", 0) / 500000, 2)
                        used_quota = round(user_data.get("used_quota", 0) / 500000, 2)
                        details["balance"] = f"${quota}"
                        details["used"] = f"${used_quota}"
                        logger.info(f"[{account_name}] 余额: ${quota}, 已用: ${used_quota}")
            except Exception as e:
                logger.warning(f"[{account_name}] 获取用户信息失败: {e}")

            # 2. 执行签到（如果需要）
            if provider.needs_manual_check_in():
                checkin_url = f"{provider.domain}{provider.sign_in_path}"
                try:
                    resp = await client.post(checkin_url, headers=headers, cookies=cookies)
                    logger.debug(f"[{account_name}] 签到响应: {resp.status_code}")

                    if resp.status_code == 200:
                        try:
                            result = resp.json()
                            msg = result.get("message") or result.get("msg") or ""

                            # 检查各种成功标志
                            if result.get("success") or result.get("ret") == 1 or result.get("code") == 0:
                                msg = msg or "签到成功"
                                logger.success(f"[{account_name}] {msg}")
                                return CheckinResult(
                                    platform=f"NewAPI ({provider.name})",
                                    account=account_name,
                                    status=CheckinStatus.SUCCESS,
                                    message=msg,
                                    details=details if details else None,
                                )
                            # "今日已签到" 也视为成功（只是今天已经签过了）
                            elif "已签到" in msg or "已经签到" in msg:
                                logger.success(f"[{account_name}] {msg}")
                                return CheckinResult(
                                    platform=f"NewAPI ({provider.name})",
                                    account=account_name,
                                    status=CheckinStatus.SUCCESS,
                                    message=msg,
                                    details=details if details else None,
                                )
                            else:
                                error_msg = msg or "签到失败"
                                logger.warning(f"[{account_name}] {error_msg}")
                                return CheckinResult(
                                    platform=f"NewAPI ({provider.name})",
                                    account=account_name,
                                    status=CheckinStatus.FAILED,
                                    message=error_msg,
                                    details=details if details else None,
                                )
                        except Exception:
                            # 非 JSON 响应
                            if "success" in resp.text.lower():
                                logger.success(f"[{account_name}] 签到成功")
                                return CheckinResult(
                                    platform=f"NewAPI ({provider.name})",
                                    account=account_name,
                                    status=CheckinStatus.SUCCESS,
                                    message="签到成功",
                                    details=details if details else None,
                                )

                    logger.error(f"[{account_name}] 签到失败: HTTP {resp.status_code}")
                    return CheckinResult(
                        platform=f"NewAPI ({provider.name})",
                        account=account_name,
                        status=CheckinStatus.FAILED,
                        message=f"HTTP {resp.status_code}",
                        details=details if details else None,
                    )

                except Exception as e:
                    logger.error(f"[{account_name}] 签到请求异常: {e}")
                    return CheckinResult(
                        platform=f"NewAPI ({provider.name})",
                        account=account_name,
                        status=CheckinStatus.FAILED,
                        message=f"请求异常: {str(e)}",
                        details=details if details else None,
                    )
            else:
                # 不需要手动签到（访问用户信息即自动签到）
                logger.success(f"[{account_name}] 签到成功（自动触发）")
                return CheckinResult(
                    platform=f"NewAPI ({provider.name})",
                    account=account_name,
                    status=CheckinStatus.SUCCESS,
                    message="签到成功（自动触发）",
                    details=details if details else None,
                )

    def _extract_session_cookie(self, cookies) -> str:
        """从 cookies 中提取 session 值"""
        if isinstance(cookies, dict):
            return cookies.get("session", "")
        if isinstance(cookies, str):
            return cookies
        return ""

    async def _get_waf_cookies(self, provider, account_name: str) -> dict | None:
        """使用 Playwright 浏览器获取 WAF cookies（参考 anyrouter-check-in 实现）"""
        # 优先使用 patchright，回退到 playwright
        try:
            from patchright.async_api import async_playwright
            logger.debug(f"[{account_name}] 使用 Patchright 浏览器")
        except ImportError:
            try:
                from playwright.async_api import async_playwright
                logger.debug(f"[{account_name}] 使用 Playwright 浏览器")
            except ImportError:
                logger.warning(f"[{account_name}] Patchright/Playwright 未安装，跳过 WAF bypass")
                return None

        logger.info(f"[{account_name}] 启动浏览器获取 WAF cookies...")
        required_cookies = provider.waf_cookie_names or []
        login_url = f"{provider.domain}{provider.login_path}"

        # 创建临时目录，不使用 with 语句以避免 Windows 文件锁定问题
        temp_dir = tempfile.mkdtemp()
        waf_cookies = {}

        try:
            async with async_playwright() as p:
                # 参考 anyrouter-check-in 的配置：headless=False 更不容易被检测
                context = await p.chromium.launch_persistent_context(
                    user_data_dir=temp_dir,
                    headless=False,  # 非 headless 模式更不容易被 WAF 检测
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
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
                logger.debug(f"[{account_name}] 访问登录页面: {login_url}")

                # 先访问页面，等待 Cloudflare 验证
                await page.goto(login_url, wait_until="domcontentloaded", timeout=60000)

                # 等待 Cloudflare 验证完成（最多等待 30 秒）
                for _ in range(30):
                    title = await page.title()
                    if "just a moment" not in title.lower() and "请稍候" not in title:
                        break
                    await page.wait_for_timeout(1000)

                # 等待页面完全加载
                import contextlib
                with contextlib.suppress(Exception):
                    await page.wait_for_load_state("networkidle", timeout=10000)

                # 获取 cookies
                cookies = await page.context.cookies()
                for cookie in cookies:
                    cookie_name = cookie.get("name")
                    cookie_value = cookie.get("value")
                    if cookie_name in required_cookies and cookie_value:
                        waf_cookies[cookie_name] = cookie_value

                await context.close()

        except Exception as e:
            logger.error(f"[{account_name}] 获取 WAF cookies 失败: {e}")
        finally:
            # 尝试清理临时目录，忽略 Windows 文件锁定错误
            try:
                import shutil
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass

        # 检查是否获取到所有需要的 cookies
        missing_cookies = [c for c in required_cookies if c not in waf_cookies]
        if missing_cookies:
            logger.warning(f"[{account_name}] 缺少 WAF cookies: {missing_cookies}")

        if waf_cookies:
            logger.success(f"[{account_name}] 获取到 {len(waf_cookies)} 个 WAF cookies: {list(waf_cookies.keys())}")
            return waf_cookies
        else:
            logger.warning(f"[{account_name}] 未获取到任何 WAF cookies")
            return None

    def send_summary_notification(self, force: bool = False) -> None:  # noqa: ARG002
        """发送签到汇总通知"""
        if not self.results:
            logger.info("没有签到结果，跳过通知")
            return

        results_dicts = [r.to_dict() for r in self.results]
        title, text_content, html_content = NotificationManager.format_summary_message(results_dicts)

        with self.notify:
            self.notify.push_message(title, html_content, msg_type="html")

    def get_exit_code(self) -> int:
        """获取退出码"""
        if not self.results:
            return 1
        success_count = sum(1 for r in self.results if r.is_success)
        return 0 if success_count > 0 else 1

    @property
    def success_count(self) -> int:
        return sum(1 for r in self.results if r.is_success)

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.results if r.status == CheckinStatus.FAILED)

    @property
    def skipped_count(self) -> int:
        return sum(1 for r in self.results if r.status == CheckinStatus.SKIPPED)

    @property
    def total_count(self) -> int:
        return len(self.results)
