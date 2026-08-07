from shiny import ui, render, reactive, module
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
        
        # Unwrap reactive values
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
        df = get_clean_data()
        proj = selected_project() if callable(selected_project) else selected_project
        if df.empty or proj == "All Projects": return ui.p("No data available for this timeline.", class_="text-warning")

        # Apply system filter
        sys_filter = input.selected_systems() if hasattr(input, 'selected_systems') else []
        if sys_filter:
            df = df[df['System'].astype(str).isin(sys_filter)]

        # Unwrap context variables
        map_df = get_map_coords()
        u_mode = unit_mode() if callable(unit_mode) else unit_mode
        u_lbl = unit_label() if callable(unit_label) else unit_label
        tz = display_tz() if callable(display_tz) else display_tz
        refs = active_refs() if callable(active_refs) else active_refs
        days = lookback_days() if callable(lookback_days) else lookback_days
        show_map_opt = global_show_map() if callable(global_show_map) else global_show_map
        show_elev_opt = global_show_elevation() if callable(global_show_elevation) else global_show_elevation

        # Timeline logic
        end_date = pd.Timestamp.now()
        start_date = end_date - pd.Timedelta(days=days)

        p_meta = project_metadata.get() if hasattr(project_metadata, 'get') else {}
        real_f_date = p_meta.get('Date_Freezedown')
        freeze_start_ts = start_date
        
        parsed_date = pd.to_datetime(real_f_date, errors='coerce')
        if pd.notnull(parsed_date):
            freeze_start_ts = parsed_date.tz_localize(None) if parsed_date.tzinfo else parsed_date

        job_num = proj.split('-')[0].strip()
        valid_locations = sorted([loc for loc in df['Location'].dropna().unique() if str(loc).strip().upper() != 'UNASSIGNED'], key=natural_sort_key)

        chart_blocks = []

        for loc in valid_locations:
            loc_data = df[df['Location'] == loc]
            if loc_data.empty: continue

            has_map = False
            if not map_df.empty and 'Location' in map_df.columns:
                has_map = str(loc) in map_df['Location'].astype(str).values

            fig = build_high_speed_graph(
                client=client, df=loc_data, title=f"Thermal Trends: {loc}",
                start_view=start_date, end_view=end_date, active_refs=refs,
                unit_mode=u_mode, unit_label=u_lbl, display_tz=tz,
                f_start_date=freeze_start_ts, curve_id=proj, show_elevation=show_elev_opt
            )
            
            # --- FIXED INDENTATION & CDN TAGS ---
            if fig:
                # Include Plotly JS dynamically so the HTML div knows how to render itself
                fig_html = fig.to_html(full_html=False, include_plotlyjs="cdn")

                if has_map and show_map_opt:
                    site_map_fig = build_cropped_site_map(job_num, loc, map_df, "as_builts")
                    if site_map_fig:
                        map_html = site_map_fig.to_html(full_html=False, include_plotlyjs="cdn")
                        row_html = f'''
                        <div style="display: flex; gap: 20px; margin-bottom: 20px;">
                            <div style="flex: 3; min-width: 0;">{fig_html}</div>
                            <div style="flex: 1; min-width: 0;">{map_html}</div>
                        </div>
                        <hr>
                        '''
                        chart_blocks.append(ui.HTML(row_html))
                    else:
                        chart_blocks.append(ui.HTML(f'<div style="margin-bottom: 20px;">{fig_html}</div><div class="alert alert-info" style="color: black;">🗺️ Map image for {job_num} not found in the as_builts folder.</div><hr>'))
                else:
                    chart_blocks.append(ui.HTML(f'<div style="margin-bottom: 20px;">{fig_html}</div><hr>'))

        if not chart_blocks:
            return ui.p("No valid locations to display.", class_="text-warning")
            
        return ui.div(*chart_blocks)

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
                # Convert the image to Base64 so Shiny serves it seamlessly regardless of local folder routing
                b64_src = get_image_base64(img_path)
                img_blocks.append(ui.img(src=b64_src, style="max-width: 100%; height: auto; margin-bottom: 20px; border: 1px solid #ddd; padding: 5px; border-radius: 5px;"))
                img_blocks.append(ui.hr())
            except Exception as e:
                img_blocks.append(ui.p(f"Could not load image: {os.path.basename(img_path)} ({e})", class_="text-danger"))
            
        return ui.div(*img_blocks)
