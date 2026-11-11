# 嘉环声枢录音卡设备管理服务（后端）

> 依据根目录 [`README.md`](../README.md)《嘉环声枢：录音卡设备管理系统开发说明》V1.0 实现。
> 技术栈：**Python 3.11+ / FastAPI / SQLAlchemy 2.0 / MySQL 8.0.16+ / Casdoor**。

---

## 1. 已实现范围

README §8「第一阶段开发范围」中的 **P0 全部功能**均已实现，并额外覆盖了 P1 中的
设备挂失 / 禁用 / 维修 / 报废 / AuthKey 轮换。

| README 需求 | 实现位置 | 状态 |
|---|---|---|
| MySQL 数据库初始化 | `app/db/models.py`、`sql/schema.mysql.sql`、`scripts/init_db.py` | ✅ |
| 设备档案管理 | `app/services/device_service.py` | ✅ |
| SN 与 AuthKey 管理 | `app/core/security.py`、`app/services/credential_service.py` | ✅ |
| 设备批量入库 | `POST /api/devices/batch` | ✅ |
| 员工设备领用 | `POST /api/devices/{id}/assign` | ✅ |
| APP 设备绑定 | `POST /api/devices/{id}/bind` | ✅ |
| 员工自定义 Nickname | `PUT /api/devices/{id}/nickname` | ✅ |
| 设备解绑和归还 | `POST /api/devices/{id}/unbind`、`POST /api/devices/{id}/return` | ✅ |
| Casdoor 身份认证 | `app/integrations/casdoor.py` | ✅ |
| 操作历史记录 | `GET /api/devices/{id}/history`（device_event 全量事件） | ✅ |
| 设备挂失和禁用 | `POST /api/devices/{id}/lost`、`/disable` | ✅ |
| 设备维修管理 | `POST /api/devices/{id}/repair` | ✅ |
| AuthKey 轮换 | `POST /api/devices/{id}/credential/rotate` | ✅ |
| 设备电量及状态监控 | `device_status` 表已建，**未开放上报接口**（固件不连后端，见 §6） | ◐ |
| 企业服务授权 / ASR 额度 / AI 摘要额度 / OTA / 资产统计 | P2，暂未实现 | ⏳ |

另有配套的**管理后台前端**（`../frontend/`，无构建步骤），按上表逐表提供管理视图：

| README 需求 | 实现位置 | 状态 |
|---|---|---|
| 设备档案管理页面 | `frontend/assets/js/pages/devices.js` | ✅ |
| 领用记录 / APP 绑定页面 | `frontend/assets/js/pages/assignments.js`、`bindings.js` | ✅ |
| 设备凭证 / 设备状态 / 事件日志页面 | `frontend/assets/js/pages/credentials.js`、`statuses.js`、`events.js` | ✅ |
| 表格级跨设备查询接口 | `app/api/routes/tables.py`、`app/services/table_service.py` | ✅ |

---

## 2. 目录结构

```
backend/
├── app/
│   ├── main.py                 # 应用入口、生命周期、异常处理、CORS、静态资源挂载
│   ├── core/
│   │   ├── config.py           # 配置中心（环境变量 / .env）
│   │   ├── enums.py            # 生命周期状态、事件类型、状态机规则
│   │   ├── errors.py           # 统一异常体系（错误码 ↔ HTTP 状态码）
│   │   └── security.py         # AuthKey 生成与格式校验
│   ├── db/
│   │   ├── models.py           # 6 张表的 ORM 定义（严格对齐 README §4）
│   │   └── session.py          # 引擎 / 会话 / 事务依赖
│   ├── schemas/                # Pydantic 请求响应契约
│   ├── integrations/
│   │   └── casdoor.py          # Casdoor OIDC 客户端与身份模型
│   ├── api/
│   │   ├── deps.py             # 认证依赖、租户范围、管理员判定
│   │   └── routes/             # 路由：devices / bindings / credentials / me / auth / tables
│   └── services/               # 业务服务层（事务、状态机、事件、表格查询）
├── sql/schema.mysql.sql        # MySQL 生产建表脚本
├── scripts/init_db.py          # 数据库初始化脚本
├── scripts/seed_demo.py        # 演示数据生成脚本
├── scripts/whoami.py           # 令牌身份自查脚本
├── tests/                      # 75 个自动化测试
├── requirements.txt
└── .env.example
```

### 2.2 生产部署（nginx 同源代理）

```
浏览器 ──> https://office.uassist.cn:40000
             ├── /voicehub/      → 管理后台静态资源（frontend/ 目录）
             └── /voicehub-api/  → 后端服务 http://127.0.0.1:8040
```

后端 `.env`：

```bash
API_PREFIX=/voicehub-api        # 与 nginx 透传的路径一致
SERVE_FRONTEND=false             # 静态资源由 nginx 直接托管
CORS_ORIGINS=https://office.uassist.cn:40000
```

nginx 配置样例见 [`../frontend/nginx.conf.example`](../frontend/nginx.conf.example)。

> ⚠️ **Windows / Git Bash 陷阱**：直接执行
> `API_PREFIX=/voicehub-api uv run -m uvicorn ...` 时，MSYS 会把以 `/` 开头的值
> 自动转换成 Windows 绝对路径（`C:/Program Files/Git/voicehub-api`），
> 导致所有接口挂在错误路径上、服务能起但接口全 404。
> **请写进 `backend/.env`**，或先 `export MSYS_NO_PATHCONV=1`。
> 本服务的 `api_prefix` 校验器会拦截这种值并在启动时给出明确提示。

---

## 3. 快速开始

```bash
cd backend

# 1. 安装依赖
uv add -r requirements.txt

# 2. 配置环境变量
cp .env.example .env
#    至少填写 CASDOOR_CLIENT_ID / CASDOOR_CLIENT_SECRET

# 3. 初始化数据库（开发环境用 SQLite 会自动建表，此步可跳过）
uv run scripts/init_db.py

# 4. 启动
uv run -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8040
```

启动后：

- 接口文档：<http://127.0.0.1:8040/docs>
- OpenAPI：<http://127.0.0.1:8040/openapi.json>
- 健康检查：`/healthz`（存活）、`/readyz`（数据库连通性）

### 切换到 MySQL

```bash
# .env
DATABASE_URL=mysql+pymysql://user:password@127.0.0.1:3306/voicehub_device?charset=utf8mb4
APP_ENV=production
```

建表（两种方式任选）：

```bash
mysql -u root -p < sql/schema.mysql.sql     # 方式一：直接执行 SQL
uv run scripts/init_db.py                     # 方式二：由 ORM 建表
```

> MySQL 需 **8.0.16 及以上**，否则 `CHECK` 约束不会被执行。

### 升级旧版设备凭证表

已有数据库在升级时先停止旧版服务，再使用目标数据库的配置执行 `uv run scripts/init_db.py`，随后启动新版。
脚本保留设备、凭证版本和状态，移除旧的摘要列，并为旧凭证留空 `auth_key`。
开发环境启动时会自动执行同一升级。

旧 HMAC 摘要无法还原原始 AuthKey。若已掌握录音卡当前使用的原始密钥，
可按凭证记录 ID 将其补录到 `device_credential.auth_key` 中对应的有效凭证。
也可通过轮换接口签发新密钥，再将同一个新密钥同步到录音卡。
补录或轮换前，旧凭证在领用列表中返回 `auth_key: null`。
> ORM 与 SQL 脚本结构等价：主键 `BIGINT UNSIGNED`、时间戳 `DATETIME(3)`、
> `updated_at ... ON UPDATE CURRENT_TIMESTAMP(3)`。

---

## 4. Casdoor 集成说明

### 4.1 实例信息

当前实例：`https://office.uassist.cn:40000/auth`

该实例配置了 **`authPrefix=auth`**，因此所有接口都在 `/auth` 之下。
OIDC 发现文档 `https://office.uassist.cn:40000/auth/.well-known/openid-configuration` 返回：

| 用途 | 端点 |
|---|---|
| 授权码登录 | `/auth/login/oauth/authorize` |
| 换取 Token | `/auth/api/login/oauth/access_token` |
| 用户信息 | `/auth/api/userinfo` |
| Token 内省（RFC 7662） | `/auth/api/login/oauth/introspect` |
| JWKS 公钥 | `/auth/.well-known/jwks` |
| 查询用户 | `/auth/api/get-user?owner=&id=` |

> 服务端签发的 `iss` 为 `http://office.uassist.cn:40000/auth`（明文 http）。
> 本服务访问使用 `https`（DigiCert 有效证书），并在 JWT 校验时对 http/https 两种
> issuer 变体一并接受，避免因协议差异导致的校验失败。

### 4.2 Token 校验模式（`CASDOOR_VERIFY_METHOD`）

| 取值 | 行为 | 适用场景 |
|---|---|---|
| `auto`（默认） | 先调 userinfo 取完整用户对象；失败且配置了 client 凭据时回退 introspect | 通用 |
| `introspect` | 服务端校验 Token 有效性，可识别已撤销 Token | 安全要求高 |
| `userinfo` | 仅调用 userinfo 端点 | 无 client 凭据时 |
| `jwt` | 拉取 JWKS 本地验签，不依赖每次网络往返 | 高并发 |

### 4.3 需要向 Casdoor 管理员申请的配置

在 Casdoor 后台「应用」中获取 `clientId` / `clientSecret`，填入 `.env`：

```bash
# Casdoor 后台应用：组织 owner=bestlink，应用 name=voicehub（组合标识 bestlink/voicehub）
CASDOOR_CLIENT_ID=6932b8ce0b655b9421b5
CASDOOR_CLIENT_SECRET=****
CASDOOR_ORGANIZATION=bestlink
CASDOOR_APPLICATION=voicehub
```

> `clientId` 是**公开标识符**（会出现在前端 OAuth 跳转 URL 里），已内置在
> `app/core/config.py` 的默认值中，环境变量可覆盖。
> `clientSecret` 是真正的机密，只放 `.env`，不写进代码。
> 组织名即 `tenant_id`：**Casdoor 组织改名后，存量设备/领用数据的 `tenant_id`
> 必须同步迁移**，否则新租户登录后看不到旧数据（越权返回 404）。
> 应用改名后请在 Casdoor 后台复核 `clientId` 是否仍为上面的值，并确认
> 「许可跳转列表」未因应用变更而丢失。

**Casdoor 应用还须登记「许可跳转列表」**，管理后台入口对应的回调地址：

| 环境 | 回调地址 |
|---|---|
| 生产（nginx） | `https://office.uassist.cn:40000/voicehub/` |
| 本地（后端自带托管） | `http://127.0.0.1:8040/admin/` |

未登记时跳转登录页会提示「重定向 URI 在许可跳转列表中未找到」。

未配置 clientSecret 时服务仍可运行（Token 走 userinfo 校验），此时：
- 管理后台的 Casdoor 登录（PKCE，公开客户端）**照常可用**；
- 设备领用直接填**工号**（`preferred_username`）即可，工号即业务唯一标识，无需反查；
- **分配时的工号存在性校验照常生效**：本实例 `GET /auth/api/get-user?id=<owner>/<工号>`
  是公开只读接口，无需令牌（详见 §4.4 末段）。若 Casdoor 收紧该接口权限，
  可配 `clientSecret` 走服务令牌，或由后端透传管理员 access token；
- introspect 校验模式仍需 clientSecret；「按用户 UUID 反查工号」在本实例
  Casdoor 版本上不受支持，因此领用表单直接拒绝 UUID 输入。

### 4.4 租户与身份映射

| Casdoor 字段 | 本服务概念 | 说明 |
|---|---|---|
| `owner`（组织名） | `tenant_id` | 一个 Casdoor 组织 = 一个企业；多租户隔离的基准 |
| `preferred_username`（工号） | `user_id`（业务表） | **员工唯一标识**：写入 `device_assignment` / `device_binding` 的 `user_id` 列；`/my/*` 与 APP 绑定均按它匹配 |
| `sub` / `id`（用户 UUID） | 技术标识 | Token 里的 `sub`，只用于审计与兼容按 UUID 写入的历史记录，**不作为业务归属值** |
| `name` / `displayName` | `display_name` | 姓名，仅用于界面展示；可能重名，**禁止参与身份匹配** |
| `roles[].name` | 管理员判定 | 角色名（Casdoor 的 `roles` 是对象数组，不是字符串数组） |
| `groups[]` | 管理员判定 | 用户组名（字符串数组，通常是 `owner/group` 全限定名，如 `bestlink/admins`） |
| `permissions[].name` | 管理员判定 | 权限名（Casdoor 的 `permissions` 是对象数组） |
| `isAdmin` | 管理员判定 | Casdoor 内置管理员标记 |
| `isForbidden` | 登录拦截 | 被禁用的账号直接拒绝 |

> **字段陷阱**：`/api/userinfo` 返回 OIDC claims，`/api/get-user` 返回 Casdoor 用户对象，
> 两者对 `name` 的定义恰好相反——前者 `name` 是姓名、`preferred_username` 是工号；
> 后者 `name` 是工号、`displayName` 是姓名。生产实调样例：
>
> ```json
> {"sub":"7994b554-b159-4a6b-bdba-0703193e6ed8",
>  "preferred_username":"JH000059","name":"胥晓冬","email":"xxd@bestlink.com.cn"}
> ```
>
> `GET /my/profile` 会同时返回 `user_id`（UUID）、`username` / `preferred_username` /
> `employee_no`（三处同值，均为工号）、`display_name`（姓名），联调先核对这几项。

**租户隔离**：管理员接口的可见范围由服务端从 Token 推导，**不采信客户端提交的
`tenant_id`**；越权访问一律返回 404（不泄露其他企业设备是否存在）。
仅 `CASDOOR_GLOBAL_ADMIN_ROLES`（默认 `global_admin`）可跨企业操作。

**分配时的工号存在性校验**（`POST /api/devices/{id}/assign`）：
管理员填工号后，服务端先向 Casdoor 查 `id=<tenant_id>/<工号>`，确认这个人在本组织
里真实存在、账号未被禁用，才写领用记录；否则拒绝，避免产生「设备分给了不存在的工号，
员工永远登不进来」的孤儿数据。

查询结果是**三态**，`unavailable` 绝不能当成「工号不存在」（Casdoor 一抖动就全部分配失败）：

| 结果 | 判定 | 处理 |
|---|---|---|
| `found` | 用户存在、`owner` 与目标租户一致、`isForbidden/isDeleted` 均为假 | 写入 Casdoor 返回的规范 `name` |
| `absent` | `data:null` / 组织不符 / 账号禁用或删除 | 422 `employee_not_found`，不落库 |
| `unavailable` | 接口不可达、无权限、响应无法解析 | 按 `CASDOOR_VERIFY_EMPLOYEE` 策略处理 |

`CASDOOR_VERIFY_EMPLOYEE`（默认 `optional`）三档：

| 值 | 查不到人 | 查询不可用 | 适用 |
|---|---|---|---|
| `off` | 放行 | 不发起查询 | 离线 / 测试 |
| `optional` | 422 拒绝 | **告警并放行**（可用性优先） | 默认 |
| `required` | 422 拒绝 | 502 `casdoor_error` 拒绝 | 要求强一致 |

生产实调结论（2026-11-10，`https://office.uassist.cn:40000/auth`）：

- `?owner=bestlink&id=bestlink/JH000059` → `{"status":"ok","data":{"owner":"bestlink",
  "name":"JH000059","displayName":"胥晓冬","id":"7994b554-…","isForbidden":false}}`；
- `?id=bestlink/NO_SUCH_EMP` → `{"status":"ok","data":null}`，即明确的「不存在」；
- **`owner` 查询参数会被 Casdoor 忽略**（`id` 才是唯一依据），所以必须校验返回对象
  的 `owner`，否则别家组织的工号会被误判存在；
- 工号大小写不敏感（`jh000059` 也返回 `JH000059`），因此写库用返回的规范 `name`；
- 裸工号、裸 UUID、`owner/UUID` 都查不到（返回 `GetOwnerAndNameFromId() error`
  或 `data:null`），所以 UUID 输入在业务层直接 422 `employee_no_required`；
- `GET /api/get-users` 需要授权（`Unauthorized operation`），不能用于匿名枚举组织成员。

查询身份按「服务令牌 → 当前管理员 access token → 匿名」依次尝试；原始 Token 只存在
请求级 `CurrentUser.access_token`，不写入日志、事件数据或任何响应体。

> **安全提示**：本实例 `/auth/api/get-user` 匿名可读，意味着组织成员对象（含邮箱等）
> 可被枚举。这是 Casdoor 侧既有配置，不是本服务引入的。若需收紧，请在 Casdoor
> 侧限制该接口权限，并在 `.env` 配好 `CASDOOR_CLIENT_SECRET` 走服务令牌；本服务的
> 校验链路会自动改用有身份的请求，`absent` / `unavailable` 判定不受影响。

### 4.5 管理员判定与 403 排障

管理员接口要求 `is_admin = true`，判定路径有 **6 条，任一命中即可**：

| # | 判定依据 | 配置项 | 默认值 |
|---|---|---|---|
| 1 | 角色命中跨组织管理员 | `CASDOOR_GLOBAL_ADMIN_ROLES` | `global_admin` |
| 2 | 角色命中管理员 | `CASDOOR_ADMIN_ROLES` | `global_admin,admin,role_admin` |
| 3 | Casdoor 用户属性 `isAdmin=true` | — | — |
| 4 | 用户组命中管理员组 | `CASDOOR_ADMIN_GROUPS` | `admins` |
| 5 | 权限命中管理员权限 | `CASDOOR_ADMIN_PERMISSIONS` | 空 |
| 6 | 用户名 / 邮箱命中白名单 | `CASDOOR_ADMIN_USERS` | 空 |

> **命名格式坑（最容易踩）**：Casdoor 后台显示 / API 返回的 `roles[].name` 常是
> `role_admin`（内置管理员角色），`groups[]` 则是**带组织前缀的全限定名**
> `bestlink/admins`，而不是 `admin` / `admins`。若白名单只写 `admin` 与 `admins`，
> 一个明明在管理员组、有管理员角色的用户仍会被判为非管理员。
> 服务端已做两项兼容：默认角色列表包含 `role_admin`；用户组匹配时自动剥离
> `owner/` 前缀后再与短名比对（也支持配置直接写 `bestlink/admins`、`*:admins`）。
> `permissions[].name` 常是 `permission_96s724` 这类自动生成的随机名，
> 用它做白名单前请确认名称稳定，更推荐用角色或用户组。

**排障三步**：

```bash
# ① 带上 Access Token 查询自己的身份与判定依据
curl -H "Authorization: Bearer <token>" http://127.0.0.1:8040/api/my/profile
```

响应中的 `roles` / `groups` / `permissions` 是 Casdoor 返回的原始名称，
`admin_policy` 是当前生效的判定配置，`is_admin` 是最终结果。

```json
{
  "username": "zhangsan",
  "roles": ["auditor"],
  "groups": ["acme"],
  "permissions": [],
  "is_admin": false,
  "admin_reason": [],
  "admin_policy": {
    "admin_roles": ["admin", "global_admin", "role_admin"],
    "admin_groups": ["admins"],
    "admin_permissions": [],
    "admin_users": [],
    "global_admin_roles": ["global_admin"]
  }
}
```

**② 403 响应自带诊断信息**，无需查日志：

```json
{
  "code": "forbidden",
  "message": "该操作需要管理员权限",
  "status": 403,
  "details": {
    "reason": "当前用户 zhangsan（角色=['auditor']，用户组=['acme']，权限=无）未命中管理员判定条件。"
  }
}
```

也可以用命令行自查脚本（无需启动服务）：

```bash
uv run scripts/whoami.py <access_token>
```

输出身份信息、各判定项命中情况与可行的授权方式。

**③ 三种修复方式（任选其一）**：

- **A. 在 Casdoor 后台授权角色/用户组**（推荐，保持 Casdoor 单一事实源）
  给该用户添加 `admin` / `role_admin` 角色，或加入 `admins` 用户组
  （API 里叫 `bestlink/admins` 也能命中）。若你们用了别的角色名或组名，
  把实际名称写进 `CASDOOR_ADMIN_ROLES` / `CASDOOR_ADMIN_GROUPS`。
- **B. 用 Casdoor Permission 授权**（Casdoor 官方 RBAC 用法）
  在 Casdoor 后台为应用创建 Permission（如 `device-admin`），授权给用户/组/角色，
  然后配置 `CASDOOR_ADMIN_PERMISSIONS=device-admin`。
- **C. 用户名白名单**（最省事的兜底）
  `CASDOOR_ADMIN_USERS=zhangsan,zhaoliu@acme.com`，无需改动 Casdoor。

> ⚠️ Casdoor 的 `roles` 与 `permissions` 在 API 返回中是**对象数组**
> （`[{"owner":..,"name":..}]`），不是字符串数组。若你的用户明明有管理员角色却仍被拒，
> 请先用 `/api/my/profile` 确认 `roles` 是否为空——这通常意味着该用户根本没被授权，
> 而不是后端解析问题。

### 4.6 APP / 管理后台登录流程

1. 前端从 `GET /api/auth/config` 读取 `authorization_endpoint` 与 `client_id`；
2. 跳转授权页 → 用户在 Casdoor 登录 → 回调拿到 `code`；
3. 用 `code` 换取 `access_token`；
4. 之后所有业务请求带 `Authorization: Bearer <access_token>`。

---

## 5. 接口清单

统一前缀 `/api`。权限列说明：**A** = 管理员，**U** = 任意已认证员工。

### 5.1 设备管理

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/devices` | A | 设备列表（分页；关键字 / 状态 / 型号 / 领用 / 绑定筛选） |
| POST | `/api/devices` | A | 设备入库，返回系统生成的 AuthKey 明文 |
| POST | `/api/devices/batch` | A | 批量入库（≤1000 条，单条失败不影响其余） |
| GET | `/api/devices/{id}` | A | 设备详情 |
| PUT | `/api/devices/{id}` | A | 更新档案（SN 不可改） |
| POST | `/api/devices/{id}/assign` | A | 分配设备给员工（提交工号；校验存在性、组织一致性与账号状态） |
| POST | `/api/devices/{id}/return` | A | 设备归还（解绑 + 关闭领用） |
| POST | `/api/devices/{id}/disable` | A | 禁用设备 |
| POST | `/api/devices/{id}/enable` | A | 启用设备 |
| POST | `/api/devices/{id}/lost` | A | 设备挂失 |
| POST | `/api/devices/{id}/repair` | A | 转入维修 |
| POST | `/api/devices/{id}/retire` | A | 设备报废（终态） |
| GET | `/api/devices/{id}/history` | A | 生命周期事件历史 |
| GET | `/api/devices/{id}/assignments` | A | 领用历史 |
| GET | `/api/devices/{id}/bindings` | A/U | 绑定历史（员工仅见自己的） |
| GET | `/api/devices/{id}/status` | A | 设备最新状态 |

### 5.2 APP 绑定（员工侧）

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/api/devices/{id}/bind` | U | 绑定自己领用的设备，可同时设昵称 |
| POST | `/api/devices/{id}/unbind` | U | 解绑自己的设备（管理员可撤销任意绑定） |
| PUT | `/api/devices/{id}/nickname` | U | 修改昵称（仅绑定员工或管理员） |

### 5.3 设备凭证

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/devices/{id}/credential` | A | 凭证版本历史（不含明文） |
| POST | `/api/devices/{id}/credential/rotate` | A | 轮换 AuthKey，返回新明文 |
| POST | `/api/devices/{id}/credential/revoke` | A | 撤销当前凭证 |

### 5.4 员工自助与认证

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/my/devices` | U | 我领用的设备，`items[].auth_key` 为当前有效的 AuthKey 明文，无可用凭证时为 `null` |
| GET | `/api/my/devices/{id}` | U | 我的设备详情 |
| GET | `/api/my/profile` | U | 当前登录身份（租户、角色、是否管理员） |
| GET | `/api/auth/config` | 公开 | Casdoor 登录参数（授权地址、client_id 等） |

### 5.5 表格管理（跨设备只读）

供管理后台「分别管理每张表」使用，全部只读；写操作仍走 §5.1–§5.3 的设备子资源接口，
以复用状态机、事务与事件记录。

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/tables/assignments` | A | device_assignment 列表（按领用状态 / 员工 / SN 筛选） |
| GET | `/api/tables/bindings` | A | device_binding 列表（按状态 / 员工 / 昵称筛选） |
| GET | `/api/tables/credentials` | A | device_credential 列表（不含 AuthKey 明文） |
| GET | `/api/tables/statuses` | A | device_status 列表（按在线 / 生命周期筛选） |
| GET | `/api/tables/events` | A | device_event 列表（按事件类型 / 设备 / 操作人筛选） |
| GET | `/api/tables/employees` | A | 员工清单（由领用记录反推，供领用弹窗选人） |
| GET | `/api/tables/overview` | A | 概览聚合指标（总量 / 生命周期分布 / 领用绑定 / 事件统计） |

### 5.6 错误响应格式

```json
{
  "code": "invalid_device_state",
  "message": "设备当前状态 disabled 不允许绑定（README §7.8）",
  "status": 409,
  "details": {}
}
```

| 错误码 | HTTP | 场景 |
|---|---|---|
| `unauthorized` | 401 | 缺少 / 无效 Token |
| `forbidden` | 403 | 权限不足、跨租户、绑定他人设备 |
| `not_found` | 404 | 设备不存在或不可见 |
| `conflict` | 409 | SN 重复、重复领用 / 绑定、设备已被占用 |
| `invalid_device_state` | 409 | 设备生命周期状态不允许当前操作 |
| `validation_error` | 422 | 参数校验失败 |
| `employee_not_found` | 422 | 工号在 Casdoor 中不存在 / 组织不符 / 账号禁用（继承 `validation_error` 状态码） |
| `employee_no_required` | 422 | 领用表单填了 Casdoor UUID 或姓名，应填工号 `preferred_username` |
| `casdoor_error` | 502 | Casdoor 服务异常；`CASDOOR_VERIFY_EMPLOYEE=required` 且工号查询不可用时也会返回 |

---

## 6. 与 README 的关键实现差异（需确认）

1. **不提供设备侧（固件）接口**。录音卡固件不与后端通信，因此：
   - 未实现「设备用 AuthKey 调用后端做身份认证」的端点；
   - `device_status` 表已建好，但**没有状态上报接口**，电量 / 充电 / 录音状态
     只能由后续接入固件或第三方同步时填充（服务层已预留
     `device_service` 中的状态读写逻辑）。
   - 手机 APP 从领用列表取得 AuthKey，用于与录音卡认证并建立连接。
2. **首次绑定即激活设备**：设备在 `registered` 状态下被员工首次绑定时，
   自动流转为 `active` 并写入 `activated_at` 与 `device_activated` 事件。
   README §5.1 只规定了入库后为 `registered`，未定义激活时机，此处按业务直觉实现。
3. **员工自助接口为 `/api/my/*`**：README §6 的接口表全部面向管理员；
   员工查看自己设备的能力由 `/api/my/devices` 提供，避免把全量设备列表开放给普通员工。
4. **归还默认同时解绑**（`revoke_binding=true`），与 README §5.4「解除 APP 绑定 → 关闭领用」一致。

---

## 7. 安全约束实现对照（README §7）

| # | 约束 | 实现 |
|---|---|---|
| 1 | SN 全局唯一，不允许员工修改 | `uk_device_sn` 唯一索引 + `DeviceUpdate` 不含 SN |
| 2 | Bluetooth Name 不唯一，不作为认证依据 | 仅作展示字段，不参与任何鉴权 |
| 3 | 手机 APP 获取领用设备的 AuthKey | 明文存入 `device_credential.auth_key`，领用列表按员工与租户授权返回当前有效凭证 |
| 4 | APP 用户身份使用 Casdoor Token | `get_current_user` 依赖强制 Bearer 校验 |
| 5 | 设备身份使用 AuthKey | `device_credential` 独立凭证表，与用户令牌完全分离 |
| 6 | 只有当前领用员工可以绑定 | `bind_device` 校验 `assignment.user_id` |
| 7 | 管理员可撤销绑定和领用授权 | `unbind_device(is_admin=True)`、`return_device` |
| 8 | 禁用 / 挂失 / 报废设备不得创建新绑定 | `BINDABLE_LIFECYCLE_STATUSES` 白名单 |
| 9 | 归还后原员工立即失去操作权限 | 关闭领用 + 解绑 + 状态校验三重拦截 |
| 10 | 所有管理操作写入 device_event | 服务层统一 `record_event` |

**数据库层兜底**（不依赖业务代码）：

- `uk_credential_active`：每台设备同时最多一个有效凭证
- `uk_assignment_current`：一台设备同时只能分配给一个员工
- `uk_binding_device` / `uk_binding_assignment`：一台设备 / 一条领用记录同时只有一个有效绑定
- `fk_binding_assignment` 组合外键：绑定员工必须与领用员工一致

---

## 8. 事件类型

| 事件 | 触发时机 |
|---|---|
| `device_registered` | 设备入库 |
| `device_activated` | 员工首次绑定，设备转为 active |
| `device_assigned` | 管理员分配设备 |
| `device_returned` | 管理员归还设备 |
| `device_bound` / `device_unbound` | APP 绑定 / 解绑 |
| `nickname_updated` | 修改昵称 |
| `credential_rotated` / `credential_revoked` | 凭证轮换 / 撤销 |
| `device_disabled` / `device_enabled` | 禁用 / 启用 |
| `device_lost` / `device_repaired` / `device_retired` | 挂失 / 维修 / 报废 |
| `device_profile_updated` | 档案字段更新 |

事件详情（`event_data` JSON）记录变更前后值、操作人与原因，可用于完整追溯。

---

## 9. 测试

```bash
cd backend
uv run -m pytest              # 75 passed
```

覆盖范围：

| 文件 | 覆盖内容 |
|---|---|
| `tests/test_device_crud.py` | 入库、SN 唯一、批量入库、档案更新、生命周期流转、事件落库 |
| `tests/test_assignment.py` | 领用、重复领用、禁用设备不可领用、归还、归还后重新分配、归还即失权 |
| `tests/test_employee_lookup.py` | 工号存在性校验：命中规范化、不存在/跨组织/禁用拒绝、查询不可用降级、required/off 策略、UUID 输入拒绝 |
| `tests/test_binding.py` | 绑定、非领用人拒绝、重复绑定、禁用设备不可绑定、昵称、解绑、绑定历史 |
| `tests/test_security.py` | 未认证 / 非管理员、跨租户隔离、AuthKey 返回范围、凭证与绑定唯一约束、SN 不可变 |
| `tests/test_casdoor.py` | Casdoor userinfo / introspect 解析、管理员判定、禁用账号拒绝、登录配置 |
| `tests/test_tables.py` | 表格级接口：五张表的跨设备列表与筛选、员工清单、概览聚合、租户隔离与管理员拦截 |
| `tests/test_config.py` | 配置中心：`api_prefix` 规范化与 Windows 路径拦截、静态目录推导 |

测试使用 SQLite 内存库，与生产 MySQL 共用同一套 ORM 与服务层代码。

---

## 10. 部署清单

生产上线前需确认：

- [ ] `.env` 中 `APP_ENV=production`
- [ ] `CASDOOR_CLIENT_ID` / `CASDOOR_CLIENT_SECRET` 已配置（introspect 与严格查询身份所需）
- [ ] `CASDOOR_VERIFY_EMPLOYEE` 已按一致性要求选定 `optional` / `required`；容器部署时
      已在 `docker-compose.yml` 透传
- [ ] `DATABASE_URL` 指向 MySQL 8.0.16+，并已执行 `sql/schema.mysql.sql`
- [ ] `CORS_ORIGINS` 收敛为具体域名（不要保留 `*`）
- [ ] Casdoor 应用已配置正确的回调地址与 Token 有效期
      （管理后台：`https://office.uassist.cn:40000/voicehub/`）
- [ ] 反向代理启用 HTTPS
- [ ] `API_PREFIX` 与 nginx 透传路径一致（如 `/voicehub-api`）
- [ ] nginx 已配置 `/voicehub/` 静态托管与 `/voicehub-api/` 反向代理
- [ ] Casdoor 应用后台已登记管理后台回调地址 `https://office.uassist.cn:40000/voicehub/`

## 11. 后续可扩展点

- **设备状态上报**：`device_status` 表已就绪，可在 `app/api/routes/` 下新增 `/api/device/status`（需固件或客户端配合）。
- **批量设备分配**：`register_devices_batch` 已是逐条事务，批量分配可复用同一模式。
- **员工离职回收**：可对接 Casdoor 的用户事件或定时任务，扫描超期领用记录并批量归还。
- **企业资产统计**：`device` / `device_assignment` 已有完整维度，直接聚合即可出报表。
