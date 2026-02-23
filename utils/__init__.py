# Shared utilities for multi-platform checkin system
# This module contains configuration, notification, retry, and logging utilities

# Import retry module
from .retry import (
    retry_decorator,
    retry_with_exponential_backoff,
    retry_with_random_delay,
    network_retry,
    browser_retry,
    calculate_delay,
)

# Import config module
from .config import (
    AppConfig,
    AnyRouterAccount,
    ProviderConfig,
    load_accounts_config,
)

# Import notification module
from .notify import (
    NotificationManager,
    get_notification_manager,
    push_message,
)

# Import logging module
from .logging import (
    setup_logging,
    mask_sensitive_data,
    get_logger,
    SensitiveFilter,
)

# Import OAuth helpers module
from .oauth_helpers import (
    # Enums
    OAuthURLType,
    OAuthStep,
    # URL classification functions
    classify_oauth_url,
    is_linuxdo_login_url,
    is_authorization_url,
    is_oauth_complete_url,
    is_oauth_related_url,
    # Retry utilities
    async_retry,
    retry_async_operation,
    # Exception classes
    OAuthError,
    NavigationTimeoutError,
    ElementNotFoundError,
    CookieNotFoundError,
    # Screenshot capture utilities
    capture_error_screenshot,
    get_debug_directory,
    cleanup_old_screenshots,
    DEFAULT_DEBUG_DIR,
)

# Import browser module
from .browser import (
    BrowserStartupError,
)

# Import failure tracker module
from .failure_tracker import (
    FailureTracker,
)

__all__ = [
    # Config
    "AppConfig",
    "AnyRouterAccount",
    "ProviderConfig",
    "load_accounts_config",
    # Notification
    "NotificationManager",
    "get_notification_manager",
    "push_message",
    # Retry utilities
    "retry_decorator",
    "retry_with_exponential_backoff",
    "retry_with_random_delay",
    "network_retry",
    "browser_retry",
    "calculate_delay",
    # Logging
    "setup_logging",
    "mask_sensitive_data",
    "get_logger",
    "SensitiveFilter",
    # OAuth helpers - Enums
    "OAuthURLType",
    "OAuthStep",
    # OAuth helpers - URL classification
    "classify_oauth_url",
    "is_linuxdo_login_url",
    "is_authorization_url",
    "is_oauth_complete_url",
    "is_oauth_related_url",
    # OAuth helpers - Retry utilities
    "async_retry",
    "retry_async_operation",
    # OAuth helpers - Exception classes
    "OAuthError",
    "NavigationTimeoutError",
    "ElementNotFoundError",
    "CookieNotFoundError",
    # OAuth helpers - Screenshot capture utilities
    "capture_error_screenshot",
    "get_debug_directory",
    "cleanup_old_screenshots",
    "DEFAULT_DEBUG_DIR",
    # Browser - Exception classes
    "BrowserStartupError",
    # Failure tracker
    "FailureTracker",
]
