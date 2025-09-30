import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import logging

logger = logging.getLogger(__name__)

class GuideSender(commands.Cog):
    """
    게임 봇의 기능 안내서를 특정 채널에 임베드 메시지로 전송하는 관리자용 Cog입니다.
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="가이드-전송", description="지정한 채널에 게임 봇 기능 안내서를 전송합니다.")
    @app_commands.describe(channel="안내서를 전송할 텍스트 채널")
    @app_commands.checks.has_permissions(administrator=True)
    async def send_guide(self, interaction: discord.Interaction, channel: discord.TextChannel):
        """
        관리자가 지정한 채널에 게임 봇 안내서를 여러 개의 임베드 메시지로 나누어 전송합니다.
        """
        await interaction.response.defer(ephemeral=True)

        embeds_to_send = []

        # 1. 환영 및 소개
        embed1 = discord.Embed(
            title="🏡 디스코드 마을에 오신 것을 환영합니다! - 게임 봇 안내서 🌟",
            description=(
                "안녕하세요, 새로운 주민님! 우리 마을에 오신 것을 진심으로 환영합니다.\n\n"
                "마을에서의 생활을 더욱 풍요롭게 만들어 줄 게임 요정 **『호시』**가 준비한 다양한 콘텐츠를 소개합니다. "
                "이 안내서를 따라 차근차근 즐기다 보면, 어느새 마을의 멋진 일원이 되어 있을 거예요!"
            ),
            color=0xFFD700  # Gold
        )
        embeds_to_send.append(embed1)

        # 2. 시작하기
        embed2 = discord.Embed(
            title="📝 시작하기: 모든 것의 기본",
            description="가장 먼저 알아야 할 필수 기능들입니다. 모든 활동은 여기에서 시작됩니다.",
            color=0x5865F2  # Discord Blurple
        )
        embed2.add_field(
            name="**1. 나의 정보 (프로필)**",
            value=(
                "**- 위치:** `#프로필` 채널의 패널\n"
                "**- 설명:** 여러분의 모든 정보를 한눈에 볼 수 있는 가장 중요한 공간입니다.\n"
                "**- 주요 기능:**\n"
                "  • **정보:** 현재 보유한 코인(재화), 레벨, 등급, 직업을 확인합니다.\n"
                "  • **아이템:** 인벤토리에 있는 모든 아이템을 종류별로 확인할 수 있습니다.\n"
                "  • **장비 장착/변경:** 활동에 필요한 도구를 장착하거나 변경할 수 있습니다.\n"
                "  • **아이템 사용:** `벌점 차감권` 같은 일부 아이템을 이곳에서 사용합니다."
            ),
            inline=False
        )
        embed2.add_field(
            name="**2. 레벨과 경험치 (XP)**",
            value=(
                "**- 설명:** 마을에서의 모든 활동은 여러분을 성장시킵니다. 레벨이 오르면 새로운 콘텐츠를 즐길 수 있습니다.\n"
                "**- 경험치 획득 방법:** 채팅, 음성 채널 참여, 모든 게임 활동, 퀘스트 완료 등"
            ),
            inline=False
        )
        embed2.add_field(
            name="**3. 재화 (코인 🪙)**",
            value=(
                "**- 설명:** 마을에서 사용하는 공식 화폐입니다. 아이템을 구매하거나 시설을 이용하는 데 사용됩니다.\n"
                "**- 코인 획득 방법:** 아이템 판매, 퀘스트 완료, 미니게임 승리, 유저 간 거래 등"
            ),
            inline=False
        )
        embed2.add_field(
            name="**4. 전직 시스템**",
            value=(
                "**- 설명:** 특정 레벨(50, 100)에 도달하면 전문 직업을 선택하여 강력한 **패시브 능력**을 얻을 수 있습니다.\n"
                "**- 1차 (Lv.50):** `낚시꾼`, `농부`, `광부`, `요리사`\n"
                "**- 2차 (Lv.100):** 1차 직업의 상위 직업\n"
                "**- 방법:** 레벨 달성 시 `#전직소`에 개인 스레드가 자동 생성되어 안내합니다."
            ),
            inline=False
        )
        embeds_to_send.append(embed2)

        # 3. 주요 활동
        embed3 = discord.Embed(
            title="🎣 주요 활동: 마을 생활의 중심",
            description="마을 주민이라면 누구나 즐길 수 있는 핵심 생활 콘텐츠입니다.",
            color=0x2ECC71  # Green
        )
        embed3.add_field(name="**1. 낚시** (`#강-낚시터`, `#바다-낚시터`)", value="낚싯대와 미끼를 장착하고, 타이밍에 맞춰 물고기를 낚아보세요! 월척이나 희귀 어종을 낚을 수도 있습니다.", inline=False)
        embed3.add_field(name="**2. 농사** (`#농장-생성`)", value="개인 농장에서 밭을 갈고, 씨앗을 심고, 물을 주어 작물을 수확하세요. 비 오는 날은 물주기가 자동입니다!", inline=False)
        embed3.add_field(name="**3. 채광** (`#광산`)", value="`광산 입장권`을 사용해 개인 광산에 입장하고, `곡괭이`로 광석을 찾아 채굴하세요.", inline=False)
        embed3.add_field(name="**4. 요리** (`#부엌-생성`)", value="`가마솥`에 다양한 재료를 조합하여 요리를 만드세요. 숨겨진 레시피를 최초로 발견하고 명성을 얻을 수 있습니다.", inline=False)
        embed3.add_field(name="**5. 대장간** (`#대장간`)", value="광물과 코인을 사용하여 각종 도구를 더 높은 등급으로 업그레이드할 수 있습니다. (24시간 소요)", inline=False)
        embeds_to_send.append(embed3)
        
        # 4. 펫 시스템
        embed4 = discord.Embed(
            title="🐾 펫 시스템: 당신의 소중한 동반자",
            description="신비한 알을 부화시켜 자신만의 펫을 키우고 함께 모험을 떠나세요.",
            color=0x7289DA # Discord Blue
        )
        embed4.add_field(name="**1. 펫 얻기** (`#인큐베이터`)", value="보유한 알을 부화기에 넣어 펫을 얻습니다.", inline=False)
        embed4.add_field(name="**2. 펫 관리 및 성장** (개인 스레드)", value="먹이주기, 놀아주기, 스탯 분배, 진화를 통해 펫을 성장시킬 수 있습니다.", inline=False)
        embed4.add_field(name="**3. 펫과 함께하는 모험**", value="**- 탐사 (`#펫-탐사`):** 펫을 탐사 보내 보상을 얻어오게 합니다.\n**- 대전 (`#펫-대전장`):** 다른 유저의 펫과 실력을 겨룹니다.", inline=False)
        embeds_to_send.append(embed4)

        # 5. 도전과 경쟁
        embed5 = discord.Embed(
            title="⚔️ 도전과 경쟁 콘텐츠",
            description="마을 생활에 익숙해졌다면, 다른 주민들과 힘을 합치거나 실력을 겨뤄보세요!",
            color=0xE74C3C  # Red
        )
        embed5.add_field(name="**1. 보스 레이드** (`#주간-보스`, `#월간-보스`)", value="모든 주민이 힘을 합쳐 강력한 보스를 처치하고 피해량 순위에 따라 보상을 받습니다.", inline=False)
        embed5.add_field(name="**2. 미니게임 (카지노)**", value="`#주사위-게임`, `#슬롯머신`, `#가위바위보` 채널에서 운을 시험하고 코인을 획득하세요.", inline=False)
        embeds_to_send.append(embed5)

        # 6. 교류와 편의
        embed6 = discord.Embed(
            title="🤝 교류와 편의 기능",
            description="다른 주민들과의 상호작용을 통해 마을 생활을 더욱 즐겁게 만들어보세요.",
            color=0x3498DB  # Blue
        )
        embed6.add_field(name="**1. 상점** (`#상점`)", value="아이템을 사거나 팔 수 있습니다. 시세는 매일 조금씩 변동됩니다.", inline=False)
        embed6.add_field(name="**2. 거래** (`#ATM`, `#거래소`)", value="다른 유저에게 코인을 보내거나, 1:1로 아이템/코인을 안전하게 교환하고, 우편을 보낼 수 있습니다.", inline=False)
        embed6.add_field(name="**3. 퀘스트** (`#오늘의-할일`)", value="매일/매주 주어지는 간단한 목표를 달성하고 보상을 받으세요. 모두 완료 시 보너스가 있습니다!", inline=False)
        embed6.add_field(name="**4. 친구 초대** (`#친구-초대`)", value="나만의 영구 초대 코드로 친구를 초대하고, 친구가 정식 주민이 되면 보상을 받습니다.", inline=False)
        embeds_to_send.append(embed6)

        # 7. 마무리
        embed7 = discord.Embed(
            description="우리 마을의 즐길 거리는 앞으로도 계속해서 늘어날 예정입니다. 궁금한 점이 있다면 언제든지 관리자나 다른 주민들에게 물어보세요!\n\n**그럼, 즐거운 마을 생활 되세요!**",
            color=0x99AAB5 # Greyple
        )
        embeds_to_send.append(embed7)

        try:
            for embed in embeds_to_send:
                await channel.send(embed=embed)
                await asyncio.sleep(0.5)  # API 속도 제한을 피하기 위한 짧은 딜레이
            
            await interaction.followup.send(f"✅ {channel.mention} 채널에 안내서를 성공적으로 전송했습니다.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send(f"❌ {channel.mention} 채널에 메시지를 보낼 권한이 없습니다. 채널 권한을 확인해주세요.", ephemeral=True)
        except Exception as e:
            logger.error(f"가이드 전송 중 오류 발생: {e}", exc_info=True)
            await interaction.followup.send(f"❌ 메시지를 보내는 중 알 수 없는 오류가 발생했습니다: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    """Cog를 봇에 추가합니다."""
    await bot.add_cog(GuideSender(bot))
