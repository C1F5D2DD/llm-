# -*- coding: utf-8 -*-
"""输入：连上 Edge 的调试端口，点击 / 拖动 / 滚动。

    import window_control as wc

    wc.init(12345)              # Edge 的 pid
    wc.click(400, 300)          # 在坐标处点一下
    wc.scroll(3)                # 向下滚 3 格
    wc.drag(100, 500, 400, 500) # 从一处拖到另一处

走 Chrome DevTools Protocol，**不移动真实光标、不需要窗口可见或获得焦点**——
全程后台，你可以一边用它一边用鼠标干别的。

坐标：就是 capture.get_frame() 图上刻度尺标注的值，所见即所点。
（截图抓的是物理像素，CDP 用 CSS 像素，模块内部按 DPR 自动换算。）

截图在 capture.py 里（capture.get_frame()）。
"""
from __future__ import annotations

import ctypes
import json
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from typing import Optional

__all__ = ["init", "find_hwnd", "click", "drag", "scroll",
           "type_text", "press_key", "page_state", "page_info", "video_info",
           "move_out", "move_in", "resize", "WindowError"]

DEFAULT_PORT = 9222     # Edge 的调试端口，和 run.py 里启动时用的一致

user32 = ctypes.WinDLL("user32", use_last_error=True)

EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.GetWindowPlacement.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.GetDpiForWindow.argtypes = [wintypes.HWND]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
# SetWindowPos 原来漏了 argtypes。参数里有 HWND（64 位指针），不声明就按 C int 传，
# 在 64 位系统上属于把指针当整数塞，靠 hwnd 数值恰好不大才没出事。
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_uint]
user32.SetWindowPos.restype = wintypes.BOOL

SW_RESTORE = 9
SWP_NOACTIVATE = 0x0010
SWP_NOZORDER = 0x0004
SWP_SHOWWINDOW = 0x0040
OFFSCREEN_X = -32000
OFFSCREEN_Y = 0
WINDOW_CLASS = "Chrome_WidgetWin_1"
_SCROLL_STEP = 100      # 一"格"滚多少像素

# 页面指纹：地址 + 标题 + 滚动位置。**必须带上滚动位置**——只比地址和标题的话，
# 滚动这个动作永远被判成"页面没变化"，模型会以为滚动没用、退回去重复点击同一个坐标
# （实测死循环到步数上限）。
_PAGE_STATE_JS = ("JSON.stringify({u: location.href, t: document.title, "
                  "y: Math.round(window.scrollY)})")

# 视频的真实播放状态，**递归钻同源 iframe**。
# 为什么必须由程序来读、不能靠模型看图：播放器正中那个圆钮，播放时是"暂停"图标、
# 暂停时是"播放"图标，两者在截图里几乎一样。模型分不清，就会对正在播的视频再点一下，
# 反而把它暂停了（实测踩过：模型报告"视频已开始播放"、实际刚被自己点停），
# 然后心安理得地等 600 秒——整段等待全白费。
# 学习通的播放器嵌在 mooc1.chaoxing.com 的 iframe 里，与主文档同源，contentDocument 读得到。
_VIDEO_JS = """(() => {
  const out = [], seen = new Set();
  function walk(doc, depth) {
    if (!doc || depth > 6) return;
    try {
      for (const v of doc.querySelectorAll('video')) {
        if (seen.has(v)) continue;
        seen.add(v);
        out.push({
          paused: v.paused,
          t: Math.round(v.currentTime || 0),
          dur: Math.round(v.duration || 0),
          rate: v.playbackRate,
          ended: v.ended,
        });
      }
    } catch (e) {}
    try {
      for (const f of doc.querySelectorAll('iframe')) {
        let inner = null;
        try { inner = f.contentDocument; } catch (e) { inner = null; }   // 跨域 = null
        if (inner) walk(inner, depth + 1);
      }
    } catch (e) {}
  }
  walk(document, 0);
  return JSON.stringify(out);
})()"""

# 页面有多大、滚到哪了、有没有自己的滚动区。
# 模型只看得到视口这一屏，不知道下面还有东西，就会对着"屏幕上看得见但不是目标"的元素反复点。
#
# 注意 iframe：学习通的课程列表就在跨域的 iframe 里（i.chaoxing.com 嵌 v24.chaoxing.com），
# 同源策略让这里读不到它内部有多长。所以**必须把 iframe 数量报出来**，
# 否则会得出"页面只有一屏"这种错得离谱的结论（实测就是这么误导模型的）。
_PAGE_JS = """(() => {
  const de = document.documentElement, bd = document.body;
  const h = Math.max(de.scrollHeight, bd ? bd.scrollHeight : 0);
  const VW = window.innerWidth, VH = window.innerHeight;
  let iframes = 0;
  for (const f of document.querySelectorAll('iframe')) {
    const r = f.getBoundingClientRect();
    if (r.width > 200 && r.height > 200) iframes++;      // 小的是埋点框，不算
  }

  // 找出"自己能滚、而且现在还看得见"的区域。
  // 两点很关键：
  // 1. 坐标必须夹进视口——元素中心可能在屏幕外，模型照抄就点到别处了
  //    （实测报过 (1357, 455)，而视口只有 1075 宽）
  // 2. 页面上往往有好几块这样的区域（学习通左边视频区、右边目录栏各滚各的），
  //    只报一块的话模型没法选，想滚右边却滚了整页
  const inners = [];
  for (const el of document.querySelectorAll('div,main,section,ul,ol')) {
    const sh = el.scrollHeight, ch = el.clientHeight;
    const left = sh - ch - Math.round(el.scrollTop);      // 还能往下滚多少
    if (ch < 150 || left < 100) continue;
    const r = el.getBoundingClientRect();
    const x0 = Math.max(0, r.left), x1 = Math.min(VW, r.right);
    const y0 = Math.max(0, r.top), y1 = Math.min(VH, r.bottom);
    if (x1 - x0 < 120 || y1 - y0 < 120) continue;         // 露出来太少，滚了也不准
    inners.push({left: left, x: Math.round((x0 + x1) / 2), y: Math.round((y0 + y1) / 2)});
  }
  inners.sort((a, b) => b.left - a.left);
  const keep = [];                                        // 同一块区域会被内外层重复报，去重
  for (const it of inners) {
    if (!keep.some(k => Math.abs(k.x - it.x) < 60 && Math.abs(k.y - it.y) < 60)) keep.push(it);
    if (keep.length >= 3) break;
  }
  return JSON.stringify({h: h, vw: VW, vh: VH, y: Math.round(window.scrollY),
                         inners: keep, iframes: iframes});
})()"""


# 直接改 DOM 的滚动位置。用在"滚轮发不出去"的时候当后路。
# 为什么需要它：页面处于 hidden（窗口被隐藏/挪到屏幕外、被遮挡）时，
# Chromium 会停掉合成器，**mouseWheel 事件永远等不到回执**——实测挂满 15 秒超时，
# 而 eval/截图不走合成器所以一切正常，症状特别有迷惑性（表现为"滚不动"）。
# 这里从给定坐标往上找第一个能滚的元素来滚，所以"右边小组件"也能滚得动；
# 找不到就滚整页；顺带钻同源 iframe。
_SCROLL_JS = """(() => {
  const D = %d, X = %d, Y = %d;
  function tryScroll(doc, x, y) {
    let cur = null;
    try { cur = doc.elementFromPoint(x, y); } catch (e) { return null; }
    while (cur) {
      const oy = doc.defaultView.getComputedStyle(cur).overflowY || '';
      const can = (cur.scrollHeight - cur.clientHeight) > 4;
      if (can && (oy === 'auto' || oy === 'scroll' || oy === 'overlay')) {
        const before = cur.scrollTop;
        cur.scrollTop = before + D;
        if (Math.abs(cur.scrollTop - before) >= 1) {
          return {how: cur.tagName + '.' + String(cur.className || '').slice(0, 24),
                  top: Math.round(cur.scrollTop)};
        }
      }
      cur = cur.parentElement;
    }
    return null;
  }
  function visit(doc, x, y, depth) {
    if (!doc || depth > 6) return null;
    let el = null;
    try { el = doc.elementFromPoint(x, y); } catch (e) { return null; }
    if (el && el.tagName === 'IFRAME') {
      try {
        const r = el.getBoundingClientRect();
        const inner = el.contentDocument;
        if (inner) {
          const got = visit(inner, x - r.left, y - r.top, depth + 1);
          if (got) return got;
        }
      } catch (e) {}
    }
    return tryScroll(doc, x, y);
  }
  const got = visit(document, X, Y, 0);
  if (got) return JSON.stringify(got);
  const before = window.scrollY;
  window.scrollBy(0, D);
  return JSON.stringify({how: 'page', top: Math.round(window.scrollY),
                         ok: Math.abs(window.scrollY - before) >= 1});
})()"""


class WindowError(RuntimeError):
    """连不上、点击失败之类的问题，消息里说明原因和怎么办。"""


def _rect(hwnd: int):
    buf = ctypes.create_string_buffer(16)
    if not user32.GetWindowRect(hwnd, ctypes.byref(buf)):
        return None
    return tuple(ctypes.cast(buf, ctypes.POINTER(ctypes.c_long * 4)).contents)


def _class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _pid_of(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _normal_rect(hwnd: int):
    """窗口"正常状态"下的位置——**最小化的窗口也能拿到真实尺寸**。

    GetWindowRect 对最小化窗口返回的是图标位置（面积很小），
    直接用它判断会漏掉最小化的窗口（踩过：find_hwnd 全返回 None）。
    """
    buf = ctypes.create_string_buffer(44)      # WINDOWPLACEMENT 至少 44 字节
    if not user32.GetWindowPlacement(hwnd, ctypes.byref(buf)):
        return None
    # rcNormalPosition 在偏移 28 处，4 个 LONG
    off = 28
    left, top, right, bottom = ctypes.cast(
        ctypes.byref(buf, off), ctypes.POINTER(ctypes.c_long * 4)).contents
    return (left, top, right, bottom)


def _find_window(pid: int) -> Optional[int]:
    """按 pid 找主窗口（面积最大的那个）。"""
    found = []

    def cb(hwnd, _):
        h = int(hwnd)
        if _class(h) != WINDOW_CLASS:
            return True
        if _pid_of(h) != pid:
            return True
        r = _normal_rect(h) or _rect(h)
        if r and (r[2] - r[0]) * (r[3] - r[1]) > 100000:
            found.append((h, (r[2] - r[0]) * (r[3] - r[1])))
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    if not found:
        return None
    found.sort(key=lambda x: -x[1])
    return found[0][0]


def find_hwnd(pid: int) -> Optional[int]:
    """按 pid 找浏览器主窗口句柄，找不到返回 None。"""
    return _find_window(pid)


class _CDP:
    """一条 CDP 连接。断线后下次调用自动重连。"""

    def __init__(self, port: int) -> None:
        self.port = port
        self.ws = None
        self.msg_id = 0
        self._ws_url = None     # 当前连着的页面，换了要重连

    def _target(self) -> dict:
        """取当前活跃的标签页。

        每次调用都重新查——用户会手动切标签页，如果锁死第一次选的那个，
        后续操作就会一直作用在旧页面上（实测踩过：切了标签页后滚动还是滚旧的）。

        挑页规则：跳过 edge://newtab 这类内部页（在上面执行 JS 会失败），
        优先 http/https，多个时取排在最前的（Edge 把活跃标签排前面）。
        """
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/json/list", timeout=5) as r:
                pages = [p for p in json.loads(r.read()) if p.get("type") == "page"]
        except (urllib.error.URLError, OSError) as exc:
            raise WindowError(f"连不上调试端口 {self.port}：{exc}") from exc
        if not pages:
            raise WindowError("浏览器里没有打开的页面")

        real = [p for p in pages
                if (p.get("url") or "").startswith(("http://", "https://"))]
        return real[0] if real else pages[0]

    def call(self, method: str, params: Optional[dict] = None, timeout: float = 15.0) -> dict:
        from websockets.exceptions import ConnectionClosed, WebSocketException

        # 页面变了（用户切了标签页）就连到新页面上去
        want = self._target()["webSocketDebuggerUrl"]
        if self.ws is None or self._ws_url != want:
            if self.ws is not None:
                try:
                    self.ws.close()
                except Exception:
                    pass
            from websockets.sync.client import connect

            self.ws = connect(want, max_size=None)
            self._ws_url = want
        self.msg_id += 1
        mid = self.msg_id
        try:
            self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            deadline = time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError
                msg = json.loads(self.ws.recv(timeout=left))
                if msg.get("id") == mid:
                    if "error" in msg:
                        raise WindowError(f"{method} 失败：{msg['error'].get('message')}")
                    return msg.get("result", {})
        except ConnectionClosed:
            self.ws = None
            self._ws_url = None      # 一并清掉，下次调用会重连
            raise WindowError(f"{method}：连接断了，重试一次看看")
        except (TimeoutError, OSError, WebSocketException) as exc:
            raise WindowError(f"{method}：没能在 {timeout}s 内完成") from exc

    def eval(self, expr: str):
        r = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return r["result"].get("value")


# ---------------------------------------------------------------- 对外接口

_cdp: Optional[_CDP] = None
_hwnd: Optional[int] = None
_pid: Optional[int] = None       # 记着 pid，句柄失效时好按它重新找窗口


def init(pid: int) -> int:
    """接管指定进程的浏览器窗口。返回窗口句柄。失败抛 WindowError。

    pid 是 Edge 的主进程 pid（run.py 启动后传进来）。
    Edge 必须带 --remote-debugging-port=9222 启动，否则连不上——
    Chromium 忽略程序伪造的鼠标消息，只能走调试协议才能后台点击。
    """
    global _cdp, _hwnd, _pid

    _pid = pid
    _hwnd = _find_window(pid)
    if _hwnd is None:
        raise WindowError(f"进程 {pid} 没找到浏览器窗口，确认它开着且没最小化")

    # 窗口找到了，但 CDP 连不上就等于没法点击，这里要明确报错别让它半死不活
    cdp = _CDP(DEFAULT_PORT)
    try:
        cdp.eval("1")
    except WindowError as exc:
        _hwnd = None
        raise WindowError(
            f"窗口找到了，但连不上调试端口 {DEFAULT_PORT}（{exc}）。"
            "Edge 必须用 run.py 那样带 --remote-debugging-port 启动，"
            "而且要用独立的 --user-data-dir，否则新命令只会被已有实例接管、端口不开"
        ) from exc

    _cdp = cdp
    return _hwnd


def _live_hwnd() -> int:
    """取一个**当前有效**的窗口句柄，失效就按 pid 重新找。

    窗口句柄不是永久的：Edge 重启、Chromium 重建窗口之后，原来存下的 hwnd
    就成了死句柄。拿死句柄调 GetWindowRect 会失败 → `_rect()` 返回 None →
    再往下 `r[2]` 抛 "NoneType is not subscriptable"，
    报错信息里完全看不出是句柄过期了（实测踩过，表现为"窗口挪不动"）。
    """
    global _hwnd

    if _hwnd and user32.IsWindow(_hwnd):
        return _hwnd
    if _pid:
        found = _find_window(_pid)
        if found:
            _hwnd = found
            return found
    raise WindowError("浏览器窗口不见了（可能 Edge 被关掉或重启过），"
                      "重新启动本程序再接管一次")


def _place(hwnd: int, x: int, y: int, w: int, h: int) -> None:
    """挪/改窗口，失败就明确报错。

    原来不看 SetWindowPos 的返回值——它失败时只返回 0，一路静默，
    表现就是"点了没反应"，完全无从排查。
    """
    ok = user32.SetWindowPos(hwnd, 0, int(x), int(y), int(w), int(h),
                             SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW)
    if not ok:
        raise WindowError(f"移动窗口失败（SetWindowPos 返回 0，错误码 "
                          f"{ctypes.get_last_error()}）")


def _require() -> _CDP:
    if _cdp is None:
        raise WindowError("还没接管窗口，先调用 init(pid)")
    return _cdp


def _mouse(event_type: str, x: float, y: float, button: str = "none",
           clicks: int = 0) -> None:
    """发一条鼠标事件。

    坐标就是截图上的刻度值（CSS 像素），不用换算——截图模块 pageCapture
    给的也是同一套单位。

    CDP 的规矩：按下/抬起要带 button，移动不带；点击额外带 clickCount。
    """
    _require().call("Input.dispatchMouseEvent", {
        "type": event_type, "x": int(x), "y": int(y),
        "button": button, "clickCount": clicks,
    })


# ---------------------------------------------------------------- 输入

def click(x: int, y: int, settle: float = 0.4) -> None:
    """在坐标 (x, y) 处点一下。

    settle 是点完之后等多久（给页面反应时间），设 0 可以立刻返回。
    """
    _mouse("mouseMoved", x, y)
    time.sleep(0.03)
    _mouse("mousePressed", x, y, button="left", clicks=1)
    time.sleep(0.02)
    _mouse("mouseReleased", x, y, button="left", clicks=1)
    if settle:
        time.sleep(settle)


def drag(x1: int, y1: int, x2: int, y2: int, steps: int = 10,
         settle: float = 0.4) -> None:
    """从 (x1, y1) 拖到 (x2, y2)。用于滑块、拖动进度条这类操作。

    中间分几步移动，一步跳过去往往不生效——页面里的拖拽大多监听 mousemove。
    """
    _mouse("mouseMoved", x1, y1)
    time.sleep(0.03)
    _mouse("mousePressed", x1, y1, button="left", clicks=1)
    time.sleep(0.03)
    for i in range(1, steps + 1):
        _mouse("mouseMoved", x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps,
               button="left")
        time.sleep(0.02)
    _mouse("mouseReleased", x2, y2, button="left", clicks=1)
    if settle:
        time.sleep(settle)


def scroll(notches: int, x: int = 0, y: int = 0, settle: float = 0.3) -> str:
    """滚轮，正数向下滚。返回一句话说明实际怎么滚的（给界面/模型看）。

    直接用 dispatchMouseEvent 的 mouseWheel——带 deltaY 就是滚轮语义。
    比 synthesizeScrollGesture 好：那个是模拟手势，会真的去挪系统光标。

    x/y 是滚动发生的坐标（和 click 一样，按截图刻度填；0 就是不指定 = 页面中间）。
    **页面里各块区域是各滚各的**，比如学习通左边视频区、右边目录栏是两个独立的
    滚动容器：想让目录栏滚，就必须把指针放到目录栏上再滚。

    两种情况会退化到"直接改 DOM 滚动位置"（见 _SCROLL_JS 的说明）：
    - 页面报 hidden：这时 Chromium 停了合成器，滚轮发出去也永远没有回执，
      硬等只会卡住十几秒
    - 滚轮发出去了但超时：同上，兜一下
    返回值里会写明是哪种方式，方便排查"为什么滚了没反应"。

    实现上**先试滚轮、失败再退**，不去看 document.visibilityState：
    那个信号不可靠（实测同一个 hidden 状态下，滚轮有时 0.1 秒就回来、
    有时永远不回来），拿它当判据会白白错过能用滚轮的时候——真滚轮能触发
    页面的 wheel 事件（懒加载之类要靠它），比直接改 scrollTop 更保真。
    """
    cdp = _require()
    if not x and not y:
        # 没指定位置就在页面中间滚
        w = cdp.eval("innerWidth") or 1
        h = cdp.eval("innerHeight") or 1
        x, y = int(w // 2), int(h // 2)
    x, y = int(x), int(y)
    delta = _SCROLL_STEP * int(notches)

    # 先把指针移过去（悬停到位），再滚——有些页面要指针真在区域上才认滚轮
    cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y,
                                          "button": "none", "clickCount": 0})
    time.sleep(0.03)
    # 正常情况滚轮几十毫秒就回来；回不来就是回不来了（实测能挂满 15 秒），
    # 所以超时给短一点，早点走退路，别让模型干等
    try:
        cdp.call("Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": x, "y": y, "deltaX": 0, "deltaY": delta,
        }, timeout=1.5)
        if settle:
            time.sleep(settle)
        return ""
    except WindowError:
        pass

    note = _js_scroll(cdp, x, y, delta, "滚轮没回执")
    if settle:
        time.sleep(settle)
    return note


def _js_scroll(cdp: "_CDP", x: int, y: int, delta: int, why: str) -> str:
    """退路：直接改 DOM 里的滚动位置。返回一句说明，附上退化的原因。"""
    try:
        raw = cdp.eval(_SCROLL_JS % (delta, x, y))
        d = json.loads(raw) if isinstance(raw, str) else raw
    except Exception as exc:
        return f"（{why}，直接滚动也失败了：{exc}）"
    if not isinstance(d, dict):
        return f"（{why}，直接滚动没有返回结果）"
    how = d.get("how", "?")
    if how == "page":
        return f"（{why}，改为直接滚整页，现在 {d.get('top')}px）"
    return f"（{why}，改为直接滚 {how}，现在 {d.get('top')}px）"


def type_text(text: str, settle: float = 0.2) -> None:
    """在当前焦点处输入文字。

    用 Input.insertText 一次性写入——比逐键模拟快，而且**中文没问题**
    （逐键模拟对中文是无效的，只能写 ASCII）。
    """
    cdp = _require()
    cdp.call("Input.insertText", {"text": str(text)})
    if settle:
        time.sleep(settle)


# CDP 按一个键需要同时给 key / code / windowsVirtualKeyCode
_KEYS = {
    "Enter": ("Enter", "Enter", 13),
    "Tab": ("Tab", "Tab", 9),
    "Escape": ("Escape", "Escape", 27),
    "Backspace": ("Backspace", "Backspace", 8),
    "Delete": ("Delete", "Delete", 46),
    "Space": (" ", "Space", 32),
    "ArrowUp": ("ArrowUp", "ArrowUp", 38),
    "ArrowDown": ("ArrowDown", "ArrowDown", 40),
    "ArrowLeft": ("ArrowLeft", "ArrowLeft", 37),
    "ArrowRight": ("ArrowRight", "ArrowRight", 39),
    "Home": ("Home", "Home", 36),
    "End": ("End", "End", 35),
    "PageUp": ("PageUp", "PageUp", 33),
    "PageDown": ("PageDown", "PageDown", 34),
}

# 组合键的修饰键位（CDP 的 modifiers 是位掩码：Alt=1 Ctrl=2 Meta=4 Shift=8）
_MODS = {"ctrl": 2, "control": 2, "alt": 1, "shift": 8, "meta": 4, "cmd": 4, "win": 4}


def press_key(key: str, settle: float = 0.2) -> None:
    """按一个键，支持组合键。

    例：Enter / Tab / Escape / a / Control+a（全选）/ Control+v（粘贴）

    早期版本只支持白名单里的单键，模型想按 Ctrl+A 时一直失败 → 卡死循环。
    现在补上修饰键和字母数字键：
    - 修饰键用 modifiers 位掩码
    - 字母/数字键自动生成 code（KeyA / Digit1）

    完全不认识的键直接抛 WindowError，别静默失败。
    """
    cdp = _require()
    key = str(key).strip()

    # 拆修饰键：Control+a → modifiers=2, base="a"

    parts = [p.strip() for p in key.split("+") if p.strip()]
    if not parts:
        raise WindowError("按键为空")

    modifiers = 0
    for p in parts[:-1]:
        m = _MODS.get(p.lower())
        if m is None:
            raise WindowError(f"不支持的修饰键 {p!r}，可用：{', '.join(_MODS)}")
        modifiers |= m

    base = parts[-1]

    if base in _KEYS:
        k, code, vk = _KEYS[base]
    elif len(base) == 1:
        ch = base
        k = ch
        up = ch.upper()
        if ch.isalpha():
            code = "Key" + up
            vk = ord(up)
        elif ch.isdigit():
            code = "Digit" + ch
            vk = ord(ch)
        else:
            code = ch
            vk = ord(up) if up.isalnum() else 0
    else:
        raise WindowError(
            f"不支持的按键 {base!r}，可用：{', '.join(_KEYS)}，或单个字母/数字，或 Control+a 这样的组合键"
        )

    params = {
        "type": "keyDown",
        "key": k, "code": code,
        "windowsVirtualKeyCode": vk,
        "nativeVirtualKeyCode": vk,
        "modifiers": modifiers,
    }
    cdp.call("Input.dispatchKeyEvent", params)
    cdp.call("Input.dispatchKeyEvent", {**params, "type": "keyUp"})
    if settle:
        time.sleep(settle)


# ---------------------------------------------------------------- 页面状况

def page_state() -> str:
    """页面指纹（地址 + 标题 + 滚动位置 + 标签页数），用来判断一步动作有没有让页面变。

    **标签页数必须算进去**：很多链接（B 站搜索结果就是）在新标签页里打开，
    原来那个标签页的地址/标题/滚动位置一点没变——只比这三样的话，一次
    成功的点击会被报成"页面没有任何变化"，模型就以为没生效、反复点同一个地方
    （实测死循环的根因）。

    拿不到就返回空串，由调用方决定怎么说——查页面信息失败不该把整回合搞断。
    """
    try:
        raw = _require().eval(_PAGE_STATE_JS)
    except Exception:
        return ""
    if not isinstance(raw, str) or not raw:
        return ""
    try:
        d = json.loads(raw)
    except ValueError:
        return ""
    d["tabs"] = _tab_count()
    return json.dumps(d, ensure_ascii=False)


def _tab_count() -> int:
    """打开着的页面标签数。查不到返回 -1（那就不参与比较）。"""
    try:
        raw = urllib.request.urlopen(
            f"http://127.0.0.1:{DEFAULT_PORT}/json/list", timeout=2).read()
        return len([t for t in json.loads(raw)
                    if t.get("type") == "page"
                    and (t.get("url") or "").startswith(("http://", "https://"))])
    except Exception:
        return -1


def video_info() -> str:
    """页面上视频的真实播放状态，拼成一句话给模型。没有视频就返回空串。

    **这是程序唯一能替模型"看准"的东西**：播放器正中的圆钮在截图里看着都一样，
    模型分不清"正在播"和"已暂停"，经常对着正在播的视频再点一下把它点停，
    还以为自己"开始播放"了，然后白等 10 分钟（实测踩过）。

    返回值形如「视频正在播放，进度 7:22 / 33:00；距播完还需约 25 分 38 秒」。
    """
    try:
        raw = _require().eval(_VIDEO_JS)
        vids = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return ""
    if not isinstance(vids, list) or not vids:
        return ""

    v = vids[0]        # 一屏基本只有一个播放器；有多个就看第一个
    try:
        t = int(v.get("t") or 0)
        dur = int(v.get("dur") or 0)
        rate = float(v.get("rate") or 1)

        def mmss(x: int) -> str:
            return f"{x // 60}:{x % 60:02d}"

        if v.get("ended"):
            state = "已播放完毕"
        elif v.get("paused"):
            state = "**已暂停，没有在播**（要播放必须点播放键，别再点视频中央）"
        else:
            state = "正在播放"

        parts = [f"视频{state}，进度 {mmss(t)} / {mmss(dur)}"]
        if rate != 1:
            parts.append(f"倍速 {rate:g}x")
        if not v.get("paused") and not v.get("ended") and dur > t:
            remain = (dur - t) / max(0.1, rate)
            parts.append(f"距播完还需约 {int(remain // 60)} 分 {int(remain % 60)} 秒")
        return "；".join(parts)
    except Exception:
        return ""


def page_info() -> str:
    """页面有多大、滚到哪了，拼成一句话塞给模型。

    模型只看得到视口这一屏。不告诉它"当前滚到 680px、下面还有 1560px"，
    它就会以为图里那张卡片是页面上唯一的卡片，对着点不动的位置反复点（实测死循环）。

    **只报查得到的，查不到就直说查不到**。跨域 iframe 里的内容读不到
    （学习通课程列表就是），这时绝不能断言"页面只有一屏"——那是误导。
    变没变、有没有更多，最终以模型看到的画面为准。
    """
    try:
        raw = _require().eval(_PAGE_JS)
        d = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(d, dict):
            return ""
    except Exception:
        return ""

    vh = int(d.get("vh") or 0)
    h = int(d.get("h") or 0)
    y = int(d.get("y") or 0)
    frames = int(d.get("iframes") or 0)
    parts = [f"视口 {int(d.get('vw') or 0)}x{vh}px（坐标 y 最大只能填到 {max(0, vh - 10)}）"]

    if frames:
        parts.append(f"页面里有 {frames} 块内嵌框架(iframe)，它自己独立滚动、我读不到里面有多少内容"
                     f"——所以在图上看到的内容可能只是开头一段，该滚就滚，别以为到底了")
    elif h > vh + 100:
        left = max(0, h - vh - y)
        if left < 50:
            parts.append(f"页面总高 {h}px，已经滚到底了")
        else:
            parts.append(f"页面总高 {h}px，当前滚到 {y}px，下面还有 {left}px 没看到"
                         f"（要看就 scroll 正数，约 {max(1, left // _SCROLL_STEP)} 格）")
    else:
        parts.append("顶层文档只有一屏（不代表页面上没有更多内容）")

    inners = d.get("inners")
    if isinstance(inners, list) and inners:
        vw = int(d.get("vw") or 0)
        desc = []
        for it in inners[:3]:
            try:
                left = int(it.get("left") or 0)
                ix, iy = int(it.get("x") or 0), int(it.get("y") or 0)
            except (TypeError, ValueError):
                continue
            if left < 100:
                continue
            desc.append(f"({ix}, {iy}) 处那块还能滚 {left}px")
        if desc:
            parts.append("页面上有独立的滚动区（**各自滚各自的**，"
                         "要滚哪块就把 scroll 的 x/y 写到那块的坐标上）："
                         + "；".join(desc))
    return "；".join(parts)


# ---------------------------------------------------------------- 窗口

def move_out() -> None:
    """把窗口挪到屏幕外。

    **截图不受影响**：CDP 截图会强制出一帧，实测挪到屏幕外之后画面照常更新
    （滚动一下再看，截到的内容确实变了）。点击也正常，所以挪到屏幕外挂着跑
    是可行的——这也是"打游戏时后台跑"想要的效果。

    只有一件事会变：文档进入 hidden，部分页面的合成器不再活跃，滚轮事件
    可能等不到回执。这个交给 scroll() 自己处理（滚轮不通就改 scrollTop）。
    """
    hwnd = _live_hwnd()
    _restore()
    r = _rect(hwnd)
    if r is None:
        raise WindowError("读不到窗口位置（窗口可能刚被关掉），挪动失败")
    _place(hwnd, OFFSCREEN_X, OFFSCREEN_Y, r[2] - r[0], r[3] - r[1])


def move_in() -> None:
    """把窗口挪回屏幕（居中偏上）。"""
    hwnd = _live_hwnd()
    _restore()
    r = _rect(hwnd)
    if r is None:
        raise WindowError("读不到窗口位置（窗口可能刚被关掉），挪动失败")
    w = r[2] - r[0]
    _place(hwnd, max(0, (user32.GetSystemMetrics(0) - w) // 2), 60,
           w, r[3] - r[1])


def resize(width: int, height: int) -> None:
    """改窗口大小（物理像素），位置不变。"""
    hwnd = _live_hwnd()
    _restore()
    r = _rect(hwnd)
    if r is None:
        raise WindowError("读不到窗口位置（窗口可能刚被关掉），改大小失败")
    _place(hwnd, r[0], r[1], width, height)


def _restore() -> None:
    hwnd = _live_hwnd()
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        for _ in range(15):
            time.sleep(0.2)
            if not user32.IsIconic(hwnd):
                break
