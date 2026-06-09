---
type: guide
---

# Obsidian Vault 引导

本 Vault 是 dev-flow-skills 项目的**模板源**。每个实际项目使用独立的 Vault。

## 架构

```
dev-flow-skills/vault/     ← 模板源（git 跟踪）
  ├── templates/              任务/资料/经验/路线图模板
  └── GETTING-STARTED.md      本引导

各项目/vault/               ← 项目 vault（不跟踪）
  ├── tasks/                  项目任务节点
  ├── lessons/                踩坑/经验沉淀（结构化）
  ├── references/             项目资料节点
  ├── maps/                   项目路线图
  ├── templates/              从模板源复制
  ├── HOME.md                 项目知识库首页
  └── GETTING-STARTED.md      从模板源复制
```

## 新项目初始化

1. 安装 [Obsidian](https://github.com/obsidianmd/obsidian-releases/releases/latest)
2. 运行初始化脚本：
```bash
~/Projects/dev-flow-skills/scripts/init-project-vault.sh <项目路径>
```
3. 用 Obsidian 打开 `<项目路径>/vault/` 目录

> 不需要手动设置环境变量。task-node 通过 `resolve_vault()` 自动发现项目 vault：
> 优先级：`OBSIDIAN_VAULT_PATH` 环境变量 > 当前目录 `vault/` > Git 根目录 `vault/` > `~/.hermes/.env`

## 目录结构

| 目录 | 用途 | 模板 |
|------|------|------|
| `tasks/` | 任务节点 — 跟踪每个任务的状态、阶段、关联 | `templates/task-template.md` |
| `lessons/` | 经验沉淀 — 踩坑/教训/成功模式，结构化记录 | `templates/lesson-template.md` |
| `references/` | 资料节点 — 决策记录、API 参考 | `templates/reference-template.md` |
| `maps/` | 路线图 — 阶段规划、里程碑 | `templates/map-template.md` |
| `templates/` | 模板 — 创建新节点时复制对应模板 | — |

## 经验沉淀机制

**核心原则：踩坑不沉淀 = 重复踩坑。**

### 何时记录经验

- **ship 阶段**（强制） — 每个任务交付时回顾：这次任务有没有值得记录的经验？
- **fix 阶段**（推荐） — 修复了一个非平凡的 bug，记录现象→根因→方案
- **review 阶段**（推荐） — 发现了审查中的共性陷阱

### 经验节点结构

每个经验节点（`lessons/`）包含六个结构化字段：

1. **触发场景** — 什么情况下会再次遇到
2. **现象** — 出了什么问题/走了什么弯路
3. **根因** — 底层原因
4. **解决方案** — 具体怎么解决
5. **预防措施** — 下次如何避免
6. **适用范围** — 适用于/不适用于哪些场景

### 经验分类

- `pitfall` — 踩坑：掉进去过的陷阱
- `principle` — 经验法则：验证过的决策原则
- `pattern` — 成功模式：可复制的做法

### 通用 vs 项目级

- **通用经验** — `dev-flow-patterns` 技能内置（第一性原理、常见陷阱），跨项目适用
- **项目级经验** — vault `lessons/` 中沉淀，是对通用经验的补充和具体化

## 节点关系

每个节点通过 YAML frontmatter 建立依赖关系：

- **blocked_by** — 前置任务（必须完成后才能开始本任务）
- **blocks** — 后续任务（本任务完成后才能开始下游）
- **related** — 关联资料（参考关系，不阻塞）
- **lessons_learned** — 关联经验（ship阶段沉淀的踩坑/模式）

用 `[[节点ID]]` 语法创建双向链接，Obsidian 图谱视图可可视化关系网络。

## 创建新任务

1. 复制 `templates/task-template.md` 到 `tasks/T{编号}-{简述}.md`
2. 填写 YAML frontmatter（id、title、priority、phase 等）
3. 在 HOME.md 添加入口链接
4. 在对应路线图（maps/）中引用

## 创建新经验

1. 复制 `templates/lesson-template.md` 到 `lessons/L{编号}-{简述}.md`
2. 填写触发场景、现象、根因、方案、预防
3. 设置 category（pitfall/principle/pattern）和 phase
4. 在关联任务的 `lessons_learned` 字段中引用

## 任务状态流转

```
backlog → in_progress → review → done → cancelled
```

## 阶段映射

任务 `phase` 字段与 dev-flow 流程阶段对应：

| phase | dev-flow 阶段 |
|-------|--------------|
| discuss | 讨论阶段 — 需求澄清 |
| decide | 决策阶段 — 方案选择 |
| code | 编码阶段 — 分工执行 |
| review | 审查阶段 — 代码审查 |
| fix | 修复阶段 — 迭代修复 |
| ship | 交付阶段 — 验收发布 |

## Git 流程集成

任务模板内嵌 Git 流程触发字段（branch、pr、commits、merged、release），由 `dev-flow-git` 技能自动维护：

- **decide 完成** → 切出分支
- **code 完成** → 推送 + 创建 PR
- **review 通过** → PR approve
- **ship 确认** → merge + 打 tag + 沉淀经验
