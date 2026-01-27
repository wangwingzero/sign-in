#!/usr/bin/env python3
"""
平台管理器

协调所有平台的签到任务，汇总结果并发送通知。

Requirements:
- 6.2: 支持运行所有平台签到
- 6.3: 支持运行指定平台签到
- 6.4: 发送汇总通知
"""

import asyncio
import random
from typing import Optional

from loguru import logger

from platforms.anyrouter import AnyRouterAdapter
from platforms.base import CheckinResult, CheckinStatus
from platforms.linuxdo import LinuxDoAdapter
from platforms.wong import WongAdapter
from utils.config import AppConfig
from utils.notify import NotificationManager


class PlatformManager:
    """平台管理器
    
    协调所有签到任务，支持按平台筛选和汇总通知。
    """
    
    def __init__(self, config: AppConfig):
        """初始化平台管理器
        
        Args:
            config: 应用配置
        """
        self.config = config
        self.notify = NotificationManager()
        self.results: list[CheckinResult] = []
    
    async def run_all(self) -> list[CheckinResult]:
        """运行所有平台签到
        
        Returns:
            list[CheckinResult]: 所有签到结果
        """
        self.results = []
        
        # LinuxDo (多账号)
        linuxdo_results = await self._run_all_linuxdo()
        self.results.extend(linuxdo_results)
        
        # WONG 公益站 (仅 WONG_ACCOUNTS 配置的)
        wong_results = await self._run_wong_accounts_only()
        self.results.extend(wong_results)
        
        # AnyRouter 类平台（包括 provider=wong 和 provider=anyrouter）
        anyrouter_results = await self._run_all_anyrouter()
        self.results.extend(anyrouter_results)
        
        return self.results
    
    async def run_platform(self, platform: str) -> list[CheckinResult]:
        """运行指定平台签到
        
        Args:
            platform: 平台名称 ("linuxdo", "wong" 或 "anyrouter")
        
        Returns:
            list[CheckinResult]: 签到结果
        
        Raises:
            ValueError: 未知平台名称
        """
        self.results = []
        
        platform_lower = platform.lower()
        
        if platform_lower == "linuxdo":
            linuxdo_results = await self._run_all_linuxdo()
            self.results.extend(linuxdo_results)
        elif platform_lower == "wong":
            wong_results = await self._run_all_wong()
            self.results.extend(wong_results)
        elif platform_lower == "anyrouter":
            anyrouter_results = await self._run_all_anyrouter()
            self.results.extend(anyrouter_results)
        else:
            raise ValueError(f"未知平台: {platform}")
        
        return self.results
    
    async def _run_all_linuxdo(self) -> list[CheckinResult]:
        """运行所有 LinuxDo 账号签到（串行执行，避免被检测为多账号关联）

        反检测策略：
        - 串行执行而非并发，避免同一时间多账号活动被关联
        - 账号执行顺序随机化，避免固定模式
        - 账号间添加随机间隔（10-30秒），模拟不同用户
        - 每个账号浏览时长随机 20-40 分钟
        """
        if not self.config.linuxdo_accounts:
            logger.warning("LinuxDo 未配置")
            return []

        # 随机打乱账号执行顺序
        accounts_with_index = list(enumerate(self.config.linuxdo_accounts))
        random.shuffle(accounts_with_index)
        logger.info(f"账号执行顺序已随机化")

        results = []
        total_accounts = len(accounts_with_index)

        for order, (original_index, account) in enumerate(accounts_with_index):
            logger.info(f"开始执行 LinuxDo 账号 {order + 1}/{total_accounts}: {account.get_display_name(original_index)}")

            # 随机化浏览时长：20-40 分钟（1200-2400秒）
            randomized_duration = random.randint(1200, 2400)
            logger.info(f"本次浏览目标时长: {randomized_duration // 60} 分 {randomized_duration % 60} 秒")

            adapter = LinuxDoAdapter(
                username=account.username,
                password=account.password,
                browse_enabled=account.browse_enabled,
                browse_duration=randomized_duration,
                account_name=account.get_display_name(original_index),
            )

            try:
                result = await adapter.run()
                results.append(result)
            except Exception as e:
                logger.error(f"LinuxDo 账号 {order + 1} 执行异常: {e}")
                results.append(CheckinResult(
                    platform="LinuxDo",
                    account=account.get_display_name(original_index),
                    status=CheckinStatus.FAILED,
                    message=f"执行异常: {str(e)}",
                ))

            # 账号间随机间隔（最后一个账号不需要等待）
            if order < total_accounts - 1:
                delay = random.uniform(10, 30)
                logger.info(f"等待 {delay:.1f} 秒后执行下一个账号...")
                await asyncio.sleep(delay)

        return results
    
    async def _run_linuxdo(self) -> CheckinResult:
        """运行 LinuxDo 签到（向后兼容，运行第一个账号）"""
        results = await self._run_all_linuxdo()
        if results:
            return results[0]
        return CheckinResult(
            platform="LinuxDo",
            account="N/A",
            status=CheckinStatus.SKIPPED,
            message="未配置 LinuxDo 账号",
        )
    
    async def _run_wong_accounts_only(self) -> list[CheckinResult]:
        """运行 WONG_ACCOUNTS 环境变量配置的账号（不包括 ANYROUTER_ACCOUNTS 里的）"""
        if not self.config.wong_accounts:
            return []
        
        results = []
        for i, account in enumerate(self.config.wong_accounts):
            logger.info(f"开始执行 WONG 账号 {i + 1}: {account.get_display_name(i)}")
            
            adapter = WongAdapter(
                linuxdo_username=account.linuxdo_username,
                linuxdo_password=account.linuxdo_password,
                fallback_cookies=account.fallback_cookies,
                api_user=account.api_user,
                account_name=account.get_display_name(i),
            )
            
            try:
                result = await adapter.run()
                results.append(result)
            except Exception as e:
                logger.error(f"WONG 账号 {i + 1} 执行异常: {e}")
                results.append(CheckinResult(
                    platform="WONG公益站",
                    account=account.get_display_name(i),
                    status=CheckinStatus.FAILED,
                    message=f"执行异常: {str(e)}",
                ))
        
        return results
    
    async def _run_all_wong(self) -> list[CheckinResult]:
        """运行所有 WONG 公益站账号签到（用于 --platform wong）
        
        包括：
        1. WONG_ACCOUNTS 环境变量配置的账号
        2. ANYROUTER_ACCOUNTS 中 provider=wong 的账号
        """
        results = []
        
        # 1. 处理 WONG_ACCOUNTS 配置的账号
        wong_accounts_results = await self._run_wong_accounts_only()
        results.extend(wong_accounts_results)
        
        # 2. 处理 ANYROUTER_ACCOUNTS 中 provider=wong 的账号
        wong_from_anyrouter = [acc for acc in self.config.anyrouter_accounts if acc.provider == "wong"]
        for i, account in enumerate(wong_from_anyrouter):
            logger.info(f"开始执行 WONG 账号 (from ANYROUTER): {account.get_display_name(i)}")
            
            session_cookie = self._extract_session_cookie(account.cookies)
            
            adapter = WongAdapter(
                fallback_cookies=session_cookie,
                api_user=account.api_user,
                account_name=account.get_display_name(i),
            )
            
            try:
                result = await adapter.run()
                results.append(result)
            except Exception as e:
                logger.error(f"WONG 账号 {account.get_display_name(i)} 执行异常: {e}")
                results.append(CheckinResult(
                    platform="WONG公益站",
                    account=account.get_display_name(i),
                    status=CheckinStatus.FAILED,
                    message=f"执行异常: {str(e)}",
                ))
        
        if not results:
            logger.info("WONG 公益站未配置")
        
        return results
    
    async def _run_all_anyrouter(self) -> list[CheckinResult]:
        """运行所有 ANYROUTER_ACCOUNTS 账号签到，根据 provider 自动选择适配器"""
        results = []
        
        for i, account in enumerate(self.config.anyrouter_accounts):
            # 根据 provider 选择适配器
            if account.provider == "wong":
                # 使用 WongAdapter
                logger.info(f"开始执行 WONG 账号: {account.get_display_name(i)}")
                session_cookie = self._extract_session_cookie(account.cookies)
                
                adapter = WongAdapter(
                    fallback_cookies=session_cookie,
                    api_user=account.api_user,
                    account_name=account.get_display_name(i),
                )
                
                try:
                    result = await adapter.run()
                    results.append(result)
                except Exception as e:
                    logger.error(f"WONG 账号 {account.get_display_name(i)} 执行异常: {e}")
                    results.append(CheckinResult(
                        platform="WONG公益站",
                        account=account.get_display_name(i),
                        status=CheckinStatus.FAILED,
                        message=f"执行异常: {str(e)}",
                    ))
            else:
                # 使用 AnyRouterAdapter
                provider = self.config.providers.get(account.provider)
                if not provider:
                    logger.warning(f"Provider '{account.provider}' 未找到，跳过账号 {i + 1}")
                    results.append(CheckinResult(
                        platform=f"AnyRouter ({account.provider})",
                        account=account.get_display_name(i),
                        status=CheckinStatus.SKIPPED,
                        message=f"Provider '{account.provider}' 未配置",
                    ))
                    continue
                
                adapter = AnyRouterAdapter(
                    account=account,
                    provider_config=provider,
                    account_index=i,
                )
                
                result = await adapter.run()
                results.append(result)
        
        return results
    
    def _extract_session_cookie(self, cookies) -> str:
        """从 cookies 中提取 session 值"""
        if isinstance(cookies, dict):
            return cookies.get("session", "")
        if isinstance(cookies, str):
            return cookies
        return ""
    
    def send_summary_notification(self, force: bool = False) -> None:
        """发送签到汇总通知
        
        Args:
            force: 是否强制发送（即使全部成功）
        """
        if not self.results:
            logger.info("没有签到结果，跳过通知")
            return
        
        # 格式化通知内容
        results_dicts = [r.to_dict() for r in self.results]
        title, text_content, html_content = NotificationManager.format_summary_message(results_dicts)
        
        # 发送通知（使用 HTML 格式）
        with self.notify:
            self.notify.push_message(title, html_content, msg_type="html")
    
    def _check_balance_change(self) -> bool:
        """检查是否有余额变化
        
        TODO: 实现余额变化检测逻辑（需要持久化上次余额）
        """
        # 暂时返回 False，后续可以实现余额变化检测
        return False
    
    def get_exit_code(self) -> int:
        """获取退出码
        
        Returns:
            int: 0 表示至少有一个成功，1 表示全部失败或无配置
        """
        if not self.results:
            return 1
        
        success_count = sum(1 for r in self.results if r.is_success)
        return 0 if success_count > 0 else 1
    
    @property
    def success_count(self) -> int:
        """成功数量"""
        return sum(1 for r in self.results if r.is_success)
    
    @property
    def failed_count(self) -> int:
        """失败数量"""
        return sum(1 for r in self.results if r.status == CheckinStatus.FAILED)
    
    @property
    def skipped_count(self) -> int:
        """跳过数量"""
        return sum(1 for r in self.results if r.status == CheckinStatus.SKIPPED)
    
    @property
    def total_count(self) -> int:
        """总数量"""
        return len(self.results)
