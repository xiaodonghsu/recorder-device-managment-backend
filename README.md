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

---

## 2. 目录结构

```
backend/
├── app/
│   ├── main.py                 # 应用入口、生命周期、异常处理、CORS
│   ├── core/
│   │   ├── config.py           # 配置中心（环境变量 / .env）
│   │   ├── enums.py            # 生命周期状态、事件类型、状态机规则
│   │   ├── errors.py           # 统一异常体系（错误码 ↔ HTTP 状态码）
│   │   └── security.py         # AuthKey 生成、HMAC-SHA256 摘要、常量时间比对
│   ├── db/
│   │   ├── models.py           # 6 张表的 ORM 定义（严格对齐 README §4）
│   │   └── session.py          # 引擎 / 会话 / 事务依赖
│   ├── schemas/                # Pydantic 请求响应契约
│   ├── integrations/
│   │   └── casdoor.py          # Casdoor OIDC 客户端与身份模型
│   ├── api/
│   │   ├── deps.py             # 认证依赖、租户范围、管理员判定
│   │   └── routes/             # 路由：devices / bindings / credentials / me / auth
│   └── services/               # 业务服务层（事务、状态机、事件）
├── sql/schema.mysql.sql        # MySQL 生产建表脚本
├── scripts/init_db.py          # 数据库初始化脚本
├── tests/                      # 45 个自动化测试
├── requirements.txt
└── .env.example
```

---

## 3. 快速开始

```bash
cd backend

# 1. 安装依赖
python -m pip install -r requirements.txt

# 2. 配置环境变量
cp .env.example .env
#    至少填写 CASDOOR_CLIENT_ID / CASDOOR_CLIENT_SECRET / AUTH_KEY_SECRET

# 3. 初始化数据库（开发环境用 SQLite 会自动建表，此步可跳过）
python scripts/init_db.py

# 4. 启动
python -m uvicorn app.main:app --reload --port 8000
```

启动后：

- 接口文档：<http://127.0.0.1:8000/docs>
- OpenAPI：<http://127.0.0.1:8000/openapi.json>
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
python scripts/init_db.py                     # 方式二：由 ORM 建表
```

> MySQL 需 **8.0.16 及以上**，否则 `CHECK` 约束不会被执行。
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
CASDOOR_CLIENT_ID=app-built-in
CASDOOR_CLIENT_SECRET=****
CASDOOR_ORGANIZATION=built-in
```

未配置 client 凭据时服务仍可运行（Token 走 userinfo 校验），但：
- 无法用用户名反查 Casdoor 用户 ID（领用时只能填 UUID 形式的用户 ID）；
- 无法使用 introspect 模式。

### 4.4 租户与身份映射

| Casdoor 字段 | 本服务概念 | 说明 |
|---|---|---|
| `owner`（组织名） | `tenant_id` | 一个 Casdoor 组织 = 一个企业；多租户隔离的基准 |
| `id`（用户 UUID） | `user_id` | 领用记录、绑定记录中保存的员工标识 |
| `name` | 用户名 | 领用时支持填用户名，服务端经 Casdoor 解析为 UUID |
| `role` / `roles` | 管理员判定 | 命中 `CASDOOR_ADMIN_ROLES` 即视为管理员 |
| `groups` | 管理员判定 | 命中 `CASDOOR_ADMIN_GROUPS` 即视为管理员 |
| `isAdmin` | 管理员判定 | Casdoor 内置管理员标记 |
| `isForbidden` | 登录拦截 | 被禁用的账号直接拒绝 |

**租户隔离**：管理员接口的可见范围由服务端从 Token 推导，**不采信客户端提交的
`tenant_id`**；越权访问一律返回 404（不泄露其他企业设备是否存在）。
仅 `CASDOOR_GLOBAL_ADMIN_ROLES`（默认 `global_admin`）可跨企业操作。

**管理员用户组**支持两种写法：

```bash
CASDOOR_ADMIN_GROUPS=admins            # 命中本组织的同名用户组
CASDOOR_ADMIN_GROUPS=acme:admins       # 仅命中指定组织的同名用户组
CASDOOR_ADMIN_GROUPS=*:admins           # 任意组织的同名用户组
```

### 4.5 APP / 管理后台登录流程

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
| POST | `/api/devices` | A | 设备入库，返回一次性 AuthKey 明文 |
| POST | `/api/devices/batch` | A | 批量入库（≤1000 条，单条失败不影响其余） |
| GET | `/api/devices/{id}` | A | 设备详情 |
| PUT | `/api/devices/{id}` | A | 更新档案（SN 不可改） |
| POST | `/api/devices/{id}/assign` | A | 分配设备给员工 |
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
| POST | `/api/devices/{id}/credential/rotate` | A | 轮换 AuthKey，返回新明文（仅此一次） |
| POST | `/api/devices/{id}/credential/revoke` | A | 撤销当前凭证 |

### 5.4 员工自助与认证

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/my/devices` | U | 我领用的设备 |
| GET | `/api/my/devices/{id}` | U | 我的设备详情 |
| GET | `/api/my/profile` | U | 当前登录身份（租户、角色、是否管理员） |
| GET | `/api/auth/config` | 公开 | Casdoor 登录参数（授权地址、client_id 等） |

### 5.5 错误响应格式

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
| `casdoor_error` | 502 | Casdoor 服务异常 |

---

## 6. 与 README 的关键实现差异（需确认）

1. **不提供设备侧（固件）接口**。录音卡固件不与后端通信，因此：
   - 未实现「设备用 AuthKey 调用后端做身份认证」的端点；
   - `device_status` 表已建好，但**没有状态上报接口**，电量 / 充电 / 录音状态
     只能由后续接入固件或第三方同步时填充（服务层已预留
     `device_service` 中的状态读写逻辑）。
   - AuthKey 当前仅用于入库时生成、轮换与撤销，作为设备独立凭证存档。
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
| 3 | AuthKey 不明文存储，不返回给普通 APP | HMAC-SHA256 摘要入库；明文仅入库 / 轮换响应一次性返回 |
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
python -m pytest              # 45 passed
```

覆盖范围：

| 文件 | 覆盖内容 |
|---|---|
| `tests/test_device_crud.py` | 入库、SN 唯一、批量入库、档案更新、生命周期流转、事件落库 |
| `tests/test_assignment.py` | 领用、重复领用、禁用设备不可领用、归还、归还后重新分配、归还即失权 |
| `tests/test_binding.py` | 绑定、非领用人拒绝、重复绑定、禁用设备不可绑定、昵称、解绑、绑定历史 |
| `tests/test_security.py` | 未认证 / 非管理员、跨租户隔离、AuthKey 不落库、凭证与绑定唯一约束、SN 不可变 |
| `tests/test_casdoor.py` | Casdoor userinfo / introspect 解析、管理员判定、禁用账号拒绝、登录配置 |

测试使用 SQLite 内存库，与生产 MySQL 共用同一套 ORM 与服务层代码。

---

## 10. 部署清单

生产上线前需确认：

- [ ] `.env` 中 `APP_ENV=production`
- [ ] `AUTH_KEY_SECRET` 替换为 32 位以上随机值（否则启动日志给出告警）
- [ ] `CASDOOR_CLIENT_ID` / `CASDOOR_CLIENT_SECRET` 已配置
- [ ] `DATABASE_URL` 指向 MySQL 8.0.16+，并已执行 `sql/schema.mysql.sql`
- [ ] `CORS_ORIGINS` 收敛为具体域名（不要保留 `*`）
- [ ] Casdoor 应用已配置正确的回调地址与 Token 有效期
- [ ] 反向代理启用 HTTPS

## 11. 后续可扩展点

- **设备状态上报**：`device_status` 表已就绪，可在 `app/api/routes/` 下新增 `/api/device/status`（需固件或客户端配合）。
- **批量设备分配**：`register_devices_batch` 已是逐条事务，批量分配可复用同一模式。
- **员工离职回收**：可对接 Casdoor 的用户事件或定时任务，扫描超期领用记录并批量归还。
- **企业资产统计**：`device` / `device_assignment` 已有完整维度，直接聚合即可出报表。
