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
            refund_message = f"**✊✌️✋ 가위바위보 중지**\n> 게임이 중지되어 참가자 {player_mentions}에게 베팅 금액 `{game['bet_amount']}`{self.currency_icon}이(가) 환불되었습니다."
            log_embed = discord.Embed(description=refund_message, color=0x99AAB5)
        
        channel = self.bot.get_channel(channel_id)
        if channel:
            await self.regenerate_panel(channel, last_game_log=log_embed)

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
```
---
#### `cogs/games/user_profile.py`
```python
# cogs/games/user_profile.py

import discord
from discord.ext import commands
from discord import ui
import logging
import asyncio
import math
from typing import Optional, Dict, List, Any

from utils.database import (
    get_inventory, get_wallet, get_aquarium, set_user_gear, get_user_gear,
    save_panel_id, get_panel_id, get_id, get_embed_from_db,
    get_item_database, get_config, get_string, BARE_HANDS,
    supabase
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

GEAR_CATEGORY = "장비"
BAIT_CATEGORY = "미끼"

class ProfileView(ui.View):
    def __init__(self, user: discord.Member, cog_instance: 'UserProfile'):
        super().__init__(timeout=300)
        self.user: discord.Member = user
        self.cog = cog_instance
        self.message: Optional[discord.WebhookMessage] = None
        self.currency_icon = get_config("GAME_CONFIG", {}).get("CURRENCY_ICON", "🪙")
        self.current_page = "info"
        self.fish_page_index = 0
        self.cached_data = {}
        self.status_message: Optional[str] = None

    async def build_and_send(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.load_data(self.user)
        embed = await self.build_embed()
        self.build_components()
        self.message = await interaction.followup.send(embed=embed, view=self, ephemeral=True)

    async def update_display(self, interaction: discord.Interaction, reload_data: bool = False):
        await interaction.response.defer()
        if reload_data:
            await self.load_data(self.user)
        embed = await self.build_embed()
        self.build_components()
        await interaction.edit_original_response(embed=embed, view=self)
        self.status_message = None

    async def load_data(self, user: discord.Member):
        wallet_data, inventory, aquarium, gear = await asyncio.gather(
            get_wallet(user.id),
            get_inventory(user),
            get_aquarium(str(user.id)),
            get_user_gear(user)
        )
        self.cached_data = {"wallet": wallet_data, "inventory": inventory, "aquarium": aquarium, "gear": gear}

    def _get_current_tab_config(self) -> Dict:
        all_ui_strings = get_config("strings", {})
        tabs_config = all_ui_strings.get("profile_view", {}).get("tabs", [])
        return next((tab for tab in tabs_config if tab.get("key") == self.current_page), {})

    async def build_embed(self) -> discord.Embed:
        inventory = self.cached_data.get("inventory", {})
        gear = self.cached_data.get("gear", {})
        balance = self.cached_data.get("wallet", {}).get('balance', 0)
        item_db = get_item_database()
        
        all_ui_strings = get_config("strings", {})
        profile_strings = all_ui_strings.get("profile_view", {})

        base_title = profile_strings.get("base_title", "{user_name}의 소지품").format(user_name=self.user.display_name)
        
        current_tab_config = self._get_current_tab_config()
        title_suffix = current_tab_config.get("title_suffix", "")

        embed = discord.Embed(title=f"{base_title}{title_suffix}", color=self.user.color or discord.Color.blue())
        if self.user.display_avatar:
            embed.set_thumbnail(url=self.user.display_avatar.url)
        description = ""
        if self.status_message:
            description += f"**{self.status_message}**\n\n"
        
        if self.current_page == "info":
            info_tab_strings = profile_strings.get("info_tab", {})
            embed.add_field(name=info_tab_strings.get("field_balance", "소지금"), value=f"`{balance:,}`{self.currency_icon}", inline=True)
            
            job_name = "일반 주민"
            try:
                job_res = await supabase.table('user_jobs').select('jobs(job_name)').eq('user_id', self.user.id).maybe_single().execute()
                if job_res and job_res.data and job_res.data.get('jobs'):
                    job_name = job_res.data['jobs']['job_name']
            except Exception as e:
                logger.error(f"직업 정보 조회 중 오류 발생 (유저: {self.user.id}): {e}")
            embed.add_field(name="직업", value=f"`{job_name}`", inline=True)

            user_rank_mention = info_tab_strings.get("default_rank_name", "새내기 주민")
            
            job_system_config = get_config("JOB_SYSTEM_CONFIG", {})
            level_tier_roles = job_system_config.get("LEVEL_TIER_ROLES", [])
            
            sorted_tier_roles = sorted(level_tier_roles, key=lambda x: x.get('level', 0), reverse=True)
            
            user_role_ids = {role.id for role in self.user.roles}
            
            for tier in sorted_tier_roles:
                role_key = tier.get('role_key')
                if not role_key: continue
                
                if (rank_role_id := get_id(role_key)) and rank_role_id in user_role_ids:
                    if rank_role := self.user.guild.get_role(rank_role_id):
                        user_rank_mention = rank_role.mention
                        break
            
            embed.add_field(name=info_tab_strings.get("field_rank", "등급"), value=user_rank_mention, inline=True)
            
            description += info_tab_strings.get("description", "아래 탭을 선택하여 상세 정보를 확인하세요.")
            embed.description = description
        
        elif self.current_page == "item":
            excluded_categories = [GEAR_CATEGORY, "농장_씨앗", "농장_작물", BAIT_CATEGORY]
            general_items = {name: count for name, count in inventory.items() if item_db.get(name, {}).get('category') not in excluded_categories}
            item_list = [f"{item_db.get(n,{}).get('emoji','📦')} **{n}**: `{c}`개" for n, c in general_items.items()]
            embed.description = description + ("\n".join(item_list) or profile_strings.get("item_tab", {}).get("no_items", "보유 중인 아이템이 없습니다."))
        
        elif self.current_page == "gear":
            gear_categories = {"낚시": {"rod": "🎣 낚싯대", "bait": "🐛 미끼"}, "농장": {"hoe": "🪓 괭이", "watering_can": "💧 물뿌리개"}}
            for category_name, items in gear_categories.items():
                field_lines = [f"**{label}:** `{gear.get(key, BARE_HANDS)}`" for key, label in items.items()]
                embed.add_field(name=f"**[ 현재 장비: {category_name} ]**", value="\n".join(field_lines), inline=False)
            owned_gear_items = {name: count for name, count in inventory.items() if item_db.get(name, {}).get('category') == GEAR_CATEGORY}
            if owned_gear_items:
                gear_list = [f"{item_db.get(n,{}).get('emoji','🔧')} **{n}**: `{c}`개" for n, c in owned_gear_items.items()]
                embed.add_field(name="\n**[ 보유 중인 장비 ]**", value="\n".join(gear_list), inline=False)
            else:
                embed.add_field(name="\n**[ 보유 중인 장비 ]**", value=profile_strings.get("gear_tab", {}).get("no_owned_gear", "보유 중인 장비가 없습니다."), inline=False)
            embed.description = description
        
        elif self.current_page == "fish":
            fish_tab_strings = profile_strings.get("fish_tab", {})
            aquarium = self.cached_data.get("aquarium", [])
            if not aquarium:
                embed.description = description + fish_tab_strings.get("no_fish", "어항에 물고기가 없습니다.")
            else:
                total_pages = math.ceil(len(aquarium) / 10)
                self.fish_page_index = max(0, min(self.fish_page_index, total_pages - 1))
                fish_on_page = aquarium[self.fish_page_index * 10 : self.fish_page_index * 10 + 10]
                embed.description = description + "\n".join([f"{f['emoji']} **{f['name']}**: `{f['size']}`cm" for f in fish_on_page])
                embed.set_footer(text=fish_tab_strings.get("pagination_footer", "페이지 {current_page} / {total_pages}").format(current_page=self.fish_page_index + 1, total_pages=total_pages))
        
        elif self.current_page == "seed":
            seed_items = {name: count for name, count in inventory.items() if item_db.get(name, {}).get('category') == "농장_씨앗"}
            item_list = [f"{item_db.get(n,{}).get('emoji','🌱')} **{n}**: `{c}`개" for n, c in seed_items.items()]
            embed.description = description + ("\n".join(item_list) or profile_strings.get("seed_tab", {}).get("no_items", "보유 중인 씨앗이 없습니다."))
        
        elif self.current_page == "crop":
            crop_items = {name: count for name, count in inventory.items() if item_db.get(name, {}).get('category') == "농장_작물"}
            item_list = [f"{item_db.get(n,{}).get('emoji','🌾')} **{n}**: `{c}`개" for n, c in crop_items.items()]
            embed.description = description + ("\n".join(item_list) or profile_strings.get("crop_tab", {}).get("no_items", "보유 중인 작물이 없습니다."))
        
        else:
            embed.description = description + profile_strings.get("wip_tab", {}).get("description", "이 기능은 현재 준비 중입니다.")
        return embed

    def build_components(self):
        self.clear_items()
        all_ui_strings = get_config("strings", {})
        profile_strings = all_ui_strings.get("profile_view", {})
        tabs_config = profile_strings.get("tabs", [])
        
        row_counter, tab_buttons_in_row = 0, 0
        for config in tabs_config:
            key = config.get("key")
            if not key: continue

            if tab_buttons_in_row >= 5:
                row_counter += 1
                tab_buttons_in_row = 0
            style = discord.ButtonStyle.primary if self.current_page == key else discord.ButtonStyle.secondary
            self.add_item(ui.Button(label=config.get("label"), style=style, custom_id=f"profile_tab_{key}", emoji=config.get("emoji"), row=row_counter))
            tab_buttons_in_row += 1
        
        row_counter += 1
        if self.current_page == "gear":
            self.add_item(ui.Button(label="낚싯대 변경", style=discord.ButtonStyle.blurple, custom_id="profile_change_rod", emoji="🎣", row=row_counter))
            self.add_item(ui.Button(label="미끼 변경", style=discord.ButtonStyle.blurple, custom_id="profile_change_bait", emoji="🐛", row=row_counter))
            
            row_counter += 1
            self.add_item(ui.Button(label="괭이 변경", style=discord.ButtonStyle.success, custom_id="profile_change_hoe", emoji="🪓", row=row_counter))
            self.add_item(ui.Button(label="물뿌리개 변경", style=discord.ButtonStyle.success, custom_id="profile_change_watering_can", emoji="💧", row=row_counter))
        
        row_counter += 1
        if self.current_page == "fish" and self.cached_data.get("aquarium"):
            if math.ceil(len(self.cached_data["aquarium"]) / 10) > 1:
                total_pages = math.ceil(len(self.cached_data["aquarium"]) / 10)
                pagination_buttons = profile_strings.get("pagination_buttons", {})
                self.add_item(ui.Button(label=pagination_buttons.get("prev", "◀"), custom_id="profile_fish_prev", disabled=self.fish_page_index == 0, row=row_counter))
                self.add_item(ui.Button(label=pagination_buttons.get("next", "▶"), custom_id="profile_fish_next", disabled=self.fish_page_index >= total_pages - 1, row=row_counter))
        
        for child in self.children:
            if isinstance(child, ui.Button):
                child.callback = self.button_callback
                
    async def button_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("자신 전용 메뉴를 조작해주세요.", ephemeral=True)
            return
        
        custom_id = interaction.data['custom_id']
        if custom_id.startswith("profile_tab_"):
            self.current_page = custom_id.split("_")[-1]
            if self.current_page == 'fish': self.fish_page_index = 0
            await self.update_display(interaction, reload_data=False) 
        elif custom_id.startswith("profile_change_"):
            gear_type = custom_id.replace("profile_change_", "", 1)
            await GearSelectView(self, gear_type).setup_and_update(interaction)
        elif custom_id.startswith("profile_fish_"):
            if custom_id.endswith("prev"): self.fish_page_index -= 1
            else: self.fish_page_index += 1
            await self.update_display(interaction)
            
class GearSelectView(ui.View):
    def __init__(self, parent_view: ProfileView, gear_type: str):
        super().__init__(timeout=180)
        self.parent_view = parent_view
        self.user = parent_view.user
        self.gear_type = gear_type
        
        GEAR_SETTINGS = {
            "rod":          (GEAR_CATEGORY, "낚싯대", "낚싯대 해제", BARE_HANDS),
            "hoe":          (GEAR_CATEGORY, "괭이", "괭이 해제", BARE_HANDS),
            "watering_can": (GEAR_CATEGORY, "물뿌리개", "물뿌리개 해제", BARE_HANDS),
            "bait":         (BAIT_CATEGORY, "낚시 미끼", "미끼 해제", "미끼 없음")
        }
        
        settings = GEAR_SETTINGS.get(self.gear_type)
        if settings:
            self.db_category, self.category_name, self.unequip_label, self.default_item = settings
        else:
            self.db_category, self.category_name, self.unequip_label, self.default_item = ("알 수 없음", "알 수 없음", "해제", "없음")

    async def setup_and_update(self, interaction: discord.Interaction):
        await interaction.response.defer()
        inventory, item_db = self.parent_view.cached_data.get("inventory", {}), get_item_database()
        
        all_ui_strings = get_config("strings", {})
        gear_select_strings = all_ui_strings.get("profile_view", {}).get("gear_select_view", {})

        options = [discord.SelectOption(label=f'{gear_select_strings.get("unequip_prefix", "✋")} {self.unequip_label}', value="unequip")]
        
        for name, count in inventory.items():
            item_data = item_db.get(name)
            if item_data and item_data.get('category') == self.db_category and item_data.get('gear_type') == self.gear_type:
                 options.append(discord.SelectOption(label=f"{name} ({count}개)", value=name, emoji=item_data.get('emoji')))

        select = ui.Select(placeholder=gear_select_strings.get("placeholder", "{category_name} 선택...").format(category_name=self.category_name), options=options)
        select.callback = self.select_callback
        self.add_item(select)

        back_button = ui.Button(label=gear_select_strings.get("back_button", "뒤로"), style=discord.ButtonStyle.grey, row=1)
        back_button.callback = self.back_callback
        self.add_item(back_button)

        embed = discord.Embed(
            title=gear_select_strings.get("embed_title", "{category_name} 변경").format(category_name=self.category_name), 
            description=gear_select_strings.get("embed_description", "장착할 아이템을 선택해주세요."), 
            color=self.user.color
        )
        await interaction.edit_original_response(embed=embed, view=self)

    async def select_callback(self, interaction: discord.Interaction):
        selected_option = interaction.data['values'][0]
        if selected_option == "unequip":
            selected_item_name = self.default_item
            self.parent_view.status_message = f"✅ {self.category_name}을(를) 해제했습니다."
        else:
            selected_item_name = selected_option
            self.parent_view.status_message = f"✅ 장비를 **{selected_item_name}**(으)로 변경했습니다."
        await set_user_gear(str(self.user.id), **{self.gear_type: selected_item_name})
        await self.go_back_to_profile(interaction, reload_data=True)

    async def back_callback(self, interaction: discord.Interaction):
        await self.go_back_to_profile(interaction)

    async def go_back_to_profile(self, interaction: discord.Interaction, reload_data: bool = False):
        self.parent_view.current_page = "gear"
        await self.parent_view.update_display(interaction, reload_data=reload_data)

class UserProfilePanelView(ui.View):
    def __init__(self, cog_instance: 'UserProfile'):
        super().__init__(timeout=None)
        self.cog = cog_instance
        profile_button = ui.Button(label="소지품 보기", style=discord.ButtonStyle.primary, emoji="📦", custom_id="user_profile_open_button")
        profile_button.callback = self.open_profile
        self.add_item(profile_button)

    async def open_profile(self, interaction: discord.Interaction):
        view = ProfileView(interaction.user, self.cog)
        await view.build_and_send(interaction)

class UserProfile(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def register_persistent_views(self):
        self.bot.add_view(UserProfilePanelView(self))

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_profile"):
        panel_name = panel_key.replace("panel_", "")
        if (panel_info := get_panel_id(panel_name)):
            if (old_channel_id := panel_info.get("channel_id")) and (old_channel := self.bot.get_channel(old_channel_id)):
                try:
                    old_message = await old_channel.fetch_message(panel_info["message_id"])
                    await old_message.delete()
                except (discord.NotFound, discord.Forbidden): pass
        
        if not (embed_data := await get_embed_from_db(panel_key)): 
            logger.warning(f"DB에서 '{panel_key}' 임베드 데이터를 찾지 못해 패널 생성을 건너뜁니다.")
            return
            
        embed = discord.Embed.from_dict(embed_data)
        view = UserProfilePanelView(self)
        
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_name, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} 패널을 성공적으로 생성했습니다. (채널: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(UserProfile(bot))
