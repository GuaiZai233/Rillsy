from __future__ import annotations

import os
import re
import io
import base64
import hashlib
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple

import httpx
from nonebot import get_driver, on_regex
from nonebot.log import logger
from nonebot.matcher import Matcher
from nonebot.params import RegexGroup
from nonebot.plugin import PluginMetadata
from nonebot.typing import T_State
from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment

from PIL import Image, ImageDraw, ImageFont

from pydantic import BaseModel

# config = get_driver().config

class Config(BaseModel):
    bili_link_enable_pic: bool = True
    bili_link_max_desc_chars:int = 50

plugin_config = Config.parse_obj(get_driver().config.dict())

max_desc_chars = plugin_config.bili_link_max_desc_chars
enable_pic = plugin_config.bili_link_enable_pic

# match bili link
_bili_url_pattern = r"(https?://(?:b23\.tv|(?:www\.)?bilibili\.com|m\.bilibili\.com)/[^\s]+)"
preview = on_regex(_bili_url_pattern, flags=re.I)

# API endpoint
API_VIEW = "https://api.bilibili.com/x/web-interface/view"

# time zone
TZ_CN = timezone(timedelta(hours=8))


async def _resolve_redirect(url: str, client: httpx.AsyncClient) -> str:
    try:
        r = await client.get(url, follow_redirects=True, timeout=10)
        return str(r.url)
    except Exception as e:
        logger.warning(f"resolve redirect failed: {e}")
        return url


def _extract_ids(url: str) -> Tuple[Optional[str], Optional[int]]:
    m = re.search(r"(BV[0-9A-Za-z]{10})", url)
    if m:
        return m.group(1), None
    m = re.search(r"(?:^|[^0-9A-Za-z])av(\d+)", url, flags=re.I)
    if m:
        try:
            return None, int(m.group(1))
        except ValueError:
            pass
    return None, None


def _fmt_views(n: int) -> str:
    if n < 10000:
        return str(n)
    if n < 100000000:
        val = n / 10000
        s = f"{val:.1f}".rstrip("0").rstrip(".")
        return f"{s}万"
    val = n / 100000000
    s = f"{val:.2f}".rstrip("0").rstrip(".")
    return f"{s}亿"


def _fmt_time_cn(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, TZ_CN)
    return dt.strftime("%Y.%m.%d %H:%M:%S")


def _fmt_desc(text: str, limit: int) -> str:
    text = (text or "").strip().replace("\r\n", " ").replace("\n", " ")
    if len(text) > limit:
        return text[:limit] + "..."
    return text


async def _fetch_video(bvid: Optional[str], aid: Optional[int], client: httpx.AsyncClient) -> Optional[dict]:
    params = {}
    if bvid:
        params["bvid"] = bvid
    elif aid:
        params["aid"] = str(aid)
    else:
        return None
    r = await client.get(API_VIEW, params=params, timeout=10, headers={
        "User-Agent": "Mozilla/5.0 (compatible; nb-bili-preview/1.0)",
        "Referer": "https://www.bilibili.com/",
    })
    j = r.json()
    if j.get("code") != 0:
        logger.warning(f"bili api error: {j}")
        return None
    return j.get("data")



if enable_pic == True:

    # ========= PICTURES GENERATE ============
    """
    _FONT_CANDIDATES = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/System/Library/Fonts/PingFang.ttc",
    ]

    def _load_font(size: int):
        for path in _FONT_CANDIDATES:
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                continue
        return ImageFont.load_default()
    """

    PLUGIN_DIR = os.path.dirname(__file__)
    TITLE_FONT_PATH = os.path.join(PLUGIN_DIR, "title_font.ttf")
    ARTICLE_FONT_PATH = os.path.join(PLUGIN_DIR, "article_font.otf")
    FOOT_FONT_PATH = os.path.join(PLUGIN_DIR, "foot.ttf")

    def _load_font(path: str, size: int):
     try:
         return ImageFont.truetype(path, size=size)
     except Exception:
          return ImageFont.load_default()

    def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> List[str]:
        lines: List[str] = []
        cur = ""
        for ch in text:
            if ch == "\n":
                lines.append(cur)
                cur = ""
                continue
            test = cur + ch
            w = draw.textlength(test, font=font)
            if w <= max_width:
                cur = test
            else:
                if cur:
                    lines.append(cur)
                cur = ch
        if cur:
            lines.append(cur)
        if not lines:
            lines = [""]
        return lines


    async def _download_image(url: str, client: httpx.AsyncClient) -> Optional[Image.Image]:
        try:
            r = await client.get(url, timeout=10)
            r.raise_for_status()
            return Image.open(io.BytesIO(r.content)).convert("RGB")
        except Exception as e:
            logger.warning(f"download cover failed: {e}")
            return None


    def _img_to_b64(img: Image.Image) -> str:
        bio = io.BytesIO()
        img.save(bio, format="PNG")
        b64 = base64.b64encode(bio.getvalue()).decode()
        return "base64://" + b64


    async def _render_card(data: dict, client: httpx.AsyncClient) -> str:
        # basic info
        title = data.get("title") or "(无标题)"
        up = (data.get("owner") or {}).get("name") or "(未知)"
        pubdate = data.get("pubdate") or data.get("ctime") or 0
        views = ((data.get("stat") or {}).get("view")) or 0
        desc_raw = data.get("desc") or ""
        desc = _fmt_desc(desc_raw, max_desc_chars)
        pic = data.get("pic") or ""

        # gen text lines
        t_title = f"标题：{title}"
        t_up = f"UP主：{up}"
        t_time = f"日期：{_fmt_time_cn(pubdate)}"
        t_views = f"播放量：{_fmt_views(int(views))}"
        t_desc = f"简介：{desc}"

        # canvas size & margin & gap
        W = 1000
        M = 40
        GAP = 14

        # font
        font_title = _load_font(TITLE_FONT_PATH, 44)
        font_meta = _load_font(ARTICLE_FONT_PATH, 30)
        font_desc = _load_font(ARTICLE_FONT_PATH, 30)
        font_footer = _load_font(FOOT_FONT_PATH, 20)

        # cover
        cover_h = 0
        cover_img: Optional[Image.Image] = None
        if pic:
            cover_img = await _download_image(pic, client)
            if cover_img:
                maxw = W - M * 2
                w0, h0 = cover_img.size
                if w0 > maxw:
                    scale = maxw / w0
                    cover_img = cover_img.resize((int(w0 * scale), int(h0 * scale)))
                cover_h = cover_img.height

        # use a temp canvas to calculate text size
        tmp_img = Image.new("RGB", (W, 10), (255, 255, 255))
        draw = ImageDraw.Draw(tmp_img)

        text_width = W - M * 2
        lines_title = _wrap_text(draw, t_title, font_title, text_width)
        lines_up = _wrap_text(draw, t_up, font_meta, text_width)
        lines_time = _wrap_text(draw, t_time, font_meta, text_width)
        lines_views = _wrap_text(draw, t_views, font_meta, text_width)
        lines_desc = _wrap_text(draw, t_desc, font_desc, text_width)

        def _block_height(lines: List[str], font: ImageFont.ImageFont) -> int:
            if not lines:
                return 0
            bbox = draw.textbbox((0, 0), "测试", font=font)
            line_h = bbox[3] - bbox[1]
            return len(lines) * line_h + (len(lines) - 1) * GAP

        h_title = _block_height(lines_title, font_title)
        h_up = _block_height(lines_up, font_meta)
        h_time = _block_height(lines_time, font_meta)
        h_views = _block_height(lines_views, font_meta)
        h_desc = _block_height(lines_desc, font_desc)

        # footer
        now = datetime.now(TZ_CN)
        today_str = f"{now.year}-{now.month}-{now.day} {now.hour}:{now.minute}:{now.second}"
        ts = int(now.timestamp())
        hash_str = hashlib.sha256(str(ts).encode()).hexdigest()[:12]
        footer_text = f"Powered by Rillsy | Generated at {today_str} | Hash: {hash_str}"

        # calculate footer height
        lines_footer = _wrap_text(draw, footer_text, font_footer, text_width)
        h_footer = _block_height(lines_footer, font_footer)

        # total height
        total_h = (
            M
            + cover_h + (GAP if cover_h else 0)
            + h_title + GAP
            + h_up + GAP
            + h_time + GAP
            + h_views + GAP
            + h_desc
            + M + h_footer + M
        )

        # real canvas
        img = Image.new("RGB", (W, total_h), (255, 255, 255))
        draw = ImageDraw.Draw(img)

        y = M
        if cover_img:
            x = (W - cover_img.width) // 2
            img.paste(cover_img, (x, y))
            y += cover_h + GAP

        def _draw_lines(lines: List[str], font: ImageFont.ImageFont, y: int) -> int:
            for line in lines:
                draw.text((M, y), line, font=font, fill=(20, 20, 20))
                bbox = draw.textbbox((M, y), line, font=font)
                line_h = bbox[3] - bbox[1]
                y += line_h + GAP
            return y

        y = _draw_lines(lines_title, font_title, y)
        y = _draw_lines(lines_up, font_meta, y)
        y = _draw_lines(lines_time, font_meta, y)
        y = _draw_lines(lines_views, font_meta, y)
        y = _draw_lines(lines_desc, font_desc, y)

        # footer
        y += 4
        draw.line((M, y, W - M, y), fill=(230, 230, 230), width=2)
        y += GAP
        # Draw footer lines with color matching ithome_link (120, 120, 120)
        for line in lines_footer:
            draw.text((M, y), line, font=font_footer, fill=(120, 120, 120))
            bbox = draw.textbbox((M, y), line, font=font_footer)
            line_h = bbox[3] - bbox[1]
            y += line_h + GAP

        return _img_to_b64(img)


    # deploy info

    def _build_message_segment(b64_img: str) -> Message:
        msg = Message()
        msg.append(MessageSegment.image(b64_img))
        return msg


    @preview.handle()
    async def _(event: MessageEvent, state: T_State, matcher: Matcher, groups: Tuple[str, ...] = RegexGroup()):
        raw_msg = str(event.message)
        urls = re.findall(_bili_url_pattern, raw_msg, flags=re.I)
        if not urls:
            await matcher.finish()

        async with httpx.AsyncClient() as client:
            replies: List[Message] = []
            for u in urls:
                final_url = await _resolve_redirect(u, client)
                bvid, aid = _extract_ids(final_url)
                if not bvid and not aid:
                    logger.info(f"skip non-video url: {final_url}")
                    continue
                data = await _fetch_video(bvid, aid, client)
                if not data:
                    replies.append(Message(f"解析失败：{final_url}"))
                    continue
                try:
                    b64_img = await _render_card(data, client)
                    replies.append(_build_message_segment(b64_img))
                except Exception as e:
                    logger.exception(f"render card failed: {e}")
                    # if render failed, fallback to text
                    title = data.get("title") or "(无标题)"
                    up = (data.get("owner") or {}).get("name") or "(未知)"
                    pubdate = data.get("pubdate") or data.get("ctime") or 0
                    views = ((data.get("stat") or {}).get("view")) or 0
                    desc = _fmt_desc(data.get("desc") or "", max_desc_chars)
                    txt = (
                        f"标题：{title}\n"
                        f"UP主：{up}\n"
                        f"日期：{_fmt_time_cn(pubdate)}\n"
                        f"播放量：{_fmt_views(int(views))}\n"
                        f"简介：{desc}"
                    )
                    replies.append(Message(txt))

        if not replies:
            await matcher.finish()

        out = Message()
        for i, m in enumerate(replies):
            if i:
                out.append("\n")
            out.extend(m)
        await matcher.finish(out)


if enable_pic == False:
    # ========= TEXTS RESPONSE ============

    def _build_message(data: dict) -> Message:
        title = data.get("title") or "(无标题)"
        up = (data.get("owner") or {}).get("name") or "(未知)"
        pubdate = data.get("pubdate") or data.get("ctime") or 0
        views = ((data.get("stat") or {}).get("view")) or 0
        desc = data.get("desc") or ""
        pic = data.get("pic") or ""

        msg = Message()
        if pic:
            msg.append(MessageSegment.image(pic))
        msg.append(f"标题：{title}\n")
        msg.append(f"UP主：{up}\n")
        if pubdate:
            msg.append(f"时间：{_fmt_time_cn(pubdate)}")
        msg.append(f"播放量：{_fmt_views(int(views))}\n")
        if desc:
            msg.append("简介：" + _fmt_desc(desc, max_desc_chars))
        return msg


    @preview.handle()
    async def _(event: MessageEvent, state: T_State, matcher: Matcher, groups: Tuple[str, ...] = RegexGroup()):
        raw_msg = str(event.message)
        urls = re.findall(_bili_url_pattern, raw_msg, flags=re.I)
        if not urls:
            await matcher.finish()

        async with httpx.AsyncClient() as client:
            replies: List[Message] = []
            for u in urls:
                final_url = await _resolve_redirect(u, client)
                bvid, aid = _extract_ids(final_url)
                if not bvid and not aid:
                    logger.info(f"skip non-video url: {final_url}")
                    continue
                data = await _fetch_video(bvid, aid, client)
                if not data:
                    replies.append(Message(f"解析失败：{final_url}"))
                    continue
                replies.append(_build_message(data))

        if not replies:
            await matcher.finish()

        out = Message()
        for i, m in enumerate(replies):
            if i:
                out.append("\n")
            out.extend(m)
        await matcher.finish(out)