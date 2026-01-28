#!/usr/bin/env python3
"""
多平台签到工具主入口

支持 LinuxDo 和 AnyRouter 平台的自动签到。

cron: 0 */6 * * *
new Env("多平台签到")

Requirements:
- 6.1: 支持命令行参数解析
- 6.5: 正确的退出码逻辑
- 6.6: 支持 --dry-run 模式
"""

import argparse
import asyncio
import sys
from datetime import datetime, timezone, timedelta

from loguru import logger

from platforms.manager import PlatformManager
from utils.config import AppConfig


# 北京时间时区 (UTC+8)
BEIJING_TZ = timezone(timedelta(hours=8))


def get_beijing_time() -> datetime:
    """获取北京时间"""
    return datetime.now(BEIJING_TZ)


def setup_logging(debug: bool = False) -> None:
    """配置日志"""
    logger.remove()
    
    level = "DEBUG" if debug else "INFO"
    format_str = (
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    )
    
    logger.add(sys.stderr, format=format_str, level=level, colorize=True)


def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="多平台签到工具 - 支持 LinuxDo 和 AnyRouter",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py                    # 运行所有平台签到
  python main.py --platform linuxdo # 仅运行 LinuxDo 签到
  python main.py --platform anyrouter # 仅运行 AnyRouter 签到
  python main.py --dry-run          # 干运行模式（仅显示配置）
  python main.py --debug            # 启用调试日志
        """,
    )
    
    parser.add_argument(
        "--platform", "-p",
        choices=["linuxdo", "anyrouter", "wong"],
        help="指定要运行的平台（默认运行所有平台）",
    )
    
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="干运行模式，仅显示配置不执行签到",
    )
    
    parser.add_argument(
        "--debug", "-d",
        action="store_true",
        help="启用调试日志",
    )
    
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="禁用通知发送",
    )
    
    parser.add_argument(
        "--force-notify",
        action="store_true",
        help="强制发送通知（即使全部成功）",
    )
    
    return parser.parse_args()


def show_config(config: AppConfig) -> None:
    """显示配置信息"""
    print("\n" + "=" * 50)
    print("配置信息")
    print("=" * 50)
    
    # LinuxDo
    if config.linuxdo_accounts:
        print(f"\n[LinuxDo] {len(config.linuxdo_accounts)} 个账号")
        for i, account in enumerate(config.linuxdo_accounts):
            print(f"  账号 {i + 1}: {account.get_display_name(i)}")
            print(f"    浏览功能: {'启用' if account.browse_enabled else '禁用'}")
    else:
        print(f"\n[LinuxDo] 未配置")
    
    # AnyRouter
    if config.anyrouter_accounts:
        print(f"\n[AnyRouter] {len(config.anyrouter_accounts)} 个账号")
        for i, account in enumerate(config.anyrouter_accounts):
            print(f"  账号 {i + 1}: {account.get_display_name(i)}")
            print(f"    Provider: {account.provider}")
    else:
        print(f"\n[AnyRouter] 未配置")
    
    # Providers
    if config.providers:
        print(f"\n[Providers] {len(config.providers)} 个")
        for name, provider in config.providers.items():
            print(f"  {name}: {provider.domain}")
    
    print("\n" + "=" * 50)


async def run_checkin(args: argparse.Namespace) -> int:
    """运行签到
    
    Returns:
        int: 退出码
    """
    # 加载配置
    config = AppConfig.load_from_env()
    
    # 干运行模式
    if args.dry_run:
        show_config(config)
        print("\n[干运行模式] 不执行签到")
        return 0
    
    # 检查是否有配置
    has_linuxdo = len(config.linuxdo_accounts) > 0
    has_anyrouter = len(config.anyrouter_accounts) > 0
    
    if not has_linuxdo and not has_anyrouter:
        logger.error("未配置任何平台，请设置环境变量")
        logger.info("LinuxDo: LINUXDO_ACCOUNTS (JSON) 或 LINUXDO_USERNAME + LINUXDO_PASSWORD")
        logger.info("AnyRouter: ANYROUTER_ACCOUNTS")
        return 1
    
    # 创建平台管理器
    manager = PlatformManager(config)
    
    # 运行签到
    logger.info(f"开始签到 - {get_beijing_time().strftime('%Y-%m-%d %H:%M:%S')}")
    
    if args.platform:
        logger.info(f"仅运行平台: {args.platform}")
        await manager.run_platform(args.platform)
    else:
        await manager.run_all()
    
    # 显示结果
    logger.info(f"签到完成 - 成功: {manager.success_count}, 失败: {manager.failed_count}, 跳过: {manager.skipped_count}")
    
    # 发送通知
    if not args.no_notify:
        manager.send_summary_notification(force=args.force_notify)
    
    return manager.get_exit_code()


def main() -> None:
    """主函数"""
    args = parse_args()
    
    # 配置日志
    setup_logging(debug=args.debug)
    
    try:
        exit_code = asyncio.run(run_checkin(args))
        sys.exit(exit_code)
    except KeyboardInterrupt:
        logger.warning("用户中断")
        sys.exit(1)
    except Exception as e:
        logger.exception(f"程序异常: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
