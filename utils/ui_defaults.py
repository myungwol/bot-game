# utils/ui_defaults.py
"""
봇이 사용하는 모든 UI 요소 및 핵심 매핑 데이터의 기본값을 정의하는 파일입니다.
봇이 시작될 때 이 파일의 데이터가 Supabase 데이터베이스에 동기화됩니다.
"""

# ==============================================================================
# 1. 역할 키 맵 (Role Key Map)
# ==============================================================================
UI_ROLE_KEY_MAP = {
    # --- 스태프 역할 (닉네임 접두사 O) ---
    "role_admin_total": {
        "name": "森の妖精",
        "is_prefix": True,
        "priority": 100
    },
    "role_staff_village_chief": {
        "name": "村長",
        "is_prefix": True,
        "priority": 90
    },
    "role_staff_deputy_chief": {
        "name": "副村長",
        "is_prefix": True,
        "priority": 85
    },
    "role_staff_police": {
        "name": "交番さん",
        "is_prefix": True,
        "priority": 80
    },
    "role_staff_festival": {
        "name": "お祭り係",
        "is_prefix": True,
        "priority": 70
    },
    "role_staff_pr": {
        "name": "ビラ配りさん",
        "is_prefix": True,
        "priority": 70
    },
    "role_staff_design": {
        "name": "村の絵描きさん",
        "is_prefix": True,
        "priority": 70
    },
    "role_staff_secretary": {
        "name": "書記",
        "is_prefix": True,
        "priority": 70
    },
    "role_staff_newbie_helper": {
        "name": "お世話係",
        "is_prefix": True,
        "priority": 70
    },
    "role_approval": {
        "name": "役場の職員",
        "is_prefix": True,
        "priority": 60
    },
    # --- 주민 등급 역할 (닉네임 접두사 O) ---
    "role_resident_elder": {
        "name": "長老",
        "is_prefix": True,
        "priority": 50
    },
    "role_resident_veteran": {
        "name": "ベテラン住民",
        "is_prefix": True,
        "priority": 40
    },
    "role_resident_regular": {
        "name": "おなじみ住民",
        "is_prefix": True,
        "priority": 30
    },
    "role_resident_rookie": {
        "name": "かけだし住民",
        "is_prefix": True,
        "priority": 20
    },
    "role_resident": {
        "name": "住民",
        "is_prefix": True,
        "priority": 10
    },
    "role_guest": {
        "name": "旅の人",
        "is_prefix": True,
        "priority": 5
    },
    # --- 온보딩 진행 역할 (구분선, 닉네임 접두사 X) ---
    "role_onboarding_step_1": {
        "name": "━━━━━━ ゲーム ━━━━━━",
        "is_prefix": False,
        "priority": 0
    },
    "role_onboarding_step_2": {
        "name": "━━━━━━ 通知 ━━━━━━",
        "is_prefix": False,
        "priority": 0
    },
    "role_onboarding_step_3": {
        "name": "━━━━━━ 情報 ━━━━━━",
        "is_prefix": False,
        "priority": 0
    },
    "role_onboarding_step_4": {
        "name": "━━━━━━ 住民 ━━━━━━",
        "is_prefix": False,
        "priority": 0
    },
    # --- 정보 역할 (닉네임 접두사 X) ---
    "role_info_male": {
        "name": "男性",
        "is_prefix": False,
        "priority": 0
    },
    "role_info_female": {
        "name": "女性",
        "is_prefix": False,
        "priority": 0
    },
    "role_info_age_70s": {
        "name": "70年代生",
        "is_prefix": False,
        "priority": 0
    },
    "role_info_age_80s": {
        "name": "80年代生",
        "is_prefix": False,
        "priority": 0
    },
    "role_info_age_90s": {
        "name": "90年代生",
        "is_prefix": False,
        "priority": 0
    },
    "role_info_age_00s": {
        "name": "00年代生",
        "is_prefix": False,
        "priority": 0
    },
    "role_info_age_private": {
        "name": "非公開",
        "is_prefix": False,
        "priority": 0
    },
    # --- 알림 역할 (닉네임 접두사 X) ---
    "role_notify_voice": {
        "name": "通話",
        "is_prefix": False,
        "priority": 0
    },
    "role_notify_friends": {
        "name": "友達",
        "is_prefix": False,
        "priority": 0
    },
    "role_notify_disboard": {
        "name": "Disboard",
        "is_prefix": False,
        "priority": 0
    },
    "role_notify_up": {
        "name": "Up",
        "is_prefix": False,
        "priority": 0
    },
    # --- 게임 역할 (닉네임 접두사 X) ---
    "role_game_minecraft": {
        "name": "マインクラフト",
        "is_prefix": False,
        "priority": 0
    },
    "role_game_valorant": {
        "name": "ヴァロラント",
        "is_prefix": False,
        "priority": 0
    },
    "role_game_overwatch": {
        "name": "オーバーウォッチ",
        "is_prefix": False,
        "priority": 0
    },
    "role_game_lol": {
        "name": "リーグ・オブ・レジェンド",
        "is_prefix": False,
        "priority": 0
    },
    "role_game_mahjong": {
        "name": "麻雀",
        "is_prefix": False,
        "priority": 0
    },
    "role_game_amongus": {
        "name": "アモングアス",
        "is_prefix": False,
        "priority": 0
    },
    "role_game_mh": {
        "name": "モンスターハンター",
        "is_prefix": False,
        "priority": 0
    },
    "role_game_genshin": {
        "name": "原神",
        "is_prefix": False,
        "priority": 0
    },
    "role_game_apex": {
        "name": "エーペックスレジェンズ",
        "is_prefix": False,
        "priority": 0
    },
    "role_game_splatoon": {
        "name": "スプラトゥーン",
        "is_prefix": False,
        "priority": 0
    },
    "role_game_gf": {
        "name": "ゴッドフィールド",
        "is_prefix": False,
        "priority": 0
    },
    "role_platform_steam": {
        "name": "スチーム",
        "is_prefix": False,
        "priority": 0
    },
    "role_platform_smartphone": {
        "name": "スマートフォン",
        "is_prefix": False,
        "priority": 0
    },
    "role_platform_switch": {
        "name": "スイッチ",
        "is_prefix": False,
        "priority": 0
    },
}

# ==============================================================================
# 2. 임베드(Embed) 기본값
# ==============================================================================
UI_EMBEDS = {
    "welcome_embed": {
        "title":
        "🎉 {guild_name}へようこそ！",
        "description": ("{member_mention}さん、はじめまして！\n\n"
                        "まずは、サーバーの案内を読んで、自己紹介の作成をお願いします。"),
        "color":
        0x3498DB
    },
    "farewell_embed": {
        "title": "👋 また会いましょう",
        "description": "{member_name}さんが村から旅立ちました。",
        "color": 0x99AAB5
    },
    "panel_roles": {
        "title": "📖 役割付与",
        "description": "下のメニューからカテゴリーを選択して、自分に必要な役割を受け取ってください。",
        "color": 0x5865F2
    },
    "embed_onboarding_info_roles": {
        "title":
        "📖 役割付与 (情報)",
        "description": ("次に、ご自身の情報を表す役割を選択してください。\n\n"
                        "この情報は、他の住民があなたをよりよく知るのに役立ちます。（非公開も可能です）"),
        "color":
        0x5865F2
    },
    "embed_onboarding_final_rules": {
        "title":
        "📝 最終確認",
        "description": ("ありがとうございます！\n\n"
                        "最後に、村のルールをもう一度確認してください。\n\n"
                        "- 他の住民を尊重し、迷惑をかけないこと。\n"
                        "- 問題が発生した場合は、すぐに村役場（管理者）に報告すること。\n\n"
                        "下のボタンを押すと、住民登録票の作成に進みます。"),
        "color":
        0x3498DB
    },
    "panel_onboarding": {
        "title": "📝 村役場・案内所",
        "description": "初めての方は、まず「案内を読む」ボタンを押して、サーバーでの過ごし方を確認してください。",
        "color": 0x5865F2
    },
    "panel_nicknames": {
        "title": "✒️ 名前変更",
        "description": "村で使う名前を変更したい場合は、下のボタンから申請してください。",
        "color": 0x5865F2
    },
    "panel_commerce": {
        "title": "🏪 Dico森商店＆買取ボックス",
        "description": "アイテムを買ったり、釣った魚などを売ったりできます。",
        "color": 0x5865F2
    },
    "panel_fishing": {
        "title": "🎣 釣り場",
        "description": ("のんびり釣りを楽しみましょう。\n"
                        "「釣りをする」ボタンで釣りを開始します。"),
        "color": 0x5865F2
    },
    "panel_profile": {
        "title": "📦 持ち物",
        "description": "自分の所持金やアイテム、装備などを確認できます。",
        "color": 0x5865F2
    },
    "embed_onboarding_approval": {
        "title": "📝 新しい住民登録票",
        "description": "{member_mention}さんが住民登録票を提出しました。",
        "color": 0xE67E22
    },
    "embed_main_chat_welcome": {
        "description": "🎉 {member_mention}さんが新しい住民になりました！これからよろしくお願いします！",
        "color": 0x2ECC71
    },
    "embed_introduction_log": {
        "title": "📝 自己紹介",
        "description": "新しい住民がやってきました！みんなで歓迎しましょう！",
        "color": 0x2ECC71
    },
    "embed_transfer_confirmation": {
        "title": "💸 送金確認",
        "description":
        "本当に {recipient_mention}さんへ `{amount}`{currency_icon} を送金しますか？",
        "color": 0xE67E22
    },
    "log_coin_gain": {
        "description":
        "{user_mention}さんが**{reason}**で`{amount}`{currency_icon}を獲得しました。",
        "color": 0x2ECC71
    },
    "log_coin_transfer": {
        "description":
        "💸 {sender_mention}さんが{recipient_mention}さんへ`{amount}`{currency_icon}を送金しました。",
        "color": 0x3498DB
    },
    "log_coin_admin": {
        "description":
        "⚙️ {admin_mention}さんが{target_mention}さんのコインを`{amount}`{currency_icon}だけ**{action}**しました。",
        "color": 0x3498DB
    },
    "embed_shop_buy": {
        "title": "🏪 Dico森商店 - 「{category}」",
        "description": "現在の所持金: `{balance}`{currency_icon}",
        "color": 0x3498DB
    },
    "embed_shop_sell": {
        "title": "📦 販売所 - 「{category}」",
        "description": "現在の所持金: `{balance}`{currency_icon}",
        "color": 0xE67E22
    }
}

# ==============================================================================
# 3. 패널 버튼(Panel Components) 기본값
# ==============================================================================
UI_PANEL_COMPONENTS = [
    {
        "component_key": "start_onboarding_guide",
        "panel_key": "onboarding",
        "component_type": "button",
        "label": "案内を読む",
        "style": "success",
        "emoji": "📖",
        "row": 0
    },
    {
        "component_key": "open_shop",
        "panel_key": "commerce",
        "component_type": "button",
        "label": "商店 (アイテム購入)",
        "style": "primary",
        "emoji": "🏪",
        "row": 0
    },
    {
        "component_key": "open_market",
        "panel_key": "commerce",
        "component_type": "button",
        "label": "買取ボックス (アイテム売却)",
        "style": "secondary",
        "emoji": "📦",
        "row": 0
    },
    {
        "component_key": "start_fishing",
        "panel_key": "fishing",
        "component_type": "button",
        "label": "釣りをする",
        "style": "primary",
        "emoji": "🎣",
        "row": 0
    },
    {
        "component_key": "request_nickname_change",
        "panel_key": "nicknames",
        "component_type": "button",
        "label": "名前変更申請",
        "style": "primary",
        "emoji": "✒️",
        "row": 0
    },
    {
        "component_key": "open_inventory",
        "panel_key": "profile",
        "component_type": "button",
        "label": "持ち物を開く",
        "style": "primary",
        "emoji": "📦",
        "row": 0
    },
]
