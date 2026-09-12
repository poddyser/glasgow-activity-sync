import json
import hashlib
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

def make_id(d):
    key = f"{d.get('name','')}|{d.get('location',{}).get('geo',{}).get('latitude','')}|{d.get('location',{}).get('geo',{}).get('longitude','')}|{(d.get('organizer') or {}).get('name','')}"
    return hashlib.md5(key.encode()).hexdigest()[:16]

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
        "id": make_id(d),
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
        res = httpx.get(
            url,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-GB,en;q=0.9",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Referer": "https://glasgowlife.sportsuite.co.uk/",
            },
            timeout=30,
            follow_redirects=True,
        )
        res.raise_for_status()
        data = res.json()
        items = data.get("items", [])
        page += 1

        for item in items:
            if item.get("state") == "deleted":
                all_to_delete.append(make_id(item.get("data", {})) if item.get("data") else str(item.get("id")))
            elif item.get("state") == "updated" and item.get("data") and is_glasgow(item["data"]):
                parsed = parse_activity(item)
                if parsed["lat"] and parsed["lng"]:
                    all_to_upsert.append(parsed)

        next_url = data.get("next")
        url = next_url if (next_url and next_url != last_url and items) else None

    print(f"Pages fetched: {page}")
    print(f"Activities to upsert: {len(all_to_upsert)}")
    print(f"Activities to delete: {len(all_to_delete)}")

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
