export type PreparedMail = {
  effectiveText: string;
  currentMessage: string;
  quotedContext: string;
  truncated: boolean;
  originalChars: number;
  keptChars: number;
};

function splitCurrentAndQuoted(raw: string): { current: string; quoted: string } {
  const lines = raw.split(/\r?\n/);
  const current: string[] = [];
  const quoted: string[] = [];

  let inQuoted = false;
  for (const line of lines) {
    const trimmed = line.trim();
    const startsQuoted =
      trimmed.startsWith(">") ||
      /^on .+wrote:$/i.test(trimmed) ||
      /^from:\s/i.test(trimmed) ||
      /^sent:\s/i.test(trimmed) ||
      /^subject:\s/i.test(trimmed) ||
      /^---+\s*original message\s*---+/i.test(trimmed);

    if (startsQuoted) inQuoted = true;
    if (inQuoted) quoted.push(line);
    else current.push(line);
  }

  return { current: current.join("\n").trim(), quoted: quoted.join("\n").trim() };
}

function clip(text: string, maxChars: number): string {
  if (text.length <= maxChars) return text;
  return `${text.slice(0, maxChars)}\n...[truncated]`;
}

export function prepareMailText(raw: string, maxCurrent = 8000, maxQuoted = 2500): PreparedMail {
  const originalChars = raw.length;
  const { current, quoted } = splitCurrentAndQuoted(raw);

  const keptCurrent = clip(current, maxCurrent);
  const keptQuoted = clip(quoted, maxQuoted);

  const effectiveText = [
    "[CURRENT_MESSAGE]",
    keptCurrent || "",
    "",
    "[OLDER_CONTEXT_LOWER_WEIGHT]",
    keptQuoted || "",
  ]
    .join("\n")
    .trim();

  const keptChars = effectiveText.length;
  return {
    effectiveText,
    currentMessage: keptCurrent,
    quotedContext: keptQuoted,
    truncated: keptChars < originalChars,
    originalChars,
    keptChars,
  };
}
