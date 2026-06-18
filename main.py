import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pydantic import BaseModel

load_dotenv()

app = FastAPI(title="合规来函分析系统")
app.mount("/static", StaticFiles(directory="static"), name="static")

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DINGTALK_WEBHOOK = os.environ.get("DINGTALK_WEBHOOK", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")  # PostgreSQL on production
DB_PATH = os.path.join(os.path.dirname(__file__), "cases.db")  # SQLite fallback

USE_PG = bool(DATABASE_URL)

# ---------------------------------------------------------------------------
# Database — PostgreSQL (production) / SQLite (local)
# ---------------------------------------------------------------------------

CREATE_TABLE_PG = """
    CREATE TABLE IF NOT EXISTS cases (
        id               SERIAL PRIMARY KEY,
        created_at       TEXT NOT NULL,
        letter_content   TEXT NOT NULL,
        regulations      TEXT,
        ai_summary       TEXT,
        ai_urgency       TEXT,
        ai_legal_opinion TEXT,
        ai_draft_a       TEXT,
        ai_draft_b       TEXT,
        ai_team_opinions TEXT,
        actual_action    TEXT,
        actual_reply     TEXT,
        correction_notes TEXT,
        status           TEXT DEFAULT 'processing'
    )
"""

CREATE_TABLE_SQLITE = CREATE_TABLE_PG.replace(
    "SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT"
)

def init_db():
    if USE_PG:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_PG)
        conn.close()
    else:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(CREATE_TABLE_SQLITE)
            conn.commit()

@contextmanager
def get_db():
    if USE_PG:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

def row_to_dict(row) -> dict:
    if USE_PG:
        return dict(row)
    return dict(row)

def placeholder() -> str:
    return "%s" if USE_PG else "?"

def db_execute(conn, sql: str, params=()) -> "cursor":
    ph = placeholder()
    # Replace ? with %s for PostgreSQL
    if USE_PG:
        sql = sql.replace("?", "%s")
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur

def db_fetchall(cur) -> list[dict]:
    if USE_PG:
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    return [dict(r) for r in cur.fetchall()]

def db_fetchone(cur) -> Optional[dict]:
    if USE_PG:
        if cur.description is None:
            return None
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
    row = cur.fetchone()
    return dict(row) if row else None

init_db()

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是一个专业的欧盟合规法律助理，服务于一个跨境电商平台合规团队，专门分析涉及欧盟产品合规要求的来函。

## 法规知识库

### 玩具类（Toys）
- EN71-1：玩具机械和物理性能安全要求
- EN71-2：玩具易燃性要求（儿童服装cosplay必须通过）
- EN71-3：玩具某些元素的迁移限量
- EN71-4：实验化学和相关活动用玩具
- EN71-5：化学玩具（非实验类）
- EN71-6：年龄警告图形符号
- EN71-7：手指绘画颜料
- EN71-8：活动玩具（家庭室内外用）
- EN71-9：有机化学化合物
- EN71-12：儿童用品碎屑限量
- EN62115：电动玩具安全
- 玩具安全指令 2009/48/EC（TSD）

### 电子电器类（E&E）
- RoHS指令 2011/65/EU + 修订 2015/863/EU：10种有害物质限制（Pb/Hg/Cd/Cr6+/PBB/PBDE/DEHP/DBP/BBP/DIBP）
- EMC指令 2014/30/EU：电磁兼容性
- LVD低压指令 2014/35/EU：50V-1000V AC设备安全
- RED无线电设备指令 2014/53/EU：含无线功能设备
- WEEE指令 2012/19/EU：废弃电子电气设备回收
- ErP指令 2009/125/EC：能源相关产品生态设计
- EN62368-1：音视频及IT设备安全
- EN62335：（适用法规条款请根据来函确认）

### 化妆品类（Cosmetics）
- EU化妆品法规 (EC) No 1223/2009：成分、标签、安全评估、RP要求
- REACH法规 (EC) No 1907/2006：化学物质注册评估授权
- CLP法规 (EC) No 1272/2008：化学品分类标签包装

### 通用产品安全
- GPSR EU 2023/988（自2024年12月13日生效，取代GPSD 2001/95/EC）：
  - 要求在线平台展示制造商信息（名称、地址、联系方式）
  - 要求展示产品警告和安全信息
  - 经济运营商责任追踪
- CE认证：强制性合格标志要求

## 平台内部三个团队职责

**资质组**：负责商品上线前的资质门槛配置
- 例：儿童cosplay服装 → 必须上传EN71-2燃烧测试通过报告才可上线
- 决策：哪些类目需要强制资质、需要哪些具体报告/证书

**产品组**：负责平台产品功能开发与整改
- 例：GPSR要求前台展示制造商信息 → 在商品详情页和商家后台上传入口配置对应字段
- 决策：需要新增/修改哪些前台展示字段、商家填报入口、平台功能

**审核组**：负责对已上线商品进行合规核查
- 例：核查现有在线商品是否已展示制造商信息、是否有合格报告
- 决策：制定审核标准、排查范围、整改期限

## 输出规则

1. **紧急程度判断**：
   - 高：涉及监管机构处罚通知、截止日期≤30天、产品下架要求
   - 中：需要采取行动但期限30-90天、新法规生效预告
   - 低：信息性来函、咨询类、期限>90天

2. **对外回复原则**：专业礼貌、不承认过失、不作出无把握的承诺

3. **团队意见原则**：具体可操作，说明做什么、为什么、建议时间节点

必须严格输出以下JSON格式，不要添加任何额外文字：

{
  "summary": "来函摘要（≤120字，中文，说明发函方、核心诉求、涉及产品类别）",
  "urgency": "高/中/低",
  "urgency_reason": "判断依据（1-2句）",
  "regulations": ["法规1", "法规2"],
  "external_a": {
    "subject": "邮件主题",
    "en": "英文确认收函Draft",
    "zh": "中文对照译文"
  },
  "external_b": {
    "subject": "邮件主题",
    "en": "英文推荐行动+回函Draft（含具体应对措施）",
    "zh": "中文对照译文"
  },
  "legal_opinion": "法律意见（中文，引用具体法规条款，分析平台义务和风险）",
  "team_opinions": {
    "资质组": "具体行动建议（中文，说明：1.需要配置什么资质要求 2.针对哪些类目 3.建议时间节点）",
    "产品组": "具体行动建议（中文，说明：1.需要开发/修改哪些功能 2.前台/后台各需要什么改动 3.建议时间节点）",
    "审核组": "具体行动建议（中文，说明：1.需要核查哪些在线商品 2.核查标准 3.建议排查时间节点）"
  }
}
"""

def build_fewshot_block(cases: list) -> str:
    if not cases:
        return ""
    lines = ["\n## 历史参考案例（请学习以下真实处理经验）\n"]
    for i, c in enumerate(cases, 1):
        lines.append(f"### 案例{i}（{c['created_at'][:10]}，{c['ai_urgency']}优先级）")
        lines.append(f"**来函摘要：** {c['ai_summary']}")
        if c["regulations"]:
            lines.append(f"**涉及法规：** {c['regulations']}")
        if c["actual_action"]:
            lines.append(f"**实际采取行动：** {c['actual_action']}")
        if c["actual_reply"]:
            lines.append(f"**真实回函：** {c['actual_reply'][:300]}")
        if c["correction_notes"]:
            lines.append(f"**纠错备注：** {c['correction_notes']}")
        lines.append("")
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class LetterRequest(BaseModel):
    content: str

class SaveCaseRequest(BaseModel):
    letter_content: str
    regulations: list[str]
    ai_summary: str
    ai_urgency: str
    ai_legal_opinion: str
    ai_draft_a: dict
    ai_draft_b: dict
    ai_team_opinions: dict
    actual_action: Optional[str] = ""
    actual_reply: Optional[str] = ""
    correction_notes: Optional[str] = ""
    status: Optional[str] = "processing"

class UpdateCaseRequest(BaseModel):
    actual_action: Optional[str] = None
    actual_reply: Optional[str] = None
    correction_notes: Optional[str] = None
    status: Optional[str] = None

# ---------------------------------------------------------------------------
# DingTalk
# ---------------------------------------------------------------------------

async def send_dingtalk(summary: str, urgency: str, regulations: list[str]) -> dict:
    if not DINGTALK_WEBHOOK:
        return {"skipped": True, "reason": "DINGTALK_WEBHOOK未配置"}

    color_map = {"高": "#FF4444", "中": "#FF9900", "低": "#00AA00"}
    emoji_map = {"高": "🔴", "中": "🟡", "低": "🟢"}
    color = color_map.get(urgency, "#888888")
    emoji = emoji_map.get(urgency, "⚪")
    regs = "、".join(regulations[:6]) if regulations else "待确认"
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": f"{emoji} 合规来函 · {urgency}优先级",
            "text": (
                f"## {emoji} 合规来函提醒\n\n"
                f"> **优先级：** <font color='{color}'>{urgency}</font>\n\n"
                f"> **涉及法规：** {regs}\n\n"
                f"**来函摘要：**\n\n{summary}\n\n"
                f"---\n"
                f"*{now} · 请登录合规系统查看完整分析及回函Draft*"
            ),
        },
    }

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(DINGTALK_WEBHOOK, json=payload)
        return resp.json()

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    with open("static/index.html", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.post("/analyze")
async def analyze(request: LetterRequest):
    if not request.content.strip():
        raise HTTPException(status_code=400, detail="来函内容不能为空")
    if not DEEPSEEK_API_KEY:
        raise HTTPException(status_code=500, detail="DEEPSEEK_API_KEY未配置")

    # Fetch recent closed cases as few-shot examples
    with get_db() as conn:
        cur = db_execute(conn, "SELECT * FROM cases WHERE status='closed' ORDER BY created_at DESC LIMIT 5")
        rows = db_fetchall(cur)
    fewshot = build_fewshot_block(rows)
    system = SYSTEM_PROMPT + fewshot

    try:
        client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
        response = client.chat.completions.create(
            model="deepseek-chat",
            max_tokens=4096,
            temperature=0.2,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": f"请分析以下来函，严格按JSON格式输出：\n\n{request.content}"},
            ],
        )
        raw = response.choices[0].message.content
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DeepSeek调用失败：{str(e)[:300]}")

    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise HTTPException(status_code=500, detail=f"AI返回格式异常：{raw[:300]}")

    try:
        result = json.loads(match.group())
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"JSON解析失败：{e}")

    result["dingtalk"] = await send_dingtalk(
        result.get("summary", ""),
        result.get("urgency", "中"),
        result.get("regulations", []),
    )
    result["letter_content"] = request.content
    return result


@app.post("/cases")
async def save_case(req: SaveCaseRequest):
    sql = """INSERT INTO cases
               (created_at, letter_content, regulations, ai_summary, ai_urgency,
                ai_legal_opinion, ai_draft_a, ai_draft_b, ai_team_opinions,
                actual_action, actual_reply, correction_notes, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"""
    params = (
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        req.letter_content,
        json.dumps(req.regulations, ensure_ascii=False),
        req.ai_summary,
        req.ai_urgency,
        req.ai_legal_opinion,
        json.dumps(req.ai_draft_a, ensure_ascii=False),
        json.dumps(req.ai_draft_b, ensure_ascii=False),
        json.dumps(req.ai_team_opinions, ensure_ascii=False),
        req.actual_action,
        req.actual_reply,
        req.correction_notes,
        req.status,
    )
    with get_db() as conn:
        if USE_PG:
            sql_pg = sql.replace("?", "%s").replace(
                "VALUES (%s", "VALUES (%s"
            ) + " RETURNING id"
            cur = db_execute(conn, sql_pg, params)
            new_id = cur.fetchone()[0]
        else:
            cur = db_execute(conn, sql, params)
            new_id = cur.lastrowid
        conn.commit() if not USE_PG else None
    return {"id": new_id, "message": "归档成功"}


@app.patch("/cases/{case_id}")
async def update_case(case_id: int, req: UpdateCaseRequest):
    updates = req.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="没有需要更新的字段")
    ph = "%s" if USE_PG else "?"
    fields = [f"{k} = {ph}" for k in updates]
    values = list(updates.values()) + [case_id]
    sql = f"UPDATE cases SET {', '.join(fields)} WHERE id = {ph}"
    with get_db() as conn:
        db_execute(conn, sql, values)
        if not USE_PG:
            conn.commit()
    return {"message": "更新成功"}


@app.get("/cases")
async def list_cases(limit: int = 20):
    with get_db() as conn:
        cur = db_execute(
            conn,
            "SELECT id, created_at, ai_summary, ai_urgency, regulations, status FROM cases ORDER BY created_at DESC LIMIT ?",
            (limit,)
        )
        return db_fetchall(cur)


@app.get("/cases/{case_id}")
async def get_case(case_id: int):
    with get_db() as conn:
        cur = db_execute(conn, "SELECT * FROM cases WHERE id = ?", (case_id,))
        row = db_fetchone(cur)
    if not row:
        raise HTTPException(status_code=404, detail="案例不存在")
    return row


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "dingtalk_configured": bool(DINGTALK_WEBHOOK),
        "deepseek_configured": bool(DEEPSEEK_API_KEY),
    }
