import discord
from discord.ext import commands, tasks
from discord import ui
import logging
from typing import Optional, Dict, List
import asyncio

from utils.database import (
    get_farm_data, create_farm, get_config,
    save_panel_id, get_panel_id, get_embed_from_db,
    supabase, get_inventory, get_user_gear, update_plot
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

# --- 농장 UI 및 상호작용 관련 클래스 ---

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
        msg = await interaction.followup.send(f"✅ 農場の名前を「{name_to_set}」に変更しました。", ephemeral=True)
        await asyncio.sleep(10)
        try: await msg.delete()
        except discord.NotFound: pass


class FarmShareSettingsView(ui.View):
    def __init__(self, original_interaction: discord.Interaction):
        super().__init__(timeout=180)
        self.original_interaction = original_interaction

    @ui.select(
        cls=ui.UserSelect,
        placeholder="畑仕事を手伝ってもらう友達を選択...",
        max_values=5
    )
    async def user_select(self, interaction: discord.Interaction, select: ui.UserSelect):
        selected_users = ", ".join(user.mention for user in select.values)
        await interaction.response.send_message(f"{selected_users} に農場の編集権限を付与しました。", ephemeral=True, delete_after=10)
        try:
            await self.original_interaction.edit_original_response(content="共有設定が完了しました。", view=None)
        except discord.NotFound:
            pass

class FarmUIView(ui.View):
    def __init__(self, cog_instance: 'Farm', farm_data: Dict):
        super().__init__(timeout=None)
        self.cog = cog_instance
        self.farm_data = farm_data
        
        self.add_item(ui.Button(label="畑を耕す", style=discord.ButtonStyle.secondary, emoji="🪓", row=0, custom_id="farm_till"))
        self.add_item(ui.Button(label="種を植える", style=discord.ButtonStyle.success, emoji="🌱", row=0, custom_id="farm_plant"))
        self.add_item(ui.Button(label="水をやる", style=discord.ButtonStyle.primary, emoji="💧", row=0, custom_id="farm_water"))
        self.add_item(ui.Button(label="収穫する", style=discord.ButtonStyle.success, emoji="🧺", row=0, custom_id="farm_harvest"))
        self.add_item(ui.Button(label="農場に招待", style=discord.ButtonStyle.grey, emoji="📢", row=1, custom_id="farm_invite"))
        self.add_item(ui.Button(label="権限を付与", style=discord.ButtonStyle.grey, emoji="🤝", row=1, custom_id="farm_share"))
        self.add_item(ui.Button(label="名前を変更", style=discord.ButtonStyle.grey, emoji="✏️", row=1, custom_id="farm_rename"))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        is_owner = interaction.user.id == self.farm_data.get('user_id')
        if not is_owner:
            await interaction.response.send_message("❌ 農場の所有者のみ操作できます。", ephemeral=True, delete_after=5)
        return is_owner

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: ui.Item) -> None:
        logger.error(f"FarmUIView에서 오류 발생 (item: {item.custom_id}): {error}", exc_info=True)
        if interaction.response.is_done():
            await interaction.followup.send("❌ 処理中に予期せぬエラーが発生しました。", ephemeral=True, delete_after=10)
        else:
            await interaction.response.send_message("❌ 処理中に予期せぬエラーが発生しました。", ephemeral=True, delete_after=10)

    # 각 버튼의 콜백을 버튼 정의 시점에 직접 연결하도록 변경 (중앙 콜백 제거)
    # 이는 각 버튼의 로직이 명확해지고, interaction_check를 통과한 후 실행됨을 보장합니다.

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
            await interaction.followup.send("❌ このコマンドはテキストチャンネルでのみ使用できます。", ephemeral=True)
            return

        if farm_data:
            farm_thread_id = farm_data.get('thread_id')
            if farm_thread_id and (thread := self.cog.bot.get_channel(farm_thread_id)):
                await interaction.followup.send(f"✅ あなたの農場はこちらです: {thread.mention}", ephemeral=True)
                try:
                    await thread.send(f"{user.mention}さんが農場にやってきました！", delete_after=10)
                except discord.Forbidden:
                    await thread.add_user(user)
                    await thread.send(f"ようこそ、{user.mention}さん！", delete_after=10)
            else:
                await self.cog.create_new_farm_thread(interaction, user)
        else:
            await self.cog.create_new_farm_thread(interaction, user)

class Farm(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.thread_locks: Dict[int, asyncio.Lock] = {}

    def build_farm_embed(self, farm_data: Dict, user: discord.User) -> discord.Embed:
        size_x = farm_data.get('size_x', 1)
        size_y = farm_data.get('size_y', 1)
        plots = farm_data.get('farm_plots', [])
        sorted_plots = {(p['pos_x'], p['pos_y']): p for p in plots}
        zwsp = "\u200b"
        farm_grid = []
        for y in range(size_y):
            row = []
            for x in range(size_x):
                plot = sorted_plots.get((x, y))
                state = plot['state'] if plot else '❓'
                if state == 'default': row.append('🟤')
                elif state == 'tilled': row.append('🟫')
                else: row.append('🌱')
            farm_grid.append(" ".join(row))
        farm_str = f"```\n{zwsp}\n" + f"\n{zwsp}\n".join(farm_grid) + f"\n{zwsp}\n```"
        farm_name = farm_data.get('name') or user.display_name
        embed = discord.Embed(title=f"**{farm_name}の農場**", color=0x8BC34A)
        embed.description = f"> 畑を耕し、作物を育てましょう！"
        embed.add_field(name="**━━━━━━━━[ 農場の様子 ]━━━━━━━━**", value=farm_str, inline=False)
        return embed

    async def update_farm_ui(self, thread: discord.Thread, user: discord.User, interaction: Optional[discord.Interaction] = None):
        lock = self.thread_locks.setdefault(thread.id, asyncio.Lock())
        async with lock:
            farm_data = await get_farm_data(user.id)
            if not farm_data or not farm_data.get("farm_message_id"):
                if interaction: await interaction.followup.send("❌ 農場UIメッセージが見つかりません。", ephemeral=True)
                return

            try:
                farm_message = await thread.fetch_message(farm_data["farm_message_id"])
                embed = self.build_farm_embed(farm_data, user)
                view = FarmUIView(self, farm_data)
                await farm_message.edit(embed=embed, view=view)
            except (discord.NotFound, discord.Forbidden):
                if interaction: await interaction.followup.send("❌ 農場UIメッセージの更新に失敗しました。", ephemeral=True)
            except Exception as e:
                logger.error(f"농장 UI 업데이트 중 오류: {e}")
                if interaction: await interaction.followup.send("❌ エラーが発生しました。", ephemeral=True)

    async def register_persistent_views(self):
        self.bot.add_view(FarmCreationPanelView(self))
        # FarmUIView는 동적으로 생성되므로, 여기서 직접 등록하지 않고 on_ready 등에서 메시지를 찾아 다시 연결하는 방법도 고려할 수 있음
        # 하지만 현재 구조에서는 UI 업데이트 시 새로 View를 생성하므로 괜찮음

    async def create_new_farm_thread(self, interaction: discord.Interaction, user: discord.Member):
        try:
            panel_channel = interaction.channel
            farm_name = f"{user.display_name}の農場"
            
            farm_thread = await panel_channel.create_thread(
                name=f"🌱｜{farm_name}",
                type=discord.ChannelType.private_thread,
                slowmode_delay=21600
            )
            
            await farm_thread.send(f"ようこそ、{user.mention}さん！このスレッドの管理権限を設定しています…", delete_after=10)

            farm_data = await get_farm_data(user.id) or await create_farm(user.id)
            await supabase.table('farms').update({'thread_id': farm_thread.id}).eq('user_id', user.id).execute()
            farm_data = await get_farm_data(user.id)

            welcome_embed_data = await get_embed_from_db("farm_thread_welcome")
            if welcome_embed_data:
                final_farm_name = farm_data.get('name') or user.display_name
                welcome_embed = format_embed_from_db(welcome_embed_data, user_name=final_farm_name)
                await farm_thread.send(embed=welcome_embed)
            
            farm_embed = self.build_farm_embed(farm_data, user)
            farm_view = FarmUIView(self, farm_data)
            farm_message = await farm_thread.send(embed=farm_embed, view=farm_view)
            
            await supabase.table('farms').update({'farm_message_id': farm_message.id}).eq('id', farm_data['id']).execute()

            await farm_thread.add_user(user)
            await interaction.followup.send(f"✅ あなただけの農場を作成しました！ {farm_thread.mention} を確認してください。", ephemeral=True)
        except Exception as e:
            logger.error(f"농장 생성 중 오류 발생: {e}", exc_info=True)
            await interaction.followup.send("❌ 農場の作成中にエラーが発生しました。", ephemeral=True)

    async def handle_farm_till(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user = interaction.user
        gear = await get_user_gear(str(user.id))
        equipped_hoe = gear.get('hoe', BARE_HANDS)

        if equipped_hoe == BARE_HANDS:
            return await interaction.followup.send("❌ まずは商店で「クワ」を購入して、プロフィール画面から装備してください。", ephemeral=True)

        farm_data = await get_farm_data(user.id)
        
        hoe_power = {'古いクワ': 1, '一般のクワ': 2, '中級のクワ': 4, '高級クワ': 8, '伝説のクワ': 16}.get(equipped_hoe, 0)
        
        tilled_count = 0
        plots_to_update = []
        for plot in farm_data.get('farm_plots', []):
            if plot['state'] == 'default' and tilled_count < hoe_power:
                plots_to_update.append(update_plot(plot['id'], {'state': 'tilled'}))
                tilled_count += 1
        
        if not plots_to_update:
            return await interaction.followup.send("ℹ️ これ以上耕せる畑がありません。", ephemeral=True, delete_after=10)
        
        await asyncio.gather(*plots_to_update)
        
        msg = await interaction.followup.send(f"✅ **{equipped_hoe}** を使って、畑を**{tilled_count}マス**耕しました。", ephemeral=True)
        await self.update_farm_ui(interaction.channel, user, interaction)
        await asyncio.sleep(10)
        try: await msg.delete()
        except discord.NotFound: pass

    async def handle_farm_plant(self, interaction: discord.Interaction):
        await interaction.response.send_message("現在、種を植える機能を開発中です。", ephemeral=True, delete_after=10)
    
    async def handle_farm_water(self, interaction: discord.Interaction):
        await interaction.response.send_message("現在、水をやる機能を開発中です。", ephemeral=True, delete_after=10)

    async def handle_farm_harvest(self, interaction: discord.Interaction):
        await interaction.response.send_message("現在、収穫機能を開発中です。", ephemeral=True, delete_after=10)
        
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
        view = FarmShareSettingsView(interaction)
        await interaction.response.send_message("誰に農場の権限を付与しますか？下のメニューから選択してください。", view=view, ephemeral=True)

    async def handle_farm_rename(self, interaction: discord.Interaction):
        farm_data = await get_farm_data(interaction.user.id)
        if not farm_data:
             return await interaction.response.send_message("❌ 農場データが見つかりませんでした。", ephemeral=True, delete_after=5)
        modal = FarmNameModal(self, farm_data)
        await interaction.response.send_modal(modal)

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_farm_creation", **kwargs):
        if panel_info := get_panel_id(panel_key):
            if (old_channel := self.bot.get_channel(panel_info['channel_id'])) and (old_message_id := panel_info.get('message_id')):
                try: await (await old_channel.fetch_message(old_message_id)).delete()
                except (discord.NotFound, discord.Forbidden): pass
        
        embed_data = await get_embed_from_db(panel_key)
        if not embed_data: return

        embed = discord.Embed.from_dict(embed_data)
        view = FarmCreationPanelView(self)
        
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_key, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} パネルを正常に生成しました。(チャンネル: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(Farm(bot))
