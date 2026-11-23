import type { ListConfig } from "./policy";
import type { PublicItem } from "./scrub";

export function xmlEscape(s: string): string {
	return s
		// eslint-disable-next-line no-control-regex
		.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]/g, " ")
		.replace(/&/g, "&amp;")
		.replace(/</g, "&lt;")
		.replace(/>/g, "&gt;")
		.replace(/"/g, "&quot;");
}

/** Atom 1.0. Content is plain text only: nothing from the email is ever emitted as markup. */
export function renderAtom(list: ListConfig, items: PublicItem[], selfUrl: string): string {
	const updated = items.reduce((max, it) => (it.published > max ? it.published : max), "1970-01-01T00:00:00.000Z");
	const entries = items
		.map(
			(it) => `  <entry>
    <id>urn:maine-feeds-mail:${xmlEscape(list.id)}:${xmlEscape(it.id)}</id>
    <title>${xmlEscape(it.title)}</title>
    <link rel="alternate" href="${xmlEscape(it.link)}"/>
    <published>${xmlEscape(it.published)}</published>
    <updated>${xmlEscape(it.published)}</updated>
    <content type="text">${xmlEscape(it.text)}</content>
  </entry>`,
		)
		.join("\n");
	return `<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <id>urn:maine-feeds-mail:${xmlEscape(list.id)}</id>
  <title>${xmlEscape(list.title)}</title>
  <subtitle>Received by email subscription and republished with subscriber details removed.</subtitle>
  <link rel="self" href="${xmlEscape(selfUrl)}"/>
  <link rel="alternate" href="${xmlEscape(list.link)}"/>
  <updated>${xmlEscape(updated)}</updated>
${entries}
</feed>
`;
}
