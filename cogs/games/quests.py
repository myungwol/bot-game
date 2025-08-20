import discord
from discord.ext import commands, tasks
from discord import ui
import logging
from typing import Optional, Dict, List, Any
import time # [✅✅✅ 핵심 수정 ✅✅✅] time 라이브러리를 import합니다.

from utils.database import (
    get_user_progress, has_checked_in_today,
    get_config, get_panel_components_from_db,
    save_panel_id, get_panel_id, get_embed_from_db,
    update_wallet, set_cooldown, get_cooldown
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

# --- 퀘스트 정의 및 보상 ---
QUEST_REWARDS = {
    "daily": {
        "attendance": 20,
        "voice": 50,
        "fishing": 30,
        "all_complete": 100
    },
    "weekly": {
        "attendance": 200,
        "voice": 500,
        "fishing": 300,
        "all_complete": 1000
    }
}

DAILY_QUESTS = {
    "attendance": {"name": "出席チェックをする", "goal": 1},
    "voice": {"name": "ボイスチャンネルに10分間参加する", "goal": 10},
    "fishing": {"name": "魚を3匹釣る", "goal": 3},
}
WEEKLY_QUESTS = {
    "attendance": {"name": "出席チェックを5回する", "goal": 5},
    "voice": {"name": "ボイスチャンネルに1時間参加する", "goal": 60},
    "fishing": {"name": "魚を10匹釣る", "goal": 10},
}

class QuestView(ui.View):
    def __init__(self, user: discord.Member, cog_instance: 'Quests'):
        super().__init__(timeout=180)
        self.user = user
        self.cog = cog_instance
        self.current_tab = "daily"

    async def update_view(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed = await self.build_embed()
        self.update_components()
        await interaction.edit_original_response(embed=embed, view=self)

    async def build_embed(self) -> discord.Embed:
        progress = await get_user_progress(self.user.id)
        has_attended_today = await has_checked_in_today(self.user.id)

        embed = discord.Embed(color=0x2ECC71)
        embed.set_author(name=f"{self.user.display_name}さんのクエスト", icon_url=self.user.display_avatar.url if self.user.display_avatar else None)

        quests_to_show = DAILY_QUESTS if self.current_tab == "daily" else WEEKLY_QUESTS
        rewards = QUEST_REWARDS[self.current_tab]
        
        progress_values = {
            "daily": {
                "attendance": 1 if has_attended_today else 0,
                "voice": progress.get('daily_voice_minutes', 0),
                "fishing": progress.get('daily_fish_count', 0),
            },
            "weekly": {
                "attendance": progress.get('weekly_attendance_count', 0),
                "voice": progress.get('weekly_voice_minutes', 0),
                "fishing": progress.get('weekly_fish_count', 0),
            }
        }[self.current_tab]

        embed.title = "📅 デイリークエスト" if self.current_tab == "daily" else "🗓️ ウィークリークエスト"

        all_complete = True
        for key, quest in quests_to_show.items():
            current = progress_values.get(key, 0)
            goal = quest["goal"]
            reward = rewards.get(key, 0)
            is_complete = current >= goal
            
            if not is_complete:
                all_complete = False

            emoji = "✅" if is_complete else "❌"
            field_name = f"{emoji} {quest['name']}"
            field_value = f"> ` {min(current, goal)} / {goal} `\n> **報酬:** `{reward:,}` {self.cog.currency_icon}"
            embed.add_field(name=field_name, value=field_value, inline=False)

        if all_complete:
            embed.set_footer(text=f"🎉 すべてのクエスト完了！追加報酬: {rewards['all_complete']:,}コイン")
        else:
            embed.set_footer(text="クエストを完了して報酬を獲得しましょう！")

        return embed

    def update_components(self):
        for item in self.children:
            if isinstance(item, ui.Button) and item.custom_id.startswith("tab_"):
                if item.custom_id == f"tab_{self.current_tab}":
                    item.style = discord.ButtonStyle.primary
                    item.disabled = True
                else:
                    item.style = discord.ButtonStyle.secondary
                    item.disabled = False
    
    @ui.button(label="デイリー", style=discord.ButtonStyle.primary, custom_id="tab_daily", disabled=True)
    async def daily_tab_button(self, interaction: discord.Interaction, button: ui.Button):
        self.current_tab = "daily"
        await self.update_view(interaction)

    @ui.button(label="ウィークリー", style=discord.ButtonStyle.secondary, custom_id="tab_weekly")
    async def weekly_tab_button(self, interaction: discord.Interaction, button: ui.Button):
        self.current_tab = "weekly"
        await self.update_view(interaction)

    @ui.button(label="完了したクエストの報酬を受け取る", style=discord.ButtonStyle.success, emoji="💰", row=1)
    async def claim_rewards_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)

        progress = await get_user_progress(self.user.id)
        has_attended_today = await has_checked_in_today(self.user.id)
        
        total_reward = 0
        reward_details = []

        quests_to_check = DAILY_QUESTS if self.current_tab == "daily" else WEEKLY_QUESTS
        rewards = QUEST_REWARDS[self.current_tab]
        
        progress_values = {
            "daily": {
                "attendance": 1 if has_attended_today else 0,
                "voice": progress.get('daily_voice_minutes', 0),
                "fishing": progress.get('daily_fish_count', 0),
            },
            "weekly": {
                "attendance": progress.get('weekly_attendance_count', 0),
                "voice": progress.get('weekly_voice_minutes', 0),
                "fishing": progress.get('weekly_fish_count', 0),
            }
        }[self.current_tab]

        all_quests_complete = True
        for key, quest in quests_to_check.items():
            is_complete = progress_values.get(key, 0) >= quest["goal"]
            if not is_complete:
                all_quests_complete = False
                continue

            cooldown_key = f"quest_claimed_{self.current_tab}_{key}"
            last_claimed_timestamp = await get_cooldown(str(self.user.id), cooldown_key)
            if last_claimed_timestamp > 0:
                continue

            reward = rewards.get(key, 0)
            total_reward += reward
            reward_details.append(f"・{quest['name']}: `{reward:,}`")
            await set_cooldown(str(self.user.id), cooldown_key, time.time())

        if all_quests_complete:
            cooldown_key = f"quest_claimed_{self.current_tab}_all"
            last_claimed_timestamp = await get_cooldown(str(self.user.id), cooldown_key)
            if last_claimed_timestamp == 0:
                reward = rewards.get("all_complete", 0)
                total_reward += reward
                reward_details.append(f"・全クエスト完了ボーナス: `{reward:,}`")
                await set_cooldown(str(self.user.id), cooldown_key, time.time())

        if total_reward > 0:
            await update_wallet(self.user, total_reward)
            details_text = "\n".join(reward_details)
            await interaction.followup.send(
                f"🎉 **以下の報酬を受け取りました！**\n{details_text}\n\n**合計:** `{total_reward:,}` {self.cog.currency_icon}",
                ephemeral=True
            )
        else:
            await interaction.followup.send("❌ 受け取れる報酬がありません。", ephemeral=True)

        embed = await self.build_embed()
        self.update_components()
        await interaction.edit_original_response(embed=embed, view=self)

class QuestPanelView(ui.View):
    def __init__(self, cog_instance: 'Quests'):
        super().__init__(timeout=None)
        self.cog = cog_instance

    async def setup_buttons(self):
        self.clear_items()
        components = await get_panel_components_from_db("panel_quests")
        for button_info in components:
            button = ui.Button(
                label=button_info.get('label'), style=discord.ButtonStyle.blurple,
                emoji=button_info.get('emoji'), custom_id=button_info.get('component_key')
            )
            button.callback = self.open_quest_view
            self.add_item(button)

    async def open_quest_view(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        view = QuestView(interaction.user, self.cog)
        embed = await view.build_embed()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

class Quests(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.currency_icon = "🪙"
    
    async def cog_load(self):
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")

    async def register_persistent_views(self):
        view = QuestPanelView(self)
        await view.setup_buttons()
        self.bot.add_view(view)

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_quests", **kwargs):
        if panel_info := get_panel_id(panel_key):
            if (old_channel := self.bot.get_channel(panel_info['channel_id'])) and (old_message_id := panel_info.get('message_id')):
                try: await (await old_channel.fetch_message(old_message_id)).delete()
                except (discord.NotFound, discord.Forbidden): pass
        
        embed_data = await get_embed_from_db(panel_key)
        if not embed_data: return

        embed = discord.Embed.from_dict(embed_data)
        view = QuestPanelView(self)
        await view.setup_buttons()
        self.bot.add_view(view)
        
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_key, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} パネルを正常に生成しました。(チャンネル: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(Quests(bot))
