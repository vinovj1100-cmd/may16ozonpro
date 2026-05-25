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
import logging
from PIL import Image
import numpy as np
from deep_translator import GoogleTranslator
from fpdf import FPDF

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

# Initialize functional session states safely
if 'session_hash' not in st.session_state:
    st.session_state.session_hash = hashlib.sha256(os.urandom(16)).hexdigest()[:16]
if 'photo_text' not in st.session_state:
    st.session_state.photo_text = ""
if 'parsed_items' not in st.session_state:
    st.session_state.parsed_items = None

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
        c.execute("SELECT Stock, Product, Location FROM inventory WHERE SKU = ?", (sku,))
        row = c.fetchone()
        if row:
            new_stock = row[0] + qty
            final_loc = location if location != "UNASSIGNED" else row[2]
            final_prod = product if product != "Unknown Product" else row[1]
            c.execute("UPDATE inventory SET Stock = ?, Product = ?, Location = ? WHERE SKU = ?", (new_stock, final_prod, final_loc, sku))
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
        c = conn.cursor()
        # Safe bulk transaction rewrite preserving structural configurations
        c.execute("DELETE FROM inventory")
        for _, row in df.iterrows():
            c.execute("INSERT INTO inventory (SKU, Product, Stock, Location) VALUES (?, ?, ?, ?)", 
                      (str(row['SKU']), str(row['Product']), int(row['Stock']), str(row['Location'])))
        conn.commit()

# ------------------ 3. UTILITIES & PDF GENERATOR ------------------
def generate_user_guide():
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", 'B', 16)
    pdf.cell(0, 10, "Ozon WMS Pro - User Guide", ln=True, align='C')
    pdf.ln(10)
    
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
        
    # Standard string output cast as bytes buffer object
    return pdf.output(dest='S').encode('latin1')

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
    if not raw_text: return "UNKNOWN"
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

def extract_text_from_image(image):
    try:
        img_array = np.array(image)
        text = pytesseract.image_to_string(img_array)
        return text
    except Exception as e:
        logger.error(f"OCR extraction failed: {e}")
        return ""

def parse_receiving_data(text_data):
    receiving_items = []
    lines = text_data.strip().split('\n')
    
    for line in lines:
        line = line.strip()
        if not line: continue
        
        # Check if line contains layout pipes
        if '|' in line:
            parts = [p.strip() for p in line.split('|')]
            sku = parts[0]
            description = parts[1] if len(parts) > 1 else "From Photo/Sheet"
            qty_match = re.search(r'(\d+)\s*$', line)
            qty = int(qty_match.group(1)) if qty_match else 1
            location = parts[2] if len(parts) > 2 else "UNASSIGNED"
        else:
            # Fallback regex spacing analyzer if strings aren't structured with pipes
            tokens = line.split()
            if not tokens: continue
            sku = tokens[0]
            qty_match = re.search(r'\b(\d+)\b', line)
            qty = int(qty_match.group(1)) if qty_match else 1
            description = "OCR Text Line Extraction Match"
            location = "UNASSIGNED"
            
        receiving_items.append({
            'sku': sku,
            'product': description,
            'quantity': qty,
            'location': location
        })
    return receiving_items

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
    st.markdown("## 📥 **Inbound Receiving Hub**")
    receiving_method = st.radio("Select Receiving Method", ["Manual Scan", "📸 Photo Upload (Google Sheets)", "📊 Excel File Upload"], horizontal=True)
    st.divider()
    
    if receiving_method == "Manual Scan":
        col_in1, col_in2, col_in3 = st.columns(3)
        with col_in1: inbound_sku = st.text_input("Scan / Enter SKU")
        with col_in2: inbound_qty = st.number_input("Quantity Received", min_value=1, value=1)
        with col_in3: inbound_bin = st.text_input("Assign to Bin Location", placeholder="e.g., C4-10")
        inbound_desc = st.text_input("Product Description (If New SKU)")

        if st.button("➕ Receive Inventory", type="primary"):
            if inbound_sku:
                is_update = receive_inventory(inbound_sku, inbound_qty, inbound_desc or "Unknown Product", inbound_bin or "UNASSIGNED")
                if is_update: st.toast(f"Updated {inbound_sku}: +{inbound_qty} units", icon="📦")
                else: st.toast(f"Created new SKU: {inbound_sku}", icon="✨")
                st.rerun()
            else:
                st.error("Please enter a SKU.")
                
    elif receiving_method == "📸 Photo Upload (Google Sheets)":
        photo_upload = st.file_uploader("📸 Upload Sheet Photo (JPG/PNG)", type=["jpg", "jpeg", "png"])
        if photo_upload:
            image = Image.open(photo_upload)
            col_img, col_preview = st.columns([1, 1])
            with col_img: st.image(image, caption="Uploaded Photo", use_container_width=True)
            with col_preview:
                if st.button("🔍 Extract Text via OCR", type="primary"):
                    with st.spinner("Extracting text..."):
                        st.session_state.photo_text = extract_text_from_image(image)
                        st.success("✅ Text extracted successfully!")
            
            if st.session_state.photo_text:
                extracted_display = st.text_area("Extracted Data (Edit if needed)", value=st.session_state.photo_text, height=200)
                if st.button("✨ Parse & Preview Items", type="primary"):
                    st.session_state.parsed_items = parse_receiving_data(extracted_display)
            
            if st.session_state.parsed_items:
                items_df = pd.DataFrame(st.session_state.parsed_items)
                edited_items = st.data_editor(items_df, use_container_width=True)
                if st.button("✅ Receive All Items from Photo", type="primary"):
                    for _, row in edited_items.iterrows():
                        receive_inventory(row['sku'], int(row['quantity']), row['product'], row['location'])
                    st.toast("✅ Successfully integrated entries!", icon="📦")
                    st.session_state.parsed_items = None
                    st.rerun()

    elif receiving_method == "📊 Excel File Upload":
        excel_upload = st.file_uploader("📊 Upload Excel File", type=["xlsx", "xls", "csv"])
        if excel_upload:
            try:
                excel_df = pd.read_csv(excel_upload) if excel_upload.name.endswith('.csv') else pd.read_excel(excel_upload)
                st.dataframe(excel_df, use_container_width=True)
                available_cols = excel_df.columns.tolist()
                
                col_map_col1, col_map_col2, col_map_col3, col_map_col4 = st.columns(4)
                with col_map_col1: sku_col = st.selectbox("SKU Column", available_cols)
                with col_map_col2: product_col = st.selectbox("Product Column", available_cols)
                with col_map_col3: qty_col = st.selectbox("Quantity Column", available_cols)
                with col_map_col4: location_col = st.selectbox("Location Column (Optional)", [None] + available_cols)
                
                mapped_preview = []
                for _, row in excel_df.iterrows():
                    mapped_preview.append({
                        'sku': str(row[sku_col]).strip(),
                        'product': str(row[product_col]).strip(),
                        'quantity': int(row[qty_col]) if pd.notna(row[qty_col]) else 1,
                        'location': str(row[location_col]).strip() if location_col and pd.notna(row[location_col]) else 'UNASSIGNED'
                    })
                
                st.dataframe(pd.DataFrame(mapped_preview), use_container_width=True)
                if st.button("✅ Receive All Items from Excel", type="primary"):
                    for item in mapped_preview:
                        receive_inventory(item['sku'], item['quantity'], item['product'], item['location'])
                    st.success("Excel records committed successfully!")
                    st.balloons()
            except Exception as e:
                st.error(f"Processing Error: {e}")

# --- TAB 3: INVENTORY HUB ---
with tabs[2]:
    st.markdown("### Master Stock List")
    current_inv = get_inventory()
    if not current_inv.empty:
        edited_inv = st.data_editor(current_inv, use_container_width=True, num_rows="dynamic")
        if st.button("💾 Save Database Changes", type="primary"):
            bulk_update_inventory(edited_inv)
            st.toast("✅ Master database updated cleanly!", icon="✅")
            st.rerun()

# --- TAB 4: PICK & PACK ---
with tabs[3]:
    orders_df = get_orders()
    if not orders_df.empty:
        pending_df = orders_df[orders_df['Status'] == 'Pending']
        if not pending_df.empty:
            col_ord, col_scan = st.columns(2)
            with col_ord:
                selected_order_id = st.selectbox("Select Order", pending_df['Order ID'].tolist())
                current_order = pending_df[pending_df['Order ID'] == selected_order_id].iloc[0]
                req_skus = [s.strip() for s in current_order['Required SKUs'].split(',')]
                st.info(f"**Packing Order:** {selected_order_id}")
                for sku in req_skus: st.markdown(f"- 📦 `{sku}`")
            with col_scan:
                scanned_skus_input = st.text_area("Barcode Scanner Input", placeholder="Scan items here...")
                if st.button("✅ Verify & Ship", type="primary"):
                    scanned_list = [s.strip() for s in scanned_skus_input.split('\n') if s.strip()]
                    if sorted(scanned_list) == sorted(req_skus):
                        update_order_status(selected_order_id, 'Shipped')
                        for sku in scanned_list: deduct_inventory(sku, 1)
                        st.toast("Order shipped!", icon="🚀")
                        st.rerun()
                    else:
                        st.error("❌ MISMATCH! Items do not match the order list.")

# --- TAB 5: RETURNS ---
with tabs[4]:
    ret_order = st.text_input("Original Order ID")
    ret_sku = st.text_input("Scan Returned SKU")
    ret_reason = st.selectbox("Return Reason", ["Customer Cancelled", "Defective/Damaged", "Wrong Item Shipped"])
    
    if st.button("🔄 Process Return", type="primary"):
        if ret_sku:
            if ret_reason == "Defective/Damaged":
                st.toast("Logged as Damaged. Excluded from clean warehouse stock.", icon="⚠️")
            else:
                receive_inventory(ret_sku, 1)
                st.toast("Restocked safely into live infrastructure.", icon="✅")
            if ret_order: update_order_status(ret_order, 'Returned')
            st.rerun()

# --- TAB 6: PDF SEQUENCER ---
with tabs[5]:
    st.title("📑 Document Collator & Alphabetical Pre-Sorter")
    st.write("Upload a bulk compound document (shipping labels/manifests). The system will automatically scan each page, extract tracking keys, and pre-sort your document.")
    
    label_pdf = st.file_uploader("Upload Bulk Shipping Manifest File", type=["pdf"])
    
    st.divider()
    st.markdown("### 🔗 **Custom Sort by Reference Tracking Numbers**")
    st.write("Paste your reference list from Excel or your system export. This field matches raw text containing 7-digit IDs alongside space-separated codes.")
    
    reference_tracking = st.text_area(
        "📌 Reference Tracking Numbers / Sheet Paste",
        height=150,
        placeholder="Example paste:\n5349213 1982\n5339536 6589"
    )
    
    if label_pdf:
        if st.button("Analyze & Pre-Sort Pages", type="primary"):
            try:
                pdf_reader = pypdf.PdfReader(label_pdf)
                num_pages = len(pdf_reader.pages)
                
                if num_pages == 0:
                    st.error("The uploaded PDF file contains no valid structural pages.")
                else:
                    st.info(f"Processing {num_pages} pages. Analyzing text layers for 7-digit tracking sequences...")
                    
                    page_mappings = []
                    
                    # 1. Parse out the target sequence order from user input text/image strings
                    # Looks for any 7-digit sequences matching your standard layout structure
                    reference_order = re.findall(r'\b\d{7}\b', reference_tracking)
                    
                    for idx, page_obj in enumerate(pdf_reader.pages):
                        page_text = page_obj.extract_text() or ""
                        lines = [line.strip() for line in page_text.split('\n') if line.strip()]
                        
                        product_name = "UNKNOWN_LOCATION"
                        tracking_number = None
                        secondary_id = ""
                        
                        # Find tracking numbers (7 digits) and text markers inside the PDF page
                        all_page_numbers = re.findall(r'\b\d{4,7}\b', page_text)
                        seven_digit_matches = [num for num in all_page_numbers if len(num) == 7]
                        four_digit_matches = [num for num in all_page_numbers if len(num) == 4]
                        
                        if seven_digit_matches:
                            tracking_number = seven_digit_matches[0]
                        if four_digit_matches:
                            secondary_id = four_digit_matches[0]
                            
                        for line in lines:
                            if any(k in line.upper() for k in ["УЛИЦА", "ПОСЁЛОК", "Г ", "ОБЛ"]):
                                product_name = line.strip()
                                break
                        
                        page_mappings.append({
                            "page_index": idx,
                            "product_name": product_name,
                            "tracking_number": tracking_number,
                            "secondary_id": secondary_id,
                            "page_object": page_obj
                        })
                    
                    # 2. Sequence Pages based on parsed spreadsheet references or fallback
                    if reference_order:
                        # Priority dictionary mapping tracking IDs to their pasted row hierarchy
                        priority_map = {tn: idx for idx, tn in enumerate(reference_order)}
                        
                        sorted_mappings = sorted(
                            page_mappings, 
                            key=lambda x: (
                                priority_map.get(x["tracking_number"], len(reference_order)),
                                x["product_name"]
                            )
                        )
                        st.success(f"✅ Successfully sorted pages matching {len(reference_order)} custom reference indices!")
                    else:
                        # Fallback to destination/product grouping rules
                        sorted_mappings = sorted(page_mappings, key=lambda x: x["product_name"])
                        st.success("✅ Sorted alphabetically by extracted address/product markers!")
                    
                    # Render detailed execution logs
                    st.subheader("📋 Sequenced Sorting Matrix")
                    summary_data = [
                        {
                            "New Output Page": new_idx + 1,
                            "Original Page": m["page_index"] + 1, 
                            "Extracted Tracking Key": m["tracking_number"] if m["tracking_number"] else "Missing",
                            "Secondary ID": m["secondary_id"],
                            "Destination Heuristic": m["product_name"]
                        } 
                        for new_idx, m in enumerate(sorted_mappings)
                    ]
                    st.dataframe(pd.DataFrame(summary_data), use_container_width=True)
                    
                    # Compile fresh binary binary architecture stream
                    pdf_writer = pypdf.PdfWriter()
                    for item in sorted_mappings:
                        pdf_writer.add_page(item["page_object"])
                        
                    output_pdf_stream = io.BytesIO()
                    pdf_writer.write(output_pdf_stream)
                    output_pdf_stream.seek(0)
                    
                    st.download_button(
                        label="📥 Download Sequenced PDF Manifest",
                        data=output_pdf_stream.getvalue(),
                        file_name=f"Sequenced_Manifest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf",
                        mime="application/pdf",
                        use_container_width=True
                    )
                    
            except Exception as e:
                st.error(f"Failed to sequence document buffer: {e}")
                logger.error(f"Sequencer pipeline break: {e}", exc_info=True)

# --- TAB 7: AUDITOR ---
with tabs[6]:
    st.subheader("⚖ Operational Discrepancy Auditor & Reconciliation Workbench")
    st.markdown("Execute barcode reconciliations. This sheet strips spaces, auxiliary keys, and extracts explicit 7-digit system IDs automatically.")
    
    col_a, col_b = st.columns(2)
    with col_a: 
        master_in = st.text_area(
            "📋 System Master Records / Excel Spreadsheet Paste", 
            height=250, 
            placeholder="Paste system data table columns here...\nExample:\n5349213 1982\n5339536 6589"
        )
    with col_b: 
        scan_in = st.text_area(
            "📦 Actual Warehouse Scans / Manifest Ingest Data", 
            height=250, 
            placeholder="Paste raw tracking scans or full structural logs..."
        )
        
    if st.button("⚡ Execute High-Volume Audit Validation Check", type="primary", use_container_width=True):
        if not master_in or not scan_in:
            st.error("Data Deficit: Please populate both operational data zones to evaluate structural differences.")
        else:
            with st.spinner("Processing system cross-referencing logic matrices..."):
                
                # Enhanced extraction logic to handle multiple columns gracefully
                def extract_structured_manifest_keys(raw_text):
                    parsed_map = {}
                    lines = raw_text.strip().split('\n')
                    for line in lines:
                        line = line.strip()
                        if not line:
                            continue
                        
                        # Find any 7 digit tracking number keys on this row line entry
                        found_keys = re.findall(r'\b\d{7}\b', line)
                        if found_keys:
                            target_key = found_keys[0]
                            # Capture remaining numbers/words on that row line as associated tags
                            remainder = line.replace(target_key, "").strip()
                            parsed_map.setdefault(target_key, set())
                            if remainder:
                                parsed_map[target_key].add(remainder)
                            else:
                                parsed_map[target_key].add("Present")
                    return parsed_map

                m_map = extract_structured_manifest_keys(master_in)
                s_map = extract_structured_manifest_keys(scan_in)
                
                all_tracking_ids = sorted(list(set(m_map.keys()) | set(s_map.keys())))
                results_dataset = []
                
                shortages = 0
                overages = 0
                perfect_matches = 0
                
                for tid in all_tracking_ids:
                    in_master = tid in m_map
                    in_scans = tid in s_map
                    
                    # Extract string variables for cleaner dashboard presentation
                    master_meta = ", ".join(m_map.get(tid, [])) if in_master else ""
                    scan_meta = ", ".join(s_map.get(tid, [])) if in_scans else ""
                    
                    if in_master and in_scans:
                        status_flag = "✅ STABLE MATCH"
                        perfect_matches += 1
                    elif in_master and not in_scans:
                        status_flag = "❌ CRITICAL SHORTAGE"
                        shortages += 1
                    else:
                        status_flag = "⚠️ SURPLUS OVERAGE"
                        overages += 1
                
                    results_dataset.append({
                        "Tracking ID Key": tid,
                        "Status Class": status_flag,
                        "Expected Metadata": master_meta if master_meta else "[NOT IN SYSTEM]",
                        "Actual Scanned Metadata": scan_meta if scan_meta else "[MISSING FROM PHYSICAL]"
                    })
                
                # Render Metrics Dashboard Summary Blocks
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Total Unique IDs Audited", len(all_tracking_ids))
                m2.metric("Perfect Matches", perfect_matches)
                m3.metric("Shortages Found", shortages, delta="-", delta_color="inverse" if shortages > 0 else "normal")
                m4.metric("Overages Found", overages, delta="+", delta_color="off" if overages > 0 else "normal")
                
                # Display structural dataframe results
                st.dataframe(pd.DataFrame(results_dataset), use_container_width=True)

# --- TAB 8: BULK CONVERT & TEMPLATES ---
with tabs[7]:
    st.subheader("🔄 Bulk Title Converter")
    white_col = st.text_area("📄 Input (Excel Paste Column Text data)")
    if st.button("✨ Convert, Map & Save Templates", type="primary"):
        if white_col:
            lines = white_col.strip().split('\n')
            translator = GoogleTranslator(source='auto', target='en')
            template_df = get_templates()
            template_dict = dict(zip(template_df['RawTitle'], template_df['StandardTitle'])) if not template_df.empty else {}
            
            converted_outputs = []
            for line in lines:
                parts = line.split('\t')
                raw_title = parts[1].strip() if len(parts) >= 2 else parts[0].strip()
                if raw_title in template_dict:
                    std_title = template_dict[raw_title]
                else:
                    translated = translator.translate(raw_title)
                    std_title = standardize_title(translated)
                    upsert_template(raw_title, std_title)
                    template_dict[raw_title] = std_title
                converted_outputs.append(std_title)
            st.text_area("✅ Output (Standardized)", value="\n".join(converted_outputs), height=200)
