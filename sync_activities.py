import json
import httpx

FEED_START = "https://www.sportsuite.co.uk/api/cs-api/openactive?afterTimestamp=0&afterId=0"
SUPABASE_URL = None
SUPABASE_KEY = None
BOUNDS = {"lat_min": 55.78, "lat_max": 55.95, "lng_min": -4.50, "lng_max": -4.05}

import os
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

def is_glasgow(d):
    loc = d.get("location", {})
    geo = loc.get("geo", {})
    lat = geo.get("latitude")
    lng = geo.get("longitude")
    in_bounds = lat and lng and (
        BOUNDS["lat_min"] <= lat <= BOUNDS["lat_max"] and
        BOUNDS["lng_min"] <= lng <= BOUNDS["lng_max"]
    )
    addr = loc.get("address", "")
    postcode = addr.get("postalCode", "") if isinstance(addr, dict) else str(addr)
    g_post = postcode.strip().upper().startswith("G")
    url = d.get("url", "")
    org_url = (d.get("organizer") or {}).get("url", "")
    gl_url = "glasgowlife" in url or "glasgowlife" in org_url
    return bool(in_bounds or g_post or gl_url)

def make_id(item):
    # Use the feed's own unique ID — no hashing needed
    return str(item.get('id', ''))

def has_future_date(parsed):
    from datetime import date
    next_date = parsed.get("next_date", "")
    if not next_date:
        return False
    try:
        return date.fromisoformat(next_date) >= date.today()
    except ValueError:
        return False

def parse_activity(item):
    d = item["data"]
    loc = d.get("location", {})
    geo = loc.get("geo", {})
    acts = d.get("activity", [])
    offers = d.get("offers", [])
    age = d.get("ageRange", {})
    sub_events = d.get("subEvent", [])

    price = (
        "See website" if not offers
        else "Free" if offers[0].get("price") == 0
        else f"£{offers[0].get('price','?')}"
    )
    min_age = age.get("minValue", "")
    max_age = age.get("maxValue", "")
    age_str = (
        f"{min_age}–{max_age}" if (min_age and max_age)
        else f"{min_age}+" if min_age
        else "All ages"
    )
    gender_raw = d.get("genderRestriction", "")
    gender = (
        "Male only" if "MaleOnly" in gender_raw
        else "Female only" if "FemaleOnly" in gender_raw
        else "No restriction"
    )
    dates = sorted([s.get("startDate", "") for s in sub_events if s.get("startDate")])
    next_date = dates[0][:10] if dates else ""
    addr = loc.get("address", "")
    if isinstance(addr, dict):
        address = ", ".join(filter(None, [addr.get("streetAddress"), addr.get("addressLocality"), addr.get("postalCode")]))
    else:
        address = str(addr)

    return {
        "id": str(item["id"]),
        "name": d.get("name", ""),
        "activity_type": acts[0].get("prefLabel", "Activity") if acts else "Activity",
        "organiser": (d.get("organizer") or {}).get("name", ""),
        "venue": loc.get("name", ""),
        "address": address,
        "lat": geo.get("latitude"),
        "lng": geo.get("longitude"),
        "price": price,
        "age": age_str,
        "gender": gender,
        "next_date": next_date,
        "url": (d.get("url", "") or "").split("?")[0],
        "description": (d.get("description", "") or "")[:500],
        "synced_at": "now()",
    }

def main():
    print("Starting OpenActive feed sync...")
    url = FEED_START
    last_url = None
    all_to_upsert = []
    all_to_delete = []
    page = 0

    while url and url != last_url:
        last_url = url
        print(f"Fetching page {page + 1}: {url}")
        # Rotate through different User-Agents to avoid bot detection
        import random
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
        ]
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-GB,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "User-Agent": random.choice(user_agents),
            "Referer": "https://glasgowlife.sportsuite.co.uk/activity-finder/activities",
            "Origin": "https://glasgowlife.sportsuite.co.uk",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "sec-ch-ua": '"Google Chrome";v="125", "Chromium";v="125", "Not=A?Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }

        # Retry up to 3 times with backoff on 403
        max_retries = 3
        res = None
        for attempt in range(max_retries):
            try:
                res = httpx.get(
                    url,
                    headers={**headers, "User-Agent": random.choice(user_agents)},
                    timeout=30,
                    follow_redirects=True,
                )
                if res.status_code == 403:
                    wait = (attempt + 1) * 10
                    print(f"  403 on attempt {attempt+1}, waiting {wait}s...")
                    import time
                    time.sleep(wait)
                    continue
                res.raise_for_status()
                break
            except httpx.HTTPStatusError as e:
                if attempt == max_retries - 1:
                    raise
                import time
                time.sleep((attempt + 1) * 10)
        if res is None:
            raise Exception("All retries failed")
        data = res.json()
        items = data.get("items", [])
        page += 1

        for item in items:
            if item.get("state") == "deleted":
                all_to_delete.append(make_id(item))
            elif item.get("state") == "updated" and item.get("data") and is_glasgow(item["data"]):
                parsed = parse_activity(item)
                if parsed["lat"] and parsed["lng"] and has_future_date(parsed):
                    all_to_upsert.append(parsed)

        next_url = data.get("next")
        url = next_url if (next_url and next_url != last_url and items) else None

    print(f"Pages fetched: {page}")
    print(f"Activities to upsert: {len(all_to_upsert)}")
    print(f"Activities to delete: {len(all_to_delete)}")

    # ── Apply location overrides from Supabase ────────────────
    try:
        override_res = httpx.get(
            f"{SUPABASE_URL}/rest/v1/activity_overrides?select=*",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
            },
            timeout=15,
        )
        overrides = override_res.json() if override_res.status_code == 200 else []
        print(f"Loaded {len(overrides)} location overrides")
    except Exception as e:
        overrides = []
        print(f"Warning: could not load overrides: {e}")

    def apply_overrides(activity, overrides):
        for ov in overrides:
            field = ov.get("match_field", "venue")
            value = ov.get("match_value", "")
            actual = activity.get(field, "") or ""
            if actual.lower() == value.lower():
                if ov.get("venue"):  activity["venue"]   = ov["venue"]
                if ov.get("address"): activity["address"] = ov["address"]
                if ov.get("lat"):    activity["lat"]     = ov["lat"]
                if ov.get("lng"):    activity["lng"]     = ov["lng"]
        return activity

    all_to_upsert = [apply_overrides(a, overrides) for a in all_to_upsert]
    print(f"Overrides applied")

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }

    # Upsert in batches of 50
    batch_size = 50
    upserted = 0
    for i in range(0, len(all_to_upsert), batch_size):
        batch = all_to_upsert[i:i+batch_size]
        res = httpx.post(
            f"{SUPABASE_URL}/rest/v1/activities?on_conflict=id",
            headers=headers,
            json=batch,
            timeout=30,
        )
        if res.status_code in (200, 201):
            upserted += len(batch)
            print(f"  Upserted batch {i//batch_size + 1}: {len(batch)} records")
        else:
            print(f"  ERROR upserting batch {i//batch_size + 1}: {res.status_code} {res.text}")

    # Delete removed activities
    deleted = 0
    if all_to_delete:
        ids_str = ",".join([f'"{i}"' for i in all_to_delete])
        res = httpx.delete(
            f"{SUPABASE_URL}/rest/v1/activities?id=in.({ids_str})",
            headers=headers,
            timeout=30,
        )
        if res.status_code in (200, 204):
            deleted = len(all_to_delete)
            print(f"Deleted {deleted} removed activities")
        else:
            print(f"ERROR deleting: {res.status_code} {res.text}")

    print(f"\n✅ Sync complete — upserted: {upserted}, deleted: {deleted}")

if __name__ == "__main__":
    main()
