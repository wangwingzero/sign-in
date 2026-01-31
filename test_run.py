#!/usr/bin/env python3
"""测试脚本 - 手动验证版本

当遇到 Cloudflare Turnstile 验证时，等待用户手动完成验证。
"""
import asyncio
import contextlib
import os
import sys

from loguru import logger

from utils.browser import BrowserManager, get_browser_engine
from utils.config import AppConfig

# 设置环境变量 - 测试所有公益站
os.environ["LINUXDO_ACCOUNTS"] = '''[
    {"username": "wangwingzero@qq.com", "password": "Hu20100416", "browse_linuxdo": false, "browse_count": 3, "name": "QQ", "sites": ["wong", "duckcoding", "kfcapi", "neb"]}
]'''

# 强制使用非 headless 模式进行调试
os.environ["BROWSER_HEADLESS"] = "false"

logger.remove()
logger.add(sys.stderr, format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>", level="DEBUG")

# 测试配置加载
print("=" * 60)
print("测试 LinuxDO OAuth 登录 + 多站点签到")
print("=" * 60)

config = AppConfig.load_from_env()

print(f"\nLinuxDO 账号: {len(config.linuxdo_accounts)}")
for i, acc in enumerate(config.linuxdo_accounts):
    print(f"  {i+1}. {acc.name} - 站点: {acc.sites}")


async def wait_for_manual_verification(tab, timeout: int = 120) -> bool:
    """等待用户手动完成 Cloudflare 验证。

    检测页面是否还在验证状态，如果是则等待用户手动完成。
    """
    from utils.browser import URLMonitor

    url_monitor = URLMonitor(tab, poll_interval=1.0)
    start_time = asyncio.get_event_loop().time()

    logger.warning("=" * 50)
    logger.warning("检测到 Cloudflare 验证，请手动完成验证！")
    logger.warning("=" * 50)

    while asyncio.get_event_loop().time() - start_time < timeout:
        current_url = await url_monitor.get_current_url()

        # 检查是否还在验证页面
        try:
            # 检查页面内容
            page_content = await tab.get_content()
            if page_content and "确认您是真人" not in page_content and "Verify you are human" not in page_content:
                    # 检查是否有 Turnstile iframe
                    turnstile = await tab.select('iframe[src*="challenges.cloudflare.com"]', timeout=1)
                    if not turnstile:
                        logger.success("Cloudflare 验证已通过！")
                        return True
        except Exception:
            pass

        # 检查 URL 是否已经跳转
        if "chrome-error" not in current_url and "challenges.cloudflare" not in current_url:
            # 可能已经通过验证
            await asyncio.sleep(2)
            new_url = await url_monitor.get_current_url()
            if new_url == current_url and "chrome-error" not in new_url:
                logger.success(f"页面已加载: {new_url}")
                return True

        await asyncio.sleep(1)

    logger.error(f"等待验证超时 ({timeout}s)")
    return False


async def login_to_linuxdo(browser_manager: BrowserManager, username: str, password: str) -> bool:
    """先登录 LinuxDO，保持会话"""
    tab = browser_manager.page
    LINUXDO_LOGIN_URL = "https://linux.do"

    logger.info("访问 LinuxDO 主页...")
    await tab.get(LINUXDO_LOGIN_URL)
    await asyncio.sleep(3)

    # 等待 Cloudflare 验证
    await browser_manager.wait_for_cloudflare(timeout=30)

    # 检查是否已经登录
    try:
        user_menu = await tab.select('.current-user', timeout=3)
        if user_menu:
            logger.info("LinuxDO 已登录，跳过登录步骤")
            return True
    except Exception:
        pass

    # 点击登录按钮显示表单
    logger.info("查找登录按钮...")
    login_clicked = False

    # 方式1: 通过 CSS 选择器
    try:
        login_btn = await tab.select('.login-button', timeout=3)
        if login_btn:
            await login_btn.click()
            login_clicked = True
            logger.info("通过 CSS 选择器点击登录按钮")
    except Exception:
        pass

    # 方式2: 通过文本
    if not login_clicked:
        try:
            login_link = await tab.find("登录", timeout=3)
            if login_link:
                await login_link.click()
                login_clicked = True
                logger.info("通过文本点击登录按钮")
        except Exception:
            pass

    if not login_clicked:
        logger.error("未找到登录按钮")
        return False

    await asyncio.sleep(3)

    # 填写用户名
    logger.info("等待登录表单...")
    username_input = await tab.select('#login-account-name', timeout=10)
    if not username_input:
        logger.error("未找到用户名输入框")
        return False

    logger.info("填写用户名...")
    await username_input.clear_input()
    await asyncio.sleep(0.2)
    await username_input.send_keys(username)
    await asyncio.sleep(0.5)

    # 填写密码
    password_input = await tab.select('#login-account-password', timeout=5)
    if password_input:
        logger.info("填写密码...")
        await password_input.clear_input()
        await asyncio.sleep(0.2)
        await password_input.send_keys(password)
        await asyncio.sleep(0.5)

    # 点击登录
    login_btn = await tab.select('#login-button', timeout=5)
    if login_btn:
        logger.info("点击登录按钮...")
        await login_btn.mouse_move()
        await asyncio.sleep(0.3)
        await login_btn.mouse_click()
        await asyncio.sleep(8)

    # 检查登录结果
    try:
        user_menu = await tab.select('.current-user', timeout=5)
        if user_menu:
            logger.success("LinuxDO 登录成功！")
            return True
    except Exception:
        pass

    logger.warning("LinuxDO 登录状态不确定")
    return True  # 继续尝试


async def checkin_site_simple(browser_manager: BrowserManager, site_config: dict) -> dict:
    """对单个站点进行签到 - 简化版本，在同一个标签页操作"""
    import json as json_module

    import httpx

    from utils.browser import CookieRetriever, URLMonitor

    site_name = site_config["name"]
    base_url = site_config["base_url"]
    cookie_domain = site_config["cookie_domain"]
    currency_unit = site_config.get("currency_unit", "$")

    tab = browser_manager.page

    logger.info(f"[{site_name}] 开始签到...")

    # 访问登录页面
    login_url = f"{base_url}/login"
    logger.info(f"[{site_name}] 访问登录页面: {login_url}")
    await tab.get(login_url)
    await browser_manager.wait_for_cloudflare(timeout=30)
    await asyncio.sleep(2)

    # 导航到注册页再回来（触发 LinuxDO 按钮显示）
    try:
        register_url = f"{base_url}/register"
        await tab.get(register_url)
        await asyncio.sleep(2)
        await tab.get(login_url)
        await asyncio.sleep(2)
    except Exception:
        pass

    # 勾选同意协议
    try:
        agreement = await tab.find("我已阅读并同意", timeout=2)
        if agreement:
            await agreement.click()
            await asyncio.sleep(0.5)
    except Exception:
        try:
            checkbox = await tab.select('input[type="checkbox"]', timeout=2)
            if checkbox:
                await checkbox.click()
        except Exception:
            pass

    # 查找 LinuxDO 按钮
    linuxdo_btn = None
    try:
        buttons = await tab.select_all('button')
        for btn in buttons:
            try:
                html = await btn.get_html()
                if html and 'LinuxDO' in html:
                    linuxdo_btn = btn
                    break
            except Exception:
                continue
    except Exception:
        pass

    if not linuxdo_btn:
        with contextlib.suppress(Exception):
            linuxdo_btn = await tab.find("LinuxDO", timeout=3)

    if not linuxdo_btn:
        return {"status": "failed", "message": "未找到 LinuxDO 按钮"}

    # 点击 LinuxDO 按钮
    logger.info(f"[{site_name}] 点击 LinuxDO 按钮...")
    await linuxdo_btn.click()
    await asyncio.sleep(5)

    # 等待页面加载，可能需要手动验证
    url_monitor = URLMonitor(tab, poll_interval=0.5)

    # 检查是否需要手动验证
    for _ in range(3):
        current_url = await url_monitor.get_current_url()
        logger.info(f"[{site_name}] 当前页面: {current_url}")

        # 如果在 connect.linux.do 且有验证
        if "connect.linux.do" in current_url or "linux.do" in current_url:
            # 检查是否有 Turnstile
            try:
                page_content = await tab.get_content()
                if page_content and ("确认您是真人" in page_content or "Verify you are human" in page_content):
                    logger.warning(f"[{site_name}] 检测到 Cloudflare 验证，请手动完成！")
                    # 等待用户手动验证
                    await wait_for_manual_verification(tab, timeout=120)
            except Exception:
                pass

        # 检查是否有授权按钮
        if "authorize" in current_url.lower():
            logger.info(f"[{site_name}] 检测到授权页面...")
            await asyncio.sleep(2)

            authorize_btn = None
            with contextlib.suppress(Exception):
                authorize_btn = await tab.find("允许", timeout=5)
            if not authorize_btn:
                with contextlib.suppress(Exception):
                    authorize_btn = await tab.find("Allow", timeout=3)

            if authorize_btn:
                logger.info(f"[{site_name}] 点击授权按钮...")
                await authorize_btn.click()
                await asyncio.sleep(5)
                break

        # 检查是否已经跳转回目标站点
        if cookie_domain in current_url:
            logger.info(f"[{site_name}] 已跳转回目标站点")
            break

        await asyncio.sleep(3)

    # 最终检查 URL
    await asyncio.sleep(3)
    current_url = await url_monitor.get_current_url()
    logger.info(f"[{site_name}] 最终页面: {current_url}")

    # 获取 session cookie
    cookie_retriever = CookieRetriever(browser_manager, cookie_domain)
    session_cookie = await cookie_retriever.get_session_cookie(max_retries=3)

    if not session_cookie:
        return {"status": "failed", "message": "未获取到 session cookie"}

    logger.info(f"[{site_name}] 获取到 session cookie")

    # 获取用户 ID
    api_user = None
    try:
        # 先导航到目标站点的控制台页面
        await tab.get(f"{base_url}/console/token")
        await asyncio.sleep(3)

        user_json = await tab.evaluate("localStorage.getItem('user')")
        if user_json:
            user_data = json_module.loads(user_json)
            if isinstance(user_data, dict) and 'id' in user_data:
                api_user = str(user_data['id'])
                logger.info(f"[{site_name}] 获取到用户 ID: {api_user}")
    except Exception as e:
        logger.debug(f"[{site_name}] 获取用户 ID 失败: {e}")

    # 构建请求头
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Cookie": f"session={session_cookie}",
        "Referer": f"{base_url}/console/personal",
        "Origin": base_url,
    }
    if api_user:
        headers["new-api-user"] = api_user

    # 执行签到
    with httpx.Client(timeout=30.0) as client:
        client.cookies.set("session", session_cookie, domain=cookie_domain)

        # 获取用户信息
        try:
            resp = client.get(f"{base_url}/api/user/self", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success"):
                    user_data = data.get("data", {})
                    quota = round(user_data.get("quota", 0) / 500000, 2)
                    used = round(user_data.get("used_quota", 0) / 500000, 2)
                    logger.info(f"[{site_name}] 💰 余额: {currency_unit}{quota}, 已用: {currency_unit}{used}")
        except Exception as e:
            logger.debug(f"[{site_name}] 获取用户信息失败: {e}")

        # 签到
        checkin_headers = headers.copy()
        checkin_headers["Content-Type"] = "application/json"

        try:
            resp = client.post(f"{base_url}/api/user/checkin", headers=checkin_headers)
            logger.info(f"[{site_name}] 签到响应: {resp.status_code}")

            if resp.status_code == 200:
                result = resp.json()
                if result.get("success"):
                    msg = result.get("message", "签到成功")
                    logger.success(f"[{site_name}] {msg}")
                    return {"status": "success", "message": msg}
                else:
                    msg = result.get("message", "签到失败")
                    if "已" in msg or "today" in msg.lower():
                        return {"status": "success", "message": msg}
                    return {"status": "failed", "message": msg}
            else:
                return {"status": "failed", "message": f"HTTP {resp.status_code}"}
        except Exception as e:
            return {"status": "failed", "message": str(e)}


async def test_with_shared_browser():
    """使用共享浏览器实例测试所有站点"""

    # 站点配置
    sites = [
        {"name": "WONG公益站", "base_url": "https://wzw.pp.ua", "cookie_domain": "wzw.pp.ua", "currency_unit": "$"},
        {"name": "Free DuckCoding", "base_url": "https://free.duckcoding.com", "cookie_domain": "free.duckcoding.com", "currency_unit": "¥"},
        {"name": "KFC API", "base_url": "https://kfc-api.sxxe.net", "cookie_domain": "kfc-api.sxxe.net", "currency_unit": "$"},
        {"name": "NEB公益站", "base_url": "https://ai.neb.cx", "cookie_domain": "ai.neb.cx", "currency_unit": "$"},
    ]

    account = config.linuxdo_accounts[0]
    results = []

    # 启动浏览器
    engine = get_browser_engine()
    logger.info(f"使用浏览器引擎: {engine}")

    headless = os.environ.get("BROWSER_HEADLESS", "true").lower() != "false"
    browser_manager = BrowserManager(engine=engine, headless=headless)

    try:
        await browser_manager.start()

        # 步骤1: 先登录 LinuxDO
        print("\n" + "=" * 60)
        print("步骤1: 登录 LinuxDO")
        print("=" * 60)

        login_success = await login_to_linuxdo(
            browser_manager,
            account.username,
            account.password
        )

        if not login_success:
            logger.warning("LinuxDO 登录可能失败，继续尝试...")

        # 步骤2: 依次访问各个站点
        print("\n" + "=" * 60)
        print("步骤2: 依次签到各站点")
        print("=" * 60)

        for site in sites:
            print(f"\n>>> {site['name']}")
            print("-" * 40)

            try:
                result = await checkin_site_simple(browser_manager, site)
                results.append({
                    "site": site["name"],
                    "status": result["status"],
                    "message": result["message"],
                })
                print(f"结果: {result['status']} - {result['message']}")
            except Exception as e:
                logger.error(f"[{site['name']}] 异常: {e}")
                import traceback
                traceback.print_exc()
                results.append({
                    "site": site["name"],
                    "status": "failed",
                    "message": str(e),
                })

            # 站点之间等待
            await asyncio.sleep(2)

    finally:
        # 关闭浏览器
        await browser_manager.close()

    # 打印汇总
    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    for r in results:
        status_icon = "✅" if r["status"] == "success" else "❌"
        print(f"{status_icon} {r['site']}: {r['status']} - {r['message']}")


asyncio.run(test_with_shared_browser())
