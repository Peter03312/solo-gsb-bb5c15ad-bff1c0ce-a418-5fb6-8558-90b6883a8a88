# 盲法编码与揭盲管理系统

用于科研受试样品的盲法编码与揭盲管理。负责人录入分层因素、处理组及每层样品数量，系统按固定随机种子生成不含组别含义的样品码，并维护仅授权可读的「样品码 → 处理组」映射；揭盲需两位不同操作者与完整理由，全程留痕。

技术栈：Python 3.12 + Flask + SQLite（WAL 模式），Gunicorn 部署，Docker / Compose 一键启动。

## 快速启动

### 方式一：Docker Compose（推荐）

```bash
docker compose up --build -d
```

访问地址：**http://localhost:8000**

### 方式二：本地运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python run.py            # 开发模式
# 或：gunicorn --bind 0.0.0.0:8000 run:app
```

访问地址：**http://localhost:8000**

### 默认账号（生产部署务必通过环境变量修改）

| 账号 | 默认密码 | 角色 | 权限 |
|---|---|---|---|
| `manager` | `manager123` | 负责人 | 录入方案、生成编码批次、发起揭盲 |
| `viewer` | `viewer123` | 授权查看 | 查看编码映射与揭盲快照、发起揭盲 |

> 账号在首次启动时写入数据库；之后修改环境变量不会覆盖已存在的账号，需删除数据卷（`docker compose down -v`）后重启生效。

## 配置项（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SECRET_KEY` | `dev-secret-key-change-me` | Flask 会话密钥，生产必须修改 |
| `DATABASE_PATH` | 容器内 `/data/app.db`；本地 `instance/blinding.db` | SQLite 数据库文件路径 |
| `DEFAULT_SEED` | `20261006` | 系统默认固定随机种子（建批时可另行指定） |
| `CODE_PREFIX` | `SMP` | 样品码前缀 |
| `MANAGER_USERNAME` / `MANAGER_PASSWORD` | `manager` / `manager123` | 负责人初始账号 |
| `VIEWER_USERNAME` / `VIEWER_PASSWORD` | `viewer` / `viewer123` | 授权查看初始账号 |

## 使用流程

1. **登录**：负责人以 `manager` 登录。
2. **新建编码批次**：录入批次名称、随机种子（留空用默认固定种子）、处理组（逗号分隔，≥2 个）、各分层因素组合及每层样品数量。
3. **生成样品码**：系统按固定种子确定性生成形如 `SMP-XXXX-XXXX` 的样品码（字符集去除 0/O、1/I/L，编码不含组别含义），每层内做平衡区组随机分配。详情页仅展示盲态样品码与所属分层。
4. **查看映射**：`viewer` 角色可在「映射」页查看样品码 → 分层 → 处理组的完整对应关系。
5. **揭盲**：任一登录角色提交两位不同操作者与完整理由（≥10 字符）。成功后记录揭盲时间、两位操作者与映射快照（JSON），批次状态变为「已揭盲」，不可重复揭盲。快照内容仅 `viewer` 可见。

## 业务规则

- **编码不可复用**：样品码全局唯一并持久化；若本次生成会复用任一已发放样品码（例如沿用了相同种子与相同样品总数），系统**拒绝整批**并逐一列出冲突样品码，不写入任何数据。
- **拒绝缺样 / 不平衡**：任一分层数量缺失、为 0 或不能整除处理组数（无法平衡分配）时，拒绝生成并说明原因。
- **原子性**：批次生成与揭盲均在单事务内完成，任何失败整体回滚。
- **揭盲校验**：操作者缺失、两操作者相同、理由不完整、批次不存在或已揭盲 —— 均只返回原因，不改变编码状态。
- **可复现性**：相同种子 + 相同方案生成相同编码与分配（同一 Python 版本下）。

## 项目结构

```
├── run.py                  # 入口（gunicorn run:app）
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── app/
    ├── __init__.py         # 应用工厂与配置
    ├── db.py               # SQLite 连接、建表、初始账号
    ├── coding.py           # 核心逻辑：校验 / 编码生成 / 冲突检测 / 揭盲
    ├── auth.py             # 登录与角色权限
    ├── routes.py           # 页面路由
    └── templates/          # 页面模板
```

## 健康检查

`GET /healthz` 返回 `{"status": "ok"}`，Compose 已配置健康检查。
