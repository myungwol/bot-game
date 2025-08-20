import discord
from discord.ext import commands, tasks
from discord import ui
import logging
import asyncio
from typing import Optional, Dict, List, Set
from datetime import datetime, timezone, timedelta

from utils.database import (
    get_wallet, update_wallet, get_config, get_panel_components_from_db,
    save_panel_id, get_panel_id, get_embed_from_db
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

HAND_EMOJIS = {"rock": "✊", "scissors": "✌️", "paper": "✋"}
HAND_NAMES = {"rock": "グー", "scissors": "チョキ", "paper": "パー"}
MAX_PLAYERS = 5

class BetAmountModal(ui.Modal, title="ベット額の入力 (じゃんけん)"):
    amount = ui.TextInput(label="金額 (10円単位)", placeholder="例: 100", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("RPSGame")
        if not cog: return await interaction.response.send_message("エラー: ゲームCogが見つかりません。", ephemeral=True, delete_after=5)
        
        try:
            bet_amount = int(self.amount.value)
            if bet_amount <= 0 or bet_amount % 10 != 0:
                raise ValueError("10コイン単位の正の整数のみ入力できます。")

            wallet = await get_wallet(interaction.user.id)
            if wallet.get('balance', 0) < bet_amount:
                raise ValueError(f"残高が不足しています。(現在の残高: {wallet.get('balance', 0):,})")

            await interaction.response.defer(ephemeral=True, thinking=True)
            await cog.create_game_lobby(interaction, bet_amount)

        except ValueError as e:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ {e}", ephemeral=True, delete_after=5)
            else:
                await interaction.followup.send(f"❌ {e}", ephemeral=True, delete_after=5)
        except Exception as e:
            logger.error(f"じゃんけんのベット処理中にエラー: {e}", exc_info=True)
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ 処理中にエラーが発生しました。", ephemeral=True, delete_after=5)

class RPSLobbyView(ui.View):
    def __init__(self, cog, channel_id: int):
        super().__init__(timeout=35)
        self.cog = cog
        self.channel_id = channel_id

    @ui.button(label="参加する", style=discord.ButtonStyle.success)
    async def join_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.cog.handle_join(interaction, self.channel_id)

    @ui.button(label="ゲーム開始", style=discord.ButtonStyle.primary)
    async def start_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.cog.handle_start_manually(interaction, self.channel_id)

    @ui.button(label="中止する", style=discord.ButtonStyle.danger)
    async def cancel_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.cog.handle_cancel(interaction, self.channel_id)

class RPSGameView(ui.View):
    def __init__(self, cog, channel_id: int):
        super().__init__(timeout=35)
        self.cog = cog
        self.channel_id = channel_id

    @ui.button(label="グー", style=discord.ButtonStyle.secondary, emoji="✊")
    async def rock_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.cog.handle_choice(interaction, self.channel_id, "rock")

    @ui.button(label="チョキ", style=discord.ButtonStyle.secondary, emoji="✌️")
    async def scissors_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.cog.handle_choice(interaction, self.channel_id, "scissors")

    @ui.button(label="パー", style=discord.ButtonStyle.secondary, emoji="✋")
    async def paper_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.cog.handle_choice(interaction, self.channel_id, "paper")

class RPSGame(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_games: Dict[int, Dict] = {}
        self.currency_icon = "🪙"
        self.user_locks: Dict[int, asyncio.Lock] = {}
        self.cleanup_stale_games.start()

    def cog_unload(self):
        self.cleanup_stale_games.cancel()

    @tasks.loop(minutes=30)
    async def cleanup_stale_games(self):
        logger.info("古いじゃんけんゲームセッションのクリーンアップを開始します...")
        now = datetime.now(timezone.utc)
        stale_game_channels = []
        for channel_id, game in self.active_games.items():
            created_at = game.get("created_at", now)
            if now - created_at > timedelta(minutes=30):
                stale_game_channels.append(channel_id)
        
        for channel_id in stale_game_channels:
            logger.warning(f"チャンネル {channel_id} の古いゲームを強制的に終了します。")
            await self.end_game(channel_id, None)
        logger.info(f"クリーンアップ完了。{len(stale_game_channels)}個のゲームを終了しました。")
    
    @cleanup_stale_games.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()

    async def cog_load(self):
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")

    async def create_game_lobby(self, interaction: discord.Interaction, bet_amount: int):
        user_lock = self.user_locks.setdefault(interaction.user.id, asyncio.Lock())
        if user_lock.locked():
            return await interaction.followup.send("❌ 現在、他の操作を処理中です。しばらくお待ちください。", ephemeral=True, delete_after=5)

        async with user_lock:
            channel_id = interaction.channel.id
            host = interaction.user

            if channel_id in self.active_games:
                await interaction.followup.send("❌ このチャンネルでは既にゲームが進行中です。", ephemeral=True, delete_after=5)
                return

            lobby_embed = self.build_lobby_embed(host, bet_amount, [host])
            view = RPSLobbyView(self, channel_id)
            
            lobby_message = await interaction.channel.send(embed=lobby_embed, view=view)

            self.active_games[channel_id] = {
                "host_id": host.id,
                "bet_amount": bet_amount,
                "players": {host.id: host},
                "lobby_message": lobby_message,
                "game_message": None,
                "round": 0,
                "choices": {},
                "task": self.bot.loop.create_task(self.lobby_countdown(channel_id, 30)),
                "created_at": datetime.now(timezone.utc)
            }
            await interaction.followup.send(f"✅ じゃんけん部屋を作成しました！ ベット額: `{bet_amount}`{self.currency_icon}", ephemeral=True, delete_after=5)

    async def start_new_round(self, channel_id: int):
        game = self.active_games.get(channel_id)
        if not game: return
        
        game["round"] += 1
        game["choices"] = {}
        
        players_in_round = list(game["players"].values())
        
        if len(players_in_round) <= 1:
            winner = players_in_round[0] if players_in_round else None
            await self.end_game(channel_id, winner)
            return

        game_embed = self.build_game_embed(game)
        view = RPSGameView(self, channel_id)

        if game.get("game_message"):
            try:
                game["game_message"] = await game["game_message"].edit(embed=game_embed, view=view)
            except discord.NotFound:
                game["game_message"] = await self.bot.get_channel(channel_id).send(embed=game_embed, view=view)
        else:
            game["game_message"] = await self.bot.get_channel(channel_id).send(embed=game_embed, view=view)

        game["task"] = self.bot.loop.create_task(self.choice_countdown(channel_id, 30))

    async def resolve_round(self, channel_id: int):
        game = self.active_games.get(channel_id)
        if not game: return

        players = game["players"]
        choices = game["choices"]
        
        made_choices: Set[str] = set(choices.values())
        participants_in_round = set(choices.keys())
        all_players_in_round = set(players.keys())
        
        losers = all_players_in_round - participants_in_round
        
        if len(made_choices) in [1, 3]:
            winners = participants_in_round
        elif len(made_choices) == 2:
            c1, c2 = list(made_choices)
            winning_hand = c1 if (c1, c2) in [("rock", "scissors"), ("scissors", "paper"), ("paper", "rock")] else c2
            winners = {uid for uid, hand in choices.items() if hand == winning_hand}
            round_losers = {uid for uid, hand in choices.items() if hand != winning_hand}
            losers.update(round_losers)
        else:
            winners = set()

        for loser_id in losers:
            players.pop(loser_id, None)

        result_text = self.format_round_result(game, winners, losers)
        game_embed = self.build_game_embed(game, result_text)
        if game.get("game_message"):
            await game["game_message"].edit(embed=game_embed, view=None)

        await asyncio.sleep(5)
        await self.start_new_round(channel_id)

    async def end_game(self, channel_id: int, winner: Optional[discord.Member]):
        game = self.active_games.get(channel_id)
        if not game: return

        for msg_key in ["lobby_message", "game_message"]:
            if msg := game.get(msg_key):
                try: await msg.delete()
                except discord.NotFound: pass

        log_embed = None
        if winner:
            total_pot = game["bet_amount"] * len(game.get("initial_players", [winner]))
            await update_wallet(winner, total_pot)
            
            if embed_data := await get_embed_from_db("log_rps_game_end"):
                participants_list = ", ".join([p.mention for p in game.get("initial_players", [])])
                log_embed = format_embed_from_db(
                    embed_data, winner_mention=winner.mention,
                    total_pot=total_pot, bet_amount=game["bet_amount"],
                    participants_list=participants_list, currency_icon=self.currency_icon
                )
        
        await self.regenerate_panel(self.bot.get_channel(channel_id), last_game_log=log_embed)
        self.active_games.pop(channel_id, None)

    async def handle_join(self, interaction: discord.Interaction, channel_id: int):
        user_lock = self.user_locks.setdefault(interaction.user.id, asyncio.Lock())
        if user_lock.locked():
            return await interaction.response.send_message("❌ 現在、他の操作を処理中です。しばらくお待ちください。", ephemeral=True, delete_after=5)

        async with user_lock:
            game = self.active_games.get(channel_id)
            user = interaction.user
            if not game: return await interaction.response.send_message("❌ 募集が終了したゲームです。", ephemeral=True, delete_after=5)
            if user.id in game["players"]: return await interaction.response.send_message("❌ すで参加しています。", ephemeral=True, delete_after=5)
            if len(game["players"]) >= MAX_PLAYERS: return await interaction.response.send_message("❌ 満員です。", ephemeral=True, delete_after=5)

            wallet = await get_wallet(user.id)
            if wallet.get('balance', 0) < game["bet_amount"]:
                return await interaction.response.send_message(f"❌ コインが不足しています。(必要: {game['bet_amount']}{self.currency_icon})", ephemeral=True, delete_after=5)

            game["players"][user.id] = user
            embed = self.build_lobby_embed(self.bot.get_user(game["host_id"]), game["bet_amount"], list(game["players"].values()))
            await game["lobby_message"].edit(embed=embed)
            await interaction.response.send_message("✅ ゲームに参加しました！", ephemeral=True, delete_after=5)

    async def handle_start_manually(self, interaction: discord.Interaction, channel_id: int):
        game = self.active_games.get(channel_id)
        if not game or interaction.user.id != game["host_id"]:
            return await interaction.response.send_message("❌ 部屋主のみがゲームを開始できます。", ephemeral=True, delete_after=5)
        if len(game["players"]) < 2:
            return await interaction.response.send_message("❌ 参加者が2人以上必要です。", ephemeral=True, delete_after=5)

        await interaction.response.defer()
        if game["task"]: game["task"].cancel()
        
        game["initial_players"] = list(game["players"].values())
        
        await game["lobby_message"].delete()
        game["lobby_message"] = None
        await self.start_new_round(channel_id)

    async def handle_cancel(self, interaction: discord.Interaction, channel_id: int):
        game = self.active_games.get(channel_id)
        if not game or interaction.user.id != game["host_id"]:
            return await interaction.response.send_message("❌ 部屋主のみがゲームを中止できます。", ephemeral=True, delete_after=5)

        await interaction.response.defer()
        if game["task"]: game["task"].cancel()
        await self.end_game(channel_id, None)
        await interaction.followup.send("ゲームを中止しました。", ephemeral=True, delete_after=5)

    async def handle_choice(self, interaction: discord.Interaction, channel_id: int, choice: str):
        game = self.active_games.get(channel_id)
        user_id = interaction.user.id
        if not game or user_id not in game["players"]: return await interaction.response.defer()
        if user_id in game["choices"]:
            return await interaction.response.send_message("❌ すでに選択済みです。", ephemeral=True, delete_after=5)

        game["choices"][user_id] = choice
        await interaction.response.send_message(f"✅ {HAND_NAMES[choice]}を出しました。", ephemeral=True, delete_after=5)

        if len(game["choices"]) == len(game["players"]):
            if game["task"]: game["task"].cancel()
            await self.resolve_round(channel_id)

    async def lobby_countdown(self, channel_id: int, seconds: int):
        await asyncio.sleep(seconds)
        game = self.active_games.get(channel_id)
        if not game: return

        if len(game["players"]) < 2:
            if game.get("lobby_message"):
                await game["lobby_message"].channel.send("参加者が集まらなかったため、ゲームは中止されました。", delete_after=10)
            await self.end_game(channel_id, None)
        else:
            game["initial_players"] = list(game["players"].values())
            if game.get("lobby_message"):
                await game["lobby_message"].delete()
                game["lobby_message"] = None
            await self.start_new_round(channel_id)

    async def choice_countdown(self, channel_id: int, seconds: int):
        await asyncio.sleep(seconds)
        if channel_id in self.active_games:
            await self.resolve_round(channel_id)
            
    def build_lobby_embed(self, host: discord.User, bet: int, players: List[discord.Member]) -> discord.Embed:
        embed = discord.Embed(title="⚔️ じゃんけん参加者募集中！ ⚔️", color=0x9B59B6)
        
        description = (
            f"・**主催者:** {host.mention}\n"
            f"・**ベット額:** `{bet:,}` {self.currency_icon}"
        )
        embed.description = description
        
        player_list = "\n".join([f"・{p.display_name}" for p in players]) or "> まだ誰もいません..."
        embed.add_field(name=f"**参加者リスト ({len(players)}/{MAX_PLAYERS})**", value=player_list, inline=False)
        
        embed.set_footer(text="⏳ 30秒後、または部屋主が開始するとゲームが始まります。")
        return embed

    # [🎨 UI 수정] 게임 진행 임베드 디자인 개선
    def build_game_embed(self, game: Dict, result: str = "") -> discord.Embed:
        embed = discord.Embed(title=f"🔥 じゃんけん勝負！ - ラウンド {game['round']} 🔥", color=0x3498DB)
        embed.description = "> 各自、下のボタンから手を選択してください！"

        player_list = "\n".join([f"・**{p.display_name}**" for p in game["players"].values()])
        embed.add_field(name="**生き残りプレイヤー**", value=player_list, inline=False)
        
        if result:
            embed.add_field(name="**━━━━━━━━結果━━━━━━━━**", value=result, inline=False)
            
        embed.set_footer(text="🕒 30秒以内に選択してください。")
        return embed

    # [🎨 UI 수정] 라운드 결과 텍스트 포맷 개선
    def format_round_result(self, game: Dict, winners: Set[int], losers: Set[int]) -> str:
        lines = []
        for pid, choice in game["choices"].items():
            user = self.bot.get_user(pid)
            if user: 
                lines.append(f"・{user.display_name}: {HAND_EMOJIS[choice]}")
        
        # 선택 안 한 사람 표시
        unchosen_players = set(game["players"].keys()) - set(game["choices"].keys())
        for pid in unchosen_players:
            user = self.bot.get_user(pid)
            if user:
                lines.append(f"・{user.display_name}: `時間切れ`")

        lines.append("\n**──────────────**")

        participants_in_round = set(game["choices"].keys())
        if not winners and participants_in_round:
            lines.append(" risultato: **引き分け！** (あいこでしょ！)")
        
        winner_mentions = [f"**{self.bot.get_user(wid).display_name}**" for wid in winners if self.bot.get_user(wid)]
        if winner_mentions:
            lines.append(f"🏆 **勝者:** {', '.join(winner_mentions)}")
        
        loser_mentions = [self.bot.get_user(lid).display_name for lid in losers if self.bot.get_user(lid)]
        if loser_mentions:
            lines.append(f"💧 **敗者:** {', '.join(loser_mentions)}")

        return "\n".join(lines)
    
    async def register_persistent_views(self):
        view = RPSGamePanelView(self)
        await view.setup_buttons()
        self.bot.add_view(view)

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_rps_game", last_game_log: Optional[discord.Embed] = None):
        embed_key = "panel_rps_game"
        if panel_info := get_panel_id(panel_key):
            if (old_channel := self.bot.get_channel(panel_info['channel_id'])) and (old_message_id := panel_info.get('message_id')):
                try: await (await old_channel.fetch_message(old_message_id)).delete()
                except (discord.NotFound, discord.Forbidden): pass
        
        if last_game_log:
            try: await channel.send(embed=last_game_log)
            except Exception as e: logger.error(f"じゃんけんゲームのログメッセージ送信に失敗: {e}")

        if not (embed_data := await get_embed_from_db(embed_key)):
            return

        embed = discord.Embed.from_dict(embed_data)
        view = RPSGamePanelView(self)
        await view.setup_buttons()
        self.bot.add_view(view)
        
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_key, new_message.id, channel.id)

class RPSGamePanelView(ui.View):
    def __init__(self, cog_instance: 'RPSGame'):
        super().__init__(timeout=None)
        self.cog = cog_instance

    async def setup_buttons(self):
        self.clear_items()
        components = await get_panel_components_from_db("panel_rps_game")
        for button_info in components:
            button = ui.Button(
                label=button_info.get('label'), style=discord.ButtonStyle.secondary,
                emoji=button_info.get('emoji'), custom_id=button_info.get('component_key')
            )
            button.callback = self.create_room_callback
            self.add_item(button)

    async def create_room_callback(self, interaction: discord.Interaction):
        user_lock = self.cog.user_locks.setdefault(interaction.user.id, asyncio.Lock())
        if user_lock.locked():
            return await interaction.response.send_message("❌ 現在、他の操作を処理中です。しばらくお待ちください。", ephemeral=True, delete_after=5)

        async with user_lock:
            if interaction.channel.id in self.cog.active_games:
                await interaction.response.send_message("❌ このチャンネルでは既にゲームが進行中です。", ephemeral=True, delete_after=5)
                return
            await interaction.response.send_modal(BetAmountModal())

async def setup(bot: commands.Bot):
    await bot.add_cog(RPSGame(bot))
