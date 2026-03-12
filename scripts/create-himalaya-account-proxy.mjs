#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";

function getArg(name, fallback = "") {
  const prefix = `--${name}=`;
  const found = process.argv.slice(2).find((a) => a.startsWith(prefix));
  return found ? found.slice(prefix.length) : fallback;
}

const outPath = path.resolve(getArg("out", "./scripts/himalaya-account-proxy.mjs"));
const himalayaCommand = getArg("himalaya", "himalaya");
const account = getArg("account", "ACCOUNT_NAME");

const content = `#!/usr/bin/env node\nimport { spawnSync } from \"node:child_process\";\n\nconst account = process.env.HIMALAYA_ACCOUNT || ${JSON.stringify(account)};\nconst baseCommand = process.env.HIMALAYA_EXE || ${JSON.stringify(himalayaCommand)};\nconst args = process.argv.slice(2);\n\nconst result = spawnSync(baseCommand, [...args, \"-a\", account], {\n  stdio: \"inherit\",\n  shell: process.platform === \"win32\",\n});\n\nprocess.exit(result.status ?? 1);\n`;

fs.mkdirSync(path.dirname(outPath), { recursive: true });
fs.writeFileSync(outPath, content, "utf8");
console.log(`Wrapper written: ${outPath}`);
