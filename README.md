# MChat —— 类 Discord 的 Matrix 端到端加密桌面应用

MChat 是一个开箱即用的桌面应用，让两个你自己的 Matrix 账号在**端到端加密**的房间里互相通信。界面参考 Discord 设计：暗色主题、账号侧栏、聊天区和消息输入框。

底层使用 [Matrix 协议](https://matrix.org/) 与 Python 的
[matrix-nio](https://github.com/matrix-nio/matrix-nio) 库，消息通过
m.megolm.v1.aes-sha2 **端到端加密（E2EE）**，服务器（matrix.org）看到的只是密文。

---

## 功能特性

- **类 Discord 暗色界面**：账号切换侧栏、频道列表、聊天气泡、消息输入框
- **端到端加密**：Matrix Megolm 协议，只有房间里的两个设备能解密
- **双账号同屏**：程序A / 程序B 各自登录，点击侧栏头像即可切换发送身份
- **Windows 可执行程序**：打包成 .exe，双击即可运行
- **安装程序**：Inno Setup 制作的安装包，带开始菜单与桌面快捷方式
- **自动更新**：启动时检查 GitHub Releases，发现新版本会提示下载

---

## 下载与安装

前往 [Releases](https://github.com/wayileina114-bit/MChat/releases) 页面，下载最新版：

| 文件 | 说明 |
|------|------|
| MChat-Setup.exe | **安装程序**（推荐）。安装后从开始菜单 / 桌面启动，可正常卸载 |
| MChat-portable.zip | 便携版。解压后运行 MChat.exe，免安装 |

---

## 使用说明

### 1. 准备两个 Matrix 账号

需要两个不同的账号（程序A 和 程序B 各一个）。

1. 打开 https://app.element.io （Element 是官方网页客户端）
2. 点 Create account，服务器保持默认 matrix.org
3. 注册两个账号，例如 @mychat-a:matrix.org 和 @mychat-b:matrix.org，记住密码

### 2. 首次配置

第一次启动 MChat 会弹出配置窗口，填写：

- **Homeserver**：默认 https://matrix.org
- **代理**（可选）：如果网络无法直连 matrix.org，可填代理，例如 http://127.0.0.1:7890
- **程序A / 程序B** 的用户名和密码

点击「连接」后，应用会：

1. 登录两个账号
2. 首次运行时自动创建端到端加密房间并完成握手
3. 进入聊天界面

配置和加密密钥保存在 %LOCALAPPDATA%/MChat/ 目录下。

### 3. 聊天

- 点击左侧栏的 A / B 头像，切换当前发送身份
- 底部输入框输入消息，**Enter** 发送，**Shift+Enter** 换行
- 双方消息实时显示

---

## 从源码运行

    pip install -r requirements.txt
    python main.py

## 构建 exe 与安装程序

    python -m PyInstaller --noconfirm --clean mchat.spec
    ISCC.exe installer.iss

## 自动更新机制

应用启动时会查询 GitHub Releases 最新版本，比对最新 tag 与本地版本号。
发现新版本会弹窗提示，并引导前往下载页。

发布新版本只需推送一个 tag（例如 v1.0.1），GitHub Actions 会自动构建
exe 与安装程序并发布到 Releases。

---

## 目录结构

| 路径 | 作用 |
|------|------|
| main.py | 程序入口 |
| mchat/gui.py | PySide6 图形界面（Discord 风格） |
| mchat/service.py | Matrix 通信服务（登录、收发、历史、加密） |
| mchat/updater.py | 自动更新检测 |
| mchat/config.py | 配置读写 |
| mchat.spec | PyInstaller 打包配置 |
| installer.iss | Inno Setup 安装脚本 |
| .github/workflows/release.yml | 自动构建 + 发布 |

## 隐私与安全

- 账号密码与访问令牌只保存在本地 %LOCALAPPDATA%/MChat/config.json，不会上传到仓库
- 加密密钥库（store_a/、store_b/）同样只存在本地
- 消息内容端到端加密，matrix.org 服务器只能看到密文
