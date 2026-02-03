# Sign-in 自动签到工具

自动签到和浏览工具，支持多个 NewAPI 公益站签到和 LinuxDO 论坛浏览。

## ✨ 功能特性

- 🔄 **NewAPI 站点签到** - 支持 13+ 个公益站自动签到
- 📖 **LinuxDO 浏览** - 模拟真实用户浏览行为，支持 Cloudflare 绕过
- 📬 **多渠道通知** - 支持邮件、微信、Telegram、钉钉等 11 种通知方式
- ⏰ **定时运行** - GitHub Actions 自动运行（NewAPI）或本地定时任务（LinuxDO）

## 🚀 快速开始

### NewAPI 签到（GitHub Actions）

1. Fork 本仓库
2. 在 Settings → Secrets → Actions 中添加 `NEWAPI_ACCOUNTS`
3. 配置通知渠道（可选）
4. GitHub Actions 会自动运行（每天 8:00 和 20:00）

### LinuxDO 浏览

```bash
# 安装依赖
uv sync

# 运行浏览
uv run python main.py --platform linuxdo
```

## 📋 配置说明

### NewAPI 账号配置

```json
[
  {
    "name": "WONG公益站",
    "provider": "wong",
    "cookies": {"session": "xxx"},
    "api_user": "12345"
  }
]
```

### LinuxDO 账号配置

```json
[
  {
    "username": "用户名",
    "password": "密码",
    "browse_minutes": 20
  }
]
```

### 支持的 NewAPI 站点

| 站点 ID | 站点名称 | 域名 |
|---------|----------|------|
| `wong` | WONG公益站 | wzw.pp.ua |
| `elysiver` | Elysiver | elysiver.h-e.top |
| `kfcapi` | KFC API | kfc-api.sxxe.net |
| `duckcoding` | Free DuckCoding | free.duckcoding.com |
| `runanytime` | 随时跑路 | runanytime.hxi.me |
| `neb` | NEB公益站 | ai.zzhdsgsss.xyz |
| `techstar` | TechnologyStar | aidrouter.qzz.io |
| `lightllm` | 轻のLLM | lightllm.online |
| `hotaru` | Hotaru API | api.hotaruapi.top |
| ... | 更多站点 | 见 000/看我.md |

## 🔔 通知渠道

支持以下通知方式（配置对应环境变量即可启用）：

- 📧 邮件（QQ邮箱等）
- 💬 PushPlus（微信推送）
- 📱 Server酱 Turbo
- ✈️ Telegram
- 🔔 钉钉/飞书/企业微信
- 🍎 Bark（iOS）
- 更多...

详细配置见 [000/看我.md](000/看我.md)

## 📁 项目结构

```
sign-in/
├── main.py                 # 主入口
├── linuxdo_browse.py       # LinuxDO 浏览脚本
├── linuxdo_scheduler.py    # LinuxDO 定时任务
├── platforms/              # 各平台签到实现
├── utils/                  # 工具函数
├── scripts/                # 辅助脚本
└── .github/workflows/      # GitHub Actions 配置
```

## ⚠️ 免责声明

本项目仅供学习交流使用，请勿用于任何违反服务条款的行为。使用本项目产生的任何后果由使用者自行承担。

## 📄 License

MIT
