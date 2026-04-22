from pathlib import Path
import os
from pathlib import Path
import re

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup


st.set_page_config(layout="wide", page_title="Carlisle Property Investment OS")


# ============================
# CONFIG - DATA SOURCES
# ============================
DATA_URL = "https://raw.githubusercontent.com/liampurdham/PIA-Deal-Stacker/main/pp-complete.csv"
LOCAL_FILE = "pp-complete.csv"
DEAL_TEMPLATE_FILE = "Deal Stacking Template - PIA.xlsx"
DEAL_TEMPLATE_URL = (
    "https://raw.githubusercontent.com/liampurdham/PIA-Deal-Stacker/main/"
    "Deal%20Stacking%20Template%20-%20PIA.xlsx"
)
EPC_API_BASE_URL = "https://api.get-energy-performance-data.communities.gov.uk"


# ============================
# NAVIGATION
# ============================
page = st.sidebar.selectbox(
    "Navigation",
    [
        "Analyse Deal",
        "Compare Deals",
        "Portfolio",
        "Area Intelligence",
    ],
)

st.title("Carlisle Property Investment OS")


# ============================
# SHARED HELPERS
# ============================
def format_money(value):
    return f"GBP {value:,.0f}"


def format_money_per_sqm(value):
    return f"GBP {value:,.0f} / sqm"


def format_percent(value):
    return f"{value:,.1f}%"


def percent_to_decimal(value):
    return value / 100


def safe_percent(numerator, denominator):
    return (numerator / denominator) * 100 if denominator else 0.0


def calculate_banded_tax(amount, bands):
    remaining = max(float(amount), 0.0)
    total_tax = 0.0

    for band_size, rate in bands:
        if remaining <= 0:
            break

        taxable = min(remaining, band_size)
        total_tax += taxable * rate
        remaining -= taxable

    return total_tax


def calculate_template_sdlt(price, property_type):
    residential_bands = [
        (125000, 0.05),
        (125000, 0.07),
        (675000, 0.10),
        (575000, 0.15),
    ]
    commercial_bands = [
        (150000, 0.00),
        (100000, 0.02),
        (1250000, 0.05),
    ]

    bands = residential_bands if property_type == "Residential" else commercial_bands
    return calculate_banded_tax(price, bands)


def render_breakdown_table(title, items):
    table = pd.DataFrame(
        {
            "Line Item": list(items.keys()),
            "Amount": [format_money(v) for v in items.values()],
        }
    )
    st.markdown(f"**{title}**")
    st.dataframe(table, use_container_width=True, hide_index=True)


def render_calculator_styles():
    st.markdown(
        """
        <style>
        .calc-hero {
            padding: 1.25rem 1.5rem;
            border-radius: 20px;
            background: linear-gradient(135deg, #efe7d6 0%, #dcebdc 100%);
            border: 1px solid #d3c4ab;
            margin-bottom: 1rem;
        }
        .calc-hero h3 {
            margin: 0 0 0.35rem 0;
            color: #213126;
        }
        .calc-hero p {
            margin: 0;
            color: #344339;
        }
        .calc-note {
            padding: 0.85rem 1rem;
            border-radius: 16px;
            background: #f7f4ed;
            border: 1px solid #e5dccb;
            color: #4a4a44;
            margin-bottom: 1rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def normalize_postcode(postcode):
    cleaned = re.sub(r"\s+", "", str(postcode or "")).upper()
    if len(cleaned) <= 3:
        return cleaned
    return f"{cleaned[:-3]} {cleaned[-3:]}"


def postcode_sector_key(postcode):
    normalized = normalize_postcode(postcode)
    parts = normalized.split()
    if len(parts) == 2 and parts[1]:
        return f"{parts[0]} {parts[1][0]}"
    return normalized


def postcode_outward_code(postcode):
    normalized = normalize_postcode(postcode)
    return normalized.split()[0] if normalized else ""


def normalize_address_text(value):
    text = str(value or "").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def looks_like_certificate_number(value):
    text = str(value or "").strip()
    return bool(re.fullmatch(r"\d{4}-\d{4}-\d{4}-\d{4}-\d{4}", text))


def build_epc_address(record):
    parts = [
        record.get("addressLine1") or record.get("address_line_1"),
        record.get("addressLine2") or record.get("address_line_2"),
        record.get("addressLine3") or record.get("address_line_3"),
        record.get("addressLine4") or record.get("address_line_4"),
    ]
    return ", ".join([part for part in parts if part])


def get_epc_bearer_token(override_token=""):
    if override_token:
        return override_token.strip()

    token = os.getenv("EPC_BEARER_TOKEN", "").strip()
    if token:
        return token

    try:
        token = str(st.secrets.get("EPC_BEARER_TOKEN", "")).strip()
        if token:
            return token
    except Exception:
        pass

    try:
        token = str(st.secrets.get("epc_bearer_token", "")).strip()
        if token:
            return token
    except Exception:
        pass

    return ""


def parse_registration_date(record):
    return (
        record.get("registrationDate")
        or record.get("registration_date")
        or record.get("lodgementDate")
        or record.get("lodgement_date")
        or ""
    )


def score_epc_candidate(record, street, postcode):
    address_text = normalize_address_text(build_epc_address(record))
    street_text = normalize_address_text(street)
    postcode_text = normalize_postcode(postcode)
    record_postcode = normalize_postcode(record.get("postcode"))

    score = 0
    if postcode_text and postcode_text == record_postcode:
        score += 30

    if street_text and street_text in address_text:
        score += 60
    elif street_text:
        street_tokens = set(street_text.split())
        address_tokens = set(address_text.split())
        score += len(street_tokens & address_tokens) * 8

    return score


def choose_best_epc_candidate(records, street, postcode):
    if not records:
        return None

    ranked = sorted(
        records,
        key=lambda record: (
            score_epc_candidate(record, street, postcode),
            parse_registration_date(record),
        ),
        reverse=True,
    )
    return ranked[0]


# ============================
# DATA LOADERS
# ============================
@st.cache_data
def load_data():
    df = None

    try:
        df = pd.read_csv(DATA_URL)
        st.sidebar.success("Loaded dataset from GitHub")
    except Exception:
        st.sidebar.warning("GitHub load failed - trying local file")

        base_dir = Path(__file__).resolve().parent
        file_path = base_dir / LOCAL_FILE

        if file_path.exists():
            df = pd.read_csv(file_path)
        else:
            return None

    df.columns = [
        "id",
        "price",
        "date",
        "postcode",
        "type",
        "new",
        "tenure",
        "paon",
        "saon",
        "street",
        "locality",
        "town",
        "district",
        "county",
        "category",
        "status",
    ]

    df = df[["price", "postcode", "street", "district"]]
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["postcode"] = df["postcode"].astype(str).str.lower()
    df["district"] = df["district"].astype(str).str.lower()
    df["street"] = df["street"].fillna("Unknown Street").astype(str).str.title()
    df = df[df["district"].str.contains("carlisle", na=False)]

    return df


@st.cache_data
def load_calculator_template_bytes():
    template_path = Path(__file__).resolve().parent / DEAL_TEMPLATE_FILE

    if template_path.exists():
        return template_path.read_bytes(), "repo file"

    try:
        response = requests.get(
            DEAL_TEMPLATE_URL,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
        )
        response.raise_for_status()
        return response.content, "GitHub"
    except Exception:
        return None, None


# ============================
# POSTCODE GEOLOOKUP
# ============================
@st.cache_data(show_spinner=False)
def fetch_nearby_postcodes(postcode, radius_meters=1500, limit=60):
    normalized = normalize_postcode(postcode)
    if not normalized:
        return []

    compact = re.sub(r"\s+", "", normalized)
    response = requests.get(
        f"https://api.postcodes.io/postcodes/{compact}/nearest",
        params={"radius": radius_meters, "limit": limit},
        timeout=20,
    )

    if response.status_code == 404:
        return []

    response.raise_for_status()
    payload = response.json()
    return payload.get("result", []) or []


# ============================
# EPC API
# ============================
@st.cache_data(show_spinner=False)
def search_epc_certificates(postcode, address, bearer_token):
    if not bearer_token:
        return {"status": "missing_token", "records": []}

    params = {
        "postcode": normalize_postcode(postcode),
        "current_page": 1,
        "page_size": 200,
    }
    if address:
        params["address"] = address

    response = requests.get(
        f"{EPC_API_BASE_URL}/api/domestic/search",
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "Accept": "application/json",
        },
        params=params,
        timeout=20,
    )

    if response.status_code == 404:
        return {"status": "not_found", "records": []}
    if response.status_code in (401, 403):
        return {"status": "auth_error", "records": []}

    response.raise_for_status()
    payload = response.json()
    return {
        "status": "ok",
        "records": payload.get("data", []),
        "pagination": payload.get("pagination", {}),
    }


@st.cache_data(show_spinner=False)
def fetch_epc_certificate(certificate_number, bearer_token):
    if not bearer_token or not certificate_number:
        return {"status": "missing_token", "record": {}}

    response = requests.get(
        f"{EPC_API_BASE_URL}/api/certificate",
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "Accept": "application/json",
        },
        params={"certificate_number": certificate_number},
        timeout=20,
    )

    if response.status_code == 404:
        return {"status": "not_found", "record": {}}
    if response.status_code in (401, 403):
        return {"status": "auth_error", "record": {}}

    response.raise_for_status()
    payload = response.json()
    return {"status": "ok", "record": payload.get("data", {})}


def extract_total_floor_area(record):
    for key in ("total_floor_area", "totalFloorArea", "floorArea", "floor_area"):
        value = record.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def enrich_comparables_with_epc(comps_df, bearer_token):
    if comps_df is None or comps_df.empty:
        return pd.DataFrame(), "no_comps"

    rows = []
    api_status = "ok" if bearer_token else "missing_token"

    for _, row in comps_df.iterrows():
        comp = row.to_dict()
        match_status = "no_match"
        floor_area = None
        epc_rating = None
        epc_address = None
        certificate_number = None

        if bearer_token:
            search_result = search_epc_certificates(comp["postcode"], comp["street"], bearer_token)
            status = search_result.get("status", "not_found")
            if status == "ok" and not search_result.get("records"):
                search_result = search_epc_certificates(comp["postcode"], "", bearer_token)
                status = search_result.get("status", "not_found")

            if status in ("auth_error", "missing_token"):
                api_status = status
                match_status = status
            if status == "ok":
                candidate = choose_best_epc_candidate(search_result.get("records", []), comp["street"], comp["postcode"])
                if candidate:
                    certificate_number = candidate.get("certificateNumber") or candidate.get("certificate_number")
                    epc_address = build_epc_address(candidate)
                    epc_rating = candidate.get("currentEnergyEfficiencyBand") or candidate.get("current_energy_efficiency_band")
                    certificate_result = fetch_epc_certificate(certificate_number, bearer_token)
                    if certificate_result.get("status") == "ok":
                        certificate = certificate_result.get("record", {})
                        floor_area = extract_total_floor_area(certificate)
                        epc_rating = epc_rating or certificate.get("current_energy_efficiency_band")
                        epc_address = epc_address or build_epc_address(certificate)
                        match_status = "matched" if floor_area else "missing_floor_area"
                    else:
                        match_status = certificate_result.get("status", "certificate_error")

        rows.append(
            {
                "street": comp["street"],
                "postcode": normalize_postcode(comp["postcode"]),
                "price": comp["price"],
                "floor_area_sqm": floor_area,
                "price_per_sqm": (comp["price"] / floor_area) if floor_area else None,
                "epc_rating": epc_rating,
                "epc_address": epc_address,
                "certificate_number": certificate_number,
                "epc_status": match_status if bearer_token else "missing_token",
            }
        )

    enriched_df = pd.DataFrame(rows)
    return enriched_df.sort_values(["price_per_sqm", "price"], ascending=[False, False], na_position="last"), api_status


# ============================
# SCRAPER
# ============================
def get_html(url):
    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    response.raise_for_status()
    return response.text


def extract(html):
    soup = BeautifulSoup(html, "html.parser")

    title = soup.find("h1")
    price = soup.find(string=re.compile("[\u00A3][\\d,]+"))

    bedrooms = None
    for text in soup.find_all(string=re.compile(r"\d+\s+bedroom", re.I)):
        bedrooms = text
        break

    return {
        "name": title.get_text(strip=True) if title else "Unknown",
        "price": price.strip() if price else None,
        "bedrooms": bedrooms or "Unknown",
    }


def parse_price(price_text):
    return int(re.sub(r"[^\d]", "", price_text)) if price_text else None


def estimate_sqm(bedrooms):
    try:
        return int(re.findall(r"\d+", bedrooms)[0]) * 25 + 40
    except Exception:
        return 100


def extract_location(name):
    text = name.lower()
    postcode = re.search(r"[a-z]{1,2}\d{1,2}\s?\d[a-z]{2}", text)
    street = text.split(",")[0] if "," in text else text
    return street, postcode.group(0) if postcode else None


# ============================
# ZOOPLA LINK
# ============================
def zoopla_link(street, postcode):
    query = f"{street} {postcode}".replace(" ", "-")
    return f"https://www.zoopla.co.uk/for-sale/details/search/?q={query}"


# ============================
# COMPARABLES
# ============================
def find_comps(postcode, data, limit=10, subject_price=None, nearby_postcodes=None):
    if data is None:
        return pd.DataFrame(columns=["price", "postcode", "street", "district"])

    comps_df = data.copy()
    normalized_postcode = normalize_postcode(postcode)
    sector_key = postcode_sector_key(postcode)
    outward_code = postcode_outward_code(postcode)
    distance_lookup = {}

    comps_df["normalized_postcode"] = comps_df["postcode"].apply(normalize_postcode)

    if nearby_postcodes:
        distance_lookup = {
            normalize_postcode(item.get("postcode")): float(item.get("distance", 0))
            for item in nearby_postcodes
            if item.get("postcode")
        }
        if distance_lookup:
            comps_df = comps_df[comps_df["normalized_postcode"].isin(distance_lookup.keys())]
            comps_df["distance_m"] = comps_df["normalized_postcode"].map(distance_lookup)
            comps_df["match_score"] = 10

    if normalized_postcode:
        comps_df["sector_key"] = comps_df["postcode"].apply(postcode_sector_key)
        comps_df["outward_code"] = comps_df["postcode"].apply(postcode_outward_code)
        if "match_score" not in comps_df.columns:
            comps_df["match_score"] = 0
            comps_df.loc[comps_df["outward_code"] == outward_code, "match_score"] += 1
            comps_df.loc[comps_df["sector_key"] == sector_key, "match_score"] += 2
            comps_df.loc[comps_df["normalized_postcode"] == normalized_postcode, "match_score"] += 3

            if (comps_df["match_score"] > 0).any():
                comps_df = comps_df[comps_df["match_score"] > 0]

    comps_df = comps_df.dropna(subset=["price"])
    if subject_price is not None:
        comps_df["price_gap"] = (comps_df["price"] - subject_price).abs()
    if "match_score" in comps_df.columns:
        sort_columns = ["match_score"]
        ascending = [False]
        if "distance_m" in comps_df.columns:
            sort_columns.append("distance_m")
            ascending.append(True)
        if "price_gap" in comps_df.columns:
            sort_columns.append("price_gap")
            ascending.append(True)
        sort_columns.append("price")
        ascending.append(True)
        comps_df = comps_df.sort_values(sort_columns, ascending=ascending)
    else:
        comps_df = comps_df.sort_values("price")

    return comps_df.head(limit).drop(
        columns=["normalized_postcode", "sector_key", "outward_code", "match_score", "price_gap"],
        errors="ignore",
    )


# ============================
# REFURB ENGINE
# ============================
def condition_multiplier(current, target):
    scale = {
        "Poor": 1.5,
        "Fair": 1.2,
        "Good": 1.0,
        "Very Good": 0.85,
        "Luxury": 1.3,
    }

    return scale[current] * scale[target]


def refurb_engine(items, contingency, multiplier):
    base = sum(items.values())
    adjusted = base * multiplier
    contingency_cost = adjusted * (contingency / 100)

    return {
        "base": round(base, 2),
        "adjusted": round(adjusted, 2),
        "contingency": round(contingency_cost, 2),
        "total": round(adjusted + contingency_cost, 2),
    }


# ============================
# DEAL ANALYSIS ENGINE
# ============================
def analyse(price, sqm, ppsqm, refurb_total):
    if price is None:
        price = 0

    gdv = sqm * ppsqm
    total = price + refurb_total + 13000
    profit = gdv - total
    roi = round((profit / total) * 100, 2) if total else 0.0

    return {
        "gdv": round(gdv),
        "profit": round(profit),
        "roi": roi,
    }


# ============================
# DEAL CALCULATORS
# ============================
def calculate_brr_scenario(inputs):
    purchase_price = inputs["purchase_price"]
    bridge_pct = percent_to_decimal(inputs["bridge_funding_pct"])
    bridge_rate = percent_to_decimal(inputs["bridge_monthly_rate"])
    refi_ltv = percent_to_decimal(inputs["refi_ltv"])
    mortgage_rate = percent_to_decimal(inputs["mortgage_interest_rate"])

    sdlt = calculate_template_sdlt(purchase_price, inputs["property_type"])
    purchase_costs = sdlt + inputs["auction_fees"] + inputs["valuation_fees"] + inputs["purchase_legal_fees"]
    development_costs = inputs["refurb_cost"] + inputs["planning_cost"] + inputs["holding_cost"]

    gross_bridge = purchase_price * bridge_pct
    bridge_arrangement_fee = gross_bridge * percent_to_decimal(inputs["arrangement_fee_pct"])
    bridge_broker_fee = gross_bridge * percent_to_decimal(inputs["bridge_broker_fee_pct"])
    bridging_interest = gross_bridge * bridge_rate * inputs["bridge_months"]

    retained_fees = bridge_arrangement_fee + bridge_broker_fee + bridging_interest if inputs["retain_fees"] else 0
    advance_received = gross_bridge - retained_fees if inputs["retain_fees"] else gross_bridge

    refinance_proceeds = inputs["gdv"] * refi_ltv
    annual_rent = inputs["monthly_rent"] * 12
    annual_mortgage_cost = refinance_proceeds * mortgage_rate

    project_costs = (
        purchase_price
        + purchase_costs
        + development_costs
        + bridge_arrangement_fee
        + bridge_broker_fee
        + bridging_interest
        + inputs["refi_legal_fees"]
        + inputs["refi_broker_fees"]
    )

    cash_required = project_costs - advance_received
    cash_left_in_deal = project_costs - refinance_proceeds
    annual_profit = annual_rent - annual_mortgage_cost - inputs["annual_other_expenses"]
    equity_created = inputs["gdv"] - project_costs

    return {
        "sdlt": sdlt,
        "purchase_costs": purchase_costs,
        "development_costs": development_costs,
        "gross_bridge": gross_bridge,
        "net_bridge": advance_received,
        "bridging_interest": bridging_interest,
        "bridge_arrangement_fee": bridge_arrangement_fee,
        "bridge_broker_fee": bridge_broker_fee,
        "project_costs": project_costs,
        "cash_required": cash_required,
        "refinance_proceeds": refinance_proceeds,
        "cash_left_in_deal": cash_left_in_deal,
        "annual_rent": annual_rent,
        "annual_mortgage_cost": annual_mortgage_cost,
        "annual_profit": annual_profit,
        "cash_on_cash_roi": safe_percent(annual_profit, cash_left_in_deal),
        "equity_created": equity_created,
        "profit_on_cost": safe_percent(equity_created, project_costs),
    }


def calculate_flip_scenario(inputs):
    purchase_price = inputs["purchase_price"]
    bridge_pct = percent_to_decimal(inputs["bridge_funding_pct"])
    bridge_rate = percent_to_decimal(inputs["bridge_monthly_rate"])

    sdlt = calculate_template_sdlt(purchase_price, inputs["property_type"])
    purchase_costs = sdlt + inputs["auction_fees"] + inputs["valuation_fees"] + inputs["purchase_legal_fees"]
    development_costs = inputs["refurb_cost"] + inputs["planning_cost"] + inputs["holding_cost"]

    gross_bridge = purchase_price * bridge_pct
    bridge_arrangement_fee = gross_bridge * percent_to_decimal(inputs["arrangement_fee_pct"])
    bridge_broker_fee = gross_bridge * percent_to_decimal(inputs["bridge_broker_fee_pct"])
    bridging_interest = gross_bridge * bridge_rate * inputs["bridge_months"]

    retained_fees = bridge_arrangement_fee + bridge_broker_fee + bridging_interest if inputs["retain_fees"] else 0
    advance_received = gross_bridge - retained_fees if inputs["retain_fees"] else gross_bridge

    agent_fees = inputs["sale_price"] * percent_to_decimal(inputs["agent_fee_pct"])
    net_sale_proceeds = inputs["sale_price"] - agent_fees - inputs["sale_legal_fees"]

    project_costs = (
        purchase_price
        + purchase_costs
        + development_costs
        + bridge_arrangement_fee
        + bridge_broker_fee
        + bridging_interest
    )

    total_costs_with_sale = project_costs + agent_fees + inputs["sale_legal_fees"]
    cash_required = total_costs_with_sale - advance_received
    profit = net_sale_proceeds - project_costs

    return {
        "sdlt": sdlt,
        "purchase_costs": purchase_costs,
        "development_costs": development_costs,
        "gross_bridge": gross_bridge,
        "net_bridge": advance_received,
        "bridging_interest": bridging_interest,
        "bridge_arrangement_fee": bridge_arrangement_fee,
        "bridge_broker_fee": bridge_broker_fee,
        "agent_fees": agent_fees,
        "project_costs": project_costs,
        "total_costs_with_sale": total_costs_with_sale,
        "cash_required": cash_required,
        "net_sale_proceeds": net_sale_proceeds,
        "profit": profit,
        "profit_on_cash": safe_percent(profit, cash_required),
        "profit_margin": safe_percent(profit, inputs["sale_price"]),
    }


def render_brr_calculator():
    st.markdown("### BRR Calculator")

    inputs_col, outputs_col = st.columns([1.2, 1])

    with inputs_col:
        st.markdown("**Property & Purchase**")
        property_type = st.radio(
            "Stamp Duty basis",
            ["Residential", "Commercial"],
            horizontal=True,
            key="brr_property_type",
        )
        property_address = st.text_input("Property address", key="brr_property_address")
        property_reference = st.text_input("Property reference", key="brr_property_reference")

        purchase_col, gdv_col = st.columns(2)
        purchase_price = purchase_col.number_input(
            "Purchase price",
            min_value=0,
            value=110000,
            step=5000,
            key="brr_purchase_price",
        )
        gdv = gdv_col.number_input(
            "GDV / refinance value",
            min_value=0,
            value=165000,
            step=5000,
            key="brr_gdv",
        )

        st.markdown("**Bridge & Exit**")
        bridge_funding_pct = st.slider(
            "% of purchase funded with bridge",
            min_value=0,
            max_value=100,
            value=75,
            step=1,
            key="brr_bridge_funding_pct",
        )
        bridge_monthly_rate = st.slider(
            "Monthly bridge interest %",
            min_value=0.0,
            max_value=2.5,
            value=1.0,
            step=0.05,
            key="brr_bridge_monthly_rate",
        )
        bridge_months = st.slider(
            "Bridge retained months",
            min_value=1,
            max_value=18,
            value=6,
            step=1,
            key="brr_bridge_months",
        )
        retain_fees = st.toggle("Retain bridge fees inside the loan", value=True, key="brr_retain_fees")
        refi_ltv = st.slider(
            "Refinance LTV %",
            min_value=50,
            max_value=85,
            value=75,
            step=1,
            key="brr_refi_ltv",
        )
        mortgage_interest_rate = st.slider(
            "Mortgage interest %",
            min_value=0.0,
            max_value=12.0,
            value=5.5,
            step=0.1,
            key="brr_mortgage_interest_rate",
        )

        fee_col_a, fee_col_b = st.columns(2)
        arrangement_fee_pct = fee_col_a.slider(
            "Arrangement fee %",
            min_value=0.0,
            max_value=5.0,
            value=2.0,
            step=0.1,
            key="brr_arrangement_fee_pct",
        )
        bridge_broker_fee_pct = fee_col_b.slider(
            "Bridge broker fee %",
            min_value=0.0,
            max_value=5.0,
            value=1.5,
            step=0.1,
            key="brr_bridge_broker_fee_pct",
        )

        st.markdown("**Works & Fees**")
        cost_col_a, cost_col_b, cost_col_c = st.columns(3)
        refurb_cost = cost_col_a.number_input("Refurb", min_value=0, value=25000, step=1000, key="brr_refurb_cost")
        planning_cost = cost_col_b.number_input("Planning", min_value=0, value=0, step=500, key="brr_planning_cost")
        holding_cost = cost_col_c.number_input("Holding", min_value=0, value=3000, step=500, key="brr_holding_cost")

        fee_cost_col_a, fee_cost_col_b, fee_cost_col_c = st.columns(3)
        auction_fees = fee_cost_col_a.number_input("Auction fees", min_value=0, value=0, step=500, key="brr_auction_fees")
        valuation_fees = fee_cost_col_b.number_input("Valuation fees", min_value=0, value=750, step=250, key="brr_valuation_fees")
        purchase_legal_fees = fee_cost_col_c.number_input(
            "Purchase legals",
            min_value=0,
            value=1500,
            step=250,
            key="brr_purchase_legal_fees",
        )

        refi_fee_col_a, refi_fee_col_b = st.columns(2)
        refi_legal_fees = refi_fee_col_a.number_input(
            "Refi legals",
            min_value=0,
            value=1200,
            step=250,
            key="brr_refi_legal_fees",
        )
        refi_broker_fees = refi_fee_col_b.number_input(
            "Refi broker fees",
            min_value=0,
            value=1200,
            step=250,
            key="brr_refi_broker_fees",
        )

        st.markdown("**Rental**")
        rent_col_a, rent_col_b = st.columns(2)
        monthly_rent = rent_col_a.number_input("Monthly rent", min_value=0, value=1100, step=50, key="brr_monthly_rent")
        annual_other_expenses = rent_col_b.number_input(
            "Annual other expenses & voids",
            min_value=0,
            value=1800,
            step=100,
            key="brr_annual_other_expenses",
        )

    brr_outputs = calculate_brr_scenario(
        {
            "property_type": property_type,
            "property_address": property_address,
            "property_reference": property_reference,
            "purchase_price": purchase_price,
            "gdv": gdv,
            "bridge_funding_pct": bridge_funding_pct,
            "bridge_monthly_rate": bridge_monthly_rate,
            "bridge_months": bridge_months,
            "retain_fees": retain_fees,
            "refi_ltv": refi_ltv,
            "mortgage_interest_rate": mortgage_interest_rate,
            "arrangement_fee_pct": arrangement_fee_pct,
            "bridge_broker_fee_pct": bridge_broker_fee_pct,
            "refurb_cost": refurb_cost,
            "planning_cost": planning_cost,
            "holding_cost": holding_cost,
            "auction_fees": auction_fees,
            "valuation_fees": valuation_fees,
            "purchase_legal_fees": purchase_legal_fees,
            "refi_legal_fees": refi_legal_fees,
            "refi_broker_fees": refi_broker_fees,
            "monthly_rent": monthly_rent,
            "annual_other_expenses": annual_other_expenses,
        }
    )

    with outputs_col:
        st.markdown("**Deal Snapshot**")
        top_left, top_right = st.columns(2)
        top_left.metric("Cash required", format_money(brr_outputs["cash_required"]))
        top_right.metric("Cash left in deal", format_money(brr_outputs["cash_left_in_deal"]))

        upper_left, upper_right = st.columns(2)
        upper_left.metric("Refinance proceeds", format_money(brr_outputs["refinance_proceeds"]))
        upper_right.metric("Equity created", format_money(brr_outputs["equity_created"]))

        mid_left, mid_right = st.columns(2)
        mid_left.metric("Annual profit", format_money(brr_outputs["annual_profit"]))
        mid_right.metric("Cash-on-cash ROI", format_percent(brr_outputs["cash_on_cash_roi"]))

        bottom_left, bottom_right = st.columns(2)
        bottom_left.metric("Profit on cost", format_percent(brr_outputs["profit_on_cost"]))
        bottom_right.metric("Stamp duty", format_money(brr_outputs["sdlt"]))

        st.divider()
        render_breakdown_table(
            "Finance Breakdown",
            {
                "Gross bridge": brr_outputs["gross_bridge"],
                "Net bridge received": brr_outputs["net_bridge"],
                "Bridge interest": brr_outputs["bridging_interest"],
                "Arrangement fee": brr_outputs["bridge_arrangement_fee"],
                "Bridge broker fee": brr_outputs["bridge_broker_fee"],
            },
        )
        render_breakdown_table(
            "Project Breakdown",
            {
                "Purchase costs": brr_outputs["purchase_costs"],
                "Development costs": brr_outputs["development_costs"],
                "Project costs": brr_outputs["project_costs"],
                "Annual rent": brr_outputs["annual_rent"],
                "Annual mortgage cost": brr_outputs["annual_mortgage_cost"],
            },
        )

    st.caption(
        "SDLT follows the banding shown in your original template, so the calculator stays aligned with your deal stacker workbook."
    )
    return brr_outputs, {
        "project_type": "BRR",
        "property_address": property_address,
        "property_reference": property_reference,
        "purchase_price": purchase_price,
        "gdv": gdv,
        "refurb_cost": refurb_cost,
        "holding_cost": holding_cost,
        "planning_cost": planning_cost,
        "bridge_months": bridge_months,
        "bridge_funding_pct": bridge_funding_pct,
        "monthly_rent": monthly_rent,
    }


def render_flip_calculator():
    st.markdown("### Flip Calculator")

    inputs_col, outputs_col = st.columns([1.2, 1])

    with inputs_col:
        st.markdown("**Property & Sale**")
        property_type = st.radio(
            "Stamp Duty basis",
            ["Residential", "Commercial"],
            horizontal=True,
            key="flip_property_type",
        )
        property_address = st.text_input("Property address", key="flip_property_address")
        property_reference = st.text_input("Property reference", key="flip_property_reference")

        purchase_col, sale_col = st.columns(2)
        purchase_price = purchase_col.number_input(
            "Purchase price",
            min_value=0,
            value=125000,
            step=5000,
            key="flip_purchase_price",
        )
        sale_price = sale_col.number_input(
            "Sale price",
            min_value=0,
            value=185000,
            step=5000,
            key="flip_sale_price",
        )

        st.markdown("**Bridge**")
        bridge_funding_pct = st.slider(
            "% of purchase funded with bridge",
            min_value=0,
            max_value=100,
            value=75,
            step=1,
            key="flip_bridge_funding_pct",
        )
        bridge_monthly_rate = st.slider(
            "Monthly bridge interest %",
            min_value=0.0,
            max_value=2.5,
            value=1.0,
            step=0.05,
            key="flip_bridge_monthly_rate",
        )
        bridge_months = st.slider(
            "Months retained",
            min_value=1,
            max_value=18,
            value=6,
            step=1,
            key="flip_bridge_months",
        )
        retain_fees = st.toggle("Retain bridge fees inside the loan", value=True, key="flip_retain_fees")

        fee_col_a, fee_col_b = st.columns(2)
        arrangement_fee_pct = fee_col_a.slider(
            "Arrangement fee %",
            min_value=0.0,
            max_value=5.0,
            value=2.0,
            step=0.1,
            key="flip_arrangement_fee_pct",
        )
        bridge_broker_fee_pct = fee_col_b.slider(
            "Bridge broker fee %",
            min_value=0.0,
            max_value=5.0,
            value=1.5,
            step=0.1,
            key="flip_bridge_broker_fee_pct",
        )

        st.markdown("**Works & Costs**")
        cost_col_a, cost_col_b, cost_col_c = st.columns(3)
        refurb_cost = cost_col_a.number_input("Refurb", min_value=0, value=30000, step=1000, key="flip_refurb_cost")
        planning_cost = cost_col_b.number_input("Planning", min_value=0, value=0, step=500, key="flip_planning_cost")
        holding_cost = cost_col_c.number_input("Holding", min_value=0, value=2500, step=500, key="flip_holding_cost")

        fee_cost_col_a, fee_cost_col_b, fee_cost_col_c = st.columns(3)
        auction_fees = fee_cost_col_a.number_input("Auction fees", min_value=0, value=0, step=500, key="flip_auction_fees")
        valuation_fees = fee_cost_col_b.number_input("Valuation fees", min_value=0, value=750, step=250, key="flip_valuation_fees")
        purchase_legal_fees = fee_cost_col_c.number_input(
            "Purchase legals",
            min_value=0,
            value=1500,
            step=250,
            key="flip_purchase_legal_fees",
        )

        st.markdown("**Sale Costs**")
        sale_fee_col_a, sale_fee_col_b = st.columns(2)
        sale_legal_fees = sale_fee_col_a.number_input(
            "Sale legals",
            min_value=0,
            value=1500,
            step=250,
            key="flip_sale_legal_fees",
        )
        agent_fee_pct = sale_fee_col_b.slider(
            "Agent fee %",
            min_value=0.0,
            max_value=5.0,
            value=1.5,
            step=0.1,
            key="flip_agent_fee_pct",
        )

    flip_outputs = calculate_flip_scenario(
        {
            "property_type": property_type,
            "property_address": property_address,
            "property_reference": property_reference,
            "purchase_price": purchase_price,
            "sale_price": sale_price,
            "bridge_funding_pct": bridge_funding_pct,
            "bridge_monthly_rate": bridge_monthly_rate,
            "bridge_months": bridge_months,
            "retain_fees": retain_fees,
            "arrangement_fee_pct": arrangement_fee_pct,
            "bridge_broker_fee_pct": bridge_broker_fee_pct,
            "refurb_cost": refurb_cost,
            "planning_cost": planning_cost,
            "holding_cost": holding_cost,
            "auction_fees": auction_fees,
            "valuation_fees": valuation_fees,
            "purchase_legal_fees": purchase_legal_fees,
            "sale_legal_fees": sale_legal_fees,
            "agent_fee_pct": agent_fee_pct,
        }
    )

    with outputs_col:
        st.markdown("**Deal Snapshot**")
        top_left, top_right = st.columns(2)
        top_left.metric("Cash required", format_money(flip_outputs["cash_required"]))
        top_right.metric("Net sale proceeds", format_money(flip_outputs["net_sale_proceeds"]))

        mid_left, mid_right = st.columns(2)
        mid_left.metric("Profit / loss", format_money(flip_outputs["profit"]))
        mid_right.metric("Profit on cash", format_percent(flip_outputs["profit_on_cash"]))

        bottom_left, bottom_right = st.columns(2)
        bottom_left.metric("Profit margin", format_percent(flip_outputs["profit_margin"]))
        bottom_right.metric("Total costs", format_money(flip_outputs["total_costs_with_sale"]))

        st.divider()
        render_breakdown_table(
            "Finance Breakdown",
            {
                "Stamp duty": flip_outputs["sdlt"],
                "Gross bridge": flip_outputs["gross_bridge"],
                "Net bridge received": flip_outputs["net_bridge"],
                "Bridge interest": flip_outputs["bridging_interest"],
                "Arrangement fee": flip_outputs["bridge_arrangement_fee"],
                "Bridge broker fee": flip_outputs["bridge_broker_fee"],
            },
        )
        render_breakdown_table(
            "Project Breakdown",
            {
                "Purchase costs": flip_outputs["purchase_costs"],
                "Development costs": flip_outputs["development_costs"],
                "Agent fees": flip_outputs["agent_fees"],
                "Project costs before sale": flip_outputs["project_costs"],
                "Total costs incl. sale": flip_outputs["total_costs_with_sale"],
                "Net sale proceeds": flip_outputs["net_sale_proceeds"],
            },
        )

    st.caption(
        "This mirrors the workbook as a deal-first UI: sliders for leverage and fees, then live outputs for profit, margin, and cash required."
    )
    return flip_outputs, {
        "project_type": "Flip",
        "property_address": property_address,
        "property_reference": property_reference,
        "purchase_price": purchase_price,
        "sale_price": sale_price,
        "refurb_cost": refurb_cost,
        "holding_cost": holding_cost,
        "planning_cost": planning_cost,
        "bridge_months": bridge_months,
        "bridge_funding_pct": bridge_funding_pct,
    }


# ============================
# SESSION STATE
# ============================
if "analysis_done" not in st.session_state:
    st.session_state.analysis_done = False

if "area_intelligence_result" not in st.session_state:
    st.session_state.area_intelligence_result = None


def seed_project_type_defaults():
    if not st.session_state.get("analysis_done"):
        return

    data = st.session_state.get("data", {})
    result = st.session_state.get("result", {})
    refurb = st.session_state.get("refurb", {})
    selected_postcode = st.session_state.get("selected_postcode", "")
    selected_price = st.session_state.get("selected_price", 0) or 0
    selected_name = data.get("name", "")
    refurb_total = int(round(refurb.get("total", 25000) or 25000))
    gdv = int(round(result.get("gdv", selected_price) or selected_price))
    project_seed = f"{selected_name}|{selected_postcode}|{selected_price}|{gdv}|{refurb_total}"

    if st.session_state.get("project_defaults_seed") == project_seed:
        return

    defaults = {
        "brr_property_address": selected_name,
        "brr_property_reference": selected_postcode,
        "brr_purchase_price": int(selected_price),
        "brr_gdv": int(gdv),
        "brr_refurb_cost": refurb_total,
        "flip_property_address": selected_name,
        "flip_property_reference": selected_postcode,
        "flip_purchase_price": int(selected_price),
        "flip_sale_price": int(gdv),
        "flip_refurb_cost": refurb_total,
    }

    for key, value in defaults.items():
        st.session_state[key] = value

    st.session_state.project_defaults_seed = project_seed


def build_investor_pack(project_details, project_outputs, investor_inputs, analysis_data=None):
    property_name = project_details.get("property_address") or "Selected property"
    project_type = project_details.get("project_type", "Project")
    purchase_price = project_details.get("purchase_price", 0)
    refurb_cost = project_details.get("refurb_cost", 0)
    available_cash = investor_inputs.get("available_cash", 0)
    investor_required = investor_inputs.get("investor_required", 0)
    target_return = investor_inputs.get("target_return_pct", 0)
    proposed_share = investor_inputs.get("profit_share_pct", 0)

    headline_value = project_outputs.get("gdv") or project_details.get("sale_price") or project_outputs.get("net_sale_proceeds", 0)
    headline_profit = project_outputs.get("equity_created")
    if headline_profit is None:
        headline_profit = project_outputs.get("profit", 0)

    summary_line = analysis_data.get("name") if analysis_data else property_name

    return f"""# Investor Pack - {property_name}

## Opportunity Snapshot
- Property: {summary_line}
- Project Type: {project_type}
- Purchase Price: {format_money(purchase_price)}
- Refurb Budget: {format_money(refurb_cost)}
- Headline Exit Value: {format_money(headline_value)}
- Forecast Profit / Equity Uplift: {format_money(headline_profit)}

## Capital Stack
- Total Cash Required: {format_money(project_outputs.get('cash_required', 0))}
- Operator Cash Going In: {format_money(available_cash)}
- Investor Funds Required: {format_money(investor_required)}
- Proposed Investor Target Return: {target_return:.1f}%
- Proposed Investor Profit Share: {proposed_share:.1f}%

## Why This Deal Works
- The project has already been underwritten through the in-app analysis and project builder.
- Refurb, finance, and exit assumptions are built into one workflow rather than split across separate sheets.
- The deal can be positioned either as a refinance-led BRR project or a clean flip, depending on the selected project type.

## Key Project Numbers
- Stamp Duty / Entry Tax: {format_money(project_outputs.get('sdlt', 0))}
- Purchase Costs: {format_money(project_outputs.get('purchase_costs', 0))}
- Development Costs: {format_money(project_outputs.get('development_costs', 0))}
- Gross Bridge: {format_money(project_outputs.get('gross_bridge', 0))}
- Bridge Interest: {format_money(project_outputs.get('bridging_interest', 0))}

## Exit View
"""


def build_investor_pack_exit_section(project_details, project_outputs):
    if project_details.get("project_type") == "BRR":
        return f"""- Refinance Proceeds: {format_money(project_outputs.get('refinance_proceeds', 0))}
- Cash Left In Deal: {format_money(project_outputs.get('cash_left_in_deal', 0))}
- Annual Profit: {format_money(project_outputs.get('annual_profit', 0))}
- Cash-on-Cash ROI: {format_percent(project_outputs.get('cash_on_cash_roi', 0))}
- Profit on Cost: {format_percent(project_outputs.get('profit_on_cost', 0))}
"""

    return f"""- Net Sale Proceeds: {format_money(project_outputs.get('net_sale_proceeds', 0))}
- Total Costs Including Sale: {format_money(project_outputs.get('total_costs_with_sale', 0))}
- Profit / Loss: {format_money(project_outputs.get('profit', 0))}
- Profit on Cash: {format_percent(project_outputs.get('profit_on_cash', 0))}
- Profit Margin: {format_percent(project_outputs.get('profit_margin', 0))}
"""


def build_investor_pack_risks_section(project_details):
    return f"""
## Risk Control
- Conservative timeline assumed: {project_details.get('bridge_months', 0)} months.
- Refinance / sale route can be monitored and adjusted as market feedback comes in.
- Cost planning includes refurb, holding, finance, and legal costs in one place.

## Investor Positioning
- This is suited to an investor who wants asset-backed exposure with a defined use of funds.
- The raise is specifically to close the capital stack, not to fund an undefined future budget.
- The project can be presented with transparent entry, works, and exit assumptions.
"""


def build_investor_email(project_details, project_outputs, investor_inputs, analysis_data=None):
    property_name = project_details.get("property_address") or "this deal"
    project_type = project_details.get("project_type", "project")
    investor_required = investor_inputs.get("investor_required", 0)
    headline_profit = project_outputs.get("equity_created")
    if headline_profit is None:
        headline_profit = project_outputs.get("profit", 0)

    summary_line = analysis_data.get("name") if analysis_data else property_name

    return f"""Subject: Investor opportunity - {summary_line}

Hi [Investor Name],

I hope you're well. I have a new {project_type.lower()} opportunity that I think could be a strong fit for you.

The deal is centred on {summary_line}. The purchase price is {format_money(project_details.get('purchase_price', 0))} with a refurb budget of {format_money(project_details.get('refurb_cost', 0))}. Based on the current underwriting, the projected upside is around {format_money(headline_profit)}.

I'm looking to raise {format_money(investor_required)} to complete the capital stack. I am putting in {format_money(investor_inputs.get('available_cash', 0))} personally, and I can share the full investor pack with the entry costs, works budget, finance assumptions, and exit numbers.

If you're open to it, I'd love to send the pack over and talk you through the project this week.

Best,
[Your Name]
"""


# ============================
# CALCULATOR PAGE
# ============================
def render_calculator_page():
    seed_project_type_defaults()
    render_calculator_styles()

    st.markdown(
        """
        <div class="calc-hero">
            <h3>Project Builder</h3>
            <p>Choose a project type and shape the whole deal with live metrics instead of spreadsheet cells.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if st.session_state.get("analysis_done"):
        st.caption("Project Type inputs are prefilled from the current analysed deal and refurb total.")
    st.markdown(
        """
        <div class="calc-note">
            Tweak leverage, timing, fees, refurb spend, and exit assumptions. The numbers update instantly so users can pressure-test each project type without touching a workbook.
        </div>
        """,
        unsafe_allow_html=True,
    )

    project_type = st.radio(
        "Project Type",
        ["BRR", "Flip"],
        horizontal=True,
        key="active_project_type",
    )

    if project_type == "BRR":
        project_outputs, project_details = render_brr_calculator()
    else:
        project_outputs, project_details = render_flip_calculator()

    template_bytes, template_source = load_calculator_template_bytes()
    with st.expander("Original workbook template"):
        if template_bytes:
            st.write(f"Original workbook loaded from {template_source}.")
            st.download_button(
                "Download original template",
                data=template_bytes,
                file_name=DEAL_TEMPLATE_FILE,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        else:
            st.write("No workbook file is needed for the app-style calculator, but you can still add the template to the repo later if you want it downloadable.")

    return project_outputs, project_details


def render_investor_funding_section(project_outputs, project_details, analysis_data=None):
    st.subheader("Funding & Investor Raise")
    st.caption("Track how much cash you already have, identify any funding gap, and generate investor-ready material when outside capital is needed.")

    cash_required = max(float(project_outputs.get("cash_required", 0)), 0.0)

    capital_col, investor_col = st.columns([1.2, 1])

    with capital_col:
        available_cash = st.number_input(
            "Your available cash to invest",
            min_value=0,
            value=25000,
            step=5000,
            key="available_cash_to_invest",
        )
        use_investor_funds = st.toggle(
            "Use investor funds for this project",
            value=cash_required > available_cash,
            key="use_investor_funds",
        )

    investor_required = max(cash_required - available_cash, 0.0) if use_investor_funds else 0.0

    with investor_col:
        st.metric("Total cash required", format_money(cash_required))
        st.metric("Your cash", format_money(available_cash))
        st.metric("Investor funds needed", format_money(investor_required))

    if not use_investor_funds or investor_required <= 0:
        st.success("This project is fully covered by your own cash based on the current assumptions.")
        return

    terms_col_a, terms_col_b, terms_col_c = st.columns(3)
    target_return_pct = terms_col_a.slider(
        "Target investor return %",
        min_value=5.0,
        max_value=25.0,
        value=12.0,
        step=0.5,
        key="target_investor_return_pct",
    )
    profit_share_pct = terms_col_b.slider(
        "Investor profit share %",
        min_value=10.0,
        max_value=70.0,
        value=35.0,
        step=1.0,
        key="investor_profit_share_pct",
    )
    investor_role = terms_col_c.selectbox(
        "Investor style",
        ["Private investor", "Joint venture partner", "Family office", "Angel investor"],
        key="investor_role",
    )

    investor_inputs = {
        "available_cash": available_cash,
        "investor_required": investor_required,
        "target_return_pct": target_return_pct,
        "profit_share_pct": profit_share_pct,
        "investor_role": investor_role,
    }

    pack_text = (
        build_investor_pack(project_details, project_outputs, investor_inputs, analysis_data)
        + build_investor_pack_exit_section(project_details, project_outputs)
        + build_investor_pack_risks_section(project_details)
    )
    email_text = build_investor_email(project_details, project_outputs, investor_inputs, analysis_data)

    st.markdown("**Investor Pitch Snapshot**")
    pitch_col1, pitch_col2, pitch_col3 = st.columns(3)
    pitch_col1.metric("Raise target", format_money(investor_required))
    pitch_col2.metric("Investor target return", format_percent(target_return_pct))
    pitch_col3.metric("Investor profit share", format_percent(profit_share_pct))

    button_col1, button_col2 = st.columns(2)
    with button_col1:
        generate_pack = st.button("Generate Investor Pack", use_container_width=True, key="generate_investor_pack")
    with button_col2:
        generate_email = st.button("Generate Investor Email", use_container_width=True, key="generate_investor_email")

    if generate_pack or generate_email:
        st.markdown("### Investor Materials")

    if generate_pack:
        st.markdown("**Investor Pack Preview**")
        st.markdown(pack_text)
        st.download_button(
            "Download investor pack",
            data=pack_text.encode("utf-8"),
            file_name="investor-pack.md",
            mime="text/markdown",
            key="download_investor_pack",
        )

    if generate_email:
        st.markdown("**Investor Email Draft**")
        st.code(email_text, language="text")
        st.download_button(
            "Download investor email",
            data=email_text.encode("utf-8"),
            file_name="investor-email.txt",
            mime="text/plain",
            key="download_investor_email",
        )


def render_area_intelligence_page():
    st.subheader("Area Intelligence")

    selected_postcode = normalize_postcode(st.session_state.get("selected_postcode", ""))
    selected_name = ""
    if st.session_state.get("analysis_done") and st.session_state.get("data"):
        selected_name = st.session_state["data"].get("name", "")

    if selected_name:
        st.caption(f"Using the selected property from your deal analysis: {selected_name}")
    else:
        st.caption("Analyse a property first, or enter a postcode below to pull local EPC-backed comparables.")

    with st.form("area_intelligence_form"):
        input_col, settings_col = st.columns([2, 1])

        with input_col:
            postcode = st.text_input(
                "Subject postcode",
                value=selected_postcode,
                placeholder="CA1 2AB",
                help="Enter a postcode here, not an EPC certificate number.",
            )
            manual_token = st.text_input(
                "EPC bearer token (optional override)",
                type="password",
                help="Leave blank if you already store EPC_BEARER_TOKEN in Streamlit secrets or an environment variable.",
            )

        with settings_col:
            max_comps = st.slider("Comparable sample size", min_value=3, max_value=15, value=8, step=1)
            radius_meters = st.slider("Search radius (metres)", min_value=250, max_value=2000, value=1200, step=50)
            submitted = st.form_submit_button("Run Area Intelligence", use_container_width=True)

    if submitted:
        normalized_postcode = normalize_postcode(postcode)
        if looks_like_certificate_number(postcode):
            st.error("That looks like an EPC certificate number. Enter the property's postcode here instead.")
        elif not normalized_postcode:
            st.error("Add a postcode first.")
        else:
            land_data = load_data()
            subject_price = st.session_state.get("selected_price")
            nearby_postcodes = fetch_nearby_postcodes(normalized_postcode, radius_meters=radius_meters, limit=80)
            comps = find_comps(
                normalized_postcode,
                land_data,
                limit=max_comps,
                subject_price=subject_price,
                nearby_postcodes=nearby_postcodes,
            )
            token = get_epc_bearer_token(manual_token)
            enriched_df, api_status = enrich_comparables_with_epc(comps, token)
            st.session_state.area_intelligence_result = {
                "postcode": normalized_postcode,
                "sample_size": max_comps,
                "radius_meters": radius_meters,
                "nearby_postcodes_found": len(nearby_postcodes),
                "api_status": api_status,
                "has_token": bool(token),
                "table": enriched_df,
                "subject_price": subject_price,
            }

    result = st.session_state.get("area_intelligence_result")
    if not result:
        st.info("Run the postcode search to build local price-per-sqm intelligence.")
        return

    result_df = result["table"]
    matched_df = result_df.dropna(subset=["floor_area_sqm", "price_per_sqm"]) if not result_df.empty else pd.DataFrame()

    st.markdown(f"**Postcode focus:** {result['postcode']}")
    st.caption(
        f"Nearby sold properties are pulled from your dataset using postcodes within roughly {result['radius_meters']} metres of the subject postcode."
    )

    if result["api_status"] == "missing_token":
        st.warning(
            "EPC enrichment is switched off because no bearer token is configured. "
            "Add `EPC_BEARER_TOKEN` to Streamlit secrets or paste a temporary token above."
        )
    elif result["api_status"] == "auth_error":
        st.error(
            "The EPC API token was rejected. Check the bearer token from the government's new energy data service."
        )

    metric_col1, metric_col2, metric_col3 = st.columns(3)
    metric_col1.metric("Comparables found", str(len(result_df)))
    metric_col2.metric("Nearby postcodes checked", str(result.get("nearby_postcodes_found", 0)))
    metric_col3.metric(
        "Median price per sqm",
        format_money_per_sqm(matched_df["price_per_sqm"].median()) if not matched_df.empty else "N/A",
    )

    if not matched_df.empty:
        epc_metric_col1, epc_metric_col2 = st.columns(2)
        epc_metric_col1.metric("EPC floor areas matched", str(len(matched_df)))
        epc_metric_col2.metric("Match rate", format_percent(safe_percent(len(matched_df), len(result_df))))

    if not matched_df.empty:
        detail_col1, detail_col2, detail_col3 = st.columns(3)
        detail_col1.metric("Average floor area", f"{matched_df['floor_area_sqm'].mean():,.1f} sqm")
        detail_col2.metric("Average price per sqm", format_money_per_sqm(matched_df["price_per_sqm"].mean()))
        detail_col3.metric("Highest price per sqm", format_money_per_sqm(matched_df["price_per_sqm"].max()))

    if result_df.empty:
        st.info("No comparable sales were found in your local dataset for that postcode sector.")
        return

    display_df = result_df.copy()
    if "price" in display_df.columns:
        display_df["price"] = display_df["price"].apply(lambda value: format_money(value) if pd.notna(value) else "N/A")
    if "floor_area_sqm" in display_df.columns:
        display_df["floor_area_sqm"] = display_df["floor_area_sqm"].apply(
            lambda value: f"{value:,.1f}" if pd.notna(value) else "N/A"
        )
    if "distance_m" in display_df.columns:
        display_df["distance_m"] = display_df["distance_m"].apply(
            lambda value: f"{value:,.0f} m" if pd.notna(value) else "N/A"
        )
    if "price_per_sqm" in display_df.columns:
        display_df["price_per_sqm"] = display_df["price_per_sqm"].apply(
            lambda value: format_money_per_sqm(value) if pd.notna(value) else "N/A"
        )

    st.markdown("**Comparable table**")
    st.dataframe(
        display_df[
            [
                "street",
                "postcode",
                "distance_m",
                "price",
                "floor_area_sqm",
                "price_per_sqm",
                "epc_rating",
                "epc_status",
                "epc_address",
            ]
        ],
        use_container_width=True,
        hide_index=True,
    )

    if not matched_df.empty:
        st.markdown("**Price-per-sqm distribution**")
        chart_df = matched_df[["street", "price_per_sqm"]].set_index("street")
        st.bar_chart(chart_df)

    with st.expander("EPC API setup"):
        st.write(
            "The current official service uses a bearer token from `get-energy-performance-data.communities.gov.uk`. "
            "For Streamlit Cloud, add `EPC_BEARER_TOKEN` to your app secrets so the Area Intelligence page can fetch floor areas automatically."
        )


# ============================
# ANALYSE PAGE
# ============================
if page == "Analyse Deal":
    land_data = load_data()

    if land_data is None:
        st.warning("Comparable sales data could not be loaded. The deal analysis page will run without comps.")

    url = st.text_input("Rightmove URL")

    st.subheader("Condition")
    current = st.selectbox("Current Condition", ["Poor", "Fair", "Good", "Very Good"])
    target = st.selectbox("Target Condition", ["Good", "Very Good", "Luxury"])

    st.subheader("Refurb")
    kitchen = st.number_input("Kitchen", min_value=0, value=5000, step=500)
    bathroom = st.number_input("Bathroom", min_value=0, value=4000, step=500)
    electrics = st.number_input("Electrics", min_value=0, value=3000, step=500)
    plumbing = st.number_input("Plumbing", min_value=0, value=3000, step=500)
    plastering = st.number_input("Plastering", min_value=0, value=2500, step=500)
    flooring = st.number_input("Flooring", min_value=0, value=2000, step=500)
    paint = st.number_input("Paint", min_value=0, value=1500, step=500)

    contingency = st.slider("Contingency %", 0, 25, 10)

    if st.button("Analyse"):
        if not url:
            st.error("Add a Rightmove URL before running the analysis.")
        else:
            try:
                html = get_html(url)
                data = extract(html)

                price = parse_price(data["price"])
                sqm = estimate_sqm(data["bedrooms"])
                street, postcode = extract_location(data["name"])
                comps = find_comps(postcode, land_data, subject_price=price)
                multiplier = condition_multiplier(current, target)

                refurb = refurb_engine(
                    {
                        "kitchen": kitchen,
                        "bathroom": bathroom,
                        "electrics": electrics,
                        "plumbing": plumbing,
                        "plastering": plastering,
                        "flooring": flooring,
                        "paint": paint,
                    },
                    contingency,
                    multiplier,
                )

                result = analyse(price, sqm, 2400, refurb["total"])

                st.session_state.data = data
                st.session_state.result = result
                st.session_state.comps = comps
                st.session_state.refurb = refurb
                st.session_state.refurb_total = refurb["total"]
                st.session_state.current_condition = current
                st.session_state.target_condition = target
                st.session_state.selected_postcode = normalize_postcode(postcode)
                st.session_state.selected_street = street.title() if street else ""
                st.session_state.selected_price = price
                st.session_state.analysis_done = True

                st.success("Analysis complete")
                st.metric("ROI", f"{result['roi']}%")
                st.metric("Profit", format_money(result["profit"]))

            except Exception as exc:
                st.error(f"Analysis failed: {exc}")

    if st.session_state.analysis_done:
        st.divider()
        st.subheader("Deal Dashboard")

        data = st.session_state.data
        result = st.session_state.result
        refurb = st.session_state.refurb
        comps = st.session_state.comps

        st.subheader("Property Under Review")
        st.markdown(f"**{data['name']}**")

        st.subheader("Condition")
        st.write(f"Current: {st.session_state.current_condition}")
        st.write(f"Target: {st.session_state.target_condition}")

        st.subheader("Refurbishment")
        st.write(refurb)

        st.subheader("Comparables")
        if comps is not None and len(comps) > 0:
            for _, row in comps.iterrows():
                link = zoopla_link(row["street"], row["postcode"])
                st.markdown(
                    f"""
                    **{row['street']}**  
                    {format_money(int(row['price']))}  
                    [View Property]({link})
                    ---
                    """
                )
        else:
            st.write("No comparables found for this property yet.")

        st.subheader("Headline Numbers")
        col1, col2, col3 = st.columns(3)
        col1.metric("GDV", format_money(result["gdv"]))
        col2.metric("Profit", format_money(result["profit"]))
        col3.metric("ROI", f"{result['roi']}%")

    st.divider()
    st.subheader("Project Builder")
    st.caption("Use the analysed deal as the starting point for the full project numbers below.")
    project_outputs, project_details = render_calculator_page()

    st.divider()
    render_investor_funding_section(
        project_outputs,
        project_details,
        st.session_state.get("data"),
    )

elif page == "Area Intelligence":
    render_area_intelligence_page()

else:
    st.subheader(page)
    st.info("This section is ready for the next feature once you want to expand the app further.")
