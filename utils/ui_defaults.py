# bot-game/utils/ui_defaults.py
"""
[게임 봇 전용]
이 파일은 게임 봇이 사용하는 UI 요소(임베드, 버튼)의 기본값을 정의합니다.
참고: 이 봇은 이 데이터를 DB에 동기화(sync)하지 않습니다. 
     DB 동기화는 서버 관리 봇의 책임입니다.
"""

# ==============================================================================
# 1. 역할 키 맵 (Role Key Map) - 게임 봇에서는 사용하지 않음
# ==============================================================================
# 역할 관리는 서버 관리 봇의 책임이므로, 이 맵은 게임 봇에 필요 없습니다.
# 역할 정보가 필요할 경우 DB에서 읽어옵니다.
UI_ROLE_KEY_MAP = {}

# ==============================================================================
# 2. 임베드(Embed) 기본값 - 게임/경제 관련만 남김
# ==============================================================================
UI_EMBEDS = {
    # --- 경제/상점 관련 ---
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
    "embed_transfer_confirmation": {
        "title": "💸 送金確認",
        "description": "本当に {recipient_mention}さんへ `{amount}`{currency_icon} を送金しますか？",
        "color": 0xE67E22
    },
    "log_coin_gain": {
        "description": "{user_mention}さんが**{reason}**で`{amount}`{currency_icon}を獲得しました。",
        "color": 0x2ECC71
    },
    "log_coin_transfer": {
        "description": "💸 {sender_mention}さんが{recipient_mention}さんへ`{amount}`{currency_icon}を送金しました。",
        "color": 0x3498DB
    },
    "log_coin_admin": {
        "description": "⚙️ {admin_mention}さんが{target_mention}さんのコインを`{amount}`{currency_icon}だけ**{action}**しました。",
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
# 3. 패널 컴포넌트(Panel Components) 기본값 - 게임/경제 관련만 남김
# ==============================================================================
UI_PANEL_COMPONENTS = [
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
        "component_key": "open_inventory",
        "panel_key": "profile",
        "component_type": "button",
        "label": "持ち物を開く",
        "style": "primary",
        "emoji": "📦",
        "row": 0
    },
]

# ==============================================================================
# 4. UI 텍스트 문자열 (UI Strings)
# ==============================================================================
UI_STRINGS = {
    # --- 프로필(UserProfile) Cog 관련 문자열 ---
    "profile_view": {
        "base_title": "{user_name}さんのプロフィール",
        "tabs": {
            "info": {"title_suffix": " - 情報", "label": "情報", "emoji": "ℹ️"},
            "item": {"title_suffix": " - アイテム", "label": "アイテム", "emoji": "📦"},
            "gear": {"title_suffix": " - 装備", "label": "装備", "emoji": "⚙️"},
            "fish": {"title_suffix": " - 魚", "label": "魚", "emoji": "🐠"},
            "seed": {"title_suffix": " - シード", "label": "シード", "emoji": "🌱"},
            "crop": {"title_suffix": " - 作物", "label": "作物", "emoji": "🌾"},
            "feed": {"title_suffix": " - 餌", "label": "餌", "emoji": "🍖"}
        },
        "info_tab": {
            "field_balance": "💰 所持金",
            "field_rank": "🏆 等級",
            "default_rank_name": "等級なし",
            "description": "現在の所持金と等級を確認できます。"
        },
        "item_tab": {
            "no_items": "所持しているアイテムがありません。"
        },
        "gear_tab": {
            "current_gear_field": "[ 現在の装備 ]",
            "owned_gear_field": "[ 所持している装備 ]",
            "no_owned_gear": "所持している装備がありません。",
            "change_rod_button": "釣竿を変更",
            "change_bait_button": "エサを変更"
        },
        "fish_tab": {
            "no_fish": "水槽に魚がいません。",
            "pagination_footer": "ページ {current_page} / {total_pages}"
        },
        "wip_tab": {
            "description": "この機能は現在準備中です。"
        },
        "pagination_buttons": {
            "prev": "◀",
            "next": "▶"
        }
    },
    # --- 장비 변경(GearSelect) View 관련 문자열 ---
    "gear_select_view": {
        "embed_title": "装備変更: {category_name}",
        "embed_description": "インベントリから装着するアイテムを選択してください。",
        "placeholder": "新しい{category_name}を選択してください...",
        "unequip_rod_label": "釣竿を外す",
        "unequip_bait_label": "エサを外す",
        "unequip_prefix": "✋",
        "back_button": "戻る"
    }
}
