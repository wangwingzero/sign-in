// ==UserScript==
// @name         NewAPI 签到信息提取器
// @namespace    https://github.com/your-repo/sign-in
// @version      2.0
// @description  一键提取 NewAPI 站点的 session cookie 和 api_user，用于签到配置
// @author       Sign-in Bot
// @match        *://wzw.pp.ua/*
// @match        *://elysiver.h-e.top/*
// @match        *://kfc-api.sxxe.net/*
// @match        *://free.duckcoding.com/*
// @match        *://runanytime.hxi.me/*
// @match        *://ai.zzhdsgsss.xyz/*
// @match        *://new.184772.xyz/*
// @match        *://api.mitchll.com/*
// @match        *://anyrouter.top/*
// @match        *://gyapi.zxiaoruan.cn/*
// @match        *://welfare.apikey.cc/*
// @match        *://lightllm.online/*
// @match        *://api.224442.xyz/*
// @match        *://api.hotaruapi.top/*
// @match        *://api.dev88.tech/*
// @grant        GM_setClipboard
// @grant        GM_notification
// @grant        GM_getValue
// @grant        GM_setValue
// ==/UserScript==

(function() {
    'use strict';

    // 站点到 provider 的映射
    const SITE_PROVIDERS = {
        'wzw.pp.ua': 'wong',
        'elysiver.h-e.top': 'elysiver',
        'kfc-api.sxxe.net': 'kfcapi',
        'free.duckcoding.com': 'duckcoding',
        'runanytime.hxi.me': 'runanytime',
        'ai.zzhdsgsss.xyz': 'neb',
        'new.184772.xyz': 'zeroliya',
        'api.mitchll.com': 'mitchll',
        'anyrouter.top': 'anyrouter',
        'gyapi.zxiaoruan.cn': 'zhongruan',
        'welfare.apikey.cc': 'apikey',
        'lightllm.online': 'lightllm',
        'api.224442.xyz': 'windhub',
        'api.hotaruapi.top': 'hotaru',
        'api.dev88.tech': 'dev88',
    };

    // 创建悬浮按钮
    const btn = document.createElement('div');
    btn.innerHTML = '📋 提取签到信息';
    btn.style.cssText = `
        position: fixed;
        bottom: 20px;
        right: 20px;
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        color: white;
        padding: 12px 20px;
        border-radius: 25px;
        cursor: pointer;
        font-size: 14px;
        font-weight: bold;
        box-shadow: 0 4px 15px rgba(102, 126, 234, 0.4);
        z-index: 99999;
        transition: all 0.3s ease;
        user-select: none;
    `;

    btn.onmouseover = () => {
        btn.style.transform = 'scale(1.05)';
        btn.style.boxShadow = '0 6px 20px rgba(102, 126, 234, 0.6)';
    };
    btn.onmouseout = () => {
        btn.style.transform = 'scale(1)';
        btn.style.boxShadow = '0 4px 15px rgba(102, 126, 234, 0.4)';
    };

    btn.onclick = extractInfo;
    document.body.appendChild(btn);

    // 获取 cookie 值
    function getCookie(name) {
        const value = `; ${document.cookie}`;
        const parts = value.split(`; ${name}=`);
        if (parts.length === 2) return parts.pop().split(';').shift();
        return null;
    }

    function extractInfo() {
        try {
            // 尝试获取 session cookie（可能因 HttpOnly 而失败）
            let sessionCookie = getCookie('session');

            // 从 localStorage 获取用户信息
            const userStr = localStorage.getItem('user');
            const user = userStr ? JSON.parse(userStr) : {};
            const apiUser = user.id ? String(user.id) : '';
            const username = user.username || user.display_name || '';

            if (!apiUser) {
                alert('❌ 未找到用户 ID，请确保已登录');
                return;
            }

            const hostname = window.location.hostname;
            const provider = SITE_PROVIDERS[hostname] || hostname.split('.')[0];

            // 如果无法获取 session cookie（HttpOnly），显示手动获取指南
            if (!sessionCookie) {
                showManualGuide(username, apiUser, provider);
                return;
            }

            // 生成签到配置 JSON
            const config = {
                name: username || `${provider}_${apiUser}`,
                provider: provider,
                cookies: {
                    session: sessionCookie
                },
                api_user: apiUser
            };

            const jsonStr = JSON.stringify(config, null, 2);

            // 复制到剪贴板
            if (typeof GM_setClipboard !== 'undefined') {
                GM_setClipboard(jsonStr, 'text');
            } else {
                navigator.clipboard.writeText(jsonStr);
            }

            // 显示结果
            showResult(config, jsonStr);

        } catch (e) {
            alert('❌ 提取失败: ' + e.message);
        }
    }

    function showManualGuide(username, apiUser, provider) {
        const overlay = document.createElement('div');
        overlay.style.cssText = `
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.5);
            z-index: 999999;
            display: flex;
            align-items: center;
            justify-content: center;
        `;

        const modal = document.createElement('div');
        modal.style.cssText = `
            background: white;
            border-radius: 12px;
            padding: 24px;
            max-width: 700px;
            width: 90%;
            max-height: 85vh;
            overflow: auto;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
        `;

        // 生成模板 JSON
        const template = {
            name: username || `${provider}_${apiUser}`,
            provider: provider,
            cookies: {
                session: "【请粘贴 session 值】"
            },
            api_user: apiUser
        };

        modal.innerHTML = `
            <h2 style="margin: 0 0 16px 0; color: #e74c3c;">⚠️ 需要手动获取 Session Cookie</h2>
            <p style="color: #666; margin-bottom: 16px;">
                由于安全限制（HttpOnly），脚本无法直接读取 session cookie。<br>
                请按以下步骤手动获取：
            </p>
            
            <div style="background: #f8f9fa; border-radius: 8px; padding: 16px; margin-bottom: 16px;">
                <h3 style="margin: 0 0 12px 0; color: #333;">📋 已获取的信息：</h3>
                <table style="width: 100%; border-collapse: collapse;">
                    <tr>
                        <td style="padding: 8px; border: 1px solid #ddd; font-weight: bold; width: 100px;">用户名</td>
                        <td style="padding: 8px; border: 1px solid #ddd;">${username || 'N/A'}</td>
                    </tr>
                    <tr>
                        <td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">api_user</td>
                        <td style="padding: 8px; border: 1px solid #ddd;">${apiUser}</td>
                    </tr>
                    <tr>
                        <td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">provider</td>
                        <td style="padding: 8px; border: 1px solid #ddd;">${provider}</td>
                    </tr>
                </table>
            </div>

            <div style="background: #fff3cd; border: 1px solid #ffc107; border-radius: 8px; padding: 16px; margin-bottom: 16px;">
                <h3 style="margin: 0 0 12px 0; color: #856404;">🔧 获取 Session Cookie 步骤：</h3>
                <ol style="margin: 0; padding-left: 20px; color: #856404;">
                    <li>按 <strong>F12</strong> 打开开发者工具</li>
                    <li>点击顶部的「<strong>应用程序</strong>」(Application) 标签</li>
                    <li>左侧展开「<strong>Cookie</strong>」→ 点击当前网站</li>
                    <li>找到名为 <strong>session</strong> 的行</li>
                    <li>双击「值」列，<strong>Ctrl+C</strong> 复制</li>
                </ol>
            </div>

            <div style="background: #1e1e1e; border-radius: 8px; padding: 16px; margin-bottom: 16px;">
                <p style="color: #888; margin: 0 0 8px 0; font-size: 12px;">JSON 模板（复制后替换 session 值）：</p>
                <pre id="jsonTemplate" style="margin: 0; color: #d4d4d4; font-size: 12px; white-space: pre-wrap; word-break: break-all;">${JSON.stringify(template, null, 2)}</pre>
            </div>

            <div style="display: flex; gap: 10px;">
                <button id="copyTemplate" style="
                    flex: 1;
                    background: #28a745;
                    color: white;
                    border: none;
                    padding: 12px 24px;
                    border-radius: 8px;
                    cursor: pointer;
                    font-size: 14px;
                ">📋 复制模板</button>
                <button id="closeModal" style="
                    flex: 1;
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    color: white;
                    border: none;
                    padding: 12px 24px;
                    border-radius: 8px;
                    cursor: pointer;
                    font-size: 14px;
                ">关闭</button>
            </div>
        `;

        overlay.appendChild(modal);
        document.body.appendChild(overlay);

        // 复制模板
        modal.querySelector('#copyTemplate').onclick = () => {
            const templateStr = JSON.stringify(template, null, 2);
            if (typeof GM_setClipboard !== 'undefined') {
                GM_setClipboard(templateStr, 'text');
            } else {
                navigator.clipboard.writeText(templateStr);
            }
            modal.querySelector('#copyTemplate').textContent = '✅ 已复制!';
            setTimeout(() => {
                modal.querySelector('#copyTemplate').textContent = '📋 复制模板';
            }, 2000);
        };

        // 关闭弹窗
        overlay.onclick = (e) => {
            if (e.target === overlay) overlay.remove();
        };
        modal.querySelector('#closeModal').onclick = () => overlay.remove();
    }

    function showResult(config, jsonStr) {
        // 创建弹窗
        const overlay = document.createElement('div');
        overlay.style.cssText = `
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.5);
            z-index: 999999;
            display: flex;
            align-items: center;
            justify-content: center;
        `;

        const modal = document.createElement('div');
        modal.style.cssText = `
            background: white;
            border-radius: 12px;
            padding: 24px;
            max-width: 700px;
            width: 90%;
            max-height: 80vh;
            overflow: auto;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
        `;

        const sessionPreview = config.cookies.session.length > 50 
            ? config.cookies.session.substring(0, 50) + '...' 
            : config.cookies.session;

        modal.innerHTML = `
            <h2 style="margin: 0 0 16px 0; color: #333;">✅ 签到信息已提取并复制</h2>
            <table style="width: 100%; border-collapse: collapse; margin-bottom: 16px;">
                <tr style="background: #f5f5f5;">
                    <td style="padding: 12px; border: 1px solid #ddd; font-weight: bold; width: 120px;">name</td>
                    <td style="padding: 12px; border: 1px solid #ddd;">${config.name}</td>
                </tr>
                <tr>
                    <td style="padding: 12px; border: 1px solid #ddd; font-weight: bold;">provider</td>
                    <td style="padding: 12px; border: 1px solid #ddd;">${config.provider}</td>
                </tr>
                <tr style="background: #f5f5f5;">
                    <td style="padding: 12px; border: 1px solid #ddd; font-weight: bold;">api_user</td>
                    <td style="padding: 12px; border: 1px solid #ddd;">${config.api_user}</td>
                </tr>
                <tr>
                    <td style="padding: 12px; border: 1px solid #ddd; font-weight: bold;">session</td>
                    <td style="padding: 12px; border: 1px solid #ddd; word-break: break-all; font-family: monospace; font-size: 11px;">${sessionPreview}</td>
                </tr>
            </table>
            <div style="background: #1e1e1e; border-radius: 8px; padding: 16px; margin-bottom: 16px;">
                <pre style="margin: 0; color: #d4d4d4; font-size: 12px; white-space: pre-wrap; word-break: break-all;">${jsonStr}</pre>
            </div>
            <p style="color: #666; margin: 0 0 16px 0;">📋 JSON 已复制到剪贴板，可直接添加到 GitHub Secrets 的配置数组中</p>
            <button id="closeModal" style="
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                border: none;
                padding: 12px 24px;
                border-radius: 8px;
                cursor: pointer;
                font-size: 14px;
                width: 100%;
            ">关闭</button>
        `;

        overlay.appendChild(modal);
        document.body.appendChild(overlay);

        // 关闭弹窗
        overlay.onclick = (e) => {
            if (e.target === overlay) overlay.remove();
        };
        modal.querySelector('#closeModal').onclick = () => overlay.remove();

        // 通知
        if (typeof GM_notification !== 'undefined') {
            GM_notification({
                title: 'NewAPI 签到信息提取',
                text: `${config.name} (${config.provider}) - api_user: ${config.api_user}`,
                timeout: 3000
            });
        }
    }
})();
