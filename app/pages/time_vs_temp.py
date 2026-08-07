from shiny import ui, render, reactive, module
from shinywidgets import output_widget, render_plotly
import pandas as pd
import os
import re
import base64

# Internal Imports
from app.utils.config import PROJECT_ID, DATASET_ID
from app.data.processor import get_universal_portal_data, apply_sanity_filter
from app.components.charts import build_high_speed_graph, build_cropped_site_map

# Generous limit for the dynamic plot factory
MAX_CHARTS = 50

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================
def natural_sort_key(text):
    return [int(c) if c.isdigit() else str(c).lower() for c in re.split(r'(\d+)', str(text))]

def get_image_base64(img_path):
    """Safely encodes local directory images to base64 so Shiny can render them dynamically."""
    with open(img_path, "rb") as image_file:
        encoded_string = base64.b64encode(image_file.read()).decode("utf-8")
    ext = os.path.splitext(img_path)[1].lower().replace('.', '')
    if ext == 'jpg': ext = 'jpeg'
    return f"data:image/{ext};base64,{encoded_string}"

# =============================================================================
# SHINY UI MODULE
# =============================================================================
@module.ui
def time_vs_temp_ui():
    """Defines the visual layout for the Time vs Temp charts."""
    return ui.div(
        ui.h2("📈 Time vs Temperature Tracking"),
        ui.navset_card_tab(
            ui.nav_panel("Telemetry Charts",
                ui.output_ui("system_filter_ui"),
                ui.hr(),
                ui.output_ui("dynamic_charts_ui")
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
    def get_clean_data():
        proj = selected_project() if callable(selected_project) else selected_project
        if not proj or proj == "All Projects": return pd.DataFrame()
        
        days = lookback_days() if callable(lookback_days) else lookback_days
        show_m = global_show_masked() if callable(global_show_masked) else global_show_masked
        show_b = global_show_baddata() if callable(global_show_baddata) else global_show_baddata
        
        raw_data = get_universal_portal_data(
            proj, 
            lookback_days=days,
            is_summary_page=False,
            show_masked=show_m,
            show_baddata=show_b
        )
        return apply_sanity_filter(raw_data)

    @reactive.Calc
    def get_map_coords():
        proj = selected_project() if callable(selected_project) else selected_project
        if not proj or proj == "All Projects" or client is None: return pd.DataFrame()
        job_num = proj.split('-')[0].strip()
        try:
            map_query = f"""
                SELECT Project, Location, Map_X, Map_Y, Image_Name 
                FROM `{PROJECT_ID}.{DATASET_ID}.TempPipeLoc` 
                WHERE CAST(Project AS STRING) = '{job_num}'
            """
            return client.query(map_query).to_dataframe()
        except:
            return pd.DataFrame()

    @reactive.Calc
    def shared_chart_data():
        """Consolidates data fetching and filtering to feed the UI loop and the Plotly factory."""
        df = get_clean_data()
        if df.empty: return None, None, []
        
        sys_filter = input.selected_systems() if hasattr(input, 'selected_systems') else []
        if sys_filter:
            df = df[df['System'].astype(str).isin(sys_filter)]
            
        map_df = get_map_coords()
        valid_locations = sorted([loc for loc in df['Location'].dropna().unique() if str(loc).strip().upper() != 'UNASSIGNED'], key=natural_sort_key)
        
        return df, map_df, valid_locations

    # --- UI RENDERING ---
    @output
    @render.ui
    def system_filter_ui():
        df = get_clean_data()
        if df.empty: return ui.HTML("")
        avail_sys = sorted([str(s) for s in df['System'].dropna().unique() if str(s).strip().upper() not in ['NAN', 'NONE', '']], key=natural_sort_key)
        
        if len(avail_sys) > 1:
            return ui.input_selectize("selected_systems", "⚙️ Filter by System (Leave blank to show all):", avail_sys, multiple=True)
        return ui.HTML("")

    @output
    @render.ui
    def dynamic_charts_ui():
        df, map_df, valid_locations = shared_chart_data()
        proj = selected_project() if callable(selected_project) else selected_project
        
        if df is None or not valid_locations or proj == "All Projects": 
            return ui.p("No data available for this timeline.", class_="text-warning")

        show_map_opt = global_show_map() if callable(global_show_map) else global_show_map

        ui_elements = []
        for i, loc in enumerate(valid_locations):
            loc_clean = str(loc).strip().upper()
            is_temp_pipe = loc_clean.startswith('T')
            
            has_map = False
            if is_temp_pipe and not map_df.empty and 'Location' in map_df.columns:
                if loc_clean in map_df['Location'].values:
                    has_map = True

            # THE FIX: DO NOT use session.ns() here. shinywidgets automatically resolves 
            # namespaces inside the UI component loop. Double-namespacing causes the gray line!
            chart_id = f"timeline_chart_{i}"
            map_id = f"timeline_map_{i}"

            if has_map and show_map_opt:
                chart_layout = ui.layout_columns(
                    ui.div(output_widget(chart_id, height="750px")),
                    ui.div(output_widget(map_id, height="750px")),
                    col_widths=[9, 3]
                )
            else:
                # Give it full width and an explicit height so it doesn't collapse
                chart_layout = output_widget(chart_id, width="100%", height="750px")

            ui_elements.append(
                ui.card(
                    chart_layout,
                    style="margin-bottom: 20px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);"
                )
            )

        return ui.TagList(*ui_elements)

    # --- FACTORY PATTERN: PLOTLY WIDGET INJECTION ---
    for i in range(MAX_CHARTS):
        def make_timeline_chart(index):
            @output(id=f"timeline_chart_{index}")
            @render_plotly
            def _plot():
                df, map_df, locs = shared_chart_data()
                if df is not None and index < len(locs):
                    loc = locs[index]
                    loc_data = df[df['Location'] == loc]
                    if loc_data.empty: return None

                    u_mode = unit_mode() if callable(unit_mode) else unit_mode
                    u_lbl = unit_label() if callable(unit_label) else unit_label
                    tz = display_tz() if callable(display_tz) else display_tz
                    refs = active_refs() if callable(active_refs) else active_refs
                    days = lookback_days() if callable(lookback_days) else lookback_days
                    show_elev_opt = global_show_elevation() if callable(global_show_elevation) else global_show_elevation
                    proj = selected_project() if callable(selected_project) else selected_project
                    
                    end_date = pd.Timestamp.now()
                    start_date = end_date - pd.Timedelta(days=days)

                    p_meta = project_metadata.get() if hasattr(project_metadata, 'get') else {}
                    real_f_date = p_meta.get('Date_Freezedown')
                    freeze_start_ts = start_date
                    
                    parsed_date = pd.to_datetime(real_f_date, errors='coerce')
                    if pd.notnull(parsed_date):
                        freeze_start_ts = parsed_date.tz_localize(None) if parsed_date.tzinfo else parsed_date
                    
                    fig = build_high_speed_graph(
                        client=client, df=loc_data, title=f"Thermal Trends: {loc}",
                        start_view=start_date, end_view=end_date, active_refs=refs,
                        unit_mode=u_mode, unit_label=u_lbl, display_tz=tz,
                        f_start_date=freeze_start_ts, curve_id=proj, show_elevation=show_elev_opt
                    )
                    return fig
                return None
            return _plot

        def make_timeline_map(index):
            @output(id=f"timeline_map_{index}")
            @render_plotly
            def _map():
                df, map_df, locs = shared_chart_data()
                if df is not None and index < len(locs):
                    loc = locs[index]
                    loc_clean = str(loc).strip().upper()
                    is_temp_pipe = loc_clean.startswith('T')
                    show_map_opt = global_show_map() if callable(global_show_map) else global_show_map
                    
                    if is_temp_pipe and show_map_opt and not map_df.empty and 'Location' in map_df.columns:
                        if loc_clean in map_df['Location'].values:
                            proj = selected_project() if callable(selected_project) else selected_project
                            job_num = proj.split('-')[0].strip()
                            as_built_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "as_builts"))
                            map_fig = build_cropped_site_map(job_num, loc_clean, map_df, as_built_path)
                            return map_fig
                return None
            return _map

        make_timeline_chart(i)
        make_timeline_map(i)

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
                b64_src = get_image_base64(img_path)
                img_blocks.append(ui.img(src=b64_src, style="max-width: 100%; height: auto; margin-bottom: 20px; border: 1px solid #ddd; padding: 5px; border-radius: 5px;"))
                img_blocks.append(ui.hr())
            except Exception as e:
                img_blocks.append(ui.p(f"Could not load image: {os.path.basename(img_path)} ({e})", class_="text-danger"))
            
        return ui.div(*img_blocks)
