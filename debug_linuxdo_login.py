#!/usr/bin/env python3
"""调试 LinuxDO 登录页面结构"""
import asyncio
import nodriver as uc
from loguru import logger


async def main():
    """调试登录页面"""
    
    logger.info("启动浏览器...")
    browser = await uc.start(headless=False)
    
    try:
        tab = await browser.get("https://linux.do/login")
        logger.info("等待页面加载...")
        await asyncio.sleep(10)  # 等待 Cloudflare 和页面加载
        
        # 检查当前 URL
        current_url = tab.target.url if hasattr(tab, 'target') else ""
        print(f"\n当前 URL: {current_url}")
        
        # 获取页面标题
        try:
            page_title = await tab.evaluate("document.title")
            print(f"页面标题: {page_title}")
        except Exception as e:
            print(f"获取标题失败: {e}")
        
        # 尝试不同的选择器
        print("\n--- 尝试查找登录元素 ---")
        
        selectors_to_try = [
            '#login-account-name',
            '#login-account-password', 
            '#login-button',
            'input[name="login"]',
            'input[name="password"]',
            'input[type="text"]',
            'input[type="password"]',
            'input[type="email"]',
            '.login-form input',
            '#login-form input',
            'button[type="submit"]',
            '.btn-primary',
            '.login-button',
        ]
        
        for selector in selectors_to_try:
            try:
                elem = await tab.select(selector, timeout=2)
                if elem:
                    print(f"  ✓ 找到: {selector}")
                else:
                    print(f"  ✗ 未找到: {selector}")
            except Exception as e:
                print(f"  ✗ {selector}: {e}")
        
        # 使用 find 查找文本
        print("\n--- 尝试 find 文本 ---")
        texts_to_find = ['登录', 'Login', '用户名', '密码', 'Username', 'Password', 'Email']
        
        for text in texts_to_find:
            try:
                elem = await tab.find(text, timeout=2)
                if elem:
                    print(f"  ✓ 找到文本: '{text}'")
            except Exception as e:
                print(f"  ✗ 未找到文本 '{text}': {type(e).__name__}")
        
        # 获取页面 HTML 片段
        print("\n--- 页面 body 前 2000 字符 ---")
        try:
            html = await tab.evaluate("document.body.innerHTML.substring(0, 2000)")
            print(html)
        except Exception as e:
            print(f"获取 HTML 失败: {e}")
        
        print("\n\n等待 120 秒后关闭（可以手动查看页面）...")
        await asyncio.sleep(120)
        
    finally:
        browser.stop()


if __name__ == "__main__":
    asyncio.run(main())
