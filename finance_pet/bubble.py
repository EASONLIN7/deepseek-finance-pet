"""气泡渲染：把一份数据快照画成带圆角、阴影、尾巴的 RGBA 贴图。

本模块只负责"画"，不负责"显示"：

* 运行时由 ``win32_window.BubbleWindow`` 通过 ``UpdateLayeredWindow`` 贴到桌面；
* 开发时用 ``export_preview`` 直接导出 PNG 做设计评审与视觉回归。

两者走的是同一个 ``compose`` 函数，因此预览图和真实气泡像素级一致。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

SS = 4          # 超采样倍数（圆角/尾巴抗锯齿）
MARGIN = 18     # 卡片四周留白，给阴影留空间
TAIL_H = 11     # 尾巴高度
RADIUS = 16     # 圆角半径


@dataclass(frozen=True)
class Palette:
    card: tuple[int, int, int]
    border: tuple[int, int, int]
    shadow: tuple[int, int, int]
    title: str
    balance: str
    text: str
    muted: str
    divider: tuple[int, int, int]
    ok: str
    warn: str
    err: str
    accent: str
    button: tuple[int, int, int]
    button_border: tuple[int, int, int]
    button_text: str


PALETTES: dict[str, Palette] = {
    "dark": Palette(
        card=(24, 26, 33),
        border=(62, 68, 84),
        shadow=(0, 0, 0),
        title="#9BA4B5",
        balance="#F3F6FC",
        text="#D8DDE8",
        muted="#828B9D",
        divider=(50, 55, 67),
        ok="#3DD68C",
        warn="#F5A524",
        err="#F0616D",
        accent="#4D6BFE",
        button=(37, 40, 51),
        button_border=(78, 85, 104),
        button_text="#EAEEF7",
    ),
    "light": Palette(
        card=(255, 255, 255),
        border=(219, 223, 231),
        shadow=(15, 20, 35),
        title="#5C6470",
        balance="#12141A",
        text="#2A2F3A",
        muted="#7C8493",
        divider=(233, 236, 241),
        ok="#17A673",
        warn="#C77700",
        err="#D93A46",
        accent="#3355F5",
        button=(244, 246, 250),
        button_border=(214, 219, 228),
        button_text="#1B1F27",
    ),
}


def hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def metrics(width: int, show_retry: bool) -> dict[str, int]:
    """气泡内所有元素的坐标（渲染与点击命中判定共用）。

    注意：调用方必须与 ``compose`` 使用同一组参数，否则高度会不一致。
    """
    return metrics_for(width, show_retry, has_notice=False)


def metrics_for(width: int, show_retry: bool, *, has_notice: bool = False) -> dict[str, int]:
    """同上，但可额外为"提醒行"留出空间。"""
    extra = 26 if has_notice else 0
    content_h = 152 + extra + (42 if show_retry else 0)
    top = MARGIN + TAIL_H + 16
    left = MARGIN + 18
    right = MARGIN + width - 18
    return {
        "content_h": content_h,
        "left": left,
        "right": right,
        "top": top,
        "balance_y": top + 24,
        "today_y": top + 68,
        "notice_y": top + 92,
        "divider_y": top + 94 + extra,
        "footer_y": top + 104 + extra,
        "button_y": top + 126 + extra,
        "button_h": 30,
    }


def _rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    w, h = size
    big = Image.new("L", (w * SS, h * SS), 0)
    ImageDraw.Draw(big).rounded_rectangle(
        [0, 0, w * SS - 1, h * SS - 1], radius=radius * SS, fill=255
    )
    return big.resize((w, h), Image.LANCZOS)


def _polygon_mask(size: tuple[int, int], points: list[tuple[float, float]]) -> Image.Image:
    w, h = size
    big = Image.new("L", (w * SS, h * SS), 0)
    ImageDraw.Draw(big).polygon([(x * SS, y * SS) for x, y in points], fill=255)
    return big.resize((w, h), Image.LANCZOS)


def render_surface(
    *,
    width: int,
    content_height: int,
    theme: str = "dark",
    tail: str = "down",
    highlight: str | None = None,
) -> Image.Image:
    """渲染带阴影与尾巴的 RGBA 卡片（透明背景，可直接做分层窗口贴图）。"""
    pal = PALETTES.get(theme, PALETTES["dark"])
    total_w = width + MARGIN * 2
    total_h = content_height + TAIL_H + MARGIN * 2
    offset_y = MARGIN + TAIL_H if tail == "down" else MARGIN

    # --- 尾巴顶点 ---
    cx = MARGIN + width // 2
    tw = 20
    if tail == "down":
        tpts = [(cx - tw / 2, offset_y + content_height - 4),
                (cx + tw / 2, offset_y + content_height - 4),
                (cx, offset_y + content_height + TAIL_H)]
    else:
        tpts = [(cx - tw / 2, offset_y + 4),
                (cx + tw / 2, offset_y + 4),
                (cx, offset_y - TAIL_H)]
    tail_mask = _polygon_mask((total_w, total_h), tpts)

    # --- 阴影：圆角矩形（下移 3px）与尾巴的并集，再高斯模糊 ---
    shadow_mask = Image.new("L", (total_w, total_h), 0)
    shadow_mask.paste(_rounded_mask((width, content_height), RADIUS), (MARGIN, offset_y + 3))
    shadow_mask = ImageChops.lighter(shadow_mask, tail_mask).filter(ImageFilter.GaussianBlur(5))
    shadow = Image.new("RGBA", (total_w, total_h), pal.shadow + (0,))
    shadow.putalpha(shadow_mask.point(lambda v: int(v * (0.45 if theme == "dark" else 0.32))))

    # --- 卡片本体 ---
    body = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
    card = Image.new("RGBA", (width, content_height), pal.card + (255,))
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle(
        [0, 0, width - 1, content_height - 1], radius=RADIUS,
        outline=pal.border + (255,), width=1,
    )
    if highlight:
        tone = {"accent": pal.accent, "err": pal.err, "ok": pal.ok, "warn": pal.warn}.get(highlight)
        if tone:
            draw.rounded_rectangle(
                [1, 1, width - 2, content_height - 2], radius=RADIUS - 1,
                outline=hex_rgb(tone) + (120,), width=1,
            )
    card_mask = _rounded_mask((width, content_height), RADIUS)
    body.paste(card, (MARGIN, offset_y), card_mask)

    tail_layer = Image.new("RGBA", (total_w, total_h), pal.card + (255,))
    body.paste(tail_layer, (0, 0), tail_mask)

    return Image.alpha_composite(shadow, body)


# ------------------------------------------------------------------- 字体

_FONT_REGULAR = (
    "C:/Windows/Fonts/msyh.ttc",        # 微软雅黑
    "C:/Windows/Fonts/simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)
_FONT_BOLD = (
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
)

_font_cache: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    for path in (_FONT_BOLD if bold else _FONT_REGULAR):
        if os.path.isfile(path):
            try:
                font = ImageFont.truetype(path, size)
                _font_cache[key] = font
                return font
            except Exception:
                continue
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf", size)
    except Exception:  # pragma: no cover
        font = ImageFont.load_default()
    _font_cache[key] = font
    return font


SAMPLE = {
    "title": "DeepSeek 余额",
    "status": "●  已连接",
    "balance": "¥12.80",
    "note": "CNY",
    "today": "今日已用 ¥1.36",
    "period": "近 7 天 ¥9.42",
    "footer": "03:12:41 更新 · 133ms",
    "retry": "重试",
}

SAMPLE_ERROR = dict(SAMPLE, status="●  缓存", footer="无法连接 DeepSeek 服务器（3 分钟前的数据）")
SAMPLE_LOW = dict(
    SAMPLE,
    status="●  余额告急",
    balance="¥4.74",
    today="今日已用 ¥5.46",
    period="近 7 天 ¥9.41",
    notice="余额仅剩 ¥4.74，建议尽快充值",
    footer="低于 ¥5.00 阈值 · 自动提醒",
)
SAMPLE_BURST = dict(
    SAMPLE,
    status="●  消耗提醒",
    period="近 30 分钟 ¥2.31",
    notice="最近 30 分钟已消耗 ¥2.31（含非本机消费）",
    footer="超过 ¥2.00 阈值 · 自动提醒",
)


def sample_from_payload(payload: dict[str, Any]) -> dict[str, str]:
    """把业务数据映射成绘制文字用的字段。"""
    return {
        "title": "DeepSeek 余额",
        "status": "●  " + str(payload.get("status_text", "")),
        "balance": str(payload.get("balance_text", "—")),
        "note": str(payload.get("currency_note") or payload.get("currency") or ""),
        "today": "今日已用 " + str(payload.get("today_text", "¥0.00")),
        "period": str(payload.get("period_text", "")),
        "notice": str(payload.get("notice", "")),
        "footer": str(payload.get("footer_text", "")),
        "retry": str(payload.get("retry_text", "重试")),
    }


def draw_text(
    img: Image.Image,
    *,
    width: int,
    theme: str = "dark",
    show_retry: bool = False,
    state: str = "ok",
    level: str = "ok",
    sample: dict[str, str] | None = None,
    has_notice: bool = False,
) -> Image.Image:
    """在卡片上绘制文字与可点击按钮区域。"""
    pal = PALETTES.get(theme, PALETTES["dark"])
    sample = sample or SAMPLE
    m = metrics_for(width, show_retry, has_notice=has_notice)
    d = ImageDraw.Draw(img)

    # 标题
    d.text((m["left"], m["top"]), sample["title"], font=load_font(14), fill=pal.title)
    # 右上状态
    status_font = load_font(13)
    tone = {"ok": pal.ok, "warn": pal.warn, "err": pal.err}.get(level, pal.muted)
    d.text((m["right"] - d.textlength(sample["status"], font=status_font), m["top"] + 1),
           sample["status"], font=status_font, fill=tone)

    # 余额大字
    d.text((m["left"], m["balance_y"] - 4), sample["balance"], font=load_font(40, True),
           fill=pal.balance)
    if sample["note"]:
        note_font = load_font(13)
        d.text((m["right"] - d.textlength(sample["note"], font=note_font), m["balance_y"] + 22),
               sample["note"], font=note_font, fill=pal.muted)

    # 今日消耗
    d.text((m["left"], m["today_y"]), sample["today"], font=load_font(15), fill=pal.text)
    if sample["period"]:
        period_font = load_font(13)
        d.text((m["right"] - d.textlength(sample["period"], font=period_font), m["today_y"] + 2),
               sample["period"], font=period_font, fill=pal.muted)

    d.line([m["left"], m["divider_y"], m["right"], m["divider_y"]], fill=pal.divider, width=1)

    # 高亮提醒行（余额告急 / 消耗提醒）
    if has_notice and sample.get("notice"):
        notice_tone = pal.err if level == "err" else pal.warn
        d.text((m["left"], m["notice_y"] - 4), sample["notice"], font=load_font(15, True),
               fill=notice_tone)

    # 底部状态行
    d.text((m["left"], m["footer_y"]), sample["footer"], font=load_font(13),
           fill=tone if state == "error" else pal.muted)

    if show_retry:
        bw, bh = m["right"] - m["left"], m["button_h"]
        btn = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
        btn.paste(Image.new("RGBA", (bw, bh), pal.button + (255,)), (0, 0),
                  _rounded_mask((bw, bh), bh // 2))
        ImageDraw.Draw(btn).rounded_rectangle(
            [0, 0, bw - 1, bh - 1], radius=bh // 2, outline=pal.button_border + (255,), width=1)
        img.alpha_composite(btn, (m["left"], m["button_y"]))
        label_font = load_font(15)
        lw = d.textlength(sample["retry"], font=label_font)
        d.text((m["left"] + (bw - lw) / 2, m["button_y"] + 6), sample["retry"],
               font=label_font, fill=pal.button_text)
    return img


def compose(
    payload: dict[str, Any],
    *,
    theme: str = "dark",
    width: int = 320,
    sample: dict[str, str] | None = None,
) -> tuple[Image.Image, dict[str, Any]]:
    """把业务数据渲染成最终贴图，并返回命中判定所需的几何信息。"""
    show_retry = bool(payload.get("show_retry"))
    state = str(payload.get("state", "ok"))
    level = str(payload.get("status_level", "ok"))
    tail = str(payload.get("_tail", "down"))
    effective = sample or sample_from_payload(payload)
    # 提醒行是否存在，以"最终要画的文案"为准（预览和运行时都走这里）
    has_notice = bool(str(effective.get("notice") or ""))
    m = metrics_for(width, show_retry, has_notice=has_notice)
    highlight = {"ok": None, "loading": "accent", "error": "err", "alert": "warn"}.get(state)
    if state == "alert" and level == "err":
        highlight = "err"

    img = render_surface(
        width=width,
        content_height=m["content_h"],
        theme=theme,
        tail=tail,
        highlight=highlight,
    )
    draw_text(
        img,
        width=width,
        theme=theme,
        show_retry=show_retry,
        state=state,
        level=level,
        sample=effective,
        has_notice=has_notice,
    )
    offset_y = MARGIN + TAIL_H if tail == "down" else MARGIN
    geometry = {
        "card": (MARGIN, offset_y, width, m["content_h"]),
        "retry": (m["left"], m["button_y"], m["right"] - m["left"], m["button_h"]) if show_retry else None,
        "tail": tail,
    }
    return img, geometry


def export_preview(
    path: str,
    *,
    theme: str = "dark",
    width: int = 320,
    show_retry: bool = False,
    error: bool = False,
    tail: str = "down",
    checkerboard: bool = True,
    variant: str = "",
) -> str:
    """导出气泡预览 PNG（棋盘底衬，便于观察透明区域）。"""
    variants = {
        "low": ("alert", "err", SAMPLE_LOW),
        "burst": ("alert", "warn", SAMPLE_BURST),
        "error": ("error", "warn", SAMPLE_ERROR),
    }
    state, level, sample = variants.get(
        variant or ("error" if error else "ok"),
        ("ok", "ok", SAMPLE),
    )
    payload = {
        "state": state,
        "status_level": level,
        "show_retry": show_retry or state == "error",
        "_tail": tail,
    }
    img, _ = compose(payload, theme=theme, width=width, sample=sample)
    if checkerboard:
        bg = Image.new("RGB", img.size, (128, 128, 128))
        draw = ImageDraw.Draw(bg)
        step = 10
        for row, y in enumerate(range(0, img.size[1], step)):
            for col, x in enumerate(range(0, img.size[0], step)):
                if (row + col) % 2 == 0:
                    draw.rectangle([x, y, x + step - 1, y + step - 1], fill=(104, 104, 104))
        bg.paste(img, (0, 0), img)
        img = bg
    img.save(path)
    return path
