python
import discord
from discord.ext import commands, tasks
from discord import ui
import logging
from typing import Optional

from utils.database import (
    get_farm_data, create_farm, get_config,
    get_panel_components_from_db, save_panel_id, get_panel_id, get_embed_from_db
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

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
            # 이미 농장이 있는 경우, 기존 스레드로 초대
            farm_thread_id = farm_data.get('thread_id')
            if farm_thread_id and (thread := self.cog.bot.get_channel(farm_thread_id)):
                await interaction.followup.send(f"✅ あなたの農場はこちらです: {thread.mention}", ephemeral=True)
                await thread.send(f"{user.mention}さんが農場にやってきました！")
            else:
                # DB에는 있지만 스레드가 없는 경우 (삭제된 경우) - 새로 생성
                await self.cog.create_new_farm_thread(interaction, user)
        else:
            # 새로운 농장 생성
            await self.cog.create_new_farm_thread(interaction, user)


# 메인 Farm Cog
class Farm(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def register_persistent_views(self):
        view = FarmCreationPanelView(self)
        await view.setup_buttons()
        self.bot.add_view(view)

    async def create_new_farm_thread(self, interaction: discord.Interaction, user: discord.Member):
        try:
            panel_channel = interaction.channel
            # 비공개 스레드 생성
            farm_thread = await panel_channel.create_thread(
                name=f"🌱｜{user.display_name}の農場",
                type=discord.ChannelType.private_thread,
                invitable=False # 관리자만 초대 가능하도록 설정
            )
            
            # DB에 농장 정보 생성/업데이트
            farm_data = await get_farm_data(user.id)
            if not farm_data:
                farm_data = await create_farm(user.id)
            
            # DB의 farms 테이블에 thread_id를 저장할 컬럼이 필요합니다.
            # 이 부분은 다음 단계에서 DB 스키마 수정으로 해결합니다.
            # await supabase.table('farms').update({'thread_id': farm_thread.id}).eq('user_id', user.id).execute()

            # 스레드에 환영 메시지와 농장 UI 전송
            welcome_embed_data = await get_embed_from_db("farm_thread_welcome")
            if welcome_embed_data:
                welcome_embed = format_embed_from_db(welcome_embed_data, user_name=user.display_name)
                await farm_thread.send(embed=welcome_embed)
            
            # TODO: 여기에 농장 UI (밭, 버튼 등)를 전송하는 로직 추가
            # farm_ui_embed = self.build_farm_ui(farm_data)
            # await farm_thread.send(embed=farm_ui_embed, view=FarmUIView(...))
            
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
