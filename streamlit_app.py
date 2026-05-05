import os
from pathlib import Path
import re
from html import escape
from datetime import date
import hashlib
import json
import sqlite3

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
APP_DB_FILE = "property_os.db"


# ============================
# NAVIGATION
# ============================
page = st.sidebar.selectbox(
    "Navigation",
    [
        "Analyse Deal",
        "Property Maintenance",
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


def bridge_basis_value(inputs):
    if inputs.get("bridge_valuation_basis") == "Current market value":
        return float(inputs.get("current_market_value", 0) or 0)
    return float(inputs.get("purchase_price", 0) or 0)


def get_db_path():
    return Path(__file__).resolve().parent / APP_DB_FILE


def get_db_connection():
    connection = sqlite3.connect(get_db_path())
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database():
    connection = get_db_connection()
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                name TEXT,
                auth_source TEXT,
                password_hash TEXT,
                password_salt TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS properties (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_email TEXT NOT NULL,
                property_title TEXT NOT NULL,
                postcode TEXT,
                street TEXT,
                notes TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS saved_deals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_email TEXT NOT NULL,
                property_title TEXT NOT NULL,
                postcode TEXT,
                project_type TEXT,
                purchase_price REAL,
                gdv REAL,
                profit REAL,
                roi REAL,
                refurb_total REAL,
                payload_json TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS maintenance_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_email TEXT NOT NULL,
                property_title TEXT NOT NULL,
                postcode TEXT,
                move_in_date TEXT,
                schedule_json TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        existing_columns = {
            row["name"]
            for row in cursor.execute("PRAGMA table_info(users)").fetchall()
        }
        if "password_hash" not in existing_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
        if "password_salt" not in existing_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN password_salt TEXT")
        connection.commit()
    finally:
        connection.close()


def auth_configured():
    try:
        return "auth" in st.secrets
    except Exception:
        return False


def streamlit_user_value(attribute_name):
    try:
        if hasattr(st.user, attribute_name):
            return getattr(st.user, attribute_name)
    except Exception:
        pass

    try:
        return st.user.get(attribute_name)
    except Exception:
        return None


def get_current_user():
    try:
        if getattr(st.user, "is_logged_in", False):
            email = streamlit_user_value("email")
            name = streamlit_user_value("name") or email
            return {
                "email": email,
                "name": name,
                "auth_source": "oidc",
            }
    except Exception:
        pass

    local_email = st.session_state.get("authenticated_user_email", "").strip().lower()
    local_name = st.session_state.get("authenticated_user_name", "").strip()
    if local_email:
        return {
            "email": local_email,
            "name": local_name or local_email,
            "auth_source": "local-password",
        }

    return None


def hash_password(password, salt_hex=None):
    salt_bytes = bytes.fromhex(salt_hex) if salt_hex else os.urandom(16)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt_bytes,
        200000,
    ).hex()
    return password_hash, salt_bytes.hex()


def fetch_user_by_email(email):
    connection = get_db_connection()
    try:
        row = connection.execute(
            "SELECT * FROM users WHERE email = ?",
            (email.strip().lower(),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def create_local_user_account(name, email, password):
    email = email.strip().lower()
    name = name.strip()
    if not email or not password:
        return False, "Email and password are required."
    if len(password) < 8:
        return False, "Use at least 8 characters for the password."
    if fetch_user_by_email(email):
        return False, "An account with that email already exists."

    password_hash, password_salt = hash_password(password)
    connection = get_db_connection()
    try:
        connection.execute(
            """
            INSERT INTO users (email, name, auth_source, password_hash, password_salt)
            VALUES (?, ?, ?, ?, ?)
            """,
            (email, name or email, "local-password", password_hash, password_salt),
        )
        connection.commit()
    finally:
        connection.close()
    return True, "Account created. You can now sign in."


def verify_local_user_credentials(email, password):
    email = email.strip().lower()
    user = fetch_user_by_email(email)
    if not user:
        return None
    if not user.get("password_hash") or not user.get("password_salt"):
        return None

    candidate_hash, _ = hash_password(password, user["password_salt"])
    if candidate_hash != user["password_hash"]:
        return None
    return user


def upsert_user(user):
    if not user or not user.get("email"):
        return

    connection = get_db_connection()
    try:
        connection.execute(
            """
            INSERT INTO users (email, name, auth_source)
            VALUES (?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                name = excluded.name,
                auth_source = excluded.auth_source,
                updated_at = CURRENT_TIMESTAMP
            """,
            (user["email"], user.get("name"), user.get("auth_source")),
        )
        connection.commit()
    finally:
        connection.close()


def save_property_record(user, property_title, postcode="", street="", notes=""):
    if not user or not property_title:
        return

    connection = get_db_connection()
    try:
        connection.execute(
            """
            INSERT INTO properties (user_email, property_title, postcode, street, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user["email"], property_title, postcode, street, notes),
        )
        connection.commit()
    finally:
        connection.close()


def save_deal_record(user, property_title, postcode, project_type, purchase_price, gdv, profit, roi, refurb_total, payload):
    if not user or not property_title:
        return

    connection = get_db_connection()
    try:
        connection.execute(
            """
            INSERT INTO saved_deals (
                user_email, property_title, postcode, project_type,
                purchase_price, gdv, profit, roi, refurb_total, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user["email"],
                property_title,
                postcode,
                project_type,
                purchase_price,
                gdv,
                profit,
                roi,
                refurb_total,
                json.dumps(payload),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def save_maintenance_plan(user, property_title, postcode, move_in_date, schedule_df):
    if not user or not property_title:
        return

    connection = get_db_connection()
    try:
        connection.execute(
            """
            INSERT INTO maintenance_plans (user_email, property_title, postcode, move_in_date, schedule_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                user["email"],
                property_title,
                postcode,
                str(move_in_date),
                schedule_df.to_json(orient="records", date_format="iso"),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def load_saved_deals(user, limit=12):
    if not user:
        return pd.DataFrame()

    connection = get_db_connection()
    try:
        return pd.read_sql_query(
            """
            SELECT property_title, postcode, project_type, purchase_price, gdv, profit, roi, refurb_total, created_at
            FROM saved_deals
            WHERE user_email = ?
            ORDER BY datetime(created_at) DESC
            LIMIT ?
            """,
            connection,
            params=(user["email"], limit),
        )
    finally:
        connection.close()


def load_saved_properties(user, limit=12):
    if not user:
        return pd.DataFrame()

    connection = get_db_connection()
    try:
        return pd.read_sql_query(
            """
            SELECT property_title, postcode, street, notes, created_at
            FROM properties
            WHERE user_email = ?
            ORDER BY datetime(created_at) DESC
            LIMIT ?
            """,
            connection,
            params=(user["email"], limit),
        )
    finally:
        connection.close()


def render_auth_sidebar():
    with st.sidebar:
        st.markdown("**Account**")
        user = get_current_user()

        if user:
            st.success("Signed in")
            st.caption(f"Signed in as {user['email']}")
            if user.get("auth_source") == "oidc":
                if st.button("Log out", use_container_width=True, key="logout_button"):
                    st.logout()
            else:
                if st.button("Sign out local profile", use_container_width=True, key="logout_local_button"):
                    st.session_state.pop("authenticated_user_email", None)
                    st.session_state.pop("authenticated_user_name", None)
                    st.rerun()
            return user

        if auth_configured():
            st.info("Sign in to load your saved properties, deals, and maintenance plans.")
            if st.button("Sign in with Google/Microsoft", use_container_width=True, key="login_button"):
                st.login()
            return None

        st.warning("Using local proof-of-concept login for now. Your account is stored in the app database.")
        login_tab, create_tab = st.tabs(["Log In", "Create Account"])

        with login_tab:
            with st.form("local_login_form"):
                login_email = st.text_input("Email", key="login_email", placeholder="you@example.com")
                login_password = st.text_input("Password", type="password", key="login_password")
                login_submitted = st.form_submit_button("Log in", use_container_width=True)
            if login_submitted:
                matched_user = verify_local_user_credentials(login_email, login_password)
                if matched_user:
                    st.session_state.authenticated_user_email = matched_user["email"]
                    st.session_state.authenticated_user_name = matched_user.get("name") or matched_user["email"]
                    st.rerun()
                else:
                    st.error("Email or password was incorrect.")

        with create_tab:
            with st.form("local_create_account_form"):
                create_name = st.text_input("Name", key="create_name", placeholder="Your name")
                create_email = st.text_input("Email", key="create_email", placeholder="you@example.com")
                create_password = st.text_input("Password", type="password", key="create_password")
                create_submitted = st.form_submit_button("Create account", use_container_width=True)
            if create_submitted:
                created, message = create_local_user_account(create_name, create_email, create_password)
                if created:
                    st.success(message)
                else:
                    st.error(message)

        st.caption("Later, we can swap this to Google login without changing the rest of your saved data features.")
        return get_current_user()


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


def format_breakdown_value(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        kind = value.get("kind", "money")
        raw_value = value.get("value", 0)
        if kind == "percent":
            return format_percent(raw_value)
        return format_money(raw_value)
    return format_money(value)


def render_breakdown_table(title, items):
    table = pd.DataFrame(
        {
            "Line Item": list(items.keys()),
            "Amount": [format_breakdown_value(v) for v in items.values()],
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
        .deal-hero {
            padding: 1.4rem 1.6rem;
            border-radius: 24px;
            background: linear-gradient(135deg, #f4ede1 0%, #d8e5d5 55%, #c8d9d0 100%);
            border: 1px solid #d1c1a8;
            margin-bottom: 1rem;
        }
        .deal-hero h3 {
            margin: 0 0 0.35rem 0;
            color: #1f2d24;
        }
        .deal-hero p {
            margin: 0;
            color: #455247;
        }
        .deal-card {
            padding: 1rem 1.1rem;
            border-radius: 18px;
            background: #fbf8f2;
            border: 1px solid #e4dbc9;
            min-height: 100%;
        }
        .deal-card h4 {
            margin: 0 0 0.55rem 0;
            color: #243229;
        }
        .deal-card p {
            margin: 0.3rem 0;
            color: #50574f;
        }
        .dashboard-stat {
            padding: 1rem 1.05rem;
            border-radius: 18px;
            background: linear-gradient(180deg, #fffdf8 0%, #f4efe5 100%);
            border: 1px solid #dfd5c3;
            box-shadow: 0 8px 24px rgba(55, 47, 31, 0.06);
            min-height: 126px;
        }
        .dashboard-stat-label {
            font-size: 0.82rem;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: #6b7068;
            margin-bottom: 0.55rem;
        }
        .dashboard-stat-value {
            font-size: 1.55rem;
            line-height: 1.1;
            color: #1e2c24;
            font-weight: 700;
        }
        .dashboard-stat-note {
            margin-top: 0.6rem;
            color: #5f685f;
            font-size: 0.92rem;
        }
        .deal-chip-row {
            display: flex;
            flex-wrap: wrap;
            gap: 0.5rem;
            margin-top: 0.75rem;
        }
        .deal-chip {
            padding: 0.4rem 0.7rem;
            border-radius: 999px;
            background: #e7efe6;
            border: 1px solid #c6d7c4;
            color: #29402f;
            font-size: 0.92rem;
        }
        .dashboard-badge-positive {
            background: #e3efe0;
            border-color: #b8cfb6;
            color: #1f4d2a;
        }
        .dashboard-badge-watch {
            background: #f2eadb;
            border-color: #d9c8a6;
            color: #6c4f18;
        }
        .dashboard-comps-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 0.9rem;
            margin-top: 0.75rem;
        }
        .comp-card {
            border-radius: 20px;
            overflow: hidden;
            border: 1px solid #ddd1bc;
            background: #fffaf2;
            box-shadow: 0 10px 26px rgba(64, 49, 28, 0.08);
        }
        .comp-thumb {
            min-height: 118px;
            padding: 0.9rem;
            display: flex;
            align-items: flex-end;
            color: #fffdf7;
            font-weight: 700;
            font-size: 1.05rem;
            letter-spacing: 0.02em;
        }
        .comp-body {
            padding: 0.95rem 1rem 1rem 1rem;
        }
        .comp-meta {
            color: #5b625b;
            font-size: 0.92rem;
            margin: 0.22rem 0;
        }
        .comp-price {
            color: #213126;
            font-weight: 700;
            font-size: 1.15rem;
            margin: 0.35rem 0 0.55rem 0;
        }
        .comp-link {
            display: inline-block;
            margin-top: 0.4rem;
            padding: 0.48rem 0.78rem;
            border-radius: 999px;
            text-decoration: none;
            background: #213126;
            color: #f8f4ea !important;
            font-size: 0.92rem;
        }
        .investor-preview {
            padding: 1.15rem 1.2rem;
            border-radius: 22px;
            background: linear-gradient(140deg, #f7f1e5 0%, #eef3eb 100%);
            border: 1px solid #d9ccb5;
            margin-top: 0.8rem;
        }
        .investor-preview h3 {
            margin: 0 0 0.35rem 0;
            color: #213126;
        }
        .investor-preview p {
            color: #505a52;
            margin: 0.25rem 0 0.45rem 0;
        }
        .investor-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 0.85rem;
            margin-top: 0.8rem;
        }
        .investor-panel {
            background: rgba(255, 250, 242, 0.82);
            border: 1px solid #e0d6c6;
            border-radius: 18px;
            padding: 0.95rem 1rem;
        }
        .investor-panel h4 {
            margin: 0 0 0.45rem 0;
            color: #243229;
        }
        .investor-panel p {
            margin: 0.3rem 0;
            color: #50574f;
        }
        .email-preview {
            padding: 1rem 1.1rem;
            border-radius: 18px;
            background: #fffaf2;
            border: 1px solid #ddd2c0;
            box-shadow: inset 0 1px 0 rgba(255,255,255,0.8);
            white-space: pre-wrap;
            color: #313732;
            line-height: 1.6;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def visual_gradient_from_text(value):
    gradients = [
        "linear-gradient(135deg, #345c4a 0%, #7ea27c 100%)",
        "linear-gradient(135deg, #5f4b32 0%, #b48a57 100%)",
        "linear-gradient(135deg, #35586d 0%, #7fa0b2 100%)",
        "linear-gradient(135deg, #6b5a4b 0%, #a89a7d 100%)",
        "linear-gradient(135deg, #3d4e5a 0%, #8aa0ad 100%)",
    ]
    index = sum(ord(character) for character in str(value or "")) % len(gradients)
    return gradients[index]


def build_dashboard_stat_html(label, value, note, badge_class):
    return f"""
    <div class="dashboard-stat">
        <div class="dashboard-stat-label">{escape(str(label))}</div>
        <div class="dashboard-stat-value">{escape(str(value))}</div>
        <div class="deal-chip {badge_class}">{escape(str(note))}</div>
    </div>
    """


def build_comp_card_html(row):
    price_text = format_money(row["price"]) if pd.notna(row["price"]) else "N/A"
    postcode_text = normalize_postcode(row.get("postcode", ""))
    street_text = row.get("street", "Comparable")
    link = zoopla_link(row.get("street", ""), row.get("postcode", ""))
    thumb_style = visual_gradient_from_text(street_text)
    return f"""
    <div class="comp-card">
        <div class="comp-thumb" style="background: {thumb_style};">{escape(str(street_text))}</div>
        <div class="comp-body">
            <div class="comp-meta">{escape(postcode_text)}</div>
            <div class="comp-price">{escape(price_text)}</div>
            <div class="comp-meta">Previously sold comparable in the local search set.</div>
            <a class="comp-link" href="{escape(link)}" target="_blank">Open comp search</a>
        </div>
    </div>
    """


def add_task_row(rows, task_name, category, due_date, reminder_days, notes):
    due_timestamp = pd.Timestamp(due_date).normalize()
    reference_date = pd.Timestamp(date.today()).normalize()
    reminder_date = due_timestamp - pd.Timedelta(days=reminder_days)
    days_until_due = int((due_timestamp - reference_date).days)

    if days_until_due < 0:
        status = "Overdue"
    elif reminder_date <= reference_date:
        status = "Due Soon"
    else:
        status = "Upcoming"

    rows.append(
        {
            "Task": task_name,
            "Category": category,
            "Due Date": due_timestamp.date(),
            "Reminder Date": reminder_date.date(),
            "Days Until Due": days_until_due,
            "Status": status,
            "Notes": notes,
        }
    )


def build_maintenance_schedule(
    tenant_move_in,
    gas_safety_due,
    eicr_due,
    boiler_service_due,
    gutter_clean_due,
    custom_task_name="",
    custom_task_due=None,
    custom_task_frequency_months=12,
):
    move_in = pd.Timestamp(tenant_move_in).normalize()
    rows = []

    add_task_row(
        rows,
        "Tenant settling-in call",
        "Tenancy",
        move_in + pd.Timedelta(days=14),
        3,
        "Quick welfare check to make sure the tenant is settled and any snags are caught early.",
    )
    add_task_row(
        rows,
        "First property visit",
        "Inspection",
        move_in + pd.Timedelta(days=60),
        10,
        "Site visit to make sure the property is being lived in well and no maintenance issues are building up.",
    )

    for months_ahead in (3, 6, 9, 12):
        add_task_row(
            rows,
            f"Routine property visit ({months_ahead} months)",
            "Inspection",
            move_in + pd.DateOffset(months=months_ahead),
            14,
            "Regular landlord visit and condition check.",
        )

    add_task_row(
        rows,
        "Tenancy renewal review",
        "Tenancy",
        move_in + pd.DateOffset(months=11),
        30,
        "Review rent, renewal options, and any works to plan before the tenancy anniversary.",
    )
    add_task_row(
        rows,
        "Gas safety certificate",
        "Compliance",
        gas_safety_due,
        30,
        "Book the gas engineer and make sure the certificate is renewed before expiry.",
    )
    add_task_row(
        rows,
        "EICR / electrical certification",
        "Compliance",
        eicr_due,
        45,
        "Electrical inspection reminder ahead of the current certificate due date.",
    )
    add_task_row(
        rows,
        "Boiler service",
        "Maintenance",
        boiler_service_due,
        30,
        "Annual boiler service to keep heating reliable and reduce emergency callouts.",
    )
    add_task_row(
        rows,
        "Gutter cleaning",
        "Maintenance",
        gutter_clean_due,
        21,
        "Prevent damp and overflow issues by clearing gutters before the due date.",
    )

    if str(custom_task_name).strip() and custom_task_due:
        add_task_row(
            rows,
            str(custom_task_name).strip(),
            "Custom",
            custom_task_due,
            14,
            f"Custom task with an assumed repeat cycle of every {custom_task_frequency_months} months.",
        )

    schedule = pd.DataFrame(rows).sort_values(["Due Date", "Task"]).reset_index(drop=True)
    return schedule


def render_deal_dashboard(data, result, refurb, comps, current_condition, target_condition):
    render_calculator_styles()

    property_name = data.get("name", "Selected property")
    refurb_total = refurb.get("total", 0)
    refurb_base = refurb.get("base", 0)
    refurb_adjusted = refurb.get("adjusted", 0)
    contingency = refurb.get("contingency", 0)
    top_comp_price = None
    if comps is not None and len(comps) > 0:
        top_comp_price = comps["price"].max()

    st.markdown(
        f"""
        <div class="deal-hero">
            <h3>Deal Dashboard</h3>
            <p>{property_name}</p>
            <div class="deal-chip-row">
                <span class="deal-chip">Current: {current_condition}</span>
                <span class="deal-chip">Target: {target_condition}</span>
                <span class="deal-chip">Refurb Total: {format_money(refurb_total)}</span>
                <span class="deal-chip">GDV: {format_money(result.get("gdv", 0))}</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    stat_col1, stat_col2, stat_col3, stat_col4 = st.columns(4)
    with stat_col1:
        st.markdown(
            build_dashboard_stat_html("GDV", format_money(result.get("gdv", 0)), "Projected end value", "dashboard-badge-watch"),
            unsafe_allow_html=True,
        )
    with stat_col2:
        st.markdown(
            build_dashboard_stat_html(
                "Profit",
                format_money(result.get("profit", 0)),
                "Margin worth chasing" if result.get("profit", 0) > 0 else "Needs a second look",
                "dashboard-badge-positive" if result.get("profit", 0) > 0 else "dashboard-badge-watch",
            ),
            unsafe_allow_html=True,
        )
    with stat_col3:
        st.markdown(
            build_dashboard_stat_html(
                "ROI",
                f"{result.get('roi', 0)}%",
                "Healthy uplift" if result.get("roi", 0) >= 15 else "Watch the numbers",
                "dashboard-badge-positive" if result.get("roi", 0) >= 15 else "dashboard-badge-watch",
            ),
            unsafe_allow_html=True,
        )
    with stat_col4:
        st.markdown(
            build_dashboard_stat_html(
                "Best Nearby Comp",
                format_money(top_comp_price) if top_comp_price else "N/A",
                "Local benchmark",
                "dashboard-badge-watch",
            ),
            unsafe_allow_html=True,
        )

    info_col1, info_col2 = st.columns([1.05, 0.95])

    with info_col1:
        st.markdown(
            f"""
            <div class="deal-card">
                <h4>Property Snapshot</h4>
                <p><strong>Property:</strong> {property_name}</p>
                <p><strong>Current condition:</strong> {current_condition}</p>
                <p><strong>Target finish:</strong> {target_condition}</p>
                <p><strong>Forecast profit:</strong> {format_money(result.get('profit', 0))}</p>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with info_col2:
        st.markdown(
            f"""
            <div class="deal-card">
                <h4>Refurb Snapshot</h4>
                <p><strong>Base works cost:</strong> {format_money(refurb_base)}</p>
                <p><strong>Adjusted works cost:</strong> {format_money(refurb_adjusted)}</p>
                <p><strong>Contingency:</strong> {format_money(contingency)}</p>
                <p><strong>Total refurb budget:</strong> {format_money(refurb_total)}</p>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.markdown("**Nearby Comparables**")
    if comps is not None and len(comps) > 0:
        comp_cards_html = "".join(build_comp_card_html(row) for _, row in comps.head(6).iterrows())
        st.markdown(
            f'<div class="dashboard-comps-grid">{comp_cards_html}</div>',
            unsafe_allow_html=True,
        )
    else:
        st.info("No comparables found for this property yet.")


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
    bridge_ltv = percent_to_decimal(inputs["bridge_ltv_pct"])
    bridge_rate = percent_to_decimal(inputs["bridge_monthly_rate"])
    refi_ltv = percent_to_decimal(inputs["refi_ltv"])
    mortgage_rate = percent_to_decimal(inputs["mortgage_interest_rate"])
    current_market_value = inputs["current_market_value"]
    bridge_security_value = bridge_basis_value(inputs)

    sdlt = calculate_template_sdlt(purchase_price, inputs["property_type"])
    purchase_costs = sdlt + inputs["auction_fees"] + inputs["valuation_fees"] + inputs["purchase_legal_fees"]
    development_costs = inputs["refurb_cost"] + inputs["planning_cost"] + inputs["holding_cost"]

    gross_bridge = bridge_security_value * bridge_ltv
    bridge_arrangement_fee = gross_bridge * percent_to_decimal(inputs["arrangement_fee_pct"])
    bridge_broker_fee = gross_bridge * percent_to_decimal(inputs["bridge_broker_fee_pct"])
    bridging_interest = gross_bridge * bridge_rate * inputs["bridge_months"]

    retained_fees = bridge_arrangement_fee + bridge_broker_fee + bridging_interest if inputs["retain_fees"] else 0
    advance_received = gross_bridge - retained_fees if inputs["retain_fees"] else gross_bridge

    refinance_proceeds = inputs["gdv"] * refi_ltv
    annual_rent = inputs["monthly_rent"] * 12
    mortgage_arrangement_fee = refinance_proceeds * percent_to_decimal(inputs["mortgage_arrangement_fee_pct"])
    mortgage_balance_for_interest = refinance_proceeds + mortgage_arrangement_fee
    annual_mortgage_cost = mortgage_balance_for_interest * mortgage_rate

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
        "gdv": inputs["gdv"],
        "current_market_value": current_market_value,
        "purchase_costs": purchase_costs,
        "development_costs": development_costs,
        "bridge_security_value": bridge_security_value,
        "bridge_ltv_pct": inputs["bridge_ltv_pct"],
        "bridge_day_one_pct_of_purchase": safe_percent(gross_bridge, purchase_price),
        "gross_bridge": gross_bridge,
        "net_bridge": advance_received,
        "bridging_interest": bridging_interest,
        "bridge_arrangement_fee": bridge_arrangement_fee,
        "bridge_broker_fee": bridge_broker_fee,
        "mortgage_arrangement_fee": mortgage_arrangement_fee,
        "mortgage_balance_for_interest": mortgage_balance_for_interest,
        "project_costs": project_costs,
        "cash_required": cash_required,
        "cash_required_before_funding": project_costs,
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
    bridge_ltv = percent_to_decimal(inputs["bridge_ltv_pct"])
    bridge_rate = percent_to_decimal(inputs["bridge_monthly_rate"])
    current_market_value = inputs["current_market_value"]
    bridge_security_value = bridge_basis_value(inputs)

    sdlt = calculate_template_sdlt(purchase_price, inputs["property_type"])
    purchase_costs = sdlt + inputs["auction_fees"] + inputs["valuation_fees"] + inputs["purchase_legal_fees"]
    development_costs = inputs["refurb_cost"] + inputs["planning_cost"] + inputs["holding_cost"]

    gross_bridge = bridge_security_value * bridge_ltv
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
    cash_required = project_costs - advance_received
    profit = net_sale_proceeds - project_costs

    return {
        "sdlt": sdlt,
        "sale_price": inputs["sale_price"],
        "current_market_value": current_market_value,
        "purchase_costs": purchase_costs,
        "development_costs": development_costs,
        "bridge_security_value": bridge_security_value,
        "bridge_ltv_pct": inputs["bridge_ltv_pct"],
        "bridge_day_one_pct_of_purchase": safe_percent(gross_bridge, purchase_price),
        "gross_bridge": gross_bridge,
        "net_bridge": advance_received,
        "bridging_interest": bridging_interest,
        "bridge_arrangement_fee": bridge_arrangement_fee,
        "bridge_broker_fee": bridge_broker_fee,
        "agent_fees": agent_fees,
        "project_costs": project_costs,
        "total_costs_with_sale": total_costs_with_sale,
        "cash_required": cash_required,
        "cash_required_before_funding": project_costs,
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
        current_market_value = st.number_input(
            "Current market value",
            min_value=0,
            value=110000,
            step=5000,
            key="brr_current_market_value",
            help="Use this when the day-one valuation is different from the agreed purchase price.",
        )

        st.markdown("**Bridge & Exit**")
        bridge_valuation_basis = st.radio(
            "Bridge leverage based on",
            ["Purchase price", "Current market value"],
            horizontal=True,
            key="brr_bridge_valuation_basis",
        )
        bridge_ltv_pct = st.slider(
            "Bridge loan to value %",
            min_value=0,
            max_value=100,
            value=75,
            step=1,
            key="brr_bridge_ltv_pct",
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
        mortgage_arrangement_fee_pct = st.slider(
            "Mortgage arrangement fee %",
            min_value=0.0,
            max_value=5.0,
            value=2.0,
            step=0.1,
            key="brr_mortgage_arrangement_fee_pct",
            help="This is added onto the mortgage balance for the annual interest calculation.",
        )
        st.caption(
            f"Bridge sizing is currently based on {bridge_valuation_basis.lower()} at "
            f"{format_percent(bridge_ltv_pct)}."
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
            "current_market_value": current_market_value,
            "gdv": gdv,
            "bridge_valuation_basis": bridge_valuation_basis,
            "bridge_ltv_pct": bridge_ltv_pct,
            "bridge_monthly_rate": bridge_monthly_rate,
            "bridge_months": bridge_months,
            "retain_fees": retain_fees,
            "refi_ltv": refi_ltv,
            "mortgage_interest_rate": mortgage_interest_rate,
            "arrangement_fee_pct": arrangement_fee_pct,
            "mortgage_arrangement_fee_pct": mortgage_arrangement_fee_pct,
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
                "Bridge basis value": brr_outputs["bridge_security_value"],
                "Bridge day-one leverage vs purchase": {"kind": "percent", "value": brr_outputs["bridge_day_one_pct_of_purchase"]},
                "Gross bridge": brr_outputs["gross_bridge"],
                "Net bridge received": brr_outputs["net_bridge"],
                "Bridge interest": brr_outputs["bridging_interest"],
                "Arrangement fee": brr_outputs["bridge_arrangement_fee"],
                "Bridge broker fee": brr_outputs["bridge_broker_fee"],
            },
        )
        render_breakdown_table(
            "Purchase Cost Breakdown",
            {
                "Stamp duty": brr_outputs["sdlt"],
                "Auction fees": auction_fees,
                "Valuation fees": valuation_fees,
                "Purchase legals": purchase_legal_fees,
                "Purchase costs": brr_outputs["purchase_costs"],
            },
        )
        render_breakdown_table(
            "Refinance Cost Breakdown",
            {
                "Refi legals": refi_legal_fees,
                "Refi broker fees": refi_broker_fees,
                "Mortgage arrangement fee": brr_outputs["mortgage_arrangement_fee"],
                "Mortgage balance for interest": brr_outputs["mortgage_balance_for_interest"],
                "Annual mortgage cost": brr_outputs["annual_mortgage_cost"],
            },
        )
        render_breakdown_table(
            "Cash Required Breakdown",
            {
                "Purchase price": purchase_price,
                "Purchase costs": brr_outputs["purchase_costs"],
                "Development costs": brr_outputs["development_costs"],
                "Bridge arrangement fee": brr_outputs["bridge_arrangement_fee"],
                "Bridge broker fee": brr_outputs["bridge_broker_fee"],
                "Bridge interest": brr_outputs["bridging_interest"],
                "Refi legals": refi_legal_fees,
                "Refi broker fees": refi_broker_fees,
                "Total project cash in": brr_outputs["cash_required_before_funding"],
                "Less net bridge received": -brr_outputs["net_bridge"],
                "Total cash required": brr_outputs["cash_required"],
                "Annual rent": brr_outputs["annual_rent"],
            },
        )

    st.caption(
        "SDLT is included inside purchase costs and is now shown line by line so the total cash requirement is easier to follow."
    )
    return brr_outputs, {
        "project_type": "BRR",
        "property_address": property_address,
        "property_reference": property_reference,
        "purchase_price": purchase_price,
        "current_market_value": current_market_value,
        "gdv": gdv,
        "refurb_cost": refurb_cost,
        "holding_cost": holding_cost,
        "planning_cost": planning_cost,
        "bridge_months": bridge_months,
        "bridge_valuation_basis": bridge_valuation_basis,
        "bridge_ltv_pct": bridge_ltv_pct,
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
        current_market_value = st.number_input(
            "Current market value",
            min_value=0,
            value=125000,
            step=5000,
            key="flip_current_market_value",
            help="Use this when the bridge lender is underwriting the day-one market value rather than the purchase price.",
        )

        st.markdown("**Bridge**")
        bridge_valuation_basis = st.radio(
            "Bridge leverage based on",
            ["Purchase price", "Current market value"],
            horizontal=True,
            key="flip_bridge_valuation_basis",
        )
        bridge_ltv_pct = st.slider(
            "Bridge loan to value %",
            min_value=0,
            max_value=100,
            value=75,
            step=1,
            key="flip_bridge_ltv_pct",
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
        st.caption(
            f"Bridge sizing is currently based on {bridge_valuation_basis.lower()} at "
            f"{format_percent(bridge_ltv_pct)}."
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
            "current_market_value": current_market_value,
            "sale_price": sale_price,
            "bridge_valuation_basis": bridge_valuation_basis,
            "bridge_ltv_pct": bridge_ltv_pct,
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
                "Bridge basis value": flip_outputs["bridge_security_value"],
                "Bridge day-one leverage vs purchase": {"kind": "percent", "value": flip_outputs["bridge_day_one_pct_of_purchase"]},
                "Gross bridge": flip_outputs["gross_bridge"],
                "Net bridge received": flip_outputs["net_bridge"],
                "Bridge interest": flip_outputs["bridging_interest"],
                "Arrangement fee": flip_outputs["bridge_arrangement_fee"],
                "Bridge broker fee": flip_outputs["bridge_broker_fee"],
            },
        )
        render_breakdown_table(
            "Purchase Cost Breakdown",
            {
                "Stamp duty": flip_outputs["sdlt"],
                "Auction fees": auction_fees,
                "Valuation fees": valuation_fees,
                "Purchase legals": purchase_legal_fees,
                "Purchase costs": flip_outputs["purchase_costs"],
            },
        )
        render_breakdown_table(
            "Cash Required Breakdown",
            {
                "Purchase price": purchase_price,
                "Purchase costs": flip_outputs["purchase_costs"],
                "Development costs": flip_outputs["development_costs"],
                "Bridge arrangement fee": flip_outputs["bridge_arrangement_fee"],
                "Bridge broker fee": flip_outputs["bridge_broker_fee"],
                "Bridge interest": flip_outputs["bridging_interest"],
                "Total project cash in": flip_outputs["cash_required_before_funding"],
                "Less net bridge received": -flip_outputs["net_bridge"],
                "Total cash required": flip_outputs["cash_required"],
            },
        )
        render_breakdown_table(
            "Exit Breakdown",
            {
                "Agent fees": flip_outputs["agent_fees"],
                "Sale legals": sale_legal_fees,
                "Total costs incl. sale": flip_outputs["total_costs_with_sale"],
                "Net sale proceeds": flip_outputs["net_sale_proceeds"],
            },
        )

    st.caption(
        "The cash required figure now shows the upfront capital needed before sale, while the exit breakdown keeps sale fees and proceeds separate."
    )
    return flip_outputs, {
        "project_type": "Flip",
        "property_address": property_address,
        "property_reference": property_reference,
        "purchase_price": purchase_price,
        "current_market_value": current_market_value,
        "sale_price": sale_price,
        "refurb_cost": refurb_cost,
        "holding_cost": holding_cost,
        "planning_cost": planning_cost,
        "bridge_months": bridge_months,
        "bridge_valuation_basis": bridge_valuation_basis,
        "bridge_ltv_pct": bridge_ltv_pct,
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
        "brr_current_market_value": int(selected_price),
        "brr_gdv": int(gdv),
        "brr_refurb_cost": refurb_total,
        "flip_property_address": selected_name,
        "flip_property_reference": selected_postcode,
        "flip_purchase_price": int(selected_price),
        "flip_current_market_value": int(selected_price),
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
    current_market_value = project_details.get("current_market_value", purchase_price)
    refurb_cost = project_details.get("refurb_cost", 0)
    available_cash = investor_inputs.get("available_cash", 0)
    available_security = investor_inputs.get("available_security", 0)
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
- Current Market Value: {format_money(current_market_value)}
- Refurb Budget: {format_money(refurb_cost)}
- Headline Exit Value: {format_money(headline_value)}
- Forecast Profit / Equity Uplift: {format_money(headline_profit)}

## Capital Stack
- Total Cash Required: {format_money(project_outputs.get('cash_required', 0))}
- Operator Cash Going In: {format_money(available_cash)}
- Equity / Security Support: {format_money(available_security)}
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
- Bridge Basis Value: {format_money(project_outputs.get('bridge_security_value', 0))}
- Gross Bridge: {format_money(project_outputs.get('gross_bridge', 0))}
- Bridge Interest: {format_money(project_outputs.get('bridging_interest', 0))}
- Mortgage Arrangement Fee: {format_money(project_outputs.get('mortgage_arrangement_fee', 0))}

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
    available_security = investor_inputs.get("available_security", 0)
    headline_profit = project_outputs.get("equity_created")
    if headline_profit is None:
        headline_profit = project_outputs.get("profit", 0)

    summary_line = analysis_data.get("name") if analysis_data else property_name

    return f"""Subject: Investor opportunity - {summary_line}

Hi [Investor Name],

I hope you're well. I have a new {project_type.lower()} opportunity that I think could be a strong fit for you.

The deal is centred on {summary_line}. The purchase price is {format_money(project_details.get('purchase_price', 0))} with a current market value of {format_money(project_details.get('current_market_value', project_details.get('purchase_price', 0)))} and a refurb budget of {format_money(project_details.get('refurb_cost', 0))}. Based on the current underwriting, the projected upside is around {format_money(headline_profit)}.

I'm looking to raise {format_money(investor_required)} to complete the capital stack. I am putting in {format_money(investor_inputs.get('available_cash', 0))} personally and can also support the deal with {format_money(available_security)} in available equity / security, and I can share the full investor pack with the entry costs, works budget, finance assumptions, and exit numbers.

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

    if st.button("Save current project scenario", use_container_width=True, key="save_project_scenario"):
        save_deal_record(
            current_user,
            project_details.get("property_address") or "Saved project scenario",
            project_details.get("property_reference", ""),
            project_details.get("project_type", ""),
            project_details.get("purchase_price", 0),
            project_outputs.get("gdv", project_details.get("sale_price", 0)),
            project_outputs.get("equity_created", project_outputs.get("profit", 0)),
            project_outputs.get("cash_on_cash_roi", project_outputs.get("profit_margin", 0)),
            project_details.get("refurb_cost", 0),
            {"project_details": project_details, "project_outputs": project_outputs},
        )
        st.success("Project scenario saved to your portfolio.")

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
    st.caption("Track how much cash and security you already have, identify any funding gap, and generate investor-ready material when outside capital is needed.")

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
        available_security = st.number_input(
            "Available equity / security you can leverage",
            min_value=0,
            value=0,
            step=5000,
            key="available_security_support",
            help="Use this for equity you could put up as additional security, for example from another property or your own home.",
        )
        use_investor_funds = st.toggle(
            "Use investor funds for this project",
            value=cash_required > (available_cash + available_security),
            key="use_investor_funds",
        )

    investor_required = max(cash_required - available_cash - available_security, 0.0) if use_investor_funds else 0.0

    with investor_col:
        st.metric("Total cash required", format_money(cash_required))
        st.metric("Your cash", format_money(available_cash))
        st.metric("Security support", format_money(available_security))
        st.metric("Investor funds needed", format_money(investor_required))

    if not use_investor_funds or investor_required <= 0:
        st.success("This project is fully covered by your current cash and available security based on the current assumptions.")
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
        "available_security": available_security,
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
        headline_value = project_outputs.get("gdv") or project_details.get("sale_price") or project_outputs.get("net_sale_proceeds", 0)
        headline_profit = project_outputs.get("equity_created")
        if headline_profit is None:
            headline_profit = project_outputs.get("profit", 0)
        pack_preview_html = f"""
        <div class="investor-preview">
            <h3>Investor Pack Preview</h3>
            <p>{escape(project_details.get('property_address') or 'Selected property')} | {escape(project_details.get('project_type', 'Project'))}</p>
            <div class="investor-grid">
                <div class="investor-panel">
                    <h4>Raise Snapshot</h4>
                    <p><strong>Raise target:</strong> {escape(format_money(investor_required))}</p>
                    <p><strong>Your cash:</strong> {escape(format_money(available_cash))}</p>
                    <p><strong>Target return:</strong> {escape(format_percent(target_return_pct))}</p>
                    <p><strong>Profit share:</strong> {escape(format_percent(profit_share_pct))}</p>
                </div>
                <div class="investor-panel">
                    <h4>Deal Numbers</h4>
                    <p><strong>Purchase:</strong> {escape(format_money(project_details.get('purchase_price', 0)))}</p>
                    <p><strong>Refurb:</strong> {escape(format_money(project_details.get('refurb_cost', 0)))}</p>
                    <p><strong>Exit value:</strong> {escape(format_money(headline_value))}</p>
                    <p><strong>Forecast upside:</strong> {escape(format_money(headline_profit))}</p>
                </div>
                <div class="investor-panel">
                    <h4>Why It Lands</h4>
                    <p>One joined-up workflow covering entry, refurb, finance, and exit.</p>
                    <p>The capital raise is tied to a specific project funding gap.</p>
                    <p>Live numbers are already pressure-tested inside the project builder.</p>
                </div>
            </div>
        </div>
        """
        st.markdown(pack_preview_html, unsafe_allow_html=True)
        with st.expander("Open full investor pack text"):
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
        st.markdown(
            f'<div class="email-preview">{escape(email_text)}</div>',
            unsafe_allow_html=True,
        )
        st.download_button(
            "Download investor email",
            data=email_text.encode("utf-8"),
            file_name="investor-email.txt",
            mime="text/plain",
            key="download_investor_email",
        )


def render_property_maintenance_page():
    render_calculator_styles()
    st.subheader("Property Maintenance")

    property_name = ""
    property_postcode = ""
    if st.session_state.get("analysis_done") and st.session_state.get("data"):
        property_name = st.session_state["data"].get("name", "")
        property_postcode = st.session_state.get("selected_postcode", "")

    st.markdown(
        f"""
        <div class="deal-hero">
            <h3>Maintenance Planner</h3>
            <p>{escape(property_name or 'Track tenancy, compliance, and recurring maintenance for each property.')}</p>
            <div class="deal-chip-row">
                <span class="deal-chip">Property: {escape(property_name or 'Manual entry')}</span>
                <span class="deal-chip">Postcode: {escape(property_postcode or 'Add when known')}</span>
                <span class="deal-chip">Focus: reminders before tasks are due</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    today = date.today()
    move_in_default = st.session_state.get("maintenance_move_in_date", today)
    gas_default = st.session_state.get("maintenance_gas_due", pd.Timestamp(today) + pd.DateOffset(months=10))
    eicr_default = st.session_state.get("maintenance_eicr_due", pd.Timestamp(today) + pd.DateOffset(years=4))
    boiler_default = st.session_state.get("maintenance_boiler_due", pd.Timestamp(today) + pd.DateOffset(months=10))
    gutter_default = st.session_state.get("maintenance_gutter_due", pd.Timestamp(today) + pd.DateOffset(months=5))

    left_col, right_col = st.columns([1.2, 1])

    with left_col:
        st.markdown("**Tenancy & Compliance Dates**")
        tenant_move_in = st.date_input("New tenant move-in date", value=pd.Timestamp(move_in_default).date(), key="maintenance_move_in_date")
        gas_safety_due = st.date_input("Gas safety due date", value=pd.Timestamp(gas_default).date(), key="maintenance_gas_due")
        eicr_due = st.date_input("Electrical certificate due date", value=pd.Timestamp(eicr_default).date(), key="maintenance_eicr_due")
        boiler_service_due = st.date_input("Boiler service due date", value=pd.Timestamp(boiler_default).date(), key="maintenance_boiler_due")
        gutter_clean_due = st.date_input("Gutter cleaning due date", value=pd.Timestamp(gutter_default).date(), key="maintenance_gutter_due")

    with right_col:
        st.markdown("**Extra Task**")
        custom_task_name = st.text_input("Custom task name", placeholder="Example: Legionella review", key="maintenance_custom_task_name")
        custom_task_due = st.date_input("Custom task due date", value=today, key="maintenance_custom_task_due")
        custom_task_frequency_months = st.slider(
            "Custom task repeat cycle (months)",
            min_value=1,
            max_value=24,
            value=12,
            step=1,
            key="maintenance_custom_task_frequency",
        )
        st.caption("Leave the custom task name blank if you only want the standard schedule.")

    schedule = build_maintenance_schedule(
        tenant_move_in,
        gas_safety_due,
        eicr_due,
        boiler_service_due,
        gutter_clean_due,
        custom_task_name=custom_task_name,
        custom_task_due=custom_task_due if custom_task_name else None,
        custom_task_frequency_months=custom_task_frequency_months,
    )

    overdue_count = int((schedule["Status"] == "Overdue").sum())
    due_soon_count = int((schedule["Status"] == "Due Soon").sum())
    upcoming_count = int((schedule["Status"] == "Upcoming").sum())
    next_due_row = schedule.iloc[0]

    stat_col1, stat_col2, stat_col3, stat_col4 = st.columns(4)
    with stat_col1:
        st.markdown(
            build_dashboard_stat_html("Next Due", str(next_due_row["Task"]), str(next_due_row["Due Date"]), "dashboard-badge-watch"),
            unsafe_allow_html=True,
        )
    with stat_col2:
        st.markdown(
            build_dashboard_stat_html("Overdue", str(overdue_count), "Needs action now", "dashboard-badge-watch"),
            unsafe_allow_html=True,
        )
    with stat_col3:
        st.markdown(
            build_dashboard_stat_html("Due Soon", str(due_soon_count), "Reminder window open", "dashboard-badge-positive" if due_soon_count == 0 else "dashboard-badge-watch"),
            unsafe_allow_html=True,
        )
    with stat_col4:
        st.markdown(
            build_dashboard_stat_html("Upcoming", str(upcoming_count), "Planned ahead", "dashboard-badge-positive"),
            unsafe_allow_html=True,
        )

    timeline_col, note_col = st.columns([1.25, 0.95])
    with timeline_col:
        st.markdown("**Maintenance Timeline**")
        display_schedule = schedule.copy()
        display_schedule["Due Date"] = display_schedule["Due Date"].astype(str)
        display_schedule["Reminder Date"] = display_schedule["Reminder Date"].astype(str)
        st.dataframe(display_schedule, use_container_width=True, hide_index=True)
        st.download_button(
            "Download maintenance schedule",
            data=schedule.to_csv(index=False).encode("utf-8"),
            file_name="property-maintenance-schedule.csv",
            mime="text/csv",
            key="download_maintenance_schedule",
        )
        if st.button("Save maintenance plan", use_container_width=True, key="save_maintenance_plan"):
            plan_property_name = property_name or "Manual maintenance plan"
            save_maintenance_plan(current_user, plan_property_name, property_postcode, tenant_move_in, schedule)
            st.success("Maintenance plan saved to your account.")

    with note_col:
        st.markdown(
            """
            <div class="deal-card">
                <h4>Reminder Notes</h4>
                <p><strong>Overdue</strong> means the task date has already passed.</p>
                <p><strong>Due Soon</strong> means the reminder window has opened before the due date.</p>
                <p><strong>Upcoming</strong> means the task is still outside its reminder window.</p>
                <p>This tab is a practical landlord checklist for visits, certifications, servicing, and recurring upkeep.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )


def render_portfolio_page():
    st.subheader("Portfolio")
    st.caption("Your saved properties, remembered deals, and reusable project history live here.")

    saved_properties = load_saved_properties(current_user)
    saved_deals = load_saved_deals(current_user)

    stat_col1, stat_col2 = st.columns(2)
    stat_col1.metric("Saved properties", int(len(saved_properties)))
    stat_col2.metric("Saved deals", int(len(saved_deals)))

    property_col, deals_col = st.columns(2)

    with property_col:
        st.markdown("**Saved Properties**")
        if saved_properties.empty:
            st.info("No properties saved yet. Save one from the Analyse Deal page.")
        else:
            st.dataframe(saved_properties, use_container_width=True, hide_index=True)

    with deals_col:
        st.markdown("**Saved Deals**")
        if saved_deals.empty:
            st.info("No deals saved yet. Save an analysed deal or project scenario to build your history.")
        else:
            display_deals = saved_deals.copy()
            for field in ("purchase_price", "gdv", "profit", "refurb_total"):
                display_deals[field] = display_deals[field].apply(lambda value: format_money(value) if pd.notna(value) else "N/A")
            display_deals["roi"] = display_deals["roi"].apply(lambda value: f"{value:,.1f}%" if pd.notna(value) else "N/A")
            st.dataframe(display_deals, use_container_width=True, hide_index=True)


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


initialize_database()
current_user = render_auth_sidebar()

if current_user:
    upsert_user(current_user)
else:
    st.markdown(
        """
        ### Sign in to continue
        Use the sidebar to sign in and unlock saved properties, previous deals, maintenance plans, and investor materials.
        """
    )
    st.stop()


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
        data = st.session_state.data
        result = st.session_state.result
        refurb = st.session_state.refurb
        comps = st.session_state.comps
        render_deal_dashboard(
            data,
            result,
            refurb,
            comps,
            st.session_state.current_condition,
            st.session_state.target_condition,
        )

        save_col1, save_col2 = st.columns(2)
        with save_col1:
            if st.button("Save property to portfolio", use_container_width=True, key="save_property_button"):
                save_property_record(
                    current_user,
                    data.get("name", "Saved property"),
                    st.session_state.get("selected_postcode", ""),
                    st.session_state.get("selected_street", ""),
                    f"Condition: {st.session_state.current_condition} -> {st.session_state.target_condition}",
                )
                st.success("Property saved to your portfolio.")
        with save_col2:
            if st.button("Save analysed deal", use_container_width=True, key="save_analysed_deal_button"):
                save_deal_record(
                    current_user,
                    data.get("name", "Saved analysed deal"),
                    st.session_state.get("selected_postcode", ""),
                    "Analyse Deal",
                    st.session_state.get("selected_price", 0),
                    result.get("gdv", 0),
                    result.get("profit", 0),
                    result.get("roi", 0),
                    refurb.get("total", 0),
                    {"analysis": data, "result": result, "refurb": refurb},
                )
                st.success("Analysed deal saved to your portfolio.")

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

elif page == "Property Maintenance":
    render_property_maintenance_page()

elif page == "Portfolio":
    render_portfolio_page()

else:
    st.subheader(page)
    st.info("This section is ready for the next feature once you want to expand the app further.")
