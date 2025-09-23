# cogs/games/dungeon.py

import discord
from discord.ext import commands, tasks
from discord import ui
import logging
import asyncio
import random
import json
import os
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any
from collections import defaultdict

from utils.database import (
    get_inventory, update_inventory, supabase, get_id,
    save_panel_id, get_panel_id, get_embed_from_db, get_item_database
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

# --- 던전 및 몬스터 데이터 ---

DUNGEON_TICKETS = {
    "초급 던전 입장권": "beginner", "중급 던전 입장권": "intermediate",
    "상급 던전 입장권": "advanced", "최상급 던전 입장권": "master",
}

DUNGEON_TIER_MAP = {
    "beginner": "초급", "intermediate": "중급", "advanced": "상급", "master": "최상급"
}

TIER_MODIFIERS = {
    "beginner":     {"hp_mult": 1.0, "atk_mult": 1.0, "def_mult": 1.0, "spd_mult": 1.0, "xp_mult": 1.0, "image_suffix": "beginner"},
    "intermediate": {"hp_mult": 2.5, "atk_mult": 2.5, "def_mult": 2.0, "spd_mult": 1.2, "xp_mult": 2.0, "image_suffix": "intermediate"},
    "advanced":     {"hp_mult": 5.0, "atk_mult": 5.0, "def_mult": 4.0, "spd_mult": 1.5, "xp_mult": 4.0, "image_suffix": "advanced"},
    "master":       {"hp_mult": 10.0, "atk_mult": 10.0, "def_mult": 8.0, "spd_mult": 2.0, "xp_mult": 8.0, "image_suffix": "master"},
}

DUNGEON_DATA = {
    "beginner":     {"name": "초급 던전", "elements": ["fire", "water", "grass"]},
    "intermediate": {"name": "중급 던전", "elements": ["fire", "water", "grass", "electric"]},
    "advanced":     {"name": "상급 던전", "elements": ["electric", "light", "dark"]},
    "master":       {"name": "최상급 던전", "elements": ["light", "dark"]},
}

MONSTER_BASE_DATA = {
    "fire":     {"name": "불의 슬라임",   "base_hp": 30, "base_attack": 12, "base_defense": 2, "base_speed": 4},
    "water":    {"name": "물의 슬라임",   "base_hp": 40, "base_attack": 6,  "base_defense": 8, "base_speed": 2},
    "grass":    {"name": "풀의 슬라임",   "base_hp": 50, "base_attack": 7,  "base_defense": 4, "base_speed": 3},
    "electric": {"name": "전기 슬라임", "base_hp": 25, "base_attack": 8,  "base_defense": 2, "base_speed": 12},
    "light":    {"name": "빛의 슬라임",   "base_hp": 45, "base_attack": 5,  "base_defense": 7, "base_speed": 3},
    "dark":     {"name": "어둠의 슬라임", "base_hp": 28, "base_attack": 11, "base_defense": 2, "base_speed": 9},
}

LOOT_TABLE = {
    "beginner":     {"슬라임의 정수": (0.8, 1, 2)},
    "intermediate": {"슬라임의 정수": (0.9, 1, 3)},
    "advanced":     {"응축된 슬라임 핵": (0.7, 1, 2)},
    "master":       {"응축된 슬라임 핵": (0.8, 2, 4), "슬라임 왕관": (0.01, 1, 1)},
}

class DungeonGameView(ui.View):
    def __init__(self, cog: 'Dungeon', user: discord.Member, pet_data: Dict, dungeon_tier: str, end_time: datetime):
        super().__init__(timeout=(end_time - datetime.now(timezone.utc)).total_seconds() + 30)
        self.cog = cog; self.user = user; self.pet_data_raw = pet_data; self.dungeon_tier = dungeon_tier; self.end_time = end_time
        
        self.final_pet_stats = self._calculate_final_pet_stats()
        
        self.state = "exploring"; self.message: Optional[discord.Message] = None
        self.battle_log: List[str] = []; self.rewards: Dict[str, int] = defaultdict(int)

        self.pet_current_hp: int = self.final_pet_stats['hp']
        self.current_monster: Optional[Dict] = None; self.monster_current_hp: int = 0
        self.is_pet_turn: bool = True
        
        self.storage_base_url = f"{os.environ.get('SUPABASE_URL')}/storage/v1/object/public/monster_images"

        self.build_components()

    def _calculate_final_pet_stats(self) -> Dict[str, int]:
        species_info = self.pet_data_raw.get('pet_species', {})
        level = self.pet_data_raw.get('level', 1)
        stats = {}
        for key in ['hp', 'attack', 'defense', 'speed']:
            base = species_info.get(f'base_{key}', 0) + (level - 1) * species_info.get(f'{key}_growth', 0)
            natural_bonus = self.pet_data_raw.get(f"natural_bonus_{key}", 0)
            allocated = self.pet_data_raw.get(f"allocated_{key}", 0)
            stats[key] = round(base) + natural_bonus + allocated
        return stats

    async def start(self, thread: discord.Thread):
        embed = self.build_embed()
        self.message = await thread.send(embed=embed, view=self)

    def generate_monster(self) -> Dict:
        dungeon_info = DUNGEON_DATA[self.dungeon_tier]
        tier_modifier = TIER_MODIFIERS[self.dungeon_tier]
        element = random.choice(dungeon_info['elements'])
        base_monster = MONSTER_BASE_DATA[element]
        hp = int(base_monster['base_hp'] * tier_modifier['hp_mult'])
        attack = int(base_monster['base_attack'] * tier_modifier['atk_mult'])
        defense = int(base_monster['base_defense'] * tier_modifier['def_mult'])
        speed = int(base_monster['base_speed'] * tier_modifier['spd_mult'])
        xp = int(hp * tier_modifier['xp_mult'])
        image_url = f"{self.storage_base_url}/{element}_{tier_modifier['image_suffix']}.png"
        logger.info(f"[Dungeon] Generating monster image URL: {image_url}")
        return {"name": f"{DUNGEON_TIER_MAP[self.dungeon_tier]} {base_monster['name']}", "hp": hp, "attack": attack, "defense": defense, "speed": speed, "xp": xp, "element": element, "image_url": image_url}

    def build_embed(self) -> discord.Embed:
        dungeon_info = DUNGEON_DATA[self.dungeon_tier]
        embed = discord.Embed(title=f"탐험 중... - {dungeon_info['name']}", color=0x71368A)
        description_content = ""
        pet_stats = (f"❤️ **체력**: {self.pet_current_hp} / {self.final_pet_stats['hp']}\n"
                     f"⚔️ **공격력**: {self.final_pet_stats['attack']}\n"
                     f"🛡️ **방어력**: {self.final_pet_stats['defense']}\n"
                     f"💨 **스피드**: {self.final_pet_stats['speed']}")
        embed.add_field(name=f"🐾 {self.pet_data_raw['nickname']}", value=pet_stats, inline=False)
        if self.state == "exploring":
            description_content = "깊은 곳으로 나아가 몬스터를 찾아보자."
        elif self.state == "in_battle" and self.current_monster:
            embed.title = f"전투 중! - {self.current_monster['name']}"
            embed.set_image(url=self.current_monster['image_url'])
            monster_stats = (f"❤️ **체력**: {self.monster_current_hp} / {self.current_monster['hp']}\n"
                             f"⚔️ **공격력**: {self.current_monster['attack']}\n"
                             f"🛡️ **방어력**: {self.current_monster['defense']}\n"
                             f"💨 **스피드**: {self.current_monster['speed']}")
            embed.add_field(name=f"몬스터: {self.current_monster['name']}", value=monster_stats, inline=False)
            if self.battle_log:
                embed.add_field(name="⚔️ 전투 기록", value="```" + "\n".join(self.battle_log) + "```", inline=False)
        elif self.state == "battle_over":
            embed.title = "전투 종료"
            description_content = "```\n" + "\n".join(self.battle_log) + "\n```"
            if self.current_monster and self.current_monster.get('image_url'):
                embed.set_thumbnail(url=self.current_monster['image_url'])
        if self.rewards:
            rewards_str = "\n".join([f"> {item}: {qty}개" for item, qty in self.rewards.items()])
            embed.add_field(name="--- 현재까지 획득한 보상 ---", value=rewards_str, inline=False)
        closing_time_text = f"\n\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n던전은 {discord.utils.format_dt(self.end_time, 'R')}에 닫힙니다."
        embed.description = (description_content + closing_time_text) if description_content else closing_time_text.strip()
        return embed
    
    def build_components(self):
        self.clear_items()
        if self.state in ["exploring", "battle_over"]:
            self.add_item(ui.Button(label="탐색하기", style=discord.ButtonStyle.success, emoji="🗺️", custom_id="explore"))
        elif self.state == "in_battle":
            self.add_item(ui.Button(label="공격", style=discord.ButtonStyle.primary, emoji="⚔️", custom_id="attack"))
            self.add_item(ui.Button(label="스킬", style=discord.ButtonStyle.secondary, emoji="✨", custom_id="skill", disabled=True))
            self.add_item(ui.Button(label="도망가기", style=discord.ButtonStyle.danger, emoji="🏃", custom_id="flee"))
        self.add_item(ui.Button(label="던전 나가기", style=discord.ButtonStyle.grey, emoji="🚪", custom_id="leave"))
        for item in self.children: item.callback = self.dispatch_callback

    async def refresh_ui(self, interaction: Optional[discord.Interaction] = None):
        if interaction and not interaction.response.is_done(): await interaction.response.defer()
        self.build_components(); embed = self.build_embed()
        if self.message:
            try: await self.message.edit(embed=embed, view=self)
            except discord.NotFound: self.stop()

    async def dispatch_callback(self, interaction: discord.Interaction):
        action = interaction.data['custom_id']
        method_map = {"explore": self.handle_explore, "attack": self.handle_attack, "flee": self.handle_flee, "leave": self.handle_leave}
        if method := method_map.get(action): await method(interaction)
    
    async def handle_explore(self, interaction: discord.Interaction):
        self.current_monster = self.generate_monster(); self.monster_current_hp = self.current_monster['hp']
        self.battle_log = [f"{self.current_monster['name']} 이(가) 나타났다!"]
        pet_speed = self.final_pet_stats['speed']; monster_speed = self.current_monster.get('speed', 0)
        if pet_speed >= monster_speed:
            self.is_pet_turn = True; self.battle_log.append(f"{self.pet_data_raw['nickname']}이(가) 민첩하게 먼저 움직인다!")
        else:
            self.is_pet_turn = False; self.battle_log.append(f"{self.current_monster['name']}의 기습 공격!"); await self._execute_monster_turn()
        self.state = "in_battle"; await self.refresh_ui(interaction)

    async def _execute_pet_turn(self):
        damage = max(1, self.final_pet_stats['attack'] - self.current_monster.get('defense', 0))
        self.monster_current_hp = max(0, self.monster_current_hp - damage)
        self.battle_log.append(f"▶ {self.pet_data_raw['nickname']}의 공격! {damage}의 데미지!")

    async def _execute_monster_turn(self):
        damage = max(1, self.current_monster.get('attack', 1) - self.final_pet_stats['defense'])
        self.pet_current_hp = max(0, self.pet_current_hp - damage)
        self.battle_log.append(f"◀ {self.current_monster['name']}의 공격! {damage}의 데미지!")

    async def handle_attack(self, interaction: discord.Interaction):
        if self.state != "in_battle" or not self.current_monster: return
        self.battle_log = []
        await self._execute_pet_turn()
        if self.monster_current_hp <= 0: return await self.handle_battle_win(interaction)
        await self._execute_monster_turn()
        if self.pet_current_hp <= 0: return await self.handle_battle_lose(interaction)
        await self.refresh_ui(interaction)

    async def handle_battle_win(self, interaction: discord.Interaction):
        self.state = "battle_over"; self.battle_log.append(f"\n🎉 {self.current_monster['name']}을(를) 물리쳤다!")
        pet_exp_gain = self.current_monster['xp']
        await supabase.rpc('add_xp_to_pet', {'p_user_id': self.user.id, 'p_xp_to_add': pet_exp_gain}).execute()
        self.battle_log.append(f"✨ 펫이 경험치 {pet_exp_gain}을 획득했다!")
        for item, (chance, min_qty, max_qty) in LOOT_TABLE.get(self.dungeon_tier, {}).items():
            if random.random() < chance:
                qty = random.randint(min_qty, max_qty)
                self.rewards[item] += qty; self.battle_log.append(f"🎁 {item} {qty}개를 획득했다!")
        await self.refresh_ui(interaction)

    async def handle_battle_lose(self, interaction: discord.Interaction):
        self.state = "battle_over"; self.battle_log.append(f"\n☠️ {self.pet_data_raw['nickname']}이(가) 쓰러졌다..."); self.battle_log.append("체력이 모두 회복되었지만, 이번 전투의 보상은 없다.")
        self.pet_current_hp = self.final_pet_stats['hp']; self.current_monster = None
        await self.refresh_ui(interaction)

    async def handle_flee(self, interaction: discord.Interaction):
        self.state = "exploring"; self.current_monster = None; self.battle_log = ["무사히 도망쳤다..."]
        await self.refresh_ui(interaction)

    async def handle_leave(self, interaction: discord.Interaction):
        await self.cog.close_dungeon_session(self.user.id, self.rewards, interaction.channel)

    async def on_timeout(self): await self.cog.close_dungeon_session(self.user.id, self.rewards)
    def stop(self):
        if self.cog and self.user: self.cog.active_sessions.pop(self.user.id, None)
        super().stop()

class Dungeon(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot; self.active_sessions: Dict[int, DungeonGameView] = {}
        self.check_expired_dungeons.start()

    def cog_unload(self): self.check_expired_dungeons.cancel()

    @tasks.loop(minutes=5)
    async def check_expired_dungeons(self):
        now = datetime.now(timezone.utc)
        res = await supabase.table('dungeon_sessions').select('*').lte('end_time', now.isoformat()).execute()
        if res and res.data:
            for session in res.data:
                user_id = int(session['user_id'])
                if user_id not in self.active_sessions:
                    logger.warning(f"DB에서 방치된 던전 세션(유저: {user_id})을 발견하여 종료합니다.")
                    await self.close_dungeon_session(user_id, json.loads(session.get('rewards_json', '{}')))
    
    @check_expired_dungeons.before_loop
    async def before_check_expired_dungeons(self): await self.bot.wait_until_ready()
    
    async def get_user_pet(self, user_id: int) -> Optional[Dict]:
        res = await supabase.table('pets').select('*, pet_species(*)').eq('user_id', user_id).gt('current_stage', 1).maybe_single().execute()
        return res.data if res and res.data else None

    async def handle_enter_dungeon(self, interaction: discord.Interaction, tier: str):
        user = interaction.user
        res = await supabase.table('dungeon_sessions').select('thread_id').eq('user_id', str(user.id)).maybe_single().execute()
        if res and res.data and (thread := self.bot.get_channel(int(res.data['thread_id']))):
            return await interaction.followup.send(f"❌ 이미 던전에 입장해 있습니다. {thread.mention}", ephemeral=True)
        pet_data = await self.get_user_pet(user.id)
        if not pet_data: return await interaction.followup.send("❌ 던전에 입장하려면 펫이 필요합니다.", ephemeral=True)
        dungeon_name = DUNGEON_DATA[tier]['name']; ticket_name = f"{dungeon_name} 입장권"
        if (await get_inventory(user)).get(ticket_name, 0) < 1: return await interaction.followup.send(f"❌ '{ticket_name}'이 부족합니다.", ephemeral=True)
        try:
            thread = await interaction.channel.create_thread(name=f"🛡️｜{user.display_name}의 {dungeon_name}", type=discord.ChannelType.private_thread, auto_archive_duration=1440)
        except Exception: return await interaction.followup.send("❌ 던전을 여는 데 실패했습니다.", ephemeral=True)
        await update_inventory(user.id, ticket_name, -1); await thread.add_user(user)
        end_time = datetime.now(timezone.utc) + timedelta(hours=24)
        await supabase.table('dungeon_sessions').upsert({"user_id": str(user.id), "thread_id": str(thread.id), "end_time": end_time.isoformat(), "pet_id": pet_data['id'], "dungeon_tier": tier, "rewards_json": "{}"}, on_conflict="user_id").execute()
        view = DungeonGameView(self, user, pet_data, tier, end_time)
        self.active_sessions[user.id] = view
        await interaction.followup.send(f"던전에 입장했습니다! {thread.mention}", ephemeral=True)
        await view.start(thread)

    async def close_dungeon_session(self, user_id: int, rewards: Dict, thread: Optional[discord.TextChannel] = None):
        if user_id in self.active_sessions:
            view = self.active_sessions.pop(user_id, None)
            if view and not view.is_finished(): view.stop()
        session_res = await supabase.table('dungeon_sessions').select('*').eq('user_id', str(user_id)).maybe_single().execute()
        if not (session_res and session_res.data): return
        session_data = session_res.data
        await supabase.table('dungeon_sessions').delete().eq('user_id', str(user_id)).execute()
        user = self.bot.get_user(user_id)
        panel_channel = self.bot.get_channel(get_id("dungeon_panel_channel_id")) if get_id("dungeon_panel_channel_id") else None
        if user and rewards:
            await asyncio.gather(*[update_inventory(user.id, item, qty) for item, qty in rewards.items()])
            rewards_text = "\n".join([f"> {item}: {qty}개" for item, qty in rewards.items()]) or "> 획득한 아이템이 없습니다."
            embed_data = await get_embed_from_db("log_dungeon_result")
            log_embed = format_embed_from_db(embed_data, user_mention=user.mention, dungeon_name=DUNGEON_DATA[session_data['dungeon_tier']]['name'], rewards_list=rewards_text)
            if user.display_avatar: log_embed.set_thumbnail(url=user.display_avatar.url)
            if panel_channel: await panel_channel.send(embed=log_embed)
        try:
            if not thread: thread = self.bot.get_channel(int(session_data['thread_id'])) or await self.bot.fetch_channel(int(session_data['thread_id']))
            await thread.send("**던전이 닫혔습니다. 이 채널은 5초 후에 삭제됩니다.**", delete_after=5)
            await asyncio.sleep(5); await thread.delete()
        except (discord.NotFound, discord.Forbidden): pass
        if panel_channel: await self.regenerate_panel(panel_channel)

    async def register_persistent_views(self): self.bot.add_view(DungeonPanelView(self))
    
    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_dungeon"):
        panel_name = panel_key.replace("panel_", "")
        if panel_info := get_panel_id(panel_name):
            if msg_id := panel_info.get('message_id'):
                try:
                    old_message = await channel.fetch_message(msg_id)
                    await old_message.delete()
                except (discord.NotFound, discord.Forbidden): pass
        if embed_data := await get_embed_from_db(panel_key):
            new_message = await channel.send(embed=format_embed_from_db(embed_data), view=DungeonPanelView(self))
            await save_panel_id(panel_name, new_message.id, channel.id)

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

async def setup(bot: commands.Bot):
    await bot.add_cog(Dungeon(bot))
