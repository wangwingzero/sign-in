#!/usr/bin/env python3
"""
平台管理器

协调所有平台的签到任务，汇总结果并发送通知。

简化版：
- linuxdo: 浏览 LinuxDO 帖子
- newapi: 所有 NewAPI 架构站点的签到（使用 Cookie + API）
"""

import httpx
from loguru import logger

from platforms.base import CheckinResult, CheckinStatus
from platforms.linuxdo import LinuxDOAdapter
from utils.config import AppConfig, DEFAULT_PROVIDERS
from utils.notify import NotificationManager


class PlatformManager:
    """平台管理器"""

    def __init__(self, config: AppConfig):
        self.config = config
        self.notify = NotificationManager()
        self.results: list[CheckinResult] = []

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
            adapter = LinuxDOAdapter(
                username=account.username,
                password=account.password,
                browse_count=account.browse_count,
                account_name=account.get_display_name(i),
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
        """运行所有 NewAPI 站点签到（纯 Cookie + API 方式）"""
        if not self.config.anyrouter_accounts:
            return []

        results = []
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

            try:
                result = await self._checkin_newapi(account, provider, account_name)
                results.append(result)
            except Exception as e:
                logger.error(f"[{account_name}] 签到异常: {e}")
                results.append(CheckinResult(
                    platform=f"NewAPI ({provider_name})",
                    account=account_name,
                    status=CheckinStatus.FAILED,
                    message=f"签到异常: {str(e)}",
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

        async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
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
                            # 检查各种成功标志
                            if result.get("success") or result.get("ret") == 1 or result.get("code") == 0:
                                msg = result.get("message") or result.get("msg") or "签到成功"
                                logger.success(f"[{account_name}] {msg}")
                                return CheckinResult(
                                    platform=f"NewAPI ({provider.name})",
                                    account=account_name,
                                    status=CheckinStatus.SUCCESS,
                                    message=msg,
                                    details=details if details else None,
                                )
                            else:
                                error_msg = result.get("message") or result.get("msg") or "签到失败"
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

    def send_summary_notification(self, force: bool = False) -> None:
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
