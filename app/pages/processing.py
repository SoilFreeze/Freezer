from shiny import ui, render, reactive, module
import pandas as pd
import zipfile
import time
from datetime import datetime, timedelta
from google.cloud import bigquery
import os

# Import your custom app modules
from app.utils.config import PROJECT_ID, DATASET_ID
from app.data.processor import get_bq_client, get_universal_portal_data

# =============================================================================
# SHINY UI MODULE
# =============================================================================
@module.ui
def processing_ui():
    """Defines the visual layout for the Data Processing page."""
    return ui.div(
        ui.h2("⚙️ Data Processing & Reference Engine"),
        
        ui.navset_card_tab(
            # --- TAB 1: UPLOAD LOGIC ---
            ui.nav_panel("📄 Upload Telemetry",
                ui.h3("📄 Manual File Ingestion"),
                ui.p("Supports: Lord SensorConnect (Wide), Lord SensorCloud (Long), and Native SensorPush formats.", class_="text-info"),
                
                ui.input_file(
                    "manual_upload_main", 
                    "Select CSV or Excel files", 
                    accept=['.csv', '.xlsx', '.zip'], 
                    multiple=True
                ),
                ui.output_ui("upload_preview_ui"),
                ui.output_ui("commit_upload_ui")
            ),
            ui.input_numeric(
                "upload_lookback_days", 
                "Days of history to keep (0 to upload all)", 
                value=7, 
                min=0
            ),
            ui.output_ui("upload_preview_ui"),
            
            # --- TAB 2: EXPORT LOGIC ---
            ui.nav_panel("📥 Export Report",
                ui.h3("📥 Wide-Format Data Export"),
                ui.output_ui("export_warning_ui"),
                
                ui.layout_columns(
                    ui.input_date("e_start", "Start Date", value=datetime.now().date() - timedelta(days=30)),
                    ui.input_date("e_end", "End Date", value=datetime.now().date())
                ),
                ui.output_ui("export_loc_filter_ui"),
                ui.hr(),
                ui.output_ui("export_download_ui")
            ),
            
            # --- TAB 3: REFERENCE CURVE LIBRARY ---
            ui.nav_panel("📈 Ref Curve Library",
                ui.h3("📚 Theoretical Curve Library"),
                ui.p("Manage the target temperature curves used for visual goal-tracking on graphs."),
                
                ui.accordion(
                    ui.accordion_panel("🗑️ Library Management (Delete/Purge)",
                        ui.p("⚠️ Action is permanent. Purging will remove curves from all graphs.", class_="text-warning"),
                        ui.output_ui("delete_curve_ui"),
                        ui.hr(),
                        ui.p("Danger: This wipes the entire reference database.", class_="text-danger"),
                        ui.input_checkbox("confirm_purge_check", "I confirm I want to DELETE ALL curves in the library."),
                        ui.input_action_button("nuclear_purge_btn", "🧨 PURGE ENTIRE LIBRARY", class_="btn-danger disabled")
                    )
                ),
                ui.hr(),
                
                ui.h4("📤 Upload New Curves"),
                ui.p("Expected Format: CSV files (e.g., `2527-TP1.csv`). Data should start on Row 3. Col 1: Day, Col 2: Temp.", class_="text-muted"),
                ui.input_file("ref_uploader", "Select CSV Files", accept=[".csv"], multiple=True),
                ui.input_action_button("commit_ref_btn", "💾 Commit Files to BigQuery", class_="btn-primary w-100"),
                ui.hr(),
                
                ui.h4("📂 Current Library Inventory"),
                ui.output_data_frame("inventory_table_df")
            )
        )
    )

# =============================================================================
# SHINY SERVER MODULE
# =============================================================================
@module.server
def processing_server(input, output, session, client, selected_project):
    """Handles reactive database fetching and file processing operations."""

    # =========================================================================
    # TAB 1: UPLOAD LOGIC
    # =========================================================================
    processed_upload_dfs = reactive.Value([])
    target_upload_table = reactive.Value(None)
    upload_messages = reactive.Value([])

    @reactive.Effect
    @reactive.event(input.manual_upload_main)
    def process_uploaded_files():
        u_files = input.manual_upload_main()
        if not u_files:
            processed_upload_dfs.set([])
            target_upload_table.set(None)
            upload_messages.set([])
            return

        all_dfs = []
        target_tbl = None
        msgs = []

        for file_info in u_files:
            f_path = file_info["datapath"]
            f_name = file_info["name"]
            
            try:
                df_raw = None
                f_identifier = f_name
                is_sensorconnect = False
                skip_rows = 0

                # 1. READ FILE INTO df_raw
                if f_name.endswith('.zip'):
                    with zipfile.ZipFile(f_path, 'r') as z:
                        csv_name = [name for name in z.namelist() if name.endswith('.csv')][0]
                        with z.open(csv_name) as zf:
                            df_raw = pd.read_csv(zf, encoding='utf-8', dtype=str)
                            f_identifier = csv_name
                elif f_name.endswith('.csv'):
                    # Check for SensorConnect header using standard open
                    with open(f_path, 'rb') as f_check:
                        for i, line in enumerate(f_check):
                            if b"DATA_START" in line:
                                is_sensorconnect, skip_rows = True, i + 1
                                break
                    df_raw = pd.read_csv(f_path, encoding='latin1', skiprows=skip_rows, dtype=str)
                else:
                    df_raw = pd.read_excel(f_path, dtype=str)

                # 2. PROCESS df_raw -> df_processed
                if df_raw is not None and not df_raw.empty:
                    df_processed = pd.DataFrame()
                    actual_headers = list(df_raw.columns)
                    clean_headers = [str(h).strip().lower() for h in actual_headers]
                    
                    if is_sensorconnect:
                        time_col = [h for h in actual_headers if 'time' in h.lower()][0]
                        value_vars = [h for h in actual_headers if h != time_col]
                        df_melted = df_raw.melt(id_vars=[time_col], value_vars=value_vars, var_name='NodeNum', value_name='temperature')
                        df_processed['timestamp'] = pd.to_datetime(df_melted[time_col], errors='coerce', utc=True)
                        df_processed['NodeNum'] = df_melted['NodeNum'].str.strip().str.replace(':', '-')
                        df_processed['temperature'] = pd.to_numeric(df_melted['temperature'], errors='coerce')
                        target_tbl = "raw_lord"
                    
                    elif any(k in clean_headers for k in ['channel', 'node']) and any('time' in h for h in clean_headers):
                        time_h = actual_headers[next(i for i, h in enumerate(clean_headers) if 'time' in h)]
                        node_h = actual_headers[next(i for i, h in enumerate(clean_headers) if 'channel' in h or 'node' in h)]
                        temp_h = [h for h in actual_headers if 'temp' in h.lower()][0]
                        df_processed['timestamp'] = pd.to_datetime(df_raw[time_h], errors='coerce', utc=True)
                        df_processed['NodeNum'] = df_raw[node_h].str.strip().str.replace(':', '-')
                        df_processed['temperature'] = pd.to_numeric(df_raw[temp_h], errors='coerce')
                        target_tbl = "raw_lord"
                        
                    else: # Generic SensorPush
                        t_match = next((h for h in actual_headers if 'timestamp' in h.lower() or 'time' in h.lower()), None)
                        v_match = next((h for h in actual_headers if 'temp' in h.lower() or 'probe' in h.lower()), None)
                        if t_match and v_match:
                            df_processed['timestamp'] = pd.to_datetime(df_raw[t_match], errors='coerce', utc=True)
                            df_processed['temperature'] = pd.to_numeric(df_raw[v_match], errors='coerce')
                            
                            raw_filename = f_identifier.split('/')[-1].replace('.csv', '').strip()
                            clean_node_num = raw_filename.split('-starts-')[0].strip() if '-starts-' in raw_filename else raw_filename
                            df_processed['NodeNum'] = clean_node_num
                            target_tbl = "raw_sensorpush"
                    
                    # 2. PROCESS df_raw -> df_processed
                    # ... (keep your existing parsing logic) ...
                    
                        if not df_processed.empty:
                            df_processed = df_processed.dropna(subset=['timestamp', 'temperature'])
                            
                            # --- NEW LOOKBACK FILTER ---
                            lookback = input.upload_lookback_days()
                            if lookback > 0:
                                latest_time = df_processed['timestamp'].max()
                                cutoff_time = latest_time - pd.Timedelta(days=lookback)
                                df_processed = df_processed[df_processed['timestamp'] >= cutoff_time]
                            # ---------------------------
                            
                            if not df_processed.empty:
                                all_dfs.append(df_processed)
                                display_name = df_processed['NodeNum'].iloc[0] if 'NodeNum' in df_processed.columns else f_identifier
                                msgs.append(f"✅ {display_name}: {len(df_processed)} records.")
            
            except Exception as e:
                msgs.append(f"❌ Error processing {f_name}: {e}")

        processed_upload_dfs.set(all_dfs)
        target_upload_table.set(target_tbl)
        upload_messages.set(msgs)

    @output
    @render.ui
    def upload_preview_ui():
        msgs = upload_messages.get()
        if not msgs: return ui.HTML("")
        return ui.div([ui.p(msg) for msg in msgs])

    @output
    @render.ui
    def commit_upload_ui():
        dfs = processed_upload_dfs.get()
        tbl = target_upload_table.get()
        if dfs and tbl:
            total_records = sum(len(d) for d in dfs)
            return ui.input_action_button("commit_batch_btn", f"🚀 Commit {total_records} records to {tbl}", class_="btn-success w-100")
        return ui.HTML("")

    @reactive.Effect
    @reactive.event(input.commit_batch_btn)
    def commit_to_bq():
        dfs = processed_upload_dfs.get()
        target_table = target_upload_table.get()
        if not dfs or not target_table or client is None: return

        combined_df = pd.concat(dfs, ignore_index=True)
        combined_df['temperature'] = combined_df['temperature'].round(1)
        
        try:
            table_id = f"{PROJECT_ID}.{DATASET_ID}.{target_table}"
            job_config = bigquery.LoadJobConfig(
                schema=[
                    bigquery.SchemaField("timestamp", "TIMESTAMP"),
                    bigquery.SchemaField("NodeNum", "STRING"),
                    bigquery.SchemaField("temperature", "FLOAT"), 
                ],
                write_disposition="WRITE_APPEND"
            )
            client.load_table_from_dataframe(combined_df[['timestamp', 'NodeNum', 'temperature']], table_id, job_config=job_config).result()
            ui.notification_show("Batch Upload Complete!", type="success")
            
            # Reset state
            processed_upload_dfs.set([])
            target_upload_table.set(None)
            upload_messages.set([])
        except Exception as e:
            ui.notification_show(f"Upload Failed: {e}", type="error")


    # =========================================================================
    # TAB 2: EXPORT LOGIC
    # =========================================================================
    export_dataset = reactive.Value(pd.DataFrame())

    @output
    @render.ui
    def export_warning_ui():
        proj = selected_project() if callable(selected_project) else selected_project
        if not proj or proj == "All Projects":
            return ui.p("⚠️ Select a specific project in the sidebar to export data.", class_="text-warning")
        return ui.HTML("")

    @reactive.Effect
    def fetch_export_data():
        proj = selected_project() if callable(selected_project) else selected_project
        if not proj or proj == "All Projects": 
            export_dataset.set(pd.DataFrame())
            return
            
        full_df = get_universal_portal_data(proj)
        export_dataset.set(full_df)

    @output
    @render.ui
    def export_loc_filter_ui():
        df = export_dataset.get()
        if df.empty: return ui.HTML("")
        all_locs = sorted(df['Location'].unique().tolist())
        return ui.input_selectize("export_locs", "Filter by Location (Leave empty for ALL)", all_locs, multiple=True)

    @output
    @render.ui
    def export_download_ui():
        df = export_dataset.get()
        if df.empty: return ui.HTML("")
        
        mask = (df['timestamp'].dt.date >= input.e_start()) & (df['timestamp'].dt.date <= input.e_end())
        locs = input.export_locs() if hasattr(input, 'export_locs') else []
        if locs:
            mask = mask & (df['Location'].isin(locs))
            
        export_df = df.loc[mask].copy()
        if export_df.empty:
            return ui.p("No data found for the selected criteria.", class_="text-warning")
            
        export_df['Sensor'] = export_df['Location'] + " (" + export_df['NodeNum'].astype(str) + ")"
        wide_df = export_df.pivot_table(index='timestamp', columns='Sensor', values='temperature', aggfunc='first').reset_index()
        wide_df['timestamp'] = wide_df['timestamp'].dt.strftime('%Y-%m-%d %H:%M:%S')
        
        return ui.div(
            ui.p(f"Report Ready: {len(wide_df.columns)-1} columns generated.", class_="text-success"),
            ui.download_button("download_export_csv", "💾 Download Custom CSV Export", class_="btn-primary w-100")
        )

    @render.download(filename=lambda: f"{selected_project() if callable(selected_project) else selected_project}_Export_{datetime.now().strftime('%Y%m%d')}.csv")
    def download_export_csv():
        df = export_dataset.get()
        mask = (df['timestamp'].dt.date >= input.e_start()) & (df['timestamp'].dt.date <= input.e_end())
        locs = input.export_locs() if hasattr(input, 'export_locs') else []
        if locs: mask = mask & (df['Location'].isin(locs))
        export_df = df.loc[mask].copy()
        export_df['Sensor'] = export_df['Location'] + " (" + export_df['NodeNum'].astype(str) + ")"
        wide_df = export_df.pivot_table(index='timestamp', columns='Sensor', values='temperature', aggfunc='first').reset_index()
        wide_df['timestamp'] = wide_df['timestamp'].dt.strftime('%Y-%m-%d %H:%M:%S')
        yield wide_df.to_csv(index=False).encode('utf-8')


    # =========================================================================
    # TAB 3: REFERENCE CURVE LIBRARY
    # =========================================================================
    @reactive.Calc
    def get_curve_inventory():
        if client is None: return pd.DataFrame()
        try:
            return client.query(
                f"SELECT CurveID, COUNT(*) as Data_Points, MIN(Day) as Start_Day, MAX(Day) as End_Day "
                f"FROM `{PROJECT_ID}.{DATASET_ID}.reference_curves` "
                f"GROUP BY CurveID ORDER BY CurveID"
            ).to_dataframe()
        except Exception:
            return pd.DataFrame()

    @output
    @render.ui
    def delete_curve_ui():
        inv_df = get_curve_inventory()
        if inv_df.empty: return ui.p("No curves available to delete.")
        
        opts = sorted(inv_df['CurveID'].tolist())
        return ui.div(
            ui.input_select("delete_curve_picker", "Select Curve to Remove", opts),
            ui.input_action_button("delete_single_curve_btn", "🗑️ Delete Selected Curve", class_="btn-outline-danger")
        )

    @output
    @render.data_frame
    def inventory_table_df():
        return render.DataGrid(get_curve_inventory())

    # --- Deletion Effects ---
    @reactive.Effect
    @reactive.event(input.delete_single_curve_btn)
    def delete_curve():
        to_delete = input.delete_curve_picker()
        try:
            client.query(f"DELETE FROM `{PROJECT_ID}.{DATASET_ID}.reference_curves` WHERE CurveID='{to_delete}'").result()
            ui.notification_show(f"Removed {to_delete} from library.", type="success")
            # Invalidate cache implicitly via user interaction/rerender
        except Exception as e:
            ui.notification_show(f"Failed to delete: {e}", type="error")

    @reactive.Effect
    def toggle_purge_btn():
        if input.confirm_purge_check():
            ui.update_action_button("nuclear_purge_btn", disabled=False)
        else:
            ui.update_action_button("nuclear_purge_btn", disabled=True)

    @reactive.Effect
    @reactive.event(input.nuclear_purge_btn)
    def purge_library():
        try:
            client.query(f"TRUNCATE TABLE `{PROJECT_ID}.{DATASET_ID}.reference_curves`").result()
            ui.notification_show("Library has been completely purged.", type="success")
            ui.update_checkbox("confirm_purge_check", value=False)
        except Exception as e:
            ui.notification_show(f"Purge failed: {e}", type="error")

    # --- Ref Curve Upload ---
    @reactive.Effect
    @reactive.event(input.commit_ref_btn)
    def upload_reference_curves():
        u_files = input.ref_uploader()
        if not u_files:
            ui.notification_show("Please select files first.", type="warning")
            return
            
        table_ref = f"{PROJECT_ID}.{DATASET_ID}.reference_curves"
        
        for file_info in u_files:
            f_path = file_info["datapath"]
            f_name = file_info["name"]
            curve_id = f_name.replace(".csv", "")
            
            try:
                try:
                    ref_df = pd.read_csv(f_path, skiprows=2, names=['Day', 'Temp'], encoding='utf-8')
                except UnicodeDecodeError:
                    ref_df = pd.read_csv(f_path, skiprows=2, names=['Day', 'Temp'], encoding='latin-1')
                    
                ref_df['Day'] = pd.to_numeric(ref_df['Day'], errors='coerce')
                ref_df['Temp'] = pd.to_numeric(ref_df['Temp'], errors='coerce')
                ref_df = ref_df.dropna(subset=['Day', 'Temp'])
                
                if ref_df.empty:
                    ui.notification_show(f"❌ {f_name} contained no valid numeric data.", type="error")
                    continue
                    
                ref_df['CurveID'] = curve_id
                
                # Atomic Update: Delete old and Load new
                client.query(f"DELETE FROM `{table_ref}` WHERE CurveID='{curve_id}'").result()
                job_config = bigquery.LoadJobConfig(
                    schema=[
                        bigquery.SchemaField("Day", "INTEGER"),
                        bigquery.SchemaField("Temp", "FLOAT"),
                        bigquery.SchemaField("CurveID", "STRING"),
                    ],
                    write_disposition="WRITE_APPEND"
                )
                client.load_table_from_dataframe(ref_df, table_ref, job_config=job_config).result()
                ui.notification_show(f"Success: {curve_id}", type="success")
            except Exception as e:
                ui.notification_show(f"❌ Error processing {f_name}: {e}", type="error")
