# bot-game/cogs/games/cooking.py

import discord
from discord.ext import commands, tasks
from discord import ui
import logging
import asyncio
from typing import Optional, Dict, List, Any
from datetime import datetime, timezone, timedelta

from utils.database import (
    get_inventory, get_wallet, get_item_database, get_config, supabase,
    save_panel_id, get_panel_id, get_embed_from_db, update_inventory, update_wallet,
    get_id
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

COOKABLE_CATEGORIES = ["농장_작물", "광물", "아이템"]
MAX_CAULDRONS = 5
FAILED_DISH_NAME = "정체불명의 요리"
DEFAULT_COOK_TIME_HOURS = 1


class IngredientSelectModal(ui.Modal):
    def __init__(self, item_name: str, max_qty: int, parent_view: 'CookingPanelView'):
        super().__init__(title=f"'{item_name}' 수량 입력")
        self.parent_view = parent_view
        self.item_name = item_name
        self.quantity_input = ui.TextInput(label="수량", placeholder=f"최대 {max_qty}개")
        self.add_item(self.quantity_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            quantity = int(self.quantity_input.value)
            max_qty = int(self.quantity_input.placeholder.split(' ')[1].replace('개', ''))
            if not 1 <= quantity <= max_qty: raise ValueError
            await self.parent_view.add_ingredient(interaction, self.item_name, quantity)
        except ValueError:
            await interaction.response.send_message(f"1에서 {max_qty} 사이의 숫자를 입력해주세요.", ephemeral=True, delete_after=5)
        except Exception as e:
            logger.error(f"재료 수량 입력 처리 중 오류: {e}", exc_info=True)


class IngredientSelectView(ui.View):
    def __init__(self, parent_view: 'CookingPanelView'):
        super().__init__(timeout=180)
        self.parent_view = parent_view
        self.user = parent_view.user

    async def start(self, interaction: discord.Interaction):
        await self.build_components()
        await interaction.response.send_message("추가할 재료를 선택하세요.", view=self, ephemeral=True)

    async def build_components(self):
        self.clear_items()
        inventory = await get_inventory(self.user)
        item_db = get_item_database()
        cauldron = self.parent_view.get_selected_cauldron()
        current_ingredients = (cauldron.get('current_ingredients') or {}).keys() if cauldron else []
        cookable_items = {
            name: qty for name, qty in inventory.items()
            if item_db.get(name, {}).get('category') in COOKABLE_CATEGORIES and name not in current_ingredients
        }
        if not cookable_items:
            self.add_item(ui.Button(label="요리할 재료가 없습니다.", disabled=True))
            return
        options = [discord.SelectOption(label=f"{name} ({qty}개)", value=name) for name, qty in cookable_items.items()]
        item_select = ui.Select(placeholder="재료 선택...", options=options[:25])
        item_select.callback = self.on_item_select
        self.add_item(item_select)

    async def on_item_select(self, interaction: discord.Interaction):
        item_name = interaction.data['values'][0]
        inventory = await get_inventory(self.user)
        max_qty = inventory.get(item_name, 0)
        modal = IngredientSelectModal(item_name, max_qty, self.parent_view)
        await interaction.response.send_modal(modal)
        try:
            await interaction.delete_original_response()
        except (discord.NotFound, discord.HTTPException): pass


class CookingPanelView(ui.View):
    def __init__(self, cog: 'Cooking', user: Optional[discord.Member] = None, message: Optional[discord.Message] = None):
        super().__init__(timeout=None)
        self.cog = cog
        self.user = user
        self.cauldrons: List[Dict] = []
        self.selected_cauldron_slot: Optional[int] = None
        self.message = message

    async def _load_context(self, interaction: discord.Interaction) -> bool:
        """상호작용이 발생했을 때, 스레드 ID를 기반으로 소유자 정보를 DB에서 불러와 View를 초기화합니다."""
        res = await supabase.table('user_settings').select('user_id').eq('kitchen_thread_id', interaction.channel.id).maybe_single().execute()
        if not (res and res.data):
            await interaction.response.send_message("이 부엌 정보를 찾을 수 없습니다.", ephemeral=True, delete_after=5)
            return False
        
        owner_id = int(res.data['user_id'])
        self.user = self.cog.bot.get_user(owner_id)
        if not self.user:
            await interaction.response.send_message("부엌 주인을 찾을 수 없습니다.", ephemeral=True, delete_after=5)
            return False

        cauldron_res = await supabase.table('cauldrons').select('*').eq('user_id', owner_id).order('slot_number').execute()
        self.cauldrons = cauldron_res.data if cauldron_res.data else []
        self.message = interaction.message
        return True

    def get_selected_cauldron(self) -> Optional[Dict]:
        if self.selected_cauldron_slot is None: return None
        return next((c for c in self.cauldrons if c['slot_number'] == self.selected_cauldron_slot), None)

    async def refresh(self, interaction: discord.Interaction):
        await self.build_components()
        embed = await self.build_embed()
        if not interaction.response.is_done():
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await self.message.edit(embed=embed, view=self)

    async def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title=f"🍲 {self.user.display_name}의 부엌", color=0xE67E22)
        inventory = await get_inventory(self.user)
        total_cauldrons = inventory.get("가마솥", 0)
        embed.description = f"**보유한 가마솥:** {len(self.cauldrons)} / {total_cauldrons} (최대 {MAX_CAULDRONS}개)"
        cauldron = self.get_selected_cauldron()
        if cauldron:
            state_map = {'idle': '대기 중', 'adding_ingredients': '재료 넣는 중', 'cooking': '요리 중', 'ready': '요리 완료'}
            state_str = state_map.get(cauldron['state'], '알 수 없음')
            field_value_parts = [f"**상태:** {state_str}"]
            ingredients = cauldron.get('current_ingredients') or {}
            if ingredients:
                ing_str = "\n".join([f"ㄴ {name}: {qty}개" for name, qty in ingredients.items()])
                field_value_parts.append(f"**넣은 재료:**\n{ing_str}")
            if cauldron['state'] == 'cooking':
                completes_at = datetime.fromisoformat(cauldron['cooking_completes_at'].replace('Z', '+00:00'))
                field_value_parts.append(f"**완료까지:** {discord.utils.format_dt(completes_at, 'R')}")
                field_value_parts.append(f"**예상 요리:** {cauldron['result_item_name']}")
            elif cauldron['state'] == 'ready':
                field_value_parts.append(f"**완성된 요리:** {cauldron['result_item_name']}")
            embed.add_field(name=f"솥 #{self.selected_cauldron_slot} 정보", value="\n".join(field_value_parts), inline=False)
        else:
            embed.add_field(name="안내", value="관리할 가마솥을 아래 메뉴에서 선택하거나, 새로 설치해주세요.", inline=False)
        return embed

    async def build_components(self):
        self.clear_items()
        inventory = await get_inventory(self.user)
        total_cauldrons = inventory.get("가마솥", 0)
        cauldron_options = []
        for i in range(1, total_cauldrons + 1):
            is_installed = any(c['slot_number'] == i for c in self.cauldrons)
            label = f"솥 #{i}" + ("" if is_installed else " (설치하기)")
            option = discord.SelectOption(label=label, value=str(i))
            if self.selected_cauldron_slot == i: option.default = True
            cauldron_options.append(option)
        
        if cauldron_options:
            cauldron_select = ui.Select(placeholder="관리할 가마솥을 선택하세요...", options=cauldron_options, custom_id="cooking_panel:select_cauldron")
            cauldron_select.callback = self.on_cauldron_select
            self.add_item(cauldron_select)

        cauldron = self.get_selected_cauldron()
        if cauldron:
            state = cauldron['state']
            if state in ['idle', 'adding_ingredients']:
                self.add_item(ui.Button(label="재료 넣기", emoji="🥕", custom_id="cooking_panel:add_ingredient", row=1))
                self.add_item(ui.Button(label="재료 비우기", emoji="🗑️", custom_id="cooking_panel:clear_ingredients", row=1, disabled=not cauldron.get('current_ingredients')))
                self.add_item(ui.Button(label="요리 시작!", style=discord.ButtonStyle.success, emoji="🔥", custom_id="cooking_panel:start_cooking", row=2, disabled=not cauldron.get('current_ingredients')))
            elif state == 'ready':
                self.add_item(ui.Button(label="요리 받기", style=discord.ButtonStyle.primary, emoji="🎁", custom_id="cooking_panel:claim_dish", row=1))

    async def on_cauldron_select(self, interaction: discord.Interaction):
        if not await self._load_context(interaction): return
        
        slot = int(interaction.data['values'][0])
        is_installed = any(c['slot_number'] == slot for c in self.cauldrons)
        if not is_installed:
            await supabase.table('cauldrons').insert({'user_id': self.user.id, 'slot_number': slot, 'state': 'idle'}).execute()
        self.selected_cauldron_slot = slot
        await self.refresh(interaction)

    async def add_ingredient_prompt(self, interaction: discord.Interaction):
        cauldron = self.get_selected_cauldron()
        if not cauldron or cauldron['state'] not in ['idle', 'adding_ingredients']:
            return await interaction.response.send_message("❌ 지금은 재료를 추가할 수 없습니다.", ephemeral=True, delete_after=5)
        
        view = IngredientSelectView(self)
        await view.start(interaction)

    async def add_ingredient(self, interaction: discord.Interaction, item_name: str, quantity: int):
        await interaction.response.defer()
        cauldron = self.get_selected_cauldron()
        current_ingredients = cauldron.get('current_ingredients') or {}
        current_ingredients[item_name] = current_ingredients.get(item_name, 0) + quantity
        await supabase.table('cauldrons').update({'state': 'adding_ingredients', 'current_ingredients': current_ingredients}).eq('id', cauldron['id']).execute()
        await self.refresh(interaction)
    
    async def clear_ingredients(self, interaction: discord.Interaction):
        cauldron = self.get_selected_cauldron()
        await supabase.table('cauldrons').update({'state': 'idle', 'current_ingredients': None}).eq('id', cauldron['id']).execute()
        await self.refresh(interaction)
        
    async def start_cooking(self, interaction: discord.Interaction):
        cauldron = self.get_selected_cauldron()
        ingredients = cauldron.get('current_ingredients') or {}
        for name, qty in ingredients.items(): await update_inventory(self.user.id, name, -qty)
            
        res = await supabase.table('recipes').select('*').execute()
        recipes = res.data if res.data else []
        matched_recipe = next((r for r in recipes if r.get('ingredients') == ingredients), None)
        
        now = datetime.now(timezone.utc)
        cook_time = timedelta(days=matched_recipe['cook_time_days']) if matched_recipe else timedelta(hours=DEFAULT_COOK_TIME_HOURS)
        result_item = matched_recipe['result_item_name'] if matched_recipe else FAILED_DISH_NAME
        completes_at = now + cook_time
        await supabase.table('cauldrons').update({
            'state': 'cooking', 'cooking_started_at': now.isoformat(),
            'cooking_completes_at': completes_at.isoformat(), 'result_item_name': result_item
        }).eq('id', cauldron['id']).execute()
        await self.refresh(interaction)
    
    async def claim_dish(self, interaction: discord.Interaction):
        cauldron = self.get_selected_cauldron()
        result_item = cauldron['result_item_name']
        await update_inventory(self.user.id, result_item, 1)
        await supabase.table('cauldrons').update({
            'state': 'idle', 'current_ingredients': None, 'cooking_started_at': None,
            'cooking_completes_at': None, 'result_item_name': None
        }).eq('id', cauldron['id']).execute()
        await interaction.followup.send(f"✅ **{result_item}** 획득!", ephemeral=True, delete_after=10)
        await self.refresh(interaction)

    async def dispatch_button_callback(self, interaction: discord.Interaction):
        """모든 버튼 상호작용의 진입점"""
        if not await self._load_context(interaction): return
        
        # 권한 확인: 버튼 누른 사람이 주인인지?
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("부엌 주인만 조작할 수 있습니다.", ephemeral=True, delete_after=5)
        
        await interaction.response.defer()
        
        custom_id = interaction.data['custom_id']
        action = custom_id.split(':')[-1]

        method_map = {
            "add_ingredient": self.add_ingredient_prompt,
            "clear_ingredients": self.clear_ingredients,
            "start_cooking": self.start_cooking,
            "claim_dish": self.claim_dish,
        }
        if method := method_map.get(action):
            await method(interaction)

class CookingCreationPanelView(ui.View):
    def __init__(self, cog: 'Cooking'):
        super().__init__(timeout=None)
        self.cog = cog
        btn = ui.Button(label="부엌 만들기", style=discord.ButtonStyle.success, emoji="🍲", custom_id="cooking_create_button")
        btn.callback = self.create_kitchen_callback
        self.add_item(btn)

    async def create_kitchen_callback(self, interaction: discord.Interaction):
        await self.cog.create_kitchen_thread(interaction)
    
class Cooking(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.currency_icon = "🪙"
        self.check_completed_cooking.start()

    async def cog_load(self):
        self.currency_icon = get_config("GAME_CONFIG", {}).get("CURRENCY_ICON", "🪙")

    def cog_unload(self):
        self.check_completed_cooking.cancel()

    @tasks.loop(minutes=1)
    async def check_completed_cooking(self):
        now = datetime.now(timezone.utc)
        res = await supabase.table('cauldrons').select('*, user_settings(kitchen_thread_id)').eq('state', 'cooking').lte('cooking_completes_at', now.isoformat()).execute()
        if not (res and res.data): return

        for cauldron in res.data:
            await supabase.table('cauldrons').update({'state': 'ready'}).eq('id', cauldron['id']).execute()
            user_id = int(cauldron['user_id'])
            user = self.bot.get_user(user_id)
            if not user: continue
            
            thread_id = cauldron.get('user_settings', {}).get('kitchen_thread_id')
            if thread_id and (thread := self.bot.get_channel(thread_id)):
                await thread.send(f"{user.mention}, **{cauldron['result_item_name']}** 요리가 완성되었습니다!", allowed_mentions=discord.AllowedMentions(users=True))
            
            try: await user.send(f"🍲 **{cauldron['result_item_name']}** 요리가 완성되었습니다! 부엌에서 확인해주세요.")
            except discord.Forbidden: pass
            
            log_channel_id = get_id("log_cooking_complete_channel_id")
            if log_channel_id and (log_channel := self.bot.get_channel(log_channel_id)):
                embed_data = await get_embed_from_db("log_cooking_complete")
                if embed_data:
                    embed = format_embed_from_db(embed_data, user_mention=user.mention, recipe_name=cauldron['result_item_name'])
                    await log_channel.send(embed=embed)

    @check_completed_cooking.before_loop
    async def before_check_completed_cooking(self): await self.bot.wait_until_ready()

    async def register_persistent_views(self):
        self.bot.add_view(CookingCreationPanelView(self))
        # CookingPanelView는 이제 영구적이므로 여기서 등록합니다.
        self.bot.add_view(CookingPanelView(self))

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_cooking_creation"):
        if panel_info := get_panel_id(panel_key):
            try:
                if old_channel := self.bot.get_channel(panel_info['channel_id']):
                    msg = await old_channel.fetch_message(panel_info['message_id'])
                    await msg.delete()
            except (discord.NotFound, discord.Forbidden): pass
        
        embed_data = await get_embed_from_db(panel_key)
        if not embed_data: return logger.error(f"DB에서 '{panel_key}' 임베드를 찾을 수 없습니다.")
        embed = discord.Embed.from_dict(embed_data)
        view = CookingCreationPanelView(self)
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_key, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} 패널을 성공적으로 생성했습니다.")

    async def create_kitchen_thread(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user = interaction.user
        try:
            res = await supabase.table('user_settings').select('kitchen_thread_id').eq('user_id', user.id).maybe_single().execute()
            thread_id = res.data.get('kitchen_thread_id') if res and res.data else None
        except Exception as e:
            logger.error(f"user_settings 테이블 조회 중 오류: {e}", exc_info=True)
            thread_id = None

        if thread_id and (thread := self.bot.get_channel(thread_id)):
            await interaction.followup.send(f"✅ 당신의 부엌은 여기입니다: {thread.mention}", ephemeral=True)
            try: await thread.add_user(user)
            except discord.HTTPException: pass
            return

        try:
            if not isinstance(interaction.channel, discord.TextChannel):
                await interaction.followup.send("❌ 이 채널에서는 스레드를 생성할 수 없습니다. 일반 텍스트 채널에서 시도해주세요.", ephemeral=True)
                return

            thread = await interaction.channel.create_thread(name=f"🍲｜{user.display_name}의 부엌", type=discord.ChannelType.private_thread)
            await thread.add_user(user)
            await supabase.table('user_settings').upsert({'user_id': user.id, 'kitchen_thread_id': thread.id}).execute()
            
            embed_data = await get_embed_from_db("cooking_thread_welcome")
            if embed_data: await thread.send(embed=format_embed_from_db(embed_data, user_name=user.display_name))

            panel_view = CookingPanelView(self, user)
            message = await thread.send("부엌 로딩 중...")
            panel_view.message = message
            await panel_view.refresh(interaction)

            await interaction.followup.send(f"✅ 당신만의 부엌을 만들었습니다! {thread.mention} 채널을 확인해주세요.", ephemeral=True)

        except Exception as e:
            logger.error(f"부엌 생성 중 오류: {e}", exc_info=True)
            await interaction.followup.send("❌ 부엌을 만드는 중 오류가 발생했습니다.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Cooking(bot))
