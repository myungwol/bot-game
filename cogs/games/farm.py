# cogs/games/farm.py

import discord
from discord.ext import commands, tasks
from discord import ui
import logging
from typing import Optional, Dict, List, Any
import asyncio
import time
import math
import random
from datetime import datetime, timezone, timedelta, time as dt_time
from collections import defaultdict

from utils.database import (
    get_farm_data, create_farm, get_config, expand_farm_db,
    save_panel_id, get_panel_id, get_embed_from_db,
    supabase, get_inventory, get_user_gear, update_plot,
    get_farmable_item_info, update_inventory, BARE_HANDS,
    check_farm_permission, grant_farm_permission, clear_plots_db,
    get_farm_owner_by_thread, get_item_database, save_config_to_db,
    get_user_abilities,
    log_activity, delete_config_from_db
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

CROP_EMOJI_MAP = {
    'seed':    {0: '🫘', 1: '🌱', 2: '🌿'},
    'sapling': {0: '🪴', 1: '🌿', 2: '🌳'}
}
WEATHER_TYPES = { "sunny": {"emoji": "☀️", "name": "맑음", "water_effect": False}, "cloudy": {"emoji": "☁️", "name": "흐림", "water_effect": False}, "rainy": {"emoji": "🌧️", "name": "비", "water_effect": True}, "stormy": {"emoji": "⛈️", "name": "폭풍", "water_effect": True}, }
KST = timezone(timedelta(hours=9))
KST_MIDNIGHT_UPDATE = dt_time(hour=0, minute=5, tzinfo=KST)

async def delete_after(message: discord.WebhookMessage, delay: int):
    """메시지를 보낸 후 지정된 시간 뒤에 삭제하는 헬퍼 함수"""
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden):
        pass

async def preload_farmable_info(farm_data: Dict) -> Dict[str, Dict]:
    item_names = {p['planted_item_name'] for p in farm_data.get('farm_plots', []) if p.get('planted_item_name')}
    if not item_names: return {}
    tasks = [get_farmable_item_info(name) for name in item_names]
    results = await asyncio.gather(*tasks)
    return {info['item_name']: info for info in results if info}

class ConfirmationView(ui.View):
    def __init__(self, user: discord.User): super().__init__(timeout=60); self.value = None; self.user = user
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id: await interaction.response.send_message("❌ 본인 전용 메뉴입니다.", ephemeral=True); return False
        return True
    @ui.button(label="예", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button): self.value = True; await interaction.response.defer(); self.stop()
    @ui.button(label="아니요", style=discord.ButtonStyle.grey)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button): self.value = False; await interaction.response.defer(); self.stop()

class FarmNameModal(ui.Modal, title="농장 이름 변경"):
    farm_name = ui.TextInput(label="새로운 농장 이름", placeholder="새로운 농장 이름을 입력해주세요", required=True, max_length=20)
    def __init__(self, cog: 'Farm', farm_data: Dict):
        super().__init__()
        self.cog, self.farm_data = cog, farm_data
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        new_name = self.farm_name.value
        thread = self.cog.bot.get_channel(self.farm_data['thread_id'])
        if thread:
            try: await thread.edit(name=f"🌱｜{new_name}")
            except Exception as e: logger.error(f"농장 스레드 이름 변경 실패: {e}")
        await supabase.table('farms').update({'name': new_name}).eq('id', self.farm_data['id']).execute()
        
        updated_farm_data = await get_farm_data(self.farm_data['user_id'])
        owner = self.cog.bot.get_user(self.farm_data['user_id'])
        if updated_farm_data and owner and thread:
             await self.cog.update_farm_ui(thread, owner, updated_farm_data)

class FarmActionView(ui.View):
    def __init__(self, parent_cog: 'Farm', farm_data: Dict, user: discord.User, action_type: str, farm_owner_id: int):
        super().__init__(timeout=180)
        self.cog, self.farm_data, self.user, self.action_type, self.farm_owner_id = parent_cog, farm_data, user, action_type, farm_owner_id
        self.selected_item: Optional[str] = None
    async def send_initial_message(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.build_components()
        embed = self.build_embed()
        await interaction.followup.send(embed=embed, view=self, ephemeral=True)
    def build_embed(self) -> discord.Embed:
        titles = {"plant_seed": "🌱 씨앗 선택", "plant_location": "📍 위치 선택", "uproot": "❌ 작물 제거"}
        descs = {"plant_seed": "인벤토리에서 심고 싶은 씨앗이나 묘목을 선택해주세요.", "plant_location": f"선택한 '{self.selected_item}'을(를) 심을 위치를 선택해주세요.", "uproot": "제거하고 싶은 작물이나 나무를 선택해주세요. 이 작업은 되돌릴 수 없습니다."}
        return discord.Embed(title=titles.get(self.action_type, "오류"), description=descs.get(self.action_type, "알 수 없는 작업입니다."), color=0x8BC34A)
    async def build_components(self):
        self.clear_items()
        if self.action_type == "plant_seed": await self._build_seed_select()
        elif self.action_type == "plant_location": await self._build_location_select()
        elif self.action_type == "uproot": await self._build_uproot_select()
        back_button = ui.Button(label="농장으로 돌아가기", style=discord.ButtonStyle.grey, row=4)
        back_button.callback = self.cancel_action
        self.add_item(back_button)
    async def _build_seed_select(self):
        inventory = await get_inventory(self.user)
        farmable_items = {n: q for n, q in inventory.items() if get_item_database().get(n, {}).get('category') == '농장_씨앗'}
        if not farmable_items: self.add_item(ui.Button(label="심을 수 있는 씨앗이 없습니다.", disabled=True)); return
        options = [discord.SelectOption(label=f"{name} ({qty}개)", value=name) for name, qty in farmable_items.items()]
        select = ui.Select(placeholder="씨앗/묘목 선택...", options=options, custom_id="seed_select")
        select.callback = self.on_seed_select
        self.add_item(select)
    async def on_seed_select(self, interaction: discord.Interaction):
        self.selected_item = interaction.data['values'][0]
        self.action_type = "plant_location"
        await self.refresh_view(interaction)
        
    async def _build_location_select(self):
        available_plots = [p for p in self.farm_data['farm_plots'] if p['state'] == 'tilled']
        
        inventory = await get_inventory(self.user)
        num_seeds = inventory.get(self.selected_item, 0)

        if not available_plots: 
            self.add_item(ui.Button(label="경작된 빈 땅이 없습니다.", disabled=True))
            return
        if num_seeds == 0:
            self.add_item(ui.Button(label=f"'{self.selected_item}' 씨앗이 부족합니다.", disabled=True))
            return

        options = [discord.SelectOption(label=f"{p['pos_y']+1}행 {p['pos_x']+1}열", value=f"{p['id']}") for p in available_plots]
        
        max_selectable = min(len(available_plots), num_seeds, 25)
        
        select = ui.Select(
            placeholder=f"심을 위치를 선택하세요 (최대 {max_selectable}개)", 
            options=options[:25], 
            min_values=1,
            max_values=max_selectable,
            custom_id="location_select"
        )
        select.callback = self.on_location_select
        self.add_item(select)
        
    async def on_location_select(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        plot_ids_to_plant = [int(val) for val in interaction.data['values']]
        num_planted = len(plot_ids_to_plant)
        
        now = datetime.now(timezone.utc)
        weather_key = get_config("current_weather", "sunny")
        is_raining = WEATHER_TYPES.get(weather_key, {}).get('water_effect', False)
        
        updates_payload = {
            'state': 'planted', 
            'planted_item_name': self.selected_item, 
            'planted_at': now.isoformat(), 
            'growth_stage': 0, 
            'quality': 5, 
            'last_watered_at': now.isoformat() if is_raining else None,
            'water_count': 1 if is_raining else 0,
            'is_regrowing': False,
            'water_retention_used': False
        }
        
        user_abilities = await get_user_abilities(self.user.id)
        seeds_to_deduct = num_planted
        seeds_saved = 0
        if 'farm_seed_saver_1' in user_abilities:
            for _ in range(num_planted):
                if random.random() < 0.2:
                    seeds_saved += 1
            seeds_to_deduct -= seeds_saved

        db_tasks = []
        db_tasks.append(
            supabase.table('farm_plots').update(updates_payload).in_('id', plot_ids_to_plant).execute()
        )
        if seeds_to_deduct > 0:
            db_tasks.append(update_inventory(self.user.id, self.selected_item, -seeds_to_deduct))

        await asyncio.gather(*db_tasks)
        
        updated_farm_data = await get_farm_data(self.farm_owner_id)
        owner = self.cog.bot.get_user(self.farm_owner_id)
        if updated_farm_data and owner:
            await self.cog.update_farm_ui(interaction.channel, owner, updated_farm_data)
        
        followup_message = f"✅ '{self.selected_item}'을(를) {num_planted}곳에 심었습니다."
        if seeds_saved > 0:
            followup_message += f"\n✨ 능력 효과로 씨앗 {seeds_saved}개를 절약했습니다!"
        if is_raining:
            followup_message += "\n🌧️ 비가 와서 자동으로 물이 뿌려졌습니다!"
        
        msg = await interaction.followup.send(followup_message, ephemeral=True)
        self.cog.bot.loop.create_task(delete_after(msg, 10))
        
        await interaction.delete_original_response()

    async def _build_uproot_select(self):
        plots = [p for p in self.farm_data['farm_plots'] if p['state'] in ['planted', 'withered']]
        if not plots: 
            self.add_item(ui.Button(label="정리할 작물이 없습니다.", disabled=True)); return
        
        options = []
        for plot in sorted(plots, key=lambda p: (p['pos_y'], p['pos_x'])):
            name = plot['planted_item_name'] or "시든 작물"
            label = f"{'🥀' if plot['state'] == 'withered' else ''}{name} ({plot['pos_y']+1}행 {plot['pos_x']+1}열)"
            options.append(discord.SelectOption(label=label, value=str(plot['id'])))
        
        max_selectable = min(len(options), 25)
        select = ui.Select(
            placeholder="제거할 작물을 여러 개 선택하세요...", 
            options=options[:max_selectable], 
            custom_id="uproot_select",
            min_values=1,
            max_values=max_selectable
        )
        select.callback = self.on_uproot_select
        self.add_item(select)

    async def on_uproot_select(self, interaction: discord.Interaction):
        plot_ids_to_uproot = [int(val) for val in interaction.data['values']]
        count = len(plot_ids_to_uproot)
        
        view = ConfirmationView(self.user)
        await interaction.response.send_message(
            f"정말로 **{count}개**의 작물을 제거하시겠습니까?\n이 작업은 되돌릴 수 없습니다.", 
            view=view, 
            ephemeral=True
        )
        await view.wait()
        
        if view.value:
            await clear_plots_db(plot_ids_to_uproot)
            
            updated_farm_data = await get_farm_data(self.farm_owner_id)
            owner = self.cog.bot.get_user(self.farm_owner_id)
            if updated_farm_data and owner:
                await self.cog.update_farm_ui(interaction.channel, owner, updated_farm_data)

            await interaction.edit_original_response(content=f"✅ {count}개의 작물을 제거했습니다.", view=None)
        else:
            await interaction.edit_original_response(content="취소되었습니다.", view=None)
    async def cancel_action(self, interaction: discord.Interaction):
        await interaction.response.defer(); await interaction.delete_original_response()
    async def refresh_view(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.build_components()
        await interaction.edit_original_response(embed=self.build_embed(), view=self)

class FarmUIView(ui.View):
    def __init__(self, cog_instance: 'Farm'):
        super().__init__(timeout=None)
        self.cog = cog_instance
        buttons = [
            ui.Button(label="밭 갈기", emoji="🪓", row=0, custom_id="farm_till"), 
            ui.Button(label="씨앗 심기", emoji="🌱", row=0, custom_id="farm_plant"), 
            ui.Button(label="물 주기", emoji="💧", row=0, custom_id="farm_water"), 
            ui.Button(label="수확하기", emoji="🧺", row=0, custom_id="farm_harvest"), 
            ui.Button(label="밭 정리", emoji="🧹", row=0, custom_id="farm_uproot"), 
            ui.Button(label="농장에 초대", emoji="📢", row=1, custom_id="farm_invite"), 
            ui.Button(label="권한 부여", emoji="🤝", row=1, custom_id="farm_share"), 
            ui.Button(label="이름 변경", emoji="✏️", row=1, custom_id="farm_rename"),
            ui.Button(label="새로고침", emoji="🔄", row=1, custom_id="farm_regenerate")
        ]
        for item in buttons:
            item.callback = self.dispatch_callback
            self.add_item(item)
    
    async def dispatch_callback(self, interaction: discord.Interaction):
        method_name = f"on_{interaction.data['custom_id']}_click"
        if hasattr(self, method_name):
            await getattr(self, method_name)(interaction)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        self.farm_owner_id = await get_farm_owner_by_thread(interaction.channel.id)
        if not self.farm_owner_id: 
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ 이 농장의 정보를 찾을 수 없습니다.", ephemeral=True, delete_after=5)
            return False
        
        if interaction.user.id == self.farm_owner_id: 
            return True
        
        if interaction.data['custom_id'] in ["farm_invite", "farm_share", "farm_rename"]: 
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ 이 작업은 농장 소유자만 할 수 있습니다.", ephemeral=True, delete_after=5)
            return False
        
        farm_data = await get_farm_data(self.farm_owner_id)
        if not farm_data: return False

        action_map = { "farm_till": "till", "farm_plant": "plant", "farm_water": "water", "farm_harvest": "harvest", "farm_uproot": "plant", "farm_regenerate": "till" }
        action = action_map.get(interaction.data['custom_id'])
        
        if not action: return False 
            
        has_perm = await check_farm_permission(farm_data['id'], interaction.user.id, action)
        if not has_perm: 
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ 이 작업을 수행할 권한이 없습니다.", ephemeral=True, delete_after=5)
        return has_perm
        
    async def on_error(self, i: discord.Interaction, e: Exception, item: ui.Item) -> None:
        logger.error(f"FarmUIView 오류 (item: {item.custom_id}): {e}", exc_info=True)
        msg_content = "❌ 처리 중 예기치 않은 오류가 발생했습니다."
        if not i.response.is_done():
            await i.response.send_message(msg_content, ephemeral=True, delete_after=5)
        else:
            try:
                msg = await i.followup.send(msg_content, ephemeral=True)
                self.cog.bot.loop.create_task(delete_after(msg, 5))
            except discord.HTTPException:
                pass
        
    async def on_farm_regenerate_click(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if interaction.message: await interaction.message.delete()
        except (discord.NotFound, discord.Forbidden) as e:
            logger.warning(f"재설치 시 이전 패널 삭제 실패: {e}")
        updated_farm_data = await get_farm_data(self.farm_owner_id)
        owner = self.cog.bot.get_user(self.farm_owner_id)
        if updated_farm_data and owner:
            updated_farm_data['farm_message_id'] = None
            await self.cog.update_farm_ui(interaction.channel, owner, updated_farm_data)
        await interaction.delete_original_response()

    async def on_farm_till_click(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        gear = await get_user_gear(interaction.user)
        hoe = gear.get('hoe', BARE_HANDS)
        if hoe == BARE_HANDS:
            msg = await interaction.followup.send("❌ 먼저 상점에서 '괭이'를 구매하고 프로필 화면에서 장착해주세요.", ephemeral=True)
            self.cog.bot.loop.create_task(delete_after(msg, 10))
            return
        power = get_item_database().get(hoe, {}).get('power', 1)
        farm_data = await get_farm_data(self.farm_owner_id)
        if not farm_data: return
        tilled, plots_to_update_db = 0, []
        
        sorted_plots = sorted(farm_data['farm_plots'], key=lambda p: (p['pos_y'], p['pos_x']))

        for plot in sorted_plots:
            if plot['state'] == 'default' and tilled < power:
                plots_to_update_db.append(plot['id'])
                tilled += 1
        
        if not tilled:
            msg = await interaction.followup.send("ℹ️ 더 이상 갈 수 있는 밭이 없습니다.", ephemeral=True)
            self.cog.bot.loop.create_task(delete_after(msg, 5))
            return
            
        await supabase.table('farm_plots').update({'state': 'tilled'}).in_('id', plots_to_update_db).execute()
        updated_farm_data = await get_farm_data(self.farm_owner_id)
        owner = self.cog.bot.get_user(self.farm_owner_id)
        if updated_farm_data and owner:
            await self.cog.update_farm_ui(interaction.channel, owner, updated_farm_data)
        await interaction.delete_original_response()
    
    async def on_farm_plant_click(self, i: discord.Interaction): 
        farm_data = await get_farm_data(self.farm_owner_id)
        if not farm_data: return
        view = FarmActionView(self.cog, farm_data, i.user, "plant_seed", self.farm_owner_id)
        await view.send_initial_message(i)
        
    async def on_farm_water_click(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        gear = await get_user_gear(interaction.user)
        can = gear.get('watering_can', BARE_HANDS)
        if can == BARE_HANDS:
            msg = await interaction.followup.send("❌ 먼저 상점에서 '물뿌리개'를 구매하고 프로필 화면에서 장착해주세요.", ephemeral=True)
            self.cog.bot.loop.create_task(delete_after(msg, 10))
            return
        power = get_item_database().get(can, {}).get('power', 1)
        today_jst_midnight = datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0)
        farm_data = await get_farm_data(self.farm_owner_id)
        if not farm_data: return
        plots_to_update_db = set()
        watered_count = 0
        for p in sorted(farm_data['farm_plots'], key=lambda x: (x['pos_y'], x['pos_x'])):
            if watered_count >= power: break
            last_watered_dt = datetime.fromisoformat(p['last_watered_at']) if p.get('last_watered_at') else datetime.fromtimestamp(0, tz=timezone.utc)
            if p['state'] == 'planted' and last_watered_dt.astimezone(KST) < today_jst_midnight:
                plots_to_update_db.add(p['id'])
                watered_count += 1
        if not plots_to_update_db:
            msg = await interaction.followup.send("ℹ️ 물을 줄 필요가 있는 작물이 없습니다.", ephemeral=True)
            self.cog.bot.loop.create_task(delete_after(msg, 5))
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        tasks = [
            supabase.table('farm_plots').update({'last_watered_at': now_iso, 'water_retention_used': False}).in_('id', list(plots_to_update_db)).execute(),
            supabase.rpc('increment_water_count', {'plot_ids': list(plots_to_update_db)}).execute()
        ]
        await asyncio.gather(*tasks)
        updated_farm_data = await get_farm_data(self.farm_owner_id)
        owner = self.cog.bot.get_user(self.farm_owner_id)
        if updated_farm_data and owner:
            await self.cog.update_farm_ui(interaction.channel, owner, updated_farm_data, message=interaction.message)
        await interaction.delete_original_response()
        
    async def on_farm_uproot_click(self, i: discord.Interaction): 
        farm_data = await get_farm_data(self.farm_owner_id)
        if not farm_data: return
        view = FarmActionView(self.cog, farm_data, i.user, "uproot", self.farm_owner_id)
        await view.send_initial_message(i)
        
    async def on_farm_harvest_click(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        farm_data = await get_farm_data(self.farm_owner_id)
        if not farm_data: return
        harvested, plots_to_reset, trees_to_update = {}, [], {}
        info_map = await preload_farmable_info(farm_data)
        owner_abilities = await get_user_abilities(self.farm_owner_id)
        yield_bonus = 0.5 if 'farm_yield_up_2' in owner_abilities else 0.0
        for p in farm_data['farm_plots']:
            info = info_map.get(p['planted_item_name'])
            if not info: continue
            if p['state'] == 'planted' and p['growth_stage'] >= info.get('max_growth_stage', 3):
                quality = p['quality']
                yield_mult = 1.0 + (quality / 100.0) + yield_bonus
                final_yield = max(1, round(info.get('base_yield', 1) * yield_mult))
                harvest_name = info['harvest_item_name']
                harvested[harvest_name] = harvested.get(harvest_name, 0) + final_yield
                if info.get('is_tree') is True:
                    max_stage = info.get('max_growth_stage', 3)
                    regrowth = info.get('regrowth_days', 1)
                    new_growth_stage = max(0, max_stage - regrowth)
                    trees_to_update[p['id']] = {'stage': new_growth_stage, 'is_regrowing': True}
                else: 
                    plots_to_reset.append(p['id'])
        if not harvested:
            msg = await interaction.followup.send("ℹ️ 수확할 수 있는 작물이 없습니다.", ephemeral=True)
            self.cog.bot.loop.create_task(delete_after(msg, 5))
            return
        owner = self.cog.bot.get_user(self.farm_owner_id)
        if not owner: return
        total_harvested_amount = sum(harvested.values())
        xp_per_crop = get_config("GAME_CONFIG", {}).get("XP_FROM_FARMING", 15)
        total_xp = total_harvested_amount * xp_per_crop
        if total_harvested_amount > 0:
            await log_activity(owner.id, 'farm_harvest', amount=total_harvested_amount, xp_earned=total_xp)
        db_tasks = []
        for name, quantity in harvested.items():
            db_tasks.append(update_inventory(str(owner.id), name, quantity))
        if plots_to_reset: db_tasks.append(clear_plots_db(plots_to_reset))
        if trees_to_update:
            now_iso = datetime.now(timezone.utc).isoformat()
            for pid, update_data in trees_to_update.items():
                db_tasks.append(update_plot(pid, {
                    'growth_stage': update_data['stage'],
                    'is_regrowing': update_data['is_regrowing'],
                    'planted_at': now_iso,
                    'last_watered_at': now_iso,
                    'quality': 5,
                    'water_retention_used': False
                }))
        if total_xp > 0:
            db_tasks.append(supabase.rpc('add_xp', {'p_user_id': str(owner.id), 'p_xp_to_add': total_xp, 'p_source': 'farming'}).execute())
        results = await asyncio.gather(*db_tasks, return_exceptions=True)
        updated_farm_data = await get_farm_data(self.farm_owner_id)
        if updated_farm_data:
            await self.cog.update_farm_ui(interaction.channel, owner, updated_farm_data)
        followup_message = f"🎉 **{', '.join([f'{n} {q}개' for n, q in harvested.items()])}**을(를) 수확했습니다!"
        if yield_bonus > 0.0:
            followup_message += "\n✨ **대농**의 능력으로 수확량이 대폭 증가했습니다!"
        msg = await interaction.followup.send(followup_message, ephemeral=True)
        self.cog.bot.loop.create_task(delete_after(msg, 10))
        for res in results:
            if isinstance(res, dict) and 'data' in res and res.data and isinstance(res.data, list) and res.data[0].get('leveled_up'):
                if (level_cog := self.cog.bot.get_cog("LevelSystem")):
                    await level_cog.handle_level_up_event(owner, res.data)
                break
    
    async def on_farm_invite_click(self, i: discord.Interaction):
        view = ui.View(timeout=180)
        select = ui.UserSelect(placeholder="농장에 초대할 유저를 선택하세요...")
        async def cb(si: discord.Interaction):
            await si.response.defer(ephemeral=True)
            for user_id_str in si.data.get('values', []):
                try: 
                    user = self.cog.bot.get_user(int(user_id_str))
                    if user: await i.channel.add_user(user)
                except Exception: pass
            await i.edit_original_response(content="초대가 완료되었습니다.", view=None)
        select.callback = cb
        view.add_item(select)
        await i.response.send_message("누구를 농장에 초대하시겠습니까?", view=view, ephemeral=True)

    async def on_farm_share_click(self, i: discord.Interaction):
        view = ui.View(timeout=180)
        select = ui.UserSelect(placeholder="권한을 부여할 유저를 선택하세요...")
        async def cb(si: discord.Interaction):
            await si.response.defer(ephemeral=True)
            farm_data = await get_farm_data(self.farm_owner_id)
            if not farm_data: return
            users_to_grant = [self.cog.bot.get_user(int(uid)) for uid in si.data.get('values', [])]
            for user in users_to_grant:
                if user: await grant_farm_permission(farm_data['id'], user.id)
            await i.edit_original_response(content=f"{', '.join(u.display_name for u in users_to_grant if u)}님에게 권한을 부여했습니다.", view=None)
        select.callback = cb
        view.add_item(select)
        await i.response.send_message("누구에게 농장 권한을 주시겠습니까?", view=view, ephemeral=True)

    async def on_farm_rename_click(self, i: discord.Interaction): 
        farm_data = await get_farm_data(self.farm_owner_id)
        if not farm_data: return
        await i.response.send_modal(FarmNameModal(self.cog, farm_data))

class FarmCreationPanelView(ui.View):
    def __init__(self, cog: 'Farm'):
        super().__init__(timeout=None)
        self.cog = cog
        btn = ui.Button(label="농장 만들기", style=discord.ButtonStyle.success, emoji="🌱", custom_id="farm_create_button")
        btn.callback = self.create_farm_callback
        self.add_item(btn)
    async def create_farm_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user = interaction.user
        farm_data = await get_farm_data(user.id)
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.followup.send("❌ 이 명령어는 텍스트 채널에서만 사용할 수 있습니다.", ephemeral=True); return
        if farm_data and farm_data.get('thread_id'):
            if thread := self.cog.bot.get_channel(farm_data['thread_id']):
                await interaction.followup.send(f"✅ 당신의 농장은 여기입니다: {thread.mention}", ephemeral=True)
                try: await thread.add_user(user)
                except: pass
            else: await self.cog.create_new_farm_thread(interaction, user)
        else: await self.cog.create_new_farm_thread(interaction, user)

class Farm(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.thread_locks: Dict[int, asyncio.Lock] = {}
        self.daily_crop_update.start()
        self.farm_ui_updater_task.start()
        
    def cog_unload(self):
        self.daily_crop_update.cancel()
        self.farm_ui_updater_task.cancel()
            
    async def register_persistent_views(self):
        self.bot.add_view(FarmCreationPanelView(self))
        self.bot.add_view(FarmUIView(self))
        logger.info("✅ 농장 관련 영구 View가 정상적으로 등록되었습니다.")
        
    @tasks.loop(time=KST_MIDNIGHT_UPDATE)
    async def daily_crop_update(self):
        logger.info("일일 작물 상태 업데이트 시작...")
        try:
            weather_key = get_config("current_weather", "sunny")
            is_raining = WEATHER_TYPES.get(weather_key, {}).get('water_effect', False)
            
            planted_plots_res = await supabase.table('farm_plots').select('*, farms!inner(user_id, id, thread_id)').eq('state', 'planted').execute()
            
            if not (planted_plots_res and planted_plots_res.data):
                logger.info("업데이트할 작물이 없습니다.")
                return

            all_plots = planted_plots_res.data
            
            item_names = {p['planted_item_name'] for p in all_plots if p.get('planted_item_name')}
            owner_ids = {p['farms']['user_id'] for p in all_plots if p.get('farms')}
            
            item_info_tasks = [get_farmable_item_info(name) for name in item_names]
            abilities_tasks = [get_user_abilities(uid) for uid in owner_ids]

            item_info_results, abilities_results = await asyncio.gather(asyncio.gather(*item_info_tasks), asyncio.gather(*abilities_tasks))
            
            item_info_map = {info['item_name']: info for info in item_info_results if info}
            owner_abilities_map = {uid: set(abilities) for uid, abilities in zip(owner_ids, abilities_results)}

            plots_to_update_db = []
            today_jst_midnight = datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0)
            
            growth_ability_activations = defaultdict(lambda: {'count': 0, 'thread_id': None})

            for plot in all_plots:
                update_payload = plot.copy()
                del update_payload['farms']

                owner_id = plot.get('farms', {}).get('user_id')
                item_info = item_info_map.get(plot['planted_item_name'])
                if not owner_id or not item_info:
                    continue
                
                if plot['growth_stage'] >= item_info.get('max_growth_stage', 99):
                    continue

                owner_abilities = owner_abilities_map.get(owner_id, set())
                last_watered_dt = datetime.fromisoformat(plot['last_watered_at']) if plot.get('last_watered_at') else datetime.fromtimestamp(0, tz=timezone.utc)
                
                is_watered_today = last_watered_dt.astimezone(KST) >= today_jst_midnight or is_raining
                
                # ▼▼▼ [진짜 최종 수정] 사용자님의 시나리오를 완벽히 구현하는 로직 ▼▼▼
                if is_watered_today:
                    # 물을 줬으므로 성장하고, 능력 플래그는 초기화
                    update_payload['water_retention_used'] = False
                    
                    growth_amount = 1
                    if 'farm_growth_speed_up_2' in owner_abilities and not plot.get('is_regrowing', False):
                        growth_amount += 1
                        growth_ability_activations[owner_id]['count'] += 1
                        growth_ability_activations[owner_id]['thread_id'] = plot['farms']['thread_id']
                    
                    update_payload['growth_stage'] = min(
                        plot['growth_stage'] + growth_amount,
                        item_info.get('max_growth_stage', 99)
                    )
                else: 
                    # 물을 안 줬을 때
                    if 'farm_water_retention_1' in owner_abilities and plot.get('water_retention_used') is not True:
                        # 능력이 있고, 아직 사용 안 함 -> 능력 사용하고 성장
                        update_payload['water_retention_used'] = True
                        
                        growth_amount = 1
                        if 'farm_growth_speed_up_2' in owner_abilities and not plot.get('is_regrowing', False):
                            growth_amount += 1
                            # 능력으로 성장할 때도 성장 가속은 적용
                            growth_ability_activations[owner_id]['count'] += 1
                            growth_ability_activations[owner_id]['thread_id'] = plot['farms']['thread_id']
                        
                        update_payload['growth_stage'] = min(
                            plot['growth_stage'] + growth_amount,
                            item_info.get('max_growth_stage', 99)
                        )
                    else:
                        # 능력이 없거나, 이미 사용함 -> 시듦
                        update_payload['state'] = 'withered'
                # ▲▲▲ [진짜 최종 수정] ▲▲▲
                
                plots_to_update_db.append(update_payload)

            if plots_to_update_db:
                await supabase.table('farm_plots').upsert(plots_to_update_db).execute()
                logger.info(f"일일 작물 업데이트 완료. {len(plots_to_update_db)}개의 밭이 영향을 받았습니다. UI 업데이트를 요청합니다.")
                
                affected_farms = {p['farms']['user_id'] for p in all_plots if p.get('farms')}
                for user_id in affected_farms:
                    await self.request_farm_ui_update(user_id)
            else:
                logger.info("상태가 변경된 작물이 없습니다.")

            for user_id, data in growth_ability_activations.items():
                if data['count'] > 0 and data['thread_id']:
                    try:
                        thread = self.bot.get_channel(data['thread_id'])
                        if thread:
                            await thread.send(f"**[농장 알림]**\n오늘 농장 업데이트에서 **성장 속도 UP (대)** 능력이 발동하여, {data['count']}개의 작물이 추가로 성장했습니다!", delete_after=3600)
                    except Exception as e:
                        logger.error(f"{user_id}의 농장 스레드({data['thread_id']})에 능력 발동 메시지 전송 중 오류: {e}")

        except Exception as e:
            logger.error(f"일일 작물 업데이트 중 오류: {e}", exc_info=True)
            
    @daily_crop_update.before_loop
    async def before_daily_crop_update(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=5.0)
    async def farm_ui_updater_task(self):
        response = None
        for attempt in range(3):
            try:
                response = await supabase.table('bot_configs').select('config_key, config_value').like('config_key', 'farm_ui_update_request_%').execute()
                break
            except Exception as e:
                if attempt < 2:
                    logger.warning(f"농장 UI 업데이트 요청 조회 중 오류 발생 (시도 {attempt + 1}/3), 2초 후 재시도합니다: {e}")
                    await asyncio.sleep(2)
                else:
                    logger.error(f"농장 UI 업데이트 루프 중 오류 (다음 루프에서 재시도합니다): {e}", exc_info=True)
                    return

        try:
            if not response or not response.data: 
                return
            
            keys_to_delete = [req['config_key'] for req in response.data]
            
            tasks = []
            for req in response.data:
                try:
                    user_id = int(req['config_key'].split('_')[-1])
                    user = self.bot.get_user(user_id)
                    farm_data = await get_farm_data(user_id)
                    if user and farm_data and farm_data.get('thread_id'):
                        if thread := self.bot.get_channel(farm_data['thread_id']):
                            force_new = req.get('config_value', {}).get('force_new', False)
                            tasks.append(self.update_farm_ui(thread, user, farm_data, force_new))
                except (ValueError, IndexError):
                    logger.warning(f"잘못된 형식의 농장 UI 업데이트 요청 키 발견: {req.get('config_key')}")

            if tasks:
                await asyncio.gather(*tasks)

            if keys_to_delete:
                await supabase.table('bot_configs').delete().in_('config_key', keys_to_delete).execute()

        except Exception as e:
            logger.error(f"농장 UI 업데이트 처리 로직 중 오류 발생: {e}", exc_info=True)

    @farm_ui_updater_task.before_loop
    async def before_farm_ui_updater_task(self):
        await self.bot.wait_until_ready()

    async def request_farm_ui_update(self, user_id: int, force_new: bool = False):
        config_key = f"farm_ui_update_request_{user_id}"
        config_value = {"timestamp": time.time(), "force_new": force_new}
        await save_config_to_db(config_key, config_value)
        
    async def build_farm_embed(self, farm_data: Dict, user: discord.User) -> discord.Embed:
        info_map = await preload_farmable_info(farm_data)
        
        plot_count = len(farm_data.get('farm_plots', []))
        
        sx, sy = 5, 5
        
        plots = {(p['pos_x'], p['pos_y']): p for p in farm_data.get('farm_plots', [])}
        grid, infos = [['' for _ in range(sx)] for _ in range(sy)], []
        today_jst_midnight = datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0)

        for y in range(sy):
            for x in range(sx):
                is_owned_plot = (y * sx + x) < plot_count
                emoji = '⬛'
                
                if is_owned_plot:
                    plot = plots.get((x, y))
                    emoji = '🟤'
                    if plot and plot['state'] != 'default':
                        state = plot['state']
                        if state == 'tilled': emoji = '🟫'
                        elif state == 'withered': emoji = '🥀'
                        elif state == 'planted':
                            name = plot['planted_item_name']
                            info = info_map.get(name)
                            if info:
                                stage = plot['growth_stage']
                                max_stage = info.get('max_growth_stage', 3)
                                item_type = info.get('item_type', 'seed')

                                if stage >= max_stage:
                                    emoji = info.get('item_emoji', '❓')
                                else:
                                    if item_type == 'sapling':
                                        if plot.get('is_regrowing', False):
                                            emoji = '🌳'
                                        else:
                                            emoji = CROP_EMOJI_MAP.get('sapling', {}).get(stage, '🪴')
                                    else:
                                        emoji = CROP_EMOJI_MAP.get('seed', {}).get(stage, '🌱')
                                
                                last_watered_dt = datetime.fromisoformat(plot['last_watered_at']) if plot.get('last_watered_at') else datetime.fromtimestamp(0, tz=timezone.utc)
                                last_watered_jst = last_watered_dt.astimezone(KST)
                                water_emoji = '💧' if last_watered_jst >= today_jst_midnight else '➖'
                                
                                growth_status_text = ""
                                if stage >= max_stage:
                                    growth_status_text = "수확 가능! 🧺"
                                else:
                                    days_to_grow = max_stage - stage
                                    growth_status_text = f"남은 날: {days_to_grow}일"

                                info_text = f"{emoji} **{name}** (물: {water_emoji}): {growth_status_text}"
                                infos.append(info_text)

                grid[y][x] = emoji

        farm_str = "\n".join("".join(row) for row in grid)
        farm_name = farm_data.get('name') or user.display_name
        embed = discord.Embed(title=f"**{farm_name}님의 농장**", color=0x8BC34A)
        
        description_parts = [f"```{farm_str}```"]
        
        if infos:
            description_parts.append("\n".join(sorted(infos)))
        
        owner_abilities = await get_user_abilities(user.id)
        
        all_farm_abilities_map = {}
        job_advancement_data = get_config("JOB_ADVANCEMENT_DATA", {})
        
        if isinstance(job_advancement_data, dict):
            for level, level_data in job_advancement_data.items():
                for job in level_data:
                    if 'farmer' in job.get('job_key', ''):
                        for ability in job.get('abilities', []):
                            all_farm_abilities_map[ability['ability_key']] = {'name': ability['ability_name'], 'description': ability['description']}
        
        active_effects = []
        EMOJI_MAP = {'seed': '🌱', 'water': '💧', 'yield': '🧺', 'growth': '⏱️'}
        
        for ability_key in owner_abilities:
            if ability_key in all_farm_abilities_map:
                ability_info = all_farm_abilities_map[ability_key]
                emoji = next((e for key, e in EMOJI_MAP.items() if key in ability_key), '✨')
                active_effects.append(f"> {emoji} **{ability_info['name']}**: {ability_info['description']}")
        
        if active_effects:
            description_parts.append(f"**--- 농장 패시브 효과 ---**\n" + "\n".join(active_effects))

        weather_key = get_config("current_weather", "sunny")
        weather = WEATHER_TYPES.get(weather_key, {"emoji": "❔", "name": "알 수 없음"})
        description_parts.append(f"**오늘의 날씨:** {weather['emoji']} {weather['name']}")
        
        now_kst = discord.utils.utcnow().astimezone(KST)
        next_update_time = now_kst.replace(hour=0, minute=5, second=0, microsecond=0)
        if now_kst >= next_update_time:
            next_update_time += timedelta(days=1)
        
        description_parts.append(f"다음 작물 업데이트: {discord.utils.format_dt(next_update_time, style='R')}")
        
        embed.description = "\n\n".join(description_parts)
        return embed
        
    async def update_farm_ui(self, thread: discord.Thread, user: discord.User, farm_data: Dict, force_new: bool = False, message: discord.Message = None):
        lock = self.thread_locks.setdefault(thread.id, asyncio.Lock())
        async with lock:
            if not (user and farm_data): return

            try:
                message_to_edit = message
                
                if not message_to_edit:
                    message_id = farm_data.get("farm_message_id")
                    if message_id and not force_new:
                        try:
                            message_to_edit = await thread.fetch_message(message_id)
                        except (discord.NotFound, discord.Forbidden):
                            logger.warning(f"농장 메시지(ID: {message_id})를 찾지 못하여 새로 생성합니다.")
                            force_new = True
                
                if force_new and message_to_edit:
                    try:
                        await message_to_edit.delete()
                    except (discord.NotFound, discord.Forbidden):
                        pass
                    message_to_edit = None

                embed = await self.build_farm_embed(farm_data, user)
                view = FarmUIView(self)
                
                if message_to_edit:
                    await message_to_edit.edit(embed=embed, view=view)
                else:
                    if force_new:
                        if embed_data := await get_embed_from_db("farm_thread_welcome"):
                            await thread.send(embed=format_embed_from_db(embed_data, user_name=farm_data.get('name') or user.display_name))
                    
                    new_message = await thread.send(embed=embed, view=view)
                    await supabase.table('farms').update({'farm_message_id': new_message.id}).eq('id', farm_data['id']).execute()
                
            except Exception as e:
                logger.error(f"농장 UI 업데이트 중 오류: {e}", exc_info=True)
                
    async def create_new_farm_thread(self, interaction: discord.Interaction, user: discord.Member):
        try:
            farm_name = f"{user.display_name}의 농장"
            thread = await interaction.channel.create_thread(name=f"🌱｜{farm_name}", type=discord.ChannelType.private_thread)
            await thread.add_user(user)

            await delete_config_from_db(f"farm_state_{user.id}")

            farm_data = await create_farm(user.id)
            if not farm_data:
                await interaction.followup.send("❌ 농장을 초기화하는 데 실패했습니다.", ephemeral=True)
                await thread.delete()
                return
            
            await supabase.table('farms').update({'thread_id': thread.id, 'name': farm_name}).eq('user_id', user.id).execute()
            
            updated_farm_data = await get_farm_data(user.id)
            if updated_farm_data:
                await self.update_farm_ui(thread, user, updated_farm_data, force_new=True)

            await interaction.followup.send(f"✅ 당신만의 농장을 만들었습니다! {thread.mention} 채널을 확인해주세요.", ephemeral=True)
        except Exception as e:
            logger.error(f"농장 생성 중 오류 발생: {e}", exc_info=True)
            await interaction.followup.send("❌ 농장을 만드는 중 오류가 발생했습니다.", ephemeral=True)

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_farm_creation", **kwargs):
        if panel_info := get_panel_id(panel_key):
            if (old_channel := self.bot.get_channel(panel_info['channel_id'])) and (old_message_id := panel_info.get('message_id')):
                try: await (await old_channel.fetch_message(old_message_id)).delete()
                except (discord.NotFound, discord.Forbidden): pass
        if not (embed_data := await get_embed_from_db(panel_key)): return
        new_message = await channel.send(embed=discord.Embed.from_dict(embed_data), view=FarmCreationPanelView(self))
        await save_panel_id(panel_key, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} 패널을 성공적으로 생성했습니다. (채널: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(Farm(bot))
