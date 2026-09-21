# -*- coding: utf-8 -*-
"""决策：把截图交给多模态模型，拿回一个动作。

    import brain

    brain.init(base_url="https://api.xxx.com/v1", api_key="sk-...",
               model="qwen-vl-plus")
    d = brain.decide(img, goal="进入我的课程列表")
    print(d.thought, d.action)
    # action 形如 {"type": "click", "x": 400, "y": 300}

坐标口径：截图上的刻度值（和 pageCapture 画的刻度一致），模型读到多少就填多少，
可以直接喂给 window_control 的 click/scroll/drag。

对话历史由模块自己维护——decide() 会自动把上一步的动作和结果带进上下文，
换任务时调 reset() 清空。
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional

from PIL import Image

__all__ = ["init", "decide", "reset", "note_result", "note_hint", "turn_elapsed",
           "save_config", "load_config",
           "BrainError", "Decision", "ACTIONS", "LOG_PATH"]

# 允许模型输出的动作类型。和执行层（window_control）的能力一一对应。
# reply 不操作页面，只是回一句话给人——用户问"你刚才做了什么""复述一下我的要求"
# 这类问题时，模型原来没有任何可用动作，只能把话塞进 thought 然后继续操作页面，
# 看着就像"听不懂人话"（实测踩过）。
ACTIONS = ("click", "scroll", "drag", "type", "press", "wait", "reload",
           "finish", "ask_human", "reply")

# 只保存一套 API 配置，存在项目目录下。里面有 api_key，别提交到版本库。
CONFIG_PATH = Path(__file__).resolve().parent / "brain.config.json"

# ---------------------------------------------------------------- 日志
# 模型返回什么、解析成什么，全记到 runs/brain.log。排查"返回不是合法 JSON"
# 这种问题只能靠原始文本，光看报错信息没用（已经把完整内容写进去了）。
LOG_PATH = Path(__file__).resolve().parent / "runs" / "brain.log"
_log = logging.getLogger("autoxxt.brain")
_log_ready = False


def _ensure_log() -> None:
    """第一次用到日志时挂上文件输出。控制台也留一份，方便前台调试。"""
    global _log_ready
    if _log_ready:
        return
    _log_ready = True
    _log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S")

    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
        fh.setFormatter(fmt)
        _log.addHandler(fh)
    except Exception:
        pass      # 写不了文件就算了，别因为日志把主流程搞挂

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    _log.addHandler(sh)


_ensure_log()      # 模块加载就挂上，这样 save_config 这些接口也都有日志

MAX_HISTORY = 12        # 上下文里保留最近多少步（文本形式，不带历史图）

# 下面这段提示词里写了"单次等待上限 600 秒""一个回合 50 步"——
# 这两个数字来自 run.py 的 `_wait`（MAX_WAIT）和 `MAX_STEPS`。
# 改了那边记得回来改这里，否则模型会按错的预算规划等待。
SYSTEM_PROMPT = """你是操作浏览器页面的助手。每次你会看到一张当前页面的截图，
需要输出**一个**动作，由程序替你真实执行。

截图说明：
- 图上叠了一层半透明红色网格，格线和数字都是**标尺**，不遮挡判断。
- 网格上的数字就是那个位置的坐标（页面像素，原点在视口左上角）。
  要点击某个点，照网格读出它的 x、y 即可——**看到多少就填多少，不用做任何加减**。
- **图里只有当前视口这一屏**。页面通常比一屏长得多，你看到的内容可能只是顶部一小部分。
  要找的东西不在图里时，**先 scroll**（正数向下滚），等下一屏再看，别去猜坐标瞎点。
  提示里会尽量告诉你当前滚动到哪、总共多长；但它也可能查不到（比如内容在跨域
  内嵌框架里），查不到时它会直说——**别把"没告诉我还有更多"当成"没有更多"**。
- **页面里各块区域是各滚各的**。左边内容区和右边目录/侧栏往往是两个独立的
  滚动容器（学习通就是这样）：只写 `{"type":"scroll","notches":3}` 是滚不到侧栏的，
  它会去滚整页，而侧栏一动不动。
  想滚某块区域，就把指针放到**那块区域上**再滚：`{"notches":3, "x":900, "y":400}`，
  x/y 填那块区域里的任意一点（照网格读数）。
  "页面状况"里会列出页面上还有哪几块能滚、各自在哪，优先照它给的坐标来。
- **从第二步起，每步会给你两张图**：上一屏（执行上一个动作之前）和当前屏。
  判断"上一步到底生效没有"是**你的事**——自己对比这两张图。
  另外会给一些原始数据（地址、滚动位置、标签页数）作参考，
  但**这些数据很不全**：页面内部区域的滚动、跨域 iframe 里的滚动、弹窗浮现、
  按钮变灰、视频开始播……它们统统看不出来。数据说"没变"时，**以你眼睛看到的为准**。

输出格式（**严格遵守，多余的字符都会让整个动作作废**）：
- 只输出一个 JSON 对象，不要解释文字、不要代码块标记、不要 <think> 之类的思考标签。
- JSON 里每个值只写一对引号，别多写——`"thought":"看这里"` 是对的，
  `"thought":" "看这里"`（多一个引号）会让整段无法解析，你的动作就白想了。
- 格式如下：
{"thought": "一句话说明你看到了什么、要做什么", "action": {动作}}

可用动作：
- {"type": "click", "x": 400, "y": 300}                  点击
- {"type": "scroll", "notches": 3}                       滚轮，正数向下，负数向上（一"格"约 100px）。默认滚整页
- {"type": "scroll", "notches": 3, "x": 900, "y": 400}   滚**指定的那块区域**（见下方说明）
- {"type": "drag", "x1": 100, "y1": 500, "x2": 400, "y2": 500}   拖动（滑块、进度条）
- {"type": "type", "text": "要输入的内容"}                在当前焦点处输入文字
- {"type": "press", "key": "Enter"}                      按键（Enter/Tab/Escape/方向键，也支持 Control+a 这种组合）
- {"type": "wait", "seconds": 5}                         等待，给页面反应时间（单次上限 600 秒）
- {"type": "reload"}                                     刷新当前页面（见下方说明）
- {"type": "finish", "reason": "说明"}                   目标已完成，本回合结束
- {"type": "ask_human", "question": "要人做什么"}         卡住了/需要人处理（登录、验证码、拿不准）
- {"type": "reply", "text": "要说的话"}                   **回话给人**，说完本回合结束，等他再说

什么时候用 reply：
- 人问你问题（"刚才干了什么""我的要求有哪些""这个页面是什么"）→ 用 reply 回答，
  答案写在 text 里。**不要**把答案写在 thought 里然后继续操作页面——那样人看不到你在回答他。
- 人让你"停下""等一下""先别动"这类要求 → 用 reply 应一声（例如"好，已停手，视频仍在播，
  进度 3:20/9:33"），然后**结束回合等他**，不要自顾自继续。
- 只是汇报进度、但没有需要继续做的事时 → 也用 reply。
用 reply 之后回合就结束了，所以**该继续干活时不要用它**。

看视频 / 长等待（网课、剧集这类"要等它播完"的任务）：
- **"页面状况"里会直接告诉你视频的真实状态**（例如「视频正在播放，进度 7:22 / 33:00；
  距播完还需约 25 分 38 秒」）。这是程序从浏览器里读出来的**准确事实，以它为准**，
  别靠看图猜。
- **播放器正中央那个圆钮是"播放/暂停"开关**：正在播的时候是暂停图标，
  暂停的时候是播放图标——两者在截图里几乎一样。**看到视频已经在播，就绝对不要再去点它**，
  一点就把视频停了（实测踩过：模型报了"视频已开始播放"，其实刚被自己点停，
  然后白等 10 分钟）。
- 上面这条是**自动播放**时的规矩：视频在播、目标又是"看完它"，那就接着 wait。
  但**人一旦说话，一切以人为准**——他说停就停（用 reply 应一声然后结束回合），
  说看这个视频就别去点别的任务点。规则是给"没人管的时候"用的，
  不要拿规则去顶人的指令。
- 状态是"已暂停"时才需要点播放键。点完之后必须 用短等待（几秒）反复核实状态真的变成
  "正在播放"了再进长等待；只看一眼截图不算数。
- 播放正常时**每次都把等待拉满**（单次上限 600 秒，也就是 10 分钟），别用 3、5 秒的小碎步——
  那是白耗步数。只有"页面在加载"这种几秒就好转的情况才用短等待。
- **每次等完，先看进度数字有没有往前走**：
  - 往前走了 → 正常，接着等下一次。
  - 没往前走 → 视频卡住了或播放出错（弹窗、需要点"继续"），**立刻停止等待去处理**，
    不要继续干等下去。
- 一个回合总步数有限。视频比这还长的话本来就等不完：把当前进度写进 finish，
  让人再发一句接着跑，别硬等。

任务点 / 学习进度：
- **"页面状况"里会列出哪些任务点已完成、哪些还没做**，那是程序从页面里读出来的
  准确事实，**以它为准**。目录里的完成标记在截图里只是个小图标，你逐条认容易看漏。
- **已经完成的任务点绝对不要去点**，那是在浪费时间、而且会把课程进度搞乱。
  要做就做"还没完成"里最靠前的那一项。
- 同一项做完之后（视频播完、测验提交），下一步应该去**下一项没完成的**，
  不要回头再点刚做完的。

什么时候该刷新页面（reload）：
- **页面卡住了**：进度数字不再往前走、按钮点了没反应、内容转圈不出来
  —— 刷新往往比反复点更管用。
- **页面上的东西不对/不全**：某块内容空白、列表比预期少、样式乱掉。
- **提交之后状态没更新**：测验交了、任务点做完了，但目录上的完成标记没变。
- 刷新会丢掉页面上没保存的东西（填了一半的答案、播放到一半的进度在少数网站上
  也会回退），所以**只在上面这些情况用**，别没事就刷。
  视频正在正常播放时尤其不要刷新——那会把进度清零、白等一场。
- 刷新之后要 wait 一会儿等页面加载回来，别立刻就去点。

规则：
0. **人的话（"人工指令"）优先级最高，高于这里所有规则，也高于目标本身。**
   冲突时听人的：让你停就停、让你看这个就看这个、别去碰别的任务点。
   人的指令需要回应时用 reply，别只在 thought 里嘀咕一句就继续干自己的。
   （踩过：人连说两次"停下看这个视频、别重复完成任务点"，模型两次都回了句
   "人工指令要求停下…"然后继续等待——因为它把下面的规则当成了不能违反的硬规矩。）
1. 一次只做一个动作。
2. 坐标要落在目标元素的中心，别压在边缘。**不要把坐标点到视口最底部**（y 接近 700 那里
   往往是别的元素），要点的东西在下面就先滚动。
3. **每步对比两屏图，自己判断上一步生效没有**。程序给的那几个数字只是参考。
4. **同一个动作绝对不能输出两次**。如果对比下来画面没变化，说明那个动作没用，
   这次必须换个完全不同的做法：改滚动（换滚动位置、换滚动区）、改坐标
   （换到元素真正的中心）、刷新、或者 ask_human。但是如果时间再深夜，就绝对不要ask_human，先换个动作试试。
5. 连续 2 次没效果就 ask_human，别硬试到步数上限。
6. 遇到登录页、验证码、滑块验证、短信验证，一律 ask_human，不要自己乱试。
7. 不要做不可逆操作（提交、发布、删除、支付、退出登录），除非目标明确要求。
8. 页面还在加载（空白、转圈）时就 wait，别乱点。
9. 务必非常非常仔细地核实视频是否开始.如果没有开始就进行长等待，会白白浪费相当多的时间。一定要进行至少3次短等待核验，完全确定视频开始播放再进入长等待。
10. **用时间判断该不该继续等**：每步都会告诉你当前时间和距上一步过了多久，
    历史里每条前面也有 [时:分:秒]。据此算清楚"这件事已经耗了多久"——
    如果同一件事反复没进展、累计已经过去很久（比如等了好几轮、加起来超过十几分钟
    页面还是老样子），**不要再机械地等下去**：该刷新（reload）、该换办法、
    或者 ask_human 让人看看。别把"再等一次"当成默认选项。
11. 历史里可能出现「（程序提醒）你已经把某个动作做了 N 次」。**这时要自己判断**：
    如果每次都能看到新东西（列表在往上翻、题目变了、进度在走），那重复是正常的，
    接着做；如果画面一直没变、或者已经到底了，就立刻换做法。
    别无视这个提醒继续机械重复——提醒过后还照旧，程序会把回合强制停掉。
12. thought 保持一句话。"""


class BrainError(RuntimeError):
    """配置缺失、调用失败、返回格式不对之类的问题。"""


class Decision(NamedTuple):
    """一次决策的结果。"""

    thought: str
    action: Dict[str, Any]

    def describe(self) -> str:
        return f"{self.thought} → {self.action}"


# ---------------------------------------------------------------- 状态

_client = None
_model = ""
_options: Dict[str, Any] = {}
_history: List[Dict[str, str]] = []

# 时间线。**模型自己看不到时钟**——不给它时间，"再等 10 分钟"这种决策就没有参照：
# 它不知道已经等了几轮、也不知道距上一步过了多久，于是一轮接一轮地重复等待，
# 每次都以为是第一次（实测：连续两轮各等 580 秒，中间被人打断，模型毫无察觉）。
_last_step_at: Optional[float] = None     # 上一步发生的时刻（time.time()）
_turn_started: Optional[float] = None     # 本段对话的起点，用来算"一共跑了多久"

# 上一步那张截图（data URI）。每步连同当前屏一起发给模型，
# 让它自己对比两屏来判断动作有没有生效——这是模型的判断，不该由程序代劳：
# 程序能查的只有地址/滚动位置/标签页数这几项，页面内部滚动、弹窗浮现、
# 按钮变灰这些变化它一概看不见，硬下结论只会误导（实测把它当成"没变化"
# 去告诉模型，模型就反复重试同一个动作）。
_last_frame: Optional[str] = None


def save_config(base_url: str, api_key: str, model: str) -> Path:
    """把这套 API 配置存下来，下次启动自动读。

    三项都必须给，不然存了也没用。文件里含 api_key，注意别提交到版本库
    （.gitignore 里已经排除）。
    """
    if not (base_url and api_key and model):
        raise BrainError("三项都要填才能保存")

    data = {
        "base_url": base_url.strip(),
        "api_key": api_key.strip(),
        "model": model.strip(),
    }
    try:
        CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except OSError as exc:
        raise BrainError(f"保存失败：{exc}") from exc
    _log.info("配置已保存到 %s", CONFIG_PATH)
    return CONFIG_PATH


def load_config() -> Optional[Dict[str, str]]:
    """读回保存的配置。没存过或文件坏了就返回 None（不报错，当没配就行）。"""
    if not CONFIG_PATH.exists():
        return None
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("读配置失败（%s），当成没配过", exc)
        return None

    if not isinstance(data, dict):
        return None
    got = {k: str(data.get(k, "")) for k in ("base_url", "api_key", "model")}
    if not all(got.values()):
        return None
    return got


def init(base_url: str, api_key: str, model: str,
         temperature: float = 0.0, max_tokens: int = 800,
         timeout: float = 90.0, json_mode: bool = True) -> None:
    """配置模型并接管。失败抛 BrainError。

    base_url/api_key/model   OpenAI 兼容网关的三项配置
    json_mode                True 时要求网关返回 JSON 对象；某些网关不支持，
                             报错了就传 False（这时靠提示词约束 + 容错解析）

    **不会清空对话历史**——想重开一段对话请显式调 reset()。
    早先这里末尾会 reset()，而调用方每发一句话都要重新 init 一次，
    结果模型每轮都从零开始、完全不记得上一轮干了什么（踩过）。
    """
    global _client, _model, _options

    if not (base_url and api_key and model):
        raise BrainError("需要 base_url、api_key、model 三项")

    from openai import OpenAI      # 延迟导入：没配模型时也能用别的模块

    _ensure_log()
    base_url = _clean_base_url(base_url)
    _client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
    _model = model
    _options = {
        "temperature": temperature,
        "max_tokens": max_tokens,
        "json_mode": json_mode,
    }
    _log.info("配置模型：%s @ %s   json_mode=%s   日志→%s",
              model, base_url, json_mode, LOG_PATH)


def _clean_base_url(url: str) -> str:
    """把 base_url 收拾成 OpenAI 库要的样子。

    **最容易踩的坑是末尾多带 `/chat/completions`**：各家文档到处都在给完整接口
    地址，复制粘贴就中招。而 OpenAI 库会自己再拼一次，于是请求打到
    `/v4/chat/completions/chat/completions` 上——服务端 404，报错信息里
    只写 "Not Found"，完全看不出是自己多填了一段（踩过）。
    """
    url = url.strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/v1/chat"):
        if url.endswith(suffix):
            fixed = url[: -len(suffix)].rstrip("/")
            _log.warning("base_url 末尾多了 %r，已自动去掉：%s -> %s",
                         suffix, url, fixed)
            url = fixed
            break
    return url


def reset() -> None:
    """清空对话历史。换任务时调用，免得模型被上一个任务的上下文带偏。"""
    global _last_frame, _last_step_at, _turn_started
    _history.clear()
    _last_frame = None      # 新任务的第一步不该看到上一个任务留下的画面
    _last_step_at = None
    _turn_started = None    # 时间线也重开，免得它拿旧对话的时间做判断


def decide(img: Image.Image, goal: str, extra: str = "", page: str = "") -> Decision:
    """看着当前画面决定下一步做什么。

    img    当前截图（pageCapture.get_frame() 的产物，带刻度）
    goal   这一步要达成的目标，自然语言
    extra  人工插话，会追加到提示里（优先级高于目标）
    page   页面尺寸/滚动位置等原始事实（run.py 从浏览器里查的），比如
           "视口 1080x720，页面总高 2960px，已滚到 680px，下面还有 1560px"。
           截图只能看到视口这一屏，没有这行模型就不知道下面还有东西，
           会对着"屏幕上看得见但其实不是目标"的元素反复点（实测死循环）。
           **这些只是参考数据，变没变由模型对比两屏自己判断。**

    为了让模型能判断上一步有没有生效，这里会把**上一次的截图一起发过去**，
    模型自己对比"上一屏 vs 当前屏"。程序不替它下"页面有没有变化"的结论——
    那需要穷举所有变化形式，而程序只查得到地址/滚动/标签页数几项，必然漏。

    失败抛 BrainError。
    """
    global _last_frame, _turn_started

    if _client is None:
        raise BrainError("还没配置，先调用 init(base_url, api_key, model)")

    now = time.time()
    if _turn_started is None:
        _turn_started = now      # 本段对话的第一步，记下来算总时长
    # 注意：**这里不要动 _last_step_at**。它在 note_result() 里更新（动作执行完的时刻）。
    # 早先在这里也赋了一次，结果每次构建提示词前刚把"上一步时刻"改成当前时刻，
    # "距上一步已过 N 秒"永远是 0——时间信息形同虚设（实测踩过）。

    _ensure_log()
    data_uri = _to_data_uri(img)
    text = _build_prompt(goal, extra, page, has_prev=_last_frame is not None)
    _log.debug("请求：goal=%r extra=%r page=%r 图=%s 上一屏=%s 历史=%d 条",
               goal, extra, page, img.size, "有" if _last_frame else "无", len(_history))

    # 先给上一屏、再给当前屏，中间用文字说清楚哪个是哪个
    parts: List[Dict[str, Any]] = [{"type": "text", "text": text}]
    if _last_frame:
        parts.append({"type": "text", "text": "上一屏（执行上一个动作**之前**的画面）："})
        parts.append({"type": "image_url", "image_url": {"url": _last_frame}})
        parts.append({"type": "text", "text": "当前屏幕（刚刚截的）："})
    parts.append({"type": "image_url", "image_url": {"url": data_uri}})

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *_history,                                        # 之前几步的文本记录
        {"role": "user", "content": parts},
    ]

    reply = _chat(messages)
    decision = _parse(reply)

    # 记进历史。两条注意：
    #
    # 1. **不要存 page**。page 是"当下这一屏"的事实（视口、滚动位置、视频状态、
    #    任务点清单…），历史里那份早就过时了，没有任何参考价值——但它很长
    #    （实测一条 400+ 字，光"还没完成 82 项：1.1 …、1.2 …"就能占几百 token）。
    #    存进去的话，8 步历史就是 8 份重复的静态数据，把真正要紧的东西
    #    （上一步干了什么、结果如何）淹在噪音里。实测模型因此发现不了自己在
    #    重复同一个动作，同一个坐标点了 41 次（输入 token 飙到 8212，大半是它）。
    #
    # 2. 每条前面加 [时:分:秒]：模型看不到时钟，不给时间戳它就不知道上一步是
    #    多久以前的，也就没法判断"是不是卡太久了"。
    hist = f"[{_hhmmss(now)}] 目标：{goal}"
    if extra:
        hist += f"\n人工指令：{extra}"
    _history.append({"role": "user", "content": hist})
    _history.append({"role": "assistant",
                     "content": f"[{_hhmmss(now)}] " + json.dumps(
                         {"thought": decision.thought, "action": decision.action},
                         ensure_ascii=False)})
    del _history[:-MAX_HISTORY * 2]

    _last_frame = data_uri      # 这一步的屏幕，成为下一步的"上一屏"
    return decision


def note_result(result: str) -> None:
    """把上一步动作的执行结果告诉模型。

    **必须调**，否则模型看不见自己动作的效果，会一直重复同一个动作
    （实测：日志里连续十几步输出完全一样的 click，撞到步数上限才停）。

    调用方在每次 decide() 执行完动作后调一次，把结果描述传进来，比如
    "已点击 (300, 700)" 或 "执行失败：坐标超出范围"。
    """
    global _last_step_at
    if not result:
        return
    now = time.time()
    _last_step_at = now          # 动作执行完的时刻，下一步据此算间隔
    _history.append({"role": "user",
                     "content": f"[{_hhmmss(now)}] （上一步的执行结果）{result}"})
    del _history[:-MAX_HISTORY * 2]
    _log.debug("记下执行结果：%s", result)


def note_hint(text: str) -> None:
    """程序给模型塞一句提醒（不是动作结果，是程序的判断）。

    用途：某个动作重复太多次时提醒它"是不是卡住了"。**只能提醒、不能替它停**——
    "重复多少次算太多"跟具体任务有关（翻长列表滚 30 次很正常，点同一个按钮
    3 次就可疑），程序分不清，得让模型结合画面自己判断。
    """
    if not text:
        return
    now = time.time()
    _history.append({"role": "user", "content": f"[{_hhmmss(now)}] （程序提醒）{text}"})
    del _history[:-MAX_HISTORY * 2]
    _log.info("给模型的提醒：%s", text)


# ---------------------------------------------------------------- 内部

def _to_data_uri(img: Image.Image, quality: int = 80) -> str:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _hhmmss(t: float) -> str:
    """给人/模型看的时刻。"""
    return time.strftime("%H:%M:%S", time.localtime(t))


def _gap_text(seconds: float) -> str:
    """把秒数说成"1 分 30 秒"这种，别让模型自己去换算。"""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    if seconds < 3600:
        return f"{seconds // 60:.0f} 分 {seconds % 60:.0f} 秒"
    return f"{seconds // 3600:.0f} 小时 {seconds % 3600 // 60:.0f} 分"


def turn_elapsed() -> float:
    """这段对话一共跑了多少秒。还没开始过就返回 0。

    给 run.py 用：提醒模型"这件事已经耗了多久"时要报这个数。
    """
    if not _turn_started:
        return 0.0
    return max(0.0, time.time() - _turn_started)


def _time_brief() -> str:
    """当前时间 + 距上一步多久 + 这段对话一共跑了多久。"""
    now = time.time()
    parts = [f"现在时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}"]
    if _last_step_at:
        parts.append(f"距上一步（{_hhmmss(_last_step_at)}）已过 {_gap_text(now - _last_step_at)}")
    if _turn_started:
        parts.append(f"这段对话从 {_hhmmss(_turn_started)} 开始，"
                     f"至今 {_gap_text(now - _turn_started)}")
    return "；".join(parts)


def _build_prompt(goal: str, extra: str, page: str = "", has_prev: bool = False) -> str:
    parts = [f"目标：{goal}"]
    if extra:
        # 带上你说这话的时刻：模型据此判断"这是刚说的、还是十分钟前说的"，
        # 后者意味着页面状态可能早就变了
        parts.append(f"人工指令（优先服从，{_hhmmss(time.time())} 说的）：{extra}")
    # 时间放在页面状况前面：模型做"还要等多久""是不是卡太久了"这类判断时
    # 靠的就是它，别埋在长串页面数据后面
    parts.append(_time_brief())
    if page:
        parts.append(f"页面状况（参考数据，不全，别只看它）：{page}")
    if has_prev:
        parts.append("下面两张图：先上一屏、后当前屏。**自己对比**，判断上一个动作有没有生效。")
    else:
        parts.append("（这是第一步，只有当前一屏）")
    parts.append("（历史里每条前面的 [时:分:秒] 是那条发生的真实时间，可以据此判断过了多久）")
    parts.append("输出下一步动作的 JSON。")
    return "\n".join(parts)


def _chat(messages: List[Dict[str, Any]]) -> Any:
    """调一次模型，返回原始响应。网关不支持 JSON 模式时自动退回纯提示词。"""
    kwargs: Dict[str, Any] = {
        "model": _model,
        "messages": messages,
        "temperature": _options.get("temperature", 0.0),
        "max_tokens": _options.get("max_tokens", 800),
    }
    if _options.get("json_mode", True):
        kwargs["response_format"] = {"type": "json_object"}

    try:
        resp = _client.chat.completions.create(**kwargs)
    except Exception as exc:
        # 只有"网关不认 response_format 这个参数"才值得去掉重试。
        # 早期版本对任何失败都重试一次，结果 401、超时这类错误也被当成参数问题，
        # 既浪费一次请求，又把真实原因埋在一句误导性的 WARNING 里（踩过）。
        if "response_format" in kwargs and _is_param_error(exc):
            _log.warning("网关不支持 response_format（%s），去掉重试一次", exc)
            kwargs.pop("response_format")
            try:
                resp = _client.chat.completions.create(**kwargs)
            except Exception as exc2:
                msg = _explain(exc2)
                _log.error("调用模型失败：%s", exc2)
                raise BrainError(msg) from exc2
        else:
            msg = _explain(exc)
            _log.error("调用模型失败：%s", exc)
            raise BrainError(msg) from exc

    _log_raw(resp)
    return resp


def _is_param_error(exc: Exception) -> bool:
    """判断这个错误是不是"参数不被支持"——只有这种才该去掉参数重试。

    看 HTTP 状态码：400 通常是参数问题，401/403 是认证、429 是限流、5xx 是服务端，
    这些重试也没用，而且会让错误信息变得难懂。
    """
    code = getattr(exc, "status_code", None)
    if code is None:
        resp = getattr(exc, "response", None)
        code = getattr(resp, "status_code", None)
    if code == 400:
        return True
    text = str(exc).lower()
    return "response_format" in text or "json_object" in text


def _explain(exc: Exception) -> str:
    """把调用失败翻译成人能看懂的一句话。

    网关报的错经常只有一个 "Not Found"，看不出是自己配置填错了（踩过：
    base_url 末尾多带了 /chat/completions，请求打到 …/chat/completions/chat/completions
    上，服务端 404，报错里完全不提这件事）。
    """
    code = getattr(exc, "status_code", None)
    if code is None:
        code = getattr(getattr(exc, "response", None), "status_code", None)

    if code == 404:
        return (f"接口地址不对（404）：{exc}\n"
                f"    当前 base_url = {_client.base_url if _client else '(未配置)'}\n"
                f"    注意 base_url 只填到版本号那一层，别带 /chat/completions——"
                f"库会自己接上，多填就会拼成两遍导致 404")
    if code in (401, 403):
        return f"密钥无效或没权限（{code}）：{exc}"
    if code == 429:
        return f"请求太频繁或额度用完了（429）：{exc}"
    if code and 500 <= code < 600:
        return f"服务端出错（{code}），过会儿再试：{exc}"
    return f"调用模型失败：{exc}"


def _log_raw(resp) -> None:
    """把模型的原始返回完整记下来。

    排查"返回不是合法 JSON"只能靠这个——报错信息里的截断片段看不出根因，
    得看全文（有没有被 max_tokens 截断、有没有多段 JSON、有没有思考过程混在里面）。
    """
    try:
        ch = resp.choices[0]
        content = ch.message.content or ""
        finish = getattr(ch, "finish_reason", "?")
        usage = getattr(resp, "usage", None)
        _log.debug("响应：finish_reason=%s tokens=%s/%s 长度=%d\n--- 原始返回 ---\n%s\n--- 结束 ---",
                   finish,
                   getattr(usage, "prompt_tokens", "?"), getattr(usage, "completion_tokens", "?"),
                   len(content), content)
        if finish == "length":
            _log.warning("返回被 max_tokens 截断了（finish_reason=length）——"
                         "调大 brain.init(max_tokens=...) 或让提示词更简短")
    except Exception as exc:
        _log.warning("记录原始返回失败：%s", exc)


def _dump_bad_reply(content: str) -> None:
    """解析不了时，把全文单独存一份，方便完整查看（日志可能被截断显示）。"""
    try:
        p = LOG_PATH.parent / "bad_reply.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        _log.error("无法解析的返回已存到 %s", p)
    except Exception:
        pass


def _parse(resp) -> Decision:
    """从响应里抠出 JSON。模型爱加代码块或前言，这里做容错。"""
    try:
        content = resp.choices[0].message.content or ""
    except Exception:
        content = ""

    obj = _extract_json(content)
    if obj is None:
        _dump_bad_reply(content)
        raise BrainError(f"模型返回的不是合法 JSON：{content[:200]}")

    action = obj.get("action") or {}
    if not isinstance(action, dict) or "type" not in action:
        _dump_bad_reply(content)
        raise BrainError(f"返回里没有 action：{obj}")

    atype = str(action["type"]).strip().lower()
    if atype not in ACTIONS:
        _dump_bad_reply(content)
        raise BrainError(f"不认识的动作用 {atype!r}，可用：{', '.join(ACTIONS)}")
    action["type"] = atype

    decision = Decision(
        thought=str(obj.get("thought", ""))[:300],
        action=action,
    )
    _log.info("决定：%s", decision.describe())
    return decision


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从一堆文字里找出那个 JSON 对象。找不出来返回 None。

    模型爱在外面包东西，见过的几种：
    - ```json ... ``` 代码块
    - `<think>推理过程</think><answer>{...}</answer>`（推理模型，GLM 新版就这样）
    - 直接一段人话 + JSON

    所以策略是**从最后一个 `{` 往前找配对的 `}`**，而不是要求整段就是 JSON。
    失败时记日志：是"压根没有花括号"还是"有花括号但语法不对"，两者修法不同。
    """
    s = (text or "").strip()
    if not s:
        _log.warning("解析失败：返回是空的")
        return None

    # 推理模型的思考段：先摘掉。它是模型自言自语，里面常带花括号，
    # 留着会干扰下面的定位（实测 <think> 段里有「动作类型是click」这类内容）。
    s = re.sub(r"<think\b[^>]*>.*?</think\s*>", " ", s, flags=re.S | re.I)
    # <answer> 包裹：只取里面的内容（标签名各家不同，泛化处理）
    m = re.search(r"<answer\b[^>]*>(.*?)</answer\s*>", s, flags=re.S | re.I)
    if m:
        s = m.group(1)

    s = s.strip()
    # 去掉 ```json ... ``` 包裹
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
        _log.debug("剥掉代码块标记后剩 %d 字符", len(s))

    # 找 JSON 对象的范围：从第一个 { 开始，依次拿后面的 { 试。
    # **顺序很关键——必须从最外层往里试**。JSON 里常有嵌套（action 就是个内层对象），
    # 如果从最后一个 { 往前找，会先拿到内层对象：轻则白解析一次、日志里留下
    # 一条吓人的"解析失败"（实测踩过），重则内层恰好是合法 JSON，于是返回了错的对象。
    start = s.find("{")
    end = s.rfind("}")
    while start >= 0 and end > start:
        obj = _try_load(s[start:end + 1])
        if obj is not None:
            return obj
        start = s.find("{", start + 1)      # 这一段不成，从下一个 { 再试

    _log.warning("解析失败：把所有 { 都试了一遍，没有一处是合法 JSON")
    return None


def _try_load(chunk: str) -> Optional[Dict[str, Any]]:
    """试一次 json.loads，失败就试着修一下常见的模型手误。

    模型写得最像又最不像 JSON 的一类毛病：**引号打多了**。
    实测见过 `"thought":" "看到播放按钮..."` —— 冒号后多一个引号，
    整段就少了个键值分隔，json.loads 报 "Expecting ',' delimiter"。
    """
    try:
        obj = json.loads(chunk)
    except json.JSONDecodeError as exc:
        fixed = _fix_quotes(chunk)
        if fixed is not None:
            try:
                obj = json.loads(fixed)
                _log.warning("JSON 有引号错误，自动修复后解析成功（原错误：%s）", exc.msg)
                return obj
            except json.JSONDecodeError:
                pass
        _log.warning("解析失败：JSON 语法错误 → %s（片段 %d 字符，位置 %d）",
                     exc.msg, len(chunk), exc.pos)
        _log.debug("出错的片段：\n%s", chunk[:500])
        return None

    if not isinstance(obj, dict):
        _log.warning("解析失败：顶层不是对象，是 %s", type(obj).__name__)
        return None
    return obj


def _fix_quotes(chunk: str) -> Optional[str]:
    """修「值前面多打了一个引号」这种毛病：`:" "内容"` → `:" 内容"`。

    实测见过的形态：模型想写 `"thought":" 看到按钮"`，手一抖写成
    `"thought":" "看到按钮"` —— 值的位置变成了 `" "`，于是整段不再合法。

    **只在已经非法的 JSON 上动手，且只删那一个多余引号**，不做任何别的猜测：
    这个模式后面必须跟着实打实的内容（不是 `,` `}` 这类分隔符），
    所以合法的 `""`、`" "` 都不会被误伤。修不了返回 None，
    宁可报错也别自作聪明改，改坏了会变成"点错地方"这种更糟的问题。
    """
    fixed = re.sub(r'(:\s*"\s*)"(?=[^"\s,}\]:])', r"\1", chunk)
    if fixed == chunk:
        return None
    return fixed
