# NewAPI 签到配置提取器 - Chrome 扩展

一键提取 NewAPI 站点的 session cookie 和 api_user，生成签到配置 JSON。

## 安装方法

1. 打开 Chrome/Edge 浏览器，进入扩展管理页面：
   - Chrome: `chrome://extensions/`
   - Edge: `edge://extensions/`

2. 开启右上角的「开发者模式」

3. 点击「加载已解压的扩展程序」

4. 选择 `scripts/chrome_extension` 文件夹

5. 扩展安装完成！

## 使用方法

1. **先登录各 NewAPI 站点**（在浏览器中正常登录）

2. **打开任意一个已登录的站点页面**（保持标签页打开）

3. 点击浏览器工具栏的扩展图标 🔑

4. 点击「📦 提取签到配置」

5. 等待提取完成，点击「📋 复制 JSON 到剪贴板」

6. 粘贴到 GitHub Secrets → `NEWAPI_ACCOUNTS`

## 失败站点联动（GitHub Action）

- Action 会把签到失败站点写入 `failed_sites.json`
- 本地 `git pull` 后，扩展可直接：
  - 一键打开失败站点页面
  - 复制失败账号模板（占位 `session`）
  - 生成可直接粘贴到 `NEWAPI_ACCOUNTS` 的 JSON（会合并本地已提取配置，AnyRouter 失败项会补占位）

## 支持的站点

- WONG公益站 (wzw.pp.ua)
- Elysiver (elysiver.h-e.top)
- KFC API (kfc-api.sxxe.net)
- Free DuckCoding (free.duckcoding.com)
- 随时跑路 (runanytime.hxi.me)
- NEB公益站 (ai.zzhdsgsss.xyz)
- Mitchll-api (api.mitchll.com)
- AnyRouter (anyrouter.top)

## 注意事项

- ⚠️ 提取 api_user 需要**打开对应站点的标签页**
- 如果显示 ⚠️ 表示找到了 session 但没获取到 api_user
- 如果显示 ❌ 表示未登录该站点

## 图标

扩展需要图标文件，可以用任意 PNG 图片：
- `icon16.png` - 16x16 像素
- `icon48.png` - 48x48 像素  
- `icon128.png` - 128x128 像素

如果没有图标，可以用在线工具生成：https://favicon.io/
