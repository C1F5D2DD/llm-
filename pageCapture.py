# -*- coding: utf-8 -*-
"""截图：走浏览器调试协议（CDP）抓页面，并在图上叠坐标网格。

    import pageCapture

    pageCapture.init(pid)          # 不返回东西
    img = pageCapture.get_frame()  # 最新一帧（带网格）

为什么走 CDP 而不是 Win32 截图：Win32 的 PrintWindow 快，但**要求窗口可见**——
被遮挡或挪到屏幕外，浏览器就停止渲染，截出来是黑的或卡在旧帧。CDP 截图慢一些
（40-70ms），但完全不依赖窗口可见性，最小化也能截，这是能后台运行的前提。

坐标：图的大小就是页面视口大小，网格线的交点标着该点的页面坐标，
模型读到多少就能填多少，可以直接喂给 window_control 的 click/scroll/drag。

内部有后台线程一直在抓帧，所以 get_frame() 几乎不耗时——它只是把现成的帧给你，
不会因为截图慢把调用方（比如界面主线程）卡住。
"""
from __future__ import annotations

import base64
import io
import json
import threading
import time
import urllib.error
import urllib.request
from typing import Optional, Tuple

from PIL import Image

__all__ = ["init", "get_frame", "has_frame", "frame_age", "CaptureError"]

DEFAULT_PORT = 9222
FRAME_INTERVAL = 0.25    # 后台抓帧间隔（秒）。CDP 截图一次 40-70ms，别排太密

# 网格样式
_LINE = (255, 80, 80, 70)          # 普通网格线（半透明红）
_LINE_MAJOR = (255, 80, 80, 150)   # 每 5 格一条重线，方便估位
_LABEL_BG = (220, 30, 30, 210)
_LABEL_FG = (255, 255, 255, 255)

_font_cache: dict = {}
_grid_cache: dict = {}      # 网格层缓存：页面尺寸不变就不用重画


class CaptureError(RuntimeError):
    """连不上浏览器、截图失败之类的问题，消息里说明原因和怎么办。"""


# ---------------------------------------------------------------- 坐标网格

def _font(size: int = 12):
    from PIL import ImageFont

    f = _font_cache.get(size)
    if f is None:
        for name in ("segoeui.ttf", "arial.ttf", "msyh.ttc", "simhei.ttf"):
            try:
                f = ImageFont.truetype(name, size)
                break
            except Exception:
                continue
        if f is None:
            f = ImageFont.load_default()
        _font_cache[size] = f
    return f


def _step(size: int, want: int = 10) -> int:
    """挑一个整齐的网格间距，让画面里大约有 want 条线。"""
    raw = max(1, size // max(1, want))
    for s in (50, 100, 200, 250, 500, 1000):
        if s >= raw:
            return s
    return 1000


def _grid_layer(size: Tuple[int, int]):
    """生成网格层（带缓存）。返回的是缓存对象，调用方别改它。"""
    from PIL import Image, ImageDraw

    w, h = size
    if _grid_cache.get("key") == size:
        return _grid_cache["layer"]

    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    font = _font(12)
    step = _step(w)

    for x in range(step, w, step):
        major = (x // step) % 5 == 0
        draw.line([(x, 0), (x, h)], fill=_LINE_MAJOR if major else _LINE, width=1)
    for y in range(step, h, step):
        major = (y // step) % 5 == 0
        draw.line([(0, y), (w, y)], fill=_LINE_MAJOR if major else _LINE, width=1)

    # 数字标在线的上端/左端，标的就是这条线的页面坐标
    for x in range(step, w, step):
        s = str(x)
        tw = draw.textlength(s, font=font)
        draw.rectangle([x + 1, 1, x + tw + 5, 17], fill=_LABEL_BG)
        draw.text((x + 3, 2), s, fill=_LABEL_FG, font=font)
    for y in range(step, h, step):
        s = str(y)
        tw = draw.textlength(s, font=font)
        draw.rectangle([1, y + 1, tw + 5, y + 17], fill=_LABEL_BG)
        draw.text((3, y + 2), s, fill=_LABEL_FG, font=font)

    _grid_cache.update(key=size, layer=layer)
    return layer


def add_grid(img):
    """把坐标网格叠在图上，返回新图。

    图上标的数字就是那个位置的页面坐标——可以直接填给 window_control.click()。
    图尺寸和页面视口一一对应，不存在偏移。
    """
    base = img.convert("RGBA")
    return Image.alpha_composite(base, _grid_layer(base.size)).convert("RGB")


# ---------------------------------------------------------------- CDP

class _CDP:
    """一条 CDP 连接。页面变了或断线了，下次调用自动重连。"""

    def __init__(self, port: int) -> None:
        self.port = port
        self.ws = None
        self.msg_id = 0
        self._ws_url = None

    def _target(self) -> dict:
        """取当前活跃的标签页。

        每次都重新查——用户会手动切标签页，锁死第一次选的那个就会一直操作旧页。
        跳过 edge://newtab 这类内部页（在上面执行 JS 会失败）。
        """
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/json/list", timeout=5) as r:
                pages = [p for p in json.loads(r.read()) if p.get("type") == "page"]
        except (urllib.error.URLError, OSError) as exc:
            raise CaptureError(
                f"连不上调试端口 {self.port}：{exc}。"
                "Edge 要用 --remote-debugging-port 启动"
            ) from exc
        if not pages:
            raise CaptureError("浏览器里没有打开的页面")

        real = [p for p in pages
                if (p.get("url") or "").startswith(("http://", "https://"))]
        return real[0] if real else pages[0]

    def _connect(self) -> None:
        from websockets.sync.client import connect

        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
        want = self._target()["webSocketDebuggerUrl"]
        self.ws = connect(want, max_size=None)
        self._ws_url = want

    def call(self, method: str, params: Optional[dict] = None,
             timeout: float = 20.0) -> dict:
        from websockets.exceptions import ConnectionClosed, WebSocketException

        # 页面变了（用户切了标签页）就重连到新页面
        want = self._target()["webSocketDebuggerUrl"]
        if self.ws is None or self._ws_url != want:
            self._connect()

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
                        raise CaptureError(f"{method} 失败：{msg['error'].get('message')}")
                    return msg.get("result", {})
        except ConnectionClosed:
            self.ws = None
            self._ws_url = None
            raise CaptureError(f"{method}：连接断了，重试一次看看")
        except (TimeoutError, OSError, WebSocketException) as exc:
            raise CaptureError(f"{method}：{timeout}s 内没有响应") from exc

    def eval(self, expr: str):
        r = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return r["result"].get("value")


# ---------------------------------------------------------------- 后台抓帧

class _Grabber:
    """后台抓帧线程：定时截图加网格，把最新一帧放着等 get_frame() 来取。

    为什么必须放线程里：CDP 截图一次 40-70ms，在调用方（比如 Tk 主线程）里
    直接抓会把事件循环堵死——界面按钮点不动、窗口拖不动。放线程里，
    get_frame() 变成"取一帧已经抓好的图"，几乎零开销。
    """

    def __init__(self) -> None:
        self.thread: Optional[threading.Thread] = None
        self.stop = False
        self.frame = None
        self.frame_at: Optional[float] = None   # 这一帧是什么时候抓到的
        self.error: Optional[str] = None
        self.interval = FRAME_INTERVAL

    def start(self) -> None:
        self.stop = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def age(self) -> float:
        """当前这帧有多旧（秒）。没帧返回 0。"""
        if self.frame_at is None:
            return 0.0
        return max(0.0, time.monotonic() - self.frame_at)

    def _run(self) -> None:
        cdp = _CDP(DEFAULT_PORT)        # 独立连接，不和主线程共用一个 websocket
        while not self.stop:
            t0 = time.monotonic()
            try:
                # 截整个视口。窗口尺寸由调用方用 window_control.resize 定好，
                # 网站会按那个尺寸自动排版，所以截下来就是"小窗口里的完整页面"。
                r = cdp.call("Page.captureScreenshot",
                             {"format": "jpeg", "quality": 85})
                img = Image.open(io.BytesIO(base64.b64decode(r["data"]))).convert("RGB")

                # 关键：截出来是**物理像素**（屏幕 150% 缩放时是 CSS 的 1.5 倍），
                # 而点击用的是 CSS 像素。缩到 CSS 尺寸，图就和页面坐标一一对应了。
                vw = int(cdp.eval("innerWidth") or 0)
                vh = int(cdp.eval("innerHeight") or 0)
                if vw and vh and img.size != (vw, vh):
                    img = img.resize((vw, vh), Image.BILINEAR)

                sent = add_grid(img)
                self.frame = sent                # add_grid 每次返回新图，不用 copy
                self.frame_at = time.monotonic()  # 记下抓到的时刻，供过期判断
                self.error = None
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                time.sleep(0.5)             # 出错别空转
            dt = time.monotonic() - t0
            time.sleep(max(0.0, self.interval - dt))


_grabber: Optional[_Grabber] = None


# ---------------------------------------------------------------- 对外接口

def init(pid: int) -> None:
    """接管浏览器。失败抛 CaptureError。不返回东西——之后只跟 get_frame 打交道。

    pid 只用来确认浏览器在跑，实际通信走调试端口。
    会在后台起一个抓帧线程，所以 init 之后 get_frame() 是很快的。
    """
    global _grabber

    if not pid:
        raise CaptureError("init(pid) 需要浏览器进程的 pid")

    # 先连一次探路，连不上就早点抛错，别等后台线程反复失败
    _CDP(DEFAULT_PORT).eval("innerWidth")

    _grabber = _Grabber()
    _grabber.start()


def has_frame() -> bool:
    """有没有抓到第一帧。

    refresh 之类的循环该先问这个，别直接调 get_frame()——它在等首帧时会阻塞（最多 2 秒），
    卡住调用方（界面主线程）。

    """
    return _grabber is not None and _grabber.frame is not None


# 一帧最多允许多旧（秒）。超过这个岁数还拿不到新帧，就认为抓帧已经坏了。
# 后台线程的间隔是 0.25 秒，所以正常情况下一帧最多 0.3 秒旧；
# 给到 5 秒是留足容错（CDP 偶发卡顿、页面在忙），又不至于让调用方
# 用一张明显过时的图去做判断。
FRAME_MAX_AGE = 5.0


def get_frame():
    """拿最新一帧，返回 PIL Image。图上叠了坐标网格。

    帧是后台线程一直在抓的，所以这里几乎不耗时。刚 init 完还没抓到第一帧时，
    会等一小会儿（最多 2 秒）。

    **拿不到新帧时抛 CaptureError，绝不返回过时的旧帧。**
    这一点很关键，踩过一个很隐蔽的坑：原来的错误检查写在
    `while _grabber.frame is None` 循环里面，所以只在"还没有第一帧"时生效。
    一旦抓到过一帧，后面抓帧再失败都会静默返回那张冻结的旧图——
    调用方毫不知情，把同一张过时画面反复喂给模型，模型就永远看到同一个页面、
    反复做同一个动作，输出还逐字相同（实测：同一个坐标点了 41 次，
    日志里 4 次原始返回一模一样，排查了很久才发现是图冻住了）。
    """
    if _grabber is None:
        raise CaptureError("还没连接，先调用 init(pid)")

    deadline = time.monotonic() + 2.0
    while _grabber.frame is None:
        if _grabber.error:
            raise CaptureError(f"抓帧失败：{_grabber.error}")
        if time.monotonic() > deadline:
            raise CaptureError("等了 2 秒还没抓到画面，检查浏览器是不是卡住了")
        time.sleep(0.05)

    # 已经有帧了，但要确认它是**新鲜的**——见上面那段说明
    age = _grabber.age()
    if _grabber.error and age > FRAME_MAX_AGE:
        raise CaptureError(
            f"抓帧已经停了 {age:.0f} 秒（最后一帧是 {age:.0f} 秒前的），"
            f"不能再拿这张过时的图去判断页面：{_grabber.error}")

    return _grabber.frame


def frame_age() -> float:
    """当前这帧有多旧（秒）。界面可以拿它提示"画面是不是卡住了"。"""
    if _grabber is None:
        return 0.0
    return _grabber.age()
