# cogs/games/farm.py

import discord
from discord.ext import commands, tasks
from discord import ui
import logging
from typing import Optional, Dict, List, Any
import asyncio
import time
from datetime import datetime, timezone, timedelta, time as dt_time

from utils.database import (
    get_farm_data, create_farm, get_config,
    save_panel_id, get_panel_id, get_embed_from_db,
    supabase, get_inventory, get_user_gear, update_plot,
    get_farmable_item_info, update_inventory, BARE_HANDS,
    check_farm_permission, grant_farm_permission, clear_plots_db,
    get_farm_owner_by_thread, get_item_database, save_config_to_db
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

CROP_EMOJI_MAP = {
    'seed': {0: '🌱', 1: '🌿', 2: '🌾', 3: '🌾'},
    'sapling': {0: '🌱', 1: '🌳', 2: '🌳', 3: '🌳'}
}
WEATHER_TYPES = {
    "sunny": {"emoji": "☀️", "name": "晴れ", "water_effect": False},
    "cloudy": {"emoji": "☁️", "name": "曇り", "water_effect": False},
    "rainy": {"emoji": "🌧️", "name": "雨", "water_effect": True},
    "stormy": {"emoji": "⛈️", "name": "嵐", "water_effect": True},
}

JST = timezone(timedelta(hours=9))
JST_MIDNIGHT_UPDATE = dt_time(hour=0, minute=1, tzinfo=JST)


async def preload_farmable_info(farm_data: Dict) -> Dict[str, Dict]:
    item_names = {p['planted_item_name'] for p in farm_data.get('farm_plots', []) if p.get('planted_item_name')}
    if not item_names: return {}
    tasks = [get_farmable_item_info(name) for name in item_names]
    results = await asyncio.gather(*tasks)
    return {info['item_name']: info for info in results if info}

class ConfirmationView(ui.View):
    def __init__(self, user: discord.User):
        super().__init__(timeout=60)
        self.value = None; self.user = user
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("❌ 自分専用のメニューです。", ephemeral=True); return False
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
        self.cog, self.farm_data, self.user, self.action_type = parent_cog, farm_data, user, action_type
        self.selected_item: Optional[str] = None

    async def send_initial_message(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.build_components()
        embed = self.build_embed()
        await interaction.followup.send(embed=embed, view=self, ephemeral=True)

    def build_embed(self) -> discord.Embed:
        titles = {"plant_seed": "🌱 種を選択", "plant_location": "📍 場所を選択", "uproot": "❌ 作物を撤去"}
        descs = {"plant_seed": "インベントリから植えたい種または苗木を選択してください。", "plant_location": f"選択した「{self.selected_item}」を植える場所を選択してください。", "uproot": "撤去したい作物または木を選択してください。この操作は元に戻せません。"}
        return discord.Embed(title=titles.get(self.action_type, "エラー"), description=descs.get(self.action_type, "不明なアクションです。"), color=0x8BC34A)

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
        farmable_items = {n: q for n, q in inventory.items() if get_item_database().get(n, {}).get('category') == '農場_種'}
        if not farmable_items:
            self.add_item(ui.Button(label="植えられる種がありません。", disabled=True)); return
        options = [discord.SelectOption(label=f"{name} ({qty}個)", value=name) for name, qty in farmable_items.items()]
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
        sx, sy = farmable_info['space_required_x'], farmable_info['space_required_y']
        available_plots = self._find_available_space(sx, sy)
        if not available_plots:
            self.add_item(ui.Button(label=f"{sx}x{sy}の空き地がありません。", disabled=True)); return
        options = [discord.SelectOption(label=f"{p['pos_y']+1}行 {p['pos_x']+1}列", value=f"{p['pos_x']},{p['pos_y']}") for p in available_plots]
        select = ui.Select(placeholder="植える場所を選択...", options=options, custom_id="location_select")
        select.callback = self.on_location_select
        self.add_item(select)
    
    def _find_available_space(self, required_x: int, required_y: int) -> List[Dict]:
        size_x, size_y = self.farm_data['size_x'], self.farm_data['size_y']
        plots = {(p['pos_x'], p['pos_y']): p for p in self.farm_data['farm_plots']}
        valid_starts = []
        for y in range(size_y - required_y + 1):
            for x in range(size_x - required_x + 1):
                if all(plots.get((x + dx, y + dy), {}).get('state') == 'tilled' for dy in range(required_y) for dx in range(required_x)):
                    valid_starts.append(plots[(x, y)])
        return valid_starts

    async def on_location_select(self, interaction: discord.Interaction):
        await interaction.response.defer()
        x, y = map(int, interaction.data['values'][0].split(','))
        info = await get_farmable_item_info(self.selected_item)
        sx, sy = info['space_required_x'], info['space_required_y']
        plots = [p for p in self.farm_data['farm_plots'] if x <= p['pos_x'] < x + sx and y <= p['pos_y'] < y + sy]
        
        now = datetime.now(timezone.utc)
        
        # [✅ 수정] 파종 시 물주기 로직 변경
        weather_key = get_config("current_weather", "sunny")
        is_raining = WEATHER_TYPES.get(weather_key, {}).get('water_effect', False)

        updates = {
            'state': 'planted', 
            'planted_item_name': self.selected_item, 
            'planted_at': now.isoformat(), 
            'growth_stage': 0,
            'quality': 5 # 기본 품질
        }
        if is_raining:
            updates['last_watered_at'] = now.isoformat()
            updates['water_count'] = 1
            followup_message = f"✅ 「{self.selected_item}」を植えました。雨が降っていて、自動で水がまかれました！"
        else:
            updates['last_watered_at'] = None
            updates['water_count'] = 0
            followup_message = f"✅ 「{self.selected_item}」を植えました。忘れずに水をあげてください！"

        await asyncio.gather(
            *[update_plot(p['id'], updates) for p in plots], 
            update_inventory(str(self.user.id), self.selected_item, -1)
        )
        await self.cog.request_farm_ui_update(interaction.user.id)
        await interaction.followup.send(followup_message, ephemeral=True)
        await interaction.delete_original_response()

    async def _build_uproot_select(self):
        plots = [p for p in self.farm_data['farm_plots'] if p['state'] in ['planted', 'withered']]
        if not plots:
            self.add_item(ui.Button(label="整理できる作物がありません。", disabled=True)); return
        processed, options = set(), []
        for plot in sorted(plots, key=lambda p: (p['pos_y'], p['pos_x'])):
            if plot['id'] in processed: continue
            name = plot['planted_item_name'] or "枯れた作物"
            info = await get_farmable_item_info(name) if name != "枯れた作物" else {}
            sx, sy = info.get('space_required_x', 1), info.get('space_required_y', 1)
            related_ids = [p['id'] for p in plots if plot['pos_x'] <= p['pos_x'] < plot['pos_x'] + sx and plot['pos_y'] <= p['pos_y'] < plot['pos_y'] + sy]
            processed.update(related_ids)
            label = f"{'🥀' if plot['state'] == 'withered' else ''}{name} ({plot['pos_y']+1}行 {plot['pos_x']+1}列)"
            options.append(discord.SelectOption(label=label, value=",".join(map(str, related_ids))))
        select = ui.Select(placeholder="撤去する作物/木を選択...", options=options, custom_id="uproot_select")
        select.callback = self.on_uproot_select
        self.add_item(select)

    async def on_uproot_select(self, interaction: discord.Interaction):
        plot_ids = list(map(int, interaction.data['values'][0].split(',')))
        view = ConfirmationView(self.user)
        await interaction.response.send_message("本当にこの作物を撤去しますか？\nこの操作は元に戻せません。", view=view, ephemeral=True)
        await view.wait()
        if view.value:
            await clear_plots_db(plot_ids)
            await self.cog.request_farm_ui_update(self.user.id)
            await interaction.edit_original_response(content="✅ 作物を撤去しました。", view=None)
        else:
            await interaction.edit_original_response(content="キャンセルしました。", view=None)

    async def cancel_action(self, interaction: discord.Interaction):
        await interaction.response.defer(); await interaction.delete_original_response()

    async def refresh_view(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.build_components()
        await interaction.edit_original_response(embed=self.build_embed(), view=self)

class FarmNameModal(ui.Modal, title="農場の新しい名前"):
    new_name = ui.TextInput(label="農場の名前を入力してください", placeholder="例: さわやかな農場", required=True, max_length=30)
    def __init__(self, cog: 'Farm', farm_data: Dict):
        super().__init__(timeout=180)
        self.cog, self.farm_data = cog, farm_data
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        name = self.new_name.value.strip()
        if not name:
            await interaction.followup.send("❌ 名前は空にできません。", ephemeral=True); return
        await supabase.table('farms').update({'name': name}).eq('id', self.farm_data['id']).execute()
        if isinstance(interaction.channel, discord.Thread):
            try: await interaction.channel.edit(name=f"🌱｜{name}")
            except Exception as e: logger.error(f"농장 스레드 이름 변경 실패: {e}")
        await self.cog.request_farm_ui_update(interaction.user.id)
        await interaction.followup.send(f"✅ 農場の名前を「{name}」に変更しました。", ephemeral=True)

class FarmUIView(ui.View):
    def __init__(self, cog_instance: 'Farm'):
        super().__init__(timeout=None)
        self.cog = cog_instance
        buttons = [ui.Button(label="畑を耕す", emoji="🪓", row=0, custom_id="farm_till"), ui.Button(label="種を植える", emoji="🌱", row=0, custom_id="farm_plant"), ui.Button(label="水をやる", emoji="💧", row=0, custom_id="farm_water"), ui.Button(label="収穫する", emoji="🧺", row=0, custom_id="farm_harvest"), ui.Button(label="畑を整理する", emoji="🧹", row=0, custom_id="farm_uproot"), ui.Button(label="農場に招待", emoji="📢", row=1, custom_id="farm_invite"), ui.Button(label="権限を付与", emoji="🤝", row=1, custom_id="farm_share"), ui.Button(label="名前を変更", emoji="✏️", row=1, custom_id="farm_rename")]
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
            await interaction.response.send_message("❌ この農場の情報を見つけられませんでした。", ephemeral=True); return False
        self.farm_data = await get_farm_data(self.farm_owner_id)
        if not self.farm_data:
            await interaction.response.send_message("❌ 農場データの読み込みに失敗しました。", ephemeral=True); return False
        if interaction.user.id == self.farm_owner_id: return True
        if interaction.data['custom_id'] in ["farm_invite", "farm_share", "farm_rename"]:
            await interaction.response.send_message("❌ この操作は農場の所有者のみ可能です。", ephemeral=True); return False
        action_map = {"farm_till": "till", "farm_plant": "plant", "farm_water": "water", "farm_harvest": "harvest", "farm_uproot": "plant"}
        action = action_map.get(interaction.data['custom_id'])
        if not action: return False
        has_perm = await check_farm_permission(self.farm_data['id'], interaction.user.id, action)
        if not has_perm: await interaction.response.send_message("❌ この操作を行う権限がありません。", ephemeral=True)
        return has_perm

    async def on_error(self, i: discord.Interaction, e: Exception, item: ui.Item) -> None:
        logger.error(f"FarmUIView 오류 (item: {item.custom_id}): {e}", exc_info=True)
        msg = "❌ 処理中に予期せぬエラーが発生しました。"
        if i.response.is_done(): await i.followup.send(msg, ephemeral=True)
        else: await i.response.send_message(msg, ephemeral=True)

    async def on_farm_till_click(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        gear = await get_user_gear(interaction.user)
        hoe = gear.get('hoe', BARE_HANDS)
        if hoe == BARE_HANDS:
            await interaction.followup.send("❌ まずは商店で「クワ」を購入して、プロフィール画面から装備してください。", ephemeral=True); return
        power = get_item_database().get(hoe, {}).get('power', 1)
        tilled, plots = 0, [p for p in self.farm_data['farm_plots'] if p['state'] == 'default']
        tasks = []
        for plot in plots:
            if tilled < power:
                tasks.append(update_plot(plot['id'], {'state': 'tilled'})); tilled += 1
            else: break
        if not tasks:
            await interaction.followup.send("ℹ️ これ以上耕せる畑がありません。", ephemeral=True); return
        await asyncio.gather(*tasks)
        await interaction.followup.send(f"✅ **{hoe}** を使って、畑を**{tilled}マス**耕しました。", ephemeral=True)
        await self.cog.request_farm_ui_update(self.farm_owner_id)

    async def on_farm_plant_click(self, i: discord.Interaction): await FarmActionView(self.cog, self.farm_data, i.user, "plant_seed").send_initial_message(i)
    async def on_farm_uproot_click(self, i: discord.Interaction): await FarmActionView(self.cog, self.farm_data, i.user, "uproot").send_initial_message(i)

    async def on_farm_water_click(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        gear = await get_user_gear(interaction.user)
        wc = gear.get('watering_can', BARE_HANDS)
        if wc == BARE_HANDS:
            await interaction.followup.send("❌ まずは商店で「じょうろ」を購入して、装備してください。", ephemeral=True); return
        info = get_item_database().get(wc, {})
        power, bonus = info.get('power', 1), info.get('quality_bonus', 5)
        watered, tasks = 0, []
        now_utc, today_jst_midnight = datetime.now(timezone.utc), datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)
        for p in self.farm_data['farm_plots']:
            if p['state'] == 'planted' and watered < power:
                last_watered = datetime.fromisoformat(p['last_watered_at']) if p.get('last_watered_at') else datetime.fromtimestamp(0, tz=timezone.utc)
                if last_watered < today_jst_midnight:
                    tasks.append(update_plot(p['id'], {'last_watered_at': now_utc.isoformat(), 'water_count': p['water_count'] + 1, 'quality': p['quality'] + bonus}))
                    watered += 1
        if not tasks:
            await interaction.followup.send("ℹ️ 今日はこれ以上水をやる必要のある作物がありません。", ephemeral=True); return
        await asyncio.gather(*tasks)
        await interaction.followup.send(f"✅ **{wc}** を使って、作物**{watered}個**に水をやりました。", ephemeral=True)
        await self.cog.request_farm_ui_update(self.farm_owner_id)

    async def on_farm_harvest_click(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        harvested, plots_to_reset, trees_to_update, processed = {}, [], {}, set()
        info_map = await preload_farmable_info(self.farm_data)
        for p in self.farm_data['farm_plots']:
            if p['id'] in processed or p['growth_stage'] < info_map.get(p['planted_item_name'], {}).get('max_growth_stage', 3): continue
            info = info_map.get(p['planted_item_name'])
            if not info: continue
            sx, sy = info['space_required_x'], info['space_required_y']
            related = [plot for plot in self.farm_data['farm_plots'] if p['pos_x'] <= plot['pos_x'] < p['pos_x'] + sx and p['pos_y'] <= plot['pos_y'] < p['pos_y'] + sy]
            plot_ids = [plot['id'] for plot in related]; processed.update(plot_ids)
            quality = sum(plot['quality'] for plot in related) / len(related)
            yield_mult = 1.0 + (quality / 100.0)
            final_yield = max(1, round(info.get('base_yield', 1) * yield_mult))
            harvest_name = info['harvest_item_name']
            harvested[harvest_name] = harvested.get(harvest_name, 0) + final_yield
            if not info.get('is_tree'): plots_to_reset.extend(plot_ids)
            else:
                for pid in plot_ids: trees_to_update[pid] = info.get('regrowth_hours', 24)
        if not harvested:
            await interaction.followup.send("ℹ️ 収穫できる作物がありません。", ephemeral=True); return
        
        owner = self.cog.bot.get_user(self.farm_owner_id)
        if not owner: return
        tasks = [update_inventory(str(owner.id), n, q) for n, q in harvested.items()]
        if plots_to_reset: tasks.append(clear_plots_db(plots_to_reset))
        if trees_to_update:
            now_iso = datetime.now(timezone.utc).isoformat()
            tasks.extend([update_plot(pid, {'growth_stage': 2, 'planted_at': now_iso, 'last_watered_at': now_iso, 'quality': 5}) for pid in trees_to_update.keys()])
        
        xp_per_crop = get_config("GAME_CONFIG", {}).get("XP_FROM_FARMING", 15)
        total_xp = sum(harvested.values()) * xp_per_crop
        if total_xp > 0:
            res = await supabase.rpc('add_xp', {'p_user_id': owner.id, 'p_xp_to_add': total_xp, 'p_source': 'farming'}).execute()
            if res and res.data and (core_cog := self.cog.bot.get_cog("EconomyCore")):
                await core_cog.handle_level_up_event(owner, res.data[0])
        
        await asyncio.gather(*tasks)
        await interaction.followup.send(f"🎉 **{', '.join([f'{n} {q}個' for n, q in harvested.items()])}**を収穫しました！", ephemeral=True)
        await self.cog.request_farm_ui_update(self.farm_owner_id)

    async def on_farm_invite_click(self, i: discord.Interaction):
        view = ui.View(timeout=180)
        select = ui.UserSelect(placeholder="農場に招待するユーザーを選択...")
        async def cb(si: discord.Interaction):
            await si.response.defer(ephemeral=True)
            for user in select.values:
                try: await i.channel.add_user(user)
                except: pass
                await si.followup.send(f"✅ {user.mention}さんを農場に招待しました。", ephemeral=True)
            await i.edit_original_response(content="招待が完了しました。", view=None)
        select.callback = cb
        view.add_item(select)
        await i.response.send_message("誰を農場に招待しますか？", view=view, ephemeral=True)

    async def on_farm_share_click(self, i: discord.Interaction):
        view = ui.View(timeout=180)
        select = ui.UserSelect(placeholder="権限を付与するユーザーを選択...")
        async def cb(si: discord.Interaction):
            await si.response.defer(ephemeral=True)
            for user in select.values:
                await grant_farm_permission(self.farm_data['id'], user.id)
                await si.followup.send(f"✅ {user.mention}さんに農場の編集権限を付与しました。", ephemeral=True)
            await i.edit_original_response(content="権限設定が完了しました。", view=None)
        select.callback = cb
        view.add_item(select)
        await i.response.send_message("誰に農場の権限を付与しますか？", view=view, ephemeral=True)

    async def on_farm_rename_click(self, i: discord.Interaction): await i.response.send_modal(FarmNameModal(self.cog, self.farm_data))

class FarmCreationPanelView(ui.View):
    def __init__(self, cog: 'Farm'):
        super().__init__(timeout=None)
        self.cog = cog
        btn = ui.Button(label="農場を作る", style=discord.ButtonStyle.success, emoji="🌱", custom_id="farm_create_button")
        btn.callback = self.create_farm_callback
        self.add_item(btn)
        
    async def create_farm_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user = interaction.user
        farm_data = await get_farm_data(user.id)
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.followup.send("❌ このコマンドはテキストチャンネルでのみ使用できます。", ephemeral=True); return
        if farm_data and farm_data.get('thread_id'):
            if thread := self.cog.bot.get_channel(farm_data['thread_id']):
                await interaction.followup.send(f"✅ あなたの農場はこちらです: {thread.mention}", ephemeral=True)
                try: await thread.add_user(user)
                except: pass
            else: await self.cog.create_new_farm_thread(interaction, user, farm_data)
        else: await self.cog.create_new_farm_thread(interaction, user, farm_data)

class Farm(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.thread_locks: Dict[int, asyncio.Lock] = {}
        self.daily_crop_update.start()
        self.farm_ui_updater_task.start()
        
    def cog_unload(self):
        self.daily_crop_update.cancel()
        self.farm_ui_updater_task.cancel()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not isinstance(message.channel, discord.Thread): return
        if await get_farm_owner_by_thread(message.channel.id):
            try: await message.delete()
            except (discord.NotFound, discord.Forbidden): pass
            await message.channel.send(f"{message.author.mention}さん、農場での操作は下のボタンを使用してください。", delete_after=10)
            
    async def register_persistent_views(self):
        self.bot.add_view(FarmCreationPanelView(self))
        self.bot.add_view(FarmUIView(self))
        logger.info("✅ 농장 관련 영구 View가 성공적으로 등록되었습니다.")
        
    @tasks.loop(time=JST_MIDNIGHT_UPDATE)
    async def daily_crop_update(self):
        logger.info("일일 작물 상태 업데이트 시작...")
        try:
            weather_key = get_config("current_weather", "sunny")
            is_raining = WEATHER_TYPES.get(weather_key, {}).get('water_effect', False)
            response = await supabase.rpc('process_daily_farm_update', {'is_raining': is_raining}).execute()
            
            if response.data and response.data > 0:
                logger.info(f"일일 작물 업데이트 완료. {response.data}개의 밭이 영향을 받았습니다. UI 업데이트를 요청합니다.")
                farms_res = await supabase.table('farms').select('user_id').execute()
                if farms_res.data:
                    for farm in farms_res.data:
                        await self.request_farm_ui_update(farm['user_id'])
            else: 
                logger.info("업데이트할 작물이 없습니다.")
        except Exception as e:
            logger.error(f"일일 작물 업데이트 중 오류: {e}", exc_info=True)
            
    @daily_crop_update.before_loop
    async def before_daily_crop_update(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=5.0)
    async def farm_ui_updater_task(self):
        try:
            response = await supabase.table('bot_configs').select('config_key').like('config_key', 'farm_ui_update_request_%').execute()
            if not response.data: return
            
            keys_to_delete = [req['config_key'] for req in response.data]
            user_ids = {int(key.split('_')[-1]) for key in keys_to_delete}

            tasks = []
            for user_id in user_ids:
                farm_data = await get_farm_data(user_id)
                if farm_data and farm_data.get('thread_id'):
                    if (thread := self.bot.get_channel(farm_data['thread_id'])) and (user := self.bot.get_user(user_id)):
                        tasks.append(self.update_farm_ui(thread, user, farm_data))
            
            await asyncio.gather(*tasks)

            if keys_to_delete:
                await supabase.table('bot_configs').delete().in_('config_key', keys_to_delete).execute()

        except Exception as e:
            logger.error(f"농장 UI 업데이트 루프 중 오류: {e}", exc_info=True)

    @farm_ui_updater_task.before_loop
    async def before_farm_ui_updater_task(self):
        await self.bot.wait_until_ready()

    async def request_farm_ui_update(self, user_id: int):
        await save_config_to_db(f"farm_ui_update_request_{user_id}", time.time())
        
    async def get_farm_owner(self, interaction: discord.Interaction) -> Optional[discord.User]:
        owner_id = await get_farm_owner_by_thread(interaction.channel.id)
        return self.bot.get_user(owner_id) if owner_id else None
        
    async def build_farm_embed(self, farm_data: Dict, user: discord.User) -> discord.Embed:
        info_map = await preload_farmable_info(farm_data)
        sx, sy = farm_data.get('size_x', 5), farm_data.get('size_y', 5)
        plots = {(p['pos_x'], p['pos_y']): p for p in farm_data['farm_plots']}
        grid, infos, processed = [['' for _ in range(sx)] for _ in range(sy)], [], set()
        today_jst_midnight = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)

        for y in range(sy):
            for x in range(sx):
                if (x, y) in processed: continue
                plot = plots.get((x, y))
                emoji = '🟤'
                if plot:
                    state = plot['state']
                    if state == 'tilled': emoji = '🟫'
                    elif state == 'withered': emoji = '🥀'
                    elif state == 'planted':
                        name = plot['planted_item_name']
                        info = info_map.get(name)
                        if info:
                            stage = plot['growth_stage']
                            max_stage = info.get('max_growth_stage', 3)
                            emoji = info.get('item_emoji') if stage >= max_stage else CROP_EMOJI_MAP.get(info.get('item_type', 'seed'), {}).get(stage, '🌱')
                            item_sx, item_sy = info['space_required_x'], info['space_required_y']
                            for dy in range(item_sy):
                                for dx in range(item_sx):
                                    if y + dy < sy and x + dx < sx:
                                        grid[y+dy][x+dx] = emoji; processed.add((x + dx, y + dy))
                            
                            last_watered = datetime.fromisoformat(plot['last_watered_at']) if plot.get('last_watered_at') else datetime.fromtimestamp(0, tz=timezone.utc)
                            water_emoji = '💧' if last_watered >= today_jst_midnight else '➖'
                            
                            info_text = f"{emoji} **{name}** (水: {water_emoji}): "
                            if stage >= max_stage: info_text += "収穫可能！ 🧺"
                            else: info_text += f"成長 {stage+1}/{max_stage+1}段階目"
                            infos.append(info_text)
                if not (x,y) in processed: grid[y][x] = emoji

        farm_str = "\n".join("".join(row) for row in grid)
        farm_name = farm_data.get('name') or user.display_name
        embed = discord.Embed(title=f"**{farm_name}さんの農場**", color=0x8BC34A, description=f"```{farm_str}```")
        if infos: embed.description += "\n" + "\n".join(sorted(infos))
        
        weather_key = get_config("current_weather", "sunny")
        weather = WEATHER_TYPES.get(weather_key, {"emoji": "❔", "name": "不明"})
        embed.description += f"\n\n**今日の天気:** {weather['emoji']} {weather['name']}"
        return embed
        
    async def update_farm_ui(self, thread: discord.Thread, user: discord.User, farm_data: Dict):
        lock = self.thread_locks.setdefault(thread.id, asyncio.Lock())
        async with lock:
            if not (user and farm_data): return

            try:
                embed = await self.build_farm_embed(farm_data, user)
                view = FarmUIView(self)
                
                if message_id := farm_data.get("farm_message_id"):
                    try:
                        message = await thread.fetch_message(message_id)
                        await message.edit(embed=embed, view=view)
                        return
                    except (discord.NotFound, discord.Forbidden):
                        logger.warning(f"농장 메시지(ID: {message_id})를 찾지 못하여 새로 생성합니다.")
                
                # 메시지를 찾지 못했거나 원래 없었으면 새로 생성
                new_message = await thread.send(embed=embed, view=view)
                await supabase.table('farms').update({'farm_message_id': new_message.id}).eq('id', farm_data['id']).execute()

            except Exception as e:
                logger.error(f"농장 UI 업데이트 중 오류: {e}", exc_info=True)
                
    async def create_new_farm_thread(self, interaction: discord.Interaction, user: discord.Member, farm_data: Optional[Dict] = None):
        try:
            farm_name = f"{user.display_name}の農場"
            thread = await interaction.channel.create_thread(name=f"🌱｜{farm_name}", type=discord.ChannelType.private_thread)
            farm_data = farm_data or await create_farm(user.id)
            if not farm_data:
                await interaction.followup.send("❌ 農場の初期化に失敗しました。", ephemeral=True); return
            
            await supabase.table('farms').update({'thread_id': thread.id, 'name': farm_name}).eq('user_id', user.id).execute()
            updated_data = await get_farm_data(user.id)
            
            if embed_data := await get_embed_from_db("farm_thread_welcome"):
                await thread.send(embed=format_embed_from_db(embed_data, user_name=updated_data.get('name') or user.display_name))
            
            await self.update_farm_ui(thread, user, updated_data)
            await thread.add_user(user)
            await interaction.followup.send(f"✅ あなただけの農場を作成しました！ {thread.mention} を確認してください。", ephemeral=True)
        except Exception as e:
            logger.error(f"농장 생성 중 오류 발생: {e}", exc_info=True)
            await interaction.followup.send("❌ 農場の作成中にエラーが発生しました。", ephemeral=True)

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_farm_creation", **kwargs):
        if panel_info := get_panel_id(panel_key):
            if (old_channel := self.bot.get_channel(panel_info['channel_id'])) and (old_message_id := panel_info.get('message_id')):
                try: await (await old_channel.fetch_message(old_message_id)).delete()
                except (discord.NotFound, discord.Forbidden): pass
        if not (embed_data := await get_embed_from_db(panel_key)): return
        new_message = await channel.send(embed=discord.Embed.from_dict(embed_data), view=FarmCreationPanelView(self))
        await save_panel_id(panel_key, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} パネルを正常に生成しました。(チャンネル: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(Farm(bot))
