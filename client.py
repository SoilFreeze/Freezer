from shiny import App, render, reactive, ui
from shinywidgets import output_widget, render_plotly
import pandas as pd
import os

# Import your decoupled backend logic
from app.pages.summary import get_summary_data
from app.pages.depth import generate_depth_figures
from app.data.processor import get_universal_portal_data, get_bq_client
from app.components.charts import build_high_speed_graph
from app.components.charts import build_cropped_site_map

from app.utils.config import PROJECT_REGISTRY_TABLE

# Define a generous maximum number of charts to support per project
MAX_CHARTS = 25

# ===============================================================
# 1. SHINY UI DEFINITION
# ===============================================================
app_sidebar = ui.sidebar(
    ui.h4("Configuration"),
    ui.input_text("job_number", "Job Number:", placeholder="e.g., 2527"),
    ui.p("Enter your assigned Job Number to view project telemetry.", style="font-size: 0.9em; color: gray;")
)

app_ui = ui.page_sidebar(
    app_sidebar, 
    ui.output_ui("dynamic_global_header"),
    ui.navset_card_underline(
        ui.nav_panel(
            "🏠 Summary", 
            ui.h3("🌐 24 hour Thermal Summary", style="margin-top: 10px; margin-bottom: 25px; color: #2c3e50; font-weight: 500;"),
            ui.output_ui("dynamic_summary_cards")
        ),
        ui.nav_panel("📈 Timeline Analysis", ui.output_ui("dynamic_timeline_ui")),
        ui.nav_panel("📏 Depth Profile", ui.output_ui("dynamic_depth_ui")),
        ui.nav_panel("🗺️ As Built", ui.output_ui("dynamic_as_builts")),
        id="main_tabs"
    ),
    title="SoilFreeze Client Portal",
    fillable=False 
)

# ===============================================================
# 2. SHINY SERVER LOGIC
# ===============================================================
def server(input, output, session):
    
    # --- FIX 1: Safely grab the input without crashing the server ---
    @reactive.Calc
    def current_job():
        # Only return the value if the text box has been populated
        return input.job_number().strip() if input.job_number() else ""
    
    @reactive.Calc
    def project_timezone():
        job = current_job()
        if not job: 
            return "US/Pacific"
            
        client = get_bq_client()
        if client is None: 
            return "US/Pacific"
            
        try:
            # Query the registry to find the specific timezone for this project
            q = f"SELECT Timezone FROM `{PROJECT_REGISTRY_TABLE}` WHERE CAST(Project AS STRING) LIKE '{job}%' LIMIT 1"
            df = client.query(q).to_dataframe()
            if not df.empty and pd.notna(df['Timezone'].iloc[0]):
                tz = str(df['Timezone'].iloc[0]).strip()
                if tz: 
                    return tz
        except Exception:
            pass
            
        return "US/Pacific" # Safe fallback
        
    # --- GLOBAL HEADER ---
    @output
    @render.ui
    def dynamic_global_header():
        job = current_job()
        if not job:
            return ui.h2("🌐 Global Active Project Summary")
            
        client = get_bq_client()
        
        # Unpack 5 values and pass approved_only=True
        active_projs, _, tel_df, appr_df, err = get_summary_data(client, selected_project=job, show_archived=False, approved_only=True)
        
        # Fetch dynamic timezone
        proj_tz = project_timezone()
        
        if err or active_projs is None or active_projs.empty:
            return ui.h1(f"📊 Project: {job}")
            
        proj_row = active_projs.iloc[0]
        proj_name = proj_row['ProjectName'] if pd.notnull(proj_row['ProjectName']) else job
        f_date = proj_row.get('Date_Freezedown')
        
        freeze_html = ""
        if pd.notnull(f_date) and str(f_date).strip() != "":
            try:
                f_date_dt = pd.to_datetime(f_date)
                if f_date_dt.tzinfo is not None:
                    f_date_dt = f_date_dt.tz_localize(None)
                
                now_dt = pd.Timestamp.now().tz_localize(None)
                days = (now_dt - f_date_dt).days
                f_date_str = f_date_dt.strftime('%B %d, %Y')
                
                freeze_html = f"""
                <div style="display: flex; justify-content: space-between; align-items: flex-end; margin-top: 30px;">
                    <h2 style="margin: 0; color: #2c3e50; font-weight: 500;">🗓️ Day {max(0, days)} of Freezedown</h2>
                    <span style="color: #495057;"><b>Freeze Start Date:</b> {f_date_str}</span>
                </div>
                """
            except Exception:
                pass
                
        latest_str = "Unknown"
        last_appr_ts = appr_df['last_approved_ts'].max() if (appr_df is not None and not appr_df.empty) else pd.NaT
        
        if pd.notnull(last_appr_ts):
            if last_appr_ts.tzinfo is None:
                last_appr_ts = last_appr_ts.tz_localize('UTC')
            # Apply dynamic timezone here
            latest_str = last_appr_ts.tz_convert(proj_tz).strftime('%B %d, %Y at %I:%M %p')
        
        return ui.HTML(f"""
            <h1 style="display: flex; align-items: center; gap: 10px; color: #343a40; font-weight: 700; font-size: 2.8rem; margin-bottom: 25px;">
                📊 {proj_name}
            </h1>
            <div style="background-color: #e8f4fd; border: 1px solid #b6d4fe; padding: 18px 20px; border-radius: 5px; margin-bottom: 25px; display: flex; align-items: center; gap: 10px;">
                <span style="background-color: #63d297; color: white; border-radius: 3px; padding: 2px 6px; font-size: 0.8em;">✅</span>
                <span style="color: #004085;"><b>Official Data Status:</b> Records approved through <b>{latest_str}</b>.</span>
            </div>
            {freeze_html}
            <hr style="margin-top: 15px; margin-bottom: 25px; border-top: 1px solid #dee2e6;">
        """)

    # --- TAB 1: SUMMARY ENGINE ---
    @output
    @render.ui
    def dynamic_summary_cards():
        job = current_job()
        if not job:
            return ui.p("Please provide a job number to view the summary.", style="color: gray; font-style: italic;")
            
        client = get_bq_client()
        
        # FIX: Unpack 5 values here to match the updated get_summary_data signature
        _, _, tel_df, _, err = get_summary_data(client, selected_project=job, show_archived=False, approved_only=True)
        
        if err: return ui.div(f"Error loading summary: {err}", style="color: red;")
        if tel_df is None or tel_df.empty: return ui.div(f"No recent telemetry found for job: {job} in the last 48 hours.", style="color: orange;")
            
        is_amb_col = tel_df['Location'].astype(str).str.upper() == 'AMBIENT'
        is_tp_col = tel_df['Depth'].notnull() & (tel_df['Depth'].astype(str).str.strip() != '') & ~is_amb_col
        is_s_col = (tel_df['Bank'].astype(str).str.startswith('S') | tel_df['Location'].astype(str).str.startswith('S')) & ~is_amb_col & ~is_tp_col
        is_r_col = (tel_df['Bank'].astype(str).str.startswith('R') | tel_df['Location'].astype(str).str.startswith('R')) & ~is_amb_col & ~is_tp_col
        
        groups = [
            ("Supply (S)", tel_df[is_s_col]), 
            ("Return (R)", tel_df[is_r_col]), 
            ("Temp Pipes (TP)", tel_df[is_tp_col]),
            ("Ambient", tel_df[is_amb_col])
        ]
        
        cols = []
        for title, g_df in groups:
            if g_df.empty or g_df['latest_temp'].isnull().all():
                cols.append(ui.HTML(f"""
                    <div style="font-family: sans-serif;">
                        <h3 style="margin-bottom: 15px; color: #343a40; font-weight: 500;">{title}</h3>
                        <p style="color: #868e96; font-size: 0.95em;">No data available.</p>
                    </div>
                """))
                continue
                
            latest_val = g_df['latest_temp'].mean()
            high_24 = g_df['max_24h'].max()
            low_24 = g_df['min_24h'].min()
            
            l_str = f"{latest_val:.1f}°F" if pd.notnull(latest_val) else "N/A"
            h_str = f"{high_24:.1f}°F" if pd.notnull(high_24) else "N/A"
            lo_str = f"{low_24:.1f}°F" if pd.notnull(low_24) else "N/A"
            
            cols.append(ui.HTML(f"""
            <div style="font-family: sans-serif;">
                <h3 style="margin-bottom: 20px; color: #343a40; font-weight: 500;">{title}</h3>
                <div style="color: #6c757d; font-size: 0.9rem; margin-bottom: 8px;">Avg (Latest)</div>
                <div style="font-size: 3rem; font-weight: 300; margin-bottom: 30px; color: #212529; line-height: 1;">{l_str}</div>
                <div style="font-size: 0.85rem; color: #868e96; display: flex; gap: 15px;">
                    <span><b>High (24h):</b> {h_str}</span>
                    <span><b>Low (24h):</b> {lo_str}</span>
                </div>
            </div>
            """))
            
        return ui.layout_column_wrap(*cols, width=1/4, gap="30px")

    # --- TAB 2: NATIVE TIMELINE CHARTS ---
    # Fetch database payload exactly once to feed all charts
    @reactive.Calc
    def shared_timeline_data():
        job = current_job()
        if not job: return None, None, []
        
        df = get_universal_portal_data(job, lookback_days=42, is_summary_page=False, show_masked=False, show_baddata=False, approved_only=True)
        client = get_bq_client()
        
        df_all_locs = pd.DataFrame()
        try:
            from app.utils.config import PROJECT_ID, DATASET_ID
            map_query = f"SELECT Project, Location, Map_X, Map_Y, Image_Name FROM `{PROJECT_ID}.{DATASET_ID}.TempPipeLoc` WHERE CAST(Project AS STRING) = '{job}'"
            df_all_locs = client.query(map_query).to_dataframe()
            if not df_all_locs.empty and 'Location' in df_all_locs.columns:
                df_all_locs['Location'] = df_all_locs['Location'].astype(str).str.strip().str.upper()
                df_all_locs['Project'] = df_all_locs['Project'].astype(str).str.strip()
        except: pass
        
        if df is not None and not df.empty:
            valid_locs = sorted([loc for loc in df['Location'].dropna().unique() if 'AMBIENT' not in str(loc).upper() and str(loc).strip().upper() != 'UNASSIGNED'])
            return df, df_all_locs, valid_locs
        return None, None, []

    @output
    @render.ui
    def dynamic_timeline_ui():
        df, _, valid_locs = shared_timeline_data()
        if df is None or not valid_locs:
            return ui.p("No timeline data found for this project.", style="color: orange;")

        ui_elements = []
        for i, loc in enumerate(valid_locs):
            ui_elements.append(
                ui.card(
                    ui.layout_columns(
                        ui.div(output_widget(f"timeline_chart_{i}")),
                        ui.div(output_widget(f"timeline_map_{i}")),
                        col_widths=[9, 3]
                    ),
                    style="margin-bottom: 20px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);"
                )
            )
        return ui.TagList(*ui_elements)

    # Factory Generator to bind charts to their respective native outputs dynamically
    for i in range(MAX_CHARTS):
        def make_timeline_chart(index):
            @output(id=f"timeline_chart_{index}")
            @render_plotly
            def _plot():
                df, map_df, locs = shared_timeline_data()
                if df is not None and index < len(locs):
                    loc = locs[index]
                    loc_data = df[df['Location'] == loc]
                    client = get_bq_client()
                    proj_tz = project_timezone()
                    
                    fig = build_high_speed_graph(
                        client=client, df=loc_data, title=f"Thermal Trends: {loc}",
                        start_view=df['timestamp'].min(), end_view=df['timestamp'].max(), 
                        active_refs=[(32.0, "Freezing")], unit_mode="Fahrenheit", unit_label="°F", 
                        display_tz=proj_tz, curve_id=f"{current_job()}-{loc}", show_elevation=False
                    )
                    return fig
                return None
            return _plot
            
        def make_timeline_map(index):
            @output(id=f"timeline_map_{index}")
            @render_plotly
            def _map():
                df, map_df, locs = shared_timeline_data()
                if df is not None and index < len(locs):
                    loc = locs[index]
                    loc_clean = str(loc).strip().upper()
                    if loc_clean.startswith('T') and not map_df.empty and 'Location' in map_df.columns:
                        if loc_clean in map_df['Location'].values:
                            as_built_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "as_builts"))
                            map_fig = build_cropped_site_map(current_job(), loc_clean, map_df, as_built_path)
                            return map_fig
                return None
            return _map
            
        make_timeline_chart(i)
        make_timeline_map(i)

    # --- TAB 3: DEPTH PROFILE ENGINE ---
    @reactive.Calc
    def shared_depth_data():
        job = current_job()
        if not job: return {}, ""
        
        # Fetch dynamic timezone
        proj_tz = project_timezone()
        
        figures_dict, chart_label, error_msg = generate_depth_figures(
            selected_project=job, unit_label="°F", display_tz=proj_tz, 
            orientation="vertical", lookback_weeks=5, show_masked=False, 
            show_baddata=False, unit_mode="Fahrenheit"
        )
        return figures_dict, chart_label

    @output
    @render.ui
    def dynamic_depth_ui():
        figures_dict, chart_label = shared_depth_data()
        if not figures_dict:
            return ui.p("No depth profile data found.", style="color: orange;")
        
        ui_elements = []
        for i, loc in enumerate(figures_dict.keys()):
            ui_elements.append(
                ui.card(
                    ui.card_header(f"📍 Temp vs {chart_label} - {loc}"),
                    output_widget(f"depth_plot_{i}"),
                    style="margin-bottom: 20px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);"
                )
            )
        return ui.TagList(*ui_elements)

    # Factory Generator for Depth Charts
    for i in range(MAX_CHARTS):
        def make_depth_renderer(index):
            @output(id=f"depth_plot_{index}")
            @render_plotly
            def _plot():
                figures_dict, _ = shared_depth_data()
                locs = list(figures_dict.keys())
                if index < len(locs):
                    return figures_dict[locs[index]]
                return None
            return _plot
        make_depth_renderer(i)

    # --- TAB 4: AS BUILTS ENGINE ---
    @output
    @render.ui
    def dynamic_as_builts():
        job = current_job()
        if not job: return ui.p("Please provide a job number to view as-builts.", style="color: gray; font-style: italic;")
            
        job_root = str(job).split('-')[0].strip()
        as_builts_dir = os.path.join(os.path.dirname(__file__), "as_builts")
        
        if not os.path.exists(as_builts_dir): return ui.div("As-builts directory not found.", style="color: orange;")
            
        found_images = sorted([
            f for f in os.listdir(as_builts_dir) 
            if f.startswith(job_root) and f.lower().endswith(('.png', '.jpg', '.jpeg'))
        ])
        
        if not found_images: return ui.div("ℹ️ Site plan is currently being processed or has not been assigned.", style="color: gray; font-style: italic;")
            
        image_elements = []
        for img_name in found_images:
            img_src = f"/as_builts/{img_name}"
            image_elements.append(
                ui.card(
                    ui.h5(img_name, style="text-align: center; margin-bottom: 15px;"),
                    ui.img(src=img_src, style="max-width: 100%; height: auto; border: 1px solid #ddd; border-radius: 5px;"),
                    style="margin-bottom: 30px; padding: 20px; align-items: center;"
                )
            )
            
        return ui.TagList(*image_elements)

# Route the local "as_builts" directory so the browser can natively load images
app = App(
    app_ui, 
    server, 
    static_assets={"/as_builts": os.path.join(os.path.dirname(__file__), "as_builts")}
)
