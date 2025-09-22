# cogs/games/dungeon.py

import discord
from discord.ext import commands, tasks
from discord import ui
import logging
import asyncio
import random
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any

from utils.database import (
    get_inventory, update_inventory, get_user_gear, supabase, get_id,
    save_panel_id, get_panel_id, get_embed_from_db, get_item_database
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

# --- 던전 및 몬스터 기본 데이터 ---

DUNGEON_TICKETS = {
    "초급 던전 입장권": "beginner",
    "중급 던전 입장권": "intermediate",
    "상급 던전 입장권": "advanced",
    "최상급 던전 입장권": "master",
}

DUNGEON_DATA = {
    "beginner": {"name": "초급 던전", "required_ticket": "초급 던전 입장권", "monster_level": 5, "monsters": ["불의 슬라임", "물의 슬라임", "풀의 슬라임"]},
    "intermediate": {"name": "중급 던전", "required_ticket": "중급 던전 입장권", "monster_level": 15, "monsters": ["불의 슬라임", "물의 슬라임", "풀의 슬라임", "전기 슬라임"]},
    "advanced": {"name": "상급 던전", "required_ticket": "상급 던전 입장권", "monster_level": 30, "monsters": ["전기 슬라임", "빛의 슬라임", "어둠의 슬라임"]},
    "master": {"name": "최상급 던전", "required_ticket": "최상급 던전 입장권", "monster_level": 50, "monsters": ["빛의 슬라임", "어둠의 슬라임", "골든 슬라임"]},
}

# 몬스터 이미지 URL은 제공해주신 것을 사용해야 합니다. 현재는 임시 URL입니다.
MONSTER_DATA = {
    "불의 슬라임":   {"element": "불", "base_hp": 30, "base_attack": 8, "image_url": "https://i.imgur.com/ffx3818.png"},
    "물의 슬라임":   {"element": "물", "base_hp": 40, "base_attack": 6, "image_url": "https://i.imgur.com/a4s2d2g.png"},
    "풀의 슬라임":   {"element": "풀", "base_hp": 35, "base_attack": 7, "image_url": "https://i.imgur.com/TOL31n0.png"},
    "전기 슬라임": {"element": "전기", "base_hp": 25, "base_attack": 10, "image_url": "https://i.imgur.com/x5S2j4a.png"},
    "빛의 슬라임":   {"element": "빛", "base_hp": 50, "base_attack": 5, "image_url": "https://i.imgur.com/sS2tW8Y.png"},
    "어둠의 슬라임": {"element": "어둠", "base_hp": 28, "base_attack": 12, "image_url": "https://i.imgur.com/N545W7d.png"},
    "골든 슬라임":   {"element": "빛", "base_hp": 100, "base_attack": 15, "image_url": "https://i.imgur.com/j1p1c2b.png"},
}

LOOT_TABLE = {
    "beginner": {"슬라임의 정수": (0.8, 1, 2), "하급 펫 경험치 물약": (0.1, 1, 1)},
    "intermediate": {"슬라임의 정수": (0.9, 1, 3), "하급 펫 경험치 물약": (0.2, 1, 2)},
    "advanced": {"응축된 슬라임 핵": (0.7, 1, 2), "중급 펫 경험치 물약": (0.15, 1, 1)},
    "master": {"응축된 슬라임 핵": (0.8, 2, 4), "상급 펫 경험치 물약": (0.2, 1, 1), "슬라임 왕관": (0.01, 1, 1)},
}

class DungeonGameView(ui.View):
    def __init__(self, cog: 'Dungeon', user: discord.Member, pet_data: Dict, dungeon_tier: str, end_time: datetime):
        super().__init__(timeout=(end_time - datetime.now(timezone.utc)).total_seconds())
        self.cog = cog; self.user = user; self.pet_data = pet_data; self.dungeon_tier = dungeon_tier; self.end_time = end_time
        
        self.state = "exploring" # exploring, in_battle, battle_over
        self.message: Optional[discord.Message] = None
        self.battle_log: List[str] = []
        self.rewards: Dict[str, int] = defaultdict(int)

        # 전투 관련 상태
        self.pet_current_hp: int = pet_data['current_hp']
        self.current_monster: Optional[Dict] = None
        self.monster_current_hp: int = 0
        
        self.build_components()

    async def start(self, interaction: discord.Interaction):
        embed = self.build_embed()
        self.message = await interaction.followup.send(embed=embed, view=self, ephemeral=True)

    def build_embed(self) -> discord.Embed:
        dungeon_info = DUNGEON_DATA[self.dungeon_tier]
        embed = discord.Embed(title=f"탐험 중... - {dungeon_info['name']}", color=0x71368A)
        embed.set_footer(text=f"던전은 {discord.utils.format_dt(self.end_time, 'R')}에 닫힙니다.")

        # 펫 상태 표시
        pet_hp_bar = f"{self.pet_current_hp} / {self.pet_data['current_hp']}"
        embed.add_field(name=f"🐾 {self.pet_data['nickname']}", value=f"❤️ {pet_hp_bar}", inline=False)
        
        if self.state == "exploring":
            embed.description = "깊은 곳으로 나아가 몬스터를 찾아보자."
        elif self.state == "in_battle" and self.current_monster:
            embed.title = f"전투 중! - {self.current_monster['name']}"
            embed.set_image(url=self.current_monster['image_url'])
            monster_hp_bar = f"{self.monster_current_hp} / {self.current_monster['hp']}"
            embed.add_field(name=f"몬스터: {self.current_monster['name']}", value=f"❤️ {monster_hp_bar}", inline=True)
            if self.battle_log:
                embed.add_field(name=" 전투 기록", value="```" + "\n".join(self.battle_log) + "```", inline=False)
        elif self.state == "battle_over":
            embed.title = "전투 종료"
            embed.description = "\n".join(self.battle_log)
        
        if self.rewards:
            rewards_str = "\n".join([f"> {item}: {qty}개" for item, qty in self.rewards.items()])
            embed.add_field(name="--- 현재까지 획득한 보상 ---", value=rewards_str, inline=False)
            
        return embed
    
    def build_components(self):
        self.clear_items()
        if self.state == "exploring":
            self.add_item(ui.Button(label="탐색하기", style=discord.ButtonStyle.success, emoji="🗺️", custom_id="explore"))
        elif self.state == "in_battle":
            self.add_item(ui.Button(label="공격", style=discord.ButtonStyle.primary, emoji="⚔️", custom_id="attack"))
            self.add_item(ui.Button(label="스킬", style=discord.ButtonStyle.secondary, emoji="✨", custom_id="skill", disabled=True)) # 스킬 기능은 추후 확장
            self.add_item(ui.Button(label="도망가기", style=discord.ButtonStyle.danger, emoji="🏃", custom_id="flee"))
        elif self.state == "battle_over":
            self.add_item(ui.Button(label="다음 탐색", style=discord.ButtonStyle.success, emoji="🗺️", custom_id="explore"))

        self.add_item(ui.Button(label="던전 나가기", style=discord.ButtonStyle.grey, emoji="🚪", custom_id="leave"))
        
        for item in self.children:
            item.callback = self.dispatch_callback

    async def refresh_ui(self, interaction: discord.Interaction):
        if interaction and not interaction.response.is_done():
            await interaction.response.defer()
        
        self.build_components()
        embed = self.build_embed()
        
        if self.message:
            await self.message.edit(embed=embed, view=self)

    async def dispatch_callback(self, interaction: discord.Interaction):
        action = interaction.data['custom_id']
        if action == "explore": await self.handle_explore(interaction)
        elif action == "attack": await self.handle_attack(interaction)
        elif action == "flee": await self.handle_flee(interaction)
        elif action == "leave": await self.handle_leave(interaction)
    
    async def handle_explore(self, interaction: discord.Interaction):
        dungeon_info = DUNGEON_DATA[self.dungeon_tier]
        monster_name = random.choice(dungeon_info['monsters'])
        monster_base = MONSTER_DATA[monster_name]
        
        level = dungeon_info['monster_level']
        hp = monster_base['base_hp'] + (level * 2)
        attack = monster_base['base_attack'] + level
        
        self.current_monster = {"name": monster_name, "hp": hp, "attack": attack, **monster_base}
        self.monster_current_hp = hp
        self.battle_log = [f"{monster_name} (Lv.{level}) 이(가) 나타났다!"]
        self.state = "in_battle"
        await self.refresh_ui(interaction)

    async def handle_attack(self, interaction: discord.Interaction):
        if self.state != "in_battle" or not self.current_monster: return
        
        # 전투 로직 (간단한 턴제)
        pet_atk = self.pet_data['current_attack']
        monster_atk = self.current_monster['attack']
        
        # 펫의 공격
        self.monster_current_hp -= pet_atk
        self.battle_log = [f"▶ {self.pet_data['nickname']}의 공격! {self.current_monster['name']}에게 {pet_atk}의 데미지!"]
        
        if self.monster_current_hp <= 0:
            await self.handle_battle_win(interaction)
            return

        # 몬스터의 공격
        self.pet_current_hp -= monster_atk
        self.battle_log.append(f"◀ {self.current_monster['name']}의 공격! {self.pet_data['nickname']}에게 {monster_atk}의 데미지!")

        if self.pet_current_hp <= 0:
            await self.handle_battle_lose(interaction)
            return
            
        await self.refresh_ui(interaction)

    async def handle_battle_win(self, interaction: discord.Interaction):
        self.state = "battle_over"
        self.battle_log.append(f"\n🎉 {self.current_monster['name']}을(를) 물리쳤다!")
        
        # 보상 및 경험치 처리
        pet_exp_gain = self.current_monster['hp'] // 2
        
        # RPC 호출로 펫 경험치 추가
        await supabase.rpc('add_xp_to_pet', {'p_user_id': self.user.id, 'p_xp_to_add': pet_exp_gain}).execute()
        self.battle_log.append(f"✨ 펫이 경험치 {pet_exp_gain}을 획득했다!")

        loot_table = LOOT_TABLE.get(self.dungeon_tier, {})
        for item, (chance, min_qty, max_qty) in loot_table.items():
            if random.random() < chance:
                qty = random.randint(min_qty, max_qty)
                self.rewards[item] += qty
                self.battle_log.append(f"🎁 {item} {qty}개를 획득했다!")

        self.current_monster = None
        await self.refresh_ui(interaction)

    async def handle_battle_lose(self, interaction: discord.Interaction):
        self.state = "battle_over"
        self.battle_log.append(f"\n☠️ {self.pet_data['nickname']}이(가) 쓰러졌다...")
        self.battle_log.append("체력이 모두 회복되었지만, 이번 전투의 보상은 없다.")
        
        # 펫 체력 회복
        self.pet_current_hp = self.pet_data['current_hp']
        self.current_monster = None
        await self.refresh_ui(interaction)

    async def handle_flee(self, interaction: discord.Interaction):
        self.state = "exploring"
        self.current_monster = None
        self.battle_log = ["무사히 도망쳤다..."]
        await self.refresh_ui(interaction)

    async def handle_leave(self, interaction: discord.Interaction):
        await self.cog.close_dungeon_session(self.user.id, self.rewards, interaction.channel)
        self.stop()
    
    async def on_timeout(self):
        await self.cog.close_dungeon_session(self.user.id, self.rewards)
        self.stop()

class DungeonPanelView(ui.View):
    def __init__(self, cog_instance: 'Dungeon'):
        super().__init__(timeout=None)
        self.cog = cog_instance
        
        for tier, data in DUNGEON_DATA.items():
            button = ui.Button(label=data['name'], style=discord.ButtonStyle.secondary, custom_id=f"enter_dungeon_{tier}")
            button.callback = self.dispatch_callback
            self.add_item(button)

    async def dispatch_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        tier = interaction.data['custom_id'].split('_')[-1]
        await self.cog.handle_enter_dungeon(interaction, tier)

class Dungeon(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_sessions: Dict[int, DungeonGameView] = {}
        self.check_expired_dungeons.start()

    def cog_unload(self):
        self.check_expired_dungeons.cancel()

    @tasks.loop(minutes=5)
    async def check_expired_dungeons(self):
        now = datetime.now(timezone.utc)
        res = await supabase.table('dungeon_sessions').select('*').lte('end_time', now.isoformat()).execute()
        if not (res and res.data): return

        for session in res.data:
            user_id = int(session['user_id'])
            if user_id not in self.active_sessions:
                logger.warning(f"DB에서 방치된 던전 세션(유저: {user_id})을 발견하여 종료합니다.")
                await self.close_dungeon_session(user_id, json.loads(session.get('rewards_json', '{}')))

    async def get_user_pet(self, user_id: int) -> Optional[Dict]:
        res = await supabase.table('pets').select('*, pet_species(*)').eq('user_id', user_id).gt('current_stage', 1).maybe_single().execute()
        return res.data if res and res.data else None

    async def handle_enter_dungeon(self, interaction: discord.Interaction, tier: str):
        user = interaction.user
        
        if user.id in self.active_sessions:
            return await interaction.followup.send("❌ 이미 다른 던전에 입장해 있습니다.", ephemeral=True)

        pet_data = await self.get_user_pet(user.id)
        if not pet_data:
            return await interaction.followup.send("❌ 던전에 입장하려면 펫이 필요합니다.", ephemeral=True)

        dungeon_info = DUNGEON_DATA[tier]
        ticket_name = dungeon_info['required_ticket']
        inventory = await get_inventory(user)
        
        if inventory.get(ticket_name, 0) < 1:
            return await interaction.followup.send(f"❌ '{ticket_name}'이 부족합니다.", ephemeral=True)

        try:
            thread = await interaction.channel.create_thread(name=f"🛡️｜{user.display_name}의 {dungeon_info['name']}", type=discord.ChannelType.private_thread)
        except Exception:
            return await interaction.followup.send("❌ 던전을 여는 데 실패했습니다.", ephemeral=True)
        
        await update_inventory(user.id, ticket_name, -1)
        await thread.add_user(user)

        end_time = datetime.now(timezone.utc) + timedelta(hours=24)
        
        await supabase.table('dungeon_sessions').upsert({
            "user_id": str(user.id), "thread_id": str(thread.id), "end_time": end_time.isoformat(), 
            "pet_id": pet_data['id'], "dungeon_tier": tier, "rewards_json": "{}"
        }, on_conflict="user_id").execute()

        view = DungeonGameView(self, user, pet_data, tier, end_time)
        self.active_sessions[user.id] = view
        
        await interaction.followup.send(f"던전에 입장했습니다! {thread.mention}", ephemeral=True)
        await view.start(interaction)

    async def close_dungeon_session(self, user_id: int, rewards: Dict, thread: Optional[discord.TextChannel] = None):
        if user_id in self.active_sessions:
            view = self.active_sessions.pop(user_id)
            if not view.is_finished(): view.stop()

        res = await supabase.table('dungeon_sessions').select('*').eq('user_id', str(user_id)).maybe_single().execute()
        session_data = res.data if res and res.data else None
        
        if not session_data:
            logger.warning(f"[{user_id}] 종료할 던전 세션이 DB에 없습니다 (이미 처리됨).")
            return
            
        await supabase.table('dungeon_sessions').delete().eq('user_id', str(user_id)).execute()
        
        user = self.bot.get_user(user_id)
        if user and rewards:
            for item, qty in rewards.items():
                await update_inventory(user.id, item, qty)

            rewards_text = "\n".join([f"> {item}: {qty}개" for item, qty in rewards.items()]) or "> 획득한 아이템이 없습니다."
            
            embed_data = await get_embed_from_db("log_dungeon_result") or {"title": "🛡️ 던전 탐사 결과", "color": 0x71368A}
            log_embed = format_embed_from_db(
                embed_data, 
                user_mention=user.mention, 
                dungeon_name=DUNGEON_DATA[session_data['dungeon_tier']]['name'], 
                rewards_list=rewards_text
            )
            if user.display_avatar: log_embed.set_thumbnail(url=user.display_avatar.url)
            
            panel_channel_id = get_id("dungeon_panel_channel_id")
            if panel_channel_id and (panel_channel := self.bot.get_channel(panel_channel_id)):
                await panel_channel.send(embed=log_embed)
        
        try:
            if not thread:
                thread = self.bot.get_channel(int(session_data['thread_id'])) or await self.bot.fetch_channel(int(session_data['thread_id']))
            await thread.send("**던전이 닫혔습니다.**", delete_after=10)
            await asyncio.sleep(1)
            await thread.delete()
        except (discord.NotFound, discord.Forbidden): pass

    async def register_persistent_views(self):
        self.bot.add_view(DungeonPanelView(self))
    
    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_dungeon"):
        if panel_info := get_panel_id(panel_key):
            try:
                if old_channel := self.bot.get_channel(panel_info['channel_id']):
                    msg = await old_channel.fetch_message(panel_info['message_id'])
                    await msg.delete()
            except (discord.NotFound, discord.Forbidden): pass

        embed_data = await get_embed_from_db(panel_key)
        if not embed_data: return
        
        embed = format_embed_from_db(embed_data)
        view = DungeonPanelView(self)
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_key, new_message.id, channel.id)

async def setup(bot: commands.Bot):
    await bot.add_cog(Dungeon(bot))
