# cogs/games/fishing.py

import discord
from discord.ext import commands
from discord import ui
import random
import asyncio
import logging
import time
from typing import Optional, Set, Dict, List

from utils.database import (
    update_wallet, get_inventory, update_inventory, add_to_aquarium,
    get_user_gear, set_user_gear, save_panel_id, get_panel_id, get_id,
    get_embed_from_db, supabase, get_item_database, get_fishing_loot, 
    get_config, save_config_to_db,
    is_whale_available, set_whale_caught,
    BARE_HANDS, DEFAULT_ROD,
    get_user_abilities,
    log_activity
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

INTERMEDIATE_ROD_NAME = "철 낚싯대"

class FishingGameView(ui.View):
    def __init__(self, bot: commands.Bot, user: discord.Member, used_rod: str, used_bait: str, remaining_baits: Dict[str, int], cog_instance: 'Fishing', location_type: str, bite_range: List[float]):
        super().__init__(timeout=35)
        self.bot = bot; self.player = user; self.message: Optional[discord.WebhookMessage] = None
        self.game_state = "waiting"; self.game_task: Optional[asyncio.Task] = None
        self.used_rod = used_rod; self.used_bait = used_bait; self.remaining_baits = remaining_baits
        self.fishing_cog = cog_instance; self.location_type = location_type; self.bite_range = bite_range
        item_db = get_item_database(); self.rod_data = item_db.get(self.used_rod, {})
        game_config = get_config("GAME_CONFIG", {}); self.bite_reaction_time = game_config.get("FISHING_BITE_REACTION_TIME", 3.0)
        self.big_catch_threshold = game_config.get("FISHING_BIG_CATCH_THRESHOLD", 70.0)

    async def start_game(self, interaction: discord.Interaction, embed: discord.Embed):
        self.message = await interaction.followup.send(embed=embed, view=self, ephemeral=True)
        self.game_task = asyncio.create_task(self.game_flow())

    async def game_flow(self):
        try:
            await asyncio.sleep(random.uniform(*self.bite_range))
            if self.is_finished(): return
            self.game_state = "biting"
            if self.children and isinstance(catch_button := self.children[0], ui.Button):
                catch_button.style = discord.ButtonStyle.success; catch_button.label = "낚아채기!"
            embed = discord.Embed(title="❗ 입질이 왔다!", description="지금이야! 버튼을 눌러 낚아채세요!", color=discord.Color.red())
            if self.message: await self.message.edit(embed=embed, view=self)
            await asyncio.sleep(self.bite_reaction_time)
            if not self.is_finished() and self.game_state == "biting":
                embed = discord.Embed(title="💧 놓쳤다...", description=f"{self.player.mention}님, 아쉽지만 물고기가 도망갔습니다.", color=discord.Color.greyple())
                await self._send_result(embed)
                self.stop()
        except asyncio.CancelledError: pass
        except Exception as e:
            logger.error(f"{self.player.display_name}의 낚시 게임 중 오류 발생: {e}", exc_info=True)
            if not self.is_finished():
                await self._send_result(discord.Embed(title="❌ 오류 발생", description="낚시 중 예기치 않은 오류가 발생했습니다.", color=discord.Color.red()))
                self.stop()

    async def _handle_catch_logic(self) -> tuple[discord.Embed, bool, bool, bool]:
        all_loot = get_fishing_loot()
        location_map = {"river": "강", "sea": "바다"}; current_location_name = location_map.get(self.location_type, "강")
        base_loot = [item for item in all_loot if item.get('location_type') == current_location_name or item.get('location_type') is None]
        rod_data = self.rod_data; rod_tier = rod_data.get('tier', 0); rod_bonus = rod_data.get('loot_bonus', 0.0)
        loot_pool = []; is_whale_catchable = is_whale_available()
        for item in base_loot:
            if item.get('name') == '고래':
                if rod_tier >= 5 and is_whale_catchable: loot_pool.append(item)
            else: loot_pool.append(item)
        if not loot_pool: return (discord.Embed(title="오류", description="이 장소에서는 아무것도 낚이지 않는 것 같습니다.", color=discord.Color.red()), False, False, False)
        
        xp_to_add = get_config("GAME_CONFIG", {}).get("XP_FROM_FISHING", 20)
        await log_activity(self.player.id, 'fishing_catch', xp_earned=xp_to_add)
        res = await supabase.rpc('add_xp', {'p_user_id': self.player.id, 'p_xp_to_add': xp_to_add, 'p_source': 'fishing'}).execute()
        if res.data: await self.fishing_cog.handle_level_up_event(self.player, res.data)

        user_abilities = await get_user_abilities(self.player.id); rare_up_bonus = 0.2 if 'fish_rare_up_2' in user_abilities else 0.0
        size_multiplier = 1.2 if 'fish_size_up_2' in user_abilities else 1.0; weights = []
        for item in loot_pool:
            weight = item['weight']; base_value = item.get('base_value'); 
            if base_value is None: base_value = 0
            if base_value > 100: weight *= (1.0 + rod_bonus + rare_up_bonus)
            else: weight *= (1.0 + rod_bonus)
            weights.append(weight)
        catch_proto = random.choices(loot_pool, weights=weights, k=1)[0]
        
        is_whale_catch = catch_proto.get('name') == '고래'; is_big_catch, log_publicly = False, False
        
        embed = discord.Embed()
        if catch_proto.get("min_size") is not None:
            log_publicly = True
            min_s, max_s = catch_proto["min_size"] * size_multiplier, catch_proto["max_size"] * size_multiplier
            size = round(random.uniform(min_s, max_s), 1)
            if is_whale_catch: await set_whale_caught()

            emoji_to_save = catch_proto.get('emoji', '🐠')
            if isinstance(emoji_to_save, str):
                emoji_to_save = emoji_to_save.strip()
            await add_to_aquarium(self.player.id, {"name": catch_proto['name'], "size": size, "emoji": emoji_to_save})

            is_big_catch = size >= self.big_catch_threshold
            title = "🏆 월척이다! 🏆" if is_big_catch else "🎉 낚시 성공! 🎉"
            if is_whale_catch: title = "🐋 전설의 시작, 고래를 낚다! 🐋"
            embed.title, embed.description, embed.color = title, f"{self.player.mention}님이 낚시에 성공했습니다!", discord.Color.blue()
            embed.add_field(name="어종", value=f"{catch_proto.get('emoji', '🐠')} **{catch_proto['name']}**", inline=True)
            embed.add_field(name="크기", value=f"`{size}`cm", inline=True)
        else:
            value = catch_proto.get('value') or 0
            if value != 0: await update_wallet(self.player, value)
            embed.title, embed.description, embed.color = catch_proto['title'], catch_proto['description'].format(user_mention=self.player.mention, value=abs(value)), int(catch_proto['color'], 16) if isinstance(catch_proto['color'], str) else catch_proto['color']
        
        if image_url := catch_proto.get('image_url'): embed.set_thumbnail(url=image_url)
        return embed, log_publicly, is_big_catch, is_whale_catch

    @ui.button(label="대기 중...", style=discord.ButtonStyle.secondary, custom_id="catch_fish_button")
    async def catch_button(self, interaction: discord.Interaction, button: ui.Button):
        if self.game_task: self.game_task.cancel()
        result_embed, log_publicly, is_big_catch, is_whale = None, False, False, False
        if self.game_state == "waiting":
            await interaction.response.defer()
            result_embed = discord.Embed(title="❌ 너무 빨라!", description=f"{interaction.user.mention}님, 너무 서두른 나머지 물고기를 놓쳤습니다...", color=discord.Color.dark_grey())
        elif self.game_state == "biting":
            await interaction.response.defer(); self.game_state = "finished"
            result_embed, log_publicly, is_big_catch, is_whale = await self._handle_catch_logic()
        if result_embed:
            if self.player.display_avatar and not result_embed.thumbnail: result_embed.set_thumbnail(url=self.player.display_avatar.url)
            await self._send_result(result_embed, log_publicly, is_big_catch, is_whale)
        self.stop()

    async def _send_result(self, embed: discord.Embed, log_publicly: bool = False, is_big_catch: bool = False, is_whale: bool = False):
        remaining_baits_config = get_config("FISHING_REMAINING_BAITS_DISPLAY", ['일반 낚시 미끼', '고급 낚시 미끼'])
        footer_private = f"남은 미끼: {' / '.join([f'{b}({self.remaining_baits.get(b, 0)}개)' for b in remaining_baits_config])}"
        footer_public = f"사용한 장비: {self.used_rod} / {self.used_bait}"
        if log_publicly:
            if is_whale: await self.fishing_cog.log_whale_catch(self.player, embed)
            elif (log_ch_id := self.fishing_cog.fishing_log_channel_id) and (log_ch := self.bot.get_channel(log_ch_id)):
                public_embed = embed.copy(); public_embed.set_footer(text=footer_public)
                content = self.player.mention if is_big_catch else None
                try: await log_ch.send(content=content, embed=public_embed, allowed_mentions=discord.AllowedMentions(users=is_big_catch))
                except Exception as e: logger.error(f"공개 낚시 로그 전송에 실패했습니다: {e}", exc_info=True)
        embed.set_footer(text=f"{footer_public}\n{footer_private}")
        if self.message:
            try:
                await self.message.edit(embed=embed, view=None)
                self.fishing_cog.last_result_messages[self.player.id] = self.message
            except (discord.NotFound, AttributeError, discord.HTTPException): pass

    async def on_timeout(self):
        if self.game_state != "finished":
            embed = discord.Embed(title="⏱️ 시간 초과", description=f"{self.player.mention}님은 시간 내에 반응하지 못했습니다.", color=discord.Color.darker_grey())
            await self._send_result(embed)
        self.stop()

    def stop(self):
        if self.game_task and not self.game_task.done(): self.game_task.cancel()
        self.fishing_cog.active_fishing_sessions_by_user.discard(self.player.id)
        super().stop()

class FishingPanelView(ui.View):
    def __init__(self, bot: commands.Bot, cog_instance: 'Fishing', panel_key: str):
        super().__init__(timeout=None)
        self.bot = bot
        self.fishing_cog = cog_instance
        self.panel_key = panel_key
        
        if panel_key == "panel_fishing_river":
            river_button = ui.Button(label="강에서 낚시하기", style=discord.ButtonStyle.primary, emoji="🏞️", custom_id="start_fishing_river")
            river_button.callback = self.dispatch_callback
            self.add_item(river_button)
        elif panel_key == "panel_fishing_sea":
            sea_button = ui.Button(label="바다에서 낚시하기", style=discord.ButtonStyle.primary, emoji="🌊", custom_id="start_fishing_sea")
            sea_button.callback = self.dispatch_callback
            self.add_item(sea_button)
    
    async def dispatch_callback(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        await self.handle_start_fishing(interaction)

    async def handle_start_fishing(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        
        if user_id in self.fishing_cog.active_fishing_sessions_by_user:
            await interaction.followup.send("이미 낚시를 진행 중입니다.", ephemeral=True)
            return

        if last_message := self.fishing_cog.last_result_messages.pop(user_id, None):
            try: await last_message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException): pass
        
        try:
            location_type = interaction.data['custom_id'].split('_')[-1]
            user = interaction.user
            
            gear, inventory, user_abilities = await asyncio.gather(
                get_user_gear(user), 
                get_inventory(user),
                get_user_abilities(user.id)
            )
            
            rod, item_db = gear.get('rod', BARE_HANDS), get_item_database()
            if rod == BARE_HANDS:
                if any('낚싯대' in item_name for item_name in inventory if item_db.get(item_name, {}).get('category') == '장비'):
                    await interaction.followup.send("❌ 프로필 화면에서 낚싯대를 먼저 장착해주세요.", ephemeral=True)
                else:
                    await interaction.followup.send(f"❌ 낚시를 하려면 먼저 상점에서 '{DEFAULT_ROD}'을(를) 구매해야 합니다.", ephemeral=True)
                return
            
            game_config = get_config("GAME_CONFIG", {})
            if location_type == 'sea':
                rod_data = item_db.get(rod, {})
                req_tier = game_config.get("FISHING_SEA_REQ_TIER", 3)
                if rod_data.get('tier', 0) < req_tier:
                    await interaction.followup.send(f"❌ 바다 낚시를 하려면 '{INTERMEDIATE_ROD_NAME}'(등급 {req_tier}) 이상의 낚싯대를 **장착**해야 합니다.", ephemeral=True)
                    return

            self.fishing_cog.active_fishing_sessions_by_user.add(user.id)
            bait = gear.get('bait', '미끼 없음')
            
            bait_saved = False
            if bait != "미끼 없음" and 'fish_bait_saver_1' in user_abilities:
                if random.random() < 0.2:
                    bait_saved = True

            if bait != "미끼 없음" and not bait_saved:
                if inventory.get(bait, 0) > 0:
                    await update_inventory(user.id, bait, -1)
                    inventory[bait] = max(0, inventory.get(bait, 0) - 1)
                else:
                    bait = "미끼 없음"
                    await set_user_gear(user.id, bait="미끼 없음")

            location_name = "강" if location_type == "river" else "바다"
            
            rod_data = item_db.get(rod, {})
            loot_bonus = rod_data.get('loot_bonus', 0.0)
            
            default_times = { "미끼 없음": [10.0, 15.0], "일반 낚시 미끼": [7.0, 12.0], "고급 낚시 미끼": [5.0, 10.0] }
            bite_times_config = game_config.get("FISHING_BITE_TIMES_BY_BAIT", default_times)

            bite_range = bite_times_config.get(bait, bite_times_config.get("미끼 없음", [10.0, 15.0]))
            
            if 'fish_bite_time_down_1' in user_abilities:
                bite_range = [max(0.5, t - 2.0) for t in bite_range]

            desc_lines = [
                f"### {location_name}에 낚싯대를 던졌습니다.",
                f"**🎣 사용 중인 낚싯대:** `{rod}` (보너스 +{loot_bonus:.0%})",
                f"**🐛 사용 중인 미끼:** `{bait}` (입질 시간: `{bite_range[0]:.1f}`～`{bite_range[1]:.1f}`초)"
            ]

            if bait_saved:
                desc_lines.append("\n✨ **미끼 절약술** 효과로 미끼를 소모하지 않았습니다!")

            active_effects = []
            if 'fish_bite_time_down_1' in user_abilities:
                active_effects.append("> ⏱️ **날렵한 챔질**: 물고기가 더 빨리 입질합니다.")
            if 'fish_rare_up_2' in user_abilities:
                active_effects.append("> ⭐ **희귀 어종 전문가**: 희귀한 물고기를 낚을 확률이 증가합니다.")
            if 'fish_size_up_2' in user_abilities:
                active_effects.append("> 📏 **월척 전문가**: 더 큰 물고기가 낚입니다.")
            if 'fish_bait_saver_1' in user_abilities and not bait_saved:
                active_effects.append("> ✨ **미끼 절약술**: 확률적으로 미끼를 소모하지 않습니다.")
            
            if active_effects:
                desc_lines.append("\n**--- 발동 중인 효과 ---**")
                desc_lines.extend(active_effects)

            desc = "\n".join(desc_lines)
            embed = discord.Embed(title=f"🎣 {location_name}에서 낚시 시작!", description=desc, color=discord.Color.light_grey())
            
            if image_url := get_config("FISHING_WAITING_IMAGE_URL"):
                embed.set_thumbnail(url=str(image_url).strip('"'))
            
            view = FishingGameView(self.bot, interaction.user, rod, bait, inventory, self.fishing_cog, location_type, bite_range)
            await view.start_game(interaction, embed)
        except Exception as e:
            self.fishing_cog.active_fishing_sessions_by_user.discard(user_id)
            logger.error(f"낚시 게임 시작 중 예기치 못한 오류가 발생했습니다: {e}", exc_info=True)
            await interaction.followup.send(f"❌ 낚시를 시작하는 중 오류가 발생했습니다.", ephemeral=True)

class Fishing(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_fishing_sessions_by_user: Set[int] = set()
        self.fishing_log_channel_id: Optional[int] = None
        self.last_result_messages: Dict[int, discord.Message] = {}
        
        logger.info("Fishing Cog가 성공적으로 초기화되었습니다.")
    
    async def cog_load(self): await self.load_configs()
    async def load_configs(self): self.fishing_log_channel_id = get_id("fishing_log_channel_id")

    async def register_persistent_views(self):
        self.bot.add_view(FishingPanelView(self.bot, self, "panel_fishing_river"))
        self.bot.add_view(FishingPanelView(self.bot, self, "panel_fishing_sea"))
        
    async def handle_level_up_event(self, user: discord.Member, result_data: List[Dict]):
        if not (result_data and result_data[0].get('leveled_up')):
            return
            
        new_level = result_data[0].get('new_level')
        
        if level_cog := self.bot.get_cog("LevelSystem"):
            await level_cog.handle_level_up_event(user, result_data)
        else:
            logger.error("LevelSystem Cog를 찾을 수 없어 레벨업 이벤트를 처리할 수 없습니다.")


    async def log_whale_catch(self, user: discord.Member, result_embed: discord.Embed):
        announcement_msg_id = get_config("whale_announcement_message_id")
        sea_fishing_channel_id = get_id("sea_fishing_panel_channel_id")

        if announcement_msg_id and sea_fishing_channel_id:
            if channel := self.bot.get_channel(sea_fishing_channel_id):
                try:
                    msg_to_delete = await channel.fetch_message(int(announcement_msg_id))
                    await msg_to_delete.delete()
                    logger.info(f"고래가 잡혀서 공지 메시지(ID: {announcement_msg_id})를 삭제했습니다.")
                    await save_config_to_db("whale_announcement_message_id", None)
                except (discord.NotFound, discord.Forbidden): pass
                except Exception as e: logger.error(f"고래 공지 메시지 삭제 중 오류가 발생했습니다: {e}", exc_info=True)

        if not self.fishing_log_channel_id or not (log_channel := self.bot.get_channel(self.fishing_log_channel_id)): return
        
        fish_field = next((f for f in result_embed.fields if f.name == "어종"), None)
        size_field = next((f for f in result_embed.fields if f.name == "크기"), None)
        if not all([fish_field, size_field]): return

        fish_name_raw = fish_field.value.split('**')[1] if '**' in fish_field.value else fish_field.value
        fish_data = next((loot for loot in get_fishing_loot() if loot['name'] == fish_name_raw), None)
        if not fish_data: return

        size_cm = float(size_field.value.strip('`cm`'))
        base_value = fish_data.get("base_value") or 0
        size_multiplier = fish_data.get("size_multiplier") or 0
        value = int(base_value + (size_cm * size_multiplier))
        
        embed_data = await get_embed_from_db("log_whale_catch") or {}

        embed = format_embed_from_db(
            embed_data, 
            user_mention=user.mention,
            emoji=fish_data.get('emoji','🐋'), 
            name=fish_name_raw, 
            size=size_cm, 
            value=f"{value:,}", 
            currency_icon=get_config('GAME_CONFIG', {}).get('CURRENCY_ICON', '🪙')
        )
        if user.display_avatar: embed.set_thumbnail(url=user.display_avatar.url)
        if image_url := fish_data.get('image_url'): embed.set_image(url=image_url)
        
        try:
            await log_channel.send(content="@here", embed=embed, allowed_mentions=discord.AllowedMentions(everyone=True))
        except Exception as e:
            logger.error(f"고래 출현 공지 전송에 실패했습니다: {e}", exc_info=True)

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str):
        if panel_key not in ["panel_fishing_river", "panel_fishing_sea"]: return
        if (panel_info := get_panel_id(panel_key)):
            if (old_ch_id := panel_info.get("channel_id")) and (old_ch := self.bot.get_channel(old_ch_id)):
                try:
                    async for message in old_ch.history(limit=10):
                        if message.id == panel_info.get('message_id'):
                            await message.delete()
                            break
                except (discord.NotFound, discord.Forbidden): pass
        
        embed_data = await get_embed_from_db(panel_key)
        if not embed_data: return
        embed = discord.Embed.from_dict(embed_data)
        view = FishingPanelView(self.bot, self, panel_key)
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_key, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} 패널을 성공적으로 생성했습니다. (채널: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(Fishing(bot))
```

#### **4. `cogs/games/mining.py` (전체 코드)**

```python
# cogs/games/mining.py

import discord
from discord.ext import commands, tasks
from discord import ui
import logging
import asyncio
import time
import random
import json
from typing import Optional, Dict, List, Any
from datetime import datetime, timezone, timedelta

from utils.database import (
    get_inventory, update_inventory, get_user_gear, BARE_HANDS,
    save_panel_id, get_panel_id, get_id, get_embed_from_db,
    log_activity, get_user_abilities, supabase, get_item_database
)
from utils.helpers import format_embed_from_db, format_timedelta_minutes_seconds, coerce_item_emoji

logger = logging.getLogger(__name__)

MINING_PASS_NAME = "광산 입장권"
DEFAULT_MINE_DURATION_SECONDS = 600
MINING_COOLDOWN_SECONDS = 10

PICKAXE_LUCK_BONUS = {
    "나무 곡괭이": 1.0, "구리 곡괭이": 1.1, "철 곡괭이": 1.25,
    "금 곡괭이": 1.5, "다이아 곡괭이": 2.0,
}

ORE_DATA = {
    "꽝":       {"weight": 40, "image_url": "https://saewayvzcyzueviasftu.supabase.co/storage/v1/object/public/game_assets/stone.jpg"},
    "구리 광석": {"weight": 30, "image_url": "https://saewayvzcyzueviasftu.supabase.co/storage/v1/object/public/game_assets/cooper.jpg"},
    "철 광석":   {"weight": 20, "image_url": "https://saewayvzcyzueviasftu.supabase.co/storage/v1/object/public/game_assets/Iron.jpg"},
    "금 광석":    {"weight": 8,  "image_url": "https://saewayvzcyzueviasftu.supabase.co/storage/v1/object/public/game_assets/gold.jpg"},
    "다이아몬드": {"weight": 2,  "image_url": "https://saewayvzcyzueviasftu.supabase.co/storage/v1/object/public/game_assets/diamond.jpg"}
}

ORE_XP_MAP = { "구리 광석": 10, "철 광석": 15, "금 광석": 30, "다이아몬드": 75 }

class MiningGameView(ui.View):
    def __init__(self, cog_instance: 'Mining', user: discord.Member, pickaxe: str, duration: int, end_time: datetime, duration_doubled: bool):
        super().__init__(timeout=duration + 30)
        self.cog = cog_instance
        self.user = user
        self.pickaxe = pickaxe
        self.end_time = end_time
        self.duration_doubled = duration_doubled
        self.mined_ores: Dict[str, int] = {}
        self.luck_bonus = PICKAXE_LUCK_BONUS.get(pickaxe, 1.0)
        self.time_reduction = 0
        self.can_double_yield = False
        self.state = "idle"
        self.discovered_ore: Optional[str] = None
        self.last_result_text: Optional[str] = None
        self.message: Optional[discord.Message] = None
        self.on_cooldown = False
        self.ui_lock = asyncio.Lock()
        self.ui_update_task = self.cog.bot.loop.create_task(self.ui_updater())
        self.initial_load_task = self.cog.bot.loop.create_task(self.load_initial_data())

        action_button = ui.Button(label="광석 찾기", style=discord.ButtonStyle.secondary, emoji="🔍", custom_id="mine_action_button")
        action_button.callback = self.dispatch_callback
        self.add_item(action_button)

    async def load_initial_data(self):
        user_abilities = await get_user_abilities(self.user.id)
        self.cog.active_abilities_cache[self.user.id] = user_abilities
        if 'mine_time_down_1' in user_abilities: self.time_reduction = 3
        if 'mine_double_yield_2' in user_abilities: self.can_double_yield = True
        if 'mine_rare_up_2' in user_abilities: self.luck_bonus += 0.5

    def stop(self):
        if hasattr(self, 'ui_update_task') and not self.ui_update_task.done(): self.ui_update_task.cancel()
        if hasattr(self, 'initial_load_task') and not self.initial_load_task.done(): self.initial_load_task.cancel()
        super().stop()

    async def ui_updater(self):
        while not self.is_finished():
            async with self.ui_lock:
                try:
                    if self.message and self.state == "idle":
                        embed = self.build_embed()
                        await self.message.edit(embed=embed)
                except (discord.NotFound, discord.Forbidden): self.stop(); break
                except Exception as e: logger.error(f"Mining UI 업데이트 중 오류: {e}", exc_info=True)
            await asyncio.sleep(10)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("본인만 채굴할 수 있습니다.", ephemeral=True, delete_after=5); return False
        if self.user.id not in self.cog.active_sessions:
            if self.message: await self.message.edit(content="만료된 광산입니다.", view=None, embed=None)
            self.stop(); return False
        return True
        
    async def on_error(self, interaction: discord.Interaction, error: Exception, item: ui.Item[Any]) -> None:
        logger.error(f"MiningGameView에서 오류 발생 (Item: {item.custom_id}): {error}", exc_info=True)

    async def dispatch_callback(self, interaction: discord.Interaction):
        if not interaction.response.is_done(): await interaction.response.defer()
        
        if self.on_cooldown:
            return await interaction.followup.send("⏳ 아직 주변을 살피고 있습니다.", ephemeral=True, delete_after=5)
        
        await self.handle_action_button(interaction, self.children[0])

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title=f"{self.user.display_name}님의 광산 채굴", color=0x607D8B)
        item_db = get_item_database()
        if self.state == "idle":
            description_parts = ["## 앞으로 나아가 광물을 찾아보자"]
            if self.last_result_text: description_parts.append(f"## 채굴 결과\n{self.last_result_text}")
            remaining_time = self.end_time - datetime.now(timezone.utc)
            timer_str = f"광산 닫힘까지: **{discord.utils.format_dt(self.end_time, 'R')}**" if remaining_time.total_seconds() > 0 else f"광산 닫힘까지: **종료됨**"
            description_parts.append(timer_str)
            active_abilities = []
            if self.duration_doubled: active_abilities.append("> ✨ 집중 탐사 (시간 2배)")
            if self.time_reduction > 0: active_abilities.append("> ⚡ 신속한 채굴 (쿨타임 감소)")
            if self.can_double_yield: active_abilities.append("> 💰 풍부한 광맥 (수량 2배 확률)")
            if 'mine_rare_up_2' in self.cog.active_abilities_cache.get(self.user.id, []): active_abilities.append("> 💎 노다지 발견 (희귀 광물 확률 증가)")
            if active_abilities: description_parts.append(f"**--- 활성화된 능력 ---**\n" + "\n".join(active_abilities))
            description_parts.append(f"**사용 중인 장비:** {self.pickaxe}")
            embed.description = "\n\n".join(description_parts)
        elif self.state == "discovered":
            ore_info = item_db.get(self.discovered_ore, {})
            ore_emoji = str(coerce_item_emoji(ore_info.get('emoji', '💎')))
            desc_text = f"### {ore_emoji} {self.discovered_ore}을(를) 발견했다!" if self.discovered_ore != "꽝" else "### 아무것도 발견하지 못했다..."
            embed.description = desc_text
            embed.set_image(url=ORE_DATA[self.discovered_ore]['image_url'])
        elif self.state == "mining":
            ore_info = item_db.get(self.discovered_ore, {})
            ore_emoji = str(coerce_item_emoji(ore_info.get('emoji', '💎')))
            embed.description = f"**{self.pickaxe}**(으)로 열심히 **{ore_emoji} {self.discovered_ore}**을(를) 캐는 중입니다..."
            embed.set_image(url=ORE_DATA[self.discovered_ore]['image_url'])
        return embed

    async def handle_action_button(self, interaction: discord.Interaction, button: ui.Button):
        async with self.ui_lock:
            if self.state == "idle":
                self.last_result_text = None
                button.disabled = True; button.label = "탐색 중..."
                await interaction.edit_original_response(embed=self.build_embed(), view=self)
                try:
                    await asyncio.sleep(1)
                    ores, weights = zip(*[(k, v['weight']) for k, v in ORE_DATA.items()])
                    new_weights = [w * self.luck_bonus if o != "꽝" else w for o, w in zip(ores, weights)]
                    self.discovered_ore = random.choices(ores, weights=new_weights, k=1)[0]
                    if self.discovered_ore == "꽝": self.state = "discovered"; button.label = "다시 찾아보기"; button.emoji = "🔍"
                    else: self.state = "discovered"; button.label = "채굴하기"; button.style = discord.ButtonStyle.primary; button.emoji = "⛏️"
                finally:
                    button.disabled = False
                    await interaction.edit_original_response(embed=self.build_embed(), view=self)
            elif self.state == "discovered":
                if self.discovered_ore == "꽝":
                    self.on_cooldown = True; button.disabled = True
                    await interaction.edit_original_response(view=self)
                    await asyncio.sleep(MINING_COOLDOWN_SECONDS - self.time_reduction)
                    self.on_cooldown = False
                    if self.is_finished(): return
                    self.state = "idle"; self.last_result_text = "### 아무것도 발견하지 못했다..."
                    button.label = "광석 찾기"; button.emoji = "🔍"; button.disabled = False
                    await interaction.edit_original_response(embed=self.build_embed(), view=self)
                else:
                    self.state = "mining"; button.disabled = True
                    mining_duration = max(3, MINING_COOLDOWN_SECONDS - self.time_reduction)
                    button.label = f"채굴 중... ({mining_duration}초)"
                    await interaction.edit_original_response(embed=self.build_embed(), view=self)
                    await asyncio.sleep(mining_duration)
                    if self.is_finished(): return
                    quantity = 2 if self.can_double_yield and random.random() < 0.20 else 1
                    xp_earned = ORE_XP_MAP.get(self.discovered_ore, 0) * quantity
                    self.mined_ores[self.discovered_ore] = self.mined_ores.get(self.discovered_ore, 0) + quantity
                    try:
                        session_res = await supabase.table('mining_sessions').select('mined_ores_json').eq('user_id', str(self.user.id)).maybe_single().execute()
                        if session_res and session_res.data:
                            current_ores_raw = session_res.data.get('mined_ores_json'); current_ores = {}
                            if isinstance(current_ores_raw, str):
                                try: current_ores = json.loads(current_ores_raw)
                                except json.JSONDecodeError: pass
                            elif isinstance(current_ores_raw, dict): current_ores = current_ores_raw
                            current_ores[self.discovered_ore] = current_ores.get(self.discovered_ore, 0) + quantity
                            await supabase.table('mining_sessions').update({'mined_ores_json': current_ores}).eq('user_id', str(self.user.id)).execute()
                    except Exception as db_error: logger.error(f"광산 채굴량 DB 업데이트 중 오류 발생: {db_error}", exc_info=True)
                    await update_inventory(self.user.id, self.discovered_ore, quantity)
                    await log_activity(self.user.id, 'mining', amount=quantity, xp_earned=xp_earned)
                    ore_info = get_item_database().get(self.discovered_ore, {}); ore_emoji = str(coerce_item_emoji(ore_info.get('emoji', '💎')))
                    self.last_result_text = f"✅ {ore_emoji} **{self.discovered_ore}** {quantity}개를 획득했습니다! (`+{xp_earned} XP`)"
                    if quantity > 1: self.last_result_text += f"\n\n✨ **풍부한 광맥** 능력으로 광석을 2개 획득했습니다!"
                    if xp_earned > 0:
                        res = await supabase.rpc('add_xp', {'p_user_id': self.user.id, 'p_xp_to_add': xp_earned, 'p_source': 'mining'}).execute()
                        if res.data and (level_cog := self.cog.bot.get_cog("LevelSystem")):
                            await level_cog.handle_level_up_event(self.user, res.data)
                    self.state = "idle"
                    button.label = "광석 찾기"; button.style = discord.ButtonStyle.secondary; button.emoji = "🔍"; button.disabled = False
                    try: await interaction.edit_original_response(embed=self.build_embed(), view=self)
                    except discord.NotFound: self.stop()

class MiningPanelView(ui.View):
    def __init__(self, cog_instance: 'Mining'):
        super().__init__(timeout=None)
        self.cog = cog_instance
        
        enter_button = ui.Button(label="입장하기", style=discord.ButtonStyle.secondary, emoji="⛏️", custom_id="enter_mine")
        enter_button.callback = self.dispatch_callback
        self.add_item(enter_button)

    async def dispatch_callback(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer()
        
        await self.cog.handle_enter_mine(interaction)

class Mining(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_sessions: Dict[int, Dict] = {}
        self.active_abilities_cache: Dict[int, List[str]] = {}
        self.check_expired_mines_from_db.start()
        
    def cog_unload(self):
        self.check_expired_mines_from_db.cancel()

    @tasks.loop(minutes=1)
    async def check_expired_mines_from_db(self):
        now = datetime.now(timezone.utc)
        res = await supabase.table('mining_sessions').select('*').lte('end_time', now.isoformat()).execute()
        if not (res and res.data): return
        
        for session in res.data:
            user_id = int(session['user_id'])
            if user_id not in self.active_sessions:
                logger.warning(f"DB에서 방치된 광산 세션(유저: {user_id})을 발견하여 안전장치로 종료합니다.")
                await self.close_mine_session(user_id)

    @check_expired_mines_from_db.before_loop
    async def before_check_expired_mines(self):
        await self.bot.wait_until_ready()

    async def handle_enter_mine(self, interaction: discord.Interaction):
        user = interaction.user

        if user.id in self.active_sessions:
            thread_id = self.active_sessions[user.id].get("thread_id")
            if thread := self.bot.get_channel(thread_id):
                await interaction.followup.send(f"이미 광산에 입장해 있습니다. {thread.mention}", ephemeral=True)
            else:
                await self.close_mine_session(user.id)
                await interaction.followup.send("이전 광산 정보를 강제 초기화했습니다. 다시 시도해주세요.", ephemeral=True)
            return

        inventory, gear, user_abilities = await asyncio.gather(get_inventory(user), get_user_gear(user), get_user_abilities(user.id))
        
        if inventory.get(MINING_PASS_NAME, 0) < 1: return await interaction.followup.send(f"'{MINING_PASS_NAME}'이 부족합니다.", ephemeral=True)
        pickaxe = gear.get('pickaxe', BARE_HANDS)
        if pickaxe == BARE_HANDS: return await interaction.followup.send("❌ 곡괭이를 장착해야 합니다.", ephemeral=True)

        try: thread = await interaction.channel.create_thread(name=f"⛏️｜{user.display_name}의 광산", type=discord.ChannelType.private_thread)
        except Exception: return await interaction.followup.send("❌ 광산을 여는 데 실패했습니다.", ephemeral=True)
        
        await update_inventory(user.id, MINING_PASS_NAME, -1)
        await thread.add_user(user)

        duration = DEFAULT_MINE_DURATION_SECONDS
        duration_doubled = 'mine_duration_up_1' in user_abilities and random.random() < 0.15
        if duration_doubled: duration *= 2
        end_time = datetime.now(timezone.utc) + timedelta(seconds=duration)
        
        await supabase.table('mining_sessions').upsert({
            "user_id": str(user.id), "thread_id": str(thread.id), "end_time": end_time.isoformat(), "pickaxe_name": pickaxe, "mined_ores_json": "{}"
        }, on_conflict="user_id").execute()
        
        view = MiningGameView(self, user, pickaxe, duration, end_time, duration_doubled)
        
        session_task = self.bot.loop.create_task(self.mine_session_timer(user.id, thread, duration))
        self.active_sessions[user.id] = {"thread_id": thread.id, "view": view, "task": session_task}

        embed = view.build_embed()
        message = await thread.send(embed=embed, view=view)
        view.message = message
        
        await interaction.followup.send(f"광산에 입장했습니다! {thread.mention}", ephemeral=True)

    async def mine_session_timer(self, user_id: int, thread: discord.Thread, duration: int):
        try:
            if duration > 60:
                await asyncio.sleep(duration - 60)
                if user_id in self.active_sessions:
                    try: await thread.send("⚠️ 1분 후 광산이 닫힙니다...", delete_after=59)
                    except (discord.Forbidden, discord.HTTPException): pass
                else: return
                await asyncio.sleep(60)
            else:
                await asyncio.sleep(duration)
            
            if user_id in self.active_sessions:
                 await self.close_mine_session(user_id)
        except asyncio.CancelledError: pass
            
    async def close_mine_session(self, user_id: int):
        res = await supabase.table('mining_sessions').select('*').eq('user_id', str(user_id)).maybe_single().execute()
        session_data = res.data if res and res.data else None
        
        if in_memory_session := self.active_sessions.pop(user_id, None):
            if task := in_memory_session.get("task"): task.cancel()
            if view := in_memory_session.get("view"): view.stop()
        
        if not session_data:
            logger.warning(f"[{user_id}] 종료할 광산 세션이 DB에 없습니다 (이미 처리됨).")
            return

        thread_id = int(session_data['thread_id'])
        logger.info(f"[{user_id}] 광산 세션(스레드: {thread_id}) 종료 시작.")
        await supabase.table('mining_sessions').delete().eq('user_id', str(user_id)).execute()

        user = self.bot.get_user(user_id)
        if user:
            mined_ores_raw = session_data.get('mined_ores_json', "{}")
            mined_ores = {}
            if isinstance(mined_ores_raw, str):
                try: mined_ores = json.loads(mined_ores_raw)
                except json.JSONDecodeError: pass
            elif isinstance(mined_ores_raw, dict):
                mined_ores = mined_ores_raw

            item_db = get_item_database()
            mined_ores_lines = []
            for ore, qty in mined_ores.items():
                ore_info = item_db.get(ore, {})
                ore_emoji = str(coerce_item_emoji(ore_info.get('emoji', '💎')))
                mined_ores_lines.append(f"> {ore_emoji} {ore}: {qty}개")

            mined_ores_text = "\n".join(mined_ores_lines) or "> 채굴한 광물이 없습니다."
            
            embed_data = await get_embed_from_db("log_mining_result") or {"title": "⛏️ 광산 탐사 결과", "color": 0x607D8B}
            log_embed = format_embed_from_db(embed_data, user_mention=user.mention, pickaxe_name=session_data.get('pickaxe_name'), mined_ores=mined_ores_text)
            if user.display_avatar: log_embed.set_thumbnail(url=user.display_avatar.url)
            
            panel_channel_id = get_id("mining_panel_channel_id")
            if panel_channel_id and (panel_channel := self.bot.get_channel(panel_channel_id)):
                await self.regenerate_panel(panel_channel, last_log=log_embed)
        
        try:
            thread = self.bot.get_channel(thread_id) or await self.bot.fetch_channel(thread_id)
            await thread.add_user(self.bot.user)
            await thread.send("**광산이 닫혔습니다.**", delete_after=10)
            await asyncio.sleep(1)
            await thread.delete()
        except (discord.NotFound, discord.Forbidden): pass

    async def register_persistent_views(self):
        self.bot.add_view(MiningPanelView(self))

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_mining", last_log: Optional[discord.Embed] = None):
        if last_log:
            try: await channel.send(embed=last_log)
            except discord.HTTPException as e: logger.error(f"광산 로그 전송 실패: {e}")
        
        panel_name = panel_key.replace("panel_", "")
        if panel_info := get_panel_id(panel_name):
            try:
                if old_channel := self.bot.get_channel(panel_info['channel_id']):
                    msg = await old_channel.fetch_message(panel_info['message_id'])
                    await msg.delete()
            except (discord.NotFound, discord.Forbidden): pass
        embed_data = await get_embed_from_db(panel_key)
        if not embed_data: return logger.error(f"DB에서 '{panel_key}' 임베드를 찾을 수 없습니다.")
        
        embed = format_embed_from_db(embed_data)
        
        view = MiningPanelView(self)
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_name, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} 패널을 성공적으로 생성했습니다. (채널: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(Mining(bot))
