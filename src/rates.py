"""Auto-source current tariff rates from Synergy's public plan pages.

Synergy publishes each residential plan's rates as plain text of the form
"<label> electricity charge <N> cents per unit" and "Supply charge <N> cents
per day" on pages under https://www.synergy.net.au/Your-home/Energy-plans/.
These are present in the raw HTML (no JS needed), so a simple httpx fetch +
regex parse recovers them. Results map onto the API's tariff-period column
names; anything we can't map is left for the user to fill in the UI.
"""

import re

import httpx

PLANS_BASE = "https://www.synergy.net.au/Your-home/Energy-plans/"

# Map the tariff label Synergy returns in getLinkedServices ("Tarrif") to the
# slug of its public plan page.
_TARIFF_SLUGS = {
    "EV2 TARIFF": "Electric-Vehicle-Add-On",
    "ELECTRIC VEHICLE ADD ON": "Electric-Vehicle-Add-On",
    "EV ADD ON": "Electric-Vehicle-Add-On",
    "MIDDAY SAVER": "Midday-Saver",
    "HOME PLAN A1": "Home-Plan-A1",
    "HOME PLAN (A1)": "Home-Plan-A1",
    "A1": "Home-Plan-A1",
    "HOME BUSINESS PLAN K1": "Home-Business-Plan-K1-Resi",
    "K1": "Home-Business-Plan-K1-Resi",
}


def plan_slug_for_tariff(tariff_label: str | None) -> str | None:
    return _TARIFF_SLUGS.get((tariff_label or "").strip().upper())


def map_label_to_periods(label: str) -> list[str]:
    """Map a published rate label to the API tariff-period column(s) it covers.

    Keyword-based so it tolerates leading noise left over from HTML tag
    stripping (e.g. "unit Overnight"). Order matters: check the more specific
    labels first ("super off peak" before "off peak" before "peak").
    """
    l = label.lower()
    if "super off peak" in l or "super off-peak" in l:
        return ["SUPER_OFFPEAK"]
    if "overnight" in l:
        return ["OVERNIGHT"]
    if "off peak" in l or "off-peak" in l:
        # EV off-peak applies to both the AM and PM off-peak columns.
        return ["OFF_PEAK", "OFF_PEAK(AM)"]
    if "peak" in l or "standard home" in l or "anytime" in l or "flat" in l:
        return ["PEAK"]
    return []


ALL_PERIODS = ["PEAK", "OFF_PEAK", "OFF_PEAK(AM)", "OVERNIGHT", "SUPER_OFFPEAK"]


def _strip_tags(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def parse_rates_html(html: str) -> dict:
    """Parse rate labels/values out of a plan page's HTML.

    Time-of-use plans list several labelled "<label> electricity charge <N>
    cents per unit" rows. Flat plans (e.g. Home Plan A1) list a single bare
    "Electricity charge <N> cents per unit"; that one rate applies to every
    period, so we spread it across all columns.
    """
    text = _strip_tags(html)
    rates: dict[str, float] = {}
    for m in re.finditer(
        r"([A-Za-z ()\-]{3,60}?) electricity charge ([\d.]+) cents per unit", text, re.I
    ):
        value = float(m.group(2))
        for col in map_label_to_periods(m.group(1)):
            rates[col] = value

    # Flat-rate fallback: no labelled TOU periods but a plain electricity charge.
    if not rates:
        flat = re.search(r"electricity charge ([\d.]+) cents per unit", text, re.I)
        if flat:
            rates = {p: float(flat.group(1)) for p in ALL_PERIODS}

    supply = None
    sm = re.search(r"Supply charge ([\d.]+) cents per day", text, re.I)
    if sm:
        supply = float(sm.group(1))
    return {"supply_charge_cents": supply, "rates": rates}


async def fetch_plan_rates(tariff_label: str | None) -> dict | None:
    """Fetch and parse current rates for a tariff label, or None on any failure.

    Callers treat None as "no data" and fall back to prefill defaults; this
    function never raises for network/parse issues.
    """
    slug = plan_slug_for_tariff(tariff_label)
    if not slug:
        return None
    try:
        async with httpx.AsyncClient(
            timeout=30.0, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            resp = await client.get(PLANS_BASE + slug)
        if resp.status_code != 200:
            return None
    except httpx.HTTPError:
        return None
    parsed = parse_rates_html(resp.text)
    return parsed if parsed["rates"] else None
