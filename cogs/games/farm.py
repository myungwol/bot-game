import discord
from discord.ext import commands, tasks
from discord import ui
import logging
from typing import Optional, Dict, List, Any
import asyncio
from datetime import datetime, timezone, timedelta

from utils.database import (
    get_farm_data, create_farm, get_config,
    save_panel_id, get_panel_id, get_embed_from_db,
    supabase, get_inventory, get_user_gear, update_plot,
    get_farmable_item_info, update_inventory, BARE_HANDS,
    check_farm_permission, grant_farm_permission, clear_plots_db
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

# [✅ NEW] 성장 단계에 따른 이모지 매핑
CROP_EMOJI_MAP = {
    'vegetable': {0: '🫘', 1: '🌱', 2: '🌾', 3: '🥕'},
    'fruit_tree': {0: '🫘', 1: '🌱', 2: '🌳', 3: '🍎'}
}
# [✅ NEW] 작물 최종 수확물 이모지 (DB에 없다면 사용)
HARVEST_EMOJI_MAP = {
    "ニンジン": "🥕", "ジャガイモ": "🥔", "イチゴ": "🍓",
    "リンゴ": "🍎", "モモ": "🍑", "オレンジ": "🍊"
}

# --- [✅ NEW] 농장 상호작용 관련 UI 클래스 ---

class ConfirmationView(ui.View):
    def __init__(self, user: discord.User):
        super().__init__(timeout=60)
        self.value = None
        self.user = user

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user.id

    @ui.button(label="はい", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        self.value = True
        await interaction.response.defer()
        self.stop()

    @ui.button(label="いいえ", style=discord.ButtonStyle.grey)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        self.value = False
        await interaction.response.defer()
        self.stop()

class FarmActionView(ui.View):
    """씨앗 심기, 위치 선택, 작물 뽑기 등 다단계 상호작용을 처리하는 View"""
    def __init__(self, parent_cog: 'Farm', farm_data: Dict, user: discord.User, action_type: str):
        super().__init__(timeout=180)
        self.cog = parent_cog
        self.farm_data = farm_data
        self.user = user
        self.action_type = action_type
        self.selected_seed: Optional[str] = None

    async def send_initial_message(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.build_components()
        embed = self.build_embed()
        await interaction.followup.send(embed=embed, view=self, ephemeral=True)

    def build_embed(self) -> discord.Embed:
        if self.action_type == "plant_seed":
            title = "🌱 種を選択"
            desc = "インベントリから植えたい種または苗木を選択してください。"
        elif self.action_type == "plant_location":
            title = "📍 場所を選択"
            desc = f"選択した「{self.selected_seed}」を植える場所を選択してください。"
        elif self.action_type == "uproot":
            title = "❌ 作物を撤去"
            desc = "撤去したい作物または木を選択してください。この操作は元に戻せません。"
        else:
            title = "エラー"
            desc = "不明なアクションです。"
        return discord.Embed(title=title, description=desc, color=0x8BC34A)

    async def build_components(self):
        self.clear_items()
        if self.action_type == "plant_seed":
            await self._build_seed_select()
        elif self.action_type == "plant_location":
            await self._build_location_select()
        elif self.action_type == "uproot":
            await self._build_uproot_select()

        back_button = ui.Button(label="農場に戻る", style=discord.ButtonStyle.grey, row=4)
        back_button.callback = self.cancel_action
        self.add_item(back_button)

    async def _build_seed_select(self):
        inventory = await get_inventory(str(self.user.id))
        farmable_items_in_inv = {name: qty for name, qty in inventory.items() if "種" in name or "苗木" in name}
        
        if not farmable_items_in_inv:
            self.add_item(ui.Button(label="植えられる種がありません。", disabled=True))
            return

        options = [
            discord.SelectOption(label=f"{name} ({qty}個)", value=name)
            for name, qty in farmable_items_in_inv.items()
        ]
        select = ui.Select(placeholder="種/苗木を選択...", options=options, custom_id="seed_select")
        select.callback = self.on_seed_select
        self.add_item(select)

    async def on_seed_select(self, interaction: discord.Interaction):
        self.selected_seed = interaction.data['values'][0]
        self.action_type = "plant_location"
        await self.refresh_view(interaction)
    
    async def _build_location_select(self):
        farmable_info = await get_farmable_item_info(self.selected_seed)
        if not farmable_info: return

        size_x, size_y = map(int, farmable_info['space_required'].split('x'))
        available_plots = self._find_available_space(size_x, size_y)

        if not available_plots:
            self.add_item(ui.Button(label=f"{size_x}x{size_y}の空き地がありません。", disabled=True))
            return
        
        options = [
            discord.SelectOption(label=f"{plot['pos_y']+1}行 {plot['pos_x']+1}列", value=f"{plot['pos_x']},{plot['pos_y']}")
            for plot in available_plots
        ]
        select = ui.Select(placeholder="植える場所を選択...", options=options, custom_id="location_select")
        select.callback = self.on_location_select
        self.add_item(select)
    
    def _find_available_space(self, required_x: int, required_y: int) -> List[Dict]:
        farm_size_x, farm_size_y = self.farm_data['size_x'], self.farm_data['size_y']
        plots = { (p['pos_x'], p['pos_y']): p for p in self.farm_data['farm_plots'] }
        valid_top_lefts = []
        for y in range(farm_size_y - required_y + 1):
            for x in range(farm_size_x - required_x + 1):
                is_space_free = True
                for dy in range(required_y):
                    for dx in range(required_x):
                        plot = plots.get((x + dx, y + dy))
                        if not plot or plot['state'] != 'tilled':
                            is_space_free = False; break
                    if not is_space_free: break
                if is_space_free:
                    valid_top_lefts.append(plots[(x,y)])
        return valid_top_lefts

    async def on_location_select(self, interaction: discord.Interaction):
        await interaction.response.defer()
        pos_x, pos_y = map(int, interaction.data['values'][0].split(','))
        farmable_info = await get_farmable_item_info(self.selected_seed)
        size_x, size_y = map(int, farmable_info['space_required'].split('x'))

        plots_to_update = []
        for dy in range(size_y):
            for dx in range(size_x):
                for p in self.farm_data['farm_plots']:
                    if p['pos_x'] == pos_x + dx and p['pos_y'] == pos_y + dy:
                        plots_to_update.append(p)
                        break
        
        update_tasks = [
            update_plot(p['id'], {
                'state': 'planted', 
                'planted_item_name': self.selected_seed, 
                'planted_at': datetime.now(timezone.utc).isoformat(),
                'growth_stage': 0, 'water_count': 0, 'last_watered_at': None
            }) for p in plots_to_update
        ]
        
        await asyncio.gather(*update_tasks)
        await update_inventory(str(self.user.id), self.selected_seed, -1)
        
        await self.cog.update_farm_ui(interaction.channel, self.user, interaction)
        await interaction.followup.send(f"✅ 「{self.selected_seed}」を植えました。", ephemeral=True, delete_after=5)
        await interaction.delete_original_response()

    async def _build_uproot_select(self):
        planted_plots = [p for p in self.farm_data['farm_plots'] if p['state'] == 'planted']
        
        if not planted_plots:
            self.add_item(ui.Button(label="植えられた作物がありません。", disabled=True))
            return
            
        processed_plots = set()
        options = []
        for plot in sorted(planted_plots, key=lambda p: (p['pos_y'], p['pos_x'])):
            if plot['id'] in processed_plots:
                continue

            item_name = plot['planted_item_name']
            farmable_info = await get_farmable_item_info(item_name)
            size_x, size_y = map(int, farmable_info['space_required'].split('x'))
            
            plot_ids_to_clear = []
            for dy in range(size_y):
                for dx in range(size_x):
                    for p_inner in planted_plots:
                        if p_inner['pos_x'] == plot['pos_x'] + dx and p_inner['pos_y'] == plot['pos_y'] + dy:
                            plot_ids_to_clear.append(p_inner['id'])
                            processed_plots.add(p_inner['id'])
            
            label = f"{item_name} ({plot['pos_y']+1}行 {plot['pos_x']+1}列)"
            value = ",".join(map(str, plot_ids_to_clear))
            options.append(discord.SelectOption(label=label, value=value))

        select = ui.Select(placeholder="撤去する作物/木を選択...", options=options, custom_id="uproot_select")
        select.callback = self.on_uproot_select
        self.add_item(select)

    async def on_uproot_select(self, interaction: discord.Interaction):
        plot_ids_str = interaction.data['values'][0]
        plot_ids = list(map(int, plot_ids_str.split(',')))
        
        confirm_view = ConfirmationView(self.user)
        msg = await interaction.response.send_message("本当にこの作物を撤去しますか？\nこの操作は元に戻せません。", view=confirm_view, ephemeral=True)
        await confirm_view.wait()
        
        if confirm_view.value:
            await clear_plots_db(plot_ids)
            await self.cog.update_farm_ui(interaction.channel, self.user, interaction)
            await interaction.edit_original_response(content="✅ 作物を撤去しました。", view=None)
            await asyncio.sleep(5)
            await interaction.delete_original_response()
        else:
            await interaction.edit_original_response(content="キャンセルしました。", view=None)
            await asyncio.sleep(5)
            await interaction.delete_original_response()

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
            await interaction.followup.send("❌ 名前は空にできません。", ephemeral=True)
            return
        await supabase.table('farms').update({'name': name_to_set}).eq('id', self.farm_data['id']).execute()
        try:
            if isinstance(interaction.channel, discord.Thread):
                await interaction.channel.edit(name=f"🌱｜{name_to_set}")
        except Exception as e:
            logger.error(f"농장 스레드 이름 변경 실패: {e}")
        await self.cog.update_farm_ui(interaction.channel, interaction.user, interaction)
        await interaction.followup.send(f"✅ 農場の名前を「{name_to_set}」に変更しました。", ephemeral=True, delete_after=10)

class FarmUIView(ui.View):
    def __init__(self, cog_instance: 'Farm', farm_data: Dict, farm_owner: discord.User):
        super().__init__(timeout=None)
        self.cog = cog_instance
        self.farm_data = farm_data
        self.farm_owner = farm_owner
        
        self.add_item(ui.Button(label="畑を耕す", style=discord.ButtonStyle.secondary, emoji="🪓", row=0, custom_id="farm_till"))
        self.add_item(ui.Button(label="種を植える", style=discord.ButtonStyle.success, emoji="🌱", row=0, custom_id="farm_plant"))
        self.add_item(ui.Button(label="水をやる", style=discord.ButtonStyle.primary, emoji="💧", row=0, custom_id="farm_water"))
        self.add_item(ui.Button(label="収穫する", style=discord.ButtonStyle.success, emoji="🧺", row=0, custom_id="farm_harvest"))
        self.add_item(ui.Button(label="畑を整理する", style=discord.ButtonStyle.danger, emoji="🧹", row=0, custom_id="farm_uproot"))
        self.add_item(ui.Button(label="農場に招待", style=discord.ButtonStyle.grey, emoji="📢", row=1, custom_id="farm_invite"))
        self.add_item(ui.Button(label="権限を付与", style=discord.ButtonStyle.grey, emoji="🤝", row=1, custom_id="farm_share"))
        self.add_item(ui.Button(label="名前を変更", style=discord.ButtonStyle.grey, emoji="✏️", row=1, custom_id="farm_rename"))
        
        # 각 버튼에 콜백을 동적으로 할당
        for item in self.children:
            if isinstance(item, ui.Button):
                callback_name = f"handle_{item.custom_id}"
                if hasattr(self.cog, callback_name):
                    setattr(item, 'callback', getattr(self.cog, callback_name))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        is_owner = interaction.user.id == self.farm_owner.id
        if is_owner:
            return True
        
        # 권한 부여 버튼과 이름 변경 버튼은 소유자만 가능
        if interaction.data['custom_id'] in ["farm_share", "farm_rename", "farm_invite"]:
            await interaction.response.send_message("❌ 農場の所有者のみ操作できます。", ephemeral=True, delete_after=5)
            return False

        has_permission = await check_farm_permission(self.farm_data['id'], interaction.user.id)
        if not has_permission:
            await interaction.response.send_message("❌ 農場の所有者または権限を付与された人のみ操作できます。", ephemeral=True, delete_after=5)
        return has_permission

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: ui.Item) -> None:
        logger.error(f"FarmUIView에서 오류 발생 (item: {item.custom_id}): {error}", exc_info=True)
        msg = "❌ 処理中に予期せぬエラーが発生しました。"
        if interaction.response.is_done(): await interaction.followup.send(msg, ephemeral=True, delete_after=10)
        else: await interaction.response.send_message(msg, ephemeral=True, delete_after=10)

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

        if farm_data:
            farm_thread_id = farm_data.get('thread_id')
            if farm_thread_id and (thread := self.cog.bot.get_channel(farm_thread_id)):
                await interaction.followup.send(f"✅ あなたの農場はこちらです: {thread.mention}", ephemeral=True)
                try:
                    await thread.send(f"{user.mention}さんが農場にやってきました！", delete_after=10)
                    await thread.add_user(user)
                except discord.Forbidden:
                    await thread.send(f"ようこそ、{user.mention}さん！", delete_after=10)
            else: await self.cog.create_new_farm_thread(interaction, user)
        else: await self.cog.create_new_farm_thread(interaction, user)

class Farm(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.thread_locks: Dict[int, asyncio.Lock] = {}
        self.crop_growth_check.start()

    def cog_unload(self):
        self.crop_growth_check.cancel()

    @tasks.loop(hours=1)
    async def crop_growth_check(self):
        logger.info("작물 성장 상태 자동 업데이트 시작...")
        try:
            response = await supabase.table('farm_plots').select('*').eq('state', 'planted').execute()
            if not response.data: return
            
            plots_to_update = []
            for plot in response.data:
                farmable_info = await get_farmable_item_info(plot['planted_item_name'])
                if not farmable_info: continue

                planted_time = datetime.fromisoformat(plot['planted_at'])
                time_since_planted = datetime.now(timezone.utc) - planted_time
                
                growth_days = farmable_info['growth_time_days']
                current_stage = plot['growth_stage']
                
                new_stage = current_stage
                if farmable_info['crop_type'] == 'vegetable':
                    if time_since_planted > timedelta(days=growth_days) and current_stage < 3: new_stage = 3
                    elif time_since_planted > timedelta(days=growth_days * 0.66) and current_stage < 2: new_stage = 2
                    elif time_since_planted > timedelta(days=growth_days * 0.33) and current_stage < 1: new_stage = 1
                elif farmable_info['crop_type'] == 'fruit_tree':
                    if current_stage < 2: # 나무 성장 단계
                        if time_since_planted > timedelta(days=growth_days) and current_stage < 2: new_stage = 2
                        elif time_since_planted > timedelta(days=growth_days * 0.5) and current_stage < 1: new_stage = 1
                    elif current_stage == 2: # 열매가 열리는 단계
                        if time_since_planted > timedelta(days=growth_days + farmable_info['regrowth_time_days']): new_stage = 3
                
                if new_stage != current_stage:
                    plots_to_update.append(update_plot(plot['id'], {'growth_stage': new_stage}))

            if plots_to_update:
                await asyncio.gather(*plots_to_update)
                logger.info(f"{len(plots_to_update)}개의 밭의 성장 단계를 업데이트했습니다.")

        except Exception as e:
            logger.error(f"작물 성장 체크 중 오류: {e}", exc_info=True)

    @crop_growth_check.before_loop
    async def before_crop_growth_check(self):
        await self.bot.wait_until_ready()

    def build_farm_embed(self, farm_data: Dict, user: discord.User) -> discord.Embed:
        size_x, size_y = farm_data.get('size_x', 1), farm_data.get('size_y', 1)
        plots_map = {(p['pos_x'], p['pos_y']): p for p in farm_data.get('farm_plots', [])}
        grid = [['' for _ in range(size_x)] for _ in range(size_y)]
        
        for y in range(size_y):
            for x in range(size_x):
                if grid[y][x] != '': continue
                plot = plots_map.get((x, y))
                if not plot: grid[y][x] = '❓'; continue
                
                if plot['state'] == 'default': grid[y][x] = '🟤'
                elif plot['state'] == 'tilled': grid[y][x] = '🟫'
                elif plot['state'] == 'planted':
                    item_name = plot['planted_item_name']
                    # 비동기 함수를 동기 함수 내에서 직접 호출할 수 없으므로, 이 부분은 단순화하거나 다른 접근이 필요.
                    # 우선 캐시된 정보나 기본값을 사용하도록 처리.
                    crop_type = 'fruit_tree' if '苗木' in item_name else 'vegetable'
                    stage = plot['growth_stage']
                    
                    if stage == 3: # 수확 가능
                        final_emoji = HARVEST_EMOJI_MAP.get(item_name.replace("の種", "").replace("の苗木", ""), '🌟')
                        emoji_to_use = final_emoji
                    else:
                        emoji_to_use = CROP_EMOJI_MAP.get(crop_type, {}).get(stage, '❓')
                    
                    if crop_type == 'fruit_tree' and "x" in "2x2":
                        for dy in range(2):
                            for dx in range(2):
                                if y + dy < size_y and x + dx < size_x:
                                    grid[y+dy][x+dx] = emoji_to_use
                    else:
                        grid[y][x] = emoji_to_use

        farm_str = "```\n" + "\n".join(" ".join(row) for row in grid) + "\n```"
        farm_name = farm_data.get('name') or user.display_name
        embed = discord.Embed(title=f"**{farm_name}の農場**", color=0x8BC34A)
        embed.description = "> 畑を耕し、作物を育てましょう！"
        embed.add_field(name="**━━━━━━━━[ 農場の様子 ]━━━━━━━━**", value=farm_str, inline=False)
        return embed

    async def update_farm_ui(self, thread: discord.Thread, user: discord.User, interaction: Optional[discord.Interaction] = None):
        lock = self.thread_locks.setdefault(thread.id, asyncio.Lock())
        async with lock:
            farm_data = await get_farm_data(user.id)
            if not farm_data or not farm_data.get("farm_message_id"):
                if interaction: await interaction.followup.send("❌ 農場UIメッセージが見つかりません。", ephemeral=True, delete_after=5)
                return
            try:
                farm_message = await thread.fetch_message(farm_data["farm_message_id"])
                embed = self.build_farm_embed(farm_data, user)
                view = FarmUIView(self, farm_data, user)
                await farm_message.edit(embed=embed, view=view)
            except (discord.NotFound, discord.Forbidden):
                if interaction and not interaction.response.is_done():
                    await interaction.followup.send("❌ 農場UIメッセージの更新に失敗しました。", ephemeral=True, delete_after=5)
            except Exception as e:
                logger.error(f"농장 UI 업데이트 중 오류: {e}", exc_info=True)
                if interaction and not interaction.response.is_done():
                    await interaction.followup.send("❌ エラーが発生しました。", ephemeral=True, delete_after=5)

    async def register_persistent_views(self):
        self.bot.add_view(FarmCreationPanelView(self))

    async def create_new_farm_thread(self, interaction: discord.Interaction, user: discord.Member):
        try:
            panel_channel = interaction.channel
            farm_name = f"{user.display_name}の農場"
            farm_thread = await panel_channel.create_thread(name=f"🌱｜{farm_name}", type=discord.ChannelType.private_thread)
            await farm_thread.send(f"ようこそ、{user.mention}さん！このスレッドの管理権限を設定しています…", delete_after=10)
            farm_data = await get_farm_data(user.id) or await create_farm(user.id)
            await supabase.table('farms').update({'thread_id': farm_thread.id}).eq('user_id', user.id).execute()
            farm_data = await get_farm_data(user.id)
            if welcome_embed_data := await get_embed_from_db("farm_thread_welcome"):
                final_farm_name = farm_data.get('name') or user.display_name
                welcome_embed = format_embed_from_db(welcome_embed_data, user_name=final_farm_name)
                await farm_thread.send(embed=welcome_embed)
            
            farm_embed = self.build_farm_embed(farm_data, user)
            farm_view = FarmUIView(self, farm_data, user)
            farm_message = await farm_thread.send(embed=farm_embed, view=farm_view)
            
            await supabase.table('farms').update({'farm_message_id': farm_message.id}).eq('id', farm_data['id']).execute()
            await farm_thread.add_user(user)
            await interaction.followup.send(f"✅ あなただけの農場を作成しました！ {farm_thread.mention} を確認してください。", ephemeral=True)
        except Exception as e:
            logger.error(f"농장 생성 중 오류 발생: {e}", exc_info=True)
            await interaction.followup.send("❌ 農場の作成中にエラーが発生しました。", ephemeral=True)

    async def handle_farm_till(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user, farm_owner = interaction.user, self.bot.get_user(self.farm_owner_id_from_interaction(interaction))
        gear = await get_user_gear(str(user.id))
        equipped_hoe = gear.get('hoe', BARE_HANDS)
        if equipped_hoe == BARE_HANDS:
            return await interaction.followup.send("❌ まずは商店で「クワ」を購入して、プロフィール画面から装備してください。", ephemeral=True)

        farm_data = await get_farm_data(farm_owner.id)
        hoe_power = {'古いクワ': 1, '一般のクワ': 4, '中級のクワ': 9, '高級クワ': 16}.get(equipped_hoe, 0)
        
        tilled_count, plots_to_update = 0, []
        for plot in farm_data.get('farm_plots', []):
            if plot['state'] == 'default' and tilled_count < hoe_power:
                plots_to_update.append(update_plot(plot['id'], {'state': 'tilled'}))
                tilled_count += 1
        
        if not plots_to_update:
            return await interaction.followup.send("ℹ️ これ以上耕せる畑がありません。", ephemeral=True, delete_after=10)
        
        await asyncio.gather(*plots_to_update)
        await interaction.followup.send(f"✅ **{equipped_hoe}** を使って、畑を**{tilled_count}マス**耕しました。", ephemeral=True, delete_after=10)
        await self.update_farm_ui(interaction.channel, farm_owner, interaction)

    async def handle_farm_plant(self, interaction: discord.Interaction):
        farm_owner = self.bot.get_user(self.farm_owner_id_from_interaction(interaction))
        farm_data = await get_farm_data(farm_owner.id)
        action_view = FarmActionView(self, farm_data, interaction.user, "plant_seed")
        await action_view.send_initial_message(interaction)
    
    async def handle_farm_water(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user, farm_owner = interaction.user, self.bot.get_user(self.farm_owner_id_from_interaction(interaction))
        gear = await get_user_gear(str(user.id))
        equipped_wc = gear.get('watering_can', BARE_HANDS)
        if equipped_wc == BARE_HANDS:
            return await interaction.followup.send("❌ まずは商店で「じょうろ」を購入して、装備してください。", ephemeral=True)

        farm_data = await get_farm_data(farm_owner.id)
        wc_power = {'古いじょうろ': 1, '一般のじょうろ': 4, '中級のじょうろ': 9, '高級じょうろ': 16}.get(equipped_wc, 0)
        
        watered_count, plots_to_update = 0, []
        now = datetime.now(timezone.utc)
        
        for plot in farm_data.get('farm_plots', []):
            if plot['state'] == 'planted' and watered_count < wc_power:
                farmable_info = await get_farmable_item_info(plot['planted_item_name'])
                if not farmable_info: continue
                
                needs_water = True
                if last_watered_str := plot.get('last_watered_at'):
                    last_watered_time = datetime.fromisoformat(last_watered_str)
                    if now - last_watered_time < timedelta(hours=farmable_info['watering_interval_hours']):
                        needs_water = False
                
                if needs_water:
                    plots_to_update.append(
                        update_plot(plot['id'], {'last_watered_at': now.isoformat(), 'water_count': plot['water_count'] + 1})
                    )
                    watered_count += 1

        if not plots_to_update:
            return await interaction.followup.send("ℹ️ 今は水をやる必要のある作物がありません。", ephemeral=True, delete_after=10)
        
        await asyncio.gather(*plots_to_update)
        await interaction.followup.send(f"✅ **{equipped_wc}** を使って、作物**{watered_count}個**に水をやりました。", ephemeral=True, delete_after=10)
        await self.update_farm_ui(interaction.channel, farm_owner, interaction)

    async def handle_farm_harvest(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        farm_owner = self.bot.get_user(self.farm_owner_id_from_interaction(interaction))
        farm_data = await get_farm_data(farm_owner.id)
        
        harvested_items = {}
        plots_to_reset = []
        trees_to_update = []
        
        for plot in farm_data.get('farm_plots', []):
            if plot['growth_stage'] != 3: continue

            farmable_info = await get_farmable_item_info(plot['planted_item_name'])
            if not farmable_info: continue

            # 수확량 계산
            growth_hours = farmable_info['growth_time_days'] * 24
            interval_hours = farmable_info['watering_interval_hours']
            max_water_count = growth_hours // interval_hours
            water_count = plot['water_count']
            
            base_yield = 3 # 기본 수확량
            bonus = max(0, water_count - max_water_count)
            penalty = max(0, max_water_count - water_count)
            final_yield = base_yield + bonus - penalty
            if final_yield < 1: final_yield = 1

            harvested_item_name = plot['planted_item_name'].replace("の種", "").replace("の苗木", "")
            harvested_items[harvested_item_name] = harvested_items.get(harvested_item_name, 0) + final_yield

            if not farmable_info['regrows']:
                plots_to_reset.append(plot['id'])
            else: # 나무
                trees_to_update.append(plot['id'])

        if not harvested_items:
            return await interaction.followup.send("ℹ️ 収穫できる作物がありません。", ephemeral=True)
        
        update_tasks = [update_inventory(str(farm_owner.id), name, qty) for name, qty in harvested_items.items()]
        if plots_to_reset:
            await clear_plots_db(plots_to_reset)
        if trees_to_update:
            now = datetime.now(timezone.utc)
            regrowth_days = (await get_farmable_item_info(plot['planted_item_name']))['regrowth_time_days']
            next_harvest_time = now + timedelta(days=regrowth_days)
            update_tasks.extend([
                update_plot(pid, {'growth_stage': 2, 'planted_at': next_harvest_time.isoformat(), 'water_count': 0})
                for pid in trees_to_update
            ])
            
        await asyncio.gather(*update_tasks)
        
        result_str = ", ".join([f"**{name}** {qty}個" for name, qty in harvested_items.items()])
        await interaction.followup.send(f"🎉 **{result_str}**を収穫しました！", ephemeral=True, delete_after=10)
        await self.update_farm_ui(interaction.channel, farm_owner, interaction)

    async def handle_farm_uproot(self, interaction: discord.Interaction):
        farm_owner = self.bot.get_user(self.farm_owner_id_from_interaction(interaction))
        farm_data = await get_farm_data(farm_owner.id)
        action_view = FarmActionView(self, farm_data, interaction.user, "uproot")
        await action_view.send_initial_message(interaction)
        
    async def handle_farm_invite(self, interaction: discord.Interaction):
        view = ui.View()
        user_select = ui.UserSelect(placeholder="農場に招待するユーザーを選択...")
        async def callback(select_interaction: discord.Interaction):
            await select_interaction.response.defer(ephemeral=True)
            for user in user_select.values:
                try:
                    await interaction.channel.add_user(user)
                    await select_interaction.followup.send(f"✅ {user.mention}さんを農場に招待しました。", ephemeral=True, delete_after=10)
                except Exception:
                    await select_interaction.followup.send(f"❌ {user.mention}さんの招待に失敗しました。", ephemeral=True, delete_after=10)
            await interaction.edit_original_response(content="招待が完了しました。", view=None)
        user_select.callback = callback
        view.add_item(user_select)
        await interaction.response.send_message("誰を農場に招待しますか？", view=view, ephemeral=True)

    async def handle_farm_share(self, interaction: discord.Interaction):
        farm_owner = self.bot.get_user(self.farm_owner_id_from_interaction(interaction))
        farm_data = await get_farm_data(farm_owner.id)
        view = ui.View()
        user_select = ui.UserSelect(placeholder="権限を付与するユーザーを選択...")
        async def callback(select_interaction: discord.Interaction):
            await select_interaction.response.defer(ephemeral=True)
            for user in user_select.values:
                await grant_farm_permission(farm_data['id'], user.id)
                await select_interaction.followup.send(f"✅ {user.mention}さんに農場の編集権限を付与しました。", ephemeral=True, delete_after=10)
            await interaction.edit_original_response(content="権限設定が完了しました。", view=None)
        user_select.callback = callback
        view.add_item(user_select)
        await interaction.response.send_message("誰に農場の権限を付与しますか？", view=view, ephemeral=True)

    async def handle_farm_rename(self, interaction: discord.Interaction):
        farm_owner = self.bot.get_user(self.farm_owner_id_from_interaction(interaction))
        farm_data = await get_farm_data(farm_owner.id)
        if not farm_data:
             return await interaction.response.send_message("❌ 農場データが見つかりませんでした。", ephemeral=True, delete_after=5)
        modal = FarmNameModal(self, farm_data)
        await interaction.response.send_modal(modal)

    def farm_owner_id_from_interaction(self, interaction: discord.Interaction) -> int:
        """FarmUIView의 데이터를 통해 상호작용이 발생한 농장의 소유자 ID를 찾습니다."""
        view = interaction.message.components[0].children[0].view # 불안정할 수 있으나 임시 방편
        if hasattr(view, 'farm_owner') and view.farm_owner:
            return view.farm_owner.id
        # 대체 로직: 스레드 이름에서 유저 이름 찾기 등... (현재는 위 로직에 의존)
        logger.warning("Farm owner ID를 찾을 수 없습니다. interaction_check 로직을 확인하세요.")
        return interaction.user.id # 임시 fallback

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
