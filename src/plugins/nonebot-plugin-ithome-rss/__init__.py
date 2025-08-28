import asyncio
import feedparser
from nonebot import get_driver
from nonebot.log import logger
from nonebot.adapters.onebot.v11 import Bot

from pydantic import BaseModel
from typing import List, Optional


class Config(BaseModel):
    ithome_rss_interval: int = 60
    ithome_rss_groups: List[int] = []
    ithome_rss_friends: List[int] = []


global_config = get_driver().config
plugin_config = Config(**global_config.dict())

RSS_URL = "https://www.ithome.com/rss/"
last_entry_link: Optional[str] = None


async def fetch_and_broadcast(bot: Bot):
    global last_entry_link

    feed = feedparser.parse(RSS_URL)
    if not feed.entries:
        logger.warning("RSS源没有内容")
        return

    latest = feed.entries[0]
    title = latest.title
    link = latest.link

    # If it's still the previous one, skip.
    if last_entry_link == link:
        return

    last_entry_link = link
    message = f"{title}\n{link}"

    # sending to groups
    for group_id in plugin_config.ithome_rss_groups:
        try:
            await bot.send_group_msg(group_id=group_id, message=message)
        except Exception as e:
            logger.error(f"发送群 {group_id} 失败: {e}")

    # sending to friends
    for user_id in plugin_config.ithome_rss_friends:
        try:
            await bot.send_private_msg(user_id=user_id, message=message)
        except Exception as e:
            logger.error(f"发送好友 {user_id} 失败: {e}")


driver = get_driver()

@driver.on_startup
async def _():
    async def task():
        await asyncio.sleep(5)
        bots = list(get_driver().bots.values())
        if not bots:
            logger.warning("没有发现任何 bot 实例，RSS 推送任务未启动")
            return

        bot = bots[0]
        logger.info("启动 IT之家 RSS 推送任务")
        while True:
            try:
                await fetch_and_broadcast(bot)
            except Exception as e:
                logger.error(f"RSS任务出错: {e}")
            await asyncio.sleep(plugin_config.ithome_rss_interval)

    asyncio.create_task(task())
