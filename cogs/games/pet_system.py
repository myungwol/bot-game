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

from utils.database import (
    supabase, get_inventory, update_inventory, get_item_database,
    save_panel_id, get_panel_id, get_embed_from_db, set_cooldown, get_cooldown,
    save_config_to_db, delete_config_from_db
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
    if level < 1: return 80
    return int(80 * (level ** 1.2))

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
        if await self.cog._is_play_on_cooldown(interaction.user.id):
             return await interaction.followup.send("❌ 오늘은 이미 놀아주었습니다. 내일 다시 시도해주세요.", ephemeral=True)
        inventory = await get_inventory(interaction.user)
        if inventory.get("공놀이 세트", 0) < 1:
            return await interaction.followup.send("❌ '공놀이 세트' 아이템이 부족합니다.", ephemeral=True)
            
        await update_inventory(self.user_id, "공놀이 세트", -1)
        
        friendship_amount = 1; stat_increase_amount = 1
        await supabase.rpc('increase_pet_friendship_and_stats', {'p_user_id': self.user_id, 'p_friendship_amount': friendship_amount, 'p_stat_amount': stat_increase_amount}).execute()

        await set_cooldown(interaction.user.id, cooldown_key)
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
            cooldown_key = f"daily_pet_play"
            await supabase.table('cooldowns').delete().eq('user_id', self.user_id).eq('cooldown_key', cooldown_key).execute()
            logger.info(f"펫을 놓아주면서 {self.user_id}의 '{cooldown_key}' 쿨다운을 초기화했습니다.")
            
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

    async def _is_play_on_cooldown(self, user_id: int) -> bool:
        cooldown_key = "daily_pet_play"
        last_played = await get_cooldown(user_id, cooldown_key)
        if last_played == 0:
            return False
        today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        last_played_date = datetime.fromtimestamp(last_played, tz=timezone.utc).strftime('%Y-%m-%d')
        return today_str == last_played_date

    # ▼▼▼ [추가] _is_evolution_ready 함수 추가 ▼▼▼
    async def _is_evolution_ready(self, pet_data: Dict, inventory: Dict) -> bool:
        if not pet_data: return False
        
        species_info = pet_data.get('pet_species')
        if not species_info: return False

        next_stage_num = pet_data['current_stage'] + 1
        stage_info_json = species_info.get('stage_info', {})
        next_stage_info = stage_info_json.get(str(next_stage_num))

        if not next_stage_info: return False

        if 'item' not in next_stage_info: return False

        if pet_data['level'] < next_stage_info['level_req']: return False
        
        required_item = next_stage_info['item']
        required_qty = next_stage_info['qty']
        
        if inventory.get(required_item, 0) < required_qty: return False

        return True

    # ▼▼▼ [수정] reload_active_pet_views 함수 수정 ▼▼▼
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
                
                cooldown_active = await self._is_play_on_cooldown(user_id)
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
            
            base_stats = self.get_base_stats(pet_data)
            
            # (레벨 성장 + 자연 보너스)를 기본 스탯으로 합산
            base_with_natural_bonus = {
                'hp': base_stats['hp'] + pet_data.get('natural_bonus_hp', 0),
                'attack': base_stats['attack'] + pet_data.get('natural_bonus_attack', 0),
                'defense': base_stats['defense'] + pet_data.get('natural_bonus_defense', 0),
                'speed': base_stats['speed'] + pet_data.get('natural_bonus_speed', 0),
            }
            
            # 유저가 분배한 스탯
            allocated_stats = {
                'hp': pet_data.get('allocated_hp', 0),
                'attack': pet_data.get('allocated_attack', 0),
                'defense': pet_data.get('allocated_defense', 0),
                'speed': pet_data.get('allocated_speed', 0),
            }

            # 현재 스탯 (배고픔 페널티 등이 적용될 수 있음)
            current_stats = {
                'hp': pet_data['current_hp'],
                'attack': pet_data['current_attack'],
                'defense': pet_data['current_defense'],
                'speed': pet_data['current_speed']
            }

            embed.add_field(name="❤️ 체력", value=f"{current_stats['hp']} (`{base_with_natural_bonus['hp']}` + `{allocated_stats['hp']}`)", inline=True)
            embed.add_field(name="⚔️ 공격력", value=f"{current_stats['attack']} (`{base_with_natural_bonus['attack']}` + `{allocated_stats['attack']}`)", inline=True)
            embed.add_field(name="🛡️ 방어력", value=f"{current_stats['defense']} (`{base_with_natural_bonus['defense']}` + `{allocated_stats['defense']}`)", inline=True)
            embed.add_field(name="💨 스피드", value=f"{current_stats['speed']} (`{base_with_natural_bonus['speed']}` + `{allocated_stats['speed']}`)", inline=True)
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
        # ▼▼▼ [수정] 부화 보너스를 natural_bonus_ 컬럼에 저장하도록 변경 ▼▼▼
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
    
    # ▼▼▼ [수정] 관리자 요청과 일반 레벨업 요청을 모두 처리하도록 함수 수정 ▼▼▼
    async def process_levelup_requests(self, requests: List[Dict]):
        user_ids_to_notify = {int(req['config_key'].split('_')[-1]): req['config_value'] for req in requests}
        
        for user_id, payload in user_ids_to_notify.items():
            new_level, points_awarded = None, None
            
            # payload가 비어있으면 관리자 테스트 명령어로 간주하고 DB 함수를 호출
            if not payload: 
                res = await supabase.rpc('admin_level_up_pet', {'p_user_id': user_id}).single().execute()
                if res.data and res.data.get('leveled_up'):
                    new_level = res.data.get('new_level')
                    points_awarded = res.data.get('points_awarded')
            # payload에 정보가 있으면 일반 레벨업으로 간주
            else:
                new_level = payload.get('new_level')
                points_awarded = payload.get('points_awarded')

            if new_level is not None and points_awarded is not None:
                await self.notify_pet_level_up(user_id, new_level, points_awarded)
            else:
                logger.warning(f"펫 레벨업 알림 실패: 유저 {user_id}의 new_level 또는 points_awarded를 결정할 수 없습니다.")

    async def notify_pet_level_up(self, user_id: int, new_level: int, points_awarded: int):
        pet_data = await self.get_user_pet(user_id)
        if not pet_data or not (thread_id := pet_data.get('thread_id')):
            return
        
        if thread := self.bot.get_channel(thread_id):
            user = self.bot.get_user(user_id)
            if user:
                await thread.send(f"🎉 {user.mention} 님의 펫이 **레벨 {new_level}** (으)로 성장했습니다! 스탯 포인트 **{points_awarded}**개를 획득했습니다. ✨")
            
            if message_id := pet_data.get('message_id'):
                try:
                    message = await thread.fetch_message(message_id)
                    await self.update_pet_ui(user_id, thread, message)
                except (discord.NotFound, discord.Forbidden):
                    logger.warning(f"펫 레벨업 후 UI 업데이트 실패: 메시지(ID: {message_id})를 찾을 수 없습니다.")

    async def update_pet_ui(self, user_id: int, channel: discord.TextChannel, message: discord.Message, is_refresh: bool = False):
        pet_data, inventory = await asyncio.gather(self.get_user_pet(user_id), get_inventory(self.bot.get_user(user_id)))
        if not pet_data:
            await message.edit(content="펫 정보를 찾을 수 없습니다.", embed=None, view=None)
            return
        user = self.bot.get_user(user_id)
        embed = self.build_pet_ui_embed(user, pet_data)
        cooldown_active = await self._is_play_on_cooldown(user_id)
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
        view = EggSelectView(interaction.user, self.cog)
        await view.start(interaction)
async def setup(bot: commands.Bot):
    await bot.add_cog(PetSystem(bot))
