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
    save_panel_id, get_panel_id, get_embed_from_db,
    get_item_database # [수정] 누락되었던 import를 정확하게 추가했습니다.
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

async def load_dungeon_data_from_db() -> Dict[str, Any]:
    try:
        dungeons_res, monsters_res, loot_res = await asyncio.gather(
            supabase.table('dungeons').select('*').order('recommended_level').execute(),
            supabase.table('monster_species').select('*').execute(),
            supabase.table('dungeon_loot').select('*').execute()
        )
        dungeon_data = {d['tier_key']: d for d in dungeons_res.data} if dungeons_res.data else {}
        monster_base_data = {m['element_key']: m for m in monsters_res.data} if monsters_res.data else {}
        loot_table = defaultdict(dict)
        if loot_res.data:
            for item in loot_res.data:
                loot_table[item['dungeon_tier']][item['item_name']] = (item['drop_chance'], item['min_qty'], item['max_qty'])
        logger.info(f"✅ 던전 데이터 로드 완료: 던전({len(dungeon_data)}), 몬스터({len(monster_base_data)}), 보상({len(loot_table)})")
        return {"dungeons": dungeon_data, "monsters": monster_base_data, "loot": dict(loot_table)}
    except Exception as e:
        logger.error(f"❌ 던전 데이터 DB 로드 실패: {e}", exc_info=True)
        return {"dungeons": {}, "monsters": {}, "loot": {}}

class DungeonGameView(ui.View):
    # ... (내부 코드는 변경 없음, 생략) ...
    def __init__(self, cog: 'Dungeon', user: discord.Member, pet_data: Dict, dungeon_tier: str, end_time: datetime):
        super().__init__(timeout=(end_time - datetime.now(timezone.utc)).total_seconds() + 30)
        self.cog = cog; self.user = user; self.pet_data_raw = pet_data; self.dungeon_tier = dungeon_tier; self.end_time = end_time
        self.final_pet_stats = self._calculate_final_pet_stats()
        self.state = "exploring"; self.message: Optional[discord.Message] = None
        self.battle_log: List[str] = []; self.rewards: Dict[str, int] = defaultdict(int)
        self.total_pet_xp_gained: int = 0
        self.pet_current_hp: int = self.final_pet_stats['hp']
        self.pet_is_defeated: bool = False
        self.current_monster: Optional[Dict] = None; self.monster_current_hp: int = 0
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
        dungeon_info = self.cog.dungeon_data[self.dungeon_tier]
        element = random.choice(dungeon_info['elements'])
        base_monster = self.cog.monster_base_data[element]
        monster_level = random.randint(dungeon_info.get('min_monster_level', 1), dungeon_info.get('max_monster_level', 5))
        hp_bonus = (monster_level - 1) * 8; other_stat_bonus = (monster_level - 1) * 5
        hp = int(base_monster['base_hp'] * dungeon_info['hp_mult']) + hp_bonus
        attack = int(base_monster['base_attack'] * dungeon_info['atk_mult']) + other_stat_bonus
        defense = int(base_monster['base_defense'] * dungeon_info['def_mult']) + other_stat_bonus
        speed = int(base_monster['base_speed'] * dungeon_info['spd_mult']) + other_stat_bonus
        xp = max(1, int(hp * dungeon_info['xp_mult']) // 20) + (monster_level * 2)
        image_url = f"{self.storage_base_url}/{element}_{dungeon_info['image_suffix']}.png"
        return {"name": f"Lv.{monster_level} {dungeon_info['name'].replace('던전', '')} {base_monster['name']}", "hp": hp, "attack": attack, "defense": defense, "speed": speed, "xp": xp, "element": element, "image_url": image_url}

    def build_embed(self) -> discord.Embed:
        dungeon_info = self.cog.dungeon_data[self.dungeon_tier]
        embed = discord.Embed(title=f"탐험 중... - {dungeon_info['name']}", color=0x71368A)
        description_content = ""
        pet_stats = (f"❤️ **체력**: {self.pet_current_hp} / {self.final_pet_stats['hp']}\n"
                     f"⚔️ **공격력**: {self.final_pet_stats['attack']}\n"
                     f"🛡️ **방어력**: {self.final_pet_stats['defense']}\n"
                     f"💨 **스피드**: {self.final_pet_stats['speed']}")
        embed.add_field(name=f"🐾 {self.pet_data_raw['nickname']}", value=pet_stats, inline=False)
        if self.pet_is_defeated:
            description_content = "☠️ 펫이 쓰러졌습니다! '아이템'을 사용해 '치료제'로 회복시키거나 던전을 나가야 합니다."
        elif self.state == "exploring":
            description_content = "깊은 곳으로 나아가 몬스터를 찾아보자."
        elif self.state == "in_battle" and self.current_monster:
            embed.title = f"전투 중! - {self.current_monster['name']}"; embed.set_image(url=self.current_monster['image_url'])
            monster_stats = (f"❤️ **체력**: {self.monster_current_hp} / {self.current_monster['hp']}\n"
                             f"⚔️ **공격력**: {self.current_monster['attack']}\n"
                             f"🛡️ **방어력**: {self.current_monster['defense']}\n"
                             f"💨 **스피드**: {self.current_monster['speed']}")
            embed.add_field(name=f"몬스터: {self.current_monster['name']}", value=monster_stats, inline=False)
            if self.battle_log: embed.add_field(name="⚔️ 전투 기록", value="```" + "\n".join(self.battle_log) + "```", inline=False)
        elif self.state == "battle_over":
            embed.title = "전투 종료"; description_content = "```\n" + "\n".join(self.battle_log) + "\n```"
            if self.current_monster and self.current_monster.get('image_url'): embed.set_thumbnail(url=self.current_monster['image_url'])
        if self.rewards:
            rewards_str = "\n".join([f"> {item}: {qty}개" for item, qty in self.rewards.items()])
            embed.add_field(name="--- 현재까지 획득한 보상 ---", value=rewards_str, inline=False)
        closing_time_text = f"\n\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n던전은 {discord.utils.format_dt(self.end_time, 'R')}에 닫힙니다."
        embed.description = (description_content + closing_time_text) if description_content else closing_time_text.strip()
        return embed
    
    def build_components(self):
        self.clear_items()
        if self.state in ["exploring", "battle_over"] or self.pet_is_defeated:
            self.add_item(ui.Button(label="탐색하기", style=discord.ButtonStyle.success, emoji="🗺️", custom_id="explore", disabled=self.pet_is_defeated))
            self.add_item(ui.Button(label="아이템", style=discord.ButtonStyle.secondary, emoji="👜", custom_id="use_item"))
        elif self.state == "in_battle":
            self.add_item(ui.Button(label="공격", style=discord.ButtonStyle.primary, emoji="⚔️", custom_id="attack"))
            self.add_item(ui.Button(label="아이템", style=discord.ButtonStyle.secondary, emoji="👜", custom_id="use_item"))
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
        method_map = {"explore": self.handle_explore, "attack": self.handle_attack, "flee": self.handle_flee, "leave": self.handle_leave, "use_item": self.handle_use_item}
        if method := method_map.get(action): await method(interaction)
    
    async def handle_explore(self, interaction: discord.Interaction):
        if self.pet_is_defeated: return await interaction.response.send_message("펫이 쓰러져서 탐색할 수 없습니다.", ephemeral=True, delete_after=5)
        self.current_monster = self.generate_monster(); self.monster_current_hp = self.current_monster['hp']
        self.battle_log = [f"{self.current_monster['name']} 이(가) 나타났다!"]
        if self.final_pet_stats['speed'] >= self.current_monster.get('speed', 0):
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
        self.total_pet_xp_gained += pet_exp_gain
        await supabase.rpc('add_xp_to_pet', {'p_user_id': self.user.id, 'p_xp_to_add': pet_exp_gain}).execute()
        self.battle_log.append(f"✨ 펫이 경험치 {pet_exp_gain}을 획득했다!")
        for item, (chance, min_qty, max_qty) in self.cog.loot_table.get(self.dungeon_tier, {}).items():
            if random.random() < chance:
                qty = random.randint(min_qty, max_qty)
                self.rewards[item] += qty; self.battle_log.append(f"🎁 {item} {qty}개를 획득했다!")
        await self.refresh_ui(interaction)

    async def handle_battle_lose(self, interaction: discord.Interaction):
        self.state = "battle_over"; self.pet_is_defeated = True
        self.battle_log.append(f"\n☠️ {self.pet_data_raw['nickname']}이(가) 쓰러졌다..."); self.current_monster = None
        await self.refresh_ui(interaction)

    async def handle_flee(self, interaction: discord.Interaction):
        self.state = "exploring"; self.current_monster = None; self.battle_log = ["무사히 도망쳤다..."]
        await self.refresh_ui(interaction)

    async def handle_leave(self, interaction: discord.Interaction):
        await interaction.response.send_message("던전에서 나가는 중입니다...", ephemeral=True, delete_after=5)
        await self.cog.close_dungeon_session(self.user.id, self.rewards, self.total_pet_xp_gained, interaction.channel)

    async def handle_use_item(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        inventory = await get_inventory(self.user); usable_items = []
        for name, qty in inventory.items():
            item_data = self.cog.item_db.get(name, {}); effect = item_data.get('effect_type')
            if effect == 'pet_revive' and self.pet_is_defeated: usable_items.append(discord.SelectOption(label=f"{name} ({qty}개)", value=name, emoji="💊"))
            elif effect == 'pet_heal' and not self.pet_is_defeated and self.pet_current_hp < self.final_pet_stats['hp']: usable_items.append(discord.SelectOption(label=f"{name} ({qty}개)", value=name, emoji="🧪"))
        if not usable_items: return await interaction.followup.send("사용할 수 있는 아이템이 없습니다.", ephemeral=True, delete_after=5)
        select = ui.Select(placeholder="사용할 아이템을 선택하세요...", options=usable_items)
        async def on_item_select(select_interaction: discord.Interaction):
            await select_interaction.response.defer()
            item_name = select_interaction.data['values'][0]; item_data = self.cog.item_db.get(item_name, {}); effect = item_data.get('effect_type')
            await update_inventory(self.user.id, item_name, -1)
            if effect == 'pet_revive':
                self.pet_is_defeated = False; self.pet_current_hp = self.final_pet_stats['hp']; self.state = "exploring"; self.battle_log = [f"💊 '{item_name}'을(를) 사용해 펫이 완전히 회복되었다!"]
            elif effect == 'pet_heal':
                heal_amount = item_data.get('power', 0); self.pet_current_hp = min(self.final_pet_stats['hp'], self.pet_current_hp + heal_amount); self.battle_log = [f"🧪 '{item_name}'을(를) 사용해 체력을 {heal_amount} 회복했다!"]
                if self.state == "in_battle":
                    await self._execute_monster_turn()
                    if self.pet_current_hp <= 0: return await self.handle_battle_lose(interaction)
            await self.refresh_ui(); await select_interaction.delete_original_response()
        select.callback = on_item_select
        view = ui.View(timeout=60).add_item(select)
        await interaction.followup.send(view=view, ephemeral=True)

    async def on_timeout(self): await self.cog.close_dungeon_session(self.user.id, self.rewards, self.total_pet_xp_gained)
    def stop(self):
        if self.cog and self.user: self.cog.active_sessions.pop(self.user.id, None)
        super().stop()

class Dungeon(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot; self.active_sessions: Dict[int, DungeonGameView] = {}
        self.dungeon_data: Dict = {}; self.monster_base_data: Dict = {}; self.loot_table: Dict = {}; self.item_db: Dict = {}
        self.check_expired_dungeons.start()

    async def cog_load(self):
        data = await load_dungeon_data_from_db()
        self.dungeon_data = data["dungeons"]; self.monster_base_data = data["monsters"]; self.loot_table = data["loot"]
        self.item_db = get_item_database()

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
        dungeon_name = self.dungeon_data[tier]['name']; ticket_name = f"{dungeon_name} 입장권"
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

    async def close_dungeon_session(self, user_id: int, rewards: Dict, total_xp: int = 0, thread: Optional[discord.TextChannel] = None):
        if user_id in self.active_sessions:
            view = self.active_sessions.pop(user_id, None)
            if view and not view.is_finished():
                if total_xp == 0: total_xp = view.total_pet_xp_gained
                view.stop()
        session_res = await supabase.table('dungeon_sessions').select('*').eq('user_id', str(user_id)).maybe_single().execute()
        if not (session_res and session_res.data): return
        session_data = session_res.data
        await supabase.table('dungeon_sessions').delete().eq('user_id', str(user_id)).execute()
        user = self.bot.get_user(user_id)
        panel_channel = self.bot.get_channel(get_id("dungeon_panel_channel_id")) if get_id("dungeon_panel_channel_id") else None
        
        if user and (rewards or total_xp > 0):
            if rewards:
                await asyncio.gather(*[update_inventory(user.id, item, qty) for item, qty in rewards.items()])
            rewards_text = "\n".join([f"> {item}: {qty}개" for item, qty in rewards.items()]) or "> 획득한 아이템이 없습니다."
            embed_data = await get_embed_from_db("log_dungeon_result")
            log_embed = format_embed_from_db(embed_data, user_mention=user.mention, dungeon_name=self.dungeon_data[session_data['dungeon_tier']]['name'], rewards_list=rewards_text, pet_xp_gained=f"{total_xp:,}")
            if user.display_avatar: log_embed.set_thumbnail(url=user.display_avatar.url)
            if panel_channel: await panel_channel.send(embed=log_embed)
        
        try:
            if not thread: thread = self.bot.get_channel(int(session_data['thread_id'])) or await self.bot.fetch_channel(int(session_data['thread_id']))
            await thread.send("**던전이 닫혔습니다. 이 채널은 5초 후에 삭제됩니다.**", delete_after=5)
            await asyncio.sleep(5); await thread.delete()
        except (discord.NotFound, discord.Forbidden): pass
        
        if panel_channel: await self.regenerate_panel(panel_channel)

    @commands.Cog.listener()
    async def on_ready(self):
        self.bot.add_view(await DungeonPanelView.create(self))
    
    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_dungeon"):
        panel_name = panel_key.replace("panel_", "")
        if panel_info := get_panel_id(panel_name):
            if msg_id := panel_info.get('message_id'):
                try:
                    old_message = await channel.fetch_message(msg_id)
                    await old_message.delete()
                except (discord.NotFound, discord.Forbidden): pass
        if embed_data := await get_embed_from_db(panel_key):
            dungeon_levels = {f"{tier}_rec_level": f"Lv.{data.get('recommended_level', '?')}" for tier, data in self.dungeon_data.items()}
            embed = format_embed_from_db(embed_data, **dungeon_levels)
            view = await DungeonPanelView.create(self)
            new_message = await channel.send(embed=embed, view=view)
            await save_panel_id(panel_name, new_message.id, channel.id)

class DungeonPanelView(ui.View):
    def __init__(self, cog_instance: 'Dungeon'):
        super().__init__(timeout=None)
        self.cog = cog_instance

    @classmethod
    async def create(cls, cog_instance: 'Dungeon'):
        view = cls(cog_instance)
        await view._add_buttons()
        return view

    async def _add_buttons(self):
        while not self.cog.dungeon_data: await asyncio.sleep(0.1)
        for tier, data in self.cog.dungeon_data.items():
            button = ui.Button(label=data['name'], style=discord.ButtonStyle.secondary, custom_id=f"enter_dungeon_{tier}")
            button.callback = self.dispatch_callback
            self.add_item(button)

    async def dispatch_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        tier = interaction.data['custom_id'].split('_')[-1]
        await self.cog.handle_enter_dungeon(interaction, tier)

async def setup(bot: commands.Bot):
    await bot.add_cog(Dungeon(bot))
