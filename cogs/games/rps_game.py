# bot-game/cogs/games/rps_game.py

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
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

HAND_EMOJIS = {"rock": "✊", "scissors": "✌️", "paper": "✋"}
HAND_NAMES = {"rock": "주먹", "scissors": "가위", "paper": "보"}

class BetAmountModal(ui.Modal, title="베팅 금액 입력 (가위바위보)"):
    amount = ui.TextInput(label="금액 (10코인 단위)", placeholder="예: 100", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("RPSGame")
        if not cog:
            await interaction.response.send_message("오류: 게임 Cog를 찾을 수 없습니다.", ephemeral=True)
            return

        try:
            bet_amount = int(self.amount.value)
            if bet_amount <= 0 or bet_amount % 10 != 0:
                raise ValueError("10코인 단위의 양수만 입력할 수 있습니다.")

            wallet = await get_wallet(interaction.user.id)
            if wallet.get('balance', 0) < bet_amount:
                raise ValueError(f"잔액이 부족합니다. (현재 잔액: {wallet.get('balance', 0):,})")

            await interaction.response.defer(ephemeral=True, thinking=True)
            await cog.create_game_lobby(interaction, bet_amount)

        except ValueError as e:
            message_content = f"❌ {e}"
            if not interaction.response.is_done():
                await interaction.response.send_message(message_content, ephemeral=True)
            else:
                await interaction.followup.send(message_content, ephemeral=True)

        except Exception as e:
            logger.error(f"가위바위보 베팅 처리 중 오류: {e}", exc_info=True)
            message_content = "❌ 처리 중 오류가 발생했습니다."
            if not interaction.response.is_done():
                await interaction.response.send_message(message_content, ephemeral=True)
            else:
                await interaction.followup.send(message_content, ephemeral=True)


class RPSLobbyView(ui.View):
    def __init__(self, cog, channel_id: int):
        lobby_timeout_str = get_config("RPS_LOBBY_TIMEOUT", "60").strip('"')
        lobby_timeout = int(lobby_timeout_str)
        super().__init__(timeout=lobby_timeout + 5)
        self.cog = cog
        self.channel_id = channel_id

    @ui.button(label="참가하기", style=discord.ButtonStyle.success)
    async def join_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.cog.handle_join(interaction, self.channel_id)

    @ui.button(label="게임 시작", style=discord.ButtonStyle.primary)
    async def start_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.cog.handle_start_manually(interaction, self.channel_id)

    @ui.button(label="취소하기", style=discord.ButtonStyle.danger)
    async def cancel_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.cog.handle_cancel(interaction, self.channel_id)

class RPSGameView(ui.View):
    def __init__(self, cog, channel_id: int):
        choice_timeout_str = get_config("RPS_CHOICE_TIMEOUT", "45").strip('"')
        choice_timeout = int(choice_timeout_str)
        super().__init__(timeout=choice_timeout + 5)
        self.cog = cog
        self.channel_id = channel_id

    @ui.button(label="주먹", style=discord.ButtonStyle.secondary, emoji="✊")
    async def rock_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.cog.handle_choice(interaction, self.channel_id, "rock")

    @ui.button(label="가위", style=discord.ButtonStyle.secondary, emoji="✌️")
    async def scissors_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.cog.handle_choice(interaction, self.channel_id, "scissors")

    @ui.button(label="보", style=discord.ButtonStyle.secondary, emoji="✋")
    async def paper_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.cog.handle_choice(interaction, self.channel_id, "paper")

class RPSGame(commands.Cog):
    # ▼▼▼ [수정] __init__ 메서드 수정 ▼▼▼
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_games: Dict[int, Dict] = {}
        self.currency_icon = "🪙"
        self.user_locks: Dict[int, asyncio.Lock] = {} # defaultdict 대신 일반 dict 사용
        self.max_players = 5
        self.cleanup_stale_games.start()
    # ▲▲▲ [수정] 완료 ▲▲▲

    # ▼▼▼▼▼ 핵심 추가 ▼▼▼▼▼
    async def cog_teardown(self):
        """Cog가 종료될 때 호출되는 비동기 클린업 메서드입니다."""
        logger.info("[RPSGame] Cog가 종료됩니다. 모든 활성 가위바위보 게임을 취소하고 환불을 진행합니다.")
        
        # active_games 딕셔너리를 반복하는 동안 수정될 수 있으므로 키 목록을 복사합니다.
        active_channel_ids = list(self.active_games.keys())
        
        if not active_channel_ids:
            logger.info("[RPSGame] 정리할 활성 게임이 없습니다.")
            return

        # 모든 게임 종료 작업을 동시에 실행합니다.
        cleanup_tasks = [self.end_game(channel_id, None) for channel_id in active_channel_ids]
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)
        
        logger.info(f"[RPSGame] {len(active_channel_ids)}개의 활성 게임을 성공적으로 정리했습니다.")

    def cog_unload(self):
        self.cleanup_stale_games.cancel()

    @tasks.loop(minutes=30)
    async def cleanup_stale_games(self):
        logger.info("오래된 가위바위보 게임 세션 정리를 시작합니다...")
        now = datetime.now(timezone.utc)
        stale_game_channels = []
        for channel_id, game in self.active_games.items():
            created_at = game.get("created_at", now)
            if now - created_at > timedelta(minutes=30):
                stale_game_channels.append(channel_id)

        for channel_id in stale_game_channels:
            logger.warning(f"채널 {channel_id}의 오래된 게임을 강제 종료합니다.")
            await self.end_game(channel_id, None)
        logger.info(f"정리 완료. {len(stale_game_channels)}개의 게임을 종료했습니다.")

    @cleanup_stale_games.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()

    async def cog_load(self):
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")
        self.max_players = int(get_config("RPS_MAX_PLAYERS", "5").strip('"'))

    async def create_game_lobby(self, interaction: discord.Interaction, bet_amount: int):
        user_lock = self.user_locks.setdefault(interaction.user.id, asyncio.Lock())
        if user_lock.locked():
            await interaction.followup.send("❌ 현재 다른 작업을 처리 중입니다. 잠시만 기다려주세요.", ephemeral=True)
            return

        async with user_lock:
            channel_id = interaction.channel.id
            host = interaction.user

            if channel_id in self.active_games:
                await interaction.followup.send("❌ 이 채널에서는 이미 게임이 진행 중입니다.", ephemeral=True)
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
            await interaction.followup.send(f"✅ 가위바위보 방을 만들었습니다! 베팅 금액: `{bet_amount}`{self.currency_icon}", ephemeral=True)

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

    # ▼▼▼ [수정] end_game 메서드 수정 ▼▼▼
    async def end_game(self, channel_id: int, winner: Optional[discord.Member]):
        game = self.active_games.pop(channel_id, None)
        if not game: return

        if game.get("task") and not game["task"].done():
            game["task"].cancel()

        for msg_key in ["lobby_message", "game_message"]:
            if msg := game.get(msg_key):
                try: await msg.delete()
                except discord.NotFound: pass

        initial_players = game.get("initial_players", [])
        log_embed = None

        if winner:
            total_pot = game["bet_amount"] * len(initial_players)
            await update_wallet(winner, total_pot)

            if embed_data := await get_embed_from_db("log_rps_game_end"):
                participants_list = ", ".join([p.mention for p in initial_players])
                log_embed = format_embed_from_db(
                    embed_data, winner_mention=winner.mention,
                    total_pot=total_pot, bet_amount=game["bet_amount"],
                    participants_list=participants_list, currency_icon=self.currency_icon
                )
        elif initial_players:
            refund_tasks = [update_wallet(player, game["bet_amount"]) for player in initial_players]
            await asyncio.gather(*refund_tasks)

            player_mentions = ", ".join(p.mention for p in initial_players)
            refund_message = f"**✊✌️✋ 가위바위보 중지**\n> 게임이 중지되어 참가자 {player_mentions}에게 베팅 금액 `{game['bet_amount']}`{self.currency_icon}이(가) 환불되었습니다."
            log_embed = discord.Embed(description=refund_message, color=0x99AAB5)

        # 게임에 참여했던 모든 유저의 lock 객체를 메모리에서 제거합니다.
        for player in initial_players:
            self.user_locks.pop(player.id, None)

        channel = self.bot.get_channel(channel_id)
        if channel:
            await self.regenerate_panel(channel, last_game_log=log_embed)
    # ▲▲▲ [수정] 완료 ▲▲▲

    async def handle_join(self, interaction: discord.Interaction, channel_id: int):
        user_lock = self.user_locks.setdefault(interaction.user.id, asyncio.Lock())
        if user_lock.locked():
            await interaction.response.send_message("❌ 현재 다른 작업을 처리 중입니다. 잠시만 기다려주세요.", ephemeral=True)
            return

        async with user_lock:
            game = self.active_games.get(channel_id)
            user = interaction.user
            if not game:
                await interaction.response.send_message("❌ 모집이 종료된 게임입니다.", ephemeral=True)
                return
            if user.id in game["players"]:
                await interaction.response.send_message("❌ 이미 참가했습니다.", ephemeral=True)
                return
            if len(game["players"]) >= self.max_players:
                await interaction.response.send_message("❌ 가득 찼습니다.", ephemeral=True)
                return

            wallet = await get_wallet(user.id)
            if wallet.get('balance', 0) < game["bet_amount"]:
                await interaction.response.send_message(f"❌ 코인이 부족합니다. (필요: {game['bet_amount']}{self.currency_icon})", ephemeral=True)
                return

            await update_wallet(user, -game["bet_amount"])
            game["players"][user.id] = user
            game["initial_players"].append(user)

            lobby_timeout_str = get_config("RPS_LOBBY_TIMEOUT", "60").strip('"')
            lobby_timeout = int(lobby_timeout_str)
            embed = self.build_lobby_embed(self.bot.get_user(game["host_id"]), game["bet_amount"], list(game["players"].values()), lobby_timeout)
            await game["lobby_message"].edit(embed=embed)

            await interaction.response.send_message("✅ 게임에 참가했습니다!", ephemeral=True)

    async def handle_start_manually(self, interaction: discord.Interaction, channel_id: int):
        game = self.active_games.get(channel_id)
        if not game or interaction.user.id != game["host_id"]:
            await interaction.response.send_message("❌ 방장만 게임을 시작할 수 있습니다.", ephemeral=True)
            return
        if len(game["players"]) < 2:
            await interaction.response.send_message("❌ 참가자가 2명 이상 필요합니다.", ephemeral=True)
            return

        await interaction.response.defer()
        if game["task"]: game["task"].cancel()

        await game["lobby_message"].delete()
        game["lobby_message"] = None
        await self.start_new_round(channel_id)

    async def handle_cancel(self, interaction: discord.Interaction, channel_id: int):
        game = self.active_games.get(channel_id)
        if not game or interaction.user.id != game["host_id"]:
            await interaction.response.send_message("❌ 방장만 게임을 취소할 수 있습니다.", ephemeral=True)
            return

        await interaction.response.defer()
        if game["task"]: game["task"].cancel()
        await self.end_game(channel_id, None)
        await interaction.followup.send("게임을 취소했습니다.", ephemeral=True)


    async def handle_choice(self, interaction: discord.Interaction, channel_id: int, choice: str):
        game = self.active_games.get(channel_id)
        user_id = interaction.user.id
        if not game or user_id not in game["players"]: return await interaction.response.defer()
        if user_id in game["choices"]:
            await interaction.response.send_message("❌ 이미 냈습니다.", ephemeral=True)
            return

        game["choices"][user_id] = choice
        await interaction.response.send_message(f"✅ {HAND_NAMES[choice]}을(를) 냈습니다.", ephemeral=True)

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
                await game["lobby_message"].channel.send("참가자가 모이지 않아 게임이 취소되었습니다.", delete_after=10)
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
        embed = discord.Embed(title="✊✌️✋ 가위바위보 참가자 모집 중!", color=0x9B59B6)
        embed.description = f"**주최자:** {host.mention}\n**베팅 금액:** `{bet}`{self.currency_icon}"
        player_list = "\n".join([p.display_name for p in players]) or "아직 없음"
        embed.add_field(name=f"참가자 ({len(players)}/{self.max_players})", value=player_list)
        embed.set_footer(text=f"{timeout}초 후에 자동으로 시작됩니다.")
        return embed

    def build_game_embed(self, game: Dict, result: str = "", choice_timeout: int = 45) -> discord.Embed:
        embed = discord.Embed(title=f"가위바위보 승부! - 라운드 {game['round']}", color=0x3498DB)

        player_status_list = []
        for player in game["players"].values():
            if player.id in game["choices"]:
                player_status_list.append(f"✅ {player.display_name}")
            else:
                player_status_list.append(f"❔ {player.display_name}")

        player_list_text = "\n".join(player_status_list)
        embed.add_field(name="현재 플레이어", value=player_list_text, inline=False)

        if result:
            embed.add_field(name="라운드 결과", value=result, inline=False)
        embed.set_footer(text=f"{choice_timeout}초 안에 패를 선택해주세요.")
        return embed

    def format_round_result(self, game: Dict, winners: Set[int], losers: Set[int]) -> str:
        lines = []

        if not game.get("players"):
            return "오류: 플레이어 정보를 찾을 수 없습니다."
        first_player = list(game["players"].values())[0]
        guild = first_player.guild

        for pid, choice in game["choices"].items():
            member = guild.get_member(pid)
            if member:
                lines.append(f"{member.display_name}: {HAND_EMOJIS[choice]}")

        participants_in_round = set(game["choices"].keys())
        if not winners and participants_in_round:
            lines.append("\n**무승부!** (다시 합니다!)")

        winner_mentions = []
        for wid in winners:
            member = guild.get_member(wid)
            if member: winner_mentions.append(member.display_name)
        if winner_mentions:
            lines.append(f"\n**승자:** {', '.join(winner_mentions)}")

        loser_mentions = []
        for lid in losers:
            member = guild.get_member(lid)
            if member: loser_mentions.append(member.display_name)
        if loser_mentions:
            lines.append(f"**패자:** {', '.join(loser_mentions)}")

        return "\n".join(lines)

    async def register_persistent_views(self):
        view = RPSGamePanelView(self)
        self.bot.add_view(view)

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_rps_game", last_game_log: Optional[discord.Embed] = None):
        if last_game_log:
            try: await channel.send(embed=last_game_log)
            except Exception as e: logger.error(f"가위바위보 게임 로그 메시지 전송 실패: {e}")

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
        create_button = ui.Button(label="방 만들기", style=discord.ButtonStyle.secondary, emoji="✊", custom_id="rps_create_room_button")
        create_button.callback = self.create_room_callback
        self.add_item(create_button)

    async def create_room_callback(self, interaction: discord.Interaction):
        user_lock = self.cog.user_locks.setdefault(interaction.user.id, asyncio.Lock())
        if user_lock.locked():
            await interaction.response.send_message("❌ 현재 다른 작업을 처리 중입니다. 잠시만 기다려주세요.", ephemeral=True)
            return

        async with user_lock:
            if interaction.channel.id in self.cog.active_games:
                await interaction.response.send_message("❌ 이 채널에서는 이미 게임이 진행 중입니다.", ephemeral=True)
                return
            await interaction.response.send_modal(BetAmountModal())

async def setup(bot: commands.Bot):
    await bot.add_cog(RPSGame(bot))
