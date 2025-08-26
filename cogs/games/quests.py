# cogs/games/quests.py

import discord
from discord.ext import commands
from discord import ui
import logging
import time
from typing import Optional, Dict, Any
from datetime import datetime, timezone, timedelta

from utils.database import (
    get_all_user_stats, 
    get_config,
    save_panel_id, get_panel_id, get_embed_from_db,
    update_wallet, set_cooldown, get_cooldown, log_activity,
    supabase, get_id
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

QUEST_REWARDS = {
    "daily": {
        "attendance": {"coin": 10, "xp": 5},
        "voice": {"coin": 55, "xp": 20},
        "fishing": {"coin": 35, "xp": 15},
        "all_complete": {"coin": 100, "xp": 50}
    },
    "weekly": {
        "attendance": {"coin": 100, "xp": 50},
        "voice": {"coin": 550, "xp": 200},
        "fishing": {"coin": 350, "xp": 150},
        "all_complete": {"coin": 1000, "xp": 500}
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

class TaskBoardView(ui.View):
    def __init__(self, cog_instance: 'Quests'):
        super().__init__(timeout=None)
        self.cog = cog_instance

        check_in_button = ui.Button(
            label="出席チェック",
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id="task_board_daily_check"
        )
        check_in_button.callback = self.check_in_callback
        self.add_item(check_in_button)

        quest_button = ui.Button(
            label="クエスト確認",
            style=discord.ButtonStyle.primary,
            emoji="📜",
            custom_id="task_board_open_quests"
        )
        quest_button.callback = self.open_quest_view
        self.add_item(quest_button)

    async def check_in_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user = interaction.user
        
        stats = await get_all_user_stats(user.id)
        if stats.get('daily', {}).get('check_in_count', 0) > 0:
            await interaction.followup.send("❌ 本日は既に出席チェックが完了しています。", ephemeral=True)
            return

        reward_str = get_config("DAILY_CHECK_REWARD", "100").strip('"')
        attendance_reward = int(reward_str)
        
        await log_activity(user.id, 'daily_check_in', coin_earned=attendance_reward, xp_earned=0)
        await update_wallet(user, attendance_reward)
        
        await interaction.followup.send(f"✅ 出席チェックが完了しました！ **`{attendance_reward}`**{self.cog.currency_icon}を獲得しました。", ephemeral=True)

        log_embed = None
        if embed_data := await get_embed_from_db("log_daily_check"):
            log_embed = format_embed_from_db(
                embed_data, user_mention=user.mention, 
                reward=attendance_reward, currency_icon=self.cog.currency_icon
            )
        
        if log_embed:
            try:
                await interaction.channel.send(embed=log_embed)
            except Exception as e:
                logger.error(f"출석체크 공개 로그 메시지 전송 실패 (채널: {interaction.channel.id}): {e}")

            if self.cog.log_channel_id and self.cog.log_channel_id != interaction.channel.id:
                if log_channel := self.cog.bot.get_channel(self.cog.log_channel_id):
                    try:
                        await log_channel.send(embed=log_embed)
                    except Exception as e:
                        logger.error(f"별도 출석체크 로그 채널로 전송 실패: {e}")
        
        await self.cog.regenerate_panel(interaction.channel)


    async def open_quest_view(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        view = QuestView(interaction.user, self.cog)
        embed = await view.build_embed()
        await view.update_components()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class QuestView(ui.View):
    def __init__(self, user: discord.Member, cog_instance: 'Quests'):
        super().__init__(timeout=180)
        self.user = user
        self.cog = cog_instance
        self.current_tab = "daily"

    async def update_view(self, interaction: discord.Interaction):
        # [✅ 수정] 인터랙션이 이미 응답되었는지 확인하는 로직 추가
        if not interaction.response.is_done():
            await interaction.response.defer()
        embed = await self.build_embed()
        await self.update_components()
        await interaction.edit_original_response(embed=embed, view=self)

    async def build_embed(self) -> discord.Embed:
        summary = await get_all_user_stats(self.user.id)
        
        embed = discord.Embed(color=0x2ECC71)
        embed.set_author(name=f"{self.user.display_name}さんのクエスト", icon_url=self.user.display_avatar.url if self.user.display_avatar else None)
        
        stats_to_show = summary.get(self.current_tab, {})
        quests_to_show = DAILY_QUESTS if self.current_tab == "daily" else WEEKLY_QUESTS
        rewards = QUEST_REWARDS[self.current_tab]

        progress_key_map = {"attendance": "check_in_count", "voice": "voice_minutes", "fishing": "fishing_count"}
        
        embed.title = "📅 デイリークエスト" if self.current_tab == "daily" else "🗓️ ウィークリークエスト"
        all_complete = True
        for key, quest in quests_to_show.items():
            db_key = progress_key_map[key]
            current = stats_to_show.get(db_key, 0)
            goal = quest["goal"]
            reward_coin = rewards.get(key, {}).get("coin", 0)
            reward_xp = rewards.get(key, {}).get("xp", 0)
            is_complete = current >= goal
            if not is_complete: all_complete = False
            emoji = "✅" if is_complete else "❌"
            field_name = f"{emoji} {quest['name']}"
            field_value = f"> ` {min(current, goal)} / {goal} `\n> **報酬:** `{reward_coin:,}`{self.cog.currency_icon} + `{reward_xp:,}` XP"
            embed.add_field(name=field_name, value=field_value, inline=False)
        
        if all_complete:
            all_in_reward_coin = rewards['all_complete'].get("coin", 0)
            all_in_reward_xp = rewards['all_complete'].get("xp", 0)
            embed.set_footer(text=f"🎉 すべてのクエスト完了！追加報酬: {all_in_reward_coin:,}{self.cog.currency_icon} + {all_in_reward_xp:,} XP")
        else:
            embed.set_footer(text="クエストを完了して報酬を獲得しましょう！")
        return embed

    async def update_components(self):
        for item in self.children:
            if isinstance(item, ui.Button) and item.custom_id.startswith("tab_"):
                item.style = discord.ButtonStyle.primary if item.custom_id == f"tab_{self.current_tab}" else discord.ButtonStyle.secondary
                item.disabled = item.custom_id == f"tab_{self.current_tab}"

        claim_button = next((child for child in self.children if isinstance(child, ui.Button) and child.custom_id == "claim_rewards_button"), None)
        if not claim_button:
            return

        quests_to_check = DAILY_QUESTS if self.current_tab == "daily" else WEEKLY_QUESTS
        summary = await get_all_user_stats(self.user.id)
        stats_to_check = summary.get(self.current_tab, {})
        
        progress_key_map = {"attendance": "check_in_count", "voice": "voice_minutes", "fishing": "fishing_count"}

        all_quests_complete = True
        for key, quest in quests_to_check.items():
            db_key = progress_key_map[key]
            if stats_to_check.get(db_key, 0) < quest["goal"]:
                all_quests_complete = False
                break
        
        today_str = datetime.now(JST).strftime('%Y-%m-%d')
        week_start_str = (datetime.now(JST) - timedelta(days=datetime.now(JST).weekday())).strftime('%Y-%m-%d')
        period_str = today_str if self.current_tab == "daily" else week_start_str
        cooldown_key = f"quest_claimed_{self.current_tab}_all_{period_str}"
        already_claimed = await get_cooldown(self.user.id, cooldown_key) > 0

        if already_claimed:
            claim_button.label = "今日の報酬を受け取りました"
            claim_button.style = discord.ButtonStyle.secondary
            claim_button.disabled = True
        elif all_quests_complete:
            claim_button.label = "完了したクエストの報酬を受け取る"
            claim_button.style = discord.ButtonStyle.success
            claim_button.disabled = False
        else:
            claim_button.label = "すべてのクエストを完了してください"
            claim_button.style = discord.ButtonStyle.secondary
            claim_button.disabled = True
    
    @ui.button(label="デイリー", style=discord.ButtonStyle.primary, custom_id="tab_daily", disabled=True)
    async def daily_tab_button(self, interaction: discord.Interaction, button: ui.Button):
        self.current_tab = "daily"
        await self.update_view(interaction)

    @ui.button(label="ウィークリー", style=discord.ButtonStyle.secondary, custom_id="tab_weekly")
    async def weekly_tab_button(self, interaction: discord.Interaction, button: ui.Button):
        self.current_tab = "weekly"
        await self.update_view(interaction)
    
    @ui.button(label="報酬を受け取る", style=discord.ButtonStyle.success, emoji="💰", custom_id="claim_rewards_button", row=1)
    async def claim_rewards_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)

        quests_to_check = DAILY_QUESTS if self.current_tab == "daily" else WEEKLY_QUESTS
        rewards = QUEST_REWARDS[self.current_tab]
        
        total_coin_reward = 0
        total_xp_reward = 0
        reward_details = []

        for key, quest in quests_to_check.items():
            reward_info = rewards.get(key, {})
            coin = reward_info.get("coin", 0)
            xp = reward_info.get("xp", 0)
            total_coin_reward += coin
            total_xp_reward += xp
            reward_details.append(f"・{quest['name']}: `{coin:,}`{self.cog.currency_icon} + `{xp:,}` XP")
        
        all_complete_reward = rewards.get("all_complete", {})
        all_coin = all_complete_reward.get("coin", 0)
        all_xp = all_complete_reward.get("xp", 0)
        total_coin_reward += all_coin
        total_xp_reward += all_xp
        reward_details.append(f"・全クエスト完了ボーナス: `{all_coin:,}`{self.cog.currency_icon} + `{all_xp:,}` XP")
        
        today_str = datetime.now(JST).strftime('%Y-%m-%d')
        week_start_str = (datetime.now(JST) - timedelta(days=datetime.now(JST).weekday())).strftime('%Y-%m-%d')
        period_str = today_str if self.current_tab == "daily" else week_start_str
        cooldown_key = f"quest_claimed_{self.current_tab}_all_{period_str}"

        if total_coin_reward > 0 or total_xp_reward > 0:
            await log_activity(self.user.id, f"quest_claim_{self.current_tab}_all", coin_earned=total_coin_reward, xp_earned=total_xp_reward)
            
            if total_coin_reward > 0:
                await update_wallet(self.user, total_coin_reward)

            if total_xp_reward > 0:
                xp_res = await supabase.rpc('add_xp', {'p_user_id': self.user.id, 'p_xp_to_add': total_xp_reward, 'p_source': 'quest'}).execute()
                if xp_res.data:
                    if (level_cog := self.cog.bot.get_cog("LevelSystem")):
                        await level_cog.handle_level_up_event(self.user, xp_res.data)

            await set_cooldown(self.user.id, cooldown_key)
            
            details_text = "\n".join(reward_details)
            await interaction.followup.send(
                f"🎉 **すべての{self.current_tab}クエスト報酬を受け取りました！**\n"
                f"{details_text}\n\n"
                f"**合計:** `{total_coin_reward:,}`{self.cog.currency_icon} と `{total_xp_reward:,}` XP",
                ephemeral=True
            )
        else:
            await interaction.followup.send("❌ 受け取れる報酬がありません。", ephemeral=True)
        
        await self.update_view(interaction)

class Quests(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.currency_icon = "🪙"
        self.log_channel_id: Optional[int] = None

    async def cog_load(self):
        await self.load_configs()

    async def load_configs(self):
        game_config = get_config("GAME_CONFIG", {})
        self.currency_icon = game_config.get("CURRENCY_ICON", "🪙")
        self.log_channel_id = get_id("log_daily_check_channel_id")

    async def register_persistent_views(self):
        self.bot.add_view(TaskBoardView(self))

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_tasks", **kwargs):
        if panel_info := get_panel_id(panel_key):
            if (old_channel := self.bot.get_channel(panel_info['channel_id'])) and (old_message_id := panel_info.get('message_id')):
                try: 
                    msg = await channel.fetch_message(old_message_id)
                    await msg.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass
        
        embed_data = await get_embed_from_db(panel_key)
        if not embed_data: 
            logger.error(f"DB에서 '{panel_key}' 임베드를 찾을 수 없어 패널을 생성할 수 없습니다.")
            return

        embed = discord.Embed.from_dict(embed_data)
        view = TaskBoardView(self)
        
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_key, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} パネルを正常に生成しました。(チャンネル: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(Quests(bot))
