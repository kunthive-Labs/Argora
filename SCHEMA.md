# SQL → KunthiveOS `leads` — schema mapping & safety

The SQL generator targets the real `leads` table in
`~/Documents/Beta/KunthiveOS/supabase/migrations/0001_init.sql`
(+ `0006_archive.sql`, **+ `0010_leads_v2_rich_fields.sql`**). This doc records
the exact mapping and the guarantees, so future-you trusts what gets pushed.

> **Run migration `0010` once before pasting any Argora SQL.** It adds the
> `'argora'` source value + the rich columns (`full_address`, `latitude`,
> `longitude`, `social_links`, `raw`). It is additive & idempotent — verified
> applying cleanly on a populated table with zero data loss.

## Column mapping (CSV → leads)

| `leads` column | type | from the CSV / how |
|---|---|---|
| `name` | text not null | `name` |
| `phone` | text | `phone` |
| `area` | text | `area`, else address with PIN/“India” stripped |
| `postal_code` | text | 6-digit PIN parsed from `address` |
| `full_address` | text *(v2)* | the complete scraped `address` (no longer dropped) |
| `latitude` | numeric(10,7) *(v2)* | `@lat,lng` parsed from the Maps URL |
| `longitude` | numeric(10,7) *(v2)* | `@lat,lng` parsed from the Maps URL |
| `rating` | numeric(2,1) | `rating`, rounded to 1 decimal |
| `reviews_count` | int | `reviews` |
| `maps_link` | text | `maps_url` (or canonical `?api=1` form if a `ChIJ` id is known) |
| `website_url` | text | the actual URL (NULL when none) |
| `website_status` | enum `none\|social\|real` | `none` = no site · `social` = FB/IG/builder/directory only · `real` = genuine site |
| `social_links` | jsonb *(v2)* | `{platform: url}` when the only link is social (e.g. `{"facebook":"…"}`); `{}` otherwise |
| `category` | text | `category` |
| `source` | enum `…\|argora` *(v2)* | **`argora`** (value added in 0010) |
| `source_dataset` | text | provenance tag, e.g. `argora/gym-yelahanka` |
| `source_place_id` | text **UNIQUE** | Maps `ChIJ…`, else feature-id `0x…:0x…` hex, else `argora:<hash>` of name+phone — **never null** |
| `score` | int | normalized 0–100 (60% rating, 40% review volume) |
| `raw` | jsonb *(v2)* | the full scraped row — future-proof catch-all |

Columns left to their DB defaults (never written): `id`, `status` (`new`),
`do_not_contact` (false), `owner_id`, timestamps, `archived_at`, etc.

> `social_links` is GIN-indexed, so "show me leads whose only presence is a
> Facebook page" is `select * from leads where social_links ? 'facebook';`

> `source` is the `lead_source` enum. Migration 0010 adds the `'argora'` value,
> so scraper leads are tagged `source = 'argora'` (with the finer provenance in
> `source_dataset`). The enum value must be committed before the INSERT runs —
> i.e. apply 0010 first. Since the generated INSERT casts `'argora'::lead_source`,
> running it against a DB without 0010 applied will (correctly) error rather than
> insert under the wrong source.

## The duplicate guarantee (4 layers)

You asked: nothing added twice, even by mistake. The generated SQL enforces it
four ways — verified against a real Postgres 16 with this schema:

1. **Intra-batch dedup** (Python): a single run can't contain the same place-id
   or the same phone twice.
2. **`WHERE NOT EXISTS`** against the live table: a row is skipped if its
   `source_place_id` **or** its digits-only phone already exists. This is what
   catches the same business already imported under a *different* id format
   (old Apify `ChIJ…` vs this scraper's `0x…` hex) — matched by **phone**.
3. **`ON CONFLICT (source_place_id) DO NOTHING`**: final backstop on the UNIQUE
   constraint; a repeated id can never insert, even under a race.
4. **INSERT-only**: no `UPDATE`/`DELETE` is ever emitted — existing rows and
   their edited `status`/`notes`/`owner` are never disturbed.

**Verified:** seeded a table with an existing Apify row (TheFit24, `ChIJ…`),
then ran a batch where TheFit24 had a `0x…` hex id but the same phone →
`INSERT 0 1` (only the genuinely-new lead), and a 2nd identical run →
`INSERT 0 0`. No duplicate.

## How to run it

1. In the app (section 4) or CLI, generate the `.sql` for a LEADS file.
2. Open Supabase → **SQL editor** (runs as service role, bypasses RLS).
3. Paste & run. Re-running is always safe.

## Known limitation

Phone is the cross-format dedup key. A genuinely new lead that happens to share
a phone with an existing row (rare — shared reception line) would be skipped.
That's the deliberate trade-off: we'd rather miss a rare edge case than ever
create a duplicate.
