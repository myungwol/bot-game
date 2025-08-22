# cogs/games/farm.py

import discord
from discord.ext import commands, tasks
from discord import ui
import logging
from typing import Optional, Dict, List, Any
import asyncio
from datetime import datetime, timezone, timedelta, time

from utils.database import (
    get_farm_data, create_farm, get_config,
    save_panel_id, get_panel_id, get_embed_from_db,
    supabase, get_inventory, get_user_gear, update_plot,
    get_farmable_item_info, update_inventory, BARE_HANDS,
    check_farm_permission, grant_farm_permission, clear_plots_db,
    get_farm_owner_by_thread, get_item_database
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

# [✅ 개선] 닫기 버튼 View를 추가하여 사용자 경험을 개선합니다.
class CloseButtonView(ui.View):
    def __init__(self, user: discord.User):
        super().__init__(timeout=180)
        self.user = user

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user.id
        
    @ui.button(label="閉じる", style=discord.ButtonStyle.secondary)
    async def close_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.message.delete()

CROP_EMOJI_MAP = {
    'seed': {0: '🌱', 1: '🌿', 2: '🌾'},
    'sapling': {0: '🌱', 1: '🌳', 2: '🌳'}
}
WEATHER_TYPES = {
    "sunny": {"emoji": "☀️", "name": "晴れ", "water_effect": False},
    "cloudy": {"emoji": "☁️", "name": "曇り", "water_effect": False},
    "rainy": {"emoji": "🌧️", "name": "雨", "water_effect": True},
    "stormy": {"emoji": "⛈️", "name": "嵐", "water_effect": True},
}

# [✅ 현지화] KST를 JST로 변경하여 코드의 명확성을 높입니다.
JST = timezone(timedelta(hours=9))
JST_MIDNIGHT_UPDATE = time(hour=0, minute=1, tzinfo=JST)

async def preload_farmable_info(farm_data: Dict) -> Dict[str, Dict]:
    item_names = {p['planted_item_name'] for p in farm_data.get('farm_plots', []) if p.get('planted_item_name')}
    if not item_names: return {}
    tasks = [get_farmable_item_info(name) for name in item_names]
    results = await asyncio.gather(*tasks)
    return {info['item_name']: info for info in results if info}


# --- UI 클래스 ---
class ConfirmationView(ui.View):
    def __init__(self, user: discord.User):
        super().__init__(timeout=60)
        self.value = None; self.user = user
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("❌ 自分専用のメニューです。", ephemeral=True, view=CloseButtonView(interaction.user)); return False
        return True
    @ui.button(label="はい", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        self.value = True; await interaction.response.defer(); self.stop()
    @ui.button(label="いいえ", style=discord.ButtonStyle.grey)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        self.value = False; await interaction.response.defer(); self.stop()


class FarmActionView(ui.View):
    def __init__(self, parent_cog: 'Farm', farm_data: Dict, user: discord.User, action_type: str):
        super().__init__(timeout=180)
        self.cog = parent_cog
        self.farm_data = farm_data
        self.user = user
        self.action_type = action_type
        self.selected_item: Optional[str] = None

    async def send_initial_message(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.build_components()
        embed = self.build_embed()
        await interaction.followup.send(embed=embed, view=self, ephemeral=True)

    def build_embed(self) -> discord.Embed:
        titles = { "plant_seed": "🌱 種を選択", "plant_location": "📍 場所を選択", "uproot": "❌ 作物を撤去" }
        descs = {
            "plant_seed": "インベントリから植えたい種または苗木を選択してください。",
            "plant_location": f"選択した「{self.selected_item}」を植える場所を選択してください。",
            "uproot": "撤去したい作物または木を選択してください。この操作は元に戻せません。"
        }
        embed = discord.Embed(
            title=titles.get(self.action_type, "エラー"),
            description=descs.get(self.action_type, "不明なアクションです。"),
            color=0x8BC34A
        )
        return embed

    async def build_components(self):
        self.clear_items()
        if self.action_type == "plant_seed": await self._build_seed_select()
        elif self.action_type == "plant_location": await self._build_location_select()
        elif self.action_type == "uproot": await self._build_uproot_select()
        back_button = ui.Button(label="農場に戻る", style=discord.ButtonStyle.grey, row=4)
        back_button.callback = self.cancel_action
        self.add_item(back_button)

    async def _build_seed_select(self):
        inventory = await get_inventory(self.user)
        farmable_items_in_inv = {name: qty for name, qty in inventory.items() if get_item_database().get(name, {}).get('category') == '農場_種'}
        if not farmable_items_in_inv:
            self.add_item(ui.Button(label="植えられる種がありません。", disabled=True)); return
        options = [discord.SelectOption(label=f"{name} ({qty}個)", value=name) for name, qty in farmable_items_in_inv.items()]
        select = ui.Select(placeholder="種/苗木を選択...", options=options, custom_id="seed_select")
        select.callback = self.on_seed_select
        self.add_item(select)

    async def on_seed_select(self, interaction: discord.Interaction):
        self.selected_item = interaction.data['values'][0]
        self.action_type = "plant_location"
        await self.refresh_view(interaction)
    
    async def _build_location_select(self):
        farmable_info = await get_farmable_item_info(self.selected_item)
        if not farmable_info: return
        size_x, size_y = farmable_info['space_required_x'], farmable_info['space_required_y']
        available_plots = self._find_available_space(size_x, size_y)
        if not available_plots:
            self.add_item(ui.Button(label=f"{size_x}x{size_y}の空き地がありません。", disabled=True)); return
        options = [discord.SelectOption(label=f"{plot['pos_y']+1}行 {plot['pos_x']+1}列", value=f"{plot['pos_x']},{plot['pos_y']}") for plot in available_plots]
        select = ui.Select(placeholder="植える場所を選択...", options=options, custom_id="location_select")
        select.callback = self.on_location_select
        self.add_item(select)
    
    def _find_available_space(self, required_x: int, required_y: int) -> List[Dict]:
        farm_size_x, farm_size_y = self.farm_data['size_x'], self.farm_data['size_y']
        plots = {(p['pos_x'], p['pos_y']): p for p in self.farm_data['farm_plots']}
        valid_top_lefts = []
        for y in range(farm_size_y - required_y + 1):
            for x in range(farm_size_x - required_x + 1):
                is_space_free = all(plots.get((x + dx, y + dy)) and plots[(x + dx, y + dy)]['state'] == 'tilled'
                                    for dy in range(required_y) for dx in range(required_x))
                if is_space_free: valid_top_lefts.append(plots[(x, y)])
        return valid_top_lefts

    async def on_location_select(self, interaction: discord.Interaction):
        await interaction.response.defer()
        pos_x, pos_y = map(int, interaction.data['values'][0].split(','))
        farmable_info = await get_farmable_item_info(self.selected_item)
        size_x, size_y = farmable_info['space_required_x'], farmable_info['space_required_y']
        plots_to_update = [p for p in self.farm_data['farm_plots'] if pos_x <= p['pos_x'] < pos_x + size_x and pos_y <= p['pos_y'] < pos_y + size_y]
        now_iso = datetime.now(timezone.utc).isoformat()
        
        update_tasks = [update_plot(p['id'], {
            'state': 'planted', 
            'planted_item_name': self.selected_item, 
            'planted_at': now_iso, 
            'last_watered_at': None, 
            'growth_stage': 0, 
            'water_count': 0, 
            'quality': 5
        }) for p in plots_to_update]

        await asyncio.gather(*update_tasks)
        await update_inventory(str(self.user.id), self.selected_item, -1)
        farm_owner = await self.cog.get_farm_owner(interaction)
        await self.cog.update_farm_ui(interaction.channel, farm_owner)
        await interaction.followup.send(f"✅ 「{self.selected_item}」を植えました。", ephemeral=True, view=CloseButtonView(self.user))
        await interaction.delete_original_response()

    async def _build_uproot_select(self):
        plots_to_clear = [p for p in self.farm_data['farm_plots'] if p['state'] in ['planted', 'withered']]
        if not plots_to_clear:
            self.add_item(ui.Button(label="整理できる作物がありません。", disabled=True)); return
        processed_plots, options = set(), []
        for plot in sorted(plots_to_clear, key=lambda p: (p['pos_y'], p['pos_x'])):
            if plot['id'] in processed_plots: continue
            if plot['state'] == 'withered':
                label = f"🥀 枯れた作物 ({plot['pos_y']+1}行 {plot['pos_x']+1}列)"
                plot_ids_to_clear = [p_inner['id'] for p_inner in plots_to_clear if p_inner.get('planted_at') == plot.get('planted_at')]
                processed_plots.update(plot_ids_to_clear)
            else:
                item_name = plot['planted_item_name']
                farmable_info = await get_farmable_item_info(item_name)
                size_x, size_y = farmable_info['space_required_x'], farmable_info['space_required_y']
                plot_ids_to_clear = [p_inner['id'] for p_inner in plots_to_clear if plot['pos_x'] <= p_inner['pos_x'] < plot['pos_x'] + size_x and plot['pos_y'] <= p_inner['pos_y'] < plot['pos_y'] + size_y]
                processed_plots.update(plot_ids_to_clear)
                label = f"{item_name} ({plot['pos_y']+1}行 {plot['pos_x']+1}列)"
            value = ",".join(map(str, plot_ids_to_clear))
            options.append(discord.SelectOption(label=label, value=value))
        select = ui.Select(placeholder="撤去する作物/木を選択...", options=options, custom_id="uproot_select")
        select.callback = self.on_uproot_select
        self.add_item(select)

    async def on_uproot_select(self, interaction: discord.Interaction):
        plot_ids = list(map(int, interaction.data['values'][0].split(',')))
        confirm_view = ConfirmationView(self.user)
        await interaction.response.send_message("本当にこの作物を撤去しますか？\nこの操作は元に戻せません。", view=confirm_view, ephemeral=True)
        await confirm_view.wait()
        if confirm_view.value:
            await clear_plots_db(plot_ids)
            farm_owner = await self.cog.get_farm_owner(interaction)
            await self.cog.update_farm_ui(interaction.channel, farm_owner)
            await interaction.edit_original_response(content="✅ 作物を撤去しました。", view=None)
        else:
            await interaction.edit_original_response(content="キャンセルしました。", view=None)

    async def cancel_action(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await interaction.delete_original_response()

    async def refresh_view(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.build_components()
        embed = self.build_embed()
        await interaction.edit_original_response(embed=embed, view=self)

class FarmNameModal(ui.Modal, title="農場の新しい名前"):
    new_name = ui.TextInput(label="農場の名前を入力してください", placeholder="例: さわやかな農場", required=True, max_length=30)
    def __init__(self, cog_instance: 'Farm', farm_data: Dict):
        super().__init__(timeout=180)
        self.cog = cog_instance
        self.farm_data = farm_data
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        name_to_set = self.new_name.value.strip()
        if not name_to_set:
            await interaction.followup.send("❌ 名前は空にできません。", ephemeral=True); return
        await supabase.table('farms').update({'name': name_to_set}).eq('id', self.farm_data['id']).execute()
        try:
            if isinstance(interaction.channel, discord.Thread):
                await interaction.channel.edit(name=f"🌱｜{name_to_set}")
        except Exception as e:
            logger.error(f"농장 스레드 이름 변경 실패: {e}")
        farm_owner = await self.cog.get_farm_owner(interaction)
        await self.cog.update_farm_ui(interaction.channel, farm_owner)
        await interaction.followup.send(f"✅ 農場の名前を「{name_to_set}」に変更しました。", ephemeral=True, view=CloseButtonView(interaction.user))

class FarmUIView(ui.View):
    def __init__(self, cog_instance: 'Farm'):
        super().__init__(timeout=None)
        self.cog = cog_instance

        buttons = [
            ui.Button(label="畑を耕す", style=discord.ButtonStyle.secondary, emoji="🪓", row=0, custom_id="farm_till"),
            ui.Button(label="種を植える", style=discord.ButtonStyle.success, emoji="🌱", row=0, custom_id="farm_plant"),
            ui.Button(label="水をやる", style=discord.ButtonStyle.primary, emoji="💧", row=0, custom_id="farm_water"),
            ui.Button(label="収穫する", style=discord.ButtonStyle.success, emoji="🧺", row=0, custom_id="farm_harvest"),
            ui.Button(label="畑を整理する", style=discord.ButtonStyle.danger, emoji="🧹", row=0, custom_id="farm_uproot"),
            ui.Button(label="農場に招待", style=discord.ButtonStyle.grey, emoji="📢", row=1, custom_id="farm_invite"),
            ui.Button(label="権限を付与", style=discord.ButtonStyle.grey, emoji="🤝", row=1, custom_id="farm_share"),
            ui.Button(label="名前を変更", style=discord.ButtonStyle.grey, emoji="✏️", row=1, custom_id="farm_rename"),
        ]
        for item in buttons:
            callback_name = f"on_{item.custom_id}_click"
            if hasattr(self, callback_name):
                setattr(item, 'callback', getattr(self, callback_name))
            self.add_item(item)
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        farm_owner_id = await get_farm_owner_by_thread(interaction.channel.id)
        if not farm_owner_id:
            await interaction.response.send_message("❌ この農場の情報を見つけられませんでした。", ephemeral=True, view=CloseButtonView(interaction.user)); return False
        
        self.farm_owner = self.cog.bot.get_user(farm_owner_id)
        if not self.farm_owner:
            await interaction.response.send_message("❌ 農場の所有者情報を見つけられませんでした。", ephemeral=True, view=CloseButtonView(interaction.user)); return False
            
        self.farm_data = await get_farm_data(farm_owner_id)

        if interaction.user.id == self.farm_owner.id: return True
        
        custom_id = interaction.data['custom_id']
        if custom_id in ["farm_share", "farm_rename", "farm_invite"]:
            await interaction.response.send_message("❌ 農場の所有者のみ操作できます。", ephemeral=True, view=CloseButtonView(interaction.user)); return False

        action_map = {"farm_till": "till", "farm_plant": "plant", "farm_water": "water", "farm_harvest": "harvest", "farm_uproot": "plant"}
        action = action_map.get(custom_id)
        if not action: return False

        has_permission = await check_farm_permission(self.farm_data['id'], interaction.user.id, action)
        if not has_permission:
            await interaction.response.send_message("❌ この操作を行う権限がありません。", ephemeral=True, view=CloseButtonView(interaction.user))
        return has_permission

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: ui.Item) -> None:
        logger.error(f"FarmUIView에서 오류 발생 (item: {item.custom_id}): {error}", exc_info=True)
        msg = "❌ 処理中に予期せぬエラーが発生しました。"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True, view=CloseButtonView(interaction.user))
        else:
            await interaction.response.send_message(msg, ephemeral=True, view=CloseButtonView(interaction.user))

    async def on_farm_till_click(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        gear = await get_user_gear(interaction.user)
        equipped_hoe = gear.get('hoe', BARE_HANDS)
        if equipped_hoe == BARE_HANDS:
            await interaction.followup.send("❌ まずは商店で「クワ」を購入して、プロフィール画面から装備してください。", ephemeral=True, view=CloseButtonView(interaction.user)); return
        hoe_power = get_item_database().get(equipped_hoe, {}).get('power', 1)
        tilled_count, plots_to_update = 0, []
        for plot in self.farm_data.get('farm_plots', []):
            if plot['state'] == 'default' and tilled_count < hoe_power:
                plots_to_update.append(update_plot(plot['id'], {'state': 'tilled'})); tilled_count += 1
        if not plots_to_update:
            await interaction.followup.send("ℹ️ これ以上耕せる畑がありません。", ephemeral=True, view=CloseButtonView(interaction.user)); return
        await asyncio.gather(*plots_to_update)
        await interaction.followup.send(f"✅ **{equipped_hoe}** を使って、畑を**{tilled_count}マス**耕しました。", ephemeral=True, view=CloseButtonView(interaction.user))
        await self.cog.update_farm_ui(interaction.channel, self.farm_owner)

    async def on_farm_plant_click(self, interaction: discord.Interaction):
        action_view = FarmActionView(self.cog, self.farm_data, interaction.user, "plant_seed")
        await action_view.send_initial_message(interaction)

    async def on_farm_water_click(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        gear = await get_user_gear(interaction.user)
        equipped_wc = gear.get('watering_can', BARE_HANDS)
        if equipped_wc == BARE_HANDS:
            await interaction.followup.send("❌ まずは商店で「じょうろ」を購入して、装備してください。", ephemeral=True, view=CloseButtonView(interaction.user)); return
            
        item_info = get_item_database().get(equipped_wc, {})
        wc_power = item_info.get('power', 1)
        quality_bonus = item_info.get('quality_bonus', 5)
        
        watered_count, plots_to_update = 0, []
        now_utc = datetime.now(timezone.utc)
        today_jst_midnight = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)

        for plot in self.farm_data.get('farm_plots', []):
            if plot['state'] == 'planted' and watered_count < wc_power:
                can_water = False
                if plot.get('last_watered_at') is None:
                    can_water = True
                else:
                    last_watered_at_dt = datetime.fromisoformat(plot['last_watered_at'])
                    if last_watered_at_dt < today_jst_midnight:
                        can_water = True
                
                if can_water:
                    plots_to_update.append(update_plot(plot['id'], {'last_watered_at': now_utc.isoformat(), 'water_count': plot['water_count'] + 1, 'quality': plot['quality'] + quality_bonus}))
                    watered_count += 1

        if not plots_to_update:
            await interaction.followup.send("ℹ️ 今日はこれ以上水をやる必要のある作物がありません。", ephemeral=True, view=CloseButtonView(interaction.user)); return
            
        await asyncio.gather(*plots_to_update)
        await interaction.followup.send(f"✅ **{equipped_wc}** を使って、作物**{watered_count}個**に水をやりました。", ephemeral=True, view=CloseButtonView(interaction.user))
        await self.cog.update_farm_ui(interaction.channel, self.farm_owner)

    async def on_farm_harvest_click(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        harvested_items, plots_to_reset, trees_to_update, processed_plots_ids = {}, [], {}, set()
        farmable_info_map = await preload_farmable_info(self.farm_data)
        for plot in self.farm_data.get('farm_plots', []):
            if plot['id'] in processed_plots_ids or plot['growth_stage'] != 3: continue
            farmable_info = farmable_info_map.get(plot['planted_item_name'])
            if not farmable_info: continue
            sx, sy = farmable_info['space_required_x'], farmable_info['space_required_y']
            related_plots = [p for p in self.farm_data['farm_plots'] if plot['pos_x'] <= p['pos_x'] < plot['pos_x'] + sx and plot['pos_y'] <= p['pos_y'] < plot['pos_y'] + sy]
            plot_ids = [p['id'] for p in related_plots]; processed_plots_ids.update(plot_ids)
            quality_score = plot['quality']
            yield_multiplier = 1.0
            if quality_score > 20: yield_multiplier = 1.5
            elif quality_score > 10: yield_multiplier = 1.2
            elif quality_score < 0: yield_multiplier = 0.5
            base_yield = farmable_info.get('base_yield', 1)
            final_yield = max(1, int(base_yield * yield_multiplier))
            harvest_name = farmable_info['harvest_item_name']
            harvested_items[harvest_name] = harvested_items.get(harvest_name, 0) + final_yield
            if not farmable_info.get('is_tree'): 
                plots_to_reset.extend(plot_ids)
            else:
                regrowth_hours = farmable_info.get('regrowth_hours', 24)
                for pid in plot_ids:
                    trees_to_update[pid] = regrowth_hours

        if not harvested_items:
            await interaction.followup.send("ℹ️ 収穫できる作物がありません。", ephemeral=True, view=CloseButtonView(interaction.user)); return
        update_tasks = [update_inventory(str(self.farm_owner.id), name, qty) for name, qty in harvested_items.items()]
        if plots_to_reset: update_tasks.append(clear_plots_db(plots_to_reset))
        if trees_to_update:
            now = datetime.now(timezone.utc)
            update_tasks.extend([
                update_plot(pid, {
                    'growth_stage': 2, 
                    'planted_at': now.isoformat(), 
                    'water_count': 0, 
                    'last_watered_at': now.isoformat(), 
                    'quality': 5
                }) for pid in trees_to_update.keys()
            ])
        await asyncio.gather(*update_tasks)
        result_str = ", ".join([f"**{name}** {qty}個" for name, qty in harvested_items.items()])
        await interaction.followup.send(f"🎉 **{result_str}**を収穫しました！", ephemeral=True, view=CloseButtonView(interaction.user))
        await self.cog.update_farm_ui(interaction.channel, self.farm_owner)

    async def on_farm_uproot_click(self, interaction: discord.Interaction):
        action_view = FarmActionView(self.cog, self.farm_data, interaction.user, "uproot")
        await action_view.send_initial_message(interaction)
        
    async def on_farm_invite_click(self, interaction: discord.Interaction):
        view = ui.View()
        user_select = ui.UserSelect(placeholder="農場に招待するユーザーを選択...")
        async def callback(select_interaction: discord.Interaction):
            await select_interaction.response.defer(ephemeral=True)
            for user in user_select.values:
                try: await interaction.channel.add_user(user)
                except: pass
                await select_interaction.followup.send(f"✅ {user.mention}さんを農場に招待しました。", ephemeral=True, view=CloseButtonView(select_interaction.user))
            await interaction.edit_original_response(content="招待が完了しました。", view=None)
        user_select.callback = callback
        view.add_item(user_select)
        await interaction.response.send_message("誰を農場に招待しますか？", view=view, ephemeral=True)

    async def on_farm_share_click(self, interaction: discord.Interaction):
        view = ui.View()
        user_select = ui.UserSelect(placeholder="権限を付与するユーザーを選択...")
        async def callback(select_interaction: discord.Interaction):
            await select_interaction.response.defer(ephemeral=True)
            for user in user_select.values:
                await grant_farm_permission(self.farm_data['id'], user.id)
                await select_interaction.followup.send(f"✅ {user.mention}さんに農場の編集権限を付与しました。", ephemeral=True, view=CloseButtonView(select_interaction.user))
            await interaction.edit_original_response(content="権限設定が完了しました。", view=None)
        user_select.callback = callback
        view.add_item(user_select)
        await interaction.response.send_message("誰に農場の権限を付与しますか？", view=view, ephemeral=True)

    async def on_farm_rename_click(self, interaction: discord.Interaction):
        modal = FarmNameModal(self.cog, self.farm_data)
        await interaction.response.send_modal(modal)

class FarmCreationPanelView(ui.View):
    def __init__(self, cog_instance: 'Farm'):
        super().__init__(timeout=None)
        self.cog = cog_instance
        create_button = ui.Button(label="農場を作る", style=discord.ButtonStyle.success, emoji="🌱", custom_id="farm_create_button")
        create_button.callback = self.create_farm_callback
        self.add_item(create_button)
        
    async def create_farm_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user = interaction.user
        farm_data = await get_farm_data(user.id)
        panel_channel = interaction.channel
        if not isinstance(panel_channel, discord.TextChannel):
            await interaction.followup.send("❌ このコマンドはテキストチャンネルでのみ使用できます。", ephemeral=True); return
        if farm_data and farm_data.get('thread_id'):
            if thread := self.cog.bot.get_channel(farm_data['thread_id']):
                await interaction.followup.send(f"✅ あなたの農場はこちらです: {thread.mention}", ephemeral=True)
                try: await thread.add_user(user)
                except: pass
                await thread.send(f"ようこそ、{user.mention}さん！", delete_after=10)
            else: await self.cog.create_new_farm_thread(interaction, user)
        else: await self.cog.create_new_farm_thread(interaction, user)

class Farm(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.thread_locks: Dict[int, asyncio.Lock] = {}
        self.daily_crop_update.start()
        
    def cog_unload(self):
        self.daily_crop_update.cancel()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or not isinstance(message.channel, discord.Thread):
            return

        owner_id = await get_farm_owner_by_thread(message.channel.id)
        if not owner_id:
            return

        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden):
            pass

    async def register_persistent_views(self):
        self.bot.add_view(FarmCreationPanelView(self))
        self.bot.add_view(FarmUIView(self))
        logger.info("✅ 농장 관련 영구 View가 성공적으로 등록되었습니다.")
        
    @tasks.loop(time=JST_MIDNIGHT_UPDATE)
    async def daily_crop_update(self):
        logger.info("일일 작물 상태 업데이트 시작 (성장 및 시듦 판정)...")
        try:
            weather_key = get_config("current_weather", "sunny").strip('"')
            is_raining = WEATHER_TYPES.get(weather_key, {}).get('water_effect', False)
            
            # [✅ 성능 최적화] 페이지네이션을 위한 변수 초기화
            page_size = 1000
            offset = 0
            total_updated_plots = 0

            while True:
                # [✅ 성능 최적화] DB에서 데이터를 1000개씩 나누어 가져옵니다.
                response = await supabase.table('farm_plots').select('*').eq('state', 'planted').range(offset, offset + page_size - 1).execute()
                
                if not response or not response.data:
                    break # 더 이상 처리할 데이터가 없으면 루프 종료

                plots_to_update = []
                now_utc = datetime.now(timezone.utc)
                today_jst_midnight = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)
                farmable_info_map = await preload_farmable_info({'farm_plots': response.data})

                for plot in response.data:
                    farmable_info = farmable_info_map.get(plot['planted_item_name'])
                    if not farmable_info: continue

                    updates = {}
                    planted_at = datetime.fromisoformat(plot['planted_at'])
                    growth_days = farmable_info.get('growth_days', 999)

                    if plot['growth_stage'] < 3:
                        time_since_planting = now_utc - planted_at
                        if time_since_planting.days >= growth_days:
                            updates['growth_stage'] = 3

                    if updates.get('growth_stage') != 3 and plot['growth_stage'] < 3:
                        last_wet_time = datetime.fromisoformat(plot['last_watered_at']) if plot.get('last_watered_at') else planted_at
                        time_since_last_water = now_utc - last_wet_time
                        wither_threshold_days = growth_days / 2.0
                        
                        if time_since_last_water.days > wither_threshold_days:
                            updates['state'] = 'withered'

                    if is_raining and updates.get('state') != 'withered':
                        can_water_today = plot.get('last_watered_at') is None or datetime.fromisoformat(plot['last_watered_at']) < today_jst_midnight
                        if can_water_today:
                            updates['last_watered_at'] = now_utc.isoformat()
                            updates['water_count'] = plot['water_count'] + 1
                            updates['quality'] = plot['quality'] + 5
                    
                    if updates:
                        plots_to_update.append((plot['id'], updates))

                if plots_to_update:
                    update_tasks = [update_plot(pid, data) for pid, data in plots_to_update]
                    await asyncio.gather(*update_tasks)
                    total_updated_plots += len(plots_to_update)

                # [✅ 성능 최적화] 처리한 데이터가 페이지 크기보다 작으면, 마지막 페이지이므로 루프를 종료합니다.
                if len(response.data) < page_size:
                    break
                
                # [✅ 성능 최적화] 다음 페이지로 이동합니다.
                offset += page_size
                await asyncio.sleep(1) # DB 부하를 줄이기 위해 잠시 대기
            
            if total_updated_plots > 0:
                logger.info(f"총 {total_updated_plots}개의 밭의 상태를 업데이트했습니다.")
            else:
                logger.info("업데이트할 작물이 없습니다.")

        except Exception as e:
            logger.error(f"일일 작물 업데이트 중 오류: {e}", exc_info=True)
            
    @daily_crop_update.before_loop
    async def before_daily_crop_update(self):
        await self.bot.wait_until_ready()
        
    async def get_farm_owner(self, interaction: discord.Interaction) -> Optional[discord.User]:
        owner_id = await get_farm_owner_by_thread(interaction.channel.id)
        return self.bot.get_user(owner_id) if owner_id else None
        
    async def build_farm_embed(self, farm_data: Dict, user: discord.User) -> discord.Embed:
        farmable_info_map = await preload_farmable_info(farm_data)
        size_x, size_y = farm_data.get('size_x', 1), farm_data.get('size_y', 1)
        plots_map = {(p['pos_x'], p['pos_y']): p for p in farm_data.get('farm_plots', [])}
        
        grid = [['' for _ in range(size_x)] for _ in range(size_y)]
        info_lines = []
        processed_plots = set()
        
        now_utc = datetime.now(timezone.utc)
        today_jst_midnight = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)

        for y in range(size_y):
            for x in range(size_x):
                if (x, y) in processed_plots: continue
                plot = plots_map.get((x, y))
                
                plot_emoji = '🟤'
                if plot:
                    if plot['state'] == 'tilled': plot_emoji = '🟫'
                    elif plot['state'] == 'withered': plot_emoji = '🥀'
                    elif plot['state'] == 'planted':
                        stage = plot['growth_stage']
                        item_name = plot['planted_item_name']
                        farmable_info = farmable_info_map.get(item_name)
                        if farmable_info:
                            emoji_to_use = farmable_info.get('item_emoji')
                            if not emoji_to_use or stage < 3:
                                item_type = farmable_info.get('item_type', 'seed')
                                emoji_to_use = CROP_EMOJI_MAP.get(item_type, {}).get(stage, '🌱')
                            
                            sx, sy = farmable_info['space_required_x'], farmable_info['space_required_y']
                            for dy in range(sy):
                                for dx in range(sx):
                                    if y + dy < size_y and x + dx < size_x:
                                        grid[y+dy][x+dx] = emoji_to_use
                                        processed_plots.add((x + dx, y + dy))
                            plot_emoji = emoji_to_use
                        else:
                            plot_emoji = '❓'
                
                if not (x,y) in processed_plots:
                    grid[y][x] = plot_emoji

                if plot and plot['state'] == 'planted':
                    farmable_info = farmable_info_map.get(plot['planted_item_name'])
                    if not farmable_info: continue

                    watered_today_emoji = '➖'
                    if plot.get('last_watered_at'):
                        if datetime.fromisoformat(plot['last_watered_at']) >= today_jst_midnight:
                            watered_today_emoji = '💧'

                    info_text = f"{plot_emoji} **{plot['planted_item_name']}** (水: {watered_today_emoji}): "
                    
                    if plot['growth_stage'] < 3:
                        planted_at = datetime.fromisoformat(plot['planted_at'])
                        growth_days = farmable_info.get('growth_days', 3)
                        days_passed = (now_utc - planted_at).days
                        days_left = max(0, growth_days - days_passed)
                        info_text += f"収穫まであと約 {days_left}日"
                    
                    elif farmable_info.get('is_tree'):
                        regrowth_hours = farmable_info.get('regrowth_hours', 24)
                        last_harvest_time = datetime.fromisoformat(plot['planted_at'])
                        next_harvest_time = last_harvest_time + timedelta(hours=regrowth_hours)
                        time_left = next_harvest_time - now_utc
                        if time_left.total_seconds() > 0:
                            hours_left = int(time_left.total_seconds() // 3600)
                            info_text += f"次の実まであと約 {hours_left}時間"
                        else:
                            info_text += "実の収穫可能！ 🧺"
                    else:
                        info_text += "収穫可能！ 🧺"
                    
                    info_lines.append(info_text)

        farm_str = "\n".join("".join(row) for row in grid)
        farm_name = farm_data.get('name') or user.display_name
        embed = discord.Embed(title=f"**{farm_name}の農場**", color=0x8BC34A)
        
        description = f"```{farm_str}```"
        if info_lines:
            description += "\n" + "\n".join(info_lines)

        weather_key = get_config("current_weather", "sunny").strip('"')
        weather = WEATHER_TYPES.get(weather_key, {"emoji": "❔", "name": "不明"})
        description += f"\n\n**今日の天気:** {weather['emoji']} {weather['name']}"
        
        embed.description = description
        return embed
        
    async def update_farm_ui(self, thread: discord.Thread, user: discord.User):
        lock = self.thread_locks.setdefault(thread.id, asyncio.Lock())
        async with lock:
            if user is None:
                logger.warning(f"농장 UI 업데이트 시도 실패: 유저 정보를 찾을 수 없습니다 (스레드 ID: {thread.id})")
                return
            farm_data = await get_farm_data(user.id)
            if not farm_data or not farm_data.get("farm_message_id"): return
            try:
                if old_message_id := farm_data.get("farm_message_id"):
                    try:
                        old_message = await thread.fetch_message(old_message_id)
                        await old_message.delete()
                    except (discord.NotFound, discord.Forbidden):
                        pass

                embed = await self.build_farm_embed(farm_data, user)
                view = FarmUIView(self)
                
                new_message = await thread.send(embed=embed, view=view)
                await supabase.table('farms').update({'farm_message_id': new_message.id}).eq('id', farm_data['id']).execute()

            except (discord.NotFound, discord.Forbidden): pass
            except Exception as e:
                logger.error(f"농장 UI 업데이트 중 오류: {e}", exc_info=True)
                
    async def create_new_farm_thread(self, interaction: discord.Interaction, user: discord.Member):
        try:
            panel_channel = interaction.channel
            farm_name = f"{user.display_name}の農場"
            farm_thread = await panel_channel.create_thread(name=f"🌱｜{farm_name}", type=discord.ChannelType.private_thread)
            await farm_thread.send(f"ようこそ、{user.mention}さん！このスレッドの管理権限を設定しています…", delete_after=10)
            farm_data = await get_farm_data(user.id) or await create_farm(user.id)
            await supabase.table('farms').update({'thread_id': farm_thread.id, 'name': farm_name}).eq('user_id', user.id).execute()
            farm_data = await get_farm_data(user.id)
            if welcome_embed_data := await get_embed_from_db("farm_thread_welcome"):
                welcome_embed = format_embed_from_db(welcome_embed_data, user_name=farm_data.get('name') or user.display_name)
                await farm_thread.send(embed=welcome_embed)
            
            farm_embed = await self.build_farm_embed(farm_data, user)
            farm_view = FarmUIView(self)
            farm_message = await farm_thread.send(embed=farm_embed, view=farm_view)
            
            await supabase.table('farms').update({'farm_message_id': farm_message.id}).eq('id', farm_data['id']).execute()
            await farm_thread.add_user(user)
            await interaction.followup.send(f"✅ あなただけの農場を作成しました！ {farm_thread.mention} を確認してください。", ephemeral=True)
        except Exception as e:
            logger.error(f"농장 생성 중 오류 발생: {e}", exc_info=True)
            await interaction.followup.send("❌ 農場の作成中にエラーが発生しました。", ephemeral=True, view=CloseButtonView(interaction.user))

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_farm_creation", **kwargs):
        if panel_info := get_panel_id(panel_key):
            if (old_channel := self.bot.get_channel(panel_info['channel_id'])) and (old_message_id := panel_info.get('message_id')):
                try: await (await old_channel.fetch_message(old_message_id)).delete()
                except (discord.NotFound, discord.Forbidden): pass
        if not (embed_data := await get_embed_from_db(panel_key)): return
        embed = discord.Embed.from_dict(embed_data)
        view = FarmCreationPanelView(self)
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_key, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} パネルを正常に生成しました。(チャンネル: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(Farm(bot))
