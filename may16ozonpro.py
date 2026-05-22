import streamlit as st
import sqlite3
import pandas as pd
import pytesseract
import pypdf
import re
import io
import os
import hashlib
from datetime import datetime
from pdf2image import convert_from_bytes
from pyzbar.pyzbar import decode
from deep_translator import GoogleTranslator
from fpdf import FPDF
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants & Regex
DB_PATH = "warehouse.db"
SCANNING_ID_REGEX = re.compile(r"\b\d{4,12}-?\d{4}-?\d?\b")

# ------------------ 1. PAGE CONFIG & UI ENHANCEMENTS ------------------
st.set_page_config(page_title="Ozon WMS Pro", layout="wide", page_icon="🏢", initial_sidebar_state="expanded")

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
        
        # Seed Inventory if empty
        c.execute("SELECT COUNT(*) FROM inventory")
        if c.fetchone()[0] == 0:
            mock_inv = [
                ("APP-IP15-256-BLK", "APPLE IPHONE 15 256GB BLACK", 45, "A1-01"),
                ("APP-IP15P-256-ORG", "APPLE IPHONE 15 PRO COSMIC ORANGE 256GB", 8, "A1-02"),
                ("SAM-S24-512-GRY", "SAMSUNG GALAXY S24 TITAN GRAY 512GB", 12, "B2-15")
            ]
            c.executemany("INSERT INTO inventory VALUES (?, ?, ?, ?)", mock_inv)
            
        # Seed Orders if empty
        c.execute("SELECT COUNT(*) FROM daily_orders")
        if c.fetchone()[0] == 0:
            mock_orders = [
                ("ORD-9981", "Pending", "APP-IP15P-256-ORG, SAM-S24-512-GRY"),
                ("ORD-9982", "Pending", "SAM-S24-512-GRY"),
                ("ORD-9983", "Shipped", "APP-IP15-256-BLK")
            ]
            c.executemany("INSERT INTO daily_orders VALUES (?, ?, ?)", mock_orders)
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
        df = df.rename(columns={'Order ID': 'OrderID', 'Required SKUs': 'RequiredSKUs'})
        df.to_sql('daily_orders', conn, if_exists='replace', index=False)

# ------------------ 3. UTILITIES & PDF GENERATOR ------------------
def generate_user_guide():
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", 'B', 16)
    pdf.cell(0, 10, "Ozon WMS Pro - User Guide", ln=True, align='C')
    pdf.ln(10)
    pdf.set_font("Arial", size=12)
    instructions = [
        ("Dashboard:", "View warehouse metrics, active orders, and low stock alerts."),
        ("Inbound Receiving:", "Scan new SKUs to add inventory to your master DB."),
        ("Inventory Hub:", "Live view of all warehouse stock. Edit stock directly."),
        ("Pick & Pack:", "Select an order, scan items, and deduct stock automatically."),
        ("Returns:", "Process inbound returns and mark items damaged or restockable."),
        ("PDF Sequencer:", "Upload bulk labels and map a sequence to print sorted PDFs."),
        ("Discrepancy Auditor:", "Paste expected vs actual IDs to spot missing items."),
        ("Bulk Convert:", "Instantly translate and standardize generic titles using saved templates.")
    ]
    for title, desc in instructions:
        pdf.set_font("Arial", 'B', 12)
        pdf.cell(0, 8, title, ln=True)
        pdf.set_font("Arial", '', 11)
        pdf.multi_cell(0, 6, desc)
        pdf.ln(4)
    return bytes(pdf.output())

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

# ------------------ 4. SIDEBAR CONFIGURATION ------------------
with st.sidebar:
    st.title("☁️ Cloud Operator")
    operator_name = st.text_input("Operator Name", value="Cloud_Staff")
    st.divider()
    st.subheader("📚 Documentation")
    st.download_button(
        label="📥 Download User Guide (PDF)",
        data=generate_user_guide(),
        file_name="Ozon_WMS_User_Guide.pdf",
        mime="application/pdf"
    )

# ------------------ 5. RUN TABS WORKSPACE ------------------
st.title("🏢 Ozon WMS Pro Workspace")

tabs = st.tabs([
    "📥 Inbound Receiving", 
    "🗄️ Inventory Hub", 
    "📦 Pick & Pack", 
    "🔄 Returns Desk", 
    "📑 PDF Sequencer", 
    "⚖️ Discrepancy Auditor"
])

# --- TAB 1: INBOUND RECEIVING ---
with tabs[0]:
    col_in1, col_in2, col_in3 = st.columns(3)
    with col_in1: 
        inbound_sku = st.text_input("Scan / Enter SKU").upper().strip()
    with col_in2: 
        inbound_qty = st.number_input("Quantity Received", min_value=1, value=1)
    with col_in3: 
        inbound_bin = st.text_input("Assign to Bin Location", placeholder="e.g., C4-10").upper().strip()
    
    inbound_desc = st.text_input("Product Description (If New SKU)")
    
    if st.button("➕ Receive Inventory", type="primary"):
        if inbound_sku:
            is_update = receive_inventory(inbound_sku, inbound_qty, inbound_desc if inbound_desc else "New Product Entry", inbound_bin if inbound_bin else "UNASSIGNED")
            if is_update:
                st.toast(f"Updated {inbound_sku}: +{inbound_qty} units", icon="📦")
            else:
                st.toast(f"Created new SKU: {inbound_sku}", icon="✨")
        else:
            st.error("Please enter a SKU.")

# --- TAB 2: INVENTORY HUB ---
with tabs[1]:
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
st.info("Inventory is empty. Please use the Inbound Receiving tab to add new stock.")
--- TAB 3: PICK & PACK ---
with tabs[2]:
orders_df = get_orders()
if orders_df.empty:
st.info("No orders found in the database.")
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
st.info(f"Packing Order: {selected_order_id}")
inv_df = get_inventory()
for sku in req_skus:
if not inv_df.empty and sku in inv_df['SKU'].values:
prod_row = inv_df.loc[inv_df['SKU'] == sku, 'Product']
p_label = prod_row.values[0]
else:
p_label = "Unknown SKU"
st.markdown(f"- 📦 {sku} ({p_label})")
with col_scan:
scanned_skus_input = st.text_area("Barcode Scanner Input", placeholder="Scan items here (comma or line separated)...", height=150)
if st.button("✅ Verify & Ship", type="primary", use_container_width=True):
# Robust cleaning handle for comma or line split values
scanned_list = [s.strip().upper() for s in scanned_skus_input.replace('\n', ',').split(',') if s.strip()]
if sorted(scanned_list) == sorted([r.upper() for r in req_skus]):
update_order_status(selected_order_id, 'Shipped')
for sku in scanned_list:
deduct_inventory(sku, 1)
st.toast(f"Order {selected_order_id} verified and shipped!", icon="🚀")
st.balloons()
st.rerun()
else:
st.error("❌ MISMATCH! Expected and scanned items do not align.")
--- TAB 4: RETURNS ---
with tabs[3]:
st.subheader("🔄 Process Inbound Return")
ret_order = st.text_input("Original Order ID (Optional)").strip()
ret_sku = st.text_input("Scan Returned SKU").upper().strip()
ret_reason = st.selectbox("Return Reason", ("Customer Cancelled", "Defective/Damaged", "Wrong Item Shipped"))
if st.button("🔄 Process Return Entry", type="primary"):
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
--- TAB 5: PDF SEQUENCER ---
with tabs[4]:
st.subheader("📑 Document Collator & Alphabetical Pre-Sorter")
st.write("Upload a bulk compound document (shipping labels/manifests). The system will automatically scan each page, extract the product name, and pre-sort the document pages alphabetically.")
label_pdf = st.file_uploader("Upload Bulk Shipping Manifest File", type=["pdf"])
if label_pdf:
if st.button("Analyze & Pre-Sort Pages Alphabetically", type="primary", use_container_width=True):
try:
pdf_reader = pypdf.PdfReader(label_pdf)
num_pages = len(pdf_reader.pages)
if num_pages == 0:
st.error("The uploaded PDF file contains no valid structural pages.")
else:
st.info(f"Processing {num_pages} pages. Analyzing underlying text coordinate grids...")
page_mappings = []
for idx, page_obj in enumerate(pdf_reader.pages):
page_text = page_obj.extract_text()
product_name = "UNKNOWN_PRODUCT"
lines = [line.strip() for line in page_text.split('\n') if line.strip()]
for line in lines:
if any(prefix in line.upper() for prefix in ("PRODUCT:", "ITEM NAME:", "DESCRIPTION:")):
product_name = re.sub(r'(?i)^(product|item name|description):\s*', '', line).strip()
break
elif any(keyword in line.upper() for keyword in ("IPHONE", "GALAXY", "SAMSUNG", "APPLE")):
product_name = line.strip()
break
if product_name == "UNKNOWN_PRODUCT" and lines:
product_name = lines[0][:50]
page_mappings.append({
"page_index": idx,
"product_name": product_name.upper(),
"page_object": page_obj
})
sorted_mappings = sorted(page_mappings, key=lambda x: x["product_name"])
st.subheader("📋 Sequenced Manifest Sort Mapping Matrix")
summary_data = [{"Original Page": m["page_index"] + 1, "Identified Product Key": m["product_name"]} for m in sorted_mappings)
st.dataframe(pd.DataFrame(summary_data), use_container_width=True)
pdf_writer = pypdf.PdfWriter()
for item in sorted_mappings:
pdf_writer.add_page(item["page_object"])
output_pdf_stream = io.BytesIO()
pdf_writer.write(output_pdf_stream)
output_pdf_stream.seek(0)
st.success("🎉 Target document compiled safely! Ready for download.")
st.download_button(
label="📥 Download Alphabetically Sorted Manifest (PDF)",
data=output_pdf_stream.getvalue(),
file_name=f"Alphabetized_Manifest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf",
mime="application/pdf",
use_container_width=True
)
except Exception as e:
st.error(f"Failed to sequence document buffer: {e}")
logger.error(f"Sequencer pipeline break: {e}", exc_info=True)
--- TAB 6: AUDITOR ---
with tabs[5]:
st.subheader("⚖ Operational Discrepancy Auditor & Reconciliation Workbench")
st.markdown("Execute line checks across systems. Paste structural manifests to evaluate physical picking accuracy.")
col_a, col_b = st.columns(2)
with col_a:
master_in = st.text_area("📋 Master Manifest Records (Expected System Data)", height=220, placeholder="Paste data containing tracking entries...")
with col_b:
scan_in = st.text_area("📦 Physical Scanner Frame Output (Actual Inbound Ingest)", height=220, placeholder="Paste or scan barcodes sequentially...")
audit_col1, audit_col2 = st.columns(2)
ignore_whitespace = audit_col1.checkbox("Normalize Variations & Clear Whitespace", value=True)
highlight_errors_only = audit_col2.checkbox("Filter Output to Display Errors Only", value=False)
if st.button("⚡ Execute High-Volume Audit Validation Check", type="primary", use_container_width=True):
if not master_in or not scan_in:
st.error("Data Deficit: Both operational telemetry zones require context vectors before checking data paths.")
else:
with st.spinner("Processing system diff tables..."):
m_map = robust_parse_multiline(master_in)
s_map = robust_parse_multiline(scan_in)
all_tracking_ids = sorted(list(set(m_map.keys()) | set(s_map.keys())))
results_dataset = []
shortages = 0
overages = 0
perfect_matches = 0
for tid in all_tracking_ids:
exp_set = m_map.get(tid, set())
got_set = s_map.get(tid, set())
if ignore_whitespace:
exp_set = {str(item).strip().upper() for item in exp_set}
got_set = {str(item).strip().upper() for item in got_set}
if not exp_set and got_set:
status_flag = "⚠️ SURPLUS OVERAGE"
overages += 1
elif exp_set and not got_set:
status_flag = "❌ CRITICAL SHORTAGE"
shortages += 1
elif exp_set == got_set:
status_flag = "✅ STABLE MATCH"
perfect_matches += 1
else:
status_flag = "☣ METADATA MISMATCH"
shortages += 1
row_data = {
"Tracking Reference ID": tid,
"Status Class": status_flag,
"Expected Elements": " | ".join(exp_set) if exp_set else "(EMPTY FIELD)",
"Actual Elements": " | ".join(got_set) if got_set else "(UNREGISTERED SCAN)"
}
if highlight_errors_only and "MATCH" in status_flag:
continue
results_dataset.append(row_data)
m1, m2, m3, m4 = st.columns(4)
m1.metric("Total Tracked Checked", len(all_tracking_ids))
m2.metric("Stable Matches", perfect_matches)
m3.metric("Shortages Identified", shortages, delta=f"-{shortages}" if shortages > 0 else None, delta_color="inverse")
m4.metric("Overages Identified", overages, delta=f"+{overages}" if overages > 0 else None)
if results_dataset:
st.dataframe(pd.DataFrame(results_dataset), use_container_width=True)
else:
st.success("No anomalies found based on configuration settings.")



