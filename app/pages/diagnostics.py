from shiny import ui, render, reactive, module
from shinywidgets import output_widget, render_plotly
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import re

# Internal Imports
from app.utils.config import (
    PROJECT_ID, 
    DATASET_ID, 
    PROJECT_REGISTRY_TABLE,
    MASTER_VIEW,
    NODE_REGISTRY_TABLE
)

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================
def natural_sort_key(s):
    if pd.isnull(s): return []
    return [int(text) if text.isdigit() else str(text).lower() for text in re.split(r'(\d+)', str(s))]

def fmt_temp(val, unit_mode, unit_label):
    if pd.isnull(val) or pd.isna(val): return "N/A"
    v = (val - 32) * 5/9 if unit_mode == "Celsius" else val
    return f"{v:.1f}{unit_label}"

def assign_row_color(hours):
    if hours is None or pd.isna(hours) or hours == float('inf'): return "background-color: #d1d5db; color: #1f2937;"
    if hours < 1.0: return "background-color: #d1fae5; color: #065f46;"
    if 1.0 <= hours <= 6.0: return "background-color: #fef08a; color: #854d0e;"
    if 6.0 < hours <= 12.0: return "background-color: #fed7aa; color: #9a3412;"
    return "background-color: #fca5a5; color: #991b1b;"

# =============================================================================
# SHINY UI MODULE
# =============================================================================
@module.ui
def diagnostics_ui():
    """Defines the visual layout for the Node Diagnostics Workspace."""
    return ui.div(
        ui.h2("🔬 Node Diagnostics Workspace"),
        ui.navset_card_tab(
            
            # --- TAB 1: DATA LOOKUP ---
            ui.nav_panel("🔍 Data Lookup",
                ui.h3("🔍 Node Telemetry Inspection (Multi-Node)"),
                ui.output_ui("lookup_scope_info"),
                ui.layout_columns(
                    ui.input_radio_buttons("diag_search_mode", "Search Method", ["Filter Mappings", "Search by Node ID"], inline=True),
                    ui.output_ui("diag_filter_ui")
                ),
                ui.hr(),
                
                ui.layout_columns(
                    ui.h4("📈 Telemetry History for Selected Nodes"),
                    ui.input_select("diag_time_opt", "Historical Window:", ["30 Days", "60 Days", "90 Days", "1 Year", "All Time"])
                ),
                
                ui.output_ui("diag_node_metrics_ui"),
                ui.h4("📉 Temperature Trend"),
                ui.input_checkbox("diag_show_ambient", "Show Ambient Office Temperature", False),
                output_widget("diag_trend_chart")
            ),
            
            # --- TAB 2: THERMAL PERFORMANCE ---
            ui.nav_panel("📊 Thermal Performance Metrics",
                ui.h3("📊 Ground Freezing System Performance"),
                ui.h5("⏳ Timeline & Baselines"),
                ui.layout_columns(
                    ui.input_slider("perf_history_weeks", "Select History Window (Weeks)", min=1, max=12, value=2),
                    ui.input_slider("perf_baseline_days", "Cluster Baseline Window (Days)", min=1, max=14, value=1)
                ),
                ui.hr(),
                ui.output_ui("perf_dynamic_filters_ui"),
                ui.hr(),
                
                ui.h4("📍 Array Summary (Latest Readings)"),
                ui.output_data_frame("perf_summary_df"),
                
                ui.h4("📈 Visual Thermodynamics"),
                ui.layout_columns(
                    output_widget("perf_scatter_chart"),
                    output_widget("perf_bar_chart")
                ),
                
                ui.h4("🗄️ Raw Mathematical Evaluation"),
                ui.output_data_frame("perf_raw_eval_df"),
                ui.hr(),
                
                ui.h4("🌡️ Thermodynamic Master View"),
                output_widget("perf_master_chart")
            ),
            
            # --- TAB 3: NODE ALERTS ---
            ui.nav_panel("⚠️ Node Alerts",
                ui.h3("⚠️ Node Alert Dashboard"),
                ui.p("Real-time tracking for telemetry dropouts, extreme temperature limits, and anomalous data spikes."),
                ui.hr(),
                
                ui.h4("📊 Fleet Health Summary"),
                ui.output_data_frame("alert_summary_df"),
                ui.hr(),
                
                ui.output_ui("alert_scope_info"),
                ui.h5("📡 Missing Nodes"),
                ui.output_ui("alert_missing_ui"),
                
                ui.h5("🌡️ Extreme Temps"),
                ui.output_ui("alert_extreme_ui"),
                
                ui.h5("📈 Spiking Data"),
                ui.output_ui("alert_spiking_ui")
            ),
            
            # --- TAB 4: BAD ACTORS & RELIABILITY ---
            ui.nav_panel("🚨 Bad Actor & Reliability",
                ui.h3("🚨 Global Network Reliability Overview"),
                ui.p("Complete node registry with drill-down overviews by location/pipe."),
                ui.output_ui("rel_global_metrics_ui"),
                ui.hr(),
                
                ui.h4("📍 Drill-Down by Location / Temp Pipe"),
                ui.input_select("rel_loc_drilldown", "Select Location to Inspect:", []),
                ui.output_data_frame("rel_drilldown_df")
            )
        )
    )

# =============================================================================
# SHINY SERVER MODULE
# =============================================================================
@module.server
def diagnostics_server(input, output, session, client, selected_project, display_tz, unit_mode, unit_label, global_show_archived):
    """Handles reactive database fetching and graph rendering for Diagnostics."""

    # --- REACTIVE CORE REGISTRY ---
    @reactive.Calc
    def get_reg_df():
        if client is None: return pd.DataFrame()
        try:
            return client.query(f"SELECT * FROM `{NODE_REGISTRY_TABLE}`").to_dataframe()
        except Exception:
            return pd.DataFrame()

    @reactive.Calc
    def get_alert_df():
        if client is None: return pd.DataFrame()
        archived_toggle = global_show_archived() if callable(global_show_archived) else global_show_archived
        active_sql = "1=1" if archived_toggle else "UPPER(TRIM(CAST(ShowActive AS STRING))) IN ('TRUE', 'YES', '1')"
        
        alert_q = f"""
            WITH ActiveJobs AS (
                SELECT CAST(Project AS STRING) as FullProjectID, TRIM(SPLIT(SPLIT(CAST(Project AS STRING), '-')[OFFSET(0)], ' ')[OFFSET(0)]) as RootJob
                FROM `{PROJECT_REGISTRY_TABLE}` WHERE {active_sql}
            ),
            BaseNodes AS (
                SELECT n.NodeNum, CAST(n.Project AS STRING) as RawProject, n.Phase, n.Location, n.Bank, n.Depth,
                CASE WHEN n.Depth IS NOT NULL AND TRIM(CAST(n.Depth AS STRING)) != '' AND UPPER(CAST(n.Location AS STRING)) NOT LIKE '%AMB%' THEN 'TempPipe' ELSE 'Brine' END as PipeType
                FROM `{NODE_REGISTRY_TABLE}` n
                WHERE (n.End_Date IS NULL OR TRIM(CAST(n.End_Date AS STRING)) = '') AND n.NodeNum IS NOT NULL
            ),
            MappedNodes AS (
                SELECT b.NodeNum, b.RawProject, b.Location, b.Bank, b.Depth, b.PipeType, a.FullProjectID,
                ROW_NUMBER() OVER(
                    PARTITION BY b.NodeNum ORDER BY CASE WHEN a.FullProjectID IS NULL THEN 99 WHEN b.Phase IS NULL OR TRIM(CAST(b.Phase AS STRING)) = '' THEN 1 WHEN UPPER(a.FullProjectID) LIKE CONCAT('%PHASE%', TRIM(CAST(b.Phase AS STRING))) THEN 1 WHEN UPPER(a.FullProjectID) LIKE CONCAT('%PHASE %', TRIM(CAST(b.Phase AS STRING))) THEN 1 ELSE 2 END ASC
                ) as rn
                FROM BaseNodes b LEFT JOIN ActiveJobs a ON TRIM(b.RawProject) LIKE CONCAT(a.RootJob, '%')
            ),
            RegisteredNodes AS (
                SELECT NodeNum, Location, Bank, Depth, PipeType, COALESCE(FullProjectID, RawProject) as FinalProjectLabel
                FROM MappedNodes WHERE rn = 1 AND (FullProjectID IS NOT NULL OR UPPER(RawProject) LIKE '%OFFICE%')
            ),
            NodeTimelineHistory AS (
                SELECT m.NodeNum, m.temperature, m.rssi, m.timestamp,
                LAG(m.temperature) OVER (PARTITION BY m.NodeNum ORDER BY m.timestamp ASC) as last_temp_val
                FROM `{MASTER_VIEW}` m WHERE m.timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
            ),
            NodeAggregates AS (
                SELECT h.NodeNum, MAX(h.timestamp) as last_seen_ts,
                ARRAY_AGG(h.temperature ORDER BY h.timestamp DESC LIMIT 1)[OFFSET(0)] as latest_temp,
                MAX(CASE WHEN h.timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR) THEN ABS(h.temperature - h.last_temp_val) ELSE 0 END) as max_single_spike_24h,
                ARRAY_AGG(h.rssi ORDER BY h.timestamp DESC LIMIT 1)[OFFSET(0)] as latest_rssi,
                AVG(CASE WHEN h.timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR) THEN h.rssi END) as avg_rssi_24h,
                COUNT(CASE WHEN h.timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR) THEN h.timestamp END) as total_pings_24h,
                COUNT(DISTINCT CASE WHEN h.timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR) THEN TIMESTAMP_TRUNC(h.timestamp, HOUR) END) as hours_with_data_24h
                FROM NodeTimelineHistory h GROUP BY h.NodeNum
            ),
            SpikeCounts AS (
                SELECT h.NodeNum, COUNTIF(h.timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR) AND h.last_temp_val IS NOT NULL AND ((r.PipeType = 'TempPipe' AND ABS(h.temperature - h.last_temp_val) > 1.0) OR (r.PipeType = 'Brine' AND ABS(h.temperature - h.last_temp_val) > 8.0))) as spike_count_24h
                FROM NodeTimelineHistory h JOIN RegisteredNodes r ON h.NodeNum = r.NodeNum GROUP BY h.NodeNum
            )
            SELECT r.FinalProjectLabel as Project, r.NodeNum, r.Location, r.Bank, r.Depth, r.PipeType,
            a.last_seen_ts, a.latest_temp, a.max_single_spike_24h, a.latest_rssi, a.avg_rssi_24h,
            COALESCE(a.hours_with_data_24h, 0) as hours_with_data_24h,
            COALESCE(a.total_pings_24h, 0) as checkin_frequency_24h,
            ROUND((COALESCE(a.hours_with_data_24h, 0) / 24.0) * 100, 1) as Signal_Reliability_Pct,
            COALESCE(s.spike_count_24h, 0) as spike_count_24h
            FROM RegisteredNodes r
            LEFT JOIN NodeAggregates a ON r.NodeNum = a.NodeNum
            LEFT JOIN SpikeCounts s ON r.NodeNum = s.NodeNum
            ORDER BY r.FinalProjectLabel ASC, r.Location ASC
        """
        try:
            return client.query(alert_q).to_dataframe()
        except Exception:
            return pd.DataFrame()

    # =========================================================================
    # TAB 1: DATA LOOKUP
    # =========================================================================
    @output
    @render.ui
    def lookup_scope_info():
        proj = selected_project() if callable(selected_project) else selected_project
        lbl = "Global Fleet" if proj == "All Projects" else proj
        return ui.p(f"🎯 Search Scope: {lbl}", class_="text-info")

    @output
    @render.ui
    def diag_filter_ui():
        reg_df = get_reg_df()
        proj = selected_project() if callable(selected_project) else selected_project
        
        if proj != "All Projects":
            job_num = proj.split('-')[0].strip()
            reg_df = reg_df[reg_df['Project'].astype(str).str.startswith(job_num)]
            
        if input.diag_search_mode() == "Filter Mappings":
            avail_locs = sorted(reg_df['Location'].dropna().unique().tolist(), key=natural_sort_key)
            return ui.div(
                ui.input_select("diag_loc_select", "Physical Location Context", avail_locs),
                ui.output_ui("diag_node_select_ui")
            )
        else:
            all_active = sorted(reg_df['NodeNum'].dropna().astype(str).unique().tolist(), key=natural_sort_key)
            return ui.input_selectize("diag_direct_node", "Search and Select Node ID(s):", all_active, multiple=True)

    @output
    @render.ui
    def diag_node_select_ui():
        if input.diag_search_mode() != "Filter Mappings" or not hasattr(input, 'diag_loc_select'): return ui.HTML("")
        
        reg_df = get_reg_df()
        proj = selected_project() if callable(selected_project) else selected_project
        if proj != "All Projects":
            job_num = proj.split('-')[0].strip()
            reg_df = reg_df[reg_df['Project'].astype(str).str.startswith(job_num)]
            
        matching_nodes = sorted(reg_df[reg_df['Location'] == input.diag_loc_select()]['NodeNum'].dropna().unique().tolist(), key=natural_sort_key)
        return ui.input_selectize("diag_mapped_nodes", "Select Target Node(s) to Inspect", matching_nodes, multiple=True)

    @reactive.Calc
    def get_target_nodes():
        if input.diag_search_mode() == "Filter Mappings":
            return input.diag_mapped_nodes() if hasattr(input, 'diag_mapped_nodes') else []
        return input.diag_direct_node() if hasattr(input, 'diag_direct_node') else []

    @reactive.Calc
    def get_node_history():
        nodes = get_target_nodes()
        if not nodes: return pd.DataFrame()
        
        days_map = {"30 Days": 30, "60 Days": 60, "90 Days": 90, "1 Year": 365, "All Time": 5000}
        lookback = days_map.get(input.diag_time_opt(), 30)
        
        node_q = f"""
            SELECT timestamp, temperature, rssi, Location, Bank, Depth, Project, SensorStatus, NodeNum
            FROM `{MASTER_VIEW}`
            WHERE NodeNum IN UNNEST({nodes})
              AND timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {lookback} DAY)
            ORDER BY timestamp DESC
        """
        try:
            df = client.query(node_q).to_dataframe()
            if not df.empty:
                tz = display_tz() if callable(display_tz) else display_tz
                if df['timestamp'].dt.tz is None:
                    df['timestamp'] = df['timestamp'].dt.tz_localize('UTC')
                df['timestamp'] = df['timestamp'].dt.tz_convert(tz)
            return df
        except Exception:
            return pd.DataFrame()

    @output
    @render.ui
    def diag_node_metrics_ui():
        nodes = get_target_nodes()
        if not nodes: return ui.p("👆 Please select at least one sensor.")
        
        history_df = get_node_history()
        if history_df.empty: return ui.p("No recent telemetry found.")
        
        ulbl = unit_label() if callable(unit_label) else unit_label
        
        # Build UI blocks dynamically for each selected node
        blocks = []
        for node in nodes:
            node_df = history_df[history_df['NodeNum'] == node]
            if node_df.empty: continue
            
            meta_row = node_df.iloc[0]
            blocks.append(ui.card(
                ui.h5(f"📡 Diagnostics: {node}"),
                ui.layout_columns(
                    ui.value_box("Current Temp", f"{meta_row['temperature']:.1f}{ulbl}"),
                    ui.value_box("Latest Location", str(meta_row['Location'])),
                    ui.value_box("Records Scanned", f"{len(node_df):,}")
                )
            ))
        return ui.div(*blocks)

    @output
    @render_plotly
    def diag_trend_chart():
        df = get_node_history()
        if df.empty: return go.Figure().update_layout(title="No Data")
        
        ulbl = unit_label() if callable(unit_label) else unit_label
        fig = px.line(df, x='timestamp', y='temperature', color='NodeNum', labels={'timestamp': 'Time', 'temperature': f'Temperature ({ulbl})'})
        
        if input.diag_show_ambient():
            # Minimal implementation for ambient overlay
            pass # Expand if needed
            
        fig.update_layout(plot_bgcolor='white', hovermode='x unified', margin=dict(l=0, r=0, t=20, b=0))
        return fig

    # =========================================================================
    # TAB 3: NODE ALERTS
    # =========================================================================
    @output
    @render.ui
    def alert_scope_info():
        proj = selected_project() if callable(selected_project) else selected_project
        lbl = "Global Fleet" if proj == "All Projects" else proj
        return ui.h4(f"🔍 Alerts: {lbl}")

    @output
    @render.data_frame
    def alert_summary_df():
        df = get_alert_df()
        if df.empty: return pd.DataFrame()
        
        proj = selected_project() if callable(selected_project) else selected_project
        if proj != "All Projects":
            df = df[df['Project'].str.lower() == proj.lower()]
            
        summary = {"Total": len(df), "Missing": len(df[df['last_seen_ts'].isnull()]), "Working Fine": len(df[df['last_seen_ts'].notnull()])}
        return render.DataGrid(pd.DataFrame([summary]))

    # =========================================================================
    # TAB 4: BAD ACTORS & RELIABILITY
    # =========================================================================
    @output
    @render.ui
    def rel_global_metrics_ui():
        df = get_alert_df()
        if df.empty: return ui.p("No network data available.")
        
        proj = selected_project() if callable(selected_project) else selected_project
        if proj != "All Projects":
            df = df[df['Project'].str.lower() == proj.lower()]
            
        avg_rel = df['Signal_Reliability_Pct'].mean() if not df.empty else 0
        
        return ui.layout_columns(
            ui.value_box("Total Active Sensors", str(len(df))),
            ui.value_box("Global Fleet Reliability", f"{avg_rel:.1f}%"),
            ui.value_box("Locations Monitored", str(df['Location'].nunique()))
        )
        
    @reactive.Effect
    def update_rel_drilldown():
        df = get_alert_df()
        if not df.empty:
            locs = sorted(df['Location'].dropna().unique().tolist())
            ui.update_select("rel_loc_drilldown", choices=locs)

    @output
    @render.data_frame
    def rel_drilldown_df():
        df = get_alert_df()
        loc = input.rel_loc_drilldown()
        if df.empty or not loc: return pd.DataFrame()
        
        disp_cols = ['NodeNum', 'Depth', 'Bank', 'Signal_Reliability_Pct', 'latest_rssi', 'avg_rssi_24h', 'spike_count_24h']
        loc_df = df[df['Location'] == loc].sort_values(by='Signal_Reliability_Pct', ascending=True)
        return render.DataGrid(loc_df[disp_cols])
