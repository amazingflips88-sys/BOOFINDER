# Private FBA Book Finder — V2

V2 changes the sourcing logic from “most popular books” to **A2A price-gap opportunities**.

## What V2 adds
- Product discovery sorted toward cheap Used acquisition references rather than best sales rank.
- Current Used vs Used-FBA price-gap filtering.
- **Intrinsic floor**: conservative lower-band average from daily Keepa history, with extreme lows trimmed.
- **Seasonality detection**: identifies recurring calendar-month price premiums only when at least two prior years support the pattern.
- Separate decisions: **BUY NOW**, **BUY & HOLD**, **REVIEW**, **PASS**.
- Profit/ROI at both conservative intrinsic value and recurring seasonal value.

## Important limitations
Keepa market prices are not guaranteed live, purchasable offers. Verify exact condition, edition/ISBN, seller, checkout price, Amazon selling eligibility, and actual FBA fees before buying. The fee field is still a placeholder until Amazon SP-API fee estimation is integrated.

## Streamlit
Main file: `bookfinder/app.py` when this folder is uploaded inside the repository as `bookfinder/`.

Secret required:
`KEEPA_API_KEY = "..."`
