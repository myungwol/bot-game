# cogs/GuideSender.py

import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import logging

logger = logging.getLogger(__name__)

class GuideSender(commands.Cog):
    """
    게임 봇 기능 가이드를 특정 채널에 임베드 메시지로 보내는 관리자용 Cog입니다.
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="가이드전송", description="지정한 채널에 게임봇 기능 가이드를 전송합니다.")
    @app_commands.describe(channel="가이드를 전송할 텍스트 채널")
    @app_commands.checks.has_permissions(administrator=True)
    async def send_guide(self, interaction: discord.Interaction, channel: discord.TextChannel):
        """
        관리자가 지정한 채널에, 게임봇 가이드를 여러 임베드 메시지로 나누어 전송합니다.
        """
        await interaction.response.defer(ephemeral=True)

        embeds_to_send = []

        # 1. 환영과 소개
        embed1 = discord.Embed(
            title="🎮 서버 게임봇 가이드",
            description=(
                "안녕하세요, 유저님! 우리 서버에 오신 것을 환영합니다.\n\n"
                "여러분의 서버 활동을 더욱 즐겁게 만들어 줄 **게임 시스템**을 소개합니다.\n"
                "이 가이드를 따라 다양한 콘텐츠를 즐기며 성장해보세요!"
            ),
            color=0xFFD700  # Gold
        )
        embeds_to_send.append(embed1)

        # 2. 시작하기 (기본 정보)
        embed2 = discord.Embed(
            title="📂 기본 정보",
            description="서버 생활의 기초가 되는 필수 기능입니다.",
            color=0x5865F2  # Discord Blurple
        )
        embed2.add_field(
            name="📋 내 정보 (프로필)",
            value=(
                "> **위치:** <#1442265364573585598>\n"
                "자신의 레벨, 코인, 아이템 등 모든 정보를 확인할 수 있습니다.\n"
                "- **정보:** 보유 코인, 레벨, 직업 확인\n"
                "- **아이템/장비:** 인벤토리 확인 및 도구 장착\n"
                "- **소모품 사용:** 각종 티켓 및 아이템 사용"
            ),
            inline=False
        )
        embed2.add_field(
            name="📈 레벨과 경험치 (XP)",
            value=(
                "> **위치:** <#1442265342272340139>\n"
                "서버 활동(채팅, 음성, 게임 등)을 통해 경험치를 얻고 성장합니다.\n"
                "레벨이 오르면 새로운 기능이 해금되거나 보상을 받을 수 있습니다."
            ),
            inline=False
        )
        embed2.add_field(
            name="🪙 재화 (코인)",
            value=(
                "서버 내에서 사용되는 화폐입니다.\n"
                "아이템 구매, 강화, 유저 간 거래 등에 사용됩니다."
            ),
            inline=False
        )
        embed2.add_field(
            name="🛡️ 전직 시스템",
            value=(
                "특정 레벨 달성 시 전문 직업을 선택하여 **패시브 능력**을 얻습니다.\n"
                "- **1차 전직 (Lv.50):** `낚시꾼`, `농부`, `광부`, `요리사`\n"
                "- **2차 전직 (Lv.100):** 상위 전문 직업\n"
                "※ 레벨 달성 시 <#1442388763987804180> 채널에 안내 메시지가 생성됩니다."
            ),
            inline=False
        )
        embeds_to_send.append(embed2)

        # 3. 주요 활동 (생활 콘텐츠)
        embed3 = discord.Embed(
            title="🎣 생활 콘텐츠",
            description="코인을 벌고 재료를 모으는 핵심 활동입니다.",
            color=0x2ECC71  # Green
        )
        embed3.add_field(
            name="🎣 낚시",
            value=(
                "> **위치:** <#1442265410790756423>(강), <#1442265422580813874>(바다)\n"
                "낚싯대와 미끼를 사용하여 물고기를 낚으세요.\n"
                "바다 낚시는 더 좋은 낚싯대가 필요합니다."
            ),
            inline=False
        )
        embed3.add_field(
            name="🌾 농사",
            value=(
                "> **위치:** <#1442265503346462922>\n"
                "개인 농장을 생성하여 작물을 재배하세요.\n"
                "밭 갈기 → 씨앗 심기 → 물 주기 → 수확 과정을 거칩니다.\n"
                "※ 비가 오는 날에는 물을 주지 않아도 됩니다!"
            ),
            inline=False
        )
        embed3.add_field(
            name="⛏️ 채광",
            value=(
                "> **위치:** <#1442265657402986518>\n"
                "`광산 입장권`을 사용하여 10분간 광물을 캘 수 있습니다.\n"
                "희귀한 광석을 얻어 장비를 업그레이드하세요."
            ),
            inline=False
        )
        embed3.add_field(
            name="🍲 요리",
            value=(
                "> **위치:** <#1442265614898036777>\n"
                "`가마솥`에 재료를 넣고 다양한 요리를 연구하세요.\n"
                "숨겨진 레시피를 발견하는 재미가 있습니다."
            ),
            inline=False
        )
        embed3.add_field(
            name="⚒️ 대장간",
            value=(
                "> **위치:** <#1442265814022750248>\n"
                "채광한 광물과 코인으로 도구 등급을 올릴 수 있습니다.\n"
                "도구가 좋을수록 작업 효율이 증가합니다."
            ),
            inline=False
        )
        embeds_to_send.append(embed3)
        
        # 4. 펫 시스템
        embed4 = discord.Embed(
            title="🐾 펫 시스템",
            description="나만의 펫을 키우고 함께 모험을 떠나세요.",
            color=0x7289DA # Discord Blue
        )
        embed4.add_field(
            name="🥚 펫 분양 및 육성",
            value=(
                "> **위치:** <#1442265987041857649>\n"
                "알을 부화시켜 펫을 얻고, 먹이를 주거나 놀아주며 키울 수 있습니다.\n"
                "펫이 성장하면 스탯을 분배하거나 진화할 수 있습니다."
            ),
            inline=False
        )
        embed4.add_field(
            name="🧭 모험과 경쟁",
            value=(
                "- **탐험 <#1442265905005461585>:** 펫을 탐험 보내 보상을 획득합니다.\n"
                "- **대전 <#1442265921463783556>:** 다른 유저의 펫과 실력을 겨룹니다."
            ),
            inline=False
        )
        embed4.add_field(
            name="👹 보스 레이드",
            value=(
                "> **위치:** <#1442265868586188881>(월간), <#1442265880825430026>(주간)\n"
                "서버의 모든 유저가 협동하여 강력한 보스를 처치합니다.\n"
                "기여도(피해량)에 따라 차등 보상이 지급됩니다."
            ),
            inline=False
        )
        embeds_to_send.append(embed4)

        # 5. 미니게임 & 편의기능
        embed5 = discord.Embed(
            title="🎲 미니게임 & 편의기능",
            description="가볍게 즐기거나 유용한 기능들입니다.",
            color=0x99AAB5  # Greyple
        )
        embed5.add_field(
            name="🎰 미니게임",
            value=(
                "운을 시험하고 코인을 획득해보세요!\n"
                "- **주사위:** <#1442266017244909688>\n"
                "- **슬롯머신:** <#1442266035637063720>\n"
                "- **가위바위보:** <#1442266052259221697>"
            ),
            inline=False
        )
        embed5.add_field(
            name="🏪 경제 활동",
            value=(
                "- **상점 <#1442264272548794440>:** 아이템 구매 및 판매\n"
                "- **거래소 <#1442264345827606609>:** 유저 간 아이템/코인 거래\n"
                "- **ATM <#1442265323775590471>:** 간편 송금"
            ),
            inline=False
        )
        embed5.add_field(
            name="📅 일일 활동",
            value=(
                "> **위치:** <#1442264394850631731>\n"
                "매일 출석 체크와 일일/주간 퀘스트를 완료하여 보너스를 받으세요."
            ),
            inline=False
        )
        embeds_to_send.append(embed5)

        # 6. 맺음말
        embed6 = discord.Embed(
            description=(
                "더 궁금한 점이 있다면 언제든지 <@835608295796113468>에게 문의해주세요.\n\n"
                "**즐거운 서버 생활 되시길 바랍니다!** 🎮"
            ),
            color=0x2C2F33 # Dark
        )
        embeds_to_send.append(embed6)

        try:
            for embed in embeds_to_send:
                await channel.send(embed=embed)
                await asyncio.sleep(0.5)  # API 속도 제한 방지
            
            await interaction.followup.send(f"✅ {channel.mention} 채널에 가이드를 전송했습니다.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send(f"❌ {channel.mention} 채널에 메시지를 보낼 권한이 없습니다.", ephemeral=True)
        except Exception as e:
            logger.error(f"가이드 전송 중 오류: {e}", exc_info=True)
            await interaction.followup.send(f"❌ 전송 중 오류가 발생했습니다: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    """Cog를 봇에 추가합니다."""
    await bot.add_cog(GuideSender(bot))
