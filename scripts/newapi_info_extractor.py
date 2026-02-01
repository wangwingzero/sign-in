#!/usr/bin/env python3
"""
NewAPI 信息提取 GUI 工具

一键从浏览器提取 NewAPI 站点的登录信息：
- 用户名 (username)
- API User ID
- API Key

使用 Patchright/Playwright 打开浏览器，自动提取 localStorage 中的信息。
运行方式: uv run python scripts/newapi_info_extractor.py
"""

import asyncio
import contextlib
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

LOG_FILE = "newapi_extract.log"


def log(message: str):
    """写入日志"""
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

    if importlib.util.find_spec("customtkinter") is None:
        missing.append("customtkinter")

    # 检查 patchright 或 playwright
    if importlib.util.find_spec("patchright") is None and importlib.util.find_spec("playwright") is None:
        missing.append("patchright")

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

import customtkinter as ctk  # noqa: E402

# 尝试导入浏览器库
try:
    from patchright.async_api import async_playwright
    BROWSER_LIB = "patchright"
except ImportError:
    from playwright.async_api import async_playwright
    BROWSER_LIB = "playwright"


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

# 浏览器数据目录
BROWSER_DATA_DIR = Path("browser_data/newapi_extractor")


class NewAPIExtractorApp(ctk.CTk):
    """NewAPI 信息提取器主窗口"""

    def __init__(self):
        super().__init__()

        self.title("🔑 NewAPI 信息提取工具")
        self.geometry("1000x850")
        self.minsize(900, 750)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.extracted_data: list[dict] = []
        self.site_vars: dict[str, ctk.BooleanVar] = {}
        self._loop = None
        self._browser_thread = None

        self._create_ui()

    def _create_ui(self):
        """创建界面"""
        main_frame = ctk.CTkFrame(self)
        main_frame.pack(fill="both", expand=True, padx=20, pady=20)

        # 标题
        title_label = ctk.CTkLabel(
            main_frame,
            text="🔑 NewAPI 站点信息一键提取",
            font=ctk.CTkFont(size=24, weight="bold"),
        )
        title_label.pack(pady=(0, 10))

        # 说明
        desc_label = ctk.CTkLabel(
            main_frame,
            text=f"使用 {BROWSER_LIB} 打开浏览器，自动提取用户名、API User、API Key\n首次使用请先点击「打开浏览器登录」登录各站点",
            font=ctk.CTkFont(size=13),
            text_color="gray",
        )
        desc_label.pack(pady=(0, 15))

        # 站点选择
        self._create_sites_section(main_frame)

        # 操作按钮
        self._create_buttons(main_frame)

        # 结果显示
        self._create_result_section(main_frame)

        # 状态栏
        self.status_label = ctk.CTkLabel(
            main_frame,
            text="💡 首次使用请先点击「打开浏览器登录」登录各站点",
            font=ctk.CTkFont(size=12),
            text_color="gray",
        )
        self.status_label.pack(pady=(10, 0))

    def _create_sites_section(self, parent):
        """创建站点选择区域"""
        sites_frame = ctk.CTkFrame(parent)
        sites_frame.pack(fill="x", pady=(0, 15))

        header_frame = ctk.CTkFrame(sites_frame, fg_color="transparent")
        header_frame.pack(fill="x", padx=15, pady=(15, 10))

        ctk.CTkLabel(
            header_frame,
            text="选择要提取的站点",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(side="left")

        btn_frame = ctk.CTkFrame(header_frame, fg_color="transparent")
        btn_frame.pack(side="right")

        ctk.CTkButton(
            btn_frame, text="全选", width=60, height=28, command=self._select_all
        ).pack(side="left", padx=5)

        ctk.CTkButton(
            btn_frame,
            text="取消",
            width=60,
            height=28,
            fg_color="gray",
            command=self._deselect_all,
        ).pack(side="left")

        # 站点网格
        scroll_frame = ctk.CTkScrollableFrame(sites_frame, height=180)
        scroll_frame.pack(fill="x", padx=15, pady=(0, 15))

        for i, (site_id, config) in enumerate(NEWAPI_SITES.items()):
            row = i // 3
            col = i % 3

            site_frame = ctk.CTkFrame(scroll_frame)
            site_frame.grid(row=row, column=col, padx=5, pady=5, sticky="ew")
            scroll_frame.columnconfigure(col, weight=1)

            var = ctk.BooleanVar(value=True)
            self.site_vars[site_id] = var

            cb = ctk.CTkCheckBox(
                site_frame,
                text=f"{config['name']}",
                variable=var,
                font=ctk.CTkFont(size=12),
            )
            cb.pack(side="left", padx=10, pady=8)

    def _create_buttons(self, parent):
        """创建操作按钮"""
        btn_frame = ctk.CTkFrame(parent, fg_color="transparent")
        btn_frame.pack(fill="x", pady=(0, 15))

        # 第一行按钮
        row1 = ctk.CTkFrame(btn_frame, fg_color="transparent")
        row1.pack(fill="x", pady=(0, 10))

        self.login_btn = ctk.CTkButton(
            row1,
            text="🌐 打开浏览器登录",
            font=ctk.CTkFont(size=14),
            height=40,
            fg_color="#6c757d",
            hover_color="#5a6268",
            command=self._open_browser_for_login,
        )
        self.login_btn.pack(side="left", expand=True, fill="x", padx=(0, 5))

        self.extract_btn = ctk.CTkButton(
            row1,
            text="🔍 提取信息",
            font=ctk.CTkFont(size=16, weight="bold"),
            height=40,
            command=self._start_extract,
        )
        self.extract_btn.pack(side="left", expand=True, fill="x", padx=(5, 0))

        # 第二行按钮
        row2 = ctk.CTkFrame(btn_frame, fg_color="transparent")
        row2.pack(fill="x")

        self.copy_table_btn = ctk.CTkButton(
            row2,
            text="📋 复制表格",
            font=ctk.CTkFont(size=14),
            height=40,
            fg_color="#28a745",
            hover_color="#218838",
            command=self._copy_as_table,
            state="disabled",
        )
        self.copy_table_btn.pack(side="left", expand=True, fill="x", padx=(0, 5))

        self.append_md_btn = ctk.CTkButton(
            row2,
            text="� 追加到汇总",
            font=ctk.CTkFont(size=14),
            height=40,
            fg_color="#17a2b8",
            hover_color="#138496",
            command=self._append_to_summary,
            state="disabled",
        )
        self.append_md_btn.pack(side="left", expand=True, fill="x", padx=(5, 0))

    def _create_result_section(self, parent):
        """创建结果显示区域"""
        result_frame = ctk.CTkFrame(parent)
        result_frame.pack(fill="both", expand=True)

        ctk.CTkLabel(
            result_frame,
            text="� 提取结果",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w", padx=15, pady=(15, 10))

        self.result_text = ctk.CTkTextbox(
            result_frame, font=ctk.CTkFont(family="Consolas", size=11), wrap="none"
        )
        self.result_text.pack(fill="both", expand=True, padx=15, pady=(0, 15))

    def _select_all(self):
        for var in self.site_vars.values():
            var.set(True)

    def _deselect_all(self):
        for var in self.site_vars.values():
            var.set(False)

    def _open_browser_for_login(self):
        """打开浏览器让用户登录"""
        self.login_btn.configure(state="disabled", text="⏳ 正在打开浏览器...")
        self.status_label.configure(text="正在启动浏览器，请在浏览器中登录各站点...", text_color="yellow")
        self.update()

        # 在新线程中运行浏览器
        def run_browser():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._browser_login_flow())
            finally:
                loop.close()
            # 更新 UI
            self.after(0, self._on_browser_closed)

        self._browser_thread = threading.Thread(target=run_browser, daemon=True)
        self._browser_thread.start()

    async def _browser_login_flow(self):
        """浏览器登录流程"""
        BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)

        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(BROWSER_DATA_DIR),
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )

            page = context.pages[0] if context.pages else await context.new_page()

            # 打开第一个选中的站点
            selected_sites = [sid for sid, var in self.site_vars.items() if var.get()]
            if selected_sites:
                first_site = NEWAPI_SITES[selected_sites[0]]
                await page.goto(f"{first_site['url']}/login")

            # 等待用户关闭浏览器
            with contextlib.suppress(Exception):
                await context.pages[0].wait_for_event("close", timeout=600000)  # 10分钟超时

            await context.close()

    def _on_browser_closed(self):
        """浏览器关闭后的回调"""
        self.login_btn.configure(state="normal", text="🌐 打开浏览器登录")
        self.status_label.configure(text="✅ 浏览器已关闭，可以点击「提取信息」", text_color="green")

    def _start_extract(self):
        """开始提取"""
        selected_sites = [sid for sid, var in self.site_vars.items() if var.get()]
        if not selected_sites:
            self.status_label.configure(text="❌ 请至少选择一个站点", text_color="red")
            return

        self.extract_btn.configure(state="disabled", text="⏳ 提取中...")
        self.status_label.configure(text="正在提取信息...", text_color="yellow")
        self.update()

        # 在新线程中运行提取
        def run_extract():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                results = loop.run_until_complete(self._extract_all_sites(selected_sites))
            finally:
                loop.close()
            # 更新 UI
            self.after(0, lambda: self._show_results(results))

        threading.Thread(target=run_extract, daemon=True).start()

    async def _extract_all_sites(self, site_ids: list) -> list:
        """提取所有站点信息"""
        results = []

        if not BROWSER_DATA_DIR.exists():
            log("浏览器数据目录不存在，请先登录")
            return results

        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(BROWSER_DATA_DIR),
                headless=True,  # 提取时使用无头模式
                args=["--disable-blink-features=AutomationControlled"],
            )

            page = context.pages[0] if context.pages else await context.new_page()

            for site_id in site_ids:
                config = NEWAPI_SITES[site_id]
                result = await self._extract_from_site(page, site_id, config)
                if result:
                    results.append(result)

            await context.close()

        return results

    async def _extract_from_site(self, page, site_id: str, config: dict) -> dict | None:
        """从单个站点提取信息"""
        url = f"{config['url']}/console/personal"
        log(f"正在访问: {config['name']} ({url})")

        try:
            await page.goto(url, timeout=30000)
            # 等待页面加载完成
            await page.wait_for_load_state("networkidle", timeout=10000)
            await asyncio.sleep(1)

            # 从 localStorage 获取用户信息
            # 使用 JS 在浏览器环境中解析 JSON，避免跨域问题
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
                log(f"  ✅ {config['name']}: 用户名={user_info['username']}, API User={user_info['api_user']}")
                return {
                    "site_id": site_id,
                    "name": config["name"],
                    "url": config["url"],
                    "linuxdo_user": config["linuxdo_user"],
                    "username": user_info["username"],
                    "api_user": user_info["api_user"],
                    "api_key": user_info["api_key"],
                }
            else:
                log(f"  ❌ {config['name']}: 未登录或无法获取信息")
                return None

        except Exception as e:
            log(f"  ❌ {config['name']}: 访问失败 - {e}")
            return None

    def _show_results(self, results: list):
        """显示结果"""
        self.extract_btn.configure(state="normal", text="� 提取信息")

        if not results:
            self.status_label.configure(
                text="❌ 未提取到任何信息，请先点击「打开浏览器登录」登录各站点",
                text_color="red",
            )
            self.result_text.delete("1.0", "end")
            self.result_text.insert("1.0", "未提取到任何信息。\n\n请先点击「打开浏览器登录」，在浏览器中登录各站点后再提取。")
            return

        self.extracted_data = results

        # 生成 Markdown 表格
        lines = []
        for item in results:
            lines.append(f"## {item['name']}\n")
            lines.append("| 项目 | 值 |")
            lines.append("| -------- | --------------------------------------------------- |")
            lines.append(f"| 用户名 | {item['username']} |")
            lines.append(f"| API User | {item['api_user']} |")
            lines.append(f"| API Key | {item['api_key']} |")
            lines.append("")

        self.result_text.delete("1.0", "end")
        self.result_text.insert("1.0", "\n".join(lines))

        self.copy_table_btn.configure(state="normal")
        self.append_md_btn.configure(state="normal")

        self.status_label.configure(
            text=f"✅ 成功提取 {len(results)} 个站点的信息",
            text_color="green",
        )

    def _copy_as_table(self):
        """复制为表格格式"""
        content = self.result_text.get("1.0", "end").strip()
        if content:
            self.clipboard_clear()
            self.clipboard_append(content)
            self.status_label.configure(text="✅ 已复制到剪贴板！", text_color="green")

    def _append_to_summary(self):
        """追加到中转站汇总.md"""
        if not self.extracted_data:
            return

        try:
            summary_file = Path("中转站汇总.md")
            existing = summary_file.read_text(encoding="utf-8") if summary_file.exists() else ""

            new_lines = []
            for item in self.extracted_data:
                linuxdo_url = f"https://linux.do/u/{item['linuxdo_user']}/summary"
                line = f"{linuxdo_url}\t{item['name']}\t{item['url']}"
                if item['url'] not in existing:
                    new_lines.append(line)

            if new_lines:
                with open(summary_file, "a", encoding="utf-8") as f:
                    f.write("\n" + "\n".join(new_lines))
                self.status_label.configure(
                    text=f"✅ 已追加 {len(new_lines)} 条记录到 {summary_file}",
                    text_color="green",
                )
            else:
                self.status_label.configure(
                    text="ℹ️ 所有站点已存在于汇总文件中",
                    text_color="yellow",
                )

        except Exception as e:
            self.status_label.configure(text=f"❌ 写入失败: {e}", text_color="red")


def main():
    app = NewAPIExtractorApp()
    app.mainloop()


if __name__ == "__main__":
    main()
