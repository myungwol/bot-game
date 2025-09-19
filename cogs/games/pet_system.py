# cogs/games/pet_system.py
# - 게임 봇의 기존 패턴을 그대로 따르는 펫 시스템 Cog
# - prefix 명령어(/pet, /pethatch)
# - 임베드/버튼은 DB에서 불러옴(없으면 안전한 기본값)
# - 패널 메시지는 고정(재설치 지원: save_panel_id / get_panel_id)
# - 버튼 연타 방지(ACK/락/디바운스) + safe_edit 재시도 내장
#
# 필요 DB Key 예시(권장):
#   panel_key            = "panel_pet"
#   embed_pet_status     = 펫 상태 패널 임베드 템플릿
#   embed_pet_hatch      = 부화 패널 임베드 템플릿
#   pet_panel_channel_id = 패널 상주 채널 ID (없으면 명령 실행 채널 사용)
#
# component(custom_id) 예시(권장):
#   pet_refresh, pet_feed, pet_play, pet_hatch_start, pet_open_ui
#
# pet_repository.py 는 프로젝트 루트의 database/supabase 유틸을 사용.
# (이미 네가 업로드한 pet_repository.py 기준으로 호출)

from __future__ import annotations
import discord
from discord.ext import commands, tasks
from discord import ui
import asyncio, time
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple
import logging

# 게임 봇의 공용 DB/헬퍼 (네 프로젝트 구조에 맞춰 import 경로 확인)
from database import (
    get_embed_from_db,
    format_embed_from_db,   # (template, variables...) -> discord.Embed
    get_panel_components_from_db,
    get_id,                 # 설정값 불러오기 (예: pet_panel_channel_id)
    save_panel_id,          # (panel_key, guild_id, channel_id, message_id)
    get_panel_id,           # (panel_key, guild_id) -> {"channel_id","message_id"}
)

# 펫 시스템 비즈니스 로직(이미 올려준 파일 기준)
import pet_repository as repo

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

KST = timezone(timedelta(hours=9))

# ===============================
# 공용: 안전 편집 & 연타 방지
# ===============================
class SafeView(ui.View):
    def __init__(self, cog: "PetSystem", *args):
        super().__init__(timeout=None)
        self.cog = cog
        self._extra_args = args

    async def _disable_all(self, interaction: discord.Interaction):
        tmp = type(self)(self.cog, *self._extra_args)
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

    def _lock_key(self, interaction: discord.Interaction) -> Tuple[int, int]:
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

# ===============================
# Hatch Panel View
# ===============================
class PetHatchView(SafeView):
    PANEL_KEY = "panel_pet"

    def __init__(self, cog: "PetSystem", owner: discord.User):
        super().__init__(cog, owner)
        self.owner = owner
        # DB에 저장된 컴포넌트(버튼 등) 불러오기 → 없으면 기본 버튼 구성
        comps = get_panel_components_from_db(self.PANEL_KEY) or []
        if comps:
            # 컴포넌트 JSON(버튼 라벨/이모지/커스텀ID 등)에 맞춰 동적 생성
            for comp in comps:
                if comp.get("type") != "button":
                    continue
                custom_id = comp.get("custom_id", "")
                label     = comp.get("label", "버튼")
                emoji     = comp.get("emoji", None)
                style     = getattr(discord.ButtonStyle, comp.get("style","secondary"), discord.ButtonStyle.secondary)
                btn = ui.Button(label=label, emoji=emoji, custom_id=custom_id, style=style)
                btn.callback = self.dispatch_callback
                self.add_item(btn)
        else:
            # 안전한 기본 버튼 세트
            for label, emoji, cid in [
                ("알 등록/부화 시작", "🥚", "pet_hatch_start"),
                ("펫 UI 열기", "🧩", "pet_open_ui"),
            ]:
                b = ui.Button(label=label, emoji=emoji, custom_id=cid, style=discord.ButtonStyle.primary)
                b.callback = self.dispatch_callback
                self.add_item(b)

    async def build_embed(self) -> discord.Embed:
        eggs = repo.get_user_inventory_eggs(self.owner.id)
        inc  = repo.get_active_incubation(self.owner.id)
        pet  = repo.get_active_pet(self.owner.id)

        # DB 템플릿 우선
        tpl = get_embed_from_db("embed_pet_hatch")
        if tpl:
            # 템플릿 변수 예: {egg_lines}, {status_line}
            egg_lines = [f"- {e['item_name']} ×{e['quantity']}" for e in eggs] or ["(보유한 알이 없습니다)"]
            if pet:
                status = "이미 펫을 키우는 중입니다. 새로 부화할 수 없습니다."
            elif inc:
                hat = inc["hatch_at"]
                if isinstance(hat, str):
                    hat = datetime.fromisoformat(hat.replace("Z","+00:00")).astimezone(KST)
                else:
                    hat = hat.astimezone(KST)
                status = f"알 부화 중… ⏳ (완료 예정: {hat:%Y-%m-%d %H:%M KST})"
            else:
                status = "부화 대기 중입니다. 알을 선택해 시작할 수 있어요."

            return format_embed_from_db(
                tpl,
                egg_lines="\n".join(egg_lines),
                status_line=status,
                user_name=self.owner.display_name
            )

        # 템플릿이 없으면 기본 임베드
        embed = discord.Embed(title="🐣 펫 부화하기", colour=discord.Colour.blurple())
        egg_lines = [f"- {e['item_name']} ×{e['quantity']}" for e in eggs] or ["(보유한 알이 없습니다)"]
        embed.add_field(name="보유한 알", value="\n".join(egg_lines), inline=False)
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
            embed.add_field(name="현재 상태", value="부화 대기 중입니다. 알을 선택해 시작할 수 있어요.", inline=False)
        embed.set_footer(text="부화는 2일(48시간) 소요됩니다. 부화 중에는 다른 알/펫 진행 불가.")
        return embed

    async def dispatch_callback(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer_update()

        cid = (interaction.data or {}).get("custom_id")
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
        if repo.get_active_pet(owner_id):
            await interaction.followup.send("❌ 이미 펫을 키우는 중이에요.", ephemeral=True); return
        if repo.get_active_incubation(owner_id):
            await interaction.followup.send("❌ 이미 알을 부화 중이에요.", ephemeral=True); return

        eggs = repo.get_user_inventory_eggs(owner_id)
        if not eggs:
            await interaction.followup.send("❌ 보유한 알이 없습니다. 상점에서 알을 먼저 구매해 주세요.", ephemeral=True); return
        egg_item_name = eggs[0]["item_name"]  # 선택 UI는 차후 확장

        if not repo.decrement_inventory_item(owner_id, egg_item_name, 1):
            await interaction.followup.send("❌ 인벤토리 차감 실패. 잠시 후 다시 시도해주세요.", ephemeral=True); return

        hatch_at = datetime.now(tz=KST) + timedelta(days=2)
        repo.create_pet_incubation(owner_id, egg_item_name, hatch_at.astimezone(timezone.utc))

        thread = await self.cog.ensure_pet_thread(self.owner)
        await self.cog.update_pet_ui(thread, self.owner)

        await interaction.followup.send(
            f"✅ `{egg_item_name}` 부화를 시작했어요! ⏳ {hatch_at:%Y-%m-%d %H:%M KST} 에 부화됩니다.\n"
            "펫 UI 스레드에서 진행 상태를 확인할 수 있어요.",
            ephemeral=True
        )

    async def open_pet_ui(self, interaction: discord.Interaction):
        thread = await self.cog.ensure_pet_thread(self.owner)
        await self.cog.update_pet_ui(thread, self.owner)
        await interaction.followup.send(f"🧩 펫 UI를 열었어요: <#{thread.id}>", ephemeral=True)

# ===============================
# Pet Panel View
# ===============================
class PetUIView(SafeView):
    PANEL_KEY = "panel_pet"

    def __init__(self, cog: "PetSystem", owner: discord.User):
        super().__init__(cog, owner)
        self.owner = owner
        comps = get_panel_components_from_db(self.PANEL_KEY) or []
        if comps:
            for comp in comps:
                if comp.get("type") != "button":
                    continue
                custom_id = comp.get("custom_id", "")
                label     = comp.get("label", "버튼")
                emoji     = comp.get("emoji", None)
                style     = getattr(discord.ButtonStyle, comp.get("style","secondary"), discord.ButtonStyle.secondary)
                btn = ui.Button(label=label, emoji=emoji, custom_id=custom_id, style=style)
                btn.callback = self.dispatch_callback
                self.add_item(btn)
        else:
            for label, emoji, cid in [
                ("새로고침", "🔄", "pet_refresh"),
                ("먹이 주기", "🍖", "pet_feed"),
                ("놀아주기", "🎾", "pet_play"),
            ]:
                b = ui.Button(label=label, emoji=emoji, custom_id=cid, style=discord.ButtonStyle.primary)
                b.callback = self.dispatch_callback
                self.add_item(b)

    async def build_embed(self) -> discord.Embed:
        inc = repo.get_active_incubation(self.owner.id)
        pet = repo.get_active_pet(self.owner.id)

        # 진행 중 부화 → 부화 임베드 템플릿 재활용
        if inc and inc.get("status") == "incubating":
            hat = inc["hatch_at"]
            if isinstance(hat, str):
                hat = datetime.fromisoformat(hat.replace("Z","+00:00")).astimezone(KST)
            else:
                hat = hat.astimezone(KST)
            tpl = get_embed_from_db("embed_pet_hatch")
            if tpl:
                return format_embed_from_db(
                    tpl,
                    egg_lines="(진행 중인 알 1개)",
                    status_line=f"알 부화 중… ⏳ (완료 예정: {hat:%Y-%m-%d %H:%M KST})",
                    user_name=self.owner.display_name
                )
            embed = discord.Embed(title="🐣 알 부화 중", colour=discord.Colour.gold())
            embed.add_field(name="부화 완료 예정", value=f"{hat:%Y-%m-%d %H:%M KST}", inline=False)
            embed.set_footer(text="완료 시간이 지나면 새로고침 시 자동으로 부화 처리됩니다.")
            return embed

        # 펫 상태 템플릿
        if pet:
            tpl = get_embed_from_db("embed_pet_status")
            if tpl:
                return format_embed_from_db(
                    tpl,
                    species=str(pet.get("species_key","unknown")),
                    attribute=str(pet.get("attribute_key","unknown")),
                    stage=str(pet.get("stage_key","hatch")),
                    level=str(pet.get("level",1)),
                    hp=str(pet.get("hp",0)),
                    atk=str(pet.get("atk",0)),
                    _def=str(pet.get("def",0)),
                    spd=str(pet.get("spd",0)),
                    affinity=str(pet.get("affinity",0)),
                    hunger=str(pet.get("hunger",0)),
                    user_name=self.owner.display_name,
                    image_url=pet.get("image_url") or ""
                )
            # 기본 임베드(템플릿 없을 때)
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

        # 아무 것도 없으면 안내
        tpl = get_embed_from_db("embed_pet_status")
        if tpl:
            return format_embed_from_db(
                tpl,
                species="(없음)",
                attribute="-",
                stage="-",
                level="-",
                hp="-", atk="-", _def="-", spd="-", affinity="-", hunger="-",
                user_name=self.owner.display_name,
                image_url=""
            )
        return discord.Embed(title="🐣 펫이 없어요", description="`/pethatch`로 알을 등록해 부화시켜 보세요!", colour=discord.Colour.red())

    async def dispatch_callback(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer_update()
        cid = (interaction.data or {}).get("custom_id")
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
                elif cid == "pet_open_ui":
                    # 혹시 패널 컴포넌트에 포함된 경우 대응
                    pass
                await self.cog.update_pet_ui(interaction.channel, self.owner)
            finally:
                await self._enable_all(interaction)

# ===============================
# Cog 본체
# ===============================
class PetSystem(commands.Cog):
    PANEL_KEY = "panel_pet"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.actor_locks: Dict[Tuple[int,int], asyncio.Lock] = {}
        self.last_action_ts: Dict[Tuple[int,int], float] = {}
        self.cooldown_sec: float = 0.8

        # 주기적으로 부화 완료 처리
        self.hatch_checker.start()

    def cog_unload(self):
        self.hatch_checker.cancel()

    # 안전 편집 (429/5xx 자동 재시도)
    async def safe_edit(self, message: discord.Message, **kwargs):
        backoff = [0.4, 0.8, 1.6, 2.0]
        for i, sleep_s in enumerate([0.0] + backoff):
            if sleep_s:
                await asyncio.sleep(sleep_s)
            try:
                return await message.edit(**kwargs)
            except Exception as e:
                status = getattr(e, "status", None)
                if status in (429, 500, 502, 503):
                    if i == len(backoff):
                        raise
                    continue
                raise

    # ---------- 명령어 ----------
    @commands.command(name="pet")
    async def cmd_pet(self, ctx: commands.Context):
        """펫 UI 스레드를 열고 패널을 표시"""
        owner = ctx.author
        thread = await self.ensure_pet_thread(owner, ctx=ctx)
        await self.update_pet_ui(thread, owner)
        await ctx.send(f"🧩 펫 UI: <#{thread.id}>")

    @commands.command(name="pethatch")
    async def cmd_pethatch(self, ctx: commands.Context):
        """부화 패널을 현재 채널에 표시 (설치 패널과 별개로 on-demand)"""
        owner = ctx.author
        view = PetHatchView(self, owner)
        embed = await view.build_embed()
        await ctx.send(embed=embed, view=view)

    # ---------- 패널 설치/재설치 ----------
    async def regenerate_panel(self, guild: discord.Guild):
        """패널 설치/재설치를 위한 헬퍼(PanelUpdater에서 호출)"""
        # 패널이 상주할 채널
        channel_id = get_id("pet_panel_channel_id", guild_id=guild.id) or 0
        channel: Optional[discord.TextChannel] = None
        if channel_id:
            channel = guild.get_channel(int(channel_id))
        if not channel:
            # fallback: 첫 번째 보이는 텍스트 채널
            for ch in guild.text_channels:
                if ch.permissions_for(guild.me).send_messages:
                    channel = ch
                    break
        if not channel:
            raise RuntimeError("펫 패널을 설치할 채널을 찾지 못했습니다.")

        # 기존 패널 메시지
        panel_info = get_panel_id(self.PANEL_KEY, guild_id=guild.id) or {}
        message_to_edit = None
        if panel_info.get("channel_id") and panel_info.get("message_id"):
            if int(panel_info["channel_id"]) == channel.id:
                try:
                    message_to_edit = await channel.fetch_message(int(panel_info["message_id"]))
                except discord.NotFound:
                    message_to_edit = None

        # 표시할 기본 뷰/임베드(설치용은 HatchView로 안내)
        # 설치 패널은 개인 소유자 개념이 없으므로 "누르면 개인별 진행"을 안내
        pseudo_owner = guild.me  # 안내 텍스트 대체용
        hatch_view = PetHatchView(self, pseudo_owner)
        embed = await hatch_view.build_embed()

        if message_to_edit:
            await self.safe_edit(message_to_edit, embed=embed, view=hatch_view)
            save_panel_id(self.PANEL_KEY, guild.id, channel.id, message_to_edit.id)
        else:
            msg = await channel.send(embed=embed, view=hatch_view)
            save_panel_id(self.PANEL_KEY, guild.id, channel.id, msg.id)

    # ---------- 스레드/패널 관리 ----------
    async def ensure_pet_thread(self, owner: discord.User, ctx: Optional[commands.Context] = None) -> discord.Thread:
        """
        항상 공개 스레드. 모두 볼 수 있어야 함.
        기존 thread_message_info를 별도로 사용하지 않고, 사용자별 하나씩 열어두는 형태.
        (원한다면 repo 레벨에 저장/조회 함수를 연결해 재사용해도 됨)
        """
        # 우선 ctx 채널에서 public thread를 만들 수 있으면 그걸 사용
        if ctx and isinstance(ctx.channel, discord.TextChannel):
            ch: discord.TextChannel = ctx.channel
            perms = ch.permissions_for(ctx.guild.me)
            if perms.create_public_threads and perms.send_messages:
                return await ch.create_thread(
                    name=f"🧩｜{owner.display_name}의 펫",
                    type=discord.ChannelType.public_thread
                )

        # 아니면 길드에서 가능한 채널 탐색
        for g in self.bot.guilds:
            member = g.get_member(owner.id)
            if not member:
                continue
            for ch in g.text_channels:
                perms = ch.permissions_for(g.me)
                if perms.create_public_threads and perms.send_messages:
                    return await ch.create_thread(
                        name=f"🧩｜{owner.display_name}의 펫",
                        type=discord.ChannelType.public_thread
                    )

        raise RuntimeError("펫 UI 스레드를 만들 텍스트 채널을 찾지 못했습니다.")

    async def update_pet_ui(self, thread: discord.Thread, owner: discord.User):
        """
        펫 UI를 스레드에 표시/갱신.
        고정 메시지 저장은 프로젝트마다 다르게 되어 있어서, 여기서는
        '없으면 새로 보내고, 있으면 위 메시지를 수정' 패턴만 사용.
        필요하면 thread_message_info 테이블로 변환 가능.
        """
        # 최신 메시지 50개 정도 훑어서 '내 펫' 패널이 있으면 그걸 수정 (간단한 휴리스틱)
        message_to_edit: Optional[discord.Message] = None
        try:
            async for msg in thread.history(limit=50):
                if msg.author == self.bot.user and msg.components:
                    message_to_edit = msg
                    break
        except Exception:
            message_to_edit = None

        view = PetUIView(self, owner)
        embed = await view.build_embed()

        if message_to_edit:
            await self.safe_edit(message_to_edit, embed=embed, view=view)
        else:
            await thread.send(embed=embed, view=view)

    # ---------- 자동 부화 처리 ----------
    @tasks.loop(minutes=5)
    async def hatch_checker(self):
        try:
            due_list = repo.list_due_incubations(limit=50)
            for inc in due_list:
                pet = repo.create_pet_from_incubation(inc)
                # 소유자 UI 갱신은 on-demand로 (스레드 위치 정보가 별도로 없으면 스킵)
                # 향후 thread_message_info를 펫에도 쓰면 여기서 갱신 가능.
        except Exception as e:
            logger.error(f"hatch_checker error: {e}", exc_info=True)

    @hatch_checker.before_loop
    async def before_hatch_checker(self):
        await self.bot.wait_until_ready()

    # ---------- 액션 ----------
    async def try_hatch_now(self, owner: discord.User):
        inc = repo.get_active_incubation(owner.id)
        if not inc: return False
        hat = inc["hatch_at"]
        if isinstance(hat, str):
            hat = datetime.fromisoformat(hat.replace("Z","+00:00"))
        now_utc = datetime.now(timezone.utc)
        if hat <= now_utc:
            repo.create_pet_from_incubation(inc)
            return True
        return False

    async def feed_pet(self, owner: discord.User, interaction: Optional[discord.Interaction] = None):
        pet = repo.get_active_pet(owner.id)
        if not pet:
            if interaction: await interaction.followup.send("❌ 펫이 없어요.", ephemeral=True)
            return
        # 소비 우선순위: 최고급 사료 > 펫 사료
        consumed = None
        for item_name in ("최고급 사료", "펫 사료"):
            if repo.decrement_inventory_item(owner.id, item_name, 1):
                consumed = item_name
                break
        if not consumed:
            if interaction: await interaction.followup.send("❌ 사료가 없습니다. 상점에서 구매해 주세요.", ephemeral=True)
            return
        if consumed == "최고급 사료":
            hunger = max(0, int(pet["hunger"]) - 25)
            affinity = min(100, int(pet["affinity"]) + 6)
        else:
            hunger = max(0, int(pet["hunger"]) - 15)
            affinity = min(100, int(pet["affinity"]) + 3)
        repo.update_pet_stats(pet["id"], hunger=hunger, affinity=affinity)
        if interaction:
            await interaction.followup.send(
                f"🍖 `{consumed}` 사용! 배고픔 {pet['hunger']}→{hunger}, 친밀도 {pet['affinity']}→{affinity}",
                ephemeral=True
            )

    async def play_with_pet(self, owner: discord.User, interaction: Optional[discord.Interaction] = None):
        pet = repo.get_active_pet(owner.id)
        if not pet:
            if interaction: await interaction.followup.send("❌ 펫이 없어요.", ephemeral=True)
            return
        if not repo.decrement_inventory_item(owner.id, "공놀이 세트", 1):
            if interaction: await interaction.followup.send("❌ '공놀이 세트'가 없습니다.", ephemeral=True)
            return
        hunger = min(100, int(pet["hunger"]) + 5)
        affinity = min(100, int(pet["affinity"]) + 5)
        repo.update_pet_stats(pet["id"], hunger=hunger, affinity=affinity)
        if interaction:
            await interaction.followup.send(
                f"🎾 놀아주기 완료! 배고픔 {pet['hunger']}→{hunger}, 친밀도 {pet['affinity']}→{affinity}",
                ephemeral=True
            )

# setup
async def setup(bot: commands.Bot):
    await bot.add_cog(PetSystem(bot))
