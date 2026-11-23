// Who may publish. Pure, so the whole decision table is unit-tested.

export interface ListConfig {
	id: string;
	title: string;
	address: string;
	senders: string[];
	link: string;
	quietDays?: number;
}

export interface AuthResults {
	trusted: boolean;
	spf?: string;
	dkim?: string;
	dmarc?: string;
}

export type Decision =
	| { action: "publish" }
	| { action: "forward"; reason: string }
	| { action: "reject"; reason: string };

/**
 * Read Cloudflare's verdict. Email Routing prepends its own
 * Authentication-Results header (authserv-id `mx.cloudflare.net`); any other
 * such header was written by the sender and proves nothing. `Headers.get`
 * joins repeated headers in order, so the trusted one is whatever the value
 * starts with, and only the first result for each method is read.
 */
export function parseAuthResults(header: string | null): AuthResults {
	if (!header) return { trusted: false };
	const value = header.trim();
	if (!/^mx\.cloudflare\.net\s*(;|$)/i.test(value)) return { trusted: false };
	const first = (method: string) => value.match(new RegExp(`(?:^|[;\\s])${method}=([a-z]+)`, "i"))?.[1]?.toLowerCase();
	return { trusted: true, spf: first("spf"), dkim: first("dkim"), dmarc: first("dmarc") };
}

export function domainOf(address: string): string {
	const at = address.lastIndexOf("@");
	return at < 0 ? "" : address.slice(at + 1).trim().toLowerCase().replace(/>$/, "");
}

/** Local part, lower-cased, without any +subaddress. */
export function localPartOf(address: string): string {
	const at = address.lastIndexOf("@");
	return (at < 0 ? address : address.slice(0, at)).trim().toLowerCase().split("+")[0];
}

export function senderAllowed(domain: string, allowed: string[]): boolean {
	if (!domain) return false;
	return allowed.some((entry) => {
		const want = entry.trim().toLowerCase();
		return want !== "" && (domain === want || domain.endsWith(`.${want}`));
	});
}

export function findList(lists: ListConfig[], recipient: string): ListConfig | undefined {
	const local = localPartOf(recipient);
	return lists.find((l) => l.address.toLowerCase() === local);
}

export function decide(input: {
	list: ListConfig | undefined;
	headerFromDomain: string;
	auth: AuthResults;
	management: boolean;
	onboarding: boolean;
}): Decision {
	const { list, headerFromDomain, auth, management, onboarding } = input;
	if (!list) return { action: "reject", reason: "unknown recipient" };

	const authenticated = auth.trusted && auth.dmarc === "pass";
	const allowed = senderAllowed(headerFromDomain, list.senders);

	if (allowed && authenticated) {
		// Confirmations and the like need a person; they are never published.
		return management ? { action: "forward", reason: "subscription management message" } : { action: "publish" };
	}
	// Anything else is never published.
	if (allowed) {
		// The list's own sender, but unauthenticated. Refusing it would be a
		// permanent bounce, and list software drops subscribers that bounce,
		// so it goes to the maintainer instead.
		return { action: "forward", reason: `sender allowed but DMARC is ${auth.dmarc ?? "absent"}` };
	}
	// An unrecognised sender. While a list is being set up it is forwarded
	// privately, so the real sender domain can be confirmed; afterwards it
	// is refused.
	const why = `sender ${headerFromDomain || "(none)"} not on the list's allowlist`;
	return onboarding ? { action: "forward", reason: why } : { action: "reject", reason: why };
}
