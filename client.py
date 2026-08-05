from shiny import App, ui, reactive, render
from shinywidgets import output_widget, render_plotly
import pandas as pd
import urllib.parse

# Import your existing heavy lifters!
from app.data.processor import get_universal_portal_data, get_bq_client
from app.components.charts import build_high_speed_graph

# ===============================================================
# 1. THE UI (The layout blueprint)
# ===============================================================
app_ui = ui.page_navbar(
    
    pp_ui = ui.page_navbar(
    ui.nav_panel(
        "🏠 Summary", 
        ui.h2("Project Summary"),
        ui.output_ui("dynamic_summary_cards")
    ),
    title="SoilFreeze Client Portal",
    id="main_nav"
),
    
    # TAB 2: Timeline 
    ui.nav_panel(
        "📈 Timeline Analysis", 
        ui.layout_sidebar(
            ui.sidebar(
                ui.input_text("job_number", "Job Number (e.g., 2527):", value="2527"),
                ui.input_select("phase_filter", "📂 Select Phase:", choices=["Loading..."]),
                ui.input_selectize("system_filter", "⚙️ Filter by System:", choices=[], multiple=True)
            ),
            output_widget("timeline_chart") 
        )
    ),
    
    # TAB 3: As-Builts 
    ui.nav_panel(
        "🗺️ As Built", 
        ui.h2("Site As-Builts"),
        ui.output_ui("as_built_images")
    ),
    
    # KEYWORD ARGUMENTS
    title="SoilFreeze Client Portal",
    id="main_nav"
)

# ===============================================================
# 2. THE SERVER (The reactive brain)
# ===============================================================
def server(input, output, session):
    
       
    # @reactive.calc is Shiny's version of caching. 
    # It only fetches from BigQuery when the job_number actually changes!
    @reactive.calc
    def master_data():
        job = input.job_number()
        if not job:
            return pd.DataFrame()
            
        # Call your existing function!
        df = get_universal_portal_data(job)
        return df

    # This updates the dropdown choices automatically when new data arrives
    @reactive.effect
    def update_filters():
        df = master_data()
        if not df.empty:
            phases = ["All Phases"] + sorted([str(p) for p in df['Phase'].dropna().unique() if str(p).strip().upper() not in ['NAN', 'NONE', '']])
            systems = sorted([str(s) for s in df['System'].dropna().unique() if str(s).strip().upper() not in ['NAN', 'NONE', '']])
            
            ui.update_select("phase_filter", choices=phases)
            ui.update_selectize("system_filter", choices=systems)

    # Another @reactive.calc to apply the filters quickly without re-querying BigQuery
    @reactive.calc
    def filtered_data():
        df = master_data()
        if df.empty:
            return df
            
        phase = input.phase_filter()
        systems = input.system_filter()
        
        if phase and phase != "All Phases":
            df = df[df['Phase'].astype(str) == phase]
        if systems:
            df = df[df['System'].astype(str).isin(systems)]
            
        return df

    # --- PLOTLY CHART RENDERING ---
    # @render_plotly connects to the output_widget("timeline_chart") in the UI
    @render_plotly
    def timeline_chart():
        df = filtered_data()
        if df.empty:
            return None
            
        # We'll just grab the first valid location for this example
        locations = [loc for loc in df['Location'].dropna().unique() if 'AMBIENT' not in str(loc).upper()]
        if not locations:
            return None
            
        target_loc = locations[0]
        loc_data = df[df['Location'] == target_loc]
        
        bq_client = get_bq_client()
        
        # Call your EXACT same chart builder!
        fig = build_high_speed_graph(
            client=bq_client,
            df=loc_data,
            title=f"Thermal Trends: {target_loc}",
            start_view=df['timestamp'].min(),
            end_view=df['timestamp'].max(),
            active_refs=[(32.0, "Freezing")],
            unit_mode="Fahrenheit",
            unit_label="°F",
            display_tz="US/Pacific",
            curve_id=f"{input.job_number()}-{target_loc}"
        )
        return fig

# ===============================================================
# 3. APP LAUNCHER
# ===============================================================
app = App(app_ui, server)
