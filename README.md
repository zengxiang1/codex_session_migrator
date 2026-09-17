# Codex Session Migrator

一个 Codex 会话 Provider 与跨电脑迁移工具。提供 Windows 桌面版和仅依赖 Python 标准库的命令行版。

## Windows 桌面版

双击 `CodexSessionMigrator.exe` 即可运行，无需安装 Python。

桌面版包括：

- 扫描本机 Codex 会话 Provider 分布
- 本机 `openai` / `custom` 等 Provider 标签迁移
- 从第一台电脑导出 `.codexsessions` 迁移包
- 在第二台电脑安全合并导入
- 导入时可统一修改 Provider，并可把旧 `cwd` 改成新电脑的项目目录

跨电脑操作顺序：

1. 两台电脑都退出 Codex。
2. 在旧电脑选择“跨电脑迁移”，导出 `.codexsessions`。
3. 通过可信方式把迁移包复制到新电脑。
4. 在新电脑选择迁移包；可填写目标 Provider 和新的项目目录。
5. 点击“合并导入”，完成后启动 Codex。

迁移包包含完整聊天正文，可能包含源码、密钥、文件路径或其他隐私，请勿上传到不可信网盘。

## macOS Intel 桌面版

仓库内置 `.github/workflows/build-macos-intel.yml`，使用 GitHub Actions 的
`macos-15-intel` runner 构建原生 `x86_64` 应用。

在仓库的 **Actions → Build macOS Intel app → Run workflow** 中可手动构建；
推送 `v*` 标签也会自动构建。完成后从该次运行底部下载：

```text
CodexSessionMigrator-macOS-Intel-x86_64-v0.2.2
```

也可以在 Intel Mac 本机运行：

```bash
chmod +x build-macos-intel.sh
./build-macos-intel.sh
```

构建结果采用 ad-hoc 签名，未使用 Apple Developer ID、未经过 notarization。
首次打开时请在 Finder 中右键应用并选择“打开”。

它不会转换聊天正文或调用任何模型 API，只修改 Codex 用于历史列表分桶的：

- JSONL 会话头：`session_meta.payload.model_provider`
- SQLite 索引：`state_5.sqlite` 的 `threads.model_provider`

适合将官方 `openai` 会话归入第三方共用的 `custom` 历史列表，或者在其他 Provider ID 之间重新归类。

## 要求

- Python 3.11+
- 操作前退出所有 Codex 进程
- 建议先执行 `scan` 和 `migrate --dry-run`

默认 Codex 目录按以下顺序确定：

1. 命令行 `--codex-dir`
2. 环境变量 `CODEX_HOME`
3. `~/.codex`

工具同时识别 `config.toml` 的 `sqlite_home` 和环境变量 `CODEX_SQLITE_HOME`。

## 快速使用

在本目录打开终端。

### 1. 扫描现有 Provider 分布

```bash
python codex_session_migrator.py scan
```

指定自定义 Codex 目录：

```bash
python codex_session_migrator.py scan --codex-dir "D:\\my-codex-home"
```

### 2. 先预演迁移

```bash
python codex_session_migrator.py migrate \
  --from-provider openai \
  --to-provider custom \
  --dry-run
```

Windows PowerShell 可以写成一行：

```powershell
python .\codex_session_migrator.py migrate --from-provider openai --to-provider custom --dry-run
```

### 3. 正式迁移

```bash
python codex_session_migrator.py migrate \
  --from-provider openai \
  --to-provider custom
```

交互确认时必须输入 `MIGRATE`。自动化环境可使用 `--yes`。

一次迁移多个旧 Provider ID：

```bash
python codex_session_migrator.py migrate \
  --from-provider old-provider-a \
  --from-provider old-provider-b \
  --to-provider custom
```

### 4. 查看备份

```bash
python codex_session_migrator.py backups
```

默认备份目录：

```text
~/.codex-session-migrator/backups/<时间戳_随机后缀>/
```

每代备份包含修改前的 JSONL、SQLite 快照和 `manifest.json` 精确迁移清单。

### 5. 恢复某次迁移

先预演：

```bash
python codex_session_migrator.py restore "/path/to/backup-generation" --dry-run
```

正式恢复：

```bash
python codex_session_migrator.py restore "/path/to/backup-generation"
```

恢复不是简单覆盖整个旧数据库。工具根据清单中的 session/thread ID，只把本次迁移且目前仍处于目标 Provider 的记录改回来源 Provider，避免误伤迁移后新建的会话。

## 跨电脑迁移的命令行方式

旧电脑导出全部会话：

```bash
python codex_session_migrator.py export codex-sessions.codexsessions
```

只导出 `openai` 会话：

```bash
python codex_session_migrator.py export codex-sessions.codexsessions --provider openai
```

新电脑合并导入并改到 `custom` Provider：

```bash
python codex_session_migrator.py import codex-sessions.codexsessions \
  --provider custom \
  --cwd "/path/to/new/project"
```

导入不会覆盖目标电脑的整份 `state_5.sqlite`：它以 rollout JSONL 为主，按 session ID 合并，已存在的 ID 直接跳过；同时尽量合并兼容的 thread 元数据。如果两台电脑 Codex 数据库版本不同，文件仍会导入，Codex 可从 JSONL read-repair 重建缺失索引。

## JSON 输出

`scan`、`migrate`、`restore`、`backups` 都支持 `--json`，便于脚本集成：

```bash
python codex_session_migrator.py scan --json
```

## 安全设计

- 正式迁移前自动备份所有将改动的 JSONL 与 SQLite 数据库。
- JSONL 使用同目录临时文件和原子替换。
- 写入前检查文件大小和修改时间，发现 Codex 正在追加时中止。
- SQLite 使用在线备份 API，并在事务中更新。
- 工具级互斥锁避免两个迁移进程同时操作。
- 恢复按清单精确匹配 ID，并且可以重复执行。
- 如果迁移中途失败，已完成部分仍会写入带 `incomplete` 标记的清单，可用同一个 `restore` 命令恢复。
- 恢复时校验清单中的会话路径，拒绝访问 Codex 目录之外的文件。
- 不删除任何会话文件。

## 限制

统一 Provider 标签只影响历史列表可见性，不保证跨后端续聊一定成功。会话里的 `encrypted_content` 可能只能由创建它的原 Provider 解密；遇到这种情况，需要切回原 Provider 续聊。

本工具也不负责把 Claude Code、Gemini CLI 等其他客户端的会话格式转换成 Codex 格式。

## 运行测试

```bash
python -m unittest -v test_codex_session_migrator.py
```
