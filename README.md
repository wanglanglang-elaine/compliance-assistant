# Compliance Assistant

合规来函分析助手是一个面向跨境电商平台合规团队的内部工具，用于分析各国监管关于商品合规相关来函，并辅助生成处理建议、对外回函草稿和内部涉案团队行动建议。

## 主要功能

- 识别来函摘要、紧急程度和涉及法规。
- 生成英文对外回函草稿，并提供中文对照。
- 输出法务意见，以及资质组、产品组、审核组的行动建议。
- 支持归档真实处理结果，后续分析时可参考历史案例。
- 支持钉钉机器人提醒。
- 本地默认使用 SQLite，生产环境可使用 Supabase PostgreSQL。

## 技术栈

- Python
- FastAPI
- Uvicorn
- DeepSeek API
- SQLite / PostgreSQL
- Render 部署

## 环境变量

运行前需要配置以下环境变量：

```env
DEEPSEEK_API_KEY=your_deepseek_api_key
DATABASE_URL=your_supabase_postgres_connection_string
DINGTALK_WEBHOOK=your_dingtalk_robot_webhook
```

说明：

- `DEEPSEEK_API_KEY`：DeepSeek API Key，必填。
- `DATABASE_URL`：生产数据库连接串。为空时会使用本地 `cases.db`。
- `DINGTALK_WEBHOOK`：钉钉机器人 webhook，可选；为空时跳过提醒。

请不要把真实 `.env`、数据库文件或任何 API Key 提交到 GitHub。仓库已通过 `.gitignore` 排除 `.env`、`*.db`、`__pycache__/` 等文件。

## 本地运行

```bash
cd ~/compliance-assistant
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

启动后访问：

```text
http://127.0.0.1:8000
```

健康检查：

```text
http://127.0.0.1:8000/health
```

## API 接口

### 分析来函

```http
POST /analyze
```

请求示例：

```json
{
  "content": "请在这里粘贴监管来函或客户来函内容"
}
```

### 保存案例

```http
POST /cases
```

用于归档 AI 分析结果、实际处理动作、真实回函和纠错备注。

### 查看案例列表

```http
GET /cases
```

### 查看单个案例

```http
GET /cases/{case_id}
```

### 更新案例

```http
PATCH /cases/{case_id}
```

用于补充实际处理动作、真实回函、纠错备注和状态。

## 部署到 Render

仓库已包含 `render.yaml`，Render 会使用以下命令：

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port $PORT
```

仓库同时包含 `runtime.txt`，用于固定 Render 的 Python 版本为 `3.11.9`，避免平台默认使用过新的 Python 版本导致依赖构建失败。

在 Render 环境变量中配置：

- `DEEPSEEK_API_KEY`
- `DATABASE_URL`
- `DINGTALK_WEBHOOK`

其中 `DATABASE_URL` 建议使用 Supabase PostgreSQL 的 connection string。

## 数据库

本地开发时，如果没有配置 `DATABASE_URL`，系统会自动创建并使用：

```text
cases.db
```

生产环境配置 `DATABASE_URL` 后，系统会自动连接 PostgreSQL，并创建 `cases` 表。

## 安全注意事项

- 不要提交 `.env`。
- 不要提交 `cases.db`。
- 不要在 README、代码、Issue、Commit Message 中粘贴真实 API Key。
- 如果 API Key 曾经暴露，应立即去对应平台作废并重新生成。
