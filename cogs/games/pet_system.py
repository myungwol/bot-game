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
from collections import defaultdict
from postgrest.exceptions import APIError
from discord import app_commands

from utils.database import (
    supabase, get_inventory, update_inventory, get_item_database,
    save_panel_id, get_panel_id, get_embed_from_db, set_cooldown, get_cooldown,
    save_config_to_db, delete_config_from_db, get_id, get_user_pet,
    get_wallet, update_wallet,
    get_inventories_for_users
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

HATCH_TIMES = {
    "ランダムペットの卵": 172800, "火の卵": 172800, "水の卵": 172800,
    "電気の卵": 172800, "草の卵": 172800, "光の卵": 172800, "闇の卵": 172800,
}
EGG_TO_ELEMENT = {
    "火の卵": "火", "水の卵": "水", "電気の卵": "電気", "草の卵": "草",
    "光の卵": "光", "闇の卵": "闇",
}
ELEMENTS = ["火", "水", "電気", "草", "光", "闇"]
ELEMENT_TO_FILENAME = {
    "火": "fire", "水": "water", "電気": "electric", "草": "grass",
    "光": "light", "闇": "dark"
}
ELEMENT_TO_TYPE = {
    "火": "攻撃型",
    "水": "防御型",
    "電気": "スピード型",
    "草": "体力型",
    "光": "体力/防御型",
    "闇": "攻撃/スピード型"
}

def create_bar(current: int, required: int, length: int = 10, full_char: str = '▓', empty_char: str = '░') -> str:
    if required <= 0: return full_char * length
    progress = min(current / required, 1.0)
    filled_length = int(length * progress)
    return f"[{full_char * filled_length}{empty_char * (length - filled_length)}]"

def calculate_xp_for_pet_level(level: int) -> int:
    if level < 1: return 0
    base_xp = 400
    increment = 100
    return base_xp + (increment * level)

async def delete_message_after(message: discord.WebhookMessage, delay: int):
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
        embed = discord.Embed(title="✨ ステータスポイント分配", color=0xFFD700)
        remaining_points = self.points_to_spend - sum(self.spent_points.values())
        embed.description = f"残りポイント: **{remaining_points}**"

        base_stats = self.cog.get_base_stats(self.pet_data)
        
        stat_emojis = {'hp': '❤️', 'attack': '⚔️', 'defense': '🛡️', 'speed': '💨'}
        stat_names = {'hp': '体力', 'attack': '攻撃力', 'defense': '防御力', 'speed': 'スピード'}

        for key in ['hp', 'attack', 'defense', 'speed']:
            base = base_stats[key]
            natural_bonus = self.pet_data.get(f"natural_bonus_{key}", 0)
            allocated = self.pet_data.get(f"allocated_{key}", 0)
            spent = self.spent_points[key]
            total = base + natural_bonus + allocated + spent
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
        
        confirm_button = ui.Button(label="確定", style=discord.ButtonStyle.success, row=2, custom_id="confirm_stats", disabled=(sum(self.spent_points.values()) == 0))
        confirm_button.callback = self.on_confirm
        self.add_item(confirm_button)
        
        cancel_button = ui.Button(label="キャンセル", style=discord.ButtonStyle.grey, row=2, custom_id="cancel_stats")
        cancel_button.callback = self.on_cancel
        self.add_item(cancel_button)

    def create_stat_button(self, stat: str, amount: int, label: str, row: int, disabled: bool) -> ui.Button:
        btn = ui.Button(label=label, row=row, custom_id=f"stat_{stat}_{amount}", disabled=disabled)
        btn.callback = self.on_stat_button_click
        return btn

    async def on_stat_button_click(self, interaction: discord.Interaction):
        await interaction.response.defer()
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
            await interaction.edit_original_response(embed=self.build_embed(), view=self)

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
                await interaction.followup.send("❌ ステータス分配中にエラーが発生しました。", ephemeral=True)

    async def on_cancel(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await interaction.delete_original_response()

class PetNicknameModal(ui.Modal, title="ペットの名前を変更"):
    nickname_input = ui.TextInput(label="新しい名前", placeholder="ペットの新しい名前を入力してください。", max_length=20)
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
            await interaction.response.send_message("❌ 本人のみ決定できます。", ephemeral=True, delete_after=5)
            return False
        return True
    @ui.button(label="はい、手放します", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        self.value = True
        self.stop()
        await interaction.response.defer()
    @ui.button(label="いいえ", style=discord.ButtonStyle.secondary)
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
        
        is_exploring = pet_data.get('status') == 'exploring'

        self.allocate_stats_button.custom_id = f"pet_allocate_stats:{user_id}"
        self.feed_pet_button.custom_id = f"pet_feed:{user_id}"
        self.play_with_pet_button.custom_id = f"pet_play:{user_id}"
        self.rename_pet_button.custom_id = f"pet_rename:{user_id}"
        self.release_pet_button.custom_id = f"pet_release:{user_id}"
        self.refresh_button.custom_id = f"pet_refresh:{user_id}"
        self.evolve_button.custom_id = f"pet_evolve:{user_id}"

        self.allocate_stats_button.disabled = self.pet_data.get('stat_points', 0) <= 0 or is_exploring
        self.feed_pet_button.disabled = self.pet_data.get('hunger', 0) >= 100 or is_exploring
        self.play_with_pet_button.disabled = play_cooldown_active or is_exploring
        self.evolve_button.disabled = not evolution_ready or is_exploring
        self.rename_pet_button.disabled = is_exploring
        self.release_pet_button.disabled = is_exploring

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        try:
            target_user_id = int(interaction.data['custom_id'].split(':')[1])
            if interaction.user.id != target_user_id:
                await interaction.response.send_message("❌ 自分のペットのみ世話をすることができます。", ephemeral=True, delete_after=5)
                return False
            self.user_id = target_user_id
            return True
        except (IndexError, ValueError):
            await interaction.response.send_message("❌ 無効なインタラクションです。", ephemeral=True, delete_after=5)
            return False

    @ui.button(label="ステータス分配", style=discord.ButtonStyle.success, emoji="✨", row=0)
    async def allocate_stats_button(self, interaction: discord.Interaction, button: ui.Button):
        allocation_view = StatAllocationView(self, interaction.message)
        await allocation_view.start(interaction)

    @ui.button(label="エサやり", style=discord.ButtonStyle.primary, emoji="🍖", row=0)
    async def feed_pet_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        inventory = await get_inventory(interaction.user)
        feed_items = {name: qty for name, qty in inventory.items() if get_item_database().get(name, {}).get('effect_type') == 'pet_feed'}
        if not feed_items:
            return await interaction.followup.send("❌ ペットにあげられるエサがありません。", ephemeral=True)
        options = [discord.SelectOption(label=f"{name} ({qty}個)", value=name) for name, qty in feed_items.items()]
        feed_select = ui.Select(placeholder="あげるエサを選択してください...", options=options)
        async def feed_callback(select_interaction: discord.Interaction):
            await select_interaction.response.defer()
            item_name = select_interaction.data['values'][0]
            item_data = get_item_database().get(item_name, {})
            hunger_to_add = item_data.get('power', 10)
            await update_inventory(self.user_id, item_name, -1)
            await supabase.rpc('increase_pet_hunger', {'p_user_id': self.user_id, 'p_amount': hunger_to_add}).execute()
            await self.cog.update_pet_ui(self.user_id, interaction.channel, interaction.message)
            msg = await select_interaction.followup.send(f"🍖 {item_name}をあげました。ペットがお腹いっぱいになりました！", ephemeral=True)
            self.cog.bot.loop.create_task(delete_message_after(msg, 5))
            await select_interaction.delete_original_response()
        feed_select.callback = feed_callback
        view = ui.View(timeout=60).add_item(feed_select)
        await interaction.followup.send("どのエサをあげますか？", view=view, ephemeral=True)

    @ui.button(label="遊ぶ", style=discord.ButtonStyle.primary, emoji="🎾", row=0)
    async def play_with_pet_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        cooldown_key = f"daily_pet_play"
        pet_id = self.pet_data['id']
        if await self.cog._is_play_on_cooldown(pet_id):
             return await interaction.followup.send("❌ 今日はすでに遊びました。また明日試してください。", ephemeral=True)
        inventory = await get_inventory(interaction.user)
        if inventory.get("ボール遊びセット", 0) < 1:
            return await interaction.followup.send("❌ 'ボール遊びセット'アイテムが不足しています。", ephemeral=True)
        await update_inventory(self.user_id, "ボール遊びセット", -1)
        friendship_amount = 1; stat_increase_amount = 1
        await supabase.rpc('increase_pet_friendship_and_stats', {'p_user_id': self.user_id, 'p_friendship_amount': friendship_amount, 'p_stat_amount': stat_increase_amount}).execute()
        await set_cooldown(pet_id, cooldown_key)
        await self.cog.update_pet_ui(self.user_id, interaction.channel, interaction.message)
        msg = await interaction.followup.send(f"❤️ ペットと楽しい時間を過ごしました！親密度が{friendship_amount}上がり、すべてのステータスが{stat_increase_amount}上昇しました。", ephemeral=True)
        self.cog.bot.loop.create_task(delete_message_after(msg, 5))

    @ui.button(label="進化", style=discord.ButtonStyle.success, emoji="🌟", row=0)
    async def evolve_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        success = await self.cog.handle_evolution(interaction.user.id, interaction.channel)
        if not success:
            await interaction.followup.send("❌ 進化条件を満たしていません。レベルと必要アイテムを確認してください。", ephemeral=True, delete_after=10)

    @ui.button(label="名前の変更", style=discord.ButtonStyle.secondary, emoji="✏️", row=1)
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
            msg = await interaction.followup.send(f"ペットの名前が'{new_name}'に変更されました。", ephemeral=True)
            self.cog.bot.loop.create_task(delete_message_after(msg, 5))

    @ui.button(label="手放す", style=discord.ButtonStyle.danger, emoji="👋", row=1)
    async def release_pet_button(self, interaction: discord.Interaction, button: ui.Button):
        confirm_view = ConfirmReleaseView(self.user_id)
        msg = await interaction.response.send_message(
            "**⚠️ 警告: ペットを手放すと二度と戻ってきません。本当に手放しますか？**", 
            view=confirm_view, 
            ephemeral=True
        )
        await confirm_view.wait()
        if confirm_view.value is True:
            try:
                await supabase.table('pets').delete().eq('user_id', self.user_id).execute()
                await interaction.edit_original_response(content="ペットを自然に返しました...", view=None)
                await interaction.channel.send(f"{interaction.user.mention}さんがペットを自然の懐に返しました。")
                await asyncio.sleep(10)
                try:
                    await interaction.channel.delete()
                except (discord.NotFound, discord.Forbidden): pass
            except APIError as e:
                logger.error(f"펫 놓아주기 처리 중 DB 오류 발생: {e}", exc_info=True)
                await interaction.edit_original_response(content="❌ ペットを手放す際にエラーが発生しました。管理者に問い合わせてください。", view=None)
        else:
            await interaction.edit_original_response(content="ペットを手放すのをキャンセルしました。", view=None)

    @ui.button(label="更新", style=discord.ButtonStyle.secondary, emoji="🔄", row=1)
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
            await interaction.followup.send("❌ 孵化させることができる卵がありません。", ephemeral=True)
            return
        options = [discord.SelectOption(label=f"{name} ({qty}個保有)", value=name) for name, qty in egg_items.items()]
        select = ui.Select(placeholder="孵化させる卵を選択してください...", options=options)
        select.callback = self.select_callback
        self.add_item(select)
        self.message = await interaction.followup.send("どの卵を孵化器に入れますか？", view=self, ephemeral=True)
    async def select_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        egg_name = interaction.data['values'][0]
        for item in self.children:
            item.disabled = True
        await self.message.edit(content=f"'{egg_name}'を選択しました。孵化手続きを開始します...", view=self)
        await self.cog.start_incubation_process(interaction, egg_name)

class IncubatorPanelView(ui.View):
    def __init__(self, cog_instance: 'PetSystem'):
        super().__init__(timeout=None)
        self.cog = cog_instance
    @ui.button(label="卵を孵化させる", style=discord.ButtonStyle.secondary, emoji="🥚", custom_id="incubator_start")
    async def start_incubation_button(self, interaction: discord.Interaction, button: ui.Button):
        if await get_user_pet(interaction.user.id):
            await interaction.response.send_message("❌ すでにペットを所有しています。ペットは一匹しか飼えません。", ephemeral=True, delete_after=5)
            return
        await interaction.response.defer(ephemeral=True, thinking=False)
        view = EggSelectView(interaction.user, self.cog)
        await view.start(interaction)

class PetSystem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_views_loaded = False

    async def cog_load(self):
        self.hatch_checker.start()
        self.hunger_and_stat_decay.start()
        self.auto_refresh_pet_uis.start()

    def cog_unload(self):
        self.hatch_checker.cancel()
        self.hunger_and_stat_decay.cancel()
        self.auto_refresh_pet_uis.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        if self.active_views_loaded:
            return
        await self.reload_active_pet_views()
        self.active_views_loaded = True

    async def _is_play_on_cooldown(self, pet_id: int) -> bool:
        cooldown_key = "daily_pet_play"
        last_played_timestamp = await get_cooldown(pet_id, cooldown_key)
        if last_played_timestamp == 0:
            return False
        
        now_jst = datetime.now(JST)
        last_played_jst = datetime.fromtimestamp(last_played_timestamp, tz=timezone.utc).astimezone(JST)
        
        return now_jst.date() == last_played_jst.date()

    async def _is_evolution_ready(self, pet_data: Dict, inventory: Dict) -> bool:
        if not pet_data: return False
        
        species_info = pet_data.get('pet_species')
        if not species_info: return False

        current_stage_num = pet_data.get('current_stage', 0)
        next_stage_num = current_stage_num + 1
        
        stage_info_json = species_info.get('stage_info', {})
        current_stage_info = stage_info_json.get(str(current_stage_num))
        next_stage_info = stage_info_json.get(str(next_stage_num))

        if not (current_stage_info and next_stage_info): return False
        if pet_data.get('level', 0) < current_stage_info.get('level_cap', 999): return False
        
        required_items = next_stage_info.get('items', {})
        if not required_items: return True

        for item, qty in required_items.items():
            if inventory.get(item, 0) < qty:
                return False
        
        return True

    async def reload_active_pet_views(self):
        logger.info("[PetSystem] 활성화된 펫 관리 UI를 다시 로드합니다...")
        try:
            res = await supabase.table('pets').select('*, pet_species(*)').gt('current_stage', 1).not_.is_('message_id', 'null').execute()
            if not res.data:
                logger.info("[PetSystem] 다시 로드할 활성 펫 UI가 없습니다.")
                return

            all_user_ids = [int(pet['user_id']) for pet in res.data]
            inventories = await get_inventories_for_users(all_user_ids)
            
            reloaded_count = 0
            for pet_data in res.data:
                user_id = int(pet_data['user_id'])
                message_id = int(pet_data['message_id'])
                user_inventory = inventories.get(user_id, {})
                
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
            await supabase.rpc('process_pet_hunger_decay', {'p_amount': 1}).execute()
        except Exception as e:
            logger.error(f"펫 배고픔 및 스탯 감소 처리 중 오류: {e}", exc_info=True)

    @tasks.loop(minutes=5)
    async def auto_refresh_pet_uis(self):
        logger.info("[Pet UI Auto-Refresh] 모든 활성 펫 UI의 자동 새로고침을 시작합니다.")
        try:
            res = await supabase.table('pets').select('*').gt('current_stage', 1).not_.is_('message_id', 'null').not_.is_('thread_id', 'null').execute()
            if not (res and res.data):
                logger.info("[Pet UI Auto-Refresh] 새로고침할 활성 펫 UI가 없습니다.")
                return

            stale_sessions_to_clear = []
            logger.info(f"[Pet UI Auto-Refresh] {len(res.data)}개의 활성 펫 UI를 새로고침합니다.")

            for pet_data in res.data:
                try:
                    user_id = int(pet_data['user_id'])
                    thread_id = int(pet_data['thread_id'])
                    message_id = int(pet_data['message_id'])

                    user = self.bot.get_user(user_id)
                    thread = self.bot.get_channel(thread_id)
                    
                    if not user or not thread:
                        stale_sessions_to_clear.append(pet_data['id'])
                        logger.warning(f"유저(ID:{user_id}) 또는 스레드(ID:{thread_id})를 찾을 수 없어 펫 UI를 정리합니다.")
                        continue

                    message = await thread.fetch_message(message_id)
                    await self.update_pet_ui(user_id, thread, message)
                    await asyncio.sleep(1.5)

                except discord.NotFound:
                    stale_sessions_to_clear.append(pet_data['id'])
                    logger.warning(f"펫 메시지(ID:{message_id})를 찾을 수 없어 UI를 정리합니다.")
                except Exception as e:
                    logger.error(f"펫 UI 자동 새로고침 중 개별 처리 오류 (Pet ID: {pet_data.get('id')}): {e}", exc_info=True)

            if stale_sessions_to_clear:
                logger.info(f"[Pet UI Auto-Refresh] {len(stale_sessions_to_clear)}개의 비활성 세션 정보를 DB에서 정리합니다.")
                await supabase.table('pets').update({'message_id': None, 'thread_id': None}).in_('id', stale_sessions_to_clear).execute()

        except Exception as e:
            logger.error(f"펫 UI 자동 새로고침 루프에서 오류 발생: {e}", exc_info=True)

    @auto_refresh_pet_uis.before_loop
    async def before_auto_refresh_pet_uis(self):
        await self.bot.wait_until_ready()

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

    async def start_incubation_process(self, interaction: discord.Interaction, egg_name: str):
        user = interaction.user
        element = EGG_TO_ELEMENT.get(egg_name) if egg_name != "ランダムペットの卵" else random.choice(ELEMENTS)
        species_res = await supabase.table('pet_species').select('*').eq('element', element).limit(1).maybe_single().execute()
        if not (species_res and species_res.data):
            await interaction.followup.send("❌ ペットの基本情報がありません。管理者に問い合わせてください。", ephemeral=True)
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
            safe_name = re.sub(r'[^\w\s\-_]', '', user.display_name).strip() # 일본어 환경을 위해 가-힣 제거
            if not safe_name: safe_name = f"ユーザー-{user.id}"
            thread_name = f"🥚｜{safe_name}の卵"
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
            await interaction.edit_original_response(content=f"✅ 孵化が始まりました！{thread.mention}チャンネルで確認してください。", view=None)
        except Exception as e:
            logger.error(f"인큐베이션 시작 중 오류 (유저: {user.id}, 알: {egg_name}): {e}", exc_info=True)
            if thread:
                try: await thread.delete()
                except (discord.NotFound, discord.Forbidden): pass
            await interaction.edit_original_response(content="❌ 孵化手続きの開始中にエラーが発生しました。", view=None)
            
    def get_base_stats(self, pet_data: Dict) -> Dict[str, int]:
        species_info = pet_data.get('pet_species', {})
        level = pet_data.get('level', 1)
        
        base_hp = species_info.get('base_hp', 0) + (level - 1) * species_info.get('hp_growth', 0)
        base_attack = species_info.get('base_attack', 0) + (level - 1) * species_info.get('attack_growth', 0)
        base_defense = species_info.get('base_defense', 0) + (level - 1) * species_info.get('defense_growth', 0)
        base_speed = species_info.get('base_speed', 0) + (level - 1) * species_info.get('speed_growth', 0)
        
        return { 'hp': round(base_hp), 'attack': round(base_attack), 'defense': round(base_defense), 'speed': round(base_speed) }

    def build_pet_ui_embed(self, user: discord.Member, pet_data: Dict) -> discord.Embed:
        species_info = pet_data.get('pet_species')
        if not species_info: return discord.Embed(title="エラー", description="ペットの基本情報を読み込めません。", color=discord.Color.red())
        current_stage = pet_data['current_stage']
        storage_base_url = f"{os.environ.get('SUPABASE_URL')}/storage/v1/object/public/pet_images"
        element_filename = ELEMENT_TO_FILENAME.get(species_info['element'], 'unknown')
        image_url = f"{storage_base_url}/{element_filename}_{current_stage}.png"
        if current_stage == 1:
            embed = discord.Embed(title="🥚 卵の孵化進行中...", color=0xFAFAFA)
            embed.set_author(name=f"{user.display_name}さんの卵", icon_url=user.display_avatar.url if user.display_avatar else None)
            embed.set_thumbnail(url=image_url)
            egg_name = f"{species_info['element']}の卵"
            embed.add_field(name="孵化中の卵", value=f"`{egg_name}`", inline=False)
            hatches_at = datetime.fromisoformat(pet_data['hatches_at'])
            embed.add_field(name="予想孵化時間", value=f"{discord.utils.format_dt(hatches_at, style='R')}", inline=False)
            embed.set_footer(text="時間になると自動で孵化します。")
        else:
            stage_info_json = species_info.get('stage_info', {})
            current_stage_info = stage_info_json.get(str(current_stage), {})
            stage_name = current_stage_info.get('name', '不明な段階')
            level_cap = current_stage_info.get('level_cap', 100)
            
            nickname = pet_data.get('nickname') or species_info['species_name']
            
            embed = discord.Embed(title=f"🐾 {nickname}", color=0xFFD700)
            embed.set_author(name=f"{user.display_name}さんのペット", icon_url=user.display_avatar.url if user.display_avatar else None)
            embed.set_thumbnail(url=image_url)

            current_level, current_xp = pet_data['level'], pet_data['xp']
            xp_for_next_level = calculate_xp_for_pet_level(current_level)
            xp_bar = create_bar(current_xp, xp_for_next_level)
            
            hunger = pet_data.get('hunger', 0); hunger_bar = create_bar(hunger, 100, full_char='🟧', empty_char='⬛')
            friendship = pet_data.get('friendship', 0); friendship_bar = create_bar(friendship, 100, full_char='❤️', empty_char='🖤')

            pet_status = pet_data.get('status', 'idle')
            status_text = "休憩中 💤"
            if pet_status == 'exploring':
                end_time = datetime.fromisoformat(pet_data['exploration_end_time'])
                status_text = f"探検中... (完了: {discord.utils.format_dt(end_time, 'R')})"

            embed.add_field(name="段階", value=f"**{stage_name}**", inline=True)
            embed.add_field(name="タイプ", value=f"{ELEMENT_TO_TYPE.get(species_info['element'], '不明')}", inline=True)
            embed.add_field(name="レベル", value=f"**Lv. {current_level} / {level_cap}**", inline=True)
            embed.add_field(name="経験値", value=f"`{current_xp} / {xp_for_next_level}`\n{xp_bar}", inline=False)
            embed.add_field(name="空腹", value=f"`{hunger} / 100`\n{hunger_bar}", inline=False)
            embed.add_field(name="親密度", value=f"`{friendship} / 100`\n{friendship_bar}", inline=False)

            stat_points = pet_data.get('stat_points', 0)
            if stat_points > 0:
                embed.add_field(name="✨ 残りステータスポイント", value=f"**{stat_points}**", inline=False)

            hatch_base_stats = {
                'hp': species_info.get('base_hp', 0) + pet_data.get('natural_bonus_hp', 0),
                'attack': species_info.get('base_attack', 0) + pet_data.get('natural_bonus_attack', 0),
                'defense': species_info.get('base_defense', 0) + pet_data.get('natural_bonus_defense', 0),
                'speed': species_info.get('base_speed', 0) + pet_data.get('natural_bonus_speed', 0)
            }
            level = pet_data.get('level', 1)
            total_bonus_stats = {
                'hp': (level - 1) * species_info.get('hp_growth', 0) + pet_data.get('allocated_hp', 0),
                'attack': (level - 1) * species_info.get('attack_growth', 0) + pet_data.get('allocated_attack', 0),
                'defense': (level - 1) * species_info.get('defense_growth', 0) + pet_data.get('allocated_defense', 0),
                'speed': (level - 1) * species_info.get('speed_growth', 0) + pet_data.get('allocated_speed', 0)
            }
            current_stats = {
                'hp': round(hatch_base_stats['hp'] + total_bonus_stats['hp']),
                'attack': round(hatch_base_stats['attack'] + total_bonus_stats['attack']),
                'defense': round(hatch_base_stats['defense'] + total_bonus_stats['defense']),
                'speed': round(hatch_base_stats['speed'] + total_bonus_stats['speed'])
            }
            embed.add_field(name="❤️ 体力", value=f"**{current_stats['hp']}** (`{round(hatch_base_stats['hp'])}` + `{round(total_bonus_stats['hp'])}`)", inline=True)
            embed.add_field(name="⚔️ 攻撃力", value=f"**{current_stats['attack']}** (`{round(hatch_base_stats['attack'])}` + `{round(total_bonus_stats['attack'])}`)", inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True) 
            embed.add_field(name="🛡️ 防御力", value=f"**{current_stats['defense']}** (`{round(hatch_base_stats['defense'])}` + `{round(total_bonus_stats['defense'])}`)", inline=True)
            embed.add_field(name="👟 スピード", value=f"**{current_stats['speed']}** (`{round(hatch_base_stats['speed'])}` + `{round(total_bonus_stats['speed'])}`)", inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True) 
            embed.add_field(name="状態", value=status_text, inline=False)
            
            next_stage_num = current_stage + 1
            next_stage_info = stage_info_json.get(str(next_stage_num))
            if next_stage_info and pet_data.get('level', 0) >= level_cap:
                required_items = next_stage_info.get('items', {})
                if required_items:
                    req_list = [f"> {item}: {qty}個" for item, qty in required_items.items()]
                    embed.add_field(
                        name=f"🌟 次の段階({next_stage_info.get('name')})進化素材",
                        value="\n".join(req_list),
                        inline=False
                    )

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
            
        await supabase.table('pets').update({
            'current_stage': 2, 'level': 1, 'xp': 0, 'hunger': 100, 'friendship': 0,
            'nickname': species_info['species_name'],
            'current_hp': final_stats['hp'],
            'current_attack': final_stats['attack'],
            'current_defense': final_stats['defense'],
            'current_speed': final_stats['speed'],
            'natural_bonus_hp': natural_bonus_stats['hp'], 
            'natural_bonus_attack': natural_bonus_stats['attack'],
            'natural_bonus_defense': natural_bonus_stats['defense'], 
            'natural_bonus_speed': natural_bonus_stats['speed']
        }).eq('id', pet_data['id']).execute()
        
        thread = self.bot.get_channel(pet_data['thread_id'])
        if thread:
            try:
                final_pet_data = await get_user_pet(user_id)
                if not final_pet_data: return

                message = await thread.fetch_message(pet_data['message_id'])
                hatched_embed = self.build_pet_ui_embed(user, final_pet_data)
                inventory = await get_inventory(user)
                cooldown_active = await self._is_play_on_cooldown(user_id)
                evo_ready = await self._is_evolution_ready(final_pet_data, inventory)
                view = PetUIView(self, user_id, final_pet_data, play_cooldown_active=cooldown_active, evolution_ready=evo_ready)
                await message.edit(embed=hatched_embed, view=view) 
                await thread.send(f"{user.mention}さんの卵が孵化しました！")
                await thread.edit(name=f"🐾｜{species_info['species_name']}")
            except (discord.NotFound, discord.Forbidden) as e:
                logger.error(f"부화 UI 업데이트 실패 (스레드: {thread.id}): {e}")
    
    async def process_levelup_requests(self, requests: List[Dict], is_admin: bool = False):
        user_ids_to_notify = {int(req['config_key'].split('_')[-1]): req.get('config_value') for req in requests}
        for user_id, payload in user_ids_to_notify.items():
            new_level, points_awarded = None, None
            if is_admin:
                pet_res = await supabase.table('pets').select('level, xp').eq('user_id', user_id).maybe_single().execute()
                if pet_res and pet_res.data:
                    current_level = pet_res.data.get('level', 1)
                    current_xp_in_level = pet_res.data.get('xp', 0)
                    xp_for_this_level = calculate_xp_for_pet_level(current_level)
                    xp_to_add = (xp_for_this_level - current_xp_in_level) + 1
                    if xp_to_add > 0:
                        res = await supabase.rpc('add_xp_to_pet', {'p_user_id': str(user_id), 'p_xp_to_add': xp_to_add}).execute()
                        if res.data and res.data[0].get('leveled_up'):
                            new_level = res.data[0].get('new_level')
                            points_awarded = res.data[0].get('points_awarded')
            else: 
                if isinstance(payload, dict):
                    new_level, points_awarded = payload.get('new_level'), payload.get('points_awarded')
            if new_level is not None and points_awarded is not None:
                await self.notify_pet_level_up(user_id, new_level, points_awarded)

    async def process_level_set_requests(self, requests: List[Dict]):
        for req in requests:
            try:
                user_id, payload = int(req['config_key'].split('_')[-1]), req.get('config_value', {})
                exact_level = payload.get('exact_level')
                if exact_level is None: continue
                total_xp_for_level = 0
                for l in range(1, exact_level):
                    total_xp_for_level += (400 + (100 * l))
                res = await supabase.rpc('set_pet_level_and_xp', {'p_user_id': user_id, 'p_new_level': exact_level, 'p_new_xp': 0, 'p_total_xp': total_xp_for_level}).execute()
                if res.data and res.data[0].get('success'):
                    points_awarded = res.data[0].get('points_awarded', 0)
                    await self.notify_pet_level_up(user_id, exact_level, points_awarded)
                    logger.info(f"관리자 요청으로 {user_id}의 펫 레벨을 {exact_level}로 설정했습니다.")
            except Exception as e:
                logger.error(f"펫 레벨 설정 요청 처리 중 오류: {e}", exc_info=True)

    async def notify_pet_level_up(self, user_id: int, new_level: int, points_awarded: int):
        pet_data = await get_user_pet(user_id)
        if not pet_data: return

        user = self.bot.get_user(user_id)
        if not user: return

        nickname = pet_data.get('nickname', '名前のないペット')
        
        log_channel_id = get_id("log_pet_levelup_channel_id")
        if log_channel_id and (log_channel := self.bot.get_channel(log_channel_id)):
            message_text = (f"🎉 {user.mention}さんの'**{nickname}**'が**レベル{new_level}**に成長しました！ステータスポイント**{points_awarded}**個を獲得しました。 ✨")
            try: await log_channel.send(message_text)
            except Exception as e: logger.error(f"펫 레벨업 로그 전송 실패: {e}")

        thread_id = pet_data.get('thread_id')
        if not thread_id: return
        thread = self.bot.get_channel(thread_id)
        if not thread: return
        
        await self.update_pet_ui(user_id, thread)

    async def check_and_process_auto_evolution(self, user_ids: set):
        for user_id in user_ids:
            try:
                user = self.bot.get_user(user_id)
                if not user: continue
                inventory = await get_inventory(user)
                pet_data = await get_user_pet(user_id)
                if pet_data and await self._is_evolution_ready(pet_data, inventory):
                    if thread := self.bot.get_channel(pet_data['thread_id']):
                        await self.handle_evolution(user_id, thread)
            except Exception as e:
                logger.error(f"자동 진화 처리 중 오류 (유저: {user_id}): {e}", exc_info=True)

    async def notify_pet_evolution(self, user_id: int, new_stage_num: int, points_granted: int):
        pet_data = await get_user_pet(user_id)
        if not pet_data or not (thread_id := pet_data.get('thread_id')): return

        species_info = pet_data.get('pet_species', {})
        stage_info_json = species_info.get('stage_info', {})
        new_stage_name = stage_info_json.get(str(new_stage_num), {}).get('name', '新しい姿')
        
        if thread := self.bot.get_channel(thread_id):
            user = self.bot.get_user(user_id)
            if user: await thread.send(f"🌟 {user.mention}さんのペットが**{new_stage_name}**に進化しました！ステータスポイント**{points_granted}**個を獲得しました！")
            
            await self.update_pet_ui(user_id, thread)

    async def handle_evolution(self, user_id: int, channel: discord.TextChannel) -> bool:
        user = self.bot.get_user(user_id)
        if not user: return False

        inventory = await get_inventory(user)
        pet_data = await get_user_pet(user_id)
        
        if not await self._is_evolution_ready(pet_data, inventory):
            return False

        species_info = pet_data.get('pet_species', {})
        next_stage_num = pet_data['current_stage'] + 1
        stage_info_json = species_info.get('stage_info', {})
        next_stage_info = stage_info_json.get(str(next_stage_num))
        required_items = next_stage_info.get('items', {})
        
        tasks = [update_inventory(user_id, item, -qty) for item, qty in required_items.items()]
        await asyncio.gather(*tasks)

        res = await supabase.rpc('evolve_pet_stage', {'p_user_id': user_id}).single().execute()

        if res.data and res.data.get('success'):
            await self.notify_pet_evolution(user_id, res.data.get('new_stage'), res.data.get('points_granted'))
            return True
        else:
            logger.error(f"펫 진화 DB 함수 호출 실패 (User: {user_id}). 재료를 환불합니다.")
            refund_tasks = [update_inventory(user_id, item, qty) for item, qty in required_items.items()]
            await asyncio.gather(*refund_tasks)
            return False

    async def update_pet_ui(self, user_id: int, channel: discord.TextChannel, message: Optional[discord.Message] = None, is_refresh: bool = False, pet_data_override: Optional[Dict] = None):
        pet_data = pet_data_override if pet_data_override else await get_user_pet(user_id)
        if not pet_data:
            if message:
                try: await message.edit(content="ペット情報が見つかりません。", embed=None, view=None)
                except discord.NotFound: pass
            return
        
        user = self.bot.get_user(user_id)
        if not user: return

        inventory = await get_inventory(user)
        embed = self.build_pet_ui_embed(user, pet_data)
        cooldown_active = await self._is_play_on_cooldown(pet_data['id'])
        evo_ready = await self._is_evolution_ready(pet_data, inventory)
        view = PetUIView(self, user_id, pet_data, play_cooldown_active=cooldown_active, evolution_ready=evo_ready)
        
        message_to_edit = message
        if not message_to_edit:
            if message_id := pet_data.get('message_id'):
                try: message_to_edit = await channel.fetch_message(message_id)
                except (discord.NotFound, discord.Forbidden): pass
        
        if is_refresh and message_to_edit:
            try: await message_to_edit.delete()
            except (discord.NotFound, discord.Forbidden): pass
            message_to_edit = None # 삭제되었으므로 None으로 설정
        
        if message_to_edit:
            await message_to_edit.edit(embed=embed, view=view)
        else:
            # 메시지가 없거나, 찾을 수 없거나, 새로고침 요청인 경우 새로 생성
            new_message = await channel.send(embed=embed, view=view)
            await supabase.table('pets').update({'message_id': new_message.id}).eq('user_id', user_id).execute()
            
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
        logger.info(f"✅ {panel_key} パネルを #{channel.name} チャンネルに正常に生成しました。")

async def setup(bot: commands.Bot):
    await bot.add_cog(PetSystem(bot))
