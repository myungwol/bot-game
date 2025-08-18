# cogs/games/user_profile.py

import discord
from discord.ext import commands
from discord import app_commands, ui
import logging
import asyncio
from typing import Optional, Dict, List, Any

from utils.database import (
    get_inventory, get_wallet, get_aquarium, set_user_gear, get_user_gear,
    save_panel_id, get_panel_id, get_id, get_embed_from_db, get_panel_components_from_db,
    get_item_database, get_fishing_loot, get_config
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

class ProfileView(ui.View):
    def __init__(self, user: discord.Member, cog_instance: 'UserProfile'):
        super().__init__(timeout=300)
        self.user = user
        self.cog = cog_instance
        self.message: Optional[discord.WebhookMessage] = None
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")
        self.current_page = "inventory"

    async def fetch_and_build(self):
        wallet_data, inventory, aquarium, gear = await asyncio.gather(
            get_wallet(self.user.id),
            get_inventory(str(self.user.id)),
            get_aquarium(str(self.user.id)),
            get_user_gear(str(self.user.id))
        )
        balance = wallet_data.get('balance', 0)

        embed = discord.Embed(
            title=f"{self.user.display_name}님의 프로필",
            color=self.user.color or discord.Color.default()
        )
        if self.user.display_avatar:
            embed.set_thumbnail(url=self.user.display_avatar.url)
        embed.add_field(name="💰 所持金", value=f"`{balance:,}`{self.currency_icon}", inline=False)
        
        if self.current_page == "inventory":
            embed.title += " - 持ち物"
            inv_text = "\n".join(f"{get_item_database().get(name,{}).get('emoji','📦')} **{name}**: `{count}`個" for name, count in inventory.items()) or "持ち物がありません。"
            embed.add_field(name="🎒 持ち物リスト", value=inv_text, inline=False)
        elif self.current_page == "aquarium":
            embed.title += " - 水槽"
            aqua_text = "\n".join(f"{fish['emoji']} **{fish['name']}**: `{fish['size']}`cm" for fish in aquarium) or "水槽に魚がいません。"
            embed.add_field(name="🐠 水槽の中", value=aqua_text, inline=False)
        elif self.current_page == "gear":
            embed.title += " - 装備"
            rod_name = gear.get('rod', '古い釣竿')
            bait_name = gear.get('bait', 'エサなし')
            rod_emoji = get_item_database().get(rod_name, {}).get('emoji', '🎣')
            bait_emoji = get_item_database().get(bait_name, {}).get('emoji', '🐛')
            embed.add_field(name="⚙️ 装備中のアイテム", value=f"{rod_emoji} **釣竿**: {rod_name}\n{bait_emoji} **エサ**: {bait_name}", inline=False)
        
        self.update_buttons()
        return embed

    def update_buttons(self):
        for item in self.children:
            if isinstance(item, ui.Button):
                item.disabled = (item.custom_id == f"profile_{self.current_page}")

    async def button_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("自分専用のメニューを操作してください。", ephemeral=True)
        
        self.current_page = interaction.data['custom_id'].split("_")[1]
        embed = await self.fetch_and_build()
        await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(label="持ち物", style=discord.ButtonStyle.primary, custom_id="profile_inventory", emoji="🎒")
    async def inventory_button(self, i: discord.Interaction, b: ui.Button): await self.button_callback(i)
    
    @ui.button(label="水槽", style=discord.ButtonStyle.secondary, custom_id="profile_aquarium", emoji="🐠")
    async def aquarium_button(self, i: discord.Interaction, b: ui.Button): await self.button_callback(i)
    
    @ui.button(label="装備", style=discord.ButtonStyle.secondary, custom_id="profile_gear", emoji="⚙️")
    async def gear_button(self, i: discord.Interaction, b: ui.Button): await self.button_callback(i)

class UserProfilePanelView(ui.View):
    def __init__(self, cog_instance: 'UserProfile'):
        super().__init__(timeout=None)
        self.cog = cog_instance

    async def setup_buttons(self):
        self.clear_items()
        components = await get_panel_components_from_db("profile")
        if not components: return
        
        button_info = components[0]
        button = ui.Button(
            label=button_info.get('label', '持ち物を開く'),
            style=discord.ButtonStyle.primary,
            emoji=button_info.get('emoji', '📦'),
            custom_id=button_info.get('component_key', 'open_inventory')
        )
        button.callback = self.open_profile
        self.add_item(button)

    async def open_profile(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        view = ProfileView(interaction.user, self.cog)
        embed = await view.fetch_and_build()
        view.message = await interaction.followup.send(embed=embed, view=view, ephemeral=True)

class UserProfile(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.view_instance: Optional[UserProfilePanelView] = None
        logger.info("UserProfile Cog가 성공적으로 초기화되었습니다.")
    
    async def register_persistent_views(self):
        self.view_instance = UserProfilePanelView(self)
        await self.view_instance.setup_buttons()
        self.bot.add_view(self.view_instance)
        
    async def cog_load(self):
        pass

    async def regenerate_panel(self, channel: discord.TextChannel):
        """요청에 의해 프로필 패널을 재생성합니다."""
        panel_key = "profile"
        embed_key = "panel_profile"

        panel_info = get_panel_id(panel_key)
        if panel_info and (old_id := panel_info.get('message_id')):
            try:
                old_message = await channel.fetch_message(old_id)
                await old_message.delete()
            except (discord.NotFound, discord.Forbidden):
                pass
        
        embed_data = await get_embed_from_db(embed_key)
        if not embed_data:
            logger.warning(f"DB에서 '{embed_key}' 임베드 데이터를 찾을 수 없어, 패널 생성을 건너뜁니다.")
            return

        embed = discord.Embed.from_dict(embed_data)
        
        self.view_instance = UserProfilePanelView(self)
        await self.view_instance.setup_buttons()
        self.bot.add_view(self.view_instance)

        new_message = await channel.send(embed=embed, view=self.view_instance)
        await save_panel_id(panel_key, new_message.id, channel.id)
        logger.info(f"✅ 프로필 패널을 성공적으로 새로 생성했습니다. (채널: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(UserProfile(bot))
