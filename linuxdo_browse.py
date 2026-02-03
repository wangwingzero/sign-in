#!/usr/bin/env python3
"""
LinuxDO 浏览签到脚本

用于 GitHub Actions 中执行 LinuxDO 浏览签到任务。
支持 Cookie 模式和用户名密码模式。
"""

import asyncio
import os
import sys
import traceback

from loguru import logger


def setup_logging():
    """配置日志"""
    logger.remove()

    if os.environ.get("DEBUG_MODE") == "true":
        logger.add(sys.stdout, level="DEBUG", format="{time:HH:mm:ss} | {level} | {message}")
    else:
        logger.add(sys.stdout, level="INFO", format="{time:HH:mm:ss} | {level} | {message}")


async def main():
    """主函数"""
    setup_logging()

    logger.info("=" * 50)
    logger.info("LinuxDO 浏览签到脚本启动")
    logger.info("=" * 50)

    # 打印环境信息
    logger.info(f"Python 版本: {sys.version}")
    logger.info(f"DISPLAY: {os.environ.get('DISPLAY', '未设置')}")
    logger.info(f"BROWSER_ENGINE: {os.environ.get('BROWSER_ENGINE', '未设置')}")
    logger.info(f"CI: {os.environ.get('CI', '未设置')}")
    logger.info(f"GITHUB_ACTIONS: {os.environ.get('GITHUB_ACTIONS', '未设置')}")

    # 检查 LINUXDO_ACCOUNTS 环境变量
    accounts_str = os.environ.get("LINUXDO_ACCOUNTS")
    if not accounts_str:
        logger.error("未配置 LINUXDO_ACCOUNTS 环境变量")
        logger.error("请在 GitHub Secrets 中添加 LINUXDO_ACCOUNTS")
        sys.exit(1)

    # 打印账号配置（隐藏敏感信息）
    logger.info(f"LINUXDO_ACCOUNTS 长度: {len(accounts_str)} 字符")

    # 导入模块
    try:
        from platforms.linuxdo import LinuxDOAdapter
        from utils.config import AppConfig
        from utils.notify import push_message

        logger.info("模块导入成功")
    except ImportError as e:
        logger.error(f"模块导入失败: {e}")
        logger.error(traceback.format_exc())
        sys.exit(1)

    # 加载配置
    try:
        config = AppConfig.load_from_env()
        logger.info(f"配置加载成功，共 {len(config.linuxdo_accounts)} 个账号")
    except Exception as e:
        logger.error(f"配置加载失败: {e}")
        logger.error(traceback.format_exc())
        sys.exit(1)

    if not config.linuxdo_accounts:
        logger.error("未找到有效的 LinuxDO 账号配置")
        logger.error("请检查 LINUXDO_ACCOUNTS 的 JSON 格式是否正确")
        sys.exit(1)

    results = []

    for i, account in enumerate(config.linuxdo_accounts):
        logger.info("-" * 40)
        logger.info(f"处理账号 [{i + 1}/{len(config.linuxdo_accounts)}]: {account.get_display_name(i)}")

        # 打印账号配置（隐藏敏感信息）
        has_cookies = bool(account.cookies)
        has_credentials = bool(account.username and account.password)
        logger.info(f"  - 有 Cookie: {has_cookies}")
        logger.info(f"  - 有用户名密码: {has_credentials}")
        logger.info(f"  - 浏览时长: {account.browse_minutes} 分钟")

        # 获取 cookies
        cookies = account.cookies if account.cookies else None

        # 登录重试配置：每次重试都打开新浏览器实例
        max_login_retries = 5
        retry_delays = [5, 10, 15, 20, 25]  # 每次重试前等待的秒数
        
        login_success = False
        adapter = None
        last_error = None
        
        for attempt in range(1, max_login_retries + 1):
            # 每次尝试都创建新的 adapter（新浏览器实例）
            adapter = LinuxDOAdapter(
                username=account.username,
                password=account.password,
                cookies=cookies,
                account_name=account.get_display_name(i),
                browse_minutes=account.browse_minutes,
            )
            
            try:
                logger.info(f"登录尝试 {attempt}/{max_login_retries}...")
                login_success = await adapter.login()
                
                if login_success:
                    logger.success(f"登录成功！方式: {adapter._login_method}")
                    break
                else:
                    logger.warning(f"登录尝试 {attempt}/{max_login_retries} 失败")
                    
            except Exception as e:
                last_error = e
                logger.warning(f"登录尝试 {attempt}/{max_login_retries} 出错: {e}")
            
            # 如果不是最后一次尝试，关闭浏览器并等待后重试
            if attempt < max_login_retries:
                try:
                    await adapter.cleanup()
                    logger.info("浏览器已关闭")
                except Exception as e:
                    logger.warning(f"清理资源时出错: {e}")
                
                wait_time = retry_delays[attempt - 1]
                logger.info(f"等待 {wait_time} 秒后打开新浏览器重试...")
                await asyncio.sleep(wait_time)
                adapter = None
        
        # 检查最终登录结果
        if not login_success:
            error_msg = str(last_error)[:50] if last_error else "登录失败"
            logger.error(f"账号 {account.get_display_name(i)} 登录失败，已重试 {max_login_retries} 次")
            results.append(f"❌ {account.get_display_name(i)}: {error_msg}")
            if adapter:
                try:
                    await adapter.cleanup()
                except Exception:
                    pass
            continue
        
        # 登录成功，执行浏览
        try:
            logger.info("开始浏览帖子...")
            result = await adapter.checkin()

            results.append(f"✅ {account.get_display_name(i)}: {result.message}")
            logger.success(f"完成: {result.message}")

        except Exception as e:
            logger.error(f"账号 {account.get_display_name(i)} 浏览出错: {e}")
            logger.error(traceback.format_exc())
            results.append(f"❌ {account.get_display_name(i)}: {str(e)[:50]}")
        finally:
            try:
                await adapter.cleanup()
            except Exception as e:
                logger.warning(f"清理资源时出错: {e}")

    # 发送通知
    logger.info("-" * 40)
    if results:
        title = "LinuxDO 浏览签到结果"
        content = "\n".join(results)
        logger.info(f"发送通知:\n{content}")

        try:
            push_message(title, content)
            logger.success("通知发送成功")
        except Exception as e:
            logger.warning(f"通知发送失败: {e}")
    else:
        logger.warning("没有任何结果，跳过通知")

    logger.info("=" * 50)
    logger.info("LinuxDO 浏览签到脚本完成")
    logger.info("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
