---
type: task
id: "{{id}}"
title: "{{title}}"
status: backlog        # backlog → in_progress → review → done → cancelled
priority: P2           # P0 / P1 / P2
phase: "{{phase}}"     # discuss / decide / code / review / fix / ship
assignee: ""           # hermes / claude / both / user
project: ""            # 关联项目
created: "{{date}}"
updated: "{{date}}"

# 关联任务
blocked_by: []         # [[任务ID]] 被阻塞
blocks: []             # [[任务ID]] 阻塞他人
related: []            # [[笔记ID]] 关联资料/决策
lessons_learned: []    # [[lesson-id]] 本任务沉淀的经验（ship阶段填写）
tags: []

# Git流程（dev-flow-git触发）
branch: ""             # feat/T003-short-desc（decide完成后切出）
pr: ""                 # PR编号或链接（code完成后创建）
commits: []            # 关联commit列表
merged: false          # ship阶段merge后标true
release: ""            # 版本tag，如 v1.0.0
---

# {{title}}

## 目标

## 验收标准

## Git流程触发

| 阶段 | Git操作 | 状态 |
|------|---------|------|
| decide完成 | `git checkout -b {{branch}} main` | ☐ |
| code完成 | `git push origin {{branch}}` + 创建PR | ☐ |
| fix完成 | fix追加commit + `git push` | ☐ |
| review通过 | PR approve | ☐ |
| ship确认 | PR merge + 打tag `{{release}}` | ☐ |

## 关联资料

## 执行记录

| 日期 | 阶段 | summary | 事件 |
|------|------|---------|------|
| {{date}} | - | - | 任务创建 |
