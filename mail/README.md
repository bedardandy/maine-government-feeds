# Mail Worker: email bulletins as feeds

Some agencies publish by email list, and `maine.gov` currently refuses this
project's page fetches. This Cloudflare Worker receives list email, removes
everything that identifies or controls the subscriber, and serves the result
as one Atom feed per list. The feed build then reads those feeds like any
other `native_rss` source.

```
list ──email──▶ Cloudflare Email Routing ──▶ Worker ──▶ KV
                                                         │
                       feed build (every 6 h) ──GET──▶ /feeds/<list>.xml
```

The Worker holds no GitHub credential and never writes to this repository.

## What gets published

A message is published only if **both** hold:

1. its `From` domain is on that list's allowlist (`senders` in `src/lists.json`), and
2. Cloudflare's own `Authentication-Results` header says `dmarc=pass`.

Everything else is never published:

| Message | What happens |
|---|---|
| Allowlisted sender, authenticated, ordinary bulletin | Scrubbed and published |
| Allowlisted sender, authenticated, subscription confirmation | Forwarded privately to the maintainer |
| Allowlisted sender, not authenticated | Forwarded privately (never bounced: list software drops addresses that bounce) |
| Unknown sender, onboarding on | Forwarded privately, so the real sender domain can be confirmed |
| Unknown sender, onboarding off | Refused |
| Unknown recipient | Refused |

## What is removed before publishing

- the subscriber's address, anywhere it appears
- unsubscribe, "manage preferences" and other per-subscriber links
- click-tracking links (GovDelivery wrappers are replaced by the page they point to; opaque short links are dropped)
- everything from the subscriber-services footer down
- all HTML: items are plain text, and attachments are not published

The item links to the bulletin's public "view as a web page" address when the
message contains one, otherwise to the list's `link`.

The untouched message is kept in KV for one year under a key that no HTTP
route serves. It is the record of what was received.

## Endpoints

| Path | Returns |
|---|---|
| `/feeds/<list>.xml` | Atom feed, newest 50 items, with `ETag` |
| `/health` | per list: last message received and whether it has gone quiet |
| `/` | list of feeds |

## Setting up

Done already:

- KV namespace `maine-feeds-mail` exists and is bound in `wrangler.jsonc`.

Still needed, in order:

1. **A domain on Cloudflare with Email Routing enabled.** A subdomain works;
   each address is then a literal routing rule (catch-all is apex-only).
2. **A verified destination address** in Email Routing: where confirmations
   and unrecognised mail are forwarded. Store it as the Worker secret
   `FORWARD_TO` (`npx wrangler secret put FORWARD_TO`). It is a private
   address; never put it in this repository.
3. **Deploy credentials.** Add repository secrets `CLOUDFLARE_API_TOKEN` and
   `CLOUDFLARE_ACCOUNT_ID`; the `Mail Worker` workflow then deploys from
   `main`.
4. **Lists.** For each list, add an entry to `src/lists.json` and its address
   to `addresses` in `wrangler.jsonc`:

   ```json
   { "id": "dep-news", "title": "Maine DEP News (by email)", "address": "dep-news",
     "senders": [], "link": "https://www.maine.gov/dep/news/", "quietDays": 30 }
   ```

   Leave `senders` empty at first.
5. **Subscribe** each address on the agency's sign-up page. The confirmation
   arrives at `FORWARD_TO`; click it.
6. **Confirm the sender.** When the first real bulletin arrives it is forwarded
   to you and the Worker log shows its `From` domain and DMARC result. Add
   that domain to the list's `senders`. From then on bulletins publish.
7. **Add the feed to `sources.yml`** as a `native_rss` source in the matching
   category, once the feed has real items.
8. When every list is confirmed, set `ONBOARDING` to `"false"`.

The receiving addresses are visible in this public repository. That is
acceptable because an address alone publishes nothing; the allowlist and the
DMARC check are the control.

## Developing

```bash
npm ci
npm test            # unit + handler tests, no network
npm run typecheck
npx wrangler dev    # real runtime locally; then deliver a sample message:
```

```bash
curl -X POST 'http://localhost:8787/cdn-cgi/handler/email?from=x%40public.govdelivery.com&to=dep-news%40mail.example.org' \
  --data-binary @test/fixtures/govdelivery-bulletin.eml
```

The fixtures are written to resemble GovDelivery mail; they are not captures
of real bulletins. Expect to adjust the footer and link patterns in
`src/scrub.ts` once real messages have been seen, and add each real message
(with the subscriber address replaced) as a fixture when you do.

## Not built yet

- An alert when a list goes quiet. `/health` reports it, but nothing polls
  that yet.
