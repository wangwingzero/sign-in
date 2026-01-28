#!/usr/bin/env python3
"""
平台适配器基类

定义所有平台适配器的公共接口和数据结构。

Requirements:
- 2.1: 定义统一的平台适配器接口
- 2.2: 定义签到结果数据结构
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional

from loguru import logger


# 北京时间时区 (UTC+8)
BEIJING_TZ = timezone(timedelta(hours=8))


def get_beijing_time() -> datetime:
    """获取北京时间"""
    return datetime.now(BEIJING_TZ)


class CheckinStatus(Enum):
    """签到状态枚举"""
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class CheckinResult:
    """签到结果数据类
    
    Attributes:
        platform: 平台名称
        account: 账号标识
        status: 签到状态
        message: 状态消息
        details: 额外信息（如余额、Connect 信息等）
        timestamp: 签到时间戳
    """
    platform: str
    account: str
    status: CheckinStatus
    message: str
    details: Optional[dict] = None
    timestamp: datetime = field(default_factory=get_beijing_time)
    
    def to_dict(self) -> dict:
        """转换为字典格式（用于通知）"""
        return {
            "platform": self.platform,
            "account": self.account,
            "status": self.status.value,
            "message": self.message,
            "details": self.details,
            "timestamp": self.timestamp.isoformat(),
        }
    
    @property
    def is_success(self) -> bool:
        """是否成功"""
        return self.status == CheckinStatus.SUCCESS


class BasePlatformAdapter(ABC):
    """平台适配器基类
    
    所有平台适配器必须继承此类并实现抽象方法。
    提供统一的签到流程：login -> checkin -> cleanup
    """
    
    @property
    @abstractmethod
    def platform_name(self) -> str:
        """返回平台名称"""
        pass
    
    @property
    @abstractmethod
    def account_name(self) -> str:
        """返回账号标识"""
        pass
    
    @abstractmethod
    async def login(self) -> bool:
        """执行登录操作
        
        Returns:
            bool: 登录是否成功
        """
        pass
    
    @abstractmethod
    async def checkin(self) -> CheckinResult:
        """执行签到操作
        
        Returns:
            CheckinResult: 签到结果
        """
        pass
    
    @abstractmethod
    async def get_status(self) -> dict:
        """获取账号状态信息
        
        Returns:
            dict: 状态信息（如余额、等级等）
        """
        pass
    
    async def cleanup(self) -> None:
        """清理资源（如关闭浏览器、HTTP 客户端等）
        
        子类可以重写此方法以实现特定的清理逻辑。
        """
        pass
    
    async def run(self) -> CheckinResult:
        """执行完整签到流程
        
        流程：login -> checkin -> cleanup
        
        Returns:
            CheckinResult: 签到结果
        """
        try:
            logger.info(f"[{self.platform_name}] 开始签到: {self.account_name}")
            
            # 登录
            if not await self.login():
                logger.error(f"[{self.platform_name}] 登录失败: {self.account_name}")
                return CheckinResult(
                    platform=self.platform_name,
                    account=self.account_name,
                    status=CheckinStatus.FAILED,
                    message="登录失败",
                )
            
            logger.info(f"[{self.platform_name}] 登录成功: {self.account_name}")
            
            # 签到
            result = await self.checkin()
            
            if result.is_success:
                logger.success(f"[{self.platform_name}] 签到成功: {self.account_name}")
            else:
                logger.warning(f"[{self.platform_name}] 签到失败: {self.account_name} - {result.message}")
            
            return result
            
        except Exception as e:
            logger.exception(f"[{self.platform_name}] 签到异常: {self.account_name}")
            return CheckinResult(
                platform=self.platform_name,
                account=self.account_name,
                status=CheckinStatus.FAILED,
                message=f"签到异常: {str(e)}",
            )
        finally:
            await self.cleanup()
    
    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(platform={self.platform_name}, account={self.account_name})"
