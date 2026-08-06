from shiny import App, render, reactive, ui
import pandas as pd
import os

# Import your decoupled backend logic
from app.pages.summary import get_summary_data
from app.pages.depth import generate_depth_figures

try:
    from shinywidgets import output_widget, render_plotly
except ImportError:
    pass

# ===============================================================
# 1. SHINY UI DEFINITION
# ===============================================================
app_sidebar = ui.sidebar(
    ui.h4("Configuration"),
    ui.input_text("job_number", "Job Number:", placeholder="e.g., 2527"),
    ui.p("Enter your assigned Job Number to view project telemetry.", style="font-size: 0.9em; color: gray;")
)

app_ui = ui.page_navbar(
    ui.nav_panel(
        "🏠 Summary", 
        ui.output_ui("dynamic_summary_header"),
        ui.output_ui("dynamic_summary_cards")
    ),
    ui.nav_panel(
        "📏 Profile Analysis", 
        ui.h2("Depth Profile Analysis"),
        ui.output_ui("dynamic_depth_charts")
    ),
    ui.nav_panel(
        "🗺️ As Built",
        ui.h2("Site As-Builts"),
        ui.output_ui("dynamic_as_builts")
    ),
    title="SoilFreeze Client Portal",
    id="main_nav",
    sidebar=app_sidebar,
    fillable=True
)

# ===============================================================
# 2. SHINY SERVER LOGIC
# ===============================================================
def server(input, output, session):
    
    @reactive.Calc
    def current_job():
        return input.job_number().strip()

    # --- TAB 1: SUMMARY ENGINE ---
    @render.ui
    def dynamic_summary_header():
        job = current_job()
        if not job:
            return ui.h2("🌐 Global Active Project Summary")
            
        # Pull data dynamically
        active_projs, _, tel_df, err = get_summary_data(selected_project=job, show_archived_opt=False)
        
        if err or active_projs is None or active_projs.empty:
            return ui.h1(f"📊 Project: {job}")
            
        proj_row = active_projs.iloc[0]
        proj_name = proj_row['ProjectName'] if pd.notnull(proj_row['ProjectName']) else job
        f_date = proj_row.get('Date_Freezedown')
        
        # 1. Calculate days of freezedown for the sub-header
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
                
        # 2. Calculate the official data status (Max Timestamp) for the banner
        latest_str = "Unknown"
        if tel_df is not None and not tel_df.empty:
            max_ts = tel_df['latest_ts'].max()
            if pd.notnull(max_ts):
                # Convert to US/Pacific matching the internal UI format
                if max_ts.tzinfo is None:
                    max_ts = max_ts.tz_localize('UTC')
                latest_str = max_ts.tz_convert('US/Pacific').strftime('%B %d, %Y at %I:%M %p')
        
        # Combine everything into the header HTML block
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
            <h3 style="margin-bottom: 25px; color: #2c3e50; font-weight: 500;">🌐 24 hour Thermal Summary</h3>
        """)

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
            
        # Classify the data based on your specific location rules
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
            
            # Format numbers safely
            l_str = f"{latest_val:.1f}°F" if pd.notnull(latest_val) else "N/A"
            h_str = f"{high_24:.1f}°F" if pd.notnull(high_24) else "N/A"
            lo_str = f"{low_24:.1f}°F" if pd.notnull(low_24) else "N/A"
            
            # Construct the clean, borderless HTML card matching the screenshot
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
            
        # Render the HTML cards in a 4-column wrap
        return ui.layout_column_wrap(*cols, width=1/4, gap="30px")

    # --- TAB 2: DEPTH PROFILE ENGINE ---
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
            widget_id = f"depth_chart_{loc}"
            
            ui_elements.append(
                ui.card(
                    ui.card_header(f"📍 Temp vs {chart_label} - {loc}"),
                    output_widget(widget_id),
                    full_screen=True
                )
            )
            
            def make_render_func(plot_fig):
                @render_plotly
                def _render_chart():
                    return plot_fig
                return _render_chart
                
            session.output.register(widget_id, make_render_func(fig))
            
        return ui.TagList(*ui_elements)

    # --- TAB 3: AS BUILTS ENGINE ---
    @render.ui
    def dynamic_as_builts():
        job = current_job()
        if not job:
            return ui.p("Please provide a job number to view as-builts.", style="color: gray; font-style: italic;")
            
        job_root = str(job).split('-')[0].strip()
        as_builts_dir = "as_builts"
        
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
            img_path = os.path.join(as_builts_dir, img_name)
            image_elements.append(
                ui.div(
                    ui.h5(img_name),
                    ui.p(f"[Image Data: {img_path}] - Note: Static file serving requires 'www' directory configuration in Shiny."),
                    style="border: 1px solid #ddd; padding: 10px; margin-bottom: 10px;"
                )
            )
            
        return ui.TagList(*image_elements)

app = App(app_ui, server)
