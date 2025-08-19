# bot-game/utils/ui_defaults.py (최종 수정본)
"""
[게임 봇 전용]
이 파일은 게임 봇이 사용하는 UI 요소(임베드, 버튼, 문자열)의 기본값을 정의합니다.
참고: 이 봇은 이 데이터를 DB에 동기화(sync)하지 않습니다. 
     DB 동기화는 서버 관리 봇의 책임입니다.
"""

# ==============================================================================
# 1. 역할 키 맵 (Role Key Map) - 게임 봇에서는 사용하지 않음
# ==============================================================================
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
    "panel_fishing_river": {
        "title": "🏞️ 강 낚시터",
        "description": "강가에서 여유롭게 낚시를 즐겨보세요.\n아래 버튼을 눌러 낚시를 시작합니다.",
        "color": 0x5865F2
    },
    "panel_fishing_sea": {
        "title": "🌊 바다 낚시터",
        "description": "넓은 바다에서 월척의 꿈을 펼쳐보세요!\n아래 버튼을 눌러 낚시를 시작합니다.",
        "color": 0x3498DB
    },
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
        "label": "持ち物を見る",
        "style": "primary",
        "emoji": "📦",
        "row": 0
    },
    {
        "component_key": "start_fishing_river",
        "panel_key": "panel_fishing_river",
        "component_type": "button",
        "label": "강에서 낚시하기",
        "style": "primary",
        "emoji": "🏞️",
        "row": 0
    },
    {
        "component_key": "start_fishing_sea",
        "panel_key": "panel_fishing_sea",
        "component_type": "button",
        "label": "바다에서 낚시하기",
        "style": "secondary",
        "emoji": "🌊",
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
    },

    # --- 상점(Commerce) Cog 관련 문자열 ---
    "commerce": {
        "category_view_title": "🏪 Dico森商店 - カテゴリー選択",
        "category_view_desc": "購入したいアイテムのカテゴリーを選択してください。",
        "item_view_title": "🏪 Dico森商店 - 「{category}」",
        "item_view_desc": "現在の所持金: `{balance}`{currency_icon}\n購入したい商品を選択してください。",
        "categories": {
            "アイテム": "アイテム",
            "釣り": "釣り",
            "農場": "農場 (準備中)",
            "ペット": "ペット (準備中)"
        },
        "back_button": "カテゴリー選択に戻る",
        "wip_category": "このカテゴリーの商品は現在準備中です。",
        "purchase_success": "✅ **{item_name}** {quantity}個の購入が完了しました。",
        "upgrade_success": "✅ **{new_item}**を購入し、古い**{old_item}**を`{sell_price}`{currency_icon}で売却しました。",
        "error_insufficient_funds": "❌ 残高が不足しています。",
        "error_already_owned": "❌ すでにそのアイテムを所持しています。",
        "error_upgrade_needed": "❌ より下位の装備を先に購入してください。",
        "error_already_have_better": "❌ すでにその装備またはより良い装備を持っています。"
    },

    # --- 낚시(Fishing) Cog 관련 문자열 ---
    "log_legendary_catch": {
        "title": "👑 伝説の魚が釣り上げられました！ 👑",
        "description": "今週の**ヌシ**が、**{user_mention}**さんの手によって釣り上げられました！\n\n巨大な魚影は、次の週まで姿を消します…。",
        "color": "0xFFD700",
        "field_name": "釣り上げられたヌシ",
        "field_value": "{emoji} **{name}**\n**サイズ**: `{size}`cm\n**価値**: `{value}`{currency_icon}"
    }
}
