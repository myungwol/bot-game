
# cogs/games/pet.py
import discord
from discord.ext import commands, tasks
from discord import ui
import asyncio, time
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List
import logging

from utils.pet_repository import (
    get_user_inventory_eggs, decrement_inventory_item,
    get_active_incubation, create_pet_incubation, cancel_pet_incubation, list_due_incubations,
    create_pet_from_incubation, get_active_pet, update_pet_stats,
    get_pet_panel_message_info, save_pet_panel_message_info
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
KST = timezone(timedelta(hours=9))

# ------------- Safe base view (debounce + disable) -------------
class SafeView(ui.View):
    def __init__(self, cog: 'Pet'):
        super().__init__(timeout=None)
        self.cog = cog

    async def _disable_all(self, interaction: discord.Interaction):
        tmp = type(self)(self.cog, *getattr(self, "_extra_args", [])) if hasattr(self, "_extra_args") else type(self)(self.cog)
        for child in tmp.children:
            if isinstance(child, ui.Button):
                child.disabled = True
        try:
            if not interaction.response.is_done():
                await interaction.response.defer_update()
            await self.cog.safe_edit(interaction.message, view=tmp)
        except Exception:
            pass

    async def _enable_all(self, interaction: discord.Interaction):
        try:
            await self.cog.safe_edit(interaction.message, view=self)
        except Exception:
            pass

    def _lock_key(self, interaction: discord.Interaction):
        return (interaction.channel.id, interaction.user.id)

    async def _acquire(self, interaction: discord.Interaction):
        key = self._lock_key(interaction)
        now = time.monotonic()
        last = self.cog.last_action_ts.get(key, 0.0)
        if now - last < self.cog.cooldown_sec:
            return None
        self.cog.last_action_ts[key] = now
        lock = self.cog.actor_locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            return None
        return lock

# ------------- Hatch Panel -------------
class PetHatchView(SafeView):
    def __init__(self, cog: 'Pet', owner: discord.User):
        super().__init__(cog)
        self.owner = owner
        self._extra_args = [owner]
        self.add_item(ui.Button(label="알 등록/부화 시작", emoji="🥚", custom_id="pet_hatch_start"))
        self.add_item(ui.Button(label="펫 UI 열기", emoji="🧩", custom_id="pet_open_ui"))

    async def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title="🐣 펫 부화하기", colour=discord.Colour.blurple())
        eggs = get_user_inventory_eggs(self.owner.id)
        egg_lines = [f"- {e['item_name']} ×{e['quantity']}" for e in eggs] or ["(보유한 알이 없습니다)"]
        embed.add_field(name="보유한 알", value="\n".join(egg_lines), inline=False)

        inc = get_active_incubation(self.owner.id)
        pet = get_active_pet(self.owner.id)
        if pet:
            embed.add_field(name="현재 상태", value="이미 펫을 키우는 중입니다. 새로 부화할 수 없습니다.", inline=False)
        elif inc:
            hat = inc["hatch_at"]
            if isinstance(hat, str):
                hat = datetime.fromisoformat(hat.replace("Z","+00:00")).astimezone(KST)
            else:
                hat = hat.astimezone(KST)
            embed.add_field(name="현재 상태", value=f"알 부화 중… ⏳ (완료 예정: {hat:%Y-%m-%d %H:%M KST})", inline=False)
        else:
            embed.add_field(name="현재 상태", value="부화 대기 중입니다. 알을 선택해 시작하세요.", inline=False)
        embed.set_footer(text="부화는 2일(48시간) 소요됩니다. 부화 중에는 다른 알/펫 진행 불가.")
        return embed

    async def dispatch_callback(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer_update()

        cid = (interaction.data or {}).get('custom_id')
        if cid == "pet_open_ui":
            await self.open_pet_ui(interaction); return
        if cid == "pet_hatch_start":
            lock = await self._acquire(interaction)
            if lock is None: return
            async with lock:
                await self._disable_all(interaction)
                try:
                    await self.start_hatching(interaction)
                finally:
                    await self._enable_all(interaction)

    async def start_hatching(self, interaction: discord.Interaction):
        owner_id = self.owner.id
        if get_active_pet(owner_id):
            await interaction.followup.send("❌ 이미 펫을 키우는 중이에요.", ephemeral=True); return
        if get_active_incubation(owner_id):
            await interaction.followup.send("❌ 이미 알을 부화 중이에요.", ephemeral=True); return

        eggs = get_user_inventory_eggs(owner_id)
        if not eggs:
            await interaction.followup.send("❌ 보유한 알이 없습니다. 상점에서 알을 먼저 구매해 주세요.", ephemeral=True); return
        egg_item_name = eggs[0]["item_name"]  # 가장 앞의 알 사용(선택 UI는 추후 확장)

        if not decrement_inventory_item(owner_id, egg_item_name, 1):
            await interaction.followup.send("❌ 인벤토리 차감 실패. 잠시 후 다시 시도해주세요.", ephemeral=True); return

        hatch_at = datetime.now(tz=KST) + timedelta(days=2)
        create_pet_incubation(owner_id, egg_item_name, hatch_at.astimezone(timezone.utc))

        thread = await self.cog.ensure_pet_thread(self.owner)
        await self.cog.update_pet_ui(thread, self.owner)

        await interaction.followup.send(
            f"✅ `{egg_item_name}` 부화를 시작했어요! ⏳ {hatch_at:%Y-%m-%d %H:%M KST} 에 부화됩니다.\n"
            f"펫 UI 스레드에서 진행 상태를 확인할 수 있어요.", ephemeral=True
        )

    async def open_pet_ui(self, interaction: discord.Interaction):
        thread = await self.cog.ensure_pet_thread(self.owner)
        await self.cog.update_pet_ui(thread, self.owner)
        await interaction.followup.send(f"🧩 펫 UI를 열었어요: <#{thread.id}>", ephemeral=True)

# ------------- Pet UI View -------------
class PetUIView(SafeView):
    def __init__(self, cog: 'Pet', owner: discord.User):
        super().__init__(cog)
        self.owner = owner
        self._extra_args = [owner]
        self.add_item(ui.Button(label="새로고침", emoji="🔄", custom_id="pet_refresh"))
        self.add_item(ui.Button(label="먹이 주기", emoji="🍖", custom_id="pet_feed"))
        self.add_item(ui.Button(label="놀아주기", emoji="🎾", custom_id="pet_play"))

    async def build_embed(self) -> discord.Embed:
        inc = get_active_incubation(self.owner.id)
        pet = get_active_pet(self.owner.id)

        if inc and inc.get("status") == "incubating":
            hat = inc["hatch_at"]
            if isinstance(hat, str):
                hat = datetime.fromisoformat(hat.replace("Z","+00:00")).astimezone(KST)
            else:
                hat = hat.astimezone(KST)
            embed = discord.Embed(title="🐣 알 부화 중", colour=discord.Colour.gold())
            embed.add_field(name="부화 완료 예정", value=f"{hat:%Y-%m-%d %H:%M KST}", inline=False)
            embed.set_footer(text="완료 시간이 지나면 새로고침 시 자동으로 부화 처리됩니다.")
            return embed

        if pet:
            embed = discord.Embed(title="🧩 내 펫", colour=discord.Colour.green())
            if pet.get("image_url"):
                embed.set_thumbnail(url=pet["image_url"])
            lines = [
                f"체력(HP): {pet['hp']}",
                f"공격력: {pet['atk']}",
                f"방어력: {pet['def']}",
                f"스피드: {pet['spd']}",
                f"친밀도: {pet['affinity']}",
                f"배고픔: {pet['hunger']}",
                f"속성: {pet['attribute_key']} | 단계: {pet.get('stage_key','hatch')} | 레벨: {pet.get('level',1)}",
            ]
            embed.add_field(name=str(pet.get("species_key", "Unknown")), value="\n".join(lines), inline=False)
            embed.set_footer(text="먹이 주기/놀아주기로 친밀도·배고픔을 관리하세요.")
            return embed

        return discord.Embed(title="🐣 펫이 없어요", description="`!펫부화`로 알을 등록해 부화시켜 보세요!", colour=discord.Colour.red())

    async def dispatch_callback(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer_update()
        cid = (interaction.data or {}).get('custom_id')
        lock = await self._acquire(interaction)
        if lock is None: return
        async with lock:
            await self._disable_all(interaction)
            try:
                if cid == "pet_refresh":
                    await self.cog.try_hatch_now(self.owner)
                elif cid == "pet_feed":
                    await self.cog.feed_pet(self.owner, interaction)
                elif cid == "pet_play":
                    await self.cog.play_with_pet(self.owner, interaction)
                await self.cog.update_pet_ui(interaction.channel, self.owner)
            finally:
                await self._enable_all(interaction)

# ------------- Cog -------------
class Pet(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.actor_locks: Dict[tuple[int, int], asyncio.Lock] = {}
        self.last_action_ts: Dict[tuple[int, int], float] = {}
        self.cooldown_sec: float = 0.8

        self.hatch_checker.start()

    def cog_unload(self):
        self.hatch_checker.cancel()

    async def safe_edit(self, message: discord.Message, **kwargs):
        backoff = [0.4, 0.8, 1.6, 2.0]
        for i, sleep_s in enumerate([0.0] + backoff):
            if sleep_s:
                await asyncio.sleep(sleep_s)
            try:
                return await message.edit(**kwargs)
            except Exception as e:
                status = getattr(e, 'status', None)
                if status in (429, 500, 502, 503):
                    if i == len(backoff):
                        raise
                    continue
                raise

    # ---- Commands ----
    @commands.command(name="펫부화")
    async def open_hatch_panel(self, ctx: commands.Context):
        owner = ctx.author
        view = PetHatchView(self, owner)
        embed = await view.build_embed()
        await ctx.send(embed=embed, view=view)

    @commands.command(name="펫")
    async def open_pet_panel(self, ctx: commands.Context):
        owner = ctx.author
        thread = await self.ensure_pet_thread(owner)
        await self.update_pet_ui(thread, owner)
        await ctx.send(f"🧩 펫 UI: <#{thread.id}>")

    # ---- Threads & Panels ----
    async def ensure_pet_thread(self, owner: discord.User) -> discord.Thread:
        info = get_pet_panel_message_info(owner.id)
        if info and info.get("thread_id"):
            t = self.bot.get_channel(int(info["thread_id"]))
            if isinstance(t, discord.Thread):
                return t

        # pick a channel to host public thread
        host_channel = None
        for g in self.bot.guilds:
            member = g.get_member(owner.id)
            if not member: continue
            for ch in g.text_channels:
                perms = ch.permissions_for(g.me)
                if perms.create_public_threads and perms.send_messages:
                    host_channel = ch
                    break
            if host_channel: break
        if not host_channel:
            raise RuntimeError("펫 UI를 만들 텍스트 채널을 찾지 못했습니다.")

        thread = await host_channel.create_thread(name=f"🧩｜{owner.display_name}의 펫", type=discord.ChannelType.public_thread)
        return thread

    async def update_pet_ui(self, thread: discord.Thread, owner: discord.User):
        info = get_pet_panel_message_info(owner.id)
        message_to_edit = None
        if info and info.get("message_id"):
            try:
                message_to_edit = await thread.fetch_message(int(info["message_id"]))
            except discord.NotFound:
                message_to_edit = None

        view = PetUIView(self, owner)
        embed = await view.build_embed()
        if message_to_edit:
            await self.safe_edit(message_to_edit, embed=embed, view=view)
        else:
            msg = await thread.send(embed=embed, view=view)
            save_pet_panel_message_info(owner.id, thread.id, msg.id)

    # ---- Background hatch checker ----
    @tasks.loop(minutes=5)
    async def hatch_checker(self):
        try:
            due = list_due_incubations(limit=50)
            for inc in due:
                pet = create_pet_from_incubation(inc)
                # update panel after hatch
                owner_id = inc["owner_id"]
                info = get_pet_panel_message_info(owner_id)
                if info and info.get("thread_id"):
                    t = self.bot.get_channel(int(info["thread_id"]))
                    if isinstance(t, discord.Thread):
                        # owner user object
                        owner = t.guild.get_member(owner_id) or (await t.guild.fetch_member(owner_id))
                        await self.update_pet_ui(t, owner)
        except Exception as e:
            logger.error(f"hatch_checker error: {e}", exc_info=True)

    @hatch_checker.before_loop
    async def before_hatch_checker(self):
        await self.bot.wait_until_ready()

    # ---- Actions ----
    async def try_hatch_now(self, owner: discord.User):
        inc = get_active_incubation(owner.id)
        if not inc: return False
        # if hatch time passed
        hat = inc["hatch_at"]
        if isinstance(hat, str):
            hat = datetime.fromisoformat(hat.replace("Z","+00:00"))
        now_utc = datetime.now(timezone.utc)
        if hat <= now_utc:
            create_pet_from_incubation(inc)
            return True
        return False

    async def feed_pet(self, owner: discord.User, interaction: Optional[discord.Interaction] = None):
        pet = get_active_pet(owner.id)
        if not pet:
            if interaction: await interaction.followup.send("❌ 펫이 없어요.", ephemeral=True)
            return
        # consume item: prefer '최고급 사료' then '펫 사료'
        consumed = None
        for item_name in ("최고급 사료", "펫 사료"):
            if decrement_inventory_item(owner.id, item_name, 1):
                consumed = item_name
                break
        if not consumed:
            if interaction: await interaction.followup.send("❌ 사료가 없습니다. 상점에서 구매해 주세요.", ephemeral=True)
            return
        # effect
        if consumed == "최고급 사료":
            hunger = max(0, int(pet["hunger"]) - 25)
            affinity = min(100, int(pet["affinity"]) + 6)
        else:
            hunger = max(0, int(pet["hunger"]) - 15)
            affinity = min(100, int(pet["affinity"]) + 3)
        update_pet_stats(pet["id"], hunger=hunger, affinity=affinity)
        if interaction: await interaction.followup.send(f"🍖 `{consumed}` 를 사용했어요! 배고픔 {pet['hunger']}→{hunger}, 친밀도 {pet['affinity']}→{affinity}", ephemeral=True)

    async def play_with_pet(self, owner: discord.User, interaction: Optional[discord.Interaction] = None):
        pet = get_active_pet(owner.id)
        if not pet:
            if interaction: await interaction.followup.send("❌ 펫이 없어요.", ephemeral=True)
            return
        if not decrement_inventory_item(owner.id, "공놀이 세트", 1):
            if interaction: await interaction.followup.send("❌ '공놀이 세트'가 없습니다.", ephemeral=True)
            return
        hunger = min(100, int(pet["hunger"]) + 5)
        affinity = min(100, int(pet["affinity"]) + 5)
        update_pet_stats(pet["id"], hunger=hunger, affinity=affinity)
        if interaction: await interaction.followup.send(f"🎾 놀아주기 완료! 배고픔 {pet['hunger']}→{hunger}, 친밀도 {pet['affinity']}→{affinity}", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Pet(bot))
