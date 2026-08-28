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
    ["Task ID", config.task.task_id],
    ["Sector", config.task.sector],
    ["Occupation", config.task.occupation],
    ["Task language", config.task.language],
    ["Rubric version", config.task.rubric_version],
    ["Candidate SHA-256", config.task.candidate_sha256],
  ];
  sheet.getRange("A3:B8").values = rows;
  sheet.getRange("A3:A8").format = { font: { bold: true }, fill: COLORS.paleTeal };
  sheet.getRange(`B3:${lastCol}8`).format = { wrapText: true };
  for (let row = 3; row <= 8; row += 1) {
    sheet.getRange(`B${row}:${lastCol}${row}`).merge();
  }
  sheet.getRange(`A3:${lastCol}8`).format.borders = {
    preset: "outside", style: "thin", color: COLORS.line,
  };
}

function addFindingsSheet() {
  const sheet = workbook.worksheets.add("Findings");
  sheet.showGridLines = false;
  setTitle(sheet, sheet.getRange("A1:F1"), "Findings / 问题记录");
  sheet.getRange("A3:F3").values = [[
    "Finding ID", "Severity", "Location", "Issue", "Recommendation", "Requires confirmation",
  ]];
  header(sheet.getRange("A3:F3"));
  const rows = Array.from({ length: 20 }, (_, index) => [
    `${config.layer === "general_review" ? "G" : config.layer === "occupational_expert_review" ? "E" : "F"}-F${String(index + 1).padStart(2, "0")}`,
    "", "", "", "", "No",
  ]);
  sheet.getRange("A4:F23").values = rows;
  body(sheet.getRange("A4:F23"));
  editable(sheet.getRange("B4:F23"));
  sheet.getRange("B4:B23").dataValidation = {
    rule: { type: "list", values: ["Blocker", "Major", "Minor"] },
  };
  sheet.getRange("F4:F23").dataValidation = {
    rule: { type: "list", values: ["Yes", "No"] },
  };
  sheet.getRange("A25:B27").values = [
    ["Findings entered", null],
    ["Blocking findings", null],
    ["Findings complete", null],
  ];
  sheet.getRange("B25").formulas = [["=COUNTIF(B4:B23,\"?*\")"]];
  sheet.getRange("B26").formulas = [["=COUNTIF(B4:B23,\"Blocker\")+COUNTIF(B4:B23,\"Major\")"]];
  sheet.getRange("B27").formulas = [[
    "=IF(AND(COUNTIF(B4:B23,\"?*\")=COUNTIF(C4:C23,\"?*\"),COUNTIF(B4:B23,\"?*\")=COUNTIF(D4:D23,\"?*\"),COUNTIF(B4:B23,\"?*\")=COUNTIF(E4:E23,\"?*\")),\"OK\",\"Complete location, issue and recommendation\")",
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
  section(sheet, row, "H", "Return status / 返回状态");
  sheet.getRange(`A${row + 1}:B${row + 4}`).values = [
    ["Conclusion", ""],
    ["Substantive opinion", ""],
    ["Project-side identity entry", "Completed after receipt; not reviewer-authored"],
    ["Ready to return", ""],
  ];
  editable(sheet.getRange(`B${row + 1}:H${row + 2}`));
  sheet.getRange(`B${row + 1}:H${row + 1}`).merge();
  sheet.getRange(`B${row + 2}:H${row + 2}`).merge();
  sheet.getRange(`B${row + 3}:H${row + 3}`).merge();
  sheet.getRange(`B${row + 4}:H${row + 4}`).merge();
  sheet.getRange(decisionCell).dataValidation = {
    rule: { type: "list", values: ["Pass", "Conditional pass", "Fail"] },
  };
  sheet.getRange(`B${row + 4}`).formulas = [[
    `=IF(AND(COUNTBLANK(${checklistRange})=0,${decisionCell}<>\"\",${opinionCell}<>\"\",${extraCondition}),\"READY\",\"INCOMPLETE\")`,
  ]];
  sheet.getRange(`B${row + 4}:H${row + 4}`).format = {
    fill: COLORS.paleGreen, font: { bold: true, color: COLORS.text },
  };
}

function buildGeneral() {
  const sheet = workbook.worksheets.add("General Review");
  sheet.showGridLines = false;
  addMetadata(sheet);
  section(sheet, 10, "H", "Checklist / 审查项");
  sheet.getRange("A11:D11").values = [["ID", "Check", "Decision", "Comment if needed"]];
  header(sheet.getRange("A11:D11"));
  const checks = config.checklist.map((item) => [item.id, item.text, "", ""]);
  const last = 11 + checks.length;
  sheet.getRange(`A12:D${last}`).values = checks;
  body(sheet.getRange(`A12:D${last}`));
  editable(sheet.getRange(`C12:D${last}`));
  sheet.getRange(`C12:C${last}`).dataValidation = {
    rule: { type: "list", values: ["Pass", "Issue", "N/A"] },
  };
  addReturnStatus(
    sheet, last + 2, `B${last + 3}`, `B${last + 4}`, `C12:C${last}`,
    `AND('Findings'!B27=\"OK\",'Findings'!B25=COUNTIF(C12:C${last},\"Issue\"))`,
  );
  sheet.freezePanes.freezeRows(11);
  sheet.getRange(`A1:A${last + 6}`).format.columnWidth = 24;
  sheet.getRange(`B1:B${last + 6}`).format.columnWidth = 66;
  sheet.getRange(`C1:C${last + 6}`).format.columnWidth = 16;
  sheet.getRange(`D1:H${last + 6}`).format.columnWidth = 24;
  addFindingsSheet();
}

function buildOccupational() {
  const sheet = workbook.worksheets.add("Occupation Review");
  sheet.showGridLines = false;
  addMetadata(sheet);
  section(sheet, 10, "H", "Occupation mapping / 职业映射");
  sheet.getRange("A11:B14").values = [
    ["Proposed mapping", config.mapping.proposed],
    ["Boundary", config.mapping.boundary],
    ["Decision", ""],
    ["Substantive reason", ""],
  ];
  sheet.getRange("B11:H12").format = { wrapText: true };
  for (let row = 11; row <= 14; row += 1) sheet.getRange(`B${row}:H${row}`).merge();
  editable(sheet.getRange("B13:H14"));
  sheet.getRange("B13").dataValidation = {
    rule: { type: "list", values: ["Accept", "Conditional accept", "Reject"] },
  };
  section(sheet, 16, "H", "Professional checks / 专业核对");
  sheet.getRange("A17:D17").values = [["ID", "Check", "Decision", "Comment if needed"]];
  header(sheet.getRange("A17:D17"));
  const checks = config.checklist.map((item) => [item.id, item.text, "", ""]);
  const last = 17 + checks.length;
  sheet.getRange(`A18:D${last}`).values = checks;
  body(sheet.getRange(`A18:D${last}`));
  editable(sheet.getRange(`C18:D${last}`));
  sheet.getRange(`C18:C${last}`).dataValidation = {
    rule: { type: "list", values: ["Pass", "Issue", "N/A"] },
  };
  addReturnStatus(
    sheet, last + 2, `B${last + 3}`, `B${last + 4}`, `C18:C${last}`,
    `AND(B13<>\"\",'Findings'!B27=\"OK\",'Findings'!B25=COUNTIF(C18:C${last},\"Issue\"),'Rubric and Gold'!B${config.rubrics.length + 11}=\"READY\")`,
  );
  sheet.freezePanes.freezeRows(17);
  sheet.getRange(`A1:A${last + 6}`).format.columnWidth = 24;
  sheet.getRange(`B1:B${last + 6}`).format.columnWidth = 66;
  sheet.getRange(`C1:C${last + 6}`).format.columnWidth = 18;
  sheet.getRange(`D1:H${last + 6}`).format.columnWidth = 24;

  const rubric = workbook.worksheets.add("Rubric and Gold");
  rubric.showGridLines = false;
  setTitle(rubric, rubric.getRange("A1:J1"), "Rubric adoption and Gold scoring / Rubric 采纳与 Gold 评分");
  rubric.getRange("A3:J3").values = [[
    "Code", "Rubric item ID", "Required", "Max", "Criterion",
    "Verification / machine result", "Adoption", "Reason or revision",
    "Gold score", "Gold evidence or reason",
  ]];
  header(rubric.getRange("A3:J3"));
  const rows = config.rubrics.map((item) => [
    item.code, item.rubric_item_id, item.required, item.max_score, item.criterion,
    `${item.verification || ""}${item.machine_result ? `\nMachine: ${item.machine_result}` : ""}`,
    "", "", null, "",
  ]);
  const rubricLast = 3 + rows.length;
  rubric.getRange(`A4:J${rubricLast}`).values = rows;
  body(rubric.getRange(`A4:J${rubricLast}`));
  editable(rubric.getRange(`G4:J${rubricLast}`));
  rubric.getRange(`G4:G${rubricLast}`).dataValidation = {
    rule: { type: "list", values: ["Adopt", "Revise", "Reject"] },
  };
  rubric.getRange(`I4:I${rubricLast}`).dataValidation = {
    rule: { type: "custom", formula1: "=AND(ISNUMBER(I4),I4=INT(I4),I4>=0,I4<=D4)" },
  };
  const summaryRow = rubricLast + 2;
  rubric.getRange(`A${summaryRow}:B${summaryRow + 6}`).values = [
    ["Items", null], ["Adopt", null], ["Revise", null], ["Reject", null],
    ["Gold total", null], ["Missing decisions/scores", null], ["Ready", null],
  ];
  rubric.getRange(`B${summaryRow}`).formulas = [[`=COUNTA(A4:A${rubricLast})`]];
  rubric.getRange(`B${summaryRow + 1}`).formulas = [[`=COUNTIF(G4:G${rubricLast},\"Adopt\")`]];
  rubric.getRange(`B${summaryRow + 2}`).formulas = [[`=COUNTIF(G4:G${rubricLast},\"Revise\")`]];
  rubric.getRange(`B${summaryRow + 3}`).formulas = [[`=COUNTIF(G4:G${rubricLast},\"Reject\")`]];
  rubric.getRange(`B${summaryRow + 4}`).formulas = [[`=SUM(I4:I${rubricLast})`]];
  rubric.getRange(`B${summaryRow + 5}`).formulas = [[
    `=COUNTBLANK(G4:G${rubricLast})+COUNTBLANK(I4:I${rubricLast})+COUNTIFS(G4:G${rubricLast},\"<>Adopt\",H4:H${rubricLast},\"\")+COUNTIF(J4:J${rubricLast},\"\")+SUMPRODUCT(--(I4:I${rubricLast}>D4:D${rubricLast}))`,
  ]];
  rubric.getRange(`B${summaryRow + 6}`).formulas = [[
    `=IF(AND(B${summaryRow + 5}=0,B${summaryRow + 4}<=SUM(D4:D${rubricLast})),\"READY\",\"INCOMPLETE\")`,
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
  const sheet = workbook.worksheets.add("Final Review");
  sheet.showGridLines = false;
  addMetadata(sheet);
  section(sheet, 10, "H", "Sequence and frozen evidence / 时序与冻结证据");
  sheet.getRange("A11:C11").values = [["Event", "Timestamp / digest", "Decision"]];
  header(sheet.getRange("A11:C11"));
  const evidence = config.final_evidence.map((item) => [
    item.label,
    displayLiteral(item.value),
    "",
  ]);
  const evidenceLast = 11 + evidence.length;
  sheet.getRange(`A12:C${evidenceLast}`).values = evidence;
  body(sheet.getRange(`A12:C${evidenceLast}`));
  editable(sheet.getRange(`C12:C${evidenceLast}`));
  sheet.getRange(`C12:C${evidenceLast}`).dataValidation = {
    rule: { type: "list", values: ["Confirmed", "Issue"] },
  };
  section(sheet, evidenceLast + 2, "H", "Final checks / 终审核对");
  const checkHeader = evidenceLast + 3;
  sheet.getRange(`A${checkHeader}:D${checkHeader}`).values = [["ID", "Check", "Decision", "Comment if needed"]];
  header(sheet.getRange(`A${checkHeader}:D${checkHeader}`));
  const checks = config.checklist.map((item) => [item.id, item.text, "", ""]);
  const last = checkHeader + checks.length;
  sheet.getRange(`A${checkHeader + 1}:D${last}`).values = checks;
  body(sheet.getRange(`A${checkHeader + 1}:D${last}`));
  editable(sheet.getRange(`C${checkHeader + 1}:D${last}`));
  sheet.getRange(`C${checkHeader + 1}:C${last}`).dataValidation = {
    rule: { type: "list", values: ["Confirmed", "Issue"] },
  };
  const closureDecisionLast = Math.max(4, 3 + config.finding_closure.length);
  addReturnStatus(
    sheet, last + 2, `B${last + 3}`, `B${last + 4}`,
    `C${checkHeader + 1}:C${last}`,
    `AND(COUNTBLANK(C12:C${evidenceLast})=0,COUNTBLANK('Finding Closure'!G4:G${closureDecisionLast})=0,COUNTIF('Finding Closure'!G4:G${closureDecisionLast},\"Issue\")=0)`,
  );
  sheet.getRange(`B${last + 3}`).dataValidation = {
    rule: { type: "list", values: ["Pass", "Fail"] },
  };
  sheet.freezePanes.freezeRows(11);
  sheet.getRange(`A1:A${last + 6}`).format.columnWidth = 24;
  sheet.getRange(`B1:B${last + 6}`).format.columnWidth = 66;
  sheet.getRange(`C1:C${last + 6}`).format.columnWidth = 18;
  sheet.getRange(`D1:H${last + 6}`).format.columnWidth = 24;

  const closure = workbook.worksheets.add("Finding Closure");
  closure.showGridLines = false;
  setTitle(closure, closure.getRange("A1:G1"), "Finding closure / 整改闭环");
  closure.getRange("A3:G3").values = [[
    "Finding ID", "Source layer", "Disposition", "Closed at", "Rationale",
    "Evidence SHA-256", "Final check",
  ]];
  header(closure.getRange("A3:G3"));
  const rows = config.finding_closure.map((item) => [
    item.finding_id, item.source_layer, item.disposition,
    displayLiteral(item.closed_at),
    item.rationale, item.evidence_sha256, "",
  ]);
  const closureLast = Math.max(4, 3 + rows.length);
  if (rows.length) closure.getRange(`A4:G${closureLast}`).values = rows;
  else closure.getRange("A4:G4").values = [["None", "", "", "", "", "", ""]];
  body(closure.getRange(`A4:G${closureLast}`));
  editable(closure.getRange(`G4:G${closureLast}`));
  closure.getRange(`G4:G${closureLast}`).dataValidation = {
    rule: { type: "list", values: ["Confirmed", "Issue"] },
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

if (config.layer === "general_review") buildGeneral();
else if (config.layer === "occupational_expert_review") buildOccupational();
else if (config.layer === "final_review") buildFinal();
else throw new Error(`unsupported review layer: ${config.layer}`);

for (const sheet of workbook.worksheets.items) {
  const used = sheet.getUsedRange();
  if (used) used.format.autofitRows();
}

await fs.mkdir(path.dirname(outputPath), { recursive: true });
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
