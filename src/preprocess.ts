import { sanitizeForRouting, SanitizeOptions } from "./sanitize.js";

export type MailMeta = {
  from?: string;
  replyTo?: string;
  returnPath?: string;
  subject?: string;
  date?: string;
  messageId?: string;
  inReplyTo?: string;
  references?: string;
  listId?: string;
  listUnsubscribe?: string;
  precedence?: string;
  autoSubmitted?: string;
  authSummary?: string;
  sourceContentType?: string;
  sourceTransferEncoding?: string;
};

export type PreparedMail = {
  effectiveText: string;
  currentMessage: string;
  quotedContext: string;
  truncated: boolean;
  originalChars: number;
  keptChars: number;
  sanitizing: ReturnType<typeof sanitizeForRouting>;
  meta: MailMeta;
};

function decodeBasicMimeWords(input: string): string {
  return input.replace(/=\?([^?]+)\?([bqBQ])\?([^?]+)\?=/g, (_m, charsetRaw, encRaw, payload) => {
    const charset = String(charsetRaw || "utf-8").toLowerCase();
    const enc = String(encRaw || "q").toLowerCase();

    let bytes: Buffer;
    try {
      if (enc === "b") {
        bytes = Buffer.from(payload, "base64");
      } else {
        const q = String(payload).replace(/_/g, " ");
        const arr: number[] = [];
        for (let i = 0; i < q.length; i += 1) {
          const ch = q[i];
          if (ch === "=" && i + 2 < q.length) {
            const hex = q.slice(i + 1, i + 3);
            if (/^[0-9A-Fa-f]{2}$/.test(hex)) {
              arr.push(Number.parseInt(hex, 16));
              i += 2;
              continue;
            }
          }
          arr.push(ch.charCodeAt(0));
        }
        bytes = Buffer.from(arr);
      }

      if (charset.includes("iso-8859-1") || charset.includes("latin1")) {
        return bytes.toString("latin1");
      }
      return bytes.toString("utf8");
    } catch {
      return String(payload || "");
    }
  });
}

function decodeQuotedPrintable(input: string): string {
  const softBreaksRemoved = input.replace(/=\r?\n/g, "");
  const bytes: number[] = [];

  for (let i = 0; i < softBreaksRemoved.length; i += 1) {
    const ch = softBreaksRemoved[i];
    if (ch === "=" && i + 2 < softBreaksRemoved.length) {
      const hex = softBreaksRemoved.slice(i + 1, i + 3);
      if (/^[0-9A-Fa-f]{2}$/.test(hex)) {
        bytes.push(Number.parseInt(hex, 16));
        i += 2;
        continue;
      }
    }
    bytes.push(softBreaksRemoved.charCodeAt(i));
  }

  return Buffer.from(bytes).toString("utf8");
}

function parseHeaders(raw: string): { headers: Map<string, string>; headerText: string; bodyStart: number } {
  const match = raw.match(/\r?\n\r?\n/);
  if (!match || match.index == null) {
    return { headers: new Map(), headerText: "", bodyStart: 0 };
  }

  const bodyStart = match.index + match[0].length;
  const headerText = raw.slice(0, match.index);
  const lines = headerText.split(/\r?\n/);
  const unfolded: string[] = [];

  for (const line of lines) {
    if (/^[ \t]/.test(line) && unfolded.length > 0) {
      unfolded[unfolded.length - 1] += ` ${line.trim()}`;
    } else {
      unfolded.push(line);
    }
  }

  const headers = new Map<string, string>();
  for (const line of unfolded) {
    const idx = line.indexOf(":");
    if (idx <= 0) continue;
    const key = line.slice(0, idx).trim().toLowerCase();
    const value = line.slice(idx + 1).trim();
    if (!headers.has(key)) headers.set(key, value);
    else headers.set(key, `${headers.get(key)} | ${value}`);
  }

  return { headers, headerText, bodyStart };
}

function authSummary(authRaw?: string): string | undefined {
  if (!authRaw) return undefined;
  const spf = /spf\s*=\s*([a-z]+)/i.exec(authRaw)?.[1];
  const dkim = /dkim\s*=\s*([a-z]+)/i.exec(authRaw)?.[1];
  const dmarc = /dmarc\s*=\s*([a-z]+)/i.exec(authRaw)?.[1];
  const parts = [spf ? `spf:${spf}` : "", dkim ? `dkim:${dkim}` : "", dmarc ? `dmarc:${dmarc}` : ""].filter(Boolean);
  return parts.length ? parts.join(", ") : undefined;
}

function extractMeta(headers: Map<string, string>): MailMeta {
  const get = (k: string): string | undefined => headers.get(k)?.trim() || undefined;
  return {
    from: decodeBasicMimeWords(get("from") || "") || undefined,
    replyTo: decodeBasicMimeWords(get("reply-to") || "") || undefined,
    returnPath: decodeBasicMimeWords(get("return-path") || "") || undefined,
    subject: decodeBasicMimeWords(get("subject") || "") || undefined,
    date: get("date"),
    messageId: get("message-id"),
    inReplyTo: get("in-reply-to"),
    references: get("references"),
    listId: get("list-id"),
    listUnsubscribe: get("list-unsubscribe"),
    precedence: get("precedence"),
    autoSubmitted: get("auto-submitted"),
    authSummary: authSummary(get("authentication-results")),
    sourceContentType: get("content-type"),
    sourceTransferEncoding: get("content-transfer-encoding"),
  };
}

function getBoundary(contentType?: string): string | undefined {
  if (!contentType) return undefined;
  const m = /boundary\s*=\s*(?:"([^"]+)"|([^;\s]+))/i.exec(contentType);
  return (m?.[1] || m?.[2] || "").trim() || undefined;
}

function decodeByEncoding(body: string, transferEncoding?: string): string {
  const enc = (transferEncoding || "").toLowerCase();
  if (enc.includes("quoted-printable")) return decodeQuotedPrintable(body);
  if (enc.includes("base64")) {
    try {
      const compact = body.replace(/\s+/g, "");
      return Buffer.from(compact, "base64").toString("utf8");
    } catch {
      return body;
    }
  }
  return body;
}

function extractBodyFromPart(partRaw: string): { contentType?: string; transferEncoding?: string; body: string } {
  const parsed = parseHeaders(partRaw);
  const headers = parsed.headers;
  const contentType = headers.get("content-type");
  const transferEncoding = headers.get("content-transfer-encoding");
  const body = decodeByEncoding(partRaw.slice(parsed.bodyStart), transferEncoding);
  return { contentType, transferEncoding, body };
}

function extractBestBody(raw: string): { body: string; detectedType: "html" | "text" } {
  const parsed = parseHeaders(raw);
  const headers = parsed.headers;
  const bodyRaw = raw.slice(parsed.bodyStart);
  const topContentType = headers.get("content-type") || "";

  // Himalaya pseudo-part wrapper fallback
  const himalayaHtml = /<#part\s+type\s*=\s*text\/html\s*>([\s\S]*?)<#\/part>/i.exec(raw)?.[1];
  if (himalayaHtml) {
    return { body: decodeQuotedPrintable(himalayaHtml), detectedType: "html" };
  }

  const boundary = getBoundary(topContentType);
  if (!boundary) {
    const transferEncoding = headers.get("content-transfer-encoding");
    const decoded = decodeByEncoding(bodyRaw, transferEncoding);
    const isHtml = /text\/html/i.test(topContentType) || /<\/?(html|body|div|a|table)\b/i.test(decoded);
    return { body: decoded, detectedType: isHtml ? "html" : "text" };
  }

  const marker = `--${boundary}`;
  const parts = bodyRaw.split(marker).map((p) => p.trim()).filter((p) => p && p !== "--");

  let htmlCandidate: string | undefined;
  let textCandidate: string | undefined;

  for (const p of parts) {
    const cleaned = p.replace(/^\r?\n/, "");
    const part = extractBodyFromPart(cleaned);
    const ctype = (part.contentType || "").toLowerCase();

    if (!htmlCandidate && ctype.includes("text/html")) {
      htmlCandidate = part.body;
      continue;
    }
    if (!textCandidate && ctype.includes("text/plain")) {
      textCandidate = part.body;
    }
  }

  if (htmlCandidate) return { body: htmlCandidate, detectedType: "html" };
  if (textCandidate) return { body: textCandidate, detectedType: "text" };

  return { body: decodeByEncoding(bodyRaw, headers.get("content-transfer-encoding")), detectedType: "text" };
}

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
      /^---+\s*original message\s*---+/i.test(trimmed) ||
      /^>>>\s*/.test(trimmed);

    if (!inQuoted && startsQuoted) {
      inQuoted = true;
    }

    if (inQuoted) quoted.push(line);
    else current.push(line);
  }

  return { current: current.join("\n").trim(), quoted: quoted.join("\n").trim() };
}

function clip(text: string, maxChars: number): string {
  if (text.length <= maxChars) return text;
  return `${text.slice(0, maxChars)}\n...[truncated]`;
}

function buildMetaPrefix(meta: MailMeta): string {
  const rows: string[] = [];
  if (meta.from) rows.push(`From: ${meta.from}`);
  if (meta.replyTo) rows.push(`Reply-To: ${meta.replyTo}`);
  if (meta.subject) rows.push(`Subject: ${meta.subject}`);
  if (meta.date) rows.push(`Date: ${meta.date}`);
  if (meta.listId) rows.push(`List-ID: ${meta.listId}`);
  if (meta.listUnsubscribe) rows.push(`List-Unsubscribe: ${meta.listUnsubscribe}`);
  if (meta.precedence) rows.push(`Precedence: ${meta.precedence}`);
  if (meta.autoSubmitted) rows.push(`Auto-Submitted: ${meta.autoSubmitted}`);
  if (meta.authSummary) rows.push(`Auth: ${meta.authSummary}`);
  if (rows.length === 0) return "";
  return `[MAIL_META]\n${rows.join("\n")}\n\n`;
}

export function prepareMailText(
  raw: string,
  maxCurrent = 8000,
  maxQuoted = 2500,
  sanitizeOptions?: SanitizeOptions,
): PreparedMail {
  const parsed = parseHeaders(raw);
  const meta = extractMeta(parsed.headers);
  const bodyExtract = extractBestBody(raw);

  if (!meta.sourceContentType) {
    meta.sourceContentType = bodyExtract.detectedType === "html" ? "text/html (detected)" : "text/plain (detected)";
  }

  const bodyInput = bodyExtract.body || raw;
  const sanitizing = sanitizeForRouting(
    bodyInput,
    sanitizeOptions ?? {
      enabled: true,
      mode: "balanced",
      stripTrackingParams: true,
      trimNewsletterFooter: true,
    },
  );

  const withMeta = `${buildMetaPrefix(meta)}${sanitizing.text}`.trim();
  const originalChars = withMeta.length;
  const { current, quoted } = splitCurrentAndQuoted(withMeta);

  const currentLimit = sanitizing.flags.htmlLikely ? Math.min(maxCurrent, 5000) : maxCurrent;
  const quotedLimit = sanitizing.flags.htmlLikely ? Math.min(maxQuoted, 1200) : maxQuoted;

  const keptCurrent = clip(current, currentLimit);
  const keptQuoted = clip(quoted, quotedLimit);

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
    sanitizing,
    meta,
  };
}
