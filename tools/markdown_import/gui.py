"""
Windows / 桌面通用多页面 Markdown 导入向导 (Tkinter)。

功能：
1. 支持站点连接与 10 分钟一次性配对码兑换 Token；
2. 动态拉取服务端注册的全部页面类型 (BlogPage / GuidePage / ProjectPage / 未来 XxPage) 与有权限的父栏目；
3. 支持选单个 .md 或资料文件夹，一键复制当前页面类型的标准 YAML 模板；
4. 支持一键 AI 阅读全文自动补全标题/导读讲解/标签（兼容 10 万字以上超长文与自动本地回退）；
5. 展示预检结果与发布门禁提示，确认后分阶段上传并创建未发布草稿，输出后台编辑链接。
"""

from pathlib import Path
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from .client import MarkdownImportClient
from .scanner import list_root_markdown_files, scan_markdown_source
from .templates import render_front_matter_template


class MarkdownImportWizardApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("myblog 通用多页面 Markdown 导入向导")
        self.geometry("960x800")
        self.client: MarkdownImportClient | None = None
        self.catalog_types: dict[str, dict] = {}
        self.parents_list: list[dict] = []
        self.selected_parent_obj: dict | None = None
        self.scanned = None
        self._build_ui()

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=16)
        root.pack(fill="both", expand=True)

        conn = ttk.LabelFrame(root, text="1. 连接站点（配对码或导入令牌）", padding=10)
        conn.pack(fill="x", pady=(0, 10))
        ttk.Label(conn, text="站点地址:").grid(row=0, column=0, sticky="w")
        self.url_var = tk.StringVar(value="http://192.168.20.5:8000")
        ttk.Entry(conn, textvariable=self.url_var, width=32).grid(row=0, column=1, padx=6)
        self.insecure_http_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(conn, text="允许内网 HTTP", variable=self.insecure_http_var).grid(row=1, column=1, sticky="w", padx=6)
        ttk.Label(conn, text="配对码 / Token:").grid(row=0, column=2, sticky="w")
        self.auth_var = tk.StringVar()
        ttk.Entry(conn, textvariable=self.auth_var, width=36, show="*").grid(row=0, column=3, padx=6)
        ttk.Button(conn, text="连接并拉取页面类型", command=self.connect_site).grid(row=0, column=4, padx=6)

        target = ttk.LabelFrame(root, text="2. 选择页面类型与目标父位置（支持搜索、完整路径换行与规则展示）", padding=10)
        target.pack(fill="x", pady=(0, 10))

        top_row = ttk.Frame(target)
        top_row.pack(fill="x")
        ttk.Label(top_row, text="导入页面类型:").pack(side="left")
        self.type_var = tk.StringVar()
        self.type_combo = ttk.Combobox(top_row, textvariable=self.type_var, state="readonly", width=30)
        self.type_combo.pack(side="left", padx=6)
        self.type_combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh_parents())
        ttk.Button(top_row, text="复制该类型 MD 模板", command=self.copy_template).pack(side="left", padx=6)
        self.containers_only_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            top_row,
            text="仅显示栏目容器页（隐藏文章子页）",
            variable=self.containers_only_var,
            command=self.refresh_parents,
        ).pack(side="right")

        search_row = ttk.Frame(target)
        search_row.pack(fill="x", pady=(8, 4))
        ttk.Label(search_row, text="🔍 搜索父位置:").pack(side="left")
        self.parent_search_var = tk.StringVar()
        search_entry = ttk.Entry(search_row, textvariable=self.parent_search_var, width=48)
        search_entry.pack(side="left", fill="x", expand=True, padx=6)
        search_entry.bind("<Return>", lambda _e: self.refresh_parents())
        search_entry.bind("<KeyRelease>", self._on_parent_search_key)
        ttk.Button(search_row, text="搜索服务端", command=self.refresh_parents).pack(side="left")

        list_frame = ttk.Frame(target)
        list_frame.pack(fill="x")
        self.parent_listbox = tk.Listbox(list_frame, height=5, exportselection=False)
        y_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.parent_listbox.yview)
        x_scroll = ttk.Scrollbar(target, orient="horizontal", command=self.parent_listbox.xview)
        self.parent_listbox.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.parent_listbox.pack(side="left", fill="both", expand=True)
        y_scroll.pack(side="right", fill="y")
        x_scroll.pack(fill="x")
        self.parent_listbox.bind("<<ListboxSelect>>", self._on_parent_selected)

        self.parent_detail_var = tk.StringVar(value="当前未选择父栏目（连接站点后自动拉取）")
        ttk.Label(
            target,
            textvariable=self.parent_detail_var,
            foreground="#0f766e",
            wraplength=880,
            justify="left",
        ).pack(fill="x", pady=(6, 0))

        src = ttk.LabelFrame(root, text="3. 选择单个 .md 或文章+图片文件夹", padding=10)
        src.pack(fill="x", pady=(0, 10))
        self.src_var = tk.StringVar()
        src_row1 = ttk.Frame(src)
        src_row1.pack(fill="x")
        ttk.Entry(src_row1, textvariable=self.src_var, width=68).pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(src_row1, text="选单个 .md", command=self.pick_file).pack(side="left", padx=4)
        ttk.Button(src_row1, text="选文件夹", command=self.pick_folder).pack(side="left", padx=4)

        src_row2 = ttk.Frame(src)
        src_row2.pack(fill="x", pady=(6, 0))
        ttk.Label(src_row2, text="文件夹内文章切换:").pack(side="left")
        self.folder_root: Path | None = None
        self.folder_mds: list[Path] = []
        self.md_pick_var = tk.StringVar()
        self.md_pick_combo = ttk.Combobox(src_row2, textvariable=self.md_pick_var, state="readonly", width=64)
        self.md_pick_combo.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self.md_pick_combo.bind("<<ComboboxSelected>>", self._on_folder_md_selected)

        meta = ttk.LabelFrame(root, text="4. 标题 / 简介 / 标签（可留空由自动/AI 补全）", padding=10)
        meta.pack(fill="x", pady=(0, 10))
        ttk.Label(meta, text="标题:").grid(row=0, column=0, sticky="w", pady=3)
        self.title_var = tk.StringVar()
        ttk.Entry(meta, textvariable=self.title_var, width=72).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Label(meta, text="简介/摘要:").grid(row=1, column=0, sticky="w", pady=3)
        self.summary_var = tk.StringVar()
        ttk.Entry(meta, textvariable=self.summary_var, width=72).grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Label(meta, text="标签(逗号分隔):").grid(row=2, column=0, sticky="w", pady=3)
        self.tags_var = tk.StringVar()
        ttk.Entry(meta, textvariable=self.tags_var, width=72).grid(row=2, column=1, sticky="ew", padx=6)
        meta.columnconfigure(1, weight=1)

        self.allow_new_tags_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            meta,
            text="允许自动/手动创建站内尚无的新标签（推荐，避免新标签被忽略）",
            variable=self.allow_new_tags_var,
        ).grid(row=3, column=1, sticky="w", padx=6, pady=(4, 0))

        actions = ttk.Frame(meta)
        actions.grid(row=4, column=1, sticky="w", pady=(8, 0))
        ttk.Button(actions, text="✨ AI 自动补全标题/导读/标签", command=self.run_ai_suggest).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="1) 预检", command=self.run_preflight).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="2) 导入未发布草稿", command=self.run_import).pack(side="left")

        log_box = ttk.LabelFrame(root, text="5. 预检报告与导入结果", padding=10)
        log_box.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_box, height=14, wrap="word")
        self.log_text.pack(fill="both", expand=True)

    def _log(self, msg: str) -> None:
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")

    def _selected_type_key(self) -> str:
        label = self.type_var.get()
        for key, item in self.catalog_types.items():
            if f"{item['label']} ({key})" == label:
                return key
        return ""

    def _selected_parent_id(self) -> int:
        if self.selected_parent_obj:
            return int(self.selected_parent_obj["id"])
        return 0

    def _on_parent_search_key(self, _event=None) -> None:
        kw = self.parent_search_var.get().strip().casefold()
        self._render_parent_list(kw)

    def _render_parent_list(self, local_filter: str = "") -> None:
        self.parent_listbox.delete(0, "end")
        self._displayed_parents = []
        for p in self.parents_list:
            kind_badge = "栏目" if p.get("is_container", True) else "文章"
            ctype_lbl = p.get("content_type_label") or p.get("content_type") or ""
            bpath = p.get("breadcrumb_path") or p.get("title") or ""
            line = f"[{kind_badge}·{ctype_lbl}] {bpath}  (ID={p['id']}, {p['url_path']})"
            if local_filter and local_filter not in line.casefold():
                continue
            self._displayed_parents.append(p)
            self.parent_listbox.insert("end", line)
        if self._displayed_parents:
            self.parent_listbox.selection_clear(0, "end")
            self.parent_listbox.selection_set(0)
            self._set_selected_parent(self._displayed_parents[0])
        else:
            self.selected_parent_obj = None
            self.parent_detail_var.set("当前筛选条件下没有可导入的父位置（若要挂在普通文章下，请取消勾选“仅显示栏目容器页”并点“搜索服务端”）。")

    def _on_parent_selected(self, _event=None) -> None:
        sel = self.parent_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if 0 <= idx < len(getattr(self, "_displayed_parents", [])):
            self._set_selected_parent(self._displayed_parents[idx])

    def _set_selected_parent(self, p: dict) -> None:
        self.selected_parent_obj = p
        allowed = "、".join(item["label"] for item in (p.get("allowed_child_types") or [])) or "当前所选类型"
        self.parent_detail_var.set(
            f"已选目标：{p.get('breadcrumb_path') or p['title']}\n"
            f"节点类型：{p.get('content_type_label', '')} (ID={p['id']}，路径={p['url_path']})  |  该位置允许导入：{allowed}"
        )

    def connect_site(self) -> None:
        url = self.url_var.get().strip()
        secret = self.auth_var.get().strip()
        try:
            client = MarkdownImportClient(url, allow_insecure_http=self.insecure_http_var.get())
            if secret.upper().startswith("PAIR-"):
                res = client.exchange_pair_code(secret)
                self.auth_var.set(res["token"])
                self._log(f"配对成功，已换取短期导入令牌 ({res['prefix']}...)。")
            else:
                client.token = secret
            catalog = client.fetch_catalog()
            self.client = client
            self.catalog_types = {t["type_key"]: t for t in catalog.get("types", [])}
            values = [f"{t['label']} ({t['type_key']})" for t in self.catalog_types.values()]
            self.type_combo["values"] = values
            if values:
                self.type_combo.current(0)
                self.refresh_parents()
            ai_state = "开启" if catalog.get("ai_enabled") else "本地兜底模式"
            self._log(f"连接成功！共发现 {len(values)} 种可用页面类型（AI 服务端状态: {ai_state}）。")
        except Exception as exc:
            messagebox.showerror("连接失败", str(exc))

    def refresh_parents(self) -> None:
        if not self.client:
            return
        tkey = self._selected_type_key()
        if not tkey:
            return
        try:
            cat = self.client.fetch_catalog(
                type_key=tkey,
                q=self.parent_search_var.get().strip(),
                containers_only=self.containers_only_var.get(),
                limit=200,
            )
            self.parents_list = cat.get("parents", [])
            self._render_parent_list()
        except Exception as exc:
            self._log(f"拉取父栏目失败: {exc}")

    def copy_template(self) -> None:
        tkey = self._selected_type_key() or "blog_page"
        tpl = self.catalog_types.get(tkey, {}).get("template_markdown") or render_front_matter_template(tkey)
        self.clipboard_clear()
        self.clipboard_append(tpl)
        self._log(f"已复制 [{tkey}] 标准 YAML Front Matter 模板到剪贴板。")

    def pick_file(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("Markdown", "*.md *.markdown")])
        if path:
            md_path = Path(path)
            self.folder_root = md_path.parent
            self.folder_mds = [md_path]
            self.md_pick_combo["values"] = [md_path.name]
            self.md_pick_var.set(md_path.name)
            self.src_var.set(path)
            self._scan(md_path, md_path.parent)

    def pick_folder(self) -> None:
        folder = filedialog.askdirectory()
        if not folder:
            return
        root, mds = list_root_markdown_files(folder)
        if not mds:
            messagebox.showwarning("无可用文件", "该文件夹顶层未找到 .md 或 .markdown 文件。")
            return
        self.folder_root = root
        self.folder_mds = mds
        names = [p.name for p in mds]
        self.md_pick_combo["values"] = names
        self.md_pick_combo.current(0)
        chosen = mds[0]
        if len(mds) > 1:
            self._log(f"检测到 {len(mds)} 篇顶层 Markdown，可在下拉框自由切换；当前选中 {chosen.name}。")
        self.src_var.set(str(chosen))
        self._scan(chosen, root)

    def _on_folder_md_selected(self, _event=None) -> None:
        if not self.folder_root or not self.folder_mds:
            return
        idx = self.md_pick_combo.current()
        if 0 <= idx < len(self.folder_mds):
            chosen = self.folder_mds[idx]
            self.src_var.set(str(chosen))
            self._scan(chosen, self.folder_root)

    def _scan(self, md_path: Path, root_dir: Path) -> None:
        try:
            self.scanned = scan_markdown_source(md_path, root_dir=root_dir)
            self._log(
                f"扫描完成: {self.scanned.source_name} | 本地资源 {len(self.scanned.assets)} 个"
                f" | 缺失 {len(self.scanned.missing_assets)} | 拦截不安全路径 {len(self.scanned.blocked_paths)}"
            )
        except Exception as exc:
            messagebox.showerror("扫描失败", str(exc))

    def _collect_edits(self, tkey: str) -> dict:
        edits: dict = {"allow_new_tags": bool(self.allow_new_tags_var.get())}
        if self.title_var.get().strip():
            edits["title"] = self.title_var.get().strip()
        if self.summary_var.get().strip():
            edits["intro" if tkey == "blog_page" else "summary"] = self.summary_var.get().strip()
        if self.tags_var.get().strip():
            edits["tags"] = self.tags_var.get().strip()
        return edits

    def run_ai_suggest(self) -> None:
        if not self.client or not self.scanned:
            messagebox.showwarning("提示", "请先连接站点并选择 Markdown 文件。")
            return
        tkey = self._selected_type_key()
        pid = self._selected_parent_id()
        try:
            res = self.client.ai_suggest(
                type_key=tkey,
                parent_id=pid,
                source_name=self.scanned.source_name,
                markdown_text=self.scanned.raw_text,
                existing_title=self.title_var.get().strip(),
                allow_external_processing=True,
                allow_new_tags=bool(self.allow_new_tags_var.get()),
            )
            s = res.get("suggestions") or {}
            if s.get("title") and not self.title_var.get().strip():
                self.title_var.set(s["title"])
            if s.get("summary"):
                self.summary_var.set(s["summary"])
            if s.get("tags"):
                self.tags_var.set(", ".join(s["tags"]))
            else:
                self._log("提示：未自动匹配到标签，可在上方标签框手动补充后导入。")
            self._log(f"AI 建议生成完成（来源: {res.get('source')}）。")
        except Exception as exc:
            messagebox.showerror("生成失败", str(exc))

    def run_preflight(self) -> None:
        if not self.client or not self.scanned:
            messagebox.showwarning("提示", "请先连接站点并选择 Markdown 文件。")
            return
        tkey = self._selected_type_key()
        pid = self._selected_parent_id()
        try:
            pre = self.client.preflight(
                type_key=tkey,
                parent_id=pid,
                scanned=self.scanned,
                edits=self._collect_edits(tkey),
            )
            nm = pre["normalized_metadata"]
            if not self.title_var.get().strip():
                self.title_var.set(nm["title"])
            self._log(f"预检完成: 标题=《{nm['title']}》(来源: {nm['title_source']}), 日期={nm['authored_on']}")
            for err in pre.get("errors", []):
                self._log(f"[错误] {err['message']}")
            for blk in pre.get("publish_blockers", []):
                self._log(f"[发布前需补齐] {blk['message']}")
        except Exception as exc:
            messagebox.showerror("预检失败", str(exc))

    def run_import(self) -> None:
        if not self.client or not self.scanned:
            messagebox.showwarning("提示", "请先连接站点并选择 Markdown 文件。")
            return
        tkey = self._selected_type_key()
        pid = self._selected_parent_id()
        edits = self._collect_edits(tkey)

        def _worker():
            try:
                res = self.client.import_source(
                    type_key=tkey,
                    parent_id=pid,
                    scanned=self.scanned,
                    edits=edits,
                    progress_cb=lambda st, i, n: self.after(0, self._log, f"上传媒体 ({i}/{n}): {st}"),
                )
                self.after(0, self._on_import_success, res)
            except Exception as exc:
                self.after(0, messagebox.showerror, "导入失败", str(exc))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_import_success(self, res: dict) -> None:
        url = res.get("edit_full_url") or res.get("edit_url")
        self._log(f"草稿已创建！Page ID={res['page_id']}, Revision={res['revision_id']}")
        self._log(f"后台编辑地址: {url}")
        if messagebox.askyesno("导入成功", f"草稿已创建（Page ID={res['page_id']}）。是否在浏览器打开编辑？"):
            webbrowser.open(url)


def main() -> None:
    app = MarkdownImportWizardApp()
    app.mainloop()


if __name__ == "__main__":
    main()
