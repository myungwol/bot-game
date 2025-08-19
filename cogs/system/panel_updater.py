# bot-management/cogs/server/system.py (게임 봇 패널 재설치 요청 기능 최종 완성본)

import discord
from discord.ext import commands
from discord import app_commands
import logging
from typing import Optional, List
from datetime import datetime, timezone

from utils.database import (
    get_config, save_id_to_db, save_config_to_db, get_id,
    get_all_stats_channels, add_stats_channel, remove_stats_channel
)
from utils.ui_defaults import UI_ROLE_KEY_MAP, SETUP_COMMAND_MAP

logger = logging.getLogger(__name__)

class ServerSystem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        logger.info("System (통합 관리 명령어) Cog가 성공적으로 초기화되었습니다.")

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(f"❌ このコマンドを使用するには、次の権限が必要です: `{', '.join(error.missing_permissions)}`", ephemeral=True)
        else:
            logger.error(f"'{interaction.command.qualified_name}'コマンドの処理中にエラーが発生しました: {error}", exc_info=True)
            if interaction.response.is_done():
                await interaction.followup.send("❌ コマンドの処理中に予期せぬエラーが発生しました。", ephemeral=True)
            else:
                await interaction.response.send_message("❌ コマンドの処理中に予期せぬエラーが発生しました。", ephemeral=True)

    async def setup_action_autocomplete(self, interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        choices = []
        
        # 1. 채널/패널 설정
        for key, info in SETUP_COMMAND_MAP.items():
            choice_name = f"{info.get('friendly_name', key)} 設定"
            if current.lower() in choice_name.lower():
                choices.append(app_commands.Choice(name=choice_name, value=f"channel_setup:{key}"))
        
        # 2. 역할 설정
        role_setup_actions = {
            "role_setup:bump_reminder_role_id": "[알림] Disboard BUMP 알림 역할 설정",
            "role_setup:dissoku_reminder_role_id": "[알림] Dissoku UP 알림 역할 설정",
        }
        for key, name in role_setup_actions.items():
            if current.lower() in name.lower():
                choices.append(app_commands.Choice(name=name, value=key))

        # 3. 모든 관리봇 패널 재설치
        panel_actions = {"panels_regenerate_all": "[패널] 모든 관리 패널 재설치"}
        for key, name in panel_actions.items():
            if current.lower() in name.lower():
                choices.append(app_commands.Choice(name=name, value=key))

        # 4. 역할 동기화
        role_actions = {"roles_sync": "[역할] 모든 역할 DB와 동기화"}
        for key, name in role_actions.items():
            if current.lower() in name.lower():
                choices.append(app_commands.Choice(name=name, value=key))
        
        # 5. 통계 기능
        stats_actions = {
            "stats_set": "[통계] 통계 채널 설정/제거",
            "stats_refresh": "[통계] 모든 통계 채널 새로고침",
            "stats_list": "[통계] 설정된 통계 채널 목록 보기",
        }
        for key, name in stats_actions.items():
            if current.lower() in name.lower():
                choices.append(app_commands.Choice(name=name, value=key))
        
        # 6. 게임 봇 패널 개별 재설치 요청
        game_bot_cogs = ["Commerce", "Fishing", "UserProfile"]
        game_panel_keys = [key for key, info in SETUP_COMMAND_MAP.items() if info.get("cog_name") in game_bot_cogs]
        for key in game_panel_keys:
            name = f"{SETUP_COMMAND_MAP[key]['friendly_name']} 재설치 요청"
            if current.lower() in name.lower():
                choices.append(app_commands.Choice(name=name, value=f"request_regenerate:{key}"))

        return sorted(choices, key=lambda c: c.name)[:25]

    @app_commands.command(name="setup", description="[管理者] ボットのチャンネル、役割、統計など、すべての設定を管理します。")
    @app_commands.describe(
        action="実行するタスクを選択してください。",
        channel="[チャンネル/統計] タスクに必要なチャンネルを選択してください。",
        role="[役割/統計] タスクに必要な役割を選択してください。",
        stat_type="[統計] 表示する統計の種類を選択してください。",
        template="[統計] チャンネル名の形式を指定します (例: 👤 ユーザー: {count}人)"
    )
    @app_commands.autocomplete(action=setup_action_autocomplete)
    @app_commands.choices(stat_type=[
        app_commands.Choice(name="[설정] 전체 멤버 수 (봇 포함)", value="total"),
        app_commands.Choice(name="[설정] 유저 수 (봇 제외)", value="humans"),
        app_commands.Choice(name="[설정] 봇 수", value="bots"),
        app_commands.Choice(name="[설정] 서버 부스트 수", value="boosters"),
        app_commands.Choice(name="[설정] 특정 역할 멤버 수", value="role"),
        app_commands.Choice(name="[삭제] 이 채널의 통계 설정 삭제", value="remove"),
    ])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup(self, interaction: discord.Interaction,
                    action: str,
                    channel: Optional[discord.TextChannel | discord.VoiceChannel | discord.ForumChannel] = None,
                    role: Optional[discord.Role] = None,
                    stat_type: Optional[str] = None,
                    template: Optional[str] = None):
        
        await interaction.response.defer(ephemeral=True)

        if action.startswith("request_regenerate:"):
            panel_key = action.split(":", 1)[1]
            db_key = f"panel_regenerate_request_{panel_key}"
            await save_config_to_db(db_key, datetime.now(timezone.utc).timestamp())
            
            friendly_name = SETUP_COMMAND_MAP.get(panel_key, {}).get("friendly_name", panel_key)
            return await interaction.followup.send(
                f"✅ ゲームボットに **{friendly_name}** の再設置を要請しました。\n"
                "ゲームボットがオンラインの場合、約10秒以内にパネルが更新されます。",
                ephemeral=True
            )

        elif action.startswith("channel_setup:"):
            setting_key = action.split(":", 1)[1]
            config = SETUP_COMMAND_MAP.get(setting_key)
            if not config:
                return await interaction.followup.send("❌ 無効な設定キーです。", ephemeral=True)
            
            required_channel_type = config.get("channel_type", "text")
            error_msg = None
            if not channel:
                error_msg = f"❌ このタスクを実行するには、「channel」オプションに**{required_channel_type}チャンネル**を指定する必要があります。"
            elif (required_channel_type == "text" and not isinstance(channel, discord.TextChannel)) or \
                 (required_channel_type == "voice" and not isinstance(channel, discord.VoiceChannel)) or \
                 (required_channel_type == "forum" and not isinstance(channel, discord.ForumChannel)):
                error_msg = f"❌ このタスクには**{required_channel_type}チャンネル**が必要です。正しいタイプのチャンネルを選択してください。"
            
            if error_msg:
                return await interaction.followup.send(error_msg, ephemeral=True)

            db_key, friendly_name = config['key'], config['friendly_name']
            await save_id_to_db(db_key, channel.id)

            cog_to_reload = self.bot.get_cog(config["cog_name"])
            if cog_to_reload and hasattr(cog_to_reload, 'load_configs'):
                await cog_to_reload.load_configs()
                logger.info(f"/{interaction.command.name} 명령어로 인해 '{config['cog_name']}' Cog의 설정이 새로고침되었습니다.")
            
            if config.get("type") == "panel":
                # 관리 봇 자신에게 regenerate_panel 기능이 있는지 확인
                if hasattr(cog_to_reload, 'regenerate_panel'):
                    success = False
                    # TicketSystem처럼 추가 인자가 필요한 경우를 대비
                    if config["cog_name"] == "TicketSystem":
                        panel_type = setting_key.replace("panel_", "")
                        success = await cog_to_reload.regenerate_panel(channel, panel_type)
                    else:
                        success = await cog_to_reload.regenerate_panel(channel)
                        
                    if success:
                        await interaction.followup.send(f"✅ `{channel.mention}` チャンネルに **{friendly_name}** パネルを正常に設置しました。", ephemeral=True)
                    else:
                        await interaction.followup.send(f"❌ `{channel.mention}` チャンネルへのパネル設置中にエラーが発生しました。詳細はログを確認してください。", ephemeral=True)
                else:
                    # 게임 봇 패널처럼 관리 봇에 기능이 없는 경우, DB에 채널 설정만 저장하고 넘어감
                    await interaction.followup.send(f"✅ **{friendly_name}** を `{channel.mention}` チャンネルに設定しました。\n"
                                                      "パネルの設置/更新は `/setup` の「再設置要請」アクションを使用してください。", ephemeral=True)
            else:
                await interaction.followup.send(f"✅ **{friendly_name}** を `{channel.mention}` チャンネルに設定しました。", ephemeral=True)

        elif action.startswith("role_setup:"):
            db_key = action.split(":", 1)[1]
            if not role:
                return await interaction.followup.send("❌ このタスクを実行するには、「role」オプションに役割を指定する必要があります。", ephemeral=True)
            
            friendly_name = "알림 역할"
            for choice in await self.setup_action_autocomplete(interaction, ""):
                if choice.value == action:
                    friendly_name = choice.name.replace(" 설정", "")
            
            await save_id_to_db(db_key, role.id)
            
            cog_to_reload = self.bot.get_cog("Reminder")
            if cog_to_reload and hasattr(cog_to_reload, 'load_configs'):
                await cog_to_reload.load_configs()
            
            await interaction.followup.send(f"✅ **{friendly_name}** を `{role.mention}` 役割に設定しました。", ephemeral=True)

        elif action == "panels_regenerate_all":
            setup_map = get_config("SETUP_COMMAND_MAP", {})
            success_list = []
            failure_list = []

            await interaction.followup.send("⏳ すべての管理パネルの再設置を開始します...", ephemeral=True)

            for key, info in setup_map.items():
                # 관리 봇이 직접 제어할 수 있는 패널만 대상으로 함 (게임 봇 패널 제외)
                cog = self.bot.get_cog(info.get("cog_name"))
                if info.get("type") == "panel" and cog is not None:
                    friendly_name = info.get("friendly_name", key)
                    try:
                        channel_db_key = info.get("key")
                        if not channel_db_key:
                            failure_list.append(f"・`{friendly_name}`: 設定情報が不完全です。")
                            continue

                        if not hasattr(cog, 'regenerate_panel'):
                            failure_list.append(f"・`{friendly_name}`: Cogに再生成機能がありません。")
                            continue
                        
                        channel_id = get_id(channel_db_key)
                        if not channel_id:
                            failure_list.append(f"・`{friendly_name}`: チャンネルが設定されていません。")
                            continue
                        
                        target_channel = self.bot.get_channel(channel_id)
                        if not target_channel:
                             failure_list.append(f"・`{friendly_name}`: チャンネル(ID: {channel_id})が見つかりません。")
                             continue
                        
                        success = False
                        if info["cog_name"] == "TicketSystem":
                            panel_type = key.replace("panel_", "")
                            success = await cog.regenerate_panel(target_channel, panel_type)
                        else:
                            success = await cog.regenerate_panel(target_channel)
                        
                        if success:
                            success_list.append(f"・`{friendly_name}` → <#{target_channel.id}>")
                        else:
                            failure_list.append(f"・`{friendly_name}`: 再生成中に不明なエラーが発生しました。")

                    except Exception as e:
                        logger.error(f"'{friendly_name}' 패널 일괄 재설치 중 오류: {e}", exc_info=True)
                        failure_list.append(f"・`{friendly_name}`: スクリプトエラー発生。")

            embed = discord.Embed(title="⚙️ すべての管理パネルの再設置結果", color=0x3498DB, timestamp=discord.utils.utcnow())
            if success_list:
                embed.add_field(name="✅ 成功", value="\n".join(success_list), inline=False)
            if failure_list:
                embed.color = 0xED4245
                embed.add_field(name="❌ 失敗", value="\n".join(failure_list), inline=False)
            
            await interaction.edit_original_response(content="すべての管理パネルの再設置が完了しました。", embed=embed)

        elif action == "roles_sync":
            role_name_map = {key: info["name"] for key, info in UI_ROLE_KEY_MAP.items()}
            await save_config_to_db("ROLE_KEY_MAP", role_name_map)
            
            synced_roles, missing_roles, error_roles = [], [], []
            server_roles_by_name = {r.name: r.id for r in interaction.guild.roles}
            
            for db_key, role_info in UI_ROLE_KEY_MAP.items():
                role_name = role_info.get('name')
                if not role_name: continue
                
                if role_id := server_roles_by_name.get(role_name):
                    try:
                        await save_id_to_db(db_key, role_id)
                        synced_roles.append(f"・`{role_name}`")
                    except Exception as e:
                        error_roles.append(f"・`{role_name}`: `{e}`")
                else:
                    missing_roles.append(f"・`{role_name}`")
            
            embed = discord.Embed(title="⚙️ 役割データベースの完全同期結果", color=0x2ECC71)
            embed.set_footer(text=f"合計 {len(UI_ROLE_KEY_MAP)}個中 | 成功: {len(synced_roles)} / 失敗: {len(missing_roles) + len(error_roles)}")

            if synced_roles:
                full_text = "\n".join(synced_roles)
                for i in range(0, len(full_text), 1024):
                    chunk = full_text[i:i+1024]
                    embed.add_field(name=f"✅ 同期成功 ({len(synced_roles)}個)", value=chunk, inline=False)
            if missing_roles:
                embed.color = 0xFEE75C
                embed.add_field(name=f"⚠️ サーバーに該当の役割なし ({len(missing_roles)}個)", value="\n".join(missing_roles)[:1024], inline=False)
            if error_roles:
                embed.color = 0xED4245
                embed.add_field(name=f"❌ DB保存エラー ({len(error_roles)}個)", value="\n".join(error_roles)[:1024], inline=False)
            
            await interaction.followup.send(embed=embed, ephemeral=True)

        elif action == "stats_set":
            if not channel or not isinstance(channel, discord.VoiceChannel):
                return await interaction.followup.send("❌ このタスクを実行するには、「channel」オプションにボイスチャンネルを指定する必要があります。", ephemeral=True)
            if not stat_type:
                return await interaction.followup.send("❌ このタスクを実行するには、「stat_type」オプションを選択する必要があります。", ephemeral=True)
            
            if stat_type == "remove":
                await remove_stats_channel(channel.id)
                await interaction.followup.send(f"✅ `{channel.name}` チャンネルの統計設定を削除しました。", ephemeral=True)
            else:
                current_template = template or f"정보: {{count}}"
                if "{count}" not in current_template:
                    return await interaction.followup.send("❌ 名前形式(`template`)には必ず`{count}`を含める必要があります。", ephemeral=True)
                if stat_type == "role" and not role:
                    return await interaction.followup.send("❌ '特定の役割の人数'を選択した場合は、「role」オプションを指定する必要があります。", ephemeral=True)
                
                role_id = role.id if role else None
                await add_stats_channel(channel.id, interaction.guild_id, stat_type, current_template, role_id)
                
                stats_cog = self.bot.get_cog("StatsUpdater")
                if stats_cog and hasattr(stats_cog, 'update_stats_loop') and stats_cog.update_stats_loop.is_running():
                    stats_cog.update_stats_loop.restart()
                
                await interaction.followup.send(f"✅ `{channel.name}` チャンネルに統計設定を追加/修正しました。まもなく更新されます。", ephemeral=True)

        elif action == "stats_refresh":
            stats_cog = self.bot.get_cog("StatsUpdater")
            if stats_cog and hasattr(stats_cog, 'update_stats_loop') and stats_cog.update_stats_loop.is_running():
                stats_cog.update_stats_loop.restart()
                await interaction.followup.send("✅ すべての統計チャンネルの更新を要求しました。", ephemeral=True)
            else:
                await interaction.followup.send("❌ 統計更新機能が見つからないか、実行中ではありません。", ephemeral=True)

        elif action == "stats_list":
            configs = await get_all_stats_channels()
            guild_configs = [c for c in configs if c.get('guild_id') == interaction.guild_id]
            if not guild_configs:
                return await interaction.followup.send("ℹ️ 設定された統計チャンネルはありません。", ephemeral=True)
            
            embed = discord.Embed(title="📊 設定された統計チャンネル一覧", color=0x3498DB)
            description = []
            for config in guild_configs:
                ch = self.bot.get_channel(config['channel_id'])
                ch_mention = f"<#{ch.id}>" if ch else f"削除されたチャンネル({config['channel_id']})"
                
                role_info = ""
                if config['stat_type'] == 'role' and config.get('role_id'):
                    role_obj = interaction.guild.get_role(config['role_id'])
                    role_info = f"\n**対象役割:** {role_obj.mention if role_obj else '不明な役割'}"
                
                description.append(f"**チャンネル:** {ch_mention}\n**種類:** `{config['stat_type']}`{role_info}\n**名前形式:** `{config['channel_name_template']}`")
            
            embed.description = "\n\n".join(description)
            await interaction.followup.send(embed=embed, ephemeral=True)
        
        else:
            await interaction.followup.send("❌ 不明なタスクです。リストから正しいタスクを選択してください。", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(ServerSystem(bot))
