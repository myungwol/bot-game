# cogs/games/pet_system.py

import discord
from discord.ext import commands, tasks
from discord import ui
import logging
import random
import os
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any
import asyncio 
import re 
# ▼▼▼ [수정] collections 라이브러리에서 defaultdict를 import 합니다. ▼▼▼
from collections import defaultdict

from utils.database import (
    supabase, get_inventory, update_inventory, get_item_database,
    save_panel_id, get_panel_id, get_embed_from_db, set_cooldown, get_cooldown,
    save_config_to_db, delete_config_from_db, get_id
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
ELEMENT_TO_FILENAME = {
    "불": "fire", "물": "water", "전기": "electric", "풀": "grass",
    "빛": "light", "어둠": "dark"
}
ELEMENT_TO_TYPE = {
    "불": "공격형",
    "물": "방어형",
    "전기": "스피드형",
    "풀": "체력형",
    "빛": "체력/방어형",
    "어둠": "공격/스피드형"
}

def create_bar(current: int, required: int, length: int = 10, full_char: str = '▓', empty_char: str = '░') -> str:
    if required <= 0: return full_char * length
    progress = min(current / required, 1.0)
    filled_length = int(length * progress)
    return f"[{full_char * filled_length}{empty_char * (length - filled_length)}]"

def calculate_xp_for_pet_level(level: int) -> int:
    # 새로운 선형 증가 경험치 공식 적용
    if level < 1: return 0
    # 레벨 L에서 L+1로 가는 데 필요한 경험치: 400 + (100 * L)
    base_xp = 400
    increment = 100
    return base_xp + (increment * level)

async def delete_message_after(message: discord.InteractionMessage, delay: int):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden):
        pass
        
class StatAllocationView(ui.View):
    def __init__(self, parent_view: 'PetUIView', message: discord.Message):
        super().__init__(timeout=180)
        self.parent_view = parent_view
        self.cog = parent_view.cog
        self.user = parent_view.cog.bot.get_user(parent_view.user_id)
        self.pet_data = parent_view.pet_data
        self.message = message
        
        self.points_to_spend = self.pet_data.get('stat_points', 0)
        self.spent_points = {'hp': 0, 'attack': 0, 'defense': 0, 'speed': 0}
        self.lock = asyncio.Lock()

    async def start(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        embed = self.build_embed()
        self.build_components()
        await interaction.followup.send(embed=embed, view=self, ephemeral=True)

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title="✨ 스탯 포인트 분배", color=0xFFD700)
        remaining_points = self.points_to_spend - sum(self.spent_points.values())
        embed.description = f"남은 포인트: **{remaining_points}**"

        base_stats = self.cog.get_base_stats(self.pet_data)
        
        stat_emojis = {'hp': '❤️', 'attack': '⚔️', 'defense': '🛡️', 'speed': '💨'}
        stat_names = {'hp': '체력', 'attack': '공격력', 'defense': '방어력', 'speed': '스피드'}

        for key in ['hp', 'attack', 'defense', 'speed']:
            base = base_stats[key]
            # ▼▼▼ [수정] bonus_ -> natural_bonus_ 로 변경 ▼▼▼
            natural_bonus = self.pet_data.get(f"natural_bonus_{key}", 0)
            allocated = self.pet_data.get(f"allocated_{key}", 0)
            spent = self.spent_points[key]
            total = base + natural_bonus + allocated + spent
            # ▼▼▼ [수정] 기본 스탯 표기를 (자연 성장 + 유저 분배) 형식으로 변경 ▼▼▼
            embed.add_field(
                name=f"{stat_emojis[key]} {stat_names[key]}",
                value=f"`{total}` (`{base + natural_bonus}` + `{allocated + spent}`)",
                inline=False
            )
        return embed

    def build_components(self):
        self.clear_items()
        remaining_points = self.points_to_spend - sum(self.spent_points.values())
        
        self.add_item(self.create_stat_button('hp', 1, '➕❤️', 0, remaining_points <= 0))
        self.add_item(self.create_stat_button('attack', 1, '➕⚔️', 0, remaining_points <= 0))
        self.add_item(self.create_stat_button('defense', 1, '➕🛡️', 0, remaining_points <= 0))
        self.add_item(self.create_stat_button('speed', 1, '➕💨', 0, remaining_points <= 0))
        
        self.add_item(self.create_stat_button('hp', -1, '➖❤️', 1, self.spent_points['hp'] <= 0))
        self.add_item(self.create_stat_button('attack', -1, '➖⚔️', 1, self.spent_points['attack'] <= 0))
        self.add_item(self.create_stat_button('defense', -1, '➖🛡️', 1, self.spent_points['defense'] <= 0))
        self.add_item(self.create_stat_button('speed', -1, '➖💨', 1, self.spent_points['speed'] <= 0))
        
        confirm_button = ui.Button(label="확정", style=discord.ButtonStyle.success, row=2, custom_id="confirm_stats", disabled=(sum(self.spent_points.values()) == 0))
        confirm_button.callback = self.on_confirm
        self.add_item(confirm_button)
        
        cancel_button = ui.Button(label="취소", style=discord.ButtonStyle.grey, row=2, custom_id="cancel_stats")
        cancel_button.callback = self.on_cancel
        self.add_item(cancel_button)

    def create_stat_button(self, stat: str, amount: int, label: str, row: int, disabled: bool) -> ui.Button:
        btn = ui.Button(label=label, row=row, custom_id=f"stat_{stat}_{amount}", disabled=disabled)
        btn.callback = self.on_stat_button_click
        return btn

    async def on_stat_button_click(self, interaction: discord.Interaction):
        async with self.lock:
            _, stat, amount_str = interaction.data['custom_id'].split('_')
            amount = int(amount_str)
            
            if amount > 0:
                remaining_points = self.points_to_spend - sum(self.spent_points.values())
                if remaining_points > 0:
                    self.spent_points[stat] += amount
            else:
                if self.spent_points[stat] > 0:
                    self.spent_points[stat] += amount
            
            self.build_components()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_confirm(self, interaction: discord.Interaction):
        async with self.lock:
            await interaction.response.defer()
            try:
                await supabase.rpc('allocate_pet_stat_points', {
                    'p_user_id': self.user.id,
                    'p_hp_points': self.spent_points['hp'],
                    'p_atk_points': self.spent_points['attack'],
                    'p_def_points': self.spent_points['defense'],
                    'p_spd_points': self.spent_points['speed']
                }).execute()
                
                await self.cog.update_pet_ui(self.user.id, interaction.channel, self.message)
                await interaction.delete_original_response()
                
            except Exception as e:
                logger.error(f"스탯 포인트 분배 DB 업데이트 중 오류: {e}", exc_info=True)
                await interaction.followup.send("❌ 스탯 분배 중 오류가 발생했습니다.", ephemeral=True)

    async def on_cancel(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await interaction.delete_original_response()

class PetNicknameModal(ui.Modal, title="펫 이름 변경"):
    nickname_input = ui.TextInput(label="새로운 이름", placeholder="펫의 새 이름을 입력하세요.", max_length=20)
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.stop()

class ConfirmReleaseView(ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.value = None
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ 본인만 결정할 수 있습니다.", ephemeral=True, delete_after=5)
            return False
        return True
    @ui.button(label="예, 놓아줍니다", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        self.value = True
        self.stop()
        await interaction.response.defer()
    @ui.button(label="아니요", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        self.value = False
        self.stop()
        await interaction.response.defer()

class PetUIView(ui.View):
    def __init__(self, cog_instance: 'PetSystem', user_id: int, pet_data: Dict, play_cooldown_active: bool, evolution_ready: bool):
        super().__init__(timeout=None)
        self.cog = cog_instance
        self.user_id = user_id
        self.pet_data = pet_data
        
        self.feed_pet_button.custom_id = f"pet_feed:{user_id}"
        self.play_with_pet_button.custom_id = f"pet_play:{user_id}"
        self.rename_pet_button.custom_id = f"pet_rename:{user_id}"
        self.release_pet_button.custom_id = f"pet_release:{user_id}"
        self.refresh_button.custom_id = f"pet_refresh:{user_id}"
        self.allocate_stats_button.custom_id = f"pet_allocate_stats:{user_id}"
        self.evolve_button.custom_id = f"pet_evolve:{user_id}"

        if self.pet_data.get('hunger', 0) >= 100:
            self.feed_pet_button.disabled = True
        
        self.play_with_pet_button.disabled = play_cooldown_active
        self.allocate_stats_button.disabled = self.pet_data.get('stat_points', 0) <= 0
        self.evolve_button.disabled = not evolution_ready

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        try:
            target_user_id = int(interaction.data['custom_id'].split(':')[1])
            if interaction.user.id != target_user_id:
                await interaction.response.send_message("❌ 자신의 펫만 돌볼 수 있습니다.", ephemeral=True, delete_after=5)
                return False
            self.user_id = target_user_id
            return True
        except (IndexError, ValueError):
            await interaction.response.send_message("❌ 잘못된 상호작용입니다.", ephemeral=True, delete_after=5)
            return False

    @ui.button(label="스탯 분배", style=discord.ButtonStyle.success, emoji="✨", row=0)
    async def allocate_stats_button(self, interaction: discord.Interaction, button: ui.Button):
        allocation_view = StatAllocationView(self, interaction.message)
        await allocation_view.start(interaction)

    @ui.button(label="먹이주기", style=discord.ButtonStyle.primary, emoji="🍖", row=0)
    async def feed_pet_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        inventory = await get_inventory(interaction.user)
        feed_items = {name: qty for name, qty in inventory.items() if get_item_database().get(name, {}).get('effect_type') == 'pet_feed'}
        if not feed_items:
            return await interaction.followup.send("❌ 펫에게 줄 수 있는 먹이가 없습니다.", ephemeral=True)
        options = [discord.SelectOption(label=f"{name} ({qty}개)", value=name) for name, qty in feed_items.items()]
        feed_select = ui.Select(placeholder="줄 먹이를 선택하세요...", options=options)
        async def feed_callback(select_interaction: discord.Interaction):
            await select_interaction.response.defer()
            item_name = select_interaction.data['values'][0]
            item_data = get_item_database().get(item_name, {})
            hunger_to_add = item_data.get('power', 10)
            await update_inventory(self.user_id, item_name, -1)
            await supabase.rpc('increase_pet_hunger', {'p_user_id': self.user_id, 'p_amount': hunger_to_add}).execute()
            await self.cog.update_pet_ui(self.user_id, interaction.channel, interaction.message)
            msg = await select_interaction.followup.send(f"🍖 {item_name}을(를) 주었습니다. 펫의 배가 든든해졌습니다!", ephemeral=True)
            self.cog.bot.loop.create_task(delete_message_after(msg, 5))
            await select_interaction.delete_original_response()
        feed_select.callback = feed_callback
        view = ui.View(timeout=60).add_item(feed_select)
        await interaction.followup.send("어떤 먹이를 주시겠습니까?", view=view, ephemeral=True)

    @ui.button(label="놀아주기", style=discord.ButtonStyle.primary, emoji="🎾", row=0)
    async def play_with_pet_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        cooldown_key = f"daily_pet_play"
        
        # ▼▼▼ 핵심 수정 ▼▼▼
        pet_id = self.pet_data['id']
        if await self.cog._is_play_on_cooldown(pet_id):
             return await interaction.followup.send("❌ 오늘은 이미 놀아주었습니다. 내일 다시 시도해주세요.", ephemeral=True)
        inventory = await get_inventory(interaction.user)
        if inventory.get("공놀이 세트", 0) < 1:
            return await interaction.followup.send("❌ '공놀이 세트' 아이템이 부족합니다.", ephemeral=True)
            
        await update_inventory(self.user_id, "공놀이 세트", -1)
        
        friendship_amount = 1; stat_increase_amount = 1
        await supabase.rpc('increase_pet_friendship_and_stats', {'p_user_id': self.user_id, 'p_friendship_amount': friendship_amount, 'p_stat_amount': stat_increase_amount}).execute()

        # ▼▼▼ 핵심 수정 ▼▼▼
        await set_cooldown(pet_id, cooldown_key)
        await self.cog.update_pet_ui(self.user_id, interaction.channel, interaction.message)
        
        msg = await interaction.followup.send(f"❤️ 펫과 즐거운 시간을 보냈습니다! 친밀도가 {friendship_amount} 오르고 모든 스탯이 {stat_increase_amount} 상승했습니다.", ephemeral=True)
        self.cog.bot.loop.create_task(delete_message_after(msg, 5))

    @ui.button(label="진화", style=discord.ButtonStyle.success, emoji="🌟", row=0)
    async def evolve_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        await self.cog.handle_evolution(interaction, interaction.message)

    @ui.button(label="이름 변경", style=discord.ButtonStyle.secondary, emoji="✏️", row=1)
    async def rename_pet_button(self, interaction: discord.Interaction, button: ui.Button):
        modal = PetNicknameModal()
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal.nickname_input.value:
            new_name = modal.nickname_input.value
            await supabase.table('pets').update({'nickname': new_name}).eq('user_id', self.user_id).execute()
            if isinstance(interaction.channel, discord.Thread):
                try:
                    await interaction.channel.edit(name=f"🐾｜{new_name}")
                except (discord.Forbidden, discord.HTTPException) as e:
                    logger.warning(f"펫 스레드 이름 변경 실패: {e}")
            await self.cog.update_pet_ui(self.user_id, interaction.channel, interaction.message)
            await interaction.followup.send(f"펫의 이름이 '{new_name}'(으)로 변경되었습니다.", ephemeral=True, delete_after=5)

    @ui.button(label="놓아주기", style=discord.ButtonStyle.danger, emoji="👋", row=1)
    async def release_pet_button(self, interaction: discord.Interaction, button: ui.Button):
        confirm_view = ConfirmReleaseView(self.user_id)
        msg = await interaction.response.send_message(
            "**⚠️ 경고: 펫을 놓아주면 다시는 되돌릴 수 없습니다. 정말로 놓아주시겠습니까?**", 
            view=confirm_view, 
            ephemeral=True
        )
        await confirm_view.wait()
        if confirm_view.value is True:
            await supabase.table('pets').delete().eq('user_id', self.user_id).execute()
            await interaction.edit_original_response(content="펫을 자연으로 돌려보냈습니다...", view=None)
            await interaction.channel.send(f"{interaction.user.mention}님이 펫을 자연의 품으로 돌려보냈습니다.")
            await asyncio.sleep(10)
            try:
                await interaction.channel.delete()
            except (discord.NotFound, discord.Forbidden): pass
        else:
            await interaction.edit_original_response(content="펫 놓아주기를 취소했습니다.", view=None)

    @ui.button(label="새로고침", style=discord.ButtonStyle.secondary, emoji="🔄", row=1)
    async def refresh_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        await self.cog.update_pet_ui(interaction.user.id, interaction.channel, interaction.message, is_refresh=True)

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
        self.active_views_loaded = False
        self.hatch_checker.start()
        self.hunger_and_stat_decay.start()

    def cog_unload(self):
        self.hatch_checker.cancel()
        self.hunger_and_stat_decay.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        if self.active_views_loaded:
            return
        await self.reload_active_pet_views()
        self.active_views_loaded = True

    async def _is_play_on_cooldown(self, pet_id: int) -> bool: # user_id -> pet_id
        cooldown_key = "daily_pet_play"
        last_played_timestamp = await get_cooldown(pet_id, cooldown_key) # user_id -> pet_id
        if last_played_timestamp == 0:
            return False
        
        now_kst = datetime.now(KST)
        last_played_kst = datetime.fromtimestamp(last_played_timestamp, tz=timezone.utc).astimezone(KST)
        
        return now_kst.date() == last_played_kst.date()

    async def _is_evolution_ready(self, pet_data: Dict, inventory: Dict) -> bool:
        if not pet_data: return False
        
        species_info = pet_data.get('pet_species')
        if not species_info: return False

        next_stage_num = pet_data['current_stage'] + 1
        stage_info_json = species_info.get('stage_info', {})
        next_stage_info = stage_info_json.get(str(next_stage_num))

        # 1. 다음 진화 단계 정보가 없으면 진화 불가
        if not next_stage_info:
            return False

        # 2. 레벨이 부족하면 진화 불가
        if pet_data['level'] < next_stage_info.get('level_req', 999):
            return False
        
        # 3. 아이템이 필요한 진화인지 확인
        if 'item' in next_stage_info and 'qty' in next_stage_info:
            required_item = next_stage_info['item']
            required_qty = next_stage_info['qty']
            
            # 3-1. 아이템이 부족하면 진화 불가
            if inventory.get(required_item, 0) < required_qty:
                return False
        
        # 4. 모든 조건을 통과했으므로 진화 가능
        return True

    async def reload_active_pet_views(self):
        logger.info("[PetSystem] 활성화된 펫 관리 UI를 다시 로드합니다...")
        try:
            res = await supabase.table('pets').select('*, pet_species(*)').gt('current_stage', 1).not_.is_('message_id', 'null').execute()
            if not res.data:
                logger.info("[PetSystem] 다시 로드할 활성 펫 UI가 없습니다.")
                return

            all_user_ids = [int(pet['user_id']) for pet in res.data]
            inventories = {}
            if all_user_ids:
                inv_res = await supabase.table('inventories').select('user_id, item_name, quantity').in_('user_id', all_user_ids).execute()
                if inv_res.data:
                    for item in inv_res.data:
                        uid = int(item['user_id'])
                        if uid not in inventories:
                            inventories[uid] = {}
                        inventories[uid][item['item_name']] = item['quantity']
            
            reloaded_count = 0
            for pet_data in res.data:
                user_id = int(pet_data['user_id'])
                message_id = int(pet_data['message_id'])
                user_inventory = inventories.get(user_id, {})
                
                # ▼▼▼ 핵심 수정 ▼▼▼
                cooldown_active = await self._is_play_on_cooldown(pet_data['id'])
                evo_ready = await self._is_evolution_ready(pet_data, user_inventory)
                
                view = PetUIView(self, user_id, pet_data, play_cooldown_active=cooldown_active, evolution_ready=evo_ready)
                self.bot.add_view(view, message_id=message_id)
                reloaded_count += 1
            logger.info(f"[PetSystem] 총 {reloaded_count}개의 펫 관리 UI를 성공적으로 다시 로드했습니다.")
        except Exception as e:
            logger.error(f"활성 펫 UI 로드 중 오류 발생: {e}", exc_info=True)

    @tasks.loop(minutes=30)
    async def hunger_and_stat_decay(self):
        try:
            await supabase.rpc('decrease_all_pets_hunger', {'p_amount': 1}).execute()
            await supabase.rpc('update_pet_stats_on_hunger').execute()
        except Exception as e:
            logger.error(f"펫 배고픔 감소 처리 중 오류: {e}", exc_info=True)
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
            await interaction.followup.send("❌ 펫 기본 정보가 없습니다. 관리자에게 문의해주세요.", ephemeral=True)
            return
        pet_species_data = species_res.data
        pet_species_id = pet_species_data['id']
        base_hatch_seconds = HATCH_TIMES.get(egg_name, 172800)
        random_offset_seconds = random.randint(-21600, 86400)
        final_hatch_seconds = base_hatch_seconds + random_offset_seconds
        now = datetime.now(timezone.utc)
        hatches_at = now + timedelta(seconds=final_hatch_seconds)
        thread = None
        try:
            safe_name = re.sub(r'[^\w\s\-_가-힣]', '', user.display_name).strip()
            if not safe_name: safe_name = f"유저-{user.id}"
            thread_name = f"🥚｜{safe_name}의 알"
            thread = await interaction.channel.create_thread(name=thread_name, type=discord.ChannelType.public_thread, auto_archive_duration=10080)
            await thread.add_user(user)
            pet_insert_res = await supabase.table('pets').insert({
                'user_id': user.id, 'pet_species_id': pet_species_id, 'current_stage': 1, 'level': 0,
                'hatches_at': hatches_at.isoformat(), 'created_at': now.isoformat(), 'thread_id': thread.id
            }).execute()
            await update_inventory(user.id, egg_name, -1)
            pet_data = pet_insert_res.data[0]
            pet_data['pet_species'] = pet_species_data
            embed = self.build_pet_ui_embed(user, pet_data)
            message = await thread.send(embed=embed)
            for i in range(5):
                try:
                    system_start_message = await interaction.channel.fetch_message(thread.id)
                    await system_start_message.delete()
                    break 
                except discord.NotFound: await asyncio.sleep(0.5)
                except discord.Forbidden: break
            await supabase.table('pets').update({'message_id': message.id}).eq('id', pet_data['id']).execute()
            await interaction.edit_original_response(content=f"✅ 부화가 시작되었습니다! {thread.mention} 채널에서 확인해주세요.", view=None)
        except Exception as e:
            logger.error(f"인큐베이션 시작 중 오류 (유저: {user.id}, 알: {egg_name}): {e}", exc_info=True)
            if thread:
                try: await thread.delete()
                except (discord.NotFound, discord.Forbidden): pass
            await interaction.edit_original_response(content="❌ 부화 절차를 시작하는 중 오류가 발생했습니다.", view=None)
            
    def get_base_stats(self, pet_data: Dict) -> Dict[str, int]:
        species_info = pet_data.get('pet_species', {})
        level = pet_data.get('level', 1)
        
        base_hp = species_info.get('base_hp', 0) + (level - 1) * species_info.get('hp_growth', 0)
        base_attack = species_info.get('base_attack', 0) + (level - 1) * species_info.get('attack_growth', 0)
        base_defense = species_info.get('base_defense', 0) + (level - 1) * species_info.get('defense_growth', 0)
        base_speed = species_info.get('base_speed', 0) + (level - 1) * species_info.get('speed_growth', 0)
        
        return {
            'hp': round(base_hp), 
            'attack': round(base_attack), 
            'defense': round(base_defense), 
            'speed': round(base_speed)
        }

    def build_pet_ui_embed(self, user: discord.Member, pet_data: Dict) -> discord.Embed:
        species_info = pet_data.get('pet_species')
        if not species_info: return discord.Embed(title="오류", description="펫 기본 정보를 불러올 수 없습니다.", color=discord.Color.red())
        current_stage = pet_data['current_stage']
        storage_base_url = f"{os.environ.get('SUPABASE_URL')}/storage/v1/object/public/pet_images"
        element_filename = ELEMENT_TO_FILENAME.get(species_info['element'], 'unknown')
        image_url = f"{storage_base_url}/{element_filename}_{current_stage}.png"
        if current_stage == 1:
            embed = discord.Embed(title="🥚 알 부화 진행 중...", color=0xFAFAFA)
            embed.set_author(name=f"{user.display_name}님의 알", icon_url=user.display_avatar.url if user.display_avatar else None)
            embed.set_thumbnail(url=image_url)
            egg_name = f"{species_info['element']}의알"
            embed.add_field(name="부화 중인 알", value=f"`{egg_name}`", inline=False)
            hatches_at = datetime.fromisoformat(pet_data['hatches_at'])
            embed.add_field(name="예상 부화 시간", value=f"{discord.utils.format_dt(hatches_at, style='R')}", inline=False)
            embed.set_footer(text="시간이 되면 자동으로 부화합니다.")
        else:
            stage_info_json = species_info.get('stage_info', {})
            stage_name = stage_info_json.get(str(current_stage), {}).get('name', '알 수 없는 단계')
            embed = discord.Embed(title=f"🐾 {stage_name}: {species_info['species_name']}", color=0xFFD700)
            embed.set_author(name=f"{user.display_name}님의 펫", icon_url=user.display_avatar.url if user.display_avatar else None)
            embed.set_thumbnail(url=image_url)
            nickname = pet_data.get('nickname') or species_info['species_name']
            current_level, current_xp = pet_data['level'], pet_data['xp']
            xp_for_next_level = calculate_xp_for_pet_level(current_level)
            xp_bar = create_bar(current_xp, xp_for_next_level)
            hunger = pet_data.get('hunger', 0)
            hunger_bar = create_bar(hunger, 100, full_char='🟧', empty_char='⬛')
            friendship = pet_data.get('friendship', 0)
            friendship_bar = create_bar(friendship, 100, full_char='❤️', empty_char='🖤')
            
            element = species_info['element']
            pet_type = ELEMENT_TO_TYPE.get(element, "알 수 없음")
            
            stat_points = pet_data.get('stat_points', 0)
            
            description_parts = [
                f"**이름:** {nickname}",
                f"**속성:** {element}",
                f"**타입:** {pet_type}",
                f"**레벨:** {current_level}",
                "",
                f"**경험치:** `{current_xp} / {xp_for_next_level}`",
                f"{xp_bar}",
                "",
                f"**배고픔:** `{hunger} / 100`",
                f"{hunger_bar}",
                "",
                f"**친밀도:** `{friendship} / 100`",
                f"{friendship_bar}"
            ]
            
            if stat_points > 0:
                description_parts.append(f"\n✨ **남은 스탯 포인트: {stat_points}**")

            embed.description = "\n".join(description_parts)
            
            # 현재 능력치는 DB에서 직접 가져옵니다.
            current_stats = {
                'hp': pet_data['current_hp'],
                'attack': pet_data['current_attack'],
                'defense': pet_data['current_defense'],
                'speed': pet_data['current_speed']
            }

            # 부화 시점(Lv.1)의 순수 기본 능력치를 가져옵니다.
            hatch_base_stats = {
                'hp': species_info.get('base_hp', 0),
                'attack': species_info.get('base_attack', 0),
                'defense': species_info.get('base_defense', 0),
                'speed': species_info.get('base_speed', 0)
            }

            # 모든 보너스(레벨업 성장 + 부화 보너스 + 분배 스탯)를 계산합니다.
            total_bonus_stats = {
                'hp': current_stats['hp'] - hatch_base_stats['hp'],
                'attack': current_stats['attack'] - hatch_base_stats['attack'],
                'defense': current_stats['defense'] - hatch_base_stats['defense'],
                'speed': current_stats['speed'] - hatch_base_stats['speed']
            }

            # 요청하신 새로운 형식으로 필드를 추가합니다.
            embed.add_field(name="❤️ 체력", value=f"**{current_stats['hp']}** (`{hatch_base_stats['hp']}` + `{total_bonus_stats['hp']}`)", inline=True)
            embed.add_field(name="⚔️ 공격력", value=f"**{current_stats['attack']}** (`{hatch_base_stats['attack']}` + `{total_bonus_stats['attack']}`)", inline=True)
            embed.add_field(name="🛡️ 방어력", value=f"**{current_stats['defense']}** (`{hatch_base_stats['defense']}` + `{total_bonus_stats['defense']}`)", inline=True)
            embed.add_field(name="💨 스피드", value=f"**{current_stats['speed']}** (`{hatch_base_stats['speed']}` + `{total_bonus_stats['speed']}`)", inline=True)
        return embed
    async def process_hatching(self, pet_data: Dict):
        user_id = int(pet_data['user_id'])
        user = self.bot.get_user(user_id)
        if not user: return
        created_at, hatches_at = datetime.fromisoformat(pet_data['created_at']), datetime.fromisoformat(pet_data['hatches_at'])
        base_duration = timedelta(seconds=172800)
        bonus_duration = (hatches_at - created_at) - base_duration
        bonus_points = max(0, int(bonus_duration.total_seconds() / 3600))
        species_info = pet_data['pet_species']
        
        final_stats = {"hp": species_info['base_hp'], "attack": species_info['base_attack'], "defense": species_info['base_defense'], "speed": species_info['base_speed']}
        natural_bonus_stats = {"hp": 0, "attack": 0, "defense": 0, "speed": 0}
        stats_keys = list(final_stats.keys())
        for _ in range(bonus_points):
            stat_to_increase = random.choice(stats_keys)
            final_stats[stat_to_increase] += 1
            natural_bonus_stats[stat_to_increase] += 1
            
        updated_pet_data_res = await supabase.table('pets').update({
            'current_stage': 2, 'level': 1, 'xp': 0, 'hunger': 100, 'friendship': 0,
            'current_hp': final_stats['hp'], 'current_attack': final_stats['attack'],
            'current_defense': final_stats['defense'], 'current_speed': final_stats['speed'],
            'nickname': species_info['species_name'],
            'natural_bonus_hp': natural_bonus_stats['hp'], 
            'natural_bonus_attack': natural_bonus_stats['attack'],
            'natural_bonus_defense': natural_bonus_stats['defense'], 
            'natural_bonus_speed': natural_bonus_stats['speed']
        }).eq('id', pet_data['id']).execute()
        
        updated_pet_data = updated_pet_data_res.data[0]
        updated_pet_data['pet_species'] = species_info
        thread = self.bot.get_channel(pet_data['thread_id'])
        if thread:
            try:
                message = await thread.fetch_message(pet_data['message_id'])
                hatched_embed = self.build_pet_ui_embed(user, updated_pet_data)
                cooldown_active = await self._is_play_on_cooldown(user_id)
                evo_ready = await self._is_evolution_ready(updated_pet_data, {})
                view = PetUIView(self, user_id, updated_pet_data, play_cooldown_active=cooldown_active, evolution_ready=evo_ready)
                await message.edit(embed=hatched_embed, view=view) 
                await thread.send(f"{user.mention} 님의 알이 부화했습니다!")
                await thread.edit(name=f"🐾｜{species_info['species_name']}")
            except (discord.NotFound, discord.Forbidden) as e:
                logger.error(f"부화 UI 업데이트 실패 (스레드: {thread.id}): {e}")
    
    async def process_levelup_requests(self, requests: List[Dict], is_admin: bool = False):
        user_ids_to_notify = {int(req['config_key'].split('_')[-1]): req.get('config_value') for req in requests}
        
        for user_id, payload in user_ids_to_notify.items():
            new_level, points_awarded = None, None
            
            if is_admin:
                logger.info(f"[펫 레벨업 디버깅] 유저 {user_id}의 관리자 레벨업 요청 처리 시작.")
                pet_res = await supabase.table('pets').select('level, xp').eq('user_id', user_id).maybe_single().execute()
                
                if pet_res and pet_res.data:
                    current_level = pet_res.data.get('level', 1)
                    current_xp_in_level = pet_res.data.get('xp', 0) # 현재 레벨에서 쌓인 경험치
                    logger.info(f"[펫 레벨업 디버깅] 현재 펫 상태: 레벨={current_level}, XP={current_xp_in_level}")

                    # ▼▼▼ 핵심 수정 ▼▼▼
                    # 현재 레벨을 기준으로 레벨업에 필요한 총량을 계산합니다.
                    xp_for_this_level = calculate_xp_for_pet_level(current_level)
                    # 필요한 총량에서 현재 쌓인 경험치를 빼서, 레벨업까지 남은 경험치를 계산합니다.
                    xp_to_add = (xp_for_this_level - current_xp_in_level) + 1
                    # ▲▲▲ 핵심 수정 ▲▲▲

                    logger.info(f"[펫 레벨업 디버깅] XP 계산: 이번 레벨 필요 XP={xp_for_this_level}, 추가할 XP={xp_to_add}")

                    if xp_to_add > 0:
                        res = await supabase.rpc('add_xp_to_pet', {'p_user_id': user_id, 'p_xp_to_add': xp_to_add}).execute()
                        logger.info(f"[펫 레벨업 디버깅] 'add_xp_to_pet' RPC 응답: {res.data}")
                        
                        if res.data and res.data[0].get('leveled_up'):
                            new_level = res.data[0].get('new_level')
                            points_awarded = res.data[0].get('points_awarded')
                            logger.info(f"[펫 레벨업 디버깅] 레벨업 성공 감지: new_level={new_level}, points_awarded={points_awarded}")
                        else:
                            logger.warning(f"[펫 레벨업 디버깅] RPC 응답에서 'leveled_up'이 true가 아니거나 데이터가 없습니다.")
                    else:
                        logger.warning(f"[펫 레벨업 디버깅] 추가할 XP가 0 이하({xp_to_add})이므로 RPC 호출을 건너뜁니다.")
                else:
                    logger.warning(f"[펫 레벨업 디버깅] 유저 {user_id}의 펫 정보를 DB에서 찾을 수 없습니다.")
            else: 
                if isinstance(payload, dict):
                    new_level = payload.get('new_level')
                    points_awarded = payload.get('points_awarded')

            if new_level is not None and points_awarded is not None:
                await self.notify_pet_level_up(user_id, new_level, points_awarded)
            else:
                logger.warning(f"펫 레벨업 알림 실패: 유저 {user_id}의 new_level 또는 points_awarded를 결정할 수 없습니다.")

    # ▼▼▼▼▼ 여기에 아래 코드를 추가하세요 ▼▼▼▼▼
    async def process_level_set_requests(self, requests: List[Dict]):
        for req in requests:
            try:
                user_id = int(req['config_key'].split('_')[-1])
                payload = req.get('config_value', {})
                exact_level = payload.get('exact_level')

                if exact_level is None:
                    continue
                
                # 레벨에 해당하는 총 경험치를 계산합니다.
                total_xp_for_level = 0
                for l in range(1, exact_level):
                    # ▼▼▼ 핵심 수정: 새로운 공식으로 변경 ▼▼▼
                    total_xp_for_level += (400 + (100 * l))
                
                # DB 함수를 호출하여 레벨과 경험치를 직접 설정합니다.
                res = await supabase.rpc('set_pet_level_and_xp', {
                    'p_user_id': user_id,
                    'p_new_level': exact_level,
                    'p_new_xp': 0, # 해당 레벨의 시작 경험치로 설정
                    'p_total_xp': total_xp_for_level
                }).execute()

                if res.data and res.data[0].get('success'):
                    points_awarded = res.data[0].get('points_awarded', 0)
                    await self.notify_pet_level_up(user_id, exact_level, points_awarded)
                    logger.info(f"관리자 요청으로 {user_id}의 펫 레벨을 {exact_level}로 설정했습니다.")
                else:
                    logger.error(f"관리자 펫 레벨 설정 DB 함수 호출 실패: {res.data}")
            except Exception as e:
                logger.error(f"펫 레벨 설정 요청 처리 중 오류: {e}", exc_info=True)
    # ▲▲▲▲▲ 여기까지 추가 ▲▲▲▲▲

    async def notify_pet_level_up(self, user_id: int, new_level: int, points_awarded: int):
        pet_data = await self.get_user_pet(user_id)
        if not pet_data:
            return

        user = self.bot.get_user(user_id)
        if not user:
            return

        # 펫의 닉네임을 가져옵니다.
        nickname = pet_data.get('nickname', '이름 없는 펫')

        # 새로 설정한 로그 채널로 알림을 보냅니다.
        log_channel_id = get_id("log_pet_levelup_channel_id")
        if log_channel_id and (log_channel := self.bot.get_channel(log_channel_id)):
            message_text = (
                f"🎉 {user.mention}님의 '**{nickname}**'이(가) **레벨 {new_level}**(으)로 성장했습니다! "
                f"스탯 포인트 **{points_awarded}**개를 획득했습니다. ✨"
            )
            try:
                await log_channel.send(message_text)
            except Exception as e:
                logger.error(f"펫 레벨업 로그 전송 실패: {e}")

        # 기존 펫 스레드의 UI는 계속 업데이트합니다.
        if thread_id := pet_data.get('thread_id'):
            if thread := self.bot.get_channel(thread_id):
                if message_id := pet_data.get('message_id'):
                    try:
                        message = await thread.fetch_message(message_id)
                        await self.update_pet_ui(user_id, thread, message)
                    except (discord.NotFound, discord.Forbidden):
                        logger.warning(f"펫 레벨업 후 UI 업데이트 실패: 메시지(ID: {message_id})를 찾을 수 없습니다.")

    async def check_and_process_auto_evolution(self, user_ids: set):
        for user_id in user_ids:
            try:
                res = await supabase.rpc('trigger_pet_auto_evolution', {'p_user_id': user_id}).single().execute()
                if res.data and res.data.get('evolved'):
                    await self.notify_pet_evolution(user_id, res.data.get('new_stage'), res.data.get('points_granted'))
            except Exception as e:
                logger.error(f"자동 진화 처리 중 오류 (유저: {user_id}): {e}", exc_info=True)

    async def notify_pet_evolution(self, user_id: int, new_stage_num: int, points_granted: int):
        pet_data = await self.get_user_pet(user_id)
        if not pet_data or not (thread_id := pet_data.get('thread_id')):
            return

        species_info = pet_data.get('pet_species', {})
        stage_info_json = species_info.get('stage_info', {})
        new_stage_name = stage_info_json.get(str(new_stage_num), {}).get('name', '새로운 모습')
        
        if thread := self.bot.get_channel(thread_id):
            user = self.bot.get_user(user_id)
            if user:
                await thread.send(f"🌟 {user.mention}님의 펫이 **{new_stage_name}**(으)로 진화했습니다! 스탯 포인트 **{points_granted}**개를 획득했습니다!")
            
            if message_id := pet_data.get('message_id'):
                try:
                    message = await thread.fetch_message(message_id)
                    await self.update_pet_ui(user_id, thread, message)
                except (discord.NotFound, discord.Forbidden):
                    pass

    async def handle_evolution(self, interaction: discord.Interaction, message: discord.Message):
        user_id = interaction.user.id
        res = await supabase.rpc('attempt_pet_evolution', {'p_user_id': user_id}).single().execute()
        
        if res.data and res.data.get('success'):
            await self.notify_pet_evolution(user_id, res.data.get('new_stage'), res.data.get('points_granted'))
        else:
            await interaction.followup.send("❌ 진화 조건을 만족하지 못했습니다. 레벨과 필요 아이템을 확인해주세요.", ephemeral=True, delete_after=10)

    async def update_pet_ui(self, user_id: int, channel: discord.TextChannel, message: discord.Message, is_refresh: bool = False):
        pet_data, inventory = await asyncio.gather(self.get_user_pet(user_id), get_inventory(self.bot.get_user(user_id)))
        if not pet_data:
            await message.edit(content="펫 정보를 찾을 수 없습니다.", embed=None, view=None)
            return
        user = self.bot.get_user(user_id)
        embed = self.build_pet_ui_embed(user, pet_data)
        # ▼▼▼ 핵심 수정 ▼▼▼
        cooldown_active = await self._is_play_on_cooldown(pet_data['id'])
        evo_ready = await self._is_evolution_ready(pet_data, inventory)
        view = PetUIView(self, user_id, pet_data, play_cooldown_active=cooldown_active, evolution_ready=evo_ready)
        if is_refresh:
            try: await message.delete()
            except (discord.NotFound, discord.Forbidden): pass
            new_message = await channel.send(embed=embed, view=view)
            await supabase.table('pets').update({'message_id': new_message.id}).eq('user_id', user_id).execute()
        else:
            await message.edit(embed=embed, view=view)
            
    async def register_persistent_views(self):
        self.bot.add_view(IncubatorPanelView(self))
        logger.info("✅ 펫 시스템(인큐베이터)의 영구 View가 성공적으로 등록되었습니다.")
        
    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_incubator"):
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
            logger.error(f"DB에서 '{panel_key}'에 대한 임베드 데이터를 찾을 수 없어 패널 생성을 중단합니다.")
            return
        embed = discord.Embed.from_dict(embed_data)
        view = IncubatorPanelView(self)
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_name, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} 패널을 #{channel.name} 채널에 성공적으로 생성했습니다.")

class IncubatorPanelView(ui.View):
    def __init__(self, cog_instance: 'PetSystem'):
        super().__init__(timeout=None)
        self.cog = cog_instance
    @ui.button(label="알 부화시키기", style=discord.ButtonStyle.secondary, emoji="🥚", custom_id="incubator_start")
    async def start_incubation_button(self, interaction: discord.Interaction, button: ui.Button):
        if await self.cog.get_user_pet(interaction.user.id):
            await interaction.response.send_message("❌ 이미 펫을 소유하고 있습니다. 펫은 한 마리만 키울 수 있습니다.", ephemeral=True, delete_after=5)
            return
        await interaction.response.defer(ephemeral=True, thinking=False)
        # ▼▼▼ [수정] self 대신 self.cog를 전달합니다. ▼▼▼
        view = EggSelectView(interaction.user, self.cog)
        await view.start(interaction)

async def setup(bot: commands.Bot):
    await bot.add_cog(PetSystem(bot))
