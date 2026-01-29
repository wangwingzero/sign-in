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
from typing import Dict, List, Literal, Optional

from loguru import logger


@dataclass
class AnyRouterAccount:
    """AnyRouter 账号配置"""
    
    cookies: dict | str
    api_user: str
    provider: str = "anyrouter"
    name: Optional[str] = None
    
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
    
    linuxdo_username: Optional[str] = None
    linuxdo_password: Optional[str] = None
    fallback_cookies: Optional[str] = None
    api_user: Optional[str] = None
    name: Optional[str] = None
    
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
class ProviderConfig:
    """Provider 配置"""

    name: str
    domain: str
    login_path: str = "/login"
    sign_in_path: Optional[str] = "/api/user/sign_in"
    user_info_path: str = "/api/user/self"
    api_user_key: str = "new-api-user"
    bypass_method: Optional[Literal["waf_cookies"]] = None
    waf_cookie_names: Optional[List[str]] = None

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
            sign_in_path=data.get("sign_in_path", "/api/user/sign_in"),
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


@dataclass
class AppConfig:
    """应用配置 - 统一管理所有平台配置"""

    anyrouter_accounts: List[AnyRouterAccount] = field(default_factory=list)
    wong_accounts: List[WongAccount] = field(default_factory=list)
    providers: Dict[str, ProviderConfig] = field(default_factory=dict)

    @classmethod
    def load_from_env(cls) -> "AppConfig":
        """从环境变量加载完整配置"""
        wong_accounts = cls._load_wong_accounts()
        anyrouter_accounts = cls._load_anyrouter_accounts()
        providers = cls._load_providers()
        return cls(
            anyrouter_accounts=anyrouter_accounts,
            wong_accounts=wong_accounts,
            providers=providers,
        )
    
    @classmethod
    def _load_wong_accounts(cls) -> List[WongAccount]:
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
    def _load_anyrouter_accounts(cls) -> List[AnyRouterAccount]:
        """从环境变量加载 AnyRouter 账号配置"""
        accounts_str = os.getenv("ANYROUTER_ACCOUNTS")
        if not accounts_str:
            return []
        
        try:
            accounts_data = json.loads(accounts_str)
            
            if not isinstance(accounts_data, list):
                logger.error("ANYROUTER_ACCOUNTS 配置格式错误: 必须是 JSON 数组格式")
                return []
            
            accounts = []
            for i, account_dict in enumerate(accounts_data):
                if not isinstance(account_dict, dict):
                    logger.error(f"账号 {i + 1} 配置格式错误: 必须是 JSON 对象")
                    continue
                
                if "cookies" not in account_dict or "api_user" not in account_dict:
                    logger.error(f"账号 {i + 1} 缺少必填字段: 需要 'cookies' 和 'api_user'")
                    continue
                
                accounts.append(AnyRouterAccount.from_dict(account_dict, i))
            
            if accounts:
                logger.info(f"成功加载 {len(accounts)} 个 AnyRouter 账号配置")
            return accounts
            
        except json.JSONDecodeError as e:
            logger.error(f"ANYROUTER_ACCOUNTS JSON 解析失败: {e}")
            return []
        except Exception as e:
            logger.error(f"加载 ANYROUTER_ACCOUNTS 时发生错误: {e}")
            return []
    
    @classmethod
    def _load_providers(cls) -> Dict[str, ProviderConfig]:
        """加载 Provider 配置"""
        providers = {
            "anyrouter": ProviderConfig(
                name="anyrouter",
                domain="https://anyrouter.top",
                bypass_method="waf_cookies",
                waf_cookie_names=["acw_tc", "cdn_sec_tc", "acw_sc__v2"],
            ),
            "agentrouter": ProviderConfig(
                name="agentrouter",
                domain="https://agentrouter.org",
                sign_in_path=None,
                bypass_method="waf_cookies",
                waf_cookie_names=["acw_tc"],
            ),
            "wong": ProviderConfig(
                name="wong",
                domain="https://wzw.pp.ua",
                sign_in_path="/api/user/checkin",
                user_info_path="/api/user/self",
                api_user_key="new-api-user",
                bypass_method=None,
                waf_cookie_names=None,
            ),
        }
        
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

    def get_provider(self, name: str) -> Optional[ProviderConfig]:
        return self.providers.get(name)
    
    def has_any_config(self) -> bool:
        return len(self.anyrouter_accounts) > 0 or len(self.wong_accounts) > 0


# Backward compatibility alias
AccountConfig = AnyRouterAccount


def load_accounts_config() -> Optional[List[AnyRouterAccount]]:
    """从环境变量加载账号配置（向后兼容函数）"""
    accounts = AppConfig._load_anyrouter_accounts()
    return accounts if accounts else None
