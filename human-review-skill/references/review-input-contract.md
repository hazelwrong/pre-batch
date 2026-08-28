# 人工审查输入材料契约

这份契约定义任务包交给人工审查流程时，上游必须提供的材料。它适用于通用审查、职业专家审查、终审、整改后复核和整改后哈希补签。

机器执行以同目录的 `review-input.schema.json` 和运行时生成的
`review_input_manifest.json` 为准。Markdown 用于解释；缺少机器契约必填字段时，
pipeline 必须在生成审核包之前失败，不能把证据缺口留给专家发现。

## 1. 两种合法输入形态

优先使用 pipeline 原生任务目录；已有历史任务包可以使用复核包目录。调用 skill 时先识别形态，再把它们归一到同一份当前版本清单。

### A. Pipeline 原生任务目录

```text
tasks/<task_id>/
├── task_meta.json
├── prompt.md
├── rubric.json
├── rubric_pretty.txt
├── provenance.json
├── gold_provenance.json
├── source_inventory.json
└── expert_profiles.json

delivery/<task_id>/
├── tasks.jsonl
├── reference_files/
├── deliverable_files/
├── manifests/
└── validation_evidence/
```

`task_meta.json` 至少包含 `task_id`、任务名称、`sector`、`occupation`、`language`、当前版本、`rubric_version` 和 `item_codes`。`rubric.json` 是结构化 Rubric，`prompt.md` 是当前题面，`rubric_pretty.txt` 是供审查人阅读的 Rubric 文本。`tasks.jsonl` 必须能唯一定位该任务，并与当前 prompt、Rubric、Gold、reference 和 deliverable 文件一致。

### B. 历史/项目方复核包

```text
<task-root>/
├── expert_profiles.json
├── 原始材料/ 或 material_pool/
├── 交付包/ 或 delivery/
├── 复核包/
│   ├── 发送说明.md 或 00-发送与回收说明.md
│   ├── 给-通用审查/
│   │   ├── 00-先读这个.md
│   │   ├── 通用审查表.md 或 *.xlsx
│   │   └── 任务材料/
│   ├── 给-职业专家/
│   │   ├── 00-先读这个.md
│   │   ├── 职业专家审查表.md 或 *.xlsx
│   │   └── 任务材料/
│   └── 给-终审/
│       ├── 00-先读这个.md
│       ├── 终审表.md 或 *.xlsx
│       └── 任务材料/
└── 整改后补充复核/（有整改时）
```

历史目录中可以出现中文文件名、ZIP、DOCX、XLSX、Markdown、JSON、CSV、TSV 和官方 PDF。PDF 可以作为待审的真实来源或交付物；生成新的 pipeline `reference_files` 时仍遵守 policy 中的格式限制。

## 2. 上游必须登记的内容

### 任务和版本

- 唯一 `task_id`、任务包名称、行业、职业、语言。
- 当前 prompt、Rubric、Gold、reference、交付包及来源证据的路径。
- 当前版本的文件清单；对每个文件记录相对路径、字节数和 SHA-256。
- `basis_digest`、候选 ZIP 路径和 SHA-256、首审核包 SHA-256；明确它是正式发布包还是受限内部候选包。
- 每层回执必须绑定其收到的 `review_basis_digest`。终审包另记录 post-remediation basis、稳定业务 payload digest 和冻结时间。

### 真实材料与权限

- `material_pool` 中的真实交付物、配套输入和 reference。
- 每个真实交付物在 `gold_provenance.real_deliverable_files` 中逐文件登记 `path`（同名文件时必填）、`source_url`、`source_sha256`、`source_type`、`rights_holder`、`license` 和 `acquired_at`；pipeline 复算当前文件 SHA-256。
- `deliverable_files` 必须逐字节等于真实来源，不允许重构、脱敏、转格式或清除元数据。经过脱敏重构的 reference 记录 `source_type=desensitization` 和 transformation record，不能把它冒充成原文件。
- 已采用的 reference 不得标为 `synthetic`；只要发生过脱敏或重构，就必须同时使用 `source_type=desensitization` 并填写 `transformation_record`。
- 公开发布、甲方内部使用、第三方再分发和转授权分别记录，不用一个模糊的 `allowed` 概括四种范围。
- 受限使用授权必须记录真实决策人、实际角色、带时区决策时间、task ID、授权范围、证据文件与 SHA-256；任务级授权和例外不得扩展成全局 policy。

### 职业标准与专家画像

- 一份职业标准卡：岗位日常交付、行业口径、单位、关键指标、质量标准和常见错误。
- 每个任务三条专家画像，分别对应通用审查、职业专家审查和终审：

```json
{
  "expert_id": "E01",
  "alias": "规划用姓名",
  "expert_role": "通用审查",
  "review_layer": "general_review",
  "required_industry": "需要的行业背景",
  "required_occupation": "需要的职业背景",
  "review_scope": "只审核哪些内容",
  "expert_profile": "典型岗位、经验和审查视角",
  "strengths": ["跨文件一致性", "证据定位"],
  "first_thought": "看到材料后首先核对什么"
}
```

画像只用于规划审查重点和招募对应行业/职业的审核人，不是资质证明，也不能直接代替真人签署姓名。真实审核人的职务、姓名和资质状态必须另外记录。画像不要求专家必须提出反对意见；真实的全量采纳可以原样保留。

## 3. 每层审核表的最小字段

### 通用审查

至少能够填写：任务/版本识别、文件清单与打开状态、reference 与 Gold 隔离、prompt 与 Rubric 一致性、证据定位、隐私与本地路径检查、渲染检查、finding（严重度、位置、建议）、结论和实质意见。

检查项可以选择 `N/A`，但必须写一句原因；不得用无理由 `N/A` 批量跳过审查。

### 职业专家审查

除通用字段外，至少能够填写：职业映射与边界、Rubric 逐条 `adopt/revise/reject`、每条 Gold 得分、证据位置、短板、专业 finding、Rubric 版本和结论。

### 终审

至少能够填写：前两层意见和整改闭环、当前版本哈希、法定权限/行政程序/发布措辞、授权和再分发边界、发布状态、剩余风险、最终结论和实质意见。

### 项目方独立登记字段

不要把身份和时间伪装成审核人表内意见。项目方应单独保存一份转录 JSON，字段仅限：

```json
{
  "task_id": "<uuid>",
  "layer": "general_review|occupational_expert_review|final_review",
  "reviewer_id": "实际签署姓名",
  "reviewer_title": "实际职务",
  "reviewed_at": "2026-08-28T10:00:00+08:00",
  "credential_status": "not_supplied"
}
```

原始返回 XLSX 必须原样保存并计算 SHA-256；项目方转录不能加入结论、finding、逐条评分、资质附件或来源声明。没有资质附件时固定登记 `not_supplied`，不推定证书、学历、年限或单位。

## 4. 整改后和哈希补签输入

整改后复核必须同时提供：

- 整改记录，说明从哪个旧版本变更到哪个新版本。
- 变更后的 prompt、Rubric、tasks、Gold、reference 和 ZIP 路径。
- 每个目标文件的预期 SHA-256，或允许工具重新计算并回填。
- 哪些条目需要重新评分，哪些条目只是哈希绑定确认；未变化条目沿用原审查，不要求专家重填。
- 每项整改的来源层、原意见、处置、证据文件、关闭时间，以及变更前后 basis/file SHA-256。
- 受限任务的任务级授权文本，以及它绑定的 task ID、版本、证据文件与当前哈希。

哈希补签不能只核对表内字符串。必须重新计算磁盘文件，检查 ZIP 内关键文件字节一致，并运行 ZIP CRC 校验。补签时间可以短于完整审查，但表内应明确它只是当前版本绑定确认，不替代完整审查。

若原审核人勾选 `Requires confirmation=Yes`、给出 `Conditional pass/Fail`、职业映射为有条件/拒绝，或 Rubric 行为 `Revise/Reject`，整改后只向受影响的原审核人生成一份 changed-items-only XLSX。专家只确认变化项；发生变化的 Rubric/Gold 行才重新采纳和评分。补签仍有 Issue 时进入下一轮整改和精简补签，不重跑无关层。

## 5. 输入不完整时的处理

- 缺少 task_id、当前版本或目标文件路径：暂停该层审查并列出缺口。
- 缺少真实来源、权利或脱敏说明：记录为证据缺口，不补造事实。
- 缺少专家资质附件：登记 `not_supplied`，不自行推定；若缺少真实签署姓名或实质意见，则不能完成该层签署。
- 缺少历史审核表但有当前版本材料：可以开展新的当前版本审查，但不得伪造历史结论。
- 发现 prompt、Rubric、Gold、manifest 或 ZIP 不一致：先标记当前版本 finding，再决定是否进入整改，不得直接声称发布就绪。

## 6. 完成标准

准备状态分两级，不能一次性生成三个审核包：

1. `phase1_ready`：任务和当前版本唯一；文件、来源、权利、职业标准卡和三条画像齐全；候选包冻结；同时生成通审与职业专家包。
2. `final_ready`：两份首审回执已录入，全部整改和必要的 changed-items-only 补签已关闭，pre-final validation 通过，随后才冻结终审包。

终审回执必须严格晚于两份首审、全部整改关闭、补签回执、pre-final validation 和终审包冻结时间。最终 strict validation 只比较冻结的业务 payload，允许 validator 写入新的 nonce/证据，但不允许 prompt、Rubric、Gold、reference 或 deliverable 任一字节变化。

生成第一阶段审核包前，pipeline 还必须确认声明语言可识别，且 prompt 与每条 Rubric 的 criterion/verification 未出现可判定的中英文错配。整改时按 policy 的 `change_impact_layers` 计算实际受影响层；补签表会列出触发本层复核的已变更输入，未受影响层不重审。
