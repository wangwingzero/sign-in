#!/usr/bin/env python3
"""
NewAPI 信息提取工具（命令行版）

使用 Patchright/Playwright 打开浏览器，自动从 NewAPI 站点提取：
- 用户名 (username)
- API User ID
- API Key (token)

运行方式: uv run python scripts/newapi_extractor_browser.py
"""

import asyncio
import contextlib
import json
import sys
from pathlib import Path

# 添加项目根目录到 path
sys.path.insert(0, str(Path(__file__).parent.parent))

# 尝试导入浏览器库
try:
    from patchright.async_api import async_playwright
    BROWSER_LIB = "patchright"
except ImportError:
    try:
        from playwright.async_api import async_playwright
        BROWSER_LIB = "playwright"
    except ImportError:
        print("❌ 请先安装 patchright 或 playwright:")
        print("   uv add patchright")
        sys.exit(1)


# NewAPI 站点配置
NEWAPI_SITES = {
    "wong": {
        "name": "WONG公益站",
        "url": "https://wzw.pp.ua",
        "linuxdo_user": "jason_wong1",
    },
    "elysiver": {
        "name": "Elysiver",
        "url": "https://elysiver.h-e.top",
        "linuxdo_user": "bytebender",
    },
    "kfcapi": {
        "name": "KFC API",
        "url": "https://kfc-api.sxxe.net",
        "linuxdo_user": "kkkyyx",
    },
    "duckcoding": {
        "name": "Free DuckCoding",
        "url": "https://free.duckcoding.com",
        "linuxdo_user": "wcyrus",
    },
    "runanytime": {
        "name": "随时跑路",
        "url": "https://runanytime.hxi.me",
        "linuxdo_user": "henryxiaoyang",
    },
    "neb": {
        "name": "NEB公益站",
        "url": "https://ai.zzhdsgsss.xyz",
        "linuxdo_user": "simon_z",
    },
    "zeroliya": {
        "name": "小呆公益站",
        "url": "https://new.184772.xyz",
        "linuxdo_user": "zeroliya",
    },
    "mitchll": {
        "name": "Mitchll-api",
        "url": "https://api.mitchll.com",
        "linuxdo_user": "mitchll",
    },
    "anyrouter": {
        "name": "AnyRouter",
        "url": "https://anyrouter.top",
        "linuxdo_user": "technologystar",
    },
    "zhongruan": {
        "name": "钟阮公益站",
        "url": "https://gyapi.zxiaoruan.cn",
        "linuxdo_user": "zhongruan",
    },
    "apikey": {
        "name": "apikey公益站",
        "url": "https://welfare.apikey.cc",
        "linuxdo_user": "freenessfish",
    },
    "lightllm": {
        "name": "轻のLLM",
        "url": "https://lightllm.online",
        "linuxdo_user": "foward",
    },
    "windhub": {
        "name": "Wind Hub公益站",
        "url": "https://api.224442.xyz",
        "linuxdo_user": "beizhi",
    },
    "hotaru": {
        "name": "Hotaru API",
        "url": "https://api.hotaruapi.top",
        "linuxdo_user": "mazhichen8780",
    },
    "dev88": {
        "name": "DEV88公益站",
        "url": "https://api.dev88.tech",
        "linuxdo_user": "sc0152",
    },
}

BROWSER_DATA_DIR = Path("browser_data/newapi_extractor")


async def extract_from_site(page, site_id: str, config: dict) -> dict | None:
    """从单个站点提取信息"""
    url = f"{config['url']}/console/personal"
    print(f"\n📍 正在访问: {config['name']} ({url})")

    try:
        await page.goto(url, timeout=30000)
        await page.wait_for_load_state("networkidle", timeout=10000)
        await asyncio.sleep(1)

        # 从 localStorage 获取用户信息
        user_info = await page.evaluate("""
            () => {
                try {
                    const userStr = localStorage.getItem('user');
                    const user = userStr ? JSON.parse(userStr) : {};
                    const token = localStorage.getItem('token') || '';
                    return {
                        username: user.username || user.display_name || '',
                        api_user: user.id ? String(user.id) : '',
                        api_key: token,
                        email: user.email || '',
                        success: true
                    };
                } catch (e) {
                    return { success: false, error: e.message };
                }
            }
        """)

        if user_info and user_info.get('success') and user_info.get('username'):
            print(f"  ✅ 用户名: {user_info['username']}")
            print(f"  ✅ API User: {user_info['api_user']}")
            api_key_display = user_info['api_key'][:30] + "..." if user_info.get('api_key') else "未找到"
            print(f"  ✅ API Key: {api_key_display}")
            return {
                "site_id": site_id,
                "name": config["name"],
                "url": config["url"],
                "linuxdo_user": config["linuxdo_user"],
                **user_info
            }
        else:
            print("  ❌ 未登录或无法获取信息")
            return None

    except Exception as e:
        print(f"  ❌ 访问失败: {e}")
        return None


async def login_mode(selected_sites: list):
    """登录模式：打开浏览器让用户登录"""
    print("\n🌐 正在启动浏览器...")
    print("请在浏览器中登录各站点，完成后关闭浏览器窗口。\n")

    BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_DATA_DIR),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )

        page = context.pages[0] if context.pages else await context.new_page()

        # 打开第一个站点的登录页
        if selected_sites:
            first_site = NEWAPI_SITES[selected_sites[0]]
            await page.goto(f"{first_site['url']}/login")

        print("💡 提示：登录完成后，关闭浏览器窗口即可。")

        # 等待用户关闭浏览器
        with contextlib.suppress(Exception):
            await page.wait_for_event("close", timeout=600000)

        await context.close()

    print("\n✅ 浏览器已关闭。")


async def extract_mode(selected_sites: list) -> list:
    """提取模式：从已登录的站点提取信息"""
    results = []

    if not BROWSER_DATA_DIR.exists():
        print("❌ 浏览器数据目录不存在，请先运行登录模式。")
        return results

    print(f"\n🔍 使用 {BROWSER_LIB} 提取信息...")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_DATA_DIR),
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )

        page = context.pages[0] if context.pages else await context.new_page()

        for site_id in selected_sites:
            config = NEWAPI_SITES[site_id]
            result = await extract_from_site(page, site_id, config)
            if result:
                results.append(result)

        await context.close()

    return results


def print_results(results: list):
    """打印结果"""
    print("\n" + "=" * 60)
    print("📊 提取结果")
    print("=" * 60)

    if results:
        # 生成 Markdown 表格
        for item in results:
            print(f"\n### {item['name']}\n")
            print("| 项目 | 值 |")
            print("| -------- | --------------------------------------------------- |")
            print(f"| 用户名 | {item['username']} |")
            print(f"| API User | {item['api_user']} |")
            print(f"| API Key | {item['api_key']} |")

        # 保存到文件
        output_file = "newapi_extracted.json"
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"\n✅ 结果已保存到 {output_file}")

        # 生成汇总格式
        print("\n## 中转站汇总格式\n")
        for item in results:
            linuxdo_url = f"https://linux.do/u/{item['linuxdo_user']}/summary"
            print(f"{linuxdo_url}\t{item['name']}\t{item['url']}")
    else:
        print("\n❌ 未提取到任何信息")
        print("请先运行登录模式，在浏览器中登录各站点。")


async def main():
    """主函数"""
    print("=" * 60)
    print("🔑 NewAPI 信息提取工具")
    print("=" * 60)

    # 显示可用站点
    print("\n可用站点:")
    site_keys = list(NEWAPI_SITES.keys())
    for i, site_id in enumerate(site_keys, 1):
        config = NEWAPI_SITES[site_id]
        print(f"  {i:2}. {config['name']} ({config['url']})")

    # 选择站点
    print("\n输入要操作的站点编号（用逗号分隔），或输入 'all' 选择全部:")
    choice = input("> ").strip()

    if choice.lower() == 'all':
        selected_sites = site_keys
    else:
        try:
            indices = [int(x.strip()) - 1 for x in choice.split(",")]
            selected_sites = [site_keys[i] for i in indices if 0 <= i < len(site_keys)]
        except ValueError:
            print("❌ 输入无效")
            return

    if not selected_sites:
        print("❌ 未选择任何站点")
        return

    print(f"\n已选择: {', '.join(selected_sites)}")

    # 选择模式
    print("\n选择操作模式:")
    print("  1. 登录模式 - 打开浏览器登录各站点")
    print("  2. 提取模式 - 从已登录的站点提取信息")
    print("  3. 完整流程 - 先登录再提取")

    mode = input("\n请选择 (1/2/3): ").strip()

    if mode == "1":
        await login_mode(selected_sites)
    elif mode == "2":
        results = await extract_mode(selected_sites)
        print_results(results)
    elif mode == "3":
        await login_mode(selected_sites)
        print("\n准备提取信息...")
        results = await extract_mode(selected_sites)
        print_results(results)
    else:
        print("❌ 无效选择")


if __name__ == "__main__":
    asyncio.run(main())
