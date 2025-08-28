from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, MessageSegment
import re
import io
import base64
import random
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

music_plugin = on_message(priority=5)

# match ncm link
URL_RE = re.compile(
    r"https?://(?:y\.)?music\.163\.com/(?:#/)?song\?id=(\d+)",
    re.IGNORECASE,
)

# font path
PLUGIN_DIR = Path(__file__).parent
TITLE_FONT_PATH = PLUGIN_DIR / "title_font.ttf"
LYRIC_FONT_PATH = PLUGIN_DIR / "article_font.otf"  # BASED ON YOUR OWN FONT

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://music.163.com/",
    "Accept": "application/json, text/plain, */*",
}


def text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return right - left


def wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    lines: list[str] = []
    line = ""
    last_space_pos = -1

    for ch in text:
        if ch == "\n":
            if line:
                lines.append(line)
            else:
                lines.append("")
            line = ""
            last_space_pos = -1
            continue

        nxt = line + ch
        w = text_width(draw, nxt, font)
        if w <= max_width:
            line = nxt
            if ch.isspace():
                last_space_pos = len(line) - 1
        else:
            if last_space_pos != -1:
                lines.append(line[: last_space_pos + 1].rstrip())
                line = line[last_space_pos + 1 :] + ch
                last_space_pos = -1
            else:
                if line:
                    lines.append(line)
                line = ch
                last_space_pos = -1

    if line:
        lines.append(line)

    return lines


def draw_text_wrap(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    x: int,
    y: int,
    max_width: int,
    line_height: int | None = None,
    fill=(0, 0, 0),
) -> int:
    if line_height is None:
        line_height = font.size + 6

    lines = wrap_text(draw, text, font, max_width)
    for i, ln in enumerate(lines):
        draw.text((x, y + i * line_height), ln, font=font, fill=fill)
    return y + len(lines) * line_height


def safe_load_font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(path), size)
    except Exception:
        # try system fonts
        for fallback in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
        ):
            try:
                return ImageFont.truetype(fallback, size)
            except Exception:
                pass
        return ImageFont.load_default()


@music_plugin.handle()
async def _(bot: Bot, event: MessageEvent):
    text = event.get_message().extract_plain_text().strip()
    m = URL_RE.search(text)
    if not m:
        return

    song_id = m.group(1)

    # get song info
    try:
        detail_api = f"https://music.163.com/api/song/detail?ids=[{song_id}]"
        resp = requests.get(detail_api, headers=UA_HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        song = data["songs"][0]
        song_name = song["name"]
        artists = ", ".join(a["name"] for a in song.get("artists", [])) or "未知歌手"
        album = song.get("album", {})
        album_name = album.get("name", "未知专辑")
        cover_url = album.get("picUrl")
    except Exception:
        await music_plugin.finish("获取歌曲信息失败")

    # get lyrics(random 2 lines)
    try:
        lyric_api = f"https://music.163.com/api/song/media?id={song_id}"
        lrc_resp = requests.get(lyric_api, headers=UA_HEADERS, timeout=10)
        lrc_resp.raise_for_status()
        lrc_data = lrc_resp.json()
        raw = lrc_data.get("lyric", "") or ""
        lines = []
        for ln in raw.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            if "]" in ln:
                ln = ln.split("]", 1)[-1].strip()
            if ln:
                lines.append(ln)
        if len(lines) >= 2:
            start = random.randint(0, len(lines) - 2)
            sel = lines[start : start + 2]
        elif lines:
            sel = lines
        else:
            sel = ["暂无歌词"]
    except Exception:
        sel = ["暂无歌词"]

    # fetch cover image
    cover_img = None
    if cover_url:
        try:
            c = requests.get(cover_url, headers=UA_HEADERS, timeout=10)
            c.raise_for_status()
            cover_img = Image.open(io.BytesIO(c.content)).convert("RGB")
            cover_img = cover_img.resize((200, 200))
        except Exception:
            cover_img = None

    # calc image size
    title_font = safe_load_font(TITLE_FONT_PATH, 40)
    info_font = safe_load_font(LYRIC_FONT_PATH, 30)
    lyric_font = safe_load_font(LYRIC_FONT_PATH, 28)

    # temp canvas for text size calc
    tmp_img = Image.new("RGB", (1, 1), "white")
    tmp_draw = ImageDraw.Draw(tmp_img)

    header_block_height = 20  # top padding
    header_block_height += len(
        wrap_text(tmp_draw, f"{song_name}", title_font, 540)
    ) * (title_font.size + 6)
    header_block_height += 10  # gap
    header_block_height += len(
        wrap_text(tmp_draw, f"歌手: {artists}", info_font, 540)
    ) * (info_font.size + 6)
    header_block_height += len(
        wrap_text(tmp_draw, f"专辑: {album_name}", info_font, 540)
    ) * (info_font.size + 6)

    lyric_lines_wrapped = []
    for line in sel:
        lyric_lines_wrapped.extend(wrap_text(tmp_draw, line, lyric_font, 540))

    lyric_block_height = max(40, len(lyric_lines_wrapped) * (lyric_font.size + 6))

    base_height = max(240, header_block_height + 20)
    height = max(base_height + lyric_block_height + 20, 400)
    width = 800

    # create image
    img = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    if cover_img:
        img.paste(cover_img, (20, 20))

    # song title
    y = draw_text_wrap(draw, f"{song_name}", title_font, 240, 20, max_width=540)

    # singer and album
    y = draw_text_wrap(draw, f"歌手: {artists}", info_font, 240, y + 10, max_width=540)
    y = draw_text_wrap(draw, f"专辑: {album_name}", info_font, 240, y, max_width=540)

    # lyrics
    lyric_y = max(y + 20, 180)  # ensure some gap
    for line in sel:
        lyric_y = draw_text_wrap(draw, line, lyric_font, 240, lyric_y, max_width=540)

    # output in base64
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    await music_plugin.finish(MessageSegment.image(file=f"base64://{b64}"))
