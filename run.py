# -*- coding: utf-8 -*-
"""学习通自动化的监控界面。

    python run.py

启动带调试端口的 Edge（登录态存在 .profile/edge，只需登录一次），
弹出监视窗口：左上角实时显示画面，下面是模型配置和点击/滚动测试。
"""
from __future__ import annotations

import warnings
# 画面用裸 PhotoImage + paste 原地更新（性能考量，见 refresh 里的说明）。
# CTkLabel 见到非 CTkImage 会警告"HighDPI 不能缩放"——我们按物理像素贴，
# 尺寸本来就是对的，这条警告没有意义，滤掉。
warnings.filterwarnings("ignore", message=".*not CTkImage.*")

import json
import queue
import subprocess
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

import customtkinter as ctk
from PIL import Image as _PilImage, ImageTk
from PIL.Image import BILINEAR as Image_BILINEAR

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
AUTO_RESUME_AFTER = 300.0  # ask_human 之后多久自动重试一次（秒）
AUTO_RESUME_MAX = 3      # 连续自动重试上限，超了就老实等人
NO_PROGRESS_WARN = 12    # 连续这么多步看不到任何状态变化 -> 提醒模型换做法
NO_PROGRESS_STOP = 30    # 提醒过还不变 -> 判定卡死，停下整个回合

# 思考强度候选。**服务端认这些值**——传别的会被 422 拒绝，
# 错误信息里会把合法值列出来（实测拿非法值试探出来的）。
# 顺序按"从省到费"排，界面上好挑。
# 默认 high 而不是 max：max 只是"想得更久"不是"想得更对"（见 cfg_row 那段注释）。
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "ultra", "max")
DEFAULT_EFFORT = "high"

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
CHAT_KEEP_MIN = 10        # 裁剪时至少留这么多条，防止"算错高度→把对话删光"

# 流式"活气泡"的参数（只针对长输出时的实时思考）。
# **必须限高**：早先每 150ms 把最近 3000 字整段 configure 给一个 CTkLabel，
# Tk 每次都要把整段重新折行排版——实测 34-55ms/次（runs/stream_bench.py、
# runs/test_ab_stream.py），长思考时界面就一顿一顿的。现在改成 tk.Text **只追加
# 新片、只保留看得见的尾巴**：实测 0.5ms/次，事件循环最大延迟 65ms
# （runs/diag_e2e4.py）。旧文字反正已经滚出这个 6 行高的框，原始全文在
# runs/brain.log 里。
STREAM_MAX_LINES = 6      # 气泡最多显示几行；内容只保留能装下的尾部

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
    # 模型的实时思考/正文（流式）。用浅灰，和正式消息区分开——
    # 它们只是过程，最终结论会以 "ai" 再发一条
    "think": ("…", "#9aa0a6", False),
}


def launch_edge() -> Optional[int]:
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
            raise RuntimeError(
                "Edge 启动超时——多半是有残留 msedge 进程占着 edge/ 目录。"
                "先在任务管理器里结束所有 msedge.exe，再重试")

    # Popen 给的 pid 直接用，但要确认它真持有窗口——Chromium 有时会让子进程接手
    if proc is not None and wc.find_hwnd(proc.pid):
        return proc.pid

    pid = find_edge_pid()
    if pid is None:
        # 端口通了但枚举不到进程/窗口：**照样用 CDP 跑**，只是挪窗口、改大小
        # 这些可选功能不可用。以前这里直接抛错，用户看到的就是"双击打不开"。
        print("警告：拿不到 Edge 的 pid，改用纯调试协议模式（挪窗口/改大小不可用）",
              flush=True)
        return None
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
        self._photo = None      # 画面用的 PhotoImage：只建一次，之后 paste 原地更新
        self._photo_size = None # 当前 photo 的物理尺寸（变了才重建）
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
        # 「插话打断等待」用：发消息时置上，让 _wait 立刻收工去处理你说的话。
        # 和 _stop_evt（结束整个回合）分开——插话只是想跳过剩余等待，不是要停。
        self._interrupt = threading.Event()
        self._waiting = False       # 是否正卡在 wait 里（决定插话时提示哪句话）
        self._scroll_pending = False  # 已排了一个滚动回调，别重复排
        self._chat_h = 0            # 对话区已占高度（px），超 CHAT_MAX_PX 就丢老的
        self._wrap_cur = 0          # 当前用的折行宽度，跟新算出来的一样就不折腾
        self._wrap_pending = False  # 已排了一个重新折行的回调
        self._wrap_px = 0           # 上次重折行时的原始宽度（差几像素就不折腾）
        # 页面跑偏保护：记住最后一个"学习页"地址，跑偏时自动切回去
        self._last_study_url = ""
        self._guard_note = ""       # 本步带给模型的一句提醒（页面跑偏处理等）
        # ask_human 之后的自动重试
        self._auto_token = 0        # 令牌：用户一说话就作废挂着的自动重试
        self._auto_resumes = 0      # 连续自动重试次数
        self._auto_firing = False   # 正在执行"自动重试"（这时 on_send 不算用户发言）
        # "连续多少步没有任何状态变化"看门狗
        self._no_progress = 0
        self._last_facts_changed = True
        # 预览：上一帧对象（同一帧就不重复 resize+paste）
        self._last_frame_obj = None
        # 流式显示：模型的思考/正文逐片到达，往一条"活气泡"里**只追加**，
        # 同时把看不见的头部裁掉（气泡是 tk.Text，原因见 STREAM_MAX_LINES 上面）。
        self._stream_text = None    # 当前那条活气泡（None 表示还没开）
        self._stream_font = None    # 气泡字体对象（估算折行用，别每 tick 新建）
        # 流式片的队列：工作线程只往里塞（list.append 原子），主线程的
        # _stream_tick 每 150ms 取一次。**绝不能**每片都 after(0) 一次——
        # 一次长输出 ~1000 片，主线程会被打成一千次重排，界面直接卡死（踩过）。
        self._stream_q: list = []
        self._stream_kind = ""      # 气泡里当前片的类型："think" / "content"

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
        # 注意：**这里不能调 _load_config()**。思考强度的下拉框 self.effort 在下面
        # 才创建，而配置里只要带了 reasoning_effort，_load_config 就会访问它——
        # 已保存过思考强度的用户一启动就 AttributeError 崩溃（实测：界面根本
        # 打不开，控制台只有一行 traceback）。统一挪到 __init__ 最后再读配置。

        # 最大轮数：一个回合里模型最多能做多少次决策，防止它停不下来。
        # 放在这一行右侧，不新增行（新增行要改后面所有行号，容易撞车）。
        self.max_steps = ctk.StringVar(value=str(MAX_STEPS))
        ctk.CTkEntry(cfg_row, textvariable=self.max_steps, width=52, height=26,
                     font=("Microsoft YaHei", 12)).pack(side="right")
        ctk.CTkLabel(cfg_row, text="最大轮数", anchor="e",
                     font=("Microsoft YaHei", 12)).pack(side="right", padx=(0, 6))

        # 思考强度（reasoning_effort）：**别默认 max**。
        # 实测同一道理论力学题：max 档它反反复复验算 2.6 万字，把 token 额度
        # 吃光、正文一个字没剩，页面全程一动不动；降到 high 只想了 784 字就
        # 把题目读得一清二楚并给出动作。max 是"想得更久"不是"想得更对"。
        self.effort = ctk.StringVar(value=DEFAULT_EFFORT)
        ctk.CTkOptionMenu(cfg_row, variable=self.effort, values=list(EFFORTS),
                          width=96, height=26, font=("Microsoft YaHei", 12),
                          dropdown_font=("Microsoft YaHei", 12)).pack(
            side="left", padx=(10, 0))
        ctk.CTkLabel(cfg_row, text="思考强度", anchor="e",
                     font=("Microsoft YaHei", 12)).pack(side="left", padx=(10, 4))

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

        # 所有控件都建好了，这时才读配置（见上面那行注释）
        self._load_config()

        self.after(100, self.refresh)
        self.after(150, self._stream_tick)   # 流式显示的节流贴字循环

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

        **不能只信 _chat_h 这个累加值**：窗口宽度一变，每条消息的高度全变，
        累加值就失真了；失真后还按"超限"去删，会把消息删过头——实测拖一下
        窗口，对话区直接被删空。所以真要裁时，先让布局把新折行落实、按实际
        高度重算一遍再删，并且至少留 CHAT_KEEP_MIN 条。
        """
        kids = self.chat.winfo_children()
        if not kids:
            self._chat_h = 0
            return
        if self._chat_h <= CHAT_MAX_PX:
            return
        self.update_idletasks()
        kids = self.chat.winfo_children()
        self._chat_h = sum(max(0, w.winfo_reqheight()) for w in kids)
        while len(kids) > CHAT_KEEP_MIN and self._chat_h > CHAT_MAX_PX:
            oldest = kids[0]
            self._chat_h -= max(0, oldest.winfo_reqheight())
            oldest.destroy()
            kids = self.chat.winfo_children()
        self._chat_h = sum(max(0, w.winfo_reqheight()) for w in kids)

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
            # 最小化/刚映射时对话区宽度可能只有 1px：这时重算折行和裁剪
            # 毫无意义，反而会把每条消息都算成"几百行高"、把消息删光（实测）。
            try:
                raw_w = self.chat.winfo_width()
            except Exception:
                raw_w = 0
            # 最小化/刚映射时宽度可能是 1px：这时重算折行和裁剪毫无意义
            if raw_w <= 80:
                return
            # 宽度只差几个像素（拖动窗口的中间态）不值得给几百条消息重折行
            if abs(raw_w - self._wrap_px) < 6:
                return
            self._wrap_px = raw_w
            wrap = self._chat_wrap()
            if wrap == self._wrap_cur:
                return
            self._wrap_cur = wrap
            kids = self.chat.winfo_children()
            for lbl in kids:
                # 只给消息标签重设折行；流式气泡是 tk.Text（自己去滚），不吃这个
                if not isinstance(lbl, ctk.CTkLabel):
                    continue
                try:
                    lbl.configure(wraplength=wrap)
                except Exception:
                    pass
            # 折行变了，每条的高度也跟着变：**先让布局算完再量**，否则量到的
            # 是旧高度，_chat_h 会失真，接下来的裁剪就会删过头。
            self.update_idletasks()
            kids = self.chat.winfo_children()
            self._chat_h = sum(max(0, w.winfo_reqheight()) for w in kids)
            self._chat_trim()
            self._scroll_bottom()

        self.after(250, do)

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
                canvas = self.chat._parent_canvas
                # **先按实际内容刷新 scrollregion**：消息被裁掉/重折行之后，
                # canvas 的 scrollregion 可能还是旧高度；此时 yview_moveto(1.0)
                # 会把视口停到"内容下方"的空白里——看起来就是对话区全空（实测）。
                canvas.configure(scrollregion=canvas.bbox("all"))
                canvas.yview_moveto(1.0)
            except Exception:
                pass
        self.after(50, do)

    def _ui(self, kind: str, text: str) -> None:
        """子线程安全地追加一条对话。"""
        self.after(0, lambda: self._say(kind, text))

    # ---------- 流式显示 ----------
    def _stream(self, kind: str, text: str) -> None:
        """接收模型流式输出的一小片（在工作线程里被调用）。

        **这里只做一件事：往队列里塞。** 不能在这儿碰 tkinter——
        早先每片都 after(0) 一次、主线程随后把整个 3000 字的 Label 重配一遍
        （重排折行很贵）。一次长输出有 ~1000 片（实测思考 576 + 正文 482），
        主线程被打成一千次小重排，期间什么都干不了——用户看到的就是
        "模型一长输出整条界面就卡死"。改成入队 + _stream_tick 每 150ms
        取一次、只贴一次，主线程最多每秒 7 次更新，怎么都不卡。
        list.append 是原子操作，跨线程塞安全。
        """
        if text:
            self._stream_q.append((kind, text))

    def _stream_tick(self) -> None:
        """每 150ms 把队列里攒的片一次性贴到活气泡上（主线程）。"""
        if self._closing:
            return
        try:
            if self._stream_q:
                self._stream_drain()
        except Exception:
            pass
        self.after(150, self._stream_tick)

    def _new_stream_bubble(self):
        """建一条流式活气泡（tk.Text，只追加、只保留看得见的尾巴）。

        为什么不用 CTkLabel：Label 每次更新都要把整段文字重新折行排版，
        长输出时一次 30-55ms；Text 只把新增的片 insert 到末尾，主线程只处理
        新增那一小块，实测 0.5ms/次（runs/test_ab_stream.py）。

        样式尽量和普通消息一致：灰字表示"这是过程，不是结论"，正式消息
        随后会以 "ai" 再发一条。
        """
        # CTk 的字体是按"逻辑字号 × 显示缩放"转成像素的，Tk 要用负数才是像素，
        # 所以这里自己算：12 号逻辑字 × 缩放 -> -30px（250% 缩放时）。
        px = -max(1, round(12 * self._sc()))
        tb = tk.Text(self.chat, height=STREAM_MAX_LINES, wrap="word",
                     bd=0, highlightthickness=0, bg="#f7f8fa",
                     fg=_STYLE["ai"][1], padx=2, pady=2,
                     font=("Microsoft YaHei", px),
                     takefocus=0, cursor="arrow")
        tb.tag_configure("think", foreground=_STYLE["think"][1])
        tb.tag_configure("body", foreground=_STYLE["ai"][1])
        # 滚轮不让气泡滚它自己——用户想滚的是整个对话区（CTkScrollableFrame）。
        tb.bind("<MouseWheel>", lambda _e: "break")
        # 估算折行用：字体对象缓存一份，别每个 tick 新建
        try:
            self._stream_font = tkfont.Font(root=self, font=tb.cget("font"))
        except Exception:
            self._stream_font = None
        tb.insert("end", "⟳ 模型输出中…\n", ("think",))
        # 只读：用户不能往里打字（内容都是程序写的）
        tb.configure(state="disabled")
        tb.pack(fill="x", padx=6, pady=2, anchor="w")
        self._stream_text = tb
        self._stream_kind = "think"
        return tb

    def _trim_stream_tail(self, tb) -> None:
        """删掉气泡里"已经滚出可视区"的头部，让内容行数不超过控件高度。

        **不做任何 Tk 折行查询**：`count -displaylines` 在长文本上会把界面
        卡死（实测过，绝对不能用），`yview_moveto(1.0)` 也要重算整段排版
        （8-10ms/次、偶发 400ms）。这里用字宽估算折行，纯 Python 数行，
        实测 0.1ms 以内（runs/diag_e2e4.py）。

        删掉的旧文字不丢信息：它们本来就看不见了，原始全文在 runs/brain.log。
        """
        try:
            width = tb.winfo_width()
        except Exception:
            width = 0
        if width <= 50:
            # 气泡刚 pack、布局还没跑（或者窗口还没映射）：拿对话区宽度兜底，
            # 免得网络卡顿攒了一大堆字时首轮不裁、第一屏显示的是最老的内容。
            try:
                width = self.chat.winfo_width() - 20
            except Exception:
                width = 0
        cw = 1
        if self._stream_font is not None:
            try:
                cw = max(1, self._stream_font.measure("中"))
            except Exception:
                cw = 1
        if width <= 50 or cw <= 1:
            return                  # 还没排版（或拿不到字宽），下一个 tick 再裁

        cpl = max(10, (width - 8) // cw)      # 一行大约能放几个汉字
        for _ in range(4):
            content = tb.get("1.0", "end-1c")
            if not content:
                return
            lines = sum(max(1, -(-len(part) // cpl)) for part in content.split("\n"))
            if lines <= STREAM_MAX_LINES:
                return
            over = (lines - STREAM_MAX_LINES) * cpl
            tb.delete("1.0", f"1.0+{max(1, over)}c")

    def _stream_drain(self) -> None:
        """把队列里攒的片**追加**到活气泡上，然后裁掉看不见的头部（主线程）。

        关键：只 insert 新片，绝不重写已有内容。早先的写法是每 150ms 把最近
        3000 字整段 configure 给 Label，Tk 要重新折行排版整段——实测 34-55ms/次，
        长思考时主线程两成时间耗在这上面，界面就是"一顿一顿"的。

        现在只保留"能装进这个 6 行框的尾部"：不调用 yview 滚动（那一步要重算
        整段排版，实测 8-10ms/次、偶发 400ms 卡顿），旧文字直接从前面删。
        实测 0.5ms/次，事件循环最大延迟 ~65ms（runs/diag_e2e4.py）。
        """
        items: list = []
        while self._stream_q:
            items.append(self._stream_q.pop(0))
        if not items:
            return

        tb = self._stream_text
        if tb is None:
            tb = self._new_stream_bubble()

        try:
            tb.configure(state="normal")
            for kind, text in items:
                if kind != self._stream_kind:
                    # 思考结束、开始写正文：换行分隔，别糊在一起
                    sep = ("\n——— 开始输出正文 ———\n" if kind == "content"
                           else "\n——— 回到思考 ———\n")
                    tb.insert("end", sep, ("think",))
                    self._stream_kind = kind
                tb.insert("end", text, ("think",) if kind == "think" else ("body",))
            self._trim_stream_tail(tb)
        finally:
            try:
                tb.configure(state="disabled")
            except Exception:
                pass
        self._scroll_bottom()

    def _stream_flush(self) -> None:
        """活气泡收尾：把气泡撤掉、状态重置。

        **不留在对话区里**：这一步的结论紧接着会以 "ai" 消息再发一条，气泡本身
        只是"实时过程"；留着的话一个回合最多 50 条、每条都带着一大坨文字，
        越跑越慢（这也是之前越用越卡的一部分）。原始全文在 runs/brain.log。
        """
        self._stream_q.clear()
        tb = self._stream_text
        self._stream_text = None
        self._stream_font = None
        self._stream_kind = ""
        if tb is not None:
            try:
                tb.destroy()
            except Exception:
                pass
        self._scroll_bottom()

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
        # 用户开口了：取消可能挂着的"ask_human 自动重试"。
        # 注意：自动重试自己也会走 on_send，那种情况不能把重试计数清零
        # （否则 AUTO_RESUME_MAX 永远到不了，会无限自动重试）。
        self._auto_token += 1
        if not self._auto_firing:
            self._auto_resumes = 0

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
        self._auto_token += 1        # 手动停了就别自动重试了
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
            brain.init(base_url=base_url, api_key=api_key, model=model,
                       reasoning_effort=self.effort.get().strip() or DEFAULT_EFFORT)
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
        # 打开流式显示：模型边想边往界面上贴，长思考时不再一片空白
        brain.set_stream_handler(self._stream)
        try:
            while not self._stop_evt.is_set():
                limit = self._turn_limit()
                if self._step >= limit:
                    self._ui("done", f"到 {limit} 轮上限，先停下"
                                     f"（可以在左边改「最大轮数」再继续）")
                    break

                # 每步开始前先把"页面跑偏"处理掉（模型误点会跳到别的网站/
                # 误开新标签页，实测两次卡在「超星智能体」页）
                self._guard_page()

                img = capture.get_frame()
                extra = self._drain_inbox()
                # 画面是不是冻住了。这个事实**必须告诉模型**：截图冻住时，
                # 它看到的"页面没变化"不是它动作没用，而是截图早就停了——
                # 分不清这两者它就会一直重复同一个动作（实测：同一个坐标点了 41 次，
                # 因为抓帧停了、图一直是同一张，模型每次都得出同样的结论）。
                # 一次拿到旧帧检查 + 最新画面。**不再给元素编号**：编号每帧都变，
                # 上下文里积着旧编号反而干扰模型（详见 pageCapture.annotate 注释）。
                img = capture.get_frame()
                stale = capture.frame_age()
                if stale > 5.0:
                    self._ui("err", f"画面已经 {stale:.0f} 秒没更新了"
                                    f"（抓帧可能断了），下面这步的判断可能不准")
                # 给模型的只有**图片 + 最基本的网页状态**（视口/滚动/视频在不在播）。
                #
                # **不喂任何从页面里爬出来的文字**。原因有两条：
                # 1. 学习通用字体加密反爬——DOM 里存的是生僻字（"无论牵连璔憘作何
                #    璔憘"），靠 font-cxsecret 字体画成正确的字。截图里是对的，
                #    爬下来的文字是**错的**，喂过去只会把模型带偏。
                # 2. 那些清单又长又占 token（元素 40 项 + 任务点 120 项能到上千 token），
                #    实测还把关键信息稀释掉。
                # 元素坐标仍然准确（不受字体影响），但那是**代码自己用来点击的**，
                # 不需要模型知道——而且编号也已经取消：模型自己按网格坐标点。
                page = wc.page_info(state_only=True)
                video = wc.video_info()
                if video:
                    page = f"{page}；{video}" if page else video
                if self._guard_note:
                    # 页面跑偏被自动纠正：必须告诉模型，否则它按旧截图做判断
                    page = f"{self._guard_note}；{page}" if page else self._guard_note
                    self._guard_note = ""

                d = brain.decide(img, goal, extra=extra, page=page, frame_age=stale)
                # 这一步想完了，把"模型输出中"那条活气泡收尾
                self.after(0, self._stream_flush)
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
                    # 以前求完助就死等人工（实测两次，第二次空了 2 小时）。
                    # 现在安排一次 5 分钟后的自动重试；用户一说话就取消。
                    self._schedule_auto_resume()
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
                changed = self._last_facts_changed
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
                # ---- 连续无进展看门狗 ----
                # 重复动作熔断只看"完全相同的动作"，但模型可能每一步都换个坐标、
                # 却全都没效果（实测 3.3 测验的 D 选项换着坐标点了 8 次）。
                # 这里用"程序观察到的事实有没有任何变化"来判断。
                if t != "wait":
                    if changed:
                        self._no_progress = 0
                    else:
                        self._no_progress += 1
                        if self._no_progress == NO_PROGRESS_WARN:
                            result += ("\n（程序提醒：最近 %d 步没有观察到任何状态变化"
                                       "（地址/滚动/勾选/弹窗都没变）。换个做法："
                                       "换滚动区域、reload、或 ask_human）"
                                       % self._no_progress)
                        elif self._no_progress >= NO_PROGRESS_STOP:
                            self._ui("err", f"连续 {self._no_progress} 步没有任何状态变化，"
                                            "判定卡死，先停下")
                            self._ui("note", "可以：换个说法重新下达目标、点「清空」重来，"
                                             "或者自己看一眼页面卡在哪")
                            brain.note_result(result)
                            break
                # 关键：把执行结果告诉模型，否则它看不见效果，会一直重复同一个动作
                brain.note_result(result)
                self._step += 1
        except Exception as exc:
            self._ui("err", f"出错：{type(exc).__name__}: {exc}")
        finally:
            self._running = False
            brain.set_stream_handler(None)      # 关掉流式回调，别让它往已结束的回合里贴字
            self.after(0, self._stream_flush)
            self.after(0, lambda: self._set_buttons(busy=False))

    def _guard_page(self) -> None:
        """每步开始前把"页面跑偏"处理掉（在工作线程里调用）。

        模型误点会跳到别的网站、或误开「超星智能体」之类的新标签页（实测两次
        卡在那里，第二次停了近 2 小时才被发现）。规则：
        - 学习页（URL 含 studentstudy）→ 记住地址；
        - 学习通内部页面之间跳转 → 不干预（可能正是模型要去的页）；
        - 已知的"误点陷阱页"（超星智能体 / 学校站点）或跳到别的网站
          → 自动导航回记住的学习页，并给模型留一句说明。
        """
        try:
            state = wc.page_state()
            if not state:
                return
            d = json.loads(state)
            url = str(d.get("u") or "")
        except Exception:
            return
        if "studentstudy" in url:
            self._last_study_url = url
            return
        if not self._last_study_url:
            return
        # 这些是实测踩过的坑：误点会开到超星智能体/学校站点，然后在那边迷路。
        trap = ("robot.chaoxing.com", "42.62.3.119", "/School/")
        if "chaoxing.com" in url and not any(t in url for t in trap):
            return          # 正常的学习通页面，不干预
        try:
            wc.navigate(self._last_study_url)
            self._guard_note = (f"（程序提醒：当前页面跑偏到了 {url[:60]}，"
                                f"已经自动切回学习页，请只在学习页上操作）")
            self._ui("note", "页面跑偏，已自动切回学习页")
        except Exception as exc:
            self._guard_note = f"（程序提醒：页面跑偏，自动切回失败：{exc}）"

    def _schedule_auto_resume(self) -> None:
        """ask_human 之后安排一次自动重试（在工作线程里调用）。

        以前求助完就死等人工（实测两次，第二次空了 2 小时）。现在 5 分钟后
        自动发一句「继续」试一次；用户提前发消息会取消。连续自动重试最多
        AUTO_RESUME_MAX 次，再不行就老实等人。
        """
        if self._auto_resumes >= AUTO_RESUME_MAX:
            self._ui("note", f"已自动重试 {AUTO_RESUME_MAX} 次仍未恢复，等你处理"
                             f"（处理完在对话区说句话即可）")
            return
        self._auto_token += 1
        token = self._auto_token
        self._ui("note", f"{int(AUTO_RESUME_AFTER / 60)} 分钟后自动重试一次"
                         f"（你发消息会取消它）")

        def fire():
            if token != self._auto_token or self._running or self._closing:
                return
            self._auto_resumes += 1
            self._auto_firing = True
            try:
                self.msg.set("（自动重试）继续完成刚才的任务")
                self.on_send()
            finally:
                self._auto_firing = False

        self.after(int(AUTO_RESUME_AFTER * 1000), fire)

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
        self._last_facts_changed = True
        try:
            if t in ("click", "scroll", "drag", "type", "press"):
                before = wc.page_state()
                clicked_at = None
                if t == "click":
                    # **不再用编号**：模型直接读网格坐标点。编号每帧都在变，
                    # 上下文里全是旧编号会把它带偏（详见 pageCapture.annotate）。
                    if action.get("target") is not None:
                        self._last_facts_changed = False
                        return ("（现在没有元素编号了，请用 x / y 坐标点击；"
                                "target 字段不再支持。对照网格刻度量一下目标中心再点）")
                    cx, cy = int(action["x"]), int(action["y"])
                    wc.click(cx, cy, settle=0.6)
                    desc = f"已点击 ({cx}, {cy})"
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
                # 点了但没观察到变化 -> 提醒模型重新量坐标。
                # **不在这里附元素文字**：那些文字是从 DOM 爬的，学习通有字体加密
                # （DOM 里是生僻字、靠字体画成正确的字），爬来的就是错的，
                # 喂过去只会把模型带偏。模型对照网格重新量目标中心即可。
                if clicked_at and not self._last_facts_changed:
                    out += ("\n（这次点击没引起任何可观测变化。如果点偏了，"
                            "对照网格刻度重新量一下目标中心再点。）")
                return out

            if t == "wait":
                return self._wait(float(action.get("seconds", 3)))
            if t == "reload":
                before = wc.page_state()
                wc.reload_page()
                out = "已刷新页面" + self._facts(before)
                self._last_facts_changed = True   # 刷新算一次主动重置，别触发卡死判定
                return out
            self._last_facts_changed = False
            return f"（{t} 没执行）"
        except Exception as exc:
            self._last_facts_changed = False
            return f"执行失败：{exc}"

    def _facts(self, before: str) -> str:
        """把动作前后的原始数据附在结果里。

        **只报事实，不下"变没变"的结论**。判断是模型的事——它会拿到动作前后
        两张截图自己比。程序的数据包括：地址、标题、滚动、标签页数，以及新增的
        已勾选数、选中态数、可见弹窗数、内部滚动总位置。
        后四项专治「选答案/切标签/开关弹窗」这类不改地址的动作——以前它们
        全被报成「没有变化」，模型就反复点（实测 3.3 测验同一个选项点了 8 次）。
        （实测：把页面内部滚动误报成"没有任何变化"，模型就反复重试同一个动作，
        而它重试的动作其实每次都成功了）。
        """
        after = wc.page_state()
        self._last_facts_changed = False
        if not (before and after):
            self._last_facts_changed = True
            return ""
        try:
            old, new = json.loads(before), json.loads(after)
        except ValueError:
            self._last_facts_changed = True
            return ""

        def num(d, key, default=None):
            if not isinstance(d, dict) or key not in d:
                return default
            try:
                return int(d.get(key))
            except (TypeError, ValueError):
                return default

        out = []
        changed = False
        if old.get("u") != new.get("u"):
            out.append(f"地址变了 -> {str(new.get('u'))[:80]}")
            changed = True
        if old.get("t") != new.get("t"):
            out.append(f"标题变了 -> {str(new.get('t'))[:40]}")
            changed = True
        a, b = num(old, "tabs", -1), num(new, "tabs", -1)
        if a >= 0 and b > a:
            out.append(f"新开了 {b - a} 个标签页")
            changed = True
        dy = num(new, "y", 0) - num(old, "y", 0)
        if abs(dy) > 2:
            out.append(f"页面滚了 {dy:+d}px（现在 {new.get('y')}px）")
            changed = True
        # ---- 结构化状态（专门覆盖"不改地址"的动作）----
        for key, label in (("checked", "已勾选"), ("sel", "选中态"),
                           ("dlg", "可见弹窗")):
            o, n2 = num(old, key), num(new, key)
            if o is None or n2 is None or o == n2:
                continue
            changed = True
            out.append(f"{label} {o} -> {n2}")
        o, n2 = num(old, "itop"), num(new, "itop")
        if o is not None and n2 is not None and o != n2:
            changed = True
            out.append(f"内部滚动 {n2 - o:+d}px")

        self._last_facts_changed = changed
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
            # 抓帧线程 250ms 才换一帧，而界面 150ms 刷一次——同一帧对象直接跳过，
            # 省掉约 40% 的 resize+paste（每帧约 10ms）。
            if img is self._last_frame_obj:
                self.after(REFRESH_MS, self.refresh)
                return
            self._last_frame_obj = img
            # 窗口最小化时不缩放/贴图：看不见还烧 CPU（挂机时很常见）
            try:
                if self.state() == "iconic":
                    self.after(REFRESH_MS, self.refresh)
                    return
            except Exception:
                pass
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
            # 显示尺寸（逻辑像素）。PhotoImage 粘贴用物理像素，所以这里乘回缩放比
            dw = max(1, int(w * k))
            dh = max(1, int(h * k))

            # **不要每帧新建 CTkImage**：它会从原图重新缩放出整套 PhotoImage
            # （1360x748 的图每 150ms 缩放一次，实测把主线程拖得很沉——
            # 项目记忆里记过的性能陷阱）。正解：PhotoImage 只建一次，
            # 以后 photo.paste(缩小后的帧) 原地替换，零新建、零缩放缓存失效。
            phys = (max(1, int(dw * sc)), max(1, int(dh * sc)))
            if self._photo is None or self._photo_size != phys:
                self._photo = ImageTk.PhotoImage(_PilImage.new("RGB", phys))
                self._photo_size = phys
                # 尺寸只在重建时设一次；每帧都 configure(width/height) 会白白触发
                # 一次布局计算（实测每帧多花 0.2ms 且可能引起重绘）。
                self.screen.configure(image=self._photo, text="",
                                      width=dw, height=dh)
            self._photo.paste(img.resize(phys, Image_BILINEAR))
        except Exception as exc:
            self._err += 1
            if self._err <= 3:
                print(f"[refresh] {type(exc).__name__}: {exc}", flush=True)
        self.after(REFRESH_MS, self.refresh)

    def _load_config(self) -> None:
        """启动时把存过的配置填进输入框。没存过就留空。

        **绝不能因为配置读不进来就崩掉界面**：这里整体 try/except，出错只提示，
        程序照常打开（之前 reasoning_effort 的加载顺序 bug 就是在这里把启动
        搞崩的，用户看到的是"双击没反应"）。
        """
        try:
            cfg = brain.load_config()
        except Exception as exc:
            self.cfg_hint.configure(text=f"配置读取失败：{exc}", text_color="#b3261e")
            return
        if not cfg:
            self.cfg_hint.configure(text="未保存过")
            return
        try:
            self.base_url.set(cfg["base_url"])
            self.api_key.set(cfg["api_key"])
            self.model.set(cfg["model"])
            # 思考强度是后加的项，老配置里没有——有就用存的，没有保持界面默认。
            # hasattr 兜底：万一将来再出现"控件还没建好就读配置"的情况，也不崩。
            eff = str(cfg.get("reasoning_effort") or "").strip()
            if eff in EFFORTS and hasattr(self, "effort"):
                self.effort.set(eff)
            self.cfg_hint.configure(text="已加载保存的配置")
        except Exception as exc:
            self.cfg_hint.configure(text=f"配置应用失败：{exc}", text_color="#b3261e")

    def on_save_config(self) -> None:
        """把当前填的三项存下来，下次启动自动读。

        **不动对话历史**——清历史只由「清空」按钮负责。
        早先这里顺手 reset()，加上 init() 内部也 reset()，
        导致每发一句话上下文就被清一次（踩过）。
        """
        try:
            brain.save_config(self.base_url.get().strip(),
                              self.api_key.get().strip(),
                              self.model.get().strip(),
                              self.effort.get().strip())
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


# （原来这里有个 _elements_text()，把可点元素列成「编号 文字」喂给模型。
#  已删除：那些文字是从 DOM 爬的，学习通用字体加密反爬——DOM 里是生僻字
#  （"无论牵连璔憘作何璔憘"），靠 font-cxsecret 字体才画成正确的字。
#  后来连编号也取消了：编号每帧都在变，模型被上下文里的旧编号干扰。）

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


def find_edge_pid() -> "int | None":
    """查 Edge 的 pid：优先有主窗口的那个，都没有就返回第一个。

    用 Toolhelp 快照（`wc.list_pids`），**不用 tasklist**：受限环境里 tasklist 会
    报 "Access denied"，以前这里返回 None，上层再抛"拿不到 Edge 的 pid"，
    整个程序就起不来——用户看到的只是"双击打不开"（实测踩过）。
    """
    pids = wc.list_pids("msedge.exe")
    if not pids:
        return None
    for pid in pids:
        if wc.find_hwnd(pid):
            return pid
    return pids[0]


def _fail(msg: str) -> int:
    """打印失败原因，并写一份到 runs/startup_error.txt。

    双击运行时控制台一闪就没了，用户只能看到"打不开"；有了这个文件，
    下次出问题直接看它就知道卡在哪（也方便发给我）。
    """
    print(msg)
    try:
        p = ROOT / "runs" / "startup_error.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n",
                     encoding="utf-8")
    except Exception:
        pass
    return 1


def main() -> int:
    try:
        pid = launch_edge()
    except Exception as exc:
        return _fail(f"启动 Edge 失败：{exc}")
    print(f"Edge pid={pid}")

    # 接管分三步，**能降级的都降级**，别因为一个可选功能失败就整个打不开：
    # 1. CDP 连不上 -> 没法截图/点击，必须退出；
    # 2. 窗口句柄拿不到 -> 照样能后台跑，只是挪窗口/改大小不可用；
    # 3. 抓帧线程起不来 -> 没法工作，退出。
    try:
        wc.init(pid)
    except Exception as exc:
        return _fail(f"接管失败：{exc}")

    try:
        # 把窗口定成小尺寸，网站会按它自动排版——这样截到的就是
        # "小窗口里的完整页面"，而不是从大窗口裁一块出来。
        # 高度要加掉浏览器界面（标签栏+地址栏）占的那一截。
        wc.resize(VIEW_W + 16, VIEW_H + 95)
        # 先摆到屏幕上，尺寸/位置有个确定的起点；之后想藏起来可以用界面的开关
        wc.move_in()
    except Exception as exc:
        print(f"窗口调整失败（不影响后台运行）：{exc}", flush=True)

    try:
        capture.init(pid)   # 截图通道（CDP）
    except Exception as exc:
        return _fail(f"抓帧初始化失败：{exc}")
    print("已接管，开始刷画面")

    Viewer().mainloop()
    return 0


def _write_startup_error() -> None:
    """把未捕获异常的完整堆栈写进 runs/startup_error.txt（双击运行时看不到控制台）。"""
    import traceback
    try:
        p = ROOT / "runs" / "startup_error.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(traceback.format_exc(), encoding="utf-8")
    except Exception:
        pass


if __name__ == "__main__":
    try:
        _code = main()
    except SystemExit:
        raise
    except BaseException:
        _write_startup_error()
        raise
    raise SystemExit(_code)
