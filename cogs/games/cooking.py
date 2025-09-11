# cogs/games/cooking.py

import discord
from discord.ext import commands, tasks
from discord import ui
import logging
import asyncio
from typing import Optional, Dict, List, Any, Set # <--- 여기에 Set 추가
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

async def delete_after(message: discord.WebhookMessage, delay: int):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden):
        pass

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
        self.selected_dishes_to_claim: List[str] = []

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await self._load_context(interaction):
            return False

        if interaction.user.id != self.user.id:
            await interaction.response.send_message("부엌 주인만 조작할 수 있습니다.", ephemeral=True, delete_after=5)
            return False
        
        return True

    async def _load_context(self, interaction: discord.Interaction) -> bool:
        logger.info(f"[_load_context] 컨텍스트 로딩 시작. 스레드 ID: {interaction.channel.id}")
        
        # ▼▼▼ [핵심 수정] 여러 번의 select 대신 새로 만든 DB 함수(RPC)를 호출합니다. ▼▼▼
        rpc_params = {'p_thread_id': interaction.channel.id}
        res = await supabase.rpc('get_kitchen_context', rpc_params).maybe_single().execute()
        
        if not (res and res.data):
            if not interaction.response.is_done(): await interaction.response.defer()
            await interaction.followup.send("이 부엌 정보를 찾을 수 없습니다. 채널을 다시 만들어주세요.", ephemeral=True, delete_after=10)
            logger.error(f"[_load_context] DB에서 스레드 ID({interaction.channel.id})에 해당하는 부엌 정보를 찾지 못했습니다.")
            return False
        
        context = res.data
        owner_id = int(context['owner_id'])
        message_id = context.get('panel_message_id')
        self.selected_cauldron_slot = context.get('selected_slot')
        self.cauldrons = context.get('cauldrons') or [] # cauldrons가 null일 경우 빈 리스트로 처리
        
        logger.info(f"[_load_context] DB 함수 호출 성공: 소유자 ID({owner_id}), 메시지 ID({message_id}), 가마솥 {len(self.cauldrons)}개.")

        try:
            guild = self.cog.bot.get_guild(interaction.guild_id)
            if not guild:
                logger.error(f"[_load_context] 길드를 찾을 수 없습니다: {interaction.guild_id}")
                return False
            self.user = await guild.fetch_member(owner_id)
            logger.info(f"[_load_context] 소유자 멤버 객체를 성공적으로 가져왔습니다: {self.user.name}")
        except (discord.NotFound, AttributeError) as e:
            if not interaction.response.is_done(): await interaction.response.defer()
            await interaction.followup.send("부엌 주인을 찾을 수 없습니다.", ephemeral=True, delete_after=5)
            logger.error(f"[_load_context] 소유자 멤버 객체를 가져오는 중 오류 발생: {e}")
            return False

        if message_id:
            try:
                self.message = await interaction.channel.fetch_message(int(message_id))
                logger.info(f"[_load_context] 패널 메시지 객체를 성공적으로 가져왔습니다: {self.message.id}")
            except (discord.NotFound, discord.Forbidden):
                logger.warning(f"[_load_context] 부엌 패널 메시지(ID: {message_id})를 찾을 수 없어 새로 생성될 수 있습니다.")
                self.message = None
        
        return True

    def get_selected_cauldron(self) -> Optional[Dict]:
        if self.selected_cauldron_slot is None: return None
        return next((c for c in self.cauldrons if c['slot_number'] == self.selected_cauldron_slot), None)

    async def refresh(self, interaction: Optional[discord.Interaction] = None):
        if interaction and not interaction.response.is_done():
            await interaction.response.defer()

        if not self.user:
            if interaction:
                await interaction.followup.send("오류: 사용자 정보를 불러올 수 없습니다.", ephemeral=True)
            return

        cauldron_res = await supabase.table('cauldrons').select('*').eq('user_id', str(self.user.id)).order('slot_number').execute()
        self.cauldrons = cauldron_res.data if cauldron_res.data else []
        
        await self.build_components()
        embed = await self.build_embed()
        
        try:
            target_message = self.message or (interaction.message if interaction else None)
            if target_message:
                await target_message.edit(content=None, embed=embed, view=self)
            else:
                channel = interaction.channel if interaction else None
                if channel:
                    self.message = await channel.send(content=None, embed=embed, view=self)
                    await supabase.table('user_settings').update({'kitchen_panel_message_id': self.message.id}).eq('user_id', str(self.user.id)).execute()
        except (discord.NotFound, AttributeError, discord.HTTPException):
            channel = interaction.channel if interaction else (self.message.channel if self.message else None)
            if channel:
                try:
                    self.message = await channel.send(content=None, embed=embed, view=self)
                    await supabase.table('user_settings').update({'kitchen_panel_message_id': self.message.id}).eq('user_id', str(self.user.id)).execute()
                except Exception as e_inner:
                    logger.error(f"요리 패널 메시지 재생성 최종 실패: {e_inner}")

    async def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title=f"🍲 {self.user.display_name}의 부엌", color=0xE67E22)
        inventory = await get_inventory(self.user)
        total_cauldrons = inventory.get("가마솥", 0)
        
        installed_cauldrons = len(self.cauldrons)
        embed.description = "아래 목록에서 관리할 가마솥을 선택하거나, 버튼을 눌러 작업을 시작하세요."

        if not self.cauldrons:
            embed.add_field(
                name="가마솥 없음",
                value="상점에서 '가마솥'을 구매한 후, 아래 메뉴에서 설치해주세요.",
                inline=False
            )
        else:
            state_order = {'ready': 0, 'cooking': 1, 'adding_ingredients': 2, 'idle': 3}
            sorted_cauldrons = sorted(self.cauldrons, key=lambda c: state_order.get(c['state'], 4))
            
            for cauldron in sorted_cauldrons:
                slot_number = cauldron['slot_number']
                state = cauldron['state']
                
                state_map = {'idle': '대기 중', 'adding_ingredients': '재료 넣는 중', 'cooking': '요리 중', 'ready': '요리 완료'}
                state_str = state_map.get(state, '알 수 없음')
                
                title_emoji = "▶️" if self.selected_cauldron_slot == slot_number else "솥"
                
                field_value_parts = [f"**상태:** {state_str}"]
                
                ingredients = cauldron.get('current_ingredients') or {}
                if ingredients:
                    ing_str = ", ".join([f"{name} {qty}개" for name, qty in ingredients.items()])
                    field_value_parts.append(f"**재료:** {ing_str}")

                if state == 'cooking':
                    completes_at = datetime.fromisoformat(cauldron['cooking_completes_at'].replace('Z', '+00:00'))
                    field_value_parts.append(f"**완료까지:** {discord.utils.format_dt(completes_at, 'R')}")
                    if result_item := cauldron.get('result_item_name'):
                        field_value_parts.append(f"**예상 요리:** {result_item}")

                elif state == 'ready':
                    if result_item := cauldron.get('result_item_name'):
                        field_value_parts.append(f"**완성된 요리:** {result_item}")

                embed.add_field(
                    name=f"--- {title_emoji} #{slot_number} ---",
                    value="\n".join(field_value_parts),
                    inline=False
                )

        owner_abilities = await get_user_abilities(self.user.id)
        
        all_cooking_abilities_map = {}
        job_advancement_data = get_config("JOB_ADVANCEMENT_DATA", {})
        
        if isinstance(job_advancement_data, dict):
            for level_data in job_advancement_data.values():
                for job in level_data:
                    if 'chef' in job.get('job_key', ''):
                        for ability in job.get('abilities', []):
                            all_cooking_abilities_map[ability['ability_key']] = {
                                'name': ability['ability_name'],
                                'description': ability['description']
                            }
        
        active_effects = []
        EMOJI_MAP = {'ingredient': '✨', 'time': '⏱️', 'quality': '⭐', 'yield': '🎁'}
        
        for ability_key in owner_abilities:
            if ability_key in all_cooking_abilities_map:
                ability_info = all_cooking_abilities_map[ability_key]
                emoji = next((e for key, e in EMOJI_MAP.items() if key in ability_key), '🍳')
                active_effects.append(f"> {emoji} **{ability_info['name']}**: {ability_info['description']}")
        
        if active_effects:
            embed.add_field(
                name="--- 요리 패시브 효과 ---",
                value="\n".join(active_effects),
                inline=False
            )
        
        footer_text = f"보유한 가마솥: {installed_cauldrons} / {total_cauldrons} (최대 {MAX_CAULDRONS}개)"
        embed.set_footer(text=footer_text)
        return embed

    async def build_components(self):
        self.clear_items()
        inventory = await get_inventory(self.user)
        total_cauldrons = inventory.get("가마솥", 0)
        
        cauldron_options = []
        for i in range(1, min(total_cauldrons, MAX_CAULDRONS) + 1):
            is_installed = any(c['slot_number'] == i for c in self.cauldrons)
            label = f"솥 #{i}" + ("" if is_installed else " (설치하기)")
            option = discord.SelectOption(label=label, value=str(i))
            if self.selected_cauldron_slot == i: option.default = True
            cauldron_options.append(option)
        
        if cauldron_options:
            cauldron_select = ui.Select(placeholder="관리할 가마솥을 선택하세요...", options=cauldron_options, custom_id="cooking_panel:select_cauldron", row=0)
            cauldron_select.callback = self.on_cauldron_select
            self.add_item(cauldron_select)

        selected_cauldron = self.get_selected_cauldron()
        if selected_cauldron:
            state = selected_cauldron['state']
            if state in ['idle', 'adding_ingredients']:
                self.add_item(ui.Button(label="재료 넣기", emoji="🥕", custom_id="cooking_panel:add_ingredient", row=1))
                self.add_item(ui.Button(label="재료 비우기", emoji="🗑️", custom_id="cooking_panel:clear_ingredients", row=1, disabled=not selected_cauldron.get('current_ingredients')))
                self.add_item(ui.Button(label="요리 시작!", style=discord.ButtonStyle.success, emoji="🔥", custom_id="cooking_panel:start_cooking", row=2, disabled=not selected_cauldron.get('current_ingredients')))

        ready_cauldrons = [c for c in self.cauldrons if c['state'] == 'ready']
        if ready_cauldrons:
            options = [
                discord.SelectOption(
                    label=f"솥 #{c['slot_number']}: {c['result_item_name']}",
                    value=str(c['id']),
                    emoji="🍲"
                ) for c in ready_cauldrons
            ]
            
            dish_select = ui.Select(
                placeholder="받을 요리를 모두 선택하세요...",
                options=options,
                custom_id="cooking_panel:select_dishes_to_claim",
                max_values=len(options),
                row=3
            )
            dish_select.callback = self.on_dish_select
            self.add_item(dish_select)
            
            claim_button = ui.Button(
                label="선택한 요리 모두 받기",
                style=discord.ButtonStyle.success,
                emoji="🎁",
                custom_id="cooking_panel:claim_selected",
                disabled=not self.selected_dishes_to_claim,
                row=4
            )
            claim_button.callback = self.dispatch_button_callback
            self.add_item(claim_button)
        
        for child in self.children:
            if isinstance(child, ui.Button):
                child.callback = self.dispatch_button_callback
    
    async def dispatch_button_callback(self, interaction: discord.Interaction):
        custom_id = interaction.data['custom_id']
        action = custom_id.split(':')[-1]

        method_map = {
            "add_ingredient": self.add_ingredient_prompt,
            "clear_ingredients": self.clear_ingredients,
            "start_cooking": self.start_cooking,
            "claim_selected": self.claim_selected_dishes,
        }
        if method := method_map.get(action):
            await method(interaction)

    async def on_cauldron_select(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        slot = int(interaction.data['values'][0])
        is_installed = any(c['slot_number'] == slot for c in self.cauldrons)
        if not is_installed:
            await supabase.table('cauldrons').insert({'user_id': str(self.user.id), 'slot_number': slot, 'state': 'idle'}).execute()
        
        await supabase.table('user_settings').update({'kitchen_selected_slot': slot}).eq('user_id', str(self.user.id)).execute()
        self.selected_cauldron_slot = slot
        await self.refresh(interaction)
    
    async def on_dish_select(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.selected_dishes_to_claim = interaction.data.get('values', [])
        await self.refresh(interaction)

    async def add_ingredient_prompt(self, interaction: discord.Interaction):
        cauldron = self.get_selected_cauldron()
        if not cauldron or cauldron['state'] not in ['idle', 'adding_ingredients']:
            await interaction.response.send_message("❌ 지금은 재료를 추가할 수 없습니다.", ephemeral=True, delete_after=5)
            return
        
        await interaction.response.defer(ephemeral=True)
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
        await interaction.response.defer()
        cauldron = self.get_selected_cauldron()
        await supabase.table('cauldrons').update({'state': 'idle', 'current_ingredients': None}).eq('id', cauldron['id']).execute()
        await self.refresh(interaction)
        
    async def start_cooking(self, interaction: discord.Interaction):
        await interaction.response.defer()
        cauldron = self.get_selected_cauldron()
        ingredients = cauldron.get('current_ingredients') or {}
        
        total_ingredients_count = sum(ingredients.values())
        xp_earned = total_ingredients_count * XP_PER_INGREDIENT

        res = await supabase.table('recipes').select('*').execute()
        recipes = res.data if res.data else []
        
        matched_recipe = next((r for r in recipes if r.get('ingredients') == ingredients), None)
        
        now = datetime.now(timezone.utc)
        cook_time_minutes = matched_recipe['cook_time_minutes'] if matched_recipe else DEFAULT_COOK_TIME_MINUTES
        cook_time = timedelta(minutes=int(cook_time_minutes))
        
        user_abilities = await get_user_abilities(self.user.id)
        if 'cook_time_down_1' in user_abilities:
            cook_time *= 0.9

        result_item = matched_recipe['result_item_name'] if matched_recipe else FAILED_DISH_NAME
        completes_at = now + cook_time
        
        try:
            ingredients_consumed = True
            if 'cook_ingredient_saver_1' in user_abilities and random.random() < 0.15:
                ingredients_consumed = False

            if ingredients_consumed:
                tasks_to_run = []
                for name, qty in ingredients.items(): 
                    tasks_to_run.append(update_inventory(self.user.id, name, -qty))
                if tasks_to_run: await asyncio.gather(*tasks_to_run)
            else:
                # ▼▼▼ [핵심 수정] delete_after를 제거하고 헬퍼 함수를 사용하도록 변경합니다. ▼▼▼
                msg = await interaction.followup.send("✨ **알뜰한 손맛** 능력 발동! 재료를 소모하지 않았습니다!", ephemeral=True)
                self.cog.bot.loop.create_task(delete_after(msg, 10))
                # ▲▲▲ [핵심 수정] ▲▲▲

            await supabase.table('cauldrons').update({
                'state': 'cooking', 'cooking_started_at': now.isoformat(),
                'cooking_completes_at': completes_at.isoformat(), 'result_item_name': result_item
            }).eq('id', cauldron['id']).execute()

            await log_activity(self.user.id, 'cooking', amount=total_ingredients_count, xp_earned=xp_earned)
            if xp_earned > 0:
                xp_res = await supabase.rpc('add_xp', {'p_user_id': str(self.user.id), 'p_xp_to_add': xp_earned, 'p_source': 'cooking'}).execute()
                if xp_res.data and (level_cog := self.cog.bot.get_cog("LevelSystem")):
                    await level_cog.handle_level_up_event(self.user, xp_res.data)
        except Exception as e:
            logger.error(f"요리 시작 중 오류: {e}", exc_info=True)
            await interaction.followup.send("❌ 요리를 시작하는 중 오류가 발생했습니다.", ephemeral=True)

        await self.refresh(interaction)
    
    async def claim_selected_dishes(self, interaction: discord.Interaction):
        await interaction.response.defer()

        if not self.selected_dishes_to_claim:
            # ▼▼▼ [핵심 수정] delete_after를 제거하고 헬퍼 함수를 사용하도록 변경합니다. ▼▼▼
            msg = await interaction.followup.send("❌ 받을 요리를 먼저 선택해주세요.", ephemeral=True)
            self.cog.bot.loop.create_task(delete_after(msg, 5))
            # ▲▲▲ [핵심 수정] ▲▲▲
            return

        cauldron_ids_to_process = [int(cid) for cid in self.selected_dishes_to_claim]
        
        total_claimed_items: Dict[str, int] = defaultdict(int)
        ability_messages = []
        db_tasks = []
        
        user_abilities = await get_user_abilities(self.user.id)

        for cauldron_id in cauldron_ids_to_process:
            cauldron = next((c for c in self.cauldrons if c['id'] == cauldron_id), None)
            if not cauldron: continue

            result_item_base_name = cauldron['result_item_name']
            
            quantity_to_claim = 1
            final_result_item = result_item_base_name

            if 'cook_quality_up_2' in user_abilities and random.random() < 0.10 and result_item_base_name != FAILED_DISH_NAME:
                final_result_item = f"[특상품] {result_item_base_name}"
                if "장인의 솜씨" not in ability_messages:
                    ability_messages.append("✨ **장인의 솜씨** 능력 발동! '특상품' 요리를 만들었습니다!")
            
            if 'cook_double_yield_2' in user_abilities and random.random() < 0.15:
                quantity_to_claim = 2
                if "풍성한 식탁" not in ability_messages:
                    ability_messages.append("✨ **풍성한 식탁** 능력 발동! 요리를 2개 획득했습니다!")

            total_claimed_items[final_result_item] += quantity_to_claim
            
            if result_item_base_name != FAILED_DISH_NAME:
                await self.cog.check_and_log_recipe_discovery(interaction.user, result_item_base_name, cauldron.get('current_ingredients'))

        for item, qty in total_claimed_items.items():
            db_tasks.append(update_inventory(self.user.id, item, qty))
        
        db_tasks.append(
            supabase.table('cauldrons').update({
                'state': 'idle', 'current_ingredients': None, 'cooking_started_at': None,
                'cooking_completes_at': None, 'result_item_name': None
            }).in_('id', cauldron_ids_to_process).execute()
        )
        
        await asyncio.gather(*db_tasks)
        
        claimed_summary = "\n".join([f"ㄴ {name}: {qty}개" for name, qty in total_claimed_items.items()])
        success_message = f"✅ **총 {len(cauldron_ids_to_process)}개의 요리를 받았습니다!**\n\n**획득 아이템:**\n{claimed_summary}"
        if ability_messages:
            success_message += "\n\n" + "\n".join(ability_messages)
            
        # ▼▼▼ [핵심 수정] delete_after를 제거하고 헬퍼 함수를 사용하도록 변경합니다. ▼▼▼
        msg = await interaction.followup.send(success_message, ephemeral=True)
        self.cog.bot.loop.create_task(delete_after(msg, 15))
        # ▲▲▲ [핵심 수정] ▲▲▲

        self.selected_dishes_to_claim.clear()
        await self.refresh(interaction)

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

    async def cog_load(self):
        self.currency_icon = get_config("GAME_CONFIG", {}).get("CURRENCY_ICON", "🪙")

    def cog_unload(self):
        self.check_completed_cooking.cancel()

    @tasks.loop(minutes=1)
    async def check_completed_cooking(self):
        now = datetime.now(timezone.utc)
        try:
            cauldrons_res = await supabase.table('cauldrons').select('*').eq('state', 'cooking').lte('cooking_completes_at', now.isoformat()).execute()
            if not (cauldrons_res and cauldrons_res.data): return

            completed_cauldrons = cauldrons_res.data
            user_ids_to_notify = list(set(int(c['user_id']) for c in completed_cauldrons))
            
            for cauldron in completed_cauldrons:
                await supabase.table('cauldrons').update({'state': 'ready'}).eq('id', cauldron['id']).execute()
            
            for user_id in user_ids_to_notify:
                await save_config_to_db(f"kitchen_ui_update_request_{user_id}", time.time())
                user = self.bot.get_user(user_id)
                if not user: continue
                
                user_completed_dishes = [c['result_item_name'] for c in completed_cauldrons if int(c['user_id']) == user_id]
                if not user_completed_dishes: continue
                
                dishes_str = ", ".join(f"**{name}**" for name in user_completed_dishes)

                log_channel_id = get_id("log_cooking_complete_channel_id")
                if log_channel_id and (log_channel := self.bot.get_channel(log_channel_id)):
                    embed_data = await get_embed_from_db("log_cooking_complete")
                    if embed_data:
                        embed = format_embed_from_db(embed_data, user_mention=user.mention, recipe_name=dishes_str)
                        await log_channel.send(embed=embed)
                try: 
                    await user.send(f"🍲 {dishes_str} 요리가 완성되었습니다! 부엌에서 확인해주세요.")
                except discord.Forbidden: pass
        except Exception as e:
            logger.error(f"요리 완료 확인 작업 중 오류 발생: {e}", exc_info=True)

    @check_completed_cooking.before_loop
    async def before_check_completed_cooking(self): await self.bot.wait_until_ready()

    async def process_ui_update_requests(self, user_ids: Set[int]):
        logger.info(f"[Kitchen UI] {len(user_ids)}명의 유저에 대한 UI 업데이트 처리 시작.")
        for user_id in user_ids:
            user = self.bot.get_user(user_id)
            if not user: continue
            
            settings_res = await supabase.table('user_settings').select('kitchen_thread_id, kitchen_panel_message_id').eq('user_id', str(user_id)).maybe_single().execute()
            if not (settings_res and settings_res.data and (thread_id := settings_res.data.get('kitchen_thread_id'))):
                continue
            
            if thread := self.bot.get_channel(thread_id):
                message = None
                if message_id := settings_res.data.get('kitchen_panel_message_id'):
                    try:
                        message = await thread.fetch_message(int(message_id))
                    except (discord.NotFound, discord.Forbidden):
                        pass
                
                panel_view = CookingPanelView(self, user, message)
                await panel_view.refresh()
                await asyncio.sleep(1.5)

    async def check_and_log_recipe_discovery(self, user: discord.Member, recipe_name: str, ingredients: Any):
        try:
            parsed_ingredients = {}
            if isinstance(ingredients, str):
                try:
                    parsed_ingredients = json.loads(ingredients)
                except json.JSONDecodeError:
                    return 
            elif isinstance(ingredients, dict):
                parsed_ingredients = ingredients

            res = await supabase.table('discovered_recipes').select('id').eq('recipe_name', recipe_name).limit(1).execute()
            
            if res and res.data:
                return
            
            await supabase.table('discovered_recipes').insert({
                'recipe_name': recipe_name,
                'discoverer_id': str(user.id),
                'guild_id': str(user.guild.id)
            }).execute()
            
            log_channel_id = get_id("log_recipe_discovery_channel_id")
            if not (log_channel_id and (log_channel := self.bot.get_channel(log_channel_id))):
                return

            embed_data = await get_embed_from_db("log_recipe_discovery")
            if not embed_data:
                return

            ingredients_str = "\n".join([f"ㄴ {name}: {qty}개" for name, qty in parsed_ingredients.items()])
            
            log_embed = format_embed_from_db(
                embed_data,
                user_mention=user.mention,
                recipe_name=recipe_name,
                ingredients_str=ingredients_str
            )

            if user.display_avatar:
                log_embed.set_thumbnail(url=user.display_avatar.url)
            
            await log_channel.send(embed=log_embed)
        except Exception as e:
            logger.error(f"레시피 발견 처리 중 오류: {e}", exc_info=True)

    async def register_persistent_views(self):
        self.bot.add_view(CookingCreationPanelView(self))
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
        user = interaction.user
        try:
            res = await supabase.table('user_settings').select('kitchen_thread_id').eq('user_id', str(user.id)).maybe_single().execute()
            thread_id = res.data.get('kitchen_thread_id') if res and res.data else None
        except Exception as e:
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
            await delete_config_from_db(f"kitchen_state_{user.id}")
            await supabase.table('user_settings').upsert({'user_id': str(user.id), 'kitchen_thread_id': thread.id}).execute()
            
            embed_data = await get_embed_from_db("cooking_thread_welcome")
            if embed_data: await thread.send(embed=format_embed_from_db(embed_data, user_name=user.display_name))

            panel_view = CookingPanelView(self, user)
            message = await thread.send("부엌 로딩 중...")
            panel_view.message = message
            
            await supabase.table('user_settings').update({'kitchen_panel_message_id': message.id}).eq('user_id', str(user.id)).execute()
            
            await panel_view.refresh()

            await interaction.followup.send(f"✅ 당신만의 부엌을 만들었습니다! {thread.mention} 채널을 확인해주세요.", ephemeral=True)

        except Exception as e:
            logger.error(f"부엌 생성 중 오류: {e}", exc_info=True)
            await interaction.followup.send("❌ 부엌을 만드는 중 오류가 발생했습니다.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Cooking(bot))
