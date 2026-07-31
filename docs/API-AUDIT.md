# Insightly v3.1 API audit — endpoints, methods, content types, throttling

Audited 2026-07-31 against three sources, in this order of authority:

1. **Live probes** against a demo pod (`na1`) — what the API actually does.
2. **Swagger** `https://api.na1.insightly.com/v3.1/swagger/docs/v3.1` — 303 paths, `basePath /v3.1`.
3. **Narrative docs** `https://api.na1.insightly.com/v3.1/#!/Overview/Introduction`.

Where they disagree, the live behaviour wins and the disagreement is recorded below.

---

## 1. Verified doc claims

| Claim | Verdict |
|---|---|
| Paginated, **100 default / 500 max** per response | ✅ Confirmed. Our `top` default is 100, `PAGE_MAX = 500`. |
| `top` + `skip` page any list **and** any `/Search` | ✅ Confirmed — swagger lists both params on both. |
| `count_total=True` returns the total in **`X-Total-Count`** | ✅ Confirmed live (`x-total-count: 81`). |
| Search works on standard **and custom** fields (`field_name`/`field_value`) | ✅ Confirmed; custom fields use the `..._c` `FIELD_NAME`. |
| **10 requests/second** per instance | ✅ Our pacer runs ~8.3/s, deliberately under it. |
| Daily quota, then `429` | ✅ Confirmed headers: `x-ratelimit-limit: 100000`, `x-ratelimit-remaining: 99986`. |
| `Accept-Encoding: gzip` supported | ✅ Confirmed (`content-encoding: gzip`); httpx negotiates it automatically. |
| Every payload carries an **`ETag`** | ✅ Confirmed on list and single-record reads. |
| `If-Match` on `PUT` gives optimistic concurrency | ✅ Works — but see the discrepancy below. |
| **Quote endpoints are named `Quotation`**; "Quote" is rejected | ✅ Confirmed. Aliased so callers can say either. |
| Linking needs valid ids and the `Links` endpoints | ✅ Confirmed; 8 objects have `/{id}/Links`. |

## 2. Discrepancies worth knowing

- **A stale `If-Match` returns `400`, not the documented `412 Precondition Failed`.** Verified
  twice: bogus ETag → `400 "If-Match header value … is not valid."`; correct ETag → `200`. Our
  `update_record` therefore treats a 4xx on an If-Match write as a concurrency conflict and says so.
- **Swagger documents neither `ETag`/`If-Match` nor the `X-RateLimit-*` headers** (0 occurrences).
  Both are real. Trust the narrative docs plus probes here, not the spec file.
- **The API is inconsistent about plurals.** Most collections are plural, but **`Ticket`,
  `Product`, `Quotation`, `Pricebook`, `Prospect`, `Instance`, `KnowledgeArticle`,
  `OpportunityLineItem`, `QuotationLineItem`, `PricebookEntry`** are singular. The plural form
  returns **405**, which reads like "broken endpoint" rather than "wrong name" — this is exactly
  what made `env_summary` report Tickets/Products as unavailable before v2.1.1. `_obj()` now
  aliases every name, either direction, any case.
- **`Notes` has no top-level `POST`** (GET/PUT only). Creating a note *must* go through the
  child endpoint `/{Parent}/{id}/Notes` — which is what `add_note` does.
- **`Organisations` is British-spelled**; `Organizations` is aliased for sanity.
- **Link object names are SINGULAR** in the body (`"Organisation"`, `"Contact"`), even though the
  endpoints are plural. A frequent 400 cause.

## 3. Throttling strategy (what we actually do)

Two different limits, and they need opposite responses:

| Limit | Signal | Our strategy |
|---|---|---|
| **Per-second burst** (10/s) | `429` while `X-RateLimit-Remaining > 0` | Prevent it: a shared async pacer spaces every request ~120 ms (~8.3/s) across all tools and background jobs. If a 429 still lands, retry with capped exponential backoff honouring `Retry-After`. |
| **Daily quota** (e.g. 100 000/day) | `429` with `X-RateLimit-Remaining: 0` | **Do not retry** — the docs are explicit that nothing succeeds until the next day. Fail immediately with the limit, the reset expectation, and the advice that retrying won't help. |

Every response's `X-RateLimit-*` headers are recorded, so `connection_info` and `env_dashboard`
report the day's remaining budget (populated after the first call — it's read from responses,
never polled). This turns "why did bulk seeding stop?" into a visible number.

Other efficiency measures: one pooled keep-alive connection per (pod, key); `brief=true` by
default on lists; heavy HTML `Body` stripped client-side; `count_total` used instead of scanning
when a count is all that's wanted; field metadata served as a **cacheable resource**.

## 4. Content types

- Requests/responses are `application/json` (`content-type: application/json; charset=utf-8`).
- Auth is HTTP Basic with the API key as the username and an empty password.
- `Accept-Encoding: gzip` is honoured and httpx sends it by default — no action needed.
- **Two date formats, and mixing them up is a common 400:** URL/query parameters take ISO-8601
  (`2026-04-09T16:58:14Z`), while dates *inside object bodies* take `yyyy-MM-dd HH:mm:ss`
  (`2026-04-10 21:15:00`, 24-hour). Our `updated_after_utc` params use the former.
- Image/file-attachment endpoints exist per object but are not wrapped (see §6).

## 5. Endpoint coverage

Top-level collections in swagger: **51** (plus a generic `/{objectName}` for custom objects, with
GET/POST/PUT and its own `/Search`).

Fully wrapped by typed tools — list/search/get/create/update/delete/notes/links:
`Contacts`, `Organisations`, `Leads`, `Opportunities`, `Projects`, `Tasks`, `Events`, `Notes`,
`Emails`, `Ticket`, `Product`, `Quotation`, `Pricebook`, `Milestones`, `KnowledgeArticle`.

Discoverable and reachable through the same tools (reference/read-mostly data), added in this
audit: `Instance`, `Countries`, `Permissions`, `Prospect`, `DocumentTemplates`,
`OpportunityCategories`, `OpportunityStateReasons`, `OpportunityLineItem`, `QuotationLineItem`,
`PricebookEntry`, `ProjectCategories`, `TaskCategories`, `FileCategories`,
`KnowledgeArticleCategory`, `KnowledgeArticleFolder`, `MarketingVisits`, `Follows` — alongside the
existing `Pipelines`, `PipelineStages`, `Relationships`, `Tags`, `Teams`, `TeamMembers`, `Users`,
`Currencies`, `LeadSources`, `LeadStatuses`, `CustomObjects`, `ActivitySets`.
`list_supported_objects` now reports **44**.

Objects with `/{id}/Links`: `Contacts`, `Organisations`, `Opportunities`, `Projects`, `Tasks`,
`Events`, `Notes`, `Emails` — surfaced as `list_links` / `link_records` / `unlink_records`, which
refuse anything else with the valid list rather than a raw 404.

## 6. Deliberately not wrapped

Reachable any time via `raw_request`, just not worth a typed tool for SE demo work:

- **Files and images** (`/{obj}/{id}/FileAttachments`, `/Image`, `/ImageField/{field}`) — binary
  bodies don't belong in a chat transcript. Note the docs' caveat: file endpoints don't return
  files attached to emails.
- **Community/forum** (`CommunityForums`, `CommunityPosts`, `CommunityComments`, `ForumCategories`).
- **Marketing** (`MarketingCustomEvent`, most of `MarketingVisits`).
- Per-record extras: `Follow`, `Tags`, `Dates`, `StateHistory`, `ActivitySetAssignment`,
  `MergeDocument`, `Merge`, `LinkEmailAddress`, `Ticket` `Comments`.

## 7. Gaps closed by this audit

1. `search_records` now supports `count_total` **and** `updated_after_utc` (both documented and in
   swagger, previously unsupported) plus a `brief` toggle.
2. `X-RateLimit-*` captured; daily-quota 429 no longer burns retries; quota surfaced to the user.
3. `If-Match` optimistic concurrency on `update_record` (`if_match=`, or `safe=true` to fetch the
   current ETag first).
4. Link management tools for all 8 linkable objects.
5. 17 further collections made discoverable; `quote`/`quotes`/`knowledge` aliases added.
