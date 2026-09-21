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

__all__ = ["init", "get_frame", "snapshot", "has_frame", "frame_age",
           "add_grid", "annotate", "CaptureError"]

DEFAULT_PORT = 9222
FRAME_INTERVAL = 0.25    # 后台抓帧间隔（秒）。CDP 截图一次 40-70ms，别排太密

# 网格样式
_LINE = (255, 80, 80, 42)          # 细线（20px）：很淡，只用来数格子
_LINE_MAJOR = (255, 80, 80, 130)   # 主格线（100px）：深一点，好定位
_LABEL_BG = (220, 30, 30, 210)
_LABEL_FG = (255, 255, 255, 255)

# 网格疏密。**这两个数字是踩坑定下来的**：
# 早先按视口宽度算间距（1359px 的窗口会选 200px），模型得在 200x200 的大格子里
# 靠肉眼估位置，实测反复算错坐标——点 (200,537) 点了五六次都没中，
# 它还以为是自己点得不够准、或者页面没反应。
# 现在细线固定 20px：模型不用估，数格子就行（最多数 5 格到下一个数字）。
# 数字保持 100px 一个：每 20px 都标数字的话，三位数会糊成一片、把页面内容全挡住。
GRID_STEP = 20         # 细网格间距（像素）
LABEL_STEP = 100       # 每多少像素标一个数字

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


def _grid_layer(size: Tuple[int, int]):
    """生成网格层（带缓存）。返回的是缓存对象，调用方别改它。

    两层线：20px 的细线（数格子用）+ 100px 的主格线（上面标数字）。
    模型只要从最近的数字往两边数几格，就能读出精确坐标，不用估。
    """
    from PIL import Image, ImageDraw

    w, h = size
    if _grid_cache.get("key") == size:
        return _grid_cache["layer"]

    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    font = _font(12)

    # 细线：跳过会被主格线覆盖的位置
    for x in range(GRID_STEP, w, GRID_STEP):
        if x % LABEL_STEP:
            draw.line([(x, 0), (x, h)], fill=_LINE, width=1)
    for y in range(GRID_STEP, h, GRID_STEP):
        if y % LABEL_STEP:
            draw.line([(0, y), (w, y)], fill=_LINE, width=1)

    # 主格线
    for x in range(LABEL_STEP, w, LABEL_STEP):
        draw.line([(x, 0), (x, h)], fill=_LINE_MAJOR, width=1)
    for y in range(LABEL_STEP, h, LABEL_STEP):
        draw.line([(0, y), (w, y)], fill=_LINE_MAJOR, width=1)

    # 数字标在线的上端/左端，标的就是这条线的页面坐标
    for x in range(LABEL_STEP, w, LABEL_STEP):
        s = str(x)
        tw = draw.textlength(s, font=font)
        draw.rectangle([x + 1, 1, x + tw + 5, 17], fill=_LABEL_BG)
        draw.text((x + 3, 2), s, fill=_LABEL_FG, font=font)
    for y in range(LABEL_STEP, h, LABEL_STEP):
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


# ---------------------------------------------------------------- 可点元素编号
#
# 为什么要有这个：让多模态模型看着截图数网格线、估出一个小元素的中心，
# **精度根本不够**。实测点「章节测验」标签（真实中心 214）读成 280，偏 66px
# 点到隔壁去了，它还以为是"页面没反应"，接着换了三个 x 又全落在同一行上打转。
# 提示词从"照网格读数"一路改到"量左右边界取中点"，都治不了——这是模型看图
# 估算的精度上限，不是它不认真。
#
# 改成：程序把页面上所有可点的东西**枚举出来、编号、画在图上**，
# 模型只要回一个编号，坐标由代码去 DOM 里查（准的）。
# 模型擅长"这是什么、该点哪个"这类判断，不擅长毫米级的空间定位——
# 那就别让它做后者的活。

# 收集可点元素的脚本。**要注意三件事**：
#
# 1. 坐标要加上 iframe 的偏移。学习通的播放器、目录都在 iframe 里，
#    iframe 内部的 getBoundingClientRect 是相对它自己视口的，不加偏移就整体错位。
# 2. **嵌套元素要去重**。一个「目录」标签往往被 li > div > span 三层都命中，
#    三层都 clickable，清单里就会冒出三个几乎一样的项，模型选编号时看花眼。
# 3. 只要**完全在视口内**的元素——滚动之后顶部那些 y 为负的还在 DOM 里，
#    编上号会让模型点到一个看不见的地方。
_ELEMENTS_JS = """(() => {
  const VW = window.innerWidth, VH = window.innerHeight;
  const cand = [];
  const TAGS = ['A','BUTTON','INPUT','SELECT','TEXTAREA','LABEL','SUMMARY'];

  function textOf(el) {
    let t = (el.innerText || el.value || el.getAttribute('aria-label')
             || el.getAttribute('title') || el.getAttribute('placeholder') || '');
    return String(t).replace(/\\s+/g, ' ').trim().slice(0, 30);
  }

  function clickable(el) {
    try {
      if (TAGS.indexOf(el.tagName) >= 0) return true;
      if (el.getAttribute('onclick')) return true;
      const role = el.getAttribute('role');
      if (role === 'button' || role === 'link' || role === 'tab' || role === 'checkbox') return true;
      return window.getComputedStyle(el).cursor === 'pointer';
    } catch (e) { return false; }
  }

  function collect(doc, ox, oy, depth) {
    if (!doc || depth > 3) return;
    let list;
    try { list = doc.querySelectorAll('*'); } catch (e) { return; }
    for (let i = 0; i < list.length; i++) {
      const el = list[i];
      try {
        const r = el.getBoundingClientRect();
        // 太小点不准、太大的是整块容器（不是"一个目标"）
        if (r.width < 10 || r.height < 10) continue;
        if (r.width > 900 || r.height > 620) continue;
        if (!clickable(el)) continue;
        const cs = window.getComputedStyle(el);
        if (cs.visibility === 'hidden' || cs.display === 'none') continue;
        if (parseFloat(cs.opacity || '1') < 0.15) continue;
        const L = r.left + ox, T = r.top + oy, R = r.right + ox, B = r.bottom + oy;
        // 完全在视口内才编号：露出一半的，点它有一半概率点空
        if (L < -2 || T < -2 || R > VW + 2 || B > VH + 2) continue;
        cand.push({t: textOf(el), tag: el.tagName,
                   l: L, ty: T, w: r.width, h: r.height});
      } catch (e) {}
    }
    let frames;
    try { frames = doc.querySelectorAll('iframe'); } catch (e) { return; }
    for (let i = 0; i < frames.length; i++) {
      try {
        const f = frames[i], ir = f.getBoundingClientRect();
        if (f.contentDocument) collect(f.contentDocument, ox + ir.left, oy + ir.top, depth + 1);
      } catch (e) {}
    }
  }

  collect(document, 0, 0, 0);

  // 去重：位置范围重合、互相包含的，只留"最具体"的那个。
  // 判据是文字更短更专指（li 的文字通常是 "1.2 xxx yyy"，里面 span 才是 "1.2"），
  // 都没文字就留面积小的。
  const keep = [];
  for (const c of cand) {
    let hit = -1;
    for (let j = 0; j < keep.length; j++) {
      const k = keep[j];
      const near = Math.abs(k.l - c.l) < 8 && Math.abs(k.ty - c.ty) < 8
                && Math.abs(k.w - c.w) < 16 && Math.abs(k.h - c.h) < 16;
      const inside = c.l >= k.l - 8 && c.ty >= k.ty - 8
                  && c.l + c.w <= k.l + k.w + 16 && c.ty + c.h <= k.ty + k.h + 16;
      if (near || inside) { hit = j; break; }
    }
    if (hit < 0) { keep.push(c); continue; }
    const k = keep[hit];
    const better = c.t && (!k.t || c.t.length < k.t.length);
    const bothEmpty = !k.t && !c.t && c.w * c.h < k.w * k.h;
    if (better || bothEmpty) keep[hit] = c;
  }

  keep.sort((a, b) => (a.ty - b.ty) || (a.l - b.l));
  const out = keep.slice(0, 90).map(c => ({
    t: c.t, tag: c.tag,
    x: Math.round(c.l + c.w / 2), y: Math.round(c.ty + c.h / 2),
    l: Math.round(c.l), ty: Math.round(c.ty),
    w: Math.round(c.w), h: Math.round(c.h),
  }));
  return JSON.stringify(out);
})()"""

# 编号样式
_BADGE_BG = (20, 110, 220, 235)      # 蓝底，和红色网格区分开
_BADGE_FG = (255, 255, 255, 255)


def _draw_badges(img, elements):
    """把编号画在图上，返回新图。

    编号框贴在元素的左上角——放中心会挡住元素本身，模型就看不清它是什么了。
    """
    if not elements:
        return img
    from PIL import ImageDraw

    out = img.convert("RGBA")
    layer = Image.new("RGBA", out.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    font = _font(12)

    for i, el in enumerate(elements, start=1):
        label = str(i)
        tw = draw.textlength(label, font=font)
        bw, bh = tw + 8, 16
        x = max(0, min(out.size[0] - bw, int(el.get("l", 0))))
        y = max(0, min(out.size[1] - bh, int(el.get("ty", 0))))
        draw.rectangle([x, y, x + bw, y + bh], fill=_BADGE_BG)
        draw.text((x + 4, y + 2), label, fill=_BADGE_FG, font=font)

    return Image.alpha_composite(out, layer).convert("RGB")


def annotate(img, elements):
    """给图叠上"网格 + 元素编号"。"""
    return _draw_badges(add_grid(img), elements)


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
        self.elements: list = []                # 这一帧上各编号对应的元素
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
                # 1) 先枚举可点元素（编号要画在图上，得先知道有哪些）
                try:
                    raw = cdp.eval(_ELEMENTS_JS)
                    elements = json.loads(raw) if isinstance(raw, str) else (raw or [])
                    if not isinstance(elements, list):
                        elements = []
                except Exception:
                    elements = []       # 枚举失败不致命，这一帧就没有编号

                # 2) 截整个视口
                r = cdp.call("Page.captureScreenshot",
                             {"format": "jpeg", "quality": 85})
                img = Image.open(io.BytesIO(base64.b64decode(r["data"]))).convert("RGB")

                # 关键：截出来是**物理像素**（屏幕 150% 缩放时是 CSS 的 1.5 倍），
                # 而点击用的是 CSS 像素。缩到 CSS 尺寸，图就和页面坐标一一对应了。
                vw = int(cdp.eval("innerWidth") or 0)
                vh = int(cdp.eval("innerHeight") or 0)
                if vw and vh and img.size != (vw, vh):
                    img = img.resize((vw, vh), Image.BILINEAR)

                # 3) 叠网格 + 编号。元素清单和这一帧是配套的，一起存。
                sent = annotate(img, elements)
                self.frame = sent
                self.elements = elements
                self.frame_at = time.monotonic()
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


def snapshot():
    """一次拿到**配套的**（图, 元素清单）。

    图和编号必须来自同一帧：图上是第 7 号，清单里的第 7 项也得是同一个元素，
    否则模型说"点 7 号"就会点到别处。所以别分别调 get_frame() 和 elements()，
    用这个原子接口——后台线程每 0.25 秒就换一帧，分两次取中间可能被换掉。

    返回 (PIL.Image, list)。元素是 dict，含 t(文字)/x/y(中心)/w/h/l/ty。
    返回的清单是**副本**，调用方随便用。
    """
    img = get_frame()          # 顺带做了过期检查，拿不到会抛
    if _grabber is None:
        return img, []
    elems = _grabber.elements or []
    # 深拷贝一层：里面的 dict 会被调用方读，别和后台线程共享同一个对象
    return img, [dict(e) for e in elems if isinstance(e, dict)]
