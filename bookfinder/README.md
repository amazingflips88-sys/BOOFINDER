# Private FBA Book Finder V1

A Streamlit proof of concept that discovers book ASINs through Keepa Product Finder, retrieves a limited batch of price data, and scores *manually verified* acquisition prices.

## Deployment
1. Create a **private** GitHub repository and upload `app.py`, `requirements.txt`, `.gitignore`.
2. At https://share.streamlit.io create a new app from the repository, entrypoint `app.py`.
3. In Advanced settings → Secrets, enter `KEEPA_API_KEY = "YOUR_OWN_KEY"` (never commit this file to GitHub).
4. Set app visibility to private under Streamlit app sharing settings.
5. Launch the app, discover 50 candidate ASINs, then analyze only a few to conserve Keepa tokens.

## Important limitations
- Keepa Product Finder costs **10 + 1 per 100 returned ASINs**; fetching details costs approximately **1 per ASIN** without offers. A 1-token/minute allowance can cause delays.
- Discovery searches the Keepa catalog, not actual Amazon purchase listings.
- Used price and 90-day averages are **reference prices**, not validated achievable FBA sale prices.
- Estimated fees are manually entered placeholders, not live Amazon FBA fees.
- The app does **not** automatically source live Amazon/eBay offers, calculate authoritative FBA fees, run in the background, or guarantee profit.
- Streamlit Community Cloud is not a persistent database; this MVP caches results temporarily. Add hosted database + scheduled worker for ongoing scanning.
- Book category ID and CSV price indices should be validated against your live Keepa results before making purchases.
