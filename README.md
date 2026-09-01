# stores-chemist-warehouse

Scrapes Chemist Warehouse's Australian store network into
[`stores.json`](./stores.json), refreshed by a GitHub Actions run that commits
only when the data actually changed.

## Source

`https://api.chemistwarehouse.com.au/web/v1/channels/cwr-cw-au/en/radius`
— the endpoint the store locator on chemistwarehouse.com.au calls as you type
a suburb. Unauthenticated, no API key, no cookie required.

```bash
curl --compressed \
  -H 'accept: */*' \
  -H 'origin: https://www.chemistwarehouse.com.au' \
  -H 'referer: https://www.chemistwarehouse.com.au/' \
  -H 'user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36' \
  'https://api.chemistwarehouse.com.au/web/v1/channels/cwr-cw-au/en/radius?channel-type=store&latitude=-33.8672&longitude=151.1997&search-type=store-locator&offset=0&limit=100'
```

To find it again: open the store locator, devtools on the Network tab, search
for a suburb. It is the only XHR that matters.

### What the endpoint will and will not do

Established by probing, and the reason the scraper looks the way it does:

| | |
| --- | --- |
| Radius | Fixed at **exactly 26.0 km**. `radius`, `distance`, `maxDistance`, `searchRadius`, `range` are all accepted and all ignored. |
| `limit` | Caps at **100**; anything higher is a `400`. Results are distance-sorted, so dense areas really do truncate — Melbourne CBD has 144 stores inside 26 km. |
| `offset` | Works, so the truncation is recoverable by paging. |
| Coordinates | Rejected beyond ~6 decimal places (`400 Invalid coordinates parameter(s) supplied`). The scraper sends 4dp. |
| List-all sibling | None. `/stores`, `/all`, `/list`, `/search` and a by-channel-key lookup are all `405`, and `/radius` without coordinates is a `500`. |
| Auth | None. The `cf_clearance` / `__cf_bm` cookies a browser sends are not required. |

Note that **`www.chemistwarehouse.com.au` is behind a Cloudflare interactive
challenge** — `/robots.txt` and `/sitemap.xml` are `403` to anything without a
solved `cf_clearance` cookie, which rules out the sitemap and page-scraping
rungs of [SCRAPING.md](https://github.com/nwbort/stores-instructions/blob/main/SCRAPING.md)
for anything unattended. `api.chemistwarehouse.com.au` is *not* challenged,
which is what makes this approach durable.

## How it works

Since the API only answers "what is near this point", the scraper covers the
country in overlapping 26 km circles and dedupes by store key — rung 6 of
[SCRAPING.md](https://github.com/nwbort/stores-instructions/blob/main/SCRAPING.md).
Probe points sit on a 34 km lattice, below the 36.8 km at which circles of this
radius start leaving gaps. `scrape.py`'s docstring has the full reasoning.

There are two sweeps, on two cadences:

| | Points | Runtime | Cadence | Finds |
| --- | --- | --- | --- | --- |
| `--full` | 7,738 | ~12 min | Sundays | Everything, including a store in a town that has never had one |
| `--targeted` | ~480 | ~1 min | Daily | Everything within ~26 km of a store already in `stores.json` — which is where nearly every new store appears |

The targeted sweep seeds itself from the committed `stores.json` and probes
each known store's lattice cell plus the eight around it, so anything the
weekly sweep discovers is covered by every daily run afterwards. With no
`stores.json` to seed from, it falls back to a full sweep.

## Running locally

```bash
./scrape.sh              # full national sweep
./scrape.sh --targeted   # fast sweep around known stores
./scrape.sh --check      # offline checks (geometry + retry), no requests
```

Standard library Python only — no `requirements.txt`, no install step. Both
check suites run automatically before every scrape; together they are a few
seconds and neither touches the network.

## Rate limiting, and where this can run

From an ordinary connection the endpoint is happy to be swept: 7,738 requests
at 8 workers without a single 429, and a deliberate 600-request burst at 40
workers could not provoke one.

**From GitHub-hosted runners it does not work at all.** Not "slowly" — the
bot filter in front of the API answers *every* request with a 429, including
at one request every two seconds, and never lets one through. The runner that
was tested sat in Azure `westus2`; a US datacentre address hitting an
Australian retail API is exactly the profile Cloudflare rejects. That is a
refusal, not a throttle, and no amount of client-side politeness changes it.

So the scheduled workflow will fail on a GitHub-hosted runner until it is
pointed at a runner with a residential or Australian egress address. `--full`
and `--targeted` both work fine locally, which is how `stores.json` is
currently produced.

The scraper is built for the throttled case anyway, since that is what a
better-placed runner will meet:

- **One `Throttle` shared by the pool.** A 429 pauses *every* worker. Eight
  workers backing off independently is still eight times the pressure on the
  thing that just asked us to slow down.
- **Additive-increase / multiplicative-decrease.** A 429 widens the interval,
  and every success narrows it again. A throttle that only slows down is a
  one-way ratchet — the first 429 of a sweep pins every remaining point at the
  worst pace it ever saw, which turned a 55-second job into a 30-minute one.
- **`Retry-After` is a floor, never a replacement.** Cloudflare answers a
  blocked address with one that parses as zero; obeying it literally means no
  backoff at all.
- **A gentle start.** Opening at full tilt from a datacentre IP is part of what
  trips the filter.
- **Fail fast when refused.** 40 rate-limit responses with nothing at all
  getting through aborts with a message saying so, rather than grinding until
  the job times out.

`tests/check_retry.py` covers all of it against a fake API. None of it can be
exercised against the real endpoint from a normal connection.

## Output

`stores.json` is a JSON array, sorted by numeric store id so a renamed or
relocated store stays on the same line of the diff.

```json
{
  "key": "cwr-cw-au-store-624",
  "store_id": 624,
  "name": "Chemist Warehouse Sydney - Wynyard",
  "type": "store",
  "capabilities": ["click-and-collect", "cw-au", "fast-delivery"],
  "address": {
    "building": null,
    "street_number": "Lower Ground Floor",
    "street_name": "309 George Street",
    "city": "Sydney",
    "region": null,
    "state": "New South Wales",
    "postal_code": "2000",
    "country": "AU"
  },
  "phone": "+61292615339",
  "fax": "+61292615870",
  "email": "wynyard@chemistwarehouse.com.au",
  "latitude": -33.86621,
  "longitude": 151.206929,
  "opening_hours": {
    "monday": { "from": "07:30", "to": "21:00" }
  }
}
```

Upstream is close to this already, but three things are reshaped rather than
passed through, because left alone they repaint the whole file on runs where
nothing changed: every store arrives wrapped in a redundant `{"channel": {…}}`,
`openingHours` comes back with per-day key order that varies between days
(`monday` as `{from, to}`, `tuesday` as `{to, from}`), and `capabilities` is
unordered. So: fixed day order, fixed key order, sorted capabilities.

## Deviations from the conventions

See [stores-instructions](https://github.com/nwbort/stores-instructions).
Where this repo differs, and why:

- **No `download.sh`.** It fetches one URL to one file; this scraper makes
  thousands of requests and reduces them to one document, so there is nothing
  for it to do.
- **One `scrape.py` rather than `download.sh` + `parse.sh`.** The grid search
  is fetch and transform interleaved — a point's results are deduped against
  every other point's as they arrive. Splitting it would mean writing ~7,700
  raw payloads to disk to read them straight back.
- **Two cron entries instead of one.** See the cadence table above.
- **A test directory**, which most `stores-*` repos do not have. Two things
  here can be wrong without looking wrong. A gap in the lattice silently drops
  whatever stores were inside it, so `tests/check_coverage.py` proves the
  country is covered — which is what lets the mask be a hand-drawn polygon.
  And the retry path only executes when something has already gone wrong, so
  it went out untested and failed on the first CI run;
  `tests/check_retry.py` now exercises it against a fake.
