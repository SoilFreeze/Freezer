from shiny import App, ui, render, reactive
import pandas as pd
import time
import os
import re

# Internal imports
from app.utils import config
from app.data.processor import get_bq_client
from app.utils.config import PROJECT_ID, DATASET_ID

# Module Imports (The Shiny modules we just created)
from app.pages.admin import admin_ui, admin_server
from app.pages.processing import processing_ui, processing_server
from app.pages.sensors import sensors_ui, sensors_server
from app.pages.diagnostics import diagnostics_ui, diagnostics_server
from app.pages.summary import summary_ui, summary_server
from app.pages.time_vs_temp import time_vs_temp_ui, time_vs_temp_server

# =============================================================================
# 1. UI SETUP & SIDEBAR NAVIGATION
# =============================================================================
app_ui = ui.page_fluid(
    ui.panel_title("❄️ SoilFreeze Data Lab"),
    ui.layout_sidebar(
        ui.sidebar(
            ui.h3("❄️ SoilFreeze Lab"),
            ui.input_select(
                "nav_page", 
                "Navigation", 
                [
                    "Summary",              
                    "Time vs Temp",        
                    "Depth Charts", 
                    "Sensor Status",       
                    "Node Diagnostics", 
                    "Data Processing", 
                    "Admin Tools"
                ]
            ),
            ui.hr(),
            ui.output_ui("project_selector_ui"),
            ui.output_ui("data_pulse_ui"),
            ui.input_action_button("refresh_btn", "🔄 Refresh Data", class_="btn-primary w-100"),
            
            ui.hr(),
            ui.h4("👁️ Visibility Controls"),
            ui.input_switch("global_show_archived", "Show Archived Projects", False),
            ui.input_switch("global_show_ambient", "Show Ambient Temp", True),
            ui.input_switch("global_show_elevation", "Show Elevations", False),
            ui.input_switch("global_show_ref", "Show Theoretical Curves", True),
            ui.input_switch("global_show_map", "🗺️ Show As-Built Site Maps", True),
            ui.input_switch("global_show_baddata", "Show Bad Data", False),
            
            ui.hr(),
            ui.h4("⏳ Timeline Navigation"),
            ui.input_switch("full_data_toggle", "🌍 See Full Data (Since Freezedown)", False),
            ui.output_ui("timeline_slider_ui"),
            
            ui.hr(),
            ui.h4("🌡️ Units"),
            ui.input_radio_buttons(
                "unit_toggle", "Temperature Scale", 
                ["Fahrenheit", "Celsius"], 
                inline=True
            ),
            
            ui.hr(),
            ui.h4("📱 Display & Time"),
            ui.input_select(
                "tz_picker", "Timezone Display", 
                ["UTC", "Local (US/Eastern)", "Local (US/Pacific)"], 
                selected="Local (US/Pacific)"
            ),
            
            ui.hr(),
            ui.h4("📏 Reference Lines"),
            ui.input_checkbox("ref_freezing", "Freezing (32°F)", True),
            ui.input_checkbox("ref_type_b", "Type B (26.6°F)", False),
            ui.input_checkbox("ref_type_a", "Type A (10.2°F)", False),
            
            width=350
        ),
        ui.output_ui("page_content")
    )
)

# =============================================================================
# 2. SERVER LOGIC & REACTIVITY
# =============================================================================
def server(input, output, session):
    client = get_bq_client()

    # --- REACTIVE STATE VARIABLES ---
    project_metadata = reactive.Value({})
    proj_df_state = reactive.Value(pd.DataFrame()) # <-- NEW: Safely holds the database query
    authenticated = reactive.Value(False)

    @reactive.Calc
    def current_unit_label():
        return "°F" if input.unit_toggle() == "Fahrenheit" else "°C"

    @reactive.Calc
    def current_display_tz():
        tz_lookup = {"UTC": "UTC", "Local (US/Eastern)": "US/Eastern", "Local (US/Pacific)": "US/Pacific"}
        return tz_lookup.get(input.tz_picker(), "UTC")
        
    @reactive.Calc
        def current_lookback_days():
            if hasattr(input, "full_data_toggle") and input.full_data_toggle():
                p_meta = project_metadata.get()
                real_f_date = p_meta.get('Date_Freezedown')
                parsed_date = pd.to_datetime(real_f_date, errors='coerce')
                
                if pd.notnull(parsed_date):
                    if parsed_date.tzinfo is not None:
                        parsed_date = parsed_date.tz_localize(None)
                    days_since = (pd.Timestamp.now() - parsed_date).days
                    return max(7, days_since + 2)
                return 90 # Fallback if no date is set
            else:
                weeks = input.lookback_weeks() if hasattr(input, "lookback_weeks") else 5
                return weeks * 7
    
        @reactive.Calc
        def current_active_refs():
            refs = []
            if input.ref_freezing(): refs.append((32.0, "Freezing"))
            if input.ref_type_b(): refs.append((26.6, "Type B"))
            if input.ref_type_a(): refs.append((10.2, "Type A"))
            return tuple(refs)
            
    # --- DYNAMIC PROJECT SELECTOR ---
    @output
    @render.ui
    def project_selector_ui():
        if client is None:
            return ui.p("⚠️ Registry Link Offline", style="color: red;")
        
        status_filter = "" if input.global_show_archived() else "AND UPPER(TRIM(CAST(ShowActive AS STRING))) IN ('TRUE', 'YES', '1')"
        proj_q = f"""
            SELECT CAST(Project AS STRING) as Project, ProjectName, Timezone, ProjectStatus, Date_Freezedown, orientation
            FROM `{config.PROJECT_REGISTRY_TABLE}` 
            WHERE Project IS NOT NULL AND TRIM(CAST(Project AS STRING)) != ''
            {status_filter}
        """
        try:
            proj_df = client.query(proj_q).to_dataframe()
            proj_list = sorted([str(p).strip() for p in proj_df['Project'].unique() if p and str(p).strip().lower() not in ['none', 'nan', 'null', '']])
            
            # FIX: Store full dataframe in the reactive value instead of session.userData
            proj_df_state.set(proj_df)
            
            return ui.input_select("selected_project", "🎯 Active Project", ["All Projects"] + proj_list)
        except Exception as e:
            return ui.p(f"⚠️ Error: {e}", style="color: red;")

    # --- METADATA EXTRACTION LOGIC ---
    @reactive.Effect
    def update_project_metadata():
        """Updates the project_metadata dictionary whenever the project dropdown changes."""
        if hasattr(input, "selected_project"):
            proj = input.selected_project()
            df = proj_df_state.get()
            
            if proj and proj != "All Projects" and not df.empty:
                meta_row = df[df['Project'] == proj]
                if not meta_row.empty:
                    project_metadata.set(meta_row.iloc[0].to_dict())
                else:
                    project_metadata.set({})
            else:
                project_metadata.set({})

    # --- DATA AGE PULSE ---
    @output
    @render.ui
    def data_pulse_ui():
        if client is None or not hasattr(input, "selected_project"):
            return ui.HTML("")
            
        proj = input.selected_project()
        pulse_q = f"SELECT FORMAT_TIMESTAMP('%m/%d/%Y %H:%M UTC', MAX(timestamp)) as last_sync FROM `{config.MASTER_VIEW}`"
        scope_label = "Last Data"

        if proj != "All Projects":
            job_num = proj.split('-')[0].strip()
            phase_sql = " AND Phase = '1' " if "Phase 1" in proj else " AND Phase = '2' " if "Phase 2" in proj or "Phase2" in proj else ""
            pulse_q += f" WHERE Project LIKE '{job_num}%' {phase_sql}"
            scope_label = f"Job {job_num} Age"
            
        try:
            pulse_df = client.query(pulse_q).to_dataframe()
            if not pulse_df.empty and pd.notna(pulse_df['last_sync'].iloc[0]):
                last_sync_str = str(pulse_df['last_sync'].iloc[0])
                elapsed_mins = int((pd.Timestamp.now(tz='UTC') - pd.to_datetime(last_sync_str, utc=True)).total_seconds() / 60)
                
                if elapsed_mins <= 60:
                    status = f"🟢 **Live** ({elapsed_mins}m ago)"
                elif elapsed_mins <= 180:
                    status = f"🟠 **Delayed** ({elapsed_mins}m ago)"
                else:
                    status = f"🔴 **Stale** ({elapsed_mins // 60}h ago)"
                    
                return ui.markdown(f"**{scope_label}:** {status}\n\n<small>Last Entry: `{last_sync_str}`</small>")
            return ui.markdown(f"**{scope_label}:** ⚠️ No Recent Sync")
        except:
            return ui.markdown("⚠️ Pulse tracking suspended.")

    # --- TIMELINE SLIDER ---
    @output
    @render.ui
    def timeline_slider_ui():
        if input.full_data_toggle():
            return ui.markdown("<small><i>Calculating days since freezedown...</i></small>")
        return ui.input_slider("lookback_weeks", "Select History Window (Weeks)", min=1, max=12, value=5, step=1)

    # --- AUTHENTICATION TRIGGER ---
    @reactive.Effect
    @reactive.event(input.unlock_btn)
    def handle_login():
        if input.admin_pwd() == "freeze123":
            authenticated.set(True)

    # --- MAIN CONTENT ROUTER ---
    @output
    @render.ui
    def page_content():
        page = input.nav_page()
        proj = input.selected_project() if hasattr(input, "selected_project") else "All Projects"
        
        # 1. Global / Admin Pages
        if page in ["Summary", "Data Processing", "Admin Tools"]:
            if page == "Summary":
                return ui.div(summary_ui("summary_module")) # <-- Inject here
            
            if not authenticated.get():
                return ui.div(
                    ui.h3("🔐 Restricted Admin Access"),
                    ui.input_password("admin_pwd", "Enter Admin Password"),
                    ui.input_action_button("unlock_btn", "Unlock Dashboard", class_="btn-warning")
                )
            
            # Module Injectors
            if page == "Data Processing":
                return ui.div(processing_ui("processing_module"))
            elif page == "Admin Tools":
                return ui.div(admin_ui("admin_module"))

        # 2. Project-Specific Pages
        if proj == "All Projects":
            return ui.div(ui.h4(f"👈 Please select a specific project from the sidebar to view the {page} dashboard.", class_="text-info"))

        if page == "Time vs Temp":
            return ui.div(time_vs_temp_ui("time_vs_temp_module"))
        elif page == "Depth Charts":
            return ui.div(ui.h3("Depth Charts Placeholder"))
        elif page == "Sensor Status":
            return ui.div(sensors_ui("sensors_module"))
        elif page == "Node Diagnostics":
            return ui.div(diagnostics_ui("diagnostics_module"))

    # =========================================================================
    # 3. INITIALIZE MODULE SERVERS
    # =========================================================================
    # These must be called at the root of the server function, outside of @render.ui
    
    admin_server("admin_module", 
                 client=client, 
                 selected_project=input.selected_project, 
                 display_tz=current_display_tz)
                 
    processing_server("processing_module", 
                      client=client, 
                      selected_project=input.selected_project)
                      
    sensors_server("sensors_module", 
                   client=client, 
                   selected_project=input.selected_project, 
                   project_metadata=project_metadata, 
                   unit_mode=input.unit_toggle, 
                   unit_label=current_unit_label, 
                   display_tz=current_display_tz)
                   
    diagnostics_server("diagnostics_module", 
                       client=client, 
                       selected_project=input.selected_project, 
                       display_tz=current_display_tz, 
                       unit_mode=input.unit_toggle, 
                       unit_label=current_unit_label, 
                       global_show_archived=input.global_show_archived)
    
    summary_server("summary_module", 
                   client=client, 
                   selected_project=input.selected_project, 
                   global_show_archived=input.global_show_archived,
                   unit_mode=input.unit_toggle, 
                   unit_label=current_unit_label, 
                   display_tz=current_display_tz,
                   global_show_ambient=input.global_show_ambient)
    
# =============================================================================
# GLOBAL APP EXPORT
# =============================================================================
app = App(app_ui, server)
