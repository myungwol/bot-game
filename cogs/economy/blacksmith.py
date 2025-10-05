# cogs/games/blacksmith.py

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
    save_panel_id, get_panel_id, get_embed_from_db,
    get_id
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

def format_timedelta(delta: timedelta) -> str:
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return "完了"
    
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    parts = []
    if days > 0:
        parts.append(f"{days}日")
    if hours > 0:
        parts.append(f"{hours}時間")
    if minutes > 0:
        parts.append(f"{minutes}分")
    if seconds > 0:
        parts.append(f"{seconds}秒")
        
    return " ".join(parts) + " 残り" if parts else "まもなく完了"

UPGRADE_RECIPES = {
    # 낚싯대
    "銅の釣り竿":   {"requires_tool": "木の釣り竿", "requires_items": {"銅鉱石": 50}, "requires_coins": 5000},
    "鉄の釣り竿":     {"requires_tool": "銅の釣り竿", "requires_items": {"鉄鉱石": 100}, "requires_coins": 25000},
    "金の釣り竿":      {"requires_tool": "鉄の釣り竿",   "requires_items": {"金鉱石": 150}, "requires_coins": 150000},
    "ダイヤの釣り竿":   {"requires_tool": "金の釣り竿",   "requires_items": {"ダイヤモンド": 200}, "requires_coins": 500000},
    
    # 괭이
    "銅のクワ":   {"requires_tool": "木のクワ",   "requires_items": {"銅鉱石": 50}, "requires_coins": 5000},
    "鉄のクワ":     {"requires_tool": "銅のクワ",   "requires_items": {"鉄鉱石": 100}, "requires_coins": 25000},
    "金のクワ":      {"requires_tool": "鉄のクワ",     "requires_items": {"金鉱石": 150}, "requires_coins": 150000},
    "ダイヤのクワ":   {"requires_tool": "金のクワ",     "requires_items": {"ダイヤモンド": 200}, "requires_coins": 500000},

    # 물뿌리개
    "銅のじょうろ": {"requires_tool": "木のじょうろ", "requires_items": {"銅鉱石": 50}, "requires_coins": 5000},
    "鉄のじょうろ":   {"requires_tool": "銅のじょうろ", "requires_items": {"鉄鉱石": 100}, "requires_coins": 25000},
    "金のじょうろ":    {"requires_tool": "鉄のじょうろ",   "requires_items": {"金鉱石": 150}, "requires_coins": 150000},
    "ダイヤのじょうろ": {"requires_tool": "金のじょうろ",   "requires_items": {"ダイヤモンド": 200}, "requires_coins": 500000},
    
    # 곡괭이
    "銅のツルハシ": {"requires_tool": "木のツルハシ", "requires_items": {"銅鉱石": 50}, "requires_coins": 5000},
    "鉄のツルハシ":   {"requires_tool": "銅のツルハシ", "requires_items": {"鉄鉱石": 100}, "requires_coins": 25000},
    "金のツルハシ":    {"requires_tool": "鉄のツルハシ",   "requires_items": {"金鉱石": 150}, "requires_coins": 150000},
    "ダイヤのツルハシ": {"requires_tool": "金のツルハシ",   "requires_items": {"ダイヤモンド": 200}, "requires_coins": 500000},
}

class ConfirmationView(ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=60)
        self.value = None
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("本人のみ使用できます。", ephemeral=True, delete_after=5)
            return False
        return True

    @ui.button(label="確認", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        self.value = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @ui.button(label="キャンセル", style=discord.ButtonStyle.grey)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        self.value = False
        self.stop()

class BlacksmithUpgradeView(ui.View):
    def __init__(self, user: discord.Member, cog: 'Blacksmith', tool_type: str):
        super().__init__(timeout=180)
        self.user = user
        self.cog = cog
        self.tool_type = tool_type
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
        embed = discord.Embed(title=f"🛠️ 鍛冶屋 - {self.tool_type} アップグレード", color=0x964B00)
        
        gear, wallet, inventory, upgrade_status = await asyncio.gather(
            get_user_gear(self.user),
            get_wallet(self.user.id),
            get_inventory(self.user),
            self.cog.get_user_upgrade_status(self.user.id)
        )
        
        if upgrade_status:
            completion_time = datetime.fromisoformat(upgrade_status['completion_timestamp'])
            now = datetime.now(timezone.utc)
            remaining_time = completion_time - now
            remaining_str = format_timedelta(remaining_time)

            embed.description = (
                f"現在 **{upgrade_status['target_tool_name']}** にアップグレード進行中です。\n"
                f"残り時間: **{remaining_str}**"
            )
            return embed

        gear_key_map = {"釣り竿": "rod", "ツルハシ": "pickaxe", "クワ": "hoe", "じょうろ": "watering_can"}
        current_tool = gear.get(gear_key_map.get(self.tool_type, "pickaxe"), "素手")
        
        embed.description = f"**現在の装備:** `{current_tool}`\n**所持コイン:** `{wallet.get('balance', 0):,}`{self.currency_icon}"

        possible_upgrades = {
            target: recipe for target, recipe in UPGRADE_RECIPES.items()
            if recipe['requires_tool'] == current_tool and self.tool_type in target
        }

        if not possible_upgrades:
            embed.add_field(name="アップグレード不可", value="現在装備している道具で可能なアップグレードはありません。", inline=False)
        else:
            for target, recipe in possible_upgrades.items():
                materials_list = []
                for item, qty in recipe['requires_items'].items():
                    owned = inventory.get(item, 0)
                    emoji = "✅" if owned >= qty else "❌"
                    materials_list.append(f"> {emoji} {item}: {owned}/{qty}")
                
                coin_emoji = "✅" if wallet.get('balance', 0) >= recipe['requires_coins'] else "❌"
                
                embed.add_field(
                    name=f"➡️ **{target}**",
                    value=(
                        f"**必要素材:**\n" + "\n".join(materials_list) +
                        f"\n> {coin_emoji} コイン: {wallet.get('balance', 0):,}/{recipe['requires_coins']:,}"
                    ),
                    inline=False
                )
        return embed

    def build_components(self):
        self.clear_items()
        
        back_button = ui.Button(label="戻る", style=discord.ButtonStyle.grey, custom_id="blacksmith_back")
        back_button.callback = self.on_back
        self.add_item(back_button)

        select = ui.Select(placeholder="アップグレードする道具を選択してください...")
        options = []
        for target, recipe in UPGRADE_RECIPES.items():
            if self.tool_type in target:
                options.append(discord.SelectOption(label=f"{target} にアップグレード", value=target))
        
        if options:
            select.options = options
            select.callback = self.on_upgrade_select
            self.add_item(select)

    async def on_upgrade_select(self, interaction: discord.Interaction):
        target_tool = interaction.data['values'][0]
        await self.cog.start_upgrade(interaction, target_tool)
        
    async def on_back(self, interaction: discord.Interaction):
        tool_select_view = BlacksmithToolSelectView(self.user, self.cog)
        await tool_select_view.start(interaction)

class BlacksmithToolSelectView(ui.View):
    def __init__(self, user: discord.Member, cog: 'Blacksmith'):
        super().__init__(timeout=180)
        self.user = user
        self.cog = cog

    async def start(self, interaction: discord.Interaction):
        embed = discord.Embed(title="🛠️ 鍛冶屋", description="アップグレードする道具の種類を選択してください。", color=0x964B00)
        
        upgrade_status = await self.cog.get_user_upgrade_status(self.user.id)
        
        if upgrade_status:
            completion_time = datetime.fromisoformat(upgrade_status['completion_timestamp'])
            now = datetime.now(timezone.utc)
            remaining_time = completion_time - now
            remaining_str = format_timedelta(remaining_time)
            
            embed.description = (
                f"現在 **{upgrade_status['target_tool_name']}** にアップグレード進行中です。\n"
                f"残り時間: **{remaining_str}**"
            )
        
        self.build_components(upgrade_status is not None)
        
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.response.send_message(embed=embed, view=self, ephemeral=True)
            
    def build_components(self, is_upgrading: bool):
        self.clear_items()
        tool_types = [
            {"label": "釣り竿", "emoji": "🎣", "value": "釣り竿"},
            {"label": "クワ", "emoji": "🪓", "value": "クワ"},
            {"label": "じょうろ", "emoji": "💧", "value": "じょうろ"},
            {"label": "ツルハシ", "emoji": "⛏️", "value": "ツルハシ"}
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

    @ui.button(label="鍛冶屋を利用する", style=discord.ButtonStyle.secondary, emoji="🛠️", custom_id="enter_blacksmith")
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
            ids_to_delete = [item['id'] for item in completed_upgrades]

            for upgrade in completed_upgrades:
                user_id = int(upgrade['user_id'])
                target_tool = upgrade['target_tool_name']
                
                user = self.bot.get_user(user_id)
                if not user:
                    logger.warning(f"업그레이드 완료 처리 중 유저(ID: {user_id})를 찾을 수 없습니다.")
                    continue

                await update_inventory(user_id, target_tool, 1)
                
                log_channel_id = get_id("log_blacksmith_channel_id")
                if log_channel_id and (log_channel := self.bot.get_channel(log_channel_id)):
                    try:
                        embed_data = await get_embed_from_db("log_blacksmith_complete")
                        if embed_data:
                            log_embed = format_embed_from_db(embed_data, user_mention=user.mention, tool_name=target_tool)
                            await log_channel.send(embed=log_embed)
                    except Exception as e:
                        logger.error(f"대장간 완료 로그 채널 메시지 전송 실패: {e}", exc_info=True)

                try:
                    await user.send(f"🎉 **{target_tool}** のアップグレードが完了しました！インベントリを確認してください。")
                except discord.Forbidden:
                    logger.warning(f"유저(ID: {user_id})에게 DM을 보낼 수 없습니다.")
            
            if ids_to_delete:
                await supabase.table('blacksmith_upgrades').delete().in_('id', ids_to_delete).execute()

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
            return await interaction.response.send_message("❌ 無効なアップグレード情報です。", ephemeral=True, delete_after=5)
            
        user_id = interaction.user.id
        
        if await self.get_user_upgrade_status(user_id):
            await interaction.response.send_message("❌ すでに他の道具をアップグレード中です。", ephemeral=True, delete_after=5)
            tool_type = next((tt for tt in ["釣り竿", "クワ", "じょうろ", "ツルハシ"] if tt in target_tool), None)
            if tool_type:
                current_view = BlacksmithUpgradeView(interaction.user, self, tool_type)
                await current_view.start(interaction)
            return

        gear, wallet, inventory = await asyncio.gather(
            get_user_gear(interaction.user),
            get_wallet(user_id),
            get_inventory(interaction.user)
        )

        gear_key_map = {"釣り竿": "rod", "ツルハシ": "pickaxe", "クワ": "hoe", "じょうろ": "watering_can"}
        
        sorted_tool_types = sorted(gear_key_map.keys(), key=len, reverse=True)
        gear_key = None
        for tool_type in sorted_tool_types:
            if tool_type in target_tool:
                gear_key = gear_key_map[tool_type]
                break
        
        if not gear_key or gear.get(gear_key) != recipe['requires_tool']:
            return await interaction.response.send_message(f"❌ このアップグレードを行うには、まず**{recipe['requires_tool']}**を装備する必要があります。", ephemeral=True, delete_after=10)

        for item, qty in recipe['requires_items'].items():
            if inventory.get(item, 0) < qty:
                return await interaction.response.send_message(f"❌ 素材が不足しています: {item} {qty}個必要", ephemeral=True, delete_after=5)
        
        if wallet.get('balance', 0) < recipe['requires_coins']:
            return await interaction.response.send_message("❌ コインが不足しています。", ephemeral=True, delete_after=5)
            
        view = ConfirmationView(user_id)
        await interaction.response.send_message(f"**{target_tool}**にアップグレードを開始しますか？\n"
                                                f"**消費素材:** {recipe['requires_tool']}, {', '.join([f'{k} {v}個' for k,v in recipe['requires_items'].items()])}, {recipe['requires_coins']:,} コイン\n"
                                                f"**所要時間:** 24時間\n\n**注意: 一度開始するとキャンセルできず、使用した素材と道具は即時消費されます。**",
                                                view=view, ephemeral=True)
        await view.wait()

        if view.value is not True:
            return await interaction.edit_original_response(content="アップグレードがキャンセルされました。", view=None)

        try:
            tasks = [
                update_wallet(interaction.user, -recipe['requires_coins']),
                set_user_gear(user_id, **{gear_key: "素手"}),
                update_inventory(user_id, recipe['requires_tool'], -1)
            ]
            for item, qty in recipe['requires_items'].items():
                tasks.append(update_inventory(user_id, item, -qty))
            
            await asyncio.gather(*tasks)

            completion_time = datetime.now(timezone.utc) + timedelta(hours=24)
            await supabase.table('blacksmith_upgrades').insert({
                "user_id": str(user_id),
                "target_tool_name": target_tool,
                "completion_timestamp": completion_time.isoformat()
            }).execute()
            
            await interaction.edit_original_response(content="✅ アップグレードを開始しました！24時間後に完了します。", view=None)
            
            final_view = BlacksmithToolSelectView(interaction.user, self)
            await final_view.start(interaction)

        except Exception as e:
            logger.error(f"업그레이드 시작 중 DB 오류: {e}", exc_info=True)
            await interaction.edit_original_response(content="❌ アップグレードの開始中にエラーが発生しました。素材が消費された可能性がありますので、管理者に問い合わせてください。", view=None)
    
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
        logger.info(f"✅ {panel_key} パネルを正常に生成しました。(チャンネル: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(Blacksmith(bot))
