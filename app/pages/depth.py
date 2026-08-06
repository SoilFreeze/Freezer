import pandas as pd
import plotly.graph_objects as go
import re
import sys
from app.data.processor import get_universal_portal_data
from app.pages.admin import natural_sort_key

# --- SAFE FRAMEWORK DETECTION ---
try:
    import streamlit as st
    # Smart detection: If Shiny is running the app, it will be in sys.modules.
    if 'shiny' in sys.modules:
        HAS_STREAMLIT = False
    else:
        HAS_STREAMLIT = True
except ImportError:
    HAS_STREAMLIT = False

def get_ui_state(var_name, opt_val, default):
    """Safely retrieves state from explicit arguments or Streamlit session."""
    if opt_val is not None:
        return opt_val
    if HAS_STREAMLIT:
        return st.session_state.get(var_name, default)
    return default

def generate_depth_figures(selected_project, unit_label, display_tz, orientation="vertical", 
                           lookback_weeks=None, show_masked=None, show_baddata=None, unit_mode=None):
    """
    Pure Python function that generates the depth profile figures.
    Returns a dictionary of figures mapped to their location names.
    """
    _lookback_weeks = get_ui_state("global_lookback_weeks_slider", lookback_weeks, 5)
    _show_masked = get_ui_state('global_show_masked', show_masked, False)
    _show_baddata = get_ui_state('global_show_baddata', show_baddata, False)
    _unit_mode = get_ui_state("unit_mode", unit_mode, "Fahrenheit")
    
    is_horizontal = str(orientation).strip().lower() == "horizontal"
    chart_type_label = "Distance" if is_horizontal else "Depth"
    
    if not selected_project or selected_project == "All Projects":
        return {}, chart_type_label, "Please select a specific project to view profiles."

    # 1. Calculate fetch days
    real_f_date = get_ui_state('project_metadata', None, {}).get('Date_Freezedown')
    now_utc = pd.Timestamp.now(tz='UTC')
    fetch_days = _lookback_weeks * 7 
    
    if pd.notnull(real_f_date):
        parsed_f_date = pd.to_datetime(real_f_date)
        if parsed_f_date.tzinfo is None:
            parsed_f_date = parsed_f_date.tz_localize('UTC')
        
        days_since_freeze = (now_utc - parsed_f_date).days
        if days_since_freeze > fetch_days:
            fetch_days = days_since_freeze + 2

    # 2. Fetch Data
    try:
        p_df = get_universal_portal_data(
            selected_project, 
            lookback_days=fetch_days,
            show_masked=_show_masked,
            show_baddata=_show_baddata
        )
    except TypeError:
        p_df = get_universal_portal_data(selected_project)
            
    if p_df is None or p_df.empty:
        return {}, chart_type_label, "No data found for this project."

    # 3. Auto-filter by Phase
    phase_match = re.search(r'(?i)Phase\s*(\d+)', selected_project)
    if phase_match:
        target_phase = phase_match.group(1)
        p_df = p_df[p_df['Phase'].astype(str) == target_phase]

    # 4. Filter systems (Only if using Streamlit for now to preserve internal UI behavior)
    if HAS_STREAMLIT:
        avail_systems = sorted([str(s) for s in p_df['System'].dropna().unique() if str(s).strip()])
        if avail_systems:
            sel_systems = st.sidebar.multiselect("Filter by System", avail_systems, default=avail_systems, key="depth_sys")
            if sel_systems:
                p_df = p_df[p_df['System'].astype(str).isin(sel_systems)]

    p_df['Depth_Num'] = pd.to_numeric(p_df['Depth'], errors='coerce')
    p_df = p_df[p_df['temperature'] <= 120.0]
    
    clean_loc = p_df['Location'].fillna('').astype(str).str.upper()
    clean_bank = p_df['Bank'].fillna('').astype(str).str.upper()

    is_bank_or_amb = (
        clean_loc.str.startswith('S') | clean_loc.str.startswith('R') | clean_loc.str.contains('BANK') |
        clean_bank.str.startswith('S') | clean_bank.str.startswith('R') | clean_bank.str.contains('BANK') |
        clean_loc.str.contains('AMB') | clean_bank.str.contains('AMB')
    )
    
    p_df = p_df[~is_bank_or_amb]
    depth_df = p_df.dropna(subset=['Depth_Num', 'Location']).copy()
    
    if depth_df.empty:
        return {}, chart_type_label, "No Temp Pipe sensors with valid numeric 'Depth' entries found."

    freeze_pt = 0 if _unit_mode == "Celsius" else 32
    cutoff_date = now_utc - pd.Timedelta(weeks=_lookback_weeks)
    mondays = pd.date_range(start=cutoff_date, end=now_utc, freq='W-MON')
    
    locations = sorted(depth_df['Location'].unique(), key=natural_sort_key)
    figures_dict = {}
    
    # 5. Build the Figures
    for loc in locations:
        loc_data = depth_df[depth_df['Location'] == loc].copy()
        
        if loc_data['timestamp'].dt.tz is None:
            loc_data['timestamp'] = loc_data['timestamp'].dt.tz_localize('UTC')
        loc_data['timestamp_local'] = loc_data['timestamp'].dt.tz_convert(display_tz)
        
        fig = go.Figure()

        # A. BASELINE
        baseline_ts = loc_data['timestamp_local'].min()
        b_window = loc_data[
            (loc_data['timestamp_local'] >= baseline_ts - pd.Timedelta(hours=12)) & 
            (loc_data['timestamp_local'] <= baseline_ts + pd.Timedelta(hours=12))
        ]
        
        baseline_date_str = ""
        snap_base = pd.DataFrame()
        if not b_window.empty:
            baseline_date_str = baseline_ts.strftime('%Y-%m-%d')
            snap_base = (
                b_window.assign(diff=(b_window['timestamp_local'] - baseline_ts).abs())
                .sort_values(['NodeNum', 'diff'])
                .drop_duplicates('NodeNum')
                .sort_values('Depth_Num')
            )

        # B. RECENT 6 AM
        loc_data['date_str'] = loc_data['timestamp_local'].dt.strftime('%Y-%m-%d')
        loc_data['hour_int'] = loc_data['timestamp_local'].dt.hour
        
        recent_6am_date_str = ""
        recent_profile_rows = []
        
        if not loc_data.empty:
            sorted_all_dates = sorted(loc_data['date_str'].unique(), reverse=True)
            
            for candidate_date in sorted_all_dates:
                if candidate_date == baseline_date_str:
                    continue
                
                day_pool = loc_data[loc_data['date_str'] == candidate_date]
                if day_pool.empty:
                    continue
                    
                recent_6am_date_str = candidate_date
                for node_id, node_group in day_pool.groupby('NodeNum'):
                    exact_6am = node_group[node_group['hour_int'] == 6]
                    if not exact_6am.empty:
                        recent_profile_rows.append(exact_6am.sort_values('timestamp_local').iloc[-1])
                    else:
                        node_group = node_group.assign(hour_dist=(node_group['hour_int'] - 6).abs())
                        best_fallback_row = node_group.sort_values(by=['hour_dist', 'timestamp_local']).iloc[0]
                        recent_profile_rows.append(best_fallback_row)
                break

        snap_recent = pd.DataFrame(recent_profile_rows).sort_values('Depth_Num') if recent_profile_rows else pd.DataFrame()

        # C. HISTORICAL
        # --- NEW: Explicit color cycle to replace Streamlit's missing theme ---
        hist_colors = ['#636EFA', '#EF553B', '#00CC96', '#AB63FA', '#19D3F3', '#FF6692', '#B6E880', '#FF97FF', '#FECB52']
        c_idx = 0
        
        for m_date in mondays:
            target_ts = m_date.replace(hour=6, minute=0, second=0)
            current_loop_date = target_ts.strftime('%Y-%m-%d')
            
            if current_loop_date == baseline_date_str or current_loop_date == recent_6am_date_str:
                continue
                
            window = loc_data[
                (loc_data['timestamp_local'] >= target_ts - pd.Timedelta(hours=12)) & 
                (loc_data['timestamp_local'] <= target_ts + pd.Timedelta(hours=12))
            ]
            
            if not window.empty:
                snap_week = (
                    window.assign(diff=(window['timestamp_local'] - target_ts).abs())
                    .sort_values(['NodeNum', 'diff'])
                    .drop_duplicates('NodeNum')
                    .sort_values('Depth_Num')
                )
                
                temps = snap_week['temperature']
                if _unit_mode == "Celsius": temps = (temps - 32) * 5/9
                
                x_week = snap_week['Depth_Num'] if is_horizontal else temps
                y_week = temps if is_horizontal else snap_week['Depth_Num']
                ht_week = f"Date: {current_loop_date}<br>{chart_type_label}: %{{{'x' if is_horizontal else 'y'}}}ft<br>Temp: %{{{'y' if is_horizontal else 'x'}:.1f}}{unit_label}<extra></extra>"

                # Grab a new color from the sequence for this specific week
                h_color = hist_colors[c_idx % len(hist_colors)]
                c_idx += 1

                fig.add_trace(go.Scatter(
                    x=x_week, y=y_week, mode='lines+markers', name=current_loop_date,
                    # --- NEW: Inject the explicit color into the line and marker dictionaries ---
                    line=dict(color=h_color, shape='spline', smoothing=1.1, width=1.5), 
                    marker=dict(color=h_color, size=4), 
                    hovertemplate=ht_week
                ))
                
        # D. RECENT LINE INJECTION
        if not snap_recent.empty:
            recent_temps = snap_recent['temperature']
            if _unit_mode == "Celsius": recent_temps = (recent_temps - 32) * 5/9
            
            x_rec = snap_recent['Depth_Num'] if is_horizontal else recent_temps
            y_rec = recent_temps if is_horizontal else snap_recent['Depth_Num']
            ht_rec = f"Most Recent: %{{text}}<br>{chart_type_label}: %{{{'x' if is_horizontal else 'y'}}}ft<br>Temp: %{{{'y' if is_horizontal else 'x'}:.1f}}{unit_label}<extra></extra>"

            fig.add_trace(go.Scatter(
                x=x_rec, y=y_rec, mode='lines+markers', name=f'<b>Most Recent ({recent_6am_date_str} 6AM*)</b>',
                line=dict(color='#ff7f0e', width=3.5, shape='spline', smoothing=1.1), marker=dict(size=6, color='#ff7f0e'),
                hovertemplate=ht_rec, text=snap_recent['timestamp_local'].dt.strftime('%b %d, %H:%M')
            ))

        # E. BASELINE INJECTION
        if not snap_base.empty:
            b_temps = snap_base['temperature']
            if _unit_mode == "Celsius": b_temps = (b_temps - 32) * 5/9
            
            x_base = snap_base['Depth_Num'] if is_horizontal else b_temps
            y_base = b_temps if is_horizontal else snap_base['Depth_Num']
            ht_base = f"Baseline: {baseline_date_str}<br>{chart_type_label}: %{{{'x' if is_horizontal else 'y'}}}ft<br>Temp: %{{{'y' if is_horizontal else 'x'}:.1f}}{unit_label}<extra></extra>"

            fig.add_trace(go.Scatter(
                x=x_base, y=y_base, mode='lines+markers', name=f'<b>Baseline ({baseline_date_str})</b>',
                line=dict(color='black', width=3, dash='dash'), marker=dict(size=5, color='black'), hovertemplate=ht_base
            ))

        if is_horizontal:
            fig.add_hline(y=freeze_pt, line_width=2, line_dash="solid", line_color="#ADD8E6")
        else:
            fig.add_vline(x=freeze_pt, line_width=2, line_dash="solid", line_color="#ADD8E6")

        max_depth = loc_data['Depth_Num'].max()
        y_limit = int(((max_depth // 10) + 1) * 10) if pd.notnull(max_depth) else 50

        dist_axis = dict(
            title=f"{chart_type_label} (ft)", range=[0, y_limit] if is_horizontal else [y_limit, 0], 
            dtick=10, minor=dict(dtick=2, showgrid=True, gridcolor='#f8f8f8'),
            gridcolor='Silver', showline=True, linewidth=2, linecolor='black', mirror=True
        )
        temp_axis = dict(
            title=f"Temperature ({unit_label})", range=[-20, 80], dtick=10,
            minor=dict(dtick=2, showgrid=True, gridcolor='#f8f8f8'),
            gridcolor='Gainsboro', showline=True, linewidth=2, linecolor='black', mirror=True
        )

        fig.update_layout(
            title=f"<b>Temp vs {chart_type_label} - {loc}</b>",
            plot_bgcolor='white', height=800, margin=dict(l=60, r=40, t=80, b=80), 
            xaxis=dist_axis if is_horizontal else temp_axis,
            yaxis=temp_axis if is_horizontal else dist_axis,
            legend=dict(orientation="h", y=-0.1, xanchor="center", x=0.5)
        )
        
        figures_dict[loc] = fig
        
    return figures_dict, chart_type_label, None


def render_depth_charts(selected_project, unit_label, display_tz, orientation="vertical"):
    """
    Streamlit UI Wrapper. This ensures your internal app doesn't break!
    """
    if not HAS_STREAMLIT:
        return
        
    figures, chart_label, error_msg = generate_depth_figures(
        selected_project, unit_label, display_tz, orientation
    )
    
    st.header(f"📏 {chart_label} Profile Analysis: {selected_project}")
    
    if error_msg:
        st.info(f"💡 {error_msg}")
        return
        
    for loc, fig in figures.items():
        with st.expander(f"📍 Temp vs {chart_label} - {loc}", expanded=True):
            st.plotly_chart(fig, use_container_width=True, key=f"depth_cht_{selected_project}_{loc}")
