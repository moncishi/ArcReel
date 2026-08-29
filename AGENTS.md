# ArcReel

AI 视频创作平台，将小说、剧本或创作构想转化为短视频。三层结构：`frontend/`（React SPA）→ `server/`（FastAPI，`agent_runtime/` 封装 Claude Agent SDK）→ `lib/`（核心库）。内嵌创作 Agent 的配置源在 `agent_runtime_profile/`，与开发态 `.claude/` 分离。

## 工具链与校验

后端使用 `uv`，前端与文档站使用 `pnpm`。push 前按改动范围执行对应校验：

```bash
uv run ruff check . && uv run ruff format . && uv run basedpyright && uv run lint-imports && uv run python -m pytest
uv run python scripts/audit_tests.py --check   # 改动测试文件时；同时扫后端 tests/ 与前端 *.test.*
(cd frontend && pnpm lint && pnpm check)
(cd website && pnpm check)
```

启动开发服务器、数据库迁移、测试规范（分层/替身/判据/闸门）、分支与提交规范、依赖管理、注释规范见 `CONTRIBUTING.md`。

### 快速验证

- **单个测试文件**：`uv run python -m pytest tests/unit/lib/db/test_x.py`（`-k` 关键字筛选、`-v` 详细输出）
- **后端全量**：`uv run python -m pytest -m "not e2e"`（`e2e` 依赖真实外部服务，CI 默认跳过；本地如需可加 `-m e2e`）
- **import 分层契约**：`uv run lint-imports` 校验 `lib.config < lib.*_backends < lib.custom_provider`，新增 ignore 条目前先确认该边无法直接消除
- **pre-commit 钩子**：`uv run pre-commit install` 一次性安装（ruff 自动修复、pre-push 全量 basedpyright、`pull_request_target` tripwire）

## 通用规范

- 面向用户的文本须同步添加全部已支持语言的翻译 key。语言清单以 `frontend/src/i18n/` 为准（当前 `zh` / `en` / `vi`），由 `tests/unit/lib/i18n/test_i18n_consistency.py` 校验——漏加任何一种语言都会 CI 失败。
- 代码与测试注释仅描述当前行为与约束；变更原因与议题编号写在 commit message / PR 描述中。
- 前端异步竞态（AbortSignal 取消链、函数型 hook option、store 刷新合并）、Windows 兼容（POSIX-only 常量、`os.chmod(0o600)`、显式 `encoding="utf-8"`）、Agent Runtime 不变量等路径绑定规则见 `.claude/rules/`，改对应路径时先读。

## 架构

架构总览、扩展新供应商、扩展新工作流阶段：`website/docs/dev/architecture.md`。领域文档（`CONTEXT.md` + `docs/adr/`，66 份决策记录）使用方式见 `docs/agents/domain.md`。

### 目录要点

- **`lib/`**：核心业务库（供应商后端、DB、资产、生成管线、成本核算），后端事实上的主代码库。
- **`server/`**：FastAPI 应用层。`app.py` 是入口，`agent_runtime/` 封装 Claude Agent SDK（沙箱 bwrap/sandbox-exec 默认开启），`remote_mcp.py` 提供外部 Agent 接入的 MCP 端点。
- **`agent_runtime_profile/`**：内嵌创作 Agent 的配置源（`.claude/skills/`、`.claude/agents/`、按 `content_mode` 拆分的 `CLAUDE.*.md`）。`lib/profile_manifest.py` 物化到各用户项目的 `.claude/`，修改配置应改源目录，不直接改项目侧文件。
- **`docs/`**：内部文档（ADR、领域、安全威胁模型等），不上站。用户文档唯一发布位置是 `website/docs/`（中文唯一写作源，英文 AI 译文）。
- **`projects/`**：用户项目数据目录（运行时生成，不入库）。

## Agent skills

- 议题追踪：GitHub Issues，用 `gh` CLI 操作；Spec 与细分 issue 的约定见 `docs/agents/issue-tracker.md`。
- Triage 标签状态机：`docs/agents/triage-labels.md`。
- 领域文档（`CONTEXT.md` + `docs/adr/`）的使用方式：`docs/agents/domain.md`。

## 发版与提交（摘要）

- trunk-based：只有 `main` 是长期分支，所有工作从最新 `main` 切短分支（`<type>/<slug>`）经 PR squash merge，禁止直接 push `main`。
- Commit message 用 Conventional Commits（`type(scope): 摘要`）；`feat` → minor、`fix` → patch，**不标记 breaking change**。
- 版本号与 changelog 由 release-please 自动维护，`pyproject.toml` / `frontend/package.json` 的 `version` 字段视为只读，**不要手动 bump**。
- 详细规范（分支寿命、squash、发版流程）见 `CONTRIBUTING.md`。
