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

# 同一个动作重复太多次怎么办。**不能直接掐断**——"重复多少次算太多"跟任务
# 有关：翻长列表往上滚 15 次完全正常（实测就是这么误伤过一次，模型在找第 1 题，
# 被我的熔断中断了），而点同一个按钮 3 次就可疑。程序分不清，所以**先提醒模型，
# 让它结合画面自己判断**，只有提醒过它还是照旧才真停下（兜底，防死循环）。
#
# 计数按"动作签名"（类型 + 参数）算，坐标取整到 10px——模型微调一两像素地
# 原地磨也算重复。
REPEAT_WARN = 15          # 普通动作：做这么多次就提醒
REPEAT_STOP = 40          # 提醒过还是不停，才真停下
REPEAT_WARN_SCROLL = 40   # 滚动/翻页：重复本来就是正常操作，阈值放宽很多
REPEAT_STOP_SCROLL = 120
# wait 完全不计数：看视频要连着等很多次（3 小时的课每次 600 秒要等 18 次）。

# 浏览器窗口的页面区域尺寸。网站会按这个尺寸自动排版，
# 改这里就等于改"模型看到的页面布局"。
VIEW_W = 1080
VIEW_H = 720
RIGHT_MIN_W = 420        # 右栏（对话区）的最小宽度，免得被左侧画面挤没

# 对话区最多占多少像素高，超过就把最早的丢掉。
# **必须限高**：tkinter 的绘制坐标上限约 32767px，超了之后靠后的消息画不出来，
# 整个对话区看起来是空的（实测踩过）。按像素而不是条数来限，是因为
# 一条消息可能折好几行，只数条数兜不住。
# 另外控件越多每次追加越慢，所以这个值也不能给太大。
CHAT_MAX_PX = 20000

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
        # 每个动作做了多少次（签名 -> 次数）。超 REPEAT_LIMIT 就掐掉整个回合，
        # 见文件顶部那段说明。每个回合开始时清零。
        self._repeat: Dict[str, int] = {}
        # 当前这帧的可点元素清单（capture.snapshot 给的），_exec 用它把
        # 模型选的编号换成真实坐标
        self._elements: list = []
        # 「插话打断等待」用：发消息时置上，让 _wait 立刻收工去处理你说的话。
        # 和 _stop_evt（结束整个回合）分开——插话只是想跳过剩余等待，不是要停。
        self._interrupt = threading.Event()
        self._waiting = False       # 是否正卡在 wait 里（决定插话时提示哪句话）
        self._scroll_pending = False  # 已排了一个滚动回调，别重复排
        self._chat_h = 0            # 对话区已占高度（px），超 CHAT_MAX_PX 就丢老的
        self._wrap_cur = 0          # 当前用的折行宽度，跟新算出来的一样就不折腾
        self._wrap_pending = False  # 已排了一个重新折行的回调

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
            side="left", fill="x", expand=True, padx=(4, 4))
        # 刷新页面：模型也能自己刷（reload 动作），这个按钮是给人手动用的
        ctk.CTkButton(row2, text="↻ 刷新", height=28, width=70,
                      command=self.on_reload).pack(side="left", padx=(0, 0))

        self.status = ctk.CTkLabel(self.panel, text="", anchor="w",
                                   font=("Microsoft YaHei", 11))
        self.status.grid(row=8, column=0, columnspan=2, sticky="w", padx=14, pady=(2, 4))

        # 开关：显示/隐藏浏览器窗口。默认开。
        # 关掉会把窗口挪到屏幕外——**截图不受影响**（CDP 截图强制出帧，实测
        # 挪走后画面照常更新），所以想挂机打游戏时可以关掉藏起来。
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
        # 窗口拉宽/缩窄时重新给所有消息折行（宽度定死过一次，踩过）。
        # **必须 add="+"**：CTkScrollableFrame 自己也绑了 <Configure> 用来更新
        # canvas 的 scrollregion，bind 不带 add 会把它顶掉——内容照样长，但
        # canvas 不知道，滚动范围永远是空的，结果整个对话区滚不动（实测踩过：
        # 不绑=True、绑空的=False、绑 add+ =True，三方对照验证过）。
        self.chat.bind("<Configure>", self._on_chat_resize, add="+")

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
    def _chat_wrap(self) -> int:
        """对话区里一条消息的折行宽度（**逻辑像素**，直接喂给 CTkLabel）。

        两个坑都在这里踩过，所以写得啰嗦一点：

        1. **CTkLabel 会把 wraplength 按显示缩放再乘一遍**。屏幕 150% 缩放时，
           传 500 实际折行在 750——按物理像素算出来的宽度传进去，文字要跑到
           屏幕外才折行，看着就是被右边缘切掉（实测踩过）。所以这里量到物理
           宽度后要**除以缩放系数**转成逻辑像素。winfo_* 给的是物理像素，
           而 CTkImage/wraplength 这类是逻辑像素，这是本项目第 N 次栽在这上面。

        2. **必须每次按当前宽度现算**，不能建消息时定死：布局还没完成时
           winfo_width() 返回 1，定死的话所有消息都按同一宽度折行，拉宽窗口
           也不跟着变。
        """
        try:
            outer = self.chat.winfo_width()      # 物理像素
        except Exception:
            outer = 0
        if outer <= 50:
            return 200          # 布局没完成，先给个保守值；布局好了会重算

        sb = 0
        bar = getattr(self.chat, "_scrollbar", None)
        if bar is not None:
            try:
                sb = bar.winfo_width()
            except Exception:
                sb = 0
        if sb <= 1:
            sb = 16 * self._sc()      # 滚动条还没画出来，按经验预留

        # 减去两侧留白（pack 的 padx=6×2）和滚动条，再换成逻辑像素
        avail = outer - 12 - sb - 8
        return max(120, int(avail / self._sc()))

    def _sc(self) -> float:
        """当前显示缩放系数（150% 缩放时是 1.5）。拿不到就按 1.0 算。"""
        try:
            return max(1.0, float(ctk.ScalingTracker.get_widget_scaling(self)))
        except Exception:
            return 1.0

    def _chat_trim(self) -> None:
        """按高度上限丢掉最早的消息。

        不丢的话会一路堆到 tkinter 的坐标上限（约 32767px），之后的消息
        画不出来，整个对话区看着是空的（实测踩过）。
        """
        kids = self.chat.winfo_children()
        while len(kids) > 1 and self._chat_h > CHAT_MAX_PX:
            oldest = kids[0]
            self._chat_h -= max(0, oldest.winfo_reqheight())
            oldest.destroy()
            kids = self.chat.winfo_children()

    def _on_chat_resize(self, _event=None) -> None:
        """对话区尺寸变了就重新折行。

        拖动窗口会连续触发 Configure，所以节流一下——几百条消息全部重设
        wraplength 并重新量高度是有开销的。
        """
        if self._closing or self._wrap_pending:
            return
        self._wrap_pending = True

        def do():
            self._wrap_pending = False
            if self._closing:
                return
            wrap = self._chat_wrap()
            if wrap == self._wrap_cur:
                return
            self._wrap_cur = wrap
            kids = self.chat.winfo_children()
            for lbl in kids:
                try:
                    lbl.configure(wraplength=wrap)
                except Exception:
                    pass
            # 折行变了，每条的高度也跟着变，重新累计并裁掉超出的
            self._chat_h = sum(max(0, w.winfo_reqheight()) for w in kids)
            self._chat_trim()

        self.after(120, do)

    def _say(self, kind: str, text: str) -> None:
        """往对话区追加一条（带时间戳）。必须在主线程调用。

        三个坑都踩过，一起说明：
        1. **必须限高**：控件堆到 tkinter 的坐标上限（约 32767px）之后，
           新消息画不出来，整个对话区看着是空的。
        2. **不能用 grid 行号**：grid 的 row 必须唯一且连续，丢掉最早那条之后
           就得把剩下的全部重排一遍——那是 O(n) 每条消息，几百条就卡死了
           （实测）。pack 按加入顺序排，丢最早的不影响其它，天然 O(1)。
        3. **折行宽度要现算**，见 _chat_wrap。
        """
        prefix, color, bold = _STYLE.get(kind, _STYLE["note"])
        font = ("Microsoft YaHei", 12, "bold") if bold else ("Microsoft YaHei", 12)
        wrap = self._chat_wrap()
        self._wrap_cur = wrap
        stamp = time.strftime("%H:%M:%S")
        lbl = ctk.CTkLabel(self.chat, text=f"{stamp}  {prefix}  {text}",
                           anchor="w", justify="left",
                           wraplength=wrap, font=font, text_color=color)
        lbl.pack(fill="x", padx=6, pady=2, anchor="w")

        # 按累计高度丢老消息。用控件自己报的请求高度累加，
        # 不去问布局（winfo_reqheight 要等重排，取到的是旧值）。
        self._chat_h += max(0, lbl.winfo_reqheight())
        self._chat_trim()

        self._scroll_bottom()

    def _scroll_bottom(self) -> None:
        """滚到底。多条消息连续来时只排一次滚动，别每次追加都排一个 after。"""
        if self._closing or self._scroll_pending:
            return
        self._scroll_pending = True

        def do():
            self._scroll_pending = False
            if self._closing:
                return
            try:
                self.chat._parent_canvas.yview_moveto(1.0)
            except Exception:
                pass
        self.after(50, do)

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

    def on_reload(self):
        """手动刷新页面。刷新有等待，放子线程免得卡界面。"""
        self._run(wc.reload_page, "已刷新页面")

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
        self._repeat.clear()         # 重复计数也重开
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
        self._chat_h = 0
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
                # 画面是不是冻住了。这个事实**必须告诉模型**：截图冻住时，
                # 它看到的"页面没变化"不是它动作没用，而是截图早就停了——
                # 分不清这两者它就会一直重复同一个动作（实测：同一个坐标点了 41 次，
                # 因为抓帧停了、图一直是同一张，模型每次都得出同样的结论）。
                # 一次拿到配套的「图 + 元素清单」：图上的编号和清单里的下标必须
                # 是同一帧的，否则模型说"点 7 号"会点到别处（踩过：分两次取，
                # 中间后台线程换了帧）
                img, elements = capture.snapshot()
                self._elements = elements      # _exec 要用它把编号换成坐标
                stale = capture.frame_age()
                if stale > 5.0:
                    self._ui("err", f"画面已经 {stale:.0f} 秒没更新了"
                                    f"（抓帧可能断了），下面这步的判断可能不准")
                page = wc.page_info()
                # 可点元素的清单（编号 + 文字）。图上有编号，但小字容易被网格或
                # 相邻元素挡住，所以把清单也用文字给一份，两边对得上。
                if elements:
                    page = (f"{page}；【可点元素】（图上的蓝色数字就是编号，"
                            f"点哪个就填 target=编号）：{_elements_text(elements)}"
                            if page else _elements_text(elements))
                # 视频的真实状态（有没有在播、进度多少）——模型光看截图分不清
                # "正在播"和"已暂停"，会对着正在播的视频再点一下把它点停（踩过）
                video = wc.video_info()
                if video:
                    page = f"{page}；{video}" if page else video
                # 目录里哪些任务点已完成——绿勾在截图里太小，模型容易看漏、
                # 反复点已经做完的（实测踩过）
                tasks = wc.tasks_info()
                if tasks:
                    page = f"{page}；{tasks}" if page else tasks

                d = brain.decide(img, goal, extra=extra, page=page, frame_age=stale)
                self._ui("ai", d.thought)
                self._ui("act", _action_text(d.action))

                t = d.action.get("type")
                if t == "reply":
                    # 模型在回答人。回完就结束这一回合，等人说下一句——
                    # 不结束的话它答完会接着操作页面，等于没听人说话（实测踩过）。
                    self._ui("ai", d.action.get("text", ""))
                    self._ui("done", "已回话，回合结束（接着说就行）")
                    break
                if t == "finish":
                    self._ui("done", f"完成：{d.action.get('reason', '')}")
                    break
                if t == "ask_human":
                    self._ui("ask", d.action.get("question", "需要你处理"))
                    break
                if self._stop_evt.is_set():
                    self._ui("done", "已停止（这一步没执行）")
                    break

                # 重复动作计数。**每步都把次数告诉模型**（见下面拼进 result 那段），
                # 不只在跨过阈值时提醒一次——一次性提醒会被历史的滑动窗口冲掉：
                # 历史只留最近 8 步（MAX_HISTORY=12），提醒在第 15 次加进去，
                # 走 8 步就被裁没了，模型又不知道自己在重复（实测踩过：
                # 同一个坐标点了 41 次才被强制停，中间 15/30 次的提醒全白给）。
                n = 0
                if t != "wait":
                    sig = _action_sig(d.action)
                    n = self._repeat.get(sig, 0) + 1
                    self._repeat[sig] = n
                    warn_at, stop_at = _repeat_limits(t)
                    if n > stop_at:
                        self._ui("err", f"同一个动作（{_action_text(d.action)}）"
                                        f"做了 {n} 次，提醒过也没改，判定卡死，停下整个回合")
                        self._ui("note", "可以：换个说法重新下达目标、点「清空」重来，"
                                         "或者自己看一眼页面卡在哪")
                        break
                    if n == warn_at:
                        self._ui("note", f"这个动作已经做了 {n} 次，开始每步提醒模型")

                result = self._exec(d.action)
                self._ui("ok", result)
                # 每步都把这件事实附在结果里。模型自己数不清重复了几次
                # （它的想法每次都写着"改为点那一行文字"，以为在换办法，
                # 实际坐标一直是同一个），所以由程序数给它看。
                if t != "wait" and n >= 3:
                    result += ("\n（提醒：这个动作你已经做了 %d 次，"
                               "每一次的页面反应都是「没有变化」。"
                               "**程序判断是否重复只看动作类型和坐标，"
                               "不看你想点的是什么**——"
                               "所以「点目录项」和「点那一行文字」如果坐标一样，"
                               "在程序看来就是同一个动作。"
                               "反复做同一个动作没用时就该换个坐标、"
                               "换滚动区域、或 reload）" % n)
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
                clicked_at = None
                if t == "click":
                    # 优先用编号：模型选编号，坐标由代码从元素清单里查（准的）。
                    # 让它自己读坐标是行不通的——实测点「章节测验」标签，
                    # 真实中心 214，它读成 280，偏 66px 点到隔壁去了。
                    tgt = action.get("target")
                    if tgt is not None:
                        try:
                            idx = int(tgt)
                            # 编号从 1 开始。这里必须显式挡住 <=0：
                            # Python 的负索引不会报错，target=0 会悄悄点到
                            # 列表最后一个元素上（实测踩过）
                            if idx < 1:
                                raise ValueError("编号必须从 1 开始")
                            el = self._elements[idx - 1]
                            cx, cy = int(el["x"]), int(el["y"])
                            label = str(el.get("t") or el.get("tag") or "")
                            desc0 = f"点击 {idx} 号「{label[:20]}」"
                        except (ValueError, TypeError, IndexError, KeyError):
                            return (f"（编号 {tgt} 不存在——请重新看图上的蓝色数字，"
                                    f"用 target 填那个编号）")
                    else:
                        cx, cy = int(action["x"]), int(action["y"])
                        desc0 = f"已点击 ({cx}, {cy})"
                    wc.click(cx, cy, settle=0.6)
                    desc = desc0
                    clicked_at = (cx, cy)
                elif t == "scroll":
                    n = int(action.get("notches", 3))
                    # 页面里各块区域各滚各的（学习通左边视频区、右边目录栏就是），
                    # 模型可以指定往哪块滚；不给就滚整页（scroll 内部默认页面中间）
                    sx = int(action.get("x", 0) or 0)
                    sy = int(action.get("y", 0) or 0)
                    note = wc.scroll(n, sx, sy)
                    desc = f"已滚动 {n:+d} 格"
                    if sx or sy:
                        desc += f"（在 {sx}, {sy} 这块区域上滚的）"
                    desc += note      # 滚轮退化到直接滚动时会带一句说明
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
                out = desc + self._facts(before)
                # 点了但页面没变 -> 很可能是坐标读偏了（模型靠看图估小元素的中心，
                # 精度不够）。这时由程序把附近可点元素的**精确坐标**直接报给它，
                # 让它挑一个，而不是继续瞎猜——实测点「章节测验」标签读成 280，
                # 实际中心 214，偏 66px 点到隔壁，它还以为"页面没反应"。
                if clicked_at and "没有" in out:
                    near = wc.elements_near(*clicked_at)
                    if near:
                        out += "\n" + near
                return out

            if t == "wait":
                return self._wait(float(action.get("seconds", 3)))
            if t == "reload":
                before = wc.page_state()
                wc.reload_page()
                return "已刷新页面" + self._facts(before)
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
        """把当前填的三项存下来，下次启动自动读。

        **不动对话历史**——清历史只由「清空」按钮负责。
        早先这里顺手 reset()，加上 init() 内部也 reset()，
        导致每发一句话上下文就被清一次（踩过）。
        """
        try:
            brain.save_config(self.base_url.get().strip(),
                              self.api_key.get().strip(),
                              self.model.get().strip())
        except Exception as exc:
            self.cfg_hint.configure(text=f"保存失败：{exc}", text_color="#b3261e")
            return
        self.cfg_hint.configure(text="已保存", text_color="#2f8f5b")

    def on_close(self) -> None:
        # 先发停止信号，免得模型线程还在跑
        self._stop_evt.set()
        # refresh 是 after 循环，窗口销毁后还会再触发一次，这里标记一下让它停
        self._closing = True
        self.destroy()


def _repeat_limits(action_type: str) -> "tuple[int, int]":
    """这个动作类型该在多少次时提醒、多少次时强制停。

    滚动/翻页单独放宽：重复滚同一块区域本来就是正常操作（长列表要滚几十次），
    用普通动作的阈值会误伤——实测就这么中断过一次正常的翻页找题。
    """
    if action_type in ("scroll", "drag"):
        return REPEAT_WARN_SCROLL, REPEAT_STOP_SCROLL
    return REPEAT_WARN, REPEAT_STOP


def _elements_text(elements, limit: int = 60) -> str:
    """把可点元素列成「编号 文字」给模型看。

    图上已经画了编号，但小字容易被网格线或相邻元素挡住——文字版更保险，
    两边对得上，模型选编号时就不会数错。
    """
    parts = []
    for i, el in enumerate(elements[:limit], start=1):
        t = str(el.get("t") or "").strip().replace("\n", " ")
        tag = str(el.get("tag") or "")
        w, h = int(el.get("w") or 0), int(el.get("h") or 0)
        label = f"{t[:18]}" if t else f"<{tag}>"
        parts.append(f"{i}={label}({w}x{h})")
    return "，".join(parts)


def _action_sig(action: Dict[str, Any]) -> str:
    """给动作算一个"是不是同一件事"的签名，用来数重复次数。

    按**类型 + 参数**算，不只看类型：
    - 同一个坐标点 15 次 -> 算同一个动作，该掐
    - 点了 15 个不同坐标 -> 是不同动作，不该掐（模型在认真找出路）
    - 每次 wait 的秒数不同 -> 本来也不计入，这条只是保底

    坐标取整到 10px：模型每次把坐标微调 1-2 像素（"这次点 520 试试，那次点 522"）
    本质上还是同一个动作在原地磨，那种也该算重复。
    """
    t = str(action.get("type", "?"))
    if t in ("click", "scroll", "drag"):
        nums = []
        for k in ("x", "y", "x1", "y1", "x2", "y2"):
            v = action.get(k)
            if v is not None:
                try:
                    nums.append(f"{k}={int(round(float(v) / 10.0))}")
                except (TypeError, ValueError):
                    pass
        return f"{t}:{','.join(nums)}"
    if t == "press":
        return f"press:{action.get('key', '')}"
    if t == "type":
        # 打字看内容长度和开头就够，不用整段——目标不变时它会反复打同一串
        s = str(action.get("text", ""))
        return f"type:{len(s)}:{s[:20]}"
    if t == "reload":
        return "reload"
    return t


def _action_text(action: Dict[str, Any]) -> str:
    """把一个动作翻成人能看懂的一句话，显示在对话区里。"""
    t = action.get("type")
    if t == "click":
        if action.get("target") is not None:
            return f"点击 {action.get('target')} 号元素"
        return f"点击 ({action.get('x')}, {action.get('y')})"
    if t == "scroll":
        n = int(action.get("notches", 3))
        where = ""
        if action.get("x") or action.get("y"):
            where = f" @ ({action.get('x')}, {action.get('y')})"
        return f"滚动 {n:+d} 格（{'向下' if n > 0 else '向上'}）{where}"
    if t == "drag":
        return (f"拖动 ({action.get('x1')}, {action.get('y1')}) → ({action.get('x2')}, {action.get('y2')})")
    if t == "type":
        return f"输入「{action.get('text', '')}」"
    if t == "press":
        return f"按键 {action.get('key', 'Enter')}"
    if t == "wait":
        return f"等待 {action.get('seconds', 3)}s"
    if t == "reload":
        return "刷新页面"
    if t == "finish":
        return f"完成：{action.get('reason', '')}"
    if t == "ask_human":
        return f"求助：{action.get('question', '')}"
    if t == "reply":
        return f"回话：{action.get('text', '')}"
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
        # 先摆到屏幕上，尺寸/位置有个确定的起点；之后想藏起来可以用界面的开关
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
