# 社区乒乓球赛 · Flask 裁判台

一个可直接用 Docker Compose 启动的社区乒乓球赛裁判 Web 台：Flask + 服务端渲染 + SQLite，
无外部服务、无前端构建。内置 A、B 两组各三人的单循环赛程（共六场），支持整表录入/更正、
自动排名、半决赛对阵、按场记分、球台对调、撤场封存、同两人重新配对、旧页面交分裁决，
并在两场现行半决赛都有比分后才显示决赛选手（另附轻量决赛/冠军）。

---

## 1. 快速开始

### 方式 A：Docker Compose（交付方式）

```bash
docker compose up --build
# 打开 http://localhost:5000
# 若宿主机 5000 端口被占用：
PORT=8080 docker compose up --build   # 然后访问 http://localhost:8080
```

- SQLite 文件位于命名卷 `pingpong-sqlite-data` 内的 `/data/referee.db`，重启/重建容器数据不丢。
- 容器入口会自动建表并播种 6 名球员、6 场小组赛和初始半决赛对阵。
- 自带 `/healthz` 健康检查。

### 方式 B：本地 Python 运行

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

python wsgi.py
# 或：DB_PATH=./data/referee.db gunicorn --bind 127.0.0.1:5000 --preload wsgi:app
```

### 跑测试 / 黑盒试跑

```bash
pip install pytest
pytest -q                                   # 15 个行为测试

# 先起服务，再跑纯标准库黑盒剧本（对调球台 + 撤场重配 + 旧页交分）
DB_PATH=/tmp/trial.db gunicorn --bind 127.0.0.1:5555 --preload wsgi:app &
python scripts/e2e_trial.py http://127.0.0.1:5555
```

---

## 2. 赛制与排名（严格按需求实现）

- 内置球员与抽签号（抽签号全局唯一，页面可见）：

  | 组 | 抽签号 / 球员 |
  |---|---|
  | A | 1 陈一鸣、2 林浩然、3 赵子轩 |
  | B | 4 周雨桐、5 吴俊杰、6 郑晓峰 |

- 每组三人单循环，固定 3 场，两组合计 **6 场**，首页一张整表一次提交/更正。
- 排名键只有两条，顺序固定：**胜场数降序 → 抽签号升序**
  （代码见 `app/tournament.py::compute_standings`，不掺入小分/相互战绩）。
- 半决赛默认：**一号台 A1 对 B2（SF1）**、**二号台 B1 对 A2（SF2）**。
- 小组整表提交后，半决赛对阵立即按最新排名调和更新。

---

## 3. 核心身份模型：场次 ≠ 球台 ≠ 球员对

这是“换台比分照旧 / 撤下可查 / 重配重新记分 / 旧页面交分裁决”的关键。
淘汰赛每一次“配对发生”都是 `matches` 表中一条独立记录，拥有稳定自增 `id`：

| 操作 | 对数据做了什么 | 旧页面交分结果 |
|---|---|---|
| **对调球台** | 只交换两条 active 记录的 `table_no`，`score1/score2` 原样不动 | 旧表单 POST 到固定的 `/matches/<id>/score`，场次仍 active → **接受** |
| **撤下对阵** | 记录置 `status='withdrawn'`，比分、时间、原因全部保留，历史页/旧链接可查 | 旧 `id` 已是 withdrawn → **HTTP 409 + “请刷新”提示**（JSON 返回 `code=match_withdrawn`） |
| **同两人重新配对** | 插入一条**全新 id、空白比分**的 active 记录；绝不继承旧比分 | 旧 id 仍 withdrawn → **继续拒绝**；新 id 页面正常记分 |

数据库还有两个部分唯一索引兜底业务不变量：同一轮次同一对球员至多一条现行场次、
同一轮次同一张球台至多一条现行场次。

### 小组重新整表提交时，已有半决赛怎么办？

调和逻辑（`app/services.py::reconcile_semifinals`）按“球员对”匹配：

- 球员对仍属于 A1-B2 / B1-A2：保留同一条场次，**已录比分和球台都保留**；
- 球员对已不在最新对阵中：
  - 从未记分的自动空壳 → 删除，避免历史噪音；
  - 已记分或人工重配场次 → 标记 `withdrawn / lineup_update`，比分封存可查，再生成空白新场。

---

## 4. 页面与 HTTP / JSON 接口

页面（服务端渲染，中文界面，关键元素带 `data-testid`）：

| 页面 | 地址 |
|---|---|
| 裁判台（排名 + 六场整表 + 球台 + 决赛选手） | `GET /` |
| 某场次的固定“旧页面”（active 可记分；withdrawn 封存/提示刷新/重配） | `GET /matches/<id>` |
| 历史对阵与撤场记录、通用重新配对 | `GET /history` |
| 完整 JSON 状态 | `GET /api/state` |
| 健康检查 | `GET /healthz` |

主要写接口（表单与 JSON 同源；`/api/...` 返回 JSON，表单提交走 PRG 跳转/错误页）：

| 行为 | 表单/页面 | JSON |
|---|---|---|
| 六场比分整表提交 | `POST /groups/scores`（字段 `score_<gmId>_p1/p2`） | `POST /api/groups/scores`，body `{"scores":{"1":[3,1], …}}` |
| 按场登记/更正比分 | `POST /matches/<id>/score`（`score1`,`score2`） | `POST /api/matches/<id>/score` |
| 旧球台页交分（支持 `match_id` 或 `player1_id/player2_id`/`pair_key` 解析） | `POST /tables/<n>/score` | 同名，JSON |
| 对调两张球台 | `POST /semifinals/swap-tables` | `POST /api/semifinals/swap-tables` |
| 单场换台 | `POST /matches/<id>/table` | — |
| 撤下对阵 | `POST /matches/<id>/withdraw` | `POST /api/matches/<id>/withdraw` |
| 原班两人重新配对 | `POST /matches/<id>/re-pair`（兼容别名 `/repait`） | `POST /api/matches/<id>/repait` |
| 任意两人新建配对 | `POST /pairings` | 同名，JSON |

另提供语义别名 `POST /semifinals/<id>/score`、`POST /semifinal/<id>/score`、`POST /swap-tables`。

比分规则：两格必须同时填写；允许 0–999 的整数（可填局分或小分）；
乒乓球必须分胜负，**双方相等会被拒绝**并给出中文错误（小组整表为原子提交，任一场不合法则六场都不入库）。

决赛选手判定：现行半决赛**恰好两场且两场都有可分胜负的比分**时，
`#finalists` 区域才显示两位决赛选手；否则显示 `已完成 x/y` 与原因。
决赛选手产生后会出现轻量决赛记分与冠军横幅（加分项）。

---

## 5. 收尾核对：做成了什么、实际测了什么、还有什么没做

### 5.1 做成了什么（对照原始要求）

- ✅ Flask 裁判台 + SQLite 持久化，Docker Compose 可启动（含命名卷与健康检查）。
- ✅ 内置 A/B 各三人单循环，固定六场；首页一张整表一次录入或更正，原子校验。
- ✅ 排名严格按“胜场降序 → 同胜场抽签号升序”，页面同时展示胜/负/已赛/得失分。
- ✅ 半决赛默认一号台 A1-B2、二号台 B1-A2；整表提交后对阵调和更新。
- ✅ 半决赛按场登记/更正；另有固定 URL 的单场页面。
- ✅ **对调球台后比分跟随球员/场次原样保留**，旧场次页/旧首页卡片继续可交分。
- ✅ **撤下记录与比分在历史页和旧链接中仍可查**。
- ✅ **同两人重新配对生成全新空白场次，重新记分**；重复的现行同对配对会被 409/提示拦截。
- ✅ **已撤下场次的旧页面交分被退回 HTTP 409，并明确提示“刷新页面”**（HTML 与 JSON 双形态）。
- ✅ 两场现行半决赛都有比分后才显示决赛选手；未满足时显示进度与原因。
- ✅ 额外：单场换台、通用配对、决赛记分/冠军、完整 JSON API、15 个 pytest 用例、黑盒试跑脚本。

### 5.2 实际测了什么（都是本环境真实执行，非仅阅读代码）

- ✅ `pytest -q`：**15 passed**。覆盖播种、连环套同胜场按抽签号排名、规则对阵、
  整表非法时原子不写库、决赛选手显示条件、换台比分随人走、旧球台入口按 match_id/球员对解析、
  撤下/历史可查/重配空白/旧页 409 且“刷新”、JSON `match_withdrawn`、
  小组改排名时旧半决赛“空壳删除 / 有比分封存”、对调所需场次数量校验等。
- ✅ `python scripts/e2e_trial.py http://127.0.0.1:5555`：对**真实 gunicorn
  （2 workers + threads + `--preload`）+ 文件 SQLite** 跑纯 HTTP 黑盒剧本，
  **32 项检查全部通过**，完整执行了题目点名的流程：
  整表提交 → 排名/对阵正确 → 旧页面登记 3:1 → 对调球台（m1 移到二号台且 3:1 保留）→
  用同一张旧页面更正为 3:2 成功 → 旧一号台上下文交分成功且记在原球员对名下 →
  撤下 m1（比分 3:0 封存）→ 历史页可见 `3 : 0` → 原班两人重配出全新空白场次 →
  **再用旧页面交分收到 409 + “刷新”提示，封存比分未被改动** → 新场记分 + 另一场记分 →
  两位决赛选手（陈一鸣、林浩然）上首页。
- ✅ 实跑 `docker-entrypoint.sh` 的初始化命令，确认生成 6 球员 / 6 小组赛 / 2 场 SF。
- ✅ 用 PyYAML 解析校验 `docker-compose.yml`，并用 JSON 解析校验 Dockerfile exec-form `CMD`。
- ✅ 所有 Python 文件 `py_compile` 通过；试跑期间 gunicorn 无 traceback / 500
  （仅有一条本沙箱 `HOME` 不可写导致的 gunicorn 控制套接字权限提示，与业务无关）。

### 5.3 还有什么没做 / 已知边界（如实说明）

- ⚠️ **本沙箱没有 docker / podman / 守护进程与 socket，无法在这里真正
  `docker compose build && docker compose up`**。Compose/Dockerfile 仅做了静态校验，
  并用与镜像相同的 `gunicorn ... wsgi:app` 命令在本机完整试跑；镜像内的 `pip install`
  未实跑（但 `requirements.txt` 中的 Flask/gunicorn 已从 PyPI 在本机成功安装并验证）。
  首次在有 Docker 的机器上构建时仍可能遇到网络/拉取基础镜像问题。
- 未做登录、裁判身份与权限控制（定位为社区内网裁判台，拿到页面即可操作）。
- 比分只记每场两个总数，未记录逐局小分、暂停、黄牌等明细。
- 小组赛六场是固定行原地更新，**不保留每次修改的历史版本**；淘汰赛的撤下/对阵更新则有状态、
  原因与时间戳可追溯，但也不是完整审计日志。
- 并发控制依赖 SQLite 事务 + `busy_timeout` + 两个部分唯一索引，没有实现乐观锁版本号；
  “旧页面”裁决依据是稳定场次 id 与 active/withdrawn 状态（正合题面语义）。
- 决赛/冠军为轻量加分项；未做季军赛、签表导出、打印海报、CSV 导出等。
- 没有浏览器级/视觉自动化测试（环境无 Playwright/Node 测试链），页面为服务端渲染，
  仅含确认框等少量原生 JS；未做多语言（仅中文）。
