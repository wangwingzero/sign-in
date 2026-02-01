#!/usr/bin/env python3
"""
测试 nodriver 登录 LinuxDO 并浏览帖子

测试账号：
- 用户名: 15021912101@139.com
- 密码: Hu20100416
"""
import asyncio
import json
import random

import nodriver as uc
from loguru import logger


async def wait_for_cloudflare(tab, timeout: int = 30):
    """等待 Cloudflare 挑战完成"""
    logger.info("检测 Cloudflare 挑战...")
    
    start_time = asyncio.get_event_loop().time()
    
    while asyncio.get_event_loop().time() - start_time < timeout:
        try:
            # 获取页面标题
            title = await tab.evaluate("document.title")
            
            # Cloudflare 挑战页面的特征
            cf_indicators = [
                "just a moment",
                "checking your browser",
                "please wait",
                "verifying",
                "something went wrong",
            ]
            
            title_lower = title.lower() if title else ""
            
            # 检查是否还在 Cloudflare 挑战中
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


async def main():
    """主函数：登录 LinuxDO 并浏览帖子"""
    
    # 测试账号
    username = "15021912101@139.com"
    password = "Hu20100416"
    
    logger.info("启动 nodriver 浏览器...")
    
    # 启动浏览器（非 headless 模式，更容易通过 Cloudflare）
    browser = await uc.start(
        headless=False,
        browser_args=[
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--disable-blink-features=AutomationControlled",
        ]
    )
    
    try:
        # 获取主标签页
        tab = await browser.get("about:blank")
        
        # 1. 先访问首页，让 Cloudflare 验证
        logger.info("访问 LinuxDO 首页...")
        await tab.get("https://linux.do")
        
        # 2. 等待 Cloudflare 挑战完成
        cf_passed = await wait_for_cloudflare(tab, timeout=30)
        if not cf_passed:
            # 尝试刷新页面
            logger.info("尝试刷新页面...")
            await tab.reload()
            await wait_for_cloudflare(tab, timeout=20)
        
        # 3. 访问登录页面
        logger.info("访问登录页面...")
        await tab.get("https://linux.do/login")
        await asyncio.sleep(3)
        
        # 4. 填写登录表单
        logger.info("填写登录表单...")
        
        # 等待登录表单加载 - 增加等待时间
        logger.info("等待登录表单加载...")
        await asyncio.sleep(5)
        
        # 使用 JS 等待输入框出现
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
        
        # 查找用户名输入框 - 使用 find 方法更可靠
        try:
            # 先尝试 select
            username_input = await tab.select('#login-account-name', timeout=5)
            if not username_input:
                # 尝试其他选择器
                username_input = await tab.select('input[name="login"]', timeout=3)
            if not username_input:
                # 尝试 type=text
                username_input = await tab.select('input[type="text"]', timeout=3)
            
            if username_input:
                await username_input.click()
                await asyncio.sleep(0.3)
                await username_input.send_keys(username)
                logger.info(f"已输入用户名: {username}")
                await asyncio.sleep(0.5)
            else:
                logger.error("未找到用户名输入框")
                # 打印页面信息帮助调试
                title = await tab.evaluate("document.title")
                url = tab.target.url if hasattr(tab, 'target') else ""
                logger.error(f"当前页面: {title} - {url}")
                
                # 保存截图
                try:
                    await tab.save_screenshot("no_input_found.png")
                    logger.info("已保存截图: no_input_found.png")
                except:
                    pass
                return
        except Exception as e:
            logger.error(f"输入用户名失败: {e}")
            return
        
        # 5. 查找密码输入框
        try:
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
                return
        except Exception as e:
            logger.error(f"输入密码失败: {e}")
            return
        
        # 6. 点击登录按钮
        logger.info("点击登录按钮...")
        try:
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
                return
        except Exception as e:
            logger.error(f"点击登录按钮失败: {e}")
            return
        
        # 7. 等待登录完成
        logger.info("等待登录完成...")
        
        # 等待登录按钮状态变化或页面跳转
        for i in range(30):  # 最多等待 30 秒
            await asyncio.sleep(1)
            
            # 检查 URL 是否变化
            current_url = tab.target.url if hasattr(tab, 'target') else ""
            if "login" not in current_url.lower():
                logger.info(f"页面已跳转: {current_url}")
                break
            
            # 检查登录按钮状态
            try:
                btn_text = await tab.evaluate("""
                    (function() {
                        const btn = document.querySelector('#login-button') || document.querySelector('button[type="submit"]');
                        return btn ? btn.innerText.trim() : '';
                    })()
                """)
                if btn_text and "登录" in btn_text and "正在" not in btn_text:
                    # 按钮恢复为"登录"状态，可能登录失败
                    logger.warning(f"登录按钮状态: {btn_text}")
                    break
            except:
                pass
            
            if i % 5 == 0:
                logger.debug(f"等待登录... ({i}s)")
        
        await asyncio.sleep(2)  # 额外等待
        
        # 8. 检查登录状态
        current_url = tab.target.url if hasattr(tab, 'target') else ""
        logger.info(f"当前 URL: {current_url}")
        
        if "login" in current_url.lower():
            logger.error("登录失败，仍在登录页面")
            # 尝试获取错误信息
            try:
                # 检查是否有错误提示
                error_elem = await tab.select('.alert-error', timeout=2)
                if error_elem:
                    error_text = await error_elem.get_property('innerText')
                    logger.error(f"错误信息: {error_text}")
                
                # 检查其他可能的错误元素
                flash_error = await tab.select('#flash-text', timeout=2)
                if flash_error:
                    flash_text = await flash_error.get_property('innerText')
                    logger.error(f"Flash 错误: {flash_text}")
                
                # 使用 JS 获取所有可能的错误信息
                errors = await tab.evaluate("""
                    const errors = [];
                    document.querySelectorAll('.alert, .error, .flash, [class*="error"]').forEach(el => {
                        if (el.innerText.trim()) errors.push(el.innerText.trim());
                    });
                    return errors.slice(0, 5);
                """)
                if errors:
                    logger.error(f"页面错误信息: {errors}")
                    
            except Exception as e:
                logger.debug(f"获取错误信息失败: {e}")
            
            # 截图保存
            try:
                await tab.save_screenshot("login_failed.png")
                logger.info("已保存截图: login_failed.png")
            except:
                pass
            
            return
        
        logger.success("登录成功！")
        
        # 9. 获取 cookies
        logger.info("获取 cookies...")
        cookies = {}
        try:
            import nodriver.cdp.network as cdp_network
            all_cookies = await tab.send(cdp_network.get_all_cookies())
            for cookie in all_cookies:
                cookies[cookie.name] = cookie.value
            logger.info(f"获取到 {len(cookies)} 个 cookies")
            
            # 打印关键 cookies
            for key in ['_forum_session', '_t', 'cf_clearance']:
                if key in cookies:
                    logger.info(f"  {key}: {cookies[key][:30]}...")
        except Exception as e:
            logger.warning(f"获取 cookies 失败: {e}")
        
        # 10. 浏览帖子
        logger.info("开始浏览帖子...")
        
        # 访问最新帖子页面
        await tab.get("https://linux.do/latest")
        await asyncio.sleep(5)  # 等待页面加载
        
        # 等待帖子列表加载
        for _ in range(10):
            has_topics = await tab.evaluate("document.querySelectorAll('a.title').length > 0")
            if has_topics:
                break
            await asyncio.sleep(1)
        
        # 获取帖子链接
        try:
            # 使用 JavaScript 获取帖子链接 - 使用 JSON.stringify 确保返回正确格式
            topic_links_json = await tab.evaluate("""
                (function() {
                    const links = document.querySelectorAll('a.title.raw-link, a.title[href*="/t/"]');
                    const result = [];
                    for (let i = 0; i < Math.min(links.length, 10); i++) {
                        const a = links[i];
                        if (a.href && a.href.includes('/t/')) {
                            result.push({
                                href: a.href,
                                title: (a.innerText || a.textContent || '').trim().substring(0, 50)
                            });
                        }
                    }
                    return JSON.stringify(result);
                })()
            """)
            
            # 解析 JSON 结果
            import json
            topic_links = []
            if topic_links_json and isinstance(topic_links_json, str):
                try:
                    topic_links = json.loads(topic_links_json)
                except json.JSONDecodeError:
                    logger.warning(f"JSON 解析失败: {topic_links_json[:100]}")
            elif isinstance(topic_links_json, list):
                topic_links = topic_links_json
            else:
                logger.warning(f"意外的返回类型: {type(topic_links_json)} - {topic_links_json}")
            
            logger.info(f"找到 {len(topic_links)} 个帖子")
            
            # 随机选择 3-5 个帖子浏览
            browse_count = min(random.randint(3, 5), len(topic_links))
            selected = random.sample(topic_links, browse_count)
            
            for i, topic in enumerate(selected):
                title = topic.get('title', 'Unknown')[:40]
                href = topic.get('href', '')
                
                logger.info(f"[{i+1}/{browse_count}] 浏览: {title}...")
                
                # 访问帖子
                await tab.get(href)
                
                # 模拟阅读（随机等待 3-8 秒）
                read_time = random.uniform(3, 8)
                logger.info(f"  阅读 {read_time:.1f} 秒...")
                await asyncio.sleep(read_time)
                
                # 滚动页面
                await tab.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                await asyncio.sleep(1)
            
            logger.success(f"成功浏览 {browse_count} 个帖子！")
            
        except Exception as e:
            logger.error(f"浏览帖子失败: {e}")
        
        # 11. 保持浏览器打开一段时间
        logger.info("测试完成，30 秒后关闭浏览器...")
        await asyncio.sleep(30)
        
    except Exception as e:
        logger.error(f"发生错误: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        # 关闭浏览器
        logger.info("关闭浏览器...")
        browser.stop()


if __name__ == "__main__":
    asyncio.run(main())
