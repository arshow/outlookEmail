# Outlook 多邮箱管理助手

基于 Web 的多账号邮件管理工具：统一接入 Outlook/Hotmail（OAuth + Microsoft Graph）、标准 IMAP，以及临时邮箱渠道；提供 Web 管理界面、可选浏览器扩展与外部 API，适合集中管理、查看、转发与自动化拉取邮件。

当前版本见仓库根目录 [`VERSION`](VERSION)。

---

## 原项目说明

本仓库基于以下开源项目二次开发 / 维护，**核心能力与大量基础实现来自原作者**：

| 项目 | 地址 |
|------|------|
| **原项目** | [assast/outlookEmail](https://github.com/assast/outlookEmail) |
| **本仓库** | [arshow/outlookEmail](https://github.com/arshow/outlookEmail) |

感谢原作者与社区贡献。使用、对比上游功能或获取官方 Docker 镜像时，请以原项目仓库与发布说明为准；本仓库侧重在本地源码上的增强与自用维护。

若你只需要上游稳定发行版，建议直接使用原项目：

```bash
git clone https://github.com/assast/outlookEmail.git
```

本仓库（含本文档中的二次开发改动）：

```bash
git clone https://github.com/arshow/outlookEmail.git
cd outlookEmail
```

---

## 功能概览

### 邮箱与账号

- Outlook / Hotmail OAuth（Graph / IMAP）、Gmail、QQ、163、126、Yahoo 等标准 IMAP，以及自定义 IMAP
- 分组管理（多层级）、标签、批量导入 / 导出、按账号配置代理（HTTP / SOCKS5）
- 临时邮箱：GPTMail、DuckMail、Cloudflare Temp Email（支持多渠道配置）
- 别名与外部邮箱映射：便于统一查看与 API 拉取

### 邮件与界面

- Web 端查看收件箱等文件夹；支持回复 / 全部回复 / 转发（Graph `Mail.Send` 或 IMAP+SMTP）
- 附件下载、全屏阅读、邮件删除（按协议能力）
- **聚合收件箱**：跨账号汇总查看（可配合本地保留加速）
- **单账号文件夹树**：按 Graph / IMAP 真实目录浏览（聚合视图仍使用页签）
- **收件箱发现（SSE）**：后台轮询发现新邮件并推送到前端，可按账号开关与并发配置
- **普通邮件本地保留**：列表 / 详情可走本地库，降低重复远程请求（详见 [本地邮件保留](docs/local-mail-retention.md)）
- 系统设置、皮肤、登录密码保护；可选 Chrome / Edge 扩展

### 自动化与运维

- Token 定时 / 手动全量刷新，刷新历史与失败统计
- 按账号邮件转发（SMTP / Telegram）、时间窗与附件策略
- WebDAV 全量备份（Cron 或手动）
- API Key 对外接口（邮件列表、验证码类场景等，见 [API 文档](docs/api.md)）

### 安全相关

- 登录保护、CSRF、XSS 防护、敏感字段加密、操作日志等（见 [安全说明](docs/security.md)）

> 刷新 Token 可能导致既有授权失效，需要时请重新完成 OAuth 授权。

---

## 本仓库相对原项目的增强（摘要）

以下能力主要在本仓库迭代中完善，上游若已合并则以实际代码为准：

1. **收件箱发现任务**：与转发 / 定时刷新解耦；SSE 推送；账号级开关；有限并发轮询
2. **聚合收件箱**：跨账号列表；本地源 `source=local` 等加载策略
3. **单账号邮箱目录树**：`GET /api/emails/<email>/folders` + 前端树形导航
4. **本地保留与点击加载策略**：有缓存用缓存；保留开启时优先本地；远程同步由用户主动刷新触发（减少无谓拉取）

更细的接口与行为说明见 `docs/`。

---

## 快速开始

默认 Web 地址：`http://127.0.0.1:5000`  
默认登录密码：`admin123`（首次登录后请立即修改）

### 方式一：Python 本地运行（推荐用于本仓库源码）

要求：Python 3.10+（建议 3.11 / 3.12 / 3.13）

```bash
git clone https://github.com/arshow/outlookEmail.git
cd outlookEmail
python -m venv .venv

# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# macOS / Linux
# source .venv/bin/activate

pip install -r requirements.txt
```

Windows 可直接：

```powershell
.\start.ps1
```

或：

```bash
# 首次运行会用到 SECRET_KEY；可用 start.ps1 / start.bat 自动生成 secret_key.txt
python web_outlook_app.py
```

数据默认落在 `./data`（SQLite）。环境变量示例见 [`.env.example`](.env.example)。

### 方式二：Docker（上游官方镜像）

官方预构建镜像由**原项目**发布：

```bash
docker pull ghcr.io/assast/outlookemail:latest

docker run -d \
  --name outlook-mail-reader \
  -p 5000:5000 \
  -v "$(pwd)/data:/app/data" \
  -e LOGIN_PASSWORD=admin123 \
  -e SECRET_KEY=please-change-me \
  ghcr.io/assast/outlookemail:latest
```

或使用仓库内 `docker-compose.yml`（镜像同样指向上游 GHCR）。

若要运行**本仓库当前源码**，请使用 `docker-compose.build.yml` 本地构建，或按 [部署文档](docs/deployment.md) 操作。

### 方式三：Windows / macOS 桌面包

原项目在 GitHub Releases 提供 `OutlookEmail` 的 Windows zip / macOS dmg。桌面包行为以原项目发布说明为准；本仓库以源码与自建镜像为主。

### 方式四：Git Push 自动部署

本仓库可按服务器现有项目的模式部署：服务器使用裸仓库接收 `main`，`post-receive` 自动检出到 `/opt/outlook-email` 并重启 systemd 服务。数据库、皮肤和密钥位于 Git 工作树外，因此后续 `git push` 不会覆盖生产数据。

首次在服务器初始化：

```bash
scp -P <SSH_PORT> scripts/setup_git_deploy_debian.sh <DEPLOY_USER>@<SERVER_HOST>:/tmp/
ssh <DEPLOY_USER>@<SERVER_HOST> -p <SSH_PORT> "bash /tmp/setup_git_deploy_debian.sh"
```

初始化会创建：

```text
/opt/git/outlook-email.git                 # 裸仓库，接收 git push
/opt/outlook-email                         # 应用工作树
/etc/outlook-email/outlook-email.env       # 生产环境变量与 SECRET_KEY
/var/lib/outlook-email/data/               # SQLite 数据库与皮肤文件
/etc/systemd/system/outlook-email.service
```

然后在本地添加远程并部署：

```bash
git remote add deploy ssh://<DEPLOY_USER>@<SERVER_HOST>:<SSH_PORT>/opt/git/outlook-email.git
git push deploy main
```

服务默认监听 `0.0.0.0:5001`，并以单 worker、16 线程运行 Gunicorn，确保内存内的任务状态与 SSE 订阅保持一致。该配置为 5–10 名同时在线用户预留了长连接与普通页面请求的并发余量；后续更新只需：

```bash
git push deploy main
```

服务器排查命令：

```bash
systemctl status outlook-email
journalctl -u outlook-email -f
```

生产登录密码的初始值是 `admin123`；请在首次登录后立刻修改。若需要修改端口、密钥或数据库位置，编辑 `/etc/outlook-email/outlook-email.env` 后执行 `systemctl restart outlook-email`。

---

## 使用说明

### 1. 获取 OAuth2 凭证（Azure）

这一步非必须：若购买的账号已自带令牌可跳过；项目也内置了默认 Client ID，跳过自定义应用注册时可直接从下面的**步骤 5**开始。

要使用本工具，您需要获取以下 OAuth2 凭证：

1. **Client ID** — Microsoft Azure 应用注册的客户端 ID
2. **Refresh Token** — OAuth2 刷新令牌

界面中的 OAuth2 助手会读取服务启动时的 `OAUTH_CLIENT_ID` 和 `OAUTH_REDIRECT_URI`。若在 Docker / Docker Compose 里配置了自己的值，授权链接和换取 Token 都会使用这些值；未配置则使用项目内置默认值。账号导入时，Client ID 要和同一次授权换出的 Refresh Token 配套使用。

#### 步骤 1：注册 Azure 应用

（按目前情况，通常需要 E3 / E5 或其他具备创建权限的开发者账号。）

访问 [Azure Portal](https://portal.azure.com/)，进入「应用注册」：

![应用注册](img/应用注册.png)

#### 步骤 2：创建新应用

点击「新注册」，填写应用信息：

![注册应用程序](img/注册应用程序.png)

- **名称**：自定义应用名称
- **支持的账户类型**：选择「任何组织目录中的账户和个人 Microsoft 账户」
- **重定向 URI**：选择「公共客户端/本机」，填写 `http://localhost:8080`

#### 步骤 3：获取应用程序 ID

创建完成后，复制「应用程序(客户端) ID」：

![获取应用程序ID](img/获取应用程序ID.png)

#### 步骤 4：配置 API 权限

这一步多数情况下可省略；内置 Client ID 未单独配置此项也能正常使用。

手动 OAuth 助手默认走 GraphAPI 单资源权限，避免 Microsoft OAuth v2 在同一次授权中混用 Graph 和 Outlook 资源时报 `AADSTS70011`：

- `offline_access` — 获取刷新令牌
- `Mail.Read` / `Mail.ReadWrite` / `Mail.Send` / `User.Read` — Graph 读信、标已读、发信与基本用户信息（旧账号需重新授权后才能发信）

如需 IMAP 访问，请在「Outlook邮箱授权」面板选择 `IMAP授权`（自动授权默认是 GraphAPI），不要和 Graph 权限放在同一次手动授权链接里。

#### 步骤 5：获取 Refresh Token

使用本工具内置的 OAuth2 助手获取 Refresh Token：

![换取token](img/换取token.png)

1. 在 Web 界面点击「获取 Token」按钮
2. 点击「生成授权链接」
3. 复制链接到浏览器打开，完成授权
4. 复制授权后的完整 URL（出于安全考虑，没有统一建设外部授权回调服务，授权都在你自己部署的服务内完成，不会外泄。重定向 URI 为 `http://localhost:8080`，浏览器里该地址本身往往打不开，需要把完整回调 URL 复制回本服务，继续换取 Refresh Token）
5. 粘贴到「授权后的 URL」输入框
6. 点击「换取 Token」按钮
7. 复制获得的 Refresh Token

### 2. 其他常用操作

1. **导入账号**：在 Web 中导入 Outlook/Hotmail OAuth 或标准 IMAP 账号。
2. **查看邮件**：左侧选账号或「聚合收件箱」；单账号可展开文件夹树切换目录。
3. **本地保留 / 发现**：在系统设置中开启普通邮件本地保留与收件箱发现相关选项，按需配置轮询间隔与并发。
4. **外部 API**：在设置中配置 API Key，对接方式见 [docs/api.md](docs/api.md)。

---

## 文档

| 文档 | 说明 |
|------|------|
| [docs/deployment.md](docs/deployment.md) | 部署 |
| [docs/api.md](docs/api.md) | 外部 / 内部 API |
| [docs/local-mail-retention.md](docs/local-mail-retention.md) | 本地邮件保留与发现 |
| [docs/security.md](docs/security.md) | 安全 |
| [docs/troubleshooting.md](docs/troubleshooting.md) | 排错（含重置登录密码） |
| [docs/upgrade.md](docs/upgrade.md) | 升级 |
| [docs/skins.md](docs/skins.md) | 皮肤 |
| [RELEASE.md](RELEASE.md) | 版本与发布流程 |
| [CHANGELOG.md](CHANGELOG.md) | 变更记录 |
| [browser-extension/README.md](browser-extension/README.md) | 浏览器扩展 |

---

## 技术栈（简要）

- 后端：Python、Flask、APScheduler、SQLite
- 前端：原生 JS / CSS（分段静态资源）
- 邮件：Microsoft Graph、IMAP/SMTP、可选临时邮箱 HTTP API
- 部署：Docker / Compose、可选 PyInstaller 桌面包（上游发布）

---

## 贡献与反馈

- 上游问题与 PR：请优先提交到 [assast/outlookEmail](https://github.com/assast/outlookEmail)
- 本仓库相关改动：请在 [arshow/outlookEmail](https://github.com/arshow/outlookEmail) 提 Issue / PR

原项目社区讨论可参考 [LINUX DO 社区](https://linux.do/)。

---

## 致谢

- 原项目作者与贡献者：[assast/outlookEmail](https://github.com/assast/outlookEmail)
- [Microsoft Graph API](https://docs.microsoft.com/graph/)
- [Flask](https://flask.palletsprojects.com/)
- [GPTMail](https://mail.chatgpt.org.uk)
- [Resin](https://github.com/Resinat/Resin)（别名粘贴源等场景）

原项目 Star History：

[![Star History Chart](https://api.star-history.com/svg?repos=assast/outlookEmail&type=Date)](https://star-history.com/#assast/outlookEmail&Date)

---

## 免责声明

本项目仅供学习、研究与个人技术用途。请遵守各邮件平台服务条款与当地法律法规，勿用于违法或侵权用途。使用本软件产生的一切风险与后果由使用者自行承担。
