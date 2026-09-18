# -*- coding: utf-8 -*-
"""学习通自动化的监控界面。

    python run.py

启动带调试端口的 Edge（登录态存在 .profile/edge，只需登录一次），
弹出监视窗口：左上角实时显示画面，下面是模型配置和点击/滚动测试。
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict

import customtkinter as ctk

import brain
import pageCapture as capture
import window_control as wc

ROOT = Path(__file__).resolve().parent
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PROFILE = ROOT / "edge"          # 专用用户目录（登录态存在这里，只需登录一次）
ENTRY = "https://v8.chaoxing.com/"
REFRESH_MS = 150         # 界面贴图间隔（毫秒）
MAX_STEPS = 50         # 一个回合最多跑多少步，防止模型停不下来
MAX_WAIT = 600.0        # 单次 wait 的上限（秒）。看视频要等很久，这个得放得够大；
                        # 提示词里告诉模型的数字要和这里一致，否则它按 600 规划、程序只等 30
WAIT_SLICE = 0.2         # wait 分片检查停止信号的间隔

# 浏览器窗口的页面区域尺寸。网站会按这个尺寸自动排版，
# 改这里就等于改"模型看到的页面布局"。
VIEW_W = 1080
VIEW_H = 720
RIGHT_MIN_W = 420        # 右栏（对话区）的最小宽度，免得被左侧画面挤没

# 对话区不同来源的样式（前缀、文字色、是否加粗）
_STYLE = {
    "you": ("你", "#1f2937", True),
    "ai": ("AI", "#2f4f7f", False),
    "act": ("▸", "#1e7a45", False),
    "ok": ("✓", "#2f8f5b", False),
    "err": ("✗", "#b3261e", False),
    "done": ("■", "#b36a1e", True),
    "ask": ("?", "#b36a1e", False),
    "note": ("·", "#6b7280", False),
}


def launch_edge() -> int:
    """启动专用 Edge（带调试端口），返回主进程 pid。已在跑就复用。

    为什么必须用专用目录 + 调试端口：
    - 调试端口：Chromium 忽略程序伪造的鼠标消息，只能走调试协议才能后台点击
    - 专用目录：普通 Edge 已在跑时，再带参数启动只会把标签页丢给旧实例然后退出，
      端口根本不会开；用独立目录才能保证起一个我们说了算的实例
    """
    url = f"http://127.0.0.1:{wc.DEFAULT_PORT}/json/version"
    proc = None
    try:
        with urllib.request.urlopen(url, timeout=2):
            print("Edge 已在运行，复用现有实例")
    except (urllib.error.URLError, OSError):
        print(f"启动 Edge（调试端口 {wc.DEFAULT_PORT}，用户目录 {PROFILE}）…")
        PROFILE.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen([
            EDGE,
            f"--remote-debugging-port={wc.DEFAULT_PORT}",
            f"--user-data-dir={PROFILE}",
            "--no-first-run",
            "--no-default-browser-check",
            ENTRY,
        ])
        for _ in range(30):
            time.sleep(1)
            try:
                with urllib.request.urlopen(url, timeout=2):
                    break
            except (urllib.error.URLError, OSError):
                continue
        else:
            raise RuntimeError("Edge 启动超时")

    # Popen 给的 pid 直接用，但要确认它真持有窗口——Chromium 有时会让子进程接手
    if proc is not None and wc.find_hwnd(proc.pid):
        return proc.pid

    pid = find_edge_pid()
    if pid is None:
        raise RuntimeError("拿不到 Edge 的 pid")
    return pid


def find_edge_pid() -> int | None:
    """按进程名查 Edge 的 pid。多个进程时取有可见主窗口的那个。"""
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq msedge.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, encoding="gbk", errors="replace",
    )
    pids = []
    for line in (out.stdout or "").splitlines():
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) >= 2 and parts[0].lower() == "msedge.exe" and parts[1].isdigit():
            pids.append(int(parts[1]))
    if not pids:
        return None
    for pid in pids:
        if wc.find_hwnd(pid):
            return pid
    return pids[0]


class Viewer(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")

        self.title("AutoXXT")
        self.configure(fg_color="#C8E4F1")

        # 开成屏幕的九成大小。窗口太小的话左栏放不下画面，图会缩得很小。
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        win_w, win_h = int(sw * 0.92), int(sh * 0.9)
        self.geometry(f"{win_w}x{win_h}+{int(sw * 0.04)}+{int(sh * 0.04)}")

        # 左右两栏：左边画面+控制，右边 LLM 聊天框（比左栏宽）
        self.grid_columnconfigure(0, weight=2)
        self.grid_columnconfigure(1, weight=3, minsize=RIGHT_MIN_W)
        self.grid_rowconfigure(0, weight=1)

        # ---- 左栏 ----
        self.left = ctk.CTkFrame(self, corner_radius=16, fg_color="#C8E4F1")
        self.left.grid(row=0, column=0, sticky="nsew", padx=(10, 5), pady=10)
        self.left.grid_columnconfigure(0, weight=1)

        # 画面
        ctk.CTkLabel(self.left, text="学习通画面（坐标照刻度填）", anchor="w",
                     font=("Microsoft YaHei", 13, "bold")).grid(
            row=0, column=0, sticky="w", padx=4, pady=(2, 6))

        self.screen = ctk.CTkLabel(self.left, text="正在抓取画面…",
                                   font=("Microsoft YaHei", 13))
        self.screen.grid(row=1, column=0, sticky="nw", padx=4)

        self._size = None       # 画面显示尺寸（逻辑像素），每帧按左栏宽度算
        self._img = None        # 当前的 CTkImage
        self._winsize = None    # 首帧定过窗口尺寸后就置真，不再重复设
        self._err = 0
        self._closing = False   # 关窗时置真，让 after 循环停下

        # 模型回合的状态。只有一个 worker 线程，天然串行，不用担心 brain 的历史竞争。
        self._running = False
        self._stop_evt = threading.Event()
        self._inbox: "queue.Queue[str]" = queue.Queue()
        self._step = 0
        # 「插话打断等待」用：发消息时置上，让 _wait 立刻收工去处理你说的话。
        # 和 _stop_evt（结束整个回合）分开——插话只是想跳过剩余等待，不是要停。
        self._interrupt = threading.Event()
        self._waiting = False       # 是否正卡在 wait 里（决定插话时提示哪句话）

        # ---- 左栏下方：控制面板 ----
        self.panel = ctk.CTkFrame(self.left, corner_radius=16, fg_color="#83a2eb",
                                  border_width=1, border_color="#00040d")
        self.panel.grid(row=2, column=0, sticky="ew", padx=4, pady=(4, 2))
        self.panel.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(self.panel, text="模型配置", anchor="w",
                     font=("Microsoft YaHei", 13, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=(14, 8), pady=(12, 2))

        self.base_url = ctk.StringVar()
        self.api_key = ctk.StringVar()
        self.model = ctk.StringVar()
        self._field(1, "接口地址", self.base_url)
        self._field(2, "密钥", self.api_key, show="•")
        self._field(3, "模型", self.model)

        # 保存 / 保存状态提示。配置存在 brain.config.json，下次启动自动读出来。
        # 注意行号：这是第 4 行，别和上面 _field 的行号撞了（撞了会重叠）。
        cfg_row = ctk.CTkFrame(self.panel, fg_color="transparent")
        cfg_row.grid(row=4, column=0, columnspan=2, sticky="ew", padx=14, pady=(2, 4))
        ctk.CTkButton(cfg_row, text="保存配置", width=76, height=26,
                      command=self.on_save_config).pack(side="left")
        self.cfg_hint = ctk.CTkLabel(cfg_row, text="", anchor="w",
                                     font=("Microsoft YaHei", 10), text_color="#3b4a63")
        self.cfg_hint.pack(side="left", padx=(8, 0))
        self._load_config()     # 有存过的就填进输入框

        # 最大轮数：一个回合里模型最多能做多少次决策，防止它停不下来。
        # 放在这一行右侧，不新增行（新增行要改后面所有行号，容易撞车）。
        self.max_steps = ctk.StringVar(value=str(MAX_STEPS))
        ctk.CTkEntry(cfg_row, textvariable=self.max_steps, width=52, height=26,
                     font=("Microsoft YaHei", 12)).pack(side="right")
        ctk.CTkLabel(cfg_row, text="最大轮数", anchor="e",
                     font=("Microsoft YaHei", 12)).pack(side="right", padx=(0, 6))

        ctk.CTkLabel(self.panel, text="点击 / 滚动测试", anchor="w",
                     font=("Microsoft YaHei", 13, "bold")).grid(
            row=5, column=0, columnspan=2, sticky="w", padx=(14, 8), pady=(16, 2))

        self.click_x = ctk.StringVar(value="400")
        self.click_y = ctk.StringVar(value="300")
        row = ctk.CTkFrame(self.panel, fg_color="transparent")
        row.grid(row=6, column=0, columnspan=2, sticky="ew", padx=14, pady=(2, 4))
        for var in (self.click_x, self.click_y):
            ctk.CTkEntry(row, textvariable=var, width=64, height=28).pack(
                side="left", padx=(0, 6))
        ctk.CTkButton(row, text="点击", width=56, height=28,
                      command=self.on_click).pack(side="left", padx=(4, 0))

        row2 = ctk.CTkFrame(self.panel, fg_color="transparent")
        row2.grid(row=7, column=0, columnspan=2, sticky="ew", padx=14, pady=(0, 4))
        ctk.CTkButton(row2, text="↑ 上滚", height=28,
                      command=lambda: self.on_scroll(-3)).pack(
            side="left", fill="x", expand=True, padx=(0, 4))
        ctk.CTkButton(row2, text="↓ 下滚", height=28,
                      command=lambda: self.on_scroll(3)).pack(
            side="left", fill="x", expand=True, padx=(4, 0))

        self.status = ctk.CTkLabel(self.panel, text="", anchor="w",
                                   font=("Microsoft YaHei", 11))
        self.status.grid(row=8, column=0, columnspan=2, sticky="w", padx=14, pady=(2, 4))

        # 开关：显示/隐藏浏览器窗口。
        # 默认开——窗口本来就在屏幕上；关掉会挪到屏幕外，但那样浏览器会停止渲染，
        # 画面就冻在最后一帧了，所以只在不需要画面时才关。
        self.edge_on = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(self.panel, text="显示浏览器窗口", variable=self.edge_on,
                      command=self.on_toggle).grid(
            row=9, column=0, columnspan=2, sticky="w", padx=14, pady=(4, 12))

        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # ---- 右栏：对话区 ----
        # 得有控件占住这一列，grid 才会按 weight 分宽度；空列不参与布局，左栏会吃掉整个窗口（踩过）。
        self.right = ctk.CTkFrame(self, corner_radius=16, fg_color="#eef1f6",
                                  border_width=1, border_color="#c8ccd4")
        self.right.grid(row=0, column=1, sticky="nsew", padx=(5, 10), pady=10)
        self.right.grid_columnconfigure(0, weight=1)
        self.right.grid_rowconfigure(1, weight=1)

        head = ctk.CTkFrame(self.right, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        ctk.CTkLabel(head, text="对话", font=("Microsoft YaHei", 13, "bold"),
                     text_color="#1f2937").pack(side="left")
        ctk.CTkButton(head, text="清空", width=48, height=24,
                      command=self.on_clear).pack(side="right")

        self.chat = ctk.CTkScrollableFrame(self.right, fg_color="#f7f8fa", corner_radius=8)
        self.chat.grid(row=1, column=0, sticky="nsew", padx=12, pady=4)
        self.chat.grid_columnconfigure(0, weight=1)

        send_row = ctk.CTkFrame(self.right, fg_color="transparent")
        send_row.grid(row=2, column=0, sticky="ew", padx=12, pady=(4, 12))
        self.msg = ctk.StringVar()
        self.entry = ctk.CTkEntry(send_row, textvariable=self.msg, height=32,
                                  placeholder_text="说点什么，回车发送")
        self.entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.entry.bind("<Return>", lambda _e: self.on_send())
        self.send_btn = ctk.CTkButton(send_row, text="发送", width=56, height=32,
                                      command=self.on_send)
        self.send_btn.pack(side="left")
        self.stop_btn = ctk.CTkButton(send_row, text="停止", width=56, height=32,
                                      fg_color="#b3261e", hover_color="#8f1f17",
                                      command=self.on_stop)
        self.stop_btn.pack(side="left", padx=(6, 0))
        self._set_buttons(busy=False)

        self.after(100, self.refresh)

    # ---------- 对话区 ----------
    def _say(self, kind: str, text: str) -> None:
        """往对话区追加一条。必须在主线程调用。"""
        prefix, color, bold = _STYLE.get(kind, _STYLE["note"])
        font = ("Microsoft YaHei", 12, "bold") if bold else ("Microsoft YaHei", 12)
        wrap = max(200, self.chat.winfo_width() - 24)
        lbl = ctk.CTkLabel(self.chat, text=f"{prefix}  {text}", anchor="w", justify="left",
                           wraplength=wrap, font=font, text_color=color)
        # 行号用当前子控件数（不能用 len-1：清空后是 -1，grid 行为会很怪）
        lbl.grid(row=len(self.chat.winfo_children()), column=0, sticky="w", padx=6, pady=2)
        self.after(50, self._scroll_bottom)

    def _scroll_bottom(self) -> None:
        if self._closing:
            return
        try:
            self.chat._parent_canvas.yview_moveto(1.0)
        except Exception:
            pass

    def _ui(self, kind: str, text: str) -> None:
        """子线程安全地追加一条对话。"""
        self.after(0, lambda: self._say(kind, text))

    def _set_buttons(self, busy: bool) -> None:
        if busy:
            self.send_btn.configure(text="运行中", state="disabled")
            self.stop_btn.configure(text="停止", state="normal")
        else:
            self.send_btn.configure(text="发送", state="normal")
            self.stop_btn.configure(text="停止", state="disabled")

    def _field(self, row: int, name: str, var: ctk.StringVar, show: str = "") -> None:
        """模型配置行：标签 + 整行输入框。"""
        ctk.CTkLabel(self.panel, text=name, anchor="w",
                     font=("Microsoft YaHei", 12)).grid(
            row=row, column=0, sticky="w", padx=(14, 8), pady=4)
        ctk.CTkEntry(self.panel, textvariable=var, show=show, height=28,
                     placeholder_text=f"填写{name}").grid(
            row=row, column=1, sticky="ew", padx=(0, 14), pady=4)

    def on_toggle(self):
        """显示/隐藏 Edge 窗口。挪窗口有 sleep，放子线程免得卡界面。

        注意这里先把要显示的文字算好、再交给 after——**不能把 except 里的 exc
        直接写进 lambda**。Python 3 在 except 块结束时会删掉 exc 这个名字，
        而 after 的回调是稍后才跑的，那时 exc 已经没了，回调会抛 NameError，
        把真正的失败原因整个吞掉（实测踩过：界面上啥也看不到，
        控制台刷一屏 "cannot access free variable 'exc'"）。
        """
        show = bool(self.edge_on.get())

        def work():
            try:
                if show:
                    wc.move_in()
                else:
                    wc.move_out()
                msg, color = ("已显示浏览器窗口" if show else "已隐藏浏览器窗口"), "#e8f4e8"
            except Exception as exc:
                msg, color = f"失败：{exc}", "#ffd0d0"
            self.after(0, lambda: self.status.configure(text=msg, text_color=color))
        threading.Thread(target=work, daemon=True).start()

    def _coords(self):
        """读输入框里的坐标，填得不对返回 None。"""
        try:
            return int(float(self.click_x.get())), int(float(self.click_y.get()))
        except ValueError:
            self.status.configure(text="坐标要填数字", text_color="#ffd0d0")
            return None

    def on_click(self):
        xy = self._coords()
        if xy:
            self._run(lambda: wc.click(*xy), f"已点击 {xy}")

    def on_scroll(self, notches: int):
        # 在输入框指定的位置滚——有些页面分区域滚（侧边栏和内容区各滚各的）
        xy = self._coords()
        if xy:
            self._run(lambda: wc.scroll(notches, *xy), f"已滚动 {notches:+d} 格 @ {xy}")

    def _run(self, fn, done_msg: str) -> None:
        """点击/滚动里有 sleep，放子线程跑，完成后回主线程更新状态。"""
        def work():
            try:
                fn()
                text, color = done_msg, "#e8f4e8"
            except Exception as exc:
                text, color = f"失败：{exc}", "#ffd0d0"
            self.after(0, lambda: self.status.configure(text=text, text_color=color))
        threading.Thread(target=work, daemon=True).start()

    # ================= 模型回合 =================
    def on_send(self) -> None:
        """发一句话。运行中就排队带到下一步，不然开一个新回合。

        **正在等待时发消息会立刻打断那次等待**——不然模型让等 10 分钟，
        你这句"任务点已经完成了"要干等完才轮到，白耗那么久。
        打断只是跳过剩余等待，回合继续跑（想彻底停请点停止）。
        """
        text = self.msg.get().strip()
        if not text:
            return
        self.msg.set("")
        self._say("you", text)

        if self._running:
            self._inbox.put(text)
            waiting = self._waiting
            self._interrupt.set()        # 让 _wait 马上收工（没在等就无副作用）
            self._say("note", "已打断当前等待，马上带上你说的" if waiting
                              else "已记下，下一步带上")
            return

        if not self._ensure_brain():
            return

        self._running = True
        self._stop_evt.clear()
        self._interrupt.clear()      # 清掉上一回合可能残留的打断信号
        self._step = 0
        self._set_buttons(busy=True)
        threading.Thread(target=self._agent, args=(text,), daemon=True).start()

    def on_stop(self) -> None:
        """停止当前回合。正在进行的网络请求没法打断，但它返回后不再执行动作。"""
        if not self._running:
            return
        self._stop_evt.set()
        self._say("note", "正在停止…")
        self.stop_btn.configure(text="停止中", state="disabled")

    def on_clear(self) -> None:
        """清空对话历史。"""
        if self._running:
            self._say("note", "运行中清不了，先停止")
            return
        brain.reset()
        for w in list(self.chat.winfo_children()):
            w.destroy()
        self._say("note", "已清空")

    def _ensure_brain(self) -> bool:
        """校验配置并初始化 brain。失败时把原因说清楚。"""
        base_url = self.base_url.get().strip()
        api_key = self.api_key.get().strip()
        model = self.model.get().strip()
        if not (base_url and api_key and model):
            self._say("err", "先在左边填好 接口地址 / 密钥 / 模型")
            return False
        try:
            brain.init(base_url=base_url, api_key=api_key, model=model)
        except Exception as exc:
            self._say("err", f"初始化失败：{exc}")
            return False
        return True

    def _turn_limit(self) -> int:
        """读界面上设的「最大轮数」。填得不对就用默认值，不让它把回合搞崩。

        每轮都重新读，所以运行中把它调小能立刻让回合停下来。
        """
        try:
            return max(1, int(float(self.max_steps.get())))
        except (ValueError, AttributeError):
            return MAX_STEPS

    def _agent(self, goal: str) -> None:
        """模型回合（子线程）：循环 截图→决策→执行，直到模型说停、到上限、或被停止。

        这里绝不碰 tkinter，所有界面更新都走 self._ui（内部用 after(0, ...)。
        """
        try:
            while not self._stop_evt.is_set():
                limit = self._turn_limit()
                if self._step >= limit:
                    self._ui("done", f"到 {limit} 轮上限，先停下"
                                     f"（可以在左边改「最大轮数」再继续）")
                    break

                img = capture.get_frame()
                extra = self._drain_inbox()
                page = wc.page_info()

                d = brain.decide(img, goal, extra=extra, page=page)
                self._ui("ai", d.thought)
                self._ui("act", _action_text(d.action))

                t = d.action.get("type")
                if t == "finish":
                    self._ui("done", f"完成：{d.action.get('reason', '')}")
                    break
                if t == "ask_human":
                    self._ui("ask", d.action.get("question", "需要你处理"))
                    break
                if self._stop_evt.is_set():
                    self._ui("done", "已停止（这一步没执行）")
                    break

                result = self._exec(d.action)
                self._ui("ok", result)
                # 关键：把执行结果告诉模型，否则它看不见效果，会一直重复同一个动作
                brain.note_result(result)
                self._step += 1
        except Exception as exc:
            self._ui("err", f"出错：{type(exc).__name__}: {exc}")
        finally:
            self._running = False
            self.after(0, lambda: self._set_buttons(busy=False))

    def _drain_inbox(self) -> str:
        """取走运行期间插入的对话，拼成一行给模型。"""
        parts = []
        while True:
            try:
                parts.append(self._inbox.get_nowait())
            except Exception:
                break
        return "；".join(parts)

    def _exec(self, action: Dict[str, Any]) -> str:
        """执行一个动作，返回给用户看的结果文字。

        结果文字会同时喂回模型（brain.note_result），所以要说清楚"页面变没变"——
        这是模型判断自己的动作有没有生效的唯一依据。早期版本只说"已点击"，
        模型看不见效果就一直重复同一个坐标（实测死循环到步数上限）。
        """
        t = action.get("type")
        try:
            if t in ("click", "scroll", "drag", "type", "press"):
                before = wc.page_state()
                if t == "click":
                    wc.click(int(action["x"]), int(action["y"]), settle=0.6)
                    desc = f"已点击 ({int(action['x'])}, {int(action['y'])})"
                elif t == "scroll":
                    n = int(action.get("notches", 3))
                    wc.scroll(n)
                    desc = f"已滚动 {n:+d} 格"
                elif t == "drag":
                    wc.drag(int(action["x1"]), int(action["y1"]),
                            int(action["x2"]), int(action["y2"]))
                    desc = "已拖动"
                elif t == "type":
                    text = str(action.get("text", ""))
                    wc.type_text(text)
                    shown = text if len(text) <= 30 else text[:30] + "…"
                    desc = f"已输入「{shown}」"
                else:
                    key = str(action.get("key", "Enter"))
                    wc.press_key(key)
                    desc = f"已按键 {key}"
                return desc + self._facts(before)

            if t == "wait":
                return self._wait(float(action.get("seconds", 3)))
            return f"（{t} 没执行）"
        except Exception as exc:
            return f"执行失败：{exc}"

    def _facts(self, before: str) -> str:
        """把动作前后的原始数据附在结果里。

        **只报事实，不下"变没变"的结论**。判断是模型的事——它会拿到动作前后
        两张截图自己比。程序的数只有地址/标题/滚动/标签页数四项，页面内部滚动、
        弹窗浮现、按钮变灰这类变化一概看不出来，硬下结论只会误导模型
        （实测：把页面内部滚动误报成"没有任何变化"，模型就反复重试同一个动作，
        而它重试的动作其实每次都成功了）。
        """
        after = wc.page_state()
        if not (before and after):
            return ""
        try:
            old, new = json.loads(before), json.loads(after)
        except ValueError:
            return ""
        out = []
        if old.get("u") != new.get("u"):
            out.append(f"地址变了 -> {str(new.get('u'))[:80]}")
        if old.get("t") != new.get("t"):
            out.append(f"标题变了 -> {str(new.get('t'))[:40]}")
        a, b = int(old.get("tabs", -1)), int(new.get("tabs", -1))
        if a >= 0 and b > a:
            out.append(f"新开了 {b - a} 个标签页")
        dy = int(new.get("y", 0)) - int(old.get("y", 0))
        if abs(dy) > 2:
            out.append(f"页面滚了 {dy:+d}px（现在 {new.get('y')}px）")
        if not out:
            return "（数据上没什么变化，但**以你看到的两张图为准**）"
        return "（数据参考：" + "；".join(out) + "——**以你看到的两张图为准**）"

    def _wait(self, seconds: float) -> str:
        """等待，但分成小片，这样停止/插话能立刻响应。

        上限见 MAX_WAIT。分片睡是为了能被打断——
        哪怕模型让等 600 秒，你点停止或发一句话，0.2 秒内就结束。
        """
        seconds = max(0.0, min(MAX_WAIT, seconds))
        end = time.monotonic() + seconds
        self._interrupt.clear()
        self._waiting = True
        try:
            while time.monotonic() < end:
                if self._stop_evt.is_set():
                    return f"等待中断（{seconds:.0f}s 未走完）"
                if self._interrupt.is_set():
                    done = seconds - max(0.0, end - time.monotonic())
                    self._interrupt.clear()
                    return (f"等待被打断（原计划 {seconds:.0f}s，"
                            f"实际等了 {done:.0f}s）——有人发话了，先看他说什么")
                time.sleep(WAIT_SLICE)
        finally:
            self._waiting = False
        if seconds >= 60:
            return f"已等待 {seconds:.0f}s（约 {seconds / 60:.0f} 分钟）"
        return f"已等待 {seconds:.0f}s"

    def refresh(self) -> None:
        """取最新一帧贴到界面上。

        取帧本身几乎不耗时——pageCapture 内部有后台线程一直在抓，
        这里只是拿现成的图。所以贴图放主线程没问题，不会卡界面。
        """
        if self._closing:
            return          # 窗口已销毁，别再调度了（否则 tkinter 会刷一堆无效命令报错）
        try:
            # 先问有没有帧，别直接 get_frame()——它在等首帧时会阻塞，会卡住界面
            if not capture.has_frame():
                self.after(REFRESH_MS, self.refresh)
                return
            img = capture.get_frame()
            w, h = img.size

            # 单位要小心：winfo_* 返回**物理像素**，而 CTkImage 的 size 是**逻辑像素**
            # （customtkinter 会按屏幕缩放自己乘一遍）。屏幕 150% 缩放时两者差 1.5 倍，
            # 直接用物理值当 size，图会被撑大、溢出容器被裁掉（踩过）。
            # 用 ScalingTracker 拿缩放比（公共 API，窗口和 widget 都适用）。
            sc = ctk.ScalingTracker.get_widget_scaling(self)
            lw = self.left.winfo_width() / sc
            lh = self.left.winfo_height() / sc
            panel_h = self.panel.winfo_reqheight() / sc

            # 布局没完成时 winfo_* 返回 1，这时什么都别做，等下一帧
            if lw <= 50 or lh <= 50:
                self.after(REFRESH_MS, self.refresh)
                return

            if self._winsize is None:
                # 首帧：按"左栏画面 + 面板 + 右栏"的需要定窗口大小。
                # geometry 用物理像素，所以要乘缩放比。
                want_w = min(w, 1000)                # 画面显示宽度上限
                want_h = int(h * want_w / w)
                need_w = want_w + RIGHT_MIN_W + 80   # 画面 + 右栏 + 边距
                need_h = want_h + panel_h + 100
                self.geometry(f"{int(need_w * sc)}x{int(need_h * sc)}")
                self._winsize = True

            avail_w = max(200, lw - 20)
            avail_h = max(150, lh - panel_h - 60)
            k = min(avail_w / w, avail_h / h, 1.0)
            self._size = (max(1, int(w * k)), max(1, int(h * k)))
            self.screen.configure(width=self._size[0], height=self._size[1])

            # CTkImage 的 size 用逻辑像素，它会按屏幕缩放自己放大到物理像素
            self._img = ctk.CTkImage(light_image=img, dark_image=img,
                                     size=self._size)
            self.screen.configure(image=self._img, text="")
        except Exception as exc:
            self._err += 1
            if self._err <= 3:
                print(f"[refresh] {type(exc).__name__}: {exc}", flush=True)
        self.after(REFRESH_MS, self.refresh)

    def _load_config(self) -> None:
        """启动时把存过的配置填进输入框。没存过就留空。"""
        cfg = brain.load_config()
        if not cfg:
            self.cfg_hint.configure(text="未保存过")
            return
        self.base_url.set(cfg["base_url"])
        self.api_key.set(cfg["api_key"])
        self.model.set(cfg["model"])
        self.cfg_hint.configure(text="已加载保存的配置")

    def on_save_config(self) -> None:
        """把当前填的三项存下来，下次启动自动读。"""
        try:
            brain.save_config(self.base_url.get().strip(),
                              self.api_key.get().strip(),
                              self.model.get().strip())
        except Exception as exc:
            self.cfg_hint.configure(text=f"保存失败：{exc}", text_color="#b3261e")
            return
        self.cfg_hint.configure(text="已保存", text_color="#2f8f5b")
        # 配置变了，清掉对话历史，免得模型带着旧上下文跑
        if not self._running:
            brain.reset()

    def on_close(self) -> None:
        # 先发停止信号，免得模型线程还在跑
        self._stop_evt.set()
        # refresh 是 after 循环，窗口销毁后还会再触发一次，这里标记一下让它停
        self._closing = True
        self.destroy()


def _action_text(action: Dict[str, Any]) -> str:
    """把一个动作翻成人能看懂的一句话，显示在对话区里。"""
    t = action.get("type")
    if t == "click":
        return f"点击 ({action.get('x')}, {action.get('y')})"
    if t == "scroll":
        n = int(action.get("notches", 3))
        return f"滚动 {n:+d} 格（{'向下' if n > 0 else '向上'}）"
    if t == "drag":
        return (f"拖动 ({action.get('x1')}, {action.get('y1')}) → ({action.get('x2')}, {action.get('y2')})")
    if t == "type":
        return f"输入「{action.get('text', '')}」"
    if t == "press":
        return f"按键 {action.get('key', 'Enter')}"
    if t == "wait":
        return f"等待 {action.get('seconds', 3)}s"
    if t == "finish":
        return f"完成：{action.get('reason', '')}"
    if t == "ask_human":
        return f"求助：{action.get('question', '')}"
    return str(action)


def main() -> int:
    try:
        pid = launch_edge()
    except Exception as exc:
        print(f"启动 Edge 失败：{exc}")
        return 1
    print(f"Edge pid={pid}")

    try:
        wc.init(pid)
        # 把窗口定成小尺寸，网站会按它自动排版——这样截到的就是
        # "小窗口里的完整页面"，而不是从大窗口裁一块出来。
        # 高度要加掉浏览器界面（标签栏+地址栏）占的那一截。
        wc.resize(VIEW_W + 16, VIEW_H + 95)
        # 确保窗口是显示出来的：最小化/被遮住的窗口浏览器会停止渲染，截不到画面
        wc.move_in()
        capture.init(pid)   # 截图通道（CDP）
        print("已接管，开始刷画面")
    except Exception as exc:
        print(f"接管失败：{exc}")
        return 1

    Viewer().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
