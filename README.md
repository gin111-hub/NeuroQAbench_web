# QAbench Human Verify & Hard-Question Showcase

> **仓库**：<https://github.com/gin111-hub/NeuroQAbench_web> — 课题组内部迭代用。
> 代码仓库，题库 / DB / salt 不进仓库（见 `.gitignore`）；
> 题库从 `data/incoming/batch-NN -> <savaal 输出>` 本地 build 出来，各机器各自产。

课题组 MCQ benchmark 的两件事：

1. **Verify**：按 paper 展示每道题 + 预设正确答案 + 证据，专家对每题填一份选择题质控问卷（1-5 分 × N 维度）+ 可选评论。
2. **Hard-Questions**：公开展示跨 8 模型评测中 "4 个参考模型全错" 的难题。

参考模型：`azure-gpt-5.5` / `deepseek-v4-flash` / `glm-5.1` / `deepseek-v4-pro`。

## 目录

```
data/
├── papers.json              # paper 元信息 + slug 映射（手工维护）
├── questionnaire.json       # verify 问卷的单一来源（维度/尺度/阈值）
├── incoming/                # 新一批源目录 symlink 进来
├── verify/papers/<slug>/    # build.py 产物（verify 页用）
├── hard/hard_index.json     # build.py 产物（hard 页用）
└── exports/                 # show_submissions.py 产物（人类可读汇总）

scripts/
├── build.py                 # 零参数：扫 incoming/ 重建 verify/ 和 hard/
├── show_submissions.py      # 零参数：把 DB 汇总导出到 data/exports/
└── reset_submissions.py     # 清空提交表（带确认 + 自动备份）

frontend/                    # 纯静态站
backend/                     # FastAPI + SQLite，仅处理 /api/verify/submit
deploy/                      # nginx + systemd + backup.sh 样例
```

## 启动服务（本地或内网开发）

**一个终端一个命令，前后端同源，/api 和 /verify.html 都在同一个端口**：
```bash
cd /srv/x_to_skills/QAbench/human_verify_web
pip install -r requirements.txt           # 第一次需要
uvicorn backend.app:app --host 0.0.0.0 --port 8787
```

浏览器打开 **`http://<server-ip>:8787/`**（服务器本机就用 `localhost`）：
- `/` — 两个按钮：Verify / Hard-Questions
- `/verify.html` — 专家评审页；选 paper → 对每题打 1-5 分 → Submit → DB 落库
- `/hard.html` — 13 道难题公开展示

> `python3 -m http.server` 不要用——它不支持 POST，verify 的 Submit 会 501。

## 导入一批新数据

每批对应一组**不同的 paper**（同 paper 不跨批）：
```bash
ln -s /path/to/combined_hard_bank data/incoming/batch-NN
python3 scripts/build.py
```

`build.py` 零参数，会扫 `data/incoming/*`，三层去重校验（重复 symlink / 重复 id /
重复 source_paper 都会 abort）。若 `data/papers.json` 缺某篇 paper 的
`source_paper_key → slug` 映射，脚本会报错让你补一行。

## 看专家 verify 结果

```bash
python3 scripts/show_submissions.py
```

一条命令就会：
1. **终端打印 overall summary**（含"各维度得分 ≤ 3 的题目数量"统计）
2. 在 `data/exports/` 下写 4 个 **VSCode 直接能打开的** 文本文件：
   - `summary.txt` — 全局汇总 + 上面那段统计
   - `by_question.txt` — 每道题完整问卷结果（mean / bad_rate / 1-5 投票柱状图 / 评论），按"任一维度 bad_rate"降序排
   - `by_paper.txt` — 每 paper 粗汇总
   - `submissions.csv` — 原始行，列按当前问卷维度自动展开（Excel 能开）

每跑一次脚本 = 对数据库拍一张新快照，老的文件被覆盖。**DB 本身永远不会被这个脚本动。**

想直接写 SQL 查：`sqlite3 backend/db.sqlite`，字段/查询 recipe 见 `backend/schema.sql` 顶部注释。

## 清掉测试数据

```bash
python3 scripts/reset_submissions.py          # 问 y/N 确认，自动备份后清空
python3 scripts/reset_submissions.py --yes    # 不问
python3 scripts/reset_submissions.py --yes --no-backup
```

备份落在 `backend/backups/pre-reset-<时间戳>.sqlite.gz`。

## 改问卷

**只改一个文件**：`data/questionnaire.json`。
- `scale.min` / `scale.max` — 分值范围（当前 1-5）
- `scale.direction` — `higher-is-better` 或 `lower-is-better`
- `scale.bad_threshold` — 质量差的阈值（当前 3，配合 `higher-is-better` = 得分 ≤ 3 算差）
- `dimensions` — 维度数组，每项 `{key, prompt}`；加/删 N 个随意

**不需要动 DB schema / 不需要迁移**——responses 存为 JSON blob，前后端 + show_submissions 都从 questionnaire.json 读。改完刷新 verify.html 即可。

## 部署

参考 `deploy/`：
- `nginx.conf.example` — 托管 `frontend/` + `data/` 静态文件，`/api/` 反代到 uvicorn
- `systemd-fastapi.service.example` — 后端常驻
- `backup.sh` — cron 每日 00:30 跑，备份 `db.sqlite` 到 `backend/backups/`

## 数据流

```
savaal 输出 ──symlink──> data/incoming/batch-NN/
                              │
                              ▼                   data/papers.json
                      scripts/build.py (零参数)   data/questionnaire.json
                       ├──> data/verify/papers/<slug>/questions.json      │
                       └──> data/hard/hard_index.json                     │
                                                                          │
浏览器 ──fetch 静态 JSON──> verify.html / hard.html ◀──────────────────────┘
       └──POST /api──> FastAPI ──> backend/db.sqlite  (仅 verify 提交)
                                        │
                                        ▼
                          scripts/show_submissions.py (零参数)
                                        │
                                        ▼
                              data/exports/*.{txt,csv}
```
