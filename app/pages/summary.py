from shiny import ui, render, reactive, module
import pandas as pd

# Internal Data & Config
from app.utils.config import PROJECT_REGISTRY_TABLE, NODE_REGISTRY_TABLE, MASTER_VIEW

def get_summary_data(client, selected_project, show_archived, approved_only=False):
    """
    Pure Python function to retrieve all summary data matrices.
    Returns: (active_projs_df, pool_df, tel_df, appr_df, error_msg)
    """
    if client is None: return None, None, None, None, "Database connection unavailable."

    status_filter = "" if show_archived else "AND UPPER(TRIM(CAST(ShowActive AS STRING))) IN ('TRUE', 'YES', '1')"
    
    project_filter = ""
    if selected_project and selected_project != "All Projects":
        job_root = str(selected_project).split('-')[0].strip()
        project_filter = f"AND CAST(Project AS STRING) LIKE '{job_root}%'"

    proj_q = f"""
        SELECT 
            CAST(Project AS STRING) as Project, 
            ProjectName, 
            ProjectStatus,
            ShowActive,
            Date_Freezedown, 
            Date_Maintenance 
        FROM `{PROJECT_REGISTRY_TABLE}`
        WHERE UPPER(Project) NOT LIKE '%OFFICE%'
          {status_filter}
          {project_filter}
        ORDER BY Project
    """
    
    try: 
        active_projs = client.query(proj_q).to_dataframe()
    except Exception as e: 
        return None, None, None, None, f"Project Registry failed: {e}"

    if active_projs.empty:
        return pd.DataFrame(), None, None, None, "No projects found in registry matching the criteria."

    pool_q = f"""
        SELECT CAST(Project AS STRING) as Project, Phase, System, UPPER(CAST(Location AS STRING)) as Location, COUNT(DISTINCT NodeNum) as total_assigned
        FROM `{NODE_REGISTRY_TABLE}`
        WHERE UPPER(Project) NOT LIKE '%OFFICE%'
          AND (End_Date IS NULL OR TRIM(CAST(End_Date AS STRING)) = '')
        GROUP BY 1, 2, 3, 4
    """
    pool_df = client.query(pool_q).to_dataframe()
    pool_df[['Phase', 'System', 'Location']] = pool_df[['Phase', 'System', 'Location']].fillna('')

    # Inject approval filter logic for the client portal
    approval_cond = "AND UPPER(CAST(approval_status AS STRING)) = 'TRUE'" if approved_only else ""

    summary_q = f"""
        WITH raw_data AS (
            SELECT Project, Phase, System, Bank, Location, Depth, temperature, timestamp, NodeNum
            FROM `{MASTER_VIEW}`
            WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 48 HOUR)
              AND Project IS NOT NULL
              AND UPPER(Project) NOT LIKE '%OFFICE%'
              {approval_cond}
        ),
        MaxTime AS (
            SELECT MAX(timestamp) as max_ts FROM raw_data
        )
        SELECT 
            r.Project, r.Phase, r.System, r.Bank, r.Location, r.Depth, r.NodeNum,
            MIN(CASE WHEN r.timestamp >= TIMESTAMP_SUB(m.max_ts, INTERVAL 1 HOUR) THEN r.temperature END) as min_now,
            MAX(CASE WHEN r.timestamp >= TIMESTAMP_SUB(m.max_ts, INTERVAL 1 HOUR) THEN r.temperature END) as max_now,
            MIN(CASE WHEN r.timestamp >= TIMESTAMP_SUB(m.max_ts, INTERVAL 24 HOUR) THEN r.temperature END) as min_24h,
            MAX(CASE WHEN r.timestamp >= TIMESTAMP_SUB(m.max_ts, INTERVAL 24 HOUR) THEN r.temperature END) as max_24h,
            COUNTIF(r.timestamp >= TIMESTAMP_SUB(m.max_ts, INTERVAL 1 HOUR)) as checkins_1h,
            COUNTIF(r.timestamp >= TIMESTAMP_SUB(m.max_ts, INTERVAL 6 HOUR)) as checkins_6h,
            COUNTIF(r.timestamp >= TIMESTAMP_SUB(m.max_ts, INTERVAL 24 HOUR)) as checkins_24h,
            ARRAY_AGG(r.temperature ORDER BY r.timestamp DESC LIMIT 1)[OFFSET(0)] as latest_temp,
            MAX(r.timestamp) as latest_ts
        FROM raw_data r CROSS JOIN MaxTime m
        GROUP BY 1, 2, 3, 4, 5, 6, 7
    """
    
    try:
        tel_df = client.query(summary_q).to_dataframe()
        if not tel_df.empty:
            tel_df[['Phase', 'System', 'Bank', 'Location']] = tel_df[['Phase', 'System', 'Bank', 'Location']].fillna('')
    except Exception as e:
        return active_projs, pool_df, None, None, f"Telemetry query failed: {e}"

    # Calculate absolute latest approval timestamp per job
    appr_q = f"""
        SELECT 
            TRIM(SPLIT(CAST(Project AS STRING), '-')[OFFSET(0)]) as RootJob,
            MAX(timestamp) as last_approved_ts
        FROM `{MASTER_VIEW}`
        WHERE UPPER(CAST(approval_status AS STRING)) = 'TRUE'
          AND UPPER(Project) NOT LIKE '%OFFICE%'
          {project_filter}
        GROUP BY RootJob
    """
    try:
        appr_df = client.query(appr_q).to_dataframe()
    except:
        appr_df = pd.DataFrame()

    return active_projs, pool_df, tel_df, appr_df, None

# =============================================================================
# SHINY UI MODULE
# =============================================================================
@module.ui
def summary_ui():
    """Defines the visual layout for the Summary page."""
    return ui.div(
        ui.output_ui("dashboard_header"),
        ui.hr(),
        ui.output_ui("project_cards_ui")
    )

# =============================================================================
# SHINY SERVER MODULE
# =============================================================================
@module.server
def summary_server(input, output, session, client, selected_project, global_show_archived, unit_mode, unit_label, display_tz, global_show_ambient):
    """Handles reactive database fetching and rendering for the Summary page."""

    @output
    @render.ui
    def dashboard_header():
        # Always return the Global headers regardless of the sidebar project selection
        show_arch = global_show_archived() if callable(global_show_archived) else global_show_archived

        if show_arch:
            return ui.h2("🌐 Global Project Summary (Includes Archived)")
        else:
            return ui.h2("🌐 Global Active Project Summary")

    @output
    @render.ui
    def project_cards_ui():
        if client is None:
            return ui.p("Database connection unavailable.", class_="text-danger")

        # Resolve reactive dependencies
        show_arch = global_show_archived() if callable(global_show_archived) else global_show_archived
        tz = display_tz() if callable(display_tz) else display_tz
        u_mode = unit_mode() if callable(unit_mode) else unit_mode
        u_lbl = unit_label() if callable(unit_label) else unit_label
        show_amb = global_show_ambient() if callable(global_show_ambient) else global_show_ambient

        # --- THE FIX: Force "All Projects" to bypass the sidebar dropdown entirely ---
        # Unpack 5 values including the newly returned appr_df dataframe
        active_projs, pool_df, tel_df, appr_df, err = get_summary_data(client, "All Projects", show_arch, approved_only=False)

        if err:
            return ui.p(err, class_="text-danger")
        if active_projs is None or active_projs.empty:
            return ui.p("No active projects found matching the criteria.", class_="text-muted")

        def build_metric_card(title, g_df, target_temp):
            if g_df.empty or g_df['latest_temp'].isnull().all():
                return ui.card(ui.h6(title), ui.p("No recent data", class_="text-muted"), style="background-color: #f8f9fa;")

            latest_val = g_df['latest_temp'].mean()
            c_min, c_max = g_df['min_now'].min(), g_df['max_now'].max()
            m24, x24 = g_df['min_24h'].min(), g_df['max_24h'].max()

            def convert(v):
                if pd.isnull(v) or pd.isna(v): return None
                return (v - 32) * 5/9 if u_mode == "Celsius" else v

            l_conv, c_min, c_max, m24, x24 = map(convert, [latest_val, c_min, c_max, m24, x24])

            pct_html = ""
            if target_temp is not None:
                total_valid_nodes = g_df['NodeNum'].nunique()
                if total_valid_nodes > 0:
                    nodes_meeting = g_df[g_df['latest_temp'] <= target_temp]['NodeNum'].nunique()
                    pct = (nodes_meeting / total_valid_nodes) * 100
                    color = "green" if pct == 100 else "#FF8C00" if pct > 0 else "gray"
                    display_target = (target_temp - 32) * 5/9 if u_mode == "Celsius" else target_temp
                    pct_html = f"<span style='color:{color}; font-size:0.85rem; font-weight:bold;'>{pct:.0f}%</span> <span style='font-size:0.85rem; color:{color};'>Nodes ≤ {display_target:.1f}{u_lbl}</span>"

            curr_str = f"{c_min:.1f} to {c_max:.1f}{u_lbl}" if c_min is not None else "No Data"
            hist_str = f"{m24:.1f} to {x24:.1f}{u_lbl}" if m24 is not None else "No Data"

            return ui.card(
                ui.h6(title, style="margin-bottom: 0px;"),
                ui.h2(f"{l_conv:.1f}{u_lbl}", style="margin-top: 5px; margin-bottom: 0px; color: #1f77b4;"),
                ui.HTML(pct_html) if pct_html else ui.HTML("<div style='height: 18px;'></div>"),
                ui.hr(style="margin-top: 10px; margin-bottom: 10px;"),
                ui.HTML(f"<div style='font-size: 0.8rem; line-height: 1.3; color: #555;'><b>Current Range:</b> {curr_str}<br><b>24h Range:</b> {hist_str}</div>"),
                style="border-top: 4px solid #1f77b4;"
            )

        # Build cards for each active project
        cards = []
        for _, row in active_projs.iterrows():
            p_project = str(row['Project']).strip()
            p_name = row['ProjectName'] if pd.notnull(row['ProjectName']) else p_project
            is_active = str(row.get('ShowActive', 'FALSE')).strip().upper() in ['TRUE', 'YES', '1']
            p_status = str(row.get('ProjectStatus', 'Archived')).strip()
            if not p_status or p_status.lower() in ['nan', 'none']: p_status = "Archived"
            
            f_date = row.get('Date_Freezedown')
            m_date = row.get('Date_Maintenance') 

            def is_valid_date(val):
                if pd.isnull(val): return False
                val_str = str(val).strip().lower()
                return val_str not in ['', 'nan', 'nat', 'none', '<na>']

            header_html = "<div style='text-align: right;'><small>Start: Not Set</small></div>"

            if is_valid_date(f_date):
                f_date_dt = pd.to_datetime(f_date).date()
                total_freezedown_days = (pd.Timestamp.now(tz=tz).date() - f_date_dt).days
                
                if is_valid_date(m_date):
                    m_date_dt = pd.to_datetime(m_date).date()
                    time_to_freeze = (m_date_dt - f_date_dt).days
                    maintenance_days = (pd.Timestamp.now(tz=tz).date() - m_date_dt).days
                    
                    header_html = f"""
                        <div style='text-align: right; line-height: 1.3;'>
                            🗓️ <b>Freezedown: {max(0, total_freezedown_days)} Days</b><br>
                            <span style='color: #28a745; display: inline-block; margin-top: 4px;'>✅ <b>Full Freezedown Provided</b></span><br>
                            <small style='color: #666;'>Time to Freeze: {max(0, time_to_freeze)} Days | In Maintenance: {max(0, maintenance_days)} Days</small>
                        </div>
                    """
                else:
                    header_html = f"""
                        <div style='text-align: right; line-height: 1.3;'>
                            🗓️ <b>Freezedown: {max(0, total_freezedown_days)} Days</b><br>
                            <small style='color: #666;'>Start Freezedown: {f_date_dt.strftime('%b %d, %Y')}</small>
                        </div>
                    """

            if not is_active:
                cards.append(ui.card(
                    ui.layout_columns(
                        ui.div(ui.h4(f"📦 {p_name}"), ui.p(f"Project Status: {p_status}", class_="text-muted")),
                        ui.HTML(header_html)
                    ),
                    style="background-color: #f8f9fa; opacity: 0.8;"
                ))
                continue

            job_num = p_project.split('-')[0].strip()
            target_phase = "1" if "Phase 1" in p_project or "Phase1" in p_project else "2" if "Phase 2" in p_project or "Phase2" in p_project else "3" if "Phase 3" in p_project or "Phase3" in p_project else ""

            pool_matches = pool_df[(pool_df['Project'].str.startswith(job_num)) & ((pool_df['Phase'] == target_phase) | (target_phase == ""))]
            systems = sorted(list(set([str(s).strip() for s in pool_matches['System'].unique() if str(s).strip()])))
            if not systems: systems = [""] 

            tel_matches = pd.DataFrame(columns=tel_df.columns) if tel_df.empty else tel_df[(tel_df['Project'].str.startswith(job_num)) & ((tel_df['Phase'] == target_phase) | (target_phase == ""))]

            for sys in systems:
                block_pool = pool_matches if sys == "" else pool_matches[(pool_matches['System'] == sys) | (pool_matches['Location'] == 'AMBIENT')]
                total_assigned = block_pool['total_assigned'].sum() if not block_pool.empty else 0
                
                sys_tel = tel_matches if sys == "" or tel_matches.empty else tel_matches[(tel_matches['System'] == sys) | (tel_matches['Location'].astype(str).str.upper() == 'AMBIENT')]

                if total_assigned == 0 and sys_tel.empty:
                    continue 

                title_ext = []
                if target_phase: title_ext.append(f"Phase {target_phase}")
                if sys: title_ext.append(f"System {sys}")
                title_suffix = f" ({', '.join(title_ext)})" if title_ext else ""

                if not sys_tel.empty:
                    active_1h = sys_tel[sys_tel['checkins_1h'] > 0]['NodeNum'].nunique()
                    active_6h = sys_tel[sys_tel['checkins_6h'] > 0]['NodeNum'].nunique()
                    active_24h = sys_tel[sys_tel['checkins_24h'] > 0]['NodeNum'].nunique()
                    latest_ts = sys_tel['latest_ts'].max()
                    
                    if pd.notnull(latest_ts):
                        # Ensure latest_ts is timezone-aware (UTC) before converting
                        if latest_ts.tzinfo is None:
                            latest_ts_utc = latest_ts.tz_localize('UTC')
                        else:
                            latest_ts_utc = latest_ts

                        # Convert to selected local timezone
                        latest_ts_local = latest_ts_utc.tz_convert(tz)
                        elapsed_mins = int((pd.Timestamp.now(tz='UTC') - latest_ts_utc).total_seconds() / 60)
                        pulse = f"🟢 **Live** ({elapsed_mins}m ago)" if elapsed_mins <= 60 else f"🟠 **Delayed** ({elapsed_mins}m ago)" if elapsed_mins <= 180 else f"🔴 **Stale** ({elapsed_mins // 60}h ago)"
                        
                        # Apply %Z format to local timestamp
                        data_age_str = f"⏱️ **Data Pulse:** {pulse} — *(Last sync: {latest_ts_local.strftime('%b %d, %H:%M %Z')})*"
                    else:
                        data_age_str = "⏱️ **Data Pulse:** 🔴 **No Data (Last 48h)**"
                else:
                    active_1h = active_6h = active_24h = 0
                    data_age_str = "⏱️ **Data Pulse:** 🔴 **No Data (Last 48h)**"
                
                status_color = "🟢" if active_24h >= total_assigned and total_assigned > 0 else "🔴" if active_24h == 0 else "🟠"
                
                # Render the Official Data Approved Strip
                last_appr_ts = pd.NaT
                if not appr_df.empty:
                    job_match = appr_df[appr_df['RootJob'] == job_num]
                    if not job_match.empty:
                        last_appr_ts = job_match['last_approved_ts'].max()
                
                if pd.notnull(last_appr_ts):
                    if last_appr_ts.tzinfo is None:
                        last_appr_ts = last_appr_ts.tz_localize('UTC')
                    
                    # Apply %Z format to approval timestamp
                    appr_str = last_appr_ts.tz_convert(tz).strftime('%b %d, %Y %I:%M %p %Z')
                    appr_html = f"<div style='margin-top: 8px;'><span style='background-color: #d1ecf1; color: #0c5460; padding: 4px 8px; border-radius: 4px; font-size: 0.85em; border: 1px solid #bee5eb;'>✅ <b>Official Data Approved Through:</b> {appr_str}</span></div>"
                else:
                    appr_html = f"<div style='margin-top: 8px;'><span style='background-color: #f8d7da; color: #721c24; padding: 4px 8px; border-radius: 4px; font-size: 0.85em; border: 1px solid #f5c6cb;'>⚠️ <b>No Approved Data Available</b></span></div>"

                status_html = f"{status_color} <b>Hardware Status:</b> <code>{active_1h}</code> (1h) | <code>{active_6h}</code> (6h) | <code>{active_24h}</code> (24h) | Assigned Pool: <code>{total_assigned}</code><br>{data_age_str}{appr_html}"

                # Metric Columns
                metric_columns = []
                if not sys_tel.empty:
                    is_amb_col = sys_tel['Location'].astype(str).str.upper() == 'AMBIENT'
                    is_tp_col = sys_tel['Depth'].notnull() & (sys_tel['Depth'].astype(str).str.strip() != '') & ~is_amb_col
                    is_s_col = (sys_tel['Bank'].astype(str).str.startswith('S') | sys_tel['Location'].astype(str).str.startswith('S')) & ~is_amb_col & ~is_tp_col
                    is_r_col = (sys_tel['Bank'].astype(str).str.startswith('R') | sys_tel['Location'].astype(str).str.startswith('R')) & ~is_amb_col & ~is_tp_col

                    groups_data = [
                        ("📥 Supply", sys_tel[is_s_col], -10), 
                        ("📤 Return", sys_tel[is_r_col], 0), 
                        ("📏 TempPipes", sys_tel[is_tp_col], 32)
                    ]
                    if show_amb: groups_data.append(("☁️ Ambient", sys_tel[is_amb_col], None))

                    for title, g_df, tgt in groups_data:
                        metric_columns.append(build_metric_card(title, g_df, tgt))

                cards.append(
                    ui.card(
                        ui.layout_columns(
                            ui.div(
                                ui.h4(f"🏗️ {p_name}{title_suffix}"),
                                ui.a(f"🔗 External Client Portal", href=f"https://soilfreeze-client.share.connect.posit.cloud/?job={job_num}", target="_blank")
                            ),
                            ui.HTML(header_html)
                        ),
                        ui.HTML(f"<div style='background-color: #f8f9fa; padding: 10px; border-radius: 5px; margin-top: 10px; margin-bottom: 15px;'>{status_html}</div>"),
                        ui.layout_columns(*metric_columns) if metric_columns else ui.p(f"No recent telemetry received for {p_project}{title_suffix}.", class_="text-muted"),
                        style="margin-bottom: 20px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);"
                    )
                )

        return ui.div(*cards)
