# cogs/games/exploration.py

import discord
from discord.ext import commands, tasks
from discord import ui
import logging
import random
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any
import asyncio
from collections import defaultdict

from utils.database import (
    supabase, get_user_pet, get_exploration_locations, get_exploration_loot,
    start_pet_exploration, get_completed_explorations, update_exploration_message_id,
    get_exploration_by_id, claim_and_end_exploration, update_inventory,
    update_wallet, get_id, get_config, save_panel_id, get_panel_id, get_embed_from_db
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

class ClaimRewardView(ui.View):
    def __init__(self, cog_instance: 'Exploration', exploration_id: int):
        super().__init__(timeout=86400)
        self.cog = cog_instance
        self.exploration_id = exploration_id

    @ui.button(label="보상 수령", style=discord.ButtonStyle.success, emoji="🎁")
    async def claim_reward_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.cog.handle_claim_reward(interaction, self.exploration_id)
        self.stop()

class PetExplorationPanelView(ui.View):
    def __init__(self, cog_instance: 'Exploration'):
        super().__init__(timeout=None)
        self.cog = cog_instance
        self.add_exploration_buttons()

    def add_exploration_buttons(self):
        locations = get_exploration_locations()
        if not locations:
            logger.warning("[PetExplorationPanelView] 탐사 지역 정보가 없어 버튼을 생성할 수 없습니다.")
            return

        self.clear_items()
        row = 0
        for i, loc in enumerate(locations):
            if i % 3 == 0 and i != 0:
                row += 1
            
            button = ui.Button(
                label=loc['name'],
                style=discord.ButtonStyle.secondary,
                custom_id=f"start_exploration:{loc['location_key']}",
                row=row
            )
            button.callback = self.on_location_select
            self.add_item(button)

    async def on_location_select(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        location_key = interaction.data['custom_id'].split(':')[1]
        
        pet = await get_user_pet(interaction.user.id)
        if not pet:
            return await interaction.followup.send("❌ 탐사를 보낼 펫이 없습니다.", ephemeral=True)
        if pet.get('status') == 'exploring':
            return await interaction.followup.send("❌ 펫이 이미 탐사 중입니다.", ephemeral=True)
        
        locations = get_exploration_locations()
        location_data = next((loc for loc in locations if loc['location_key'] == location_key), None)
        
        if not location_data:
            return await interaction.followup.send("❌ 유효하지 않은 탐사 지역입니다.", ephemeral=True)

        if pet.get('level', 0) < location_data.get('required_pet_level', 999):
            return await interaction.followup.send(f"❌ 이 지역은 펫 레벨 {location_data['required_pet_level']} 이상부터 탐사할 수 있습니다.", ephemeral=True)

        await self.cog.start_exploration(interaction, interaction.user, location_data)

class Exploration(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.exploration_completer.start()

    def cog_unload(self):
        self.exploration_completer.cancel()
    
    async def start_exploration(self, interaction: discord.Interaction, user: discord.Member, location: Dict[str, Any]):
        pet = await get_user_pet(user.id)
        if not pet: return

        duration_hours = location['duration_hours']
        start_time = datetime.now(timezone.utc)
        end_time = start_time + timedelta(hours=duration_hours)
        
        new_exploration = await start_pet_exploration(pet['id'], user.id, location['location_key'], start_time, end_time)

        if not new_exploration:
            await interaction.followup.send("❌ 탐사를 시작하는 데 실패했습니다. 다시 시도해주세요.", ephemeral=True)
            return
        
        description_text = (
            f"펫이 **{location['name']}**(으)로 탐사를 떠났습니다.\n\n"
            f"> 완료 예정: {discord.utils.format_dt(end_time, 'R')}"
        )
        embed = discord.Embed(
            title="🧭 탐사 시작",
            description=description_text,
            color=0x5865F2
        )
        if image_url := location.get('image_url'):
            embed.set_image(url=image_url)
        
        await interaction.followup.send(embed=embed, ephemeral=True)
        
        if (pet_cog := self.bot.get_cog("PetSystem")):
            pet_thread_id = pet.get('thread_id')
            if pet_thread_id and (pet_thread := self.bot.get_channel(pet_thread_id)):
                await pet_cog.update_pet_ui(user.id, pet_thread)

    @tasks.loop(minutes=1)
    async def exploration_completer(self):
        try:
            completed_explorations = await get_completed_explorations()
            if not completed_explorations:
                return

            for exp in completed_explorations:
                user_id = int(exp['user_id'])
                pet_id = exp['pet_id']
                
                pet_res = await supabase.table('pets').select('thread_id').eq('id', pet_id).single().execute()
                if not (pet_res.data and (thread_id := pet_res.data.get('thread_id'))):
                    continue

                thread = self.bot.get_channel(thread_id)
                user = self.bot.get_user(user_id)
                if not thread or not user:
                    continue
                
                view = ClaimRewardView(self, exp['id'])

                message = await thread.send(
                    content=f"{user.mention}, 펫이 탐사를 마치고 돌아왔습니다! 아래 버튼을 눌러 보상을 확인하세요.",
                    view=view
                )
                await update_exploration_message_id(exp['id'], message.id)
        except Exception as e:
            logger.error(f"탐사 완료 처리 중 오류: {e}", exc_info=True)
    
    @exploration_completer.before_loop
    async def before_exploration_completer(self):
        await self.bot.wait_until_ready()

    async def handle_claim_reward(self, interaction: discord.Interaction, exploration_id: int):
        exploration_data = await get_exploration_by_id(exploration_id)
        if not exploration_data:
            return await interaction.followup.send("❌ 만료되었거나 잘못된 탐사 정보입니다.", ephemeral=True)
        
        pet_level = exploration_data.get('pets', {}).get('level', 1)
        location = exploration_data.get('exploration_locations', {})
        
        xp_reward = random.randint(location.get('base_xp_min', 0), location.get('base_xp_max', 0))
        coin_reward = random.randint(location.get('base_coin_min', 0), location.get('base_coin_max', 0))
        
        item_rewards = defaultdict(int)
        loot_table = get_exploration_loot(location['location_key'], pet_level)
        for item in loot_table:
            if random.random() < item['drop_chance']:
                qty = random.randint(item['min_qty'], item['max_qty'])
                item_rewards[item['item_name']] += qty
        
        db_tasks = []
        if coin_reward > 0: db_tasks.append(update_wallet(interaction.user, coin_reward))
        if xp_reward > 0: 
            db_tasks.append(
                supabase.rpc('add_xp_to_pet', {'p_user_id': interaction.user.id, 'p_xp_to_add': xp_reward}).execute()
            )
        for item, qty in item_rewards.items():
            db_tasks.append(update_inventory(interaction.user.id, item, qty))
        
        results = await asyncio.gather(*db_tasks, return_exceptions=True)

        await claim_and_end_exploration(exploration_id, exploration_data['pet_id'])

        reward_lines = [
            f"✨ **경험치**: `{xp_reward:,}` XP",
            f"🪙 **코인**: `{coin_reward:,}` 코인"
        ]
        if item_rewards:
            reward_lines.append("\n**획득 아이템:**")
            for item, qty in item_rewards.items():
                reward_lines.append(f"📦 {item}: `{qty}`개")

        await interaction.followup.send(f"🎉 **탐사 보상**\n\n" + "\n".join(reward_lines), ephemeral=True)
        
        try:
            await interaction.message.delete()
        except (discord.NotFound, discord.Forbidden):
            pass

        if (pet_cog := self.bot.get_cog("PetSystem")):
            pet_data = await get_user_pet(interaction.user.id)
            if pet_data and (thread_id := pet_data.get("thread_id")):
                if thread := self.bot.get_channel(thread_id):
                    await pet_cog.update_pet_ui(interaction.user.id, thread, message=None, is_refresh=True)

        for res in results:
            if isinstance(res, dict) and 'data' in res and res.data:
                if isinstance(res.data, list) and res.data[0].get('leveled_up'):
                    if (pet_cog := self.bot.get_cog("PetSystem")):
                        await pet_cog.notify_pet_level_up(
                            interaction.user.id,
                            res.data[0].get('new_level'),
                            res.data[0].get('points_awarded')
                        )
                    break

    async def register_persistent_views(self):
        # 데이터 로드가 완료된 후에 호출되므로, 여기서 View를 생성하고 등록합니다.
        self.bot.add_view(PetExplorationPanelView(self))
        # ClaimRewardView는 동적이므로 여기서 등록하지 않습니다.

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_pet_exploration"):
        panel_name = panel_key.replace("panel_", "")
        if panel_info := get_panel_id(panel_name):
            if old_channel_id := panel_info.get("channel_id"):
                if old_channel := self.bot.get_channel(old_channel_id):
                    try:
                        old_message = await old_channel.fetch_message(panel_info["message_id"])
                        await old_message.delete()
                    except (discord.NotFound, discord.Forbidden): pass
        
        embed_data = await get_embed_from_db(panel_key)
        if not embed_data:
            logger.error(f"DB에서 '{panel_key}' 임베드 템플릿을 찾을 수 없습니다.")
            return

        embed = discord.Embed.from_dict(embed_data)
        view = PetExplorationPanelView(self)
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_name, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} 패널을 #{channel.name} 채널에 성공적으로 생성했습니다.")

async def setup(bot: commands.Bot):
    await bot.add_cog(Exploration(bot))
