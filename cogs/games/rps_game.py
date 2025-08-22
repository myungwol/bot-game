# bot-game/cogs/rps_game.py

import discord
from discord.ext import commands, tasks
from discord import ui
import logging
import asyncio
from typing import Optional, Dict, List, Set
from datetime import datetime, timezone, timedelta

from utils.database import (
    get_wallet, update_wallet, get_config,
    save_panel_id, get_panel_id, get_embed_from_db
)
from utils.helpers import format_embed_from_db, CloseButtonView

logger = logging.getLogger(__name__)

HAND_EMOJIS = {"rock": "✊", "scissors": "✌️", "paper": "✋"}
HAND_NAMES = {"rock": "グー", "scissors": "チョキ", "paper": "パー"}

class BetAmountModal(ui.Modal, title="ベット額の入力 (じゃんけん)"):
    amount = ui.TextInput(label="金額 (10円単位)", placeholder="例: 100", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("RPSGame")
        if not cog: 
            await interaction.response.send_message("エラー: ゲームCogが見つかりません。", ephemeral=True, view=CloseButtonView(interaction.user))
            return
        
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
            message_content = f"❌ {e}"
            view = CloseButtonView(interaction.user)
            if not interaction.response.is_done():
                await interaction.response.send_message(message_content, ephemeral=True, view=view)
            else:
                await interaction.followup.send(message_content, ephemeral=True, view=view)

        except Exception as e:
            logger.error(f"じゃんけんのベット処理中にエラー: {e}", exc_info=True)
            message_content = "❌ 処理中にエラーが発生しました。"
            view = CloseButtonView(interaction.user)
            if not interaction.response.is_done():
                await interaction.response.send_message(message_content, ephemeral=True, view=view)
            else:
                await interaction.followup.send(message_content, ephemeral=True, view=view)


class RPSLobbyView(ui.View):
    def __init__(self, cog, channel_id: int):
        lobby_timeout_str = get_config("RPS_LOBBY_TIMEOUT", "60").strip('"')
        lobby_timeout = int(lobby_timeout_str)
        super().__init__(timeout=lobby_timeout + 5)
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
        choice_timeout_str = get_config("RPS_CHOICE_TIMEOUT", "45").strip('"')
        choice_timeout = int(choice_timeout_str)
        super().__init__(timeout=choice_timeout + 5)
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
        self.max_players = 5
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
        self.max_players = int(get_config("RPS_MAX_PLAYERS", "5").strip('"'))

    async def create_game_lobby(self, interaction: discord.Interaction, bet_amount: int):
        user_lock = self.user_locks.setdefault(interaction.user.id, asyncio.Lock())
        if user_lock.locked():
            await interaction.followup.send("❌ 現在、他の操作を処理中です。しばらくお待ちください。", ephemeral=True, view=CloseButtonView(interaction.user))
            return

        async with user_lock:
            channel_id = interaction.channel.id
            host = interaction.user

            if channel_id in self.active_games:
                await interaction.followup.send("❌ このチャンネルでは既にゲームが進行中です。", ephemeral=True, view=CloseButtonView(interaction.user))
                return

            await update_wallet(host, -bet_amount)

            lobby_timeout_str = get_config("RPS_LOBBY_TIMEOUT", "60").strip('"')
            lobby_timeout = int(lobby_timeout_str)

            lobby_embed = self.build_lobby_embed(host, bet_amount, [host], lobby_timeout)
            view = RPSLobbyView(self, channel_id)
            
            lobby_message = await interaction.channel.send(embed=lobby_embed, view=view)

            self.active_games[channel_id] = {
                "host_id": host.id,
                "bet_amount": bet_amount,
                "players": {host.id: host},
                "initial_players": [host],
                "lobby_message": lobby_message,
                "game_message": None,
                "round": 0,
                "choices": {},
                "task": self.bot.loop.create_task(self.lobby_countdown(channel_id, lobby_timeout)),
                "created_at": datetime.now(timezone.utc)
            }
            await interaction.followup.send(f"✅ じゃんけん部屋を作成しました！ ベット額: `{bet_amount}`{self.currency_icon}", ephemeral=True, view=CloseButtonView(interaction.user))

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

        choice_timeout_str = get_config("RPS_CHOICE_TIMEOUT", "45").strip('"')
        choice_timeout = int(choice_timeout_str)

        game_embed = self.build_game_embed(game, choice_timeout=choice_timeout)
        view = RPSGameView(self, channel_id)

        if game.get("game_message"):
            try:
                game["game_message"] = await game["game_message"].edit(embed=game_embed, view=view)
            except discord.NotFound:
                game["game_message"] = await self.bot.get_channel(channel_id).send(embed=game_embed, view=view)
        else:
            game["game_message"] = await self.bot.get_channel(channel_id).send(embed=game_embed, view=view)

        game["task"] = self.bot.loop.create_task(self.choice_countdown(channel_id, choice_timeout))

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

        choice_timeout_str = get_config("RPS_CHOICE_TIMEOUT", "45").strip('"')
        choice_timeout = int(choice_timeout_str)

        result_text = self.format_round_result(game, winners, losers)
        game_embed = self.build_game_embed(game, result_text, choice_timeout)
        if game.get("game_message"):
            await game["game_message"].edit(embed=game_embed, view=None)

        await asyncio.sleep(5)
        await self.start_new_round(channel_id)

    async def end_game(self, channel_id: int, winner: Optional[discord.Member]):
        game = self.active_games.pop(channel_id, None)
        if not game: return

        if game.get("task") and not game["task"].done():
            game["task"].cancel()

        for msg_key in ["lobby_message", "game_message"]:
            if msg := game.get(msg_key):
                try: await msg.delete()
                except discord.NotFound: pass

        log_embed = None
        if winner:
            initial_players = game.get("initial_players", [winner])
            total_pot = game["bet_amount"] * len(initial_players)
            
            await update_wallet(winner, total_pot)
            
            if embed_data := await get_embed_from_db("log_rps_game_end"):
                participants_list = ", ".join([p.mention for p in initial_players])
                log_embed = format_embed_from_db(
                    embed_data, winner_mention=winner.mention,
                    total_pot=total_pot, bet_amount=game["bet_amount"],
                    participants_list=participants_list, currency_icon=self.currency_icon
                )
        else:
            initial_players = game.get("initial_players", [])
            if not initial_players: return

            refund_tasks = [update_wallet(player, game["bet_amount"]) for player in initial_players]
            await asyncio.gather(*refund_tasks)
            
            player_mentions = ", ".join(p.mention for p in initial_players)
            refund_message = f"**✊✌️✋ じゃんけん中止**\n> ゲームが中止されたため、参加者 {player_mentions} にベット額 `{game['bet_amount']}`{self.currency_icon}が返金されました。"
            log_embed = discord.Embed(description=refund_message, color=0x99AAB5)
        
        channel = self.bot.get_channel(channel_id)
        if channel:
            await self.regenerate_panel(channel, last_game_log=log_embed)

    async def handle_join(self, interaction: discord.Interaction, channel_id: int):
        user_lock = self.user_locks.setdefault(interaction.user.id, asyncio.Lock())
        if user_lock.locked():
            await interaction.response.send_message("❌ 現在、他の操作を処理中です。しばらくお待ちください。", ephemeral=True, view=CloseButtonView(interaction.user))
            return

        async with user_lock:
            game = self.active_games.get(channel_id)
            user = interaction.user
            if not game: 
                await interaction.response.send_message("❌ 募集が終了したゲームです。", ephemeral=True, view=CloseButtonView(user))
                return
            if user.id in game["players"]:
                await interaction.response.send_message("❌ すで参加しています。", ephemeral=True, view=CloseButtonView(user))
                return
            if len(game["players"]) >= self.max_players:
                await interaction.response.send_message("❌ 満員です。", ephemeral=True, view=CloseButtonView(user))
                return

            wallet = await get_wallet(user.id)
            if wallet.get('balance', 0) < game["bet_amount"]:
                await interaction.response.send_message(f"❌ コインが不足しています。(必要: {game['bet_amount']}{self.currency_icon})", ephemeral=True, view=CloseButtonView(user))
                return

            await update_wallet(user, -game["bet_amount"])
            game["players"][user.id] = user
            game["initial_players"].append(user)

            lobby_timeout_str = get_config("RPS_LOBBY_TIMEOUT", "60").strip('"')
            lobby_timeout = int(lobby_timeout_str)
            embed = self.build_lobby_embed(self.bot.get_user(game["host_id"]), game["bet_amount"], list(game["players"].values()), lobby_timeout)
            await game["lobby_message"].edit(embed=embed)
            
            await interaction.response.send_message("✅ ゲームに参加しました！", ephemeral=True, view=CloseButtonView(user))

    async def handle_start_manually(self, interaction: discord.Interaction, channel_id: int):
        game = self.active_games.get(channel_id)
        if not game or interaction.user.id != game["host_id"]:
            await interaction.response.send_message("❌ 部屋主のみがゲームを開始できます。", ephemeral=True, view=CloseButtonView(interaction.user))
            return
        if len(game["players"]) < 2:
            await interaction.response.send_message("❌ 参加者が2人以上必要です。", ephemeral=True, view=CloseButtonView(interaction.user))
            return

        await interaction.response.defer()
        if game["task"]: game["task"].cancel()
        
        await game["lobby_message"].delete()
        game["lobby_message"] = None
        await self.start_new_round(channel_id)

    async def handle_cancel(self, interaction: discord.Interaction, channel_id: int):
        game = self.active_games.get(channel_id)
        if not game or interaction.user.id != game["host_id"]:
            await interaction.response.send_message("❌ 部屋主のみがゲームを中止できます。", ephemeral=True, view=CloseButtonView(interaction.user))
            return

        await interaction.response.defer()
        if game["task"]: game["task"].cancel()
        await self.end_game(channel_id, None)
        await interaction.followup.send("ゲームを中止しました。", ephemeral=True, view=CloseButtonView(interaction.user))


    async def handle_choice(self, interaction: discord.Interaction, channel_id: int, choice: str):
        game = self.active_games.get(channel_id)
        user_id = interaction.user.id
        if not game or user_id not in game["players"]: return await interaction.response.defer()
        if user_id in game["choices"]:
            await interaction.response.send_message("❌ すでに選択済みです。", ephemeral=True, view=CloseButtonView(interaction.user))
            return

        game["choices"][user_id] = choice
        await interaction.response.send_message(f"✅ {HAND_NAMES[choice]}を出しました。", ephemeral=True, view=CloseButtonView(interaction.user))

        # [✅ 수정] 유저가 수를 낼 때마다, 누가 냈는지 알 수 있도록 게임 현황판을 즉시 업데이트합니다.
        if game.get("game_message"):
            try:
                updated_embed = self.build_game_embed(game)
                await game["game_message"].edit(embed=updated_embed)
            except Exception as e:
                logger.warning(f"RPS 게임 보드 업데이트 실패: {e}")

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
            if game.get("lobby_message"):
                await game["lobby_message"].delete()
                game["lobby_message"] = None
            await self.start_new_round(channel_id)

    async def choice_countdown(self, channel_id: int, seconds: int):
        await asyncio.sleep(seconds)
        if channel_id in self.active_games:
            await self.resolve_round(channel_id)
            
    def build_lobby_embed(self, host: discord.User, bet: int, players: List[discord.Member], timeout: int) -> discord.Embed:
        embed = discord.Embed(title="✊✌️✋ じゃんけん参加者募集中！", color=0x9B59B6)
        embed.description = f"**主催者:** {host.mention}\n**ベット額:** `{bet}`{self.currency_icon}"
        # [✅ 수정] discord.Member 객체의 .display_name을 사용하여 서버 별명을 표시합니다.
        player_list = "\n".join([p.display_name for p in players]) or "まだいません"
        embed.add_field(name=f"参加者 ({len(players)}/{self.max_players})", value=player_list)
        embed.set_footer(text=f"{timeout}秒後に自動で開始します。")
        return embed

    def build_game_embed(self, game: Dict, result: str = "", choice_timeout: int = 45) -> discord.Embed:
        embed = discord.Embed(title=f"じゃんけん勝負！ - ラウンド {game['round']}", color=0x3498DB)
        
        # [✅ 수정] 플레이어 목록을 생성할 때, 수를 냈는지 여부에 따라 이모지를 붙여줍니다.
        player_status_list = []
        for player in game["players"].values():
            if player.id in game["choices"]:
                player_status_list.append(f"✅ {player.display_name}")
            else:
                player_status_list.append(f"❔ {player.display_name}")
        
        player_list_text = "\n".join(player_status_list)
        embed.add_field(name="現在のプレイヤー", value=player_list_text, inline=False)

        if result:
            embed.add_field(name="ラウンド結果", value=result, inline=False)
        embed.set_footer(text=f"{choice_timeout}秒以内に手を選択してください。")
        return embed

    def format_round_result(self, game: Dict, winners: Set[int], losers: Set[int]) -> str:
        lines = []
        for pid, choice in game["choices"].items():
            user = self.bot.get_user(pid)
            # [✅ 수정] user 객체가 discord.Member 객체일 경우를 대비하여 서버 별명을 우선적으로 사용합니다.
            if user:
                # 길드(서버)에서 해당 유저 정보를 찾아 Member 객체로 가져옵니다.
                guild = self.bot.get_channel(list(game["players"].values())[0].guild.id).guild
                member = guild.get_member(pid)
                display_name = member.display_name if member else user.name
                lines.append(f"{display_name}: {HAND_EMOJIS[choice]}")

        participants_in_round = set(game["choices"].keys())
        if not winners and participants_in_round:
            lines.append("\n**引き分け！** (あいこでしょ！)")
        
        # [✅ 수정] 여기도 마찬가지로 서버 별명을 사용하도록 수정합니다.
        winner_mentions = []
        guild = self.bot.get_channel(list(game["players"].values())[0].guild.id).guild
        for wid in winners:
            member = guild.get_member(wid)
            winner_mentions.append(member.display_name if member else self.bot.get_user(wid).name)
        if winner_mentions:
            lines.append(f"\n**勝者:** {', '.join(winner_mentions)}")

        loser_mentions = []
        for lid in losers:
            member = guild.get_member(lid)
            loser_mentions.append(member.display_name if member else self.bot.get_user(lid).name)
        if loser_mentions:
            lines.append(f"**敗者:** {', '.join(loser_mentions)}")

        return "\n".join(lines)
    
    async def register_persistent_views(self):
        view = RPSGamePanelView(self)
        self.bot.add_view(view)

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_rps_game", last_game_log: Optional[discord.Embed] = None):
        if last_game_log:
            try: await channel.send(embed=last_game_log)
            except Exception as e: logger.error(f"じゃんけんゲームのログメッセージ送信に失敗: {e}")

        if panel_info := get_panel_id(panel_key):
            if (old_channel := self.bot.get_channel(panel_info['channel_id'])) and (old_message_id := panel_info.get('message_id')):
                try: await (await old_channel.fetch_message(old_message_id)).delete()
                except (discord.NotFound, discord.Forbidden): pass
        
        embed_data = await get_embed_from_db(panel_key)
        if not embed_data: return

        embed = discord.Embed.from_dict(embed_data)
        view = RPSGamePanelView(self)
        
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_key, new_message.id, channel.id)

class RPSGamePanelView(ui.View):
    def __init__(self, cog_instance: 'RPSGame'):
        super().__init__(timeout=None)
        self.cog = cog_instance
        create_button = ui.Button(label="部屋を作る", style=discord.ButtonStyle.secondary, emoji="✊", custom_id="rps_create_room_button")
        create_button.callback = self.create_room_callback
        self.add_item(create_button)

    async def create_room_callback(self, interaction: discord.Interaction):
        user_lock = self.cog.user_locks.setdefault(interaction.user.id, asyncio.Lock())
        if user_lock.locked():
            await interaction.response.send_message("❌ 現在、他の操作を処理中です。しばらくお待ちください。", ephemeral=True, view=CloseButtonView(interaction.user))
            return

        async with user_lock:
            if interaction.channel.id in self.cog.active_games:
                await interaction.response.send_message("❌ このチャンネルでは既にゲームが進行中です。", ephemeral=True, view=CloseButtonView(interaction.user))
                return
            await interaction.response.send_modal(BetAmountModal())

async def setup(bot: commands.Bot):
    await bot.add_cog(RPSGame(bot))
