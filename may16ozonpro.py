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
        
    # Convert the bytearray to standard bytes for Streamlit
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
        file_name="Ozon_WMS_Pro_User_Guide.pdf",
        mime="application/pdf",
        use_container_width=True
    )

    st.divider()
    st.subheader("📷 Scanner Settings")
    scan_dpi = st.select_slider("PDF DPI Resolution", options=[150, 200, 300], value=200)

st.title(f"🏢 Ozon WMS Pro | **{operator_name}**")

# ------------------ 5. TABS LAYOUT ------------------
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

    st.divider()
    st.markdown("### 📡 Quick External Tracking")
    status_input = st.text_area("Paste External Tracking Numbers", height=100)
    if st.button("Check API Status"):
        tn_list = SCANNING_ID_REGEX.findall(status_input)
        if tn_list:
            results = [{'Tracking ID': tn, 'Status': 'In Transit', 'Location': 'Hub', 'Updated': datetime.now().strftime('%H:%M')} for tn in tn_list]
            st.dataframe(pd.DataFrame(results), use_container_width=True)

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
        st.info("Inventory is empty. Please use the Inbound Receiving tab to add new stock.")

# --- TAB 4: PICK & PACK ---
with tabs[3]:
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
# --- PDF SEQUENCER ---
elif page == "PDF Sequencer":
    st.title("📑 Document Collator & Alphabetical Pre-Sorter")
    st.write("Upload a bulk compound document (shipping labels/manifests). The system will automatically scan each page, extract the product name, and pre-sort the document pages alphabetically.")
    
    label_pdf = st.file_uploader("Upload Bulk Shipping Manifest File", type=["pdf"])
    
    if label_pdf:
        if st.button("Analyze & Pre-Sort Pages Alphabetically"):
            try:
                # Read PDF using pypdf
                pdf_reader = pypdf.PdfReader(label_pdf)
                num_pages = len(pdf_reader.pages)
                
                if num_pages == 0:
                    st.error("The uploaded PDF file contains no valid structural pages.")
                else:
                    st.info(f"Processing {num_pages} pages. Analyzing underlying text coordinate grids...")
                    
                    page_mappings = []
                    
                    for idx, page_obj in enumerate(pdf_reader.pages):
                        page_text = page_obj.extract_text()
                        
                        # Fallback heuristic: look for common labels like "Product:", "Item:", or lines matching inventory SKU naming formats
                        product_name = "UNKNOWN_PRODUCT"
                        
                        # Clean up text lines for parsing
                        lines = [line.strip() for line in page_text.split('\n') if line.strip()]
                        
                        for line in lines:
                            # Heuristic 1: Explicit metadata prefix identification
                            if any(prefix in line.upper() for prefix in ["PRODUCT:", "ITEM NAME:", "DESCRIPTION:"]):
                                product_name = re.sub(r'(?i)^(product|item name|description):\s*', '', line).strip()
                                break
                            # Heuristic 2: Match lines containing known stock variants (e.g., iPhone, Galaxy)
                            elif any(keyword in line.upper() for keyword in ["IPHONE", "GALAXY", "SAMSUNG", "APPLE"]):
                                product_name = line.strip()
                                break
                        
                        # If nothing matches, use the first non-empty line as a fallback placeholder
                        if product_name == "UNKNOWN_PRODUCT" and lines:
                            product_name = lines[0][:50] # constraint boundary length
                            
                        page_mappings.append({
                            "page_index": idx,
                            "product_name": product_name.upper(),
                            "page_object": page_obj
                        })
                    
                    # Sort list natively using the extracted product names alphabetically
                    sorted_mappings = sorted(page_mappings, key=lambda x: x["product_name"])
                    
                    # Log summary metrics matrix back to dashboard user
                    st.subheader("📋 Sequenced Manifest Sort Mapping Matrix")
                    summary_data = [{"Original Page": m["page_index"] + 1, "Identified Product Key": m["product_name"]} for m in sorted_mappings]
                    st.dataframe(pd.DataFrame(summary_data), use_container_width=True)
                    
                    # Write out sorted structure to a fresh binary stream
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
                        mime="application/pdf"
                    )
                    
            except Exception as e:
                st.error(f"Failed to sequence document buffer: {e}")
                logger.error(f"Sequencer pipeline break: {e}", exc_info=True)

# --- TAB 7: AUDITOR ---
with tabs[6]:
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
                    
                    # Structural Logic Condition Matching
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
                        "Expected Elements": " | ".join(exp_set) if exp_set else "[EMPTY FIELD]",
                        "Actual Elements": " | ".join(got_set) if got_set else "[UNREGISTERED SCAN]"
                    }
                    
                    if highlight_errors_only and "MATCH" in status_flag:
                        continue
                    results_dataset.append(row_data)
                
                # Render Metrics Board
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Total Tracked Entities Checked", len(all_tracking_ids))

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
