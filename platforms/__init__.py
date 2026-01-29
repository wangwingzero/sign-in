"""
平台适配器模块

提供多平台签到支持的适配器实现。
"""

from platforms.base import BasePlatformAdapter, CheckinResult, CheckinStatus
from platforms.anyrouter import AnyRouterAdapter
from platforms.manager import PlatformManager

__all__ = [
    "BasePlatformAdapter",
    "CheckinResult",
    "CheckinStatus",
    "AnyRouterAdapter",
    "PlatformManager",
]
