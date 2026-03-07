export type SanitizeMode = "off" | "balanced" | "strict";

export type SanitizeOptions = {
  enabled: boolean;
  mode: SanitizeMode;
  stripTrackingParams: boolean;
  trimNewsletterFooter: boolean;
};

export type SanitizedMail = {
  text: string;
  flags: {
    htmlLikely: boolean;
    newsletterLikely: boolean;
    hasUnsubscribe: boolean;
    bulkLikely: boolean;
  };
  meta: {
    originalChars: number;
    sanitizedChars: number;
    removedTags: number;
    linkCount: number;
    trackingParamsRemoved: number;
  };
};

const TRACKING_KEYS_EXACT = new Set([
  "fbclid",
  "gclid",
  "msclkid",
  "mc_eid",
  "mc_cid",
  "vero_id",
  "vero_conv",
  "oly_anon_id",
  "oly_enc_id",
  "ems_l",
  "_esuh",
  "i",
  "d",
  "p",
]);

function isHtmlLikely(raw: string): boolean {
  if (/content-type:\s*text\/html/i.test(raw)) return true;
  if (/<#part\s+type\s*=\s*text\/html\s*>/i.test(raw)) return true;
  const tagHits = (raw.match(/<\/?(html|body|div|span|table|tr|td|a|img|p|br|h[1-6])\b/gi) || []).length;
  return tagHits >= 6;
}

function decodeBasicEntities(input: string): string {
  return input
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&quot;/gi, '"')
    .replace(/&#39;/gi, "'")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">");
}

function stripDangerousBlocks(input: string): { text: string; removedTags: number } {
  let removedTags = 0;
  let out = input;

  const blockPatterns = [
    /<script\b[\s\S]*?<\/script>/gi,
    /<style\b[\s\S]*?<\/style>/gi,
    /<iframe\b[\s\S]*?<\/iframe>/gi,
    /<object\b[\s\S]*?<\/object>/gi,
    /<embed\b[\s\S]*?>/gi,
    /<form\b[\s\S]*?<\/form>/gi,
    /<svg\b[\s\S]*?<\/svg>/gi,
    /<meta\b[^>]*>/gi,
    /<link\b[^>]*>/gi,
  ];

  for (const pattern of blockPatterns) {
    const m = out.match(pattern);
    if (m) removedTags += m.length;
    out = out.replace(pattern, " ");
  }

  const comments = out.match(/<!--[\s\S]*?-->/g);
  if (comments) removedTags += comments.length;
  out = out.replace(/<!--[\s\S]*?-->/g, " ");

  const onAttrs = out.match(/\son[a-z]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi);
  if (onAttrs) removedTags += onAttrs.length;
  out = out.replace(/\son[a-z]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, "");

  return { text: out, removedTags };
}

function normalizeUrl(rawUrl: string, stripTracking: boolean): { normalized: string; trackingParamsRemoved: number } {
  let urlText = rawUrl.trim();
  // common HTML-encoded leftovers from newsletter links
  urlText = urlText
    .replace(/&amp;/gi, "&")
    .replace(/amp%3B/gi, "")
    .replace(/\|+$/g, "")
    .replace(/[)>.,;]+$/g, "");
  if (!/^https?:\/\//i.test(urlText) && !/^mailto:/i.test(urlText)) {
    return { normalized: "", trackingParamsRemoved: 0 };
  }

  if (!stripTracking || /^mailto:/i.test(urlText)) {
    return { normalized: urlText, trackingParamsRemoved: 0 };
  }

  try {
    const u = new URL(urlText);
    let removed = 0;

    const keys = Array.from(u.searchParams.keys());
    for (const key of keys) {
      const k = key.toLowerCase();
      const remove =
        k.startsWith("utm_") ||
        k.startsWith("mc_") ||
        k.startsWith("mkt_") ||
        k.startsWith("vero_") ||
        TRACKING_KEYS_EXACT.has(k);
      if (remove) {
        u.searchParams.delete(key);
        removed += 1;
      }
    }

    const qs = u.searchParams.toString();
    const cleaned = `${u.origin}${u.pathname}${qs ? `?${qs}` : ""}${u.hash || ""}`;
    return { normalized: cleaned, trackingParamsRemoved: removed };
  } catch {
    return { normalized: urlText, trackingParamsRemoved: 0 };
  }
}

function dedupeRepeatingLinks(text: string): string {
  const seen = new Map<string, number>();

  return text.replace(/https?:\/\/[^\s)]+/gi, (url) => {
    const key = url.toLowerCase();
    const count = (seen.get(key) || 0) + 1;
    seen.set(key, count);

    // Keep first two occurrences, then collapse further duplicates.
    if (count <= 2) return url;
    if (count === 3) return `${url} [x${count}]`;
    return "";
  });
}

function sanitizeHtmlToRoutingText(html: string, options: SanitizeOptions): SanitizedMail {
  const originalChars = html.length;

  // Himalaya wrapper format: <#part type=text/html> ... <#/part>
  const unwrapped = html
    .replace(/<#part\b[^>]*>/gi, "\n")
    .replace(/<#\/part>/gi, "\n");

  const { text: stripped, removedTags } = stripDangerousBlocks(unwrapped);

  const hasUnsubscribe = /unsubscribe|abbestellen|austragen|manage preferences/i.test(stripped);
  const bulkLikely = /list-unsubscribe:|list-id:|precedence:\s*bulk|x-mailer/i.test(stripped);

  let linkCount = 0;
  let trackingParamsRemoved = 0;

  // keep anchor text and append normalized URL in parens
  let out = stripped.replace(/<a\b[^>]*href\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+))[^>]*>([\s\S]*?)<\/a>/gi, (_m, _h, d1, d2, d3, inner) => {
    const hrefRaw = (d1 || d2 || d3 || "").trim();
    const textInner = String(inner || "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
    const { normalized, trackingParamsRemoved: removed } = normalizeUrl(hrefRaw, options.stripTrackingParams);
    trackingParamsRemoved += removed;
    if (!normalized) return textInner || "";
    linkCount += 1;
    if (!textInner) return normalized;
    return `${textInner} (${normalized})`;
  });

  // image handling
  out = out.replace(/<img\b[^>]*alt\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+))[^>]*>/gi, (_m, _a, d1, d2, d3) => {
    const alt = (d1 || d2 || d3 || "").trim();
    return alt ? ` [IMG: ${alt}] ` : " ";
  });
  out = out.replace(/<img\b[^>]*>/gi, " ");

  // structural separators before removing tags
  out = out
    .replace(/<\s*br\s*\/?>/gi, "\n")
    .replace(/<\/(p|div|section|article|header|footer|h[1-6]|li|tr)\s*>/gi, "\n")
    .replace(/<(td|th)\b[^>]*>/gi, " ")
    .replace(/<\/(td|th)\s*>/gi, " | ");

  // remaining tags removed
  out = out.replace(/<[^>]+>/g, " ");
  out = decodeBasicEntities(out);

  // normalize plain URLs that are already visible in text (common in forwarded newsletters)
  out = out.replace(/https?:\/\/[^\s)]+/gi, (url) => {
    const normalized = normalizeUrl(url, options.stripTrackingParams);
    trackingParamsRemoved += normalized.trackingParamsRemoved;
    if (normalized.normalized) linkCount += 1;
    return normalized.normalized || "";
  });

  // optional aggressive footer trimming for newsletter/bulk mails
  const newsletterLikely = hasUnsubscribe || bulkLikely || /view in browser|newsletter|campaign|mailing list/i.test(out);
  if (options.trimNewsletterFooter && newsletterLikely) {
    const lines = out.split(/\r?\n/);
    const cutIndex = lines.findIndex((line) => /unsubscribe|abbestellen|manage preferences|impressum|privacy policy/i.test(line));
    if (cutIndex > 0) {
      out = lines.slice(0, cutIndex).join("\n");
    }
  }

  // whitespace/layout normalization
  out = out
    .replace(/(?:\|\s*){2,}/g, " ") // collapse newsletter table separators
    .replace(/[ \t]+/g, " ")
    .replace(/\n[ \t]*\n[ \t]*\n+/g, "\n\n")
    .replace(/\n[ \t]+/g, "\n")
    .replace(/[ \t]+\n/g, "\n")
    .trim();

  if (options.mode === "strict") {
    out = out
      .split(/\r?\n/)
      .filter((line) => !/click here|view online|read more|jetzt entdecken|zum produkt/i.test(line))
      .join("\n")
      .trim();
  }

  out = out
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line, idx, arr) => {
      if (!line) return idx > 0 && arr[idx - 1] !== "" ? true : false;
      return true;
    })
    .join("\n");

  out = dedupeRepeatingLinks(out)
    .replace(/\(\s*\)/g, "")
    .replace(/[ \t]+/g, " ")
    .replace(/\s+\)/g, ")")
    .replace(/\(\s+/g, "(")
    .replace(/\n{3,}/g, "\n\n")
    .trim();

  return {
    text: out,
    flags: {
      htmlLikely: true,
      newsletterLikely,
      hasUnsubscribe,
      bulkLikely,
    },
    meta: {
      originalChars,
      sanitizedChars: out.length,
      removedTags,
      linkCount,
      trackingParamsRemoved,
    },
  };
}

function sanitizePlainText(raw: string, options: SanitizeOptions): SanitizedMail {
  const originalChars = raw.length;
  let trackingParamsRemoved = 0;

  const out = raw.replace(/https?:\/\/[^\s)]+/gi, (url) => {
    const normalized = normalizeUrl(url, options.stripTrackingParams);
    trackingParamsRemoved += normalized.trackingParamsRemoved;
    return normalized.normalized || "";
  });

  const text = dedupeRepeatingLinks(out)
    .replace(/\(\s*\)/g, "")
    .replace(/[ \t]+/g, " ")
    .replace(/\s+\)/g, ")")
    .replace(/\(\s+/g, "(")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
  const hasUnsubscribe = /unsubscribe|abbestellen|austragen|manage preferences/i.test(text);
  const bulkLikely = /list-unsubscribe:|list-id:|precedence:\s*bulk/i.test(text);

  return {
    text,
    flags: {
      htmlLikely: false,
      newsletterLikely: hasUnsubscribe || bulkLikely,
      hasUnsubscribe,
      bulkLikely,
    },
    meta: {
      originalChars,
      sanitizedChars: text.length,
      removedTags: 0,
      linkCount: (text.match(/https?:\/\//gi) || []).length,
      trackingParamsRemoved,
    },
  };
}

export function sanitizeForRouting(raw: string, options: SanitizeOptions): SanitizedMail {
  if (!options.enabled || options.mode === "off") {
    const trimmed = raw.trim();
    return {
      text: trimmed,
      flags: {
        htmlLikely: isHtmlLikely(raw),
        newsletterLikely: /newsletter|unsubscribe|list-unsubscribe/i.test(raw),
        hasUnsubscribe: /unsubscribe|abbestellen|austragen/i.test(raw),
        bulkLikely: /list-unsubscribe:|list-id:|precedence:\s*bulk/i.test(raw),
      },
      meta: {
        originalChars: raw.length,
        sanitizedChars: trimmed.length,
        removedTags: 0,
        linkCount: (raw.match(/https?:\/\//gi) || []).length,
        trackingParamsRemoved: 0,
      },
    };
  }

  if (isHtmlLikely(raw)) {
    return sanitizeHtmlToRoutingText(raw, options);
  }
  return sanitizePlainText(raw, options);
}
