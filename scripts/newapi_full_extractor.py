#!/usr/bin/env python3
"""
NewAPI 签到配置完整提取工具

自动从浏览器提取：
- session cookie (通过 rookiepy)
- api_user (通过读取 localStorage LevelDB)

生成完整的签到配置 JSON，可直接用于 GitHub Secrets

运行方式: uv run python scripts/newapi_full_extractor.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

LOG_FILE = "newapi_full_extract.log"


def log(message: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] {message}"
    print(log_line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(log_line + "\n")
    except Exception:
        pass


def check_and_install_deps():
    """检查并安装依赖"""
    import importlib.util

    missing = []

    if importlib.util.find_spec("rookiepy") is None:
        missing.append("rookiepy")

    if importlib.util.find_spec("leveldb") is None:
        missing.append("leveldb-py")

    if missing:
        print(f"正在安装缺失的依赖: {', '.join(missing)}")
        try:
            subprocess.check_call(["uv", "add"] + missing)
        except (FileNotFoundError, subprocess.CalledProcessError):
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install"] + missing)
            except subprocess.CalledProcessError:
                print("\n❌ 自动安装失败，请手动运行:")
                print(f"   uv add {' '.join(missing)}")
                sys.exit(1)
        print("依赖安装完成，请重新运行脚本")
        sys.exit(0)


check_and_install_deps()

import rookiepy  # noqa: E402

try:
    import leveldb
    HAS_LEVELDB = True
except ImportError:
    HAS_LEVELDB = False
    log("警告: leveldb-py 导入失败，将无法自动读取 localStorage")


# 站点配置
SITES_CONFIG = {
    "wong": {"domain": "wzw.pp.ua", "name": "WONG公益站", "provider": "wong"},
    "elysiver": {"domain": "h-e.top", "name": "Elysiver", "provider": "elysiver"},
    "kfcapi": {"domain": "kfc-api.sxxe.net", "name": "KFC API", "provider": "kfcapi"},
    "duckcoding": {"domain": "free.duckcoding.com", "name": "Free DuckCoding", "provider": "duckcoding"},
    "runanytime": {"domain": "runanytime.hxi.me", "name": "随时跑路", "provider": "runanytime"},
    "neb": {"domain": "ai.zzhdsgsss.xyz", "name": "NEB公益站", "provider": "neb"},
    "zeroliya": {"domain": "new.184772.xyz", "name": "小呆公益站", "provider": "zeroliya"},
    "mitchll": {"domain": "api.mitchll.com", "name": "Mitchll-api", "provider": "mitchll"},
    "anyrouter": {"domain": "anyrouter.top", "name": "AnyRouter", "provider": "anyrouter"},
}


def get_browser_paths():
    """获取浏览器数据路径"""
    local_app_data = os.environ.get("LOCALAPPDATA", "")

    paths = {
        "edge": {
            "localStorage": Path(local_app_data) / "Microsoft/Edge/User Data/Default/Local Storage/leveldb",
            "name": "Microsoft Edge",
        },
        "chrome": {
            "localStorage": Path(local_app_data) / "Google/Chrome/User Data/Default/Local Storage/leveldb",
            "name": "Google Chrome",
        },
    }
    return paths


def read_localstorage_for_domain(ldb_path: Path, target_domain: str) -> dict:
    """从 LevelDB 读取指定域名的 localStorage"""
    if not HAS_LEVELDB:
        return {}

    if not ldb_path.exists():
        log(f"localStorage 路径不存在: {ldb_path}")
        return {}

    # 复制到临时目录（避免浏览器锁定问题）
    temp_dir = tempfile.mkdtemp()
    temp_ldb = Path(temp_dir) / "leveldb"

    try:
        shutil.copytree(ldb_path, temp_ldb)

        result = {}
        db = leveldb.DB(str(temp_ldb))

        for key, value in db.scan():
            try:
                # Chrome localStorage key 格式: _https://domain\x00\x01key
                key_str = key.decode("utf-8", errors="ignore")
                if target_domain in key_str:
                    # 提取实际的 key 名称
                    # 格式通常是: _https://domain\x00\x01actualkey
                    parts = key_str.split("\x00\x01")
                    if len(parts) >= 2:
                        actual_key = parts[-1]
                        # value 可能有前缀，尝试解码
                        value_str = value.decode("utf-8", errors="ignore")
                        # 去掉可能的前缀字符
                        if value_str.startswith("\x01"):
                            value_str = value_str[1:]
                        result[actual_key] = value_str
            except Exception:
                continue

        db.close()
        return result

    except Exception as e:
        log(f"读取 localStorage 失败: {e}")
        return {}
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def extract_user_info_from_localstorage(ls_data: dict) -> dict:
    """从 localStorage 数据中提取用户信息"""
    user_info = {}

    # 尝试解析 'user' 键
    if "user" in ls_data:
        try:
            user_data = json.loads(ls_data["user"])
            user_info["username"] = user_data.get("username") or user_data.get("display_name", "")
            user_info["api_user"] = str(user_data.get("id", ""))
        except (json.JSONDecodeError, TypeError):
            pass

    return user_info


def get_cookies_with_rookiepy(browser: str, domains: list) -> dict:
    """使用 rookiepy 获取 cookies"""
    cookies_by_domain = {}

    try:
        if browser == "edge":
            all_cookies = rookiepy.edge(domains)
        elif browser == "chrome":
            all_cookies = rookiepy.chrome(domains)
        else:
            return cookies_by_domain

        for cookie in all_cookies:
            domain = cookie.get("domain", "").lstrip(".")
            if domain not in cookies_by_domain:
                cookies_by_domain[domain] = {}
            cookies_by_domain[domain][cookie["name"]] = cookie["value"]

    except Exception as e:
        log(f"rookiepy 获取 cookies 失败: {e}")

    return cookies_by_domain


def is_admin():
    """检查是否以管理员权限运行"""
    try:
        import ctypes
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def main():
    print("=" * 60)
    print("🔑 NewAPI 签到配置完整提取工具")
    print("=" * 60)

    # 检查管理员权限
    if not is_admin():
        print("\n⚠️  警告: 未以管理员权限运行！")
        print("   Edge/Chrome v130+ 需要管理员权限才能解密 cookies")
        print("   请右键点击终端，选择「以管理员身份运行」后重试\n")

    print("\n⚠️  请先关闭浏览器再运行此工具！\n")

    # 选择浏览器
    print("选择浏览器:")
    print("  1. Microsoft Edge")
    print("  2. Google Chrome")
    browser_choice = input("\n请选择 (1/2): ").strip()

    browser = "edge" if browser_choice == "1" else "chrome"
    browser_paths = get_browser_paths()

    if browser not in browser_paths:
        print("❌ 不支持的浏览器")
        return

    browser_info = browser_paths[browser]
    print(f"\n使用浏览器: {browser_info['name']}")

    # 获取所有域名
    domains = [config["domain"] for config in SITES_CONFIG.values()]

    # 获取 cookies
    print("\n📦 正在提取 cookies...")
    cookies_by_domain = get_cookies_with_rookiepy(browser, domains)

    # 获取 localStorage
    print("📦 正在提取 localStorage...")
    ls_path = browser_info["localStorage"]

    results = []

    for _site_id, config in SITES_CONFIG.items():
        domain = config["domain"]
        provider = config["provider"]
        name = config["name"]

        print(f"\n处理: {name} ({domain})")

        # 查找 session cookie
        session = None
        for cookie_domain, cookies in cookies_by_domain.items():
            if domain in cookie_domain or cookie_domain in domain:
                session = cookies.get("session")
                if session:
                    break

        if not session:
            print("  ❌ 未找到 session cookie (可能未登录)")
            continue

        print("  ✅ 找到 session cookie")

        # 读取 localStorage 获取 api_user
        ls_data = read_localstorage_for_domain(ls_path, domain)
        user_info = extract_user_info_from_localstorage(ls_data)

        api_user = user_info.get("api_user", "")
        username = user_info.get("username", "")

        if api_user:
            print(f"  ✅ api_user: {api_user}")
        else:
            print("  ⚠️  未能自动获取 api_user")
            api_user = input(f"  请输入 {name} 的 api_user (用户ID，可在网页个人中心查看): ").strip()

        if not api_user:
            print(f"  ❌ 跳过 {name}（缺少 api_user）")
            continue

        # 生成配置
        account_name = username or f"{provider}_{api_user}"
        config_item = {
            "name": account_name,
            "provider": provider,
            "cookies": {"session": session},
            "api_user": api_user,
        }

        results.append(config_item)
        print(f"  ✅ 配置生成成功: {account_name}")

    # 输出结果
    print("\n" + "=" * 60)
    print("📊 提取结果")
    print("=" * 60)

    if results:
        json_output = json.dumps(results, indent=2, ensure_ascii=False)
        print("\n生成的 JSON 配置:\n")
        print(json_output)

        # 保存到文件
        output_file = "newapi_accounts.json"
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(json_output)
        print(f"\n✅ 已保存到 {output_file}")

        # 复制到剪贴板
        try:
            import pyperclip
            pyperclip.copy(json_output)
            print("✅ 已复制到剪贴板")
        except ImportError:
            print("💡 安装 pyperclip 可自动复制到剪贴板: uv add pyperclip")

        print("\n📋 使用方法:")
        print("  1. 复制上面的 JSON")
        print("  2. 到 GitHub 仓库 Settings → Secrets → Actions")
        print("  3. 添加/更新 NEWAPI_ACCOUNTS secret")

    else:
        print("\n❌ 未提取到任何配置")
        print("请确保已在浏览器中登录各站点，并关闭浏览器后重试")


if __name__ == "__main__":
    main()
