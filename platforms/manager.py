#!/usr/bin/env python3
"""
平台管理器

协调所有平台的签到任务，汇总结果并发送通知。

简化版：
- linuxdo: 浏览 LinuxDO 帖子
- newapi: 所有 NewAPI 架构站点的签到（使用 Cookie + API）
- 支持 401/403 失败后自动使用浏览器 OAuth 重试
"""

import asyncio
import json
import os
import ssl
import tempfile
from urllib.parse import urlparse

import httpx
from loguru import logger

from platforms.base import CheckinResult, CheckinStatus
from platforms.linuxdo import LinuxDOAdapter
from utils.config import DEFAULT_PROVIDERS, AnyRouterAccount, AppConfig, ProviderConfig
from utils.cookie_cache import CookieCache
from utils.notify import NotificationManager


def _create_ssl_context() -> ssl.SSLContext:
    """创建兼容旧服务器的 SSL 上下文"""
    ctx = ssl.create_default_context()
    ctx.set_ciphers('DEFAULT@SECLEVEL=1')
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
        # Cookie 缓存：OAuth 成功后自动保存，下次优先使用 Cookie+API（更快）
        self._cookie_cache = CookieCache()
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

    @staticmethod
    def _unwrap_eval_value(value):
        """解包 nodriver evaluate 可能返回的 {'value': ...} 结构。"""
        if isinstance(value, dict) and "value" in value and len(value) <= 3:
            return PlatformManager._unwrap_eval_value(value.get("value"))
        return value

    @staticmethod
    def _normalize_domain(domain: str) -> str:
        """标准化域名 URL，统一为 https://host 形式（无尾斜杠）。"""
        if not domain:
            return ""
        normalized = domain.strip()
        if not normalized:
            return ""
        if not normalized.startswith(("http://", "https://")):
            normalized = f"https://{normalized}"
        return normalized.rstrip("/")

    @staticmethod
    def _make_ldoh_provider_name(domain: str, existing_names: set[str]) -> str:
        """为 LDOH 动态站点生成稳定 provider 名称。"""
        host = urlparse(domain).netloc.lower().split(":")[0]
        base = host.replace(".", "_").replace("-", "_")
        if not base:
            base = "site"
        if not base[0].isalpha():
            base = f"site_{base}"
        candidate = f"ldoh_{base}"
        idx = 2
        while candidate in existing_names:
            candidate = f"ldoh_{base}_{idx}"
            idx += 1
        return candidate

    def _register_runtime_provider(self, provider_name: str, provider: ProviderConfig) -> None:
        """运行时注册 provider，确保浏览器 OAuth 回退可直接复用。"""
        self.config.providers[provider_name] = provider
        config_data = {
            "domain": provider.domain,
            "login_path": provider.login_path,
            "sign_in_path": provider.sign_in_path,
            "user_info_path": provider.user_info_path,
            "api_user_key": provider.api_user_key,
        }
        if provider.bypass_method:
            config_data["bypass_method"] = provider.bypass_method
        if provider.waf_cookie_names:
            config_data["waf_cookie_names"] = provider.waf_cookie_names
        if provider.oauth_path:
            config_data["oauth_path"] = provider.oauth_path
        DEFAULT_PROVIDERS[provider_name] = config_data

    def _get_local_auto_providers(self) -> dict[str, ProviderConfig]:
        """获取自动模式本地兜底站点列表（跳过特殊站点）。"""
        providers_to_test: dict[str, ProviderConfig] = {}

        source_providers = self.config.providers
        if not source_providers:
            source_providers = {
                name: ProviderConfig.from_dict(name, config_data)
                for name, config_data in DEFAULT_PROVIDERS.items()
            }

        for name, provider in source_providers.items():
            provider_obj = provider
            if not isinstance(provider_obj, ProviderConfig):
                try:
                    provider_obj = ProviderConfig.from_dict(name, provider_obj)
                except Exception as e:
                    logger.warning(f"跳过无效 provider 配置 {name}: {e}")
                    continue

            bypass = provider_obj.bypass_method
            if bypass in ("waf_cookies", "manual_cookie_only"):
                logger.debug(f"跳过特殊站点: {name} ({bypass})")
                continue
            if not provider_obj.domain:
                logger.debug(f"跳过空域名站点: {name}")
                continue
            providers_to_test[name] = provider_obj

        return providers_to_test

    @staticmethod
    def _force_nodriver_headed_for_oauth() -> None:
        """自动 OAuth 固定使用 nodriver + 非 headless，避免环境变量误配。"""
        current_engine = (os.environ.get("BROWSER_ENGINE") or "").lower()
        if current_engine != "nodriver":
            logger.warning(
                f"自动 OAuth 强制使用 nodriver（当前 BROWSER_ENGINE={current_engine or '未设置'}）"
            )
        if (os.environ.get("BROWSER_HEADLESS") or "").lower() == "true":
            logger.warning("自动 OAuth 强制使用有头模式（忽略 BROWSER_HEADLESS=true）")

        os.environ["BROWSER_ENGINE"] = "nodriver"
        os.environ["BROWSER_HEADLESS"] = "false"

    @staticmethod
    def _env_int(name: str, default: int, min_value: int = 0) -> int:
        """读取整型环境变量（非法值回退默认值）。"""
        try:
            value = int(os.getenv(name, str(default)))
            if value < min_value:
                return default
            return value
        except Exception:
            return default

    @staticmethod
    def _env_float(name: str, default: float, min_value: float = 0.0) -> float:
        """读取浮点环境变量（非法值回退默认值）。"""
        try:
            value = float(os.getenv(name, str(default)))
            if value < min_value:
                return default
            return value
        except Exception:
            return default

    @staticmethod
    def _is_retryable_network_message(message: str) -> bool:
        """根据错误消息判断是否属于可重试网络错误。"""
        if not message:
            return False
        msg = message.lower()
        network_signatures = (
            "winerror 1225",
            "connection refused",
            "connecterror",
            "connect timeout",
            "read timeout",
            "timed out",
            "network is unreachable",
            "name or service not known",
            "temporary failure in name resolution",
            "remote host closed",
            "connection reset",
            "connection aborted",
        )
        return any(sig in msg for sig in network_signatures)

    @classmethod
    def _is_retryable_network_error(cls, err: Exception) -> bool:
        """判断异常是否属于可重试网络错误。"""
        if isinstance(
            err,
            (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.ReadTimeout,
                httpx.WriteTimeout,
                httpx.TimeoutException,
                ConnectionError,
                TimeoutError,
                OSError,
            ),
        ):
            return cls._is_retryable_network_message(str(err))
        return cls._is_retryable_network_message(str(err))

    async def _auto_approve_linuxdo_oauth(self, tab) -> bool:
        """在 LinuxDO OAuth 授权页自动点击“允许/Authorize”按钮。"""
        try:
            current_url = getattr(tab.target, "url", "") or ""
            if "linux.do" not in current_url.lower() or "authorize" not in current_url.lower():
                return False

            logger.info(f"LDOH: 检测到 LinuxDO 授权页，尝试自动同意: {current_url}")

            # 多策略点击：文本匹配 -> submit 按钮 -> 主按钮类
            click_result = await tab.evaluate(
                r"""
                (function() {
                    function clickTarget(el) {
                        if (!el) return false;
                        try { el.scrollIntoView({block: 'center'}); } catch (e) {}
                        try { el.focus(); } catch (e) {}
                        try { el.click(); return true; } catch (e) {}
                        try {
                            const ev = new MouseEvent('click', { bubbles: true, cancelable: true, view: window });
                            el.dispatchEvent(ev);
                            return true;
                        } catch (e) {}
                        return false;
                    }

                    const all = Array.from(document.querySelectorAll('button, a, input[type="submit"], [role="button"]'));
                    const byText = all.find((el) => {
                        const text = (el.innerText || el.value || el.textContent || '').trim().toLowerCase();
                        return text.includes('允许') || text.includes('同意') || text.includes('authorize') || text.includes('allow');
                    });
                    if (clickTarget(byText)) return 'text';

                    const submitBtn = document.querySelector('button[type="submit"], input[type="submit"]');
                    if (clickTarget(submitBtn)) return 'submit';

                    const primaryBtn = document.querySelector('.btn-primary, .btn.btn-primary');
                    if (clickTarget(primaryBtn)) return 'primary';

                    const form = document.querySelector('form[action*="oauth"], form[action*="authorize"]');
                    if (form) {
                        try { form.submit(); return 'form_submit'; } catch (e) {}
                    }
                    return '';
                })()
                """
            )

            strategy = self._unwrap_eval_value(click_result)
            if strategy:
                logger.info(f"LDOH: 已执行授权点击策略: {strategy}")
                await asyncio.sleep(2)
                new_url = getattr(tab.target, "url", "") or ""
                if new_url != current_url:
                    logger.success(f"LDOH: 授权页已跳转: {new_url}")
                    return True
                logger.warning("LDOH: 已点击授权按钮，但页面暂未跳转")
            else:
                logger.warning("LDOH: 授权页未找到可点击的“允许”按钮")
        except Exception as e:
            logger.warning(f"LDOH: 自动同意授权失败: {e}")

        return False

    async def _trigger_ldoh_login_button(self, tab) -> bool:
        """在 LDOH 登录页触发“使用 LinuxDo 登录”按钮。"""
        try:
            click_result = await tab.evaluate(
                r"""
                (function() {
                    const candidates = Array.from(
                        document.querySelectorAll('a, button, [role="button"], input[type="submit"]')
                    );
                    for (const el of candidates) {
                        const text = (el.innerText || el.value || el.textContent || '').trim().toLowerCase();
                        const href = (el.href || el.getAttribute('href') || '').toLowerCase();
                        const looksLikeLogin =
                            text.includes('linuxdo') ||
                            text.includes('linux do') ||
                            text.includes('使用 linuxdo 登录') ||
                            text.includes('login') ||
                            href.includes('linux.do') ||
                            href.includes('/auth/linuxdo') ||
                            href.includes('/oauth');
                        if (!looksLikeLogin) continue;
                        try { el.scrollIntoView({ block: 'center' }); } catch (e) {}
                        try { el.focus(); } catch (e) {}
                        try {
                            if (el.tagName === 'A' && el.href) {
                                window.location.href = el.href;
                            } else {
                                el.click();
                            }
                            return true;
                        } catch (e) {}
                    }
                    return false;
                })()
                """
            )
            clicked = bool(self._unwrap_eval_value(click_result))
            if clicked:
                logger.info("LDOH: 已触发登录按钮（使用 LinuxDo 登录）")
            return clicked
        except Exception as e:
            logger.warning(f"LDOH: 触发登录按钮失败: {e}")
            return False

    async def _try_sync_ldoh_providers(
        self, tab, local_providers: dict[str, ProviderConfig]
    ) -> dict[str, ProviderConfig] | None:
        """尝试从 LDOH 同步可签到站点；失败返回 None。"""
        ldoh_base_url = self._normalize_domain(os.getenv("LDOH_BASE_URL", "https://ldoh.105117.xyz"))
        ldoh_host = urlparse(ldoh_base_url).netloc.lower()
        login_url = f"{ldoh_base_url}/auth/login?returnTo=%2F"

        logger.info(f"尝试同步 LDOH 站点: {ldoh_base_url}")

        try:
            # 1) 进入登录页，触发 LinuxDo SSO（复用当前已登录 LinuxDo 会话）
            await tab.get(login_url)
            await asyncio.sleep(3)

            for i in range(90):
                current_url = getattr(tab.target, "url", "") or ""
                current_url_lower = current_url.lower()
                if i % 5 == 0:
                    logger.info(f"LDOH 状态机: step={i}, url={current_url}")

                if "linux.do" in current_url_lower and "authorize" in current_url_lower:
                    await self._auto_approve_linuxdo_oauth(tab)

                if ldoh_host in current_url_lower and "/auth/login" in current_url_lower:
                    # 登录页按钮点击可能偶发失效，间隔重试触发
                    if i % 3 == 0:
                        await self._trigger_ldoh_login_button(tab)

                if ldoh_host in current_url_lower and "/auth/login" not in current_url_lower:
                    break
                await asyncio.sleep(1)

            # 2) 在 LDOH 会话中获取站点列表
            await tab.get(ldoh_base_url)
            await asyncio.sleep(2)
            raw_payload = await tab.evaluate(
                r"""
                (async function() {
                    try {
                        const resp = await fetch('/api/sites', { credentials: 'include' });
                        const text = await resp.text();
                        let data = null;
                        try { data = JSON.parse(text); } catch (e) { data = null; }
                        const sites = Array.isArray(data && data.sites) ? data.sites : [];
                        const compact = sites.map(s => ({
                            name: s?.name || '',
                            apiBaseUrl: s?.apiBaseUrl || '',
                            supportsCheckin: !!s?.supportsCheckin,
                            checkinUrl: s?.checkinUrl || '',
                        }));
                        return JSON.stringify({
                            status: resp.status,
                            total: compact.length,
                            sites: compact
                        });
                    } catch (e) {
                        return JSON.stringify({
                            status: -1,
                            total: 0,
                            error: String(e),
                            sites: []
                        });
                    }
                })()
                """
            )
            payload_text = self._unwrap_eval_value(raw_payload)
            if not isinstance(payload_text, str):
                payload_text = str(payload_text or "")
            try:
                payload = json.loads(payload_text)
            except json.JSONDecodeError:
                logger.warning(
                    f"LDOH 站点同步返回非 JSON，前200字符: {payload_text[:200]!r}"
                )
                return None

            status = int(payload.get("status", -1))
            sites = payload.get("sites") if isinstance(payload.get("sites"), list) else []
            if status != 200 or not sites:
                logger.warning(f"LDOH 站点同步失败: status={status}, sites={len(sites)}")
                return None

            local_by_domain = {
                self._normalize_domain(provider.domain): (name, provider)
                for name, provider in local_providers.items()
            }

            dynamic_providers: dict[str, ProviderConfig] = {}
            existing_names = set(local_providers.keys())
            skipped_count = 0
            reused_count = 0
            new_count = 0

            for site in sites:
                if not isinstance(site, dict):
                    skipped_count += 1
                    continue
                if not site.get("supportsCheckin"):
                    skipped_count += 1
                    continue

                domain = self._normalize_domain(str(site.get("apiBaseUrl", "")))
                if not domain:
                    skipped_count += 1
                    continue

                if domain in local_by_domain:
                    provider_name, provider_obj = local_by_domain[domain]
                    dynamic_providers[provider_name] = provider_obj
                    reused_count += 1
                    continue

                provider_name = self._make_ldoh_provider_name(domain, existing_names)
                existing_names.add(provider_name)
                provider_obj = ProviderConfig(
                    name=provider_name,
                    domain=domain,
                    login_path="/login",
                    sign_in_path="/api/user/checkin",
                    user_info_path="/api/user/self",
                    api_user_key="new-api-user",
                )
                dynamic_providers[provider_name] = provider_obj
                new_count += 1

            if not dynamic_providers:
                logger.warning("LDOH 返回站点为空（可签到=0），回退本地兜底")
                return None

            # 运行时注册，保证后续共享 OAuth / 回退 OAuth 可直接使用
            for provider_name, provider in dynamic_providers.items():
                self._register_runtime_provider(provider_name, provider)

            logger.info(
                f"LDOH 同步成功: 可签到 {len(dynamic_providers)} 个 "
                f"(复用本地={reused_count}, 新增={new_count}, 跳过={skipped_count})"
            )
            return dynamic_providers
        except Exception as e:
            logger.warning(f"LDOH 同步异常: {e}")
            return None

    async def _probe_provider_availability(
        self, client: httpx.AsyncClient, provider_name: str, provider: ProviderConfig,
    ) -> tuple[bool, str]:
        """探测站点可用性：仅保留可访问站点，避免无效站点进入签到流程。"""
        status_ok = {200, 201, 202, 204, 301, 302, 307, 308, 400, 401, 403, 405, 429}
        targets = [
            f"{provider.domain}{provider.user_info_path}",
            provider.domain,
        ]
        last_reason = "unknown"

        for idx, url in enumerate(targets):
            try:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True)
                code = resp.status_code
                if code in status_ok:
                    return True, f"HTTP {code} ({'user_info' if idx == 0 else 'root'})"
                last_reason = f"HTTP {code} ({'user_info' if idx == 0 else 'root'})"
            except Exception as e:
                last_reason = f"{type(e).__name__}: {str(e)}"

        logger.debug(f"[{provider_name}] 可用性探测失败: {last_reason}")
        return False, last_reason

    async def _filter_available_providers(
        self, providers: dict[str, ProviderConfig]
    ) -> dict[str, ProviderConfig]:
        """过滤掉不可用站点，仅返回当前可用站点。"""
        if not providers:
            return providers

        connect_timeout = self._env_float("SITE_PROBE_CONNECT_TIMEOUT", 4.0, min_value=1.0)
        read_timeout = self._env_float("SITE_PROBE_READ_TIMEOUT", 6.0, min_value=1.0)
        probe_concurrency = self._env_int("SITE_PROBE_CONCURRENCY", 10, min_value=1)
        timeout = httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=read_timeout,
            pool=read_timeout,
        )
        limits = httpx.Limits(
            max_connections=max(10, probe_concurrency * 2),
            max_keepalive_connections=max(5, probe_concurrency),
        )
        semaphore = asyncio.Semaphore(probe_concurrency)

        logger.info(
            "站点可用性探测参数: "
            f"connect={connect_timeout}s, read={read_timeout}s, concurrency={probe_concurrency}"
        )

        available: dict[str, ProviderConfig] = {}
        unavailable: list[tuple[str, str]] = []

        async with httpx.AsyncClient(verify=False, timeout=timeout, limits=limits) as client:
            async def check_one(name: str, provider: ProviderConfig) -> None:
                async with semaphore:
                    ok, reason = await self._probe_provider_availability(client, name, provider)
                    if ok:
                        available[name] = provider
                        logger.debug(f"[{name}] 站点可用: {reason}")
                    else:
                        unavailable.append((name, reason))

            await asyncio.gather(*[
                check_one(name, provider)
                for name, provider in providers.items()
            ])

        if unavailable:
            preview = ", ".join(f"{name}({reason})" for name, reason in unavailable[:8])
            logger.warning(
                f"站点可用性过滤: 总计 {len(providers)}，可用 {len(available)}，跳过 {len(unavailable)}。"
                f"{' 示例: ' + preview if preview else ''}"
            )
        else:
            logger.info(f"站点可用性过滤: {len(providers)} 个全部可用")

        return available

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
        """运行所有 NewAPI 站点签到

        两种模式：
        1. 手动模式：NEWAPI_ACCOUNTS 已配置 → 按账号签到（Cookie+API，失败回退OAuth）
        2. 自动模式：NEWAPI_ACCOUNTS 未配置但有 LINUXDO_ACCOUNTS → 自动遍历所有站点 OAuth 签到
        """
        if self.config.anyrouter_accounts:
            return await self._run_newapi_with_accounts()
        elif self._linuxdo_accounts:
            logger.info("NEWAPI_ACCOUNTS 未配置，使用 LINUXDO_ACCOUNTS 自动发现并签到所有站点")
            return await self._run_newapi_auto_oauth()
        return []

    async def _run_newapi_auto_oauth(self) -> list[CheckinResult]:
        """自动模式：用 LinuxDO 账号遍历所有 NewAPI 站点，自动 OAuth 登录签到

        用户只需配置 LINUXDO_ACCOUNTS，系统自动：
        1. 优先从 LDOH 同步“可签到站点”
        2. 同步失败时回退到本地 DEFAULT/PROVIDERS 站点
        3. 优先使用缓存的 Cookie 签到（快速）
        4. 无缓存或 Cookie 过期时，自动使用浏览器 OAuth 获取新 Cookie
        5. 签到成功后缓存 Cookie，下次直接用
        """
        from platforms.newapi_browser import NewAPIBrowserCheckin
        from utils.browser import BrowserManager

        results = []

        # 使用第一个 LinuxDO 账号
        linuxdo_account = self._linuxdo_accounts[0]
        linuxdo_username = linuxdo_account["username"]
        linuxdo_password = linuxdo_account["password"]
        linuxdo_name = linuxdo_account.get("name", linuxdo_username)

        logger.info(f"自动模式: 使用 LinuxDO 账号 [{linuxdo_name}] 遍历站点")

        # 本地兜底站点（LDOH 同步失败时使用）
        providers_to_test = self._get_local_auto_providers()
        if not providers_to_test:
            logger.warning("未找到可用的本地站点配置，自动模式终止")
            return results
        logger.info(f"本地兜底站点数量: {len(providers_to_test)}")

        # 登录 LinuxDO 授权必须复用 GitHub Action 同款模式：nodriver + 有头
        self._force_nodriver_headed_for_oauth()

        # 共享浏览器会话：用于 LDOH 同步 + 批量 OAuth
        is_ci = bool(os.environ.get("CI")) or bool(os.environ.get("GITHUB_ACTIONS"))
        if is_ci and not bool(os.environ.get("DISPLAY")):
            logger.warning("CI 环境未检测到 DISPLAY，nodriver 将回退 headless，授权成功率可能下降")

        browser_mgr = BrowserManager(engine="nodriver", headless=False)
        tab = None
        linuxdo_logged_in = False

        try:
            await browser_mgr.start(max_retries=5 if is_ci else 3)
            tab = browser_mgr.page

            # 登录 LinuxDO 一次（后续站点复用）
            seed_provider_name = next(iter(providers_to_test))
            checker_for_login = NewAPIBrowserCheckin(
                provider_name=seed_provider_name,
                linuxdo_username=linuxdo_username,
                linuxdo_password=linuxdo_password,
                account_name="shared_login",
            )
            checker_for_login._browser_manager = browser_mgr
            checker_for_login._debug = False  # 共享会话禁用截图，避免拖慢

            for login_attempt in range(3):
                if login_attempt > 0:
                    logger.info(f"共享会话: LinuxDO 登录重试 {login_attempt + 1}/3...")
                    await tab.get("about:blank")
                    await asyncio.sleep(1)
                linuxdo_logged_in = await checker_for_login._login_linuxdo(tab)
                if linuxdo_logged_in:
                    break
                logger.warning(f"共享会话: LinuxDO 登录失败（第 {login_attempt + 1} 次）")

            if linuxdo_logged_in:
                synced_providers = await self._try_sync_ldoh_providers(tab, providers_to_test)
                if synced_providers:
                    providers_to_test = synced_providers
                else:
                    logger.warning("LDOH 同步失败，使用本地兜底站点继续")
            else:
                logger.warning("共享会话 LinuxDO 登录失败，后续改为逐站独立 OAuth")
        except Exception as e:
            logger.error(f"共享浏览器启动失败: {e}")
            logger.warning("无法连接 LDOH 或共享浏览器不可用，使用本地兜底站点继续")

        logger.info(f"本轮待处理站点: {len(providers_to_test)}")

        providers_to_test = await self._filter_available_providers(providers_to_test)
        if not providers_to_test:
            logger.warning("可用站点数为 0，跳过本轮 NewAPI 自动签到")
            try:
                logger.info("共享会话: 关闭浏览器")
                await browser_mgr.close()
            except Exception as e:
                logger.debug(f"关闭共享浏览器失败（可忽略）: {e}")
            return results

        # 统计需要浏览器 OAuth 的站点（无缓存或缓存失效）
        need_oauth = []
        for provider_name, provider in providers_to_test.items():
            account_name = f"{linuxdo_name}_{provider_name}"

            # 1. 优先尝试缓存的 Cookie
            cached = self._cookie_cache.get(provider_name, account_name)
            if cached:
                logger.info(f"[{account_name}] 发现缓存Cookie，尝试Cookie+API签到...")
                try:
                    cached_account = AnyRouterAccount(
                        cookies={"session": cached["session"]},
                        api_user=cached["api_user"],
                        provider=provider_name,
                        name=account_name,
                    )
                    result = await self._checkin_newapi(cached_account, provider, account_name)

                    if result.status == CheckinStatus.SUCCESS:
                        result.message = f"{result.message} (缓存Cookie)"
                        if result.details is None:
                            result.details = {}
                        result.details["login_method"] = "cached_cookie"
                        results.append(result)
                        logger.success(f"[{account_name}] 缓存Cookie签到成功！")
                        continue

                    # Cookie 过期，清除缓存，需要 OAuth
                    msg = result.message or ""
                    if "401" in msg or "403" in msg or "过期" in msg:
                        logger.warning(f"[{account_name}] 缓存Cookie已失效，需要重新OAuth")
                        self._cookie_cache.invalidate(provider_name, account_name)
                    else:
                        logger.warning(f"[{account_name}] 签到失败: {msg}")
                        results.append(result)
                        continue
                except Exception as e:
                    logger.warning(f"[{account_name}] 缓存Cookie签到异常: {e}")
                    self._cookie_cache.invalidate(provider_name, account_name)

            # 2. 无缓存或缓存失效，标记为需要 OAuth
            need_oauth.append({
                "provider": provider,
                "provider_name": provider_name,
                "account_name": account_name,
            })

        if not need_oauth:
            logger.info("所有站点均通过缓存Cookie完成，无需 OAuth")
            try:
                logger.info("共享会话: 关闭浏览器")
                await browser_mgr.close()
            except Exception as e:
                logger.debug(f"关闭共享浏览器失败（可忽略）: {e}")
            return results

        # 3. 优先共享会话 OAuth；失败再回退逐站独立浏览器
        site_timeout = 120  # 单站点超时：2 分钟
        if browser_mgr and linuxdo_logged_in and tab:
            logger.info(f"需要浏览器OAuth登录: {len(need_oauth)} 个站点（共享会话，每站限时 {site_timeout}s）")

            for idx, item in enumerate(need_oauth):
                provider = item["provider"]
                provider_name = item["provider_name"]
                account_name = item["account_name"]

                logger.info(f"[{idx+1}/{len(need_oauth)}] [{account_name}] OAuth 登录...")

                try:
                    result = await asyncio.wait_for(
                        self._oauth_single_site_shared(
                            tab, browser_mgr, provider, provider_name,
                            account_name, linuxdo_username, linuxdo_password,
                        ),
                        timeout=site_timeout,
                    )

                    if result.status == CheckinStatus.SUCCESS:
                        logger.success(f"[{account_name}] OAuth 签到成功！")
                        if result.details:
                            cached_session = result.details.pop("_cached_session", None)
                            cached_api_user = result.details.pop("_cached_api_user", None)
                            if cached_session and cached_api_user:
                                self._cookie_cache.save(
                                    provider_name, account_name, cached_session, cached_api_user
                                )
                                logger.success(f"[{account_name}] 新Cookie已缓存")
                    else:
                        logger.warning(f"[{account_name}] OAuth 签到失败: {result.message}")

                    results.append(result)

                except asyncio.TimeoutError:
                    logger.error(f"[{account_name}] 超时（>{site_timeout}s），跳过")
                    results.append(CheckinResult(
                        platform=f"NewAPI ({provider_name})",
                        account=account_name,
                        status=CheckinStatus.FAILED,
                        message=f"OAuth 超时（>{site_timeout}s）",
                    ))
                except Exception as e:
                    logger.error(f"[{account_name}] OAuth 异常: {e}")
                    results.append(CheckinResult(
                        platform=f"NewAPI ({provider_name})",
                        account=account_name,
                        status=CheckinStatus.FAILED,
                        message=f"OAuth 异常: {str(e)}",
                    ))
        else:
            logger.warning(f"共享会话不可用，回退为逐站独立浏览器 OAuth（{len(need_oauth)} 个站点）")
            await self._run_newapi_oauth_fallback(
                need_oauth, linuxdo_username, linuxdo_password, results
            )

        try:
            logger.info("共享会话: 关闭浏览器")
            await browser_mgr.close()
        except Exception as e:
            logger.debug(f"关闭共享浏览器失败（可忽略）: {e}")

        return results

    async def _oauth_single_site_shared(
        self, tab, browser_mgr, provider, provider_name: str,
        account_name: str, linuxdo_username: str, linuxdo_password: str,
    ) -> CheckinResult:
        """在共享浏览器会话中对单个站点执行 OAuth 登录+签到"""
        from platforms.newapi_browser import NewAPIBrowserCheckin

        # 创建 checker 实例（复用已有的浏览器，不重新登录 LinuxDO）
        checker = NewAPIBrowserCheckin(
            provider_name=provider_name,
            linuxdo_username=linuxdo_username,
            linuxdo_password=linuxdo_password,
            account_name=account_name,
        )
        checker._browser_manager = browser_mgr

        retry_count = self._env_int("OAUTH_NETWORK_RETRY_COUNT", 2, min_value=0)
        backoff_base = self._env_float("OAUTH_NETWORK_RETRY_BACKOFF", 2.0, min_value=0.5)
        attempt_total = retry_count + 1

        for attempt in range(attempt_total):
            try:
                # 直接在共享 tab 上做 OAuth（跳过 LinuxDO 登录，已经登录了）
                session_cookie, api_user = await checker._oauth_login_and_get_session(tab)

                if not session_cookie or not api_user:
                    return CheckinResult(
                        platform=f"NewAPI ({provider_name})",
                        account=account_name,
                        status=CheckinStatus.FAILED,
                        message="OAuth 登录失败，无法获取 session",
                    )

                # 用获取到的 session 签到
                logger.info(f"[{account_name}] 使用新 session 签到...")
                success, message, details = await checker._checkin_with_cookies(session_cookie, api_user)

                details["login_method"] = "shared_oauth"
                details["_cached_session"] = session_cookie
                details["_cached_api_user"] = api_user

                return CheckinResult(
                    platform=f"NewAPI ({provider_name})",
                    account=account_name,
                    status=CheckinStatus.SUCCESS if success else CheckinStatus.FAILED,
                    message=message,
                    details=details,
                )
            except Exception as e:
                retryable = self._is_retryable_network_error(e)
                if retryable and attempt < retry_count:
                    delay = backoff_base * (2 ** attempt)
                    logger.warning(
                        f"[{account_name}] 共享OAuth网络异常，{delay:.1f}s 后重试 "
                        f"({attempt + 1}/{attempt_total}): {e}"
                    )
                    await asyncio.sleep(delay)
                    continue

                if retryable:
                    logger.error(
                        f"[{account_name}] 共享OAuth网络不可达（重试{retry_count}次后失败）: {e}"
                    )
                    return CheckinResult(
                        platform=f"NewAPI ({provider_name})",
                        account=account_name,
                        status=CheckinStatus.FAILED,
                        message=f"OAuth 网络不可达: {str(e)}",
                    )

                logger.error(f"[{account_name}] 共享OAuth异常: {e}")
                return CheckinResult(
                    platform=f"NewAPI ({provider_name})",
                    account=account_name,
                    status=CheckinStatus.FAILED,
                    message=f"OAuth 异常: {str(e)}",
                )

    async def _run_newapi_oauth_fallback(
        self, need_oauth: list[dict], linuxdo_username: str, linuxdo_password: str,
        results: list[CheckinResult],
    ) -> None:
        """回退模式：共享会话失败时，逐站独立启动浏览器"""
        from platforms.newapi_browser import browser_checkin_newapi

        site_timeout = 180
        retry_count = self._env_int("OAUTH_NETWORK_RETRY_COUNT", 2, min_value=0)
        backoff_base = self._env_float("OAUTH_NETWORK_RETRY_BACKOFF", 2.0, min_value=0.5)
        attempt_total = retry_count + 1
        logger.warning(f"回退模式：逐站独立浏览器，{len(need_oauth)} 个站点")

        for idx, item in enumerate(need_oauth):
            provider_name = item["provider_name"]
            account_name = item["account_name"]

            logger.info(f"[{idx+1}/{len(need_oauth)}] [{account_name}] 独立浏览器 OAuth...")

            final_result: CheckinResult | None = None
            for attempt in range(attempt_total):
                try:
                    result = await asyncio.wait_for(
                        browser_checkin_newapi(
                            provider_name=provider_name,
                            linuxdo_username=linuxdo_username,
                            linuxdo_password=linuxdo_password,
                            cookies=None, api_user=None,
                            account_name=account_name,
                        ),
                        timeout=site_timeout,
                    )

                    if (
                        result.status == CheckinStatus.FAILED
                        and self._is_retryable_network_message(result.message or "")
                        and attempt < retry_count
                    ):
                        delay = backoff_base * (2 ** attempt)
                        logger.warning(
                            f"[{account_name}] 独立OAuth网络失败，{delay:.1f}s 后重试 "
                            f"({attempt + 1}/{attempt_total}): {result.message}"
                        )
                        await asyncio.sleep(delay)
                        continue

                    if result.status == CheckinStatus.SUCCESS and result.details:
                        cached_session = result.details.pop("_cached_session", None)
                        cached_api_user = result.details.pop("_cached_api_user", None)
                        if cached_session and cached_api_user:
                            self._cookie_cache.save(provider_name, account_name, cached_session, cached_api_user)

                    final_result = result
                    break

                except asyncio.TimeoutError:
                    if attempt < retry_count:
                        delay = backoff_base * (2 ** attempt)
                        logger.warning(
                            f"[{account_name}] 独立OAuth超时，{delay:.1f}s 后重试 "
                            f"({attempt + 1}/{attempt_total})"
                        )
                        await asyncio.sleep(delay)
                        continue
                    final_result = CheckinResult(
                        platform=f"NewAPI ({provider_name})",
                        account=account_name,
                        status=CheckinStatus.FAILED,
                        message=f"OAuth 超时（>{site_timeout}s）",
                    )
                    break
                except Exception as e:
                    retryable = self._is_retryable_network_error(e)
                    if retryable and attempt < retry_count:
                        delay = backoff_base * (2 ** attempt)
                        logger.warning(
                            f"[{account_name}] 独立OAuth网络异常，{delay:.1f}s 后重试 "
                            f"({attempt + 1}/{attempt_total}): {e}"
                        )
                        await asyncio.sleep(delay)
                        continue
                    final_result = CheckinResult(
                        platform=f"NewAPI ({provider_name})",
                        account=account_name,
                        status=CheckinStatus.FAILED,
                        message=(
                            f"OAuth 网络不可达: {str(e)}"
                            if retryable
                            else f"OAuth 异常: {str(e)}"
                        ),
                    )
                    break

            if final_result is None:
                final_result = CheckinResult(
                    platform=f"NewAPI ({provider_name})",
                    account=account_name,
                    status=CheckinStatus.FAILED,
                    message="OAuth 未知失败",
                )
            results.append(final_result)

    async def _run_newapi_with_accounts(self) -> list[CheckinResult]:
        """手动模式：使用 NEWAPI_ACCOUNTS 中预配置的账号签到"""
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

            # ===== 优先尝试缓存的 Cookie（OAuth 成功后自动保存的） =====
            cached = self._cookie_cache.get(provider_name, account_name)
            if cached:
                logger.info(f"[{account_name}] 发现缓存Cookie，优先尝试Cookie+API方式...")
                try:
                    cached_account = AnyRouterAccount(
                        cookies={"session": cached["session"]},
                        api_user=cached["api_user"],
                        provider=account.provider,
                        name=account.name,
                    )
                    result = await self._checkin_newapi(cached_account, provider, account_name)

                    if result.status == CheckinStatus.SUCCESS:
                        # 缓存 Cookie 有效，签到成功
                        result.message = f"{result.message} (缓存Cookie)"
                        if result.details is None:
                            result.details = {}
                        result.details["login_method"] = "cached_cookie"
                        results.append(result)
                        logger.success(f"[{account_name}] 使用缓存Cookie签到成功！")
                        continue

                    # 检查是否是 Cookie 过期，需要清除缓存
                    msg = result.message or ""
                    if "401" in msg or "403" in msg or "过期" in msg:
                        logger.warning(f"[{account_name}] 缓存Cookie已失效，清除缓存")
                        self._cookie_cache.invalidate(provider_name, account_name)
                    else:
                        logger.warning(f"[{account_name}] 缓存Cookie签到失败: {msg}")
                except Exception as e:
                    logger.warning(f"[{account_name}] 使用缓存Cookie签到异常: {e}")
                    self._cookie_cache.invalidate(provider_name, account_name)
            # ===== 缓存检查结束 =====

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

                    # 缓存 OAuth 获取的新 Cookie，下次直接用 Cookie+API（更快）
                    if result.details:
                        cached_session = result.details.pop("_cached_session", None)
                        cached_api_user = result.details.pop("_cached_api_user", None)
                        if cached_session and cached_api_user:
                            self._cookie_cache.save(
                                provider.name, account_name, cached_session, cached_api_user
                            )
                            logger.success(
                                f"[{account_name}] 新Cookie已缓存，下次将优先使用Cookie+API方式"
                            )
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

        # 对需要 WAF bypass 的站点使用浏览器直接请求（CDN 阻止非浏览器 TLS）
        if provider.needs_waf_cookies():
            return await self._checkin_newapi_browser(provider, account_name, headers, cookies, details)

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

    async def _checkin_newapi_browser(
        self, provider, account_name: str, headers: dict, cookies: dict, details: dict,
    ) -> CheckinResult:
        """使用 Patchright 浏览器执行签到（绕过 CDN TLS 指纹检测）"""
        try:
            from patchright.async_api import async_playwright
        except ImportError:
            from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context()

                # 注入 session cookie 和 WAF cookies
                browser_cookies = []
                domain = provider.domain.replace("https://", "").replace("http://", "")
                for name, value in cookies.items():
                    browser_cookies.append({
                        "name": name, "value": value,
                        "domain": domain, "path": "/",
                    })
                await context.add_cookies(browser_cookies)

                page = await context.new_page()
                # 先访问站点让 WAF cookies 生效
                await page.goto(f"{provider.domain}/login", wait_until="networkidle")

                # 构建 fetch headers（排除浏览器自动管理的头）
                fetch_headers = {
                    "Accept": "application/json, text/plain, */*",
                    provider.api_user_key: headers.get(provider.api_user_key, ""),
                }

                # 1. 获取用户信息
                user_info_path = provider.user_info_path
                try:
                    resp = await page.evaluate(f"""
                        async () => {{
                            const r = await fetch('{user_info_path}', {{
                                headers: {json.dumps(fetch_headers)}
                            }});
                            return {{ status: r.status, text: await r.text() }};
                        }}
                    """)
                    if resp["status"] == 200:
                        try:
                            data = json.loads(resp["text"])
                            if data.get("success"):
                                user_data = data.get("data", {})
                                quota = round(user_data.get("quota", 0) / 500000, 2)
                                used_quota = round(user_data.get("used_quota", 0) / 500000, 2)
                                details["balance"] = f"${quota}"
                                details["used"] = f"${used_quota}"
                                logger.info(f"[{account_name}] 余额: ${quota}, 已用: ${used_quota}")
                        except json.JSONDecodeError:
                            pass
                    else:
                        logger.warning(f"[{account_name}] 获取用户信息失败: HTTP {resp['status']}")
                except Exception as e:
                    logger.warning(f"[{account_name}] 获取用户信息失败: {e}")

                # 2. 执行签到（如果需要）
                if provider.needs_manual_check_in():
                    sign_in_path = provider.sign_in_path
                    try:
                        resp = await page.evaluate(f"""
                            async () => {{
                                const r = await fetch('{sign_in_path}', {{
                                    method: 'POST',
                                    headers: {json.dumps(fetch_headers)}
                                }});
                                return {{ status: r.status, text: await r.text() }};
                            }}
                        """)
                        logger.debug(f"[{account_name}] 签到响应: {resp['status']}")

                        if resp["status"] == 200:
                            try:
                                result = json.loads(resp["text"])
                                msg = result.get("message") or result.get("msg") or ""
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
                            except json.JSONDecodeError:
                                if "success" in resp["text"].lower():
                                    return CheckinResult(
                                        platform=f"NewAPI ({provider.name})",
                                        account=account_name,
                                        status=CheckinStatus.SUCCESS,
                                        message="签到成功",
                                        details=details if details else None,
                                    )

                        logger.error(f"[{account_name}] 签到失败: HTTP {resp['status']}")
                        return CheckinResult(
                            platform=f"NewAPI ({provider.name})",
                            account=account_name,
                            status=CheckinStatus.FAILED,
                            message=f"HTTP {resp['status']}",
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
                    # 自动签到 — 用户信息获取成功即视为签到完成
                    if details:
                        logger.success(f"[{account_name}] 签到成功（自动触发）")
                        return CheckinResult(
                            platform=f"NewAPI ({provider.name})",
                            account=account_name,
                            status=CheckinStatus.SUCCESS,
                            message="签到成功（自动触发）",
                            details=details,
                        )
                    else:
                        logger.warning(f"[{account_name}] 无法确认签到状态（用户信息获取失败）")
                        return CheckinResult(
                            platform=f"NewAPI ({provider.name})",
                            account=account_name,
                            status=CheckinStatus.FAILED,
                            message="无法确认签到状态",
                        )
            finally:
                await browser.close()

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
