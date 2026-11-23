// Email bulletins in, scrubbed Atom feeds out.
//
//   email()  one message arrives -> decide (policy.ts) -> scrub (scrub.ts) -> KV
//   fetch()  GET /feeds/<list>.xml | /health | /
//
// The repository's build reads the feeds like any other source. Nothing here
// holds a GitHub credential, and nothing from a message is published unless
// it comes from a sender on that list's allowlist and passed DMARC.
import PostalMime from "postal-mime";

import { renderAtom } from "./feed";
import listsFile from "./lists.json";
import { decide, domainOf, findList, parseAuthResults, type ListConfig } from "./policy";
import { isSubscriptionManagement, toPublicItem, type PublicItem } from "./scrub";

export interface Env {
	MAIL: KVNamespace;
	/** "true" while lists are being set up: unrecognised mail is forwarded privately instead of refused. */
	ONBOARDING?: string;
	/** Secret. A destination address verified in Email Routing. Never commit it. */
	FORWARD_TO?: string;
}

const LISTS = (listsFile as { lists: ListConfig[] }).lists;
const FEED_ITEMS = 50;
const MAX_MESSAGE_BYTES = 5 * 1024 * 1024;
const RAW_TTL_SECONDS = 365 * 24 * 3600;
const ITEM_TTL_SECONDS = 2 * 365 * 24 * 3600;

/** Keys sort oldest-last, so a prefix listing returns the newest items first. */
function itemKey(listId: string, item: PublicItem): string {
	const reversed = String(9_999_999_999_999 - Date.parse(item.published)).padStart(13, "0");
	return `item:${listId}:${reversed}:${item.id}`;
}

async function forwardPrivately(message: ForwardableEmailMessage, env: Env, reason: string): Promise<void> {
	if (!env.FORWARD_TO) {
		console.log(`not forwarded (FORWARD_TO unset): ${reason}`);
		return;
	}
	try {
		await message.forward(env.FORWARD_TO);
		console.log(`forwarded privately: ${reason}`);
	} catch (err) {
		// e.g. the destination is not verified yet. The message is not lost to
		// a bounce: it simply is not published.
		console.log(`forward failed (${reason}): ${err instanceof Error ? err.message : String(err)}`);
	}
}

export async function handleEmail(
	message: ForwardableEmailMessage,
	env: Env,
	lists: ListConfig[] = LISTS,
	now: Date = new Date(),
): Promise<string> {
	const list = findList(lists, message.to);
	if (!list) {
		message.setReject("Unknown recipient");
		return "reject: unknown recipient";
	}
	if (message.rawSize > MAX_MESSAGE_BYTES) {
		message.setReject("Message too large");
		return "reject: too large";
	}

	const raw = await new Response(message.raw).arrayBuffer();
	const mail = await PostalMime.parse(raw);
	const headerFromDomain = domainOf(mail.from?.address ?? "");
	const auth = parseAuthResults(message.headers.get("authentication-results"));
	const management = isSubscriptionManagement(mail.subject ?? "", mail.text ?? "");
	const onboarding = env.ONBOARDING === "true";

	const decision = decide({ list, headerFromDomain, auth, management, onboarding });
	// Sender domains are the one thing needed to finish setting a list up, so
	// they are logged; message content never is.
	console.log(
		JSON.stringify({ list: list.id, from: headerFromDomain, dmarc: auth.dmarc ?? null, trusted: auth.trusted, action: decision.action }),
	);

	if (decision.action === "reject") {
		message.setReject("Not accepted");
		return `reject: ${decision.reason}`;
	}
	if (decision.action === "forward") {
		await forwardPrivately(message, env, decision.reason);
		return `forward: ${decision.reason}`;
	}

	const item = await toPublicItem(
		{ subject: mail.subject, text: mail.text, html: mail.html, date: mail.date, messageId: mail.messageId },
		{ listId: list.id, recipient: message.to, fallbackLink: list.link, now },
	);
	const rawKey = `raw:${list.id}:${item.id}`;
	if ((await env.MAIL.get(rawKey)) !== null) return "duplicate";

	// The untouched message is kept privately for a year as the record of what
	// was received; only the scrubbed item is ever served.
	await env.MAIL.put(rawKey, raw, { expirationTtl: RAW_TTL_SECONDS });
	await env.MAIL.put(itemKey(list.id, item), JSON.stringify(item), { expirationTtl: ITEM_TTL_SECONDS });
	await env.MAIL.put(`meta:${list.id}`, JSON.stringify({ last_received: now.toISOString() }));
	return `publish: ${item.id}`;
}

async function loadItems(env: Env, listId: string): Promise<PublicItem[]> {
	const listing = await env.MAIL.list({ prefix: `item:${listId}:`, limit: FEED_ITEMS });
	const values = await Promise.all(listing.keys.map((k) => env.MAIL.get(k.name)));
	return values.filter((v): v is string => v !== null).map((v) => JSON.parse(v) as PublicItem);
}

async function etagFor(body: string): Promise<string> {
	const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(body));
	return `"${[...new Uint8Array(digest)].slice(0, 12).map((b) => b.toString(16).padStart(2, "0")).join("")}"`;
}

export async function handleFetch(request: Request, env: Env, lists: ListConfig[] = LISTS, now: Date = new Date()): Promise<Response> {
	if (request.method !== "GET" && request.method !== "HEAD") {
		return new Response("Method not allowed\n", { status: 405, headers: { Allow: "GET, HEAD" } });
	}
	const url = new URL(request.url);

	const feed = url.pathname.match(/^\/feeds\/([a-z0-9-]+)\.xml$/);
	if (feed) {
		const list = lists.find((l) => l.id === feed[1]);
		if (!list) return new Response("No such feed\n", { status: 404 });
		const body = renderAtom(list, await loadItems(env, list.id), `${url.origin}${url.pathname}`);
		const etag = await etagFor(body);
		const headers = {
			"Content-Type": "application/atom+xml; charset=utf-8",
			"Cache-Control": "public, max-age=300",
			"X-Content-Type-Options": "nosniff",
			ETag: etag,
		};
		if (request.headers.get("If-None-Match") === etag) return new Response(null, { status: 304, headers });
		return new Response(request.method === "HEAD" ? null : body, { headers });
	}

	if (url.pathname === "/health") {
		const report = await Promise.all(
			lists.map(async (l) => {
				const meta = JSON.parse((await env.MAIL.get(`meta:${l.id}`)) ?? "{}") as { last_received?: string };
				const last = meta.last_received ?? null;
				const quietDays = l.quietDays ?? 45;
				const quiet = last === null || now.getTime() - Date.parse(last) > quietDays * 86_400_000;
				return { id: l.id, last_received: last, quiet_after_days: quietDays, quiet };
			}),
		);
		return Response.json({ lists: report, quiet: report.filter((r) => r.quiet).map((r) => r.id) }, { headers: { "Cache-Control": "no-store" } });
	}

	if (url.pathname === "/") {
		const lines = lists.map((l) => `${url.origin}/feeds/${l.id}.xml  ${l.title}`);
		return new Response(`Maine Government Feeds: email bulletins republished as feeds.\n\n${lines.join("\n")}\n`, {
			headers: { "Content-Type": "text/plain; charset=utf-8" },
		});
	}
	return new Response("Not found\n", { status: 404 });
}

export default {
	async email(message, env, _ctx): Promise<void> {
		try {
			console.log(await handleEmail(message, env));
		} catch (err) {
			// Fail closed: an unexpected error never publishes anything. It
			// also must not refuse the message: setReject is a permanent
			// bounce, and bouncing a list's mail gets the address dropped
			// from the list. Hand the message to the maintainer instead.
			console.error(`email handler failed: ${err instanceof Error ? err.stack : String(err)}`);
			await forwardPrivately(message, env, "processing error");
		}
	},
	async fetch(request, env, _ctx): Promise<Response> {
		return handleFetch(request, env);
	},
} satisfies ExportedHandler<Env>;
