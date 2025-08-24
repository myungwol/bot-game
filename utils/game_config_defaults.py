# bot-game/utils/game_config_defaults.py
"""
게임 봇이 사용하는 직업, 레벨, 게임 시스템의 기본값을 정의하는 파일입니다.
"""

# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
# 1. 직업 및 레벨 시스템 설정
# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
JOB_SYSTEM_CONFIG = {
    # 직업 키와 역할 키를 매핑합니다. (서버 관리 봇의 ui_defaults.py와 일치해야 합니다)
    "JOB_ROLE_MAP": {
        "fisherman": "role_job_fisherman",
        "farmer": "role_job_farmer",
        "master_angler": "role_job_master_angler",
        "master_farmer": "role_job_master_farmer",
    },
    # 레벨에 따라 부여될 주민 등급 역할입니다. 높은 레벨부터 순서대로 적어야 합니다.
    "LEVEL_TIER_ROLES": [
        {"level": 150, "role_key": "role_resident_elder"},
        {"level": 100, "role_key": "role_resident_veteran"},
        {"level": 50,  "role_key": "role_resident_regular"},
        {"level": 1,   "role_key": "role_resident_rookie"}
    ]
}

# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
# 2. 전직 시스템 데이터
# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
JOB_ADVANCEMENT_DATA = {
    # --- 레벨 50 전직 정보 ---
    50: [
        {
            "job_key": "fisherman",
            "job_name": "釣り人",
            "role_key": "role_job_fisherman",
            "description": "魚を釣ることに特化した専門家です。",
            "abilities": [
                {
                    "ability_key": "fish_bait_saver_1",
                    "ability_name": "エサ消費なし (確率)",
                    "description": "釣りの際、一定の確率でエサを消費しません。"
                },
                {
                    "ability_key": "fish_bite_time_down_1",
                    "ability_name": "アタリ時間短縮",
                    "description": "魚が食いつくまでの時間が全体的に2秒短縮されます。"
                }
            ]
        },
        {
            "job_key": "farmer",
            "job_name": "農家",
            "role_key": "role_job_farmer",
            "description": "作物を育て、収穫することに特化した専門家です。",
            "abilities": [
                {
                    "ability_key": "farm_seed_saver_1",
                    "ability_name": "種消費なし (確率)",
                    "description": "種を植える際、一定の確率で種を消費しません。"
                },
                {
                    "ability_key": "farm_water_retention_1",
                    "ability_name": "水分保持力UP",
                    "description": "作物が水分を保ちやすくなり、水やりの間隔が長くなります。"
                }
            ]
        }
    ],
    # --- 레벨 100 전직 정보 ---
    100: [
        {
            "job_key": "master_angler",
            "job_name": "太公望",
            "role_key": "role_job_master_angler",
            "description": "釣りの道を極め、伝説の魚を追い求める者。釣り人の上位職です。",
            "prerequisite_job": "fisherman", # [✅✅✅ 핵심 수정] '낚시꾼'이 필요하다고 명시
            "abilities": [
                {
                    "ability_key": "fish_rare_up_2",
                    "ability_name": "レア魚確率UP (大)",
                    "description": "珍しい魚を釣る確率が上昇します。"
                },
                {
                    "ability_key": "fish_size_up_2",
                    "ability_name": "魚のサイズUP (大)",
                    "description": "釣り上げる魚の平均サイズが大きくなります。"
                }
            ]
        },
        {
            "job_key": "master_farmer",
            "job_name": "大農家",
            "role_key": "role_job_master_farmer",
            "description": "農業の神髄を悟り、大地から最大の恵みを得る者。農家の上位職です。",
            "prerequisite_job": "farmer", # [✅✅✅ 핵심 수정] '농가'가 필요하다고 명시
            "abilities": [
                {
                    "ability_key": "farm_yield_up_2",
                    "ability_name": "収穫量UP (大)",
                    "description": "作物を収穫する際の収穫量が大幅に増加します。"
                },
                {
                    "ability_key": "farm_growth_speed_up_2",
                    "ability_name": "成長速度UP (大)",
                    "description": "作物の成長に必要な時間が短縮されます。"
                }
            ]
        }
    ]
}

# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
# 3. 게임 시스템 설정 (GAME_CONFIG)
# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
GAME_CONFIG = {
    "CURRENCY_ICON": "🪙",
    "FISHING_BITE_REACTION_TIME": 3.0,
    "FISHING_BIG_CATCH_THRESHOLD": 70.0,
    "FISHING_SEA_REQ_TIER": 3,
    "FISHING_WAITING_IMAGE_URL": "https://i.imgur.com/AcLgC2g.gif",
    "RPS_LOBBY_TIMEOUT": 60,
    "RPS_CHOICE_TIMEOUT": 45,
    "RPS_MAX_PLAYERS": 5,
    "SLOT_MAX_ACTIVE": 5,
    "XP_FROM_FISHING": 20,
    "XP_FROM_FARMING": 15,
    "XP_FROM_VOICE": 10,
    "XP_FROM_CHAT": 5,
    "VOICE_TIME_REQUIREMENT_MINUTES": 10,
    "VOICE_REWARD_RANGE": [10, 15],
    "CHAT_MESSAGE_REQUIREMENT": 20,
    "CHAT_REWARD_RANGE": [5, 10],
    "JOB_ADVANCEMENT_LEVELS": [50, 100]
}
