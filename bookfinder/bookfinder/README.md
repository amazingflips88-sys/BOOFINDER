# Private Used-Book Finder — V4

V4 is a compact Amazon used-book sourcing dashboard built around generic Keepa USED price history rather than an assumed FBA price premium.

## V4 additions
- ASIN and book-type quick glance.
- Editable **shipped buy cost** (item + shipping) for real sourcing decisions.
- Conservative intrinsic Used value and 365-day Used median.
- Mini Keepa-style graph: black Used-price line + green sales-rank line.
- 90-day rank-drop demand proxy.
- Current Used offer count and demand-adjusted Supply Meter: Low / Balanced / High.
- BUY / BUY & HOLD / ANALYZE / PASS screening.
- Max-buy calculation constrained by minimum profit and ROI.
- Keepa and Amazon links.
- Reviewed / Watch / Buy / Pass buttons.
- Previously marked ASINs are removed from New-only detailed analysis.
- Optional Supabase persistence so ASIN history survives Streamlit restarts.

## Streamlit Secrets
Required:
```toml
KEEPA_API_KEY = "your-keepa-key"
```

Optional persistent history:
```toml
SUPABASE_URL = "https://YOUR_PROJECT.supabase.co"
SUPABASE_KEY = "YOUR_SUPABASE_ANON_KEY"
```

Create this table in Supabase if you enable persistence:
```sql
create table if not exists public.book_status (
  asin text primary key,
  status text not null,
  title text,
  reason text,
  updated_at timestamptz default now()
);

alter table public.book_status enable row level security;
create policy "anon read book_status" on public.book_status for select to anon using (true);
create policy "anon insert book_status" on public.book_status for insert to anon with check (true);
create policy "anon update book_status" on public.book_status for update to anon using (true) with check (true);
```

For a private personal app this is convenient, but do not expose the Supabase anon key outside Streamlit Secrets. For stronger multi-user security, add authentication and user-scoped policies.

## Important
Keepa's generic marketplace price is a market reference and may not include shipping. V4 therefore lets you edit the **Buy Cost SHIPPED** before it calculates the sourcing decision.

The fee input is still an estimate/placeholder. Verify Amazon fees, restrictions, exact edition, condition, seller and checkout price before buying.
