import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { renderAtom, xmlEscape } from "../src/feed";
import { handleEmail, handleFetch, type Env } from "../src/index";
import { decide, findList, localPartOf, parseAuthResults, senderAllowed, type ListConfig } from "../src/policy";
import { htmlToText, isSubscriptionManagement, scrubText, toPublicItem, unwrapTracking } from "../src/scrub";

const fixture = (name: string) => readFileSync(new URL(`./fixtures/${name}`, import.meta.url), "utf8").replace(/\r?\n/g, "\r\n");

const LISTS: ListConfig[] = [
	{ id: "dep-news", title: "Maine DEP News (by email)", address: "dep-news", senders: ["govdelivery.com"], link: "https://www.maine.gov/dep/news/", quietDays: 30 },
	{ id: "mrs-alerts", title: "Maine Tax Alerts (by email)", address: "mrs-alerts", senders: ["maine.gov"], link: "https://www.maine.gov/revenue/" },
	{ id: "new-list", title: "Not set up yet", address: "new-list", senders: [], link: "https://example.org/" },
];
const NOW = new Date("2026-11-23T18:00:00Z");

/** In-memory stand-in for the KV binding (only the calls the Worker makes). */
function fakeKV() {
	const store = new Map<string, string | ArrayBuffer>();
	return {
		store,
		async get(key: string) {
			const v = store.get(key);
			return v === undefined ? null : typeof v === "string" ? v : "[binary]";
		},
		async put(key: string, value: string | ArrayBuffer) {
			store.set(key, value);
		},
		async list({ prefix, limit }: { prefix: string; limit: number }) {
			const keys = [...store.keys()].filter((k) => k.startsWith(prefix)).sort().slice(0, limit);
			return { keys: keys.map((name) => ({ name })) };
		},
	};
}

function fakeMessage(raw: string, to: string, overrides: { headers?: Record<string, string>; forwardFails?: boolean } = {}) {
	const headerBlock = raw.split("\r\n\r\n")[0];
	const headers = new Headers();
	for (const line of headerBlock.split("\r\n")) {
		const m = line.match(/^([A-Za-z-]+):\s*(.*)$/);
		if (m) headers.append(m[1], m[2]);
	}
	for (const [k, v] of Object.entries(overrides.headers ?? {})) headers.set(k, v);
	const state = { rejected: null as string | null, forwardedTo: [] as string[] };
	const message = {
		from: "bounce@sender.example",
		to,
		headers,
		rawSize: raw.length,
		raw: new Response(raw).body!,
		canBeForwarded: true,
		setReject(reason: string) {
			state.rejected = reason;
		},
		async forward(rcpt: string) {
			if (overrides.forwardFails) throw new Error("destination address not verified");
			state.forwardedTo.push(rcpt);
		},
		async reply() {},
	} as unknown as ForwardableEmailMessage;
	return { message, state };
}

function env(extra: Partial<Env> = {}) {
	const kv = fakeKV();
	return { kv, env: { MAIL: kv as unknown as KVNamespace, ...extra } as Env };
}

describe("scrubbing", () => {
	it("removes everything that identifies or controls the subscriber", async () => {
		const { default: PostalMime } = await import("postal-mime");
		const mail = await PostalMime.parse(fixture("govdelivery-bulletin.eml"));
		const item = await toPublicItem(mail, { listId: "dep-news", recipient: "dep-news@mail.example.org", fallbackLink: "https://fallback/", now: NOW });

		expect(item.title).toBe("Maine DEP Issues Air Quality Alert for Coastal Counties");
		expect(item.link).toBe("https://content.govdelivery.com/accounts/MEDEP/bulletins/41a9c3f");
		expect(item.published).toBe("2026-11-18T14:15:00.000Z");
		expect(item.text).toContain("forecasting elevated particle pollution");
		expect(item.text).toContain("David R. Madore");
		// No leftovers: the body opens with the headline, not the bulletin URL,
		// and a label whose link was removed goes with it.
		expect(item.text.startsWith("Maine DEP Issues Air Quality Alert")).toBe(true);
		expect(item.text).not.toContain("Short link");
		expect(item.text.endsWith("(207) 287-5842")).toBe(true);
		// The click-tracking wrapper is replaced by the page it points to.
		expect(item.text).toContain("https://www.maine.gov/dep/air/ozone/");

		for (const forbidden of [
			"dep-news@mail.example.org", "unsubscribe", "Unsubscribe", "verification=", "destination=", "subscriber",
			"links-1.govdelivery.com", "lnks.gd", "Manage Subscriptions", "SUBSCRIBER SERVICES", "This email was sent to",
			"Having trouble viewing",
		]) {
			expect(item.text, `must not contain ${forbidden}`).not.toContain(forbidden);
		}
	});

	it("handles an HTML-only message: text extracted, scripts dropped, footer cut", async () => {
		const { default: PostalMime } = await import("postal-mime");
		const mail = await PostalMime.parse(fixture("html-only.eml"));
		const item = await toPublicItem(mail, { listId: "mrs-alerts", recipient: "mrs-alerts@mail.example.org", fallbackLink: "https://www.maine.gov/revenue/", now: NOW });

		expect(item.title).toBe("Maine Tax Alert – November 2026");
		expect(item.link).toBe("https://www.maine.gov/revenue/"); // no public bulletin URL in the message
		expect(item.text).toContain("Maine Tax Alert is now available & includes:");
		expect(item.text).toContain("- Rule 801 amendments");
		expect(item.text).toContain("Read the alert (https://www.maine.gov/revenue/publications/maine-tax-alerts)");
		expect(item.text).not.toMatch(/alert\("x"\)|jane\.subscriber|unsubscribe|mrs-alerts@/i);
	});

	it("ids are stable per message and distinct across lists", async () => {
		const mail = { subject: "S", text: "body", messageId: "<m1@x>", date: "2026-11-01T00:00:00Z" };
		const a = await toPublicItem(mail, { listId: "one", recipient: "a@b", fallbackLink: "https://x/", now: NOW });
		const again = await toPublicItem(mail, { listId: "one", recipient: "a@b", fallbackLink: "https://x/", now: NOW });
		const other = await toPublicItem(mail, { listId: "two", recipient: "a@b", fallbackLink: "https://x/", now: NOW });
		expect(a.id).toBe(again.id);
		expect(a.id).not.toBe(other.id);
		expect(a.id).toMatch(/^[0-9a-f]{20}$/);
	});

	it("never trusts a future or missing date, and caps the body", async () => {
		const future = await toPublicItem({ subject: "S", text: "x", date: "2031-01-01T00:00:00Z" }, { listId: "l", recipient: "a@b", fallbackLink: "https://x/", now: NOW });
		expect(future.published).toBe(NOW.toISOString());
		const long = await toPublicItem({ subject: "", text: "word ".repeat(5000) }, { listId: "l", recipient: "a@b", fallbackLink: "https://x/", now: NOW });
		expect(long.text.length).toBeLessThanOrEqual(8000);
		expect(long.title).toBe("(no subject)");
	});

	it("helpers", () => {
		expect(unwrapTracking("see https://links.govdelivery.com/CL0/https:%2F%2Fexample.gov%2Fa%3Fb=1/2/0100-x/sig=1 now")).toBe("see https://example.gov/a?b=1 now");
		expect(scrubText("keep\nmail me at Me@Example.org please\nkeep2", "me@example.org")).toBe("keep\nkeep2");
		expect(scrubText("a \x0b b\x00", "")).toBe("a b");
		expect(htmlToText('<p>A &amp; B &#8211; C&nbsp;D</p><a href="mailto:x@y">mail</a> <a href="https://e.org">https://e.org</a>').replace(/\s+/g, " ").trim()).toBe(
			"A & B – C D mail https://e.org",
		);
		expect(isSubscriptionManagement("Please confirm your subscription", "")).toBe(true);
		expect(isSubscriptionManagement("Air Quality Alert", "The department confirms the forecast.")).toBe(false);
	});
});

describe("who may publish", () => {
	it("trusts only Cloudflare's own Authentication-Results", () => {
		expect(parseAuthResults("mx.cloudflare.net; spf=pass; dkim=pass; dmarc=pass")).toEqual({ trusted: true, spf: "pass", dkim: "pass", dmarc: "pass" });
		expect(parseAuthResults("mx.cloudflare.net; spf=pass; dkim=none; dmarc=fail")).toMatchObject({ trusted: true, dmarc: "fail" });
		// A header the sender wrote proves nothing.
		expect(parseAuthResults("mx.evil.example; dmarc=pass")).toEqual({ trusted: false });
		expect(parseAuthResults("spoof.mx.cloudflare.net.evil.example; dmarc=pass")).toEqual({ trusted: false });
		expect(parseAuthResults(null)).toEqual({ trusted: false });
		// Headers.get() joins repeats in order: Cloudflare's comes first, and a
		// later sender-supplied "pass" must not override its "fail".
		expect(parseAuthResults("mx.cloudflare.net; spf=fail; dkim=fail; dmarc=fail, mx.evil.example; dmarc=pass").dmarc).toBe("fail");
	});

	it("matches recipients and sender domains exactly", () => {
		expect(localPartOf("DEP-News+tag@mail.example.org")).toBe("dep-news");
		expect(findList(LISTS, "dep-news+x@mail.example.org")?.id).toBe("dep-news");
		expect(findList(LISTS, "dep-news2@mail.example.org")).toBeUndefined();
		expect(senderAllowed("public.govdelivery.com", ["govdelivery.com"])).toBe(true);
		expect(senderAllowed("govdelivery.com", ["govdelivery.com"])).toBe(true);
		expect(senderAllowed("evilgovdelivery.com", ["govdelivery.com"])).toBe(false);
		expect(senderAllowed("govdelivery.com.evil.example", ["govdelivery.com"])).toBe(false);
		expect(senderAllowed("anything.example", [])).toBe(false);
		expect(senderAllowed("", ["", " "])).toBe(false);
	});

	it("decision table", () => {
		const list = LISTS[0];
		const pass = { trusted: true, dmarc: "pass" };
		const base = { list, headerFromDomain: "public.govdelivery.com", auth: pass, management: false, onboarding: false };
		expect(decide(base)).toEqual({ action: "publish" });
		expect(decide({ ...base, management: true }).action).toBe("forward");
		// The list's own sender is never bounced, and never published unauthenticated.
		expect(decide({ ...base, auth: { trusted: true, dmarc: "fail" } }).action).toBe("forward");
		expect(decide({ ...base, auth: { trusted: false } }).action).toBe("forward");
		expect(decide({ ...base, headerFromDomain: "spammer.example" }).action).toBe("reject");
		expect(decide({ ...base, headerFromDomain: "spammer.example", onboarding: true }).action).toBe("forward");
		expect(decide({ ...base, list: undefined, onboarding: true }).action).toBe("reject");
		// A list with no allowlist yet can never publish.
		expect(decide({ ...base, list: LISTS[2], onboarding: true }).action).toBe("forward");
		expect(decide({ ...base, list: LISTS[2] }).action).toBe("reject");
	});
});

describe("email handler", () => {
	const bulletin = fixture("govdelivery-bulletin.eml");

	it("publishes an authenticated bulletin once, keeping the raw copy private", async () => {
		const { kv, env: e } = env();
		const first = fakeMessage(bulletin, "dep-news@mail.example.org");
		expect(await handleEmail(first.message, e, LISTS, NOW)).toMatch(/^publish: [0-9a-f]{20}$/);
		expect(first.state).toEqual({ rejected: null, forwardedTo: [] });

		const keys = [...kv.store.keys()].sort();
		expect(keys.map((k) => k.split(":")[0])).toEqual(["item", "meta", "raw"]);
		const stored = JSON.parse(kv.store.get(keys[0]) as string);
		expect(stored.title).toContain("Air Quality Alert");
		expect(JSON.stringify(stored)).not.toMatch(/dep-news@mail\.example\.org|verification=|links-1\.govdelivery/);

		const second = fakeMessage(bulletin, "dep-news@mail.example.org");
		expect(await handleEmail(second.message, e, LISTS, NOW)).toBe("duplicate");
		expect([...kv.store.keys()].length).toBe(3);
	});

	it("never publishes a spoofed or unauthenticated message", async () => {
		for (const authHeader of ["mx.cloudflare.net; spf=fail; dkim=fail; dmarc=fail", "mx.evil.example; spf=pass; dkim=pass; dmarc=pass"]) {
			const { kv, env: e } = env({ FORWARD_TO: "owner@private.example" });
			const m = fakeMessage(bulletin, "dep-news@mail.example.org", { headers: { "Authentication-Results": authHeader } });
			expect(await handleEmail(m.message, e, LISTS, NOW)).toMatch(/^forward: sender allowed but DMARC/);
			expect(kv.store.size).toBe(0);
			expect(m.state).toEqual({ rejected: null, forwardedTo: ["owner@private.example"] });
		}
	});

	it("refuses unknown recipients and, outside onboarding, unknown senders", async () => {
		const { kv, env: e } = env({ FORWARD_TO: "owner@private.example" });
		const unknownRcpt = fakeMessage(bulletin, "nobody@mail.example.org");
		expect(await handleEmail(unknownRcpt.message, e, LISTS, NOW)).toBe("reject: unknown recipient");
		expect(unknownRcpt.state.rejected).toBe("Unknown recipient");

		const wrongList = fakeMessage(bulletin, "mrs-alerts@mail.example.org"); // govdelivery is not allowed on this list
		expect(await handleEmail(wrongList.message, e, LISTS, NOW)).toMatch(/^reject: sender public\.govdelivery\.com not on/);
		expect(wrongList.state).toEqual({ rejected: "Not accepted", forwardedTo: [] });
		expect(kv.store.size).toBe(0);
	});

	it("in onboarding, forwards unrecognised mail privately and still publishes nothing", async () => {
		const { kv, env: e } = env({ ONBOARDING: "true", FORWARD_TO: "owner@private.example" });
		const m = fakeMessage(bulletin, "new-list@mail.example.org");
		expect(await handleEmail(m.message, e, LISTS, NOW)).toMatch(/^forward: sender public\.govdelivery\.com not on/);
		expect(m.state).toEqual({ rejected: null, forwardedTo: ["owner@private.example"] });
		expect(kv.store.size).toBe(0);

		// No destination configured, or one that is not verified yet: neither
		// publishes, bounces, nor throws.
		for (const [extra, opts] of [[{ ONBOARDING: "true" }, {}], [{ ONBOARDING: "true", FORWARD_TO: "x@y.example" }, { forwardFails: true }]] as const) {
			const ctx = env(extra);
			const msg = fakeMessage(bulletin, "new-list@mail.example.org", opts);
			expect(await handleEmail(msg.message, ctx.env, LISTS, NOW)).toMatch(/^forward:/);
			expect(msg.state).toEqual({ rejected: null, forwardedTo: [] });
			expect(ctx.kv.store.size).toBe(0);
		}
	});

	it("sends a subscription confirmation to a person instead of the feed", async () => {
		const confirm = bulletin.replace("Subject: Maine DEP Issues Air Quality Alert for Coastal Counties", "Subject: Please confirm your subscription");
		const { kv, env: e } = env({ FORWARD_TO: "owner@private.example" });
		const m = fakeMessage(confirm, "dep-news@mail.example.org");
		expect(await handleEmail(m.message, e, LISTS, NOW)).toBe("forward: subscription management message");
		expect(m.state.forwardedTo).toEqual(["owner@private.example"]);
		expect(kv.store.size).toBe(0);
	});

	it("refuses oversized messages before reading them", async () => {
		const { env: e } = env();
		const m = fakeMessage(bulletin, "dep-news@mail.example.org");
		(m.message as unknown as { rawSize: number }).rawSize = 6 * 1024 * 1024;
		expect(await handleEmail(m.message, e, LISTS, NOW)).toBe("reject: too large");
	});
});

describe("feeds", () => {
	async function seeded() {
		const ctx = env();
		const bulletin = fixture("govdelivery-bulletin.eml");
		await handleEmail(fakeMessage(bulletin, "dep-news@mail.example.org").message, ctx.env, LISTS, NOW);
		const newer = bulletin
			.replace("<0101019a2b3c4d5e-6f7a8b9c@govdelivery.example>", "<second@govdelivery.example>")
			.replace("Subject: Maine DEP Issues Air Quality Alert for Coastal Counties", "Subject: Newer <bulletin> & \"quotes\"")
			.replace("Date: Wed, 18 Nov 2026 09:15:00 -0500", "Date: Fri, 20 Nov 2026 09:15:00 -0500");
		await handleEmail(fakeMessage(newer, "dep-news@mail.example.org").message, ctx.env, LISTS, NOW);
		return ctx;
	}

	it("serves a well-formed Atom feed, newest first, with conditional requests", async () => {
		const { env: e } = await seeded();
		const res = await handleFetch(new Request("https://mail.example.org/feeds/dep-news.xml"), e, LISTS, NOW);
		expect(res.status).toBe(200);
		expect(res.headers.get("Content-Type")).toContain("application/atom+xml");
		const xml = await res.text();
		expect(xml.indexOf("Newer &lt;bulletin&gt; &amp; &quot;quotes&quot;")).toBeGreaterThan(0);
		expect(xml.indexOf("Newer &lt;bulletin&gt;")).toBeLessThan(xml.indexOf("Air Quality Alert for Coastal Counties</title>"));
		expect(xml.match(/<entry>/g)?.length).toBe(2);
		expect(xml).toContain("<updated>2026-11-20T14:15:00.000Z</updated>");
		expect(xml).toContain('<link rel="self" href="https://mail.example.org/feeds/dep-news.xml"/>');
		expect(xml).not.toMatch(/dep-news@mail\.example\.org|unsubscribe|verification=/i);
		// Every & in the document starts a known entity.
		expect(xml.replace(/&(amp|lt|gt|quot);/g, "")).not.toContain("&");

		const etag = res.headers.get("ETag")!;
		const again = await handleFetch(new Request("https://mail.example.org/feeds/dep-news.xml", { headers: { "If-None-Match": etag } }), e, LISTS, NOW);
		expect(again.status).toBe(304);
		expect(await again.text()).toBe("");
	});

	it("serves an empty but valid feed for a list with no mail yet, and 404s unknown ones", async () => {
		const { env: e } = env();
		const empty = await handleFetch(new Request("https://m.example/feeds/mrs-alerts.xml"), e, LISTS, NOW);
		expect(empty.status).toBe(200);
		const xml = await empty.text();
		expect(xml).toContain("<feed xmlns=\"http://www.w3.org/2005/Atom\">");
		expect(xml).not.toContain("<entry>");
		expect((await handleFetch(new Request("https://m.example/feeds/nope.xml"), e, LISTS, NOW)).status).toBe(404);
		expect((await handleFetch(new Request("https://m.example/feeds/../raw"), e, LISTS, NOW)).status).toBe(404);
		expect((await handleFetch(new Request("https://m.example/feeds/dep-news.xml", { method: "POST" }), e, LISTS, NOW)).status).toBe(405);
	});

	it("reports quiet lists on /health and never exposes raw mail over HTTP", async () => {
		const { env: e, kv } = await seeded();
		const health = (await (await handleFetch(new Request("https://m.example/health"), e, LISTS, NOW)).json()) as { quiet: string[]; lists: { id: string; last_received: string | null }[] };
		expect(health.quiet).toEqual(["mrs-alerts", "new-list"]);
		expect(health.lists[0]).toMatchObject({ id: "dep-news", last_received: NOW.toISOString() });
		const later = new Date(NOW.getTime() + 31 * 86_400_000);
		const stale = (await (await handleFetch(new Request("https://m.example/health"), e, LISTS, later)).json()) as { quiet: string[] };
		expect(stale.quiet).toContain("dep-news");

		const rawKey = [...kv.store.keys()].find((k) => k.startsWith("raw:"))!;
		for (const path of [`/${rawKey}`, `/raw/${rawKey.split(":")[2]}`, "/meta:dep-news", "/item"]) {
			expect((await handleFetch(new Request(`https://m.example${path}`), e, LISTS, NOW)).status).toBe(404);
		}
		const index = await (await handleFetch(new Request("https://m.example/"), e, LISTS, NOW)).text();
		expect(index).toContain("https://m.example/feeds/dep-news.xml");
	});

	it("escapes and strips control characters", () => {
		expect(xmlEscape('a<b>&"c\x0b\x00')).toBe("a&lt;b&gt;&amp;&quot;c  ");
		const xml = renderAtom(LISTS[0], [{ id: "i", title: "T\x1f", text: "x", link: "https://e/?a=1&b=2", published: "2026-01-01T00:00:00.000Z" }], "https://s/");
		expect(xml).toContain('href="https://e/?a=1&amp;b=2"');
		expect(xml).not.toMatch(/[\x00-\x08\x0b\x0c\x0e-\x1f]/);
	});
});
