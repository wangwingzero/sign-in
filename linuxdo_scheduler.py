#!/usr/bin/env python3
"""
LinuxDO 多账号定时刷帖脚本

功能：
- 支持多账号配置
- 定时执行（早上5点、8点、晚上10点）
- 根据账号等级分配浏览时间
- 每次运行1.5小时

等级说明：
- level 1: 最高优先级，刷帖时间最长
- level 2: 中等优先级
- level 3: 最低优先级，刷帖时间最短（约10分钟）
"""
import asyncio
import json
import random
import sys
from datetime import datetime
from pathlib import Path

import nodriver as uc
import schedule
from loguru import logger

# 配置日志
logger.remove()
logger.add(
    sys.stdout,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{message}</cyan>",
    level="INFO"
)
logger.add(
    "logs/linuxdo_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="7 days",
    level="DEBUG"
)

# 账号配置文件路径
# 请复制 accounts.example.json 为 accounts.json 并填写你的账号信息
ACCOUNTS_FILE = Path(__file__).parent / "accounts.json"


def load_accounts() -> list:
    """从配置文件加载账号信息"""
    if not ACCOUNTS_FILE.exists():
        logger.error(f"账号配置文件不存在: {ACCOUNTS_FILE}")
        logger.error("请复制 accounts.example.json 为 accounts.json 并填写账号信息")
        sys.exit(1)
    
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            accounts = json.load(f)
        logger.info(f"已加载 {len(accounts)} 个账号")
        return accounts
    except json.JSONDecodeError as e:
        logger.error(f"账号配置文件格式错误: {e}")
        sys.exit(1)

# 定时配置
SCHEDULE_TIMES = ["05:00", "08:00", "22:00"]

# 总运行时间（分钟）
TOTAL_RUN_TIME_MINUTES = 90

# 各等级时间分配（分钟）
LEVEL_TIME_CONFIG = {
    1: 20,  # level 1 每个账号20分钟（快速刷帖，每帖至少1分钟）
    2: 15,  # level 2 每个账号15分钟（中速刷帖）
    3: 10,  # level 3 每个账号10分钟（慢速刷帖）
}


def calculate_time_allocation(accounts: list) -> dict:
    """
    根据账号等级计算时间分配
    
    Returns:
        dict: {username: browse_minutes}
    """
    allocation = {}
    
    for account in accounts:
        if not account.get("browse_enabled", True):
            allocation[account["username"]] = 0
            continue
        
        level = account.get("level", 1)
        minutes = LEVEL_TIME_CONFIG.get(level, 15)
        allocation[account["username"]] = minutes
    
    # 计算总分配时间
    total_allocated = sum(allocation.values())
    logger.info(f"时间分配总计: {total_allocated} 分钟 (目标: {TOTAL_RUN_TIME_MINUTES} 分钟)")
    
    # 如果总时间超过限制，按比例缩减
    if total_allocated > TOTAL_RUN_TIME_MINUTES:
        ratio = TOTAL_RUN_TIME_MINUTES / total_allocated
        for username in allocation:
            allocation[username] = int(allocation[username] * ratio)
        logger.warning(f"时间超限，按比例缩减。调整后总计: {sum(allocation.values())} 分钟")
    
    return allocation


async def wait_for_cloudflare(tab, timeout: int = 30) -> bool:
    """等待 Cloudflare 挑战完成"""
    logger.info("检测 Cloudflare 挑战...")
    
    start_time = asyncio.get_event_loop().time()
    
    while asyncio.get_event_loop().time() - start_time < timeout:
        try:
            title = await tab.evaluate("document.title")
            
            cf_indicators = [
                "just a moment",
                "checking your browser",
                "please wait",
                "verifying",
                "something went wrong",
            ]
            
            title_lower = title.lower() if title else ""
            is_cf_page = any(ind in title_lower for ind in cf_indicators)
            
            if not is_cf_page and title and "linux" in title_lower:
                logger.success(f"Cloudflare 挑战通过！页面标题: {title}")
                return True
            
            if is_cf_page:
                logger.debug(f"等待 Cloudflare... 当前标题: {title}")
            
        except Exception as e:
            logger.debug(f"检查页面状态时出错: {e}")
        
        await asyncio.sleep(2)
    
    logger.warning(f"等待 Cloudflare 超时 ({timeout}s)")
    return False


async def login_account(tab, username: str, password: str) -> bool:
    """登录单个账号"""
    try:
        # 访问首页，通过 Cloudflare
        logger.info("访问 LinuxDO 首页...")
        await tab.get("https://linux.do")
        
        cf_passed = await wait_for_cloudflare(tab, timeout=30)
        if not cf_passed:
            logger.info("尝试刷新页面...")
            await tab.reload()
            await wait_for_cloudflare(tab, timeout=20)
        
        # 访问登录页面
        logger.info("访问登录页面...")
        await tab.get("https://linux.do/login")
        await asyncio.sleep(3)
        
        # 等待登录表单加载
        logger.info("等待登录表单加载...")
        await asyncio.sleep(5)
        
        for attempt in range(10):
            try:
                has_input = await tab.evaluate("""
                    const input = document.querySelector('#login-account-name') || 
                                  document.querySelector('input[name="login"]') ||
                                  document.querySelector('input[type="text"]');
                    return !!input;
                """)
                if has_input:
                    logger.info("登录表单已加载")
                    break
            except:
                pass
            await asyncio.sleep(1)
        
        # 输入用户名
        username_input = await tab.select('#login-account-name', timeout=5)
        if not username_input:
            username_input = await tab.select('input[name="login"]', timeout=3)
        if not username_input:
            username_input = await tab.select('input[type="text"]', timeout=3)
        
        if username_input:
            await username_input.click()
            await asyncio.sleep(0.3)
            await username_input.send_keys(username)
            logger.info(f"已输入用户名: {username}")
            await asyncio.sleep(0.5)
        else:
            logger.error("未找到用户名输入框")
            return False
        
        # 输入密码
        password_input = await tab.select('#login-account-password', timeout=5)
        if not password_input:
            password_input = await tab.select('input[type="password"]', timeout=3)
        
        if password_input:
            await password_input.click()
            await asyncio.sleep(0.3)
            await password_input.send_keys(password)
            logger.info("已输入密码")
            await asyncio.sleep(0.5)
        else:
            logger.error("未找到密码输入框")
            return False
        
        # 点击登录按钮
        logger.info("点击登录按钮...")
        login_btn = await tab.select('#login-button', timeout=5)
        if not login_btn:
            login_btn = await tab.select('button[type="submit"]', timeout=3)
        if not login_btn:
            login_btn = await tab.find("登录", timeout=3)
        
        if login_btn:
            await login_btn.mouse_click()
            logger.info("已点击登录按钮")
        else:
            logger.error("未找到登录按钮")
            return False
        
        # 等待登录完成
        logger.info("等待登录完成...")
        for i in range(30):
            await asyncio.sleep(1)
            
            current_url = tab.target.url if hasattr(tab, 'target') else ""
            if "login" not in current_url.lower():
                logger.info(f"页面已跳转: {current_url}")
                break
            
            if i % 5 == 0:
                logger.debug(f"等待登录... ({i}s)")
        
        await asyncio.sleep(2)
        
        # 检查登录状态
        current_url = tab.target.url if hasattr(tab, 'target') else ""
        if "login" in current_url.lower():
            logger.error("登录失败，仍在登录页面")
            return False
        
        logger.success("登录成功！")
        return True
        
    except Exception as e:
        logger.error(f"登录过程发生错误: {e}")
        return False


async def browse_topics(tab, browse_minutes: int, level: int = 1):
    """
    浏览帖子指定时间
    
    Args:
        tab: 浏览器标签页
        browse_minutes: 浏览时间（分钟）
        level: 账号等级 (1=快速刷, 2=中速, 3=慢速)
    """
    # 根据等级设置浏览参数
    if level == 1:
        # Level 1: 快速刷帖，找评论多的帖子，每个帖子至少刷1分钟
        min_time_per_topic = 60  # 每个帖子最少60秒
        scroll_delay_range = (0.8, 1.5)  # 滚动间隔
        sort_by_replies = True  # 按评论数排序
        max_topics_per_page = 30  # 每页获取更多帖子
        mode_name = "快速刷帖"
    elif level == 2:
        # Level 2: 中速刷帖
        read_time_range = (3, 8)
        scroll_delay_range = (0.5, 1.5)
        sort_by_replies = True
        max_topics_per_page = 20
        scroll_steps = 10
        mode_name = "中速刷帖"
    else:
        # Level 3: 慢速刷帖，模拟真实阅读
        read_time_range = (10, 30)
        scroll_delay_range = (2, 5)
        sort_by_replies = False
        max_topics_per_page = 10
        scroll_steps = 4
        mode_name = "慢速刷帖"
    
    logger.info(f"开始浏览帖子，计划时间: {browse_minutes} 分钟，模式: {mode_name}")
    
    start_time = asyncio.get_event_loop().time()
    end_time = start_time + browse_minutes * 60
    
    topics_browsed = 0
    
    try:
        # Level 1 优先访问热门帖子页面（评论多）
        if level == 1:
            await tab.get("https://linux.do/top")
        else:
            await tab.get("https://linux.do/latest")
        await asyncio.sleep(3 if level == 1 else 5)
        
        # 等待帖子列表加载
        for _ in range(10):
            has_topics = await tab.evaluate("document.querySelectorAll('a.title').length > 0")
            if has_topics:
                break
            await asyncio.sleep(1)
        
        while asyncio.get_event_loop().time() < end_time:
            # 获取帖子链接（包含评论数信息）
            topic_links_json = await tab.evaluate(f"""
                (function() {{
                    const rows = document.querySelectorAll('tr.topic-list-item, .topic-list-item');
                    const result = [];
                    for (let i = 0; i < Math.min(rows.length, {max_topics_per_page}); i++) {{
                        const row = rows[i];
                        const a = row.querySelector('a.title.raw-link, a.title[href*="/t/"]');
                        if (!a || !a.href || !a.href.includes('/t/')) continue;
                        
                        // 获取评论数
                        let replies = 0;
                        const repliesEl = row.querySelector('.posts .number, .replies .number, td.replies, .num.posts span');
                        if (repliesEl) {{
                            const text = repliesEl.innerText || repliesEl.textContent || '0';
                            replies = parseInt(text.replace(/[^0-9]/g, '')) || 0;
                        }}
                        
                        result.push({{
                            href: a.href,
                            title: (a.innerText || a.textContent || '').trim().substring(0, 50),
                            replies: replies
                        }});
                    }}
                    return JSON.stringify(result);
                }})()
            """)
            
            topic_links = []
            if topic_links_json and isinstance(topic_links_json, str):
                try:
                    topic_links = json.loads(topic_links_json)
                except json.JSONDecodeError:
                    logger.warning(f"JSON 解析失败: {topic_links_json[:100]}")
            elif isinstance(topic_links_json, list):
                topic_links = topic_links_json
            
            if not topic_links:
                logger.warning("未找到帖子，尝试刷新页面...")
                await tab.get("https://linux.do/top" if level == 1 else "https://linux.do/latest")
                await asyncio.sleep(3)
                continue
            
            # Level 1 按评论数排序，优先刷评论多的帖子
            if sort_by_replies:
                topic_links.sort(key=lambda x: x.get('replies', 0), reverse=True)
            else:
                random.shuffle(topic_links)
            
            for topic in topic_links:
                if asyncio.get_event_loop().time() >= end_time:
                    break
                
                title = topic.get('title', 'Unknown')[:40]
                href = topic.get('href', '')
                replies = topic.get('replies', 0)
                
                if level == 1:
                    logger.info(f"快刷: {title}... ({replies}条评论)")
                else:
                    logger.info(f"浏览: {title}...")
                
                # 访问帖子
                await tab.get(href)
                topics_browsed += 1
                
                # 计算剩余时间
                remaining = end_time - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                
                if level == 1:
                    # Level 1: 持续滚动直到到底，每个帖子至少刷1分钟
                    topic_start_time = asyncio.get_event_loop().time()
                    topic_min_end_time = topic_start_time + min_time_per_topic
                    
                    logger.info(f"  持续滚动刷评论，至少 {min_time_per_topic} 秒... (剩余 {remaining/60:.1f} 分钟)")
                    
                    # 等待页面加载完成
                    await asyncio.sleep(2)
                    
                    scroll_count = 0
                    reached_bottom = False
                    last_scroll_top = -1
                    no_change_count = 0
                    
                    # 持续滚动直到：1.到底了且满足最少时间 或 2.总时间用完
                    while asyncio.get_event_loop().time() < end_time:
                        current_time = asyncio.get_event_loop().time()
                        time_in_topic = current_time - topic_start_time
                        
                        # 检查是否可以退出当前帖子
                        if reached_bottom and current_time >= topic_min_end_time:
                            logger.info(f"  已到底且满足最少时间，切换下一帖 (本帖 {time_in_topic:.0f}s, 滚动 {scroll_count} 次)")
                            break
                        
                        # 获取当前滚动位置和页面高度
                        scroll_info = await tab.evaluate("""
                            JSON.stringify({
                                scrollTop: window.scrollY,
                                scrollHeight: document.body.scrollHeight,
                                clientHeight: window.innerHeight
                            })
                        """)
                        try:
                            info = json.loads(scroll_info)
                            scroll_top = info.get('scrollTop', 0)
                            scroll_height = info.get('scrollHeight', 0)
                            client_height = info.get('clientHeight', 0)
                        except:
                            scroll_top = 0
                            scroll_height = 1000
                            client_height = 500
                        
                        # 检查是否到底（滚动位置不再变化）
                        if scroll_top == last_scroll_top:
                            no_change_count += 1
                            if no_change_count >= 3:  # 连续3次位置不变，认为到底
                                if not reached_bottom:
                                    reached_bottom = True
                                    logger.info(f"  已滑到底部 (滚动 {scroll_count} 次)")
                        else:
                            no_change_count = 0
                        
                        last_scroll_top = scroll_top
                        
                        # 到底了但还没满足最少时间，继续等待（同时尝试滚动加载更多）
                        if reached_bottom:
                            if current_time < topic_min_end_time:
                                # 尝试触发加载更多内容
                                await tab.evaluate("window.scrollBy(0, 300)")
                                await asyncio.sleep(1)
                                continue
                        
                        # 继续滚动
                        scroll_amount = random.randint(400, 1000)
                        await tab.evaluate(f"window.scrollBy(0, {scroll_amount})")
                        scroll_count += 1
                        
                        # 滚动间隔
                        await asyncio.sleep(random.uniform(*scroll_delay_range))
                        
                        # 每滚动50次打印状态
                        if scroll_count % 50 == 0:
                            logger.info(f"  滚动中... ({scroll_count} 次, {time_in_topic:.0f}s)")
                else:
                    # Level 2/3: 正常阅读模式
                    read_time = min(random.uniform(*read_time_range), remaining)
                    logger.info(f"  阅读 {read_time:.1f} 秒... (剩余 {remaining/60:.1f} 分钟)")
                    await asyncio.sleep(read_time)
                    
                    # 滚动页面模拟阅读
                    for i in range(scroll_steps):
                        if asyncio.get_event_loop().time() >= end_time:
                            break
                        scroll_pos = (i + 1) / scroll_steps
                        await tab.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {scroll_pos})")
                        await asyncio.sleep(random.uniform(*scroll_delay_range))
            
            # 返回列表页获取新帖子
            if asyncio.get_event_loop().time() < end_time:
                if level == 1:
                    # Level 1: 快速切换分类，刷更多帖子
                    categories = [
                        "https://linux.do/top",
                        "https://linux.do/top?period=weekly",
                        "https://linux.do/top?period=monthly",
                        "https://linux.do/latest",
                    ]
                else:
                    categories = [
                        "https://linux.do/latest",
                        "https://linux.do/top",
                        "https://linux.do/new",
                    ]
                await tab.get(random.choice(categories))
                await asyncio.sleep(2 if level == 1 else 3)
        
        logger.success(f"浏览完成！共浏览 {topics_browsed} 个帖子")
        
    except Exception as e:
        logger.error(f"浏览帖子失败: {e}")


async def process_account(account: dict, browse_minutes: int):
    """处理单个账号"""
    name = account.get("name", account["username"])
    username = account["username"]
    password = account["password"]
    level = account.get("level", 1)
    
    logger.info(f"=" * 50)
    logger.info(f"开始处理账号: {name} ({username})")
    logger.info(f"分配浏览时间: {browse_minutes} 分钟, 等级: Level {level}")
    logger.info(f"=" * 50)
    
    if browse_minutes <= 0:
        logger.info(f"账号 {name} 浏览已禁用，跳过")
        return
    
    browser = None
    try:
        # 启动浏览器
        browser = await uc.start(
            headless=False,
            browser_args=[
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--disable-blink-features=AutomationControlled",
            ]
        )
        
        tab = await browser.get("about:blank")
        
        # 登录
        login_success = await login_account(tab, username, password)
        
        if login_success:
            # 浏览帖子（传入等级参数）
            await browse_topics(tab, browse_minutes, level)
        else:
            logger.error(f"账号 {name} 登录失败")
        
    except Exception as e:
        logger.error(f"处理账号 {name} 时发生错误: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        if browser:
            logger.info(f"关闭浏览器 ({name})...")
            browser.stop()
            await asyncio.sleep(2)  # 等待浏览器完全关闭


async def run_all_accounts():
    """运行所有账号"""
    start_time = datetime.now()
    logger.info(f"{'=' * 60}")
    logger.info(f"开始执行定时任务 - {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"{'=' * 60}")
    
    # 加载账号配置
    accounts = load_accounts()
    
    # 计算时间分配
    time_allocation = calculate_time_allocation(accounts)
    
    # 打印时间分配
    logger.info("时间分配:")
    for account in accounts:
        name = account.get("name", account["username"])
        minutes = time_allocation[account["username"]]
        level = account.get("level", 1)
        logger.info(f"  - {name} (Level {level}): {minutes} 分钟")
    
    # 按等级排序，高等级（数字小）优先
    sorted_accounts = sorted(accounts, key=lambda x: x.get("level", 1))
    
    # 依次处理每个账号
    for account in sorted_accounts:
        if not account.get("browse_enabled", True):
            continue
        
        browse_minutes = time_allocation[account["username"]]
        await process_account(account, browse_minutes)
    
    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds() / 60
    logger.info(f"{'=' * 60}")
    logger.info(f"定时任务完成 - 总耗时: {duration:.1f} 分钟")
    logger.info(f"{'=' * 60}")


def run_scheduled_task():
    """运行定时任务的包装函数"""
    try:
        asyncio.run(run_all_accounts())
    except Exception as e:
        logger.error(f"定时任务执行失败: {e}")
        import traceback
        traceback.print_exc()


def main():
    """主函数"""
    # 确保日志目录存在
    Path("logs").mkdir(exist_ok=True)
    
    logger.info("LinuxDO 多账号定时刷帖脚本启动")
    logger.info(f"定时执行时间: {', '.join(SCHEDULE_TIMES)}")
    logger.info(f"每次运行时间: {TOTAL_RUN_TIME_MINUTES} 分钟")
    
    # 检查命令行参数
    if len(sys.argv) > 1 and sys.argv[1] == "--now":
        logger.info("立即执行模式")
        run_scheduled_task()
        return
    
    # 设置定时任务
    for time_str in SCHEDULE_TIMES:
        schedule.every().day.at(time_str).do(run_scheduled_task)
        logger.info(f"已设置定时任务: 每天 {time_str}")
    
    logger.info("定时任务已启动，等待执行...")
    logger.info("提示: 使用 --now 参数可立即执行一次")
    
    # 运行调度器
    while True:
        schedule.run_pending()
        
        # 显示下次执行时间
        next_run = schedule.next_run()
        if next_run:
            logger.debug(f"下次执行时间: {next_run}")
        
        asyncio.run(asyncio.sleep(60))  # 每分钟检查一次


if __name__ == "__main__":
    main()
