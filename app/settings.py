"""路径与常量。数据目录可用 METERCALC_DATA_DIR 覆盖（测试用 tmp 目录）。"""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent

DATA_DIR = Path(os.environ.get("METERCALC_DATA_DIR") or (PROJECT_DIR / "data"))
SAMPLE_DIR = Path(os.environ.get("METERCALC_SAMPLE_DIR") or (PROJECT_DIR / "sample_data"))
READINGS_SAMPLE = SAMPLE_DIR / "readings.csv"
READINGS_AUTO_SAMPLE = SAMPLE_DIR / "readings_auto.csv"
METER_CHANGES_SAMPLE = SAMPLE_DIR / "meter_changes.json"
METERS_SAMPLE = SAMPLE_DIR / "meters.json"
DB_PATH = Path(os.environ.get("METERCALC_DB") or (DATA_DIR / "metercalc.sqlite3"))

APP_NAME = "园区电表累计读数还原"
APP_SHORT = "metercalc"
SEED_VERSION = "sample-2025-04..2026-03-v1"
SEED_SOURCE = "sample_data/readings.csv + meter_changes.json + meters.json"

# 量程（回绕判定用），可由请求覆盖或按表配置覆盖
DEFAULT_FULL_SCALE = 99999.9
# 相邻读数间隔超过该天数即缺口，禁止插值
MAX_GAP_DAYS = 31
# 分时一致性容差
TOU_TOLERANCE = 0.005

# 同表同类型有两路来源（现场抄表 manual / 自动采集 auto）时，视为同一读数点的最大日期偏差
SOURCE_ALIGN_DAYS = 1
# 读数末位精度（kWh）：两路来源对齐后允许的最大读数偏差，超过即视为矛盾
READING_PRECISION = 0.1
# 同一来源前后两个读数点日期相同（同一天两条不同读数）视为来源自相矛盾
SOURCE_CONFLICT_DAYS = 1

