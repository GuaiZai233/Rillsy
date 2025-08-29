from __future__ import annotations

import re
import io
import hashlib
import base64
from datetime import datetime, date
import xml.etree.ElementTree as ET
from typing import Optional, Tuple
import urllib.parse
import os

from nonebot.plugin import PluginMetadata
from nonebot import get_driver, on_message
from nonebot.adapters.onebot.v11 import MessageSegment, MessageEvent
import httpx
from PIL import Image, ImageDraw, ImageFont
import html2text
config = get_driver().config


# URL match
PC_URL_RE = re.compile(r"https?://(?:www\.)?ithome\.com/0/(\d+)/(\d+)\.htm", re.I)
M_URL_RE  = re.compile(r"https?://m\.ithome\.com/html/(\d+)\.htm", re.I)
API_URL_RE = re.compile(r"https?://api\.ithome\.com/xml/newscontent/(\d+)/(\d+)\.xml", re.I)

API_TMPL = "https://api.ithome.com/xml/newscontent/{major}/{minor}.xml"



api_base = getattr(config, "ai_api_base")

matcher = on_message(priority=5, block=False)

# font paths
PLUGIN_DIR = os.path.dirname(__file__)
TITLE_FONT_PATH = os.path.join(PLUGIN_DIR, "title_font.ttf")
ARTICLE_FONT_PATH = os.path.join(PLUGIN_DIR, "article_font.otf")

# html2text converter
html2text_converter = html2text.HTML2Text()
html2text_converter.ignore_links = True
html2text_converter.ignore_images = True
html2text_converter.body_width = 0


def pick_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size=size)
    except Exception:
        return ImageFont.load_default()


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines():
        buf = ""
        for ch in paragraph:
            test = buf + ch
            w = draw.textlength(test, font=font)
            if w <= max_width:
                buf = test
            else:
                if buf:
                    lines.append(buf)
                buf = ch
        if buf:
            lines.append(buf)
        lines.append("")  # paragraph break
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def extract_major_minor_from_any(url: str) -> Optional[Tuple[str, str]]:
    if m := PC_URL_RE.search(url):
        return m.group(1), m.group(2)
    if m := M_URL_RE.search(url):
        num = m.group(1)
        if len(num) <= 3:
            major = "0"
            minor = num
        else:
            major = num[:-3] or "0"
            minor = num[-3:]
        return major, minor
    if m := API_URL_RE.search(url):
        return m.group(1), m.group(2)
    return None


async def fetch_title_detail_by_api(major: str, minor: str) -> tuple[str, str]:
    url = API_TMPL.format(major=major, minor=minor)
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url)
        r.raise_for_status()
    content = r.content.decode("utf-8", errors="replace")

    try:
        root = ET.fromstring(content)
        title_el = root.find(".//title")
        detail_el = root.find(".//detail")
        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        detail_html = detail_el.text.strip() if detail_el is not None and detail_el.text else ""
        detail = html2text_converter.handle(detail_html).strip()
        detail = detail.replace("*", "")
        return title, detail
    except Exception:
        m_title = re.search(r"<title>(.*?)</title>", content, re.S | re.I)
        m_detail = re.search(r"<detail>(.*?)</detail>", content, re.S | re.I)
        title = m_title.group(1).strip() if m_title else ""
        detail_html = m_detail.group(1).strip() if m_detail else ""
        detail = html2text_converter.handle(detail_html).strip()
        detail = detail.replace("*", "")
        return title, detail


async def fetch_ai_summary(detail_text: str) -> str:
    detail_text = detail_text.replace("+", "++").replace("/", "//")
    url_safe_detail = urllib.parse.quote(detail_text)
    api_url = f"{api_base}{url_safe_detail}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(api_url)
            r.raise_for_status()
            return r.text.strip()
    except Exception:
        return "AI 总结获取失败"


def build_image(title: str, detail: str, ai_summary: str) -> str:
    padding_h = 36
    padding_v = 28
    max_text_width = 900
    title_font = pick_font(TITLE_FONT_PATH, 44)
    detail_font = pick_font(ARTICLE_FONT_PATH, 32)
    footer_font = pick_font(TITLE_FONT_PATH, 20)

    dummy = Image.new("RGB", (10, 10), "white")
    draw = ImageDraw.Draw(dummy)

    title_lines = wrap_text(draw, title, title_font, max_text_width)
    detail_lines = wrap_text(draw, detail, detail_font, max_text_width)
    ai_lines = wrap_text(draw, f"AI 总结：{ai_summary}", detail_font, max_text_width)

    line_gap = 12
    title_block_h = sum(draw.textbbox((0, 0), ln, font=title_font)[3] -
                        draw.textbbox((0, 0), ln, font=title_font)[1] + line_gap
                        for ln in title_lines)
    title_block_h = max(0, title_block_h - line_gap)

    detail_block_h = sum(draw.textbbox((0, 0), ln, font=detail_font)[3] -
                         draw.textbbox((0, 0), ln, font=detail_font)[1] + line_gap
                         for ln in detail_lines)
    detail_block_h = max(0, detail_block_h - line_gap)

    ai_block_h = sum(draw.textbbox((0, 0), ln, font=detail_font)[3] -
                     draw.textbbox((0, 0), ln, font=detail_font)[1] + line_gap
                     for ln in ai_lines)
    ai_block_h = max(0, ai_block_h - line_gap)

    today_str = date.today().strftime("%Y-%m-%d")
    ts = int(datetime.now().timestamp())
    h = hashlib.sha256(str(ts).encode("utf-8")).hexdigest()[:10]
    footer_text = f"Powered by Rillsy | Generated at {today_str} | Hash: {h}"

    footer_h = draw.textbbox((0, 0), footer_text, font=footer_font)[3]
    gap_mid = 24
    footer_extra_space = 38  # bottom padding

    width = padding_h * 2 + max_text_width
    height = padding_v * 2 + title_block_h + detail_block_h + ai_block_h + gap_mid + footer_h + footer_extra_space

    img = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(img)

    # draw title
    y = padding_v
    for ln in title_lines:
        d.text((padding_h, y), ln, font=title_font, fill="black")
        line_h = d.textbbox((0, 0), ln, font=title_font)[3] - d.textbbox((0, 0), ln, font=title_font)[1]
        y += line_h + line_gap

    # devide line
    d.line([(padding_h, y), (width - padding_h, y)], fill=(200, 200, 200), width=2)
    y += 12

    # draw detail
    for ln in detail_lines:
        d.text((padding_h, y), ln, font=detail_font, fill=(50, 50, 50))
        line_h = d.textbbox((0, 0), ln, font=detail_font)[3] - d.textbbox((0, 0), ln, font=detail_font)[1]
        y += line_h + line_gap

    y += 12

    # draw ai summary
    for ln in ai_lines:
        d.text((padding_h, y), ln, font=detail_font, fill=(80, 0, 0))
        line_h = d.textbbox((0, 0), ln, font=detail_font)[3] - d.textbbox((0, 0), ln, font=detail_font)[1]
        y += line_h + line_gap

    y += gap_mid

    # page footer
    d.text((padding_h, y), footer_text, font=footer_font, fill=(120, 120, 120))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return "base64://" + b64


@matcher.handle()
async def _(event: MessageEvent):
    text = str(event.message)
    if "ithome.com" not in text:
        return

    urls = re.findall(r"https?://[^\s]+", text)
    target = None
    for u in urls:
        if "ithome.com" in u:
            target = u
            break
    if not target:
        return

    await matcher.send("AI正在总结，请稍候...")

    pair = extract_major_minor_from_any(target)
    if not pair:
        await matcher.finish("无法解析 IT 之家文章 ID")

    major, minor = pair
    try:
        title, detail = await fetch_title_detail_by_api(major, minor)
    except Exception as e:
        await matcher.finish(f"解析失败：{e}")

    ai_summary = await fetch_ai_summary(detail)
    data_uri = build_image(title, detail, ai_summary)
    await matcher.finish(MessageSegment.image(data_uri))
