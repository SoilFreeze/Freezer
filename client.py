from shiny import App, render, reactive, ui
import os

# Import your decoupled backend logic
from app.pages.summary import get_summary_data
from app.pages.depth import generate_depth_figures

# We will use Plotly for rendering the charts Shiny returns
try:
    from shinywidgets import output_widget, render_plotly
except ImportError:
    pass # Let the server catch this if shinywidgets is missing

# ===============================================================
# 1. SHINY UI DEFINITION
# ===============================================================

# Define the sidebar for Job Number input
app_sidebar = ui.sidebar(
    ui.h4("Configuration"),
    ui.input_text("job_number", "Job Number:", placeholder="e.g., 2527"),
    ui.p("Enter your assigned Job Number to view project telemetry.", style="font-size: 0.9em; color: gray;")
)

# Define the main navigation pages
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
    
    # --- Reactive state to track the current job number ---
    @reactive.Calc
    def current_job():
        return input.job_number().strip()

    # --- TAB 1: SUMMARY ENGINE ---
    @render.ui
    def dynamic_summary_header():
        job = current_job()
        if not job:
            return ui.h2("🌐 Global Active Project Summary")
        return ui.h2(f"📊 Project Summary: {job}")

    @render.ui
    def dynamic_summary_cards():
        job = current_job()
        if not job:
            return ui.p("Please provide a job number to view the summary.", style="color: gray; font-style: italic;")
            
        # Call the decoupled python function
        active_projs, pool_df, tel_df, err = get_summary_data(selected_project=job, show_archived_opt=False)
        
        if err:
            return ui.div(f"Error loading summary: {err}", style="color: red;")
        if tel_df is None or tel_df.empty:
            return ui.div(f"No recent telemetry found for job: {job} in the last 48 hours.", style="color: orange;")
            
        # Classify the data
        is_amb_col = tel_df['Location'].astype(str).str.upper() == 'AMBIENT'
        is_tp_col = tel_df['Depth'].notnull() & (tel_df['Depth'].astype(str).str.strip() != '') & ~is_amb_col
        is_s_col = (tel_df['Bank'].astype(str).str.startswith('S') | tel_df['Location'].astype(str).str.startswith('S')) & ~is_amb_col & ~is_tp_col
        is_r_col = (tel_df['Bank'].astype(str).str.startswith('R') | tel_df['Location'].astype(str).str.startswith('R')) & ~is_amb_col & ~is_tp_col
        
        groups = [
            ("📥 Supply", tel_df[is_s_col], "blue"), 
            ("📤 Return", tel_df[is_r_col], "orange"), 
            ("📏 TempPipes", tel_df[is_tp_col], "green"),
            ("☁️ Ambient", tel_df[is_amb_col], "gray")
        ]
        
        cards = []
        for title, g_df, color in groups:
            if g_df.empty or g_df['latest_temp'].isnull().all():
                continue
                
            latest_val = g_df['latest_temp'].mean()
            # Construct a simple card UI
            card = ui.div(
                ui.h4(title, style="margin-bottom: 5px;"),
                ui.h2(f"{latest_val:.1f} °F", style=f"color: {color}; margin-top: 0;"),
                style="border: 1px solid #ddd; padding: 15px; border-radius: 8px; text-align: center; background-color: #f9f9f9;"
            )
            cards.append(card)
            
        if not cards:
            return ui.p("Insufficient data to render summary cards.")
            
        return ui.layout_column_wrap(*cards, width=1/4)

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
            
            # Wrap the widget in a card structure
            ui_elements.append(
                ui.card(
                    ui.card_header(f"📍 Temp vs {chart_label} - {loc}"),
                    output_widget(widget_id),
                    full_screen=True
                )
            )
            
            # Register the render function dynamically
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
            # In Shiny, local images must be served via a web directory or handled specifically.
            # For simplicity, if the image is within the app directory, we can wrap it in an img tag if it's in a 'www' folder.
            # *NOTE: For Posit Connect Cloud, we assume the 'as_builts' folder is accessible to the app logic.*
            image_elements.append(
                ui.div(
                    ui.h5(img_name),
                    ui.p(f"[Image Data: {img_path}] - Note: Static file serving requires 'www' directory configuration in Shiny."),
                    style="border: 1px solid #ddd; padding: 10px; margin-bottom: 10px;"
                )
            )
            
        return ui.TagList(*image_elements)

app = App(app_ui, server)
