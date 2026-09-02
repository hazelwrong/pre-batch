# GDPval Pipeline 设计

版本：v4.0

日期：2026-08-23

格式基准：`delivery2`。冲突优先级见 `policy.json`。

机器可读源只有两份：`agent_roles.json` 定义角色契约，`policy.json` 定义预算、业务阈值和发布不变量。本文解释顺序与理由，不复制每个字段的完整值。

## 0. v4 解决什么

v3 已有 artifact digest、角色隔离和 stale 传播，但存在五个结构问题：任意命令可被登记为 gate；`not_run` 没有统一阻断发布；validator 以单任务全局状态组织；T14 能看到 `design_notes`；T10 在机器契约中被误配成模型角色。v4 的目标是保留多任务、多模型和局部回流的灵活性，同时把发布门做成不可配置绕过的固定路径。

契约文件声明的是应满足的状态，不是既有任务已经达到该状态的证据。任务是否可发布只看当前 workflow、固定 validation 记录、人审记录和确定性打包结果。

## 1. 三层结构

### 1.1 批次调度

批次调度只回答“下一波哪些角色可运行”。任务之间可并行，slot 上限由 `policy.execution.concurrency_slots` 给出；同一任务仍受依赖图约束。调度不得改写角色输出或发布结论。

```bash
python3 pipeline/manage.py next workbench --slots 4 --json
```

### 1.2 单任务状态机

一条任务按下列必要顺序推进：

```text
T10 Gold 落地/登记 → 固定技术预检 → T11 → S-REF → T12 → (T13/G1 ∥ T14/G3) → T15/G4
```

角色提交绑定输入 digest。上游变化只使依赖它的后继、固定校验和人审 stale；next wave 从最早的 stale 节点恢复，不重跑仍 current 的前驱。配置绑定采用“整份 policy SHA 留作审计、角色相关 section SHA 决定 current”的双层记录，避免 `human_review` 等无关口径改动使生产角色全线重跑；相关 section 变化仍按角色依赖图向下游传播。

### 1.3 固定发布核

发布核只接受固定 validator 生成的结构化结果，不接受调用方传入 shell 命令或退出码声明。

```bash
python3 pipeline/orchestrator.py record-validation \
  workbench/<task_id> <delivery_root>
```

`record-validation` 只登记 delivery 中由固定 validator 生成的结果，不接受调用方自报 passed。检查集合、输入 snapshot 和证据 digest 都进入记录；它不是运行 validator 的任意命令入口。

## 2. 身份与模型

`T10`–`T15` 是角色号；`A-xxx` 是一次角色实例号。一个 A 号只能代表一个实际的人或模型上下文，不能换名复用。T13、T14 的重试必须使用新的 A 号和未见过答案的上下文。

| 角色 | 类型 | 档位 | 默认模型/思考 |
|---|---|---|---|
| T10 Gold 登记 | `human_registration` | L0 | 人 + 固定脚本 |
| T11 蓝图与输入设计 | judgment | L4 | GPT-5.6 Sol / high |
| T12 Prompt | judgment | L2 | GPT-5.6 Terra / medium |
| T13 冷解题 | independent judgment | L4 | GPT-5.6 Sol / high |
| T14 冷复算 | independent judgment | L3 | GPT-5.6 Terra / high；T12 后与 T13 并行 |
| T15 Rubric | judgment | L3 | GPT-5.6 Terra / high |

T10 不启动 LLM。它登记真实文件、许可、生产方式与变换记录，并通过技术预检；真实性交付无法用“再跑一次模型”修复。

## 3. 输入隔离

| 角色 | 必需可见 | 明确不可见 |
|---|---|---|
| T10 | material_pool、source_manifest、occupation_standard | prompt、rubric |
| T11 | coverage、occupation_standard、gold、gold_provenance | prompt、rubric |
| T12 | occupation_standard、task_blueprint、references | gold、design_notes、production_notes、expected_values、rubric |
| T13 | references、prompt | gold、lineage、制作记录、rubric |
| T14 | occupation_standard、references、prompt、gold、lineage_draft | design_notes、production_notes、旧 verifier 结果、rubric |
| T15 | occupation_standard、references、prompt、gold、expected_values、policy | solver_deliverables |

`required_inputs` 默认挂载；`allowed_inputs` 是许可集合；`optional_inputs` 默认不挂载。可选输入只有同时满足三件事才进入 run packet：角色声明允许、运行请求显式 opt-in、run contract 记录类别与 digest。T14 没有 `design_notes` 例外。

## 4. Completion criteria 与失败路由

每个角色只有在 `completion_criteria` 全部成立时才可提交 passed。失败不靠自然语言猜路由；runner 或固定检查写 `failure_routes[].reason_code`，调度器据此选择最早责任节点。T13 与 T14 在 T12 通过后独立并行；只有两者都 current passed 才能进入 T15。回流只使最早责任节点及其后继 stale，不重跑无关前驱。

主要路由：

| reason code | 回流 |
|---|---|
| `T10.DELIVERABLE_NOT_REAL` | 停止，人工更换真实 deliverable |
| `T11.INPUT_FACT_GAP` | T11，重建 references 及全部后继 |
| `T12.PROMPT_INCOMPLETE` | T12，再以新 A 号运行 T13/T14 |
| `T13.PROMPT_LEAK` | T12，清除泄漏并创建新冷上下文 wave |
| `T14.GOLD_MISMATCH` | T10，核实来源；受影响后继全部 stale |
| `T14.REFERENCE_FACT_GAP` | T11，修输入设计 |
| `T15.RUBRIC_STRUCTURE` | T15，仅重做 rubric 与后续检查 |
| `T15.HIDDEN_REQUIREMENT` | T12，prompt 变化使 T13–T15 stale |

未登记的 reason code 是契约错误，不得默认退回 T11，也不得继续发布。

V1/V2 整改另须遵守 [V1V2迭代整改与发布卫生.md](V1V2迭代整改与发布卫生.md)。其中“确定性 QA 工具说明”“对外交付中的内部流程残留”“真实人审状态”和“悬空声明路径”是四类不同问题，不得合并为 AI 痕迹处理。

## 5. 预算与并发

预算定义在 `policy.execution`：每角色有 `max_attempts_per_role`，每任务还有 `max_agent_runs_per_task`。失败、主动放弃和已准备后取消的 agent run 都计入总数；T10 计人工尝试但不计 agent runs。预算在创建 run 时检查：已经合法创建的最后一个 `prepared` run 必须允许执行和提交，不能因为 `used == limit` 被 planner 二次拦截。其失败后再创建新 run 才是预算耗尽，状态转 blocked，交给人决定替换材料、改任务、显式 override 或终止；不得静默加预算。更晚的同角色 current passed run 会使旧 prepared 变成 superseded，后者保留审计但不占并发 slot。

四个 slot 是全批次共享上限，不是每任务四个。调度优先填充不同任务的 ready 节点；单任务不会为了占满 slot 而越过 T11→T15 的依赖。

## 6. 两阶段、三层人审

角色生产与 rubric 完成后，先生成并验证 `review-input-v1`。该机器契约一次性绑定任务与版本、prompt/Rubric/Gold/reference/deliverable 路径及哈希、真实来源与权利边界、职业标准卡和三层审查人画像。字段缺失、真实 deliverable 与来源哈希不一致、权利边界不完整或三位画像不覆盖全部审查层时 fail-close，不生成空壳审核包。

契约通过后先冻结 `Candidate-Delivery-Package.zip`，并生成 `Phase-1-Human-Review-Kit.zip`。每份审核工作簿必须有面向当前审核人的审核摘要与文件清单，明确行业、职业、范围、真实来源、权利边界、字节数和 SHA-256。通用审查与职业专家审查可并行；每位审核人只返回一个 XLSX。职业专家在同一工作簿中完成职业映射、rubric 逐条采纳和 Gold 逐条评分，不再在 MD 与 TSV 间切换。工作簿跟随任务语言，不得因模板引入不必要的中英混排。

项目方从真实回执录入姓名、职务、带时区时间和资质状态，原始 XLSX 逐字节保留并登记 SHA-256。姓名、时间、资质不得由 pipeline 推断或补造；缺少资质证据时使用 `credential_status=not_supplied`。

两份首审回执完成后，任一 `Conditional pass`/`Fail`、不接受的职业映射、Rubric `Revise/Reject`、finding 或显式勾选的补充确认要求，均汇总为同一整改清单。finding 必须逐项记录 disposition、理由、证据文件与带时区 `closed_at`，且 `closed_at` 必须严格晚于原审核时间。不得强制审核人制造反对意见；无异议可直接通过，`N/A` 则需一句简短理由。

如整改改变了审核人已签署的项目，pipeline 按 `policy.human_review.change_impact_layers` 将任务文件和生产 artifact 的真实变化映射到受影响层，只向对应原审核人生成一份 changed-items-only 补充复核 XLSX；补签表列明触发本层复核的变更输入，未变更决定自动延续，变更的 Rubric/Gold 才重新采纳和评分。补充复核不通过时回到新一轮整改，原始回执、历史整改与每轮补签均保留。整改后运行 pre-final validation；它只允许终审层为 `not_run`。随后冻结终审包，第三位真人只返回一个 `Final-Review.xlsx`。终审时间必须严格晚于前两层、全部 `closed_at`、补充确认、pre-final validation 和终审包冻结时间；相同时间戳不构成时间差。

第一阶段审核包生成前另有两个 fail-close 入口：`real_input_and_real_deliverable` 的逐文件来源记录必须包含 URL、来源哈希、权利主体、许可和获取日期，且当前文件必须与来源哈希逐字节一致；任务范围内从真实来源脱敏或重构的 Gold 必须使用 `desensitization`，同时绑定真实来源 URL/哈希、当前字节哈希、明确转换说明、adopted source inventory 记录和经哈希校验的 lineage。任一绑定缺失或不一致即失败，`generated_deliverable` 不得借此通行。任务声明语言必须可识别，prompt 与 Rubric 的 criterion/verification 不得出现可机器判定的中英文错配。采用的 reference 不得标为 `synthetic`，脱敏或重构 reference 必须标为 `desensitization` 并登记 transformation record。这些缺口不得转嫁给审核专家。

证据绑定 review basis 与 rubric 版本；三位签署人必须不同，职业专家资质状态、逐条采纳/修改/拒绝、Gold 评分与取证位置必须齐全。首审绑定 `initial_basis`，整改用 `from_basis_digest → to_basis_digest` 保留历史；后续变更只使实际依赖它的确认、validation 与 H-REG stale。终审冻结的 `review_payload_digest` 只绑定业务内容：当前 `tasks.jsonl` 任务行、已声明 reference 和 deliverable；验证证据可在终审后更新，但任何已审业务内容变更都会使终审失效。

项目采用外置三层合并确认时，主管理进程须在基础内容、固定绑定和拟议审查结论冻结后，启动一个全新且受限上下文的子会话生成一份 Markdown 确认函。确认函固定绑定任务、当前 revision、Prompt、Reference、Deliverable、Gold/lineage、Rubric 及 A10–A12 审查记录的哈希；通审、职业专家和终审人各只在自己的一行填写姓名与 `YYYY-MM-DD` 日期。签署人必须实际审阅其绑定材料并确认自己的结论；签名不得把未审阅的草稿自动转换为人工审查。主进程须逐字节验证固定内容未改、三行签名日期完整且三位签署人不同。验签后的 Markdown 只能归档在项目根的 `专家签署函归档/`，待签目录中的返回件随即清除；它不得进入待交付任务包、`delivery/`、manifest、inventory 或任何交付校验哈希。主进程把三层专家实际确认的姓名、日期、状态和意见写入工作台 H-REG `human_review_record.json`；该 JSON 不写确认函路径/哈希，也不描述意见起草过程。

## 7. Strict final validation 与发布

人审证据写入后才运行 final validation，使人审证据也在最终 delivery snapshot 内受检。validation 分两级：

1. 每任务检查 schema、路径、哈希、隔离、真实性声明、lineage、独立复算、rubric 执行、泄漏、安全、渲染、规范与当前人审材料。
2. 批次检查 task id 唯一、bundle 隔离、manifest 全覆盖、跨任务重复和聚合状态。

发布前还运行只读的对外交付卫生审计：保留正常的确定性 QA 证据，拒绝内部整改话术和工作底稿进入 `delivery/`，核对所有声明路径实际存在，并阻止没有真实当前版人审支持的通过或发布声明。任一正文或证据修改后，必须重跑 inventory/provenance、security scans、validation 与 final checksums；受影响的人审绑定按 change-impact 规则失效并重签。

多任务 validator 为每个 task 保持独立 context，再生成批次汇总；任何任务的 `failed`、`not_run` 或 `stale` 都进入批次计数。`not_run` 表示证据缺失或工具未完成，不是豁免。final validation 记录绑定角色 artifact、rubric、人审、policy、validator 版本和 evidence digest。

```text
T10 Gold 落地/登记与固定技术预检
→ T11–T15 角色生产
→ 首审两包并行回收
→ finding 闭环 + pre-final validation
→ 第三位真人终审
→ strict final validation：failed=0、not_run=0、stale=0
→ H-REG 登记既有人审记录并绑定 validation digest
→ 冻结 snapshot
→ 构建 zip 两次且哈希一致
→ 发布
```

新式 staged workflow 的 `run.py` 直接从 review cycle 执行 H-REG；旧 workflow 才读取 `GDPVAL_HUMAN_REVIEW_ROOT` 或 `GDPVAL_HUMAN_REVIEW_RECORDS`。三份原始 XLSX、项目方录入、整改记录、终审包或 validation 任一发生变化，H-REG 都会失效。

final validation 通过后执行 H-REG：`record-human-review` 把先前已完成的人审记录登记进 workflow，并将它绑定到当前 validation digest；H-REG 不是第二次审阅。任一任务未通过时禁止部分发布。workflow 的 `release_ready` 只由当前角色、strict final validation 与 H-REG 证据派生，不能由配置、人工布尔值或命令退出码直接设置；它仍不是最终发布收据。只有设置 `GDPVAL_ARCHIVE` 的实际发布路径才会核对批次内全部 workflow，并执行两次 zip 哈希比较。

## 8. 技术预检

技术预检在 T10 尝试提交 `passed` 时由 orchestrator 固定执行，并将结构化证据写入该 run 的 `t10_preflight.json`：文件存在且非空、Office/PDF 元数据已剥离、无恶意内容、绝对/穿越路径和密钥，权利方、许可与用途字段齐全且无未解决 blocker。任一 check 失败就拒绝 T10 completion，T11 不会成为 ready；这样不会让高推理模型等待一个在入口即可发现的材料问题。

## 9. 事实边界

- `delivery2` 是格式基准，不证明新任务内容真实或当前证据有效。
- deliverable 必须是现实工作中真实交付过的文件；重新生成或换格式复现的文件须人工判定，不能只凭 `is_real_deliverable: true` 放行。
- evaluator-only 数据留在 `build/tasks/<task_id>/`，不整体挂给 agent。
- 归档脚本不是生产入口。
- JSON 契约改动会使依赖其 digest 的结果 stale；文档改动本身不制造通过证据。
