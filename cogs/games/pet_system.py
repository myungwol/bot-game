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
    get_learnable_skills, set_pet_skill, get_wallet, update_wallet,
    get_skills_unlocked_at_level,
    get_skills_unlocked_at_exact_level,
    get_inventories_for_users # 방금 추가한 함수 import
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

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

class ConfirmReplaceView(ui.View):
    """스킬 교체 여부를 확인하는 간단한 View"""
    def __init__(self, user_id: int):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.value = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("본인만 결정할 수 있습니다.", ephemeral=True, delete_after=5)
            return False
        return True

    @ui.button(label="예, 교체합니다", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        self.value = True
        self.stop()
        await interaction.response.defer()

    @ui.button(label="아니요", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        self.value = False
        self.stop()
        await interaction.response.defer()

class NewSkillLearnView(ui.View):
    """새로운 드롭다운 기반 스킬 학습 UI"""
    def __init__(self, cog: 'PetSystem', user_id: int, pet_data: Dict, unlocked_skills: List[Dict]):
        super().__init__(timeout=86400) # 하루 동안 유효
        self.cog = cog
        self.user_id = user_id
        self.pet_data = pet_data
        self.unlocked_skills = unlocked_skills
        self.selected_skill_id: Optional[int] = None
        self.selected_slot: Optional[int] = None

    async def start(self, thread: discord.TextChannel):
        self.update_components()
        embed = self.build_embed()
        message_text = f"<@{self.user_id}>, 펫이 성장하여 새로운 스킬을 배울 수 있게 되었습니다!"
        await thread.send(message_text, embed=embed, view=self)

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title="🎓 새로운 스킬 습득", color=0x00FF00)
        embed.description = "아래 메뉴에서 배울 스킬과 등록할 슬롯을 선택해주세요."
        
        if self.selected_skill_id:
            skill = next((s for s in self.unlocked_skills if s['id'] == self.selected_skill_id), None)
            if skill:
                embed.add_field(name="선택한 스킬", value=f"**{skill['skill_name']}**\n> {skill['description']}", inline=False)

        if self.selected_slot:
            learned_skills = self.pet_data.get('learned_skills', [])
            skill_in_slot = next((s for s in learned_skills if s['slot_number'] == self.selected_slot), None)
            slot_desc = f"**{skill_in_slot['pet_skills']['skill_name']}** (교체 예정)" if skill_in_slot else "비어있음"
            embed.add_field(name="선택한 슬롯", value=f"**{self.selected_slot}번 슬롯**\n> 현재 스킬: {slot_desc}", inline=False)
        return embed

    def update_components(self):
        self.clear_items()
        
        # 1. 배울 스킬 선택 드롭다운
        skill_options = [discord.SelectOption(label=s['skill_name'], value=str(s['id']), description=f"위력: {s['power']}") for s in self.unlocked_skills]
        skill_select = ui.Select(placeholder="① 배울 스킬을 선택하세요...", options=skill_options)
        skill_select.callback = self.on_skill_select
        self.add_item(skill_select)

        # 2. 등록할 슬롯 선택 드롭다운
        learned_skills = self.pet_data.get('learned_skills', [])
        slot_options = []
        for i in range(1, 5):
            skill_in_slot = next((s for s in learned_skills if s['slot_number'] == i), None)
            label = f"{i}번 슬롯" + (f" (현재: {skill_in_slot['pet_skills']['skill_name']})" if skill_in_slot else " (비어있음)")
            slot_options.append(discord.SelectOption(label=label, value=str(i)))
        
        slot_select = ui.Select(placeholder="② 등록할 슬롯을 선택하세요...", options=slot_options, disabled=(self.selected_skill_id is None))
        slot_select.callback = self.on_slot_select
        self.add_item(slot_select)

        # 3. 확정 및 취소 버튼
        confirm_button = ui.Button(label="결정", style=discord.ButtonStyle.success, disabled=(self.selected_skill_id is None or self.selected_slot is None))
        confirm_button.callback = self.on_confirm
        self.add_item(confirm_button)
        
        cancel_button = ui.Button(label="취소", style=discord.ButtonStyle.grey)
        cancel_button.callback = self.on_cancel
        self.add_item(cancel_button)

    async def update_view(self, interaction: discord.Interaction):
        self.update_components()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_skill_select(self, interaction: discord.Interaction):
        self.selected_skill_id = int(interaction.data['values'][0])
        await self.update_view(interaction)

    async def on_slot_select(self, interaction: discord.Interaction):
        self.selected_slot = int(interaction.data['values'][0])
        await self.update_view(interaction)

    async def on_cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="스킬 배우기를 취소했습니다.", view=None, embed=None)
        self.stop()

    async def on_confirm(self, interaction: discord.Interaction):
        learned_skills = self.pet_data.get('learned_skills', [])
        skill_in_slot = next((s for s in learned_skills if s['slot_number'] == self.selected_slot), None)
        new_skill_name = next(s['skill_name'] for s in self.unlocked_skills if s['id'] == self.selected_skill_id)

        if skill_in_slot:
            # 스킬 교체 확인 절차
            confirm_view = ConfirmReplaceView(self.user_id)
            await interaction.response.send_message(
                f"**{self.selected_slot}번 슬롯**에 있는 '**{skill_in_slot['pet_skills']['skill_name']}**' 스킬을"
                f" '**{new_skill_name}**'(으)로 교체하시겠습니까?",
                view=confirm_view, ephemeral=True
            )
            await confirm_view.wait()
            if confirm_view.value is not True:
                await interaction.edit_original_response(content="교체를 취소했습니다.", view=None)
                return
            # 확인 후 원래 메시지 삭제
            await interaction.delete_original_response()
        else:
            await interaction.response.defer()

        # 스킬 설정 실행
        await set_pet_skill(self.pet_data['id'], self.selected_skill_id, self.selected_slot)
        await interaction.message.edit(content=f"✅ **{new_skill_name}** 스킬을 {self.selected_slot}번 슬롯에 등록했습니다!", embed=None, view=None)
        
        updated_pet_data = await get_user_pet(self.user_id)
        if updated_pet_data:
            await self.cog.update_pet_ui(self.user_id, interaction.channel, pet_data_override=updated_pet_data)
        self.stop()

# ... (SkillAcquisitionView, SkillChangeView, StatAllocationView, PetNicknameModal, ConfirmReleaseView, PetUIView, EggSelectView, IncubatorPanelView 클래스는 변경 없이 그대로 유지) ...
class SkillAcquisitionView(ui.View):
    def __init__(self, cog: 'PetSystem', user_id: int, pet_data: Dict, unlocked_skill: Dict):
        super().__init__(timeout=86400)
        self.cog = cog
        self.user_id = user_id
        self.pet_data = pet_data
        self.unlocked_skill = unlocked_skill
        self.selected_slot_to_replace: Optional[int] = None

    async def start(self, thread: discord.TextChannel):
        embed = self.build_embed()
        self.update_components()
        message_text = f"<@{self.user_id}>, 펫이 성장하여 새로운 스킬을 배울 수 있게 되었습니다!"
        await thread.send(message_text, embed=embed, view=self)

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"🎓 새로운 스킬 습득 가능: {self.unlocked_skill['skill_name']}",
            description=f"> {self.unlocked_skill['description']}",
            color=0x00FF00
        )
        embed.add_field(name="속성", value=self.unlocked_skill['element'], inline=True)
        embed.add_field(name="위력", value=str(self.unlocked_skill['power']), inline=True)
        return embed
        
    def update_components(self):
        self.clear_items()
        learned_skills = self.pet_data.get('learned_skills', [])
        
        if len(learned_skills) < 4:
            learn_button = ui.Button(label="새로운 스킬 배우기", style=discord.ButtonStyle.success, emoji="✅")
            learn_button.callback = self.on_learn
            self.add_item(learn_button)
        else:
            replace_options = [
                discord.SelectOption(label=f"{s['slot_number']}번 슬롯: {s['pet_skills']['skill_name']}", value=str(s['slot_number']))
                for s in learned_skills
            ]
            replace_select = ui.Select(placeholder="교체할 스킬을 선택하세요...", options=replace_options)
            replace_select.callback = self.on_replace_select
            self.add_item(replace_select)
            
            confirm_replace_button = ui.Button(label="이 스킬로 교체하기", style=discord.ButtonStyle.primary, emoji="🔄", disabled=(self.selected_slot_to_replace is None))
            confirm_replace_button.callback = self.on_confirm_replace
            self.add_item(confirm_replace_button)

        pass_button = ui.Button(label="배우지 않기", style=discord.ButtonStyle.grey, emoji="❌")
        pass_button.callback = self.on_pass
        self.add_item(pass_button)

    async def on_learn(self, interaction: discord.Interaction):
        await interaction.response.defer()
        learned_skills = self.pet_data.get('learned_skills', [])
        empty_slot = next((s for s in range(1, 5) if s not in [ls['slot_number'] for ls in learned_skills]), None)
        if empty_slot:
            await set_pet_skill(self.pet_data['id'], self.unlocked_skill['id'], empty_slot)
            await interaction.message.edit(content=f"✅ **{self.unlocked_skill['skill_name']}** 스킬을 배웠습니다!", embed=None, view=None)
            
            updated_pet_data = await get_user_pet(self.user_id)
            if updated_pet_data:
                await self.cog.update_pet_ui(self.user_id, interaction.channel, pet_data_override=updated_pet_data)
        self.stop()

    async def on_replace_select(self, interaction: discord.Interaction):
        self.selected_slot_to_replace = int(interaction.data['values'][0])
        self.update_components()
        await interaction.response.edit_message(view=self)

    async def on_confirm_replace(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await set_pet_skill(self.pet_data['id'], self.unlocked_skill['id'], self.selected_slot_to_replace)
        await interaction.message.edit(content=f"✅ **{self.unlocked_skill['skill_name']}** 스킬로 교체했습니다!", embed=None, view=None)
        
        updated_pet_data = await get_user_pet(self.user_id)
        if updated_pet_data:
            await self.cog.update_pet_ui(self.user_id, interaction.channel, pet_data_override=updated_pet_data)
        self.stop()
        
    async def on_pass(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await interaction.message.edit(content="스킬을 배우지 않고 넘어갔습니다.", embed=None, view=None)
        self.stop()

class SkillChangeView(ui.View):
    def __init__(self, parent_view: 'PetUIView'):
        super().__init__(timeout=180)
        self.parent_view = parent_view
        self.cog = parent_view.cog
        self.user_id = parent_view.user_id
        self.pet_data = parent_view.pet_data
        self.learnable_skills: List[Dict] = []
        self.selected_slot: Optional[int] = None
        self.selected_new_skill_id: Optional[int] = None
        
    async def start(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        learned_skill_ids = [s['skill_id'] for s in self.pet_data.get('learned_skills', [])]
        all_possible_skills = await get_skills_unlocked_at_level(self.pet_data['level'], self.pet_data['pet_species']['element'])
        self.learnable_skills = [s for s in all_possible_skills if s['id'] not in learned_skill_ids]

        self.update_components()
        embed = self.build_embed()
        await interaction.followup.send(embed=embed, view=self, ephemeral=True)

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title="🔧 스킬 관리", color=0xFFA500)
        embed.description = "스킬을 배우거나 교체할 슬롯과 새로 배울 스킬을 선택해주세요.\n**비용: `1,000` 코인**"
        return embed

    def update_components(self):
        self.clear_items()
        
        learned_skills = self.pet_data.get('learned_skills', [])
        
        slot_options = []
        for i in range(1, 5):
            learned_skill_in_slot = next((s for s in learned_skills if s['slot_number'] == i), None)
            label = f"{i}번 슬롯"
            if learned_skill_in_slot:
                label += f" (현재: {learned_skill_in_slot['pet_skills']['skill_name']})"
            else:
                label += " (비어있음)"
            slot_options.append(discord.SelectOption(label=label, value=str(i)))

        slot_select = ui.Select(placeholder="① 스킬을 배우거나 교체할 슬롯 선택...", options=slot_options)
        slot_select.callback = self.on_slot_select
        self.add_item(slot_select)

        new_skill_options = [
            discord.SelectOption(label=s['skill_name'], value=str(s['id']), description=f"위력:{s['power']}, 속성:{s['element']}")
            for s in self.learnable_skills[:25]
        ]
        
        if not new_skill_options:
            new_skill_options.append(discord.SelectOption(label="배울 수 있는 스킬이 없습니다.", value="no_skills_available"))
        
        new_skill_select = ui.Select(
            placeholder="② 새로 배울 스킬을 선택하세요...", 
            options=new_skill_options, 
            disabled=(not self.learnable_skills)
        )
        
        new_skill_select.callback = self.on_new_skill_select
        self.add_item(new_skill_select)

        confirm_button = ui.Button(label="확정 (1,000 코인)", style=discord.ButtonStyle.success, disabled=(self.selected_slot is None or self.selected_new_skill_id is None))
        confirm_button.callback = self.on_confirm
        self.add_item(confirm_button)

    async def on_slot_select(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.selected_slot = int(interaction.data['values'][0])
        self.update_components()
        await interaction.edit_original_response(view=self)

    async def on_new_skill_select(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if interaction.data['values'][0] == "no_skills_available":
            return

        self.selected_new_skill_id = int(interaction.data['values'][0])
        self.update_components()
        await interaction.edit_original_response(view=self)

    async def on_confirm(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        wallet = await get_wallet(self.user_id)
        if wallet.get('balance', 0) < 1000:
            return await interaction.edit_original_response(content="❌ 코인이 부족합니다.", view=None)

        await update_wallet(interaction.user, -1000)
        success = await set_pet_skill(self.pet_data['id'], self.selected_new_skill_id, self.selected_slot)
        
        if success:
            await interaction.edit_original_response(content="✅ 스킬을 성공적으로 배웠습니다/변경했습니다!", view=None)
            
            updated_pet_data = await get_user_pet(self.user_id)
            if updated_pet_data:
                await self.cog.update_pet_ui(self.user_id, interaction.channel, pet_data_override=updated_pet_data)
        else:
            await update_wallet(interaction.user, 1000)
            await interaction.edit_original_response(content="❌ 스킬 설정에 실패했습니다. 코인이 환불되었습니다.", view=None)

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
        self.change_skills_button.custom_id = f"pet_change_skills:{user_id}"
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
        pet_id = self.pet_data['id']
        if await self.cog._is_play_on_cooldown(pet_id):
             return await interaction.followup.send("❌ 오늘은 이미 놀아주었습니다. 내일 다시 시도해주세요.", ephemeral=True)
        inventory = await get_inventory(interaction.user)
        if inventory.get("공놀이 세트", 0) < 1:
            return await interaction.followup.send("❌ '공놀이 세트' 아이템이 부족합니다.", ephemeral=True)
        await update_inventory(self.user_id, "공놀이 세트", -1)
        friendship_amount = 1; stat_increase_amount = 1
        await supabase.rpc('increase_pet_friendship_and_stats', {'p_user_id': self.user_id, 'p_friendship_amount': friendship_amount, 'p_stat_amount': stat_increase_amount}).execute()
        await set_cooldown(pet_id, cooldown_key)
        await self.cog.update_pet_ui(self.user_id, interaction.channel, interaction.message)
        msg = await interaction.followup.send(f"❤️ 펫과 즐거운 시간을 보냈습니다! 친밀도가 {friendship_amount} 오르고 모든 스탯이 {stat_increase_amount} 상승했습니다.", ephemeral=True)
        self.cog.bot.loop.create_task(delete_message_after(msg, 5))

    @ui.button(label="진화", style=discord.ButtonStyle.success, emoji="🌟", row=0)
    async def evolve_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        success = await self.cog.handle_evolution(interaction.user.id, interaction.channel)
        if not success:
            await interaction.followup.send("❌ 진화 조건을 만족하지 못했습니다. 레벨과 필요 아이템을 확인해주세요.", ephemeral=True, delete_after=10)

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
            msg = await interaction.followup.send(f"펫의 이름이 '{new_name}'(으)로 변경되었습니다.", ephemeral=True)
            self.cog.bot.loop.create_task(delete_message_after(msg, 5))

    @ui.button(label="스킬 변경", style=discord.ButtonStyle.secondary, emoji="🔧", row=1)
    async def change_skills_button(self, interaction: discord.Interaction, button: ui.Button):
        change_view = SkillChangeView(self)
        await change_view.start(interaction)

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
            try:
                # ▼▼▼ [핵심 수정] 펫 놓아주기 시 던전 세션을 정상적으로 종료하는 로직 추가 ▼▼▼
                
                # 1. 이 펫이 참여 중인 던전 세션이 있는지 확인합니다.
                session_res = await supabase.table('dungeon_sessions').select('thread_id').eq('pet_id', self.pet_data['id']).maybe_single().execute()
                
                if session_res and session_res.data:
                    thread_id = int(session_res.data['thread_id'])
                    dungeon_cog = self.cog.bot.get_cog("Dungeon")
                    thread = self.cog.bot.get_channel(thread_id)
                    
                    if dungeon_cog and thread:
                        logger.info(f"펫(ID:{self.pet_data['id']})을 놓아주기 전에 활성 던전(스레드:{thread_id})을 먼저 종료합니다.")
                        # Dungeon 코그의 세션 종료 함수를 호출하여 스레드까지 깔끔하게 삭제합니다.
                        # 보상은 포기하는 것으로 처리합니다.
                        await dungeon_cog.close_dungeon_session(self.user_id, rewards={}, total_xp=0, thread=thread)
                        await asyncio.sleep(1) # 스레드 삭제가 처리될 시간을 줍니다.

                # 2. 이제 펫을 안전하게 삭제할 수 있습니다.
                await supabase.table('pets').delete().eq('user_id', self.user_id).execute()

                # 펫 전용 스레드(알 채널)도 삭제합니다.
                await interaction.edit_original_response(content="펫을 자연으로 돌려보냈습니다...", view=None)
                await interaction.channel.send(f"{interaction.user.mention}님이 펫을 자연의 품으로 돌려보냈습니다.")
                await asyncio.sleep(10)
                try:
                    await interaction.channel.delete()
                except (discord.NotFound, discord.Forbidden): pass
                # ▲▲▲ [핵심 수정] 완료 ▲▲▲

            except APIError as e:
                logger.error(f"펫 놓아주기 처리 중 DB 오류 발생: {e}", exc_info=True)
                await interaction.edit_original_response(content="❌ 펫을 놓아주는 중 오류가 발생했습니다. 관리자에게 문의해주세요.", view=None)
            # ▲▲▲ [핵심 수정] 완료 ▲▲▲
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

class IncubatorPanelView(ui.View):
    def __init__(self, cog_instance: 'PetSystem'):
        super().__init__(timeout=None)
        self.cog = cog_instance
    @ui.button(label="알 부화시키기", style=discord.ButtonStyle.secondary, emoji="🥚", custom_id="incubator_start")
    async def start_incubation_button(self, interaction: discord.Interaction, button: ui.Button):
        if await get_user_pet(interaction.user.id):
            await interaction.response.send_message("❌ 이미 펫을 소유하고 있습니다. 펫은 한 마리만 키울 수 있습니다.", ephemeral=True, delete_after=5)
            return
        await interaction.response.defer(ephemeral=True, thinking=False)
        view = EggSelectView(interaction.user, self.cog)
        await view.start(interaction)

class PetSystem(commands.Cog):
    # ▼▼▼ [수정] __init__ 과 cog_load/unload 를 수정하여 에러를 해결합니다. ▼▼▼
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_views_loaded = False
        # __init__ 에서는 태스크를 시작하지 않습니다.

    async def cog_load(self):
        # Cog가 로드될 때 태스크를 시작합니다.
        self.hatch_checker.start()
        self.hunger_and_stat_decay.start()
        self.auto_refresh_pet_uis.start()

    def cog_unload(self):
        # Cog가 언로드될 때 태스크를 취소합니다.
        self.hatch_checker.cancel()
        self.hunger_and_stat_decay.cancel()
        self.auto_refresh_pet_uis.cancel()
    # ▲▲▲ [수정] 완료 ▲▲▲

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

        if not next_stage_info: return False
        if pet_data['level'] < next_stage_info.get('level_req', 999): return False
        
        if 'item' in next_stage_info and 'qty' in next_stage_info:
            required_item = next_stage_info['item']
            required_qty = next_stage_info['qty']
            if inventory.get(required_item, 0) < required_qty: return False
        
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
        
        return { 'hp': round(base_hp), 'attack': round(base_attack), 'defense': round(base_defense), 'speed': round(base_speed) }

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
            nickname = pet_data.get('nickname') or species_info['species_name']
            
            embed = discord.Embed(title=f"🐾 {nickname}", color=0xFFD700)
            embed.set_author(name=f"{user.display_name}님의 펫", icon_url=user.display_avatar.url if user.display_avatar else None)
            embed.set_thumbnail(url=image_url)

            current_level, current_xp = pet_data['level'], pet_data['xp']
            xp_for_next_level = calculate_xp_for_pet_level(current_level)
            xp_bar = create_bar(current_xp, xp_for_next_level)
            
            hunger = pet_data.get('hunger', 0); hunger_bar = create_bar(hunger, 100, full_char='🟧', empty_char='⬛')
            friendship = pet_data.get('friendship', 0); friendship_bar = create_bar(friendship, 100, full_char='❤️', empty_char='🖤')

            embed.add_field(name="단계", value=f"**{stage_name}**", inline=True)
            embed.add_field(name="타입", value=f"{ELEMENT_TO_TYPE.get(species_info['element'], '알 수 없음')}", inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True)
            embed.add_field(name="레벨", value=f"**Lv. {current_level}**", inline=True)
            embed.add_field(name="속성", value=f"{species_info['element']}", inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True)
            embed.add_field(name="경험치", value=f"`{current_xp} / {xp_for_next_level}`\n{xp_bar}", inline=False)
            embed.add_field(name="배고픔", value=f"`{hunger} / 100`\n{hunger_bar}", inline=False)
            embed.add_field(name="친밀도", value=f"`{friendship} / 100`\n{friendship_bar}", inline=False)

            stat_points = pet_data.get('stat_points', 0)
            if stat_points > 0:
                embed.add_field(name="✨ 남은 스탯 포인트", value=f"**{stat_points}**", inline=False)

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
                'hp': hatch_base_stats['hp'] + total_bonus_stats['hp'],
                'attack': hatch_base_stats['attack'] + total_bonus_stats['attack'],
                'defense': hatch_base_stats['defense'] + total_bonus_stats['defense'],
                'speed': hatch_base_stats['speed'] + total_bonus_stats['speed']
            }
            embed.add_field(name="❤️ 체력", value=f"**{current_stats['hp']}** (`{hatch_base_stats['hp']}` + `{total_bonus_stats['hp']}`)", inline=True)
            embed.add_field(name="⚔️ 공격력", value=f"**{current_stats['attack']}** (`{hatch_base_stats['attack']}` + `{total_bonus_stats['attack']}`)", inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True) 
            embed.add_field(name="🛡️ 방어력", value=f"**{current_stats['defense']}** (`{hatch_base_stats['defense']}` + `{total_bonus_stats['defense']}`)", inline=True)
            embed.add_field(name="👟 스피드", value=f"**{current_stats['speed']}** (`{hatch_base_stats['speed']}` + `{total_bonus_stats['speed']}`)", inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True) 
            
            learned_skills = sorted(pet_data.get('learned_skills', []), key=lambda s: s['slot_number'])
            skill_texts = []
            if not learned_skills:
                skill_texts.append("・ 아직 배운 스킬이 없습니다.")
            else:
                for skill_info in learned_skills:
                    skill = skill_info.get('pet_skills', {})
                    skill_texts.append(f"・ **{skill.get('skill_name', '알수없음')}** (속성: {skill.get('element')}, 위력: {skill.get('power')})")
            
            embed.add_field(name="🐾 배운 스킬", value="\n".join(skill_texts), inline=False)
            
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
        
        await set_pet_skill(pet_data['id'], 1, 1)
        
        updated_pet_data = updated_pet_data_res.data[0]
        updated_pet_data['pet_species'] = species_info
        thread = self.bot.get_channel(pet_data['thread_id'])
        if thread:
            try:
                final_pet_data = await get_user_pet(user_id)
                if not final_pet_data: return

                message = await thread.fetch_message(pet_data['message_id'])
                hatched_embed = self.build_pet_ui_embed(user, final_pet_data)
                cooldown_active = await self._is_play_on_cooldown(user_id)
                evo_ready = await self._is_evolution_ready(final_pet_data, {})
                view = PetUIView(self, user_id, final_pet_data, play_cooldown_active=cooldown_active, evolution_ready=evo_ready)
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
                pet_res = await supabase.table('pets').select('level, xp').eq('user_id', user_id).maybe_single().execute()
                if pet_res and pet_res.data:
                    current_level = pet_res.data.get('level', 1)
                    current_xp_in_level = pet_res.data.get('xp', 0)
                    xp_for_this_level = calculate_xp_for_pet_level(current_level)
                    xp_to_add = (xp_for_this_level - current_xp_in_level) + 1
                    if xp_to_add > 0:
                        res = await supabase.rpc('add_xp_to_pet', {'p_user_id': user_id, 'p_xp_to_add': xp_to_add}).execute()
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

        nickname = pet_data.get('nickname', '이름 없는 펫')
        
        log_channel_id = get_id("log_pet_levelup_channel_id")
        if log_channel_id and (log_channel := self.bot.get_channel(log_channel_id)):
            message_text = (f"🎉 {user.mention}님의 '**{nickname}**'이(가) **레벨 {new_level}**(으)로 성장했습니다! 스탯 포인트 **{points_awarded}**개를 획득했습니다. ✨")
            try: await log_channel.send(message_text)
            except Exception as e: logger.error(f"펫 레벨업 로그 전송 실패: {e}")

        thread_id = pet_data.get('thread_id')
        if not thread_id: return
        thread = self.bot.get_channel(thread_id)
        if not thread: return
        
        await self.update_pet_ui(user_id, thread)

        pet_element = pet_data.get('pet_species', {}).get('element')
        if not pet_element: return

        newly_unlocked_skills = await get_skills_unlocked_at_exact_level(new_level, pet_element)

        # ▼▼▼ [핵심 수정] 이 부분을 수정합니다. ▼▼▼
        if newly_unlocked_skills:
            logger.info(f"{user.display_name}의 펫이 {new_level}레벨에 도달하여 {len(newly_unlocked_skills)}개의 스킬을 해금했습니다.")
            
            # 여러 스킬이 해금될 경우를 대비해, 한 번에 하나의 View만 띄웁니다.
            fresh_pet_data = await get_user_pet(user_id)
            if not fresh_pet_data: return
            
            learn_view = NewSkillLearnView(self, user_id, fresh_pet_data, newly_unlocked_skills)
            await learn_view.start(thread)
        # ▲▲▲ [핵심 수정] 완료 ▲▲▲

    async def check_and_process_auto_evolution(self, user_ids: set):
        for user_id in user_ids:
            try:
                res = await supabase.rpc('trigger_pet_auto_evolution', {'p_user_id': user_id}).single().execute()
                if res.data and res.data.get('evolved'):
                    await self.notify_pet_evolution(user_id, res.data.get('new_stage'), res.data.get('points_granted'))
            except Exception as e:
                logger.error(f"자동 진화 처리 중 오류 (유저: {user_id}): {e}", exc_info=True)

    async def notify_pet_evolution(self, user_id: int, new_stage_num: int, points_granted: int):
        pet_data = await get_user_pet(user_id)
        if not pet_data or not (thread_id := pet_data.get('thread_id')): return

        species_info = pet_data.get('pet_species', {})
        stage_info_json = species_info.get('stage_info', {})
        new_stage_name = stage_info_json.get(str(new_stage_num), {}).get('name', '새로운 모습')
        
        if thread := self.bot.get_channel(thread_id):
            user = self.bot.get_user(user_id)
            if user: await thread.send(f"🌟 {user.mention}님의 펫이 **{new_stage_name}**(으)로 진화했습니다! 스탯 포인트 **{points_granted}**개를 획득했습니다!")
            
            await self.update_pet_ui(user_id, thread)

    async def handle_evolution(self, user_id: int, channel: discord.TextChannel) -> bool:
        res = await supabase.rpc('attempt_pet_evolution', {'p_user_id': user_id}).single().execute()
        if res.data and res.data.get('success'):
            await self.notify_pet_evolution(user_id, res.data.get('new_stage'), res.data.get('points_granted'))
            return True
        return False

    async def update_pet_ui(self, user_id: int, channel: discord.TextChannel, message: Optional[discord.Message] = None, is_refresh: bool = False, pet_data_override: Optional[Dict] = None):
        pet_data = pet_data_override if pet_data_override else await get_user_pet(user_id)
        if not pet_data:
            if message: await message.edit(content="펫 정보를 찾을 수 없습니다.", embed=None, view=None)
            return
        
        inventory = await get_inventory(self.bot.get_user(user_id))
        user = self.bot.get_user(user_id)
        embed = self.build_pet_ui_embed(user, pet_data)
        cooldown_active = await self._is_play_on_cooldown(pet_data['id'])
        evo_ready = await self._is_evolution_ready(pet_data, inventory)
        view = PetUIView(self, user_id, pet_data, play_cooldown_active=cooldown_active, evolution_ready=evo_ready)
        
        if not message and not is_refresh:
            if pet_data.get('message_id'):
                try: message = await channel.fetch_message(pet_data['message_id'])
                except (discord.NotFound, discord.Forbidden): pass
        
        if is_refresh and message:
            try: await message.delete()
            except (discord.NotFound, discord.Forbidden): pass
            new_message = await channel.send(embed=embed, view=view)
            await supabase.table('pets').update({'message_id': new_message.id}).eq('user_id', user_id).execute()
        elif message:
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

    # dungeon.py에서 가져온 자동 완성 함수
    async def skill_autocomplete(self, interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        res = await supabase.table('pet_skills').select('skill_name').ilike('skill_name', f'%{current}%').limit(25).execute()
        if not (res and res.data): return []
        return [app_commands.Choice(name=row['skill_name'], value=row['skill_name']) for row in res.data]

    @app_commands.command(name="펫스킬등록", description="[관리자] 유저의 펫에게 특정 스킬을 등록/교체합니다.")
    @app_commands.describe(
        user="스킬을 등록할 펫의 주인입니다.",
        skill_name="등록할 스킬의 이름입니다.",
        slot="스킬을 등록할 슬롯 번호입니다 (1~4)."
    )
    @app_commands.autocomplete(skill_name=skill_autocomplete)
    async def admin_set_pet_skill(self, interaction: discord.Interaction, user: discord.Member, skill_name: str, slot: app_commands.Range[int, 1, 4]):
        # 봇 소유자 또는 관리자만 사용할 수 있도록 권한 체크
        if not await self.bot.is_owner(interaction.user) and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ 이 명령어는 관리자만 사용할 수 있습니다.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        try:
            # 1. 펫 정보와 스킬 정보 가져오기
            pet_res = await supabase.table('pets').select('id').eq('user_id', user.id).maybe_single().execute()
            skill_res = await supabase.table('pet_skills').select('id').eq('skill_name', skill_name).maybe_single().execute()

            if not (pet_res and pet_res.data):
                return await interaction.followup.send(f"❌ {user.display_name}님은 펫을 소유하고 있지 않습니다.", ephemeral=True)
            if not (skill_res and skill_res.data):
                return await interaction.followup.send(f"❌ '{skill_name}' 스킬을 찾을 수 없습니다. 정확한 이름을 입력해주세요.", ephemeral=True)
                
            pet_id = pet_res.data['id']
            skill_id = skill_res.data['id']

            # 2. 스킬 설정 (database.py의 set_pet_skill 함수 재사용)
            success = await set_pet_skill(pet_id, skill_id, slot)
            
            if success:
                # 3. 성공 메시지 및 UI 업데이트 요청
                await interaction.followup.send(f"✅ {user.display_name}님의 펫 {slot}번 슬롯에 '{skill_name}' 스킬을 성공적으로 등록했습니다.", ephemeral=True)
                
                pet_data = await get_user_pet(user.id)
                if pet_data and pet_data.get('thread_id'):
                    if thread := self.bot.get_channel(pet_data['thread_id']):
                        await self.update_pet_ui(user.id, thread)
            else:
                await interaction.followup.send("❌ 스킬을 등록하는 중 오류가 발생했습니다. (이미 배운 스킬일 수 있습니다)", ephemeral=True)

        except Exception as e:
            logger.error(f"관리자 펫 스킬 등록 중 오류: {e}", exc_info=True)
            await interaction.followup.send("❌ 처리 중 심각한 오류가 발생했습니다. 로그를 확인해주세요.", ephemeral=True)
    # ▲▲▲ [핵심 추가] 완료 ▲▲▲

async def setup(bot: commands.Bot):
    await bot.add_cog(PetSystem(bot))
