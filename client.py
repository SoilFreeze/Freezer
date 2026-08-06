from shiny import App, render, reactive, ui
import pandas as pd
import os
import html # <--- NEW: Used to safely escape the HTML for the iframe

# Import your decoupled backend logic
from app.pages.summary import get_summary_data
from app.pages.depth import generate_depth_figures
from app.data.processor import get_universal_portal_data, get_bq_client
from app.components.charts import build_high_speed_graph
from app.components.charts import build_cropped_site_map


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
        ui.nav_panel("📈 Timeline Analysis", ui.output_ui("dynamic_timeline_charts")),
        ui.nav_panel("📏 Depth Profile", ui.output_ui("dynamic_depth_charts")),
        ui.nav_panel("🗺️ As Built", ui.output_ui("dynamic_as_builts")),
        id="main_tabs"
    ),
    
    title="SoilFreeze Client Portal",
    fillable=False  # <--- THE FIX: This stops the weird scrolling box behavior!
)

# ===============================================================
# 2. SHINY SERVER LOGIC
# ===============================================================
def server(input, output, session):
    
    @reactive.Calc
    def current_job():
        return input.job_number().strip()

    # --- GLOBAL HEADER ---
    @render.ui
    def dynamic_global_header():
        job = current_job()
        if not job:
            return ui.h2("🌐 Global Active Project Summary")
            
        active_projs, _, tel_df, err = get_summary_data(selected_project=job, show_archived_opt=False)
        
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
        if tel_df is not None and not tel_df.empty:
            max_ts = tel_df['latest_ts'].max()
            if pd.notnull(max_ts):
                if max_ts.tzinfo is None:
                    max_ts = max_ts.tz_localize('UTC')
                latest_str = max_ts.tz_convert('US/Pacific').strftime('%B %d, %Y at %I:%M %p')
        
        return ui.HTML(f"""
            <h1 style="display: flex; align-items: center; gap: 10px; color: #343a40; font-weight: 700; font-size: 2.8rem; margin-bottom: 25px;">
                📊 {proj_name}
            </h1>
            
            <div style="background-color: #f0f8ff; border: 1px solid #cce5ff; padding: 18px 20px; border-radius: 5px; margin-bottom: 25px; display: flex; align-items: center; gap: 10px;">
                <span style="background-color: #28a745; color: white; border-radius: 3px; padding: 2px 6px; font-size: 0.8em;">✅</span>
                <span style="color: #004085;"><b>Official Data Status:</b> Records approved through <b>{latest_str}</b>.</span>
            </div>
            
            {freeze_html}
            <hr style="margin-top: 15px; margin-bottom: 25px; border-top: 1px solid #dee2e6;">
        """)

    # --- TAB 1: SUMMARY ENGINE ---
    @render.ui
    def dynamic_summary_cards():
        job = current_job()
        if not job:
            return ui.p("Please provide a job number to view the summary.", style="color: gray; font-style: italic;")
            
        _, _, tel_df, err = get_summary_data(selected_project=job, show_archived_opt=False)
        
        if err:
            return ui.div(f"Error loading summary: {err}", style="color: red;")
        if tel_df is None or tel_df.empty:
            return ui.div(f"No recent telemetry found for job: {job} in the last 48 hours.", style="color: orange;")
            
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
            
            card_html = f"""
            <div style="font-family: sans-serif;">
                <h3 style="margin-bottom: 20px; color: #343a40; font-weight: 500;">{title}</h3>
                <div style="color: #6c757d; font-size: 0.9rem; margin-bottom: 8px;">Avg (Latest)</div>
                <div style="font-size: 3rem; font-weight: 300; margin-bottom: 30px; color: #212529; line-height: 1;">{l_str}</div>
                <div style="font-size: 0.85rem; color: #868e96; display: flex; gap: 15px;">
                    <span><b>High (24h):</b> {h_str}</span>
                    <span><b>Low (24h):</b> {lo_str}</span>
                </div>
            </div>
            """
            cols.append(ui.HTML(card_html))
            
        return ui.layout_column_wrap(*cols, width=1/4, gap="30px")

    # --- TAB 2: TIMELINE ANALYSIS ENGINE ---
    @render.ui
    def dynamic_timeline_charts():
        job = current_job()
        if not job:
            return ui.p("Please provide a job number.", style="color: gray; font-style: italic;")
            
        df = get_universal_portal_data(job, lookback_days=42, is_summary_page=False, show_masked=False, show_baddata=False)
        if df is None or df.empty:
            return ui.p("No timeline data found for this project.", style="color: orange;")
            
        client = get_bq_client()
        unique_locations = [loc for loc in df['Location'].dropna().unique() if 'AMBIENT' not in str(loc).upper() and str(loc).strip().upper() != 'UNASSIGNED']
        
        if not unique_locations:
            return ui.p("No assigned locations available to chart.", style="color: gray;")
            
        start_date = df['timestamp'].min()
        end_date = df['timestamp'].max()
        job_root = str(job).split('-')[0].strip()
        
        # --- Fetch map data robustly ---
        df_all_locs = pd.DataFrame()
        try:
            import app.utils.config as cfg
            map_query = f"""
                SELECT CAST(Project AS STRING) as Project, CAST(Location AS STRING) as Location, Map_X, Map_Y, Image_Name 
                FROM `{cfg.PROJECT_ID}.{cfg.DATASET_ID}.TempPipeLoc` 
                WHERE CAST(Project AS STRING) = '{job_root}'
            """
            df_all_locs = client.query(map_query).to_dataframe()
            
            if not df_all_locs.empty and 'Location' in df_all_locs.columns:
                # Force uppercase and strip spaces for bulletproof matching
                df_all_locs['Location'] = df_all_locs['Location'].astype(str).str.strip().str.upper()
                df_all_locs['Project'] = df_all_locs['Project'].astype(str).str.strip()
        except Exception as e:
            print(f"Warning: Map data fetch failed: {e}")
            
        # Guarantee absolute path to the as_builts folder for the cloud environment
        as_built_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "as_builts"))
        
        ui_elements = []
        for loc in sorted(unique_locations):
            loc_data = df[df['Location'] == loc]
            if loc_data.empty: continue
            
            fig = build_high_speed_graph(
                client=client,  
                df=loc_data, 
                title=f"Thermal Trends: {loc}",
                start_view=start_date, 
                end_view=end_date, 
                active_refs=[(32.0, "Freezing")],
                unit_mode="Fahrenheit",
                unit_label="°F",
                display_tz="US/Pacific",
                curve_id=f"{job}-{loc}",
                show_elevation=False
            )
            
            if fig:
                # 1. Prepare Main Chart
                plot_html = fig.to_html(full_html=True, include_plotlyjs="cdn")
                escaped_html = html.escape(plot_html)
                main_chart_iframe = f'<iframe srcdoc="{escaped_html}" width="100%" height="800px" style="border:none; overflow:hidden;"></iframe>'
                
                content_html = main_chart_iframe
                
                # 2. Check Map Eligibility
                has_map = False
                loc_clean = str(loc).strip().upper()
                
                if not df_all_locs.empty and 'Location' in df_all_locs.columns:
                    has_map = loc_clean in df_all_locs['Location'].values
                
                if has_map:
                    map_fig = build_cropped_site_map(job_root, loc_clean, df_all_locs, as_built_path)
                    
                    if map_fig:
                        map_html = map_fig.to_html(full_html=True, include_plotlyjs="cdn")
                        escaped_map_html = html.escape(map_html)
                        map_iframe = f'<iframe srcdoc="{escaped_map_html}" width="100%" height="800px" style="border:none; overflow:hidden;"></iframe>'
                        
                        # --- BULLETPROOF RAW CSS FLEXBOX ---
                        content_html = f"""
                        <div style="display: flex; flex-wrap: wrap; gap: 15px; width: 100%;">
                            <div style="flex: 3; min-width: 600px;">
                                {main_chart_iframe}
                            </div>
                            <div style="flex: 1; min-width: 250px;">
                                {map_iframe}
                            </div>
                        </div>
                        """
                    else:
                        # VISUAL DEBUGGER: If it fails to build the map
                        content_html = f"""
                        <div style="display: flex; flex-wrap: wrap; gap: 15px; width: 100%;">
                            <div style="flex: 3; min-width: 600px;">
                                {main_chart_iframe}
                            </div>
                            <div style="flex: 1; min-width: 250px; display: flex; align-items: center; justify-content: center; background-color: #f8d7da; color: #721c24; border-radius: 5px; padding: 20px; text-align: center;">
                                <div>
                                    <b>⚠️ Map Image Error</b><br><br>
                                    Coordinates found for {loc_clean}, but image could not be loaded from:<br>
                                    <small style="word-break: break-all;">{as_built_path}</small>
                                </div>
                            </div>
                        </div>
                        """
                else:
                    # VISUAL DEBUGGER 2: If it didn't find a map in BigQuery
                    bq_locs = df_all_locs['Location'].tolist() if not df_all_locs.empty and 'Location' in df_all_locs.columns else "Empty or Failed Fetch"
                    
                    content_html = f"""
                    <div style="display: flex; flex-wrap: wrap; gap: 15px; width: 100%;">
                        <div style="flex: 3; min-width: 600px;">
                            {main_chart_iframe}
                        </div>
                        <div style="flex: 1; min-width: 250px; display: flex; align-items: center; justify-content: center; background-color: #cce5ff; color: #004085; border-radius: 5px; padding: 20px; text-align: center;">
                            <div>
                                <b>ℹ️ Matching Debugger</b><br><br>
                                Chart is looking for: <b>{loc_clean}</b><br><br>
                                BigQuery found these locations for {job_root}:<br>
                                <small style="word-break: break-all;">{bq_locs}</small>
                            </div>
                        </div>
                    </div>
                    """
                
                ui_elements.append(
                    ui.card(
                        ui.HTML(content_html),
                        style="margin-bottom: 20px;"
                    )
                )
                
        return ui.TagList(*ui_elements)

    # --- TAB 3: DEPTH PROFILE ENGINE ---
    @render.ui
    def dynamic_depth_charts():
        job = current_job()
        if not job:
            return ui.p("Please provide a job number to view profiles.", style="color: gray; font-style: italic;")
            
        figures_dict, chart_label, error_msg = generate_depth_figures(
            selected_project=job, 
            unit_label="°F", 
            display_tz="US/Pacific", 
            orientation="vertical",
            lookback_weeks=5,
            show_masked=False,
            show_baddata=False,
            unit_mode="Fahrenheit"
        )
        
        if error_msg:
            return ui.div(error_msg, style="color: orange;")
            
        ui_elements = []
        for loc, fig in figures_dict.items():
            
            plot_html = fig.to_html(full_html=True, include_plotlyjs="cdn")
            escaped_html = html.escape(plot_html)
            iframe_tag = f'<iframe srcdoc="{escaped_html}" width="100%" height="800px" style="border:none; overflow:hidden;"></iframe>'
            
            ui_elements.append(
                ui.card(
                    ui.card_header(f"📍 Temp vs {chart_label} - {loc}"),
                    ui.HTML(iframe_tag),
                    style="margin-bottom: 20px;"
                )
            )
            
        return ui.TagList(*ui_elements)

    # --- TAB 4: AS BUILTS ENGINE ---
    @render.ui
    def dynamic_as_builts():
        job = current_job()
        if not job:
            return ui.p("Please provide a job number to view as-builts.", style="color: gray; font-style: italic;")
            
        job_root = str(job).split('-')[0].strip()
        
        # Use an absolute path to ensure Posit Connect Cloud finds the folder
        as_builts_dir = os.path.join(os.path.dirname(__file__), "as_builts")
        
        if not os.path.exists(as_builts_dir):
            return ui.div("As-builts directory not found.", style="color: orange;")
            
        found_images = sorted([
            f for f in os.listdir(as_builts_dir) 
            if f.startswith(job_root) and f.lower().endswith(('.png', '.jpg', '.jpeg'))
        ])
        
        if not found_images:
            return ui.div("ℹ️ Site plan is currently being processed or has not been assigned.", style="color: gray; font-style: italic;")
            
        image_elements = []
        for img_name in found_images:
            # Map the web source URL to the static asset folder we define below
            img_src = f"/as_builts/{img_name}"
            
            image_elements.append(
                ui.card(
                    ui.h5(img_name, style="text-align: center; margin-bottom: 15px;"),
                    ui.img(src=img_src, style="max-width: 100%; height: auto; border: 1px solid #ddd; border-radius: 5px;"),
                    style="margin-bottom: 30px; padding: 20px; align-items: center;"
                )
            )
            
        return ui.TagList(*image_elements)

# Mount the local "as_builts" directory to the "/as_builts" web path so the browser can see the images
app = App(
    app_ui, 
    server, 
    static_assets={"/as_builts": os.path.join(os.path.dirname(__file__), "as_builts")}
)
