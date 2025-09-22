# cogs/games/pet_system.py

import discord
from discord.ext import commands, tasks
from discord import ui
import logging
import random
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any

from utils.database import (
    supabase, get_inventory, update_inventory, get_item_database,
    save_panel_id, get_panel_id, get_embed_from_db
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

# 알 아이템 이름과 기본 부화 시간(초) 설정
HATCH_TIMES = {
    "랜덤 펫 알": 172800, "불의알": 172800, "물의알": 172800,
    "전기알": 172800, "풀의알": 172800, "빛의알": 172800, "어둠의알": 172800,
}

# 알 이름과 해당 속성 매핑
EGG_TO_ELEMENT = {
    "불의알": "불", "물의알": "물", "전기알": "전기", "풀의알": "풀",
    "빛의알": "빛", "어둠의알": "어둠",
}
ELEMENTS = ["불", "물", "전기", "풀", "빛", "어둠"]

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

        # 모든 컴포넌트 비활성화
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
            # 부화 시간이 지났고, 아직 알 상태(stage=1)인 펫들을 조회
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
        res = await supabase.table('pets').select('*').eq('user_id', user_id).maybe_single().execute()
        return res.data if res and res.data else None

    async def start_incubation_process(self, interaction: discord.Interaction, egg_name: str):
        user = interaction.user

        element = EGG_TO_ELEMENT.get(egg_name) if egg_name != "랜덤 펫 알" else random.choice(ELEMENTS)
        species_res = await supabase.table('pet_species').select('id').eq('element', element).limit(1).maybe_single().execute()
        
        if not (species_res and species_res.data):
            logger.error(f"'{element}' 속성의 펫 종류(species)를 DB에서 찾을 수 없습니다.")
            await interaction.followup.send("❌ 펫 기본 정보가 없습니다. 관리자에게 문의해주세요.", ephemeral=True)
            return

        pet_species_id = species_res.data['id']

        base_hatch_seconds = HATCH_TIMES.get(egg_name, 172800)
        random_offset_seconds = random.randint(-21600, 86400) # -6시간 ~ +24시간
        final_hatch_seconds = base_hatch_seconds + random_offset_seconds
        
        now = datetime.now(timezone.utc)
        hatches_at = now + timedelta(seconds=final_hatch_seconds)

        try:
            thread = await interaction.channel.create_thread(
                name=f"🥚｜{user.display_name}의 알",
                type=discord.ChannelType.private_thread,
                auto_archive_duration=10080 # 1주일
            )
            await thread.add_user(user)

            # DB에 펫 생성 (아직 message_id는 null)
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
            embed = self.build_incubation_embed(user, egg_name, hatches_at)
            message = await thread.send(embed=embed)

            # 생성된 메시지 ID를 DB에 업데이트
            await supabase.table('pets').update({'message_id': message.id}).eq('id', pet_data['id']).execute()

            await interaction.edit_original_response(content=f"✅ 부화가 시작되었습니다! {thread.mention} 채널에서 확인해주세요.", view=None)

        except Exception as e:
            logger.error(f"인큐베이션 시작 중 오류 (유저: {user.id}, 알: {egg_name}): {e}", exc_info=True)
            await interaction.edit_original_response(content="❌ 부화 절차를 시작하는 중 오류가 발생했습니다.", view=None)

    def build_incubation_embed(self, user: discord.User, egg_name: str, hatches_at: datetime) -> discord.Embed:
        embed = discord.Embed(title="🥚 알 부화 진행 중...", color=0xFAFAFA)
        embed.set_author(name=f"{user.display_name}님의 알", icon_url=user.display_avatar.url if user.display_avatar else None)
        embed.add_field(name="부화 중인 알", value=f"`{egg_name}`", inline=False)
        embed.add_field(name="예상 부화 시간", value=f"{discord.utils.format_dt(hatches_at, style='R')}", inline=False)
        embed.set_footer(text="시간이 되면 자동으로 부화합니다.")
        return embed

    def build_hatched_pet_embed(self, user: discord.Member, pet_data: Dict) -> discord.Embed:
        species_info = pet_data['pet_species']
        embed = discord.Embed(title=f"🎉 {species_info['species_name']} 탄생! 🎉", color=0xFFD700)
        
        # Supabase Storage URL 구조 (프로젝트에 맞게 'project_id'와 'bucket_name'을 확인해야 할 수 있습니다)
        # 예시: https://<project_id>.supabase.co/storage/v1/object/public/<bucket_name>/fire_dragon.png
        # 아래는 일반적인 구조입니다. 'pet_images' 버킷이 있다고 가정합니다.
        storage_base_url = f"{os.environ.get('SUPABASE_URL')}/storage/v1/object/public/pet_images"
        image_name = species_info['species_name'].replace(" ", "_").lower() # "화염 드래곤" -> "화염_드래곤"
        thumbnail_url = f"{storage_base_url}/{image_name}.png"
        
        embed.set_thumbnail(url=thumbnail_url)
        embed.set_author(name=f"{user.display_name}님의 펫", icon_url=user.display_avatar.url if user.display_avatar else None)
        
        nickname = pet_data.get('nickname') or species_info['species_name']
        embed.description = f"**이름:** {nickname}\n**레벨:** {pet_data['level']}\n**속성:** {species_info['element']}"
        
        embed.add_field(name="❤️ 체력", value=str(pet_data['current_hp']), inline=True)
        embed.add_field(name="⚔️ 공격력", value=str(pet_data['current_attack']), inline=True)
        embed.add_field(name="🛡️ 방어력", value=str(pet_data['current_defense']), inline=True)
        embed.add_field(name="💨 스피드", value=str(pet_data['current_speed']), inline=True)
        
        return embed

    async def process_hatching(self, pet_data: Dict):
        user_id = int(pet_data['user_id'])
        user = self.bot.get_user(user_id)
        if not user:
            logger.warning(f"부화 처리 중 유저(ID: {user_id})를 찾을 수 없어 스킵합니다.")
            return

        # 부화 시간 보너스 스탯 계산
        created_at = datetime.fromisoformat(pet_data['created_at'])
        hatches_at = datetime.fromisoformat(pet_data['hatches_at'])
        base_duration = timedelta(seconds=HATCH_TIMES.get(pet_data.get('egg_name', '랜덤 펫 알'), 172800))
        
        bonus_duration = (hatches_at - created_at) - base_duration
        bonus_points = max(0, int(bonus_duration.total_seconds() / 3600)) # 1시간당 1포인트

        species_info = pet_data['pet_species']
        final_stats = {
            "hp": species_info['base_hp'], "attack": species_info['base_attack'],
            "defense": species_info['base_defense'], "speed": species_info['base_speed']
        }

        # 보너스 포인트를 스탯에 랜덤하게 분배
        stats_keys = list(final_stats.keys())
        for _ in range(bonus_points):
            stat_to_buff = random.choice(stats_keys)
            final_stats[stat_to_buff] += 1
            
        # DB 업데이트
        await supabase.table('pets').update({
            'current_stage': 2, # 유년기
            'level': 1,
            'xp': 0,
            'current_hp': final_stats['hp'],
            'current_attack': final_stats['attack'],
            'current_defense': final_stats['defense'],
            'current_speed': final_stats['speed'],
            'nickname': species_info['species_name'] # 초기 닉네임은 종족 이름으로
        }).eq('id', pet_data['id']).execute()
        
        # UI 업데이트
        thread = self.bot.get_channel(pet_data['thread_id'])
        if thread:
            try:
                message = await thread.fetch_message(pet_data['message_id'])
                hatched_embed = self.build_hatched_pet_embed(user, {**pet_data, **{'current_hp': final_stats['hp'], 'current_attack': final_stats['attack'], 'current_defense': final_stats['defense'], 'current_speed': final_stats['speed']}})
                
                # TODO: 추후 펫 관리 View로 교체
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
    async def start_incubation_button(self, interaction: discord.Interaction, button: ui.Button):
        view = EggSelectView(interaction.user, self.cog)
        await view.start(interaction)

async def setup(bot: commands.Bot):
    await bot.add_cog(PetSystem(bot))
