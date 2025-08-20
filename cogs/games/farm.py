import discord
from discord.ext import commands, tasks
from discord import ui
import logging
from typing import Optional, Dict, List

from utils.database import (
    get_farm_data, create_farm, get_config,
    get_panel_components_from_db, save_panel_id, get_panel_id, get_embed_from_db,
    supabase # thread_id 저장을 위해 import
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

# 농장 내부 UI (밭 갈기, 씨앗 심기 등 버튼)
class FarmUIView(ui.View):
    def __init__(self, cog_instance: 'Farm', farm_data: Dict):
        super().__init__(timeout=None)
        self.cog = cog_instance
        self.farm_data = farm_data

    @ui.button(label="畑を耕す", style=discord.ButtonStyle.secondary, emoji="🪓")
    async def till_button(self, interaction: discord.Interaction, button: ui.Button):
        # TODO: 다음 단계에서 괭이 등급별 밭 갈기 로직 구현
        await interaction.response.send_message("現在、畑を耕す機能を開発中です。", ephemeral=True)

    @ui.button(label="種を植える", style=discord.ButtonStyle.success, emoji="🌱")
    async def plant_button(self, interaction: discord.Interaction, button: ui.Button):
        # TODO: 다음 단계에서 씨앗 심기 로직 구현
        await interaction.response.send_message("現在、種を植える機能を開発中です。", ephemeral=True)

    @ui.button(label="水をやる", style=discord.ButtonStyle.primary, emoji="💧")
    async def water_button(self, interaction: discord.Interaction, button: ui.Button):
        # TODO: 다음 단계에서 물뿌리개 등급별 물 주기 로직 구현
        await interaction.response.send_message("現在、水をやる機能を開発中です。", ephemeral=True)

    @ui.button(label="収穫する", style=discord.ButtonStyle.success, emoji="🧺")
    async def harvest_button(self, interaction: discord.Interaction, button: ui.Button):
        # TODO: 다음 단계에서 수확 로직 구현
        await interaction.response.send_message("現在、収穫機能を開発中です。", ephemeral=True)

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
        """이모티콘 그리드로 농장 UI를 생성합니다."""
        size_x = farm_data['size_x']
        size_y = farm_data['size_y']
        plots = farm_data['farm_plots']
        
        # 정렬된 plots 딕셔너리 생성
        sorted_plots = {(p['pos_x'], p['pos_y']): p for p in plots}

        farm_grid = []
        for y in range(size_y):
            row = []
            for x in range(size_x):
                plot = sorted_plots.get((x, y))
                if not plot:
                    row.append('❓') # 데이터가 없는 경우
                    continue
                
                state = plot['state']
                if state == 'default':
                    row.append('🟤')
                elif state == 'tilled':
                    row.append('🟫')
                # TODO: 심겨진 작물에 따른 이모티콘 추가
                else:
                    row.append('🌱') # 임시
            farm_grid.append(" ".join(row))
        
        farm_str = "\n".join(farm_grid)
        
        embed = discord.Embed(title=f"🌱｜{user.display_name}の農場", description="畑を耕し、作物を育てましょう！", color=0x8BC34A)
        embed.add_field(name="農場の様子", value=farm_str, inline=False)
        return embed

    async def register_persistent_views(self):
        view = FarmCreationPanelView(self)
        await view.setup_buttons()
        self.bot.add_view(view)

    async def create_new_farm_thread(self, interaction: discord.Interaction, user: discord.Member):
        try:
            panel_channel = interaction.channel
            farm_thread = await panel_channel.create_thread(
                name=f"🌱｜{user.display_name}の農場",
                type=discord.ChannelType.private_thread,
                invitable=False
            )
            
            farm_data = await get_farm_data(user.id)
            if not farm_data:
                farm_data = await create_farm(user.id)
            
            # [✅] DB에 생성된 스레드 ID를 저장합니다.
            await supabase.table('farms').update({'thread_id': farm_thread.id}).eq('user_id', user.id).execute()
            # 최신 정보를 다시 불러옵니다.
            farm_data = await get_farm_data(user.id)

            welcome_embed_data = await get_embed_from_db("farm_thread_welcome")
            if welcome_embed_data:
                welcome_embed = format_embed_from_db(welcome_embed_data, user_name=user.display_name)
                await farm_thread.send(embed=welcome_embed)
            
            # 농장 UI 전송
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
