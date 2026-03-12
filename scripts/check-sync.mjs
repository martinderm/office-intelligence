#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";

function fail(msg) {
  console.error(`[check-sync] ${msg}`);
  process.exit(1);
}

function parseArgs(argv) {
  const pairs = [];
  let strict = false;

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--strict") {
      strict = true;
      continue;
    }
    if (arg === "--pair") {
      const source = argv[i + 1];
      const target = argv[i + 2];
      if (!source || !target) {
        fail("--pair braucht zwei Pfade: --pair <source> <target>");
      }
      pairs.push({ source, target });
      i += 2;
      continue;
    }

    fail(`unbekanntes Argument: ${arg}`);
  }

  if (!pairs.length) {
    fail("mindestens ein Pair nötig: --pair <source> <target>");
  }

  return { pairs, strict };
}

function listFilesRecursive(root) {
  const files = [];
  const stack = [root];

  while (stack.length) {
    const current = stack.pop();
    if (!current) continue;
    const entries = fs.readdirSync(current, { withFileTypes: true });
    for (const entry of entries) {
      const full = path.join(current, entry.name);
      if (entry.isDirectory()) {
        stack.push(full);
      } else if (entry.isFile()) {
        files.push(path.relative(root, full));
      }
    }
  }

  files.sort();
  return files;
}

function compareFile(sourceFile, targetFile) {
  if (!fs.existsSync(sourceFile)) return { ok: false, reason: "source_missing" };
  if (!fs.existsSync(targetFile)) return { ok: false, reason: "target_missing" };

  const source = fs.readFileSync(sourceFile);
  const target = fs.readFileSync(targetFile);
  return { ok: source.equals(target), reason: source.equals(target) ? "equal" : "content_diff" };
}

function comparePair(sourcePath, targetPath, strict) {
  const sourceStat = fs.existsSync(sourcePath) ? fs.statSync(sourcePath) : null;
  const targetStat = fs.existsSync(targetPath) ? fs.statSync(targetPath) : null;

  if (!sourceStat) return [{ type: "error", message: `source fehlt: ${sourcePath}` }];
  if (!targetStat) return [{ type: "error", message: `target fehlt: ${targetPath}` }];

  if (sourceStat.isFile() && targetStat.isFile()) {
    const result = compareFile(sourcePath, targetPath);
    if (!result.ok) {
      return [{ type: "error", message: `Datei abweichend (${result.reason}): ${sourcePath} -> ${targetPath}` }];
    }
    return [{ type: "ok", message: `OK Datei: ${sourcePath} == ${targetPath}` }];
  }

  if (sourceStat.isDirectory() && targetStat.isDirectory()) {
    const errors = [];
    const sourceFiles = listFilesRecursive(sourcePath);
    const sourceSet = new Set(sourceFiles);

    for (const rel of sourceFiles) {
      const sourceFile = path.join(sourcePath, rel);
      const targetFile = path.join(targetPath, rel);
      const result = compareFile(sourceFile, targetFile);
      if (!result.ok) {
        errors.push({ type: "error", message: `Datei abweichend (${result.reason}): ${sourceFile} -> ${targetFile}` });
      }
    }

    if (strict) {
      const targetFiles = listFilesRecursive(targetPath);
      for (const rel of targetFiles) {
        if (!sourceSet.has(rel)) {
          errors.push({ type: "error", message: `zusätzliche Datei im Target (strict): ${path.join(targetPath, rel)}` });
        }
      }
    }

    if (!errors.length) {
      return [{ type: "ok", message: `OK Verzeichnis: ${sourcePath} == ${targetPath}` }];
    }

    return errors;
  }

  return [{ type: "error", message: `Typ-Mismatch (File/Dir): ${sourcePath} -> ${targetPath}` }];
}

const { pairs, strict } = parseArgs(process.argv.slice(2));
let hasErrors = false;

for (const pair of pairs) {
  const sourcePath = path.resolve(pair.source);
  const targetPath = path.resolve(pair.target);
  const results = comparePair(sourcePath, targetPath, strict);
  for (const r of results) {
    const prefix = r.type === "ok" ? "OK" : "ERR";
    console.log(`[check-sync] ${prefix} ${r.message}`);
    if (r.type === "error") hasErrors = true;
  }
}

if (hasErrors) {
  process.exit(1);
}
