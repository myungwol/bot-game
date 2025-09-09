import discord
from discord.ext import commands, tasks
from discord import ui
import logging
import asyncio
from typing import Optional, Dict, List, Any
from datetime import datetime, timezone, timedelta
import json
import random
import time

from utils.database import (
    get_inventory, get_wallet, get_item_database, get_config, supabase,
    save_panel_id, get_panel_id, get_embed_from_db, update_inventory,
    get_id, log_activity, get_user_abilities, delete_config_from_db, save_config_to_db
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

COOKABLE_CATEGORIES = ["농장_작물", "광물", "아이템", "생선"]
MAX_CAULDRONS = 5
FAILED_DISH_NAME = "정체불명의 요리"
DEFAULT_COOK_TIME_MINUTES = 10
XP_PER_INGREDIENT = 3

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
        await interaction.followup.send("추가할 재료를 선택하세요.", view=self, ephemeral=True)

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
        res = await supabase.table('user_settings').select('user_id').eq('kitchen_thread_id', interaction.channel.id).maybe_single().execute()
        if not (res and res.data):
            await interaction.followup.send("이 부엌 정보를 찾을 수 없습니다.", ephemeral=True, delete_after=5)
            return False
        
        owner_id = int(res.data['user_id'])
        try:
            guild = self.cog.bot.get_guild(interaction.guild_id)
            self.user = await guild.fetch_member(owner_id)
        except (discord.NotFound, AttributeError):
            await interaction.followup.send("부엌 주인을 찾을 수 없습니다.", ephemeral=True, delete_after=5)
            return False

        cauldron_res = await supabase.table('cauldrons').select('*').eq('user_id', owner_id).order('slot_number').execute()
        self.cauldrons = cauldron_res.data if cauldron_res.data else []
        self.message = interaction.message
        return True

    def get_selected_cauldron(self) -> Optional[Dict]:
        if self.selected_cauldron_slot is None: return None
        return next((c for c in self.cauldrons if c['slot_number'] == self.selected_cauldron_slot), None)

    async def refresh(self, interaction: Optional[discord.Interaction] = None):
        if interaction and not interaction.response.is_done():
            await interaction.response.defer()

        if not self.user:
            logger.error("CookingPanelView refresh: self.user가 설정되지 않았습니다.")
            return

        cauldron_res = await supabase.table('cauldrons').select('*').eq('user_id', self.user.id).order('slot_number').execute()
        self.cauldrons = cauldron_res.data if cauldron_res.data else []
        
        await self.build_components()
        embed = await self.build_embed()
        
        try:
            target_editor = interaction.edit_original_response if interaction and not interaction.is_expired() else self.message.edit
            await target_editor(content=None, embed=embed, view=self)
        except (discord.NotFound, AttributeError, discord.HTTPException) as e:
            logger.warning(f"요리 패널 메시지 수정/생성 실패: {e}")
            channel = interaction.channel if interaction else self.message.channel if self.message else None
            if channel:
                try:
                    self.message = await channel.send(content=None, embed=embed, view=self)
                    await supabase.table('user_settings').update({'kitchen_panel_message_id': self.message.id}).eq('user_id', self.user.id).execute()
                except Exception as e_inner:
                    logger.error(f"요리 패널 메시지 재생성 실패: {e_inner}")

    async def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title=f"🍲 {self.user.display_name}의 부엌", color=0xE67E22)
        inventory = await get_inventory(self.user)
        total_cauldrons = inventory.get("가마솥", 0)
        
        installed_cauldrons = len(self.cauldrons)
        embed.description = f"**보유한 가마솥:** {installed_cauldrons} / {total_cauldrons} (최대 {MAX_CAULDRONS}개)"
        
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
        await interaction.response.defer()
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
            await interaction.followup.send("❌ 지금은 재료를 추가할 수 없습니다.", ephemeral=True, delete_after=5)
            return
        
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
        
        total_ingredients_count = sum(ingredients.values())
        xp_earned = total_ingredients_count * XP_PER_INGREDIENT

        res = await supabase.table('recipes').select('*').execute()
        recipes = res.data if res.data else []
        matched_recipe = next((r for r in recipes if r.get('ingredients') == json.dumps(ingredients)), None)
        
        now = datetime.now(timezone.utc)
        cook_time_minutes = matched_recipe['cook_time_minutes'] if matched_recipe else DEFAULT_COOK_TIME_MINUTES
        cook_time = timedelta(minutes=int(cook_time_minutes))
        
        result_item = matched_recipe['result_item_name'] if matched_recipe else FAILED_DISH_NAME
        completes_at = now + cook_time
        
        try:
            for name, qty in ingredients.items(): 
                await update_inventory(self.user.id, name, -qty)
            
            await supabase.table('cauldrons').update({
                'state': 'cooking', 'cooking_started_at': now.isoformat(),
                'cooking_completes_at': completes_at.isoformat(), 'result_item_name': result_item
            }).eq('id', cauldron['id']).execute()

            await log_activity(self.user.id, 'cooking', amount=total_ingredients_count, xp_earned=xp_earned)
            if xp_earned > 0:
                xp_res = await supabase.rpc('add_xp', {'p_user_id': self.user.id, 'p_xp_to_add': xp_earned, 'p_source': 'cooking'}).execute()
                if xp_res.data and (level_cog := self.cog.bot.get_cog("LevelSystem")):
                    await level_cog.handle_level_up_event(self.user, xp_res.data)
        except Exception as e:
            logger.error(f"요리 시작 중 오류: {e}", exc_info=True)
            await interaction.followup.send("❌ 요리를 시작하는 중 오류가 발생했습니다.", ephemeral=True)

        await self.refresh(interaction)
    
    async def claim_dish(self, interaction: discord.Interaction):
        cauldron = self.get_selected_cauldron()
        result_item = cauldron['result_item_name']
        
        user_abilities = await get_user_abilities(self.user.id)
        quantity_to_claim = 1
        double_yield_activated = False
        
        if 'cook_double_yield_2' in user_abilities and random.random() < 0.15:
            quantity_to_claim = 2
            double_yield_activated = True

        await update_inventory(self.user.id, result_item, quantity_to_claim)

        if result_item != FAILED_DISH_NAME:
            await self.cog.check_and_log_recipe_discovery(interaction.user, result_item, cauldron.get('current_ingredients'))

        await supabase.table('cauldrons').update({
            'state': 'idle', 'current_ingredients': None, 'cooking_started_at': None,
            'cooking_completes_at': None, 'result_item_name': None
        }).eq('id', cauldron['id']).execute()
        
        success_message = f"✅ **{result_item}** {quantity_to_claim}개 획득!"
        if double_yield_activated:
            success_message += "\n✨ **풍성한 식탁** 능력 발동! 요리를 2개 획득했습니다!"
            
        msg = await interaction.followup.send(success_message, ephemeral=True)
        await asyncio.sleep(5)
        try:
            await msg.delete()
        except (discord.NotFound, discord.Forbidden):
            pass

        await self.refresh(interaction)

    async def dispatch_button_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not await self._load_context(interaction): return
        
        if interaction.user.id != self.user.id:
            await interaction.followup.send("부엌 주인만 조작할 수 있습니다.", ephemeral=True, delete_after=5)
            return
        
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
        await interaction.response.defer(ephemeral=True)
        await self.cog.create_kitchen_thread(interaction)
    
class Cooking(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.currency_icon = "🪙"
        self.check_completed_cooking.start()
        self.kitchen_ui_updater.start()

    async def cog_load(self):
        self.currency_icon = get_config("GAME_CONFIG", {}).get("CURRENCY_ICON", "🪙")
        # [추가] 봇이 켜질 때 영속성 뷰를 등록
        self.register_persistent_views()

    def cog_unload(self):
        self.check_completed_cooking.cancel()
        self.kitchen_ui_updater.cancel()

    @tasks.loop(minutes=1)
    async def check_completed_cooking(self):
        now = datetime.now(timezone.utc)
        try:
            cauldrons_res = await supabase.table('cauldrons').select('*').eq('state', 'cooking').lte('cooking_completes_at', now.isoformat()).execute()
            if not (cauldrons_res and cauldrons_res.data): return

            completed_cauldrons = cauldrons_res.data
            user_ids = list(set(int(c['user_id']) for c in completed_cauldrons))

            user_settings_res = await supabase.table('user_settings').select('user_id, kitchen_thread_id').in_('user_id', user_ids).execute()
            
            thread_id_map = {}
            if user_settings_res and user_settings_res.data:
                thread_id_map = {int(setting['user_id']): setting.get('kitchen_thread_id') for setting in user_settings_res.data}

            for cauldron in completed_cauldrons:
                await supabase.table('cauldrons').update({'state': 'ready'}).eq('id', cauldron['id']).execute()
                user_id = int(cauldron['user_id'])
                user = self.bot.get_user(user_id)
                if not user: continue
                
                thread_id_str = thread_id_map.get(user_id)
                if thread_id_str and (thread := self.bot.get_channel(int(thread_id_str))):
                    try:
                        await thread.send(f"{user.mention}, **{cauldron['result_item_name']}** 요리가 완성되었습니다!", allowed_mentions=discord.AllowedMentions(users=True))
                    except discord.Forbidden:
                        logger.warning(f"채널 {thread_id_str}에 메시지를 보낼 수 없습니다.")
                
                try: 
                    await user.send(f"🍲 **{cauldron['result_item_name']}** 요리가 완성되었습니다! 부엌에서 확인해주세요.")
                except discord.Forbidden: pass
                
                log_channel_id = get_id("log_cooking_complete_channel_id")
                if log_channel_id and (log_channel := self.bot.get_channel(log_channel_id)):
                    embed_data = await get_embed_from_db("log_cooking_complete")
                    if embed_data:
                        embed = format_embed_from_db(embed_data, user_mention=user.mention, recipe_name=cauldron['result_item_name'])
                        await log_channel.send(embed=embed)
        except Exception as e:
            logger.error(f"요리 완료 확인 작업 중 오류 발생: {e}", exc_info=True)

    @check_completed_cooking.before_loop
    async def before_check_completed_cooking(self): await self.bot.wait_until_ready()

    @tasks.loop(seconds=5.0)
    async def kitchen_ui_updater(self):
        try:
            res = await supabase.table('bot_configs').select('config_key').like('config_key', 'kitchen_ui_update_request_%').execute()
            if not (res and res.data): return
            
            keys_to_delete = [req['config_key'] for req in res.data]
            keys_to_delete_tuple = tuple(keys_to_delete)

            for req in res.data:
                try:
                    user_id = int(req['config_key'].split('_')[-1])
                    user = self.bot.get_user(user_id)
                    if not user: continue

                    settings_res = await supabase.table('user_settings').select('kitchen_thread_id, kitchen_panel_message_id').eq('user_id', user_id).maybe_single().execute()
                    if not (settings_res and settings_res.data and settings_res.data.get('kitchen_thread_id')):
                        continue
                    
                    thread_id = int(settings_res.data['kitchen_thread_id'])
                    message_id = settings_res.data.get('kitchen_panel_message_id')
                    
                    thread = self.bot.get_channel(thread_id)
                    if not thread: continue

                    message = None
                    if message_id:
                        try:
                            message = await thread.fetch_message(int(message_id))
                        except (discord.NotFound, discord.Forbidden):
                            logger.warning(f"키친 패널 메시지(ID: {message_id})를 찾을 수 없어 새로 생성합니다.")
                    
                    panel_view = CookingPanelView(self, user, message)
                    await panel_view.refresh()

                except Exception as e:
                    logger.error(f"개별 키친 UI 업데이트 중 오류({req['config_key']}): {e}", exc_info=True)
            
            if keys_to_delete_tuple:
                await supabase.table('bot_configs').delete().in_('config_key', keys_to_delete_tuple).execute()
        except Exception as e:
            logger.error(f"키친 UI 업데이터 루프 중 오류: {e}", exc_info=True)

    @kitchen_ui_updater.before_loop
    async def before_kitchen_ui_updater(self):
        await self.bot.wait_until_ready()

    async def check_and_log_recipe_discovery(self, user: discord.Member, recipe_name: str, ingredients: Dict):
        try:
            res = await supabase.table('discovered_recipes').select('id').eq('recipe_name', recipe_name).maybe_single().execute()
            if res.data:
                return
            
            await supabase.table('discovered_recipes').insert({
                'recipe_name': recipe_name,
                'discoverer_id': user.id,
                'guild_id': user.guild.id
            }).execute()
            
            log_channel_id = get_id("log_recipe_discovery_channel_id")
            if not (log_channel_id and (log_channel := self.bot.get_channel(log_channel_id))):
                logger.warning("레시피 발견 로그 채널이 설정되지 않았습니다.")
                return

            embed = discord.Embed(
                title="🎉 새로운 레시피 발견!",
                description=f"**{user.mention}**님이 새로운 요리 **'{recipe_name}'**의 레시피를 최초로 발견했습니다!",
                color=0xFFD700
            )
            
            ingredients_str = "\n".join([f"ㄴ {name}: {qty}개" for name, qty in ingredients.items()])
            embed.add_field(name="📜 레시피", value=ingredients_str, inline=False)
            embed.set_thumbnail(url=user.display_avatar.url if user.display_avatar else None)
            
            await log_channel.send(content="@here", embed=embed, allowed_mentions=discord.AllowedMentions(everyone=True))
        except Exception as e:
            logger.error(f"레시피 발견 처리 중 오류: {e}", exc_info=True)

    # [수정] register_persistent_views 함수 추가
    async def register_persistent_views(self):
        self.bot.add_view(CookingCreationPanelView(self))
        self.bot.add_view(CookingPanelView(self))
        logger.info("✅ 요리 시스템의 영구 View가 성공적으로 등록되었습니다.")

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
        user = interaction.user
        try:
            res = await supabase.table('user_settings').select('kitchen_thread_id').eq('user_id', user.id).maybe_single().execute()
            thread_id = res.data.get('kitchen_thread_id') if res and res.data else None
        except Exception as e:
            logger.error(f"user_settings 테이블 조회 중 오류: {e}", exc_info=True)
            thread_id = None

        if thread_id and (thread := self.bot.get_channel(int(thread_id))):
            await interaction.followup.send(f"✅ 당신의 부엌은 여기입니다: {thread.mention}", ephemeral=True)
            try: await thread.add_user(user)
            except discord.HTTPException: pass
            return

        try:
            if not isinstance(interaction.channel, discord.TextChannel):
                await interaction.followup.send("❌ 이 채널에서는 스레드를 생성할 수 없습니다.", ephemeral=True)
                return

            thread = await interaction.channel.create_thread(name=f"🍲｜{user.display_name}의 부엌", type=discord.ChannelType.private_thread)
            await thread.add_user(user)
            await supabase.table('user_settings').upsert({'user_id': user.id, 'kitchen_thread_id': thread.id}).execute()
            
            embed_data = await get_embed_from_db("cooking_thread_welcome")
            if embed_data: await thread.send(embed=format_embed_from_db(embed_data, user_name=user.display_name))

            panel_view = CookingPanelView(self, user)
            message = await thread.send("부엌 로딩 중...")
            panel_view.message = message
            
            await supabase.table('user_settings').update({'kitchen_panel_message_id': message.id}).eq('user_id', user.id).execute()
            
            await panel_view.refresh(interaction) 

            await interaction.followup.send(f"✅ 당신만의 부엌을 만들었습니다! {thread.mention} 채널을 확인해주세요.", ephemeral=True)

        except Exception as e:
            logger.error(f"부엌 생성 중 오류: {e}", exc_info=True)
            await interaction.followup.send("❌ 부엌을 만드는 중 오류가 발생했습니다.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Cooking(bot))
