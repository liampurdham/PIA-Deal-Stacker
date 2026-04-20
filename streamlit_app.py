import streamlit as st
import requests
from bs4 import BeautifulSoup
import re
import pandas as pd
from pathlib import Path
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet


st.set_page_config(layout="wide")


# ============================
# CONFIG — DATA SOURCE
# ============================

# OPTION 1 (RECOMMENDED): GitHub RAW URL
DATA_URL = "https://raw.githubusercontent.com/liampurdham/PIA-Deal-Stacker/main/pp-complete.csv"

# OPTION 2: Local fallback (for dev)
LOCAL_FILE = "pp-complete.csv"


# ============================
# NAVIGATION
# ============================
page = st.sidebar.selectbox("Navigation", [
    "Analyse Deal",
    "Compare Deals",
    "Portfolio",
    "Area Intelligence"
])

st.title("🏠 Carlisle Property Investment OS")


# ============================
# DATA LOADER (CLOUD SAFE)
# ============================
@st.cache_data
def load_data():

    df = None

    # ---- TRY CLOUD FIRST ----
    try:
        df = pd.read_csv(DATA_URL)
        st.sidebar.success("Loaded dataset from GitHub")
    except Exception:
        st.sidebar.warning("GitHub load failed — trying local file")

        # ---- FALLBACK LOCAL ----
        base_dir = Path(__file__).resolve().parent
        file_path = base_dir / LOCAL_FILE

        if file_path.exists():
            df = pd.read_csv(file_path)
        else:
            st.error("❌ No dataset found (GitHub or local)")
            return None

    # ----------------------------
    # NORMALISE COLUMNS
    # ----------------------------
    df.columns = [
        "id","price","date","postcode","type","new","tenure",
        "paon","saon","street","locality","town","district","county",
        "category","status"
    ]

    df = df[["price", "postcode", "street", "district"]]

    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["postcode"] = df["postcode"].astype(str).str.lower()
    df["district"] = df["district"].astype(str).str.lower()

    # CLEAN STREET
    df["street"] = df["street"].fillna("Unknown Street")
    df["street"] = df["street"].astype(str).str.title()

    # FILTER CARLISLE
    df = df[df["district"].str.contains("carlisle", na=False)]

    return df


land_data = load_data()

if land_data is None:
    st.stop()


# ============================
# SCRAPER
# ============================
def get_html(url):
    return requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20).text


def extract(html):
    soup = BeautifulSoup(html, "html.parser")

    title = soup.find("h1")
    price = soup.find(string=re.compile(r"£[\d,]+"))

    bedrooms = None
    for t in soup.find_all(string=re.compile(r"\d+\s+bedroom", re.I)):
        bedrooms = t
        break

    return {
        "name": title.get_text(strip=True) if title else "Unknown",
        "price": price.strip() if price else None,
        "bedrooms": bedrooms or "Unknown"
    }


def parse_price(p):
    return int(re.sub(r"[^\d]", "", p)) if p else None


def estimate_sqm(beds):
    try:
        return int(re.findall(r"\d+", beds)[0]) * 25 + 40
    except:
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
    q = f"{street} {postcode}".replace(" ", "-")
    return f"https://www.zoopla.co.uk/for-sale/details/search/?q={q}"


# ============================
# COMPARABLES
# ============================
def find_comps(postcode):

    df = land_data.copy()

    if postcode:
        comps = df[df["postcode"].str.contains(postcode[:4], na=False)]
    else:
        comps = df

    comps = comps.dropna(subset=["price"])
    comps = comps.sort_values("price").head(10)

    return comps


# ============================
# REFURB ENGINE
# ============================
def condition_multiplier(current, target):

    scale = {
        "Poor": 1.5,
        "Fair": 1.2,
        "Good": 1.0,
        "Very Good": 0.85,
        "Luxury": 1.3
    }

    return scale[current] * scale[target]


def refurb_engine(items, contingency, multiplier):

    base = sum(items.values())
    adjusted = base * multiplier
    contingency_cost = adjusted * (contingency / 100)

    return {
        "base": base,
        "adjusted": adjusted,
        "contingency": contingency_cost,
        "total": adjusted + contingency_cost
    }


# ============================
# DEAL ENGINE
# ============================
def analyse(price, sqm, ppsqm):

    refurb = st.session_state.get("refurb_total", 30000)

    gdv = sqm * ppsqm
    total = price + refurb + 13000

    profit = gdv - total

    return {
        "gdv": round(gdv),
        "profit": round(profit),
        "roi": round((profit / total) * 100, 2)
    }


# ============================
# SESSION STATE
# ============================
if "analysis_done" not in st.session_state:
    st.session_state.analysis_done = False


# ============================
# ANALYSE PAGE
# ============================
if page == "Analyse Deal":

    url = st.text_input("Rightmove URL")

    st.subheader("🏚 Condition")
    current = st.selectbox("Current Condition", ["Poor","Fair","Good","Very Good"])
    target = st.selectbox("Target Condition", ["Good","Very Good","Luxury"])

    st.subheader("🏗 Refurb")
    kitchen = st.number_input("Kitchen", 5000)
    bathroom = st.number_input("Bathroom", 4000)
    electrics = st.number_input("Electrics", 3000)
    plumbing = st.number_input("Plumbing", 3000)
    plastering = st.number_input("Plastering", 2500)
    flooring = st.number_input("Flooring", 2000)
    paint = st.number_input("Paint", 1500)

    contingency = st.slider("Contingency %", 0, 25, 10)

    if st.button("Analyse"):

        html = get_html(url)
        data = extract(html)

        price = parse_price(data["price"])
        sqm = estimate_sqm(data["bedrooms"])

        street, postcode = extract_location(data["name"])

        comps = find_comps(postcode)

        multiplier = condition_multiplier(current, target)

        refurb = refurb_engine(
            {
                "kitchen": kitchen,
                "bathroom": bathroom,
                "electrics": electrics,
                "plumbing": plumbing,
                "plastering": plastering,
                "flooring": flooring,
                "paint": paint
            },
            contingency,
            multiplier
        )

        result = analyse(price, sqm, 2400)

        # STORE EVERYTHING
        st.session_state.data = data
        st.session_state.result = result
        st.session_state.comps = comps
        st.session_state.refurb = refurb
        st.session_state.refurb_total = refurb["total"]
        st.session_state.current_condition = current
        st.session_state.target_condition = target
        st.session_state.analysis_done = True

        st.success("Analysis Complete")

        st.metric("ROI", f"{result['roi']}%")
        st.metric("Profit", f"£{result['profit']}")


# ============================
# DASHBOARD
# ============================
if st.session_state.analysis_done:

    st.divider()
    st.subheader("📊 Deal Dashboard")

    data = st.session_state.data
    result = st.session_state.result
    refurb = st.session_state.refurb
    comps = st.session_state.comps

    # PROPERTY
    st.subheader("🏠 Property Under Review")
    st.markdown(f"**{data['name']}**")

    # CONDITION
    st.subheader("🏚 Condition")
    st.write(f"Current: {st.session_state.current_condition}")
    st.write(f"Target: {st.session_state.target_condition}")

    # REFURB
    st.subheader("🏗 Refurbishment")
    st.write(refurb)

    # COMPARABLES
    st.subheader("🏠 Comparables")

    if comps is not None and len(comps) > 0:

        for _, r in comps.iterrows():

            link = zoopla_link(r["street"], r["postcode"])

            st.markdown(
                f"""
                **{r['street']}**  
                💰 £{int(r['price'])}  
                🔗 [View Property]({link})
                ---
                """
            )
