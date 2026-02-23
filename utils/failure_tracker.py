#!/usr/bin/env python3
"""
连续失败跟踪器

记录每个 provider+account 组合的连续失败次数。
当连续失败次数达到阈值时自动跳过该站点，节省浏览器资源和 CI 时间。
一旦签到成功，计数器自动清零。

存储路径: .newapi_failure_tracker.json（通过 GitHub Actions cache 持久化）
"""

import json
import os
import tempfile
from datetime import datetime, timezone

from loguru import logger

DEFAULT_TRACKER_FILE = ".newapi_failure_tracker.json"


class FailureTracker:
    """连续失败跟踪器

    JSON 结构示例::

        {
            "hotaru:主账号_hotaru": {
                "consecutive_failures": 3,
                "last_failure_reason": "OAuth 登录失败...",
                "last_failure_at": "2026-02-23T08:00:00+00:00",
                "last_success_at": "2026-02-20T08:00:00+00:00"
            }
        }
    """

    def __init__(self, file_path: str | None = None):
        self._file_path = file_path or os.getenv("FAILURE_TRACKER_FILE", DEFAULT_TRACKER_FILE)
        self._data: dict[str, dict] = {}
        self.load()

    @staticmethod
    def _make_key(provider: str, account_name: str) -> str:
        """生成 provider:account_name 形式的唯一 key"""
        return f"{provider}:{account_name}"

    def load(self) -> None:
        """从文件加载跟踪数据，文件不存在或损坏时初始化为空"""
        if not os.path.exists(self._file_path):
            self._data = {}
            return
        try:
            with open(self._file_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._data = data
            else:
                logger.warning(f"[FailureTracker] 文件格式异常（非 dict），已重置: {self._file_path}")
                self._data = {}
        except Exception as e:
            logger.warning(f"[FailureTracker] 加载失败，已重置: {e}")
            self._data = {}

    def save(self) -> None:
        """原子写入跟踪数据到文件"""
        try:
            target_dir = os.path.dirname(self._file_path) or "."
            os.makedirs(target_dir, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", delete=False, dir=target_dir, encoding="utf-8", suffix=".tmp"
            ) as tmp:
                json.dump(self._data, tmp, ensure_ascii=False, indent=2)
                tmp_path = tmp.name
            os.replace(tmp_path, self._file_path)
        except Exception as e:
            logger.warning(f"[FailureTracker] 保存失败: {e}")

    def record_failure(self, provider: str, account_name: str, reason: str = "") -> int:
        """记录一次失败，递增连续失败计数

        Returns:
            递增后的连续失败次数
        """
        key = self._make_key(provider, account_name)
        entry = self._data.get(key, {})
        count = entry.get("consecutive_failures", 0) + 1
        entry["consecutive_failures"] = count
        entry["last_failure_reason"] = reason[:200] if reason else ""
        entry["last_failure_at"] = datetime.now(timezone.utc).isoformat()
        # 保留上次成功时间
        if "last_success_at" not in entry:
            entry["last_success_at"] = None
        self._data[key] = entry
        return count

    def record_success(self, provider: str, account_name: str) -> None:
        """记录一次成功，清零连续失败计数"""
        key = self._make_key(provider, account_name)
        entry = self._data.get(key, {})
        entry["consecutive_failures"] = 0
        entry["last_failure_reason"] = ""
        entry["last_success_at"] = datetime.now(timezone.utc).isoformat()
        self._data[key] = entry

    def should_skip(self, provider: str, account_name: str, threshold: int = 3) -> bool:
        """判断是否应跳过该 provider+account（连续失败 >= threshold）"""
        return self.get_failure_count(provider, account_name) >= threshold

    def get_failure_count(self, provider: str, account_name: str) -> int:
        """获取连续失败次数"""
        key = self._make_key(provider, account_name)
        entry = self._data.get(key, {})
        return entry.get("consecutive_failures", 0)

    def get_skip_summary(self, threshold: int = 3) -> dict[str, dict]:
        """返回所有被跳过站点的摘要（连续失败 >= threshold）

        Returns:
            {key: {consecutive_failures, last_failure_reason, last_failure_at}} 的子集
        """
        return {
            key: {
                "consecutive_failures": entry.get("consecutive_failures", 0),
                "last_failure_reason": entry.get("last_failure_reason", ""),
                "last_failure_at": entry.get("last_failure_at", ""),
            }
            for key, entry in self._data.items()
            if entry.get("consecutive_failures", 0) >= threshold
        }
