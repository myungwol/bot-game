import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import logging

logger = logging.getLogger(__name__)

class GuideSender(commands.Cog):
    """
    ゲームボットの機能ガイドを特定のチャンネルに埋め込みメッセージとして送信する管理者用Cogです。
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ガイド送信", description="指定したチャンネルにゲームボット機能ガイドを送信します。")
    @app_commands.describe(channel="ガイドを送信するテキストチャンネル")
    @app_commands.checks.has_permissions(administrator=True)
    async def send_guide(self, interaction: discord.Interaction, channel: discord.TextChannel):
        """
        管理者が指定したチャンネルに、ゲームボットのガイドを複数の埋め込みメッセージに分けて送信します。
        """
        await interaction.response.defer(ephemeral=True)

        embeds_to_send = []

        # 1. 歓迎と紹介
        embed1 = discord.Embed(
            title="🏡 気まぐれへようこそ！ - ゲームボットガイド 🌟",
            description=(
                "こんにちは、新しい住民さん！ 私たちの村へようこそ。\n\n"
                "村での生活をより豊かにしてくれるゲームの妖精 **『ほし』**が用意した、様々なコンテンツをご紹介します。"
                "このガイドに沿って一つ一つ楽しんでいけば、いつの間にか村の素敵な一員になっているはずです！"
            ),
            color=0xFFD700  # Gold
        )
        embeds_to_send.append(embed1)

        # 2. はじめに
        embed2 = discord.Embed(
            title="📝 はじめに：すべての基本",
            description="最初に知っておくべき必須機能です。すべての活動はここから始まります。",
            color=0x5865F2  # Discord Blurple
        )
        embed2.add_field(
            name="**1. マイ情報（プロフィール）**",
            value=(
                "**- 場所:** <#1433383447941873714>\n"
                "**- 説明:** あなたのすべての情報を一目で確認できる最も重要な場所です。\n"
                "**- 主な機能:**\n"
                "  • **情報:** 現在の所持コイン（財貨）、レベル、等級、職業を確認します。\n"
                "  • **アイテム:** インベントリにあるすべてのアイテムを種類別に確認できます。\n"
                "  • **装備の装着/変更:** 活動に必要な道具を装着したり変更したりできます。\n"
                "  • **アイテム使用:** `罰点取り消し券`のような一部のアイテムはここで使用します。"
            ),
            inline=False
        )
        embed2.add_field(
            name="**2. レベルと経験値（XP）**",
            value=(
                "**- 場所:** <#1433382545986420737>\n"
                "**- 説明:** 村でのすべての活動はあなたを成長させます。レベルが上がると新しいコンテンツを楽しむことができます。\n"
                "**- 経験値の獲得方法:** チャット、ボイスチャンネルへの参加、すべてのゲーム活動、クエスト完了など"
            ),
            inline=False
        )
        embed2.add_field(
            name="**3. 財貨（コイン 🪙）**",
            value=(
                "**- 説明:** 村で使用される公式通貨です。アイテムを購入したり、施設を利用したりするのに使われます。\n"
                "**- コインの獲得方法:** アイテム売却、クエスト完了、ミニゲームでの勝利、ユーザー間取引など"
            ),
            inline=False
        )
        embed2.add_field(
            name="**4. 転職システム**",
            value=(
                "**- 説明:** 特定のレベル（50、100）に達すると、専門職を選択して強力な**パッシブ能力**を得ることができます。\n"
                "**- 1次（Lv.50）:** `釣り人`、`農家`、`鉱夫`、`料理人`\n"
                "**- 2次（Lv.100）:** 1次職の上位職\n"
                "**- 方法:** レベル達成時に <#1433382608783540314> に個人スレッドが自動生成され、案内されます。"
            ),
            inline=False
        )
        embeds_to_send.append(embed2)

        # 3. 主な活動
        embed3 = discord.Embed(
            title="🎣 主な活動：村の生活の中心",
            description="村の住民なら誰でも楽しめる、中心的な生活コンテンツです。",
            color=0x2ECC71  # Green
        )
        embed3.add_field(name="**1. 釣り** <#1433384242750034002>, <#1433384273079173240>", value="釣り竿とエサを装着し、タイミングを合わせて魚を釣り上げましょう！大物や珍しい魚種を釣ることもできます。", inline=False)
        embed3.add_field(name="**2. 農業** <#1433384508299804732>", value="個人農場で畑を耕し、種を植え、水をやって作物を収穫しましょう。雨の日は水やりが自動で行われます！", inline=False)
        embed3.add_field(name="**3. 採掘** <#1433384576016715827>", value="`鉱山入場券`を使って個人鉱山に入場し、`ツルハシ`で鉱石を探して採掘しましょう。", inline=False)
        embed3.add_field(name="**4. 料理** <#1433383798195748935>", value="`釜`に様々な材料を組み合わせて料理を作りましょう。隠されたレシピを初めて発見して名声を得ることもできます。", inline=False)
        embed3.add_field(name="**5. 鍛冶屋** <#1433383652447879339>", value="鉱物とコインを使って各種道具をより高い等級にアップグレードできます。（24時間所要）", inline=False)
        embeds_to_send.append(embed3)
        
        # 4. ペットシステム
        embed4 = discord.Embed(
            title="🐾 ペットシステム：あなたの大切なパートナー",
            description="神秘的な卵を孵化させて、自分だけのペットを育て、共に冒険に出かけましょう。",
            color=0x7289DA # Discord Blue
        )
        embed4.add_field(name="**1. ペットを手に入れる** <#1433384994738405418>", value="所持している卵を孵化器に入れてペットを手に入れます。", inline=False)
        embed4.add_field(name="**2. ペットの管理と成長**（個人スレッド）", value="エサやり、遊び、ステータス分配、進化を通じてペットを成長させることができます。", inline=False)
        embed4.add_field(name="**3. ペットとの冒険**", value="**- 探検 <#1433384932713037844>:** ペットを探検に送り、報酬を得させます。\n**- 対戦 <#1433384956050280489>:** 他のユーザーのペットと腕を競います。", inline=False)
        embed4.add_field(name="**4. ボスレイド** <#1433384894100148234>, <#1433384912777515038>", value="すべての住民が力を合わせ、強力なボスを倒し、与えたダメージの順位に応じて報酬を受け取ります。", inline=False)
        embeds_to_send.append(embed4)

        # 5. 挑戦と競争
        embed5 = discord.Embed(
            title="⚔️ 挑戦と競争のコンテンツ",
            description="村の生活に慣れてきたら、他の住民と力を合わせたり、腕を競ったりしてみましょう！",
            color=0xE74C3C  # Red
        )
        embed5.add_field(name="**1. ミニゲーム（カジノ）**", value="<#1433383509501939822>、<#1433383543383523399>、<#1433383572617560075>のチャンネルで運試しをしてコインを獲得しましょう。", inline=False)
        embeds_to_send.append(embed5)

        # 6. 交流と便利機能
        embed6 = discord.Embed(
            title="🤝 交流と便利機能",
            description="他の住民との相互作用を通じて、村の生活をより楽しくしましょう。",
            color=0x3498DB  # Blue
        )
        embed6.add_field(name="**1. 商店** <#1433383381500166174>", value="アイテムを買ったり売ったりできます。相場は毎日少しずつ変動します。", inline=False)
        embed6.add_field(name="**2. 取引** <#1433383401053753484>, <#1433383425796210748>", value="他のユーザーにコインを送ったり、1対1でアイテム/コインを安全に交換したり、郵便を送ったりできます。", inline=False)
        embed6.add_field(name="**3. クエスト** <#1433382520669601822>", value="毎日/毎週与えられる簡単な目標を達成して報酬を受け取りましょう。すべて完了するとボーナスがあります！", inline=False)
        embeds_to_send.append(embed6)

        # 7. 締め
        embed7 = discord.Embed(
            description="何か質問があれば、いつでも<@835608295796113468>と管理者や他の住民に聞いてみてください！\n\n**それでは、楽しい村の生活をお送りください！**",
            color=0x99AAB5 # Greyple
        )
        embeds_to_send.append(embed7)

        try:
            for embed in embeds_to_send:
                await channel.send(embed=embed)
                await asyncio.sleep(0.5)  # APIレート制限を避けるための短い遅延
            
            await interaction.followup.send(f"✅ {channel.mention} チャンネルにガイドを正常に送信しました。", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send(f"❌ {channel.mention} チャンネルにメッセージを送信する権限がありません。チャンネルの権限を確認してください。", ephemeral=True)
        except Exception as e:
            logger.error(f"ガイド送信中にエラーが発生しました: {e}", exc_info=True)
            await interaction.followup.send(f"❌ メッセージの送信中に不明なエラーが発生しました: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    """Cogをボットに追加します。"""
    await bot.add_cog(GuideSender(bot))
