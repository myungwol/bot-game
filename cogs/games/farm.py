import discord
from discord.ext import commands, tasks
from discord import ui
import logging
from typing import Optional, Dict, List

from utils.database import (
    get_farm_data, create_farm, get_config,
    get_panel_components_from_db, save_panel_id, get_panel_id, get_embed_from_db,
    supabase
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

# 농장 이름 변경을 위한 모달
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

        # 1. DB 업데이트
        await supabase.table('farms').update({'name': name_to_set}).eq('id', self.farm_data['id']).execute()
        
        # 2. 스레드 이름 변경
        try:
            # interaction.channel은 스레드를 가리킵니다.
            if isinstance(interaction.channel, discord.Thread):
                await interaction.channel.edit(name=f"🌱｜{name_to_set}")
        except Exception as e:
            logger.error(f"농장 스레드 이름 변경 실패: {e}")

        # 3. 농장 UI 업데이트
        await self.cog.update_farm_ui(interaction.channel, interaction.user)

        await interaction.followup.send(f"✅ 農場の名前を「{name_to_set}」に変更しました。", ephemeral=True)

# 농장 공유 설정을 위한 View
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
        # TODO: 선택된 유저에게 권한을 부여하는 DB 로직 추가
        selected_users = ", ".join(user.mention for user in select.values)
        await interaction.response.send_message(f"{selected_users} に農場の編集権限を付与しました。", ephemeral=True)
        try:
            await self.original_interaction.edit_original_response(content="共有設定が完了しました。", view=None)
        except discord.NotFound:
            pass

# 농장 내부 UI
class FarmUIView(ui.View):
    def __init__(self, cog_instance: 'Farm', farm_data: Dict):
        super().__init__(timeout=None)
        self.cog = cog_instance
        self.farm_data = farm_data

    @ui.button(label="畑を耕す", style=discord.ButtonStyle.secondary, emoji="🪓", row=0)
    async def till_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message("現在、畑を耕す機能を開発中です。", ephemeral=True)

    @ui.button(label="種を植える", style=discord.ButtonStyle.success, emoji="🌱", row=0)
    async def plant_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message("現在、種を植える機能を開発中です。", ephemeral=True)

    @ui.button(label="水をやる", style=discord.ButtonStyle.primary, emoji="💧", row=0)
    async def water_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message("現在、水をやる機能を開発中です。", ephemeral=True)

    @ui.button(label="収穫する", style=discord.ButtonStyle.success, emoji="🧺", row=0)
    async def harvest_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message("現在、収穫機能を開発中です。", ephemeral=True)
        
    @ui.button(label="農場を公開", style=discord.ButtonStyle.grey, emoji="📢", row=1)
    async def publish_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        updated_farm_data = await get_farm_data(interaction.user.id)
        if not updated_farm_data:
            return await interaction.followup.send("❌ 農場データが見つかりませんでした。", ephemeral=True, delete_after=5)
        self.farm_data = updated_farm_data
        
        farm_embed = self.cog.build_farm_embed(self.farm_data, interaction.user)
        farm_embed.description = f"{interaction.user.mention}さんの農場です！"
        
        await interaction.channel.send(embed=farm_embed)
        await interaction.followup.send("✅ 農場の様子をチャンネルに公開しました。", ephemeral=True, delete_after=5)

    @ui.button(label="友達と共有", style=discord.ButtonStyle.grey, emoji="🤝", row=1)
    async def share_button(self, interaction: discord.Interaction, button: ui.Button):
        view = FarmShareSettingsView(interaction)
        await interaction.response.send_message("誰と農場を共有しますか？下のメニューから選択してください。", view=view, ephemeral=True)

    @ui.button(label="名前を変更", style=discord.ButtonStyle.grey, emoji="✏️", row=1)
    async def rename_button(self, interaction: discord.Interaction, button: ui.Button):
        updated_farm_data = await get_farm_data(interaction.user.id)
        if not updated_farm_data:
             return await interaction.response.send_message("❌ 農場データが見つかりませんでした。", ephemeral=True, delete_after=5)
        self.farm_data = updated_farm_data

        modal = FarmNameModal(self.cog, self.farm_data)
        await interaction.response.send_modal(modal)

# 농장 생성 패널의 View
class FarmCreationPanelView(ui.View):
    def __init__(self, cog_instance: 'Farm'):
        super().__init__(timeout=None)
        self.cog = cog_instance

    async def setup_buttons(self):
        self.clear_items()
        components = await get_panel_components_from_db("panel_farm_creation")
        for button_info in components:
            button = ui.Button(
                label=button_info.get('label'), style=discord.ButtonStyle.success,
                emoji=button_info.get('emoji'), custom_id=button_info.get('component_key')
            )
            button.callback = self.create_farm_callback
            self.add_item(button)

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
                    await thread.send(f"{user.mention}さんが農場にやってきました！")
                except discord.Forbidden:
                    await thread.add_user(user)
                    await thread.send(f"ようこそ、{user.mention}さん！")
            else:
                await self.cog.create_new_farm_thread(interaction, user)
        else:
            await self.cog.create_new_farm_thread(interaction, user)


# 메인 Farm Cog
class Farm(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def build_farm_embed(self, farm_data: Dict, user: discord.User) -> discord.Embed:
        size_x = farm_data.get('size_x', 1)
        size_y = farm_data.get('size_y', 1)
        plots = farm_data.get('farm_plots', [])
        
        sorted_plots = {(p['pos_x'], p['pos_y']): p for p in plots}

        farm_grid = []
        for y in range(size_y):
            row = []
            for x in range(size_x):
                plot = sorted_plots.get((x, y))
                if not plot:
                    row.append('❓')
                    continue
                state = plot['state']
                if state == 'default': row.append('🟤')
                elif state == 'tilled': row.append('🟫')
                else: row.append('🌱')
            farm_grid.append(" ".join(row))
        
        farm_str = "\n".join(farm_grid)
        
        farm_name = farm_data.get('name') or user.display_name
        
        embed = discord.Embed(title=f"🌱｜{farm_name}の農場", description="畑を耕し、作物を育てましょう！", color=0x8BC34A)
        embed.add_field(name="農場の様子", value=farm_str, inline=False)
        return embed

    async def update_farm_ui(self, thread: discord.Thread, user: discord.User):
        farm_data = await get_farm_data(user.id)
        if not farm_data:
            return

        async for message in thread.history(limit=50):
            if message.author.id == self.bot.user.id and message.components:
                view_labels = [c.label for c in message.components[0].children if isinstance(c, ui.Button)]
                if "畑を耕す" in view_labels:
                    embed = self.build_farm_embed(farm_data, user)
                    view = FarmUIView(self, farm_data)
                    await message.edit(embed=embed, view=view)
                    return

    async def register_persistent_views(self):
        view = FarmCreationPanelView(self)
        await view.setup_buttons()
        self.bot.add_view(view)

    async def create_new_farm_thread(self, interaction: discord.Interaction, user: discord.Member):
        try:
            panel_channel = interaction.channel
            
            farm_data_pre = await get_farm_data(user.id)
            farm_name = farm_data_pre.get('name') if farm_data_pre else user.display_name
            
            thread_name = f"🌱｜{farm_name}"
            farm_thread = await panel_channel.create_thread(
                name=thread_name,
                type=discord.ChannelType.private_thread,
                invitable=False
            )
            
            farm_data = farm_data_pre or await create_farm(user.id)
            
            await supabase.table('farms').update({'thread_id': farm_thread.id}).eq('user_id', user.id).execute()
            farm_data = await get_farm_data(user.id)

            welcome_embed_data = await get_embed_from_db("farm_thread_welcome")
            if welcome_embed_data:
                final_farm_name = farm_data.get('name') or user.display_name
                welcome_embed = format_embed_from_db(welcome_embed_data, user_name=final_farm_name)
                await farm_thread.send(embed=welcome_embed)
            
            farm_embed = self.build_farm_embed(farm_data, user)
            farm_view = FarmUIView(self, farm_data)
            await farm_thread.send(embed=farm_embed, view=farm_view)
            
            await farm_thread.add_user(user)
            await interaction.followup.send(f"✅ あなただけの農場を作成しました！ {farm_thread.mention} を確認してください。", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("❌ スレッドを作成する権限がありません。サーバー管理者に確認してください。", ephemeral=True)
        except Exception as e:
            logger.error(f"농장 생성 중 오류 발생: {e}", exc_info=True)
            await interaction.followup.send("❌ 農場の作成中にエラーが発生しました。", ephemeral=True)

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_farm_creation", **kwargs):
        if panel_info := get_panel_id(panel_key):
            if (old_channel := self.bot.get_channel(panel_info['channel_id'])) and (old_message_id := panel_info.get('message_id')):
                try: await (await old_channel.fetch_message(old_message_id)).delete()
                except (discord.NotFound, discord.Forbidden): pass
        
        embed_data = await get_embed_from_db(panel_key)
        if not embed_data: return

        embed = discord.Embed.from_dict(embed_data)
        view = FarmCreationPanelView(self)
        await view.setup_buttons()
        self.bot.add_view(view)
        
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_key, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} パネルを正常に生成しました。(チャンネル: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(Farm(bot))
