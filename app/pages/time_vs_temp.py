from shiny import ui, render, reactive, module
import plotly.graph_objects as go
import pandas as pd
import os
import re
import base64

# Internal Imports
from app.utils.config import PROJECT_ID, DATASET_ID
from app.data.processor import get_universal_portal_data, apply_sanity_filter
from app.components.charts import build_high_speed_graph, build_cropped_site_map

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================
def natural_sort_key(text):
    return [int(c) if c.isdigit() else str(c).lower() for c in re.split(r'(\d+)', str(text))]

def get_image_base64_from_file(img_path):
    with open(img_path, "rb") as image_file:
        encoded_string = base64.b64encode(image_file.read()).decode("utf-8")
    ext = os.path.splitext(img_path)[1].lower().replace('.', '')
    if ext == 'jpg': ext = 'jpeg'
    return f"data:image/{ext};base64,{encoded_string}"

def plotly_to_base64_img(fig):
    """Converts a Plotly figure to a base64 string for static HTML rendering."""
    if not fig:
        return ""
    try:
        # Requires 'kaleido' to be installed in the environment
        img_bytes = fig.to_image(format="png", engine="kaleido")
        encoded = base64.b64encode(img_bytes).decode("utf-8")
        return f"data:image/png;base64,{encoded}"
    except Exception as e:
        print(f"Kaleido rendering error: {e}")
        return ""

# =============================================================================
# SHINY UI MODULE
# =============================================================================
@module.ui
def time_vs_temp_ui():
    """Defines the visual layout for the static image charts."""
    return ui.div(
        ui.h2("📈 Time vs Temperature Tracking (Static View)"),
        ui.navset_card_tab(
            ui.nav_panel("Telemetry Charts",
                ui.output_ui("system_filter_ui"), 
                ui.hr(),
                
                # A single output target for ALL dynamically generated static charts
                ui.output_ui("stacked_static_charts") 
            ),
            ui.nav_panel("Site As-Builts",
                ui.output_ui("as_builts_ui")
            )
        )
    )

# =============================================================================
# SHINY SERVER MODULE
# =============================================================================
@module.server
def time_vs_temp_server(input, output, session, client, selected_project, lookback_days, 
                        global_show_masked, global_show_baddata, global_show_map, 
                        global_show_elevation, unit_mode, unit_label, display_tz, 
                        active_refs, project_metadata):

    # --- REACTIVE DATA FETCHING ---
    @reactive.Calc
    def get_raw_data():
        proj = selected_project() if callable(selected_project) else selected_project
        if not proj or proj == "All Projects": return pd.DataFrame()
        
        days = lookback_days() if callable(lookback_days) else lookback_days
        show_m = global_show_masked() if callable(global_show_masked) else global_show_masked
        show_b = global_show_baddata() if callable(global_show_baddata) else global_show_baddata
        
        raw_data = get_universal_portal_data(proj, lookback_days=days, is_summary_page=False, show_masked=show_m, show_baddata=show_b)
        return apply_sanity_filter(raw_data)

    @output
    @render.ui
    def system_filter_ui():
        df = get_raw_data()
        if df.empty: return ui.HTML("")
        avail_sys = sorted([str(s) for s in df['System'].dropna().unique() if str(s).strip().upper() not in ['NAN', 'NONE', '']], key=natural_sort_key)
        
        if len(avail_sys) > 1:
            return ui.input_selectize("selected_systems", "⚙️ Filter by System:", choices=avail_sys, multiple=True)
        return ui.HTML("")

    @reactive.Calc
    def shared_chart_data():
        df = get_raw_data()
        if df.empty: return pd.DataFrame(), pd.DataFrame(), []
        
        avail_sys = sorted([str(s) for s in df['System'].dropna().unique() if str(s).strip().upper() not in ['NAN', 'NONE', '']], key=natural_sort_key)
        
        sys_filter = []
        if len(avail_sys) > 1:
            if hasattr(input, "selected_systems"):
                sys_filter = input.selected_systems()
            
        if sys_filter:
            df = df[df['System'].astype(str).isin(sys_filter)]
            
        valid_locs = sorted([loc for loc in df['Location'].dropna().unique() if str(loc).strip().upper() != 'UNASSIGNED'], key=natural_sort_key)
        
        map_df = pd.DataFrame()
        proj = selected_project() if callable(selected_project) else selected_project
        if proj and proj != "All Projects":
            job_num = proj.split('-')[0].strip()
            try:
                map_query = f"SELECT Project, Location, Map_X, Map_Y, Image_Name FROM `{PROJECT_ID}.{DATASET_ID}.TempPipeLoc` WHERE CAST(Project AS STRING) = '{job_num}'"
                map_df = client.query(map_query).to_dataframe()
            except Exception:
                pass
                
        return df, map_df, valid_locs

    # --- THE STATIC CHART GENERATOR ---
    @output
    @render.ui
    def stacked_static_charts():
        """Generates all Plotly figures, converts them to static HTML images, and stacks them."""
        df, map_df, locs = shared_chart_data()
        if not locs:
            return ui.p("Waiting for valid sensor data...", class_="text-muted")

        u_mode = unit_mode() if callable(unit_mode) else unit_mode
        u_lbl = unit_label() if callable(unit_label) else unit_label
        tz = display_tz() if callable(display_tz) else display_tz
        refs = active_refs() if callable(active_refs) else active_refs
        days = lookback_days() if callable(lookback_days) else lookback_days
        show_elev_opt = global_show_elevation() if callable(global_show_elevation) else global_show_elevation
        proj = selected_project() if callable(selected_project) else selected_project
        show_m = global_show_masked() if callable(global_show_masked) else global_show_masked
        show_b = global_show_baddata() if callable(global_show_baddata) else global_show_baddata
        show_map_opt = global_show_map() if callable(global_show_map) else global_show_map
        
        end_date = pd.Timestamp.now()
        start_date = end_date - pd.Timedelta(days=days)

        p_meta = project_metadata.get() if hasattr(project_metadata, 'get') else {}
        real_f_date = p_meta.get('Date_Freezedown')
        freeze_start_ts = start_date
        
        parsed_date = pd.to_datetime(real_f_date, errors='coerce')
        if pd.notnull(parsed_date):
            freeze_start_ts = parsed_date.tz_localize(None) if parsed_date.tzinfo else parsed_date

        cards = []
        
        # Loop through valid locations, render to image, and append to the DOM
        for loc in locs:
            loc_data = df[df['Location'] == loc]
            if loc_data.empty:
                continue

            # 1. Build and convert Trend Graph
            fig = build_high_speed_graph(
                client=client, df=loc_data, title=f"Thermal Trends: {loc}",
                start_view=start_date, end_view=end_date, active_refs=refs,
                unit_mode=u_mode, unit_label=u_lbl, display_tz=tz,
                f_start_date=freeze_start_ts, curve_id=proj, show_elevation=show_elev_opt,
                opt_show_masked=show_m, opt_show_baddata=show_b, opt_project_name=proj
            )
            trend_img_src = plotly_to_base64_img(fig)
            trend_ui = ui.img(src=trend_img_src, style="width: 100%; max-width: 1200px; height: auto;") if trend_img_src else ui.p("Chart rendering failed (Check if kaleido is installed).", class_="text-danger")

            # 2. Build and convert Map Graph
            loc_clean = str(loc).strip().upper()
            map_ui = ui.div()
            
            if loc_clean.startswith('T') and not map_df.empty and show_map_opt:
                if 'Location' in map_df.columns and loc_clean in map_df['Location'].values:
                    job_num = proj.split('-')[0].strip() if proj else ""
                    as_built_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "as_builts"))
                    
                    map_fig = build_cropped_site_map(job_num, loc_clean, map_df, as_built_path)
                    map_img_src = plotly_to_base64_img(map_fig)
                    
                    if map_img_src:
                        map_ui = ui.img(src=map_img_src, style="width: 100%; max-width: 400px; height: auto;")

            # 3. Assemble the visual card
            card = ui.card(
                ui.h4(f"📍 Location: {loc}"),
                ui.layout_columns(
                    ui.div(trend_ui),
                    ui.div(map_ui),
                    col_widths=[9, 3]
                ),
                style="margin-bottom: 30px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);"
            )
            cards.append(card)

        # Return the entire stack to the UI at once
        return ui.div(*cards)

    # --- 3. AS-BUILTS ENGINE ---
    @output
    @render.ui
    def as_builts_ui():
        proj = selected_project() if callable(selected_project) else selected_project
        if not proj or proj == "All Projects": return ui.p("Please select a specific project.")
        
        job_num = proj.split('-')[0].strip()
        as_builts_dir = "as_builts"
        
        if not os.path.exists(as_builts_dir):
            return ui.p(f"⚠️ Please create an `{as_builts_dir}` folder in your main directory.", class_="text-warning")
            
        found_images = [os.path.join(as_builts_dir, f) for f in os.listdir(as_builts_dir) 
                        if f.startswith(job_num) and f.lower().endswith(('.png', '.jpg', '.jpeg'))]
        
        if not found_images:
            return ui.p(f"ℹ️ No as-built images found for Job {job_num}.", class_="text-info")
            
        found_images = sorted(found_images, key=natural_sort_key)
        
        img_blocks = [ui.h4(f"Site As-Builts: {proj}")]
        for img_path in found_images:
            try:
                b64_src = get_image_base64_from_file(img_path)
                img_blocks.append(ui.img(src=b64_src, style="max-width: 100%; height: auto; margin-bottom: 20px; border: 1px solid #ddd; padding: 5px; border-radius: 5px;"))
                img_blocks.append(ui.hr())
            except Exception as e:
                img_blocks.append(ui.p(f"Could not load image: {os.path.basename(img_path)} ({e})", class_="text-danger"))
            
        return ui.div(*img_blocks)
