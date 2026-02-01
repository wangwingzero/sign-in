#!/usr/bin/env python3
"""
配置管理模块

统一管理所有平台的账号和设置配置。

Requirements:
- 3.1: 从环境变量加载配置
- 3.2: 支持 JSON 格式的多账号配置 (ANYROUTER_ACCOUNTS)
- 3.4: 验证必填字段并报告清晰的错误消息
- 3.5: 支持通过 PROVIDERS 环境变量自定义 provider 配置
- 3.6: 缺少必需配置时记录描述性错误并跳过该平台
"""

import json
import os
from dataclasses import dataclass, field
from typing import Literal

from loguru import logger


@dataclass
class AnyRouterAccount:
    """AnyRouter 账号配置"""

    cookies: dict | str
    api_user: str
    provider: str = "anyrouter"
    name: str | None = None

    @classmethod
    def from_dict(cls, data: dict, index: int) -> "AnyRouterAccount":
        """从字典创建 AnyRouterAccount"""
        provider = data.get("provider", "anyrouter")
        name = data.get("name") or f"Account {index + 1}"
        return cls(cookies=data["cookies"], api_user=data["api_user"], provider=provider, name=name)

    def get_display_name(self, index: int) -> str:
        return self.name if self.name else f"Account {index + 1}"

    def to_dict(self) -> dict:
        result = {"cookies": self.cookies, "api_user": self.api_user, "provider": self.provider}
        if self.name:
            result["name"] = self.name
        return result


@dataclass
class WongAccount:
    """WONG 公益站账号配置

    支持两种登录方式：
    1. LinuxDO OAuth（优先）
    2. Cookie 回退
    """

    linuxdo_username: str | None = None
    linuxdo_password: str | None = None
    fallback_cookies: str | None = None
    api_user: str | None = None
    name: str | None = None

    @classmethod
    def from_dict(cls, data: dict, index: int) -> "WongAccount":
        """从字典创建 WongAccount"""
        name = data.get("name") or f"WONG Account {index + 1}"
        return cls(
            linuxdo_username=data.get("linuxdo_username"),
            linuxdo_password=data.get("linuxdo_password"),
            fallback_cookies=data.get("fallback_cookies") or data.get("cookies"),
            api_user=data.get("api_user"),
            name=name,
        )

    def get_display_name(self, index: int) -> str:
        if self.name:
            return self.name
        if self.linuxdo_username:
            return self.linuxdo_username
        return f"WONG Account {index + 1}"


@dataclass
class ElysiverAccount:
    """Elysiver 账号配置

    支持两种登录方式：
    1. LinuxDO OAuth（优先）
    2. Cookie 回退
    """

    linuxdo_username: str | None = None
    linuxdo_password: str | None = None
    fallback_cookies: str | None = None
    api_user: str | None = None
    name: str | None = None

    @classmethod
    def from_dict(cls, data: dict, index: int) -> "ElysiverAccount":
        """从字典创建 ElysiverAccount"""
        name = data.get("name") or f"Elysiver Account {index + 1}"
        return cls(
            linuxdo_username=data.get("linuxdo_username"),
            linuxdo_password=data.get("linuxdo_password"),
            fallback_cookies=data.get("fallback_cookies") or data.get("cookies"),
            api_user=data.get("api_user"),
            name=name,
        )

    def get_display_name(self, index: int) -> str:
        if self.name:
            return self.name
        if self.linuxdo_username:
            return self.linuxdo_username
        return f"Elysiver Account {index + 1}"


@dataclass
class KFCAPIAccount:
    """KFC API 账号配置

    支持两种登录方式：
    1. LinuxDO OAuth（优先）
    2. Cookie 回退
    """

    linuxdo_username: str | None = None
    linuxdo_password: str | None = None
    fallback_cookies: str | None = None
    api_user: str | None = None
    name: str | None = None

    @classmethod
    def from_dict(cls, data: dict, index: int) -> "KFCAPIAccount":
        """从字典创建 KFCAPIAccount"""
        name = data.get("name") or f"KFC API Account {index + 1}"
        return cls(
            linuxdo_username=data.get("linuxdo_username"),
            linuxdo_password=data.get("linuxdo_password"),
            fallback_cookies=data.get("fallback_cookies") or data.get("cookies"),
            api_user=data.get("api_user"),
            name=name,
        )

    def get_display_name(self, index: int) -> str:
        if self.name:
            return self.name
        if self.linuxdo_username:
            return self.linuxdo_username
        return f"KFC API Account {index + 1}"


@dataclass
class DuckCodingAccount:
    """Free DuckCoding 账号配置

    支持两种登录方式：
    1. LinuxDO OAuth（优先）
    2. Cookie 回退
    """

    linuxdo_username: str | None = None
    linuxdo_password: str | None = None
    fallback_cookies: str | None = None
    api_user: str | None = None
    name: str | None = None

    @classmethod
    def from_dict(cls, data: dict, index: int) -> "DuckCodingAccount":
        """从字典创建 DuckCodingAccount"""
        name = data.get("name") or f"DuckCoding Account {index + 1}"
        return cls(
            linuxdo_username=data.get("linuxdo_username"),
            linuxdo_password=data.get("linuxdo_password"),
            fallback_cookies=data.get("fallback_cookies") or data.get("cookies"),
            api_user=data.get("api_user"),
            name=name,
        )

    def get_display_name(self, index: int) -> str:
        if self.name:
            return self.name
        if self.linuxdo_username:
            return self.linuxdo_username
        return f"DuckCoding Account {index + 1}"


# 支持的 NewAPI 站点列表
NEWAPI_SITES = {
    "wong": {
        "name": "WONG公益站",
        "domain": "https://wzw.pp.ua",
        "cookie_domain": "wzw.pp.ua",
        "currency": "$",
    },
    "elysiver": {
        "name": "Elysiver",
        "domain": "https://elysiver.h-e.top",
        "cookie_domain": "h-e.top",
        "currency": "E ",
    },
    "kfcapi": {
        "name": "KFC API",
        "domain": "https://kfc-api.sxxe.net",
        "cookie_domain": "kfc-api.sxxe.net",
        "currency": "$",
    },
    "duckcoding": {
        "name": "Free DuckCoding",
        "domain": "https://free.duckcoding.com",
        "cookie_domain": "free.duckcoding.com",
        "currency": "¥",
    },
    "runanytime": {
        "name": "随时跑路",
        "domain": "https://runanytime.hxi.me",
        "cookie_domain": "runanytime.hxi.me",
        "currency": "$",
    },
    "neb": {
        "name": "NEB公益站",
        "domain": "https://ai.zzhdsgsss.xyz",
        "cookie_domain": "ai.zzhdsgsss.xyz",
        "currency": "$",
    },
    "zeroliya": {
        "name": "小呆公益站",
        "domain": "https://new.184772.xyz",
        "cookie_domain": "new.184772.xyz",
        "currency": "$",
    },
    "mitchll": {
        "name": "Mitchll-api公益站",
        "domain": "https://api.mitchll.com",
        "cookie_domain": "api.mitchll.com",
        "currency": "$",
    },
    "kingo": {
        "name": "Kingo API公益站",
        "domain": "https://new-api-bxhm.onrender.com",
        "cookie_domain": "new-api-bxhm.onrender.com",
        "currency": "$",
    },
    "techstar": {
        "name": "TechnologyStar",
        "domain": "https://aidrouter.qzz.io",
        "cookie_domain": "aidrouter.qzz.io",
        "currency": "$",
    },
    "lightllm": {
        "name": "轻のLLM",
        "domain": "https://lightllm.online",
        "cookie_domain": "lightllm.online",
        "currency": "$",
    },
    "hotaru": {
        "name": "Hotaru API",
        "domain": "https://api.hotaruapi.top",
        "cookie_domain": "api.hotaruapi.top",
        "currency": "$",
    },
    "dev88": {
        "name": "DEV88公益站",
        "domain": "https://api.dev88.tech",
        "cookie_domain": "api.dev88.tech",
        "currency": "$",
    },
    "huan": {
        "name": "huan公益站",
        "domain": "https://ai.huan666.de",
        "cookie_domain": "ai.huan666.de",
        "currency": "$",
    },
}


@dataclass
class LinuxDOAccount:
    """LinuxDO 统一账号配置

    一个 LinuxDO 账号可以签到多个支持 LinuxDO OAuth 的站点。

    支持的字段：
    - username: 用户名（必填）
    - password: 密码（必填）
    - name: 账号显示名称（可选）
    - browse_enabled / browse_linuxdo: 是否浏览帖子（可选，默认 True）
    - browse_count: 浏览帖子数量（可选，默认 10）
    - level: 账号等级，用于确定浏览数量（可选，1-5 对应 5-25 个帖子）
    - sites: 要签到的站点列表（可选，默认空，仅浏览主站）
    """

    username: str
    password: str
    sites: list[str] = field(default_factory=list)  # 默认不签到任何站点，仅浏览主站
    browse_linuxdo: bool = True  # 是否浏览 LinuxDO 帖子
    browse_count: int = 10  # 浏览帖子数量
    name: str | None = None

    @classmethod
    def from_dict(cls, data: dict, index: int) -> "LinuxDOAccount":
        """从字典创建 LinuxDOAccount"""
        name = data.get("name") or data.get("username") or f"LinuxDO Account {index + 1}"
        # 默认不签到任何站点（仅浏览主站）
        sites = data.get("sites", [])

        # 支持 browse_enabled 或 browse_linuxdo 字段
        browse_linuxdo = data.get("browse_enabled", data.get("browse_linuxdo", True))

        # 支持 level 字段来确定浏览数量（level 1-5 对应 5-25 个帖子）
        level = data.get("level", 2)
        browse_count = data.get("browse_count", level * 5)  # level=1 -> 5, level=2 -> 10, etc.

        return cls(
            username=data["username"],
            password=data["password"],
            sites=sites,
            browse_linuxdo=browse_linuxdo,
            browse_count=browse_count,
            name=name,
        )

    def get_display_name(self, index: int) -> str:
        if self.name:
            return self.name
        return self.username or f"LinuxDO Account {index + 1}"


@dataclass
class ProviderConfig:
    """Provider 配置"""

    name: str
    domain: str
    login_path: str = "/login"
    sign_in_path: str | None = "/api/user/checkin"
    user_info_path: str = "/api/user/self"
    api_user_key: str = "new-api-user"
    bypass_method: Literal["waf_cookies"] | None = None
    waf_cookie_names: list[str] | None = None

    def __post_init__(self):
        required_waf_cookies = set()
        if self.waf_cookie_names and isinstance(self.waf_cookie_names, list):
            for item in self.waf_cookie_names:
                name = "" if not item or not isinstance(item, str) else item.strip()
                if name:
                    required_waf_cookies.add(name)

        if not required_waf_cookies:
            self.bypass_method = None
        self.waf_cookie_names = list(required_waf_cookies) if required_waf_cookies else None

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "ProviderConfig":
        return cls(
            name=name,
            domain=data["domain"],
            login_path=data.get("login_path", "/login"),
            sign_in_path=data.get("sign_in_path", "/api/user/checkin"),
            user_info_path=data.get("user_info_path", "/api/user/self"),
            api_user_key=data.get("api_user_key", "new-api-user"),
            bypass_method=data.get("bypass_method"),
            waf_cookie_names=data.get("waf_cookie_names"),
        )

    def to_dict(self) -> dict:
        result = {"name": self.name, "domain": self.domain, "login_path": self.login_path}
        if self.sign_in_path:
            result["sign_in_path"] = self.sign_in_path
        if self.bypass_method:
            result["bypass_method"] = self.bypass_method
        if self.waf_cookie_names:
            result["waf_cookie_names"] = self.waf_cookie_names
        return result

    def needs_waf_cookies(self) -> bool:
        return self.bypass_method == "waf_cookies"

    def needs_manual_check_in(self) -> bool:
        """判断是否需要手动调用签到 API

        如果配置了 sign_in_path，则需要手动签到
        """
        return self.sign_in_path is not None


# 预设的 Provider 配置（所有支持的 NewAPI 站点）
# 用户只需要提供 cookies 和 api_user，代码会根据 provider 字段自动匹配
DEFAULT_PROVIDERS: dict[str, dict] = {
    "anyrouter": {
        "domain": "https://anyrouter.top",
        "sign_in_path": "/api/user/checkin",
        "bypass_method": "waf_cookies",
        "waf_cookie_names": ["acw_tc", "cdn_sec_tc", "acw_sc__v2"],
    },
    "wong": {
        "domain": "https://wzw.pp.ua",
        "sign_in_path": "/api/user/checkin",
    },
    "elysiver": {
        "domain": "https://h-e.top",
        "sign_in_path": "/api/user/checkin",
    },
    "kfcapi": {
        "domain": "https://kfc-api.sxxe.net",
        "sign_in_path": "/api/user/checkin",
    },
    "duckcoding": {
        "domain": "https://free.duckcoding.com",
        "sign_in_path": "/api/user/checkin",
    },
    "runanytime": {
        "domain": "https://runanytime.hxi.me",
        "sign_in_path": "/api/user/checkin",
    },
    "neb": {
        "domain": "https://ai.zzhdsgsss.xyz",
        "sign_in_path": "/api/user/checkin",
    },
    "zeroliya": {
        "domain": "https://new.184772.xyz",
        "sign_in_path": "/api/user/checkin",
    },
    "mitchll": {
        "domain": "https://api.mitchll.com",
        "sign_in_path": "/api/user/checkin",
    },
    "kingo": {
        "domain": "https://new-api-bxhm.onrender.com",
        "sign_in_path": "/api/user/checkin",
    },
    "techstar": {
        "domain": "https://aidrouter.qzz.io",
        "sign_in_path": "/api/user/checkin",
    },
    "lightllm": {
        "domain": "https://lightllm.online",
        "sign_in_path": "/api/user/checkin",
    },
    "hotaru": {
        "domain": "https://api.hotaruapi.top",
        "sign_in_path": "/api/user/checkin",
    },
    "dev88": {
        "domain": "https://api.dev88.tech",
        "sign_in_path": "/api/user/checkin",
    },
    "huan": {
        "domain": "https://ai.huan666.de",
        "sign_in_path": "/api/user/checkin",
    },
    "agentrouter": {
        "domain": "https://agentrouter.org",
        "sign_in_path": None,  # 自动签到，无需手动调用
        "bypass_method": "waf_cookies",
        "waf_cookie_names": ["acw_tc"],
    },
}


@dataclass
class AppConfig:
    """应用配置 - 统一管理所有平台配置"""

    anyrouter_accounts: list[AnyRouterAccount] = field(default_factory=list)
    wong_accounts: list[WongAccount] = field(default_factory=list)
    elysiver_accounts: list[ElysiverAccount] = field(default_factory=list)
    kfcapi_accounts: list[KFCAPIAccount] = field(default_factory=list)
    duckcoding_accounts: list[DuckCodingAccount] = field(default_factory=list)
    linuxdo_accounts: list[LinuxDOAccount] = field(default_factory=list)
    providers: dict[str, ProviderConfig] = field(default_factory=dict)

    @classmethod
    def load_from_env(cls) -> "AppConfig":
        """从环境变量加载完整配置"""
        wong_accounts = cls._load_wong_accounts()
        elysiver_accounts = cls._load_elysiver_accounts()
        kfcapi_accounts = cls._load_kfcapi_accounts()
        duckcoding_accounts = cls._load_duckcoding_accounts()
        linuxdo_accounts = cls._load_linuxdo_accounts()
        anyrouter_accounts = cls._load_anyrouter_accounts()
        providers = cls._load_providers()
        return cls(
            anyrouter_accounts=anyrouter_accounts,
            wong_accounts=wong_accounts,
            elysiver_accounts=elysiver_accounts,
            kfcapi_accounts=kfcapi_accounts,
            duckcoding_accounts=duckcoding_accounts,
            linuxdo_accounts=linuxdo_accounts,
            providers=providers,
        )

    @classmethod
    def _load_wong_accounts(cls) -> list[WongAccount]:
        """从环境变量加载 WONG 公益站账号配置"""
        accounts = []

        # 从 WONG_ACCOUNTS 环境变量加载
        accounts_str = os.getenv("WONG_ACCOUNTS")
        if accounts_str:
            try:
                accounts_data = json.loads(accounts_str)

                if not isinstance(accounts_data, list):
                    logger.error("WONG_ACCOUNTS 配置格式错误: 必须是 JSON 数组格式")
                else:
                    for i, account_dict in enumerate(accounts_data):
                        if not isinstance(account_dict, dict):
                            logger.error(f"WONG 账号 {i + 1} 配置格式错误: 必须是 JSON 对象")
                            continue

                        accounts.append(WongAccount.from_dict(account_dict, i))

                    if accounts:
                        logger.info(f"成功加载 {len(accounts)} 个 WONG 账号配置 (JSON 格式)")
                        return accounts
            except json.JSONDecodeError as e:
                logger.error(f"WONG_ACCOUNTS JSON 解析失败: {e}")
            except Exception as e:
                logger.error(f"加载 WONG_ACCOUNTS 时发生错误: {e}")

        return accounts

    @classmethod
    def _load_elysiver_accounts(cls) -> list[ElysiverAccount]:
        """从环境变量加载 Elysiver 账号配置"""
        accounts = []

        # 从 ELYSIVER_ACCOUNTS 环境变量加载
        accounts_str = os.getenv("ELYSIVER_ACCOUNTS")
        if accounts_str:
            try:
                accounts_data = json.loads(accounts_str)

                if not isinstance(accounts_data, list):
                    logger.error("ELYSIVER_ACCOUNTS 配置格式错误: 必须是 JSON 数组格式")
                else:
                    for i, account_dict in enumerate(accounts_data):
                        if not isinstance(account_dict, dict):
                            logger.error(f"Elysiver 账号 {i + 1} 配置格式错误: 必须是 JSON 对象")
                            continue

                        accounts.append(ElysiverAccount.from_dict(account_dict, i))

                    if accounts:
                        logger.info(f"成功加载 {len(accounts)} 个 Elysiver 账号配置 (JSON 格式)")
                        return accounts
            except json.JSONDecodeError as e:
                logger.error(f"ELYSIVER_ACCOUNTS JSON 解析失败: {e}")
            except Exception as e:
                logger.error(f"加载 ELYSIVER_ACCOUNTS 时发生错误: {e}")

        return accounts

    @classmethod
    def _load_kfcapi_accounts(cls) -> list[KFCAPIAccount]:
        """从环境变量加载 KFC API 账号配置"""
        accounts = []

        # 从 KFCAPI_ACCOUNTS 环境变量加载
        accounts_str = os.getenv("KFCAPI_ACCOUNTS")
        if accounts_str:
            try:
                accounts_data = json.loads(accounts_str)

                if not isinstance(accounts_data, list):
                    logger.error("KFCAPI_ACCOUNTS 配置格式错误: 必须是 JSON 数组格式")
                else:
                    for i, account_dict in enumerate(accounts_data):
                        if not isinstance(account_dict, dict):
                            logger.error(f"KFC API 账号 {i + 1} 配置格式错误: 必须是 JSON 对象")
                            continue

                        accounts.append(KFCAPIAccount.from_dict(account_dict, i))

                    if accounts:
                        logger.info(f"成功加载 {len(accounts)} 个 KFC API 账号配置 (JSON 格式)")
                        return accounts
            except json.JSONDecodeError as e:
                logger.error(f"KFCAPI_ACCOUNTS JSON 解析失败: {e}")
            except Exception as e:
                logger.error(f"加载 KFCAPI_ACCOUNTS 时发生错误: {e}")

        return accounts

    @classmethod
    def _load_duckcoding_accounts(cls) -> list[DuckCodingAccount]:
        """从环境变量加载 Free DuckCoding 账号配置"""
        accounts = []

        accounts_str = os.getenv("DUCKCODING_ACCOUNTS")
        if accounts_str:
            try:
                accounts_data = json.loads(accounts_str)

                if not isinstance(accounts_data, list):
                    logger.error("DUCKCODING_ACCOUNTS 配置格式错误: 必须是 JSON 数组格式")
                else:
                    for i, account_dict in enumerate(accounts_data):
                        if not isinstance(account_dict, dict):
                            logger.error(f"DuckCoding 账号 {i + 1} 配置格式错误: 必须是 JSON 对象")
                            continue

                        accounts.append(DuckCodingAccount.from_dict(account_dict, i))

                    if accounts:
                        logger.info(f"成功加载 {len(accounts)} 个 DuckCoding 账号配置 (JSON 格式)")
                        return accounts
            except json.JSONDecodeError as e:
                logger.error(f"DUCKCODING_ACCOUNTS JSON 解析失败: {e}")
            except Exception as e:
                logger.error(f"加载 DUCKCODING_ACCOUNTS 时发生错误: {e}")

        return accounts

    @classmethod
    def _load_linuxdo_accounts(cls) -> list[LinuxDOAccount]:
        """从环境变量加载 LinuxDO 统一账号配置

        支持两种格式：
        1. LINUXDO_ACCOUNTS: JSON 数组格式，支持多账号和站点选择
        2. LINUXDO_USERNAME + LINUXDO_PASSWORD: 简单格式，单账号签到所有站点

        配置示例:
        {
            "username": "your_username",
            "password": "your_password",
            "sites": ["wong", "elysiver"],  // 可选，默认所有站点
            "browse_linuxdo": true,          // 可选，是否浏览帖子
            "browse_count": 10               // 可选，浏览帖子数量
        }
        """
        accounts = []

        # 方式1: JSON 数组格式
        accounts_str = os.getenv("LINUXDO_ACCOUNTS")
        if accounts_str:
            try:
                accounts_data = json.loads(accounts_str)

                if not isinstance(accounts_data, list):
                    logger.error("LINUXDO_ACCOUNTS 配置格式错误: 必须是 JSON 数组格式")
                else:
                    for i, account_dict in enumerate(accounts_data):
                        if not isinstance(account_dict, dict):
                            logger.error(f"LinuxDO 账号 {i + 1} 配置格式错误: 必须是 JSON 对象")
                            continue

                        if "username" not in account_dict or "password" not in account_dict:
                            logger.error(f"LinuxDO 账号 {i + 1} 缺少必填字段: 需要 'username' 和 'password'")
                            continue

                        accounts.append(LinuxDOAccount.from_dict(account_dict, i))

                    if accounts:
                        logger.info(f"成功加载 {len(accounts)} 个 LinuxDO 统一账号配置")
                        return accounts
            except json.JSONDecodeError as e:
                logger.error(f"LINUXDO_ACCOUNTS JSON 解析失败: {e}")
            except Exception as e:
                logger.error(f"加载 LINUXDO_ACCOUNTS 时发生错误: {e}")

        # 方式2: 简单环境变量格式
        username = os.getenv("LINUXDO_USERNAME")
        password = os.getenv("LINUXDO_PASSWORD")
        if username and password:
            # 从环境变量读取可选配置
            browse_linuxdo = os.getenv("LINUXDO_BROWSE", "true").lower() == "true"
            browse_count = int(os.getenv("LINUXDO_BROWSE_COUNT", "10"))

            accounts.append(LinuxDOAccount(
                username=username,
                password=password,
                sites=list(NEWAPI_SITES.keys()),
                browse_linuxdo=browse_linuxdo,
                browse_count=browse_count,
                name=username,
            ))
            logger.info(f"成功加载 LinuxDO 账号: {username} (签到所有站点, 浏览帖子: {browse_linuxdo})")

        return accounts

    @classmethod
    def _load_anyrouter_accounts(cls) -> list[AnyRouterAccount]:
        """从环境变量加载 NewAPI 站点账号配置
        
        支持两个环境变量（优先级从高到低）：
        1. NEWAPI_ACCOUNTS - 新的统一配置名
        2. ANYROUTER_ACCOUNTS - 兼容旧配置
        
        JSON 格式示例:
        [
            {
                "name": "我的账号",
                "provider": "anyrouter",  // 站点ID，见 DEFAULT_PROVIDERS
                "cookies": {"session": "xxx"},
                "api_user": "12345"
            }
        ]
        """
        # 优先使用 NEWAPI_ACCOUNTS，兼容 ANYROUTER_ACCOUNTS
        accounts_str = os.getenv("NEWAPI_ACCOUNTS") or os.getenv("ANYROUTER_ACCOUNTS")
        if not accounts_str:
            return []

        try:
            accounts_data = json.loads(accounts_str)

            if not isinstance(accounts_data, list):
                logger.error("NEWAPI_ACCOUNTS 配置格式错误: 必须是 JSON 数组格式")
                return []

            accounts = []
            for i, account_dict in enumerate(accounts_data):
                if not isinstance(account_dict, dict):
                    logger.error(f"账号 {i + 1} 配置格式错误: 必须是 JSON 对象")
                    continue

                if "cookies" not in account_dict or "api_user" not in account_dict:
                    logger.error(f"账号 {i + 1} 缺少必填字段: 需要 'cookies' 和 'api_user'")
                    continue
                
                # 验证 provider 是否在预设列表中
                provider = account_dict.get("provider", "anyrouter")
                if provider not in DEFAULT_PROVIDERS:
                    logger.warning(f"账号 {i + 1} 的 provider '{provider}' 不在预设列表中，将使用默认配置")

                accounts.append(AnyRouterAccount.from_dict(account_dict, i))

            if accounts:
                # 统计各站点账号数量
                provider_counts = {}
                for acc in accounts:
                    provider_counts[acc.provider] = provider_counts.get(acc.provider, 0) + 1
                
                count_str = ", ".join(f"{p}: {c}" for p, c in sorted(provider_counts.items()))
                logger.info(f"成功加载 {len(accounts)} 个 NewAPI 账号配置 ({count_str})")
            return accounts

        except json.JSONDecodeError as e:
            logger.error(f"NEWAPI_ACCOUNTS JSON 解析失败: {e}")
            return []
        except Exception as e:
            logger.error(f"加载 NEWAPI_ACCOUNTS 时发生错误: {e}")
            return []

    @classmethod
    def _load_providers(cls) -> dict[str, ProviderConfig]:
        """加载 Provider 配置
        
        默认加载所有预设的站点配置，用户可以通过 PROVIDERS 环境变量覆盖或添加新站点。
        """
        # 从预设配置创建 ProviderConfig 对象
        providers = {}
        for name, config_data in DEFAULT_PROVIDERS.items():
            providers[name] = ProviderConfig.from_dict(name, config_data)

        # 允许用户通过环境变量覆盖或添加新配置
        providers_str = os.getenv("PROVIDERS")
        if providers_str:
            try:
                providers_data = json.loads(providers_str)
                if not isinstance(providers_data, dict):
                    logger.warning("PROVIDERS 必须是 JSON 对象格式")
                    return providers

                for name, provider_data in providers_data.items():
                    try:
                        providers[name] = ProviderConfig.from_dict(name, provider_data)
                    except KeyError as e:
                        logger.error(f"Provider '{name}' 缺少必填字段: {e}")
                    except Exception as e:
                        logger.warning(f"解析 Provider '{name}' 失败: {e}")

                logger.info(f"从 PROVIDERS 加载了 {len(providers_data)} 个自定义配置")
            except json.JSONDecodeError as e:
                logger.warning(f"PROVIDERS JSON 解析失败: {e}")
            except Exception as e:
                logger.warning(f"加载 PROVIDERS 时发生错误: {e}")

        return providers

    def get_provider(self, name: str) -> ProviderConfig | None:
        return self.providers.get(name)

    def has_any_config(self) -> bool:
        return (len(self.anyrouter_accounts) > 0 or len(self.wong_accounts) > 0
                or len(self.elysiver_accounts) > 0 or len(self.kfcapi_accounts) > 0
                or len(self.duckcoding_accounts) > 0 or len(self.linuxdo_accounts) > 0)


# Backward compatibility alias
AccountConfig = AnyRouterAccount


def load_accounts_config() -> list[AnyRouterAccount] | None:
    """从环境变量加载账号配置（向后兼容函数）"""
    accounts = AppConfig._load_anyrouter_accounts()
    return accounts if accounts else None
