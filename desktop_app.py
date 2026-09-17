from __future__ import annotations

import json
import threading
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

import codex_session_migrator as core


class CodexMigratorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"Codex 会话迁移工具 v{core.TOOL_VERSION}")
        self.root.geometry("920x700")
        self.root.minsize(760, 580)
        self.busy = False

        self.codex_dir = tk.StringVar(value=str(core.default_codex_dir()))
        self.source_provider = tk.StringVar(value="openai")
        self.target_provider = tk.StringVar(value="custom")
        self.dry_run = tk.BooleanVar(value=True)
        self.export_filter = tk.StringVar(value="")
        self.import_package = tk.StringVar(value="")
        self.import_provider = tk.StringVar(value="")
        self.import_cwd = tk.StringVar(value="")
        self.status = tk.StringVar(value="就绪")

        self._build()

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        path_frame = ttk.LabelFrame(outer, text="Codex 数据目录", padding=8)
        path_frame.pack(fill="x")
        ttk.Entry(path_frame, textvariable=self.codex_dir).pack(side="left", fill="x", expand=True)
        ttk.Button(path_frame, text="选择…", command=self.choose_codex_dir).pack(side="left", padx=(8, 0))
        ttk.Button(path_frame, text="扫描", command=self.scan).pack(side="left", padx=(8, 0))

        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True, pady=(10, 8))
        notebook.add(self._provider_tab(notebook), text="本机 Provider 迁移")
        notebook.add(self._transfer_tab(notebook), text="跨电脑迁移")

        log_frame = ttk.LabelFrame(outer, text="执行日志", padding=6)
        log_frame.pack(fill="both", expand=True)
        self.log_box = scrolledtext.ScrolledText(log_frame, height=13, wrap="word", state="disabled")
        self.log_box.pack(fill="both", expand=True)

        status_bar = ttk.Frame(outer)
        status_bar.pack(fill="x", pady=(6, 0))
        ttk.Label(status_bar, textvariable=self.status).pack(side="left")
        ttk.Label(status_bar, text="操作前请完全退出 Codex", foreground="#a33").pack(side="right")

    def _provider_tab(self, parent: ttk.Notebook) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=14)
        ttk.Label(
            frame,
            text="只修改 JSONL 与 SQLite 中的 model_provider 标签，不修改聊天正文。",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 14))
        ttk.Label(frame, text="源 Provider（多个用逗号分隔）").grid(row=1, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.source_provider, width=42).grid(
            row=1, column=1, sticky="ew", padx=8
        )
        ttk.Label(frame, text="例如 openai").grid(row=1, column=2, sticky="w")
        ttk.Label(frame, text="目标 Provider").grid(row=2, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(frame, textvariable=self.target_provider).grid(
            row=2, column=1, sticky="ew", padx=8, pady=(10, 0)
        )
        ttk.Label(frame, text="通常为 custom").grid(row=2, column=2, sticky="w", pady=(10, 0))
        ttk.Checkbutton(frame, text="仅预演，不写入文件", variable=self.dry_run).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(16, 8)
        )
        ttk.Button(frame, text="执行 Provider 迁移", command=self.run_provider_migration).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=8
        )
        ttk.Label(
            frame,
            text="正式迁移会自动备份；可以使用命令行版按 manifest.json 精确恢复。",
            foreground="#555",
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 0))
        frame.columnconfigure(1, weight=1)
        return frame

    def _transfer_tab(self, parent: ttk.Notebook) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=14)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="第一台电脑：导出迁移包", font=("TkDefaultFont", 10, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w"
        )
        ttk.Label(frame, text="Provider 筛选（留空导出全部）").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(frame, textvariable=self.export_filter).grid(
            row=1, column=1, sticky="ew", padx=8, pady=(10, 0)
        )
        ttk.Button(frame, text="导出 .codexsessions…", command=self.export_package).grid(
            row=1, column=2, sticky="e", pady=(10, 0)
        )

        ttk.Separator(frame).grid(row=2, column=0, columnspan=3, sticky="ew", pady=18)
        ttk.Label(frame, text="第二台电脑：导入迁移包", font=("TkDefaultFont", 10, "bold")).grid(
            row=3, column=0, columnspan=3, sticky="w"
        )
        ttk.Label(frame, text="迁移包").grid(row=4, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(frame, textvariable=self.import_package).grid(
            row=4, column=1, sticky="ew", padx=8, pady=(10, 0)
        )
        ttk.Button(frame, text="选择…", command=self.choose_import_package).grid(
            row=4, column=2, sticky="e", pady=(10, 0)
        )
        ttk.Label(frame, text="导入为 Provider（可选）").grid(row=5, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(frame, textvariable=self.import_provider).grid(
            row=5, column=1, sticky="ew", padx=8, pady=(10, 0)
        )
        ttk.Label(frame, text="留空保留原值").grid(row=5, column=2, sticky="w", pady=(10, 0))
        ttk.Label(frame, text="新的项目目录 cwd（可选）").grid(row=6, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(frame, textvariable=self.import_cwd).grid(
            row=6, column=1, sticky="ew", padx=8, pady=(10, 0)
        )
        ttk.Button(frame, text="选择…", command=self.choose_import_cwd).grid(
            row=6, column=2, sticky="e", pady=(10, 0)
        )
        ttk.Button(frame, text="合并导入", command=self.import_package_action).grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(16, 8)
        )
        ttk.Label(
            frame,
            text="迁移包包含完整聊天内容，可能含源码、密钥或隐私；请通过可信渠道传输。",
            foreground="#a33",
            wraplength=760,
        ).grid(row=8, column=0, columnspan=3, sticky="w", pady=(12, 0))
        ttk.Label(
            frame,
            text="导入采用合并模式：同一 session ID 已存在时跳过，绝不覆盖目标电脑已有会话或整份数据库。",
            foreground="#555",
            wraplength=760,
        ).grid(row=9, column=0, columnspan=3, sticky="w", pady=(6, 0))
        return frame

    def choose_codex_dir(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.codex_dir.get() or str(Path.home()))
        if selected:
            self.codex_dir.set(selected)

    def choose_import_package(self) -> None:
        selected = filedialog.askopenfilename(
            filetypes=[("Codex 会话迁移包", "*.codexsessions"), ("ZIP 文件", "*.zip"), ("所有文件", "*.*")]
        )
        if selected:
            self.import_package.set(selected)

    def choose_import_cwd(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.import_cwd.get() or str(Path.home()))
        if selected:
            self.import_cwd.set(selected)

    def append_log(self, text: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text.rstrip() + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def run_async(self, label: str, function) -> None:
        if self.busy:
            messagebox.showinfo("正在执行", "请等待当前操作完成。")
            return
        self.busy = True
        self.status.set(label)
        self.append_log(f"\n[{label}]")

        def worker() -> None:
            try:
                result = function()
                self.root.after(0, lambda: self.finish_task(label, result, None))
            except Exception as exc:
                details = traceback.format_exc()
                self.root.after(
                    0,
                    lambda exc=exc, details=details: self.finish_task(
                        label, None, (exc, details)
                    ),
                )

        threading.Thread(target=worker, daemon=True).start()

    def finish_task(self, label: str, result, error) -> None:
        self.busy = False
        if error:
            exc, details = error
            self.status.set(f"{label}失败")
            self.append_log(details)
            messagebox.showerror("操作失败", str(exc))
            return
        self.status.set(f"{label}完成")
        self.append_log(json.dumps(result, ensure_ascii=False, indent=2))

    def validated_codex_dir(self) -> Path:
        return core.ensure_safe_codex_dir(Path(self.codex_dir.get().strip()))

    def scan(self) -> None:
        self.run_async("扫描", lambda: core.scan(self.validated_codex_dir()))

    def run_provider_migration(self) -> None:
        sources = {item.strip() for item in self.source_provider.get().split(",") if item.strip()}
        target = self.target_provider.get().strip()
        if not sources or not target:
            messagebox.showwarning("参数不完整", "请填写源 Provider 和目标 Provider。")
            return
        dry_run = self.dry_run.get()
        if not dry_run and not messagebox.askyesno(
            "确认正式迁移",
            "请确认已经完全退出 Codex。工具将修改本地会话标签并自动备份。是否继续？",
        ):
            return
        self.run_async(
            "Provider 迁移预演" if dry_run else "Provider 正式迁移",
            lambda: core.migrate(
                self.validated_codex_dir(), core.default_backup_dir(), sources, target, dry_run
            ),
        )

    def export_package(self) -> None:
        output = filedialog.asksaveasfilename(
            defaultextension=".codexsessions",
            filetypes=[("Codex 会话迁移包", "*.codexsessions")],
            initialfile="codex-sessions.codexsessions",
        )
        if not output:
            return
        filters = {item.strip() for item in self.export_filter.get().split(",") if item.strip()}
        self.run_async(
            "导出跨电脑迁移包",
            lambda: core.export_transfer_package(
                self.validated_codex_dir(), Path(output), filters or None
            ),
        )

    def import_package_action(self) -> None:
        package = self.import_package.get().strip()
        if not package:
            messagebox.showwarning("未选择迁移包", "请先选择 .codexsessions 文件。")
            return
        if not messagebox.askyesno(
            "确认导入",
            "请确认已经完全退出 Codex。导入会合并会话文件并尝试写入索引，不会覆盖同 ID 会话。是否继续？",
        ):
            return
        provider = self.import_provider.get().strip() or None
        cwd = self.import_cwd.get().strip() or None
        self.run_async(
            "导入跨电脑迁移包",
            lambda: core.import_transfer_package(
                self.validated_codex_dir(), Path(package), provider, cwd
            ),
        )


def main() -> None:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    CodexMigratorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
