# cogs/games/fishing.py (임시 명령어 삭제 최종본)

import discord
from discord.ext import commands
from discord import ui
# [🔴 핵심 수정] app_commands import를 제거합니다.
import random
import asyncio
import logging
from typing import Optional, Set, Dict

from utils.database import (
    update_wallet, get_inventory, update_inventory, add_to_aquarium,
    get_user_gear, set_user_gear, save_panel_id, get_panel_id, get_id,
    get_embed_from_db, get_panel_components_from_db,
    get_item_database, get_fishing_loot, get_config, get_string,
    is_legendary_fish_available, set_legendary_fish_cooldown,
    BARE_HANDS, DEFAULT_ROD
)

logger = logging.getLogger(__name__)

# ... (FishingGameView 클래스는 이전과 동일) ...

# ... (FishingPanelView 클래스는 이전과 동일) ...

class Fishing(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_fishing_sessions_by_user: Set[int] = set()
        self.fishing_log_channel_id: Optional[int] = None
        self.view_instance = None
        self.last_result_messages: Dict[int, discord.Message] = {}
        logger.info("Fishing Cog가 성공적으로 초기화되었습니다.")

    async def register_persistent_views(self):
        self.view_instance = FishingPanelView(self.bot, self)
        await self.view_instance.setup_buttons()
        self.bot.add_view(self.view_instance)

    async def cog_load(self):
        await self.load_configs()

    async def load_configs(self):
        self.fishing_log_channel_id = get_id("fishing_log_channel_id")

    async def log_legendary_catch(self, user: discord.Member, result_embed: discord.Embed):
        if not self.fishing_log_channel_id or not (log_channel := self.bot.get_channel(self.fishing_log_channel_id)): return

        fish_field = next((f for f in result_embed.fields if f.name == "魚"), None)
        size_field = next((f for f in result_embed.fields if f.name == "サイズ"), None)
        if not all([fish_field, size_field]): return

        fish_name_raw = fish_field.value.replace('**', '')
        fish_data = next((loot for loot in get_fishing_loot() if loot['name'] == fish_name_raw), None)
        if not fish_data: return

        size_cm_str = size_field.value.strip('`cm`')
        size_cm = float(size_cm_str)
        value = int(fish_data.get("base_value", 0) + (size_cm * fish_data.get("size_multiplier", 0)))

        field_value = get_string("log_legendary_catch.field_value", emoji=fish_data.get('emoji','👑'), name=fish_name_raw, size=size_cm_str, value=f"{value:,}", currency_icon=get_config('CURRENCY_ICON', '🪙'))

        embed = discord.Embed(
            title=get_string("log_legendary_catch.title"),
            description=get_string("log_legendary_catch.description", user_mention=user.mention),
            color=int(get_string("log_legendary_catch.color", "0xFFD700").replace("0x", ""), 16)
        )
        embed.add_field(name=get_string("log_legendary_catch.field_name"), value=field_value)
        if user.display_avatar: embed.set_thumbnail(url=user.display_avatar.url)

        if image_url := fish_data.get('image_url'):
            embed.set_image(url=image_url)

        try:
            await log_channel.send(content="@here", embed=embed, allowed_mentions=discord.AllowedMentions(everyone=True))
        except Exception as e:
            logger.error(f"전설의 물고기 공지 전송 실패: {e}", exc_info=True)

    async def regenerate_panel(self, channel: discord.TextChannel):
        panel_key, embed_key = "fishing", "panel_fishing"
        if (panel_info := get_panel_id(panel_key)) and (old_id := panel_info.get('message_id')):
            try: await (await channel.fetch_message(old_id)).delete()
            except (discord.NotFound, discord.Forbidden): pass
        if not (embed_data := await get_embed_from_db(embed_key)):
            return logger.error(f"DB에서 '{embed_key}' 임베드를 찾을 수 없어 패널 생성을 중단합니다.")
        embed = discord.Embed.from_dict(embed_data)
        self.view_instance = FishingPanelView(self.bot, self)
        await self.view_instance.setup_buttons()
        self.bot.add_view(self.view_instance)
        new_message = await channel.send(embed=embed, view=self.view_instance)
        await save_panel_id(panel_key, new_message.id, channel.id)
        logger.info(f"✅ 낚시터 패널을 성공적으로 새로 생성했습니다. (채널: #{channel.name})")

    # [🔴 핵심 수정] /checkimages 명령어 전체를 삭제합니다.

async def setup(bot: commands.Bot):
    await bot.add_cog(Fishing(bot))
