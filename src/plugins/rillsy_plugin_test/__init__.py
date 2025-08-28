from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Event

test_cmd = on_command("test", aliases={"/test"})

@test_cmd.handle()
async def handle_test(bot: Bot, event: Event):
    await test_cmd.finish("200 OK")