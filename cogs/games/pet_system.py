# cogs/games/pet_system.py

import discord
from discord.ext import commands, tasks
from discord import ui
import logging
import random
import os
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any

from utils.database import (
    supabase, get_inventory, update_inventory, get_item_database,
    save_panel_id, get_panel_id, get_embed_from_db
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

HATCH_TIMES = {
    "랜덤 펫 알": 172800, "불의알": 172800, "물의알": 172800,
    "전기알": 172800, "풀의알": 172800, "빛의알": 172800, "어둠의알": 172800,
}

EGG_TO_ELEMENT = {
    "불의알": "불", "물의알": "물", "전기알": "전기", "풀의알": "풀",
    "빛의알": "빛", "어둠의알": "어둠",
}
ELEMENTS = ["불", "물", "전기", "풀", "빛", "어둠"]

# ▼▼▼ [추가] DB 속성 이름과 이미지 파일명 앞부분을 매핑합니다. ▼▼▼
ELEMENT_TO_FILENAME = {
    "불": "fire", "물": "water", "전기": "electric", "풀": "grass",
    "빛": "light", "어둠": "dark"
}

class EggSelectView(ui.View):
    def __init__(self, user: discord.Member, cog_instance: 'PetSystem'):
        super().__init__(timeout=180)
        self.user = user
        self.cog = cog_instance
        self.message: Optional[discord.WebhookMessage] = None

    async def start(self, interaction: discord.Interaction):
        inventory = await get_inventory(self.user)
        egg_items = {name: qty for name, qty in inventory.items() if get_item_database().get(name, {}).get('category') == '알'}

        if not egg_items:
            await interaction.followup.send("❌ 부화시킬 수 있는 알이 없습니다.", ephemeral=True)
            return

        options = [discord.SelectOption(label=f"{name} ({qty}개 보유)", value=name) for name, qty in egg_items.items()]
        select = ui.Select(placeholder="부화시킬 알을 선택하세요...", options=options)
        select.callback = self.select_callback
        self.add_item(select)
        
        self.message = await interaction.followup.send("어떤 알을 부화기에 넣으시겠습니까?", view=self, ephemeral=True)

    async def select_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        egg_name = interaction.data['values'][0]

        for item in self.children:
            item.disabled = True
        await self.message.edit(content=f"'{egg_name}'을 선택했습니다. 부화 절차를 시작합니다...", view=self)

        await self.cog.start_incubation_process(interaction, egg_name)


class PetSystem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.hatch_checker.start()

    def cog_unload(self):
        self.hatch_checker.cancel()

    @tasks.loop(seconds=30)
    async def hatch_checker(self):
        try:
            now = datetime.now(timezone.utc)
            res = await supabase.table('pets').select('*, pet_species(*)').eq('current_stage', 1).lte('hatches_at', now.isoformat()).execute()
            
            if not res.data:
                return

            for pet_data in res.data:
                await self.process_hatching(pet_data)

        except Exception as e:
            logger.error(f"펫 부화 확인 중 오류 발생: {e}", exc_info=True)

    @hatch_checker.before_loop
    async def before_hatch_checker(self):
        await self.bot.wait_until_ready()

    async def get_user_pet(self, user_id: int) -> Optional[Dict]:
        res = await supabase.table('pets').select('*, pet_species(*)').eq('user_id', user_id).maybe_single().execute()
        return res.data if res and res.data else None

    async def start_incubation_process(self, interaction: discord.Interaction, egg_name: str):
        user = interaction.user

        element = EGG_TO_ELEMENT.get(egg_name) if egg_name != "랜덤 펫 알" else random.choice(ELEMENTS)
        species_res = await supabase.table('pet_species').select('*').eq('element', element).limit(1).maybe_single().execute()
        
        if not (species_res and species_res.data):
            logger.error(f"'{element}' 속성의 펫 종류(species)를 DB에서 찾을 수 없습니다.")
            await interaction.followup.send("❌ 펫 기본 정보가 없습니다. 관리자에게 문의해주세요.", ephemeral=True)
            return

        pet_species_data = species_res.data
        pet_species_id = pet_species_data['id']

        base_hatch_seconds = HATCH_TIMES.get(egg_name, 172800)
        random_offset_seconds = random.randint(-21600, 86400)
        final_hatch_seconds = base_hatch_seconds + random_offset_seconds
        
        now = datetime.now(timezone.utc)
        hatches_at = now + timedelta(seconds=final_hatch_seconds)

        try:
            thread = await interaction.channel.create_thread(
                name=f"🥚｜{user.display_name}의 알",
                type=discord.ChannelType.private_thread,
                auto_archive_duration=10080
            )
            await thread.add_user(user)

            pet_insert_res = await supabase.table('pets').insert({
                'user_id': user.id,
                'pet_species_id': pet_species_id,
                'current_stage': 1,
                'level': 0,
                'hatches_at': hatches_at.isoformat(),
                'created_at': now.isoformat(),
                'thread_id': thread.id
            }).select().single().execute()

            await update_inventory(user.id, egg_name, -1)

            pet_data = pet_insert_res.data
            pet_data['pet_species'] = pet_species_data # 수동으로 species 정보 추가

            embed = self.build_pet_ui_embed(user, pet_data)
            message = await thread.send(embed=embed)

            await supabase.table('pets').update({'message_id': message.id}).eq('id', pet_data['id']).execute()

            await interaction.edit_original_response(content=f"✅ 부화가 시작되었습니다! {thread.mention} 채널에서 확인해주세요.", view=None)

        except Exception as e:
            logger.error(f"인큐베이션 시작 중 오류 (유저: {user.id}, 알: {egg_name}): {e}", exc_info=True)
            await interaction.edit_original_response(content="❌ 부화 절차를 시작하는 중 오류가 발생했습니다.", view=None)
    
    # ▼▼▼ [수정] build_incubation_embed 와 build_hatched_pet_embed 를 하나로 통합 ▼▼▼
    def build_pet_ui_embed(self, user: discord.Member, pet_data: Dict) -> discord.Embed:
        species_info = pet_data.get('pet_species')
        if not species_info:
            return discord.Embed(title="오류", description="펫 기본 정보를 불러올 수 없습니다.", color=discord.Color.red())

        current_stage = pet_data['current_stage']
        
        # --- 이미지 URL 동적 생성 ---
        storage_base_url = f"{os.environ.get('SUPABASE_URL')}/storage/v1/object/public/pet_images"
        element_filename = ELEMENT_TO_FILENAME.get(species_info['element'], 'unknown')
        image_url = f"{storage_base_url}/{element_filename}_{current_stage}.png"

        # --- UI 분기 처리 ---
        if current_stage == 1: # 알 상태
            embed = discord.Embed(title="🥚 알 부화 진행 중...", color=0xFAFAFA)
            embed.set_author(name=f"{user.display_name}님의 알", icon_url=user.display_avatar.url if user.display_avatar else None)
            embed.set_thumbnail(url=image_url)
            
            # DB에 egg_name 컬럼이 없으므로, species_info로 유추
            egg_name = f"{species_info['element']}의알"
            embed.add_field(name="부화 중인 알", value=f"`{egg_name}`", inline=False)
            
            hatches_at = datetime.fromisoformat(pet_data['hatches_at'])
            embed.add_field(name="예상 부화 시간", value=f"{discord.utils.format_dt(hatches_at, style='R')}", inline=False)
            embed.set_footer(text="시간이 되면 자동으로 부화합니다.")

        else: # 부화 후 상태
            stage_info_json = species_info.get('stage_info', {})
            stage_name = stage_info_json.get(str(current_stage), {}).get('name', '알 수 없는 단계')

            embed = discord.Embed(title=f"🐾 {stage_name}: {species_info['species_name']}", color=0xFFD700)
            embed.set_author(name=f"{user.display_name}님의 펫", icon_url=user.display_avatar.url if user.display_avatar else None)
            embed.set_thumbnail(url=image_url)

            nickname = pet_data.get('nickname') or species_info['species_name']
            embed.description = f"**이름:** {nickname}\n**레벨:** {pet_data['level']}\n**속성:** {species_info['element']}"
            
            embed.add_field(name="❤️ 체력", value=str(pet_data['current_hp']), inline=True)
            embed.add_field(name="⚔️ 공격력", value=str(pet_data['current_attack']), inline=True)
            embed.add_field(name="🛡️ 방어력", value=str(pet_data['current_defense']), inline=True)
            embed.add_field(name="💨 스피드", value=str(pet_data['current_speed']), inline=True)

        return embed
    # ▲▲▲ [수정] 완료 ▲▲▲

    async def process_hatching(self, pet_data: Dict):
        user_id = int(pet_data['user_id'])
        user = self.bot.get_user(user_id)
        if not user:
            logger.warning(f"부화 처리 중 유저(ID: {user_id})를 찾을 수 없어 스킵합니다.")
            return

        created_at = datetime.fromisoformat(pet_data['created_at'])
        hatches_at = datetime.fromisoformat(pet_data['hatches_at'])
        base_duration = timedelta(seconds=172800) # 2일 기본
        
        bonus_duration = (hatches_at - created_at) - base_duration
        bonus_points = max(0, int(bonus_duration.total_seconds() / 3600))

        species_info = pet_data['pet_species']
        final_stats = {
            "hp": species_info['base_hp'], "attack": species_info['base_attack'],
            "defense": species_info['base_defense'], "speed": species_info['base_speed']
        }
        
        stats_keys = list(final_stats.keys())
        for _ in range(bonus_points):
            stat_to_buff = random.choice(stats_keys)
            final_stats[stat_to_buff] += 1
            
        updated_pet_data = await supabase.table('pets').update({
            'current_stage': 2, 'level': 1, 'xp': 0,
            'current_hp': final_stats['hp'], 'current_attack': final_stats['attack'],
            'current_defense': final_stats['defense'], 'current_speed': final_stats['speed'],
            'nickname': species_info['species_name']
        }).eq('id', pet_data['id']).select().single().execute()
        
        updated_pet_data.data['pet_species'] = species_info # species 정보 다시 합치기

        thread = self.bot.get_channel(pet_data['thread_id'])
        if thread:
            try:
                message = await thread.fetch_message(pet_data['message_id'])
                hatched_embed = self.build_pet_ui_embed(user, updated_pet_data.data)
                
                await message.edit(embed=hatched_embed, view=None) 
                await thread.send(f"{user.mention} 님의 알이 부화했습니다!")
                await thread.edit(name=f"🐾｜{species_info['species_name']}")
            except (discord.NotFound, discord.Forbidden) as e:
                logger.error(f"부화 UI 업데이트 실패 (스레드: {thread.id}): {e}")

class IncubatorPanelView(ui.View):
    def __init__(self, cog_instance: 'PetSystem'):
        super().__init__(timeout=None)
        self.cog = cog_instance

    @ui.button(label="알 부화시키기", style=discord.ButtonStyle.secondary, emoji="🥚", custom_id="incubator_start")
    async def start_incubation_button(self, interaction: discord.Interaction):
        # 1인 1펫 규칙 확인
        if await self.cog.get_user_pet(interaction.user.id):
            await interaction.response.send_message("❌ 이미 펫을 소유하고 있습니다. 펫은 한 마리만 키울 수 있습니다.", ephemeral=True, delete_after=5)
            return

        await interaction.response.defer(ephemeral=True, thinking=False)
        view = EggSelectView(interaction.user, self.cog)
        await view.start(interaction)

async def setup(bot: commands.Bot):
    await bot.add_cog(PetSystem(bot))
