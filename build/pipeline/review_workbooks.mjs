import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const args = process.argv.slice(2);
const valueAfter = (name) => {
  const index = args.indexOf(name);
  if (index < 0 || index + 1 >= args.length) {
    throw new Error(`missing ${name}`);
  }
  return args[index + 1];
};

const configPath = valueAfter("--config");
const outputPath = valueAfter("--output");
const config = JSON.parse(await fs.readFile(configPath, "utf8"));
const locale = config.locale === "zh" ? "zh" : "en";
const ui = config.ui;
const TEXT = {
  en: {
    sector: "Sector", occupation: "Occupation", taskLanguage: "Task language",
    checklist: "Checklist", check: "Check", decision: "Decision", comment: "Comment if needed",
    findingsTitle: "Findings", findingId: "Finding ID", severity: "Severity",
    findingNote: "No finding is required. Complete one row only when a checklist item is marked Issue.",
    location: "Location", issue: "Issue", recommendation: "Recommendation",
    requiresConfirmation: "Requires confirmation", findingsEntered: "Findings entered",
    blockingFindings: "Blocking findings", findingsComplete: "Findings complete",
    completeFinding: "Complete location, issue and recommendation", ok: "OK",
    returnStatus: "Return status", projectIdentity: "Project-side identity entry",
    identityNote: "Completed after receipt; not reviewer-authored", readyToReturn: "Ready to return",
    ready: "READY", incomplete: "INCOMPLETE", occupationMapping: "Occupation mapping",
    professionalChecks: "Professional checks", rubricTitle: "Rubric adoption and Gold scoring",
    code: "Code", rubricItemId: "Rubric item ID", required: "Required", max: "Max",
    criterion: "Criterion", verification: "Verification / machine result", adoption: "Adoption",
    reasonRevision: "Reason or revision", goldScore: "Gold score",
    goldEvidence: "Gold evidence or reason", machine: "Machine", items: "Items",
    missingDecisions: "Missing decisions/scores", sequence: "Sequence and frozen evidence",
    event: "Event", timestampDigest: "Timestamp / digest", finalChecks: "Final checks",
    closureTitle: "Finding closure", sourceLayer: "Source layer", disposition: "Disposition",
    closedAt: "Closed at", rationale: "Rationale", evidenceSha: "Evidence SHA-256",
    finalCheck: "Final check", none: "None", changedOnly: "Changed items only",
    requirementId: "Requirement ID", type: "Type", originalRequest: "Original request",
    remediationEvidence: "Remediation and evidence", issueComment: "Comment if Issue",
    rubricAdoption: "Rubric adoption", reviewBrief: "Review brief", requiredIndustry: "Required industry",
    requiredOccupation: "Required occupation", reviewScope: "Review scope",
    typicalPerspective: "Typical perspective", strengths: "Strengths", firstCheck: "First check",
    rightsHolder: "Rights holder", licence: "Licence", internalUse: "Internal use",
    publicRelease: "Public release", redistribution: "Third-party redistribution",
    sublicensing: "Sublicensing", authenticSources: "Authentic sources",
    fileSourceId: "File / Source ID", sourceUrl: "Source URL",
    sourceShaType: "Source SHA-256 / Type", licenceNotes: "Licence / Notes",
    inventoryTitle: "Current file inventory", scope: "Scope", path: "Path", bytes: "Bytes",
  },
  zh: {
    sector: "行业", occupation: "职业", taskLanguage: "任务语言",
    checklist: "审查项", check: "核对内容", decision: "判断", comment: "必要时填写意见",
    findingsTitle: "问题记录", findingId: "问题编号", severity: "严重度",
    findingNote: "不要求提出问题；仅当审查项选择“有问题”时填写一行。",
    location: "位置", issue: "问题", recommendation: "整改建议",
    requiresConfirmation: "需要补充确认", findingsEntered: "已登记问题数",
    blockingFindings: "阻断或重大问题数", findingsComplete: "问题记录完整性",
    completeFinding: "请补全位置、问题和整改建议", ok: "完整",
    returnStatus: "回执状态", projectIdentity: "项目侧身份录入",
    identityNote: "收到回执后由项目侧补录，不由审核者填写", readyToReturn: "可否返回",
    ready: "可返回", incomplete: "未完成", occupationMapping: "职业映射",
    professionalChecks: "专业核对", rubricTitle: "评分标准采纳与 Gold 评分",
    code: "代码", rubricItemId: "评分项 ID", required: "是否必需", max: "最高分",
    criterion: "评分标准", verification: "验证方式 / 机器结果", adoption: "采纳判断",
    reasonRevision: "理由或修改建议", goldScore: "Gold 得分",
    goldEvidence: "Gold 证据或理由", machine: "机器结果", items: "项目数",
    missingDecisions: "缺少判断或评分", sequence: "时序与冻结证据",
    event: "事件", timestampDigest: "时间 / 摘要", finalChecks: "最终核对",
    closureTitle: "问题闭环", sourceLayer: "来源层", disposition: "处理结果",
    closedAt: "关闭时间", rationale: "处理说明", evidenceSha: "证据 SHA-256",
    finalCheck: "最终核对", none: "无", changedOnly: "仅复核变更项",
    requirementId: "整改项编号", type: "类型", originalRequest: "原始要求",
    remediationEvidence: "整改与证据", issueComment: "有问题时填写意见",
    rubricAdoption: "评分标准采纳", reviewBrief: "审核摘要", requiredIndustry: "所需行业",
    requiredOccupation: "所需职业", reviewScope: "审核范围",
    typicalPerspective: "典型视角", strengths: "关注优势", firstCheck: "首要核对",
    rightsHolder: "权利人", licence: "许可", internalUse: "内部使用",
    publicRelease: "公开发布", redistribution: "第三方再分发",
    sublicensing: "转授权", authenticSources: "真实来源",
    fileSourceId: "文件 / 来源 ID", sourceUrl: "来源网址",
    sourceShaType: "来源 SHA-256 / 类型", licenceNotes: "许可 / 备注",
    inventoryTitle: "当前文件清单", scope: "范围", path: "路径", bytes: "字节数",
  },
}[locale];
const choice = ui.choices;
const labels = ui.labels;
const sheets = ui.sheet_names;

const COLORS = {
  navy: "#17324D",
  teal: "#197278",
  paleTeal: "#DCEFED",
  yellow: "#FFF2CC",
  paleRed: "#FCE8E6",
  paleGreen: "#E2F0D9",
  grey: "#E7EBEF",
  text: "#1F2933",
  white: "#FFFFFF",
  line: "#B8C2CC",
};

const workbook = Workbook.create();
workbook.comments.setSelf({ displayName: "GDPval Pipeline" });

function displayLiteral(value) {
  if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}T/.test(value)) {
    return `ISO-8601 ${value}`;
  }
  return value;
}

function setTitle(sheet, range, title) {
  range.merge();
  range.values = [[title]];
  range.format = {
    fill: COLORS.navy,
    font: { bold: true, color: COLORS.white, size: 16 },
    verticalAlignment: "center",
  };
  range.format.rowHeight = 30;
}

function section(sheet, row, lastCol, title) {
  const range = sheet.getRange(`A${row}:${lastCol}${row}`);
  range.merge();
  range.values = [[title]];
  range.format = {
    fill: COLORS.teal,
    font: { bold: true, color: COLORS.white },
    verticalAlignment: "center",
  };
  range.format.rowHeight = 22;
}

function header(range) {
  range.format = {
    fill: COLORS.grey,
    font: { bold: true, color: COLORS.text },
    wrapText: true,
    verticalAlignment: "center",
    borders: { preset: "outside", style: "thin", color: COLORS.line },
  };
}

function editable(range) {
  range.format = {
    fill: COLORS.yellow,
    font: { color: COLORS.text },
    wrapText: true,
    verticalAlignment: "top",
    borders: { preset: "outside", style: "thin", color: COLORS.line },
  };
}

function body(range) {
  range.format = {
    font: { color: COLORS.text },
    wrapText: true,
    verticalAlignment: "top",
    borders: {
      insideHorizontal: { style: "thin", color: "#D9E0E6" },
      bottom: { style: "thin", color: COLORS.line },
    },
  };
}

function addMetadata(sheet, lastCol = "H") {
  setTitle(sheet, sheet.getRange(`A1:${lastCol}1`), config.title);
  const rows = [
    [labels.task_id, config.task.task_id],
    [TEXT.sector, config.task.sector],
    [TEXT.occupation, config.task.occupation],
    [TEXT.taskLanguage, config.task.language],
    [labels.rubric_version, config.task.rubric_version],
    [labels.candidate_sha256, config.task.candidate_sha256],
  ];
  sheet.getRange("A3:B8").values = rows;
  sheet.getRange("A3:A8").format = { font: { bold: true }, fill: COLORS.paleTeal };
  sheet.getRange(`B3:${lastCol}8`).format = { wrapText: true };
  for (let row = 3; row <= 8; row += 1) {
    sheet.getRange(`B${row}:${lastCol}${row}`).merge();
  }
  sheet.getRange(`B8:${lastCol}8`).format.numberFormat = "@";
  sheet.getRange(`A3:${lastCol}8`).format.borders = {
    preset: "outside", style: "thin", color: COLORS.line,
  };
}

function addFindingsSheet() {
  const sheet = workbook.worksheets.add(sheets.findings);
  sheet.showGridLines = false;
  setTitle(sheet, sheet.getRange("A1:F1"), TEXT.findingsTitle);
  sheet.getRange("A2:F2").merge();
  sheet.getRange("A2:F2").values = [[TEXT.findingNote]];
  sheet.getRange("A2:F2").format = {
    fill: COLORS.paleGreen, font: { color: COLORS.text }, wrapText: true,
  };
  sheet.getRange("A3:F3").values = [[
    TEXT.findingId, TEXT.severity, TEXT.location, TEXT.issue, TEXT.recommendation, TEXT.requiresConfirmation,
  ]];
  header(sheet.getRange("A3:F3"));
  const rows = Array.from({ length: 20 }, (_, index) => [
    `${config.layer === "general_review" ? "G" : config.layer === "occupational_expert_review" ? "E" : "F"}-F${String(index + 1).padStart(2, "0")}`,
    "", "", "", "", choice.no,
  ]);
  sheet.getRange("A4:F23").values = rows;
  body(sheet.getRange("A4:F23"));
  editable(sheet.getRange("B4:F23"));
  sheet.getRange("B4:B23").dataValidation = {
    rule: { type: "list", values: [choice.blocker, choice.major, choice.minor] },
  };
  sheet.getRange("F4:F23").dataValidation = {
    rule: { type: "list", values: [choice.yes, choice.no] },
  };
  sheet.getRange("A25:B27").values = [
    [TEXT.findingsEntered, null],
    [TEXT.blockingFindings, null],
    [TEXT.findingsComplete, null],
  ];
  sheet.getRange("B25").formulas = [["=COUNTIF(B4:B23,\"?*\")"]];
  sheet.getRange("B26").formulas = [[`=COUNTIF(B4:B23,"${choice.blocker}")+COUNTIF(B4:B23,"${choice.major}")`]];
  sheet.getRange("B27").formulas = [[
    `=IF(AND(COUNTIF(B4:B23,"?*")=COUNTIF(C4:C23,"?*"),COUNTIF(B4:B23,"?*")=COUNTIF(D4:D23,"?*"),COUNTIF(B4:B23,"?*")=COUNTIF(E4:E23,"?*")),"${TEXT.ok}","${TEXT.completeFinding}")`,
  ]];
  sheet.getRange("A25:A27").format = { fill: COLORS.paleTeal, font: { bold: true } };
  sheet.getRange("A25:B27").format.borders = { preset: "outside", style: "thin", color: COLORS.line };
  sheet.freezePanes.freezeRows(3);
  sheet.getRange("A:A").format.columnWidth = 14;
  sheet.getRange("B:B").format.columnWidth = 12;
  sheet.getRange("C:C").format.columnWidth = 24;
  sheet.getRange("D:E").format.columnWidth = 42;
  sheet.getRange("F:F").format.columnWidth = 20;
  return sheet;
}

function addReturnStatus(sheet, row, decisionCell, opinionCell, checklistRange, extraCondition = "TRUE") {
  section(sheet, row, "H", TEXT.returnStatus);
  sheet.getRange(`A${row + 1}:B${row + 4}`).values = [
    [labels.conclusion, ""],
    [labels.opinion, ""],
    [TEXT.projectIdentity, TEXT.identityNote],
    [TEXT.readyToReturn, ""],
  ];
  editable(sheet.getRange(`B${row + 1}:H${row + 2}`));
  sheet.getRange(`B${row + 1}:H${row + 1}`).merge();
  sheet.getRange(`B${row + 2}:H${row + 2}`).merge();
  sheet.getRange(`B${row + 3}:H${row + 3}`).merge();
  sheet.getRange(`B${row + 4}:H${row + 4}`).merge();
  sheet.getRange(decisionCell).dataValidation = {
    rule: { type: "list", values: [choice.pass, choice.conditional_pass, choice.fail] },
  };
  sheet.getRange(`B${row + 4}`).formulas = [[
    `=IF(AND(COUNTBLANK(${checklistRange})=0,${decisionCell}<>"",${opinionCell}<>"",${extraCondition}),"${TEXT.ready}","${TEXT.incomplete}")`,
  ]];
  sheet.getRange(`B${row + 4}:H${row + 4}`).format = {
    fill: COLORS.paleGreen, font: { bold: true, color: COLORS.text },
  };
}

function buildGeneral() {
  const sheet = workbook.worksheets.add(sheets.general_review);
  sheet.showGridLines = false;
  addMetadata(sheet);
  section(sheet, 10, "H", TEXT.checklist);
  sheet.getRange("A11:D11").values = [[labels.id, TEXT.check, TEXT.decision, TEXT.comment]];
  header(sheet.getRange("A11:D11"));
  const checks = config.checklist.map((item) => [item.id, item.text, "", ""]);
  const last = 11 + checks.length;
  sheet.getRange(`A12:D${last}`).values = checks;
  body(sheet.getRange(`A12:D${last}`));
  editable(sheet.getRange(`C12:D${last}`));
  sheet.getRange(`C12:C${last}`).dataValidation = {
    rule: { type: "list", values: [choice.pass, choice.issue, choice.na] },
  };
  addReturnStatus(
    sheet, last + 2, `B${last + 3}`, `B${last + 4}`, `C12:C${last}`,
    `AND('${sheets.findings}'!B27="${TEXT.ok}",'${sheets.findings}'!B25=COUNTIF(C12:C${last},"${choice.issue}"))`,
  );
  sheet.freezePanes.freezeRows(11);
  sheet.getRange(`A1:A${last + 6}`).format.columnWidth = 24;
  sheet.getRange(`B1:B${last + 6}`).format.columnWidth = 66;
  sheet.getRange(`C1:C${last + 6}`).format.columnWidth = 16;
  sheet.getRange(`D1:H${last + 6}`).format.columnWidth = 24;
  addFindingsSheet();
}

function buildOccupational() {
  const sheet = workbook.worksheets.add(sheets.occupational_expert_review);
  sheet.showGridLines = false;
  addMetadata(sheet);
  section(sheet, 10, "H", TEXT.occupationMapping);
  sheet.getRange("A11:B14").values = [
    [labels.mapping, config.mapping.proposed],
    [labels.boundary, config.mapping.boundary],
    [labels.decision, ""],
    [labels.mapping_reason, ""],
  ];
  sheet.getRange("B11:H12").format = { wrapText: true };
  for (let row = 11; row <= 14; row += 1) sheet.getRange(`B${row}:H${row}`).merge();
  editable(sheet.getRange("B13:H14"));
  sheet.getRange("B13").dataValidation = {
    rule: { type: "list", values: [choice.accept, choice.conditional_accept, choice.reject] },
  };
  section(sheet, 16, "H", TEXT.professionalChecks);
  sheet.getRange("A17:D17").values = [[labels.id, TEXT.check, TEXT.decision, TEXT.comment]];
  header(sheet.getRange("A17:D17"));
  const checks = config.checklist.map((item) => [item.id, item.text, "", ""]);
  const last = 17 + checks.length;
  sheet.getRange(`A18:D${last}`).values = checks;
  body(sheet.getRange(`A18:D${last}`));
  editable(sheet.getRange(`C18:D${last}`));
  sheet.getRange(`C18:C${last}`).dataValidation = {
    rule: { type: "list", values: [choice.pass, choice.issue, choice.na] },
  };
  addReturnStatus(
    sheet, last + 2, `B${last + 3}`, `B${last + 4}`, `C18:C${last}`,
    `AND(B13<>"",B14<>"",'${sheets.findings}'!B27="${TEXT.ok}",'${sheets.findings}'!B25=COUNTIF(C18:C${last},"${choice.issue}"),'${sheets.rubric_gold}'!B${config.rubrics.length + 11}="${TEXT.ready}")`,
  );
  sheet.freezePanes.freezeRows(17);
  sheet.getRange(`A1:A${last + 6}`).format.columnWidth = 24;
  sheet.getRange(`B1:B${last + 6}`).format.columnWidth = 66;
  sheet.getRange(`C1:C${last + 6}`).format.columnWidth = 18;
  sheet.getRange(`D1:H${last + 6}`).format.columnWidth = 24;

  const rubric = workbook.worksheets.add(sheets.rubric_gold);
  rubric.showGridLines = false;
  setTitle(rubric, rubric.getRange("A1:J1"), TEXT.rubricTitle);
  rubric.getRange("A3:J3").values = [[
    TEXT.code, TEXT.rubricItemId, TEXT.required, TEXT.max, TEXT.criterion,
    TEXT.verification, TEXT.adoption, TEXT.reasonRevision,
    TEXT.goldScore, TEXT.goldEvidence,
  ]];
  header(rubric.getRange("A3:J3"));
  const rows = config.rubrics.map((item) => [
    item.code, item.rubric_item_id, item.required, item.max_score, item.criterion,
    `${item.verification || ""}${item.machine_result ? `\n${TEXT.machine}: ${item.machine_result}` : ""}`,
    "", "", null, "",
  ]);
  const rubricLast = 3 + rows.length;
  rubric.getRange(`A4:J${rubricLast}`).values = rows;
  body(rubric.getRange(`A4:J${rubricLast}`));
  editable(rubric.getRange(`G4:J${rubricLast}`));
  rubric.getRange(`G4:G${rubricLast}`).dataValidation = {
    rule: { type: "list", values: [choice.adopt, choice.revise, choice.reject] },
  };
  rubric.getRange(`I4:I${rubricLast}`).dataValidation = {
    rule: { type: "custom", formula1: "=AND(ISNUMBER(I4),I4=INT(I4),I4>=0,I4<=D4)" },
  };
  const summaryRow = rubricLast + 2;
  rubric.getRange(`A${summaryRow}:B${summaryRow + 6}`).values = [
    [TEXT.items, null], [choice.adopt, null], [choice.revise, null], [choice.reject, null],
    [locale === "en" ? "Gold total" : "Gold 总分", null], [TEXT.missingDecisions, null], [TEXT.readyToReturn, null],
  ];
  rubric.getRange(`B${summaryRow}`).formulas = [[`=COUNTA(A4:A${rubricLast})`]];
  rubric.getRange(`B${summaryRow + 1}`).formulas = [[`=COUNTIF(G4:G${rubricLast},"${choice.adopt}")`]];
  rubric.getRange(`B${summaryRow + 2}`).formulas = [[`=COUNTIF(G4:G${rubricLast},"${choice.revise}")`]];
  rubric.getRange(`B${summaryRow + 3}`).formulas = [[`=COUNTIF(G4:G${rubricLast},"${choice.reject}")`]];
  rubric.getRange(`B${summaryRow + 4}`).formulas = [[`=SUM(I4:I${rubricLast})`]];
  rubric.getRange(`B${summaryRow + 5}`).formulas = [[
    `=COUNTBLANK(G4:G${rubricLast})+COUNTBLANK(I4:I${rubricLast})+COUNTIFS(G4:G${rubricLast},"<>${choice.adopt}",H4:H${rubricLast},"")+COUNTIF(J4:J${rubricLast},"")+SUMPRODUCT(--(I4:I${rubricLast}>D4:D${rubricLast}))`,
  ]];
  rubric.getRange(`B${summaryRow + 6}`).formulas = [[
    `=IF(AND(B${summaryRow + 5}=0,B${summaryRow + 4}<=SUM(D4:D${rubricLast})),"${TEXT.ready}","${TEXT.incomplete}")`,
  ]];
  rubric.getRange(`A${summaryRow}:A${summaryRow + 6}`).format = { fill: COLORS.paleTeal, font: { bold: true } };
  rubric.getRange(`A${summaryRow}:B${summaryRow + 6}`).format.borders = { preset: "outside", style: "thin", color: COLORS.line };
  rubric.getRange(`A1:A${summaryRow + 6}`).format.columnWidth = 10;
  rubric.getRange(`B1:B${summaryRow + 6}`).format.columnWidth = 38;
  rubric.getRange(`C1:C${summaryRow + 6}`).format.columnWidth = 11;
  rubric.getRange(`D1:D${summaryRow + 6}`).format.columnWidth = 9;
  rubric.getRange(`E1:E${summaryRow + 6}`).format.columnWidth = 62;
  rubric.getRange(`F1:F${summaryRow + 6}`).format.columnWidth = 48;
  rubric.getRange(`G1:G${summaryRow + 6}`).format.columnWidth = 14;
  rubric.getRange(`H1:H${summaryRow + 6}`).format.columnWidth = 38;
  rubric.getRange(`I1:I${summaryRow + 6}`).format.columnWidth = 12;
  rubric.getRange(`J1:J${summaryRow + 6}`).format.columnWidth = 42;
  rubric.freezePanes.freezeRows(3);
  addFindingsSheet();
}

function buildFinal() {
  const sheet = workbook.worksheets.add(sheets.final_review);
  sheet.showGridLines = false;
  addMetadata(sheet);
  section(sheet, 10, "H", TEXT.sequence);
  sheet.getRange("A11:C11").values = [[TEXT.event, TEXT.timestampDigest, TEXT.decision]];
  header(sheet.getRange("A11:C11"));
  const evidence = config.final_evidence.map((item) => [
    item.label,
    displayLiteral(item.value),
    "",
  ]);
  const evidenceLast = 11 + evidence.length;
  sheet.getRange(`A12:C${evidenceLast}`).values = evidence;
  sheet.getRange(`B12:B${evidenceLast}`).format.numberFormat = "@";
  body(sheet.getRange(`A12:C${evidenceLast}`));
  editable(sheet.getRange(`C12:C${evidenceLast}`));
  sheet.getRange(`C12:C${evidenceLast}`).dataValidation = {
    rule: { type: "list", values: [choice.confirmed, choice.issue] },
  };
  section(sheet, evidenceLast + 2, "H", TEXT.finalChecks);
  const checkHeader = evidenceLast + 3;
  sheet.getRange(`A${checkHeader}:D${checkHeader}`).values = [[labels.id, TEXT.check, TEXT.decision, TEXT.comment]];
  header(sheet.getRange(`A${checkHeader}:D${checkHeader}`));
  const checks = config.checklist.map((item) => [item.id, item.text, "", ""]);
  const last = checkHeader + checks.length;
  sheet.getRange(`A${checkHeader + 1}:D${last}`).values = checks;
  body(sheet.getRange(`A${checkHeader + 1}:D${last}`));
  editable(sheet.getRange(`C${checkHeader + 1}:D${last}`));
  sheet.getRange(`C${checkHeader + 1}:C${last}`).dataValidation = {
    rule: { type: "list", values: [choice.confirmed, choice.issue] },
  };
  const closureDecisionLast = Math.max(4, 3 + config.finding_closure.length);
  addReturnStatus(
    sheet, last + 2, `B${last + 3}`, `B${last + 4}`,
    `C${checkHeader + 1}:C${last}`,
    `AND(COUNTBLANK(C12:C${evidenceLast})=0,COUNTBLANK('${sheets.finding_closure}'!G4:G${closureDecisionLast})=0,COUNTIF('${sheets.finding_closure}'!G4:G${closureDecisionLast},"${choice.issue}")=0)`,
  );
  sheet.getRange(`B${last + 3}`).dataValidation = {
    rule: { type: "list", values: [choice.pass, choice.fail] },
  };
  sheet.freezePanes.freezeRows(11);
  sheet.getRange(`A1:A${last + 6}`).format.columnWidth = 24;
  sheet.getRange(`B1:B${last + 6}`).format.columnWidth = 66;
  sheet.getRange(`C1:C${last + 6}`).format.columnWidth = 18;
  sheet.getRange(`D1:H${last + 6}`).format.columnWidth = 24;

  const closure = workbook.worksheets.add(sheets.finding_closure);
  closure.showGridLines = false;
  setTitle(closure, closure.getRange("A1:G1"), TEXT.closureTitle);
  closure.getRange("A3:G3").values = [[
    TEXT.findingId, TEXT.sourceLayer, TEXT.disposition, TEXT.closedAt, TEXT.rationale,
    TEXT.evidenceSha, TEXT.finalCheck,
  ]];
  header(closure.getRange("A3:G3"));
  const rows = config.finding_closure.map((item) => [
    item.finding_id, item.source_layer, item.disposition,
    displayLiteral(item.closed_at),
    item.rationale, item.evidence_sha256, "",
  ]);
  const closureLast = Math.max(4, 3 + rows.length);
  if (rows.length) closure.getRange(`A4:G${closureLast}`).values = rows;
  else closure.getRange("A4:G4").values = [[TEXT.none, "", "", "", "", "", ""]];
  closure.getRange(`F4:F${closureLast}`).format.numberFormat = "@";
  body(closure.getRange(`A4:G${closureLast}`));
  editable(closure.getRange(`G4:G${closureLast}`));
  closure.getRange(`G4:G${closureLast}`).dataValidation = {
    rule: { type: "list", values: [choice.confirmed, choice.issue] },
  };
  closure.getRange(`A1:A${closureLast}`).format.columnWidth = 16;
  closure.getRange(`B1:B${closureLast}`).format.columnWidth = 24;
  closure.getRange(`C1:C${closureLast}`).format.columnWidth = 22;
  closure.getRange(`D1:D${closureLast}`).format.columnWidth = 24;
  closure.getRange(`E1:E${closureLast}`).format.columnWidth = 50;
  closure.getRange(`F1:F${closureLast}`).format.columnWidth = 68;
  closure.getRange(`G1:G${closureLast}`).format.columnWidth = 16;
  closure.freezePanes.freezeRows(3);
}

function buildSupplemental() {
  const sheet = workbook.worksheets.add(sheets.supplemental_review);
  sheet.showGridLines = false;
  addMetadata(sheet, "I");
  section(sheet, 10, "I", TEXT.changedOnly);
  sheet.getRange("A11:I11").values = [[
    TEXT.requirementId, TEXT.type, TEXT.originalRequest, TEXT.remediationEvidence,
    TEXT.decision, TEXT.issueComment, TEXT.rubricAdoption, TEXT.goldScore, TEXT.goldEvidence,
  ]];
  header(sheet.getRange("A11:I11"));
  const rows = config.requirements.map((item) => [
    item.requirement_id, item.kind_display, item.summary,
    item.remediation_display,
    "", "", item.rubric ? "" : choice.na, item.rubric ? null : choice.na,
    item.rubric ? "" : choice.na,
  ]);
  const last = 11 + rows.length;
  sheet.getRange(`A12:I${last}`).values = rows;
  body(sheet.getRange(`A12:I${last}`));
  editable(sheet.getRange(`E12:F${last}`));
  sheet.getRange(`E12:E${last}`).dataValidation = {
    rule: { type: "list", values: [choice.confirmed, choice.issue] },
  };
  config.requirements.forEach((item, index) => {
    if (!item.rubric) return;
    const row = 12 + index;
    editable(sheet.getRange(`G${row}:I${row}`));
    sheet.getRange(`G${row}`).dataValidation = {
      rule: { type: "list", values: [choice.adopt, choice.revise, choice.reject] },
    };
    sheet.getRange(`H${row}`).dataValidation = {
      rule: { type: "custom", formula1: `=AND(ISNUMBER(H${row}),H${row}=INT(H${row}),H${row}>=0,H${row}<=${item.rubric.max_score})` },
    };
  });
  const rowConditions = config.requirements.map((item, index) => {
    const row = 12 + index;
    const decision = `AND(E${row}<>"",OR(E${row}<>"${choice.issue}",F${row}<>""))`;
    if (!item.rubric) return decision;
    const rubric = `AND(G${row}<>"",ISNUMBER(H${row}),I${row}<>"",OR(G${row}="${choice.adopt}",F${row}<>""))`;
    return `AND(${decision},${rubric})`;
  });
  addReturnStatus(
    sheet, last + 2, `B${last + 3}`, `B${last + 4}`, `E12:E${last}`,
    `AND(${rowConditions.join(",")})`,
  );
  sheet.getRange(`B${last + 3}`).dataValidation = {
    rule: { type: "list", values: [choice.pass, choice.fail] },
  };
  sheet.freezePanes.freezeRows(11);
  sheet.getRange("A:A").format.columnWidth = 18;
  sheet.getRange("B:B").format.columnWidth = 22;
  sheet.getRange("C:C").format.columnWidth = 44;
  sheet.getRange("D:D").format.columnWidth = 60;
  sheet.getRange("E:E").format.columnWidth = 16;
  sheet.getRange("F:F").format.columnWidth = 36;
  sheet.getRange("G:G").format.columnWidth = 18;
  sheet.getRange("H:H").format.columnWidth = 12;
  sheet.getRange("I:I").format.columnWidth = 42;
}

function addBriefSheets() {
  if (!config.brief) return;
  const brief = config.brief;
  const sheet = workbook.worksheets.add(locale === "en" ? "Review Brief" : "审核摘要");
  sheet.showGridLines = false;
  setTitle(sheet, sheet.getRange("A1:D1"), TEXT.reviewBrief);
  const boundaries = brief.rights.usage_boundaries || {};
  const profile = brief.reviewer_profile || {};
  const rows = [
    [TEXT.requiredIndustry, profile.required_industry || "", TEXT.requiredOccupation, profile.required_occupation || ""],
    [TEXT.reviewScope, profile.review_scope || "", TEXT.typicalPerspective, profile.expert_profile || ""],
    [TEXT.strengths, (profile.strengths || []).join("; "), TEXT.firstCheck, profile.first_thought || ""],
    [TEXT.rightsHolder, brief.rights.rights_holder || "", TEXT.licence, brief.rights.license || ""],
    [TEXT.internalUse, boundaries.internal_use || "", TEXT.publicRelease, boundaries.public_release || ""],
    [TEXT.redistribution, boundaries.third_party_redistribution || "", TEXT.sublicensing, boundaries.sublicensing || ""],
  ];
  sheet.getRange("A3:D8").values = rows;
  sheet.getRange("A3:A8").format = { font: { bold: true }, fill: COLORS.paleTeal };
  sheet.getRange("C3:C8").format = { font: { bold: true }, fill: COLORS.paleTeal };
  body(sheet.getRange("A3:D8"));
  section(sheet, 10, "D", TEXT.authenticSources);
  sheet.getRange("A11:D11").values = [[TEXT.fileSourceId, TEXT.sourceUrl, TEXT.sourceShaType, TEXT.licenceNotes]];
  header(sheet.getRange("A11:D11"));
  const sourceRows = [
    ...(brief.deliverable_sources || []).map((item) => [
      item.path || item.filename, item.source_url, item.source_sha256,
      [item.rights_holder, item.license, item.acquired_at, item.transformation_record].filter(Boolean).join("; "),
    ]),
    ...(brief.reference_sources || []).filter((item) => item.adopted !== false).map((item) => [item.source_id, item.source_url, item.source_type, item.license]),
  ];
  if (sourceRows.length) sheet.getRange(`A12:D${11 + sourceRows.length}`).values = sourceRows;
  if (sourceRows.length) sheet.getRange(`C12:C${11 + sourceRows.length}`).format.numberFormat = "@";
  body(sheet.getRange(`A12:D${Math.max(12, 11 + sourceRows.length)}`));
  sheet.getRange("A:A").format.columnWidth = 26;
  sheet.getRange("B:B").format.columnWidth = 66;
  sheet.getRange("C:C").format.columnWidth = 48;
  sheet.getRange("D:D").format.columnWidth = 48;
  sheet.freezePanes.freezeRows(11);

  const inventory = workbook.worksheets.add(locale === "en" ? "File Inventory" : "文件清单");
  inventory.showGridLines = false;
  setTitle(inventory, inventory.getRange("A1:D1"), TEXT.inventoryTitle);
  inventory.getRange("A3:D3").values = [[TEXT.scope, TEXT.path, TEXT.bytes, "SHA-256"]];
  header(inventory.getRange("A3:D3"));
  const fileRows = (brief.files || []).map((item) => [item.scope, item.path, item.bytes, item.sha256]);
  if (fileRows.length) inventory.getRange(`A4:D${3 + fileRows.length}`).values = fileRows;
  if (fileRows.length) inventory.getRange(`D4:D${3 + fileRows.length}`).format.numberFormat = "@";
  body(inventory.getRange(`A4:D${Math.max(4, 3 + fileRows.length)}`));
  inventory.getRange("A:A").format.columnWidth = 22;
  inventory.getRange("B:B").format.columnWidth = 62;
  inventory.getRange("C:C").format.columnWidth = 14;
  inventory.getRange("D:D").format.columnWidth = 70;
  inventory.freezePanes.freezeRows(3);
}

if (config.layer === "general_review") buildGeneral();
else if (config.layer === "occupational_expert_review") buildOccupational();
else if (config.layer === "final_review") buildFinal();
else if (config.layer === "supplemental_review") buildSupplemental();
else throw new Error(`unsupported review layer: ${config.layer}`);

addBriefSheets();

for (const sheet of workbook.worksheets.items) {
  const used = sheet.getUsedRange();
  if (used) used.format.autofitRows();
}

await fs.mkdir(path.dirname(outputPath), { recursive: true });
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
