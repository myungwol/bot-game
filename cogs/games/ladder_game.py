# cogs/ladder_game.py

import discord
from discord.ext import commands
from discord import ui, app_commands
import random
import asyncio
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# --- 게임 로비 UI ---
class LobbyView(ui.View):
    def __init__(self, cog: 'GhostLegGame', interaction: discord.Interaction):
        super().__init__(timeout=300)  # 5분 동안 아무도 시작/취소하지 않으면 자동 종료
        self.cog = cog
        self.interaction = interaction

    async def on_timeout(self):
        # View가 타임아웃되면, 해당 게임을 정리합니다.
        await self.cog.cleanup_game(self.interaction.channel.id, "時間切れのため、ゲームは自動的にキャンセルされました。")

    @ui.button(label="参加する", style=discord.ButtonStyle.success, emoji="✅")
    async def join_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.cog.handle_join(interaction)

    @ui.button(label="ゲーム開始", style=discord.ButtonStyle.primary, emoji="▶️")
    async def start_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.cog.handle_start(interaction)

    @ui.button(label="キャンセル", style=discord.ButtonStyle.danger, emoji="✖️")
    async def cancel_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.cog.handle_cancel(interaction)


# --- 메인 Cog ---
class GhostLegGame(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_games: Dict[int, Dict] = {}  # Key: channel_id, Value: game_state

    # --- 임베드 생성 헬퍼 ---
    def build_lobby_embed(self, game_state: Dict) -> discord.Embed:
        """게임 로비 상태를 보여주는 임베드를 생성합니다."""
        host = game_state['host']
        players = game_state['players']
        num_winners = game_state['num_winners']
        
        embed = discord.Embed(
            title="🎲 運命のあみだくじ",
            description=f"**{host.mention}**さんがゲームの参加者を募集しています！\n「参加する」ボタンを押してゲームに参加してください。",
            color=0x3498DB # Blue
        )
        
        player_mentions = [p.mention for p in players]
        embed.add_field(name=f"👥 参加者 ({len(players)}/10)", value="\n".join(player_mentions) if player_mentions else "まだいません", inline=True)
        embed.add_field(name="🏆 当たり", value=f"{num_winners}人", inline=True)
        embed.set_footer(text="ホストは「ゲーム開始」を押して始めることができます。")
        return embed

    # --- 게임 상태 관리 ---
    async def cleanup_game(self, channel_id: int, reason: Optional[str] = None):
        """진행 중인 게임을 정리하고 메시지를 수정합니다."""
        if channel_id in self.active_games:
            game = self.active_games.pop(channel_id)
            message = await self.bot.get_channel(channel_id).fetch_message(game['message_id'])
            if message and reason:
                await message.edit(content=reason, embed=None, view=None)
            elif message:
                # 이유가 없으면 그냥 View만 제거
                await message.edit(view=None)
        
    # --- 버튼 콜백 핸들러 ---
    async def handle_join(self, interaction: discord.Interaction):
        """'참가하기' 버튼 로직"""
        game = self.active_games.get(interaction.channel.id)
        if not game:
            return await interaction.response.send_message("募集が終了したゲームです。", ephemeral=True)
            
        if interaction.user in game['players']:
            return await interaction.response.send_message("すでに参加しています。", ephemeral=True)
            
        if len(game['players']) >= 10:
            return await interaction.response.send_message("満員のため、参加できません。", ephemeral=True)
            
        game['players'].append(interaction.user)
        embed = self.build_lobby_embed(game)
        await interaction.response.edit_message(embed=embed)

    async def handle_start(self, interaction: discord.Interaction):
        """'게임 시작' 버튼 로직"""
        game = self.active_games.get(interaction.channel.id)
        if not game:
            return await interaction.response.send_message("募集が終了したゲームです。", ephemeral=True)
        
        if interaction.user.id != game['host'].id:
            return await interaction.response.send_message("ゲームの主催者のみ開始できます。", ephemeral=True)
            
        if len(game['players']) < 2:
            return await interaction.response.send_message("ゲームを開始するには最低2人の参加者が必要です。", ephemeral=True)

        if game['num_winners'] >= len(game['players']):
            return await interaction.response.send_message("当たりの人数は、参加者の人数より少なくなければなりません。", ephemeral=True)

        # 게임 시작 처리
        await self.run_game_logic(interaction)

    async def handle_cancel(self, interaction: discord.Interaction):
        """'취소' 버튼 로직"""
        game = self.active_games.get(interaction.channel.id)
        if not game:
            return await interaction.response.send_message("募集が終了したゲームです。", ephemeral=True)
            
        if interaction.user.id != game['host'].id:
            return await interaction.response.send_message("ゲームの主催者のみキャンセルできます。", ephemeral=True)
        
        await self.cleanup_game(interaction.channel.id, "主催者によってゲームがキャンセルされました。")
        await interaction.response.defer() # 버튼 클릭에 대한 응답

    # --- 메인 게임 로직 ---
    async def run_game_logic(self, interaction: discord.Interaction):
        """사다리타기 결과를 생성하고 발표합니다."""
        game = self.active_games.get(interaction.channel.id)
        if not game: return

        # 로비 View 비활성화
        original_message = await interaction.channel.fetch_message(game['message_id'])
        if original_message and original_message.view:
            for item in original_message.view.children:
                item.disabled = True
            await original_message.edit(view=original_message.view)
        
        # 애니메이션 효과
        await interaction.response.send_message("🚀 あみだくじを開始します！")
        await asyncio.sleep(2)
        await interaction.edit_original_response(content="🪜 あみだくじに乗って下っています...")
        await asyncio.sleep(2)
        await interaction.edit_original_response(content="ドキドキ...結果は...？")
        await asyncio.sleep(2)

        # 결과 생성
        players = game['players']
        num_players = len(players)
        num_winners = game['num_winners']
        num_losers = num_players - num_winners
        
        results = ['O'] * num_winners + ['X'] * num_losers
        random.shuffle(results)
        
        player_results = dict(zip(players, results))
        
        # 결과 임베드 생성
        result_embed = discord.Embed(title="🎉 結果発表！ 🎉", color=0x2ECC71)
        
        winners = []
        losers = []
        
        result_lines = []
        for player, result in player_results.items():
            emoji = "🏆" if result == 'O' else "❌"
            result_lines.append(f"{emoji} {player.mention} -> **{result}**")
            if result == 'O':
                winners.append(player.mention)
            else:
                losers.append(player.mention)
        
        result_embed.description = "\n".join(result_lines)
        
        if winners:
            result_embed.add_field(name="👑 当たり", value="\n".join(winners), inline=False)
        if losers:
            result_embed.add_field(name="😥 ハズレ", value="\n".join(losers), inline=False)
            
        await interaction.edit_original_response(content=None, embed=result_embed)

        # 게임 상태 정리
        await self.cleanup_game(interaction.channel.id)


    # --- 슬래시 커맨드 ---
    @app_commands.command(name="あみだくじ", description="運命のあみだくじゲームを開始します。")
    @app_commands.describe(
        winners="当たりの人数を選択してください。 (1-9人)"
    )
    @app_commands.rename(winners='当たり人数')
    async def ladder_game(self, interaction: discord.Interaction, winners: app_commands.Range[int, 1, 9]):
        if interaction.channel.id in self.active_games:
            return await interaction.response.send_message(
                "❌ このチャンネルではすでにゲームが進行中です。",
                ephemeral=True
            )
        
        # 게임 상태 초기화
        game_state = {
            "host": interaction.user,
            "players": [interaction.user],
            "num_winners": winners,
            "message_id": None # 메시지 ID는 나중에 저장
        }
        self.active_games[interaction.channel.id] = game_state
        
        # 로비 임베드 및 View 생성
        embed = self.build_lobby_embed(game_state)
        view = LobbyView(self, interaction)
        
        await interaction.response.send_message(embed=embed, view=view)
        message = await interaction.original_response()
        
        # 메시지 ID 저장
        self.active_games[interaction.channel.id]['message_id'] = message.id


# --- Cog 등록 ---
async def setup(bot: commands.Bot):
    await bot.add_cog(GhostLegGame(bot))
