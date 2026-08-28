import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const args = process.argv.slice(2);
const valueAfter = (name) => {
  const index = args.indexOf(name);
  if (index < 0 || index + 1 >= args.length) throw new Error(`missing ${name}`);
  return args[index + 1];
};

const inputPath = valueAfter("--input");
const renderDir = valueAfter("--render-dir");
const input = await FileBlob.load(inputPath);
const workbook = await SpreadsheetFile.importXlsx(input);
await fs.mkdir(renderDir, { recursive: true });

const report = { input: inputPath, sheets: [], formulaErrors: null };
for (const sheet of workbook.worksheets.items) {
  const used = sheet.getUsedRange();
  const address = used ? used.address : "A1";
  const inspected = await workbook.inspect({
    kind: "table",
    range: `'${sheet.name}'!${address}`,
    include: "values,formulas",
    tableMaxRows: 12,
    tableMaxCols: 12,
    maxChars: 5000,
  });
  const rendered = await workbook.render({
    sheetName: sheet.name,
    autoCrop: "all",
    scale: 1.25,
    format: "png",
  });
  const filename = `${sheet.name.replace(/[^A-Za-z0-9_-]+/g, "_")}.png`;
  await fs.writeFile(path.join(renderDir, filename),
    new Uint8Array(await rendered.arrayBuffer()));
  report.sheets.push({ name: sheet.name, range: address,
    preview: filename, inspect: inspected.ndjson });
}

const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 300 },
  summary: "final formula error scan",
});
report.formulaErrors = errors.ndjson;
console.log(JSON.stringify(report, null, 2));
