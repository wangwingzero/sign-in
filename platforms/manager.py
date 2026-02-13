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
import time
from datetime import datetime, timezone
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
        # NEWAPI_ACCOUNTS 覆盖文件：Secrets 只读时，用文件缓存“最新可用 cookie”覆盖旧配置
        self._newapi_override_file = os.getenv(
            "NEWAPI_ACCOUNTS_OVERRIDE_FILE", ".newapi_accounts_override.json"
        )
        self._newapi_failed_sites_file = os.getenv(
            "NEWAPI_FAILED_SITES_FILE", "scripts/chrome_extension/failed_sites.json"
        )
        self._newapi_accounts_export_file = os.getenv(
            "NEWAPI_ACCOUNTS_EXPORT_FILE", os.path.join("签到账户", "NEWAPI_ACCOUNTS.json")
        )
        self._newapi_original_state: dict[int, dict] = {}
        self._newapi_override_applied_accounts: set[int] = set()
        # 缓存 LinuxDO 账户，用于浏览器回退登录
        self._linuxdo_accounts: list[dict] = []
        self._load_linuxdo_accounts()
        self._apply_newapi_accounts_override()

    def _load_newapi_accounts_override(self) -> dict:
        """加载 NEWAPI 账号覆盖信息（用于覆盖 Secrets 中过期 cookie）。"""
        try:
            if not os.path.exists(self._newapi_override_file):
                return {}
            with open(self._newapi_override_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"读取 NEWAPI 覆盖文件失败: {e}")
            return {}

    def _save_newapi_accounts_override(self, payload: dict) -> None:
        """原子写入 NEWAPI 覆盖文件。"""
        try:
            target_dir = os.path.dirname(self._newapi_override_file) or "."
            os.makedirs(target_dir, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", delete=False, dir=target_dir, encoding="utf-8"
            ) as tmp:
                json.dump(payload, tmp, ensure_ascii=False, indent=2)
                tmp_path = tmp.name
            os.replace(tmp_path, self._newapi_override_file)
        except Exception as e:
            logger.warning(f"写入 NEWAPI 覆盖文件失败: {e}")

    @staticmethod
    def _build_newapi_override_keys(provider: str, name: str | None, api_user: str | None) -> list[str]:
        """生成账号覆盖匹配 key（按稳定性优先级）。"""
        keys: list[str] = []
        p = provider or ""
        if name:
            keys.append(f"{p}::name::{name}")
        if api_user:
            keys.append(f"{p}::api_user::{api_user}")
        return keys

    def _apply_newapi_accounts_override(self) -> None:
        """启动时将覆盖文件中的新 cookie 应用到 NEWAPI_ACCOUNTS 内存配置。"""
        if not self.config.anyrouter_accounts:
            return

        overrides = self._load_newapi_accounts_override()
        if not overrides:
            return

        applied = 0
        for idx, account in enumerate(self.config.anyrouter_accounts):
            account_name = account.get_display_name(idx)
            keys = self._build_newapi_override_keys(account.provider, account.name, account.api_user)
            hit = None
            for k in keys:
                value = overrides.get(k)
                if isinstance(value, dict) and value.get("cookies") and value.get("api_user"):
                    hit = value
                    break
            if not hit:
                continue

            # 记录原始值，便于覆盖cookie失效后回退
            self._newapi_original_state[id(account)] = {
                "cookies": account.cookies,
                "api_user": account.api_user,
            }
            # 只在内存中覆盖；后续运行将优先使用这个新值
            account.cookies = hit["cookies"]
            account.api_user = hit["api_user"]
            self._newapi_override_applied_accounts.add(id(account))
            applied += 1

            source = hit.get("source", "override")
            updated_at = hit.get("updated_at", "")
            logger.info(
                f"[{account_name}] 应用账号覆盖Cookie（source={source}, updated_at={updated_at}）"
            )

        if applied:
            logger.success(f"已应用 {applied} 个 NEWAPI 账号覆盖Cookie")

    def _remove_newapi_account_override(self, account: AnyRouterAccount, provider_name: str) -> None:
        """删除某账号的覆盖记录（覆盖cookie失效时调用）。"""
        payload = self._load_newapi_accounts_override()
        if not payload:
            return

        original = self._newapi_original_state.get(id(account), {})
        original_api_user = original.get("api_user")
        current_api_user = account.api_user
        current_name = account.name

        delete_keys: list[str] = []
        for key, value in payload.items():
            if not isinstance(value, dict):
                continue
            if str(value.get("provider", "")) != provider_name:
                continue
            val_name = value.get("name")
            val_api_user = value.get("api_user")
            if current_name and val_name == current_name:
                delete_keys.append(key)
                continue
            if val_api_user and val_api_user in {current_api_user, original_api_user}:
                delete_keys.append(key)

        if not delete_keys:
            return
        for key in delete_keys:
            payload.pop(key, None)
        self._save_newapi_accounts_override(payload)
        logger.warning(f"已删除失效覆盖Cookie记录: {provider_name}/{current_name or current_api_user}")

    def _restore_newapi_account_original(self, account: AnyRouterAccount) -> bool:
        """恢复账号到 NEWAPI_ACCOUNTS 原始 cookie/api_user。"""
        original = self._newapi_original_state.get(id(account))
        if not original:
            return False
        account.cookies = original.get("cookies")
        account.api_user = original.get("api_user")
        self._newapi_override_applied_accounts.discard(id(account))
        return True

    def _persist_newapi_account_override(
        self,
        account: AnyRouterAccount,
        account_name: str,
        provider_name: str,
        session_cookie: str,
        api_user: str,
        cookies: dict | None = None,
        source: str = "oauth_refresh",
    ) -> None:
        """将新 cookie 持久化为 NEWAPI 账号覆盖，供下次运行优先使用。"""
        if not session_cookie or not api_user:
            return

        cookie_bundle: dict[str, str] = {}
        if isinstance(cookies, dict):
            cookie_bundle = {
                str(k): str(v)
                for k, v in cookies.items()
                if k and v is not None and str(v).strip()
            }
        if "session" not in cookie_bundle:
            cookie_bundle["session"] = session_cookie

        payload = self._load_newapi_accounts_override()
        keys = self._build_newapi_override_keys(provider_name, account.name, account.api_user)
        record = {
            "provider": provider_name,
            "name": account.name,
            "api_user": api_user,
            "cookies": cookie_bundle,
            "source": source,
            "updated_at": str(int(time.time())),
        }
        for key in keys:
            payload[key] = record
        self._save_newapi_accounts_override(payload)

        # 同步更新当前内存对象，确保本次运行后续逻辑直接用新值
        account.cookies = cookie_bundle
        account.api_user = api_user
        logger.success(f"[{account_name}] 已覆盖 NEWAPI 账号Cookie，下次运行将优先使用新Cookie")

    def _load_linuxdo_accounts(self) -> None:
        """加载 LinuxDO 账户用于浏览器回退登录（不用于浏览帖子）"""
        # 从配置中获取 LinuxDO 账户，仅用于 OAuth 登录
        if self.config.linuxdo_accounts:
            for acc in self.config.linuxdo_accounts:
                self._linuxdo_accounts.append({
                    "username": acc.username,
                    "password": acc.password,
                    "name": acc.name,
                    "checkin_sites": acc.checkin_sites,  # 空=全部站点，非空=仅指定站点（白名单）
                    "exclude_sites": acc.exclude_sites,  # 空=不排除，非空=跳过指定站点（黑名单）
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

    def _export_available_sites_list(
        self, providers: dict[str, "ProviderConfig"], ldoh_status: str
    ) -> None:
        """导出可用站点列表到 000/可用站点列表.md（自动更新部分）。

        在每次签到运行后自动更新，方便用户查看当前可用的站点 ID 和 URL，
        用于配置 checkin_sites（白名单）和 exclude_sites（黑名单）字段。
        """
        sites_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "000", "可用站点列表.md")
        if not os.path.exists(sites_file):
            logger.debug("000/可用站点列表.md 不存在，跳过导出")
            return

        try:
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            source = "LDOH 同步" if ldoh_status == "success" else "本地 DEFAULT_PROVIDERS"

            lines = [
                f"**更新时间**: {now_str}  ",
                f"**数据来源**: {source}  ",
                f"**可用站点数**: {len(providers)}",
                "",
                "| 站点 ID | 名称 | URL |",
                "|---------|------|-----|",
            ]
            for name, prov in sorted(providers.items()):
                display_name = prov.name or name
                domain = prov.domain or ""
                lines.append(f"| `{name}` | {display_name} | {domain} |")

            auto_content = "\n".join(lines)

            with open(sites_file, "r", encoding="utf-8") as f:
                content = f.read()

            start_marker = "<!-- AUTO_SITES_START -->"
            end_marker = "<!-- AUTO_SITES_END -->"
            start_idx = content.find(start_marker)
            end_idx = content.find(end_marker)

            if start_idx == -1 or end_idx == -1:
                logger.debug("000/可用站点列表.md 中未找到 AUTO_SITES 标记，跳过导出")
                return

            new_content = (
                content[:start_idx + len(start_marker)]
                + "\n"
                + auto_content
                + "\n"
                + content[end_idx:]
            )

            with open(sites_file, "w", encoding="utf-8") as f:
                f.write(new_content)
            logger.info(f"已导出 {len(providers)} 个可用站点到 000/可用站点列表.md")
        except Exception as e:
            logger.debug(f"导出可用站点列表失败（非关键）: {e}")

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
    def _env_bool(name: str, default: bool = False) -> bool:
        """读取布尔环境变量（非法值回退默认值）。"""
        raw = os.getenv(name)
        if raw is None:
            return default
        value = str(raw).strip().lower()
        if value in {"1", "true", "yes", "y", "on"}:
            return True
        if value in {"0", "false", "no", "n", "off"}:
            return False
        return default

    @staticmethod
    def _is_debug_mode() -> bool:
        """检查调试模式（命令行 --debug 或环境变量）。"""
        return (
            str(os.getenv("DEBUG", "")).strip().lower() in {"1", "true", "yes", "on"}
            or str(os.getenv("DEBUG_MODE", "")).strip().lower() in {"1", "true", "yes", "on"}
            or str(os.getenv("NEWAPI_DEBUG", "")).strip().lower() in {"1", "true", "yes", "on"}
        )

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

    @staticmethod
    def _looks_like_ldoh_site_item(item: dict) -> bool:
        """判断单个字典是否像 LDOH 站点对象。"""
        if not isinstance(item, dict):
            return False
        keys = {str(k).lower() for k in item.keys()}
        strong_keys = {"apibaseurl", "supportscheckin", "checkinurl"}
        if keys & strong_keys:
            return True
        has_name = "name" in keys or "title" in keys
        has_urlish = any(("api" in k and "url" in k) for k in keys) or "domain" in keys
        return has_name and has_urlish

    @classmethod
    def _extract_ldoh_sites_from_json(
        cls, node, path: str = "root", depth: int = 0, max_depth: int = 8
    ) -> tuple[list, str] | tuple[None, None]:
        """从任意 JSON 结构中递归提取最可能的站点数组，并返回来源路径。"""
        if depth > max_depth:
            return None, None

        if isinstance(node, list):
            if node:
                sample = node[: min(5, len(node))]
                if all(isinstance(item, dict) for item in sample):
                    hit = sum(1 for item in sample if cls._looks_like_ldoh_site_item(item))
                    if hit >= max(1, (len(sample) + 1) // 2):
                        return node, path
            # 列表里继续向下找，限制扫描数量避免性能退化
            for idx, item in enumerate(node[:30]):
                if not isinstance(item, (dict, list)):
                    continue
                sites, hit_path = cls._extract_ldoh_sites_from_json(
                    item, f"{path}[{idx}]", depth + 1, max_depth
                )
                if sites is not None:
                    return sites, hit_path
            return None, None

        if isinstance(node, dict):
            priority_words = ("site", "data", "list", "item", "result", "rows")
            keys = list(node.keys())
            keys.sort(
                key=lambda k: any(w in str(k).lower() for w in priority_words),
                reverse=True,
            )

            for key in keys:
                value = node.get(key)
                next_path = f"{path}.{key}"
                if isinstance(value, (dict, list)):
                    sites, hit_path = cls._extract_ldoh_sites_from_json(
                        value, next_path, depth + 1, max_depth
                    )
                    if sites is not None:
                        return sites, hit_path
                elif isinstance(value, str):
                    stripped = value.strip()
                    if not stripped or len(stripped) > 200_000 or stripped[0] not in "[{":
                        continue
                    try:
                        parsed = json.loads(stripped)
                    except Exception:
                        continue
                    sites, hit_path = cls._extract_ldoh_sites_from_json(
                        parsed, f"{next_path}(json)", depth + 1, max_depth
                    )
                    if sites is not None:
                        return sites, hit_path

        return None, None

    async def _auto_approve_linuxdo_oauth(self, tab) -> bool:
        """在 LinuxDO OAuth 授权页自动点击“允许/Authorize”按钮。"""
        try:
            current_url = getattr(tab.target, "url", "") or ""
            if "linux.do" not in current_url.lower() or "authorize" not in current_url.lower():
                return False

            logger.info(f"LDOH: 检测到 LinuxDO 授权页，尝试自动同意: {current_url}")

            # 多策略点击：文本匹配 -> 红色主按钮 -> submit/form
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
                    let target = all.find((el) => {
                        const text = (el.innerText || el.value || el.textContent || '').trim().toLowerCase();
                        return text.includes('允许') || text.includes('同意') || text.includes('authorize') || text.includes('allow');
                    });

                    // LinuxDO 授权页允许按钮通常是红色主按钮
                    if (!target) {
                        target = all.find((el) => {
                            const cls = (el.className || '').toLowerCase();
                            return cls.includes('btn-danger') || cls.includes('bg-red') || cls.includes('danger');
                        });
                    }

                    if (!target) {
                        target = document.querySelector('button[type="submit"], input[type="submit"]');
                    }

                    if (!target) return '';

                    const text = (target.innerText || target.value || target.textContent || '').trim().substring(0, 16);

                    // 如果是链接，直接导航（最可靠）
                    if (target.tagName === 'A' && target.href && !target.href.startsWith('javascript:')) {
                        window.location.href = target.href;
                        return ['navigated', text, target.href.substring(0, 120)];
                    }

                    if (clickTarget(target)) {
                        const form = target.closest('form');
                        if (form) {
                            try {
                                form.submit();
                                return ['form_submitted', text, form.action || ''];
                            } catch (e) {}
                        }
                        return ['clicked', text, ''];
                    }

                    const form = document.querySelector('form[action*="oauth"], form[action*="authorize"], form[action*="approve"]');
                    if (form) {
                        try {
                            form.submit();
                            return ['form_submit_only', '', form.action || ''];
                        } catch (e) {}
                    }
                    return '';
                })()
                """
            )

            strategy = self._unwrap_eval_value(click_result)
            if strategy and isinstance(strategy, (list, tuple)):
                action = strategy[0] if len(strategy) > 0 else "unknown"
                btn_text = strategy[1] if len(strategy) > 1 else ""
                detail = strategy[2] if len(strategy) > 2 else ""
                action = self._unwrap_eval_value(action)
                btn_text = self._unwrap_eval_value(btn_text)
                detail = self._unwrap_eval_value(detail)
                logger.info(f"LDOH: 已执行授权点击策略: {action}, 按钮={btn_text}, detail={detail}")
                await asyncio.sleep(2)
                new_url = getattr(tab.target, "url", "") or ""
                if new_url != current_url:
                    logger.success(f"LDOH: 授权页已跳转: {new_url}")
                    return True
                logger.warning("LDOH: 已点击授权按钮，但页面暂未跳转")
            elif strategy:
                logger.info(f"LDOH: 已执行授权点击策略: {strategy}")
                await asyncio.sleep(2)
            else:
                logger.warning("LDOH: 授权页未找到可点击的“允许”按钮")
        except Exception as e:
            logger.warning(f"LDOH: 自动同意授权失败: {e}")

        return False

    async def _fetch_ldoh_sites_payload_by_navigation(self, tab, ldoh_base_url: str) -> dict | None:
        """当 fetch('/api/sites') 不稳定时，回退到直接访问 API 页面读取正文。"""
        try:
            api_url = f"{ldoh_base_url}/api/sites"
            await tab.get(api_url)
            await asyncio.sleep(2)
            raw_text = await tab.evaluate(
                r"""
                (function() {
                    const pre = document.querySelector('pre');
                    if (pre) return pre.innerText || pre.textContent || '';
                    if (document.body) return document.body.innerText || document.body.textContent || '';
                    return '';
                })()
                """
            )
            text = self._unwrap_eval_value(raw_text)
            if not isinstance(text, str):
                text = str(text or "")
            text = text.strip()
            if not text:
                logger.warning("LDOH: 直接访问 /api/sites 仍为空响应")
                return None

            payload = json.loads(text)
            sites, hit_path = self._extract_ldoh_sites_from_json(payload)
            if sites is None:
                if isinstance(payload, dict):
                    keys_preview = ",".join(list(payload.keys())[:8])
                    logger.warning(
                        f"LDOH: /api/sites JSON 未识别到站点数组，顶层keys={keys_preview}"
                    )
                else:
                    logger.warning(f"LDOH: /api/sites 返回不支持的 JSON 类型: {type(payload).__name__}")
                return None

            status = 200
            if isinstance(payload, dict) and "status" in payload:
                raw_status = payload.get("status")
                try:
                    status = int(raw_status)
                except (TypeError, ValueError):
                    status = 200 if sites else -1

            return {
                "status": status,
                "total": len(sites),
                "sites": sites,
                "_source": f"direct:{hit_path or 'unknown'}",
            }
        except Exception as e:
            logger.warning(f"LDOH: 直接访问 /api/sites 失败: {e}")
            return None

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
            payload_text = payload_text.strip()
            try:
                payload = json.loads(payload_text)
            except json.JSONDecodeError:
                logger.warning(
                    f"LDOH 站点同步返回非 JSON，前200字符: {payload_text[:200]!r}"
                )
                payload = await self._fetch_ldoh_sites_payload_by_navigation(tab, ldoh_base_url)
                if payload is None:
                    return None

            sites = payload.get("sites") if isinstance(payload.get("sites"), list) else []
            raw_status = payload.get("status", None)
            if raw_status is None and sites:
                status = 200
            else:
                try:
                    status = int(raw_status)
                except (TypeError, ValueError):
                    status = -1

            source = payload.get("_source", "fetch")
            logger.debug(f"LDOH 站点同步解析: source={source}, status={status}, sites={len(sites)}")
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

    def _log_auto_oauth_summary(
        self, stats: dict[str, int | str], results: list[CheckinResult]
    ) -> None:
        """输出自动 OAuth 结构化摘要，便于 CI 抽取。"""
        success_count = sum(1 for r in results if r.status == CheckinStatus.SUCCESS)
        failed_count = sum(1 for r in results if r.status == CheckinStatus.FAILED)
        skipped_count = sum(1 for r in results if r.status == CheckinStatus.SKIPPED)
        payload = {
            **stats,
            "result_total": len(results),
            "result_success": success_count,
            "result_failed": failed_count,
            "result_skipped": skipped_count,
        }
        logger.info(f"AUTO_OAUTH_SUMMARY: {json.dumps(payload, ensure_ascii=False)}")

    @staticmethod
    def _parse_newapi_provider(platform_name: str) -> str | None:
        """从平台名中解析 provider，如 'NewAPI (wong)' -> 'wong'。"""
        prefix = "NewAPI ("
        if not platform_name.startswith(prefix) or not platform_name.endswith(")"):
            return None
        return platform_name[len(prefix):-1].strip() or None

    def export_newapi_failed_sites_for_extension(self, output_path: str | None = None) -> str:
        """导出 NewAPI 失败站点报告给 Chrome 插件读取。"""
        target_path = output_path or self._newapi_failed_sites_file
        failed_results = [
            r
            for r in self.results
            if r.status == CheckinStatus.FAILED
            and isinstance(r.platform, str)
            and r.platform.startswith("NewAPI (")
        ]

        account_lookup: dict[tuple[str, str], AnyRouterAccount] = {}
        for idx, account in enumerate(self.config.anyrouter_accounts):
            account_lookup[(account.provider, account.get_display_name(idx))] = account

        failed_sites: list[dict] = []
        for result in failed_results:
            provider_name = self._parse_newapi_provider(result.platform) or "unknown"
            provider = self.config.providers.get(provider_name)
            if not provider and provider_name in DEFAULT_PROVIDERS:
                provider = ProviderConfig.from_dict(provider_name, DEFAULT_PROVIDERS[provider_name])

            domain = provider.domain if provider else ""
            login_url = f"{domain}/login" if domain else ""
            oauth_url = ""
            if provider and domain:
                oauth_path = getattr(provider, "oauth_path", None)
                oauth_url = f"{domain}{oauth_path}" if oauth_path else f"{domain}/auth/login?returnTo=%2F"

            account = account_lookup.get((provider_name, result.account))
            api_user = ""
            if account and getattr(account, "api_user", None) is not None:
                api_user = str(account.api_user)

            message = result.message or "签到失败"
            lowered_msg = message.lower()
            oauth_blocked = any(
                key in lowered_msg
                for key in ("无法获取 session", "oauth 登录失败", "linuxdo 登录失败", "cloudflare", "验证失败")
            )
            result_details = result.details if isinstance(result.details, dict) else {}
            failed_sites.append(
                {
                    "provider": provider_name,
                    "account_name": result.account,
                    "api_user": api_user,
                    "platform": result.platform,
                    "site_url": domain,
                    "login_url": login_url,
                    "oauth_login_url": oauth_url,
                    "reason": message,
                    "failure_kind": result_details.get("failure_kind", ""),
                    "runtime_cookie_keys": result_details.get("runtime_cookie_keys", []),
                    "last_url": result_details.get("last_url", ""),
                    "needs_manual_auth": True,
                    "oauth_cookie_blocked": oauth_blocked,
                }
            )

        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "github_actions" if os.getenv("GITHUB_ACTIONS") == "true" else "local",
            "failed_count": len(failed_sites),
            "failed_sites": failed_sites,
        }

        target_dir = os.path.dirname(target_path) or "."
        os.makedirs(target_dir, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", delete=False, dir=target_dir, encoding="utf-8") as tmp:
            json.dump(payload, tmp, ensure_ascii=False, indent=2)
            tmp_path = tmp.name
        os.replace(tmp_path, target_path)
        logger.info(f"已导出失败站点清单到: {target_path} (count={len(failed_sites)})")
        return target_path

    @staticmethod
    def _merge_newapi_export_entry(
        records: dict[tuple[str, str], dict],
        *,
        provider: str,
        name: str,
        session: str,
        api_user: str,
        updated_at: float,
        source: str,
        source_priority: int,
    ) -> None:
        """合并 NEWAPI 导出记录，优先保留更新且更可靠的数据源。"""
        provider_norm = (provider or "").strip().lower()
        name_norm = (name or "").strip()
        session_norm = (session or "").strip()
        api_user_norm = (api_user or "").strip()
        if not provider_norm or not session_norm or not api_user_norm:
            return
        if not name_norm:
            name_norm = provider_norm

        key = (provider_norm, name_norm)
        current = records.get(key)
        candidate = {
            "name": name_norm,
            "provider": provider_norm,
            "cookies": {"session": session_norm},
            "api_user": api_user_norm,
            "_updated_at": float(updated_at or 0),
            "_source": source,
            "_source_priority": source_priority,
        }
        if not current:
            records[key] = candidate
            return

        current_ts = float(current.get("_updated_at", 0))
        current_pri = int(current.get("_source_priority", 0))
        cand_ts = float(candidate.get("_updated_at", 0))
        cand_pri = int(candidate.get("_source_priority", 0))

        if cand_ts > current_ts or (cand_ts == current_ts and cand_pri >= current_pri):
            records[key] = candidate

    def export_newapi_accounts_for_sync(self, output_path: str | None = None) -> str:
        """导出最新 NEWAPI_ACCOUNTS 快照（可直接用于 Secret 回填）。"""
        target_path = output_path or self._newapi_accounts_export_file
        records: dict[tuple[str, str], dict] = {}
        from_config = 0
        from_override = 0
        from_cache = 0

        # 1) 当前内存配置（包含启动时应用的 override）
        for idx, account in enumerate(self.config.anyrouter_accounts):
            provider = str(account.provider or "").strip().lower()
            name = account.get_display_name(idx)
            session = self._extract_session_cookie(account.cookies)
            api_user = str(account.api_user or "").strip()
            if provider and session and api_user:
                self._merge_newapi_export_entry(
                    records,
                    provider=provider,
                    name=name,
                    session=session,
                    api_user=api_user,
                    updated_at=0.0,
                    source="config",
                    source_priority=10,
                )
                from_config += 1

        # 2) 覆盖文件（通常来自 OAuth 刷新）
        override_payload = self._load_newapi_accounts_override()
        for value in override_payload.values():
            if not isinstance(value, dict):
                continue
            provider = str(value.get("provider") or "").strip().lower()
            name = str(value.get("name") or "").strip() or provider
            session = self._extract_session_cookie(value.get("cookies"))
            api_user = str(value.get("api_user") or "").strip()
            updated_at_raw = value.get("updated_at")
            try:
                updated_at = float(updated_at_raw or 0)
            except Exception:
                updated_at = 0.0
            if provider and session and api_user:
                self._merge_newapi_export_entry(
                    records,
                    provider=provider,
                    name=name,
                    session=session,
                    api_user=api_user,
                    updated_at=updated_at,
                    source=str(value.get("source") or "override"),
                    source_priority=20,
                )
                from_override += 1

        # 3) Cookie 缓存（本轮/历史成功签到后最可靠）
        for cached in self._cookie_cache.list_valid():
            provider = str(cached.get("provider") or "").strip().lower()
            name = str(cached.get("account_name") or "").strip() or provider
            session = str(cached.get("session") or "").strip()
            api_user = str(cached.get("api_user") or "").strip()
            updated_at = float(cached.get("cached_at") or 0)
            if provider and session and api_user:
                self._merge_newapi_export_entry(
                    records,
                    provider=provider,
                    name=name,
                    session=session,
                    api_user=api_user,
                    updated_at=updated_at,
                    source="cookie_cache",
                    source_priority=30,
                )
                from_cache += 1

        export_data = []
        for _, item in sorted(records.items(), key=lambda kv: (kv[0][0], kv[0][1])):
            export_data.append(
                {
                    "name": item["name"],
                    "provider": item["provider"],
                    "cookies": item["cookies"],
                    "api_user": item["api_user"],
                }
            )

        target_dir = os.path.dirname(target_path) or "."
        os.makedirs(target_dir, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", delete=False, dir=target_dir, encoding="utf-8") as tmp:
            json.dump(export_data, tmp, ensure_ascii=False, indent=2)
            tmp_path = tmp.name
        os.replace(tmp_path, target_path)
        logger.info(
            f"已导出 NEWAPI_ACCOUNTS 到: {target_path} "
            f"(records={len(export_data)}, from_config={from_config}, "
            f"from_override={from_override}, from_cache={from_cache})"
        )
        return target_path

    def send_newapi_accounts_export_email(
        self,
        export_path: str | None,
        failed_sites_path: str | None = None,
    ) -> None:
        """可选：发送 NEWAPI 导出附件邮件（账号快照 + 失败站点清单）。"""
        if not self._env_bool("NEWAPI_EXPORT_EMAIL_ENABLED", False):
            logger.info("NEWAPI 导出附件邮件未启用（NEWAPI_EXPORT_EMAIL_ENABLED=false）")
            return

        attachments: list[str] = []

        if export_path:
            if os.path.exists(export_path):
                attachments.append(export_path)
            else:
                logger.warning(f"NEWAPI 导出附件不存在，已跳过: {export_path}")

        if failed_sites_path:
            if os.path.exists(failed_sites_path):
                attachments.append(failed_sites_path)
            else:
                logger.warning(f"失败站点附件不存在，已跳过: {failed_sites_path}")

        if not attachments:
            logger.warning("NEWAPI 导出附件邮件未发送：没有可用附件")
            return

        title = os.getenv(
            "NEWAPI_EXPORT_EMAIL_SUBJECT",
            f"NEWAPI_ACCOUNTS 导出 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        )
        content_lines = ["本次 NewAPI 运行已生成附件文件："]
        if export_path and export_path in attachments:
            content_lines.append(
                f"- {os.path.basename(export_path)}：运行后账号快照，可按需更新到仓库 Secret。"
            )
        if failed_sites_path and failed_sites_path in attachments:
            content_lines.append(
                f"- {os.path.basename(failed_sites_path)}：失败站点清单，可在插件中导入后一键打开站点进行人工登录补录。"
            )
        content = "\n".join(content_lines)

        with self.notify:
            self.notify.send_email_with_attachments(
                title=title,
                content=content,
                attachments=attachments,
                msg_type="text",
            )

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

        仅自动模式：
        - 必须配置 LINUXDO_ACCOUNTS
        - NEWAPI_ACCOUNTS 默认作为 seed cookie 补充来源
        - 未映射到 LinuxDO 的 anyrouter 账号会额外独立执行
        """
        if not self._linuxdo_accounts:
            if not self.config.anyrouter_accounts:
                logger.warning("未配置 LINUXDO_ACCOUNTS，自动模式无法执行")
                return []
            logger.warning("未配置 LINUXDO_ACCOUNTS，将仅执行独立 anyrouter 账号")
            return await self._run_unmapped_anyrouter_accounts()
        logger.info("使用仅自动模式：以 LINUXDO_ACCOUNTS 遍历站点，NEWAPI_ACCOUNTS 仅作为 seed cookie")

        all_results: list[CheckinResult] = []
        used_seed_identities: set[tuple[str, str]] = set()
        total_accounts = len(self._linuxdo_accounts)

        for idx, linuxdo_account in enumerate(self._linuxdo_accounts):
            linuxdo_username = linuxdo_account.get("username", "")
            linuxdo_name = linuxdo_account.get("name", linuxdo_username or f"LinuxDO账号{idx + 1}")
            logger.info(
                f"自动模式: 开始处理 LinuxDO 账号 [{idx + 1}/{total_accounts}] [{linuxdo_name}]"
            )
            try:
                account_results = await self._run_newapi_auto_oauth(
                    linuxdo_account=linuxdo_account,
                    account_index=idx,
                    account_total=total_accounts,
                    used_seed_identities=used_seed_identities,
                )
                all_results.extend(account_results)
            except Exception as e:
                logger.exception(f"[{linuxdo_name}] 自动模式运行异常: {e}")
                all_results.append(CheckinResult(
                    platform="NewAPI",
                    account=linuxdo_name,
                    status=CheckinStatus.FAILED,
                    message=f"自动模式运行异常: {str(e)}",
                ))

        standalone_anyrouter_results = await self._run_unmapped_anyrouter_accounts(used_seed_identities)
        all_results.extend(standalone_anyrouter_results)

        return all_results

    def _build_seed_accounts_by_provider(self) -> dict[str, list[AnyRouterAccount]]:
        """构建 NEWAPI_ACCOUNTS seed 映射（provider -> 账号列表）。

        同一 provider 可能有多个不同账号（如多个 LinuxDO 用户各有独立 anyrouter 账户），
        全部保留，签到时按 LinuxDO 账号名匹配对应 seed。
        """
        seeds: dict[str, list[AnyRouterAccount]] = {}
        for idx, account in enumerate(self.config.anyrouter_accounts):
            provider = (account.provider or "").strip().lower()
            if not provider:
                continue
            session = self._extract_session_cookie(account.cookies)
            api_user = str(account.api_user or "").strip()
            if not session or not api_user:
                continue
            if provider not in seeds:
                seeds[provider] = []
            seeds[provider].append(account)
            logger.debug(f"[seed] provider={provider}, account={account.get_display_name(idx)}, api_user={api_user}")
        return seeds

    @staticmethod
    def _match_seed_for_linuxdo(
        seeds: list[AnyRouterAccount], linuxdo_name: str, account_index: int = 0,
    ) -> AnyRouterAccount | None:
        """从多个 seed 中匹配当前 LinuxDO 账号的 seed。

        匹配优先级：
        1. seed.name 包含 linuxdo_name（如 seed='主账号_anyrouter', linuxdo='主账号'）
        2. 按 LinuxDO 账号轮次索引选择（第 N 个 LinuxDO 账号用第 N 个 seed）
        3. 兜底：第一个 seed
        """
        if not seeds:
            return None

        ld_lower = linuxdo_name.lower()
        # 优先名称匹配
        for seed in seeds:
            seed_name = (seed.name or "").lower()
            if ld_lower and seed_name and (ld_lower in seed_name or seed_name in ld_lower):
                return seed

        # 按轮次索引选择（不同 LinuxDO 账号用不同 seed，支持任意命名）
        if account_index < len(seeds):
            return seeds[account_index]

        # 兜底：第一个
        return seeds[0]

    @staticmethod
    def _build_seed_identity(account: AnyRouterAccount) -> tuple[str, str] | None:
        """构建 seed 账号标识（provider + api_user），用于跨流程去重。"""
        provider = (account.provider or "").strip().lower()
        api_user = str(account.api_user or "").strip()
        if not provider or not api_user:
            return None
        return (provider, api_user)

    def _get_provider_with_default(self, provider_name: str) -> ProviderConfig | None:
        """读取 provider 配置，不存在时回退 DEFAULT_PROVIDERS。"""
        provider = self.config.providers.get(provider_name)
        if provider:
            return provider

        if provider_name in DEFAULT_PROVIDERS:
            try:
                return ProviderConfig.from_dict(provider_name, DEFAULT_PROVIDERS[provider_name])
            except Exception as e:
                logger.warning(f"加载默认 provider '{provider_name}' 失败: {e}")
        return None

    async def _run_unmapped_anyrouter_accounts(
        self,
        used_seed_identities: set[tuple[str, str]] | None = None,
    ) -> list[CheckinResult]:
        """执行未被 LinuxDO 映射到的 anyrouter 账号。"""
        if not self.config.anyrouter_accounts:
            return []

        provider_name = "anyrouter"
        provider = self._get_provider_with_default(provider_name)
        if not provider:
            logger.warning("独立 anyrouter 账号执行失败：未找到 anyrouter provider 配置")
            return []

        used = used_seed_identities or set()
        handled_identities: set[tuple[str, str]] = set()
        results: list[CheckinResult] = []

        for idx, account in enumerate(self.config.anyrouter_accounts):
            if (account.provider or "").strip().lower() != provider_name:
                continue

            identity = self._build_seed_identity(account)
            if not identity or identity in used or identity in handled_identities:
                continue

            session = self._extract_session_cookie(account.cookies)
            if not session:
                continue

            handled_identities.add(identity)
            account_name = account.get_display_name(idx)
            logger.info(f"[{account_name}] 作为独立 anyrouter 账号执行（未关联 LinuxDO）")

            result = await self._checkin_newapi(account, provider, account_name)
            if result.status == CheckinStatus.SUCCESS:
                result.message = f"{result.message} (NEWAPI_ACCOUNTS 独立账号)"
                if result.details is None:
                    result.details = {}
                result.details["login_method"] = "newapi_accounts_standalone"

                cookies = account.cookies if isinstance(account.cookies, dict) else {"session": session}
                self._cookie_cache.save(
                    provider_name,
                    account_name,
                    session,
                    str(account.api_user or ""),
                    cookies=cookies,
                )
            results.append(result)

        if handled_identities:
            logger.info(f"独立 anyrouter 账号执行完成: {len(handled_identities)} 个账号")
        return results

    async def _run_newapi_auto_oauth(
        self,
        linuxdo_account: dict | None = None,
        account_index: int = 0,
        account_total: int = 1,
        used_seed_identities: set[tuple[str, str]] | None = None,
    ) -> list[CheckinResult]:
        """自动模式：用单个 LinuxDO 账号遍历所有 NewAPI 站点，自动 OAuth 登录签到

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
        stats: dict[str, int | str] = {
            "ldoh_sync_status": "not_started",
            "candidate_total": 0,
            "candidate_after_probe": 0,
            "candidate_skipped_by_probe": 0,
            "cookie_hit": 0,
            "cookie_success": 0,
            "cookie_invalidated": 0,
            "oauth_needed": 0,
            "oauth_success": 0,
            "oauth_failed": 0,
            "oauth_network_failed": 0,
        }

        if linuxdo_account is None:
            linuxdo_account = self._linuxdo_accounts[0]

        linuxdo_username = linuxdo_account["username"]
        linuxdo_password = linuxdo_account["password"]
        linuxdo_name = linuxdo_account.get("name", linuxdo_username)
        checkin_sites: list[str] = linuxdo_account.get("checkin_sites") or []
        exclude_sites: list[str] = linuxdo_account.get("exclude_sites") or []

        # 环境变量覆盖 checkin_sites（用于快速调试单个站点，如 CHECKIN_SITES_OVERRIDE=anyrouter）
        env_checkin_override = os.environ.get("CHECKIN_SITES_OVERRIDE", "").strip()
        if env_checkin_override:
            checkin_sites = [s.strip() for s in env_checkin_override.split(",") if s.strip()]
            logger.info(f"[{linuxdo_name}] CHECKIN_SITES_OVERRIDE 覆盖: {checkin_sites}")
        account_progress = (
            f"{account_index + 1}/{account_total}"
            if account_total > 0
            else "1/1"
        )

        logger.info(f"自动模式[{account_progress}]: 使用 LinuxDO 账号 [{linuxdo_name}] 遍历站点")
        seed_accounts = self._build_seed_accounts_by_provider()
        if seed_accounts:
            logger.info(f"自动模式: 加载 NEWAPI_ACCOUNTS seed cookie {len(seed_accounts)} 个 provider")

        # 本地兜底站点（LDOH 同步失败时使用）
        providers_to_test = self._get_local_auto_providers()
        if not providers_to_test:
            logger.warning("未找到可用的本地站点配置，自动模式终止")
            self._log_auto_oauth_summary(stats, results)
            return results
        logger.info(f"本地兜底站点数量: {len(providers_to_test)}")
        stats["candidate_total"] = len(providers_to_test)

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
                    stats["ldoh_sync_status"] = "success"
                else:
                    logger.warning("LDOH 同步失败，使用本地兜底站点继续")
                    stats["ldoh_sync_status"] = "fallback_local"

            # 业务要求：无论 LDOH 同步结果如何，始终保留 anyrouter 参与后续流程
            # anyrouter 因 bypass_method="waf_cookies" 被 _get_local_auto_providers 跳过，
            # 需要在此处强制补回，确保每轮都能处理 anyrouter 签到
            if "anyrouter" not in providers_to_test:
                anyrouter_provider = self.config.providers.get("anyrouter")
                if not anyrouter_provider and "anyrouter" in DEFAULT_PROVIDERS:
                    try:
                        anyrouter_provider = ProviderConfig.from_dict(
                            "anyrouter", DEFAULT_PROVIDERS["anyrouter"]
                        )
                    except Exception:
                        anyrouter_provider = None
                if anyrouter_provider:
                    providers_to_test["anyrouter"] = anyrouter_provider
                    self._register_runtime_provider("anyrouter", anyrouter_provider)
                    logger.info("强制并入 anyrouter（确保每轮处理）")
                else:
                    logger.warning("尝试并入 anyrouter 失败：本地未找到 anyrouter 配置")
            else:
                logger.warning("共享会话 LinuxDO 登录失败，后续改为逐站独立 OAuth")
                stats["ldoh_sync_status"] = "linuxdo_login_failed_fallback_local"
        except Exception as e:
            logger.error(f"共享浏览器启动失败: {e}")
            logger.warning("无法连接 LDOH 或共享浏览器不可用，使用本地兜底站点继续")
            stats["ldoh_sync_status"] = "shared_browser_failed_fallback_local"

        logger.info(f"本轮待处理站点: {len(providers_to_test)}")

        providers_to_test = await self._filter_available_providers(providers_to_test)
        stats["candidate_after_probe"] = len(providers_to_test)
        stats["candidate_skipped_by_probe"] = int(stats["candidate_total"]) - len(providers_to_test)

        # 导出全部可用站点列表到 000/可用站点列表.md（仅首个账号执行，避免重复写入）
        if account_index == 0:
            self._export_available_sites_list(providers_to_test, stats.get("ldoh_sync_status", ""))

        if not providers_to_test:
            logger.warning("可用站点数为 0，跳过本轮 NewAPI 自动签到")
            try:
                logger.info("共享会话: 关闭浏览器")
                await browser_mgr.close()
            except Exception as e:
                logger.debug(f"关闭共享浏览器失败（可忽略）: {e}")
            self._log_auto_oauth_summary(stats, results)
            return results

        # 按 checkin_sites 过滤（白名单）：非空时仅保留指定站点，空则保留全部（默认行为）
        # 支持精确匹配 + 模糊匹配（LDOH 同步后站点名称可能变化，如 hotaru → ldoh_hotaruapi_com）
        if checkin_sites:
            checkin_set = {s.strip().lower() for s in checkin_sites if s.strip()}
            before_count = len(providers_to_test)
            filtered: dict[str, ProviderConfig] = {}
            matched_specs: set[str] = set()

            for prov_name, prov in providers_to_test.items():
                prov_lower = prov_name.lower()
                # 精确匹配
                if prov_lower in checkin_set:
                    filtered[prov_name] = prov
                    matched_specs.add(prov_lower)
                    continue
                # 模糊匹配：用户指定的名称是 provider 名称或显示名的子串
                # 例如 "hotaru" 匹配 "ldoh_hotaruapi_com"，"duckcoding" 匹配 "ldoh_free_duckcoding_com"
                for spec in checkin_set:
                    if spec in prov_lower or spec in (prov.name or "").lower():
                        filtered[prov_name] = prov
                        matched_specs.add(spec)
                        logger.debug(
                            f"[{linuxdo_name}] checkin_sites 模糊匹配: '{spec}' → '{prov_name}'"
                        )
                        break

            providers_to_test = filtered
            unmatched = checkin_set - matched_specs
            if unmatched:
                logger.warning(
                    f"[{linuxdo_name}] checkin_sites 中以下站点未匹配到任何可用站点: {sorted(unmatched)}"
                )
            logger.info(
                f"[{linuxdo_name}] checkin_sites 白名单过滤: {before_count} → {len(providers_to_test)} 个站点 "
                f"(指定: {sorted(checkin_set)}, 匹配: {sorted(matched_specs)})"
            )
            if not providers_to_test:
                logger.warning(f"[{linuxdo_name}] checkin_sites 过滤后无可用站点，跳过")
                try:
                    await browser_mgr.close()
                except Exception:
                    pass
                self._log_auto_oauth_summary(stats, results)
                return results
        else:
            logger.info(f"[{linuxdo_name}] checkin_sites 未设置，签到全部可用站点")

        # 按 exclude_sites 排除（黑名单）：非空时从候选列表中移除指定站点
        # 同样支持模糊匹配
        if exclude_sites:
            exclude_set = {s.strip().lower() for s in exclude_sites if s.strip()}
            before_count = len(providers_to_test)
            excluded_names: set[str] = set()

            for prov_name, prov in list(providers_to_test.items()):
                prov_lower = prov_name.lower()
                should_exclude = False
                # 精确匹配
                if prov_lower in exclude_set:
                    should_exclude = True
                else:
                    # 模糊匹配
                    for spec in exclude_set:
                        if spec in prov_lower or spec in (prov.name or "").lower():
                            should_exclude = True
                            break
                if should_exclude:
                    excluded_names.add(prov_name)

            providers_to_test = {
                name: prov for name, prov in providers_to_test.items()
                if name not in excluded_names
            }
            if excluded_names:
                logger.info(
                    f"[{linuxdo_name}] exclude_sites 黑名单排除: {before_count} → {len(providers_to_test)} 个站点 "
                    f"(排除: {sorted(n.lower() for n in excluded_names)})"
                )
            if not providers_to_test:
                logger.warning(f"[{linuxdo_name}] exclude_sites 排除后无可用站点，跳过")
                try:
                    await browser_mgr.close()
                except Exception:
                    pass
                self._log_auto_oauth_summary(stats, results)
                return results

        # 统计需要浏览器 OAuth 的站点（无缓存或缓存失效）
        need_oauth = []
        for provider_name, provider in providers_to_test.items():
            account_name = f"{linuxdo_name}_{provider_name}"

            # 1. 优先尝试 NEWAPI_ACCOUNTS seed cookie（补充来源，不是主流程）
            # 支持多账号：按 LinuxDO 账号名匹配对应的 seed（同 provider 可能有多个不同用户）
            seed_list = seed_accounts.get(provider_name)
            seed_account = self._match_seed_for_linuxdo(seed_list, linuxdo_name, account_index) if seed_list else None
            if seed_account and used_seed_identities is not None:
                seed_identity = self._build_seed_identity(seed_account)
                if seed_identity:
                    used_seed_identities.add(seed_identity)
            if seed_account:
                logger.info(f"[{account_name}] 发现 NEWAPI_ACCOUNTS seed（api_user={seed_account.api_user}），优先尝试")
                try:
                    seed_result = await self._checkin_newapi(seed_account, provider, account_name)
                    if seed_result.status == CheckinStatus.SUCCESS:
                        seed_result.message = f"{seed_result.message} (NEWAPI_ACCOUNTS seed)"
                        if seed_result.details is None:
                            seed_result.details = {}
                        seed_result.details["login_method"] = "newapi_accounts_seed"
                        results.append(seed_result)
                        # seed 成功后同步写入持久化缓存
                        seed_session = self._extract_session_cookie(seed_account.cookies)
                        if seed_session and seed_account.api_user:
                            seed_cookies = (
                                seed_account.cookies
                                if isinstance(seed_account.cookies, dict)
                                else {"session": seed_session}
                            )
                            self._cookie_cache.save(
                                provider_name,
                                account_name,
                                seed_session,
                                str(seed_account.api_user),
                                cookies=seed_cookies,
                            )
                        logger.success(f"[{account_name}] NEWAPI_ACCOUNTS seed 签到成功")
                        continue

                    seed_msg = seed_result.message or ""
                    if "401" in seed_msg or "403" in seed_msg or "过期" in seed_msg:
                        logger.warning(f"[{account_name}] NEWAPI_ACCOUNTS seed 已失效，继续尝试缓存/OAuth")
                    else:
                        logger.warning(f"[{account_name}] NEWAPI_ACCOUNTS seed 失败: {seed_msg}")
                        results.append(seed_result)
                        continue
                except Exception as e:
                    logger.warning(f"[{account_name}] NEWAPI_ACCOUNTS seed 尝试异常: {e}")

            # 2. 尝试 GitHub 持久化缓存 Cookie
            cached = self._cookie_cache.get(provider_name, account_name)
            if cached:
                stats["cookie_hit"] = int(stats["cookie_hit"]) + 1
                logger.info(f"[{account_name}] 发现缓存Cookie，尝试Cookie+API签到...")
                try:
                    cached_account = AnyRouterAccount(
                        cookies=(
                            cached.get("cookies")
                            if isinstance(cached.get("cookies"), dict)
                            else {"session": cached["session"]}
                        ),
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
                        stats["cookie_success"] = int(stats["cookie_success"]) + 1
                        logger.success(f"[{account_name}] 缓存Cookie签到成功！")
                        continue

                    # Cookie 过期，清除缓存，需要 OAuth
                    msg = result.message or ""
                    if "401" in msg or "403" in msg or "过期" in msg:
                        logger.warning(f"[{account_name}] 缓存Cookie已失效，需要重新OAuth")
                        self._cookie_cache.invalidate(provider_name, account_name)
                        stats["cookie_invalidated"] = int(stats["cookie_invalidated"]) + 1
                    else:
                        logger.warning(f"[{account_name}] 签到失败: {msg}")
                        results.append(result)
                        continue
                except Exception as e:
                    logger.warning(f"[{account_name}] 缓存Cookie签到异常: {e}")
                    self._cookie_cache.invalidate(provider_name, account_name)
                    stats["cookie_invalidated"] = int(stats["cookie_invalidated"]) + 1

            # 3. seed/cache 均不可用，标记为需要 OAuth
            need_oauth.append({
                "provider": provider,
                "provider_name": provider_name,
                "account_name": account_name,
            })
        stats["oauth_needed"] = len(need_oauth)

        if not need_oauth:
            logger.info("所有站点均通过缓存Cookie完成，无需 OAuth")
            try:
                logger.info("共享会话: 关闭浏览器")
                await browser_mgr.close()
            except Exception as e:
                logger.debug(f"关闭共享浏览器失败（可忽略）: {e}")
            self._log_auto_oauth_summary(stats, results)
            return results

        # 3. 优先共享会话 OAuth；失败再回退逐站独立浏览器
        debug_mode = self._is_debug_mode()
        site_timeout = self._env_int(
            "OAUTH_SITE_TIMEOUT_SHARED",
            self._env_int("OAUTH_SITE_TIMEOUT", 180 if debug_mode else 150, min_value=60),
            min_value=60,
        )
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
                        stats["oauth_success"] = int(stats["oauth_success"]) + 1
                        if result.details:
                            cached_session = result.details.pop("_cached_session", None)
                            cached_api_user = result.details.pop("_cached_api_user", None)
                            cached_cookies = result.details.pop("_cached_cookies", None)
                            if cached_session and cached_api_user:
                                self._cookie_cache.save(
                                    provider_name,
                                    account_name,
                                    cached_session,
                                    cached_api_user,
                                    cookies=(
                                        cached_cookies
                                        if isinstance(cached_cookies, dict)
                                        else {"session": cached_session}
                                    ),
                                )
                                logger.success(f"[{account_name}] 新Cookie已缓存")
                    else:
                        stats["oauth_failed"] = int(stats["oauth_failed"]) + 1
                        if self._is_retryable_network_message(result.message or ""):
                            stats["oauth_network_failed"] = int(stats["oauth_network_failed"]) + 1
                        logger.warning(f"[{account_name}] OAuth 签到失败: {result.message}")

                    results.append(result)

                except asyncio.TimeoutError:
                    logger.error(f"[{account_name}] 超时（>{site_timeout}s），跳过")
                    stats["oauth_failed"] = int(stats["oauth_failed"]) + 1
                    results.append(CheckinResult(
                        platform=f"NewAPI ({provider_name})",
                        account=account_name,
                        status=CheckinStatus.FAILED,
                        message=f"OAuth 超时（>{site_timeout}s）",
                    ))
                except Exception as e:
                    logger.error(f"[{account_name}] OAuth 异常: {e}")
                    stats["oauth_failed"] = int(stats["oauth_failed"]) + 1
                    if self._is_retryable_network_error(e):
                        stats["oauth_network_failed"] = int(stats["oauth_network_failed"]) + 1
                    results.append(CheckinResult(
                        platform=f"NewAPI ({provider_name})",
                        account=account_name,
                        status=CheckinStatus.FAILED,
                        message=f"OAuth 异常: {str(e)}",
                    ))
        else:
            logger.warning(f"共享会话不可用，回退为逐站独立浏览器 OAuth（{len(need_oauth)} 个站点）")
            await self._run_newapi_oauth_fallback(
                need_oauth, linuxdo_username, linuxdo_password, results, stats
            )

        try:
            logger.info("共享会话: 关闭浏览器")
            await browser_mgr.close()
        except Exception as e:
            logger.debug(f"关闭共享浏览器失败（可忽略）: {e}")

        self._log_auto_oauth_summary(stats, results)
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

                if not session_cookie:
                    return CheckinResult(
                        platform=f"NewAPI ({provider_name})",
                        account=account_name,
                        status=CheckinStatus.FAILED,
                        message="OAuth 登录失败，无法获取 session",
                        details={
                            "failure_kind": "session_missing",
                            "runtime_cookie_keys": sorted(list(checker.get_runtime_cookies().keys())),
                        },
                    )

                # 用获取到的 session 签到
                logger.info(f"[{account_name}] 使用新 session 签到...")
                runtime_cookies = checker.get_runtime_cookies()
                success, message, details = await checker._checkin_with_cookies(
                    session_cookie,
                    api_user,
                    extra_cookies=runtime_cookies,
                )

                details["login_method"] = "shared_oauth"
                details["_cached_session"] = session_cookie
                details["_cached_api_user"] = (api_user or details.get("resolved_api_user") or "")
                details["_cached_cookies"] = runtime_cookies or {"session": session_cookie}

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
        results: list[CheckinResult], stats: dict[str, int | str] | None = None,
    ) -> None:
        """回退模式：共享会话失败时，逐站独立启动浏览器"""
        from platforms.newapi_browser import browser_checkin_newapi

        debug_mode = self._is_debug_mode()
        site_timeout = self._env_int(
            "OAUTH_SITE_TIMEOUT_FALLBACK",
            self._env_int("OAUTH_SITE_TIMEOUT", 220 if debug_mode else 180, min_value=60),
            min_value=60,
        )
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
                        cached_cookies = result.details.pop("_cached_cookies", None)
                        if cached_session and cached_api_user:
                            self._cookie_cache.save(
                                provider_name,
                                account_name,
                                cached_session,
                                cached_api_user,
                                cookies=(
                                    cached_cookies
                                    if isinstance(cached_cookies, dict)
                                    else {"session": cached_session}
                                ),
                            )

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
            if stats is not None:
                if final_result.status == CheckinStatus.SUCCESS:
                    stats["oauth_success"] = int(stats.get("oauth_success", 0)) + 1
                else:
                    stats["oauth_failed"] = int(stats.get("oauth_failed", 0)) + 1
                    if self._is_retryable_network_message(final_result.message or ""):
                        stats["oauth_network_failed"] = int(stats.get("oauth_network_failed", 0)) + 1

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
            logger.info(f"[{account_name}] 优先使用 GitHub 持久化Cookie，其次 NEWAPI_ACCOUNTS Cookie")

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

            # ===== 1) GitHub 持久化缓存 Cookie 优先 =====
            cached = self._cookie_cache.get(provider_name, account_name)
            if cached:
                logger.info(f"[{account_name}] 检测到持久化Cookie，优先尝试")
                try:
                    cached_account = AnyRouterAccount(
                        cookies=(
                            cached.get("cookies")
                            if isinstance(cached.get("cookies"), dict)
                            else {"session": cached["session"]}
                        ),
                        api_user=cached["api_user"],
                        provider=account.provider,
                        name=account.name,
                    )
                    cached_result = await self._checkin_newapi(cached_account, provider, account_name)
                    if cached_result.status == CheckinStatus.SUCCESS:
                        cached_result.message = f"{cached_result.message} (GitHub持久化Cookie)"
                        if cached_result.details is None:
                            cached_result.details = {}
                        cached_result.details["login_method"] = "github_persisted_cookie"
                        results.append(cached_result)
                        logger.success(f"[{account_name}] 持久化Cookie签到成功")
                        continue

                    msg = cached_result.message or ""
                    if "401" in msg or "403" in msg or "过期" in msg:
                        logger.warning(f"[{account_name}] 持久化Cookie已失效，删除缓存")
                        self._cookie_cache.invalidate(provider_name, account_name)
                    else:
                        logger.warning(f"[{account_name}] 持久化Cookie尝试失败，继续用配置Cookie: {msg}")
                except Exception as e:
                    logger.warning(f"[{account_name}] 持久化Cookie尝试异常，删除缓存后继续: {e}")
                    self._cookie_cache.invalidate(provider_name, account_name)

            try:
                result = await self._checkin_newapi(account, provider, account_name)

                # 检查是否需要浏览器回退（401/403 错误）
                if result.status == CheckinStatus.FAILED:
                    msg = result.message or ""
                    if "401" in msg or "403" in msg or "过期" in msg:
                        logger.warning(f"[{account_name}] 配置Cookie失效，准备回退处理")

                        # 若当前是覆盖cookie，先删除覆盖并恢复 NEWAPI_ACCOUNTS 原始值再试一次
                        if id(account) in self._newapi_override_applied_accounts:
                            logger.warning(f"[{account_name}] 当前为覆盖Cookie且已失效，删除覆盖并恢复原始配置重试")
                            self._remove_newapi_account_override(account, provider_name)
                            restored = self._restore_newapi_account_original(account)
                            if restored:
                                # 覆盖失效时，相关持久化缓存也同步清理
                                self._cookie_cache.invalidate(provider_name, account_name)
                                try:
                                    restored_result = await self._checkin_newapi(account, provider, account_name)
                                    if restored_result.status == CheckinStatus.SUCCESS:
                                        restored_result.message = f"{restored_result.message} (恢复原始NEWAPI_ACCOUNTS)"
                                        if restored_result.details is None:
                                            restored_result.details = {}
                                        restored_result.details["login_method"] = "newapi_accounts_restored"
                                        results.append(restored_result)
                                        logger.success(f"[{account_name}] 恢复原始配置Cookie后签到成功")
                                        continue
                                    msg2 = restored_result.message or ""
                                    if "401" not in msg2 and "403" not in msg2 and "过期" not in msg2:
                                        results.append(restored_result)
                                        continue
                                except Exception as e:
                                    logger.warning(f"[{account_name}] 恢复原始配置后重试异常: {e}")

                        # NEWAPI_ACCOUNTS 当前 cookie 失败后，最后再尝试一次本地缓存 cookie（若仍存在）
                        cached = self._cookie_cache.get(provider_name, account_name)
                        if cached:
                            logger.info(f"[{account_name}] 检测到缓存Cookie，作为最终兜底再尝试一次")
                            try:
                                cached_account = AnyRouterAccount(
                                    cookies=(
                                        cached.get("cookies")
                                        if isinstance(cached.get("cookies"), dict)
                                        else {"session": cached["session"]}
                                    ),
                                    api_user=cached["api_user"],
                                    provider=account.provider,
                                    name=account.name,
                                )
                                cached_result = await self._checkin_newapi(cached_account, provider, account_name)
                                if cached_result.status == CheckinStatus.SUCCESS:
                                    cached_result.message = f"{cached_result.message} (缓存Cookie最终兜底)"
                                    if cached_result.details is None:
                                        cached_result.details = {}
                                    cached_result.details["login_method"] = "cached_cookie_last_fallback"
                                    results.append(cached_result)
                                    logger.success(f"[{account_name}] 缓存Cookie最终兜底签到成功！")
                                    continue
                                msg3 = cached_result.message or ""
                                if "401" in msg3 or "403" in msg3 or "过期" in msg3:
                                    self._cookie_cache.invalidate(provider_name, account_name)
                            except Exception as e:
                                logger.warning(f"[{account_name}] 缓存Cookie兜底异常: {e}")

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
                        cached_cookies = result.details.pop("_cached_cookies", None)
                        if cached_session and cached_api_user:
                            self._cookie_cache.save(
                                provider.name,
                                account_name,
                                cached_session,
                                cached_api_user,
                                cookies=(
                                    cached_cookies
                                    if isinstance(cached_cookies, dict)
                                    else {"session": cached_session}
                                ),
                            )
                            logger.success(
                                f"[{account_name}] 新Cookie已缓存，下次将优先使用Cookie+API方式"
                            )
                            # 同步覆盖 NEWAPI_ACCOUNTS（通过覆盖文件持久化）
                            self._persist_newapi_account_override(
                                account=account,
                                account_name=account_name,
                                provider_name=provider.name,
                                session_cookie=cached_session,
                                api_user=cached_api_user,
                                cookies=(
                                    cached_cookies
                                    if isinstance(cached_cookies, dict)
                                    else {"session": cached_session}
                                ),
                                source="oauth_refresh",
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
        # 提取 cookie（优先使用完整 cookie bundle，至少包含 session）
        cookies: dict[str, str] = {}
        if isinstance(account.cookies, dict):
            cookies = {
                str(k): str(v)
                for k, v in account.cookies.items()
                if k and v is not None and str(v).strip()
            }

        session_cookie = cookies.get("session") or self._extract_session_cookie(account.cookies)
        if not session_cookie:
            return CheckinResult(
                platform=f"NewAPI ({provider.name})",
                account=account_name,
                status=CheckinStatus.FAILED,
                message="无效的 session cookie",
            )
        if "session" not in cookies:
            cookies["session"] = session_cookie

        # 构建请求
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/138.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": provider.domain,
            "Origin": provider.domain,
            provider.api_user_key: str(account.api_user),
        }

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

                # ---- 辅助：浏览器内 GET 用户信息 ----
                async def _fetch_user_info_in_browser():
                    """在浏览器内获取用户信息，返回 (quota, used_quota) 或 None"""
                    try:
                        r = await page.evaluate(f"""
                            async () => {{
                                const r = await fetch('{provider.user_info_path}', {{
                                    headers: {json.dumps(fetch_headers)}
                                }});
                                return {{ status: r.status, text: await r.text() }};
                            }}
                        """)
                        if r["status"] == 200:
                            d = json.loads(r["text"])
                            if d.get("success"):
                                ud = d.get("data", {})
                                return (
                                    round(ud.get("quota", 0) / 500000, 2),
                                    round(ud.get("used_quota", 0) / 500000, 2),
                                )
                        else:
                            logger.warning(f"[{account_name}] 获取用户信息失败: HTTP {r['status']}")
                    except Exception as e:
                        logger.warning(f"[{account_name}] 获取用户信息失败: {e}")
                    return None

                # 1. 获取签到前余额
                pre_info = await _fetch_user_info_in_browser()
                pre_quota: float | None = None
                if pre_info:
                    pre_quota, used_quota = pre_info
                    details["balance"] = f"${pre_quota}"
                    details["used"] = f"${used_quota}"
                    logger.info(f"[{account_name}] 签到前余额: ${pre_quota}, 已用: ${used_quota}")

                # 2. 执行签到（如果需要）
                if provider.needs_manual_check_in():
                    sign_in_path = provider.sign_in_path
                    # 签到 POST 请求需要额外的 Content-Type 和 X-Requested-With 头
                    checkin_fetch_headers = {**fetch_headers, "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}
                    try:
                        resp = await page.evaluate(f"""
                            async () => {{
                                const r = await fetch('{sign_in_path}', {{
                                    method: 'POST',
                                    headers: {json.dumps(checkin_fetch_headers)}
                                }});
                                return {{ status: r.status, text: await r.text() }};
                            }}
                        """)
                        logger.debug(f"[{account_name}] 签到响应: status={resp['status']}, body={resp['text'][:200]}")

                        if resp["status"] == 200:
                            try:
                                result = json.loads(resp["text"])
                                msg = result.get("message") or result.get("msg") or ""
                                if result.get("success") or result.get("ret") == 1 or result.get("code") == 0:
                                    msg = msg or "签到成功"

                                    # 3. 签到后验证：二次查询余额确认签到真实性
                                    post_info = await _fetch_user_info_in_browser()
                                    if post_info and pre_quota is not None:
                                        post_quota, post_used = post_info
                                        delta = round(post_quota - pre_quota, 2)
                                        details["balance"] = f"${post_quota}"
                                        details["used"] = f"${post_used}"
                                        if delta > 0:
                                            details["checkin_reward"] = f"+${delta}"
                                            logger.success(f"[{account_name}] ✅ 签到验证通过: 余额 ${pre_quota} → ${post_quota} (奖励 +${delta})")
                                        elif delta == 0:
                                            logger.warning(f"[{account_name}] ⚠️ 签到API返回成功但余额未变: ${pre_quota} → ${post_quota}")
                                            details["checkin_verify"] = "余额未变(可能已签到过)"
                                        else:
                                            logger.warning(f"[{account_name}] ⚠️ 签到后余额反而减少: ${pre_quota} → ${post_quota}")
                                    elif post_info:
                                        post_quota, post_used = post_info
                                        details["balance"] = f"${post_quota}"
                                        details["used"] = f"${post_used}"
                                        logger.info(f"[{account_name}] 签到后余额: ${post_quota}")

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

                        logger.error(f"[{account_name}] 签到失败: HTTP {resp['status']}, body={resp['text'][:200]}")
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
