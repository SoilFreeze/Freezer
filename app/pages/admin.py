from shiny import ui, render, reactive, module
import pandas as pd
import time
import re
import requests
import numpy as np
import os
from PIL import Image
from datetime import datetime, timedelta

# Internal Config & Data connections
from app.utils.config import (
    PROJECT_ID, 
    DATASET_ID, 
    PROJECT_REGISTRY_TABLE, 
    NODE_REGISTRY_TABLE
)
from app.data.processor import get_bq_client

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================
def natural_sort_key(s):
    if pd.isnull(s):
        return []
    return [int(text) if text.isdigit() else str(text).lower() for text in re.split(r'(\d+)', str(s))]

def get_project_mask(df, selected_project):
    if selected_project == "All Projects":
        return pd.Series([True] * len(df), index=df.index)
        
    job_num = str(selected_project).split('-')[0].strip()
    is_job = df['Project'].astype(str).str.startswith(job_num)
    
    phase_match = re.search(r'(?i)Phase\s*(\d+)', selected_project)
    if phase_match and 'Phase' in df.columns:
        target_phase = phase_match.group(1)
        return is_job & (df['Phase'].astype(str).str.strip() == target_phase)
        
    return is_job

# =============================================================================
# SHINY UI MODULE
# =============================================================================
@module.ui
def admin_ui():
    """Defines the visual layout for the Admin Tools page."""
    return ui.div(
        ui.h2("🛠️ Admin Tools"),
        
        ui.navset_card_tab(
            # --- TAB 1: ADMIN SUMMARY ---
            ui.nav_panel("📋 Admin Summary",
                ui.h3("📋 Centralized Infrastructure Status Overview"),
                ui.h4("📡 Hardware Inventory Fleet Breakdown"),
                ui.output_data_frame("fleet_breakdown_df"),
                ui.hr(),
                ui.h4("🏗️ Active Deployment Overview Matrix"),
                ui.output_data_frame("deployment_overview_df")
            ),
            
            # --- TAB 2: BULK APPROVAL ---
            ui.nav_panel("⚡ Bulk Approval",
                ui.h3("⚡ Bulk Approval and Database Maintenance"),
                ui.p("Consolidate raw datasets into 1-decimal hourly averages and safely remove all high-frequency and duplicate records system-wide."),
                
                ui.layout_columns(
                    ui.input_radio_buttons("blk_target_scope", "Target Scope", ["Project Wide", "Specific Location", "Specific Node"], inline=True),
                    ui.input_select("blk_current_status", "Filter Current Designation Status:", ["ALL", "ALL BUT NULL", "TRUE", "NULL (STREAMING / UNREVIEWED)", "MASKED", "OFFICE", "BADDATA"]),
                    ui.input_select("blk_new_status", "Set Approval Status To:", ["TRUE", "MASKED", "OFFICE", "BADDATA"])
                ),
                ui.hr(),
                
                ui.layout_columns(
                    ui.div(
                        ui.input_select("blk_temp_dir", "Temporal Direction", ["Between Range", "Older Than", "Newer Than"]),
                        ui.output_ui("blk_temporal_ui")
                    ),
                    ui.div(
                        ui.input_select("blk_val_filter", "Value Filter", ["No Threshold", "Above Threshold", "Below Threshold"]),
                        ui.input_numeric("blk_threshold", "Threshold Value (°F)", value=100.0)
                    ),
                    ui.output_ui("blk_scope_ui")
                ),
                ui.hr(),
                
                ui.input_action_button("blk_verify_btn", "🔍 Step 1: Verify Match Count & Current Status Profiles", class_="btn-info w-100"),
                ui.output_ui("blk_verify_results_ui"),
                ui.hr(),
                
                ui.input_checkbox("blk_confirm_check", "I authorize updating these data markers to the target parameters specified."),
                ui.output_ui("blk_execute_btn_ui")
            ),
            
            # --- TAB 3: DATA RECOVERY ---
            ui.nav_panel("📡 Data Recovery",
                ui.h3("📡 Data Recovery Engine"),
                ui.p("Extract raw chronological data streams directly from the SensorPush Cloud API architecture."),
                ui.hr(),
                
                ui.layout_columns(
                    ui.output_ui("rec_proj_ui"),
                    ui.output_ui("rec_loc_ui"),
                    ui.output_ui("rec_node_ui")
                ),
                ui.hr(),
                
                ui.h4("📅 Define Recovery Timeline Parameters"),
                ui.layout_columns(
                    ui.input_date("rec_start_date", "Extraction Window Start Date"),
                    ui.input_date("rec_end_date", "Extraction Window End Date")
                ),
                ui.hr(),
                
                ui.output_ui("rec_warning_banner"),
                ui.input_action_button("rec_execute_btn", "🚀 Execute Cloud Backfill Ingestion Pipeline Run", class_="btn-danger w-100"),
                ui.output_ui("rec_results_ui")
            ),
            
            # --- TAB 4: PROJECT MASTER ---
            ui.nav_panel("⚙️ Project Master",
                ui.h3("🗄️ Complete Master Project Lifecycle Directory"),
                ui.output_data_frame("project_master_df")
            ),
            
            # --- TAB 5: PIPE MAPPER ---
            ui.nav_panel("🗺️ Pipe Mapper",
                ui.h3("🗺️ As-Built Pipe Mapper"),
                ui.p("Select a site plan to log X/Y pixel coordinates for physical locations."),
                
                ui.layout_columns(
                    ui.div(
                        ui.output_ui("mapper_image_ui")
                    ),
                    ui.div(
                        ui.output_ui("mapper_controls_ui"),
                        ui.output_data_frame("mapper_table_df"),
                        ui.output_ui("mapper_download_ui"),
                        ui.input_action_button("mapper_clear_btn", "Clear All Data", class_="btn-warning w-100")
                    ),
                    col_widths=[8, 4]
                )
            )
        )
    )

# =============================================================================
# SHINY SERVER MODULE
# =============================================================================
@module.server
def admin_server(input, output, session, client, selected_project, display_tz):
    """Handles the reactive logic and database operations for the Admin Tools."""
    
    # --- REACTIVE CACHES ---
    @reactive.Calc
    def get_full_registry():
        if client is None: return pd.DataFrame()
        reg_df = client.query(f"SELECT * FROM `{NODE_REGISTRY_TABLE}` WHERE End_Date IS NULL OR TRIM(CAST(End_Date AS STRING)) = ''").to_dataframe()
        reg_df['Project'] = reg_df['Project'].astype(str).str.split('.').str[0].str.strip()
        return reg_df

    @reactive.Calc
    def get_fleet_matrix():
        if client is None: return pd.DataFrame()
        sum_q = f"""
            WITH ProjectBase AS (
              SELECT Project, ProjectName, ProjectStatus, Date_Freezedown, TRIM(SPLIT(CAST(Project AS STRING), '-')[OFFSET(0)]) as RootJob, REGEXP_EXTRACT(CAST(Project AS STRING), r'(?i)Phase\\s*(\\d+)') as ProjectPhase
              FROM `{PROJECT_REGISTRY_TABLE}`
              WHERE ShowActive IS TRUE AND UPPER(CAST(Project AS STRING)) NOT LIKE '%OFFICE%'
            ),
            ActiveNodes AS (
              SELECT NodeNum, CAST(Phase AS STRING) as Phase, TRIM(SPLIT(CAST(Project AS STRING), '-')[OFFSET(0)]) as NodeRootJob
              FROM `{NODE_REGISTRY_TABLE}`
              WHERE (End_Date IS NULL OR TRIM(CAST(End_Date AS STRING)) = '')
            )
            SELECT p.Project, p.ProjectName, p.ProjectStatus, p.Date_Freezedown, COUNT(DISTINCT n.NodeNum) as Mapped_Sensors, 
                   COUNT(DISTINCT CASE WHEN m.timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 6 HOUR) THEN n.NodeNum END) as Active_6h, 
                   COUNT(DISTINCT CASE WHEN m.timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR) THEN n.NodeNum END) as Active_24h 
            FROM ProjectBase p
            LEFT JOIN ActiveNodes n ON n.NodeRootJob = p.RootJob AND (p.ProjectPhase IS NULL OR TRIM(n.Phase) = p.ProjectPhase)
            LEFT JOIN `{PROJECT_ID}.{DATASET_ID}.master_data_view_v2` m ON UPPER(TRIM(CAST(n.NodeNum AS STRING))) = UPPER(TRIM(CAST(m.NodeNum AS STRING))) AND m.timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
            GROUP BY 1,2,3,4 ORDER BY p.Project ASC
        """
        return client.query(sum_q).to_dataframe()
    @reactive.Effect
    @reactive.event(input.run_consolidation_btn)
    def run_global_consolidation():
        if client is None: return
        
        target_table = f"{PROJECT_ID}.{DATASET_ID}.master_data_view_v2"
        
        # This replaces the table with an hourly aggregated version of itself
        consolidation_q = f"""
            CREATE OR REPLACE TABLE `{target_table}` AS
            SELECT 
                TIMESTAMP_TRUNC(timestamp, HOUR) as timestamp,
                NodeNum,
                MAX(Project) as Project,
                MAX(Location) as Location,
                MAX(Bank) as Bank,
                MAX(Depth) as Depth,
                MAX(Phase) as Phase,
                MAX(System) as System,
                MAX(Hardware) as Hardware,
                MAX(approval_status) as approval_status,
                MAX(BaseElevation) as BaseElevation,
                MAX(node_elevation) as node_elevation,
                ROUND(AVG(temperature), 1) as temperature
            FROM `{target_table}`
            GROUP BY timestamp, NodeNum
        """
        
        try:
            client.query(consolidation_q).result()
            ui.notification_show("Consolidation complete! High-frequency data has been averaged.", type="success")
        except Exception as e:
            ui.notification_show(f"Consolidation failed: {e}", type="error")
            
    # --- TAB 1: ADMIN SUMMARY LOGIC ---
    @output
    @render.data_frame
    def fleet_breakdown_df():
        reg_df = get_full_registry()
        if reg_df.empty: return pd.DataFrame()
        
        def classify_family(node): return "Lord" if "-ch" in str(node).lower() else "SP" if str(node).lower().startswith("sp") else "TP" if str(node).lower().startswith("tp") else "Other"
        
        fleet_df = reg_df.copy()
        fleet_df['Hardware Family'] = fleet_df['NodeNum'].apply(classify_family)
        fleet_df['Parent ID'] = fleet_df['NodeNum'].apply(lambda x: re.split(r'(?i)-ch', str(x))[0] if "-ch" in str(x).lower() else x)
        
        deduped = fleet_df.sort_values(by=['Parent ID']).drop_duplicates(subset=['Parent ID']).copy()
        # The current problematic line in admin_server:
        pivot = deduped.groupby(['Hardware Family', 'SensorStatus']).size().unstack(fill_value=0).reindex(["TP", "SP", "Lord", "Other"], fill_value=0)
        
        for col in ["Available", "Dead", "Diagnostic", "On Project"]: 
            if col not in pivot.columns: pivot[col] = 0
            
        pivot = pivot[["Available", "Dead", "Diagnostic", "On Project"]]
        pivot['Total Units'] = pivot.sum(axis=1)
        return render.DataGrid(pivot.reset_index())

    @output
    @render.data_frame
    def deployment_overview_df():
        matrix_df = get_fleet_matrix()
        if matrix_df.empty: return pd.DataFrame()
        
        rows = []
        # Reactive inputs like display_tz must be resolved by caller (passed down from main)
        current_tz = display_tz() if callable(display_tz) else display_tz 
        
        for _, r in matrix_df.iterrows():
            elapsed = max(0, (pd.Timestamp.now(tz=current_tz).date() - pd.to_datetime(r['Date_Freezedown']).date()).days) if pd.notnull(r['Date_Freezedown']) else 0
            rows.append({
                "Project ID": r['Project'], 
                "Project Name": r['ProjectName'] or r['Project'], 
                "Mapped Sensors": int(r['Mapped_Sensors']), 
                "Active (6h)": int(r['Active_6h']), 
                "Active (24h)": int(r['Active_24h']), 
                "Project Status Timeline": f"Day {elapsed} of {str(r['ProjectStatus']).title()}" if pd.notnull(r['Date_Freezedown']) else "Not Freezing"
            })
        return render.DataGrid(pd.DataFrame(rows))
    # =========================================================================
    # TAB 2: BULK APPROVAL & VERIFICATION LOGIC
    # =========================================================================
    ui.hr(),
    ui.h4("🧹 System-Wide Consolidation"),
    ui.p("This will permanently compress high-frequency data into 1-hour averages and round temperatures to 1 decimal place."),
    ui.input_action_button("run_consolidation_btn", "🔄 Run Global Consolidation Job", class_="btn-warning w-100")
    
    verify_match_count = reactive.Value(None)
    constructed_where_clause = reactive.Value(None)
    
    # --- 1. Dynamic UI Renderers ---
    @output
    @render.ui
    def blk_scope_ui():
        scope = input.blk_target_scope()
        return ui.input_text("blk_scope_val", f"Enter {scope} ID/Name:")

    @output
    @render.ui
    def blk_temporal_ui():
        temp_dir = input.blk_temp_dir()
        if temp_dir == "Between Range":
            return ui.layout_columns(
                ui.input_date("blk_start_date", "Start Date"),
                ui.input_date("blk_end_date", "End Date")
            )
        else:
            return ui.input_date("blk_single_date", "Target Date")

    # --- 2. Verification & Match Lookup ---
    @reactive.Effect
    @reactive.event(input.blk_verify_btn)
    def verify_bulk_action():
        if client is None: return
        
        where_parts = []
        
        # Scope Filter
        scope = input.blk_target_scope()
        scope_val = input.blk_scope_val()
        if scope_val:
            if scope == "Project Wide":
                where_parts.append(f"Project LIKE '{scope_val.strip()}%'")
            elif scope == "Specific Location":
                where_parts.append(f"UPPER(Location) = '{scope_val.strip().upper()}'")
            elif scope == "Specific Node":
                where_parts.append(f"UPPER(NodeNum) = '{scope_val.strip().upper()}'")
                
        # Status Filter
        curr_status = input.blk_current_status()
        if curr_status == "ALL BUT NULL":
            where_parts.append("approval_status IS NOT NULL")
        elif curr_status == "NULL (STREAMING / UNREVIEWED)":
            where_parts.append("approval_status IS NULL")
        elif curr_status != "ALL":
            where_parts.append(f"UPPER(approval_status) = '{curr_status}'")
            
        # Temporal Filter
        temp_dir = input.blk_temp_dir()
        if temp_dir == "Between Range":
            where_parts.append(f"timestamp >= '{input.blk_start_date()} 00:00:00' AND timestamp <= '{input.blk_end_date()} 23:59:59'")
        elif temp_dir == "Older Than":
            where_parts.append(f"timestamp < '{input.blk_single_date()} 00:00:00'")
        elif temp_dir == "Newer Than":
            where_parts.append(f"timestamp > '{input.blk_single_date()} 23:59:59'")
            
        # Value Filter
        val_filt = input.blk_val_filter()
        thresh = input.blk_threshold()
        if val_filt == "Above Threshold":
            where_parts.append(f"temperature > {thresh}")
        elif val_filt == "Below Threshold":
            where_parts.append(f"temperature < {thresh}")
            
        where_clause = " AND ".join(where_parts) if where_parts else "1=1"
        constructed_where_clause.set(where_clause)
        
        # Execute Count Query
        try:
            target_table = f"{PROJECT_ID}.{DATASET_ID}.master_data_view_v2" # Adjust table name if needed
            q = f"SELECT COUNT(*) as match_count FROM `{target_table}` WHERE {where_clause}"
            df = client.query(q).to_dataframe()
            verify_match_count.set(int(df['match_count'].iloc[0]))
        except Exception as e:
            verify_match_count.set(f"Error: {e}")

    # --- 3. Render Verification Results ---
    @output
    @render.ui
    def blk_verify_results_ui():
        count = verify_match_count.get()
        if count is None: return ui.HTML("")
        
        if isinstance(count, str) and count.startswith("Error"):
            return ui.p(count, class_="text-danger")
            
        return ui.div(
            ui.h4(f"📊 Match Found: {count:,} records", class_="text-success"),
            ui.p(f"These records will be set to: '{input.blk_new_status()}'")
        )
        
    @output
    @render.ui
    def blk_execute_btn_ui():
        count = verify_match_count.get()
        if input.blk_confirm_check() and isinstance(count, int) and count > 0:
            return ui.input_action_button("blk_execute_btn", "⚠️ EXECUTE BULK UPDATE", class_="btn-danger w-100")
        return ui.HTML("")

    # --- 4. Execute the Bulk Update ---
    @reactive.Effect
    @reactive.event(input.blk_execute_btn)
    def execute_bulk_update():
        where_clause = constructed_where_clause.get()
        new_status = input.blk_new_status()
        
        if not where_clause or client is None: return
        
        target_table = f"{PROJECT_ID}.{DATASET_ID}.master_data_view_v2"
        update_q = f"UPDATE `{target_table}` SET approval_status = '{new_status}' WHERE {where_clause}"
        
        try:
            client.query(update_q).result()
            ui.notification_show("Bulk update successful!", type="success")
            verify_match_count.set(None) 
            ui.update_checkbox("blk_confirm_check", value=False)
        except Exception as e:
            ui.notification_show(f"Update failed: {e}", type="error")
            
    # --- TAB 4: PROJECT MASTER LOGIC ---
    @output
    @render.data_frame
    def project_master_df():
        if client is None: return pd.DataFrame()
        directory_q = f"""
            SELECT Project as `Project ID`, ProjectName as `Friendly Name`, ProjectStatus as `Operational Phase`, 
                   Date_Freezedown as `Freezedown Date`, Date_Maintenance as `Maintenance Date`, Date_EndFreeze as `End Freeze Date`,
                   City, Timezone 
            FROM `{PROJECT_REGISTRY_TABLE}` 
            WHERE Project IS NOT NULL AND TRIM(CAST(Project AS STRING)) != ''
            ORDER BY Project ASC
        """
        return render.DataGrid(client.query(directory_q).to_dataframe())

    # --- TAB 5: PIPE MAPPER LOGIC (Interactive Image Click) ---
    mapped_pipes_df = reactive.Value(pd.DataFrame(columns=['Location', 'Map_X', 'Map_Y']))
    
    @output
    @render.ui
    def mapper_controls_ui():
        AS_BUILT_DIR = "as_builts"
        available_images = ["(None)"]
        if os.path.exists(AS_BUILT_DIR):
            available_images += sorted([f for f in os.listdir(AS_BUILT_DIR) if f.lower().endswith(('.png', '.jpg', '.jpeg'))])
        
        reg_df = get_full_registry()
        all_projects = ["(None)"] + sorted(reg_df['Project'].dropna().unique().tolist())
        
        return ui.div(
            ui.input_select("mapper_img", "1. Select As-Built Image:", available_images),
            ui.input_select("mapper_proj", "2. Link to Project Database:", all_projects),
            ui.input_text("mapper_manual_loc", "3. Location Name (Manual Entry):")
        )

    @output
    @render.ui
    def mapper_image_ui():
        img_val = input.mapper_img() if hasattr(input, 'mapper_img') else "(None)"
        if img_val == "(None)": return ui.p("Please select an image on the right.")
        
        img_path = os.path.join("as_builts", img_val)
        
        # In Shiny, we use ui.output_image with click=True to capture X/Y pixel coordinates natively
        return ui.div(
            ui.p(f"👆 Click on the map to log coordinates."),
            ui.output_image("site_map_image", click=True)
        )
        
    @output
    @render.image
    def site_map_image():
        img_val = input.mapper_img()
        img_path = os.path.join("as_builts", img_val)
        return {"src": img_path, "width": "100%", "style": "cursor: crosshair;"}

    @reactive.Effect
    @reactive.event(input.site_map_image_click)
    def handle_map_click():
        click_data = input.site_map_image_click()
        if click_data:
            loc_name = input.mapper_manual_loc().upper().strip()
            if loc_name:
                current_df = mapped_pipes_df.get()
                new_row = pd.DataFrame({'Location': [loc_name], 'Map_X': [int(click_data['x'])], 'Map_Y': [int(click_data['y'])]})
                mapped_pipes_df.set(pd.concat([current_df, new_row], ignore_index=True))

    @output
    @render.data_frame
    def mapper_table_df():
        return render.DataGrid(mapped_pipes_df.get())

    @reactive.Effect
    @reactive.event(input.mapper_clear_btn)
    def clear_mapper_data():
        mapped_pipes_df.set(pd.DataFrame(columns=['Location', 'Map_X', 'Map_Y']))
