# B2B Email Finder

Find public business contact data (email and phone) for small shops in Germany: kiosks,
Spätis, gas stations, vape and tobacco shops, candy shops, shisha bars and food shops.

It is free, runs on your own machine, and uses only open data and the shops' own websites.

It started as a hackathon challenge: build an open alternative to paid "Google Maps email
scraper" services, which are closed source, charge per lead and scrape Google Maps against
Google's terms.

## What it does

1. **Finds shops** in an area (for example "Köln") from two open map databases.
2. **Visits each shop's website** politely and reads the email from the legal notice
   (Impressum), contact page or privacy policy.
3. **Looks for a website** when a shop lists none, by trying likely domain names.
4. **Checks every email**: can the domain receive mail, does it belong to this shop, how
   confident are we?
5. **Shows the results** in a web app with filters, a map, exports and a review screen.

## Example: Köln

One run over the city of Köln, with free sources only:

| | Result |
|---|---|
| Shops found | 2,401 |
| With an email | 487 (20%) |
| With a phone number | 1,241 (52%) |
| With an email or a phone number | 1,287 (54%) |

Coverage differs a lot by type of shop:

| Segment | Shops | With email |
|---|---|---|
| Süßwaren (candy) | 68 | 46% |
| Lebensmittel (food) | 783 | 36% |
| Vape & Shisha-Shop | 37 | 32% |
| Tabak / Lotto / Presse | 132 | 30% |
| Shisha-Bar | 44 | 27% |
| Späti / Convenience | 194 | 18% |
| Tankstelle (gas station) | 217 | 16% |
| Kiosk | 926 | 5% |

Most kiosks have no website at all, so no public email exists for them. That is the main
limit of this approach.

## Run with Docker (easiest)

You only need Docker (Docker Desktop or Rancher Desktop).

```bash
git clone https://github.com/akadiryigit/b2b_email_finder.git
cd b2b_email_finder
docker compose up
```

Then open **http://localhost:8501**. The first build takes a few minutes.

- Result files in the `demo/` folder are shown right away, so you can explore the app
  without running a search first.
- Start a new search from the sidebar in the app, or from the command line:
  ```bash
  docker compose run --rm app b2b-email-finder search --area "Bonn" --out results/bonn
  ```
- Results and saved work are kept in a Docker volume and survive restarts. Remove them with
  `docker compose down -v`.
- Optional keys are passed through from your shell, for example
  `BRAVE_API_KEY=... docker compose up`.

## Run without Docker

You need Python 3.10 or newer. On Windows, use WSL.

```bash
git clone https://github.com/akadiryigit/b2b_email_finder.git
cd b2b_email_finder
python -m venv .venv && source .venv/bin/activate
pip install -e ".[ui,sources,dev]"

# search a city (takes a while the first time; later runs reuse saved work)
b2b-email-finder search --area "Köln" --out results/koeln

# open the web app at http://localhost:8501
b2b-email-finder ui
```

For a quick first test, limit the search:

```bash
b2b-email-finder search --area "Bonn" --category kiosk vape --limit 50 --out results/test
```

Results are saved as `results/<name>.json` (complete) and `results/<name>.csv` (opens in
Excel). Run the tests with `pytest`.

## The web app

- **Filters:** search box, segment, city, district, ownership, email confidence, only with
  email, only with phone, international markets.
- **Table:** one row per shop, with a coloured segment label, email, confidence, phone,
  website, and the page where the email was found.
- **Exports:** filtered table (CSV), email list (one row per email, ready for a newsletter
  tool), Excel workbook (one sheet per segment), selected rows.
- **Email coverage:** per segment and per district.
- **Map:** one dot per shop, coloured by segment or by "email found / not found".
- **Review:** check a random sample of emails by hand to measure how many are correct.
  Corrections are kept across runs.
- **Compare runs:** what changed since an earlier search.
- **Never-contact list:** emails or domains that must never appear in results again.
- **New search:** runs in the background, with live progress and a cancel button.

## Where the data comes from

| Source | What it gives | Setup |
|---|---|---|
| [OpenStreetMap](https://www.openstreetmap.org) | Shops, addresses, some websites, emails and phones | none |
| [Overture Maps](https://overturemaps.org) | More shops, websites, emails and phones | none |
| The shops' own websites | Emails from the Impressum, contact and privacy pages | none |
| Domain guessing | Websites for shops that list none (`kiosk-ali-baba.de`) | none |
| Web search (Brave Search API) | Websites that no database lists | optional: `export BRAVE_API_KEY=...` |
| [Foursquare Open Source Places](https://opensource.foursquare.com/os-places/) | A third shop database | optional: free Hugging Face login, then set `FSQ_PLACES_PARQUET` |
| [Common Crawl](https://commoncrawl.org) | Archived copies of sites that can't be opened | optional: `--commoncrawl` (slow) |

A shop that appears in several databases is merged into one row.

## Segments

| Segment | OpenStreetMap tags |
|---|---|
| Kiosk | `shop=kiosk` |
| Tankstelle | `amenity=fuel` |
| Späti / Convenience | `shop=convenience` |
| Vape & Shisha-Shop | `shop=e-cigarette`, `shop=hookah` |
| Tabak / Lotto / Presse | `shop=tobacco`, `shop=lottery`, `shop=newsagent` |
| Süßwaren | `shop=confectionery`, `shop=chocolate` |
| Shisha-Bar | `amenity=hookah_lounge` |
| Lebensmittel | `shop=supermarket`, `shop=beverages`, `shop=greengrocer`, `shop=deli` |

To add a segment, add one entry in
[`categories.py`](src/b2b_email_finder/categories.py). Discounters and drugstore chains
(Aldi, Lidl, dm, Rossmann, ...) are left out.

Each shop also gets an **ownership** label (independent, chain, or independently run
franchise such as "EDEKA Müller") and a flag for **international markets**, guessed from the
name.

## How wrong emails are kept out

- **Head-office emails never count.** An address on a chain's domain (`@aral.de`) is not
  the station's own contact.
- **Shared emails are dropped.** The same address on four or more shops belongs to a
  website builder or an agency.
- **Third parties are ignored:** authorities, hosting companies and payment providers named
  in privacy policies.
- **Redirects are not trusted.** A page that ends up on a different website is discarded.
- **A guessed website must prove itself:** its pages have to show the shop's name plus its
  street, postcode or city.
- **Every email has a confidence level** (high, medium or low) and the page it came from.

## What it deliberately does not do

- It does not scrape Google Maps, Facebook, Instagram or business directories. Their terms
  forbid it.
- It does not decode hidden addresses such as `info [at] kiosk.de`. The owner wrote it that
  way to keep automated collectors out.
- It does not probe mail servers to test single mailboxes, rotate proxies or solve CAPTCHAs.
- It respects `robots.txt`, waits between requests and identifies itself honestly.

## Legal notes (Germany / EU)

This is not legal advice.

- **Finding an address does not allow you to advertise to it.** In Germany a first
  advertising email or WhatsApp message to a business needs prior consent
  (§ 7 UWG). A phone call is allowed when the business can be presumed to be interested.
- **GDPR applies.** Many small shops are sole traders, so their contact data is personal
  data. Keep the source of every address (the tool records it) and honour objections (the
  never-contact list).
- **Credit the data.** OpenStreetMap data is under the ODbL licence: credit
  "© OpenStreetMap contributors". Overture data is under CDLA-Permissive 2.0.
- **Don't publish result files.** They contain contact data of real businesses. The
  `results/` folder is excluded from git. Only put result files into `demo/` if the
  repository is private.

## Next steps

Planned improvements, all free. The gains are estimates from the Köln run.

| # | Improvement | Why | Expected gain |
|---|---|---|---|
| 1 | **Run on a private network or small server** | Company firewalls block some shop websites | +50 to 100 emails |
| 2 | **Web search for missing websites** | 61% of shops list no website; search finds sites that no database knows. Built, needs a free API key. | +50 to 130 emails |
| 3 | **Follow shops that moved domains** | Redirects to a new domain are discarded today; accept them when the new site names the shop | +10 to 25 emails |
| 4 | **Read more kinds of pages** | A headless browser for JavaScript sites, more names for the legal page, PDF legal notices | +30 to 60 emails |
| 5 | **Add Foursquare as a third database** | More shops, websites and phones. Built, needs a free login. | +20 to 50 emails |
| 6 | **Cleaner results** | Remove wholesalers from the food segment, merge leftover duplicates, publish a measured precision figure | Better quality |
| 7 | **Phone outreach support** | Call lists per district and a contact status per shop, since half the shops have a phone but no email | Reaches shops without email |
| 8 | **More regions** | Use a full OpenStreetMap download instead of the public query servers, to scale beyond one city | Germany-wide runs |

Together, steps 1 to 5 should bring Köln from 20% to roughly 28–35% email coverage.

## Project layout

```
src/b2b_email_finder/
  cli.py           command line
  pipeline.py      runs all steps in order
  geocode.py       area name -> map area
  overpass.py      shops from OpenStreetMap
  sources.py       shops from Overture and Foursquare
  conflate.py      merges the same shop from several databases
  admin_areas.py   city and district of every shop
  crawler.py       polite website visits
  extract.py       emails and contact links from a page
  discovery.py     finds websites for shops without one
  verify.py        email checks, ranking and confidence
  segments.py      chains, ownership, international markets
  export.py        JSON and CSV output
  app.py           web app (Streamlit)
tests/             offline tests, no network needed
```

## License

MIT for the code, see [LICENSE](LICENSE).
