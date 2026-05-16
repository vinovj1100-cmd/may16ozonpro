import streamlit as st
import sqlite3
import pandas as pd
import pytesseract
import pypdf
import re
import io
import json
import os
import hashlib
from datetime import datetime
from pdf2image import convert_from_bytes
from pyzbar.pyzbar import decode
from deep_translator import GoogleTranslator
from fpdf import FPDF
import logging

# Attempt to import Google Sheets dependencies
try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSHEETS_AVAILABLE = True
except ImportError:
    GSHEETS_AVAILABLE = False

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants & Regex
DB_PATH = "warehouse.db"
SCANNING_ID_REGEX = re.compile(r"\b\d{4,12}-?\d{4}-?\d?\b")

# ------------------ 1. PAGE CONFIG & UI ENHANCEMENTS ------------------
st.set_page_config(page_title="Ozon WMS Pro (Cloud)", layout="wide", page_icon="☁️", initial_sidebar_state="expanded")

st.markdown("""
<style>
    div[data-testid="metric-container"] {
        background-color: #1e1e2e;
        border: 1px solid #2d2d44;
        padding: 5% 5% 5% 10%;
        border-radius: 10px;
        box-shadow: 0 4px 6px rgba(0, 0, 0, 0.3);
        transition: transform 0.2s ease-in-out;
    }
    div[data-testid="metric-container"]:hover {
        transform: translateY(-5px);
        border-color: #4CAF50;
    }
    .stButton>button {
        border-radius: 8px;
        font-weight: bold;
        transition: all 0.3s;
    }
    .stButton>button:hover {
        box-shadow: 0 4px 12px rgba(0,0,0,0.2);
    }
    h1, h2, h3 { color: #f8f8f2; }
</style>
""", unsafe_allow_html=True)

# ------------------ 2. SQLITE DATABASE ENGINE ------------------
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS inventory
                     (SKU TEXT PRIMARY KEY, Product TEXT, Stock INTEGER, Location TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS daily_orders
                     (OrderID TEXT PRIMARY KEY, Status TEXT, RequiredSKUs TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS title_templates
                     (RawTitle TEXT PRIMARY KEY, StandardTitle TEXT)''')
        
        # We no longer seed mock data automatically. The user should pull from Google Sheets.
        conn.commit()

init_db()

def get_inventory():
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql_query("SELECT * FROM inventory", conn)

def get_orders():
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql_query("SELECT OrderID as 'Order ID', Status, RequiredSKUs as 'Required SKUs' FROM daily_orders", conn)

def get_templates():
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql_query("SELECT * FROM title_templates", conn)

def upsert_template(raw, standard):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO title_templates (RawTitle, StandardTitle) VALUES (?, ?)", (raw, standard))
        conn.commit()

def receive_inventory(sku, qty, product="Unknown Product", location="UNASSIGNED"):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT Stock FROM inventory WHERE SKU = ?", (sku,))
        row = c.fetchone()
        if row:
            new_stock = row[0] + qty
            if location != "UNASSIGNED":
                c.execute("UPDATE inventory SET Stock = ?, Location = ? WHERE SKU = ?", (new_stock, location, sku))
            else:
                c.execute("UPDATE inventory SET Stock = ? WHERE SKU = ?", (new_stock, sku))
            conn.commit()
            return True 
        else:
            c.execute("INSERT INTO inventory (SKU, Product, Stock, Location) VALUES (?, ?, ?, ?)", (sku, product, qty, location))
            conn.commit()
            return False 

def deduct_inventory(sku, qty=1):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("UPDATE inventory SET Stock = MAX(0, Stock - ?) WHERE SKU = ?", (qty, sku))
        conn.commit()

def update_order_status(order_id, status):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("UPDATE daily_orders SET Status = ? WHERE OrderID = ?", (status, order_id))
        conn.commit()

def bulk_update_inventory(df):
    with sqlite3.connect(DB_PATH) as conn:
        df.to_sql('inventory', conn, if_exists='replace', index=False)

def bulk_update_orders(df):
    with sqlite3.connect(DB_PATH) as conn:
        # Standardize column names back to SQL format
        df = df.rename(columns={'Order ID': 'OrderID', 'Required SKUs': 'RequiredSKUs'})
        df.to_sql('daily_orders', conn, if_exists='replace', index=False)

# ------------------ 3. GOOGLE SHEETS CLOUD SYNC ENGINE ------------------
@st.cache_resource(show_spinner="Authenticating Google Sheets...")
def init_gsheets_client(json_credentials_str):
    if not GSHEETS_AVAILABLE:
        st.error("Missing libraries. Please run: pip install gspread google-auth")
        return None
    try:
        creds_dict = json.loads(json_credentials_str)
        scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
        credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        return gspread.authorize(credentials)
    except Exception as e:
        st.error(f"Failed to authenticate Google Sheets: {e}")
        return None

def push_to_gsheets(client, url, dataframe):
    try:
        sheet = client.open_by_url(url).sheet1
        sheet.clear()
        if not dataframe.empty:
            sheet.update([dataframe.columns.values.tolist()] + dataframe.values.tolist())
        return True
    except Exception as e:
        st.error(f"Failed to push to Google Sheets: {e}")
        return False

def pull_from_gsheets(client, url):
    try:
        sheet = client.open_by_url(url).sheet1
        data = sheet.get_all_records()
        return pd.DataFrame(data)
    except Exception as e:
        st.error(f"Failed to pull from Google Sheets: {e}")
        return pd.DataFrame()

# ------------------ 4. UTILITIES & PDF GENERATOR ------------------
def generate_user_guide():
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", 'B', 16)
    pdf.cell(0, 10, "Ozon WMS Pro - Cloud User Guide", ln=True, align='C')
    pdf.ln(10)
    pdf.set_font("Arial", size=12)
    instructions = [
        ("CLOUD WARNING:", "Streamlit Cloud is ephemeral. ALWAYS push to Google Sheets when done, and pull from Sheets when starting."),
        ("Dashboard:", "View warehouse metrics, active orders, and low stock alerts."),
        ("Inbound Receiving:", "Scan new SKUs to add inventory to your master DB."),
        ("Inventory Hub:", "Live view of all warehouse stock. Edit stock directly."),
        ("Pick & Pack:", "Select an order, scan items, and deduct stock automatically."),
        ("PDF Sequencer:", "Upload bulk labels and map a sequence to print sorted PDFs."),
        ("Bulk Convert:", "Instantly translate and standardize generic titles using saved templates.")
    ]
    for title, desc in instructions:
        pdf.set_font("Arial", 'B', 12)
        pdf.cell(0, 8, title, ln=True)
        pdf.set_font("Arial", '', 11)
        pdf.multi_cell(0, 6, desc)
        pdf.ln(4)
    return pdf.output()

def robust_parse_multiline(text_data):
    data_map = {}
    current_tn = None
    for line in text_data.strip().split('\n'):
        line = line.strip()
        if not line: continue
        tn_match = SCANNING_ID_REGEX.search(line)
        if tn_match:
            current_tn = tn_match.group()
            desc = line.replace(current_tn, "").strip('|').strip()
            data_map.setdefault(current_tn, set())
            if desc: data_map[current_tn].add(desc)
        elif current_tn:
            data_map[current_tn].add(line)
    return data_map

def standardize_title(raw_text):
    text = raw_text.upper().replace("SMARTPHONE ", "").replace("MOBILE PHONE ", "")
    mappings = {
        "IPHONE": "APPLE IPHONE", " ORANGE": " COSMIC ORANGE", 
        " BLUE": " DEEP BLUE", " GRAY": " TITAN GRAY", 
        " GREY": " TITAN GRAY", " PURPLE": " SANDY PURPLE",
        "СМАРТФОН": "", "ГБ": "GB"
    }
    for key, value in mappings.items():
        if key in text and value not in text:
            text = text.replace(key, value)
    return text.strip()

if 'session_hash' not in st.session_state:
    st.session_state.session_hash = hashlib.sha256(os.urandom(16)).hexdigest()[:16]

# ------------------ 5. SIDEBAR CONFIGURATION ------------------
with st.sidebar:
    st.title("☁️ Cloud Operator")
    operator_name = st.text_input("Operator Name", value="Cloud_Staff")
    
    st.info("⚠️ **IMPORTANT:** Always pull data when you log in, and push data before you leave.")
    
    st.divider()
    st.subheader("🔗 Database Sync Engine")
    
    # Try to load secrets from Streamlit Cloud Secrets (if configured), otherwise use text input
    default_json = st.secrets.get("GCP_CREDENTIALS", "") if hasattr(st, "secrets") else ""
    default_inv_url = st.secrets.get("INV_SHEET", "") if hasattr(st, "secrets") else ""
    default_ord_url = st.secrets.get("ORD_SHEET", "") if hasattr(st, "secrets") else ""

    gsheet_json = st.text_area("Service Account JSON", type="password", value=default_json)
    inventory_sheet_url = st.text_input("Inventory Sheet URL", value=default_inv_url)
    orders_sheet_url = st.text_input("Orders Sheet URL", value=default_ord_url)
    
    col_pull, col_push = st.columns(2)
    
    with col_pull:
        if st.button("📥 PULL from Cloud", use_container_width=True):
            if gsheet_json and inventory_sheet_url:
                with st.spinner("Downloading from Sheets..."):
                    client = init_gsheets_client(gsheet_json)
                    if client:
                        inv_df = pull_from_gsheets(client, inventory_sheet_url)
                        if not inv_df.empty:
                            bulk_update_inventory(inv_df)
                        
                        if orders_sheet_url:
                            ord_df = pull_from_gsheets(client, orders_sheet_url)
                            if not ord_df.empty:
                                bulk_update_orders(ord_df)
                        st.success("✅ Downloaded!")
                        st.rerun()
            else:
                st.warning("Needs credentials & URL")

    with col_push:
        if st.button("📤 PUSH to Cloud", use_container_width=True, type="primary"):
            if gsheet_json and inventory_sheet_url:
                with st.spinner("Uploading to Sheets..."):
                    client = init_gsheets_client(gsheet_json)
                    if client:
                        push_to_gsheets(client, inventory_sheet_url, get_inventory())
                        if orders_sheet_url:
                            push_to_gsheets(client, orders_sheet_url, get_orders())
                        st.success("✅ Uploaded!")
            else:
                st.warning("Needs credentials & URL")

    st.divider()
    st.subheader("📷 Scanner Settings")
    scan_dpi = st.select_slider("PDF DPI Resolution", options=[150, 200, 300], value=200)

st.title(f"🏢 Ozon WMS Pro | **{operator_name}**")

# ------------------ 6. TABS LAYOUT ------------------
tabs = st.tabs([
    "📊 Dashboard", "📥 Inbound Receiving", "📦 Inventory", "🛒 Pick & Pack", 
    "🔙 Returns", "🔍 PDF Sequencer", "⚖️ Auditor", "🔄 Bulk Convert"
])

# --- TAB 1: DASHBOARD ---
with tabs[0]:
    inv_df = get_inventory()
    orders_df = get_orders()
    
    total_stock = inv_df['Stock'].sum() if not inv_df.empty else 0
    low_stock = len(inv_df[inv_df['Stock'] < 10]) if not inv_df.empty else 0
    pending_orders = len(orders_df[orders_df['Status'] == 'Pending']) if not orders_df.empty else 0
    shipped_orders = len(orders_df[orders_df['Status'] == 'Shipped']) if not orders_df.empty else 0

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("📦 Total Items in Stock", total_stock)
    m2.metric("⚠️ Low Stock Alerts", low_stock, delta_color="inverse")
    m3.metric("⏳ Pending Orders", pending_orders)
    m4.metric("✅ Shipped Today", shipped_orders)

# --- TAB 2: INBOUND RECEIVING ---
with tabs[1]:
    col_in1, col_in2, col_in3 = st.columns(3)
    with col_in1: inbound_sku = st.text_input("Scan / Enter SKU")
    with col_in2: inbound_qty = st.number_input("Quantity Received", min_value=1, value=1)
    with col_in3: inbound_bin = st.text_input("Assign to Bin Location", placeholder="e.g., C4-10")
    inbound_desc = st.text_input("Product Description (If New SKU)")

    if st.button("➕ Receive Inventory", type="primary"):
        if inbound_sku:
            is_update = receive_inventory(inbound_sku, inbound_qty, inbound_desc, inbound_bin)
            if is_update:
                st.toast(f"Updated {inbound_sku}: +{inbound_qty} units", icon="📦")
            else:
                st.toast(f"Created new SKU: {inbound_sku}", icon="✨")
        else:
            st.error("Please enter a SKU.")

# --- TAB 3: INVENTORY HUB ---
with tabs[2]:
    st.markdown("### Master Stock List")
    current_inv = get_inventory()
    if not current_inv.empty:
        edited_inv = st.data_editor(
            current_inv, 
            use_container_width=True, 
            num_rows="dynamic",
            column_config={"Stock": st.column_config.NumberColumn("Stock", min_value=0, step=1)}
        )
        if st.button("💾 Save Database Changes", type="primary"):
            bulk_update_inventory(edited_inv)
            st.toast("✅ Master database updated successfully!")
            st.rerun()
    else:
        st.info("Inventory is empty. Please pull from Google Sheets or receive new stock.")

# --- TAB 4: PICK & PACK ---
with tabs[3]:
    orders_df = get_orders()
    
    if orders_df.empty:
         st.info("No orders found. Please Pull from Cloud.")
    else:
        pending_df = orders_df[orders_df['Status'] == 'Pending']
        
        if pending_df.empty:
            st.success("🎉 All caught up! No pending orders.")
        else:
            col_ord, col_scan = st.columns(2)
            with col_ord:
                selected_order_id = st.selectbox("Select Order", pending_df['Order ID'].tolist())
                current_order = pending_df[pending_df['Order ID'] == selected_order_id].iloc[0]
                req_skus = [s.strip() for s in current_order['Required SKUs'].split(',')]
                st.info(f"**Packing Order:** {selected_order_id}")
                
                inv_df = get_inventory()
                for sku in req_skus:
                    if not inv_df.empty and sku in inv_df['SKU'].values:
                        prod_row = inv_df.loc[inv_df['SKU'] == sku, 'Product']
                        p_label = prod_row.values[0]
                    else:
                        p_label = "Unknown SKU"
                    st.markdown(f"- 📦 `{sku}` ({p_label})")

            with col_scan:
                scanned_skus_input = st.text_area("Barcode Scanner Input", placeholder="Scan items here...", height=150)
                if st.button("✅ Verify & Ship", type="primary", use_container_width=True):
                    scanned_list = [s.strip() for s in scanned_skus_input.split('\n') if s.strip()]
                    if sorted(scanned_list) == sorted(req_skus):
                        update_order_status(selected_order_id, 'Shipped')
                        for sku in scanned_list:
                            deduct_inventory(sku, 1)
                        st.toast(f"Order {selected_order_id} verified and shipped!", icon="🚀")
                        st.balloons()
                        st.rerun()
                    else:
                        st.error("❌ MISMATCH! Expected and scanned items do not align.")

# --- TAB 5: RETURNS ---
with tabs[4]:
    ret_order = st.text_input("Original Order ID (Optional)")
    ret_sku = st.text_input("Scan Returned SKU")
    ret_reason = st.selectbox("Return Reason", ["Customer Cancelled", "Defective/Damaged", "Wrong Item Shipped"])
    
    if st.button("🔄 Process Return", type="primary"):
        if ret_sku:
            if ret_reason == "Defective/Damaged":
                st.toast(f"Logged {ret_sku} as damaged. Not added to active inventory.", icon="⚠️")
            else:
                receive_inventory(ret_sku, 1)
                st.toast(f"Restocked 1 unit of {ret_sku}.", icon="✅")
            if ret_order:
                update_order_status(ret_order, 'Returned')
        else:
            st.error("Please scan a returning SKU.")

# --- TAB 6: PDF SEQUENCER ---
with tabs[5]:
    col1, col2 = st.columns([1, 2])
    with col1: sort_list = st.text_area("🎯 Target Sequence Order", height=300)
    with col2:
        label_file = st.file_uploader("📄 Upload Labels PDF", type="pdf")
        use_ocr = st.checkbox("Enable OCR Fallback", value=True)

    if st.button("🚀 Scan & Sort PDF", type="primary", use_container_width=True):
        target_ids = [tid.strip() for tid in sort_list.split('\n') if tid.strip()]
        if not target_ids or not label_file:
            st.warning("⚠️ Provide sequence IDs and upload a PDF.")
        else:
            with st.spinner("Mapping PDF pages..."):
                try:
                    pdf_reader = pypdf.PdfReader(io.BytesIO(label_file.getvalue()))
                    pdf_writer = pypdf.PdfWriter()
                    images = convert_from_bytes(label_file.getvalue(), dpi=scan_dpi)
                    id_to_page_map = {}
                    for i, img in enumerate(images):
                        page_codes = []
                        barcodes = decode(img)
                        for b in barcodes: page_codes.extend(SCANNING_ID_REGEX.findall(b.data.decode("utf-8")))
                        if not barcodes and use_ocr: page_codes.extend(SCANNING_ID_REGEX.findall(pytesseract.image_to_string(img)))
                        for code in set(page_codes): id_to_page_map[code] = pdf_reader.pages[i]

                    matched_count = 0
                    for tid in target_ids:
                        clean_tid = SCANNING_ID_REGEX.search(tid).group() if SCANNING_ID_REGEX.search(tid) else tid
                        if clean_tid in id_to_page_map:
                            pdf_writer.add_page(id_to_page_map[clean_tid])
                            matched_count += 1

                    if matched_count > 0:
                        out_io = io.BytesIO()
                        pdf_writer.write(out_io)
                        st.success(f"✅ Created PDF with {matched_count} sorted pages!")
                        st.download_button("📥 Download SORTED_LABELS.pdf", out_io.getvalue(), "sorted_labels.pdf", "application/pdf")
                    else:
                        st.error("❌ No matches found.")
                except Exception as e:
                    st.error(f"❌ Error: {str(e)}")

# --- TAB 7: AUDITOR ---
with tabs[6]:
    col_a, col_b = st.columns(2)
    with col_a: master_in = st.text_area("**MASTER (Expected)**", height=200)
    with col_b: scan_in = st.text_area("**SCAN (Actual)**", height=200)

    if st.button("⚡ Run Discrepancy Analysis"):
        if master_in and scan_in:
            m_map, s_map = robust_parse_multiline(master_in), robust_parse_multiline(scan_in)
            results = []
            for tid in sorted(list(set(m_map.keys()) | set(s_map.keys()))):
                exp, got = m_map.get(tid, set()), s_map.get(tid, set())
                status = "✅ MATCH" if exp == got else "❌ ERROR"
                results.append({"ID": tid, "Status": status, "Expected": " | ".join(exp), "Actual": " | ".join(got)})
            st.dataframe(pd.DataFrame(results).style.apply(lambda x: ['background-color: #ffcccc' if '❌' in str(v) else '' for v in x], axis=1), use_container_width=True)

# --- TAB 8: BULK CONVERT & TEMPLATES ---
with tabs[7]:
    st.subheader("🔄 **Bulk Title Converter & Smart Templates**")
    
    with st.expander("📂 View & Manage Saved Templates (Dictionary)"):
        template_df = get_templates()
        if not template_df.empty:
            edited_templates = st.data_editor(template_df, num_rows="dynamic", use_container_width=True)
            if st.button("💾 Save Manual Template Edits"):
                with sqlite3.connect(DB_PATH) as conn:
                    edited_templates.to_sql('title_templates', conn, if_exists='replace', index=False)
                st.toast("Templates updated!", icon="✅")
                st.rerun()
        else:
            st.info("No templates saved yet. They will be generated automatically when you convert below.")

    st.markdown("Paste data directly from Excel. It handles single columns (Titles) or double columns (Tracking ID + Title).")
    col_w, col_g = st.columns(2)
    with col_w: 
        white_col = st.text_area("📄 Input (Excel Paste)", height=300, help="Paste Excel columns here (ID + Name, or just Name).")
    
    if st.button("✨ Convert, Map & Save Templates", type="primary"):
        if white_col:
            with st.spinner("Processing templates and translating..."):
                lines = white_col.strip().split('\n')
                translator = GoogleTranslator(source='auto', target='en')
                
                template_df = get_templates()
                template_dict = dict(zip(template_df['RawTitle'], template_df['StandardTitle'])) if not template_df.empty else {}
                
                results = []
                new_saves = 0

                for l in lines:
                    parts = l.split('\t')
                    if len(parts) >= 2:
                        tracking_id, raw_title = parts[0].strip(), parts[1].strip()
                        if raw_title in template_dict:
                            std_title = template_dict[raw_title]
                        else:
                            translated = translator.translate(raw_title)
                            std_title = standardize_title(translated) if translated else "UNKNOWN"
                            upsert_template(raw_title, std_title)
                            template_dict[raw_title] = std_title
                            new_saves += 1
                        results.append(f"{tracking_id}\t{std_title}")
                    elif len(parts) == 1 and parts[0].strip():
                        raw_title = parts[0].strip()
                        if raw_title in template_dict:
                            std_title = template_dict[raw_title]
                        else:
                            translated = translator.translate(raw_title)
                            std_title = standardize_title(translated) if translated else "UNKNOWN"
                            upsert_template(raw_title, std_title)
                            template_dict[raw_title] = std_title
                            new_saves += 1
                        results.append(std_title)

                if new_saves > 0:
                    st.toast(f"Saved {new_saves} new items to the Template Database!", icon="💾")
                else:
                    st.toast("100% Match from existing templates. Zero translations used!", icon="⚡")

                with col_g: 
                    st.text_area("✅ Output (Standardized)", value="\n".join(results), height=300)
