import streamlit as st
import pandas as pd
import os

# Import from your existing modular architecture
from app.data.processor import get_universal_portal_data, get_bq_client
from app.components.charts import build_high_speed_graph, build_cropped_site_map
from app.pages.summary import render_summary_dashboard
from app.pages.depth import render_depth_charts
from app.utils.config import PROJECT_ID, DATASET_ID

# ===============================================================
# 1. AUTHENTICATION & UI LOCKDOWN
# ===============================================================
TARGET_JOB_NUMBER = None
if "JOB_NUMBER" in st.secrets:
    TARGET_JOB_NUMBER = str(st.secrets["JOB_NUMBER"])
elif "job_number" in st.secrets:
    TARGET_JOB_NUMBER = str(st.secrets["job_number"])
else:
    TARGET_JOB_NUMBER = st.query_params.get("job", None)

page_title = f"SoilFreeze Portal #{TARGET_JOB_NUMBER}" if TARGET_JOB_NUMBER else "SoilFreeze Client Portal"
st.set_page_config(page_title=page_title, layout="wide")

# Completely hide the internal sidebar navigation for clients
st.markdown("""
    <style> 
        [data-testid="stSidebarNav"] {display: none;} 
        [data-testid="collapsedControl"] {display: none;}
    </style>
""", unsafe_allow_html=True)

if not TARGET_JOB_NUMBER:
    st.title("🌐 SoilFreeze Client Portal")
    st.info("Please enter your assigned Job Number to view project telemetry.")
    
    manual_job = st.text_input("Job Number:", placeholder="e.g., 2527")
    if not manual_job:
        st.stop()
        
    st.query_params["job"] = str(manual_job)
    st.rerun()

# ===============================================================
# 2. CLIENT DEFAULTS (Injecting required session states)
# ===============================================================
st.session_state['selected_project'] = TARGET_JOB_NUMBER
st.session_state['global_show_archived'] = False
st.session_state['global_show_ambient'] = True
st.session_state['global_show_elevation'] = False
st.session_state['global_show_map'] = True
st.session_state['global_show_baddata'] = False
st.session_state['global_show_masked'] = False
st.session_state['global_show_ref'] = True
st.session_state['global_lookback_days'] = 42 

unit_mode = "Fahrenheit"
unit_label = "°F"
st.session_state["unit_mode"] = unit_mode
st.session_state["unit_label"] = unit_label

display_tz = "US/Pacific" 
st.session_state["display_tz"] = display_tz
active_refs = [(32.0, "Freezing")]
st.session_state["active_refs"] = active_refs

# ===============================================================
# 3. INTERACTIVE FRAGMENT (The Speed Fix & Map Restoration)
# ===============================================================
@st.fragment
def render_interactive_timeline(df, job_num, bq_client):
    st.write("### 📈 Timeline Analysis")
    
    available_phases = sorted([str(p) for p in df['Phase'].dropna().unique() if str(p).strip().upper() not in ['NAN', 'NONE', '', 'UNASSIGNED']])
    available_systems = sorted([str(s) for s in df['System'].dropna().unique() if str(s).strip().upper() not in ['NAN', 'NONE', '', 'UNASSIGNED']])
    
    selected_phase = None
    selected_systems = []
    
    # ONLY render the filter columns if there are actual choices to make
    if len(available_phases) > 1 or len(available_systems) > 1:
        col1, col2 = st.columns(2)
        with col1:
            if len(available_phases) > 1:
                selected_phase = st.selectbox("📂 Select Project Phase:", ["All Phases"] + available_phases)
        with col2:
            if len(available_systems) > 1:
                selected_systems = st.multiselect("⚙️ Filter by System:", options=available_systems, default=[])
                
    # Apply Filters
    filtered_df = df.copy()
    if selected_phase and selected_phase != "All Phases":
        filtered_df = filtered_df[filtered_df['Phase'].astype(str) == str(selected_phase)]
    if selected_systems:
        filtered_df = filtered_df[filtered_df['System'].astype(str).isin(selected_systems)]
        
    if filtered_df.empty:
        st.warning("No data found for the selected filters.")
        return

    st.divider()
    
    # FETCH MAP COORDINATES ONCE
    try:
        map_query = f"""
            SELECT Project, Location, Map_X, Map_Y, Image_Name 
            FROM `{PROJECT_ID}.{DATASET_ID}.TempPipeLoc` 
            WHERE CAST(Project AS STRING) = '{job_num}'
        """
        df_all_locs = bq_client.query(map_query).to_dataframe()
    except Exception as e:
        df_all_locs = pd.DataFrame()
        st.error(f"⚠️ Map Registry Error: {e}")

    # Render Charts & Maps side-by-side
    unique_locations = [loc for loc in filtered_df['Location'].dropna().unique() if 'AMBIENT' not in str(loc).upper()]
    
    start_date = filtered_df['timestamp'].min()
    end_date = filtered_df['timestamp'].max()

    for loc in sorted(unique_locations):
        loc_data = filtered_df[filtered_df['Location'] == loc]
        if loc_data.empty: continue
            
        # Check if this specific location has map coordinates
        has_map = False
        if not df_all_locs.empty and 'Location' in df_all_locs.columns:
            has_map = str(loc) in df_all_locs['Location'].astype(str).values
            
        if has_map and st.session_state.get('global_show_map', True):
            # Split layout: Chart on left, Map on right
            col_chart, col_map = st.columns([3, 1])
            with col_chart:
                fig = build_high_speed_graph(
                    client=bq_client,  
                    df=loc_data, 
                    title=f"Thermal Trends: {loc}",
                    start_view=start_date, 
                    end_view=end_date, 
                    active_refs=active_refs,
                    unit_mode=unit_mode,
                    unit_label=unit_label,
                    display_tz=display_tz,
                    curve_id=f"{job_num}-{loc}"
                )
                if fig:
                    st.plotly_chart(fig, use_container_width=True)

            with col_map:
                site_map_fig = build_cropped_site_map(
                    project_id=job_num, 
                    location_name=loc, 
                    df_map=df_all_locs,
                    as_built_dir="as_builts"
                )
                if site_map_fig:
                    st.plotly_chart(site_map_fig, use_container_width=True)
                else:
                    st.info(f"🗺️ Map image for {job_num} not found in the as_builts folder.")
        else:
            # Full width layout if no map data exists for this hole
            fig = build_high_speed_graph(
                client=bq_client,  
                df=loc_data, 
                title=f"Thermal Trends: {loc}",
                start_view=start_date, 
                end_view=end_date, 
                active_refs=active_refs,
                unit_mode=unit_mode,
                unit_label=unit_label,
                display_tz=display_tz,
                curve_id=f"{job_num}-{loc}"
            )
            if fig:
                st.plotly_chart(fig, use_container_width=True)

        st.markdown("---")

# ===============================================================
# 4. MAIN APP EXECUTION
# ===============================================================
def main():
    client = get_bq_client()
    if client is None: return

    with st.spinner("Synchronizing official records..."):
        master_df = get_universal_portal_data(TARGET_JOB_NUMBER)
        
    if master_df is None or master_df.empty:
        st.warning("⚠️ No approved data records available yet.")
        return

    job_num_root = str(TARGET_JOB_NUMBER).split('-')[0].strip()
    
    tabs = st.tabs([
        "🏠 Summary", 
        "📈 Timeline Analysis", 
        "📏 Profile Analysis", 
        "🗺️ As Built"
    ])
    
    with tabs[0]:
        render_summary_dashboard(TARGET_JOB_NUMBER, unit_label, unit_mode, display_tz)
        
    with tabs[1]:
        render_interactive_timeline(master_df, job_num_root, client)
        
    with tabs[2]:
        render_depth_charts(TARGET_JOB_NUMBER, unit_label, display_tz, orientation="vertical")
        
    with tabs[3]:
        st.subheader("🗺️ Site As-Builts")
        as_builts_dir = "as_builts"
        if os.path.exists(as_builts_dir):
            found_images = sorted([
                f for f in os.listdir(as_builts_dir) 
                if f.startswith(job_num_root) and f.lower().endswith(('.png', '.jpg', '.jpeg'))
            ])
            if found_images:
                for img_name in found_images:
                    st.image(os.path.join(as_builts_dir, img_name), caption=img_name, use_container_width=True)
                    st.markdown("---")
            else:
                st.info("ℹ️ Site plan is currently being processed or has not been assigned.")
        else:
            st.warning("As-builts directory not found.")

if __name__ == "__main__":
    main()
