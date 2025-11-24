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
            title="🏡 서버에 오신 것을 환영합니다! - 게임봇 가이드 🌟",
            description=(
                "안녕하세요, 새로운 유저님! 우리 서버에 오신 것을 환영합니다.\n\n"
                "서버에서의 생활을 더욱 풍요롭게 해줄 게임의 요정 **『호시』**가 준비한, 다양한 콘텐츠를 소개합니다."
                "이 가이드를 따라 하나하나 즐기다 보면, 어느새 서버의 멋진 일원이 되어 있을 거예요!"
            ),
            color=0xFFD700  # Gold
        )
        embeds_to_send.append(embed1)

        # 2. 시작하기
        embed2 = discord.Embed(
            title="📝 시작하기: 모든 것의 기본",
            description="가장 먼저 알아두어야 할 필수 기능입니다. 모든 활동은 여기서부터 시작됩니다.",
            color=0x5865F2  # Discord Blurple
        )
        embed2.add_field(
            name="**1. 내 정보 (프로필)**",
            value=(
                "**- 위치:** <#1442265364573585598>\n"
                "**- 설명:** 당신의 모든 정보를 한눈에 확인할 수 있는 가장 중요한 곳입니다.\n"
                "**- 주요 기능:**\n"
                "  • **정보:** 현재 보유 코인(재화), 레벨, 등급, 직업을 확인합니다.\n"
                "  • **아이템:** 인벤토리에 있는 모든 아이템을 종류별로 확인할 수 있습니다.\n"
                "  • **장비 장착/변경:** 활동에 필요한 도구를 장착하거나 변경할 수 있습니다.\n"
                "  • **아이템 사용:** `벌점 취소권` 같은 일부 아이템은 여기서 사용합니다."
            ),
            inline=False
        )
        embed2.add_field(
            name="**2. 레벨과 경험치 (XP)**",
            value=(
                "**- 위치:** <#1442265342272340139>\n"
                "**- 설명:** 마을에서의 모든 활동은 당신을 성장시킵니다. 레벨이 오르면 새로운 콘텐츠를 즐길 수 있습니다.\n"
                "**- 경험치 획득 방법:** 채팅, 음성 채널 참여, 모든 게임 활동, 퀘스트 완료 등"
            ),
            inline=False
        )
        embed2.add_field(
            name="**3. 재화 (코인 🪙)**",
            value=(
                "**- 설명:** 마을에서 사용되는 공식 화폐입니다. 아이템을 구매하거나 시설을 이용하는 데 사용됩니다.\n"
                "**- 코인 획득 방법:** 아이템 판매, 퀘스트 완료, 미니게임 승리, 유저 간 거래 등"
            ),
            inline=False
        )
        embed2.add_field(
            name="**4. 전직 시스템**",
            value=(
                "**- 설명:** 특정 레벨(50, 100)에 도달하면, 전문 직업을 선택하여 강력한 **패시브 능력**을 얻을 수 있습니다.\n"
                "**- 1차 (Lv.50):** `낚시꾼`, `농부`, `광부`, `요리사`\n"
                "**- 2차 (Lv.100):** 1차 직업의 상위 직업\n"
                "**- 방법:** 레벨 달성 시 <#1442388763987804180>에 개인 스레드가 자동 생성되어 안내됩니다."
            ),
            inline=False
        )
        embeds_to_send.append(embed2)

        # 3. 주요 활동
        embed3 = discord.Embed(
            title="🎣 주요 활동: 마을 생활의 중심",
            description="마을 주민이라면 누구나 즐길 수 있는, 핵심적인 생활 콘텐츠입니다.",
            color=0x2ECC71  # Green
        )
        embed3.add_field(name="**1. 낚시** <#1442265410790756423>, <#1442265422580813874>", value="낚싯대와 미끼를 장착하고, 타이밍에 맞춰 물고기를 낚아 올리세요! 월척이나 희귀 어종을 낚을 수도 있습니다.", inline=False)
        embed3.add_field(name="**2. 농사** <#1442265503346462922>", value="개인 농장에서 밭을 갈고, 씨앗을 심고, 물을 주어 작물을 수확하세요. 비 오는 날에는 물주기가 자동으로 된답니다!", inline=False)
        embed3.add_field(name="**3. 채광** <#1442265657402986518>", value="`광산 입장권`을 사용해 개인 광산에 입장하고, `곡괭이`로 광석을 찾아 캐보세요.", inline=False)
        embed3.add_field(name="**4. 요리** <#1442265614898036777>", value="`가마솥`에 다양한 재료를 조합하여 요리를 만들어보세요. 숨겨진 레시피를 처음으로 발견하여 명성을 얻을 수도 있습니다.", inline=False)
        embed3.add_field(name="**5. 대장간** <#1442265814022750248>", value="광물과 코인을 사용하여 각종 도구를 더 높은 등급으로 업그레이드할 수 있습니다. (24시간 소요)", inline=False)
        embeds_to_send.append(embed3)
        
        # 4. 펫 시스템
        embed4 = discord.Embed(
            title="🐾 펫 시스템: 당신의 소중한 파트너",
            description="신비한 알을 부화시켜, 자신만의 펫을 키우고 함께 모험을 떠나보세요.",
            color=0x7289DA # Discord Blue
        )
        embed4.add_field(name="**1. 펫 얻기** <#1442265987041857649>", value="보유한 알을 부화기에 넣어 펫을 얻습니다.", inline=False)
        embed4.add_field(name="**2. 펫 관리와 성장** (개인 스레드)", value="먹이주기, 놀아주기, 스탯 분배, 진화를 통해 펫을 성장시킬 수 있습니다.", inline=False)
        embed4.add_field(name="**3. 펫과의 모험**", value="**- 탐험 <#1442265905005461585>:** 펫을 탐험 보내 보상을 얻게 합니다.\n**- 대전 <#1442265921463783556>:** 다른 유저의 펫과 실력을 겨룹니다.", inline=False)
        embed4.add_field(name="**4. 보스 레이드** <#1442265868586188881>, <#1442265880825430026>", value="모든 주민이 힘을 합쳐 강력한 보스를 물리치고, 가한 피해량 순위에 따라 보상을 받습니다.", inline=False)
        embeds_to_send.append(embed4)

        # 5. 도전과 경쟁
        embed5 = discord.Embed(
            title="⚔️ 도전과 경쟁 콘텐츠",
            description="마을 생활에 익숙해졌다면, 다른 주민들과 힘을 합치거나 실력을 겨뤄보세요!",
            color=0xE74C3C  # Red
        )
        embed5.add_field(name="**1. 미니게임**", value="<#1442266017244909688>, <#1442266035637063720>, <#1442266052259221697> 채널에서 운을 시험하고 코인을 획득해보세요.", inline=False)
        embeds_to_send.append(embed5)

        # 6. 교류와 편의 기능
        embed6 = discord.Embed(
            title="🤝 교류와 편의 기능",
            description="다른 주민과의 상호작용을 통해 마을 생활을 더욱 즐겁게 만들어 보세요.",
            color=0x3498DB  # Blue
        )
        embed6.add_field(name="**1. 상점** <#1442264272548794440>", value="아이템을 사거나 팔 수 있습니다. 시세는 매일 조금씩 변동됩니다.", inline=False)
        embed6.add_field(name="**2. 거래** <#1442264345827606609>, <#1442265323775590471>", value="다른 유저에게 코인을 보내거나, 1대1로 아이템/코인을 안전하게 교환하거나, 우편을 보낼 수 있습니다.", inline=False)
        embed6.add_field(name="**3. 출석&퀘스트** <#1442264394850631731>", value="매일/매주 주어지는 간단한 목표를 달성하고 보상을 받으세요. 모두 완료하면 보너스가 있습니다!", inline=False)
        embeds_to_send.append(embed6)

        # 7. 맺음말
        embed7 = discord.Embed(
            description="무언가 질문이 있다면, 언제든지 <@835608295796113468>에게 물어보세요!\n\n**그럼, 즐거운 서버 생활을 보내시길 바랍니다!**",
            color=0x99AAB5 # Greyple
        )
        embeds_to_send.append(embed7)

        try:
            for embed in embeds_to_send:
                await channel.send(embed=embed)
                await asyncio.sleep(0.5)  # API 속도 제한을 피하기 위한 짧은 지연
            
            await interaction.followup.send(f"✅ {channel.mention} 채널에 가이드를 정상적으로 전송했습니다.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send(f"❌ {channel.mention} 채널에 메시지를 전송할 권한이 없습니다. 채널 권한을 확인해주세요.", ephemeral=True)
        except Exception as e:
            logger.error(f"가이드 전송 중 오류가 발생했습니다: {e}", exc_info=True)
            await interaction.followup.send(f"❌ 메시지를 전송하는 중 알 수 없는 오류가 발생했습니다: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    """Cog를 봇에 추가합니다."""
    await bot.add_cog(GuideSender(bot))
