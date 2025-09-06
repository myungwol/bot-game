# cogs/economy/blacksmith.py

import discord
from discord.ext import commands, tasks
from discord import ui
import logging
import asyncio
from typing import Optional, Dict
from datetime import datetime, timezone, timedelta

from utils.database import (
    get_inventory, update_inventory, get_user_gear, set_user_gear,
    get_wallet, update_wallet, supabase, get_config,
    save_panel_id, get_panel_id, get_embed_from_db
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

# 업그레이드 레시피 정의
UPGRADE_RECIPES = {
    "구리 곡괭이": {"requires_tool": "나무 곡괭이", "requires_items": {"구리 광석": 25}, "requires_coins": 2500},
    "철 곡괭이":   {"requires_tool": "구리 곡괭이", "requires_items": {"철 광석": 50}, "requires_coins": 10000},
    "금 곡괭이":    {"requires_tool": "철 곡괭이",   "requires_items": {"금 광석": 75}, "requires_coins": 50000},
    "다이아 곡괭이": {"requires_tool": "금 곡괭이",   "requires_items": {"다이아몬드": 100}, "requires_coins": 200000},
    
    "구리 괭이":   {"requires_tool": "나무 괭이",   "requires_items": {"구리 광석": 25}, "requires_coins": 2500},
    "철 괭이":     {"requires_tool": "구리 괭이",   "requires_items": {"철 광석": 50}, "requires_coins": 10000},
    "금 괭이":      {"requires_tool": "철 괭이",     "requires_items": {"금 광석": 75}, "requires_coins": 50000},
    "다이아 괭이":   {"requires_tool": "금 괭이",     "requires_items": {"다이아몬드": 100}, "requires_coins": 200000},

    "구리 물뿌리개": {"requires_tool": "나무 물뿌리개", "requires_items": {"구리 광석": 25}, "requires_coins": 2500},
    "철 물뿌리개":   {"requires_tool": "구리 물뿌리개", "requires_items": {"철 광석": 50}, "requires_coins": 10000},
    "금 물뿌리개":    {"requires_tool": "철 물뿌리개",   "requires_items": {"금 광석": 75}, "requires_coins": 50000},
    "다이아 물뿌리개": {"requires_tool": "금 물뿌리개",   "requires_items": {"다이아몬드": 100}, "requires_coins": 200000},
}

class ConfirmationView(ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=60)
        self.value = None
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("본인만 사용할 수 있습니다.", ephemeral=True, delete_after=5)
            return False
        return True

    @ui.button(label="확인", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        self.value = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @ui.button(label="취소", style=discord.ButtonStyle.grey)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        self.value = False
        self.stop()

class BlacksmithUpgradeView(ui.View):
    def __init__(self, user: discord.Member, cog: 'Blacksmith', tool_type: str):
        super().__init__(timeout=180)
        self.user = user
        self.cog = cog
        self.tool_type = tool_type # "곡괭이", "괭이", "물뿌리개"
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")

    async def start(self, interaction: discord.Interaction):
        await self.update_view(interaction)

    async def update_view(self, interaction: discord.Interaction):
        embed = await self.build_embed()
        self.build_components()
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.response.send_message(embed=embed, view=self, ephemeral=True)

    async def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title=f"🛠️ 대장간 - {self.tool_type} 업그레이드", color=0x964B00)
        
        gear, wallet, inventory, upgrade_status = await asyncio.gather(
            get_user_gear(self.user),
            get_wallet(self.user.id),
            get_inventory(self.user),
            self.cog.get_user_upgrade_status(self.user.id)
        )
        
        if upgrade_status:
            embed.description = (
                f"현재 **{upgrade_status['target_tool_name']}**(으)로 업그레이드 진행 중입니다.\n"
                f"완료 시간: {discord.utils.format_dt(datetime.fromisoformat(upgrade_status['completion_timestamp']), style='R')}"
            )
            return embed

        gear_key_map = {"곡괭이": "pickaxe", "괭이": "hoe", "물뿌리개": "watering_can"}
        current_tool = gear.get(gear_key_map.get(self.tool_type, "pickaxe"), "맨손")
        
        embed.description = f"**현재 장착 도구:** `{current_tool}`\n**보유 코인:** `{wallet.get('balance', 0):,}`{self.currency_icon}"

        possible_upgrades = {
            target: recipe for target, recipe in UPGRADE_RECIPES.items()
            if recipe['requires_tool'] == current_tool and self.tool_type in target
        }

        if not possible_upgrades:
            embed.add_field(name="업그레이드 불가", value="현재 장착된 도구로 가능한 업그레이드가 없습니다.", inline=False)
        else:
            for target, recipe in possible_upgrades.items():
                materials_list = []
                has_materials = True
                for item, qty in recipe['requires_items'].items():
                    owned = inventory.get(item, 0)
                    emoji = "✅" if owned >= qty else "❌"
                    materials_list.append(f"> {emoji} {item}: {owned}/{qty}")
                    if owned < qty: has_materials = False
                
                coin_emoji = "✅" if wallet.get('balance', 0) >= recipe['requires_coins'] else "❌"
                
                embed.add_field(
                    name=f"➡️ **{target}**",
                    value=(
                        f"**필요 재료:**\n" + "\n".join(materials_list) +
                        f"\n> {coin_emoji} 코인: {wallet.get('balance', 0):,}/{recipe['requires_coins']:,}"
                    ),
                    inline=False
                )
        return embed

    def build_components(self):
        self.clear_items()
        
        back_button = ui.Button(label="뒤로", style=discord.ButtonStyle.grey, custom_id="blacksmith_back")
        back_button.callback = self.on_back
        self.add_item(back_button)

        # 업그레이드 버튼 추가 로직
        select = ui.Select(placeholder="업그레이드할 도구를 선택하세요...")
        options = []
        for target, recipe in UPGRADE_RECIPES.items():
            if self.tool_type in target:
                options.append(discord.SelectOption(label=f"{target} (으)로 업그레이드", value=target))
        
        if options:
            select.options = options
            select.callback = self.on_upgrade_select
            self.add_item(select)

    async def on_upgrade_select(self, interaction: discord.Interaction):
        target_tool = interaction.data['values'][0]
        await self.cog.start_upgrade(interaction, target_tool)
        # Refresh the view after attempting an upgrade
        await self.update_view(interaction)


    async def on_back(self, interaction: discord.Interaction):
        tool_select_view = BlacksmithToolSelectView(self.user, self.cog)
        await tool_select_view.start(interaction)

class BlacksmithToolSelectView(ui.View):
    def __init__(self, user: discord.Member, cog: 'Blacksmith'):
        super().__init__(timeout=180)
        self.user = user
        self.cog = cog

    async def start(self, interaction: discord.Interaction):
        embed = discord.Embed(title="🛠️ 대장간", description="업그레이드할 도구의 종류를 선택해주세요.", color=0x964B00)
        
        # Check upgrade status
        upgrade_status = await self.cog.get_user_upgrade_status(self.user.id)
        if upgrade_status:
            embed.description = (
                f"현재 **{upgrade_status['target_tool_name']}**(으)로 업그레이드 진행 중입니다.\n"
                f"완료 시간: {discord.utils.format_dt(datetime.fromisoformat(upgrade_status['completion_timestamp']), style='R')}"
            )
        
        self.build_components(upgrade_status is not None)
        
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.response.send_message(embed=embed, view=self, ephemeral=True)
            
    def build_components(self, is_upgrading: bool):
        self.clear_items()
        tool_types = [
            {"label": "곡괭이", "emoji": "⛏️", "value": "곡괭이"},
            {"label": "괭이", "emoji": "🪓", "value": "괭이"},
            {"label": "물뿌리개", "emoji": "💧", "value": "물뿌리개"}
        ]
        
        for tool in tool_types:
            button = ui.Button(label=tool["label"], emoji=tool["emoji"], custom_id=f"select_tool_{tool['value']}", disabled=is_upgrading)
            button.callback = self.on_tool_select
            self.add_item(button)
            
    async def on_tool_select(self, interaction: discord.Interaction):
        tool_type = interaction.data['custom_id'].split('_')[-1]
        upgrade_view = BlacksmithUpgradeView(self.user, self.cog, tool_type)
        await upgrade_view.start(interaction)

class BlacksmithPanelView(ui.View):
    def __init__(self, cog_instance: 'Blacksmith'):
        super().__init__(timeout=None)
        self.cog = cog_instance

    @ui.button(label="대장간 이용하기", style=discord.ButtonStyle.secondary, emoji="🛠️", custom_id="enter_blacksmith")
    async def enter_blacksmith(self, interaction: discord.Interaction, button: ui.Button):
        tool_select_view = BlacksmithToolSelectView(interaction.user, self.cog)
        await tool_select_view.start(interaction)

class Blacksmith(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.check_completed_upgrades.start()

    def cog_unload(self):
        self.check_completed_upgrades.cancel()

    @tasks.loop(minutes=1)
    async def check_completed_upgrades(self):
        try:
            now = datetime.now(timezone.utc)
            response = await supabase.table('blacksmith_upgrades').select('*').lte('completion_timestamp', now.isoformat()).execute()
            
            if not (response and response.data):
                return

            completed_upgrades = response.data
            user_ids_to_delete = []

            for upgrade in completed_upgrades:
                user_id = int(upgrade['user_id'])
                target_tool = upgrade['target_tool_name']
                
                user = self.bot.get_user(user_id)
                if not user:
                    logger.warning(f"업그레이드 완료 처리 중 유저(ID: {user_id})를 찾을 수 없습니다.")
                    continue

                await update_inventory(user_id, target_tool, 1)
                
                try:
                    await user.send(f"🎉 **{target_tool}** 업그레이드가 완료되었습니다! 인벤토리를 확인해주세요.")
                except discord.Forbidden:
                    logger.warning(f"유저(ID: {user_id})에게 DM을 보낼 수 없습니다.")

                user_ids_to_delete.append(user_id)
            
            if user_ids_to_delete:
                await supabase.table('blacksmith_upgrades').delete().in_('user_id', [str(uid) for uid in user_ids_to_delete]).execute()

        except Exception as e:
            logger.error(f"완료된 업그레이드 확인 중 오류: {e}", exc_info=True)

    @check_completed_upgrades.before_loop
    async def before_check_completed_upgrades(self):
        await self.bot.wait_until_ready()

    async def get_user_upgrade_status(self, user_id: int) -> Optional[Dict]:
        res = await supabase.table('blacksmith_upgrades').select('*').eq('user_id', str(user_id)).maybe_single().execute()
        return res.data if res and res.data else None

    async def start_upgrade(self, interaction: discord.Interaction, target_tool: str):
        recipe = UPGRADE_RECIPES.get(target_tool)
        if not recipe:
            await interaction.response.send_message("❌ 잘못된 업그레이드 정보입니다.", ephemeral=True, delete_after=5)
            return

        user_id = interaction.user.id
        
        # 1. 중복 업그레이드 확인
        if await self.get_user_upgrade_status(user_id):
            await interaction.response.send_message("❌ 이미 다른 도구를 업그레이드하는 중입니다.", ephemeral=True, delete_after=5)
            return

        # 2. 재료 및 비용 확인
        gear, wallet, inventory = await asyncio.gather(
            get_user_gear(interaction.user),
            get_wallet(user_id),
            get_inventory(interaction.user)
        )

        gear_key = next((k for k, v in {"pickaxe":"곡괭이", "hoe":"괭이", "watering_can":"물뿌리개"}.items() if v in target_tool), None)
        if not gear_key or gear.get(gear_key) != recipe['requires_tool']:
            await interaction.response.send_message(f"❌ 이 업그레이드를 하려면 먼저 **{recipe['requires_tool']}**(을)를 장착해야 합니다.", ephemeral=True, delete_after=10)
            return

        for item, qty in recipe['requires_items'].items():
            if inventory.get(item, 0) < qty:
                await interaction.response.send_message(f"❌ 재료가 부족합니다: {item} {qty}개 필요", ephemeral=True, delete_after=5)
                return
        
        if wallet.get('balance', 0) < recipe['requires_coins']:
            await interaction.response.send_message("❌ 코인이 부족합니다.", ephemeral=True, delete_after=5)
            return
            
        # 3. 확인 절차
        view = ConfirmationView(user_id)
        await interaction.response.send_message(f"**{target_tool}**(으)로 업그레이드를 시작하시겠습니까?\n"
                                                f"**소모 재료:** {recipe['requires_tool']}, {', '.join([f'{k} {v}개' for k,v in recipe['requires_items'].items()])}, {recipe['requires_coins']:,} 코인\n"
                                                f"**소요 시간:** 24시간\n\n**주의: 일단 시작하면 취소할 수 없으며, 사용한 재료와 도구는 즉시 소모됩니다.**",
                                                view=view, ephemeral=True)
        await view.wait()

        if not view.value:
            await interaction.edit_original_response(content="업그레이드가 취소되었습니다.", view=None)
            return

        # 4. 업그레이드 시작 (DB 작업)
        try:
            # 재료 소모
            tasks = [update_wallet(interaction.user, -recipe['requires_coins'])]
            for item, qty in recipe['requires_items'].items():
                tasks.append(update_inventory(user_id, item, -qty))
            
            await asyncio.gather(*tasks)
            await set_user_gear(user_id, **{gear_key: "맨손"}) # 장착 해제

            # DB에 업그레이드 기록
            completion_time = datetime.now(timezone.utc) + timedelta(hours=24)
            await supabase.table('blacksmith_upgrades').insert({
                "user_id": str(user_id),
                "target_tool_name": target_tool,
                "completion_timestamp": completion_time.isoformat()
            }).execute()

            await interaction.edit_original_response(content="✅ 업그레이드를 시작했습니다! 24시간 후에 완료됩니다.", view=None)

        except Exception as e:
            logger.error(f"업그레이드 시작 중 DB 오류: {e}", exc_info=True)
            # TODO: 재료 롤백 로직 추가 필요
            await interaction.edit_original_response(content="❌ 업그레이드를 시작하는 중 오류가 발생했습니다. 재료가 소모되었을 수 있으니 관리자에게 문의하세요.", view=None)

    async def register_persistent_views(self):
        self.bot.add_view(BlacksmithPanelView(self))
        logger.info("✅ 대장간의 영구 View가 성공적으로 등록되었습니다.")

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_blacksmith"):
        if panel_info := get_panel_id(panel_key):
            try:
                if old_channel := self.bot.get_channel(panel_info['channel_id']):
                    msg = await old_channel.fetch_message(panel_info['message_id'])
                    await msg.delete()
            except (discord.NotFound, discord.Forbidden): pass

        embed_data = await get_embed_from_db(panel_key)
        if not embed_data: return

        embed = discord.Embed.from_dict(embed_data)
        view = BlacksmithPanelView(self)

        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_key, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} 패널을 성공적으로 생성했습니다. (채널: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(Blacksmith(bot))
