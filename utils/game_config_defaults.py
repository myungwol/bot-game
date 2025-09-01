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
            "job_name": "낚시꾼",
            "role_key": "role_job_fisherman",
            "description": "물고기를 낚는 데 특화된 전문가입니다.",
            "abilities": [
                {
                    "ability_key": "fish_bait_saver_1",
                    "ability_name": "미끼 절약 (확률)",
                    "description": "낚시할 때 일정 확률로 미끼를 소모하지 않습니다."
                },
                {
                    "ability_key": "fish_bite_time_down_1",
                    "ability_name": "입질 시간 단축",
                    "description": "물고기가 미끼를 무는 데 걸리는 시간이 전체적으로 2초 단축됩니다."
                }
            ]
        },
        {
            "job_key": "farmer",
            "job_name": "농부",
            "role_key": "role_job_farmer",
            "description": "작물을 키우고 수확하는 데 특화된 전문가입니다.",
            "abilities": [
                {
                    "ability_key": "farm_seed_saver_1",
                    "ability_name": "씨앗 절약 (확률)",
                    "description": "씨앗을 심을 때 일정 확률로 씨앗을 소모하지 않습니다."
                },
                {
                    "ability_key": "farm_water_retention_1",
                    "ability_name": "수분 유지력 UP",
                    "description": "작물이 수분을 더 오래 머금어 물을 주는 간격이 길어집니다."
                }
            ]
        }
    ],
    # --- 레벨 100 전직 정보 ---
    100: [
        {
            "job_key": "master_angler",
            "job_name": "강태공",
            "role_key": "role_job_master_angler",
            "description": "낚시의 길을 통달하여 전설의 물고기를 쫓는 자. 낚시꾼의 상위 직업입니다.",
            "prerequisite_job": "fisherman", # '낚시꾼'이 필요하다고 명시
            "abilities": [
                {
                    "ability_key": "fish_rare_up_2",
                    "ability_name": "희귀어 확률 UP (대)",
                    "description": "희귀한 물고기를 낚을 확률이 상승합니다."
                },
                {
                    "ability_key": "fish_size_up_2",
                    "ability_name": "물고기 크기 UP (대)",
                    "description": "낚는 물고기의 평균 크기가 커집니다."
                }
            ]
        },
        {
            "job_key": "master_farmer",
            "job_name": "대농",
            "role_key": "role_job_master_farmer",
            "description": "농업의 정수를 깨달아 대지로부터 최대의 은혜를 얻는 자. 농부의 상위 직업입니다.",
            "prerequisite_job": "farmer", # '농부'가 필요하다고 명시
            "abilities": [
                {
                    "ability_key": "farm_yield_up_2",
                    "ability_name": "수확량 UP (대)",
                    "description": "작물을 수확할 때의 수확량이 대폭 증가합니다."
                },
                {
                    "ability_key": "farm_growth_speed_up_2",
                    "ability_name": "성장 속도 UP (대)",
                    "description": "작물의 성장 시간이 단축됩니다."
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
