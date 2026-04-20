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


# ============================
# NAVIGATION
# ============================
page = st.sidebar.selectbox(
    "Navigation",
    [
        "Analyse Deal",
        "Deal Calculator",
        "Compare Deals",
        "Portfolio",
        "Area Intelligence",
    ],
)

st.title("Property Investment Acadmey AI Assistant")


# ============================
# SHARED HELPERS
# ============================
def format_money(value):
    return f"GBP {value:,.0f}"


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
def find_comps(postcode, data):
    if data is None:
        return pd.DataFrame(columns=["price", "postcode", "street", "district"])

    comps_df = data.copy()

    if postcode:
        comps_df = comps_df[comps_df["postcode"].str.contains(postcode[:4], na=False)]

    comps_df = comps_df.dropna(subset=["price"])
    comps_df = comps_df.sort_values("price").head(10)

    return comps_df


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


# ============================
# SESSION STATE
# ============================
if "analysis_done" not in st.session_state:
    st.session_state.analysis_done = False


# ============================
# CALCULATOR PAGE
# ============================
def render_calculator_page():
    render_calculator_styles()

    brr_tab, flip_tab = st.tabs(["BRR Strategy", "Flip Strategy"])

    with brr_tab:
        render_brr_calculator()

    with flip_tab:
        render_flip_calculator()

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
                comps = find_comps(postcode, land_data)
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

elif page == "Deal Calculator":
    render_calculator_page()

else:
    st.subheader(page)
    st.info("This section is ready for the next feature once you want to expand the app further.")
