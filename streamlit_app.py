from io import BytesIO
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

st.title("Carlisle Property Investment OS")


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


def excel_column_name(index):
    name = ""
    current = index + 1

    while current > 0:
        current, remainder = divmod(current - 1, 26)
        name = chr(65 + remainder) + name

    return name


def prepare_sheet_for_display(df):
    cleaned = df.dropna(how="all").dropna(axis=1, how="all").fillna("")
    cleaned.columns = [excel_column_name(i) for i in range(len(cleaned.columns))]
    cleaned.insert(0, "Row", range(1, len(cleaned) + 1))
    return cleaned


def parse_calculator_workbook(file_bytes):
    workbook = pd.read_excel(
        BytesIO(file_bytes),
        sheet_name=None,
        header=None,
        engine="openpyxl",
    )

    return {
        sheet_name: prepare_sheet_for_display(df)
        for sheet_name, df in workbook.items()
    }


@st.cache_data
def load_calculator_template():
    template_path = Path(__file__).resolve().parent / DEAL_TEMPLATE_FILE

    if template_path.exists():
        file_bytes = template_path.read_bytes()
        return file_bytes, parse_calculator_workbook(file_bytes), "repo file"

    try:
        response = requests.get(
            DEAL_TEMPLATE_URL,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
        )
        response.raise_for_status()
        file_bytes = response.content
        return file_bytes, parse_calculator_workbook(file_bytes), "GitHub"
    except Exception:
        return None, {}, None


def load_uploaded_calculator_template(uploaded_file):
    file_bytes = uploaded_file.getvalue()
    return file_bytes, parse_calculator_workbook(file_bytes)


def extract_sheet_summary(sheet_df):
    if "A" not in sheet_df.columns:
        return pd.DataFrame(columns=["Metric", "Value", "Status"])

    summary_rows = []
    value_column = "B" if "B" in sheet_df.columns else None
    status_column = "C" if "C" in sheet_df.columns else None

    for _, row in sheet_df.iterrows():
        metric = str(row.get("A", "")).strip()
        value = row.get(value_column, "") if value_column else ""
        status = row.get(status_column, "") if status_column else ""

        if not metric:
            continue

        if metric.lower() == "deal stacking template":
            continue

        if value == "" and status == "":
            continue

        summary_rows.append(
            {
                "Metric": metric,
                "Value": value,
                "Status": status,
            }
        )

    return pd.DataFrame(summary_rows)


def build_workbook_download(edited_sheets):
    buffer = BytesIO()

    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for sheet_name, sheet_df in edited_sheets.items():
            export_df = sheet_df.drop(columns=["Row"], errors="ignore")
            export_df.to_excel(writer, sheet_name=sheet_name, index=False, header=False)

    buffer.seek(0)
    return buffer.getvalue()


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
    price = soup.find(string=re.compile(r"£[\d,]+"))

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
# DEAL ENGINE
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
# SESSION STATE
# ============================
if "analysis_done" not in st.session_state:
    st.session_state.analysis_done = False


# ============================
# CALCULATOR PAGE
# ============================
def render_calculator_page():
    st.subheader("Deal Calculator")
    st.caption(
        "The BRR and Flip workbook is bundled into the app so users can review and tweak the template in-browser."
    )

    template_bytes, sheets, template_source = load_calculator_template()

    if not sheets:
        uploaded_template = st.file_uploader(
            "Upload your deal stacking workbook",
            type=["xlsx"],
            help="Use this if the template is not yet bundled into the GitHub repo.",
        )
        if uploaded_template is not None:
            try:
                template_bytes, sheets = load_uploaded_calculator_template(uploaded_template)
                template_source = "uploaded file"
                st.success("Workbook loaded from upload.")
            except Exception as exc:
                st.error(f"That workbook could not be read: {exc}")
                return
        else:
            st.error(f"Could not find `{DEAL_TEMPLATE_FILE}` in the deployed app.")
            st.info(
                "Add the `.xlsx` file to the same GitHub repo as `app.py`, or upload the workbook here manually."
            )
            return
    else:
        st.success(f"Workbook loaded from {template_source}.")

    st.info("Most user inputs in this template live in column B. The right-hand blocks are reference calculators.")

    edited_sheets = {}
    tabs = st.tabs(list(sheets.keys()))

    for tab, sheet_name in zip(tabs, sheets.keys()):
        with tab:
            sheet_df = sheets[sheet_name].copy()

            left_col, right_col = st.columns([3, 2])

            with left_col:
                edited_df = st.data_editor(
                    sheet_df,
                    use_container_width=True,
                    hide_index=True,
                    key=f"sheet_editor_{sheet_name}",
                )
                edited_sheets[sheet_name] = edited_df
                summary_df = extract_sheet_summary(edited_df)

            with right_col:
                st.markdown(f"**{sheet_name} key rows**")
                if summary_df.empty:
                    st.write("No summary rows were found in this sheet.")
                else:
                    st.dataframe(summary_df, use_container_width=True, hide_index=True)

                    numeric_values = pd.to_numeric(summary_df["Value"], errors="coerce").dropna()
                    if not numeric_values.empty:
                        st.metric("Visible numeric total", f"GBP {numeric_values.sum():,.0f}")

            st.download_button(
                f"Download {sheet_name} as CSV",
                data=edited_df.to_csv(index=False).encode("utf-8"),
                file_name=f"{sheet_name.lower()}-calculator.csv",
                mime="text/csv",
                key=f"download_{sheet_name}_csv",
            )

    if edited_sheets:
        workbook_bytes = build_workbook_download(edited_sheets)
        st.download_button(
            "Download edited workbook",
            data=workbook_bytes,
            file_name="deal-calculator-edited.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    if template_bytes:
        st.download_button(
            "Download original template",
            data=template_bytes,
            file_name=DEAL_TEMPLATE_FILE,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
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
                st.metric("Profit", f"£{result['profit']:,}")

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
                    £{int(row['price']):,}  
                    [View Property]({link})
                    ---
                    """
                )
        else:
            st.write("No comparables found for this property yet.")

        st.subheader("Headline Numbers")
        col1, col2, col3 = st.columns(3)
        col1.metric("GDV", f"£{result['gdv']:,}")
        col2.metric("Profit", f"£{result['profit']:,}")
        col3.metric("ROI", f"{result['roi']}%")

elif page == "Deal Calculator":
    render_calculator_page()

else:
    st.subheader(page)
    st.info("This section is ready for the next feature once you want to expand the app further.")
