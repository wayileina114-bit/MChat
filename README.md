# MChat —— 类 Discord 的 Matrix 端到端加密聊天客户端

MChat 是一个开箱即用的桌面聊天应用：登录**你自己的一个** Matrix 账号，即可和好友进行**端到端加密**聊天。界面参考 Discord 设计——暗色主题、房间列表侧栏、聊天气泡和消息输入框。

底层使用 [Matrix 协议](https://matrix.org/) 与 Python 的
[matrix-nio](https://github.com/matrix-nio/matrix-nio) 库，消息通过
m.megolm.v1.aes-sha2 **端到端加密（E2EE）**，服务器只能看到密文。

---

## 功能特性

- **类 Discord 暗色界面**：房间列表、聊天气泡、消息输入框
- **端到端加密**：Matrix Megolm 协议，只有房间成员能解密
- **群聊房间**：加入多个房间，和房间里的所有人聊天
- **私聊（1 对 1）**：和某个好友单独聊天
- **创建房间并邀请好友**：自己建群，通过好友的 Matrix ID 邀请对方
- **加入房间**：输入房间 ID 或邀请链接，加入别人创建的房间
- **自动接受邀请**：好友邀请你时自动加入并出现在房间列表
- **Windows 可执行程序**：打包成 .exe，双击即可运行
- **安装程序**：Inno Setup 制作的安装包，带开始菜单与桌面快捷方式
- **自动更新**：启动时检查 GitHub Releases，发现新版本可一键下载安装包

---

## 下载与安装

前往 [Releases](https://github.com/wayileina114-bit/MChat/releases) 页面，下载最新版：

| 文件 | 说明 |
|------|------|
| MChat-Setup.exe | **安装程序**（推荐）。安装后从开始菜单 / 桌面启动，可正常卸载 |
| MChat-portable.zip | 便携版。解压后运行 MChat.exe，免安装 |

---

## 使用说明

### 1. 注册一个 Matrix 账号

Matrix 是开放的通讯协议，账号可以在任意 Matrix 服务器注册。

1. 打开 https://app.element.io （Element 是官方网页客户端）
2. 点 Create account，服务器保持默认 matrix.org
3. 注册一个账号，例如 @myname:matrix.org，记住密码

> 你的好友也需要有一个 Matrix 账号（可以在同一服务器，也可以在不同服务器）。

### 2. 登录

第一次启动 MChat 会弹出登录窗口，填写：

- **Homeserver**：你的账号所在服务器，默认 https://matrix.org
- **代理**（可选）：如果网络无法直连服务器，可填代理，例如 http://127.0.0.1:7890
- **用户名 / 密码**：你的 Matrix 账号

点击「登录」后即可进入主界面。配置和加密密钥保存在 %LOCALAPPDATA%/MChat/ 目录下。

### 3. 聊天

- **房间列表**：左侧显示你加入的所有房间，分「群聊」和「私聊」两组
- **发消息**：点击一个房间，底部输入框输入消息，**Enter** 发送，**Shift+Enter** 换行
- **发起私聊**：点左下角「＋ 新建 → 发起私聊」，输入好友的 Matrix ID（如 @friend:matrix.org）
- **创建群聊**：点「＋ 新建 → 创建群聊房间」，输入房间名，创建后可邀请好友
- **邀请好友**：在房间右上角点「邀请好友」，输入好友的 Matrix ID
- **加入房间**：点左下角「加入」，输入房间 ID（!xxx:matrix.org）或邀请链接
- **接受邀请**：好友邀请你时会自动加入，刷新后出现在房间列表

---

## 从源码运行

    pip install -r requirements.txt
    python main.py

## 构建 exe 与安装程序

    python -m PyInstaller --noconfirm --clean mchat.spec
    ISCC.exe installer.iss

## 自动更新机制

应用启动时会查询 GitHub Releases 最新版本，比对最新 tag 与本地版本号。
发现新版本会弹窗提示，可一键下载安装包或前往下载页。

发布新版本只需推送一个 tag（例如 v1.0.1），GitHub Actions 会自动构建
exe 与安装程序并发布到 Releases。

---

## 目录结构

| 路径 | 作用 |
|------|------|
| main.py | 程序入口 |
| mchat/gui.py | PySide6 图形界面（Discord 风格） |
| mchat/service.py | Matrix 通信服务（登录、房间、收发、加密） |
| mchat/updater.py | 自动更新检测与下载 |
| mchat/config.py | 配置读写 |
| mchat.spec | PyInstaller 打包配置 |
| installer.iss | Inno Setup 安装脚本 |
| .github/workflows/release.yml | 自动构建 + 发布 |

## 隐私与安全

- 账号密码与访问令牌只保存在本地 %LOCALAPPDATA%/MChat/config.json，不会上传到仓库
- 加密密钥库（store/）同样只存在本地
- 消息内容端到端加密，Matrix 服务器只能看到密文
