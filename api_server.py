"""
api_server.py —— 把 bazi_engine 包装成 HTTP API，供 Dify 自定义工具接入

本地测试运行：
    pip install fastapi uvicorn --break-system-packages
    python3 api_server.py
    然后访问 http://localhost:8000/docs 查看自动生成的接口文档

部署到云端后，Dify 在"自定义工具"里填入：
    https://你的域名/openapi.json
即可自动导入这个工具的所有参数定义。
"""

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from typing import Optional
from bazi_engine import calculate_full_bazi

app = FastAPI(
    title="精准八字排盘 API",
    description="输入公历生辰，返回精确四柱、十神、五行旺衰、大运、流年。基于太阳视黄经天文算法计算节气边界，已交叉验证。",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class UTF8JSONResponse(JSONResponse):
    """显式声明 charset=utf-8，避免部分浏览器/插件误判编码导致中文乱码"""
    media_type = "application/json; charset=utf-8"


@app.get("/bazi", summary="计算八字排盘", operation_id="calculate_bazi", response_class=UTF8JSONResponse)
def get_bazi(
    year: int = Query(..., description="出生年份(公历)，例如 1990", ge=1900, le=2100),
    month: int = Query(..., description="出生月份，1-12", ge=1, le=12),
    day: int = Query(..., description="出生日期，1-31", ge=1, le=31),
    hour: int = Query(..., description="出生小时(24小时制)，0-23", ge=0, le=23),
    minute: int = Query(0, description="出生分钟，0-59", ge=0, le=59),
    gender: str = Query("male", description="性别，male或female，用于大运排盘方向判断"),
    query_year: Optional[int] = Query(None, description="可选，查询指定年份的流年干支；不传则返回当前日期所在流年"),
):
    """
    返回内容包括：
    - 四柱（年月日时）天干地支、五行、十神、地支藏干十神
    - 身强身弱判定和五行分布评分
    - 大运排列（8步，含起运年龄）
    - 流年干支（当前年或指定年份）
    """
    try:
        result = calculate_full_bazi(year, month, day, hour, minute, gender, query_year)
        return {"success": True, "data": result}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/", summary="健康检查", response_class=UTF8JSONResponse)
def root():
    return {"status": "ok", "service": "精准八字排盘 API"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
