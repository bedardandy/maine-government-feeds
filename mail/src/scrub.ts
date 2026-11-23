// Turning one received bulletin into what is safe to publish.
//
// A list email is addressed to one subscriber. It carries that subscriber's
// address, per-subscriber unsubscribe and "manage preferences" links, and
// click-tracking links that identify the subscriber. None of that may reach a
// public feed: anyone could unsubscribe the address or act as the subscriber.
// Everything here is pure (no Worker APIs) so it is tested in plain Node.

export interface ParsedMail {
	subject?: string;
	text?: string;
	html?: string;
	date?: string;
	messageId?: string;
}

export interface PublicItem {
	id: string;
	title: string;
	text: string;
	link: string;
	published: string;
}

const MAX_TEXT_CHARS = 8000;

// Hosts whose links exist to track or manage one subscriber.
const TRACKING_HOST =
	/(^|\.)(links(-\d+)?\.govdelivery\.com|lnks\.gd|click\.[a-z0-9.-]+|list-manage\.com|sendgrid\.net|mailchi\.mp)$/i;

// A link is subscriber-specific when its host is a tracker or its path/query
// names a subscriber action.
const SUBSCRIBER_PATH =
	/(unsubscribe|subscriber|preferences|manage[_-]?subscription|opt[_-]?out|verification=|destination=|[?&](email|e|u|uid|sid|token)=)/i;

// The public, shareable copy of a GovDelivery bulletin.
const PUBLIC_BULLETIN = /https:\/\/content\.govdelivery\.com\/accounts\/[A-Za-z0-9_]+\/bulletins\/[A-Za-z0-9]+/;

// GovDelivery click-tracking wraps the real destination, percent-encoded:
//   https://links-1.govdelivery.com/CL0/https:%2F%2Fwww.maine.gov%2Fdep%2F/1/0100.../sig=429
const WRAPPED_LINK = /https?:\/\/links(?:-\d+)?\.govdelivery\.com\/CL0\/(https?:%2F%2F[^\/\s"'<>]+)\/[^\s"'<>]*/gi;

const URL_RE = /https?:\/\/[^\s"'<>)\]]+/gi;

// Once one of these appears, the rest of the message is subscriber housekeeping.
const FOOTER_MARKERS = [
	/^\s*subscriber services\b/i,
	/^\s*stay connected with\b/i,
	/this (e-?mail|message) was sent to\b/i,
	/update your subscriptions?, modify your password/i,
	/you (are|were) (receiving|subscribed)\b/i,
	/to (stop receiving|unsubscribe)\b/i,
	/^\s*manage (your )?(subscriptions?|preferences)\b/i,
	/^\s*unsubscribe( all)?\s*$/i,
];

const DROP_LINE = [
	/having trouble viewing this (e-?mail|message)/i,
	/view (it|this (e-?mail|message)) (as a web ?page|in (your|a) browser)/i,
	/^\s*bookmark and share\s*$/i,
];

const ENTITIES: Record<string, string> = {
	amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " ", ndash: "–", mdash: "—",
	rsquo: "’", lsquo: "‘", rdquo: "”", ldquo: "“", bull: "•", middot: "·", hellip: "…", copy: "©",
};

export function decodeEntities(s: string): string {
	return s.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (whole, body: string) => {
		if (body[0] === "#") {
			const code = body[1].toLowerCase() === "x" ? parseInt(body.slice(2), 16) : parseInt(body.slice(1), 10);
			return Number.isFinite(code) && code > 0 && code < 0x110000 ? String.fromCodePoint(code) : "";
		}
		return ENTITIES[body.toLowerCase()] ?? whole;
	});
}

/** HTML to readable text. Links become "text (url)" so their targets survive. */
export function htmlToText(html: string): string {
	let s = html
		.replace(/<!--[\s\S]*?-->/g, "")
		.replace(/<(script|style|head|title)\b[\s\S]*?<\/\1\s*>/gi, "")
		.replace(/<img\b[^>]*>/gi, "");
	s = s.replace(/<a\b[^>]*?href\s*=\s*("([^"]*)"|'([^']*)')[^>]*>([\s\S]*?)<\/a\s*>/gi, (_m, _q, d: string, sq: string, inner: string) => {
		const href = decodeEntities((d ?? sq ?? "").trim());
		const label = inner.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
		if (!/^https?:/i.test(href)) return label;
		return label && label !== href ? `${label} (${href})` : href;
	});
	s = s
		.replace(/<br\s*\/?>/gi, "\n")
		.replace(/<li\b[^>]*>/gi, "\n- ")
		.replace(/<\/(p|div|tr|table|h[1-6]|li|ul|ol|blockquote|section|article|header|footer)\s*>/gi, "\n")
		.replace(/<[^>]+>/g, " ");
	return decodeEntities(s);
}

function hostOf(url: string): string {
	try {
		return new URL(url).hostname;
	} catch {
		return "";
	}
}

function isSubscriberLink(url: string): boolean {
	const host = hostOf(url);
	return !host || TRACKING_HOST.test(host) || SUBSCRIBER_PATH.test(url);
}

/** Replace tracking wrappers by their real destination where it is recoverable. */
export function unwrapTracking(s: string): string {
	return s.replace(WRAPPED_LINK, (whole, encoded: string) => {
		try {
			return decodeURIComponent(encoded);
		} catch {
			return whole;
		}
	});
}

function stripControl(s: string): string {
	// XML 1.0 cannot carry these; one of them in a feed breaks every reader.
	// eslint-disable-next-line no-control-regex
	return s.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]/g, " ");
}

export function scrubText(raw: string, recipient: string): string {
	const out: string[] = [];
	const local = recipient.toLowerCase();
	for (const original of stripControl(unwrapTracking(raw)).replace(/\r\n?/g, "\n").split("\n")) {
		if (FOOTER_MARKERS.some((re) => re.test(original))) break;
		if (DROP_LINE.some((re) => re.test(original))) continue;
		let removed = false;
		let line = original.replace(URL_RE, (url) => {
			// The public bulletin URL is the item link; repeating it as a bare
			// line at the top of the body adds nothing.
			const drop = isSubscriberLink(url) || (PUBLIC_BULLETIN.test(url) && original.trim() === url);
			removed ||= drop;
			return drop ? "" : url;
		});
		line = line.replace(/\(\s*\)/g, "").replace(/[ \t\u00a0]+/g, " ").trim();
		if (local && line.toLowerCase().includes(local)) continue;
		// A label left dangling once its link is gone ("Short link:").
		if (removed && /^[^.!?]{0,40}:?$/.test(line)) continue;
		out.push(line);
	}
	const text = out.join("\n").replace(/\n{3,}/g, "\n\n").trim();
	return text.slice(0, MAX_TEXT_CHARS);
}

async function sha256Hex(s: string): Promise<string> {
	const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
	return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** Messages that manage the subscription itself: a person must act on them. */
export function isSubscriptionManagement(subject: string, text: string): boolean {
	const head = `${subject}\n${text.slice(0, 600)}`;
	return /(confirm (your )?(subscription|e-?mail|address)|verify your (e-?mail|address)|subscription (confirmation|request)|activate your (account|subscription)|welcome to .{0,60}(subscription|updates|list)|you have (been )?(successfully )?(subscribed|unsubscribed)|password reset)/i.test(
		head,
	);
}

export async function toPublicItem(
	mail: ParsedMail,
	opts: { listId: string; recipient: string; fallbackLink: string; now?: Date },
): Promise<PublicItem> {
	const source = mail.text && mail.text.trim() ? mail.text : htmlToText(mail.html ?? "");
	const everything = unwrapTracking(`${mail.text ?? ""}\n${mail.html ?? ""}`);
	const bulletin = everything.match(PUBLIC_BULLETIN)?.[0];
	const title = stripControl((mail.subject ?? "").replace(/^\s*((re|fwd?|fw):\s*)+/i, "").replace(/\s+/g, " ").trim());
	const parsedDate = mail.date ? new Date(mail.date) : null;
	const now = opts.now ?? new Date();
	const published = parsedDate && !Number.isNaN(parsedDate.getTime()) && parsedDate <= now ? parsedDate : now;
	const key = mail.messageId?.trim() || `${title}|${published.toISOString()}`;
	return {
		id: (await sha256Hex(`${opts.listId}|${key}`)).slice(0, 20),
		title: title || "(no subject)",
		text: scrubText(source, opts.recipient),
		link: bulletin ?? opts.fallbackLink,
		published: published.toISOString(),
	};
}
