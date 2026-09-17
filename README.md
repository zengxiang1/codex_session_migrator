<div align="center">

# Codex Session Migrator

### 安全迁移 Codex 会话，在 Provider 与电脑之间自由切换

[![Release](https://img.shields.io/github/v/release/zengxiang1/codex_session_migrator?style=flat-square)](https://github.com/zengxiang1/codex_session_migrator/releases/latest)
[![macOS Intel Build](https://img.shields.io/github/actions/workflow/status/zengxiang1/codex_session_migrator/build-macos-intel.yml?style=flat-square&label=macOS%20Intel)](https://github.com/zengxiang1/codex_session_migrator/actions/workflows/build-macos-intel.yml)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20Intel-lightgrey?style=flat-square)](#下载与运行)

无需上传云端 · 不调用模型 API · 不覆盖目标电脑已有会话

[立即下载](#下载与运行) · [跨电脑迁移](#跨电脑迁移) · [命令行](#命令行使用) · [安全设计](#安全设计)

</div>

---

## 为什么需要它？

Codex 会使用 `model_provider` 给本地会话分组。官方订阅通常属于 `openai`，第三方 Provider 通常属于 `custom`；切换 Provider 后，旧会话可能仍在磁盘上，却不再出现在当前历史列表中。

更换电脑时，仅复制项目代码也不会带走 `~/.codex` 中的本地会话和索引。

Codex Session Migrator 解决这两个问题：

- **Provider 迁移**：安全调整会话的 Provider 分桶，让旧会话重新可见。
- **跨电脑迁移**：把会话导出为便携的 `.codexsessions` 文件，在另一台电脑合并导入。

> 它不会转换聊天正文，也不会把聊天发送到任何服务器。所有处理都在本地完成。

## 功能概览

| 能力 | 说明 |
| --- | --- |
| 桌面图形界面 | 扫描、Provider 迁移、跨电脑导入导出，无需操作数据库 |
| Provider 分桶迁移 | 支持 `openai → custom`，也支持任意 Provider ID |
| 跨电脑迁移包 | 导出完整 rollout JSONL 与兼容的 thread 元数据 |
| 安全合并 | 目标电脑已有相同 session ID 时跳过，绝不覆盖 |
| 项目路径重映射 | 导入时可把旧电脑的 `cwd` 改成新项目目录 |
| 自动备份与恢复 | 改写前备份 JSONL、SQLite 和精确迁移清单 |
| Dry Run | 正式修改前先预演，查看影响范围 |
| CLI 与 JSON 输出 | 适合脚本、批处理和自动化场景 |

## 下载与运行

### macOS Intel

从 [最新 Release](https://github.com/zengxiang1/codex_session_migrator/releases/latest) 下载：

```text
CodexSessionMigrator-macOS-Intel-x86_64-*.zip
```

解压后，在 Finder 中右键 `CodexSessionMigrator.app`，选择 **打开**。

> 当前应用采用 ad-hoc 签名，没有 Apple Developer ID，也未经过 Apple notarization。首次运行不能直接双击时，请使用右键“打开”。

### Windows

安装 Python 3.11+ 后，可直接启动桌面界面：

```powershell
python desktop_app.py
```

也可以自行生成单文件 EXE：

```powershell
python -m pip install -r requirements.txt
python -m PyInstaller --noconfirm --clean --onefile --windowed `
  --name CodexSessionMigrator desktop_app.py
```

生成文件位于 `dist\CodexSessionMigrator.exe`。

## 跨电脑迁移

```text
旧电脑 ~/.codex
       │
       │ 导出
       ▼
codex-sessions.codexsessions
       │
       │ 通过可信方式复制
       ▼
新电脑 ~/.codex ── 安全合并 ──► Codex 历史列表
```

### 第一步：在旧电脑导出

1. 完全退出 Codex。
2. 打开桌面应用，进入 **跨电脑迁移**。
3. Provider 筛选留空可导出全部会话，也可只填写 `openai` 或 `custom`。
4. 点击 **导出 `.codexsessions`**。

### 第二步：复制迁移包

使用 U 盘、局域网或可信的加密存储，把 `.codexsessions` 文件复制到新电脑。

迁移包包含完整聊天正文，可能含有源码、文件路径、终端输出、密钥或其他隐私。请勿发送给不可信的人，也不要上传到公开网盘。

### 第三步：在新电脑导入

1. 完全退出新电脑上的 Codex。
2. 选择迁移包。
3. 按需填写目标 Provider，例如 `custom`。
4. 如果项目位置发生变化，选择新的项目目录以重写 `cwd`。
5. 点击 **合并导入**，完成后重新启动 Codex。

导入策略：

- 以 rollout JSONL 为数据主体。
- 按 session ID 合并，已存在的会话直接跳过。
- 不覆盖目标电脑的整份 `state_5.sqlite`。
- 尽可能合并兼容的 thread 索引数据。
- 数据库版本不同时保留 JSONL，由 Codex read-repair 重建索引。

## Provider 迁移

Provider 迁移只会修改两个归类字段：

```text
JSONL:  session_meta.payload.model_provider
SQLite: state_5.sqlite → threads.model_provider
```

例如，将官方会话归入第三方共用的 `custom` 历史桶：

```text
openai ──► custom
```

推荐流程：

1. 完全退出 Codex。
2. 点击 **扫描**，确认当前 Provider 分布。
3. 保持“仅预演”开启，执行一次迁移。
4. 检查日志中的 JSONL 和 SQLite 影响数量。
5. 关闭“仅预演”，再执行正式迁移。

## 命令行使用

命令行版仅使用 Python 标准库，要求 Python 3.11+。

### 扫描 Provider 分布

```bash
python codex_session_migrator.py scan
```

### 预演 Provider 迁移

```bash
python codex_session_migrator.py migrate \
  --from-provider openai \
  --to-provider custom \
  --dry-run
```

### 正式迁移

```bash
python codex_session_migrator.py migrate \
  --from-provider openai \
  --to-provider custom
```

一次迁移多个旧 Provider：

```bash
python codex_session_migrator.py migrate \
  --from-provider old-provider-a \
  --from-provider old-provider-b \
  --to-provider custom
```

### 导出跨电脑迁移包

导出全部会话：

```bash
python codex_session_migrator.py export codex-sessions.codexsessions
```

只导出指定 Provider：

```bash
python codex_session_migrator.py export codex-sessions.codexsessions \
  --provider openai
```

### 在新电脑导入

```bash
python codex_session_migrator.py import codex-sessions.codexsessions \
  --provider custom \
  --cwd "/path/to/new/project"
```

### 查看与恢复备份

```bash
python codex_session_migrator.py backups
python codex_session_migrator.py restore "/path/to/backup-generation" --dry-run
python codex_session_migrator.py restore "/path/to/backup-generation"
```

### JSON 输出

查询和操作命令支持结构化 JSON 输出，便于脚本集成：

```bash
python codex_session_migrator.py scan --json
```

## 路径发现规则

默认 Codex 目录按以下顺序确定：

1. 命令行 `--codex-dir`
2. 环境变量 `CODEX_HOME`
3. `~/.codex`

SQLite 位置同时支持：

- `~/.codex/state_5.sqlite`
- `config.toml` 中的 `sqlite_home`
- 环境变量 `CODEX_SQLITE_HOME`

## 安全设计

- **修改前备份**：备份所有将改动的 JSONL 和 SQLite 数据库。
- **原子写入**：JSONL 经同目录临时文件写入后整体替换。
- **并发检测**：写入前检查大小与修改时间，发现 Codex 正在追加时中止。
- **SQLite 事务**：使用在线备份 API 和数据库事务。
- **进程互斥**：锁文件避免两个迁移操作同时执行。
- **精确恢复**：按 session/thread ID 恢复，不整体覆盖数据库。
- **可重复执行**：重复恢复不会反复修改已经恢复的记录。
- **路径防护**：拒绝迁移清单访问 Codex 目录之外的文件。
- **失败可恢复**：中途失败会保留带 `incomplete` 标记的清单。
- **永不删除会话**：工具没有删除会话文件的迁移路径。

本机 Provider 迁移的默认备份目录：

```text
~/.codex-session-migrator/backups/<时间戳_随机后缀>/
```

## 从源码构建

### macOS Intel

仓库内置原生构建脚本：

```bash
chmod +x build-macos-intel.sh
./build-macos-intel.sh
```

也可以打开仓库的 **Actions → Build macOS Intel app → Run workflow**。推送 `v*` 标签时，工作流会自动：

1. 在 `macos-15-intel` runner 上运行测试。
2. 构建并验证 `x86_64` Mach-O。
3. 执行 ad-hoc codesign。
4. 生成 ZIP 与 SHA-256。
5. 上传 Actions artifact，并发布到 GitHub Releases。

### 运行测试

```bash
python -X dev -m unittest -v test_codex_session_migrator.py
```

## 限制与常见问题

### 为什么会话能看到，却无法跨 Provider 继续？

Provider 标签只控制历史列表的分桶和可见性。会话中的 `encrypted_content` 可能只能由创建它的原后端解密；如果续聊失败，请切回原 Provider。

### 能迁移 Claude Code 或 Gemini CLI 的会话吗？

不能。当前版本只处理 Codex rollout JSONL 和 Codex SQLite 索引，不负责其他客户端格式转换。

### 导入会覆盖新电脑已有聊天吗？

不会。相同 session ID 会被跳过，工具也不会用源电脑的整份数据库覆盖目标数据库。

### 为什么 macOS 首次运行提示未知开发者？

当前 Release 采用 ad-hoc 签名，没有 Apple Developer ID 和 notarization。请确认下载来源和 SHA-256 后，在 Finder 中右键应用选择“打开”。

## 项目结构

```text
.
├── desktop_app.py                   # Tkinter 桌面界面
├── codex_session_migrator.py        # 迁移、导入导出与 CLI 核心
├── test_codex_session_migrator.py   # 跨平台回归测试
├── build-macos-intel.sh             # Intel Mac 本机构建脚本
├── requirements.txt                 # 构建依赖
└── .github/workflows/
    └── build-macos-intel.yml        # macOS Intel 构建与 Release
```

---

<div align="center">

如果这个工具帮你找回或迁移了重要会话，欢迎点一个 ⭐

</div>
