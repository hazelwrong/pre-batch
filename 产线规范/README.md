# GDPval Pipeline v4 使用说明

给接手产线的人、调度器和 agent。`agent_roles.json` 是角色契约，`policy.json` 是预算与业务口径；Python 实现必须服从两者，但文档不把契约声明冒充为已产生的运行证据。

## 标识与运行模型

- `T10`–`T15` 是稳定的**角色号**，表示责任。
- `A-xxx` 是一次具体的**角色实例号**，表示谁在某次尝试中执行。重试 T13/T14 时必须换新的 A 号和上下文。
- 批次内多任务可并行，最多使用 `policy.execution.concurrency_slots` 个 slot；单任务保持必要串行，局部失败只让依赖该产物的后继 stale。

| 角色 | 类型 | 模型档 |
|---|---|---|
| T10 Gold 登记 | 人工登记 + 固定脚本 | L0，无模型 |
| T11 蓝图与输入设计 | agent | L4，Opus/high |
| T12 Prompt | agent | L2，Sonnet/medium |
| T13 冷解题 | 独立 agent | L4，Opus/high |
| T14 冷复算 | 独立 agent | L3，Opus/medium |
| T15 Rubric | agent | L3，Opus/medium |

模型、思考档、每角色最大尝试数和每任务总 agent runs 都只在两份 JSON 契约中维护。

## 隔离纪律

`prepare` 产生的是证据包，不是 OS 沙箱；runner 只能挂载该 run 目录。

- T12 不见 gold、design_notes、production_notes、rubric、expected_values。
- T13 只见 prompt 与 references。
- T14 见 references、prompt、gold、lineage_draft，但不见 design_notes、production_notes 或旧 verifier 结果。
- `optional_inputs` 默认不挂载。只有角色契约列出、调用方显式 opt-in、run contract 记录类别与 digest 时才可见。

## 单任务运行

```bash
cd hazel-任务包/build

python3 pipeline/orchestrator.py init workbench/<task_id> <task_id>
python3 pipeline/orchestrator.py add-artifact workbench/<task_id> \
  coverage=<path> occupation_standard=<path> material_pool=<path> source_manifest=<path>

python3 pipeline/orchestrator.py prepare workbench/<task_id> gold_curator \
  --agent-id A-001 --context-id ctx-a-001
# 可选输入逐项显式登记，例如：--include material_pool
# runner 只挂 runs/<run_id>/，产物写入 output/<category>/
# T10 提交 passed 时会先生成 t10_preflight.json，预检失败不会进入 T11
python3 pipeline/orchestrator.py submit workbench/<task_id> <run_id> passed
```

提交只表示本角色契约满足，不等于可以发布。失败必须使用 `agent_roles.json.failure_routes[].reason_code` 路由；不得把 stale 手工改回 passed。

## 批次与 next wave

```bash
# 查看 workbench 根下当前可运行的下一波；调度上限固定取 4
python3 pipeline/manage.py next workbench --slots 4 --json
```

`next` 是只读 planner：只列出依赖已满足的角色实例，不替人启动或提交 agent。任务之间可并行；同一任务在 T12 通过后，T13 冷解题与 T14 冷复算也可并行，T15 等两者都通过后再运行。一次回流只重跑最早责任节点及其后继，不重跑无关 current 前驱。触达尝试或总 run 预算时停止并升级给人。已经合法创建的 `prepared` run 是本次预算内的最后一次尝试，即使计数刚好达到上限也必须允许执行和提交；预算只阻止创建下一次 run。若同角色已有更晚的 current passed run，旧 prepared 仅记为 superseded，不占 slot、不阻断后继。

run 同时记录整份 policy SHA（审计）和角色相关 policy scope SHA（current 判定）。例如只改 `human_review` 不会使 T10–T15 stale；改 `reference_files` 会使 T11 及依赖它的后继 stale。迁移旧 run 时必须提供上一版 policy 并显式声明变更 section，命令会先验证没有未声明差异：

```bash
python3 pipeline/orchestrator.py migrate-policy-scope \
  workbench/<task_id> <previous-policy.json> \
  --changed-section human_review
```

## 固定 validation 登记

```bash
python3 pipeline/orchestrator.py record-validation \
  workbench/<task_id> <delivery_root>
python3 pipeline/orchestrator.py record-human-review \
  workbench/<task_id> <human_review_record.json>
```

`record-validation` 是发布流程唯一认可的确定性校验登记入口：它登记固定 final validator 的结果和证据 digest。旧式“传入任意命令、退出码 0 即通过”的 gate 不构成发布证据，也不写入操作流程。

顺序固定为：T10 Gold 落地与固定技术预检 → T11–T15 角色生产 → 通用审查/职业专家审查 → 意见闭环 → 第三人最终终审 → 覆盖该证据的 strict final validation → H-REG 将人审记录登记进 workflow 并绑定 validation digest → 确定性打包。终审时间必须严格晚于前两层与全部闭环时间；H-REG 不是第二次审阅。发布前必须同时满足：

```text
failed = 0
not_run = 0
stale = 0
gold 完整人工评分达到阈值
三层不同签署人、资质状态如实登记、实质驳回记录齐全
哈希自检无不符
连续两次构建 zip 哈希一致
```

任一任务不满足时，整个批次不可部分发布。

实际写发布 archive 时由 `run.py` 逐任务校验、登记 workflow、核对全部任务的 `release_ready`，再连续打包两次：

```bash
GDPVAL_TASK_ID=<task_id>[,<task_id>...] \
GDPVAL_HUMAN_REVIEW_ROOT=<root-containing-task-subdirs> \
GDPVAL_ARCHIVE=<发布zip路径> python3 pipeline/run.py
```

`GDPVAL_HUMAN_REVIEW_ROOT/<task_id>/human_review_record.json` 必须是三层人审已完成的正式记录；`run.py` 会在每个任务的 final validation 之后调用 H-REG，重新绑定本次 validation digest。也可以用 `GDPVAL_HUMAN_REVIEW_RECORDS` 传入 JSON 对象，将 task id 映射到各自记录路径。未提供记录时归档会明确跳过，不会把旧的人审绑定误当成当前证据。

不设置 `GDPVAL_ARCHIVE` 只生成并检查 unpacked delivery，不产生发布包。

## 任务数据与真实性交付

`build/tasks/<task_id>/` 是 evaluator-only 数据，永不作为 Agent 可见输入整体挂载。rubric 数字只能来自 T14 的 `expected_values`；无 `check` 的条目必须有 `verification`。每个 agent 提交前必须做完整自检，首稿质量目标为 80 分；这是减少往返的生产目标，不是替代 strict validation 的放行条件。

deliverable 必须是现实工作中真实交付过的文件。reference 可据真实材料脱敏重构，并须如实登记模型参与；把原交付物重新生成成另一格式，不因内容相似而自动成为真实 deliverable。

## 文档索引

- `pipeline设计.md`：v4 顺序、不变量、回流和实现状态。
- `agent_roles.json`：角色输入、输出、completion criteria、失败 reason code。
- `policy.json`：业务阈值、预算、并发和严格发布条件。
- `flowchart.mmd`：多任务并行与单任务串行图。
- `../../甲方要求/甲方口径与踩坑清单/`：上位硬规范。

`pipeline/archive/` 仅供追溯；其中的 gold 生成器不属于可执行产线。
